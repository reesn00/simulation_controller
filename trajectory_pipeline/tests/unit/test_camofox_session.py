"""camofox 会话层的**口径**测试——**不联网、不起浏览器**。

三件都是「改错了不报错、但整批不可归因」的形状：

1. **预检要把「REST 服务没起」与「站点有问题」分开**。MCP 适配器只做转发——
   REST 服务没起时**每个** tool 都失败，而症状是「一屏 tool 错误」，
   会被读成「站点的问题」⇒ 一整批站点被判成反爬或不可达。
2. **cookie 导入被硬拦**（不是「约定不用」）。导入 cookie 等于把登录态带进
   采集面，而本项目的样本必须「裸奔」才有归因意义。
3. **凭据不出进程**：``CAMOFOX_ACCESS_KEY`` / ``CAMOFOX_API_KEY`` 由子进程
   **继承**（不显式传、不落盘、不进指纹）；指纹里只有 REST 服务的 **origin**
   （去掉路径、查询串与用户信息）。

⚠️ **本文件不真起 MCP 通道**：``McpClient`` 被打桩成假实现
（``camofox_session`` 在模块层 import 它 ⇒ 打桩点在本模块的命名空间里）。
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from trajectory_pipeline.executor.browser import camofox_session as mod
from trajectory_pipeline.executor.browser.camofox_session import (
    CAMOFOX_BASE_URL_DEFAULT,
    CAMOFOX_EXE_DEFAULT,
    FORBIDDEN_TOOLS,
    TOOL_CLOSE_TAB,
    TOOL_CREATE_TAB,
    TOOL_EVALUATE,
    TOOL_IMPORT_COOKIES,
    TOOL_LIST_TABS,
    CamofoxSession,
)
from trajectory_pipeline.executor.browser.mcp_client import (
    McpError,
    McpServerInfo,
    ToolResult,
)
from trajectory_pipeline.executor.browser.page_driver import DriverError


# ─────────────── 替身 ───────────────


def _ok(payload: Any) -> ToolResult:
    """一次成功的 tool 返回（上游把 JSON **字符串化**成一个文本块）。"""
    return ToolResult(
        text=json.dumps(payload, ensure_ascii=False),
        is_error=False,
        structured=None,
        latency_ms=1,
    )


def _err(text: str) -> ToolResult:
    return ToolResult(text=text, is_error=True, structured=None, latency_ms=1)


class _FakeMcp:
    """假 MCP 客户端——只实现会话用到的那几个口子。"""

    def __init__(self, exe: str, args: tuple[str, ...] = (),
                 timeout: float = 60.0, env: Any = None) -> None:
        self.exe = exe
        self.args = tuple(args)
        self.timeout = timeout
        self.env = env
        self.info = McpServerInfo(
            name="camofox-browser-mcp",
            version="0.1.0",
            protocol_version="2025-06-18",
            tool_count=11,
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, Any] = {}
        self.default: Any = None
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> "_FakeMcp":
        self.entered += 1
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.exited += 1

    async def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        self.calls.append((name, dict(args or {})))
        resp = self.responses.get(name, self.default)
        if resp is None:
            raise McpError(f"fake: 未配置 tool {name}")
        if isinstance(resp, Exception):
            raise resp
        return resp


class _FakeMcpFactory:
    """``McpClient`` 的替身工厂。

    ⚠️ **为什么要「下一次」这个概念**：``launch`` 在**内部**造 client，
    测试没法在它造出来之前配置它的返回。所以配置得**提前**给工厂，
    由工厂在建实例时套上去。
    """

    def __init__(self) -> None:
        self.clients: list[_FakeMcp] = []
        #: 套在**下一个** client 上的 tool 返回。
        self.next_responses: dict[str, Any] = {}
        #: 下一个 client 的兜底返回（默认：list_tabs 返回空列表 ⇒ 预检过）。
        self.next_default: Any = _ok({"tabs": []})

    def __call__(self, exe: str, args: tuple[str, ...] = (), timeout: float = 60.0,
                 env: Any = None) -> _FakeMcp:
        client = _FakeMcp(exe, args, timeout, env)
        client.responses.update(self.next_responses)
        client.default = self.next_default
        self.clients.append(client)
        return client

    @property
    def client(self) -> _FakeMcp:
        return self.clients[-1]


@pytest.fixture()
def fake_mcp(monkeypatch: pytest.MonkeyPatch) -> _FakeMcpFactory:
    """把 ``McpClient`` 换成假实现。"""
    factory = _FakeMcpFactory()
    monkeypatch.setattr(mod, "McpClient", factory)
    return factory


#: 浏览器侧指纹读数（一次 evaluate 读全）。
_FP_JSON = json.dumps({
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) "
                  "Gecko/20100101 Firefox/156.0",
    "platform": "Win32",
    "language": "zh-CN",
    "languages": ["zh-CN", "zh"],
    "timezone": "Asia/Shanghai",
    "screen": [1920, 1080],
    "window": [1905, 940],
    "dpr": 1.0,
    "webdriver": False,
}, ensure_ascii=False)


# ─────────────── 预检 ───────────────


class Test预检:
    async def test_适配器找不到就报错不猜(self, fake_mcp: _FakeMcpFactory) -> None:
        """**缺件即报错，不猜**（与 ``OBSCURA_EXE`` 同纪律）。

        猜一个同名程序 ⇒ 「配错了」与「配对了」产出同一份数据，
        而症状在报告上看不出异常。
        """
        with pytest.raises(RuntimeError) as exc:
            await CamofoxSession.launch(
                exe="definitely-not-a-real-camofox-mcp-xyz")
        assert "找不到" in str(exc.value)
        assert "npm" in str(exc.value)          # 说了怎么装
        assert not fake_mcp.clients              # 没起通道

    async def test_REST_服务没起要说清缺什么(self, fake_mcp: _FakeMcpFactory) -> None:
        """REST 服务没起 = **每个** tool 都失败，症状是「一屏 tool 错误」。

        不预检 ⇒ 那屏错误会被读成「站点的问题」⇒ 整批站点被判成反爬。
        """
        fake_mcp.next_responses[TOOL_LIST_TABS] = _err("ECONNREFUSED")
        with pytest.raises(RuntimeError) as exc:
            await CamofoxSession.launch(
                exe=sys.executable, base_url="http://localhost:9377")
        msg = str(exc.value)
        assert "REST" in msg
        assert "9377" in msg
        assert "npm start" in msg

    async def test_预检通过后记下握手读数(self, fake_mcp: _FakeMcpFactory) -> None:
        session = await CamofoxSession.launch(exe=sys.executable)
        try:
            assert session.readings["rest_tabs"] == 0
            assert session.readings["mcp_server"] == "camofox-browser-mcp"
            assert session.readings["mcp_tool_count"] == 11
        finally:
            await session.close()

    async def test_预检失败会把通道收掉(self, fake_mcp: _FakeMcpFactory) -> None:
        """半途失败必须断开 MCP 通道——留着就是**进程泄漏**，
        而症状是「机器越来越慢、最后 OOM」，跟这里完全对不上。"""
        fake_mcp.next_responses[TOOL_LIST_TABS] = _err("ECONNREFUSED")
        with pytest.raises(RuntimeError):
            await CamofoxSession.launch(exe=sys.executable)
        assert fake_mcp.client.exited == 1


# ─────────────── 禁用 tool ───────────────


class Test禁用Cookie导入:
    async def test_cookie_导入被硬拦(self, fake_mcp: _FakeMcpFactory) -> None:
        """导入 cookie 等于把登录态带进采集面 ⇒ **被禁用**，不是「不用」。"""
        session = await CamofoxSession.launch(exe=sys.executable)
        try:
            with pytest.raises(RuntimeError) as exc:
                await session.call(TOOL_IMPORT_COOKIES, {"cookies": []})
            assert "禁用" in str(exc.value)
        finally:
            await session.close()

    async def test_被拦的_tool_根本没发出去(self, fake_mcp: _FakeMcpFactory) -> None:
        """拦在**发出去之前**——发出去就等于登录态已经进了浏览器。"""
        session = await CamofoxSession.launch(exe=sys.executable)
        try:
            with pytest.raises(RuntimeError):
                await session.call(TOOL_IMPORT_COOKIES, {"cookies": []})
            names = [name for name, _ in fake_mcp.client.calls]
            assert TOOL_IMPORT_COOKIES not in names
        finally:
            await session.close()

    def test_禁用清单只含_cookie_导入(self) -> None:
        """清单**只增不减**，且不许把别的一起禁掉（那会让能力静默消失）。"""
        assert FORBIDDEN_TOOLS == frozenset({TOOL_IMPORT_COOKIES})


# ─────────────── tool 调用的失败语义 ───────────────


class TestTool失败语义:
    async def test_tool_报错抛_DriverError(self, fake_mcp: _FakeMcpFactory) -> None:
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_LIST_TABS] = _err("ECONNREFUSED")
        try:
            with pytest.raises(DriverError):
                await session.list_tabs()
        finally:
            await session.close()

    async def test_返回不是_JSON_抛_DriverError(self, fake_mcp: _FakeMcpFactory) -> None:
        """形状变了就该**炸穿**——当成「空结果」继续跑，下游读到的是
        「这个站没有链接」而不是「上游换了格式」。"""
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_LIST_TABS] = ToolResult(
            text="not json at all", is_error=False, structured=None, latency_ms=1)
        try:
            with pytest.raises(DriverError):
                await session.list_tabs()
        finally:
            await session.close()

    async def test_返回空串抛_DriverError(self, fake_mcp: _FakeMcpFactory) -> None:
        """空串 ≠ 「空结果」——空串是**没测到**。"""
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_LIST_TABS] = ToolResult(
            text="   ", is_error=False, structured=None, latency_ms=1)
        try:
            with pytest.raises(DriverError):
                await session.list_tabs()
        finally:
            await session.close()

    async def test_传输失败也抛_DriverError(self, fake_mcp: _FakeMcpFactory) -> None:
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_LIST_TABS] = McpError("stdio 断了")
        try:
            with pytest.raises(DriverError):
                await session.list_tabs()
        finally:
            await session.close()


# ─────────────── 标签页栈 ───────────────


class Test标签页:
    async def test_建页压栈且返回上游_tabId(self, fake_mcp: _FakeMcpFactory) -> None:
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_CREATE_TAB] = _ok({"tabId": "t1"})
        try:
            tab = await session.new_page("https://example.com/")
            assert tab == "t1"
            assert session.pages == ("t1",)
            assert session.current == "t1"
        finally:
            await session.close()

    async def test_建页没给_tabId_抛(self, fake_mcp: _FakeMcpFactory) -> None:
        """没给 tabId ⇒ 后续动作**没有目标页**——不猜一个空的。"""
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_CREATE_TAB] = _ok({"url": "https://example.com/"})
        try:
            with pytest.raises(DriverError):
                await session.new_page("https://example.com/")
        finally:
            await session.close()

    async def test_关页弹栈(self, fake_mcp: _FakeMcpFactory) -> None:
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_CREATE_TAB] = _ok({"tabId": "t1"})
        fake_mcp.client.responses[TOOL_CLOSE_TAB] = _ok({"ok": True})
        try:
            await session.new_page()
            assert session.pages == ("t1",)
            await session.close_page("t1")
            assert session.pages == ()
        finally:
            await session.close()

    async def test_栈空时_当前页_抛(self, fake_mcp: _FakeMcpFactory) -> None:
        """栈空着还去读当前页 ⇒ 每次观察都在读一个**已关闭的句柄**，
        症状是「页面读不到」，而真相是「我们自己把它关空了」。"""
        session = await CamofoxSession.launch(exe=sys.executable)
        try:
            with pytest.raises(DriverError):
                _ = session.current
        finally:
            await session.close()


# ─────────────── evaluate ───────────────


class TestEvaluate:
    async def test_取的是_result_不是整个载荷(self, fake_mcp: _FakeMcpFactory) -> None:
        """上游返回 ``{"ok": true, "result": <值>}``——**``result`` 才是值**。
        整个载荷当值 ⇒ 读出来是 ``{"ok": true, ...}``，看着像有内容。"""
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_CREATE_TAB] = _ok({"tabId": "t1"})
        fake_mcp.client.responses[TOOL_EVALUATE] = _ok({"ok": True, "result": "42"})
        try:
            await session.new_page()
            assert await session.evaluate("t1", "1+1") == "42"
        finally:
            await session.close()

    async def test_载荷不是对象时原样返回(self, fake_mcp: _FakeMcpFactory) -> None:
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_CREATE_TAB] = _ok({"tabId": "t1"})
        fake_mcp.client.responses[TOOL_EVALUATE] = _ok([1, 2, 3])
        try:
            await session.new_page()
            assert await session.evaluate("t1", "[1,2,3]") == [1, 2, 3]
        finally:
            await session.close()


# ─────────────── 指纹 ───────────────


class Test指纹:
    async def test_实测九项齐全且_engine_是_camoufox(
        self, fake_mcp: _FakeMcpFactory
    ) -> None:
        """``engine`` 仍是 **camoufox**：上游底层依赖 ``camoufox-js``，
        **浏览器内核没换**——换的是**接入方式**。
        ⇒ 这一项**跨批次可比**（与旧 camoufox 批次对读是同一件事）。"""
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_CREATE_TAB] = _ok({"tabId": "t1"})
        fake_mcp.client.responses[TOOL_EVALUATE] = _ok({"ok": True, "result": _FP_JSON})
        try:
            await session.new_page()
            fp = await session.measure_fingerprint()
            assert set(fp) == {
                "user_agent", "platform", "language", "languages", "timezone",
                "screen", "window", "dpr", "webdriver",
                "engine", "base_url",
            }
            assert fp["engine"] == "camoufox"
            assert fp["screen"] == [1920, 1080]
            assert fp["window"] == [1905, 940]
            assert fp["dpr"] == 1.0
            assert fp["timezone"] == "Asia/Shanghai"
            assert fp["webdriver"] is False
        finally:
            await session.close()

    async def test_读不到指纹是空账_不是空读数(
        self, fake_mcp: _FakeMcpFactory
    ) -> None:
        """空 dict = **没测到**，不是「测出来是空的」——
        两者在批次元数据里必须分得开（读档人会把「没测到」读成「指纹为空」）。"""
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_CREATE_TAB] = _ok({"tabId": "t1"})
        fake_mcp.client.responses[TOOL_EVALUATE] = _err("炸了")
        try:
            await session.new_page()
            fp = await session.measure_fingerprint()
            assert fp == {}
            assert session.readings.get("fingerprint_error")
        finally:
            await session.close()

    async def test_指纹里没有任何凭据(self, fake_mcp: _FakeMcpFactory) -> None:
        """红线：凭据 / Cookie / 登录态**不许进存档**。"""
        session = await CamofoxSession.launch(exe=sys.executable)
        fake_mcp.client.responses[TOOL_CREATE_TAB] = _ok({"tabId": "t1"})
        fake_mcp.client.responses[TOOL_EVALUATE] = _ok({"ok": True, "result": _FP_JSON})
        try:
            await session.new_page()
            await session.measure_fingerprint()
            blob = repr(session.fingerprint) + repr(session.readings)
            for banned in ("cookie", "localStorage", "sessionStorage",
                           "token", "access_key", "api_key", "password"):
                assert banned not in blob
        finally:
            await session.close()

    def test_base_url_只留_origin(self) -> None:
        """地址里可能带路径、查询串，甚至**用户信息**——
        整条进存档就等于把凭据抄进产物。"""
        assert mod._origin_of(
            "http://user:pass@localhost:9377/api/v1?k=1") == "http://localhost:9377"

    def test_默认地址是上游的_9377(self) -> None:
        assert CAMOFOX_BASE_URL_DEFAULT == "http://localhost:9377"

    def test_适配器命令名是上游注册的那个(self) -> None:
        """上游文档给宿主注册的就是 ``camofox-browser-mcp``——
        换成别的名字就找不到同一个程序。"""
        assert CAMOFOX_EXE_DEFAULT == "camofox-browser-mcp"


# ─────────────── 生命周期 ───────────────


class Test生命周期:
    async def test_close_幂等(self, fake_mcp: _FakeMcpFactory) -> None:
        session = await CamofoxSession.launch(exe=sys.executable)
        await session.close()
        await session.close()          # 再关一次不许炸
        assert session.pages == ()

    async def test_async_with_退出即关(self, fake_mcp: _FakeMcpFactory) -> None:
        async with await CamofoxSession.launch(exe=sys.executable) as session:
            assert session.readings.get("mcp_server")
        assert fake_mcp.client.exited == 1

    async def test_凭据靠继承不靠显式传(self, fake_mcp: _FakeMcpFactory) -> None:
        """``env=None`` ⇒ 子进程**继承**本进程环境，
        于是 ``CAMOFOX_ACCESS_KEY`` / ``CAMOFOX_API_KEY`` 天然到位，
        而本模块**一个字节都不经手**。"""
        await CamofoxSession.launch(exe=sys.executable)
        assert fake_mcp.client.env is None
        assert fake_mcp.client.args == ()
