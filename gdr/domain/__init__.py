"""domain: 核心数据契约 (Session/Message/Block* 等 Pydantic 模型)。

所有上下游都通过 `from domain import ...` 访问, 与原 `from schema import ...` 等价。
"""
from domain.schema import (
    BlockIndex,
    BlockRefineRecord,
    BlockUnion,
    DefectTag,
    Message,
    MessageHealth,
    RefineLogEntry,
    Session,
    TextBlock,
    ThinkingBlock,
    ToolcallBlock,
    ToolresultBlock,
    ValidationResult,
    load_session,
    locate_block,
    save_session,
)

__all__ = [
    "BlockIndex",
    "BlockRefineRecord",
    "BlockUnion",
    "DefectTag",
    "Message",
    "MessageHealth",
    "RefineLogEntry",
    "Session",
    "TextBlock",
    "ThinkingBlock",
    "ToolcallBlock",
    "ToolresultBlock",
    "ValidationResult",
    "load_session",
    "locate_block",
    "save_session",
]