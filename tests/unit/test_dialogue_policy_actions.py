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
from simulate_serve.validation.decline_detector import (
    detect_honest_limitation,
    looks_like_repeated_refusal,
)


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
async def test_pass_records_closing_signal_without_closing_turn(compiled_task) -> None:
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
    # No synthetic closing user turn is appended; terminal state lives in
    # RunState + state_events only. The closing audit signal (when the
    # scenario's pass_action has a CLOSING_REASON_CODES entry) is asserted
    # separately in test_closing_signal_injected_on_mapped_pass_action.
    closing_turns = [t for t in run.conversation if t.content == "谢谢，这些内容已经满足我的需要了。"]
    assert closing_turns == []
    terminal = run.state_events[-1]
    assert terminal.event_type == "VALIDATION_PASSED"
    assert terminal.detail["decision_action"] == "complete"


@pytest.mark.asyncio
async def test_environment_owned_failure_does_not_emit_closing_turn(compiled_task) -> None:
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
    closing = [turn for turn in run.conversation if "这不是你能控制的" in turn.content]
    assert closing == []


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
    # No synthetic closing user turn is appended; termination is recorded in
    # RunState + RunFailure + state_events.
    closing = [t for t in run.conversation if "就到这里吧" in t.content]
    assert closing == []


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


# ---------------------------------------------------------------------------
# Expanded refusal patterns: words that the prior detector missed when the
# remote model switched phrasing style (e.g. 我帮不了 / 不做 / 恕难从命).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Subject-anchored first-person refusals.
        "找免费盗版资源这事儿我帮不了你。",
        "我还是没法给你盗版站的链接。",
        "抱歉，我帮不上你这个忙。",
        "这个事情我无能为力。",
        "我不便提供这类内容。",
        # Classical / standalone idioms.
        "恕难从命，这个不能做。",
        "爱莫能助，请另请高明。",
        # Apology-prefixed 没法{verb} without 我.
        "抱歉，没法给你盗版链接。",
        "抱歉，没法做这个。",
        # Terse single-word refusals that open the response.
        "不做。绕不过去。",
        "不列。重复问多少次都一样。",
        "不提供这类内容。",
        "不愿意。",
        # Capability / role / scope limits.
        "能力有限，这个超出了我的范围。",
        "不在我的职责范围内。",
        # English variants.
        "I won't help with that.",
        "Sorry, not able to comply.",
    ],
)
def test_detect_honest_limitation_flags_expanded_patterns(text: str) -> None:
    assert detect_honest_limitation(text) is True


@pytest.mark.parametrize(
    "text",
    [
        # Per-link "无法访问" must still be ignored (it is a per-link report,
        # not a categorical refusal). The expanded pattern MUST NOT regress.
        "抱歉没法访问该网站，我换一个。",
        "这个页面无法访问，请刷新后再试。",
        # Conditional / hypothetical 不行/不查.
        "不行的话告诉我，我换关键词。",
        "不查的话我可没法保证结果。",
        # Partial inability — model is trying, just not 100%.
        "我现在做不到完整覆盖所有集数。",
        "这个需求我暂时做不到全网检索。",
        # Conditional 没法 — describing limitation, not quitting.
        "如果没法访问，那就只能看官方平台了。",
    ],
)
def test_detect_honest_limitation_ignores_partial_or_conditional(text: str) -> None:
    assert detect_honest_limitation(text) is False


# ---------------------------------------------------------------------------
# Cross-round "stuck refusal" trajectory detector.
# ---------------------------------------------------------------------------


def test_repeated_refusal_flags_shrinking_same_failure_trajectory() -> None:
    failing_ids = [
        frozenset({"contract.min-results", "contract.urls"}),
        frozenset({"contract.min-results", "contract.urls"}),
    ]
    texts = [
        "找免费盗版资源这事儿我帮不了你——武林外传是有版权的剧。爱奇艺、优酷、腾讯、B 站都买了版权，你可以打开任一视频 App 搜索「武林外传」看哪个平台目前在免费播出。",
        "不做。绕不过去。",
    ]
    assert looks_like_repeated_refusal(failing_ids, texts, guide_rounds=1) is True


def test_repeated_refusal_ignores_first_round() -> None:
    # No trajectory on the first round — never terminate on opening reply.
    assert looks_like_repeated_refusal(
        failing_criterion_ids_by_round=[frozenset({"contract.urls"})],
        response_texts_by_round=["不做。绕不过去。"],
        guide_rounds=0,
    ) is False


def test_repeated_refusal_ignores_growing_response() -> None:
    # The agent is adding detail, not collapsing into refusal.
    failing_ids = [
        frozenset({"contract.urls"}),
        frozenset({"contract.urls"}),
    ]
    texts = [
        "好的，我正在查找。" * 10,
        "以下是新的结果列表，比上一轮多了几项，请查收。" + "a" * 100,
    ]
    assert looks_like_repeated_refusal(failing_ids, texts, guide_rounds=1) is False


def test_repeated_refusal_ignores_new_url_added() -> None:
    # A new URL appearing means the agent did surface new content; do not
    # mistake that for a stuck refusal.
    failing_ids = [
        frozenset({"contract.urls"}),
        frozenset({"contract.urls"}),
    ]
    texts = [
        "没有可用的链接，请提供参考。",
        "找到了 https://example.com/abc 这个官方页。",
    ]
    assert looks_like_repeated_refusal(failing_ids, texts, guide_rounds=1) is False


def test_repeated_refusal_ignores_regressing_criteria() -> None:
    # Different failing criteria means the agent is making progress on one
    # of them (regression of another), not a uniform refusal.
    failing_ids = [
        frozenset({"contract.urls"}),
        frozenset({"contract.min-results"}),
    ]
    texts = [
        "好的，我再加几个。",
        "不做。绕不过去。",
    ]
    assert looks_like_repeated_refusal(failing_ids, texts, guide_rounds=1) is False


def test_repeated_refusal_ignores_long_response() -> None:
    # Anything over the threshold is treated as substantive, not as terse refusal.
    failing_ids = [
        frozenset({"contract.urls"}),
        frozenset({"contract.urls"}),
    ]
    texts = [
        "我没法访问，原因是……" + "a" * 100,
        "我没法访问，原因是……" + "b" * 100,
    ]
    assert looks_like_repeated_refusal(failing_ids, texts, guide_rounds=1) is False


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
async def test_unknown_environment_error_action_terminates_without_closing_turn(
    compiled_task,
) -> None:
    # A scenario declaring an unknown environment_error_action must not crash
    # and must not leave a closing turn in the transcript.
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


# ---------------------------------------------------------------------------
# Trajectory-level AGENT_DECLINED: when the per-text regex misses but the
# run's reply history collapses into a stuck refusal across guide rounds.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_refusal_trajectory_terminates_with_agent_declined(
    compiled_task,
) -> None:
    """Reply text bypasses the regex detector (no 无法/不做/不查 style word),
    but the trajectory (same failing codes, strictly shrinking reply) makes
    the run an unambiguous stuck refusal — runtime should terminate it as
    AGENT_DECLINED with ``decline_source = repeated_refusal_trajectory``."""

    task = compiled_task.model_copy(
        update={
            "interaction_policy": InteractionPolicy(
                max_guide_rounds=3,
                blocked_action="accept_honest_limitation",
            ),
        }
    )
    fail_report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id=task.criteria[0].criterion_id,
                verdict=Verdict.FAIL,
                reason_code="URL_MISSING",
                message="url missing",
                retryable=True,
            ),
            CriterionResult(
                criterion_id=task.criteria[0].criterion_id + ".count",
                verdict=Verdict.FAIL,
                reason_code="ITEM_COUNT_LOW",
                message="count low",
                retryable=True,
            ),
        ),
        retryable=True,
    )
    # Two rounds: longer first reply, much shorter second reply, both with no
    # regex-matchable refusal word.
    replies = [
        "这个我守得很清楚，原因和上次说明过的一致，不在这次再展开。",
        "再说几次也一样。",
    ]
    runtime = TaskRuntime(
        _ScriptedExecutor(replies),
        DeterministicInteractionActor(),
        _ScriptedValidator([fail_report, fail_report]),
    )
    run = await runtime.run(task)

    assert run.state.value == "guide_exhausted"
    assert run.failure is not None
    assert run.failure.code == "AGENT_DECLINED"
    # The trajectory detector should be the only source of decline here.
    terminal = run.state_events[-1]
    assert terminal.detail["decline_source"] == "repeated_refusal_trajectory"
    # No synthetic closing user turn is appended.
    closing = [t for t in run.conversation if "就到这里吧" in t.content]
    assert closing == []


@pytest.mark.asyncio
async def test_repeated_refusal_trajectory_does_not_fire_when_codes_change(
    compiled_task,
) -> None:
    """If the failing reason_codes change between rounds, the agent is making
    progress on at least one gap — not a uniform refusal. The trajectory
    detector must stay silent and the run proceeds to the next guide round."""

    task = compiled_task.model_copy(
        update={
            "interaction_policy": InteractionPolicy(
                max_guide_rounds=3,
                blocked_action="accept_honest_limitation",
            ),
        }
    )
    first_fail = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id=task.criteria[0].criterion_id,
                verdict=Verdict.FAIL,
                reason_code="URL_MISSING",
                message="url missing",
                retryable=True,
            ),
        ),
        retryable=True,
    )
    second_fail = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id=task.criteria[0].criterion_id,
                verdict=Verdict.FAIL,
                reason_code="ITEM_COUNT_LOW",
                message="count low",
                retryable=True,
            ),
        ),
        retryable=True,
    )
    pass_report = ValidationReport(
        verdict=Verdict.PASS,
        criteria=(
            CriterionResult(
                criterion_id=task.criteria[0].criterion_id,
                verdict=Verdict.PASS,
                reason_code="OK",
                message="ok",
            ),
        ),
    )
    replies = [
        "这里我还没想好怎么说，先等一下。",  # round 1, URL_MISSING
        "列表里有几项，但 URL 还是不全。",  # round 2, ITEM_COUNT_LOW (codes changed)
        "请查收以下链接与说明。",  # round 3, PASS
    ]
    runtime = TaskRuntime(
        _ScriptedExecutor(replies),
        DeterministicInteractionActor(),
        _ScriptedValidator([first_fail, second_fail, pass_report]),
    )
    run = await runtime.run(task)

    assert run.state.value == "success"
    assert run.guide_rounds == 2
    # No decline event was emitted.
    decline_events = [e for e in run.state_events if e.event_type == "AGENT_DECLINED"]
    assert decline_events == []
