"""orchestration.workers.gdr_worker: gdr 阶段 worker.

新架构 ``simulation server → gdr → etl`` 下 gdr 是首阶段, 流程：
    1. ``pull()``: 从队列 ``state='pending'`` 拉任务
    2. ``process()``: 读 trajectory JSONL（C1 契约）→
       ``gdr.parsers.from_trajectory`` 重放为 Session →
       ``gdr.pipeline._process_one_file(input, base_path, cfg)`` 精修 →
       ``gdr.domain.save_refined_session`` 写 C2 单文件（``gdr_refined_path``）
    3. ``mark_done()``: ``queue.mark_gdr_done(task.id, gdr_refined_path=...)``
       → state 转 ``pending_etl``

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

契约边界：gdr 不读 ``qf_output_path``（qf 阶段已删除）；输入是 trajectory
JSONL，由 ``gdr.parsers.from_trajectory`` 负责重放。
"""

from __future__ import annotations

from pathlib import Path

from gdr.config.settings import Settings
from gdr.pipeline.runner import _process_one_file
from gdr.parsers import from_trajectory

from orchestration.errors import NonRetryableError
from orchestration.queue import (
    STAGE_GDR,
    SQLiteQueue,
    Task,
)
from orchestration.workers.base_worker import BaseWorker


class GdrWorker(BaseWorker):
    """gdr 阶段 worker (新架构首阶段)."""

    stage = STAGE_GDR

    def __init__(
        self,
        *,
        queue: SQLiteQueue,
        worker_id: str,
        refined_dir: Path,
        gdr_settings: Settings | None = None,
        llm_concurrency: int = 4,
        n: int = 1,
        poll_seconds: float = 2.0,
        #: 方向 B: 允许拉取的 batch_id 集合 (引用, 由 Master 持有并在 batch
        #: 启停时增删). 为空集合 / None 时退化为旧行为 (拉所有 pending).
        allowed_batch_ids: set[int] | None = None,
    ) -> None:
        super().__init__(
            queue=queue, worker_id=worker_id, n=n, poll_seconds=poll_seconds,
        )
        self._refined_dir = Path(refined_dir)
        self._gdr_settings = gdr_settings
        self._llm_concurrency = int(llm_concurrency)
        #: process 写出的 C2 路径 (供 mark_done 回写队列)；见 C2 契约。
        self._last_output: Path | None = None
        #: 引用 Master 的活跃 batch 集合. 取一次快照传给 pull_pending_gdr,
        #: 避免 GIL 下边迭代边改引发的 RuntimeError; 下次 pull 再取新快照。
        self._allowed_batch_ids = allowed_batch_ids

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    def pull(self) -> list[Task]:
        # 方向 B: 只拉当前活跃 batch 的任务, 防止跨 batch 偷拉导致 master
        # shutdown 时 worker 仍在跑别的 batch 的活 → interpreter shutdown 错误。
        # 取一次快照而非直接传 set, 避免边迭代边改 (CPython GIL 下 set 迭代修改
        # 会抛 RuntimeError; 取 list 副本后传给 SQL 是安全的)。
        if self._allowed_batch_ids is None:
            batch_filter: list[int] | None = None
        else:
            batch_filter = sorted(self._allowed_batch_ids) if self._allowed_batch_ids else []
            # 空集合 = 当前没有活跃 batch, 直接返回空, 不打 SQL。
            if not batch_filter:
                return []
        return self._queue.pull_pending_gdr(
            worker_id=self._worker_id, n=self._n,
            batch_ids=batch_filter,
        )

    # ------------------------------------------------------------------
    # process
    # ------------------------------------------------------------------

    def process(self, task: Task) -> Path:
        # 输入缺失是永久性错误，直接 dead，不白跑 LLM 重试
        if not task.src_path.exists():
            raise NonRetryableError(
                f"gdr worker {self._worker_id}: trajectory missing for task {task.id}: {task.src_path}"
            )
        # C1 契约入口: trajectory JSONL → Session (单一解析路径)
        try:
            from_trajectory(task.src_path)
        except (ValueError, UnicodeDecodeError, FileNotFoundError) as exc:
            # 解析失败是永久性错误 (重试不会改变文件内容)
            raise NonRetryableError(
                f"gdr worker {self._worker_id}: trajectory parse failed "
                f"({type(exc).__name__}): {exc}"
            ) from exc

        session_id = task.session_id or task.src_path.stem
        # C2 单文件名: <task_id>__<session_id>.json (与 etl 末端 4 视图的 stem 共用
        # 同一 prefix, etl 末端 ``_save_outputs`` 沿用此 stem 加 .messages.json 等尾缀)
        out_path = self._refined_dir / self._output_name(task, session_id, suffix="")
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # 构造/复用 gdr.Settings：workers=1 走单进程，llm_concurrency 控制 LLM 信号量
        if self._gdr_settings is None:
            cfg = Settings(
                batch_output_dir=self._refined_dir,
                workers=1,
                llm_concurrency=self._llm_concurrency,
                max_files=1,
            )
        else:
            # 调用方已显式提供 settings（强制 batch_output_dir + workers=1）
            cfg = self._gdr_settings.model_copy(update={
                "batch_output_dir": self._refined_dir,
                "workers": 1,
                "max_files": 1,
            })

        result = _process_one_file(task.src_path, out_path, cfg)
        if result is None or result.get("status") != "success":
            # gdr 返回 None 通常是软超时部分保存；save_error / None 按可重试处理。
            status = result.get("status") if result else "None"
            err = (result or {}).get("error", "")
            if status in ("load_error", "discard", "incomplete"):
                # load_error = 输入文件坏（永久）；discard = 结构不可用（硬丢弃，
                # 重试结果相同）—— 都不值得再花 LLM 调用。incomplete = 未闭合
                # session 检测已旁路到 refine_data/incomplete.jsonl, refine_data
                # 跳过, 重跑只会让 detector 再命中一次, 不改变 outcome。
                raise NonRetryableError(
                    f"gdr worker {self._worker_id}: gdr status={status!r} "
                    f"(task={task.id}) {err}"
                )
            raise RuntimeError(
                f"gdr worker {self._worker_id}: gdr returned non-success "
                f"(status={status!r}, task={task.id}) {err}"
            )
        self._last_output = Path(result["output"])
        return self._last_output

    # ------------------------------------------------------------------
    # mark_done
    # ------------------------------------------------------------------

    def mark_done(self, task: Task, output: Path) -> None:
        """标记 gdr 完成：state 从 ``gdr_processing`` → ``pending_etl``.

        写 C2 refined Session 单文件路径。etl worker 可立即抢占并把 state
        推到 ``done``；因此这里不做 post-mark 校验（避免并发场景下的误报）。
        ``mark_gdr_done`` 自身的 SQL 守卫（``WHERE state='gdr_processing'``）
        已保证写操作的原子性。
        """
        self._queue.mark_gdr_done(task.id, gdr_refined_path=output)