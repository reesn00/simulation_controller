"""ls_bridge.settings: 环境变量版配置（v2 重写, 弃 YAML）。

口径对齐 trajectory_pipeline 现行约定（CLAUDE.md）:

* **key 只走环境变量** —— ``TRAJECTORY_LS_API_KEY``, 不走 YAML、不走
  api_key 文件（v1 的 ``config_loader`` + ``api_key_path`` 整条链路不迁移）。
* ``TRAJECTORY_LS_`` 前缀防与 v1 的 ``LABEL_STUDIO_*`` 撞名 —— 两套环境
  同时存在时（迁移期）互不污染。
* CLI 参数覆盖环境变量; 环境变量覆盖默认值。

照读自 ``archive/v1/label_studio/settings.py``, 删除的段: ``HookSettings``
（v2 无 orchestration hook）、``COMPONENT_WEIGHTS`` 及估算分量标注（gdr
专有概念）、tier / min_score 过滤（scout 已三态分流, 上传侧不再过滤）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class SettingsError(ValueError):
    """配置字段级校验失败。"""


@dataclass(frozen=True)
class UploadSettings:
    """CLI 全量推送策略。"""

    enabled: bool = False
    batch_size: int = 50
    include_predictions: bool = True
    #: 数量安全闸（**不是分数过滤**）: 本次待推送条数超过它且未 ``--force``
    #: 时拒推, 逼人先 ``--dry-run`` 核对。v1 实测教训保留。
    dry_run_skip_threshold: int = 5000

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise SettingsError("upload.batch_size 必须 ≥ 1")


@dataclass(frozen=True)
class ScorecardSettings:
    """评分卡（scorecard.v2）生成策略。"""

    enabled: bool = True


@dataclass(frozen=True)
class CredentialScanSettings:
    """推送前凭据扫描 (v1 方案 §16 R11) —— fail-closed。"""

    enabled: bool = True
    patterns: tuple[str, ...] = (
        r"Bearer\s",
        r"password\s*=",
        r"-----BEGIN .*PRIVATE KEY-----",
        r"sk-[A-Za-z0-9]{16,}",
        r"ghp_[A-Za-z0-9]{20,}",
        r"AKIA[0-9A-Z]{16}",
        r"eyJ[A-Za-z0-9_-]{10,}\.",
    )
    on_hit: str = "reject_task"

    def __post_init__(self) -> None:
        if self.on_hit not in ("reject_task", "skip_task"):
            raise SettingsError(
                f"credential_scan.on_hit 只支持 reject_task / skip_task, got {self.on_hit!r}"
            )


@dataclass(frozen=True)
class HealthCheckSettings:
    """LS 连通性探测。"""

    timeout_seconds: float = 5.0
    retry_attempts: int = 3
    retry_backoff_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise SettingsError("health_check.timeout_seconds 必须 > 0")
        if self.retry_attempts < 1:
            raise SettingsError("health_check.retry_attempts 必须 ≥ 1")


@dataclass(frozen=True)
class LSSettings:
    """ls_bridge 的完整配置。**单向推送**: 只上传, 不回流（回流等 L7）。"""

    #: 本机 LS 默认地址。**不是 8088** —— v1 实测 8088 上是执行后端; 指错
    #: 不报"连不上", 而是伪装成 "API key 无效", 把人引去查 key。
    base_url: str = "http://127.0.0.1:8099"
    api_key: str | None = None

    project_title: str = "trajectory-review"
    label_config_path: Path | None = None
    project_id: int | None = None

    upload: UploadSettings = field(default_factory=UploadSettings)
    scorecard: ScorecardSettings = field(default_factory=ScorecardSettings)
    credential_scan: CredentialScanSettings = field(
        default_factory=CredentialScanSettings
    )
    health_check: HealthCheckSettings = field(default_factory=HealthCheckSettings)

    def __post_init__(self) -> None:
        # 纯空白也算空 —— 静默通过会在推送时才炸, 报错点离配置点很远。
        if not (self.base_url or "").strip():
            raise SettingsError("TRAJECTORY_LS_URL 不能为空")

    def resolve_api_key(self) -> str | None:
        """取 API key。空 / 纯空白 → None, 由调用方报清晰错误。

        本方法**不**抛异常, 也不打印凭据内容。
        """
        key = (self.api_key or "").strip()
        return key or None

    def has_credentials(self) -> bool:
        return self.resolve_api_key() is not None

    def is_push_enabled(self) -> bool:
        return self.upload.enabled


#: 环境变量名 —— 单一事实来源, CLI 帮助文本也引用这里。
ENV_URL = "TRAJECTORY_LS_URL"
ENV_API_KEY = "TRAJECTORY_LS_API_KEY"
ENV_PROJECT_ID = "TRAJECTORY_LS_PROJECT_ID"
ENV_ENABLED = "TRAJECTORY_LS_ENABLED"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def load_settings(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    project_id: int | None = None,
    enabled: bool | None = None,
    batch_size: int | None = None,
) -> LSSettings:
    """从环境变量构建 settings, 显式参数覆盖环境变量。

    环境变量缺省时回落到 :class:`LSSettings` 默认值 —— ``status`` 等只读
    命令在零配置下也能跑（只报连通性, 不推数据）。
    """
    url = base_url if base_url is not None else os.environ.get(ENV_URL, "")
    key = api_key if api_key is not None else os.environ.get(ENV_API_KEY, "")
    pid = project_id
    if pid is None:
        raw = os.environ.get(ENV_PROJECT_ID, "").strip()
        if raw:
            try:
                pid = int(raw)
            except ValueError as exc:
                raise SettingsError(
                    f"{ENV_PROJECT_ID}={raw!r} 不是整数"
                ) from exc
    flag = enabled if enabled is not None else _env_flag(ENV_ENABLED)
    size = batch_size if batch_size is not None else 50
    return LSSettings(
        base_url=url or "http://127.0.0.1:8099",
        api_key=key or None,
        project_id=pid,
        upload=UploadSettings(enabled=flag, batch_size=size),
    )


__all__ = [
    "LSSettings",
    "UploadSettings",
    "ScorecardSettings",
    "CredentialScanSettings",
    "HealthCheckSettings",
    "SettingsError",
    "ENV_URL",
    "ENV_API_KEY",
    "ENV_PROJECT_ID",
    "ENV_ENABLED",
    "load_settings",
]
