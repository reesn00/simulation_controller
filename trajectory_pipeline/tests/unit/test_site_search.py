"""``executor/steps/site_search.py`` 单测——最小站内搜索。

**不碰真浏览器**：值得测的是四条失败形态的分流（no_box /
no_results / routed / unresolved 不合并）、动作流（fill → press →
重试按钮）与子候选的溯源字段（origin / parent）。
"""

from __future__ import annotations

from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.steps import search
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.executor.steps.site_search import (
    NO_BOX,
    NO_RESULTS,
    ROUTED,
    UNRESOLVED,
    _find_search_button,
    _heuristic_results,
    _selected_candidates,
    site_search,
)
from trajectory_pipeline.perception.base import (
    Decision,
    InteractiveElement,
    LinkItem,
    Observation,
    Q,
)


# ═══════════════════════════════════════════════════════════════════════
# Fake
# ═══════════════════════════════════════════════════════════════════════


class FakeDriver:
    """``press_key`` / ``click`` 可配置导航目标的 driver。

    ``on_press`` / ``on_click``：按键 / 点击后切换到哪个观察。
    不配置 = 页面不变（提交失败的形态）。
    """

    name = "fake"

    def __init__(
        self,
        *,
        on_press: Observation | None = None,
        on_click: Observation | None = None,
    ) -> None:
        self.on_press = on_press
        self.on_click = on_click
        self.current: Observation | None = None
        self.typed: list[tuple[str, str]] = []
        self.pressed: list[str] = []
        self.clicked: list[str] = []

    async def type_text(self, *, text, ref=None, selector=None) -> None:
        self.typed.append((ref or selector or "", text))

    async def press_key(self, key: str) -> None:
        self.pressed.append(key)
        if self.on_press is not None:
            self.current = self.on_press

    async def click(self, *, ref=None, selector=None) -> None:
        self.clicked.append(ref or selector or "")
        if self.on_click is not None:
            self.current = self.on_click

    async def observe(self, *, max_chars=None) -> Observation:
        assert self.current is not None
        return self.current


class ScriptedPerceptor:
    """按题返回预设答案。"""

    name = "scripted"

    def __init__(self, script: dict[str, Decision]) -> None:
        self.script = script
        self.calls: list[str] = []

    def decide(self, question: str, obs: Observation) -> Decision:
        self.calls.append(question)
        d = self.script.get(question)
        if d is None:
            return Decision(question, None, 1.0, "未配置", "rule", {})
        return Decision(
            question, d.answer, 1.0, d.evidence, "rule", dict(d.payload)
        )


def ok(question, payload=None, evidence="e"):
    return Decision(question, True, 1.0, evidence, "rule", payload or {})


def no(question, evidence="e"):
    return Decision(question, False, 1.0, evidence, "rule", {})


def none_(question, evidence="e"):
    return Decision(question, None, 1.0, evidence, "rule", {})


def elements(*pairs):
    return tuple(InteractiveElement(ref=r, tag=t, label=l) for r, t, l in pairs)


def obs_at(
    url,
    *,
    title="T",
    body="正文",
    elements=(),
    links=(),
) -> Observation:
    return Observation(
        url=url,
        page_title=title,
        body_text=body,
        interactive_elements=tuple(
            InteractiveElement(ref=r, tag=t, label=l) for r, t, l in elements
        ),
        links=tuple(LinkItem(text=t, href=h) for t, h in links),
    )


HOME = "https://v.test/"
RESULTS = "https://v.test/search?q=%E5%8A%9F%E5%A4%AB"

HOME_OBS = obs_at(
    HOME,
    elements=(("e1", "input", "搜索片名"), ("e2", "a", "首页")),
)
RESULTS_OBS = obs_at(
    RESULTS,
    body="功夫 搜索结果",
    links=(
        ("功夫全集", "https://v.test/play/1"),
        ("功夫预告", "https://v.test/play/2"),
    ),
)
CAND = Candidate(url=HOME, text="影视站", rank=1)


async def run(driver, perceptor, *, title="功夫", candidate=CAND):
    ledger = RunLedger("T001")
    steplog = StepLog()
    stats: dict[str, int] = {}
    result = await site_search(
        driver,
        perceptor,
        ledger,
        steplog,
        candidate,
        HOME_OBS,
        title,
        stats=stats,
    )
    return result, ledger, steplog, stats


# ═══════════════════════════════════════════════════════════════════════
# 入口守卫
# ═══════════════════════════════════════════════════════════════════════


class TestGuards:
    async def test_空片名_unresolved(self):
        d = FakeDriver()
        result, ledger, _, stats = await run(
            d, ScriptedPerceptor({}), title="  "
        )
        assert result.status == UNRESOLVED
        assert ledger.outcomes[0].branch == "unresolved"
        assert stats.get("site_search_empty_title") == 1
        assert d.typed == []  # 没填词就退出

    async def test_无搜索框_no_box(self):
        d = FakeDriver()
        result, ledger, _, _ = await run(
            d, ScriptedPerceptor({Q.FIND_SITE_SEARCH: no(Q.FIND_SITE_SEARCH)})
        )
        assert result.status == NO_BOX
        assert ledger.outcomes[0].branch == "no_site_search"
        assert d.typed == []

    async def test_搜索框判不出_fail_closed(self):
        """None ≠ False：判不出不是「这个站没有搜索框」。"""
        d = FakeDriver()
        result, ledger, _, _ = await run(
            d, ScriptedPerceptor({Q.FIND_SITE_SEARCH: none_(Q.FIND_SITE_SEARCH)})
        )
        assert result.status == UNRESOLVED
        assert ledger.outcomes[0].branch == "unresolved"

    async def test_answer_true_无ref_契约违规_unresolved(self):
        """I6：answer=True 必须带可溯源 ref，缺了不猜框在哪。"""
        d = FakeDriver()
        result, ledger, _, _ = await run(
            d, ScriptedPerceptor({Q.FIND_SITE_SEARCH: ok(Q.FIND_SITE_SEARCH, {})})
        )
        assert result.status == UNRESOLVED
        assert "I6" in ledger.outcomes[0].evidence


# ═══════════════════════════════════════════════════════════════════════
# 提交与路由
# ═══════════════════════════════════════════════════════════════════════


class TestRouting:
    async def test_正常路由_routed(self):
        d = FakeDriver(on_press=RESULTS_OBS)
        d.current = HOME_OBS
        p = ScriptedPerceptor(
            {
                Q.FIND_SITE_SEARCH: ok(
                    Q.FIND_SITE_SEARCH, {"ref": "e1", "why": "搜索框"}
                ),
                Q.SELECT_PLAY_SITES: ok(
                    Q.SELECT_PLAY_SITES,
                    {
                        "selected": [{"url": "https://v.test/play/1"}],
                        "rejected": [],
                    },
                ),
            }
        )
        result, ledger, steplog, _ = await run(d, p)
        assert result.status == ROUTED
        assert [c.url for c in result.selected] == ["https://v.test/play/1"]
        # 溯源字段：发现链出了问题能从候选回溯到结果页
        assert all(c.origin == "site_search" for c in result.selected)
        assert all(c.parent == RESULTS for c in result.selected)
        # 动作流：fill → press，观察回填
        tools = [s.action.tool for s in steplog.steps]
        assert tools == ["fill", "press"]
        assert d.typed == [("e1", "功夫")]
        assert d.pressed == ["Enter"]
        # 路由记账是 NON_SAMPLE 分支，question 为空（代码选用）
        routed = [o for o in ledger.outcomes if o.branch == "site_search_routed"]
        assert len(routed) == 1
        assert routed[0].question == ""

    async def test_结果页选题_false_no_results(self):
        """搜到了结果页但选题判 False——真结论，记 no_results。"""
        d = FakeDriver(on_press=RESULTS_OBS)
        d.current = HOME_OBS
        p = ScriptedPerceptor(
            {
                Q.FIND_SITE_SEARCH: ok(
                    Q.FIND_SITE_SEARCH, {"ref": "e1", "why": "搜索框"}
                ),
                Q.SELECT_PLAY_SITES: no(Q.SELECT_PLAY_SITES),
            }
        )
        result, ledger, _, stats = await run(d, p)
        assert result.status == NO_RESULTS
        assert result.selected == []
        nr = [o for o in ledger.outcomes if o.branch == "site_search_no_results"]
        assert len(nr) == 1
        assert stats.get("site_search_no_results") == 1

    async def test_结果页选题_none_启发式全量(self):
        """fail-closed 不中断采集：启发式按 href 去重全量提取。"""
        d = FakeDriver(on_press=RESULTS_OBS)
        d.current = HOME_OBS
        p = ScriptedPerceptor(
            {
                Q.FIND_SITE_SEARCH: ok(
                    Q.FIND_SITE_SEARCH, {"ref": "e1", "why": "搜索框"}
                ),
                Q.SELECT_PLAY_SITES: none_(Q.SELECT_PLAY_SITES),
            }
        )
        result, ledger, _, _ = await run(d, p)
        assert result.status == ROUTED
        assert {c.url for c in result.selected} == {
            "https://v.test/play/1",
            "https://v.test/play/2",
        }
        assert any("启发式" in n for n in result.notes)

    async def test_回车未触发_改点搜索按钮(self):
        """同页判定 + 第二条提交路径。按钮词表命中「搜索」。"""
        after_button = RESULTS_OBS
        d = FakeDriver(on_click=after_button)  # press 不换页，click 换
        home_with_button = obs_at(
            HOME,
            elements=(
                ("e1", "input", "搜索片名"),
                ("e3", "button", "搜 索"),
            ),
        )
        d.current = home_with_button
        p = ScriptedPerceptor(
            {
                Q.FIND_SITE_SEARCH: ok(
                    Q.FIND_SITE_SEARCH, {"ref": "e1", "why": "搜索框"}
                ),
                Q.SELECT_PLAY_SITES: ok(
                    Q.SELECT_PLAY_SITES,
                    {"selected": [{"url": "https://v.test/play/1"}], "rejected": []},
                ),
            }
        )
        ledger = RunLedger("T001")
        steplog = StepLog()
        result = await site_search(
            d, p, ledger, steplog, CAND, home_with_button, "功夫"
        )
        assert result.status == ROUTED
        tools = [s.action.tool for s in steplog.steps]
        assert tools == ["fill", "press", "click"]
        assert d.clicked == ["e3"]

    async def test_两条提交路都失败_unresolved(self):
        """提交失败 ≠ 没结果：页面未变且无搜索按钮可点。"""
        d = FakeDriver()  # press / click 都不换页
        d.current = HOME_OBS
        p = ScriptedPerceptor(
            {
                Q.FIND_SITE_SEARCH: ok(
                    Q.FIND_SITE_SEARCH, {"ref": "e1", "why": "搜索框"}
                ),
            }
        )
        result, ledger, _, stats = await run(d, p)
        assert result.status == UNRESOLVED
        assert "提交失败" in ledger.outcomes[0].evidence or (
            ledger.outcomes[0].branch == "unresolved"
        )
        assert stats.get("site_search_submit_failed") == 1


# ═══════════════════════════════════════════════════════════════════════
# 纯函数
# ═══════════════════════════════════════════════════════════════════════


class TestFindSearchButton:
    def test_词表命中(self):
        els = elements(("e1", "button", "搜 索"), ("e2", "a", "首页"))
        assert _find_search_button(els).ref == "e1"

    def test_空label不命中(self):
        els = elements(("e1", "input", ""))
        assert _find_search_button(els) is None

    def test_非可点标签不命中(self):
        els = elements(("e1", "span", "搜索"))
        assert _find_search_button(els) is None


class TestHeuristicResults:
    def test_按href去重_同站深层页都保留(self):
        """与直搜链的刻意差异：站内结果页全是本站，按 host 去重
        会把整页结果折成一条。"""
        obs = obs_at(
            RESULTS,
            links=(
                ("功夫1", "https://v.test/play/1"),
                ("功夫2", "https://v.test/play/2"),
                ("重复", "https://v.test/play/1"),
            ),
        )
        got = _heuristic_results(obs, engine="baidu", limit=10)
        assert [c.url for c in got] == [
            "https://v.test/play/1",
            "https://v.test/play/2",
        ]

    def test_引擎域剔除(self):
        obs = obs_at(
            RESULTS,
            links=(("百度", "https://www.baidu.com/x"),),
        )
        assert _heuristic_results(obs, engine="baidu", limit=10) == []

    def test_上限生效(self):
        obs = obs_at(
            RESULTS,
            links=tuple(
                (f"项{n}", f"https://v.test/play/{n}") for n in range(5)
            ),
        )
        assert len(_heuristic_results(obs, engine="baidu", limit=2)) == 2


class TestSelectedCandidates:
    def test_原始href与解包url双键匹配(self):
        """感知层读原始 href，遍历用解包地址——少试一个键会整批丢失。"""
        redirect = "https://www.baidu.com/link?url=https%3A%2F%2Fv.test%2Fplay%2F1"
        obs = obs_at(
            RESULTS,
            links=(("功夫", redirect),),
        )
        got, unmatched = _selected_candidates(
            ["https://v.test/play/1"], obs, limit=5
        )
        assert [c.url for c in got] == ["https://v.test/play/1"]
        assert unmatched == 0
        assert got[0].source_href == redirect

    def test_点名但页面上找不到_计unmatched(self):
        obs = obs_at(RESULTS, links=(("功夫", "https://v.test/play/1"),))
        got, unmatched = _selected_candidates(
            ["https://v.test/play/99"], obs, limit=5
        )
        assert got == []
        assert unmatched == 1

    def test_子候选带溯源(self):
        obs = obs_at(RESULTS, links=(("功夫", "https://v.test/play/1"),))
        got, _ = _selected_candidates(
            ["https://v.test/play/1"], obs, limit=5
        )
        assert got[0].origin == "site_search"
        assert got[0].parent == RESULTS


class TestSearchBoxWordlist:
    """rule_perceptor 的搜索框词表（FIND_SITE_SEARCH 的代码侧兜底）。"""

    def test_input带搜索词命中(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            search_box_candidates,
        )

        obs = obs_at(
            HOME, elements=(("e1", "input", "搜索片名"), ("e2", "a", "首页"))
        )
        got = search_box_candidates(obs)
        assert [e.ref for e in got] == ["e1"]

    def test_按钮不进词表_填词ref必须可输入(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            search_box_candidates,
        )

        obs = obs_at(HOME, elements=(("e3", "button", "搜索"),))
        assert search_box_candidates(obs) == []

    def test_泛输入词不命中(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            search_box_candidates,
        )

        obs = obs_at(HOME, elements=(("e1", "input", "请输入邮箱地址"),))
        assert search_box_candidates(obs) == []
