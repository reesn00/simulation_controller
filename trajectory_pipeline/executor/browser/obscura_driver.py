"""obscura 驱动的 :class:`PageDriver` 实现。

格式知识全部集中在这里——传输层（``mcp_client.py``）与协议层
（``page_driver.py``）都不认识 obscura 的 tool 名。将来换浏览器，
只改本文件 + :mod:`trajectory_pipeline.executor.browser.driver_factory`
（装配唯一入口；生产代码**不许直接构造本类**，门禁见
``tests/unit/test_driver_factory.py``）。

全部格式知识来自实测（``output/pipeline/obscura_returns_*.json``），
**不是按 tool 描述推测**。已踩过的格式坑：

- ``browser_count`` 返回 **JSON 数字**（``70``），不是文本
- ``browser_links`` 是 **NDJSON**，整体 ``json.loads`` 必失败，须逐行解析；
  空页返回哨兵文本 ``No links found.``
- ``browser_interactive_elements`` 是**列式文本**（列间多个空格），
  label 外层是 JSON 风格字符串且内含转义引号
- ``browser_snapshot`` 带 ``URL:`` / ``Title:`` 前缀，且**正文会混入
  ``<style>`` 的 CSS**——不剥离则 max_chars 预算被样式表吃光
- 错误有两种形态：``isError=True``，以及正文以 ``Error:`` 开头但
  ``isError=False``（如 ``Element not found``）。后者是业务事实而非故障
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from trajectory_pipeline.executor.browser.mcp_client import McpClient, McpError
from trajectory_pipeline.executor.browser.nav_failure import site_unreachable
from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    DriverUnreachableError,
)
from trajectory_pipeline.executor.dom import (
    IFRAME_SELECTOR,        # noqa: F401  （定义已迁 dom.py；此处再导出供既有引用）
    INNER_TEXT_EXPR,        # noqa: F401  （定义已迁 dom.py；此处再导出供既有引用）
    MEDIA_SELECTOR,         # noqa: F401
    TEXT_CONTENT_EXPR,      # noqa: F401
    CleanedLink,
    CleanedSnapshot,
    is_mojibake,
    looks_like_error,
    parse_body_text,
    parse_count,
    parse_extract,
    parse_interactive_elements,
    parse_links,
    parse_snapshot,
)
from trajectory_pipeline.common.elements import (
    ELEMENT_VOCAB_HTML_TAG,
)
from trajectory_pipeline.executor.browser.element_basis import (
    SHARED_DOM_JS,
    parse_shared_basis,
)
from trajectory_pipeline.perception.base import (
    InteractiveElement,
    LinkItem,
    Observation,
)

#: 交互元素与链接的默认取回上限。播放页的候选元素通常远少于 200。
DEFAULT_LIMIT = 200

#: **红线：永久不得包装的 tool**（CLAUDE.md「不保存 Cookie / Authorization
#: Header 或浏览器 Profile」）。obscura 37 个 tool 里有 5 个能碰这些东西，
#: :class:`ObscuraDriver` 一律不包装——不是「暂时没需求」，是**永不提供**：
#: 一旦暴露，将来某次「需要登录态」的临时需求就会把 profile 写进 P1 存档，
#: 而存档是要推给 Label Studio 的。补救成本远高于现在的 5 行代码。
#:
#: 真出现登录墙站点时，正确做法是判 negative 并记 ``login_wall_or_blocked``，
#: 而不是绕过登录——这与失败分支表的设计一致。
FORBIDDEN_TOOLS = frozenset(
    {
        "browser_get_cookies",
        "browser_set_cookie",
        "browser_clear_cookies",
        "browser_set_storage_state",
        "browser_storage_state",
    }
)


def _assert_tool_allowed(tool: str) -> None:
    """拦住红线 tool。调用点统一走 :meth:`_call`，故这是唯一的闸门。"""
    if tool in FORBIDDEN_TOOLS:
        raise DriverError(f"obscura tool {tool} 触碰隐私红线，禁止包装")


def _reject_role_name(op: str, *, role: str | None, name: str | None) -> None:
    """挡住跨面调用：obscura **没有** ``role+name`` 这条定位路径。

    为什么抛而不是忽略：obscura 的 ``browser_click`` 只认 ``ref``/``selector``，
    多给的参数会被上游忽略——于是「按 role+name 点」会**静默退化成什么都没指定**，
    症状与「点了但页面没变」完全同形。跨面调用是接线错误（调用方拿错了面的词汇），
    必须当场炸，不能留到读存档时才发现整批动作没发生。

    ⚠️ **抛 ``ValueError`` 而不是 ``DriverError``**：后者被 `steps/` 捕获后会走
    「能力不可用」分支、**照样产出样本**——于是一个接线 bug 会被写成一条
    理由通顺的负样本。接线错误必须炸穿，不能变成数据。
    """
    if role is not None or name is not None:
        raise ValueError(
            f"obscura 面的 {op} 不支持 role+name（它的动作面是 ref/selector）。"
            f"role={role!r} name={name!r} 是 camoufox 面的目标编码——"
            f"拿错面的词汇说明调用点接了错实现。"
        )


class ObscuraDriver:
    """obscura MCP server 的 :class:`PageDriver` 实现。"""

    name = "obscura"

    def __init__(self, client: McpClient, *,
                 collect_element_basis: bool = False) -> None:
        self._mc = client
        #: **跨面可比的元素账目要不要采**（设计方案 04 §4.9.6）。
        #:
        #: **默认关**：它多一次 ``browser_evaluate`` 往返（MCP 往返不便宜），
        #: 是**测量仪器**不是生产链的必需项。开着的时候 :meth:`observe`
        #: 会把两本账落进 :attr:`Observation.element_basis`。
        self._collect_element_basis = collect_element_basis

    # ── 内部：调 tool 并把业务性错误折叠掉 ───────────────────────

    async def _call(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        on_error: str = "raise",
        degraded: list[str] | None = None,
    ) -> str | None:
        """调用 tool，返回正文。

        Args:
            on_error:
                ``"raise"``   出错抛 :class:`DriverError`（用于导航等必需动作）
                ``"empty"``   出错返回 ``None``（用于可选采集，缺失即缺失）
                ``"zero"``    出错返回 ``"0"``（用于计数，缺失降级为 0）
            degraded: 传入 list 时，**降级会把 tool 名记进去**。
                这是 :attr:`Observation.degraded` 的唯一来源——没有它，
                「没采到」与「确实为空」在下游无法区分（见 base.py 注释）。
        """
        _assert_tool_allowed(tool)
        try:
            result = await self._mc.call(tool, args or {})
        except McpError:
            if on_error == "raise":
                raise DriverError(f"obscura tool {tool} 传输失败") from None
            if degraded is not None:
                degraded.append(tool)
            return None if on_error == "empty" else "0"

        text = result.text
        if looks_like_error(text, result.is_error):
            if on_error == "raise":
                # ⚠️ **判据必须在截断之前判**（M-44）：obscura 的错误正文
                # 是 Rust/reqwest 风格的长句，``dns error`` 那个机器码
                # 恰好落在**第 200 字之后** ⇒ 拿截断后的原文判，
                # **DNS 失败会被误判成「证明不了站点不可达」**。
                # ⇒ 先用**完整原文**判，再把消息截短（存档文本格式不变）。
                hit = site_unreachable(text)
                if hit is not None:
                    raise DriverUnreachableError(
                        f"obscura tool {tool} 失败: {text.strip()[:200]}",
                        code=hit.code,
                    ) from None
                raise DriverError(f"obscura tool {tool} 失败: {text.strip()[:200]}")
            if degraded is not None:
                degraded.append(tool)
            return None if on_error == "empty" else "0"
        return text

    # ── 原子原语 ──────────────────────────────────────────────────

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        """导航。失败抛 :class:`DriverError`——调用方须 fail-closed。"""
        args: dict[str, Any] = {"url": url}
        if wait_until and wait_until != "load":
            args["waitUntil"] = wait_until
        await self._call("browser_navigate", args, on_error="raise")

    async def snapshot(
        self, *, max_chars: int | None = None, degraded: list[str] | None = None
    ) -> CleanedSnapshot:
        """取快照。URL / Title 来自 snapshot，**正文优先来自 innerText**。

        两段式是因为实测出来 obscura 的两个原语各缺一半（``dom.py`` 模块
        docstring 有完整数据）：

            ``browser_snapshot``  给了 ``URL:`` / ``Title:`` 前缀，
                                  但正文被 CSS 抢光——baidu 首页 6000 字符预算
                                  里没有一个字是正文（真实正文只有 474 字符）
            ``innerText``         正文干净（474 字符、47ms、零 CSS），
                                  但**不带 URL / Title**

        snapshot 仍然必需——它是 fail-closed 的锚点，也是 ``truncated`` /
        ``stripped_ratio`` 的诊断来源；innerText 取不到时降级用 snapshot 正文，
        并由 ``CleanedSnapshot.body_source`` 如实标记。

        ⚠️ innerText 降级**必须记进 ``degraded``**：退回的快照正文可能是
        「整段 CSS 被剥光后的空壳」（实测 iqiyi / ixigua / sohu 三站
        ``body_len=0``、``interactive_elements=0``）。不记的话下游分不清
        「这页没内容」与「正文根本没采到」，会把采集失败写成业务负样本。
        """
        args: dict[str, Any] = {"max_chars": max_chars} if max_chars else {}
        text = await self._call("browser_snapshot", args, on_error="raise")

        inner = await self._call("browser_evaluate", {"expression": INNER_TEXT_EXPR},
                                 on_error="empty", degraded=degraded)
        body = parse_body_text(inner, max_chars=max_chars) if inner else None
        if not body and degraded is not None:
            # 2026-10-14 增量 H1-β：innerText 失败/空时 fallback 到
            # ``document.body.textContent``。textContent 不参与 layout 计算，
            # 在 innerText 因 JS 渲染未完成/选择器触发的 layout thrashing 失败
            # 时仍能拿到已渲染的部分文本。fallback 仍失败才追加
            # ``degraded.append("body_text")``——保持原有 fail-closed 边界。
            fallback = await self._call(
                "browser_evaluate",
                {"expression": TEXT_CONTENT_EXPR},
                on_error="empty",
                degraded=None,  # 不让 fallback 失败污染 degraded 集合
            )
            body = parse_body_text(fallback, max_chars=max_chars) if fallback else None
            if not body:
                # 两种「没正文」都要记：tool 报错（降级进 degraded），
                # 以及 tool 成功但返回空串——JS 页面还没渲染时正是后者，
                # **不是错误**所以 _call 不会记，得在这里补。
                degraded.append("body_text")
        elif body is not None and degraded is not None and is_mojibake(body):
            # 乱码正文**不是「没采到」，是「采到了但不可读」**——两者对
            # 下游的影响不同：前者无据可依，后者最危险，因为它看起来
            # 有内容，rationale 闸门的实体核查会把「模型引用了页面上
            # 真实存在但已损坏的实体」判成幻觉（凭空造出负样本）。
            degraded.append("body_mojibake")
        snap = parse_snapshot(text or "", max_chars=max_chars, body=body or None)
        # 标题单独记，且**与正文分开**——两者可以只坏一个。
        # 标题是 P1 里「不给就不可判定」的字段之一（判断点 ② 的判据
        # 之一就是「标题是否与目标影片一致」），标题烂掉时那条判断
        # 根本没法做，而正文可能完好——合成一个标记就分不出是哪部分
        # 不可读，降级范围也会被错误地放大到整个观察。
        if degraded is not None and snap.title and is_mojibake(snap.title):
            degraded.append("title_mojibake")
        return snap

    async def links(self, *, limit: int = DEFAULT_LIMIT) -> tuple[CleanedLink, ...]:
        text = await self._call("browser_links", {"limit": limit}, on_error="empty")
        return parse_links(text or "")

    async def interactive(self, *, limit: int = DEFAULT_LIMIT) -> tuple[InteractiveElement, ...]:
        text = await self._call(
            "browser_interactive_elements", {"limit": limit}, on_error="empty"
        )
        return tuple(
            InteractiveElement(ref=ref, role=tag, name=label)
            for ref, tag, label in parse_interactive_elements(text or "")
        )

    async def element_basis(self) -> dict[str, int]:
        """**跨面可比的**元素枚举账目（设计方案 04 §4.9.6）。

        跑的是 :data:`element_basis.SHARED_DOM_JS`——**与 aria 面（camoufox /
        camofox）同一段 JS、同一把尺子**，于是两面的数字**按构造可比**。

        与 :meth:`interactive` 的差别是**枚举基础**：
        这里是 **DOM 交互候选**（role/name 自己算），
        那里是**上游工具给什么拿什么**（黑盒）。两者**故意不同**——
        共享口径要回答的是「同一个页面在两个浏览器上差多少」，
        用同一段 JS 才量得出来。

        ⚠️ **失败抛** :class:`DriverError`：空账会被读成
        「这个页面上一个交互元素都没有」，而真相是「没测到」。
        """
        text = await self._call(
            "browser_evaluate", {"expression": SHARED_DOM_JS}, on_error="raise",
        )
        try:
            _, ledger = parse_shared_basis(text)
        except ValueError as exc:
            raise DriverError(f"obscura 共享口径返回不合法: {exc}") from None
        return ledger

    async def count(self, selector: str) -> int:
        text = await self._call("browser_count", {"selector": selector}, on_error="zero")
        return parse_count(text or "")

    async def extract(self, fields: Mapping[str, str]) -> Mapping[str, Any]:
        """``browser_extract`` 接入。

        obscura 的字段名带 ``[]`` 表示「取全部匹配，值为数组」，
        且 ``'a@href'`` 表示取属性而非文本——这两条都是上游既有语义，
        这里原样透传，**不重命名也不解释**：重命名等于把上游约定
        抄一份，抄的那份迟早与上游漂移。

        ⚠️ **上游的入参键叫 ``schema``，不叫 ``fields``。**
        写成 ``fields`` 时上游回 ``Error: Missing schema object``，
        而 :meth:`_call` 的 ``on_error="empty"`` 会把它吞成空字典——
        于是症状是「抽不到标题」，和「选择器失效」「页面没渲染」
        「选择器名写错了」长得一模一样。obscura 的
        ``tools/list`` **不给 inputSchema**（实测四个采集 tool 全是
        ``null``，它把 schema 放在自定义的 ``input_schema`` 键里），
        所以这个键名只能靠 ``probe_returns.py`` 取证，不能靠猜。
        """
        if not fields:
            return {}
        text = await self._call(
            "browser_extract", {"schema": dict(fields)}, on_error="empty",
        )
        return parse_extract(text or "")

    async def evaluate(self, expression: str) -> Any:
        text = await self._call("browser_evaluate", {"expression": expression}, on_error="empty")
        raw = (text or "").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw          # 表达式返回非 JSON（如字符串）时原样给出

    async def click(
        self,
        *,
        ref: str | None = None,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> None:
        """点击。元素不存在时静默返回——那是业务事实，由上层转 unresolved。

        静默处理是有意的：播放按钮不存在 ≠ 站点不可用。
        上层拿不到 ``goto`` 之后的快照变化，会自然走 ``answer=None`` 分支。
        """
        _reject_role_name("click", role=role, name=name)
        if not ref and not selector:
            raise ValueError("click 需要 ref 或 selector 之一")
        args: dict[str, Any] = {}
        if ref:
            args["ref"] = ref
        else:
            args["selector"] = selector
        await self._call("browser_click", args, on_error="empty")

    async def fill(
        self,
        *,
        text: str,
        ref: str | None = None,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> None:
        """设值（先清空再写）。**旧名 ``type_text`` 见协议注释**。

        obscura 同时提供 ``browser_fill``（设值）与 ``browser_type``（追加），
        搜索场景要设值——往已有关键词的搜索框里追加会产生错误 query。
        """
        _reject_role_name("fill", role=role, name=name)
        if not ref and not selector:
            raise ValueError("fill 需要 ref 或 selector 之一")
        key = "ref" if ref else "selector"
        target = ref if ref else selector
        await self._call("browser_fill", {key: target, "value": text}, on_error="empty")

    async def press(self, key: str, *, role: str | None = None, name: str | None = None) -> None:
        """页面级按键。**带目标的形式只有 camoufox 面支持**——obscura 的
        ``browser_press_key`` 没有元素参数，硬塞会被上游忽略（静默变成
        页面级按键），所以在这里显式拒绝。"""
        _reject_role_name("press", role=role, name=name)
        await self._call("browser_press_key", {"key": key}, on_error="empty")

    async def new_tab(self, url: str | None = None) -> str:
        text = await self._call("browser_tab_new", {"url": url} if url else {}, on_error="raise")
        # 返回形如 tab id；直接给原文，由调用方决定如何解析
        return (text or "").strip()

    async def close_tab(self, tab_id: str | None = None) -> None:
        args = {"tab_id": tab_id} if tab_id else {}
        await self._call("browser_tab_close", args, on_error="empty")

    # ── 组合采集 ──────────────────────────────────────────────────

    async def observe(self, *, max_chars: int | None = None) -> Observation:
        """采集结构化观察。

        snapshot 是**唯一必需项**——失败即抛（没有观察就没有判定，I4）。
        其余各项缺失降级为空/0，不让整次采集作废，但**降级会被记录**：
        宁可让感知层判 ``answer=None``，也不让「没采到」被当成
        「页面上确实没有」（否则基础设施故障会被写进业务结论）。
        """
        degraded: list[str] = []
        snap = await self.snapshot(max_chars=max_chars, degraded=degraded)

        links_raw = await self._call(
            "browser_links", {"limit": DEFAULT_LIMIT},
            on_error="empty", degraded=degraded,
        )
        elements_raw = await self._call(
            "browser_interactive_elements", {"limit": DEFAULT_LIMIT},
            on_error="empty", degraded=degraded,
        )
        video_count = await self._count_or_zero(MEDIA_SELECTOR, degraded)
        iframe_count = await self._count_or_zero(IFRAME_SELECTOR, degraded)

        links = parse_links(links_raw or "")
        elements = tuple(
            InteractiveElement(ref=ref, role=tag, name=label)
            for ref, tag, label in parse_interactive_elements(elements_raw or "")
        )
        # 跨面可比的元素账目（**默认不采**，见 __init__ 的说明）。
        #
        # ⚠️ 本面（face）的账目**只有两条数**——上游工具只给最终列表，
        # 给不出「role 命中几条 / 无名丢掉几条」。**缺就是缺，不填 0**：
        # 填 0 会被读成「一条都没被过滤」。
        #
        # ⚠️ **测量失败不进 ``degraded``**（2026-10-21 修）：``degraded``
        # 是**内容块**的降级通道，下游只判它非空——``extract_candidates``
        # 见到降级就**不取候选**，于是「尺子坏了」会**作废整批数据**。
        # 失败只记进 ``element_basis_error``，face 账目照旧给。
        basis = None
        basis_error = None
        if self._collect_element_basis:
            try:
                shared = await self.element_basis()
            except DriverError as exc:
                basis_error = str(exc)
                shared = None
            basis = {"face": {"nodes": len(elements), "kept": len(elements)}}
            if shared is not None:
                basis["shared"] = shared
        return Observation(
            url=snap.url,
            page_title=snap.title,
            body_text=snap.body,
            interactive_elements=elements,
            links=tuple(LinkItem(text=l.text, href=l.href) for l in links),
            # 采到的条数（已被 DEFAULT_LIMIT 截断）——存档侧还会再截到
            # 80/50，落盘的量必须能对上，否则 D-2 分不清「被裁掉」与
            # 「不存在」。理由见 Observation.elements_total 的说明。
            elements_total=len(elements),
            links_total=len(links),
            element_basis=basis,
            element_basis_error=basis_error,
            # 方言由驱动实现自己填（P4）。obscura 面的 ``role`` 槽装的是
            # **HTML 标签名**——这是那个方言的值域，不是命名错误；下游按
            # 这个字段选词表，不按驱动名去猜。
            element_vocabulary=ELEMENT_VOCAB_HTML_TAG,
            video_tag_count=video_count,
            iframe_count=iframe_count,
            max_chars=snap.max_chars,
            truncated=snap.truncated,
            raw_len=snap.raw_len,
            stripped_ratio=snap.stripped_ratio,
            body_source=snap.body_source,
            degraded=tuple(dict.fromkeys(degraded)),   # 去重且保序
        )

    async def _count_or_zero(self, selector: str, degraded: list[str]) -> int:
        text = await self._call(
            "browser_count", {"selector": selector},
            on_error="zero", degraded=degraded,
        )
        return parse_count(text or "0")

    async def close(self) -> None:
        await self._mc.__aexit__(None, None, None)