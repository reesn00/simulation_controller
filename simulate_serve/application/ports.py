from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.task import CompiledTask
from simulate_serve.domain.validation import ValidationReport


class ExecutorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    session_id: str
    remote_task_id: str
    agent_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Toolcall blocks from the most recent executor round, sourced from the
    # trajectory JSONL after the archiver's flush wait. Empty tuple when the
    # remote side did not surface any tool calls this round (text-only
    # replies) — the validator treats empty as a no-op rather than zero
    # repetition, since a session with no tool calls is not a loop.
    toolcall_blocks: tuple[dict, ...] = ()


class ExecutorGateway(Protocol):
    async def open_session(self, message: str) -> ExecutorResponse: ...

    async def continue_session(self, session_id: str, message: str) -> ExecutorResponse: ...

    async def close(self) -> None: ...


class ValidationPort(Protocol):
    async def validate(
        self,
        task: CompiledTask,
        run: TaskRun,
        response_text: str,
        *,
        toolcall_blocks: tuple[dict, ...] = (),
    ) -> ValidationReport: ...


class RunRepositoryPort(Protocol):
    def save_run(self, run: TaskRun) -> None: ...

    def append_event(self, run_id: str, event: object) -> None: ...


class TrajectoryArchivePort(Protocol):
    """Archive the remote agent's session trajectory for one run.

    Implementations must not raise: trajectory capture is auxiliary audit
    data and must never influence a run's outcome.
    """

    def archive(self, run_id: str, agent_id: str, session_id: str) -> None: ...


class Clock(Protocol):
    async def sleep(self, seconds: float) -> None: ...
