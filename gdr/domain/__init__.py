"""domain: 核心数据契约 (Session/Message/Block* 等 Pydantic 模型)。

所有上下游都通过 `from domain import ...` 访问, 与原 `from schema import ...` 等价。

GDR 仅消费 etl/qwenformat 导出的 qf_out 格式 (单 Session JSON,
``messages[*].blocks`` 形态), 由 ``load_session`` 直接加载. 不再支持
直接读取原始 trajectory JSONL —— 该加载路径已删除, 旧调用方走 dead 路径.
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
    StepEditStatus,
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
    "StepEditStatus",
    "TextBlock",
    "ThinkingBlock",
    "ToolcallBlock",
    "ToolresultBlock",
    "ValidationResult",
    "load_session",
    "locate_block",
    "save_session",
]