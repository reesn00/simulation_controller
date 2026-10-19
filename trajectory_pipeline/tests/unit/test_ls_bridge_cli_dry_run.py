"""ls_bridge CLI dry-run 输出测试(Phase 6, 风险 1 加注)。

盯住:

* 主路径(trajectory)与旧路径(per-unit)分别有**正确的桶分布打印**
  + **口径标签**(per-Run vs per-unit),防分母混淆
* T 视图缺失进 ``__skipped`` 桶(不是正例)
* ``--dry-run`` 不触网且 0 退出
* ``--buckets`` 校验:空字符串返 2
* 缺 scout_dir 返 2
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_pipeline.evaluation.ls_bridge.cli import _build_parser, _cmd_upload


def _ns(**overrides: Any):
    """最小 namespace 模拟 argparse,只填上传相关参数。"""
    base = {
        "url": None,
        "api_key": None,
        "command": "upload",
        "scout_dir": None,
        "buckets": "positive,negative,quarantine",
        "views_root": None,
        "task_views_root": None,
        "legacy_unit_trajectory": False,
        "limit": None,
        "batch_size": None,
        "no_scorecard": False,
        "dry_run": True,
        "dry_run_show": 5,
        "force": False,
    }
    base.update(overrides)
    return type("Args", (), base)()


def _dim(dim: str, score: int | None, *, veto: bool = False, reasons=None) -> dict:
    return {
        "dimension": dim,
        "score": score,
        "is_veto": veto,
        "reasons": list(reasons or []),
    }


def _write_t_view(root: Path, trajectory_id: str) -> None:
    from trajectory_pipeline.storage import task_views_dir
    tdir = task_views_dir(root)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{trajectory_id}.json").write_text(
        json.dumps({
            "messages": [{"role": "user", "content": "hi"}],
            "openai": [], "meta": {"task_id": "T001", "run_id": "r-1"},
        }, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture()
def task_views_root(tmp_path: Path) -> Path:
    """落 T 视图(``<root>/task_views/<tid>.json``)——预落 ``T001__r-1.json``。"""
    _write_t_view(tmp_path, "T001__r-1")
    return tmp_path
    from trajectory_pipeline.storage import task_views_dir
    tdir = task_views_dir(root)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{trajectory_id}.json").write_text(
        json.dumps({
            "messages": [{"role": "user", "content": "hi"}],
            "openai": [], "meta": {"task_id": "T001", "run_id": "r-1"},
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _write_trajectory_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


# ── TestDryRunOutput ──────────────────────────────────────


class TestDryRunOutput:
    """dry-run 段输出格式 + 口径标签(风险 1 加注)。"""

    def test_主路径_打印per_Run口径(self, task_views_root: Path, tmp_path: Path):
        rec = {
            "trajectory_id": "T001__r-1", "task_id": "T001", "run_id": "r-1",
            "status": "scored",
            "scores": [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
            "tags": [], "unit_outcomes": [], "evidence_supplement": {},
            "aggregation_log": [],
        }
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        ns = _ns(scout_dir=str(tmp_path), task_views_root=str(task_views_root))
        rc = _cmd_upload(ns)
        assert rc == 0
        # 但 stdout 是 capsys 抓的,见下

    def test_主路径_stdout_含口径与桶分布(self, task_views_root: Path, tmp_path: Path, capsys):
        rec = {
            "trajectory_id": "T001__r-1", "task_id": "T001", "run_id": "r-1",
            "status": "scored",
            "scores": [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
            "tags": [], "unit_outcomes": [], "evidence_supplement": {},
            "aggregation_log": [],
        }
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        ns = _ns(scout_dir=str(tmp_path), task_views_root=str(task_views_root))
        _cmd_upload(ns)
        out = capsys.readouterr().out
        assert "主路径" in out
        assert "trajectory_candidates.jsonl" in out
        assert "per-Run" in out  # 口径标签
        assert "桶分布" in out
        assert "positive" in out  # 桶名

    def test_legacy路径_stdout_打DEPRECATED与per_unit口径(
        self, tmp_path: Path, capsys,
    ):
        # 写 per-unit scout 三件套 + P3 视图
        (tmp_path / "positive_candidates.jsonl").write_text(
            json.dumps({
                "sample_id": "s-001", "task_id": "T001", "run_id": "r-1",
                "status": "scored",
                "scores": [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
                "tags": [], "evidence_supplement": {},
            }, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        views = tmp_path / "views"
        views.mkdir()
        for view in ("messages", "openai", "meta"):
            (views / f"s-001_{view}.json").write_text(
                json.dumps(
                    {"messages": [{"role": "user", "content": "hi"}],
                     "openai": [], "meta": {}}[view],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        ns = _ns(
            scout_dir=str(tmp_path), views_root=str(tmp_path),
            legacy_unit_trajectory=True,
        )
        _cmd_upload(ns)
        out = capsys.readouterr().out
        assert "DEPRECATED" in out
        assert "per-unit" in out  # 口径标签
        assert "桶分布" in out

    def test_反例_两路径口径不混(self, task_views_root: Path, tmp_path: Path, capsys):
        """trajectory 主路径下绝不出现 per-unit 口径标签(防标签漂移)。"""
        rec = {
            "trajectory_id": "T001__r-1", "task_id": "T001", "run_id": "r-1",
            "status": "scored",
            "scores": [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
            "tags": [], "unit_outcomes": [], "evidence_supplement": {},
            "aggregation_log": [],
        }
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        ns = _ns(scout_dir=str(tmp_path), task_views_root=str(task_views_root))
        _cmd_upload(ns)
        out = capsys.readouterr().out
        # 主路径下不应出现 legacy/per-unit 字样
        assert "DEPRECATED" not in out
        assert "per-unit" not in out

    def test_T视图缺进skipped_不混进positive(
        self, tmp_path: Path, capsys,
    ):
        rec = {
            "trajectory_id": "T001__r-1", "task_id": "T001", "run_id": "r-1",
            "status": "scored",
            "scores": [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
            "tags": [], "unit_outcomes": [], "evidence_supplement": {},
            "aggregation_log": [],
        }
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        # T 视图不写 → 应当 skipped
        ns = _ns(scout_dir=str(tmp_path), task_views_root=str(tmp_path))
        _cmd_upload(ns)
        out = capsys.readouterr().out
        assert "__skipped" in out
        assert "1" in out  # 至少 1 条 skipped
        # 不应出现 positive
        assert "'positive':" not in out
        assert "'positive': " not in out

    def test_空桶分布_打印空plan_不崩(self, task_views_root: Path, tmp_path: Path, capsys):
        # 不写 trajectory_candidates.jsonl
        ns = _ns(scout_dir=str(tmp_path), task_views_root=str(task_views_root))
        rc = _cmd_upload(ns)
        out = capsys.readouterr().out
        assert rc == 0
        assert "桶分布" in out
        assert "(空)" in out

    def test_多桶混合_各桶计打印(
        self, task_views_root: Path, tmp_path: Path, capsys,
    ):
        recs = [
            {
                "trajectory_id": "T001__r-1", "task_id": "T001", "run_id": "r-1",
                "status": "scored",
                "scores": [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
                "tags": [], "unit_outcomes": [], "evidence_supplement": {},
                "aggregation_log": [],
            },
            {
                "trajectory_id": "T002__r-1", "task_id": "T002", "run_id": "r-1",
                "status": "scored",
                "scores": [_dim("d2", 2), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
                "tags": ["d2_failed"], "unit_outcomes": [], "evidence_supplement": {},
                "aggregation_log": [],
            },
        ]
        _write_t_view(task_views_root, "T001__r-1")
        _write_t_view(task_views_root, "T002__r-1")
        _write_trajectory_jsonl(tmp_path / "trajectory_candidates.jsonl", recs)
        ns = _ns(scout_dir=str(tmp_path), task_views_root=str(task_views_root))
        _cmd_upload(ns)
        out = capsys.readouterr().out
        assert "'positive': 1" in out
        assert "'negative': 1" in out


# ── TestArgsValidation ──────────────────────────────────


class TestArgsValidation:
    """参数校验:早返不浪费计算。"""

    def test_buckets空返2(self, tmp_path: Path, capsys):
        ns = _ns(scout_dir=str(tmp_path), buckets="")
        rc = _cmd_upload(ns)
        assert rc == 2
        assert "--buckets" in capsys.readouterr().err

    def test_scout_dir不是目录返2(self, tmp_path: Path, capsys):
        not_a_dir = tmp_path / "missing"
        ns = _ns(scout_dir=str(not_a_dir))
        rc = _cmd_upload(ns)
        assert rc == 2
        assert "不是目录" in capsys.readouterr().err


# ── TestBuildParserCoverage ──────────────────────────────


class TestBuildParserCoverage:
    """parser 阶段必须暴露 D14.5 新参数(防静默失效)。"""

    def test_parser_有task_views_root参数(self):
        parser = _build_parser()
        args = parser.parse_args([
            "upload", "--scout-dir", "x",
            "--task-views-root", "y",
        ])
        assert args.task_views_root == "y"

    def test_parser_有legacy_unit_trajectory旗标(self):
        parser = _build_parser()
        args = parser.parse_args([
            "upload", "--scout-dir", "x", "--legacy-unit-trajectory",
        ])
        assert args.legacy_unit_trajectory is True

    def test_parser_默认legacy_unit_trajectory_False(self):
        parser = _build_parser()
        args = parser.parse_args(["upload", "--scout-dir", "x"])
        assert args.legacy_unit_trajectory is False
