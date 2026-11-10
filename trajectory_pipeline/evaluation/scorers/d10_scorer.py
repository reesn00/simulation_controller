"""LLM 裁判层评分器 —— **D-10「结论正确性」**（样本级 ★）。

## 为什么不是「一个裁判判五档」

rubric v1.3 的 D-10 是**分判两因子**：

=====================================  =====================  ==============
因子                                   判据来源               产出
=====================================  =====================  ==============
**A · 一致性**（结论 vs 人工预核验）    **代码断言**（语义等价类） 三态
**B · 依据陈述**（依据准不准 / 成因对不对） **LLM 裁判**（本模块）    因子取值
=====================================  =====================  ==============

档位由 (A, B) **查表**得到（:data:`~...rubric_version.D10_EVIDENCE_FACTORS`
/ :data:`~...rubric_version.D10_CAUSE_FACTORS`），不另设旁路。

**这不是投票、不是集成**：每个因子**只有一个**判据来源，两因子之间
**不互相表决、不取多数**（G-7 同款纪律）。若让一个 LLM 同时判「是否一致」
与「依据准不准」，它会在两问之间挑好答的答；把「是否一致」交给已经定线、
已产读数的语义等价类口径 C，D-10 就有**一个**结论，不会有两个互相打架的。

⚠️ **因子 A 不在本模块**：它由 :mod:`trajectory_pipeline.evaluation.golden.alignment`
的 ``hard_agreement`` 算出（三态），由**调用方**传进来。本模块**不 import
golden 包**——依赖方向是 ``golden → scorers``，反向会成环（见架构总览）。

## fail-closed（v1.3 新增的红线）

**判不出 ≠ 判错**。以下情形一律落 ``undetermined``（``score=None``），
**绝不落 1 档**：

- 未提供 LLM client；
- 因子 A 判「未判定」（口径 C 三态里的第三态）；
- 观察存档缺失或不足以支撑判断；
- 依据陈述文本缺失；
- LLM 调不通 / JSON 抠不出 / 取值不在契约集合内；
- 裁判自报「无法判定」。

理由写在 rubric v1.3 的 D-10 卡边界里：**「裁判没判出来」与「结论错了」
是两件事**，把前者记成后者，等于把评估器的失败算成轨迹的缺陷。1 档是
★ 一票否决面——拿它去惩罚一次网络抖动，是把红线反过来用。

## 原始 CoT 红线

evidence 拼装前必 :func:`trajectory_pipeline.llm.schema_parse.strip_think`
（CLAUDE.md 红线：raw CoT 不进任何持久化路径）。``extract_json_reporting``
入口已剥，本模块自身不再剥一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from trajectory_pipeline.assembler.observation_view import element_line
from trajectory_pipeline.common.elements import element_name, element_role
from trajectory_pipeline.evaluation.scorers import rubric_version
from trajectory_pipeline.llm.client import LLMClient
from trajectory_pipeline.llm.schema_parse import as_str, extract_json_reporting, field

#: 本实现的置信度先验——**不是模型自报值**（与 d3 / d9 同款理由：
#: 自报 confidence 普遍虚高，未经标注集校准前只当先验）。
CONFIDENCE_JUDGED = 0.8

#: fail-closed 时的置信度。刻意为 0——表示「**没有结论**」。
CONFIDENCE_NONE = 0.0

#: 裁判自报「判不出来」的取值。它**不在**任何一张因子表里 ⇒
#: :func:`~...rubric_version.d10_tier` 查不到 ⇒ 不出分档。
D10_CANNOT_JUDGE = "无法判定"


# ── 协议 ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class D10Case:
    """一条待判读的 D-10 样本（**只装事实**，不装结论）。

    ⚠️ **不装「因子 A 的结论」**以外的任何判断结果——装进来就等于把
    判读结论当输入喂回去（自证）。
    """

    #: 因子 A 的状态：``agree`` / ``disagree``（``undetermined`` 也会被
    #: 收下，但直接落不出分档）。
    state: str
    #: 站点 URL——给裁判当事实层锚点。
    site_url: str = ""
    #: 代码侧结论：分支名 / 分支中文名。
    code_branch: str = ""
    code_branch_label: str = ""
    #: 代码侧的依据陈述原文（``outcomes[].evidence``）。
    code_evidence: str = ""
    #: 判断点（``outcomes[].question``）——「在哪个判断点上落的分支」。
    code_question: str = ""
    #: 人工预核验侧：标签 / 标签中文名 / 人工说明。
    gold_label: str = ""
    gold_label_label: str = ""
    gold_note: str = ""
    #: 观察存档（P1 的 observation 形态：``body_text`` /
    #: ``interactive_elements`` / ``video_tag_count`` …）。
    observation: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class D10Verdict:
    """D-10 的判定结果。"""

    score: int | None
    kind: str  # "scored" | "undetermined"
    state: str = rubric_version.D10_STATE_UNDETERMINED
    factor: str | None = None
    evidence: str = ""
    confidence: float | None = None
    reasons: tuple[str, ...] = ()

    @property
    def is_veto(self) -> bool:
        """rubric G-1 接入点：**只有 1 档**触发样本级一票否决。

        ⚠️ ``score is None`` **不是** 1 档——判不出就不否决
        （fail-closed：``None ≠ 0``）。
        """
        return self.score == 1

    def to_json(self) -> dict[str, Any]:
        return {
            "dimension": "D-10", "score": self.score, "kind": self.kind,
            "state": self.state, "factor": self.factor,
            "evidence": self.evidence, "confidence": self.confidence,
            "reasons": list(self.reasons),
        }


# ── prompt ────────────────────────────────────────────────────────


#: 因子 B 在「一致」时问什么、在「不一致」时问什么——**两张表不交叉**。
#:
#: 让裁判在同一个提示词里同时回答两个不同的问题，它会挑好答的那个答；
#: 所以按因子 A 的状态**只问一个**。
D10_QUESTION_BY_STATE: Mapping[str, str] = {
    rubric_version.D10_STATE_AGREE: "依据陈述质量",
    rubric_version.D10_STATE_DISAGREE: "成因判断对错",
}


def _factor_legend() -> str:
    """两组因子取值 + 各自对应的档——由 :mod:`rubric_version` 的表渲染。

    **不另写一份散文**：改了因子表而不改这里 ⇒ 提示词与尺子漂。
    """
    rows = []
    for state, title in (("agree", "依据陈述质量（结论与人工预核验**一致**时）"),
                         ("disagree", "成因判断对错（结论与人工预核验**不一致**时）")):
        table = rubric_version.D10_FACTORS_BY_STATE[state]
        rows.append(f"### {title}")
        for label, tier in sorted(table.items(), key=lambda kv: -kv[1]):
            rows.append(f"- `{label}`（对应 {tier} 档）")
        rows.append("")
    return "\n".join(rows)


#: 给 LLM 的系统提示。**因子取值由表渲染**（不手抄第二份）。
D10_SYSTEM_PROMPT: str = (
    "你是一名轨迹判读员。你的任务：**仅依据下面提供的材料**，判断这条轨迹"
    "的**依据陈述质量**（或**成因判断的对错**）。\n\n"
    "**你不做什么**：不重新做站点类型判断、不重新判定「结论对不对」——"
    "结论与人工预核验是否一致**已经判过了**，你只判依据本身。\n\n"
    "## 你要回答的问题（用户消息会指定问哪一类，只问一类）\n"
    + _factor_legend()
    + "\n## 输出契约（严格 JSON，无前后文字、无 markdown 代码块）\n"
    "```\n"
    '{"factor": "<上表里与本次问题对应的那一组取值之一>",'
    '"evidence": "引用观察原文短语（≥4 字子串 或 元素 label）或复述因果链",'
    '"confidence": 0.0-1.0'
    "}\n"
    "```\n\n"
    "## 纪律\n"
    "1. `factor` 只能取**本次问题对应那一组**里的取值，一个字不许改。\n"
    "2. `evidence` 必须引用**观察存档里的具体内容**；观察里没有的，"
    "就说「观察里没有」——**不许拿结论本身当依据**。\n"
    "3. **判不出来就说判不出来**：材料不足时把 `factor` 写成 `无法判定`，"
    "并在 `evidence` 里说清缺什么。（`无法判定` 会让本条**不出分档**，"
    "不算扣分；把它硬猜成某一档才是错。）\n"
    "4. **不要输出**任何超出该契约的字段。"
    # 评分锚一栏**从锚点表渲染**（方案 16 §7 / C-10；owner 2026-10-23 裁定
    # D-C7 = rubric 变更强制连带改 prompt）。D-10 的锚点表是 v1.3 新增的。
    + rubric_version.render_anchor_block("d10")
)

#: 给 LLM 的用户提示的体量上限（仿 ``d3_scorer.D3_BODY_CHARS``）。
D10_BODY_CHARS = 600
D10_ELEMENTS = 25


def _render_observation(obs: Mapping[str, Any]) -> str:
    body = str(obs.get("body_text") or "").strip()
    if len(body) > D10_BODY_CHARS:
        body = body[:D10_BODY_CHARS] + "…[已截断]"
    elements = list(obs.get("interactive_elements") or ())[:D10_ELEMENTS]
    elem_lines = []
    for e in elements:
        if not isinstance(e, Mapping):
            continue
        # 行写法与训练侧**同一处**（``element_line``）——方言一换，
        # 两边各写一份就有一边停在 <tag>、另一边变成 [role]。
        if element_role(e) or element_name(e).strip():
            elem_lines.append(element_line(e))
    media = int(obs.get("video_tag_count") or 0)
    iframe = int(obs.get("iframe_count") or 0)
    page_title = str(obs.get("page_title") or "")
    url = str(obs.get("url") or "")
    total = int(obs.get("elements_total") or 0) or len(elements)
    return (
        f"[观察] url={url} title={page_title}\n"
        f"  body: {body or '(空)'}\n"
        f"  video_tag_count={media} iframe_count={iframe} elements_total={total}\n"
        f"  elements ({len(elements)} 条):\n" + "\n".join(elem_lines)
    )


def _render_user(case: D10Case) -> str:
    question = D10_QUESTION_BY_STATE.get(case.state, "")
    allowed = rubric_version.D10_FACTORS_BY_STATE.get(case.state, {})
    allowed_text = " / ".join(f"`{k}`" for k in allowed) or "（本次无合法取值）"
    return (
        f"站点 URL：{case.site_url or '(未知)'}\n"
        f"本次要回答的问题：**{question or '(状态未知)'}**\n"
        f"本次允许的 factor 取值：{allowed_text}\n\n"
        f"[代码侧结论] 分支={case.code_branch or '(空)'}"
        f"（{case.code_branch_label or '无中文名'}）"
        f" 判断点={case.code_question or '(空)'}\n"
        f"[代码侧依据陈述] {case.code_evidence or '(空)'}\n\n"
        f"[人工预核验] 标签={case.gold_label or '(空)'}"
        f"（{case.gold_label_label or '无中文名'}）\n"
        f"[人工说明] {case.gold_note or '(无)'}\n\n"
        + (_render_observation(case.observation) if case.observation else "[观察] (无观察存档)")
    )


# ── 判读前的自查 ──────────────────────────────────────────────────


def _truncated_flag(value: Any) -> bool:
    """``truncated`` 字段统一判真——兼容 p1 bool 与 p2 dict 两种形态
    （INC42；与 d3_scorer._truncated_flag 同款独立实现）。

    dict（``{'source': False, 'view': False}``）按 ``any(values)`` 判；
    非空 dict 不等于截断——第一版把它一律判真，E001__57bbf995__site-2
    被误标截断。
    """
    if isinstance(value, Mapping):
        return any(bool(v) for v in value.values())
    return bool(value)


def _observation_usable(obs: Mapping[str, Any] | None) -> str:
    """这份观察**能不能支撑**依据陈述的判读。空串 = 能。

    ⚠️ **判读「依据有没有引用观察之外的东西」的前提是看过观察**：没看过
    就说它引用了观察外的东西，是冤枉它。同款纪律见 ``d3_scorer``
    的 ``_observation_shortfall``（**独立实现**——保持
    ``evaluation ↔ rationale`` 单向依赖方向）。

    M-3：与 d3 的决策边界对齐——**``degraded`` 非空 / ``truncated`` 判真
    也算不可用**。截断观察里「没写到的实体」与「页面上真没有」分不清，
    拿它当充分证据判「依据引用了观察之外」，就是把采集的缺件算成轨迹的错。
    边界一致性由 ``tests/contract/test_observation_shortfall_consistency.py``
    盯着（d10 是本契约的四处之一；原 code_scorer 那处已于 rubric v1.4 退出）。
    """
    if not obs:
        return "没有观察存档"
    if list(obs.get("degraded") or ()):
        return f"观察有采集降级项 {list(obs['degraded'])}"
    if _truncated_flag(obs.get("truncated")):
        return "观察被上游 max_chars 截断，「依据是否引用观察之外」无从判读"
    body = str(obs.get("body_text") or "").strip()
    elements = list(obs.get("interactive_elements") or ())
    total = int(obs.get("elements_total") or 0) or len(elements)
    if not body and not elements:
        return "正文与交互元素皆空"
    if total > len(elements):
        return (f"交互元素被裁剪（保留 {len(elements)} / 采到 {total}），"
                "「依据是否引用观察之外」无从判读")
    return ""


# ── 打分 ──────────────────────────────────────────────────────────


def score_d10(case: D10Case, *,
              llm_client: LLMClient | None = None,
              system_prompt: str | None = None) -> D10Verdict:
    """D-10 打分。**永不抛**——LLM 失败 / 解析失败 / 材料不足，
    全部落 ``undetermined``（**不落 1 档**）。

    Args:
        case: 待判读的事实（:class:`D10Case`）。
        llm_client: LLM 后端；``None`` → 本条不出分（fail-closed）。
        system_prompt: **换一版提示词**（默认用 :data:`D10_SYSTEM_PROMPT`）。
            理由同 ``d3_scorer``：A/B 要比的就是「同一份输入、两版提示词」。
    """
    state = case.state
    if state == rubric_version.D10_STATE_UNDETERMINED:
        return D10Verdict(
            score=None, kind="undetermined", state=state,
            confidence=CONFIDENCE_NONE,
            reasons=("因子 A（一致性）未判定——口径 C 的三态里它不进一致 / "
                     "不一致任一档，本卡不出分档",),
        )
    if state not in rubric_version.D10_FACTORS_BY_STATE:
        return D10Verdict(
            score=None, kind="undetermined", state=state,
            confidence=CONFIDENCE_NONE,
            reasons=(f"因子 A 的状态 {state!r} 不在 "
                     f"{sorted(rubric_version.D10_FACTORS_BY_STATE)} 里",),
        )
    if llm_client is None:
        return D10Verdict(
            score=None, kind="undetermined", state=state,
            confidence=CONFIDENCE_NONE,
            reasons=("未提供 LLM client——judge 不在评估链内，本条不出分",),
        )

    shortfall = _observation_usable(case.observation)
    if shortfall:
        return D10Verdict(
            score=None, kind="undetermined", state=state,
            confidence=CONFIDENCE_NONE,
            reasons=(f"观察不足以判读依据陈述：{shortfall}",),
        )
    if not str(case.code_evidence or "").strip():
        # 依据陈述都没有 —— 无从判「依据准不准」。
        return D10Verdict(
            score=None, kind="undetermined", state=state,
            confidence=CONFIDENCE_NONE,
            reasons=("代码侧没有依据陈述文本——无从判读依据质量",),
        )
    user_prompt = _render_user(case)
    try:
        raw = llm_client.chat(system_prompt or D10_SYSTEM_PROMPT, user_prompt)
    except Exception as exc:  # pragma: no cover - 兜底：裁判挂了不许炸流水线
        return D10Verdict(
            score=None, kind="undetermined", state=state,
            confidence=CONFIDENCE_NONE,
            reasons=(f"LLM 调用异常：{exc}",),
        )

    data, why = _parse_response(raw)
    if data is None:
        return D10Verdict(
            score=None, kind="undetermined", state=state,
            confidence=CONFIDENCE_NONE,
            reasons=(f"LLM 未给出可解析的答案（{why}）——判不出，**不落 1 档**",),
        )

    factor = as_str(field(data, "factor", ""))
    evidence = as_str(field(data, "evidence", ""))
    if not factor:
        return D10Verdict(
            score=None, kind="undetermined", state=state,
            confidence=CONFIDENCE_NONE,
            reasons=("LLM 没给 factor——判不出，**不落 1 档**",),
        )
    if factor == D10_CANNOT_JUDGE:
        return D10Verdict(
            score=None, kind="undetermined", state=state,
            factor=factor, evidence=evidence,
            confidence=CONFIDENCE_NONE,
            reasons=(f"裁判自报「{D10_CANNOT_JUDGE}」——本条不出分档",),
        )

    tier = rubric_version.d10_tier(state, factor)
    if tier is None:
        allowed = sorted(rubric_version.D10_FACTORS_BY_STATE[state])
        return D10Verdict(
            score=None, kind="undetermined", state=state,
            factor=factor, evidence=evidence,
            confidence=CONFIDENCE_NONE,
            reasons=(f"factor {factor!r} 不在本次问题的合法取值 {allowed} 里"
                     "——契约外取值不猜档，落不出分档",),
        )

    return D10Verdict(
        score=tier, kind="scored", state=state, factor=factor,
        evidence=evidence, confidence=CONFIDENCE_JUDGED,
        reasons=(f"因子 A={state} × 因子 B={factor} → {tier} 档"
                 f"（rubric v1.3 D-10 五档映射）",),
    )


# ── 解析 ──────────────────────────────────────────────────────────


def _parse_response(raw: str | None) -> tuple[dict[str, Any] | None, str]:
    """从 LLM 原文抠 JSON。**入口已剥 think**（``schema_parse`` 内部）。

    Returns:
        ``(data, why)`` —— ``why`` 仅在 ``data is None`` 时非空。
    """
    if not raw:
        return None, "LLM 返回空"
    data, _repairs = extract_json_reporting(raw)
    if data is None:
        return None, "JSON 抠不出来"
    return data, ""


__all__ = [
    "CONFIDENCE_JUDGED",
    "CONFIDENCE_NONE",
    "D10_CANNOT_JUDGE",
    "D10_SYSTEM_PROMPT",
    "D10_BODY_CHARS",
    "D10_ELEMENTS",
    "D10Case",
    "D10Verdict",
    "D10_QUESTION_BY_STATE",
    "score_d10",
]
