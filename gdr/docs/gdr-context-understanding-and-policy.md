# GDR 上下文理解与决策层设计备忘录

> 本文是对 gdr 流水线两个增强方向的方案汇总：
> 1. **ContextUnderstanding 模块**——双层上下文架构，为决策层提供上下文感知能力。
> 2. **Policy 决策层**——基于 defect + context view 选择"原地修复 / 上下文删除 / 标记待复核"三种策略。
>
> 配套文档：[gdr-module-functional-overview.md](gdr-module-functional-overview.md)（现有模块功能）、[gdr-mvp-design.md](gdr-mvp-design.md)（原始 MVP 设计）。

---

## 0. 背景与动机

### 0.1 当前 gdr 的盲点

- `pipeline/runner.py:_build_context` 只看 ±2 个 block，refiner 决策时缺乏对话级上下文。
- Router、Refiner、Reassembler 三方各做各的策略判断，**没有统一的决策层**。
- 修剪策略写死在 reassembler 里（只覆盖 REPETITIVE_CALL 和 CONTEXT_SWITCH_LOOP 两类循环），其他低相关内容场景未覆盖。
- 失败处理是二元的：通过 → 落盘；不通过 → 丢弃（按 block 或按 session 整体）。缺少中间态。

### 0.2 设计目标

1. 让精修器**感知整段对话的结构**，而不是只看 ±2 个 block。
2. 把"策略选择"显式化为一个决策模块，按 defect × block × context 三维度选策略。
3. 引入"待人工复核"作为合法的中间结果，避免非黑即白。

---

## 1. 三级响应框架（Refinement Policy）

| 策略 | 触发语义 | 适用场景 |
|---|---|---|
| **1. REPAIR_IN_PLACE** | 调整优化后保留 | 内容可用、缺陷可改写 |
| **2. PRUNE_** | 低相关内容可删除 | 内容冗余 / 与上下文无关 / 有更优等价版本 |
| **3. DEFER_TO_HUMAN** | 重要但不可自动修 | 关键转折 / 重试耗尽 / 跨轮次判断 |

### 1.1 三个子类（针对 PRUNE 细分）

| 子策略 | 含义 | 默认行为 |
|---|---|---|
| `PRUNE_BLOCK` | 仅删除单个 block | 保留所在 assistant 消息 |
| `PRUNE_WITH_PAIR` | 删除 block + 上一轮 user turn | 当 block 是 user 问题的直接应答且无后续引用 |
| `PRUNE_MESSAGE` | 整条 assistant 消息删除 | 消息级健康分已判定不健康且 block 精修耗尽 |

### 1.2 决策原则

```
defect 可改写    +  content 有引用价值  →  REPAIR_IN_PLACE
defect 可改写    +  content 已被覆盖     →  PRUNE_BLOCK
defect 不可改写  +  content 无引用价值  →  PRUNE_*
defect 不可改写  +  content 关键转折     →  DEFER_TO_HUMAN
```

---

## 2. ContextUnderstanding 模块设计

### 2.1 双层架构

```
┌─────────────────────────────────────────────┐
│  Active Window (近期窗口)                   │  cfg.context_active_window_size (3~5, 默认 4)
│  ─ 完整 blocks + 抽取事实/工具调用/决策     │
│  ─ 实体、toolcall → toolresult 引用关系 │
│  ─ tool 成功/失败标记 │
└─────────────────────────────────────────────┘
              │  滑动窗口 (随 current_block 移动)
              ▼
┌─────────────────────────────────────────────┐
│  Archived Summary (已读压缩)                 │  cfg.context_max_archive_chars (默认 80000)
│  ─ 分级保留（Tier 0~4，非统一压缩）         │
│  ─ 关键内容：全文保留                       │
│  ─ 重要内容：详述保留                       │
│  ─ 常规内容：简述保留                       │
│  ─ 噪声 / 冗余：指针 / 丢弃                 │
└─────────────────────────────────────────────┘
```

**核心原则**：
1. 不需要掌握完整上下文，只重点关注可配置 N 轮，其余压缩留存。
2. **压缩不是均匀的**：重要内容保留更多细节，无关内容大面积去除。
3. 容量预算 80k 字符（~30-40k 中文字，可容纳 50-80 条消息的结构化摘要）。

### 2.2 BlockContextView 数据结构

```python
@dataclass
class BlockContextView:
    """单个 block 的上下文视图 (供 policy / refiner 使用)"""

    # === 定位 ===
    block_id: str
    msg_idx: int
    block_idx: int
    block_type: str                                # thinking/toolcall/toolresult/text

    # === 近期窗口 (Active Window) ===
    active_window: list[MessageSnapshot]           # 最近 N 条消息的结构化快照
    window_siblings: list[str]                     # 同一消息内的 block_id 列表

    # === 已读部分 (Archive Summary) ===
    archive_summary: str                           # 滚动摘要 (≤ archive_max_chars)

    # === 跨 block 引用图 (基于 block_id) ===
    referenced_by: list[str]                       # 哪些后续 block 引用了我的输出
    depends_on: list[str]                          # 我引用了哪些前置 block

    # === 相关性评分 ===
    relevance_to_active: float                    # 与当前活跃窗口的相关度 [0, 1]
    is_redundant_in_window: bool                   # 窗口内是否已有等价/更优版本
    is_referenced_in_active: bool                  # 近期窗口是否引用我

    # === 角色判断 ===
    is_transition_point: bool                      # 是否承上启下的关键转折
    block_role: Literal["opening", "middle", "closing", "isolated"]

    # === 压缩用 ===
    entities_mentioned: set[str]                   # 实体集合 (供实体守恒校验)
    key_decisions: list[str]                       # 提取的决策句
```

### 2.3 ContextUnderstanding 类骨架

```python
class ContextUnderstanding:
    """对一条 session 构建 block 级别的上下文视图，供 policy 和 refiner 使用。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.active_window_size = cfg.context_active_window_size    # 默认 4
        self.max_archive_chars = cfg.context_max_archive_chars     # 默认 80000
        self.relevance_threshold = cfg.context_relevance_threshold  # 默认 0.6
        self.compression_strategy = cfg.context_compression_strategy  # "hybrid"
        self.max_llm_compressions = cfg.context_max_llm_compressions  # 默认 3

        # 内部状态
        self._block_index: dict[str, BlockContextView] = {}
        self._reference_graph: dict[str, set[str]] = {}
        self._archive: TieredArchive = TieredArchive()              # 分级 archive
        self._llm_compression_count: int = 0

    # --- 一次性构建 (整 session) ---
    def build(self, session: Session) -> None:
        self._index_blocks(session)
        self._build_reference_graph()
        self._extract_active_window(session)
        self._build_tiered_archive(session)         # 替代旧的 _compress_archive

    # --- 单 block 查询 (O(1)) ---
    def get_view(self, block_id: str) -> BlockContextView | None:
        return self._block_index.get(block_id)

    # --- archive 渲染 (供 refiner 注入 prompt) ---
    def render_archive(self, max_chars: int | None = None) -> str:
        return self._archive.render(max_chars or self.max_archive_chars)

    # --- 内部步骤 ---
    def _index_blocks(self, session: Session) -> None: ...
    def _build_reference_graph(self) -> None: ...
    def _extract_active_window(self, session: Session) -> None: ...
    def _build_tiered_archive(self, session: Session) -> None: ...   # 核心
    def _score_importance(self, block_id: str) -> float: ...         # 5 因子加权
    def _assign_tier(self, importance: float) -> int: ...             # 分数 → Tier 0~4
    def _evict_until_under_budget(self) -> None: ...                  # 降级保容量
    def _compress_tier0_with_llm(self) -> bool: ...                   # T0 合并摘要
    def _score_relevance(self, block_id: str) -> float: ...
    def _detect_redundancy(self, block_id: str) -> bool: ...
    def _detect_transition(self, block_id: str) -> bool: ...
```

### 2.4 4 个子能力

#### 2.4.1 近期窗口抽取（Active Window）

- 取最近 N 条 assistant 消息的**完整 blocks**。
- 结构化保留：toolcall.name + input、toolresult.output_text、thinking 全文、text 全文。
- 跨消息保留消息级元信息（msg_id / created_at / is_healthy）。

```python
@dataclass
class MessageSnapshot:
    msg_id: str
    role: str
    blocks: list[dict]                          # 简化后的 block 结构
    is_healthy: bool
    created_at: str
```

#### 2.4.2 已读压缩（Archive Compression）—— 分级保留

不再做"统一概括"式压缩。改为**按内容重要性分级保留**，高价值内容保细节，低价值内容大幅压缩或丢弃。

##### 5 个保留级别

| Tier | 内容类型 | 保留形态 | 典型压缩比 | 触发条件（示例） |
|---|---|---|---|---|
| **T0 - 全文** | 关键决策、最终结论、过渡转折点、首次成功突破 | 完整原文 | 1:1 | importance ≥ 0.7 |
| **T1 - 详述** | 错误与解决方案、关键 toolcall、含新实体的 toolresult | 句级摘要（保留参数/数值/错误码） | ~1:0.5 | 0.5 ≤ importance < 0.7 |
| **T2 - 简述** | 常规 tool result、辅助推理、重复观察 | 短语级（一句话） | ~1:0.2 | 0.3 ≤ importance < 0.5 |
| **T3 - 指针** | 调试噪声、失败尝试、已被后续覆盖的信息 | `block_id + 一句话结果` | ~1:0.05 | 0.1 ≤ importance < 0.3 |
| **T4 - 丢弃** | 完全冗余、无引用的碎片 | 仅记录 id（保留可追溯性） | 1:0 | importance < 0.1 |

##### 重要性评分公式

```
importance(block) =
    0.30 × error_relevance          # 是否含失败/错误/异常关键词（_NOISE_PATTERN 反向）
  + 0.25 × transition_signal        # 是否在消息/决策边界（开头/结尾 toolcall、决策性 thinking）
  + 0.20 × reference_count          # 被引用次数（来自引用图）
  + 0.15 × finality_signal          # 是否该方向上唯一/最终成功的尝试
  + 0.10 × entity_novelty           # 引入新实体 vs 重复实体
```

各子项均为 [0, 1] 区间；总分越高越倾向全文保留。

##### 数据结构

```python
@dataclass
class SummaryEntry:
    block_id: str
    tier: int                          # 0~4
    importance: float                  # 原始评分
    content: str                       # 按 tier 压缩后的内容
    referenced_by: list[str]           # 引用关系（保留便于追溯）


@dataclass
class TieredArchive:
    full: list[SummaryEntry]           # T0：原文
    detailed: list[SummaryEntry]       # T1：句级摘要
    compact: list[SummaryEntry]        # T2：短语
    pointers: list[SummaryEntry]       # T3：id + 一句结果
    dropped_ids: set[str]              # T4：仅 id
    
    total_chars: int                   # 跨所有 tier 的字符总数
    
    def render(self, max_chars: int) -> str:
        """渲染为可注入 prompt 的文本"""
        ...
    
    def evict_until_under_budget(self, max_chars: int) -> None:
        """按 T2→T3→T4 顺序降级, 直到 ≤ max_chars"""
        ...
```

##### 滚动更新流程

```
窗口滑动 (消息从 active window 滑出 → 进入 archive):
  ① 对离开消息的每条 block 计算 importance
  ② 按 tier 阈值分配到 T0/T1/T2/T3/T4
  ③ 累计 total_chars
  ④ 若 total_chars > cfg.context_max_archive_chars (默认 80000):
       → 先把最旧的 T2 降级为 T3
       → 仍超 → 把最旧的 T3 降级为 T4
       → 仍超 → 把最旧的 T1 降级为 T2
       → T0 永不降级 (关键内容保底)
  ⑤ 检查 T0 总数：若 T0 > cfg.context_max_t0_entries (默认 200), 触发 LLM 二次摘要压缩 T0
```

##### LLM 调用策略

LLM **只在以下情况**触发摘要生成（rule 优先）：

| 触发场景 | 调 LLM | 实现 |
|---|---|---|
| T0 数量超限触发压缩 | ✅ | judge_model 对最早 10 条 T0 做"合并摘要"，产出新的 T0 |
| T1 详述需要句级摘要 | 视模式 | `llm` 模式全部走 LLM；`hybrid` 模式仅在 rule 摘要置信度 < 0.6 时调 LLM |
| T2/T3 简述/指针 | ❌ | 全 rule（模板填空） |
| 重要性评分有歧义 | ❌ | rule 评分 |

防爆量约束：
- `cfg.context_max_llm_compressions`（默认 3）—— 单 session 内 LLM 压缩调用总次数上限。
- `cfg.context_max_t0_entries`（默认 200）—— T0 条数上限，超出触发 T0 合并摘要。

#### 2.4.3 跨 block 引用图（Reference Graph）

基于 block_id 构建：

```
thinking₁ ─→ toolcall₂ (input 提到 entity X)
toolresult₃ ←┘ (output_text 提到 entity X)
text₄ ─→ "根据 entity X 中的数据..." (引用 toolresult₃)
```

构建方式：
1. **实体共指**：block A 的输入/输出中提到的实体，在 block B 中再次出现 → A 被 B 引用。
2. **ID 直接引用**：block B 的 input 字段含 block_id（A.id）→ A 被 B 引用。
3. **时序相邻**：toolcall 之后的 toolresult 默认配对。

#### 2.4.4 相关性评分

```
relevance_to_active = (
    0.4 × is_referenced_in_active_window +    # 近期窗口是否引用我
    0.3 × has_successful_duplicate_in_window + # 窗口内是否有更优等价版本 (反向)
    0.2 × entity_overlap_with_active +          # 与窗口实体的交集
    0.1 × recency_score                          # 距离衰减
)
```

`is_referenced_in_active_window`：直接查引用图。
`has_successful_duplicate_in_window`：扫描窗口内同类型 block，比较语义相似度。
`entity_overlap_with_active`：Jaccard 相似度。
`recency_score`：`exp(-distance / half_life)`。

---

## 3. Policy 决策层

### 3.1 决策接口

```python
class RefinementPolicy(StrEnum):
    REPAIR_IN_PLACE = "repair_in_place"
    PRUNE_BLOCK = "prune_block"
    PRUNE_WITH_PAIR = "prune_with_pair"
    PRUNE_MESSAGE = "prune_message"
    DEFER_TO_HUMAN = "defer_to_human"


def decide_policy(
    block: BlockUnion,
    defects: list[DefectTag],
    context_view: BlockContextView,
    retry_exhausted: bool,
    cfg,
) -> RefinementPolicy:
    ...
```

### 3.2 决策表

| 当前缺陷 | 上下文特征 | 策略 |
|---|---|---|
| REPETITIVE_CALL | active window 已有成功版本 | PRUNE_BLOCK |
| REPETITIVE_CALL | active window 仅有失败版本 | REPAIR_IN_PLACE |
| REPETITIVE_CALL | active window 无 | REPAIR_IN_PLACE |
| CONTEXT_SWITCH_LOOP | archive 显示此前切换同样目的 | PRUNE_BLOCK |
| CONTEXT_SWITCH_LOOP | archive 无相关切换 | DEFER_TO_HUMAN |
| OBS_DEBUG_LEAK | output_text 被后续 block 引用 | REPAIR_IN_PLACE |
| OBS_DEBUG_LEAK | output_text 无任何引用 | PRUNE_BLOCK |
| TOOL_HALLUCINATED | 工具名无等价替代 | REPAIR_IN_PLACE |
| TOOL_HALLUCINATED | retry_exhausted=True | DEFER_TO_HUMAN |
| THOUGHT_TOO_LONG | 信息密度高（实体数 / 字符数 > 阈值） | REPAIR_IN_PLACE |
| THOUGHT_TOO_LONG | 信息密度低 | PRUNE_BLOCK |
| THOUGHT_BROKEN_LOGIC | is_transition_point=True | DEFER_TO_HUMAN |
| THOUGHT_BROKEN_LOGIC | 否则 | REPAIR_IN_PLACE |
| TEXT_FACT_HALLUCINATION | 数值/平台名在 archive 出现过 | REPAIR_IN_PLACE |
| TEXT_FACT_HALLUCINATION | 无来源 | DEFER_TO_HUMAN |

### 3.3 重试耗尽后的统一降级

```
REPAIR 失败 → escalate 到 32B → 仍失败 → 转为 DEFER_TO_HUMAN
                                              (而不是返回 None 静默丢弃)
```

这样确保**没有任何内容在没有人工痕迹的情况下消失**。

---

## 4. 配置项（加入 [config/settings.py](../config/settings.py)）

```python
# === 上下文理解 - 近期窗口 ===
context_active_window_size: int = 4          # 近期窗口消息数 (3~5 推荐)
context_relevance_threshold: float = 0.6     # 相关性阈值
context_redundancy_threshold: float = 0.85   # 判定"窗口内已存在等价版本"的语义相似度阈值

# === 上下文理解 - 分级压缩 ===
context_max_archive_chars: int = 80000       # archive 总字符上限 (默认 80k, 留 4 倍余量)
context_max_t0_entries: int = 200            # T0 全文条目上限, 超出触发 T0 合并摘要
context_compression_strategy: str = "hybrid" # rule / llm / hybrid
context_max_llm_compressions: int = 3        # 单 session LLM 压缩调用上限 (防爆量)

# === Tier 阈值 (重要性分数 → 级别) ===
context_tier0_threshold: float = 0.7         # ≥ 0.7 → T0 全文
context_tier1_threshold: float = 0.5         # 0.5~0.7 → T1 详述
context_tier2_threshold: float = 0.3         # 0.3~0.5 → T2 简述
context_tier3_threshold: float = 0.1         # 0.1~0.3 → T3 指针
# < 0.1 → T4 丢弃

# === 重要性评分子项权重 ===
context_importance_w_error:    float = 0.30  # 错误/失败信号
context_importance_w_transit:  float = 0.25  # 转折点
context_importance_w_refs:     float = 0.20  # 被引用次数
context_importance_w_finality: float = 0.15  # 唯一/最终成功尝试
context_importance_w_novelty:  float = 0.10  # 新实体占比

# === 决策层 ===
policy_defer_on_exhausted: bool = True       # REPAIR 失败耗尽是否转为 DEFER (而不是丢弃)
policy_prune_with_pair_enabled: bool = False # 是否启用"连带 user 删除" (默认关闭，保守)
policy_min_redundancy_for_prune: int = 2     # 窗口内至少 N 个等价版本才允许 PRUNE
```

---

## 5. 在流水线中的位置

```
load_session
 │
Router.tag ─────────► defects_index, health_scores
 │
ContextUnderstanding.build(session)  ◄── P0 新增：O(n) 一次性构建
 │
 │
 │
 │ 3 大精修器 ─► refined candidates
 │      │
 │      └─ validate_block (L1/L2/L3)
 │              │
 │              └─ Policy.decide(block, defects, context_view)  ◄── P0 新增
 │                     │
 │                     ├─ REPAIR_IN_PLACE ─► refiner (当前逻辑)
 │                     ├─ PRUNE_* ─► 加入 prune_ids 集合
 │                     ├─ DEFER_TO_HUMAN ─► 加入 deferred_ids 集合
 │                     │
 │                     ▼
 reassemble
 │ ── 在原有 _prune_repetitive_blocks / _prune_context_switch_blocks 之外
 │ ── 增加 context-driven prune_ids / deferred_ids 的处理
 │ ── 元数据落盘 policy_decisions / needs_review 标记
 │
save_session ─► output.json (+ metadata.deferred_blocks, metadata.policy_decisions)
```

---

## 6. 元数据扩展

在 `session.metadata` 中新增：

```python
{
  # 已有的
  "refine_history": [...],
  "validation_summary": {...},
  "modified_blocks": [...],

  # 新增
  "policy_decisions": [
    {
      "block_id": "blk_123",
      "defects": ["repetitive_call"],
      "policy": "prune_block",
      "reason": "active window already has 2 successful browser calls with same query",
      "context_relevance": 0.32,
    },
    ...
  ],
  "deferred_blocks": [
    {
      "block_id": "blk_456",
      "defects": ["thought_broken_logic"],
      "reason": "transition point with exhausted retries",
      "needs_review": True,
    },
    ...
  ],
  "context_stats": {
    "active_window_size": 4,
    "archive_summary_chars": 1820,
    "llm_compressions_used": 1,
  }
}
```

---

## 7. 实现优先级

| 阶段 | 内容 | 增量价值 | LLM 依赖 |
|---|---|---|---|
| **P0** | `BlockContextView` 数据结构 + 引用图 + active window 抽取（纯 rule） + 决策表（rule-based） | 立即改善 PRUNE 准确性 + DEFER 状态引入 | 无 |
| **P1** | 相关性评分 + 实体守恒 + 现有 defect 子集全覆盖决策 | 替代现有硬编码剪枝逻辑 | 无 |
| **P2** | LLM 压缩摘要 + hybrid 模式 + archive 滚动 | 处理超长会话 | judge_model |
| **P3** | 与 evaluator 集成（基于 context 理解的"语义冗余度"指标） | 评估器升级 | 视情况 |

P0 不引入任何 LLM 调用，纯 Python + 规则，可**立即落地**作为后续策略决策的基础设施。

---

## 8. 与现有 gdr 模块的关系

| 现有模块 | 现状 | 改造 |
|---|---|---|
| `routing/router.py` | 标 defect | 不变；新增 `decide_action()` 返回 (defect → policy) 映射初稿 |
| `pipeline/runner.py` | 调度 refiner + 落 record | 在 `_build_context` 之后调用 `context.get_view(block_id)`，传给 refiner |
| `refiners/*` | 9B→32B 重试 → None | 失败时**不再返回 None**，而是返回 `RefinementOutcome(refined=None, policy=DEFER)` |
| `reassembler/reassembler.py` | block 修剪 + 一致性终检 | 增加 `prune_ids` / `deferred_ids` 入参；处理 PRUNE 集合；落新 metadata |
| `validators/*` | L1/L2/L3 | 不变 |
| `data/sft_pairs.py` | 程序化 SFT 样本 | 扩展：根据 policy 选择性生成 PRUNE 训练样本 |
| `evaluator/*` | 双维评测 | P3：新增"context-driven 决策一致性"指标 |

---

## 9. 关键设计要点

1. **零 LLM 即可启动 P0**：ContextUnderstanding 的核心（active window 抽取、引用图、决策表 rule 版）不依赖任何 LLM 调用，避免在已经有 3 层 LLM 依赖（refiner / validator / judge）的情况下再加新瓶颈。

2. **决策层是纯函数**：`decide_policy(block, defects, context_view, retry_exhausted, cfg)` 无副作用，输入充分决定输出，**便于单测**。

3. **DEFER 是合法终态**：与"成功"和"失败"并列，落到 `metadata.deferred_blocks`，不会因为重试耗尽而悄悄丢失内容。

4. **PRUNE 保持保守**：默认 `policy_prune_with_pair_enabled=False`（不连带 user 删除），只在显式开启时才允许更激进删除。

5. **压缩预算硬限**：
   - `context_max_archive_chars=80000` —— archive 总字符上限；
   - `context_max_llm_compressions=3` —— 单 session LLM 压缩调用总次数上限；
   - `context_max_t0_entries=200` —— T0 全文条目上限。
   - 三层预算独立约束，防止长 session 让压缩成为新瓶颈。

6. **分级保留 vs 均匀压缩**：80k 字符预算 + 5 个 Tier (T0~T4) 取代"统一概括"模式。重要内容（决策、错误、关键 toolcall）保留全文或句级摘要；无关内容（debug 噪声、重复失败）可丢弃或仅留指针。**关键内容保细节，无关内容大面积去除**。

7. **T0 保底原则**：T0 永不降级为更高级别；若 T0 总数超限，唯一允许的"压缩"是触发 LLM 合并摘要产出新的 T0（保留关键内容，合并重复决策）。

8. **跨模块接口稳定**：`BlockContextView` 是 dataclass，新增字段不影响下游；policy 是 StrEnum，新增策略不破坏 switch。

---

## 10. 后续可探索方向

- **跨 session 上下文**：单条 session 上下文理解已覆盖；如果需要跨 session 摘要（用于 evaluator 的 SFT 数据准备），可以扩展 `ContextUnderstanding.build_corpus()`。
- **动态窗口大小**：根据 session 长度自适应（短会话 5、长会话 3），避免信息丢失。
- **Decision-aware 评测指标——Policy Consistency**：在 evaluator 中新增"refined 探针的最终决策是否与人工标注策略一致"的指标。

---

**变更记录**

| 日期 | 版本 | 内容 |
|---|---|---|
| 2026-08-25 | v0.1 | 初始设计：双层上下文架构 + 三级响应框架 + P0~P3 实现阶段 |
| 2026-08-25 | v0.2 | Archive 从"统一概括"改为**分级保留**（Tier 0~4）；容量 2k → **80k 字符**；新增重要性评分公式（5 因子加权）、`TieredArchive` 数据结构、`evict_until_under_budget` 降级策略、T0 合并摘要机制；新增 6 个 tier/importance 配置项 |