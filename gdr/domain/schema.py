import json
from dataclasses import dataclass
from enum import StrEnum
from types import SimpleNamespace
from typing import Any, Literal, Annotated, Optional
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
    # P0-1.3: 工具与 user 初始意图无关（agent "为展示能力"调用的无关工具）
    TOOL_OFF_TOPIC = "tool_off_topic"


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

    @model_validator(mode="before")
    @classmethod
    def _normalize_state(cls, data: dict) -> dict:
        """上游存在 success/error 之外的状态 (如审批拒绝的 denied), 统一映射为
        error, 原值保留在 original_state 中。"""
        if isinstance(data, dict):
            state = data.get("state")
            if state is not None and state not in ("success", "error"):
                data = {**data, "original_state": state, "state": "error"}
        return data


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
    role: Literal["system", "user", "assistant"]
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
    run_id: str = ""
    source_file: str = ""
    summary: str = ""
    raw_state_keys: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)
    event_count: int = 0
    event_types: dict[str, int] = Field(default_factory=dict)
    model_name: str = ""
    provider_id: str = ""
    agent_id: str = ""
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
    """加载 etl/qwenformat 导出的 qf_out 格式 (单 Session JSON, ``messages[*].blocks``).

    这是 GDR pipeline runner 唯一接受的输入格式. 旧 ``load_trajectory`` (从
    原始 trajectory JSONL 直接加载) 已删除 —— 按用户主旨: GDR 不应绕开 etl
    直接消费原始 trajectory; etl 的产物 qf_out 才是单一真相来源.
    """
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    if "messages" in raw:
        parsed_messages = []
        for msg in raw["messages"]:
            if "blocks" in msg:
                msg["blocks"] = _parse_blocks(msg["blocks"])
            parsed_messages.append(Message(**msg))
        raw["messages"] = parsed_messages
    return Session.model_validate(raw)


@dataclass(frozen=True)
class SessionOutputs:
    """``save_session`` 拆出的 4 份视图文件路径；``qwenjina`` 可为 None."""

    messages: Path
    openai: Path
    qwenjina: Optional[Path]
    meta: Path


def save_session(session: Session, base_path: Path) -> SessionOutputs:
    """把 refined Session 按视图拆成 4 份文件写入 ``base_path`` 所在目录.

    ``base_path`` 是无扩展名 stem（如 ``.../xxx_refined``），4 份文件共用同一
    stem、仅尾缀与扩展名不同：

    * ``<base>.messages.json``  — blocks 视图（``{"messages": [...], "tools": [...]}``）
    * ``<base>.openai.json``    — OpenAI function-calling 视图（顶层带 ``tools``）
    * ``<base>.qwenjina.txt``   — Qwen3 chat_template 纯文本（qf_text 已含 tools
                                  文本化，渲染阶段传入）；qf_text 缺失则不写
    * ``<base>.meta.json``      — 审计 metadata 全量 + session_id

    F1 fix: tools 字段同时写入 messages.json 与 openai.json 顶层, 受
    ``include_tools_in_payloads`` 与 ``tools_payload_max`` 控制. qwenjina.txt
    由 ``transform.render_sample_text`` 渲染时已传 tools, 不需重复注入.

    F3-D 强化: 对 ``messages.json`` / ``openai.json`` / ``qwenjina.txt`` 三份
    训练数据落地文件做 ⟦...⟧ 元注释剥离; ``meta.json`` 额外记录
    ``meta_tag_contamination`` 字段供观测 (出现次数与路径).
    """
    # 局部导入避免循环依赖 (schema 是 gdr.domain 的最底层)
    from gdr.refiners.meta_tag_strip import (
        annotate_meta_tags,
        strip_meta_tags,
        strip_session_payload,
    )

    base_path = Path(base_path)
    base_path.parent.mkdir(parents=True, exist_ok=True)

    tools_payload = _extract_tools_payload(session)

    # 1. messages.json: 递归剥离 ⟦⟧
    messages_path = Path(str(base_path) + ".messages.json")
    raw_messages_payload: dict[str, Any] = {
        "messages": [m.model_dump(mode="json", exclude_none=True) for m in session.messages],
    }
    payload = strip_session_payload(raw_messages_payload)
    if tools_payload is not None:
        payload["tools"] = tools_payload
    messages_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    # 2. openai.json: 递归剥离 ⟦⟧
    openai_path = Path(str(base_path) + ".openai.json")
    raw_openai_payload: dict[str, Any] = {
        "openai_messages": (session.metadata or {}).get("openai_messages", []),
    }
    openai_payload = strip_session_payload(raw_openai_payload)
    if tools_payload is not None:
        openai_payload["tools"] = tools_payload
    openai_path.write_text(
        json.dumps(openai_payload, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    # 3. qwenjina.txt: 纯文本剥离 ⟦⟧
    qf_text = (session.metadata or {}).get("qf_text")
    qwenjina_path: Optional[Path] = None
    if qf_text:
        cleaned_qf_text = strip_meta_tags(str(qf_text))
        qwenjina_path = Path(str(base_path) + ".qwenjina.txt")
        qwenjina_path.write_text(cleaned_qf_text, encoding="utf-8")
    # 4. meta.json: 不剥离, 但扫描 3 份输出文件 + session 原始内容, 写入
    # ``meta_tag_contamination`` 字段, 含 has_meta_tag / total_count /
    # occurrences (path, tag, char_offset)
    meta = dict(session.metadata or {})
    meta["session_id"] = session.session_id
    meta["meta_tag_contamination"] = annotate_meta_tags(
        payload,
        openai_payload,
        cleaned_qf_text if qf_text else "",
        session.model_dump(mode="json", exclude_none=True),
    )
    meta_path = Path(str(base_path) + ".meta.json")
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
    )

    return SessionOutputs(
        messages=messages_path,
        openai=openai_path,
        qwenjina=qwenjina_path,
        meta=meta_path,
    )


def _extract_tools_payload(session: Session) -> Optional[list[dict[str, Any]]]:
    """F1 fix: 从 ``session.metadata["tools"]`` 取 tools, 按 settings 做截断.

    返回 None 表示不写入 (开关关闭或 metadata["tools"] 为空).
    tools 顺序由 qf 阶段保证 (末次 model_request 的 tools 胜出 + 去重),
    这里只做透传, 不重新排序.
    """
    cfg = _current_settings()
    if not getattr(cfg, "include_tools_in_payloads", True):
        return None
    tools = (session.metadata or {}).get("tools") or []
    if not tools:
        return None
    cap = max(0, int(getattr(cfg, "tools_payload_max", 64)))
    if cap and len(tools) > cap:
        tools = tools[:cap]
    return tools


def _current_settings() -> Any:
    """取 gdr settings, 失败时返回包含默认值的 Namespace.

    单元测试或 root config 缺失时, 默认开启 tools 透传 + 截断阈值 64,
    与 ``Settings`` 字段默认值一致, 避免 import-time 副作用.
    """
    from gdr.config.settings import Settings
    try:
        return Settings()
    except Exception:
        return SimpleNamespace(include_tools_in_payloads=True, tools_payload_max=64)


def _json_default(obj: Any) -> Any:
    """``save_session`` / ``write_refined_session`` 共用的 JSON 兜底序列化."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json", exclude_none=True)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def locate_block(session: Session, block_id: str) -> tuple[int, int] | None:
    for msg_idx, msg in enumerate(session.messages):
        for blk_idx, blk in enumerate(msg.blocks):
            blk_id = blk.get("id", "") if isinstance(blk, dict) else getattr(blk, "id", "")
            if blk_id == block_id:
                return (msg_idx, blk_idx)
    return None