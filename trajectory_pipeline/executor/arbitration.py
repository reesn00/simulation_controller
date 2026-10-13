"""L5 \u4ef2\u88c1\u5c42\u2014\u2014L3 rule + L4 VLM + L2 signal \u52a0\u6743\u4ea7\u51fa 13 \u5206\u652f\u3002

\u589e\u91cf\u4e8c\u5341\u4e03\uff082026-10-13\uff09\u3002\u8bbe\u8ba1\u7eaa\u5f8b\uff084 \u6761\u62cd\u677f\uff09\uff1a

1. **L5 = N-of-M \u52a0\u6743\u4ef2\u88c1**\uff0c\u4e0d\u662f\u7b80\u5355 L3 \u62a2 L4\u3001\u4e5f\u4e0d\u662f\u53cd\u8fc7\u6765\u2014\u2014
   \u6309 ``source \u6743\u91cd \u00d7 confidence`` \u6392\u5e8f\u3001\u540c\u8d28\u4e00\u81f4\u53d6 max\uff0c\u51b2\u7a81\u65f6\u4f18\u5148 L4 llm > L4 heuristic > L3 rule
2. **L5 \u8f93\u5165 = BrowserProbeResult \u9876\u5c42**\uff0c\u4e0d\u518d\u4ece Observation \u91cd\u626b
3. **L5 \u8f93\u51fa = SiteOutcome 13 \u5206\u652f**\uff0c\u4e0e :mod:`executor.branches` \u540c\u6e90
4. **L5 fail-closed \u77ed\u8def**\uff1a``result.note \u2208 {credential_leak, driver_missing, timeout, driver_error}``\u65f6
   \u76f4\u63a5\u8fdb unresolved/player_unverified\uff0c\u4e0d\u8d70\u52a0\u6743

\u8f93\u51fa\u5f62\u72b6\uff08\u5d4c\u5165 BrowserProbeResult.l5_outcome\uff09\uff1a

.. code-block:: python

    SiteOutcome(
        url=...,
        branch="blocked" | "login_required" | ... | None,
        evidence="\u3010L5\u4ef2\u88c1\u3011...",
        question="business.signal" | "playback.success",
        source="arbiter",
        fallback_used=False,
        page_quotes=(),
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from trajectory_pipeline.executor.branches import (
    NON_SAMPLE_BRANCHES,
    SiteOutcome,
)
from trajectory_pipeline.perception.base import Decision

#: L5 fail-closed note \u96c6\uff08\u4e0e L3 / L4 \u540c\u6e90\u2014\u2014\u9a71\u52a8\u5c42\u62a5\u9519\uff09\u3002
L5_FAIL_CLOSED_NOTES: frozenset[str] = frozenset(
    {"credential_leak", "driver_missing", "timeout", "driver_error"}
)

#: L3 access_kind \u2192 13 \u5206\u652f\u6620\u5c04\uff08L3 \u8bcd\u5178 7 \u503c\u7684\u8d77\u70b9\uff09\u3002
#: \u672a\u547d\u4e2d\u8fd4\u56de None\uff0c\u4ea4\u7531 L4 / L2 \u8865\u3002
L3_ACCESS_KIND_TO_BRANCH: Mapping[str, str] = {
    "region": "blocked",
    "anti_bot": "blocked",
    "captcha": "blocked",
    "not_found": "broken",
    "login": "login_required",
}

#: L3 \u5224\u51fa paywall \u540e\uff0cL3 paywall_type \u2192 \u5206\u652f\u3002
L3_PAYWALL_TYPE_TO_BRANCH: Mapping[str, str] = {
    "preview": "partial_free",
    "trial": "partial_free",
}

#: L4 label \u2192 13 \u5206\u652f\u6620\u5c04\u3002None \u4ee3\u8868\u201c\u672c\u8eab\u5c31\u662f\u6210\u529f\u201d\uff08\u4e0d\u8d70\u5931\u8d25\u5206\u652f\uff09\u3002
#: \u4e0d\u5728\u8868\u4e2d\u7684 label \uff08empty / unknown\uff09\u8fd4\u56de None\uff0c\u4ea4\u7531 L2 / L3 \u8865\u3002
L4_LABEL_TO_BRANCH: Mapping[str, str | None] = {
    "playable": None,
    "ads_then_playable": None,
    "paywall": "subscription_wall",  # \u9ed8\u8ba4\u4e3a\u8ba2\u9605\u5f0f\uff0c\u88ab paywall_type \u8986\u76d6
    "login_required": "login_required",
    "partial_free": "partial_free",
    "blocked": "blocked",
    "broken": "broken",
}

#: L4 paywall_type \u2192 \u5f53 label=paywall \u65f6\u7684\u7ec6\u5206\u3002trial \u4e0e L3 \u540c\u6e90\u4e0d\u540c dict\u3002
L4_PAYWALL_TYPE_TO_BRANCH: Mapping[str, str] = {
    "trial": "partial_free",
}

#: L4 source \u6743\u91cd\uff08\u4ef2\u88c1\u65f6\u51b2\u7a81\u53d6\u8f83\u9ad8\u8005\uff09\u3002
#: llm > heuristic > rule\uff0c\u4e0e\u300c\u4eba\u5728\u73af\u300d\u4e00\u81f4\uff1aLLM \u770b\u4e86\u591a\u6a21\u6001\u4e0d\u5e94\u8be5\u8f93\u7ed9\u8bcd\u5178\u3002
L5_SOURCE_WEIGHT: Mapping[str, int] = {
    "llm": 3,
    "heuristic": 2,
    "gpt4v": 3,
    "claude_vision": 3,
    "rule": 1,
    "arbiter": 0,  # \u672c\u5c42\u6807\u8bb0\uff0c\u4e0d\u53c2\u4e0e\u52a0\u6743
}

#: 2026-10-13 \u589e\u91cf\u4e8c\u5341\u516b\uff1aL1 ffprobe \u5a92\u4f53\u5c42\u5224\u5b9a\u6743\u91cd\u3002
#: L1 \u662f\u300c\u5b9e\u9645\u5a92\u4f53\u5b57\u8282\u6d41\u300d\u5224\u5b9a\u7684\uff08duration / has_video\uff09\uff0c
#: \u5f3a\u4e8e L3 \u8bcd\u5178\u3001\u5f31\u4e8e L4 LLM \u591a\u6a21\u6001\u3002\u51b2\u7a81\u65f6 L1 \u8f93\u7ed9 llm (3)\uff0c
#: \u4e0e heuristic (2) \u5e73\u624b\u2014\u2014\u4f46 duration \u662f\u786c\u6570\u636e\uff0c
#: \u56e0\u6b64 partial_free / broken \u4ecd\u53ef\u6bd4 heuristic \u66f4\u8ba9\u4eba\u4fe1\u670d\u3002
L1_SOURCE_WEIGHT: int = 2
#: L1 \u51fa\u5206\u652f\u65f6\u7684\u9ed8\u8ba4 confidence\u2014\u2014duration \u662f\u786c\u8bc1\u636e\uff0c\u7ed9\u5230 0.8\uff1b
#: L1 \u6ca1\u51fa\u5206\u652f\u65f6\u8fd4 (None, 0.0)\uff0c\u8ba9 _pick_winner \u8df3\u8fc7 L1 \u6295\u7968\u3002
L1_BRANCH_CONFIDENCE: float = 0.8


def _branch_from_l3(l3_decision: Decision | None) -> tuple[str | None, float]:
    """L3 Decision \u2192 \u5206\u652f + confidence\u3002\u672a\u547d\u4e2d\u8fd4\u56de (None, 0.0)\u3002"""
    if l3_decision is None:
        return None, 0.0
    payload = dict(l3_decision.payload) if l3_decision.payload else {}
    access_kind = payload.get("access_kind")
    if access_kind == "paywall":
        # \u8c03\u7528 L3 paywall_type \u7ec6\u5206
        pw_type = payload.get("paywall_type", "none")
        branch = L3_PAYWALL_TYPE_TO_BRANCH.get(pw_type, "subscription_wall")
        return branch, float(l3_decision.confidence)
    branch = L3_ACCESS_KIND_TO_BRANCH.get(access_kind or "", None)
    if branch is None:
        return None, 0.0
    return branch, float(l3_decision.confidence)


def _branch_from_l4(vlm_review: Any) -> tuple[str | None, float, str]:
    """VLMReviewResult \u2192 \u5206\u652f + confidence + source\u3002

    \u8fd4\u56de (branch_or_None, confidence, source)\u3002branch=None \u4ee3\u8868\u201c\u672c\u8eab\u5c31\u662f\u6210\u529f\u201d
    \u6216\u201c\u672a\u547d\u4e2d\u201d\u2014\u2014\u4e24\u8005\u5747\u4ea4\u7531\u4e0a\u4e0b\u6e38\u3002
    """
    if vlm_review is None:
        return None, 0.0, ""
    label = getattr(vlm_review, "label", "")
    conf = float(getattr(vlm_review, "confidence", 0.0) or 0.0)
    source = str(getattr(vlm_review, "source", "") or "")
    if label == "paywall":
        # \u770b L4 paywall_type \u662f\u5426\u80fd\u7ec6\u5206
        pw_type = getattr(vlm_review, "paywall_type", "none")
        branch = L4_PAYWALL_TYPE_TO_BRANCH.get(pw_type, "subscription_wall")
        return branch, conf, source
    if label in L4_LABEL_TO_BRANCH:
        return L4_LABEL_TO_BRANCH[label], conf, source
    # empty / unknown / \u672a\u77e5\u6807\u7b7e
    return None, conf, source


def _branch_from_signal(
    signal: Any,
    media_count: int,
    note: str,
) -> tuple[str | None, str]:
    """L2 MediaSignal \u542f\u53d1\u5f0f\u2192 \u5206\u652f + \u8bc1\u636e\u6587\u6848\u3002

    \u8fd4\u56de (branch_or_None, evidence_phrase)\u3002
    """
    play_started = bool(getattr(signal, "play_started", False))
    current_time = getattr(signal, "current_time_sec", None)
    timeupdate = bool(getattr(signal, "timeupdate_progress", False))
    frames = getattr(signal, "frames_decoded", None)
    black = bool(getattr(signal, "black_screen", False))
    media_error = getattr(signal, "media_error", None)

    # 1) \u771f\u64ad\u653e\u4fe1\u53f7\uff1aplay_started + currentTime>0 / timeupdate\u8fdb\u5c55
    if play_started and (current_time is not None and current_time > 0 or timeupdate):
        return None, f"L2 \u5b9e\u64ad\uff08currentTime={current_time}s, frames={frames}\uff09"

    # 2) \u9ed1\u5c4f + 0 \u5e27\u89e3\u7801 \u2192 broken
    if black and (frames == 0 or frames is None) and media_count > 0:
        return "broken", "L2 \u9ed1\u5c4f\uff080 \u5e27\uff09"

    # 3) HTMLMediaElement error code >= 4 \u2192 broken
    if isinstance(media_error, int) and media_error >= 4:
        return "broken", f"L2 media_error code={media_error}"

    # 4) \u65e0\u5a92\u4f53 + \u65e0 fail-closed \u2192 not_play_site
    if media_count == 0 and note == "":
        return "not_play_site", "L2 media_count=0, \u65e0 \u003cvideo\u003e/\u003caudio\u003e"

    # 5) \u5176\u4ed6 \u2192 \u4e0d\u4ea7\u5206\u652f\uff0c\u4ea4\u4e0a\u4e0b\u6e38
    return None, ""


def _branch_from_l1(l1_probe: Any) -> tuple[str | None, float]:
    """L1 ffprobe \u5a92\u4f53\u63a2\u6d4b\u7ed3\u679c \u2192 \u5206\u652f + confidence\u3002

    \u672a\u8dd1 L1 / l1_probe=None \u2192 (None, 0.0)\uff0c\u8ba9 _pick_winner \u8df3\u8fc7 L1 \u6295\u7968\u3002
    \u51fa\u5206\u652f\u65f6\u4ee5 :data:`L1_BRANCH_CONFIDENCE` \u4e3a\u4fdd\u8bc1\u503c\uff08duration \u662f\u786c\u6570\u636e\uff09\u3002
    \u903b\u8f91\u590d\u7528 :func:`trajectory_pipeline.executor.probe.ffprobe.to_branch`\uff0c
    \u4e0e L1 \u300c\u5206\u652f\u6620\u5c04\u300d\u540c\u6e90\u3002
    """
    if l1_probe is None:
        return None, 0.0
    # \u52a8\u6001 import \u907f\u514d\u6a21\u5757\u542f\u52a8\u987a\u5e8f\u95ee\u9898\u3002
    try:
        from trajectory_pipeline.executor.probe.ffprobe import to_branch as _to_l1_branch
    except ImportError:  # pragma: no cover
        return None, 0.0
    branch = _to_l1_branch(l1_probe)
    if branch is None:
        return None, 0.0
    return branch, L1_BRANCH_CONFIDENCE


@dataclass(frozen=True, slots=True)
class L5Verdict:
    """L5 \u4ef2\u88c1\u8f93\u51fa\u4e2d\u95f4\u8868\u793a\u2014\u2014\u4e0d\u5165 result\uff0c\u53ea\u7ed9 SiteOutcome \u7528\u3002"""

    branch: str | None
    evidence: str
    source: str  # \u603b\u662f "arbiter"
    fallback_used: bool
    l3_branch: str | None
    l4_branch: str | None
    l2_branch: str | None
    # 2026-10-13 \u589e\u91cf\u4e8c\u5341\u516b\uff1aL1 ffprobe \u5224\u5b9a\u52a0\u5165 verdict\u3002
    l1_branch: str | None
    l1_conf: float
    l3_conf: float
    l4_conf: float
    l4_source: str

    def to_evidence_phrase(self) -> str:
        """\u8bc1\u636e\u6587\u6848\uff0c\u5305\u542b L1/L3/L4/L2 \u5404\u81ea\u5224\u51fa + \u4ef2\u88c1\u8def\u5f84\u3002"""
        parts: list[str] = []
        if self.l1_branch is not None:
            parts.append(f"L1\u2192{self.l1_branch}({self.l1_conf:.2f})")
        if self.l3_branch is not None:
            parts.append(f"L3\u2192{self.l3_branch}({self.l3_conf:.2f})")
        if self.l4_branch is not None or self.l4_source:
            parts.append(f"L4[{self.l4_source or '?'}]\u2192{self.l4_branch or '\u672a\u547d\u4e2d'}({self.l4_conf:.2f})")
        if self.l2_branch is not None:
            parts.append(f"L2\u2192{self.l2_branch}")
        if not parts:
            return "\u3010L5\u4ef2\u88c1\u3011\u56db\u5c42\u5168\u65e0\u5224\u51fa\uff0c\u8fdb unresolved"
        return "\u3010L5\u4ef2\u88c1\u3011" + " | ".join(parts) + f" \u2192 {self.branch or '\u6210\u529f'}"


# INC28_L1_ARB_INTEGRATED_v1
def _pick_winner(
    l3_branch: str | None,
    l3_conf: float,
    l4_branch: str | None,
    l4_conf: float,
    l4_source: str,
    l2_branch: str | None,
    l1_branch: str | None = None,
    l1_conf: float = 0.0,
) -> str | None:
    """\u9009\u51fa\u6700\u7ec8\u5206\u652f\u3002None \u4ee3\u8868\u6210\u529f\u3002

    2026-10-13 \u589e\u91cf\u4e8c\u5341\u516b\uff1aL1 \u4f5c\u4e3a\u7b2c\u56db\u4ef2\u88c1\u4f19\u4f34\uff08duration \u1973fa hard \u8bc1\u636e\uff09\u3002

    \u4f18\u5148\u7ea7\uff081 \u6700\u9ad8\uff09\uff1a
    1. L2 \u771f\u64ad\u653e\uff08l2_branch=None\uff09\u2192 \u4e00\u7968\u5426\u51b3\uff0c\u8fd4\u56de None
    2. L2 \u9ed1\u5c4f/\u9519\u8bef\u4ee3\u7801 \u2192 \u4e00\u7968\u51b3\u5b9a broken
    3. L3 + L4 \u4e00\u81f4 \u2192 \u91c7\u7eb3
    4. L3 + L4 \u51b2\u7a81 \u2192 \u6309 source \u6743\u91cd \u00d7 confidence \u53d6\u8f83\u9ad8\u8005
    5. L1 \u4e0e\u4efb\u4f55\u4e00\u81f4 \u2192 \u91c7\u7eb3\uff1bL1 \u4e0e\u4e09\u8005\u90fd\u51b2\u7a81 \u2192 \u6309\u6743\u91cd\u53d6\u8f83\u9ad8\u8005\uff08llm > l1\uff09
    6. \u4ec5 L3 \u2192 \u7528 L3\uff1b\u4ec5 L4 \u2192 \u7528 L4\uff1b\u4ec5 L1 \u2192 \u7528 L1\uff1b\u5168\u65e0 \u2192 unresolved
    """
    # 1) L2 \u771f\u64ad\u653e\uff1a\u4e00\u7968\u5426\u51b3
    if l2_branch is None and l3_branch is None and l4_branch is None:
        # L2 \u4ec5\u4ea7\u51fa "None" \u4e0e \u4e0d\u4ea7\u51fa\uff0c\u9700\u8981\u533a\u5206
        # \u8fd9\u91cc\u9760 L2 \u662f\u5426\u4ea7\u51fa\u201c\u6210\u529f\u4fe1\u53f7\u201d\uff08play_started\uff09\u6765\u5224
        pass
    # \u5b9e\u9645\u4e0a _branch_from_signal \u8fd4\u56de None \u4e24\u79cd\uff1a
    # (a) \u771f\u64ad\u653e \u2192 (None, "...\u5b9e\u64ad...")\uff1b(b) \u672a\u547d\u4e2d \u2192 (None, "")
    # \u6211\u4eec\u5728 _arbitrate \u91cc\u5e26\u4e0a evidence_phrase \u4f20\u5165\uff0c\u8fd9\u91cc\u53ea\u770b\u5206\u652f\u4e0d\u591f
    # \u2192 \u6539\u4e3a\u8ba9 _arbitrate \u5904\u7406 L2 \u4f18\u5148\u7ea7

    if l3_branch is not None and l4_branch is not None:
        if l3_branch == l4_branch:
            # L1 \u4e0e\u4e00\u81f4 \u2192 \u91c7\u7eb3
            if l1_branch == l3_branch:
                return l3_branch
            # L1 \u4e0e\u4e24\u8005\u90fd\u51b2\u7a81 \u2192 \u6309\u6743\u91cd\u53d6\u8f83\u9ad8\u8005\uff08llm > l1\uff09
            if l1_branch is not None and l1_branch != l3_branch:
                l3_score = L5_SOURCE_WEIGHT.get("rule", 1) * l3_conf
                l4_score = L5_SOURCE_WEIGHT.get(l4_source, 2) * l4_conf
                l1_score = L1_SOURCE_WEIGHT * l1_conf
                top = max(
                    (l3_score, l3_branch),
                    (l4_score, l4_branch),
                    (l1_score, l1_branch),
                    key=lambda x: x[0],
                )
                return top[1]
            # L3 + L4 \u51b2\u7a81\u4f46 L1 \u672a\u51fa\u5206\u652f
            l3_score = L5_SOURCE_WEIGHT.get("rule", 1) * l3_conf
            l4_score = L5_SOURCE_WEIGHT.get(l4_source, 2) * l4_conf
            return l3_branch if l3_score >= l4_score else l4_branch
        # L3 + L4 \u51b2\u7a81
        l3_score = L5_SOURCE_WEIGHT.get("rule", 1) * l3_conf
        l4_score = L5_SOURCE_WEIGHT.get(l4_source, 2) * l4_conf
        # L1 \u53ef\u80fd\u4e0e L3 \u4e00\u81f4\u3001\u4e0e L4 \u4e00\u81f4\u3001\u6216\u4e0e\u4e24\u8005\u90fd\u4e0d\u4e00\u81f4
        l1_score = L1_SOURCE_WEIGHT * l1_conf if l1_branch is not None else 0.0
        candidates: list[tuple[float, str]] = [
            (l3_score, l3_branch),
            (l4_score, l4_branch),
        ]
        if l1_branch is not None:
            candidates.append((l1_score, l1_branch))
        top = max(candidates, key=lambda x: x[0])
        return top[1]
    # INC28_L1_PICK_FIX_v1: L1 \u4e0e L3/L4 \u51b2\u7a81\u65f6\u6309\u6743\u91cd\u9009
    if l3_branch is not None:
        # L1 \u4e0e L3 \u51b2\u7a81 \u2192 \u6309\u6743\u91cd\u53d6\u8f83\u9ad8\u8005
        if l1_branch is not None and l1_branch != l3_branch:
            l3_score = L5_SOURCE_WEIGHT.get("rule", 1) * l3_conf
            l1_score = L1_SOURCE_WEIGHT * l1_conf
            return l1_branch if l1_score > l3_score else l3_branch
        return l3_branch
    if l4_branch is not None:
        # L1 \u4e0e L4 \u51b2\u7a81 \u2192 \u6309\u6743\u91cd\u53d6\u8f83\u9ad8\u8005
        if l1_branch is not None and l1_branch != l4_branch:
            l4_score = L5_SOURCE_WEIGHT.get(l4_source, 2) * l4_conf
            l1_score = L1_SOURCE_WEIGHT * l1_conf
            return l1_branch if l1_score > l4_score else l4_branch
        return l4_branch
    # \u4ec5 L1 \u6709\u5224\u51fa
    if l1_branch is not None:
        return l1_branch
    return "unresolved"


def arbitrate(result: Any) -> SiteOutcome:
    """L5 \u4ef2\u88c1\u4e3b\u51fd\u6570\u3002

    \u8f93\u5165\uff1a``BrowserProbeResult``\uff08result.signal + result.l3_decision + result.vlm_review + result.note\uff09
    \u8f93\u51fa\uff1a``SiteOutcome``\uff08branch \u5728 13 \u5206\u652f\u96c6\u5408\u5185\uff0c\u6216 None = \u6210\u529f\uff09

    \u8c03\u7528\u8005\uff1acli.py \u5728 L3 / L4 \u4e4b\u540e\u6ce8\u5165\uff1b
    test_arbitration.py \u76f4\u63a5 unit \u9a8c\u8bc1\u3002
    """
    l3_decision = getattr(result, "l3_decision", None)
    vlm_review = getattr(result, "vlm_review", None)
    signal = getattr(result, "signal", None)
    note = str(getattr(result, "note", "") or "")
    media_count = int(getattr(result, "media_count", 0) or 0)
    url = str(getattr(result, "final_url", "") or getattr(result, "requested_url", "") or "")

    # === Step 0\uff1afail-closed \u77ed\u8def ===
    if note in L5_FAIL_CLOSED_NOTES:
        # \u9a71\u52a8\u5c42\u672a\u91c7\u5230\u8db3\u591f\u4fe1\u606f\u3002\u4f46 L4 \u53ef\u80fd\u4ecd\u6709\u5224\u51fa\uff08\u5c11\u89c1\uff09\u2014\u2014
        # \u4ecd\u4fdd\u7559 L4 \u5224\u51fa\uff0c\u4ec5\u5728 evidence \u91cc\u52a0\u4e0a fail-closed \u540e\u7f00\u3002
        l4_branch, l4_conf, l4_source = _branch_from_l4(vlm_review)
        if l4_branch is not None:
            verdict_branch = l4_branch
            ev = (
                f"\u3010L5\u4ef2\u88c1\u3011fail-closed note={note}, \u4f46 L4[{l4_source}]\u2192{l4_branch}"
                f"\uff08{l4_conf:.2f}\uff09\u4ecd\u91c7\u7eb3"
            )
            return SiteOutcome(
                url=url,
                branch=verdict_branch,
                evidence=ev,
                question="business.signal",
                source="arbiter",
                fallback_used=False,
            )
        # L4 \u4e5f\u672a\u5224\u51fa \u2192 unresolved
        return SiteOutcome(
            url=url,
            branch="unresolved",
            evidence=f"\u3010L5\u4ef2\u88c1\u3011fail-closed note={note}, L3/L4 \u5168\u65e0\u5224\u51fa",
            question="business.signal",
            source="arbiter",
            fallback_used=False,
        )

    # === Step 1\uff1a\u89e3\u6790 L1 / L3 / L4 / L2 \u5404\u81ea\u8d21\u732e ===
    # 2026-10-13 \u589e\u91cf\u4e8c\u5341\u516b\uff1aL1 ffprobe \u4e0a\u4f4d\uff0c\u4e0e\u540c\u4f4d\u7ade\u4e89\u3002
    l1_probe = getattr(result, "l1_probe", None)
    l1_branch, l1_conf = _branch_from_l1(l1_probe)
    l3_branch, l3_conf = _branch_from_l3(l3_decision)
    l4_branch, l4_conf, l4_source = _branch_from_l4(vlm_review)
    l2_branch, l2_ev_phrase = _branch_from_signal(signal, media_count, note)

    # === Step 2\uff1aL2 \u771f\u64ad\u653e\u4fe1\u53f7\uff08\u4e00\u7968\u5426\u51b3\uff09 ===
    if (
        l2_branch is None
        and l2_ev_phrase  # \u975e\u7a7a\uff1a\u8868\u793a L2 \u662f\u771f\u64ad\u653e\uff0c\u4e0d\u662f\u201c\u672a\u547d\u4e2d\u201d
        and "L2 \u5b9e\u64ad" in l2_ev_phrase
    ):
        return SiteOutcome(
            url=url,
            branch=None,
            evidence=f"\u3010L5\u4ef2\u88c1\u3011L2 \u5b9e\u64ad\uff08{url}\uff09",
            question="playback.success",
            source="arbiter",
            fallback_used=False,
        )

    # === Step 3\uff1a\u4ef2\u88c1\u9009\u62d4 ===
    # 2026-10-13 \u589e\u91cf\u4e8c\u5341\u516b\uff1aL1 \u4f5c\u4e3a\u7b2c\u56db\u4ef2\u88c1\u4f19\u4f34\u53c2\u4e0e\u9009\u62d4\u3002
    verdict_branch = _pick_winner(
        l3_branch, l3_conf, l4_branch, l4_conf, l4_source, l2_branch,
        l1_branch, l1_conf,
    )

    # \u6784 L5Verdict \u4f9b evidence
    # 2026-10-13 \u589e\u91cf\u4e8c\u5341\u516b\uff1a\u4f9b L1 \u5206\u652f + \u7f6e\u4fe1\u5ea6\u3002
    verdict = L5Verdict(
        branch=verdict_branch,
        evidence="",
        source="arbiter",
        fallback_used=False,
        l3_branch=l3_branch,
        l4_branch=l4_branch,
        l2_branch=l2_branch,
        l1_branch=l1_branch,
        l1_conf=l1_conf,
        l3_conf=l3_conf,
        l4_conf=l4_conf,
        l4_source=l4_source,
    )
    ev_phrase = verdict.to_evidence_phrase()
    # \u8865\u4e0a L2 evidence
    if l2_branch is not None:
        ev_phrase += f" | L2: {l2_ev_phrase}"

    # === Step 4\uff1a\u6821\u9a8c\u8f93\u51fa\u6620\u5c04\u662f\u5426\u5408\u6cd5\uff08\u9632\u6b62 arbiter \u81ea\u5df1\u9020\u51fa\u672a\u77e5\u5206\u652f\uff09 ===
    if verdict_branch is not None and verdict_branch not in NON_SAMPLE_BRANCHES:
        # \u53ef\u80fd\u8fd8\u4e0d\u662f\u6700\u7ec8\u5408\u6cd5\u503c\uff0c\u4e0d\u8fc7 _pick_winner \u53ea\u8fd4\u56de\u4e09\u5c42\u90fd\u5408\u6cd5\u7684\u503c\u3002\u4ee5\u4e0b\u53ea\u662f\u9632\u5fa1\u3002
        pass

    return SiteOutcome(
        url=url,
        branch=verdict_branch,
        evidence=ev_phrase,
        question="business.signal" if verdict_branch != "unresolved" else "playback.success",
        source="arbiter",
        fallback_used=False,
    )
