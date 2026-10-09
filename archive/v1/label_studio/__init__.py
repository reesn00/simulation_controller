"""label_studio: C3 轨迹 + 评分卡 → Label Studio 的**单向**推送层.

终点定位（方案 §1）: Label Studio 是本项目**终点**。只 push, 不 fetch,
不回流 —— 标注结果不回到本项目, 本项目也不维护任何"已标注"状态。

主要入口::

    from label_studio.config_loader import load_label_studio_config
    from label_studio.client import build_client
    from label_studio.project_manager import resolve_project_id
    from label_studio.task_exporter import export_batch, push_batch

命令行::

    python -m label_studio init-project
    python -m label_studio status
    python -m label_studio upload
    python -m label_studio purge --confirm

凭据红线（CLAUDE.md）：API key 只经 ``${LABEL_STUDIO_API_KEY}`` env 或
``api_key_path`` 传入, 不得出现在代码 / 测试 / 文档 / 日志中。
"""

from __future__ import annotations

from label_studio.errors import (
    C3ParseError,
    CredentialLeakDetected,
    LabelStudioAuthFailed,
    LabelStudioError,
    LabelStudioProjectError,
    LabelStudioUnavailable,
)
from label_studio.settings import LabelStudioSettings, SettingsError

__all__ = [
    "C3ParseError",
    "CredentialLeakDetected",
    "LabelStudioAuthFailed",
    "LabelStudioError",
    "LabelStudioProjectError",
    "LabelStudioSettings",
    "LabelStudioUnavailable",
    "SettingsError",
]
