from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum


class InvalidStateTransition(RuntimeError):
    pass


class RunState(str, Enum):
    PENDING = "pending"
    PREPARING = "preparing"
    GENERATING_OPENING = "generating_opening"
    WAITING_EXECUTOR = "waiting_executor"
    VALIDATING = "validating"
    GENERATING_FOLLOWUP = "generating_followup"
    SUCCESS = "success"
    GUIDE_EXHAUSTED = "guide_exhausted"
    INCONCLUSIVE = "inconclusive"
    VALIDATION_ERROR = "validation_error"
    EXECUTOR_ERROR = "executor_error"
    ACTOR_ERROR = "actor_error"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES


TERMINAL_STATES = frozenset(
    {
        RunState.SUCCESS,
        RunState.GUIDE_EXHAUSTED,
        RunState.INCONCLUSIVE,
        RunState.VALIDATION_ERROR,
        RunState.EXECUTOR_ERROR,
        RunState.ACTOR_ERROR,
        RunState.CANCELLED,
        RunState.INTERRUPTED,
    }
)

ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PENDING: frozenset({RunState.PREPARING, RunState.CANCELLED, RunState.INTERRUPTED}),
    RunState.PREPARING: frozenset({RunState.GENERATING_OPENING, RunState.ACTOR_ERROR, RunState.CANCELLED, RunState.INTERRUPTED}),
    RunState.GENERATING_OPENING: frozenset({RunState.WAITING_EXECUTOR, RunState.ACTOR_ERROR, RunState.CANCELLED, RunState.INTERRUPTED}),
    RunState.WAITING_EXECUTOR: frozenset({RunState.VALIDATING, RunState.EXECUTOR_ERROR, RunState.CANCELLED, RunState.INTERRUPTED}),
    RunState.VALIDATING: frozenset(
        {
            RunState.SUCCESS,
            RunState.GENERATING_FOLLOWUP,
            RunState.GUIDE_EXHAUSTED,
            RunState.INCONCLUSIVE,
            RunState.VALIDATION_ERROR,
            RunState.CANCELLED,
            RunState.INTERRUPTED,
        }
    ),
    RunState.GENERATING_FOLLOWUP: frozenset({RunState.WAITING_EXECUTOR, RunState.ACTOR_ERROR, RunState.CANCELLED, RunState.INTERRUPTED}),
}


class RunStateMachine:
    @staticmethod
    def transition(run: "TaskRun", target: RunState, event_type: str, detail: dict | None = None) -> None:
        from .run import RunEvent, TaskRun

        if not isinstance(run, TaskRun):
            raise TypeError("run must be TaskRun")
        allowed = ALLOWED_TRANSITIONS.get(run.state, frozenset())
        if target not in allowed:
            raise InvalidStateTransition(f"Illegal transition: {run.state.value} -> {target.value}")
        previous = run.state
        run.state = target
        run.state_events.append(
            RunEvent(event_type=event_type, from_state=previous, to_state=target, detail=detail or {})
        )
        if target.is_terminal:
            run.completed_at = datetime.now(UTC)
