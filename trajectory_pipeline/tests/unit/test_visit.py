"""``executor`` 控制流的离线单测——用 fake driver 跑完整条链。

**不碰真浏览器**。理由：`visit_site` 里值得测的不是 obscura 的返回格式
（那是 ``test_dom.py`` 的事），而是**控制流的正确性**——分支该走哪条、
记账对不对、点击后有没有重新观察。用 fake 是把这些测得又快又全的办法。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from trajectory_pipeline.common.elements import (
    ELEMENT_VOCAB_ARIA_ROLE,
    ELEMENT_VOCAB_HTML_TAG,
)
from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import StepLog
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.executor import orchestrator
from trajectory_pipeline.executor.orchestrator import Orchestrator, RunConfig, RunRecord
from trajectory_pipeline.executor.steps import search as search_step
from trajectory_pipeline.executor.steps import site_search as site_search_step
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.executor.steps.visit import visit_site
from trajectory_pipeline.perception import llm_perceptor as lp
from trajectory_pipeline.perception.base import (
    Decision,
    InteractiveElement,
    LinkItem,
    Observation,
    Perceptor,
    Q,
)
from trajectory_pipeline.perception.rule_perceptor import RulePerceptor


# ═══════════════════════════════════════════════════════════════════════
# Fake
# ═══════════════════════════════════════════════════════════════════════


class FakeDriver:
    """按 URL 返回预设观察的 driver。

    ``pages`` 是 ``url → Observation`` 映射；未登记的 URL 抛
    :class:`DriverError`，用来模拟导航失败。
    """

    name = "fake"

    def __init__(self, pages: dict[str, Observation] | None = None) -> None:
        self.pages = pages or {}
        self.current: Observation | None = None
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.observe_calls = 0
        self.tabs: list[str] = []
        #: ``browser_extract`` 的返回值（``{titles, urls}``）。
        #: **默认 None = 不支持 extract**——保持既有测试里
        #: ``_result_titles`` 一律返回空的行为不变；需要覆盖
        #: 「标题写回观察」那条路径时显式传进来。
        self.extracted: dict | None = None

    async def extract(self, fields) -> dict:
        if self.extracted is None:
            raise DriverError("fake: 不支持 browser_extract")
        return self.extracted

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        self.visited.append(url)
        if url not in self.pages:
            raise DriverError(f"fake: {url} 不存在")
        self.current = self.pages[url]

    async def snapshot(self, *, max_chars=None):
        from trajectory_pipeline.executor.dom import CleanedSnapshot

        c = self.current
        return CleanedSnapshot(
            url=c.url,
            title=c.page_title,
            body=c.body_text,
            raw_len=len(c.body_text),
            stripped_ratio=1.0,
            truncated=c.truncated,
            max_chars=max_chars,
            body_source=c.body_source,
        )

    async def links(self, *, limit=50):
        return ()

    async def interactive(self, *, limit=50):
        return self.current.interactive_elements if self.current else ()

    async def count(self, selector: str) -> int:
        return 0

    async def evaluate(self, expression: str):
        return None

    async def click(self, *, ref=None, selector=None) -> None:
        # 点击后**换页**——这正是「必须重新 observe」要捕获的场景
        self.clicked.append(ref or selector or "")
        target = f"https://x.test/play/{ref}"
        if target in self.pages:
            self.current = self.pages[target]

    async def fill(self, *, text, ref=None, selector=None, role=None, name=None) -> None: ...

    async def press(self, key: str, *, role=None, name=None) -> None: ...

    async def new_tab(self, url: str | None = None) -> str:
        self.tabs.append(url or "")
        return f"tab{len(self.tabs)}"

    async def close_tab(self, tab_id: str | None = None) -> None: ...

    async def observe(self, *, max_chars=None) -> Observation:
        self.observe_calls += 1
        return self.current

    async def close(self) -> None: ...


class ScriptedPerceptor:
    """按题返回预设答案。记录调用顺序。"""

    def __init__(self, script: dict[str, Decision]) -> None:
        self.script = script
        self.calls: list[str] = []

    name = "scripted"

    def decide(self, question: str, obs: Observation) -> Decision:
        self.calls.append(question)
        d = self.script.get(question)
        if d is None:
            return Decision(question, None, 1.0, "未配置", "rule", {})
        return Decision(question, d.answer, 1.0, d.evidence, "rule", d.payload)


def ok(question, payload=None, evidence="e"):
    return Decision(question, True, 1.0, evidence, "rule", payload or {})


def no(question, evidence="e", payload=None):
    """②/③ 答 False。

    ⚠️ **payload 决定落哪支**：② 答 False 而 payload 缺失 ⇒
    ``unresolved``（2026-10-21 增量七十四，H-15，owner 裁定）——
    「分不清是哪一种拦截」不是「有拦截」。带 ``access_control_type``
    时照旧落细分分支（真负样本不丢）。
    """
    return Decision(question, False, 1.0, evidence, "rule", payload or {})


def none(question, evidence="e"):
    return Decision(question, None, 1.0, evidence, "rule", {})


def obs_at(url, *, title="T", elements=(), body="正文", video=0, iframe=0, **kw):
    return Observation(
        url=url,
        page_title=title,
        body_text=body,
        interactive_elements=tuple(
            InteractiveElement(ref=r, role=t, name=l) for r, t, l in elements
        ),
        video_tag_count=video,
        iframe_count=iframe,
        **kw,
    )


CAND = Candidate(url="https://x.test/movie", text="电影", rank=1)


# ═══════════════════════════════════════════════════════════════════════
# visit_site 控制流
# ═══════════════════════════════════════════════════════════════════════


class TestVisitFlow:
    async def test_导航失败记_unreachable_hard(self):
        d = FakeDriver()  # 没有任何页面 → 全部导航失败
        ledger = RunLedger("T001")
        await visit_site(d, ScriptedPerceptor({}), ledger, CAND)
        assert ledger.outcomes[0].branch == "unreachable_hard"

    async def test_页面不可达_无细分_payload_记_unresolved(self):
        """⚠️ **② 答 False 而 payload 缺失 ⇒ ``unresolved``**（2026-10-21，
        H-15，owner 裁定「只落 unresolved，不新开分支」）。

        旧口径落 ``login_wall_or_blocked``——那一支的语义是
        「登录墙/验证码/地区限制」，而**三个前提一个都没观测到**。
        实测那 10 条因此以真负样本的身份进了池，而 LLM 自己的 evidence
        写的是「未加载出目标内容」——**与分支名直接矛盾**。
        """
        d = FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")})
        p = ScriptedPerceptor({Q.IS_REACHABLE: no(Q.IS_REACHABLE)})
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "unresolved"
        # **不是负样本** ⇒ 不进池
        assert ledger.outcomes[0].is_negative_sample is False
        assert ledger.negative_samples() == []

    async def test_页面不可达_带细分_payload_仍记真负样本(self):
        """兜底改 unresolved **只影响 payload 缺失那一路**——
        有 ``access_control_type`` 时照旧落细分分支。"""
        d = FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")})
        p = ScriptedPerceptor({
            Q.IS_REACHABLE: no(
                Q.IS_REACHABLE, payload={"access_control_type": "login"})
        })
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "login_required"
        assert ledger.outcomes[0].is_negative_sample is True

    async def test_无播放控件记_no_play_control(self):
        d = FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")})
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: no(Q.FIND_PLAY_CONTROL),
            }
        )
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "no_play_control"

    async def test_trailer_only_不进_播放页(self):
        d = FakeDriver(
            {
                "https://x.test/movie": obs_at(
                    "https://x.test/movie", elements=(("e1", "a", "预告片"),)
                )
            }
        )
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: ok(
                    Q.FIND_PLAY_CONTROL, {"ref": "", "trailer_only": True}
                ),
            }
        )
        ledger = RunLedger("T001")
        result = await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "trailer_only"
        assert d.clicked == []  # 预告片不该被点——它不是播放控件

    async def test_trailer_suspect_交人工不自动判负(self):
        """误杀不可逆：样本没了且没人知道它曾存在。

        payload 形状必须用 :class:`RulePerceptor` **真会发出的那种**：
        ``trailer_only = not suspects``，两者互斥。早先这里写的是
        ``trailer_only=True`` + 有 suspects，而感知层从不这样发——测试绿着，
        控制流里那条分支却一次都走不到（疑似预告会掉进
        ``component_unverified``，变成一条货真价实的假负样本）。
        """
        d = FakeDriver(
            {
                "https://x.test/movie": obs_at(
                    "https://x.test/movie", elements=(("e1", "a", "预告"),)
                )
            }
        )
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: ok(
                    Q.FIND_PLAY_CONTROL,
                    {
                        "ref": "",
                        "trailer_only": False,
                        "trailer_suspect": ["观看 trailer 解析"],
                    },
                ),
            }
        )
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "trailer_suspect"
        assert ledger.negative_samples() == []

    async def test_suspect_优先于_trailer_only(self):
        """真·规则版产出的 payload 走一遍控制流——
        疑似预告必须落 ``trailer_suspect`` 而不是被当成「无 ref 契约违规」。"""
        d = FakeDriver(
            {
                "https://x.test/movie": obs_at(
                    "https://x.test/movie", elements=(("e1", "a", "观看 trailer 解析"),)
                )
            }
        )
        p = RulePerceptor()
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "trailer_suspect"
        assert d.clicked == []

    async def test_answer_true_但无_ref_按_fail_closed_记账(self):
        """I6 被违反时不能硬点一个——ref 错会点到别的元素上。"""
        d = FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")})
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": ""}),
            }
        )
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "component_unverified"
        assert "I6" in ledger.outcomes[0].evidence
        assert d.clicked == []

    async def test_成功路径记_success(self):
        pages = {
            "https://x.test/movie": obs_at(
                "https://x.test/movie", elements=(("e5", "button", "播放"),)
            ),
            "https://x.test/play/e5": obs_at(
                "https://x.test/play/e5", title="播放器", video=1
            ),
        }
        d = FakeDriver(pages)
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e5"}),
                Q.PLAYER_OK: ok(Q.PLAYER_OK, {"media_count": 1}),
            }
        )
        ledger = RunLedger("T001")
        result = await visit_site(d, p, ledger, CAND)
        assert result.success
        assert ledger.outcomes[0].branch is None
        assert ledger.outcomes[0].reached is True
        assert d.clicked == ["e5"]

    async def test_点击后_必须重新_observe(self):
        """**这条最容易写错且最难发现**：不重新 observe 就判播放页，
        等于判的是站点页，点击结果完全没参与判定。"""
        pages = {
            "https://x.test/movie": obs_at(
                "https://x.test/movie", elements=(("e5", "button", "播放"),), video=0
            ),
            "https://x.test/play/e5": obs_at(
                "https://x.test/play/e5", title="播放器", video=3
            ),
        }
        d = FakeDriver(pages)
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e5"}),
                Q.PLAYER_OK: ok(Q.PLAYER_OK),
            }
        )
        ledger = RunLedger("T001")
        result = await visit_site(d, p, ledger, CAND)
        assert d.observe_calls == 2, "站点页 + 播放页各一次"
        # 第二次观察到的必须是播放页的 URL
        assert result.player_obs.url == "https://x.test/play/e5"
        assert result.player_obs.video_tag_count == 3

    async def test_点击失败记unresolved(self):
        """点击**失败** ⇒ 从未到达播放页 ⇒「播放页上没有可验证的播放组件」
        这个结论**没有新证据支撑**（``component_unverified`` 的前置事实是
        「到了播放页」）⇒ 记 ``unresolved``：**采集缺件 ≠ 业务负样本**
        （M-27）。

        ⚠️ 同页守卫在这条路上**结构性无从触发**——它要点击前后两份观察，
        而点击失败时只有一份。
        """
        pages = {
            "https://x.test/movie": obs_at(
                "https://x.test/movie", elements=(("e5", "button", "播放"),)
            )
        }
        d = FakeDriver(pages)

        async def boom(**kw):
            raise DriverError("click failed")

        d.click = boom

        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e5"}),
            }
        )
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        out = ledger.outcomes[0]
        assert out.branch == "unresolved"
        assert "点击" in out.evidence, "证据要留证——那是复看的现场"
        assert not out.is_negative_sample, "unresolved 不进负样本池"

    async def test_点击后取不到快照记unresolved(self):
        """点击**成功**了但快照取不到 ⇒ 我们**不知道**那是不是播放页
        ⇒ 同样缺「到了播放页」这个前置事实 ⇒ 记 ``unresolved``（M-27）。

        这条路原先**没有测试**——机制没修也看不出来。
        """
        pages = {
            "https://x.test/movie": obs_at(
                "https://x.test/movie", elements=(("e5", "button", "播放"),)
            )
        }

        class Boom(FakeDriver):
            async def observe(self, *, max_chars=None):
                self.observe_calls += 1
                if self.observe_calls >= 2:
                    raise DriverError("快照取不到")
                return self.current

        d = Boom(pages)
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e5"}),
            }
        )
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        out = ledger.outcomes[0]
        assert out.branch == "unresolved"
        assert "取不到页面快照" in out.evidence
        assert not out.is_negative_sample, "unresolved 不进负样本池"

    async def test_开_tab_失败不阻断(self):
        pages = {"https://x.test/movie": obs_at("https://x.test/movie")}
        d = FakeDriver(pages)

        async def boom(url=None):
            raise DriverError("tab failed")

        d.new_tab = boom

        # 带细分 payload ⇒ 落真负样本分支——**这样断言才证明
        # 「走到了判断环节」**（落 unresolved 分不清是 ② 判的还是别处兜的）
        p = ScriptedPerceptor({
            Q.IS_REACHABLE: no(
                Q.IS_REACHABLE, payload={"access_control_type": "login"})
        })
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "login_required"  # 走到了判断环节

    async def test_前置环节的_none_记_unresolved(self):
        """fail-closed：没判出来不是失败。

        范围是**环节①–④**（IS_REACHABLE / FIND_PLAY_CONTROL）——这三处没走到
        播放页。环节⑦ 是另一回事，见 ``TestPlayerUnverifiedAtStep7``。
        """
        for q in (Q.IS_REACHABLE, Q.FIND_PLAY_CONTROL):
            d = FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")})
            ledger = RunLedger("T001")
            await visit_site(d, ScriptedPerceptor({}), ledger, CAND)
            assert ledger.outcomes[0].branch == "unresolved", q
            assert ledger.negative_samples() == []

    async def test_每条outcome都带候选地址(self):
        """D-6「站点 URL」必填的机械前提：控制流任何一条分支都不许漏。

        漏了不会炸，只会在存档里少一个字段——而读档的人无从知道
        「这个空是没记」还是「这个空是本来就没有」。"""
        cases = {
            "unreachable_hard": (FakeDriver(), ScriptedPerceptor({})),
            "login_wall_or_blocked": (
                FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")}),
                ScriptedPerceptor({Q.IS_REACHABLE: no(Q.IS_REACHABLE)}),
            ),
            "no_play_control": (
                FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")}),
                ScriptedPerceptor(
                    {
                        Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                        Q.FIND_PLAY_CONTROL: no(Q.FIND_PLAY_CONTROL),
                    }
                ),
            ),
        }
        for branch, (d, p) in cases.items():
            ledger = RunLedger("T001")
            await visit_site(d, p, ledger, CAND)
            assert ledger.outcomes[0].site_url == "https://x.test/movie", branch

    async def test_未到播放页时不编播放页地址(self):
        """空是正确值——D-6 口径是「不适用、不扣分」。
        填一个 `url` 进去看着齐整，实际是把猜测写成事实。"""
        d = FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")})
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: no(Q.FIND_PLAY_CONTROL),
            }
        )
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].play_page_url == ""


class TestPlayerUnverifiedAtStep7:
    """环节⑦：控件点了、播放页就在眼前，感知层却判不了能不能播。

    这三支（success / player_unverified / component_unverified）都必须带
    **播放页地址**：D-6 要求它必填，而这三支恰恰是唯一确定自己到了播放页的
    情形。少了它，人工复核时手上只有「候选链接」，
    根本不知道该去看哪个页面——这一批人工复核就会白白浪费。
    """

    PAGES = {
        "https://x.test/movie": obs_at(
            "https://x.test/movie", elements=(("e5", "button", "播放"),)
        ),
        "https://x.test/play/e5": obs_at(
            "https://x.test/play/e5", title="播放器", video=1
        ),
    }

    async def _run(self, player_decision):
        ledger = RunLedger("T001")
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e5"}),
                Q.PLAYER_OK: player_decision,
            }
        )
        await visit_site(FakeDriver(self.PAGES), p, ledger, CAND)
        return ledger.outcomes[0]

    async def test_none落player_unverified(self):
        out = await self._run(none(Q.PLAYER_OK))
        assert out.branch == "player_unverified"

    async def test_none时仍算访问过(self):
        """报成「没到播放页」就把这一条从最有价值的复核样本里划走了——
        而页面就在存档里躺着。"""
        out = await self._run(none(Q.PLAYER_OK))
        assert out.reached is True
        assert out.url == "https://x.test/play/e5"

    async def test_none不是负样本(self):
        assert (await self._run(none(Q.PLAYER_OK))).is_negative_sample is False

    async def test_success分支也带播放页地址(self):
        out = await self._run(ok(Q.PLAYER_OK, {"media_count": 1}))
        assert out.branch is None
        assert out.play_page_url == "https://x.test/play/e5"

    async def test_false分支也带播放页地址(self):
        """反例护栏：分流不能把带地址的那一支漏掉。
        （点击后换了页 movie → play/e5，前置事实「到了播放页」成立。）"""
        out = await self._run(no(Q.PLAYER_OK))
        assert out.branch == "component_unverified"
        assert out.play_page_url == "https://x.test/play/e5"

    async def test_三支都带候选地址与播放页地址(self):
        for dec_ in (ok(Q.PLAYER_OK), none(Q.PLAYER_OK), no(Q.PLAYER_OK)):
            out = await self._run(dec_)
            assert out.site_url == "https://x.test/movie", dec_.answer
            assert out.play_page_url == "https://x.test/play/e5", dec_.answer

    async def test_前置_none_不中断_继续收集证据(self):
        """**fail-closed 管结论不管采集**。

        实测逼出来的：W1 规则版对 IS_REACHABLE 只会返回 None，
        若据此中断，5 个真实候选站点全落 unresolved，负样本池一条不产，
        W1 验收直接不达标。

        继续走**不等于**断言「页面可达」——分支仍按有没有得到
        确定性结论来定：拿到 False 就是货真价实的 no_play_control。
        """
        d = FakeDriver(
            {
                "https://x.test/movie": obs_at(
                    "https://x.test/movie", elements=(("e1", "a", "首页"),)
                )
            }
        )
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: none(Q.IS_REACHABLE),  # 前置判不出来
                Q.FIND_PLAY_CONTROL: no(Q.FIND_PLAY_CONTROL),  # 但这一题有确定结论
            }
        )
        ledger = RunLedger("T001")
        result = await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "no_play_control"
        assert ledger.negative_samples()
        assert any("不中断" in n for n in (result.notes or []))

    async def test_前置_false_仍然中断(self):
        """None 不中断 ≠ 所有 None 都放过。**确定判 False 时必须停**——
        页面明明是登录墙还继续点控件，点出来的结论没有意义。"""
        d = FakeDriver(
            {
                "https://x.test/movie": obs_at(
                    "https://x.test/movie", elements=(("e1", "a", "播放"),)
                )
            }
        )
        # 带细分 payload（真登录墙）⇒ 落真负样本分支
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: no(
                    Q.IS_REACHABLE, payload={"access_control_type": "login"}),
                Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            }
        )
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "login_required"
        assert d.clicked == []  # 登录墙上不该点播放


class TestComponentUnverifiedRequiresNewPage:
    """判断点④ False + **点击前后同页** → 降级 unresolved（前置事实不成立）。

    mgtvtv 现场（避坑指南 §5.14）：点击没把页面带进播放页，直接贴
    ``component_unverified`` 会把可看的站写成看不了的。FakeDriver 的
    PAGES 不登记 play 页即模拟这个场景——点击后 ``current`` 不变，
    点击后的观察与点击前是同一个页面（url / 元素数 / 正文全同）。
    """

    async def _run(self, pages):
        ledger = RunLedger("T001")
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e5"}),
                Q.PLAYER_OK: no(Q.PLAYER_OK, evidence="LLM 判 False：并非实际播放页"),
            }
        )
        await visit_site(FakeDriver(pages), p, ledger, CAND)
        return ledger.outcomes[0]

    async def test_点击后同页降级unresolved(self):
        out = await self._run(
            {
                "https://x.test/movie": obs_at(
                    "https://x.test/movie", elements=(("e5", "button", "播放"),)
                ),
            }
        )
        assert out.branch == "unresolved"
        assert "同一个页面" in out.evidence

    async def test_同页降级时模型原话保留(self):
        out = await self._run(
            {
                "https://x.test/movie": obs_at(
                    "https://x.test/movie", elements=(("e5", "button", "播放"),)
                ),
            }
        )
        assert "并非实际播放页" in out.evidence

    async def test_点击后换页照贴component_unverified(self):
        """反例护栏：守卫只拦「没换页」，不拦合法排除——
        哪怕播放页上 video=0（真实视频站的常态）。"""
        out = await self._run(
            {
                "https://x.test/movie": obs_at(
                    "https://x.test/movie", elements=(("e5", "button", "播放"),)
                ),
                "https://x.test/play/e5": obs_at(
                    "https://x.test/play/e5", title="播放器", video=0
                ),
            }
        )
        assert out.branch == "component_unverified"


# ═══════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════


class TestOrchestrator:
    def _search_page(self, *links):
        return Observation(
            url="https://www.baidu.com/s?wd=x",
            page_title="百度",
            body_text="搜索结果",
            links=tuple(LinkItem(text=t, href=h) for t, h in links),
        )

    async def test_取候选并遍历(self):
        pages = {
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": self._search_page(
                ("功夫电影", "https://www.a.test/movie"),
                ("知乎讨论", "https://www.zhihu.com/question/1"),
            ),
            "https://www.a.test/movie": obs_at("https://www.a.test/movie"),
        }
        d = FakeDriver(pages)
        # 方案 12 阶段 A1：zhihu 已不在结构性排除表里（路径 C 语义信源），
        # 它的淘汰走分类任务——被判 article_post（非 direct 目标类），
        # 落 **not_play_site 有证据负样本**，而不是结构剔除的静默消失。
        script = {
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "video_site": [
                        {"url": "https://www.a.test/movie", "why": "标题含片名"}
                    ],
                    "article_post": [
                        {"url": "https://www.zhihu.com/question/1", "why": "知乎讨论帖"}
                    ],
                },
            ),
            Q.IS_REACHABLE: no(Q.IS_REACHABLE),
        }
        perceptor = ScriptedPerceptor(script)
        cfg = RunConfig(stop_after_success=1)
        orch = Orchestrator(d, perceptor, cfg)
        rec = await orch.run("T001", "功夫")

        assert rec.title == "功夫"
        assert "功夫 在线观看" in rec.query
        assert [c.url for c in rec.candidates] == ["https://www.a.test/movie"]
        assert rec.candidate_source == "perceptor"
        assert Q.CLASSIFY_SEARCH_LINKS in perceptor.calls
        # 知乎被分类点名淘汰 → 有证据入账；a.test 走 visit 链出结果
        outcomes = {o.url: o.branch for o in rec.ledger.outcomes}
        assert outcomes["https://www.zhihu.com/question/1"] == "not_play_site"
        assert len(outcomes) == 2

    async def test_代码不做语义筛选(self):
        """`news.b.test` 这种域名**必须**留在候选里。

        写成「剔除以 news. 开头的域名」会是错的——`newsletter.com`、`newsy.com`
        也以 news 开头，而国内视频站的域名里带 news 的更不少见。
        代码能做的是剔除**结构上就不是内容站**的（社交 / CDN / 搜索引擎自身），
        「这是不是播放站」是 SELECT_PLAY_SITES 的语义职责。
        """
        pages = {
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": self._search_page(
                ("新闻", "https://news.b.test/x")
            ),
        }
        d = FakeDriver(pages)
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert [c.url for c in rec.candidates] == ["https://news.b.test/x"]

    async def test_搜索页_失败时提前返回且不崩(self):
        d = FakeDriver()  # 搜索页也不存在
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert rec.candidates == []
        assert rec.warnings
        assert rec.succeeded is False

    async def test_超时按_unresolved_记账而非静默消失(self):
        """超时的站点若不记账，样本分布里会凭空少一块——看不见的损失。"""
        import asyncio

        pages = {
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": self._search_page(
                ("a", "https://slow.test/movie")
            ),
            "https://slow.test/movie": obs_at("https://slow.test/movie"),
        }
        d = FakeDriver(pages)

        # 让该站点的观察真的慢到超时——不 sleep 的话 wait_for 可能来不及触发，
        # 那测到的就不是超时路径
        fast = d.observe

        async def slow_observe(*, max_chars=None):
            if d.current and d.current.url.startswith("https://slow.test"):
                await asyncio.sleep(0.5)
            return await fast(max_chars=max_chars)

        d.observe = slow_observe

        cfg = RunConfig(per_site_timeout_s=0.02)
        orch = Orchestrator(d, ScriptedPerceptor({}), cfg)
        rec = await orch.run("T001", "功夫")
        assert rec.ledger.outcomes[0].branch == "unresolved"
        assert any("超时" in w for w in rec.warnings)

    async def test_stop_after_success_生效(self):
        base = ("{host}/movie", "")
        links = [(f"站{i}", f"https://s{i}.test/movie") for i in range(5)]
        pages = {
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": self._search_page(
                *links
            ),
        }
        for i in range(5):
            pages[f"https://s{i}.test/movie"] = obs_at(
                f"https://s{i}.test/movie", elements=(("e1", "button", "播放"),)
            )
            pages[f"https://x.test/play/e1"] = obs_at("https://x.test/play/e1", video=1)

        d = FakeDriver(pages)
        script = {
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        orch = Orchestrator(
            d, ScriptedPerceptor(script), RunConfig(stop_after_success=2)
        )
        rec = await orch.run("T001", "功夫")
        assert len(rec.visits) == 2

    async def test_运行参数由_run_自己填(self):
        """**不能只在序列化时现填**——那样手写存档、
        `review --write` 产出的复核档、以及未来任何不走 ``run()`` 的入口
        都会留下空快照，而 B-2 的分母不可知不会在存档里显形。

        这条测的是「填在 ``run()`` 里」，不是「落盘时有这个键」：
        后者由 :class:`TestRunRecordAudit` 盯。
        """
        pages = {
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": self._search_page(
                ("a", "https://a.test/movie")
            ),
            "https://a.test/movie": obs_at("https://a.test/movie"),
        }
        cfg = RunConfig(max_candidates=3, per_site_timeout_s=12.5)
        rec = await Orchestrator(FakeDriver(pages), ScriptedPerceptor({}), cfg).run(
            "T001", "功夫"
        )
        assert rec.run_config["max_candidates"] == 3
        assert rec.run_config["per_site_timeout_s"] == 12.5


class TestClassifyCandidatesWiring:
    """环节 ①.5 分类任务的控制流接线（方案 12 阶段 A1，2026-10-16）。

    它的前身 ``TestSelectPlaySitesWiring`` 测的是旧 ①（SELECT_PLAY_SITES
    的 selected/rejected 二元筛选）；A1 起搜索页分叉改问
    ``CLASSIFY_SEARCH_LINKS`` 四分类，按 ``discovery_path`` 取目标类分发
    （direct→video_site / B→aggregator / C→article_post）。旧三道
    SELECT_* 题保留在注册表：站内搜索结果页仍复用 ①（见
    ``steps/site_search.py``），金标重放与 A/B 对照也需要它们。

    负样本纪律一条不变：**只有模型点名的候选才记负分支**——
    ``not_play_site`` 在真实链路上必须触发得了，否则报表分部数字齐全、
    这支却空着，没人看得出来。
    """

    SEARCH = "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B"

    def _search_page(self, *links):
        return Observation(
            url=self.SEARCH,
            page_title="百度",
            body_text="搜索结果",
            links=tuple(LinkItem(text=t, href=h) for t, h in links),
        )

    def _pages(self, *links):
        pages = {self.SEARCH: self._search_page(*links)}
        for _, href in links:
            if href.startswith("https://") and "baidu" not in href:
                pages[href] = obs_at(href, elements=(("e1", "button", "播放"),))
                pages["https://x.test/play/e1"] = obs_at(
                    "https://x.test/play/e1", video=1
                )
        return pages

    def _classify(self, **classes) -> Decision:
        """构造分类任务的预设应答。``classes`` 形如
        ``video_site=[{"url": ..., "why": ...}]``，未给的四类为空。"""
        payload = {cls: rows for cls, rows in classes.items()}
        return ok(Q.CLASSIFY_SEARCH_LINKS, payload)

    async def test_目标类保留后才遍历(self):
        pages = self._pages(
            ("a站", "https://www.a.test/movie"), ("b站", "https://www.b.test/movie")
        )
        script = {
            Q.CLASSIFY_SEARCH_LINKS: self._classify(
                video_site=[
                    {"url": "https://www.a.test/movie", "why": "标题含片名"}
                ],
                noise=[{"url": "https://www.b.test/movie", "why": "只有影评"}],
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        d = FakeDriver(pages)
        rec = await Orchestrator(d, ScriptedPerceptor(script), RunConfig()).run(
            "T001", "功夫"
        )

        assert [c.url for c in rec.candidates] == ["https://www.a.test/movie"]
        assert rec.candidate_source == "perceptor"
        assert len(rec.visits) == 1

    async def test_非目标类点名记not_play_site(self):
        """**每条分支都必须有对应样本入库**——负样本不是副产品。"""
        pages = self._pages(
            ("a站", "https://www.a.test/movie"), ("b站", "https://www.b.test/movie")
        )
        script = {
            Q.CLASSIFY_SEARCH_LINKS: self._classify(
                video_site=[{"url": "https://www.a.test/movie", "why": "w"}],
                noise=[{"url": "https://www.b.test/movie", "why": "只有影评无播放"}],
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")

        dropped = [o for o in rec.ledger.outcomes if o.branch == "not_play_site"]
        assert len(dropped) == 1
        assert dropped[0].url == "https://www.b.test/movie"
        assert "只有影评无播放" in dropped[0].evidence

    async def test_未取得结论不中断采集(self):
        """fail-closed 管的是「结论」不是「采集」。W1 的规则版对分类题
        只会返回 None，若据此中断，候选一个都不跑，负样本池永远空。"""
        pages = self._pages(
            ("a站", "https://www.a.test/movie"), ("b站", "https://www.b.test/movie")
        )
        script = {
            Q.CLASSIFY_SEARCH_LINKS: none(Q.CLASSIFY_SEARCH_LINKS, "W1 无语义能力")
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")

        assert len(rec.candidates) == 2
        assert rec.candidate_source == "heuristic"
        assert rec.candidates and any("不中断" in w for w in rec.warnings)

    async def test_目标类为零时不遍历(self):
        """旧 ① 的 False 分支（「这页没有可看站点」→ 中断遍历）在 A1 里
        是分发的**涌现结果**：目标类 kept=0 且逐条点名记负，遍历自然
        没有目标——同一份账面，不再需要整页 False 文案。"""
        pages = self._pages(
            ("a站", "https://www.a.test/movie"), ("b站", "https://www.b.test/movie")
        )
        script = {
            Q.CLASSIFY_SEARCH_LINKS: self._classify(
                noise=[
                    {"url": "https://www.a.test/movie", "why": "资讯站"},
                    {"url": "https://www.b.test/movie", "why": "资讯站"},
                ]
            ),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")
        assert rec.visits == []
        assert [o.branch for o in rec.ledger.outcomes] == [
            "not_play_site", "not_play_site",
        ]

    async def test_目标类保留顺序保持引擎原序(self):
        """重排等于用模型的判断覆盖引擎的相关性排序，那是没有依据的。"""
        pages = self._pages(
            *[(f"站{i}", f"https://www.s{i}.test/movie") for i in range(3)]
        )
        script = {
            # 模型把第 3 个排第一——控制流不该照它重排
            Q.CLASSIFY_SEARCH_LINKS: self._classify(
                video_site=[
                    {"url": "https://www.s2.test/movie", "why": "最像"},
                    {"url": "https://www.s0.test/movie", "why": "也行"},
                    {"url": "https://www.s1.test/movie", "why": "可以"},
                ]
            ),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")
        assert [c.rank for c in rec.candidates] == [1, 2, 3]

    async def test_分类匹配用解包后URL而非原始href(self):
        """百度结果页大量用 ``/link?url=`` 包裹。A1 的匹配键是候选视图的
        ``href``——它**就是** ``Candidate.url``（解包后目标地址），由
        :func:`orchestrator._candidates_view` 同源构造：「模型看到的链接」
        与「分发命中的候选」不靠 source_href 猜测。"""
        wrapped = "https://www.baidu.com/link?url=https%3A%2F%2Fwww.a.test%2Fmovie"
        pages = {
            self.SEARCH: self._search_page(("a站", wrapped)),
            "https://www.a.test/movie": obs_at(
                "https://www.a.test/movie", elements=(("e1", "button", "播放"),)
            ),
        }
        script = {
            Q.CLASSIFY_SEARCH_LINKS: self._classify(
                video_site=[{"url": "https://www.a.test/movie", "why": "w"}]
            ),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")
        assert [c.url for c in rec.candidates] == ["https://www.a.test/movie"]

    async def test_规则版批次行为不变(self):
        """W1 回归：``RulePerceptor`` 对分类题返回 None → 全遍历。
        这是既有能力，新插件上线不能让既有批次报废。"""
        from trajectory_pipeline.perception.rule_perceptor import RulePerceptor

        pages = self._pages(("a站", "https://www.a.test/movie"))
        rec = await Orchestrator(FakeDriver(pages), RulePerceptor(), RunConfig()).run(
            "T001", "功夫"
        )
        assert len(rec.candidates) == 1
        assert rec.candidate_source == "heuristic"


class TestClassifyCandidatesBatches:
    """分类任务的分批与窗口常量同步（方案 12 阶段 A1）。

    「模型看得到全部候选」由构造保证：候选按 ``PERCEPTION_LINK_WINDOW``
    分批构造候选视图（钉窗机制的消解，见 ``steps/search.py`` 尾注）。
    窗口常量若与感知层 ``LLM_LINKS`` 漂移——批比窗口大——感知层会
    **静默**把多出的链接近回窗口，「全部候选都送到了」就成了假话。
    """

    class EchoClassifyPerceptor:
        """按观察回声分类：把收到的每条链接原样归入 video_site。

        用来测分批本身——每批送了哪些链接、批数多少，与模型语义无关。
        """

        name = "echo"

        def __init__(self):
            self.batches: list[list[str]] = []

        def decide(self, question: str, obs: Observation) -> Decision:
            self.batches.append([l.href for l in obs.links])
            return ok(
                question,
                {
                    "video_site": [
                        {"url": l.href, "why": "echo"} for l in obs.links
                    ]
                },
            )

    async def test_超窗候选分批送感知层(self):
        n = 2 * orchestrator.PERCEPTION_LINK_WINDOW + 5  # 3 批：20 / 20 / 5
        links = [(f"站{i}", f"https://s{i}.test/movie") for i in range(n)]
        pages = {
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": Observation(
                url="https://www.baidu.com/s?wd=x",
                page_title="百度",
                body_text="搜索结果",
                links=tuple(LinkItem(text=t, href=h) for t, h in links),
            ),
        }
        perceptor = self.EchoClassifyPerceptor()
        rec = await Orchestrator(
            FakeDriver(pages), perceptor, RunConfig(max_candidates=n)
        ).run("T001", "功夫")

        assert [len(b) for b in perceptor.batches] == [
            orchestrator.PERCEPTION_LINK_WINDOW,
            orchestrator.PERCEPTION_LINK_WINDOW,
            5,
        ]
        # 每条候选都送到了（「全部候选都可见」由构造保证）
        sent = {href for b in perceptor.batches for href in b}
        assert sent == {f"https://s{i}.test/movie" for i in range(n)}
        # echo 全判 video_site（direct 目标类）→ 全保留、保引擎原序
        assert [c.rank for c in rec.candidates] == list(range(1, n + 1))
        assert rec.candidate_source == "perceptor"

    def test_窗口常量与感知层LLM_LINKS一致(self):
        """executor 侧复制了窗口宽度常量——不得与感知层漂移。
        漂移 = 分批按错的宽度切，批比窗口大时感知层静默截断，
        「模型看得到全部候选」的构造保证就破了，且校验还报绿。"""
        assert orchestrator.PERCEPTION_LINK_WINDOW == lp.LLM_LINKS

    def test_窗口常量为正整数(self):
        assert isinstance(orchestrator.PERCEPTION_LINK_WINDOW, int)
        assert orchestrator.PERCEPTION_LINK_WINDOW > 0


class Test无证据不记负样本:
    """分类任务里**没被模型点名**的候选不得记 ``not_play_site``。

    实测形态（2026-10-10，T001《功夫》，baidu）：修复前整批 5 个候选
    落进 ``not_play_site`` 负样本池，evidence 写着「模型未给出排除理由」
    ——代码明知没有理由，仍把它记成了「这个站看不了」。

    机制（A1 前的旧形态）：① 返回 True，但模型的 ``rejected`` 里只有它
    **实际看到**的那 20 条导航链接；候选排在第 25 条之后，模型压根没见过
    它们。「没被选中」被当成了「被否决」，而这两件事在无证据时无法区分。

    A1 后的形态：分类任务按 ``PERCEPTION_LINK_WINDOW`` 分批、候选视图
    由构造保证可见（上述窗口机制已消解），但「未点名」仍然可能出现
    ——模型漏分类就是漏答，与「这个站看不了」是两回事。

    口径：**只有模型点名（出现在分类 payload 里且非目标类）的候选才记
    负样本**，其余记 ``unresolved``——人工复核分支，**不入负样本池**
    （CLAUDE.md：「``unresolved`` 不是负样本」）。
    """

    #: 复用既有夹具的常量而不是抄一遍：抄过一次就把「在线」抄成了
    #: 「在看」（``%E5%9C%A8%E7%BA%BF`` vs ``%E5%8A%BF``），症状是
    #: ``DriverError: 不存在``——FakeDriver 按 URL 取页，而 search_url
    #: 由 query 拼出，抄错一个字符就取不到搜索页，于是「什么都没记」，
    #: 看着像记账逻辑坏了。
    SEARCH = TestClassifyCandidatesWiring.SEARCH

    #: 实测（2026-10-10 T001，baidu 搜《功夫》）：``browser_extract`` 给的
    #: ``urls[]`` 是**解包后的目标地址**，而 ``links[].href`` 是
    #: ``/link?url=`` **包裹形态**。两者的键名不同，指向同一条链接。
    WRAPPED = "https://www.baidu.com/link?url=https%3A%2F%2Fwww.a.test%2Fmovie"
    REAL = "https://www.a.test/movie"

    async def test_标题写回观察_包裹链接也对上(self):
        """**这一处此前静默失败。** 只按原始 href 查标题，baidu 上
        一条都命不中 → 写回形同虚设 → 存档的 links 仍是导航栏 →
        判断点 ① 读的就是它 → 永远看不到片名、恒 fail-closed。

        症状是「选择器失效」（``result_titles`` 明明返回了 8 条），
        而选择器是好的——没人按对的键去取。
        """
        pages = self._pages(("导航", self.WRAPPED))
        d = FakeDriver(pages)
        d.extracted = {"titles": ["《功夫》电影完整版-爱奇艺"], "urls": [self.REAL]}
        rec = await Orchestrator(d, ScriptedPerceptor({}), RunConfig()).run(
            "T001", "功夫"
        )

        assert rec.result_titles == 1
        texts = [l.text for l in rec.search_obs.links]
        assert "《功夫》电影完整版-爱奇艺" in texts
        # ① 于是真的拿到了判别力，而不是靠 fail-closed 侥幸避开
        assert any("功夫" in t for t in texts)

    async def test_取不到标题时写回不生效(self):
        """反例护栏：写回不得凭空造标题——拿不到就保留原样，
        让 ① 照旧 fail-closed。"""
        pages = self._pages(("导航", self.WRAPPED))
        d = FakeDriver(pages)
        d.extracted = {"titles": [], "urls": []}  # 字段不等长 → 抽不到
        rec = await Orchestrator(d, ScriptedPerceptor({}), RunConfig()).run(
            "T001", "功夫"
        )

        assert not rec.result_titles
        assert [l.text for l in rec.search_obs.links] == ["导航"]
        assert any("未取到" in w for w in rec.warnings)

    def _pages(self, *links):
        pages = {
            self.SEARCH: Observation(
                url=self.SEARCH,
                page_title="百度",
                body_text="搜索结果",
                links=tuple(LinkItem(text=t, href=h) for t, h in links),
            )
        }
        for _, href in links:
            if "baidu" not in href:
                pages[href] = obs_at(href, elements=(("e1", "button", "播放"),))
                pages["https://x.test/play/e1"] = obs_at(
                    "https://x.test/play/e1", video=1
                )
        return pages

    async def test_未点名的候选记unresolved(self):
        pages = self._pages(
            ("a站", "https://www.a.test/movie"), ("b站", "https://www.b.test/movie")
        )
        script = {
            # 分类 payload 只点了模型自己看到的那条导航，与两个候选无关
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "noise": [
                        {"url": "https://www.baidu.com/", "why": "搜索引擎"}
                    ]
                },
            ),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")

        assert not [o for o in rec.ledger.outcomes if o.branch == "not_play_site"]
        undec = [o for o in rec.ledger.outcomes if o.branch == "unresolved"]
        assert len(undec) == 2
        assert "无证据不记负样本" in undec[0].evidence
        assert any("记 unresolved" in w for w in rec.warnings)

    async def test_点名的候选仍记not_play_site(self):
        """反例护栏：口径不能放水到「一律 unresolved」——
        那样 ``not_play_site`` 永远空，而每条分支都必须有样本入库。"""
        pages = self._pages(
            ("a站", "https://www.a.test/movie"), ("b站", "https://www.b.test/movie")
        )
        script = {
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "video_site": [
                        {"url": "https://www.a.test/movie", "why": "含片名"}
                    ],
                    "noise": [
                        {"url": "https://www.b.test/movie", "why": "只有影评"}
                    ],
                },
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")

        dropped = [o for o in rec.ledger.outcomes if o.branch == "not_play_site"]
        assert len(dropped) == 1
        assert dropped[0].url == "https://www.b.test/movie"
        assert "只有影评" in dropped[0].evidence
        assert [c.url for c in rec.candidates] == ["https://www.a.test/movie"]

    async def test_点名但无理由仍算否决(self):
        """判据是「**点名**」不是「给了理由」。理由可以为空——
        模型点名了却没写理由，仍然是它考虑过并否决了。"""
        pages = self._pages(("a站", "https://www.a.test/movie"))
        script = {
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {"noise": [{"url": "https://www.a.test/movie"}]},
            ),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")

        dropped = [o for o in rec.ledger.outcomes if o.branch == "not_play_site"]
        assert len(dropped) == 1
        assert "未给出理由" in dropped[0].evidence

    async def test_整体无目标类时未点名的仍不记负(self):
        """旧 False 分支的坑在 A1 的对应形态：目标类 kept=0 的「整页
        没有可看站点」不等于逐条被否决——未点名的候选照旧 unresolved，
        点了名的才落 not_play_site。"""
        pages = self._pages(
            ("a站", "https://www.a.test/movie"), ("b站", "https://www.b.test/movie")
        )
        script = {
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    # 只点名了 a（noise），b 未点名——kept=0 但 b 不入负
                    "noise": [
                        {"url": "https://www.a.test/movie", "why": "资讯站"}
                    ]
                },
            ),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")

        assert rec.visits == []
        dropped = [o for o in rec.ledger.outcomes if o.branch == "not_play_site"]
        assert len(dropped) == 1
        assert dropped[0].url == "https://www.a.test/movie"
        assert len([o for o in rec.ledger.outcomes if o.branch == "unresolved"]) == 1


# ═══════════════════════════════════════════════════════════════════════
# 审计字段
# ═══════════════════════════════════════════════════════════════════════


class TestRunRecordAudit:
    async def test_候选来源必须显式标注(self):
        """读档的人若不知道候选来自启发式，会把「W1 选出的站点」
        误读成「规则版能选出正确站点」——它不能。"""
        from trajectory_pipeline.executor.orchestrator import RunRecord

        rec = RunRecord(
            task_id="T001",
            title="功夫",
            query="q",
            search_url="u",
            candidate_source="heuristic",
        )
        assert rec.to_json()["candidate_source"] == "heuristic"

    async def test_观察序列化_正文全文落盘(self):
        """P1 是批次唯一的真值源，正文必须全文留存。

        早期版本只留前 400 字符，理由是「体积失控、需要时按 url 重抓」。
        那条理由站不住：站点会下线改版（重抓拿到的是另一个页面）、
        被反爬时根本重抓不回来、rationale 的实体核查会把落在
        摘要外的实体判成幻觉——而那不是幻觉，是**没存**。
        """
        from trajectory_pipeline.executor.orchestrator import RunRecord

        big = obs_at("https://x.test/", body="x" * 100_000)
        rec = RunRecord(
            task_id="T001", title="t", query="q", search_url="u", search_obs=big
        )
        j = rec.to_json()["search_observation"]
        assert j["body_len"] == 100_000
        assert j["body_text"] == big.body_text
        assert "body_preview" not in j  # 摘要字段已退役

    async def test_序列化_正文尾部内容可取回(self):
        """反例保护：正文**尾部**的内容必须在存档里。
        rationale 引用的实体落在 400 字符之后是常态。"""
        from trajectory_pipeline.executor.orchestrator import RunRecord

        marker = "《武林外传》第39集"
        body = ("填充" * 300) + marker
        rec = RunRecord(
            task_id="T001",
            title="t",
            query="q",
            search_url="u",
            search_obs=obs_at("https://x.test/", body=body),
        )
        assert marker in rec.to_json()["search_observation"]["body_text"]

    async def test_序列化不含页面句柄(self):
        from trajectory_pipeline.executor.orchestrator import RunRecord

        rec = RunRecord(
            task_id="T001",
            title="t",
            query="q",
            search_url="u",
            search_obs=obs_at("https://x.test/"),
        )
        blob = repr(rec.to_json())
        for token in ("page_driver", "McpClient", "obscura", "_driver"):
            assert token not in blob

    async def test_元素落盘被裁时总量字段仍在(self):
        """D-2 的机械前提：落盘条数上限 80、链接上限 50，
        而 ``elements_total`` 记的是**裁之前**的量。

        没它就是「两种相反的错误同形」：页面真有 300 个控件而采了 80，
        与页面只有 80 个控件，都落成 80 条——前者该判不可判定，后者不该。"""
        from trajectory_pipeline.executor.orchestrator import (
            OBS_ELEMENT_LIMIT,
            OBS_LINK_LIMIT,
            RunRecord,
        )

        elements = tuple(("e%d" % i, "a", "链接%d" % i) for i in range(120))
        links = tuple(("t%d" % i, "https://x.test/%d" % i) for i in range(90))
        obs = Observation(
            url="https://x.test/",
            page_title="T",
            body_text="正文",
            interactive_elements=tuple(
                InteractiveElement(ref=r, role=t, name=l) for r, t, l in elements
            ),
            links=tuple(LinkItem(text=t_, href=h) for t_, h in links),
            elements_total=120,
            links_total=90,
        )
        j = RunRecord(
            task_id="T001", title="t", query="q", search_url="u", search_obs=obs
        ).to_json()["search_observation"]
        assert len(j["interactive_elements"]) == OBS_ELEMENT_LIMIT
        assert len(j["links"]) == OBS_LINK_LIMIT
        assert j["elements_total"] == 120 > OBS_ELEMENT_LIMIT
        assert j["links_total"] == 90 > OBS_LINK_LIMIT

    async def test_没设总量时按落盘条数兜底(self):
        """老观察对象（手工构造、第三方传入）没有这两个字段。
        兜底成 0 会让 D-2 判成「整页都不可信」，宁可等于实际条数。"""
        from trajectory_pipeline.executor.orchestrator import RunRecord

        obs = obs_at("https://x.test/", elements=(("e1", "a", "甲"), ("e2", "a", "乙")))
        j = RunRecord(
            task_id="T001", title="t", query="q", search_url="u", search_obs=obs
        ).to_json()["search_observation"]
        assert j["elements_total"] == 2

    async def test_运行参数进存档(self):
        """B-2（覆盖完整性）的分母是 ``max_candidates``、B-4（时间限制）
        判的是 ``per_site_timeout_s``。两者不落盘的话，
        ``max_candidates`` 取 20 与取 5 的两批数据**长得一模一样**，
        而它们的采集完整度根本不是一回事。"""
        from trajectory_pipeline.executor.orchestrator import RunRecord

        cfg = RunConfig(engine="bing", max_candidates=5, per_site_timeout_s=30.0)
        rec = RunRecord(task_id="T001", title="t", query="q", search_url="u")
        rec.run_config = dataclasses.asdict(cfg)
        assert rec.to_json()["run_config"]["max_candidates"] == 5
        assert rec.to_json()["run_config"]["per_site_timeout_s"] == 30.0

    async def test_没跑时运行参数为空对象(self):
        """空 dict 而不是 ``None``：assembler 侧判「缺件」用的是
        「为假」，两种空值会让那条判断在两边不一致。"""
        from trajectory_pipeline.executor.orchestrator import RunRecord

        rec = RunRecord(task_id="T001", title="t", query="q", search_url="u")
        assert rec.to_json()["run_config"] == {}


# ═══════════════════════════════════════════════════════════════════════
# 反爬拦截：必须与「素材没价值」分开
# ═══════════════════════════════════════════════════════════════════════


class TestAntiScraping:
    """实测：连跑 3 个 task，第 1 个正常，后 2 个被百度验证码页拦下。

    拦截的表现是「正文短 + 零链接」，若混进「搜索页未提取到候选」，
    处置就错了——那句话说「这批素材没价值」，真相是「搜索引擎把我们拦了」。
    """

    CAPTCHA_URL = "https://wappass.baidu.com/static/captcha/tuxing_v2.html?logid=1"

    async def test_被拦时短路且不产候选(self):
        d = FakeDriver(
            {
                "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": Observation(
                    url=self.CAPTCHA_URL,
                    page_title="百度安全验证",
                    body_text="网络不给力，请稍后重试",
                ),
            }
        )
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert rec.search_blocked == "captcha"
        assert rec.candidates == []
        assert any("被反爬拦截" in w for w in rec.warnings)

    async def test_告警必须否定素材质量说法(self):
        """措辞本身就是防线——读的人第一眼看到的就是它。"""
        d = FakeDriver(
            {
                "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": Observation(
                    url=self.CAPTCHA_URL, page_title="百度安全验证", body_text="x"
                ),
            }
        )
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        joined = " ".join(rec.warnings)
        assert "不是素材质量问题" in joined
        assert "换引擎" in joined

    async def test_被拦不进负样本池(self):
        """⚠️ 拦截发生在**取候选之前**，根本没有 outcome——
        塞进 8 条分支会让负样本池混入连站点都没访问到的条目。"""
        d = FakeDriver(
            {
                "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": Observation(
                    url=self.CAPTCHA_URL, page_title="百度安全验证", body_text="x"
                ),
            }
        )
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert rec.ledger is not None and rec.ledger.outcomes == []
        assert rec.ledger.negative_samples() == []

    async def test_被拦落gatekeeper事件账本(self):
        """方案 12 阶段 G：同一拦截事件的结构化记账，与 search_blocked /
        warnings **并存不替代**。处置是 FailClosed 运行级特例——branch
        必须为空（拦截没有 outcome，不进分支体系）。"""
        d = FakeDriver(
            {
                "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": Observation(
                    url=self.CAPTCHA_URL, page_title="百度安全验证", body_text="x"
                ),
            }
        )
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert len(rec.gatekeeper_events) == 1
        ev = rec.gatekeeper_events[0]
        assert ev["stage"] == "search"
        # CAPTCHA_URL 命中 URL marker（wappass）→ captcha；标题「百度安全
        # 验证」不含 CF 子集词 → 不细分为 blocked_cf。
        assert ev["verdict"] == "captcha"
        assert ev["reason"] == "captcha"
        assert ev["disposition"]["kind"] == "fail_closed"
        assert ev["disposition"]["branch"] == ""
        # 落盘与内存同形
        assert rec.to_json()["gatekeeper_events"] == [ev]

    async def test_未被拦时事件账本为空(self):
        d = FakeDriver(
            {
                "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": Observation(
                    url="https://www.baidu.com/s?wd=x",
                    page_title="百度",
                    body_text="结果",
                    links=(LinkItem(text="a", href="https://a.test/x"),),
                ),
            }
        )
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert rec.gatekeeper_events == []

    async def test_落盘带search_blocked(self):
        from trajectory_pipeline.executor.orchestrator import RunRecord

        rec = RunRecord(
            task_id="T001",
            title="t",
            query="q",
            search_url="u",
            search_blocked="captcha",
        )
        assert rec.to_json()["search_blocked"] == "captcha"

    async def test_未被拦时字段为空(self):
        d = FakeDriver(
            {
                "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": Observation(
                    url="https://www.baidu.com/s?wd=x",
                    page_title="百度",
                    body_text="结果",
                    links=(LinkItem(text="a", href="https://a.test/x"),),
                ),
            }
        )
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert rec.search_blocked == ""

    async def test_候选过滤落进存档(self):
        """「引擎给了 50 个链接、我们只跑了 11 个」中间那批去哪了要能查到。"""
        d = FakeDriver(
            {
                "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B": Observation(
                    url="https://www.baidu.com/s?wd=x",
                    page_title="百度",
                    body_text="结果",
                    links=(
                        LinkItem(text="备案", href="https://beian.miit.gov.cn/"),
                        LinkItem(text="腾讯", href="https://v.qq.com/x/1"),
                        LinkItem(text="百科", href="https://baike.baidu.com/x"),
                    ),
                ),
            }
        )
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert "impossible_content:gov.cn" in rec.candidate_filter
        assert "excluded_host:baidu.com" in rec.candidate_filter
        assert rec.to_json()["candidate_filter"] == rec.candidate_filter

    async def test_报告重算存量存档(self):
        """字段引入之前跑的存档没有 ``search_blocked``。

        若直接 ``.get(...) or ""``，那些被验证码页拦下的存档会被算成
        「可跑」——恰恰是最不该算的那批。**缺字段 ≠ 空值。**
        存档里的观察数据都在，用同一个 :func:`detect_block` 确定性重算。
        """
        import json
        from trajectory_pipeline.executor.cli import cmd_report
        import argparse
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "T001__a.json").write_text(
                json.dumps(
                    {
                        "task_id": "T001",
                        "query": "q",
                        "ledger": {
                            "total_sites": 0,
                            "by_branch": {},
                            "missing_branches": [],
                        },
                        # 旧格式：没有 search_blocked 键
                        "search_observation": {
                            "url": "https://wappass.baidu.com/static/captcha/tuxing_v2.html?x=1",
                            "page_title": "百度安全验证",
                            "body_preview": "网络不给力，请稍后重试",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (root / "T018__b.json").write_text(
                json.dumps(
                    {
                        "task_id": "T018",
                        "query": "q",
                        "search_blocked": "",
                        "ledger": {
                            "total_sites": 1,
                            "by_branch": {"unresolved": 1},
                            "missing_branches": [],
                        },
                        "search_observation": {
                            "url": "https://www.baidu.com/s?wd=x",
                            "page_title": "功夫_百度搜索",
                            "body_preview": "功夫 在线观看",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            import io
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cmd_report(argparse.Namespace(out=str(root)))
            out = buf.getvalue()
        assert "被反爬拦截 1/2" in out
        assert "存量存档重算" in out
        assert "T001__a.json" in out
        # 显式记了空串的那条**不算**重算，且仍算可跑
        assert "可跑 1 个" in out

    async def test_报告重算用正文全文而非摘要(self):
        """限流词完全可能落在 400 字符摘要之外。

        正文改全文落盘后，若重算仍读摘要，这条会被判成「可跑」——
        而它一次搜索都没搜成。**漏判的方向正好是报表最不该放过的那批。**
        """
        import json
        from trajectory_pipeline.executor.cli import cmd_report
        import argparse
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "T001__a.json").write_text(
                json.dumps(
                    {
                        "task_id": "T001",
                        "query": "q",
                        "ledger": {
                            "total_sites": 0,
                            "by_branch": {},
                            "missing_branches": [],
                        },
                        # 新格式：正文全文，且限流词落在第 500 个字符之后
                        "search_observation": {
                            "url": "https://www.baidu.com/s?wd=x",
                            "page_title": "功夫_百度搜索",
                            "body_text": "正常搜索结果"
                            + "填充" * 300
                            + "访问过于频繁，请稍后再试",
                            "body_len": 615,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            import io
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cmd_report(argparse.Namespace(out=str(root)))
            out = buf.getvalue()
        assert "被反爬拦截 1/1" in out
        assert "rate_limit" in out


# ═══════════════════════════════════════════════════════════════════════
# report 的存档选取 + 成功域名分布（D7 验收）
#
# 这一节锁的都是**分母**与**归因**——报表上错这两样，结论直接反过来，
# 而错的那一栏还长得跟对的完全一样。
# ═══════════════════════════════════════════════════════════════════════


def _report(root):
    """跑一次 report 并返回它的输出。"""
    import argparse
    import contextlib
    import io
    from trajectory_pipeline.executor.cli import cmd_report

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_report(argparse.Namespace(out=str(root)))
    return buf.getvalue()


def _arch(tmp_path, name, *, outcomes, blocked="", total=None, **kw):
    """造一份存档。``by_branch`` **从 outcomes 现推**，不手填——

    手填就会造出「分支表说有 unresolved、outcomes 里没有」的存档，
    而 report 的两张表各读各的字段，那种存档测出来的东西是假的。
    """
    import json

    total = len(outcomes) if total is None else total
    by_branch: dict[str, int] = {}
    for o in outcomes:
        if o.get("branch"):
            by_branch[o["branch"]] = by_branch.get(o["branch"], 0) + 1
    (tmp_path / name).write_text(
        json.dumps(
            {
                "task_id": name.split("__")[0],
                "search_blocked": blocked,
                "ledger": {
                    "total_sites": total,
                    "by_branch": by_branch,
                    "missing_branches": [],
                },
                "outcomes": outcomes,
                **kw,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _o(url, branch=None, *, source="rule", fallback=False):
    return {"url": url, "branch": branch, "source": source, "fallback_used": fallback}


class TestReportArchiveSelection:
    def test_探针取证不算存档(self, tmp_path):
        """同目录还躺着 obscura_tools.json / probe_observe.json。按 ``*.json`` 收
        会把它们算成存档，于是「被拦 X/N」的分母偏大、拦截率被稀释。"""
        _arch(
            tmp_path,
            "T001__a.json",
            outcomes=[_o("https://a.test/x", "no_play_control")],
        )
        (tmp_path / "obscura_tools.json").write_text('{"tools": []}', encoding="utf-8")
        (tmp_path / "probe_observe.json").write_text('{"seen": 1}', encoding="utf-8")
        out = _report(tmp_path)
        assert "存档 1 个" in out

    def test_复核档取代原档不重复计数(self, tmp_path):
        """回填是覆盖式的，两份一起数等于同一批站点数两遍——
        成功数与分母同时翻倍。"""
        import json

        o = [_o("https://a.test/x", "unresolved")]
        _arch(tmp_path, "T001__a.json", outcomes=o)
        _arch(
            tmp_path,
            "T001__a.reviewed.json",
            outcomes=[
                {**o[0], "branch": None, "source": "human", "is_negative_sample": False}
            ],
        )
        out = _report(tmp_path)
        assert "存档 1 个" in out
        assert "已过人工复核" in out
        assert "访问 1 站 / 成功 1 站" in out, "人工确认的成功没被统计进来"

    def test_复核档是唯一被读的那份(self, tmp_path):
        """复核档说成功、原档说 unresolved 时，只能读出一个成功。

        读成两份就等于「未裁定时按原判、裁定时按裁定」各算一次，
        而同一批站点不能既是成功又不是失败。"""
        import json

        _arch(tmp_path, "T001__a.json", outcomes=[_o("https://a.test/x", "unresolved")])
        _arch(
            tmp_path,
            "T001__a.reviewed.json",
            outcomes=[_o("https://a.test/x", None, source="human")],
        )
        out = _report(tmp_path)
        assert "成功 1 站" in out
        # 若原档也读了，同一站会既算成功又算「未判 1」
        assert "未判 0" in out and "未判 1" not in out


class TestDomainReach:
    def test_成功按来源拆开(self, tmp_path):
        """「规则版找到了播放页」与「人工确认这站能看」是两件事。
        合起来报一个「成功 N」，结论只站得住其中一种。"""
        _arch(
            tmp_path,
            "T001__a.json",
            outcomes=[
                _o("https://v.youku.com/a", None, source="rule", fallback=True),
                _o("https://v.iqiyi.com/b", None, source="human"),
            ],
        )
        out = _report(tmp_path)
        assert "rule 1" in out and "human 1" in out

    def test_fallback成功单独点名(self, tmp_path):
        """存在性判定 ≠ 语义判定。fallback 判出的成功会虚高正样本基线，
        而报表不点破的话没人看得出来。"""
        _arch(
            tmp_path,
            "T001__a.json",
            outcomes=[_o("https://www.hao123.com/", None, fallback=True)],
        )
        out = _report(tmp_path)
        assert "fallback" in out
        assert "虚高" in out

    def test_零成功给出人工复核路径(self, tmp_path):
        """W1 的常态就是零成功（真实视频站没 <video> 标签）。
        报表不能只显示「成功 0」——得告诉人下一步走哪条路。"""
        _arch(
            tmp_path,
            "T001__a.json",
            outcomes=[
                _o("https://a.test/x", "unresolved"),
                _o("https://b.test/x", "unresolved"),
            ],
        )
        out = _report(tmp_path)
        assert "本批零成功" in out
        assert "review --write" in out

    def test_单次访问不进黑名单候选(self, tmp_path):
        """1 次访问 1 次失败说明不了任何事（这片可能没有、可能网络抖）。
        报出来只会让黑名单塞满一次性噪音。"""
        _arch(
            tmp_path,
            "T001__a.json",
            outcomes=[
                _o("https://rare.test/x", "no_play_control"),
                _o("https://common.test/1", "no_play_control"),
                _o("https://common.test/2", "unresolved"),
            ],
        )
        out = _report(tmp_path)
        assert "common.test 2" in out
        assert "rare.test" not in out.split("黑名单")[-1]

    def test_未判不进黑名单候选的分母混淆(self, tmp_path):
        """``unresolved`` 既不是成功也不是负样本，但它是**访问过**——
        分母里必须算它，否则通过率被虚高。"""
        _arch(
            tmp_path,
            "T001__a.json",
            outcomes=[
                _o("https://a.test/1", "unresolved"),
                _o("https://a.test/2", "unresolved"),
                _o("https://a.test/3", "unresolved"),
            ],
        )
        out = _report(tmp_path)
        assert "访问 3 站 / 成功 0 站" in out
        assert "未判 3" in out

    def test_域名只剥www(self, tmp_path):
        """``v.youku.com`` 与 ``www.youku.com`` 在反检测上是两个入口，
        归并掉就看不见「一个通一个不通」。"""
        from trajectory_pipeline.executor.cli import _site_domain

        assert _site_domain("https://www.iqiyi.com/v_x.html?a=1") == "iqiyi.com"
        assert _site_domain("https://v.youku.com/v_show/id_1.html") == "v.youku.com"
        assert _site_domain("不是 url") == ""

    def test_不读冗余的is_negative_sample(self, tmp_path):
        """分类从 ``branch`` 现算。

        存档里那份 ``is_negative_sample`` 是冗余字段，老存档可能压根没有；
        读它的话真负样本会静默掉进「未判」桶，而那一栏读起来仍然合理。
        """
        _arch(
            tmp_path,
            "T001__a.json",
            outcomes=[
                {"url": "https://a.test/1", "branch": "no_play_control"},  # 无该字段
                {"url": "https://a.test/2", "branch": "unresolved"},
            ],
        )
        out = _report(tmp_path)
        assert "负 1 · 未判 1" in out  # 真负样本没被掉进「未判」

    def test_口径与负样本池一致(self, tmp_path):
        """报表说「负」的那几条，负样本池里必须一条不少、一条不多。

        两边分叉的代价是**静默**的：报表和池子都看着正常，只是从某个切片
        开始少样本——而那是评估结论的输入。这条断言把「负」的判定锁死在
        同一个 ``NON_SAMPLE_BRANCHES`` 上。
        """
        import json

        from trajectory_pipeline.executor.archive import P1Archive
        from trajectory_pipeline.executor.branches import SiteOutcome

        outcomes = [
            {"url": "https://a.test/1", "branch": "no_play_control", "evidence": "e"},
            {"url": "https://a.test/2", "branch": "unresolved", "evidence": "e"},
            {"url": "https://a.test/3", "branch": "trailer_suspect", "evidence": "e"},
            {
                "url": "https://b.test/1",
                "branch": "component_unverified",
                "evidence": "e",
            },
            {"url": "https://b.test/2", "branch": None, "evidence": "e"},
        ]

        class _Rec:
            def __init__(self):
                self.ledger = type(
                    "L",
                    (),
                    {
                        "negative_samples": staticmethod(
                            lambda: [
                                SiteOutcome(
                                    url=o["url"], branch=o["branch"], evidence="e"
                                )
                                for o in outcomes
                            ]
                        )
                    },
                )()
                self.provenance = {}

            def to_json(self):
                return {
                    "task_id": "T001",
                    "search_blocked": "",
                    "ledger": {
                        "total_sites": len(outcomes),
                        "by_branch": {},
                        "missing_branches": [],
                    },
                    "outcomes": outcomes,
                }

        P1Archive(tmp_path).write(_Rec(), task_id="T001")
        pooled = {
            r["url"]
            for r in (
                json.loads(l)
                for l in (tmp_path / "negative.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )
        }
        assert pooled == {"https://a.test/1", "https://b.test/1"}
        assert "负 1 · 未判 2" in _report(tmp_path)  # a.test：1 负 2 未判


# ═══════════════════════════════════════════════════════════════════════
# 动作流（executor.actions）——P2 六件套第 ④⑤ 件的数据源
#
# 锁的是**会静默失效**的性质：动作少记一步、分支分错家、ref 漏进训练
# 数据，在报表上一个数都不变。放在本文件而不是单开一个模块，是因为
# fake driver 住在这儿——复制一份 fixture 就是「规则和 fixture 一起错」
# 那个坑（见本文件开头）。
# ═══════════════════════════════════════════════════════════════════════


EL = (InteractiveElement(ref="e5", role="button", name="立即播放"),)


class TestTargetOf:
    def test_解出两个槽(self):
        """槽位搬过去、**值不动**：这里不解「标签名 → ARIA role」。"""
        t = actions.target_of("e5", EL)
        assert (t.role, t.name) == ("button", "立即播放")
        # 存档投影跟着方言走（P4）：同一份 target 两种键名。
        assert t.to_json(ELEMENT_VOCAB_HTML_TAG) == {"tag": "button", "label": "立即播放"}
        assert t.to_json(ELEMENT_VOCAB_ARIA_ROLE) == {"role": "button", "name": "立即播放"}

    def test_解不出返回None(self):
        """fail-closed：ref 溯源不到就承认溯源不到，不拿第一个元素顶上。"""
        assert actions.target_of("e99", EL) is None

    def test_空ref返回None(self):
        assert actions.target_of("", EL) is None
        assert actions.target_of("e5", ()) is None

    def test_目标里没有ref(self):
        """结构保证，不是过滤规则——``ActionTarget`` 没有可放 ref 的字段。

        写成「序列化时把 ref 滤掉」的话，将来加字段时就会漏。
        """
        assert actions.target_of("e5", EL).to_json() == {
            "tag": "button",
            "label": "立即播放",
        }


class TestStepLog:
    def test_先记动作再回填观察(self):
        log = StepLog()
        i = log.act(actions.goto("https://x.test"))
        assert log.steps[i].observation is None
        obs = obs_at("https://x.test")
        log.settle(i, obs)
        assert log.steps[i].observation is obs

    def test_动作失败与没观察是两回事(self):
        """``observation=None`` 且 ``error=""`` 是「还没 observe」，
        而 ``error`` 非空是「试过且失败」。合成一种就丢了区分。"""
        log = StepLog()
        log.settle(log.act(actions.goto("u")), error="导航失败")
        assert log.steps[0].observation is None
        assert log.steps[0].error == "导航失败"


def _play_script(ref="e5"):
    return ScriptedPerceptor(
        {
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": ref}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK, {"media_count": 1}),
        }
    )


_PLAY_PAGES = {
    "https://x.test/movie": obs_at(
        "https://x.test/movie", elements=(("e5", "button", "立即播放"),)
    ),
    "https://x.test/play/e5": obs_at("https://x.test/play/e5", video=1),
}


class TestVisitSteps:
    async def test_成功路径三步(self):
        d = FakeDriver(dict(_PLAY_PAGES))
        r = await visit_site(d, _play_script(), RunLedger("T001"), CAND)
        assert [s.action.tool for s in r.steps.steps] == ["goto", "new_tab", "click"]
        assert all(s.error == "" for s in r.steps.steps)

    async def test_观察挂在动作上而不是自己成步(self):
        """``observe`` 是环境对上一个动作的回执。记成动作的话，
        训练集里每步都多一个模型**不该选**的选项。"""
        d = FakeDriver(dict(_PLAY_PAGES))
        r = await visit_site(d, _play_script(), RunLedger("T001"), CAND)
        got, _, click = r.steps.steps
        assert got.observation is r.site_obs
        assert click.observation is r.player_obs, "点击后的观察没挂到 click 上"

    async def test_点击目标是语义化的(self):
        d = FakeDriver(dict(_PLAY_PAGES))
        r = await visit_site(d, _play_script(), RunLedger("T001"), CAND)
        click = r.steps.steps[-1].action
        assert click.tool == "click"
        assert click.target.to_json() == {"tag": "button", "label": "立即播放"}

    async def test_new_tab标为infrastructure(self):
        """会话隔离是执行器自己做的，模型永远不该输出它——
        但它是真发生过的动作，删掉 P1 就无法回放。"""
        d = FakeDriver(dict(_PLAY_PAGES))
        r = await visit_site(d, _play_script(), RunLedger("T001"), CAND)
        assert [s.action.origin for s in r.steps.steps] == [
            "model",
            "infrastructure",
            "model",
        ]

    async def test_导航失败仍留下动作(self):
        """``unreachable_hard`` 在动作流里整段消失的话，
        负样本池里那条就成了没有动作的判决，说不清该学什么。"""
        r = await visit_site(
            FakeDriver(), ScriptedPerceptor({}), RunLedger("T001"), CAND
        )
        assert len(r.steps.steps) == 1
        assert r.steps.steps[0].action.tool == "goto"
        assert r.steps.steps[0].observation is None
        assert "导航失败" in r.steps.steps[0].error

    async def test_无播放控件时不记点击(self):
        """控制流在判断点 ③ 就返回了，动作流不该凭空多出一次点击——
        多出来会让「模型在没控件时点了什么」变成一个有答案的问题。"""
        d = FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")})
        p = ScriptedPerceptor(
            {
                Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
                Q.FIND_PLAY_CONTROL: no(Q.FIND_PLAY_CONTROL),
            }
        )
        r = await visit_site(d, p, RunLedger("T001"), CAND)
        assert [s.action.tool for s in r.steps.steps] == ["goto"]

    async def test_点击失败留下动作与原因(self):
        class Boom(FakeDriver):
            async def click(self, *, ref=None, selector=None):
                raise DriverError("元素已失效")

        d = Boom({"https://x.test/movie": _PLAY_PAGES["https://x.test/movie"]})
        r = await visit_site(d, _play_script(), RunLedger("T001"), CAND)
        click = r.steps.steps[-1]
        assert click.action.tool == "click"
        assert "元素已失效" in click.error

    async def test_ref溯源不到时按target为None记录(self):
        """I6 存疑的现场要**留证**，不是编一个目标补上——fail-closed 管的是
        「不猜」，不是「不点」：点不点仍由控制流决定，不由记录方式决定。"""
        d = FakeDriver(
            {
                "https://x.test/movie": obs_at("https://x.test/movie"),  # 无元素
                "https://x.test/play/e5": obs_at("https://x.test/play/e5", video=1),
            }
        )
        r = await visit_site(d, _play_script("e5"), RunLedger("T001"), CAND)
        assert r.steps.steps[-1].action.target is None
        assert d.clicked == ["e5"], "记录方式不该改变点击行为"
        assert any("I6" in n for n in (r.notes or []))


class TestArchivedSteps:
    def test_搜索与站点各有动作流(self):
        rec = RunRecord(
            task_id="T001", title="功夫", query="q", search_url="https://s.test?q=1"
        )
        i = rec.steps.act(actions.goto("https://s.test?q=1"))
        rec.steps.settle(i, obs_at("https://s.test?q=1"))
        data = rec.to_json()
        assert [s["action"]["tool"] for s in data["steps"]] == ["goto"]
        assert data["steps"][0]["observation"]["url"] == "https://s.test?q=1"

    def test_visit动作流随存档落盘(self):
        from trajectory_pipeline.executor.steps.visit import VisitResult

        rec = RunRecord(task_id="T001", title="功夫", query="q", search_url="u")
        log = StepLog()
        j = log.act(actions.click(actions.ActionTarget("button", "立即播放")))
        log.settle(j, obs_at("https://x.test/play", video=1))
        rec.visits.append(VisitResult(candidate=CAND, steps=log))
        visit = rec.to_json()["visits"][0]
        assert visit["steps"][0]["action"]["tool"] == "click"
        assert visit["steps"][0]["action"]["target"]["label"] == "立即播放"

    def test_存档可json序列化(self):
        rec = RunRecord(task_id="T001", title="t", query="q", search_url="u")
        rec.steps.act(actions.goto("u"))
        json.dumps(rec.to_json(), ensure_ascii=False)  # 契约要求可序列化

    async def test_动作参数里没有ref(self):
        """``ref`` 是会话内句柄，换个 session 就失效，进训练数据等于让
        模型学一个随机数，样本还永远不可回放。

        锁的是「不引入」而非「移除」：实测 4 份真实存档里 ``ref=`` 出现 0
        次——它从来没进过 P1（带 ref 的 ``FIND_PLAY_CONTROL`` 结论**只在
        失败路径上记账**，走到播放页就不记账）。所以 ``tag`` + ``label``
        比 P1 原来有的**更多**：原来「点了哪个元素」在存档里根本没留。

        用真 :class:`RulePerceptor`：``ScriptedPerceptor`` 的 evidence 是
        占位串，拿它断言等于什么都没断言。

        ⚠️ **2026-10-23（M-26）**：规则版的 ④ 不再凭媒体计数判成功
        （护栏可以否、不能许），所以这里**不能**再断言 ``r.success``。
        改断言两件**与成功无关**的事实：① 走到了播放页（否则下面的断言
        没有意义）；② **带 ref 的 ``FIND_PLAY_CONTROL`` 结论没有进账**
        ——那才是 ref 会泄漏的地方。
        """
        d = FakeDriver(dict(_PLAY_PAGES))
        ledger = RunLedger("T001")
        r = await visit_site(d, RulePerceptor(), ledger, CAND)
        assert r.player_obs is not None, "没走到播放页，下面的断言没有意义"
        assert not any(
            o.question == Q.FIND_PLAY_CONTROL for o in ledger.outcomes
        ), "带 ref 的 FIND_PLAY_CONTROL 结论被记账了——它含会话句柄"
        blob = json.dumps(
            [s.to_json(lambda o: None) for s in r.steps.steps], ensure_ascii=False
        )
        assert "e5" not in blob, "动作流里漏了会话句柄"
        assert "立即播放" in blob, "点了哪个元素必须留下语义化记录"

    async def test_老存档没有steps也能被消费(self, tmp_path):
        """动作流是新加的键。**字段引入之前跑的存档全都没有它**，
        而那批存档正是最该留着的（唯一的真实运行证据）。

        消费方一律用 ``.get(...) or []`` 取——这里显式钉住：改 P1 形状时
        很容易顺手把 ``archive["visits"][i]["steps"]`` 写成直取，
        而那会让老存档在复核队列那一步整批消失。
        """
        from trajectory_pipeline.executor.review_queue import collect

        old = {
            "task_id": "T001",
            "search_blocked": "",
            "ledger": {
                "total_sites": 1,
                "by_branch": {"unresolved": 1},
                "missing_branches": [],
            },
            "outcomes": [
                {
                    "url": "https://a.test/x",
                    "branch": "unresolved",
                    "evidence": "e",
                    "reached_play_page": False,
                }
            ],
            "visits": [
                {
                    "url": "https://a.test/x",
                    "landed_url": "https://a.test/x",
                    "success": False,
                    "notes": [],
                    "site_obs": {
                        "url": "https://a.test/x",
                        "page_title": "T",
                        "body_text": "正文",
                        "video_tag_count": 0,
                        "iframe_count": 0,
                    },
                    "player_obs": None,
                }
            ],
        }
        (tmp_path / "T001__old.json").write_text(
            json.dumps(old, ensure_ascii=False), encoding="utf-8"
        )
        items = collect(tmp_path)
        assert len(items) == 1, "老存档在复核队列这一步整批消失了"
        assert items[0].url == "https://a.test/x"

    async def test_动作流不参与控制流(self):
        """记录方式与判定彻底解耦：动作流里有 ``target=None`` 的点击，
        控制流照常按 ``control.payload['ref']`` 决定点哪个。

        两者一旦耦合，改「记什么」就会改「做什么」——而 P2 落地后必然要改
        动作流的记法（要喂训练视图了），那时就会发现改记录等于改行为。
        """
        d = FakeDriver(
            {
                "https://x.test/movie": obs_at(
                    "https://x.test/movie"
                ),  # 无元素 → target=None
                "https://x.test/play/e5": obs_at("https://x.test/play/e5", video=1),
            }
        )
        r = await visit_site(d, _play_script("e5"), RunLedger("T001"), CAND)
        assert r.steps.steps[-1].action.target is None
        assert d.clicked == ["e5"], "点击行为被记录方式改变了"

    async def test_动作名都在声明的原语表内(self):
        """P2 会拿 ``actions.TOOLS`` 当**训练动作空间**。

        表里列了执行流从未产生过的动作，就等于训出一批永远不会被真实运行
        验证的样本——而那种样本在离线评测里是绿的。控制流哪天发了新原语，
        这里再跟着加（那时它才有真实存档兜底）。
        """
        d = FakeDriver(dict(_PLAY_PAGES))
        r = await visit_site(d, RulePerceptor(), RunLedger("T001"), CAND)
        used = {s.action.tool for s in r.steps.steps}
        assert used <= set(actions.TOOLS), f"未登记的原语: {used - set(actions.TOOLS)}"
        # 反向：表里不该有「设计上不会成为动作」的 observe
        assert "observe" not in actions.TOOLS


class TestTitleIsRequired:
    """片名为空必须**炸**，不得拿 task_id 顶替。

    实测（2026-10-09）：``run`` 少了 ``--title`` 就拿 ``T001`` 当片名，
    搜回来的是**轮胎** T001（泰坦途 Turanza 胎压监测），于是判断点 ①
    交出 6 条 ``not_play_site``，每条理由都写得通顺——「T001（泰坦途）
    轮胎商品详情页，非影视作品观看页」。整批零成功、零异常，
    **每一条都是垃圾**。

    这是本文件里唯一一个「整批作废但零报错」的失败模式，
    也是最该在结构上堵死的那一种：读存档时看到的只是一份
    「模型很有道理的拒绝记录」，没人会去怀疑片名。
    """

    @pytest.mark.parametrize("title", ["", "   ", "\n"])
    async def test_空片名抛错(self, title):
        orch = Orchestrator(FakeDriver({}), ScriptedPerceptor({}), RunConfig())
        with pytest.raises(ValueError, match="片名"):
            await orch.run("T001", title)

    async def test_错误信息点明task_id不能顶替(self):
        """错误串要能指导下一步——光说「片名缺失」，读的人不知道
        该检查计划还是该检查命令行。"""
        orch = Orchestrator(FakeDriver({}), ScriptedPerceptor({}), RunConfig())
        with pytest.raises(ValueError) as ei:
            await orch.run("T001", "")
        assert "task_id" in str(ei.value)

    async def test_抛错前没有发出任何动作(self):
        """守卫必须在**建记录与导航之前**：一旦已经搜了、已经点了，
        半批产物已经落进 pools，抛错也来不及了。"""
        d = FakeDriver({})
        with pytest.raises(ValueError):
            await Orchestrator(d, ScriptedPerceptor({}), RunConfig()).run("T001", "")
        assert d.visited == [], f"抛错前已经导航过：{d.visited}"
        assert d.clicked == [] and d.observe_calls == 0

    async def test_正常片名不受影响(self):
        search = (
            "https://www.bing.com/search?q=%E5%8A%9F%E5%A4%AB+"
            "%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B"
        )
        pages = {
            search: Observation(
                url=search,
                page_title="搜索",
                body_text="结果",
                links=(LinkItem(text="功夫 在线观看", href="https://www.a.test/m"),),
            )
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor({}), RunConfig()
        ).run("T001", "功夫")
        assert rec.title == "功夫"


class TestRationale接线:
    """模块 4 接线（2026-10-10）：执行期逐步生成 → P1 的 ``steps[i].rationale``。

    **生成在执行期、核查在组装期**是设计决定不是漏掉的——时序核查需要
    全序列，执行期那一刻后续步还不存在。
    """

    SEARCH = TestClassifyCandidatesWiring.SEARCH

    def _pages(self):
        return {
            self.SEARCH: Observation(
                url=self.SEARCH,
                page_title="百度",
                body_text="搜索结果",
                links=(LinkItem(text="a站", href="https://www.a.test/movie"),),
            ),
            "https://www.a.test/movie": obs_at(
                "https://www.a.test/movie", elements=(("e1", "button", "播放"),)
            ),
            "https://x.test/play/e1": obs_at("https://x.test/play/e1", video=1),
        }

    def _writer(self, sink):
        def write(**kw):
            sink.append(kw)
            return {
                "status": "written",
                "rationale": {"text": f"第 {len(sink)} 步的思考", "conclusion": ""},
                "reason": "",
                "degraded": [],
            }

        return write

    async def test_每一步都生成(self):
        sink: list = []
        script = {
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "video_site": [
                        {"url": "https://www.a.test/movie", "why": "含片名"}
                    ]
                },
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            FakeDriver(self._pages()),
            ScriptedPerceptor(script),
            RunConfig(),
            rationale_writer=self._writer(sink),
        ).run("T001", "功夫", user_prompt="有能看的吗")

        steps = rec.visits[0].steps.steps
        tools = [s.action.tool for s in steps]
        # 步数不写死（执行器可能插入会话隔离动作）——只钉「每步都生成、
        # 且拿到的是该步自己的东西」。搜索步（顶层 record.steps）也在补记
        # 之列，所以 sink 数量 = 搜索步 + 访问步步数。
        assert tools[0] == "goto" and "click" in tools
        assert len(sink) == 1 + len(steps)
        assert all(s.rationale and s.rationale["status"] == "written" for s in steps)
        # 搜索步本身也有 rationale（2026-10-11 补生成闭环）
        assert (
            rec.steps.steps[0].rationale
            and rec.steps.steps[0].rationale["status"] == "written"
        )

        by_tool = {kw["action"]["tool"]: kw for kw in sink}
        # 片名与用户原话都传下去了（豁免域靠它们）
        assert by_tool["goto"]["target_title"] == "功夫"
        assert by_tool["goto"]["user_prompt"] == "有能看的吗"
        # INC46：history 是 ``(动作, 观察)`` 对——动作前动机要依据历史观察，
        # 但**不含本步观察**（本步还没执行）。
        assert [a["tool"] for a, _ in by_tool["click"]["history"]] == tools[
            : tools.index("click")
        ]
        # 本步观察没有入口：喂了它，模型就会描述一个还没看到的页面
        assert "observation" not in by_tool["click"]
        assert "error" not in by_tool["click"]

    async def test_落盘进存档(self):
        sink: list = []
        script = {
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "video_site": [
                        {"url": "https://www.a.test/movie", "why": "w"}
                    ]
                },
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            FakeDriver(self._pages()),
            ScriptedPerceptor(script),
            RunConfig(),
            rationale_writer=self._writer(sink),
        ).run("T001", "功夫")
        js = rec.to_json()
        # 搜索步与访问步都落盘进存档（搜索步在顶层 steps[0]，
        # 2026-10-11 补生成闭环后不再恒 null）
        assert js["steps"][0]["rationale"]["status"] == "written"
        got = js["visits"][0]["steps"][0]["rationale"]
        assert got["status"] == "written"
        assert got["rationale"]["text"] == "第 2 步的思考"

    async def test_没有writer时该字段为None(self):
        """W1 规则版批次（无后端）——**不生成占位**，
        闸门据此记 not_run 而不是 passed。搜索步同样不生成。"""
        script = {
            Q.CLASSIFY_SEARCH_LINKS: none(Q.CLASSIFY_SEARCH_LINKS, "W1 无语义能力")
        }
        rec = await Orchestrator(
            FakeDriver(self._pages()), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")
        assert rec.visits[0].steps.steps[0].rationale is None
        assert rec.steps.steps[0].rationale is None

    async def test_writer抛异常不中断采集(self):
        """rationale 是产物，采集是主业——**绝不让它拖垮一次真实访问**。"""

        def boom(**kw):
            raise RuntimeError("后端炸了")

        script = {
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "video_site": [
                        {"url": "https://www.a.test/movie", "why": "w"}
                    ]
                },
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            FakeDriver(self._pages()),
            ScriptedPerceptor(script),
            RunConfig(),
            rationale_writer=boom,
        ).run("T001", "功夫")

        assert rec.visits and rec.visits[0].success
        assert rec.visits[0].steps.steps[0].rationale is None
        assert any("rationale 生成异常" in w for w in rec.warnings)


# ═══════════════════════════════════════════════════════════════════════
# 环节 ①.6：站点首页核（代码层确定性事实）
# ═══════════════════════════════════════════════════════════════════════


class Test站点首页核:
    """实测（2026-10-11）：hao123 被搜索引擎当成《功夫》的搜索结果返回。

    访问后它按「页面上没有播放控件」入负样本池——**结论方向碰巧对了，
    理由全错**。那个站根本不是内容站，问题是「压根不是搜索结果该返回的
    东西」，不是「去了发现看不了」。两类混成一条，负样本池就再也答不出
    「为什么看不了」，而那正是它存在的唯一理由。

    **为什么不是加进** ``EXCLUDED_HOST_ROOTS``：那样它压根不成为候选，
    ``not_play_site`` 永远 0 条——而「每条分支都必须有对应样本入库」是
    硬要求，删掉就再也长不出来。
    """

    SEARCH = "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B"

    def _search_page(self, *links):
        return Observation(
            url=self.SEARCH,
            page_title="百度",
            body_text="搜索结果",
            links=tuple(LinkItem(text=t, href=h) for t, h in links),
        )

    def _pages(self, *links):
        pages = {self.SEARCH: self._search_page(*links)}
        for _, href in links:
            if href.startswith("https://") and "baidu" not in href:
                pages[href] = obs_at(href, elements=(("e1", "button", "播放"),))
                pages["https://x.test/play/e1"] = obs_at(
                    "https://x.test/play/e1", video=1
                )
        return pages

    async def test_首页记not_play_site(self):
        pages = self._pages(
            ("导航站首页", "https://www.hao123.com/"),
            ("a站", "https://www.a.test/movie"),
        )
        script = {
            # 分类把两个都判成 video_site（direct 目标类）——首页核的
            # 判据是 URL 结构（path 为空），与分类无关，照样剔
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "video_site": [
                        {"url": "https://www.hao123.com/", "why": "看着像"},
                        {"url": "https://www.a.test/movie", "why": "标题含片名"},
                    ]
                },
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")

        dropped = [o for o in rec.ledger.outcomes if o.url == "https://www.hao123.com/"]
        assert len(dropped) == 1
        assert dropped[0].branch == "not_play_site"

    async def test_首页不被访问(self):
        """访问它只会得出「这页没有播放控件」——那是导航站的属性，
        与《功夫》能不能看毫无关系。访问它本身就是浪费一次浏览器往返。"""
        pages = self._pages(
            ("导航站首页", "https://www.hao123.com/"),
            ("a站", "https://www.a.test/movie"),
        )
        script = {
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "video_site": [
                        {"url": "https://www.hao123.com/", "why": "看着像"},
                        {"url": "https://www.a.test/movie", "why": "标题含片名"},
                    ]
                },
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")

        assert "https://www.hao123.com/" not in [c.url for c in rec.candidates]
        assert [v.candidate.url for v in rec.visits] == ["https://www.a.test/movie"]

    async def test_判断点无结论时照样剔除(self):
        """**实测那批走的正是这条路**：分类任务判 None → 候选全部保留遍历。

        首页核挂在分类分发**之后**（orchestrator 环节 ①.6），分类的
        fail-closed 路径照样要过这道核——这正是首页混进
        负样本池的实际路径。"""
        pages = self._pages(
            ("导航站首页", "https://www.hao123.com/"),
            ("a站", "https://www.a.test/movie"),
        )
        script = {
            Q.CLASSIFY_SEARCH_LINKS: none(Q.CLASSIFY_SEARCH_LINKS, "模型未作答"),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")

        dropped = [o for o in rec.ledger.outcomes if o.url == "https://www.hao123.com/"]
        assert [o.branch for o in dropped] == ["not_play_site"]
        assert [v.candidate.url for v in rec.visits] == ["https://www.a.test/movie"]

    async def test_内容页不受影响(self):
        """反向护栏：别把带路径的内容页一起剔掉。path 不为空的
        一律不算首页——判据窄是刻意的，判宽了会误伤正样本。"""
        pages = self._pages(
            ("a站", "https://www.a.test/movie"), ("根路径站", "https://b.test/x")
        )
        script = {
            Q.SELECT_PLAY_SITES: ok(
                Q.SELECT_PLAY_SITES,
                {
                    "selected": [
                        {"url": "https://www.a.test/movie", "why": "w"},
                        {"url": "https://b.test/x", "why": "w"},
                    ],
                    "rejected": [],
                },
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            FakeDriver(pages), ScriptedPerceptor(script), RunConfig()
        ).run("T001", "功夫")
        assert len(rec.visits) == 2

    def test_首页判据只认path(self):
        """带 query 的仍是首页——``?from=baidu`` 不改变它是首页这件事。"""
        from trajectory_pipeline.executor.orchestrator import _is_site_homepage

        assert _is_site_homepage("https://www.hao123.com/") is True
        assert _is_site_homepage("https://www.hao123.com") is True
        assert _is_site_homepage("https://www.hao123.com/?from=baidu") is True
        assert _is_site_homepage("https://www.hao123.com/#top") is True
        assert _is_site_homepage("https://www.hao123.com/movie") is False
        assert _is_site_homepage("https://v.qq.com/x/cover/abc.html") is False

    def test_畸形url不判首页(self):
        """拿不准时**不判**。这条判据的全部价值在「确定」，
        不确定就不该用它去剔候选。"""
        from trajectory_pipeline.executor.orchestrator import _is_site_homepage

        assert _is_site_homepage("https://[::1") is False


# ═══════════════════════════════════════════════════════════════════════
# 路径 B/C 发现链：三路径分叉 + 首页站内搜索（2026-10-16）
# ═══════════════════════════════════════════════════════════════════════


class TabbedDriver(FakeDriver):
    """外链校验 / 站内搜索需要的 fake：``new_tab`` 切页、``close_tab``
    恢复、``press("Enter")`` 按 ``enter_targets`` 跳转。

    基类 ``FakeDriver`` 的 ``new_tab`` 只记账不切页——对既有测试
    （infrastructure 步骤不进消息视图）是对的，但外链校验链的
    ``observe()`` 读的是**新 tab 的页面**，不切页会让每条外链都落回
    中间页自己（same_host → dropped），链路根本测不出来。子类化而不
    是改基类：两个需求都正当，不该互相将就。
    """

    def __init__(self, pages, enter_targets=None):
        super().__init__(pages)
        self._tab_stack: list[Observation | None] = []
        #: 当前页 URL → 回车后跳去的 URL（站内搜索提交）
        self.enter_targets = dict(enter_targets or {})

    async def new_tab(self, url: str | None = None) -> str:
        self.tabs.append(url or "")
        self._tab_stack.append(self.current)
        if url and url in self.pages:
            self.current = self.pages[url]
        return f"tab{len(self.tabs)}"

    async def close_tab(self, tab_id: str | None = None) -> None:
        if self._tab_stack:
            self.current = self._tab_stack.pop()

    async def press(self, key: str, *, role=None, name=None) -> None:
        target = self.enter_targets.get(self.current.url if self.current else "")
        if target and target in self.pages:
            self.current = self.pages[target]


class Test路径BC发现链:
    """三路径分叉（direct / aggregator / article）与 B/C 首页站内搜索。

    设计口径见 ``.trae/documents/路径BC发现链实施计划.md``：direct
    不动（回归由既有测试守住），B/C 的差异落在检索词、①.5 选题、
    ①.6 中间页遍历、visit ③.5 站内搜索分叉四处。
    """

    #: B 路径的搜索页 URL：聚合词按 task 轮换，测试与 orchestrator
    #: 用同一对函数计算——硬编码会在词表调整时静默失配（FakeDriver
    #: 对未登记 URL 直接抛 DriverError，表现为「搜索页都打不开」）。
    AGG_SEARCH = search_step.search_url(search_step.aggregator_query("T001"))

    SEARCH = (
        "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+"
        "%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B"
    )

    def _search_page(self, url, *links):
        return Observation(
            url=url,
            page_title="百度",
            body_text="搜索结果",
            links=tuple(LinkItem(text=t, href=h) for t, h in links),
        )

    def _play_pages(self, *urls):
        """内容站页面：播放按钮 e1 + 点击后的播放页（visit 链两跳）。"""
        pages = {
            u: obs_at(u, elements=(("e1", "button", "播放"),)) for u in urls
        }
        pages["https://x.test/play/e1"] = obs_at(
            "https://x.test/play/e1", video=1
        )
        return pages

    async def test_B路径聚合词检索到aggregator候选(self):
        """B 全链：聚合词检索 → ①B 选题 → 中间页感知层 None 时启发式
        全量（fail-closed 不中断）→ 外链校验产出 ``origin="aggregator"``
        候选 → 标准 visit 链收尾。"""
        pages = {
            self.AGG_SEARCH: self._search_page(
                self.AGG_SEARCH, ("聚合站", "https://agg.test/")
            ),
            "https://agg.test/": obs_at(
                "https://agg.test/",
                links=(LinkItem(text="P1站", href="https://www.p1.test/movie"),),
            ),
        }
        pages.update(self._play_pages("https://www.p1.test/movie"))
        script = {
            # A1：B 路径的分类分发取 aggregator 类（旧 ①B 选题的分叉）
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "aggregator": [
                        {"url": "https://agg.test/", "why": "w"}
                    ]
                },
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        perceptor = ScriptedPerceptor(script)
        rec = await Orchestrator(
            TabbedDriver(pages), perceptor,
            RunConfig(discovery_path="aggregator"),
        ).run("T001", "功夫")

        # 检索词与片名解耦：query 是聚合词，不是「功夫 在线观看」
        assert rec.query in search_step.AGGREGATOR_QUERIES
        assert rec.search_url == self.AGG_SEARCH
        assert rec.discovery_path == "aggregator"
        assert Q.CLASSIFY_SEARCH_LINKS in perceptor.calls
        # 中间页：IS_AGGREGATOR / SELECT_SITE_ENTRIES 未配置（None）→
        # fail-closed 启发式全量，产出带溯源的最终候选
        assert len(rec.intermediates) == 1
        assert rec.intermediates[0].role == "aggregator"
        assert [c.url for c in rec.candidates] == ["https://www.p1.test/movie"]
        assert rec.candidates[0].origin == "aggregator"
        assert rec.candidates[0].parent == "https://agg.test/"
        # 最终候选（非首页）走标准 visit 链，不做站内搜索
        assert [v.candidate.url for v in rec.visits] == ["https://www.p1.test/movie"]
        assert rec.visits[0].search is None

    async def test_B路径首页候选走站内搜索子候选不再搜(self):
        """首页型候选不丢弃、不做控件判定（§5.14 前置事实污染）：
        先站内搜索定位深层页，派发的子候选进标准 visit 链，且
        **子候选不再触发站内搜索**——「首页→搜索→结果页又含首页→
        再搜」的循环派发真会发生。"""
        home = "https://www.p2.test/"
        results = "https://www.p2.test/so?wd=%E5%8A%9F%E5%A4%AB"
        deep = "https://www.p2.test/v/1"
        pages = {
            self.AGG_SEARCH: self._search_page(
                self.AGG_SEARCH, ("聚合站", "https://agg.test/")
            ),
            "https://agg.test/": obs_at(
                "https://agg.test/",
                links=(LinkItem(text="P2站", href=home),),
            ),
            # P2 是首页：有搜索框 s1，回车后到结果页
            home: obs_at(home, elements=(("s1", "input", "搜索片名"),)),
            results: obs_at(
                results, links=(LinkItem(text="功夫正片", href=deep),),
            ),
        }
        pages.update(self._play_pages(deep))
        script = {
            # A1：B 路径的分类分发取 aggregator 类（旧 ①B 选题的分叉）
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "aggregator": [
                        {"url": "https://agg.test/", "why": "w"}
                    ]
                },
            ),
            Q.FIND_SITE_SEARCH: ok(Q.FIND_SITE_SEARCH, {"ref": "s1"}),
            # 站内搜索结果页复用 ①（题意原样成立）——SELECT_* 在 A1 后
            # 的唯一调用方就是这里
            Q.SELECT_PLAY_SITES: ok(
                Q.SELECT_PLAY_SITES,
                {"selected": [{"url": deep, "why": "w"}], "rejected": []},
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            TabbedDriver(pages, enter_targets={home: results}),
            ScriptedPerceptor(script),
            RunConfig(discovery_path="aggregator"),
        ).run("T001", "功夫")

        # 首页候选没被丢弃也没做控件判定，先搜索后核验
        assert [v.candidate.url for v in rec.visits] == [home, deep]
        first = rec.visits[0]
        assert first.search is not None
        assert first.search.status == site_search_step.ROUTED
        assert [c.url for c in first.search.selected] == [deep]
        assert first.search.selected[0].origin == "site_search"
        assert first.search.selected[0].parent == results
        # 子候选走标准链，且不再触发站内搜索（防循环派发）
        assert rec.visits[1].candidate.origin == "site_search"
        assert rec.visits[1].search is None
        branches = [o.branch for o in rec.ledger.outcomes]
        assert "site_search_routed" in branches
        # B 路径不丢首页：那是站内搜索的入口，不是「找错站」
        assert "not_play_site" not in branches

    async def test_C路径文章引荐产出article候选(self):
        """C 全链：片名检索（与 A 同查询）→ ①C 选题 → 文章页直接选
        外链（无页面确认题，``is_intermediate`` 恒 None）→
        ``origin="article"`` 候选。"""
        pages = {
            self.SEARCH: self._search_page(
                self.SEARCH, ("影评", "https://blog.test/post/1")
            ),
            "https://blog.test/post/1": obs_at(
                "https://blog.test/post/1",
                links=(LinkItem(text="在线看", href="https://www.p1.test/movie"),),
            ),
        }
        pages.update(self._play_pages("https://www.p1.test/movie"))
        script = {
            # A1：C 路径的分类分发取 article_post 类（旧 ①C 选题的分叉）
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "article_post": [
                        {"url": "https://blog.test/post/1", "why": "w"}
                    ]
                },
            ),
            Q.SELECT_REFERRAL_LINKS: ok(
                Q.SELECT_REFERRAL_LINKS,
                {"selected": [{"url": "https://www.p1.test/movie", "why": "w"}],
                 "rejected": []},
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            TabbedDriver(pages), ScriptedPerceptor(script),
            RunConfig(discovery_path="article"),
        ).run("T001", "功夫")

        assert rec.discovery_path == "article"
        assert "功夫" in rec.query  # C 与 A 同查询（文章按片名长出来）
        assert len(rec.intermediates) == 1
        assert rec.intermediates[0].role == "article"
        # C 没有「这是不是文章页」的确认题——①C 选题时已判过
        assert rec.intermediates[0].is_intermediate is None
        assert [c.url for c in rec.candidates] == ["https://www.p1.test/movie"]
        assert rec.candidates[0].origin == "article"
        assert rec.candidates[0].parent == "https://blog.test/post/1"
        assert [v.candidate.url for v in rec.visits] == ["https://www.p1.test/movie"]

    async def test_中间页候选按host跨页去重(self):
        """两个聚合站引荐同一个站：第二个是重复发现不是新信息。
        opaque（无 host）不去重——没有依据的合并与没有依据的排除
        是同一种错。"""
        pages = {
            self.AGG_SEARCH: self._search_page(
                self.AGG_SEARCH,
                ("聚合1", "https://agg1.test/"), ("聚合2", "https://agg2.test/"),
            ),
            "https://agg1.test/": obs_at(
                "https://agg1.test/",
                links=(LinkItem(text="P1", href="https://www.p1.test/movie"),),
            ),
            "https://agg2.test/": obs_at(
                "https://agg2.test/",
                links=(LinkItem(text="P1备用", href="https://www.p1.test/movie2"),),
            ),
        }
        pages.update(self._play_pages(
            "https://www.p1.test/movie", "https://www.p1.test/movie2"
        ))
        script = {
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "aggregator": [
                        {"url": "https://agg1.test/", "why": "w"},
                        {"url": "https://agg2.test/", "why": "w"},
                    ]
                },
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            TabbedDriver(pages), ScriptedPerceptor(script),
            RunConfig(discovery_path="aggregator"),
        ).run("T001", "功夫")

        # 两个中间页都访问了，但同 host 只保留首个（保序）
        assert len(rec.intermediates) == 2
        assert [c.url for c in rec.candidates] == ["https://www.p1.test/movie"]
        assert [v.candidate.url for v in rec.visits] == ["https://www.p1.test/movie"]

    async def test_中间页超上限截断保序(self):
        """截断是预算控制不是语义筛选：保序、记 warning、不中断。"""
        pages = {
            self.AGG_SEARCH: self._search_page(
                self.AGG_SEARCH,
                *((f"聚合{i}", f"https://agg{i}.test/") for i in range(3)),
            ),
        }
        for i in range(3):
            pages[f"https://agg{i}.test/"] = obs_at(
                f"https://agg{i}.test/",
                links=(LinkItem(text="P", href=f"https://www.p{i}.test/movie"),),
            )
        pages.update(self._play_pages(
            *[f"https://www.p{i}.test/movie" for i in range(3)]
        ))
        script = {
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "aggregator": [
                        {"url": f"https://agg{i}.test/", "why": "w"}
                        for i in range(3)
                    ]
                },
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        rec = await Orchestrator(
            TabbedDriver(pages), ScriptedPerceptor(script),
            RunConfig(discovery_path="aggregator", max_intermediates=2),
        ).run("T001", "功夫")

        assert len(rec.intermediates) == 2
        assert [c.url for c in rec.candidates] == [
            "https://www.p0.test/movie", "https://www.p1.test/movie",
        ]
        assert any("超上限" in w for w in rec.warnings)

    async def test_B路径忽略传入检索词并记警告(self):
        """taskgen 的检索词按 persona 渲染，B 用不上——静默忽略会让
        「这批 B 的 query 为什么是聚合词」查无对证，warning 一句话
        就能说清。"""
        pages = {
            self.AGG_SEARCH: self._search_page(
                self.AGG_SEARCH, ("聚合站", "https://agg.test/")
            ),
        }
        rec = await Orchestrator(
            TabbedDriver(pages), ScriptedPerceptor({}),
            RunConfig(discovery_path="aggregator"),
        ).run("T001", "功夫", search_query="功夫 在线观看")

        assert rec.query in search_step.AGGREGATOR_QUERIES
        assert any("忽略传入的检索词" in w for w in rec.warnings)

    async def test_发现链关键节点日志(self, caplog):
        """排查用的实时日志（与 record.warnings 分工：log 给跑批当场
        看，warnings 给复盘落盘读）。B 全链的五个路标——启动 / 取候选 /
        中间页遍历 / 站点遍历 / 收尾——少一段就是排查时少一个路标，
        而「哪段没打」只有在跑真批卡住时才会暴露，届时再找来不及。"""
        import logging as _logging

        pages = {
            self.AGG_SEARCH: self._search_page(
                self.AGG_SEARCH, ("聚合站", "https://agg.test/")
            ),
            "https://agg.test/": obs_at(
                "https://agg.test/",
                links=(LinkItem(text="P1站", href="https://www.p1.test/movie"),),
            ),
        }
        pages.update(self._play_pages("https://www.p1.test/movie"))
        script = {
            Q.CLASSIFY_SEARCH_LINKS: ok(
                Q.CLASSIFY_SEARCH_LINKS,
                {
                    "aggregator": [
                        {"url": "https://agg.test/", "why": "w"}
                    ]
                },
            ),
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        with caplog.at_level(
            _logging.INFO, logger="trajectory_pipeline.executor.orchestrator"
        ):
            await Orchestrator(
                TabbedDriver(pages), ScriptedPerceptor(script),
                RunConfig(discovery_path="aggregator"),
            ).run("T001", "功夫")

        msgs = " || ".join(caplog.messages)
        for marker in (
            "发现链启动",
            "环节① 取回候选",
            "中间页遍历开始",
            "中间页（aggregator）",
            "环节②",
            "发现链结束",
        ):
            assert marker in msgs, f"关键节点日志缺失：{marker}"
