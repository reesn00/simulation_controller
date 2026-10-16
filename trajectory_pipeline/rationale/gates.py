"""一致性闸门三查——模块 4 的**纯代码**部分。

设计方案的分工：实体核查**纯代码零成本、100% 覆盖**，另两查是规则近似。
所以本模块**不调 LLM**，它是闸门的全部确定性内核。

三查
====

==========  ==================================  ==========================
查          判什么                              判不出的
==========  ==================================  ==========================
实体核查    rationale 引用的实体能在**前置步**    「没引用实体」不等于
            的观察里检索到                       「没有幻觉」
因果核查    rationale 说的动作 == 本步实际动作    理由是否**充分**支持它
时序核查    rationale 引用的实体**不在后续步**才   措辞上的后见之明
            出现（那是「事后才知道」的铁证）      （词典层只给辅助信号）
==========  ==================================  ==========================

⚠️ INC46 窗口语义（2026-10-15）
===============================

rationale 改成**动作前动机**之后，「本步观察」从「当时可见」挪到了
「后见」——第 ``i`` 步写下的动机**不可能**引用 ``obs[i]``，
引用了就是铁证级的后见之明。

于是三查的可见窗口统一是 **``[:step_index]``（不含本步）**：

- 实体核查：可见 = ``observations[:step_index]``；
- 时序核查：「当时看不到」= ``observations[step_index:]``（**含本步**）。

**step 0 的窗口是空的**，这是全链路最容易误伤的位置：首步动机的合法
依据只有 ``user_prompt`` / ``target_title``（豁免域），而空窗口若走
「没有任何观察 → 查不到 → 判幻觉」那条隐式路径，会把每一条 goto 首步
判成幻觉。所以空窗口**显式早退**返回 ``undeterminable``——
「首步无历史观察，核查不适用」。


最重要的一条：截断时**不得判幻觉**
================================

rubric D-9 边界规则：「观察被截断时，**落在截断外的实体不得判成幻觉**——
判『不可判定』」。这与 D-2 的 ``undetermined``、D-6 的缺件不可判定
是同一条纪律在 rationale 侧的形态。

它有一个**很凶的失败形态**：obscura 的 browser_snapshot 的 ``max_chars``
预算会被 CSS 抢光（实测 baidu 首页 6000 字符预算里**正文字数为 0**，
而 CSS 吃掉 16675 字符）。于是截断信号与「采集失败」信号**同形**——
而 LLM 看到空的观察会讲一个**通顺的错故事**（§5.5 实测：iqiyi 播放页
``video=1`` 被判「否」，模型的分析是「正文为空、元素为空，说明未渲染」）。

所以本模块的口径是：**观察不充分时不判幻觉，只记「查不了」**。
理由说得越确定、证据越薄，这一维的分数越低。

处置：三值
----------

``GateStatus`` 与 :class:`trajectory_pipeline.assembler.schema.GateMark`
同款——**「没跑」不是「通过」**。设计决策 D3 明确：闸门不过**没有**合成后
兜底路径，样本带 ``gate_failed`` 标记落负样本池与人工抽检队列，
**而不是被悄悄修好**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from trajectory_pipeline.rationale import lexicon

#: 三值。**不是两值**——同 ``assembler.schema.GateMark``。
GateStatus = Literal["passed", "failed", "not_run"]

#: 单条发现。
FindingKind = Literal[
    "unsupported_entity",   # 实体不在观察内（前置步）
    "action_mismatch",      # 说的动作 ≠ 实际动作
    "temporal_leak",        # 引用了只在后续步出现的实体
    "hindsight",            # 措辞上的后见之明（辅助信号）
    "boilerplate_only",     # 通篇套话，没有实质推理
]

#: 动作名集合。用于因果核查的「提到了别的动作」判断。
#: **刻意不覆盖全部动作语义**——判断点 ①②③④ 与 ``goto``/``click``/
#: ``new_tab`` 不是一一对应（①② 发生在 goto 之后、③ 驱动 click），
#: 所以因果核查**只在 rationale 明确提到别的动作名时报**，
#: 不要求「必须提到当前动作」。
_ACTION_NAMES: tuple[str, ...] = ("goto", "click", "new_tab")


@dataclass(frozen=True, slots=True)
class Finding:
    """一条闸门发现。

    ``severity`` 分两级：``hard`` 会让闸门 fail，``soft`` 只记录。
    分两级是因为有些「发现」是**提示**而不是缺陷——
    比如「通篇套话」在一条只有一步的样本上是正常的。
    """

    kind: FindingKind
    detail: str
    severity: Literal["hard", "soft"] = "hard"

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "severity": self.severity}


@dataclass(frozen=True, slots=True)
class GateResult:
    """闸门对**一条 rationale** 的判定。"""

    status: GateStatus
    findings: tuple[Finding, ...] = ()
    #: **可判定性**——与 :attr:`status` 分开。
    #: ``status="passed"`` 且 ``undeterminable=True`` 意味着
    #: 「查过的都过了」，而 ``undeterminable=False`` 才意味着「全查过了」。
    #: 两者混成一个字段就是 §3.1 的老毛病（观察与判定自相矛盾却照样给结论）。
    undeterminable: tuple[str, ...] = ()
    checked_entities: int = 0

    @property
    def hard_findings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "hard")

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "undeterminable": list(self.undeterminable),
                "checked_entities": self.checked_entities,
                "findings": [f.to_json() for f in self.findings]}


# ── 可判定性 ──────────────────────────────────────────────────────


def _truncated_flag(value: Any) -> bool:
    """``truncated`` 字段统一判真——兼容 p1 bool 与 p2 dict 两种形态
    （INC42；与 evaluation 侧各实现同款但独立，靠 contract 盯守）。
    dict（``{'source': False, 'view': False}``）按 ``any(values)`` 判。"""
    if isinstance(value, Mapping):
        return any(bool(v) for v in value.values())
    return bool(value)


def observation_shortfall(obs: Mapping[str, Any] | None) -> str:
    """这份观察**能不能支撑**「某实体不存在/存在」的结论。空串 = 能。

    刻意与 :mod:`trajectory_pipeline.evaluation.scorers.code_scorer`
    的 ``_observation_shortfall`` **同款判据但分开实现**——
    rationale 不 import evaluation（依赖方向），而两处口径靠
    ``tests/contract/`` 对齐。

    这一层是 §5.5 那条最贵的教训的落点：iqiyi 播放页 ``video=1``
    被判「否」，模型把**采集失败**讲成了通顺的错结论。观察不充分时
    判「查不了」，那条路就堵死了。
    """
    if not obs:
        return "没有观察"
    if list(obs.get("degraded") or ()):
        return f"观察有采集降级项 {list(obs['degraded'])}"
    if _truncated_flag(obs.get("truncated")):
        return ("观察被上游 max_chars 截断——实测 CSS 会抢光预算，"
                "实体可能落在截断外")
    elements = list(obs.get("interactive_elements") or ())
    total = int(obs.get("elements_total") or 0) or len(elements)
    if total > len(elements):
        return (f"交互元素被裁剪（保留 {len(elements)} / 采到 {total}），"
                f"实体可能落在裁剪区外")
    if not str(obs.get("body_text") or "").strip() and not elements:
        return "正文与交互元素皆空，采集可能整体失败"
    return ""


# ── 查一：实体核查 ────────────────────────────────────────────────


def _bag(observations: Sequence[Mapping[str, Any]]) -> str:
    """观察序列 → 可检索袋。**前置步**（INC46：不含本步）。"""
    return "\n".join(lexicon.resettable_text(o) for o in observations)


def _in_haystack(text: str, haystack_l: str) -> bool:
    """实体 ``text`` 在已归一化的 haystack 里吗。

    **整串命中**，或**合成实体拆开后每段都命中**
    （见 :func:`~trajectory_pipeline.rationale.lexicon.split_composite`）。

    合成命中要求**每段都在**：模型把观察到的两处合并写成「A/B」时，
    A 与 B 都得真实出现过才算有据；少一段就落回整串匹配的结果
    （不命中）——所以这条是**收紧的补充**，不是放宽。
    """
    if (text or "").lower() in haystack_l:
        return True
    parts = lexicon.split_composite(text)
    if not parts:
        return False
    return all(p.lower() in haystack_l for p in parts)


def check_entities(
    text: str,
    observations: Sequence[Mapping[str, Any]],
    *,
    exempt: str = "",
    later_observations: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[Finding], int, list[str]]:
    """实体核查。

    Args:
        text: rationale 原文。
        observations: **前置步**的观察（按时间序，INC46 起**不含本步**）。
        exempt: 任务给的豁免文本（片名 / 用户诉求）。rationale 引用
            这里的词**不是幻觉**——它来自任务，不是模型看来的。
        later_observations: **后续步**观察。只用于一个判断：
            某个实体在后续步的**不完整**观察里出现过 → 它当时**可能**
            就在页面上而只是没采到，所以本步查不到它**不能判幻觉**。

    Returns:
        ``(findings, checked_count, undeterminable)``。

    ⚠️ **空窗口显式早退**：``observations`` 为空（首步）时**不判任何幻觉**，
    直接返回 undeterminable「首步无历史观察，核查不适用」。这不是可选项——
    动作前动机口径下首步的合法依据只有任务给的豁免文本，走「没有观察
    → 什么都查不到 → 判幻觉」那条隐式路径会把每一条 goto 首步判死。
    """
    findings: list[Finding] = []
    undeterminable: list[str] = []

    # 空窗口**先于**实体抽取早退：它是**这一步**的属性（首步没有历史），
    # 与这段文本里有没有可核实体无关。放在实体抽取之后的话，
    # 不引实体的首步动机就拿不到这条说明，而下游据此区分
    # 「查不了」与「没得查」——两者在 undeterminable 里同形就分不出。
    if not observations:
        return findings, 0, ["首步无历史观察，实体核查不适用"]

    entities = lexicon.filter_entities(lexicon.extract_entities(text))
    if not entities:
        # 没有可核实体 ≠ 没有幻觉。这一条不判 fail——
        # 但若整段是套话，交给 :func:`check_boilerplate` 处理。
        return findings, 0, undeterminable

    # 前置步任一份观察不充分 → 该实体不可判定。
    # **不是「全部不充分才算」**：只要有一份完整的就能定案，
    # 而一份都不完整时**不能**判幻觉（rubric D-9 边界规则）。
    complete_bag = ""
    for obs in observations:
        if not observation_shortfall(obs):
            complete_bag += lexicon.resettable_text(obs) + "\n"
    if not complete_bag.strip():
        reason = "；".join(
            f"第{i + 1}份观察：{observation_shortfall(o)}"
            for i, o in enumerate(observations)) or "没有可核的观察"
        undeterminable.append(f"观察不充分，实体核查无法判定（{reason}）")

    # 后续步的**不完整**观察：命中它说明「当时可能就在而没采到」。
    # 判幻觉是**判反**（把懒加载/渲染时序造成的漏采记成模型幻觉），
    # 而「放过一条幻觉」的代价只是少一条训练信号。
    shaky_later = "\n".join(
        lexicon.resettable_text(o) for o in later_observations
        if observation_shortfall(o)
    )
    shaky_later_l = lexicon.strip_noise(shaky_later).lower()

    haystack = lexicon.strip_noise(complete_bag)
    haystack += "\n" + lexicon.strip_noise(exempt or "")
    haystack_l = haystack.lower()

    checked = 0
    for ent in entities:
        needle = ent.text.lower()
        # URL 按「去 scheme / 去 www」再比一次——真实文本里写
        # ``iqiyi.com`` 而观察里存的是 ``https://www.iqiyi.com/...``
        # 是常态，按全串比会把它们判成幻觉。
        variants = {needle}
        if ent.kind == "url":
            bare = re.sub(r"^https?://", "", needle).removeprefix("www.")
            variants.add(bare)
        checked += 1
        if any(v in haystack_l or v in haystack for v in variants):
            continue
        # 合成实体（「A/B」）：整串落空但每段都真实出现过 → 有据，不算幻觉。
        if _in_haystack(ent.text, haystack_l):
            continue
        if undeterminable:
            # 观察不充分时不判幻觉（rubric D-9 边界规则）。
            continue
        if any(v in shaky_later_l for v in variants):
            # 后续步的**不完整**观察里出现过 → 它当时**可能**就在页面上，
            # 只是没被采到（懒加载/渲染时序）。判幻觉是把**采集器的漏采**
            # 记成**模型的幻觉**——与 §3.1「观察与判定自相矛盾时一律
            # 降级」同款。只记不可判定。
            undeterminable.append(
                f"实体「{ent.text}」在后续步的不完整观察里出现过，"
                f"本步查不到它——可能是当时就在而没采到（懒加载/渲染时序），"
                f"不判幻觉")
            continue
        findings.append(Finding(
            "unsupported_entity",
            f"实体「{ent.text}」不在前置步的观察里，也不在任务给的"
            f"片名/诉求里，更不在后续步的任何观察里——按 I1 与 D-9 判为幻觉候选",
        ))
    return findings, checked, undeterminable


# ── 查二：因果核查 ────────────────────────────────────────────────


def check_causality(
    text: str,
    action_tool: str,
) -> list[Finding]:
    """因果核查：rationale 说做了别的动作吗。

    **只查「提到了别的动作」，不查「必须提到当前动作」**——
    thinking 里不提动作名是完全正常的写法（「正文写着加载失败」
    就是一条好 thinking），要求必须提到会把正常写法判成错。
    """
    if not text or not action_tool:
        return []
    norm = text.lower()
    mentioned = [a for a in _ACTION_NAMES if a != action_tool and a in norm]
    if not mentioned:
        return []
    return [Finding(
        "action_mismatch",
        f"本步实际动作是 {action_tool}，而 thinking 提到了 "
        f"{mentioned}——说做的与实际做的不一致",
    )]


# ── 查三：时序核查 ────────────────────────────────────────────────


def check_temporal_at(
    text: str,
    observations: Sequence[Mapping[str, Any]],
    step_index: int,
    *,
    exempt: str = "",
) -> tuple[list[Finding], list[str]]:
    """时序核查（给定本步下标）。

    这是**最强的后见之明信号**，纯代码、可确证——
    词典只能判「措辞上像事后口吻」，而这里判的是「引用了当时看不到的东西」。

    ``observations`` 是**全序列**，``step_index`` 是本步下标。

    ⚠️ INC46 窗口（动作前动机口径）：

    - 「当时可见」= ``[:step_index]``——**不含本步**。本步观察是这一动作
      执行**之后**才有的，引用它就是后见之明；
    - 「当时看不到」= ``[step_index:]``——**含本步**。

    **step 0 的可见窗口为空**，此时「引用了当时看不到的东西」这个判断
    只能靠任务豁免文本兜底：命中豁免域（片名 / 用户诉求）的不算泄漏，
    其余一律判 temporal_leak——首步动机引用任何具体页面实体都没有来源
    （还没有任何观察），那确实是幻觉。
    """
    findings: list[Finding] = []
    undeterminable: list[str] = []

    visible = list(observations[:step_index])
    later = list(observations[step_index:])

    if not later:
        # 没有后续步（含本步）就没有「后见之明」这个概念——
        # **不是通过，是不适用**。
        return findings, ["本样本无后续步，时序核查不适用"]

    entities = lexicon.filter_entities(lexicon.extract_entities(text))
    if not entities:
        return findings, undeterminable

    # 后续步观察不完整时，不能说「这个实体当时看不到」——
    # 它可能当时就在，只是没被采到。
    later_complete = [o for o in later if not observation_shortfall(o)]
    if not later_complete:
        undeterminable.append(
            "后续步观察不充分，无法判定「当时看不到」——"
            "实体可能当时就在页面里而只是没采到")

    visible_l = lexicon.strip_noise(_bag(visible) + "\n" + (exempt or "")).lower()
    later_l = lexicon.strip_noise(_bag(later_complete)).lower()

    for ent in entities:
        needle = ent.text.lower()
        if _in_haystack(needle, visible_l):
            continue                     # 当时就看得见，不是后见之明
        if not _in_haystack(needle, later_l):
            continue                     # 前后都没有 → 实体核查管它
        findings.append(Finding(
            "temporal_leak",
            f"实体「{ent.text}」只在**后续步（含本步）**的观察里出现，"
            f"前置步没有——thinking 引用了当时看不到的东西"
            f"（rubric D-9 锚点 3：1 处后见之明）",
        ))
    return findings, undeterminable


# ── 套话层（软信号）────────────────────────────────────────────────


def check_boilerplate(text: str) -> list[Finding]:
    """通篇套话 → **soft** 发现。

    soft 的理由：一条只有一步、且那一步什么都没发生的样本，
    thinking 里全是「无法确认」是完全正常的。硬判它 fail
    会把一批好样本判死。
    """
    if not text or not lexicon.strip_noise(text):
        return []
    entities = lexicon.extract_entities(text)
    concrete = [e for e in entities if not lexicon.is_boilerplate(e.text)]
    if concrete:
        return []                        # 有具体实体，不是套话
    words = lexicon.strip_noise(text)
    if lexicon.is_boilerplate(words) or len(words) <= 20:
        return [Finding(
            "boilerplate_only",
            "thinking 里没有任何具体实体，整段是套话或过短——"
            "rubric D-9 锚点 2「套话替代推理」",
            severity="soft",
        )]
    return []


def check_hindsight(text: str) -> list[Finding]:
    """措辞上的后见之明 → **soft**。

    最强的判据是 :func:`check_temporal_at`（引用了后续步才出现的实体），
    这里是辅助：只判措辞，不判事实。
    """
    spans = lexicon.hindsight_spans(text)
    if not spans:
        return []
    return [Finding(
        "hindsight",
        f"thinking 含后见之明措辞 {list(spans)}——"
        f"要求写「当时的思考」而不是事后复盘",
        severity="soft",
    )]


# ── 汇总 ──────────────────────────────────────────────────────────


def gate(
    text: str,
    *,
    action_tool: str = "",
    observations: Sequence[Mapping[str, Any]] = (),
    step_index: int = 0,
    exempt: str = "",
) -> GateResult:
    """跑三查 + 两个软信号，汇总成一条闸门判定。

    Args:
        text: rationale 原文。**空串 → ``not_run``**（不是 ``passed``）。
        action_tool: 本步实际动作（因果核查用）。
        observations: **全序列**观察（时序核查需要后续步）。
        step_index: 本步下标。
        exempt: 任务给的豁免文本（片名 / 用户诉求）。

    ⚠️ INC46：可见窗口是 ``[:step_index]``（**不含本步**），
    「后见」窗口是 ``[step_index:]``（**含本步**）。rationale 是动作前
    动机，本步观察此刻还不存在。step 0 的可见窗口为空，实体核查
    显式早退为 undeterminable，不判幻觉。

    ``status`` 的口径：任何一条 **hard** 发现 → ``failed``；
    否则 ``passed``，而 :attr:`GateResult.undeterminable` 非空表示
    「查过的都过了，但有一部分查不了」。
    """
    if not (text or "").strip():
        return GateResult("not_run", (), ("rationale 为空——闸门未运行",), 0)
    findings: list[Finding] = []
    undeterminable: list[str] = []
    checked = 0

    visible = list(observations[:step_index])
    later = list(observations[step_index:])

    ent_findings, checked, ent_und = check_entities(
        text, visible, exempt=exempt, later_observations=later)
    findings += ent_findings
    undeterminable += ent_und

    findings += check_causality(text, action_tool)

    tmp_findings, tmp_und = check_temporal_at(
        text, observations, step_index, exempt=exempt)
    findings += tmp_findings
    undeterminable += tmp_und

    findings += check_boilerplate(text)
    findings += check_hindsight(text)

    hard = [f for f in findings if f.severity == "hard"]
    status: GateStatus = "failed" if hard else "passed"
    return GateResult(status, tuple(findings),
                      tuple(dict.fromkeys(undeterminable)), checked)


__all__ = [
    "GateStatus", "FindingKind", "Finding", "GateResult",
    "observation_shortfall",
    "check_entities", "check_causality", "check_temporal_at",
    "check_boilerplate", "check_hindsight", "gate",
]
