"""orchestration.workers.gdr_worker: gdr 阶段 worker.

流程：
    1. ``pull()``: 从队列 ``state='pending_gdr'`` 拉任务
    2. ``process()``: 读 qf_out/<session>.json → 调
       ``gdr.pipeline._process_one_file(input, base_path, cfg)`` → 落
       ``gdr_output_dir/<session>_refined.{messages,openai}.json`` 等 4 份视图
    3. ``mark_done()``: ``queue.mark_gdr_done(task.id, gdr_*_path=...)``
       → state 转 ``done``

并发模型：
    - 本进程不依赖 gdr 内部 multiprocessing.Pool（``cfg.workers=1``）；
      LLM 调用并发由 ``cfg.llm_concurrency`` 信号量控制（见
      ``gdr/pipeline/runner.py:202-206``）。
    - 跨进程并发由 master 起 M 个 gdr_worker 进程达到：每个 worker 独立
      pull SQLite 队列，SQLite 写锁自动串行化抢占。
    - 若日后需要 Pool，可改为对每批 task 调一次 ``gdr.pipeline.run(cfg)``
      的 batch 模式并设 ``cfg.workers>1``。

失败由 ``base_worker._handle_failure`` 走 ``queue.mark_failed(stage=gdr)``；
attempts 超 max 时入 dead。
"""

from __future__ import annotations

from pathlib import Path

from gdr.config.settings import Settings
from gdr.pipeline.runner import _process_one_file

from orchestration.errors import NonRetryableError
from orchestration.queue import (
    STAGE_GDR,
    SQLiteQueue,
    Task,
)
from orchestration.workers.base_worker import BaseWorker


class GdrWorker(BaseWorker):
    """gdr 阶段 worker."""

    stage = STAGE_GDR

    def __init__(
        self,
        *,
        queue: SQLiteQueue,
        worker_id: str,
        gdr_output_dir: Path,
        gdr_settings: Settings | None = None,
        llm_concurrency: int = 4,
        n: int = 1,
        poll_seconds: float = 2.0,
    ) -> None:
        super().__init__(
            queue=queue, worker_id=worker_id, n=n, poll_seconds=poll_seconds,
        )
        self._gdr_output_dir = Path(gdr_output_dir)
        self._gdr_settings = gdr_settings
        self._llm_concurrency = int(llm_concurrency)
        #: process 拆出的 4 份视图路径 (供 mark_done 回写队列)；见 §5 契约 A。
        self._last_outputs: dict[str, str | None] | None = None

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    def pull(self) -> list[Task]:
        return self._queue.pull_pending_gdr(worker_id=self._worker_id, n=self._n)

    # ------------------------------------------------------------------
    # process
    # ------------------------------------------------------------------

    def process(self, task: Task) -> Path:
        # qf_output_path 由 qf_worker 写入，存的是绝对路径
        qf_input = Path(task.qf_output_path) if task.qf_output_path else task.src_path
        if not qf_input.exists():
            # 输入缺失是永久性错误，直接 dead，不白跑 LLM 重试
            raise NonRetryableError(
                f"gdr worker {self._worker_id}: qf output missing for task {task.id}: {qf_input}"
            )
        session_id = task.session_id or task.src_path.stem
        out_base = self._gdr_output_dir / self._output_name(task, session_id, suffix="_refined")
        base_path = out_base.with_suffix("")  # strip `.json` → 拆 4 份的 stem

        # 构造/复用 gdr.Settings：workers=1 走单进程，llm_concurrency 控制 LLM 信号量
        if self._gdr_settings is None:
            cfg = Settings(
                batch_output_dir=self._gdr_output_dir,
                workers=1,
                llm_concurrency=self._llm_concurrency,
                max_files=1,
            )
        else:
            # 调用方已显式提供 settings（强制 batch_output_dir + workers=1）
            cfg = self._gdr_settings.model_copy(update={
                "batch_output_dir": self._gdr_output_dir,
                "workers": 1,
                "max_files": 1,
            })

        result = _process_one_file(qf_input, base_path, cfg)
        if result is None or result.get("status") != "success":
            # gdr 返回 None 通常是软超时部分保存；save_error / None 按可重试处理。
            status = result.get("status") if result else "None"
            err = (result or {}).get("error", "")
            if status in ("load_error", "discard"):
                # load_error = 输入文件坏（永久）；discard = 结构不可用（硬丢弃，
                # 重试结果相同）—— 都不值得再花 LLM 调用
                raise NonRetryableError(
                    f"gdr worker {self._worker_id}: gdr status={status!r} "
                    f"(task={task.id}) {err}"
                )
            raise RuntimeError(
                f"gdr worker {self._worker_id}: gdr returned non-success "
                f"(status={status!r}, task={task.id}) {err}"
            )
        outputs = result.get("outputs") or {}
        self._last_outputs = outputs
        return Path(outputs["messages"])

    # ------------------------------------------------------------------
    # mark_done
    # ------------------------------------------------------------------

    def mark_done(self, task: Task, output: Path) -> None:
        """标记 gdr 完成：state 从 ``gdr_processing`` → ``done``.

        与 qf_worker.mark_done 同样的并发考量：不在 mark 后做 post-mark 校验，
        ``mark_gdr_done`` 自身的 SQL 守卫已保证原子性。
        """
        outputs = self._last_outputs or {}
        self._queue.mark_gdr_done(
            task.id,
            gdr_messages_path=Path(outputs["messages"]),
            gdr_openai_path=Path(outputs["openai"]),
            gdr_qwenjina_path=Path(outputs["qwenjina"]) if outputs.get("qwenjina") else None,
            gdr_meta_path=Path(outputs["meta"]),
        )