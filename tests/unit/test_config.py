import pytest
from pydantic import ValidationError

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
