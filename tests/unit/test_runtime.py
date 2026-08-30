from __future__ import annotations

from collections import deque

import pytest

from simulate_serve.application.ports import ExecutorResponse
from simulate_serve.application.errors import ExecutorPortError
from simulate_serve.application.run_task import TaskRuntime
from simulate_serve.application.run_guard import RuntimeGuardPolicy
from simulate_serve.domain.state_machine import RunState
from simulate_serve.domain.task import AcceptanceCriterion, InteractionPolicy, RemediationSpec
from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.validation import CriterionResult, ValidationReport, Verdict
from simulate_serve.interaction.actor import DeterministicInteractionActor
from simulate_serve.validation.pipeline import ValidationPipeline


class ScriptedExecutor:
    def __init__(self, responses: list[str]):
        self.responses = deque(responses)
        self.sessions: list[str | None] = []

    async def open_session(self, message: str) -> ExecutorResponse:
        self.sessions.append(None)
        return self._response()

    async def continue_session(self, session_id: str, message: str) -> ExecutorResponse:
        self.sessions.append(session_id)
        return self._response()

    def _response(self) -> ExecutorResponse:
        index = len(self.sessions)
        return ExecutorResponse(text=self.responses.popleft(), session_id="s1", remote_task_id=f"rt{index}", agent_id="a")

    async def close(self) -> None:
        return None


class ScriptedValidator:
    def __init__(self, verdicts: list[Verdict]):
        self.verdicts = deque(verdicts)

    async def validate(self, task, run, response) -> ValidationReport:
        verdict = self.verdicts.popleft()
        result = CriterionResult(
            criterion_id=task.criteria[0].criterion_id,
            verdict=verdict,
            reason_code="SCRIPTED",
            message="needs work" if verdict is not Verdict.PASS else "done",
            retryable=verdict is Verdict.FAIL,
        )
        return ValidationReport(
            verdict=verdict,
            criteria=(result,),
            missing_items=() if verdict is Verdict.PASS else (result.message,),
            retryable=verdict is Verdict.FAIL,
        )


@pytest.mark.asyncio
async def test_runtime_first_turn_success(compiled_task) -> None:
    executor = ScriptedExecutor(["done"])
    run = await TaskRuntime(executor, DeterministicInteractionActor(), ScriptedValidator([Verdict.PASS])).run(compiled_task)
    assert run.state is RunState.SUCCESS
    assert run.guide_rounds == 0
    assert run.executor_turns == 1


@pytest.mark.asyncio
async def test_runtime_records_explicit_rerun_relationship(compiled_task) -> None:
    run = await TaskRuntime(
        ScriptedExecutor(["done"]),
        DeterministicInteractionActor(),
        ScriptedValidator([Verdict.PASS]),
    ).run(compiled_task, rerun_of="previous-run")
    assert run.rerun_of == "previous-run"


@pytest.mark.asyncio
async def test_runtime_followup_then_success(compiled_task) -> None:
    executor = ScriptedExecutor(["partial", "done"])
    run = await TaskRuntime(executor, DeterministicInteractionActor(), ScriptedValidator([Verdict.FAIL, Verdict.PASS])).run(compiled_task)
    assert run.state is RunState.SUCCESS
    assert run.guide_rounds == 1
    assert executor.sessions == [None, "s1"]
    assert [turn.role for turn in run.conversation] == ["user", "assistant", "user", "assistant", "user"]
    assert run.conversation[-1].content == "谢谢，这些内容已经满足我的需要了。"
    followup_event = next(item for item in run.state_events if item.event_type == "FOLLOWUP_CREATED")
    assert followup_event.detail["reason_codes"] == ["SCRIPTED"]
    assert followup_event.detail["target_criteria"] == [compiled_task.criteria[0].criterion_id]
    assert followup_event.detail["guidance_level"] == "L2"
    assert followup_event.detail["decision_action"] == "retry"


@pytest.mark.asyncio
async def test_runtime_requests_complete_revision_to_preserve_passed_criteria(compiled_task, source_ref) -> None:
    criteria = (
        AcceptanceCriterion(
            criterion_id="task.alpha",
            description="contains alpha",
            validator="keyword",
            parameters={"keywords": ["Alpha"], "mode": "all"},
            remediation=RemediationSpec(guidance="请补充 Alpha"),
            source=source_ref,
        ),
        AcceptanceCriterion(
            criterion_id="task.beta",
            description="contains beta",
            validator="keyword",
            parameters={"keywords": ["Beta"], "mode": "all"},
            remediation=RemediationSpec(guidance="请补充 Beta"),
            source=source_ref,
        ),
    )
    task = compiled_task.model_copy(
        update={
            "criteria": criteria,
            "interaction_policy": InteractionPolicy(max_guide_rounds=1, preserve_satisfied_criteria=True),
        }
    )
    executor = ScriptedExecutor(["Alpha", "Alpha Beta"])

    run = await TaskRuntime(executor, DeterministicInteractionActor(), ValidationPipeline()).run(task)

    assert run.state is RunState.SUCCESS
    assert "完整的修订结果" in run.conversation[2].content
    assert run.validation_rounds[0].criteria[0].verdict is Verdict.PASS
    assert all(item.verdict is Verdict.PASS for item in run.validation_rounds[1].criteria)


@pytest.mark.asyncio
async def test_runtime_detects_regressed_criteria_and_requests_merged_answer(compiled_task, source_ref) -> None:
    criteria = (
        AcceptanceCriterion(
            criterion_id="task.alpha",
            description="contains alpha",
            validator="keyword",
            parameters={"keywords": ["Alpha"], "mode": "all"},
            remediation=RemediationSpec(guidance="请补充 Alpha"),
            source=source_ref,
        ),
        AcceptanceCriterion(
            criterion_id="task.beta",
            description="contains beta",
            validator="keyword",
            parameters={"keywords": ["Beta"], "mode": "all"},
            remediation=RemediationSpec(guidance="请补充 Beta"),
            source=source_ref,
        ),
    )
    task = compiled_task.model_copy(
        update={
            "criteria": criteria,
            "interaction_policy": InteractionPolicy(max_guide_rounds=2, preserve_satisfied_criteria=True),
        }
    )
    executor = ScriptedExecutor(["Alpha", "Beta", "Alpha Beta"])

    run = await TaskRuntime(executor, DeterministicInteractionActor(), ValidationPipeline()).run(task)

    assert run.state is RunState.SUCCESS
    regression_followup = run.conversation[4].content
    assert "遗漏了之前已经满足的内容" in regression_followup
    assert "完整的修订结果" in regression_followup
    followup_events = [item for item in run.state_events if item.event_type == "FOLLOWUP_CREATED"]
    assert followup_events[1].detail["regressed_criteria"] == ["task.alpha"]


@pytest.mark.asyncio
async def test_runtime_exhausts_exact_round_limit(compiled_task) -> None:
    executor = ScriptedExecutor(["bad", "bad", "bad"])
    validator = ScriptedValidator([Verdict.FAIL, Verdict.FAIL, Verdict.FAIL])
    run = await TaskRuntime(executor, DeterministicInteractionActor(), validator).run(compiled_task)
    assert run.state is RunState.GUIDE_EXHAUSTED
    assert run.guide_rounds == compiled_task.interaction_policy.max_guide_rounds


@pytest.mark.asyncio
async def test_runtime_breaks_repeated_response_loop(compiled_task) -> None:
    task = compiled_task.model_copy(
        update={"interaction_policy": InteractionPolicy(max_guide_rounds=5)}
    )
    run = await TaskRuntime(
        ScriptedExecutor(["same answer", "same answer", "same answer"]),
        DeterministicInteractionActor(),
        ScriptedValidator([Verdict.FAIL, Verdict.FAIL, Verdict.FAIL]),
        guard_policy=RuntimeGuardPolicy(max_identical_responses=3, max_identical_failures=0),
    ).run(task)

    assert run.state is RunState.GUIDE_EXHAUSTED
    assert run.guide_rounds == 2
    assert run.failure is not None
    assert run.failure.code == "RESPONSE_LOOP_DETECTED"
    assert run.state_events[-1].detail["decision_action"] == "stop_guard"
    assert run.state_events[-1].detail["guard_code"] == "RESPONSE_LOOP_DETECTED"


@pytest.mark.asyncio
async def test_runtime_stops_when_elapsed_budget_is_exhausted(compiled_task) -> None:
    task = compiled_task.model_copy(
        update={"interaction_policy": InteractionPolicy(max_guide_rounds=5)}
    )
    run = await TaskRuntime(
        ScriptedExecutor(["partial"]),
        DeterministicInteractionActor(),
        ScriptedValidator([Verdict.FAIL]),
        guard_policy=RuntimeGuardPolicy(
            max_identical_responses=0,
            max_identical_failures=0,
            max_elapsed_seconds=0,
        ),
    ).run(task)

    assert run.state is RunState.GUIDE_EXHAUSTED
    assert run.guide_rounds == 0
    assert run.failure is not None
    assert run.failure.code == "TIME_BUDGET_EXHAUSTED"


@pytest.mark.asyncio
async def test_runtime_records_escalating_guidance_levels(compiled_task) -> None:
    task = compiled_task.model_copy(
        update={"interaction_policy": InteractionPolicy(max_guide_rounds=3)}
    )
    run = await TaskRuntime(
        ScriptedExecutor(["one", "two", "done"]),
        DeterministicInteractionActor(),
        ScriptedValidator([Verdict.FAIL, Verdict.FAIL, Verdict.PASS]),
    ).run(task)

    levels = [
        event.detail["guidance_level"]
        for event in run.state_events
        if event.event_type == "FOLLOWUP_CREATED"
    ]
    assert levels == ["L2", "L3"]


def test_deferred_criterion_is_not_reported_as_regression(compiled_task) -> None:
    run = TaskRun(run_id="r", task_id=compiled_task.task_id, task_type=compiled_task.task_type)
    criterion_id = compiled_task.criteria[0].criterion_id
    run.validation_rounds = [
        ValidationReport(
            verdict=Verdict.FAIL,
            criteria=(
                CriterionResult(
                    criterion_id=criterion_id,
                    verdict=Verdict.PASS,
                    reason_code="PASSED",
                    message="done",
                ),
            ),
        ),
        ValidationReport(
            verdict=Verdict.FAIL,
            criteria=(
                CriterionResult(
                    criterion_id=criterion_id,
                    verdict=Verdict.INCONCLUSIVE,
                    reason_code="DEFERRED_AFTER_HARD_FAIL",
                    message="deferred",
                ),
            ),
        ),
    ]

    context = TaskRuntime._context(compiled_task, run)

    assert context.regressed_criteria == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict,state",
    [(Verdict.ERROR, RunState.VALIDATION_ERROR), (Verdict.INCONCLUSIVE, RunState.INCONCLUSIVE)],
)
async def test_runtime_validation_terminal_states(compiled_task, verdict: Verdict, state: RunState) -> None:
    run = await TaskRuntime(
        ScriptedExecutor(["answer"]),
        DeterministicInteractionActor(),
        ScriptedValidator([verdict]),
    ).run(compiled_task)
    assert run.state is state


@pytest.mark.asyncio
async def test_runtime_records_remote_ids_from_poll_failure(compiled_task) -> None:
    class PollFailingExecutor:
        async def open_session(self, message: str) -> ExecutorResponse:
            raise ExecutorPortError(
                "poll failed",
                stage="poll",
                remote_task_id="accepted-task",
                remote_session_id="accepted-session",
                agent_id="agent-a",
            )

    run = await TaskRuntime(
        PollFailingExecutor(),
        DeterministicInteractionActor(),
        ScriptedValidator([Verdict.PASS]),
    ).run(compiled_task)

    assert run.state is RunState.EXECUTOR_ERROR
    assert run.remote_task_ids == ["accepted-task"]
    assert run.remote_session_id == "accepted-session"
    assert run.remote_agent_id == "agent-a"
