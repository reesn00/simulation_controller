# Phase 6 Legacy Deprecation

旧 SessionRunner、同步 AgentClient、SemanticValidator、rule_check、DataSink、旧领域模型和 LegacyTaskAdapter 已从生产代码删除。CLI 只使用 CompiledTask、TaskRuntime、ValidationPipeline 和 JsonRunRepository。

LegacyExporter 仅保留一版输出兼容周期。后续消费者迁移到 v2 后可删除 `legacy/` 导出选项，不需要修改领域和运行层。

重复 `simulate_serve/config/config copy.yaml` 已确认无运行引用后删除，且从 wheel 排除规则仍作为防回归保护。
