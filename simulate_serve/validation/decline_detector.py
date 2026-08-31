from __future__ import annotations

import re
from collections.abc import Sequence

# Honest-limitation declarations. We keep this list narrow and high-precision:
# a false positive stops the run and forfeits distillation. Each entry is a
# regex applied to the visible response text. Deliberately excluded: requests
# for clarification (需要您先确认…) and recoverable per-link reports (该站无法
# 访问，换一个) — both are legitimate working behaviour, not quitting.
#
# Patterns are grouped by linguistic shape so the source of a hit can be
# audited from test failures. Order matters only for readability; the detector
# itself returns on the first match via any().
_DECLINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 1. Subject-anchored first-person refusals (highest precision, no filler).
    #    我无法 / 我不能 / 我没办法 / 我帮不了 / 我没法 / 我帮不上 /
    #    我无能为力 / 我不便 / 我不予 / 我无法协助.
    #    我做不到 deliberately excluded — the partial-work shape
    #    "我做不到完整覆盖" must not be flagged. Sentence-final 做不到 is
    #    covered separately by pattern #1c.
    re.compile(r"我(?:无法|不能|没办法|帮不了|没法|帮不上|无能为力|不便|不予|无法协助)"),
    # 1b. Relaxed subject-anchored: 我 + 0-3 specific connector words + the
    #     same refusal verbs. Connectors are limited to 还是/也/都/可能/暂
    #     时/仍然/已经 to keep "我建议你无法…" from matching.
    re.compile(
        r"我(?:还是|也|都|可能|暂时|仍然|已经)"
        r"(?:无法|不能|没办法|帮不了|没法|帮不上|无能为力)"
    ),
    # 1c. Sentence-final 做不到 only (categorical: "我做不到。" / "我做不到！").
    #     Partial shapes like "我做不到完整覆盖" do NOT end at 做不到, so
    #     this never fires on partial-work phrasings.
    re.compile(r"我(?:.{0,3}?)?做不到(?:$|[。！？!，、\s])"),
    # 2. Classical / standalone refusal idioms.
    re.compile(r"恕难从命|爱莫能助|无能为力|束手无策|无可奉告"),
    # 3. "抱歉, 没法{verb}" — common apology-prefixed refusal without 我.
    #    没法访问/没法打开 are intentionally excluded (per-link failure shape).
    re.compile(r"(?:^|[。！？；\s])(?:抱歉[,，]?\s*)?没法(?:给你|帮你|做|做这个|提供|继续|完成|协助)"),
    # 4. Terse single-word refusal at the very start of a short response.
    #    The refusal word must open the reply; up to 8 chars of object / clause
    #    are allowed before the sentence-end punctuation, so
    #    "不提供这类内容。" matches but "不行的话告诉我" does not.
    re.compile(
        r"^\s*(?:不做|不列|不提供|不写|不答|不接|不予|不行|不干|不算|不愿|不愿意"
        r"|不给|不出|不搜|不查|不查了).{0,8}?[。！？!\.]"
    ),
    # 5. Capability / role / scope limits (kept from original).
    re.compile(r"没有权限|权限不足|超出.{0,4}能力|能力有限|不在.{0,4}(?:范围|职责)|职责范围"),
    # 6. Generic task-level 无法 + completion verbs.
    #    无法访问 / 无法获取 deliberately excluded — those are per-link reports.
    re.compile(r"无法(?:完成|满足|提供|继续|实现|协助|做到)"),
    # 7. English refusals (kept + extended).
    re.compile(
        r"\b(?:I (?:can(?:not|'t) |cannot |am unable to|won't|will not|refuse to)\b"
        r"|out of (?:my )?scope"
        r"|no permission"
        r"|not (?:able|willing|allowed)"
        r")"
    ),
)

# Cross-round heuristic — invoked only when the per-text regex above did NOT
# match, so a single-round false positive never terminates a run on its own.
#
# Signal: across consecutive guide rounds the same failure reason_codes
# persist while the agent's visible reply is shrinking and short. This is the
# characteristic shape of an agent declining in different wording each round
# ("不做。绕不过去。" → "不列。重复问都一样。"), which the regex set cannot
# catch deterministically.
_REPEATED_REFUSAL_SHORT_THRESHOLD = 200
_REPEATED_REFUSAL_MIN_GUIDE_ROUNDS = 1

_URL_RE = re.compile(r"https?://[^\s　]+")


def detect_honest_limitation(text: str) -> bool:
    """Return True when the remote agent appears to honestly declare an inability.

    Conservative: only signals a single regex hit in the response text. Callers
    must run validation first — a passing reply must never be terminated for its
    phrasing — and combine with `blocked_action` policy before terminating.
    """
    if not text:
        return False
    return any(pattern.search(text) for pattern in _DECLINE_PATTERNS)


def looks_like_repeated_refusal(
    failing_criterion_ids_by_round: Sequence[frozenset[str]],
    response_texts_by_round: Sequence[str],
    *,
    guide_rounds: int,
    short_threshold: int = _REPEATED_REFUSAL_SHORT_THRESHOLD,
    min_guide_rounds: int = _REPEATED_REFUSAL_MIN_GUIDE_ROUNDS,
) -> bool:
    """Heuristic detector for "stuck refusal" trajectories.

    Complements `detect_honest_limitation`: that one looks at a single reply,
    this one looks at the trajectory across guide rounds. It only fires when:

    1. At least ``min_guide_rounds`` follow-up rounds have been issued
       (opening reply alone never triggers — there is no trajectory yet).
    2. The last two rounds' *failing criterion id sets* are identical. Same
       reason_code alone is not enough: a regression (round 1 had criterion X
       pass while Y failed; round 2 has X fail while Y passes) produces the
       same failing reason_code set but represents progress, not refusal.
    3. The current reply is short (``<= short_threshold`` chars) AND strictly
       shorter than the immediately previous reply. A model adding new content
       does not look stuck.
    4. The current reply does not introduce any URL the previous reply did not
       already contain (so a model that finally surfaces a candidate link is
       not mistaken for a stuck refuser).

    Returns False on any condition not satisfied. Caller is responsible for
    confirming ``detect_honest_limitation(current_text) is False`` so this
    heuristic never overrides a confirmed single-round decline.
    """
    if guide_rounds < min_guide_rounds:
        return False
    if len(failing_criterion_ids_by_round) < 2 or len(response_texts_by_round) < 2:
        return False
    last_ids = failing_criterion_ids_by_round[-1]
    prev_ids = failing_criterion_ids_by_round[-2]
    if not last_ids or last_ids != prev_ids:
        return False
    last_text = response_texts_by_round[-1]
    prev_text = response_texts_by_round[-2]
    if not last_text or len(last_text) > short_threshold:
        return False
    if len(last_text) >= len(prev_text):
        return False
    last_urls = set(_URL_RE.findall(last_text))
    prev_urls = set(_URL_RE.findall(prev_text))
    if last_urls - prev_urls:
        return False
    return True