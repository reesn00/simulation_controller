"""``executor.branches`` 单测。

重点测**校验**而不是计数：分支名写错必须当场炸，
「静默落盘一条分支名拼错的负样本」是这类系统最典型的慢性病。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.executor.branches import (
    BRANCH_LABELS,
    NON_SAMPLE_BRANCHES,
    RunLedger,
    UnknownBranch,
    decision_branch,
)
from trajectory_pipeline.perception import questions
from trajectory_pipeline.perception.base import Decision, Observation, Q


def dec(question: str, answer: bool | None, **kw) -> Decision:
    return Decision(
        question=question, answer=answer, confidence=1.0,
        evidence=kw.pop("evidence", "e"), source="rule",
        payload=kw.pop("payload", {}), fallback_used=kw.pop("fallback_used", False),
    )


class TestLedgerValidation:
    def test_合法分支可记(self):
        ledger = RunLedger("T001")
        for b in questions.all_branches():
            ledger.record("https://x.test/", b, "理由")
        assert len(ledger.outcomes) == len(questions.all_branches())

    def test_未知分支当场抛错(self):
        ledger = RunLedger("T001")
        with pytest.raises(UnknownBranch):
            ledger.record("https://x.test/", "not_a_branch", "理由")
        assert ledger.outcomes == []          # 抛错时不得留下半条记录

    def test_成功可记空分支(self):
        ledger = RunLedger("T001")
        out = ledger.record("https://x.test/", None, "有播放组件")
        assert out.branch is None
        assert ledger.succeeded == 1

    def test_每个分支都有中文标签(self):
        """Label Studio 与报表直接用这张表，不在下游做二次翻译。"""
        for b in questions.all_branches():
            assert BRANCH_LABELS.get(b), f"{b} 缺标签"


class TestSampleClassification:
    def test_unresolved_不算负样本(self):
        """I4 的直接后果：没判出来 ≠ 失败。混进负样本池会污染纯度指标，
        且这种污染在报表上看不出来（都表现为「有一条负样本」）。"""
        ledger = RunLedger("T001")
        ledger.record("https://x.test/", "unresolved", "规则版不具备语义能力")
        assert ledger.negative_samples() == []
        assert ledger.summary()["negative_samples"] == 0

    def test_trailer_suspect_也不算负样本(self):
        ledger = RunLedger("T001")
        ledger.record("https://x.test/", "trailer_suspect", "词表判不准")
        assert ledger.negative_samples() == []

    def test_真负样本进池(self):
        ledger = RunLedger("T001")
        ledger.record("https://x.test/", "no_play_control", "无控件")
        ledger.record("https://y.test/", "login_wall_or_blocked", "登录墙")
        assert len(ledger.negative_samples()) == 2


class TestSummary:
    def test_缺失分支可见(self):
        """八条里有哪条没产出——这比「各分支数量」更能说明问题。
        数量为 0 可能只是样本少，missing 会直接指向「这条路径没接上」。"""
        ledger = RunLedger("T001")
        ledger.record("https://x.test/", "no_play_control", "e")
        s = ledger.summary()
        assert "no_play_control" in s["by_branch"]
        assert "login_wall_or_blocked" in s["missing_branches"]
        assert len(s["missing_branches"]) == len(questions.all_branches()) - 1

    def test_全分支覆盖时_missing_为空(self):
        ledger = RunLedger("T001")
        for b in questions.all_branches():
            ledger.record("https://x.test/", b, "e")
        assert ledger.summary()["missing_branches"] == []


class TestDecisionMapping:
    def test_none_映射_unresolved(self):
        assert decision_branch(dec(Q.IS_REACHABLE, None)) == "unresolved"

    def test_false_映射该题的负分支(self):
        assert decision_branch(dec(Q.IS_REACHABLE, False)) == "login_wall_or_blocked"
        assert decision_branch(dec(Q.FIND_PLAY_CONTROL, False)) == "no_play_control"
        assert decision_branch(dec(Q.PLAYER_OK, False)) == "component_unverified"

    def test_true_不是失败分支(self):
        assert decision_branch(dec(Q.PLAYER_OK, True)) == ""

    def test_映射有唯一出处(self):
        """判 false 归哪支只在 questions 里定义一次——控制流不许硬编码字符串。"""
        assert decision_branch(dec(Q.SELECT_PLAY_SITES, False)) == "not_play_site"

    def test_record_from_走同一映射(self):
        ledger = RunLedger("T001")
        out = ledger.record_from("https://x.test/", dec(Q.IS_REACHABLE, None))
        assert out.branch == "unresolved"
        out2 = ledger.record_from("https://y.test/", dec(Q.IS_REACHABLE, False))
        assert out2.branch == "login_wall_or_blocked"


class TestSerialization:
    def test_成功项_标签_不显示为失败(self):
        ledger = RunLedger("T001")
        out = ledger.record("https://x.test/", None, "有播放组件", reached=True)
        j = out.to_json()
        assert j["branch"] is None
        assert j["branch_label"] == "成功"
        assert j["reached_play_page"] is True

    def test_决策来源透传(self):
        ledger = RunLedger("T001")
        out = ledger.record_from("https://x.test/",
                                 dec(Q.PLAYER_OK, True, fallback_used=True))
        assert out.source == "rule"
        assert out.fallback_used is True


class TestObservationWarning:
    def test_降级与截断都提示(self):
        from trajectory_pipeline.executor.branches import (
            validate_observation_for_decision,
        )
        obs = Observation(url="u", page_title="t", body_text="b",
                          degraded=("browser_links",), truncated=True)
        msg = validate_observation_for_decision(obs, Q.SELECT_PLAY_SITES)
        assert "降级" in msg and "截断" in msg

    def test_正常观察无提示(self):
        from trajectory_pipeline.executor.branches import (
            validate_observation_for_decision,
        )
        obs = Observation(url="u", page_title="t", body_text="b")
        assert validate_observation_for_decision(obs, Q.SELECT_PLAY_SITES) == ""


def test_non_sample_branches_确实不在注册表的负分支里():
    """NON_SAMPLE_BRANCHES 必须是「无题的兜底分支」，不能是某道题的负分支——
    否则它既会被 question 判出、又不算负样本，语义打架。"""
    q_branches = {q.negative_branch for q in questions.REGISTRY.values()}
    assert not (NON_SAMPLE_BRANCHES & q_branches)


class TestPlayerUnverified:
    """环节⑦ 的 ``None``：控件点了、快照拿到了、播放页就在眼前，
    只是判不了「组件能不能用」。

    与 ``unresolved`` 的区别不是程度、是**责任方**：那个是「连播放页都没到」，
    这个是「到了但证据不足以判」。混在一支里，复核队列就分不出
    「请补一条判据」与「请看一眼这个页面」这两种待办。
    """

    def _dec(self, answer=None):
        return dec(Q.PLAYER_OK, answer, evidence="判不了能不能播")

    def test_落_player_unverified_而不是_unresolved(self):
        led = RunLedger("T001")
        out = led.record_unresolved_reached("https://x.test/p", self._dec())
        assert out.branch == "player_unverified"

    def test_是访问过的(self):
        """``reached`` 必须为 True——这条恰恰是「到了却没结论」，
        报成「没到」会把最有价值的复核样本（页面就在存档里）说没了。"""
        led = RunLedger("T001")
        assert led.record_unresolved_reached(
            "https://x.test/p", self._dec()).reached is True

    def test_不进负样本池(self):
        """同 I4：判不了 ≠ 失败。这条进池就是在说「这里看不了」，
        而我们明明正站在播放页上。"""
        led = RunLedger("T001")
        led.record_unresolved_reached("https://x.test/p", self._dec())
        assert led.negative_samples() == []
        assert led.summary()["negative_samples"] == 0

    def test_三支没判出来的成因分开统计(self):
        """报表按分支定位该修什么。合成一支后，
        「感知层该加能力」与「播放页证据不足」会长得一模一样。"""
        assert {"unresolved", "player_unverified", "trailer_suspect"} \
            <= NON_SAMPLE_BRANCHES
        led = RunLedger("T001")
        led.record("https://a.test/", "unresolved", "e")
        led.record_unresolved_reached("https://b.test/p", self._dec())
        assert led.summary()["by_branch"] == \
            {"unresolved": 1, "player_unverified": 1}

    def test_拒收非None(self):
        """传了 True/False 进来会记错分支，而调用方拼错应该炸。"""
        led = RunLedger("T001")
        for answer in (True, False):
            with pytest.raises(ValueError, match="只接受 answer=None"):
                led.record_unresolved_reached("https://x.test/p", self._dec(answer))

    def test_拒收时不留半条记录(self):
        """抛错却已追加了一条 outcome 的话，批次报表会多出一条来路不明的记录，
        而谁也不知道它是怎么来的。"""
        led = RunLedger("T001")
        with pytest.raises(ValueError):
            led.record_unresolved_reached("https://x.test/p", self._dec(True))
        assert led.outcomes == []

    def test_record_from_不会产出这一支(self):
        """反向护栏：环节①–④ 的入口。环节⑦ 若误接它，None 会退回
        unresolved——分类退化但不炸，属于最该被测试盯住的那类改动。"""
        led = RunLedger("T001")
        assert led.record_from("https://x.test/p", self._dec()).branch \
            == "unresolved"


class TestOutcomeUrls:
    """D-6 要「站点 URL / 播放页 URL」两条必填，前提是账本分得清三层地址。"""

    def test_三个字段各自落盘(self):
        led = RunLedger("T001")
        out = led.record_unresolved_reached(
            "https://x.test/play/e5", dec(Q.PLAYER_OK, None),
            site_url="http://x.test/movie", play_page_url="https://x.test/play/e5",
        )
        j = out.to_json()
        assert j["site_url"] == "http://x.test/movie"
        assert j["play_page_url"] == "https://x.test/play/e5"
        assert j["url"] == "https://x.test/play/e5"

    def test_跳转过的站两个地址不同且都留着(self):
        """实测存档里 4 个 run 的候选 url 是 http、outcome url 是 https。
        只留一个字段的话，丢的那个就再也无法从存档恢复了。"""
        led = RunLedger("T001")
        out = led.record_from("https://x.test/play/e5", dec(Q.PLAYER_OK, False),
                              site_url="http://x.test/movie",
                              play_page_url="https://x.test/play/e5")
        assert out.site_url != out.url

    def test_没到播放页时播放页地址为空(self):
        """空是**正确**的值：按 D-6 口径「不适用、不扣分」。
        编一个出来才是把猜测写成事实。"""
        led = RunLedger("T001")
        assert led.record("https://x.test/m", "no_play_control", "e").play_page_url == ""


class TestBranchPrecondition:
    """负分支的**前置事实**——贴标签之前页面上必须真有那个东西。

    实测（2026-10-09 www.mgtvtv.com/tv/32080/）：点击前后两次观察完全
    相同（390 字正文 / 37 元素 / video=0），说明点击没把页面带进播放页；
    模型判 False 的原话是「仍显示影片详情和『立即播放』按钮，并非实际
    播放页」。映射只看「模型说了 False」，于是贴上 ``component_unverified``
    ——一条**货真价实的负样本**，说的是「这个站看不了《功夫》」。
    而它有、有播放按钮，只是我们没点进去。
    """

    def _dec(self, answer):
        from trajectory_pipeline.perception.base import Decision

        return Decision(question=Q.PLAYER_OK, answer=answer, confidence=0.8,
                        evidence="LLM 判 False：并非实际播放页",
                        source="llm")

    def _obs(self, **kw):
        from trajectory_pipeline.perception.base import Observation

        base = dict(url="https://x.test/p", page_title="功夫 - 某站",
                    body_text="立即播放")
        base.update(kw)
        return Observation(**base)

    def test_无媒体标签时降级为unresolved(self):
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from("https://x.test/p", self._dec(False),
                              obs=self._obs(video_tag_count=0))
        assert out.branch == "unresolved", "没有 media 就不该贴 component_unverified"
        assert "前置事实不成立" in out.evidence

    def test_原始证据保留(self):
        """降级不是改写结论——模型的原话必须留在证据里，
        否则人工复核看不到「它当时说了什么」，而降级理由会变成唯一线索。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from("https://x.test/p", self._dec(False),
                              obs=self._obs(video_tag_count=0))
        assert "并非实际播放页" in out.evidence

    def test_有媒体标签时照旧贴分支(self):
        """反例护栏：守卫不是封死这条路。页面真的有 <video> 而播放器没起来，
        那正是 ``component_unverified`` 的定义。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from("https://x.test/p", self._dec(False),
                              obs=self._obs(video_tag_count=1))
        assert out.branch == "component_unverified"

    def test_没传观察时照旧(self):
        """老调用方不传 obs 时不能被打成 unresolved——
        那是「守卫反噬」，比不设守卫更糟。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from("https://x.test/p", self._dec(False))
        assert out.branch == "component_unverified"

    def test_answer为None不受影响(self):
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from("https://x.test/p", self._dec(None),
                              obs=self._obs(video_tag_count=0))
        assert out.branch == "unresolved"

    def test_只有一条分支有前置事实(self):
        """这张表要短。长得跟分支表一样长就没人维护了——
        一旦要求给每条分支都填前置事实，没人填的会变成空串，
        而空串与「不需要前置事实」长得一模一样。"""
        from trajectory_pipeline.executor.branches import BRANCH_PRECONDITION

        assert set(BRANCH_PRECONDITION) == {"component_unverified"}
