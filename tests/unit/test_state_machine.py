import pytest

from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.state_machine import InvalidStateTransition, RunState, RunStateMachine


def test_happy_path_state_transitions() -> None:
    run = TaskRun(run_id="r1", task_id="T1", task_type="x")
    for state in (
        RunState.PREPARING,
        RunState.GENERATING_OPENING,
        RunState.WAITING_EXECUTOR,
        RunState.VALIDATING,
        RunState.SUCCESS,
    ):
        RunStateMachine.transition(run, state, state.value)
    assert run.is_terminal
    assert run.completed_at is not None
    assert len(run.state_events) == 5


def test_illegal_transition_fails() -> None:
    run = TaskRun(run_id="r1", task_id="T1", task_type="x")
    with pytest.raises(InvalidStateTransition):
        RunStateMachine.transition(run, RunState.SUCCESS, "skip")


@pytest.mark.parametrize("state", [RunState.PREPARING, RunState.WAITING_EXECUTOR, RunState.VALIDATING])
def test_interrupted_is_legal_from_non_terminal_state(state: RunState) -> None:
    run = TaskRun(run_id="r1", task_id="T1", task_type="x", state=state)
    RunStateMachine.transition(run, RunState.INTERRUPTED, "crash")
    assert run.state is RunState.INTERRUPTED
