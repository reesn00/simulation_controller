"""orchestration.failure_handler 单元测试."""

from __future__ import annotations

import json
from pathlib import Path

from orchestration.failure_handler import reap_dead
from orchestration.queue import SQLiteQueue


def _force_dead(queue: SQLiteQueue, src_path: Path, *, qf_output: Path | None = None,
                gdr_output: Path | None = None) -> int:
    """登记 task 并通过 qf→gdr→手动 mark_dead 走到 dead 状态."""
    tid, _ = queue.insert(src_path=src_path, run_id="r", session_id="s", batch_id=1)
    queue.pull_pending_qf(worker_id="w", n=1)
    if qf_output is not None:
        qf_output.write_text("{}", encoding="utf-8")
        queue.mark_qf_done(tid, qf_output_path=qf_output)
        queue.pull_pending_gdr(worker_id="w", n=1)
        if gdr_output is not None:
            queue.mark_gdr_done(tid, gdr_output_path=gdr_output)
    queue.mark_dead(tid, error_msg="forced")
    return tid


def test_reap_dead_moves_src(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    _force_dead(queue, src)

    dead_dir = tmp_path / "dead"
    archives = reap_dead(queue, dead_dir=dead_dir)
    assert len(archives) == 1
    assert archives[0].moved_to  # 至少移了 1 个
    moved = archives[0].moved_to[0]
    assert moved.startswith(str(dead_dir))
    assert "raw.json" in moved
    assert not src.exists()
    assert Path(moved).is_file()


def test_reap_dead_moves_src_and_qf_output(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    qf = tmp_path / "qf.json"
    _force_dead(queue, src, qf_output=qf)
    # qf 存在 → 移
    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert len(archives[0].moved_to) == 2
    assert not src.exists()
    assert not qf.exists()


def test_reap_dead_missing_src_no_crash(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    _force_dead(queue, src)
    src.unlink()  # 模拟源文件已丢失

    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert len(archives) == 1
    assert archives[0].moved_to == []  # 没移成


def test_reap_dead_appends_log(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    _force_dead(queue, src)

    log = tmp_path / "dead.log"
    reap_dead(queue, dead_dir=tmp_path / "dead", dead_log_path=log)
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert "task_id" in entry
    assert entry["attempts_qf"] >= 0
    assert "moved_to" in entry


def test_reap_dead_ignores_non_dead(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    src = tmp_path / "raw.json"
    src.write_text("{}", encoding="utf-8")
    queue.insert(src_path=src, run_id="r", session_id="s", batch_id=1)
    # state = pending，没走到 dead

    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert archives == []
    assert src.exists()  # 没动


def test_reap_dead_empty_when_no_dead(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    archives = reap_dead(queue, dead_dir=tmp_path / "dead")
    assert archives == []
