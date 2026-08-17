import re
import unicodedata

from simulate_serve.domain.task import AcceptanceCriterion
from simulate_serve.domain.validation import CriterionResult, Verdict


def normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", value).strip()


def passed(criterion: AcceptanceCriterion, message: str = "已满足") -> CriterionResult:
    return CriterionResult(criterion_id=criterion.criterion_id, verdict=Verdict.PASS, reason_code="PASSED", message=message)


def failed(criterion: AcceptanceCriterion, code: str, message: str, *, retryable: bool = True) -> CriterionResult:
    return CriterionResult(
        criterion_id=criterion.criterion_id,
        verdict=Verdict.FAIL,
        reason_code=code,
        message=message,
        retryable=retryable,
    )
