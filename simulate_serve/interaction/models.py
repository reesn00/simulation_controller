from typing import Literal

from pydantic import BaseModel, ConfigDict

from simulate_serve.domain.run import ConversationTurn
from simulate_serve.domain.task import CompiledTask


class InteractionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task: CompiledTask
    conversation: tuple[ConversationTurn, ...] = ()
    guide_rounds: int = 0
    regressed_criteria: tuple[str, ...] = ()


class UserUtterance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    action: str
    reason_codes: tuple[str, ...] = ()
    target_criteria: tuple[str, ...] = ()
    guidance_level: Literal["L2", "L3", "L4"] | None = None
