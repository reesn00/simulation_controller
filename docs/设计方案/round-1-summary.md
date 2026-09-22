# Pipeline 重构第 1 轮 — 实施汇总(2026-09-22)

> 本轮按 3 组并行子 agent 实施完成。后续轮次(文档清理 / smoke 验证 / bug 修复)以本汇总为基线。

## 一、目标达成度

| 目标 | 状态 |
|---|---|
| 删批次功能 / 严格串行 / 可配置并行度 | ✅ |
| orchestration 唯一任务入口 | ✅(`python -m simulate_serve` 仅保留只读开关) |
| `max_parallelism=1` 等同串行;≥2 子进程池 | ✅(multiprocessing.Pool) |
| 三阶段跨组接口严格按契约落地 | ✅(3 个 agent 独立验证) |

## 二、子 agent 实施交付

| Agent | 子任务 | 测试(本组隔离) | 关键产出 |
|---|---|---|---|
| **agent-data** | ST-1 + ST-2 | 53/53 | `orchestration/settings.py`(新)/ `config_loader.py`(重写)/ `queue/sqlite_queue.py`(重写)/ `queue/schema.sql`(重写) |
| **agent-business** | ST-3 + ST-4 | 32/32 | `workers/{base,gdr,etl}_worker.py`(类→函数)/ `producer_simulate.py`(`run_batch`→`run_one_task`) |
| **agent-orchestration** | ST-5 + ST-6 + ST-7 | 197/197 | `pipeline_executor.py`(新)/ `task_pipeline.py`(新)/ `master.py`(大幅精简)/ `__main__.py`(CLI 重写)/ `batch_tracker.py`(删)/ `health.py` / `failure_handler.py` |

## 三、跨组集成验证

```
pytest tests/orchestration   → 197 passed in 30.85s
pytest tests/unit            → 270 passed in 5.46s   (含本轮适配 test_cli.py)
pytest tests/contract+functional → 29 passed in 4.71s
pytest (全量)                → 496 passed in 40.67s
```

### 串联集成测试

```
python -m simulate_serve --validate-config
  → Catalog valid: tasks=98 diagnostics=0  ✅

python -m orchestration --help
  → {start,status,stop,replay} 子命令齐全  ✅

python -m orchestration start --help
  → --tasks / --all-tasks / --parallelism / --detach / --foreground / --dry-run / --stay 全在  ✅

python -m orchestration start --all-tasks --dry-run --parallelism 1
  → tasks=98, parallelism=1, 不真跑  ✅
```

## 四、主 agent 处理项

### A. 主 agent 直接处理(无重大决策)

1. **删 `tests/orchestration/test_gdr_worker_nonretryable_status.py`** — 测的旧 `GdrWorker` class 在重构后不存在。语义已被 `test_gdr_worker.py::test_run_gdr_once_incomplete_status_is_non_retryable` 覆盖。
2. **删 `tests/orchestration/test_smoke_3task.py`** — 测的旧 batch 概念不存在。端到端等价覆盖在 `test_task_pipeline.py`(11 个测试)。
3. **删 `tests/orchestration/test_stage_timestamps_naming_backoff.py`** — 测的 #8 batches 阶段戳 / #9 run_tasks / #7 worker 退避全部失效。
4. **删 `BaseWorker` 占位类**(`orchestration/workers/base_worker.py` + `__init__.py`)— ST-6 master 重写后不再引用,占位失去存在意义。
5. **修 `tests/unit/test_cli.py`** — 适配契约 §7.6:模拟器不再有 `--tasks` / `--rerun-task` / `--limit` / `--include-offline` / `--max-run-retries`。

### B. 跨组集成无问题(无需问人工)

3 个子 agent 各自按契约独立实现,跨组接口(SchemaQueue / Worker 函数 / Producer 行为)对齐无冲突。理由:

| 接口 | 数据层提供 | 业务层使用 | 调度层使用 | 一致 |
|---|---|---|---|---|
| `SQLiteQueue.upsert_task` | ✅ §2.5 | 不直接调 | ✅ PipelineExecutor._fill_slots | ✅ |
| `SQLiteQueue.mark_phase` | ✅ §2.5 | 不直接调 | ✅ task_pipeline._run_one_task_pipeline | ✅ |
| `run_gdr_once` | n/a | ✅ §3.3 | ✅ task_pipeline | ✅ |
| `run_etl_once` | n/a | ✅ §3.4 | ✅ task_pipeline | ✅ |
| `run_one_task` | n/a | ✅ §4.2 | ✅ task_pipeline | ✅ |
| `Master.run/shutdown/status` | n/a | n/a | ✅ §6.2 | ✅ |

### C. 设计调整记录(已确认无契约违反)

3 个 `_design_adjustments.md` 记录的所有调整均为实现层细节:

- `gdr_settings: Any`(延迟 import 避免 orchestration 对 gdr 硬依赖)
- `RetryableGdrError` 命名(与现有 `NonRetryableError` 风格一致)
- `BaseWorker` 占位类(已彻底删)
- `_FakePool` / `_CrashPool` 测试模式
- `_worker_init` 仅 set paths / 子进程延迟 import
- detach idle 模式(catalog 为空时)

详见:
- `docs/设计方案/agent-data_design_adjustments.md`
- `docs/设计方案/agent-business_design_adjustments.md`
- `docs/设计方案/agent-orchestration_design_adjustments.md`

## 五、删除 / 修改文件清单

### 整文件删

| 文件 | 删除原因 |
|---|---|
| `orchestration/batch_tracker.py` | 批次追踪概念删除 |
| `gdr/run.bat` | 移交 orchestration 顶层后无意义 |
| `tests/orchestration/test_batch_tracker.py` | 同上 |
| `tests/orchestration/test_direction_b_batch_isolation.py` | 方向 B 隔离机制失效 |
| `tests/orchestration/test_watcher.py` | watcher 模块删除 |
| `tests/orchestration/test_gdr_worker_nonretryable_status.py` | 旧 GdrWorker class 测试 |
| `tests/orchestration/test_smoke_3task.py` | 旧 batch 概念测试 |
| `tests/orchestration/test_stage_timestamps_naming_backoff.py` | 旧 #8/#9/#7 测试 |

### 重写文件

- `orchestration/master.py`(14 个旧 batch 方法 → 3 个:run/shutdown/status)
- `orchestration/queue/sqlite_queue.py`(16 个旧方法 → 9 个新方法)
- `orchestration/queue/schema.sql`(删 batches/run_tasks,改 tasks 表)
- `orchestration/queue/__init__.py`(state 常量 → phase 常量)
- `orchestration/workers/{base,gdr,etl}_worker.py`(类 → 模块函数)
- `orchestration/producer_simulate.py`(`run_batch` → `run_one_task`)
- `orchestration/config_loader.py`(重构 OrchestrationConfig)
- `orchestration/health.py`(`collect_batches` → `collect_tasks`)
- `orchestration/failure_handler.py`(删 batch_id)
- `orchestration/__main__.py`(argparse 精简 + 重写子命令)
- `simulate_serve/__main__.py`(删任务 CLI,保留只读开关)
- `config/config.yaml` + `config/config.example.yaml`(orchestration 段重写)

### 新增文件

- `orchestration/pipeline_executor.py`(PipelineExecutor + PipelineSummary)
- `orchestration/task_pipeline.py`(_worker_init + _run_one_task_pipeline)
- `orchestration/settings.py`(PipelineSettings + Paths)
- `tests/orchestration/test_pipeline_executor.py`(13 测试)
- `tests/orchestration/test_task_pipeline.py`(11 测试)
- `tests/orchestration/test_etl_worker.py`(10 测试)
- `docs/设计方案/pipeline-serial-parallel-refactor.md`(方案)
- `docs/设计方案/pipeline-contracts.md`(契约)
- `docs/设计方案/parallel-execution-plan.md`(本轮执行清单)
- `docs/设计方案/round-1-summary.md`(本汇总)

## 六、验证标准达成

| 标准 | 状态 |
|---|---|
| `pytest tests/orchestration` 全绿 | ✅ 197/197 |
| `pytest tests/contract tests/functional tests/unit` 全绿 | ✅ 299/299 |
| `python -m simulate_serve --validate-config` 工作 | ✅ |
| `python -m orchestration start --all-tasks --parallelism 1 --dry-run` 跑通 | ✅ |
| CLI 子命令(开始/状态/停止/重放) | ✅ |
| max_parallelism ≥2 真起多子进程 | ⚠️ dry-run 验证,dry-run 不起进程;真实跑需要联调时再做 |

## 七、未完成项(留给下一轮)

1. **README.md / orchestration/README.md / docs/orchestration-design.md 文档改写** — 方案文档 §10 列出但本轮未做。
2. **真实 catalog 端到端 smoke**(`python -m orchestration start --all-tasks --parallelism 1`) — 本轮只跑了 `--dry-run`,真实跑需联调。
3. **并行 N=4 真子进程验证** — 本轮未跑。
4. **catalog 98 task 完整跑通** — 需后续联调 + 监控产出。

## 八、下一轮待办

建议下一轮做以下其中之一:

| 选项 | 范围 |
|---|---|
| A | 文档改写(README / orchestration/README / docs/orchestration-design.md) |
| B | 联调 smoke:真实跑 1 task / 3 task / 全 catalog(parallelism=1 与 =4) |
| C | 集成 verify:`status` 子命令 / 死信归档 / replay 全流程 |
| D | A + B + C 三合一 |

请指定下一轮内容。
