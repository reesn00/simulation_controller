# CLAUDE.md

## 项目概述

本项目是基于 CAMEL-AI 的 Agent 用户模拟端。它把 Persona、Scenario、Task 编译为 `CompiledTask`，以用户身份驱动远端 QwenPaw 执行 Agent，多轮验证结果并生成追问，最终输出可审计 Run 和清洁蒸馏数据。

## 常用命令

```powershell
uv sync --group dev
uv run python -m simulate_serve --validate-config
uv run python -m simulate_serve --check-tools
uv run python -m simulate_serve --readiness
uv run python -m orchestration start --tasks T001,T003 --parallelism 1
uv run python -m orchestration start --all-tasks --parallelism 4 --dry-run
uv run python -m orchestration status
uv run python -m orchestration replay
uv run python -m pytest -q
```

> `python -m simulate_serve --tasks / --rerun-task / --limit / --include-offline` 已于 2026-09-22 删除（任务运行入口移交 `orchestration`）；`simulate_serve` 仅保留只读开关。orchestration 默认 `max_parallelism=1` 严格串行，≥2 启用 `multiprocessing.Pool` 并发。

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
  *例外*:Langfuse 观测副本(2026-09-23 起的可选可观测性,见
  [`docs/observability-langfuse.md`](docs/observability-langfuse.md) 用户视角总览 /
  [`docs/observability-langfuse-plan.md`](docs/observability-langfuse-plan.md) 设计基线)
  按设计上传完整 trajectory / refined Session / 4 视图内容用于对比观察,
  **不入训练集**(独立 Langfuse 项目),不替代 `output/` 制品的脱敏策略。
  完整字段级 schema 与 13 + 1 个白名单字段见基线 §3;启用方式见用户视角 §2。
- `gdr/reassembly/reassembler.py` 工具配对扫描必须**跨 toolcall 连续扫描**——并行调用（call, call, result, result）下"在下一个 toolcall 处截断"会把成功调用误判为失败删除。

## 数据格式约定

QwenPaw trajectory 形态（2026-09-18 起，单路径事件流）：重放唯一入口 `etl/qwenformat/load.py::parse_trajectory`（被 `gdr/parsers.from_trajectory` 薄包装），每轮一个 assistant message（含全部 thinking / tool_call / tool_result / 最终 text）。

事件约定：
- `model_response.payload.content` 携带模型输出块：`thinking`（独立结构化块）/ `tool_call`（state=pending）/ `text`
- `tool_call_request` 独立事件回归但冗余（与 model_response 重复），重放跳过
- `tool_execution` 是工具结果唯一事件源（state 在 `metadata.end_state`）
- `final_reply.payload.content` 是冗余快照，只取 `metadata.usage`

格式演化历史与早期 AI SDK 内嵌快照路径见 `docs/project-notes.md`。

## Pipeline 流程（2026-09-22 起新架构）

`simulation server → gdr → etl`。三阶段各守一道边界，每段交接面写一份契约文件。

```text
┌──────────────────┐         ┌──────────────┐         ┌──────────────────┐
│ simulation       │  C1     │     gdr      │  C2     │       etl        │
│ server           │ ──────► │ (refine)     │ ──────► │ (format convert) │ ─► 训练
└──────────────────┘         └──────────────┘         └──────────────────┘
output/agent_trajectory/      output/refined/          output/refine_data/
```

| 阶段 | 输入 | 输出 | 职责 |
|---|---|---|---|
| **simulation server** | Persona + Scenario + Task | C1 trajectory 事件流 | 驱动远端 Agent，多轮验证 + 追问；落 run 元数据 + 轨迹 |
| **gdr** | C1 trajectory | C2 refined Session（单文件） | 块级精修：硬过滤 + 健康分 + CU + fold + retry_loop_clip + router + policy + refiners + validators + reassemble + meta_tag_strip |
| **etl** | C2 refined Session | C3 4 视图文件 | 格式整理：usage_prune + transform + system_prompt partition + tool_templates + tool_output_summarizer → save_session_v2 拆 4 视图 |

### 关键契约

| 编号 | 路径 | 入口 | 出口 |
|---|---|---|---|
| C1 | `output/agent_trajectory/<run_id>__<session_id>.json` | simulate_serve archiver | `gdr/parsers.from_trajectory` |
| C2 | `output/refined/<TXXX>__<session_id>.json` | `gdr/pipeline/runner.py::_process_one_file` | `etl/parsers.load_refined_session` |
| C3 | `output/refine_data/<TXXX>__<session_id>_refined.{messages,openai,qwenjina.txt,meta}.json` | `etl/writers/render_to_4_views` → `gdr.domain.schema.save_session_v2` | 训练框架 / audit |

完整契约字段级 schema 见 [docs/contracts/](docs/contracts/)。

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
| `etl/qwenformat/` | trajectory 重放 + transform + system_prompt partition + tool_output_summarizer + usage_prune + chat_template |
| `etl/parsers/` | C2 契约入口：`load_refined_session` |
| `etl/writers/` | C3 4 视图写入：`render_to_4_views` → `gdr.domain.schema.save_session_v2` |
| `gdr/parsers/` | C1 契约入口：`from_trajectory` |
| `gdr/domain/` | Session / Message / Block pydantic 类型 + `save_session_v2` / `save_refined_session` |
| `gdr/{refiners,validators,core,reassembly,routing,config,prompts}/` | gdr 内部模块（详见 [docs/设计方案/gdr-plan.md](docs/设计方案/gdr-plan.md)） |
| `orchestration/` | 顶层调度（master / pipeline_executor / task_pipeline / producer / workers / queue / failure_handler；2026-09-22 起删 watcher / batch_tracker / qf_worker） |
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
- 全项目统一配置入口：仓库根 `config/config.yaml`（gitignored，含真实凭据；提交版模板 `config/config.example.yaml`）。四个模块（simulate_serve / orchestration / gdr / etl.qwenformat）的配置收纳于对应 section，`llm:` 共享段提供端点/密钥/模型缺省，支持 `${VAR}` 环境变量占位符。模块级配置文件已删除，根配置缺失直接报错、无兜底。定位可用 `SIMCTL_CONFIG`（gdr 用 `GDR_CONFIG_FILE`）重定向。凭据不得提交、打包、复制到测试、文档或日志。

## 输出

按阶段分目录（新架构，2026-09-22 起）：

- `output/runs|artifacts|reports` —— simulate_serve 自洽（run 元数据 / content-addressed 制品 / 聚合统计）
- `output/agent_trajectory/` —— C1 trajectory 事件流（simulate_serve → gdr 交接面）
- `output/refined/` —— C2 单 refined Session（gdr → etl 交接面）
- `output/refine_data/` —— C3 4 视图文件（etl → 训练 / audit）；旁路 jsonl（incomplete / judge_low / deferred / routing_low）也在此

审计保存所有 Run；非终态启动恢复时标记 `INTERRUPTED`，绝不自动重复远端任务。
`simulate_serve` 不再导出 `output/datasets/all_runs.v2.jsonl` / `distill_dataset.v2.jsonl`
（已被 C3 取代）。

## 文档

- `docs/orchestration-design.md` — orchestration 三阶段流水线设计基线（2026-09-22 重写）
- `docs/refactor-development-progress.md` — gdr SFT 数据质量修复迭代日志（含 2026-09-19 六件套 F1/F2/F3-C + Fix A/B/C + F3-D/E）
- `docs/执行agent资料/`、`docs/任务合集/`、`docs/设计方案/` — 项目历史档案
- 框架与策略长文：`docs/gdr-context-understanding-and-policy.md`、`docs/gdr-module-functional-overview.md`、`docs/gdr-mvp-design.md`、`docs/incremental-state-tracking-plan.md`
