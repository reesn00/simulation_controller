"""训练步筛选：死循环步剔出、重复动作标记（方案 23 R3-1 / R3-2）。

**只作用于训练视图，不动 P1/P2 存档。** 证据与语料是两件事：某一步
在存档里原样保留（可回放、可复核），只是**不教给模型**。

两条口径
--------

**R3-1：相邻「同动作 + 观察逐字相同」⇒ 后一步整步剔出。**

这类步是执行器在同一个页面上反复发同一个动作、拿到同一份回执——
训练它等于教模型「没反应就再点一次」。审计实测存量 469 处
（`docs/设计方案/23-...md` §1.5），验收要求训练集里为 0。

比较对象是**渲染后的观察文本**，不是原始 observation dict：审计口径
就是「P3 视图里相邻 (assistant, tool) 逐字比对」，而且模型看到的是
渲染文本——原文不同但渲染后相同（或反之）时，该不该剔取决于模型
实际看到的那一份。

**R3-2：同一 unit 内同一动作出现 ≥2 次 ⇒ 逐条标记。**

重复不总是死循环（滚动加载、换关键词重搜都可能合法），所以这里只
标记不剔。标记落在 meta（训练代码不看，人核与批次筛选用），并且
只标**留在训练集里的步**——被 R3-1 剔掉的步由 ``deduped`` 交代。

``unit`` 是重复判定的边界：搜索单元和每个站点单元各自独立。跨 unit
的同一个 ``goto`` 不算重复（那是「换了一个站再访问」，不是原地打转）。

为什么动作签名用规范化 JSON 而不是渲染后的 ``[动作]`` 行
--------------------------------------------------------

那一行的参数顺序取决于 dict 插入序；同一动作两次调用若参数来源顺序
不同，渲染行就会不同，死循环会漏掉。签名对键排序后再比。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


def action_key(action: Mapping[str, Any]) -> str:
    """动作的规范化签名：``tool`` + 平铺参数（键排序、紧凑 JSON）。

    ``params`` 与 ``target`` 的平铺规则与 :func:`views._arguments`
    一致（``click`` 的语义目标在 ``target`` 里，``goto`` 的 url 在
    ``params`` 里）；空值槽不参与签名，与渲染侧「不写空参数」同款。
    """
    tool = str(action.get("tool") or "")
    args: dict[str, Any] = dict(action.get("params") or {})
    target = action.get("target")
    if isinstance(target, Mapping):
        for k, v in target.items():
            if v not in (None, ""):
                args[str(k)] = v
    return tool + "\x00" + json.dumps(
        args, ensure_ascii=False, sort_keys=True, default=str
    )


@dataclass(frozen=True, slots=True)
class Step:
    """待筛选的一步。

    - ``step_index``：**原始**步号（P3 是 P2 ``actions`` 的索引，T 是
      跨 unit 的全局序号，均含 infrastructure 步），与 meta 的
      ``rationale_steps.step_index`` 同一坐标系——人能直接对上；
    - ``unit``：重复判定的边界；P3 每份样本一个 unit；
    - ``observation``：**已经渲染好**的训练态文本（比较对象）。
    """

    step_index: int
    unit: str
    action: Mapping[str, Any]
    observation: str


@dataclass(frozen=True, slots=True)
class Screening:
    """一次筛选的结果。

    - ``steps``：保留进训练视图的步（顺序不变，内容原样）；
    - ``deduped``：被 R3-1 剔出的原始步号（按出现顺序）；逐条列出，
      不合成计数——合成之后就分不出「剔了哪几步」，复核时无从对账；
    - ``repeats``：R3-2 标记，逐条 ``{step_index, unit, tool, occurrence}``；
      ``occurrence`` 从 2 起（第一次出现不是重复），且只统计保留下来的步。
    """

    steps: tuple[Step, ...]
    deduped: tuple[int, ...]
    repeats: tuple[dict[str, Any], ...]


def screen(steps: Sequence[Step]) -> Screening:
    """按 R3-1 剔出死循环步，再按 R3-2 标记重复动作。

    相邻比较以 **unit 为界**：进入新 unit 时「上一步」清空，跨 unit 的
    同动作同观察不剔（观察文本里通常带网址，本就不会相同；但即使相同，
    语义也是两件事）。

    连续三步相同时只保留第一步：比较对象是**上一条保留的步**，所以
    A/A/A 里的第二、三步都与第一步比，都会被剔。
    """
    kept: list[Step] = []
    deduped: list[int] = []
    prev: Step | None = None
    for step in steps:
        if (
            prev is not None
            and prev.unit == step.unit
            and action_key(prev.action) == action_key(step.action)
            and prev.observation == step.observation
        ):
            deduped.append(step.step_index)
            continue
        kept.append(step)
        prev = step

    # R3-2 只在保留下来的步上统计：训练集里的重复才是模型学到的重复。
    repeats: list[dict[str, Any]] = []
    seen: dict[str, dict[str, int]] = {}
    for step in kept:
        counts = seen.setdefault(step.unit, {})
        key = action_key(step.action)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] >= 2:
            repeats.append({
                "step_index": step.step_index,
                "unit": step.unit,
                "tool": str(step.action.get("tool") or ""),
                "occurrence": counts[key],
            })

    return Screening(tuple(kept), tuple(deduped), tuple(repeats))