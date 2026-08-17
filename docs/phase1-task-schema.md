# Phase 1 Task Catalog Schema

## 当前版本

内置 Catalog 使用 Schema v2：

```yaml
schema_version: "2"
tasks: []
```

Scenario 文件对应使用 `schema_version + scenarios`。Loader 继续接受 v1 envelope 和 legacy v0 顶层列表；Task 与 Scenario 的非零版本必须一致，未知版本直接失败。

## 编译过程

```text
TaskDocument + ScenarioDocument
  -> strict validation + version gates
  -> Global < Scenario < Task merge
  -> intent/output/remediation/reference compilation
  -> optional v1 legacy rule compilation
  -> frozen CompiledTask
```

v2 Task 必须配置 `initial_request` 和 `intent`，禁止 `validation_rules` 与 `expected_reference`。`test_fixture` 可以进入 `CompiledTask` 供离线测试编排，但任何 Actor Prompt 和 Judge Payload 都不得读取它。

`output_contract` 生成稳定的 `contract.<task_id>.*` Criterion：format、fields、min-results 和 urls。数量准则明确使用 `list_items/table_rows/urls`，不再用一个 `min_items` 猜测对象类型。

Acceptance Criteria 默认 `extend`，显式配置 `acceptance_policy.mode=replace` 时才替换 Scenario 标准。每条 Criterion 编译 `RemediationSpec`，包含修复责任、自然话术和是否允许继续引导。

当前内置 Catalog：58 个 Task、11 个 Scenario、0 diagnostics、0 个 legacy 派生 Criterion。
