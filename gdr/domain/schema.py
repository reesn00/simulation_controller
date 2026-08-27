import json
from enum import StrEnum
from typing import Literal, Annotated, Optional
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict, model_validator


class DefectTag(StrEnum):
    THOUGHT_TOO_SHORT = "thought_too_short"
    THOUGHT_TOO_LONG = "thought_too_long"
    THOUGHT_BROKEN_LOGIC = "thought_broken_logic"
    TOOL_JSON_INVALID = "tool_json_invalid"
    TOOL_HALLUCINATED = "tool_hallucinated"
    API_HALLUCINATION = "api_hallucination"
    TOOL_WRONG_SELECTION = "tool_wrong_selection"
    REPETITIVE_CALL = "repetitive_call"
    CONTEXT_SWITCH_LOOP = "context_switch_loop"
    OBS_NOISE = "obs_noise"
    OBS_DEBUG_LEAK = "obs_debug_leak"
    # 改进1: Text 块事实性校验
    TEXT_FACT_HALLUCINATION = "text_fact_hallucination"
    # 改进2: 宏观轨迹质量
    MESSAGE_UNHEALTHY = "message_unhealthy"


class StepEditStatus(StrEnum):
    """编辑状态（方案 §5.5）。"""
    UNTOUCHED = "untouched"        # 未修改
    EDITED = "edited"              # 已成功精修
    PRESERVED = "preserved"        # 重要但无法准确修改, 保留原文
    ROLLBACK = "rollback"          # 一致性校验冲突, 已回滚
    NEEDS_REVIEW = "needs_review"  # 无法自动判断, 进入人工审核


class BlockIndex(BaseModel):
    msg_idx: int
    block_idx: int
    block_id: str
    block_type: str


class RefineLogEntry(BaseModel):
    module: str
    attempt: int
    model_used: str
    result: str
    reason: Optional[str] = None


class ValidationResult(BaseModel):
    level: Literal["L1", "L2", "L3"]
    passed: bool
    score: Optional[float] = None
    detail: Optional[str] = None


class BlockRefineRecord(BaseModel):
    block_index: BlockIndex
    module: str
    original_content: dict
    refined_content: Optional[dict] = None
    attempts: int = 0
    result: Literal["success", "failed", "escalated_then_failed", "rollback"] = "failed"
    refine_log: list[RefineLogEntry] = []
    validation_results: list[ValidationResult] = []
    # 方案 §5.5: 编辑状态, 由 reassembler 一致性校验与 metadata 落盘维护
    edit_status: StepEditStatus = StepEditStatus.UNTOUCHED


# 改进2: 宏观轨迹质量评分模型
class MessageHealth(BaseModel):
    """单条 assistant 消息的健康度评分"""
    msg_idx: int
    msg_id: str = ""
    total_toolcalls: int = 0
    success_toolcalls: int = 0
    failed_toolcalls: int = 0
    failures_before_first_success: int = 0
    has_repetitive_loop: bool = False
    has_context_switch_loop: bool = False
    health_score: float = 0.0
    is_healthy: bool = True
    defects: list[str] = Field(default_factory=list)


class ThinkingBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["thinking"]
    id: str
    thinking: str


class ToolcallBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["toolcall"]
    id: str
    name: str
    input: str
    state: Literal["finished"]


class ToolresultBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["toolresult"]
    id: str
    name: str
    output_text: str
    state: Literal["success", "error"]


class TextBlock(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["text"]
    id: str
    text: str


BlockUnion = Annotated[
    ThinkingBlock | ToolcallBlock | ToolresultBlock | TextBlock,
    Field(discriminator="type"),
]


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["user", "assistant"]
    name: str = ""
    id: str
    blocks: list[BlockUnion]
    metadata: dict = Field(default_factory=dict)
    usage: Optional[dict] = None
    error: Optional[str] = None
    created_at: str = ""
    finished_at: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_blocks(cls, data: dict) -> dict:
        """加载时把 dict 形式的 block 统一转成 Pydantic 模型，减少下游分支。"""
        raw_blocks = data.get("blocks") if isinstance(data, dict) else getattr(data, "blocks", None)
        if isinstance(raw_blocks, list):
            data["blocks"] = _parse_blocks(raw_blocks)
        return data


class Session(BaseModel):
    model_config = ConfigDict(extra="allow")
    session_id: str
    source_file: str = ""
    summary: str = ""
    raw_state_keys: list[str] = Field(default_factory=list)
    reply_context: dict = Field(default_factory=dict)
    permission_context: dict = Field(default_factory=dict)
    tool_context_activated_groups: list = Field(default_factory=list)
    messages: list[Message]
    metadata: dict = Field(default_factory=dict)


def _parse_blocks(raw_blocks: list) -> list:
    result = []
    for block in raw_blocks:
        if isinstance(block, (ThinkingBlock, ToolcallBlock, ToolresultBlock, TextBlock)):
            result.append(block)
            continue
        block_type = block.get("type", "") if isinstance(block, dict) else getattr(block, "type", "")
        if block_type == "thinking":
            result.append(ThinkingBlock(**block))
        elif block_type == "toolcall":
            result.append(ToolcallBlock(**block))
        elif block_type == "toolresult":
            result.append(ToolresultBlock(**block))
        elif block_type == "text":
            result.append(TextBlock(**block))
        else:
            result.append(block)
    return result


def load_session(input_path: Path) -> Session:
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    if "messages" in raw:
        parsed_messages = []
        for msg in raw["messages"]:
            if "blocks" in msg:
                msg["blocks"] = _parse_blocks(msg["blocks"])
            parsed_messages.append(Message(**msg))
        raw["messages"] = parsed_messages
    return Session.model_validate(raw)


def save_session(session: Session, output_path: Path) -> None:
    data = session.model_dump(mode="json", exclude_none=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def locate_block(session: Session, block_id: str) -> tuple[int, int] | None:
    for msg_idx, msg in enumerate(session.messages):
        for blk_idx, blk in enumerate(msg.blocks):
            blk_id = blk.get("id", "") if isinstance(blk, dict) else getattr(blk, "id", "")
            if blk_id == block_id:
                return (msg_idx, blk_idx)
    return None