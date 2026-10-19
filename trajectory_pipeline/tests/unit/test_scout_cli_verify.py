"""scout.cli verify 子命令测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_pipeline.evaluation.scout.cli import main as _scout_main


def _ns(**overrides: Any):
    base = {
        "cmd": "verify",
        "scout_dir": None,
        "task_views_root": None,
    }
    base.update(overrides)
    return type("Args", (), base)()


def _write_t_view(root: Path, trajectory_id: str) -> None:
    from trajectory_pipeline.storage import task_views_dir
    tdir = task_views_dir(root)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{trajectory_id}.json").write_text(
        json.dumps({
            "messages": [{"role": "user", "content": "hi"}],
            "openai": [], "meta": {},
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _trajectory(trajectory_id: str = "T001__r-1") -> dict[str, Any]:
    return {
        "trajectory_id": trajectory_id, "task_id": "T001", "run_id": "r-1",
        "status": "scored",
        "scores": [
            {"dimension": "d2", "score": 4, "is_veto": False, "reasons": []},
            {"dimension": "d4", "score": 5, "is_veto": False, "reasons": []},
            {"dimension": "d5", "score": 4, "is_veto": False, "reasons": []},
            {"dimension": "d6", "score": 5, "is_veto": False, "reasons": []},
        ],
        "tags": [], "unit_outcomes": [], "evidence_supplement": {},
        "aggregation_log": [],
    }


# ── TestBuildParserCoverage ─────────────────────────────


class TestBuildParserCoverage:
    """通过 main(['verify', ...]) 触发 dispatch;确认子命令 + 参数 + 必填检查。"""

    def test_parser_有verify子命令(self, capsys):
        # 缺参数 main() 会 SystemExit → 用 pytest.raises 接
        with pytest.raises(SystemExit):
            _scout_main(["verify"])
        # SystemExit(2) 是 argparse 缺参;capsys 会有 usage 信息
        assert "verify" in capsys.readouterr().err or capsys.readouterr().out

    def test_scout_dir必填(self, capsys):
        # main 会 argparse error → SystemExit(2)
        with pytest.raises(SystemExit) as exc:
            _scout_main(["verify"])
        assert exc.value.code == 2

    def test_task_views_root可显式传(self, tmp_path, capsys):
        # 跑通整个 dispatch 到 _cmd_verify
        # 没 jsonl / stats → missing_jsonl error → 返 1
        rc = _scout_main([
            "verify", "--scout-dir", str(tmp_path), "--task-views-root", str(tmp_path),
        ])
        assert rc == 1  # missing_jsonl
        out = capsys.readouterr().out
        assert "[scout verify]" in out
        assert "missing_jsonl" in out

    def test_task_views_root_默认None_走storage默认根(self, tmp_path, capsys):
        # 落 jsonl + stats 在 tmp_path(scout_dir)
        # 但 task_views_root 默认 → 仓库默认 task_views 根
        # 因为这里没 T 视图 → 必报 missing_t_view(每个 trajectory 都报)
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(_trajectory(), ensure_ascii=False) + "\n", encoding="utf-8",
        )
        (tmp_path / "trajectory_stats.json").write_text(
            json.dumps({"scored": 1, "undetermined": 0, "not_run": 0}),
            encoding="utf-8",
        )
        # 不传 --task-views-root
        rc = _scout_main(["verify", "--scout-dir", str(tmp_path)])
        # stats 一致,T 视图缺是 warning,exit 0
        assert rc == 0
        out = capsys.readouterr().out
        assert "missing_t_view" in out  # 至少 1 条 warning


# ── TestVerifyCliBehavior ──────────────────────────────


class TestVerifyCliBehavior:
    def test_全过返0(self, tmp_path: Path, capsys):
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(_trajectory(), ensure_ascii=False) + "\n", encoding="utf-8",
        )
        (tmp_path / "trajectory_stats.json").write_text(
            json.dumps({"scored": 1, "undetermined": 0, "not_run": 0}),
            encoding="utf-8",
        )
        _write_t_view(tmp_path, "T001__r-1")
        rc = _scout_main([
            "verify", "--scout-dir", str(tmp_path), "--task-views-root", str(tmp_path),
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "[scout verify]" in out
        assert "✓ 全部通过" in out

    def test_有error返1(self, tmp_path: Path, capsys):
        # 缺 jsonl → missing_jsonl error
        (tmp_path / "trajectory_stats.json").write_text(
            json.dumps({"scored": 0, "undetermined": 0, "not_run": 0}),
            encoding="utf-8",
        )
        rc = _scout_main([
            "verify", "--scout-dir", str(tmp_path), "--task-views-root", str(tmp_path),
        ])
        assert rc == 1
        out = capsys.readouterr().out
        assert "✗ missing_jsonl" in out

    def test_纯warning返0(self, tmp_path: Path, capsys):
        # T 视图缺是 warning
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(_trajectory(), ensure_ascii=False) + "\n", encoding="utf-8",
        )
        (tmp_path / "trajectory_stats.json").write_text(
            json.dumps({"scored": 1, "undetermined": 0, "not_run": 0}),
            encoding="utf-8",
        )
        # 不写 T 视图
        rc = _scout_main([
            "verify", "--scout-dir", str(tmp_path), "--task-views-root", str(tmp_path),
        ])
        assert rc == 0  # warning 不影响退出码
        out = capsys.readouterr().out
        assert "⚠ missing_t_view" in out

    def test_scout_dir不是目录返2(self, tmp_path: Path, capsys):
        rc = _scout_main([
            "verify",
            "--scout-dir", str(tmp_path / "missing"),
            "--task-views-root", str(tmp_path),
        ])
        assert rc == 2
        assert "不是目录" in capsys.readouterr().err

    def test_反例_混合error_warning_exitcode_1(self, tmp_path: Path, capsys):
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(_trajectory(), ensure_ascii=False) + "\n", encoding="utf-8",
        )
        # stats 算错
        (tmp_path / "trajectory_stats.json").write_text(
            json.dumps({"scored": 5, "undetermined": 0, "not_run": 0}),
            encoding="utf-8",
        )
        # T 视图缺(算 warning)
        rc = _scout_main([
            "verify", "--scout-dir", str(tmp_path), "--task-views-root", str(tmp_path),
        ])
        assert rc == 1
        out = capsys.readouterr().out
        # error 先排
        err_pos = out.find("stats_count_mismatch")
        warn_pos = out.find("missing_t_view")
        assert err_pos != -1 and warn_pos != -1
        assert err_pos < warn_pos
