"""orchestration.errors: 跨模块共享的异常类型."""

from __future__ import annotations


class NonRetryableError(RuntimeError):
    """永久性错误：重试不会改变结果（文件损坏 / 格式不符 / 输入缺失）.

    worker 抛出该异常时，``base_worker`` 会让 ``queue.mark_failed``
    直接入 dead（不消耗 attempts），避免给永久错误白跑重试
    （gdr 阶段每次重试都是真实 LLM 调用费用）。
    """
