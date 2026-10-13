"""L3 rule perceptor contract tests (incr 26).

设计纪律:
1. L3 独立判定（不复用 L4 反推）。
2. L3 词典独立但与 L4 同源对齐（不同 dict，同义词表）。
3. L3 决策 Question 不走 REGISTRY（business.signal / paywall.type），L5 仲裁层消费。
4. L3 触发条件 = 与 L4 同 fail-closed 跳过（不在此处测，由 test_cmd_mediabrowse.TestL3Wiring 覆盖）。

测试主题（20+ contract）：
  T01-T07   Schema 一致性（7 access_kind / 4 paywall_type / 优先级顺序）
  T08-T13   各 access_kind 关键词命中（region/anti_bot/captcha/not_found/login/paywall）
  T14-T15   paywall_type 细粒度（preview / trial）
  T16-T17   fail-closed（空 body_text / 无命中 → unknown）
  T18       优先级：region > paywall（设计纪律）
  T19       Decision shape（不入 REGISTRY；answer=True 表示已判出）
  T20       幂等：同一 obs 两次调用产同一 Decision（I3）
  T21       词典独立性：L3 与 L4 不同 dict 对象
  T22       词典覆盖度：6 个非 unknown 类至少有 5 个关键词（兜底门槛）
"""
from __future__ import annotations

import pytest

from trajectory_pipeline.perception.base import Observation
from trajectory_pipeline.perception.rule_perceptor import (
    L3_ACCESS_HINTS,
    L3_ACCESS_KIND_PRIORITY,
    L3_ACCESS_KINDS,
    L3_CONF_MEDIUM,
    L3_CONF_STRONG,
    L3_CONF_WEAK,
    L3_PAYWALL_TYPES,
    PAYWALL_WORDS,
    RulePerceptor,
    _l3_access_kind_confidence,
    _l3_find_first_hit,
)
from trajectory_pipeline.executor.review.vlm_reviewer import (
    _BLOCKED_HINTS,
    _LOGIN_HINTS,
    _PAYWALL_HINTS,
)


_R = RulePerceptor()


def _obs(text):
    return Observation(url="x", page_title="t", body_text=text)


# ===== T01-T07 Schema 一致性 =====
class TestL3SchemaConsistency:

    def test_t01_access_kinds_seven(self):
        assert len(L3_ACCESS_KINDS) == 7
        assert "unknown" in L3_ACCESS_KINDS
        for k in ("region", "anti_bot", "captcha", "not_found", "login", "paywall"):
            assert k in L3_ACCESS_KINDS, f"missing access_kind: {k}"

    def test_t02_paywall_types_four(self):
        assert L3_PAYWALL_TYPES == ("none", "preview", "trial", "unknown")

    def test_t03_priority_order(self):
        assert L3_ACCESS_KIND_PRIORITY[0] == "region"
        assert L3_ACCESS_KIND_PRIORITY[-1] == "unknown"
        assert L3_ACCESS_KIND_PRIORITY[-2] == "paywall"
        assert L3_ACCESS_KIND_PRIORITY == (
            "region", "anti_bot", "captcha", "not_found", "login", "paywall", "unknown",
        )

    def test_t04_access_hints_keys_match_kinds(self):
        assert set(L3_ACCESS_HINTS.keys()) == set(L3_ACCESS_KINDS)

    def test_t05_unknown_hints_empty(self):
        assert L3_ACCESS_HINTS["unknown"] == ()

    def test_t06_paywall_hints_reuse_paywall_words(self):
        assert L3_ACCESS_HINTS["paywall"] == PAYWALL_WORDS

    def test_t07_confidence_three_levels(self):
        assert L3_CONF_STRONG == 0.85
        assert L3_CONF_MEDIUM == 0.65
        assert L3_CONF_WEAK == 0.30
        assert 0 < L3_CONF_WEAK < L3_CONF_MEDIUM < L3_CONF_STRONG < 1


# ===== T08-T13 各 access_kind 关键词命中 =====
class TestL3KeywordPriority:

    def test_t08_region_hit(self):
        d = _R.decide_business_signal(_obs("该视频仅限中国大陆播放"))
        assert d.payload["access_kind"] == "region"
        assert d.confidence == L3_CONF_STRONG
        assert "hit_keyword" in d.payload

    def test_t09_anti_bot_hit(self):
        d = _R.decide_business_signal(_obs("performing security verification, please wait"))
        assert d.payload["access_kind"] == "anti_bot"
        assert d.confidence == L3_CONF_STRONG

    def test_t10_captcha_hit(self):
        d = _R.decide_business_signal(_obs("请完成验证后继续"))
        assert d.payload["access_kind"] == "captcha"

    def test_t11_not_found_hit(self):
        d = _R.decide_business_signal(_obs("抱歉，该视频不存在或已被删除"))
        assert d.payload["access_kind"] == "not_found"

    # 2026-10-13 增量三十二 · D10 偏差修缮：英文 + 通用 5xx 短语落 not_found
    def test_t11_not_found_hit_英文_404(self):
        """nginx 默认 404 页英文短语——kmslxsh.com/nginx 实测样本。"""
        d = _R.decide_business_signal(_obs("404 Not Found\nnginx"))
        assert d.payload["access_kind"] == "not_found"

    def test_t11_not_found_hit_英文_5xx(self):
        """通用 5xx 短语——ixigua Internal Server Error 实测样本。"""
        d = _R.decide_business_signal(_obs("Internal Server Error"))
        assert d.payload["access_kind"] == "not_found"

    def test_t11_not_found_hit_腾讯error页(self):
        """腾讯视频 error.html——「那条视频不见了」实测样本。"""
        d = _R.decide_business_signal(_obs("那条视频不见了"))
        assert d.payload["access_kind"] == "not_found"

    def test_t12_login_hit(self):
        d = _R.decide_business_signal(_obs("请先登录后观看完整版"))
        assert d.payload["access_kind"] == "login"
        assert d.confidence == L3_CONF_MEDIUM

    def test_t13_paywall_hit(self):
        d = _R.decide_business_signal(_obs("开通vip观看完整版"))
        assert d.payload["access_kind"] == "paywall"
        assert d.confidence == L3_CONF_STRONG


# ===== T14-T15 paywall_type 细粒度 =====
class TestL3PaywallType:

    def test_t14_preview(self):
        d = _R.decide_paywall_type(_obs("免费试看 6 分钟"))
        assert d.payload["paywall_type"] == "preview"
        assert d.confidence == L3_CONF_STRONG
        assert d.payload["access_kind"] == "paywall"

    def test_t15_trial_or_unknown(self):
        d = _R.decide_paywall_type(_obs("开通vip观看完整版"))
        assert d.payload["paywall_type"] in ("unknown", "none")
        assert d.confidence in (L3_CONF_WEAK, L3_CONF_MEDIUM)


# ===== T16-T17 fail-closed =====
class TestL3FailClosed:

    def test_t16_empty_body(self):
        d = _R.decide_business_signal(_obs(""))
        assert d.payload["access_kind"] == "unknown"
        assert d.confidence == L3_CONF_WEAK
        assert d.payload["hit_keyword"] == ""

    def test_t17_no_keyword_hit(self):
        d = _R.decide_business_signal(_obs("this is some normal web page text"))
        assert d.payload["access_kind"] == "unknown"
        assert d.confidence == L3_CONF_WEAK
        assert d.payload["hit_keyword"] == ""


# ===== T18 优先级：region > paywall =====
class TestL3PriorityOverPaywall:

    def test_t18_region_beats_paywall(self):
        text = "该视频仅限中国大陆，开通vip观看完整版"
        d = _R.decide_business_signal(_obs(text))
        assert d.payload["access_kind"] == "region"
        assert d.payload["hit_keyword"] in L3_ACCESS_HINTS["region"]


# ===== T19 Decision shape =====
class TestL3DecisionShape:

    def test_t19_decision_shape(self):
        d = _R.decide_business_signal(_obs("该地区不支持播放"))
        assert d.question == "business.signal"
        assert d.answer is True
        assert d.source == "rule"
        assert d.confidence > 0
        assert dict(d.payload)["access_kind"] == "region"
        assert "hit_keyword" in d.payload
        assert "paywall_type" in d.payload


# ===== T20 幂等 =====
class TestL3Idempotent:

    def test_t20_idempotent(self):
        obs = _obs("请先登录后观看完整版")
        d1 = _R.decide_business_signal(obs)
        d2 = _R.decide_business_signal(obs)
        assert d1.payload == d2.payload
        assert d1.confidence == d2.confidence
        assert d1.payload["access_kind"] == "login"


# ===== T21 词典独立性 =====
class TestL3VsL4DictionaryIndependence:

    def test_t21_dict_independence(self):
        assert L3_ACCESS_HINTS["region"] is not _BLOCKED_HINTS
        assert L3_ACCESS_HINTS["login"] is not _LOGIN_HINTS
        assert L3_ACCESS_HINTS["paywall"] is not _PAYWALL_HINTS
        assert L3_ACCESS_HINTS["paywall"] == PAYWALL_WORDS


# ===== T22 词典覆盖度 =====
class TestL3CoverageBaseline:

    @pytest.mark.parametrize("kind", [
        "region", "anti_bot", "captcha", "not_found", "login", "paywall",
    ])
    def test_t22_each_kind_has_min_5_keywords(self, kind):
        hints = L3_ACCESS_HINTS[kind]
        assert isinstance(hints, tuple)
        assert len(hints) >= 5, f"{kind} only has {len(hints)} keywords"


# ===== helpers smoke =====
class TestL3HelpersSmoke:

    def test_find_first_hit_returns_first_match(self):
        hit = _l3_find_first_hit("a 该地区不支持 b", L3_ACCESS_HINTS["region"])
        assert hit == "该地区不支持"

    def test_find_first_hit_none_on_empty(self):
        assert _l3_find_first_hit("", L3_ACCESS_HINTS["region"]) is None
        assert _l3_find_first_hit("nothing here", ()) is None

    def test_confidence_strong_kinds(self):
        for k in ("region", "anti_bot", "captcha", "not_found", "paywall"):
            assert _l3_access_kind_confidence(k) == L3_CONF_STRONG

    def test_confidence_login_is_medium(self):
        assert _l3_access_kind_confidence("login") == L3_CONF_MEDIUM

    def test_confidence_unknown_is_weak(self):
        assert _l3_access_kind_confidence("unknown") == L3_CONF_WEAK
        assert _l3_access_kind_confidence("__bogus__") == L3_CONF_WEAK
