"""orchestration.queue.SQLiteQueue 单元测试 (ST-2 重构版).

契约: docs/设计方案/pipeline-contracts.md §2.

每个测试用 tmp_path 下的独立 db 文件, 不污染仓库.
新架构 ``simulation server → gdr → etl`` 下:
    pending → simulate → gdr → etl → done
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orchestration.queue import (
    ALL_PHASES,
    PHASE_DEAD,
    PHASE_DONE,
    PHASE_ETL,
    PHASE_GDR,
    PHASE_PENDING,
    PHASE_SIMULATE,
    STAGE_ETL,
    STAGE_GDR,
    STAGE_SIMULATE,
    SQLiteQueue,
    Task,
    TaskAlreadyTerminal,
    TERMINAL_PHASES,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def queue(tmp_path: Path) -> SQLiteQueue:
    return SQLiteQueue(tmp_path / "queue.db")


# ---------------------------------------------------------------------------
# schema / init
# ---------------------------------------------------------------------------

def test_init_creates_tasks_table_only(tmp_path: Path) -> None:
    """新 schema 只建 tasks 表; 旧 batches / run_tasks 不应存在."""
    db = tmp_path / "q.db"
    SQLiteQueue(db)
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r[0] for r in rows}
        assert "tasks" in names
        assert "batches" not in names
        assert "run_tasks" not in names
    finally:
        conn.close()


def test_init_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    SQLiteQueue(db)
    SQLiteQueue(db)  # 第二次 init 不应报错


def test_init_creates_phase_index(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    SQLiteQueue(db)
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
        names = {r[0] for r in rows}
        assert "idx_tasks_phase" in names
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# context manager
# ---------------------------------------------------------------------------

def test_context_manager_returns_self(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    with SQLiteQueue(db) as q:
        assert isinstance(q, SQLiteQueue)
        assert q.db_path == db


# ---------------------------------------------------------------------------
# upsert_task
# ---------------------------------------------------------------------------

def test_upsert_task_creates_new(queue: SQLiteQueue) -> None:
    t = queue.upsert_task("T001")
    assert t.task_id == "T001"
    assert t.phase == PHASE_PENDING
    assert t.attempts_simulate == 0
    assert t.attempts_gdr == 0
    assert t.attempts_etl == 0
    assert t.run_id is None
    assert t.session_id is None
    assert t.src_path is None
    assert t.error_msg is None


def test_upsert_task_idempotent_for_non_terminal(queue: SQLiteQueue) -> None:
    """非终态 task 重复 upsert: 重置 phase + 清错误 + 清产物路径."""
    queue.upsert_task("T001")
    queue.mark_phase("T001", new_phase=PHASE_SIMULATE, run_id="r1", session_id="s1")
    t = queue.upsert_task("T001")
    assert t.phase == PHASE_PENDING
    assert t.run_id is None
    assert t.session_id is None


def test_upsert_task_explicit_phase(queue: SQLiteQueue) -> None:
    t = queue.upsert_task("T001", phase=PHASE_SIMULATE)
    assert t.phase == PHASE_SIMULATE


def test_upsert_task_terminal_done_raises(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.mark_phase("T001", new_phase=PHASE_DONE)
    with pytest.raises(TaskAlreadyTerminal) as exc_info:
        queue.upsert_task("T001")
    assert exc_info.value.task_id == "T001"
    assert exc_info.value.current_phase == PHASE_DONE


def test_upsert_task_terminal_dead_raises(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.mark_failed("T001", stage=STAGE_GDR, error_msg="boom")
    with pytest.raises(TaskAlreadyTerminal) as exc_info:
        queue.upsert_task("T001")
    assert exc_info.value.current_phase == PHASE_DEAD


def test_upsert_after_requeue_dead_succeeds(queue: SQLiteQueue) -> None:
    """requeue_dead 后 task 复活, 此时 upsert_task 不再报 TaskAlreadyTerminal."""
    queue.upsert_task("T001")
    queue.mark_failed("T001", stage=STAGE_GDR, error_msg="boom")
    assert queue.requeue_dead() == 1
    t = queue.upsert_task("T001")
    assert t.phase == PHASE_PENDING


def test_upsert_task_invalid_phase_raises(queue: SQLiteQueue) -> None:
    with pytest.raises(ValueError):
        queue.upsert_task("T001", phase="unknown")


# ---------------------------------------------------------------------------
# mark_phase
# ---------------------------------------------------------------------------

def test_mark_phase_basic_transition(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.mark_phase("T001", new_phase=PHASE_SIMULATE)
    t = queue.get_task("T001")
    assert t is not None
    assert t.phase == PHASE_SIMULATE


def test_mark_phase_writes_run_and_session(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.mark_phase(
        "T001",
        new_phase=PHASE_GDR,
        run_id="run-123",
        session_id="sess-456",
        src_path=Path("output/agent_trajectory/run-123__sess-456.json"),
    )
    t = queue.get_task("T001")
    assert t is not None
    assert t.phase == PHASE_GDR
    assert t.run_id == "run-123"
    assert t.session_id == "sess-456"
    assert t.src_path == Path("output/agent_trajectory/run-123__sess-456.json")


def test_mark_phase_writes_gdr_refined(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.mark_phase(
        "T001",
        new_phase=PHASE_ETL,
        gdr_refined_path=Path("output/refined/T001__s.json"),
    )
    t = queue.get_task("T001")
    assert t is not None
    assert t.phase == PHASE_ETL
    assert t.gdr_refined_path == Path("output/refined/T001__s.json")


def test_mark_phase_writes_etl_outputs(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.mark_phase(
        "T001",
        new_phase=PHASE_DONE,
        etl_messages_path=Path("o/m.json"),
        etl_openai_path=Path("o/o.json"),
        etl_qwenjina_path=None,
        etl_meta_path=Path("o/meta.json"),
    )
    t = queue.get_task("T001")
    assert t is not None
    assert t.phase == PHASE_DONE
    assert t.etl_messages_path == Path("o/m.json")
    assert t.etl_openai_path == Path("o/o.json")
    assert t.etl_qwenjina_path is None
    assert t.etl_meta_path == Path("o/meta.json")


def test_mark_phase_invalid_raises(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    with pytest.raises(ValueError):
        queue.mark_phase("T001", new_phase="bogus")


def test_mark_phase_partial_does_not_clobber(queue: SQLiteQueue) -> None:
    """mark_phase 不传 run_id 时不应覆盖已有 run_id."""
    queue.upsert_task("T001")
    queue.mark_phase("T001", new_phase=PHASE_GDR, run_id="r1", session_id="s1")
    queue.mark_phase("T001", new_phase=PHASE_ETL)  # 不传 run_id
    t = queue.get_task("T001")
    assert t is not None
    assert t.run_id == "r1"
    assert t.session_id == "s1"


# ---------------------------------------------------------------------------
# increment_attempts
# ---------------------------------------------------------------------------

def test_increment_attempts_simulate(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    n = queue.increment_attempts("T001", stage=STAGE_SIMULATE)
    assert n == 1
    n2 = queue.increment_attempts("T001", stage=STAGE_SIMULATE)
    assert n2 == 2
    t = queue.get_task("T001")
    assert t is not None
    assert t.attempts_simulate == 2


def test_increment_attempts_gdr(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    assert queue.increment_attempts("T001", stage=STAGE_GDR) == 1
    t = queue.get_task("T001")
    assert t is not None
    assert t.attempts_gdr == 1


def test_increment_attempts_etl(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    assert queue.increment_attempts("T001", stage=STAGE_ETL) == 1
    t = queue.get_task("T001")
    assert t is not None
    assert t.attempts_etl == 1


def test_increment_attempts_independent_counters(queue: SQLiteQueue) -> None:
    """simulate/gdr/etl 三个 attempts 计数器独立递增."""
    queue.upsert_task("T001")
    queue.increment_attempts("T001", stage=STAGE_SIMULATE)
    queue.increment_attempts("T001", stage=STAGE_GDR)
    queue.increment_attempts("T001", stage=STAGE_GDR)
    queue.increment_attempts("T001", stage=STAGE_ETL)
    queue.increment_attempts("T001", stage=STAGE_ETL)
    queue.increment_attempts("T001", stage=STAGE_ETL)
    t = queue.get_task("T001")
    assert t is not None
    assert t.attempts_simulate == 1
    assert t.attempts_gdr == 2
    assert t.attempts_etl == 3


def test_increment_attempts_invalid_stage_raises(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    with pytest.raises(ValueError, match="invalid stage"):
        queue.increment_attempts("T001", stage="qf")


def test_increment_attempts_unknown_task_raises(queue: SQLiteQueue) -> None:
    with pytest.raises(KeyError):
        queue.increment_attempts("UNKNOWN", stage=STAGE_GDR)


# ---------------------------------------------------------------------------
# mark_failed
# ---------------------------------------------------------------------------

def test_mark_failed_sets_dead_phase_and_error(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.mark_failed("T001", stage=STAGE_GDR, error_msg="boom")
    t = queue.get_task("T001")
    assert t is not None
    assert t.phase == PHASE_DEAD
    assert t.error_msg == "boom"


def test_mark_failed_invalid_stage_raises(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    with pytest.raises(ValueError, match="invalid stage"):
        queue.mark_failed("T001", stage="qf", error_msg="x")


# ---------------------------------------------------------------------------
# requeue_dead
# ---------------------------------------------------------------------------

def test_requeue_dead_returns_count(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.upsert_task("T002")
    queue.mark_failed("T001", stage=STAGE_GDR, error_msg="a")
    queue.mark_failed("T002", stage=STAGE_GDR, error_msg="b")
    n = queue.requeue_dead()
    assert n == 2
    counts = queue.count_by_phase()
    assert counts[PHASE_PENDING] == 2
    assert counts[PHASE_DEAD] == 0


def test_requeue_dead_clears_error_and_paths(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.mark_phase("T001", new_phase=PHASE_GDR, run_id="r1", session_id="s1")
    queue.mark_failed("T001", stage=STAGE_GDR, error_msg="boom")
    queue.requeue_dead()
    t = queue.get_task("T001")
    assert t is not None
    assert t.phase == PHASE_PENDING
    assert t.error_msg is None
    assert t.run_id is None
    assert t.session_id is None


def test_requeue_dead_only_affects_dead(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.upsert_task("T002")
    queue.mark_phase("T002", new_phase=PHASE_DONE)
    queue.mark_failed("T001", stage=STAGE_GDR, error_msg="x")
    n = queue.requeue_dead()
    assert n == 1
    t_done = queue.get_task("T002")
    assert t_done is not None
    assert t_done.phase == PHASE_DONE


# ---------------------------------------------------------------------------
# get_task / list_tasks
# ---------------------------------------------------------------------------

def test_get_task_missing_returns_none(queue: SQLiteQueue) -> None:
    assert queue.get_task("UNKNOWN") is None


def test_list_tasks_all_by_default(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.upsert_task("T002")
    queue.upsert_task("T003")
    tasks = queue.list_tasks()
    assert len(tasks) == 3
    assert [t.task_id for t in tasks] == ["T001", "T002", "T003"]


def test_list_tasks_filter_by_phase(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.upsert_task("T002")
    queue.mark_phase("T002", new_phase=PHASE_SIMULATE)
    queue.upsert_task("T003")
    queue.mark_failed("T003", stage=STAGE_SIMULATE, error_msg="x")

    pending = queue.list_tasks(phase=PHASE_PENDING)
    simulate = queue.list_tasks(phase=PHASE_SIMULATE)
    dead = queue.list_tasks(phase=PHASE_DEAD)

    assert [t.task_id for t in pending] == ["T001"]
    assert [t.task_id for t in simulate] == ["T002"]
    assert [t.task_id for t in dead] == ["T003"]


def test_list_tasks_invalid_phase_raises(queue: SQLiteQueue) -> None:
    with pytest.raises(ValueError):
        queue.list_tasks(phase="bogus")


def test_list_tasks_with_limit(queue: SQLiteQueue) -> None:
    for tid in [f"T{i:03d}" for i in range(5)]:
        queue.upsert_task(tid)
    out = queue.list_tasks(limit=2)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# count_by_phase
# ---------------------------------------------------------------------------

def test_count_by_phase_empty(queue: SQLiteQueue) -> None:
    counts = queue.count_by_phase()
    # 所有 phase 默认 0
    assert counts == {p: 0 for p in ALL_PHASES}


def test_count_by_phase_mixed(queue: SQLiteQueue) -> None:
    queue.upsert_task("T001")
    queue.upsert_task("T002")
    queue.upsert_task("T003")
    queue.upsert_task("T004")

    queue.mark_phase("T001", new_phase=PHASE_SIMULATE)
    queue.mark_phase("T002", new_phase=PHASE_GDR)
    queue.mark_phase("T003", new_phase=PHASE_DONE)
    queue.mark_failed("T004", stage=STAGE_SIMULATE, error_msg="x")

    counts = queue.count_by_phase()
    assert counts[PHASE_PENDING] == 0
    assert counts[PHASE_SIMULATE] == 1
    assert counts[PHASE_GDR] == 1
    assert counts[PHASE_ETL] == 0
    assert counts[PHASE_DONE] == 1
    assert counts[PHASE_DEAD] == 1


# ---------------------------------------------------------------------------
# terminal phases & exception
# ---------------------------------------------------------------------------

def test_terminal_phases_set() -> None:
    """契约 §2.3: TERMINAL_PHASES = {done, dead}."""
    assert TERMINAL_PHASES == frozenset({PHASE_DONE, PHASE_DEAD})


def test_task_already_terminal_attributes() -> None:
    exc = TaskAlreadyTerminal("T001", PHASE_DONE)
    assert exc.task_id == "T001"
    assert exc.current_phase == PHASE_DONE
    assert "T001" in str(exc)


# ---------------------------------------------------------------------------
# 持久化 / 多实例并发安全
# ---------------------------------------------------------------------------

def test_changes_visible_across_instances(tmp_path: Path) -> None:
    """同一 db 文件的两个 SQLiteQueue 实例应共享状态 (契约 §2.7 子进程模型)."""
    db = tmp_path / "shared.db"
    q1 = SQLiteQueue(db)
    q1.upsert_task("T001")
    q1.mark_phase("T001", new_phase=PHASE_SIMULATE, run_id="r1")

    q2 = SQLiteQueue(db)
    t = q2.get_task("T001")
    assert t is not None
    assert t.phase == PHASE_SIMULATE
    assert t.run_id == "r1"


def test_db_path_exposed(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    q = SQLiteQueue(db)
    assert q.db_path == db
