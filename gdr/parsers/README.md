# gdr/parsers — C1 契约入口

新架构 `simulation server → gdr → etl` 下，gdr 不再消费 etl 的 qf_out 产物，而是直接读 trajectory 事件流。本目录是 gdr 对 C1 契约（trajectory 事件流）的唯一入口。

## 入口

- `from_trajectory(path: Path) -> Session`：把 trajectory JSONL 解析为 `gdr.domain.Session`
- 详细契约见 [docs/contracts/C1-trajectory-events.md](../../contracts/C1-trajectory-events.md)

## 约定

1. **不绕过**：gdr 内部所有加载 trajectory 的代码必须经过 `from_trajectory`，不得直接 `import etl.qwenformat.load`。
2. **不渲染**：本入口只解析 blocks，不做 qf_text 渲染、system prompt partition、tool template、tool output summarization——这些是 etl 阶段的事。
3. **依赖稳定**：未来若 parser 从 `etl.qwenformat.load` 迁出，只改本文件，其他 gdr 代码不动。