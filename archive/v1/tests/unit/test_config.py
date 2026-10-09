from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from simulate_serve.config import PACKAGE_DIR, load_config
from simulate_serve.config import AgentEndpointConfig, ModelConfig


def test_model_config_rejects_non_ascii_api_key() -> None:
    # Full-width dashes (CJK IME) end up in the Authorization header and crash
    # httpx with UnicodeEncodeError at request time; must fail at load instead.
    with pytest.raises(ValidationError, match="model.api_key"):
        ModelConfig(api_key="local——YOUR_API_KEY")


def test_agent_endpoint_rejects_non_ascii_header_fields() -> None:
    with pytest.raises(ValidationError, match="auth_token"):
        AgentEndpointConfig(auth_token="token——值")
    with pytest.raises(ValidationError, match="execution_agent_id"):
        AgentEndpointConfig(execution_agent_id="agent—1")
    with pytest.raises(ValidationError, match="validation_agent_id"):
        AgentEndpointConfig(validation_agent_id="agent—1")


def test_header_fields_accept_ascii_and_empty() -> None:
    model = ModelConfig(api_key="local-your-api-key")
    endpoint = AgentEndpointConfig(execution_agent_id="", validation_agent_id="agent-1", auth_token="")
    assert model.api_key == "local-your-api-key"
    assert endpoint.validation_agent_id == "agent-1"


# ---------------------------------------------------------------------------
# 根配置格式 (统一 config/config.yaml 的 simulate_serve: 段)
# ---------------------------------------------------------------------------

def test_load_config_root_format_merges_llm_defaults(tmp_path) -> None:
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({
        "llm": {"base_url": "http://llm/v1", "api_key": "k", "model": "m1"},
        "simulate_serve": {"model": {"temperature": 0.1}},
    }), encoding="utf-8")
    cfg = load_config(str(fp))
    assert cfg.model.base_url == "http://llm/v1"
    assert cfg.model.api_key == "k"
    assert cfg.model.model_name == "m1"
    assert cfg.model.temperature == 0.1


def test_load_config_root_format_section_overrides_llm(tmp_path) -> None:
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({
        "llm": {"base_url": "http://llm/v1", "api_key": "k", "model": "m1"},
        "simulate_serve": {"model": {"model_name": "m2"}},
    }), encoding="utf-8")
    cfg = load_config(str(fp))
    assert cfg.model.model_name == "m2"          # section 显式值优先
    assert cfg.model.base_url == "http://llm/v1"  # 未写字段继承 llm 段


def test_load_config_root_format_anchors_task_files(tmp_path) -> None:
    fp = tmp_path / "root.yaml"
    fp.write_text(yaml.safe_dump({"simulate_serve": {}}), encoding="utf-8")
    cfg = load_config(str(fp))
    # tasks/scenarios 未写时锚定包内 config 目录 (根配置位于 config/, 不含任务目录)
    assert Path(cfg.tasks_file) == PACKAGE_DIR / "config" / "tasks.yaml"
    assert Path(cfg.scenarios_file) == PACKAGE_DIR / "config" / "scenarios.yaml"


def test_load_config_legacy_flat_format_still_works(tmp_path) -> None:
    fp = tmp_path / "legacy.yaml"
    fp.write_text(yaml.safe_dump({
        "model": {"model_name": "m", "api_key": "k", "base_url": "http://x/v1"},
    }), encoding="utf-8")
    cfg = load_config(str(fp))
    assert cfg.model.model_name == "m"
    assert cfg.model.api_key == "k"
