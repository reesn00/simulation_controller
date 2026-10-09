# Orchestration 流水线重构方案(2026-09-22)

> 目标:把 simulate_serve / gdr / etl 三阶段从"按批次切片 + 批次内并行调度"改成
> "单 task 三阶段严格串行,跨 task 可配置并行度"的一体化流水线。**不做兼容性兼容**,
> 原 batch / watcher / 多 worker 线程 / 中间态 / 旧配置字段一律按需删或重写。

## 1. 背景

现状 orchestration 是"批次调度系统":
- `_split_batches(task_ids, batch_size)` 把任务列表切批
- 每批起 `producer_simulate` + `watcher` + N 个 `GdrWorker` + M 个 `EtlWorker` 线程
- SQLite 状态机驱动 `pending → gdr_processing → pending_etl → etl_processing → done`
- `wait_batch_drained` 阻塞到本批所有 task 走完

用户需求:删批次概念,改成"一个 task 跑完三阶段才进下一个 task",且支持配置并行度
(例如 `--parallelism 2` 时 t001 / t002 并行,跑完一组再进 t003 / t004)。

## 2. 设计决策

| 决策 | 选择 |
|---|---|
| 并行语义 | **A**:任务级并行 + 阶段内顺序。同一 task 内 simulate→gdr→etl 严格串行,不同 task 可任意阶段重叠 |
| 并行实现 | **multiprocessing 子进程池**(`multiprocessing.Pool`) |
| 任务清单 | 默认 `--all-tasks`,可选 `--tasks T1,T2` 过滤 |
| 兼容性 | **不考虑**。原 batch / watcher / 多 worker / 中间态 / 旧配置字段按需重写或删除 |
| max_parallelism 默认值 | **1**(等同严格串行,与原始需求对齐) |

## 3. 配置层

`config/config.yaml` 中 `orchestration:` section:

```yaml
orchestration:
  pipeline:
    max_parallelism: 1            # 默认串行; >1 启用子进程并行
    max_retry_gdr: 3
    max_retry_etl: 3
    retry_poll_seconds: 2         # status 子命令轮询 SQLite 用
  paths:
    simulate_serve_config: ...
    trajectory_dir: "output/agent_trajectory"
    runs_dir: "output/runs"
    refined_dir: "output/refined"
    etl_outputs_dir: "output/refine_data"
    sqlite_db: "output/orchestration/orchestration.db"
    dead_dir: "output/orchestration/dead"
    pid_file: "output/orchestration/orchestration.pid"
    log_dir: "output/orchestration/logs"
```

**删除字段**:
- `orchestration.batch_size` / `gdr_workers` / `qf_workers` / `gdr_wait_seconds`
- `orchestration.max_retry_qf` / `watcher_poll_seconds` / `reap_stale_seconds`
- `orchestration.worker_idle_backoff_max_seconds` / `watcher_idle_backoff_max_seconds`
- `paths.qf_output_dir` / `gdr_output_dir`
- `orchestration.batch_drain_poll_seconds` / `batch_drain_timeout_seconds`
- `orchestration.reap_stale_interval_seconds`

## 4. CLI 层

### 4.1 `orchestration/__main__.py`

```
python -m orchestration start                       # 默认 max_parallelism=1 严格串行
python -m orchestration start --parallelism 4       # 4 个 task 并行
python -m orchestration start --tasks T001,T005     # 子集过滤
python -m orchestration start --all-tasks           # 显式声明全 catalog
python -m orchestration start --detach / --foreground / --dry-run / --stay
python -m orchestration status                      # 实时健康检查
python -m orchestration stop                        # 优雅停止
python -m orchestration replay                      # 死信复活
```

**删除**:`--batch-size` / `--exit-when-done`(already deprecated)

### 4.2 `simulate_serve/__main__.py`

**删除**:`--tasks` / `--rerun-task` / `--limit` / `--include-offline` / `--max-run-retries`
(模拟任务的入口全部移交 orchestration)

**保留**:`--validate-config` / `--check-tools` / `--readiness` / `--list-interrupted`
(只读开关,不影响 orchestration)

### 4.3 `gdr/pipeline/cli.py`

**保留所有 CLI 选项**(开发期调试仍可独立调),但不再被 orchestration 引用。

### 4.4 `gdr/run.bat`

**删除**(开发期 wrapper,与新流水线语义冲突)

### 4.5 `scripts/run.bat`

**保留**(透传 `python -m orchestration %*`),改注释:`--batch-size N` 示例改为 `--parallelism N`

## 5. 调度层

### 5.1 新模块 `orchestration/pipeline_executor.py`

核心数据结构 + 主循环:

```python
class PipelineExecutor:
    """按 max_parallelism 调度 N 个 task 的 simulate→gdr→etl 流水线."""

    def __init__(self, *, queue, settings, paths, gdr_settings):
        self._queue = queue
        self._settings = settings
        self._paths = paths
        self._gdr_settings = gdr_settings
        self._process_pool: multiprocessing.Pool | None = None

    def run(self, task_ids: list[str]) -> PipelineSummary:
        self._process_pool = multiprocessing.Pool(
            processes=max(1, self._settings.max_parallelism),
            initializer=_worker_init,
            initargs=(self._paths,),
        )
        try:
            return self._dispatch(task_ids)
        finally:
            self._process_pool.close()
            self._process_pool.join()

    def _dispatch(self, task_ids: list[str]) -> PipelineSummary:
        in_flight: dict[AsyncResult, str] = {}
        pending = list(task_ids)
        done_count = 0
        dead_count = 0

        def _fill_slots() -> None:
            while (len(in_flight) < self._settings.max_parallelism
                   and pending):
                task_id = pending.pop(0)
                self._queue.upsert_task(task_id, phase="pending")
                future = self._process_pool.apply_async(
                    _run_one_task_pipeline,
                    (task_id, self._paths, self._gdr_settings, self._settings),
                )
                in_flight[future] = task_id

        _fill_slots()
        while in_flight:
            for future in list(in_flight):
                if future.ready():
                    task_id = in_flight.pop(future)
                    try:
                        result = future.get(timeout=0)
                        if result["phase"] == "done":
                            done_count += 1
                        else:
                            dead_count += 1
                    except Exception as exc:
                        dead_count += 1
                        logger.error("task %s pipeline crashed: %s",
                                     task_id, exc)
                    _fill_slots()
            time.sleep(0.05)
        return PipelineSummary(total=len(task_ids),
                               done=done_count, dead=dead_count)
```

### 5.2 新模块 `orchestration/task_pipeline.py`(子进程入口)

```python
def _worker_init(paths: Paths) -> None:
    """子进程初始化:读 simulate_serve config / 建独立 SQLite 连接."""
    global _WORKER_PATHS
    _WORKER_PATHS = paths


def _run_one_task_pipeline(
    task_id: str,
    paths: Paths,
    gdr_settings: Settings,
    orchestration_settings: PipelineSettings,
) -> dict:
    """单个 task 在子进程里跑 simulate → gdr → etl."""
    # 1. simulate_serve
    run = _run_simulate(task_id, paths)
    if run.state in TERMINAL_FAIL_STATES:
        _mark_dead(task_id, run, "simulate_failed")
        return {"task_id": task_id, "phase": "dead", "stage": "simulate"}

    # 2. gdr
    gdr_out = _run_gdr(run, paths, gdr_settings,
                       orchestration_settings.max_retry_gdr)
    if gdr_out is None:
        _mark_dead(task_id, run, "gdr_failed")
        return {"task_id": task_id, "phase": "dead", "stage": "gdr"}

    # 3. etl
    etl_out = _run_etl(gdr_out, paths, orchestration_settings.max_retry_etl)
    if etl_out is None:
        _mark_dead(task_id, run, "etl_failed")
        return {"task_id": task_id, "phase": "dead", "stage": "etl"}

    _mark_done(task_id, run, gdr_out, etl_out)
    return {"task_id": task_id, "phase": "done", "stage": "done"}
```

**关键点**:
- 每个子进程跑一个 task 的完整 pipeline(不跨进程),单 task 内三阶段不需要跨进程通信
- 子进程间唯一共享资源是 SQLite(SQLite 内部 serializes 写)
- `max_parallelism=N` 等价于 N 个子进程同时跑

### 5.3 `orchestration/master.py`(精简)

```python
class Master:
    def __init__(self, cfg: OrchestrationConfig):
        self._cfg = cfg
        self._queue = SQLiteQueue(cfg.paths.sqlite_db)
        self._stop_event = threading.Event()

    def run(self, task_ids: list[str]) -> PipelineSummary:
        executor = PipelineExecutor(
            queue=self._queue,
            settings=self._cfg.settings.pipeline,
            paths=self._cfg.paths,
            gdr_settings=self._build_gdr_settings(),
        )
        try:
            return executor.run(task_ids)
        finally:
            write_health(self._queue, ...)

    def shutdown(self) -> None:
        self._stop_event.set()

    def status(self) -> dict:
        return collect_tasks(self._queue)
```

**删除**:`start_workers` / `_start_batch_watcher` / `_first_scan_watcher` /
`wait_batch_drained` / `register_active_batch` / `unregister_active_batch` /
`_reaper_loop` / `_add_thread` / `_threads` / `_active_batch_ids` /
`_workers_started` / `alive_workers` / `_count_terminal_for_batch` /
`_run_one_batch`(改名为 `_run_one_task` 并简化)

## 6. SQLite 状态机

### 6.1 `orchestration/queue/schema.sql`

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL UNIQUE,
    run_id              TEXT,
    session_id          TEXT,
    phase               TEXT NOT NULL CHECK(phase IN
                          ('pending','simulate','gdr','etl','done','dead')),
    attempts_simulate   INTEGER NOT NULL DEFAULT 0,
    attempts_gdr        INTEGER NOT NULL DEFAULT 0,
    attempts_etl        INTEGER NOT NULL DEFAULT 0,
    src_path            TEXT,
    gdr_refined_path    TEXT,
    etl_messages_path   TEXT,
    etl_openai_path     TEXT,
    etl_qwenjina_path   TEXT,
    etl_meta_path       TEXT,
    error_msg           TEXT,
    started_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_phase ON tasks(phase);
```

**删除**:
- `batches` 表(整表)
- `run_tasks` 表(整表)
- `tasks.batch_id` / `locked_by` / `locked_at` 列
- `tasks.state` 中间态(`gdr_processing` / `etl_processing`)— 用内存 future + 进程边界替代

### 6.2 `SQLiteQueue` 方法变更

| 方法 | 处理 |
|---|---|
| `upsert_task(task_id)` | 新增 — `INSERT OR IGNORE` |
| `mark_phase(task_id, phase, **paths)` | 新增 — 推进 phase |
| `mark_failed(task_id, stage, error_msg)` | 改 — 不再写 batch_id |
| `requeue_dead()` | 改 — 删 batch_id 参数,扫全表 |
| `count_by_phase()` | 新增 — 替 `count_pending_gdr` / `count_pending_etl` |
| `list_tasks(phase=..., limit=...)` | 新增 — 替 `list_tasks_for_batch` |
| `insert_batch` / `update_batch` / `insert_run_task_map` / `lookup_run` / `lookup_task_id` / `reap_stale` / `_pull_n` / `_stamp_stage_done` | **删** |

## 7. Worker 层(改函数式)

### 7.1 `orchestration/workers/base_worker.py`

- 删 `BaseWorker.run_forever`(无线程)
- 保留 `run_once` 作为函数式实现参考

### 7.2 `orchestration/workers/gdr_worker.py`

- `GdrWorker.process()` → `def run_gdr_once(src_path, refined_dir, gdr_settings) -> Path`
- 删 `pull()` / `run_forever()` / 走 SQLite 推进的部分
- `mark_done` 简化为"返回 Path",由 PipelineExecutor 调 `queue.mark_phase(task_id, 'etl', gdr_refined_path=...)`

### 7.3 `orchestration/workers/etl_worker.py`

- 同上,改 `run_etl_once(c2_path, etl_outputs_dir) -> dict[Path]`

## 8. Producer 层(`orchestration/producer_simulate.py`)

```python
async def run_one_task(task_id: str) -> TaskRun:
    """in-process 跑 simulate_serve 单 task,返回 TaskRun."""
    services = await build_application(_load_config())
    task = next(t for t in services.task_manager.compiled_tasks
                if t.task_id == task_id)
    runs = await services.batch_runner.run([task])
    return runs[0]
```

- 删 `insert_batch` / `update_batch` / `insert_run_task_map` / `limit` 参数
- 删 `run_batch` 多 task 调度(由 PipelineExecutor 调 `run_one_task` 多次)

## 9. 入口层清理

| 文件 | 处理 |
|---|---|
| `simulate_serve/__main__.py` | 删 `--tasks` 等任务相关 CLI,保留只读开关 |
| `gdr/pipeline/cli.py` | 保留 CLI(开发期用),不删 |
| `gdr/run.bat` | 删 |
| `etl/` | 无独立 CLI,无需改 |
| `scripts/run.bat` | 保留,改注释 |

## 10. 文档改动

| 文件 | 改动 |
|---|---|
| `README.md` | 删 `--batch-size` 例子;改"orchestration 三阶段"章节为"流水线并行";改"orchestration 边界"章节 |
| `orchestration/README.md` | 同步 |
| `docs/orchestration-design.md` | §6 主循环章节改写 |
| `docs/contracts/C1/C2/C3-*.md` | 不变(契约层未动) |
| `docs/contracts/migration-plan.md` | 历史归档,保留 |

## 11. 测试改动

### 11.1 整文件删

- `tests/orchestration/test_batch_tracker.py`
- `tests/orchestration/test_direction_b_batch_isolation.py`
- `tests/orchestration/test_watcher.py`
- `tests/orchestration/test_smoke_3task.py` 的 `test_smoke_multi_batch_sequential`

### 11.2 大幅改

- `tests/orchestration/test_master.py`:删 batch 相关测试;新增 PipelineExecutor 测试
- `tests/orchestration/test_queue.py`:删 batch / run_tasks / state 中间态测试
- `tests/orchestration/test_health.py`:改 `collect_batches` → `collect_tasks`
- `tests/orchestration/test_failure_handler.py`:删 batch_id 引用
- `tests/orchestration/test_gdr_worker.py` + `test_etl_worker.py`:改测 inline 函数
- `tests/orchestration/test_producer_simulate.py`:删 batches 表断言
- `tests/orchestration/test_orchestration_cli.py`:删 `--batch-size` / `--batch N` 测试,加 `--parallelism N` 测试
- `tests/orchestration/test_smoke_3task.py`:改单 task 端到端

### 11.3 新增

- `tests/orchestration/test_pipeline_executor.py`:
  - 覆盖 N=1 / N=2 / N=4 三种并行度
  - 死信传播:某 task gdr 失败不影响其他 task
  - 子进程异常兜底:子进程崩溃仍 mark_dead
  - SQLite 并发写:多子进程同时 upsert_task 不冲突
- `tests/orchestration/test_task_pipeline.py`:
  - 覆盖 `_run_one_task_pipeline` 三阶段顺序执行
  - 阶段内重试:max_retry_gdr=2 时 gdr 失败重试 2 次后 dead

## 12. 实施顺序

```
1.  配置层 (config.yaml + config_loader.py + 新增 pipeline section)
2.  SQLite schema + queue (删 batch/run_tasks,新增 mark_phase)
3.  Producer (run_one_task 函数式)
4.  Worker (gdr/etl 改 inline 函数)
5.  PipelineExecutor (新模块 + 子进程入口)
6.  Master (重写 run + 删所有 batch 相关方法)
7.  CLI (orchestration/__main__.py + simulate_serve/__main__.py)
8.  文档 (README + orchestration/README + docs/orchestration-design.md)
9.  测试 (按 §11 逐项改 + 新增 test_pipeline_executor / test_task_pipeline)
10. 验证 (pytest 全量 + smoke 跑 3 task 端到端,parallelism=1 / =4 各一次)
```

## 13. 风险点

| 风险 | 缓解 |
|---|---|
| 多子进程并发写 SQLite | SQLite 自身 serializes 写;索引 `idx_tasks_phase` 加速查询 |
| 子进程内 simulate_serve 初始化开销 | 子进程复用同一 pool,init 一次 |
| 子进程崩溃未清理 tasks 表 dead 状态 | `_run_one_task_pipeline` 顶层 try/except,任何异常都 mark_dead |
| 并行 N=8 时 LLM rate limit | 沿用 `gdr_settings.llm_concurrency` 控制单 task 内并发;max_parallelism 是 task 级别 |
| `requeue_dead` 在多子进程场景下竞态 | requeue 后用 `phase='pending'` 的原子 `INSERT OR IGNORE` 防重复 |
| 老 CLI 用户迁移(`python -m simulate_serve --tasks T001`) | 不保留过渡,直接删(用户已确认) |

## 14. 验证标准

1. `pytest tests/orchestration` 全绿
2. `pytest tests/contract tests/functional tests/unit` 全绿
3. `python -m simulate_serve --validate-config` 仍工作(只读开关保留)
4. `python -m orchestration start --all-tasks --parallelism 1` 跑完所有 catalog
5. `python -m orchestration start --parallelism 4` 真起 4 个子进程(`ps` / task manager 验证)
6. 死信:故意制造 1 个 gdr 失败的 task,验证其余 task 不阻塞、`replay` 后能复活
7. status 子命令:实时反映 done/dead/pending 数量

## 15. 不在本次范围内

- `gdr/` 库代码(`gdr/pipeline/runner.py` 等)本身不变,只调用入口被替换
- `etl/` 库代码不变
- `simulate_serve/` 内部逻辑不变,只 CLI 入口变化
- `docs/contracts/C1/C2/C3-*.md` 契约层不变
- 旧 orchestration 配置文件迁移工具(用户已确认不兼容)

## 16. 子任务拆分(实施清单)

按模块/文件边界切分为 8 个子任务,每个子任务独立可测、可单独交付。

### ST-1 配置层改造

- **依赖**:无
- **范围**:`config/config.yaml`(`orchestration:` section 重写)+ `orchestration/config_loader.py`(`OrchestrationSettings` 重构为 `PipelineSettings + Paths`)
- **输出**:配置加载跑通,字段 `max_parallelism` 可读
- **测试**:`tests/orchestration/test_config_loader.py` 改

### ST-2 SQLite 状态机重写

- **依赖**:ST-1
- **范围**:`orchestration/queue/schema.sql`(删 `batches` / `run_tasks`,改 `tasks`)+ `sqlite_queue.py`(删旧方法,新增 `upsert_task` / `mark_phase` / `count_by_phase` / `list_tasks`)+ `__init__.py`(state 常量改 phase)
- **测试**:`tests/orchestration/test_queue.py` 大改

### ST-3 Worker 层改函数式

- **依赖**:ST-2
- **范围**:`orchestration/workers/base_worker.py`(删 `run_forever`)+ `gdr_worker.py`(改 `run_gdr_once` 函数)+ `etl_worker.py`(改 `run_etl_once` 函数)
- **测试**:`test_gdr_worker.py` + `test_etl_worker.py` 大改

### ST-4 Producer 改单 task 函数式

- **依赖**:ST-3
- **范围**:`orchestration/producer_simulate.py`(`run_batch` 改 `run_one_task`)
- **测试**:`test_producer_simulate.py` 改

### ST-5 PipelineExecutor + TaskPipeline(核心调度)

- **依赖**:ST-2 / ST-3 / ST-4
- **范围**:新增 `orchestration/pipeline_executor.py`(`PipelineExecutor` + `PipelineSummary`)+ 新增 `orchestration/task_pipeline.py`(`_worker_init` + `_run_one_task_pipeline` + 内部辅助)
- **测试**:新增 `test_pipeline_executor.py`(N=1/N=2/N=4 + 死信 + 子进程崩溃)+ 新增 `test_task_pipeline.py`(三阶段顺序 + 重试)

### ST-6 Master 重写 + 删除 batch 相关方法

- **依赖**:ST-5
- **范围**:`orchestration/master.py`(大幅精简)+ 删 `batch_tracker.py` + `failure_handler.py`(删 batch_id)+ `health.py`(`collect_batches` 改 `collect_tasks`)
- **测试**:`test_master.py` / `test_health.py` / `test_failure_handler.py` / `test_failure_recovery.py` 改

### ST-7 CLI 层重写

- **依赖**:ST-6
- **范围**:`orchestration/__main__.py`(argparse 精简 + `--parallelism N`)+ `simulate_serve/__main__.py`(删任务相关 CLI)+ 删 `gdr/run.bat` + `scripts/run.bat` 注释更新
- **测试**:`test_orchestration_cli.py` 大改

### ST-8 文档 + 测试清理 + 全量验证

- **依赖**:ST-1 ~ ST-7 全部
- **范围**:`README.md` + `orchestration/README.md` + `docs/orchestration-design.md` 改写;删 `test_batch_tracker.py` / `test_direction_b_batch_isolation.py` / `test_watcher.py` / `test_smoke_multi_batch_sequential`;改 `test_smoke_3task.py`;pytest 全量 + smoke 验证
- **测试**:全绿 + smoke 跑通

### 依赖图

```
ST-1 ──┬──> ST-2 ──┬──> ST-5 ──> ST-6 ──> ST-7 ──┐
       │           ├──> ST-3 ──┘                   │
       │           └──> ST-4 ──────────────────────┤
       └───────────────────────────────────────────┴──> ST-8
```
