import os

from camel.models import ModelFactory
from camel.types import ModelPlatformType, ModelType

from simulate_serve.config import ModelConfig


def _connection_values(config: ModelConfig) -> tuple[str | None, str | None]:
    if config.model_type.upper() == "ANTHROPIC":
        return (
            config.api_key or os.getenv("ANTHROPIC_API_KEY") or None,
            config.base_url or os.getenv("ANTHROPIC_BASE_URL") or None,
        )
    return (
        config.api_key or os.getenv("OPENAI_API_KEY") or None,
        config.base_url or os.getenv("OPENAI_BASE_URL") or None,
    )


def model_runtime_configured(config: ModelConfig) -> bool:
    api_key, base_url = _connection_values(config)
    return bool(api_key or base_url)


def build_camel_model(config: ModelConfig, *, temperature: float | None = None):
    api_key, base_url = _connection_values(config)
    if config.model_type.upper() == "ANTHROPIC":
        platform = ModelPlatformType.ANTHROPIC
        model_type = config.model_name or ModelType.CLAUDE_3_5_SONNET
    else:
        platform = ModelPlatformType.OPENAI_COMPATIBLE_MODEL
        model_type = config.model_name
    return ModelFactory.create(
        model_platform=platform,
        model_type=model_type,
        model_config_dict={"temperature": config.temperature if temperature is None else temperature},
        api_key=api_key,
        url=base_url,
    )
