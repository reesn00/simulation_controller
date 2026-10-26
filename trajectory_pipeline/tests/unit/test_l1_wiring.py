"""L1 ffprobe 真接入 unit tests (增量二十八)。

覆盖：
1. _branch_from_l1 边界 (None / 无媒体 / 全失败 / duration<=60 / duration>60)
3. _pick_winner 步入 l1_branch / l1_conf (不冲突 / 与 L3 一致 / 与 L4 冲突 / 仅 L1)
4. arbitrate() 贯 L1 会 产出 SiteOutcome
5. BrowserProbeResult.l1_probe 字段在 dump JSON 中出现
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


# ---------- 固定 factory ----------

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
    """造一个 MediaProbeResult——仅 probed / media_urls 两字段在仲裁里有用。"""
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


# ---------- _branch_from_l1 边界 ----------

class TestBranchFromL1:
    """L1 -> branch + confidence 边界。"""

    def test_none_returns_none_zero(self) -> None:
        assert _branch_from_l1(None) == (None, 0.0)

    def test_no_probed_returns_none_zero(self) -> None:
        # 没解析出媒体 -> L1 没结论
        l1 = _mk_l1(probed=())
        assert _branch_from_l1(l1) == (None, 0.0)

    def test_all_failed_returns_broken(self) -> None:
        # 全失败 -> 媒体不可解码
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="a.mp4", ok=False, error="exit=1"),
            FFProbeOutput(url="b.mp4", ok=False, error="timeout"),
        ))
        branch, conf = _branch_from_l1(l1)
        assert branch == "broken"
        assert conf == L1_BRANCH_CONFIDENCE

    def test_short_duration_returns_partial_free(self) -> None:
        # duration_s <= 60 -> partial_free (试看/预告)
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="trailer.mp4", ok=True, duration_s=5.0, has_video=True),
        ))
        branch, conf = _branch_from_l1(l1)
        assert branch == "partial_free"
        assert conf == L1_BRANCH_CONFIDENCE

    def test_long_duration_returns_none(self) -> None:
        # duration_s > 60 -> L1 不下分支 (让 L2/L4 去评)
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="movie.mp4", ok=True, duration_s=1800.0, has_video=True),
        ))
        assert _branch_from_l1(l1) == (None, 0.0)


# ---------- _pick_winner 步入 L1 ----------

class TestPickWinnerL1:
    """_pick_winner 接受 l1_branch / l1_conf 后的表现。"""

    def test_only_l1_returns_l1_branch(self) -> None:
        # 仅 L1 有判出 -> L1 赢
        winner = _pick_winner(
            l3_branch=None, l3_conf=0.0,
            l4_branch=None, l4_conf=0.0, l4_source="",
            l2_branch=None,
            l1_branch="partial_free", l1_conf=0.8,
        )
        assert winner == "partial_free"

    def test_l1_matches_l3_returns_l3(self) -> None:
        # L1 与 L3 一致 -> 采纳
        winner = _pick_winner(
            l3_branch="partial_free", l3_conf=0.85,
            l4_branch=None, l4_conf=0.0, l4_source="",
            l2_branch=None,
            l1_branch="partial_free", l1_conf=0.8,
        )
        assert winner == "partial_free"

    def test_l1_conflicts_l3_l1_wins(self) -> None:
        # L1 与 L3 冲突 -> L1 weight 2 * 0.8 = 1.6 vs rule 1 * 0.85 = 0.85 -> L1 赢
        winner = _pick_winner(
            l3_branch="blocked", l3_conf=0.85,
            l4_branch=None, l4_conf=0.0, l4_source="",
            l2_branch=None,
            l1_branch="partial_free", l1_conf=0.8,
        )
        assert winner == "partial_free"

    def test_l1_conflicts_llm_loses(self) -> None:
        # L1 与 L4 llm 冲突 -> llm weight 3 * 0.9 = 2.7 vs L1 2 * 0.8 = 1.6 -> L4 赢
        winner = _pick_winner(
            l3_branch=None, l3_conf=0.0,
            l4_branch="subscription_wall", l4_conf=0.9, l4_source="llm",
            l2_branch=None,
            l1_branch="partial_free", l1_conf=0.8,
        )
        assert winner == "subscription_wall"

    def test_no_l1_unchanged_behavior(self) -> None:
        # L1 未跑 -> 与原先仲裁逻辑一致
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


# ---------- arbitrate() 贯 L1 输出 SiteOutcome ----------

class TestArbitrateL1:
    """arbitrate() 会读 result.l1_probe 产出 SiteOutcome。"""

    def test_l1_partial_free_alone_yields_partial_free(self) -> None:
        # L1 partial_free 独存 -> verdict = partial_free
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="t.mp4", ok=True, duration_s=5.0, has_video=True),
        ))
        result = _mk_browser_result(l1_probe=l1)
        out = arbitrate(result)
        assert out.branch == "partial_free"
        # evidence 包含 L1 起始证据
        assert "L1" in out.evidence

    def test_l1_broken_alone_yields_broken(self) -> None:
        # L1 broken 独存 -> verdict = broken
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="a.mp4", ok=False, error="exit=1"),
            FFProbeOutput(url="b.mp4", ok=False, error="exit=1"),
        ))
        result = _mk_browser_result(l1_probe=l1)
        out = arbitrate(result)
        assert out.branch == "broken"

    def test_no_l1_unresolved_when_nothing_else(self) -> None:
        # 全无 -> unresolved (与原先一致)
        result = _mk_browser_result(l1_probe=None)
        out = arbitrate(result)
        assert out.branch == "unresolved"

    def test_l1_conflicts_llm_llm_wins(self) -> None:
        # L1 partial_free + L4 llm subscription_wall -> llm 赢
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="t.mp4", ok=True, duration_s=5.0, has_video=True),
        ))
        vlm = _mk_vlm(label="paywall", paywall_type="subscription", source="llm", confidence=0.9)
        result = _mk_browser_result(l1_probe=l1, vlm_review=vlm)
        out = arbitrate(result)
        assert out.branch == "subscription_wall"

    def test_l1_matches_l3_yields_l3_branch(self) -> None:
        # L1 partial_free + L3 partial_free -> 采纳 partial_free
        l1 = _mk_l1(probed=(
            FFProbeOutput(url="t.mp4", ok=True, duration_s=5.0, has_video=True),
        ))
        l3 = _mk_decision(access_kind="paywall", paywall_type="trial", confidence=0.8)
        result = _mk_browser_result(l1_probe=l1, l3_decision=l3)
        out = arbitrate(result)
        assert out.branch == "partial_free"


# ---------- dump JSON 含 l1_probe ----------

class TestDumpL1Field:
    """dump_browser_result() 的 JSON 输出字段集含 l1_probe。"""

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
        # 子字段名验证
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
