# Orchestration 三阶段 Pipeline · 设计文档

> **状态**：方案定稿（待实施）
> **范围**：`simulate_serve / etl/qwenformat / gdr` 三个独立模块的流水线串联层
> **不在范围**：`simulate_serve` 内部重构、`data_refiner` 接入（明确跳过）、`etl/pawsession` 接入（明确跳过）

---

## 1. 背景与动机

仓库现有三个独立子系统，各自具备完整的 CLI 与编程入口，但**没有上层任务调度器**把它们串成一条流水线：

| 子系统 | 当前使用方式 | 入口 |
|---|---|---|
| `simulate_serve` | 手动 `python -m simulate_serve --limit N`；可产出 `output/agent_trajectory/<run_id>__<session_id>.json` | [`simulate_serve/__main__.py:23-174`](simulate_serve/__main__.py) |
| `etl/qwenformat` | 手动 `python etl/qwenformat/run_etl.py`；吃 OpenAI messages 数组 | [`etl/qwenformat/run_etl.py:33-66`](etl/qwenformat/run_etl.py) |
| `gdr` | 手动 `gdr-pipeline --batch-input-dir ...`；吃 QwenPaw Session JSON | [`gdr/pipeline/runner.py:574-629`](gdr/pipeline/runner.py) |

**目标**：新增一个 `orchestration/` 顶层模块，按"批次驱动 + 持续消费"的混合模式把三者串起来，让用户一个 `start` 命令完成：

1. simulate_serve 按 `batch_size` 跑 N 个 task，每批等所有 `run.json` 终态
2. 落地的新 trajectory 文件由 SQLite 队列登记
3. qwenformat worker 把 trajectory 转成"含 OpenAI metadata 的 Session JSON"
4. gdr worker（内启 multiprocessing.Pool）做无效 message 裁剪调整
5. 重试 3 次失败入死信队列并告警

---

## 2. 设计决策汇总

| # | 决策点 | 选定方案 | 备注 |
|---|---|---|---|
| 1 | 跳过 `data_refiner` | gdr 直接吃 trajectory | trajectory 已是 gdr 期待的 Session JSON 形态 |
| 2 | 跳过 `etl/pawsession` | qwenformat 直接吃 trajectory | qwenformat 内部扩展一个转换函数 |
| 3 | 处理顺序 | trajectory → qwenformat → gdr | 与需求一致 |
| 4 | qf→gdr 格式 | qf 输出 Session JSON（含 OpenAI metadata） | 保留 blocks + 在 metadata 加 OpenAI 字段 |
| 5 | qf worker 粒度 | 单 trajectory → 单 sample | 与 gdr 单文件粒度对齐 |
| 6 | simulate_serve 模式 | 混合：批驱动 + 持续消费 | 保留 `--limit` 行为，下游按事件消费 |
| 7 | 批次结束判定 | 所有 task `run.json.state ∈ TERMINAL_STATES` | 见 [`domain/state_machine.py:11-43`](simulate_serve/domain/state_machine.py) |
| 8 | 中间件队列 | SQLite 单文件 | 事务安全，无需外部依赖 |
| 9 | 失败重试 | 重试 3 次后入死信 | qf / gdr 各自计数 |
| 10 | gdr 并发路径 | gdr 内置 Pool（orchestration 1 worker 进程 + `cfg.workers=M`） | 复用 `_batch_report.json` |
| 11 | 生命周期 | daemon 服务（`start` / `status` / `stop` CLI） | PID file + signal handler |
| 12 | watcher 发现 | 轮询 2s | 跨平台稳定 |
| 13 | 汇聚产出 | 不要 | 每个 trajectory 一份 refined JSON，不聚合 |

---

## 3. 完整数据流

```mermaid
flowchart TD
    subgraph Master["orchestration daemon (master)"]
        M[master.py 主循环]
        BT[batch_tracker]
    end

    subgraph Producer
        P[producer_simulate.py]
        SS[simulate_serve.bootstrap]
    end

    subgraph FSQueue["文件系统"]
        TRJ[output/agent_trajectory]
    end

    subgraph Watcher
        W[watcher.py 轮询 2s]
    end

    subgraph SQLite["SQLite 队列"]
        DB[(orchestration.db)]
    end

    subgraph QFStage["qf_worker × K"]
        QF[qf_worker.py]
        QFM[qwenformat.transform 新函数]
    end

    subgraph GDRStage["gdr_worker × 1 + 内置 Pool"]
        GD[gdr_worker.py]
        GDRP[gdr.pipeline._process_one_file]
    end

    subgraph Outputs
        QFO[qf_out/session.json]
        GDRO[gdr/refine_data/stem_refined.json]
        DEAD[dead/]
    end

    M -->|spawn| P
    M -->|spawn| W
    M -->|spawn| QF
    M -->|spawn| GD
    M --> BT

    P --> SS
    SS --> TRJ
    SS --> BT
    BT -.等 run.json 终态.-> M

    TRJ --> W
    W -->|INSERT pending| DB

    QF -->|PULL state=pending| DB
    QF --> QFM
    QFM --> QFO
    QF -->|UPDATE state=pending_gdr, qf_output_path| DB

    GD -->|PULL state=pending_gdr<br/>凑批等待 10s| DB
    GD --> GDRP
    GDRP --> GDRO
    GD -->|UPDATE state=done| DB

    QF -.失败 attempts_qf>3.-> DEAD
    GD -.失败 attempts_gdr>3.-> DEAD
```

---

## 4. 模块划分

### 4.1 新增模块

| 路径 | 职责 |
|---|---|
| `orchestration/__init__.py` | 空 |
| `orchestration/__main__.py` | CLI：`start` / `status` / `stop` / `replay` |
| `orchestration/daemon.py` | daemonize：fork + PID file + signal handler + 日志重定向 |
| `orchestration/master.py` | 主进程：读 config → 启动子进程 → 等批次结束 → 切下一批 → 死信告警 |
| `orchestration/producer_simulate.py` | 包装 `bootstrap.build_application` → `await batch_runner.run(batch, limit=N)`；返回本批 `run_ids` |
| `orchestration/watcher.py` | 轮询 `output/agent_trajectory/` → 调 `SQLiteQueue.insert()` |
| `orchestration/queue/sqlite_queue.py` | SQLite CRUD：`insert` / `pull_pending` / `mark_processing` / `mark_done` / `mark_failed` / `reap_stale` / `count_by_state` |
| `orchestration/queue/schema.sql` | `tasks` + `batches` 表 DDL |
| `orchestration/workers/base_worker.py` | 通用循环：`pull → process → update`；retry/dead 逻辑 |
| `orchestration/workers/qf_worker.py` | 继承 base；调 `qwenformat.transform.trajectory_to_session_with_openai_metadata` |
| `orchestration/workers/gdr_worker.py` | 继承 base；调 `gdr.pipeline._process_one_file`；凑批等待在 `pull_pending_gdr` 内 |
| `orchestration/batch_tracker.py` | 等 `run.json.state ∈ TERMINAL_STATES`；写 `orchestration/data/batches.jsonl` |
| `orchestration/health.py` | 主进程定时查 SQLite counts + 子进程存活 + 写 `orchestration/data/health.json` |
| `orchestration/failure_handler.py` | dead 任务：移动 src + qf_output 到 `dead/`；追加 `dead.log` |
| `orchestration/config.yaml` | 见 §7 |
| `orchestration/run.bat` | Windows wrapper：`orchestration start` / `status` / `stop` |
| `orchestration/README.md` | 使用说明 |

### 4.2 改动模块（最小侵入）

| 路径 | 改动 | 依赖行号 |
|---|---|---|
| `etl/qwenformat/transform.py` | **新增** `trajectory_to_session_with_openai_metadata(trajectory: dict, template_str: str, env) -> dict`：从 trajectory 提取 `session_id` + `messages[role, blocks]` 不变；在 `metadata` 里追加 `openai_messages` + `tools` + `qf_text` | 复用 [`etl/qwenformat/transform.py:51-120`](etl/qwenformat/transform.py) 现有 `build_chat_env` / `load_chat_template` / `render_sample_text` / `sanitize_agent_sample` |
| `pyproject.toml` | **新增** 包声明 `orchestration` + `[project.scripts]` 加 `orchestration = "orchestration.__main__:main"` | — |

### 4.3 不改动

- `simulate_serve/*`：直接复用 [`simulate_serve/bootstrap.py:71-161`](simulate_serve/bootstrap.py) + [`simulate_serve/application/run_batch.py:16-47`](simulate_serve/application/run_batch.py)
- `gdr/pipeline/runner.py`：直接复用 [`pipeline/runner.py:489-513`](gdr/pipeline/runner.py) `_process_one_file`
- `gdr/config/settings.py`：直接 `Settings(batch_input_dir=..., batch_output_dir=..., workers=M)`
- `data_refiner/`：**完全跳过**
- `etl/pawsession/`：**完全跳过**

---

## 5. SQLite schema

```sql
CREATE TABLE tasks (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  src_path        TEXT NOT NULL UNIQUE,
  run_id          TEXT NOT NULL,
  session_id      TEXT,
  batch_id        INTEGER NOT NULL,
  state           TEXT NOT NULL,
  attempts_qf     INTEGER DEFAULT 0,
  attempts_gdr    INTEGER DEFAULT 0,
  qf_output_path  TEXT,
  gdr_output_path TEXT,
  error_msg       TEXT,
  locked_by       TEXT,
  locked_at       TIMESTAMP,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_state ON tasks(state, batch_id);
CREATE INDEX idx_run  ON tasks(run_id);

CREATE TABLE batches (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  task_ids            TEXT NOT NULL,
  simulate_started_at TIMESTAMP,
  simulate_done_at    TIMESTAMP,
  qf_count            INTEGER DEFAULT 0,
  gdr_count           INTEGER DEFAULT 0,
  dead_count          INTEGER DEFAULT 0,
  status              TEXT
);
```

**`state` 取值**：

| state | 含义 |
|---|---|
| `pending` | 已登记，待 qf 处理 |
| `qf_processing` | qf_worker 占用中 |
| `qf_done` | qf 完成，待 gdr 处理（与 `pending_gdr` 等价，文档里统一称 `pending_gdr`） |
| `gdr_processing` | gdr_worker 占用中 |
| `done` | 全流程完成 |
| `failed_qf` / `failed_gdr` | 重试未超限，下次还会被拉起 |
| `dead` | 重试超限，已移入 `dead/` 目录 |

---

## 6. 关键算法

### 6.1 master.py 主循环

```python
def run():
    cfg = load_config()
    queue = SQLiteQueue(cfg.sqlite_db)
    spawn_workers(queue, cfg)            # qf×K, gdr×1, watcher×1

    batches = load_tasks_yaml(cfg.tasks_file)
    while batches:
        batch = batches.pop(0)
        run_ids = producer_simulate.run_batch(batch, cfg.simulate_serve_config,
                                              limit=cfg.batch_size)
        batch_tracker.wait_for_terminal(run_ids)
        queue.wait_batch_drained(batch.id)   # 等 SQLite 中本批全 done/dead
    shutdown_workers()
```

### 6.2 qf_worker.process

```python
def process(task):
    trajectory = json.load(open(task.src_path, encoding="utf-8"))
    out = qf_transform.trajectory_to_session_with_openai_metadata(
        trajectory, TEMPLATE, ENV)
    out_path = Path(cfg.qf_output_dir) / f"{task.session_id}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(out_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    return out_path
```

### 6.3 gdr_worker 含凑批等待

```python
def pull_with_batch_wait(queue, workers, wait_s):
    """若 pending_gdr < workers，sleep 后再取；仍不足按实际数处理"""
    while True:
        tasks = queue.pull_pending_gdr(worker_id=WORKER_ID, n=workers)
        if len(tasks) >= workers or len(tasks) == 0:
            return tasks
        time.sleep(wait_s)
        tasks = queue.pull_pending_gdr(WORKER_ID, n=workers)
        return tasks  # 仍不够也按实际数处理

def process_batch(tasks, cfg):
    gdr_cfg = Settings(
        batch_output_dir=Path(cfg.gdr_output_dir),
        workers=cfg.gdr_workers,
        max_files=len(tasks),
    )
    for t in tasks:
        out = Path(cfg.gdr_output_dir) / f"{t.session_id}_refined.json"
        _process_one_file(Path(t.qf_output_path), out, gdr_cfg)
```

### 6.4 trajectory_to_session_with_openai_metadata（qf 扩展函数）

输入：`trajectory: dict`（gdr 期待的 Session 形态，含 `session_id`, `messages:[{role, blocks:[...]}]`, `summary` 等）

输出：

```json
{
  "session_id": "useramulation-xxx",
  "summary": "...",
  "messages": [                       ← 保留原 blocks 不动
    {"role": "user", "name": "user", "id": "turn_xxx",
     "blocks": [{"type": "text", "text": "..."}], "metadata": {}}
  ],
  "metadata": {
    "openai_messages": [               ← 新增：拆 blocks 转 messages
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "...", "reasoning_content": "...",
       "tool_calls": [{"id": "call_xxx", "type": "function",
                       "function": {"name": "web_search", "arguments": {...}}}]},
      {"role": "tool", "tool_call_id": "call_xxx", "name": "web_search", "content": "..."}
    ],
    "tools": [                        ← 新增：从 tool_call.name 推导
      {"type": "function",
       "function": {"name": "web_search", "description": "", "parameters": {"type": "object"}}}
    ],
    "qf_text": "<|im_start|>system\n# Tools\n...",   ← 新增：jinja 渲染结果
    "qf_rendered_at": "2026-09-01T10:00:00Z"
  }
}
```

实现要点（复用现有函数）：

- `openai_messages`：遍历 `messages[].blocks`，按 block.type 拼装；`tool_call.input` 是 JSON 字符串，用 `json.loads` 反序列化；多个 thinking / text block 合并到同一 message 的 `content` 或 `reasoning_content`
- `tools`：从所有 `assistant` message 的 `blocks` 收集 `tool_call.name` 去重，按 OpenAI function 规范生成空 parameters
- `qf_text`：用 `render_sample_text(openai_messages, tools, TEMPLATE, ENV)` 渲染

### 6.5 watcher.py 轮询

```python
def run(self):
    while not stop_event.is_set():
        for fp in sorted(self.trajectory_dir.glob("*.json")):
            try:
                queue.insert(fp, run_id=parse_run_id(fp),
                             session_id=parse_session_id(fp),
                             batch_id=current_batch_id)
            except AlreadyExists:
                pass
        stop_event.wait(self.poll_seconds)
```

### 6.6 batch_tracker.wait_for_terminal

```python
TERMINAL_STATES = {"SUCCESS", "GUIDE_EXHAUSTED", "INCONCLUSIVE",
                   "VALIDATION_ERROR", "EXECUTOR_ERROR",
                   "ACTOR_ERROR", "CANCELLED", "INTERRUPTED"}

def wait_for_terminal(run_ids, timeout=None):
    """轮询 output/runs/<run_id>/run.json.state ∈ TERMINAL_STATES"""
    pending = set(run_ids)
    while pending:
        for run_id in list(pending):
            run_json = Path(f"output/runs/{run_id}/run.json")
            if run_json.exists():
                state = json.load(open(run_json))["state"]
                if state in TERMINAL_STATES:
                    pending.discard(run_id)
        if pending:
            time.sleep(5)
```

---

## 7. 配置（`orchestration/config.yaml`）

```yaml
orchestration:
  batch_size: 3
  gdr_workers: 2
  qf_workers: 4
  gdr_wait_seconds: 10
  max_retry_qf: 3
  max_retry_gdr: 3
  watcher_poll_seconds: 2
  reap_stale_seconds: 300

paths:
  simulate_serve_config: "simulate_serve/config/config.yaml"
  trajectory_dir: "output/agent_trajectory"
  qf_output_dir: "orchestration/data/qf_out"
  gdr_output_dir: "gdr/refine_data"
  sqlite_db: "orchestration/data/orchestration.db"
  dead_dir: "orchestration/data/dead"
  pid_file: "orchestration/data/orchestration.pid"
  log_dir: "orchestration/logs"

gdr_settings:                 # 透传给 gdr.Settings
  workers: 2
  llm_concurrency: 4
  llm_base_url: "${GDR_LLM_BASE_URL}"
  # 其它字段从 gdr/config/gdr_config.yaml 继承
```

---

## 8. CLI 行为

```bash
# 启动（daemon 化）
uv run python -m orchestration start
# 状态（队列深度、子进程存活、最近 dead）
uv run python -m orchestration status
# 优雅停止（SIGTERM 给 master，master 再传播给子进程）
uv run python -m orchestration stop
# 重置 dead → pending（手动重试死信）
uv run python -m orchestration replay --batch 0
# dry-run：仅打印将要做的事
uv run python -m orchestration start --dry-run
```

---

## 9. 失败语义与重试

| 阶段 | 失败信号 | orchestration 应对 |
|---|---|---|
| producer（simulate_serve） | `run.json.state ∉ TERMINAL_STATES` 超时 / 抛异常 | batch_tracker 标本批 simulate 失败，master 仍继续下一批；失败的 task 不进 SQLite |
| watcher | 单文件 parse 失败 | 写 `dead.log`，不入 SQLite |
| qf worker | `trajectory_to_session_with_openai_metadata` 抛异常 | `attempts_qf++`；> 3 → state=`dead`，src + qf_output 移入 `dead/` |
| gdr worker | `_process_one_file` 返回非 success / 抛异常 | `attempts_gdr++`；> 3 → state=`dead`，src + qf_output + 任意中间产物移入 `dead/` |

**`SQLiteQueue.reap_stale`**：master 定时（默认 5 分钟）将 `locked_at < NOW - reap_stale_seconds` 的任务从 `*_processing` 退回 `pending` / `pending_gdr`，防止 worker 崩溃后任务永久挂起。

---

## 10. 验收测试要点

| 场景 | 期望 |
|---|---|
| smoke：`orchestration start`，3 task 批次 | `orchestration/data/health.json` 记录 `done=3, dead=0`；3 份 `gdr/refine_data/<session>_refined.json` 落盘 |
| 凑批：手动放 1 个 trajectory | gdr_worker 等 10s 后按 1 个启动（不阻塞等满 workers） |
| 失败注入：mock gdr 抛异常 | attempts_gdr 累加；第 4 次入 `dead/` 目录；`dead.log` 多一行 |
| 崩溃恢复：kill -9 gdr_worker | master reap_stale 把 locked 的 `gdr_processing` 退回 `pending_gdr`；新 worker 接管 |
| 批次同步：simulate 慢 task | watcher 不阻塞；生产者完成本批后等 `run.json` 终态再切下一批 |
| qf 转换正确性 | `qf_out/<session>.json` 含 `metadata.openai_messages` 与 `metadata.qf_text`；block 拆解与 pawsession 输出一致（用 pawsession 单元测试 fixture 对照） |
| gdr 端到端 | 拿 `qf_out/<session>.json` 喂 `gdr.pipeline._process_one_file` 手工验证 success |

---

## 11. 与现有文档的关系

- **不重复**：[`flow-architecture.md`](flow-architecture.md) 描述 simulate_serve 内部三层（CLI/Bootstrap、任务运行、验证取证）；本设计文档描述 simulate_serve 之上的**跨子系统流水线**，层级更高
- **不冲突**：[`refactor-implementation-plan.md`](refactor-implementation-plan.md) 是 simulate_serve 内部重构基线；本设计不动 simulate_serve 任何代码
- **补充**：[`phase6-final-validation-report.md`](phase6-final-validation-report.md) 描述 simulate_serve v2 schema；本设计直接消费其产物

---

## 12. 后续步骤

实施顺序（建议）：

1. **骨架**：建 `orchestration/` 空目录 + `pyproject.toml` 注册 + 空 `__main__.py`（可 `start`/`status`/`stop` 但 no-op）
2. **SQLite 队列**：`queue/sqlite_queue.py` + `queue/schema.sql`；写单元测试覆盖 CRUD + reap_stale
3. **qf 扩展**：`etl/qwenformat/transform.py` 加 `trajectory_to_session_with_openai_metadata`；复用 pawsession 单元测试 fixture 验证产物一致
4. **qf_worker**：独立进程，能跑通单 trajectory → qf_out
5. **watcher**：独立进程，能跑通 trajectory_dir → SQLite
6. **gdr_worker**：独立进程，能跑通 qf_out → refined.json
7. **producer_simulate + batch_tracker**：包装 `bootstrap.build_application` 跑一批 + 等终态
8. **master**：串起 producer/watcher/qf/gdr
9. **daemon**：PID + signal + 日志重定向
10. **CLI**：start/status/stop/replay 完整语义
11. **run.bat**：Windows wrapper
12. **冒烟测试**：3 task 批次端到端
13. **失败注入 / 崩溃恢复**：把 §10 全过一遍

每步可独立 commit、独立 review、独立回滚。

---

## 附录 A：术语对照

| 本文档 | 现有模块 | 说明 |
|---|---|---|
| trajectory | `output/agent_trajectory/<run_id>__<session_id>.json` | simulate_serve 从 QwenPaw 拷贝过来的原始 session JSON |
| refined JSON | `gdr/refine_data/<stem>_refined.json` | gdr 裁剪调整后的产物 |
| qf output | `orchestration/data/qf_out/<session>.json` | qwenformat 转换后的 Session JSON（含 OpenAI metadata） |
| batch | `tasks.yaml` 中切出的 N 个 task | 与 simulate_serve `--limit N` 对齐 |
| dead | `orchestration/data/dead/<batch>_<task>.json` | 重试 3 次仍失败的任务 |
