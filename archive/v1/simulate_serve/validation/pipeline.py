from __future__ import annotations

from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.task import AcceptanceCriterion, CompiledTask
from simulate_serve.domain.validation import CriterionResult, Verdict, aggregate_results

from .claims import extract_claims
from .deterministic import (
    ConstraintValidator,
    CountValidator,
    FieldValidator,
    FormatValidator,
    KeywordValidator,
    ToolRepetitiveValidator,
    UrlSyntaxValidator,
)
from .evidence_collector import EvidenceCollector
from .semantic_judge import SemanticJudge


class ValidationPipeline:
    def __init__(self, judge: SemanticJudge | None = None, evidence_collector: EvidenceCollector | None = None):
        self.judge = judge
        self.evidence_collector = evidence_collector
        self.validators = {
            "keyword": KeywordValidator(),
            "format": FormatValidator(),
            "fields": FieldValidator(),
            "count": CountValidator(),
            "url_syntax": UrlSyntaxValidator(),
            "constraint": ConstraintValidator(),
        }

    async def validate(
        self,
        task: CompiledTask,
        run: TaskRun,
        response_text: str,
        *,
        toolcall_blocks: tuple[dict, ...] | list[dict] = (),
    ):
        text = response_text.strip()
        required_ids = frozenset(item.criterion_id for item in task.criteria if item.required)
        if not text:
            results = tuple(
                CriterionResult(
                    criterion_id=item.criterion_id,
                    verdict=Verdict.ERROR,
                    reason_code="RESPONSE_EMPTY",
                    message="远端回复没有可见最终文本",
                )
                for item in task.criteria
            )
            return aggregate_results(results, required_ids)

        claims = extract_claims(text)
        results: list[CriterionResult] = []
        expensive: list[AcceptanceCriterion] = []
        semantic: list[AcceptanceCriterion] = []
        for criterion in task.criteria:
            if criterion.required_capabilities:
                expensive.append(criterion)
                continue
            validator = self.validators.get(criterion.validator)
            if validator:
                results.append(validator.validate(criterion, text))
            else:
                semantic.append(criterion)

        hard_failure = any(
            item.verdict is Verdict.FAIL and item.criterion_id in required_ids
            for item in results
        )
        if hard_failure:
            results.extend(
                CriterionResult(
                    criterion_id=item.criterion_id,
                    verdict=Verdict.INCONCLUSIVE,
                    reason_code="DEFERRED_AFTER_HARD_FAIL",
                    message="存在可先修复的确定性缺口，本轮延后昂贵验证",
                )
                for item in (*expensive, *semantic)
            )
        else:
            for criterion in expensive:
                if self.evidence_collector is None:
                    results.append(
                        CriterionResult(
                            criterion_id=criterion.criterion_id,
                            verdict=Verdict.INCONCLUSIVE,
                            reason_code="TOOL_UNAVAILABLE",
                            message=f"验证所需能力不可用：{', '.join(sorted(criterion.required_capabilities))}",
                        )
                    )
                else:
                    results.append(await self.evidence_collector.collect(task, run, criterion, claims))

        evidence_ids = {item.criterion_id for item in expensive}
        evidence_blocked = any(
            item.criterion_id in evidence_ids
            and item.criterion_id in required_ids
            and item.verdict is not Verdict.PASS
            for item in results
        )
        if semantic and not hard_failure:
            if evidence_blocked:
                results.extend(
                    CriterionResult(
                        criterion_id=item.criterion_id,
                        verdict=Verdict.INCONCLUSIVE,
                        reason_code="DEFERRED_AFTER_EVIDENCE_RESULT",
                        message="必选工具证据尚未通过，本轮延后语义判定",
                    )
                    for item in semantic
                )
            elif self.judge is None:
                results.extend(
                    CriterionResult(
                        criterion_id=item.criterion_id,
                        verdict=Verdict.INCONCLUSIVE,
                        reason_code="JUDGE_UNAVAILABLE",
                        message="该准则需要语义判定，但本地 Judge 不可用",
                    )
                    for item in semantic
                )
            else:
                results.extend(await self.judge.judge(task, text, tuple(semantic)))

        # Deterministic post-processor: tool-call repetition guard. Runs after
        # the text-only deterministic validators AND the semantic judge so it
        # can override any prior PASS / FAIL / INCONCLUSIVE on every criterion
        # (deterministic, evidence, semantic) with a TOOL_REPETITIVE verdict.
        # Threshold and toolcall snapshot come from the most recent executor
        # round (passed in via ``toolcall_blocks``); when ``toolcall_blocks`` is
        # empty the executor did not surface any tool calls this round and
        # the validator is a no-op.
        threshold = task.interaction_policy.tool_repetitive_threshold
        if toolcall_blocks and threshold >= 2:
            tool_rep = ToolRepetitiveValidator(toolcall_blocks, threshold)
            criteria_by_id = {criterion.criterion_id: criterion for criterion in task.criteria}
            overridden: list[CriterionResult] = []
            for item in results:
                criterion = criteria_by_id.get(item.criterion_id)
                if criterion is None:
                    overridden.append(item)
                    continue
                # Only override a verdict when the tool-repeat detector
                # actually fires; otherwise keep the existing result so other
                # validators (keyword, count, judge, etc.) still drive the
                # verdict.
                replacement = tool_rep.validate(criterion, text)
                overridden.append(replacement if replacement is not None else item)
            results = overridden

        # Preserve task criterion order regardless of validator execution path.
        by_id = {item.criterion_id: item for item in results}
        ordered_items: list[CriterionResult] = []
        for criterion in task.criteria:
            item = by_id[criterion.criterion_id]
            if item.verdict is Verdict.FAIL:
                retryable = criterion.remediation.owner == "executor" and criterion.remediation.retryable
                item = item.model_copy(update={"retryable": retryable})
            ordered_items.append(item)
        ordered = tuple(ordered_items)
        return aggregate_results(ordered, required_ids)
