"""label_studio.settings: 强类型 frozen dataclass.

只声明类型与字段级校验; YAML 解析在
:mod:`label_studio.config_loader` (与 ``orchestration.config_loader`` 同风格)。

方案: ``docs/设计方案/label-studio-integration.md`` §6
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 默认 label_config 相对路径 (相对仓库根)。
DEFAULT_LABEL_CONFIG = "label_studio/label_configs/trajectory_review.xml"

#: ``quality_scorer`` 的七维权重 (gdr/config/settings.py 的默认值)。
#: 复制而非 import, 避免 label_studio 依赖 gdr 内部 settings 路径 ——
#: 只用于**标注"该分量是估算"**, 不参与任何计算。
COMPONENT_WEIGHTS: dict[str, float] = {
    "health": 0.25,
    "judge": 0.25,
    "intent": 0.20,
    "modified": 0.10,
    "diversity": 0.10,
    "noise": 0.05,
    "depth": 0.05,
}

#: 权重 ≥ 该值的分量若为估算, UI 必须提示 (方案 §5.2)。
ESTIMATED_WEIGHT_ALERT_THRESHOLD = 0.20

#: 明确标注为"粗估"的分量 —— ``quality_scorer._avg_health_score`` 的
#: docstring 自承: router 的 health_scores 没落盘, 这里用 toolcall 成功率粗估。
KNOWN_ESTIMATED_COMPONENTS = frozenset({"health"})


class SettingsError(ValueError):
    """配置字段级校验失败 (对齐 orchestration 的 ConfigValidationError)。"""


@dataclass(frozen=True)
class UploadSettings:
    """CLI 全量推送策略。

    注意: 这组开关**只管** ``python -m label_studio upload``;
    orchestration hook 只看 :class:`HookSettings` (方案 §6)。
    """

    enabled: bool = False
    batch_size: int = 50
    include_predictions: bool = True
    filter_min_training_value_score: float = 0.0
    filter_complexity_tiers: tuple[str, ...] = ()
    skip_task_ids: tuple[str, ...] = ()
    dry_run_skip_threshold: int = 5000

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise SettingsError("upload.batch_size 必须 ≥ 1")
        if not 0.0 <= self.filter_min_training_value_score <= 1.0:
            raise SettingsError("upload.filter_min_training_value_score 必须在 [0,1]")
        unknown = set(self.filter_complexity_tiers) - {"easy", "medium", "hard"}
        if unknown:
            raise SettingsError(f"upload.filter_complexity_tiers 含未知 tier: {sorted(unknown)}")


@dataclass(frozen=True)
class ScorecardSettings:
    """评分卡生成策略。"""

    enabled: bool = True
    require_estimated_flag: bool = True
    drop_missing_dimensions: bool = True


@dataclass(frozen=True)
class CredentialScanSettings:
    """推送前凭据扫描 (方案 §16 R11) —— fail-closed。"""

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
class HookSettings:
    """orchestration task_pipeline step 11 的行为。

    刻意只有两个字段。原先还有一个 ``on_failure``（log_only / log_and_metric）
    —— 它被解析、被校验、被写进示例配置, 但 ``ls_hook`` 从未读取过, 两个值
    行为完全相同。推送是旁路: 失败既不重试也不抛异常, 所以"失败后怎么办"
    本来就没有可配置的分叉。留着一个不生效的开关比没有它更糟 —— 配了
    ``log_and_metric`` 的人会以为指标是特意打开的。已删除。
    """

    enabled: bool = False

    #: 一次推送的完整预算: PAT refresh + 建连 + 查项目 + PATCH label_config
    #: + create task + 预标注 POST。5s 下首次必超, 超时即丢样本。
    hook_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.hook_timeout_seconds <= 0:
            raise SettingsError("hook.hook_timeout_seconds 必须 > 0")


@dataclass(frozen=True)
class LabelStudioSettings:
    """``config/config.yaml`` 顶层 ``label_studio:`` 段。

    终点定位: Label Studio 是本项目**终点** —— 只推送, 不 fetch, 不回流。
    """

    #: 本机 LS 默认地址。**不是 8088** —— 8088 上是 QwenPaw 执行后端
    #: (``simulate_serve.agent_endpoint``)。指错不报"连不上", 而是伪装成
    #: "API key 无效", 把人引去查 key —— 实际连的是另一个服务。
    base_url: str = "http://127.0.0.1:8099"
    api_key: str | None = None
    api_key_path: Path | None = None

    project_title: str = "trajectory-sft-quality"
    label_config_path: Path | None = None
    project_id: int | None = None

    #: 推送台账（``output/label_studio/``）的落盘根。测试用 ``tmp_path`` 覆盖。
    output_root: Path = Path("output")

    upload: UploadSettings = field(default_factory=UploadSettings)
    scorecard: ScorecardSettings = field(default_factory=ScorecardSettings)
    credential_scan: CredentialScanSettings = field(
        default_factory=CredentialScanSettings
    )
    health_check: HealthCheckSettings = field(default_factory=HealthCheckSettings)
    hook: HookSettings = field(default_factory=HookSettings)

    def __post_init__(self) -> None:
        # 纯空白也算空 —— `"  "` truthy 但拼出来的 URL 不可用, 静默通过会
        # 在推送时才炸, 报错点离配置点很远。
        if not (self.base_url or "").strip():
            raise SettingsError("label_studio.base_url 不能为空")

    # -- 凭据解析 ---------------------------------------------------------

    def resolve_api_key(self) -> str | None:
        """取 API key: ``api_key_path`` 优先于 ``api_key``。

        两者都为空 / 路径读不出 → 返回 None, 由调用方报清晰错误。
        本方法**不**抛异常, 也不打印凭据内容。
        """
        if self.api_key_path is not None:
            try:
                text = Path(self.api_key_path).read_text(encoding="utf-8").strip()
            except OSError:
                return None
            return text or None
        key = (self.api_key or "").strip()
        return key or None

    def has_credentials(self) -> bool:
        return self.resolve_api_key() is not None

    def label_config_file(self) -> Path:
        """label_config 绝对路径; 未配置时回落到仓库内默认路径。

        相对路径统一按 **仓库根** 解析（与 ``config_loader._resolve_path`` 同口径）——
        直接构造 ``LabelStudioSettings()`` 与经 config loader 构造必须得到同一个
        文件, 否则 CLI 与测试会读两份不同的 XML。
        """
        path = self.label_config_path or Path(DEFAULT_LABEL_CONFIG)
        if path.is_absolute():
            return path
        from shared_config import REPO_ROOT

        return REPO_ROOT / path

    def is_push_enabled(self) -> bool:
        """任一推送通道开启 (CLI upload 或 orchestration hook)。"""
        return self.upload.enabled or self.hook.enabled

    def tier_filter(self) -> frozenset[str]:
        return frozenset(self.upload.filter_complexity_tiers)


__all__ = [
    "LabelStudioSettings",
    "UploadSettings",
    "ScorecardSettings",
    "CredentialScanSettings",
    "HealthCheckSettings",
    "HookSettings",
    "SettingsError",
    "COMPONENT_WEIGHTS",
    "ESTIMATED_WEIGHT_ALERT_THRESHOLD",
    "KNOWN_ESTIMATED_COMPONENTS",
    "DEFAULT_LABEL_CONFIG",
]
