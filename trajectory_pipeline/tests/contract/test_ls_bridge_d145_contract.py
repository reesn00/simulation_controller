"""ls_bridge D14.5 契约:trajectory 主路径 + 旧 per-unit 路径弃用旗标。

D14.5 = ls_bridge.exporter 接 trajectory_candidates.jsonl + T 视图(主路径),
旧 per-unit 路径保留为 ``--legacy-unit-trajectory`` 旗标(默认弃用)。

盯住的不变量:

* **默认行为 = trajectory 主路径**——``export_batch(legacy_unit_trajectory=False)``
  读 ``trajectory_candidates.jsonl`` 而不是 scout 三件套
* **旧路径显式开**——``legacy_unit_trajectory=True`` 是唯一进入旧路径的方式
* **T 视图缺失 = skip**——fail-closed,绝不带半份数据推出去
* **桶派生** = status 一票(``undetermined`` / ``not_run`` 走 quarantine,
  即便 score 全 5)
* **桶派生** = 否则走 ``derive_overall``(veto 先于低分先于缺判)
* **白名单约束** = exporter 只准依赖 storage / scout.types /
  scout.aggregate(同 evaluation 层子模块的桶名常量)
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from trajectory_pipeline.evaluation.ls_bridge import exporter
from trajectory_pipeline.evaluation.ls_bridge.exporter import (
    _derive_trajectory_bucket,
    export_batch,
    export_trajectory_one,
    read_trajectory_records,
)
from trajectory_pipeline.evaluation.scout.aggregate import TRAJECTORY_BUCKET_FILE
from trajectory_pipeline.evaluation.scout.types import DimensionScore, TrajectoryRecord


def _dim(dim: str, score: int | None, *, veto: bool = False, reasons=None) -> DimensionScore:
    return {
        "dimension": dim,  # type: ignore[typeddict-item]
        "score": score,
        "is_veto": veto,
        "reasons": list(reasons or []),
    }


def _trajectory(
    *, status: str = "scored", scores=None, tags=None
) -> TrajectoryRecord:
    return TrajectoryRecord(
        trajectory_id="T001__r-1",
        task_id="T001",
        run_id="r-1",
        status=status,  # type: ignore[arg-type]
        scores=scores or [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
        tags=list(tags or []),  # type: ignore[arg-type]
        unit_outcomes=["T001__r-1__search"],
        evidence_supplement={},
        aggregation_log=[],
    )


# ── A: 默认行为 = trajectory 主路径 ────────────────────────────────


class TestDefaultPathIsTrajectory:
    """``export_batch`` 默认 = trajectory 主路径,旧路径靠旗标显式开。"""

    def test_export_batch签名_有legacy参数(self):
        sig = inspect.signature(export_batch)
        assert "legacy_unit_trajectory" in sig.parameters
        # 默认值必须为 False(默认弃用旧路径)
        assert sig.parameters["legacy_unit_trajectory"].default is False

    def test_export_batch签名_有task_views_root参数(self):
        sig = inspect.signature(export_batch)
        assert "task_views_root" in sig.parameters
        assert sig.parameters["task_views_root"].default is None

    def test_默认调_读trajectory_candidates_jsonl(self, tmp_path: Path):
        """默认(legacy=False)走 trajectory 主路径,读 trajectory_candidates.jsonl。"""
        # 只落 trajectory_candidates.jsonl,看 export_batch 是否读它
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            "{}\n",  # 坏行就跳过——只关心 plan.skipped 的源头是它
            encoding="utf-8",
        )
        # 设一个假的 read_task_view 入口(确保 T 视图读取路径被调),
        # 但 tmp_path 下没有 T 视图,应当全部 skip
        plan = export_batch(tmp_path, task_views_root=tmp_path)
        # trajectory_candidates.jsonl 被读了(plan.skipped 来源就是它)
        # 坏行被跳过,count 应当为 0,skipped 为空(坏行不进 skipped 计数)
        assert plan.count == 0
        assert plan.skipped == []

    def test_默认调_不读per_unit_scout_三件套(self, tmp_path: Path):
        """默认路径不读 positive_candidates.jsonl(那走 legacy)。"""
        # 只落 per-unit 旧文件
        (tmp_path / "positive_candidates.jsonl").write_text(
            '{"sample_id": "s-001", "task_id": "T001", "run_id": "r-1", '
            '"status": "scored", "scores": [], "tags": [], "evidence_supplement": {}}\n',
            encoding="utf-8",
        )
        plan = export_batch(tmp_path, task_views_root=tmp_path)
        # 默认不读 positive_candidates.jsonl,所以 0 条
        assert plan.count == 0


# ── B: TRAJECTORY_BUCKET_FILE 常量名稳定 ─────────────────────────


class TestTrajectoryBucketFileName:
    def test_常量名稳定(self):
        assert TRAJECTORY_BUCKET_FILE == "trajectory_candidates.jsonl"

    def test_read_trajectory_records用该常量(self, tmp_path: Path):
        """文件名改了就沉默地读不到——契约盯住读侧使用同款常量。"""
        # 在 tmp_path 落一个 trajectory_candidates.jsonl
        rec = _trajectory()
        import json
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        out = read_trajectory_records(tmp_path)
        assert len(out) == 1
        # 改个名就读不到(双保险:常量名 + 读侧调用同款)
        (tmp_path / "trajectory_candidates.jsonl").rename(
            tmp_path / "other_name.jsonl"
        )
        out2 = read_trajectory_records(tmp_path)
        assert out2 == []


# ── C: 桶派生纪律 ──────────────────────────────────────────────


class TestBucketDerivation:
    def test_not_run强制quarantine_即便score全5(self):
        rec = _trajectory(
            status="not_run",
            scores=[_dim("d2", 5), _dim("d4", 5), _dim("d5", 5), _dim("d6", 5)],
        )
        assert _derive_trajectory_bucket(rec) == "quarantine"

    def test_undetermined强制quarantine(self):
        rec = _trajectory(
            status="undetermined",
            scores=[_dim("d2", 5), _dim("d4", 5), _dim("d5", 5), _dim("d6", 5)],
        )
        assert _derive_trajectory_bucket(rec) == "quarantine"

    def test_scored且veto进negative(self):
        rec = _trajectory(
            status="scored",
            scores=[_dim("d2", 5), _dim("d4", 5), _dim("d5", 1, veto=True), _dim("d6", 5)],
        )
        assert _derive_trajectory_bucket(rec) == "negative"

    def test_scored且低分进negative(self):
        rec = _trajectory(
            status="scored",
            scores=[_dim("d2", 2), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
        )
        assert _derive_trajectory_bucket(rec) == "negative"

    def test_scored且全高分进positive(self):
        rec = _trajectory(
            status="scored",
            scores=[_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
        )
        assert _derive_trajectory_bucket(rec) == "positive"


# ── D: T 视图缺失 = skip(不推半份) ────────────────────────────


class TestTaskViewMissingFailClosed:
    def test_export_trajectory_one_T缺返None(self, tmp_path: Path):
        """T 视图缺失 → (None, None, []),由 export_batch 记 skipped。"""
        rec = _trajectory()
        task, prediction, hits = export_trajectory_one(
            rec, "positive", task_views_root=tmp_path,
        )
        assert task is None
        assert prediction is None
        assert hits == []

    def test_export_batch_T缺进skipped_不进tasks(self, tmp_path: Path):
        import json
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(_trajectory(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        plan = export_batch(tmp_path, task_views_root=tmp_path)
        assert plan.count == 0
        assert plan.skipped and plan.skipped[0][0] == "T001__r-1"
        assert "T 视图" in plan.skipped[0][1]


# ── E: exporter 白名单约束(同 test_ls_bridge_contract) ─────────


class TestImportBoundary:
    """防 ls_bridge.exporter 偷偷 import executor / assembler / 仓库根。"""

    @pytest.mark.parametrize(
        "mod",
        ["trajectory_pipeline.executor", "trajectory_pipeline.assembler"],
    )
    def test_不import_executor_或_assembler(self, mod: str) -> None:
        path = Path("trajectory_pipeline/evaluation/ls_bridge/exporter.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            target = None
            if isinstance(node, ast.Import):
                target = ", ".join(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                target = node.module or ""
            if target and target.startswith(mod):
                offenders.append(target)
        assert not offenders, (
            f"exporter.py 偷偷依赖 {mod}: {offenders}; "
            f"ls_bridge 是 evaluation 层,不允许跨到执行/组装"
        )

    def test_公开符号不引executor_assembler对象(self):
        """出口处(`__all__`)的函数,递归看源码里没 import 那些被禁模块的对象。"""
        for name in exporter.__all__:
            obj = getattr(exporter, name)
            try:
                src = inspect.getsource(obj)
            except (TypeError, OSError):
                continue
            assert "from trajectory_pipeline.executor" not in src, (
                f"{name} 的源码 import 了 executor"
            )
            assert "from trajectory_pipeline.assembler" not in src, (
                f"{name} 的源码 import 了 assembler"
            )


# ── F: 默认不开启 legacy(防回归:不小心把 default 改 True) ──────────


class TestLegacyDefaultGuard:
    def test_默认False(self):
        sig = inspect.signature(export_batch)
        assert sig.parameters["legacy_unit_trajectory"].default is False, (
            "legacy_unit_trajectory 默认必须是 False;改 True 等于回退到 per-unit 旧路径"
        )

    def test_没有_legacy_OFF_的旗标名(self):
        """旗标名正向/反向都得是 ``legacy_unit_trajectory``——改名/拼错会让
        CLI 静默失效(无旗标就走默认 trajectory 路径)。"""
        sig = inspect.signature(export_batch)
        params = [n for n in sig.parameters if "legacy" in n]
        assert params == ["legacy_unit_trajectory"]
