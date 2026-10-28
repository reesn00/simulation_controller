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

from typing import Any, Mapping, Protocol, runtime_checkable

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

    async def extract(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        """按 ``{字段名: CSS selector}`` 抽取结构化内容。**原子原语。**

        ``'a@href'`` 取属性，字段名加 ``[]`` 取全部匹配（值为数组）。
        抽不到 / 选择器失效返回空映射，**不抛异常**——那是页面事实，
        由上层决定是 fail-closed 还是换判据。

        存在的理由：``links()`` 的 ``text`` 对搜索结果页往往**不是标题**
        （实测 bing 只给面包屑 ``qq.com https://v.qq.com › cover``），
        而「标题 ↔ URL 的对应」正是判断点 ① 的全部判据。本原语不含任何
        业务语义——**选哪个 selector 是 :mod:`~trajectory_pipeline.executor.steps.search`
        的事**，驱动层只负责按 selector 取。
        """
        ...

    async def evaluate(self, expression: str) -> Any:
        """在页面上下文求值。

        ⚠️ 注意 ``isTrusted``：合成事件的 ``isTrusted`` 为 false，
        而 stealth 下 obscura 内部产生的事件为 true。
        **点击一律走 :meth:`click`**，不要用 evaluate 伪造点击。
        """
        ...

    async def click(
        self,
        *,
        ref: str | None = None,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> None:
        """点击元素。

        **参数按「面」分家（D12），同名不同参**：

        | 面 | 目标编码 | 实现 |
        |---|---|---|
        | obscura | ``ref``（来自 :meth:`interactive`）或 ``selector`` | ``browser_click`` |
        | camoufox | ``role`` + ``name``（``get_by_role(…, exact=True)``） | :meth:`CamoufoxDriver.click` |

        每个实现**必须显式拒绝另一面的参数**，并**抛 ``ValueError`` 而不是
        :class:`DriverError`**：静默忽略会让「按 ref 点」与「按 role+name 点」
        在调用方看起来一样，而其中一条什么都没点；而 ``DriverError`` 会被
        ``steps/aggregator`` 吃成 ``unreachable_hard``——于是一个接线 bug
        变成一条**理由通顺的负样本**。跨面调用是接线错误，必须炸穿。

        元素不存在时**不抛异常**——那是业务事实（该元素不在页面上），
        由上层转成 ``answer=None`` 走 unresolved，而不是让整站作废。
        """
        ...

    async def fill(
        self,
        *,
        text: str,
        ref: str | None = None,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> None:
        """向输入框**设值**（先清空再写，不是追加）。

        面别同 :meth:`click`：

        | 面 | 目标编码 | 实现 |
        |---|---|---|
        | obscura | ``ref`` / ``selector`` | ``browser_fill`` |
        | camoufox | ``role`` + ``name`` | ``locator.fill(text)`` |

        ⚠️ **名字从 ``type_text`` 改过来是有意的**：旧名暗示「逐字追加」，
        而两个实现做的都是设值——名字撒谎，迟早有人照着它写个真追加。

        ⚠️ **``<select>`` 不归本方法**：playwright 的 ``fill`` 只对可编辑元素有效，
        对 ``<select>`` 会失败。``combobox`` 因此不在枚举词表要填的那一档里。
        """
        ...

    async def press(self, key: str, *, role: str | None = None, name: str | None = None) -> None:
        """派发按键（如 ``Enter``）。

        两种形态：
        - 不带目标 → 页面级按键（``page.keyboard.press``；obscura ``browser_press_key``）；
        - 带 ``role``/``name`` → 在元素上按键（``locator.press``）——仅 camoufox 面。

        旧名 ``press_key`` 已随 D12 改掉：camoufox 的方法就叫 ``press``，
        两代名字并存等于给「同一个动作两个词」留位置。
        """
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


class DriverSessionError(DriverError):
    """**执行层故障**——坏的是我们的浏览器会话/页面，**不是目标站点**。

    与 :class:`DriverError` 的分界在**归属**：``DriverError`` 说
    「这一次导航没成」（可能是站点慢、站点挂了、DNS 不通）；
    本异常说「**坏的是我们这一侧**」——
    手里那一页被上游销毁了、浏览器崩了、卡死被回收、
    **连一张新页都开不出来**、或者上游**明说**「会话过期、重来即可」
    ⇒ **与目标站点无关**。

    ⚠️ **为什么必须分开**（2026-10-27 实测）：``unreachable_hard`` 的
    定义是「**真·站点不可达**」（DNS 解析不出来 / 连接被拒）——那是
    **站点**的传输层事实。把「我们的页被上游销毁了」也记成这一支，
    等于把**采集侧的故障**写成**站点的性质** ⇒ 产出**理由通顺的
    假负样本**：那一批 10 条里有一个候选连炸 9 次，9 条全进了负样本池，
    而那个站点**一次都没被试过**。

    ⇒ 分支口径：执行层故障**不记** ``unreachable_hard``，
    记「没判出来」那一支（不进池）——**采集失败不是站点结论**。
    """


class DriverUnreachableError(DriverError):
    """**真·站点不可达**——有**机器码**证明站点本身打不开。

    与 :class:`DriverSessionError` 正好是一对：那一个说「坏的是我们
    这一侧」，这一个说「**坏的是站点那一侧，而且机器码说了算**」。

    ⚠️ **只有这一类可以记 ``unreachable_hard``**（M-44 裁定，
    owner 2026-10-27）。其余一切导航失败——超时 / 打断 / abort /
    空响应 / 建页失败 / 会话失效 / 快照失败——都只是「**这次没打开**」，
    记 ``unresolved``（**不进负样本池**）：它们**证明不了站点不可达**，
    而池里那条断言是「**这里真的看不了**」。

    :param code: 命中的**机器码标签**（如 ``NS_ERROR_UNKNOWN_HOST``）——
        进 evidence，让人核的时候看得出「凭什么判成站点不可达」。
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        #: 机器码标签（见 :mod:`trajectory_pipeline.executor.browser.nav_failure`）。
        self.code = code