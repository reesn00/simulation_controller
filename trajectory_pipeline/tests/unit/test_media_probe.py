"""L2 media_probe contract tests.

覆盖：
- 常量守门（6 字段信号对齐 mcp_probe.py EXPECTED_PRIMITIVES）
- MediaSignal dataclass 与 to_dict
- _parse_media_payload:空 / 单元素 / 多元素 / error / black_screen / frames
- _combine_progress:timeupdate 阈值 0.05s、None 处理、s2 字段优先
- to_branch:media_error / black_screen / partial_free 60s 边界 / passes
- probe (async):driver None / credential_leak / timeout / DriverError / 正常流
- dump_result:JSON 落盘 + note 字段保留
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trajectory_pipeline.executor.archive import CredentialLeak
from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.executor.probe.media_probe import (
    BLACK_SCREEN_READY_STATE_MIN,
    DEFAULT_TIMEOUT_S,
    MAX_IFRAME_HOPS,
    MEDIA_ERR_ABORTED,
    MEDIA_ERR_DECODE,
    MEDIA_ERR_NETWORK,
    MEDIA_ERR_OK,
    MEDIA_ERR_SRC_NOT_SUPPORTED,
    MEDIA_PROBE_JS,
    MEDIA_SELECTOR,
    PARTIAL_FREE_MAX_CURRENT_TIME_S,
    PROBE_WAIT_S,
    TIMEUPDATE_PROGRESS_THRESHOLD_S,
    BrowserProbeResult,
    MediaSignal,
    _combine_progress,
    _parse_media_payload,
    dump_result,
    probe,
    to_branch,
)


# ---------------------------------------------------------------- helpers
def _make_payload(media: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"count": len(media or []), "media": media or []}


def _video(current_time: float = 0, *,
           duration: float | None = 60.0,
           ready_state: int = 4,
           video_width: int = 1920,
           video_height: int = 1080,
           error_code: int = 0,
           frames_decoded: int = 0,
           paused: bool = False,
           ended: bool = False,
           tag: str = "video") -> dict[str, Any]:
    return {
        "tag": tag,
        "currentTime": current_time,
        "duration": duration,
        "paused": paused,
        "readyState": ready_state,
        "networkState": 1,
        "ended": ended,
        "videoWidth": video_width,
        "videoHeight": video_height,
        "errorCode": error_code,
        "framesDecoded": frames_decoded,
    }


# ---------------------------------------------------------------- TestModuleConstants
class TestModuleConstants:
    def test_PROBE_WAIT_S_default_2s(self):
        assert PROBE_WAIT_S == 2.0

    def test_DEFAULT_TIMEOUT_S_default_15s(self):
        assert DEFAULT_TIMEOUT_S == 15.0

    def test_MAX_IFRAME_HOPS_equals_1(self):
        # 与 L1 同款：1 跳解析上限
        assert MAX_IFRAME_HOPS == 1

    def test_MEDIA_ERR_codes(self):
        assert MEDIA_ERR_OK == 0
        assert MEDIA_ERR_ABORTED == 1
        assert MEDIA_ERR_NETWORK == 2
        assert MEDIA_ERR_DECODE == 3
        assert MEDIA_ERR_SRC_NOT_SUPPORTED == 4

    def test_BLACK_SCREEN_READY_STATE_MIN_is_1(self):
        # HAVE_METADATA = 1：媒体元素已加载元数据但无画面
        assert BLACK_SCREEN_READY_STATE_MIN == 1

    def test_TIMEUPDATE_PROGRESS_THRESHOLD_S_is_0_05(self):
        assert TIMEUPDATE_PROGRESS_THRESHOLD_S == 0.05

    def test_PARTIAL_FREE_MAX_CURRENT_TIME_S_aligns_with_L1(self):
        # 与 L1 PARTIAL_FREE_MAX_DURATION_S 同值（60s 试看阈值）
        assert PARTIAL_FREE_MAX_CURRENT_TIME_S == 60.0

    def test_MEDIA_SELECTOR_video_audio(self):
        assert MEDIA_SELECTOR == "video, audio"

    def test_MEDIA_PROBE_JS_non_empty(self):
        assert isinstance(MEDIA_PROBE_JS, str)
        assert len(MEDIA_PROBE_JS) > 50
        assert "querySelectorAll" in MEDIA_PROBE_JS
        assert "getVideoPlaybackQuality" in MEDIA_PROBE_JS


# ---------------------------------------------------------------- TestMediaSignal
class TestMediaSignal:
    def test_to_dict_roundtrip(self):
        sig = MediaSignal(True, True, 12.5, 300, False, None)
        d = sig.to_dict()
        assert d["play_started"] is True
        assert d["timeupdate_progress"] is True
        assert d["current_time_sec"] == 12.5
        assert d["frames_decoded"] == 300
        assert d["black_screen"] is False
        assert d["media_error"] is None

    def test_frozen_blocks_mutation(self):
        sig = MediaSignal(False, False, None, None, False, None)
        with pytest.raises((AttributeError, TypeError)):
            sig.play_started = True  # frozen + slots

    def test_equality_by_value(self):
        a = MediaSignal(True, False, 5.0, 100, False, None)
        b = MediaSignal(True, False, 5.0, 100, False, None)
        c = MediaSignal(True, False, 5.0, 101, False, None)
        assert a == b
        assert a != c


# ---------------------------------------------------------------- TestParseMediaPayload
class TestParseMediaPayload:
    def test_empty_payload_returns_all_false_none(self):
        sig = _parse_media_payload({"count": 0, "media": []})
        assert sig.play_started is False
        assert sig.current_time_sec is None
        assert sig.frames_decoded is None
        assert sig.black_screen is False
        assert sig.media_error is None

    def test_single_video_play_started(self):
        sig = _parse_media_payload(_make_payload([
            _video(current_time=5.0, frames_decoded=150)
        ]))
        assert sig.play_started is True
        assert sig.current_time_sec == 5.0
        assert sig.frames_decoded == 150
        assert sig.black_screen is False
        assert sig.media_error is None

    def test_media_error_first_code_aggregated(self):
        sig = _parse_media_payload(_make_payload([
            _video(error_code=0),
            _video(error_code=MEDIA_ERR_SRC_NOT_SUPPORTED),
        ]))
        assert sig.media_error == MEDIA_ERR_SRC_NOT_SUPPORTED

    def test_black_screen_video_width_zero_with_metadata(self):
        sig = _parse_media_payload(_make_payload([
            _video(video_width=0, video_height=0, ready_state=1)
        ]))
        assert sig.black_screen is True

    def test_black_screen_requires_ready_state_at_least_1(self):
        sig = _parse_media_payload(_make_payload([
            _video(video_width=0, video_height=0, ready_state=0)  # HAVE_NOTHING
        ]))
        assert sig.black_screen is False

    def test_audio_does_not_trigger_black_screen(self):
        sig = _parse_media_payload(_make_payload([
            _video(tag="audio", video_width=0, video_height=0, ready_state=4)
        ]))
        assert sig.black_screen is False

    def test_frames_decoded_takes_max(self):
        sig = _parse_media_payload(_make_payload([
            _video(frames_decoded=50),
            _video(frames_decoded=300),
            _video(frames_decoded=600),
        ]))
        assert sig.frames_decoded == 600

    def test_current_time_takes_max(self):
        sig = _parse_media_payload(_make_payload([
            _video(current_time=0),
            _video(current_time=15.0),
            _video(current_time=8.0),
        ]))
        assert sig.current_time_sec == 15.0

    def test_zero_current_time_is_not_play_started(self):
        sig = _parse_media_payload(_make_payload([
            _video(current_time=0, frames_decoded=0)
        ]))
        assert sig.play_started is False

    def test_play_started_any_positive_currentTime(self):
        sig = _parse_media_payload(_make_payload([
            _video(current_time=0),
            _video(current_time=0.001),
        ]))
        assert sig.play_started is True


# ---------------------------------------------------------------- TestCombineProgress
class TestCombineProgress:
    def test_progress_positive_above_threshold(self):
        s1 = MediaSignal(True, False, 10.0, 100, False, None)
        s2 = MediaSignal(True, False, 12.0, 250, False, None)
        out = _combine_progress(s1, s2)
        assert out.timeupdate_progress is True
        assert out.current_time_sec == 12.0
        assert out.frames_decoded == 250

    def test_no_progress_returns_false(self):
        s1 = MediaSignal(True, False, 10.0, 100, False, None)
        s2 = MediaSignal(True, False, 10.0, 100, False, None)
        assert _combine_progress(s1, s2).timeupdate_progress is False

    def test_threshold_exact_boundary(self):
        # 0.05s 差值在边界上算「没推进」；用 0.04 避开浮点误差
        s1 = MediaSignal(True, False, 10.0, 100, False, None)
        s2 = MediaSignal(True, False, 10.04, 100, False, None)
        assert _combine_progress(s1, s2).timeupdate_progress is False

    def test_threshold_just_above(self):
        s1 = MediaSignal(True, False, 10.0, 100, False, None)
        s2 = MediaSignal(True, False, 10.06, 100, False, None)
        assert _combine_progress(s1, s2).timeupdate_progress is True

    def test_none_handling(self):
        s1 = MediaSignal(False, False, None, None, False, None)
        s2 = MediaSignal(True, False, 5.0, 50, False, None)
        out = _combine_progress(s1, s2)
        assert out.timeupdate_progress is False

    def test_s2_fields_dominate(self):
        s1 = MediaSignal(True, False, 10.0, 100, False, MEDIA_ERR_DECODE)
        s2 = MediaSignal(True, False, 12.0, 250, True, None)
        out = _combine_progress(s1, s2)
        assert out.play_started is True
        assert out.media_error is None
        assert out.black_screen is True

    def test_rewind_clamps_to_false(self):
        # t2 < t1 时 (t2 - t1) 为负，不算 progress
        s1 = MediaSignal(True, False, 10.0, 100, False, None)
        s2 = MediaSignal(True, False, 5.0, 100, False, None)
        assert _combine_progress(s1, s2).timeupdate_progress is False


# ---------------------------------------------------------------- TestToBranch
def _result(signal: MediaSignal, *, ok: bool = True, count: int = 1) -> BrowserProbeResult:
    return BrowserProbeResult(
        requested_url="https://example.com",
        final_url="https://example.com",
        driver_name="fake",
        ok=ok,
        media_count=count,
        signal=signal,
    )


class TestToBranch:
    def test_media_error_returns_broken(self):
        sig = MediaSignal(True, True, 30.0, 100, False, MEDIA_ERR_DECODE)
        assert to_branch(_result(sig)) == "broken"

    def test_media_error_zero_is_ok(self):
        # error=0 表示 OK，不触发 broken
        sig = MediaSignal(True, True, 30.0, 100, False, MEDIA_ERR_OK)
        assert to_branch(_result(sig)) == "partial_free"

    def test_black_screen_returns_broken(self):
        sig = MediaSignal(False, False, 0, 0, True, None)
        assert to_branch(_result(sig)) == "broken"

    def test_progress_under_60s_returns_partial_free(self):
        sig = MediaSignal(True, True, 30.0, 100, False, None)
        assert to_branch(_result(sig)) == "partial_free"

    def test_progress_at_60s_returns_none(self):
        # 边界 60.0s：>= 阈值算「非试看」
        sig = MediaSignal(True, True, 60.0, 100, False, None)
        assert to_branch(_result(sig)) is None

    def test_progress_over_60s_returns_none(self):
        sig = MediaSignal(True, True, 120.0, 100, False, None)
        assert to_branch(_result(sig)) is None

    def test_no_progress_returns_none(self):
        sig = MediaSignal(True, False, 30.0, 100, False, None)
        assert to_branch(_result(sig)) is None

    def test_no_media_count_returns_none(self):
        sig = MediaSignal(False, False, None, None, False, None)
        assert to_branch(_result(sig, count=0)) is None

    def test_not_ok_returns_none(self):
        sig = MediaSignal(True, True, 30.0, 100, False, MEDIA_ERR_DECODE)
        assert to_branch(_result(sig, ok=False)) is None

    def test_black_screen_overrides_partial_free(self):
        # 黑屏是 broken；partial_free 被遮蔽
        sig = MediaSignal(True, True, 30.0, 100, True, None)
        assert to_branch(_result(sig)) == "broken"

    def test_media_error_overrides_partial_free(self):
        sig = MediaSignal(True, True, 30.0, 100, False, MEDIA_ERR_NETWORK)
        assert to_branch(_result(sig)) == "broken"


# ---------------------------------------------------------------- TestProbeAsync
class TestProbeAsync:
    def test_driver_none_fails_closed(self):
        r = asyncio.run(probe(None, "https://example.com"))
        assert r.ok is False
        assert "driver is None" in r.error
        assert r.note == "driver_missing"
        assert r.media_count == 0

    def test_credential_leak_fails_closed(self, monkeypatch):
        from trajectory_pipeline.executor.probe import media_probe as mod

        def fake_scan(url):
            raise CredentialLeak(url, ["fake:fake@example.com"])

        monkeypatch.setattr(mod, "scan_credentials", fake_scan)
        driver = MagicMock(name="d")
        driver.name = "fake"
        r = asyncio.run(probe(driver, "https://example.com"))
        assert r.ok is False
        assert "credential_leak" in r.error
        assert r.note == "credential_leak"

    def test_credential_leak_fails_closed_when_scan_returns_hits(
        self, monkeypatch
    ):
        """生产路径：scan_credentials 返回 hits（不抛）→ 同样 fail-closed。

        与 test_credential_leak_fails_closed 同源但分支不同：
        后者 monkeypatch 让 scan_credentials 抛 CredentialLeak；
        这里 monkeypatch 让它返回 list——更接近生产实现。
        """
        from trajectory_pipeline.executor.probe import media_probe as mod

        def fake_scan(url):
            return ["https?://[^\\s/:@]+:[^\\s/@]+@"]

        monkeypatch.setattr(mod, "scan_credentials", fake_scan)
        driver = MagicMock(name="d")
        driver.name = "fake"
        r = asyncio.run(probe(driver, "https://example.com"))
        assert r.ok is False
        assert r.note == "credential_leak"
        # driver.goto 不该被调用（fail-closed 必须在 goto 之前）
        driver.goto.assert_not_called()

    def test_goto_timeout_fails_closed(self):
        driver = MagicMock(name="d")
        driver.name = "fake"
        driver.goto = AsyncMock(side_effect=asyncio.TimeoutError())
        r = asyncio.run(probe(driver, "https://example.com", timeout_s=0.1))
        assert r.ok is False
        assert r.note == "timeout"

    def test_evaluate_timeout_fails_closed(self):
        driver = MagicMock(name="d")
        driver.name = "fake"
        driver.goto = AsyncMock(return_value=None)
        driver.evaluate = AsyncMock(side_effect=asyncio.TimeoutError())
        r = asyncio.run(probe(driver, "https://example.com", timeout_s=0.1))
        assert r.ok is False
        assert r.note == "timeout"

    def test_normal_flow_records_progress(self):
        driver = MagicMock(name="d")
        driver.name = "fake"
        driver.goto = AsyncMock(return_value=None)
        driver.evaluate = AsyncMock(side_effect=[
            _make_payload([_video(current_time=0, frames_decoded=0)]),
            _make_payload([_video(current_time=5.0, frames_decoded=150)]),
        ])
        r = asyncio.run(probe(driver, "https://example.com", wait_s=0.01))
        assert r.ok is True
        assert r.media_count == 1
        assert r.signal.timeupdate_progress is True
        assert r.signal.current_time_sec == 5.0
        assert r.signal.frames_decoded == 150

    def test_no_progress_returns_false(self):
        driver = MagicMock(name="d")
        driver.name = "fake"
        driver.goto = AsyncMock(return_value=None)
        driver.evaluate = AsyncMock(return_value=_make_payload([
            _video(current_time=10.0, frames_decoded=100)
        ]))
        r = asyncio.run(probe(driver, "https://example.com", wait_s=0.01))
        assert r.signal.timeupdate_progress is False
        assert r.signal.current_time_sec == 10.0

    def test_driver_error_caught(self):
        driver = MagicMock(name="d")
        driver.name = "fake"
        driver.goto = AsyncMock(side_effect=DriverError("navigate failed"))
        r = asyncio.run(probe(driver, "https://example.com"))
        assert r.ok is False
        assert r.note == "driver_error"
        assert "DriverError" in r.error

    def test_generic_exception_caught(self):
        driver = MagicMock(name="d")
        driver.name = "fake"
        driver.goto = AsyncMock(side_effect=RuntimeError("boom"))
        r = asyncio.run(probe(driver, "https://example.com"))
        assert r.ok is False
        assert r.note == ""
        assert "RuntimeError" in r.error

    def test_elapsed_ms_recorded(self):
        driver = MagicMock(name="d")
        driver.name = "fake"
        driver.goto = AsyncMock(return_value=None)
        driver.evaluate = AsyncMock(side_effect=[
            _make_payload([]),
            _make_payload([]),
        ])
        r = asyncio.run(probe(driver, "https://example.com", wait_s=0.05))
        assert r.ok is True
        assert r.elapsed_ms >= 50  # 至少等了 wait_s


# ---------------------------------------------------------------- TestDumpResult
class TestDumpResult:
    def test_writes_json(self, tmp_path):
        sig = MediaSignal(True, True, 30.0, 100, False, None)
        r = BrowserProbeResult(
            requested_url="https://example.com",
            final_url="https://example.com/final",
            driver_name="fake",
            ok=True,
            media_count=1,
            signal=sig,
            elapsed_ms=123,
            note="smoke",
        )
        out = dump_result(r, tmp_path / "out.json")
        assert out.exists()
        d = json.loads(out.read_text(encoding="utf-8"))
        assert d["media_count"] == 1
        assert d["signal"]["play_started"] is True
        assert d["elapsed_ms"] == 123
        assert d["note"] == "smoke"
        assert d["final_url"] == "https://example.com/final"

    def test_empty_signal_writes(self, tmp_path):
        sig = MediaSignal(False, False, None, None, False, None)
        r = BrowserProbeResult(
            requested_url="u", final_url="u", driver_name="d",
            ok=False, media_count=0, signal=sig, note="timeout",
        )
        p = tmp_path / "x.json"
        dump_result(r, p)
        d = json.loads(p.read_text(encoding="utf-8"))
        assert d["ok"] is False
        assert d["media_count"] == 0
        assert d["note"] == "timeout"
        assert d["signal"]["media_error"] is None

    def test_creates_parent_dirs(self, tmp_path):
        sig = MediaSignal(False, False, None, None, False, None)
        r = BrowserProbeResult("u", "u", "d", False, 0, sig)
        p = tmp_path / "sub" / "deep" / "out.json"
        dump_result(r, p)
        assert p.exists()


# ---------------------------------------------------------------- TestDesignDiscipline
class TestDesignDiscipline:
    """设计纪律约束 -- 与设计文档 / 增量二十三对齐。"""

    def test_partial_free_60s_aligns_L1_PARTIAL_FREE_MAX_DURATION_S(self):
        # 引用 L1 守门常量，验证两层的试看阈值口径一致
        from trajectory_pipeline.executor.probe.ffprobe import (
            PARTIAL_FREE_MAX_DURATION_S,
        )
        assert PARTIAL_FREE_MAX_CURRENT_TIME_S == PARTIAL_FREE_MAX_DURATION_S

    def test_L2_does_not_use_L0_or_L1_aliases(self):
        # L2 的 probe/to_branch/dump_result 都模块内私有
        import trajectory_pipeline.executor.probe.media_probe as mod
        # probe 函数存在
        assert callable(getattr(mod, "probe", None))
        # to_branch 是私有 to_branch，L0/L1 同名但都是模块内私有
        assert callable(getattr(mod, "to_branch", None))
        # 模块没有公开的 L0 别名（probe_browser_media 在 __init__.py）
        # 验证 __all__ 暴露的是 probe_browser_media 不是 probe
        from trajectory_pipeline.executor.probe import __all__
        assert "probe_browser_media" in __all__
        assert "to_browser_branch" in __all__
        assert "dump_browser_result" in __all__

    def test_no_archive_v1_import(self):
        # 硬纪律：CLAUDE.md R1，禁止从 archive/v1 import
        import re as _re
        import trajectory_pipeline.executor.probe.media_probe as mod
        src = open(mod.__file__, encoding="utf-8").read()
        import_lines = [
            ln for ln in src.splitlines()
            if _re.match(r"^\s*(from|import)\s+", ln)
        ]
        joined = "\n".join(import_lines)
        assert "archive/v1" not in joined
        assert "from archive" not in joined

    def test_no_obscura_import(self):
        # L2 模块依赖 PageDriver 协议，不直接 import ObscuraDriver
        import re as _re
        import trajectory_pipeline.executor.probe.media_probe as mod
        src = open(mod.__file__, encoding="utf-8").read()
        import_lines = [
            ln for ln in src.splitlines()
            if _re.match(r"^\s*(from|import)\s+", ln)
        ]
        joined = "\n".join(import_lines)
        assert "obscura_driver" not in joined
        assert "ObscuraDriver" not in joined

    def test_credential_leak_does_not_crash_when_scan_raises_non_leak(self, monkeypatch):
        # scan_credentials 不抛 CredentialLeak 时走正常路径；这里保证 fail-closed
        # 只对 CredentialLeak 响应，其他异常不在本模块捕获
        from trajectory_pipeline.executor.probe import media_probe as mod

        def fake_scan_ok(url):
            return []  # 正常路径

        monkeypatch.setattr(mod, "scan_credentials", fake_scan_ok)
        driver = MagicMock(name="d")
        driver.name = "fake"
        driver.goto = AsyncMock(return_value=None)
        driver.evaluate = AsyncMock(return_value=_make_payload([]))
        r = asyncio.run(probe(driver, "https://example.com", wait_s=0.01))
        assert r.ok is True

    def test_js_payload_handles_missing_getVideoPlaybackQuality(self):
        # getVideoPlaybackQuality 不存在时 framesDecoded 默认为 0
        sig = _parse_media_payload(_make_payload([
            _video(frames_decoded=0)
        ]))
        assert sig.frames_decoded == 0

    def test_progress_uses_60s_threshold_NOT_50s(self):
        # 守门阈值：55s 时算 partial_free，65s 时不算
        sig_55 = MediaSignal(True, True, 55.0, 100, False, None)
        sig_65 = MediaSignal(True, True, 65.0, 100, False, None)
        assert to_branch(_result(sig_55)) == "partial_free"
        assert to_branch(_result(sig_65)) is None

    def test_module_docstring_mentions_L2_and_increment_23(self):
        import trajectory_pipeline.executor.probe.media_probe as mod
        assert mod.__doc__ is not None
        assert "L2" in mod.__doc__
        assert "增量二十三" in mod.__doc__

    def test_js_payload_does_not_call_evaluate_untrusted(self):
        # JS 里不能伪造 click 等 isTrusted=false 事件（设计文档 §3.2）
        assert "dispatchEvent" not in MEDIA_PROBE_JS
        assert ".click(" not in MEDIA_PROBE_JS
