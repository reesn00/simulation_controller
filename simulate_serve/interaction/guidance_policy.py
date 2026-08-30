from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict

from simulate_serve.domain.task import CompiledTask
from simulate_serve.domain.validation import CriterionResult, ValidationReport, Verdict

from .models import InteractionContext, UserUtterance


# Last-resort user phrasing, used when a reason_code has neither scenario
# variants nor a natural message. Deliberately user-owned wording ("我最初
# 说的"): it never quotes criterion descriptions or remediation guidance.
_GENERIC_GUIDANCE = "这个结果还没满足我最初说的要求，请对照我的要求再检查补充。"

# Deterministic codes that can never become guidance gaps: they appear on
# PASS/INCONCLUSIVE/ERROR results or are pipeline bookkeeping, so no user
# phrasing is needed for them.
NON_GAP_DETERMINISTIC_CODES = frozenset({
    "DEFERRED_AFTER_HARD_FAIL",
    "DEFERRED_AFTER_EVIDENCE_RESULT",
    "PASSED",
    "SCRIPTED_JUDGE",
    "EVIDENCE_CONFIRMED",
    "JUDGE_UNAVAILABLE",
    "JUDGE_ERROR",
})

_NATURAL_MESSAGES = {
    "URL_MISSING": "你还没有给我可以直接打开的链接。",
    "SOURCE_EXCLUDED": "这个平台不符合我的要求，请换一个来源。",
    "KEYWORD_MISSING": "还有几个关键信息没说清楚。",
    "ITEM_COUNT_LOW": "数量还不够，请把结果补齐。",
    "FORMAT_TABLE_REQUIRED": "能不能按表格整理一下？",
    "RESPONSE_EMPTY": "你回了个空，把结果发给我。",
    "TOOL_UNAVAILABLE": "本地验证不了就没法确认，你把情况说清楚。",
    "CHAR_COUNT_LOW": "说得太少了，把该讲的讲清楚。",
    "FIELD_MISSING": "还缺几个关键信息，补全。",
    "FORMAT_JSON_REQUIRED": "按 JSON 格式发我。",
    "FORMAT_JSON_OBJECT_REQUIRED": "要一个完整的 JSON 对象，重新整理一下。",
    "FORMAT_JSON_ARRAY_REQUIRED": "要一个 JSON 数组，重新整理一下。",
    "FORMAT_LIST_REQUIRED": "按清单列出来。",
    "FORMAT_CARD_REQUIRED": "按卡片的形式整理给我。",
    "FORMAT_UNSUPPORTED": "这个格式我没法用，换个格式发。",
    "KEYWORD_ANY_MISSING": "几处关键信息里还缺了点，补齐。",
    "KEYWORD_FORBIDDEN": "里面出现了不该出现的内容，去掉。",
    "SOURCE_CONTEXT_AMBIGUOUS": "来源说不清楚是哪个平台，标明白。",
    "EVIDENCE_COUNT_LOW": "确认能用的还是太少，继续验证再补。",
    "EVIDENCE_EMPTY": "什么都没验证到，实际打开看看再回我。",
    "EVIDENCE_NOT_CONFIRMED": "这个结果我这边确认不了，你实际核一遍再说。",
    "HTTP_STATUS_INVALID": "有链接已经打不开了，剔掉换新的。",
    "BROWSER_BARRIER": "这个页面还有登录或付费门槛，不符合要求，换一个。",
    "MEDIA_MISSING": "页面能开，但没看到能播的正片，再确认一下。",
    "MEDIA_PLAYBACK_UNCONFIRMED": "我还是不能确认视频真的开始播了，换个能当场验证的。",
    "TOOL_ERROR": "工具出问题就直说，别拿编的结果糊弄我。",
    "PARTIAL_FILES": "到底处理了多少个、放到哪里了？说清楚。",
    "PATH_CONFLICT": "有重名冲突吧？你打算怎么处理，先告诉我。",
    "UNCONFIRMED_DELETE": "删东西之前得先问我一声，说明影响。",
}

_COMPLETE_REVISION_REQUEST = "请保留已经满足的内容，并给我一份包含全部要求的完整的修订结果。"
_REGRESSION_REQUEST = "你这次补充时遗漏了之前已经满足的内容，请把前后结果合并。"

_LEVEL_ORDER: dict[str, int] = {"L2": 0, "L3": 1, "L4": 2}

_VERBOSITY_BUDGETS: dict[str, int] = {"concise": 60, "moderate": 120, "detailed": 200}
_VERBOSITY_ALIASES: dict[str, str] = {"medium": "moderate"}


class GuidanceDirective(BaseModel):
    """Internal repair instruction produced by the guidance policy.

    This is the policy half of the policy/surface split: everything here is
    harness-owned state for choosing *what* to tell the executor. Turning it
    into user speech happens in the surface realizers (LLM actor with the
    variant pool as fallback) — never by reading criterion text directly.
    """

    model_config = ConfigDict(frozen=True)

    gaps: tuple[CriterionResult, ...] = ()
    reason_codes: tuple[str, ...] = ()
    target_criteria: tuple[str, ...] = ()
    guidance_level: Literal["L2", "L3", "L4"] = "L2"
    emphasis: Literal["first", "repeat", "regress"] = "first"
    pass_ratio: float = 1.0
    repeated_criteria: tuple[str, ...] = ()
    regressed_criteria: tuple[str, ...] = ()
    verbosity_level: str = "moderate"
    verbosity_budget: int = 120


def normalize_verbosity(value: str | None) -> str:
    verbosity = (value or "moderate").strip().lower()
    verbosity = _VERBOSITY_ALIASES.get(verbosity, verbosity)
    return verbosity if verbosity in _VERBOSITY_BUDGETS else "moderate"


def verbosity_budget(value: str | None) -> int:
    return _VERBOSITY_BUDGETS[normalize_verbosity(value)]


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


def build_guidance_directive(context: InteractionContext, report: ValidationReport) -> GuidanceDirective:
    """Derive the turn's guidance state from the validation history.

    Level reacts to what the executor actually did (repeated failures,
    regressions, round progress) instead of only counting rounds, and is
    monotonic within a run: the simulated user's patience only decreases.
    """
    task = context.task
    gaps = select_guidance_gaps(task, report)
    passed = sum(1 for item in report.criteria if item.verdict is Verdict.PASS)
    failed = sum(1 for item in report.criteria if item.verdict is Verdict.FAIL)
    pass_ratio = passed / (passed + failed) if passed + failed else 1.0
    repeated = tuple(
        item.criterion_id
        for item in gaps
        if context.fail_streaks.get(item.criterion_id, 1) >= 2
    )
    # A first-time failure always stays at L2 (first mention, directional);
    # the overall-stall upgrade only applies once a guided round has already
    # happened, otherwise single-criterion tasks would never show an
    # L2 -> L3 -> L4 gradient.
    level = "L2"
    if repeated or (pass_ratio < 0.5 and context.guide_rounds >= 1):
        level = "L3"
    if context.regressed_criteria or any(
        context.fail_streaks.get(item.criterion_id, 1) >= 3 for item in gaps
    ):
        level = "L4"
    level = _max_level(level, context.previous_guidance_level)
    emphasis: Literal["first", "repeat", "regress"] = "first"
    if context.regressed_criteria:
        emphasis = "regress"
    elif repeated:
        emphasis = "repeat"
    verbosity = normalize_verbosity(task.persona.verbosity)
    return GuidanceDirective(
        gaps=gaps,
        reason_codes=tuple(item.reason_code for item in gaps),
        target_criteria=tuple(item.criterion_id for item in gaps),
        guidance_level=level,
        emphasis=emphasis,
        pass_ratio=pass_ratio,
        repeated_criteria=repeated,
        regressed_criteria=context.regressed_criteria,
        verbosity_level=verbosity,
        verbosity_budget=_VERBOSITY_BUDGETS[verbosity],
    )


def _max_level(current: str, previous: str | None) -> str:
    if previous not in _LEVEL_ORDER:
        return current
    return current if _LEVEL_ORDER[current] >= _LEVEL_ORDER[previous] else previous


def pick_guidance_variant(
    variants,
    run_id: str,
    code: str,
    guide_rounds: int,
    *,
    exclude: set[str] | None = None,
) -> tuple[str, str] | None:
    """Pick one phrasing from a reason code's variant pool.

    Deterministic in (run_id, code, guide_rounds): the same run replays to the
    same utterance, while different runs spread across the pool. Within a run
    the pool rotates by round and skips previously used sentences so the
    simulated user does not repeat herself.
    """
    pool = tuple(text.strip() for text in variants if isinstance(text, str) and text.strip())
    if not pool:
        return None
    seed = hashlib.sha256(f"{run_id}|{code}".encode("utf-8")).digest()
    start = int.from_bytes(seed[:4], "big") % len(pool)
    for offset in range(len(pool)):
        index = (start + guide_rounds + offset) % len(pool)
        text = pool[index]
        if exclude is None or text not in exclude:
            return text, f"{code}[{index}]"
    return None


def realize_guidance(
    task: CompiledTask,
    directive: GuidanceDirective,
    run_id: str,
    guide_rounds: int,
    *,
    exclude: set[str] | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Deterministic surface realization from the variant pool.

    Resolution per gap: scenario variants -> builtin natural message -> one
    generic user-owned sentence. Criterion descriptions, remediation guidance
    and judge messages are never read: this is the last-resort user turn when
    the language layer is unavailable, so it must already be persona-safe.
    """
    messages: list[str] = []
    used_ids: list[str] = []
    seen: set[str] = set(exclude or ())
    for item in directive.gaps:
        variants = task.interaction_policy.guidance_by_reason.get(item.reason_code) or ()
        picked = pick_guidance_variant(variants, run_id, item.reason_code, guide_rounds, exclude=seen)
        if picked is not None:
            text, variant_id = picked
        else:
            text = _NATURAL_MESSAGES.get(item.reason_code) or _GENERIC_GUIDANCE
            variant_id = item.reason_code
        if text in seen:
            continue
        seen.add(text)
        used_ids.append(variant_id)
        messages.append(text)
    content = "。".join(message.rstrip("。？") for message in messages).strip()
    if not content:
        fallback = task.interaction_policy.fallback_guidance
        # Rotate instead of always taking [0]; rounds advance the pool.
        content = fallback[guide_rounds % len(fallback)] if fallback else _GENERIC_GUIDANCE
    if not content.endswith(("。", "？", "！")):
        content += "。"
    return content, tuple(used_ids)


def variant_pool_utterance(context: InteractionContext, directive: GuidanceDirective) -> UserUtterance:
    """Build the fallback follow-up utterance from the variant pool."""
    exclude = {turn.content for turn in context.conversation if turn.role == "user"}
    content, variant_ids = realize_guidance(
        context.task, directive, context.run_id, context.guide_rounds, exclude=exclude
    )
    content = ensure_complete_revision_request(context.task, content, context.regressed_criteria)
    return UserUtterance(
        content=content,
        action="followup",
        reason_codes=directive.reason_codes,
        target_criteria=directive.target_criteria,
        guidance_level=directive.guidance_level,
        source="variants",
        variant_ids=variant_ids,
        emphasis=directive.emphasis,
        pass_ratio=directive.pass_ratio,
        verbosity_level=directive.verbosity_level,
        content_chars=len(content),
    )


def _append_sentence(text: str, sentence: str) -> str:
    if sentence in text:
        return text
    if text and not text.endswith(("。", "？", "！")):
        text += "。"
    return text + sentence


def ensure_complete_revision_request(
    task: CompiledTask,
    content: str,
    regressed_criteria: tuple[str, ...] = (),
) -> str:
    text = content.strip()
    if regressed_criteria:
        text = _append_sentence(text, _REGRESSION_REQUEST)
    if task.interaction_policy.preserve_satisfied_criteria:
        text = _append_sentence(text, _COMPLETE_REVISION_REQUEST)
    return text
