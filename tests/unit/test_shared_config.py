"""shared_config (根配置 config/config.yaml 读取工具) 单元测试."""

from __future__ import annotations

from pathlib import Path

import yaml

import shared_config as sc


# ---------------------------------------------------------------------------
# expand_env_placeholders
# ---------------------------------------------------------------------------

def test_expand_env_placeholders_strings_and_containers(monkeypatch) -> None:
    monkeypatch.setenv("SC_FOO", "x")
    value = {
        "a": "${SC_FOO}",
        "b": ["${SC_FOO}", 1, None],
        "c": {"d": "pre-${SC_FOO}-post"},
        "e": "${SC_MISSING_VAR}",
        "f": 42,
    }
    assert sc.expand_env_placeholders(value) == {
        "a": "x",
        "b": ["x", 1, None],
        "c": {"d": "pre-x-post"},
        "e": "",   # 未设置的环境变量展开为空串
        "f": 42,
    }


# ---------------------------------------------------------------------------
# load_yaml_file
# ---------------------------------------------------------------------------

def test_load_yaml_file_expands_placeholders(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SC_KEY", "secret")
    fp = tmp_path / "c.yaml"
    fp.write_text(yaml.safe_dump({"llm": {"api_key": "${SC_KEY}"}}), encoding="utf-8")
    assert sc.load_yaml_file(fp) == {"llm": {"api_key": "secret"}}


def test_load_yaml_file_missing_returns_empty(tmp_path: Path) -> None:
    assert sc.load_yaml_file(tmp_path / "nope.yaml") == {}


def test_load_yaml_file_non_dict_returns_empty(tmp_path: Path) -> None:
    fp = tmp_path / "c.yaml"
    fp.write_text("- a\n- b\n", encoding="utf-8")
    assert sc.load_yaml_file(fp) == {}


# ---------------------------------------------------------------------------
# find_root_config
# ---------------------------------------------------------------------------

def test_find_root_config_env_override(tmp_path: Path, monkeypatch) -> None:
    fp = tmp_path / "root.yaml"
    fp.write_text("llm: {}\n", encoding="utf-8")
    monkeypatch.setenv("SIMCTL_CONFIG", str(fp))
    assert sc.find_root_config() == fp


def test_find_root_config_env_missing_returns_none(tmp_path: Path, monkeypatch) -> None:
    # 显式 env 覆盖失败时不静默回退仓库根默认路径
    monkeypatch.setenv("SIMCTL_CONFIG", str(tmp_path / "nope.yaml"))
    assert sc.find_root_config() is None


def test_find_root_config_repo_default(monkeypatch) -> None:
    # 仓库根 config/config.yaml 在本仓库存在 (gitignored 本地文件)
    monkeypatch.delenv("SIMCTL_CONFIG", raising=False)
    found = sc.find_root_config()
    if sc.ROOT_CONFIG_PATH.is_file():
        assert found == sc.ROOT_CONFIG_PATH
    else:
        assert found is None


# ---------------------------------------------------------------------------
# is_root_config / llm_defaults
# ---------------------------------------------------------------------------

def test_is_root_config() -> None:
    assert sc.is_root_config({"llm": {}}) is True
    assert sc.is_root_config({"simulate_serve": {}}) is True
    assert sc.is_root_config({"model": {}}) is False


def test_llm_defaults() -> None:
    assert sc.llm_defaults({"llm": {"model": "m"}}) == {"model": "m"}
    assert sc.llm_defaults({}) == {}
    assert sc.llm_defaults({"llm": "not-a-dict"}) == {}
