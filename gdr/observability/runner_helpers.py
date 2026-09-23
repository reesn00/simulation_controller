"""gdr 端 21 步骤 span 模板 + thread-local task_id (PR 3, 方案 §2.1).

调用规约 (以 retry_loop_clip 为例):

    from gdr.observability import _gdr_step_span_ctx

    with _gdr_step_span_ctx(
        "gdr.retry_loop_clip", session, metadata={"tool": "retry_loop_clip"},
    ):
        removed_clip = clip_session(session, clip_client, ...)

设计要点:
1. 进入时 ``snapshot(session)`` 拿 input (deepcopy, 隔离步骤内修改);
2. 退出时 ``output_capture=lambda: session`` 拿 output (闭包可见, **不要**
   ``if 'session' in locals()`` —— lambda 体在 span __exit__ 时执行, 闭包内
   的 session 始终可见);
3. ``langfuse_enabled=False`` 或凭据缺失时 ``get_client`` 返回 None, helper
   立即 yield None, 业务流程零侵入;
4. ``payload_mode`` 优先读 gdr 扁平字段 ``cfg.langfuse_upload_payload``, 回退
   ``cfg.upload_payload`` (兼容早期嵌套形态), 再回退 ``"full"``;
5. ``max_block_payload_bytes`` 由 ``_maybe_truncate`` 在工厂内执行, 这里只透
   传配置。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any

# 工厂位于 simulate_serve (workspace 成员, PR 1 冻结签名); 仅 import 需要的
# 几个, 不耦合 simulate_serve 业务模块。
from simulate_serve.observability.langfuse_client import (
    get_client,
    snapshot,
    step_span,
)

_TLS = threading.local()


def set_current_task_id(task_id: str | None) -> None:
    """由 ``gdr_worker.run_gdr_once`` 进入时设置, 退出时清空 (finally 清 None)。

    21 步骤子 span 通过 ``_current_task_id()`` 读取并写入 metadata, 让 UI 可
    按 task_id 过滤。Worker 子进程内每个 task 重置 (不再沿用父进程)。
    """
    _TLS.task_id = task_id


def _current_task_id() -> str | None:
    return getattr(_TLS, "task_id", None)


def _payload_mode(cfg) -> str:
    """gdr 端优先读 ``langfuse_upload_payload`` (扁平), 回退 ``upload_payload`` (嵌套),
    再回退 ``"full"``。
    """
    v = getattr(cfg, "langfuse_upload_payload", None)
    if v is not None:
        return v
    return getattr(cfg, "upload_payload", None) or "full"


def _per_step_enabled(cfg) -> bool:
    """per-step span 总开关: ``langfuse_enabled=True`` AND ``per_step_span=True``。
    """
    return bool(getattr(cfg, "langfuse_enabled", False)) and bool(
        getattr(cfg, "langfuse_gdr_per_step_span", True)
    )


def _max_block_payload_bytes(cfg) -> int:
    """per-block payload 上限 (字节); 0 = 不截断。"""
    return int(getattr(cfg, "langfuse_max_block_payload_bytes", 0))


@contextmanager
def _gdr_step_span_ctx(name: str, session, metadata=None, **_ignored):
    """21 步骤模板。进入时 ``snapshot(session)`` → input; 退出时
    ``output_capture=session`` → output。

    边界情况:
    * cfg 缺失或 disabled → yield None, 业务不受影响
    * client 实例化失败 → yield None, 同上
    * span 内 session 被改 → input 已是 deep copy, 不受影响

    签名设计: ``metadata`` 用显式参数名 (而非 ``**metadata``), 避免 ``**metadata``
    把传入的 ``metadata={...}`` 收成 ``metadata["metadata"] = {...}`` 双层包装;
    其他多余 kwargs 被 ``**_ignored`` 吞掉, 调用方写错时尽早暴露。
    """
    cfg = getattr(session, "_gdr_cfg", None)
    if cfg is None or not _per_step_enabled(cfg):
        yield None
        return
    client = get_client(cfg)
    if client is None:
        yield None
        return
    payload_mode = _payload_mode(cfg)
    before = snapshot(session)
    with step_span(
        client,
        name=name,
        payload_mode=payload_mode,
        input_data=before,
        output_capture=lambda: session,
        session_id=getattr(session, "session_id", "") or "",
        task_id=_current_task_id(),
        max_payload_bytes=_max_block_payload_bytes(cfg),
        metadata=dict(metadata or {}),
    ) as span:
        yield span
