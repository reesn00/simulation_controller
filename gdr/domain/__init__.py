"""domain: 核心数据契约 (Session/Message/Block* 等 Pydantic 模型)。

所有上下游都通过 `from domain import ...` 访问, 与原 `from schema import ...` 等价。

新架构 ``simulation server → gdr → etl`` 下：
- gdr 通过 ``gdr.parsers.from_trajectory`` 加载 trajectory（C1 契约），
  不再通过本目录的 ``load_session``。``load_session`` 保留供测试 / 回灌使用。
- gdr 末端用 ``save_refined_session`` 写单 C2 refined Session（C2 契约）。
- etl 通过 ``save_session_v2`` 拆 4 视图（C3 契约），4 视图写入实现在
  本目录下，不在 etl 中重复实现。
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
    SessionOutputs,
    StepEditStatus,
    TextBlock,
    ThinkingBlock,
    ToolcallBlock,
    ToolresultBlock,
    ValidationResult,
    load_session,
    locate_block,
    save_refined_session,
    save_session_v2,
)
from domain.scoring_schema import (
    AbsoluteQuality,
    AnchorScoreRecord,
    Breakpoint,
    DiffItem,
    DriftReport,
    FidelityVerdict,
    InstructionAdherence,
    RedlineResult,
    RedlineViolation,
    TrajectoryCompareResult,
    TrajectoryFreeResult,
)

__all__ = [
    "AbsoluteQuality",
    "AnchorScoreRecord",
    "BlockIndex",
    "BlockRefineRecord",
    "BlockUnion",
    "Breakpoint",
    "DefectTag",
    "DiffItem",
    "DriftReport",
    "FidelityVerdict",
    "InstructionAdherence",
    "Message",
    "MessageHealth",
    "RedlineResult",
    "RedlineViolation",
    "RefineLogEntry",
    "Session",
    "SessionOutputs",
    "StepEditStatus",
    "TextBlock",
    "ThinkingBlock",
    "ToolcallBlock",
    "ToolresultBlock",
    "TrajectoryCompareResult",
    "TrajectoryFreeResult",
    "ValidationResult",
    "load_session",
    "locate_block",
    "save_refined_session",
    "save_session_v2",
]