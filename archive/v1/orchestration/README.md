# orchestration · 顶层调度器（2026-09-22 新架构）

把 `simulate_serve` / `gdr` / `etl` 三个独立子系统串成
`simulation server → gdr → etl` 单 task 三阶段严格串行 + 跨 task 可配置并行度的
调度器。新架构删除了旧版"批次驱动 + 多 worker 线程 + watcher + 中间态"的所有
组件，详见 [`docs/设计方案/pipeline-serial-parallel-refactor.md`](../docs/设计方案/pipeline-serial-parallel-refactor.md)
与 [`docs/设计方案/pipeline-contracts.md`](../docs/设计方案/pipeline-contracts.md)。

设计、决策、契约见 [`docs/orchestration-design.md`](../docs/orchestration-design.md)。

## 概述

入口模块：`python -m orchestration`（或 [`scripts\run.bat`](../scripts/run.bat) 的 Windows 包装）。
子命令：`start` / `status` / `stop` / `replay`，与 `master.py` /
`pipeline_executor.py` / `task_pipeline.py` / `queue/sqlite_queue.py` /
`settings.py` / `workers/{gdr,etl}_worker.py` / `producer_simulate.py` 协同工作。

进程模型（2026-09-22 起）：`master.run(task_ids)` 调
`PipelineExecutor.run(task_ids)`，后者起 `multiprocessing.Pool(processes=max_parallelism)`
并维护 `in_flight: dict[AsyncResult, str]` 槽位填充；每个子进程由
`task_pipeline._run_one_task_pipeline` 完整跑单个 task 的
`simulate → gdr → etl` 三阶段。停止通道为 SIGINT/SIGTERM/SIGBREAK + STOP
哨兵文件双保险（Windows detach 子进程无控制台，CTRL_BREAK_EVENT 不可达，哨兵文件
是唯一可靠通道）。

2026-09-28 起，`mark_phase(done)` 之后多一步 **step 11：Label Studio 旁路推送**
（[`ls_hook.py`](ls_hook.py)）。它是流水线的**终点层**——推的是 C3 4 视图 + 一张
`scorecard.v1` 分层评分卡，标注结果**不回流**本项目。默认关闭；推送失败不改变
task 终态、不写 SQLite。详见下文「终点标注（Label Studio）」。

旧版 `watcher.py` / `batch_tracker.py` / `qf_worker` 概念已全部删除，新架构下
trajectory 不再单独入队，由子进程内 `_run_one_task_pipeline` 直接走
`producer_simulate.run_one_task` → 模拟消费者拿 `run_id/session_id` → 子进程拼
`src_path = trajectory_dir / <safe_run>__<safe_session>.json`。

## Windows 包装脚本 `run.bat`

> 包装脚本**必须保持 ASCII 注释**：Windows cmd.exe 在打开 `.bat` 时就锁定代码页，
> 后续 `chcp 65001` 不能重新解码同一文件，非 ASCII 注释会被 GBK 936 破坏而
> 触发 `'xx' 不是内部命令` 报错。详细中文说明放在本文档，bat 内只保留英文
> quick reference。

`run.bat` 把 `%*` 透传给 `python -m orchestration %*`，等价于直接调用 CLI。
会自动检测并优先使用 `uv run python`，未安装 `uv` 时回退到系统 `python`：

```powershell
# 后台跑全部 task（含 E001-E030 评估集）
scripts\run.bat start --all-tasks --detach --parallelism 4

# 前台跑 T001/T003，单 task 三阶段严格串行（默认 max_parallelism=1）
scripts\run.bat start --tasks T001,T003

# 4 子进程并行跑评估集 E001-E003（与 T 任务 schema 一致，行为无差）
scripts\run.bat start --tasks E001,E002,E003 --parallelism 4

# 仅打印计划（config / sqlite_db / pid_file / task_ids），不真启动
scripts\run.bat start --all-tasks --dry-run --parallelism 1

# 查看队列 / 进程 / phase=dead 状态
scripts\run.bat status

# 优雅停止（30s 超时后 taskkill 强杀）
scripts\run.bat stop --timeout 30

# 重放全部 phase=dead 的 task
scripts\run.bat replay
```

### 全局选项（必须放在子命令前）

- `--config PATH` 配置 yaml 路径。默认仓库根 `config/config.yaml`，也可用
  `SIMCTL_CONFIG` 环境变量重定向。配置缺失直接报错，无兜底。
  详见 [`CLAUDE.md`](../CLAUDE.md) 的"配置和工具"章节与
  [`config/config.example.yaml`](../config/config.example.yaml)。

### `start` 子命令选项

- `--detach` 后台化（写 `pid_file` + 子进程，父进程立即返回）。查看后台状态
  用 `run.bat status`；停止用 `run.bat stop`。
- `--foreground` 前台运行（默认；`--detach` 子进程内部会传此标志，用户一般
  不需要手动加）。
- `--dry-run` 仅打印计划，不真的启动 worker。
- `--tasks T1,T2,...` 逗号分隔的 task_id 列表；显式指定视为用户意图，与
  `python -m simulate_serve --tasks` 一致语义。
- `--all-tasks` 加载 `simulate_serve` 全部 task_id 并提交（默认行为）。
- `--parallelism N` 子进程并行度（默认 1 严格串行；设 ≥2 启用 `multiprocessing.Pool`
  并发）。N = 子进程槽位数，每槽位跑一个完整 task。
- `--stay` task 跑完后继续常驻；默认 task 跑完即退出。

**删除**（2026-09-22 起，与旧批次概念不再兼容）：
- `--batch-size`（旧批次大小；新架构无 batch 概念，按 `--parallelism N` 直接并发 task）。
- `--exit-when-done`（已废弃；task 跑完即退现在是默认行为，保留仅为兼容旧命令）。

### `status` 子命令

打印 `health.json` + `queue_counts`（`pending / simulate / gdr / etl / done / dead`
6 个 phase 分布）+ 最近 10 个 task 的 `task_id / phase / error_msg` +
`sqlite_db` 是否存在。详见 [`docs/设计方案/pipeline-contracts.md` §7.4](../docs/设计方案/pipeline-contracts.md)。

### `stop` 子命令

- `--timeout SECONDS` 优雅停止超时秒数（默认 10）。写 STOP 哨兵文件让 master
  优雅 shutdown；超时后 `taskkill /F /T` 强杀。正在跑的子进程会跑完当前 task
  才退出（子进程不响应 stop_event）。

### `replay` 子命令

`phase=dead` 的 task 全部重置为 `pending` 重新入队。无 `--batch` 选项（新架构
无 batch 概念）。详见 [`docs/设计方案/pipeline-contracts.md` §7.5](../docs/设计方案/pipeline-contracts.md)。

## 配置字段（`config/config.yaml` 的 `orchestration:` section）

`config_loader.load_config` 把 YAML 解析成
[`OrchestrationConfig`](config_loader.py)（含
[`PipelineSettings`](settings.py) +
[`Paths`](settings.py) +
`gdr_settings: gdr.Settings`）。详细契约见
[`docs/设计方案/pipeline-contracts.md` §1](../docs/设计方案/pipeline-contracts.md)。

### `pipeline.*`（[`PipelineSettings`](settings.py)）

| 字段 | 类型 | 默认 | 约束 |
|---|---|---|---|
| `max_parallelism` | int | 1 | ≥1；= 1 等同严格串行；≥ 2 启用 `multiprocessing.Pool` |
| `max_retry_gdr` | int | 3 | ≥0 |
| `max_retry_etl` | int | 3 | ≥0 |
| `retry_poll_seconds` | float | 2.0 | > 0 |

**删除**（与旧批次概念不再兼容）：`batch_size` / `gdr_workers` / `qf_workers` /
`watcher_poll_seconds` / `reap_stale_seconds` / `worker_idle_backoff_max_seconds` /
`watcher_idle_backoff_max_seconds` / `batch_drain_poll_seconds` /
`batch_drain_timeout_seconds` / `reap_stale_interval_seconds`。

### `paths.*`（[`Paths`](settings.py)）

| 字段 | 默认值 | 用途 |
|---|---|---|
| `simulate_serve_config` | `config/config.yaml` | simulate_serve 配置（与根配置共文件时自动锚定） |
| `trajectory_dir` | `output/agent_trajectory` | C1 trajectory 事件流落盘根目录 |
| `runs_dir` | `output/runs` | `JsonRunRepository` 的 run 元数据根目录 |
| `refined_dir` | `output/refined` | C2 单 refined Session 文件根目录 |
| `etl_outputs_dir` | `output/refine_data` | C3 4 视图文件 + 旁路 jsonl 根目录 |
| `sqlite_db` | `output/orchestration/orchestration.db` | 编排状态机 SQLite 数据库 |
| `dead_dir` | `output/orchestration/dead` | `phase=dead` task 归档目录 |
| `pid_file` | `output/orchestration/orchestration.pid` | daemon PID 文件 |
| `log_dir` | `output/orchestration/logs` | 运行日志 + `health.json` 落盘目录 |

**删除**（新架构无 `qf` 阶段）：`paths.qf_output_dir` / `paths.gdr_output_dir`
（旧版 `qf_out` / `gdr/refine_data` 由 gdr/etl 内部路径管理，不再走 orchestration 配置）。

`load_config` 不创建任何目录（契约 §1.5），由调用方按需 `mkdir`。

## 终点标注（Label Studio，2026-09-28）

`_run_one_task_pipeline` 的 step 11 把 C3 4 视图 + 一张 `scorecard.v1` 分层评分卡
推给本机 Label Studio 供人工判定。**LS 是本项目终点，标注结果不回流** —— 不实现
fetch、不落 `output/labeled/`、**`queue/` 零修改**（`tasks` 表不新增 `ls_task_id`
列；去重靠 LS 原生 `inner_id = session_id`）。

```powershell
# 1. 注入凭据（不写进 yaml / 不提交）
$env:LABEL_STUDIO_API_KEY = "<your ls api token>"

# 2. 建项目（幂等，重复跑不会建出第二个）+ 灌 label_config
uv run python -m label_studio init-project

# 3. 自检：LS 通不通 / 配置全不全 / 凭据在不在（不打印凭据内容）
uv run python -m label_studio status

# 4. 全量或单条推送（CLI 走 upload.enabled，与 hook.enabled 互不串）
uv run python -m label_studio upload --dry-run
uv run python -m label_studio upload --task-id T007
```

orchestration 侧的自动推送走**另一个开关**：在 `config/config.yaml` 顶层
`label_studio.hook.enabled: true` 打开（默认关），每个 task 跑完自动推一条。

```yaml
label_studio:
  base_url: "http://127.0.0.1:8099"
  api_key: "${LABEL_STUDIO_API_KEY}"
  project_title: "trajectory-sft-quality"
  upload:
    enabled: false          # 只管 `python -m label_studio upload`（全量）
  hook:
    enabled: false          # 只管 step 11 自动推送（单条）
    hook_timeout_seconds: 30.0   # PAT refresh + PATCH label_config + 建 task + 预标注
  credential_scan:
    enabled: true
    on_hit: reject_task     # R11 命中 → fail-closed 整条拒推, 不静默脱敏
```

| 约束 | 说明 |
|---|---|
| 绝不阻塞主流程 | LS 挂了 / 认证失败 / 超时，task 终态照常落库。`ls_hook` 全函数不抛异常 |
| 独立超时 | `ThreadPoolExecutor` + `future.result(timeout=)` + **显式 `shutdown(wait=False)`**。用 `with` 会在退出时 join，超时白设。默认 **30s**——一次推送串完 PAT 刷新 + 查项目 + PATCH label_config + 建 task + 预标注 |
| 超时不丢样本 | `push_single_c3` **先拿 LS task id 再写台账**，所以「台账没有」严格等价于「LS 上没建成」。批次后 `scripts/label_studio.bat upload` 幂等补推 |
| project 只解析一次 | 进程级缓存 `(base_url, project_title) → project_id`。`resolve_project_id(sync=True)` 会 PATCH label_config —— 那是**覆盖不是同步**，每 task 调一次会把标注员在 LS 上的调整冲掉 |
| 台账并发写有锁 | `--parallelism ≥2` 时 N 个 Pool worker 写同一 jsonl。锁加在 `<台账>.lock` 上（不污染 jsonl）。实测无锁时 4 进程 × 40 条只落 66/161 行 |
| 默认关闭 | 只看 `hook.enabled`，不看 `upload.enabled`——两个开关交叉会让 hook 静默空转 |
| 指标不互相污染 | LS 侧**自己**抛的 `TimeoutError` 与"我们等超时"共用同一异常类型，代码用 `future.done()` 区分 `ls_hook_failed` / `ls_hook_timed_out` |
| 凭据不外泄 | `api_key` 只经 env 或 `api_key_path` 解析；异常消息由 `label_studio/errors.py::redact` 脱敏；R11 扫描命中只记位置不记原文 |

**低分样本也会推**（2026-09-29）：gdr 判 `judge_discard` 的 session 结构合格、
只是评分低，按 CLAUDE.md「数据保留原则」仍走完 etl 出 C3 并推 LS 供人工复核。
终态仍是 `PHASE_AUDITED` —— 产出了 C3 不等于洗成 `done`，否则 status 统计会
把低分样本算成正常样本。`audit_reason` 一路透传进 C3 meta，评分卡据此打低分
标记（label_config 最上方的只读块 + 风险提示第一条）。

`scoring_reject` 推不了：gdr 那一支刻意不写 C2（「C2 即为待训练产物，拒收样本
不该占训练目录」），没有 C2 就走不到 etl。设计硬墙，不是遗漏。

| 关注点 | 文档 |
|---|---|
| 用户视角（评分卡为什么标 `source` / 哪类样本被拒推 / 常见问题） | [`docs/observability-label-studio.md`](../docs/observability-label-studio.md) |
| 设计基线（架构转向、评分卡设计、模糊点处理） | [`docs/设计方案/label-studio-integration.md`](../docs/设计方案/label-studio-integration.md) |
| `scorecard.v1` 字段级 schema（C4 契约） | [`docs/contracts/C4-scorecard.md`](../docs/contracts/C4-scorecard.md) |
| orchestration 侧接入点 | [`docs/orchestration-design.md`](../docs/orchestration-design.md) §3.2 / §6.8 |

## Windows 控制台窗口抑制（2026-09-23）

pytest / IDE 测试运行器在 Windows 上跑 `multiprocessing.Pool` worker 或
`daemon.start_detached` 子进程时，会弹一个 cmd 终端窗口挤占桌面。
[`orchestration/_windows.py`](_windows.py) 提供幂等的
`install_no_window_policy()`，自动安装于：

- `orchestration.daemon` module 加载时 → `start_detached` 的 `creationflags`
  显式 OR 上 `CREATE_NO_WINDOW`（`0x08000000`）
- `orchestration.pipeline_executor` module 加载时 → monkey-patch
  `_winapi.CreateProcess`，对 cmd 含 `--multiprocessing-fork` 指纹
  （CPython 3.12 multiprocessing spawn worker 唯一标记）的调用，把
  `dwCreationFlags=0` 补成 `CREATE_NO_WINDOW`
- `tests/conftest.py` 顶部 import 时 → pytest 自身启的子进程也覆盖

**对调用方透明**——`multiprocessing.Pool(...)` / `subprocess.run([python, ...])`
调用语法不变；非 Windows 平台自动 no-op。详见
[`docs/orchestration-design.md` §6.7](../docs/orchestration-design.md)。

## 与 `simulate_serve` CLI 的差异（重要）

新架构 `python -m simulate_serve` 只保留只读开关（`--validate-config` /
`--check-tools` / `--readiness` / `--list-interrupted`），任务运行入口**全部
移交** `python -m orchestration`。`simulate_serve` 删除了 `--tasks` /
`--rerun-task` / `--limit` / `--include-offline` / `--max-run-retries` 等
任务运行开关。

`run.bat` 不识别 `--include-offline`（orchestration 子命令本身没有这个选项）。
`offline_only` 任务（T052/T053）的 `test_fixture` 不会被远端 QwenPaw 消费
（fixture 仅本地注入，按 `CLAUDE.md` 与
[`tests/unit/test_catalog_v2.py`](../tests/unit/test_catalog_v2.py) 强制不进入远端消息），
但 orchestration 会无差别提交它们。要跳过离线 fixture 任务，需在 `--tasks` 中
显式列出其他 task_id，或用 `--all-tasks` + 后置 SQL 清理。

`run.bat` 与 `python -m simulate_serve` 的过滤策略差异：

| 维度 | `simulate_serve` | `orchestration` |
|---|---|---|
| `offline_only` 默认 | 跳过（除非 `--include-offline`） | 不过滤（见 `producer_simulate.py`） |
| unready task 默认 | 过滤 | 过滤（受 config `skip_unready_tasks` 控制） |
| 显式 `--tasks` 视为用户意图 | 是 | 是（显式列出不受上述过滤约束） |

## 依赖与环境

优先使用 `uv run python`（已安装 `uv` 时）；否则 fallback 到系统 `python`。
Windows 下推荐 `uv`，可保证依赖与 lock 文件一致。运行前先执行：

```powershell
uv sync --group dev
```

## 目录约定

```
orchestration/
├── __init__.py
├── __main__.py          CLI 入口（start / status / stop / replay）
├── master.py            Master 包装（持有 cfg / queue / stop_event，调 PipelineExecutor）
├── pipeline_executor.py PipelineExecutor（multiprocessing.Pool 调度）+ PipelineSummary
├── task_pipeline.py     _worker_init + _run_one_task_pipeline（子进程入口）
├── settings.py          PipelineSettings + Paths（frozen dataclass）
├── producer_simulate.py simulate_serve in-process 单 task 入口（run_one_task）
├── config_loader.py     加载 OrchestrationConfig（强类型 dataclass）
├── queue/
│   ├── schema.sql       tasks 表 schema（删 batches / run_tasks）
│   └── sqlite_queue.py  SQLiteQueue（upsert_task / mark_phase / mark_failed / requeue_dead）
├── workers/
│   ├── base_worker.py   _output_filename（工具函数）+ gdr/etl 模块入口
│   ├── gdr_worker.py    run_gdr_once（顶层函数）
│   └── etl_worker.py    run_etl_once（顶层函数）
├── failure_handler.py   dead task 归档（reap_dead，删 batch_id）
├── health.py            collect_tasks + write_health
├── daemon.py            pid_file + STOP 哨兵 + 日志（start_detached / start_foreground / stop）
├── ls_hook.py           step 11: C3 + 评分卡 → Label Studio 旁路推送（全函数不抛异常）
├── criterion_source.py  只读取 output/runs/<run_id>/ 的 ValidationReport.criteria，etl 阶段注入 C3
├── errors.py            OrchestrationError 等
```

终点模块 [`label_studio/`](../label_studio/) **不在上表** —— 它不被 orchestration
主链路 import，只由 `ls_hook` 调用。