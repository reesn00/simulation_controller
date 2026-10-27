"""camofox-browser 的执行层会话——**MCP stdio 通道 + 标签页栈 + 指纹读数**。

为什么单列一支
--------------
2026-10-26 执行层再换一次：camoufox（playwright 直连）暂退，改走
**camofox-browser 自带的 MCP 适配器**（``camofox-browser-mcp``，11 个 tool）。
换的是**传输层与定位面**，不是「控制流铁律」——

- 浏览器会话 → 本模块（:class:`CamofoxSession`）；
- 动作/采集面 → :mod:`trajectory_pipeline.executor.browser.camofox_driver`；
- 传输层 → :mod:`trajectory_pipeline.executor.browser.mcp_client`（**没改**）。

⚠️ **上游源码不许改**（owner 裁定 2026-10-26）：``C:\\Users\\klpc\\workspace\\tool\\camofox-browser``
是别人的仓库，缺口一律在**本仓库这一侧**消化（驱动内部合并、账目里记降级）。

## 三条与 camoufox 会话**不同**的地方

1. **没有「launch 一个浏览器」这一步**——浏览器由 camofox-browser 的 REST 服务
   （默认 9377）持有，**MCP 适配器只做转发**。所以本会话管的是
   **一条 stdio 连接 + 一组 tab**，不是「一个浏览器进程」。
   ⇒ **REST 服务没起 = 整条链不可用**，且症状是「每个 tool 都失败」，
   所以 :meth:`CamofoxSession.launch` 里有一次**预检**（列 tab），
   失败就当场说清「REST 服务没起」。

2. **标签页的 id 是上游给的**（``tabId``），不是我们自己编的——
   与 obscura 的服务端 tab id 同类。**驱动另发一套自己的句柄**（``c1``/``c2``），
   两套 id 不可互换（同 camoufox 的纪律）。

3. **指纹是「读回来的」而不是「配出来的」**：camoufox 会话自己 launch
   playwright，``humanize``/``geoip``/``locale`` 都是**请求值**；camofox 的这些
   **由上游决定，本仓库配不了**。所以本会话记的是
   **实测的浏览器侧读数**（UA / 平台 / 语言 / 时区 / 屏幕 / 视口 / DPR）
   + **上游不可配**这个事实本身。

## 红线

- **不导入 cookie**：``camofox_import_cookies`` 被 :func:`CamofoxSession.call`
  **硬拦**（不是不用，是不许调）——导入 cookie 等于把登录态带进采集面，
  而本项目的样本必须「裸奔」才有归因意义。
- **凭据不出进程**：``CAMOFOX_ACCESS_KEY`` / ``CAMOFOX_API_KEY`` 由子进程
  继承（不显式传、不落盘、不进指纹）。指纹里只有 REST 服务的 **origin**
  （去掉路径、查询串与用户信息）。
- **不保存浏览器 Profile**：本会话不做任何落盘，退出即弃。
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Mapping

from trajectory_pipeline.executor.browser.mcp_client import (
    McpClient,
    McpError,
    resolve_executable,
)
from trajectory_pipeline.executor.browser.page_driver import DriverError

__all__ = [
    "CamofoxSession",
    "CAMOFOX_BASE_URL_DEFAULT",
    "CAMOFOX_EXE_ENV_VAR",
    "CAMOFOX_EXE_DEFAULT",
    "FORBIDDEN_TOOLS",
    "TOOL_CLOSE_TAB",
    "TOOL_CLICK",
    "TOOL_CREATE_TAB",
    "TOOL_EVALUATE",
    "TOOL_IMPORT_COOKIES",
    "TOOL_LIST_TABS",
    "TOOL_NAVIGATE",
    "TOOL_SNAPSHOT",
    "TOOL_TYPE",
]

#: camofox-browser 的 REST 服务默认地址（上游 ``camofox.config.json`` / 端口 9377）。
CAMOFOX_BASE_URL_DEFAULT = "http://localhost:9377"

#: MCP 适配器命令名。**默认走 PATH 查找**——上游文档（依赖说明 §4.2）给宿主
#: 注册的就是这个命令名（``claude mcp add ... -- camofox-browser-mcp``），
#: 它由 ``npm link`` / 全局安装 / ``npx`` 三种方式之一提供。
CAMOFOX_EXE_DEFAULT = "camofox-browser-mcp"

#: **可覆盖**适配器路径的环境变量名。给了绝对路径就用它（不想依赖 PATH 时用）。
#:
#: ⚠️ 这是**可执行文件的位置**，不是凭据——凭据是 ``CAMOFOX_ACCESS_KEY`` /
#: ``CAMOFOX_API_KEY``，那两个由子进程继承，本模块不碰。
CAMOFOX_EXE_ENV_VAR = "CAMOFOX_MCP_EXE"

#: 上游的 11 个 tool 名。**只在本模块里出现一次**——driver 通过会话方法调用，
#: 不直接拼 tool 名（拼两遍必然漂）。
TOOL_CREATE_TAB = "camofox_create_tab"
TOOL_SNAPSHOT = "camofox_snapshot"
TOOL_CLICK = "camofox_click"
TOOL_TYPE = "camofox_type"
TOOL_NAVIGATE = "camofox_navigate"
TOOL_SCROLL = "camofox_scroll"
TOOL_SCREENSHOT = "camofox_screenshot"
TOOL_CLOSE_TAB = "camofox_close_tab"
TOOL_EVALUATE = "camofox_evaluate"
TOOL_LIST_TABS = "camofox_list_tabs"
TOOL_IMPORT_COOKIES = "camofox_import_cookies"

#: **禁用的 tool**。cookie 导入把登录态带进采集面——本项目的样本要能归因，
#: 就必须是「没登录」的浏览器。所以它**被硬拦**，不是「约定不用」。
FORBIDDEN_TOOLS: frozenset[str] = frozenset({TOOL_IMPORT_COOKIES})

#: MCP 适配器的默认启动超时（秒）。比 obscura 的 60s 长：
#: 上游的 snapshot 要做一次完整的可访问性树 + 一次截图，**慢**。
DEFAULT_TIMEOUT_S = 180.0


def _origin_of(base_url: str) -> str:
    """把 REST 地址压成 **origin**（scheme://host:port）。

    ⚠️ **为什么只留 origin**：地址里可能带路径、查询串，甚至用户信息
    （``http://user:pass@host:9377/…``）。整条地址进存档就等于把凭据抄进
    产物——而指纹是要进批次元数据、进而进样本包的。
    """
    text = str(base_url or "").strip()
    if "://" not in text:
        return text
    scheme, _, rest = text.partition("://")
    rest = rest.split("/", 1)[0]
    if "@" in rest:
        rest = rest.rsplit("@", 1)[1]
    return f"{scheme}://{rest}"


#: 读浏览器侧指纹的 JS。**一次 evaluate 读全部**——分多次读会跨时刻，
#: 而批次元数据要的是「同一个瞬间」的一组读数。
_FINGERPRINT_JS = r"""
(function () {
  var nav = window.navigator || {};
  var scr = window.screen || {};
  var tz = "";
  try {
    if (window.Intl && Intl.DateTimeFormat) {
      tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    }
  } catch (e) { tz = ""; }
  return JSON.stringify({
    user_agent: String(nav.userAgent || ""),
    platform: String(nav.platform || ""),
    language: String(nav.language || ""),
    languages: (nav.languages && nav.languages.length)
      ? Array.prototype.slice.call(nav.languages, 0, 5).map(String) : [],
    timezone: tz,
    screen: [Number(scr.width || 0), Number(scr.height || 0)],
    window: [Number(window.innerWidth || 0), Number(window.innerHeight || 0)],
    dpr: Number(window.devicePixelRatio || 0),
    webdriver: nav.webdriver === true
  });
})()
"""


class CamofoxSession:
    """camofox-browser 的一次执行层会话（**一个 Run 一份**）。

    生命周期：:meth:`launch` →（若干 tab 操作）→ :meth:`close`。
    **一个 Run 一次会话**的纪律同 camoufox：跨 Run 复用会把上一条样本的
    会话状态带进下一条，而归因要的正是「这一条样本是在哪个身份下采到的」。

    标签页是**栈**：:meth:`new_page` 压栈、:meth:`close_page` 弹栈，
    当前页永远是栈顶。栈空了再关就会「关空」——那之后每次观察都在读一个
    已关闭的句柄，症状是「页面读不到」，而真相是「我们自己把它关空了」。
    """

    def __init__(self, client: McpClient, *, base_url: str) -> None:
        self._client = client
        self._base_url = str(base_url or CAMOFOX_BASE_URL_DEFAULT)
        #: **标签页栈**（上游给的 ``tabId``）。栈顶 = 当前页。
        self._pages: list[str] = []
        #: 指纹读数（**实测**）。空 dict = **没测到**，不是「测出来是空的」——
        #: 两者混用会让「没采到指纹」被读成「指纹为空」。
        self._fingerprint: dict[str, Any] = {}
        #: 诊断读数（**不进批次元数据**）：MCP 握手信息、REST 可达性等。
        self._readings: dict[str, Any] = {}

    # ── 建立 / 拆除 ────────────────────────────────────────────────

    @classmethod
    async def launch(
        cls,
        *,
        exe: str = "",
        base_url: str = "",
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> "CamofoxSession":
        """起一条 MCP 通道并**预检** REST 服务。

        Raises:
            RuntimeError: 适配器不可执行（**缺件即报错，不猜**），
                或 REST 服务连不上（**当场说清缺什么**）。
        """
        command = resolve_executable(
            exe or os.environ.get(CAMOFOX_EXE_ENV_VAR, "").strip()
            or CAMOFOX_EXE_DEFAULT,
            env_var=CAMOFOX_EXE_ENV_VAR,
            hint=(
                "安装：npm i -g @askjo/camofox-browser-mcp"
                "（或 cd camofox-browser/mcp && npm link），"
                "也可设 "
                f"{CAMOFOX_EXE_ENV_VAR}=<绝对路径>。"
            ),
        )
        url = str(base_url or os.environ.get("CAMOFOX_BASE_URL", "").strip()
                  or CAMOFOX_BASE_URL_DEFAULT)
        # ⚠️ **env 传 ``None``**：子进程**继承**本进程环境，
        # 于是 ``CAMOFOX_BASE_URL`` / ``CAMOFOX_ACCESS_KEY`` / ``CAMOFOX_API_KEY``
        # 天然到位，而本模块**一个字节都不经手**（凭据不出进程）。
        client = McpClient(command, args=(), timeout=timeout, env=None)
        await client.__aenter__()
        session = cls(client, base_url=url)
        try:
            await session._preflight()
        except Exception:
            await session.close()
            raise
        return session

    async def _preflight(self) -> None:
        """预检：**REST 服务在不在**。

        MCP 适配器只做转发——REST 服务没起时，**每个** tool 都会失败，
        而症状是「一屏 tool 错误」，会被读成「站点的问题」。
        所以这里先列一次 tab，把「服务没起」与「站点有问题」分开。
        """
        try:
            tabs = await self.list_tabs()
        except DriverError as exc:
            raise RuntimeError(
                f"camofox-browser 的 REST 服务连不上（{_origin_of(self._base_url)}）"
                f"：{exc}。MCP 适配器只做转发——**REST 服务要单独起**"
                f"（cd camofox-browser && npm start，默认端口 9377）。"
            ) from exc
        self._readings["rest_tabs"] = len(tabs)
        self._readings["mcp_server"] = self._client.info.name
        self._readings["mcp_version"] = self._client.info.version
        self._readings["mcp_protocol"] = self._client.info.protocol_version
        self._readings["mcp_tool_count"] = self._client.info.tool_count

    async def close(self) -> None:
        """关掉所有 tab 并断开 MCP 通道。**幂等**。"""
        for tab_id in list(reversed(self._pages)):
            try:
                await self.close_tab(tab_id)
            except Exception:      # noqa: BLE001 —— 清理失败不得掩盖主流程
                pass
        self._pages.clear()
        try:
            await self._client.__aexit__(None, None, None)
        except Exception:          # noqa: BLE001
            pass

    async def __aenter__(self) -> "CamofoxSession":
        """支持 ``async with``。**已经 launch 过的会话**才进得来——
        ``launch`` 是类方法，构造出来的东西一定是起好的。"""
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ── 读数 ──────────────────────────────────────────────────────

    @property
    def fingerprint(self) -> dict[str, Any]:
        """本 Run 的指纹读数（批次元数据用）。

        **实测 9 项**（浏览器侧读回来）+ **请求值 2 项**：

        | 键 | 语义 |
        |---|---|
        | ``user_agent`` / ``platform`` / ``language`` / ``languages`` / ``timezone`` | 实测 |
        | ``screen`` / ``window`` / ``dpr`` / ``webdriver`` | 实测 |
        | ``engine`` | 常量（上游驱动的浏览器内核） |
        | ``base_url`` | 请求值（REST 服务 **origin**，已去掉凭据与路径） |

        ⚠️ **camoufox 那套「请求值」在这里没有对应物**：
        ``humanize`` / ``geoip`` / ``locale`` / ``main_world_eval``
        **由上游决定，本仓库配不了**——那不是「读数」，是「本仓库管不着」。
        记 ``"upstream"`` 这个事实，比记一个假读数诚实。
        """
        return dict(self._fingerprint)

    @property
    def readings(self) -> dict[str, Any]:
        """诊断读数（**不进批次元数据**）。"""
        return dict(self._readings)

    @property
    def pages(self) -> tuple[str, ...]:
        """标签页栈（自底向上）。栈顶 = 当前页。"""
        return tuple(self._pages)

    @property
    def current(self) -> str:
        """当前页的 ``tabId``。栈空 ⇒ 抛（**不猜一个空的**）。"""
        if not self._pages:
            raise DriverError("camofox 会话里没有打开的标签页")
        return self._pages[-1]

    # ── tool 调用 ──────────────────────────────────────────────────

    async def call(self, tool: str, args: Mapping[str, Any] | None = None) -> Any:
        """调一个 MCP tool，返回**解析后的 JSON**。

        上游适配器把 REST 的 JSON 响应原样 ``JSON.stringify`` 成一个文本块，
        所以这里解析回来。**解析不了就抛**——那说明返回形状变了，
        而形状变了就该炸穿，不该当成「空结果」继续跑。

        Raises:
            DriverError: tool 报错 / 返回不是 JSON。
            RuntimeError: 调了**禁用**的 tool（cookie 导入）。
        """
        if tool in FORBIDDEN_TOOLS:
            raise RuntimeError(
                f"camofox tool {tool!r} 被禁用：导入 cookie 会把登录态带进采集面，"
                f"而本项目的样本必须是「没登录」的浏览器才有归因意义。"
            )
        try:
            result = await self._client.call(tool, dict(args or {}))
        except McpError as exc:
            raise DriverError(f"camofox tool {tool} 调用失败: {exc}") from None
        if result.is_error:
            raise DriverError(f"camofox tool {tool} 返回错误: {result.text[:400]}")
        text = (result.text or "").strip()
        if not text:
            raise DriverError(f"camofox tool {tool} 返回空串")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise DriverError(
                f"camofox tool {tool} 返回的不是 JSON: {text[:200]!r}"
            ) from exc

    # ── 标签页 ────────────────────────────────────────────────────

    async def new_page(self, url: str = "") -> str:
        """开一张新页并压栈。返回上游的 ``tabId``。"""
        args: dict[str, Any] = {}
        if url:
            args["url"] = url
        payload = await self.call(TOOL_CREATE_TAB, args)
        tab_id = str(payload.get("tabId") or "") if isinstance(payload, Mapping) else ""
        if not tab_id:
            raise DriverError(f"camofox 建页没给 tabId: {payload!r}")
        self._pages.append(tab_id)
        return tab_id

    async def close_page(self, tab_id: str) -> bool:
        """关掉一页。**关的是栈顶就弹栈**；关的不是栈顶也照样从栈里摘掉。

        Returns:
            ``True`` = 关掉了，``False`` = 上游说这个 tab 不存在。
        """
        payload = await self.call(TOOL_CLOSE_TAB, {"tabId": tab_id})
        ok = True
        if isinstance(payload, Mapping):
            ok = bool(payload.get("ok", True))
        if tab_id in self._pages:
            self._pages.remove(tab_id)
        return ok

    async def list_tabs(self) -> list[Any]:
        payload = await self.call(TOOL_LIST_TABS, {})
        if isinstance(payload, Mapping):
            tabs = payload.get("tabs")
            if isinstance(tabs, list):
                return tabs
        if isinstance(payload, list):
            return payload
        return []

    # ── 页面操作 ──────────────────────────────────────────────────

    async def navigate(self, tab_id: str, url: str) -> Any:
        return await self.call(TOOL_NAVIGATE, {"tabId": tab_id, "url": url})

    async def snapshot(self, tab_id: str, offset: int | None = None) -> Any:
        """取可访问性快照（**带 ``[eN]`` 句柄的注记 YAML**）。

        ``offset`` 是上游的分页偏移（字符）。⚠️ **上游在 ``offset>0`` 时
        复用缓存的快照**（不刷新 ref），所以一次分页读取内部是**自洽**的；
        而下一次 ``offset=0`` 的读取会**重新编号** ref——
        所以「读完一页就动作」是安全的，「读了一半就动作」不是。
        """
        args: dict[str, Any] = {"tabId": tab_id}
        if offset is not None:
            args["offset"] = int(offset)
        return await self.call(TOOL_SNAPSHOT, args)

    async def click(self, tab_id: str, ref: str) -> Any:
        return await self.call(TOOL_CLICK, {"tabId": tab_id, "ref": ref})

    async def type_text(
        self, tab_id: str, ref: str, text: str, *, press_enter: bool = False
    ) -> Any:
        return await self.call(
            TOOL_TYPE,
            {"tabId": tab_id, "ref": ref, "text": text, "pressEnter": bool(press_enter)},
        )

    async def evaluate(self, tab_id: str, expression: str) -> Any:
        """在页面上下文求值。

        上游返回 ``{"ok": true, "result": <值>}``——**``result`` 才是值**。
        求值失败上游会抛（HTTP 错误）⇒ 本方法抛 :class:`DriverError`。
        """
        payload = await self.call(TOOL_EVALUATE, {"tabId": tab_id, "expression": expression})
        if isinstance(payload, Mapping):
            return payload.get("result")
        return payload

    # ── 指纹 ──────────────────────────────────────────────────────

    async def measure_fingerprint(self) -> dict[str, Any]:
        """读一次浏览器侧指纹并**缓存**。读不到 ⇒ 空 dict + 记进 ``readings``。

        ⚠️ **空 dict = 没测到**，不是「测出来是空的」——两者在批次元数据里
        必须分得开（读档人会把「没测到」读成「指纹为空」）。
        """
        if self._fingerprint:
            return dict(self._fingerprint)
        try:
            raw = await self.evaluate(self.current, _FINGERPRINT_JS)
        except DriverError as exc:
            self._readings["fingerprint_error"] = str(exc)
            return {}
        if not isinstance(raw, str) or not raw.strip():
            self._readings["fingerprint_error"] = "读回来不是字符串"
            return {}
        try:
            measured = json.loads(raw)
        except ValueError as exc:
            self._readings["fingerprint_error"] = f"不是 JSON: {exc}"
            return {}
        if not isinstance(measured, Mapping):
            self._readings["fingerprint_error"] = "不是对象"
            return {}
        self._fingerprint = {
            # ── 实测（浏览器侧）──
            "user_agent": str(measured.get("user_agent") or ""),
            "platform": str(measured.get("platform") or ""),
            "language": str(measured.get("language") or ""),
            "languages": list(measured.get("languages") or []),
            "timezone": str(measured.get("timezone") or ""),
            "screen": list(measured.get("screen") or []),
            "window": list(measured.get("window") or []),
            "dpr": measured.get("dpr"),
            "webdriver": bool(measured.get("webdriver")),
            # ── 本仓库能控 / 能说清的 ──
            #
            # ⚠️ ``engine`` 仍是 **"camoufox"**：camofox-browser 底层依赖的是
            # ``camoufox-js``（上游 package.json），**浏览器内核没换**——
            # 换的是**接入方式**（playwright 直连 → 上游 REST + MCP 适配器）。
            # 所以这一项**跨批次可比**：拿它与旧 camoufox 批次的 ``engine``
            # 对读，是同一件事。
            "engine": "camoufox",
            # REST 服务的 **origin**（已去掉路径、查询串与用户信息）——
            # 凭据不许进存档（红线），所以只留 origin。
            "base_url": _origin_of(self._base_url),
        }
        return dict(self._fingerprint)

    @staticmethod
    def new_user_id() -> str:
        """一个新的会话隔离标识。

        ⚠️ **默认不设** ``CAMOFOX_USER_ID``——上游适配器自己会生成
        ``mcp-<uuid>``，于是「一条 MCP 通道 = 一个隔离会话」天然成立。
        本函数只给「必须显式隔离」的场景留个口子。
        """
        return f"trajectory-{uuid.uuid4().hex[:12]}"
