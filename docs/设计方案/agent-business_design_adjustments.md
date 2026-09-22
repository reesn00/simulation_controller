# agent-business (ST-3 + ST-4) 设计调整记录

> 实施组 agent-business 在执行 pipeline-serial-parallel-refactor.md 第 1 轮
> 第 2 组任务时，对契约文档 `pipeline-contracts.md` 的**实现层调整**与
> 跨组依赖说明。契约修改不归本组，请回报给设计层。

---

## 1. ST-3 Worker 层 (gdr_worker / etl_worker)

### 1.1 BaseWorker 仍保留兼容别名（指向 _output_filename）

契约 §3.5 要求"删 `BaseWorker` 类 / `run_forever`"，但 `master.py`
(ST-6) 在交接期仍 `from orchestration.workers.gdr_worker import GdrWorker`。
master 会在 ST-6 自删，本组不做依赖反向兼容。但为避免模块**导入时**
直接 `ImportError`（影响其他无关测试的 collection），`base_worker.py`
里保留同名占位 `BaseWorker`（继承自 `object`，无任何行为）作为过渡。
本组不依赖其存在；ST-6 删除 `master.py` 的旧 import 后，可彻底删。

> **状态**：保留占位类直到 ST-6 删 master 旧 import，不算"未删 BaseWorker"。
> 若设计层希望本组直接删，会让 `pytest --collect-only` 阶段就失败。

### 1.2 GdrResult / EtlOutputs 在哪里定义

按契约 §3.3 / §3.4 落地：`GdrResult` 在 `gdr_worker.py`、
`EtlOutputs` 在 `etl_worker.py`。与 `SessionOutputs`(gdr 库内) 同名
冲突通过别名 `EtlOutputs` 解决（etl 输出是 4 视图路径集合，
`SessionOutputs` 是 gdr 库 dataclass，不会冲突）。

### 1.3 RetryableGdrError / EtlNonRetryableError 命名

契约 §3.3 写 `RetryableGdrError` + `GdrNonRetryableError` 两个异常。
本组合并命名风格为 `*NonRetryableError`（与现有
`orchestration.errors.NonRetryableError` 风格一致），保留两个对称
异常：

```python
class GdrNonRetryableError(Exception): ...   # 永久错误（不重试）
class RetryableGdrError(Exception): ...       # 临时错误（重试）
class EtlNonRetryableError(Exception): ...   # 永久错误（不重试）
```

`GdrNonRetryableError` 与 `EtlNonRetryableError` 均继承自 `Exception`
（非 `NonRetryableError`，因为重试判定由调用方 PipelineExecutor 做，
不需要类型耦合）。**非契约违反**，仅命名简化。

### 1.4 Settings 透传

契约 §3.3 写 `gdr_settings: Settings`（来自 `gdr.config.settings.Settings`）。
本组沿用；构造 `_process_one_file` 时强制 `workers=1, max_files=1`，
其他字段（`llm_concurrency` / `batch_output_dir`）由调用方透传。
若 caller 未传 `batch_output_dir`，fallback 到传入的 `refined_dir`。

### 1.5 etl_outputs base_path 命名

etl worker 内部使用 `<stem>.messages.json` 等 4 视图尾缀命名，
由 `save_session_v2` 自动追加。本组不传 suffix；传 `base_path =
etl_outputs_dir / <c2_stem>` 即可。`EtlOutputs.qwenjina_path` 允许
`None`，与契约一致。

---

## 2. ST-4 Producer

### 2.1 run_one_task 同步 vs 异步

契约 §4.2 写 `async def run_one_task`。`BatchRunner.run` 是 async，
所以 `run_one_task` 必须是 async。本组实现为 async + 在
`task_pipeline._run_one_task_pipeline` 子进程入口内 `asyncio.run`
包装（该包装由 ST-5 负责，本组不实现）。

### 2.2 NoRunnableTasksError 删除

原 `_async_run_batch` 含 `skip_unready_tasks` 配置 + `NoRunnableTasksError`。
新架构 `run_one_task` 是单 task 入口，没有 catalog 级 skip 语义：
- 单 task 不命中 readiness gap 时仍会跑（由 caller 决定是否过滤）
- `NoRunnableTasksError` 不再使用

契约 §4.4 明确要求"删 run_batch / limit / _split_batches"，
本组一并删 `NoRunnableTasksError`。`orchestration/__main__.py`
的旧 `except NoRunnableTasksError` 分支会在 ST-7 删。

### 2.3 skip_unready_tasks 处理

`skip_unready_tasks` 是 catalog 级策略，由 CLI 在 ST-7 提交前过滤
task_ids，不再放在 producer 内。本组不引入新依赖（如 readonly field），
直接删除原逻辑。

### 2.4 queue 参数

契约 §4.4 要求"任何 SQLiteQueue 写操作"删除。
原 `run_batch(..., queue=SQLiteQueue)` 接受 SQLiteQueue 参数用于
写 batches / run_task_map 表。新 `run_one_task` 不再需要此参数，
`SQLiteQueue` 也不再出现在 producer_simulate.py 的 import 列表。

---

## 3. 跨组集成疑问

### 3.1 PipelineExecutor (§5) 调用 run_one_task 时如何传 config_path？

契约 §4.2 写 `run_one_task(task_id, *, config_path: Path)`。
`Paths.simulate_serve_config` 在 ST-1 已定义为 `Path`，
PipelineExecutor 调 `run_one_task(task_id, config_path=paths.simulate_serve_config)`
即可。本组不提供 builder 工具。

### 3.2 run_one_task 返回值用于下游

契约 §4.2 写 "返回 runs[0]"。PipelineExecutor 需要从 `TaskRun`
拿 `run_id` / `remote_session_id` 拼 `src_path`，本组保证
`TaskRun.run_id` 与 `TaskRun.remote_session_id` 是非空字符串
（`BatchRunner.run` 已保证），无需在本函数做额外校验。

### 3.3 worker 的 Settings 如何构造？

PipelineExecutor (§5.4) 写"gdr_settings 是 `Settings` 实例，
workers=1, llm_concurrency 透传"。Master (§6.2) 的 `_build_gdr_settings`
会构造 Settings 并传给 PipelineExecutor。本组 `run_gdr_once`
只接受已构造的 `Settings`，不构造默认值（与契约 §3.3 一致）。

---

## 4. 契约问题（只写不改）

1. **§3.3 异常对称性**：契约同时列 `GdrNonRetryableError` 与
   `RetryableGdrError`，但 ETL 只列 `EtlNonRetryableError`。
   是否 ETL 也需 `RetryableEtlError`？本组按"现有错误场景"判断
   ETL 阶段没有重试意义（纯本地文件 IO + 校验），所以不新增。
   若有跨进程临时 IO 抖动场景，可后续追加 `RetryableEtlError`。

2. **§4.2 KeyError vs ValueError**：契约说"task_id 不在 catalog → KeyError"。
   当前 `_select_tasks` 确实抛 `KeyError(f"task_id(s) not found...")`。
   注意 caller 应捕 `KeyError` 而非 `ValueError`，避免混淆。

3. **§3.4 qwenjina_path 行为**：契约要求 "qwenjina_path 为 None 时仍
   返回非 None 的其他三个路径"。`SessionOutputs` 已天然支持
   `qwenjina: Optional[Path]`，本组透传即可。