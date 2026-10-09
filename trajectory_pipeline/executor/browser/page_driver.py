"""``PageDriver`` 协议——执行层与浏览器之间**唯一的稳定契约**。

设计约束：**只暴露原子原语，不封装任何业务语义**。

「播放按钮算不算剧集」「预告片要不要排除」这类判断**绝不能进这里**——
它们属于 ``executor/steps/``。原因是：本协议是"驱动面"，
一旦业务语义漏进来，执行层就再也无法在不改动驱动的前提下更换浏览器
（换 obscura → 换别的 MCP server）。

唯一的组合方法是 :meth:`PageDriver.observe`——它把多个原语拼成一个
``Observation`` 值对象，属于**数据采集**而非**业务判断**，故允许在此。
业务上的 trailer_only 词表过滤**不在** observe 里做，而在 steps 层做，
observe 返回的是未过滤的原始交互元素。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from trajectory_pipeline.executor.dom import CleanedLink, CleanedSnapshot
from trajectory_pipeline.perception.base import InteractiveElement, Observation


@runtime_checkable
class PageDriver(Protocol):
    """浏览器驱动。

    生命周期：实现通常持有**一条长连接**（obscura 每连接一个 V8 isolate），
    多站点遍历应复用连接并开 tab 切换，而不是每步重连。
    """

    name: str

    # ── 原子原语 ──────────────────────────────────────────────────

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        """导航到 ``url``。

        Raises:
            DriverError: 导航失败（网络错误 / 超时）。**调用方须 fail-closed**——
                不得当成"页面是空的"继续走，那会把网络故障误判成"站点没内容"。
        """
        ...

    async def snapshot(
        self, *, max_chars: int | None = None, degraded: list[str] | None = None
    ) -> CleanedSnapshot:
        """取当前页快照并做 DOM 预处理（分离 URL/Title、剥离 CSS/JS）。

        Args:
            degraded: 传入 list 时，**正文采集降级会把来源记进去**。
                正文主路径（``innerText``）取不到时会退回快照正文，
                那份正文可能整段是 CSS 被剥光后的空壳——不记下来，
                下游会把「正文没采到」当成「这页没内容」。
        """
        ...

    async def links(self, *, limit: int = 50) -> tuple[CleanedLink, ...]:
        """列出页面上的链接。空页返回空元组（不抛异常）。"""
        ...

    async def interactive(self, *, limit: int = 50) -> tuple[InteractiveElement, ...]:
        """列出可交互元素及其**稳定 ref**（形如 ``e1``，导航前有效）。"""
        ...

    async def count(self, selector: str) -> int:
        """计数匹配 CSS selector 的元素数。探测失败返回 0。"""
        ...

    async def evaluate(self, expression: str) -> Any:
        """在页面上下文求值。

        ⚠️ 注意 ``isTrusted``：合成事件的 ``isTrusted`` 为 false，
        而 stealth 下 obscura 内部产生的事件为 true。
        **点击一律走 :meth:`click`**，不要用 evaluate 伪造点击。
        """
        ...

    async def click(self, *, ref: str | None = None, selector: str | None = None) -> None:
        """点击元素。优先用 ``ref``（来自 :meth:`interactive`）。

        元素不存在时**不抛异常**——那是业务事实（该元素不在页面上），
        由上层转成 ``answer=None`` 走 unresolved，而不是让整站作废。
        """
        ...

    async def type_text(
        self, *, text: str, ref: str | None = None, selector: str | None = None
    ) -> None:
        """向输入框填入文本（优先 ``ref``）。"""
        ...

    async def press_key(self, key: str) -> None:
        """派发按键（如 ``Enter``）。"""
        ...

    async def new_tab(self, url: str | None = None) -> str:
        """新开标签页，返回 tab id。用于多站点遍历时隔离会话。"""
        ...

    async def close_tab(self, tab_id: str | None = None) -> None:
        """关闭标签页（``None`` = 当前活动页）。"""
        ...

    # ── 组合采集（无业务语义）────────────────────────────────────

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        """采集当前页的结构化观察。

        调用链：snapshot + links + interactive + count(video,audio) + count(iframe)。
        任何一项失败**不让整个 observe 失败**——该项降级为空/0，
        并由 ``Observation`` 的元数据反映；只有 snapshot 本身失败才抛
        ``DriverError``（没有观察就没有判定，I4 fail-closed）。
        """
        ...

    async def close(self) -> None:
        """关闭连接。必须可重复调用，且关闭失败不得掩盖主流程异常。"""
        ...


class DriverError(RuntimeError):
    """驱动层失败（导航失败 / 连接断开 / 协议错误）。

    与「元素不存在」区分开：后者是业务事实，走 ``answer=None``；
    前者是能力不可用，必须 fail-closed。
    """