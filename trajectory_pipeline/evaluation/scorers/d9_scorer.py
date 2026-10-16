"""LLM 裁判层评分器 —— **D-9「思考真实性」**（样本级，复检层）。

## 判据

rubric v1.2 §D-9：「思考内容是『当时观察的忠实翻译』——不幻觉、不后见之明」。
管辖层 = LLM 裁判（rubric 明写）——它是 :mod:`trajectory_pipeline.rationale.gates`
闸门之上的**复检层**：闸门查硬事实，judge 查「写得是否通顺且不自相矛盾」。

## 评分锚（直接照 rubric §D-9）

===============================================  ======
情形                                             分数
===============================================  ======
全部锚定；不确定处如实表达                         5
1 处措辞含糊，无事实错误                           4
1 处轻微幻觉（不影响动作）或 1 处后见之明           3
系统性不符或套话替代推理                           2
思考编造关键事实并驱动错误动作（与 D-2 联动）      1
===============================================  ======

D-9 **不是 G-1 一票否决维度**（rubric §G-1 只有 D-5 / D-10 是）：score=1
只是「这条样本 rationale 有问题」，不会让整条样本直接进负面池。
``is_veto`` property 仍按同款协议保留，方便未来 rubric 升级时挂入 G-1
而不改 d9_scorer 接口。

## 触发条件（成本控制）

本模块**不会对所有样本都跑 LLM**——rationale 平均 800-1500 token / 次，
3 查全过且无可判性短板的 rationale 没有 LLM 复检的余地（plan §三·第三步）：

  - ``gate.status == "failed"`` → **强制**走 LLM（硬事实有问题，看 LLM 怎么圆）
  - ``gate.status == "passed" + undeterminable 非空`` → 走 LLM（不能全判的抓出）
  - ``gate.status == "passed" + undeterminable 空`` → **不走** LLM（成本控制）
  - ``gate.status == "not_run"`` → 不走（rationale 为空，闸门未跑）

「不走」会得到 ``kind="not_evaluated"``——这是成本控制跳过，**不是**
``undetermined``（不可判定）。两者语义不同，下游可分别记账：
``undetermined`` → 仲裁队列；``not_evaluated`` → 通过闸门即可，不上报。

## 三态 + 一档

  - ``score=None`` ≠ ``score=0``：前者 quarantine（不可判定）
  - ``score=N`` ∈ {1,2,3,4,5}：LLM 复检有结论
  - ``kind="not_evaluated"``：成本控制跳过，**不出分**

## 失败是返回值

LLM 调不通 / JSON 抠不出 / verdict 不在契约集合 → ``undetermined``（不出分，
不静默吞）。D-9 rubric 锚点 1「思考编造关键事实」是 LLM **真实判断**得出的
1 档，不是 fail-closed——失败走 ``undetermined`` 不走 1。

## 不采信模型自报 confidence

``confidence`` 字段写死 :data:`CONFIDENCE_JUDGED`（0.8） 或
:data:`CONFIDENCE_NONE`（0.0）。理由与 :mod:`trajectory_pipeline.evaluation.scorers.d3_scorer`
同源：未经标注集校准，避免下游误当实测值。

## 独立实现 ``_observation_shortfall``

与 :func:`trajectory_pipeline.rationale.gates.observation_shortfall`、
:func:`trajectory_pipeline.evaluation.scorers.code_scorer._observation_shortfall`
同款判据但**分开实现**——保持 ``evaluation ↔ rationale`` 单向依赖方向。
两处口径靠 ``tests/contract/test_observation_shortfall_consistency.py``
双向盯住（与 d3_scorer 同款——该 contract 测试本批可与 d3 一起补）。

## raw CoT 红线

evidence 拼装前必 :func:`trajectory_pipeline.llm.schema_parse.strip_think`
——CLAUDE.md 红线。``extract_json_reporting`` 入口已剥。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from trajectory_pipeline.assembler.schema import GateMark, Sample
from trajectory_pipeline.llm.client import LLMClient
from trajectory_pipeline.llm.schema_parse import (
    as_str,
    extract_json_reporting,
    field,
)

#: rubric §D-9 6 类契约——LLM 只能在这六类里选。
#: 与 rubric 锚点表的对应：anchored=5 / vague=4 /
#: mild_hallucination=3 / hindsight=3 / systematic=2 / misleading=1。
D9_VERDICTS: frozenset[str] = frozenset({
    "anchored", "vague", "mild_hallucination", "hindsight",
    "systematic", "misleading",
})

#: 本实现的置信度先验——**不是模型自报值**。与 d3_scorer / llm_perceptor 同源。
CONFIDENCE_JUDGED = 0.8

#: fail-closed 时的置信度。刻意为 0——表示「**没有结论**」。
CONFIDENCE_NONE = 0.0

#: 判定结果类型——三态 + 一档成本控制跳过。
#: ``not_evaluated`` 与 ``undetermined`` 语义不同：前者是闸门过的
#: 「无需复检」，后者是「想复检但没法复检」。下游分别记账。
VerdictKind = Literal["scored", "undetermined", "not_evaluated"]


# ── 协议 ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class D9Verdict:
    """D-9 的判定结果。"""

    score: int | None
    kind: str  # "scored" | "undetermined" | "not_evaluated"
    verdict: str | None = None   # D9_VERDICTS 之一
    evidence: str = ""
    confidence: float | None = None
    reasons: tuple[str, ...] = ()

    @property
    def is_veto(self) -> bool:
        """D-9 **不是** G-1 一票否决维度（rubric §G-1 只有 D-5 / D-10 是）。
        此 property 仅为协议对称保留——见模块 docstring；始终返回 False，
        即使 ``score == 1``（rubric 锚点 1 = misleading 是 LLM 真实判断，
        不是 fail-closed，也不进负面池）。"""
        return False

    def to_json(self) -> dict[str, Any]:
        return {
            "dimension": "D-9", "score": self.score, "kind": self.kind,
            "verdict": self.verdict,
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


def _as_mappings(items: Any) -> list[Mapping[str, Any]]:
    """元素序列 → Mapping 列表。**dataclass 走它的 ``to_json()``**。

    ⚠️ 为什么不能只认 ``Mapping``：``Sample`` 对象与 P2 字典的元素
    **形状不同**——``Sample.rationales`` 是 ``Rationale`` dataclass 元组、
    ``Sample.actions`` 是 ``ActionView`` 元组，而 P2 字典里的
    ``six['rationales']`` 是 dict 列表。只认 Mapping 会让前者**静默变空**：
    不抛异常、不给 ``undetermined``，judge 照常出分，只是它手里根本没有
    rationale 原文（INC46 单档探针实测出 ``vague``，evidence 写着
    「未提供 rationale 原文」——那句话是 judge 在如实说它没东西可判）。

    生产路径（``evaluation/cli.py``）喂的是 P2 dict，绕开了这个坑；
    但 :func:`score_d9` 的签名明写 ``Sample | Any``，签名承诺了就得真能吃。
    """
    out: list[Mapping[str, Any]] = []
    for it in items or ():
        if isinstance(it, Mapping):
            out.append(it)
            continue
        to_json = getattr(it, "to_json", None)
        if callable(to_json):
            got = to_json()
            if isinstance(got, Mapping):
                out.append(got)
    return out


def _observations(sample: Any) -> list[Mapping[str, Any]]:
    """本样本的观察列表。``Sample`` 对象在顶层、P2 字典在 ``six`` 里。"""
    if isinstance(sample, Mapping):
        six = sample.get("six") or {}
        obs = six.get("observations") or sample.get("observations") or ()
    else:
        obs = getattr(sample, "observations", ()) or ()
    return _as_mappings(obs)


def _rationales(sample: Any) -> list[Mapping[str, Any]]:
    """rationale 列表。``Sample`` 对象在顶层、P2 字典在 ``six`` 里。
    每条 rationale 含 ``step_index`` / ``text`` / ``conclusion``。
    """
    if isinstance(sample, Mapping):
        six = sample.get("six") or {}
        rats = six.get("rationales") or sample.get("rationales") or ()
    else:
        rats = getattr(sample, "rationales", ()) or ()
    return _as_mappings(rats)


def _actions(sample: Any) -> list[Mapping[str, Any]]:
    if isinstance(sample, Mapping):
        six = sample.get("six") or {}
        acts = six.get("actions") or sample.get("actions") or ()
    else:
        acts = getattr(sample, "actions", ()) or ()
    return _as_mappings(acts)


def _gate_mark(sample: Any) -> GateMark | None:
    if isinstance(sample, Mapping):
        gate = sample.get("gate")
        if gate is None:
            return None
        if isinstance(gate, Mapping):
            return GateMark(status=gate.get("status", "not_run"),
                            reasons=tuple(gate.get("reasons") or ()))
        return gate  # already GateMark
    return getattr(sample, "gate", None)


# ── 观察充分性（独立实现） ────────────────────────────────────────


def _truncated_flag(value: Any) -> bool:
    """``truncated`` 字段的统一判真——兼容两种存档形态（INC42）。

    - p1 存档：bool；
    - p2 样本 ``six.observations``：dict（如 ``{'source': False, 'view': False}``）。

    INC42 前的 ``if obs.get("truncated")`` 把**非空 dict 一律判真**
    （全 False 也是真）→ E001__57bbf995__site-2 被误标「观察被截断」。
    dict 形态按 ``any(values)`` 判：任一子观察截断才算截断。
    """
    if isinstance(value, Mapping):
        return any(bool(v) for v in value.values())
    return bool(value)


def _observation_shortfall(obs: Mapping[str, Any] | None) -> str:
    """这份观察**能不能支撑**rationale 复检。空串 = 能。

    同款判据于 :func:`trajectory_pipeline.rationale.gates.observation_shortfall`
    与 :func:`trajectory_pipeline.evaluation.scorers.code_scorer._observation_shortfall`，
    但**独立实现**——保持 ``evaluation ↔ rationale`` 单向依赖方向。
    """
    if not obs:
        return "没有观察"
    if list(obs.get("degraded") or ()):
        return f"观察有采集降级项 {list(obs['degraded'])}"
    if _truncated_flag(obs.get("truncated")):
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
        return tuple(reasons)
    return ()


#: 裁剪敏感 verdict——断言「rationale 引用了观察中不存在的锚」。
#: 在交互元素被裁剪的观察上，「没找到锚」与「锚被裁掉」不可区分，
#: 这些 verdict 不可信（INC42 方向性论证）。anchored / vague /
#: hindsight 不在此列：保留区里找到的锚必为真（裁剪只减不增），
#: vague / hindsight 是措辞判断，不依赖元素全集。
_CAPPING_SENSITIVE_VERDICTS: frozenset[str] = frozenset({
    "mild_hallucination", "systematic", "misleading",
})


def _capping_only(obs: Mapping[str, Any] | None) -> bool:
    """这份观察的**唯一**不充分项是「交互元素被裁剪」吗？

    即：无采集降级、未截断、body 与保留元素非空、仅 ``elements_total``
    超出保留条数。INC42 前这类观察与降级观察一起被 fail-closed——
    实测 D-9 judge 有效覆盖仅 1/14，最大头正是 80/N 裁剪 + body 完好
    （8/13）。定向放行后的兜底见 :data:`_CAPPING_SENSITIVE_VERDICTS`。
    """
    if not obs:
        return False
    if list(obs.get("degraded") or ()):
        return False
    if _truncated_flag(obs.get("truncated")):
        return False
    elements = list(obs.get("interactive_elements") or ())
    total = int(obs.get("elements_total") or 0) or len(elements)
    if total <= len(elements):
        return False
    if not str(obs.get("body_text") or "").strip() and not elements:
        return False
    return True


# ── 触发条件 ──────────────────────────────────────────────────────


def _should_trigger_llm(sample: Any,
                         gate_result: Any) -> tuple[bool, str]:
    """LLM 复检是否触发——成本控制的核心判定。

    Args:
        sample: P2 样本。
        gate_result: 闸门详细结果（``GateResult`` / Mapping 都吃）；为 ``None``
            时回落 ``sample.gate``（仅 ``status`` 可用）。

    Returns:
        ``(should, reason)`` —— ``reason`` 是为什么触发 / 不触发的原文。
    """
    if gate_result is not None:
        status = _get(gate_result, "status", "not_run")
        if status == "not_run":
            return False, "gate_result.status == not_run → 不走 LLM（rationale 为空或闸门未跑）"
        if status == "failed":
            n_hard = len([f for f in (_get(gate_result, "findings", ()) or ())
                          if _get(f, "severity", "") == "hard"])
            return True, (f"gate_result.status == failed（{n_hard} 条 hard 发现）"
                          "→ 强制走 LLM")
        und = list(_get(gate_result, "undeterminable", ()) or ())
        if und:
            return True, (f"gate_result.status == passed + {len(und)} 项 "
                          "undeterminable → 走 LLM（不能全判的抓出）")
        return False, ("gate_result.status == passed + undeterminable 空"
                        " → 不走 LLM（成本控制，3 查全过无需 LLM 复检）")

    # gate_result is None → 回落 sample.gate（仅 status 可用，无 undeterminable）
    gate_mark = _gate_mark(sample)
    if gate_mark is None:
        return False, "未提供 gate_result 且 sample.gate 缺失 → 不走 LLM"
    if gate_mark.status == "failed":
        return True, "sample.gate.status == failed（无详细 gate_result）→ 触发"
    if gate_mark.status == "not_run":
        return False, "sample.gate.status == not_run → 不走 LLM"
    # passed with no detail → 当作 passed + undeterminable 空
    return False, ("sample.gate.status == passed（无详细 gate_result）"
                    " → 不走 LLM（成本控制）")


# ── prompt ────────────────────────────────────────────────────────


#: rationale 单条文本裁剪上限。rationale 平均 800-1500 token，
#: 大样本可能更短——保留 1200 字符足以覆盖典型场景。
D9_RATIONALE_CHARS = 1200

#: D-9 few-shot 示例池（INC40，2026-10-15 接入 v1 · 6 条）。
#: **不要**从 ``ai-assisted-*`` 桶挑——违反 D-9 边界③
#: （详见 [10-judge接入方案.md](../../../docs/设计方案/10-judge接入方案.md)
#: §七 已知后续路径 #2）。v1 全部取自 manual-review 桶覆盖的存档。
#: 例子形态 ``{"verdict": "anchored|vague|...", "rationale": "...", "observation": "..."}``。
#: ``score_d9(few_shot_examples=None)`` 时回落本常量；显式传 ``[]`` 强制为空。
#:
#: ⚠️ **INC46（2026-10-15）整体清空，judge 零样本出分。**
#:
#: 清空理由不是「示例质量差」，而是**口径失效**：这六例的 rationale 全是
#: **事后反思**口径（写于动作执行之后、描述刚看到的页面），而存量
#: rationale 重生成后全部变成**动作前动机**——拿旧口径的示例去校新口径的
#: 判断，等于教 judge 把「描述了还没看到的页面」认成正常形态，
#: 那正是 INC46 要修的东西。
#:
#: 留空常量而不是删掉常量：调用方与测试都以它为「默认池」，
#: 删掉会让 ``score_d9(few_shot_examples=None)`` 这一路失去定义。
#: 重选纪律不变且更严——**待存量重生成后，从 manual-review 桶人工重选**
#: （铁律：不挑 ``ai-assisted-*`` 桶，见 D-9 边界③）。
#:
#: 零样本期间 ``score_d9`` 在 verdict reason 里标注 ``fewshot=none``，
#: 报表侧据此知道这批分数与带示例的批次**不可直接比较**。
#:
#: 以下为 v1 六例的选条纪律存档（供重选时比照，勿直接复用）：
#:
#: - anchored ×4：不同形态各一——导航站（E001 hao123，金标 not_play_site）、
#:   404 失败页（T025 kmslxsh）、VIP 付费页（TPIN02 优酷，"正版"锚在
#:   page_title）、选集候选页（T001 央视网）。四条的每个关键断言都能在
#:   body / page_title / video_tag_count / iframe_count / 元素裁剪比里找到锚。
#: - vague ×1（E005 腾讯专题页，金标 no_play_control）：「可能是有效资源
#:   入口」一处措辞含糊（"有效"无判据），其余陈述有锚、无事实错误。
#: - mild_hallucination ×1（E014 腾讯，金标 subscription_wall）：「时长约
#:   1小时49分」在观察全文 / interactive_elements / links 里**均无锚**
#:   （LLM 用片长常识补位）；不影响动作——结论仍是"无法确认画质"。
#:
#: **未覆盖 verdict**：hindsight / systematic / misleading——151 对
#: manual-review 素材里无干净实例（rationale writer 的确定性优先纪律使然）。
#: INC41（2026-10-15，owner 解除 14 天间隔后提前重测）双路复核仍无：
#: judge 两轮（A/B 稳定）仅 E005__b8513a71 两步判 mild_hallucination
#: （"8期链接"无锚 vs 页面"全6集"，同型已覆盖）；人工核实 15 对
#: retest 工作表 + E023 v.qq visit 抽查，关键断言均有锚。三类素材缺口
#: 需 rationale writer 变体实验或新人工复核批次——不合成假例。
#: observation 字段为存档 verbatim 摘录（省略处以 ``…`` 标记），与
#: 评判器实际可见面（url + body 摘录 + 闸门 findings 携带的元数据）对齐。
D9_FEW_SHOT_EXAMPLES: tuple[dict, ...] = (
    # INC46：整体清空（理由见上方常量注）。六例原内容已移除，
    # 待存量 rationale 重生成后从 manual-review 桶人工重选
    # ——铁律：不挑 ai-assisted-* 桶（D-9 边界③）。
)

#: 给 LLM 的系统提示。中文 + JSON 契约 + 6 类枚举 + 三条纪律。
#: **字段顺序按字母**：evidence → verdict——与 schema_parse.ALIASES 表
#: 的键对齐。
D9_SYSTEM_PROMPT: str = (
    "你是一名思考真实性复检员。你的任务：**仅依据下面提供的观察存档 + "
    "rationale 原文 + 闸门 findings**，判断 rationale 是否是『**动作前动机的"
    "忠实翻译**』——它只该依据**此前**看到的观察与任务要求，不能描述或"
    "评价本步尚未执行的结果。\n\n"
    "INC46 口径（2026-10-15）：每条 rationale 附带的『可见观察』是**该步执行"
    "之前**能看到的东西（第 i 步 → 前 i 条观察）；第 0 步没有可见观察，"
    "它的合法依据只有任务与动作本身。\n"
    "于是：引用了本步或后续步才出现的实体 = **后见之明**（hindsight）；"
    "描述了本步动作的结果 = **后见之明**。\n\n"
    "## verdict（6 选 1，必填）\n"
    "- ``anchored``：全部锚定；不确定处如实表达\n"
    "- ``vague``：1 处措辞含糊，无事实错误\n"
    "- ``mild_hallucination``：1 处轻微幻觉（不影响动作）\n"
    "- ``hindsight``：1 处后见之明（措辞像「事后才知道」）\n"
    "- ``systematic``：系统性不符或套话替代推理\n"
    "- ``misleading``：思考编造关键事实并驱动错误动作（与 D-2 联动）\n\n"
    "## 输出契约（严格 JSON，无前后文字、无 markdown 代码块）\n"
    "```\n"
    '{"evidence": "依据引用 rationale 原文 + 观察的具体短语",'
    ' "verdict": "anchored|vague|mild_hallucination|hindsight|systematic|misleading",'
    ' "confidence": 0.0-1.0'
    "}\n"
    "```\n\n"
    "## 纪律\n"
    "1. ``evidence`` 必含**rationale 原文中的具体短语** + 观察对照（不是空泛描述）。\n"
    "2. 闸门已查过的硬事实（实体 / 因果 / 时序）不算你的工作；你的工作是**软层面**——"
    "措辞是否含糊、是否轻微幻觉、是否后见之明。\n"
    "3. **不要输出**任何超出该契约的字段。"

    "\n\n## few-shot examples 会在下面追加（INC46 起默认为空）\n"
    "（系统提示词到此为止；以下 few-shot 与样本数据走**用户提示词**，由"
    " :func:`_render_user` / :func:`score_d9` 注入——见模块 docstring 与"
    " 增量三十八章节联动事项。INC46 起示例池为空，**零样本出分**。）"
)


def _render_action(idx: int, action: Mapping[str, Any]) -> str:
    tool = str(action.get("tool") or "")
    params = action.get("params") or {}
    if not isinstance(params, Mapping):
        params = {}
    target = action.get("target") or {}
    if not isinstance(target, Mapping):
        target = {}
    return (f"[step {idx}] action tool={tool} params={dict(params)} "
            f"target={dict(target)}")


def _render_observation(idx: int, obs: Mapping[str, Any]) -> str:
    """观察 → 一行。**可见性口径与闸门对齐**。

    ⚠️ INC46 单档真跑 E005 site-4 发现：原版只渲染 ``url`` + ``body_text``，
    **没有 ``page_title``**——而闸门侧实体核查的 haystack
    （``lexicon.resettable_text``）是 url / page_title / body / 元素 label /
    链接**五项**全并的。于是同一个「当时能看到什么」的问题，闸门与 judge
    拿着两个不同的答案：judge 会把**标题里明写**的东西判成
    「当时看不到」，系统性高估后见之明，进而把好 rationale 判进负样本池。

    实测那一条：judge 说 step0 里没有「【中国好声音越剧特别季】第一期：」，
    而它就在 ``obs[0]['page_title']`` 里。

    不并元素 label 是**成本**取舍：那会让每份观察的输入显著变长，而
    title 是最强的「这页是什么」信号，先补它。
    """
    body = str(obs.get("body_text") or "").strip()
    if len(body) > 400:
        body = body[:400] + "…[已截断]"
    url = str(obs.get("url") or "")
    title = str(obs.get("page_title") or "").strip()
    head = f"[step {idx}] url={url}"
    if title:
        head += f" title={title}"
    return f"{head} body: {body or '(空)'}"


def _render_rationale(idx: int, rat: Mapping[str, Any]) -> str:
    text = str(rat.get("text") or "").strip()
    if len(text) > D9_RATIONALE_CHARS:
        text = text[:D9_RATIONALE_CHARS] + "…[已截断]"
    conclusion = str(rat.get("conclusion") or "")
    return f"[rationale step {idx}] text: {text or '(空)'} conclusion: {conclusion or '(无)'}"


def _render_finding(f: Mapping[str, Any]) -> str:
    kind = str(_get(f, "kind", ""))
    detail = str(_get(f, "detail", ""))
    severity = str(_get(f, "severity", ""))
    return f"  - [{severity or '?'}] {kind}: {detail}"


def _render_few_shot(examples: Sequence[Mapping[str, Any]] | None) -> str:
    """把 few-shot 示例渲染成 prompt 块。空（``None`` / 空序列）→ 空串——
    **整块不出现**，调用方据此决定是否插入 ``\\n`` 分隔。

    每条示例形态（key 可缺省，缺省按空串处理）：

        ``{"verdict": "anchored|vague|...", "rationale": "...", "observation": "..."}``

    与 rubric §D-9 锚点表对齐——``verdict`` 字段不在 JSON 输出契约里，仅
    作示例标签。
    """
    if not examples:
        return ""
    lines = ["## few-shot examples"]
    for i, ex in enumerate(examples):
        verdict = str(ex.get("verdict") or "（未标注 verdict）")
        rationale = str(ex.get("rationale") or "")
        observation = str(ex.get("observation") or "")
        lines.append(f"### 示例 {i + 1}（{verdict}）")
        if observation:
            lines.append(f"- 观察：{observation}")
        if rationale:
            lines.append(f"- Rationale：{rationale}")
        lines.append(f"- 判定：{verdict}")
    return "\n".join(lines)
def _render_visible(i: int, observations: Sequence[Mapping[str, Any]]) -> str:
    """第 ``i`` 步 rationale 的**可见观察**块（INC46 窗口 ``obs[0..i-1]``）。

    动作前动机口径下，第 ``i`` 步写的东西只能依据**前 i 条**观察：
    第 ``i`` 条是本步动作执行**之后**才回来的，把它算进「可见」就等于
    允许后见之明——那正是 INC46 要判的东西。

    ``i == 0`` 时窗口为空，此时**显式渲染**「（无前置观察，只有任务与
    动作）」而不是留白：留白会让 judge 以为「观察块丢了」，
    从而把首步一律判成「无锚」（vague / mild_hallucination），
    而首步动机的合法依据本就只有任务与动作。
    """
    window = list(observations[:i])
    if not window:
        return "（无前置观察，只有任务与动作）"
    return "\n".join(_render_observation(j, o) for j, o in enumerate(window))


def _render_user(observations: list[Mapping[str, Any]],
                 rationales: list[Mapping[str, Any]],
                 actions: list[Mapping[str, Any]],
                 gate_result: Any,
                 *,
                 few_shot_examples: Sequence[Mapping[str, Any]] | None = None) -> str:
    """渲染 D-9 用户提示词。

    顺序（每个块按需追加，互不重叠）：

      1. ``## 动作序列`` —— LLM 上下文
      2. ``## 观察存档`` —— LLM 可读到的观察全文（**全序列**，供对照）
      3. ``## 闸门 findings`` + ``## 闸门 undeterminable`` —— 系统已知问题
      4. ``## few-shot examples`` —— **仅当** ``few_shot_examples`` 非空
         时插入；位置在闸门 findings 之后、Rationale 原文之前
      5. ``## Rationale 原文`` —— LLM 要判的对象（必须放最后，焦点），
         **逐条附上该步的可见观察窗口**（:func:`_render_visible`）

    few-shot 插在闸门 findings 之后 / rationale 原文之前的设计动机是：
    「先给 LLM 看系统视角 + 教它格式，最后才看要判的对象」。

    ⚠️ 为什么第 2 块仍是**全序列**：judge 要判「这个实体是不是在后续步
    才出现」，它必须看得到后续步。窗口对位是**逐条**给的（第 5 块），
    而全序列给的是**检索用的底稿**——两者用途不同，缺了后者 judge
    就只能看到窗口内的东西，反而判不出「引用了窗口外」。
    """
    parts: list[str] = []
    if actions:
        parts.append("## 动作序列")
        parts.extend(_render_action(i, a) for i, a in enumerate(actions))
    if observations:
        parts.append("\n## 观察存档")
        parts.extend(_render_observation(i, o) for i, o in enumerate(observations))
    if gate_result is not None:
        findings = list(_get(gate_result, "findings", ()) or ())
        und = list(_get(gate_result, "undeterminable", ()) or ())
        parts.append("\n## 闸门 findings")
        if findings:
            parts.extend(_render_finding(f) for f in findings)
        else:
            parts.append("  (无 hard findings)")
        if und:
            parts.append("\n## 闸门 undeterminable")
            parts.extend(f"  - {u}" for u in und)
    # few-shot 块——「闸门 findings 之后、Rationale 原文之前」。
    # 空时不插入（含 ``None`` / 空序列）；``Sequence`` 兼容 tuple + list。
    shot_block = _render_few_shot(few_shot_examples)
    if shot_block:
        parts.append("\n" + shot_block)
    if rationales:
        parts.append("\n## Rationale 原文")
        for i, r in enumerate(rationales):
            # 逐条带上该步的**可见观察窗口**（obs[0..i-1]）——
            # 锚定关系必须在同一块里，不然 judge 要自己数下标，
            # 而数错一格就是把后见之明判成 anchored。
            parts.append(f"\n### step {i} 可见观察（本步执行前）")
            parts.append(_render_visible(i, observations))
            parts.append(_render_rationale(i, r))
    return "\n".join(parts) or "(无内容)"


# ── 解析 ──────────────────────────────────────────────────────────


def _parse_response(raw: str | None) -> tuple[dict[str, Any] | None, str]:
    """从 LLM 原文抠 JSON。**入口已剥 think**（``schema_parse`` 内部）。"""
    if not raw:
        return None, "LLM 返回空"
    data, _repairs = extract_json_reporting(raw)
    if data is None:
        return None, "parse_fail"
    return data, ""


# ── 打分 ──────────────────────────────────────────────────────────


#: verdict → rubric 锚点档。
#: mild_hallucination 与 hindsight 都是 3 档（rubric §D-9 原文把它们并列）。
_VERDICT_TO_SCORE: Mapping[str, int] = {
    "anchored": 5,
    "vague": 4,
    "mild_hallucination": 3,
    "hindsight": 3,
    "systematic": 2,
    "misleading": 1,
}


def score_d9(sample: Sample | Any, *,
             gate_result: Any = None,
             llm_client: LLMClient | None = None,
             few_shot_examples: Sequence[Mapping[str, Any]] | None = None) -> D9Verdict:
    """D-9 打分。**永不抛**——LLM 失败 / 解析失败 / 观察不充分 / 成本控制跳过，
    全部走对应分支。

    Args:
        sample: P2 样本（``Sample`` 对象 / Mapping 都吃，与 d3/d4 同款纪律）。
        gate_result: 闸门详细结果（``GateResult`` / Mapping / ``None``）。
            为 ``None`` 时回落 ``sample.gate``（仅 ``status`` 可用）。
            **plan §三·第三步风险**：rationale 写入与复检耦合，复检滞后一
            个批次——第一版只对带 rationale 的样本打分，老批次 ``gate=not_run``
            不判。
        llm_client: LLM 后端；为 ``None`` 时若触发条件成立，返回 ``undetermined``
            并写明「未提供 LLM client」。
        few_shot_examples: D-9 few-shot 示例池。``None``（默认）→ 回落
            :data:`D9_FEW_SHOT_EXAMPLES` 模块常量；显式传 ``[]`` 强制不插
            few-shot 块；非空序列则按 :func:`_render_few_shot` 渲染。
            **不要**从 ``ai-assisted-*`` 桶挑——违反 D-9 边界③
            （[10-judge接入方案.md §七 #2](../../../docs/设计方案/10-judge接入方案.md)）。
    """
    observations = _observations(sample)
    rationales = _rationales(sample)
    actions = _actions(sample)

    # few-shot 回落模块常量——``None`` = 用常量；``[]`` / 非空 = 用调用方值
    if few_shot_examples is None:
        few_shot_examples = D9_FEW_SHOT_EXAMPLES

    # 闸门触发条件——成本控制的核心
    should, why_trigger = _should_trigger_llm(sample, gate_result)
    if not should:
        return D9Verdict(
            score=None, kind="not_evaluated",
            reasons=(why_trigger,),
        )

    # 观察充分性——INC42 起两档处理（原版一律 fail-closed，实测 judge
    # 有效覆盖仅 1/14，最大头是 80/N 元素裁剪 + body 完好）：
    #   - hard（降级 / 截断 / 全空 / 没有观察）→ 仍 fail-closed
    #     （§5.5 最贵教训：采集失败与「真没有」在存档里同形）；
    #   - capping-only（仅元素被裁剪）→ 放行 LLM，judge 结果按
    #     _CAPPING_SENSITIVE_VERDICTS 方向性兜底（见下）。
    insufficient = _all_observations_insufficient(observations)
    capped = any(_capping_only(o) for o in observations)
    if insufficient and not capped:
        return D9Verdict(
            score=None, kind="undetermined",
            reasons=(f"观察不充分（{len(insufficient)} 份都判不了）："
                     + "；".join(insufficient),),
        )

    if llm_client is None:
        return D9Verdict(
            score=None, kind="undetermined",
            reasons=("闸门触发 LLM 复检但未提供 LLM client——本条不出分",),
        )

    user_prompt = _render_user(
        observations, rationales, actions, gate_result,
        few_shot_examples=few_shot_examples,
    )
    raw = llm_client.chat(D9_SYSTEM_PROMPT, user_prompt)
    data, why = _parse_response(raw)
    if data is None:
        # D-9 失败 ≠ 1 档——rubric 锚点 1「misleading」是 LLM 真实判断
        # 出来的，不是 fail-closed。
        return D9Verdict(
            score=None, kind="undetermined",
            confidence=CONFIDENCE_NONE,
            reasons=(f"闸门触发但 LLM 未判（{why}）→ undetermined，不走 1 档"
                     "（1 档是 LLM 真判 misleading，不是 fail-closed）",),
        )

    verdict = as_str(field(data, "verdict", ""))
    evidence = as_str(field(data, "evidence", ""))
    if verdict not in D9_VERDICTS:
        return D9Verdict(
            score=None, kind="undetermined",
            confidence=CONFIDENCE_NONE,
            evidence=evidence,
            reasons=(f"verdict {verdict!r} 不在契约 6 类 "
                     f"{sorted(D9_VERDICTS)} → undetermined",),
        )

    score = _VERDICT_TO_SCORE[verdict]

    # INC42 方向性兜底：裁剪观察上的 missing-anchor 类 verdict 不可信
    # ——「没找到锚」可能是「锚被裁掉」。降为 undetermined（fail-closed），
    # judge 的原始 verdict / evidence 留在字段里供审计。
    if capped and verdict in _CAPPING_SENSITIVE_VERDICTS:
        return D9Verdict(
            score=None, kind="undetermined",
            verdict=None, evidence=evidence,
            confidence=CONFIDENCE_NONE,
            reasons=(f"judge 判 {verdict}，但观察存在交互元素裁剪——"
                     "锚点缺失无法与「被裁剪」区分 → fail-closed（INC42）；"
                     f"judge 原始证据：{evidence[:200]}",),
        )

    # INC46：零样本期间把「没给示例」写进 reason。
    # 这不是记账癖——带示例与不带示例的分数**不可直接比较**，
    # 而报表上两者长得一模一样（都是 ``verdict=anchored → 锚点 5``）。
    # 不标注的话，重生成前后两批分数的差异会被读成「素材质量变了」。
    fewshot_note = "" if few_shot_examples else " fewshot=none"
    return D9Verdict(
        score=score, kind="scored", verdict=verdict, evidence=evidence,
        confidence=CONFIDENCE_JUDGED,
        reasons=(f"verdict={verdict} → rubric §D-9 锚点 {score}{fewshot_note}",),
    )


__all__ = [
    "D9_VERDICTS", "_VERDICT_TO_SCORE",
    "CONFIDENCE_JUDGED", "CONFIDENCE_NONE",
    "VerdictKind", "D9Verdict", "score_d9",
    "D9_SYSTEM_PROMPT", "D9_RATIONALE_CHARS", "D9_FEW_SHOT_EXAMPLES",
]
