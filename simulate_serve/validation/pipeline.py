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

    async def validate(self, task: CompiledTask, run: TaskRun, response_text: str):
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
