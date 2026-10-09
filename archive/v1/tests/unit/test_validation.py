from __future__ import annotations

import pytest

from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.task import AcceptanceCriterion
from simulate_serve.domain.validation import CriterionResult, Verdict, aggregate_results
from simulate_serve.validation.deterministic import CountValidator, FieldValidator, FormatValidator, KeywordValidator, UrlSyntaxValidator
from simulate_serve.validation.pipeline import ValidationPipeline
from simulate_serve.validation.semantic_judge import ScriptedSemanticJudge


def criterion(source_ref, validator: str, **parameters) -> AcceptanceCriterion:
    return AcceptanceCriterion(
        criterion_id=f"test.{validator}",
        description=validator,
        validator=validator,
        parameters=parameters,
        source=source_ref,
    )


@pytest.mark.parametrize(
    "validator,item,text,verdict",
    [
        (KeywordValidator(), {"keywords": ["Alpha", "Beta"], "mode": "all"}, "alpha beta", Verdict.PASS),
        (KeywordValidator(), {"keywords": ["Alpha", "Beta"], "mode": "all"}, "alpha", Verdict.FAIL),
        (FormatValidator(), {"format": "json"}, '{"a": 1}', Verdict.PASS),
        (FormatValidator(), {"format": "list"}, "1. one\n2. two", Verdict.PASS),
        (FormatValidator(), {"format": "table"}, "|a|b|\n|---|---|\n|1|2|", Verdict.PASS),
        (FormatValidator(), {"format": "card"}, "name: demo\nurl: https://example.com", Verdict.PASS),
        (CountValidator(), {"min_chars": 5}, "12345", Verdict.PASS),
        (CountValidator(), {"min_items": 2}, "- one\n- two", Verdict.PASS),
        (UrlSyntaxValidator(), {"min_items": 1}, "see https://example.com/a", Verdict.PASS),
    ],
)
def test_deterministic_validators(source_ref, validator, item, text, verdict) -> None:
    name = validator.__class__.__name__.replace("Validator", "").lower()
    assert validator.validate(criterion(source_ref, name, **item), text).verdict is verdict


def test_field_validator_checks_json_paths(source_ref) -> None:
    item = criterion(source_ref, "fields", fields=["a.b"])
    assert FieldValidator().validate(item, '{"a": {"b": 1}}').verdict is Verdict.PASS
    assert FieldValidator().validate(item, '{"a": {}}').verdict is Verdict.FAIL


def test_aggregation_priority(source_ref) -> None:
    results = (
        CriterionResult(criterion_id="a", verdict=Verdict.ERROR, reason_code="E", message="e"),
        CriterionResult(criterion_id="b", verdict=Verdict.FAIL, reason_code="F", message="f", retryable=True),
        CriterionResult(criterion_id="c", verdict=Verdict.INCONCLUSIVE, reason_code="I", message="i"),
    )
    report = aggregate_results(results, frozenset({"a", "b", "c"}))
    assert report.verdict is Verdict.FAIL
    assert report.retryable


@pytest.mark.asyncio
async def test_semantic_without_judge_is_inconclusive(compiled_task, source_ref) -> None:
    task = compiled_task.model_copy(
        update={
            "criteria": (
                AcceptanceCriterion(criterion_id="semantic.one", description="semantic", validator="semantic", source=source_ref),
            )
        }
    )
    report = await ValidationPipeline().validate(
        task,
        TaskRun(run_id="r", task_id=task.task_id, task_type=task.task_type),
        "answer",
    )
    assert report.verdict is Verdict.INCONCLUSIVE


@pytest.mark.asyncio
async def test_scripted_semantic_judge_passes(compiled_task, source_ref) -> None:
    task = compiled_task.model_copy(
        update={
            "criteria": (
                AcceptanceCriterion(criterion_id="semantic.one", description="semantic", validator="semantic", source=source_ref),
            )
        }
    )
    report = await ValidationPipeline(judge=ScriptedSemanticJudge()).validate(
        task,
        TaskRun(run_id="r", task_id=task.task_id, task_type=task.task_type),
        "answer",
    )
    assert report.verdict is Verdict.PASS


@pytest.mark.asyncio
async def test_semantic_fail_retryability_comes_from_criterion_contract(compiled_task, source_ref) -> None:
    class NonRetryingJudge:
        async def judge(self, task, response_text, criteria):
            return tuple(
                CriterionResult(
                    criterion_id=item.criterion_id,
                    verdict=Verdict.FAIL,
                    reason_code="SEMANTIC_GAP",
                    message="missing",
                    retryable=False,
                )
                for item in criteria
            )

    task = compiled_task.model_copy(
        update={
            "criteria": (
                AcceptanceCriterion(
                    criterion_id="semantic.retryable",
                    description="semantic",
                    validator="semantic",
                    source=source_ref,
                ),
            )
        }
    )
    report = await ValidationPipeline(judge=NonRetryingJudge()).validate(
        task,
        TaskRun(run_id="r", task_id=task.task_id, task_type=task.task_type),
        "answer",
    )

    assert report.verdict is Verdict.FAIL
    assert report.retryable is True


@pytest.mark.asyncio
async def test_hard_failure_defers_semantic_judge(compiled_task, source_ref) -> None:
    class TrackingJudge:
        def __init__(self) -> None:
            self.calls = 0

        async def judge(self, task, response_text, criteria):
            self.calls += 1
            return ()

    judge = TrackingJudge()
    task = compiled_task.model_copy(
        update={
            "criteria": (
                AcceptanceCriterion(
                    criterion_id="semantic.expensive",
                    description="semantic",
                    validator="semantic",
                    source=source_ref,
                ),
                AcceptanceCriterion(
                    criterion_id="hard.keyword",
                    description="must include Alpha",
                    validator="keyword",
                    parameters={"keywords": ["Alpha"], "mode": "all"},
                    source=source_ref,
                ),
            )
        }
    )

    report = await ValidationPipeline(judge=judge).validate(
        task,
        TaskRun(run_id="r", task_id=task.task_id, task_type=task.task_type),
        "missing",
    )

    assert report.verdict is Verdict.FAIL
    assert judge.calls == 0
    semantic = next(item for item in report.criteria if item.criterion_id == "semantic.expensive")
    assert semantic.verdict is Verdict.INCONCLUSIVE
    assert semantic.reason_code == "DEFERRED_AFTER_HARD_FAIL"


@pytest.mark.asyncio
async def test_failed_evidence_defers_semantic_judge(compiled_task, source_ref) -> None:
    class FailingEvidenceCollector:
        async def collect(self, task, run, criterion, claims):
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.FAIL,
                reason_code="EVIDENCE_FAILED",
                message="evidence failed",
            )

    class TrackingJudge:
        def __init__(self) -> None:
            self.calls = 0

        async def judge(self, task, response_text, criteria):
            self.calls += 1
            return ()

    judge = TrackingJudge()
    task = compiled_task.model_copy(
        update={
            "criteria": (
                AcceptanceCriterion(
                    criterion_id="evidence.required",
                    description="evidence",
                    validator="semantic",
                    required_capabilities=frozenset({"browser.navigate"}),
                    source=source_ref,
                ),
                AcceptanceCriterion(
                    criterion_id="semantic.expensive",
                    description="semantic",
                    validator="semantic",
                    source=source_ref,
                ),
            )
        }
    )

    report = await ValidationPipeline(
        judge=judge,
        evidence_collector=FailingEvidenceCollector(),
    ).validate(
        task,
        TaskRun(run_id="r", task_id=task.task_id, task_type=task.task_type),
        "answer",
    )

    assert report.verdict is Verdict.FAIL
    assert judge.calls == 0
    semantic = next(item for item in report.criteria if item.criterion_id == "semantic.expensive")
    assert semantic.reason_code == "DEFERRED_AFTER_EVIDENCE_RESULT"
