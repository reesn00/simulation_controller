# useramulation

基于 CAMEL-AI 的 Agent 用户模拟端。系统读取 Persona、Scenario、Task 和运行配置，以真实用户口吻驱动远端执行 Agent，并由本地验证取证层逐轮判断任务是否完成。

## 项目模块

```
simulate_serve（模拟采集 Run/审计 JSON）
  → orchestration（顶层调度：trajectory → qwenformat → gdr 三阶段流水线）
       ├── trajectory  →  watcher → SQLite 队列
       ├── qf_worker   →  etl/qwenformat 扩展（带 OpenAI metadata 的 Session JSON）
       └── gdr_worker  →  gdr 三级精修
  → data_refiner（规则剪裁合成数据）
  → etl/pawsession（QwenPaw 会话 → OpenAI SFT 格式）
  → etl/qwenformat（OpenAI SFT → Qwen3 训练 JSONL）
  → scripts/model_train（unsloth LoRA 微调 Qwen3.5-9B + 推理验证）
  → gdr（平行的 LLM 驱动三级精修流水线）
```

- `simulate_serve/`：主应用，六边形/分层架构。`configuration/` 加载严格 Schema v2 Catalog；`domain/` + `application/` 编译任务、维护异步运行状态机、编排远端会话；`interaction/` 生成首轮请求和针对验证缺口的自然追问，不拥有验证工具；`validation/` + `tools/` 负责确定性规则、语义 Judge、工具取证和四态结果聚合；`infrastructure/` 提供 QwenPaw HTTP、CAMEL 模型和 JSON v2 持久化。产出 Run/审计/蒸馏 JSON，是下游数据加工的源头。入口 `python -m simulate_serve`。
- `orchestration/`：顶层流水线调度器，把 `simulate_serve → etl/qwenformat → gdr` 三个独立子系统串成 `trajectory → qf → gdr` 的批驱动 + 持续消费管道（设计见 [`docs/orchestration-design.md`](docs/orchestration-design.md)）。`master.py` 跑主循环，按批次 spawn `producer_simulate / watcher / qf_workers / gdr_workers`；`queue/sqlite_queue.py` 用单文件 SQLite 提供事务安全的状态机（`pending → qf_processing → pending_gdr → gdr_processing → done`，超限入 `dead`），`reap_stale` 周期回退卡死的 `*_processing` 任务；`workers/base_worker.py` 提供通用 pull-process-mark 循环 + 重试/dead 逻辑，`qf_worker` 调 [`etl/qwenformat/transform.py`](etl/qwenformat/transform.py) 的 `trajectory_to_session_with_openai_metadata`，`gdr_worker` 调 [`gdr/pipeline/runner.py`](gdr/pipeline/runner.py) 的 `_process_one_file`（含凑批等待）；`batch_tracker.py` 等 `run.json.state ∈ TERMINAL_STATES`；`watcher.py` 轮询 `output/agent_trajectory/` 入队；`failure_handler.py` 把 `state=dead` 的 `src + qf_output + gdr_output` 移到 `data/dead/` 并追加 `dead.log`；`health.py` 写 `data/health.json`；`daemon.py` 处理 PID file + signal + STOP 哨兵文件（Windows 上 CTRL_BREAK_EVENT 不可达，靠哨兵文件兜底）+ 日志重定向；`__main__.py` 提供 `start / status / stop / replay` 四个子命令；`run.bat` 是 Windows wrapper。完整跑完后修正完的精修 JSON 落在 `gdr/refine_data/<TXXX>__<session>_refined.json`；低分但结构可用的 session 走 `refine_data/judge_low.jsonl` 审核通道（见 [`gdr/pipeline/runner.py:584-610`](gdr/pipeline/runner.py#L584-L610)）；多次失败的死信进 `orchestration/data/dead/`。入口 `python -m orchestration`。
- `data_refiner/`：合成会话数据的轻量规则清洗，只标注不删除。依次执行无效文件判定（R3）、连续工具调用失败段剪裁（R1）、thinking 长度标注（R2），并输出轨迹块状态报告（R5）。入口 `python -m data_refiner --input ... --output ...`。
- `etl/`：SFT 训练格式转换，两个子管线。`pawsession/` 按 extract/transform/load 把 QwenPaw origindata 转为 OpenAI function-calling 格式 `sft_openai.jsonl`，并附每会话审计与 `stats.json`；`qwenformat/` 渲染 `chat_template.jinja` 生成 Qwen3 SFT 格式 `sft_qwen3.jsonl`。入口 `etl/pawsession/run_etl.py`。
- `gdr/`：独立的 uv workspace 成员（gdr-agent），对 QwenPaw Agent 轨迹做"脏数据入、干净数据出"的自动缺陷检测与精修。Session → Message → Block 三级数据模型，13 种缺陷标签（规则层 + LLM 三票投票），含 obs_denoiser/thought_refactor/tool_fixer 精修器、L1/L2/L3 三级验证、模型路由与评估闭环。入口 `gdr-pipeline`（编排）与 `gdr-evaluator`（评估）。
- `scripts/`：迁移与训练脚本。`migrate_catalog_v2.py` 为 v1 → v2 Task Catalog 的一次性确定性迁移；`model_train/main.py` 用 unsloth + LoRA 在 WSL2 下微调 Qwen3.5-9B（数据指向 `etl/qwenformat` 产物）；`model_train/infer.py` 做训练后推理验证。
- `tool_runtime/`：Node 侧工具运行时，当前仅包含 Playwright MCP（`@playwright/mcp`）依赖，打包时并入 `simulate_serve/tool_runtime/`，默认禁用。
- `tests/`：主应用离线测试套件（pytest-socket 限本机），分 `unit/`、`contract/`、`functional/` 三层；`tests/orchestration/` 覆盖 master / queue / watcher / workers / failure_handler / daemon / CLI 等子模块，含离线 3-task 端到端冒烟与失败注入。
- `docs/`：实施基线、phase0–6 系列报告、Catalog v2 优化说明、QwenPaw HTTP API 定义、`orchestration-design.md`（orchestration 设计基线）等 20 余篇文档。

## orchestration 三阶段流水线

把"模拟采集 → 精修"做成一条 daemon 化的批驱动流水线。子命令语义：

| 子命令 | 作用 |
|---|---|
| `start` | 启动 master + workers；`--detach` 后台化、`--dry-run` 只打印计划、`--tasks T001,T002` 指定批次、`--all-tasks` 加载 catalog 全部 task、`--batch-size N` 覆盖 config、`--exit-when-done` 跑完即退 |
| `status` | 读 `data/orchestration.db` 队列状态 + `data/health.json` + 死信列表 + 阶段时间戳（`sim@/sim!` `qf@/qf!` `gdr@/gdr!`，`@`=开始 `!=`收尾） |
| `stop` | 写 STOP 哨兵文件让 master 优雅 shutdown；超时后 `taskkill /F /T`（Windows）或 `SIGKILL`（POSIX）兜底 |
| `replay` | `state=dead` 的 task 重置回 `pending`；`--batch N` 仅限该批次 |

进程模型：master 主线程跑批循环，qf/gdr/watcher 都是常驻 Thread + 独立 stop_event；`reap_stale` 走独立 Thread 周期跑；stop 通道为 SIGINT/SIGTERM/SIGBREAK + STOP 哨兵文件双保险。空闲退避（`worker_idle_backoff_max_seconds`）让连续空轮指数翻倍、封顶到配置上限，省 CPU/写锁。

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

# orchestration 顶层流水线
python -m orchestration --dry-run start --all-tasks         # 打印计划，不真启动
python -m orchestration start --detach --tasks T001,T002,T003 # 后台跑指定批次
python -m orchestration start --all-tasks --batch-size 5     # 整 catalog 按 5 个/批
python -m orchestration status                              # 队列/进程/dead/阶段时间戳
python -m orchestration stop --timeout 15                   # 优雅停，超时强杀
python -m orchestration replay --batch 7                    # 重放指定批次的 dead
# Windows wrapper 等价于：
orchestration\run.bat start --tasks T001,T002

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

## orchestration 边界

- master 主线程跑批循环；qf/gdr/watcher 是常驻 Thread + 独立 `stop_event`，worker 异常不致死。
- `reap_stale` 周期（默认 60s 一次，5 分钟前的 `*_processing` 视为陈旧）回退卡死锁。
- 优雅停止走 SIGINT/SIGTERM/SIGBREAK + STOP 哨兵文件双保险；Windows detach 子进程无控制台、CTRL_BREAK_EVENT 不可达，哨兵文件是唯一可靠通道。
- 空闲退避（`worker_idle_backoff_max_seconds`）让连续空轮指数翻倍、封顶到配置上限；拉到任务立即复位。生产建议开启，省 CPU/写锁。
- judge 低分不进主输出，但完整精修 session 走 `refine_data/judge_low.jsonl` 审核通道（数据不丢）；真正硬丢弃仅三种（只剩 user / assistant 全空壳 / 极少且全失败），见 [`gdr/pipeline/runner.py:_session_structurally_unusable`](gdr/pipeline/runner.py)。
- orchestration 不改 `simulate_serve` / `gdr` 任何代码；qf 走的是 [`etl/qwenformat/transform.py`](etl/qwenformat/transform.py) 的扩展函数。

## 旁路模块（不参与 orchestration 主链路）

orchestration 的 `start → producer_simulate → qf_worker → gdr_worker → watcher → reap_dead` 主链路只调用四个外部入口：`simulate_serve` 全量、`etl.qwenformat.transform`、`gdr.config.settings`、`gdr.pipeline.runner._process_one_file`。以下目录/脚本**不在该主链路**——或平行存在、或一次性、或只服务特定子任务。

### A. 独立垂类工具链（自有入口，不依赖 orchestration）

| 路径 | 职责 | 入口 |
|---|---|---|
| `data_refiner/` | 合成会话数据的轻量规则清洗，只标注不删除：thinking 长度检查、连续失败裁剪、无效文件过滤、轨迹块状态报告；`refiner/` 下含 `runner / trimmer / validity / thinking_check / loader / report` 六个子模块 | `python -m data_refiner` |
| `etl/pawsession/` | **只服务**"PawSession origindata → OpenAI function-calling 格式"的单向 ETL；与 `etl/qwenformat` 平行，**未被 orchestration 引用**（orchestration 的 qf 阶段走 `qwenformat`） | `python -m etl.pawsession.run_etl` |
| `scripts/model_train/` | 独立的 unsloth + LoRA 训练/推理脚本（`main.py` 微调 Qwen3.5-9B，`infer.py` 推理验证），不在主 `pyproject.toml` 依赖里，需单独安装 unsloth / trl / datasets | `python scripts/model_train/main.py` |

### B. 一次性工具（脚本级，不再演进）

- `scripts/migrate_catalog_v2.py`：v0/v1 → v2 Task Catalog 的一次性确定性迁移，跑完即可丢弃。

### C. 配套生态（不进入 Python 进程）

- `tool_runtime/playwright/`：Node 子工程，仅含 `package.json` + `package-lock.json`；由 wheel 的 `force-include` 把 `node_modules/` 拷贝到 `simulate_serve/tools/browser/`，运行时按需启用。
- `refine_data/judge_low.jsonl`：gdr 终检 judge 低分但结构可用的 session 审核通道快照，**单文件 JSONL 数据**，非代码；详见 [`gdr/pipeline/runner.py:584-610`](gdr/pipeline/runner.py#L584-L610)。

### D. `gdr/` 内部子模块（orchestration 只取 `pipeline._process_one_file`）

`gdr/` 子目录共 11 类，只有 `gdr.pipeline.runner._process_one_file` 被 orchestration 调用；其余子模块互依并汇入同一接缝，不直接对外：

- `core/`（context_understanding、policy）、`domain/`（schema）、`evaluator/`（cli、dual_eval、probe、report、feedback）、`infrastructure/`（http_embed、llm_client、logging）、`prompts/`（YAML 模板）、`refiners/`（obs_denoiser、thought_refactor、tool_fixer）、`routing/`（router、health）、`validators/`（l1_rules、l2_semantic、l3_judge）、`data/`（sft_pairs）、`origindata/`（原始数据集）、`docs/`（gdr 设计文档）

主链路外部接口：`gdr-pipeline`（编排入口）/ `gdr-evaluator`（评估入口），orchestration 不依赖这两个 CLI，只复用 `_process_one_file` 进程内函数。

> **编排侧依赖清单基于** `orchestration/__main__.py`、`master.py`、`producer_simulate.py`、`watcher.py`、`workers/base_worker.py`、`workers/qf_worker.py`、`workers/gdr_worker.py` 的静态 `import` 扫描结果。

Catalog v2 字段、迁移决策和本地验收矩阵见 `docs/catalog-v2-optimization.md`。orchestration 设计与决策见 `docs/orchestration-design.md`。
