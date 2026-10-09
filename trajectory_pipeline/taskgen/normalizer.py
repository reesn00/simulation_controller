"""判分保真检查——「persona 只改表述不改判分」这条铁律的**唯一执行者**。

为什么必须是代码而不是纪律
--------------------------
改写把「注明可观看的集数范围」悄悄删掉时，**没有任何东西会报错**：
任务还是那个任务，样本站得住，跑得通，只是那条要求消失了。
这类"静默的语义丢失"不可能靠 review 发现——读 300 条样本没人会逐条对照判分。
所以这条铁律必须有可执行的守卫。

处置：不修、不重试、不猜
------------------------
检查不过就**丢弃改写、退回骨架原文**（``initial_request`` 原样）。
理由与 :mod:`trajectory_pipeline.executor.dom` 的剥离逻辑同源：
「猜出来修好了」比「明摆着坏了」贵得多——前者带着通过的假象进训练集，
而后者一看就知道要去看。原始表述永远可用，所以退回没有信息损失。

误报是刻意接受的
----------------
探针按"命中任一别名即通过"设计，宁可放过也要避免把合规改写判死。
但**误报率必须可见**：:func:`probe_report` 把失败原因按类型汇总，
调宽探针因此是一个能看到数字变化的决策，而不是"改到能过为止"的静默降级。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Mapping, Sequence

from trajectory_pipeline.taskgen.persona.lexicon import CONSTRAINT_PROBES
from trajectory_pipeline.taskgen.persona.schema import PersonaProfile
from trajectory_pipeline.taskgen.skeleton import (
    Constraint,
    TaskSkeleton,
    TaskInstance,
)

#: 渲染后的表述短于这个长度就判失败。
#:
#: 门槛定得很低（骨架原句普遍 15–30 字），因为**目的不是判"太短"，
#: 而是把"要求被整段吃掉"的形态暴露出来**——那种情况下剩下的
#: 只有催促句和语气词，长度会掉到个位数。定高了会让真正该拦的漏过去，
#: 而漏过去的代价（一条静默丢判分的训练样本）远高于误报代价。
MIN_PROMPT_CHARS: Final = 8

#: 归一失败的原因分类。**分类是为了让失败分布可读**——
#: 一个笼统的"归一失败"计数会让人看不出是探针太严还是渲染器有 bug。
FAIL_PROBE_MISS: Final = "probe_miss"          # 某条判分要求的探针没命中
FAIL_TOO_SHORT: Final = "too_short"            # 表述被削得只剩语气词
FAIL_EMPTY: Final = "empty"                    # 渲染出空串


@dataclass(frozen=True, slots=True)
class NormalizeResult:
    """归一结论。

    ``accepted=False`` 时 ``prompt_text`` 已退回骨架原文——
    调用方**直接用**这个字段，不需要自己判断该用哪个。
    """

    prompt_text: str
    accepted: bool
    reason: str
    fail_kind: str = ""
    dropped: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)


def check_preserves_criteria(text: str, constraints: Sequence[Constraint]) -> tuple[str, ...]:
    """返回**没被保住**的约束标签。空元组 = 全过。

    命中规则是 OR：某条约束的探针词里**任意一个**出现在表述中即算保住。
    """
    lowered = text.lower()
    lost: list[str] = []
    for con in constraints:
        probes = CONSTRAINT_PROBES.get(con.key)
        if not probes:
            continue
        if not any(p.lower() in lowered for p in probes[1]):
            lost.append(con.label)
    return tuple(lost)


def normalize(
    skeleton: TaskSkeleton,
    persona: PersonaProfile,
    rendered_prompt: str,
    *,
    extra_constraints: Sequence[Constraint] = (),
) -> NormalizeResult:
    """检查渲染结果是否保住判分要求；不过就退回骨架原文。

    ``extra_constraints`` 用于注入 :class:`Constraint` 未覆盖的要求——
    存在意义是**让外部能加严而不必改本模块**。加严是允许的，
    悄悄放松则不行（那会让"检查"退化成恒过的形式）。
    """
    constraints = tuple(skeleton.constraints) + tuple(extra_constraints)
    text = (rendered_prompt or "").strip()

    if not text:
        return NormalizeResult(
            prompt_text=skeleton.initial_request,
            accepted=False, reason="渲染出空串", fail_kind=FAIL_EMPTY,
        )
    if len(text) < MIN_PROMPT_CHARS:
        return NormalizeResult(
            prompt_text=skeleton.initial_request,
            accepted=False,
            reason=f"表述被削到 {len(text)} 字（<{MIN_PROMPT_CHARS}），"
                   f"疑似要求整段丢失",
            fail_kind=FAIL_TOO_SHORT,
        )

    lost = check_preserves_criteria(text, constraints)
    if lost:
        return NormalizeResult(
            prompt_text=skeleton.initial_request,
            accepted=False,
            reason=f"改写丢了判分要求：{list(lost)}",
            fail_kind=FAIL_PROBE_MISS,
            dropped=lost,
            detail={
                "probes": {
                    con.key: CONSTRAINT_PROBES[con.key][1]
                    for con in constraints
                    if con.key in CONSTRAINT_PROBES
                },
                "constraints": [con.label for con in constraints],
            },
        )

    return NormalizeResult(
        prompt_text=text, accepted=True,
        reason="判分要求全部保留",
    )


def probe_report(
    samples: Sequence[tuple[TaskSkeleton, PersonaProfile, str]]
) -> dict[str, Any]:
    """渲染结果的探针体检——**归一前的原始体检**。

    ``samples`` 是 ``(骨架, 画像, 渲染文本)`` 三元组。返回：

    - ``accepted`` / ``total``：能过探针的比例
    - ``by_constraint``：每条判分要求的丢失计数（从宽到严排序）
    - ``samples``：逐条明细，供人工看**丢在哪**

    这个报告的作用是让"探针是否过严"变成一个**可回答的问题**。
    没有它，唯一能看到的现象就是"归一失败率 40%"，而所有人对它的
    第一反应都是把探针调宽——纪律就这样被静默拆掉。
    """
    accepted = 0
    by_constraint: dict[str, int] = {}
    detail_rows: list[dict[str, Any]] = []

    for skeleton, persona, text in samples:
        res = normalize(skeleton, persona, text)
        if res.accepted:
            accepted += 1
        for label in res.dropped:
            by_constraint[label] = by_constraint.get(label, 0) + 1
        if not res.accepted:
            detail_rows.append({
                "task_id": skeleton.task_id,
                "persona_id": persona.persona_id,
                "fail_kind": res.fail_kind,
                "reason": res.reason,
                "text": (text or "")[:120],
                "origin": skeleton.initial_request[:80],
            })

    total = len(samples)
    return {
        "total": total,
        "accepted": accepted,
        "accept_rate": round(accepted / total, 4) if total else None,
        "by_constraint": dict(sorted(by_constraint.items(), key=lambda kv: -kv[1])),
        "samples": detail_rows,
    }


def build_instance(
    skeleton: TaskSkeleton,
    persona: PersonaProfile,
    *,
    rendered_prompt: str,
    search_query: str,
    provenance_extra: Mapping[str, Any] | None = None,
) -> TaskInstance:
    """归一 + 组装 :class:`TaskInstance`。

    无论归一过没过，**产出的实例都可用**：没过的时候 ``prompt_text``
    退回骨架原文、``rewritten=False``。丢掉整个实例是错的——
    骨架原文本身就是合法的任务表述，丢掉等于白采一次画像。
    """
    res = normalize(skeleton, persona, rendered_prompt)
    provenance: dict[str, Any] = {
        **persona.profile_dict(),
        "task_type": skeleton.task_type,
        "dimension": skeleton.dimension,
        # content_tier 是组合层派生的（见 persona/schema.py ①），
        # 落在这里而不是画像上——它描述的是"这条样本"，不是"这个人"。
        "content_tier": persona.content_tier_for(
            skeleton_popularity=skeleton.skeleton_popularity
        ),
        "constraints": [c.label for c in skeleton.constraints],
        "normalize_accepted": res.accepted,
        "normalize_reason": res.reason,
        "normalize_fail_kind": res.fail_kind,
        "rewritten": res.accepted,
    }
    provenance.update(provenance_extra or {})
    return TaskInstance(
        task_id=skeleton.task_id,
        persona_id=persona.persona_id,
        scenario_id=skeleton.scenario_id,
        prompt_text=res.prompt_text,
        search_query=search_query,
        has_standard=persona.has_standard,
        retrieval_mode=skeleton.retrieval_mode,
        title=skeleton.title,
        provenance=provenance,
        rewritten=res.accepted,
    )