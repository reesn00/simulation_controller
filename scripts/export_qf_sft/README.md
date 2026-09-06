# export_qf_sft — Agent 轨迹转 Qwen3.5 SFT 数据

把 `orchestration/data/qf_out/` 中的 agent 轨迹 JSON 转换为按
`etl/qwenformat/chat_template.jinja`（Qwen3.5 模板）逐字节渲染的 SFT 训练数据，
并提供配套的 Unsloth LoRA 训练脚本。

## 文件

| 文件 | 说明 |
|---|---|
| `export_qf_sft.py` | 轨迹 JSON → SFT 数据导出脚本 |
| `train_unsloth.py` | Unsloth Qwen3.5 LoRA SFT 训练脚本 |
| `output/` | 默认输出目录 |

## 输入格式（qf_out 轨迹 JSON）

```jsonc
{
  "session_id": "...",
  "messages": [
    {
      "role": "system|user|assistant",
      "blocks": [
        {"type": "text", "text": "..."},
        {"type": "toolcall", "id": "...", "name": "browser", "input": "<json str>"},
        {"type": "toolresult", "id": "...", "output_text": "..."}
      ]
    }
  ],
  "metadata": {"tools": [ {"type": "function", "function": {...}}, ... ]}
}
```

转换规则：

- 连续 `toolcall` 聚合为一条带 `tool_calls` 的 assistant 消息（`input` 自动反序列化）
- `toolresult` 转为 `role=tool` 消息（模板渲染为 `<|im_start|>user` 中的 `<tool_response>`）
- `thinking` / `reasoning` block 映射为 `reasoning_content`（渲染为 `<think>...</think>`）
- 工具 schema 取自 `metadata.tools`，渲染进 system 段的 `<tools>` 块

## 导出

```powershell
# 默认: unsloth 格式 + jsonl，输入 orchestration/data/qf_out，输出 output/
python scripts/export_qf_sft/export_qf_sft.py

# 指定参数
python scripts/export_qf_sft/export_qf_sft.py `
    --input orchestration/data/qf_out/T001__xxx.json `
    --out-dir scripts/export_qf_sft/output `
    --mode unsloth --format parquet
```

### 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--input` | `orchestration/data/qf_out` | 轨迹 JSON 文件或目录 |
| `--out-dir` | `output/` | 输出目录 |
| `--mode` | `unsloth` | 见下表 |
| `--format` | `jsonl` | `jsonl` 或 `parquet` |
| `--template` | `etl/qwenformat/chat_template.jinja` | chat template 路径 |
| `--no-tools` | 关 | 不在样本中携带 tools 定义 |
| `--drop-empty-think` | 关 | 移除空 `<think>\n\n</think>` 块（非思考型 SFT） |

### 导出模式

| 模式 | 每条轨迹样本数 | 字段 | 适用场景 |
|---|---|---|---|
| `unsloth`（默认） | 每个助手回复 1 条 | `session_id, turn_index, text` | Unsloth SFTTrainer（单列 text） |
| `per-turn` | 每个助手回复 1 条 | `prompt, completion, messages, tools` | prompt/completion 对（alpaca 风格），或自行拼接 |
| `full` | 1 条 | `messages, tools, text` | 多轮 sharegpt 风格，由框架套模板并 mask assistant |

`unsloth` 模式样本示例（`text` 已按 Qwen3.5 模板渲染）：

```
<|im_start|>system
# Tools
...
<|im_start|>user
找到...<|im_end|>
<|im_start|>assistant
<think>

</think>

<tool_call>
<function=browser>
<parameter=code>
tavily_search("...", country="China", max_results=10)
</parameter>
</function>
</tool_call><|im_end|>
```

## 训练（Unsloth）

```powershell
python scripts/export_qf_sft/train_unsloth.py `
    --data scripts/export_qf_sft/output/*.sft_unsloth.jsonl `
    --model unsloth/Qwen3.5-4B
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--data` | 必填 | unsloth 模式导出的 jsonl/parquet（支持 glob） |
| `--model` | `unsloth/Qwen3.5-4B` | 稠密模型；MoE（35B-A3B 等）加 `--moe` |
| `--full-finetune` | 关 | 全量微调（显存约为 LoRA 的 4 倍） |
| `--max-steps` | 200 | 训练步数 |
| `--batch-size` / `--grad-accum` | 1 / 4 | 等效 batch = 4 |

关键实现（依据 [Unsloth Qwen3.5 官方指南](https://unsloth.ai/docs/models/qwen3.5/fine-tune)）：

- Qwen3.5 需 **transformers v5**（`pip install --upgrade --force-reinstall --no-cache-dir unsloth unsloth_zoo`）
- 稠密模型用 `FastLanguageModel`，MoE 用 `FastModel`（`--moe`）
- **不使用 4bit QLoRA**（官方明确不推荐），用 `load_in_16bit=True`
- `train_on_responses_only(instruction_part="<|im_start|>user\n", response_part="<|im_start|>assistant\n")`
  只对 assistant 回复计损失，训练前会 decode 一条样本自检 mask
- `max_seq_length=8192`（agent 轨迹较长，OOM 降到 4096）
- 结束保存 LoRA 适配器；合并导出 16bit 见脚本内注释

## 显存参考（bf16 LoRA）

0.8B: 3GB • 2B: 5GB • 4B: 10GB • 9B: 22GB • 27B: 56GB • 35B-A3B(MoE): 74GB
