import re

from simulate_serve.domain.task import AcceptanceCriterion
from simulate_serve.domain.validation import CriterionResult, Verdict

from .common import normalize, passed

_NEGATION = re.compile(r"(?:不推荐|不要|排除|不符合|无法|不可用|拒绝|避免)")


class ConstraintValidator:
    def validate(self, criterion: AcceptanceCriterion, text: str) -> CriterionResult:
        excluded = [normalize(str(item)) for item in criterion.parameters.get("excluded_platforms", [])]
        normalized = normalize(text)
        violations: list[str] = []
        ambiguous: list[str] = []
        for platform in excluded:
            if not platform:
                continue
            for match in re.finditer(re.escape(platform), normalized):
                start = match.start()
                context = normalized[max(0, start - 16) : match.end() + 16]
                recommends = any(token in context for token in ("推荐", "可以", "链接", "http"))
                negated = bool(_NEGATION.search(context))
                if recommends:
                    violations.append(platform)
                    break
                if not negated:
                    ambiguous.append(platform)
                    break
        violations = list(dict.fromkeys(violations))
        ambiguous = [item for item in dict.fromkeys(ambiguous) if item not in violations]
        if violations:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.FAIL,
                reason_code="SOURCE_EXCLUDED",
                message=f"结果使用了被排除的平台：{' / '.join(violations)}",
                retryable=True,
            )
        if ambiguous:
            return CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.INCONCLUSIVE,
                reason_code="SOURCE_CONTEXT_AMBIGUOUS",
                message=f"无法确定是否在推荐被排除平台：{' / '.join(ambiguous)}",
            )
        return passed(criterion, "未使用被排除平台")
