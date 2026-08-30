"""ContextUnderstanding 模块 —— 双层上下文架构。

设计文档：docs/gdr-context-understanding-and-policy.md §2

P0 范围 (零 LLM 调用):
  - BlockContextView 数据结构
  - 近期窗口抽取 (active window)
  - 跨 block 引用图 (实体共指 + 时序相邻)
  - 分级 archive (Tier 0~4) + 重要性评分 (5 因子加权)
  - 容量预算硬限 + 降级保底
  - 相关性评分 (4 因子加权)

非 P0 范围 (留 P2):
  - LLM 合并摘要 (T0 超限时触发)
  - 完整 LLM 句级摘要 (rule 置信度低时)
"""
from __future__ import annotations

import copy
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Literal

log = logging.getLogger(__name__)


# === 全局状态（增量状态追踪核心） ===

@dataclass
class GlobalState:
    """增量状态追踪的结构化状态对象。全文摘要是 = state_snapshots[-1]。

    字段语义见 docs/incremental-state-tracking-plan.md §3.1。
    """
    task_goal: str = ""
    current_step: str = ""
    key_entities: dict[str, Any] = field(default_factory=dict)
    completed_actions: list[str] = field(default_factory=list)
    archived_actions: list[str] = field(default_factory=list)
    open_issues: list[str] = field(default_factory=list)
    last_error: str | None = None
    critical_constraints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_goal": self.task_goal,
            "current_step": self.current_step,
            "key_entities": dict(self.key_entities),
            "completed_actions": list(self.completed_actions),
            "archived_actions": list(self.archived_actions),
            "open_issues": list(self.open_issues),
            "last_error": self.last_error,
            "critical_constraints": list(self.critical_constraints),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GlobalState":
        return cls(
            task_goal=data.get("task_goal", "") or "",
            current_step=data.get("current_step", "") or "",
            key_entities=dict(data.get("key_entities", {}) or {}),
            completed_actions=list(data.get("completed_actions", []) or []),
            archived_actions=list(data.get("archived_actions", []) or []),
            open_issues=list(data.get("open_issues", []) or []),
            last_error=data.get("last_error"),
            critical_constraints=list(data.get("critical_constraints", []) or []),
        )

    def render(self) -> str:
        """将状态对象渲染为可注入 prompt 的纯文本。"""
        lines = ["## 当前全局状态 (GlobalState)"]
        if self.task_goal:
            lines.append(f"- task_goal: {self.task_goal}")
        if self.current_step:
            lines.append(f"- current_step: {self.current_step}")
        if self.key_entities:
            ent_str = ", ".join(f"{k}={v}" for k, v in self.key_entities.items())
            lines.append(f"- key_entities: {ent_str}")
        if self.completed_actions:
            lines.append(f"- completed_actions: {', '.join(self.completed_actions)}")
        if self.open_issues:
            lines.append(f"- open_issues: {', '.join(self.open_issues)}")
        if self.last_error:
            lines.append(f"- last_error: {self.last_error}")
        if self.critical_constraints:
            lines.append(f"- critical_constraints: {', '.join(self.critical_constraints)}")
        return "\n".join(lines)


GLOBAL_STATE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "task_goal": {"type": "string"},
        "current_step": {"type": "string"},
        "key_entities": {"type": "object"},
        "completed_actions": {"type": "array", "items": {"type": "string"}},
        "archived_actions": {"type": "array", "items": {"type": "string"}},
        "open_issues": {"type": "array", "items": {"type": "string"}},
        "last_error": {"type": ["string", "null"]},
        "critical_constraints": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _try_parse_state_dict(text: str) -> dict | None:
    """解析 LLM 输出的状态 JSON。优先从代码块中抽取，其次全文解析。

    Qwen 系模型常输出 <think>...</think> 推理前缀, 先剥掉再解析,
    否则首段 { ... } 贪婪匹配会吞掉 think 内文本导致解析失败、触发无谓重试。
    剥离后仍失败再回退原文 (兼容把 JSON 写在 think 块内的输出)。
    """
    if not text:
        return None
    return _parse_state_json(_THINK_RE.sub("", text)) or _parse_state_json(text)


def _parse_state_json(text: str) -> dict | None:
    if not text:
        return None
    # 抽取 ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        candidate = m.group(1)
    else:
        # 抽取首个 { ... } 段
        m2 = re.search(r"\{.*\}", text, re.DOTALL)
        if not m2:
            return None
        candidate = m2.group(0)
    try:
        return json.loads(candidate)
    except Exception:
        # 尝试 json_repair 容错
        try:
            import json_repair  # type: ignore
            repaired = json_repair.repair_json(candidate)
            return json.loads(repaired) if isinstance(repaired, str) else None
        except Exception:
            return None


def _validate_state_dict(data: dict | None) -> dict | None:
    """校验状态 dict 是否符合 schema。返回校验后的 dict 或 None。"""
    if not isinstance(data, dict):
        return None
    try:
        import jsonschema  # type: ignore
        jsonschema.validate(instance=data, schema=GLOBAL_STATE_SCHEMA)
    except Exception as e:
        log.debug("state dict schema validation failed: %s", e)
        return None
    return data


_DEFAULT_STATE_TRACKER_PROMPT = """[角色] 你是一个 Agent 轨迹状态追踪器。
[任务] 根据新的对话片段, 仅更新状态 JSON 中发生变化的字段。未提及的字段保持原值。如果新片段包含关键实体、约束或错误, 必须提取并写入对应字段。
[当前状态]
{current_state}
[新对话片段]
{chunk}
[输出要求]
- 输出完整的状态 JSON (不是 diff, 避免合并错误)
- 未变化的字段保留原值
- 关键实体 (订单号、用户名、文件路径等) 必须保留
- 输出纯 JSON, 不要加任何解释
"""


_NOISE_PATTERN = re.compile(
    r"DEBUG|Traceback|status:\s*5\d\d|Error:|\[API_MISUSE\]|FATAL|"
    r"ModuleNotFoundError|IndentationError|SyntaxError"
)

_DECISION_KEYWORDS = re.compile(
    r"(决定|因此|所以|判断|认为|应该|下一步|执行|call\s+\w+|invoke\s+\w+|let'?s\s+|we\s+should)",
    re.IGNORECASE,
)


# === 数据类 ===

@dataclass
class MessageSnapshot:
    """单条消息的结构化快照。"""
    msg_id: str
    msg_idx: int
    role: str
    blocks: list[dict]
    is_healthy: bool
    created_at: str = ""
    error: str | None = None


@dataclass
class SummaryEntry:
    """分级 archive 的单条条目。"""
    block_id: str
    tier: int                              # 0~4
    importance: float
    content: str
    referenced_by: list[str] = field(default_factory=list)
    msg_idx: int = 0
    block_type: str = ""


@dataclass
class TieredArchive:
    """分级 archive: T0 全文 → T4 仅 id。"""
    full: list[SummaryEntry] = field(default_factory=list)
    detailed: list[SummaryEntry] = field(default_factory=list)
    compact: list[SummaryEntry] = field(default_factory=list)
    pointers: list[SummaryEntry] = field(default_factory=list)
    dropped_ids: set[str] = field(default_factory=set)
    total_chars: int = 0

    def add(self, entry: SummaryEntry) -> None:
        if entry.tier == 0:
            self.full.append(entry)
        elif entry.tier == 1:
            self.detailed.append(entry)
        elif entry.tier == 2:
            self.compact.append(entry)
        elif entry.tier == 3:
            self.pointers.append(entry)
        else:
            self.dropped_ids.add(entry.block_id)
        self.total_chars += len(entry.content)

    def recompute_chars(self) -> int:
        self.total_chars = (
            sum(len(e.content) for e in self.full)
            + sum(len(e.content) for e in self.detailed)
            + sum(len(e.content) for e in self.compact)
            + sum(len(e.content) for e in self.pointers)
        )
        return self.total_chars

    def render(self, max_chars: int | None = None) -> str:
        """渲染为可注入 prompt 的文本, 截断到 max_chars。"""
        parts: list[str] = []
        budget = max_chars if max_chars is not None else float("inf")
        used = 0

        def _emit(label: str, entries: list[SummaryEntry]) -> None:
            nonlocal used
            if not entries:
                return
            lines = [f"## {label}"]
            for e in entries:
                line = f"- [{e.block_type}@{e.msg_idx}] {e.content}"
                if used + len(line) > budget:
                    return
                lines.append(line)
                used += len(line)
            if len(lines) > 1:
                parts.append("\n".join(lines))

        _emit("T0 关键 (原文)", self.full)
        _emit("T1 重要 (详述)", self.detailed)
        _emit("T2 常规 (简述)", self.compact)
        _emit("T3 指针", self.pointers)

        return "\n\n".join(parts) if parts else ""


@dataclass
class BlockContextView:
    """单个 block 的上下文视图。"""
    block_id: str
    msg_idx: int = 0
    block_idx: int = 0
    block_type: str = ""

    active_window: list[MessageSnapshot] = field(default_factory=list)
    window_siblings: list[str] = field(default_factory=list)
    archive_summary: str = ""

    referenced_by: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    # 仅记录 active window 中 type ∈ {thinking, text} 的 block 对本 block 的引用。
    # fold 失败重试时, 仅用此字段判断"被引用 → 豁免删除", 避免 entity co-reference
    # 把所有失败重试都标成 referenced 导致 fold 失效。
    referenced_by_active_text: list[str] = field(default_factory=list)

    relevance_to_active: float = 0.0
    is_redundant_in_window: bool = False
    is_referenced_in_active: bool = False

    is_transition_point: bool = False
    block_role: Literal["opening", "middle", "closing", "isolated"] = "isolated"

    entities_mentioned: set[str] = field(default_factory=set)
    key_decisions: list[str] = field(default_factory=list)

    importance: float = 0.0
    importance_breakdown: dict[str, float] = field(default_factory=dict)


# === 工具函数 ===

def _get_attr(block, attr: str, default=""):
    if isinstance(block, dict):
        return block.get(attr, default)
    return getattr(block, attr, default)


def _extract_entities(text: str) -> set[str]:
    entities: set[str] = set()
    if not text:
        return entities
    # 防御: 单条超长/含恶意正则元字符的文本不应让整个 session 崩溃.
    # entity 抽取失败时返回已累积的子集 (不抛).
    try:
        _REGEX_LIMIT = 500_000  # 字符; 超过则降级为前 N 字符抽取
        if len(text) > _REGEX_LIMIT:
            text = text[:_REGEX_LIMIT]
        for m in re.finditer(r'"([^"]+)"', text):
            entities.add(m.group(1))
        for m in re.finditer(r"'([^']+)'", text):
            entities.add(m.group(1))
        for m in re.finditer(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", text):
            entities.add(m.group(1))
        for m in re.finditer(
            r"\b(browser|execute_shell_command|write_file|read_file|search_file|"
            r"list_files|glob|grep|tavily_search)\b", text, re.IGNORECASE
        ):
            entities.add(m.group(1).lower())
        for m in re.finditer(r"\b(url|file_path|command|content|code|input|query|name)\b", text, re.IGNORECASE):
            entities.add(m.group(1).lower())
        # CJK 实体: 1~4 个汉字 (城市名/平台名/动词等)
        for m in re.finditer(r"[一-鿿]{1,4}", text):
            ent = m.group(0)
            # 过滤常见停用词
            if ent not in _CJK_STOPWORDS:
                entities.add(ent)
        # 数字 (含小数)
        for m in re.finditer(r"\b\d+(?:\.\d+)?\b", text):
            entities.add(m.group(0))
    except re.error as e:
        # 极端输入触发 re.error (罕见, 但不应让整条 session 因 entity 抽取失败而崩)
        log.warning("_extract_entities re.error (text_len=%d): %s", len(text), e)
    except Exception as e:
        log.warning("_extract_entities failed (text_len=%d): %s", len(text), e)
    return entities
    for m in re.finditer(r'"([^"]+)"', text):
        entities.add(m.group(1))
    for m in re.finditer(r"'([^']+)'", text):
        entities.add(m.group(1))
    for m in re.finditer(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", text):
        entities.add(m.group(1))
    for m in re.finditer(
        r"\b(browser|execute_shell_command|write_file|read_file|search_file|"
        r"list_files|glob|grep|tavily_search)\b", text, re.IGNORECASE
    ):
        entities.add(m.group(1).lower())
    for m in re.finditer(r"\b(url|file_path|command|content|code|input|query|name)\b", text, re.IGNORECASE):
        entities.add(m.group(1).lower())
    # CJK 实体: 1~4 个汉字 (城市名/平台名/动词等)
    for m in re.finditer(r"[一-鿿]{1,4}", text):
        ent = m.group(0)
        # 过滤常见停用词
        if ent not in _CJK_STOPWORDS:
            entities.add(ent)
    # 数字 (含小数)
    for m in re.finditer(r"\b\d+(?:\.\d+)?\b", text):
        entities.add(m.group(0))
    return entities


_CJK_STOPWORDS = frozenset({
    "的", "了", "是", "在", "和", "与", "或", "为", "我", "你", "他", "她", "它",
    "这", "那", "有", "没", "都", "也", "就", "还", "才", "要", "会", "能",
    "把", "被", "让", "给", "从", "到", "向", "以", "用", "对", "按",
    "个", "些", "点", "次", "条", "个", "件", "下", "上", "中", "里",
    "什么", "怎么", "为什么", "因为", "所以", "但是", "不过", "如果",
    "今天", "昨天", "明天", "现在", "之前", "之后",
})


def _block_text(block) -> str:
    bt = _get_attr(block, "type", "")
    if bt == "thinking":
        return _get_attr(block, "thinking", "")
    if bt == "toolcall":
        return f"{_get_attr(block, 'name', '')} {_get_attr(block, 'input', '')}"
    if bt == "toolresult":
        return _get_attr(block, "output_text", "")
    if bt == "text":
        return _get_attr(block, "text", "")
    return ""


def _toolcall_name(block) -> str:
    if _get_attr(block, "type", "") == "toolcall":
        return _get_attr(block, "name", "")
    return ""


def _toolresult_state(block) -> str:
    if _get_attr(block, "type", "") == "toolresult":
        return _get_attr(block, "state", "")
    return ""


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# === 主类 ===

class ContextUnderstanding:
    """对一条 session 构建 block 级别的上下文视图。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.active_window_size: int = cfg.context_active_window_size
        self.max_archive_chars: int = cfg.context_max_archive_chars
        self.relevance_threshold: float = cfg.context_relevance_threshold
        self.compression_strategy: str = cfg.context_compression_strategy
        self.max_llm_compressions: int = cfg.context_max_llm_compressions
        self.max_t0_entries: int = cfg.context_max_t0_entries

        self.w_error = cfg.context_importance_w_error
        self.w_transit = cfg.context_importance_w_transit
        self.w_refs = cfg.context_importance_w_refs
        self.w_finality = cfg.context_importance_w_finality
        self.w_novelty = cfg.context_importance_w_novelty

        self.tier_thresholds = (
            cfg.context_tier3_threshold,   # <t3 → T4
            cfg.context_tier2_threshold,   # [t3,t2) → T3
            cfg.context_tier1_threshold,   # [t2,t1) → T2
            cfg.context_tier0_threshold,   # [t1,t0) → T1; ≥t0 → T0
        )

        self._block_index: dict[str, BlockContextView] = {}
        self._block_order: list[str] = []
        self._block_cache: dict[str, object] = {}               # block_id -> 原始 block
        self._block_content_cache: dict[str, str] = {}          # block_id -> 内容文本
        self._msg_blocks: dict[int, list[str]] = defaultdict(list)
        self._reference_graph: dict[str, set[str]] = defaultdict(set)
        self._dependency_graph: dict[str, set[str]] = defaultdict(set)

        self._archive: TieredArchive = TieredArchive()
        self._llm_compression_count: int = 0
        self._unhealthy_msg_indices: set[int] = set()

        # === 增量状态追踪（新增：方案 §3） ===
        self._chunks: list[list[str]] = []              # chunk_idx -> [block_id, ...]
        self._block_to_chunk: dict[str, int] = {}       # block_id -> chunk_idx
        self._state_snapshots: dict[int, GlobalState] = {}
        self._latest_state: GlobalState = GlobalState()
        self._state_tracking_calls: int = 0             # 用于 context_max_state_llm_calls 限额

    # === 公开 API ===

    def build(
        self, session,
        unhealthy_msg_indices: set[int] | None = None,
        light_health: dict[int, float] | None = None,
        track_state: bool = True,
    ) -> None:
        """构建上下文理解。

        Args:
            session: 待理解的 session。
            unhealthy_msg_indices: 不健康消息索引集合（旧参数，兼容）。
            light_health: msg_idx -> health_score 的轻量映射；
                当提供时，用于初始化 MessageSnapshot.is_healthy，
                未提供则回退到 unhealthy_msg_indices。
            track_state: 是否在本调用内执行 chunk 切分 + LLM 状态追踪。
                传 False 只构建零 LLM 的结构层 (引用图/视图/archive, 供 fold 保护),
                状态追踪留待 fold 之后调用 retrack_state() 完成,
                保证 chunk 划分反映折叠后的 session 且状态追踪只跑一次。
        """
        self._unhealthy_msg_indices = unhealthy_msg_indices or set()
        self._light_health = light_health or {}
        self._index_blocks(session)
        self._build_reference_graph()
        self._score_all_importance()
        self._extract_active_window(session)
        self._build_tiered_archive(session)
        self._evict_until_under_budget()
        self._annotate_block_views()
        # === 增量状态追踪阶段（方案 §3） ===
        if track_state:
            self.retrack_state(session)

    def get_view(self, block_id: str) -> BlockContextView | None:
        return self._block_index.get(block_id)

    def render_archive(self, max_chars: int | None = None) -> str:
        """渲染全局上下文。两段式：最新状态快照 + 分级 archive。

        当 context_state_tracker_enabled 且已生成快照时, 状态快照优先;
        否则回退到旧 TieredArchive 渲染。
        """
        max_chars = max_chars or self.max_archive_chars
        parts: list[str] = []
        if getattr(self.cfg, "context_state_tracker_enabled", True) and self._state_snapshots:
            state_text = self._latest_state.render()
            if state_text and len(state_text) <= max_chars:
                parts.append(state_text)
                max_chars -= len(state_text)
        if max_chars > 0:
            arc_text = self._archive.render(max_chars)
            if arc_text:
                parts.append(arc_text)
        return "\n\n".join(parts)

    def render_archive_for_block(
        self, block_id: str,
        max_chars: int | None = None,
        strategy: str | None = None,
    ) -> str:
        """为指定 block 渲染用于 Router LLM prompt 的 archive 子集。

        Args:
            block_id: 目标 block id。
            max_chars: 字符预算；默认使用 cfg.cu_prompt_max_chars 或 archive 上限。
            strategy: 子集策略；"referenced" 只保留 referenced_by/depends_on 相关条目；
                其他则返回完整 archive 渲染。
        """
        max_chars = max_chars or getattr(self.cfg, "cu_prompt_max_chars", self.max_archive_chars)
        strategy = strategy or getattr(self.cfg, "cu_prompt_archive_strategy", "full")

        view = self._block_index.get(block_id)
        if strategy == "referenced" and view is not None:
            related = set(view.referenced_by) | set(view.depends_on) | {block_id}
            subset = TieredArchive()
            for entry in self._archive.full + self._archive.detailed + self._archive.compact + self._archive.pointers:
                if entry.block_id in related:
                    subset.add(entry)
            rendered = subset.render(max_chars)
            return rendered if rendered else self._archive.render(max_chars)
        return self._archive.render(max_chars)

    def stats(self) -> dict:
        return {
            "active_window_size": self.active_window_size,
            "archive_chars": self._archive.total_chars,
            "archive_t0_count": len(self._archive.full),
            "archive_t1_count": len(self._archive.detailed),
            "archive_t2_count": len(self._archive.compact),
            "archive_t3_count": len(self._archive.pointers),
            "archive_t4_count": len(self._archive.dropped_ids),
            "llm_compressions_used": self._llm_compression_count,
            "block_views_count": len(self._block_index),
        }

    # === 内部步骤 ===

    def _index_blocks(self, session) -> None:
        for msg_idx, msg in enumerate(session.messages):
            for blk_idx, block in enumerate(msg.blocks):
                bid = _get_attr(block, "id", "")
                if not bid:
                    continue
                content_text = _block_text(block)
                entities = _extract_entities(content_text)
                decisions = []
                if content_text and _DECISION_KEYWORDS.search(content_text):
                    decisions.append(content_text[:120])

                view = BlockContextView(
                    block_id=bid,
                    msg_idx=msg_idx,
                    block_idx=blk_idx,
                    block_type=_get_attr(block, "type", ""),
                )
                view.entities_mentioned = entities
                view.key_decisions = decisions
                view.window_siblings = [
                    _get_attr(b, "id", "")
                    for b in msg.blocks
                    if _get_attr(b, "id", "") and _get_attr(b, "id", "") != bid
                ]
                self._block_index[bid] = view
                self._block_order.append(bid)
                self._block_cache[bid] = block
                self._block_content_cache[bid] = content_text
                self._msg_blocks[msg_idx].append(bid)

    def _build_reference_graph(self) -> None:
        # 1. 时序相邻: toolcall → 紧邻 toolresult 配对
        for msg_idx, bids in self._msg_blocks.items():
            for i, bid in enumerate(bids):
                if i + 1 >= len(bids):
                    continue
                nb = bids[i + 1]
                if self._block_index[bid].block_type == "toolcall" and \
                        self._block_index[nb].block_type == "toolresult":
                    self._reference_graph[bid].add(nb)
                    self._dependency_graph[nb].add(bid)

        # 2. 实体共指: A 在前提到 entity X, B 在后也提到 → A → B
        sorted_bids = list(self._block_order)
        for i, bid_a in enumerate(sorted_bids):
            entities_a = self._block_index[bid_a].entities_mentioned
            if not entities_a:
                continue
            for bid_b in sorted_bids[i + 1:]:
                if self._block_index[bid_b].msg_idx < self._block_index[bid_a].msg_idx:
                    continue
                if self._block_index[bid_b].entities_mentioned & entities_a:
                    self._reference_graph[bid_a].add(bid_b)
                    self._dependency_graph[bid_b].add(bid_a)

    def _score_all_importance(self) -> None:
        seen_entities: set[str] = set()
        # 先统计每个 toolcall name 的最终成功状态 (finality)
        name_success: dict[str, bool] = {}
        name_first_seen: dict[str, str] = {}
        for bid in self._block_order:
            block = self._block_cache[bid]
            if _get_attr(block, "type", "") == "toolresult":
                tool_name = _get_attr(block, "name", "")
                state = _toolresult_state(block)
                name_success[tool_name] = name_success.get(tool_name, False) or (state == "success")
                if name_first_seen.get(tool_name) is None and state == "success":
                    name_first_seen[tool_name] = bid

        for bid in self._block_order:
            view = self._block_index[bid]
            content_text = self._block_content_cache.get(bid, "")
            breakdown = self._score_one(view, content_text, seen_entities, name_success, name_first_seen)
            view.importance = breakdown["total"]
            view.importance_breakdown = breakdown
            seen_entities.update(view.entities_mentioned)

    def _score_one(self, view: BlockContextView, content_text: str,
                   seen_entities: set[str],
                   name_success: dict[str, bool],
                   name_first_seen: dict[str, str]) -> dict[str, float]:
        # 1. error_relevance
        has_noise = bool(content_text and _NOISE_PATTERN.search(content_text))
        block = self._block_cache.get(view.block_id)
        state = _toolresult_state(block) if block is not None else ""
        error = 1.0 if has_noise or state == "error" else 0.0

        # 2. transition_signal
        is_first = view.block_idx == 0
        # closing 判定需要总 block 数, 暂用 window_siblings 推断
        siblings_total = len(view.window_siblings) + 1
        is_last = view.block_idx == siblings_total - 1 and siblings_total > 1
        transit = 1.0 if (is_first or is_last) else 0.0

        # 3. reference_count
        refs = self._reference_graph.get(view.block_id, set())
        refs_score = min(len(refs) / 3.0, 1.0)

        # 4. finality: toolcall 之后是否有同名成功 toolresult
        finality = 0.0
        if view.block_type == "toolcall":
            tool_name = _get_attr(block, "name", "") if block is not None else ""
            if name_success.get(tool_name):
                finality = 1.0
        elif view.block_type == "toolresult" and state == "success":
            finality = 1.0
        else:
            finality = 0.5  # 默认中性

        # 5. entity_novelty
        if view.entities_mentioned:
            new_count = len(view.entities_mentioned - seen_entities)
            novelty = new_count / len(view.entities_mentioned)
        else:
            novelty = 0.0

        total = (
            self.w_error * error
            + self.w_transit * transit
            + self.w_refs * refs_score
            + self.w_finality * finality
            + self.w_novelty * novelty
        )

        return {
            "error": error,
            "transit": transit,
            "refs": refs_score,
            "finality": finality,
            "novelty": novelty,
            "total": min(max(total, 0.0), 1.0),
        }

    def _assign_tier(self, importance: float) -> int:
        t3, t2, t1, t0 = self.tier_thresholds
        if importance >= t0:
            return 0
        if importance >= t1:
            return 1
        if importance >= t2:
            return 2
        if importance >= t3:
            return 3
        return 4

    def _summarize_content(self, content_text: str, tier: int, block_type: str) -> str:
        if tier == 0:
            return content_text
        if tier == 1:
            sentences = re.split(r"[。.!?！？\n]", content_text)
            first = next((s.strip() for s in sentences if s.strip()), content_text[:80])
            nums = re.findall(r"\b(?:[A-Z]+_\w+|status:\s*\d{3}|\d{3,})\b", content_text)
            extra = f" [{', '.join(nums[:3])}]" if nums else ""
            return (first[:120] + extra).strip()
        if tier == 2:
            truncated = content_text[:60].strip()
            return truncated + ("…" if len(content_text) > 60 else "")
        return f"[{block_type}]"

    def _build_tiered_archive(self, session) -> None:
        active_msg_indices = set()
        assistant_indices = [i for i, m in enumerate(session.messages) if m.role == "assistant"]
        if assistant_indices and self.active_window_size > 0:
            active_msg_indices = set(assistant_indices[-self.active_window_size:])

        for msg_idx, msg in enumerate(session.messages):
            if msg_idx in active_msg_indices:
                continue
            for block in msg.blocks:
                bid = _get_attr(block, "id", "")
                if not bid:
                    continue
                view = self._block_index.get(bid)
                if view is None:
                    continue
                content_text = self._block_content_cache.get(bid, "")
                tier = self._assign_tier(view.importance)
                entry = SummaryEntry(
                    block_id=bid,
                    tier=tier,
                    importance=view.importance,
                    content=self._summarize_content(content_text, tier, view.block_type),
                    referenced_by=sorted(self._reference_graph.get(bid, set())),
                    msg_idx=msg_idx,
                    block_type=view.block_type,
                )
                self._archive.add(entry)

    def _evict_until_under_budget(self) -> None:
        self._archive.recompute_chars()
        if self._archive.total_chars <= self.max_archive_chars:
            return

        # 1) T2 → T3
        while self._archive.compact and self._archive.total_chars > self.max_archive_chars:
            oldest = self._archive.compact.pop(0)
            oldest.tier = 3
            oldest.content = f"[{oldest.block_type}]"
            self._archive.pointers.append(oldest)
            self._archive.recompute_chars()

        # 2) T3 → T4
        while self._archive.pointers and self._archive.total_chars > self.max_archive_chars:
            oldest = self._archive.pointers.pop(0)
            self._archive.dropped_ids.add(oldest.block_id)
            self._archive.recompute_chars()

        # 3) T1 → T2
        while self._archive.detailed and self._archive.total_chars > self.max_archive_chars:
            oldest = self._archive.detailed.pop(0)
            oldest.tier = 2
            oldest.content = (oldest.content[:60] + ("…" if len(oldest.content) > 60 else "")).strip()
            self._archive.compact.append(oldest)
            self._archive.recompute_chars()

        self._archive.recompute_chars()

    def _extract_active_window(self, session) -> None:
        assistant_indices = [i for i, m in enumerate(session.messages) if m.role == "assistant"]
        active_indices = (
            assistant_indices[-self.active_window_size:]
            if assistant_indices and self.active_window_size > 0
            else []
        )

        snapshots: list[MessageSnapshot] = []
        for idx in active_indices:
            msg = session.messages[idx]
            # 优先使用轻量健康分；未提供则回退旧 unhealthy_msg_indices
            if self._light_health:
                is_healthy = self._light_health.get(idx, 1.0) >= getattr(
                    self.cfg, "message_health_min_ratio", 0.3
                )
            else:
                is_healthy = idx not in self._unhealthy_msg_indices
            blocks_simple = []
            for b in msg.blocks:
                bt = _get_attr(b, "type", "")
                bid = _get_attr(b, "id", "")
                if bt == "thinking":
                    blocks_simple.append({"type": bt, "id": bid, "thinking": _get_attr(b, "thinking", "")})
                elif bt == "toolcall":
                    blocks_simple.append({"type": bt, "id": bid, "name": _get_attr(b, "name", ""), "input": _get_attr(b, "input", "")})
                elif bt == "toolresult":
                    blocks_simple.append({"type": bt, "id": bid, "name": _get_attr(b, "name", ""), "output_text": _get_attr(b, "output_text", "")})
                elif bt == "text":
                    blocks_simple.append({"type": bt, "id": bid, "text": _get_attr(b, "text", "")})
                else:
                    blocks_simple.append({"type": bt, "id": bid})
            snapshots.append(MessageSnapshot(
                msg_id=_get_attr(msg, "id", ""),
                msg_idx=idx,
                role=msg.role,
                blocks=blocks_simple,
                is_healthy=is_healthy,
                created_at=_get_attr(msg, "created_at", ""),
                error=_get_attr(msg, "error", None),
            ))

        archive_text = self._archive.render(self.max_archive_chars)
        active_msg_set = set(active_indices)

        for bid, view in self._block_index.items():
            view.active_window = snapshots
            view.archive_summary = archive_text
            view.referenced_by = sorted(self._reference_graph.get(bid, set()))
            view.depends_on = sorted(self._dependency_graph.get(bid, set()))
            # 是否被 active window 引用
            active_ids = {b.get("id", "") for snap in snapshots for b in snap.blocks}
            view.is_referenced_in_active = any(ref in active_ids for ref in view.referenced_by)
            # 仅 active window 内**其它 message** 的 thinking/text 类型对本 block 的引用。
            # fold 失败重试用这个判断"被引用", 避免 entity 共指把同一 message 内
            # 所有失败重试都标成 referenced 而失效 (同 message 内的 thinking 只是
            # 决策上下文, 不构成对失败调用的实质依赖)。
            active_text_ids_other_msg = {
                b.get("id", "")
                for snap in snapshots
                if snap.msg_idx != view.msg_idx
                for b in snap.blocks
                if b.get("type") in ("thinking", "text")
            }
            view.referenced_by_active_text = sorted(
                set(view.referenced_by) & active_text_ids_other_msg
            )

    def _annotate_block_views(self) -> None:
        for bid, view in self._block_index.items():
            siblings_total = len(view.window_siblings) + 1
            if siblings_total == 1:
                view.block_role = "isolated"
            elif view.block_idx == 0:
                view.block_role = "opening"
            elif view.block_idx == siblings_total - 1:
                view.block_role = "closing"
            else:
                view.block_role = "middle"

            view.is_transition_point = (
                view.block_role in ("opening", "closing")
                and (bool(view.referenced_by) or bool(view.depends_on))
            )

        # is_redundant_in_window: 在 active window 内存在同 tool_name + 高相似 input
        active_bids_by_type: dict[str, list[tuple[str, str]]] = defaultdict(list)
        #  block_type -> [(bid, name)]
        for snap in next(iter(self._block_index.values())).active_window if self._block_index else []:
            for b in snap.blocks:
                if b.get("type") == "toolcall":
                    active_bids_by_type["toolcall"].append((b.get("id", ""), b.get("name", "")))

        for bid in self._block_order:
            view = self._block_index[bid]
            block = self._block_cache.get(bid)
            if view.block_type != "toolcall" or block is None:
                continue
            name = _toolcall_name(block)
            input_str = _get_attr(block, "input", "")
            count = sum(
                1 for (other_bid, other_name) in active_bids_by_type.get("toolcall", [])
                if other_name == name and other_bid != bid
            )
            if count >= self.cfg.policy_min_redundancy_for_prune:
                view.is_redundant_in_window = True

        # 相关性评分 (4 因子加权)
        for bid, view in self._block_index.items():
            active_ids = {b.get("id", "") for snap in view.active_window for b in snap.blocks}
            referenced_in_active = 1.0 if any(r in active_ids for r in view.referenced_by) else 0.0
            entity_pool: set[str] = set()
            for snap in view.active_window:
                for b in snap.blocks:
                    txt = b.get("thinking", "") or b.get("output_text", "") or b.get("text", "") or \
                          f"{b.get('name', '')} {b.get('input', '')}"
                    entity_pool.update(_extract_entities(txt))
            if view.entities_mentioned and entity_pool:
                jaccard = len(view.entities_mentioned & entity_pool) / \
                          len(view.entities_mentioned | entity_pool)
            else:
                jaccard = 0.0
            try:
                pos = self._block_order.index(bid)
                distance = max(0, len(self._block_order) - 1 - pos)
                recency = 1.0 / (1.0 + distance)
            except ValueError:
                recency = 0.0
            dup_penalty = 1.0 if view.is_redundant_in_window else 0.0

            view.relevance_to_active = (
                0.4 * referenced_in_active
                + 0.2 * jaccard
                + 0.1 * recency
                - 0.3 * dup_penalty
            )
            view.relevance_to_active = max(0.0, min(1.0, view.relevance_to_active))

    # === 增量状态追踪相关方法（方案 §3, §5.4） ===

    @property
    def num_chunks(self) -> int:
        return len(self._chunks)

    @property
    def chunks(self) -> list[list[str]]:
        return self._chunks

    @property
    def chunk_blocks(self) -> dict[int, list[str]]:
        return {i: list(self._chunks[i]) for i in range(len(self._chunks))}

    @property
    def state_snapshots(self) -> dict[int, GlobalState]:
        return self._state_snapshots

    @property
    def state_tracking_calls(self) -> int:
        """已消耗的状态追踪 LLM 调用数（含重试）。"""
        return self._state_tracking_calls

    def retrack_state(self, session) -> None:
        """(重新)切分 chunk 并执行增量状态追踪。

        fold/编辑后调用, 保证 chunk 划分反映当前 session 内容。
        受 context_state_tracker_enabled 与 context_max_state_llm_calls 约束;
        整个 CU 生命周期内状态追踪只应跑这一次。
        """
        if not getattr(self.cfg, "context_state_tracker_enabled", True):
            return
        self._chunkify(session)
        self._track_state(session)

    def update_state_chunk(
        self, session, current_state: GlobalState, chunk_idx: int, cfg=None,
    ) -> GlobalState | None:
        """基于给定前置状态对单个 chunk 做一次增量更新 (一致性校验专用)。

        与 state_after 的"从 chunk 0 重放"不同, 本方法只更新一个 chunk,
        由调用方在外层循环中携带状态前进, 把一致性校验从 O(N²) 降到 O(N)。
        返回新状态; chunk 越界或 LLM 更新失败返回 None。
        """
        if not (0 <= chunk_idx < len(self._chunks)):
            return None
        max_retries = max(0, getattr(self.cfg, "context_state_max_retries", 1))
        return self._state_update_one(
            current_state, session, self._chunks[chunk_idx], max_retries,
        )

    def chunk_of_block(self, block_id: str) -> int | None:
        return self._block_to_chunk.get(block_id)

    def latest_state(self) -> GlobalState:
        return copy.deepcopy(self._latest_state)

    def snapshot_at(self, chunk_idx: int) -> GlobalState | None:
        s = self._state_snapshots.get(chunk_idx)
        return copy.deepcopy(s) if s is not None else None

    def _chunkify(self, session) -> None:
        """按 toolcall 边界切分 session 为 Chunk（方案 §3.2）。

        每个 Chunk 包含 1~N (默认 3) 个完整 toolcall-toolresult 对,
        绝不拆开 Action-Observation。thinking 归属其随后 toolcall; text 归属最近 chunk 尾部。
        """
        max_pairs = max(1, getattr(self.cfg, "context_chunk_max_tool_pairs", 3))
        chunks: list[list[str]] = []
        block_to_chunk: dict[str, int] = {}

        for msg_idx, msg in enumerate(session.messages):
            if msg.role != "assistant":
                continue
            blocks = list(msg.blocks)
            i = 0
            current: list[str] = []
            pair_count = 0
            while i < len(blocks):
                b = blocks[i]
                bt = _get_attr(b, "type", "")
                bid = _get_attr(b, "id", "")
                # thinking 永远归属当前 chunk, 不计入 pair
                if bt == "thinking":
                    if bid:
                        current.append(bid)
                        block_to_chunk[bid] = len(chunks)
                    i += 1
                    continue
                # toolcall: 加入当前 chunk, 后续配对的 toolresult 也加入
                if bt == "toolcall":
                    if bid:
                        current.append(bid)
                        block_to_chunk[bid] = len(chunks)
                    # 紧邻的同 id toolresult 一并加入
                    j = i + 1
                    while j < len(blocks):
                        nb = blocks[j]
                        nt = _get_attr(nb, "type", "")
                        nbid = _get_attr(nb, "id", "")
                        if nt == "toolresult" and nbid == bid:
                            current.append(nbid)
                            block_to_chunk[nbid] = len(chunks)
                            break
                        j += 1
                    pair_count += 1
                    i = j + 1
                    if pair_count >= max_pairs:
                        chunks.append(current)
                        current = []
                        pair_count = 0
                    continue
                # text: 归属最近 chunk 尾部
                if bt == "text":
                    if current and bid:
                        current.append(bid)
                        block_to_chunk[bid] = len(chunks)
                    elif bid:
                        current.append(bid)
                        block_to_chunk[bid] = len(chunks)
                    i += 1
                    continue
                # 其它类型（理论上不应出现）: 直接跳过
                i += 1
            if current:
                chunks.append(current)
        if current and not chunks:
            chunks.append(current)

        self._chunks = chunks
        self._block_to_chunk = block_to_chunk

    def _track_state(self, session) -> None:
        """对每个 Chunk 增量更新 GlobalState, 保存快照。

        严格遵守 max_calls 上限, 失败时沿用上一版状态并告警。
        """
        if not self._chunks:
            return
        max_calls = max(0, getattr(self.cfg, "context_max_state_llm_calls", 20))
        max_retries = max(0, getattr(self.cfg, "context_state_max_retries", 1))
        current = GlobalState()
        snapshots: dict[int, GlobalState] = {}
        for ci, block_ids in enumerate(self._chunks):
            if self._state_tracking_calls >= max_calls:
                log.warning(
                    "state tracking hit max calls=%d, chunk %d+ reuses previous state",
                    max_calls, ci,
                )
                snapshots[ci] = copy.deepcopy(current)
                continue
            new_state = self._state_update_one(current, session, block_ids, max_retries)
            if new_state is None:
                log.warning("state update failed at chunk %d, keep previous state", ci)
                snapshots[ci] = copy.deepcopy(current)
                continue
            current = new_state
            snapshots[ci] = copy.deepcopy(current)
        self._latest_state = current
        self._state_snapshots = snapshots

    def _state_update_one(
        self, current: GlobalState, session, block_ids: list[str], max_retries: int,
    ) -> GlobalState | None:
        """对单个 Chunk 调用 LLM 更新状态; 返回新状态或 None（失败时）。

        渲染基于 session 的当前 block 内容 (而非 build 时快照),
        这样编辑后重算 (一致性校验的 update_state_chunk) 才能反映最新文本。
        """
        try:
            from infrastructure import LlamaCppClient
        except Exception as e:
            log.warning("LlamaCppClient not importable, skip state update: %s", e)
            return None

        chunk_text = self._render_chunk_text(session, block_ids)
        if not chunk_text:
            # 空 Chunk: 直接返回当前状态快照
            return copy.deepcopy(current)

        prompt = self._build_state_prompt(current, chunk_text)
        # _build_state_prompt 返回的可能是 "system\n\nuser" 拼接, 解析出 messages
        system_prompt, user_prompt = self._split_state_prompt(prompt)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        model = getattr(self.cfg, "context_state_model", None) or self.cfg.main_model
        timeout = getattr(self.cfg, "llm_timeout_s", 120)

        last_err: str = ""
        for attempt in range(max_retries + 1):
            self._state_tracking_calls += 1
            retry_messages = messages
            if attempt > 0:
                # Retry: 注入 Nudge 提示, 强制 LLM 输出 JSON
                retry_messages = list(messages) + [
                    {"role": "user", "content": "上轮你只给了分析说明, 没有给出 JSON 代码块。请立即输出 ```json ... ``` 格式的完整状态 JSON, 不要继续解释。"}
                ]
            try:
                client = LlamaCppClient.get(model, cfg=self.cfg, timeout=timeout)
                # reasoning 模型 (如 MiniMax-M2.7) 的思考 token 计入 max_tokens; 该模型
                # 单次状态输出可达 5-9k token, 预算不足会截断 JSON (invalid state json)
                text, _ = client.chat(retry_messages, max_tokens=10000, temperature=0.0, timeout_s=timeout)
                data = _validate_state_dict(_try_parse_state_dict(text))
                if data is None:
                    raise ValueError("invalid state json after repair")
                return GlobalState.from_dict(data)
            except Exception as e:
                last_err = str(e)
                log.debug("state update attempt %d failed: %s; raw=%r", attempt + 1, e, text[:400] if isinstance(text, str) else text)
        log.warning("state update exhausted retries: %s", last_err)
        return None

    def _render_chunk_text(self, session, block_ids: list[str]) -> str:
        """将 Chunk 的 block 列表渲染为适合送入 LLM 的纯文本。

        从 session 当前内容取 block (支持编辑后重跑), 已被 prune 的 block 跳过。
        """
        # 建立 block_id -> 当前 block 的快速索引
        live: dict[str, object] = {}
        for msg in session.messages:
            for b in msg.blocks:
                bid = _get_attr(b, "id", "")
                if bid:
                    live[bid] = b

        lines: list[str] = []
        for bid in block_ids:
            block = live.get(bid)
            if block is None:
                continue  # 已被 prune 的 block 不参与状态更新
            bt = _get_attr(block, "type", "")
            if bt == "thinking":
                content = _get_attr(block, "thinking", "")
                lines.append(f"[thinking] {content}")
            elif bt == "toolcall":
                name = _get_attr(block, "name", "")
                inp = _get_attr(block, "input", "")
                lines.append(f"[toolcall:{name}] {inp}")
            elif bt == "toolresult":
                name = _get_attr(block, "name", "")
                state = _get_attr(block, "state", "")
                out = _get_attr(block, "output_text", "")
                lines.append(f"[toolresult:{name} state={state}] {out[:1000]}")
            elif bt == "text":
                content = _get_attr(block, "text", "")
                lines.append(f"[text] {content[:500]}")
        return "\n".join(lines)

    def _build_state_prompt(self, current: GlobalState, chunk_text: str) -> str:
        """构造增量状态更新 prompt。"""
        current_json = json.dumps(current.to_dict(), ensure_ascii=False, indent=2)
        # 复用 prompts/state_tracker.yaml（如存在），否则用内置默认
        try:
            from prompts import load_and_render
            tpl = load_and_render("state_tracker", "user",
                                  current_state=current_json,
                                  chunk=chunk_text)
            system = load_and_render("state_tracker", "system")
            return system + "\n\n" + tpl
        except Exception:
            # 内置默认
            return _DEFAULT_STATE_TRACKER_PROMPT.format(
                current_state=current_json, chunk=chunk_text,
            )

    @staticmethod
    def _split_state_prompt(prompt: str) -> tuple[str, str]:
        """把 _build_state_prompt 返回的 "system\\n\\nuser" 拆回 (system, user)。"""
        marker = "\n\n"
        idx = prompt.find(marker)
        if idx == -1:
            return prompt, ""
        return prompt[:idx], prompt[idx + len(marker):]

    def state_after(
        self, session, chunk_idx: int, cfg=None,
    ) -> GlobalState | None:
        """编辑后, 对单个 chunk 重跑状态更新, 返回该 chunk 的新快照。

        旧版重放式实现: 从 chunk 0 累积至 chunk_idx, O(chunk_idx) 次调用。
        生产路径已不再使用 —— 一致性校验改用 update_state_chunk 逐 chunk
        真增量推进 (O(N) 总量), 本方法保留作兼容/调试用途。
        """
        if chunk_idx < 0 or chunk_idx >= len(self._chunks):
            return None
        cfg = cfg or self.cfg
        max_retries = max(0, getattr(cfg, "context_state_max_retries", 1))
        # 从 chunk 0 重新累积到 chunk_idx (基于编辑后的 session 内容)
        current = GlobalState()
        for ci in range(0, chunk_idx + 1):
            new_state = self._state_update_one(current, session, self._chunks[ci], max_retries)
            if new_state is None:
                return None
            current = new_state
        return current


# === 便捷入口 ===

def build_context_for_session(
    session, cfg,
    unhealthy_msg_indices: set[int] | None = None,
    light_health: dict[int, float] | None = None,
    track_state: bool = True,
) -> ContextUnderstanding:
    """便捷工厂: 直接 build 完整上下文。

    track_state=False 时只构建零 LLM 结构层, 状态追踪由调用方在 fold 之后
    调用 retrack_state(session) 完成。
    """
    if not getattr(cfg, "enable_context_understanding", True):
        return None  # 由调用方 fallback 到旧 ±2 上下文
    cu = ContextUnderstanding(cfg)
    cu.build(
        session,
        unhealthy_msg_indices=unhealthy_msg_indices,
        light_health=light_health,
        track_state=track_state,
    )
    return cu