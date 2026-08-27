# GDR 增量状态追踪适配方案（落地文档）

> 状态：**已定稿待实施**。基于 `docs/优化上下文理解.md` 的适配章节整理而成，作为代码实施的唯一依据。
> 范围：仅限 `gdr/` 目录。方案采纳"直接采用**增量结构化状态追踪**模式"，不保留零 LLM 文本摘要回退。

---

## 1. 定位与目标

对 Agent 蒸馏轨迹做自动化清洗、剪裁与修补。核心痛点是长会话的**全局上下文摘要**：

- ❌ 一次性塞入：超长轨迹超 token 预算。
- ❌ 简单分段摘要再拼接：信息漂移，后段摘要遗忘前段关键 ID。
- ✅ **增量结构化状态追踪**：全局摘要 = 不断更新的 `GlobalState` JSON 快照序列，可计算、可校验、可编辑、可回滚。

---

## 2. 世界模型

### 2.1 数据模型映射（不改 QwenPaw schema）

| 通用 Trajectory 概念 | gdr 实际 | 说明 |
|---|---|---|
| `Trajectory` / `Step` | `Session` / `Message` + `BlockUnion` | QwenPaw 原生格式，保持不动 |
| `Step.thought` | `ThinkingBlock.thinking` | — |
| `Step.action` | `ToolcallBlock`（name + input） | — |
| `Step.observation` | `ToolresultBlock`（output_text + state） | — |
| 文本/最终答案 | `TextBlock` | — |

### 2.2 全局摘要的两种载体

| 载体 | 角色 | 说明 |
|---|---|---|
| `state_snapshots[i]`（`GlobalState`） | **唯一来源** | 增量 LLM 更新，O(n) token，可 diff、可回滚 |
| `TieredArchive`（T0~T4 文本压缩） | 降级 | 仅供 Router LLM 投票层做局部引用上下文 |

---

## 3. 增量状态追踪设计（核心改造）

### 3.1 GlobalState 数据结构

位于 `core/context_understanding.py`：

```python
@dataclass
class GlobalState:
    task_goal: str = ""                              # 任务目标
    current_step: str = ""                           # 当前所处步骤
    key_entities: dict[str, Any] = field(default_factory=dict)   # 关键实体 (order_id / user_intent 等)
    completed_actions: list[str] = field(default_factory=list)   # 已完成动作
    archived_actions: list[str] = field(default_factory=list)    # 状态衰减: 已完成且不再相关的动作
    open_issues: list[str] = field(default_factory=list)         # 未决问题
    last_error: str | None = None                    # 最近一次错误
    critical_constraints: list[str] = field(default_factory=list) # 关键约束
```

### 3.2 增量更新流程

```mermaid
graph LR
    A[初始空状态] --> B[切分Chunk1]
    B --> C[LLM: 当前状态 + Chunk1 → 完整状态JSON]
    C --> D[快照 v1]
    D --> E[切分Chunk2]
    E --> F[LLM: 状态v1 + Chunk2 → 状态JSON]
    F --> G[快照 v2]
    G --> H[...]
    H --> I[最终快照 vn = 全局上下文摘要]
```

1. **按工具调用边界切分 Chunk**：每个 Chunk 含 1~3 个完整 `toolcall-toolresult` 对，绝不拆开 Action-Observation。`thinking` 归属其随后 toolcall；`text` 归属最近的 Chunk 尾部。
2. 初始状态为空 `GlobalState`。
3. 依次将 `{当前状态JSON} + {新Chunk原文}` 发送 `main_model`（9B，小模型优先）。Prompt 核心指令：
   > 你是一个 Agent 轨迹状态追踪器。请根据新的对话片段，仅更新状态 JSON 中发生变化的字段。未提及的字段保持原值。如果新片段包含关键实体、约束或错误，必须提取并写入对应字段。输出纯 JSON。
4. 模型输出**完整状态 JSON**（不是 diff，避免合并错误），替换当前状态。
5. 每个 Chunk 边界保存快照：`state_snapshots[chunk_idx] = deepcopy(state)`。
6. 最终快照 `state_snapshots[n-1]` 即全局摘要，注入下游（Router 投票层 / refiner / 一致性校验）。

### 3.3 容错设计

| 故障 | 处理 |
|---|---|
| 输出非法 JSON | `jsonschema` 校验 → `json_repair` 修复 → 重试 1 次 |
| 重试仍失败 | 保留上一版状态快照 + 日志告警，不中断流水线 |
| 单 session 调用超限（默认 20） | 剩余 Chunk 沿用上一版状态 + 告警 |

### 3.4 成本控制

- 小模型优先：默认 `main_model`（9B），可按 `state_escalate_to_tool_model` 升级 32B。
- 状态字段裁剪：极长轨迹下已完成的无关动作移入 `archived_actions`。
- Chunk 粒度 1~3 个工具调用周期，避免固定轮数切坏 Action-Observation 对。

---

## 4. 与现有能力融合（关键决策）

| 现有能力 | 处置 |
|---|---|
| `_index_blocks` / `_build_reference_graph` / `_score_all_importance` | **保留**：纯计算，服务 policy 决策与 PRUNE 保护 |
| `BlockContextView` 定位/引用/角色字段 | **保留**：编辑定位与决策仍依赖 |
| `TieredArchive` 全局摘要角色 | **降级**：仅供 Router 投票层；`render_archive` 输出改为"最新快照渲染 + 分级 archive"两段式 |
| `archive_summary` 字段 | 内容替换为当前状态快照渲染文本 |
| `_llm_compression_count` 与 LLM 摘要 | **删除**：状态追踪替代，不再需要 T0 合并摘要 |

### 新增 API（`ContextUnderstanding`）

```python
state_snapshots: dict[int, GlobalState]          # chunk_idx -> 快照
chunk_of_block(block_id) -> int | None           # block 所属 chunk
latest_state() -> GlobalState                    # 全局摘要
snapshot_at(chunk_idx) -> GlobalState | None
state_after(session, chunk_idx, cfg) -> GlobalState | None   # 编辑后重跑单 chunk
num_chunks: int
chunk_blocks: dict[int, list[str]]               # chunk -> block_ids
```

---

## 5. 分模块适配

### 5.1 规则硬过滤层（session 级入口）

`pipeline/runner.py` 的 `process_one` 最前端：

```python
def _hard_filter_session(session: Session, cfg: Settings) -> bool:
    total_blocks = sum(len(m.blocks) for m in session.messages)
    if total_blocks > cfg.session_max_blocks and not _has_successful_terminal(session):
        log.warning("hard filter: too many blocks without successful termination")
        return False
    if session.error or any(m.error for m in session.messages):
        log.warning("hard filter: session/message has error field")
        return False
    return True
```

### 5.2 滑动窗口打分 / 标签

`BlockContextView` 追加训练导向字段（不替换现有重要性评分）：

```python
quality_score: float = 0.0            # 0-1 综合训练质量分
quality_breakdown: dict[str, float] = field(default_factory=dict)
step_label: str | None = None         # important / failed_tool_call / recovery_step / ...
```

LLM 复核上下文：`{前Chunk状态快照} + {前中后3个block原文} + {后Chunk状态快照}`。

维度映射：

| 通用维度 | gdr 计算依据 | 说明 |
|---|---|---|
| 规划完整性 | `key_decisions` 非空 + thinking 含步骤词 | 已有 |
| 工具对齐度 | 引用图中 toolcall 与 thinking 实体交集 | 已有 |
| 错误归因 | toolresult=error 后 thinking 含归因关键词 | 可新增规则 |
| 信息冗余度 | `is_redundant_in_window` + 实体重复度 | 已有 |
| 关键性 | `importance` + `is_transition_point` | 已有 |

实现：新增 `core/quality_scorer.py`，纯规则计算。

### 5.3 剪裁修补引擎：三种失败调用处理模式

`core/policy.py` 新增：

```python
class FailureHandlingMode(StrEnum):
    CLEAN = "clean"    # 只保留成功路径
    ROBUST = "robust"  # 保留 1 次典型错误 + 恢复
    DROP = "drop"      # 连续失败过多且无明确恢复 → 丢弃
```

| 模式 | 判定 | 动作 |
|---|---|---|
| CLEAN | 存在成功结果 | `PRUNE_BLOCK` 删除同组失败尝试 |
| ROBUST | 失败次数 ≤ `robust_max_failure_streak`(3) | 保留最后一次失败及恢复步骤，`thought_refactor` 显式补充错误原因 |
| DROP | 失败 > 3 或 health 过低 | `PRUNE_MESSAGE` 或 session 丢弃 |

### 5.4 全局一致性校验：状态快照对比 + 自动回滚

`reassembly/reassembler.py` 复用状态追踪器（替代重建整个 CU）：

```python
def _validate_edit_consistency(session, refine_records, cu, cfg):
    edited_chunks = {
        cu.chunk_of_block(r.block_index.block_id)
        for r in refine_records if r.result == "success"
    } - {None}
    first_edited = min(edited_chunks)
    for ci in range(first_edited, cu.num_chunks):
        new_state = cu.state_after(session, ci, cfg)
        if new_state is None:
            # 标记该 chunk 及之后所有编辑为 needs_review
            return refine_records
        before = cu.state_snapshots[ci]
        lost = _lost_critical_fields(before, new_state)
        if lost:
            # 回滚该 chunk 内所有编辑: r.refined_content=None
            # r.result="rollback"; r.edit_status=ROLLBACK
            cu.state_snapshots[ci] = before   # 刷新快照后继续
    return refine_records

def _lost_critical_fields(before, after) -> list[str]:
    lost = []
    delta = set(before.key_entities) - set(before.archived_actions) \
          - (set(after.key_entities) - set(after.archived_actions))
    if delta: lost.append("key_entities")
    if set(before.critical_constraints) - set(after.critical_constraints):
        lost.append("critical_constraints")
    if before.task_goal and before.task_goal != after.task_goal:
        lost.append("task_goal")
    return lost
```

user 判定规则：
- 关键字段丢失/冲突 → 自动回滚该 Chunk 修改，`edit_status = "rollback"`。
- 状态重跑失败无法自动判断 → `"needs_review"` 进入人工审核队列。

### 5.5 兜底与反馈：edit_status 与版本血缘

`domain/schema.py`：

```python
class StepEditStatus(StrEnum):
    UNTOUCHED = "untouched"   # 默认
    EDITED = "edited"
    PRESERVED = "preserved"
    ROLLBACK = "rollback"
    NEEDS_REVIEW = "needs_review"

class BlockRefineRecord(BaseModel):
    # ... 现有字段 ...
    result: Literal["success", "failed", "escalated_then_failed", "rollback"] = "failed"
    edit_status: StepEditStatus = StepEditStatus.UNTOUCHED
```

`_attach_metadata` 新增：

```python
session.metadata["edit_status_summary"] = {...}
session.metadata["original_session_id"] = session.session_id   # 血缘追溯
session.metadata["refined_version"] = "v2"
```

人工审核队列：`DEFER_TO_HUMAN` 块已写入 `metadata.deferred_blocks`，另输出独立文件 `refine_data/deferred.jsonl` 便于批量审核反哺 prompt/规则更新。

---

## 6. 流水线位置（process_one）

```
load_session
  → 硬过滤 (5.1)
  → light_health
  → ContextUnderstanding.build()  # 含增量状态追踪 → state_snapshots
  → fold_failed_toolresults / fold_repeated_thinking
  → Router.tag(defects, health, cu)   # 投票层注入最新状态快照
  → policy 决策 (REPAIR / PRUNE / DEFER / 三种失败模式)
      → refiner → validate_block(L1/L2/L3)
  → reassemble()
      ├─ 决策层 prune / message 健康分剪枝（现状）
      ├─ _validate_edit_consistency (5.4)   ← 新增
      └─ L3 judge 一致性终检（现状）
  → save_session (+ deferred.jsonl)
```

---

## 7. 配置项（新增到 config/settings.py）

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `session_hard_filter_enabled` | `True` | session 级硬过滤开关 |
| `session_max_blocks` | `200` | 单 session 最大 block 数 |
| `failure_handling_mode` | `"clean"` | clean/robust/drop |
| `robust_max_failure_streak` | `3` | robust 模式允许的最大连续失败次数 |
| `enable_quality_scorer` | `True` | 训练质量维度评分开关 |
| `enable_edit_consistency_check` | `True` | 编辑前后状态快照校验开关 |
| `consistency_rollback_on_entity_loss` | `True` | 关键字段丢失时自动回滚 |
| `deferred_output_path` | `./refine_data/deferred.jsonl` | 人工审核队列输出路径 |
| `context_state_tracker_enabled` | `True` | 增量状态追踪开关（无回退开关） |
| `context_chunk_max_tool_pairs` | `3` | 每 Chunk 最大 toolcall-toolresult 对数 |
| `context_max_state_llm_calls` | `20` | 单 session 状态追踪 LLM 调用上限 |
| `context_state_model` | 用 `main_model` | 状态追踪模型（小模型优先） |
| `state_escalate_to_tool_model` | `False` | 复杂歧义场景升级 32B |
| `context_state_max_retries` | `1` | 单 chunk 状态更新失败重试次数 |

废弃配置（保留在 Settings 但不生效）：`context_compression_strategy` / `context_max_llm_compressions` / `context_max_t0_entries`。

---

## 8. 实施优先级

| 阶段 | 内容 | 改动文件 |
|---|---|---|
| **P0** | 增量状态追踪核心：`GlobalState` + Chunk 切分 + 状态更新 Prompt + 快照 + 容错 | `core/context_understanding.py`, `config/settings.py`, `prompts/state_tracker.yaml` |
| **P0** | 下游接入：Router 投票层 / refiner 消费状态快照，TieredArchive 降级 | `routing/router.py`, `pipeline/runner.py` |
| **P0** | session 级硬过滤入口 | `pipeline/runner.py`, `config/settings.py` |
| **P0** | 失败调用三种模式接入 policy | `core/policy.py`, `config/settings.py` |
| **P1** | 编辑前后状态快照校验 + 自动回滚 | `reassembly/reassembler.py`, `core/context_understanding.py`, `domain/schema.py` |
| **P1** | `edit_status` 与版本血缘元数据 | `domain/schema.py`, `reassembly/reassembler.py` |
| **P1** | 训练质量评分模块 | `core/quality_scorer.py` |
| **P2** | 状态衰减优化（`archived_actions`）+ 32B 升级策略 | `core/context_understanding.py` |
| **P2** | 人工审核队列独立输出 + 反馈回路 | `pipeline/runner.py`, `evaluator/feedback.py` |

---

## 9. 接口兼容性

- `ContextUnderstanding.build()` 签名不变，内部追加状态追踪；`get_view()` / `render_archive_for_block()` 保持可用。
- `decide_policy` 签名不变，新增读取 `cfg.failure_handling_mode`。
- `reassemble()` 新增可选参数 `cu`，不传则跳过编辑前后快照对比（兼容旧调用）。
- `BlockRefineRecord.result` 增加 `"rollback"`，`edit_status` 默认 `untouched`，不影响已有分支。
- CU 构建失败 fallback 保持现状（`context_understanding=None` 走旧 ±N 上下文）；仅状态追踪阶段失败沿用上一版快照。