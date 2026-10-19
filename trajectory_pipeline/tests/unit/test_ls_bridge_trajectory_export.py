"""ls_bridge.exporter trajectory 主路径(Phase 4, D14.5)单测。

覆盖:

* :func:`_derive_trajectory_bucket` —— 三态桶派生
* :func:`read_trajectory_records` —— 读 trajectory_candidates.jsonl
* :func:`export_trajectory_one` —— T 视图读 + data 拼装 + 凭据扫描
* :func:`export_batch` —— 默认走 trajectory 路径;``legacy_unit_trajectory=True``
  退到旧 per-unit 路径
* :func:`_render_aggregation_log` / :func:`_render_trajectory_audit_text` ——

不触网,纯函数 + 临时文件。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_pipeline.evaluation.ls_bridge.exporter import (
    _derive_trajectory_bucket,
    _render_aggregation_log,
    _render_trajectory_audit_text,
    export_batch,
    export_trajectory_one,
    read_trajectory_records,
)
from trajectory_pipeline.evaluation.scout.types import (
    AggregationLogEntry,
    DimensionScore,
    ScoutStatus,
    TrajectoryRecord,
)


# ── fixtures ───────────────────────────────────────────────────────


def _dim(dim: str, score: int | None, *, veto: bool = False, reasons=None) -> DimensionScore:
    return {
        "dimension": dim,  # type: ignore[typeddict-item]
        "score": score,
        "is_veto": veto,
        "reasons": list(reasons or []),
    }


def _trajectory(
    trajectory_id: str = "T001__r-1",
    *,
    task_id: str = "T001",
    run_id: str = "r-1",
    scores: list[DimensionScore] | None = None,
    tags: list[str] | None = None,
    status: ScoutStatus = "scored",
    unit_outcomes: list[str] | None = None,
    aggregation_log: list[AggregationLogEntry] | None = None,
    evidence_supplement: dict[str, Any] | None = None,
) -> TrajectoryRecord:
    # ``is None`` 区分「没传」与「传了空列表」(空 list 是合法:task 跑了但
    # 一个 unit 都没拿到——这正是 audit_text「不显示参与 unit」的边界 case)
    if unit_outcomes is None:
        unit_outcomes = ["T001__r-1__search", "T001__r-1__play"]
    return TrajectoryRecord(
        trajectory_id=trajectory_id,
        task_id=task_id,
        run_id=run_id,
        status=status,
        scores=scores if scores is not None else [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
        tags=list(tags or []),  # type: ignore[arg-type]
        unit_outcomes=list(unit_outcomes),
        evidence_supplement=dict(evidence_supplement or {}),
        aggregation_log=list(aggregation_log or []),
    )


@pytest.fixture()
def task_views_root(tmp_path: Path) -> Path:
    """落 T 视图(``<root>/task_views/<tid>.json``)——``task_views_dir`` 接管子目录。"""
    from trajectory_pipeline.storage import task_views_dir

    root = tmp_path
    tdir = task_views_dir(root)
    tdir.mkdir(parents=True, exist_ok=True)
    # 写一份 T001__r-1 的 T 视图
    (tdir / "T001__r-1.json").write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "播放这首歌"},
                {"role": "assistant", "content": "好的,正在播放。"},
            ],
            "openai": [
                {"role": "user", "content": "播放这首歌"},
                {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "play", "arguments": '{"ref": "btn-1"}'}}
                ]},
            ],
            "meta": {"gate": "pass", "step_count": 2, "task_id": "T001", "run_id": "r-1"},
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def trajectory_jsonl(tmp_path: Path) -> Path:
    """落 trajectory_candidates.jsonl(单 trajectory 一行)。"""
    path = tmp_path / "trajectory_candidates.jsonl"
    rec = _trajectory()
    path.write_text(
        json.dumps(rec, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return tmp_path  # 整个目录当 scout_dir


# ── TestDeriveTrajectoryBucket ────────────────────────────────────


class TestDeriveTrajectoryBucket:
    """桶派生:TrajectoryRecord → positive/negative/quarantine。"""

    def test_全高分_无tag_进positive(self):
        rec = _trajectory()
        assert _derive_trajectory_bucket(rec) == "positive"

    def test_任一低分_进negative(self):
        rec = _trajectory(scores=[_dim("d2", 2), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)])
        assert _derive_trajectory_bucket(rec) == "negative"

    def test_任一veto_进negative(self):
        rec = _trajectory(scores=[_dim("d2", 5), _dim("d4", 5), _dim("d5", 1, veto=True), _dim("d6", 5)])
        assert _derive_trajectory_bucket(rec) == "negative"

    def test_任一缺判_进quarantine(self):
        rec = _trajectory(
            scores=[_dim("d2", None), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
            status="undetermined",
        )
        assert _derive_trajectory_bucket(rec) == "quarantine"

    def test_not_run_走quarantine(self):
        rec = _trajectory(status="not_run")
        assert _derive_trajectory_bucket(rec) == "quarantine"

    def test_反例_高score但带tag仍positive(self):
        """tags 不直接驱动 bucket——驱动走 derive_overall.score/veto/缺判。"""
        rec = _trajectory(tags=["d4_failed"])
        # 没低分/veto/缺判,即便带 tag 也是 positive
        assert _derive_trajectory_bucket(rec) == "positive"


# ── TestReadTrajectoryRecords ─────────────────────────────────────


class TestReadTrajectoryRecords:
    def test_缺文件返空(self, tmp_path: Path):
        assert read_trajectory_records(tmp_path) == []

    def test_单行_读出派生桶(self, trajectory_jsonl: Path):
        out = read_trajectory_records(trajectory_jsonl)
        assert len(out) == 1
        bucket, rec = out[0]
        assert bucket == "positive"
        assert rec["trajectory_id"] == "T001__r-1"

    def test_多trajectory_全读(self, tmp_path: Path):
        path = tmp_path / "trajectory_candidates.jsonl"
        rec1 = _trajectory(trajectory_id="T001__r-1")
        rec2 = _trajectory(
            trajectory_id="T002__r-1",
            task_id="T002",
            scores=[_dim("d2", 2), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
        )
        path.write_text(
            json.dumps(rec1, ensure_ascii=False) + "\n"
            + json.dumps(rec2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        out = read_trajectory_records(tmp_path)
        assert [b for b, _ in out] == ["positive", "negative"]

    def test_坏行跳过(self, tmp_path: Path):
        path = tmp_path / "trajectory_candidates.jsonl"
        path.write_text(
            "{this is not json}\n"
            + json.dumps(_trajectory(), ensure_ascii=False) + "\n"
            + json.dumps({"no_trajectory_id": True}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        out = read_trajectory_records(tmp_path)
        assert len(out) == 1
        assert out[0][0] == "positive"

    def test_空行跳过(self, tmp_path: Path):
        path = tmp_path / "trajectory_candidates.jsonl"
        path.write_text(
            "\n" + json.dumps(_trajectory(), ensure_ascii=False) + "\n\n",
            encoding="utf-8",
        )
        out = read_trajectory_records(tmp_path)
        assert len(out) == 1


# ── TestRenderAggregationLog ──────────────────────────────────────


class TestRenderAggregationLog:
    def test_空返占位(self):
        assert _render_aggregation_log([]) == "(无聚合留痕)"

    def test_多行格式(self):
        entries: list[AggregationLogEntry] = [
            {"unit": "T001__r-1__search", "action": "min_score", "reason": "d2 选最低分"},
            {"unit": "-", "action": "missing_unit", "reason": "期望 2 个 unit, 实际 1 个"},
        ]
        out = _render_aggregation_log(entries)
        assert "[T001__r-1__search] min_score: d2 选最低分" in out
        assert "[-] missing_unit: 期望 2 个 unit, 实际 1 个" in out

    def test_反例_缺字段不崩(self):
        out = _render_aggregation_log([
            {"unit": "u1", "action": "tag_merged"},  # 无 reason
        ])
        assert "u1" in out and "tag_merged" in out


# ── TestRenderTrajectoryAuditText ─────────────────────────────────


class TestRenderTrajectoryAuditText:
    def test_negative开场(self):
        rec = _trajectory(
            scores=[_dim("d2", 2), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
            tags=["d2_failed"],
        )
        text = _render_trajectory_audit_text(rec, "negative")
        assert "任务级" in text
        assert "劣质" in text
        assert "d2_failed" in text
        assert "参与 unit" in text

    def test_quarantine开场(self):
        rec = _trajectory(
            scores=[_dim("d2", None), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
            status="undetermined",
        )
        text = _render_trajectory_audit_text(rec, "quarantine")
        assert "无法判定" in text
        assert "d2: 缺判" in text

    def test_positive开场(self):
        text = _render_trajectory_audit_text(_trajectory(), "positive")
        assert "合格" in text

    def test_聚合留痕拼上(self):
        rec = _trajectory(
            aggregation_log=[
                {"unit": "T001__r-1__play", "action": "veto", "reason": "d5 一票否决"},
            ],
        )
        text = _render_trajectory_audit_text(rec, "negative")
        assert "聚合留痕" in text
        assert "T001__r-1__play" in text
        assert "veto" in text

    def test_反例_无unit_outcomes_不崩(self):
        rec = _trajectory(unit_outcomes=[])
        text = _render_trajectory_audit_text(rec, "positive")
        assert "参与 unit" not in text  # 没 unit 就别提


# ── TestExportTrajectoryOne ───────────────────────────────────────


class TestExportTrajectoryOne:
    def test_正常导出(self, task_views_root: Path):
        rec = _trajectory()
        task, prediction, hits = export_trajectory_one(
            rec, "positive", task_views_root=task_views_root,
        )
        assert hits == []
        assert task is not None
        assert prediction is not None  # scorecard enabled + score 到齐
        data = task["data"]
        assert data["sample_id"] == "T001__r-1"
        assert data["task_id"] == "T001"
        assert data["run_id"] == "r-1"
        assert data["status"] == "positive"
        assert "T001__r-1__search" in data["unit_outcomes"]
        assert "聚合留痕" in data["aggregation_log_text"] or "(无聚合留痕)" in data["aggregation_log_text"]
        # messages 视图来自 T 视图,不是 P3 单元视图
        assert "播放这首歌" in data["messages_text"]

    def test_T视图缺失返None(self, tmp_path: Path):
        rec = _trajectory()
        task, prediction, hits = export_trajectory_one(
            rec, "positive", task_views_root=tmp_path,
        )
        assert task is None and prediction is None and hits == []

    def test_反例_negative桶data_status也negative(self, task_views_root: Path):
        rec = _trajectory(
            scores=[_dim("d2", 2), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
            tags=["d2_failed"],
        )
        task, prediction, _hits = export_trajectory_one(
            rec, "negative", task_views_root=task_views_root,
        )
        assert task is not None
        assert task["data"]["status"] == "negative"
        assert "d2_failed" in task["data"]["tags"]

    def test_凭据扫描命中_拒推(self, task_views_root: Path):
        # 把凭据形态直接塞进 T 视图(绕开 build 阶段,模拟坏档)
        from trajectory_pipeline.storage import task_views_dir
        tdir = task_views_dir(task_views_root)
        (tdir / "T001__r-1.json").write_text(
            json.dumps({
                "messages": [{"role": "user", "content": "key is sk-" + "a" * 20}],
                "openai": [],
                "meta": {},
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        rec = _trajectory()
        task, prediction, hits = export_trajectory_one(
            rec, "positive", task_views_root=task_views_root,
        )
        assert task is None and prediction is None
        assert hits, "凭据扫描应当命中"

    def test_scorecard_disabled_不带prediction(self, task_views_root: Path):
        rec = _trajectory()
        task, prediction, _hits = export_trajectory_one(
            rec, "positive", task_views_root=task_views_root,
            scorecard_enabled=False,
        )
        assert task is not None
        assert prediction is None  # disabled → 不发 prediction

    def test_反例_quarantine桶_audit_text含缺判(self, task_views_root: Path):
        rec = _trajectory(
            scores=[_dim("d2", None), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
            status="undetermined",
        )
        task, _, _ = export_trajectory_one(
            rec, "quarantine", task_views_root=task_views_root,
        )
        assert task is not None
        assert task["data"]["status"] == "quarantine"
        assert "无法判定" in task["data"]["audit_text"]
        assert "缺判" in task["data"]["audit_text"]


# ── TestExportBatchPathSelection ────────────────────────────────


class TestExportBatchPathSelection:
    def test_默认走trajectory路径(self, task_views_root: Path, tmp_path: Path):
        # 在 tmp_path 落 trajectory_candidates.jsonl(由 _trajectory_jsonl_in fixture)
        rec = _trajectory()
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        plan = export_batch(tmp_path, task_views_root=task_views_root)
        assert plan.count == 1
        assert plan.skipped == []
        assert plan.tasks[0]["data"]["sample_id"] == "T001__r-1"

    def test_缺trajectory_jsonl返空plan(self, task_views_root: Path, tmp_path: Path):
        plan = export_batch(tmp_path, task_views_root=task_views_root)
        assert plan.count == 0
        assert plan.skipped == []  # 文件都不存在,谈不上 skip

    def test_T视图缺_skipped进plan(self, tmp_path: Path):
        rec = _trajectory()
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        plan = export_batch(tmp_path, task_views_root=tmp_path)
        assert plan.count == 0
        assert plan.skipped and plan.skipped[0][0] == "T001__r-1"
        assert "T 视图" in plan.skipped[0][1]

    def test_legacy旗标走旧per_unit路径(self, task_views_root: Path, tmp_path: Path):
        """legacy_unit_trajectory=True 时,期望读 scout 三件套而非 trajectory_candidates.jsonl。"""
        # 写 per-unit 旧格式(1 条正例)
        (tmp_path / "positive_candidates.jsonl").write_text(
            json.dumps({
                "sample_id": "s-001", "task_id": "T001", "run_id": "r-1",
                "status": "scored",
                "scores": [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
                "tags": [], "evidence_supplement": {},
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # 手写 P3 视图文件(read_p3 的目录约定:<root>/views/<sid>_<view>.json)
        views_dir = tmp_path / "views"
        views_dir.mkdir()
        for view in ("messages", "openai", "meta"):
            (views_dir / f"s-001_{view}.json").write_text(
                json.dumps(
                    {"messages": [{"role": "user", "content": "hi"}],
                     "openai": [], "meta": {}}[view],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        plan = export_batch(
            tmp_path, views_root=tmp_path, legacy_unit_trajectory=True,
        )
        assert plan.count == 1
        assert plan.tasks[0]["data"]["sample_id"] == "s-001"

    def test_默认路径不读per_unit旧文件(self, task_views_root: Path, tmp_path: Path):
        """默认 trajectory 路径下,per-unit scout 三件套不参与。"""
        (tmp_path / "positive_candidates.jsonl").write_text(
            json.dumps({
                "sample_id": "s-001", "task_id": "T001", "run_id": "r-1",
                "status": "scored",
                "scores": [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
                "tags": [], "evidence_supplement": {},
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        plan = export_batch(tmp_path, task_views_root=task_views_root)
        # 没 trajectory_candidates.jsonl → 0 条
        assert plan.count == 0

    def test_limit裁剪(self, task_views_root: Path, tmp_path: Path):
        path = tmp_path / "trajectory_candidates.jsonl"
        path.write_text(
            json.dumps(_trajectory(trajectory_id="T001__r-1"), ensure_ascii=False) + "\n"
            + json.dumps(
                _trajectory(
                    trajectory_id="T002__r-1", task_id="T002",
                    scores=[_dim("d2", 2), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
                ),
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        # T002 的 T 视图没写
        plan = export_batch(tmp_path, task_views_root=task_views_root, limit=1)
        assert plan.count == 1
        assert plan.tasks[0]["data"]["sample_id"] == "T001__r-1"


# ── TestIntegrationWithAggregate ─────────────────────────────────


class TestIntegrationWithAggregate:
    """scout.aggregate 出的 trajectory_candidates.jsonl → exporter 直接读通。"""

    def test_aggregate产物_exporter能直接读(self, tmp_path: Path, task_views_root: Path):
        from trajectory_pipeline.evaluation.scout.aggregate import (
            aggregate_from_buckets,
        )
        from trajectory_pipeline.evaluation.scout.types import ScoutRecord
        # 落 1 份 per-unit scout 三件套
        scout_dir = tmp_path
        unit: ScoutRecord = {
            "sample_id": "T001__r-1__search", "task_id": "T001", "run_id": "r-1",
            "status": "scored",
            "scores": [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
            "tags": [],
            "evidence_supplement": {},
        }
        (scout_dir / "positive_candidates.jsonl").write_text(
            json.dumps(unit, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        # 走 aggregate 落 trajectory_candidates.jsonl
        recs, stats = aggregate_from_buckets(scout_dir, overwrite=True)
        assert len(recs) == 1
        assert stats["scored"] == 1

        # 直接喂给 export_batch —— 链路通
        plan = export_batch(scout_dir, task_views_root=task_views_root)
        assert plan.count == 1
        assert plan.tasks[0]["data"]["status"] == "positive"
        assert plan.tasks[0]["data"]["sample_id"] == "T001__r-1"
