"""Behavioural tests for the T2/T3 reason_code emission tracks.

- T2: the semantic judge payload narrows FAIL codes to the scenario's own
  guidance_policy vocabulary (`preferred_reason_codes`).
- T3: a run terminating with a mapped closing action carries a behaviour
  label (closing_reason_code / closing_target_criterion) in the terminal
  decision detail — without touching validation results.
"""
from __future__ import annotations

import pytest

from simulate_serve.application.ports import ExecutorResponse
from simulate_serve.application.run_task import TaskRuntime
from simulate_serve.domain.task import AcceptanceCriterion, InteractionPolicy, RemediationSpec
from simulate_serve.domain.validation import CriterionResult, ValidationReport, Verdict
from simulate_serve.interaction.actor import DeterministicInteractionActor
from simulate_serve.validation.reason_codes import SEMANTIC_REASON_CODE_HINTS
from simulate_serve.validation.semantic_judge import judge_payload
from test_dialogue_policy_actions import _ScriptedExecutor, _ScriptedValidator


def _pass_report(criterion_id: str) -> ValidationReport:
    return ValidationReport(
        verdict=Verdict.PASS,
        criteria=(CriterionResult(criterion_id=criterion_id, verdict=Verdict.PASS, reason_code="OK", message="ok"),),
    )


# ---------------------------------------------------------------------------
# T2: judge payload
# ---------------------------------------------------------------------------


def test_judge_payload_prefers_scenario_guidance_semantic_codes(compiled_task) -> None:
    task = compiled_task.model_copy(
        update={
            "interaction_policy": InteractionPolicy(
                guidance_by_reason={
                    "FABRICATION_DETECTED": ("不要编造",),
                    "URL_MISSING": ("请给链接",),  # deterministic track, judge must not be offered it
                    "POLICY_BLOCKED": ("闭合力不足",),  # closing track, judge must not be offered it
                }
            )
        }
    )
    payload = judge_payload(task, "回复内容", task.criteria)
    assert payload["preferred_reason_codes"] == {
        "FABRICATION_DETECTED": SEMANTIC_REASON_CODE_HINTS["FABRICATION_DETECTED"],
    }


def test_judge_payload_without_semantic_guidance_is_empty(compiled_task) -> None:
    payload = judge_payload(compiled_task, "回复内容", compiled_task.criteria)
    assert payload["preferred_reason_codes"] == {}


# ---------------------------------------------------------------------------
# T3: closing signal
# ---------------------------------------------------------------------------


def _policy_boundary_task(compiled_task) -> object:
    return compiled_task.model_copy(
        update={
            "scenario_id": "policy_boundary",
            "criteria": (
                AcceptanceCriterion(
                    criterion_id="policy.boundary-clear",
                    description="decline clearly",
                    remediation=RemediationSpec(owner="executor", guidance="x"),
                    source=compiled_task.criteria[0].source,
                ),
            ),
            "interaction_policy": InteractionPolicy(pass_action="decline_and_offer_alternative"),
        }
    )


@pytest.mark.asyncio
async def test_closing_signal_injected_on_mapped_pass_action(compiled_task) -> None:
    task = _policy_boundary_task(compiled_task)
    runtime = TaskRuntime(
        _ScriptedExecutor(["我无法协助该请求，可以改用……"]),
        DeterministicInteractionActor(),
        _ScriptedValidator([_pass_report("policy.boundary-clear")]),
    )
    run = await runtime.run(task)

    assert run.state.value == "success"
    # No synthetic closing user turn is appended; the closing signal lives in
    # the terminal decision detail only.
    closing = [t for t in run.conversation if "谢谢" in t.content]
    assert closing == []
    terminal = run.state_events[-1]
    assert terminal.detail["decision_action"] == "complete"
    assert terminal.detail["closing_reason_code"] == "POLICY_BLOCKED"
    assert terminal.detail["closing_target_criterion"] == "policy.boundary-clear"
    # Validation results are untouched by the closing injection.
    assert run.validation_rounds[-1].criteria[0].reason_code == "OK"


@pytest.mark.asyncio
async def test_closing_signal_absent_for_unmapped_combination(compiled_task) -> None:
    # pass_action known but no closing map entry for this scenario_id.
    task = compiled_task.model_copy(
        update={
            "scenario_id": "aggregate_and_compare",
            "interaction_policy": InteractionPolicy(pass_action="decline_and_offer_alternative"),
        }
    )
    runtime = TaskRuntime(
        _ScriptedExecutor(["answer"]),
        DeterministicInteractionActor(),
        _ScriptedValidator([_pass_report("test.text")]),
    )
    run = await runtime.run(task)

    assert run.state.value == "success"
    assert "closing_reason_code" not in run.state_events[-1].detail
    assert "closing_target_criterion" not in run.state_events[-1].detail


@pytest.mark.asyncio
async def test_default_pass_action_has_no_closing_signal(compiled_task) -> None:
    runtime = TaskRuntime(
        _ScriptedExecutor(["answer"]),
        DeterministicInteractionActor(),
        _ScriptedValidator([_pass_report("test.text")]),
    )
    run = await runtime.run(compiled_task)

    assert run.state.value == "success"
    assert "closing_reason_code" not in run.state_events[-1].detail
