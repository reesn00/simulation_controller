"""scout 三态分流纯函数的单测矩阵。

覆盖 :func:`trajectory_pipeline.evaluation.scout.policy.classify` 与
:func:`trajectory_pipeline.evaluation.scout.policy.tags_for` 的全部边界,
每条都配一条「反过来仍成立」的反例(沿用 tests/unit/test_rationale_gates.py
的纪律,见避坑指南 §7.1)。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.evaluation.scout.policy import (
    NEGATIVE_SCORE_THRESHOLD,
    classify,
    tags_for,
)
from trajectory_pipeline.evaluation.scout.types import (
    DimensionScore,
    ScoutRecord,
)


# ── 夹具 ──────────────────────────────────────────────────────────


def _score(dim: str, score: int | None, *, is_veto: bool = False) -> DimensionScore:
    return {
        "dimension": dim,  # type: ignore[typeddict-item]
        "score": score,
        "is_veto": is_veto,
        "reasons": [],
    }


def _rec(
    *,
    status: str = "scored",
    scores: list[DimensionScore] | None = None,
    tags: list[str] | None = None,
) -> ScoutRecord:
    if scores is None:
        scores = [
            _score("d2", 4),
            _score("d4", 4),
            _score("d5", 4),
            _score("d6", 4),
        ]
    return {
        "sample_id": "s-1",
        "task_id": "t-1",
        "run_id": "r-1",
        "status": status,  # type: ignore[typeddict-item]
        "scores": scores,
        "tags": tags or [],
        "evidence_supplement": {},
    }


# ── 不变量:阈值常量 ──────────────────────────────────────────────


class TestThreshold:
    def test_阈值锁定2(self) -> None:
        """负例阈值为 2。改这个数 = 改业务语义,需要在 commit msg 里点名。"""
        assert NEGATIVE_SCORE_THRESHOLD == 2


# ── classify:全 4 维判分 ─────────────────────────────────────────


class TestClassifyScored:
    def test_全高分_positive(self) -> None:
        assert classify(_rec()) == "positive"

    def test_全4_positive(self) -> None:
        # 4 是「通过」分;锁定这个边界
        rec = _rec(scores=[_score(d, 4) for d in ("d2", "d4", "d5", "d6")])
        assert classify(rec) == "positive"

    def test_全3_positive(self) -> None:
        # 3 是阈值上界;锁定这个边界
        rec = _rec(scores=[_score(d, 3) for d in ("d2", "d4", "d5", "d6")])
        assert classify(rec) == "positive"

    def test_单维3_其它4_positive(self) -> None:
        rec = _rec(scores=[
            _score("d2", 3),
            _score("d4", 4),
            _score("d5", 4),
            _score("d6", 4),
        ])
        assert classify(rec) == "positive"


# ── classify:负例 ───────────────────────────────────────────────


class TestClassifyNegative:
    def test_单维等于阈值_negative(self) -> None:
        rec = _rec(scores=[
            _score("d2", NEGATIVE_SCORE_THRESHOLD),  # = 2
            _score("d4", 4),
            _score("d5", 4),
            _score("d6", 4),
        ])
        assert classify(rec) == "negative"

    def test_单维0_negative(self) -> None:
        """score=0 是「判了零分」,等同 ≤ 2,走 negative(而不是 quarantine)。"""
        rec = _rec(scores=[
            _score("d2", 0),
            _score("d4", 4),
            _score("d5", 4),
            _score("d6", 4),
        ])
        assert classify(rec) == "negative"

    def test_单维1_negative(self) -> None:
        rec = _rec(scores=[
            _score("d2", 4),
            _score("d4", 1),
            _score("d5", 4),
            _score("d6", 4),
        ])
        assert classify(rec) == "negative"

    def test_多维都低_negative(self) -> None:
        rec = _rec(scores=[
            _score("d2", 2),
            _score("d4", 1),
            _score("d5", 4),
            _score("d6", 4),
        ])
        assert classify(rec) == "negative"


# ── classify:veto 维 ──────────────────────────────────────────────


class TestClassifyVeto:
    def test_单维is_veto_negative(self) -> None:
        """veto 是一票否决,即使 score=4 也走 negative。"""
        rec = _rec(scores=[
            _score("d2", 4),
            _score("d4", 4),
            _score("d5", 4, is_veto=True),
            _score("d6", 4),
        ])
        assert classify(rec) == "negative"

    def test_veto配低分也是negative(self) -> None:
        rec = _rec(scores=[
            _score("d2", 2),
            _score("d4", 4),
            _score("d5", 4, is_veto=True),
            _score("d6", 4),
        ])
        assert classify(rec) == "negative"

    def test_veto配None也是negative(self) -> None:
        """is_veto=True 时 score 字段无意义(可能没判出来);只看 veto 即可。"""
        rec = _rec(scores=[
            _score("d2", 4),
            _score("d4", 4),
            _score("d5", None, is_veto=True),
            _score("d6", 4),
        ])
        assert classify(rec) == "negative"


# ── classify:quarantine ──────────────────────────────────────────


class TestClassifyQuarantine:
    def test_单维None_quarantine(self) -> None:
        """score=None 是「没判」,走 quarantine 而非 negative。"""
        rec = _rec(scores=[
            _score("d2", None),
            _score("d4", 4),
            _score("d5", 4),
            _score("d6", 4),
        ])
        assert classify(rec) == "quarantine"

    def test_全部None_quarantine(self) -> None:
        rec = _rec(scores=[_score(d, None) for d in ("d2", "d4", "d5", "d6")])
        assert classify(rec) == "quarantine"

    def test_status_undetermined_quarantine(self) -> None:
        """scout 上游已统一把「任一维 None」标 undetermined,该路径也要兜底。"""
        rec = _rec(status="undetermined", scores=[_score(d, 4) for d in ("d2", "d4", "d5", "d6")])
        assert classify(rec) == "quarantine"

    def test_status_scored_但有None_兜底quarantine(self) -> None:
        """scored + 部分 None 是上游 bug,classify 兜底走 quarantine。"""
        rec = _rec(status="scored", scores=[
            _score("d2", 4),
            _score("d4", None),
            _score("d5", 4),
            _score("d6", 4),
        ])
        assert classify(rec) == "quarantine"


# ── classify:skip ────────────────────────────────────────────────


class TestClassifySkip:
    def test_status_not_run_skip(self) -> None:
        rec = _rec(status="not_run")
        assert classify(rec) == "skip"

    def test_not_run_即使有低分也skip(self) -> None:
        """not_run 是主动放弃,不参评;不会因为低分切到 negative。"""
        rec = _rec(
            status="not_run",
            scores=[_score(d, 0) for d in ("d2", "d4", "d5", "d6")],
        )
        assert classify(rec) == "skip"


# ── tags_for:排序 ────────────────────────────────────────────────


class TestTagsForOrdering:
    def test_单维低分_tag只含该维(self) -> None:
        rec = _rec(scores=[
            _score("d2", 2),
            _score("d4", 4),
            _score("d5", 4),
            _score("d6", 4),
        ])
        assert tags_for(rec) == ("d2_failed",)

    def test_多维低分_tag按dim升序(self) -> None:
        rec = _rec(scores=[
            _score("d2", 4),
            _score("d4", 2),
            _score("d5", 1),
            _score("d6", 4),
        ])
        assert tags_for(rec) == ("d4_failed", "d5_failed")

    def test_veto配全高分_tag只含veto(self) -> None:
        rec = _rec(scores=[
            _score("d2", 4),
            _score("d4", 4),
            _score("d5", 4, is_veto=True),
            _score("d6", 4),
        ])
        assert tags_for(rec) == ("veto",)

    def test_veto配低分_veto在最后(self) -> None:
        """veto 语义最重,放在 dim_failed 之后;不变量 9。"""
        rec = _rec(scores=[
            _score("d2", 4),
            _score("d4", 2),
            _score("d5", 4, is_veto=True),
            _score("d6", 1),
        ])
        assert tags_for(rec) == ("d4_failed", "d6_failed", "veto")

    def test_全高分_tag为空(self) -> None:
        assert tags_for(_rec()) == ()

    def test_None维不产生tag(self) -> None:
        """score=None 不算负向 —— 它走 quarantine,不该再被 dim_failed 标记。"""
        rec = _rec(scores=[
            _score("d2", 4),
            _score("d4", None),
            _score("d5", 4),
            _score("d6", 4),
        ])
        assert tags_for(rec) == ()


# ── 不变量交叉检验 ──────────────────────────────────────────────


class TestInvariants:
    def test_positive与negative互斥(self) -> None:
        """classify 的输出在「正常维度」下不会同时满足两条 tag 路径。"""
        for status in ("scored",):
            for d2 in range(6):
                for d4 in range(6):
                    rec = _rec(status=status, scores=[
                        _score("d2", d2),
                        _score("d4", d4),
                        _score("d5", 4),
                        _score("d6", 4),
                    ])
                    cls = classify(rec)
                    assert cls in ("positive", "negative", "quarantine")
                    if cls == "positive":
                        # positive 时 d2/d4 都该 ≥ 3
                        assert d2 >= 3 and d4 >= 3, (d2, d4)

    def test_阈值_2_等于2时判负(self) -> None:
        """≤ 2 走负 —— 不变量 8 的反向:不能因为「凑巧等于阈值」就放过去。"""
        rec = _rec(scores=[
            _score("d2", 2),
            _score("d4", 4),
            _score("d5", 4),
            _score("d6", 4),
        ])
        assert classify(rec) == "negative"

    def test_0_与_None_走不同路径(self) -> None:
        """0 走负,None 走 quarantine —— 业务上等价「差」与「没测」,绝不混。"""
        rec_zero = _rec(scores=[
            _score("d2", 0),
            _score("d4", 4),
            _score("d5", 4),
            _score("d6", 4),
        ])
        rec_none = _rec(scores=[
            _score("d2", None),
            _score("d4", 4),
            _score("d5", 4),
            _score("d6", 4),
        ])
        assert classify(rec_zero) == "negative"
        assert classify(rec_none) == "quarantine"
        assert classify(rec_zero) != classify(rec_none)
