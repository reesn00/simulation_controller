"""SQLite 任务队列实现.

每个 ``SQLiteQueue`` 实例 = 一个进程内单例；不同进程共享同一个 db 文件。
SQLite 的 WAL 模式天然支持读并发 + 写串行，因此不需要进程间锁。

抢占（pull）操作使用 ``BEGIN IMMEDIATE`` + ``UPDATE...RETURNING`` 原子地
把最多 N 条目标 state 的行改成 ``*_processing`` 并返回，避免
``SELECT-then-UPDATE`` 的竞态。

详见 ``docs/orchestration-design.md`` §5（schema）和 §6.1-§6.3（算法）。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

STATE_PENDING = "pending"
STATE_QF_PROCESSING = "qf_processing"
STATE_PENDING_GDR = "pending_gdr"
STATE_GDR_PROCESSING = "gdr_processing"
STATE_DONE = "done"
STATE_DEAD = "dead"

STAGE_QF = "qf"
STAGE_GDR = "gdr"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    """从 tasks 表读出的一行，路径字段已转为 Path."""
    id: int
    src_path: Path
    run_id: str
    session_id: str | None
    batch_id: int
    state: str
    attempts_qf: int
    attempts_gdr: int
    qf_output_path: str | None
    gdr_output_path: str | None
    error_msg: str | None
    locked_by: str | None
    locked_at: str | None
    created_at: str
    updated_at: str


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    """UTC ISO8601 字符串（微秒精度，Z 结尾），用作 SQLite TEXT 时间戳."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------

class SQLiteQueue:
    """SQLite-backed 任务队列.

    设计要点：
    * 每个方法开新连接、用 ``BEGIN IMMEDIATE`` 串行化写操作；不需要进程内锁。
    * WAL 模式下多个 reader 可并发，writer 自动排队。
    * 抢占（pull）用 ``UPDATE...RETURNING`` 原子完成。
    * 进程崩溃恢复靠 ``reap_stale`` 把超时 ``locked_at`` 的 ``*_processing``
      退回 ``pending`` / ``pending_gdr``。
    """

    def __init__(
        self,
        db_path: Path,
        *,
        max_retry_qf: int = 3,
        max_retry_gdr: int = 3,
        busy_timeout_ms: int = 30_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._max_retry_qf = int(max_retry_qf)
        self._max_retry_gdr = int(max_retry_gdr)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._init_schema()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self._db_path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,            # autocommit; 我们显式 BEGIN/COMMIT
            check_same_thread=False,         # SQLiteQueue 内部不持有连接，跨线程安全
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
        with self._conn() as conn:
            conn.executescript(ddl)

    # ------------------------------------------------------------------
    # 内部：行转 Task
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            src_path=Path(row["src_path"]),
            run_id=row["run_id"],
            session_id=row["session_id"],
            batch_id=row["batch_id"],
            state=row["state"],
            attempts_qf=row["attempts_qf"],
            attempts_gdr=row["attempts_gdr"],
            qf_output_path=row["qf_output_path"],
            gdr_output_path=row["gdr_output_path"],
            error_msg=row["error_msg"],
            locked_by=row["locked_by"],
            locked_at=row["locked_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # insert / 重复登记
    # ------------------------------------------------------------------

    def insert(
        self,
        *,
        src_path: Path,
        run_id: str,
        session_id: str | None,
        batch_id: int,
    ) -> tuple[int, bool]:
        """登记一个 task；按 ``src_path`` UNIQUE 做幂等.

        Returns: ``(task_id, inserted_now)``。重复登记返回 ``inserted_now=False``。
        """
        with self._conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO tasks (src_path, run_id, session_id, batch_id, state)
                    VALUES (?, ?, ?, ?, 'pending')
                    """,
                    (str(src_path), run_id, session_id, int(batch_id)),
                )
                inserted = True
                task_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                conn.execute("COMMIT")
                return int(task_id), inserted
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                row = conn.execute(
                    "SELECT id FROM tasks WHERE src_path = ?", (str(src_path),)
                ).fetchone()
                assert row is not None  # UNIQUE 违反说明行存在
                return int(row["id"]), False

    # ------------------------------------------------------------------
    # 批量登记：batches 表
    # ------------------------------------------------------------------

    def insert_batch(self, task_ids: list[str]) -> int:
        """在 batches 表登记一批 task 列表，返回 batch_id."""
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO batches (task_ids, status) VALUES (?, 'running')",
                (",".join(task_ids),),
            )
            batch_id = cur.lastrowid
            conn.execute("COMMIT")
            return int(batch_id)

    def update_batch(
        self,
        batch_id: int,
        *,
        status: str | None = None,
        simulate_started_at: str | None = None,
        simulate_done_at: str | None = None,
        qf_count: int | None = None,
        gdr_count: int | None = None,
        dead_count: int | None = None,
    ) -> None:
        """增量更新 batches 行；只覆盖传入字段."""
        sets: list[str] = []
        args: list[object] = []
        if status is not None:
            sets.append("status = ?"); args.append(status)
        if simulate_started_at is not None:
            sets.append("simulate_started_at = ?"); args.append(simulate_started_at)
        if simulate_done_at is not None:
            sets.append("simulate_done_at = ?"); args.append(simulate_done_at)
        if qf_count is not None:
            sets.append("qf_count = ?"); args.append(int(qf_count))
        if gdr_count is not None:
            sets.append("gdr_count = ?"); args.append(int(gdr_count))
        if dead_count is not None:
            sets.append("dead_count = ?"); args.append(int(dead_count))
        if not sets:
            return
        args.append(int(batch_id))
        with self._conn() as conn:
            conn.execute(f"UPDATE batches SET {', '.join(sets)} WHERE id = ?", args)

    # ------------------------------------------------------------------
    # 抢占：pull_pending_qf / pull_pending_gdr
    # ------------------------------------------------------------------

    def _pull_n(
        self,
        *,
        from_state: str,
        to_state: str,
        worker_id: str,
        n: int,
    ) -> list[Task]:
        """原子地把最多 n 条 ``from_state`` 行改成 ``to_state`` 并返回它们."""
        if n <= 0:
            return []
        now = _utc_now_iso()
        with self._conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    """
                    UPDATE tasks
                    SET state = ?,
                        locked_by = ?,
                        locked_at = ?,
                        updated_at = ?
                    WHERE id IN (
                        SELECT id FROM tasks
                        WHERE state = ?
                        ORDER BY id
                        LIMIT ?
                    )
                    RETURNING id, src_path, run_id, session_id, batch_id,
                              state, attempts_qf, attempts_gdr,
                              qf_output_path, gdr_output_path,
                              error_msg, locked_by, locked_at,
                              created_at, updated_at
                    """,
                    (to_state, worker_id, now, now, from_state, int(n)),
                ).fetchall()
                conn.execute("COMMIT")
                return [self._row_to_task(r) for r in rows]
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def pull_pending_qf(self, *, worker_id: str, n: int) -> list[Task]:
        return self._pull_n(
            from_state=STATE_PENDING,
            to_state=STATE_QF_PROCESSING,
            worker_id=worker_id,
            n=n,
        )

    def pull_pending_gdr(self, *, worker_id: str, n: int) -> list[Task]:
        return self._pull_n(
            from_state=STATE_PENDING_GDR,
            to_state=STATE_GDR_PROCESSING,
            worker_id=worker_id,
            n=n,
        )

    # ------------------------------------------------------------------
    # 标记完成 / 失败 / 死信
    # ------------------------------------------------------------------

    def mark_qf_done(self, task_id: int, *, qf_output_path: Path) -> None:
        """qf 处理成功 → state=pending_gdr，等待 gdr 抢占."""
        now = _utc_now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET state = 'pending_gdr',
                    qf_output_path = ?,
                    error_msg = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE id = ? AND state = 'qf_processing'
                """,
                (str(qf_output_path), now, int(task_id)),
            )

    def mark_gdr_done(self, task_id: int, *, gdr_output_path: Path) -> None:
        """gdr 处理成功 → state=done."""
        now = _utc_now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET state = 'done',
                    gdr_output_path = ?,
                    error_msg = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE id = ? AND state = 'gdr_processing'
                """,
                (str(gdr_output_path), now, int(task_id)),
            )

    def mark_failed(self, task_id: int, *, stage: str, error_msg: str) -> str:
        """失败处理：attempts++；超 max → state=dead，否则退回可重试 state.

        Returns: 新的 state（pending / pending_gdr / dead）。
        """
        if stage not in (STAGE_QF, STAGE_GDR):
            raise ValueError(f"unknown stage: {stage!r}")
        now = _utc_now_iso()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT attempts_qf, attempts_gdr, state FROM tasks WHERE id = ?",
                (int(task_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"task {task_id} not found")
            if stage == STAGE_QF:
                new_attempts = int(row["attempts_qf"]) + 1
                max_retry = self._max_retry_qf
                attempts_col = "attempts_qf"
            else:
                new_attempts = int(row["attempts_gdr"]) + 1
                max_retry = self._max_retry_gdr
                attempts_col = "attempts_gdr"

            if new_attempts > max_retry:
                new_state = STATE_DEAD
            elif stage == STAGE_QF:
                new_state = STATE_PENDING
            else:
                new_state = STATE_PENDING_GDR

            conn.execute(
                f"""
                UPDATE tasks
                SET state = ?,
                    {attempts_col} = ?,
                    error_msg = ?,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (new_state, new_attempts, error_msg, now, int(task_id)),
            )
            return new_state

    def mark_dead(self, task_id: int, *, error_msg: str) -> None:
        """强制置 dead（不计入 attempts，用于 watcher 解析失败等场景）."""
        now = _utc_now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET state = 'dead',
                    error_msg = ?,
                    locked_by = NULL,
                    locked_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (error_msg, now, int(task_id)),
            )

    # ------------------------------------------------------------------
    # 崩溃恢复
    # ------------------------------------------------------------------

    def reap_stale(self, *, older_than_seconds: int) -> int:
        """把 ``locked_at`` 超过阈值的 ``*_processing`` 行退回可重试 state.

        用法：master 定时调用；处理崩溃 worker 留下的"幽灵锁"。

        Returns: 被退回的行数。
        """
        # 计算 cutoff：当前时间 - older_than_seconds（按 ISO 字符串字典序比较）
        # 简化做法：拉出所有 processing 行，Python 侧判断是否超时
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, state, locked_at
                FROM tasks
                WHERE state IN ('qf_processing', 'gdr_processing')
                  AND locked_at IS NOT NULL
                """
            ).fetchall()
            now = datetime.now(timezone.utc)
            reap_ids_qf: list[int] = []
            reap_ids_gdr: list[int] = []
            for r in rows:
                try:
                    ts = datetime.strptime(r["locked_at"], "%Y-%m-%dT%H:%M:%S.%fZ")
                    ts = ts.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    continue
                if (now - ts).total_seconds() < older_than_seconds:
                    continue
                if r["state"] == "qf_processing":
                    reap_ids_qf.append(int(r["id"]))
                else:
                    reap_ids_gdr.append(int(r["id"]))

            now_iso = _utc_now_iso()
            if reap_ids_qf:
                conn.execute(
                    f"""
                    UPDATE tasks
                    SET state = 'pending',
                        locked_by = NULL,
                        locked_at = NULL,
                        error_msg = COALESCE(error_msg, 'reaped: stale qf lock'),
                        updated_at = '{now_iso}'
                    WHERE id IN ({','.join('?' * len(reap_ids_qf))})
                    """,
                    reap_ids_qf,
                )
            if reap_ids_gdr:
                conn.execute(
                    f"""
                    UPDATE tasks
                    SET state = 'pending_gdr',
                        locked_by = NULL,
                        locked_at = NULL,
                        error_msg = COALESCE(error_msg, 'reaped: stale gdr lock'),
                        updated_at = '{now_iso}'
                    WHERE id IN ({','.join('?' * len(reap_ids_gdr))})
                    """,
                    reap_ids_gdr,
                )
            return len(reap_ids_qf) + len(reap_ids_gdr)

    # ------------------------------------------------------------------
    # 统计 / 查询
    # ------------------------------------------------------------------

    def count_by_state(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"
            ).fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    def count_pending_gdr(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE state = 'pending_gdr'"
            ).fetchone()
        return int(row["n"]) if row else 0

    def count_pending_qf(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE state = 'pending'"
            ).fetchone()
        return int(row["n"]) if row else 0

    def list_tasks_for_batch(self, batch_id: int) -> list[Task]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, src_path, run_id, session_id, batch_id, state,
                       attempts_qf, attempts_gdr, qf_output_path,
                       gdr_output_path, error_msg, locked_by, locked_at,
                       created_at, updated_at
                FROM tasks
                WHERE batch_id = ?
                ORDER BY id
                """,
                (int(batch_id),),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def get(self, task_id: int) -> Task | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT id, src_path, run_id, session_id, batch_id, state,
                       attempts_qf, attempts_gdr, qf_output_path,
                       gdr_output_path, error_msg, locked_by, locked_at,
                       created_at, updated_at
                FROM tasks
                WHERE id = ?
                """,
                (int(task_id),),
            ).fetchone()
        return self._row_to_task(row) if row else None