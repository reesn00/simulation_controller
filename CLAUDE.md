# CLAUDE.md

## 项目概述

本项目是基于 CAMEL-AI 的 Agent 用户模拟端。它把 Persona、Scenario、Task 编译为 `CompiledTask`，以用户身份驱动远端 QwenPaw 执行 Agent，多轮验证结果并生成追问，最终输出可审计 Run 和清洁蒸馏数据。

## 常用命令

```powershell
uv sync --group dev
uv run python -m simulate_serve --validate-config
uv run python -m simulate_serve --check-tools
uv run python -m simulate_serve --readiness
uv run python -m simulate_serve --limit 1 --output-format both
uv run python -m pytest -q
```

远端执行 Agent 的 LLM 功能已可用（2026-09 确认，此前"未启用"记录已失效）。完整链路验证为可执行项：真实模型输出的端到端批次应当实际运行并记录结果，不再标记为待验证；日常回归仍以单元、合约和离线功能测试为默认门禁。

## 当前架构

```text
CLI / Bootstrap
  -> CatalogLoader -> TaskCompiler -> CompiledTask
  -> BatchRunner -> TaskRuntime / RunStateMachine
       -> InteractionActor
       -> AsyncQwenPawExecutor
       -> ValidationPipeline
            -> deterministic validators
            -> ToolRegistry / BrowserEvidenceProvider
            -> local Semantic Judge
       -> JsonRunRepository
```

边界要求：

- Interaction Actor 只负责自然表达，不拥有验证工具、不决定成功。
- TaskRuntime 使用普通 Python 状态机，不让 LLM 控制状态和重试。
- 本地 ValidationPipeline 拥有最终验收权，聚合为 `FAIL > ERROR > INCONCLUSIVE > PASS`。
- ToolRegistry 是工具创建、健康检查、能力选择和关闭的唯一 owner。
- 所有必选 Criterion 必须 PASS 才能成功；工具缺失不能 fail-open。
- 不保存自由文本思维链、Cookie、Authorization Header 或浏览器 Profile。

## 目录索引

| 目录 | 职责 |
|---|---|
| `configuration/` | 严格 Raw Catalog Schema、加载和诊断 |
| `domain/` | Persona、CompiledTask、Run、Validation、Evidence、状态机 |
| `application/` | TaskCompiler、TaskRuntime、BatchRunner、端口 |
| `interaction/` | Prompt、InteractionActor、GuidancePolicy |
| `validation/` | 确定性校验、Claim、Evidence、Semantic Judge、聚合 |
| `tools/` | Registry、health、CAMEL adapter、Playwright/Camoufox |
| `infrastructure/` | 异步 QwenPaw、CAMEL model、v2 Repository/Exporter |
| `tests/` | unit、contract、functional；默认不访问公网 |

## 配置和工具

- Python 配置代码位于 `simulate_serve/config.py` 和 `configuration/`。
- 内置 YAML 只位于 `simulate_serve/config/`，采用文件级 `schema_version: "2"`；v1/v0 仅作为兼容输入。
- 58 个内置 Task 全部关联 11 个对话策略 Scenario；公开 `initial_request` 与本地 `test_fixture` 严格隔离。
- `initial_request` 原样作为首轮远端消息；本地模型不得改写或削弱请求。
- Criterion 的 `remediation` 决定失败责任、自然反馈和是否允许继续引导；只有可重试的 executor-owned FAIL 可以触发追问。
- 追问必须要求远端保留已满足内容并返回包含全部要求的完整修订结果，避免只验最新回复时发生准则振荡。
- Runtime 会识别“此前 PASS、本轮非 PASS”的回退准则，并在追问和 `FOLLOWUP_CREATED` 事件中明确记录。
- Playwright/Camoufox 默认 disabled，启动不自动安装。使用 `--check-tools` 查看完整状态。
- 使用 `--readiness` 在不连接 QwenPaw 的情况下汇总 Judge/Provider 缺口及受影响 Task；该命令不创建 Run 日志。
- 内置并打包的 `config.yaml` 不含实际模型凭据或内部 endpoint；默认从 `OPENAI_API_KEY` / `OPENAI_BASE_URL`（或 Anthropic 对应变量）读取。自定义配置仍可显式填写，但不得提交、打包、复制到测试、文档或日志。

## 输出

v2 输出在 `output/runs|artifacts|datasets|reports`；legacy 兼容投影在 `output/legacy`。审计保存所有 Run，蒸馏只导出干净的 SUCCESS 对话。非终态启动恢复时标记 `INTERRUPTED`，绝不自动重复远端任务。

## 文档

实施基线为 `docs/refactor-implementation-plan.md`，当前实现和验证结果见 `docs/refactor-development-progress.md` 与 `docs/phase6-final-validation-report.md`。
