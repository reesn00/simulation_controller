from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

PACKAGE_DIR = Path(__file__).parent


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentEndpointConfig(StrictConfig):
    base_url: str = "http://localhost:8088"
    execution_agent_id: str = ""
    validation_agent_id: str = ""
    auth_token: str = ""
    user_id: str = "useramulation"
    timeout: float = Field(default=30.0, gt=0)
    task_timeout: float = Field(default=120.0, gt=0)
    poll_interval: float = Field(default=1.0, gt=0)
    max_poll_time: float = Field(default=180.0, gt=0)
    max_retries: int = Field(default=2, ge=0)


class ModelConfig(StrictConfig):
    model_type: str = "OPENAI"
    model_name: str = "gpt-4o"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.7


class ToolProviderConfig(StrictConfig):
    name: str
    type: str
    enabled: bool = False
    required: bool = False
    priority: int = 0
    capabilities: list[str] = Field(default_factory=list)
    allowed_task_types: list[str] = Field(default_factory=list)
    startup_timeout_seconds: float = Field(default=20.0, gt=0)
    call_timeout_seconds: float = Field(default=30.0, gt=0)
    max_concurrency: int = Field(default=1, ge=1)
    config: dict[str, Any] = Field(default_factory=dict)


class ToolsConfig(StrictConfig):
    startup_policy: Literal["warn", "strict"] = "warn"
    providers: list[ToolProviderConfig] = Field(default_factory=list)


class ValidationConfig(StrictConfig):
    semantic_judge_enabled: bool = True
    judge_timeout_seconds: float = Field(default=60.0, gt=0)


class AppConfig(StrictConfig):
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent_endpoint: AgentEndpointConfig = Field(default_factory=AgentEndpointConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    max_guide_rounds: int = Field(default=3, ge=0)
    output_dir: str = "output"
    tasks_file: str = "tasks.yaml"
    scenarios_file: str = "scenarios.yaml"
    source_path: str = Field(default="", exclude=True)

    @property
    def config_dir(self) -> Path:
        return Path(self.source_path).resolve().parent if self.source_path else PACKAGE_DIR / "config"


def load_config(path: str | None = None) -> AppConfig:
    """Load app configuration; missing explicit paths fail, missing implicit default returns defaults."""
    config_path = Path(path).resolve() if path else PACKAGE_DIR / "config" / "config.yaml"
    if not config_path.exists():
        if path:
            raise FileNotFoundError(f"Config file not found: {config_path}")
        return AppConfig(source_path=str(config_path))
    with config_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream) or {}
    return AppConfig.model_validate({**raw, "source_path": str(config_path)})
