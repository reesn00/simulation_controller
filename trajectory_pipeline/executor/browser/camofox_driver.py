"""camofox-browser 驱动的 :class:`PageDriver` 实现——**MCP 工具面**。

2026-10-26 执行层再换一次：camoufox（playwright 直连）暂退，改走
**camofox-browser 自带的 MCP stdio 适配器**（``camofox-browser-mcp``，11 个 tool）。
协议不变，所以 :class:`~trajectory_pipeline.executor.browser.page_driver.PageDriver`
的 15 个成员一个没动——**换的是实现，不是契约**。

## 目标编码：``role`` + ``name``（与 camoufox 面同方言）

模型看到的仍然是 ``{role, name}``，**camofox 的 ``eN`` 句柄只在驱动内部消化**。
这与 camoufox 面的方言相同（``aria_role``），所以 P1/P2/P3 的键名投影、
``target_params`` 的派发、``agent_loop`` 的动作组装——**一个字都不用改**。

## 为什么 ``eN`` 不能进训练数据

``eN`` 是**会话内句柄**（上游每次快照重新编号），与 obscura 的 ``ref`` 同类：
进了训练数据等于让模型学随机数，且样本永远不可回放。所以本驱动**内部维护两张表**：

======================  =========================================  ============
表                      内容                                        用途
======================  =========================================  ============
``_refs``               我们的 ``e1..eN`` → ``(role, name)``        判断点 ③ 回查
``_camo_refs``          我们的 ``eN`` → camofox 的 ``eM``            发动作
======================  =========================================  ============

两张表都**只活在驱动内部**；动作面出去的还是 ``{role, name}``。

## ★ 四条「定位正确性」契约（不是推测，是上游代码读出来的）

上游 ``refToLocator``（server.js）的实现是 ``page.getByRole(role, {name}).nth(nth)``
——**没有 ``exact: true``**。于是 Playwright 的 name 匹配是
**大小写不敏感 + 子串匹配**。这带来四条必须处理的后果：

1. **同名整组必须丢**（与 camoufox 面同一条）——但**理由不同**：
   camoufox 丢它是因为 strict mode 报错；camofox 丢它是因为
   **模型分不清两个同名元素**（训练样本里只记 ``{role, name}``，
   重放时命中哪一个是赌）。⇒ 枚举期就整组丢，不进动作面。

2. **「名字是别人的子串」必须丢**（camofox 面**特有**的一档）。
   上游给每个元素记的 ``nth`` 是**同名计数**（精确名），
   而 ``getByRole(role, {name})`` 命中的是**含该子串**的所有元素。
   两者对不上时，上游的 ``.nth(nth)`` 会落到**另一个元素**上——
   **点错，而且报告上看不出任何异常**。
   ⇒ 本驱动在枚举期加一档 ``name_ambiguous``：
   **若同 role 的更早节点的名字包含本名**，则本节点不可寻址，剔掉。
   判据用 **YAML 序**（与上游自己编号的口径一致）。

3. **没有上游句柄的节点不可寻址**（camofox 面**特有**的一档）。
   上游对一部分节点不分配 ``eN``（名字命中 ``SKIP_PATTERNS``（datepicker /
   calendar / date）、节点数超过 ``MAX_SNAPSHOT_NODES``=500、或该行不在
   上游的 ``INTERACTIVE_ROLES`` 里）⇒ 那些节点**点不到**，
   留在动作面里就是给模型一个假的可点目标。⇒ 记 ``unref_dropped``，剔掉。

4. **★ 上面的「同名整组丢」同时兜住了上游自己的一个坑**（读 server.js 读出来的）：
   上游的**注记**（annotation）与它的**句柄表**（``buildRefs``）**统计口径不同**——
   注记按**整份 YAML**（主框架 + iframe）全局计数 ``role:name``，
   而句柄表对 iframe 用**每帧独立**的计数。于是
   「主框架有个 ``button "播放"``、某个 iframe 里也有一个」时，
   注记会把**主框架那一行**标上 **iframe 的句柄** ⇒ 点它会点进 iframe。
   ⇒ 但这种情形下 **YAML 里 ``(role, name)`` 出现两次** ⇒ 被第 1 条整组丢掉
   ⇒ **永远进不了动作面**。
   ⇒ **推论（不可改）**：**只要「同名整组丢」这条还在，
   活到动作面的 ``(role, name)`` 就是整份 YAML 里唯一的** ⇒ 上游的 ``nth``
   在所有口径下都是 0 ⇒ 注记一定正确。
   **把这条规则去掉，注记就会开始指错元素。**

## 快照是**分页**的

上游的快照是 80k 字符的窗口（超了给 ``hasMore`` / ``nextOffset``），
而且**每一页都重复同一段 5k 字符的尾部**。本驱动把**所有页读完再合并**
（:meth:`_read_annotated_nodes`），因为：

- 只读第一页 ⇒ 元素清单**缺一截** ⇒ 模型看不到页面上的大部分可点目标；
- 只读一半就过滤 ⇒ 「真同名」是在**半页**上算的 ⇒ 页面上另一半的同名元素
  会被漏判成「唯一」⇒ **点错**。

⚠️ **分页期间不许动作**：上游在 ``offset>0`` 时**复用缓存的快照**
（不刷新 ref），所以一次分页读取内部是自洽的；但**下一次** ``offset=0``
的读取会**重新编号** ref ⇒ 「读完再动作」安全，「读一半就动作」不是。
本驱动在分页前后各读一次**文档代号**，变了就记 ``nav_unsettled``。

## ``press`` 的口径（A2：驱动缓存合并，**不改上游**）

上游没有「按键」这个 tool——``camofox_type`` 带 ``pressEnter=true``
是**唯一**能提交表单的路径。所以本驱动：

- **缓存上一次 ``fill`` 的 ``(role, name, text)``**；
- ``press("Enter")`` ⇒ **重发一次同文本的 ``type`` 并带 ``pressEnter=true``**。

⚠️ **重发是安全的**：上游的 ``type`` 默认 ``mode='fill'`` ⇒
``locator.fill(text)`` 是**设值**（先清空再写），**不是追加**。
⇒ 重发不会把文本写两遍。

⚠️ ``agent_loop`` 派发 ``press`` 时**不带目标**（``driver.press(key)``），
所以「回车提交」这条最常见的路径**正好落在**缓存合并的口径上。

其它按键 / 页面级按键 ⇒ **记能力降级**（``press_unsupported``），
**不是**「元素不存在」——两者的正确处置相反（一个是能力不可用，
一个是页面事实），混起来就是一条理由通顺的假负样本。

## 已知的上游成本（**不许改上游**，只能记账）

上游 ``camofox_snapshot`` 的请求**硬编码** ``includeScreenshot=true``，
而 MCP tool 的入参里没有这个开关 ⇒ **每次快照都有一张 base64 PNG 穿过 stdio**，
然后被传输层丢弃（只取文本块）。这是**已知成本**，不是 bug；
要消掉它得改上游（owner 裁定：许）。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from typing import Any, Mapping

from trajectory_pipeline.common.elements import ELEMENT_VOCAB_ARIA_ROLE
from trajectory_pipeline.executor.browser.aria_snapshot import parse_nodes
from trajectory_pipeline.executor.browser.camofox_session import (
    CamofoxSession,
    CamofoxToolError,
)
from trajectory_pipeline.executor.browser.element_basis import (
    ROLE_VOCAB,
    SHARED_DOM_JS,
    ladder_stages,
    parse_shared_basis,
)
from trajectory_pipeline.executor.browser.nav_failure import site_unreachable
from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    DriverSessionError,
    DriverUnreachableError,
)
from trajectory_pipeline.executor.dom import (
    IFRAME_SELECTOR,
    MEDIA_SELECTOR,
    TEXT_CONTENT_EXPR,
    TRUNCATION_RATIO,
    CleanedLink,
    CleanedSnapshot,
    clean_body,
    is_mojibake,
    parse_body_text,
)
from trajectory_pipeline.perception.base import (
    InteractiveElement,
    LinkItem,
    Observation,
)

__all__ = [
    "CAMOFOX_FACE_STAGES",
    "CamofoxDriver",
    "DEGRADED_PRESS_UNSUPPORTED",
    "DROP_REASONS",
    "ROLE_VOCAB",
]

#: 元素列表的默认上限。与 obscura / camoufox 同一个数（200），
#: 免得三面的 ``elements_total`` 因为上限不同而不可比。
DEFAULT_LIMIT = 200

#: 观察**读到一半**时的重来次数（与 camoufox 面同口径）。
OBSERVE_RETRY = 2

#: 「读到一半」记进 ``degraded`` 的名字（能力名，与 ``links`` / ``interactive`` 同族）。
DEGRADED_NAV_UNSETTLED = "nav_unsettled"

#: 「按键做不到」记进 ``degraded`` 的名字。
#:
#: ⚠️ 它**不是**「元素不存在」：前者是**能力不可用**（本层没有按键这个动作），
#: 后者是**页面事实**。混起来会让「这层没有这个能力」被读成「页面上没这个元素」。
DEGRADED_PRESS_UNSUPPORTED = "press_unsupported"

#: camofox 面**特有**的两档过滤（不属于共享的两把尺子）。
#:
#: ⚠️ 它们**只在这一面**成立：obscura 面没有上游句柄这回事，
#: camoufox 面的定位是 ``get_by_role(..., exact=True)``，也没有子串歧义。
#: ⇒ 共享阶梯（:func:`element_basis.ladder_stages`）一个字都没改，
#: 这两档是本面在共享阶梯**之后**自己加的。
CAMOFOX_FACE_STAGES: tuple[str, ...] = ("unref_dropped", "name_ambiguous")

#: 元素被本面剔掉的**原因**（供 ``resolve`` 区分「页面上没有」与
#: 「页面上有、但本面不可寻址」）。
#:
#: ⚠️ 这个区分是**归因**用的：把「本面设计上的取舍」记成
#: 「名字脆」，会让可移植性的读数指错对象。
DROP_REASONS: tuple[str, ...] = (
    "role",             # role 不在词表内
    "nameless",         # 没有可访问名
    "ambiguous",        # 真同名整组
    "unref",            # 上游没给句柄
    "name_ambiguous",   # 名字是更早同 role 名字的子串
)

#: 「页面没有 ``performance.timeOrigin``」时代号取这个值（恒定 ⇒ 永不误判）。
DOC_STAMP_NONE = "none"

#: 取**文档代号**的表达式：``performance.timeOrigin``。
#:
#: **每个文档一个常量**（同文档内恒定、跨文档必变），且是**原生属性**——
#: 页面上多不出任何痕迹。⇒ 它是「读到一半」的判据（与 camoufox 面同一条）。
#:
#: ⚠️⚠️ **它必须是「求得出值的表达式」，不能是函数字面量**
#: （2026-10-27 踩到，症状是整批 0 候选）：
#: 上游把这段文本交给 Playwright 的 ``page.evaluate(expression)``
#: （上游 ``server.js:5511``），而 **Playwright 收到字符串时按
#: 「表达式」求值**——``() => …`` 求出来是**函数对象**，序列化后是
#: ``undefined`` ⇒ 响应里**没有** ``result`` 键 ⇒ 本层读到的代号是
#: **空串** ⇒ 每次观察都被判成「读到一半」⇒ ``degraded=['nav_unsettled']``
#: ⇒ **环节① 直接返回 0 候选**（10 条任务全在这一步夭折，零报错）。
#:
#: ⇒ 这段文本写成**立即调用的 IIFE**，与本文件其余 JS（``_PAGE_TEXT_JS`` /
#: ``_LINKS_JS`` / ``SHARED_DOM_JS``）**同一种形态**。
#: 契约测试 ``test_文档代号表达式求得出值`` 盯着这件事。
DOC_STAMP_EXPR = (
    "(function(){return (window.performance && performance.timeOrigin != null)"
    f" ? String(performance.timeOrigin) : '{DOC_STAMP_NONE}';}})()"
)

#: 动作之后等新文档 **commit** 的上限（毫秒）。有界是重点：
#: 等不到就照实记 ``last_action["nav"]="same"``，**不猜**。
NAV_COMMIT_TIMEOUT_MS = 2000

#: 动作之后等 **DOM ready** 的上限（毫秒）。
NAV_SETTLE_TIMEOUT_MS = 2000

#: 等 commit 时的轮询间隔（秒）。每次轮询是一次浏览器往返，间隔不宜太小。
NAV_COMMIT_POLL_S = 0.1

#: 上游分页窗口的截断标记（行首）。**只在它之后的内容是「尾部」**。
_PAGINATION_MARKER = "[... truncated at char "

#: 取页面元信息（URL / 标题 / 正文）的 JS。**一次往返读全**——
#: 分多次读会跨时刻，而正文与标题要配对。
_PAGE_TEXT_JS = (
    "(function(){var b=document.body;"
    "return JSON.stringify({"
    "href:String(location.href||''),"
    "title:String(document.title||''),"
    "innerText:b?(b.innerText||''):''"
    "});})()"
)

#: 取整页 HTML 的 JS（``innerText`` / ``textContent`` 都空时的兜底）。
_HTML_JS = "document.documentElement ? (document.documentElement.outerHTML || '') : ''"

#: ``document.readyState`` 的取值——``"loading"`` 意味着文档还在解析。
_READY_STATE_JS = "String(document.readyState || '')"


def _tab_is_gone(exc: BaseException) -> bool:
    """这个失败是不是「**这一页已经不在上游了**」（该换一张新页）？

    判据是**上游自己给的结构**（``recovery`` / ``code`` / 状态码），
    不是我们猜的英文短语——猜错的后果是「把一次普通的站点超时
    当成会话失效，换页重来」，而重来会**多打站点一次**。

    ⚠️ **不是** ``CamofoxToolError`` 的一律判 False：
    别的驱动（obscura）与别的失败形态都不该走这条路。
    """
    return isinstance(exc, CamofoxToolError) and exc.tab_is_gone


def _execution_fault(exc: BaseException) -> bool:
    """这个失败是不是**上游明说的执行层故障**（不是「站点打不开」）？

    与 :func:`_tab_is_gone` 同一纪律：判据是**上游自己给的结构**
    （``recovery`` / ``code``），不是我们猜的英文短语。

    上游的 ``recovery="retry"`` 一族（``session_expired`` /
    ``concurrency_timeout`` / ``browser_launch_timeout``）说的是
    「**我们这边坏了**，重来即可」——那个站点**一次都没被试过**
    ⇒ 不能记 ``unreachable_hard``。

    ⚠️ **不是** ``CamofoxToolError`` 的一律判 False：
    别的驱动（obscura）与别的失败形态都不该走这条路。
    """
    return isinstance(exc, CamofoxToolError) and exc.execution_fault


class CamofoxDriver:
    """camofox-browser 的 :class:`PageDriver` 实现（MCP 工具面）。"""

    name = "camofox"

    def __init__(self, session: CamofoxSession, *,
                 collect_element_basis: bool = False) -> None:
        self._session = session
        #: **跨面可比的元素账目要不要采**（设计方案 04 §4.9.6）。**默认关**——
        #: 它多一次 evaluate 往返，是**测量仪器**不是生产链必需项。
        self._collect_element_basis = collect_element_basis
        #: 我们的 ``e1..eN`` → ``(role, name)``。**只在采集面内部活着**：
        #: 判断点 ③ 回传 ref，代码按它取回目标；动作面另走 ``role+name``。
        self._refs: dict[str, tuple[str, str]] = {}
        #: 我们的 ``eN`` → camofox 的 ``eM``。**发动作时用**——
        #: 训练数据里出去的还是 ``{role, name}``。
        self._camo_refs: dict[str, str] = {}
        #: ``(role, name)`` → 我们的 ``eN``。动作时按语义目标查它。
        self._target_index: dict[tuple[str, str], str] = {}
        #: 页面上的 ``(role, name)`` → **被本面剔掉的原因**。
        #:
        #: ⚠️ **它让「页面上没有」与「页面上有但本面不可寻址」分得开**——
        #: 两者的正确处置相反（一个是事实，一个是本面设计上的取舍），
        #: 混起来会让可移植性的读数指错对象。
        self._dropped: dict[tuple[str, str], str] = {}
        #: 上一次 :meth:`interactive` 的过滤账目。**空列表必须能自证**——
        #: 「页面上真没有元素」与「全被过滤了」在观察里同形，只有这里分得开。
        self.last_enumeration: dict[str, int] = {}
        #: **分页读取跨了文档**（:meth:`_read_annotated_nodes` 读的）。
        #:
        #: ⚠️ **它必须单独存着**：``_read_annotated_nodes`` 跑在
        #: :meth:`interactive` **之前**，而 ``interactive`` 会**整份覆盖**
        #: ``last_enumeration``——直接往 ``last_enumeration`` 里写
        #: 会被立刻抹掉，于是「这份清单横跨两个文档」这件事**静默消失**。
        #: 症状是动作面拿着一份不属于任何页面的清单去点 ⇒ 点错，
        #: 而报告上看不出异常。
        self._nav_unsettled: bool = False
        #: 上一次动作的结果账目（``op`` / ``role`` / ``name`` / ``outcome``）。
        self.last_action: dict[str, str] = {}
        #: **换页自愈账目**（2026-10-27）：每一次「上游把当前页销毁了，
        #: 驱动换一张新页重试」都记一条。
        #:
        #: ⚠️ **它必须记**：自愈之后那一步是**成功**的，存档里看不出
        #: 「这一页死过一次」——而「这一页死过」正是读档人判断
        #: 「这个结论可不可信」的依据（页面被换掉 ⇒ 观察来自另一页）。
        #: 不记就是**静默**。
        self._recoveries: list[dict[str, Any]] = []
        #: 上一次 ``new_tab`` / ``close_tab`` 的账目。
        self.last_tab: dict[str, Any] = {}
        #: **上一次 ``fill`` 的缓存**——``press`` 的合并依据（A2）。
        #:
        #: ⚠️ **为什么缓存**：上游没有「按键」这个动作，回车提交只能靠
        #: ``camofox_type(..., pressEnter=true)``。而那条要**重发文本**——
        #: 没有缓存就不知道该重发什么。
        #:
        #: ⚠️ **只在同一个目标上复用**：``press`` 带了别的 ``role``/``name``
        #: 时缓存**不适用** ⇒ 记能力降级，不猜。
        self._fill_cache: dict[str, Any] | None = None
        #: 本驱动发出去的 tab 句柄 → camofox 的 ``tabId``。
        #: **两套 id 不可互换**（同 camoufox 的纪律）。
        self._tabs: dict[str, str] = {}
        self._tab_seq = 0

    # ── 页级状态的失效 ────────────────────────────────────────────

    def _reset_page_state(self) -> None:
        """**换页即作废上一页的一切页级状态**。

        这五样（ref 表 / 上游句柄表 / 目标索引 / 剔掉清单 / 填值缓存）
        全都**只对当前页有效**——上游的 ``eN`` 是**每次快照重新编号**的，
        导航之后旧编号指向的是**另一个元素**：点错不可逆，而且看起来完全正常。

        ⚠️ **``last_action`` 也是页级状态**（2026-10-27 补进这一组）：
        它记的是「**上一页**上那一次动作的结果」，而 :meth:`observe` 拿它
        判「这一页有没有降级」——**不一起作废就是拿上一页的账记这一页的头**。

        踩过的坑（真实批次读出来的）：一次**做不到的按键**（比如按 ``F5``）
        记了 ``last_action["outcome"]="unsupported"`` ⇒ 之后**每一次导航**之后
        的观察都挂上了 ``press_unsupported`` ⇒ **18% 的观察带着一个假降级**，
        而这个假降级**直接进了 agent 的输入**（``agent_loop._render_observation``
        把 ``degraded`` 投影给模型）⇒ agent 以为「这一页没读全」。

        ⇒ **这一组状态只能一起清**：漏掉一样就是「上一页的账记到这一页头上」
        （同一族错误，:meth:`observe` 里对 ``last_enumeration`` 已经防过一次）。
        """
        self._refs = {}
        self._camo_refs = {}
        self._target_index = {}
        self._dropped = {}
        self._fill_cache = None
        self.last_action = {}

    # ── 读数 ──────────────────────────────────────────────────────

    @property
    def fingerprint(self) -> dict[str, Any]:
        """本 Run 的指纹读数（批次元数据用）。**空 dict = 没测到**。"""
        return self._session.fingerprint

    @property
    def refs(self) -> Mapping[str, tuple[str, str]]:
        """``ref`` → ``(role, name)`` 的只读快照（**不进动作流**）。"""
        return dict(self._refs)

    @property
    def _tab(self) -> str:
        """当前页的 camofox ``tabId``。栈空 ⇒ 抛（**不猜一个空的**）。"""
        return self._session.current

    @property
    def recoveries(self) -> list[dict[str, Any]]:
        """**换页自愈账目**（本 run 发生过几次、每次是为了哪个 URL）。

        ⚠️ **空列表 ≠ 「没发生过」，是「没记到」**——与指纹同一口径。
        它进存档的 run 级 ``warnings``（由 orchestrator 抄），
        因为自愈之后那一步在动作流里是**成功**的，不记就看不出
        「这一页死过一次」。
        """
        return [dict(item) for item in self._recoveries]

    # ── 原子原语 ──────────────────────────────────────────────────

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        """导航。失败抛 :class:`DriverError`——调用方须 fail-closed。

        **导航即清空 ref 表**：上游的 ``eN`` 是**每次快照重新编号**的，
        导航之后旧编号指向的是**另一个元素**——点错不可逆，
        而且看起来完全正常。

        ⚠️ **建页与导航是两次调用**（2026-10-27 改）：上游的建页路由把
        两件事塞进**同一个 30 秒预算** ⇒ 那个路由失败**证明不了站点
        打不开** ⇒ 拆成「建空白页 + 导航」之后，失败落在**哪个路由**
        就是归属判据。详见 :meth:`_open_blank_and_navigate`。

        ⚠️ **上游把当前页销毁了 ⇒ 换一张新页重来一次**（2026-10-27）：
        详见 :meth:`_navigate`。
        """
        self._reset_page_state()
        try:
            await self._navigate(url)
        except DriverSessionError:
            # ⚠️ **会话失效不许被重新包成普通 DriverError**——上层要靠
            # 这个类型把「执行层坏了」与「站点打不开」分开。
            raise
        except DriverError as exc:
            # ⚠️ **真·站点不可达要单独一个类型**（M-44）：只有**机器码
            # 证明**站点本身打不开（DNS 解析不出来 / 连接被拒）才配记
            # ``unreachable_hard``；超时 / 打断 / abort / 空响应这些
            # 「这次没打开」的形态**证明不了**站点不可达 ⇒ 上层要能把
            # 两者分开。判据见 ``nav_failure.py``（码表建在实测原文上）。
            hit = site_unreachable(str(exc))
            if hit is not None:
                raise DriverUnreachableError(
                    f"camofox 导航失败: {exc}", code=hit.code
                ) from None
            raise DriverError(f"camofox 导航失败: {exc}") from None
        # 指纹读一次就够：身份在浏览器起来那一刻就定了。
        # 读不到**不抛**——它是**批次元数据**，不是采集内容；
        # 失败的原因记进 ``session.readings``，存档里那个字段会是空 dict
        # （= 没测到，不是「测出来是空的」）。
        await self._session.measure_fingerprint()

    async def _navigate(self, url: str) -> None:
        """导航到 ``url``。**上游把当前页销毁了 ⇒ 换一张新页重来一次**。

        ⚠️ **建页与导航拆成两次调用**（2026-10-27 实测后改）：新建页一律
        走 :meth:`_open_blank_and_navigate`（**先建空白页再导航**），
        不再走「建页带 URL」的合并路由。理由是**归属**：
        合并路由的失败**证明不了站点打不开**（详见
        :meth:`_open_blank_and_navigate` 的说明）。

        ⚠️ **为什么必须有这一层自愈**（2026-10-27 实测，10 条 camofox 批）：
        上游 camofox-browser 会在**操作超时 / 连续超时 / 页崩溃 / 锁队列超时**
        时**销毁那一页**（上游 ``server.js`` 的 ``destroyTab`` /
        ``destroyTimedOutTab``），而**销毁不通知客户端** ⇒ 客户端手里的
        ``tabId`` 成了死引用 ⇒ 再导航就是
        ``404 {"error":"Tab not found"}``，**每一次都 404**。

        实测症状：一个候选**连炸 9 次**，9 次全被记成 ``unreachable_hard``
        （"站点打不开"）⇒ **九条理由通顺的假负样本进了池**，
        而那个站点**一次都没被试过**。

        ⇒ 这里在上游**明说「这一页没了」**（``recovery="create_new_tab"``
        / 404 / 410）时，**摘掉死页 + 开一张新页 + 同一个 URL 重来一次**。

        ⚠️ **为什么只有「导航」能自愈、动作不能**：动作（点击/填值）的目标
        是**页级 ref**，换页之后 ref 空间整个变了 ⇒ 重放动作会点错元素。
        导航能重来，是因为**目标由 URL 决定**，不依赖页内状态。

        ⚠️ **重来只做一次**：重来之后还失败就是真失败——不做无限重试，
        免得把「站点打不开」拖成一个没有尽头的循环（预算在更上层）。
        """
        if not self._session.pages:
            await self._open_blank_and_navigate(url)
            return
        tab = self._tab
        try:
            await self._navigate_on(tab, url)
        except DriverSessionError:
            # 执行层故障（上游明说「重来就好」）：**不是**「页没了」，
            # 换页也没用——换页只会把站点再打一次。
            raise
        except DriverError as exc:
            if not _tab_is_gone(exc):
                raise
            # 上游说这一页没了 ⇒ 我们手里的 tabId 已经是死引用。
            # 摘掉它（**不通知上游**——那一页已经不在了），换新页重来。
            self._session.discard_page(tab)
            self._recoveries.append(
                {"url": url, "tab": tab, "reason": str(exc)[:200]}
            )
            try:
                await self._open_blank_and_navigate(url)
            except DriverSessionError as retry_exc:
                # 换页重试也没成功 ⇒ 会话真的死了。
                # ⇒ 抛**执行层故障**，不是「站点打不开」——
                # 归属不同，处置就不同（见 DriverSessionError）。
                raise DriverSessionError(
                    f"camofox 会话失效（换页重试也没成功）: {retry_exc}"
                ) from None
            # ⚠️ **站点事实不许在这里被降级**：「换页成功、导航失败」
            # 是**在一张干净的新页上**得到的传输层事实 ⇒ 仍然是
            # 站点打不开（``unreachable_hard``）。
            # 只有「换页本身失败」才是采集侧的故障。

    async def _navigate_on(self, tab: str, url: str) -> None:
        """在 ``tab`` 上导航，并把上游的失败**翻译成我们的归属**。

        ⚠️ **为什么这里要做翻译**：上游的 ``recovery="retry"`` 一族
        （``session_expired`` / ``concurrency_timeout`` /
        ``browser_launch_timeout``）说的是「**我们这边坏了**，重来即可」
        ——那个站点**一次都没被试过** ⇒ 抛
        :class:`DriverSessionError`（⇒ 上层记 ``unresolved``，
        **不进负样本池**），而不是普通 :class:`DriverError`
        （那是「站点打不开」）。

        ⚠️ **这里不许做别的事**：``tab_is_gone``（这一页没了）由调用方
        判——它要换页重来，不是抛错。
        """
        try:
            await self._session.navigate(tab, url)
        except DriverError as exc:
            if _execution_fault(exc):
                raise DriverSessionError(
                    f"camofox 执行层故障（不是站点打不开）: {exc}"
                ) from None
            raise

    async def _open_blank_and_navigate(self, url: str) -> None:
        """**先建一张空白页，再导航过去**——建页与导航**必须是两次调用**。

        ⚠️ **为什么必须拆开**（2026-10-27 实测，方案 21 §R5 的处置）：
        上游的 ``POST /tabs``（``camofox_create_tab``）把「建页」与「导航」
        塞进**同一个 30 秒预算**（上游 ``server.js`` 的
        ``withTimeout(..., tabCreateRequestTimeoutMs(), 'tab create')``）
        ⇒ **那个路由失败证明不了站点打不开**——也可能是建页本身卡住了。

        实测两种失败**在错误体里同形**：都是 HTTP 500 +
        ``{"error":…, "retryable":false}``、**没有** ``code`` /
        ``recovery`` ⇒ 光看错误体**分不开**。
        （这正是 T048 那条假负样本的成因：``tab create timed out after
        30000ms`` 被记成 ``unreachable_hard``，而同一批的 T038
        **打开了同一个 iqiyi URL** ⇒ 站点是通的。）

        ⇒ 唯一能分开的是**走哪个路由**。上游的建页路由在 ``url`` 为空时
        **只建页不导航**（上游 ``server.js`` 的 ``if (url)``），
        而导航路由（``camofox_navigate``）**只做导航**
        ⇒ 拆成两次调用之后：

        - **建页**失败 ⇒ 那一页里**根本没有站点 URL**
          ⇒ 站点不可能是原因 ⇒ **执行层故障**
          ⇒ :class:`DriverSessionError` ⇒ 上层记 ``unresolved``
          （**不进负样本池**）；
        - **导航**失败 ⇒ 才是**站点层**的事实 ⇒ 但**只有机器码证明**
          站点不可达（DNS 解析不出来 / 连接被拒）才配记
          ``unreachable_hard``；超时 / 打断 / 证书等形态**证明不了**
          ⇒ 普通 :class:`DriverError` ⇒ ``unresolved``
          （M-44 裁定，见 ``nav_failure.py``）。

        ⚠️ **多一次往返，换的是「归因」**：一次建页往返实测 ~1.4 秒，
        而它换来的是「这个站点到底打不打得开」这个事实**站得住**。
        """
        try:
            tab = await self._session.new_page("")
        except DriverError as exc:
            raise DriverSessionError(
                f"camofox 建页失败（执行层，不是站点打不开）: {exc}"
            ) from None
        try:
            await self._navigate_on(tab, url)
        except DriverError as exc:
            if _tab_is_gone(exc):
                # **刚建出来的新页就没了** ⇒ 上游在拆我们的页
                # ⇒ 执行层故障，不是站点打不开。
                raise DriverSessionError(
                    f"camofox 会话失效（刚建的新页就没了）: {exc}"
                ) from None
            raise

    async def snapshot(
        self, *, max_chars: int | None = None, degraded: list[str] | None = None
    ) -> CleanedSnapshot:
        """取快照：URL / Title / 正文三级取（innerText → textContent → HTML）。

        三级不是冗余，是 obscura 侧实测出来的同一张降级表
        （见 :mod:`trajectory_pipeline.executor.dom`）：

        1. ``innerText``  —— 渲染后的可见文本，是唯一可信的正文来源；
        2. ``textContent`` —— innerText 因 layout 计算失败返回空时的救济；
        3. HTML          —— 前两者都空时经 :func:`clean_body` 剥标签兜底，
           ``body_source`` 会标成 ``"snapshot"`` 以示证据更弱。

        ⚠️ **这里刻意不调 ``camofox_snapshot``**：那个 tool 会**刷新上游的 ref 表**，
        而本驱动只在 :meth:`interactive` 里读它——**一处刷新、一处编号**，
        避免「读完快照、ref 已经变了」的错位。
        """
        payload = await self._eval_json(_PAGE_TEXT_JS)
        url = str(payload.get("href") or "")
        title = str(payload.get("title") or "")
        raw = str(payload.get("innerText") or "")

        body: str | None = None
        body_source = "inner_text"
        raw_len = 0
        stripped_ratio = 1.0

        if not raw.strip():
            try:
                raw = await self._eval_text(TEXT_CONTENT_EXPR)
            except DriverError:
                raw = ""
        if raw:
            raw_len = len(raw)
            body = parse_body_text(raw, max_chars=max_chars)
        if not body:
            try:
                html = await self._eval_text(_HTML_JS)
            except DriverError:
                html = ""
            if html:
                cleaned = clean_body(html)
                raw_len = len(html)
                stripped_ratio = (len(cleaned) / raw_len) if raw_len else 1.0
                body_source = "snapshot"
                body = (cleaned[:max_chars]
                        if max_chars is not None and len(cleaned) > max_chars
                        else cleaned)
            if not body and degraded is not None:
                degraded.append("body_text")
        if (body and degraded is not None and body_source == "inner_text"
                and is_mojibake(body)):
            degraded.append("body_mojibake")
        if degraded is not None and title and is_mojibake(title):
            degraded.append("title_mojibake")

        cleaned_body = body or ""
        if body_source == "snapshot":
            truncated = (max_chars is not None
                         and raw_len >= max_chars * TRUNCATION_RATIO)
        else:
            truncated = max_chars is not None and len(cleaned_body) >= max_chars
        return CleanedSnapshot(
            url=url,
            title=title or "",
            body=cleaned_body,
            raw_len=raw_len,
            stripped_ratio=stripped_ratio,
            truncated=truncated,
            max_chars=max_chars,
            body_source=body_source,
        )

    async def interactive(
        self, *, limit: int = DEFAULT_LIMIT
    ) -> tuple[InteractiveElement, ...]:
        """枚举可交互元素：**读上游的注记快照**（带 ``[eN]`` 句柄）。

        过滤顺序（每一步的丢弃数都进 :attr:`last_enumeration`）：
        role 词表 → 无名 → 真同名整组 → **没有上游句柄** → **名字子串歧义**
        → 截断到 ``limit``。后两档是 camofox 面**特有**的
        （见模块 docstring 的「四条定位正确性契约」）。

        我们的 ``e1..eN`` 按**留下的顺序**编号——与 camoufox 面同一条规矩，
        所以上层的 ``refs`` 回查、``last_action`` 渲染**一个字都不用改**。

        ⚠️ **失败抛** :class:`DriverError`：空列表意味着「页面上没有可点目标」，
        会把一次**能力故障**写成**页面事实**。
        """
        nodes = await self._read_annotated_nodes()
        pairs = [(n.role, n.name or "") for n in nodes]
        unique, ledger = ladder_stages(pairs, role_vocab=ROLE_VOCAB)
        unique_set = set(unique)

        # 逐节点判定：① 在共享阶梯的「唯一」集合里吗
        #             ② 有上游句柄吗
        #             ③ 名字会被更早的同 role 节点「子串命中」吗
        seen_by_role: dict[str, list[str]] = {}
        addressable: list[tuple[str, str, str]] = []   # (role, name, camo_ref)
        dropped: dict[tuple[str, str], str] = {}
        unref_dropped = 0
        name_ambiguous = 0
        annotated = 0
        for node in nodes:
            role = node.role
            name = node.name or ""
            key = (role, name)
            if node.ref:
                annotated += 1
            if not name:
                # 无可访问名 ⇒ 定位不到（与「元素不存在」同形）。不列。
                seen_by_role.setdefault(role, []).append(name)
                continue
            if role not in ROLE_VOCAB:
                dropped.setdefault(key, "role")
                seen_by_role.setdefault(role, []).append(name)
                continue
            if key not in unique_set:
                # 真同名整组——两个都不进动作面。
                dropped.setdefault(key, "ambiguous")
                seen_by_role.setdefault(role, []).append(name)
                continue
            # ③ 子串歧义：更早的同 role 节点里，有名字**包含**本名的吗？
            #    有 ⇒ 上游的 ``getByRole(role, {name})`` 会先命中它，
            #    而上游的 ``.nth()`` 按**精确同名**计数 ⇒ 落错元素。
            lower = name.lower()
            if any(lower in prev.lower() for prev in seen_by_role.get(role, ())):
                name_ambiguous += 1
                dropped.setdefault(key, "name_ambiguous")
                seen_by_role.setdefault(role, []).append(name)
                continue
            # ② 上游句柄：没有就点不到 ⇒ 剔掉（不是「页面没有」）。
            if not node.ref:
                unref_dropped += 1
                dropped.setdefault(key, "unref")
                seen_by_role.setdefault(role, []).append(name)
                continue
            addressable.append((role, name, node.ref))
            seen_by_role.setdefault(role, []).append(name)

        cap = max(int(limit), 0)
        kept = addressable[:cap]

        refs: dict[str, tuple[str, str]] = {}
        camo_refs: dict[str, str] = {}
        target_index: dict[tuple[str, str], str] = {}
        elements: list[InteractiveElement] = []
        for index, (role, name, camo_ref) in enumerate(kept, start=1):
            ref = f"e{index}"
            refs[ref] = (role, name)
            camo_refs[ref] = camo_ref
            target_index[(role, name)] = ref
            elements.append(InteractiveElement(ref=ref, role=role, name=name))

        self._refs = refs
        self._camo_refs = camo_refs
        self._target_index = target_index
        self._dropped = dropped
        self.last_enumeration = {
            "nodes": len(nodes),
            "annotated": annotated,
            **ledger,
            "unref_dropped": unref_dropped,
            "name_ambiguous": name_ambiguous,
            "kept": len(kept),
            "truncated": len(addressable) - len(kept),
        }
        if self._nav_unsettled:
            # 分页横跨两个文档 ⇒ 这份清单**不是任何一个页面的**清单。
            # 记进账目（不是 ``degraded``）：它不说明页面不可信，
            # 只说明**这份元素表**不可信——两者的处置不同。
            self.last_enumeration["nav_unsettled"] = 1
        return tuple(elements)

    async def element_basis(self) -> dict[str, int]:
        """**跨面可比的**元素枚举账目。

        跑的是 :data:`element_basis.SHARED_DOM_JS`——**与 obscura 面同一段
        JS、同一把尺子**，于是两面的数字**按构造可比**。

        ⚠️ **失败抛** :class:`DriverError`：空账会被读成
        「这个页面上一个交互元素都没有」，而真相是「没测到」。
        """
        raw = await self._eval_text(SHARED_DOM_JS)
        try:
            _, ledger = parse_shared_basis(raw)
        except ValueError as exc:
            raise DriverError(f"camofox 共享口径返回不合法: {exc}") from None
        return ledger

    async def _collect_basis(
        self, degraded: list[str], face_ledger: Mapping[str, int]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """采两本元素账目。返回 ``(两本账, 没测到的原因)``。

        ⚠️ **测量失败不进 ``degraded``**：``degraded`` 是**内容块**的降级通道，
        下游见到降级就**不取候选**，于是「尺子坏了」会**作废整批数据**。
        失败只记进 ``element_basis_error``，**face 账目照旧给**。
        """
        if not self._collect_element_basis:
            return None, None
        try:
            shared = await self.element_basis()
        except DriverError as exc:
            return {"face": dict(face_ledger)}, str(exc)
        return {"shared": shared, "face": dict(face_ledger)}, None

    async def count(self, selector: str) -> int:
        """选择器命中数。

        ⚠️ **失败返回 0**（与 obscura / camoufox 同口径）——这条口径本身有缺陷
        （「能力不可用」被折叠成「页面上没有」），但换口径会让实现不可比。
        """
        expr = (
            "(function(){try{return String("
            "document.querySelectorAll(" + json.dumps(selector) + ").length"
            ");}catch(e){return '0';}})()"
        )
        try:
            value = await self._eval_text(expr)
        except DriverError:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    async def evaluate(self, expression: str) -> Any:
        """执行表达式，**原样返回上游给的值**。

        与 camoufox 面同口径：上游是 playwright 的 ``page.evaluate``，
        直接把 JS 值序列化成 Python 对象，**没有 JSON 解包这一步**。
        表达式返回 ``undefined`` 时拿到 ``None``——与「取不到」同形，
        需要区分时由调用方自己包一层哨兵值。

        失败**抛** :class:`DriverError`（obscura 同位置是吞成 ``None``，
        有意不一致）：吞掉之后调用方无法把「表达式跑挂了」与「值是 null」分开，
        而这两者的正确处置相反。
        """
        try:
            return await self._session.evaluate(self._tab, expression)
        except DriverError as exc:
            raise DriverError(f"camofox 表达式失败: {exc}") from None

    async def close(self) -> None:
        """关闭整次会话（一个 Run 一份）。幂等。"""
        await self._session.close()

    # ── 动作面（D12：role+name 定位）──────────────────────────────

    async def click(
        self,
        *,
        ref: str | None = None,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> None:
        """点一个可交互元素：按 ``role+name`` 查到我们的 ref，再查上游的 ``eM``。

        ⚠️ **定位是「快照时定」的**：``{role, name}`` → 上游 ``eM`` 的映射
        是上一次 :meth:`interactive` 时定的。**动作必须紧跟在同一份观察之后**
        ——否则页面变了，映射可能已经过期（模块 docstring 的「四条定位正确性契约」）。
        """
        target = self._locate("click", role=role, name=name, ref=ref, selector=selector)
        if target is None:
            return
        camo_ref = self._camo_refs[target]
        pre_stamp = await self._doc_stamp()
        await self._do(
            "click",
            lambda: self._session.click(self._tab, camo_ref),
            role=role, name=name,
        )
        await self._settle(pre_stamp)

    async def fill(
        self,
        *,
        text: str,
        ref: str | None = None,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> None:
        """往输入框**设值**：上游的 ``camofox_type``（默认 ``mode='fill'``）。

        与动作流的 ``fill`` 同名同义——``fill`` 就是「设值」，不是追加。
        （上游的 ``mode='keyboard'`` 才是逐字符追加，本驱动**不用**它。）

        ⚠️ **顺带缓存**：``(role, name, text)`` 存下来，供 :meth:`press` 合并用
        （A2）。缓存**只在同一个目标上复用**。
        """
        target = self._locate("fill", role=role, name=name, ref=ref, selector=selector)
        if target is None:
            return
        camo_ref = self._camo_refs[target]
        pre_stamp = await self._doc_stamp()
        await self._do(
            "fill",
            lambda: self._session.type_text(self._tab, camo_ref, text),
            role=role, name=name,
        )
        self._fill_cache = {
            "role": role or "", "name": name or "", "text": text, "ref": target,
        }
        await self._settle(pre_stamp)

    async def press(
        self, key: str, *, role: str | None = None, name: str | None = None
    ) -> None:
        """按键。**本层只支持「在填过的那个输入框上按回车」**（A2）。

        上游没有「按键」这个 tool——``camofox_type(..., pressEnter=true)``
        是唯一能提交表单的路径。所以：

        | 情形 | 处置 |
        |---|---|
        | ``key != "Enter"`` | **能力降级**（``press_unsupported``）——本层做不到 |
        | 目标与上次 ``fill`` 不同 / 从没 ``fill`` 过 | **能力降级**——没有可重发的文本 |
        | 其余 | 重发同文本的 ``type`` + ``pressEnter=true`` |

        ⚠️ **重发是安全的**：上游的 ``type`` 是**设值**（先清空再写），不是追加。

        ⚠️ **降级不是「元素不存在」**：前者是能力不可用，后者是页面事实。
        记在 ``last_action["outcome"]="unsupported"``，
        并在 :meth:`observe` 里落进 ``degraded``。
        """
        if str(key) != "Enter":
            self._degrade_press("press", role, name, f"key:{key}")
            return
        cache = self._fill_cache
        want_role = role if role is not None else str((cache or {}).get("role") or "")
        want_name = name if name is not None else str((cache or {}).get("name") or "")
        if not want_role or not want_name:
            self._degrade_press("press", role, name, "no_target")
            return
        if not cache or cache.get("role") != want_role or cache.get("name") != want_name:
            # 目标变了 ⇒ 缓存里的文本**不适用**。**不猜**——降级。
            self._degrade_press("press", want_role, want_name, "no_cached_text")
            return
        target = self._target_index.get((want_role, want_name))
        if not target or target not in self._camo_refs:
            self._degrade_press("press", want_role, want_name, "not_found")
            return
        camo_ref = self._camo_refs[target]
        pre_stamp = await self._doc_stamp()
        await self._do(
            "press",
            lambda: self._session.type_text(
                self._tab, camo_ref, str(cache.get("text") or ""), press_enter=True
            ),
            role=want_role, name=want_name,
        )
        await self._settle(pre_stamp)

    def _degrade_press(
        self, op: str, role: str | None, name: str | None, reason: str
    ) -> None:
        """记一次**按键能力降级**。不是「元素不存在」。"""
        self.last_action = {
            "op": op, "role": role or "", "name": name or "",
            "outcome": "unsupported", "reason": reason,
        }

    async def resolve(
        self, *, role: str | None, name: str | None
    ) -> dict[str, Any]:
        """**只定位，不动作**——给判据⑥（动作可移植性）的重放探针用。

        返回 ``{"outcome", "matches", "role", "name", "reason"}``，``outcome`` 四态：

        ==================  ==================================================
        outcome             含义
        ==================  ==================================================
        ``ok``              恰好 1 个（本面可寻址）
        ``not_found``       **页面上没有**这个 ``(role, name)``
        ``not_addressable`` **页面上有**，但被**本面的过滤**剔了（附 ``reason``）
        ``ambiguous``       >1 个真同名——**在枚举期就整组丢掉了**，本面**不会**出现
        ==================  ==================================================

        ⚠️ **``not_addressable`` 为什么要单列**：它与 ``not_found`` 的正确处置
        相反——前者是「**本面**的取舍」（元素在，只是我们不寻址它），
        后者是「**页面**的事实」（元素不在）。
        混起来会把「本面设计上的取舍」记成「名字脆」，
        可移植性的读数于是指错了对象。
        """
        target = self._locate("resolve", role=role, name=name)
        if target is None:
            rec = dict(self.last_action)
            outcome = str(rec.get("outcome") or "not_found")
            try:
                matches = int(str(rec.get("matches") or "0"))
            except ValueError:
                matches = 0
            return {
                "outcome": outcome,
                "matches": matches,
                "role": role or "",
                "name": name or "",
                "reason": str(rec.get("reason") or ""),
            }
        self.last_action = {
            "op": "resolve", "role": role or "", "name": name or "",
            "outcome": "ok", "matches": "1",
        }
        return {
            "outcome": "ok", "matches": 1, "role": role or "", "name": name or "",
            "reason": "",
        }

    def _locate(
        self,
        op: str,
        *,
        role: str | None,
        name: str | None,
        ref: str | None = None,
        selector: str | None = None,
    ) -> str | None:
        """按 ``role+name`` 取**我们的** ref；**取不到返回 ``None``（业务事实）**，不抛。

        三态，全部记进 :attr:`last_action` 的 ``outcome``：

        | outcome | 触发 | 处置 |
        |---|---|---|
        | ``ok`` | 恰好 1 个 | 执行动作 |
        | ``not_found`` | **页面上没有**这个目标 | 静默返回——元素不在页面上 |
        | ``not_addressable`` | 页面上有，但被本面过滤剔了 | 静默返回，**附原因** |

        **失败一律抛** :class:`DriverError`——「定位能力不可用」与
        「元素不存在」不能折叠。
        """
        cross = [k for k, v in (("ref", ref), ("selector", selector)) if v]
        if cross:
            # 跨面调用是接线错误：camofox 的定位词是 role+name。
            # 抛 ValueError（不是 DriverError）——后者会被 steps/ 捕获成
            # unreachable_hard，于是接线 bug 变成一条理由通顺的负样本。
            raise ValueError(
                f"camofox 面的 {op} 不支持 {cross}（它的目标编码是 role+name）。"
                f"ref/selector 是 obscura 面的词汇——拿错面说明调用点接了错实现。"
            )
        if not role or not name:
            # 无名字的元素**不提供定位路径**（枚举期已丢）。
            self._miss(op, role, name, "not_found", "")
            return None
        if role not in ROLE_VOCAB:
            self._miss(op, role, name, "not_addressable", "role")
            return None
        target = self._target_index.get((role, name))
        if target:
            return target
        reason = self._dropped.get((role, name))
        if reason:
            self._miss(op, role, name, "not_addressable", reason)
            return None
        self._miss(op, role, name, "not_found", "")
        return None

    def _miss(
        self, op: str, role: str | None, name: str | None,
        outcome: str, reason: str,
    ) -> None:
        self.last_action = {
            "op": op, "role": role or "", "name": name or "",
            "outcome": outcome, "reason": reason,
        }

    async def _do(
        self, op: str, fn: Any, *, role: str | None = None, name: str | None = None
    ) -> None:
        """执行动作。成功记 ``ok``；失败抛——走到这里说明元素确实在，
        所以任何异常都是**能力不可用**（页面崩 / 浏览器挂 / 句柄过期）。"""
        try:
            await fn()
        except DriverError as exc:
            raise DriverError(f"camofox 动作失败（{op}）: {exc}") from None
        self.last_action = {
            "op": op, "role": role or "", "name": name or "", "outcome": "ok",
        }

    async def _settle(self, pre_stamp: str = "") -> None:
        """动作之后**先等新文档 commit，再等 DOM ready**——有界，等不到要**说出来**。

        与 camoufox 面同一条纪律（方案 04 §4.2）：动作触发的导航是**异步
        commit** 的，观察落在「旧文档正在被替换」的那一瞬 = **读到一半**。
        判据用**文档代号**（``performance.timeOrigin``），不用「title 非空」
        那种代理（真有页面没有 ``<title>``）。

        结果记进 ``last_action``：
        - ``nav``：``new`` / ``same`` / ``unknown``（没取到基线，**不猜**）；
        - ``settle``：``ok`` / ``timeout``。
        """
        nav = await self._await_commit(pre_stamp)
        if self.last_action:
            self.last_action["nav"] = nav
        ok = await self._await_ready()
        if self.last_action:
            self.last_action["settle"] = "ok" if ok else "timeout"

    async def _await_commit(self, pre: str) -> str:
        """等新文档 commit。返回 ``"new"`` / ``"same" / "unknown"``。"""
        if not pre:
            return "unknown"
        deadline = time.monotonic() + NAV_COMMIT_TIMEOUT_MS / 1000
        saw_readable = False
        while True:
            cur = await self._doc_stamp()
            if cur:
                saw_readable = True
                if cur != pre:
                    return "new"
            if time.monotonic() >= deadline:
                return "same" if saw_readable else "unknown"
            await asyncio.sleep(NAV_COMMIT_POLL_S)

    async def _await_ready(self) -> bool:
        """等**当前文档** DOM ready（有界）。返回「等到了没有」。

        上游没有「等 load state」这个 tool，所以判据用
        ``document.readyState !== "loading"``——**读一次是一次往返**，
        所以间隔不能太小。
        """
        deadline = time.monotonic() + NAV_SETTLE_TIMEOUT_MS / 1000
        while True:
            try:
                state = await self._eval_text(_READY_STATE_JS)
            except DriverError:
                return False
            if state and state != "loading":
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(NAV_COMMIT_POLL_S)

    # ── 内部 ─────────────────────────────────────────────────────

    async def _doc_stamp(self) -> str:
        """当前文档的**代号**——``performance.timeOrigin``。

        ⚠️ **读不到就返回空串**——**空串不是「同一个文档」，是「不知道」**。
        读不到的原因：导航中执行上下文被销毁（⇒ **正处在换文档的路上**）。
        猜一个「没变」出来会把「读到了半个文档」说成「读稳了」。
        """
        try:
            value = await self._session.evaluate(self._tab, DOC_STAMP_EXPR)
        except DriverError:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return ""
        return str(value)

    async def _eval_text(self, expression: str) -> str:
        """求值并取**文本**。**失败抛** :class:`DriverError`（不吞）。

        需要「失败当空串」的地方由调用方显式 ``try/except``——
        **吞异常的位置必须看得见**，否则「没测到」会被写成「测出来是空的」。
        """
        value = await self._session.evaluate(self._tab, expression)
        return value if isinstance(value, str) else ""

    async def _eval_json(self, expression: str) -> dict[str, Any]:
        """求值并**解析 JSON 对象**。解析不了 ⇒ 抛（形状变了就该炸穿）。"""
        raw = await self._eval_text(expression)
        if not raw.strip():
            raise DriverError("camofox 表达式返回空串——没测到，不是「空对象」")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise DriverError(f"camofox 表达式返回的不是 JSON: {raw[:200]!r}") from exc
        if not isinstance(payload, Mapping):
            raise DriverError("camofox 表达式返回的不是对象")
        return dict(payload)

    async def _read_annotated_nodes(self) -> tuple[Any, ...]:
        """读**全部页**的注记快照并合并，解析成节点序列。

        ## 合并的三条口径（照上游 ``lib/snapshot.js`` 的 ``windowSnapshot`` 读出来的）

        1. **每一页都带同一段尾部**（整份 YAML 的最后 5k 字符）⇒ 合并时**只取一次**；
        2. **最后一页的头部可能已经盖住尾部** ⇒ 尾部按**总长**裁一刀
           （``totalChars - len(tail)``），不靠内容猜；
        3. **页与页的边界可能切在行中间** ⇒ 把上一块末尾的**不完整行**
           挪到下一块前面，那一行才不会丢。

        ⚠️ **分页期间不许动作**：上游在 ``offset>0`` 时复用缓存的快照
        （不刷新 ref），所以一次分页读取内部是**自洽**的；
        但下一次 ``offset=0`` 的读取会**重新编号** ref。
        """
        stamp_before = await self._doc_stamp()
        # 每次枚举**从头算**——上一次留下的旗标不许漏进这一次的账。
        self._nav_unsettled = False
        parts: list[str] = []
        carry = ""
        tail = ""
        total = 0
        offset: int | None = None
        while True:
            payload = await self._session.snapshot(self._tab, offset)
            if not isinstance(payload, Mapping):
                raise DriverError("camofox 快照返回的不是对象")
            text = str(payload.get("snapshot") or "")
            head, chunk_tail = _split_window(text)
            if chunk_tail:
                tail = chunk_tail
            try:
                total = int(payload.get("totalChars") or 0)
            except (TypeError, ValueError):
                total = 0
            chunk = carry + head
            if not payload.get("hasMore"):
                parts.append(chunk)
                carry = ""
                break
            next_offset = payload.get("nextOffset")
            if not isinstance(next_offset, int):
                raise DriverError("camofox 快照说有下一页，却没给 nextOffset")
            # 本块末尾可能截断了一行 ⇒ 挪到下一块前面。
            nl = chunk.rfind("\n")
            if nl >= 0:
                parts.append(chunk[: nl + 1])
                carry = chunk[nl + 1:]
            else:
                carry = chunk
            offset = next_offset
        merged_head = "".join(parts)
        if tail and total > 0:
            # 尾部在**每一页**里都出现过 ⇒ 按总长裁掉头部里的重叠部分，
            # 只留一份尾部。**不靠内容猜重叠**——内容猜会在「结尾恰好重复」
            # 的页面上裁掉真内容。
            cut = max(total - len(tail), 0)
            merged = merged_head[:cut] + tail
        else:
            merged = merged_head
        stamp_after = await self._doc_stamp()
        if stamp_before and stamp_after and stamp_before != stamp_after:
            # 分页期间换了文档 ⇒ 这份清单**跨了两个文档** ⇒ 不可信。
            # ⚠️ 记在**独立字段**上，不写 ``last_enumeration``——
            # 调用方（``interactive``）紧接着会整份覆盖那本账（见 ``__init__``）。
            self._nav_unsettled = True
        return parse_nodes(merged)

    # ── 采集面：链接 / 抽取（P3-β）────────────────────────────────

    #: 取链接的 JS：先滤掉空 ``href`` 的锚，再取 ``[可见文本, 绝对地址]``。
    #:
    #: 两条口径都定死在这里（与 camoufox 面同口径）：
    #: - ``getAttribute('href').trim()`` 判空——``<a href="">`` 的 ``a.href``
    #:   会被解析成**当前页地址**，不过滤就凭空多出一串「指向自己的链接」；
    #: - 取 ``a.href`` 而**不是**属性原文——它是**已解析的绝对地址**。
    _LINKS_JS = (
        "(function(){var els=document.querySelectorAll('a[href]');"
        "var out=[];for(var i=0;i<els.length;i++){var e=els[i];"
        "if(!((e.getAttribute('href')||'').trim()))continue;"
        "out.push([(e.innerText||e.textContent||'').trim(),e.href]);}"
        "return JSON.stringify(out);})()"
    )

    async def links(self, *, limit: int = DEFAULT_LIMIT) -> tuple[CleanedLink, ...]:
        """列链接：``a[href]`` 的 ``(文本, 绝对地址)``，按文档顺序，**按 href 去重**。

        **去重是量出来的对齐，不是洁癖**（与 camoufox 面同一条）：
        obscura 那边 ``total == distinct`` 恒成立（它自己就按 href 去重）。

        ⚠️ **失败抛** :class:`DriverError`（obscura 同位置是 ``on_error="empty"``
        吞成空元组）。空元组意味着「页面上没有链接」——那是页面事实；
        把一次能力故障写成页面事实，正是本仓库最贵的一类错。
        """
        raw = await self._eval_text(self._LINKS_JS)
        try:
            rows = json.loads(raw) if raw.strip() else []
        except ValueError:
            rows = []
        cap = max(int(limit), 0)
        out: list[CleanedLink] = []
        seen: set[str] = set()
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue                      # 形状不对就丢，不猜
            text = str(row[0] or "").strip()
            href = str(row[1] or "").strip()
            if not href or href in seen:
                continue
            seen.add(href)
            out.append(CleanedLink(text=text, href=href))
            if len(out) >= cap:
                break
        return tuple(out)

    @staticmethod
    def _pluck_js(selector: str, attr: str | None) -> str:
        """生成「按元素取值」的 JS（IIFE 形态）。

        两条口径，与 camoufox 面同一条（**同一个上游的同一段语义**）：

        - **文本 = ``innerText || textContent``**（``innerText`` 在真实页面上
          常常是空串，只取它的症状是「抽到 10 个空标题」）；
        - **属性 = ``typeof e[attr] === 'string' ? e[attr] : getAttribute(attr)``**：
          ``href`` / ``src`` 这类给的是**解析后的绝对地址**。
        """
        quoted = json.dumps(selector)
        if attr:
            quoted_attr = json.dumps(attr)
            return (
                "(function(){var els=document.querySelectorAll(" + quoted + ");"
                "var out=[];for(var i=0;i<els.length;i++){var e=els[i];"
                "var a=" + quoted_attr + ";"
                "var v=(typeof e[a]==='string'?e[a]:e.getAttribute(a));"
                "out.push(v==null?'':String(v).trim());}"
                "return JSON.stringify(out);})()"
            )
        return (
            "(function(){var els=document.querySelectorAll(" + quoted + ");"
            "var out=[];for(var i=0;i<els.length;i++){var e=els[i];"
            "out.push(((e.innerText||e.textContent||'')).trim());}"
            "return JSON.stringify(out);})()"
        )

    async def extract(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        """按 ``{字段名: CSS selector}`` 抽内容——**复刻 obscura 的两条入参约定**。

        | 形态 | 例 | 行为 |
        |---|---|---|
        | 普通 | ``"h2"`` | 首个匹配的**可见文本** |
        | 属性 | ``"a@href"`` | 该元素的属性值（URL 类属性取解析后的绝对值） |
        | 数组 | ``"titles[]"`` | 全部匹配，值为数组（**返回的键名不带** ``[]``） |

        **抽不到就不写这个键**，不编造 ``""``——写空串会让「抽不到」与
        「页面上是空的」同形，而 ``steps/search.result_titles`` 恰是按
        「两数组等长且非空」判有效性。
        """
        if not fields:
            return {}
        out: dict[str, Any] = {}
        for raw_name, raw_spec in fields.items():
            name = str(raw_name)
            want_all = name.endswith("[]")
            key = name[:-2] if want_all else name
            spec = str(raw_spec)
            selector, sep, attr = spec.rpartition("@")
            if not sep:                      # 没有 ``@``：整串都是选择器
                selector, attr = spec, ""
            values = await self._pluck(selector, attr or None)
            if want_all:
                # 空数组照写：键在、值是空数组，这是「选择器没命中」的如实形态。
                out[key] = values
            elif values:
                out[key] = values[0]
        return out

    async def _pluck(self, selector: str, attr: str | None) -> list[str]:
        target = f"{selector}@{attr}" if attr else selector
        try:
            raw = await self._eval_text(self._pluck_js(selector, attr))
        except DriverError as exc:
            raise DriverError(f"camofox 抽取失败（{target}）: {exc}") from None
        try:
            rows = json.loads(raw) if raw.strip() else []
        except ValueError:
            rows = []
        return [str(v) if v is not None else "" for v in (rows or [])]

    # ── 采集面：标签页（P3-β）────────────────────────────────────

    async def new_tab(self, url: str | None = None) -> str:
        """开一张新页并切成当前页；返回**本驱动发的**句柄（``c1`` / ``c2``…）。

        ⚠️ 句柄**不是**上游的 ``tabId``，两套 id 不可互换；它唯一的用途是
        让 ``close_tab(id)`` 能指名关后台页。

        开页后导航失败**先收掉这一页再抛**：留着它，当前页就停在一张
        ``about:blank`` 上——一次外链取不到，会连带把中间页的证据一起弄丢。
        """
        camo_tab = ""
        try:
            camo_tab = await self._session.new_page(url or "")
        except DriverError as exc:
            self.last_tab = {"op": "new_tab", "tab": "", "pages": "", "outcome": "failed"}
            raise DriverError(f"camofox 开新页失败: {exc}") from None

        self._tab_seq += 1
        tab_id = f"c{self._tab_seq}"
        self._tabs[tab_id] = camo_tab
        self._reset_page_state()              # 换页即换 ref 空间
        self.last_tab = {
            "op": "new_tab", "tab": tab_id,
            "pages": str(len(self._session.pages)), "outcome": "ok",
        }
        return tab_id

    async def close_tab(self, tab_id: str | None = None) -> None:
        """关掉一页（``None`` = 当前页），**关的是当前页就回到上一页**。

        认不出的 ``tab_id`` **抛**，绝不「找一个关掉」——关错页的后果是后续
        ``observe`` 读到别的页，而那**不会报错**。
        """
        if tab_id is None:
            camo_tab = self._tab
            target = next((k for k, v in self._tabs.items() if v == camo_tab), "")
        else:
            camo_tab = self._tabs.get(tab_id) or ""
            target = tab_id
            if not camo_tab:
                raise DriverError(
                    f"camofox 不认识 tab {tab_id!r}"
                    f"（本会话发过的是 {sorted(self._tabs) or '无'}）"
                )
        try:
            closed = await self._session.close_page(camo_tab)
        except DriverError as exc:
            self.last_tab = {"op": "close_tab", "tab": target, "pages": "", "outcome": "failed"}
            raise DriverError(f"camofox 关页失败: {exc}") from None

        if target:
            self._tabs.pop(target, None)
        self._reset_page_state()              # 关页即换页
        self.last_tab = {
            "op": "close_tab", "tab": target,
            "pages": str(len(self._session.pages)),
            "outcome": "ok" if closed else "not_found",
        }

    # ── 组合采集（P3-β）──────────────────────────────────────────

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        """采集结构化观察——与 obscura / camoufox 面**同字段、同语义**。

        ``snapshot`` 是唯一必需项：失败即抛（没有观察就没有判定，I4）。
        其余各项缺失**降级为空/0 并把降级项记进 ``degraded``**——宁可让感知层判
        ``answer=None``，也不让「没采到」被当成「页面上确实没有」。

        ## 读数自检

        一次观察是 4 次以上独立往返（url/title/正文 → 链接 → 交互元素 → 计数）。
        导航是**异步 commit** 的——commit 落在两次往返之间，这份观察就是
        **两个文档拼起来的**。⇒ 读数前后各取一次**文档代号**，变了就是
        **读到一半** ⇒ **重来一次**（:data:`OBSERVE_RETRY` 次）；
        一直读不稳就记 ``degraded``，让下游**不判**（fail-closed）。
        """
        obs: Observation | None = None
        torn = False
        for attempt in range(OBSERVE_RETRY + 1):
            degraded: list[str] = []
            stamp_before = await self._doc_stamp()
            try:
                snap = await self.snapshot(max_chars=max_chars, degraded=degraded)
            except DriverError:
                # 快照失败抛（I4）。但**导航途中**的失败是「读到一半」，
                # 给它一次机会重来——重试仍失败就照旧抛（不掩盖真因）。
                if attempt >= OBSERVE_RETRY:
                    raise
                await self._await_ready()
                continue

            try:
                links = await self.links()
            except DriverError:
                degraded.append("links")
                links = ()
            try:
                elements = await self.interactive()
            except DriverError:
                degraded.append("interactive")
                elements = ()
            # **当场的**阶梯账目：``last_enumeration`` 是驱动级状态，
            # 枚举失败时它还留着**上一页**的数——直接读会把上一页的账
            # 记到这一页头上。取不到就是空账。
            face_ledger = dict(self.last_enumeration) if elements else {}

            video_count = await self._count_or_zero(MEDIA_SELECTOR, degraded)
            iframe_count = await self._count_or_zero(IFRAME_SELECTOR, degraded)

            basis, basis_error = await self._collect_basis(degraded, face_ledger)

            # 「按键做不到」是**能力降级**，不是「元素不存在」——
            # 上一次 press 降级了就记进 degraded，让下游**不判**。
            if self.last_action.get("outcome") == "unsupported":
                degraded.append(DEGRADED_PRESS_UNSUPPORTED)

            stamp_after = await self._doc_stamp()
            obs = Observation(
                url=snap.url,
                page_title=snap.title,
                body_text=snap.body,
                interactive_elements=elements,
                links=tuple(LinkItem(text=l.text, href=l.href) for l in links),
                elements_total=len(elements),
                links_total=len(links),
                element_basis=basis,
                element_basis_error=basis_error,
                # 方言由**驱动实现**自己填（P4）：camofox 面的 role 是 ARIA role。
                element_vocabulary=ELEMENT_VOCAB_ARIA_ROLE,
                video_tag_count=video_count,
                iframe_count=iframe_count,
                max_chars=snap.max_chars,
                truncated=snap.truncated,
                raw_len=snap.raw_len,
                stripped_ratio=snap.stripped_ratio,
                body_source=snap.body_source,
                degraded=tuple(dict.fromkeys(degraded)),   # 去重且保序
            )

            # **读数跨了文档** = 半成品。三种证据：
            #  ① 前后代号**变了** ⇒ 中途换了文档；
            #  ② 某个代号**读不到**（空串）⇒ 执行上下文被销毁 ⇒
            #     **正处在换文档的路上**（比 ① 更早的信号）。
            # 代号读到 ``"none"``（页面没有 performance）**不算**证据——
            # 它恒定不变，恒定的东西永远判不出「变了」。
            torn = (not stamp_before) or (not stamp_after) or stamp_before != stamp_after
            if not torn:
                break
            if attempt >= OBSERVE_RETRY:
                break
            # 重来之前先等新文档落定：commit 可能在读完之后才发生。
            await self._await_ready()

        if obs is None:      # pragma: no cover - 循环至少跑一次
            raise DriverError("camofox 观察未产出")
        if torn:
            obs = replace(obs, degraded=tuple(dict.fromkeys(
                (*obs.degraded, DEGRADED_NAV_UNSETTLED))))
        return obs

    async def _count_or_zero(self, selector: str, degraded: list[str]) -> int:
        """计数，**失败记降级并返回 0**。

        为什么不复用 :meth:`count`：那条已经把失败折叠成 0，于是 ``observe``
        里**看不出**这个 0 是「页面上没有」还是「没采到」——而这两者的正确
        处置相反。严格版把区别留下来，对外口径仍由 :meth:`count` 维持。
        """
        try:
            return int(await self.count(selector))
        except (DriverError, TypeError, ValueError):
            degraded.append(f"count:{selector}")
            return 0


def _split_window(text: str) -> tuple[str, str]:
    """把上游的一页窗口拆成 ``(头部, 尾部)``。

    上游的窗口是 ``头部 + 截断标记行 + 尾部``，而**尾部每页都一样**
    （整份 YAML 的最后 5k 字符）。合并多页时**只取一次尾部**，否则会重复。

    标记没找到 ⇒ 整段都是头部（页面没超限，一次就读完了）。
    """
    idx = text.find(_PAGINATION_MARKER)
    if idx < 0:
        return text, ""
    line_end = text.find("\n", idx)
    if line_end < 0:
        return text[:idx], ""
    return text[:idx], text[line_end + 1:]
