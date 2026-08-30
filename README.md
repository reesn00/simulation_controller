# useramulation

基于 CAMEL-AI 的 Agent 用户模拟端。系统读取 Persona、Scenario、Task 和运行配置，以真实用户口吻驱动远端执行 Agent，并由本地验证取证层逐轮判断任务是否完成。

## 项目模块

```
simulate_serve（模拟采集 Run/审计 JSON）
  → data_refiner（规则剪裁合成数据）
  → etl/pawsession（QwenPaw 会话 → OpenAI SFT 格式）
  → etl/qwenformat（OpenAI SFT → Qwen3 训练 JSONL）
  → scripts/model_train（unsloth LoRA 微调 Qwen3.5-9B + 推理验证）
  → gdr（平行的 LLM 驱动三级精修流水线）
```

- `simulate_serve/`：主应用，六边形/分层架构。`configuration/` 加载严格 Schema v2 Catalog；`domain/` + `application/` 编译任务、维护异步运行状态机、编排远端会话；`interaction/` 生成首轮请求和针对验证缺口的自然追问，不拥有验证工具；`validation/` + `tools/` 负责确定性规则、语义 Judge、工具取证和四态结果聚合；`infrastructure/` 提供 QwenPaw HTTP、CAMEL 模型和 JSON v2 持久化。产出 Run/审计/蒸馏 JSON，是下游数据加工的源头。入口 `python -m simulate_serve`。
- `data_refiner/`：合成会话数据的轻量规则清洗，只标注不删除。依次执行无效文件判定（R3）、连续工具调用失败段剪裁（R1）、thinking 长度标注（R2），并输出轨迹块状态报告（R5）。入口 `python -m data_refiner --input ... --output ...`。
- `etl/`：SFT 训练格式转换，两个子管线。`pawsession/` 按 extract/transform/load 把 QwenPaw origindata 转为 OpenAI function-calling 格式 `sft_openai.jsonl`，并附每会话审计与 `stats.json`；`qwenformat/` 渲染 `chat_template.jinja` 生成 Qwen3 SFT 格式 `sft_qwen3.jsonl`。入口 `etl/pawsession/run_etl.py`。
- `gdr/`：独立的 uv workspace 成员（gdr-agent），对 QwenPaw Agent 轨迹做"脏数据入、干净数据出"的自动缺陷检测与精修。Session → Message → Block 三级数据模型，13 种缺陷标签（规则层 + LLM 三票投票），含 obs_denoiser/thought_refactor/tool_fixer 精修器、L1/L2/L3 三级验证、模型路由与评估闭环。入口 `gdr-pipeline`（编排）与 `gdr-evaluator`（评估）。
- `scripts/`：迁移与训练脚本。`migrate_catalog_v2.py` 为 v1 → v2 Task Catalog 的一次性确定性迁移；`model_train/main.py` 用 unsloth + LoRA 在 WSL2 下微调 Qwen3.5-9B（数据指向 `etl/qwenformat` 产物）；`model_train/infer.py` 做训练后推理验证。
- `tool_runtime/`：Node 侧工具运行时，当前仅包含 Playwright MCP（`@playwright/mcp`）依赖，打包时并入 `simulate_serve/tool_runtime/`，默认禁用。
- `tests/`：主应用离线测试套件（pytest-socket 限本机），分 `unit/`、`contract/`、`functional/` 三层，含离线端到端用例。
- `docs/`：实施基线、phase0–6 系列报告、Catalog v2 优化说明、QwenPaw HTTP API 定义等 20 篇文档。

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

# 运行任务；默认跳过 offline_only 任务（T052/T053/F001），如需包含加 --include-offline
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
- 标记 `offline_only` 的任务默认不进入线上批次（需 `--include-offline`）：fixture 驱动的环境异常任务（T052/T053）与本地无对应取证工具的任务（F001）。
- 放弃预检（AGENT_DECLINED）在验证未通过之后才执行，且可通过场景 `blocked_action: no_decline_check` 关闭；"拒绝/澄清/诚实降级"即任务目标的场景均已关闭。
- 未达标反馈由 Criterion remediation 生成，只追问远端可以修复的差量缺口。

Catalog v2 字段、迁移决策和本地验收矩阵见 `docs/catalog-v2-optimization.md`。
