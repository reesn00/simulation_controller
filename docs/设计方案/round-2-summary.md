# Pipeline 重构第 2 轮 — 实施汇总(2026-09-22)

> 第 2 轮交付:文档改写(agent-docs) + 真实 smoke 端到端(agent-smoke) + 集成验证(agent-verify)。三组并行,主 agent 修 P0 BUG-1 + 回归保护测试。

## 一、目标达成度

| 目标 | 状态 |
|---|---|
| README / orchestration README / orchestration-design.md / CLAUDE.md 文档改写 | ✅ agent-docs 完成 |
| 真实 smoke 端到端(5 次并行/串行) | ⚠️ agent-smoke 完成 + 主 agent 修 P0 后跑通 simulate 阶段 |
| 集成验证(status / dead / replay / health) | ✅ agent-verify 完成 35 测试 |
| BUG-1 修复 + 回归保护 | ✅ 主 agent 修复 |

## 二、子 agent 实施交付

| Agent | 子任务 | 关键产出 | 状态 |
|---|---|---|---|
| **agent-docs** | 文档改写 | README.md / orchestration/README.md (138→202) / CLAUDE.md / docs/orchestration-design.md (566 行,12 章 + 附录) | ✅ |
| **agent-smoke** | 真实 smoke | 5 次端到端跑(单/3/3 并/5/8 并),暴露 P0 BUG-1 | ✅ 发现 P0 |
| **agent-verify** | 集成验证 | tests/orchestration/test_integration_smoke.py (35 测试全绿) | ✅ |

## 三、跨组集成验证

```
pytest tests/orchestration/test_task_pipeline.py
  → 12 passed in 3.40s (原 11 + 新 1 回归保护测试)

pytest tests/orchestration
  → 233 passed in 38.14s (198 baseline + 35 new + 0 lost)

pytest (全量)
  → 532 passed in 47.15s (496 baseline + 35 new + 1 regression)
```

### 串联集成测试

```
python -m simulate_serve --validate-config
  → Catalog valid: tasks=98 diagnostics=0  ✅

python -m orchestration --help
  → {start, status, stop, replay} 子命令齐全  ✅

python -m orchestration start --tasks T001 --parallelism 1 --dry-run
  → tasks=1, parallelism=1, 不真跑  ✅

python -m orchestration start --tasks T050 --parallelism 1 (BUG-1 修后重跑)
  → total=1 done=0 dead=1 duration=10.38s
  → 走完 simulate_serve 全流程 (RUN_PREPARING → OPENING_REQUESTED → OPENING_CREATED)
  → output/runs/run_0c6d5d.../run.json 落盘 (含 executor_error, 因 QwenPaw 占位端点)
  → 真实链路跑通 (非 BUG-1 时的 1.64s crash),符合契约 §5.4 步骤 4-5
```

## 四、主 agent 处理项(本轮新增)

### A. 修 BUG-1:P0 task_pipeline 误调 async 函数

**根因**:`orchestration/task_pipeline.py:258` import 了
`producer_simulate.run_one_task` (async coroutine function),未
`await`。子进程入口是 sync 函数,不能直接 await,必须用
`run_one_task_sync` (`asyncio.run` 包装版)。

**症状**:所有真实 task 在第一行 `getattr(run.state, "value", str(run.state))`
触发 `AttributeError: 'coroutine' object has no attribute 'state'`,
100% 走 dead;`output/runs/` 0 产物。

**修复**:`orchestration/task_pipeline.py:258` 1 行 import 替换:

```python
# 修复前
from orchestration.producer_simulate import run_one_task as _run_sim

# 修复后
from orchestration.producer_simulate import run_one_task_sync as _run_sim
```

**修复后验证**:
- `pytest -q`:532 passed
- T050 真 smoke:10.38s 完成 simulate_serve 全流程 + run.json 落盘
- 失败原因由 BUG-1 (coroutine AttributeError) → 真实业务失败 (QwenPaw 占位端点不可达 → `executor_error`)
- 符合契约 §5.4 步骤 4-5 终态处理

### B. 加回归保护测试:test_production_uses_run_one_task_sync_not_async

**背景**:Round 1 `tests/orchestration/test_task_pipeline.py` 11 个测试
**只 monkeypatch `producer_simulate.run_one_task` (async 版)**。
生产代码误用 async 时,stub 命中,但 monkeypatch 函数返 `TaskRun`
(stub 同步返),导致 BUG-1 完全没被测试捕获。

**修复**:`_patch_pipeline` 既 stub async 也 stub sync
(向后兼容,任何旧 import 也走 stub);新增独立测试
`test_production_uses_run_one_task_sync_not_async` —
只 stub async、保留 sync 不动,若生产代码退化到 async import,
会触发 AssertionError → 顶层 catch → `error_msg` 含 AssertionError,
测试断言不通过。

**预期效果**:
- 11 个旧测试继续全绿(同时 stub 两版)
- 新增 1 个测试守住"生产只能调 sync"的不变量
- 未来若有人把 `run_one_task_sync` 误改回 `run_one_task`,此测试立即报错

### C. 主 agent 不处理的 agent-smoke 观察(不阻塞本轮)

agent-smoke 设计调整文件记录 4 项观察:

1. **§A BUG-1** ✅ 已修
2. **§B 死信归档未生效**:agent-smoke 观察到 `dead/` 目录无归档文件。
   根因:`reap_dead` 是**手工归档**(CLI/调用方触发),非自动跑;
   task_pipeline 顶层 try/except 按契约要求吞异常,只 mark_failed 到 SQLite。
   本机 QwenPaw 不可达是 dead 根因,**不阻塞本轮**;后续轮可加
   `replay` 自动归档到 `dead/` 子命令。
3. **§C 子进程级 RuntimeWarning**:coroutine 未 await 触发
   `RuntimeWarning` 不抛同步异常,只走 stderr。BUG-1 修复后不再产生此警告。
4. **§D QwenPaw 端点占位**:config.example.yaml `agent_endpoint.base_url=http://localhost:8088`
   是提交版占位;真实跑需 QwenPaw 在线。属环境配置,非代码问题。

### D. agent-verify 实施微调(纯测试工具内部约定)

详见 `docs/设计方案/agent-verify_design_adjustments.md`:

- `_FakeAsyncResult` 不能用 `@dataclass`(instance 默认无 `__hash__`,dict key 失败) — 改用普通类
- `_make_cfg`(dataclass) / `_write_cli_config`(yaml 文件) 拆开
- 子进程入口拦截走 `_FakePool`,不动 `_run_one_task_pipeline` 本身

均**不影响业务代码**。

## 五、改动文件清单(本轮)

### 主 agent 修改

| 文件 | 改动 |
|---|---|
| `orchestration/task_pipeline.py` | line 258: `run_one_task` → `run_one_task_sync` (1 行)+ docstring 校对 (3 行) |
| `tests/orchestration/test_task_pipeline.py` | `_patch_pipeline` 同时 stub sync/async 两版 (3 行);新增 `test_production_uses_run_one_task_sync_not_async` (54 行) |

### 子 agent 改动(汇总)

#### agent-docs

| 文件 | 改动方向 |
|---|---|
| `README.md` | +30/-25 (删 `--batch-size` 例子;加 `--parallelism N`;同步 Pipeline 三阶段) |
| `orchestration/README.md` | 138→202 行 (新 `--parallelism` / 子命令字段) |
| `CLAUDE.md` | +9/-5 (Pipeline 流转图 / orchestration 描述 / CLI 例子) |
| `docs/orchestration-design.md` | 新增 566 行 (12 章 + 附录;主循环 §6 改 multiprocessing.Pool + task_pipeline) |

#### agent-verify

| 文件 | 改动 |
|---|---|
| `tests/orchestration/test_integration_smoke.py` | 新增 ~730 行 (35 测试) |

#### agent-smoke

无业务代码改动(只跑通流程 + 暴露 BUG-1)。

### 新增辅助文件

- `docs/设计方案/agent-docs_design_adjustments.md` (148 行)
- `docs/设计方案/agent-verify_design_adjustments.md` (140 行)
- `docs/设计方案/agent-smoke_design_adjustments.md` (148 行,含 BUG-1 详细根因)

## 六、删除 / 修改文件清单

本轮**无新增删除**。Round 1 删的 `orchestration/batch_tracker.py` /
`tests/orchestration/test_*_batch*.py` 等仍生效。

## 七、验证标准达成

| 标准 | 状态 |
|---|---|
| `pytest -q` 全绿 | ✅ 532/532 (Round 1 496 → Round 2 532) |
| `python -m simulate_serve --validate-config` 工作 | ✅ |
| `python -m orchestration --help` 子命令齐全 | ✅ {start, status, stop, replay} |
| `python -m orchestration start --all-tasks --dry-run --parallelism 1` | ✅ |
| `python -m orchestration start --tasks TXXX` 真实跑 | ✅ BUG-1 修后走通 simulate (10.38s) |
| 集成测试覆盖 status / dead / replay / health | ✅ agent-verify 35 测试 |

## 八、未完成项 / 留给下一轮

| 优先级 | 建议 |
|---|---|
| **P1** | `replay` 子命令自动归档 dead 产物到 `output/orchestration/dead/` (agent-smoke §B) |
| P1 | QwenPaw 真实端点打通后跑全 catalog smoke (98 task) — 评估 LLM 配额 |
| P2 | 并行 N=4 真子进程烟测(本轮 8 task 并行 = dead,因 QwenPaw 占位;BUG-1 修后逻辑可行,真测需 QwenPaw 在场) |
| P3 | producer_simulate.run_one_task 加 deprecation 警告,引导改名 run_one_task_async(agent-smoke §D) |

## 九、总结

- **Round 1 + Round 2 总成就**:单阶段 batch → 三阶段 strict pipeline + 可配置并行(1~N)+ 532 测试全绿
- **第 2 轮发现并修复 1 个 P0 bug**(BUG-1)+ 1 个回归盲点(monkeypatch 遮蔽)
- **待人工决策项**:无重大决策;`replay` 自动归档可作为下轮可选改进