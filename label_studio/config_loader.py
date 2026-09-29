"""label_studio.config_loader: 从根配置读 ``label_studio:`` 段.

与 ``orchestration.config_loader`` 同风格: 复用
:mod:`shared_config` 的根配置定位 (``SIMCTL_CONFIG`` env > ``config/config.yaml``)
与 ``${VAR}`` 占位符展开。

``load_label_studio_config`` **不创建任何目录**, 也不建立任何连接 ——
纯解析, 便于单元测试与 ``--validate-config`` 风格的离线检查。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from shared_config import find_root_config, load_yaml_file

from label_studio.settings import (
    DEFAULT_LABEL_CONFIG,
    CredentialScanSettings,
    HealthCheckSettings,
    HookSettings,
    LabelStudioSettings,
    ScorecardSettings,
    SettingsError,
    UploadSettings,
)

log = logging.getLogger(__name__)

#: 根配置中的 section 名。
SECTION_KEY = "label_studio"

#: 相对路径字段 (相对仓库根)。
_REPO_RELATIVE_PATHS = ("label_config_path", "api_key_path")


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v is not None and str(v).strip())
    return ()


def _as_patterns(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """凭据扫描模式: 用户给的是**正则源串**, 这里编译一遍提前暴露语法错误。"""
    patterns = _as_str_tuple(value)
    if not patterns:
        return default
    import re

    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise SettingsError(f"credential_scan.patterns 含非法正则 {pattern!r}: {exc}") from exc
    return patterns


def _repo_root() -> Path:
    from shared_config import REPO_ROOT

    return REPO_ROOT


def _resolve_path(value: Any) -> Path | None:
    if value is None or not str(value).strip():
        return None
    path = Path(str(value))
    return path if path.is_absolute() else _repo_root() / path


def _build_upload(raw: Any) -> UploadSettings:
    raw = raw if isinstance(raw, dict) else {}
    return UploadSettings(
        enabled=_as_bool(raw.get("enabled"), False),
        batch_size=_as_int(raw.get("batch_size"), 50),
        include_predictions=_as_bool(raw.get("include_predictions"), True),
        filter_min_training_value_score=_as_float(
            raw.get("filter_min_training_value_score"), 0.0
        ),
        filter_complexity_tiers=_as_str_tuple(raw.get("filter_complexity_tiers")),
        skip_task_ids=_as_str_tuple(raw.get("skip_task_ids")),
        dry_run_skip_threshold=_as_int(raw.get("dry_run_skip_threshold"), 5000),
    )


def _build_scorecard(raw: Any) -> ScorecardSettings:
    raw = raw if isinstance(raw, dict) else {}
    return ScorecardSettings(
        enabled=_as_bool(raw.get("enabled"), True),
        require_estimated_flag=_as_bool(raw.get("require_estimated_flag"), True),
        drop_missing_dimensions=_as_bool(raw.get("drop_missing_dimensions"), True),
    )


def _build_credential_scan(raw: Any) -> CredentialScanSettings:
    raw = raw if isinstance(raw, dict) else {}
    defaults = CredentialScanSettings()
    return CredentialScanSettings(
        enabled=_as_bool(raw.get("enabled"), True),
        patterns=_as_patterns(raw.get("patterns"), defaults.patterns),
        on_hit=str(raw.get("on_hit") or defaults.on_hit),
    )


def _build_health_check(raw: Any) -> HealthCheckSettings:
    raw = raw if isinstance(raw, dict) else {}
    return HealthCheckSettings(
        timeout_seconds=_as_float(raw.get("timeout_seconds"), 5.0),
        retry_attempts=_as_int(raw.get("retry_attempts"), 3),
        retry_backoff_seconds=_as_float(raw.get("retry_backoff_seconds"), 2.0),
    )


def _build_hook(raw: Any) -> HookSettings:
    raw = raw if isinstance(raw, dict) else {}
    return HookSettings(
        enabled=_as_bool(raw.get("enabled"), False),
        hook_timeout_seconds=_as_float(raw.get("hook_timeout_seconds"), 30.0),
    )


def build_settings(raw: dict[str, Any] | None) -> LabelStudioSettings:
    """从已加载的根配置 dict 构造 :class:`LabelStudioSettings`。

    ``raw`` 为 None / 空 / 无 ``label_studio:`` 段时返回**全默认**设置
    (所有开关关闭), 这样未配置的仓库也能安全 import 与跑测试。
    """
    section = (raw or {}).get(SECTION_KEY) if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return LabelStudioSettings(label_config_path=_repo_root() / DEFAULT_LABEL_CONFIG)

    label_config = _resolve_path(section.get("label_config_path"))
    return LabelStudioSettings(
        base_url=str(section.get("base_url") or "http://127.0.0.1:8099").rstrip("/"),
        api_key=section.get("api_key") or None,
        api_key_path=_resolve_path(section.get("api_key_path")),
        project_title=str(section.get("project_title") or "trajectory-sft-quality"),
        label_config_path=label_config or (_repo_root() / DEFAULT_LABEL_CONFIG),
        project_id=_as_int(section.get("project_id"), 0) or None,
        output_root=Path(
            str(section.get("output_root") or "output")
        ),
        upload=_build_upload(section.get("upload")),
        scorecard=_build_scorecard(section.get("scorecard")),
        credential_scan=_build_credential_scan(section.get("credential_scan")),
        health_check=_build_health_check(section.get("health_check")),
        hook=_build_hook(section.get("hook")),
    )


def load_label_studio_config(config_path: Path | None = None) -> LabelStudioSettings:
    """读取根配置的 ``label_studio:`` 段。

    Args:
        config_path: 显式配置路径；None 时走 ``SIMCTL_CONFIG`` env →
            仓库根 ``config/config.yaml`` 的默认顺序。

    Returns:
        构造好的 settings。根配置不存在时返回全默认（开关关闭）。
    """
    path = config_path or find_root_config()
    if path is None:
        log.debug("load_label_studio_config: 未找到根配置, 返回默认设置 (全部关闭)")
        return build_settings(None)
    raw = load_yaml_file(path)
    return build_settings(raw)


__all__ = [
    "SECTION_KEY",
    "build_settings",
    "load_label_studio_config",
]
