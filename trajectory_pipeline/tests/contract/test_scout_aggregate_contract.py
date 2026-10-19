"""``evaluation/scout/aggregate.py`` 的**契约测试**——任何分支版本
都成立的不变式、文档与代码必须同步。

与 :file:`tests/unit/test_scout_aggregate.py` 的分工:
    unit/        测某个输入下聚合出什么
    contract/    测**任何**聚合模块都必须满足的性质(模块图、文档同步、
                  落盘纪律)

盯四件事:
1. **模块图**: aggregator 不 import executor / assembler——聚合只动 scout
   三件套的内存 dict(交接面是格式,见方案 14 §6.3 的 TestTaskAggregatorMode)
2. **类型对齐**: :class:`TrajectoryRecord` 字段名 + 类型与方案 14 §4.2
   字段表逐项一致
3. **落盘纪律**: ``trajectory_candidates.jsonl`` 默认不覆盖,坏行容错,
   ``trajectory_stats.json`` 同产物根
4. **D14.5 边界**: aggregator 不直接做 LS 上传(那是 ls_bridge.exporter
   的事——两段独立,避免循环依赖)
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trajectory_pipeline.evaluation.scout import aggregate
from trajectory_pipeline.evaluation.scout.types import (
    AggregationLogEntry,
    ScoutRecord,
    TrajectoryRecord,
)

AGGREGATE_SRC = Path(aggregate.__file__)
SCOUT_PKG = Path(aggregate.__file__).parent


# ── 工具:AST / 字段收集 ─────────────────────────────────────────


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                mod = f"{'.' * node.level}{mod}"
            out.append(mod)
    return out


# ── 1. 模块图 ────────────────────────────────────────────────────


class Test模块图:
    def test_aggregator不import执行端点(self):
        """聚合只动 scout 三件套的内存 dict——不能 import executor。"""
        for name in _imports(AGGREGATE_SRC):
            assert "trajectory_pipeline.executor" not in name, (
                f"aggregate.py import {name}：聚合与执行端版本绑死"
            )

    def test_aggregator不import组装端点(self):
        """聚合不读 P1 / P2 / P3——交接面是 scout 三件套 dict,不是对象。"""
        for name in _imports(AGGREGATE_SRC):
            assert "trajectory_pipeline.assembler" not in name, (
                f"aggregate.py import {name}：聚合与组装端版本绑死"
            )

    def test_aggregator不import_t视图(self):
        """聚合**不**直接读 T 视图——T 视图由 ls_bridge.exporter 在上传时读。
        两段必须独立(否则引入循环依赖,见方案 14 §6.3 TestTaskAggregatorMode)。"""
        for name in _imports(AGGREGATE_SRC):
            assert "trajectory_pipeline.storage" not in name, (
                f"aggregate.py import {name}：聚合不该读 T 视图;"
                f"读 T 视图是 ls_bridge.exporter 的事"
            )


# ── 2. 类型对齐:TrajectoryRecord 字段表与方案 14 §4.2 一致 ─────


class Test类型对齐:
    def test_TrajectoryRecord必填字段(self):
        """方案 14 §4.2 列出的字段必须存在,缺一即视为任务级契约破坏。"""
        ann = TrajectoryRecord.__annotations__
        required = {
            "trajectory_id", "task_id", "run_id", "status",
            "scores", "tags", "unit_outcomes",
            "evidence_supplement", "aggregation_log",
        }
        missing = required - set(ann.keys())
        assert not missing, f"TrajectoryRecord 缺字段: {missing}"

    def test_AggregationLogEntry必填字段(self):
        ann = AggregationLogEntry.__annotations__
        required = {"unit", "action", "reason"}
        missing = required - set(ann.keys())
        assert not missing, f"AggregationLogEntry 缺字段: {missing}"

    def test_DIMENSION_ORDER是四维固定顺序(self):
        """方案 14 §4.3: 四维固定 d2/d4/d5/d6 顺序。
        改顺序会破坏 LS 评分卡对齐——这是契约。"""
        assert aggregate.DIMENSION_ORDER == ("d2", "d4", "d5", "d6")

    def test_TRAJECTORY_BUCKET_FILE名稳定(self):
        """ls_bridge.exporter 必须按这个文件名读产物——改名字要同步改两侧。"""
        assert aggregate.TRAJECTORY_BUCKET_FILE == "trajectory_candidates.jsonl"

    def test_TRAJECTORY_STATS_FILE名稳定(self):
        assert aggregate.TRAJECTORY_STATS_FILE == "trajectory_stats.json"


# ── 3. 落盘纪律 ────────────────────────────────────────────────


class Test落盘纪律:
    def test_产物文件名稳定(self):
        """``trajectory_candidates.jsonl`` 是 D14.5 主路径产物的固定名——
        ls_bridge.exporter 的 ``TRAJECTORY_BUCKET_FILE`` 必须引它。"""
        # 双向引用:aggregate 的常量名 与 ls_bridge.exporter 期望名 一致
        from trajectory_pipeline.evaluation.ls_bridge.exporter import (
            BUCKET_FILES, read_scout_records,
        )
        # 现有三件套名稳定
        assert "positive" in BUCKET_FILES
        assert "negative" in BUCKET_FILES
        assert "quarantine" in BUCKET_FILES
        # 新增 trajectory 桶名(若 ls_bridge.exporter 后续要直接读它)
        # 当前还没引,只盯 aggregate 这一侧名稳定
        assert aggregate.TRAJECTORY_BUCKET_FILE == "trajectory_candidates.jsonl"

    def test_默认不覆盖是文档明示(self):
        """``overwrite`` 参数默认 False——重建批次不该把别人刚标好的
        trajectory 抹掉(与 cli build / cli task-view 同款纪律)。"""
        import inspect
        sig = inspect.signature(aggregate.aggregate_from_buckets)
        assert sig.parameters["overwrite"].default is False
