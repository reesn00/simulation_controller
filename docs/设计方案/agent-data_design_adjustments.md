# agent-data 实施设计调整记录 (ST-1 + ST-2)

> 本文件记录 agent-data 子任务 (Pipeline 重构第 1 轮第 1 组: 数据层)
> 在严格按 `docs/设计方案/pipeline-contracts.md` 实施时遇到的细节决策,
> 供后续 ST-3 ~ ST-7 的子 agent 查阅。

---

## A. 已落地的契约偏离 / 扩展

### A1. `OrchestrationConfig.gdr_settings` 字段类型注解

契约 §1.3 写作 `gdr_settings: Settings` (即 `gdr.config.settings.Settings`).
我实现时把字段注解写为 `gdr_settings: Any`, 实际值仍是 `Settings` 实例.

理由: `orchestration/config_loader.py` 顶层 import `gdr.config.settings`
会让 orchestration 模块依赖 gdr (违反 gdr 是独立 uv workspace 成员的设计).
`Any` 注解 + 内部在 `_build_gdr_settings` 内延迟 import, 保持模块解耦.
调用方仍可通过 `isinstance(cfg.gdr_settings, Settings)` 做类型检查.

### A2. 额外校验规则

契约 §1.5 只显式要求 `max_parallelism ≥ 1`. 我额外加了:
- `max_retry_gdr ≥ 0` 抛 `ConfigValidationError("max_retry_gdr must be ≥ 0")`
- `max_retry_etl ≥ 0` 抛 `ConfigValidationError("max_retry_etl must be ≥ 0")`
- `retry_poll_seconds > 0` 抛 `ConfigValidationError("retry_poll_seconds must be > 0")`
- 任何 `paths.*` 字段为空字符串抛 `ConfigValidationError("paths.{key} must be a non-empty string")`

理由: 与契约 §1.5 末段 "所有 paths 必须非空字符串" 对齐; 重试上限与
轮询秒数的下界检查是显然的 (≤0 没有意义), 不应该让非法值悄悄生效.

不影响其他子 agent: 所有额外异常类型仍是 `ConfigValidationError`,
没有引入新类型. 测试断言在 `tests/orchestration/test_config_loader.py`
内已覆盖.

### A3. `Paths` 字段保持 `Path` 类型 (非字符串)

契约 §1.3 给的字段类型就是 `Path`, 但 YAML 源是字符串. 我在
`_build_paths` 内做 `Path(...)` 转换, 让下游消费者拿到 `Path`.
`OrchestrationConfig.paths.simulate_serve_config` 等字段全部是 `Path`,
与契约一致.

### A4. `simulate_serve_config` 默认指向根配置文件本身

契约 §1.2 写的默认是字符串 `"config/config.yaml"`, 但同时又约定
"统一根配置文件本身就是 simulate_serve 的配置来源". 我保留了旧实现
的兼容行为: 当 YAML 文件含 `simulate_serve:` 段 (根配置格式) 时,
`paths.simulate_serve_config` 自动指向该 YAML 文件本身, 覆盖默认.
用户显式写 `paths.simulate_serve_config: ...` 时优先用用户值.

理由: producer 在模拟阶段需要用同一文件读 simulate_serve 配置;
旧行为已测试覆盖 (`test_load_config_root_format_sets_simulate_serve_config`),
不应在新架构里悄悄改变.

### A5. `mark_phase` 的"None 不覆盖" 语义

契约 §2.5 `mark_phase` 列出所有路径字段为 keyword-only 参数,
未规定 None 的语义. 我采用:
- 调用方显式传 None → 当作"不更新该字段" (保留旧值)
- 调用方显式传 Path → 更新

理由: task 推进时通常只更新"当前阶段"的产物路径, 旧阶段路径保留
便于调试与审计. 若调用方想清空某字段, 应显式写空字符串 / 走
`upsert_task` 重置路径.

如果其他子 agent (ST-3/4/5) 需要"显式 None 即清空"的语义, 请告诉我
我再调整.

### A6. `SQLiteQueue.__exit__` 不关闭外部资源

契约 §2.5 列出 `__enter__` / `__exit__` 作为上下文管理方法.
当前实现每次方法调用都开新 connection, 没有外部资源需要释放, 所以
`__exit__` 是 no-op (`return None`). 这让 `with SQLiteQueue(...) as q:`
可以正常工作, 但 `__exit__` 不会做实际清理 — 与契约"接口存在" 一致,
不意味着有副作用.

---

## B. 实施过程中的发现 (供后续子 agent 参考)

### B1. 其他模块 import 旧 API — 预期 ST-3/6/7 处理

我按契约 §2.8 删除 `SQLiteQueue` 的 16 个旧方法, 导致以下模块在
`pytest tests/orchestration/` 时出现 import error / collection error:

- `orchestration/master.py` (来自 `from orchestration.queue import STATE_DEAD, SQLiteQueue` +
  内部使用 `pull_pending_gdr` / `pull_pending_etl` / `mark_gdr_done` /
  `mark_etl_done` / `_pull_n` 等)
- `orchestration/__main__.py` (使用 `STATE_DEAD` / `count_by_state` /
  `requeue_dead(batch_id=...)`)
- `orchestration/health.py` (使用 `collect_batches`, 调 `count_by_state`)
- `orchestration/failure_handler.py` (`reap_dead` 的 `batch_id` 字段)
- `orchestration/watcher.py` (使用 `STATE_DEAD`, `mark_dead`)
- `orchestration/producer_simulate.py` (使用 `STATE_DEAD`)
- `orchestration/workers/{base,gdr,etl}_worker.py` (使用 `BaseWorker` /
  `GdrWorker` / `EtlWorker` 旧类)
- 它们的对应测试文件 (`test_master.py`, `test_smoke_3task.py`,
  `test_failure_recovery.py`, `test_direction_b_batch_isolation.py`,
  `test_watcher.py`, `test_orchestration_cli.py`, `test_daemon.py`,
  `test_batch_tracker.py`, `test_failure_handler.py`,
  `test_health.py`, `test_stage_timestamps_naming_backoff.py`,
  `test_system_prompt.py`, `test_gdr_worker.py`,
  `test_gdr_worker_nonretryable_status.py`, `test_producer_simulate.py`)

这些错误按任务约束"不改公共文件"已不属于我的修改范围, ST-3 / ST-4 /
ST-5 / ST-6 / ST-7 子 agent 应在各自步骤内修复.

### B2. `db_path` 已作为公开属性暴露

契约 §2.5 未列 `db_path`, 但 §2.7 明确"子进程内重新构造
`SQLiteQueue(paths.sqlite_db)`". 我加了 `db_path` property 方便调用方
确认实例绑定的 db 文件; 测试 `test_db_path_exposed` 覆盖.

### B3. `mock test_run_etl_once_filename_sanitizes_unsafe_chars` 失败
**与本次重构无关**: 该测试在 ST-3 重构之前就因
`tmp_path/s/1.json` 的父目录未创建而失败, 是 etl worker 自身
bug, 不在 agent-data 范围.

### B4. 新增契约里的 `attempts_simulate` 字段

契约 §2.2 在 tasks 表加了 `attempts_simulate` 列 + Task dataclass 加
`attempts_simulate` 字段 + `STAGE_SIMULATE` 常量 (用于
`increment_attempts` 与 `mark_failed`). 旧代码只有 `attempts_gdr` 和
`attempts_etl`. 新字段虽未被 ST-5 PipelineExecutor 在 simulate 阶段
用 (simulate 内部由 `TaskRuntime` 自己重试), 但契约要求支持.

实现已落实:
- `STAGE_SIMULATE = "simulate"` 已 export
- `increment_attempts(..., stage=STAGE_SIMULATE)` 自增 `attempts_simulate`
- `mark_failed(..., stage=STAGE_SIMULATE)` 校验通过 (虽然契约 §2.5 说
  `mark_failed` 也接受 stage 参数, 但 stage 在这里其实不写库, 仅做合法性校验)

---

## C. 验证状态

```
uv run python -m pytest tests/orchestration/test_config_loader.py \
                                tests/orchestration/test_queue.py -q
→ 53 passed in 2.27s
```

`uv run python -m pytest tests/orchestration/` 全集有 10 个 collection error,
全部来自 ST-3 / ST-6 / ST-7 负责的模块 (按约束不在本 agent 范围).

---

## D. 跨组集成疑问

1. **`simulate_serve_config` 默认行为**: 我保留"根配置格式下指向自身"的旧
   行为 (A4). 若 ST-5 PipelineExecutor 期望 `paths.simulate_serve_config`
   永远是显式路径 (而非默认相对路径), 需要单独通知调整.

2. **`mark_phase` 的 None 语义**: 当前实现"None 不覆盖" (A5). 若 ST-4
   producer_simulate 想用 `mark_phase(..., run_id=None)` 清空 run_id,
   当前行为不会清空, 需要改用显式 `update` SQL 或调整我的实现.

3. **`gdr_settings` 是否需要延迟构造**: 当前 `_build_gdr_settings` 调
   `Settings()` (走 gdr 的 pydantic BaseSettings, 自动读仓库根 yaml + env).
   若 ST-5 PipelineExecutor 期望 `gdr_settings` 是预先构造好的对象
   (而非每次 `load_config` 都新建), 需要调整 `_build_gdr_settings` 或
   改成单例 cache.
