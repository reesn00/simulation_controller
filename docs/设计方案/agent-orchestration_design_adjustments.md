# agent-orchestration 实施调整记录(2026-09-22)

> 本文件由 ST-5 + ST-6 + ST-7 实施组 (agent-orchestration) 维护,记录在落地
> `pipeline-contracts.md` / `pipeline-serial-parallel-refactor.md` 时遇到的
> 设计冲突 / 实施细节 / 跨组集成疑问。**契约文件本身不修改**;若发现契约
> 与现有代码不一致,在此登记并附依据。

## 1. ST-5 PipelineExecutor 设计调整

### 1.1 子进程入口与 `_worker_init` 签名

契约 §5.4 写:`_worker_init(paths: Paths)` 仅保存 paths。`_run_one_task_pipeline`
接受 4 个位置参数:`task_id, paths, gdr_settings, orchestration_settings`。

**实施选择**:`_worker_init` 仍然只 set `paths`(全局 stash)。子进程入口
`_run_one_task_pipeline` 接收全部参数(全部可 pickle 的浅 dataclass)。这样
保证 `multiprocessing.Pool` 的 `initargs` 只传 `paths`(简单),子进程内
init 期间不读 SQLite。

### 1.2 子进程入口用延迟 import

契约 §5.6 建议:子进程内延迟 import 以避免不必要的 asyncio / LLM SDK 初始化
代价。`_run_one_task_pipeline` 函数体顶部:

```python
from .producer_simulate import run_one_task
from .workers.gdr_worker import run_gdr_once, GdrNonRetryableError
from .workers.etl_worker import run_etl_once, EtlNonRetryableError
from ..queue.sqlite_queue import SQLiteQueue
```

这样:
- 子进程只 import 必需的模块,父进程不会被动触发大块 import
- pickle 时 `_run_one_task_pipeline` 不再依赖这些模块已在父级 import(避免
  ModuleNotFoundError when reimporting under spawn)

### 1.3 子进程返回 dict 的字段稳定

契约 §5.4:`{"task_id": str, "phase": "done"|"dead", "stage": str, "error": str|None}`。
实施补充 `stage` 取值 ∈ {"simulate", "gdr", "etl", "done"};主进程按
`(phase, stage)` 决定是否 mark_failed。

### 1.4 simulate_serve domain.run.TaskRun.state 终态判断

子进程需要判定 TaskRun.state 是否 ∈ TERMINAL_FAIL_STATES。契约 §4.2 提到
`run.state ∈ TERMINAL_FAIL_STATES`。实施时直接用
`simulate_serve.domain.state_machine.TERMINAL_STATES`,然后排除 success
(gdr/etl 仍可跑):

```python
SIMULATE_OK_STATES = TERMINAL_STATES - {RunState.SUCCESS}
```

注意:`TERMINAL_STATES` 包含 `RunState.SUCCESS`,SUCCEED → 进入 gdr 阶段;
其余终态(`VALIDATION_ERROR` / `EXECUTOR_ERROR` / `ACTOR_ERROR` / `CANCELLED`
/ `INTERRUPTED` / `COMPLETION_INCOMPLETE` / `INCONCLUSIVE` / `GUIDE_EXHAUSTED`)
直接标 dead。

## 2. ST-6 Master 设计调整

### 2.1 Master 不持有 `_threads` / `_workers_started` / `_active_batch_ids`

契约 §6.3 删除的方法列表明确包含上述字段。多进程 Pool 由 PipelineExecutor
持有;Master 仅持有 stop_event 与 queue。

### 2.2 Master.shutdown 不调线程 join

旧实现 `Master.shutdown(timeout=...)` join 一堆 worker threads。新架构下
主线程只持有 `PipelineExecutor._process_pool` 的引用;shutdown 仅 set
stop_event(若需要提前退出)。Pool 在 PipelineExecutor.run 内部 finally 块
close/join,所以 Master.shutdown() 不再做 process wait。

### 2.3 Master.status 直接读 `count_by_phase()`

`Master.status() -> dict` 用 `SQLiteQueue.count_by_phase()` + 当前 ISO 时间戳。
最近 N 条 task 用 `SQLiteQueue.list_tasks(limit=N)`,过滤 dead 优先。

### 2.4 health.collect_tasks 返回结构

契约 §6.5:`{"phases": {phase: count}, "total": int, "last_updated": str}`。
实施时把 phases 全部初始化为 0(契约 §6.5 给的示例含 6 个 phase 全字段),
保证 CLI 打印时 keys 稳定。

### 2.5 failure_handler.reap_dead 删除 batch_id

契约 §6.4:`DeadArchive` dataclass 删 batch_id 字段;`log_entry["batch_id"]`
删除。归档文件名沿用 `<task_id>__<src_basename>`(无 batch 前缀)。

## 3. ST-7 CLI 设计调整

### 3.1 删除 `gdr/run.bat`

契约 §7.7 明确整文件删。本组负责执行删除。

### 3.2 `simulate_serve/__main__.py` 删除任务相关 CLI

契约 §7.6 明确删除 `--tasks` / `--rerun-task` / `--limit` / `--include-offline`
/ `--max-run-retries`。**保留只读开关**:`--validate-config` / `--check-tools`
/ `--readiness` / `--list-interrupted` / `--verbose`。

注意:删除 `--limit` / `--include-offline` 后,`simulate_serve/__main__.py`
的 `_run` 函数实现需要相应简化(全 catalog + 默认全过 offline_only 过滤
关闭 / 开启由 `cfg.skip_unready_tasks` 控,与 orchestration 无关)。

### 3.3 orchestration/__main__.py 的 detach 子进程 argv

契约 §7.3 `_cmd_start` 需要 detach 时构造子进程 argv。`--parallelism` 参数
必须透传给 detached 子进程(否则子进程会读 cfg 默认值),需加入 detach_argv
构造逻辑。

### 3.4 orchestration/__main__.py 的 daemon.py 复用

旧实现 `start_detached` / `start_foreground` / `daemon_stop` 复用现有
`orchestration.daemon` 模块。新实现保留这三个函数(它们只关心 pid_file /
log_dir / 子进程 spawn,不依赖 batch 概念)。

## 4. 跨组集成疑问(留给后续阶段)

1. **`producer_simulate.run_one_task` 是否已实现?** ST-4 负责实施。
   `task_pipeline._run_one_task_pipeline` 显式调用该函数。如 ST-4 滞后,
   `task_pipeline.py` import 会失败 → 需协调 ST-4 落地。
2. **`workers.gdr_worker.run_gdr_once` / `workers.etl_worker.run_etl_once`
   是否已实现?** ST-3 负责。当前 `workers/gdr_worker.py` 仍是 `GdrWorker`
   class 实现,无模块函数入口。`task_pipeline.py` 调用预期形式已写,实际
   模块函数由 ST-3 提供。
3. **`SQLiteQueue.upsert_task` 在已 terminal 时抛 `TaskAlreadyTerminal`**
   (契约 §2.5)。`PipelineExecutor.run` 在分配新 task 槽时调 upsert_task,
   若 task 已被另一个 worker 标 done,会抛。实施时 `PipelineExecutor._fill_slots`
   捕获此异常,视为该 task 已被别人处理完,**不计入 dead**(计入 done)。

## 5. 已知遗留 / 不在本任务范围

- `tests/orchestration/test_queue.py` 仍引用旧 `STATE_*` 常量,ST-2 负责改写。
- `tests/orchestration/test_watcher.py` 引用旧 `STATE_PENDING` / `count_pending_gdr`,
  本组 ST-7 负责整文件删(watcher 在新架构中无意义,producer 直接写 trajectory
  + main 流程由 mark_phase 推进)。
- `tests/orchestration/test_batch_tracker.py` 引用旧 batch_tracker 模块,
  本组负责整文件删(batch_tracker 整模块删)。
- `tests/orchestration/test_direction_b_batch_isolation.py` 测试旧方向 B 隔离,
  新架构无 batch 隔离概念,本组负责整文件删。
- `tests/orchestration/test_stage_timestamps_naming_backoff.py` /
  `test_gdr_worker.py` / `test_etl_worker.py` / `test_gdr_worker_nonretryable_status.py`
  / `test_system_prompt.py` / `test_producer_simulate.py` 引用旧 worker API,
  由 ST-3 / ST-4 / ST-2 负责改写。

## 6. 实施期补充(test_failure_recovery.py 改写 / 测试细节)

### 6.1 `DeadArchive.task_id` 是 SQLite rowid,不是 user-facing task_id

`failure_handler.reap_dead` 内部用 SQLite rowid 作归档前缀
(`<rowid>__<src_basename>`),与 `Task.task_id` (str, user-facing) 命名空间
不同。`test_dead_artifact_archived_to_dead_dir` 测试仅断言
`<digits>__<filename>` 形式,不假设 rowid == task_id。

### 6.2 `_FakePool` 复用 `multiprocessing.Pool` 替身

为避免真起子进程,`test_pipeline_executor.py` / `test_failure_recovery.py`
均用 monkeypatch 把 `multiprocessing.Pool` 替换为同步 `_FakePool`,
其 `apply_async` 直接跑 `_run_one_task_pipeline` 的等价逻辑
(simulate/gdr/etl 三阶段 + SQLite 状态机推进),主进程通过 `future.get()`
拿到 dict 返回值。

`_CrashPool` 子类用于测试子进程崩溃兜底:`apply_async` 返回的 future
直接 `ready=True`,`get()` 抛 `RuntimeError`。

### 6.3 `_run_one_task_pipeline` 返回字段

实际跑测试时确认契约 §5.4 字段稳定:子进程返回
`{"task_id", "phase", "stage", "error"}`,主进程按 `phase="done"` 计入
done,其余计入 dead。`stage` 取值 ∈ {simulate/gdr/etl/done}。

### 6.4 detach 子进程要 idle 起 master

测试 `test_start_detached_spawns_child` 暴露了一个边界:detached 子进程
走 `_cmd_start --foreground`,若 catalog 解析出 task_ids=[] 时
`Master.run([])` 会 raise ValueError,子进程没机会写 PID 文件就退了。

**调整**:`_cmd_start` 走前台路径时,先判断 `task_ids` 是否为空:
* 空 → 不调 `Master.run`,直接 `stop_event.wait()` 等停信号(idle 模式)
* 非空 → 调 `Master.run(task_ids)`,然后按 `--stay` 决定是否 wait

`Master.shutdown()` 仍负责关闭 stop_event,这样 detach 测试 / 真实 catalog
为空 / `--stay` 三种场景都能让 master 正常存活到收到 stop。

### 6.5 `tests/orchestration/test_gdr_worker_nonretryable_status.py` /
`test_smoke_3task.py` / `test_stage_timestamps_naming_backoff.py` 当前
仍引用旧 API

这三个文件不在本组 ST-5/6/7 任务列表里,推测由其他组 (ST-3 gdr / ST-4
producer) 改写。本组暂不动它们,也不在 `tests/orchestration` 收集时阻塞
(用 `--ignore` 跳过)。**请对应组在上线前完成改写**,本组代码已准备好
旧名 → 新名的映射 (`STATE_DONE` → `PHASE_DONE`,
`GdrWorker` → 顶层函数 `run_gdr_once`,`collect_batches` → `collect_tasks`)。