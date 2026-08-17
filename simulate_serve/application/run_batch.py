from __future__ import annotations

import logging
import uuid

from simulate_serve.domain.run import RunFailure, TaskRun
from simulate_serve.domain.state_machine import RunState
from simulate_serve.domain.task import CompiledTask
from .errors import RepositoryPortError

from .run_task import TaskRuntime

logger = logging.getLogger(__name__)


class BatchRunner:
    def __init__(self, runtime: TaskRuntime):
        self.runtime = runtime

    async def run(
        self,
        tasks: list[CompiledTask] | tuple[CompiledTask, ...],
        *,
        limit: int = 0,
        rerun_of: str | None = None,
    ) -> list[TaskRun]:
        selected = list(tasks[:limit] if limit > 0 else tasks)
        if rerun_of and len(selected) != 1:
            raise ValueError("rerun_of requires exactly one selected task")
        runs: list[TaskRun] = []
        for task in selected:
            try:
                runs.append(await self.runtime.run(task, rerun_of=rerun_of))
            except RepositoryPortError:
                raise
            except Exception as exc:
                logger.exception("Unhandled task boundary error for %s", task.task_id)
                runs.append(
                    TaskRun(
                        run_id=f"boundary_{uuid.uuid4().hex}",
                        task_id=task.task_id,
                        task_type=task.task_type,
                        state=RunState.EXECUTOR_ERROR,
                        failure=RunFailure(code="BATCH_BOUNDARY_ERROR", message=str(exc), stage="batch"),
                    )
                )
        return runs
