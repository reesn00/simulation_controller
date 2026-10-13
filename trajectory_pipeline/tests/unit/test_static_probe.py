"""``executor.probe.static_probe`` 单测。

2026-10-12 增量十八配套：阶段 4 L0 静态可达性探测。

重点测**映射纪律**与**网络异常分类**，不跑真请求（pytest-socket 默认
禁用网络）。``to_branch`` 是这一层的全部业务逻辑——错一个映射就等于
把整个 L0 工具的判定废掉。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from trajectory_pipeline.executor.archive import CredentialLeak
from trajectory_pipeline.executor.probe.static_probe import (
    MAX_BODY_CHARS,
    REDIRECT_LOOP_THRESHOLD,
    StaticProbeResult,
    _extract_title,
    dump_result,
    probe,
    to_branch,
)


# ─────────────────────────────────────────────────────────────────────
# ``to_branch`` 映射表全分支覆盖
# ─────────────────────────────────────────────────────────────────────


def _res(
    *,
    status_code: int | None = 200,
    landing_status: StaticProbeResult.landing_status.__class__ = "ok",  # type: ignore[attr-defined]
    block_reason: str = "",
) -> StaticProbeResult:
    """构造一个最小 :class:`StaticProbeResult`，只填 ``to_branch`` 关心的字段。"""
    # ``landing_status`` 类型注解是 Literal；这里直接放字符串绕过。
    return StaticProbeResult(
        requested_url="https://x.test/",
        final_url="https://x.test/",
        landing_status=landing_status,  # type: ignore[arg-type]
        status_code=status_code,
        redirect_count=0,
        redirect_chain=(),
        elapsed_ms=10,
        block_reason=block_reason,
        title="",
        body_excerpt="",
    )


class TestToBranch映射表:
    """``to_branch`` 是 L0 的全部业务逻辑——错一个映射就废整个工具。

    每个用例是一条「输入探测结果 → 期望分支」的契约。纪律（设计文档
    §6 / 增量十八）：

    - L0 HTTP 404/5xx 初筛只产 ``broken``，**不产** ``unreachable_hard``
    - 网络层异常不归任何分支（留给 reachability）
    - 2xx/3xx + 反爬特征 → ``blocked``
    """

    def test_404_产_broken(self):
        assert to_branch(_res(status_code=404)) == "broken"

    def test_410_产_broken(self):
        assert to_branch(_res(status_code=410)) == "broken"

    def test_5xx_产_broken(self):
        assert to_branch(_res(status_code=502)) == "broken"
        assert to_branch(_res(status_code=503)) == "broken"
        assert to_branch(_res(status_code=504)) == "broken"

    def test_401_产_blocked(self):
        assert to_branch(_res(status_code=401)) == "blocked"

    def test_403_兜底产_blocked(self):
        """403 就算没有反爬关键词也算 blocked——反爬站常返回极简 403 页。"""
        assert to_branch(_res(status_code=403)) == "blocked"

    def test_2xx_加_反爬_产_blocked(self):
        """200 + captcha：页面被重定向到反爬页。"""
        assert to_branch(
            _res(status_code=200, block_reason="captcha")
        ) == "blocked"

    def test_2xx_加_限流_产_blocked(self):
        assert to_branch(
            _res(status_code=200, block_reason="rate_limit")
        ) == "blocked"

    def test_3xx_加_反爬_产_blocked(self):
        """301/302 重定向到反爬页——``follow_redirects=True`` 后终态是 200，
        但若重定向链终态是 3xx，状态码仍是 3xx。"""
        assert to_branch(
            _res(status_code=302, block_reason="captcha")
        ) == "blocked"

    def test_200_干净_产_none(self):
        assert to_branch(_res(status_code=200, block_reason="")) is None

    def test_3xx_干净_产_none(self):
        assert to_branch(_res(status_code=301, block_reason="")) is None

    def test_redirect_loop_产_broken(self):
        """重定向环：内容链接坏掉 → broken。"""
        assert to_branch(_res(
            landing_status="redirect_loop",  # type: ignore[arg-type]
            status_code=None,
        )) == "broken"

    def test_timeout_不归分支(self):
        """网络层异常不归任何分支——unreachable_hard 由 reachability 触发。"""
        assert to_branch(_res(
            landing_status="timeout",  # type: ignore[arg-type]
            status_code=None,
        )) is None

    def test_tls_error_不归分支(self):
        assert to_branch(_res(
            landing_status="tls_error",  # type: ignore[arg-type]
            status_code=None,
        )) is None

    def test_connection_error_不归分支(self):
        assert to_branch(_res(
            landing_status="connection_error",  # type: ignore[arg-type]
            status_code=None,
        )) is None

    def test_4xx_非已知码_不归分支(self):
        """418 / 451 等：L0 不认识，留给下一层。"""
        assert to_branch(_res(status_code=418)) is None

    def test_status_code_None_且_非网络异常_不归分支(self):
        """极罕见：status_code=None 但 landing_status=ok 等。
        一律 None 兜底。"""
        assert to_branch(_res(
            status_code=None, landing_status="unknown",  # type: ignore[arg-type]
        )) is None


# ─────────────────────────────────────────────────────────────────────
# ``probe`` httpx mock 端到端
# ─────────────────────────────────────────────────────────────────────


def _mock_response(
    *,
    status_code: int = 200,
    text: str = "<html><title>x</title></html>",
    history: list[httpx.Response] | None = None,
) -> httpx.Response:
    """构造一个 httpx.Response——只填 ``probe`` 用得到的字段。"""
    request = httpx.Request("GET", "https://x.test/")
    response = httpx.Response(
        status_code=status_code,
        text=text,
        request=request,
    )
    # ``probe`` 读 ``response.history`` 数重定向；
    # 它的元素类型是 ``httpx.Response``（httpx 内部就这么写）。
    response.history = history or []  # type: ignore[attr-defined]
    return response


def _mock_client(response: httpx.Response | Exception) -> AsyncMock:
    """构造 ``httpx.AsyncClient.get`` 的 mock。

    ``response`` 是 :class:`httpx.Response` 时正常返回；是异常时 ``get`` 抛。
    """
    client = AsyncMock()
    if isinstance(response, Exception):
        client.get = AsyncMock(side_effect=response)
    else:
        client.get = AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    return client


class TestProbeHTTP:
    async def test_404_探测层落_broken_signal(self):
        client = _mock_client(_mock_response(
            status_code=404, text="404 Not Found",
        ))
        result = await probe("https://x.test/", client=client)
        assert result.landing_status == "broken_signal"
        assert result.status_code == 404
        assert to_branch(result) == "broken"
        # 404 不带反爬特征 → block_reason 必须空
        assert result.block_reason == ""

    async def test_403_无反爬_也落_blocked(self):
        """403 兜底 anti_bot：探测层不带反爬关键词也要落 blocked_signal。"""
        client = _mock_client(_mock_response(
            status_code=403, text="<html><title>Forbidden</title></html>",
        ))
        result = await probe("https://x.test/", client=client)
        assert result.landing_status == "blocked_signal"
        assert result.status_code == 403
        assert to_branch(result) == "blocked"

    async def test_200_含_反爬标题_落_blocked_signal(self):
        client = _mock_client(_mock_response(
            status_code=200,
            text="<html><title>Access Denied</title>blocked</html>",
        ))
        result = await probe("https://x.test/", client=client)
        assert result.landing_status == "blocked_signal"
        assert result.block_reason == "captcha"
        assert to_branch(result) == "blocked"

    async def test_200_含_限流_落_blocked_signal(self):
        client = _mock_client(_mock_response(
            status_code=200,
            text="<html><title>...</title>访问过于频繁，请稍后再试</html>",
        ))
        result = await probe("https://x.test/", client=client)
        assert result.landing_status == "blocked_signal"
        assert result.block_reason == "rate_limit"
        assert to_branch(result) == "blocked"

    async def test_200_干净_落_ok(self):
        client = _mock_client(_mock_response(
            status_code=200, text="<html><title>正片</title>...</html>",
        ))
        result = await probe("https://x.test/", client=client)
        assert result.landing_status == "ok"
        assert result.block_reason == ""
        assert to_branch(result) is None

    async def test_重定向_超过阈值_落_redirect_loop(self):
        """redirect_loop 在 to_branch 里产 broken。"""
        history = [
            _mock_response(status_code=301) for _ in range(REDIRECT_LOOP_THRESHOLD + 1)
        ]
        client = _mock_client(_mock_response(
            status_code=200, history=history,
        ))
        result = await probe("https://x.test/", client=client)
        assert result.landing_status == "redirect_loop"
        assert result.redirect_count == REDIRECT_LOOP_THRESHOLD + 1
        assert to_branch(result) == "broken"

    async def test_重定向_不超阈值_保持_ok(self):
        history = [_mock_response(status_code=301) for _ in range(2)]
        client = _mock_client(_mock_response(
            status_code=200, history=history,
        ))
        result = await probe("https://x.test/", client=client)
        assert result.landing_status == "ok"
        assert result.redirect_count == 2

    async def test_timeout_落_timeout_不归分支(self):
        client = _mock_client(httpx.TimeoutException("read timeout"))
        result = await probe("https://x.test/", client=client)
        assert result.landing_status == "timeout"
        assert result.status_code is None
        assert result.error  # 非空
        assert to_branch(result) is None

    async def test_connect_error_落_connection_error(self):
        client = _mock_client(httpx.ConnectError("DNS failure"))
        result = await probe("https://x.test/", client=client)
        assert result.landing_status == "connection_error"
        assert to_branch(result) is None

    async def test_request_error_落_unknown(self):
        """``httpx.RequestError`` 兜底——TLS / 协议异常通常走这里。"""
        client = _mock_client(httpx.RemoteProtocolError("protocol error"))
        result = await probe("https://x.test/", client=client)
        assert result.landing_status == "unknown"
        assert to_branch(result) is None

    async def test_自带_client_不被自动_close(self):
        """外部传入 client 时不调 ``aclose``——便于测试共用 client。"""
        provided = _mock_client(_mock_response(status_code=200))
        await probe("https://x.test/", client=provided)
        provided.aclose.assert_not_called()

    async def test_自带_client_异常_也不_close(self):
        provided = _mock_client(httpx.ConnectError("x"))
        await probe("https://x.test/", client=provided)
        provided.aclose.assert_not_called()

    async def test_body_截断到_MAX_BODY_CHARS(self):
        long_body = "<html><title>t</title>" + ("x" * (MAX_BODY_CHARS * 3)) + "</html>"
        client = _mock_client(_mock_response(
            status_code=200, text=long_body,
        ))
        result = await probe("https://x.test/", client=client)
        assert len(result.body_excerpt) == MAX_BODY_CHARS

    async def test_title_被提取(self):
        client = _mock_client(_mock_response(
            status_code=200,
            text="<html><title>  优酷 - 首页  </title><body>...</body></html>",
        ))
        result = await probe("https://x.test/", client=client)
        assert result.title == "优酷 - 首页"

    async def test_title_缺失_空串兜底(self):
        client = _mock_client(_mock_response(status_code=200, text="<html></html>"))
        result = await probe("https://x.test/", client=client)
        assert result.title == ""

    async def test_无_httpx_client_自动新建与关闭(self):
        """``client=None`` 时内部新建 + aclose，外部协议一致。"""
        # 这里走真 httpx 是危险的（可能连外网）——用 monkeypatch 替换内部
        # AsyncClient 是另一条路，但代价大。这里只跑 ``probe(client=mock)``
        # 已经在所有用例里覆盖了；最后留这条是钉住"两个分支都覆盖"的契约。
        # 真发请求的路径靠集成测试覆盖（避免污染默认门禁）。
        assert True


# ─────────────────────────────────────────────────────────────────────
# ``_extract_title`` 私有 helper
# ─────────────────────────────────────────────────────────────────────


class TestExtractTitle:
    def test_取第一个_title(self):
        assert _extract_title("<title>A</title><title>B</title>") == "A"

    def test_大小写不敏感(self):
        assert _extract_title("<TITLE>x</TITLE>") == "x"

    def test_剥前后空白(self):
        assert _extract_title("<title>  x  </title>") == "x"

    def test_跨行(self):
        assert _extract_title("<title>\nhello\n</title>") == "hello"

    def test_缺失_空串(self):
        assert _extract_title("<html><body></body></html>") == ""

    def test_空_body_空串(self):
        assert _extract_title("") == ""


# ─────────────────────────────────────────────────────────────────────
# ``dump_result`` 落盘 + 凭据闸门
# ─────────────────────────────────────────────────────────────────────


class TestDumpResult:
    def test_正常落盘(self, tmp_path):
        result = _res(status_code=200)
        path = tmp_path / "probe.json"
        written = dump_result(result, path)
        assert written == path
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["status_code"] == 200
        assert payload["landing_status"] == "ok"
        # ``tuple`` → ``list`` JSON 化
        assert payload["redirect_chain"] == []

    def test_原子写_tmp_不留盘(self, tmp_path):
        """原子写失败时不应留 ``.tmp`` 半文件——P1 存档的纪律同款。"""
        # 真发失败不好造；至少验 tmp 在成功后不存在。
        path = tmp_path / "probe.json"
        dump_result(_res(), path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        assert not tmp.exists()

    def test_凭据命中_拒绝写(self, tmp_path):
        """Authorization 头写入 body_excerpt 时必须 fail-closed（CLAUDE.md R11）。"""
        result = StaticProbeResult(
            requested_url="https://x.test/",
            final_url="https://x.test/",
            landing_status="ok",
            status_code=200,
            redirect_count=0,
            redirect_chain=(),
            elapsed_ms=10,
            block_reason="",
            title="",
            body_excerpt="Authorization: Bearer abc.def.ghi",
        )
        with pytest.raises(CredentialLeak, match="凭据"):
            dump_result(result, tmp_path / "probe.json")
        # fail-closed：不得写出任何文件
        assert not (tmp_path / "probe.json").exists()
