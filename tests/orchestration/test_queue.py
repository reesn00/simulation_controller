"""orchestration.queue.SQLiteQueue 单元测试.

每个测试用 tmp_path 下的独立 db 文件，不污染仓库。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from orchestration.queue import (
    STAGE_GDR,
    STAGE_QF,
    STATE_DEAD,
    STATE_DONE,
    STATE_GDR_PROCESSING,
    STATE_PENDING,
    STATE_PENDING_GDR,
    STATE_QF_PROCESSING,
    SQLiteQueue,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def queue(tmp_path: Path) -> SQLiteQueue:
    return SQLiteQueue(tmp_path / "queue.db", max_retry_qf=2, max_retry_gdr=2)


def _seed(q: SQLiteQueue, n: int, batch_id: int = 1, prefix: str = "traj") -> list[int]:
    ids: list[int] = []
    for i in range(n):
        tid, inserted = q.insert(
            src_path=q._db_path.parent / f"{prefix}_b{batch_id}_{i}.json",  # noqa: SLF001
            run_id=f"run_b{batch_id}_{i:03d}",
            session_id=f"session_b{batch_id}_{i:03d}",
            batch_id=batch_id,
        )
        assert inserted is True
        ids.append(tid)
    return ids


# ---------------------------------------------------------------------------
# schema / init
# ---------------------------------------------------------------------------

def test_init_creates_tables(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    SQLiteQueue(db)
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [r[0] for r in rows]
        assert "tasks" in names
        assert "batches" in names
    finally:
        conn.close()


def test_init_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    SQLiteQueue(db)
    SQLiteQueue(db)  # 第二次 init 不应报错


# ---------------------------------------------------------------------------
# insert
# ---------------------------------------------------------------------------

def test_insert_idempotent_by_src_path(queue: SQLiteQueue) -> None:
    p = queue._db_path.parent / "traj.json"  # noqa: SLF001
    tid1, ins1 = queue.insert(src_path=p, run_id="r1", session_id="s1", batch_id=1)
    tid2, ins2 = queue.insert(src_path=p, run_id="r1", session_id="s1", batch_id=1)
    assert ins1 is True
    assert ins2 is False
    assert tid1 == tid2


# ---------------------------------------------------------------------------
# pull_pending_qf
# ---------------------------------------------------------------------------

def test_pull_pending_qf_returns_pending_only(queue: SQLiteQueue) -> None:
    _seed(queue, 3)
    pulled = queue.pull_pending_qf(worker_id="w1", n=10)
    assert len(pulled) == 3
    assert all(t.state == STATE_QF_PROCESSING for t in pulled)
    assert all(t.locked_by == "w1" for t in pulled)


def test_pull_pending_qf_isolates_by_n(queue: SQLiteQueue) -> None:
    _seed(queue, 5)
    pulled = queue.pull_pending_qf(worker_id="w1", n=2)
    assert len(pulled) == 2
    pulled2 = queue.pull_pending_qf(worker_id="w1", n=10)
    assert len(pulled2) == 3


def test_two_workers_no_double_claim(queue: SQLiteQueue) -> None:
    """两个 worker 各自 pull 应拿到不同的 task."""
    _seed(queue, 6)
    a = queue.pull_pending_qf(worker_id="wA", n=3)
    b = queue.pull_pending_qf(worker_id="wB", n=3)
    assert len(a) == 3
    assert len(b) == 3
    ids_a = {t.id for t in a}
    ids_b = {t.id for t in b}
    assert ids_a.isdisjoint(ids_b)


def test_two_workers_concurrent_no_double_claim(tmp_path: Path) -> None:
    """跨线程并发 pull 也不应重复领取（SQLite 写串行保证）."""
    q = SQLiteQueue(tmp_path / "q.db", max_retry_qf=5, max_retry_gdr=5)
    _seed(q, 20)
    results: dict[str, list[int]] = {"A": [], "B": []}

    def worker(name: str) -> None:
        for _ in range(5):
            pulled = q.pull_pending_qf(worker_id=name, n=2)
            results[name].extend(t.id for t in pulled)
            time.sleep(0.01)

    ta = threading.Thread(target=worker, args=("A",))
    tb = threading.Thread(target=worker, args=("B",))
    ta.start(); tb.start()
    ta.join(); tb.join()

    ids_a = set(results["A"])
    ids_b = set(results["B"])
    assert ids_a.isdisjoint(ids_b)
    assert len(ids_a | ids_b) == 20


# ---------------------------------------------------------------------------
# mark_qf_done
# ---------------------------------------------------------------------------

def test_mark_qf_done_transitions_to_pending_gdr(queue: SQLiteQueue) -> None:
    _seed(queue, 1)
    [task] = queue.pull_pending_qf(worker_id="w", n=1)
    queue.mark_qf_done(task.id, qf_output_path=task.src_path.parent / "out.json")
    refreshed = queue.get(task.id)
    assert refreshed is not None
    assert refreshed.state == STATE_PENDING_GDR
    assert refreshed.qf_output_path is not None
    assert refreshed.locked_by is None


# ---------------------------------------------------------------------------
# pull_pending_gdr
# ---------------------------------------------------------------------------

def test_pull_pending_gdr_only_after_qf_done(queue: SQLiteQueue) -> None:
    _seed(queue, 2)
    pending_only = queue.pull_pending_gdr(worker_id="w", n=10)
    assert pending_only == []

    pulled = queue.pull_pending_qf(worker_id="w", n=2)
    for t in pulled:
        queue.mark_qf_done(t.id, qf_output_path=t.src_path.parent / "out.json")

    gdr_pending = queue.pull_pending_gdr(worker_id="w", n=10)
    assert len(gdr_pending) == 2
    assert all(t.state == STATE_GDR_PROCESSING for t in gdr_pending)


# ---------------------------------------------------------------------------
# mark_gdr_done
# ---------------------------------------------------------------------------

def test_mark_gdr_done_transitions_to_done(queue: SQLiteQueue) -> None:
    _seed(queue, 1)
    [qf] = queue.pull_pending_qf(worker_id="w", n=1)
    queue.mark_qf_done(qf.id, qf_output_path=qf.src_path.parent / "qf.json")
    [gdr] = queue.pull_pending_gdr(worker_id="w", n=1)
    queue.mark_gdr_done(gdr.id, gdr_output_path=gdr.src_path.parent / "refined.json")

    refreshed = queue.get(qf.id)
    assert refreshed is not None
    assert refreshed.state == STATE_DONE


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------

def test_mark_failed_qf_increments_and_keeps_pending(queue: SQLiteQueue) -> None:
    _seed(queue, 1)
    [task] = queue.pull_pending_qf(worker_id="w", n=1)
    new_state = queue.mark_failed(task.id, stage=STAGE_QF, error_msg="boom")
    assert new_state == STATE_PENDING
    refreshed = queue.get(task.id)
    assert refreshed is not None
    assert refreshed.state == STATE_PENDING
    assert refreshed.attempts_qf == 1
    assert refreshed.error_msg == "boom"


def test_mark_failed_gdr_increments_and_keeps_pending_gdr(queue: SQLiteQueue) -> None:
    _seed(queue, 1)
    [qf] = queue.pull_pending_qf(worker_id="w", n=1)
    queue.mark_qf_done(qf.id, qf_output_path=qf.src_path.parent / "qf.json")
    [gdr] = queue.pull_pending_gdr(worker_id="w", n=1)
    new_state = queue.mark_failed(gdr.id, stage=STAGE_GDR, error_msg="oops")
    assert new_state == STATE_PENDING_GDR
    refreshed = queue.get(gdr.id)
    assert refreshed is not None
    assert refreshed.state == STATE_PENDING_GDR
    assert refreshed.attempts_gdr == 1
    assert refreshed.error_msg == "oops"


def test_mark_failed_dead_after_max_retry(queue: SQLiteQueue) -> None:
    """max_retry_qf=2：第 3 次失败应进 dead."""
    _seed(queue, 1)
    [task] = queue.pull_pending_qf(worker_id="w", n=1)

    s1 = queue.mark_failed(task.id, stage=STAGE_QF, error_msg="e1")
    s2 = queue.mark_failed(queue.get(task.id).id, stage=STAGE_QF, error_msg="e2")  # noqa: SLF001
    s3 = queue.mark_failed(queue.get(task.id).id, stage=STAGE_QF, error_msg="e3")  # noqa: SLF001
    # 第二次失败时 attempts=2 == max(2)，>max 才 dead；attempts 第三次 = 3 > 2
    assert s1 == STATE_PENDING
    assert s2 == STATE_PENDING  # attempts=2 == max, 不算超
    assert s3 == STATE_DEAD     # attempts=3 > max=2

    refreshed = queue.get(task.id)
    assert refreshed is not None
    assert refreshed.state == STATE_DEAD
    assert refreshed.attempts_qf == 3


def test_mark_failed_unknown_stage_raises(queue: SQLiteQueue) -> None:
    _seed(queue, 1)
    [task] = queue.pull_pending_qf(worker_id="w", n=1)
    with pytest.raises(ValueError):
        queue.mark_failed(task.id, stage="unknown", error_msg="x")


# ---------------------------------------------------------------------------
# mark_dead
# ---------------------------------------------------------------------------

def test_mark_dead_forces_dead(queue: SQLiteQueue) -> None:
    _seed(queue, 1)
    [task] = queue.pull_pending_qf(worker_id="w", n=1)
    queue.mark_dead(task.id, error_msg="poison")
    refreshed = queue.get(task.id)
    assert refreshed is not None
    assert refreshed.state == STATE_DEAD
    assert refreshed.attempts_qf == 0  # 不计入 attempts


# ---------------------------------------------------------------------------
# reap_stale
# ---------------------------------------------------------------------------

def test_reap_stale_returns_qf_processing_to_pending(tmp_path: Path) -> None:
    """手动把 locked_at 写到过去，再 reap_stale 应退回."""
    q = SQLiteQueue(tmp_path / "q.db")
    _seed(q, 2)
    q.pull_pending_qf(worker_id="w", n=2)

    # 把 locked_at 改到 1 小时前
    conn = sqlite3.connect(q._db_path)  # noqa: SLF001
    old = "2020-01-01T00:00:00.000000Z"
    conn.execute("UPDATE tasks SET locked_at = ?", (old,))
    conn.commit()
    conn.close()

    n = q.reap_stale(older_than_seconds=60)
    assert n == 2

    counts = q.count_by_state()
    assert counts.get(STATE_PENDING) == 2
    assert counts.get(STATE_QF_PROCESSING, 0) == 0


def test_reap_stale_keeps_recent_locks(queue: SQLiteQueue) -> None:
    _seed(queue, 1)
    queue.pull_pending_qf(worker_id="w", n=1)
    n = queue.reap_stale(older_than_seconds=3600)
    assert n == 0
    counts = queue.count_by_state()
    assert counts.get(STATE_QF_PROCESSING) == 1


# ---------------------------------------------------------------------------
# count
# ---------------------------------------------------------------------------

def test_count_by_state_mixed(queue: SQLiteQueue) -> None:
    _seed(queue, 4)
    qf_pulled = queue.pull_pending_qf(worker_id="w", n=2)
    for t in qf_pulled:
        queue.mark_qf_done(t.id, qf_output_path=t.src_path.parent / "qf.json")
    queue.pull_pending_gdr(worker_id="w", n=1)
    counts = queue.count_by_state()
    assert counts.get(STATE_PENDING, 0) == 2
    assert counts.get(STATE_QF_PROCESSING, 0) == 0
    assert counts.get(STATE_PENDING_GDR, 0) == 1
    assert counts.get(STATE_GDR_PROCESSING, 0) == 1


def test_count_pending_gdr_and_qf(queue: SQLiteQueue) -> None:
    _seed(queue, 5)
    qf = queue.pull_pending_qf(worker_id="w", n=3)
    for t in qf:
        queue.mark_qf_done(t.id, qf_output_path=t.src_path.parent / "qf.json")
    assert queue.count_pending_qf() == 2
    assert queue.count_pending_gdr() == 3


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------

def test_insert_batch_and_update(queue: SQLiteQueue) -> None:
    _seed(queue, 2)
    bid = queue.insert_batch(["run_000", "run_001"])
    assert isinstance(bid, int)
    queue.update_batch(bid, status="simulate_done",
                       simulate_started_at="2026-09-01T10:00:00.000000Z",
                       simulate_done_at="2026-09-01T10:05:00.000000Z")
    conn = sqlite3.connect(queue._db_path)  # noqa: SLF001
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (bid,)).fetchone()
    conn.close()
    assert row is not None
    assert row["status"] == "simulate_done"
    assert row["simulate_done_at"] == "2026-09-01T10:05:00.000000Z"


# ---------------------------------------------------------------------------
# list_tasks_for_batch
# ---------------------------------------------------------------------------

def test_list_tasks_for_batch(queue: SQLiteQueue) -> None:
    _seed(queue, 3, batch_id=7)
    _seed(queue, 2, batch_id=8)
    tasks = queue.list_tasks_for_batch(7)
    assert len(tasks) == 3
    assert all(t.batch_id == 7 for t in tasks)