# useramulation

基于 CAMEL-AI 的 Agent 用户模拟端。系统读取 Persona、Scenario、Task 和运行配置，以真实用户口吻驱动远端执行 Agent，并由本地验证取证层逐轮判断任务是否完成。

## 架构

- `interaction/`：生成首轮请求和针对验证缺口的自然追问，不拥有验证工具。
- `application/` + `domain/`：编译任务、维护异步运行状态机、编排远端会话。
- `validation/` + `tools/`：确定性规则、语义 Judge、工具取证和四态结果聚合。
- `infrastructure/`：QwenPaw HTTP、CAMEL 模型和 JSON v2 持久化。

## 常用命令

```powershell
# 验证 58 个内置任务，不连接模型、远端 Agent 或公网
python -m simulate_serve --validate-config

# 检查全部配置工具并打印 READY/DISABLED/失败原因
python -m simulate_serve --check-tools

# 运行任务；当前远端 Agent 未启动时请使用测试门禁
python -m simulate_serve --limit 1 --output-format both

# 离线测试
python -m pytest -q
```

Playwright 和 Camoufox 默认禁用，不会在应用启动时自动安装或下载。安装方式见 `docs/phase4-playwright-setup.md` 和 `docs/phase5-camoufox-setup.md`。

## 关键保证

- 本地模拟端拥有最终验收权；远端 Validation Agent 不能直接判成功。
- 必选准则只有全部 `PASS` 才能成功；工具缺失为 `INCONCLUSIVE`，异常为 `ERROR`。
- POST 结果不明且远端没有幂等键时不会自动重复提交。
- 不保存自由文本思维链、Cookie、Authorization Header 或浏览器 Profile。
- 审计数据保存所有 Run；蒸馏数据只导出干净的成功对话。
- 内置 Catalog 使用 Schema v2：58 个 Task 全部关联 11 个对话策略 Scenario。
- `test_fixture` 仅用于本地离线用例，不进入远端首轮请求、交互 Prompt 或 Semantic Judge。
- 未达标反馈由 Criterion remediation 生成，只追问远端可以修复的差量缺口。

Catalog v2 字段、迁移决策和本地验收矩阵见 `docs/catalog-v2-optimization.md`。
