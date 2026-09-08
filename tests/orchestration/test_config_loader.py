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
    """无显式路径: 根配置存在则优先读根配置."""
    # 仓库根 config/config.yaml 若存在 (本地开发), load_config() 读它;
    # 用 SIMCTL_CONFIG 指向临时文件保证测试确定性。
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({
        "orchestration": {"batch_size": 4},
        "simulate_serve": {"model": {}},
    }), encoding="utf-8")
    monkeypatch.setenv("SIMCTL_CONFIG", str(fp))
    cfg = load_config()
    assert isinstance(cfg, OrchestrationConfig)
    assert cfg.settings.batch_size == 4
    assert cfg.paths.simulate_serve_config == str(fp)


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
        "orchestration": {"batch_size": 5},
        "paths": {"sqlite_db": "/tmp/x.db"},
    }), encoding="utf-8")
    cfg = load_config(fp)
    assert cfg.settings.batch_size == 5
    assert cfg.paths.sqlite_db == "/tmp/x.db"
    assert cfg.paths.simulate_serve_config == str(fp)
    assert cfg.source_path == str(fp)


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
    assert cfg.paths.simulate_serve_config == "config/config.yaml"


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
