# Agent Trajectory JSONL 格式规范

> 依据 2026-09-18 真实 trajectory（`output/agent_trajectory/1789693315617-n3dhd01.jsonl`，
> qwen3.8-flash / qwenpaw backend）梳理。本文档是 ETL 重放（`etl/qwenformat/load.py`）
> 的唯一格式依据；旧格式（CAMEL 单对象、无 `model_response.content` 的早期事件流、
> thinking 内嵌 ` md` 的 AI SDK 形态）已全部废弃，不做兼容。

## 1. 文件与链路

```text
QwenPaw 远端: ~/.qwenpaw/workspaces/<agent>/trajectory/<session_id>.jsonl
  -> simulate_serve (QwenPawTrajectoryArchiver) 复制为
     output/agent_trajectory/<run_id>__<session_id>.json
  -> orchestration.watcher 登记 -> qf_worker (etl.qwenformat.load 重放)
  -> output/qf_out/<session_id>.json (Session blocks 视图)
  -> gdr refine -> 训练数据
```

- 每行一个事件（JSONL），但 `tool_execution.payload.output` 可含原始换行/引号，
  **不能按行切分**，须用大括号深度计数（`load._iter_json_objects`）。
- 多轮会话（追问复用同一 session）：每轮一对 `turn_start` / `final_reply`，
  事件按发生顺序追加。

## 2. 事件信封

每个事件共享同一信封结构：

| 字段 | 类型 | 说明 |
|---|---|---|
| `trace_id` | str | 一次 run 的追踪 id |
| `span_id` | str | 事件 span |
| `parent_span_id` | str \| null | `model_response` 挂在 `model_request` 下；`tool_call_request` 挂在 `model_response` 下 |
| `event_type` | str | 见 §3 |
| `timestamp` | str | ISO8601 UTC |
| `session_id` | str | 远端会话 id（文件名同源） |
| `agent_id` / `user_id` / `channel` | str | 环境信息 |
| `provider_id` / `model_name` | str | 模型信息 |
| `payload` | object | 按 event_type 不同 |
| `metadata` | object | 附加信息 |

## 3. 事件类型与 payload

### 3.1 `turn_start` — 用户轮开始

```json
{"payload": {"input_text": "<用户输入原文>", "request_agent_id": null,
             "agent_backend": "qwenpaw"}}
```

### 3.2 `model_request` — 发给模型的请求

```json
{"payload": {
  "messages": [Message, ...],   // 完整对话快照, 见 §4
  "tools": [ToolDef, ...]       // OpenAI function 定义, 见 §6
}}
```

`messages` 是**累积快照**：末次 `model_request` 含 system / 全部 user / assistant
（assistant 的 content 内嵌该轮全部 thinking / tool_call / tool_result 块）。

### 3.3 `model_response` — 模型输出（重放的主数据源）

```json
{"payload": {
  "content": [ContentBlock, ...],   // 见 §5
  "usage": {"input_tokens": 13601, "output_tokens": 159, "time": 3.58,
            "cache_creation_input_tokens": 0, "cache_input_tokens": 6656,
            "type": "chat", "metadata": null},
  "finished_reason": "completed"     // 或 "tool_calls"
}}
```

- 工具循环轮：`content` = `[thinking, tool_call, tool_call, ...]`
- 最终轮：`content` = `[thinking, text]`
- `tool_call` 块的 `state` 为 `"pending"`（尚未执行）。

### 3.4 `tool_call_request` — 工具调用请求（冗余）

```json
{"payload": {"tool_calls": [
  {"id": "toolu_...", "type": "function",
   "function": {"name": "web_search", "arguments": "{...}"}}]}}
```

与 `model_response.content` 的 `tool_call` 块同数据、OpenAI function 形态。
**重放跳过**（以 `model_response` 为准）。

### 3.5 `tool_execution` — 工具执行结果

```json
{"payload": {"tool_call_id": "toolu_...", "tool_name": "web_search",
             "input": {"search_term": "..."},   // dict
             "output": "<工具原始输出, str, 可含换行>"},
 "metadata": {"duration_ms": 1668, "end_state": "success",
              "offload_reason": null, "cancel_reason": null}}
```

`end_state`：`"success"` / 失败态（映射到 blocks 的 `state`）。

### 3.6 `final_reply` — 轮终态（重放只取 usage）

```json
{"payload": {"content": [Message, ...]},   // 整轮 assistant 侧快照, 见 §7
 "metadata": {"status": "completed",
              "usage": {"input_tokens": 22225, "output_tokens": 742}}}
```

`payload.content` 是冗余快照（与事件流重放结果一致，已验证
末段 `message` == 末次 `model_response` 的 text 块、末段 `reasoning` == 其
thinking 块）；**重放跳过**，只取 `metadata.usage`。

### 3.7 `error` / `cancel` — 异常终态

轮中断时 flush 已累积内容。archiver 以
`final_reply / error / cancel` 为终态事件判定落盘完成。

## 4. `model_request.payload.messages` — Message 形态

```json
{"role": "system|user|assistant",
 "content": [ContentBlock, ...]}
```

- `system` / `user`：content 为 `[{"type": "text", "text": ...}]`
- `assistant`：content 内嵌该轮全部块（见 §5），一个 user 轮对应一个
  assistant message。

## 5. ContentBlock 类型（`model_response.content` / `model_request.messages[].content`）

| type | 字段 | 说明 |
|---|---|---|
| `text` | `text`, `id`, `created_at`, `finished_at` | 可见文本 |
| `thinking` | `thinking`, `id`, `created_at`, `finished_at` | **结构化思维链**（独立块，非内嵌 ` md`） |
| `tool_call` | `id`, `name`, `input`(JSON 字符串), `state`(`pending`/`finished`), `created_at` | 模型发起的调用 |
| `tool_result` | `id`, `name`, `output`(**ContentBlock 列表**), `state`(`success`/`error`), `metadata` | 仅出现在 `model_request` 快照中；`output` 是 `[{"type": "text", "text": ...}]` 列表而非字符串 |

## 6. ToolDef（`model_request.payload.tools`）

OpenAI function 形态：

```json
{"type": "function",
 "function": {"name": "Skill", "description": "...",
              "parameters": {"type": "object", "properties": {...}, "required": [...]}}}
```

## 7. `final_reply.payload.content` — Message 快照（冗余，仅备查）

| type | role | content | 携带数据 |
|---|---|---|---|
| `reasoning` | assistant | `[{"type": "text", "text": ...}]` | 各轮 thinking |
| `plugin_call` | assistant | `[{"type": "data", "data": {"call_id", "name", "arguments"}}]` | 各工具调用 |
| `plugin_call_output` | tool | `[{"type": "data", "data": {"call_id", "name", "output"}}]` | 各工具结果 |
| `message` | assistant | `[{"type": "text", "text": ...}]` | 最终答复（无内嵌 ` md`） |

## 8. ETL 重放规则（`etl/qwenformat/load.py`）

每类事件只有一个职责，冗余源一律跳过：

| 事件 | 重放行为 |
|---|---|
| `turn_start` | flush assistant buffer；追加 user message（`input_text`） |
| `model_request` | 首个事件提取 system prompt（summary + system message）；最后一个非空 `payload.tools` 胜出 |
| `model_response` | `thinking` → ThinkingBlock；`tool_call` → ToolCallBlock（`state` 归一为 `finished`）；`text` → TextBlock |
| `tool_execution` | ToolResultBlock（`state` 取 `metadata.end_state`） |
| `final_reply` | flush 本轮 assistant message；`metadata.usage` 附到该 message |
| `error` / `cancel` | flush |
| `tool_call_request` | 跳过（与 `model_response` 重复） |

输出 SessionRecord.messages 结构：

```text
[system?, (user, assistant) × 轮数]
```

每个 user 轮对应**一个** assistant message，含该轮全部块（按发生顺序）：
`thinking, tool_call×n, tool_result×n, ..., thinking(最终), text(最终)`。
qf transform 按 `toolresult` 边界自动拆分为 OpenAI 消息序列
（assistant(tool_calls) / tool / ... / assistant(最终)），ChatML 渲染时仅
最后一轮 user 之后的 assistant 消息包裹 ` md`（与 Qwen3 官方模板一致）。

## 9. 与旧格式的差异（废弃项，不兼容）

| 旧格式行为 | 现状 |
|---|---|
| thinking 内嵌在 text 块（` md... md`），ETL 用 `_split_thinking` 拆分 | 独立 `type=thinking` 块，直接读取 |
| `tool_call_request` / `tool_execution` 是工具调用唯一来源（早期事件流） | `model_response.content` 为主数据源；`tool_call_request` 冗余 |
| `model_response.payload` 只有 `usage` | 增加 `content`（thinking / tool_call / text 块）与 `finished_reason` |
| 最终回复取自 `final_reply` 末段 message（含内嵌 ` md`） | 取自末次 `model_response` 的 text 块；`final_reply` 只取 usage |
| `tool_result.output` 为字符串（AI SDK 快照内） | ContentBlock 列表（快照内）；事件流 `tool_execution.output` 仍为字符串 |
| tool_call id 形如 `call_*` | `toolu_*`（OpenAI 导出时 transform 统一加 `call_` 前缀） |
