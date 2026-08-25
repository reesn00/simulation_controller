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

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Literal

log = logging.getLogger(__name__)


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

    # === 公开 API ===

    def build(self, session, unhealthy_msg_indices: set[int] | None = None) -> None:
        self._unhealthy_msg_indices = unhealthy_msg_indices or set()
        self._index_blocks(session)
        self._build_reference_graph()
        self._score_all_importance()
        self._extract_active_window(session)
        self._build_tiered_archive(session)
        self._evict_until_under_budget()
        self._annotate_block_views()

    def get_view(self, block_id: str) -> BlockContextView | None:
        return self._block_index.get(block_id)

    def render_archive(self, max_chars: int | None = None) -> str:
        return self._archive.render(max_chars or self.max_archive_chars)

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
        if assistant_indices:
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
        active_indices = assistant_indices[-self.active_window_size:] if assistant_indices else []

        snapshots: list[MessageSnapshot] = []
        for idx in active_indices:
            msg = session.messages[idx]
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


# === 便捷入口 ===

def build_context_for_session(session, cfg, unhealthy_msg_indices: set[int] | None = None) -> ContextUnderstanding:
    """便捷工厂: 直接 build 完整上下文。"""
    if not getattr(cfg, "enable_context_understanding", True):
        return None  # 由调用方 fallback 到旧 ±2 上下文
    cu = ContextUnderstanding(cfg)
    cu.build(session, unhealthy_msg_indices=unhealthy_msg_indices)
    return cu