"""MCP stdio 客户端——传输层。

职责边界：本模块只管「把 JSON-RPC 送出去、把文本取回来」，
**不认识 obscura 的任何 tool 名，也不解析任何返回格式**。
obscura 的 tool 语义与返回格式知识全部在 ``obscura_driver.py``。

分层理由：传输层若混入格式知识，换浏览器时（MCP server 换了）整个模块都得重写；
而格式知识留在 driver 里，``PageDriver`` 协议对上层保持稳定。

失败语义：传输失败抛 :class:`McpError`。调用方**据此 fail-closed**
（感知层不变式 I4），不得回退到猜测或默认值。

用法::

    async with McpClient.from_env() as mc:
        info = await mc.initialize()
        result = await mc.call("browser_navigate", {"url": "https://example.com"})
        print(result.text)
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

#: obscura 的默认调用形态。``--stealth`` 仅在 stealth build 存在该 flag，
#: 非 stealth build 传了会报未知参数而不是静默忽略——故在 ``from_env`` 里可关。
DEFAULT_ARGS: tuple[str, ...] = ("mcp", "--stealth")

#: obscura 可执行文件的环境变量名。**不硬编码本机绝对路径**，
#: 也不从仓库配置读（含凭据的配置文件不应被工具层触碰）。
EXE_ENV_VAR = "OBSCURA_EXE"


@dataclass(frozen=True, slots=True)
class McpServerInfo:
    """握手结果——W1 首次接入时用来核对 server 版本与 tool 数是否漂移。"""

    name: str
    version: str
    protocol_version: str
    tool_count: int


@dataclass(frozen=True, slots=True)
class ToolResult:
    """一次 tool 调用的结果。

    ``is_error`` **不在此处抛异常**：tool 层主动返回的错误与传输失败是不同的事，
    前者属于业务语义（如"元素不存在"），由 driver 决定如何处置；
    后者才是传输故障，由 :class:`McpError` 表达。
    """

    text: str
    is_error: bool
    structured: dict[str, Any] | None
    latency_ms: int


class McpError(RuntimeError):
    """传输层失败（进程起不来 / 超时 / 协议错误）。调用方须 fail-closed。"""


class McpClient:
    """obscura MCP server 的异步会话。

    生命周期：一个 client 对应一条 stdio 连接和一个 obscura 进程；
    obscura 每个连接自带 V8 isolate，故**多站点遍历应复用同一连接**
    （开 tab 切换），而不是每步重连。
    """

    def __init__(
        self,
        exe: str,
        args: tuple[str, ...] = DEFAULT_ARGS,
        timeout: float = 60.0,
    ) -> None:
        self._exe = exe
        self._args = tuple(args)
        self._timeout = timeout
        self._devnull: Any = None
        self._stdio_cm: Any = None
        self._session_cm: Any = None
        self._session: ClientSession | None = None
        self._info: McpServerInfo | None = None

    @classmethod
    def from_env(cls, *, stealth: bool = True, timeout: float = 60.0) -> "McpClient":
        """从 ``OBSCURA_EXE`` 环境变量构造。

        Raises:
            RuntimeError: 环境变量未设置——**不猜路径**。凭经验猜一个
                ``obscura`` 命令名去 PATH 找，会在换机器时静默走到别的实现上。
        """
        exe = os.environ.get(EXE_ENV_VAR, "").strip()
        if not exe:
            raise RuntimeError(
                f"未设置 {EXE_ENV_VAR}，无法定位 obscura 可执行文件。"
                f"请先 export {EXE_ENV_VAR}=<obscura.exe 绝对路径>"
            )
        if not Path(exe).exists():
            raise RuntimeError(f"{EXE_ENV_VAR} 指向的文件不存在: {exe}")
        return cls(exe, ("mcp", "--stealth") if stealth else ("mcp",), timeout)

    async def __aenter__(self) -> "McpClient":
        params = StdioServerParameters(command=self._exe, args=list(self._args))
        # obscura 的 banner / 日志走 stderr。Windows 下 sys.stderr 默认 GBK，
        # obscura 输出 UTF-8 时会抛 UnicodeEncodeError 把整条链带崩——故丢弃而非转发。
        self._devnull = open(os.devnull, "w", encoding="utf-8")
        try:
            self._stdio_cm = stdio_client(params, errlog=self._devnull)
            read, write = await self._stdio_cm.__aenter__()
            self._session_cm = ClientSession(read, write)
            self._session = await self._session_cm.__aenter__()
            await self._initialize()
        except Exception as exc:
            await self.__aexit__(type(exc), exc, exc.__traceback__)
            raise McpError(
                f"obscura MCP 连接失败（exe={self._exe}, args={self._args}）: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        for cm in (self._session_cm, self._stdio_cm):
            if cm is None:
                continue
            try:
                await cm.__aexit__(*exc_info)
            except Exception:
                # 关闭失败不得掩盖主流程的异常，也不该让连接泄漏拖垮批处理
                pass
        self._session = self._stdio_cm = self._session_cm = None
        if self._devnull is not None:
            try:
                self._devnull.close()
            except Exception:
                pass
            self._devnull = None

    async def _initialize(self) -> None:
        assert self._session is not None
        init = await asyncio.wait_for(self._session.initialize(), timeout=self._timeout)
        info = getattr(init, "serverInfo", None)
        listing = await asyncio.wait_for(self._session.list_tools(), timeout=self._timeout)
        self._info = McpServerInfo(
            name=getattr(info, "name", "") or "",
            version=getattr(info, "version", "") or "",
            protocol_version=getattr(init, "protocolVersion", "") or "",
            tool_count=len(listing.tools),
        )

    @property
    def info(self) -> McpServerInfo:
        """握手信息。调用前必须已 ``__aenter__``。"""
        if self._info is None:
            raise McpError("MCP 会话未初始化（info 不可用）")
        return self._info

    async def list_tools(self) -> list[dict[str, Any]]:
        """列出全部 tool 的名称/描述/入参 schema。用于接入自检与版本回归。"""
        if self._session is None:
            raise McpError("MCP 会话未初始化")
        listing = await asyncio.wait_for(self._session.list_tools(), timeout=self._timeout)
        return [
            {
                "name": t.name,
                "description": (t.description or "").strip(),
                "input_schema": t.inputSchema,
            }
            for t in listing.tools
        ]

    async def call(self, name: str, args: dict[str, Any] | None = None) -> ToolResult:
        """调用一个 tool。

        Raises:
            McpError: 传输失败（超时 / 进程退出 / 协议错误）。调用方须 fail-closed。
        """
        if self._session is None:
            raise McpError("MCP 会话未初始化")
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, args or {}), timeout=self._timeout
            )
        except asyncio.TimeoutError as exc:
            raise McpError(f"tool {name} 超时（>{self._timeout}s）") from exc
        except Exception as exc:
            raise McpError(f"tool {name} 调用失败: {type(exc).__name__}: {exc}") from exc
        text = "".join(
            getattr(block, "text", "") or "" for block in (result.content or [])
        )
        return ToolResult(
            text=text,
            is_error=bool(getattr(result, "isError", False)),
            structured=getattr(result, "structuredContent", None),
            latency_ms=int((time.monotonic() - started) * 1000),
        )