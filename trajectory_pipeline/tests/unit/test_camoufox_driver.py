"""camoufox 驱动（P1 骨架）的口径测试——**不联网、不开浏览器**。

测三件事，每一件都是「实现看起来能跑但其实写错了」的形状：

1. **正文三级降级**（innerText → textContent → HTML）与 ``body_source`` 标记。
   写错的后果不是报错，是「正文空」被当成「页面没内容」——假负样本。
2. **未实现的原语要抛，不许返回空**。骨架期返回空列表/空正文，与
   「页面上确实没有」完全同形（这就是本项目最贵的一类错）。
3. **``evaluate`` 没有 JSON 解包**。obscura 那层 ``json.loads`` 是它的**传输
   格式**（MCP 把 JS 值当文本给），照抄过来会把字符串 ``'["a"]'`` 变成 list，
   两个实现对同一表达式给出不同类型——比错更糟，因为看起来都对。
4. **``interactive`` 的三类丢弃与账目**（P3-α）。丢无名与真同名的元素是**有意**
   的（它们定位不到），但「丢了几个」必须记进 ``last_enumeration``——
   否则「页面上真没有可点目标」与「全被过滤了」在观察里完全同形。

``snapshot`` 是 P1 存档的唯一必需项（不变式 I4），所以它的降级记账是重点。
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.async_api import Error as PwError

from trajectory_pipeline.executor.browser import camoufox_driver
from trajectory_pipeline.executor.browser.camoufox_driver import (
    ACTION_TIMEOUT_MS,
    DEGRADED_NAV_UNSETTLED,
    DOC_STAMP_EXPR,
    DOC_STAMP_NONE,
    NAV_SETTLE_TIMEOUT_MS,
    CamoufoxDriver,
)
from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.executor.dom import INNER_TEXT_EXPR, TEXT_CONTENT_EXPR


class _FakeFrame:
    """主框架占位——留着是为了「子框架导航」那条口径还能测。

    真 playwright 里主框架的导航事件带的是 **frame**；驱动现在用
    **文档代号**（``performance.timeOrigin``）当判据，不再订阅事件——
    但「子框架换文档不算换文档」这条口径**仍然要测**，所以假页保留
    ``main_frame`` 与 ``navigate(main=False)``。
    """

    def __init__(self, page: "_FakePage") -> None:
        self.page = page


class _FakePage:
    """按表达式分发返回值；``locator()`` 只支持 ``count()``。"""

    def __init__(
        self,
        *,
        url: str = "https://example.com/watch",
        title: str = "示例影片 在线观看",
        evals: dict[str, Any] | None = None,
        html: str = "",
        count: int | Exception = 0,
        fail_goto: bool = False,
        fail_title: bool = False,
        aria: str | Exception = "",
        role_counts: dict[tuple[str, str | None], tuple[int | Exception, Exception | None]] | None = None,
        keyboard_error: Exception | None = None,
        selectors: dict[str, Any] | None = None,
        selector_counts: dict[str, Any] | None = None,
        navigate_verbs: tuple[str, ...] = ("click", "press"),
        fail_stamp: bool = False,
        no_performance: bool = False,
    ) -> None:
        self.url = url
        self._title = title
        self.evals = evals or {}
        self.html = html
        self.count_value = count
        self.fail_goto = fail_goto
        self.fail_title = fail_title
        self.aria = aria
        self.goto_calls: list[tuple[str, str]] = []
        self.aria_calls = 0
        #: ``(role, name) → (count, 动作时抛的异常)``；缺省是「1 个且不抛」。
        self.role_counts = role_counts or {}
        #: ``get_by_role`` 的调用流水：``(role, name, exact)``。
        self.role_calls: list[tuple[str, str | None, bool]] = []
        #: 动作流水：``(verb, role, name, 细节, timeout)``。
        self.actions: list[tuple[str, str, str | None, Any, Any]] = []
        self.keyboard = _FakeKeyboard(self)
        self.keyboard_keys: list[str] = []
        self.keyboard_error = keyboard_error
        #: 动作是否关掉了「等导航完成」（``no_wait_after``）——
        #: 关掉是**有意的**（见 ``_settle``），所以它得能被断言。
        self.no_wait_after: list[tuple[str, bool]] = []
        #: ``wait_for_load_state`` 的调用流水：``(state, timeout)``。
        self.load_state_calls: list[tuple[str, Any]] = []
        #: 设成异常时 ``wait_for_load_state`` 抛它（= 新文档 2s 内没 ready）。
        self.load_state_error: Exception | None = None
        #: ``eval_on_selector_all(selector, …)`` 的返回值，按 **selector** 配；
        #: 值可以是 list，也可以是异常（= 选择器语法错 / 页面坏了）。
        self.selectors: dict[str, Any] = selectors or {}
        #: ``eval_on_selector_all`` 的调用流水：``(selector, js)``。JS 也要记——
        #: 「取文本」与「取属性」走同一个 API、**不同函数体**，只看 selector
        #: 分不出走的是哪条路（而那正是 extract 最容易写错的地方）。
        self.pluck_calls: list[tuple[str, str]] = []
        #: ``locator(selector).count()`` 的返回值，按 selector 配（缺省用 ``count``）。
        self.selector_counts: dict[str, Any] = selector_counts or {}
        self.locator_calls: list[str] = []
        #: ``close()`` 的记录：被关掉的页不允许再被当成当前页用。
        self.closed = False
        # ── 文档代号（2026-10-22）─────────────────────────────────
        #
        # 驱动用 **``performance.timeOrigin``**（每文档一个常量）当
        # 「新文档 commit 了」的判据（见 camoufox_driver 的「动作之后的
        # 读稳」）。假对象必须**同形**：换文档 ⇒ 代号变；同文档导航
        # （pushState）⇒ 代号**不变**；子框架换文档 ⇒ 代号**不变**。
        self.main_frame = _FakeFrame(self)
        #: ``event → [handler, …]``（驱动现在不订阅，留着只为测「别订阅」）。
        self.listeners: dict[str, list[Any]] = {}
        #: 当前文档的代号。每次**主框架**导航 +1（同文档导航不变）。
        self.stamp = "1000"
        #: 哪些动作会触发**导航 commit**（默认可点/可按键会）。
        #: 填 ``()`` 就是「点了不跳页」——驱动该记 ``nav="same"``。
        self.navigate_verbs = navigate_verbs
        #: 已发生的导航次数（诊断用）。
        self.navigations = 0
        #: 代号**读不到**（= 执行上下文被销毁 ⇒ 正在换文档）⇒ 抛。
        self.fail_stamp = fail_stamp
        #: 页面**没有** ``performance`` ⇒ 代号恒为 ``"none"``（读得到）。
        self.no_performance = no_performance
        #: 代号读取次数（诊断用）。
        self.stamp_reads = 0

    def on(self, event: str, handler: Any) -> None:
        """订阅事件（真 playwright 同名 API）。

        ⚠️ **驱动现在不订阅导航事件了**（改用文档代号）——留着这个方法
        只为让「驱动误订阅事件」这件事**测得出来**：真要是有人又改回
        事件计数，这条假实现不该挡路。
        """
        self.listeners.setdefault(event, []).append(handler)

    def navigate(self, *, main: bool = True) -> None:
        """模拟一次**导航 commit**：换文档 ⇒ 代号变。

        ``main=False`` 模拟**子框架**导航——子框架换文档**不换主文档**，
        代号**不变**（驱动不该把它算成「当前页换了文档」）。
        """
        if not main:
            return
        self.navigations += 1
        self.stamp = str(int(self.stamp) + 1)

    def push_state(self) -> None:
        """模拟**同文档导航**（改 query 不换文档）——代号**不变**。"""
        self.url = f"{self.url}?tp_fake=1"

    async def title(self) -> str:
        if self.fail_title:
            raise PwError("title 读不到")
        return self._title

    async def content(self) -> str:
        return self.html

    async def evaluate(self, expression: str) -> Any:
        if expression == DOC_STAMP_EXPR:
            self.stamp_reads += 1
            if self.fail_stamp:
                raise PwError("fake: 执行上下文被销毁")
            return DOC_STAMP_NONE if self.no_performance else self.stamp
        if expression not in self.evals:
            raise PwError(f"fake: 表达式未配置 {expression!r}")
        value = self.evals[expression]
        if isinstance(value, Exception):
            raise value
        # **可调用值**：让测试在读数**中途**做点事（= 模拟「读到一半」——
        # 导航恰好落在两次往返之间）。这是本轮要治的那个病。
        if callable(value):
            value = value()
        return value

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        if self.fail_goto:
            raise PwError("net::ERR_NAME_NOT_RESOLVED")
        self.goto_calls.append((url, wait_until))

    async def aria_snapshot(self) -> str:
        self.aria_calls += 1
        if isinstance(self.aria, Exception):
            raise self.aria
        return self.aria

    async def wait_for_load_state(self, state: str, timeout: Any = None) -> None:
        self.load_state_calls.append((state, timeout))
        if self.load_state_error is not None:
            raise self.load_state_error

    def locator(self, selector: str) -> _FakeLocator:
        self.locator_calls.append(selector)
        return _FakeLocator(self.selector_counts.get(selector, self.count_value))

    async def eval_on_selector_all(self, selector: str, js: str) -> Any:
        self.pluck_calls.append((selector, js))
        if selector not in self.selectors:
            return []
        value = self.selectors[selector]
        if isinstance(value, Exception):
            raise value
        return value

    async def close(self) -> None:
        self.closed = True

    # ── 动作面（P2）──────────────────────────────────────────────
    #
    # 只模拟「playwright 给了什么」：``get_by_role`` 返回一个能 count /
    # click / fill / press 的 locator。计数与异常都按 ``(role, name)`` 配，
    # 这样测试才写得出「同一个 role 下只有某个名字存在」。

    def get_by_role(self, role: str, *, name: str | None = None,
                    exact: bool = False) -> "_FakeRoleLocator":
        self.role_calls.append((role, name, exact))
        return _FakeRoleLocator(self, role, name)

    keyboard: "_FakeKeyboard"     # 在 __init__ 里赋值


class _FakeRoleLocator:
    def __init__(self, page: "_FakePage", role: str, name: str | None) -> None:
        self._page = page
        self._role = role
        self._name = name

    @property
    def _spec(self) -> tuple[int | Exception, Exception | None]:
        return self._page.role_counts.get(
            (self._role, self._name), (1, None)
        )

    async def count(self) -> int:
        count, _ = self._spec
        if isinstance(count, Exception):
            raise count
        return count

    def _record(self, verb: str, detail: Any, timeout: Any) -> None:
        self._page.actions.append(
            (verb, self._role, self._name, detail, timeout)
        )
        _, err = self._spec
        if err is not None:
            raise err
        # 动作真的落地了 ⇒ 有没有跟着导航，由假页说了算
        # （默认可点/可按键会跳页；``fill`` 不会）。
        if verb in self._page.navigate_verbs:
            self._page.navigate()

    # click/press 收 ``no_wait_after``：动作面**不**让 playwright 等它触发的
    # 那次导航（实测那样要 9–13s，见 camoufox_driver._settle）。假 page 记下来，
    # 让「有没有传」也能被断言。
    async def click(self, timeout: int | None = None, no_wait_after: bool = False) -> None:
        self._page.no_wait_after.append(("click", no_wait_after))
        self._record("click", None, timeout)

    async def fill(self, text: str, timeout: int | None = None) -> None:
        self._record("fill", text, timeout)

    async def press(self, key: str, timeout: int | None = None,
                    no_wait_after: bool = False) -> None:
        self._page.no_wait_after.append(("press", no_wait_after))
        self._record("press", key, timeout)


class _FakeKeyboard:
    def __init__(self, page: "_FakePage") -> None:
        self._page = page

    async def press(self, key: str) -> None:
        self._page.keyboard_keys.append(key)
        if self._page.keyboard_error is not None:
            raise self._page.keyboard_error
        # 页面级按键（回车提交）会跳页——与元素级 press 同一口径。
        if "press" in self._page.navigate_verbs:
            self._page.navigate()


class _FakeLocator:
    def __init__(self, count: int | Exception) -> None:
        self._count = count

    async def count(self) -> int:
        if isinstance(self._count, Exception):
            raise self._count
        return self._count


class _FakeSession:
    """页面**栈**的假实现——``page`` 是栈顶，``new_page``/``close_page`` 动栈。

    与真实现同一套语义（只剩一页时补空白页、关非当前页不动栈顶）：
    标签页这件事**必须在假对象上也是同一套规则**，否则「关完回到哪一页」
    这条正是测不到的部分。
    """

    def __init__(self, page: _FakePage, *, new_pages: list[_FakePage] | None = None) -> None:
        self._pages: list[_FakePage] = [page]
        self._spawned: list[_FakePage] = list(new_pages or [])
        self.new_page_calls = 0
        self.close_page_calls: list[_FakePage] = []
        self.closed = False

    @property
    def page(self) -> _FakePage:
        return self._pages[-1]

    @property
    def pages(self) -> tuple[_FakePage, ...]:
        return tuple(self._pages)

    async def new_page(self, *, url: str | None = None) -> _FakePage:
        self.new_page_calls += 1
        page = self._spawned.pop(0) if self._spawned else _FakePage()
        self._pages.append(page)
        return page

    async def close_page(self, page: _FakePage) -> bool:
        if page not in self._pages:
            return False
        self.close_page_calls.append(page)
        if len(self._pages) == 1:
            self._pages.append(_FakePage())
        self._pages.remove(page)
        await page.close()
        return True

    async def close(self) -> None:
        self.closed = True


def _driver(page: _FakePage) -> tuple[CamoufoxDriver, _FakeSession]:
    session = _FakeSession(page)
    return CamoufoxDriver(session), session       # type: ignore[arg-type]


class TestSnapshot:
    async def test_innerText_路径带出_url_title_且标_inner_text(self):
        page = _FakePage(evals={INNER_TEXT_EXPR: "  第一行\n\n\n第二行  "})
        driver, _ = _driver(page)
        snap = await driver.snapshot(max_chars=100)
        assert snap.url == "https://example.com/watch"
        assert snap.title == "示例影片 在线观看"
        assert snap.body == "第一行\n\n第二行"      # 空白压到最多一个空行
        assert snap.body_source == "inner_text"
        assert snap.raw_len == len("  第一行\n\n\n第二行  ")
        assert snap.stripped_ratio == 1.0
        assert snap.truncated is False

    async def test_innerText_空走_textContent_且不记降级(self):
        """走了备用路 ≠ 没采到。降级标记只在**三路都空**时记。"""
        page = _FakePage(evals={
            INNER_TEXT_EXPR: "",
            TEXT_CONTENT_EXPR: "从 textContent 拿到的正文",
        })
        driver, _ = _driver(page)
        degraded: list[str] = []
        snap = await driver.snapshot(max_chars=100, degraded=degraded)
        assert snap.body == "从 textContent 拿到的正文"
        assert snap.body_source == "inner_text"     # 仍是文本源，不是 HTML
        assert degraded == []

    async def test_innerText_抛错也走_textContent(self):
        page = _FakePage(evals={
            INNER_TEXT_EXPR: PwError("layout 没算完"),
            TEXT_CONTENT_EXPR: "兜底正文",
        })
        driver, _ = _driver(page)
        degraded: list[str] = []
        snap = await driver.snapshot(max_chars=100, degraded=degraded)
        assert snap.body == "兜底正文"
        assert degraded == []

    async def test_两路都空走_HTML_兜底并标_snapshot(self):
        """HTML 兜底是**更弱的证据**：body_source 必须如实标出来。"""
        page = _FakePage(
            evals={INNER_TEXT_EXPR: "", TEXT_CONTENT_EXPR: ""},
            html="<html><head><style>.a{color:red}</style></head>"
                 "<body><div>正文在 HTML 里</div></body></html>",
        )
        driver, _ = _driver(page)
        degraded: list[str] = []
        snap = await driver.snapshot(max_chars=1000, degraded=degraded)
        assert snap.body_source == "snapshot"
        assert "正文在 HTML 里" in snap.body
        assert "color:red" not in snap.body          # style 块被剥掉
        assert snap.stripped_ratio < 1.0
        assert degraded == []

    async def test_取到空白文本也要继续兜底(self):
        """``innerText`` 拿到 ``"\\n\\n  "``（渲染未完成）规范化后是空。

        此时必须继续走 HTML 兜底——按「原始文本非空」提前收工，会**既不兜底
        也不记降级**，正文空着进存档，读的人当成「这页没内容」。
        """
        page = _FakePage(
            evals={INNER_TEXT_EXPR: "\n\n  ", TEXT_CONTENT_EXPR: "  "},
            html="<html><body><div>HTML 里才有正文</div></body></html>",
        )
        driver, _ = _driver(page)
        degraded: list[str] = []
        snap = await driver.snapshot(max_chars=1000, degraded=degraded)
        assert "HTML 里才有正文" in snap.body
        assert snap.body_source == "snapshot"
        assert degraded == []

    async def test_三路都空才记_body_text(self):
        page = _FakePage(evals={INNER_TEXT_EXPR: "", TEXT_CONTENT_EXPR: ""},
                         html="<html><body></body></html>")
        driver, _ = _driver(page)
        degraded: list[str] = []
        snap = await driver.snapshot(max_chars=1000, degraded=degraded)
        assert snap.body == ""
        assert degraded == ["body_text"]

    async def test_乱码分别记正文与标题(self):
        """标题烂掉与正文烂掉是**两件事**：前者让「标题是否与目标一致」
        这条判据失效，后者让整段证据失效——合成一个标记就分不出范围。

        乱码的判据是**替换字符出现次数**（非阈值 3），不是「看起来像乱码」。
        """
        bad = "\ufffd" * 3
        page = _FakePage(
            url="https://example.com/x",
            title=f"示例影片{bad}",
            evals={INNER_TEXT_EXPR: f"正文{bad}"},
        )
        driver, _ = _driver(page)
        degraded: list[str] = []
        await driver.snapshot(max_chars=1000, degraded=degraded)
        assert "body_mojibake" in degraded
        assert "title_mojibake" in degraded

    async def test_取快照失败抛_DriverError_不返回空快照(self):
        """snapshot 是 fail-closed 的锚点（I4）：不能降级成空快照，
        否则下层会把它读成「页面真的什么都没有」。"""
        page = _FakePage(fail_title=True, evals={INNER_TEXT_EXPR: "正文"})
        driver, _ = _driver(page)
        with pytest.raises(DriverError):
            await driver.snapshot(max_chars=100)

    async def test_截断按_innerText_自身长度判断(self):
        page = _FakePage(evals={INNER_TEXT_EXPR: "字" * 50})
        driver, _ = _driver(page)
        snap = await driver.snapshot(max_chars=50)
        assert snap.truncated is True
        assert len(snap.body) == 50


class TestPrimitives:
    async def test_goto_失败抛_DriverError(self):
        driver, _ = _driver(_FakePage(fail_goto=True))
        with pytest.raises(DriverError):
            await driver.goto("https://example.com/")

    async def test_goto_透传_wait_until(self):
        page = _FakePage()
        driver, _ = _driver(page)
        await driver.goto("https://example.com/", wait_until="domcontentloaded")
        assert page.goto_calls == [("https://example.com/", "domcontentloaded")]

    async def test_evaluate_原样返回值不做_JSON_解包(self):
        """``'["a"]'`` 必须仍是**字符串**——obscura 会解成 list，
        照抄那层解包会让两个实现对同一表达式给出不同类型。"""
        page = _FakePage(evals={"expr": '["a"]'})
        driver, _ = _driver(page)
        assert await driver.evaluate("expr") == '["a"]'

    async def test_evaluate_失败抛_DriverError(self):
        driver, _ = _driver(_FakePage())
        with pytest.raises(DriverError):
            await driver.evaluate("未配置的表达式")

    async def test_count_正常与失败(self):
        driver, _ = _driver(_FakePage(count=7))
        assert await driver.count("video, audio") == 7
        driver2, _ = _driver(_FakePage(count=PwError("selector 非法")))
        assert await driver2.count("!!!") == 0      # 与 obscura on_error="zero" 同口径

    async def test_close_关的是会话且幂等由会话负责(self):
        driver, session = _driver(_FakePage())
        await driver.close()
        assert session.closed is True


#: 一段真实形态的可访问性树：嵌套、无名、真同名、词表外 role 全都有。
ARIA_SAMPLE = """\
- generic [active]:
  - button "立即播放"
  - link "在线观看":
    - /url: /watch/1
  - textbox "站内搜索"
  - textbox
  - button "详情"
  - button "详情"
  - heading "影片介绍"
"""


class TestInteractive:
    async def test_role_name_全取_playwright_的结论(self):
        """``role`` 装 ARIA role、``name`` 装 name——采集层不重算可访问名。

        自己拼的那条链（``aria-label || innerText || placeholder``）在真实页面
        上只有 87.9% 与 playwright 一致，对不上的那些**定位不到**。
        """
        driver, _ = _driver(_FakePage(aria=ARIA_SAMPLE))
        elements = await driver.interactive()
        assert [(e.ref, e.role, e.name) for e in elements] == [
            ("e1", "button", "立即播放"),
            ("e2", "link", "在线观看"),
            ("e3", "textbox", "站内搜索"),
        ]

    async def test_三类丢弃都记账(self):
        """无名 1（第二个 textbox）、真同名整组 2（两个「详情」）、
        词表外 role 3（generic / /url / heading）。

        **这四项是空列表的自证**：「页面上一个可点目标都没有」与「全被过滤了」
        在观察里同形，只有这里分得开。

        ⚠️ **2026-10-21 加了 ``role_unmatched``**：以前词表外那 3 个是
        「知道丢了但没记数」，于是账目对不上（nodes 9 ≠ role_matched 6 +
        后面三项），读账的人以为漏了一档。现在过滤阶梯是**共用**的
        （:func:`element_basis.filter_ladder`），每一档都有数，
        ``nodes = role_matched + role_unmatched`` 恒等。
        """
        driver, _ = _driver(_FakePage(aria=ARIA_SAMPLE))
        await driver.interactive()
        assert driver.last_enumeration == {
            "nodes": 9,
            "role_matched": 6,
            "role_unmatched": 3,
            "nameless_dropped": 1,
            "ambiguous_dropped": 2,
            "kept": 3,
            "truncated": 0,
        }

    async def test_ref_到_role_name_的映射留在驱动内部(self):
        """ref 只给判断点 ③ 回查用；动作面走 role+name（D12：ref 不进训练数据）。"""
        driver, _ = _driver(_FakePage(aria=ARIA_SAMPLE))
        await driver.interactive()
        assert driver.refs["e2"] == ("link", "在线观看")

    async def test_导航清空_ref_表(self):
        """不清的话，上个页面残留的 ``e3`` 会指向本页一个**合法但不同**的元素
        ——点错不可逆，而且看起来完全正常。"""
        driver, _ = _driver(_FakePage(aria=ARIA_SAMPLE))
        await driver.interactive()
        assert driver.refs
        await driver.goto("https://example.com/next")
        assert driver.refs == {}

    async def test_limit_截断并记账(self):
        driver, _ = _driver(_FakePage(aria=ARIA_SAMPLE))
        elements = await driver.interactive(limit=2)
        assert [e.ref for e in elements] == ["e1", "e2"]
        assert driver.last_enumeration["truncated"] == 1

    async def test_limit_为零给空列表且不是失败(self):
        """``limit=0`` 是调用方要空，不是能力故障——与「取快照失败」必须分开。"""
        driver, _ = _driver(_FakePage(aria=ARIA_SAMPLE))
        assert await driver.interactive(limit=0) == ()
        assert driver.last_enumeration["kept"] == 0

    async def test_取_aria_快照失败抛错不返回空列表(self):
        """obscura 同位置是 ``on_error="empty"``（吞成空列表）。这里**有意不一致**：
        空列表会把一次**能力故障**写成**页面事实**，两者正确处置相反。"""
        driver, _ = _driver(_FakePage(aria=PwError("Target closed")))
        with pytest.raises(DriverError):
            await driver.interactive()

    async def test_只取一次整页快照(self):
        """一页一次 ``aria_snapshot()``（51ms/页）。逐元素取是 3.3ms×N——
        200 个元素就是 4.8s，成本差在量级上。"""
        page = _FakePage(aria=ARIA_SAMPLE)
        driver, _ = _driver(page)
        await driver.interactive()
        assert page.aria_calls == 1


class TestActionFace:
    """P2 动作面：``role+name`` + ``exact=True`` + 先 count 再操作。

    这一组的每条用例都在守一个**静默失败**：
    ``exact`` 掉了 → 命中率变成 100%（判据变空）；count 省了 → 「页面卡住」
    与「元素不存在」同形；同名整组点了 → 点错目标（不可逆）。
    """

    async def test_click_按_role_name_定位且_exact_必开(self):
        """``exact=True`` 不是可选项：默认是 substring + 忽略大小写，
        实测那会让 P-1 的命中率判据变成 100%（什么都命中）。"""
        page = _FakePage()
        driver, _ = _driver(page)
        await driver.click(role="button", name="立即播放")
        assert page.role_calls == [("button", "立即播放", True)]
        assert page.actions == [("click", "button", "立即播放", None, ACTION_TIMEOUT_MS)]
        assert driver.last_action["outcome"] == "ok"

    async def test_click_元素不存在时静默返回并记账(self):
        """0 命中 = 业务事实（元素不在页面上），**不抛**——抛了会让整站
        变成 ``unreachable_hard`` 那种理由通顺的负样本。但动作返回 None，
        于是「点成功了」与「没点」同形，所以账目必须记。"""
        page = _FakePage(role_counts={("button", "立即播放"): (0, None)})
        driver, _ = _driver(page)
        await driver.click(role="button", name="立即播放")      # 不抛
        assert page.actions == []                               # 也确实没点
        assert driver.last_action == {
            "op": "click", "role": "button", "name": "立即播放",
            "outcome": "not_found",
        }

    async def test_真同名整组丢弃_不赌哪一个(self):
        """``count > 1`` = 真同名（实测 6.3%）。点哪个都是赌，而点击**不可逆**
        （可能点进另一个视频）。方案 04 §4.5.3 的「代码按文本挑一个」还没落地，
        落地前这里就是 fail-closed：不点、记账、让上层看得到。"""
        page = _FakePage(role_counts={("link", "50集全 电视剧"): (3, None)})
        driver, _ = _driver(page)
        await driver.click(role="link", name="50集全 电视剧")   # 不抛
        assert page.actions == []
        assert driver.last_action["outcome"] == "ambiguous"
        assert driver.last_action["matches"] == "3"

    async def test_count_本身报错是能力故障不是元素不存在(self):
        """与 :meth:`count` 的「失败返回 0」**有意不一致**：那条是对齐
        obscura 的旧口径，这条是判断的前置——把故障折叠成 0 就分不出
        「页面卡住」与「元素不在」。"""
        page = _FakePage(
            role_counts={("button", "立即播放"): (PwError("Target closed"), None)}
        )
        driver, _ = _driver(page)
        with pytest.raises(DriverError):
            await driver.click(role="button", name="立即播放")

    async def test_元素在但动作抛错是能力故障(self):
        page = _FakePage(
            role_counts={("button", "立即播放"): (1, PwError("intercepted"))}
        )
        driver, _ = _driver(page)
        with pytest.raises(DriverError):
            await driver.click(role="button", name="立即播放")

    async def test_词表外的_role_直接判不存在且不往返(self):
        """playwright 对未知 role 会抛 ValueError——那会被误读成能力故障。
        直接按「不可定位」处理，省一次往返，也不惊动调用方。"""
        page = _FakePage()
        driver, _ = _driver(page)
        await driver.click(role="heading", name="影片介绍")
        assert page.role_calls == []                    # 一次都没问浏览器
        assert driver.last_action["outcome"] == "not_found"

    async def test_空_name_直接判不存在且不往返(self):
        """``get_by_role(role, name="")`` 命中 0 **且不报错**——那是
        F5-c 的死亡路径（与「元素不存在」同形）。所以先在这里挡住。"""
        page = _FakePage()
        driver, _ = _driver(page)
        await driver.click(role="link", name="")
        assert page.role_calls == []
        assert driver.last_action["outcome"] == "not_found"

    async def test_跨面参数炸穿而不是变成负样本(self):
        """拿 obscura 面的词汇（``ref``）调 camoufox 的动作面 = 接线错误。

        抛 ``ValueError`` 而不是 ``DriverError``：后者会被 ``steps/`` 捕获成
        ``unreachable_hard``，于是接线 bug 变成一条**理由通顺的负样本**。
        """
        page = _FakePage()
        driver, _ = _driver(page)
        with pytest.raises(ValueError):
            await driver.click(ref="e1")
        assert page.role_calls == []

    async def test_fill_是设值且把文本交给_locator(self):
        page = _FakePage()
        driver, _ = _driver(page)
        await driver.fill(role="textbox", name="站内搜索", text="功夫")
        assert page.role_calls == [("textbox", "站内搜索", True)]
        assert page.actions == [
            ("fill", "textbox", "站内搜索", "功夫", ACTION_TIMEOUT_MS)
        ]

    async def test_fill_框不存在时不填(self):
        page = _FakePage(role_counts={("textbox", "站内搜索"): (0, None)})
        driver, _ = _driver(page)
        await driver.fill(role="textbox", name="站内搜索", text="功夫")
        assert page.actions == []
        assert driver.last_action["outcome"] == "not_found"

    async def test_动作之后要等新文档_dom_ready(self):
        """``click`` / ``press`` 之后等一次 ``domcontentloaded``。

        为什么不让 playwright 自己等（默认行为）：实测 baidu 上默认等待
        **9–13s**，`goto` 同一地址只要 0.3s——5s 的动作预算让一半点击直接超时。
        所以关掉 ``no_wait_after`` 的等待，换成这里有界的一次。
        """
        page = _FakePage()
        driver, _ = _driver(page)
        await driver.click(role="button", name="立即播放")
        assert page.no_wait_after == [("click", True)]
        assert page.load_state_calls == [("domcontentloaded", NAV_SETTLE_TIMEOUT_MS)]
        assert driver.last_action["settle"] == "ok"

    async def test_等不到_dom_ready_不算动作失败但要说出来(self):
        """新文档 2s 内没 ready 是**页面事实**，不是动作失败——点击确实发出去了。

        但「等到了」与「没等到」必须在账上分得开：下一步读到的可能是旧页
        （避坑指南 §8.8），归因要靠这个字段。
        """
        page = _FakePage()
        page.load_state_error = PwError("Timeout 2000ms exceeded")
        driver, _ = _driver(page)
        await driver.click(role="button", name="立即播放")      # 不抛
        assert driver.last_action["outcome"] == "ok"
        assert driver.last_action["settle"] == "timeout"

    async def test_press_不带目标是页面级按键(self):
        page = _FakePage()
        driver, _ = _driver(page)
        await driver.press("Enter")
        assert page.keyboard_keys == ["Enter"]
        assert page.actions == []
        assert driver.last_action["outcome"] == "ok"

    async def test_press_带目标时压在元素上(self):
        page = _FakePage()
        driver, _ = _driver(page)
        await driver.press("Enter", role="textbox", name="站内搜索")
        assert page.actions == [
            ("press", "textbox", "站内搜索", "Enter", ACTION_TIMEOUT_MS)
        ]
        assert page.keyboard_keys == []          # 没退化成页面级按键

    async def test_press_目标不存在时不按键(self):
        """带目标却按不到 = 页面事实；**不能退化成页面级按键**——
        那会把「搜索框没找到」变成「在随便哪里敲了回车」。"""
        page = _FakePage(role_counts={("textbox", "站内搜索"): (0, None)})
        driver, _ = _driver(page)
        await driver.press("Enter", role="textbox", name="站内搜索")
        assert page.keyboard_keys == []
        assert driver.last_action["outcome"] == "not_found"

    async def test_页面级按键失败是能力故障(self):
        page = _FakePage(keyboard_error=PwError("Target closed"))
        driver, _ = _driver(page)
        with pytest.raises(DriverError):
            await driver.press("Enter")


class TestLinks:
    async def test_取绝对地址并跳过空_href(self):
        """``<a href="">`` 的 ``a.href`` 会解析成**当前页地址**。

        假 page 给的就是解析后的结果，故这里断言的是「拿到什么就怎么处理」：
        空 href 已经在 JS 里滤掉，剩下形状不对的行不猜。
        """
        page = _FakePage(selectors={"a[href]": [
            ["首页", "https://example.com/"],
            ["", ""],                       # 空 href：不该留下
            ["只有一格"],                    # 形状不对：丢
            [None, "https://example.com/a"],
        ]})
        driver, _ = _driver(page)
        links = await driver.links()
        assert [(l.text, l.href) for l in links] == [
            ("首页", "https://example.com/"),
            ("", "https://example.com/a"),
        ]

    async def test_按_limit_截断(self):
        page = _FakePage(selectors={"a[href]": [
            [str(i), f"https://example.com/{i}"] for i in range(10)
        ]})
        driver, _ = _driver(page)
        links = await driver.links(limit=3)
        assert len(links) == 3
        assert links[0].text == "0"

    async def test_按_href_去重保留第一条(self):
        """实测对齐：obscura 的 ``browser_links`` 恒 ``total == distinct``（131/131、
        33/33），不去重时 camoufox 的 ``links_total`` 比它高 28–37%——
        而 ``links_total`` 要进 D-2 口径，两边不同义就是并池比出来的分布是假的。"""
        page = _FakePage(selectors={"a[href]": [
            ["第一条", "https://example.com/a"],
            ["重复", "https://example.com/a"],
            ["第二条", "https://example.com/b"],
        ]})
        driver, _ = _driver(page)
        links = await driver.links()
        assert [(l.text, l.href) for l in links] == [
            ("第一条", "https://example.com/a"),
            ("第二条", "https://example.com/b"),
        ]

    async def test_去重后再按_limit_截断(self):
        page = _FakePage(selectors={"a[href]": [
            ["a", "https://example.com/a"], ["a2", "https://example.com/a"],
            ["b", "https://example.com/b"], ["c", "https://example.com/c"],
        ]})
        driver, _ = _driver(page)
        links = await driver.links(limit=2)
        assert [l.href for l in links] == [
            "https://example.com/a", "https://example.com/b",
        ]

    async def test_取链接失败抛_DriverError_不返回空(self):
        """空元组 = 「页面上没有链接」，那是页面事实。

        能力故障写成页面事实，会让判断点 ① 去判「这页没有候选」——
        纯假负样本。``observe`` 会把它转成 ``degraded``。
        """
        page = _FakePage(selectors={"a[href]": PwError("Execution context destroyed")})
        driver, _ = _driver(page)
        with pytest.raises(DriverError):
            await driver.links()


class TestExtract:
    async def test_三种形态_普通_属性_数组(self):
        page = _FakePage(selectors={
            "h2": ["结果标题"],
            "a": ["https://example.com/1"],
            "li a": ["甲", "乙"],
        })
        driver, _ = _driver(page)
        out = await driver.extract({
            "title": "h2", "url": "a@href", "titles[]": "li a",
        })
        # 键名**不带** ``[]``——上游约定，抄错就抽不到（见 dom.parse_extract）
        assert out == {"title": "结果标题", "url": "https://example.com/1",
                       "titles": ["甲", "乙"]}

    async def test_属性走属性取值_文本走可见文本(self):
        """两条路同一个 API、**不同 JS**——只看选择器分不出走错了哪条。

        写错（对 ``a@href`` 取 innerText）的症状是「抽到一堆标题当 URL」，
        与「抽不到」完全不同，但都要靠这里分开。
        """
        page = _FakePage(selectors={"a": ["x"], "h2": ["y"]})
        driver, _ = _driver(page)
        await driver.extract({"url": "a@href", "title": "h2"})
        js_by_selector = dict(page.pluck_calls)
        assert "getAttribute" in js_by_selector["a"]
        assert "getAttribute" not in js_by_selector["h2"]
        assert "innerText" in js_by_selector["h2"]

    async def test_标量抽不到不写键_数组照写空数组(self):
        """不编造 ``""``：填满空串的数组会让 ``result_titles`` 把 N 条假配对
        当成 N 条好结果交出去，而判断点 ① 拿它当唯一判据。"""
        page = _FakePage(selectors={"missing": []})
        driver, _ = _driver(page)
        out = await driver.extract({"title": "missing", "titles[]": "missing"})
        assert "title" not in out
        assert out["titles"] == []

    async def test_取文本必须带_textContent_兜底(self):
        """实测（2026-10-20，bing 结果页 `li.b_algo h2 a`）：10 个锚的
        ``innerText`` **全是空串**，``textContent`` 才是标题。

        只取 innerText 的症状是「抽到 10 个空标题」→ 上层判「为空」→
        ① 退回 fail-closed：看着像选择器失效，其实是取文本的路窄了。
        """
        page = _FakePage(selectors={"h2": []})
        driver, _ = _driver(page)
        await driver.extract({"title": "h2"})
        js = page.pluck_calls[0][1]
        assert "innerText" in js and "textContent" in js

    async def test_链接文本同样带_textContent_兜底(self):
        assert "textContent" in CamoufoxDriver._LINKS_JS

    async def test_空_fields_不发请求(self):
        page = _FakePage()
        driver, _ = _driver(page)
        assert await driver.extract({}) == {}
        assert page.pluck_calls == []

    async def test_选择器语法错误抛_DriverError(self):
        """空映射 = 「抽不到」，而语法错是**配置 bug**——症状同形，处置相反。"""
        page = _FakePage(selectors={"oops": PwError("Unexpected token")})
        driver, _ = _driver(page)
        with pytest.raises(DriverError):
            await driver.extract({"x": "oops"})


class TestTabs:
    async def test_new_tab_压栈并切成当前页(self):
        first = _FakePage(url="https://example.com/mid")
        second = _FakePage(url="https://example.com/ext")
        session = _FakeSession(first, new_pages=[second])
        driver = CamoufoxDriver(session)          # type: ignore[arg-type]
        tab = await driver.new_tab("https://example.com/ext")
        assert tab == "c1"
        assert session.pages == (first, second)
        assert session.page is second
        assert second.goto_calls == [("https://example.com/ext", "load")]
        assert driver.last_tab["outcome"] == "ok"
        assert driver.last_tab["pages"] == "2"

    async def test_new_tab_导航失败收掉新页并回原页(self):
        """留着那张 about:blank，后面每条 observe 都在读空页——
        一次外链取不到会连带把中间页的证据弄丢。"""
        first = _FakePage(url="https://example.com/mid")
        second = _FakePage(fail_goto=True)
        session = _FakeSession(first, new_pages=[second])
        driver = CamoufoxDriver(session)          # type: ignore[arg-type]
        with pytest.raises(DriverError):
            await driver.new_tab("https://example.com/ext")
        assert session.pages == (first,)
        assert session.page is first
        assert second.closed is True
        assert driver.last_tab["outcome"] == "failed"

    async def test_close_tab_回到上一页(self):
        first = _FakePage(url="https://example.com/mid")
        second = _FakePage(url="https://example.com/ext")
        session = _FakeSession(first, new_pages=[second])
        driver = CamoufoxDriver(session)          # type: ignore[arg-type]
        await driver.new_tab("https://example.com/ext")
        await driver.close_tab()
        assert session.page is first
        assert second.closed is True
        assert driver.last_tab["outcome"] == "ok"
        assert driver.last_tab["tab"] == "c1"

    async def test_close_tab_只剩一页时先补空白页(self):
        """``page`` 必须永远可用：关空了之后每条 observe 都会读已关闭句柄。"""
        first = _FakePage()
        session = _FakeSession(first)
        driver = CamoufoxDriver(session)          # type: ignore[arg-type]
        await driver.close_tab()
        assert first.closed is True
        assert len(session.pages) == 1
        assert session.page is not first

    async def test_close_tab_不认识的_id_抛而不是猜一个关(self):
        """关错页的后果是后续 observe 读到别的页——**不报错**。"""
        first = _FakePage()
        session = _FakeSession(first)
        driver = CamoufoxDriver(session)          # type: ignore[arg-type]
        with pytest.raises(DriverError):
            await driver.close_tab("c99")
        assert session.pages == (first,)


class TestObserve:
    async def test_字段齐全且与_obscura_同口径(self):
        page = _FakePage(
            url="https://example.com/watch",
            title="示例影片",
            evals={INNER_TEXT_EXPR: "正文内容"},
            aria=ARIA_SAMPLE,
            selectors={"a[href]": [["首页", "https://example.com/"]]},
            selector_counts={"video, audio": 2, "iframe": 1},
        )
        driver, _ = _driver(page)
        obs = await driver.observe(max_chars=100)

        assert obs.url == "https://example.com/watch"
        assert obs.page_title == "示例影片"
        assert obs.body_text == "正文内容"
        assert len(obs.links) == 1 and obs.links[0].href == "https://example.com/"
        assert obs.links_total == 1
        assert len(obs.interactive_elements) == 3      # ARIA_SAMPLE 过滤后剩 3 个
        assert obs.elements_total == 3
        assert obs.video_tag_count == 2
        assert obs.iframe_count == 1
        assert obs.degraded == ()
        # 媒体选择器**与 obscura 共用同一对常量**（迁到 dom.py 的唯一理由）
        assert page.locator_calls == ["video, audio", "iframe"]

    async def test_采集项失败记降级不抛(self):
        """snapshot 之外的可选项失败 → 空 + ``degraded``，不许整次作废。

        但不许**静默**：``degraded`` 非空会让感知层走 fail-closed，
        那才是「没采到」的正确形态。
        """
        page = _FakePage(
            evals={INNER_TEXT_EXPR: "正文"},
            aria=PwError("aria snapshot 失败"),
            selectors={"a[href]": PwError("链接失败")},
            selector_counts={"video, audio": PwError("count 失败")},
        )
        driver, _ = _driver(page)
        obs = await driver.observe(max_chars=100)
        assert obs.body_text == "正文"            # snapshot 成功就不抛
        assert obs.links == ()
        assert obs.interactive_elements == ()
        assert obs.video_tag_count == 0
        assert obs.degraded == ("links", "interactive", "count:video, audio")

    async def test_snapshot_失败仍然抛(self):
        """snapshot 是唯一必需项（I4）——降级成空观察等于「这页什么都没有」。"""
        driver, _ = _driver(_FakePage(fail_title=True, evals={INNER_TEXT_EXPR: "正文"}))
        with pytest.raises(DriverError):
            await driver.observe(max_chars=100)


class Test超时预算与不重试:
    """动作超时这条纪律（2026-10-21）。

    背景：headful 窗口不可渲染时（最小化 / 息屏 / 锁屏 / 完全遮挡），
    Firefox 降频渲染与输入派发，页内 ``requestAnimationFrame`` 滞后从
    4–17ms 涨到 902ms–62s，单次 ``locator.click`` 随之从 0.4–1.5s
    涨到 **5–13s**（``research/probes/click_occluded.json``：
    13323 / 9103 / 9063ms，且**事件照样落地**）。

    原来的 5s 预算必然超时，而超时的点击被下游读成
    ``component_unverified``——「理由通顺的假负样本」。

    两条纪律因此定死：**预算要盖住实测上界**、**超时不许重试**。
    """

    def test_预算盖得住实测上界(self):
        """实测上界 13323ms（``click_occluded.json`` 三组里最慢的一组）。

        预算低于它 ⇒ 无人值守时那组点击必然超时 ⇒ 假负样本。
        无头口径下实测 34–71ms，永远碰不到上限，所以放宽是无代价的。
        """
        assert ACTION_TIMEOUT_MS > 13323

    def test_预算不是旧的五秒(self):
        """5s 是**反转前**的数。留着它等于把假负样本的成因留在这里。"""
        assert ACTION_TIMEOUT_MS != 5000

    async def test_点击超时后_不重试(self):
        """超时的点击**事件可能已经落地**（探针实测 ``pointerdown`` 计数上涨）。

        重试 = 点两次。所以驱动层必须**一次就抛**，把这次尝试原样记进账目。
        """
        page = _FakePage(
            role_counts={
                ("button", "立即播放"): (
                    1, PwError("Locator.click: Timeout 15000ms exceeded"),
                )
            }
        )
        driver, _ = _driver(page)
        with pytest.raises(DriverError):
            await driver.click(role="button", name="立即播放")
        # **一次**。两次就说明有人在重试——那会把「点了但点不动」变成「点了两次」。
        assert len(page.actions) == 1

    async def test_超时用_的是新预算(self):
        """传给 playwright 的 timeout 必须就是那个新预算。

        写死 5000 的地方（比如某个调用点自己传了 timeout）会让上面的断言
        全部作废——所以连「传了什么」一起钉住。
        """
        page = _FakePage()
        driver, _ = _driver(page)
        await driver.click(role="button", name="立即播放")
        verb, _role, _name, _detail, timeout = page.actions[0]
        assert verb == "click"
        assert timeout == ACTION_TIMEOUT_MS

class TestResolve:
    """**只定位不动作**——判据⑥（动作可移植性）的重放探针用的那个口。

    它的价值在于**不执行**：点击不可逆（可能点进另一个视频、
    可能触发付费流程），而可移植性问的是「**还能不能定位到**」。

    判定必须与 :meth:`click` **走同一条路径**（同一把尺子）——
    探针另写一份的话，「重放定位率」就不是生产链的读数了。
    """

    async def test_命中一个算_ok_且不动作(self):
        page = _FakePage()
        driver, _ = _driver(page)
        res = await driver.resolve(role="button", name="立即播放")
        assert res == {"outcome": "ok", "matches": 1,
                       "role": "button", "name": "立即播放"}
        # **没点**——这是这个口存在的理由
        assert page.actions == []
        assert page.role_calls == [("button", "立即播放", True)]

    async def test_命中不到算_not_found_且不动作(self):
        page = _FakePage(role_counts={("button", "立即播放"): (0, None)})
        driver, _ = _driver(page)
        res = await driver.resolve(role="button", name="立即播放")
        assert res["outcome"] == "not_found"
        assert res["matches"] == 0
        assert page.actions == []

    async def test_真同名算_ambiguous_并带命中数(self):
        page = _FakePage(role_counts={("link", "50集全 电视剧"): (3, None)})
        driver, _ = _driver(page)
        res = await driver.resolve(role="link", name="50集全 电视剧")
        assert res["outcome"] == "ambiguous"
        assert res["matches"] == 3
        assert page.actions == []

    async def test_空_name_直接判不存在且不往返(self):
        page = _FakePage()
        driver, _ = _driver(page)
        res = await driver.resolve(role="link", name="")
        assert res["outcome"] == "not_found"
        assert page.role_calls == []

    async def test_词表外的_role_直接判不存在(self):
        page = _FakePage()
        driver, _ = _driver(page)
        res = await driver.resolve(role="heading", name="影片介绍")
        assert res["outcome"] == "not_found"
        assert page.role_calls == []

    async def test_count_报错是能力故障不是元素不存在(self):
        """与动作面同一条纪律：定位能力不可用**不许**折叠成
        「元素不存在」——两者的正确处置相反。"""
        page = _FakePage(
            role_counts={("button", "立即播放"): (PwError("Target closed"), None)}
        )
        driver, _ = _driver(page)
        with pytest.raises(DriverError):
            await driver.resolve(role="button", name="立即播放")

    async def test_账目不许出现成功与不存在同形(self):
        """``ok`` 这一支必须自己记一份账——否则「定位到了」与
        「没定位到」在 ``last_action`` 里同形（后者是三态里的一态）。"""
        page = _FakePage()
        driver, _ = _driver(page)
        await driver.resolve(role="button", name="立即播放")
        assert driver.last_action["outcome"] == "ok"
        assert driver.last_action["op"] == "resolve"


# ── 动作之后的读稳（2026-10-22，第一批 camoufox 生产批实测）───────
#
# 这一节治的病：**读到一半**。
#
# 实测：同一页、同一动作位置、两次独立运行，``iframe_count`` 读出 **3** 与 **0**。
# 同一个页面的计数在两次运行间翻面 ⇒ 那个 0 **不是页面事实**。
#
# 机制：``observe`` 是 4 次以上独立往返，而导航是**异步 commit** 的——
# commit 落在两次往返之间，观察就是**两个文档拼起来的**。
#
# ⇒ 治法两条：① 动作之后**等新文档 commit** 再观察（判据 = **文档代号**
# ``performance.timeOrigin``，每文档一个常量）；② 观察侧**读数自检**：
# 读数跨了文档就重来，一直不稳就记降级让下游**不判**。
#
# ⚠️ **判据为什么不是「主框架 ``framenavigated``」**：它对**同文档导航**
# （pushState / 改 query）也会触发，而 SPA 页面会在播放时改 URL——
# 拿它当判据会把「只改了 URL」误判成「换了文档」，观察白白重来，
# 连续撞上还误记 ``nav_unsettled``，**把好观察当半成品丢掉**。
# 下面 ``test_同文档导航不算换文档`` 就是钉这一条的。


def _navigate_on_read(page: _FakePage, *, times: int = 1) -> str:
    """正文读数时触发导航——「读到一半」的最小复现。

    真场景里导航落在两次往返之间；这里让它落在**读正文**这一次往返里，
    于是这一次观察的 URL/正文来自旧文档、链接/计数来自新文档。
    """
    state = {"left": times}

    def _body() -> str:
        if state["left"] > 0:
            state["left"] -= 1
            page.navigate()
        return "正文"

    return _body


class Test动作之后的读稳:
    @pytest.fixture(autouse=True)
    def _短预算(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """等 commit 的预算压到 50ms——测「没等到」那条不该真等 2s。"""
        monkeypatch.setattr(camoufox_driver, "NAV_COMMIT_TIMEOUT_MS", 50)

    async def test_动作之后等新文档_commit_账上写_new(self):
        """点击触发导航 ⇒ 驱动**等它 commit**（而不是立刻观察旧页）。"""
        page = _FakePage()
        driver, _ = _driver(page)
        await driver.click(role="button", name="立即播放")
        assert page.navigations == 1
        assert driver.last_action["nav"] == "new"

    async def test_没跳页时写_same_不是_new_也不是猜(self):
        """点击没导航 ⇒ 账上写 ``same``。

        ⚠️ **不许写 ``new``**——那会把「没跳页」说成「跳了」；
        也不许写 ``unknown``——那个是「**不知道**」，与「知道没跳」不同。
        """
        page = _FakePage(navigate_verbs=())
        driver, _ = _driver(page)
        await driver.click(role="button", name="立即播放")
        assert driver.last_action["nav"] == "same"

    async def test_同文档导航不算换文档(self):
        """**pushState（改 query 不换文档）不算「换了文档」**。

        这是选「文档代号」而不是「导航事件」的全部理由：SPA 页面
        （bilibili 等）会在播放时改 URL——拿导航事件当判据会把好观察
        当半成品丢掉（白白重来，连续撞上还误记 ``nav_unsettled``）。
        """
        page = _FakePage()
        driver, _ = _driver(page)
        page.push_state()          # 改 URL，但代号不变
        assert await driver._doc_stamp() == "1000"
        # 读数自检也不该把它当成「读到一半」
        obs = await driver.observe()
        assert DEGRADED_NAV_UNSETTLED not in obs.degraded

    async def test_子框架导航不算换文档(self):
        """播放器 iframe 换文档**不是**「当前页换了文档」。

        把它算进去会让读数自检无谓地重来（每次都等一次落定）。
        """
        page = _FakePage()
        driver, _ = _driver(page)
        before = await driver._doc_stamp()
        page.navigate(main=False)          # 子框架换文档
        assert await driver._doc_stamp() == before

    async def test_取不到基线时写_unknown_不猜(self):
        """代号**读不到** ⇒ 没有基线 ⇒ **不猜、也不等**。

        没有基线就没有「变了没有」可判——这时候写 ``same`` 就是
        把「不知道」说成「没跳页」。
        """
        page = _FakePage(fail_stamp=True)
        driver, _ = _driver(page)
        await driver.click(role="button", name="立即播放")
        assert driver.last_action["nav"] == "unknown"

    async def test_页面没有_performance_时不是_读不稳(self):
        """代号恒为 ``"none"``（页面没有 ``performance``）⇒ **不误伤**。

        ``"none"`` 是**读到了**而且**恒定**——恒定的东西永远判不出
        「变了」。混成「读不到」的话，每次观察都重来、每次都记降级
        ⇒ 整站一份样本都留不下。
        """
        page = _FakePage(no_performance=True)
        driver, _ = _driver(page)
        await driver.click(role="button", name="立即播放")
        assert driver.last_action["nav"] == "same"
        obs = await driver.observe()
        assert DEGRADED_NAV_UNSETTLED not in obs.degraded

    async def test_fill_也等_因为页面脚本可能因输入跳页(self):
        """``fill`` 只设值，但 agent 链里 ``fill`` 之后**下一步就是观察**
        ——不等的代价是读到旧页，下一步拿旧页的名字去点新页。"""
        page = _FakePage(navigate_verbs=("fill",))
        driver, _ = _driver(page)
        await driver.fill(role="textbox", name="站内搜索", text="功夫")
        assert driver.last_action["nav"] == "new"

    async def test_读数跨了文档就重来一次(self):
        """导航落在读数中间 ⇒ 那份观察是半成品 ⇒ **重来**。

        重来之后拿到的是**干净**的观察 ⇒ 不带降级标记。
        """
        page = _FakePage()
        page.evals[INNER_TEXT_EXPR] = _navigate_on_read(page)
        driver, _ = _driver(page)
        obs = await driver.observe()
        assert page.navigations == 1          # 第一次读坏了，第二次没坏
        assert DEGRADED_NAV_UNSETTLED not in obs.degraded

    async def test_一直读不稳就记降级让下游不判(self):
        """读几遍都不稳 ⇒ 这份观察**不可信** ⇒ 记降级。

        降级 ⇒ 感知层判 ``answer=None`` ⇒ 落 ``unresolved``，
        **不进负样本池**——半成品绝不能变成业务负样本。
        """
        page = _FakePage()
        page.evals[INNER_TEXT_EXPR] = _navigate_on_read(page, times=99)
        driver, _ = _driver(page)
        obs = await driver.observe()
        assert DEGRADED_NAV_UNSETTLED in obs.degraded

    async def test_读数途中执行上下文被销毁也算读到一半(self):
        """代号**读不到**（执行上下文被销毁）⇒ **正在换文档** ⇒ 也要重来。

        这是比「代号变了」更早的信号：导航还没 commit 完时就读，
        那一刻的读数跨着两个文档。
        """
        page = _FakePage()
        driver, _ = _driver(page)
        # 让「读正文」那一次往返触发「执行上下文被销毁」：
        # 代号读不到 ⇒ 驱动读不出代号 ⇒ 判不稳 ⇒ 重来。
        state = {"left": 1}

        def _body() -> str:
            if state["left"] > 0:
                state["left"] -= 1
                page.fail_stamp = True
            return "正文"

        page.evals[INNER_TEXT_EXPR] = _body
        obs = await driver.observe()
        # 读不稳 ⇒ 记降级（绝不让半成品变成业务负样本）
        assert DEGRADED_NAV_UNSETTLED in obs.degraded
