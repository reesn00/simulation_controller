from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from simulate_serve.domain.validation import ValidationReport

from .content_policy import leaks_internal_rules, strip_hidden_markup
from .guidance_policy import (
    deterministic_guidance,
    ensure_complete_revision_request,
    guidance_level_for_round,
    select_guidance_gaps,
)
from .models import ClosingTrigger, InteractionContext, UserUtterance
from .prompt_builder import build_followup_prompt, build_system_prompt

logger = logging.getLogger(__name__)


class InteractionActor(Protocol):
    async def create_opening(self, context: InteractionContext) -> UserUtterance: ...

    async def create_followup(self, context: InteractionContext, report: ValidationReport) -> UserUtterance: ...

    async def create_closing(self, context: InteractionContext, trigger: ClosingTrigger) -> UserUtterance: ...


# Closing messages are deterministic templates on purpose: no extra LLM call and no
# risk of the language layer weakening the terminal state.
_CLOSING_MESSAGES = {
    ClosingTrigger.PASS: "谢谢，这些内容已经满足我的需要了。",
    ClosingTrigger.ENVIRONMENT_STOP: "好的，我知道这不是你能控制的，我们先到这里。",
    ClosingTrigger.AGENT_DECLINED: "没关系，你能说明做不到的原因也很好，就到这里吧。",
}


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

    async def create_closing(self, context: InteractionContext, trigger: ClosingTrigger) -> UserUtterance:
        return UserUtterance(content=_CLOSING_MESSAGES[trigger], action="closing")


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
        content = await self._generate(context, report, build_followup_prompt(context, report))
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

    async def create_closing(self, context: InteractionContext, trigger: ClosingTrigger) -> UserUtterance:
        # Deterministic template: terminal wording must not depend on model output.
        return UserUtterance(content=_CLOSING_MESSAGES[trigger], action="closing")

    async def _generate(self, context: InteractionContext, report: ValidationReport, prompt: str) -> str:
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
            if context.task.interaction_policy.never_expose_internal_rules and leaks_internal_rules(content):
                # Never let the language layer leak validation internals to the remote
                # agent; fall back to the deterministic guidance wording.
                content, _, _ = deterministic_guidance(
                    context.task,
                    context.report,
                    context.regressed_criteria,
                )
            return content
        finally:
            close = getattr(agent, "close", None)
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
