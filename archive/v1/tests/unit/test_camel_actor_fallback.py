from __future__ import annotations

import asyncio

import pytest

from simulate_serve.domain.validation import CriterionResult, ValidationReport, Verdict
from simulate_serve.interaction.actor import CamelInteractionActor
from simulate_serve.interaction.models import InteractionContext


class _LeakyMessage:
    content = "内部验证准则 c1 没满足，请按验证器要求补全"


class _LeakyResponse:
    msgs = (_LeakyMessage(),)


class _FakeChatAgent:
    def __init__(self, *args: object, **kwargs: object) -> None:
        return None

    async def astep(self, message: object) -> _LeakyResponse:
        return _LeakyResponse()


class _SlowChatAgent(_FakeChatAgent):
    async def astep(self, message: object) -> _LeakyResponse:
        await asyncio.sleep(5)
        return _LeakyResponse()


@pytest.mark.asyncio
async def test_camel_actor_leak_falls_back_to_variant_pool(compiled_task, monkeypatch) -> None:
    """泄漏输出必须整句丢弃并降级到变体池，而不是进入用户轮。

    历史缺陷：兜底路径引用了 InteractionContext 上不存在的 report 字段，
    一触发即 AttributeError → ACTOR_ERROR，整个 run 报废。
    """
    import camel.agents as camel_agents

    monkeypatch.setattr(camel_agents, "ChatAgent", _FakeChatAgent)
    actor = CamelInteractionActor(model=object())
    report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id=compiled_task.criteria[0].criterion_id,
                verdict=Verdict.FAIL,
                reason_code="X",
                message="结果数量不足",
                retryable=True,
            ),
        ),
        retryable=True,
    )
    context = InteractionContext(task=compiled_task, guide_rounds=0)

    utterance = await actor.create_followup(context, report)

    assert "验证准则" not in utterance.content  # 泄漏文本被整句丢弃
    assert utterance.action == "followup"
    assert utterance.content  # 兜底后仍有可发送的追问内容
    assert utterance.source == "variants"  # 兜底来自变体池，而非报告原文
    assert "结果数量不足" not in utterance.content  # 报告技术话术不进入用户轮


@pytest.mark.asyncio
async def test_camel_actor_timeout_falls_back_to_variant_pool(compiled_task, monkeypatch) -> None:
    """Actor 生成超时必须降级为变体池追问，而不是把整个 run 打成 ACTOR_ERROR。"""
    import camel.agents as camel_agents

    monkeypatch.setattr(camel_agents, "ChatAgent", _SlowChatAgent)
    actor = CamelInteractionActor(model=object(), timeout_seconds=0.05)
    report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id=compiled_task.criteria[0].criterion_id,
                verdict=Verdict.FAIL,
                reason_code="X",
                message="结果数量不足",
                retryable=True,
            ),
        ),
        retryable=True,
    )
    context = InteractionContext(task=compiled_task, guide_rounds=0)

    utterance = await actor.create_followup(context, report)

    assert utterance.action == "followup"
    assert utterance.content  # 降级后仍有可发送的追问内容
    assert utterance.source == "variants"
