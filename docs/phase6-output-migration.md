# Phase 6 输出迁移

`--output-format v2|both|legacy` 控制导出，迁移期默认 `both`。

- `all_runs.v2.jsonl`：包含全部成功、失败、不确定、错误、取消和中断 Run，用于审计。
- `distill_dataset.v2.jsonl`：只包含 `SUCCESS` 且通过内容清洁检查的 user/assistant 对话。
- `legacy/`：单向兼容投影，不反向写回 v2 领域模型。

自由文本思维链、Cookie、Authorization Header、完整内部 Header 和浏览器 Profile 不进入任何输出；legacy `internal_thoughts` 恒为空。
