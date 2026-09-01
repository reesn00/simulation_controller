"""orchestration.config_loader 单元测试."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestration.config_loader import (
    GdrSettings,
    OrchestrationConfig,
    OrchestrationSettings,
    PathsConfig,
    load_config,
)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def test_load_config_default_when_no_file(tmp_path: Path, monkeypatch) -> None:
    """无显式路径 + 默认文件存在 → 读默认文件."""
    # 默认 config.yaml 在包内；存在即读
    cfg = load_config()
    assert isinstance(cfg, OrchestrationConfig)
    assert cfg.settings.batch_size == 3
    assert cfg.gdr.workers == 2


def test_load_config_missing_explicit_path_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Orchestration config not found"):
        load_config(tmp_path / "nope.yaml")


def test_load_config_overrides(tmp_path: Path) -> None:
    fp = tmp_path / "orch.yaml"
    fp.write_text(yaml.safe_dump({
        "orchestration": {"batch_size": 7, "qf_workers": 8, "gdr_workers": 9},
        "paths": {"trajectory_dir": "/tmp/x", "sqlite_db": "/tmp/x.db"},
        "gdr_settings": {"workers": 5, "llm_concurrency": 11},
    }), encoding="utf-8")
    cfg = load_config(fp)
    assert cfg.settings.batch_size == 7
    assert cfg.settings.qf_workers == 8
    assert cfg.settings.gdr_workers == 9
    assert cfg.paths.trajectory_dir == "/tmp/x"
    assert cfg.paths.sqlite_db == "/tmp/x.db"
    assert cfg.gdr.workers == 5
    assert cfg.gdr.llm_concurrency == 11
    assert cfg.source_path == str(fp)


def test_load_config_partial_overrides_keep_defaults(tmp_path: Path) -> None:
    fp = tmp_path / "orch.yaml"
    fp.write_text(yaml.safe_dump({"orchestration": {"batch_size": 99}}),
                  encoding="utf-8")
    cfg = load_config(fp)
    assert cfg.settings.batch_size == 99
    assert cfg.settings.qf_workers == 4  # default
    assert cfg.paths.simulate_serve_config == "simulate_serve/config/config.yaml"


def test_load_config_empty_file_returns_defaults(tmp_path: Path) -> None:
    fp = tmp_path / "orch.yaml"
    fp.write_text("", encoding="utf-8")
    cfg = load_config(fp)
    assert cfg.settings.batch_size == 3
    assert cfg.gdr.workers == 2


# ---------------------------------------------------------------------------
# 强类型 dataclass 直接构造
# ---------------------------------------------------------------------------

def test_settings_frozen() -> None:
    s = OrchestrationSettings()
    with pytest.raises(Exception):
        s.batch_size = 99  # type: ignore[misc]


def test_paths_frozen() -> None:
    p = PathsConfig()
    with pytest.raises(Exception):
        p.sqlite_db = "/tmp/x.db"  # type: ignore[misc]


def test_gdr_settings_default() -> None:
    g = GdrSettings()
    assert g.workers == 2
    assert g.llm_concurrency == 4
