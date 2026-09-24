# Orchestration 三阶段 Pipeline · 设计文档（2026-09-22 重写）

> **状态**：已实施（第 1 轮 + 第 2 轮文档同步完成）
> **范围**：`simulate_serve / gdr / etl` 三个独立模块的流水线串联层
> **不在范围**：`simulate_serve` 内部重构、`data_refiner` 接入（明确跳过）、`etl/pawsession` 接入（明确跳过）

---

## 1. 背景与动机

第 1 轮重构（2026-09-22）把 orchestration 从"批次调度系统"改造为"单 task 三阶段流水线"。旧版的批次切分 + 多 worker 线程 + watcher + 中间态（`gdr_processing` / `etl_processing`）+ 凑批等待等机制在新版本中**全部删除**，原因：

| 旧机制 | 问题 | 新方案 |
|---|---|---|
| `_split_batches(task_ids, batch_size)` | 批次切分是人为边界，与"task 跑完才进下一个"语义冲突 | 删批次；按 `--parallelism N` 直接并发 task |
| `watcher` 轮询 trajectory 入队 | simulate 完成后才能 discover，延迟 + 跨进程通信成本 | 子进程内 `_run_one_task_pipeline` 直接拼 `src_path`，无中间 watcher |
| `gdr_workers` / `etl_workers` 常驻 Thread | 空闲退避 / 拉空轮 / 死锁恢复复杂 | 删 Thread；改 `multiprocessing.Pool` 子进程，每个 task 一个完整流水线 |
| 中间态 `gdr_processing` / `etl_processing` | 需要 `reap_stale` 周期回退卡死任务 | 用 `multiprocessing.Pool` 的 future + 内存 `in_flight: dict[AsyncResult, str]` 替代，无中间态 |
| `batch_tracker.wait_for_terminal` | 凑批等待逻辑复杂 | 子进程内 `producer_simulate.run_one_task` 同步阻塞，跑完直接 mark_phase |

详细删除清单与新机制映射见 [`docs/设计方案/pipeline-serial-parallel-refactor.md`](设计方案/pipeline-serial-parallel-refactor.md) 与 [`docs/contracts/migration-plan.md`](../contracts/migration-plan.md)；模块接口级契约见 [`docs/设计方案/pipeline-contracts.md`](设计方案/pipeline-contracts.md)。

## 2. 设计决策汇总

| # | 决策点 | 选定方案 | 备注 |
|---|---|---|---|
| 1 | 跳过 `data_refiner` | gdr 直接吃 trajectory | trajectory 已是 gdr 期待的 Session JSON 形态 |
| 2 | 跳过 `etl/pawsession` | orchestration 不再调 pawsession | pawsession 是独立 ETL，与主链路平行 |
| 3 | 跳过 `qf` 中间阶段 | 直接 `simulation server → gdr → etl` | 旧 qf 转换已并入 etl 阶段（见 [`etl/qwenformat/transform`](etl/qwenformat/transform.py)） |
| 4 | 处理顺序 | simulation server → gdr → etl | 单 task 严格串行 |
| 5 | 并行语义 | 任务级并行 + 阶段内顺序 | 同一 task 内 simulate→gdr→etl 严格串行；不同 task 可任意阶段重叠 |
| 6 | 并行实现 | `multiprocessing.Pool` 子进程池 | 避免 GIL；子进程间唯一共享资源是 SQLite（自身 serializes 写） |
| 7 | 默认并行度 | `max_parallelism=1`（严格串行） | 与原始需求对齐；≥2 启用子进程并行 |
| 8 | 任务清单 | `--all-tasks`（默认）/ `--tasks T1,T2` 过滤 | 不再走 batch 切分 |
| 9 | 兼容性 | **不考虑** | 原 batch / watcher / 多 worker / 中间态 / 旧配置字段一律重写或删除 |
| 10 | 中间件队列 | 沿用 SQLite 单文件 | 事务安全，无需外部依赖 |
| 11 | 失败重试 | 单 task 内 gdr/etl 阶段重试 max_retry 次；超限入 `dead` | 重试由子进程内 `_safe_run_gdr` / `_safe_run_etl` 实现 |
| 12 | 死信恢复 | `python -m orchestration replay` | 把 `phase=dead` 全部重置为 `phase=pending` |
| 13 | 生命周期 | daemon 服务（`start` / `status` / `stop` / `replay` CLI） | PID file + STOP 哨兵文件（Windows 不可达 SIGBREAK） |
| 14 | 汇聚产出 | 不要 | 每个 trajectory 一份 4 视图，不聚合 |

## 3. 完整数据流

```text
┌──────────────────────────────────────────────────────────────────────┐
│ master.run(task_ids)                                                   │
│   └─> PipelineExecutor.run(task_ids)                                  │
│         ├─ Pool(processes=max_parallelism, init=_worker_init(paths))  │
│         │   │                                                         │
│         │   ├─> apply_async(_run_one_task_pipeline, (task_id, ...))   │
│         │   │     │                                                   │
│         │   │     ├─ 1. SQLiteQueue(paths.sqlite_db)                  │
│         │   │     ├─ 2. upsert_task(task_id)                          │
│         │   │     ├─ 3. mark_phase(simulate)                          │
│         │   │     ├─ 4. producer_simulate.run_one_task(task_id)       │
│         │   │     │      └─> simulate_serve BatchRunner.run([task])   │
│         │   │     │            └─> out/agent_trajectory/<run>__<sess>.json │
│         │   │     ├─ 5. mark_phase(gdr, run_id, session_id, src_path)│
│         │   │     ├─ 6. run_gdr_once(src_path, refined_dir, ...)      │
│         │   │     │      └─> gdr/pipeline/runner._process_one_file    │
│         │   │     │            └─> out/refined/<T>__<sid>.json (C2)  │
│         │   │     ├─ 7. mark_phase(etl, gdr_refined_path)             │
│         │   │     ├─ 8. run_etl_once(c2_path, etl_outputs_dir, ...)   │
│         │   │     │      └─> etl/parsers.load_refined_session         │
│         │   │     │            └─> gdr/domain/save_session_v2         │
│         │   │     │                  └─> out/refine_data/<T>__<sid>_refined.{messages,openai,qwenjina.txt,meta}.json │
│         │   │     ├─ 9. mark_phase(done, etl_*_path=...)              │
│         │   │     └─ 10. return {"phase": "done", "stage": "done"}    │
│         │   │                                                         │
│         │   └─> in_flight: dict[AsyncResult, str] 槽位填充            │
│         │         ├─> future.ready() 时 future.get() 取结果           │
│         │         ├─> result["phase"]=="done" → done++                │
│         │         └─> result["phase"]=="dead" / 异常 → dead++        │
│         └─> return PipelineSummary(total, done, dead, duration)       │
└──────────────────────────────────────────────────────────────────────┘
```

跨进程共享：唯一资源是 SQLite（`output/orchestration/orchestration.db`）；SQLite 自身 serializes 写，多子进程并发安全。文件系统产物各子进程独立写。

### 3.1 横切观测层（Langfuse，可选）

三阶段产生 3 个相互独立的 trace，由同一个 `session_id` 串联（**不强制父子跨进程**）：

```text
┌──────────────────────────────────────────────────────────────────────┐
│ trace 1: simulate_serve:<task_id>                                     │
│   └─ stage_trace (input=None, output=C1 trajectory dict)             │
│       └─ 触发:trajectory_archiver.archive() 的 finally 块             │
│                                                                      │
│ trace 2: gdr.process_one                                              │
│   └─ 25 子 span(21 step + 3 reassemble generation + 1 judge gen)     │
│       └─ 触发:gdr/pipeline/runner.py::process_one body 起             │
│                                                                      │
│ trace 3: etl:<task_id>                                                │
│   └─ 2 子 span(etl.load_refined_session + etl.save_c3_4views)        │
│       └─ 触发:orchestration/workers/etl_worker.py::run_etl_once      │
└──────────────────────────────────────────────────────────────────────┘
```

默认关闭:`langfuse.enabled=false` 时工厂 `get_client()` 返回 None,业务零侵入;启用时由 `orchestration.task_pipeline._run_one_task_pipeline` 入口一次性 `load_langfuse_config()` + `get_client(cfg)` 装配 client,子阶段 `run_gdr_once` / `run_etl_once` 接收 `langfuse_cfg` / `langfuse_client` 双轨参数。`multiprocessing.Pool` 子进程 fork 后 SDK 不 fork-safe(socket 失效),`_worker_init` 调用 `_reset_for_fork()` 清旧 `_client`,子进程下次 `get_client()` 重新 init 一份(主进程 / 子进程各持一份);atexit `_lf_shutdown`(仅当 enabled)确保退出前 flush。详见 §6.6 与用户视角 [`docs/observability-langfuse.md`](observability-langfuse.md)。

## 4. 模块划分

| 模块 | 角色 | 文件 |
|---|---|---|
| `orchestration/__main__.py` | CLI 入口：`start` / `status` / `stop` / `replay` 四个子命令 | [`orchestration/__main__.py`](../orchestration/__main__.py) |
| `orchestration.master` | `Master` 类：持有 cfg / queue / stop_event；`run()` 调 PipelineExecutor，`shutdown()` set stop_event，`status()` 走 `count_by_phase()` | [`orchestration/master.py`](../orchestration/master.py) |
| `orchestration.pipeline_executor` | `PipelineExecutor`：起 `multiprocessing.Pool`，维护 `in_flight: dict[AsyncResult, str]` 槽位填充 | [`orchestration/pipeline_executor.py`](../orchestration/pipeline_executor.py) |
| `orchestration.task_pipeline` | `_worker_init`(fork-safe:`_reset_for_fork()` + atexit `_lf_shutdown`)+ `_run_one_task_pipeline`:子进程入口,完整跑 simulate → gdr → etl + Langfuse client 装配(`load_langfuse_config` + `get_client`)+ `_safe_run_gdr` / `_safe_run_etl` 双轨传 `langfuse_cfg` / `langfuse_client` | [`orchestration/task_pipeline.py`](../orchestration/task_pipeline.py) |
| `orchestration.settings` | `PipelineSettings` + `Paths`（frozen dataclass） | [`orchestration/settings.py`](../orchestration/settings.py) |
| `orchestration.config_loader` | `load_config`：从 YAML 解析为 `OrchestrationConfig` | [`orchestration/config_loader.py`](../orchestration/config_loader.py) |
| `orchestration.queue.sqlite_queue` | `SQLiteQueue`：upsert_task / mark_phase / mark_failed / requeue_dead / list_tasks / count_by_phase | [`orchestration/queue/sqlite_queue.py`](../orchestration/queue/sqlite_queue.py) |
| `orchestration.producer_simulate` | `run_one_task(task_id)`：simulate_serve in-process 单 task 入口 | [`orchestration/producer_simulate.py`](../orchestration/producer_simulate.py) |
| `orchestration.workers.gdr_worker` | `run_gdr_once(src_path, refined_dir, ...)`：C1 trajectory → C2 refined Session | [`orchestration/workers/gdr_worker.py`](../orchestration/workers/gdr_worker.py) |
| `orchestration.workers.etl_worker` | `run_etl_once(c2_path, etl_outputs_dir, ...)`：C2 → C3 4 视图 | [`orchestration/workers/etl_worker.py`](../orchestration/workers/etl_worker.py) |
| `orchestration.workers.base_worker` | `_output_filename(task_id, session_id, suffix)`：产物文件名工具 | [`orchestration/workers/base_worker.py`](../orchestration/workers/base_worker.py) |
| `orchestration.failure_handler` | `reap_dead(queue, dead_dir, log_path)`：把 `phase=dead` 任务产物归档到 `dead/` 并追加 `dead.log` | [`orchestration/failure_handler.py`](../orchestration/failure_handler.py) |
| `orchestration.health` | `collect_tasks(queue)` + `write_health(queue, log_dir)`：phase 分布 + `health.json` 落盘 | [`orchestration/health.py`](../orchestration/health.py) |
| `orchestration.daemon` | `start_detached` / `start_foreground` / `daemon_stop` + PID file + STOP 哨兵文件 + 日志重定向 | [`orchestration/daemon.py`](../orchestration/daemon.py) |
| `orchestration.errors` | `OrchestrationError` 等异常类型 | [`orchestration/errors.py`](../orchestration/errors.py) |

**删除**（2026-09-22 起）：
- `orchestration/batch_tracker.py` —— 批次追踪概念删除
- `orchestration/watcher.py` —— trajectory watcher 删除，子进程内直接拼 `src_path`
- `orchestration/workers/qf_worker.py` —— qf 中间阶段删除
- `gdr/run.bat` —— 移交 orchestration 顶层后无意义

## 5. SQLite schema

```sql
-- orchestration/queue/schema.sql
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

**删除**（与旧批次概念不再兼容）：
- `batches` 表（旧批次元数据）
- `run_tasks` 表（旧 batch → task 映射）
- `tasks.batch_id` / `locked_by` / `locked_at` 列
- `tasks.state` 中间态（`gdr_processing` / `etl_processing`）

### Phase 常量

```python
# orchestration/queue/__init__.py
PHASE_PENDING    = "pending"
PHASE_SIMULATE   = "simulate"
PHASE_GDR        = "gdr"
PHASE_ETL        = "etl"
PHASE_DONE       = "done"
PHASE_DEAD       = "dead"

ALL_PHASES = frozenset({
    PHASE_PENDING, PHASE_SIMULATE, PHASE_GDR,
    PHASE_ETL, PHASE_DONE, PHASE_DEAD,
})
TERMINAL_PHASES = frozenset({PHASE_DONE, PHASE_DEAD})
```

详见 [`docs/设计方案/pipeline-contracts.md` §2](设计方案/pipeline-contracts.md)。

## 6. 关键算法

### 6.1 `PipelineExecutor.run(task_ids)` 主循环

[`orchestration/pipeline_executor.py::_dispatch`](../orchestration/pipeline_executor.py) 维护
`in_flight: dict[AsyncResult, str]`；槽位空时从 pending 取 task 投递；future.ready()
就取结果统计 done/dead；所有 task 跑完返回 `PipelineSummary`。

```python
def run(self, task_ids: list[str]) -> PipelineSummary:
    parallelism = max(1, int(self._settings.max_parallelism))
    self._process_pool = multiprocessing.Pool(
        processes=parallelism,
        initializer=_worker_init,
        initargs=(self._paths,),
    )
    try:
        return self._dispatch(task_ids)
    finally:
        self._process_pool.close()
        self._process_pool.join()
```

`_dispatch` 槽位填充循环：

```python
in_flight: dict[AsyncResult, str] = {}
pending: list[str] = list(task_ids)
done_count = 0
dead_count = 0

def _fill_slots() -> None:
    while (len(in_flight) < self._settings.max_parallelism and pending):
        task_id = pending.pop(0)
        try:
            self._queue.upsert_task(task_id)         # 兜底:已 terminal → 计入 done
        except TaskAlreadyTerminal as exc:
            done_count += 1
            continue
        future = self._process_pool.apply_async(
            _run_one_task_pipeline,
            (task_id, self._paths, self._gdr_settings, self._settings),
        )
        in_flight[future] = task_id

_fill_slots()
while in_flight:
    for future in list(in_flight):
        if not future.ready():
            continue
        task_id = in_flight.pop(future)
        try:
            result = future.get(timeout=0)
        except Exception as exc:                       # 子进程崩溃兜底
            dead_count += 1
            self._queue.mark_failed(
                task_id, stage="simulate",
                error_msg=f"pipeline crashed: {type(exc).__name__}: {exc}",
            )
            continue
        if isinstance(result, dict) and result.get("phase") == "done":
            done_count += 1
        else:
            dead_count += 1
    _fill_slots()
    if in_flight:
        time.sleep(0.05)                              # 短睡避免忙等
```

**关键点**：
- 子进程跑一个完整 task（simulate → gdr → etl），不跨进程；单 task 内三阶段不需要跨进程通信。
- 子进程间唯一共享资源是 SQLite（自身 serializes 写）。
- `max_parallelism=N` 等价于 N 个子进程同时跑。
- `future.get()` 抛 `Exception` = 子进程崩溃；主进程捕获后 `dead++` + 兜底 `mark_failed(stage="simulate")`，**继续下一个**。
- 子进程不响应 stop_event（跑完一个 task 自然退出）；master 主线程 `shutdown()` 仅 set stop_event 提前退出 wait loop。

### 6.2 `_run_one_task_pipeline(task_id)` 子进程入口

[`orchestration/task_pipeline.py::_run_one_task_pipeline`](../orchestration/task_pipeline.py)
在子进程内完整跑单个 task 的三阶段：

```python
def _run_one_task_pipeline(
    task_id: str,
    paths: Paths,
    gdr_settings: GdrSettings,
    orchestration_settings: PipelineSettings,
) -> dict:
    """返回 {"task_id": str, "phase": "done"|"dead", "stage": str, "error": str|None}."""
    queue = SQLiteQueue(paths.sqlite_db)
    try:
        # 1. 兜底 upsert — 若已被另一个 worker 标 done, 直接返 done
        try:
            queue.upsert_task(task_id)
        except TaskAlreadyTerminal:
            existing = queue.get_task(task_id)
            if existing is not None and existing.phase == PHASE_DONE:
                return {"task_id": task_id, "phase": "done", "stage": "done"}
            return {"task_id": task_id, "phase": "dead", "stage": "unknown",
                    "error": "already dead in another worker"}

        # 2. 标记 simulate 阶段开始
        queue.mark_phase(task_id, new_phase=PHASE_SIMULATE)

        # 3. simulate 阶段 (in-process)
        run = run_one_task(task_id, config_path=paths.simulate_serve_config)
        if run.state ∈ TERMINAL_FAIL_STATES:
            queue.mark_failed(task_id, stage="simulate", error_msg=...)
            return {"task_id": task_id, "phase": "dead", "stage": "simulate"}

        # 4. 拼 src_path = trajectory_dir / <safe_run>__<safe_session>.json
        src_path = paths.trajectory_dir / f"{safe_run}__{safe_session}.json"
        if not src_path.is_file():
            # fallback: 扫 dir 找匹配 session 的 .json
            ...

        # 5. 标记 gdr 阶段 + 写 src_path / run_id / session_id
        queue.mark_phase(task_id, new_phase=PHASE_GDR,
                         run_id=run_id, session_id=session_id, src_path=src_path)

        # 6. gdr 重试循环 (max_retry_gdr 次)
        refined_path = _safe_run_gdr(
            task_id=task_id, src_path=src_path, refined_dir=paths.refined_dir,
            session_id=session_id, gdr_settings=gdr_settings,
            max_retry=orchestration_settings.max_retry_gdr, queue=queue,
        )
        if refined_path is None:
            return {"task_id": task_id, "phase": "dead", "stage": "gdr",
                    "error": "gdr failed"}

        # 7. 标记 etl 阶段 + 写 gdr_refined_path
        queue.mark_phase(task_id, new_phase=PHASE_ETL, gdr_refined_path=refined_path)

        # 8. etl 重试循环 (max_retry_etl 次)
        etl_outputs = _safe_run_etl(
            task_id=task_id, c2_path=refined_path,
            etl_outputs_dir=paths.etl_outputs_dir, session_id=session_id,
            max_retry=orchestration_settings.max_retry_etl, queue=queue,
        )
        if etl_outputs is None:
            return {"task_id": task_id, "phase": "dead", "stage": "etl",
                    "error": "etl failed"}

        # 9. 标记 done + 写 etl 输出路径
        messages_path, openai_path, qwenjina_path, meta_path = etl_outputs
        queue.mark_phase(task_id, new_phase=PHASE_DONE,
                         etl_messages_path=messages_path,
                         etl_openai_path=openai_path,
                         etl_qwenjina_path=qwenjina_path,
                         etl_meta_path=meta_path)
        return {"task_id": task_id, "phase": "done", "stage": "done"}

    except Exception as exc:
        # 顶层兜底: 任何未捕获异常都 mark_failed, 不抛给主进程
        tb = traceback.format_exc(limit=4)
        msg = f"{type(exc).__name__}: {exc}\n{tb}"
        _mark_dead(queue, task_id, stage=result.get("stage", "unknown"),
                   error_msg=f"pipeline crashed: {msg}")
        return {"task_id": task_id, "phase": "dead", "stage": result.get("stage"),
                "error": msg}
```

**关键约束**：
- 子进程入口必须是模块顶层函数（`multiprocessing.Pool.apply_async` 需要 picklable）。
- 不传 `SQLiteQueue` 实例（sqlite3 connection 不可 pickle）；子进程内 `SQLiteQueue(paths.sqlite_db)` 重建。
- 入参全部是可 pickle 的浅 dataclass（`Paths` / `PipelineSettings` / `GdrSettings`）。
- 业务模块（`producer_simulate` / `workers.gdr_worker` / `workers.etl_worker`）在子进程内延迟 import，避免父进程触发不必要的初始化。

### 6.3 子进程约束

| 约束 | 原因 |
|---|---|
| 子进程入口必须是模块顶层函数 | `multiprocessing.Pool.apply_async` 需要 picklable |
| 不传 `SQLiteQueue` 实例 | SQLite connection 不可 pickle |
| 不传 `TaskRuntime` 实例 | 不可 pickle |
| 配置对象必须可 pickle | 浅 dataclass 即可，深对象不可 pickle |
| 日志 handler 在子进程内重新初始化 | `logging` 模块多进程不安全，子进程用默认 stderr |

### 6.4 （删除）watcher 章节

旧版的 `orchestration/watcher.py`（轮询 `output/agent_trajectory/` 把 trajectory 入 SQLite
队列）已于 2026-09-22 删除。新架构下：
- simulate_serve 不再"先落 trajectory + 后置 watcher 入队"，而是在子进程内
  `_run_one_task_pipeline` 步骤 4 直接拼 `src_path = trajectory_dir / <safe_run>__<safe_session>.json`。
- 子进程之间无 SQLite 队列争夺 simulate→gdr 边界；唯一共享资源是 SQLite 状态机
  （`tasks` 表）。
- 详见 [`docs/设计方案/pipeline-serial-parallel-refactor.md` §2](设计方案/pipeline-serial-parallel-refactor.md) 设计决策汇总。

### 6.5 （删除）batch_tracker 章节

旧版的 `orchestration/batch_tracker.py`（`run.json.state ∈ TERMINAL_STATES` 凑批等待）已于
2026-09-22 整文件删除。新架构下：
- `producer_simulate.run_one_task` 是同步函数，模拟完成后直接返回 `TaskRun`；
- `_run_one_task_pipeline` 在子进程内同步阻塞，跑完直接 `mark_phase(gdr)`。
- 无跨批次凑批等待逻辑。

### 6.6 Langfuse 横切观测层接入点（2026-09-23，PR 1–6）

Langfuse 集成是**横切观测层**，**不**改变三阶段数据契约（`simulation server → gdr → etl` 的 C1/C2/C3 文件格式不变），仅在三阶段的关键节点开 trace 供 UI 对比观察。

#### 6.6.1 装配点（`_run_one_task_pipeline` 入口）

子进程入口一次性 `load_langfuse_config()` + `get_client(cfg)`，仅当 `enabled=True` 构造 client，否则 `langfuse_client=None`，所有下游 `_safe_run_gdr` / `_safe_run_etl` 拿到 `None` 时业务零侵入：

```python
langfuse_cfg = load_langfuse_config()  # 从根 config/config.yaml 的 langfuse: 段
langfuse_client = get_client(langfuse_cfg) if langfuse_cfg.enabled else None
refined_path = _safe_run_gdr(
    task_id=task_id, src_path=src_path, ..., langfuse_cfg=langfuse_cfg, langfuse_client=langfuse_client,
)
etl_outputs = _safe_run_etl(
    task_id=task_id, c2_path=refined_path, ..., langfuse_cfg=langfuse_cfg, langfuse_client=langfuse_client,
)
```

子阶段 worker（`run_gdr_once` / `run_etl_once`）接收双轨参数，**优先级**：显式 `langfuse_client` > 显式 `langfuse_cfg` > 旧 `gdr_settings` / `etl_settings` 兜底。这样 PR 5 引入的 orchestration 入口不影响 PR 3 阶段直接调 worker 的旧路径（gdr 进程内 `pipeline/runner.py::_process_one_file` 仍走 `gdr.config.settings.langfuse_*` flat 字段）。

#### 6.6.2 fork-safe 客户端（`_worker_init`）

`multiprocessing.Pool` 默认 fork 模型下，主进程的 Langfuse SDK client（含 HTTPS socket / 内部 queue）在子进程里**不可用**——`file descriptor` 失效导致 `ConnectionError` / `Bad file descriptor`。

[`orchestration/task_pipeline.py::_worker_init`](../orchestration/task_pipeline.py) 在 `Pool(initializer=_worker_init)` 触发时：

1. 调工厂 `_reset_for_fork()` 把主进程 `_client` 单例清空（idempotent）；
2. 当 `langfuse_cfg.enabled=True` 时 `atexit.register(_lf_shutdown)` 兜底退出前 `client.flush()`，避免 SIGKILL/OOM 丢批次。

子进程下次 `get_client(cfg)` 触发重新 init 一份（主进程 / 子进程各持一份，互不污染）。

**注**：`Pool` 必须显式传 `initializer=_worker_init, initargs=(paths,)`，不能只传 `processes=N`。旧版只传 `processes=N` 会跳过 worker init → `_reset_for_fork()` 不被调用 → 偶发 `ConnectionError` 但 trace 仍能写出（业务不挂，观测丢数据）。

#### 6.6.3 三阶段 trace 名与签出位置

| 阶段 | Trace 名 | 触发点 | 子 span |
|---|---|---|---|
| simulate_serve | `simulate_serve:<task_id>` | `trajectory_archiver.archive()` finally 块（每 turn 调用一次，最终覆盖到终态） | 0（一个 stage trace） |
| gdr | `gdr.process_one`（在 `pipeline/runner.py::process_one` body 起；`run_gdr_once` 不再起 outer） | 同左 | 25 个（21 step + 3 reassemble generation + 1 retry_loop_clip judge generation） |
| etl | `etl:<task_id>`（`stage_trace` 外层；`metadata.attempt:N` 标记重试） | `etl_worker.run_etl_once` body | 2 个（`etl.load_refined_session` + `etl.save_c3_4views`） |

**session_id 串联三阶段**：simulate_serve 用 `trajectory_archiver.set_run_context(run_ctx)` 注入 `session_id = run.remote_session_id`；gdr 从 C1 → C2 保留 `Session.session_id`；etl 入口用 `run_etl_once(session_id=...)` 入参。三阶段必须用**同一个** `session_id`，否则 Langfuse UI 看到 3 个独立 trace 不串起来。`_run_one_task_pipeline` 函数体内 step 5/7 把 `session_id` 传下去，**不要**在 worker 内重派生。

#### 6.6.4 payload 模式与 fork-safe 关闭

`upload_payload: full | summary | none` 三态（默认 full）。`max_payload_bytes` 触发降级：单 span payload 超过阈值时降级到 summary（仅保留 keys + byte 数），避免大 session（1000+ block）OOM。

`enabled=false` 时：

- 工厂 `get_client()` 返回 None，`step_span` / `stage_trace` 都是空 no-op；
- `_worker_init` 不会注册 atexit；
- 业务路径零侵入（已验证 678 passed + 3 skipped with `enabled=false`）。

**基线**：三阶段数据契约（C1/C2/C3）字段、文件路径、SSE 事件流、QwenPaw 交互协议 — 全部不受 Langfuse 影响。`output/` 的脱敏策略继续适用；Langfuse 端是独立项目，独立数据流。

详见用户视角 [`docs/observability-langfuse.md`](observability-langfuse.md)（启用 / 关闭 / 字段白名单 / 故障排查 / 采样建议）· 设计基线 [`docs/observability-langfuse-plan.md`](observability-langfuse-plan.md)（13 字段 schema + 风险与回退）· 模块级实施参考 [`docs/langfuse-simulate-server.md`](langfuse-simulate-server.md) · [`docs/langfuse-gdr.md`](langfuse-gdr.md) · [`docs/langfuse-etl.md`](langfuse-etl.md)。

### 6.7 Windows 控制台窗口抑制（2026-09-23）

**问题**：pytest / IDE 测试运行器在 Windows 上跑 `multiprocessing.Pool` worker 或 `daemon.start_detached` 子进程时，会弹一个 cmd 终端窗口挤占桌面。

**根因（实测 CPython 3.12）**：

1. `Lib/multiprocessing/popen_spawn_win32.py:77` 调 `_winapi.CreateProcess(python_exe, cmd, ..., 0, ...)`，`creationflags=0` 即默认行为 → `python.exe`（console application）会创建新控制台窗口。CPython 内部**未**硬编码 `CREATE_NO_WINDOW`（与早期文档认知不符）。
2. `daemon.start_detached` 调 `subprocess.Popen(argv, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)`。`DETACHED_PROCESS` 仅让子进程脱离父控制台，但 `python.exe` 仍创建**新**控制台窗口。

**修复**：幂等的 [`orchestration/_windows.py`](../orchestration/_windows.py) `install_no_window_policy()`：

| 路径 | 措施 | 触发时机 |
|---|---|---|
| `multiprocessing.Pool` worker | monkey-patch `_winapi.CreateProcess`，对 cmd 含 `--multiprocessing-fork` 指纹（CPython 3.12 spawn worker 唯一标记）的调用，把 `dwCreationFlags=0` 补成 `CREATE_NO_WINDOW`（`0x08000000`） | `orchestration.pipeline_executor` module 加载时调一次 |
| `daemon.start_detached` | `subprocess.Popen` 的 `creationflags` 显式 OR 上 `CREATE_NO_WINDOW` | `orchestration.daemon` module 加载时调一次 |
| pytest 自身 | 同 `install_no_window_policy()` 自动安装；防止 pytest 自身启动的子进程弹窗 | `tests/conftest.py` 顶部 import 时调一次（兜底） |

**关键不变量**：

- `install_no_window_policy()` **幂等**：首次 patch，后续 no-op；非 Windows 平台标记"已处理"也是 no-op。
- **不覆盖**调用方已显式传入的非零 `dwCreationFlags`（尊重意图，例如 `daemon.start_detached` 的 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` 不会被覆盖——它们在自己配置时已 OR 上 `CREATE_NO_WINDOW`）。
- **指纹匹配**精确到 `--multiprocessing-fork`：其他 `_winapi.CreateProcess` 用途（非 multiprocessing spawn）不受影响。

**验证**（`tests/orchestration/test_no_window_policy.py`，7 项全过）：

- `test_install_is_idempotent`：多次调不重复 patch
- `test_createprocess_gets_wrapped_marker`：Windows 上 `_winapi.CreateProcess` 有 `_no_window_wrapped` 标记
- `test_wrap_with_mp_fork_injects_create_no_window`：mp fork 指纹 → 补 `CREATE_NO_WINDOW`
- `test_wrap_with_explicit_nonzero_flags_respects_caller`：调用方已传 `0x00000008` → 不覆盖
- `test_wrap_without_mp_fork_does_not_inject`：无指纹 → 不动
- `test_mp_pool_worker_creationflags_have_no_window`：真实 `multiprocessing.Pool(1)` worker 触发 wrap，flags 含 `CREATE_NO_WINDOW`
- `test_daemon_start_detached_creationflags_have_no_window`：mock Popen 验证 `daemon.start_detached` 传入的 `creationflags` 包含 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`

**非 Windows 平台**：`_winapi.CreateProcess` 不存在，`install_no_window_policy()` 直接标记 `_INSTALLED=True` 返回 False（no-op）；不抛错。

## 7. 配置（`config/config.yaml`）

```yaml
orchestration:
  pipeline:
    max_parallelism: 1            # 默认串行; >1 启用 multiprocessing.Pool 并发
    max_retry_gdr: 3
    max_retry_etl: 3
    retry_poll_seconds: 2.0
  # 注: simulate_serve_config: 默认指向根配置文件本身
  # (gdr_settings 从 gdr.config.settings 自动加载)

paths:
  simulate_serve_config: config/config.yaml
  trajectory_dir: output/agent_trajectory
  runs_dir: output/runs
  refined_dir: output/refined
  etl_outputs_dir: output/refine_data
  sqlite_db: output/orchestration/orchestration.db
  dead_dir: output/orchestration/dead
  pid_file: output/orchestration/orchestration.pid
  log_dir: output/orchestration/logs
```

**删除字段**（与旧批次概念不再兼容）：
- `orchestration.batch_size` / `gdr_workers` / `qf_workers` / `gdr_wait_seconds`
- `orchestration.max_retry_qf` / `watcher_poll_seconds` / `reap_stale_seconds`
- `orchestration.worker_idle_backoff_max_seconds` / `watcher_idle_backoff_max_seconds`
- `orchestration.batch_drain_poll_seconds` / `batch_drain_timeout_seconds`
- `orchestration.reap_stale_interval_seconds`
- `paths.qf_output_dir` / `paths.gdr_output_dir`

详细契约见 [`docs/设计方案/pipeline-contracts.md` §1](设计方案/pipeline-contracts.md)
与 [`config/config.example.yaml`](../config/config.example.yaml)。

## 8. CLI 行为

```powershell
# 启动 (前台, 默认 max_parallelism=1 严格串行)
python -m orchestration start
# 启动 + 4 子进程并行
python -m orchestration start --parallelism 4
# 启动 + 子集过滤
python -m orchestration start --tasks T001,T002 --parallelism 2
# 启动 + 拉全 catalog
python -m orchestration start --all-tasks --parallelism 4
# 启动 + 后台 (detach)
python -m orchestration start --detach --all-tasks --parallelism 4
# 启动 + dry-run (只打印计划)
python -m orchestration start --all-tasks --dry-run --parallelism 1
# 启动 + task 跑完常驻
python -m orchestration start --all-tasks --stay

# 状态 (队列 6 phase 计数 + 最近 10 task)
python -m orchestration status

# 优雅停止 (STOP 哨兵 + 超时强杀)
python -m orchestration stop --timeout 15

# 重放 phase=dead 的 task
python -m orchestration replay
```

**删除**（2026-09-22 起）：
- `--batch-size N`（旧批次大小；新架构无 batch 概念，按 `--parallelism N` 直接并发 task）。
- `--exit-when-done`（已废弃；task 跑完即退现在是默认行为，保留仅为兼容旧命令）。
- `replay --batch N`（新架构无 batch 概念）。

详细契约见 [`docs/设计方案/pipeline-contracts.md` §7](设计方案/pipeline-contracts.md)
与 [`orchestration/__main__.py`](../orchestration/__main__.py)。

### 8.1 `simulate_serve/__main__.py` 保留的只读开关

新架构下 `simulate_serve` 不再承担任务运行入口（移交 `orchestration`），仅保留只读开关：

```powershell
python -m simulate_serve --validate-config   # 校验 catalog
python -m simulate_serve --check-tools       # 检查工具 + health
python -m simulate_serve --readiness         # readiness 汇总
python -m simulate_serve --list-interrupted  # 列出 INTERRUPTED 的 run
```

**删除**（2026-09-22 起）：`--tasks` / `--rerun-task` / `--limit` / `--include-offline` /
`--max-run-retries`。

## 9. 失败语义与重试

| 阶段 | 失败信号 | 子进程应对 |
|---|---|---|
| simulate | `run.state ∉ TERMINAL_STATES`（如 `VALIDATION_ERROR` / `EXECUTOR_ERROR` / `ACTOR_ERROR` / `CANCELLED` / `INTERRUPTED` / `COMPLETION_INCOMPLETE` / `INCONCLUSIVE` / `GUIDE_EXHAUSTED`） | `mark_failed(stage="simulate", error_msg=...)` + 返回 `{"phase": "dead"}` |
| simulate | `KeyError`（task_id 不在 catalog） | `mark_failed(stage="simulate", error_msg="[non-retryable] KeyError: ...")` + 返回 `{"phase": "dead"}` |
| simulate | 子进程抛任意 `Exception` | 顶层 `try/except` 兜底 → `mark_failed(stage="simulate")` + 返回 `{"phase": "dead"}` |
| gdr | `GdrNonRetryableError`（trajectory 不合法 / schema 不匹配 / 远程 LLM 永久错误） | `mark_failed(stage="gdr", error_msg="[non-retryable] ...")` + 返回 `{"phase": "dead"}`（不重试） |
| gdr | 任意 `Exception`（LLM 调用失败 / 临时 IO 错误） | `increment_attempts(stage="gdr")`；重试 max_retry_gdr 次；超限 → `mark_failed(stage="gdr")` + 返回 `{"phase": "dead"}` |
| etl | `EtlNonRetryableError`（C2 schema 不匹配 / load 失败） | `mark_failed(stage="etl", error_msg="[non-retryable] ...")` + 返回 `{"phase": "dead"}` |
| etl | 任意 `Exception`（临时 IO 错误） | `increment_attempts(stage="etl")`；重试 max_retry_etl 次；超限 → `mark_failed(stage="etl")` + 返回 `{"phase": "dead"}` |
| 子进程崩溃 | 子进程异常退出（`future.get()` 抛 `Exception`） | 主进程捕获 → `dead++` + 兜底 `mark_failed(stage="simulate", error_msg="pipeline crashed: ...")`，**继续下一个** |

**子进程不抛异常给主进程**：所有未捕获异常都在子进程顶层 try/except 兜底 → `mark_failed` + 返回 `{"phase": "dead"}`。主进程只通过 `future.get()` 拿到 dict 结果（或异常），按 `phase` 计入 `done/dead`。

**死信恢复**：用 `python -m orchestration replay` 把所有 `phase=dead` 的 task 全部重置为 `phase=pending` 重新入队（无需重启 master；新调 `python -m orchestration start --tasks T1,T002` 即可）。

## 10. 验收测试要点

### 10.1 单 task smoke

```powershell
python -m orchestration start --tasks T001 --parallelism 1
```

| 期望产物 | 期望值 |
|---|---|
| `output/runs/<run_id>/run.json` | 落盘 |
| `output/agent_trajectory/<run_id>__<session_id>.json` | 落盘（C1 契约） |
| `output/refined/<T001>__<session_id>.json` | 落盘（C2 契约，`schema_version: refined_session.v1`） |
| `output/refine_data/<T001>__<session_id>_refined.{messages,openai,qwenjina.txt,meta}.json` | 4 视图全落盘（C3 契约） |
| SQLite `tasks` 表 phase=done 行数 | = 1 |
| SQLite `tasks` 表 phase=dead 行数 | = 0 |
| `output/orchestration/logs/health.json` | 含 `status="completed"`, `summary={total:1, done:1, dead:0}` |

### 10.2 3 task 串行 smoke

```powershell
python -m orchestration start --tasks T001,T002,T003 --parallelism 1
```

| 期望产物 | 期望值 |
|---|---|
| C2 refined JSON 数量 | 3 份 |
| C3 4 视图文件数量 | 3 × 4 = 12 份 |
| SQLite phase=done 行数 | = 3 |

### 10.3 3 task 并行 smoke

```powershell
python -m orchestration start --tasks T001,T002,T003 --parallelism 4
```

| 期望产物 | 期望值 |
|---|---|
| C2 refined JSON 数量 | 3 份 |
| C3 4 视图文件数量 | 3 × 4 = 12 份 |
| SQLite phase=done 行数 | = 3 |
| 性能 | 并行耗时 ≤ 串行耗时（视 LLM 调用并行度而定） |

### 10.4 小批量串行 smoke（≤5 task）

```powershell
python -m orchestration start --tasks T001,T002,T003,T004,T005 --parallelism 1
```

| 期望 | 失败率 ≤ 5%（若有失败，记录 task_id + stage + error_msg） |
|---|---|

### 10.5 全 catalog 并行 smoke

```powershell
python -m orchestration start --all-tasks --parallelism 4
```

| 警告 | 98 task × 4 并行 = 大量产物；需要磁盘空间 + LLM 调用配额 |
|---|---|

如实际跑不动（磁盘/配额），降级跑 catalog 子集并记录到
[`docs/设计方案/agent-smoke_design_adjustments.md`](设计方案/agent-smoke_design_adjustments.md)。

### 10.6 集成 verify（由 agent-verify 组负责）

- `python -m orchestration status` 打印 6 个 phase key 全在 + 计数正确
- 死信归档：构造 gdr 失败的 task，验证 `output/orchestration/dead/` 有归档文件 + `dead.log` 多一行 + phase=dead 计数 +1
- replay 复活：调 `python -m orchestration replay`，验证 SQLite phase=dead → pending + error_msg 清空 + 再次 `start --tasks Txxx` 能正常走完三阶段
- 详见 [`tests/orchestration/test_integration_smoke.py`](../tests/orchestration/test_integration_smoke.py)

## 11. 与现有文档的关系

- **不重复**：[`docs/gdr-context-understanding-and-policy.md`](gdr-context-understanding-and-policy.md) /
  [`docs/gdr-module-functional-overview.md`](gdr-module-functional-overview.md)
  描述 gdr 内部策略与模块结构；本设计文档描述 simulate_serve 之上的**跨子系统流水线**，
  层级更高。
- **不冲突**：[`CLAUDE.md`](../CLAUDE.md) 的"Pipeline 流程"段定义了 simulation server →
  gdr → etl 三阶段的契约骨架；本设计文档是该骨架的 orchestration 侧实施细节。
- **补充**：[`docs/contracts/C1-trajectory-events.md`](contracts/C1-trajectory-events.md) /
  [`docs/contracts/C2-refined-session.md`](contracts/C2-refined-session.md) /
  [`docs/contracts/C3-final-sft-views.md`](contracts/C3-final-sft-views.md) 定义三阶段的字段级
  schema；本设计文档描述 orchestration 如何把它们串起来。
- **设计基线**：[`docs/设计方案/pipeline-serial-parallel-refactor.md`](设计方案/pipeline-serial-parallel-refactor.md) /
  [`docs/设计方案/pipeline-contracts.md`](设计方案/pipeline-contracts.md) /
  [`docs/contracts/migration-plan.md`](../contracts/migration-plan.md) 是本次重写的方案、契约与实施汇总。
- **横切观测（Langfuse，2026-09-23）**：[`docs/observability-langfuse.md`](observability-langfuse.md)（用户视角总览：启用 / 关闭 / 字段白名单 / 各阶段 span 名 / 故障排查 / 采样建议）/ [`docs/observability-langfuse-plan.md`](observability-langfuse-plan.md)（设计基线：13 字段 schema + 风险与回退）/ [`docs/langfuse-simulate-server.md`](langfuse-simulate-server.md) · [`docs/langfuse-gdr.md`](langfuse-gdr.md) · [`docs/langfuse-etl.md`](langfuse-etl.md)（模块级实施参考）。本设计文档的 §3.1 / §6.6 / §4 `task_pipeline` 行涵盖其在 orchestration 侧的接入点；观测层**不**改变三阶段数据契约（C1/C2/C3 字段、文件路径、SSE 事件流）。
- **Windows 控制台窗口抑制（2026-09-23）**：§6.7 + [`orchestration/_windows.py`](../orchestration/_windows.py) `install_no_window_policy()`：幂等 monkey-patch `_winapi.CreateProcess`，对 `multiprocessing.Pool` worker（cmd 含 `--multiprocessing-fork` 指纹）强制补 `CREATE_NO_WINDOW`；`daemon.start_detached` 的 `creationflags` 显式 OR 上 `CREATE_NO_WINDOW`。避免 pytest / IDE 测试运行器在 Windows 上弹 cmd 窗口。`tests/orchestration/test_no_window_policy.py` 7 项验证全过。

## 12. 后续步骤

第 2 轮（2026-09-22 进行中）做 A + B + C 三合一：

| 选项 | 范围 |
|---|---|
| A（已完成） | 文档改写：本文件 + README.md + orchestration/README.md + CLAUDE.md |
| B（agent-smoke） | 真实 catalog smoke：单 task / 3 task / 全 catalog（parallelism=1 与 =4 各一次） |
| C（agent-verify） | 集成 verify：status 子命令 / 死信归档 / replay 全流程 + 新增 `test_integration_smoke.py` |

详见 [`docs/设计方案/round-2-execution-plan.md`](设计方案/round-2-execution-plan.md)
与 [`docs/设计方案/round-2-summary.md`](设计方案/round-2-summary.md)（汇总待写）。

## 附录 A：术语对照

| 本文档 | 现有模块 | 说明 |
|---|---|---|
| trajectory | `output/agent_trajectory/<run_id>__<session_id>.json` | simulate_serve 从 QwenPaw 拷贝过来的原始 session JSON |
| refined Session | `output/refined/<TXXX>__<session_id>.json` | gdr 裁剪调整后的产物（C2 契约） |
| 4 视图 | `output/refine_data/<TXXX>__<session_id>_refined.{messages,openai,qwenjina.txt,meta}.json` | etl 拆出的训练框架 / audit 视图（C3 契约） |
| task | simulate_serve catalog 中的单个 `task_id`（如 T001 / E005） | 流水线最小调度单位 |
| `phase` | SQLite `tasks.phase` ∈ {pending, simulate, gdr, etl, done, dead} | 子进程推进 + 终态判定 |
| dead | `output/orchestration/dead/<rowid>__<src_basename>` | 重试 max_retry 次仍失败的 task 产物 |
| batch（已删除） | n/a | 旧版的批次切分；新架构无此概念，直接 `--parallelism N` 并发 task |