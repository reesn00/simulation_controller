"""orchestration.settings: 编排层的强类型 dataclass 定义.

按 ``docs/设计方案/pipeline-contracts.md`` §1 设计, 拆出
``PipelineSettings`` 与 ``Paths`` 两个 frozen dataclass,
由 ``orchestration.config_loader.OrchestrationConfig`` 组合持有.

注意:
- 这里只声明类型, 不做加载逻辑; ``OrchestrationConfig.load_config``
  负责从 YAML 解析并校验.
- ``Paths`` 字段全部是 ``pathlib.Path`` (从 YAML 字符串转换).
- ``load_config`` 不会创建任何目录 (契约 §1.5), 由调用方按需 mkdir.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PipelineSettings:
    """流水线运行时配置 (新架构 simulation server → gdr → etl).

    字段:
        max_parallelism: 同时跑的 task 子进程槽位数 (≥1).
        max_retry_gdr:   gdr 阶段最大重试次数 (≥0).
        max_retry_etl:   etl 阶段最大重试次数 (≥0).
        retry_poll_seconds: 重试循环之间的休眠秒数 (>0).

    校验规则 (契约 §1.5):
        max_parallelism < 1  → ConfigValidationError
        retry_poll_seconds ≤ 0 → ConfigValidationError
    """

    max_parallelism: int = 1
    max_retry_gdr: int = 3
    max_retry_etl: int = 3
    retry_poll_seconds: float = 2.0


@dataclass(frozen=True)
class Paths:
    """编排层涉及的产物 / 状态 / 日志路径集合.

    所有字段都是 ``pathlib.Path``. 默认值与契约 §1.2 一致.
    ``simulate_serve_config`` 在根配置格式下会自动指向该文件本身
    (见 ``config_loader.load_config``).
    """

    simulate_serve_config: Path
    trajectory_dir: Path
    runs_dir: Path
    refined_dir: Path
    etl_outputs_dir: Path
    sqlite_db: Path
    dead_dir: Path
    pid_file: Path
    log_dir: Path


__all__ = ["PipelineSettings", "Paths"]
