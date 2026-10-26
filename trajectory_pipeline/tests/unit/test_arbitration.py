"""L5 仲裁层 unit tests (增量二十七)。

覆盖（1）L3 → branch 映射（access_kind 7 值 × 上下游）；
（2）L4 → branch 映射（label 9 值 × paywall_type 6 值）；
（3）L2 signal 启发式（实播 / 黑屏 / 错误代码 / 无媒体）；
（4）N-of-M 仲裁路径（一致 / 冲突 / 仅 L3 / 仅 L4 / 全无）；
（5）fail-closed 短路（4 类 note）；
（6）输出 SiteOutcome 形状 + 21 分支合法性。
（口径：``BRANCH_LABELS`` 全集 21 key = 20 活跃 + 1 deprecated
``login_wall_or_blocked``；判定层不主动产出 deprecated，历史存档才有。）
"""
from __future__ import annotations

import pytest

from trajectory_pipeline.executor.arbitration import (
    L3_ACCESS_KIND_TO_BRANCH,
    L4_LABEL_TO_BRANCH,
    L5_FAIL_CLOSED_NOTES,
    L5_SOURCE_WEIGHT,
    _branch_from_l3,
    _branch_from_l4,
    _branch_from_signal,
    _pick_winner,
    arbitrate,
)
from trajectory_pipeline.executor.branches import (
    NON_SAMPLE_BRANCHES,
    SiteOutcome,
    BRANCH_LABELS,
)
from trajectory_pipeline.executor.probe.media_probe import (
    BrowserProbeResult,
    MediaSignal,
)
from trajectory_pipeline.executor.review.vlm_reviewer import VLMReviewResult
from trajectory_pipeline.perception.base import Decision


# ---------- 固定 factory ----------

def _mk_decision(
    question: str = "business.signal",
    answer: bool | None = True,
    confidence: float = 0.85,
    source: str = "rule",
    payload: dict | None = None,
) -> Decision:
    return Decision(
        question=question,
        answer=answer,
        confidence=confidence,
        evidence="unit",
        source=source,
        payload=payload or {},
    )


def _mk_signal(
    play_started: bool = False,
    timeupdate_progress: bool = False,
    current_time_sec: float | None = None,
    frames_decoded: int | None = None,
    black_screen: bool = False,
    media_error: int | None = None,
) -> MediaSignal:
    return MediaSignal(
        play_started=play_started,
        timeupdate_progress=timeupdate_progress,
        current_time_sec=current_time_sec,
        frames_decoded=frames_decoded,
        black_screen=black_screen,
        media_error=media_error,
    )


def _mk_vlm(
    label: str = "unknown",
    paywall_type: str = "none",
    confidence: float = 0.85,
    source: str = "heuristic",
    evidence: str = "L4 unit",
) -> VLMReviewResult:
    return VLMReviewResult(
        label=label,
        paywall_type=paywall_type,
        evidence=evidence,
        confidence=confidence,
        source=source,
    )


def _mk_result(
    *,
    url: str = "https://u.test/",
    media_count: int = 0,
    note: str = "",
    signal: MediaSignal | None = None,
    l3: Decision | None = None,
    vlm: VLMReviewResult | None = None,
) -> BrowserProbeResult:
    return BrowserProbeResult(
        requested_url=url,
        final_url=url,
        driver_name="fake",
        ok=True,
        media_count=media_count,
        signal=signal if signal is not None else _mk_signal(),
        l3_decision=l3,
        vlm_review=vlm,
        note=note,
    )


# ====== T01-T10 L3 → branch mapping ======

class TestL3ToBranch:
    def test_region_to_blocked(self):
        l3 = _mk_decision(payload={"access_kind": "region", "hit_keyword": "仅限中国", "paywall_type": "none"})
        branch, conf = _branch_from_l3(l3)
        assert branch == "blocked"
        assert conf == 0.85

    def test_anti_bot_to_blocked(self):
        l3 = _mk_decision(payload={"access_kind": "anti_bot", "hit_keyword": "cf", "paywall_type": "none"})
        branch, _ = _branch_from_l3(l3)
        assert branch == "blocked"

    def test_captcha_to_blocked(self):
        l3 = _mk_decision(payload={"access_kind": "captcha", "hit_keyword": "captcha", "paywall_type": "none"})
        branch, _ = _branch_from_l3(l3)
        assert branch == "blocked"

    def test_not_found_to_broken(self):
        l3 = _mk_decision(payload={"access_kind": "not_found", "hit_keyword": "404", "paywall_type": "none"})
        branch, _ = _branch_from_l3(l3)
        assert branch == "broken"

    def test_login_to_login_required(self):
        l3 = _mk_decision(payload={"access_kind": "login", "hit_keyword": "请登录", "paywall_type": "none"})
        branch, _ = _branch_from_l3(l3)
        assert branch == "login_required"

    def test_paywall_preview_to_partial_free(self):
        l3 = _mk_decision(payload={"access_kind": "paywall", "hit_keyword": "preview", "paywall_type": "preview"})
        branch, _ = _branch_from_l3(l3)
        assert branch == "partial_free"

    def test_paywall_trial_to_partial_free(self):
        l3 = _mk_decision(payload={"access_kind": "paywall", "hit_keyword": "trial", "paywall_type": "trial"})
        branch, _ = _branch_from_l3(l3)
        assert branch == "partial_free"

    def test_paywall_paid_to_subscription_wall(self):
        l3 = _mk_decision(payload={"access_kind": "paywall", "hit_keyword": "VIP", "paywall_type": "unknown"})
        branch, _ = _branch_from_l3(l3)
        assert branch == "subscription_wall"

    def test_unknown_to_none(self):
        l3 = _mk_decision(payload={"access_kind": "unknown", "hit_keyword": "", "paywall_type": "none"})
        branch, _ = _branch_from_l3(l3)
        assert branch is None

    def test_none_l3_returns_none(self):
        assert _branch_from_l3(None) == (None, 0.0)


# ====== T11-T18 L4 → branch mapping ======

class TestL4ToBranch:
    def test_playable_to_none(self):
        vlm = _mk_vlm(label="playable")
        branch, _, source = _branch_from_l4(vlm)
        assert branch is None
        assert source == "heuristic"

    def test_ads_then_playable_to_none(self):
        vlm = _mk_vlm(label="ads_then_playable")
        branch, _, _ = _branch_from_l4(vlm)
        assert branch is None

    def test_paywall_with_trial_to_partial_free(self):
        vlm = _mk_vlm(label="paywall", paywall_type="trial")
        branch, _, _ = _branch_from_l4(vlm)
        assert branch == "partial_free"

    def test_paywall_paid_to_subscription_wall(self):
        vlm = _mk_vlm(label="paywall", paywall_type="paid_subscription")
        branch, _, _ = _branch_from_l4(vlm)
        assert branch == "subscription_wall"

    def test_login_required_label(self):
        vlm = _mk_vlm(label="login_required")
        branch, _, _ = _branch_from_l4(vlm)
        assert branch == "login_required"

    def test_blocked_label(self):
        vlm = _mk_vlm(label="blocked")
        branch, _, _ = _branch_from_l4(vlm)
        assert branch == "blocked"

    def test_broken_label(self):
        vlm = _mk_vlm(label="broken")
        branch, _, _ = _branch_from_l4(vlm)
        assert branch == "broken"

    def test_empty_unknown_to_none(self):
        vlm = _mk_vlm(label="empty")
        branch, _, _ = _branch_from_l4(vlm)
        assert branch is None


# ====== T19-T22 L2 signal heuristics ======

class TestL2SignalHeuristics:
    def test_play_started_with_progress_to_none(self):
        sig = _mk_signal(play_started=True, current_time_sec=2.5, frames_decoded=120)
        branch, ev = _branch_from_signal(sig, media_count=1, note="")
        assert branch is None
        assert "L2" in ev

    def test_play_started_timeupdate_to_none(self):
        sig = _mk_signal(play_started=True, timeupdate_progress=True)
        branch, _ = _branch_from_signal(sig, media_count=1, note="")
        assert branch is None

    def test_black_screen_to_broken(self):
        sig = _mk_signal(play_started=False, black_screen=True, frames_decoded=0)
        branch, _ = _branch_from_signal(sig, media_count=1, note="")
        assert branch == "broken"

    def test_media_error_code_4_to_broken(self):
        sig = _mk_signal(play_started=False, media_error=4)
        branch, _ = _branch_from_signal(sig, media_count=1, note="")
        assert branch == "broken"

    def test_no_media_to_not_play_site(self):
        sig = _mk_signal(play_started=False)
        branch, _ = _branch_from_signal(sig, media_count=0, note="")
        assert branch == "not_play_site"

    def test_no_signal_no_media_with_note_to_none(self):
        sig = _mk_signal(play_started=False)
        # 有 note 但不是 fail-closed，不该产生 not_play_site
        branch, _ = _branch_from_signal(sig, media_count=0, note="some_warning")
        assert branch is None


# ====== T23-T27 冲突仲裁 ======

class TestConflictArbitration:
    def test_l3_l4_consistent_blocked(self):
        # L3 region → blocked, L4 label=blocked，一致
        result = _mk_result(
            l3=_mk_decision(payload={"access_kind": "region", "hit_keyword": "x", "paywall_type": "none"}),
            vlm=_mk_vlm(label="blocked", confidence=0.65, source="heuristic"),
        )
        out = arbitrate(result)
        assert out.branch == "blocked"
        assert out.source == "arbiter"

    def test_l3_l4_conflict_l4_llm_wins(self):
        # L3 region → blocked, L4 login_required llm → login_required
        # L4 weight(3) × conf(0.85) = 2.55; L3 weight(1) × conf(0.85) = 0.85
        # → L4 胜
        result = _mk_result(
            l3=_mk_decision(payload={"access_kind": "region", "hit_keyword": "x", "paywall_type": "none"}),
            vlm=_mk_vlm(label="login_required", confidence=0.85, source="llm"),
        )
        out = arbitrate(result)
        assert out.branch == "login_required"

    def test_l3_l4_conflict_l4_heuristic_high_conf_wins(self):
        # L3 access_kind=login → login_required (conf=0.65)
        # L4 label=blocked (heuristic, conf=0.85)
        # L3: 1 × 0.65 = 0.65; L4: 2 × 0.85 = 1.7 → L4
        result = _mk_result(
            l3=_mk_decision(confidence=0.65, payload={"access_kind": "login", "hit_keyword": "y", "paywall_type": "none"}),
            vlm=_mk_vlm(label="blocked", confidence=0.85, source="heuristic"),
        )
        out = arbitrate(result)
        assert out.branch == "blocked"

    def test_l3_only_wins(self):
        result = _mk_result(
            l3=_mk_decision(payload={"access_kind": "not_found", "hit_keyword": "404", "paywall_type": "none"}),
        )
        out = arbitrate(result)
        assert out.branch == "broken"

    def test_l4_only_wins(self):
        result = _mk_result(
            vlm=_mk_vlm(label="blocked", confidence=0.85, source="heuristic"),
        )
        out = arbitrate(result)
        assert out.branch == "blocked"


# ====== T28-T31 真播放一票否决 + fail-closed ======

class TestL2AndFailClosed:
    def test_l2_real_play_overrides_l4_blocked(self):
        # L2 实播，L4 说 blocked → 依然成功（一票否决）
        result = _mk_result(
            signal=_mk_signal(play_started=True, current_time_sec=3.0, frames_decoded=200),
            vlm=_mk_vlm(label="blocked", confidence=0.85, source="heuristic"),
        )
        out = arbitrate(result)
        assert out.branch is None
        assert out.question == "playback.success"

    def test_credential_leak_note_with_l4_unresolved(self):
        # credential_leak + L4 unknown → unresolved
        result = _mk_result(
            note="credential_leak",
            vlm=_mk_vlm(label="unknown"),
        )
        out = arbitrate(result)
        assert out.branch == "unresolved"
        assert "credential_leak" in out.evidence

    def test_credential_leak_note_with_l4_login_required(self):
        # credential_leak + L4 login_required → 依然采纳 L4
        result = _mk_result(
            note="credential_leak",
            vlm=_mk_vlm(label="login_required", confidence=0.85, source="heuristic"),
        )
        out = arbitrate(result)
        assert out.branch == "login_required"
        assert "fail-closed" in out.evidence

    def test_timeout_note_with_no_l4(self):
        result = _mk_result(note="timeout")
        out = arbitrate(result)
        assert out.branch == "unresolved"

    def test_driver_missing_note(self):
        result = _mk_result(note="driver_missing")
        out = arbitrate(result)
        assert out.branch == "unresolved"

    def test_all_three_layers_absent_to_unresolved(self):
        # 全无：L3=None, L4=None, L2 也不命中
        result = _mk_result(signal=_mk_signal(play_started=False), media_count=1)
        out = arbitrate(result)
        assert out.branch == "unresolved"


# ====== T32-T36 输出形状 + 合法性 ======

class TestOutputShapeAndLegality:
    def test_returns_siteoutcome_instance(self):
        result = _mk_result()
        out = arbitrate(result)
        assert isinstance(out, SiteOutcome)

    def test_url_propagated(self):
        result = _mk_result(url="https://example.com/x")
        out = arbitrate(result)
        assert out.url == "https://example.com/x"

    def test_source_always_arbiter(self):
        result = _mk_result(l3=_mk_decision(payload={"access_kind": "region", "hit_keyword": "x", "paywall_type": "none"}))
        out = arbitrate(result)
        assert out.source == "arbiter"

    def test_evidence_contains_arbiter_marker(self):
        result = _mk_result(l3=_mk_decision(payload={"access_kind": "region", "hit_keyword": "x", "paywall_type": "none"}))
        out = arbitrate(result)
        assert "L5" in out.evidence and "仲裁" in out.evidence

    def test_branch_in_legal_set(self):
        legal_branches = set(BRANCH_LABELS.keys()) | {None}
        # sweep across multiple inputs
        for vlm_label in ["playable", "paywall", "login_required", "blocked", "broken", "partial_free", "empty"]:
            for media_count in (0, 1):
                vlm = _mk_vlm(label=vlm_label, paywall_type="trial" if vlm_label == "paywall" else "none")
                result = _mk_result(vlm=vlm, media_count=media_count, signal=_mk_signal(play_started=(media_count==1)))
                out = arbitrate(result)
                assert out.branch in legal_branches, f"illegal branch {out.branch!r} for vlm={vlm_label}, mc={media_count}"
