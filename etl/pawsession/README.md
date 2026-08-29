# etl/pawsession 使用说明

## 模块概述

将 QwenPaw 会话 JSON（`origindata/` 目录下）批量转换为 OpenAI function-calling 格式的 SFT（监督微调）数据。

转换产物：

| 产物 | 路径 | 说明 |
|---|---|---|
| `sft_openai.jsonl` | `<output>/sft_openai.jsonl` | 每行一条样本（OpenAI messages 格式） |
| `sft_openai.json` | `<output>/sft_openai.json` | 数组形式，便于人工查看 |
| `audit/` | `<output>/audit/<session_id>.json` | 每个会话的审计：简化原始 state + 中间消息 + 统计 |
| `stats.json` | `<output>/stats.json` | 全量汇总统计 |

## 用法

```bash
python run_etl.py --input origindata --output output [选项]
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
| `--input` | `origindata/` | 原始 session JSON 所在目录（递归扫描 `*.json`） |
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

## 输入格式（origindata/*.json）

每个 JSON 文件结构（节选）：

```json
{
  "agent": {
    "state": {
      "session_id": "...",
      "summary": "...",
      "context": [
        { "role": "user"|"assistant", "content": [...], "metadata": {...} }
      ]
    },
    "scroll": {...},
    "mode_state": {...}
  }
}
```

消息块类型：

- `text`：原文文本
- `thinking`：推理内容（转换后放入 `reasoning_content`）
- `tool_call`：函数调用（`id`, `name`, `input` JSON 字符串）
- `tool_result`：工具结果（`id` 与对应 `tool_call.id` 相同，`output` 为文本数组）

## 转换规则

1. 若会话 `summary` 非空且未加 `--no-summary-system`，则在最前面插入一条 `system` 消息。
2. 同一 assistant 轮次内的 `thinking`/`text` 会累积为一条 assistant 消息；`tool_call` 累积为 `tool_calls`；遇到 `tool_result` 先 flush 当前 assistant 消息（若非空），再输出一条 `tool` 消息。
3. 轮次末尾若残留内容，flush 为最终 assistant 回复。
4. `tool_calls` 使用 `call_` 前缀重命名 id，`tool` 消息使用 `tool_call_id` 与之关联。
5. `tool` 消息的 `content` 在 `state` 非 `success` 且保留 `--keep-tool-state` 时，加 `[state] ` 前缀，例如：`[error] 出错了`。
6. 工具定义从原始消息中出现的 `tool_call` 名称推导（`tools` 汇总）。

## 统计字段

`stats.json` / 审计记录中包含的统计指标：

- `session_id`：会话 ID
- `user_turns` / `assistant_turns`：用户/助手轮次
- `tool_calls` / `tool_results`：工具调用次数/工具结果数
- `output_messages`：最终输出消息数
- `has_summary`：是否存在非空 summary

## 审计（audit/*.json）

每个会话记录包含：

- 原始 `raw_state` 键列表
- `reply_context` 和 `permission_context`
- `tool_context.activated_groups`
- 按时间排列的 `messages`
- 每个消息/块/使用/错误信息
- `sft_stats`：与上面相同的统计信息