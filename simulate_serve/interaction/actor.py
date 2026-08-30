from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from simulate_serve.domain.task import CompiledTask
from simulate_serve.domain.validation import ValidationReport

from .content_policy import leaks_internal_rules, overlaps_internal_text, strip_hidden_markup
from .guidance_policy import (
    build_guidance_directive,
    ensure_complete_revision_request,
    variant_pool_utterance,
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

# Minimum length for a text to take part in the criterion-echo gate: shorter
# strings produce trivially high containment ratios.
_ECHO_MIN_TEXT_LENGTH = 12


def _internal_texts(task: CompiledTask) -> tuple[str, ...]:
    """Harness-owned wording the user turn must not echo (P1-1 exit gate).

    Criterion descriptions and remediation guidance are internal repair
    semantics. Requirement texts are excluded on purpose: restating what the
    user already asked for is legitimate user behaviour, not a leak.
    """
    texts = [criterion.description for criterion in task.criteria]
    texts += [
        criterion.remediation.guidance
        for criterion in task.criteria
        if criterion.remediation is not None and criterion.remediation.guidance
    ]
    return tuple(text for text in texts if len(text) >= _ECHO_MIN_TEXT_LENGTH)


class _SurfaceRejected(Exception):
    """The language-layer output failed an exit gate and must be discarded."""


class DeterministicInteractionActor:
    async def create_opening(self, context: InteractionContext) -> UserUtterance:
        return UserUtterance(content=context.task.task_prompt.strip(), action="open")

    async def create_followup(self, context: InteractionContext, report: ValidationReport) -> UserUtterance:
        directive = build_guidance_directive(context, report)
        return variant_pool_utterance(context, directive)

    async def create_closing(self, context: InteractionContext, trigger: ClosingTrigger) -> UserUtterance:
        return UserUtterance(content=_CLOSING_MESSAGES[trigger], action="closing")


class CamelInteractionActor:
    """CAMEL-backed language layer. No validation tools are registered here.

    Surface-realization contract: the LLM paraphrases from colloquial gap
    semantics; every output passes the exit gates (keyword leak, criterion
    echo, verbosity budget) and any rejection falls back to the variant pool,
    so a language-layer failure degrades diversity, never quality.
    """

    # Class-level default keeps __new__-constructed test instances working.
    _timeout_seconds: float = 180.0

    def __init__(self, model: object, *, timeout_seconds: float = 180.0):
        from camel.agents import ChatAgent
        from camel.messages import BaseMessage

        self._base_message = BaseMessage
        self._model = model
        self._agent_type = ChatAgent
        self._timeout_seconds = timeout_seconds

    async def create_opening(self, context: InteractionContext) -> UserUtterance:
        # Catalog v2 defines initial_request/task_prompt as the authoritative
        # first user message. Do not let a language model weaken or omit it.
        return UserUtterance(content=context.task.task_prompt.strip(), action="open")

    async def create_followup(self, context: InteractionContext, report: ValidationReport) -> UserUtterance:
        directive = build_guidance_directive(context, report)
        try:
            content = await self._generate(context, report, directive)
        except Exception as exc:
            # A language-layer failure (slow model timeout, connection error,
            # empty output, exit-gate rejection) must not destroy an otherwise
            # valid run: discard the output and fall back to the variant pool.
            logger.warning(
                "CAMEL actor followup rejected (%s: %s); using variant pool guidance",
                type(exc).__name__,
                exc,
            )
            return variant_pool_utterance(context, directive)
        content = ensure_complete_revision_request(
            context.task,
            content,
            context.regressed_criteria,
        )
        return UserUtterance(
            content=content,
            action="followup",
            reason_codes=directive.reason_codes,
            target_criteria=directive.target_criteria,
            guidance_level=directive.guidance_level,
            source="llm",
            emphasis=directive.emphasis,
            pass_ratio=directive.pass_ratio,
            verbosity_level=directive.verbosity_level,
            content_chars=len(content),
        )

    async def create_closing(self, context: InteractionContext, trigger: ClosingTrigger) -> UserUtterance:
        # Deterministic template: terminal wording must not depend on model output.
        return UserUtterance(content=_CLOSING_MESSAGES[trigger], action="closing")

    async def _generate(self, context: InteractionContext, report: ValidationReport, directive) -> str:
        agent = self._agent_type(
            system_message=self._base_message.make_assistant_message(
                role_name="UserActor",
                content=build_system_prompt(context),
            ),
            model=self._model,
            tools=None,
        )
        try:
            content = await self._complete(agent, build_followup_prompt(context, report))
            if not content:
                raise ValueError("CAMEL actor returned empty visible content")
            self._check_gates(context, content)
            budget = directive.verbosity_budget
            if len(content) > budget:
                # One compression retry keeps a good paraphrase that is merely
                # verbose; a second failure falls back to the variant pool.
                content = await self._complete(
                    agent,
                    f"你刚才说的话太长了，请压缩到{budget}字以内，保留全部要点，直接输出压缩后的话。",
                )
                if not content:
                    raise ValueError("CAMEL actor compression returned empty content")
                self._check_gates(context, content)
                if len(content) > budget:
                    raise _SurfaceRejected(f"content still exceeds verbosity budget {budget}")
            return content
        finally:
            close = getattr(agent, "close", None)
            if close:
                result = close()
                if asyncio.iscoroutine(result):
                    await result

    @staticmethod
    def _check_gates(context: InteractionContext, content: str) -> None:
        if context.task.interaction_policy.never_expose_internal_rules and leaks_internal_rules(content):
            raise _SurfaceRejected("keyword leak")
        if overlaps_internal_text(content, _internal_texts(context.task)):
            raise _SurfaceRejected("criterion echo")

    async def _complete(self, agent, prompt: str) -> str:
        message = self._base_message.make_user_message(role_name="UserActor", content=prompt)
        if hasattr(agent, "astep"):
            response = await asyncio.wait_for(agent.astep(message), timeout=self._timeout_seconds)
        else:
            response = await asyncio.wait_for(
                asyncio.to_thread(agent.step, message), timeout=self._timeout_seconds
            )
        raw = response.msgs[0].content if response.msgs else ""
        return strip_hidden_markup(raw)
