# Agent 用户模拟端重构实施记录

## Source Inputs

- 已批准方案：`docs/refactor-implementation-plan.md` v2.0
- 现有架构说明：`CLAUDE.md`、`docs/flow-architecture.md`
- 远端契约：`docs/qwenpaw-backend-api.md`
- 实施范围：Phase 0–6 代码、测试和配套文档

## Preflight

| 项目 | 结论 | 处理 |
|---|---|---|
| 需求状态 | GO | 用户已明确批准 v2.0 方案并授权实施全部阶段。 |
| 仓库规则 | GO | 已读取根 `CLAUDE.md`，未发现 `AGENTS.md`。 |
| 版本控制 | RISK | 当前目录无 Git 元数据，无法使用 status/diff/commit 作为回滚保障；实施期间严格限制修改范围并记录验证。 |
| 远端 Agent | NON-BLOCKING | HTTP 可用、LLM 未启用；所有必达门禁仍使用单元、合约和离线功能测试。 |
| 凭据策略 | ACCEPTED + HARDENED | 保留自定义配置显式填写能力；内置配置和 Wheel 不携带实际凭据或内部 endpoint，推荐使用环境变量。 |

## Phase Gates

| Phase | 状态 | 核心产物 | 门禁结果 |
|---|---|---|---|
| 0 | COMPLETE | 测试基线、CLI/打包入口 | pytest 与 wheel 门禁通过 |
| 1 | COMPLETE + V2 | Catalog Schema、TaskCompiler、CompiledTask | 58 Task / 11 Scenario，0 diagnostics，内置 Catalog 无 legacy rule |
| 2 | COMPLETE | 异步 Executor、Interaction Actor、TaskRuntime、状态机 | 状态、轮次、会话和 POST 幂等边界测试通过 |
| 3 | COMPLETE | ValidationPipeline、Validator、Semantic Judge | 四态聚合和 fail-closed 测试通过 |
| 4 | COMPLETE | ToolRegistry、Health Check、Playwright MCP | 离线 Registry/Fake Provider 通过，npm lock 已生成 |
| 5 | COMPLETE | Camoufox、Provider Selector、统一浏览器协议 | optional lock、回退和安全边界测试通过 |
| 6 | COMPLETE | RunRepository v2、导出、CLI、兼容清理 | 持久化/导出/中断测试通过，旧主链路已删除 |

## Implementation Rules

- 每阶段先定契约与测试，再实现最小可验证变更。
- 阶段门禁失败时不进入下一阶段。
- 不依赖真实远端 Agent、公网或本地已安装浏览器完成默认测试。Windows asyncio 建立事件循环需要 loopback socketpair，pytest 仅允许 `127.0.0.1/::1`，仍禁止公网连接。
- 外部依赖的安装/下载使用显式 setup，普通启动不自动修改环境。
- 不保存自由文本思维链、Cookie、Authorization Header 或浏览器 Profile。

## Verification Log

| 日期 | Phase | 命令/检查 | 结果 |
|---|---|---|---|
| 2026-08-15 | Preflight | 项目规则、源码、方案与远端契约核对 | GO；记录无 Git 元数据风险 |
| 2026-08-15 | 0 | 首轮 pytest | 20 passed，24 errors；错误均为 Windows asyncio 与全禁 socket 冲突、系统 Temp 目录无写权；改用 loopback-only 和工作区 basetemp 后重跑 |
| 2026-08-15 | 0–6 + 审查纠正 | `python -m pytest -q` | 69 passed |
| 2026-08-15 | 0–6 + 审查纠正 | `python -m pytest --cov=simulate_serve` | 69 passed，总覆盖率 83% |
| 2026-08-15 | 1 | `python -m simulate_serve --validate-config` | 58 tasks / 2 scenarios / 0 diagnostics |
| 2026-08-15 | 4–5 | `python -m simulate_serve --check-tools` | Playwright/Camoufox 均按默认配置明确报告 DISABLED |
| 2026-08-15 | 0/6 | `python -m build --wheel --no-isolation` + wheel 内容检查 | 构建成功，三份正式 YAML 存在，`config copy.yaml` 不存在 |
| 2026-08-15 | 0/4 | Wheel 独立 target 安装 + `--validate-config` | 通过；包内 Playwright package/lock 可定位 |
| 2026-08-15 | 4 | `npm install --package-lock-only --ignore-scripts` | 锁定 `@playwright/mcp 0.0.78`，未下载浏览器 |
| 2026-08-15 | 3–5 | 浏览器取证语义测试 | 已区分工具错误、访问门槛、媒体缺失、播放未确认和置信度不足；不得以“页面可访问”替代“媒体可播放” |
| 2026-08-15 | 2 / 远端契约 | 真实 HTTP 单次提交 + 轮询、QwenPaw 源码核对 | `/api/console/chat/task` submit/poll 已确认；LLM 未启用时先保持 `running`，约 80 秒后按 `finished -> result.failed` 返回 `MODEL_EXECUTION_ERROR` / HTTP 502；完整 LLM 链路未尝试 |
| 2026-08-15 | 2 / 会话隔离 | QwenPaw 缺省会话语义核对 + contract tests | 修复 `open_session()` 省略 ID 导致复用远端 `default` 会话；显式生成 UUID，7 个 Executor 合约测试通过 |
| 2026-08-16 | Catalog v2 | `python -m pytest -q --cov=simulate_serve` | 78 passed，总覆盖率 84%；全程未连接远端 Agent 或公网 |
| 2026-08-16 | Catalog v2 | `python -m simulate_serve --validate-config` | 58 tasks / 11 scenarios / 0 diagnostics |
| 2026-08-16 | Catalog v2 | 内容、防泄漏和迁移幂等门禁 | 0 legacy 字段、0 fixture 泄漏、58/58 第一人称请求；迁移脚本 SHA256 前后一致 |
| 2026-08-16 | Catalog v2 | `python -m compileall -q simulate_serve tests scripts` | 通过 |
| 2026-08-16 | Catalog v2 | `uv --cache-dir .pytest-tmp/uv-cache-catalog lock --check` | 104 packages 解析通过，无依赖变更 |
| 2026-08-16 | Catalog v2 | Wheel 构建、临时 target 安装、包内 `--validate-config` | 通过；安装包内仍为 58 tasks / 11 scenarios / 0 diagnostics |
| 2026-08-16 | 反馈闭环优化 | 首轮保真、可重试 FAIL 筛选、完整修订、事件审计定向测试 | 22 passed；不连接远端 Agent |
| 2026-08-16 | 反馈闭环优化 | `python -m pytest -q --cov=simulate_serve` | 85 passed，总覆盖率 84% |
| 2026-08-16 | 反馈闭环优化 | `--validate-config` / `--check-tools` / `compileall` | 58 tasks / 11 scenarios / 0 diagnostics；浏览器 Provider 明确 DISABLED；编译通过 |
| 2026-08-16 | 配置与打包加固 | Wheel 重建、包内配置检查、独立 target 安装 | 构建与安装通过；包内模型凭据和内部 endpoint 均为空 |
| 2026-08-16 | 跨轮回退优化 | Scripted Executor：A 通过/B 缺失 → 仅补 B → 识别 A 回退 → 合并 A+B | 最终 SUCCESS；回退 Criterion 写入 `FOLLOWUP_CREATED.detail` |
| 2026-08-16 | Validation readiness | `python -m simulate_serve --readiness` | 只读通过；当前报告 58 个任务缺 semantic Judge、22 个任务缺浏览器能力、F001 缺 filesystem.inspect；未连接 QwenPaw |
| 2026-08-16 | 跨轮回退 + readiness | `python -m pytest -q --cov=simulate_serve` | 89 passed，总覆盖率 85% |
| 2026-08-17 | 真实远端多轮 | T006 首轮 + 3 次追问、同一 session 续跑 | 4 个 remote task 全部返回；Run `run_898d7b8e056541fba7dbb2dd3a7d8e16` 因无 Judge 最终 INCONCLUSIVE，验证远端 submit/poll/continue 与审计持久化 |
| 2026-08-17 | MiniMax Judge 兼容 | 最小真实探针 + 真实长回复重放 | 发现并修复 response_format 未传递、parsed 未消费、timeout 被重复放大；长回复语义结果为 PASS / INCONCLUSIVE / PASS |
| 2026-08-17 | 远端异常终态 | 修复后 T006 完整 Run | 远端返回 `Task cancelled`；Run `run_53ff5891ea8b46dc867abc537ede17ac` 正确记录 EXECUTOR_ERROR、poll stage 及远端标识 |
| 2026-08-17 | 最终回归 | `python -m pytest -q` / `compileall` / readiness / 凭据残留检查 | 90 passed；编译通过；内置模型凭据与内部 endpoint 恢复为空；源码与文档无残留 |
| 2026-08-17 | SCA 增量能力 P0/P1 | 响应清洁、分阶段验证、运行熔断、结构化决策、L2-L4 引导 | 98 passed，覆盖率 85%；58 tasks / 11 scenarios / 0 diagnostics；未新增依赖或持久化 Schema |

## SCA 增量能力路线

- 参考 SCA 方案已定位为增量能力路线图，不替换现有三层主链路。
- QwenPaw 响应优先消费可见 text block，结构化 reasoning/tool block 不进入验证文本。
- 无法可靠分离但带内部推理信号的 Run 保留在审计数据中，不进入默认蒸馏集。
- ValidationPipeline 在必选确定性准则失败时延后 Provider/Judge，避免无效外部调用。
- TaskRuntime 增加重复响应、重复失败和可选时间预算熔断，复用 GUIDE_EXHAUSTED 终态并记录独立 failure code。
- PASS、重试、错误、不确定和熔断选择使用结构化事件字段记录；不保存自由文本思维链。
- 引导等级按追问轮次从 L2 升至 L3/L4，并写入 FOLLOWUP_CREATED 事件。
- LangGraph、Zvec、Docker 沙箱、Token 预算和动态修改 Criterion 继续延后；触发条件见 docs/sca-incremental-capability-roadmap.md。

## Catalog v2 后续增强

- 新增结构化 `intent`、`output_contract`、`test_fixture`、`fallback_plan` 和 `reference`。
- Criterion 新增 `remediation.owner/guidance/retryable`，Semantic FAIL 的重试权由任务契约决定。
- Scenario 从 2 个业务场景扩展为 11 个对话策略场景。
- 异常任务的环境前置条件已从公开请求迁移到 10 个本地 fixture。
- 旧式 keyword/format/count/semantic `validation_rules` 已从 58 个内置任务完全移除；v1 外部输入仍兼容。
- 详细字段和验收矩阵见 `docs/catalog-v2-optimization.md`。
