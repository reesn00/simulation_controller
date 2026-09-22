"""simulate_serve.domain.completion — trajectory 完整性判定值对象。

在 ``simulation server → gdr → etl`` 架构下, ``CompletionCheck`` 由
``simulate_serve.checker`` 在 trajectory 落盘后产出, 落 ``TaskRun.completion_check``
字段, 既供本进程重试决策, 也供后续 ``runs/<run_id>/run.json`` 审计读出。

pydantic BaseModel 以复用 ``JsonRunRepository`` 的 ``model_dump(mode="json")``
序列化路径, 与 ``TaskRun`` / ``RunFailure`` 保持一致。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class CompletionCheck(BaseModel):
    """trajectory 完整性判定结果 — 落 ``run.json.completion_check`` 字段。

    status 取值:
      - ``complete``:   agent 给出结束任务内容 (不论目标是否达成)
      - ``incomplete``: trajectory 仍在工具循环 / 末段 text 被截断 / 无终态事件
      - ``aborted``:    终态事件为 ``error`` / ``cancel``, agent 未正常结束

    retryable:
      - complete / aborted → False (不应再投)
      - incomplete → True (可重投 simulate_serve)
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["complete", "incomplete", "aborted"]
    reasons: tuple[str, ...]
    last_event_type: str
    has_final_reply: bool
    terminal_event_seen: bool
    last_block_type: str
    final_text_preview: str
    summary: str
    retryable: bool
    detected_at: str


__all__ = ["CompletionCheck"]