from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from simulate_serve.config import ModelConfig


class ToolStatus(str, Enum):
    DISABLED = "disabled"
    READY = "ready"
    DEPENDENCY_MISSING = "dependency_missing"
    INIT_FAILED = "init_failed"
    CONNECT_FAILED = "connect_failed"
    SCHEMA_INVALID = "schema_invalid"
    PROBE_FAILED = "probe_failed"
    SHUTDOWN_FAILED = "shutdown_failed"


class ToolDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    provider_type: str
    enabled: bool = False
    required: bool = False
    priority: int = 0
    capabilities: frozenset[str] = frozenset()
    allowed_task_types: frozenset[str] = frozenset()
    startup_timeout_seconds: float = 20
    call_timeout_seconds: float = 30
    max_concurrency: int = 1
    model: ModelConfig | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ToolHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    provider_type: str
    required: bool
    status: ToolStatus
    reason: str = ""
    tool_count: int = 0
    duration_ms: int = 0


class ToolReadinessReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: tuple[ToolHealth, ...]

    @property
    def required_failures(self) -> tuple[ToolHealth, ...]:
        return tuple(item for item in self.tools if item.required and item.status is not ToolStatus.READY)

    def render(self) -> str:
        lines = ["Tool readiness"]
        for item in self.tools:
            reason = f" reason={item.reason}" if item.reason else ""
            lines.append(
                f"  {item.name:<14} {item.status.value.upper():<20} "
                f"required={str(item.required).lower()} tools={item.tool_count} duration={item.duration_ms}ms{reason}"
            )
        return "\n".join(lines)
