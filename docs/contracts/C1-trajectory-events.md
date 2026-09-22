# C1 — Trajectory Events

> `simulation server → gdr` 输入契约。
> 详细事件流 / 块格式 / 重放规则见
> [docs/设计方案/agent-trajectory-format.md](../设计方案/agent-trajectory-format.md)；
> 本文件是接口层描述（关注生产 / 消费双方的输入输出约束与边界条件）。

## 1. 生产者

`simulate_serve/infrastructure/trajectory_archiver.py::QwenPawTrajectoryArchiver`

- 源：`~/.qwenpaw/workspaces/<agent_id>/trajectory/<session_id>.jsonl`
- 触发：终态事件（`final_reply` / `error` / `cancel`）到达后复制到本地
- 命名：`sanitize_filename_part(<run_id>) + "__" + sanitize_filename_part(<session_id>) + ".json"`
- 落点：`output/agent_trajectory/`

## 2. 文件形态

| 项 | 约束 |
|---|---|
| 扩展名 | `.json`（内容是 JSONL 事件流，不是单 JSON 对象） |
| 单行 | 一个事件对象，UTF-8，无空行 |
| 多事件切分 | **大括号深度计数**（`tool_execution.payload.output` 可含原始换行 / 引号，不能按行 split） |
| 文件大小 | 单文件通常 100KB–2MB（典型 200–500 个事件） |
| 终止条件 | `final_reply` / `error` / `cancel` 任一为终态事件 |

## 3. 事件信封（每个事件共享）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `trace_id` | str | ✓ | 一次 run 的追踪 id |
| `span_id` | str | ✓ | 事件 span |
| `parent_span_id` | str \| null | ✓ | 父子关系 |
| `event_type` | str | ✓ | 见 §4 |
| `timestamp` | str | ✓ | ISO8601 UTC |
| `session_id` | str | ✓ | 与文件名同源 |
| `agent_id` | str | ✓ | |
| `user_id` | str | ✓ | |
| `channel` | str | ✓ | |
| `provider_id` | str | ✓ | |
| `model_name` | str | ✓ | |
| `payload` | object | ✓ | 按 event_type 不同 |
| `metadata` | object | ✓ | 附加信息 |

## 4. event_type 摘要

| event_type | payload 关键字段 | 重放行为（gdr 端） |
|---|---|---|
| `turn_start` | `input_text` | flush assistant buffer；追加 user message |
| `model_request` | `messages[]` + `tools[]` | 首事件提 system prompt；末次非空 `payload.tools` 胜出 |
| `model_response` | `content[]` + `usage` + `finished_reason` | **主数据源**；按块类型生成 ThinkingBlock / ToolCallBlock / TextBlock |
| `tool_call_request` | `tool_calls[]` | **跳过**（与 `model_response.tool_call` 重复） |
| `tool_execution` | `tool_call_id` + `tool_name` + `input` + `output` + `metadata.end_state` | ToolResultBlock |
| `final_reply` | `content[]` + `metadata.usage` | flush；只取 `metadata.usage` |
| `error` / `cancel` | — | flush |

完整 ContentBlock 类型 / 渲染规则 → 见
[agent-trajectory-format.md §5, §8](../设计方案/agent-trajectory-format.md)。

## 5. 文件名模板

`<run_id>__<session_id>.json`

- `run_id` 与 `session_id` 都经过 `sanitize_filename_part`
  （保留字母数字 + `_-`，其余替换为 `_`）
- 双下划线 `__` 是分隔符，禁止出现在 `run_id` / `session_id` 内部
- 校验失败 → orchestrator 不入队，文件留在 `trajectory_dir` 等人工处置

## 6. 入队规则

`orchestration/watcher.py` 扫描 `output/agent_trajectory/*.json`：

- 文件完整（首字节到终态事件）→ 入队 `pending`，`src_path = 文件路径`
- 文件不完整（仅有 `turn_start`，无终态事件）→ 跳过本轮，下轮再扫
- 解析失败（重放抛 `ValueError` / `JSONDecodeError`）→ 入队后由 worker 走
  `NonRetryableError` → dead；`failure_handler` 把文件移到 `output/orchestration/dead/`

## 7. 与 C2 的关系

C1 的消费方（gdr）通过 `gdr.parsers.from_trajectory(path) -> Session` 接收 C1。
该函数是 `etl.qwenformat.load.load_trajectory` 的薄包装：

```python
# gdr/parsers/__init__.py
from etl.qwenformat.load import load_trajectory
from gdr.domain.schema import Session

def from_trajectory(path: Path) -> Session:
    record = load_trajectory(path)         # 解析事件流 → SessionRecord
    session_dict = record.to_session_dict()  # dataclass → dict
    return Session.model_validate(session_dict)
```

约定：`gdr.parsers` 是 gdr 模块对 C1 的唯一入口；不允许 gdr 内部代码直接
`import etl.qwenformat.load`，必须经过 `gdr.parsers`。这样未来若把 parser
迁出 etl，只需改 `gdr/parsers/__init__.py` 一处。

该函数**只解析 blocks，不做**：
- system prompt 切分（`partition_system_prompt`）—— etl 阶段做
- tool schema 持久化（`save_tool_templates`）—— etl 阶段做
- tool output 摘要（`ToolOutputSummarizer`）—— etl 阶段做
- qf_text / openai_messages 渲染—— etl 阶段做

## 8. 失败模式

| 失败点 | 现象 | 处理 |
|---|---|---|
| archiver 没等到终态 | 文件不完整 | watcher 不入队；下轮再扫；archiver 内置 tail-scan 兜底 |
| JSONL 解析异常（坏行） | `load_trajectory` 抛 `JSONDecodeError` | worker 包 `NonRetryableError` → dead |
| event_type 未知 | `parse_trajectory` 跳过该事件（不抛错） | 记 warning 字段到 `metadata.unrecognized_event_types` |
| 缺失 system prompt | session 无 system 消息 | gdr 正常运行；refine 时 `user_intent.heuristic` 仍能取首条 user |
| 0 个 `model_response` | session 只有 user / 0 assistant | gdr 硬过滤淘汰；不写 refined |

## 9. 不在本契约范围内的内容

- `output/runs/<run_id>/{run.json, events.jsonl, validations.jsonl, evidence.jsonl}`
  —— simulate_serve 内部状态，simulate_serve 自洽使用；orchestration 通过
  `run_id → task_id` SQLite 映射查询，不读取这些文件
- `output/artifacts/*` —— 内容寻址制品，仅 simulate_serve 引用
- `output/datasets/all_runs.v2.jsonl` / `distill_dataset.v2.jsonl` —— simulate_serve
  自导出（已被 C3 取代，新流程中可下线或仅作为 fallback）