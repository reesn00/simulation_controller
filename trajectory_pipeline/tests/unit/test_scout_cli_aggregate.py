"""scout.cli aggregate 子命令测试。

**这是 2026-10-19 增量六十一 NameError 回归封堵**:
``scout.cli aggregate`` / ``verify`` 之前的函数定义在 ``main()`` 之后,
模块作为 ``__main__`` 执行时(命令行触发)会 NameError;测试 ``import`` 走另
一条路径(模块级函数定义照样绑定)所以测试能过但命令炸。本测试端到端调
``main(["aggregate", ...])`` 走 dispatch 路径,任何子命令的解析期 / 运行期
故障都暴露出来。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_pipeline.evaluation.scout.cli import main as _scout_main


def _scout_record(
    sample_id: str = "u-001",
    task_id: str = "T001",
    run_id: str = "r-1",
    status: str = "positive",
    score: int = 4,
) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "task_id": task_id,
        "run_id": run_id,
        "status": status,
        "scores": [
            {"dimension": "d2", "score": score, "is_veto": False, "reasons": []},
            {"dimension": "d4", "score": score, "is_veto": False, "reasons": []},
            {"dimension": "d5", "score": score, "is_veto": False, "reasons": []},
            {"dimension": "d6", "score": score, "is_veto": False, "reasons": []},
        ],
        "tags": [],
        "evidence_supplement": {},
    }


def _write_bucket(scout_dir: Path, bucket: str, record: dict[str, Any]) -> Path:
    # ⚠️ 文件名约定来自 ls_bridge.exporter.BUCKET_FILES,
    # 不是 ``{bucket}.jsonl``——那是带历史缓存的别名
    p = scout_dir / f"{bucket}_candidates.jsonl"
    p.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


# ── TestBuildParserCoverage ──────────────────────────────


class TestBuildParserCoverage:
    """通过 main(['aggregate', ...]) 触发 dispatch;确认子命令 + 参数 + 必填检查。"""

    def test_parser_有aggregate子命令(self, capsys):
        # 缺参数 main() 会 SystemExit → 用 pytest.raises 接
        with pytest.raises(SystemExit):
            _scout_main(["aggregate"])
        assert "aggregate" in capsys.readouterr().err or "aggregate" in capsys.readouterr().out

    def test_scout_dir必填(self, capsys):
        # main 会 argparse error → SystemExit(2)
        with pytest.raises(SystemExit) as exc:
            _scout_main(["aggregate"])
        assert exc.value.code == 2


# ── TestAggregateCliBehavior ───────────────────────────────


class TestAggregateCliBehavior:
    def test_scout_dir不是目录返2(self, tmp_path: Path, capsys):
        rc = _scout_main([
            "aggregate",
            "--scout-dir", str(tmp_path / "missing"),
        ])
        assert rc == 2
        assert "不是目录" in capsys.readouterr().err

    def test_空三件套返0_空trajectory(self, tmp_path: Path, capsys):
        # 三件套目录存在但 jsonl 都不存在 → aggregate_from_buckets 走空集
        rc = _scout_main(["aggregate", "--scout-dir", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "scout 聚合" in out
        # 0 条 trajectory
        out_path = tmp_path / "trajectory_candidates.jsonl"
        assert out_path.exists()
        assert out_path.read_text(encoding="utf-8") == ""
        stats_path = tmp_path / "trajectory_stats.json"
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        assert stats == {"scored": 0, "undetermined": 0, "not_run": 0}

    def test_一条正样本端到端跑通(self, tmp_path: Path, capsys):
        # 一条 positive bucket unit → 一条 trajectory (scored)
        _write_bucket(tmp_path, "positive", _scout_record(score=4))
        rc = _scout_main(["aggregate", "--scout-dir", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "scored=1" in out
        assert "undetermined=0" in out
        assert "not_run=0" in out

        # 落盘形态检查
        out_path = tmp_path / "trajectory_candidates.jsonl"
        lines = [
            ln for ln in out_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["task_id"] == "T001"
        assert rec["run_id"] == "r-1"
        assert rec["trajectory_id"] == "T001__r-1"
        assert rec["status"] == "scored"
        assert len(rec["scores"]) == 4

    def test_overwrite默认不覆盖已存在产物(self, tmp_path: Path, capsys):
        _write_bucket(tmp_path, "positive", _scout_record(score=4))
        # 第一次跑 — 落盘
        rc = _scout_main(["aggregate", "--scout-dir", str(tmp_path)])
        assert rc == 0

        out_path = tmp_path / "trajectory_candidates.jsonl"
        # 把现有产物塞一个不一样的内容作为"旧"产物
        out_path.write_text(
            json.dumps({"trajectory_id": "STALE", "task_id": "X"},
                       ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        # 第二次跑(不传 --overwrite)→ 应跳过写入
        rc = _scout_main(["aggregate", "--scout-dir", str(tmp_path)])
        assert rc == 0
        # 旧产物仍在(STALE 没被覆盖)
        lines = [
            ln for ln in out_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) == 1
        assert json.loads(lines[0])["trajectory_id"] == "STALE"

    def test_overwrite_显式传_允许覆盖(self, tmp_path: Path, capsys):
        _write_bucket(tmp_path, "positive", _scout_record(score=4))
        _scout_main(["aggregate", "--scout-dir", str(tmp_path)])
        out_path = tmp_path / "trajectory_candidates.jsonl"
        out_path.write_text(
            json.dumps({"trajectory_id": "STALE"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        rc = _scout_main(["aggregate", "--scout-dir", str(tmp_path), "--overwrite"])
        assert rc == 0
        # STALE 已被新产物替换
        lines = [
            ln for ln in out_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len(lines) == 1
        assert json.loads(lines[0])["trajectory_id"] == "T001__r-1"

    def test_buckets_filter_only_positive(self, tmp_path: Path, capsys):
        # positive 一条 + negative 一条 → --buckets=positive 应只剩 1 条 trajectory
        _write_bucket(tmp_path, "positive", _scout_record(sample_id="p-u", score=4))
        _write_bucket(tmp_path, "negative", _scout_record(
            sample_id="n-u", status="negative", score=2, task_id="T002",
        ))
        rc = _scout_main([
            "aggregate", "--scout-dir", str(tmp_path),
            "--buckets", "positive",
        ])
        assert rc == 0
        out_path = tmp_path / "trajectory_candidates.jsonl"
        lines = [
            ln for ln in out_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        # 只剩 positive 的 T001__r-1
        assert len(lines) == 1
        assert json.loads(lines[0])["task_id"] == "T001"

    def test_out_explicit_output_path(self, tmp_path: Path, capsys):
        _write_bucket(tmp_path, "positive", _scout_record(score=4))
        custom = tmp_path / "custom.jsonl"
        rc = _scout_main([
            "aggregate", "--scout-dir", str(tmp_path),
            "--out", str(custom),
        ])
        assert rc == 0
        assert custom.exists()
        # 默认位置不应被写
        assert not (tmp_path / "trajectory_candidates.jsonl").exists()
        # stats 默认落 <out 的父目录>
        assert (custom.parent / "trajectory_stats.json").exists()


# ── TestNoNameErrorRegression ─────────────────────────────


class TestNoNameErrorRegression:
    """**回归封堵**:之前 ``_cmd_aggregate`` / ``_cmd_verify`` 在 ``main()``
    之后定义,作为 ``__main__`` 执行时 main 函数体内 ``return _cmd_aggregate(args)``
    触发 NameError。本测试只要 dispatch 走到函数体就不抛 NameError。

    ⚠️ 仅 import 不够——import 路径下模块级 def 在 ``main`` 之后照样会绑定,
    旧测试也"过";本测试以 ``main([...])`` 端到端触发,确保定义顺序不再错位。
    """

    def test_aggregate_dispatch_不抛NameError(self, tmp_path: Path):
        # 即便子命令走完返回非 0(比如 2 = 目录不存在),只要不是 NameError 就算修好
        rc = _scout_main(["aggregate", "--scout-dir", str(tmp_path)])
        assert isinstance(rc, int)  # 走到函数体,没 NameError

    def test_verify_dispatch_不抛NameError(self, tmp_path: Path):
        rc = _scout_main(["verify", "--scout-dir", str(tmp_path)])
        assert isinstance(rc, int)

    def test_run_dispatch_不抛NameError(self, tmp_path: Path, capsys):
        # run 子命令走原 main 逻辑;--p2-root 是必填。给个不存在的根 → 返 2
        rc = _scout_main(["run", "--p2-root", str(tmp_path / "x"),
                          "--out", str(tmp_path)])
        assert rc == 2