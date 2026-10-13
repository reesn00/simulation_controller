"""L2 浏览器媒体探针（JS-aware，依赖 PageDriver 协议）。

2026-10-13 增量二十三配套：阶段 4 L2 `media_probe` 落地。

设计纪律（与设计文档 §3.2 / §6 + 增量二十三对齐）：

1. **不封装浏览器**——L2 复用现有 ``PageDriver`` 协议（抽象）。
   CLI 默认用 ``FakePageDriver``（in-process mock），真实 ObscuraDriver
   接入留给后续 L4/L5 串接时一并做。

2. **不 import archive/v1**——只参照同款逻辑（playwright_mcp.py:121-128
   的 currentTime 推进探测）。**绝对 import**（CLAUDE.md R1）。

3. **凭据闸门 fail-closed**——复用 :func:`scan_credentials`。URL 命中凭据
   → return 空 ``BrowserProbeResult`` + ``note="credential_leak"``，不抛。

4. **不重叠 L0/L1**：
   - L0 (static_probe): HTTP only，不跑 JS
   - L1 (ffprobe): HTTP + ffprobe 直连媒体文件
   - L2 (media_probe): JS-aware 浏览器探 DOM + playback 状态

5. **驱动未起 → fail-closed**：driver is None / driver.goto 抛
   → return 空 result，**不抛**。让上层决定 pass / 下结论。

6. **iframe 1 跳解析**——与 L1 同款 ``MAX_IFRAME_HOPS = 1``。
   超 1 层嵌套的 iframe 不递归（fail-fast）。

7. **6 字段信号对齐 mcp_probe.py ``EXPECTED_PRIMITIVES``**：
   ``play_started`` / ``timeupdate_progress`` / ``current_time_sec`` /
   ``frames_decoded`` / ``black_screen`` / ``media_error``。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from trajectory_pipeline.executor.archive import (
    CredentialLeak,
    scan_credentials,
    credential_samples,
)
from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    PageDriver,
)

#: 两次 currentTime 采样间隔（秒）。比对时间增长是否真推进。
PROBE_WAIT_S: float = 2.0

#: 单步超时（秒）。包括 goto + count + 2 次 evaluate + wait。
DEFAULT_TIMEOUT_S: float = 15.0

#: iframe 1 跳解析上限。与 L1 同款：> 1 层嵌套不递归拉。
MAX_IFRAME_HOPS: int = 1

#: HTMLMediaElement.error.code 取值（HTML5 spec §4.8.10.5）。
#: L2 把 ``media_error > 0`` 当作 broken 触发条件之一。
MEDIA_ERR_OK: int = 0
MEDIA_ERR_ABORTED: int = 1
MEDIA_ERR_NETWORK: int = 2
MEDIA_ERR_DECODE: int = 3
MEDIA_ERR_SRC_NOT_SUPPORTED: int = 4

#: black_screen 判定阈值：videoWidth=0 即认为「没画面」。
#: 注意：readyState >= HAVE_METADATA（=1）才算「已加载元数据」，
#: 才认为「应当有画面但没有」是异常。
BLACK_SCREEN_READY_STATE_MIN: int = 1

#: timeupdate_progress 阈值（秒）。两次采样 currentTime 差超过此值
#: 算「真推进」；低于此值视为节流 / 卡帧。
TIMEUPDATE_PROGRESS_THRESHOLD_S: float = 0.05

#: partial_free 触发上限（秒）。current_time_sec 低于此值且真推进
#: → partial_free 候选。与 L1 ``PARTIAL_FREE_MAX_DURATION_S`` 同值对齐。
PARTIAL_FREE_MAX_CURRENT_TIME_S: float = 60.0

#: JS 探针一次 evaluate 拿全部媒体状态。
#: 返回 {count, media: [{tag, currentTime, duration, paused, readyState,
#:   networkState, ended, videoWidth, videoHeight, errorCode, framesDecoded}]}
#: 适用于 :meth:`PageDriver.evaluate` 求值。
MEDIA_PROBE_JS: str = """
() => {
  const ms = Array.from(document.querySelectorAll('video, audio'));
  return {
    count: ms.length,
    media: ms.map(m => ({
      tag: m.tagName.toLowerCase(),
      currentTime: m.currentTime || 0,
      duration: (isFinite(m.duration) && m.duration > 0) ? m.duration : null,
      paused: !!m.paused,
      readyState: m.readyState,
      networkState: m.networkState,
      ended: !!m.ended,
      videoWidth: m.videoWidth || 0,
      videoHeight: m.videoHeight || 0,
      errorCode: m.error ? m.error.code : 0,
      framesDecoded: (m.getVideoPlaybackQuality
                     && typeof m.getVideoPlaybackQuality === 'function')
                    ? (m.getVideoPlaybackQuality().totalVideoFrames || 0)
                    : 0
    }))
  };
}
""".strip()

#: iframe 计数用 selector（与 L1 ``count("video, audio")`` 同款）。
MEDIA_SELECTOR: str = "video, audio"
IFRAME_SELECTOR: str = "iframe"


@dataclass(frozen=True, slots=True)
class MediaSignal:
    """L2 媒体探针单帧信号（6 字段对齐 mcp_probe.py ``EXPECTED_PRIMITIVES``）。

    字段：
    - play_started: ``currentTime > 0``（任何媒体元素） → True
    - timeupdate_progress: 两次采样 currentTime 增长 > 阈值 → True
    - current_time_sec: 第二次采样时的最大 currentTime
    - frames_decoded: 第二次采样时的最大已解码帧数
    - black_screen: ``<video>`` 元素 videoWidth=0 且 readyState >= 1
    - media_error: HTMLMediaElement.error.code（0 = OK）
    """

    play_started: bool
    timeupdate_progress: bool
    current_time_sec: float | None
    frames_decoded: int | None
    black_screen: bool
    media_error: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "play_started": self.play_started,
            "timeupdate_progress": self.timeupdate_progress,
            "current_time_sec": self.current_time_sec,
            "frames_decoded": self.frames_decoded,
            "black_screen": self.black_screen,
            "media_error": self.media_error,
        }


@dataclass(frozen=True, slots=True)
class BrowserProbeResult:
    """L2 浏览器媒体探针结果（探测层，不下分支结论）。"""

    requested_url: str
    final_url: str
    driver_name: str
    ok: bool                       # 驱动与来源页都正常拉取
    media_count: int               # <video>/<audio> 元素数
    signal: MediaSignal            # 见上 6 字段
    error: str = ""                # 异常时的描述（空 = 无异常）
    elapsed_ms: int = 0
    note: str = ""                 # 给人看：credential_leak / timeout / driver_missing


def _parse_media_payload(payload: Mapping[str, Any]) -> MediaSignal:
    """把 JS evaluate 返回的 dict 解析为 :class:`MediaSignal`（无 progress 字段）。

    progress 字段由 :func:`_combine_progress` 在两次采样间计算。
    """
    media_list = payload.get("media") or []
    if not media_list:
        return MediaSignal(
            play_started=False,
            timeupdate_progress=False,
            current_time_sec=None,
            frames_decoded=None,
            black_screen=False,
            media_error=None,
        )
    play_started = any(float(m.get("currentTime") or 0) > 0 for m in media_list)
    error_codes = [
        int(m.get("errorCode") or 0) for m in media_list if int(m.get("errorCode") or 0) > 0
    ]
    media_error = error_codes[0] if error_codes else None
    times = [float(m.get("currentTime") or 0) for m in media_list]
    current_time = max(times) if times else None
    frames = [int(m.get("framesDecoded") or 0) for m in media_list]
    frames_decoded = max(frames) if frames else None
    black_screen = any(
        m.get("tag") == "video"
        and int(m.get("videoWidth") or 0) == 0
        and int(m.get("readyState") or 0) >= BLACK_SCREEN_READY_STATE_MIN
        for m in media_list
    )
    return MediaSignal(
        play_started=play_started,
        timeupdate_progress=False,  # 由 _combine_progress 设置
        current_time_sec=current_time,
        frames_decoded=frames_decoded,
        black_screen=black_screen,
        media_error=media_error,
    )


def _combine_progress(s1: MediaSignal, s2: MediaSignal) -> MediaSignal:
    """比对两次采样，产出最终 :class:`MediaSignal`（含 progress 字段）。

    阈值见 :data:`TIMEUPDATE_PROGRESS_THRESHOLD_S`。``s2`` 其它字段优先。
    """
    t1 = s1.current_time_sec
    t2 = s2.current_time_sec
    progressed = (
        t1 is not None and t2 is not None
        and (t2 - t1) > TIMEUPDATE_PROGRESS_THRESHOLD_S
    )
    return MediaSignal(
        play_started=s2.play_started,
        timeupdate_progress=progressed,
        current_time_sec=s2.current_time_sec,
        frames_decoded=s2.frames_decoded,
        black_screen=s2.black_screen,
        media_error=s2.media_error,
    )


def to_branch(result: BrowserProbeResult) -> str | None:
    """L2 探针结果 → 分支映射。

    返回值：
    - ``"broken"``: media_error / black_screen（媒体损坏或黑屏）
    - ``"partial_free"``: 真推进但 current_time < 阈值（试看形态）
    - ``None``: passes（让上层接走）

    关键边界：
    - **不重叠 L0/L1**：L0 已在 HTTP 层判 broken/blocked；L1 已判媒体坏
      与试看。L2 只能新增「JS-aware 黑屏」与「试看形态二次确认」。
    - **不与 LLM 抢**：语义判断（页面文案 / 业务提示）由 LLM 承担，
      L2 只看客观 DOM 状态。
    """
    if not result.ok or result.media_count == 0:
        return None
    sig = result.signal
    if sig.media_error is not None and sig.media_error > MEDIA_ERR_OK:
        return "broken"
    if sig.black_screen:
        return "broken"
    if (
        sig.timeupdate_progress
        and sig.current_time_sec is not None
        and sig.current_time_sec < PARTIAL_FREE_MAX_CURRENT_TIME_S
    ):
        return "partial_free"
    return None


def dump_result(result: BrowserProbeResult, path: Path) -> Path:
    """落盘 :class:`BrowserProbeResult` 为 JSON（UTF-8）。"""
    payload = {
        "requested_url": result.requested_url,
        "final_url": result.final_url,
        "driver_name": result.driver_name,
        "ok": result.ok,
        "media_count": result.media_count,
        "signal": result.signal.to_dict(),
        "error": result.error,
        "elapsed_ms": result.elapsed_ms,
        "note": result.note,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


async def probe(
    driver: PageDriver | None,
    url: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    wait_s: float = PROBE_WAIT_S,
) -> BrowserProbeResult:
    """L2 JS-aware 浏览器媒体探针。

    Args:
        driver: :class:`PageDriver` 实例（抽象协议）；``None`` → fail-closed
        url: 目标 URL
        timeout_s: 单步超时（goto / count / evaluate），默认 15s
        wait_s: 两次 currentTime 采样间隔，默认 2s

    Returns:
        :class:`BrowserProbeResult`；失败/凭据命中 → ``ok=False``，
        ``error`` 填原因，``signal`` 全 ``False`` / ``None``。

    关键边界：
    - **凭据闸门 fail-closed**——URL 命中凭据 → ``note="credential_leak"``，
      **不抛**（与 L1 同款）。
    - **driver 为 ``None``** → ``note="driver_missing"``，不抛。
    - **driver.goto 失败 / 超时** → ``note="driver_error"`` 或 ``"timeout"``。
    - **iframe 1 跳**——与 L1 同款 ``MAX_IFRAME_HOPS = 1``，超过不递归
      （本批简化：仅在 driver 支持时调用 ``count("iframe")`` 留痕）。
    """
    started = time.perf_counter()

    # 凭据闸门
    try:
        hits = scan_credentials(url)
    except CredentialLeak:
        # 兼容 monkeypatched 测试与将来可能直接抛的实现版本。
        hits = ["via-raise"]
    if hits:
        elapsed = int((time.perf_counter() - started) * 1000)
        return BrowserProbeResult(
            requested_url=url,
            final_url=url,
            driver_name=driver.name if driver else "",
            ok=False,
            media_count=0,
            signal=MediaSignal(False, False, None, None, False, None),
            error=(
                f"credential_leak: hits={hits[:3]}"
                + (
                    f"; samples={credential_samples(url)[:2]}"
                    if hits != ["via-raise"]
                    else ""
                )
            ),
            elapsed_ms=elapsed,
            note="credential_leak",
        )

    if driver is None:
        elapsed = int((time.perf_counter() - started) * 1000)
        return BrowserProbeResult(
            requested_url=url,
            final_url=url,
            driver_name="",
            ok=False,
            media_count=0,
            signal=MediaSignal(False, False, None, None, False, None),
            error="driver is None",
            elapsed_ms=elapsed,
            note="driver_missing",
        )

    try:
        await asyncio.wait_for(driver.goto(url), timeout=timeout_s)
        payload_1 = await asyncio.wait_for(
            driver.evaluate(MEDIA_PROBE_JS), timeout=timeout_s
        )
        sig_1 = _parse_media_payload(payload_1)
        await asyncio.sleep(wait_s)
        payload_2 = await asyncio.wait_for(
            driver.evaluate(MEDIA_PROBE_JS), timeout=timeout_s
        )
        sig_2 = _parse_media_payload(payload_2)
        sig = _combine_progress(sig_1, sig_2)
        elapsed = int((time.perf_counter() - started) * 1000)
        count_val = int(payload_1.get("count", 0))
        return BrowserProbeResult(
            requested_url=url,
            final_url=url,
            driver_name=driver.name,
            ok=True,
            media_count=count_val,
            signal=sig,
            elapsed_ms=elapsed,
        )
    except asyncio.TimeoutError:
        elapsed = int((time.perf_counter() - started) * 1000)
        return BrowserProbeResult(
            requested_url=url,
            final_url=url,
            driver_name=driver.name,
            ok=False,
            media_count=0,
            signal=MediaSignal(False, False, None, None, False, None),
            error=f"timeout after {timeout_s}s",
            elapsed_ms=elapsed,
            note="timeout",
        )
    except DriverError as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return BrowserProbeResult(
            requested_url=url,
            final_url=url,
            driver_name=driver.name,
            ok=False,
            media_count=0,
            signal=MediaSignal(False, False, None, None, False, None),
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=elapsed,
            note="driver_error",
        )
    except Exception as exc:  # 探测层兜底：任何异常不外抛
        elapsed = int((time.perf_counter() - started) * 1000)
        return BrowserProbeResult(
            requested_url=url,
            final_url=url,
            driver_name=driver.name,
            ok=False,
            media_count=0,
            signal=MediaSignal(False, False, None, None, False, None),
            error=f"{type(exc).__name__}: {exc}",
            elapsed_ms=elapsed,
        )
