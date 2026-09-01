"""orchestration.health 单元测试."""

from __future__ import annotations

import json
from pathlib import Path

from orchestration.health import collect_batches, write_health
from orchestration.queue import SQLiteQueue, STATE_DONE, STATE_PENDING


def test_collect_batches_empty(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    assert collect_batches(queue) == {}


def test_collect_batches_returns_inserted(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    fp = tmp_path / "t.json"
    fp.write_text("{}", encoding="utf-8")
    tid, _ = queue.insert(src_path=fp, run_id="r", session_id="s", batch_id=1)
    queue.pull_pending_qf(worker_id="w", n=1)
    queue.mark_qf_done(tid, qf_output_path=fp)
    queue.pull_pending_gdr(worker_id="w", n=1)
    queue.mark_gdr_done(tid, gdr_output_path=fp)

    bid = queue.insert_batch(["r"])
    queue.update_batch(bid, simulate_started_at="2026-09-01T00:00:00Z",
                       simulate_done_at="2026-09-01T00:00:01Z",
                       qf_count=1, gdr_count=1, dead_count=0, status="done")
    batches = collect_batches(queue)
    assert bid in batches
    assert batches[bid]["task_ids"] == ["r"]
    assert batches[bid]["qf_count"] == 1
    assert batches[bid]["gdr_count"] == 1
    assert batches[bid]["dead_count"] == 0
    assert batches[bid]["status"] == "done"


def test_write_health_writes_file(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    out = tmp_path / "health.json"
    payload = write_health(queue, output_path=out)
    assert out.exists()
    assert payload["queue_counts"] == {}  # 空
    assert payload["batches"] == {}
    assert "last_updated" in payload

    data = json.loads(out.read_text(encoding="utf-8"))
    assert "queue_counts" in data
    assert "batches" in data


def test_write_health_includes_extra(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    out = tmp_path / "health.json"
    write_health(queue, output_path=out, extra={"alive_workers": ["qf_0"]})
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["alive_workers"] == ["qf_0"]


def test_write_health_creates_parent_dir(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    out = tmp_path / "deep" / "nested" / "health.json"
    write_health(queue, output_path=out)
    assert out.exists()
