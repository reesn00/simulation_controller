"""orchestration.health 单元测试 (新架构 simulation server → gdr → etl)."""

from __future__ import annotations

import json
from pathlib import Path

from orchestration.health import collect_tasks, write_health
from orchestration.queue import (
    PHASE_DONE,
    PHASE_PENDING,
    SQLiteQueue,
)


def test_collect_tasks_empty(tmp_path: Path) -> None:
    """空 SQLite → phases 全 0, total=0."""
    queue = SQLiteQueue(tmp_path / "q.db")
    result = collect_tasks(queue)
    assert "phases" in result
    assert result["phases"]["pending"] == 0
    assert result["phases"]["simulate"] == 0
    assert result["phases"]["gdr"] == 0
    assert result["phases"]["etl"] == 0
    assert result["phases"]["done"] == 0
    assert result["phases"]["dead"] == 0
    assert result["total"] == 0
    assert "last_updated" in result


def test_collect_tasks_counts_by_phase(tmp_path: Path) -> None:
    """upsert_task → 登记到 pending; mark_phase → 推进 phase."""
    queue = SQLiteQueue(tmp_path / "q.db")

    # 3 task: 1 done, 1 dead, 1 pending
    queue.upsert_task("T_DONE")
    queue.mark_phase("T_DONE", new_phase=PHASE_DONE)

    queue.upsert_task("T_DEAD")
    queue.mark_phase("T_DEAD", new_phase="gdr")
    queue.mark_failed("T_DEAD", stage="gdr", error_msg="oops")

    queue.upsert_task("T_PENDING")

    result = collect_tasks(queue)
    assert result["phases"]["done"] == 1
    assert result["phases"]["dead"] == 1
    assert result["phases"]["pending"] == 1
    assert result["total"] == 3
    assert "last_updated" in result


def test_collect_tasks_initializes_all_phase_keys(tmp_path: Path) -> None:
    """契约 §6.5: phases 必须含 6 个 phase 全字段 (即使计数为 0)."""
    queue = SQLiteQueue(tmp_path / "q.db")
    queue.upsert_task("T1")
    queue.mark_phase("T1", new_phase="simulate")

    result = collect_tasks(queue)
    assert set(result["phases"].keys()) == {
        "pending", "simulate", "gdr", "etl", "done", "dead",
    }


def test_write_health_writes_file(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    out_path = tmp_path / "health.json"
    payload = write_health(queue, log_dir=tmp_path)
    assert out_path.exists()
    assert payload["total"] == 0
    assert "phases" in payload
    assert "last_updated" in payload

    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert "phases" in data
    assert "total" in data
    assert "last_updated" in data
    # 老 batches 字段必须不存在 (契约 §6.5)
    assert "batches" not in data
    assert "queue_counts" not in data


def test_write_health_includes_extra(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    write_health(queue, log_dir=tmp_path, extra={"alive_workers": ["fake"]})
    data = json.loads((tmp_path / "health.json").read_text(encoding="utf-8"))
    assert data["alive_workers"] == ["fake"]


def test_write_health_creates_log_dir(tmp_path: Path) -> None:
    queue = SQLiteQueue(tmp_path / "q.db")
    deep = tmp_path / "deep" / "nested"
    write_health(queue, log_dir=deep)
    assert (deep / "health.json").exists()