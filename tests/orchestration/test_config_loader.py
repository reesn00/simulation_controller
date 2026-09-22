"""orchestration.config_loader 单元测试 (ST-1 重构版).

对应契约: docs/设计方案/pipeline-contracts.md §1.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestration.config_loader import (
    ConfigValidationError,
    OrchestrationConfig,
    load_config,
)
from orchestration.settings import Paths, PipelineSettings


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def test_load_config_default_when_no_file(tmp_path: Path, monkeypatch) -> None:
    """无显式路径: 根配置存在则优先读根配置."""
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({
        "orchestration": {"pipeline": {"max_parallelism": 4}},
        "simulate_serve": {"model": {}},
    }), encoding="utf-8")
    monkeypatch.setenv("SIMCTL_CONFIG", str(fp))
    cfg = load_config()
    assert isinstance(cfg, OrchestrationConfig)
    assert cfg.settings.max_parallelism == 4
    assert cfg.paths.simulate_serve_config == Path(str(fp))
    assert cfg.source_path == str(fp)


def test_load_config_missing_root_config_raises(tmp_path: Path, monkeypatch) -> None:
    """根配置不存在 → FileNotFoundError (无包内兜底)."""
    monkeypatch.setenv("SIMCTL_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError, match="Unified root config not found"):
        load_config()


def test_load_config_root_format_sets_simulate_serve_config(tmp_path: Path) -> None:
    """统一根配置 (含 simulate_serve: 段): simulate_serve_config 指向该文件本身."""
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({
        "llm": {"base_url": "http://llm/v1", "api_key": "k", "model": "m"},
        "simulate_serve": {"model": {"temperature": 0.1}},
        "orchestration": {"pipeline": {"max_parallelism": 5}},
        "paths": {"sqlite_db": "/tmp/x.db"},
    }), encoding="utf-8")
    cfg = load_config(fp)
    assert cfg.settings.max_parallelism == 5
    assert cfg.paths.sqlite_db == Path("/tmp/x.db")
    assert cfg.paths.simulate_serve_config == Path(str(fp))
    assert cfg.source_path == str(fp)


def test_load_config_missing_explicit_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Orchestration config not found"):
        load_config(tmp_path / "nope.yaml")


def test_load_config_overrides(tmp_path: Path) -> None:
    fp = tmp_path / "orch.yaml"
    fp.write_text(yaml.safe_dump({
        "orchestration": {
            "pipeline": {
                "max_parallelism": 7,
                "max_retry_gdr": 8,
                "max_retry_etl": 9,
                "retry_poll_seconds": 1.5,
            },
        },
        "paths": {
            "trajectory_dir": "/tmp/x",
            "sqlite_db": "/tmp/x.db",
        },
    }), encoding="utf-8")
    cfg = load_config(fp)
    assert cfg.settings.max_parallelism == 7
    assert cfg.settings.max_retry_gdr == 8
    assert cfg.settings.max_retry_etl == 9
    assert cfg.settings.retry_poll_seconds == 1.5
    assert cfg.paths.trajectory_dir == Path("/tmp/x")
    assert cfg.paths.sqlite_db == Path("/tmp/x.db")
    assert cfg.source_path == str(fp)


def test_load_config_partial_overrides_keep_defaults(tmp_path: Path) -> None:
    fp = tmp_path / "orch.yaml"
    fp.write_text(yaml.safe_dump({"orchestration": {"pipeline": {"max_parallelism": 99}}}),
                  encoding="utf-8")
    cfg = load_config(fp)
    assert cfg.settings.max_parallelism == 99
    assert cfg.settings.max_retry_etl == 3  # default
    assert cfg.paths.simulate_serve_config == Path(str(fp))


def test_load_config_empty_file_returns_defaults(tmp_path: Path) -> None:
    fp = tmp_path / "orch.yaml"
    fp.write_text("", encoding="utf-8")
    cfg = load_config(fp)
    assert cfg.settings.max_parallelism == 1
    assert cfg.settings.max_retry_gdr == 3
    assert cfg.paths.sqlite_db == Path("output/orchestration/orchestration.db")
    assert cfg.paths.refined_dir == Path("output/refined")


def test_load_config_max_parallelism_zero_raises(tmp_path: Path) -> None:
    fp = tmp_path / "orch.yaml"
    fp.write_text(yaml.safe_dump({"orchestration": {"pipeline": {"max_parallelism": 0}}}),
                  encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="max_parallelism must be ≥ 1"):
        load_config(fp)


def test_load_config_max_parallelism_negative_raises(tmp_path: Path) -> None:
    fp = tmp_path / "orch.yaml"
    fp.write_text(yaml.safe_dump({"orchestration": {"pipeline": {"max_parallelism": -2}}}),
                  encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="max_parallelism must be ≥ 1"):
        load_config(fp)


def test_load_config_retry_poll_seconds_zero_raises(tmp_path: Path) -> None:
    fp = tmp_path / "orch.yaml"
    fp.write_text(yaml.safe_dump({"orchestration": {"pipeline": {"retry_poll_seconds": 0}}}),
                  encoding="utf-8")
    with pytest.raises(ConfigValidationError, match="retry_poll_seconds must be > 0"):
        load_config(fp)


def test_load_config_does_not_create_dirs(tmp_path: Path) -> None:
    """契约 §1.5: load_config 不创建任何目录."""
    fp = tmp_path / "orch.yaml"
    nested = "output/orchestration/somewhere/db.sqlite"
    fp.write_text(yaml.safe_dump({"paths": {"sqlite_db": nested}}), encoding="utf-8")
    cfg = load_config(fp)
    # 路径字段被解析
    assert cfg.paths.sqlite_db == Path(nested)
    # 但实际目录不应该被创建
    assert not (tmp_path / "output").exists()


# ---------------------------------------------------------------------------
# 强类型 dataclass 直接构造
# ---------------------------------------------------------------------------

def test_pipeline_settings_frozen() -> None:
    s = PipelineSettings()
    with pytest.raises(Exception):
        s.max_parallelism = 99  # type: ignore[misc]


def test_paths_frozen() -> None:
    p = Paths(
        simulate_serve_config=Path("c"),
        trajectory_dir=Path("t"),
        runs_dir=Path("r"),
        refined_dir=Path("x"),
        etl_outputs_dir=Path("e"),
        sqlite_db=Path("s"),
        dead_dir=Path("d"),
        pid_file=Path("p"),
        log_dir=Path("l"),
    )
    with pytest.raises(Exception):
        p.sqlite_db = Path("/tmp/x.db")  # type: ignore[misc]


def test_pipeline_settings_defaults() -> None:
    s = PipelineSettings()
    assert s.max_parallelism == 1
    assert s.max_retry_gdr == 3
    assert s.max_retry_etl == 3
    assert s.retry_poll_seconds == 2.0
