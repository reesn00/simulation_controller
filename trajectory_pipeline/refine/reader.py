"""P1 存档 → 观察清单——**按文件格式读**，不 import 装配层。

为什么自己读
------------
装配层（:mod:`trajectory_pipeline.assembler`）是**下游**：P2 读 P1 切条、
T 读 P1 渲染。精炼是**上游**的一步（它产出训练语料要用的东西），
方向不能反——所以这里按 P1 的**文件格式**读，不 import 装配层的切条逻辑。

⚠️ 也因此这里**不追求与 P2/T 的步号对齐**：旁挂按**正文内容哈希**索引
（:mod:`trajectory_pipeline.common.refine_sidecar`），两份渲染各自按
自己的坐标系找到同一条观察。正文一样 ⇒ 键一样 ⇒ 精炼结果一样。

保真口径：``keep`` 取**并集**
-----------------------------
P2 的「下一步动作目标」跨单元算（样本的动作流是拍平的），
T 的只在本单元内算。两边都要保住 ⇒ 取并集：

- 本单元内下一个**可见**动作（``origin != "infrastructure"``）的目标；
- 下一单元第一个可见动作的目标（P2 那侧会看到它）。

并集只会让保真判据**更严**，不会更松——多保住一行不会有害。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ObservationSpec:
    """一条待精炼的观察。

    ``keep`` = 下一步动作目标的并集（见模块 docstring）。
    ``observation`` 是 P1 的观察 dict（原样，**不裁剪**）。
    ``body`` 是**正文原文**（按与渲染层同一份挑法取的，见 :func:`body_of`）。
    """

    unit: str
    index: int
    observation: Mapping[str, Any]
    keep: tuple[str, ...] = ()
    body: str = ""


def body_of(observation: Mapping[str, Any] | None) -> str:
    """这份观察的**正文原文**——**与渲染层同一份挑法**。

    ⚠️ 两边必须挑**同一份**：旁挂按**正文内容哈希**索引，挑错了就是
    「按 A 的哈希去查 B 的精炼」——查不到 ⇒ 回退（安全，但白跑一趟）。
    渲染层的挑法见
    :func:`trajectory_pipeline.assembler.observation_view._pick_body`：
    先 ``body_text``，再退回 ``body_preview``（老存档），都没有就是空。
    """
    obs = observation or {}
    body = obs.get("body_text")
    if isinstance(body, str) and body:
        return body
    legacy = obs.get("body_preview")
    if isinstance(legacy, str) and legacy:
        return legacy
    return ""


def action_target(action: Mapping[str, Any] | None) -> tuple[str, ...]:
    """一条动作的可读目标（点击名 / 导航网址）。

    与 P2 的口径同款：目标名取 ``target.name`` 或 ``target.label``
    （两种方言都见过），网址取 ``params.url``。
    """
    act = action or {}
    target = act.get("target") or {}
    name = str(target.get("name") or target.get("label") or "").strip()
    url = str((act.get("params") or {}).get("url") or "").strip()
    return tuple(x for x in (name, url) if x)


def _is_visible(action: Mapping[str, Any] | None) -> bool:
    """这一步是不是**模型可见**的动作。

    ``infrastructure``（开合标签页）是执行器自己的会话管理动作，
    模型永远不该输出它——它也不该占「下一步动作目标」的名额。
    """
    return str((action or {}).get("origin") or "") != "infrastructure"


def _next_visible_target(
    actions: Sequence[Mapping[str, Any]], index: int
) -> tuple[str, ...]:
    """第 ``index`` 步之后**本单元内**下一个可见动作的目标。"""
    for act in actions[index + 1:]:
        if not _is_visible(act):
            continue
        target = action_target(act)
        if target:
            return target
    return ()


def _first_visible_target(steps: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """一单元里第一个可见动作的目标。"""
    for step in steps:
        act = step.get("action") or {}
        if not _is_visible(act):
            continue
        target = action_target(act)
        if target:
            return target
    return ()


def _units(archive: Mapping[str, Any]) -> list[tuple[str, list[Mapping[str, Any]]]]:
    """P1 → 单元列表（``search`` + ``site-1..N``）。

    ⚠️ **不按 ``schema._units_of`` 的口径**：那里在搜索阶段只有一步时
    会把顶层的 ``search_observation`` 快照换进那一步（P2 现行契约用的
    是那份快照）。精炼不切条、不出样本，用哪份观察都行——但**两份都
    要覆盖**：顶层快照与步骤观察是两份不同的正文，各自进精炼，
    键不同、结果不同、互不覆盖。
    """
    units: list[tuple[str, list[Mapping[str, Any]]]] = [
        ("search", list(archive.get("steps") or ())),
    ]
    for i, visit in enumerate(archive.get("visits") or (), 1):
        units.append((f"site-{i}", list(visit.get("steps") or ())))
    return units


def iter_observations(archive: Mapping[str, Any]) -> list[ObservationSpec]:
    """一份 P1 → 待精炼的观察清单。

    **正文为空的观察不进清单**——没有正文就没有可精炼的东西，
    进清单只会给旁挂塞一堆空记录。
    """
    units = _units(archive)
    out: list[ObservationSpec] = []
    for u, (name, steps) in enumerate(units):
        actions = [s.get("action") or {} for s in steps]
        next_unit_target = (
            _first_visible_target(units[u + 1][1]) if u + 1 < len(units) else ()
        )
        for i, step in enumerate(steps):
            obs = step.get("observation")
            if not isinstance(obs, Mapping):
                continue
            body = body_of(obs)
            if not body.strip():
                continue
            keep = _next_visible_target(actions, i)
            if next_unit_target:
                keep = tuple(dict.fromkeys([*keep, *next_unit_target]))
            out.append(ObservationSpec(name, i, obs, keep, body))
    return out


__all__ = [
    "ObservationSpec",
    "action_target",
    "body_of",
    "iter_observations",
]
