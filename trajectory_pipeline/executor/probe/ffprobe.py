"""L1 媒体解析（HTTP 拉页 + ffprobe 直连，无浏览器）。

2026-10-13 增量二十二配套：阶段 4 L1 媒体解析。

设计纪律（设计文档 §4.1 + §6 / 增量二十二）：

1. **不封装浏览器**——L1 只跑 HTTP GET（来源页 + iframe 1 跳）与
   ffprobe 直连媒体 URL；不弹 obscura。L2 ``media_probe``（增量十七后续路径）
   才上浏览器。

2. **HTML 解析只取 URL 字符串**——不解析 DOM、不跑 JS、不判
   业务语义；规则版 ``rule_perceptor`` 已经在做这件事。
   L1 的「业务价值」是**确定 media 是否真能播**与**确定 duration**——这两件
   纯 HTML 解析拿不到。

3. **iframe 走 1 跳解析**——``MAX_IFRAME_HOPS = 1``，超过 1 层嵌套的 iframe
   URL 不再递归拉（与 L0 同款 fail-fast：拿不到就空，**不猜**）。
   服务端 HTTP GET，不受 CORS 限制。

4. **凭据闸门复用 :func:`scan_credentials`**——与 L0 同款 fail-closed。
   iframe 1 跳拉回的 HTML 可能含 cookie / token，不能绕过。

5. **ffprobe 未装 → fail-closed**——与 ``OBSCURA_EXE`` 同款纪律：
   CLI 调时显式校验，缺即报错不猜。模块层只暴露 ``FFProbeRunner``
   Protocol，让测试注入 mock。

`to_branch` 映射纪律：

- **ffprobe 失败 / 全媒体失败 → `broken`**（媒体不可解码——partial_free / broken 共边）。
- **至少一条媒体 `duration <= PARTIAL_FREE_MAX_DURATION_S`** → `partial_free`（试看/预告）。
- **全媒体 `duration > PARTIAL_FREE_MAX_DURATION_S` 且全成功** → `None`（交给下一层；当前 L2/L3 未实现，「真能播」不归 L1）。
- **没有任何 media 标签可解析** → `None`（L1 没结论，留给 L2 / LLM）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from trajectory_pipeline.executor.archive import (
    CredentialLeak,
    credential_samples,
    scan_credentials,
)

#: 试看 / 预告时长上限（秒）。超过此值的媒体视为「整段可看」，
#: 不在 L1 这一层判 partial_free。来源：爱奇艺 / 优酷试看常见 30–60s，
#: 取上限留余量；与设计文档 §4.1「partial_free」语义对齐。
PARTIAL_FREE_MAX_DURATION_S: float = 60.0

#: iframe 1 跳解析上限。> 1 层嵌套的 iframe URL 不递归拉，
#: 防止 iframe 套娃递归把页面拉炸。L1 与 L0 同款：拿不到就空，不猜。
MAX_IFRAME_HOPS: int = 1

#: 单来源页最多解析的 media URL 数。超过截断到 ``MAX_MEDIA_URLS``，
#: 防广告 / 埋点把数组撑爆；与 L0 ``MAX_BODY_CHARS`` 同款 fail-fast。
MAX_MEDIA_URLS: int = 20

#: 来源页 / iframe 页 HTTP 拉取超时（秒）。
DEFAULT_TIMEOUT_S: float = 10.0

#: ffprobe 子进程超时（秒）。单媒体探测不应超过此值；
#: 超过即视为媒体不可解码（落 ``broken``）。
FFPROBE_TIMEOUT_S: float = 15.0

#: 默认 ffprobe 可执行名（PATH 寻）。
FFPROBE_BIN: str = "ffprobe"

#: 媒体扩展名集合——后缀筛 `<a>` / `<source src="…">` 用。
#: 不含 `.m3u8` / `.mpd`（这些是 HLS / DASH manifest 不是媒体本体）；
#: 它们在 L2 ``media_probe`` 才走业务探测。
MEDIA_EXTENSIONS: frozenset[str] = frozenset({
    ".mp4", ".m4v", ".webm", ".mkv", ".flv", ".avi", ".mov", ".ts",
    ".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac",
})

#: `<video>` / `<audio>` / `<source>` / `<iframe>` 的 src 属性提取。
#: 取 `src=` / `data-src=` 两类（H5 懒加载常见 data-src）。允许单/双引号。
_MEDIA_SRC_RE = re.compile(
    r"<(?:video|audio|source|iframe)[^>]*?(?:src|data-src)\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)

#: `<a href="…">` 后缀筛——只挑扩展名命中 ``MEDIA_EXTENSIONS`` 的链接。
#: 不抓全部 <a> 是因为页面里 "打开 APP" / "联系客服" 链接带 query string 太长。
_A_HREF_RE = re.compile(
    r"<a[^>]*?href\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
_A_HREF_RE = re.compile(
    r"<a[^>]*?href\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FFProbeOutput:
    """单条媒体 ffprobe 输出。

    字段映射 `ffprobe -v quiet -print_format json -show_format -show_streams` 的子集：
    - `url`           — 探测对象（来源页解析出来的 URL）
    - `ok`            — ffprobe 退出码 0 且 JSON 解析成功
    - `duration_s`    — `format.duration`（秒；缺失落 0.0）
    - `format_name`   — `format.format_name`（缺失落 ""）
    - `has_video`     — 至少一条 stream `codec_type == "video"`
    - `error`         — 异常 / 超时 / 非 0 退出码时的描述（无则 ""）
    """

    url: str
    ok: bool
    duration_s: float = 0.0
    format_name: str = ""
    has_video: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class MediaProbeResult:
    """L1 媒体探测结果（页级汇总）。

    字段分层：

    - **元信息** — `requested_url` / `final_url` / `elapsed_ms`
    - **来源页** — `html_excerpt`（截前 16KB 留档）
    - **解析出的媒体** — `media_urls`（去重有序，截 `MAX_MEDIA_URLS`）
    - **iframe 1 跳 URL** — `iframe_urls`（供下游「是否去 iframe 拉」参考）
    - **每条媒体探测结果** — `probed`（与 `media_urls` 同序；缺失则空 `FFProbeOutput`）
    """

    requested_url: str
    final_url: str
    html_excerpt: str
    media_urls: tuple[str, ...]
    iframe_urls: tuple[str, ...]
    probed: tuple[FFProbeOutput, ...] = field(default_factory=tuple)
    elapsed_ms: int = 0


class FFProbeRunner(Protocol):
    """ffprobe 执行器协议——测试时塞 mock。

    入参 `url` 是要探测的媒体 URL，返回 :class:`FFProbeOutput`。
    """

    async def __call__(self, url: str) -> FFProbeOutput: ...


async def run_ffprobe_subprocess(
    url: str,
    *,
    bin_name: str = FFPROBE_BIN,
    timeout_s: float = FFPROBE_TIMEOUT_S,
) -> FFProbeOutput:
    """默认 `FFProbeRunner` 实现：异步跑 ``ffprobe -v quiet -show_format``。

    实现要点：

    - **异步包同步 subprocess** —— ``asyncio.create_subprocess_exec`` 避免阻塞事件循环；
      httpx 客户端与 ffprobe 在同一 ``probe`` 里协作，需要并发安全。
    - **超时硬切** —— ``timeout_s`` 到即 ``TimeoutExpired``，返回 ``ok=False``。
    - **非 0 退出码** —— ``ok=False``，``error`` 字段记录 stderr 末 200 字。
    - **JSON 解析失败** —— ``ok=False``，``error`` 记录首段输出。

    返回 :class:`FFProbeOutput`，**不抛异常**（ffprobe 探测失败属业务信号）。
    """

    try:
        proc = await asyncio.create_subprocess_exec(
            bin_name, "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", url,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return FFProbeOutput(
            url=url, ok=False, error=f"ffprobe binary not found: {bin_name}",
        )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        finally:
            return FFProbeOutput(
                url=url, ok=False,
                error=f"ffprobe timeout after {timeout_s}s",
            )

    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace")[-200:]
        return FFProbeOutput(
            url=url, ok=False,
            error=f"ffprobe exit={proc.returncode}: {tail}",
        )

    try:
        payload = json.loads(stdout or b"{}")
    except json.JSONDecodeError as exc:
        head = (stdout or b"").decode("utf-8", errors="replace")[:120]
        return FFProbeOutput(
            url=url, ok=False,
            error=f"ffprobe json parse failed: {exc}; head={head}",
        )

    return _parse_ffprobe_payload(url, payload)


def _parse_ffprobe_payload(url: str, payload: dict) -> FFProbeOutput:
    """把 ffprobe JSON payload 解析成 :class:`FFProbeOutput`。

    失败时 ``ok=False`` —— payload 缺关键字段、duration 非数、streams 缺失。
    """
    fmt = payload.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration < 0:
        duration = 0.0
    fmt_name = str(fmt.get("format_name") or "")

    streams = payload.get("streams") or []
    has_video = any(
        isinstance(s, dict) and s.get("codec_type") == "video"
        for s in streams
    )

    return FFProbeOutput(
        url=url,
        ok=True,
        duration_s=duration,
        format_name=fmt_name,
        has_video=has_video,
    )
    return FFProbeOutput(
        url=url,
        ok=True,
        duration_s=duration,
        format_name=fmt_name,
        has_video=has_video,
    )


def _parse_html_for_media(
    html: str,
    base_url: str,
    *,
    max_urls: int = MAX_MEDIA_URLS,
) -> tuple[list[str], list[str]]:
    """从 HTML 中解析出 (media_urls, iframe_urls)。

    实现要点：

    - **5 类来源** — `<video>` / `<audio>` / `<source>` / `<iframe>` / 后缀筛 `<a>`
    - **去重保序** — ``dict.fromkeys`` 一行；同一 URL 多次出现只算一条
    - **相对 URL 解析** — ``httpx.URL(base).join(相对)`` 处理 ``../`` / 绝对路径 / 同源相对
    - **media URL 截断** — 超过 ``max_urls`` 截断（防广告 / 埋点）
    - **iframe 与 media 分开** — ``<iframe>`` 进 ``iframe_urls``（由 1 跳解析层处理），
      其余四类（video/audio/source/后缀筛 a）合并进 ``media_urls``
    """

    def _abs(raw: str) -> str:
        raw = raw.strip()
        if not raw:
            return ""
        try:
            joined = str(httpx.URL(base_url).join(raw))
        except (httpx.InvalidURL, ValueError):
            return raw
        return joined

    media: list[str] = []
    iframes: list[str] = []

    for m in _MEDIA_SRC_RE.finditer(html):
        raw = m.group(1)
        abs_url = _abs(raw)
        if not abs_url:
            continue
        # tag = video|audio|source|iframe
        tag = m.group(0).split("<", 1)[1].split(">", 1)[0].split()[0].lower()
        if tag == "iframe":
            iframes.append(abs_url)
        else:
            media.append(abs_url)

    for m in _A_HREF_RE.finditer(html):
        raw = m.group(1)
        # 后缀筛
        if not _looks_like_media(raw):
            continue
        abs_url = _abs(raw)
        if abs_url:
            media.append(abs_url)

    media = list(dict.fromkeys(media))[:max_urls]
    iframes = list(dict.fromkeys(iframes))[:max_urls]
    return media, iframes


def _looks_like_media(url: str) -> bool:
    """后缀筛 `<a>` —— 路径末尾扩展名命中 ``MEDIA_EXTENSIONS``。

    取 query string 前的 path 部分再 lower-case。
    """
    try:
        path = httpx.URL(url).path
    except (httpx.InvalidURL, ValueError):
        # 非法 URL 时按字符串末端判断
        tail = url.split("?", 1)[0].rsplit(".", 1)[-1].lower()
        return f".{tail}" in MEDIA_EXTENSIONS
    ext = ("." + path.rsplit(".", 1)[-1]).lower() if "." in path else ""
    return ext in MEDIA_EXTENSIONS


def to_branch(result: MediaProbeResult) -> str | None:
    """探测结果 → 分支映射（仅 `broken` / `partial_free` / `None`）。

    关键边界（设计文档 §6 / 增量二十二）：

    - **L1 与 L0 不重叠** —— L0 在 HTTP 层就报 ``broken``/``blocked``，到 L1 时
      来源页已经 200。所以 L1 只产 ``broken``（媒体坏）与 ``partial_free``（试看）。
    - **没解析出任何 media** —— ``None``（L1 没结论，留给 L2 / LLM）。
    - **至少一条 media `duration <= PARTIAL_FREE_MAX_DURATION_S`** —— ``partial_free``。
      即"试看 / 预告"。
    - **ffprobe 全失败** —— ``broken``（媒体不可解码）。
    - **全 media `duration > PARTIAL_FREE_MAX_DURATION_S` 且全成功** —— ``None``
      （"真能播"不归 L1，留给 L2/LLM）
    """
    if not result.probed:
        return None
    ok_probed = [p for p in result.probed if p.ok]
    if not ok_probed:
        # 全失败 → 媒体不可解码
        return "broken"
    if any(p.duration_s <= PARTIAL_FREE_MAX_DURATION_S for p in ok_probed):
        return "partial_free"
    return None

    if any(p.duration_s <= PARTIAL_FREE_MAX_DURATION_S for p in ok_probed):
        return "partial_free"
    return None


async def probe(
    url: str,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_body_chars: int = 16384,
    max_urls: int = MAX_MEDIA_URLS,
    client: httpx.AsyncClient | None = None,
    runner: FFProbeRunner | None = None,
) -> MediaProbeResult:
    """HTTP 层拉来源页 + iframe 1 跳 + ffprobe 直连媒体。

    流程：

    1. **拉来源页** —— ``client.get(url)``（自带 client 不 aclose）。
    2. **解析 media_urls / iframe_urls** —— :func:`_parse_html_for_media`。
    3. **iframe 1 跳** —— 对每个 iframe URL 异步 GET，**不解析其内部**
       （超过 ``MAX_IFRAME_HOPS = 1`` 即停）。
    4. **ffprobe 直连每条 media_url** —— 异步并发跑（``asyncio.gather``）。
    5. **汇总** —— :class:`MediaProbeResult`，``probed`` 与 ``media_urls`` 同序。

    异常分类：

    - 来源页拉失败 —— ``media_urls = ()``，``iframe_urls = ()``，``probed = ()``，
      ``html_excerpt = ""``，**返回结果而不是抛**（让 L1 caller 自己决定下分支）。
    - ffprobe 失败 —— 单条 ``FFProbeOutput.ok = False``，**不抛**（业务信号）。
    """
    started = time.perf_counter()
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout_s,
            headers={"User-Agent": "trajectory-mediaprobe/1.0"},
        )
    try:
        try:
            response = await client.get(url)
            response.raise_for_status()
            final_url = str(response.url)
            html = (response.text or "")
        except (httpx.HTTPError, httpx.RequestError, httpx.TimeoutException):
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            return MediaProbeResult(
                requested_url=url,
                final_url=url,
                html_excerpt="",
                media_urls=(),
                iframe_urls=(),
                probed=(),
                elapsed_ms=elapsed_ms,
            )

        html_excerpt = html[:max_body_chars]
        media_urls, iframe_urls = _parse_html_for_media(
            html_excerpt, final_url, max_urls=max_urls,
        )

        # iframe 1 跳：拉 iframe URL，但不再解析其内部 media
        if iframe_urls:
            await _fetch_iframes(client, iframe_urls, timeout_s=timeout_s)

        # ffprobe 每条 media
        probed: list[FFProbeOutput] = []
        if runner is not None and media_urls:
            probed.extend(await asyncio.gather(
                *(runner(m) for m in media_urls),
            ))

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return MediaProbeResult(
            requested_url=url,
            final_url=final_url,
            html_excerpt=html_excerpt,
            media_urls=tuple(media_urls),
            iframe_urls=tuple(iframe_urls),
            probed=tuple(probed),
            elapsed_ms=elapsed_ms,
        )
    finally:
        if own_client:
            await client.aclose()


async def _fetch_iframes(
    client: httpx.AsyncClient,
    iframe_urls: list[str],
    *,
    timeout_s: float,
) -> None:
    """iframe 1 跳解析 —— 服务端 GET，**不解析其内部**。

    存在意义：让 L1 caller / 后续层能判断「iframe 后是否还有真页面」。
    失败 / 超时不抛 —— 留给下游判断。
    """
    sem = asyncio.Semaphore(4)

    async def _one(u: str) -> None:
        async with sem:
            try:
                await client.get(u, timeout=timeout_s)
            except (httpx.HTTPError, httpx.RequestError, httpx.TimeoutException):
                pass

    await asyncio.gather(*(_one(u) for u in iframe_urls), return_exceptions=True)


def dump_result(result: MediaProbeResult, path: Path) -> Path:
    """落盘探测结果：JSON + 原子写 + 凭据扫描。

    复用 :func:`scan_credentials` 走与 P1 / L0 **同一道**凭据闸门
    （CLAUDE.md R11）——L1 ``html_excerpt`` / media URL 可能含 cookie / token。

    ``scan_credentials`` 只返回命中的 pattern 字符串，排错不便；
    本函数额外把匹配到的样本文本（前 60 字）打到
    :class:`CredentialLeak` 异常消息里——HTML 页面里偶尔会嵌
    ``Authorization:`` / ``Bearer xxx`` / 资源链接带 ``user:pass@``
    这类误报，保留 fail-closed，但要把噪音看清楚。
    """
    payload = json.dumps(asdict(result), ensure_ascii=False, indent=2)
    hits = scan_credentials(payload)
    if hits:
        samples = credential_samples(payload)
        raise CredentialLeak(
            f"L1 探测结果命中 {len(hits)} 项凭据特征，已拒绝写盘: "
            f"{hits[:3]}; samples={samples}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return path


def ffprobe_available(*, bin_name: str = FFPROBE_BIN) -> bool:
    """检查本机是否装了 ffprobe。CLI 入口在调用 :func:`run_ffprobe_subprocess` 前
    应先校验——与 ``OBSCURA_EXE`` 同款纪律：缺件即报错，不猜。
    """
    return shutil.which(bin_name) is not None
