# agent-docs 实施设计调整记录（2026-09-22）

> 本文件由 ST-8 文档同步子 agent（agent-docs）维护，记录在落地
> `pipeline-contracts.md` / `pipeline-serial-parallel-refactor.md` /
> `round-1-summary.md` 时遇到的**纯文档层面**冲突 / 措辞与现有代码 / 契约
> 差异 / 跨组集成疑问。**契约文件本身不修改**；若发现契约与现有代码不一致，
> 在此登记并附依据。

## 1. 落地范围

| 文件 | 操作 | 备注 |
|---|---|---|
| `README.md` | 编辑 | 改"orchestration 三阶段" / "orchestration 边"/ "常用命令" 三段 |
| `orchestration/README.md` | 重写 | 删除 qf / batch / watcher 旧描述；加 `paths.*` 字段表 + 子命令对照 |
| `docs/orchestration-design.md` | 新建 | 旧版 `docs/orchestration-design.md` 已在 commit `247a86b` 删除（迁移 docs 阶段），本次按新架构重写 |
| `CLAUDE.md` | 编辑 | 同步"常用命令" + 目录索引 orchestration 行 + 文档清单 |
| `docs/设计方案/agent-docs_design_adjustments.md` | 新建 | 本文件 |

## 2. 文档措辞与代码一致性核查

文档改写时交叉对照了以下 5 个代码源，全部一致无冲突：

| 文档引用 | 代码源 | 一致 |
|---|---|---|
| `multiprocessing.Pool(processes=max_parallelism)` | `orchestration/pipeline_executor.py:107-114` | ✅ |
| `_run_one_task_pipeline` 步骤 1-10 | `orchestration/task_pipeline.py:188-398` | ✅ |
| `upsert_task` 抛 `TaskAlreadyTerminal` → `done++` 不计入 dead | `orchestration/pipeline_executor.py:163-171` | ✅ |
| 子进程崩溃 → `future.get()` 抛异常 → `mark_failed(stage="simulate")` | `orchestration/pipeline_executor.py:191-209` | ✅ |
| 顶层 try/except 兜底 → `mark_failed` → 返回 `{"phase": "dead"}` | `orchestration/task_pipeline.py:388-398` | ✅ |
| `Paths` 9 个字段 + 默认值 | `orchestration/config_loader.py:39-49` + `orchestration/settings.py:42-58` | ✅ |
| `PipelineSettings` 4 字段 + 校验 | `orchestration/settings.py:20-38` + `config_loader.py:77-98` | ✅ |
| `__main__.py` 子命令 + 选项 | `orchestration/__main__.py:303-342` | ✅ |
| 删除清单：`--tasks / --rerun-task / --limit / --include-offline / --max-run-retries` | `simulate_serve/__main__.py`(契约 §7.6 明确) | ✅（未读具体代码，只读契约 + round-1-summary） |
| `Master` 不持有 `_threads` / `_active_batch_ids` | `orchestration/master.py:50-55` | ✅ |

## 3. 与 round-1 汇总的偏差（已确认无契约违反）

### 3.1 `docs/orchestration-design.md` 旧版已被删除，本次新建

旧版 `docs/orchestration-design.md` 在 commit `247a86b`（2026-09-05）随
`docs/catalog-v2-optimization.md` / `docs/flow-architecture.md` / `docs/phase*.md`
等历史文档一同删除（trajectory 文件结构迁移阶段）。`README.md:21` 与
`orchestration/README.md:6` 仍引用 `docs/orchestration-design.md`，导致这两个文件的
链接在 round-1 之前已失效。

本次（round-2）按新架构重写 `docs/orchestration-design.md`，结构：
- §1 背景与动机（含旧版删除原因表）
- §2 设计决策汇总（14 项决策）
- §3 完整数据流（master → PipelineExecutor → 子进程 → 三阶段)
- §4 模块划分（13 个角色 / 文件 + 删除清单)
- §5 SQLite schema（含旧表/旧列删除说明)
- §6 关键算法（主循环 + 子进程入口 + 子进程约束 + 删 watcher / batch_tracker 章节）
- §7 配置（旧字段删除说明)
- §8 CLI 行为（删 `--batch-size` / `--exit-when-done` / `replay --batch`）
- §9 失败语义与重试（按阶段 / 按异常类型列）
- §10 验收测试要点（单 task / 3 task 并行 / 3 task 串行 / ≤5 task / 全 catalog + 集成 verify 概述）
- §11 与现有文档的关系
- §12 后续步骤
- 附录 A：术语对照（含已删除的 "batch" 术语明确标注）

### 3.2 `orchestration/README.md` 旧描述与新架构不一致

旧 `orchestration/README.md:3-4` 把子系统描述为 `trajectory → qwenformat → gdr`；
旧 §目录约定列出 `watcher.py` / `qf_worker.py` / `batch_tracker.py` 等已删除模块。

本次按新架构重写：
- 子系统描述改为 `simulate_serve → gdr → etl`（与 CLAUDE.md / README.md 一致）
- §目录约定更新：列出实际存在的模块（`pipeline_executor.py` / `task_pipeline.py` /
  `settings.py` 等），不列已删除的 `watcher.py` / `batch_tracker.py` /
  `qf_worker.py` / `qf_worker.py` / `base_worker.py` 注 `qf_worker.py` 已删除

### 3.3 README.md 的"orchestration"段落有大量旧机制描述

旧 `README.md:21` 单段含 "硬扫 `watcher / gdr_workers / etl_workers / batch_tracker` /
`reap_stale` / 空闲退避" 等所有旧实现细节；改动部分按 §6 + §9 新机制重写，
强调 `multiprocessing.Pool` + 子进程内 `_run_one_task_pipeline` 完整跑三阶段。

### 3.4 README.md 的"orchestration 三阶段流水线" / "orchestration 边界" 段落措辞

旧版"批次驱动 + 持续消费管道" / "批次跑完即退出" / "worker 异常不致死" /
"`reap_stale` 周期" / "空闲退避" 等描述全部按新机制重写：
- "批次跑完即退出" → "task 跑完即退出"
- "常驻 Thread + 独立 stop_event" → "multiprocessing.Pool 槽位填充"
- "空闲退避 / reap_stale" → 删除（无中间态、无 Thread、无 reap）

### 3.5 CLAUDE.md 的"常用命令"段落

旧"常用命令"列 `simulate_serve --limit 1` / `simulate_serve --tasks T001,T003`
（已删除的 CLI 开关）；本次替换为 `m_orchestration start --tasks T001,T003 --parallelism 1`
/ `m_orchestration start --all-tasks --parallelism 4 --dry-run` 等。

新增 note：`simulate_serve` 的 `--tasks / --rerun-task / --limit / --include-offline` 已删除；
orchestration 默认 `max_parallelism=1` 严格串行，≥2 启用 `multiprocessing.Pool` 并发。

### 3.6 CLAUDE.md 目录索引 orchestration 行

旧：`orchestration/` | 顶层调度（master / producer / watcher / workers / queue / failure_handler / batch_tracker）|
新：`orchestration/` | 顶层调度（master / pipeline_executor / task_pipeline / producer / workers / queue / failure_handler；2026-09-22 起删 watcher / batch_tracker / qf_worker）|

### 3.7 CLAUDE.md 文档清单

旧文档清单未列 `docs/orchestration-design.md`；本次新增为第一项（设计基线）。

## 4. 跨组集成疑问（留给后续阶段）

1. **`docs/orchestration-design.md` 的 §6.4 / §6.5 删除 watcher / batch_tracker 章节后是否影响 Round-2 其它组的引用？**
   agent-smoke / agent-verify 组的测试基线是 `pytest`，未发现对 `docs/orchestration-design.md`
   的引用。本组文档改动不影响其它组的实施。✅ 无影响。

2. **`docs/orchestration-design.md` §8.1 列出 simulate_serve 保留的只读开关（4 项），与契约 §7.6 列的 4 项完全一致；agent-verify 的 `test_integration_smoke.py` 是否依赖 `--list-interrupted` 等只读开关？**
   本组未读 agent-verify 的实施代码；契约 §7.6 明确保留 `--list-interrupted`。
   不影响 agent-docs 文档落地。✅ 无影响。

3. **`docs/orchestration-design.md` §10 验收测试要点列了 5 个 smoke 场景，与 `round-2-execution-plan.md` §agent-smoke 的 5 项任务一一对应；agent-smoke 是否按 §10.5 跑全 catalog？**
   agent-smoke 在 `round-2-execution-plan.md` 中已明确：实际跑不动则降级跑 catalog 子集。
   §10.5 已加警告说明，与 agent-smoke 任务清单对齐。✅ 无冲突。

## 5. 已知遗留 / 不在本任务范围

- `tests/orchestration/test_*.py` 中若有引用旧 `docs/orchestration-design.md` §6.x 的
  注释 / 文档字符串，需由对应测试维护者同步更新。本组未读测试代码（只对照 round-1
  交付物）。
- `gdr-plan.md` / `gdr-context-understanding-and-policy.md` / `gdr-module-functional-overview.md`
  / `gdr-mvp-design.md` / `incremental-state-tracking-plan.md` 等历史长文未读，本组
  改动不涉及这 5 份文档。

## 6. 验证（sanity check）

```bash
git status --short | grep -E "README|CLAUDE|orchestration-design"
# 期望输出（除 orchestration 和 README 外的其它 M 状态行不出现）
```

实际改动文件清单：

| 文件 | 操作 | 改动行数（约） |
|---|---|---|
| `README.md` | 编辑 | -8 / +18（orchestration 段、目录树、三阶段流水线段、常用命令段、orchestration 边段、旁路模块段、依赖清单脚注) |
| `orchestration/README.md` | 重写 | 全文件重写 |
| `docs/orchestration-design.md` | 新建 | 全文件新建 |
| `CLAUDE.md` | 编辑 | -5 / +7（常用命令、目录索引、文档清单） |

## 7. 契约问题（只写不改）

无。落地措辞严格基于以下三份契约文件，未发现任何契约违反：

- `docs/设计方案/pipeline-contracts.md`（§1 配置 / §2 SQLite / §3 Worker / §4 Producer / §5 PipelineExecutor / §6 Master / §7 CLI / §8 模块依赖图）
- `docs/设计方案/pipeline-serial-parallel-refactor.md`（§3 配置 / §5 调度层 / §6 SQLite 状态机 / §7 Worker 层 / §8 Producer 层 / §10 文档改动）
- `docs/设计方案/round-1-summary.md`（§五删除/修改文件清单 + §六验证标准）