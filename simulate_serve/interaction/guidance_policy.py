from simulate_serve.domain.validation import CriterionResult, ValidationReport, Verdict
from simulate_serve.domain.task import CompiledTask


_NATURAL_MESSAGES = {
    "URL_MISSING": "你还没有给我可以直接打开的链接。",
    "FORMAT_TABLE_REQUIRED": "能不能按表格整理一下？",
    "SOURCE_EXCLUDED": "这个平台不符合我的要求，请换一个来源。",
    "KEYWORD_MISSING": "还有几个关键信息没说清楚。",
    "ITEM_COUNT_LOW": "数量还不够，请把结果补齐。",
}
_COMPLETE_REVISION_REQUEST = "请保留已经满足的内容，并给我一份包含全部要求的完整的修订结果。"
_REGRESSION_REQUEST = "你这次补充时遗漏了之前已经满足的内容，请把前后结果合并。"


def guidance_level_for_round(guide_rounds: int) -> str:
    if guide_rounds <= 0:
        return "L2"
    if guide_rounds == 1:
        return "L3"
    return "L4"


def select_guidance_gaps(task: CompiledTask, report: ValidationReport) -> tuple[CriterionResult, ...]:
    """Return only failures that the remote executor can actually repair."""
    criteria = {item.criterion_id: item for item in task.criteria}
    return tuple(
        item
        for item in report.criteria
        if item.verdict is Verdict.FAIL
        and item.retryable
        and item.criterion_id in criteria
        and criteria[item.criterion_id].remediation.owner == "executor"
        and criteria[item.criterion_id].remediation.retryable
    )[: task.interaction_policy.max_gaps_per_turn]


def ensure_complete_revision_request(
    task: CompiledTask,
    content: str,
    regressed_criteria: tuple[str, ...] = (),
) -> str:
    text = content.strip()
    if regressed_criteria and _REGRESSION_REQUEST not in text:
        if text and not text.endswith(("。", "？", "！")):
            text += "。"
        text += _REGRESSION_REQUEST
    if task.interaction_policy.preserve_satisfied_criteria and _COMPLETE_REVISION_REQUEST not in text:
        if text and not text.endswith(("。", "？", "！")):
            text += "。"
        text += _COMPLETE_REVISION_REQUEST
    return text


def deterministic_guidance(
    task: CompiledTask,
    report: ValidationReport,
    regressed_criteria: tuple[str, ...] = (),
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    criteria = {item.criterion_id: item for item in task.criteria}
    failed = select_guidance_gaps(task, report)
    messages: list[str] = []
    for item in failed:
        criterion = criteria[item.criterion_id]
        message = (
            task.interaction_policy.guidance_by_reason.get(item.reason_code)
            or criterion.remediation.guidance
            or _NATURAL_MESSAGES.get(item.reason_code)
            or item.message
        )
        if message:
            messages.append(message)
    content = "。".join(message.rstrip("。？") for message in messages if message).strip()
    if not content:
        fallback = task.interaction_policy.fallback_guidance
        content = fallback[0] if fallback else "这个结果还没满足我的需求，请再检查并补充。"
    if not content.endswith(("。", "？", "！")):
        content += "。"
    content = ensure_complete_revision_request(task, content, regressed_criteria)
    return content, tuple(item.reason_code for item in failed), tuple(item.criterion_id for item in failed)
