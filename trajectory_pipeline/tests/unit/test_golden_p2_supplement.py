"""M-23：P2 补充件不得因为「这个 Run 已有轨迹行」而被扔掉。

per-Run 之后 ``sample_id == 轨迹 id``，而只要 Run 有 P1 存档，候选集里就
已经有同 id 的轨迹行。老写法 ``if sample_id in existing: continue`` 于是
把**有存档的 Run 的 P2 补充件全部跳过**——评分 / 源存档 / persona 静默
缺失，候选集与报表上都看不出来。

反向也要守住：同一轨迹同时有 per-Run 与 per-unit 两份样例时，per-Run
（``sample_id == 轨迹 id``）是权威件，不能被后到的 per-unit 覆盖。
"""
from __future__ import annotations

import json
import pathlib

from trajectory_pipeline.evaluation.golden import builder


def _write_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _archive(root: pathlib.Path, task_id: str, run_id: str) -> None:
    """一份最小 P1 存档：能被 ``_scan_archives`` 认成一条轨迹行。"""
    _write_json(
        root / f"{task_id}__{run_id}.json",
        {
            "task_id": task_id,
            "run_id": run_id,
            "driver": "",
            "candidates": [],
            "outcomes": [],
            "visits": [],
            "steps": [],
        },
    )


def _sample(
    root: pathlib.Path,
    filename: str,
    *,
    sample_id: str,
    task_id: str,
    run_id: str,
    source_archive: str,
) -> None:
    _write_json(
        root / "samples" / filename,
        {
            "sample_id": sample_id,
            "task_id": task_id,
            "run_id": run_id,
            "source_archive": source_archive,
            "six": {"provenance": {}},
        },
    )


def _candidates(tmp_path: pathlib.Path) -> list[dict]:
    root = tmp_path / "pipeline"
    golden_root = tmp_path / "golden"
    rc = builder.main([
        "--root", str(root),
        "--golden-root", str(golden_root),
        "candidates",
    ])
    assert rc == 0
    path = golden_root / builder.CANDIDATES_FILE
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_有P1存档的run仍合并P2补充件(tmp_path):
    """M-23 的失败场景：``sample_id == 轨迹 id`` 被当成「重复行」跳过。"""
    root = tmp_path / "pipeline"
    _archive(root, "T001", "a1b2c3d4")
    _sample(
        root, "T001__a1b2c3d4.json",
        sample_id="T001__a1b2c3d4",
        task_id="T001", run_id="a1b2c3d4",
        source_archive="T001__a1b2c3d4.json",
    )

    rows = _candidates(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["golden_id"] == "T001__a1b2c3d4"
    assert row["sample_id"] == "T001__a1b2c3d4"
    assert row["source_archive"] == "T001__a1b2c3d4.json"


def test_per_unit样例不覆盖per_run权威件(tmp_path):
    """同一轨迹两份样例时，后到的 per-unit 不许把 per-Run 的补充件换掉。"""
    root = tmp_path / "pipeline"
    _archive(root, "T001", "a1b2c3d4")
    _sample(
        root, "T001__a1b2c3d4.json",
        sample_id="T001__a1b2c3d4",
        task_id="T001", run_id="a1b2c3d4",
        source_archive="per-run.json",
    )
    _sample(
        root, "T001__a1b2c3d4__site-1.json",
        sample_id="T001__a1b2c3d4__site-1",
        task_id="T001", run_id="a1b2c3d4",
        source_archive="per-unit.json",
    )

    rows = _candidates(tmp_path)
    assert len(rows) == 1
    assert rows[0]["sample_id"] == "T001__a1b2c3d4"
    assert rows[0]["source_archive"] == "per-run.json"