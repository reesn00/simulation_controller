"""L4 VLM 复核层（2026-10-13 增量二十五）。

阶段 4 七层流水之 L4：基于 L2 探针信号 + DOM 文本 + 页面引文做结构化复核，
对齐设计方案/URL播放黄金集 §4.4 的 6 字段输出 schema（label / paywall_type
/ evidence / price / currency / confidence）。

设计纪律：
1. **协议先行**——``VLMReviewer`` Protocol 把输入/输出契约钉死，
   首实现 ``HeuristicVLMReviewer`` 是纯文本启发式（无视觉模型依赖），
   后续接真视觉 VLM（GPT-4V / Claude vision / 自研多模态）时只换实现，
   ``_run_l4_review`` 的调用点不动。
2. **截图字段预留**——``VLMReviewResult.screenshots: tuple[str, ...] = ()``
   默认空元组；当前没有 ``browser_take_screenshot`` tool（obscura MCP 37 个
   tool 里**没有**截图原语），等真视觉链路到位再填。这条让 P1 存档的字段
   先稳——回填历史归档时不破坏契约。
3. **fail-soft**——``VLMReviewer.review()`` 不抛异常：调用方 ``_run_l4_review``
   把异常折叠为 ``note="vlm_error"`` + confidence=0.0 的 sentinel result，
   L5 仲裁据此降权（不让人工复核队列被 VLM 故障一次性击穿）。
4. **不重叠 L3 规则层**——L4 在结构上是「模拟 VLM 看图」的判定，
   而 L3 是「rule-based 命中关键词」。两者走 L5 仲裁对账，不互相让位。
   HeuristicVLMReviewer 内部**显式**标 ``source="heuristic"`` 区分二者。
5. **页面引文优先**——``page_quotes`` 是 P4a 改造后 LLM 单独回传的页面
   原文字段（见增量十六/十七）；HeuristicVLMReviewer 把 page_quotes 作为
   「最权威页面文本」输入，DOM 正文降级为辅助信号——避免启发式规则
   把 LLM 抽到的 region 文案误判（v.qq.com "当前设备不支持播放"必须
   触发 blocked，不是未知）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable

from trajectory_pipeline.executor.probe.media_probe import MediaSignal


# ── L4 输出枚举（对齐设计方案 §4.4 / §2.1 / §2.2）────────────────────

#: 设计方案 §2.1 一级标签全集。L4 输出 label 必须命中其一；
#: 启发式规则把不命中的归 ``unknown`` 让 L5 仲裁/人工复核兜底。
L4_LABELS: tuple[str, ...] = (
    "playable",
    "ads_then_playable",
    "paywall",
    "login_required",
    "partial_free",
    "blocked",
    "broken",
    "empty",
    "unknown",
)

#: 设计方案 §2.2 paywall_type 全集。
L4_PAYWALL_TYPES: tuple[str, ...] = (
    "none",
    "paid_subscription",
    "rental",
    "purchase",
    "trial",
    "unknown",
)

#: 设计方案 §2.3 currency 白名单。空 = 没识别到 / 不强制约束。
L4_CURRENCIES: tuple[str, ...] = ("CNY", "USD", "EUR", "JPY", "HKD", "GBP")


# ── 关键词词典（启发式判定用）────────────────────────────────────────

#: 付费墙文案（中文）——命中 ≥ 1 段 → label=paywall + paywall_type=paid_subscription。
#: 与增量二十一 ``ACCESS_HINTS`` 同源（region 类命名），归 ``_BUSINESS_QUOTE_HINTS``
#: 同业务词典；这里聚焦"付费提示"子集。
_PAYWALL_HINTS: tuple[str, ...] = (
    "开通VIP", "开通会员", "会员抢先", "VIP会员", "订阅", "付费观看",
    "开通VIP观看", "立即开通", "VIP专享", "付费内容", "试看6分钟",
    "开通超级会员", "连续包月", "开通包月", "开通包年",
)

#: 租赁/单片付费关键词 —— 命中 ≥ 1 段 → paywall_type=rental。
_RENTAL_HINTS: tuple[str, ...] = (
    "单片购买", "单片租赁", "付费租赁", "租赁本片", "购买本片",
    "5元观看", "3元观看", "用券观看", "用券租赁", "单点付费",
)

#: 试看 / 部分免费关键词 —— 命中 ≥ 1 段 → label=partial_free。
#: 与 L1 ``PARTIAL_FREE_MAX_DURATION_S`` 同语义（试看形态 < 60s）。
_PARTIAL_FREE_HINTS: tuple[str, ...] = (
    "试看", "免费试看", "限时免费", "前6分钟", "前5分钟", "前3分钟",
    "试看6分钟", "试看结束", "开通会员看完整版",
)

#: region / blocked 关键词（v.qq.com region 文案核心）—— 命中 ≥ 1 段 → label=blocked。
#: 与增量二十一 ``ACCESS_HINTS`` region 段同源；这里聚焦"地区/版权限制"子集。
_BLOCKED_HINTS: tuple[str, ...] = (
    "当前设备不支持播放", "地区限制", "该视频因版权", "暂不可播放",
    "不支持您所在地区", "视频已下架", "抱歉，该视频", "因版权原因",
    "不支持当前地区", "未在该地区播出", "版权方要求",
)

#: 登录墙关键词 —— 命中 ≥ 1 段 → label=login_required。
#: 不引「登录」二字就触发是粗暴的；要求配合「请先登录 / 登录后」动作语义。
_LOGIN_HINTS: tuple[str, ...] = (
    "请先登录", "登录后观看", "请登录", "登录观看", "未登录",
    "登录账号", "扫码登录", "请扫码", "登录后可观看",
)

#: 价格数字 + 货币正则（人民币 / 美元 / 欧元 / 日元 / 港币 / 英镑）。
#: 优先级：人民币（¥/￥）→ 美元（$）→ 欧元（€）→ 日元（円）→ 港币（HK$）
#: → 英镑（£）。命中首个非空组即返回。
_PRICE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("CNY", re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)")),
    ("USD", re.compile(r"\$\s*(\d+(?:\.\d+)?)")),
    ("EUR", re.compile(r"€\s*(\d+(?:\.\d+)?)")),
    ("JPY", re.compile(r"(\d+(?:\.\d+)?)\s*円")),
    ("HKD", re.compile(r"HK\$\s*(\d+(?:\.\d+)?)")),
    ("GBP", re.compile(r"£\s*(\d+(?:\.\d+)?)")),
)

#: 内置信度档位（L4 heuristic confidence 输出）。
#: 信号越具体，置信度越高：broken / blocked / playable 是「强信号」档；
#: unknown / empty 是「弱信号」档。
_CONFIDENCE_STRONG = 0.85
_CONFIDENCE_MEDIUM = 0.65
_CONFIDENCE_WEAK = 0.30


# ── 数据契约 ────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class VLMReviewInput:
    """L4 复核的输入包（启发式 / 真 VLM 都吃同一形状）。

    字段：
    - signal:         L2 探针出的 :class:`MediaSignal`（6 字段对齐）
    - body_excerpt:   ``document.body.innerText`` 前 1200 字符
                     （启发式走这里做关键词命中；真 VLM 走这里喂 OCR）
    - snapshot_excerpt: snapshot 前 600 字符（含 ``URL:`` / ``Title:``）
                     —— 比 innerText 多 `<style>` 干扰，主做兜底文本源
    - page_quotes:    上游 L0/LLM 抽到的页面原文短语（增量十六/十七 §P4a）
                     —— 最权威文本源，命中即强信号
    - final_url:      L2 落地 URL（用于跨站归一，目前启发式不直接吃）
    """

    signal: MediaSignal
    body_excerpt: str = ""
    snapshot_excerpt: str = ""
    page_quotes: tuple[str, ...] = ()
    final_url: str = ""

    def combined_text(self) -> str:
        """把 3 个文本源按权重拼成单一文本——启发式判定走这里。"""
        # page_quotes 第一（最权威 P4a 字段，启发式优先看这里）
        # body_excerpt 第二（DOM 文本，主要信号源）
        # snapshot_excerpt 第三（最差，含 `<style>` 干扰，只做兜底）
        parts: list[str] = []
        if self.page_quotes:
            parts.append(" ".join(self.page_quotes))
        if self.body_excerpt:
            parts.append(self.body_excerpt)
        if self.snapshot_excerpt:
            parts.append(self.snapshot_excerpt)
        return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class VLMReviewResult:
    """L4 复核结果（对齐设计方案 §4.4 的 6 字段输出）。

    字段：
    - label:         一级标签（见 :data:`L4_LABELS`）
    - paywall_type:  二级标签（见 :data:`L4_PAYWALL_TYPES`）；仅 label=paywall
                    时有效，其余填 ``"none"``
    - evidence:      一句话判据（≤ 120 字）—— 必须含命中的页面原文短语，
                    与 LLM ``evidence`` 字段对齐（增量十六"页面引文在 page_quotes，
                    evidence 只装推断"）
    - price:         数字价格（识别到时填 ``float``，否则 ``None``）
    - currency:      货币代码（见 :data:`L4_CURRENCIES`；未识别时 ``None``）
    - confidence:    0–1 浮点置信度（强信号档 0.85 / 中信号 0.65 / 弱信号 0.30）
    - screenshots:   截图路径列表（默认空 tuple；视觉链路就位后填）
    - note:          给人看 / 给 L5 仲裁看的标记：「body_health」/
                    「no_text」 / 「vlm_error」 / ``""``（默认）
    - source:        评审来源标识：「heuristic」/「gpt4v」/「claude_vision」/
                    「unknown」（给 L5 仲裁用——L4 = L3 rule 时按 source
                    决定加权方式）
    """

    label: str
    paywall_type: str
    evidence: str
    price: float | None = None
    currency: str | None = None
    confidence: float = _CONFIDENCE_WEAK
    screenshots: tuple[str, ...] = ()
    note: str = ""
    source: str = "heuristic"

    def to_dict(self) -> dict[str, object]:
        """JSON 序列化形态（落到 P1 archive 的 ``vlm_review`` 字段）。"""
        return {
            "label": self.label,
            "paywall_type": self.paywall_type,
            "evidence": self.evidence,
            "price": self.price,
            "currency": self.currency,
            "confidence": self.confidence,
            "screenshots": list(self.screenshots),
            "note": self.note,
            "source": self.source,
        }


# ── 协议 ─────────────────────────────────────────────────────────────

@runtime_checkable
class VLMReviewer(Protocol):
    """L4 复核接口（duck-typed runtime_checkable）。

    真视觉模型接进来时只换实现类（``HeuristicVLMReviewer`` →
    ``GPT4VReviewer`` 等），调用点 ``_run_l4_review`` 的 ``review()``
    调用形式不变。

    协议边界：
    - ``review()`` 接受 ``VLMReviewInput``，返回 ``VLMReviewResult``。
    - 实现类**不得**抛异常——所有失败折叠成 ``note="vlm_error"``
      的 sentinel result（``_run_l4_review`` 兜一层 try/except，
      这里不依赖实现自己吞异常）。
    """

    name: str

    def review(self, inp: VLMReviewInput) -> VLMReviewResult:
        ...


# ── 首实现：HeuristicVLMReviewer ─────────────────────────────────────

class HeuristicVLMReviewer:
    """纯文本启发式 VLM 复核（2026-10-13 增量二十五首实现）。

    适用条件（截至 2026-10-13）：
    - **没有真视觉模型** —— Obscura MCP 37 个 tool 都没暴露截图原语。
    - **页面文本 + 媒体信号已够用** —— v.qq.com / bilibili 类 region 文案
      关键词命中准确率高，与 L3 rule-based 仲裁对账足以补足 VLM 缺口。
    - **page_quotes 是最强信号源** —— P4a 改造后 LLM/L0 抽到的页面原文
      直接是页面直接字符串，少了一层推断误差。

    不适用条件（写在这里给 future）：
    - 真正的视觉弹窗（动态 toast / 蒙层 / 角标）—— 文本看不到，靠未来截图。
    - 图标+价格组合按钮 —— 文本看不到，靠未来截图。
    - 词表覆盖不了的新语言 / 新 UI —— 未来接真视觉多模态模型解决。

    决策树（信号优先级从高到低）：
    1. **MediaSignal 强信号** → black_screen / media_error → broken
                                timeupdate_progress 且 current_time < 60s
                                → partial_free
                                timeupdate_progress 且 current_time >= 60s
                                → playable
    2. **关键词命中（强于媒体信号的反向）** → 走 _classify_by_keywords
       注意：MediaSignal 优先于关键词——「真推进 30s」应该被判 partial_free，
       而不是被 page_quotes 里的「试看」文案干扰（两者方向相同，不互相试）
    3. **关键词命中也无** → unknown（弱信号档，让 L5 仲裁/人工兜底）

    注意本类**不判 empty / ads_then_playable** —— 当前启发式信号下
    「empty 页面」与「unknown 无信号」同形；留给真视觉模型看到空白播放器
    时再细分。``unknown`` 的特征让 L5 仲裁直接走人工复核队列。
    """

    name: str = "heuristic-v1"

    def review(self, inp: VLMReviewInput) -> VLMReviewResult:
        sig = inp.signal
        text = inp.combined_text()

        # 步骤 1：MediaSignal 强信号（先看 DOM 客观状态，关键词不反推）
        sig_result = self._from_signal(sig)
        if sig_result is not None:
            return sig_result

        # 步骤 2：关键词命中（无信号 / 没 media 元素时按文案判定）
        kw_result = self._classify_by_keywords(text)
        if kw_result is not None:
            return kw_result

        # 步骤 3：什么都没有 → unknown（弱信号档，L5 仲裁兜底）
        return VLMReviewResult(
            label="unknown",
            paywall_type="none",
            evidence="无媒体信号也无业务关键词命中",
            confidence=_CONFIDENCE_WEAK,
            note="no_signal",
            source="heuristic",
        )

    # ── 内部：MediaSignal 决策 ────────────────────────────────────────

    @staticmethod
    def _from_signal(sig: MediaSignal) -> VLMReviewResult | None:
        """从 ``MediaSignal`` 出 L4 result。返回 ``None`` 表示无强信号。

        信号阈值（与 L1/L2 同款）：
        - ``media_error > 0`` 或 ``black_screen=True`` → broken
        - ``timeupdate_progress=True`` 且 ``current_time_sec`` 在
          ``(0, 60)`` → partial_free（试看形态）
        - ``timeupdate_progress=True`` 且 ``current_time_sec >= 60`` → playable

        边界：
        - ``play_started=True`` 但 ``timeupdate_progress=False``（卡帧 / 节流）
          不算 L4 强信号——让关键词层兜底。
        """
        if sig.media_error is not None and sig.media_error > 0:
            return VLMReviewResult(
                label="broken",
                paywall_type="none",
                evidence=f"媒体错误码 {sig.media_error}（HTMLMediaElement.error）",
                confidence=_CONFIDENCE_STRONG,
                note="signal_broken",
                source="heuristic",
            )
        if sig.black_screen:
            return VLMReviewResult(
                label="broken",
                paywall_type="none",
                evidence="videoWidth=0 且 readyState >= HAVE_METADATA（黑屏）",
                confidence=_CONFIDENCE_STRONG,
                note="signal_broken",
                source="heuristic",
            )
        if (
            sig.timeupdate_progress
            and sig.current_time_sec is not None
            and 0 < sig.current_time_sec < 60.0
        ):
            return VLMReviewResult(
                label="partial_free",
                paywall_type="none",
                evidence=(
                    f"current_time={sig.current_time_sec:.1f}s 真推进（试看 < 60s）"
                ),
                confidence=_CONFIDENCE_STRONG,
                note="signal_partial_free",
                source="heuristic",
            )
        if (
            sig.timeupdate_progress
            and sig.current_time_sec is not None
            and sig.current_time_sec >= 60.0
        ):
            return VLMReviewResult(
                label="playable",
                paywall_type="none",
                evidence=(
                    f"current_time={sig.current_time_sec:.1f}s 真推进（>= 60s 阈值）"
                ),
                confidence=_CONFIDENCE_STRONG,
                note="signal_playable",
                source="heuristic",
            )
        return None

    # ── 内部：关键词决策 ──────────────────────────────────────────────

    @staticmethod
    def _classify_by_keywords(text: str) -> VLMReviewResult | None:
        """无强信号时按页面关键词判定。返回 ``None`` 表示无关键词命中。

        命中优先级（从强到弱）：
        1. ``_BLOCKED_HINTS`` → blocked（region 文案最权威——v.qq.com 实跑 8/8）
        2. ``_LOGIN_HINTS`` → login_required
        3. ``_RENTAL_HINTS`` → paywall + paywall_type=rental
        4. ``_PAYWALL_HINTS`` → paywall + paywall_type=paid_subscription
        5. ``_PARTIAL_FREE_HINTS``（无 MediaSignal 真推进时的纯文案态）
           → partial_free（中信号档——纯文案试看不可强信）

        注意 ``_PARTIAL_FREE_HINTS`` 档最低（_CONFIDENCE_MEDIUM 而不是
        _CONFIDENCE_STRONG）——「试看6分钟」可能只是 banner 文字而不是真
        试看形态，让 L5 仲裁对此权重降一档。
        """
        if not text:
            return None

        # blocked（region / 版权）—— v.qq.com 70017001.1 / 「当前设备不支持」
        blocked_hit = _find_first_hit(text, _BLOCKED_HINTS)
        if blocked_hit:
            return VLMReviewResult(
                label="blocked",
                paywall_type="none",
                evidence=f"命中 region/版权文案：「{blocked_hit}」",
                confidence=_CONFIDENCE_STRONG,
                note="kw_blocked",
                source="heuristic",
            )

        # login_required
        login_hit = _find_first_hit(text, _LOGIN_HINTS)
        if login_hit:
            return VLMReviewResult(
                label="login_required",
                paywall_type="none",
                evidence=f"命中登录墙文案：「{login_hit}」",
                confidence=_CONFIDENCE_STRONG,
                note="kw_login",
                source="heuristic",
            )

        # paywall + rental
        rental_hit = _find_first_hit(text, _RENTAL_HINTS)
        if rental_hit:
            return VLMReviewResult(
                label="paywall",
                paywall_type="rental",
                evidence=f"命中租赁文案：「{rental_hit}」",
                confidence=_CONFIDENCE_STRONG,
                note="kw_rental",
                source="heuristic",
            )

        # paywall + paid_subscription
        paywall_hit = _find_first_hit(text, _PAYWALL_HINTS)
        if paywall_hit:
            price, currency = _extract_price(text)
            return VLMReviewResult(
                label="paywall",
                paywall_type="paid_subscription",
                evidence=f"命中付费墙文案：「{paywall_hit}」",
                confidence=_CONFIDENCE_STRONG,
                price=price,
                currency=currency,
                note="kw_paywall",
                source="heuristic",
            )

        # partial_free（纯文案试看——中信号档）
        partial_hit = _find_first_hit(text, _PARTIAL_FREE_HINTS)
        if partial_hit:
            return VLMReviewResult(
                label="partial_free",
                paywall_type="none",
                evidence=f"命中试看文案：「{partial_hit}」（仅纯文案，须中信号档）",
                confidence=_CONFIDENCE_MEDIUM,
                note="kw_partial_free",
                source="heuristic",
            )

        return None


# ── 内部工具函数 ─────────────────────────────────────────────────────

def _find_first_hit(text: str, hints: Iterable[str]) -> str | None:
    """在 ``text`` 中找首个命中的提示词；返回命中后返回原字符串（不参与命中）。

    返回 ``None`` 表示无命中。

    实现说明：用 ``str.find`` 而不是 regex——「开通VIP」/「VIP订阅」等
    子串重叠简单，regex 反而是过度设计。短文本（< 1500 字符）扫描
    复杂度可忽略。
    """
    if not text:
        return None
    for hint in hints:
        idx = text.find(hint)
        if idx >= 0:
            return hint
    return None


def _extract_price(text: str) -> tuple[float | None, str | None]:
    """从 ``text`` 抽 (price, currency)。未识别 → ``(None, None)``。

    货币优先级与 :data:`_PRICE_PATTERNS` 声明序一致：CNY > USD > EUR
    > JPY > HKD > GBP。命中首个非空即返回。

    注意：v.qq.com 「¥25」/「￥25」两种人民币符号都识别（两个 unicode
    码点：U+00A5 / U+FFE5）。regex ``[¥￥]`` 覆盖。
    """
    for currency, pattern in _PRICE_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                return (float(m.group(1)), currency)
            except (TypeError, ValueError):
                continue
    return (None, None)


__all__ = [
    "HeuristicVLMReviewer",
    "L4_CURRENCIES",
    "L4_LABELS",
    "L4_PAYWALL_TYPES",
    "VLMReviewer",
    "VLMReviewInput",
    "VLMReviewResult",
]
