from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from simulate_serve.checker import check_completion, snapshot_partial_trajectory
from simulate_serve.domain.run import RunEvent, RunFailure, TaskRun
from simulate_serve.domain.state_machine import RunState, RunStateMachine
from simulate_serve.domain.task import CompiledTask
from .errors import RepositoryPortError

from .run_task import TaskRuntime

logger = logging.getLogger(__name__)


def _force_terminal(
    run: TaskRun,
    target: RunState,
    event_type: str,
    detail: dict,
) -> None:
    """强制把 run 设为终态 — 跳过 ``RunStateMachine.transition`` 的白名单校验。

    仅在 BatchRunner 兜底放弃时使用: trajectory 完整性否决业务结果
    (例如 SUCCESS 但 archiver 报 error 终态) 必须把 run 标 COMPLETION_INCOMPLETE,
    而 RunStateMachine 不允许终态之间迁移, 故走直接赋值 + 追加 ``state_events``
    审计行。语义与 ``JsonRunRepository.mark_interrupted`` 一致。
    """
    previous = run.state
    run.state = target
    run.completed_at = datetime.now(UTC)
    run.state_events.append(
        RunEvent(
            event_type=event_type,
            from_state=previous,
            to_state=target,
            detail=detail,
        )
    )


class BatchRunner:
    def __init__(self, runtime: TaskRuntime):
        self.runtime = runtime

    async def run(
        self,
        tasks: list[CompiledTask] | tuple[CompiledTask, ...],
        *,
        limit: int = 0,
        rerun_of: str | None = None,
        max_run_retries: int | None = None,
    ) -> list[TaskRun]:
        selected = list(tasks[:limit] if limit > 0 else tasks)
        if rerun_of and len(selected) != 1:
            raise ValueError("rerun_of requires exactly one selected task")
        runs: list[TaskRun] = []
        for task in selected:
            effective_max = max_run_retries if max_run_retries is not None else task.max_run_retries
            runs.append(
                await self._run_one_task_with_retry(
                    task,
                    rerun_of=rerun_of,
                    effective_max_run_retries=effective_max,
                )
            )
        return runs

    async def _run_one_task_with_retry(
        self,
        task: CompiledTask,
        *,
        rerun_of: str | None,
        effective_max_run_retries: int | None = None,
    ) -> TaskRun:
        """单 task 跑一次, 若 trajectory 不完整则按 ``task.max_run_retries`` 重投.

        重投语义:
          - 复用 run_id (与 ``--rerun-task`` 的 ``rerun_of`` 血缘区分)
          - ``run.retry_count`` 自增
          - 重投前把当前 partial trajectory 复制为
            ``<run_id>.trajectory_attempt_<N>.json`` 留底 (决策点 5)
          - 超过 ``max_run_retries`` 仍未完整 → 落 ``RunFailure(code=
            "COMPLETION_INCOMPLETE")`` + ``COMPLETION_INCOMPLETE`` 终态
        """
        max_retries = (
            effective_max_run_retries
            if effective_max_run_retries is not None
            else task.max_run_retries
        )
        max_attempts = max(0, int(max_retries)) + 1
        prior_run: TaskRun | None = None
        last_run: TaskRun | None = None

        for attempt in range(max_attempts):
            try:
                run = (
                    await self.runtime.run(task, rerun_of=rerun_of)
                    if prior_run is None
                    else await self.runtime.run(task, rerun_of=rerun_of, reuse_run=prior_run)
                )
            except RepositoryPortError:
                raise
            except Exception as exc:
                logger.exception("Unhandled task boundary error for %s", task.task_id)
                run = TaskRun(
                    run_id=f"boundary_{uuid.uuid4().hex}",
                    task_id=task.task_id,
                    task_type=task.task_type,
                    state=RunState.EXECUTOR_ERROR,
                    failure=RunFailure(code="BATCH_BOUNDARY_ERROR", message=str(exc), stage="batch"),
                )
            last_run = run

            # 完整性判定 — 只对 trajectory 路径可达且 run 已落盘的情形生效.
            # record-only / 验证关闭 / 测试 fixture 路径下 trajectory_path_for
            # 返回 None, 直接跳过判定: 不阻塞流程, 也不触发重投 (archiver
            # 关闭是配置而非数据问题).
            trajectory_path = self.runtime.trajectory_path_for(run)
            if trajectory_path is None:
                logger.debug(
                    "task %s run %s: trajectory archiver disabled, skipping completion check",
                    task.task_id, run.run_id,
                )
                return run
            check = check_completion(trajectory_path)
            run.completion_check = check
            if self.runtime.repository is not None:
                self.runtime.repository.save_run(run)

            if check.status == "complete":
                logger.info(
                    "task %s run %s COMPLETE on attempt %d/%d: %s",
                    task.task_id, run.run_id, attempt + 1, max_attempts, check.summary,
                )
                return run

            # trajectory 不完整/aborted: 决策分支取决于 run 是否已是终态.
            # - 已终态 (如 SUCCESS): validation 已通过, 不覆盖; 仅记录
            #   ``completion_check`` + ``RunFailure.stage=sentinel_*`` 供审计.
            # - 非终态: 落 ``COMPLETION_INCOMPLETE`` 终态 + RunFailure.
            if check.status == "aborted":
                run.failure = RunFailure(
                    code="COMPLETION_ABORTED",
                    message=check.summary,
                    stage="trajectory",
                    retryable=False,
                )
                _force_terminal(
                    run,
                    RunState.COMPLETION_INCOMPLETE,
                    "RUN_COMPLETION_ABORTED",
                    {"reasons": list(check.reasons), "summary": check.summary},
                )
                if self.runtime.repository is not None:
                    self.runtime.repository.save_run(run)
                logger.warning(
                    "task %s run %s ABORTED on attempt %d/%d: %s",
                    task.task_id, run.run_id, attempt + 1, max_attempts, check.summary,
                )
                return run

            # incomplete — 是否还有重投预算
            if attempt + 1 >= max_attempts:
                run.failure = RunFailure(
                    code="COMPLETION_INCOMPLETE",
                    message=check.summary,
                    stage="trajectory",
                    retryable=False,
                )
                _force_terminal(
                    run,
                    RunState.COMPLETION_INCOMPLETE,
                    "RUN_COMPLETION_INCOMPLETE",
                    {
                        "reasons": list(check.reasons),
                        "summary": check.summary,
                        "attempts": attempt + 1,
                        "max_run_retries": max_retries,
                    },
                )
                if self.runtime.repository is not None:
                    self.runtime.repository.save_run(run)
                logger.warning(
                    "task %s run %s INCOMPLETE after %d attempt(s), giving up: %s",
                    task.task_id, run.run_id, attempt + 1, check.summary,
                )
                return run

            # 还有预算: snapshot partial trajectory 然后准备重投.
            snapshot_partial_trajectory(
                trajectory_path,
                run.run_id,
                attempt=attempt + 1,
            )
            logger.info(
                "task %s run %s INCOMPLETE on attempt %d/%d, retrying: %s",
                task.task_id, run.run_id, attempt + 1, max_attempts, check.summary,
            )
            prior_run = run

        # 不可达 — 保留 last_run 防御性返回.
        assert last_run is not None
        return last_run