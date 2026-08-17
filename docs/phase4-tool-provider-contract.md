# Phase 4 Tool Provider Contract

`ToolRegistry` 是 Provider 创建、能力选择、健康状态和关闭的唯一 owner。YAML 只能引用代码中已注册的 `provider_type`，不能指定任意 dotted import。

健康状态：`DISABLED/READY/DEPENDENCY_MISSING/INIT_FAILED/CONNECT_FAILED/SCHEMA_INVALID/PROBE_FAILED/SHUTDOWN_FAILED`。启动会检查完全部 enabled Provider 后打印完整报告；required 非 READY 才阻止运行，optional 非 READY 允许降级。

业务验证只依赖 `BrowserEvidenceProvider.inspect_url()`，不依赖 MCP 原始工具名。Persona Actor 永远不注册验证工具；Semantic Judge 只能获得当前任务所需的只读 FunctionTool。
