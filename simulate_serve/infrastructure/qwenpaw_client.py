from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import httpx

from simulate_serve.application.ports import ExecutorResponse
from simulate_serve.application.errors import ExecutorPortError
from simulate_serve.config import AgentEndpointConfig
from simulate_serve.interaction.content_policy import INTERNAL_BLOCK_TYPES, strip_hidden_markup

logger = logging.getLogger(__name__)


class ExecutorError(ExecutorPortError):
    pass


class AsyncQwenPawExecutor:
    def __init__(self, config: AgentEndpointConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._base = config.base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.timeout),
            transport=httpx.AsyncHTTPTransport(http1=True, http2=False),
        )

    async def open_session(self, message: str) -> ExecutorResponse:
        # QwenPaw falls back to the fixed session id ``default`` when the
        # caller omits this field. Generate one here so independent runs
        # cannot accidentally share remote conversation history.
        session_id = f"useramulation-{uuid.uuid4().hex}"
        return await self._send(message, session_id)

    async def continue_session(self, session_id: str, message: str) -> ExecutorResponse:
        if not session_id:
            raise ExecutorError("Cannot continue an empty session", stage="submit")
        return await self._send(message, session_id)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _send(self, message: str, session_id: str | None) -> ExecutorResponse:
        agent_id = self.config.execution_agent_id
        remote_task_id, returned_session = await self._submit(message, agent_id, session_id)
        try:
            data = await self._poll(remote_task_id, agent_id)
        except ExecutorPortError as exc:
            exc.remote_task_id = exc.remote_task_id or remote_task_id
            exc.remote_session_id = exc.remote_session_id or returned_session or session_id or ""
            exc.agent_id = exc.agent_id or agent_id
            raise
        result = data.get("result") or {}
        final_session = returned_session or str(result.get("session_id") or session_id or "")
        if not final_session:
            raise ExecutorError("Executor did not return a session_id", stage="poll")
        response_metadata: dict[str, Any] = {"status": result.get("status", "finished")}
        if data.get("started_at") is not None:
            response_metadata["started_at"] = data["started_at"]
        if result.get("created_at") is not None:
            response_metadata["created_at"] = result["created_at"]
        if result.get("completed_at") is not None:
            response_metadata["completed_at"] = result["completed_at"]
        return ExecutorResponse(
            text=self._extract_text(result),
            session_id=final_session,
            remote_task_id=remote_task_id,
            agent_id=agent_id,
            metadata=response_metadata,
        )

    async def _submit(self, message: str, agent_id: str, session_id: str | None) -> tuple[str, str]:
        body: dict[str, Any] = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "text", "text": message}],
                }
            ],
            "user_id": self.config.user_id,
            "timeout": self.config.task_timeout,
            "metadata": {"source": "useramulation"},
        }
        if session_id is not None:
            body["session_id"] = session_id
        data: dict[str, Any] | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = await self._client.post(
                    f"{self._base}/api/console/chat/task",
                    json=body,
                    headers=self._headers(agent_id),
                )
                response.raise_for_status()
                data = response.json()
                break
            except httpx.ConnectError as exc:
                # No connection was established, so this is the only safe automatic POST retry.
                if attempt >= self.config.max_retries:
                    raise ExecutorError(str(exc), stage="submit", retryable=True, ambiguous_submit=False) from exc
                await asyncio.sleep(min(0.25 * (2**attempt), 2.0))
            except (httpx.TimeoutException, httpx.HTTPError) as exc:
                # The server may have accepted a POST. Without an idempotency key, do not retry.
                raise ExecutorError(str(exc), stage="submit", retryable=False, ambiguous_submit=True) from exc
            except ValueError as exc:
                raise ExecutorError("Invalid JSON submit response", stage="submit") from exc
        if data is None:
            raise ExecutorError("Submit failed before a response was received", stage="submit", retryable=True)
        task_id = data.get("task_id")
        if not task_id:
            raise ExecutorError("Submit response did not contain task_id", stage="submit")
        return str(task_id), str(data.get("session_id") or "")

    async def _poll(self, task_id: str, agent_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.max_poll_time
        url = f"{self._base}/api/console/chat/task/{task_id}"
        while time.monotonic() < deadline:
            try:
                response = await self._client.get(url, headers=self._headers(agent_id))
                response.raise_for_status()
                data = response.json()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                await asyncio.sleep(self.config.poll_interval)
                continue
            except (httpx.HTTPError, ValueError) as exc:
                raise ExecutorError(str(exc), stage="poll") from exc
            status = data.get("status")
            if status in {"failed", "cancelled"}:
                raise ExecutorError(f"Remote task {task_id} ended with {status}", stage="poll")
            if status == "finished":
                result = data.get("result") or {}
                if result.get("status") in {"failed", "cancelled"}:
                    error = result.get("error") or {}
                    raise ExecutorError(str(error.get("message") or "Remote execution failed"), stage="poll")
                return data
            await asyncio.sleep(self.config.poll_interval)
        raise ExecutorError(f"Remote task {task_id} timed out", stage="poll", retryable=True)

    def _headers(self, agent_id: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if agent_id:
            headers["X-Agent-Id"] = agent_id
        if self.config.auth_token:
            headers["Authorization"] = f"Bearer {self.config.auth_token}"
        return headers

    @staticmethod
    def _extract_text(result: dict[str, Any]) -> str:
        blocks = result.get("content") or result.get("output") or []
        if isinstance(blocks, str):
            text = blocks
        else:
            parts: list[str] = []
            for block in blocks if isinstance(blocks, list) else []:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    block_type = str(block.get("type") or "").casefold()
                    if block_type in INTERNAL_BLOCK_TYPES:
                        continue
                    if block_type == "text":
                        parts.append(str(block.get("text") or ""))
                        continue
                    if block_type == "refusal":
                        parts.append(str(block.get("refusal") or ""))
                        continue
                    for item in block.get("content") or []:
                        if (
                            isinstance(item, dict)
                            and str(item.get("type") or "").casefold() == "text"
                        ):
                            parts.append(str(item.get("text") or ""))
            text = "\n".join(parts)
        visible = strip_hidden_markup(text)
        if not visible:
            raise ExecutorError("Remote response has no visible final text", stage="extract")
        return visible
