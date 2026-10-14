# -*- coding: utf-8 -*-
"""L4 VLM 复核单元测试（2026-10-13 增量二十五）。"""
from __future__ import annotations

from trajectory_pipeline.executor.probe.media_probe import MediaSignal
from trajectory_pipeline.executor.review.vlm_reviewer import (
    HeuristicVLMReviewer,
    L4_CURRENCIES,
    L4_LABELS,
    L4_PAYWALL_TYPES,
    VLMReviewer,
    VLMReviewInput,
    VLMReviewResult,
)


# 辅助工具

def _empty_signal():
    return MediaSignal(False, False, None, None, False, None)


def _input_with(text="", body="", snap="", page_quotes=()):
    return VLMReviewInput(
        signal=_empty_signal(),
        body_excerpt=body or text,
        snapshot_excerpt=snap,
        page_quotes=page_quotes,
        final_url="",
    )


# 设计文档一致性

class TestL4SchemaConsistency:
    def test_L4_labels_match_design_doc(self):
        expected = (
            "playable", "ads_then_playable", "paywall", "login_required",
            "partial_free", "blocked", "broken", "empty", "unknown",
        )
        assert L4_LABELS == expected

    def test_L4_paywall_types_match_design_doc(self):
        expected = (
            "none", "paid_subscription", "rental", "purchase",
            "trial", "unknown",
        )
        assert L4_PAYWALL_TYPES == expected

    def test_L4_currencies_are_supported_iso4217_subset(self):
        assert L4_CURRENCIES == ("CNY", "USD", "EUR", "JPY", "HKD", "GBP")


# MediaSignal 强信号决策树

class TestHeuristicSignalDecision:
    def test_heuristic_review_broken_on_media_error(self):
        sig = MediaSignal(True, False, 2.0, 10, False, 2)
        rv = HeuristicVLMReviewer().review(VLMReviewInput(signal=sig))
        assert rv.label == "broken"
        assert rv.paywall_type == "none"
        assert rv.confidence >= 0.8
        assert "2" in rv.evidence
        assert rv.source == "heuristic"

    def test_heuristic_review_broken_on_black_screen(self):
        sig = MediaSignal(False, False, None, None, True, None)
        rv = HeuristicVLMReviewer().review(VLMReviewInput(signal=sig))
        assert rv.label == "broken"
        assert "黑屏" in rv.evidence or "videoWidth" in rv.evidence
        assert rv.confidence >= 0.8

    def test_heuristic_review_partial_free_on_short_progress(self):
        sig = MediaSignal(True, True, 10.0, 200, False, None)
        rv = HeuristicVLMReviewer().review(VLMReviewInput(signal=sig))
        assert rv.label == "partial_free"
        assert rv.paywall_type == "none"
        assert rv.confidence >= 0.8
        assert "10" in rv.evidence

    def test_heuristic_review_playable_on_long_progress(self):
        sig = MediaSignal(True, True, 120.0, 3000, False, None)
        rv = HeuristicVLMReviewer().review(VLMReviewInput(signal=sig))
        assert rv.label == "playable"
        assert rv.paywall_type == "none"
        assert rv.confidence >= 0.8
        assert "120" in rv.evidence

    def test_heuristic_review_signal_priority_over_kw_text(self):
        sig = MediaSignal(True, True, 30.0, 600, False, None)
        inp = VLMReviewInput(signal=sig, body_excerpt="试看6分钟")
        rv = HeuristicVLMReviewer().review(inp)
        assert rv.label == "partial_free"


# 关键词命中优先级

class TestHeuristicKeywordPriority:
    def test_heuristic_review_blocked_on_region_keywords(self):
        rv = HeuristicVLMReviewer().review(_input_with("当前设备不支持播放"))
        assert rv.label == "blocked"
        assert "当前设备不支持播放" in rv.evidence
        assert rv.confidence >= 0.8

    def test_heuristic_review_login_required_on_login_keywords(self):
        rv = HeuristicVLMReviewer().review(_input_with("请先登录，登录后观看完整视频"))
        assert rv.label == "login_required"
        assert rv.paywall_type == "none"
        assert rv.confidence >= 0.8

    def test_heuristic_review_rental_on_rental_keywords(self):
        rv = HeuristicVLMReviewer().review(_input_with("单片购买 5元观看"))
        assert rv.label == "paywall"
        assert rv.paywall_type == "rental"
        assert rv.confidence >= 0.8

    def test_heuristic_review_paywall_vip_on_subscription_keywords(self):
        rv = HeuristicVLMReviewer().review(_input_with("开通VIP即可观看完整版"))
        assert rv.label == "paywall"
        assert rv.paywall_type == "paid_subscription"
        assert rv.confidence >= 0.8

    def test_heuristic_review_partial_free_on_text_keyword_free_text(self):
        # 纯「试看6分钟」文案 + 无媒体信号 → partial_free，置信度 MEDIUM。
        # 注意：「试看6分钟」是 _PAYWALL_HINTS 里的关键词（试看是付费墙
        # 形态的子集），paywall 优先级高于 partial_free——所以文案
        # 里不能带「开通VIP / 订阅」等高优先级付费提示。
        rv = HeuristicVLMReviewer().review(_input_with("免费试看前6分钟"))
        assert rv.label == "partial_free"
        assert rv.paywall_type == "none"
        assert 0.5 <= rv.confidence < 0.8

    def test_heuristic_review_unknown_when_no_signal(self):
        rv = HeuristicVLMReviewer().review(_input_with(""))
        assert rv.label == "unknown"
        assert rv.paywall_type == "none"
        assert rv.confidence <= 0.4
        assert rv.note == "no_signal"


# 价格 + 货币抽取

class TestHeuristicPriceExtract:
    def test_heuristic_review_extracts_price_cny(self):
        rv = HeuristicVLMReviewer().review(_input_with("开通VIP ¥25 即可观看"))
        assert rv.price == 25.0
        assert rv.currency == "CNY"

    def test_heuristic_review_extracts_price_usd(self):
        # 跨语言混排：中文付费墙关键词（_PAYWALL_HINTS 命中「开通VIP」）
        # + 英文 USD 价格。注意：rental 分支不抽 price（设计使然
        # —— rental 价是「单片价」语义弱于 subscription 月度价），
        # 只有 subscription 分支才调 _extract_price，所以这里用「开通VIP」。
        rv = HeuristicVLMReviewer().review(_input_with("开通VIP $5.99 即可观看"))
        assert rv.label == "paywall"
        assert rv.paywall_type == "paid_subscription"
        assert rv.price == 5.99
        assert rv.currency == "USD"

    def test_heuristic_review_no_price_when_no_number(self):
        rv = HeuristicVLMReviewer().review(_input_with("开通VIP观看"))
        assert rv.price is None
        assert rv.currency is None


# VLMReviewResult 序列化形态

class TestVLMReviewResult:
    def test_vlm_review_result_to_dict_serializable(self):
        rv = VLMReviewResult(
            label="paywall",
            paywall_type="rental",
            evidence="命中租赁文案",
            price=5.0,
            currency="USD",
            confidence=0.85,
            screenshots=("path/a.png",),
            note="kw_rental",
            source="heuristic",
        )
        d = rv.to_dict()
        assert d["label"] == "paywall"
        assert d["paywall_type"] == "rental"
        assert d["evidence"] == "命中租赁文案"
        assert d["price"] == 5.0
        assert d["currency"] == "USD"
        assert d["confidence"] == 0.85
        assert d["screenshots"] == ["path/a.png"]
        assert d["note"] == "kw_rental"
        assert d["source"] == "heuristic"

    def test_vlm_review_result_empty_screenshots_default(self):
        rv = VLMReviewResult(label="unknown", paywall_type="none", evidence="x")
        assert rv.screenshots == ()
        assert rv.to_dict()["screenshots"] == []


# 协议

class TestVLMReviewerProtocol:
    def test_vlm_reviewer_protocol_runtime_checkable(self):
        assert isinstance(HeuristicVLMReviewer(), VLMReviewer)

    def test_multim_vlm_reviewer_also_satisfies_protocol(self):
        # 增量三十四（2026-10-13）：MultimVLMReviewer 必须同 Protocol 等价。
        # 协议先行——任何满足 VLMReviewer 协议的类都可被 _run_l4_review_async 吃下。
        from trajectory_pipeline.executor.review.multim_vlm import MultimVLMReviewer
        assert isinstance(MultimVLMReviewer(), VLMReviewer)


# 拼文本

class TestCombinedText:
    def test_combined_text_prefers_page_quotes(self):
        inp = VLMReviewInput(
            signal=_empty_signal(),
            body_excerpt="body 文案",
            snapshot_excerpt="snapshot 文案",
            page_quotes=("当前设备不支持播放",),
        )
        combined = inp.combined_text()
        assert combined.index("当前设备不支持播放") < combined.index("body 文案")
        assert combined.index("当前设备不支持播放") < combined.index("snapshot 文案")

    def test_combined_text_handles_empty_page_quotes(self):
        inp = VLMReviewInput(
            signal=_empty_signal(),
            body_excerpt="试看6分钟",
            snapshot_excerpt="",
            page_quotes=(),
        )
        combined = inp.combined_text()
        assert "试看6分钟" in combined


# fail-soft

class TestFailSoft:
    def test_heuristic_review_handles_empty_signal_gracefully(self):
        rv = HeuristicVLMReviewer().review(VLMReviewInput(signal=_empty_signal()))
        assert rv.label == "unknown"
        assert rv.source == "heuristic"

    def test_heuristic_review_handles_unicode_text(self):
        # emoji + 试看 关键词——不抛。
        rv = HeuristicVLMReviewer().review(_input_with("\U0001F3AC 试看 6 分钟 \U0001F6AB"))
        assert rv.label in ("partial_free", "unknown")
