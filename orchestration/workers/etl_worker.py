"""orchestration.workers.etl_worker: etl 阶段 worker (新架构末阶段).

新架构 ``simulation server → gdr → etl`` 下 etl 是末阶段, 流程：
    1. ``pull()``: 从队列 ``state='pending_etl'`` 拉任务
    2. ``process()``: 读 C2 refined Session 单文件 (``gdr_refined_path``,
       schema_version=refined_session.v1) → ``etl.parsers.load_refined_session``
       → ``gdr.domain.save_session_v2`` 拆 4 视图 → 落 ``<stem>.messages.json``
       / ``<stem>.openai.json`` / ``<stem>.qwenjina.txt`` (optional) /
       ``<stem>.meta.json``
    3. ``mark_done()``: ``queue.mark_etl_done(task.id, etl_*_path=...)``
       → state 转 ``done``

gdr 写 C2 的 stem 与 etl 拆 4 视图的 stem 共用：gdr 写 ``<stem>.json``,
etl 沿用同一 stem 加 ``.messages.json`` / ``.openai.json`` 等尾缀, 这样产物
归并到同一前缀下 (和 gdr.refined_path 共用 task_id 前缀)。

并发模型：master 起 M 个 etl_worker 进程, 每个 worker 独立 pull SQLite
队列, SQLite 写锁自动串行化抢占。LLM 调用在本阶段不使用 (所有 LLM 调用
已在 gdr 完成)，纯本地计算 + 文件 IO, 不需要并发控制信号量。

失败由 ``base_worker._handle_failure`` 走 ``queue.mark_failed(stage=etl)``；
attempts 超 max 时入 dead。
"""

from __future__ import annotations

from pathlib import Path

from gdr.domain import save_session_v2

from etl.parsers import load_refined_session

from orchestration.errors import NonRetryableError
from orchestration.queue import (
    STAGE_ETL,
    SQLiteQueue,
    Task,
)
from orchestration.workers.base_worker import BaseWorker


class EtlWorker(BaseWorker):
    """etl 阶段 worker (新架构末阶段)."""

    stage = STAGE_ETL

    def __init__(
        self,
        *,
        queue: SQLiteQueue,
        worker_id: str,
        outputs_dir: Path,
        n: int = 1,
        poll_seconds: float = 2.0,
        #: 方向 B: 允许拉取的 batch_id 集合 (引用, 由 Master 持有并在 batch
        #: 启停时增删). 为空集合 / None 时退化为旧行为 (拉所有 pending_etl).
        allowed_batch_ids: set[int] | None = None,
    ) -> None:
        super().__init__(
            queue=queue, worker_id=worker_id, n=n, poll_seconds=poll_seconds,
        )
        self._outputs_dir = Path(outputs_dir)
        self._outputs_dir.mkdir(parents=True, exist_ok=True)
        self._last_outputs = None  # SessionOutputs from save_session_v2
        #: 引用 Master 的活跃 batch 集合, 同 GdrWorker 处理 (取 list 副本传 SQL)
        self._allowed_batch_ids = allowed_batch_ids

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    def pull(self) -> list[Task]:
        # 方向 B: 同 GdrWorker, 只拉当前活跃 batch 的任务。
        if self._allowed_batch_ids is None:
            batch_filter: list[int] | None = None
        else:
            batch_filter = sorted(self._allowed_batch_ids) if self._allowed_batch_ids else []
            if not batch_filter:
                return []
        return self._queue.pull_pending_etl(
            worker_id=self._worker_id, n=self._n,
            batch_ids=batch_filter,
        )

    # ------------------------------------------------------------------
    # process
    # ------------------------------------------------------------------

    def process(self, task: Task) -> Path:
        # 输入是 gdr 阶段写的 C2 refined Session 单文件路径 (gdr_refined_path)。
        # C2 缺失是永久性错误：gdr 已标记 done, 不会重写, 重试不会改变结果。
        if not task.gdr_refined_path:
            raise NonRetryableError(
                f"etl worker {self._worker_id}: gdr_refined_path missing for task {task.id}"
            )
        c2_path = Path(task.gdr_refined_path)
        if not c2_path.exists():
            raise NonRetryableError(
                f"etl worker {self._worker_id}: gdr_refined missing on disk: {c2_path}"
            )
        # C2 契约入口: 校验 schema_version=refined_session.v1, 失败抛 NonRetryableError
        session = load_refined_session(c2_path)

        # 4 视图产物落 etl outputs_dir, stem 与 C2 同 stem (去掉 .json 扩展名,
        # save_session_v2 会自动追加 .messages.json / .openai.json 等尾缀)
        base_path = self._outputs_dir / c2_path.stem
        try:
            outputs = save_session_v2(session, base_path)
        except Exception as exc:
            # 写盘失败通常可重试 (IO 抖动), 让 base_worker 走 mark_failed(stage=etl)
            raise RuntimeError(
                f"etl worker {self._worker_id}: save_session_v2 failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        self._last_outputs = outputs
        return outputs.messages

    # ------------------------------------------------------------------
    # mark_done
    # ------------------------------------------------------------------

    def mark_done(self, task: Task, output: Path) -> None:
        """标记 etl 完成：state 从 ``etl_processing`` → ``done``.

        ``mark_etl_done`` 自身的 SQL 守卫已保证原子性, 不在 mark 后做校验。
        """
        outputs = self._last_outputs
        if outputs is None:
            # 理论上 process 一定会 set _last_outputs; 兜底防止 process 异常路径
            # 漏过 (例如 save_session_v2 抛前被外层捕获, 但 _last_outputs 未清空)。
            # 此时 outputs 是未知, 不能调 mark_etl_done。
            raise RuntimeError(
                f"etl worker {self._worker_id}: _last_outputs unset for task {task.id}"
            )
        self._queue.mark_etl_done(
            task.id,
            etl_messages_path=outputs.messages,
            etl_openai_path=outputs.openai,
            etl_qwenjina_path=outputs.qwenjina,
            etl_meta_path=outputs.meta,
        )