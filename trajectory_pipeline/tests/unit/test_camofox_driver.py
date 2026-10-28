"""camofox 驱动（P1 骨架）的口径测试——**不联网、不开浏览器**。

测的每一件都是「实现看起来能跑、但其实写错了」的形状：

1. **正文三级降级**（innerText → textContent → HTML）与 ``body_source`` 标记。
   写错的后果不是报错，是「正文空」被当成「页面没内容」——假负样本。
2. **camofox 面特有的两档过滤**（模块 docstring 的「四条定位正确性契约」）：
   **没有上游句柄的节点**（点不到）与**名字是更早同 role 名字的子串**
   （上游的 ``.nth()`` 按精确同名计数 ⇒ 落错元素）。这两档漏了，
   症状是「点了一个看起来合理的元素」——**报告上看不出任何异常**。
3. **``press`` 的能力降级**（A2 驱动缓存合并）：上游没有「按键」这个 tool，
   别的键 / 别的目标 ⇒ **能力不可用**，不是「元素不存在」——两者的
   正确处置相反，混起来就是一条理由通顺的假负样本。
4. **分页合并**：上游的快照是 80k 字符的窗口，且**每页都重复同一段尾部**。
   只读第一页 ⇒ 元素清单缺一截；合并错了 ⇒ 真同名在半页上算 ⇒ 点错。
5. **``resolve`` 的四态**（``ok`` / ``not_found`` / ``not_addressable``）：
   「页面上没有」与「页面上有但本面不可寻址」必须分得开。

``snapshot`` 是 P1 存档的唯一必需项（不变式 I4），所以它的降级记账是重点。
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from trajectory_pipeline.executor.browser import camofox_driver
from trajectory_pipeline.executor.browser.camofox_driver import (
    DEGRADED_NAV_UNSETTLED,
    DEGRADED_PRESS_UNSUPPORTED,
    DOC_STAMP_EXPR,
    DOC_STAMP_NONE,
    CamofoxDriver,
)
from trajectory_pipeline.executor.browser.camofox_session import CamofoxToolError
from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    DriverSessionError,
    DriverUnreachableError,
)
from trajectory_pipeline.executor.dom import TEXT_CONTENT_EXPR


def _tab_gone_error() -> CamofoxToolError:
    """上游说「这一页没了」的真身（2026-10-27 实测撞到的那一条）。

    上游 ``server.js`` 的 ``tabNotFoundResponse`` 对不认识的 ``tabId``
    只给 ``404 {"error":"Tab not found"}``——**没有** ``code`` 字段。
    """
    return CamofoxToolError(
        'camofox tool camofox_navigate 返回错误: camofox error: 404: '
        '{"error":"Tab not found"}',
        tool="camofox_navigate",
        status=404,
        body={"error": "Tab not found"},
    )


# ── 假会话 ──────────────────────────────────────────────────────


def _is_bare_function_literal(expression: str) -> bool:
    """这段 JS 文本是不是一个**没被调用**的函数字面量。

    ## 为什么要有这个判据

    上游把表达式交给 Playwright 的 ``page.evaluate(expression)``
    （上游 ``server.js:5511``），而 **Playwright 收到字符串时按「表达式」
    求值** ⇒ 表达式必须**求得出一个值**。

    函数字面量（``() => …`` / ``function …``）求出来是**函数对象**，
    序列化后是 ``undefined`` ⇒ 响应里**没有** ``result`` 键
    ⇒ 本层读到的是 ``None`` ⇒ **「没测到」**。

    ⚠️ 2026-10-27 那批 10 条（camofox 面）就是这么夭折的：文档代号写成
    ``() => …`` ⇒ **每次观察都被判成「读到一半」** ⇒ ``degraded`` 非空
    ⇒ 环节① 返回 **0 候选** ⇒ 10 条全在第一步夭折，**且零报错**。

    ## 这不是 JS 引擎

    本函数**只判形状**（是不是函数字面量），不执行 JS。
    它复刻的是上游那条**语义规则**，用来挡住「把表达式写成函数字面量」
    这一类缺陷——**不是**用来算表达式的值。
    """
    text = expression.strip()
    if text.startswith("async "):
        text = text[len("async "):].lstrip()
    if text.startswith("function"):
        return not text.endswith("()")
    # 箭头函数：``(a, b) => …`` / ``a => …``
    if re.match(r"^\(\s*[^()]*\s*\)\s*=>", text):
        return not text.endswith("()")
    if re.match(r"^[A-Za-z_$][\w$]*\s*=>", text):
        return not text.endswith("()")
    return False


class _FakeSession:
    """假 camofox 会话。

    **同形要求**：本驱动只经由会话的**方法**与上游对话（不直接碰 playwright），
    所以假会话必须提供**同名同义**的那几个方法——否则测的就不是生产链
    的调用路径了（判据⑥ 的教训：探针另写一份口径，读数就不是生产链的读数）。
    """

    def __init__(
        self,
        *,
        snapshot_pages: list[Any] | None = None,
        evals: dict[str, Any] | None = None,
        fail_snapshot: bool = False,
    ) -> None:
        #: 上游分页快照的页（**一项一页**；字符串会被包成单页载荷）。
        self.snapshot_pages = list(snapshot_pages or [])
        #: 表达式 → 返回值。可调用值会被调用（模拟「读到一半」）。
        self.evals: dict[str, Any] = dict(evals or {})
        self.fail_snapshot = fail_snapshot
        #: 标签页栈（上游的 ``tabId``）。**一上来就有一页**——
        #: 驱动每次动作都要当前页，栈空着的话「没有当前页」会盖掉被测的那件事。
        self.stack: list[str] = ["tab-1"]
        #: 动作流水。
        self.clicks: list[tuple[str, str]] = []
        self.types: list[tuple[str, str, str, bool]] = []
        self.navigations: list[tuple[str, str]] = []
        self.closed = 0
        self._tab_seq = 1
        self._snapshot_call = 0
        #: 文档代号（每文档一个常量）。
        self.stamp = "1000"
        #: 代号**读不到**（= 执行上下文被销毁 ⇒ 正在换文档）。
        self.fail_stamp = False
        #: 页面**没有** ``performance`` ⇒ 代号恒为 ``"none"``。
        self.no_performance = False
        #: 导航时报几次「这一页没了」（上游销毁了）。0 = 不报。
        self.tab_gone = 0
        #: 建页时报「这一页没了」——用来模拟「会话真的死了」。
        self.new_page_tab_gone = False
        #: 建页失败（**任何**形状）——模拟上游那个「建页 + 导航」合并路由
        #: 的超时（2026-10-27 实测撞到的那一条）。
        self.new_page_error: BaseException | None = None
        #: 导航失败（**不是**「页没了」）——模拟站点超时 / DNS 不通。
        self.navigate_error: BaseException | None = None
        #: 每次建页**带的 url**（空串 = 建空白页）。上游的建页路由是
        #: ``if (url)`` ⇒ **不带 url 就只建页不导航**（见
        #: ``_open_blank_and_navigate``）——假会话必须同形，否则测的
        #: 就不是生产链的调用路径。
        self.creates: list[str] = []

    # ── 读数 ──

    @property
    def fingerprint(self) -> dict[str, Any]:
        return {"engine": "camoufox", "base_url": "http://localhost:9377"}

    @property
    def pages(self) -> tuple[str, ...]:
        return tuple(self.stack)

    @property
    def current(self) -> str:
        if not self.stack:
            raise DriverError("camofox 会话里没有打开的标签页")
        return self.stack[-1]

    async def measure_fingerprint(self) -> dict[str, Any]:
        return dict(self.fingerprint)

    # ── tool 调用 ──

    async def new_page(self, url: str = "") -> str:
        if self.new_page_tab_gone:
            raise _tab_gone_error()
        if self.new_page_error is not None:
            raise self.new_page_error
        self._tab_seq += 1
        tab = f"tab-{self._tab_seq}"
        self.stack.append(tab)
        self.creates.append(url)
        if url:
            # 上游的建页路由**带 url 时顺带导航**（上游 ``server.js`` 的
            # ``if (url)``）⇒ **不带 url 就只建页**——生产链走的正是后者。
            self.navigations.append((tab, url))
        return tab

    async def navigate(self, tab_id: str, url: str) -> Any:
        self.navigations.append((tab_id, url))
        if self.tab_gone:
            self.tab_gone -= 1
            raise _tab_gone_error()
        if self.navigate_error is not None:
            raise self.navigate_error
        self._navigate()
        return {"ok": True}

    async def close_page(self, tab_id: str) -> bool:
        if tab_id in self.stack:
            self.stack.remove(tab_id)
        return True

    def discard_page(self, tab_id: str) -> bool:
        """把一页从**本地栈**里摘掉——**不通知上游**（同生产语义）。"""
        if tab_id in self.stack:
            self.stack.remove(tab_id)
            return True
        return False

    async def close(self) -> None:
        self.closed += 1

    async def snapshot(self, tab_id: str, offset: int | None = None) -> Any:
        if self.fail_snapshot:
            raise DriverError("camofox tool camofox_snapshot 调用失败: 页面没了")
        pages = self.snapshot_pages or [""]
        index = min(self._snapshot_call, len(pages) - 1)
        self._snapshot_call += 1
        item = pages[index]
        if isinstance(item, str):
            return {
                "url": "https://example.com/watch",
                "snapshot": item,
                "refsCount": 0,
                "truncated": False,
                "totalChars": len(item),
            }
        return dict(item)

    async def click(self, tab_id: str, ref: str) -> Any:
        self.clicks.append((tab_id, ref))
        self._navigate()
        return {"ok": True}

    async def type_text(
        self, tab_id: str, ref: str, text: str, *, press_enter: bool = False
    ) -> Any:
        self.types.append((tab_id, ref, text, press_enter))
        self._navigate()
        return {"ok": True}

    async def evaluate(self, tab_id: str, expression: str) -> Any:
        if _is_bare_function_literal(expression):
            # 上游的语义：**函数字面量**求值 ⇒ 函数对象 ⇒ 序列化后
            # ``undefined`` ⇒ 响应里**没有** ``result`` 键
            # ⇒ 本层读到的是 ``None``（「没测到」，不是「空串」）。
            # ⚠️ 这一条**不是**假会话的宽容——它复刻的是上游
            # ``page.evaluate(string)`` 的真实语义。
            return None
        if expression in self.evals:
            value = self.evals[expression]
        elif expression == DOC_STAMP_EXPR:
            if self.fail_stamp:
                raise DriverError("执行上下文被销毁")
            value = DOC_STAMP_NONE if self.no_performance else self.stamp
        elif expression == camofox_driver._READY_STATE_JS:
            value = "complete"
        elif "querySelectorAll" in expression:
            # 计数式（``try{return String(…querySelectorAll(…).length)``）
            # 与抽取式（``out.push(…)``）——两者都含 querySelectorAll，
            # 靠**计数式的固定前缀**分开。
            value = "0" if "try{return String(" in expression else "[]"
        else:
            raise DriverError(f"fake: 表达式未配置 {expression!r}")
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        return value

    # ── 内部 ──

    def _navigate(self) -> None:
        """模拟一次**导航 commit**：换文档 ⇒ 文档代号变。"""
        self.stamp = str(int(self.stamp) + 1)


def _driver(session: _FakeSession) -> CamofoxDriver:
    return CamofoxDriver(session)          # type: ignore[arg-type]


def _page_json(**kw: Any) -> str:
    """构造 ``_PAGE_TEXT_JS`` 的返回值（URL / 标题 / 正文一次读全）。"""
    payload = {
        "href": "https://example.com/watch",
        "title": "示例影片 在线观看",
        "innerText": "",
    }
    payload.update(kw)
    return json.dumps(payload, ensure_ascii=False)


#: 一段真实形态的**注记**快照（camofox 的行尾带 ``[eN]``）。
ANNOTATED_SAMPLE = """\
- generic [active]:
  - button "立即播放" [e1]
  - link "在线观看" [e2]
  - textbox "站内搜索" [e3]
  - textbox
  - button "详情" [e4]
  - button "详情" [e5]
  - heading "影片介绍" [e6]
"""


# ── 快照 ────────────────────────────────────────────────────────


class TestSnapshot:
    async def test_一次读全_url_title_正文(self):
        session = _FakeSession(evals={camofox_driver._PAGE_TEXT_JS:
                                      _page_json(innerText="第一行\n\n\n第二行  ")})
        driver = _driver(session)
        snap = await driver.snapshot(max_chars=100)
        assert snap.url == "https://example.com/watch"
        assert snap.title == "示例影片 在线观看"
        assert snap.body == "第一行\n\n第二行"      # 空白压到最多一个空行
        assert snap.body_source == "inner_text"
        assert snap.stripped_ratio == 1.0
        assert snap.truncated is False

    async def test_innerText_空走_textContent_且不记降级(self):
        """走了备用路 ≠ 没采到。降级标记只在**三路都空**时记。"""
        session = _FakeSession(evals={
            camofox_driver._PAGE_TEXT_JS: _page_json(),
            TEXT_CONTENT_EXPR: "从 textContent 拿到的正文",
        })
        driver = _driver(session)
        degraded: list[str] = []
        snap = await driver.snapshot(max_chars=100, degraded=degraded)
        assert snap.body == "从 textContent 拿到的正文"
        assert snap.body_source == "inner_text"     # 仍是文本源，不是 HTML
        assert degraded == []

    async def test_两路都空走_HTML_兜底并标_snapshot(self):
        """HTML 兜底是**更弱的证据**：body_source 必须如实标出来。"""
        session = _FakeSession(evals={
            camofox_driver._PAGE_TEXT_JS: _page_json(),
            TEXT_CONTENT_EXPR: "",
            camofox_driver._HTML_JS: "<html><head><style>.a{color:red}</style></head>"
                                     "<body><div>正文在 HTML 里</div></body></html>",
        })
        driver = _driver(session)
        degraded: list[str] = []
        snap = await driver.snapshot(max_chars=1000, degraded=degraded)
        assert snap.body_source == "snapshot"
        assert "正文在 HTML 里" in snap.body
        assert "color:red" not in snap.body
        assert snap.stripped_ratio < 1.0
        assert degraded == []

    async def test_三路都空才记_body_text(self):
        session = _FakeSession(evals={
            camofox_driver._PAGE_TEXT_JS: _page_json(),
            TEXT_CONTENT_EXPR: "",
            camofox_driver._HTML_JS: "<html><body></body></html>",
        })
        driver = _driver(session)
        degraded: list[str] = []
        snap = await driver.snapshot(max_chars=1000, degraded=degraded)
        assert snap.body == ""
        assert degraded == ["body_text"]

    async def test_乱码分别记正文与标题(self):
        bad = "�" * 3
        session = _FakeSession(evals={
            camofox_driver._PAGE_TEXT_JS: _page_json(
                title=f"示例影片{bad}", innerText=f"正文{bad}"),
        })
        driver = _driver(session)
        degraded: list[str] = []
        await driver.snapshot(max_chars=1000, degraded=degraded)
        assert "body_mojibake" in degraded
        assert "title_mojibake" in degraded

    async def test_取快照失败抛_DriverError_不返回空快照(self):
        """snapshot 是 fail-closed 的锚点（I4）：不能降级成空快照，
        否则下层会把它读成「页面真的什么都没有」。"""
        session = _FakeSession(evals={
            camofox_driver._PAGE_TEXT_JS: DriverError("页面没了"),
        })
        driver = _driver(session)
        with pytest.raises(DriverError):
            await driver.snapshot(max_chars=100)


# ── 元素枚举：camofox 面的两档特有过滤 ──────────────────────────


class TestInteractive:
    async def test_按_role_name_给元素_句柄不出动作面(self):
        """``eN`` 是**上游的会话句柄**——只在本驱动内部消化。

        训练数据里出去的是 ``{role, name}``；``eN`` 进了训练数据等于让
        模型学一个随机数，且样本永远不可回放（CLAUDE.md 红线）。
        """
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        elements = await driver.interactive()
        assert [(e.ref, e.role, e.name) for e in elements] == [
            ("e1", "button", "立即播放"),
            ("e2", "link", "在线观看"),
            ("e3", "textbox", "站内搜索"),
        ]
        # 我们的 ``e1`` ↔ 上游的 ``e1``（本例恰好同号）——
        # **两套 id 各自独立**，靠驱动的两张内部表对上。
        assert driver.refs["e2"] == ("link", "在线观看")

    async def test_四类丢弃都记账(self):
        """账目是空列表的自证：「页面上一个可点目标都没有」与「全被过滤了」
        在观察里同形，只有这里分得开。

        ⚠️ **「同名整组丢」丢的是两个节点**（不是一个）——「详情」出现两次，
        两个都不可寻址（留哪个是赌）。所以 ``ambiguous_dropped`` 是 **2**。
        """
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.interactive()
        assert driver.last_enumeration == {
            "nodes": 8,               # 8 行注记（含 generic 容器行）
            "annotated": 6,           # 其中 6 行带上游句柄
            "role_matched": 6,        # role 命中词表
            "role_unmatched": 2,      # generic / heading
            "nameless_dropped": 1,    # 那个没有名字的 textbox
            "ambiguous_dropped": 2,   # 真同名「详情」——**两个节点都丢**
            "unref_dropped": 0,
            "name_ambiguous": 0,
            "kept": 3,
            "truncated": 0,
        }

    async def test_没有上游句柄的节点剔掉并记账(self):
        """上游对一部分节点**不分配句柄**（名字命中 datepicker 之类、
        节点数超限、或该行不在交互 role 里）⇒ 那些节点**点不到**。

        留在动作面里就是给模型一个假的可点目标——点它**不报错、也不动**，
        症状是「点了但页面没变化」，而报告上看不出异常。
        """
        sample = (
            '- button "立即播放" [e1]\n'
            '- link "更多资讯"\n'          # ← 没有句柄
            '- link "查看详情" [e2]\n'
        )
        session = _FakeSession(snapshot_pages=[sample])
        driver = _driver(session)
        elements = await driver.interactive()
        assert [(e.role, e.name) for e in elements] == [
            ("button", "立即播放"),
            ("link", "查看详情"),
        ]
        assert driver.last_enumeration["unref_dropped"] == 1

    async def test_名字是更早同role名字的子串时剔掉(self):
        """上游的定位是 ``getByRole(role, {name}).nth(nth)``——**没有 exact**
        ⇒ 名字走**子串匹配**；而 ``nth`` 按**精确同名**计数。

        「详情」与「查看详情」同 role 时：请求「详情」会同时命中两个，
        ``.nth(0)`` 落到**先出现的那个** ⇒ 点错，且报告上看不出异常。
        ⇒ 本面在枚举期就把「本名被更早的同 role 名字包含」的节点剔掉。
        """
        sample = (
            '- link "查看详情" [e1]\n'
            '- link "详情" [e2]\n'          # ← 「详情」是「查看详情」的子串
        )
        session = _FakeSession(snapshot_pages=[sample])
        driver = _driver(session)
        elements = await driver.interactive()
        assert [(e.role, e.name) for e in elements] == [("link", "查看详情")]
        assert driver.last_enumeration["name_ambiguous"] == 1

    async def test_反向的子串不算歧义(self):
        """「查看详情」不是「详情」的子串 ⇒ 两者**都**可寻址。

        判据的方向不能搞反：搞反了会把大半页面剔光（清单缺一截 ⇒
        模型看不到页面上大部分可点目标）。
        """
        sample = (
            '- link "详情" [e1]\n'
            '- link "查看详情" [e2]\n'
        )
        session = _FakeSession(snapshot_pages=[sample])
        driver = _driver(session)
        elements = await driver.interactive()
        assert [(e.role, e.name) for e in elements] == [
            ("link", "详情"), ("link", "查看详情"),
        ]
        assert driver.last_enumeration["name_ambiguous"] == 0

    async def test_导航清空_ref_表(self):
        """不清的话，上个页面残留的 ``e2`` 会指向本页一个**合法但不同**的元素
        ——点错不可逆，而且看起来完全正常。"""
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        assert driver.refs
        await driver.goto("https://example.com/next")
        assert driver.refs == {}

    async def test_limit_截断并记账(self):
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        elements = await driver.interactive(limit=2)
        assert [e.ref for e in elements] == ["e1", "e2"]
        assert driver.last_enumeration["truncated"] == 1

    async def test_取快照失败抛错不返回空列表(self):
        """空列表会把一次**能力故障**写成**页面事实**，两者正确处置相反。"""
        session = _FakeSession(fail_snapshot=True)
        driver = _driver(session)
        with pytest.raises(DriverError):
            await driver.interactive()


# ── 分页合并 ────────────────────────────────────────────────────


class Test分页合并:
    """上游的快照是 80k 字符的窗口，且**每页都重复同一段 5k 尾部**。

    只读第一页 ⇒ 元素清单缺一截；合并错了 ⇒ 「真同名」是在半页上算的
    ⇒ 页面上另一半的同名元素会被漏判成「唯一」⇒ **点错**。
    """

    FULL = (
        '- button "立即播放" [e1]\n'
        '- link "在线观看" [e2]\n'
        '- textbox "站内搜索" [e3]\n'
    )
    L1 = '- button "立即播放" [e1]\n'
    L2 = '- link "在线观看" [e2]\n'
    L3 = '- textbox "站内搜索" [e3]\n'

    @staticmethod
    def _page(head: str, tail: str, *, has_more: bool, total: int,
              next_offset: int = 100) -> dict[str, Any]:
        """造一页上游载荷：``头 + 截断标记行 + 尾``。

        标记格式照上游 ``lib/snapshot.js``：``[... truncated at char N]``。
        """
        text = head
        if tail:
            text = head + "[... truncated at char 999]\n" + tail
        payload: dict[str, Any] = {
            "url": "https://example.com/watch",
            "snapshot": text,
            "refsCount": 0,
            "truncated": has_more,
            "totalChars": total,
        }
        if has_more:
            payload["hasMore"] = True
            payload["nextOffset"] = next_offset
        return payload

    async def test_两页合并出完整清单(self):
        total = len(self.FULL)
        page1 = self._page(self.L1, self.L3, has_more=True, total=total)
        page2 = self._page(self.L2, "", has_more=False, total=total)

        session = _FakeSession(snapshot_pages=[page1, page2])
        driver = _driver(session)
        elements = await driver.interactive()
        assert [(e.role, e.name) for e in elements] == [
            ("button", "立即播放"),
            ("link", "在线观看"),
            ("textbox", "站内搜索"),
        ]

    async def test_末页头部盖住尾部时只留一份尾部(self):
        """末页的头部可能**已经包含**那段尾部 ⇒ 合并时不能留两份。

        留两份 ⇒ 同一个节点出现两次 ⇒ 被判成「真同名」⇒ 整组丢掉。
        """
        total = len(self.FULL)
        page1 = self._page(self.L1, self.L3, has_more=True, total=total)
        page2 = self._page(self.L2 + self.L3, "", has_more=False, total=total)

        session = _FakeSession(snapshot_pages=[page1, page2])
        driver = _driver(session)
        elements = await driver.interactive()
        assert [(e.role, e.name) for e in elements] == [
            ("button", "立即播放"),
            ("link", "在线观看"),
            ("textbox", "站内搜索"),
        ]

    async def test_页边界切在行中间时那一行不丢(self):
        """页与页的边界可能切在行中间 ⇒ 把上一块末尾的**不完整行**
        挪到下一块前面，那一行才不会丢。"""
        total = len(self.FULL)
        head1 = self.FULL[:10]              # ← 切在第一行中间
        page1 = self._page(head1, self.L3, has_more=True, total=total)
        page2 = self._page(self.FULL[10:], "", has_more=False, total=total)

        session = _FakeSession(snapshot_pages=[page1, page2])
        driver = _driver(session)
        elements = await driver.interactive()
        assert [(e.role, e.name) for e in elements] == [
            ("button", "立即播放"),
            ("link", "在线观看"),
            ("textbox", "站内搜索"),
        ]

    async def test_分页期间换文档就记_不可信(self):
        """分页读取跨了两个文档 ⇒ 这份清单**不是任何一个页面的**清单。

        动作面拿着它去点 ⇒ 点到的是另一个文档上的元素。
        """
        total = len(self.FULL)
        page1 = self._page(self.L1, self.L3, has_more=True, total=total)
        page2 = self._page(self.L2, "", has_more=False, total=total)

        session = _FakeSession(snapshot_pages=[page1, page2])
        driver = _driver(session)
        # 读第一页之后换文档：代号在分页前后变了。
        original = session.snapshot

        async def _snapshot(tab_id: str, offset: int | None = None) -> Any:
            payload = await original(tab_id, offset)
            if offset is None:
                session._navigate()
            return payload

        session.snapshot = _snapshot      # type: ignore[method-assign]
        await driver.interactive()
        assert driver.last_enumeration.get("nav_unsettled") == 1


# ── 动作面 ──────────────────────────────────────────────────────


class TestActionFace:
    async def test_click_按_role_name_找到上游句柄(self):
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        await driver.click(role="button", name="立即播放")
        assert session.clicks == [("tab-1", "e1")]
        assert driver.last_action["outcome"] == "ok"

    async def test_click_元素不在清单上时静默返回并记账(self):
        """0 命中 = 业务事实（元素不在页面上），**不抛**——抛了会让整站
        变成 ``unreachable_hard`` 那种理由通顺的负样本。但动作返回 None，
        于是「点成功了」与「没点」同形，所以账目必须记。"""
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        await driver.click(role="button", name="不存在的按钮")
        assert session.clicks == []
        assert driver.last_action["outcome"] == "not_found"

    async def test_词表外的_role_不往返且判不可寻址(self):
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        await driver.click(role="heading", name="影片介绍")
        assert session.clicks == []
        assert driver.last_action["outcome"] == "not_addressable"
        assert driver.last_action["reason"] == "role"

    async def test_被本面剔掉的元素判_不可寻址_不是_不存在(self):
        """「页面上有」与「页面上没有」**必须分得开**——两者的正确处置相反
        （一个是本面设计上的取舍，一个是页面事实）。混起来会让可移植性的
        读数指错对象（把「面剔掉了」读成「名字脆」）。"""
        sample = '- link "查看详情" [e1]\n- link "详情" [e2]\n'
        session = _FakeSession(snapshot_pages=[sample])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        await driver.click(role="link", name="详情")
        assert session.clicks == []
        assert driver.last_action["outcome"] == "not_addressable"
        assert driver.last_action["reason"] == "name_ambiguous"

    async def test_跨面参数炸穿而不是变成负样本(self):
        """拿 obscura 面的词汇（``ref``）调 camofox 的动作面 = 接线错误。

        抛 ``ValueError`` 而不是 ``DriverError``：后者会被 ``steps/`` 捕获成
        ``unreachable_hard``，于是接线 bug 变成一条**理由通顺的负样本**。
        """
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        with pytest.raises(ValueError):
            await driver.click(ref="e1")
        assert session.clicks == []

    async def test_fill_是设值并缓存文本(self):
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        await driver.fill(role="textbox", name="站内搜索", text="功夫")
        assert session.types == [("tab-1", "e3", "功夫", False)]


# ── press：A2 驱动缓存合并 ──────────────────────────────────────


class TestPress:
    async def test_回车重发同文本并附带回车(self):
        """上游没有「按键」这个 tool——``type(..., pressEnter=true)`` 是
        唯一能提交表单的路径 ⇒ 缓存上一次 ``fill`` 的文本并重发。

        ⚠️ **重发是安全的**：上游的 ``type`` 默认 ``mode='fill'`` ⇒
        ``fill(text)`` 是**设值**（先清空再写），不是追加。
        """
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        await driver.fill(role="textbox", name="站内搜索", text="功夫")
        await driver.press("Enter")
        assert session.types[-1] == ("tab-1", "e3", "功夫", True)
        assert driver.last_action["outcome"] == "ok"

    async def test_别的键是能力降级不是元素不存在(self):
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        await driver.press("Escape")
        assert session.types == []
        assert driver.last_action["outcome"] == "unsupported"
        assert driver.last_action["reason"] == "key:Escape"

    async def test_没填过就按回车是能力降级(self):
        """没有可重发的文本 ⇒ **不猜**——降级并说清原因。"""
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        await driver.press("Enter")
        assert session.types == []
        assert driver.last_action["outcome"] == "unsupported"
        assert driver.last_action["reason"] == "no_target"

    async def test_目标变了就降级(self):
        """缓存里的文本**只对同一个目标适用**——目标变了就不适用。"""
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        await driver.fill(role="textbox", name="站内搜索", text="功夫")
        await driver.press("Enter", role="textbox", name="别的框")
        assert len(session.types) == 1          # 只剩那次 fill
        assert driver.last_action["outcome"] == "unsupported"
        assert driver.last_action["reason"] == "no_cached_text"

    async def test_降级会进_degraded_让下游不判(self):
        """能力降级 ⇒ 观察带降级标记 ⇒ 感知层判 ``answer=None``。

        不带标记的话，下游会把「这层没有这个能力」读成「页面上没这个元素」
        ⇒ 一条理由通顺的假负样本。
        """
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE], evals={
            camofox_driver._PAGE_TEXT_JS: _page_json(innerText="正文"),
            CamofoxDriver._LINKS_JS: "[]",
        })
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        await driver.press("Escape")
        obs = await driver.observe()
        assert DEGRADED_PRESS_UNSUPPORTED in obs.degraded


class Test降级不跨页:
    """**页级状态只能活一页**——降级标记尤其。

    一次做不到的按键（按 ``F5``）会记 ``last_action["outcome"]="unsupported"``，
    而 :meth:`observe` 拿它判「这一页有没有降级」。
    ⚠️ **若 ``last_action`` 不随导航作废**，它就一路跟着走——
    **之后每一次导航**之后的观察都挂上 ``press_unsupported``
    ⇒ **假降级**进了 agent 的输入（``agent_loop._render_observation``
    把 ``degraded`` 投影给模型）⇒ agent 以为「这一页没读全」。

    真实批次（2026-10-27 那批 10 条）量到过：**18% 的观察**带着这个假降级
    （T004：39 个观察里 7 个是陈旧的）。
    """

    async def test_上一页的按键降级不会挂到下一页的观察上(self):
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE], evals={
            camofox_driver._PAGE_TEXT_JS: _page_json(innerText="正文"),
            CamofoxDriver._LINKS_JS: "[]",
        })
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.press("F5")
        obs1 = await driver.observe()
        assert DEGRADED_PRESS_UNSUPPORTED in obs1.degraded

        # **换页** ⇒ 上一页那笔降级作废。
        await driver.goto("https://example.com/other")
        obs2 = await driver.observe()
        assert DEGRADED_PRESS_UNSUPPORTED not in obs2.degraded

    async def test_开新页也不带上一页的降级(self):
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE], evals={
            camofox_driver._PAGE_TEXT_JS: _page_json(innerText="正文"),
            CamofoxDriver._LINKS_JS: "[]",
        })
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.press("F5")
        assert DEGRADED_PRESS_UNSUPPORTED in (await driver.observe()).degraded

        await driver.new_tab("https://example.com/other")
        obs = await driver.observe()
        assert DEGRADED_PRESS_UNSUPPORTED not in obs.degraded


# ── resolve：只定位不动作 ───────────────────────────────────────


class TestResolve:
    async def test_命中一个算_ok_且不动作(self):
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        res = await driver.resolve(role="button", name="立即播放")
        assert res["outcome"] == "ok"
        assert res["matches"] == 1
        assert session.clicks == []          # **没点**——这是这个口存在的理由

    async def test_清单上没有算_not_found(self):
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        res = await driver.resolve(role="button", name="没有这个按钮")
        assert res["outcome"] == "not_found"
        assert res["matches"] == 0

    async def test_被本面剔掉的算_not_addressable_并带原因(self):
        sample = '- link "查看详情" [e1]\n- link "详情" [e2]\n'
        session = _FakeSession(snapshot_pages=[sample])
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        await driver.interactive()
        res = await driver.resolve(role="link", name="详情")
        assert res["outcome"] == "not_addressable"
        assert res["reason"] == "name_ambiguous"

    async def test_没枚举过就查等于_not_found(self):
        """没枚举就没有清单 ⇒ 清单上没有 ⇒ ``not_found``（不猜）。"""
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        res = await driver.resolve(role="button", name="立即播放")
        assert res["outcome"] == "not_found"


# ── 观察：读数自检（读到一半） ──────────────────────────────────


class TestObserve:
    async def test_字段齐全(self):
        session = _FakeSession(
            snapshot_pages=[ANNOTATED_SAMPLE],
            evals={
                camofox_driver._PAGE_TEXT_JS: _page_json(innerText="正文内容"),
                CamofoxDriver._LINKS_JS: json.dumps(
                    [["首页", "https://example.com/"]], ensure_ascii=False),
            },
        )
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        obs = await driver.observe(max_chars=100)
        assert obs.url == "https://example.com/watch"
        assert obs.page_title == "示例影片 在线观看"
        assert obs.body_text == "正文内容"
        assert obs.links_total == 1
        assert obs.elements_total == 3
        assert obs.degraded == ()

    async def test_读数跨了文档就记降级让下游不判(self):
        """导航落在读数中间 ⇒ 那份观察是半成品。

        降级 ⇒ 感知层判 ``answer=None`` ⇒ 落 ``unresolved``，
        **不进负样本池**——半成品绝不能变成业务负样本。
        """
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])

        def _body() -> str:
            session._navigate()          # 每次读正文都换文档
            return _page_json(innerText="正文")

        session.evals[camofox_driver._PAGE_TEXT_JS] = _body
        session.evals[CamofoxDriver._LINKS_JS] = "[]"
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        obs = await driver.observe(max_chars=100)
        assert DEGRADED_NAV_UNSETTLED in obs.degraded

    async def test_页面没有_performance_时不是_读不稳(self):
        """代号恒为 ``"none"``（页面没有 performance）⇒ **不误伤**。"""
        session = _FakeSession(
            snapshot_pages=[ANNOTATED_SAMPLE],
            evals={
                camofox_driver._PAGE_TEXT_JS: _page_json(innerText="正文"),
                CamofoxDriver._LINKS_JS: "[]",
            },
        )
        session.no_performance = True
        driver = _driver(session)
        await driver.goto("https://example.com/watch")
        obs = await driver.observe(max_chars=100)
        assert DEGRADED_NAV_UNSETTLED not in obs.degraded

    async def test_文档代号表达式求得出值(self):
        """**回归**：文档代号曾经写成 ``() => …`` ⇒ 整批 0 候选（2026-10-27）。

        上游是 ``page.evaluate(expression)``（**字符串** ⇒ 按**表达式**求值）
        ⇒ 函数字面量求出来是函数对象 ⇒ 序列化后 ``undefined``
        ⇒ 响应里**没有** ``result`` 键 ⇒ 代号恒为空串
        ⇒ **每次观察都被判成「读到一半」** ⇒ ``degraded`` 非空
        ⇒ 环节① 返回 0 候选 ⇒ 10 条任务全在第一步夭折，**且零报错**。

        这条断言盯的是**形状**（不是函数字面量），
        假会话里那条上游语义规则（``_is_bare_function_literal``）是它的执行者。
        """
        assert not _is_bare_function_literal(DOC_STAMP_EXPR), (
            "文档代号表达式写成了函数字面量——上游按表达式求值，"
            "函数字面量序列化后是 undefined ⇒ 代号恒为空串 ⇒ 整批 0 候选"
        )
        # 假会话按上游语义求值 ⇒ 代号必须真的读得回来。
        session = _FakeSession(snapshot_pages=[ANNOTATED_SAMPLE])
        driver = _driver(session)
        assert await driver._doc_stamp() == "1000"

    async def test_发给上游的表达式都不是函数字面量(self):
        """**同一条规则的通用版**：本驱动送上游的每一段 JS 都要求得出值。

        一次只错一段就够了（2026-10-27 就是错了一段），
        所以这里盯的是**全集**，不是逐个用例。
        """
        expressions = {
            "DOC_STAMP_EXPR": DOC_STAMP_EXPR,
            "_PAGE_TEXT_JS": camofox_driver._PAGE_TEXT_JS,
            "_HTML_JS": camofox_driver._HTML_JS,
            "_READY_STATE_JS": camofox_driver._READY_STATE_JS,
            "_LINKS_JS": CamofoxDriver._LINKS_JS,
            "TEXT_CONTENT_EXPR": TEXT_CONTENT_EXPR,
        }
        for name, expression in expressions.items():
            assert not _is_bare_function_literal(expression), (
                f"{name} 是函数字面量——上游按表达式求值，求不出值"
            )
        # 计数 / 抽取是**生成**的 JS，一并盯住。
        generated = CamofoxDriver._pluck_js("video, audio", None)
        assert not _is_bare_function_literal(generated)


# ── 链接与抽取 ──────────────────────────────────────────────────


class TestLinks与Extract:
    async def test_按_href_去重保留第一条(self):
        session = _FakeSession(evals={
            CamofoxDriver._LINKS_JS: json.dumps([
                ["第一条", "https://example.com/a"],
                ["重复", "https://example.com/a"],
                ["第二条", "https://example.com/b"],
            ], ensure_ascii=False),
        })
        driver = _driver(session)
        links = await driver.links()
        assert [(l.text, l.href) for l in links] == [
            ("第一条", "https://example.com/a"),
            ("第二条", "https://example.com/b"),
        ]

    async def test_取链接失败抛_DriverError_不返回空(self):
        """空元组 = 「页面上没有链接」，那是页面事实。"""
        session = _FakeSession(evals={
            CamofoxDriver._LINKS_JS: DriverError("炸了"),
        })
        driver = _driver(session)
        with pytest.raises(DriverError):
            await driver.links()

    async def test_标量抽不到不写键_数组照写空数组(self):
        session = _FakeSession()
        driver = _driver(session)
        out = await driver.extract({"title": "missing", "titles[]": "missing"})
        assert "title" not in out
        assert out["titles"] == []


# ── 标签页 ──────────────────────────────────────────────────────


class TestTabs:
    async def test_new_tab_压栈并切成当前页(self):
        session = _FakeSession()
        driver = _driver(session)
        tab = await driver.new_tab("https://example.com/ext")
        assert tab == "c1"                    # **本驱动发的**句柄
        assert session.pages == ("tab-1", "tab-2")   # 上游的 tabId
        assert driver.last_tab["outcome"] == "ok"

    async def test_两套_id_不可互换(self):
        """驱动的 ``c1`` 与上游的 ``tab-1`` 是**两套**——混用会关错页。"""
        session = _FakeSession()
        driver = _driver(session)
        tab = await driver.new_tab("https://example.com/ext")
        assert tab != session.current
        assert driver.last_tab["tab"] == "c1"

    async def test_close_tab_不认识的_id_抛而不是猜一个关(self):
        session = _FakeSession()
        driver = _driver(session)
        with pytest.raises(DriverError):
            await driver.close_tab("c99")


# ── 指纹时机 ──────────────────────────────────────────────────────


class _懒测指纹会话(_FakeSession):
    """**懒测指纹**的会话：只有 ``measure_fingerprint`` 跑过才有读数。

    真会话就是这个形状——**第一次建页**时（也就是第一次 ``goto`` 里）
    才测得到身份，之前读到的必然是空 dict。
    """

    def __init__(self) -> None:
        super().__init__()
        self._fp: dict[str, Any] = {}
        self.measured = 0

    @property
    def fingerprint(self) -> dict[str, Any]:
        return dict(self._fp)

    async def measure_fingerprint(self) -> dict[str, Any]:
        # 真会话**命中缓存就返回**（身份只测一次）⇒ 这里照抄那个形状，
        # ``measured`` 记的是**真测了几次**，不是**被叫了几次**。
        if not self._fp:
            self.measured += 1
            self._fp = {"engine": "camoufox", "base_url": "http://localhost:9377"}
        return dict(self._fp)


class Test指纹时机:
    async def test_指纹在第一次导航之后才有(self):
        """**指纹是懒测的**——第一次 ``goto`` 里第一次建页时才测得到。

        钉的是「**什么时候才有**」：编排器在**导航之前**构造存档记录，
        那一刻读到的必然是空 dict ⇒ 补读必须紧跟第一次导航
        （见 ``test_visit.py::Test指纹补读``）。

        ⚠️ 空 dict = **没测到**，不是「测出来是空的」——两者在批次元数据里
        必须分得开（读档人会把「没记到」读成「指纹为空」）。
        """
        session = _懒测指纹会话()
        driver = _driver(session)
        assert driver.fingerprint == {}, "测之前就应当是空的"
        await driver.goto("https://example.com/")
        assert session.measured == 1
        assert driver.fingerprint["engine"] == "camoufox"

    async def test_指纹只测一次_后来导航不重测(self):
        """身份在浏览器起来那一刻就定了 ⇒ **测一次就够**。

        每次导航都重测会把「这一批的身份」换成「最后一个页面的身份」，
        而后者在跨批次对读时是**没有意义的**。
        """
        session = _懒测指纹会话()
        driver = _driver(session)
        await driver.goto("https://example.com/a")
        await driver.goto("https://example.com/b")
        assert session.measured == 1



class Test换页自愈:
    """**上游把当前页销毁了 ⇒ 驱动换一张新页，同一个 URL 重来一次。**

    ⚠️ **为什么这是必须修的**（2026-10-27 实测，10 条 camofox 批）：
    上游 camofox-browser 在**操作超时 / 连续超时 / 页崩溃 / 锁队列超时**时
    **销毁那一页**，而**销毁不通知客户端** ⇒ 客户端手里的 ``tabId``
    成了死引用 ⇒ 再导航就是 ``404 {"error":"Tab not found"}``，**每一次都 404**。

    实测：一个候选**连炸 9 次**，9 次全被记成 ``unreachable_hard``
    （"站点打不开"）⇒ **九条理由通顺的假负样本进了池**，
    而那个站点**一次都没被试过**。
    """

    async def test_页没了就换新页重来(self):
        session = _FakeSession()
        session.tab_gone = 1
        driver = _driver(session)
        await driver.goto("https://example.com/a")
        # 死页被摘掉、栈顶换成了新页、同一个 URL 重新导航过一次
        assert session.stack == ["tab-2"]
        assert session.navigations == [
            ("tab-1", "https://example.com/a"),
            ("tab-2", "https://example.com/a"),
        ]

    async def test_自愈要记账_不许静默(self):
        """自愈之后那一步在动作流里是**成功**的——不记就看不出
        「这一页死过一次」，而那正是读档人判断结论可不可信的依据。"""
        session = _FakeSession()
        session.tab_gone = 1
        driver = _driver(session)
        await driver.goto("https://example.com/a")
        assert len(driver.recoveries) == 1
        assert driver.recoveries[0]["url"] == "https://example.com/a"
        assert driver.recoveries[0]["tab"] == "tab-1"

    async def test_没有自愈时账本是空的(self):
        session = _FakeSession()
        driver = _driver(session)
        await driver.goto("https://example.com/a")
        assert driver.recoveries == []

    async def test_普通的导航失败不换页(self):
        """**站点超时不是「页没了」**——换页重来会多打站点一次，
        而且掩盖了「这个站真的打不开」这个事实。"""
        session = _FakeSession()
        session.navigate_error = DriverError(
            "camofox tool camofox_navigate 返回错误: camofox error: 500: "
            '{"error":"page.goto: Timeout 30000ms exceeded"}'
        )
        driver = _driver(session)
        with pytest.raises(DriverError):
            await driver.goto("https://example.com/a")
        assert session.stack == ["tab-1"], "不该换页"
        assert driver.recoveries == []

    async def test_换页也失败就是会话失效(self):
        """连「开一张新页」都开不出来 ⇒ 会话真的死了。

        ⇒ 抛 :class:`DriverSessionError`（**执行层坏了**），
        不是普通 ``DriverError``（那是「站点打不开」）——
        上层要靠这个类型分开记账。
        """
        session = _FakeSession()
        session.tab_gone = 1
        session.new_page_tab_gone = True
        driver = _driver(session)
        with pytest.raises(DriverSessionError):
            await driver.goto("https://example.com/a")

    async def test_会话失效不许被包成普通导航失败(self):
        """``goto`` 会把普通 ``DriverError`` 包成「camofox 导航失败: …」，
        但**会话失效不许被包掉**——上层靠类型分账。"""
        session = _FakeSession()
        session.tab_gone = 1
        session.new_page_tab_gone = True
        driver = _driver(session)
        with pytest.raises(DriverSessionError) as caught:
            await driver.goto("https://example.com/a")
        assert "会话失效" in str(caught.value)


# ── 归因：失败落在哪个路由 ──────────────────────────────────────


def _session_expired_error() -> CamofoxToolError:
    """上游明说「会话过期，重来即可」（``recovery="retry"``）的真身。

    上游 ``lib/browser-errors.js`` 的 ``browserErrorRecovery``：
    ``session_expired`` ⇒ ``recovery="retry"``；``browserErrorStatus``
    把它映成 HTTP 503。
    """
    return CamofoxToolError(
        'camofox tool camofox_navigate 返回错误: camofox error: 503: '
        '{"error":"Browser session expired","code":"session_expired",'
        '"retryable":true,"recovery":"retry"}',
        tool="camofox_navigate",
        status=503,
        body={
            "error": "Browser session expired",
            "code": "session_expired",
            "retryable": True,
            "recovery": "retry",
        },
    )


class Test归因:
    """**失败落在哪个路由 ⇒ 那就是归属判据**（2026-10-27 实测后改）。

    ⚠️ **为什么必须修**（T048 那条假负样本的成因）：上游的建页路由
    把「建页」与「导航」塞进**同一个 30 秒预算** ⇒ 那个路由失败
    **证明不了站点打不开**；而实测两种失败**在错误体里同形**
    （都是 HTTP 500 + ``{"error":…,"retryable":false}``、**没有**
    ``code`` / ``recovery``）⇒ 光看错误体**分不开**。

    ⇒ 拆成「建空白页 + 导航」之后：
    **建页**失败 ⇒ 执行层（记 ``unresolved``，**不进负样本池**）；
    **导航**失败 ⇒ 站点事实（记 ``unreachable_hard``，真负样本要留住）。
    """

    async def test_建页与导航是两次调用且建页不带_url(self):
        """结构判据本身：建页**不带 URL**（上游 ``if (url)`` ⇒ 只建页）。"""
        session = _FakeSession()
        session.stack = []
        driver = _driver(session)
        await driver.goto("https://example.com/a")
        assert session.creates == [""], "建页必须不带 URL（否则又回到合并路由）"
        assert session.navigations == [("tab-2", "https://example.com/a")]

    async def test_建页失败是执行层故障_不是站点打不开(self):
        """**T048 那条假负样本的原样形状**（``tab create timed out``）。

        ⚠️ 判据是**路由**不是错误形状：建页那一趟**页面里根本没有
        站点 URL** ⇒ 站点不可能是原因 ⇒ 抛
        :class:`DriverSessionError` ⇒ 上层记 ``unresolved``
        ⇒ **不进负样本池**（采集失败不是站点结论）。
        """
        session = _FakeSession()
        session.stack = []
        session.new_page_error = DriverError(
            "camofox tool camofox_create_tab 返回错误: camofox error: 500: "
            '{"error":"tab create timed out after 30000ms","retryable":false}'
        )
        driver = _driver(session)
        with pytest.raises(DriverSessionError):
            await driver.goto("https://example.com/a")

    async def test_自愈时建页失败也是执行层故障(self):
        """**T048 的原样时序**：页没了 ⇒ 换页 ⇒「建页超时」
        ⇒ 之前记成了 ``unreachable_hard`` ⇒ 一条**理由通顺的假负样本**
        （而同一批的 T038 **打开了同一个 iqiyi URL**）。"""
        session = _FakeSession()
        session.tab_gone = 1
        session.new_page_error = DriverError(
            "camofox tool camofox_create_tab 返回错误: camofox error: 500: "
            '{"error":"tab create timed out after 30000ms","retryable":false}'
        )
        driver = _driver(session)
        with pytest.raises(DriverSessionError):
            await driver.goto("https://example.com/a")

    async def test_导航失败仍然是站点事实(self):
        """**DNS 不通是站点的事实** ⇒ 抛 ``DriverUnreachableError``
        ⇒ ``unreachable_hard``（真负样本要留住，不许被降级）。

        ⚠️ **类型变了**（M-44 裁定，owner 2026-10-27）：原先「站点打不开」
        与「这次没打开」都抛普通 ``DriverError``，上层分不开 ⇒ 收窄成
        「**机器码证明**站点不可达」才抛这个新类型。
        """
        session = _FakeSession()
        session.navigate_error = DriverError(
            "camofox tool camofox_navigate 返回错误: camofox error: 500: "
            '{"error":"page.goto: NS_ERROR_UNKNOWN_HOST"}'
        )
        driver = _driver(session)
        with pytest.raises(DriverUnreachableError) as caught:
            await driver.goto("https://example.com/a")
        assert not isinstance(caught.value, DriverSessionError)
        assert caught.value.code == "NS_ERROR_UNKNOWN_HOST"

    async def test_连接被拒也是真站点不可达(self):
        """**连接被拒**（公网主机 + 关闭端口 ⇒ 服务器回 RST）同样配记
        ``unreachable_hard``——机器码 ``NS_ERROR_CONNECTION_REFUSED``
        （实测原文见 ``research/probes/camofox_nav_failure_codes.json``）。"""
        session = _FakeSession()
        session.navigate_error = DriverError(
            "camofox tool camofox_navigate 返回错误: camofox error: 500: "
            '{"error":"page.goto: NS_ERROR_CONNECTION_REFUSED"}'
        )
        driver = _driver(session)
        with pytest.raises(DriverUnreachableError) as caught:
            await driver.goto("https://example.com/a")
        assert caught.value.code == "NS_ERROR_CONNECTION_REFUSED"

    async def test_超时不算真站点不可达(self):
        """⚠️ **超时证明不了站点不可达**（M-44）——站点可能只是慢。

        ⇒ 抛普通 ``DriverError``（**不是** ``DriverUnreachableError``）
        ⇒ 上层记 ``unresolved``（**不进负样本池**）。
        """
        session = _FakeSession()
        session.navigate_error = DriverError(
            "camofox tool camofox_navigate 返回错误: camofox error: 500: "
            '{"error":"page.goto: NS_ERROR_NET_TIMEOUT"}'
        )
        driver = _driver(session)
        with pytest.raises(DriverError) as caught:
            await driver.goto("https://example.com/a")
        assert not isinstance(caught.value, DriverUnreachableError)

    async def test_被打断不算真站点不可达(self):
        """⚠️ **打断**的形态包含「**重定向确实发生了**」的情形
        ⇒ 站点是通的 ⇒ 不算「站点不可达」。"""
        session = _FakeSession()
        session.navigate_error = DriverError(
            "camofox tool camofox_navigate 返回错误: camofox error: 500: "
            '{"error":"page.goto: Navigation to \\"http://a.test/\\" is '
            'interrupted by another navigation to \\"http://b.test/\\""}'
        )
        driver = _driver(session)
        with pytest.raises(DriverError) as caught:
            await driver.goto("https://example.com/a")
        assert not isinstance(caught.value, DriverUnreachableError)

    async def test_第一页的站点失败也是站点事实(self):
        """第一页走的是「建空白页 + 导航」⇒ 站点失败仍落在**导航**路由
        ⇒ 仍然是站点事实（别把「第一页」也一起降级）。"""
        session = _FakeSession()
        session.stack = []
        session.navigate_error = DriverError(
            "camofox tool camofox_navigate 返回错误: camofox error: 500: "
            '{"error":"page.goto: NS_ERROR_UNKNOWN_HOST"}'
        )
        driver = _driver(session)
        with pytest.raises(DriverError) as caught:
            await driver.goto("https://example.com/a")
        assert not isinstance(caught.value, DriverSessionError)

    async def test_上游明说会话过期就是执行层故障(self):
        """上游的 ``recovery="retry"`` ⇒ **明说**「我们这边坏了，重来即可」
        ——那个站点**一次都没被试过** ⇒ 不许记 ``unreachable_hard``。

        ⚠️ 这条在改之前是**潜伏**的（实测池里 0 条命中）：上游的
        ``session_expired`` 走 HTTP 503 且**带** ``recovery``，
        漏判的后果与「页没了」那条一模一样。
        """
        session = _FakeSession()
        session.navigate_error = _session_expired_error()
        driver = _driver(session)
        with pytest.raises(DriverSessionError):
            await driver.goto("https://example.com/a")

    async def test_换页重试后站点仍然失败_仍然是站点事实(self):
        """**换页成功、导航失败** = 在一张**干净的新页**上得到的传输层事实
        ⇒ 仍然是 ``unreachable_hard``，不许降级成 ``unresolved``。

        ⚠️ **只有「换页本身失败」才是采集侧的故障**（方案 21 §R5）。
        """
        session = _FakeSession()
        session.tab_gone = 1
        session.navigate_error = DriverError(
            "camofox tool camofox_navigate 返回错误: camofox error: 500: "
            '{"error":"page.goto: NS_ERROR_UNKNOWN_HOST"}'
        )
        driver = _driver(session)
        with pytest.raises(DriverError) as caught:
            await driver.goto("https://example.com/a")
        assert not isinstance(caught.value, DriverSessionError)
        assert len(driver.recoveries) == 1, "换过一次页要记账"
