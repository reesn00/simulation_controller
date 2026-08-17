from __future__ import annotations

from dataclasses import dataclass
import re
import time

from simulate_serve.domain.run import TaskRun
from simulate_serve.domain.validation import Verdict


_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class RuntimeGuardPolicy:
    """Deterministic loop and elapsed-time limits for retryable runs."""

    max_identical_responses: int = 3
    max_identical_failures: int = 3
    max_elapsed_seconds: float | None = None


@dataclass(frozen=True)
class RuntimeGuardResult:
    code: str
    message: str
    detail: dict[str, object]


def evaluate_runtime_guard(
    run: TaskRun,
    policy: RuntimeGuardPolicy,
    *,
    started_monotonic: float,
) -> RuntimeGuardResult | None:
    if policy.max_elapsed_seconds is not None:
        elapsed = time.monotonic() - started_monotonic
        if elapsed >= policy.max_elapsed_seconds:
            return RuntimeGuardResult(
                code="TIME_BUDGET_EXHAUSTED",
                message=f"运行时间达到预算上限 {policy.max_elapsed_seconds:g} 秒",
                detail={"elapsed_seconds": elapsed, "limit_seconds": policy.max_elapsed_seconds},
            )

    responses = [
        _normalize(turn.content)
        for turn in run.conversation
        if turn.role == "assistant"
    ]
    if _has_trailing_repeat(responses, policy.max_identical_responses):
        return RuntimeGuardResult(
            code="RESPONSE_LOOP_DETECTED",
            message=f"执行端连续 {policy.max_identical_responses} 轮返回相同内容",
            detail={"repeat_count": policy.max_identical_responses},
        )

    failures = [_failure_signature(report) for report in run.validation_rounds]
    if _has_trailing_repeat(failures, policy.max_identical_failures):
        return RuntimeGuardResult(
            code="FAILURE_LOOP_DETECTED",
            message=f"连续 {policy.max_identical_failures} 轮出现相同验证缺口",
            detail={"repeat_count": policy.max_identical_failures},
        )
    return None


def _normalize(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip().casefold()


def _failure_signature(report: object) -> str:
    criteria = getattr(report, "criteria", ())
    values = sorted(
        (
            str(item.criterion_id),
            str(item.reason_code),
            item.verdict.value,
        )
        for item in criteria
        if item.verdict is not Verdict.PASS
    )
    return repr(values)


def _has_trailing_repeat(values: list[str], threshold: int) -> bool:
    if threshold <= 0 or len(values) < threshold:
        return False
    trailing = values[-threshold:]
    return bool(trailing[0]) and len(set(trailing)) == 1
