from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from simulate_serve.domain.validation import ValidationReport

from .content_policy import strip_hidden_markup
from .guidance_policy import (
    deterministic_guidance,
    ensure_complete_revision_request,
    guidance_level_for_round,
    select_guidance_gaps,
)
from .models import InteractionContext, UserUtterance
from .prompt_builder import build_followup_prompt, build_system_prompt

logger = logging.getLogger(__name__)


class InteractionActor(Protocol):
    async def create_opening(self, context: InteractionContext) -> UserUtterance: ...

    async def create_followup(self, context: InteractionContext, report: ValidationReport) -> UserUtterance: ...


class DeterministicInteractionActor:
    async def create_opening(self, context: InteractionContext) -> UserUtterance:
        return UserUtterance(content=context.task.task_prompt.strip(), action="open")

    async def create_followup(self, context: InteractionContext, report: ValidationReport) -> UserUtterance:
        content, reasons, targets = deterministic_guidance(
            context.task,
            report,
            context.regressed_criteria,
        )
        return UserUtterance(
            content=content,
            action="followup",
            reason_codes=reasons,
            target_criteria=targets,
            guidance_level=guidance_level_for_round(context.guide_rounds),
        )


class CamelInteractionActor:
    """CAMEL-backed language layer. No validation tools are registered here."""

    def __init__(self, model: object):
        from camel.agents import ChatAgent
        from camel.messages import BaseMessage

        self._base_message = BaseMessage
        self._model = model
        self._agent_type = ChatAgent

    async def create_opening(self, context: InteractionContext) -> UserUtterance:
        # Catalog v2 defines initial_request/task_prompt as the authoritative
        # first user message. Do not let a language model weaken or omit it.
        return UserUtterance(content=context.task.task_prompt.strip(), action="open")

    async def create_followup(self, context: InteractionContext, report: ValidationReport) -> UserUtterance:
        content = await self._generate(context, build_followup_prompt(context, report))
        content = ensure_complete_revision_request(
            context.task,
            content,
            context.regressed_criteria,
        )
        failed = select_guidance_gaps(context.task, report)
        return UserUtterance(
            content=content,
            action="followup",
            reason_codes=tuple(item.reason_code for item in failed),
            target_criteria=tuple(item.criterion_id for item in failed),
            guidance_level=guidance_level_for_round(context.guide_rounds),
        )

    async def _generate(self, context: InteractionContext, prompt: str) -> str:
        agent = self._agent_type(
            system_message=self._base_message.make_assistant_message(
                role_name="UserActor",
                content=build_system_prompt(context),
            ),
            model=self._model,
            tools=None,
        )
        message = self._base_message.make_user_message(role_name="UserActor", content=prompt)
        try:
            if hasattr(agent, "astep"):
                response = await asyncio.wait_for(agent.astep(message), timeout=60)
            else:
                response = await asyncio.wait_for(asyncio.to_thread(agent.step, message), timeout=60)
            raw = response.msgs[0].content if response.msgs else ""
            content = strip_hidden_markup(raw)
            if not content:
                raise ValueError("CAMEL actor returned empty visible content")
            return content
        finally:
            close = getattr(agent, "close", None)
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
