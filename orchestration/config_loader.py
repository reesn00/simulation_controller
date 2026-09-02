"""orchestration.config_loader: 加载 ``orchestration/config.yaml`` 为强类型 dataclass.

设计见 ``docs/orchestration-design.md`` §7。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class PathsConfig:
    simulate_serve_config: str = "simulate_serve/config/config.yaml"
    trajectory_dir: str = "output/agent_trajectory"
    qf_output_dir: str = "orchestration/data/qf_out"
    gdr_output_dir: str = "gdr/refine_data"
    sqlite_db: str = "orchestration/data/orchestration.db"
    dead_dir: str = "orchestration/data/dead"
    pid_file: str = "orchestration/data/orchestration.pid"
    log_dir: str = "orchestration/logs"
    runs_dir: str = "output/runs"  # JsonRunRepository 的 runs 根目录

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "PathsConfig":
        return cls(**raw) if raw else cls()


@dataclass(frozen=True)
class OrchestrationSettings:
    """顶层 orchestration.* 配置."""
    batch_size: int = 3
    gdr_workers: int = 2
    qf_workers: int = 4
    gdr_wait_seconds: float = 10.0
    max_retry_qf: int = 3
    max_retry_gdr: int = 3
    watcher_poll_seconds: float = 2.0
    reap_stale_seconds: int = 300
    reap_stale_interval_seconds: int = 60
    batch_drain_poll_seconds: float = 5.0
    batch_drain_timeout_seconds: float | None = None
    # 空闲退避上限 (#7): 连续空轮时轮询间隔指数增长的封顶秒数;
    # 0 = 关闭退避 (恒定 poll)。默认关闭以兼容短超时测试/低延迟场景,
    # 内置 config.yaml 里给生产值。
    worker_idle_backoff_max_seconds: float = 0.0
    watcher_idle_backoff_max_seconds: float = 0.0

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "OrchestrationSettings":
        if not raw:
            return cls()
        # 允许 None 表示无限等待
        if raw.get("batch_drain_timeout_seconds") is None:
            raw = {**raw, "batch_drain_timeout_seconds": None}
        return cls(**raw)


@dataclass(frozen=True)
class GdrSettings:
    """透传给 ``gdr.Settings`` 的子集（master 只覆盖并发/路径）."""
    workers: int = 2
    llm_concurrency: int = 4

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "GdrSettings":
        return cls(**raw) if raw else cls()


@dataclass(frozen=True)
class OrchestrationConfig:
    """整个 ``orchestration/config.yaml`` 的强类型视图."""
    settings: OrchestrationSettings = field(default_factory=OrchestrationSettings)
    paths: PathsConfig = field(default_factory=PathsConfig)
    gdr: GdrSettings = field(default_factory=GdrSettings)
    source_path: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any], *, source_path: str = "") -> "OrchestrationConfig":
        return cls(
            settings=OrchestrationSettings.from_raw(raw.get("orchestration") or {}),
            paths=PathsConfig.from_raw(raw.get("paths") or {}),
            gdr=GdrSettings.from_raw(raw.get("gdr_settings") or {}),
            source_path=source_path,
        )


def load_config(path: str | Path | None = None) -> OrchestrationConfig:
    """加载 ``orchestration/config.yaml``，缺省走包内默认.

    缺省时返回全默认 ``OrchestrationConfig``；显式路径不存在则抛 ``FileNotFoundError``.
    """
    if path is None:
        cfg_path = Path(__file__).resolve().parent / "config.yaml"
    else:
        cfg_path = Path(path).resolve()
    if not cfg_path.exists():
        if path is None:
            return OrchestrationConfig()
        raise FileNotFoundError(f"Orchestration config not found: {cfg_path}")
    with cfg_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    return OrchestrationConfig.from_raw(raw, source_path=str(cfg_path))
