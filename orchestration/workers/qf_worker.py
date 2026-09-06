"""orchestration.workers.qf_worker: qf 阶段 worker.

流程：
    1. ``pull()``: 从队列 ``state='pending'`` 拉任务
    2. ``process()``: 读 trajectory（``run_<run_id>__<session_id>.json`` JSONL 事件流）
       → ``etl.qwenformat.load.load_trajectory`` 重放为 ``SessionRecord``
       → ``SessionRecord.to_session_dict`` 得到 Session 形态 dict
       → ``etl.qwenformat.transform.trajectory_to_session_with_openai_metadata``
       渲染出 Qwen3 训练文本，落 ``qf_output_dir/<session_id>.json``
    3. ``mark_done()``: ``queue.mark_qf_done(task.id, qf_output_path=...)``
       → state 转 ``pending_gdr``

失败由 ``base_worker._handle_failure`` 走 ``queue.mark_failed(stage=qf)``；
attempts 超 max 时入 dead。

注: 仅识别新格式 trajectory JSONL 事件流 (``run_<run_id>__<session_id>.json``)；
旧 CAMEL 单对象 / 裸 .jsonl 直接抛异常, 由 worker 标记失败入 dead, 不再兼容.
"""

from __future__ import annotations

import json
from pathlib import Path

from jinja2.sandbox import ImmutableSandboxedEnvironment

from etl.qwenformat.load import load_trajectory
from etl.qwenformat.transform import (
    build_chat_env,
    load_chat_template,
    trajectory_to_session_with_openai_metadata,
)
from orchestration.queue import (
    STAGE_QF,
    SQLiteQueue,
    Task,
)
from orchestration.workers.base_worker import BaseWorker


class QfWorker(BaseWorker):
    """qf 阶段 worker."""

    stage = STAGE_QF

    def __init__(
        self,
        *,
        queue: SQLiteQueue,
        worker_id: str,
        qf_output_dir: Path,
        template_str: str | None = None,
        env: ImmutableSandboxedEnvironment | None = None,
        template_path: Path | str | None = None,
        n: int = 1,
        poll_seconds: float = 2.0,
    ) -> None:
        super().__init__(
            queue=queue, worker_id=worker_id, n=n, poll_seconds=poll_seconds,
        )
        self._qf_output_dir = Path(qf_output_dir)
        if template_str is None:
            if template_path is None:
                # 默认走仓库根 etl/qwenformat/chat_template.jinja
                template_path = Path(__file__).resolve().parents[2] / "etl" / "qwenformat" / "chat_template.jinja"
            template_str = load_chat_template(str(template_path))
        if env is None:
            env = build_chat_env()
        self._template = template_str
        self._env = env

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    def pull(self) -> list[Task]:
        return self._queue.pull_pending_qf(worker_id=self._worker_id, n=self._n)

    # ------------------------------------------------------------------
    # process
    # ------------------------------------------------------------------

    def process(self, task: Task) -> Path:
        # 新格式 trajectory: ``run_<run_id>__<session_id>.json`` (JSONL 事件流)
        # 不再支持旧 CAMEL 单对象 / 裸 .jsonl 形态; 解析失败抛异常, 由 worker
        # 走 ``_handle_failure`` 标记失败并最终入 dead 归档 (与 producer 终止后
        # 残留旧格式文件的预期一致, 不丢数据, 不兼容转换).
        record = load_trajectory(task.src_path)
        trajectory = record.to_session_dict()
        out = trajectory_to_session_with_openai_metadata(
            trajectory, self._template, self._env,
        )
        session_id = task.session_id or task.src_path.stem
        out_path = self._qf_output_dir / self._output_name(task, session_id, suffix="")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(out, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return out_path

    # ------------------------------------------------------------------
    # mark_done
    # ------------------------------------------------------------------

    def mark_done(self, task: Task, output: Path) -> None:
        """标记 qf 完成：state 从 ``qf_processing`` → ``pending_gdr``.

        注：gdr worker 可能立刻抢占并把 state 推到 ``done``；因此这里不做
        post-mark 校验（避免并发场景下的误报）。``mark_qf_done`` 自身的 SQL
        守卫（``WHERE state='qf_processing'``）已保证写操作的原子性。
        """
        self._queue.mark_qf_done(task.id, qf_output_path=output)
