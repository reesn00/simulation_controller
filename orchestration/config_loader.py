"""orchestration.config_loader: 加载编排配置为强类型 dataclass.

配置统一来自仓库根 ``config/config.yaml`` (``SIMCTL_CONFIG`` env 可重定向);
新架构 ``simulation server → gdr → etl`` 下的契约见
``docs/设计方案/pipeline-contracts.md`` §1.

新结构:
- ``settings: PipelineSettings`` —  ``max_parallelism`` / 重试上限 / 轮询秒数
- ``paths: Paths``               — 全部为 ``pathlib.Path``
- ``gdr_settings: gdr.Settings`` — 复用 gdr 库的 ``Settings`` 类型

``load_config`` 不创建任何目录 (契约 §1.5), 由调用方按需 ``mkdir``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shared_config import (
    ROOT_CONFIG_ENV,
    ROOT_CONFIG_PATH,
    find_root_config,
    load_yaml_file,
)

from orchestration.settings import Paths, PipelineSettings


class ConfigValidationError(ValueError):
    """编排配置校验失败时抛 (契约 §1.5)."""


# ---------------------------------------------------------------------------
# Paths 默认值 (契约 §1.2)
# ---------------------------------------------------------------------------

_PATHS_DEFAULTS: dict[str, str] = {
    "simulate_serve_config": "config/config.yaml",
    "trajectory_dir": "output/agent_trajectory",
    "runs_dir": "output/runs",
    "refined_dir": "output/refined",
    "etl_outputs_dir": "output/refine_data",
    "sqlite_db": "output/orchestration/orchestration.db",
    "dead_dir": "output/orchestration/dead",
    "pid_file": "output/orchestration/orchestration.pid",
    "log_dir": "output/orchestration/logs",
}


# ---------------------------------------------------------------------------
# 顶层 dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OrchestrationConfig:
    """仓库根 ``config/config.yaml`` 的编排层强类型视图.

    字段:
        settings: PipelineSettings
        paths: Paths
        gdr_settings: ``gdr.config.settings.Settings`` 实例
        source_path: 实际加载的 yaml 文件绝对路径 (调试用)
    """

    settings: PipelineSettings
    paths: Paths
    gdr_settings: Any  # 实际类型: gdr.config.settings.Settings; 类型注解用 Any 避免硬依赖
    source_path: str = ""


# ---------------------------------------------------------------------------
# YAML → dataclass 转换
# ---------------------------------------------------------------------------

def _build_pipeline_settings(raw: dict[str, Any]) -> PipelineSettings:
    """解析 ``orchestration.pipeline:`` 段, 应用契约 §1.5 校验."""
    if raw is None:
        raw = {}
    max_parallelism = int(raw.get("max_parallelism", 1))
    if max_parallelism < 1:
        raise ConfigValidationError("max_parallelism must be ≥ 1")
    max_retry_gdr = int(raw.get("max_retry_gdr", 3))
    if max_retry_gdr < 0:
        raise ConfigValidationError("max_retry_gdr must be ≥ 0")
    max_retry_etl = int(raw.get("max_retry_etl", 3))
    if max_retry_etl < 0:
        raise ConfigValidationError("max_retry_etl must be ≥ 0")
    retry_poll_seconds = float(raw.get("retry_poll_seconds", 2.0))
    if retry_poll_seconds <= 0:
        raise ConfigValidationError("retry_poll_seconds must be > 0")
    return PipelineSettings(
        max_parallelism=max_parallelism,
        max_retry_gdr=max_retry_gdr,
        max_retry_etl=max_retry_etl,
        retry_poll_seconds=retry_poll_seconds,
    )


def _build_paths(raw: dict[str, Any], *, default_config_path: Path) -> Paths:
    """解析 ``paths:`` 段, 把字符串路径转 ``Path``.

    根配置同时含 ``simulate_serve:`` 段时, ``paths.simulate_serve_config``
    默认指向该配置文件本身 (与旧行为一致 — producer 从同一文件读 simulate_serve
    配置, 避免双配置).
    """
    if raw is None:
        raw = {}
    resolved: dict[str, str] = dict(_PATHS_DEFAULTS)
    for key in resolved:
        if key in raw and raw[key] is not None:
            resolved[key] = str(raw[key])
    # 根格式下 simulate_serve_config 默认指向文件本身 (若用户未显式覆盖)
    if "simulate_serve_config" not in raw:
        resolved["simulate_serve_config"] = str(default_config_path)
    # 校验非空
    for key, val in resolved.items():
        if not val or not isinstance(val, str):
            raise ConfigValidationError(f"paths.{key} must be a non-empty string")
    return Paths(
        simulate_serve_config=Path(resolved["simulate_serve_config"]),
        trajectory_dir=Path(resolved["trajectory_dir"]),
        runs_dir=Path(resolved["runs_dir"]),
        refined_dir=Path(resolved["refined_dir"]),
        etl_outputs_dir=Path(resolved["etl_outputs_dir"]),
        sqlite_db=Path(resolved["sqlite_db"]),
        dead_dir=Path(resolved["dead_dir"]),
        pid_file=Path(resolved["pid_file"]),
        log_dir=Path(resolved["log_dir"]),
    )


def _build_gdr_settings() -> Any:
    """构造 ``gdr.config.settings.Settings`` 实例.

    gdr 的 ``Settings`` 是 pydantic ``BaseSettings``, 自动从仓库根配置
    (或 ``GDR_CONFIG_FILE`` env) 加载; 我们只需 ``Settings()`` 即可.
    """
    from gdr.config.settings import Settings
    return Settings()


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def load_config(config_path: Path | None = None) -> OrchestrationConfig:
    """加载编排配置; 失败抛 ``ConfigValidationError`` 或 ``FileNotFoundError``.

    Args:
        config_path: 显式配置文件路径; ``None`` 时按 ``find_root_config`` 定位
            (``SIMCTL_CONFIG`` env > 仓库根 ``config/config.yaml``).
            文件不存在 → ``FileNotFoundError`` (无包内兜底).

    校验:
        - ``max_parallelism ≥ 1`` (契约 §1.5).
        - 各 path 必须是非空字符串 (契约 §1.5).

    副作用:
        - 不创建任何目录 (契约 §1.5).
    """
    if config_path is None:
        root = find_root_config()
        if root is None:
            raise FileNotFoundError(
                f"Unified root config not found: {ROOT_CONFIG_PATH} "
                f"(or set {ROOT_CONFIG_ENV}); no fallback config exists"
            )
        cfg_path = root
    else:
        cfg_path = Path(config_path).resolve()
    if not cfg_path.exists():
        raise FileNotFoundError(f"Orchestration config not found: {cfg_path}")

    raw = load_yaml_file(cfg_path)
    if not isinstance(raw, dict):
        raw = {}

    orchestration_raw = raw.get("orchestration") or {}
    if not isinstance(orchestration_raw, dict):
        orchestration_raw = {}
    # 兼容两种结构:
    #   1) 新契约: orchestration.pipeline: {...}
    #   2) 过渡期: orchestration: 直接是 pipeline 字段 (顶层展开)
    pipeline_raw = orchestration_raw.get("pipeline")
    if not isinstance(pipeline_raw, dict):
        # 旧顶层展开结构 — 把 orchestration 整段当 pipeline 字段读
        # (去掉已知的 paths 字段)
        pipeline_raw = orchestration_raw

    paths_raw = raw.get("paths")
    if paths_raw is None and isinstance(orchestration_raw.get("paths"), dict):
        paths_raw = orchestration_raw["paths"]
    if not isinstance(paths_raw, dict):
        paths_raw = {}

    settings = _build_pipeline_settings(pipeline_raw)
    paths = _build_paths(paths_raw, default_config_path=cfg_path)
    gdr_settings = _build_gdr_settings()

    return OrchestrationConfig(
        settings=settings,
        paths=paths,
        gdr_settings=gdr_settings,
        source_path=str(cfg_path),
    )
