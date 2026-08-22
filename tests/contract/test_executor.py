from __future__ import annotations

import json

import httpx
import pytest

from simulate_serve.config import AgentEndpointConfig
from simulate_serve.infrastructure.qwenpaw_client import AsyncQwenPawExecutor, ExecutorError


@pytest.mark.asyncio
async def test_executor_keeps_agent_and_session_contract() -> None:
    requests: list[httpx.Request] = []
    submitted_session = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submitted_session
        requests.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert request.url.path == "/api/console/chat/task"
            assert body["session_id"].startswith("useramulation-")
            assert body["timeout"] == 120.0
            submitted_session = body["session_id"]
            # The real endpoint returns only task_id at submission time.
            return httpx.Response(200, json={"task_id": "rt1"})
        assert request.url.path == "/api/console/chat/task/rt1"
        return httpx.Response(
            200,
            json={
                "status": "finished",
                "result": {
                    "status": "completed",
                    "session_id": submitted_session,
                    "content": [{"type": "text", "text": "<think>secret</think>visible"}],
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(
        AgentEndpointConfig(base_url="https://executor.test", execution_agent_id="agent-a", poll_interval=0.001),
        client,
    )
    response = await executor.open_session("hello")
    assert response.text == "visible"
    assert response.session_id == submitted_session
    assert [item.headers["X-Agent-Id"] for item in requests] == ["agent-a", "agent-a"]
    await client.aclose()


@pytest.mark.asyncio
async def test_executor_continue_sends_session() -> None:
    seen_body = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_body
        if request.method == "POST":
            seen_body = json.loads(request.content)
            return httpx.Response(200, json={"task_id": "rt2"})
        return httpx.Response(200, json={"status": "finished", "result": {"content": "done", "session_id": "s1"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(AgentEndpointConfig(base_url="https://executor.test", poll_interval=0.001), client)
    response = await executor.continue_session("s1", "more")
    assert seen_body["session_id"] == "s1"
    assert response.session_id == "s1"
    await client.aclose()


@pytest.mark.asyncio
async def test_ambiguous_submit_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(AgentEndpointConfig(base_url="https://executor.test", max_retries=9), client)
    with pytest.raises(ExecutorError) as caught:
        await executor.open_session("hello")
    assert caught.value.ambiguous_submit
    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_connect_failure_retries_before_submit() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("not connected", request=request)
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "rt", "session_id": "s"})
        return httpx.Response(200, json={"status": "finished", "result": {"content": "done"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(
        AgentEndpointConfig(base_url="https://executor.test", max_retries=1, poll_interval=0.001),
        client,
    )
    response = await executor.open_session("hello")
    assert response.text == "done"
    assert calls == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_empty_visible_response_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "rt", "session_id": "s"})
        return httpx.Response(200, json={"status": "finished", "result": {"content": "<think>only hidden</think>"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(AgentEndpointConfig(base_url="https://executor.test"), client)
    with pytest.raises(ExecutorError):
        await executor.open_session("hello")
    await client.aclose()


@pytest.mark.asyncio
async def test_executor_ignores_structured_reasoning_and_tool_blocks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "rt", "session_id": "s"})
        return httpx.Response(
            200,
            json={
                "status": "finished",
                "result": {
                    "content": [
                        {"type": "reasoning", "text": "private reasoning"},
                        {"type": "tool_call", "name": "search", "arguments": {"q": "secret"}},
                        {"type": "text", "text": "visible final answer"},
                    ],
                    "session_id": "s",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(AgentEndpointConfig(base_url="https://executor.test"), client)

    response = await executor.open_session("hello")

    assert response.text == "visible final answer"
    assert "private reasoning" not in response.text
    await client.aclose()


@pytest.mark.asyncio
async def test_poll_failure_preserves_accepted_remote_identifiers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "accepted-task", "session_id": "accepted-session"})
        return httpx.Response(200, json={"status": "failed"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(AgentEndpointConfig(base_url="https://executor.test"), client)

    with pytest.raises(ExecutorError) as caught:
        await executor.open_session("hello")

    assert caught.value.remote_task_id == "accepted-task"
    assert caught.value.remote_session_id == "accepted-session"
    await client.aclose()


@pytest.mark.asyncio
async def test_real_nested_failure_envelope_is_rejected() -> None:
    submitted_session = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal submitted_session
        if request.method == "POST":
            submitted_session = json.loads(request.content)["session_id"]
            return httpx.Response(200, json={"task_id": "accepted-task"})
        return httpx.Response(
            200,
            json={
                "status": "finished",
                "result": {
                    "status": "failed",
                    "error": {"message": "LLM provider is unavailable"},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(AgentEndpointConfig(base_url="https://executor.test"), client)

    with pytest.raises(ExecutorError, match="LLM provider is unavailable") as caught:
        await executor.open_session("hello")

    assert caught.value.remote_task_id == "accepted-task"
    assert caught.value.remote_session_id == submitted_session
    await client.aclose()


@pytest.mark.asyncio
async def test_executor_filters_documented_structured_block_types() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "rt", "session_id": "s"})
        return httpx.Response(
            200,
            json={
                "status": "finished",
                "result": {
                    "content": [
                        {"type": "plugin_call", "name": "search", "arguments": {"q": "secret"}},
                        {"type": "plugin_call_output", "output": "secret result"},
                        {"type": "function_call", "name": "calc", "arguments": "{}"},
                        {"type": "function_call_output", "output": "secret output"},
                        {"type": "mcp_tool_call", "name": "mcp_tool", "arguments": "{}"},
                        {"type": "mcp_tool_call_output", "output": "secret mcp output"},
                        {"type": "progress", "text": "in progress"},
                        {"type": "data", "data": {"call_id": "c1", "name": "fn", "arguments": "{}"}},
                        {"type": "text", "text": "visible answer"},
                    ],
                    "session_id": "s",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(AgentEndpointConfig(base_url="https://executor.test"), client)
    response = await executor.open_session("hello")
    assert response.text == "visible answer"
    await client.aclose()


@pytest.mark.asyncio
async def test_executor_extracts_refusal_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "rt", "session_id": "s"})
        return httpx.Response(
            200,
            json={
                "status": "finished",
                "result": {
                    "content": [{"type": "refusal", "refusal": "I cannot help with that request."}],
                    "session_id": "s",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(AgentEndpointConfig(base_url="https://executor.test"), client)
    response = await executor.open_session("hello")
    assert response.text == "I cannot help with that request."
    await client.aclose()


@pytest.mark.asyncio
async def test_executor_sends_message_type_and_metadata() -> None:
    seen_body = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_body
        if request.method == "POST":
            seen_body = json.loads(request.content)
            return httpx.Response(200, json={"task_id": "rt", "session_id": "s"})
        return httpx.Response(200, json={"status": "finished", "result": {"content": "done", "session_id": "s"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(
        AgentEndpointConfig(base_url="https://executor.test", poll_interval=0.001), client
    )
    await executor.open_session("hello")
    assert seen_body["input"][0]["type"] == "message"
    assert seen_body["input"][0]["role"] == "user"
    assert seen_body["metadata"] == {"source": "useramulation"}
    await client.aclose()


@pytest.mark.asyncio
async def test_executor_captures_response_timestamps() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"task_id": "rt", "session_id": "s"})
        return httpx.Response(
            200,
            json={
                "status": "finished",
                "started_at": 1710000000.0,
                "result": {
                    "status": "completed",
                    "session_id": "s",
                    "content": [{"type": "text", "text": "done"}],
                    "created_at": "2026-08-22T10:00:00Z",
                    "completed_at": "2026-08-22T10:01:00Z",
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    executor = AsyncQwenPawExecutor(
        AgentEndpointConfig(base_url="https://executor.test", poll_interval=0.001), client
    )
    response = await executor.open_session("hello")
    assert response.metadata["started_at"] == 1710000000.0
    assert response.metadata["created_at"] == "2026-08-22T10:00:00Z"
    assert response.metadata["completed_at"] == "2026-08-22T10:01:00Z"
    assert response.metadata["status"] == "completed"
    await client.aclose()
