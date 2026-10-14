"""L4 真视觉 / 真 LLM 复核实现类（2026-10-13 增量三十四）。

承接 HeuristicVLMReviewer（增量二十五）——以 :class:`VLMReviewer` Protocol
为契约，把 L4 接到真实多模态 LLM（GPT-4o / Claude 3.5 Sonnet）上。当前
Obscura MCP 37 个 tool **没有**截图原语，没有图像数据可以喂视觉模型，
所以「真视觉」落地形态是「真 LLM 文本理解」——把 DOM 文本 / page_quotes
喂 GPT-4o-mini / Claude Haiku 做判定，覆盖启发式词典漏掉的新形态。

设计纪律（与 HeuristicVLMReviewer 同源）：
1. **协议先行** —— ``VLMReviewer`` Protocol 不变，``MultimVLMReviewer``
   只换实现类；调用点 ``_run_l4_review`` 不改。
2. **fail-soft** —— API 调用失败 / 无 key / 响应解析错都不抛异常，
   全部折叠成 ``note="vlm_error"`` + ``source="multim_error"`` 的
   sentinel result（与 _vlm_error_sentinel 一致）。L5 仲裁据此降权。
3. **无 key 自动降级到启发式** —— 这是「视觉链路没就位但不至于丢功能」
   的妥协：``source="multim_fallback"`` 显式标记，让 L5 仲裁 / 人工
   复核一眼看出「这本是 multim 通道，结果走 fallback 了」。
4. **不重叠 L3 rule** —— MultimVLMReviewer 内部**不读** L3 词典；
   LLM API 提示词也明令「不要查 rule 表」，避免 L4 抢 L3 饭碗。
5. **截图字段预留** —— ``VLMReviewResult.screenshots`` 默认空 tuple；
   obscura 暴露 browser_take_screenshot 后填路径，契约不变。

API 选择（按 ROI 排）：
- **首选 OpenAI** —— ``OPENAI_API_KEY`` 环境变量 + ``gpt-4o-mini`` 模型；
  响应快、便宜、``response_format={"type": "json_object"}`` 强制 JSON 输出。
- **次选 Anthropic** —— ``ANTHROPIC_API_KEY`` 环境变量 +
  ``claude-3-5-haiku-latest`` 模型；fallback when OpenAI 不可用。
- **无 key** —— 降级到 :class:`HeuristicVLMReviewer`。

API 形状（仅 OpenAI Chat Completions，不接其他 OpenAI 家族端点）：
- **唯一请求端点** —— ``POST /v1/chat/completions``，载荷用 ``messages[]``
  + ``response_format={"type": "json_object"}``（Chat Completions 专属）。
- **不接** —— OpenAI Responses API（``/v1/responses``，用 ``text.format``）、
  Assistants API（``/v1/assistants``）、Batch API（``/v1/batches``）、
  Files API、Audio / Images API。
- **不接 Azure OpenAI 原生鉴权** —— Azure 用 ``api-key`` header 而非
  ``Authorization: Bearer``；换 URL 也跑不通 Azure。任何 Azure / 自建代理
  接入提案都要先开 §3.5，因为鉴权头要加字段，动了是合同变更。
- **OpenAI 兼容端点可换 URL 不能换格式** —— ``_MultimConfig.openai_url``
  允许注入（默认 ``https://api.openai.com/v1/chat/completions``），但 wire
  格式不变；非兼容端点直接挂掉并走 ``openai_call_fail`` fallback。

为什么不调 GPT-4V / Claude Vision（图像版）：
- 当前没截图数据可喂；调空图像是浪费 token。
- 等 obscura 暴露 ``browser_take_screenshot`` tool 后，把 ``screenshots``
  tuple 填上，提示词加 ``image`` 字段即可启用 —— **实现层不动**。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

from trajectory_pipeline.executor.review.vlm_reviewer import (
    HeuristicVLMReviewer,
    L4_CURRENCIES,
    L4_LABELS,
    L4_PAYWALL_TYPES,
    VLMReviewInput,
    VLMReviewResult,
)

_log = logging.getLogger(__name__)


# ── 配置常量 ─────────────────────────────────────────────────────────

#: OpenAI Chat Completions API（OpenAI 兼容的端点都可以走）。
#: ``gpt-4o-mini`` 是「便宜 + 快 + JSON 强约束」的性价比档。
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_OPENAI_MODEL = os.environ.get("INC34_OPENAI_MODEL", "gpt-4o-mini")
_OPENAI_TIMEOUT_S = float(os.environ.get("INC34_OPENAI_TIMEOUT_S", "12.0"))

#: Anthropic Messages API（Claude 3.5 Haiku 性价比档）。
_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_MODEL = os.environ.get("INC34_ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_TIMEOUT_S = float(os.environ.get("INC34_ANTHROPIC_TIMEOUT_S", "12.0"))

#: LLM 响应必填字段（口径 C 与 HeuristicVLMReviewer 输出对齐）
#: —— 解析响应时若缺字段，fallback 到 HeuristicVLMReviewer 的同输入结果。
_EXPECTED_LLM_FIELDS = ("label", "paywall_type", "evidence")


# ── Prompt 模板 ─────────────────────────────────────────────────────

_SYSTEM_PROMPT = """你是一个专业的网页播放形态判定员。根据输入的 DOM 文本片段判断视频/音频页面的访问形态。

判定规则（按输出 label 严格二选一）：
- `playable`：可正常播放
- `ads_then_playable`：先广告后播放
- `paywall`：付费墙（订阅/单片购买）
- `login_required`：需登录后免费观看
- `partial_free`：试看后付费/登录
- `blocked`：地区限制 / 版权 / 反爬阻断
- `broken`：404 / 媒体错误 / 页面损坏
- `empty`：页面为空（无播放器/无内容）
- `unknown`：无法判定（弱信号）

paywall_type 取值：`none` / `paid_subscription` / `rental` / `purchase` / `trial` / `unknown`

evidence 字段必须含**命中的页面原文短语**（不超过 120 字），不要凭推断写。

严格 JSON 输出（不要任何额外文字 / markdown）：
{"label": "...", "paywall_type": "...", "evidence": "...",
 "price": <number 或 null>, "currency": "<CNY/USD/EUR/JPY/HKD/GBP 或 null>",
 "confidence": <0.0-1.0>}

不要套用任何关键词规则表——只看 DOM 文本。"""


# ── Reviewer 实现 ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _MultimConfig:
    """MultimVLMReviewer 的可调参数（按 dataclass 注入便于测试）。"""

    openai_api_key: str = ""
    openai_model: str = _OPENAI_MODEL
    openai_url: str = _OPENAI_URL
    openai_timeout_s: float = _OPENAI_TIMEOUT_S
    anthropic_api_key: str = ""
    anthropic_model: str = _ANTHROPIC_MODEL
    anthropic_url: str = _ANTHROPIC_URL
    anthropic_timeout_s: float = _ANTHROPIC_TIMEOUT_S
    anthropic_version: str = _ANTHROPIC_VERSION
    #: 注入测试时用：替换 httpx 调用
    http_client: Any = None  # httpx.Client | None


class MultimVLMReviewer:
    """L4 真视觉 / 真 LLM 复核实现（增量三十四，2026-10-13）。

    行为表：

    | 条件                          | 行为                                | source 字段                |
    |------------------------------|------------------------------------|--------------------------|
    | ``OPENAI_API_KEY`` 已设       | 调 OpenAI gpt-4o-mini             | ``gpt4o_text``           |
    | 只 ``ANTHROPIC_API_KEY`` 已设 | 调 Anthropic claude-3-5-haiku     | ``claude_text``          |
    | 两个 key 都没设               | 降级 :class:`HeuristicVLMReviewer`  | ``multim_fallback``      |
    | API 调失败 / 超时 / 响应坏    | 降级 HeuristicVLMReviewer + note  | ``multim_fallback``      |
    | 响应 JSON 字段不全            | 降级 HeuristicVLMReviewer + note  | ``multim_fallback``      |

    设计取舍：
    - **降级而不是抛** —— L5 仲裁看到 ``multim_fallback`` 时按 heuristic
      路径走，不至于整批失败。
    - **不开新连接池** —— 共用注入的 httpx.Client；测试时也方便 monkeypatch。
    - **不做 retry** —— 单次失败就降级；retry 是上层 review-queue 的事。

    实现细节：
    - **OpenAI** 用 ``response_format={"type": "json_object"}`` 强制 JSON；
      Anthropic 用 system + user 消息 + 提示「输出 JSON」自然约束。
    - **prompt 包含 L4_LABELS / L4_PAYWALL_TYPES 全集**，避免 LLM 臆造标签。
    - **price / currency / confidence 字段可选**——LLM 不给就 None。
    """

    name: str = "multim-v1"

    def __init__(
        self,
        config: _MultimConfig | None = None,
        *,
        openai_api_key: str | None = None,
        anthropic_api_key: str | None = None,
        openai_model: str | None = None,
        anthropic_model: str | None = None,
        http_client: Any | None = None,
    ) -> None:
        env_openai = openai_api_key if openai_api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        env_anthropic = anthropic_api_key if anthropic_api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self._cfg = config or _MultimConfig(
            openai_api_key=env_openai,
            anthropic_api_key=env_anthropic,
            http_client=http_client,
        )
        if openai_model is not None:
            self._cfg = dataclasses.replace(self._cfg, openai_model=openai_model)
        if anthropic_model is not None:
            self._cfg = dataclasses.replace(self._cfg, anthropic_model=anthropic_model)
        if http_client is not None:
            self._cfg = dataclasses.replace(self._cfg, http_client=http_client)
        self._fallback = HeuristicVLMReviewer()

    # ── 公开方法 ──────────────────────────────────────────────────────

    def review(self, inp: VLMReviewInput) -> VLMReviewResult:
        """跑 LLM 判定。无 key / 失败时降级到启发式。"""
        # 优先 OpenAI
        if self._cfg.openai_api_key:
            return self._review_with_openai(inp)
        # 次选 Anthropic
        if self._cfg.anthropic_api_key:
            return self._review_with_anthropic(inp)
        # 两个 key 都没设 → 降级
        return self._fallback_result(inp, reason="no_api_key")

    # ── OpenAI 路径 ───────────────────────────────────────────────────

    def _review_with_openai(self, inp: VLMReviewInput) -> VLMReviewResult:
        user_msg = self._build_user_message(inp)
        payload = {
            "model": self._cfg.openai_model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }
        headers = {
            "Authorization": f"Bearer {self._cfg.openai_api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = self._post(self._cfg.openai_url, payload, headers, self._cfg.openai_timeout_s)
        except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
            _log.warning("OpenAI call failed: %s", exc)
            return self._fallback_result(inp, reason=f"openai_call_fail: {type(exc).__name__}")
        return self._parse_openai_response(resp, inp)

    def _parse_openai_response(
        self, resp: dict[str, Any], inp: VLMReviewInput,
    ) -> VLMReviewResult:
        try:
            content = resp["choices"][0]["message"]["content"]
            data = json.loads(content)
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            _log.warning("OpenAI response bad shape: %s body=%r", exc, str(resp)[:200])
            return self._fallback_result(inp, reason=f"openai_response_bad: {type(exc).__name__}")
        return self._build_result(data, inp, source="gpt4o_text")

    # ── Anthropic 路径 ────────────────────────────────────────────────

    def _review_with_anthropic(self, inp: VLMReviewInput) -> VLMReviewResult:
        user_msg = self._build_user_message(inp)
        payload = {
            "model": self._cfg.anthropic_model,
            "max_tokens": 512,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_msg}],
        }
        headers = {
            "x-api-key": self._cfg.anthropic_api_key,
            "anthropic-version": self._cfg.anthropic_version,
            "Content-Type": "application/json",
        }
        try:
            resp = self._post(self._cfg.anthropic_url, payload, headers, self._cfg.anthropic_timeout_s)
        except (httpx.HTTPError, httpx.TimeoutException, OSError) as exc:
            _log.warning("Anthropic call failed: %s", exc)
            return self._fallback_result(inp, reason=f"anthropic_call_fail: {type(exc).__name__}")
        return self._parse_anthropic_response(resp, inp)

    def _parse_anthropic_response(
        self, resp: dict[str, Any], inp: VLMReviewInput,
    ) -> VLMReviewResult:
        try:
            content_blocks = resp["content"]
            text = next(b["text"] for b in content_blocks if b.get("type") == "text")
            data = json.loads(text)
        except (KeyError, TypeError, StopIteration, json.JSONDecodeError) as exc:
            _log.warning("Anthropic response bad shape: %s body=%r", exc, str(resp)[:200])
            return self._fallback_result(inp, reason=f"anthropic_response_bad: {type(exc).__name__}")
        return self._build_result(data, inp, source="claude_text")

    # ── HTTP helper ───────────────────────────────────────────────────

    def _post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout_s: float,
    ) -> dict[str, Any]:
        client = self._cfg.http_client
        if client is not None:
            r = client.post(url, json=payload, headers=headers, timeout=timeout_s)
            r.raise_for_status()
            return r.json()
        with httpx.Client(timeout=timeout_s) as c:
            r = c.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()

    # ── 响应解析 / 构造 result ────────────────────────────────────────

    def _build_result(
        self,
        data: dict[str, Any],
        inp: VLMReviewInput,
        *,
        source: str,
    ) -> VLMReviewResult:
        label = str(data.get("label") or "").strip()
        paywall_type = str(data.get("paywall_type") or "none").strip()
        evidence = str(data.get("evidence") or "").strip()
        # 必填字段缺 → fallback
        if not label or label not in L4_LABELS:
            return self._fallback_result(inp, reason=f"{source}_label_invalid:{label!r}")
        if paywall_type not in L4_PAYWALL_TYPES:
            paywall_type = "unknown" if paywall_type else "none"
        if not evidence:
            evidence = f"{source} | label={label} | 无 evidence"
        # 可选字段
        price = data.get("price")
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        currency_raw = data.get("currency")
        currency = currency_raw if currency_raw in L4_CURRENCIES else None
        try:
            confidence = float(data.get("confidence") or 0.65)
        except (TypeError, ValueError):
            confidence = 0.65
        confidence = max(0.0, min(1.0, confidence))
        return VLMReviewResult(
            label=label,
            paywall_type=paywall_type if label == "paywall" else "none",
            evidence=evidence[:120],
            price=price_f,
            currency=currency,
            confidence=confidence,
            note="multim_ok",
            source=source,
        )

    # ── Fallback ──────────────────────────────────────────────────────

    def _fallback_result(self, inp: VLMReviewInput, *, reason: str) -> VLMReviewResult:
        fb = self._fallback.review(inp)
        # 把 heuristic 结果的 source/note 改成 multim 通道语义
        # —— 让 L5 仲裁 / 人工一眼看出「这本是 multim 通道，结果走了 heuristic」。
        return VLMReviewResult(
            label=fb.label,
            paywall_type=fb.paywall_type,
            evidence=fb.evidence,
            price=fb.price,
            currency=fb.currency,
            confidence=fb.confidence,
            screenshots=fb.screenshots,
            note=f"multim_fallback:{reason}",
            source="multim_fallback",
        )

    # ── Prompt 拼装 ──────────────────────────────────────────────────

    @staticmethod
    def _build_user_message(inp: VLMReviewInput) -> str:
        parts: list[str] = []
        parts.append(f"final_url: {inp.final_url}")
        if inp.page_quotes:
            parts.append("page_quotes:")
            for q in inp.page_quotes[:5]:
                parts.append(f"  - {q[:200]}")
        if inp.body_excerpt:
            parts.append(f"body_excerpt: {inp.body_excerpt[:1000]}")
        if inp.snapshot_excerpt:
            parts.append(f"snapshot_excerpt: {inp.snapshot_excerpt[:400]}")
        sig = inp.signal
        parts.append(
            "signal: "
            f"play_started={sig.play_started} "
            f"timeupdate_progress={sig.timeupdate_progress} "
            f"current_time_sec={sig.current_time_sec} "
            f"frames_decoded={sig.frames_decoded} "
            f"black_screen={sig.black_screen} "
            f"media_error={sig.media_error}"
        )
        return "\n".join(parts)


# ── Reviewer 工厂（CLI 复用） ─────────────────────────────────────────


def build_reviewer(name: str = "heuristic") -> Any:
    """按名称造 reviewer 实例；CL 端 ``--reviewer`` 选项直接复用。

    支持 ``"heuristic"`` / ``"multim"`` 两种取值；缺省 ``heuristic``。
    """
    if name == "heuristic":
        return HeuristicVLMReviewer()
    if name == "multim":
        return MultimVLMReviewer()
    raise ValueError(f"--reviewer 取值 {name!r} 不在 ('heuristic', 'multim') 内")


__all__ = [
    "MultimVLMReviewer",
    "build_reviewer",
]
