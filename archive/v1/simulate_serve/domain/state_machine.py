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
    # 终态: BatchRunner 重试 max_run_retries 次后 trajectory 仍不完整,
    # 落 ``RunFailure(code="COMPLETION_INCOMPLETE")`` 并标记本终态.
    # 与 INTERRUPTED 类似: 半跑半终态, 标记后 ``orchestration.batch_tracker``
    # 会从轮询中识别 (TERMINAL_STATES 包含).
    COMPLETION_INCOMPLETE = "completion_incomplete"

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
        RunState.COMPLETION_INCOMPLETE,
    }
)

# 非终态都可以跳到 CANCELLED / INTERRUPTED / COMPLETION_INCOMPLETE (兜底).
# COMPLETION_INCOMPLETE 与 INTERRUPTED 语义相近但来源不同:
#   - INTERRUPTED: 进程被杀 / 启动扫库兜底
#   - COMPLETION_INCOMPLETE: 主动判定 trajectory 不完整且重试耗尽
_NONTERMINAL_RECOVERY_TARGETS = frozenset(
    {RunState.CANCELLED, RunState.INTERRUPTED, RunState.COMPLETION_INCOMPLETE}
)


def _with_recovery(base: frozenset[RunState]) -> frozenset[RunState]:
    """给非终态的迁移集合追加兜底目标 (CANCELLED / INTERRUPTED / COMPLETION_INCOMPLETE)."""
    return base | _NONTERMINAL_RECOVERY_TARGETS


ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PENDING: _with_recovery(
        frozenset({RunState.PREPARING})
    ),
    RunState.PREPARING: _with_recovery(
        frozenset({RunState.GENERATING_OPENING, RunState.ACTOR_ERROR})
    ),
    RunState.GENERATING_OPENING: _with_recovery(
        frozenset({RunState.WAITING_EXECUTOR, RunState.ACTOR_ERROR})
    ),
    RunState.WAITING_EXECUTOR: _with_recovery(
        frozenset({RunState.VALIDATING, RunState.EXECUTOR_ERROR})
    ),
    RunState.VALIDATING: _with_recovery(
        frozenset(
            {
                RunState.SUCCESS,
                RunState.GENERATING_FOLLOWUP,
                RunState.GUIDE_EXHAUSTED,
                RunState.INCONCLUSIVE,
                RunState.VALIDATION_ERROR,
            }
        )
    ),
    RunState.GENERATING_FOLLOWUP: _with_recovery(
        frozenset({RunState.WAITING_EXECUTOR, RunState.ACTOR_ERROR})
    ),
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