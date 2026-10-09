from __future__ import annotations

from enum import Enum
import uuid

from pydantic import BaseModel, ConfigDict


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


class ValidationDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    retryable: bool = False


class CriterionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str
    verdict: Verdict
    reason_code: str
    message: str
    evidence_ids: tuple[str, ...] = ()
    retryable: bool = False


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    report_id: str = ""
    verdict: Verdict
    criteria: tuple[CriterionResult, ...]
    missing_items: tuple[str, ...] = ()
    retryable: bool = False
    diagnostics: tuple[ValidationDiagnostic, ...] = ()

    def model_post_init(self, __context: object) -> None:
        if not self.report_id:
            object.__setattr__(self, "report_id", f"vr_{uuid.uuid4().hex}")


def aggregate_results(results: tuple[CriterionResult, ...], required_ids: frozenset[str]) -> ValidationReport:
    required = [item for item in results if item.criterion_id in required_ids]
    verdict = Verdict.PASS
    for candidate in (Verdict.FAIL, Verdict.ERROR, Verdict.INCONCLUSIVE):
        if any(item.verdict is candidate for item in required):
            verdict = candidate
            break
    missing = tuple(item.message for item in required if item.verdict is not Verdict.PASS)
    retryable = verdict is Verdict.FAIL and any(item.retryable for item in required if item.verdict is Verdict.FAIL)
    return ValidationReport(verdict=verdict, criteria=results, missing_items=missing, retryable=retryable)
