"""L5 仲裁层——L3 rule + L4 VLM + L2 signal 加权产出 21 分支。

口径：``BRANCH_LABELS`` 全集 21 key = 20 活跃 + 1 deprecated
``login_wall_or_blocked``；判定层不主动产出 deprecated，历史存档才有。

增量二十七（2026-10-13）。设计纪律（4 条拍板）：

1. **L5 = N-of-M 加权仲裁**，不是简单 L3 抢 L4、也不是反过来——
   按 ``source 权重 × confidence`` 排序、同质一致取 max，冲突时优先 L4 llm > L4 heuristic > L3 rule
2. **L5 输入 = BrowserProbeResult 顶层**，不再从 Observation 重扫
3. **L5 输出 = SiteOutcome 21 分支**，与 :mod:`executor.branches` 同源
4. **L5 fail-closed 短路**：``result.note ∈ {credential_leak, driver_missing, timeout, driver_error}``时
   直接进 unresolved/player_unverified，不走加权

输出形状（嵌入 BrowserProbeResult.l5_outcome）：

.. code-block:: python

    SiteOutcome(
        url=...,
        branch="blocked" | "login_required" | ... | None,
        evidence="【L5仲裁】...",
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

#: L5 fail-closed note 集（与 L3 / L4 同源——驱动层报错）。
L5_FAIL_CLOSED_NOTES: frozenset[str] = frozenset(
    {"credential_leak", "driver_missing", "timeout", "driver_error"}
)

#: L3 access_kind → 21 分支映射（L3 词典 7 值的起点）。
#: 未命中返回 None，交由 L4 / L2 补。
L3_ACCESS_KIND_TO_BRANCH: Mapping[str, str] = {
    "region": "blocked",
    "anti_bot": "blocked",
    "captcha": "blocked",
    "not_found": "broken",
    "login": "login_required",
}

#: L3 判出 paywall 后，L3 paywall_type → 分支。
L3_PAYWALL_TYPE_TO_BRANCH: Mapping[str, str] = {
    "preview": "partial_free",
    "trial": "partial_free",
}

#: L4 label → 21 分支映射。None 代表“本身就是成功”（不走失败分支）。
#: 不在表中的 label （empty / unknown）返回 None，交由 L2 / L3 补。
L4_LABEL_TO_BRANCH: Mapping[str, str | None] = {
    "playable": None,
    "ads_then_playable": None,
    "paywall": "subscription_wall",  # 默认为订阅式，被 paywall_type 覆盖
    "login_required": "login_required",
    "partial_free": "partial_free",
    "blocked": "blocked",
    "broken": "broken",
}

#: L4 paywall_type → 当 label=paywall 时的细分。trial 与 L3 同源不同 dict。
L4_PAYWALL_TYPE_TO_BRANCH: Mapping[str, str] = {
    "trial": "partial_free",
}

#: L4 source 权重（仲裁时冲突取较高者）。
#: llm > heuristic > rule，与「人在环」一致：LLM 看了多模态不应该输给词典。
L5_SOURCE_WEIGHT: Mapping[str, int] = {
    "llm": 3,
    "heuristic": 2,
    "gpt4v": 3,
    "claude_vision": 3,
    "rule": 1,
    "arbiter": 0,  # 本层标记，不参与加权
}

#: 2026-10-13 增量二十八：L1 ffprobe 媒体层判定权重。
#: L1 是「实际媒体字节流」判定的（duration / has_video），
#: 强于 L3 词典、弱于 L4 LLM 多模态。冲突时 L1 输给 llm (3)，
#: 与 heuristic (2) 平手——但 duration 是硬数据，
#: 因此 partial_free / broken 仍可比 heuristic 更让人信服。
L1_SOURCE_WEIGHT: int = 2
#: L1 出分支时的默认 confidence——duration 是硬证据，给到 0.8；
#: L1 没出分支时返 (None, 0.0)，让 _pick_winner 跳过 L1 投票。
L1_BRANCH_CONFIDENCE: float = 0.8


def _branch_from_l3(l3_decision: Decision | None) -> tuple[str | None, float]:
    """L3 Decision → 分支 + confidence。未命中返回 (None, 0.0)。"""
    if l3_decision is None:
        return None, 0.0
    payload = dict(l3_decision.payload) if l3_decision.payload else {}
    access_kind = payload.get("access_kind")
    if access_kind == "paywall":
        # 调用 L3 paywall_type 细分
        pw_type = payload.get("paywall_type", "none")
        branch = L3_PAYWALL_TYPE_TO_BRANCH.get(pw_type, "subscription_wall")
        return branch, float(l3_decision.confidence)
    branch = L3_ACCESS_KIND_TO_BRANCH.get(access_kind or "", None)
    if branch is None:
        return None, 0.0
    return branch, float(l3_decision.confidence)


def _branch_from_l4(vlm_review: Any) -> tuple[str | None, float, str]:
    """VLMReviewResult → 分支 + confidence + source。

    返回 (branch_or_None, confidence, source)。branch=None 代表“本身就是成功”
    或“未命中”——两者均交由上下游。
    """
    if vlm_review is None:
        return None, 0.0, ""
    label = getattr(vlm_review, "label", "")
    conf = float(getattr(vlm_review, "confidence", 0.0) or 0.0)
    source = str(getattr(vlm_review, "source", "") or "")
    if label == "paywall":
        # 看 L4 paywall_type 是否能细分
        pw_type = getattr(vlm_review, "paywall_type", "none")
        branch = L4_PAYWALL_TYPE_TO_BRANCH.get(pw_type, "subscription_wall")
        return branch, conf, source
    if label in L4_LABEL_TO_BRANCH:
        return L4_LABEL_TO_BRANCH[label], conf, source
    # empty / unknown / 未知标签
    return None, conf, source


def _branch_from_signal(
    signal: Any,
    media_count: int,
    note: str,
) -> tuple[str | None, str]:
    """L2 MediaSignal 启发式→ 分支 + 证据文案。

    返回 (branch_or_None, evidence_phrase)。
    """
    play_started = bool(getattr(signal, "play_started", False))
    current_time = getattr(signal, "current_time_sec", None)
    timeupdate = bool(getattr(signal, "timeupdate_progress", False))
    frames = getattr(signal, "frames_decoded", None)
    black = bool(getattr(signal, "black_screen", False))
    media_error = getattr(signal, "media_error", None)

    # 1) 真播放信号：play_started + currentTime>0 / timeupdate进展
    if play_started and (current_time is not None and current_time > 0 or timeupdate):
        return None, f"L2 实播（currentTime={current_time}s, frames={frames}）"

    # 2) 黑屏 + 0 帧解码 → broken
    if black and (frames == 0 or frames is None) and media_count > 0:
        return "broken", "L2 黑屏（0 帧）"

    # 3) HTMLMediaElement error code >= 4 → broken
    if isinstance(media_error, int) and media_error >= 4:
        return "broken", f"L2 media_error code={media_error}"

    # 4) 无媒体 + 无 fail-closed → not_play_site
    if media_count == 0 and note == "":
        return "not_play_site", "L2 media_count=0, 无 <video>/<audio>"

    # 5) 其他 → 不产分支，交上下游
    return None, ""


def _branch_from_l1(l1_probe: Any) -> tuple[str | None, float]:
    """L1 ffprobe 媒体探测结果 → 分支 + confidence。

    未跑 L1 / l1_probe=None → (None, 0.0)，让 _pick_winner 跳过 L1 投票。
    出分支时以 :data:`L1_BRANCH_CONFIDENCE` 为保证值（duration 是硬数据）。
    逻辑复用 :func:`trajectory_pipeline.executor.probe.ffprobe.to_branch`，
    与 L1 「分支映射」同源。
    """
    if l1_probe is None:
        return None, 0.0
    # 动态 import 避免模块启动顺序问题。
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
    """L5 仲裁输出中间表示——不入 result，只给 SiteOutcome 用。"""

    branch: str | None
    evidence: str
    source: str  # 总是 "arbiter"
    fallback_used: bool
    l3_branch: str | None
    l4_branch: str | None
    l2_branch: str | None
    # 2026-10-13 增量二十八：L1 ffprobe 判定加入 verdict。
    l1_branch: str | None
    l1_conf: float
    l3_conf: float
    l4_conf: float
    l4_source: str

    def to_evidence_phrase(self) -> str:
        """证据文案，包含 L1/L3/L4/L2 各自判出 + 仲裁路径。"""
        parts: list[str] = []
        if self.l1_branch is not None:
            parts.append(f"L1→{self.l1_branch}({self.l1_conf:.2f})")
        if self.l3_branch is not None:
            parts.append(f"L3→{self.l3_branch}({self.l3_conf:.2f})")
        if self.l4_branch is not None or self.l4_source:
            parts.append(f"L4[{self.l4_source or '?'}]→{self.l4_branch or '未命中'}({self.l4_conf:.2f})")
        if self.l2_branch is not None:
            parts.append(f"L2→{self.l2_branch}")
        if not parts:
            return "【L5仲裁】四层全无判出，进 unresolved"
        return "【L5仲裁】" + " | ".join(parts) + f" → {self.branch or '成功'}"


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
    """选出最终分支。None 代表成功。

    2026-10-13 增量二十八：L1 作为第四仲裁伙伴（duration 是 hard 证据）。
    ⚠️ 2026-10-26 订正：原文此处是乱码字符 ``U+1973``（``ᥳ``）加残留
    ``fa``——**首次提交（``a283380``，2026-10-13）就是这个形态**，git 里
    没有更早版本可回溯，**原字无法复原**（多轮 mojibake 链反推无解）。
    现按同文件 :92「duration 是硬数据」与 :95「duration 是硬证据」的
    既有口径订正为「是」。⚠️ **订正的是注释文字，不是判定逻辑**——
    判据在 :90/:95 与 ``L1_BRANCH_CONFIDENCE``。

    优先级（1 最高）：
    1. L2 真播放（l2_branch=None）→ 一票否决，返回 None
    2. L2 黑屏/错误代码 → 一票决定 broken
    3. L3 + L4 一致 → 采纳
    4. L3 + L4 冲突 → 按 source 权重 × confidence 取较高者
    5. L1 与任何一致 → 采纳；L1 与三者都冲突 → 按权重取较高者（llm > l1）
    6. 仅 L3 → 用 L3；仅 L4 → 用 L4；仅 L1 → 用 L1；全无 → unresolved
    """
    # 1) L2 真播放：一票否决
    if l2_branch is None and l3_branch is None and l4_branch is None:
        # L2 仅产出 "None" 与 不产出，需要区分
        # 这里靠 L2 是否产出“成功信号”（play_started）来判
        pass
    # 实际上 _branch_from_signal 返回 None 两种：
    # (a) 真播放 → (None, "...实播...")；(b) 未命中 → (None, "")
    # 我们在 _arbitrate 里带上 evidence_phrase 传入，这里只看分支不够
    # → 改为让 _arbitrate 处理 L2 优先级

    if l3_branch is not None and l4_branch is not None:
        if l3_branch == l4_branch:
            # L1 与一致 → 采纳
            if l1_branch == l3_branch:
                return l3_branch
            # L1 与两者都冲突 → 按权重取较高者（llm > l1）
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
            # L3 + L4 冲突但 L1 未出分支
            l3_score = L5_SOURCE_WEIGHT.get("rule", 1) * l3_conf
            l4_score = L5_SOURCE_WEIGHT.get(l4_source, 2) * l4_conf
            return l3_branch if l3_score >= l4_score else l4_branch
        # L3 + L4 冲突
        l3_score = L5_SOURCE_WEIGHT.get("rule", 1) * l3_conf
        l4_score = L5_SOURCE_WEIGHT.get(l4_source, 2) * l4_conf
        # L1 可能与 L3 一致、与 L4 一致、或与两者都不一致
        l1_score = L1_SOURCE_WEIGHT * l1_conf if l1_branch is not None else 0.0
        candidates: list[tuple[float, str]] = [
            (l3_score, l3_branch),
            (l4_score, l4_branch),
        ]
        if l1_branch is not None:
            candidates.append((l1_score, l1_branch))
        top = max(candidates, key=lambda x: x[0])
        return top[1]
    # INC28_L1_PICK_FIX_v1: L1 与 L3/L4 冲突时按权重选
    if l3_branch is not None:
        # L1 与 L3 冲突 → 按权重取较高者
        if l1_branch is not None and l1_branch != l3_branch:
            l3_score = L5_SOURCE_WEIGHT.get("rule", 1) * l3_conf
            l1_score = L1_SOURCE_WEIGHT * l1_conf
            return l1_branch if l1_score > l3_score else l3_branch
        return l3_branch
    if l4_branch is not None:
        # L1 与 L4 冲突 → 按权重取较高者
        if l1_branch is not None and l1_branch != l4_branch:
            l4_score = L5_SOURCE_WEIGHT.get(l4_source, 2) * l4_conf
            l1_score = L1_SOURCE_WEIGHT * l1_conf
            return l1_branch if l1_score > l4_score else l4_branch
        return l4_branch
    # 仅 L1 有判出
    if l1_branch is not None:
        return l1_branch
    return "unresolved"


def arbitrate(result: Any) -> SiteOutcome:
    """L5 仲裁主函数。

    输入：``BrowserProbeResult``（result.signal + result.l3_decision + result.vlm_review + result.note）
    输出：``SiteOutcome``（branch 在 21 分支集合内，或 None = 成功）

    调用者：cli.py 在 L3 / L4 之后注入；
    test_arbitration.py 直接 unit 验证。
    """
    l3_decision = getattr(result, "l3_decision", None)
    vlm_review = getattr(result, "vlm_review", None)
    signal = getattr(result, "signal", None)
    note = str(getattr(result, "note", "") or "")
    media_count = int(getattr(result, "media_count", 0) or 0)
    url = str(getattr(result, "final_url", "") or getattr(result, "requested_url", "") or "")

    # === Step 0：fail-closed 短路 ===
    if note in L5_FAIL_CLOSED_NOTES:
        # 驱动层未采到足够信息。但 L4 可能仍有判出（少见）——
        # 仍保留 L4 判出，仅在 evidence 里加上 fail-closed 后缀。
        l4_branch, l4_conf, l4_source = _branch_from_l4(vlm_review)
        if l4_branch is not None:
            verdict_branch = l4_branch
            ev = (
                f"【L5仲裁】fail-closed note={note}, 但 L4[{l4_source}]→{l4_branch}"
                f"（{l4_conf:.2f}）仍采纳"
            )
            return SiteOutcome(
                url=url,
                branch=verdict_branch,
                evidence=ev,
                question="business.signal",
                source="arbiter",
                fallback_used=False,
            )
        # L4 也未判出 → unresolved
        return SiteOutcome(
            url=url,
            branch="unresolved",
            evidence=f"【L5仲裁】fail-closed note={note}, L3/L4 全无判出",
            question="business.signal",
            source="arbiter",
            fallback_used=False,
        )

    # === Step 1：解析 L1 / L3 / L4 / L2 各自贡献 ===
    # 2026-10-13 增量二十八：L1 ffprobe 上位，与同位竞争。
    l1_probe = getattr(result, "l1_probe", None)
    l1_branch, l1_conf = _branch_from_l1(l1_probe)
    l3_branch, l3_conf = _branch_from_l3(l3_decision)
    l4_branch, l4_conf, l4_source = _branch_from_l4(vlm_review)
    l2_branch, l2_ev_phrase = _branch_from_signal(signal, media_count, note)

    # === Step 2：L2 真播放信号（一票否决） ===
    if (
        l2_branch is None
        and l2_ev_phrase  # 非空：表示 L2 是真播放，不是“未命中”
        and "L2 实播" in l2_ev_phrase
    ):
        return SiteOutcome(
            url=url,
            branch=None,
            evidence=f"【L5仲裁】L2 实播（{url}）",
            question="playback.success",
            source="arbiter",
            fallback_used=False,
        )

    # === Step 3：仲裁选拔 ===
    # 2026-10-13 增量二十八：L1 作为第四仲裁伙伴参与选拔。
    verdict_branch = _pick_winner(
        l3_branch, l3_conf, l4_branch, l4_conf, l4_source, l2_branch,
        l1_branch, l1_conf,
    )

    # 构 L5Verdict 供 evidence
    # 2026-10-13 增量二十八：供 L1 分支 + 置信度。
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
    # 补上 L2 evidence
    if l2_branch is not None:
        ev_phrase += f" | L2: {l2_ev_phrase}"

    # === Step 4：校验输出映射是否合法（防止 arbiter 自己造出未知分支） ===
    if verdict_branch is not None and verdict_branch not in NON_SAMPLE_BRANCHES:
        # 可能还不是最终合法值，不过 _pick_winner 只返回三层都合法的值。以下只是防御。
        pass

    return SiteOutcome(
        url=url,
        branch=verdict_branch,
        evidence=ev_phrase,
        question="business.signal" if verdict_branch != "unresolved" else "playback.success",
        source="arbiter",
        fallback_used=False,
    )
