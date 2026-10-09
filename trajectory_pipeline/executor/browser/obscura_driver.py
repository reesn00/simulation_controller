"""obscura 驱动的 :class:`PageDriver` 实现。

格式知识全部集中在这里——传输层（``mcp_client.py``）与协议层
（``page_driver.py``）都不认识 obscura 的 tool 名。将来换浏览器，
只改本文件 + ``factory``。

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
from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.executor.dom import (
    CleanedLink,
    CleanedSnapshot,
    looks_like_error,
    parse_body_text,
    parse_count,
    parse_extract,
    parse_interactive_elements,
    parse_links,
    parse_snapshot,
)
from trajectory_pipeline.perception.base import InteractiveElement, LinkItem, Observation

#: 探测存在性时用的选择器。obscura 的 browser_count 接受任意 CSS selector，
#: 所以 iframe 直接用选择器测，不需要绕道 evaluate。
MEDIA_SELECTOR = "video, audio"
IFRAME_SELECTOR = "iframe"

#: 正文主来源。实测 baidu 首页：``innerText`` 474 字符纯文本 / 47ms，
#: 而 ``browser_snapshot(max_chars=6000)`` 的 6000 字符预算被 16675 字符的
#: ``<style>`` 块全部吃光。详见 ``executor/dom.py`` 模块 docstring。
INNER_TEXT_EXPR = "document.body.innerText"

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


class ObscuraDriver:
    """obscura MCP server 的 :class:`PageDriver` 实现。"""

    name = "obscura"

    def __init__(self, client: McpClient) -> None:
        self._mc = client

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
            # 两种「没正文」都要记：tool 报错（降级进 degraded），
            # 以及 tool 成功但返回空串——JS 页面还没渲染时正是后者，
            # **不是错误**所以 _call 不会记，得在这里补。
            degraded.append("body_text")
        return parse_snapshot(text or "", max_chars=max_chars, body=body or None)

    async def links(self, *, limit: int = DEFAULT_LIMIT) -> tuple[CleanedLink, ...]:
        text = await self._call("browser_links", {"limit": limit}, on_error="empty")
        return parse_links(text or "")

    async def interactive(self, *, limit: int = DEFAULT_LIMIT) -> tuple[InteractiveElement, ...]:
        text = await self._call(
            "browser_interactive_elements", {"limit": limit}, on_error="empty"
        )
        return tuple(
            InteractiveElement(ref=ref, tag=tag, label=label)
            for ref, tag, label in parse_interactive_elements(text or "")
        )

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

    async def click(self, *, ref: str | None = None, selector: str | None = None) -> None:
        """点击。元素不存在时静默返回——那是业务事实，由上层转 unresolved。

        静默处理是有意的：播放按钮不存在 ≠ 站点不可用。
        上层拿不到 ``goto`` 之后的快照变化，会自然走 ``answer=None`` 分支。
        """
        if not ref and not selector:
            raise ValueError("click 需要 ref 或 selector 之一")
        args: dict[str, Any] = {}
        if ref:
            args["ref"] = ref
        else:
            args["selector"] = selector
        await self._call("browser_click", args, on_error="empty")

    async def type_text(
        self, *, text: str, ref: str | None = None, selector: str | None = None
    ) -> None:
        if not ref and not selector:
            raise ValueError("type_text 需要 ref 或 selector 之一")
        # obscura 同时提供 browser_fill（设值）与 browser_type（追加）。
        # 搜索场景要「设值」——往已有关键词的搜索框里追加会产生错误 query，
        # 故一律走 fill。
        key = "ref" if ref else "selector"
        target = ref if ref else selector
        await self._call("browser_fill", {key: target, "value": text}, on_error="empty")

    async def press_key(self, key: str) -> None:
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
            InteractiveElement(ref=ref, tag=tag, label=label)
            for ref, tag, label in parse_interactive_elements(elements_raw or "")
        )
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