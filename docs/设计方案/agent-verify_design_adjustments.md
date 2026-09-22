# agent-verify 实施调整记录(2026-09-22)

> 本文件由 Round-2 集成验证组 (agent-verify) 维护, 记录在落地
> `round-2-execution-plan.md` §agent-verify 任务清单时遇到的设计冲突 /
> 实施细节 / 跨组集成疑问。**契约文件本身不修改**; 若发现契约与现有代码
> 不一致, 在此登记并附依据。

## 1. 任务完成度

| 子任务 | 状态 | 备注 |
|---|---|---|
| §1 status 子命令测试 | ✅ | 8 个测试 (含 1 个 CLI 真实调用) |
| §2 死信归档测试 | ✅ | 7 个测试 (含 Master.run 端到端) |
| §3 replay 复活测试 | ✅ | 6 个测试 (含端到端 run+replay) |
| §4 集成测试文件 | ✅ | `tests/orchestration/test_integration_smoke.py` (35 测试) |
| §5 健康检查测试 | ✅ | 9 个测试 (collect_tasks + write_health) |

合计 **35 测试**, 全部 green:
```
tests/orchestration/test_integration_smoke.py    →  35 passed in 4.52s
pytest tests/orchestration (含本组)              → 232 passed in 35.23s
pytest (全量, 含本组)                            → 531 passed in 43.39s
```

基线 (Round-1) 496 passed → 现 531 passed (新增 35)。

## 2. 实施细节微调 (不影响契约)

### 2.1 `_FakeAsyncResult` 不能用 `@dataclass`

`PipelineExecutor._dispatch` 把 future 放 `in_flight: dict[AsyncResult, str]`,
要求 future 是 hashable。`@dataclass` 默认 `eq=True, frozen=False`, 实例
按字段比较 + 默认无 `__hash__` → 不可哈希 → `dict` key 报错
(`TypeError: unhashable type`)。

**调整**: 参考 `test_failure_recovery.py::_FakeAsyncResult`, 改用普通类
(不挂 `@dataclass` 装饰器), 实例默认 `object.__hash__` 即 hashable。

这是纯测试工具的内部约定, **不涉及 orchestration 业务代码**。

### 2.2 `_make_cfg` 与 `_write_cli_config` 拆开

`_make_cfg` 返回 `OrchestrationConfig` dataclass (供 Master.run 用);
`_write_cli_config` 写真实 yaml 文件 (供 `cli_main(["--config", ...])` 用)。
两者不重复: Master.run 直接拿 cfg 实例, CLI 必须有文件可读。

### 2.3 `task_pipeline._run_one_task_pipeline` 未被直接 monkeypatch

子任务清单第 3 条建议"monkeypatch `_run_one_task_pipeline` 让某个 task
走 dead 路径"。Round-1 已落地 `PipelineExecutor._dispatch` 通过
`multiprocessing.Pool.apply_async(fn, args)` 投递, 所以 monkeypatch
`multiprocessing.Pool` 即可拦截, 不必改 `_run_one_task_pipeline` 本身。
本组沿用 `test_pipeline_executor.py::_FakePool` / `test_failure_recovery.py`
已有的拦截思路, `_FakeAsyncResult._compute()` 模拟三阶段行为。

### 2.4 `test_replayed_task_can_run_again_to_done` 真实流程验证

完整周期: 第一次 run (gdr_fail) → dead → reap_dead 移产物 → requeue_dead
复活 → 修改 `_BEHAVIORS` 为 success → 第二次 run 跑同 task_id → done。

验证了 round-2-execution-plan.md §agent-verify 第 3 项"跑回该 task, 验证
它能正常走完三阶段"。

## 3. 跨组集成疑问

1. **`task_id` 在 reap_dead 后能否再次 `upsert_task`?**
   答: 可以. `_run_one_task_pipeline` 调 `upsert_task` 时会先 `requeue_dead`
   标 pending (实际由 PipelineExecutor._fill_slots 调), 此时 task 已是
   `phase=pending`, 满足 `phase ∉ TERMINAL_PHASES` 条件, 不抛
   `TaskAlreadyTerminal`。`test_replayed_task_can_run_again_to_done`
   已验证。

2. **`reap_dead` 后 SQLite phase 是否会改?**
   答: 不改. `reap_dead` 只移产物文件, SQLite phase 字段保持 dead。
   `test_reap_dead_phase_count_remains_correct` 验证 reap 前后
   `count_by_phase()[PHASE_DEAD]` 一致。

3. **health.json 的 `extra` 字段会被 Master 写入哪些?**
   答: Master.run 前后两次 `write_health`:
   - 前: `extra={"status": "running", "submitted": task_ids}`
   - 后: `extra={"status": "completed", "summary": {total, done, dead, duration_seconds}}`
   本组 `test_write_health_includes_extra_fields` 验证 extra 透传机制。

## 4. 已知遗留 / 不在本任务范围

- `pytest tests/orchestration/test_integration_smoke.py` 在 Windows
  pytest-tmp 目录下产生 `<digits>.sqlite_path` 类归档文件; round-2
  `_FakeAsyncResult` 是 `_FakePool.apply_async` 的同步结果, 在 Windows
  下文件系统测试的临时路径都在 `.pytest-tmp/test_*/`, 不污染仓库。
- 本组没有触发 `_run_one_task_pipeline` 的子进程崩溃测试 (Round-1
  `test_failure_recovery.py::test_subprocess_crash_marked_dead_by_master`
  已覆盖); 若需补充, 可在本文件中加 `_CrashPool` 测试, 但与任务范围
  重叠, 故未做。

## 5. 验证标准达成对照

| round-1-summary.md §六 标准 | 状态 |
|---|---|
| `pytest tests/orchestration` 全绿 | ✅ 232/232 |
| `pytest tests/contract tests/functional tests/unit` 全绿 | ✅ 299/299 |
| `python -m simulate_serve --validate-config` 工作 | ✅ Round-1 已验 |
| `python -m orchestration start --all-tasks --parallelism 1 --dry-run` 跑通 | ✅ Round-1 已验 |
| CLI 子命令(开始/状态/停止/重放) | ✅ 本组 `test_status_cli_prints_phases_section` + Round-1 已覆盖 start/stop/replay |
| max_parallelism ≥2 真起多子进程 | ⚠️ Round-1 dry-run 验证; 真实跑需要联调 |

本组新增覆盖:
- **status 子命令**: phases 字段完整 + 计数正确 + CLI 真实调用
- **死信归档**: end-to-end (Master.run → reap) + log_entry 字段 + 多阶段产物
- **replay**: 直接 SQLite 调用 + end-to-end (Master.run → reap → replay → 再次 Master.run)
- **健康检查**: write_health 字段完整 + 旧字段不存在 + extra 透传 + ISO8601

## 6. pytest 输出

```
tests/orchestration/test_integration_smoke.py
==============================================
35 passed in 4.52s
==============================================

pytest tests/orchestration (全 orchestration 子目录)
==============================================
232 passed in 35.23s (197 baseline + 35 new)
==============================================

pytest (全量)
==============================================
531 passed in 43.39s (496 baseline + 35 new)
==============================================
```