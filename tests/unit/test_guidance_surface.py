"""Behavioural tests for the guidance policy/surface split (P1 fixes).

- Variant pools rotate deterministically per (run, reason_code, round) and
  never repeat within a run.
- The deterministic realization chain never emits criterion/remediation text.
- The guidance level reacts to failure state (repeats, regressions, stall)
  and stays monotonic within a run.
- The exit gates reject keyword leaks and criterion echoes; verbosity budget
  triggers one compression retry before falling back to the variant pool.
"""
from __future__ import annotations

import pytest

from simulate_serve.domain.task import PersonaSpec
from simulate_serve.domain.validation import CriterionResult, ValidationReport, Verdict
from simulate_serve.interaction.actor import CamelInteractionActor
from simulate_serve.interaction.content_policy import overlaps_internal_text
from simulate_serve.interaction.guidance_policy import (
    NON_GAP_DETERMINISTIC_CODES,
    _NATURAL_MESSAGES,
    build_guidance_directive,
    normalize_verbosity,
    pick_guidance_variant,
    realize_guidance,
    verbosity_budget,
)
from simulate_serve.interaction.models import InteractionContext
from simulate_serve.validation.reason_codes import DETERMINISTIC_REASON_CODES


def _fail_report(criterion_id: str, code: str = "X", retryable: bool = True, verdict: Verdict = Verdict.FAIL) -> ValidationReport:
    result = CriterionResult(
        criterion_id=criterion_id, verdict=verdict, reason_code=code, message="m", retryable=retryable
    )
    return ValidationReport(
        verdict=verdict,
        criteria=(result,),
        retryable=verdict is Verdict.FAIL and retryable,
    )


def _directives_for_variant_rotation():
    return ("第一句话术", "第二句话术", "第三句话术")


# ---------------------------------------------------------------------------
# Variant pool rotation
# ---------------------------------------------------------------------------


def test_pick_guidance_variant_is_deterministic_per_run_and_round() -> None:
    variants = _directives_for_variant_rotation()
    first = pick_guidance_variant(variants, "run-a", "URL_MISSING", 0)
    assert pick_guidance_variant(variants, "run-a", "URL_MISSING", 0) == first
    texts = {pick_guidance_variant(variants, "run-a", "URL_MISSING", round_)[0] for round_ in range(3)}
    assert len(texts) == 3, "pool must rotate across rounds within a run"


def test_pick_guidance_variant_skips_excluded_sentences() -> None:
    variants = _directives_for_variant_rotation()
    first_text, _ = pick_guidance_variant(variants, "run-a", "URL_MISSING", 0)
    second_text, second_id = pick_guidance_variant(
        variants, "run-a", "URL_MISSING", 0, exclude={first_text}
    )
    assert second_text != first_text
    assert second_id.startswith("URL_MISSING[")


def test_pick_guidance_variant_empty_pool_returns_none() -> None:
    assert pick_guidance_variant((), "run-a", "URL_MISSING", 0) is None


# ---------------------------------------------------------------------------
# Deterministic realization chain
# ---------------------------------------------------------------------------


def test_realize_guidance_never_emits_remediation_text(compiled_task, source_ref) -> None:
    """The last-resort chain reads only scenario phrasing and builtin natural
    messages — never criterion descriptions or remediation guidance."""
    from simulate_serve.domain.task import AcceptanceCriterion, InteractionPolicy, RemediationSpec

    criterion = AcceptanceCriterion(
        criterion_id="task.x",
        description="内部准则描述文本",
        remediation=RemediationSpec(owner="executor", guidance="绝不出现的内部修复话术"),
        source=source_ref,
    )
    task = compiled_task.model_copy(
        update={
            "criteria": (criterion,),
            "interaction_policy": InteractionPolicy(guidance_by_reason={"UNKNOWN_CODE": ("场景话术甲", "场景话术乙")}),
        }
    )
    report = _fail_report("task.x", code="UNKNOWN_CODE")
    directive = build_guidance_directive(InteractionContext(task=task), report)

    content, variant_ids = realize_guidance(task, directive, "run-a", 0)

    assert content.startswith("场景话术")
    assert "绝不出现的内部修复话术" not in content
    assert "内部准则描述文本" not in content
    assert variant_ids[0].startswith("UNKNOWN_CODE[")


def test_realize_guidance_falls_back_to_natural_then_generic(compiled_task, source_ref) -> None:
    from simulate_serve.domain.task import AcceptanceCriterion, InteractionPolicy

    criterion = AcceptanceCriterion(criterion_id="task.url", description="url", source=source_ref)
    # URL_MISSING has a natural message; an unknown code lands on the generic.
    task = compiled_task.model_copy(
        update={
            "criteria": (
                criterion,
                AcceptanceCriterion(criterion_id="task.zzz", description="z", source=source_ref),
            ),
            "interaction_policy": InteractionPolicy(),
        }
    )
    report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(criterion_id="task.url", verdict=Verdict.FAIL, reason_code="URL_MISSING", message="m", retryable=True),
            CriterionResult(criterion_id="task.zzz", verdict=Verdict.FAIL, reason_code="TOTALLY_UNKNOWN", message="m", retryable=True),
        ),
        retryable=True,
    )
    directive = build_guidance_directive(InteractionContext(task=task), report)

    content, _ = realize_guidance(task, directive, "run-a", 0)

    assert _NATURAL_MESSAGES["URL_MISSING"].rstrip("。") in content
    assert "对照我的要求" in content  # generic last resort for the unknown code


def test_realize_guidance_rotates_fallback_guidance(compiled_task, source_ref) -> None:
    from simulate_serve.domain.task import AcceptanceCriterion, InteractionPolicy

    criterion = AcceptanceCriterion(criterion_id="task.x", description="x", source=source_ref)
    task = compiled_task.model_copy(
        update={
            "criteria": (criterion,),
            "interaction_policy": InteractionPolicy(fallback_guidance=("甲句", "乙句")),
        }
    )
    directive = build_guidance_directive(InteractionContext(task=task), _fail_report("task.x", code="NO_VARIANTS"))

    # The unknown code has no pool and no natural message, so the empty gap
    # realization turns to the rotating fallback list.
    from simulate_serve.interaction.guidance_policy import GuidanceDirective

    empty_directive = GuidanceDirective()
    assert realize_guidance(task, empty_directive, "run-a", 0)[0].startswith("甲句")
    assert realize_guidance(task, empty_directive, "run-a", 1)[0].startswith("乙句")
    assert realize_guidance(task, empty_directive, "run-a", 2)[0].startswith("甲句")


# ---------------------------------------------------------------------------
# State-aware guidance level
# ---------------------------------------------------------------------------


def test_first_failure_stays_at_l2(compiled_task, source_ref) -> None:
    from simulate_serve.domain.task import AcceptanceCriterion

    criterion = AcceptanceCriterion(criterion_id="task.a", description="a", source=source_ref)
    task = compiled_task.model_copy(update={"criteria": (criterion,)})
    directive = build_guidance_directive(
        InteractionContext(task=task, guide_rounds=0), _fail_report("task.a")
    )
    assert directive.guidance_level == "L2"
    assert directive.emphasis == "first"


def test_repeated_failure_upgrades_to_l3_with_repeat_emphasis(compiled_task, source_ref) -> None:
    from simulate_serve.domain.task import AcceptanceCriterion

    criterion = AcceptanceCriterion(criterion_id="task.a", description="a", source=source_ref)
    task = compiled_task.model_copy(update={"criteria": (criterion,)})
    directive = build_guidance_directive(
        InteractionContext(task=task, guide_rounds=1, fail_streaks={"task.a": 2}),
        _fail_report("task.a"),
    )
    assert directive.guidance_level == "L3"
    assert directive.emphasis == "repeat"
    assert directive.repeated_criteria == ("task.a",)


def test_triple_failure_and_regression_upgrade_to_l4(compiled_task, source_ref) -> None:
    from simulate_serve.domain.task import AcceptanceCriterion

    criterion = AcceptanceCriterion(criterion_id="task.a", description="a", source=source_ref)
    task = compiled_task.model_copy(update={"criteria": (criterion,)})
    streaked = build_guidance_directive(
        InteractionContext(task=task, guide_rounds=2, fail_streaks={"task.a": 3}),
        _fail_report("task.a"),
    )
    regressed = build_guidance_directive(
        InteractionContext(task=task, guide_rounds=1, regressed_criteria=("task.a",)),
        _fail_report("task.a"),
    )
    assert streaked.guidance_level == "L4"
    assert regressed.guidance_level == "L4"
    assert regressed.emphasis == "regress"


def test_guidance_level_is_monotonic_within_a_run(compiled_task, source_ref) -> None:
    from simulate_serve.domain.task import AcceptanceCriterion

    criterion = AcceptanceCriterion(criterion_id="task.a", description="a", source=source_ref)
    task = compiled_task.model_copy(update={"criteria": (criterion,)})
    directive = build_guidance_directive(
        InteractionContext(task=task, guide_rounds=2, previous_guidance_level="L4"),
        _fail_report("task.a"),
    )
    assert directive.guidance_level == "L4", "a fresh gap must not soften the simulated user"


def test_multi_criterion_stall_upgrades_after_a_guided_round(compiled_task, source_ref) -> None:
    from simulate_serve.domain.task import AcceptanceCriterion

    criteria = (
        AcceptanceCriterion(criterion_id="task.a", description="a", source=source_ref),
        AcceptanceCriterion(criterion_id="task.b", description="b", source=source_ref),
        AcceptanceCriterion(criterion_id="task.c", description="c", source=source_ref),
    )
    task = compiled_task.model_copy(update={"criteria": criteria})
    report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(criterion_id="task.a", verdict=Verdict.PASS, reason_code="OK", message="ok"),
            CriterionResult(criterion_id="task.b", verdict=Verdict.FAIL, reason_code="X", message="m", retryable=True),
            CriterionResult(criterion_id="task.c", verdict=Verdict.FAIL, reason_code="Y", message="m", retryable=True),
        ),
        retryable=True,
    )
    gentle = build_guidance_directive(InteractionContext(task=task, guide_rounds=0), report)
    stalled = build_guidance_directive(InteractionContext(task=task, guide_rounds=1), report)
    assert gentle.guidance_level == "L2"
    assert stalled.guidance_level == "L3"


# ---------------------------------------------------------------------------
# Exit gates
# ---------------------------------------------------------------------------


def test_overlaps_internal_text_detects_criterion_echo() -> None:
    internal = "回复必须包含可播放的网址并注明可观看的集数范围，逐一验证每个链接的可播放状态"
    assert overlaps_internal_text(internal, [internal]) is True
    assert overlaps_internal_text(f"那个，{internal}，就这样", [internal]) is True


def test_overlaps_internal_text_allows_natural_replies() -> None:
    internal = "回复必须包含可播放的网址并注明可观看的集数范围，逐一验证每个链接的可播放状态"
    assert overlaps_internal_text("链接我这边打不开啊，你换一个我再试试", [internal]) is False
    assert overlaps_internal_text("", [internal]) is False


def test_natural_messages_cover_all_gap_eligible_deterministic_codes() -> None:
    missing = sorted(
        DETERMINISTIC_REASON_CODES - NON_GAP_DETERMINISTIC_CODES - set(_NATURAL_MESSAGES)
    )
    assert not missing, f"deterministic gap codes without natural user phrasing: {missing}"


# ---------------------------------------------------------------------------
# Verbosity budget
# ---------------------------------------------------------------------------


def test_verbosity_normalization_and_budget() -> None:
    assert normalize_verbosity("concise") == "concise"
    assert normalize_verbosity("medium") == "moderate"
    assert normalize_verbosity(None) == "moderate"
    assert normalize_verbosity("nonsense") == "moderate"
    assert verbosity_budget("concise") == 60
    assert verbosity_budget("detailed") == 200


@pytest.mark.asyncio
async def test_camel_actor_compresses_over_budget_output(compiled_task, monkeypatch) -> None:
    """An over-budget but clean paraphrase gets one compression retry instead
    of being discarded."""
    import camel.agents as camel_agents

    long_reply = (
        "我看了你发来的东西，整体感觉还是跟我一开始说的那些要求差了一点，"
        "所以你最好把我最开始提到的那几个点重新认真核对一遍，然后再整理一份完整的发给我看看吧。"
    )
    short_reply = "数量不够，补齐了再发我"

    class _VerboseChatAgent:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.calls = 0

        async def astep(self, message: object):
            self.calls += 1
            message_obj = type("M", (), {})()
            message_obj.content = long_reply if self.calls == 1 else short_reply
            return type("R", (), {"msgs": (message_obj,)})()

    monkeypatch.setattr(camel_agents, "ChatAgent", _VerboseChatAgent)
    actor = CamelInteractionActor(model=object())
    task = compiled_task.model_copy(update={"persona": PersonaSpec(verbosity="concise")})
    report = _fail_report(compiled_task.criteria[0].criterion_id)
    context = InteractionContext(task=task, guide_rounds=0)

    utterance = await actor.create_followup(context, report)

    assert utterance.source == "llm"
    assert utterance.content.startswith(short_reply)
    assert len(long_reply) > utterance.content_chars
