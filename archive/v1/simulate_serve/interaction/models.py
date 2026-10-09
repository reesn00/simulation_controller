from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from simulate_serve.domain.run import ConversationTurn
from simulate_serve.domain.task import CompiledTask


class InteractionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: CompiledTask
    conversation: tuple[ConversationTurn, ...] = ()
    guide_rounds: int = 0
    regressed_criteria: tuple[str, ...] = ()
    # Provenance and guidance-state inputs; computed by TaskRuntime from the
    # run's validation history before each interaction turn.
    run_id: str = ""
    fail_streaks: dict[str, int] = Field(default_factory=dict)
    previous_guidance_level: str | None = None


class UserUtterance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    action: str
    reason_codes: tuple[str, ...] = ()
    target_criteria: tuple[str, ...] = ()
    guidance_level: Literal["L2", "L3", "L4"] | None = None
    # Trace-quality provenance (SFT data filtering): where the user turn came
    # from and which guidance decision produced it.
    source: Literal["llm", "variants"] | None = None
    variant_ids: tuple[str, ...] = ()
    emphasis: Literal["first", "repeat", "regress"] | None = None
    pass_ratio: float | None = None
    verbosity_level: str | None = None
    content_chars: int | None = None
