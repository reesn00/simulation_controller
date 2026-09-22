# 3 组并行实施执行清单(2026-09-22)

## 总体方案

- **切片方式**:纵向切片 A(数据 / 业务 / 调度)
- **契约依据**:`docs/设计方案/pipeline-contracts.md`(只读,子 agent 不改)
- **范围依据**:`docs/设计方案/pipeline-serial-parallel-refactor.md`(只读)
- **测试归属**:各组负责自己模块的单元测试
- **契约变更**:子 agent **不能改契约**,只能写入 `<agent_id>_design_adjustments.md`

---

## 三组任务包

### 组 1:数据层(Data Layer)

**负责 agent**:`agent-data`
**问题记录文件**:`docs/设计方案/agent-data_design_adjustments.md`

**职责范围**(对应 ST-1 + ST-2):

| 文件 | 操作 | 依据 |
|---|---|---|
| `config/config.yaml` | 改 `orchestration:` section(§3 schema) | §1.2 |
| `orchestration/settings.py` | **新增**:`PipelineSettings` / `Paths` / `OrchestrationConfig` | §1.3 |
| `orchestration/config_loader.py` | 改:实现 `load_config()` | §1.4 |
| `orchestration/queue/schema.sql` | 改:删 `batches` / `run_tasks`,改 `tasks` | §2.2 |
| `orchestration/queue/__init__.py` | 改:state 常量 → phase 常量 | §2.3 |
| `orchestration/queue/sqlite_queue.py` | 改:`Task` dataclass + `upsert_task` / `mark_phase` / `increment_attempts` / `mark_failed` / `requeue_dead` / `get_task` / `list_tasks` / `count_by_phase` | §2.4 - §2.5 |

**删除**(明确不导出):见 §2.8

**测试文件**:
- `tests/orchestration/test_config_loader.py` 改
- `tests/orchestration/test_queue.py` 大改

**约束**:
- 不写 SQLiteQueue 之外的逻辑(不实现 PipelineExecutor 等)
- 不读 orchestration/master.py / pipeline_executor.py(不依赖调度层)
- 不写 multiprocessing 相关代码
- 严格按契约 §1 - §2 落地

**输出**:
- 改动文件清单
- `agent-data_design_adjustments.md`(如有问题)

---

### 组 2:业务层(Business Layer)

**负责 agent**:`agent-business`
**问题记录文件**:`docs/设计方案/agent-business_design_adjustments.md`

**职责范围**(对应 ST-3 + ST-4):

| 文件 | 操作 | 依据 |
|---|---|---|
| `orchestration/workers/base_worker.py` | 改:删 `BaseWorker` 类,留 `_output_filename` 模块函数 | §3.2 |
| `orchestration/workers/gdr_worker.py` | 改:实现 `run_gdr_once()` + `GdrResult` + `GdrNonRetryableError` | §3.3 |
| `orchestration/workers/etl_worker.py` | 改:实现 `run_etl_once()` + `EtlOutputs` + `EtlNonRetryableError` | §3.4 |
| `orchestration/producer_simulate.py` | 改:实现 `async run_one_task(task_id, *, config_path)` | §4.2 |

**删除**(明确不导出):见 §3.5 + §4.4

**测试文件**:
- `tests/orchestration/test_gdr_worker.py` 大改
- `tests/orchestration/test_etl_worker.py` 大改
- `tests/orchestration/test_producer_simulate.py` 改

**约束**:
- 不写 PipelineExecutor / Master / CLI(不依赖调度层)
- 不写 SQLiteQueue 写操作(由 PipelineExecutor 调 `mark_phase`)
- 不动 SQLite schema / config loader
- 严格按契约 §3 - §4 落地

**输出**:
- 改动文件清单
- `agent-business_design_adjustments.md`(如有问题)

---

### 组 3:调度层(Orchestration Layer)

**负责 agent**:`agent-orchestration`
**问题记录文件**:`docs/设计方案/agent-orchestration_design_adjustments.md`

**职责范围**(对应 ST-5 + ST-6 + ST-7):

| 文件 | 操作 | 依据 |
|---|---|---|
| `orchestration/pipeline_executor.py` | **新增**:`PipelineExecutor` + `PipelineSummary` | §5.2 - §5.3 |
| `orchestration/task_pipeline.py` | **新增**:`_worker_init` + `_run_one_task_pipeline` + 内部辅助 | §5.4 - §5.6 |
| `orchestration/master.py` | 改:实现 `Master.run/shutdown/status/_build_gdr_settings`,删 14 个旧方法 | §6.2 - §6.3 |
| `orchestration/batch_tracker.py` | **整文件删** | §6 |
| `orchestration/failure_handler.py` | 改:`reap_dead` 删 batch_id 引用,改 `DeadArchive` dataclass | §6.4 |
| `orchestration/health.py` | 改:`collect_batches` → `collect_tasks` + `write_health` | §6.5 |
| `orchestration/__main__.py` | 改:`_cmd_start/status/replay` 重写 + argparse 精简 | §7.3 - §7.5 |
| `simulate_serve/__main__.py` | 改:删 `--tasks` / `--rerun-task` / `--limit` / `--include-offline` / `--max-run-retries`,保留只读开关 | §7.6 |
| `gdr/run.bat` | **整文件删** | §7.7 |
| `scripts/run.bat` | 改注释:`--batch-size N` 示例改 `--parallelism N` | §7.7 |

**测试文件**:
- **新增** `tests/orchestration/test_pipeline_executor.py`(N=1/2/4 + 死信 + 子进程崩溃)
- **新增** `tests/orchestration/test_task_pipeline.py`(三阶段顺序 + 阶段内重试)
- `tests/orchestration/test_master.py` 大改
- `tests/orchestration/test_orchestration_cli.py` 大改
- `tests/orchestration/test_health.py` 改
- `tests/orchestration/test_failure_handler.py` 改
- `tests/orchestration/test_failure_recovery.py` 大改
- **整删** `tests/orchestration/test_batch_tracker.py`
- **整删** `tests/orchestration/test_direction_b_batch_isolation.py`
- **整删** `tests/orchestration/test_watcher.py`

**约束**:
- 严格按契约 §5 - §7 落地
- SQLiteQueue 接口按 §2 调用(数据层落地后才有真接口;契约文档是 API 蓝图)
- 不动 worker / producer(业务层负责)
- 不动 config / sqlite schema

**输出**:
- 改动文件清单
- `agent-orchestration_design_adjustments.md`(如有问题)

---

## 主 agent 工作流

### 阶段 0:启动前确认
- 契约文档 `pipeline-contracts.md` 已写完 ✓
- 方案文档 `pipeline-serial-parallel-refactor.md` 已写完 ✓
- 本执行清单 `parallel-execution-plan.md` 已写完 ✓

### 阶段 1:启动 3 个并行子 agent
- 3 个 `Agent` tool 调用,**同一条 message 内**并行发出
- 每个 agent 拿到:
  - 契约文档路径 + 方案文档路径 + 本执行清单
  - 自己组的任务包(上表)
  - 问题记录文件路径
  - 严格约束(不能改契约,只能写问题文件)

### 阶段 2:等待 3 个 agent 完成
- 用 `SendMessage` 续接在途 agent(如有需要)
- 不读 agent 输出文件(避免灌入 context),只看 hand-back 报告

### 阶段 3:收集 + 分析
- 收 3 个 hand-back 报告
- 读 3 个 `_design_adjustments.md` 文件
- 分类:
  - **A. 重大决策**(必须问人工):契约边界假设错误 / 跨组接口不匹配 / 删除的东西被某模块隐式依赖
  - **B. 实现细节微调**(主流方案处理):内部命名风格 / 局部变量名 / 异常信息措辞
  - **C. 测试覆盖缺口**(主 agent 补):某场景漏测

### 阶段 4:处理遗留项
- A 类 → AskUserQuestion 询问
- B 类 → 直接按主流方案修(在主 agent 这边改)
- C 类 → 主 agent 补测试

### 阶段 5:串联验证
```bash
# 1. 编译/导入检查
python -c "from orchestration.config_loader import load_config; from orchestration.queue.sqlite_queue import SQLiteQueue; from orchestration.workers.gdr_worker import run_gdr_once; from orchestration.workers.etl_worker import run_etl_once; from orchestration.producer_simulate import run_one_task; from orchestration.pipeline_executor import PipelineExecutor; from orchestration.master import Master; from orchestration.__main__ import main"

# 2. 各组单元测试
pytest tests/orchestration -q
pytest tests/unit -q
pytest tests/contract -q
pytest tests/functional -q

# 3. 集成 smoke
python -m simulate_serve --validate-config
python -m orchestration start --all-tasks --parallelism 1 --dry-run

# 4. (如有 fixture) 真实跑 1 个 task
python -m orchestration start --tasks T001 --parallelism 1
```

### 阶段 6:汇总报告 + 进入下一轮
- 主 agent 写最终汇总到 `docs/设计方案/round-1-summary.md`
- 进入下一轮(下一轮内容待你指定,可能是文档 + smoke 验证 / 修 bug / 新需求)

---

## 子 agent 报告模板(子 agent 必须按此模板回)

```markdown
# <agent_id> 实施报告

## 完成度
- [ ] 全部任务完成
- [ ] 部分完成(列出未完成项)
- [ ] 阻塞(说明阻塞原因)

## 改动文件清单
| 文件 | 操作 | 改动行数 |
|---|---|---|
| ... | 新增 / 改 / 删 | +N -M |

## 测试覆盖
- 新增测试:<数量>
- 改测试:<数量>
- 删测试:<数量>
- 全部测试通过:[是 / 否 + 失败列表]

## 设计调整与遗留问题
见 `docs/设计方案/<agent_id>_design_adjustments.md`

## 跨组集成疑问
1. [对其他组的疑问,需主 agent 协调]

## 实施过程中发现的契约问题(只写不改)
1. [契约文档某条款与实际不符 / 缺失]
```

---

## 关键风险

| 风险 | 缓解 |
|---|---|
| 3 个 agent 同时改公共文件 | 切片已按文件边界切,无公共文件 |
| 跨组接口对不上 | 契约文档是单一真理源,3 个 agent 都按契约实现 |
| 子 agent 改契约文件 | 明确禁止,只写问题文件 |
| 子 agent 引入新依赖 | 严禁 pip install 新包 |
| 主 agent context 灌爆 | 不读子 agent 输出文件,只看 hand-back 报告 |
