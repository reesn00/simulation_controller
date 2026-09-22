# C2 — Refined Session

> `gdr → etl` 输入契约。gdr 对 trajectory 做 refine 后产出的 Session 对象。

## 1. 生产者

`gdr/pipeline/runner.py::_process_one_file`（末尾 `save_refined_session`）

- 输入：C1 trajectory events（经 `gdr.parsers.from_trajectory` 解析为 Session）
- 处理：硬过滤 + health + CU + fold + retry_loop_clip + user_intent + router +
  policy + refiners（并发） + validators（L1/L2/L3） + reassemble + meta_tag_strip
- 输出：单 Session JSON，写到 `output/refined/`
- 与 etl 的耦合：**不调用 etl**（`_apply_usage_prune` 在新流程中删除）

## 2. 文件形态

| 项 | 约束 |
|---|---|
| 路径 | `output/refined/<TXXX>__<session_id>.json` |
| 扩展名 | `.json` |
| 顶层 | 单 Session 对象（不是列表，不是包裹对象） |
| 编码 | UTF-8，pretty-print 关闭（紧凑单行） |
| schema_version | `"refined_session.v1"` |

`<TXXX>` 是 `task_id`（如 `T001`），由 orchestrator 通过 `run_id → task_id`
映射从 SQLite 取，写入队列的 `task_id` 字段；gdr 从队列读 `task_id`，不依赖
trajectory 文件名反推。

## 3. 顶层字段

```json
{
  "schema_version": "refined_session.v1",
  "session_id": "useramulation-8f58b83c2f814cceaaec46ed64e59af7",
  "original_session_id": "useramulation-8f58b83c2f814cceaaec46ed64e59af7",
  "refined_version": "v2",
  "run_id": "run_6031ae99...",
  "task_id": "T001",
  "source_file": "output/agent_trajectory/run_6031ae99...__useramulation-...json",
  "summary": "...",

  "model_name": "qwen3.8-flash",
  "provider_id": "qwenpaw",
  "agent_id": "agent_xxx",
  "trace_ids": ["trace_xxx"],
  "event_count": 487,
  "event_types": ["turn_start", "model_request", "model_response",
                  "tool_execution", "final_reply"],

  "messages": [Message, ...],
  "tools": [ToolDef, ...],

  "metadata": {
    "refine_history": [...],
    "validation_summary": {...},
    "policy_decisions": [...],
    "modified_blocks": [...],
    "edit_status_summary": {...},
    "folded_failed_toolresults": [...],
    "routing_abstentions": [...],
    "unknown_tool_names": [...],
    "judge_discard": null,
    "judge_unavailable_at": null,
    "meta_tag_contamination": {"has_meta_tag": false, "total_count": 0,
                                "occurrences": []},
    "user_intent": {"heuristic": "...", "llm": "..."},
    "training_value_score": 0.78,
    "complexity_tier": "medium",
    "health_score": 0.85,
    "intent_achievement": 0.82,
    "unrecognized_event_types": []
  }
}
```

### 3.1 必填字段

- `schema_version` —— `"refined_session.v1"`
- `session_id`, `run_id`, `task_id`, `source_file`
- `messages[]` —— refined blocks；empty session 仍写出
- `tools[]` —— refined 后保留的工具 schema；可能为空
- `metadata.refine_history[]` —— 每次精修的 `[module, attempts, model_used, result, reason, block_id]`
- `metadata.validation_summary` —— `total_blocks / modified_blocks / passed_L{1,2,3} / failed_L{1,2,3}`

### 3.2 可选字段（缺失写 null）

- `summary` —— 取自 model_request 首事件的 system summary
- `original_session_id` —— 同 `session_id`（保留字段，跨血缘追溯）
- `refined_version` —— 固定 `"v2"`
- `model_name / provider_id / agent_id` —— 取自 trajectory 末事件
- `trace_ids / event_count / event_types` —— 取自 trajectory 重放统计
- `metadata.*` 其余字段 —— gdr 内部自由扩展，etl **必须容忍未知字段**

### 3.3 metadata 字段约束（写入示例见 §6）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `refine_history` | list[dict] | ✓ | 每个被精修的 block 一条记录 |
| `validation_summary` | dict | ✓ | 三层校验聚合 |
| `policy_decisions` | list[dict] | 否 | 决策层五选一的输出 |
| `modified_blocks` | list[str] | 否 | 成功编辑的 block_id |
| `edit_status_summary` | dict | 否 | 五种 status 计数 |
| `folded_failed_toolresults` | list[dict] | 否 | fold 阶段折叠的失败 tool_result |
| `routing_abstentions` | list[dict] | 否 | Router 弃权审计 |
| `unknown_tool_names` | list[str] | 否 | tool_fixer 拒识的工具名 |
| `judge_discard` | dict \| null | 否 | judge 拒收详情（fix A） |
| `judge_unavailable_at` | str \| null | 否 | judge 不可用时间戳 |
| `meta_tag_contamination` | dict | ✓ | ⟦⟧ 剥离统计（F3-D） |
| `user_intent` | dict | 否 | 启发式 + LLM 两版 |
| `training_value_score` | float ∈ [0,1] | 否 | quality_scorer 输出 |
| `complexity_tier` | str ∈ {easy,medium,hard} | 否 | quality_scorer 输出 |
| `health_score` | float | 否 | routing.health 输出 |
| `intent_achievement` | float | 否 | judge 终评输出 |
| `unrecognized_event_types` | list[str] | 否 | 解析时跳过的未知事件类型 |

## 4. Message 形态

```json
{
  "role": "system | user | assistant",
  "name": null,
  "id": "msg_xxx",
  "blocks": [Block, ...],
  "metadata": {},
  "usage": {"input_tokens": ..., "output_tokens": ..., "time": ...,
            "cache_creation_input_tokens": 0, "cache_input_tokens": ...},
  "error": null,
  "created_at": "2026-09-22T...Z",
  "finished_at": "2026-09-22T...Z"
}
```

- `system` 消息：`blocks` 通常为 1 个 text block（保留原文，未切分）
- `user` 消息：`blocks` 为 1 个 text block（来自 `turn_start.input_text`）
- `assistant` 消息：`blocks` 含该轮全部块（按发生顺序：
  `thinking, tool_call×n, tool_result×n, ..., text`）

## 5. Block 形态

| type | 关键字段 | 说明 |
|---|---|---|
| `text` | `id`, `text`, `created_at`, `finished_at` | 可见文本 |
| `thinking` | `id`, `thinking`, `created_at`, `finished_at` | **结构化思维链**（独立块，非 meta 内嵌） |
| `tool_call` | `id`, `name`, `input` (JSON str), `state="finished"` | gdr 归一化为 finished |
| `tool_result` | `id`, `name`, `output_text`, `state ∈ {success, error}`, `metadata` | output_text 已合并 ContentBlock 列表 |

注：
- `tool_call.input` 是 JSON 字符串（OpenAI 导出形态）
- `tool_result.output_text` 是字符串（合并 ContentBlock 列表后的纯文本）
- `state="finished"` / `state="success"` 已是 refine 后的稳定形态，不再含
  `pending` / `running` 等中间态

## 6. metadata 字段写入示例

### 6.1 refine_history

```json
[
  {"module": "thought_refactor", "attempts": 1, "model_used": "9B",
   "result": "passed", "reason": null, "block_id": "blk_2"},
  {"module": "tool_fixer", "attempts": 2, "model_used": "9B→32B",
   "result": "exhausted", "reason": "tool_fixer_exhausted", "block_id": "blk_7"}
]
```

每个被精修的 block 一条；失败的精修（`result ∈ {failed, escalated, exhausted}`）也记录。

### 6.2 validation_summary

```json
{
  "total_blocks": 47,
  "modified_blocks": 12,
  "passed_L1": 12,
  "passed_L2": 10,
  "passed_L3": 1,
  "failed_L1": 0,
  "failed_L2": 2,
  "failed_L3": 0
}
```

### 6.3 meta_tag_contamination

```json
{"has_meta_tag": false, "total_count": 0, "occurrences": []}
```

⟦⟧ 已被 gdr 递归剥离；此字段记录剥离统计（用于审计）。

### 6.4 policy_decisions（每块一条）

```json
[
  {"block_id": "blk_3", "strategy": "REPAIR_IN_PLACE",
   "defect_tags": ["TOOL_JSON_INVALID"], "rationale": "..."},
  {"block_id": "blk_9", "strategy": "PRUNE_BLOCK",
   "defect_tags": ["TOOL_OFF_TOPIC"], "rationale": "..."},
  {"block_id": "blk_12", "strategy": "DEFER_TO_HUMAN",
   "defect_tags": ["TOOL_HALLUCINATED"], "rationale": "..."}
]
```

`strategy ∈ {REPAIR_IN_PLACE, PRUNE_BLOCK, PRUNE_WITH_PAIR, PRUNE_MESSAGE, DEFER_TO_HUMAN}`

## 7. 旁路文件（audit-only，不进 etl）

| 路径 | 触发条件 | 内容 |
|---|---|---|
| `output/refine_data/incomplete.jsonl` | F2 / F3-D / F3-E 任一命中 incomplete | session 整 dump（含原始 blocks） |
| `output/refine_data/judge_low.jsonl` | judge 评分 < 阶梯阈值 | session 整 dump + judge 详情 |
| `output/refine_data/deferred.jsonl` | DEFER_TO_HUMAN 决策非空 | block_id 列表 + 缺陷标签 |
| `output/refine_data/routing_low.jsonl` | Router 投票解析失败 | block_id 列表 + 投票详情 |

旁路文件路径沿用 `output/refine_data/`（即使目录名是历史遗留 "refine_data"，
新流程中该目录只承载 audit 旁路）；不在新 etl 流水线范围内。

## 8. 与 C3 的关系

etl 通过 `etl.parsers.refined_session.load_refined_session(path) -> Session` 接收 C2；
然后按顺序调用：

1. `etl.qwenformat.usage_prune.collect_usage(session)` —— 取真实调用的 tools/skills
2. `etl.qwenformat.usage_prune.prune_session_in_place(session, ...)` —— 裁
   system/tools + 重渲染 `metadata.qf_text`
3. `etl.qwenformat.transform.trajectory_to_session_with_openai_metadata(...)`
   —— 写 `metadata.openai_messages` / `metadata.tools` / `metadata.qf_rendered_at`
   / `metadata.qf_stats`
4. `etl.writers.split_4_views(session, base_path)` —— 拆 4 视图落盘（详见 C3）

约定：etl 通过 `gdr.parsers` 借用 `Session` / `Message` / `Block` 的 pydantic
类型 + `save_session_v2` 拆分写入函数；**etl 不直接构造 Session**，只读取
C2 后 mutate 其 `metadata` / `messages` / `tools`，再调用 gdr domain 暴露的
4-视图写入函数。

## 9. 失败模式

| 失败点 | 现象 | 处理 |
|---|---|---|
| 精修全部失败 | session 仍写出（挂 metadata） | etl 仍可读 → 输出 4 视图 → audit 通道审 |
| judge 不可用 | session 写出，`judge_unavailable_at` 记录时间 | etl 仍可读 |
| 完整性检查命中 incomplete | 不写 refined | 改写 `incomplete.jsonl` |
| LLM 卡顿 / 网络异常 | `process_one` 抛 `RetryableError` | queue 重试，超 max → `dead` |
| meta_tag strip 全无命中 | 正常 | `meta_tag_contamination.has_meta_tag = false` |

## 10. 不在本契约范围内的内容

- `output/agent_trajectory/*.json` —— C1 契约范围
- `output/refine_data/_refined.{messages,openai,qwenjina.txt,meta}.json` —— C3 契约范围
- `qf_out/` —— 历史遗留，新流程不再生成；存量可忽略