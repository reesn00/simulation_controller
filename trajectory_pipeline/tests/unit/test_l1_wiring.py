"""L1 ffprobe \u771f\u63a5\u5165 unit tests (\u589e\u91cf\u4e8c\u5341\u516b)\u3002

\u8986\u76d6\uff1a
1. _branch_from_l1 \u8fb9\u754c (None / \u65e0\u5a92\u4f53 / \u5168\u5931\u8d25 / duration<=60 / duration>60)
3. _pick_winner \u6b65\u5165 l1_branch / l1_conf (\u4e0d\u51b2\u7a81 / \u4e0e L3 \u4e00\u81f4 / \u4e0e L4 \u51b2\u7a81 / \u4ec5 L1)
4. arbitrate() \u8d2f L1 \u4f1a \u4ea7\u51fa SiteOutcome
5. BrowserProbeResult.l1_probe \u5b57\u6bb5\u5728 dump JSON \u4e2d\u51fa\u73b0
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.executor.arbitration import (
    L1_BRANCH_CONFIDENCE,
    _branch_from_l1,
    _pick_winner,
    arbitrate,
)
from trajectory_pipeline.executor.probe.ffprobe import (
    FFProbeOutput,
    MediaProbeResult,
)
from trajectory_pipeline.executor.probe.media_probe import (
    BrowserProbeResult,
    MediaSignal,
    dump_result as dump_browser_result,
)
from trajectory_pipeline.executor.review.vlm_reviewer import VLMReviewResult
from trajectory_pipeline.perception.base import Decision


# ---------- \u56fa\u5b9a factory ----------

def _mk_signal(
    play_started: bool = False,
    timeupdate_progress: bool = False,
    current_time_sec: float | None = None,
    frames_decoded: int | None = 0,
    black_screen: bool = False,
    media_error: int | None = 0,
) -> MediaSignal:
    return MediaSignal(
        play_started=play_started,
        timeupdate_progress=timeupdate_progress,
        current_time_sec=current_time_sec,
        frames_decoded=frames_decoded,
        black_screen=black_screen,
        media_error=media_error,
    )


def _mk_l1(
    probed: tuple[FFProbeOutput, ...] = (),
    media_urls: tuple[str, ...] = (),
) -> MediaProbeResult:
    """\u9020\u4e00\u4e2a MediaProbeResult\u2014\u2014\u4ec5 probed / media_urls \u4e24\u5b57\u6bb5\u5728\u4ef2\u88c1\u91cc\u6709\u7528\u3002"""
    return MediaProbeResult(
        requested_url="https://example.com",
        final_url="https://example.com",
        html_excerpt="",
        media_urls=media_urls,
        iframe_urls=(),
        probed=probed,
        elapsed_ms=10,
    )


def _mk_browser_result(
    *,
    l1_probe: MediaProbeResult | None = None,
    l3_decision: Decision | None = None,
    vlm_review: VLMReviewResult | None = None,
    signal: MediaSignal | None = None,
    note: str = "",
    media_count: int = 1,
) -> BrowserProbeResult:
    return BrowserProbeResult(
        requested_url="https://example.com",
        final_url="https://example.com",
        driver_name="fake",
        ok=True,
        media_count=media_count,
        signal=signal or _mk_signal(),
        error="",
        elapsed_ms=100,
        note=note,
        l1_probe=l1_probe,
        l3_decision=l3_decision,
        vlm_review=vlm_review,
    )


def _mk_decision(
    *,
    access_kind: str | None = None,
    paywall_type: str = "none",
    confidence: float = 0.85,
) -> Decision:
    payload: dict = {}
    if access_kind is not None:
        payload["access_kind"] = access_kind
        if access_kind == "paywall":
            payload["paywall_type"] = paywall_type
    return Decision(
        question="business.signal",
        answer=True,
        confidence=confidence,
        evidence="unit",
        source="rule",
        payload=payload,
    )


def _mk_vlm(
    *,
    label: str = "unknown",
    paywall_type: str = "none",
    confidence: float = 0.7,
    source: str = "heuristic",
    note: str = "",
) -> VLMReviewResult:
    return VLMReviewResult(
        label=label,
        paywall_type=paywall_type,
        evidence="unit",
        confidence=confidence,
        note=note,
        source=source,
    )


# ---------- _branch_from_l1 \u8fb9\u754c ----------

class TestBranchFromL1:
    """L1 -> branch + confidence \u8fb9\u754c\u3002"""

    def test_none_returns_none_zero(self) -> None:
        assert _branch_from_l1(None) == (None, 0.0)

    def test_no_probed_returns_none_zero(self) -> None:
        # \u6ca1\u89e3\u6790\u51fa\u5a92\u4f53 -> L1 \u6ca1\u7ed3\u8bba
        l1 = _mk_l1(probed=())
        assert _branch_from_l1(l1) == (None, 0.0)

    def test_all_failed_returns_broken(self) -> None:
        # \u5168\u5931\u8d25 -> \u5a92\u4f53\u4e0d\u53ef\u89e3\u7801
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="a.mp4", ok=False, error="exit=1"),
            FFProbeOutput(url="b.mp4", ok=False, error="timeout"),
        ))
        branch, conf = _branch_from_l1(l1)
        assert branch == "broken"
        assert conf == L1_BRANCH_CONFIDENCE

    def test_short_duration_returns_partial_free(self) -> None:
        # duration_s <= 60 -> partial_free (\u8bd5\u770b/\u9884\u544a)
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="trailer.mp4", ok=True, duration_s=5.0, has_video=True),
        ))
        branch, conf = _branch_from_l1(l1)
        assert branch == "partial_free"
        assert conf == L1_BRANCH_CONFIDENCE

    def test_long_duration_returns_none(self) -> None:
        # duration_s > 60 -> L1 \u4e0d\u4e0b\u5206\u652f (\u8ba9 L2/L4 \u53bb\u8bc4)
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="movie.mp4", ok=True, duration_s=1800.0, has_video=True),
        ))
        assert _branch_from_l1(l1) == (None, 0.0)


# ---------- _pick_winner \u6b65\u5165 L1 ----------

class TestPickWinnerL1:
    """_pick_winner \u63a5\u53d7 l1_branch / l1_conf \u540e\u7684\u8868\u73b0\u3002"""

    def test_only_l1_returns_l1_branch(self) -> None:
        # \u4ec5 L1 \u6709\u5224\u51fa -> L1 \u8d62
        winner = _pick_winner(
            l3_branch=None, l3_conf=0.0,
            l4_branch=None, l4_conf=0.0, l4_source="",
            l2_branch=None,
            l1_branch="partial_free", l1_conf=0.8,
        )
        assert winner == "partial_free"

    def test_l1_matches_l3_returns_l3(self) -> None:
        # L1 \u4e0e L3 \u4e00\u81f4 -> \u91c7\u7eb3
        winner = _pick_winner(
            l3_branch="partial_free", l3_conf=0.85,
            l4_branch=None, l4_conf=0.0, l4_source="",
            l2_branch=None,
            l1_branch="partial_free", l1_conf=0.8,
        )
        assert winner == "partial_free"

    def test_l1_conflicts_l3_l1_wins(self) -> None:
        # L1 \u4e0e L3 \u51b2\u7a81 -> L1 weight 2 * 0.8 = 1.6 vs rule 1 * 0.85 = 0.85 -> L1 \u8d62
        winner = _pick_winner(
            l3_branch="blocked", l3_conf=0.85,
            l4_branch=None, l4_conf=0.0, l4_source="",
            l2_branch=None,
            l1_branch="partial_free", l1_conf=0.8,
        )
        assert winner == "partial_free"

    def test_l1_conflicts_llm_loses(self) -> None:
        # L1 \u4e0e L4 llm \u51b2\u7a81 -> llm weight 3 * 0.9 = 2.7 vs L1 2 * 0.8 = 1.6 -> L4 \u8d62
        winner = _pick_winner(
            l3_branch=None, l3_conf=0.0,
            l4_branch="subscription_wall", l4_conf=0.9, l4_source="llm",
            l2_branch=None,
            l1_branch="partial_free", l1_conf=0.8,
        )
        assert winner == "subscription_wall"

    def test_no_l1_unchanged_behavior(self) -> None:
        # L1 \u672a\u8dd1 -> \u4e0e\u539f\u5148\u4ef2\u88c1\u903b\u8f91\u4e00\u81f4
        winner_no = _pick_winner(
            l3_branch="partial_free", l3_conf=0.85,
            l4_branch=None, l4_conf=0.0, l4_source="",
            l2_branch=None,
        )
        winner_with_none = _pick_winner(
            l3_branch="partial_free", l3_conf=0.85,
            l4_branch=None, l4_conf=0.0, l4_source="",
            l2_branch=None,
            l1_branch=None, l1_conf=0.0,
        )
        assert winner_no == winner_with_none == "partial_free"


# ---------- arbitrate() \u8d2f L1 \u8f93\u51fa SiteOutcome ----------

class TestArbitrateL1:
    """arbitrate() \u4f1a\u8bfb result.l1_probe \u4ea7\u51fa SiteOutcome\u3002"""

    def test_l1_partial_free_alone_yields_partial_free(self) -> None:
        # L1 partial_free \u72ec\u5b58 -> verdict = partial_free
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="t.mp4", ok=True, duration_s=5.0, has_video=True),
        ))
        result = _mk_browser_result(l1_probe=l1)
        out = arbitrate(result)
        assert out.branch == "partial_free"
        # evidence \u5305\u542b L1 \u8d77\u59cb\u8bc1\u636e
        assert "L1" in out.evidence

    def test_l1_broken_alone_yields_broken(self) -> None:
        # L1 broken \u72ec\u5b58 -> verdict = broken
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="a.mp4", ok=False, error="exit=1"),
            FFProbeOutput(url="b.mp4", ok=False, error="exit=1"),
        ))
        result = _mk_browser_result(l1_probe=l1)
        out = arbitrate(result)
        assert out.branch == "broken"

    def test_no_l1_unresolved_when_nothing_else(self) -> None:
        # \u5168\u65e0 -> unresolved (\u4e0e\u539f\u5148\u4e00\u81f4)
        result = _mk_browser_result(l1_probe=None)
        out = arbitrate(result)
        assert out.branch == "unresolved"

    def test_l1_conflicts_llm_llm_wins(self) -> None:
        # L1 partial_free + L4 llm subscription_wall -> llm \u8d62
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="t.mp4", ok=True, duration_s=5.0, has_video=True),
        ))
        vlm = _mk_vlm(label="paywall", paywall_type="subscription", source="llm", confidence=0.9)
        result = _mk_browser_result(l1_probe=l1, vlm_review=vlm)
        out = arbitrate(result)
        assert out.branch == "subscription_wall"

    def test_l1_matches_l3_yields_l3_branch(self) -> None:
        # L1 partial_free + L3 partial_free -> \u91c7\u7eb3 partial_free
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="t.mp4", ok=True, duration_s=5.0, has_video=True),
        ))
        l3 = _mk_decision(access_kind="paywall", paywall_type="trial", confidence=0.8)
        result = _mk_browser_result(l1_probe=l1, l3_decision=l3)
        out = arbitrate(result)
        assert out.branch == "partial_free"


# ---------- dump JSON \u542b l1_probe ----------

class TestDumpL1Field:
    """dump_browser_result() \u7684 JSON \u8f93\u51fa\u5b57\u6bb5\u96c6\u542b l1_probe\u3002"""

    def test_dump_with_l1_contains_field(self, tmp_path: Path) -> None:
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="t.mp4", ok=True, duration_s=5.0, has_video=True, format_name="mov"),
        ))
        result = _mk_browser_result(l1_probe=l1)
        out_path = tmp_path / "p.json"
        dump_browser_result(result, out_path)
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert "l1_probe" in data
        assert data["l1_probe"] is not None
        # \u5b50\u5b57\u6bb5\u540d\u9a8c\u8bc1
        assert "media_urls" in data["l1_probe"]
        assert "probed" in data["l1_probe"]

    def test_dump_without_l1_field_is_none(self, tmp_path: Path) -> None:
        result = _mk_browser_result(l1_probe=None)
        out_path = tmp_path / "p.json"
        dump_browser_result(result, out_path)
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["l1_probe"] is None

    def test_dump_l1_probed_items_have_ok(self, tmp_path: Path) -> None:
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="ok.mp4", ok=True, duration_s=5.0, has_video=True),
            FFProbeOutput(url="bad.mp4", ok=False, error="exit=1"),
        ))
        result = _mk_browser_result(l1_probe=l1)
        out_path = tmp_path / "p.json"
        dump_browser_result(result, out_path)
        data = json.loads(out_path.read_text(encoding="utf-8"))
        probed = data["l1_probe"]["probed"]
        assert len(probed) == 2
        assert probed[0]["ok"] is True
        assert probed[1]["ok"] is False
