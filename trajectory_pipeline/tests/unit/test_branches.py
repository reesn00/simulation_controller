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
        assert len(ledger.outcomes) == 8

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
        assert len(s["missing_branches"]) == 7

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