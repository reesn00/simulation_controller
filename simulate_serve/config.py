from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

PACKAGE_DIR = Path(__file__).parent


class StrictConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_header_safe(field: str, value: str) -> str:
    """Header-bound values (Authorization, X-Agent-Id) must be ASCII.

    Non-ASCII here (e.g. full-width dashes typed with a CJK IME) only surfaces
    as an obscure UnicodeEncodeError deep inside the HTTP client at request
    time; rejecting it at config load makes the misconfiguration obvious.
    """
    if value and not value.isascii():
        raise ValueError(
            f"{field}: contains non-ASCII characters; header-bound config values "
            "must be ASCII (check for full-width punctuation, e.g. '——' vs '--')"
        )
    return value


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
    # Remote session trajectory capture: copies the per-session JSON that
    # QwenPaw persists under its workspace into output/agent_trajectory.
    # Capture failures are logged, never fail a run (auxiliary audit data).
    trajectory_capture_enabled: bool = True
    trajectory_source_dir: str = ""

    @field_validator("execution_agent_id", "validation_agent_id", "auth_token")
    @classmethod
    def _header_fields_are_ascii(cls, value: str, info) -> str:
        return _check_header_safe(f"agent_endpoint.{info.field_name}", value)


class ModelConfig(StrictConfig):
    model_type: str = "OPENAI"
    model_name: str = "gpt-4o"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.7

    @field_validator("api_key")
    @classmethod
    def _api_key_is_ascii(cls, value: str) -> str:
        return _check_header_safe("model.api_key", value)


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
    # enabled=false 进入 record-only 模式：远端照常执行、轨迹照常归档，
    # 但本地不做任何验收，Run 以 INCONCLUSIVE/VALIDATION_DISABLED 终态收场，
    # 永远不会标记 SUCCESS（不污染蒸馏数据集）。
    enabled: bool = True
    semantic_judge_enabled: bool = True
    judge_timeout_seconds: float = Field(default=60.0, gt=0)


class InteractionConfig(StrictConfig):
    # Local thinking models emit reasoning tokens before the visible follow-up;
    # a too-small timeout silently degrades every follow-up to deterministic wording.
    actor_timeout_seconds: float = Field(default=180.0, gt=0)


class AppConfig(StrictConfig):
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent_endpoint: AgentEndpointConfig = Field(default_factory=AgentEndpointConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    interaction: InteractionConfig = Field(default_factory=InteractionConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    max_guide_rounds: int = Field(default=3, ge=0)
    # Drop tasks whose required validation capabilities are unavailable before
    # submitting them to the executor; otherwise they run a full remote round
    # and end in a guaranteed INCONCLUSIVE terminal state.
    skip_unready_tasks: bool = False
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
