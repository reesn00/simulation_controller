# orchestration · 顶层调度器

把 `simulate_serve` / `etl/qwenformat` / `gdr` 三个独立子系统串成
`trajectory → qwenformat → gdr` 的三阶段流水线。

设计、决策、契约见 [`docs/orchestration-design.md`](../docs/orchestration-design.md)。

## 概述

入口模块：`python -m orchestration`（或 `orchestration\run.bat` 的 Windows 包装）。
子命令：`start` / `status` / `stop` / `replay`，与 `master.py` / `queue/sqlite_queue.py` /
`workers/*.py` 协同工作。

进程模型：master 主线程跑批循环，qf/gdr/watcher 都是常驻 Thread + 独立 stop_event；
`reap_stale` 走独立 Thread 周期回退卡死的 `*_processing` 任务；停止通道为
SIGINT/SIGTERM/SIGBREAK + STOP 哨兵文件双保险（Windows detach 子进程无控制台，
CTRL_BREAK_EVENT 不可达，哨兵文件是唯一可靠通道）。

## Windows 包装脚本 `run.bat`

> 包装脚本**必须保持 ASCII 注释**：Windows cmd.exe 在打开 `.bat` 时就锁定代码页，
> 后续 `chcp 65001` 不能重新解码同一文件，非 ASCII 注释会被 GBK 936 破坏而
> 触发 `'xx' 不是内部命令` 报错。详细中文说明放在本文档，bat 内只保留英文
> quick reference。

`run.bat` 把 `%*` 透传给 `python -m orchestration %*`，等价于直接调用 CLI。
会自动检测并优先使用 `uv run python`，未安装 `uv` 时回退到系统 `python`：

```powershell
# 后台跑全部 task（含 E001-E030 评估集）
orchestration\run.bat start --all-tasks --detach

# 前台跑 T001/T003，跑完即退
orchestration\run.bat start --tasks T001,T003

# 前台跑评估集 E001-E003（与 T 任务 schema 一致，行为无差）
orchestration\run.bat start --tasks E001,E002,E003 --batch-size 3

# 仅打印计划（config / sqlite_db / pid_file / 批次切分），不真启动
orchestration\run.bat start --all-tasks --dry-run

# 查看队列 / 进程 / dead 状态
orchestration\run.bat status

# 优雅停止（30s 超时后 taskkill 强杀）
orchestration\run.bat stop --timeout 30

# 重放 batch_id=7 的 dead task
orchestration\run.bat replay --batch 7
```

### 全局选项（必须放在子命令前）

- `--config PATH` 配置 yaml 路径。默认仓库根 `config/config.yaml`，也可用
  `SIMCTL_CONFIG` 环境变量重定向。配置缺失直接报错，无兜底。
  详见 [`docs/refactor-implementation-plan.md`](../docs/refactor-implementation-plan.md)
  与 `CLAUDE.md` 的"配置和工具"章节。

### `start` 子命令选项

- `--detach` 后台化（写 `pid_file` + 子进程，父进程立即返回）。查看后台状态
  用 `run.bat status`；停止用 `run.bat stop`。
- `--foreground` 前台运行（默认；`--detach` 子进程内部会传此标志，用户一般
  不需要手动加）。
- `--dry-run` 仅打印计划，不真的启动 worker。
- `--tasks T1,T2,...` 逗号分隔的 task_id 列表；显式指定视为用户意图，与
  `python -m simulate_serve --tasks` 一致语义。
- `--all-tasks` 加载 `simulate_serve` 全部 task_id 并提交（默认从
  `config.tasks_file` 解析）。
- `--stay` 批次跑完后继续常驻；默认跑完即退出。
- `--batch-size N` 覆盖 config 中的 `batch_size`（默认 1）。
- `--exit-when-done` 已废弃：跑完即退现在是默认行为，保留仅为兼容旧命令。
  等价于不加任何停留选项。

### `status` 子命令

打印 `health.json` + `queue_counts` + dead 列表（含每批次的阶段时间戳
`sim@/sim!` `qf@/qf!` `gdr@/gdr!`，`@`=开始 `!=`收尾）和 `sqlite_db` 是否存在。

### `stop` 子命令

- `--timeout SECONDS` 优雅停止超时秒数（默认 10）。写 STOP 哨兵文件让 master
  优雅 shutdown；超时后 `taskkill /F /T` 强杀。

### `replay` 子命令

- `--batch BATCH_ID` 仅重放指定 batch_id 的 dead；缺省 = 全部。
  `state=dead` 的 task 重置为 `pending` 重新入队。

## 与 `simulate_serve` CLI 的差异（重要）

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
├── master.py            主循环 + 批次切分
├── producer_simulate.py simulate_serve 子进程管理
├── watcher.py           trajectory 入队
├── queue/
│   └── sqlite_queue.py  单文件 SQLite 队列状态机
├── workers/
│   ├── base_worker.py   pull-process-mark 通用循环
│   ├── qf_worker.py     etl/qwenformat 处理
│   └── gdr_worker.py    gdr 三级精修
├── failure_handler.py   dead task 归档
├── health.py            health.json 写入
├── daemon.py            pid_file + signal + STOP 哨兵 + 日志
├── batch_tracker.py     run.json.state 终结态等待
├── config.yaml          默认配置（被打进 wheel）
├── run.bat              Windows 包装（ASCII-only）
├── data/                运行时数据：SQLite 队列 / qf_out / pid / dead
└── logs/                运行时日志
```
