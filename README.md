# useramulation

基于 CAMEL-AI 的 Agent 用户模拟端。系统读取 Persona、Scenario、Task 和运行配置，以真实用户口吻驱动远端执行 Agent，并由本地验证取证层逐轮判断任务是否完成。

## 项目模块

```
simulate_serve（模拟采集 Run/审计 JSON）
  → orchestration（顶层调度：multiprocessing.Pool 单 task 流水线，2026-09-22 新架构 simulation server → gdr → etl）
       ├── PipelineExecutor.run：multiprocessing.Pool 子进程调度（max_parallelism 槽位填充）
       ├── task_pipeline._run_one_task_pipeline：单 task 三阶段严格串行
       ├── producer_simulate.run_one_task：simulate_serve in-process 入口
       ├── workers.gdr_worker.run_gdr_once：C1 trajectory → C2 refined Session
       └── workers.etl_worker.run_etl_once：C2 refined Session → C3 4 视图
  → data_refiner（规则剪裁合成数据）
  → etl/pawsession（QwenPaw 会话 → OpenAI SFT 格式）
  → etl/qwenformat（trajectory → Session 解析；gdr.parsers 唯一调用入口）
  → scripts/model_train（unsloth LoRA 微调 Qwen3.5-9B + 推理验证）
  → gdr（平行的 LLM 驱动三级精修流水线）
```

- `simulate_serve/`：主应用，六边形/分层架构。`configuration/` 加载严格 Schema v2 Catalog；`domain/` + `application/` 编译任务、维护异步运行状态机、编排远端会话；`interaction/` 生成首轮请求和针对验证缺口的自然追问，不拥有验证工具；`validation/` + `tools/` 负责确定性规则、语义 Judge、工具取证和四态结果聚合；`infrastructure/` 提供 QwenPaw HTTP、CAMEL 模型和 JSON v2 持久化。产出 Run/审计/蒸馏 JSON，是下游数据加工的源头。入口 `python -m simulate_serve`。
- `orchestration/`：顶层流水线调度器（2026-09-22 重写），把 `simulate_serve → gdr → etl` 三个独立子系统串成 `simulation server → gdr → etl` 单 task 三阶段流水线（设计见 [`docs/orchestration-design.md`](docs/orchestration-design.md)，契约见 [`docs/设计方案/pipeline-contracts.md`](docs/设计方案/pipeline-contracts.md)）。`master.py` 只持有配置 / queue / `stop_event`，按 `--parallelism N` 起 `PipelineExecutor` 调 `multiprocessing.Pool` 子进程池；`pipeline_executor.py` 维护 `in_flight: dict[AsyncResult, str]` 槽位填充（详见 [`orchestration/pipeline_executor.py:_dispatch`](orchestration/pipeline_executor.py)），`task_pipeline._run_one_task_pipeline` 是子进程顶层入口（picklable），每个子进程完整跑单个 task 的 simulate → gdr → etl 三阶段；`settings.py` 拆 `PipelineSettings` + `Paths` 两个 frozen dataclass；`queue/sqlite_queue.py` 用单文件 SQLite 提供事务安全的状态机（`pending → simulate → gdr → etl → done`，超限入 `dead`，见 [`docs/设计方案/pipeline-contracts.md` §2](docs/设计方案/pipeline-contracts.md)）；`workers/{base,gdr,etl}_worker.py` 把旧 class 改为模块顶层函数 `run_gdr_once` / `run_etl_once`，分别调 [`gdr/pipeline/runner.py`](gdr/pipeline/runner.py) 的 `_process_one_file`（C1 trajectory → C2 refined Session）和 [`etl/parsers.py::load_refined_session`](etl/parsers/__init__.py) + [`gdr/domain/schema.py::save_session_v2`](gdr/domain/schema.py)（C2 → C3 4 视图）；`producer_simulate.run_one_task` 是 simulate_serve 的 in-process 单 task 入口；`failure_handler.py` 把 `phase=dead` 的 task 产物移到 `output/orchestration/dead/` 并追加 `dead.log`（删 batch_id 字段）；`health.py` 走 `collect_tasks` 直读 SQLite 6 个 phase 计数；`daemon.py` 处理 PID file + STOP 哨兵文件（Windows detach 子进程无控制台，靠哨兵文件兜底）+ 日志重定向；`__main__.py` 提供 `start / status / stop / replay` 四个子命令（`--parallelism N` 控并行度，`--tasks T1,T2` 子集过滤，`--all-tasks` 拉全 catalog，`--dry-run` 只打印计划）；`run.bat` 是 Windows wrapper。所有阶段产物与运行时状态统一收在 `output/` 下。gdr 阶段把精修完的 Session 单文件写到 `output/refined/<TXXX>__<session_id>.json`（C2 契约，`schema_version: refined_session.v1`），etl 阶段沿用同一 stem 拆 4 视图到 `output/refine_data/<TXXX>__<session_id>_refined.{messages,openai,qwenjina.txt,meta}.json`（C3 契约）；低分但结构可用的 session 走 `output/refine_data/judge_low.jsonl` 审核通道（见 [`gdr/pipeline/runner.py:584-610`](gdr/pipeline/runner.py#L584-L610)）；多次失败的死信进 `output/orchestration/dead/`。入口 `python -m orchestration`。
- `data_refiner/`：合成会话数据的轻量规则清洗，只标注不删除。依次执行无效文件判定（R3）、连续工具调用失败段剪裁（R1）、thinking 长度标注（R2），并输出轨迹块状态报告（R5）。入口 `python -m data_refiner --input ... --output ...`。
- `etl/`：SFT 训练格式转换。`pawsession/` 按 extract/transform/load 把 QwenPaw origindata 转为 OpenAI function-calling 格式 `sft_openai.jsonl`，并附每会话审计与 `stats.json`（入口 `etl/pawsession/run_etl.py`）；`qwenformat/` 提供 trajectory 重放（`load.parse_trajectory`，新架构下被 [`gdr.parsers.from_trajectory`](gdr/parsers/__init__.py) 局部导入调用，C1 契约重放唯一入口）+ 训练格式转换（`transform.trajectory_to_session_with_openai_metadata` / `chat_template.jinja`，etl 阶段 C2 → C3 时复用，写 `metadata.openai_messages` / `qf_text` / `qf_rendered_at`）。`parsers/` 包是 C2 契约入口（[`etl/parsers.load_refined_session`](etl/parsers/__init__.py)），`writers/` 包是 C3 写入入口（[`etl.writers.render_to_4_views`](etl/writers/__init__.py)）。
- `gdr/`：独立的 uv workspace 成员（gdr-agent），对 QwenPaw Agent 轨迹做"脏数据入、干净数据出"的自动缺陷检测与精修。Session → Message → Block 三级数据模型，13 种缺陷标签（规则层 + LLM 三票投票），含 obs_denoiser/thought_refactor/tool_fixer 精修器、L1/L2/L3 三级验证、模型路由与评估闭环。入口 `gdr-pipeline`（编排）与 `gdr-evaluator`（评估）。
- `scripts/`：迁移与训练脚本。`migrate_catalog_v2.py` 为 v1 → v2 Task Catalog 的一次性确定性迁移；`model_train/main.py` 用 unsloth + LoRA 在 WSL2 下微调 Qwen3.5-9B（数据指向 `etl/qwenformat` 产物）；`model_train/infer.py` 做训练后推理验证。
- `tool_runtime/`：Node 侧工具运行时，当前仅包含 Playwright MCP（`@playwright/mcp`）依赖，打包时并入 `simulate_serve/tool_runtime/`，默认禁用。
- `tests/`：主应用离线测试套件（pytest-socket 限本机），分 `unit/`、`contract/`、`functional/` 三层；`tests/orchestration/` 覆盖 master / pipeline_executor / task_pipeline / queue / workers / failure_handler / health / CLI 等子模块，含离线 3-task 端到端冒烟与失败注入。
- `docs/`：实施基线、phase0–6 系列报告、Catalog v2 优化说明、QwenPaw HTTP API 定义、`orchestration-design.md`（orchestration 设计基线）等 20 余篇文档。

## orchestration 三阶段流水线

把"模拟采集 → 精修 → 训练视图拆分"做成单 task 三阶段严格串行 + 跨 task 可配置并行度的一条流水线。子命令语义：

| 子命令 | 作用 |
|---|---|
| `start` | 启动 master 跑流水线；`--tasks T1,T2` 子集过滤、`--all-tasks` 拉全 catalog、`--parallelism N` 设子进程并行度（默认 1 严格串行）、`--detach` 后台化、`--dry-run` 只打印计划；task 跑完即退出（默认），`--stay` 常驻 |
| `status` | 读 `output/orchestration/orchestration.db` 队列 6 个 phase 计数 + `output/orchestration/logs/health.json` + 最近 10 个 task 的 `task_id/phase/error_msg` |
| `stop` | 写 STOP 哨兵文件让 master 优雅 shutdown；超时后 `taskkill /F /T`（Windows）或 `SIGKILL`（POSIX）兜底 |
| `replay` | `phase=dead` 的 task 重置回 `pending` 重新入队（无 `--batch` 选项，新架构无 batch 概念） |

进程模型：master 主线程跑一次 `PipelineExecutor.run(task_ids)`，由 [`orchestration/pipeline_executor.py`](orchestration/pipeline_executor.py) 起 `multiprocessing.Pool(processes=max_parallelism)` 并维护 `in_flight` 槽位填充；每个子进程内由 [`orchestration/task_pipeline.py::_run_one_task_pipeline`](orchestration/task_pipeline.py) 完整跑单个 task 的 `simulate → gdr → etl` 三阶段；三阶段顺序由 `_run_one_task_pipeline` 函数体 step 1–10 顺序保证，不依赖外部调度。stop 通道为 SIGINT/SIGTERM/SIGBREAK + STOP 哨兵文件双保险（Windows detach 子进程无控制台，靠哨兵文件兜底）。子进程不响应 stop_event（跑完一个 task 自然退出）；master 主线程 `shutdown()` 仅 set stop_event 提前退出 wait loop。

## 数据格式与三阶段产物（2026-09-22 新架构 `simulation server → gdr → etl`）

| 阶段 | 入口模块 | 产物文件 | 数据形态 |
|---|---|---|---|
| 模拟采集（C1） | `simulate_serve` | `output/agent_trajectory/run_<session>.json` | QwenPaw trajectory JSONL 事件流（独立事件流形态，见 docs/agent-trajectory-format.md） |
| 精修（C2） | `gdr` | `output/refined/<TXXX>__<session_id>.json` | 单 Session JSON，`schema_version: refined_session.v1`，含 `messages[*].blocks` + `metadata.refine_history` |
| 4 视图（C3） | `etl` | `output/refine_data/<TXXX>__<session_id>_refined.{messages,openai,qwenjina.txt,meta}.json` | 训练框架 / audit 用的 4 视图文件 |

GDR 直接消费 trajectory C1 契约（[`gdr/parsers.from_trajectory`](gdr/parsers/__init__.py)），不再走 `etl/qwenformat` 的中间转换；`output/refined/` 是 `gdr → etl` 之间的唯一交接面（C2 契约）。etl 通过 [`etl/parsers.load_refined_session`](etl/parsers/__init__.py) 校验 C2 后调用 [`gdr/domain/schema.py::save_session_v2`](gdr/domain/schema.py) 拆 4 视图（C3 契约）。

### trajectory（独立事件流形态，2026-09-18 确认）

完整格式定义见 [`docs/agent-trajectory-format.md`](docs/agent-trajectory-format.md)。每类事件只有一个职责，冗余源一律跳过：

- `turn_start` → user message（`payload.input_text`）。
- `model_request` → 首个事件提取 system prompt；`payload.tools` 是工具定义权威来源。
- `model_response` → **模型输出主数据源**：`payload.content` 携带 `thinking`（独立结构化块）/ `tool_call`（state=pending，重放归一为 finished）/ `text` 块。
- `tool_execution` → 工具结果唯一事件源（state 在 `metadata.end_state`）。
- `final_reply` → 轮终态：flush 本轮 assistant message，`metadata.usage` 附到该消息；`payload.content` 是冗余快照，跳过。
- `tool_call_request` / `model_request.payload.messages` 快照 → 冗余，重放跳过。

事件序列：`turn_start → model_request → model_response (thinking+tool_call×n) → tool_call_request (冗余) → tool_execution ×n → … → model_response (thinking+text) → final_reply`。多轮会话每轮一对 `turn_start` / `final_reply`。

[`etl/qwenformat/load.py::parse_trajectory`](etl/qwenformat/load.py) 是单路径事件重放，不做旧格式兼容（AI SDK 内嵌快照 / inline ` md` 拆分路径已于 2026-09-18 移除）。新架构下它由 [`gdr/parsers.from_trajectory`](gdr/parsers/__init__.py) 局部导入调用，是 trajectory 重放的唯一调用入口；orchestration 不直接读 trajectory。

### refined（C2 单 Session 文件）

```
Session {
  schema_version: "refined_session.v1",
  session_id, original_session_id, refined_version, run_id, task_id,
  source_file, summary,
  model_name, provider_id, agent_id, trace_ids, event_count, event_types,
  messages: [
    Message {
      role: "system" | "user" | "assistant",
      blocks: [ThinkingBlock | ToolcallBlock | ToolresultBlock | TextBlock],
      metadata: {},
      usage: {...}
    }
  ],
  tools: [...],
  metadata: {
    refine_history: [...],                # 每次精修的 [module, attempts, model_used, result, reason, block_id]
    validation_summary: {...},             # L1/L2/L3 通过块数
    policy_decisions: [...],
    modified_blocks: [...],
    meta_tag_contamination: {...},         # ⟦⟧ 剥离统计（F3-D）
    training_value_score, complexity_tier,
    health_score, intent_achievement,
    ...
  }
}
```

完整字段级 schema 见 [`docs/contracts/C2-refined-session.md`](docs/contracts/C2-refined-session.md)。

### refine_data（C3 4 视图）

GDR 精修（C2）通过后，etl 在尾部做格式整理（`usage_prune` + `transform` + `system_prompt` 切分 + `tool_templates` + `tool_output_summarizer`）并 [`save_session_v2`](gdr/domain/schema.py) 拆 4 视图：

- `<stem>.messages.json` —— 完整 Session（与 C2 同结构，但带 etl 处理的 `metadata.openai_messages` / `qf_text` / `qf_rendered_at`）
- `<stem>.openai.json` —— OpenAI function-calling 形态
- `<stem>.qwenjina.txt` —— Qwen3 chat_template 渲染的训练文本（可选）
- `<stem>.meta.json` —— 元数据 + ⟦⟧ 污染统计

完整字段级 schema 见 [`docs/contracts/C3-final-sft-views.md`](docs/contracts/C3-final-sft-views.md)。

低分但结构可用的 session 走 `output/refine_data/judge_low.jsonl` 审核通道，**数据不丢**；只有三种硬丢弃（只剩 user / assistant 全空壳 / 极少且全失败），见 [`gdr/pipeline/runner.py::_session_structurally_unusable`](gdr/pipeline/runner.py)。

### E2E 验证

`.\run.bat start --tasks T001`（2026-09-22 新架构验证）：

- trajectory：8 events（`turn_start / model_request / model_response / 2 tool_execution / model_request / model_response / final_reply`），最后 `final_reply`。
- refined `messages`：system / user / assistant(thinking+text+2 toolcall+2 toolresult) / assistant(thinking+text)，所有 thinking 块非空。
- refined `metadata.openai_messages.roles`：`['system','user','assistant','tool','tool','assistant']`。
- refined `metadata.tools`：27（含完整 description + parameters）。
- refine_data 4 视图：`<stem>.messages.json` / `<stem>.openai.json` / `<stem>.qwenjina.txt` / `<stem>.meta.json`；`batch_id=32 runs=1 drained=True dead=0`。

## 架构

- `interaction/`：生成首轮请求和针对验证缺口的自然追问，不拥有验证工具。
- `application/` + `domain/`：编译任务、维护异步运行状态机、编排远端会话。
- `validation/` + `tools/`：确定性规则、语义 Judge、工具取证和四态结果聚合。
- `infrastructure/`：QwenPaw HTTP、CAMEL 模型和 JSON v2 持久化。

## 常用命令

```powershell
# 验证 98 个内置任务，不连接模型、远端 Agent 或公网
python -m simulate_serve --validate-config

# 检查全部配置工具并打印 READY/DISABLED/失败原因
python -m simulate_serve --check-tools

# orchestration 顶层流水线 (新架构 simulation server → gdr → etl)
python -m orchestration start --all-tasks --dry-run --parallelism 1   # 打印计划，不真启动
python -m orchestration start --detach --tasks T001,T002,T003         # 后台跑指定 task
python -m orchestration start --all-tasks --parallelism 4             # 整 catalog 4 子进程并行
python -m orchestration start --all-tasks --parallelism 1 --stay      # 单进程串行，跑完常驻
python -m orchestration status                                       # 队列 6 phase 计数 + 最近 task
python -m orchestration stop --timeout 15                            # 优雅停，超时强杀
python -m orchestration replay                                       # 重放全部 phase=dead 的 task
# Windows wrapper 等价于：
scripts\run.bat start --tasks T001,T002

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
- 内置 Catalog 使用 Schema v2：68 个训练任务加 30 个分布外评估任务（E001-E030），共 98 个 Task 全部关联 10 个对话策略 Scenario。
- `test_fixture` 仅用于本地离线用例，不进入远端首轮请求、交互 Prompt 或 Semantic Judge。
- 标记 `offline_only` 的任务默认不进入线上批次（需 `--include-offline`）：fixture 驱动的环境异常任务（T052/T053）。
- 放弃预检（AGENT_DECLINED）在验证未通过之后才执行，且可通过场景 `blocked_action: no_decline_check` 关闭；"拒绝/澄清/诚实降级"即任务目标的场景均已关闭。
- 未达标反馈由 Criterion remediation 生成，只追问远端可以修复的差量缺口。
- Trajectory 归档源路径固定为 `~/.qwenpaw/workspaces/{agent_id}/trajectory/{session_id}.jsonl`；不做 `sessions/console` 目录兜底探测。QwenPaw 须将 trajectory 写入此路径，archiver 才能复制到 `output/agent_trajectory/`。

## orchestration 边界

- `master.py` 仅持有配置 / `SQLiteQueue` / `stop_event`，不直接起 worker 线程；调度全部由 [`PipelineExecutor`](orchestration/pipeline_executor.py) 的 `multiprocessing.Pool` 完成。
- 单 task 三阶段 `simulate → gdr → etl` 在子进程内严格串行（[`task_pipeline._run_one_task_pipeline`](orchestration/task_pipeline.py) 函数体 step 1–10），不依赖外部调度；不同 task 之间可任意阶段重叠，由 `max_parallelism` 槽位控制并发度。
- 子进程内未捕获异常被顶层 try/except 兜底 → `queue.mark_failed(stage=<current_stage>)` + 返回 `{"phase": "dead"}`，**不抛异常给主进程**；主进程通过 `future.get()` 拿到 dict，按 `phase` 计入 `done/dead`。
- 子进程崩溃（pool 进程异常退出）由 `future.get()` 抛 `Exception`，主进程捕获后 `dead++` + 兜底 `mark_failed(stage="simulate")`，**继续下一个**。
- 优雅停止走 SIGINT/SIGTERM/SIGBREAK + STOP 哨兵文件双保险；Windows detach 子进程无控制台、CTRL_BREAK_EVENT 不可达，哨兵文件是唯一可靠通道。
- `phase=dead` 的 task 不自动重跑；用 `python -m orchestration replay` 复活。
- judge 低分不进主输出，但完整精修 session 走 `refine_data/judge_low.jsonl` 审核通道（数据不丢）；真正硬丢弃仅三种（只剩 user / assistant 全空壳 / 极少且全失败），见 [`gdr/pipeline/runner.py::_session_structurally_unusable`](gdr/pipeline/runner.py)。
- orchestration 不改 `simulate_serve` / `gdr` / `etl` 任何代码；只通过 [`gdr/parsers.from_trajectory`](gdr/parsers/__init__.py)、[`gdr/pipeline.runner._process_one_file`](gdr/pipeline/runner.py)、[`etl/parsers.load_refined_session`](etl/parsers/__init__.py)、[`gdr/domain.save_session_v2`](gdr/domain/schema.py) 四个公开入口串联三阶段。

## 旁路模块（不参与 orchestration 主链路）

orchestration 的 `start → PipelineExecutor (multiprocessing.Pool) → task_pipeline._run_one_task_pipeline → producer_simulate.run_one_task + workers.{gdr,etl}_worker` 主链路只调用四个公开入口：`gdr.parsers.from_trajectory`、`gdr.pipeline.runner._process_one_file`、`etl.parsers.load_refined_session`、`gdr.domain.save_session_v2`。以下目录/脚本**不在该主链路**——或平行存在、或一次性、或只服务特定子任务。

### A. 独立垂类工具链（自有入口，不依赖 orchestration）

| 路径 | 职责 | 入口 |
|---|---|---|
| `data_refiner/` | 合成会话数据的轻量规则清洗，只标注不删除：thinking 长度检查、连续失败裁剪、无效文件过滤、轨迹块状态报告；`refiner/` 下含 `runner / trimmer / validity / thinking_check / loader / report` 六个子模块 | `python -m data_refiner` |
| `etl/pawsession/` | **只服务**"PawSession origindata → OpenAI function-calling 格式"的单向 ETL；与 `etl/qwenformat` 平行，**未被 orchestration 引用**（orchestration 直接走 `gdr.parsers.from_trajectory` → trajectory 重放） | `python -m etl.pawsession.run_etl` |
| `scripts/model_train/` | 独立的 unsloth + LoRA 训练/推理脚本（`main.py` 微调 Qwen3.5-9B，`infer.py` 推理验证），不在主 `pyproject.toml` 依赖里，需单独安装 unsloth / trl / datasets | `python scripts/model_train/main.py` |

### B. 一次性工具（脚本级，不再演进）

- `scripts/migrate_catalog_v2.py`：v0/v1 → v2 Task Catalog 的一次性确定性迁移，跑完即可丢弃。

### C. 配套生态（不进入 Python 进程）

- `tool_runtime/playwright/`：Node 子工程，仅含 `package.json` + `package-lock.json`；由 wheel 的 `force-include` 把 `node_modules/` 拷贝到 `simulate_serve/tools/browser/`，运行时按需启用。
- `output/refine_data/judge_low.jsonl`：gdr 终检 judge 低分但结构可用的 session 审核通道快照，**单文件 JSONL 数据**，非代码；详见 [`gdr/pipeline/runner.py:584-610`](gdr/pipeline/runner.py#L584-L610)。

### D. `gdr/` 内部子模块（orchestration 只取 `parsers.from_trajectory` + `pipeline._process_one_file`）

`gdr/` 子目录共 11 类，orchestration 只通过两个接缝调用：trajectory 重放走 `gdr.parsers.from_trajectory`（C1 契约入口），单文件精修走 `gdr.pipeline.runner._process_one_file`（C2 契约入口）；`gdr.domain.save_session_v2` 是 etl 拆 4 视图时复用的写入函数：

- `core/`（context_understanding、policy）、`domain/`（schema，含 `save_refined_session` / `save_session_v2`）、`evaluator/`（cli、dual_eval、probe、report、feedback）、`infrastructure/`（http_embed、llm_client、logging）、`prompts/`（YAML 模板）、`refiners/`（obs_denoiser、thought_refactor、tool_fixer）、`routing/`（router、health）、`validators/`（l1_rules、l2_semantic、l3_judge）、`parsers/`（C1 契约入口）、`data/`（sft_pairs）、`origindata/`（原始数据集）、`docs/`（gdr 设计文档）

主链路外部接口：`gdr-pipeline`（编排入口）/ `gdr-evaluator`（评估入口），orchestration 不依赖这两个 CLI，只复用进程内函数。

> **编排侧依赖清单基于** `orchestration/__main__.py`、`master.py`、`pipeline_executor.py`、`task_pipeline.py`、`producer_simulate.py`、`workers/base_worker.py`、`workers/gdr_worker.py`、`workers/etl_worker.py` 的静态 `import` 扫描结果。

Catalog v2 字段、迁移决策和本地验收矩阵见 `docs/catalog-v2-optimization.md`。orchestration 设计与决策见 `docs/orchestration-design.md`。
