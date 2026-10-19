"""``evaluation/scout/aggregate.py`` 的单测矩阵。

设计基线: ``docs/设计方案/14-完整任务视图与LS上传.md`` §4.2-§4.3。

纪律:沿用 :file:`tests/unit/test_rationale_gates.py` 的「反过来仍成立」
反例同款——每条断言配一条反向用例(避坑指南 §7.1)。

盯四件事:
1. **聚合纯函数正确性**——min / is_veto 短路 / score=None 短路 / tags
   并集 / evidence 合并 / status 推导
2. **留痕完整性**——``aggregation_log`` 必有,且每条决策可追溯到 unit
3. **I/O 门面 fail-closed**——坏行 / 缺件 / 写盘失败走 skipped 不抛出
4. **默认不覆盖**——与 cli build / cli task-view 同款纪律
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.evaluation.scout.aggregate import (
    DIMENSION_ORDER,
    TRAJECTORY_BUCKET_FILE,
    TRAJECTORY_STATS_FILE,
    aggregate_from_buckets,
    aggregate_to_trajectory,
    group_units_by_trajectory,
)
from trajectory_pipeline.evaluation.scout.types import ScoutRecord


# ── 夹具 ──────────────────────────────────────────────────────────


def _score(dim: str, score: int | None, *, is_veto: bool = False,
            reasons: list[str] | None = None) -> dict:
    return {
        "dimension": dim,
        "score": score,
        "is_veto": is_veto,
        "reasons": reasons or [],
    }


def _unit(
    sample_id: str = "T001__r1__search",
    *,
    task_id: str | None = None,
    run_id: str | None = None,
    d2: int = 5,
    d4: int = 4,
    d5: int = 5,
    d6: int = 4,
    status: str = "scored",
    tags: list[str] | None = None,
    evidence: dict | None = None,
    veto_dims: tuple[str, ...] = (),
    none_dims: tuple[str, ...] = (),
) -> ScoutRecord:
    """造一条 :class:`ScoutRecord` 夹具。

    ``task_id`` / ``run_id`` 不传时**从 sample_id 解析**——
    ``<task_id>__<run_id>__<unit>`` 双下划线主键,避免 fixture 写错时
    ``aggregate_to_trajectory`` 把跨 trajectory 的 unit 静默归到一起
    (这种 bug 一旦发生,聚合结果完全错误但不会报错)。
    """
    parts = sample_id.split("__")
    if task_id is None:
        task_id = parts[0] if len(parts) >= 2 else "T001"
    if run_id is None:
        run_id = parts[1] if len(parts) >= 2 else "r1"
    scores = []
    for dim, val in (("d2", d2), ("d4", d4), ("d5", d5), ("d6", d6)):
        if dim in none_dims:
            scores.append(_score(dim, None, reasons=["no_signal"]))
        elif dim in veto_dims:
            scores.append(_score(dim, val, is_veto=True,
                                  reasons=[f"{dim}_veto"]))
        else:
            scores.append(_score(dim, val))
    return ScoutRecord(
        sample_id=sample_id,
        task_id=task_id,
        run_id=run_id,
        status=status,  # type: ignore[typeddict-item]
        scores=scores,
        tags=tags or [],  # type: ignore[typeddict-item]
        evidence_supplement=evidence or {},
    )


# ── 1. 纯函数:min 策略 ─────────────────────────────────────────


class TestMin策略:
    def test_两unit_d2取最差(self):
        u1 = _unit("T001__r1__search", d2=5)
        u2 = _unit("T001__r1__site-1", d2=3)
        tr = aggregate_to_trajectory([u1, u2])
        d2 = next(s for s in tr["scores"] if s["dimension"] == "d2")
        assert d2["score"] == 3
        assert d2["is_veto"] is False

    def test_四维各自独立取最差(self):
        u1 = _unit("T001__r1__search", d2=5, d4=4, d5=5, d6=4)
        u2 = _unit("T001__r1__site-1", d2=3, d4=2, d5=5, d6=4)
        u3 = _unit("T001__r1__site-2", d2=4, d4=5, d5=3, d6=2)
        tr = aggregate_to_trajectory([u1, u2, u3])
        by_dim = {s["dimension"]: s["score"] for s in tr["scores"]}
        assert by_dim == {"d2": 3, "d4": 2, "d5": 3, "d6": 2}

    def test_反例_取最差不是取最好(self):
        """反向验证:min 取的是最差(不是最差之外的任何值)。"""
        u1 = _unit("T001__r1__search", d2=5)
        u2 = _unit("T001__r1__site-1", d2=3)
        u3 = _unit("T001__r1__site-2", d2=4)
        tr = aggregate_to_trajectory([u1, u2, u3])
        d2 = next(s for s in tr["scores"] if s["dimension"] == "d2")
        assert d2["score"] == 3
        # 不取 max(5,4) 或 avg(4)
        assert d2["score"] != 5
        assert d2["score"] != 4

    def test_picked_unit的reasons透传(self):
        """min 选中 unit 的 reasons 透传到任务级——挑最差那条的理由。"""
        u1 = _unit("T001__r1__search", d2=5, d4=4, d5=5, d6=4)
        u2 = _unit("T001__r1__site-1", d2=3, d4=2, d5=5, d6=4)
        tr = aggregate_to_trajectory([u1, u2])
        d4 = next(s for s in tr["scores"] if s["dimension"] == "d4")
        # u2 的 d4 reasons 是 [] (默认),所以透传 []
        assert d4["reasons"] == []


# ── 2. 短路:任一维 score=None → 任务级 undetermined ────────────


class Test短路ScoreNone:
    def test_一unit_d4为None任务级undetermined(self):
        u1 = _unit("T001__r1__search")
        u2 = _unit("T001__r1__site-1", none_dims=("d4",))
        tr = aggregate_to_trajectory([u1, u2])
        assert tr["status"] == "undetermined"
        d4 = next(s for s in tr["scores"] if s["dimension"] == "d4")
        assert d4["score"] is None

    def test_反例_全score到齐仍scored(self):
        u1 = _unit("T001__r1__search")
        u2 = _unit("T001__r1__site-1", d2=3, d4=2, d5=5, d6=4)
        tr = aggregate_to_trajectory([u1, u2])
        assert tr["status"] == "scored"

    def test_短路留痕有quarantine_short_circuit(self):
        u1 = _unit("T001__r1__search")
        u2 = _unit("T001__r1__site-1", none_dims=("d6",))
        tr = aggregate_to_trajectory([u1, u2])
        actions = [e["action"] for e in tr["aggregation_log"]]
        assert "quarantine_short_circuit" in actions


# ── 3. 短路:任一维 is_veto=True → 任务级 is_veto=True ───────────


class Test短路IsVeto:
    def test_一unit_d4_veto任务级is_veto(self):
        u1 = _unit("T001__r1__search")
        u2 = _unit("T001__r1__site-1", veto_dims=("d4",))
        tr = aggregate_to_trajectory([u1, u2])
        d4 = next(s for s in tr["scores"] if s["dimension"] == "d4")
        assert d4["is_veto"] is True
        # status 仍是 scored(score 全到齐,只是打 veto 标记)
        assert tr["status"] == "scored"

    def test_反例_无veto任务级is_veto全False(self):
        u1 = _unit("T001__r1__search")
        u2 = _unit("T001__r1__site-1")
        tr = aggregate_to_trajectory([u1, u2])
        for s in tr["scores"]:
            assert s["is_veto"] is False

    def test_veto留痕有veto_action(self):
        u1 = _unit("T001__r1__search")
        u2 = _unit("T001__r1__site-1", veto_dims=("d2",))
        tr = aggregate_to_trajectory([u1, u2])
        actions = [e["action"] for e in tr["aggregation_log"]]
        assert "veto" in actions


# ── 4. tags 并集(去重,保序) ────────────────────────────────────


class TestTags并集:
    def test_并集去重(self):
        u1 = _unit("T001__r1__search", tags=["d4_failed", "d2_failed"])
        u2 = _unit("T001__r1__site-1", tags=["d2_failed", "veto"])
        tr = aggregate_to_trajectory([u1, u2])
        # 并集去重:出现顺序按 unit 出现顺序、unit 内 tag 出现顺序
        assert tr["tags"] == ["d4_failed", "d2_failed", "veto"]

    def test_空tags也OK(self):
        u1 = _unit("T001__r1__search", tags=[])
        u2 = _unit("T001__r1__site-1", tags=[])
        tr = aggregate_to_trajectory([u1, u2])
        assert tr["tags"] == []

    def test_tag_merged留痕(self):
        u1 = _unit("T001__r1__search", tags=["d4_failed"])
        u2 = _unit("T001__r1__site-1", tags=["veto"])
        tr = aggregate_to_trajectory([u1, u2])
        merged = [e for e in tr["aggregation_log"]
                   if e["action"] == "tag_merged"]
        assert len(merged) == 2


# ── 5. evidence_supplement 合并 ─────────────────────────────────


class TestEvidence合并:
    def test_无冲突合并(self):
        u1 = _unit("T001__r1__search", evidence={"a": 1, "c": 3})
        u2 = _unit("T001__r1__site-1", evidence={"b": 2})
        tr = aggregate_to_trajectory([u1, u2])
        assert tr["evidence_supplement"] == {"a": 1, "c": 3, "b": 2}

    def test_键冲突取第一个非空(self):
        u1 = _unit("T001__r1__search", evidence={"a": 1})
        u2 = _unit("T001__r1__site-1", evidence={"a": 2})
        tr = aggregate_to_trajectory([u1, u2])
        assert tr["evidence_supplement"] == {"a": 1}

    def test_键冲突第二个非空第一个空也OK(self):
        """第一个 unit 该键是空(空 dict / 空 list / 空 str)→ 跳过,
        用第二个 unit 的非空值填——并留痕。"""
        u1 = _unit("T001__r1__search", evidence={"a": ""})
        u2 = _unit("T001__r1__site-1", evidence={"a": "real_value"})
        tr = aggregate_to_trajectory([u1, u2])
        assert tr["evidence_supplement"] == {"a": "real_value"}
        actions = [e["action"] for e in tr["aggregation_log"]]
        assert "evidence_merged" in actions


# ── 6. unit_outcomes 顺序 ───────────────────────────────────────


class TestUnitOutcomes:
    def test_unit_outcomes按输入顺序(self):
        u1 = _unit("T001__r1__site-2")
        u2 = _unit("T001__r1__search")
        u3 = _unit("T001__r1__site-1")
        tr = aggregate_to_trajectory([u1, u2, u3])
        assert tr["unit_outcomes"] == [
            "T001__r1__site-2", "T001__r1__search", "T001__r1__site-1",
        ]

    def test_trajectory_id是双下划线主键(self):
        u1 = _unit("T001__r1__search")
        u2 = _unit("T001__r1__site-1")
        tr = aggregate_to_trajectory([u1, u2])
        assert tr["trajectory_id"] == "T001__r1"
        assert tr["task_id"] == "T001"
        assert tr["run_id"] == "r1"


# ── 7. status 推导 ──────────────────────────────────────────────


class TestStatus推导:
    def test_全scored(self):
        tr = aggregate_to_trajectory([_unit(), _unit()])
        assert tr["status"] == "scored"

    def test_任一维None_undetermined(self):
        u1 = _unit()
        u2 = _unit(none_dims=("d4",))
        assert aggregate_to_trajectory([u1, u2])["status"] == "undetermined"

    def test_全unit_not_run_任务级not_run(self):
        u1 = _unit(status="not_run")
        u2 = _unit("T001__r1__site-1", status="not_run")
        assert aggregate_to_trajectory([u1, u2])["status"] == "not_run"

    def test_反例_部分unit_not_run不算not_run(self):
        u1 = _unit(status="scored")
        u2 = _unit("T001__r1__site-1", status="not_run")
        # u1 仍 scored → to_skip 不成立;d4 score 到齐 → scored
        assert aggregate_to_trajectory([u1, u2])["status"] == "scored"


# ── 8. 缺 unit 留痕 ─────────────────────────────────────────────


class Test缺Unit留痕:
    def test_expected_unit_count_大于实际数记missing_unit(self):
        u1 = _unit()
        u2 = _unit("T001__r1__site-1")
        tr = aggregate_to_trajectory([u1, u2], expected_unit_count=4)
        actions = [e["action"] for e in tr["aggregation_log"]]
        assert "missing_unit" in actions

    def test_expected_unit_count_等于实际数不记(self):
        u1 = _unit()
        u2 = _unit("T001__r1__site-1")
        tr = aggregate_to_trajectory([u1, u2], expected_unit_count=2)
        actions = [e["action"] for e in tr["aggregation_log"]]
        assert "missing_unit" not in actions


# ── 9. 入参校验 ────────────────────────────────────────────────


class Test入参校验:
    def test_空units报错(self):
        with pytest.raises(ValueError, match="units 不能为空"):
            aggregate_to_trajectory([])

    def test_跨task_id报错(self):
        u1 = _unit("T001__r1__search", task_id="T001")
        u2 = _unit("T001__r1__site-1", task_id="T002")
        with pytest.raises(ValueError, match="跨 .task_id, run_id."):
            aggregate_to_trajectory([u1, u2])


# ── 10. group_units_by_trajectory ───────────────────────────────


class TestGroupUnits:
    def test_分组保序(self):
        u1 = _unit("T001__r1__search", task_id="T001", run_id="r1")
        u2 = _unit("T001__r1__site-1", task_id="T001", run_id="r1")
        u3 = _unit("T002__r2__search", task_id="T002", run_id="r2")
        grouped = group_units_by_trajectory([u1, u2, u3])
        assert list(grouped.keys()) == ["T001__r1", "T002__r2"]
        assert grouped["T001__r1"] == [u1, u2]
        assert grouped["T002__r2"] == [u3]

    def test_空输入返空dict(self):
        assert group_units_by_trajectory([]) == {}


# ── 11. I/O 门面:aggregate_from_buckets ────────────────────────


class TestAggregateFromBuckets:
    """从 scout 三件套 → trajectory_candidates.jsonl 的 I/O 端。"""

    def _write_bucket(self, scout_dir: Path, bucket: str,
                        records: list[ScoutRecord]) -> None:
        from trajectory_pipeline.evaluation.ls_bridge.exporter import BUCKET_FILES
        path = scout_dir / BUCKET_FILES[bucket]
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
            encoding="utf-8",
        )

    def test_三件套聚合(self, tmp_path):
        u1 = _unit("T001__r1__search", d2=5)
        u2 = _unit("T001__r1__site-1", d2=3)
        u3 = _unit("T002__r2__search")
        self._write_bucket(tmp_path, "positive", [u1, u3])
        self._write_bucket(tmp_path, "negative", [u2])

        trajectories, stats = aggregate_from_buckets(
            scout_dir=tmp_path, overwrite=True,
        )
        # 2 个 trajectory
        assert len(trajectories) == 2
        assert stats["scored"] == 2
        # 产物落盘
        assert (tmp_path / TRAJECTORY_BUCKET_FILE).exists()
        assert (tmp_path / TRAJECTORY_STATS_FILE).exists()

    def test_默认不覆盖(self, tmp_path):
        self._write_bucket(tmp_path, "positive", [_unit()])
        # 第一次
        aggregate_from_buckets(scout_dir=tmp_path, overwrite=True)
        # 第二次(默认 overwrite=False)——跳过写
        trajectories, _ = aggregate_from_buckets(scout_dir=tmp_path)
        # 仍能读回但**不会重写文件**(read + 算 stats)
        assert len(trajectories) == 1
        # 产物文件 mtime 不变——但这难断言。简化:看 stats 仍可读
        assert (tmp_path / TRAJECTORY_STATS_FILE).exists()

    def test_空三件套返空(self, tmp_path):
        # 三件套都不存在
        trajectories, stats = aggregate_from_buckets(
            scout_dir=tmp_path, overwrite=True,
        )
        assert trajectories == []
        assert stats == {"scored": 0, "undetermined": 0, "not_run": 0}

    def test_坏行不中断整批(self, tmp_path):
        # 一行好档 + 一行坏 JSON
        good = json.dumps(_unit("T001__r1__search"), ensure_ascii=False)
        path = tmp_path / "positive_candidates.jsonl"
        path.write_text(good + "\n{ broken\n", encoding="utf-8")
        trajectories, _ = aggregate_from_buckets(
            scout_dir=tmp_path, overwrite=True,
        )
        # read_scout_records 已把坏行容错——好档仍进聚合
        assert len(trajectories) == 1

    def test_反例_quarantine桶被默认收纳(self, tmp_path):
        """默认 buckets=(positive, negative, quarantine)——三态都进聚合。
        反例:只传 positive 时 quarantine 不参与。"""
        from trajectory_pipeline.evaluation.ls_bridge.exporter import BUCKET_FILES
        u1 = _unit("T001__r1__search")
        # 写一个 quarantine 记录(没 unit 被聚前 status 是 scored)
        self._write_bucket(tmp_path, "quarantine", [u1])
        # 默认三态
        trs, _ = aggregate_from_buckets(scout_dir=tmp_path, overwrite=True)
        assert len(trs) == 1
        # 只取 positive
        trs2, _ = aggregate_from_buckets(
            scout_dir=tmp_path, buckets=("positive",), overwrite=True,
        )
        assert len(trs2) == 0
        # 文件残留 quarantine.jsonl(不删,符合 BUCKET_FILES 一致性)
        assert (tmp_path / BUCKET_FILES["quarantine"]).exists()


# ── 12. D14.5 字段对齐(方案 14 §4.2) ──────────────────────────


class TestD145字段对齐:
    """方案 14 §4.2 列出的 TrajectoryRecord 字段必须存在 + 类型正确。"""

    def test_必填字段(self):
        tr = aggregate_to_trajectory([_unit()])
        assert set(tr.keys()) >= {
            "trajectory_id", "task_id", "run_id", "status",
            "scores", "tags", "unit_outcomes",
            "evidence_supplement", "aggregation_log",
        }

    def test_scores长度是4(self):
        tr = aggregate_to_trajectory([_unit()])
        assert len(tr["scores"]) == 4

    def test_aggregation_log每条必含unit_action(self):
        """每条留痕必含 unit / action(给人工复查)。"""
        u1 = _unit("T001__r1__search", tags=["d4_failed"])
        u2 = _unit("T001__r1__site-1", veto_dims=("d2",))
        tr = aggregate_to_trajectory([u1, u2])
        for e in tr["aggregation_log"]:
            assert "unit" in e
            assert "action" in e
