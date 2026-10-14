"""LLM 裁判层评分器 —— **D-3「播放站识别正确性」**（样本级）。

## 判据

rubric v1.2 §D-3：「站点类型判断（播放站/导航站/无关站）与人工判断一致」。
管辖层 = LLM 裁判（rubric 明写），与 D-4 的「代码断言 + LLM 复检」不同——
D-3 **全是 LLM**：rubric 锚点本身就是「与黄金集预标类别对照」，没有
机械判据可剥出来。

## 评分锚（直接照 rubric §D-3）

===============================================  ======  ======
情形                                             分数    is_veto
===============================================  ======  ======
category ∈ 契约 5 类 + evidence 引用观察具体短语   5       否
category ∈ 契约 5 类 + evidence 泛泛描述           4       否
category == "ambiguous"                           3       否
category 不在契约 5 类（判错）；playable 但         2       否
  subtype 不在 4 类内也归 2
LLM 调不通 / JSON 解析失败 **且** 观察充分           1       **是**
===============================================  ======  ======

``is_veto`` 是 rubric G-1「样本级一票否决」的接入点：score=1 时整条样本
转「负面池」，其余维度停止打分。

## 三态纪律（与 d4/d5/d6 同款）

``score=None`` ≠ ``score=0``。前一种进 quarantine、不出分档；后一种
按 0 分算。前者对应「观察三项全空 / 观察裁剪 / 元素被裁 / ``body_text``
为空+元素空」——这四种是采集侧故障，不是轨迹的缺陷，所以**不扣分**，
只留痕（``kind="undetermined"``）。

## 失败是返回值

:func:`score_d3` **永不抛**：LLM 调不通 / JSON 抠不出来 / 解析后
``category`` 不在契约集合 / ``confidence`` 越界，全部走不同分支——
LLM 失败判 1 分（且该样本可判）、采集不充分判 ``None``、``llm_client``
为 ``None`` 判 ``None``。三种结局各走各路，不静默吞。

## 不采信模型自报 confidence

``confidence`` 字段写死 :data:`CONFIDENCE_JUDGED`（0.8） 或
:data:`CONFIDENCE_NONE`（0.0）。``llm_perceptor.CONFIDENCE_JUDGED``
模块 docstring 已记：自报 confidence 普遍虚高，须经验校准后才可用于阈值
判断；本模块给的是**未校准先验**。拿到黄金集后再按题校准。

## 独立实现 ``_observation_shortfall``

与 :func:`trajectory_pipeline.rationale.gates.observation_shortfall`、
:func:`trajectory_pipeline.evaluation.scorers.code_scorer._observation_shortfall`
同款判据但**分开实现**——保持 ``evaluation ↔ rationale`` 单向依赖方向
（``docs/01-架构总览.md``）。两处口径靠 ``tests/contract/`` 双向盯住。

## raw CoT 红线

evidence 拼装前必 :func:`trajectory_pipeline.llm.schema_parse.strip_think`
——CLAUDE.md 红线：raw CoT 不进任何持久化路径。``extract_json_reporting``
入口已剥，本模块自身不再剥一次。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from trajectory_pipeline.assembler.schema import Sample
from trajectory_pipeline.llm.client import LLMClient
from trajectory_pipeline.llm.schema_parse import (
    as_str,
    extract_json_reporting,
    field,
)

#: rubric §D-3 5 类契约——LLM 只能在这五类里选。其它字符串（实测模型会
#: 飘出 ``video_hosting`` / ``streaming`` 之类）一律归 2 档（判错）。
D3_CATEGORIES: frozenset[str] = frozenset({
    "playable", "nav_aggregator", "review", "irrelevant", "ambiguous",
})

#: playable 的子分类——仅 category == "playable" 时必填。其它三类不
#: 应填，但模型偶尔会填且字段类型不固定，解析层不抛、归 2 档。
D3_SUBTYPES: frozenset[str] = frozenset({
    "free", "login_required", "partial_free", "subscription",
})

#: 本实现的置信度先验——**不是模型自报值**。与 ``llm_perceptor`` 同源
#: 理由：未经标注集校准，evidence 里写明它不是实测值，避免下游误当。
CONFIDENCE_JUDGED = 0.8

#: fail-closed 时的置信度。刻意为 0——表示「**没有结论**」。
CONFIDENCE_NONE = 0.0

#: 判定结果类型——三态。
VerdictKind = Literal["scored", "undetermined"]


# ── 协议 ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class D3Verdict:
    """D-3 的判定结果。"""

    score: int | None
    kind: str  # "scored" | "undetermined"
    category: str | None = None
    playable_subtype: str | None = None
    evidence: str = ""
    confidence: float | None = None
    reasons: tuple[str, ...] = ()

    @property
    def is_veto(self) -> bool:
        """rubric G-1 接入点：1 档 = 整条样本转「负面池」。"""
        return self.score == 1

    def to_json(self) -> dict[str, Any]:
        return {
            "dimension": "D-3", "score": self.score, "kind": self.kind,
            "category": self.category,
            "playable_subtype": self.playable_subtype,
            "evidence": self.evidence, "confidence": self.confidence,
            "reasons": list(self.reasons),
        }


# ── 取值 ──────────────────────────────────────────────────────────


def _get(obj: Any, key: str, default: Any = "") -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _observations(sample: Any) -> list[Mapping[str, Any]]:
    """本样本的观察列表。``Sample`` 对象在顶层、P2 字典在 ``six`` 里。"""
    if isinstance(sample, Mapping):
        six = sample.get("six") or {}
        obs = six.get("observations") or sample.get("observations") or ()
    else:
        obs = getattr(sample, "observations", ()) or ()
    return [o for o in obs if isinstance(o, Mapping)]


def _site_url(sample: Any) -> str:
    """站点 URL——给 LLM 当事实层锚点。

    优先 outcome.site_url（已校验），回落 sample 顶层 / P2 字典 / 空串。
    """
    if isinstance(sample, Mapping):
        outcome = sample.get("outcome") or {}
        url = (outcome.get("site_url") if isinstance(outcome, Mapping)
               else "") or ""
        if url:
            return str(url)
        return str(sample.get("site_url") or "")
    outcome = getattr(sample, "outcome", None)
    if outcome is not None:
        url = getattr(outcome, "site_url", "") or ""
        if url:
            return str(url)
    return str(getattr(sample, "site_url", "") or "")


# ── 观察充分性（独立实现） ────────────────────────────────────────


def _observation_shortfall(obs: Mapping[str, Any] | None) -> str:
    """这份观察**能不能支撑**站点类型判断。空串 = 能。

    同款判据于 :func:`trajectory_pipeline.rationale.gates.observation_shortfall`
    与 :func:`trajectory_pipeline.evaluation.scorers.code_scorer._observation_shortfall`，
    但**独立实现**——保持 ``evaluation ↔ rationale`` 单向依赖方向。
    两处口径靠 ``tests/contract/test_observation_shortfall_consistency.py``
    双向盯住。
    """
    if not obs:
        return "没有观察"
    if list(obs.get("degraded") or ()):
        return f"观察有采集降级项 {list(obs['degraded'])}"
    if obs.get("truncated"):
        return "观察被上游 max_chars 截断，实体可能落在截断外"
    elements = list(obs.get("interactive_elements") or ())
    total = int(obs.get("elements_total") or 0) or len(elements)
    if total > len(elements):
        return (f"交互元素被裁剪（保留 {len(elements)} / 采到 {total}），"
                f"实体可能落在裁剪区外")
    if not str(obs.get("body_text") or "").strip() and not elements:
        return "正文与交互元素皆空，采集可能整体失败"
    return ""


def _all_observations_insufficient(
    observations: list[Mapping[str, Any]],
) -> tuple[str, ...]:
    """返回「每份观察都判不了」的原因列表。空元组 = 至少有一份可判。"""
    if not observations:
        return ("本样本没有观察",)
    reasons = [_observation_shortfall(o) for o in observations]
    reasons = [r for r in reasons if r]
    if len(reasons) == len(observations):
        # 全部都判不了 → 整条不可判定（quarantine）
        return tuple(reasons)
    return ()


# ── prompt ────────────────────────────────────────────────────────


#: 给 LLM 的系统提示。中文 + JSON 契约示例 + 5 类枚举 + playable_subtype
#: 4 子枚举。**字段顺序按字母**：category → playable_subtype → evidence
#: → confidence——与 schema_parse.ALIASES 表的键对齐，便于解析层兜底。
D3_SYSTEM_PROMPT: str = (
    "你是一名站点类型分类员。你的任务：**仅依据下面提供的观察存档**，"
    "判断这个站点属于哪一类。\n\n"
    "## 类别（5 选 1，必填）\n"
    "- ``playable``：能直接播放目标视频的站点（含需登录/付费/部分免费子分类）。\n"
    "- ``nav_aggregator``：导航/聚合站——把用户引到外部播放站，自身不存正片。\n"
    "- ``review``：影评/资讯/解说站——围绕某片讨论，但不播放正片。\n"
    "- ``irrelevant``：与目标视频完全无关（电商、博客、论坛其他主题）。\n"
    "- ``ambiguous``：观察不足以定类——既可能是导航聚合也可能含内嵌播放器。\n\n"
    "## playable 子分类（仅 ``playable`` 时必填）\n"
    "- ``free``：免费可看\n"
    "- ``login_required``：需登录\n"
    "- ``partial_free``：部分免费（如前 N 集免费、预览可看正片需付费）\n"
    "- ``subscription``：需订阅/付费\n\n"
    "## 输出契约（严格 JSON，无前后文字、无 markdown 代码块）\n"
    "```\n"
    '{"category": "playable|nav_aggregator|review|irrelevant|ambiguous",'
    '"playable_subtype": "free|login_required|partial_free|subscription|null",'
    '"evidence": "依据引用观察原文短语（≥4 字子串 或 元素 label 或数字）",'
    '"confidence": 0.0-1.0'
    "}\n"
    "```\n\n"
    "## 纪律\n"
    "1. ``evidence`` 必含**观察中的具体短语**——别只写「不是播放站」。\n"
    "2. ``playable`` 时 ``playable_subtype`` 必填；其它 4 类填 ``null``。\n"
    "3. **不要输出**任何超出该契约的字段。"
)

#: 给 LLM 的用户提示——观察存档 + 站点 URL。观察裁剪上限仿
#: ``llm_perceptor.LLM_BODY_CHARS``（实测：1200→600 后大输入超时恢复）。
D3_BODY_CHARS = 600
D3_ELEMENTS = 25


def _render_observation(obs: Mapping[str, Any]) -> str:
    body = str(obs.get("body_text") or "").strip()
    if len(body) > D3_BODY_CHARS:
        body = body[:D3_BODY_CHARS] + "…[已截断]"
    elements = list(obs.get("interactive_elements") or ())[:D3_ELEMENTS]
    elem_lines = []
    for e in elements:
        if not isinstance(e, Mapping):
            continue
        tag = str(e.get("tag") or "")
        label = str(e.get("label") or "").strip()
        if tag or label:
            elem_lines.append(f"  - <{tag}>{(' ' + label) if label else ''}")
    media = int(obs.get("video_tag_count") or 0)
    iframe = int(obs.get("iframe_count") or 0)
    page_title = str(obs.get("page_title") or "")
    url = str(obs.get("url") or "")
    return (
        f"[观察] url={url} title={page_title}\n"
        f"  body: {body or '(空)'}\n"
        f"  video_tag_count={media} iframe_count={iframe}\n"
        f"  elements ({len(elements)} 条):\n" + "\n".join(elem_lines)
    )


def _render_user(site_url: str, observations: list[Mapping[str, Any]]) -> str:
    head = f"目标站点 URL：{site_url or '(未知)'}\n\n"
    body = "\n\n".join(_render_observation(o) for o in observations) or "(无观察)"
    return head + body


# ── 解析 ──────────────────────────────────────────────────────────


#: 解析失败但**不**算 veto 的回 1 档信号——观察充分但 LLM 没给答案。
#: ``chat()`` 返 None / JSON 抠不出 / category 解析失败全归这里。
_PARSE_FAIL = "parse_fail"


def _parse_response(raw: str | None) -> tuple[dict[str, Any] | None, str]:
    """从 LLM 原文抠 JSON。**入口已剥 think**（``schema_parse`` 内部）。

    Returns:
        ``(data, why)`` —— ``why`` 仅在 ``data is None`` 时非空。
    """
    if not raw:
        return None, "LLM 返回空"
    data, _repairs = extract_json_reporting(raw)
    if data is None:
        return None, _PARSE_FAIL
    return data, ""


# ── evidence 「concrete」启发式 ────────────────────────────────────


_NUMBER_RE = re.compile(r"\d+")


def _is_evidence_concrete(evidence: str,
                          observations: list[Mapping[str, Any]]) -> bool:
    """evidence 是否引用了观察中的具体短语。

    三类信号（任一成立即视为具体）：
      (a) evidence 含某条观察 ``body_text`` 的 ≥4 字连续子串；
      (b) evidence 含某条观察 ``interactive_elements`` 的 ``label``；
      (c) evidence 提到具体数字（``video_tag_count`` / ``iframe_count``
          之一 > 0 时的数字本身）。

    ⚠️ 这是**字面层**匹配——LLM 真要编 quote 也能骗过。
    evidence 可信度由 contract 7「rubric v1.2 对齐」+ raw CoT 红线
    钉住，本启发式只用来在 5/4 两档之间二选一。
    """
    if not evidence or not observations:
        return False

    for obs in observations:
        body = str(obs.get("body_text") or "")
        if body:
            for start in range(0, len(body) - 3):
                piece = body[start:start + 4]
                if piece and piece in evidence:
                    return True
        for elem in (obs.get("interactive_elements") or ()):
            if not isinstance(elem, Mapping):
                continue
            label = str(elem.get("label") or "").strip()
            if label and len(label) >= 2 and label in evidence:
                return True
        media = int(obs.get("video_tag_count") or 0)
        iframe = int(obs.get("iframe_count") or 0)
        if media > 0 and str(media) in evidence:
            return True
        if iframe > 0 and str(iframe) in evidence:
            return True
        for n in _NUMBER_RE.findall(evidence):
            try:
                if int(n) == media and media > 0:
                    return True
                if int(n) == iframe and iframe > 0:
                    return True
            except ValueError:
                pass
    return False


# ── 打分 ──────────────────────────────────────────────────────────


def score_d3(sample: Sample | Any,
             *, llm_client: LLMClient | None = None) -> D3Verdict:
    """D-3 打分。**永不抛**——LLM 失败 / 解析失败 / 观察不充分，
    全部走对应分支。"""
    observations = _observations(sample)
    insufficient = _all_observations_insufficient(observations)
    if insufficient:
        # rubric 锚点 1：「未判且该站本可判」才回 1 档；
        # 观察**整体**不充分是采集侧故障，不是轨迹的缺陷——quarantine。
        return D3Verdict(
            score=None, kind="undetermined",
            reasons=(f"观察不充分（{len(insufficient)} 份都判不了）："
                     + "；".join(insufficient),),
        )

    if llm_client is None:
        # cmd_agreement 可在 judge 链外空跑——这条让「不传 LLM」合法。
        return D3Verdict(
            score=None, kind="undetermined",
            reasons=("未提供 LLM client——judge 不在评估链内，"
                     "本条不出分（与 cmd_agreement 空跑同款纪律）",),
        )

    site_url = _site_url(sample)
    user_prompt = _render_user(site_url, observations)
    raw = llm_client.chat(D3_SYSTEM_PROMPT, user_prompt)
    data, why = _parse_response(raw)
    if data is None:
        # rubric 锚点 1：观察充分 + LLM 没给答案 = fail-closed + 该站本可判
        # → 1 档（**is_veto**）。
        return D3Verdict(
            score=1, kind="scored",
            confidence=CONFIDENCE_JUDGED,
            evidence="",
            reasons=(f"LLM 未判（{why}），观察充分 → rubric §D-3 锚点 1："
                     "未判且本可判",),
        )

    category = as_str(field(data, "category", ""))
    if category not in D3_CATEGORIES:
        # 模型写了契约外的字符串（实测 ``video_hosting`` / ``streaming``
        # 之类）→ 判错，归 2 档。
        return D3Verdict(
            score=2, kind="scored", category=category or None,
            confidence=CONFIDENCE_JUDGED,
            evidence=as_str(field(data, "evidence", "")),
            reasons=(f"category {category!r} 不在契约 5 类 "
                     f"{sorted(D3_CATEGORIES)}——rubric §D-3 锚点 2：判错",),
        )

    evidence = as_str(field(data, "evidence", ""))

    if category == "ambiguous":
        # rubric 锚点 3：ambiguous 处置正确（判「选择进入验证」）= 3 档。
        return D3Verdict(
            score=3, kind="scored", category=category,
            confidence=CONFIDENCE_JUDGED, evidence=evidence,
            reasons=("ambiguous 站处置正确（归 ambiguous 而非判" +
                     "「是/否播放站」）——rubric §D-3 锚点 3",),
        )

    if category == "playable":
        subtype = as_str(field(data, "playable_subtype", "")) or None
        if subtype not in D3_SUBTYPES:
            # playable 但 subtype 飘出去了——rubric 锚点 2 收口。
            return D3Verdict(
                score=2, kind="scored", category=category,
                playable_subtype=subtype,
                confidence=CONFIDENCE_JUDGED, evidence=evidence,
                reasons=(f"playable 的 subtype {subtype!r} 不在契约 4 类 "
                         f"{sorted(D3_SUBTYPES)}——rubric §D-3 锚点 2",),
            )
    else:
        subtype = None

    # 5 vs 4：判对（category ∈ 契约 5 类）+ evidence 是否引用观察具体短语。
    if _is_evidence_concrete(evidence, observations):
        score = 5
        why_score = "category 判对 + evidence 引用观察具体短语——rubric §D-3 锚点 5"
    else:
        score = 4
        why_score = "category 判对 + evidence 泛泛描述——rubric §D-3 锚点 4"

    return D3Verdict(
        score=score, kind="scored", category=category,
        playable_subtype=subtype, evidence=evidence,
        confidence=CONFIDENCE_JUDGED, reasons=(why_score,),
    )


__all__ = [
    "D3_CATEGORIES", "D3_SUBTYPES",
    "CONFIDENCE_JUDGED", "CONFIDENCE_NONE",
    "VerdictKind", "D3Verdict", "score_d3",
    "D3_SYSTEM_PROMPT", "D3_BODY_CHARS", "D3_ELEMENTS",
]
