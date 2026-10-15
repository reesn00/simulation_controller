"""Label Studio 双向通道（决策 D6：改造为双向评估节点）。

现状是单向终点（推送不回流），双向化须处理存量已踩过的坑：

    LS 1.23 无原生去重      → 本地台账 push_index__<project_id>.jsonl
    PATCH label_config 覆盖标注员改动 → 双向后禁全量 PATCH
    editable=true → annotation 是标注员修正稿 → 回流只限黄金集/评估集，
                                                   训练制品永不被覆盖
    validate/ 绿灯不算数     → 验收以标注页实视为准
    R11 凭据扫描 fail-closed → 保留，双向不豁免

单向推送链路（本包已实现）::

    scout 三件套(positive+negative) → P3 视图回溯 → task.data
    + scorecard.v2 预标注 → R11 扫描 → 台账判重 → LS

入口: ``python -m trajectory_pipeline.evaluation.ls_bridge``
（status / init-project / upload / purge）。配置走 ``TRAJECTORY_LS_*``
环境变量。迁移自 ``archive/v1/label_studio``（照读重写, 未 import）。
"""

from trajectory_pipeline.evaluation.ls_bridge.errors import (
    CredentialLeakDetected,
    LabelStudioAuthFailed,
    LabelStudioError,
    LabelStudioProjectError,
    LabelStudioUnavailable,
    P3ViewError,
    redact,
)
from trajectory_pipeline.evaluation.ls_bridge.settings import LSSettings, load_settings

__all__ = [
    "CredentialLeakDetected",
    "LabelStudioAuthFailed",
    "LabelStudioError",
    "LabelStudioProjectError",
    "LabelStudioUnavailable",
    "P3ViewError",
    "LSSettings",
    "load_settings",
    "redact",
]
