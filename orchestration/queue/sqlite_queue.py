"""SQLite 任务队列实现 (2026-09-22 ST-2 重构).

每个 ``SQLiteQueue`` 实例 = 一个进程内单例; 不同进程共享同一个 db 文件.
SQLite 的 WAL 模式天然支持读并发 + 写串行, 因此不需要进程间锁.

新架构 ``simulation server → gdr → etl`` 下, 状态机:
    pending → simulate → gdr → etl → done
(失败终态走 dead, 调用 ``mark_failed`` 一步到位; ``requeue_dead`` 复活)

阶段推进不通过中间的 ``*_processing`` 抢占, 而是由 ``PipelineExecutor``
按 ``max_parallelism`` 调度 ``multiprocessing.Pool`` 一次性跑单 task 全流程.
子进程内通过 ``SQLiteQueue(paths.sqlite_db)`` 重新构造 (connection 不可
pickle, 见契约 §2.7).

详见 ``docs/设计方案/pipeline-contracts.md`` §2.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal


# ---------------------------------------------------------------------------
# 常量 (契约 §2.3)
# ---------------------------------------------------------------------------

PHASE_PENDING = "pending"
PHASE_SIMULATE = "simulate"
PHASE_GDR = "gdr"
PHASE_ETL = "etl"
PHASE_DONE = "done"
PHASE_DEAD = "dead"

ALL_PHASES = frozenset({
    PHASE_PENDING, PHASE_SIMULATE, PHASE_GDR,
    PHASE_ETL, PHASE_DONE, PHASE_DEAD,
})
TERMINAL_PHASES = frozenset({PHASE_DONE, PHASE_DEAD})

STAGE_SIMULATE = "simulate"
STAGE_GDR = "gdr"
STAGE_ETL = "etl"
_VALID_STAGES = (STAGE_SIMULATE, STAGE_GDR, STAGE_ETL)

_ATTEMPT_COLUMNS = {
    STAGE_SIMULATE: "attempts_simulate",
    STAGE_GDR: "attempts_gdr",
    STAGE_ETL: "attempts_etl",
}


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Task:
    """从 tasks 表读出的一行 (契约 §2.4); 路径字段已转为 Path."""
    task_id: str
    phase: str
    run_id: str | None
    session_id: str | None
    attempts_simulate: int
    attempts_gdr: int
    attempts_etl: int
    src_path: Path | None
    gdr_refined_path: Path | None
    etl_messages_path: Path | None
    etl_openai_path: Path | None
    etl_qwenjina_path: Path | None
    etl_meta_path: Path | None
    error_msg: str | None
    started_at: str
    updated_at: str


class TaskAlreadyTerminal(Exception):
    """upsert_task 时遇到终态 task 时抛 (契约 §2.6)."""

    def __init__(self, task_id: str, current_phase: str) -> None:
        super().__init__(
            f"task {task_id!r} is already terminal (phase={current_phase!r}); "
            "use requeue_dead() to revive before upserting"
        )
        self.task_id = task_id
        self.current_phase = current_phase


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    """UTC ISO8601 字符串 (微秒精度, Z 结尾), 用作 SQLite TEXT 时间戳."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _to_path_or_none(val: str | None) -> Path | None:
    return Path(val) if val else None


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------

class SQLiteQueue:
    """SQLite-backed 任务队列 (契约 §2.5).

    设计要点:
    * 每个方法开新连接、用 ``BEGIN IMMEDIATE`` 串行化写操作; 不需要进程内锁.
    * WAL 模式下多个 reader 可并发, writer 自动排队.
    * 多进程并发安全: 子进程必须重新构造 ``SQLiteQueue(db_path)`` 实例
      (sqlite3 connection 不可 pickle, 契约 §2.7).
    """

    def __init__(
        self,
        db_path: Path,
        *,
        busy_timeout_ms: int = 30_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._init_schema()

    # ------------------------------------------------------------------
    # 上下文管理 (契约 §2.5)
    # ------------------------------------------------------------------

    def __enter__(self) -> "SQLiteQueue":
        return self

    def __exit__(self, *exc) -> None:
        # 当前实现每个方法都开新连接, 无外部资源需释放; 保留接口契约.
        return None

    @property
    def db_path(self) -> Path:
        """暴露 db 文件路径, 供子进程重连 / 健康检查使用."""
        return self._db_path

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self._db_path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,            # autocommit; 显式 BEGIN/COMMIT
            check_same_thread=False,         # 跨线程安全 (单实例不持连接)
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
            # 旧架构遗留表 (batches / run_tasks) 直接 DROP — 新 schema 不用它们.
            # CREATE IF NOT EXISTS 不会创建它们, 留着占空间且干扰测试断言.
            for tbl in ("batches", "run_tasks"):
                cur = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name=?",
                    (tbl,),
                )
                if cur.fetchone() is not None:
                    conn.execute(f"DROP TABLE {tbl}")
            conn.executescript(ddl)

    # ------------------------------------------------------------------
    # 内部：行转 Task
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            task_id=row["task_id"],
            phase=row["phase"],
            run_id=row["run_id"],
            session_id=row["session_id"],
            attempts_simulate=row["attempts_simulate"],
            attempts_gdr=row["attempts_gdr"],
            attempts_etl=row["attempts_etl"],
            src_path=_to_path_or_none(row["src_path"]),
            gdr_refined_path=_to_path_or_none(row["gdr_refined_path"]),
            etl_messages_path=_to_path_or_none(row["etl_messages_path"]),
            etl_openai_path=_to_path_or_none(row["etl_openai_path"]),
            etl_qwenjina_path=_to_path_or_none(row["etl_qwenjina_path"]),
            etl_meta_path=_to_path_or_none(row["etl_meta_path"]),
            error_msg=row["error_msg"],
            started_at=row["started_at"],
            updated_at=row["updated_at"],
        )

    _TASK_COLUMNS = (
        "task_id, phase, run_id, session_id, "
        "attempts_simulate, attempts_gdr, attempts_etl, "
        "src_path, gdr_refined_path, "
        "etl_messages_path, etl_openai_path, etl_qwenjina_path, etl_meta_path, "
        "error_msg, started_at, updated_at"
    )

    def _select_task_by_task_id(
        self, conn: sqlite3.Connection, task_id: str, *,
        for_update: bool = False,
    ) -> sqlite3.Row | None:
        sql = f"SELECT {self._TASK_COLUMNS} FROM tasks WHERE task_id = ?"
        if for_update:
            sql += " AND phase NOT IN ('done', 'dead')"
        return conn.execute(sql, (task_id,)).fetchone()

    # ------------------------------------------------------------------
    # 写入: upsert_task / mark_phase / increment_attempts / mark_failed
    # ------------------------------------------------------------------

    def upsert_task(self, task_id: str, *, phase: str = PHASE_PENDING) -> Task:
        """新建或重置 task 行 (契约 §2.5).

        - 不存在 → INSERT (phase=pending 默认, attempts 清零).
        - 存在但 phase ∉ TERMINAL_PHASES → 重置 phase + 清 attempts + 清错误 + 清产物路径.
        - 存在且 phase ∈ TERMINAL_PHASES → 抛 ``TaskAlreadyTerminal``.

        返回: 新建的 Task (含 DB 自动写的时间戳).
        """
        if phase not in ALL_PHASES:
            raise ValueError(f"invalid phase: {phase!r}")
        now = _utc_now_iso()
        with self._conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = self._select_task_by_task_id(conn, task_id)
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO tasks (
                            task_id, phase,
                            attempts_simulate, attempts_gdr, attempts_etl,
                            error_msg, started_at, updated_at
                        )
                        VALUES (?, ?, 0, 0, 0, NULL, ?, ?)
                        """,
                        (task_id, phase, now, now),
                    )
                else:
                    if row["phase"] in TERMINAL_PHASES:
                        raise TaskAlreadyTerminal(task_id, row["phase"])
                    conn.execute(
                        """
                        UPDATE tasks
                        SET phase = ?,
                            attempts_simulate = 0,
                            attempts_gdr = 0,
                            attempts_etl = 0,
                            run_id = NULL,
                            session_id = NULL,
                            src_path = NULL,
                            gdr_refined_path = NULL,
                            etl_messages_path = NULL,
                            etl_openai_path = NULL,
                            etl_qwenjina_path = NULL,
                            etl_meta_path = NULL,
                            error_msg = NULL,
                            updated_at = ?
                        WHERE task_id = ?
                        """,
                        (phase, now, task_id),
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            refreshed = self._select_task_by_task_id(conn, task_id)
            assert refreshed is not None
            return self._row_to_task(refreshed)

    def mark_phase(
        self,
        task_id: str,
        *,
        new_phase: str,
        run_id: str | None = None,
        session_id: str | None = None,
        src_path: Path | None = None,
        gdr_refined_path: Path | None = None,
        etl_messages_path: Path | None = None,
        etl_openai_path: Path | None = None,
        etl_qwenjina_path: Path | None = None,
        etl_meta_path: Path | None = None,
    ) -> None:
        """推进 phase, 同步写各阶段产物路径 (契约 §2.5).

        阶段间合法性由调用方保证 (典型序列: pending → simulate → gdr → etl → done);
        本方法不做迁移检查, 但 new_phase 必须 ∈ ALL_PHASES.
        """
        if new_phase not in ALL_PHASES:
            raise ValueError(f"invalid phase: {new_phase!r}")
        now = _utc_now_iso()
        sets = ["phase = ?", "updated_at = ?"]
        args: list[object] = [new_phase, now]
        # 各字段仅在显式传入时更新 (None 不覆盖) — 但契约允许显式 None 表示"清空"
        # 这里采用"显式传 None 也保留原值"的语义, 调用方需要清空就再写一次.
        if run_id is not None:
            sets.append("run_id = ?"); args.append(run_id)
        if session_id is not None:
            sets.append("session_id = ?"); args.append(session_id)
        if src_path is not None:
            sets.append("src_path = ?"); args.append(str(src_path))
        if gdr_refined_path is not None:
            sets.append("gdr_refined_path = ?"); args.append(str(gdr_refined_path))
        if etl_messages_path is not None:
            sets.append("etl_messages_path = ?"); args.append(str(etl_messages_path))
        if etl_openai_path is not None:
            sets.append("etl_openai_path = ?"); args.append(str(etl_openai_path))
        if etl_qwenjina_path is not None:
            sets.append("etl_qwenjina_path = ?"); args.append(str(etl_qwenjina_path))
        if etl_meta_path is not None:
            sets.append("etl_meta_path = ?"); args.append(str(etl_meta_path))
        args.append(task_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ?",
                args,
            )

    def increment_attempts(self, task_id: str, *, stage: str) -> int:
        """自增 attempts_simulate | attempts_gdr | attempts_etl, 返回新值.

        stage ∈ {STAGE_SIMULATE, STAGE_GDR, STAGE_ETL}; 其他值抛 ``ValueError``.
        """
        if stage not in _VALID_STAGES:
            raise ValueError(
                f"invalid stage: {stage!r}; expected one of {_VALID_STAGES}"
            )
        col = _ATTEMPT_COLUMNS[stage]
        now = _utc_now_iso()
        with self._conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"SELECT {col} AS n FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(f"task {task_id!r} not found")
                new_val = int(row["n"]) + 1
                conn.execute(
                    f"UPDATE tasks SET {col} = ?, updated_at = ? WHERE task_id = ?",
                    (new_val, now, task_id),
                )
                conn.execute("COMMIT")
                return new_val
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def mark_failed(
        self,
        task_id: str,
        *,
        stage: str,
        error_msg: str,
    ) -> None:
        """标 phase=dead, 写 error_msg (契约 §2.5).

        注: 本方法不递增 attempts; 调用方负责按重试上限决定何时调用它
        (典型用法: 失败次数达到 max_retry_* 后再 mark_failed).
        """
        if stage not in _VALID_STAGES:
            raise ValueError(
                f"invalid stage: {stage!r}; expected one of {_VALID_STAGES}"
            )
        del stage  # 仅用于校验, 不写库
        now = _utc_now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET phase = ?,
                    error_msg = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (PHASE_DEAD, error_msg, now, task_id),
            )

    def requeue_dead(self) -> int:
        """所有 phase=dead 改 phase=pending, 清空 error_msg / 产物路径.

        返回: 被重置的行数 (契约 §2.5).
        """
        now = _utc_now_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                SET phase = ?,
                    error_msg = NULL,
                    run_id = NULL,
                    session_id = NULL,
                    src_path = NULL,
                    gdr_refined_path = NULL,
                    etl_messages_path = NULL,
                    etl_openai_path = NULL,
                    etl_qwenjina_path = NULL,
                    etl_meta_path = NULL,
                    updated_at = ?
                WHERE phase = ?
                """,
                (PHASE_PENDING, now, PHASE_DEAD),
            )
            return int(cur.rowcount)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> Task | None:
        """按 task_id 查单行, 不存在返 None (契约 §2.5)."""
        with self._conn() as conn:
            row = self._select_task_by_task_id(conn, task_id)
            return self._row_to_task(row) if row else None

    def list_tasks(
        self,
        *,
        phase: str | None = None,
        limit: int | None = None,
    ) -> list[Task]:
        """按 phase 过滤; phase=None 返全部; 按 started_at 排序 (契约 §2.5)."""
        if phase is not None and phase not in ALL_PHASES:
            raise ValueError(f"invalid phase: {phase!r}")
        sql = f"SELECT {self._TASK_COLUMNS} FROM tasks"
        args: list[object] = []
        if phase is not None:
            sql += " WHERE phase = ?"
            args.append(phase)
        sql += " ORDER BY started_at, task_id"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [self._row_to_task(r) for r in rows]

    def count_by_phase(self) -> dict[str, int]:
        """返 {phase: count} 全分布 (契约 §2.5)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT phase, COUNT(*) AS n FROM tasks GROUP BY phase"
            ).fetchall()
        out: dict[str, int] = {p: 0 for p in ALL_PHASES}
        for r in rows:
            out[str(r["phase"])] = int(r["n"])
        return out
