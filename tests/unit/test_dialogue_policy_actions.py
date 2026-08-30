from __future__ import annotations

from collections import deque

import pytest

from simulate_serve.application.ports import ExecutorResponse
from simulate_serve.application.run_task import TaskRuntime
from simulate_serve.domain.task import AcceptanceCriterion, InteractionPolicy, RemediationSpec
from simulate_serve.domain.validation import CriterionResult, ValidationReport, Verdict
from simulate_serve.interaction.actor import DeterministicInteractionActor
from simulate_serve.interaction.content_policy import leaks_internal_rules
from simulate_serve.interaction.models import InteractionContext
from simulate_serve.interaction.prompt_builder import build_followup_prompt
from simulate_serve.validation.decline_detector import detect_honest_limitation


class _ScriptedExecutor:
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
        return ExecutorResponse(
            text=self.responses.popleft(),
            session_id="s1",
            remote_task_id=f"rt{index}",
            agent_id="a",
        )

    async def close(self) -> None:
        return None


class _ScriptedValidator:
    def __init__(self, reports: list[ValidationReport]):
        self.reports = deque(reports)

    async def validate(self, task, run, response) -> ValidationReport:
        return self.reports.popleft()


def _single_criterion_task(compiled_task, owner: str = "executor"):
    return compiled_task.model_copy(
        update={
            "criteria": (
                AcceptanceCriterion(
                    criterion_id="t.only",
                    description="check",
                    remediation=RemediationSpec(owner=owner, guidance="x"),
                    source=compiled_task.criteria[0].source,
                ),
            ),
        }
    )


@pytest.mark.asyncio
async def test_pass_action_emits_thank_you_closing_turn(compiled_task) -> None:
    task = _single_criterion_task(compiled_task)
    pass_report = ValidationReport(
        verdict=Verdict.PASS,
        criteria=(CriterionResult(criterion_id="t.only", verdict=Verdict.PASS, reason_code="OK", message="ok"),),
    )
    runtime = TaskRuntime(
        _ScriptedExecutor(["answer"]),
        DeterministicInteractionActor(),
        _ScriptedValidator([pass_report]),
    )
    run = await runtime.run(task)

    assert run.state is Verdict.PASS or run.state.value == "success"
    assert run.conversation[-1].role == "user"
    assert run.conversation[-1].content == "谢谢，这些内容已经满足我的需要了。"


@pytest.mark.asyncio
async def test_environment_owned_failure_emits_blame_free_closing_turn(compiled_task) -> None:
    task = _single_criterion_task(compiled_task, owner="environment")
    report = ValidationReport(
        verdict=Verdict.INCONCLUSIVE,
        criteria=(
            CriterionResult(
                criterion_id="t.only",
                verdict=Verdict.INCONCLUSIVE,
                reason_code="BROWSER_BARRIER",
                message="browser blocked",
            ),
        ),
    )
    runtime = TaskRuntime(
        _ScriptedExecutor(["answer"]),
        DeterministicInteractionActor(),
        _ScriptedValidator([report]),
    )
    run = await runtime.run(task)

    assert run.state.value == "inconclusive"
    assert run.conversation[-1].role == "user"
    assert "这不是你能控制的" in run.conversation[-1].content


@pytest.mark.asyncio
async def test_executor_owned_failure_does_not_emit_environment_closing(compiled_task) -> None:
    task = _single_criterion_task(compiled_task, owner="executor")
    report = ValidationReport(
        verdict=Verdict.INCONCLUSIVE,
        criteria=(
            CriterionResult(
                criterion_id="t.only",
                verdict=Verdict.INCONCLUSIVE,
                reason_code="UNKNOWN",
                message="x",
            ),
        ),
    )
    runtime = TaskRuntime(
        _ScriptedExecutor(["answer"]),
        DeterministicInteractionActor(),
        _ScriptedValidator([report]),
    )
    run = await runtime.run(task)

    assert run.state.value == "inconclusive"
    closing = [turn for turn in run.conversation if turn.content == "好的，我知道这不是你能控制的，我们先到这里。"]
    assert closing == []


@pytest.mark.asyncio
async def test_agent_declined_terminates_after_failed_validation(compiled_task) -> None:
    task = compiled_task.model_copy(
        update={
            "interaction_policy": InteractionPolicy(blocked_action="accept_honest_limitation"),
        }
    )
    fail_report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id=compiled_task.criteria[0].criterion_id,
                verdict=Verdict.FAIL,
                reason_code="X",
                message="missing",
                retryable=True,
            ),
        ),
        retryable=True,
    )
    runtime = TaskRuntime(
        _ScriptedExecutor(["抱歉，我无法完成这个任务。"]),
        DeterministicInteractionActor(),
        _ScriptedValidator([fail_report]),
    )
    run = await runtime.run(task)

    assert run.state.value == "guide_exhausted"
    assert run.failure is not None
    assert run.failure.code == "AGENT_DECLINED"
    assert run.validation_rounds  # 放弃判定发生在验证之后，验证记录保留
    assert run.conversation[-1].content == "没关系，你能说明做不到的原因也很好，就到这里吧。"


@pytest.mark.asyncio
async def test_pass_beats_decline_phrasing(compiled_task) -> None:
    # 验证通过时不再做放弃判定：含“无法/需要确认”措辞的正确回复不会被终止。
    pass_report = ValidationReport(
        verdict=Verdict.PASS,
        criteria=(CriterionResult(criterion_id=compiled_task.criteria[0].criterion_id, verdict=Verdict.PASS, reason_code="OK", message="ok"),),
    )
    runtime = TaskRuntime(
        _ScriptedExecutor(["抱歉，我无法访问该网站，需要您先提供授权。"]),
        DeterministicInteractionActor(),
        _ScriptedValidator([pass_report]),
    )
    run = await runtime.run(compiled_task)

    assert run.state.value == "success"


@pytest.mark.asyncio
async def test_no_decline_check_continues_instead_of_terminating(compiled_task) -> None:
    task = compiled_task.model_copy(
        update={"interaction_policy": InteractionPolicy(blocked_action="no_decline_check")}
    )
    fail_report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id=compiled_task.criteria[0].criterion_id,
                verdict=Verdict.FAIL,
                reason_code="X",
                message="missing",
                retryable=True,
            ),
        ),
        retryable=True,
    )
    inconclusive_report = ValidationReport(
        verdict=Verdict.INCONCLUSIVE,
        criteria=(
            CriterionResult(
                criterion_id=compiled_task.criteria[0].criterion_id,
                verdict=Verdict.INCONCLUSIVE,
                reason_code="X",
                message="x",
            ),
        ),
    )
    runtime = TaskRuntime(
        _ScriptedExecutor(["抱歉，我无法完成这个任务。", "结果如下，请查收。"]),
        DeterministicInteractionActor(),
        _ScriptedValidator([fail_report, inconclusive_report]),
    )
    run = await runtime.run(task)

    # no_decline_check 场景：放弃话术不终止 run，而是进入引导追问。
    assert run.state.value == "inconclusive"
    assert run.guide_rounds == 1


@pytest.mark.parametrize(
    "text",
    [
        "抱歉，我无法完成这个任务。",
        "没有权限访问该资源，任务到此为止。",
        "I cannot access this page; out of my scope.",
    ],
)
def test_detect_honest_limitation_flags_declarations(text: str) -> None:
    assert detect_honest_limitation(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "这是结果列表，请查收。",
        "I can help with that. Here is the answer.",
        "需要您先确认要哪一部《功夫》，我再继续搜索。",
        "该网站无法访问，我给您换一个来源。",
    ],
)
def test_detect_honest_limitation_ignores_normal_replies(text: str) -> None:
    assert detect_honest_limitation(text) is False


@pytest.mark.parametrize(
    "text,expected",
    [
        ("准则 c1 没满足", True),
        ("verify with the validator API", True),
        ("请你按要求补全结果", False),
    ],
)
def test_leaks_internal_rules(text: str, expected: bool) -> None:
    assert leaks_internal_rules(text) is expected


def test_followup_prompt_omits_progress_when_acknowledge_disabled(compiled_task) -> None:
    task = compiled_task.model_copy(
        update={"interaction_policy": InteractionPolicy(acknowledge_progress=False)}
    )
    context = InteractionContext(task=task)
    report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id=task.criteria[0].criterion_id,
                verdict=Verdict.FAIL,
                reason_code="X",
                message="missing",
                retryable=True,
            ),
        ),
        retryable=True,
    )
    prompt = build_followup_prompt(context, report)
    # The instruction text still mentions "已经满足的内容" (要求对方保留已经满足的内容),
    # so we only check that the "已经满足的内容：" progress-section header is gone.
    assert "已经满足的内容：" not in prompt


def test_followup_prompt_drops_no_leak_rule_when_disabled(compiled_task) -> None:
    task = compiled_task.model_copy(
        update={"interaction_policy": InteractionPolicy(never_expose_internal_rules=False)}
    )
    context = InteractionContext(task=task)
    report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id=task.criteria[0].criterion_id,
                verdict=Verdict.FAIL,
                reason_code="X",
                message="missing",
                retryable=True,
            ),
        ),
        retryable=True,
    )
    prompt = build_followup_prompt(context, report)
    assert "不得提及验证器" not in prompt


@pytest.mark.asyncio
async def test_closing_message_unknown_action_is_silently_skipped(compiled_task) -> None:
    # A scenario declaring an unknown action must not crash; the runtime
    # simply does not append a closing turn.
    task = compiled_task.model_copy(
        update={
            "criteria": (
                AcceptanceCriterion(
                    criterion_id="t.only",
                    description="check",
                    remediation=RemediationSpec(owner="environment", guidance="x"),
                    source=compiled_task.criteria[0].source,
                ),
            ),
            "interaction_policy": InteractionPolicy(environment_error_action="custom_action"),
        }
    )
    report = ValidationReport(
        verdict=Verdict.INCONCLUSIVE,
        criteria=(
            CriterionResult(
                criterion_id="t.only",
                verdict=Verdict.INCONCLUSIVE,
                reason_code="X",
                message="x",
            ),
        ),
    )
    runtime = TaskRuntime(
        _ScriptedExecutor(["answer"]),
        DeterministicInteractionActor(),
        _ScriptedValidator([report]),
    )
    run = await runtime.run(task)
    closing = [turn for turn in run.conversation if turn.content.startswith("好的")]
    assert closing == []
