"""scout.verify 单测:trajectory 产物一致性门禁。

盯:

* :func:`_read_jsonl_records` / :func:`_read_stats` / :func:`_check_stats_count` /
  :func:`_check_task_views` 各自的错误路径
* :func:`verify_from_dir` 整合行为
* :class:`VerifyReport` 属性 / :func:`format_report` 输出
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_pipeline.evaluation.scout.verify import (
    EXPECTED_STATS_KEYS,
    VerifyIssue,
    VerifyReport,
    _check_stats_count,
    _check_task_views,
    _read_jsonl_records,
    _read_stats,
    format_report,
    verify_from_dir,
)


# ── helpers ──────────────────────────────────────────────


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


# ── TestReadJsonlRecords ────────────────────────────────


class TestReadJsonlRecords:
    def test_缺文件返error(self, tmp_path: Path):
        path = tmp_path / "trajectory_candidates.jsonl"
        records, issues = _read_jsonl_records(path)
        assert records == []
        assert len(issues) == 1
        assert issues[0].code == "missing_jsonl"
        assert issues[0].severity == "error"

    def test_坏行进warning(self, tmp_path: Path):
        path = tmp_path / "trajectory_candidates.jsonl"
        path.write_text(
            "{not json}\n"
            + json.dumps(_trajectory(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        records, issues = _read_jsonl_records(path)
        assert len(records) == 1
        assert any(i.code == "bad_jsonl_line" for i in issues)

    def test_缺trajectory_id进warning(self, tmp_path: Path):
        path = tmp_path / "trajectory_candidates.jsonl"
        path.write_text(
            json.dumps({"no_tid": True}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        records, issues = _read_jsonl_records(path)
        assert records == []
        assert any(i.code == "bad_jsonl_line" for i in issues)

    def test_空文件_或全空行返error(self, tmp_path: Path):
        path = tmp_path / "trajectory_candidates.jsonl"
        path.write_text("\n\n\n", encoding="utf-8")
        records, issues = _read_jsonl_records(path)
        assert records == []
        assert any(i.code == "empty_jsonl" and i.severity == "error" for i in issues)

    def test_正常读取(self, tmp_path: Path):
        path = tmp_path / "trajectory_candidates.jsonl"
        path.write_text(
            json.dumps(_trajectory("T001__r-1"), ensure_ascii=False) + "\n"
            + json.dumps(_trajectory("T002__r-1"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        records, issues = _read_jsonl_records(path)
        assert len(records) == 2
        assert issues == []
        assert [r["trajectory_id"] for r in records] == ["T001__r-1", "T002__r-1"]


# ── TestReadStats ──────────────────────────────────────


class TestReadStats:
    def test_缺文件返error(self, tmp_path: Path):
        path = tmp_path / "trajectory_stats.json"
        stats, issues = _read_stats(path)
        assert stats == {}
        assert any(i.code == "missing_stats" and i.severity == "error" for i in issues)

    def test_坏json返error(self, tmp_path: Path):
        path = tmp_path / "trajectory_stats.json"
        path.write_text("{not json", encoding="utf-8")
        stats, issues = _read_stats(path)
        assert stats == {}
        assert any(i.code == "bad_stats_json" and i.severity == "error" for i in issues)

    def test_顶层不是dict返error(self, tmp_path: Path):
        path = tmp_path / "trajectory_stats.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        stats, issues = _read_stats(path)
        assert any(i.code == "bad_stats_json" for i in issues)

    def test_缺三键各一条warning(self, tmp_path: Path):
        path = tmp_path / "trajectory_stats.json"
        path.write_text(json.dumps({"scored": 5}), encoding="utf-8")
        stats, issues = _read_stats(path)
        assert stats == {"scored": 5}
        missing_key_issues = [i for i in issues if i.code == "stats_missing_key"]
        assert len(missing_key_issues) == 2
        # 两键未到齐
        keys = {i.detail for i in missing_key_issues}
        assert any("undetermined" in k for k in keys)
        assert any("not_run" in k for k in keys)

    def test_正常stats_0_issues(self, tmp_path: Path):
        path = tmp_path / "trajectory_stats.json"
        path.write_text(
            json.dumps({"scored": 3, "undetermined": 1, "not_run": 1}),
            encoding="utf-8",
        )
        stats, issues = _read_stats(path)
        assert stats == {"scored": 3, "undetermined": 1, "not_run": 1}
        assert issues == []


# ── TestCheckStatsCount ────────────────────────────────


class TestCheckStatsCount:
    def test_总数吻合_0_issues(self):
        assert _check_stats_count(5, {"scored": 3, "undetermined": 1, "not_run": 1}) == []

    def test_总数不吻合_返error(self):
        # 1 条 record 但 stats 算 5,差 4
        issues = _check_stats_count(1, {"scored": 5, "undetermined": 0, "not_run": 0})
        assert len(issues) == 1
        assert issues[0].code == "stats_count_mismatch"
        assert issues[0].severity == "error"

    def test_反例_stats空不报错(self):
        # stats 空 = 上一步 _read_stats 已经记 missing_stats,这里不该重复报
        assert _check_stats_count(5, {}) == []


# ── TestCheckTaskViews ────────────────────────────────


class TestCheckTaskViews:
    def test_T视图齐全_0_issues(self, tmp_path: Path):
        _write_t_view(tmp_path, "T001__r-1")
        _write_t_view(tmp_path, "T002__r-1")
        recs = [_trajectory("T001__r-1"), _trajectory("T002__r-1")]
        issues = _check_task_views(recs, tmp_path)
        assert issues == []

    def test_T视图缺进warning(self, tmp_path: Path):
        # T001__r-1 不写
        _write_t_view(tmp_path, "T002__r-1")
        recs = [_trajectory("T001__r-1"), _trajectory("T002__r-1")]
        issues = _check_task_views(recs, tmp_path)
        assert len(issues) == 1
        assert issues[0].code == "missing_t_view"
        assert issues[0].trajectory_id == "T001__r-1"
        assert issues[0].severity == "warning"

    def test_T视图坏档进warning(self, tmp_path: Path):
        from trajectory_pipeline.storage import task_views_dir
        tdir = task_views_dir(tmp_path)
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "T001__r-1.json").write_text("{not json", encoding="utf-8")
        recs = [_trajectory("T001__r-1")]
        issues = _check_task_views(recs, tmp_path)
        assert any(i.code == "bad_t_view_json" for i in issues)
        assert issues[0].trajectory_id == "T001__r-1"

    def test_T视图缺三键进warning(self, tmp_path: Path):
        from trajectory_pipeline.storage import task_views_dir
        tdir = task_views_dir(tmp_path)
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "T001__r-1.json").write_text(
            json.dumps({"messages": [], "openai": []}),  # 缺 meta
            encoding="utf-8",
        )
        recs = [_trajectory("T001__r-1")]
        issues = _check_task_views(recs, tmp_path)
        assert any(i.code == "t_view_missing_keys" for i in issues)
        assert "meta" in issues[0].detail

    def test_反例_空trajectory_id跳过(self, tmp_path: Path):
        recs = [{"trajectory_id": "", "task_id": "T", "run_id": "r"}]
        issues = _check_task_views(recs, tmp_path)
        assert issues == []  # 空 id 直接跳,不当 T 视图缺


# ── TestVerifyFromDir ──────────────────────────────────


class TestVerifyFromDir:
    def test_全过(self, tmp_path: Path):
        # jsonl 2 条 + stats 吻合 + T 视图齐
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(_trajectory("T001__r-1"), ensure_ascii=False) + "\n"
            + json.dumps(_trajectory("T002__r-1"), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "trajectory_stats.json").write_text(
            json.dumps({"scored": 2, "undetermined": 0, "not_run": 0}),
            encoding="utf-8",
        )
        _write_t_view(tmp_path, "T001__r-1")
        _write_t_view(tmp_path, "T002__r-1")
        report = verify_from_dir(tmp_path, task_views_root=tmp_path)
        assert report.ok
        assert report.record_count == 2
        assert report.issues == []

    def test_缺jsonl(self, tmp_path: Path):
        # 不写 jsonl;但 stats 仍要检查
        (tmp_path / "trajectory_stats.json").write_text(
            json.dumps({"scored": 0, "undetermined": 0, "not_run": 0}),
            encoding="utf-8",
        )
        report = verify_from_dir(tmp_path)
        assert not report.ok
        assert any(i.code == "missing_jsonl" for i in report.issues)
        # stats 缺 jsonl 时应**不**调 stats / T 视图(避免噪声)
        assert not any(i.code == "missing_stats" for i in report.issues)
        assert not any(i.code == "missing_t_view" for i in report.issues)

    def test_缺stats(self, tmp_path: Path):
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(_trajectory(), ensure_ascii=False) + "\n", encoding="utf-8",
        )
        # 不写 stats
        report = verify_from_dir(tmp_path)
        assert not report.ok
        assert any(i.code == "missing_stats" for i in report.issues)

    def test_stats不一致(self, tmp_path: Path):
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(_trajectory(), ensure_ascii=False) + "\n", encoding="utf-8",
        )
        (tmp_path / "trajectory_stats.json").write_text(
            json.dumps({"scored": 5, "undetermined": 0, "not_run": 0}),
            encoding="utf-8",
        )
        report = verify_from_dir(tmp_path)
        assert not report.ok
        assert any(i.code == "stats_count_mismatch" for i in report.issues)

    def test_T视图缺_但ok(self, tmp_path: Path):
        # T 视图缺是 warning,不影响 ok
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(_trajectory(), ensure_ascii=False) + "\n", encoding="utf-8",
        )
        (tmp_path / "trajectory_stats.json").write_text(
            json.dumps({"scored": 1, "undetermined": 0, "not_run": 0}),
            encoding="utf-8",
        )
        # 不写 T 视图
        report = verify_from_dir(tmp_path)
        assert report.ok  # warning 不影响
        assert any(i.code == "missing_t_view" for i in report.issues)
        assert report.warning_count == 1

    def test_反例_混合error和warning(self, tmp_path: Path):
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(_trajectory(), ensure_ascii=False) + "\n", encoding="utf-8",
        )
        # stats 错(算 5 但只 1 条)+ T 视图缺(算 warning)
        (tmp_path / "trajectory_stats.json").write_text(
            json.dumps({"scored": 5, "undetermined": 0, "not_run": 0}),
            encoding="utf-8",
        )
        report = verify_from_dir(tmp_path)
        assert not report.ok
        assert report.error_count >= 1  # stats_count_mismatch
        assert report.warning_count >= 1  # missing_t_view


# ── TestVerifyReport ────────────────────────────────────


class TestVerifyReport:
    def test_ok_空issues(self):
        r = VerifyReport()
        assert r.ok
        assert r.error_count == 0
        assert r.warning_count == 0
        assert r.by_code == {}

    def test_ok_纯warning(self):
        r = VerifyReport(issues=[
            VerifyIssue(code="missing_t_view", severity="warning",
                        trajectory_id="T1", detail="缺"),
        ])
        assert r.ok  # warning 不影响
        assert r.warning_count == 1
        assert r.error_count == 0

    def test_not_ok_error(self):
        r = VerifyReport(issues=[
            VerifyIssue(code="missing_jsonl", severity="error", detail="缺"),
        ])
        assert not r.ok
        assert r.error_count == 1

    def test_summary(self):
        r = VerifyReport(record_count=5, stats={"scored": 5})
        assert "5 条" in r.summary()
        assert "0 error" in r.summary()
        assert "0 warning" in r.summary()


# ── TestFormatReport ─────────────────────────────────────


class TestFormatReport:
    def test_全过格式(self):
        r = VerifyReport(record_count=3, stats={"scored": 3, "undetermined": 0, "not_run": 0})
        out = format_report(r)
        assert "3 条" in out
        assert "✓ 全部通过" in out
        assert "scored=3" in out

    def test_失败格式_error先排(self):
        r = VerifyReport(record_count=2, issues=[
            VerifyIssue(code="missing_t_view", severity="warning",
                        trajectory_id="T1", detail="缺 T"),
            VerifyIssue(code="missing_jsonl", severity="error", detail="缺 jsonl"),
        ])
        out = format_report(r)
        # error(✗)应先于 warning(⚠)
        err_pos = out.find("missing_jsonl")
        warn_pos = out.find("missing_t_view")
        assert err_pos != -1 and warn_pos != -1
        assert err_pos < warn_pos
        assert "1 error / 1 warning" in out

    def test_无issues_返简洁(self):
        r = VerifyReport(record_count=0, issues=[])
        out = format_report(r)
        assert "0 条" in out
        assert "✓" in out
