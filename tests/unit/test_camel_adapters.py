from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from simulate_serve.config import ModelConfig
from simulate_serve.domain.validation import CriterionResult, ValidationReport, Verdict
from simulate_serve.interaction.actor import CamelInteractionActor
from simulate_serve.interaction.models import InteractionContext
from simulate_serve.infrastructure.camel_model_factory import build_camel_model, model_runtime_configured
from simulate_serve.validation.semantic_judge import CamelSemanticJudge, JudgeResponse


class FakeBaseMessage:
    @staticmethod
    def make_assistant_message(**kwargs):
        return SimpleNamespace(**kwargs)

    @staticmethod
    def make_user_message(**kwargs):
        return SimpleNamespace(**kwargs)


@pytest.mark.asyncio
async def test_camel_actor_preserves_compiled_initial_request_without_model_call(compiled_task) -> None:
    class UnexpectedAgent:
        def __init__(self, **kwargs):
            raise AssertionError("opening must not invoke the language model")

    actor = CamelInteractionActor.__new__(CamelInteractionActor)
    actor._agent_type = UnexpectedAgent
    actor._base_message = FakeBaseMessage
    actor._model = object()

    utterance = await actor.create_opening(InteractionContext(task=compiled_task))

    assert utterance.content == compiled_task.task_prompt


@pytest.mark.asyncio
async def test_camel_followup_strips_hidden_reasoning_and_registers_no_tools(compiled_task) -> None:
    instances = []

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            instances.append(self)

        async def astep(self, message):
            return SimpleNamespace(msgs=[SimpleNamespace(content="<think>hidden</think>请帮我完成任务。")])

        async def close(self):
            self.closed = True

    actor = CamelInteractionActor.__new__(CamelInteractionActor)
    actor._agent_type = FakeAgent
    actor._base_message = FakeBaseMessage
    actor._model = object()
    context = InteractionContext(task=compiled_task)

    criterion = compiled_task.criteria[0]
    report = ValidationReport(
        verdict=Verdict.FAIL,
        criteria=(
            CriterionResult(
                criterion_id=criterion.criterion_id,
                verdict=Verdict.FAIL,
                reason_code="MISSING",
                message="missing",
                retryable=True,
            ),
        ),
        retryable=True,
    )

    utterance = await actor.create_followup(context, report)

    assert utterance.content.startswith("请帮我完成任务。")
    assert "完整的修订结果" in utterance.content
    assert instances[0].kwargs["tools"] is None
    assert instances[0].closed


def test_camel_model_factory_uses_environment_when_builtin_config_is_empty(monkeypatch) -> None:
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setenv("OPENAI_API_KEY", "test-key-from-env")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://model.test/v1")
    monkeypatch.setattr("simulate_serve.infrastructure.camel_model_factory.ModelFactory.create", fake_create)

    result = build_camel_model(ModelConfig(api_key="", base_url=""))

    assert result is not None
    assert captured["api_key"] == "test-key-from-env"
    assert captured["url"] == "https://model.test/v1"
    assert model_runtime_configured(ModelConfig(api_key="", base_url=""))


def test_camel_model_runtime_reports_missing_configuration(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    assert not model_runtime_configured(ModelConfig(api_key="", base_url=""))


@pytest.mark.asyncio
async def test_camel_semantic_judge_accepts_only_exact_criterion_set(compiled_task, monkeypatch) -> None:
    criterion = compiled_task.criteria[0].model_copy(update={"validator": "semantic"})
    created = []

    class FakeJudgeAgent:
        def __init__(self, **kwargs):
            self.closed = False
            created.append(self)

        async def astep(self, message, response_format=None):
            assert response_format is JudgeResponse
            payload = {
                "criteria": [
                    {
                        "criterion_id": criterion.criterion_id,
                        "verdict": "pass",
                        "reason_code": "SUPPORTED",
                        "message": "meets requirement",
                    }
                ]
            }
            return SimpleNamespace(
                msgs=[SimpleNamespace(content="not-json", parsed=JudgeResponse.model_validate(payload))]
            )

        async def close(self):
            self.closed = True

    monkeypatch.setattr("camel.agents.ChatAgent", FakeJudgeAgent)
    judge = CamelSemanticJudge(object(), timeout_seconds=1)

    results = await judge.judge(compiled_task, "answer", (criterion,))

    assert results[0].verdict is Verdict.PASS
    assert created[0].closed


@pytest.mark.asyncio
async def test_camel_semantic_judge_malformed_output_fails_closed(compiled_task, monkeypatch) -> None:
    criterion = compiled_task.criteria[0].model_copy(update={"validator": "semantic"})

    class InvalidJudgeAgent:
        def __init__(self, **kwargs):
            pass

        async def astep(self, message, response_format=None):
            return SimpleNamespace(msgs=[SimpleNamespace(content="not-json")])

        async def close(self):
            return None

    monkeypatch.setattr("camel.agents.ChatAgent", InvalidJudgeAgent)
    judge = CamelSemanticJudge(object(), timeout_seconds=1)

    results = await judge.judge(compiled_task, "answer", (criterion,))

    assert results[0].verdict is Verdict.ERROR
    assert results[0].reason_code == "JUDGE_ERROR"


@pytest.mark.asyncio
async def test_camel_semantic_judge_timeout_does_not_retry(compiled_task, monkeypatch) -> None:
    criterion = compiled_task.criteria[0].model_copy(update={"validator": "semantic"})
    created = []

    class HangingJudgeAgent:
        def __init__(self, **kwargs):
            self.closed = False
            created.append(self)

        async def astep(self, message, response_format=None):
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    monkeypatch.setattr("camel.agents.ChatAgent", HangingJudgeAgent)
    judge = CamelSemanticJudge(object(), timeout_seconds=0.01)

    results = await judge.judge(compiled_task, "answer", (criterion,))

    assert len(created) == 1
    assert created[0].closed
    assert results[0].verdict is Verdict.ERROR
    assert results[0].reason_code == "JUDGE_ERROR"
    assert "TimeoutError" in results[0].message
