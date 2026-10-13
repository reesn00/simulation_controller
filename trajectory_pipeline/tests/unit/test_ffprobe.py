
"""``executor.probe.ffprobe`` 单测。

2026-10-13 增量二十二配套：阶段 4 L1 媒体解析。

重点测**映射纪律**与**异常分类**与**HTML 解析边界**，不跑真请求
（pytest-socket 默认禁用网络）也不跑真 ffprobe（subprocess 全 mock）。
``to_branch`` 是这一层的全部业务逻辑——错一个映射就等于把整个 L1
工具的判定废掉。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from trajectory_pipeline.executor.archive import CredentialLeak
from trajectory_pipeline.executor.probe.ffprobe import (
    DEFAULT_TIMEOUT_S,
    FFPROBE_TIMEOUT_S,
    FFProbeOutput,
    MAX_IFRAME_HOPS,
    MAX_MEDIA_URLS,
    MEDIA_EXTENSIONS,
    PARTIAL_FREE_MAX_DURATION_S,
    MediaProbeResult,
    _looks_like_media,
    _parse_ffprobe_payload,
    _parse_html_for_media,
    dump_result,
    ffprobe_available,
    probe,
    run_ffprobe_subprocess,
    to_branch,
)

from trajectory_pipeline.executor.probe.ffprobe import (
    DEFAULT_TIMEOUT_S,
    FFPROBE_TIMEOUT_S,
    FFProbeOutput,
    MAX_IFRAME_HOPS,
    MAX_MEDIA_URLS,
    MEDIA_EXTENSIONS,
    PARTIAL_FREE_MAX_DURATION_S,
    MediaProbeResult,
    _looks_like_media,
    _parse_ffprobe_payload,
    _parse_html_for_media,
    dump_result,
    ffprobe_available,
    probe,
    run_ffprobe_subprocess,
    to_branch,
)


# ─────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────


def _probed(url: str, *, ok: bool = True, duration_s: float = 600.0,
            format_name: str = "mov,mp4,m4a,3gp,3g2,mj2",
            has_video: bool = True, error: str = "") -> FFProbeOutput:
    """构造一个最小 :class:`FFProbeOutput`，只填 ``to_branch`` 关心的字段。"""
    return FFProbeOutput(
        url=url, ok=ok, duration_s=duration_s,
        format_name=format_name, has_video=has_video, error=error,
    )


def _result(
    *, probed: tuple[FFProbeOutput, ...] = (),
    media_urls: tuple[str, ...] = (),
    iframe_urls: tuple[str, ...] = (),
) -> MediaProbeResult:
    """构造一个最小 :class:`MediaProbeResult`，只填 ``to_branch`` 关心的字段。"""
    return MediaProbeResult(
        requested_url="https://x.test/",
        final_url="https://x.test/",
        html_excerpt="",
        media_urls=media_urls,
        iframe_urls=iframe_urls,
        probed=probed,
    )


def _html(html_body: str) -> str:
    """wrap to a complete-ish HTML doc."""
    return f"<html><body>{html_body}</body></html>"


# ─────────────────────────────────────────────────────────────────────
# ``to_branch`` 映射表全分支覆盖
# ─────────────────────────────────────────────────────────────────────


class TestToBranch映射表:
    """``to_branch`` 是 L1 的全部业务逻辑——错一个映射就废整个工具。

    每个用例是一条「输入探测结果 → 期望分支」的契约。纪律（设计文档
    §6 / 增量二十二）：

    - L1 只产 ``broken``（媒体坏）与 ``partial_free``（试看），**不产**
      ``blocked`` / ``unresolved``（L0 阶段已分别产出 / 留给 reachability）。
    - 没解析出任何 media → ``None``（留给 L2 / LLM）。
    - 至少一条 media duration ≤ ``PARTIAL_FREE_MAX_DURATION_S`` → ``partial_free``。
    """

    def test_probed_空_产_none(self):
        """没解析出任何 media → L1 没结论。"""
        assert to_branch(_result(probed=())) is None

    def test_probed_空_但_media_urls_非空_仍_none(self):
        """probed 空但 media_urls 非空 —— runner 没跑（CLI 没装 ffprobe 等）。
        视为「没结论」，不猜。"""
        assert to_branch(_result(
            probed=(),
            media_urls=("https://cdn.example.com/v.mp4",),
        )) is None

    def test_全_failed_产_broken(self):
        """ffprobe 全失败 → 媒体不可解码 → broken。"""
        assert to_branch(_result(probed=(
            _probed("a.mp4", ok=False, error="timeout"),
            _probed("b.mp4", ok=False, error="exit=1"),
        ))) == "broken"

    def test_至少一条_short_产_partial_free(self):
        """至少一条 media duration <= 阈值 → partial_free。"""
        assert to_branch(_result(probed=(
            _probed("a.mp4", duration_s=PARTIAL_FREE_MAX_DURATION_S),
            _probed("b.mp4", duration_s=600.0),
        ))) == "partial_free"

    def test_全_long_且_全_ok_产_none(self):
        """全 media duration > 阈值且全成功 → 交给下一层。"""
        assert to_branch(_result(probed=(
            _probed("a.mp4", duration_s=600.0),
            _probed("b.mp4", duration_s=1800.0),
        ))) is None

    def test_混合_ok_failed_且_ok_全_long_产_none(self):
        """混合 OK + failed：OK 全 long → 留给下一层；failed 不污染。"""
        assert to_branch(_result(probed=(
            _probed("a.mp4", ok=False, error="x"),
            _probed("b.mp4", duration_s=600.0),
        ))) is None

    def test_混合_ok_failed_且_ok_含_short_产_partial_free(self):
        """混合 OK + failed：OK 里有 short → partial_free（不取 failed）。"""
        assert to_branch(_result(probed=(
            _probed("a.mp4", ok=False, error="x"),
            _probed("b.mp4", duration_s=30.0),
        ))) == "partial_free"

    def test_short_阈值_边界(self):
        """duration == PARTIAL_FREE_MAX_DURATION_S 仍算 short。"""
        assert to_branch(_result(probed=(
            _probed("a.mp4", duration_s=PARTIAL_FREE_MAX_DURATION_S),
        ))) == "partial_free"


    def test_short_阈值_边界(self):
        """duration == PARTIAL_FREE_MAX_DURATION_S 仍算 short。"""
        assert to_branch(_result(probed=(
            _probed("a.mp4", duration_s=PARTIAL_FREE_MAX_DURATION_S),
        ))) == "partial_free"


# ─────────────────────────────────────────────────────────────────────
# ``_parse_html_for_media`` 解析边界
# ─────────────────────────────────────────────────────────────────────


class TestParseHTMLForMedia:
    """5 类标签 + 相对 URL + 去重 + 截断 —— 单测要锁死的边界。"""

    def test_video_src_被识别(self):
        html = _html('<video src="https://cdn.example.com/v.mp4"></video>')
        media, iframes = _parse_html_for_media(html, "https://x.test/")
        assert media == ["https://cdn.example.com/v.mp4"]
        assert iframes == []

    def test_audio_src_被识别(self):
        html = _html('<audio src="https://cdn.example.com/a.mp3"></audio>')
        media, _ = _parse_html_for_media(html, "https://x.test/")
        assert media == ["https://cdn.example.com/a.mp3"]

    def test_source_src_被识别(self):
        html = _html('<video><source src="https://cdn.example.com/v.webm"></video>')
        media, _ = _parse_html_for_media(html, "https://x.test/")
        assert media == ["https://cdn.example.com/v.webm"]

    def test_iframe_src_归_iframe_urls(self):
        html = _html('<iframe src="https://player.example.com/embed/123"></iframe>')
        media, iframes = _parse_html_for_media(html, "https://x.test/")
        assert media == []
        assert iframes == ["https://player.example.com/embed/123"]

    def test_a_href_后缀_命中_被识别(self):
        html = _html('<a href="https://cdn.example.com/v.mp4">下载</a>')
        media, _ = _parse_html_for_media(html, "https://x.test/")
        assert "https://cdn.example.com/v.mp4" in media

    def test_a_href_非媒体后缀_被忽略(self):
        html = _html('<a href="https://example.com/about.html">关于</a>')
        media, _ = _parse_html_for_media(html, "https://x.test/")
        assert media == []

    def test_相对_url_被解析为绝对(self):
        """``https://x.test/page`` + ``../cdn/v.mp4`` → ``https://x.test/cdn/v.mp4``"""
        html = _html('<video src="/cdn/v.mp4"></video>')
        media, _ = _parse_html_for_media(html, "https://x.test/page")
        assert media == ["https://x.test/cdn/v.mp4"]

    def test_去重保序(self):
        """同一 URL 多次出现只算一条，保留首次出现顺序。"""
        html = _html(
            '<video src="https://cdn.example.com/a.mp4"></video>'
            '<video src="https://cdn.example.com/b.mp4"></video>'
            '<video src="https://cdn.example.com/a.mp4"></video>'
        )
        media, _ = _parse_html_for_media(html, "https://x.test/")
        assert media == [
            "https://cdn.example.com/a.mp4",
            "https://cdn.example.com/b.mp4",
        ]

    def test_超过_max_urls_截断(self):
        """``max_urls`` 截断——防广告 / 埋点。"""
        urls = "".join(
            f'<a href="https://cdn.example.com/v{i}.mp4">x</a>'
            for i in range(MAX_MEDIA_URLS + 5)
        )
        html = _html(urls)
        media, _ = _parse_html_for_media(html, "https://x.test/")
        assert len(media) == MAX_MEDIA_URLS

    def test_空_html_空结果(self):
        media, iframes = _parse_html_for_media("", "https://x.test/")
        assert media == []
        assert iframes == []

    def test_data_src_被识别(self):
        """``data-src`` 是 H5 懒加载常见写法——也得收。"""
        html = _html('<video data-src="https://cdn.example.com/v.mp4"></video>')
        media, _ = _parse_html_for_media(html, "https://x.test/")
        assert media == ["https://cdn.example.com/v.mp4"]

    def test_单引号_双引号_都识别(self):
        """属性引号单/双都识别。"""
        html = _html(
            '<video src=\'https://cdn.example.com/a.mp4\'></video>'
            '<video src="https://cdn.example.com/b.mp4"></video>'
        )
        media, _ = _parse_html_for_media(html, "https://x.test/")
        assert sorted(media) == sorted([
            "https://cdn.example.com/a.mp4",
            "https://cdn.example.com/b.mp4",
        ])


class TestLooksLikeMedia:
    """``_looks_like_media`` 后缀筛单测。"""

    def test_命中扩展名(self):
        assert _looks_like_media("https://x.test/v.mp4") is True
        assert _looks_like_media("https://x.test/v.MP4") is True  # case-insensitive
        assert _looks_like_media("https://x.test/a.m4a") is True

    def test_未命中扩展名(self):
        assert _looks_like_media("https://x.test/page.html") is False
        assert _looks_like_media("") is False

    def test_query_string_不影响(self):
        """签名 URL 通常带 query string，应忽略。"""
        assert _looks_like_media("https://x.test/v.mp4?token=abc&exp=123") is True


    def test_query_string_不影响(self):
        """签名 URL 通常带 query string，应忽略。"""
        assert _looks_like_media("https://x.test/v.mp4?token=abc&exp=123") is True


# ─────────────────────────────────────────────────────────────────────
# ``_parse_ffprobe_payload`` 解析边界
# ─────────────────────────────────────────────────────────────────────


class TestParseFFprobePayload:
    """ffprobe JSON payload → ``FFProbeOutput`` 边界。"""

    def test_正常_payload(self):
        payload = {
            "format": {"duration": "120.5", "format_name": "mov,mp4"},
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        }
        out = _parse_ffprobe_payload("https://x.test/v.mp4", payload)
        assert out.ok is True
        assert out.duration_s == 120.5
        assert out.format_name == "mov,mp4"
        assert out.has_video is True

    def test_duration_缺失_落_0(self):
        payload = {
            "format": {"format_name": "mp3"},
            "streams": [{"codec_type": "audio"}],
        }
        out = _parse_ffprobe_payload("https://x.test/a.mp3", payload)
        assert out.duration_s == 0.0

    def test_duration_为_0(self):
        payload = {"format": {"duration": "0", "format_name": "mp4"}, "streams": []}
        out = _parse_ffprobe_payload("https://x.test/v.mp4", payload)
        assert out.duration_s == 0.0

    def test_duration_为负_被_clamp_到_0(self):
        payload = {"format": {"duration": "-5.0"}, "streams": []}
        out = _parse_ffprobe_payload("https://x.test/v.mp4", payload)
        assert out.duration_s == 0.0

    def test_format_name_缺失_空串(self):
        payload = {"format": {"duration": "10"}, "streams": []}
        out = _parse_ffprobe_payload("https://x.test/v.mp4", payload)
        assert out.format_name == ""

    def test_无_video_stream(self):
        payload = {
            "format": {"duration": "300", "format_name": "mp3"},
            "streams": [{"codec_type": "audio"}],
        }
        out = _parse_ffprobe_payload("https://x.test/a.mp3", payload)
        assert out.has_video is False

    def test_duration_非法_字符串_落_0(self):
        """ffprobe 偶尔对损坏媒体返回非数字 duration——按 0 兜底。"""
        payload = {"format": {"duration": "N/A"}, "streams": []}
        out = _parse_ffprobe_payload("https://x.test/v.mp4", payload)
        assert out.duration_s == 0.0


    def test_duration_非法_字符串_落_0(self):
        """ffprobe 偶尔对损坏媒体返回非数字 duration——按 0 兜底。"""
        payload = {"format": {"duration": "N/A"}, "streams": []}
        out = _parse_ffprobe_payload("https://x.test/v.mp4", payload)
        assert out.duration_s == 0.0


# ─────────────────────────────────────────────────────────────────────
# ``run_ffprobe_subprocess`` subprocess mock
# ─────────────────────────────────────────────────────────────────────


class TestRunFFprobeSubprocess:
    """``run_ffprobe_subprocess`` 全 mock——subprocess 不可真跑。"""

    @pytest.mark.asyncio
    async def test_FileNotFound_返回_ok_false(self):
        """ffprobe 二进制缺失 → ok=False + 错误信息——不抛。"""
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("ffprobe not found"),
        ):
            out = await run_ffprobe_subprocess("https://x.test/v.mp4")
        assert out.ok is False
        assert "not found" in out.error

    @pytest.mark.asyncio
    async def test_TimeoutExpired_返回_ok_false(self):
        """``wait_for`` 超时 → ok=False + timeout 信息——不抛。"""
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            out = await run_ffprobe_subprocess(
                "https://x.test/v.mp4", timeout_s=0.01,
            )
        assert out.ok is False
        assert "timeout" in out.error
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_returncode_非零_返回_ok_false(self):
        """非 0 退出码 → ok=False + stderr 末 200 字——不抛。"""
        proc = MagicMock()
        proc.communicate = AsyncMock(
            return_value=(b"", b"Invalid data found when processing input"),
        )
        proc.returncode = 1
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            out = await run_ffprobe_subprocess("https://x.test/v.mp4")
        assert out.ok is False
        assert "exit=1" in out.error
        assert "Invalid data" in out.error

    @pytest.mark.asyncio
    async def test_正常_payload_返回_ok_true(self):
        """正常 exit + JSON → ok=True + 字段填齐。"""
        payload = json.dumps({
            "format": {"duration": "60.0", "format_name": "mov,mp4"},
            "streams": [{"codec_type": "video"}],
        }).encode("utf-8")
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(payload, b""))
        proc.returncode = 0
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            out = await run_ffprobe_subprocess("https://x.test/v.mp4")
        assert out.ok is True
        assert out.duration_s == 60.0
        assert out.has_video is True

    @pytest.mark.asyncio
    async def test_非_JSON_返回_ok_false(self):
        """stdout 不是 JSON → ok=False + 解析错误信息。"""
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"<html>oops</html>", b""))
        proc.returncode = 0
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            out = await run_ffprobe_subprocess("https://x.test/v.mp4")
        assert out.ok is False
        assert "json parse failed" in out.error


class TestFFprobeAvailable:
    """``ffprobe_available`` 守门纪律——缺件即报错，不猜。"""

    def test_找到_返回_True(self):
        with patch("shutil.which", return_value="/usr/bin/ffprobe"):
            assert ffprobe_available() is True

    def test_找不到_返回_False(self):
        with patch("shutil.which", return_value=None):
            assert ffprobe_available() is False


    def test_找不到_返回_False(self):
        with patch("shutil.which", return_value=None):
            assert ffprobe_available() is False


# ─────────────────────────────────────────────────────────────────────
# ``probe`` httpx mock + runner mock
# ─────────────────────────────────────────────────────────────────────


def _mock_response(
    *,
    status_code: int = 200,
    text: str = "<html></html>",
    history: list[httpx.Response] | None = None,
    raise_for_status_exc: Exception | None = None,
) -> httpx.Response:
    """构造 httpx.Response——只填 ``probe`` 用得到的字段。"""
    request = httpx.Request("GET", "https://x.test/")
    response = httpx.Response(
        status_code=status_code,
        text=text,
        request=request,
    )
    response.history = history or []  # type: ignore[attr-defined]
    if raise_for_status_exc:
        response.raise_for_status = lambda: (None if raise_for_status_exc is None else (_ for _ in ()).throw(raise_for_status_exc))  # type: ignore[assignment]
    return response


def _mock_client(response: httpx.Response | Exception) -> AsyncMock:
    """``httpx.AsyncClient.get`` 的 mock."""
    client = AsyncMock()
    if isinstance(response, Exception):
        client.get = AsyncMock(side_effect=response)
    else:
        client.get = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    return client


class TestProbeHTTP:
    """``probe`` 是 L1 的入口——错一个异常分类 / 一条 URL 处理就废整层。"""

    @pytest.mark.asyncio
    async def test_来源页_正常_200_拉回_html(self):
        client = _mock_client(_mock_response(
            status_code=200,
            text=_html('<video src="https://cdn.example.com/v.mp4"></video>'),
        ))
        result = await probe("https://x.test/", client=client)
        assert result.final_url == "https://x.test/"
        assert "v.mp4" in result.html_excerpt
        assert result.media_urls == ("https://cdn.example.com/v.mp4",)

    @pytest.mark.asyncio
    async def test_来源页_404_空结果(self):
        """来源页 4xx/5xx → ``raise_for_status`` 抛 → 空结果。"""
        client = _mock_client(_mock_response(
            status_code=404,
            raise_for_status_exc=httpx.HTTPStatusError(
                "404", request=httpx.Request("GET", "https://x.test/"),
                response=httpx.Response(404),
            ),
        ))
        result = await probe("https://x.test/", client=client)
        assert result.media_urls == ()
        assert result.iframe_urls == ()
        assert result.probed == ()
        assert result.html_excerpt == ""

    @pytest.mark.asyncio
    async def test_来源页_timeout_空结果(self):
        """来源页拉超时 → 空结果——L1 不抛。"""
        client = _mock_client(httpx.TimeoutException("read timeout"))
        result = await probe("https://x.test/", client=client)
        assert result.media_urls == ()
        assert result.probed == ()

    @pytest.mark.asyncio
    async def test_来源页_connect_error_空结果(self):
        """来源页连接失败 → 空结果——L1 不抛。"""
        client = _mock_client(httpx.ConnectError("DNS failure"))
        result = await probe("https://x.test/", client=client)
        assert result.media_urls == ()

    @pytest.mark.asyncio
    async def test_无_media_标签_probed_空(self):
        client = _mock_client(_mock_response(
            status_code=200, text=_html('<p>no media here</p>'),
        ))
        result = await probe("https://x.test/", client=client)
        assert result.media_urls == ()
        assert result.probed == ()
        # 没解析出任何 media → to_branch = None
        assert to_branch(result) is None

    @pytest.mark.asyncio
    async def test_iframe_被解析_进_iframe_urls(self):
        client = _mock_client(_mock_response(
            status_code=200,
            text=_html('<iframe src="https://player.example.com/embed/1"></iframe>'),
        ))
        # 把 client 设为可同时响应来源页 + iframe URL（同一 mock）
        result = await probe("https://x.test/", client=client)
        assert result.iframe_urls == ("https://player.example.com/embed/1",)
        # iframe 1 跳拉了一次（probe 函数本身调 client.get 拉 iframe）
        assert client.get.await_count == 2  # 来源页 + 1 iframe

    @pytest.mark.asyncio
    async def test_iframe_1_跳拉_失败_不抛(self):
        """iframe URL 拉失败 → 探针不抛——失败留空给下游。"""
        client = AsyncMock()
        # 来源页 200 + iframe URL 拉超时
        ok_resp = _mock_response(
            status_code=200,
            text=_html('<iframe src="https://player.example.com/embed/1"></iframe>'),
        )
        client.get = AsyncMock(side_effect=[
            ok_resp,
            httpx.TimeoutException("iframe timeout"),
        ])
        client.aclose = AsyncMock()
        # 探针不应抛
        result = await probe("https://x.test/", client=client)
        assert result.iframe_urls == ("https://player.example.com/embed/1",)

    @pytest.mark.asyncio
    async def test_runner_调用_每条_media(self):
        """runner 被调用 N 次（N = media_urls 数），返回顺序与 media_urls 一致。"""
        client = _mock_client(_mock_response(
            status_code=200,
            text=_html(
                '<video src="https://cdn.example.com/a.mp4"></video>'
                '<video src="https://cdn.example.com/b.mp4"></video>'
            ),
        ))
        calls: list[str] = []

        async def fake_runner(url: str) -> FFProbeOutput:
            calls.append(url)
            return FFProbeOutput(
                url=url, ok=True, duration_s=600.0, has_video=True,
            )

        result = await probe("https://x.test/", client=client, runner=fake_runner)
        assert calls == [
            "https://cdn.example.com/a.mp4",
            "https://cdn.example.com/b.mp4",
        ]
        assert len(result.probed) == 2
        assert all(p.ok for p in result.probed)
        assert to_branch(result) is None  # 全 long → None

    @pytest.mark.asyncio
    async def test_runner_部分_失败_不影响_其他(self):
        """单条 runner 失败：to_branch 看 OK 集合；一条 short 命中 partial_free。"""
        client = _mock_client(_mock_response(
            status_code=200,
            text=_html(
                '<video src="https://cdn.example.com/a.mp4"></video>'
                '<video src="https://cdn.example.com/b.mp4"></video>'
            ),
        ))

        async def mixed_runner(url: str) -> FFProbeOutput:
            if url.endswith("a.mp4"):
                return FFProbeOutput(url=url, ok=False, error="timeout")
            return FFProbeOutput(
                url=url, ok=True, duration_s=30.0, has_video=True,
            )

        result = await probe("https://x.test/", client=client, runner=mixed_runner)
        assert len(result.probed) == 2
        assert to_branch(result) == "partial_free"

    @pytest.mark.asyncio
    async def test_runner_None_则_probed_空(self):
        """runner=None（测试场景）：probed 留空。"""
        client = _mock_client(_mock_response(
            status_code=200,
            text=_html('<video src="https://cdn.example.com/v.mp4"></video>'),
        ))
        result = await probe("https://x.test/", client=client, runner=None)
        assert result.media_urls == ("https://cdn.example.com/v.mp4",)
        assert result.probed == ()

    @pytest.mark.asyncio
    async def test_自带_client_不被_close(self):
        """外部传入 client 时不调 aclose。"""
        provided = _mock_client(_mock_response(
            status_code=200, text=_html('<p>x</p>'),
        ))
        await probe("https://x.test/", client=provided, runner=None)
        provided.aclose.assert_not_called()

    @pytest.mark.asyncio
    async def test_自带_client_异常_也不_close(self):
        provided = _mock_client(httpx.ConnectError("x"))
        await probe("https://x.test/", client=provided, runner=None)
        provided.aclose.assert_not_called()

    @pytest.mark.asyncio
    async def test_html_截断_到_max_body_chars(self):
        long_html = _html('<video src="https://x.test/v.mp4"></video>') + "x" * (20000)
        client = _mock_client(_mock_response(status_code=200, text=long_html))
        result = await probe("https://x.test/", client=client, runner=None)
        assert len(result.html_excerpt) <= 16384

    @pytest.mark.asyncio
    async def test_max_urls_截断(self):
        """``max_urls`` 截断——max_urls 缩到 2 时只看前 2 条。"""
        urls = "".join(
            f'<video src="https://cdn.example.com/v{i}.mp4"></video>'
            for i in range(5)
        )
        client = _mock_client(_mock_response(status_code=200, text=_html(urls)))
        result = await probe(
            "https://x.test/", client=client, runner=None, max_urls=2,
        )
        assert len(result.media_urls) == 2


# ─────────────────────────────────────────────────────────────────────
# ``dump_result`` 落盘 + 凭据闸门
# ─────────────────────────────────────────────────────────────────────


class TestDumpResult:
    """``dump_result`` 原子写 + 凭据闸门——与 L0 同款纪律。"""

    def test_正常落盘(self, tmp_path):
        result = _result(probed=(
            _probed("https://x.test/v.mp4", duration_s=600.0),
        ), media_urls=("https://x.test/v.mp4",))
        path = tmp_path / "probe.json"
        written = dump_result(result, path)
        assert written == path
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["media_urls"] == ["https://x.test/v.mp4"]
        assert payload["probed"][0]["ok"] is True

    def test_原子写_tmp_不留盘(self, tmp_path):
        path = tmp_path / "probe.json"
        dump_result(_result(), path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        assert not tmp.exists()

    def test_凭据命中_拒绝写(self, tmp_path):
        """``Authorization: Bearer abc`` 写入 media_urls 时必须 fail-closed。"""
        result = MediaProbeResult(
            requested_url="https://x.test/",
            final_url="https://x.test/",
            html_excerpt="",
            media_urls=("https://x.test/v.mp4?token=Authorization: Bearer abc",),
            iframe_urls=(),
            probed=(_probed("https://x.test/v.mp4"),),
        )
        with pytest.raises(CredentialLeak, match="凭据"):
            dump_result(result, tmp_path / "probe.json")
        # fail-closed：不得写出任何文件
        assert not (tmp_path / "probe.json").exists()

    def test_空_result_也能落盘(self, tmp_path):
        """空 probed + 空 media_urls：合法探测结果（来源页无 media）。"""
        path = tmp_path / "probe.json"
        dump_result(_result(), path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["probed"] == []
        assert payload["media_urls"] == []


# ─────────────────────────────────────────────────────────────────────
# 顶层常量
# ─────────────────────────────────────────────────────────────────────


class TestModuleConstants:
    """常量单测——避免有人悄悄改了阈值把整层规则改掉。"""

    def test_PARTIAL_FREE_MAX_DURATION_S_为_60(self):
        """试看阈值——爱奇艺 / 优酷试看常见 30–60s。"""
        assert PARTIAL_FREE_MAX_DURATION_S == 60.0

    def test_MAX_IFRAME_HOPS_为_1(self):
        """iframe 1 跳解析——增量二十二设计点。"""
        assert MAX_IFRAME_HOPS == 1

    def test_MAX_MEDIA_URLS_合理(self):
        """``MAX_MEDIA_URLS`` 不应过大（防广告撑爆）。"""
        assert 5 <= MAX_MEDIA_URLS <= 100

    def test_MEDIA_EXTENSIONS_不含_m3u8(self):
        """``.m3u8`` / ``.mpd`` 是 HLS/DASH manifest，不是媒体本体——留给 L2。"""
        assert ".m3u8" not in MEDIA_EXTENSIONS
        assert ".mpd" not in MEDIA_EXTENSIONS

    def test_DEFAULT_TIMEOUT_S_合理(self):
        assert 5.0 <= DEFAULT_TIMEOUT_S <= 60.0

    def test_FFPROBE_TIMEOUT_S_合理(self):
        assert 5.0 <= FFPROBE_TIMEOUT_S <= 60.0

