# C3 — Final SFT Views

> `etl → 训练 / 审计` 输出契约。etl 对 C2 refined Session 做格式整理
> （system prompt 切分 / tool schema 持久化 / tool output 摘要 / qf_text 渲染 /
> openai_messages 渲染 / usage_prune）后拆分为 4 视图文件。

## 1. 生产者

`orchestration/workers/etl_worker.py::EtlWorker.process`（新；原 `qf_worker` 改名 / 重写）

- 输入：C2 refined Session（路径 `output/refined/<TXXX>__<session_id>.json`）
- 处理链：
  1. `etl.parsers.refined_session.load_refined_session(path)` —— 读 C2
  2. `etl.qwenformat.usage_prune.collect_usage(session)` —— 取真实调用的 tools/skills
  3. `etl.qwenformat.usage_prune.prune_session_in_place(session, ...)` —— 裁
     system/tools + 重渲染 `metadata.qf_text`
  4. `etl.qwenformat.transform.trajectory_to_session_with_openai_metadata(...)`
     —— 写 `metadata.openai_messages` / `metadata.tools` / `metadata.qf_rendered_at`
     / `metadata.qf_stats`
  5. `etl.qwenformat.system_prompt.partition_system_prompt(...)` —— 切分 system 段落
  6. `etl.qwenformat.tool_templates.save_tool_templates(...)` —— tool schema 持久化
  7. 可选 `etl.qwenformat.tool_output_summarizer.summarize_record(...)` —— tool
     result 精简（L0 规则 + 可选 L1 LLM 锚点）
  8. `etl.writers.split_4_views(session, base_path)` —— 拆 4 视图落盘
- 输出：4 份文件写到 `output/refine_data/<stem>_refined.{messages,openai,qwenjina.txt,meta}.json`

## 2. 文件清单（4 视图）

```
output/refine_data/<TXXX>__<session_id>_refined.messages.json   ← 块视图（refined blocks）
output/refine_data/<TXXX>__<session_id>_refined.openai.json     ← OpenAI function-calling 视图
output/refine_data/<TXXX>__<session_id>_refined.qwenjina.txt    ← Qwen3 chat_template 纯文本（qf_text）
output/refine_data/<TXXX>__<session_id>_refined.meta.json       ← 全量 metadata + audit + 渲染附属
```

`<TXXX>` 沿用 C2 写入的 `task_id`；`<session_id>` 沿用 C2 写入的 `session_id`。
4 份文件共用同一 stem，命名/后缀不可改。

## 3. `<base>.messages.json` —— 块视图

```json
{
  "schema_version": "sft_views.v1",
  "session_id": "useramulation-...",
  "messages": [Message, ...],
  "tools": [ToolDef, ...]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | str | 固定 `"sft_views.v1"` |
| `session_id` | str | 取自 C2 |
| `messages` | list[Message] | 与 C2 `session.messages` 一致（post usage_prune） |
| `tools` | list[ToolDef] | 与 C2 `session.tools` 一致（post usage_prune） |

Message / Block 形态与 C2 §4 §5 一致；唯一的差异是 message 经过
usage_prune 后 system message 的内容可能更紧凑（移除未被调用的 tool 列表
对应段落）。

## 4. `<base>.openai.json` —— OpenAI function-calling 视图

```json
{
  "schema_version": "sft_views.v1",
  "session_id": "useramulation-...",
  "openai_messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": null,
     "tool_calls": [{"id": "call_xxx", "type": "function",
                     "function": {"name": "web_search",
                                  "arguments": "{...}"}}]},
    {"role": "tool", "tool_call_id": "call_xxx", "name": "web_search",
     "content": "..."}
  ],
  "tools": [ToolDef, ...]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | str | 固定 `"sft_views.v1"` |
| `session_id` | str | 取自 C2 |
| `openai_messages` | list[dict] | etl transform 渲染后写入 |
| `tools` | list[ToolDef] | 末次 `model_request.payload.tools` 形态（OpenAI function 定义） |

OpenAI tool_call id 形态：trajectory 里是 `toolu_xxx`，etl transform 统一加
`call_` 前缀变 `call_xxx`（与 `agent-trajectory-format.md §9` 一致）。

## 5. `<base>.qwenjina.txt` —— Qwen3 chat_template 纯文本

- 纯文本（无 JSON 包装），单段连续字符串，UTF-8
- 内容 = `session.metadata.qf_text`（`usage_prune` 重渲染后）
- **qf_text 缺失则跳过不写**（不报错、不写空文件）—— 该约定来自
  `docs/设计方案/refined_split_plan.md §2 决策汇总`
- 渲染规则：见 `etl/qwenformat/chat_template.jinja` 与
  `agent-trajectory-format.md §8`（ChatML 标签 + `<think>` 块 + tool_call /
  tool_response 配对）

## 6. `<base>.meta.json` —— 全量 metadata

```json
{
  "schema_version": "sft_views.v1",
  "session_id": "useramulation-...",
  "original_session_id": "useramulation-...",
  "refined_version": "v2",

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
  "meta_tag_contamination": {...},
  "user_intent": {...},
  "training_value_score": 0.78,
  "complexity_tier": "medium",

  "qf_rendered_at": "2026-09-22T...Z",
  "qf_stats": {"total_chars": 12345, "render_seconds": 0.42, ...},
  "tools": [ToolDef, ...],
  "system_prompt_partitions": {"role": "...", "constraints": [...],
                                "framework": "...", "unknown": [...]}
}
```

字段来源：
- 前半（refine_history → training_value_score 等）—— **直接复制自 C2 metadata**，
  etl 不修改；保留 audit 完整性
- 后半（qf_rendered_at / qf_stats / tools / system_prompt_partitions）——
  etl transform / usage_prune / system_prompt 阶段新增

## 7. 与 C2 的一致性约束

1. C3 不修改 refine 阶段产出的任何字段（refine_history / validation_summary /
   policy_decisions / judge_discard / user_intent / training_value_score 等）
2. C3 只新增以下字段：`qf_rendered_at` / `qf_stats` / `system_prompt_partitions`
3. C3 可能修改 `messages` / `tools` / `system_prompt`（usage_prune 裁剪），
   这些修改是 etl 的职责
4. `meta_tag_contamination` 字段由 gdr 写入，C3 不修改（已剥离的 ⟦⟧ 不会被
   etl 重新引入）

## 8. 训练消费形态

参考 [docs/设计方案/cot-sft-trajectory-data-spec.md](../设计方案/cot-sft-trajectory-data-spec.md)：

- 主流：`<base>.qwenjina.txt`（Qwen3 ChatML 纯文本喂训练，无需反序列化）
- 备选：`<base>.openai.json`（OpenAI SFT 框架可直接消费）
- 审计：`<base>.meta.json`（verify / 去重 / 阈值核对）
- 调试：`<base>.messages.json`（块视图，便于 diff）

## 9. 失败模式

| 失败点 | 现象 | 处理 |
|---|---|---|
| C2 文件缺失 | worker 抛 `FileNotFoundError` | queue 重试，超 max → `dead` |
| C2 缺 `qf_text` 渲染依赖（system 缺失） | transform 抛 | worker 包 `NonRetryableError` → `dead` |
| chat_template.jinja 缺失 / 语法错 | transform 抛 | 启动时校验，worker 启动失败 → master 不调度 |
| usage_prune 内部异常 | `prune_session_in_place` 抛 | worker 抛 `RetryableError` → 重试 |
| tool_output_summarizer LLM 异常 | 摘要失败，保留原文 | `block.metadata["raw_output"]` 落盘，`summarizer_failed=true` |
| 4 视图落盘某一份写失败（磁盘满） | 部分文件存在 | worker 抛 → 重试；下次写覆盖（atomic rename） |

## 10. 不在本契约范围内的内容

- `output/refined/*.json` —— C2 契约范围（etl 的输入）
- `output/agent_trajectory/*.json` —— C1 契约范围
- `output/datasets/{all_runs.v2.jsonl, distill_dataset.v2.jsonl}` —— simulate_serve
  自导出，新流程可下线（保留为可选 fallback，不进入主链路）