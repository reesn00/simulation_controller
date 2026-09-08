"""gdr Settings 统一根配置 source (仓库根 config/config.yaml gdr: 段) 测试."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from config import Settings


def test_root_config_llm_fallback_and_section_override(tmp_path, monkeypatch):
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({
        "llm": {"base_url": "http://llm/v1", "api_key": "k", "model": "mm"},
        "gdr": {"judge_min_score": 9, "embedding_endpoint_model": "emb-x"},
    }), encoding="utf-8")
    monkeypatch.setenv("GDR_CONFIG_FILE", str(fp))
    s = Settings()
    # llm 共享段缺省映射
    assert s.llm_base_url == "http://llm/v1"
    assert s.llm_api_key == "k"
    assert s.main_model == "mm"
    assert s.tool_model == "mm"
    assert s.judge_model == "mm"
    # gdr: 段显式覆盖
    assert s.judge_min_score == 9
    assert s.embedding_endpoint_model == "emb-x"
    # 根配置未提的字段走代码默认值
    assert s.llm_timeout_s == 120


def test_missing_root_config_raises(tmp_path, monkeypatch):
    # 根配置缺失 → FileNotFoundError (无包内兜底)
    monkeypatch.setenv("GDR_CONFIG_FILE", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError, match="Unified root config not found"):
        Settings()


def test_root_config_env_overrides_root_file(tmp_path, monkeypatch):
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({
        "llm": {"base_url": "http://llm/v1", "model": "mm"},
    }), encoding="utf-8")
    monkeypatch.setenv("GDR_CONFIG_FILE", str(fp))
    monkeypatch.setenv("GDR_MAIN_MODEL", "env-model")
    s = Settings()
    # env (GDR_*) 优先于根配置
    assert s.main_model == "env-model"


def test_root_config_placeholder_expansion(tmp_path, monkeypatch):
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({
        "llm": {"base_url": "http://llm/v1", "api_key": "${TEST_GDR_KEY}", "model": "mm"},
    }), encoding="utf-8")
    monkeypatch.setenv("GDR_CONFIG_FILE", str(fp))
    monkeypatch.setenv("TEST_GDR_KEY", "expanded")
    s = Settings()
    assert s.llm_api_key == "expanded"
