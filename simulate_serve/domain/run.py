from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field

from .completion import CompletionCheck
from .state_machine import RunState
from .validation import ValidationReport


class ConversationTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn_id: str = ""
    role: str
    content: str
    remote_task_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def model_post_init(self, __context: object) -> None:
        if not self.turn_id:
            object.__setattr__(self, "turn_id", f"turn_{uuid.uuid4().hex}")


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = ""
    event_type: str
    from_state: RunState | None = None
    to_state: RunState
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context: object) -> None:
        if not self.event_id:
            object.__setattr__(self, "event_id", f"event_{uuid.uuid4().hex}")


class RunFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    stage: str
    retryable: bool = False


class TaskRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_schema_version: str = "2"
    catalog_schema_version: str = "0"
    run_id: str
    task_id: str
    task_type: str
    dimension: str = ""
    scenario_id: str | None = None
    persona_role: str = ""
    state: RunState = RunState.PENDING
    remote_session_id: str = ""
    remote_task_ids: list[str] = Field(default_factory=list)
    remote_agent_id: str = ""
    executor_turns: int = 0
    guide_rounds: int = 0
    # Guidance level of each follow-up turn, in order; used to keep the
    # simulated user's escalation monotonic within a run.
    guidance_levels: list[str] = Field(default_factory=list)
    conversation: list[ConversationTurn] = Field(default_factory=list)
    state_events: list[RunEvent] = Field(default_factory=list)
    validation_rounds: list[ValidationReport] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    failure: RunFailure | None = None
    rerun_of: str | None = None
    # trajectory 完整性重投计数: BatchRunner 监测 ``completion_check.status``
    # 不为 complete 时递增, 触发原地重跑同 run_id (不创建新 run).
    retry_count: int = 0
    # 来自 ``CompiledTask.max_run_retries``; 落 TaskRun 便于审计每个 run
    # 当时设定的上限 (catalog 配置变更不影响历史 run).
    max_run_retries: int = 0
    # ``simulate_serve.checker`` 在 trajectory 落盘后产出, 既用于本地
    # 重试决策, 也落 ``run.json`` 供下游 / 审计读取.
    completion_check: CompletionCheck | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal
