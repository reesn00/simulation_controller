"""gdr 端 Langfuse 可观测性 helper (PR 3).

工厂位于 ``simulate_serve/observability/langfuse_client.py`` (PR 1 冻结签名);
本包只放 gdr 专用 thin wrapper (21 步通用模板 + thread-local task_id +
per-LLM-call hook):

* ``_gdr_step_span_ctx`` — 21 步骤统一 span 模板 (snapshot(session) →
  input, 退出时 output_capture 拿当前 session → output)
* ``set_current_task_id`` / ``_current_task_id`` — 由 ``gdr_worker.run_gdr_once``
  进入时设置, 退出时清空, 让子步骤 metadata 携带 task_id
* ``maybe_llm_span`` — per-LLM-call generation span (Commit 9,
  ``langfuse_gdr_per_llm_span=True`` 时启用)

调用规约::

    from gdr.observability import _gdr_step_span_ctx, set_current_task_id, maybe_llm_span

零侵入: 工厂 ``get_client(cfg)`` 在 ``langfuse_enabled=False`` 或凭据缺失时
返回 None, 本 helper 检测到 None 后立即 yield None, 业务流程不受影响。
"""

from gdr.observability.runner_helpers import (
    _current_task_id,
    _gdr_step_span_ctx,
    _max_block_payload_bytes,
    _payload_mode,
    _per_step_enabled,
    set_current_task_id,
)

# Commit 9: per-LLM-span helper (延迟 import 避免工厂依赖问题)
def maybe_llm_span(client, *, name: str, model: str, messages=None, metadata=None):
    """延迟导入避免循环引用 (llm_hook 内部 import 工厂)."""
    from gdr.observability.llm_hook import maybe_llm_span as _impl
    return _impl(
        client, name=name, model=model, messages=messages, metadata=metadata,
    )


__all__ = [
    "set_current_task_id",
    "_current_task_id",
    "_payload_mode",
    "_per_step_enabled",
    "_max_block_payload_bytes",
    "_gdr_step_span_ctx",
    "maybe_llm_span",
]
