# etl/pawsession 使用说明

## 模块概述

将 QwenPaw agent trajectory JSONL（`output/agent_trajectory/` 目录下）批量转换为 OpenAI function-calling 格式的 SFT（监督微调）数据。

转换产物：

| 产物 | 路径 | 说明 |
|---|---|---|
| `sft_openai.jsonl` | `<output>/sft_openai.jsonl` | 每行一条样本（OpenAI messages 格式） |
| `sft_openai.json` | `<output>/sft_openai.json` | 数组形式，便于人工查看 |
| `audit/` | `<output>/audit/<session_id>.json` | 每个会话的审计：简化原始 state + 中间消息 + 统计 |
| `stats.json` | `<output>/stats.json` | 全量汇总统计 |

## 用法

```bash
python run_etl.py --input output/agent_trajectory --output output [选项]
```

### Windows 批处理

```bat
run_etl.bat                       :: 处理全部
run_etl.bat 5                     :: 只处理 5 个
run_etl.bat 5 10                  :: 跳过 10 个后处理 5 个
run_etl.bat 5 0 1 42              :: --limit 5 --offset 0 --shuffle --seed 42
run_batch.bat 49 10               :: 总数 49，每批 10
run_batch.bat 49 10 0 1 42        :: 同上，带 shuffle
set ETL_INPUT=D:\data\sessions ^& set ETL_OUTPUT=D:\out ^& run_etl.bat 5
```

### 参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--input` | `output/agent_trajectory/` | trajectory JSONL 所在目录（递归扫描 `*.jsonl`） |
| `--output` | `output/` | 产物输出目录 |
| `--limit N` | 全部 | 最多处理 N 个 session |
| `--offset N` | `0` | 跳过前 N 个 session |
| `--shuffle` | 关闭 | 按 `--seed` 打乱文件顺序后截取 |
| `--seed N` | `0` | `--shuffle` 的随机种子 |
| `--no-thinking` | 保留 | 不把 `thinking` 保留为 `reasoning_content` |
| `--no-summary-system` | 保留 | `summary` 非空时不作为 `system` 消息插入 |
| `--drop-empty-assistant` | 保留 | 丢弃 `content` 为空且无 `tool_calls` 的 assistant 消息 |
| `--no-keep-tool-state` | 保留 | 在 error 的 tool result 前不加 `[state]` 标记 |

### 分批处理

```bash
python run_etl.py --limit 5
python run_etl.py --offset 10 --limit 5
python run_etl.py --shuffle --seed 42 --limit 5
python run_batch.py --total 49 --batch 10     # 写入 output/batch_0000/..batch_0004/
```

## 输入格式（trajectory JSONL）

每个 `.jsonl` 文件对应一个 session，每行一个 `TrajectoryEvent`：

```json
{
  "trace_id": "...", "span_id": "...", "parent_span_id": null,
  "event_type": "turn_start|model_request|model_response|tool_call_request|tool_execution|thinking|error|cancel|final_reply",
  "timestamp": "2026-09-05T03:29:21.785695+00:00",
  "session_id": "...", "agent_id": "default", "user_id": "default",
  "channel": "console", "provider_id": "local", "model_name": "...",
  "payload": { ... }, "metadata": { ... }
}
```

事件类型与 payload：

| event_type | payload | 语义 |
|---|---|---|
| `turn_start` | `{input_text, request_agent_id, agent_backend}` | 用户输入 |
| `model_request` | `{messages, tools, tool_choice?, ...}` | 发往 LLM 的请求；`messages` 含完整历史 |
| `model_response` | `{usage, ...}` | LLM 响应统计 |
| `tool_call_request` | `{tool_calls: [{id, type, function: {name, arguments}}]}` | 模型要求调工具 |
| `tool_execution` | `{tool_call_id, tool_name, input, output}` | 工具实际执行结果 |
| `thinking` | `{thinking: str}` | 思考内容 |
| `error` | `{...}` | 阶段异常 |
| `cancel` | `{...}` | 用户取消 |
| `final_reply` | `{content: [Message]}` | 最终回复；`Message.type` ∈ `reasoning`/`message`/`function_call`/... |

## 转换规则（事件重放）

1. `turn_start` → 追加一条 `user` 消息（`input_text`）。
2. `model_request` → 首次提取 `system` message 文本作为 system prompt（复用为 summary，非空时由 `--no-summary-system` 控制是否插入 `system` 消息）；记录 `tools` 定义。
3. `thinking` → 累积 `ThinkingBlock` 到当前 assistant buffer。
4. `tool_call_request` → 累积 `ToolCallBlock` 到当前 assistant buffer。
5. `tool_execution` → 累积 `ToolResultBlock` 到当前 assistant buffer。
6. `final_reply` → 先 flush 当前 assistant buffer，再追加最终 assistant 消息（`reasoning` → `reasoning_content`，`message` → `content`）。
7. `error`/`cancel` → flush 当前 assistant buffer。
8. 同一 assistant 轮次内的 `thinking`/`text` 累积为一条 assistant 消息；`tool_call` 累积为 `tool_calls`；遇到 `tool_result` 先 flush 当前 assistant 消息（若非空），再输出一条 `tool` 消息。
9. `tool_calls` 使用 `call_` 前缀重命名 id，`tool` 消息使用 `tool_call_id` 与之关联。
10. `tool` 消息的 `content` 在 `state` 非 `success` 且保留 `--keep-tool-state` 时，加 `[state]` 前缀。
11. `tools` 优先使用 `model_request.payload.tools`（含完整 description/parameters），否则从 `tool_call` 名称推导。

## 统计字段

`stats.json` / 审计记录中包含的统计指标：

- `session_id`：会话 ID
- `user_turns` / `assistant_turns`：用户/助手轮次
- `tool_calls` / `tool_results`：工具调用次数/工具结果数
- `output_messages`：最终输出消息数
- `has_summary`：是否存在非空 system prompt（复用为 summary）

## 审计（audit/*.json）

每个会话记录包含：

- 原始 `raw_state` 键列表
- `trace_ids` / `event_count` / `event_types`
- `model_name` / `provider_id` / `agent_id`
- 按时间排列的 `messages`
- 每个消息/块/使用/错误信息
- `sft_stats`：与上面相同的统计信息