# Pipeline 重构 — 模块对接契约与函数签名(2026-09-22)

> 本文件是 `pipeline-serial-parallel-refactor.md` 的**实施级补充**:
> 每个模块的**边界 / 对外接口 / 函数签名 / 数据契约 / 错误语义**都写死,
> 子任务 ST-1 ~ ST-7 实施时直接对照本文件落地。

## 文档约定

- **数据不可变 dataclass**:`@dataclass(frozen=True)`,字段全部 typed
- **错误语义**:每个公开函数都标"成功返回 / 抛出异常 / 不抛但写日志" 三种归宿
- **进程模型**:multiprocessing 子进程的入口函数必须 `picklable`,模块顶层函数而非闭包
- **SQLite 写**:所有子进程写 SQLite 都通过 `SQLiteQueue` 实例方法,不直连

---

## §1 配置层契约(ST-1)

### 1.1 文件改动

| 文件 | 改动 |
|---|---|
| `config/config.yaml` | `orchestration:` section 重写 |
| `orchestration/config_loader.py` | 重构 `OrchestrationConfig` / `OrchestrationSettings` |
| `orchestration/settings.py`(新增) | 拆出 `PipelineSettings` / `Paths` dataclass |

### 1.2 YAML schema

```yaml
orchestration:
  pipeline:
    max_parallelism: 1            # int, ≥1
    max_retry_gdr: 3              # int, ≥0
    max_retry_etl: 3              # int, ≥0
    retry_poll_seconds: 2.0       # float, >0
  paths:
    simulate_serve_config: str    # 绝对/相对路径
    trajectory_dir: str           # 默认 output/agent_trajectory
    runs_dir: str                 # 默认 output/runs
    refined_dir: str              # 默认 output/refined
    etl_outputs_dir: str          # 默认 output/refine_data
    sqlite_db: str                # 默认 output/orchestration/orchestration.db
    dead_dir: str                 # 默认 output/orchestration/dead
    pid_file: str                 # 默认 output/orchestration/orchestration.pid
    log_dir: str                  # 默认 output/orchestration/logs
```

### 1.3 Python 类型

```python
@dataclass(frozen=True)
class PipelineSettings:
    max_parallelism: int           # ≥1
    max_retry_gdr: int             # ≥0
    max_retry_etl: int             # ≥0
    retry_poll_seconds: float      # >0

@dataclass(frozen=True)
class Paths:
    simulate_serve_config: Path
    trajectory_dir: Path
    runs_dir: Path
    refined_dir: Path
    etl_outputs_dir: Path
    sqlite_db: Path
    dead_dir: Path
    pid_file: Path
    log_dir: Path

@dataclass(frozen=True)
class OrchestrationConfig:
    settings: PipelineSettings
    paths: Paths
    gdr_settings: Settings         # 复用 gdr 库 Settings dataclass
```

### 1.4 对外接口

```python
# orchestration/config_loader.py
def load_config(config_path: Path | None = None) -> OrchestrationConfig:
    """从 config.yaml 加载并校验,失败抛 ConfigValidationError."""
```

### 1.5 校验规则

- `max_parallelism ≥ 1`,否则抛 `ConfigValidationError("max_parallelism must be ≥ 1")`
- 所有 paths 必须非空字符串
- `Paths.trajectory_dir / refined_dir / etl_outputs_dir / sqlite_db / dead_dir`
  在 `load_config` 时**不创建目录**(由调用方按需 mkdir)

### 1.6 不导出

- `OrchestrationSettings`(旧类,删)
- `batch_size` / `gdr_workers` / `qf_workers` / `watcher_poll_seconds` 等旧字段

---

## §2 SQLite 状态机契约(ST-2)

### 2.1 文件改动

| 文件 | 改动 |
|---|---|
| `orchestration/queue/schema.sql` | 删 `batches` / `run_tasks`,改 `tasks` 表 |
| `orchestration/queue/sqlite_queue.py` | 删旧方法,新增 4 个方法 |
| `orchestration/queue/__init__.py` | state 常量改 phase |

### 2.2 表 schema

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

### 2.3 Phase 常量

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

### 2.4 Task dataclass(返回类型)

```python
@dataclass(frozen=True)
class Task:
    task_id: str
    phase: str
    run_id: str | None
    session_id: str | None
    attempts_simulate: int
    attempts_gdr: int
    attempts_etl: int
    src_path: Path | None
    gdr_refined_path: Path | None
    etl_messages_path: Path | None
    etl_openai_path: Path | None
    etl_qwenjina_path: Path | None
    etl_meta_path: Path | None
    error_msg: str | None
    started_at: str
    updated_at: str
```

### 2.5 公开接口

```python
class SQLiteQueue:
    def __init__(self, db_path: Path) -> None: ...

    # 写入
    def upsert_task(self, task_id: str, *, phase: str = PHASE_PENDING) -> Task:
        """新建或重置 task 行;若已存在且 phase ∈ TERMINAL_PHASES 则报错."""
        # 抛:TaskAlreadyTerminal(task_id, current_phase)
        # 不抛:首次创建 / phase=pending 时覆盖

    def mark_phase(
        self,
        task_id: str,
        *,
        new_phase: str,
        run_id: str | None = None,
        session_id: str | None = None,
        src_path: Path | None = None,
        gdr_refined_path: Path | None = None,
        etl_messages_path: Path | None = None,
        etl_openai_path: Path | None = None,
        etl_qwenjina_path: Path | None = None,
        etl_meta_path: Path | None = None,
    ) -> None:
        """推进 phase,同步写各阶段产物路径.phase 转移合法性由调用方保证."""

    def increment_attempts(self, task_id: str, *, stage: str) -> int:
        """自增 attempts_simulate | attempts_gdr | attempts_etl,返回新值."""
        # stage ∈ {"simulate", "gdr", "etl"}
        # 抛:ValueError if stage invalid

    def mark_failed(
        self,
        task_id: str,
        *,
        stage: str,
        error_msg: str,
    ) -> None:
        """标 phase=dead,写 error_msg."""

    def requeue_dead(self) -> int:
        """所有 phase=dead 改 phase=pending,清空 error_msg / 产物路径;返回受影响行数."""

    # 读取
    def get_task(self, task_id: str) -> Task | None:
        """按 task_id 查单行,不存在返 None."""

    def list_tasks(
        self,
        *,
        phase: str | None = None,
        limit: int | None = None,
    ) -> list[Task]:
        """按 phase 过滤;phase=None 返全部;按 started_at 排序."""

    def count_by_phase(self) -> dict[str, int]:
        """返 {phase: count} 全分布."""

    # 上下文
    def __enter__(self) -> "SQLiteQueue": ...
    def __exit__(self, *exc) -> None: ...
```

### 2.6 异常

```python
class TaskAlreadyTerminal(Exception):
    def __init__(self, task_id: str, current_phase: str) -> None: ...
```

### 2.7 进程模型

- `SQLiteQueue` 实例**不能 pickle**(含 sqlite3 connection),所以子进程内不传实例
- 子进程内通过 `SQLiteQueue(paths.sqlite_db)` 重新创建;每个子进程独立 connection
- SQLite 自身 serializes 写,多进程并发安全

### 2.8 删除的方法(明确不导出)

```python
# 以下方法全部删除,引用方必须改用新方法
- insert_batch / update_batch
- insert_run_task_map / lookup_run / lookup_task_id
- list_tasks_for_batch / count_pending_gdr / count_pending_etl
- pull_pending_gdr / pull_pending_etl / _pull_n
- mark_gdr_done / mark_etl_done  → 用 mark_phase 替代
- _stamp_stage_done
- reap_stale
- delete (legacy db 重命名)
```

---

## §3 Worker 层契约(ST-3)

### 3.1 文件改动

| 文件 | 改动 |
|---|---|
| `orchestration/workers/base_worker.py` | 删 `BaseWorker` 类 / `run_forever`;保留模块函数 `_output_filename(task_id, session_id, suffix="")` |
| `orchestration/workers/gdr_worker.py` | 改 `GdrWorker.process()` 为模块函数 `run_gdr_once()` |
| `orchestration/workers/etl_worker.py` | 改 `EtlWorker.process()` 为模块函数 `run_etl_once()` |

### 3.2 模块函数 `_output_filename`(base_worker.py)

```python
def _output_filename(
    task_id: str,
    session_id: str,
    *,
    suffix: str = "",
) -> str:
    """产物文件命名: <safe_task_id>__<safe_session_id><suffix>.

    safe_*: 文件名非法字符替换为 '_'
    suffix: '.messages.json' / '.openai.json' / '.meta.json' 等
    """
```

### 3.3 GDR worker 接口

```python
# orchestration/workers/gdr_worker.py

@dataclass(frozen=True)
class GdrResult:
    refined_path: Path
    task_id: str
    session_id: str
    duration_seconds: float

class GdrNonRetryableError(Exception):
    """轨迹文件不合法 / schema 不匹配 / 远程 LLM 永久错误 — 不应重试."""

def run_gdr_once(
    *,
    src_path: Path,
    refined_dir: Path,
    gdr_settings: Settings,
    task_id: str,
    session_id: str,
) -> GdrResult:
    """单个 trajectory JSONL 文件 → 一个 C2 refined Session JSON.

    流程:
      1. from_trajectory(src_path) 校验 + 解析
      2. 构造 _process_one_file 的 Settings (workers=1, llm_concurrency, ...)
      3. 调 _process_one_file(src_path, out_path, cfg)
      4. 成功后返 GdrResult(refined_path=<out_path>, ...)

    抛:
      GdrNonRetryableError: src_path 不存在 / 解析失败 / schema 不匹配
      RetryableGdrError: LLM 调用失败 / 临时 IO 错误(可重试)
      Exception: 其他未捕获(由 PipelineExecutor 兜底标 dead)

    不抛:
      refine_data/incomplete / judge_low / routing_low / deferred 旁路 —
      这些是 gdr runner 自身的输出,与本函数无关。
    """
```

### 3.4 ETL worker 接口

```python
# orchestration/workers/etl_worker.py

@dataclass(frozen=True)
class EtlOutputs:
    messages_path: Path
    openai_path: Path
    qwenjina_path: Path | None
    meta_path: Path
    task_id: str
    session_id: str
    duration_seconds: float

class EtlNonRetryableError(Exception):
    """C2 文件 schema 不匹配 / load_refined_session 失败 — 不应重试."""

def run_etl_once(
    *,
    c2_path: Path,
    etl_outputs_dir: Path,
    task_id: str,
    session_id: str,
) -> EtlOutputs:
    """单个 C2 refined Session JSON → 4 视图文件.

    流程:
      1. load_refined_session(c2_path) 读 + 校验
      2. save_session_v2(session, base_path) 写 4 视图
      3. 返 EtlOutputs(messages_path=..., openai_path=..., ...)

    抛:
      EtlNonRetryableError: c2_path 不存在 / schema 不匹配 / load 失败
      Exception: 其他未捕获(由 PipelineExecutor 兜底)

    不抛:
      qwenjina_path 为 None 时仍返回非 None 的其他三个路径
    """
```

### 3.5 删除

```python
# 以下全部删除
- BaseWorker 类(含 run_forever / pull / run_once 等所有方法)
- GdrWorker 类 / EtlWorker 类
- worker_idle_backoff / batch_ids 等参数
- 任何 threading.Thread 启动代码
- 任何 SQLiteQueue 写操作(由 PipelineExecutor 调 mark_phase)
```

---

## §4 Producer 契约(ST-4)

### 4.1 文件改动

| 文件 | 改动 |
|---|---|
| `orchestration/producer_simulate.py` | `run_batch` 改 `run_one_task(task_id) -> TaskRun` |

### 4.2 接口

```python
# orchestration/producer_simulate.py

async def run_one_task(
    task_id: str,
    *,
    config_path: Path,
) -> TaskRun:
    """in-process 跑 simulate_serve 单 task,返回 TaskRun.

    流程:
      1. 读 config_path 构造 settings
      2. build_application(config) 构造 services
      3. 从 services.task_manager.compiled_tasks 找 task_id 对应 task
      4. services.batch_runner.run([task]) 跑单 task
      5. 返 runs[0]

    抛:
      KeyError: task_id 不在 catalog
      Exception: simulate_serve 任意异常(由 PipelineExecutor 兜底)

    副作用:
      - output/runs/<run_id>/run.json 已落盘(JsonRunRepository)
      - output/agent_trajectory/<run_id>__<session_id>.json 已落盘(archiver)
      - SQLite tasks.phase 仍为 pending(由 PipelineExecutor 调 mark_phase 推进)
    """
```

### 4.3 关键约定

- **每个子进程独立调一次**:子进程 pool 内,`run_one_task(task_id)` 是入口函数的一部分
- **不在此处写 SQLite**:避免与 PipelineExecutor 双重写
- **不在此处做重试**:simulate 阶段的 retry 由 TaskRuntime 自己处理(已有 `--max-run-retries`)

### 4.4 删除

```python
# 删除
- run_batch 函数(被 run_one_task 替代)
- _split_batches
- insert_batch / update_batch / insert_run_task_map 调用
- limit 参数(只跑单 task)
- BatchRunner 之外的任何状态机
```

---

## §5 PipelineExecutor 契约(ST-5)— 核心

### 5.1 文件改动(新增)

| 文件 | 作用 |
|---|---|
| `orchestration/pipeline_executor.py` | `PipelineExecutor` 类 + `PipelineSummary` dataclass |
| `orchestration/task_pipeline.py` | 子进程入口函数 `_run_one_task_pipeline` |

### 5.2 PipelineSummary

```python
@dataclass(frozen=True)
class PipelineSummary:
    total: int                  # 提交的总 task 数
    done: int                   # phase=done 的 task 数
    dead: int                   # phase=dead 的 task 数
    duration_seconds: float     # 主循环总耗时
```

### 5.3 PipelineExecutor 接口

```python
class PipelineExecutor:
    def __init__(
        self,
        *,
        queue: SQLiteQueue,
        settings: PipelineSettings,
        paths: Paths,
        gdr_settings: Settings,
    ) -> None: ...

    def run(self, task_ids: list[str]) -> PipelineSummary:
        """按 max_parallelism 调度 N 个 task 的 simulate→gdr→etl 流水线.

        算法:
          - 起 multiprocessing.Pool(processes=max_parallelism, initializer=_worker_init)
          - 维护 in_flight: dict[AsyncResult, str]
          - 槽位空时从 pending 取 task 投递
          - future.ready() 时取结果,统计 done/dead
          - 所有 task 跑完返回 PipelineSummary

        抛:
          ValueError: task_ids 为空
          Exception: 子进程崩溃被 future.get() 捕获,标 dead,继续下一个
        """

    def shutdown(self) -> None:
        """优雅关闭:close() pool + join(),正在跑的子进程等待完成."""
```

### 5.4 TaskPipeline 入口函数

```python
# orchestration/task_pipeline.py

def _worker_init(paths: Paths) -> None:
    """子进程初始化:把 paths 写入模块全局.

    注意:
      - 必须顶层函数,可 pickle
      - 不读 SQLite(SQLiteQueue 在 _run_one_task_pipeline 内按需创建)
    """

def _run_one_task_pipeline(
    task_id: str,
    paths: Paths,
    gdr_settings: Settings,
    orchestration_settings: PipelineSettings,
) -> dict:
    """单个 task 在子进程内完整跑 simulate → gdr → etl.

    返回:
      {"task_id": str, "phase": "done"|"dead", "stage": str, "error": str|None}

    流程:
      1. queue = SQLiteQueue(paths.sqlite_db)        # 子进程内独立连接
      2. queue.upsert_task(task_id, phase="pending") # 兜底:若已被另一个 worker 标 done 不重复跑
      3. queue.mark_phase(task_id, new_phase="simulate")
      4. run = producer_simulate.run_one_task(task_id, config_path=paths.simulate_serve_config)
      5. 若 run.state ∈ TERMINAL_FAIL_STATES: queue.mark_failed(...); return dead
      6. queue.mark_phase(task_id, new_phase="gdr", run_id=run.run_id, session_id=run.remote_session_id, src_path=...)
      7. 重试循环(max_retry_gdr 次):
           调 run_gdr_once(...)
           成功 → break;NonRetryable → mark_failed;Retryable → 增 attempts_gdr,继续重试
      8. queue.mark_phase(task_id, new_phase="etl", gdr_refined_path=...)
      9. 重试循环(max_retry_etl 次): 类似 gdr 阶段
     10. queue.mark_phase(task_id, new_phase="done", etl_*_path=...)
     11. return {"phase": "done", ...}

    任何未捕获异常:
      - 顶层 try/except 兜底
      - queue.mark_failed(task_id, stage=<当前 stage>, error_msg=str(exc))
      - return {"phase": "dead", "error": str(exc), ...}

    子进程不抛异常给主进程(主进程只看 future.get() 返回 dict)。
    """
```

### 5.5 子进程约束

| 约束 | 原因 |
|---|---|
| 子进程入口必须是模块顶层函数 | multiprocessing.Pool.apply_async 需要 picklable |
| 不传 SQLiteQueue 实例 | SQLite connection 不可 pickle |
| 不传 TaskRuntime 实例 | 不可 pickle |
| 配置对象必须可 pickle | 浅 dataclass 即可,深对象不可 pickle |
| 日志 handler 在子进程内重新初始化 | logging 模块多进程不安全 |

### 5.6 子进程内模块导入

```python
# task_pipeline.py 顶部
import multiprocessing
import logging
from pathlib import Path

# 延迟 import:模拟器内 asyncio 初始化代价高,只在子进程需要时 import
def _run_one_task_pipeline(...):
    from .producer_simulate import run_one_task
    from .workers.gdr_worker import run_gdr_once, GdrNonRetryableError
    from .workers.etl_worker import run_etl_once, EtlNonRetryableError
    from ..queue.sqlite_queue import SQLiteQueue
```

---

## §6 Master 契约(ST-6)

### 6.1 文件改动

| 文件 | 改动 |
|---|---|
| `orchestration/master.py` | 大幅精简 |
| `orchestration/batch_tracker.py` | **整文件删** |
| `orchestration/failure_handler.py` | `reap_dead` 删 batch_id 引用 |
| `orchestration/health.py` | `collect_batches` 改 `collect_tasks` |

### 6.2 Master 接口

```python
class Master:
    def __init__(self, cfg: OrchestrationConfig) -> None: ...

    def run(self, task_ids: list[str]) -> PipelineSummary:
        """创建 PipelineExecutor,运行,返回汇总.

        副作用:
          - 调 executor.run() 前写 health
          - 调 executor.run() 后再写 health(done/dead 统计)
        """

    def shutdown(self) -> None:
        """设置 stop_event,通知 executor 优雅停止.

        注:executor 子进程不响应 stop_event(子进程跑一个 task 就退),
        shutdown 仅触发 main thread 提前退出 wait loop,但已经在跑的
        task 仍会跑完。
        """

    def status(self) -> dict:
        """返 {phases: {phase: count}, total: int, last_updated: str}.

        数据源:SQLiteQueue.count_by_phase() + list_tasks(phase="running...")。
        """

    def _build_gdr_settings(self) -> Settings:
        """构造 gdr 库 Settings 实例,workers=1,llm_concurrency 透传."""
```

### 6.3 删除的方法(明确不导出)

```python
# 以下全部删除
- start_workers / _add_thread
- _start_batch_watcher / _first_scan_watcher
- wait_batch_drained
- register_active_batch / unregister_active_batch
- _reaper_loop / _reaper_thread
- _active_batch_ids / _threads / _workers_started
- alive_workers property
- _count_terminal_for_batch
- _run_one_batch(改名为 _run_one_task,且实现大幅简化)
```

### 6.4 failure_handler.py

```python
# orchestration/failure_handler.py

def reap_dead(queue: SQLiteQueue, *, dead_dir: Path, log_path: Path | None = None) -> list[DeadArchive]:
    """扫描 phase=dead 的 tasks,把产物移到 dead_dir,写日志.

    返回:
      list[DeadArchive]: 每个 dead task 一条,含 task_id / 各阶段产物路径 / error_msg

    删除:
      batch_id 字段(原 DeadArchive dataclass)
      log_entry["batch_id"] 字段
    """
```

### 6.5 health.py

```python
# orchestration/health.py

def collect_tasks(queue: SQLiteQueue) -> dict[str, object]:
    """统计 tasks 表状态.

    返回:
      {
        "phases": {"pending": 0, "simulate": 0, "gdr": 0, "etl": 0, "done": 0, "dead": 0},
        "total": int,
        "last_updated": str,  # ISO8601
      }

    删除:
      collect_batches 函数
      batches 表 SELECT
      dead_count / gdr_count / etl_count / status 字段
    """

def write_health(queue: SQLiteQueue, *, log_dir: Path) -> None:
    """收集状态写到 log_dir/health.json."""
```

---

## §7 CLI 契约(ST-7)

### 7.1 文件改动

| 文件 | 改动 |
|---|---|
| `orchestration/__main__.py` | argparse 精简 + 主入口重写 |
| `simulate_serve/__main__.py` | 删任务相关 CLI |
| `gdr/run.bat` | **整文件删** |
| `scripts/run.bat` | 注释更新 |

### 7.2 orchestration 子命令

```
python -m orchestration start [options]
python -m orchestration status
python -m orchestration stop
python -m orchestration replay

start 选项:
  --tasks T1,T2,T3          # 可选,逗号分隔子集过滤
  --all-tasks               # 默认行为,拉全 catalog
  --parallelism N           # 默认 1,设 ≥2 启用子进程并行
  --detach                  # 后台进程
  --foreground              # 前台(默认)
  --dry-run                 # 只打印计划,不真跑
  --stay                    # 跑完不退出 master
```

### 7.3 `_cmd_start` 接口

```python
def _cmd_start(args: argparse.Namespace, cfg: OrchestrationConfig) -> int:
    """start 子命令入口.

    流程:
      1. 解析 task_ids:
         - args.tasks → 逗号分隔列表
         - args.all_tasks → _load_all_task_ids(cfg.paths.simulate_serve_config)
         - 二者均空 → _load_all_task_ids() (默认行为)
      2. 校验 task_ids 全部在 catalog 内(否则 ValueError)
      3. args.dry_run → 打印计划并 return 0
      4. Master(cfg).run(task_ids) → PipelineSummary
      5. 打印汇总
      6. 返 0
    """
```

### 7.4 `_cmd_status` 接口

```python
def _cmd_status(args: argparse.Namespace, cfg: OrchestrationConfig) -> int:
    """status 子命令入口.

    流程:
      1. SQLiteQueue(cfg.paths.sqlite_db)
      2. collect_tasks(queue)
      3. 格式化打印 phases / total / last_updated
      4. 列出最近 10 个 task 的 task_id / phase / error_msg
      5. 返 0
    """
```

### 7.5 `_cmd_replay` 接口

```python
def _cmd_replay(args: argparse.Namespace, cfg: OrchestrationConfig) -> int:
    """replay 子命令入口:把 phase=dead 复活为 pending.

    流程:
      1. SQLiteQueue(cfg.paths.sqlite_db)
      2. queue.requeue_dead()
      3. 打印复活数量
      4. 返 0

    删除: --batch N 选项
    """
```

### 7.6 simulate_serve 保留的 CLI

```python
# 保留(只读)
--validate-config
--check-tools
--readiness
--list-interrupted

# 删除(移交 orchestration)
--tasks / --rerun-task / --limit / --include-offline / --max-run-retries
```

### 7.7 删除

```python
# orchestration/__main__.py 删除
- --batch-size 选项
- --exit-when-done 选项(已 deprecated)
- replay 的 --batch N 选项
- _split_batches 函数

# 整文件删
- gdr/run.bat
```

---

## §8 模块依赖图

```
CLI (§7)
  └─> Master (§6)
        └─> PipelineExecutor (§5)
              ├─> multiprocessing.Pool
              │     └─> _run_one_task_pipeline (§5.4)
              │           ├─> producer_simulate.run_one_task (§4)
              │           │     └─> simulate_serve.application.BatchRunner
              │           ├─> workers.gdr_worker.run_gdr_once (§3.3)
              │           │     └─> gdr.pipeline.runner._process_one_file
              │           ├─> workers.etl_worker.run_etl_once (§3.4)
              │           │     └─> etl.parsers.load_refined_session
              │           │     └─> gdr.domain.save_session_v2
              │           └─> SQLiteQueue (§2)
              └─> SQLiteQueue (§2)

  ├─> failure_handler.reap_dead (§6.4)
  ├─> health.collect_tasks (§6.5)
  └─> config_loader.load_config (§1.4)
```

## §9 调用顺序(子任务交付)

| 步骤 | 子任务 | 验证 |
|---|---|---|
| 1 | ST-1 配置层 | `load_config()` 返 OrchestrationConfig 实例 |
| 2 | ST-2 SQLite | `SQLiteQueue(db_path).upsert_task("T001")` 成功,新 schema 跑通 |
| 3 | ST-3 Worker | `run_gdr_once(src_path, refined_dir, gdr_settings, task_id, session_id)` 返 GdrResult |
| 4 | ST-4 Producer | `await run_one_task("T001", config_path=...)` 返 TaskRun |
| 5 | ST-5 PipelineExecutor | `PipelineExecutor(...).run(["T001"])` 返 PipelineSummary(done=1) |
| 6 | ST-6 Master | `Master(cfg).run(["T001"])` 返 PipelineSummary |
| 7 | ST-7 CLI | `python -m orchestration start --tasks T001 --dry-run` 打印计划不报错 |
| 8 | ST-8 全量验证 | `pytest -q` 全绿;`python -m orchestration start --all-tasks --parallelism 1` 跑通 |
