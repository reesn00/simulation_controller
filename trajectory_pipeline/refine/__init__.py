"""``refine``：正文精炼——把「硬裁前 320 字」换成「模型选行」。

一句话
------
原文进，**选出来的行**出：模型只报行号，文本由渲染层从 P1 原文重建，
所以它只可能**漏**、不可能**造**。

模块地图
--------
``prompt``   提示词与用户消息（筛选器口径，不是摘要器口径）
``refiner``  核心：候选行 → 模型选行 → 校验 → 回退
``reader``   P1 存档 → 观察清单（按文件格式读，不 import 装配层）

产物落在**旁挂**里（:mod:`trajectory_pipeline.common.refine_sidecar`），
**不写回 P1**：P1 是取证态，模型输出不进证据链。
"""

from __future__ import annotations

from trajectory_pipeline.refine.prompt import (
    REFINE_PROMPT_VERSION,
    REFINE_SYSTEM,
    build_user_payload,
)
from trajectory_pipeline.refine.reader import (
    ObservationSpec,
    action_target,
    body_of,
    iter_observations,
)
from trajectory_pipeline.refine.refiner import (
    RefineOutcome,
    RefineStep,
    estimate_max_lines,
    needs_refine,
    parse_selection,
    refine_body,
    validate_selection,
)

__all__ = [
    "REFINE_PROMPT_VERSION",
    "REFINE_SYSTEM",
    "ObservationSpec",
    "RefineOutcome",
    "RefineStep",
    "action_target",
    "body_of",
    "build_user_payload",
    "estimate_max_lines",
    "iter_observations",
    "needs_refine",
    "parse_selection",
    "refine_body",
    "validate_selection",
]
