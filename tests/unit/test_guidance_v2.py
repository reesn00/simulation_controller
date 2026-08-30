from __future__ import annotations

import pytest

from simulate_serve.domain.task import AcceptanceCriterion, InteractionPolicy, RemediationSpec
from simulate_serve.domain.validation import CriterionResult, ValidationReport, Verdict
from simulate_serve.interaction.actor import DeterministicInteractionActor
from simulate_serve.interaction.models import InteractionContext


@pytest.mark.asyncio
async def test_deterministic_guidance_excludes_remediation_text_and_respects_gap_limit(compiled_task, source_ref) -> None:
    """P1-1: remediation guidance is internal repair semantics and must never
    surface as simulated-user speech; the variant/generic chain speaks instead."""
    criteria = (
        AcceptanceCriterion(
            criterion_id="task.one",
            description="first",
            remediation=RemediationSpec(owner="executor", guidance="请补充第一个缺口"),
            source=source_ref,
        ),
        AcceptanceCriterion(
            criterion_id="task.two",
            description="second",
            remediation=RemediationSpec(owner="executor", guidance="请补充第二个缺口"),
            source=source_ref,
        ),
        AcceptanceCriterion(
            criterion_id="task.environment",
            description="environment",
            remediation=RemediationSpec(owner="environment", guidance="内部工具坏了"),
            source=source_ref,
        ),
    )
    task = compiled_task.model_copy(
        update={
            "criteria": criteria,
            "interaction_policy": InteractionPolicy(max_gaps_per_turn=1, acknowledge_progress=True),
        }
    )
    report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(criterion_id="task.one", verdict=Verdict.FAIL, reason_code="ONE", message="one", retryable=True),
            CriterionResult(criterion_id="task.two", verdict=Verdict.FAIL, reason_code="TWO", message="two", retryable=True),
            CriterionResult(
                criterion_id="task.environment",
                verdict=Verdict.FAIL,
                reason_code="TOOL_ERROR",
                message="environment",
            ),
        ),
        retryable=True,
    )

    utterance = await DeterministicInteractionActor().create_followup(InteractionContext(task=task), report)

    assert "请补充第一个缺口" not in utterance.content
    assert "内部工具坏了" not in utterance.content
    assert utterance.content  # the fallback chain still produces a sendable turn
    assert "完整的修订结果" in utterance.content
    assert utterance.target_criteria == ("task.one",)
    assert utterance.source == "variants"


@pytest.mark.asyncio
async def test_deterministic_guidance_consumes_scenario_reason_policy(compiled_task, source_ref) -> None:
    criterion = AcceptanceCriterion(criterion_id="task.url", description="url", source=source_ref)
    task = compiled_task.model_copy(
        update={
            "criteria": (criterion,),
            "interaction_policy": InteractionPolicy(guidance_by_reason={"URL_MISSING": ("给我一个完整网址",)}),
        }
    )
    report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id="task.url",
                verdict=Verdict.FAIL,
                reason_code="URL_MISSING",
                message="missing",
                retryable=True,
            ),
        ),
        retryable=True,
    )

    utterance = await DeterministicInteractionActor().create_followup(InteractionContext(task=task), report)

    assert utterance.content.startswith("给我一个完整网址。")
    assert "完整的修订结果" in utterance.content
    assert utterance.variant_ids == ("URL_MISSING[0]",)


@pytest.mark.asyncio
async def test_guidance_only_targets_retryable_executor_failures(compiled_task, source_ref) -> None:
    criteria = (
        AcceptanceCriterion(
            criterion_id="task.nonretryable",
            description="cannot retry",
            remediation=RemediationSpec(owner="executor", guidance="不要追问这个", retryable=False),
            source=source_ref,
        ),
        AcceptanceCriterion(
            criterion_id="task.retryable",
            description="can retry",
            remediation=RemediationSpec(owner="executor", guidance="请修复可重试缺口", retryable=True),
            source=source_ref,
        ),
    )
    task = compiled_task.model_copy(
        update={
            "criteria": criteria,
            "interaction_policy": InteractionPolicy(
                max_gaps_per_turn=1,
                guidance_by_reason={"RETRYABLE": ("请修复可重试缺口",)},
            ),
        }
    )
    report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id="task.nonretryable",
                verdict=Verdict.FAIL,
                reason_code="NONRETRYABLE",
                message="cannot retry",
                retryable=False,
            ),
            CriterionResult(
                criterion_id="task.retryable",
                verdict=Verdict.FAIL,
                reason_code="RETRYABLE",
                message="can retry",
                retryable=True,
            ),
        ),
        retryable=True,
    )

    utterance = await DeterministicInteractionActor().create_followup(InteractionContext(task=task), report)

    assert "请修复可重试缺口" in utterance.content
    assert "不要追问这个" not in utterance.content
    assert utterance.target_criteria == ("task.retryable",)
