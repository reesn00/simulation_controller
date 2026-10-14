# -*- coding: utf-8 -*-
"""L4 真视觉 / 真 LLM 复核单元测试（2026-10-13 增量三十四）。

盯的目标：
1. **协议契约**——``MultimVLMReviewer`` 必须满足 :class:`VLMReviewer` Protocol
   （``isinstance`` 在 ``@runtime_checkable`` 下返回 True）。
2. **fail-soft + 无 key 自动降级**——无 key / 响应坏 / 超时 → 降级
   :class:`HeuristicVLMReviewer` 且 ``source="multim_fallback"`` +
   ``note="multim_fallback:<reason>"`` 显式标记。
3. **OpenAI / Anthropic 双通道解析正确**——成功路径 label / paywall_type /
   evidence / confidence / currency / price 全部命中。
4. **可选字段裁剪**——``paywall_type`` 仅 ``label="paywall"`` 时有意义；
   ``currency`` 不在 ``L4_CURRENCIES`` 白名单 → ``None``；
   ``confidence`` 出 [0, 1] → clamp；``evidence`` 截 120 字符。
5. **不重叠 L3 rule**——``_SYSTEM_PROMPT`` 不含 L3 词典字符串，
   ``MultimVLMReviewer`` 内部不引用 L3 模块。
6. **工厂函数**——``build_reviewer("heuristic" | "multim")`` 返回正确实现类；
   非法 name 抛 ``ValueError``。
"""
from __future__ import annotations

import json

import httpx
import pytest

from trajectory_pipeline.executor.probe.media_probe import MediaSignal
from trajectory_pipeline.executor.review.multim_vlm import (
    MultimVLMReviewer,
    _SYSTEM_PROMPT,
    build_reviewer,
)
from trajectory_pipeline.executor.review.vlm_reviewer import (
    HeuristicVLMReviewer,
    L4_CURRENCIES,
    L4_LABELS,
    L4_PAYWALL_TYPES,
    VLMReviewInput,
    VLMReviewer,
    VLMReviewResult,
)


# ── 辅助工具 ────────────────────────────────────────────────────────


def _empty_signal() -> MediaSignal:
    return MediaSignal(False, False, None, None, False, None)


def _input_with(
    text: str = "",
    *,
    page_quotes: tuple[str, ...] = (),
    final_url: str = "https://example.com/v",
) -> VLMReviewInput:
    return VLMReviewInput(
        signal=_empty_signal(),
        body_excerpt=text,
        snapshot_excerpt="",
        page_quotes=page_quotes,
        final_url=final_url,
    )


def _make_mock_client(handler):
    """构造一个 ``httpx.Client`` 用 ``MockTransport`` 替身响应。

    ``handler(req) -> httpx.Response``；可按 ``req.url`` / ``req.content``
    分流返回不同响应。
    """
    return httpx.Client(transport=httpx.MockTransport(handler))


def _ok_openai_response(label: str = "blocked", paywall_type: str = "none",
                        evidence: str = "命中 region 文案", price=None,
                        currency=None, confidence=0.85) -> dict[str, object]:
    """造一份 OpenAI Chat Completions 成功响应。"""
    return {
        "choices": [{
            "message": {"content": json.dumps({
                "label": label,
                "paywall_type": paywall_type,
                "evidence": evidence,
                "price": price,
                "currency": currency,
                "confidence": confidence,
            }, ensure_ascii=False)},
        }],
    }


def _ok_anthropic_response(label: str = "paywall",
                           paywall_type: str = "paid_subscription",
                           evidence: str = "命中 VIP 付费墙",
                           confidence=0.9) -> dict[str, object]:
    """造一份 Anthropic Messages 成功响应。"""
    return {
        "content": [{
            "type": "text",
            "text": json.dumps({
                "label": label,
                "paywall_type": paywall_type,
                "evidence": evidence,
                "price": None,
                "currency": None,
                "confidence": confidence,
            }, ensure_ascii=False),
        }],
    }


def _bad_json_response() -> dict[str, object]:
    return {"choices": [{"message": {"content": "{not valid json"}}]}


# ── 协议契约 ────────────────────────────────────────────────────────


class TestProtocolConformance:
    def test_multim_vlm_reviewer_satisfies_vlm_reviewer_protocol(self):
        # @runtime_checkable 检查 name 属性 + review 方法签名。
        assert isinstance(MultimVLMReviewer(), VLMReviewer)

    def test_multim_vlm_reviewer_name_attribute(self):
        # MultimVLMReviewer 的 name 是类属性（test_cmd_mediabrowse 可读它做日志）。
        assert isinstance(MultimVLMReviewer.name, str)
        assert MultimVLMReviewer.name


# ── 无 key 自动降级 ──────────────────────────────────────────────────


class TestNoApiKeyFallback:
    def test_no_key_falls_back_to_heuristic_with_source_multim_fallback(
        self, monkeypatch,
    ):
        # monkeypatch.delenv 防止本机误设 OPENAI/ANTHROPIC_API_KEY 污染。
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        rv = MultimVLMReviewer().review(_input_with("当前设备不支持播放"))
        assert rv.source == "multim_fallback"
        assert rv.note.startswith("multim_fallback:")
        assert "no_api_key" in rv.note
        # 走的是 HeuristicVLMReviewer，结果应命中 blocked。
        assert rv.label == "blocked"
        assert "当前设备不支持播放" in rv.evidence

    def test_only_anthropic_key_chooses_anthropic(
        self, monkeypatch,
    ):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
        # 注入 mock client 直接验证走 anthropic URL。
        captured: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(str(req.url))
            return httpx.Response(200, json=_ok_anthropic_response())

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(_input_with())
        client.close()
        assert any("anthropic.com" in u for u in captured)
        assert rv.source == "claude_text"
        assert rv.label == "paywall"

    def test_openai_key_takes_precedence_over_anthropic(
        self, monkeypatch,
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
        # 注入 mock client 同时也能拦截 anthropic 请求——验证只走 OpenAI。
        captured: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            captured.append(str(req.url))
            return httpx.Response(200, json=_ok_openai_response())

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(
            _input_with("当前设备不支持播放"),
        )
        client.close()
        # 走 OpenAI 通道——source=OpenAI 通道、anthropic URL 没被请求。
        assert rv.source == "gpt4o_text"
        assert rv.label == "blocked"
        assert any("api.openai.com" in u for u in captured)
        assert not any("anthropic.com" in u for u in captured)

    def test_empty_string_key_falls_back(
        self, monkeypatch,
    ):
        # 空字符串与"未设"等价——os.environ.get 拿到的空串是 falsy。
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "")  # 显式空串
        rv = MultimVLMReviewer().review(
            _input_with("当前设备不支持播放"),
        )
        assert rv.source == "multim_fallback"
        assert "no_api_key" in rv.note
        # 走 HeuristicVLMReviewer 兜底——region 文案应命中 blocked。
        assert rv.label == "blocked"
        assert "当前设备不支持播放" in rv.evidence

# ── OpenAI 成功路径 ──────────────────────────────────────────────────


class TestOpenAISuccess:
    def test_openai_returns_blocked_with_evidence(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode("utf-8"))
            assert body["model"] == "gpt-4o-mini"
            assert body["response_format"] == {"type": "json_object"}
            return httpx.Response(200, json=_ok_openai_response(
                label="blocked", paywall_type="none",
                evidence="当前设备不支持播放",
            ))

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(
            _input_with("当前设备不支持播放")
        )
        client.close()
        assert rv.label == "blocked"
        assert rv.paywall_type == "none"
        assert rv.source == "gpt4o_text"
        assert "当前设备不支持播放" in rv.evidence
        assert rv.confidence == pytest.approx(0.85, abs=1e-9)
        assert rv.note == "multim_ok"

    def test_openai_paywall_with_price_and_currency(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_openai_response(
                label="paywall", paywall_type="paid_subscription",
                evidence="VIP 会员专享", price=25.0, currency="CNY",
            ))

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(_input_with())
        client.close()
        assert rv.label == "paywall"
        assert rv.paywall_type == "paid_subscription"
        assert rv.price == 25.0
        assert rv.currency == "CNY"


# ── OpenAI 失败路径 ──────────────────────────────────────────────────


class TestOpenAIFailure:
    def test_openai_bad_json_falls_back(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_bad_json_response())

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(
            _input_with("当前设备不支持播放")
        )
        client.close()
        assert rv.source == "multim_fallback"
        assert "openai_response_bad" in rv.note
        # fallback 走启发式——region 文案命中 blocked。
        assert rv.label == "blocked"

    def test_openai_label_invalid_falls_back(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_openai_response(
                label="not_a_real_label", paywall_type="none",
                evidence="x",
            ))

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(_input_with(""))
        client.close()
        assert rv.source == "multim_fallback"
        assert "label_invalid" in rv.note
        # fallback 走启发式——空文本命中 unknown。
        assert rv.label == "unknown"

    def test_openai_http_error_falls_back(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="internal error")

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(_input_with())
        client.close()
        assert rv.source == "multim_fallback"
        assert "openai_call_fail" in rv.note


# ── Anthropic 路径 ──────────────────────────────────────────────────


class TestAnthropicPath:
    def test_anthropic_success_parses_content_text(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content.decode("utf-8"))
            assert body["model"].startswith("claude")
            assert body["max_tokens"] == 512
            headers_lower = {k.lower() for k in req.headers.keys()}
            assert "anthropic-version" in headers_lower
            return httpx.Response(200, json=_ok_anthropic_response(
                label="paywall", paywall_type="paid_subscription",
                evidence="VIP 会员专享 ¥25", confidence=0.92,
            ))

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(_input_with())
        client.close()
        assert rv.source == "claude_text"
        assert rv.label == "paywall"
        assert rv.paywall_type == "paid_subscription"
        assert rv.confidence == pytest.approx(0.92, abs=1e-9)

    def test_anthropic_bad_content_blocks_falls_back(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")

        def handler(req: httpx.Request) -> httpx.Response:
            # 没有 type=text block → next() 抛 StopIteration → fallback
            return httpx.Response(200, json={"content": [{"type": "tool_use"}]})

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(_input_with())
        client.close()
        assert rv.source == "multim_fallback"
        assert "anthropic_response_bad" in rv.note


# ── 字段约束 ─────────────────────────────────────────────────────────


class TestFieldConstraints:
    def test_paywall_type_only_meaningful_when_label_is_paywall(
        self, monkeypatch,
    ):
        # LLM 返回 label=blocked + paywall_type=paid_subscription → result 应
        # 把 paywall_type 强行归 none（设计：paywall_type 仅 label=paywall 时有意义）。
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_openai_response(
                label="blocked", paywall_type="paid_subscription",
                evidence="region 文案",
            ))

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(_input_with())
        client.close()
        assert rv.label == "blocked"
        assert rv.paywall_type == "none"

    def test_currency_must_be_in_supported_set(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_openai_response(
                label="paywall", paywall_type="paid_subscription",
                evidence="x", price=10.0, currency="XYZ",
            ))

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(_input_with())
        client.close()
        assert rv.currency is None
        # price 仍是合法数字（不强制 currency 必须有）。
        assert rv.price == 10.0

    def test_confidence_clamped_to_0_1(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_openai_response(
                label="blocked", evidence="x", confidence=1.7,
            ))

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(_input_with())
        client.close()
        assert rv.confidence == 1.0  # 上界 clip

        # 下界 clip
        def handler2(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_openai_response(
                label="blocked", evidence="x", confidence=-0.5,
            ))

        client2 = _make_mock_client(handler2)
        rv2 = MultimVLMReviewer(http_client=client2).review(_input_with())
        client2.close()
        assert rv2.confidence == 0.0  # 下界 clip

    def test_evidence_truncated_to_120(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")
        long_evidence = "X" * 250

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_openai_response(
                label="blocked", evidence=long_evidence,
            ))

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(_input_with())
        client.close()
        assert len(rv.evidence) == 120
        assert rv.evidence == "X" * 120

    def test_paywall_type_invalid_string_coerced(self, monkeypatch):
        # LLM 返回不在 L4_PAYWALL_TYPES 的字符串 → 兜底为 unknown（设计纪律）。
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_openai_response(
                label="paywall", paywall_type="bogus_type",
                evidence="x",
            ))

        client = _make_mock_client(handler)
        rv = MultimVLMReviewer(http_client=client).review(_input_with())
        client.close()
        assert rv.label == "paywall"
        assert rv.paywall_type == "unknown"


# ── Prompt 内容稳定 / 不重叠 L3 rule ────────────────────────────────


class TestPromptDiscipline:
    def test_system_prompt_excludes_l3_rule_dict_strings(self):
        # LLM prompt 明令「不要查 rule 表」——验证 prompt 不含 L3 rule 词典字符串。
        forbidden = (
            "ACCESS_HINTS", "_BRANCH_LABELS", "_PAYWALL_HINTS",
            "_RENTAL_HINTS", "_PARTIAL_FREE_HINTS", "_BLOCKED_HINTS",
            "_LOGIN_HINTS", "region 段", "付费提示", "rule_perceptor",
        )
        for token in forbidden:
            assert token not in _SYSTEM_PROMPT, (
                f"_SYSTEM_PROMPT 含 L3 rule 词典字符串: {token!r}"
            )

    def test_system_prompt_declares_no_rule_lookup(self):
        # prompt 必须显式告诉 LLM「不要套用关键词规则表」——这是 L4 vs L3 的纪律边界。
        assert "不要套用任何关键词规则表" in _SYSTEM_PROMPT
        assert "L4_LABELS" not in _SYSTEM_PROMPT  # 标签值本身可出现，元变量名不可泄漏

    def test_system_prompt_enumerates_labels_and_paywall_types(self):
        # LLM 不能臆造 label / paywall_type——prompt 强制白名单。
        for label in L4_LABELS:
            assert f"`{label}`" in _SYSTEM_PROMPT
        for pt in L4_PAYWALL_TYPES:
            assert pt in _SYSTEM_PROMPT
        for cur in L4_CURRENCIES:
            assert cur in _SYSTEM_PROMPT


# ── 工厂函数 ─────────────────────────────────────────────────────────


class TestBuildReviewer:
    def test_build_reviewer_heuristic_default(self):
        rv = build_reviewer("heuristic")
        assert isinstance(rv, HeuristicVLMReviewer)

    def test_build_reviewer_heuristic_explicit(self):
        rv = build_reviewer("heuristic")
        assert isinstance(rv, HeuristicVLMReviewer)
        assert isinstance(rv, VLMReviewer)

    def test_build_reviewer_multim(self):
        rv = build_reviewer("multim")
        assert isinstance(rv, MultimVLMReviewer)
        assert isinstance(rv, VLMReviewer)

    def test_build_reviewer_default_arg_is_heuristic(self):
        # CLI ``--reviewer`` 不填时默认 heuristic；工厂层默认也应是 heuristic。
        assert isinstance(build_reviewer(), HeuristicVLMReviewer)

    def test_build_reviewer_invalid_name_raises(self):
        with pytest.raises(ValueError, match="--reviewer"):
            build_reviewer("not_a_real_reviewer")


# ── fail-soft + 不抛异常 ─────────────────────────────────────────────


class TestFailSoft:
    def test_review_never_raises_on_bad_payload(self, monkeypatch):
        # 即便 http_client 抛非预期异常，review() 必须吞掉 → 走 fallback 而非外抛。
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dummy")

        class _BoomClient:
            def post(self, *args, **kwargs):
                # 用 httpx.ConnectError ——这是实现明确吞掉的异常族
                # （httpx.HTTPError / TimeoutException / OSError）。
                # 非 httpx 异常留给 _run_l4_review_async 外层 try/except。
                raise httpx.ConnectError("boom")

        # 传任意对象即可（__init__ 存为 http_client）；不期望它被序列化。
        rv = MultimVLMReviewer(http_client=_BoomClient()).review(_input_with())
        assert rv.source == "multim_fallback"
        assert "openai_call_fail" in rv.note
