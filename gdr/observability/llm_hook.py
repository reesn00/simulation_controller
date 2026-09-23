"""gdr 端 per-LLM-span helper (PR 3 / Commit 9).

``maybe_llm_span`` 是 gdr 端 thin wrapper, 调用 ``step_span(..., as_type="generation")``.
仅当 ``langfuse_gdr_per_llm_span=True`` 时才包裹 LLM 调用; 默认 false, 不改原
``LlamaCppClient.chat`` 行为, 避免 100+ spans/session 配额爆炸.

调用规约 (在 ``LlamaCppClient.chat`` 入口):

    if getattr(self._cfg, "langfuse_gdr_per_llm_span", False):
        from gdr.observability.llm_hook import maybe_llm_span
        client = get_client(self._cfg)
        if client is not None:
            with maybe_llm_span(client, name=f"gdr.llm.{self._model}", ...) as span:
                ...
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator


@contextmanager
def maybe_llm_span(
    client: Any,
    *,
    name: str,
    model: str,
    messages: list[dict] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """构造一个 generation span (per-LLM call).

    工厂 ``step_span(client, name=..., as_type="generation", ...)``: 上传 LLM
    call 细节 (model / messages 摘要 / usage). metadata 会自动合并 model + 调用方
    提供字段. 业务异常由工厂 ``step_span`` 处理 (level="ERROR" + status_message).
    """
    from simulate_serve.observability.langfuse_client import step_span

    md = dict(metadata or {})
    md.setdefault("model", model)
    md.setdefault("tool", "llm_chat")

    # messages 摘要 (角色 + 首 100 字符) 而非全文, 避免 input 过大.
    msg_summary = None
    if messages:
        msg_summary = [
            {"role": m.get("role", "?"), "preview": str(m.get("content", ""))[:100]}
            for m in messages
        ]

    with step_span(
        client,
        name=name,
        as_type="generation",
        input_data={"messages": msg_summary} if msg_summary else None,
        metadata=md,
    ) as span:
        yield span
