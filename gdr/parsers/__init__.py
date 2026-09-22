"""gdr.parsers — C1 契约入口.

新架构 ``simulation server → gdr → etl`` 下，gdr 不再消费 etl/qwenformat 的 qf_out
产物，而是直接读 trajectory 事件流（C1 契约，详见 ``docs/contracts/C1-trajectory-events.md``），
轻量解析为 ``gdr.domain.Session`` 后进入 refine 流水线。

本包是 gdr 对 C1 契约的**唯一入口**：所有 gdr 内部代码必须通过本包加载
trajectory，不得直接 ``import etl.qwenformat.load``。

约定：未来若把 parser 从 etl 迁出，本包内部对 ``etl.qwenformat.load.load_trajectory``
的引用是唯一改动点，其他位置不需调整。
"""
from __future__ import annotations

from pathlib import Path

from gdr.domain.schema import Session


def from_trajectory(path: Path) -> Session:
    """把 trajectory JSONL 事件流（C1 契约）解析为 gdr Session.

    步骤：
      1. ``etl.qwenformat.load.load_trajectory(path)`` 重放事件流为
         ``SessionRecord``（dataclass）
      2. ``SessionRecord.to_session_dict()`` 转 dict
      3. ``Session.model_validate(...)`` 走 pydantic 校验生成最终对象

    该函数**只解析 blocks**，不做 system prompt partition / tool template /
    tool output summarization / qf_text 渲染——这些是 etl 阶段职责。
    """
    # 局部导入: 避免 gdr.parsers → etl.qwenformat.load 的循环依赖 (gdr domain
    # 也可能被 etl 通过 gdr.domain.schema 反向引用)
    from etl.qwenformat.load import load_trajectory

    record = load_trajectory(path)
    return Session.model_validate(record.to_session_dict())


__all__ = ["from_trajectory"]