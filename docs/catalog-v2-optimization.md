# Task / Scenario Catalog v2 优化实施记录

## 目标与边界

- 将 Catalog 从“提示词和关键词集合”升级为可编译的任务意图、输出契约、验收证据和修复反馈契约。
- 远端执行 Agent 当前未启动；本次不连接远端、不访问公网，使用单元、契约和离线 Scripted/Fake 用例验收。
- Catalog v1 和 legacy v0 继续可读取；内置 Catalog 全量迁移为 v2。

## 关键决策

| 决策 | 结果 |
|---|---|
| Scenario 分类原则 | 按对话策略和状态模式分类，不按影视/文件等业务名简单复用。 |
| 公开请求 | `initial_request` 是唯一首轮用户请求；必须为自然第一人称表达，并由 Runtime 原样发送，不交给本地模型改写。 |
| 隐藏条件 | `test_fixture` 只用于本地测试，不进入 Actor Prompt、Semantic Judge 或远端请求。 |
| 任务理解 | `intent.goal/context/priorities/assumptions/uncertainties` 编译为 `TaskIntent`，Actor 可读取但不机械复述。 |
| 输出要求 | `output_contract` 编译为 format/fields/count/url_syntax 确定性准则。 |
| 数量语义 | 使用 `list_items/table_rows/urls` 明确计数对象，避免 card 和列表计数混淆。 |
| 反馈闭环 | Criterion 使用 `remediation.owner/guidance/retryable` 决定是否追问以及如何自然表达；追问要求返回保留既有成果的完整修订结果。 |
| 责任边界 | 只有 `owner=executor`、`verdict=FAIL` 且 `retryable=true` 的失败可引导远端修复；环境、模拟端、ERROR 和 INCONCLUSIVE 不归咎远端。 |
| 事实时效 | `reference.as_of` 和 `forbidden_assumptions` 约束易变化的权利、平台、版本和地区事实。 |
| 媒体取证 | “可播放”必须使用 browser evidence；工具不可用保持 `INCONCLUSIVE`。 |

## v2 文件契约

```yaml
schema_version: "2"
tasks:
  - task_id: T001
    scenario: media_lookup_standard
    initial_request: 我想找到……
    intent:
      goal: 我想取得的最终结果
      priorities:
        - priority: required
          requirement: 必选要求
    test_fixture:                 # 可选；永不发送给远端
      kind: scripted_executor
      description: 本地异常条件
      payload: {}
    output_contract:
      format: table
      required_fields: [平台, 网址]
      min_results: 2
      count_unit: table_rows
      min_urls: 2
    acceptance_criteria:
      - criterion_id: task.result-verified
        item: 结果经过验证
        validator: semantic
        remediation:
          owner: executor
          guidance: 请补充每个候选的验证结论
          retryable: true
    fallback_plan:
      - trigger: 无法完全满足
        outcome: partial_success
        guidance: 如实说明并提供合法替代
    reference:
      as_of: "2026-08-16"
      evaluation_notes: []
      forbidden_assumptions: []
```

## 内置 Catalog 结果

- 58 个 Task 全部迁移到 Schema v2。
- 11 个 Scenario：标准媒体检索、聚合对比、验证清洗、歧义澄清、事实纠错、约束冲突、部分成功、工具恢复、合规边界、权利/使用场景和文件操作。
- 58/58 Task 均关联 Scenario。
- 内置 Task 不再包含 `validation_rules`、`expected_reference`、`min_chars: 1` 或公开请求中的“注：测试条件”。
- 10 个异常条件迁移到 `test_fixture`。
- 所有 executor-owned Criterion 都有自然修复话术。
- 首轮严格复用公开 `initial_request`；追问触发的 reason code、Criterion ID 和 ValidationReport ID 写入状态事件。
- 追问只选择可重试的 executor-owned FAIL，并要求远端返回完整修订结果，避免增量回答令上一轮 PASS 重新丢失。
- 若某 Criterion 此前已 PASS、当前完整回复却再次非 PASS，Runtime 将其标记为 regressed，并要求远端合并前后结果；回退 ID 写入追问事件供审计。
- 媒体可播放准则从少量任务扩展为 Scenario 公共 browser evidence；远端自述不能作为成功证据。

## 本地验收矩阵

| 门禁 | 目的 |
|---|---|
| Catalog v2 schema tests | 检查必需意图、公开请求、禁用 legacy 字段和版本一致性。 |
| Compiler tests | 检查 output contract、稳定 Criterion、来源和 fixture 编译隔离。 |
| Prompt tests | 检查 fixture 描述和 payload key 不进入系统提示词。 |
| Guidance tests | 检查 Criterion remediation、Scenario reason policy、gap limit、可重试 FAIL 责任过滤和完整修订约束。 |
| Validation tests | 检查语义失败的重试权由 Criterion 契约控制。 |
| Offline functional tests | Scripted Executor 覆盖首轮失败、自然追问、增量回复导致准则回退、合并完整修订后成功；不访问远端。 |
| Readiness tests | 静态判断本地 Judge 配置和 READY Provider，按 capability 汇总无法达到 PASS 的任务。 |
| Existing tool contract tests | Fake Browser Provider 检查页面、媒体、播放进度、数量和门槛语义。 |

一次性迁移逻辑保留在 `scripts/migrate_catalog_v2.py`，脚本不在应用启动路径中，不会修改运行环境或自动访问外部服务。
