# agent-smoke 实施报告 (2026-09-22)

> Pipeline 重构第 2 轮第 2 组 — 真实 smoke 端到端
> 任务包对应 `round-2-execution-plan.md` §agent-smoke
> 关键约束: **不改 orchestration / simulate_serve / gdr / etl 业务代码**

## 完成度

- [ ] **阻塞**:发现 orchestration task_pipeline 子进程入口存在 P0 bug,所有真实 smoke **全部走 dead**;无法验证契约产物。
- [x] dry-run / 配置文件 / status 子命令验证通过。
- [x] 5 次真实 smoke 跑通 (失败但流程跑通),统计齐全。

## 关键发现 (P0 阻塞项)

### BUG-1: `task_pipeline._run_one_task_pipeline` 调错异步函数

- **文件**:`orchestration/task_pipeline.py:258`
- **现状**:
  ```python
  from orchestration.producer_simulate import run_one_task as _run_sim
  ...
  run = _run_sim(task_id, config_path=paths.simulate_serve_config)  # ← 返 coroutine,没 await
  ```
- **应当**:
  ```python
  from orchestration.producer_simulate import run_one_task_sync as _run_sim
  ```
- **症状**:`run = _run_sim(...)` 返回 coroutine 对象;`run.state` 触发 `AttributeError: 'coroutine' object has no attribute 'state'`;任务 100% 走 dead。
- **根因**:`producer_simulate.run_one_task` 是 `async def` (coroutine function),`run_one_task_sync` 才是 `asyncio.run` 包装的同步版;ST-5 重构时 `_run_one_task_pipeline` 选错了名字。
- **旁证**:
  - `producer_simulate.py:40` `async def run_one_task(...)`
  - `producer_simulate.py:132` `def run_one_task_sync(...)` (供 ST-5 子进程入口调)
  - docstring `producer_simulate.py:121` 明确写 "PipelineExecutor 子进程入口在子进程内 pickle / spawn 重启, 不能直接 ``await``; 用 ``asyncio.run`` 跑 async 链路"
- **影响**:所有真 smoke (parallelism=1 或 ≥2) 全部 100% 走 dead。
- **修复成本**:1 行 import 替换 + 2 行函数 docstring 校对,不影响契约。
- **建议**:主 agent 直接修 (P0,与契约不冲突,纯实现层 bug)。

## smoke 跑通情况

| 任务 | 命令 | 是否跑通 | 耗时 (含 daemon 启动) | 产物数 | 失败 task 列表 |
|---|---|---|---|---|---|
| 单 task | `start --tasks T001 --parallelism 1` | ❌ dead | 1.64s (orchestration 报) | 0 | T001 |
| 3 task 串行 | `start --tasks T001,T002,T003 --parallelism 1` | ❌ 全部 dead | 1.73s (orchestration 报) | 0 | T001,T002,T003 |
| 3 task 并行 | `start --tasks T001,T002,T003 --parallelism 4` | ❌ 全部 dead | 1.78s (orchestration 报) | 0 | T001,T002,T003 |
| 5 task 串行 | `start --tasks T001,...,T005 --parallelism 1` | ❌ 全部 dead | 1.95s (orchestration 报) | 0 | T001..T005 |
| 8 task 并行 | `start --tasks T001..T008 --parallelism 4` | ❌ 全部 dead | 2.20s (orchestration 报) | 0 | T001..T008 |
| 全 catalog | (跳过) | — | — | — | — |

**注**:耗时是 daemon 启动 + 调度 + 子进程 crash 的总时长,实际子进程跑的时间远小于此。

## SQLite phase 计数

每次 smoke 后 (DB 文件被清空再生成):

| smoke | done | dead | pending | simulate | gdr | etl | total |
|---|---|---|---|---|---|---|---|
| 单 task | 0 | 1 | 0 | 0 | 0 | 0 | 1 |
| 3 task 串行 | 0 | 3 | 0 | 0 | 0 | 0 | 3 |
| 3 task 并行 | 0 | 3 | 0 | 0 | 0 | 0 | 3 |
| 5 task 串行 | 0 | 5 | 0 | 0 | 0 | 0 | 5 |
| 8 task 并行 | 0 | 8 | 0 | 0 | 0 | 0 | 8 |

**所有 task 状态 `dead`,`error_msg` 全部为**:
```
pipeline crashed: AttributeError: 'coroutine' object has no attribute 'state'
Traceback (most recent call last):
  File "...\orchestration\task_pipeline.py", line 279, in _run_one_task_pipeline
    run_state = getattr(run.state, "value", str(run.state))
                        ^^^^^^^^^
AttributeError: 'coroutine' object has no attribute 'state'
```

## 性能对比

**无意义** — 全部 task 在子进程入口第一行就 crash,实际工作量为零;duration (1.64 ~ 2.20s) 全是 daemon 启动 / SQLite 写盘 / multiprocessing.Pool 子进程 fork 开销。

修复 BUG-1 后需重跑才能拿到真实性能数据。

## 契约产物验证

| 契约路径 | 期望 | 实际 |
|---|---|---|
| `output/runs/<run_id>/run.json` | C1 模拟器落盘 | ❌ 0 份 |
| `output/agent_trajectory/<run_id>__<session_id>.json` | C1 trajectory | ❌ 0 份 |
| `output/refined/<task_id>__<session_id>.json` | C2 gdr 产物 | ❌ 0 份 |
| `output/refine_data/<task_id>__<session_id>.{messages,openai,qwenjina.txt,meta}.json` | C3 4 视图 | ❌ 0 份 |
| `output/orchestration/orchestration.db` | phase 计数 | ✅ 全部 dead,attempts 全 0 |
| `output/orchestration/logs/health.json` | 健康文件 | ✅ 落盘 |
| `output/orchestration/dead/` | 死信归档 | ❌ 空 (failure_handler 未触发) |

## 全 catalog smoke

**未跑**。理由:
1. BUG-1 让所有 task 100% 失败,跑全 catalog 只会浪费时间和日志空间。
2. 修复 BUG-1 后,98 task × 4 并行 + LLM 调用配额仍需要人工评估 (LLM API 调用一次 T001 跑通 90s,98 task 串行 ≥2.5 小时;并行可压缩到 ≥30min 但需要 4×LLM 配额)。
3. 建议:第 3 轮先修 BUG-1 + 重跑 5 task 串行 smoke 验证产物路径齐全 + 再决定是否跑全 catalog。

## 验证过的非 smoke 入口

| 命令 | 状态 |
|---|---|
| `python -m simulate_serve --validate-config` | ✅ Catalog valid: tasks=98 diagnostics=0 |
| `python -m orchestration start --tasks T001 --dry-run --parallelism 1` | ✅ 打印配置 |
| `python -m orchestration status` | ✅ 6 phase 字段齐全 + 计数正确 + recent_tasks 列表 |
| `python -m orchestration --help` | ✅ 子命令齐全 |

## 设计调整与遗留问题

### A. 业务代码 bug (P0)

BUG-1 必须修,否则任何真实 smoke 都跑不通。建议列入第 3 轮 P0,与契约不冲突 (只是 task_pipeline 实现层调用错函数名)。

### B. 死信归档未生效

修复 BUG-1 后,失败 task 应该被 `failure_handler` 归档到 `output/orchestration/dead/`,但本次 smoke 看不到 dead 文件 (因为 task_pipeline 顶层 try/except 已经把异常吞了,只 mark_failed 到 SQLite,没走 failure_handler 路径)。

需要确认 `mark_failed` → `failure_handler.handle_dead_task` 的链路是否真的在 task 失败时被触发。

### C. 子进程入口未上报子进程级异常日志

`RuntimeWarning: coroutine 'run_one_task' was never awaited` 是子进程内 asyncio 抛的,但 `_run_one_task_pipeline` 顶层 try/except 只 catch 同步异常;coroutine 没被 await 本身没抛同步异常,只产生 RuntimeWarning。建议加 `inspect.iscoroutine(run)` 守卫或直接换 `run_one_task_sync`。

### D. simulate_serve 也未直接调用本地工具

dry-run / status OK;真实 LLM 调用 (QwenPaw) 尚未在本机起服务,QwenPaw `http://localhost:8088` 当前无响应。但修复 BUG-1 后,子进程会真正调用 `run_one_task_sync → AsyncQwenPawExecutor`,如果 QwenPaw 不可达会进入 simulate 阶段失败 (executor_error),仍走 dead。所以即使修 BUG-1,本机 smoke 仍需要 QwenPaw 服务在线。

## 跨组集成疑问

1. **agent-orchestration 重构时是否真的测试过 task_pipeline?**
   - `tests/orchestration/test_task_pipeline.py` 11 个测试存在,但测试都用 monkeypatch 替换 `_run_sim` 为 stub 函数,返回 `TaskRun` 对象而非 coroutine — 所以测试全绿但生产代码实际从未跑通过。
   - 建议下一轮加一个"不 mock `_run_sim`" 的真子进程集成测试,验证 `_run_one_task_pipeline` 真的能跑 simulate。
2. **agent-business 重构时为什么提供两个 `run_one_task`?**
   - `producer_simulate.run_one_task` (async) 和 `run_one_task_sync` (sync wrapper) 都在,使用方应调 sync 版但 ST-5 `_run_one_task_pipeline` 选错了 — 建议把 async 版重命名 `run_one_task_async` 或加 deprecation warning。

## 契约问题 (只写不改)

1. **契约 §5.4 / §5.5 描述子进程入口为可 pickle 模块函数,这一条没问题**。但契约没明文要求子进程入口必须直接返回 TaskRun 对象 (而非 coroutine) — 也许应该加一句 "子进程入口函数必须为同步函数,async 入口应通过 asyncio.run 包装为 sync"。
2. **契约 §7.2 smoke 章节** 已写 "真实 catalog 端到端 smoke" 是验证项之一,本次未达成;应在下一轮修复 BUG-1 后补做。

## 报告模板要求的统计

- 完成度: 部分完成 (5 项 smoke 流程跑通,但全部因 BUG-1 走 dead)
- 失败率: 100% (BUG-1 让所有 task 必 fail)
- 耗时: 全部 < 3s (子进程入口即崩,无实际工作)
- 产物数: 0 (无 C1/C2/C3 产物)
- SQLite phase: 全部 dead,attempts_simulate=0,attempts_gdr=0,attempts_etl=0

## 下一步建议 (给主 agent)

| 优先级 | 动作 |
|---|---|
| **P0** | 修 BUG-1: 1 行 import 替换 (`run_one_task` → `run_one_task_sync`) |
| P1 | BUG-1 修后重跑 5 task 串行 smoke 验证产物路径齐全 |
| P1 | 加 `test_task_pipeline.py::test_real_run_one_task_returns_TaskRun_not_coroutine` 集成测试 |
| P2 | BUG-1 修后跑全 catalog smoke (98 task,parallelism=4) — 评估 LLM 配额 |
| P3 | 查 `failure_handler.handle_dead_task` 是否在 task_pipeline 顶层 catch 后触发 |
