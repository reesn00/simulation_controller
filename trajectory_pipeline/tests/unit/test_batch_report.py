"""批次级断言的**生产落盘**（L-23）守门测试。

设计来源：``docs/设计方案/24-rubric对齐优化方案.md`` §2.1（读数落盘 +
闸门第五条判据）。本文件盯四件事：

1. **真的接了生产链**：``agreement --version N --emit-disagreements``
   会在 ``v{N}/`` 下落 ``batch_asserts.json``（与 ``snapshot.json`` 同目录、
   同版本号绑定）；
2. **按 run 归集**：一批 = 一个 run，``runs[]`` 一个 run 一条；
3. **读不到就测不成**：缺 P1 存档的 run，B-1 / B-2 / B-4 记
   ``STATUS_UNTESTABLE``——**不是不通过，更不是通过**；B-5 不依赖存档，
   照常出读数；
4. **B-5 只读调参池**：持有集 / 回归集是封存面，不进分母。
"""
from __future__ import annotations

import json
import pathlib

import pytest

from trajectory_pipeline.evaluation.batch import asserts
from trajectory_pipeline.evaluation.batch import report as batch_report
from trajectory_pipeline.evaluation.golden import builder


# ── 夹具 ──────────────────────────────────────────────────────────


def _row(
    gid: str,
    *,
    task_id: str = "T001",
    run_id: str = "abc12345",
    gold: str = "success",
    branch: str | None = None,
    split: str = "calibration",
    source_kind: str = "archive_outcome",
) -> dict:
    return {
        "golden_id": gid,
        "source_kind": source_kind,
        "task_id": task_id,
        "run_id": run_id,
        "url": f"https://{gid}.test/",
        "site_url": f"https://{gid}.test/",
        "__split__": split,
        "code": {"branch": branch, "evidence": "e"},
        "golden": {"label": gold, "note": "", "dimension_scores": {}},
        "dimension_scores": {},
        "scores_detail": [],
    }


def _archive(*, task_id: str = "T001") -> dict:
    """一份能判的最小 P1 存档：B-1 有一个合规动作，B-2 有 1/1 处置。"""
    return {
        "task_id": task_id,
        "run_config": {"max_candidates": 2},
        "candidates": [{"url": "https://a.test/"}],
        "outcomes": [{"url": "https://a.test/", "branch": "not_play_site"}],
        "visits": [],
        "steps": [
            {
                "action": {
                    "tool": "goto",
                    "params": {"url": "https://a.test/"},
                    "target": None,
                }
            }
        ],
    }


def _reader(archives: dict[tuple[str, str], dict | None]):
    def read(task_id: str, run_id: str):
        key = (task_id, run_id)
        if key not in archives:
            return None, f"没有 {task_id}__{run_id}.json"
        return archives[key], ""

    return read


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _setup_golden(tmp_path: pathlib.Path, *, version: int = 3) -> pathlib.Path:
    golden_root = tmp_path / "golden"
    vdir = golden_root / f"v{version}"
    _write_jsonl(vdir / "calibration.jsonl", [
        _row("s1__abc12345__h", gold="success", branch=None),
    ])
    _write_jsonl(vdir / "holdout.jsonl", [])
    (vdir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "label_schema_version": "2",
                "version": version,
                "frozen_at": "2026-10-12T00:00:00+00:00",
                "seed": 42,
                "counts": {"calibration": 1, "holdout": 0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return golden_root


# ── 按 run 归集 ────────────────────────────────────────────────────


def test_一个run一条_批次绑定版本():
    rows = [
        _row("g1", task_id="T001", run_id="r1"),
        _row("g2", task_id="T001", run_id="r1"),
        _row("g3", task_id="T002", run_id="r2"),
    ]
    archives = {
        ("T001", "r1"): _archive(task_id="T001"),
        ("T002", "r2"): _archive(task_id="T002"),
    }
    report = batch_report.build_batch_asserts(
        rows, version=16, p2_root="unused",
        archive_reader=_reader(archives), generated_at="2026-11-09T00:00:00+00:00",
    )
    assert report["schema_version"] == batch_report.SCHEMA_VERSION
    assert report["golden_version"] == 16
    assert report["batch"] == {"unit": "run", "run_count": 2}
    assert [r["run_id"] for r in report["runs"]] == ["r1", "r2"]
    assert report["summary"]["run_count"] == 2
    assert report["summary"]["failed_runs"] == []
    assert report["summary"]["missing_archive_runs"] == []


def test_缺run标识直接报错_不猜():
    with pytest.raises(batch_report.BatchReportError, match="task_id / run_id"):
        batch_report.build_batch_asserts(
            [{"golden_id": "x", "task_id": "T001", "run_id": ""}],
            version=1, p2_root="unused", archive_reader=_reader({}),
        )


# ── 读不到存档 = 测不成 ───────────────────────────────────────────


def test_缺存档_三条断言测不成_不是不通过也不是通过():
    rows = [_row("g1", gold="success", branch=None)]
    report = batch_report.build_batch_asserts(
        rows, version=3, p2_root="nowhere",
        archive_reader=_reader({}),  # 全部读不到
    )
    run = report["runs"][0]
    assert run["archive_found"] is False
    assert "没有 T001__abc12345.json" in run["archive_note"]
    by_clause = {r["clause"]: r for r in run["readings"]}
    for clause in ("B-1", "B-2", "B-4"):
        assert by_clause[clause]["status"] == asserts.STATUS_UNTESTABLE, clause
        assert "存档" in by_clause[clause]["reading"] or "读不了" in by_clause[clause]["reading"]
    # B-5 只吃金标行，照常出读数（空 branch = success，与 gold 一致）
    assert by_clause["B-5"]["status"] == asserts.STATUS_READOUT
    assert by_clause["B-5"]["denominator"] == 1
    assert by_clause["B-5"]["numerator"] == 0
    assert report["summary"]["missing_archive_runs"] == ["T001__abc12345"]
    assert report["summary"]["failed_runs"] == []


def test_坏存档_同样测不成():
    rows = [_row("g1")]

    def broken(task_id: str, run_id: str):
        return None, f"{task_id}__{run_id}.json 读不了：顶层不是 JSON 对象"

    report = batch_report.build_batch_asserts(
        rows, version=3, p2_root="x", archive_reader=broken,
    )
    assert report["runs"][0]["archive_found"] is False
    assert "读不了" in report["runs"][0]["archive_note"]


def test_默认读存档_优先人工复核档(tmp_path):
    p2 = tmp_path / "p2"
    p2.mkdir()
    plain = _archive()
    plain["run_config"] = {"max_candidates": 9}
    reviewed = _archive()
    reviewed["run_config"] = {"max_candidates": 2}
    (p2 / "T001__abc12345.json").write_text(
        json.dumps(plain), encoding="utf-8"
    )
    (p2 / "T001__abc12345.reviewed.json").write_text(
        json.dumps(reviewed), encoding="utf-8"
    )
    read = batch_report.default_archive_reader(p2)
    archive, note = read("T001", "abc12345")
    assert note == ""
    assert archive is not None
    assert archive["run_config"]["max_candidates"] == 2  # 复核档赢


# ── B-5 只读调参池 ────────────────────────────────────────────────


def test_B5只读调参池_封存面不进分母():
    calib = _row("g1", gold="success", branch="blocked")  # 不一致
    holdout_bad = _row("h1", gold="success", branch="blocked", split="holdout")
    report = batch_report.build_batch_asserts(
        [calib, holdout_bad], version=3, p2_root="unused",
        archive_reader=_reader({("T001", "abc12345"): _archive()}),
    )
    b5 = {r["clause"]: r for r in report["runs"][0]["readings"]}["B-5"]
    assert b5["status"] == asserts.STATUS_READOUT
    assert b5["denominator"] == 1   # 只有调参池那一行
    assert b5["numerator"] == 1     # 不一致 1 条
    assert report["runs"][0]["row_splits"] == {"calibration": 1, "holdout": 1}


def test_p2_sample行不进B5分母():
    site = _row("g1", gold="success", branch=None)
    unit = _row("u1", gold="success", branch=None, source_kind="p2_sample")
    report = batch_report.build_batch_asserts(
        [site, unit], version=3, p2_root="unused",
        archive_reader=_reader({("T001", "abc12345"): _archive()}),
    )
    b5 = {r["clause"]: r for r in report["runs"][0]["readings"]}["B-5"]
    assert b5["denominator"] == 1


# ── 落盘 / 读盘 ───────────────────────────────────────────────────


def test_写读往返_缺失返回None_结构漂移报错(tmp_path):
    rows = [_row("g1")]
    report = batch_report.build_batch_asserts(
        rows, version=3, p2_root="unused",
        archive_reader=_reader({("T001", "abc12345"): _archive()}),
    )
    path = tmp_path / "v3" / batch_report.BATCH_ASSERTS_FILE
    batch_report.write_batch_asserts(path, report)
    loaded = batch_report.load_batch_asserts(path)
    assert loaded is not None
    assert loaded["golden_version"] == 3

    assert batch_report.load_batch_asserts(tmp_path / "nope.json") is None

    path.write_text(json.dumps({"schema_version": 999, "runs": []}), encoding="utf-8")
    with pytest.raises(batch_report.BatchReportError, match="schema_version"):
        batch_report.load_batch_asserts(path)


def test_汇总行带口径与数():
    report = {
        "summary": {
            "run_count": 2,
            "by_clause": {
                "B-1": {"通过": 1, "不通过": 1, "只出读数": 0, "测不成": 0}
            },
            "failed_runs": ["T001__r1"],
            "missing_archive_runs": [],
        }
    }
    line = batch_report.summary_line(report)
    assert "2 个 run" in line
    assert "B-1 通过 1 / 不通过 1" in line
    assert "整批不通过 1 个 run" in line


# ── 生产链：agreement --emit-disagreements ────────────────────────


def test_agreement落盘batch_asserts_与快照同目录(tmp_path):
    """真的是生产调用方：emit 一次就有 ``v{N}/batch_asserts.json``。"""
    golden_root = _setup_golden(tmp_path, version=3)
    p2_root = tmp_path / "p2"
    p2_root.mkdir()
    (p2_root / "T001__abc12345.json").write_text(
        json.dumps(_archive(), ensure_ascii=False), encoding="utf-8"
    )
    out_root = tmp_path / "calibration"
    rc = builder.main([
        "--golden-root", str(golden_root),
        "agreement", "--version", "3",
        "--emit-disagreements", str(out_root),
        "--p2-root", str(p2_root),
    ])
    assert rc == 0
    vdir = out_root / "v3"
    assert (vdir / "snapshot.json").exists()
    batch_path = vdir / batch_report.BATCH_ASSERTS_FILE
    assert batch_path.exists()
    data = batch_report.load_batch_asserts(batch_path)
    assert data is not None
    assert data["golden_version"] == 3
    assert data["batch"] == {"unit": "run", "run_count": 1}
    assert data["runs"][0]["archive_found"] is True
    assert data["runs"][0]["run_id"] == "abc12345"


def test_批次断言出不来时_不留下半份产物(tmp_path):
    """先算后写：批次断言输入不成立 ⇒ 退 2，**snapshot.json 不落盘**。

    否则那份快照会挡住重跑（M-29 S3′：快照已存在就退 2），一次可修的
    输入错误被固化成「要么加 --force-snapshot、要么换目录」。
    """
    golden_root = _setup_golden(tmp_path, version=3)
    vdir = golden_root / "v3"
    _write_jsonl(vdir / "calibration.jsonl", [{**_row("s1"), "run_id": ""}])
    out_root = tmp_path / "calibration"
    rc = builder.main([
        "--golden-root", str(golden_root),
        "agreement", "--version", "3",
        "--emit-disagreements", str(out_root),
    ])
    assert rc == 2
    assert not (out_root / "v3" / "snapshot.json").exists()


def test_快照已存在时_批次断言同样不静默覆盖(tmp_path):
    """M-29 S3′ 的同一纪律：重跑不重新盖章。"""
    golden_root = _setup_golden(tmp_path, version=3)
    p2_root = tmp_path / "p2"
    p2_root.mkdir()
    (p2_root / "T001__abc12345.json").write_text(
        json.dumps(_archive(), ensure_ascii=False), encoding="utf-8"
    )
    out_root = tmp_path / "calibration"
    argv = [
        "--golden-root", str(golden_root),
        "agreement", "--version", "3",
        "--emit-disagreements", str(out_root),
        "--p2-root", str(p2_root),
    ]
    assert builder.main(argv) == 0
    batch_path = out_root / "v3" / batch_report.BATCH_ASSERTS_FILE
    first = batch_path.read_bytes()
    # 第二次：快照已存在 ⇒ 整条 emit 拒绝（不是安静地只改批次断言）
    assert builder.main(argv) == 2
    assert batch_path.read_bytes() == first