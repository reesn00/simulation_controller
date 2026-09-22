# 迁移方案 — 直切新架构

> 不考虑旧架构 / 功能兼容性，直接对接 `simulation server → gdr → etl`。
> 三份契约（C1/C2/C3）已落地于同目录。

## 1. 调整范围

### 1.1 模块依赖方向

```
simulation server → gdr → etl
  gdr ──────────► etl.qwenformat.load        (parser only, 单函数)
  etl ──────────► gdr.domain.schema           (Session pydantic 类型 + save_session_v2 拆分写入)
```

依赖箭头变化（与旧 `simulation server → etl → gdr` 对比）：

| 维度 | 旧 | 新 |
|---|---|---|
| etl 调用时机 | 头部 qf_worker + 尾部 gdr._apply_usage_prune | 单一 etl_worker（尾部） |
| gdr 输入 | qf_out 渲染后 | 原始 trajectory（轻解析 Session） |
| gdr → etl 引用 | 强（_apply_usage_prune + transform + chat_template） | 弱（仅 load_trajectory 单函数） |
| etl → gdr 引用 | 无 | 弱（仅 Session pydantic + save_session_v2） |

### 1.2 代码改动清单（按模块）

#### 新建 `gdr/parsers/`（gdr 对 C1 契约的唯一入口）

| 文件 | 内容 |
|---|---|
| `gdr/parsers/__init__.py` | `from_trajectory(path) -> Session`，薄包装 `etl.qwenformat.load.load_trajectory` + `Session.model_validate` |
| `gdr/parsers/README.md` | 说明 gdr.parsers 是 C1 契约入口，不允许直接 `import etl.qwenformat.load` |

约定：未来若把 parser 迁出 etl，只需改 `gdr/parsers/__init__.py` 一处。

#### 新建 `etl/parsers/`（etl 对 C2 契约的唯一入口）

| 文件 | 内容 |
|---|---|
| `etl/parsers/__init__.py` | `load_refined_session(path) -> Session` |

#### 新建 `etl/writers/`（etl 的写入层）

| 文件 | 内容 |
|---|---|
| `etl/writers/__init__.py` | 暴露 `render_to_4_views(c2_path, base_path, settings) -> dict[Path]` |
| `etl/writers/split_4_views.py` | 编排链：load_refined_session → usage_prune → transform → system_prompt partition → tool_templates → tool_output_summarizer → save_session_v2 |

#### `gdr/domain/schema.py`

| 位置 | 改动 |
|---|---|
| `save_session(session, output_path)` | **删除**（不再被任何代码调用；旧单文件形态废弃） |
| `save_session_v2(session, base_path)` | **新增**：拆 4 视图写入（messages / openai / qwenjina.txt / meta） |
| `save_refined_session(session, path)` | **新增**：gdr 写 C2 单文件的入口 |
| `load_session(path)` | **保留**：从 C2 文件读 Session（test 仍可能用到；生产链路不调用） |
| `SessionOutputs` dataclass | 字段扩展（4 路径，可选 qwenjina） |

#### `gdr/pipeline/runner.py`

| 位置 | 改动 |
|---|---|
| 入口 | `load_session(qf_out_path)` → `from_trajectory(task.src_path)` |
| `_apply_usage_prune` (line ~1064) | **整段删除**（含 `from etl.qwenformat.transform import ...` 等 import） |
| `_resolve_output` | 返回单路径 `<batch_output_dir>/<input_stem>.json`（无 `_refined` 后缀） |
| `_process_one_file` 末尾 | `save_session(...)` → `save_refined_session(result, output_path)` |
| 返回值 | `{"output": str(output_path)}` 形态不变（单文件） |

#### `gdr/config/settings.py`

| 字段 | 改动 |
|---|---|
| `enable_usage_prune: bool = True` | **删除** |
| `qf_chat_template_path: str` | **删除**（chat_template 是 etl 关注） |

#### `orchestration/queue/schema.sql`

```sql
state TEXT CHECK (state IN ('pending','gdr_processing','pending_etl',
                            'etl_processing','done','error','dead'))

-- 删除：qf_output_path
-- 重命名：gdr_output_path → gdr_refined_path
-- 新增：etl_messages_path / etl_openai_path / etl_qwenjina_path / etl_meta_path
gdr_refined_path TEXT,
etl_messages_path TEXT,
etl_openai_path   TEXT,
etl_qwenjina_path TEXT,
etl_meta_path     TEXT,
```

`_init_schema` 检测到旧列存在则 DROP TABLE tasks 后重建。

#### `orchestration/queue/sqlite_queue.py`

- `Task` dataclass：删 `qf_output_path`，`gdr_output_path` 改名 `gdr_refined_path`，
  新增 4 个 `etl_*_path`
- `_row_to_task` / `_pull_n` / `list_tasks_for_batch` / `get`：SELECT / RETURNING 列表同步
- `mark_gdr_done(task_id, *, gdr_refined_path)` 单参版本
- 新增 `mark_etl_done(task_id, *, etl_messages_path, etl_openai_path, etl_qwenjina_path, etl_meta_path)`
- `_init_schema` 增加旧表检测 + DROP 重建

#### `orchestration/workers/qf_worker.py` → `etl_worker.py`

- 文件**改名** + 类名 `QfWorker` → `EtlWorker`
- `process(task)` 改：
  - 输入：读 `task.gdr_refined_path`（C2）
  - 调用：`render_to_4_views(task.gdr_refined_path, base_path, settings)`
  - 输出：4 路径回写队列 `mark_etl_done(...)`

#### `orchestration/workers/gdr_worker.py`

- `process(task)` 改：
  - 输入：读 `task.src_path`（trajectory JSON）
  - 入口：`from gdr.parsers import from_trajectory`
  - 调用链：`from_trajectory(task.src_path)` → `_process_one_file`
  - 输出：单 `gdr_refined_path` 回写队列 `mark_gdr_done(...)`
  - `_output_name(task, session_id, suffix="")`（不带 `_refined`）

#### `orchestration/workers/base_worker.py`

- 契约 `process -> Path` + `mark_done(task, output: Path)` 不变
- etl_worker 返回主路径（messages.json），存 `self._last_outputs`，mark_done 从实例取

#### `orchestration/master.py`

- 状态机：`pending → gdr_processing` → `pending_etl` → `etl_processing` → `done`
- 阶段切换按 state 分发到 gdr / etl worker pool
- batch_tracker 聚合按 (gdr_done_count, etl_done_count) 双计数

#### `orchestration/failure_handler.py`

- SELECT 列表：`gdr_output_path` → `gdr_refined_path` + 4 个 `etl_*_path`
- 归档逻辑：每个非空路径 move 到 dead_dir

#### `config/config.yaml`

```yaml
orchestration:
  paths:
    trajectory_dir: output/agent_trajectory      # C1 落点
    runs_dir: output/runs                         # simulate_serve 自洽
    refined_dir: output/refined                   # C2 落点（新增）
    refine_data_dir: output/refine_data           # C3 落点
    # 删除：qf_output_dir
    sqlite_db: output/orchestration/orchestration.db
    dead_dir: output/orchestration/dead

gdr:
  # 删除字段：
  # enable_usage_prune: true
  # qf_chat_template_path: ./etl/qwenformat/chat_template.jinja
```

#### `simulate_serve/infrastructure/run_repository.py`

- **删除** `export` 方法中写 `all_runs.v2.jsonl` / `distill_dataset.v2.jsonl` 的代码
  （C3 已取代；simulate_serve 不再自导出 SFT 数据）

### 1.3 测试改动

| 测试文件 | 改动点 |
|---|---|
| `tests/orchestration/test_gdr_worker.py` | mock 返回值改 `gdr_refined_path`；`_process_one_file` mock 输入改 trajectory |
| `tests/orchestration/test_qf_worker.py` | **改名为** `test_etl_worker.py`；fixture 改 C2 形态 |
| `tests/orchestration/test_smoke_3task.py` | `_patch_gdr` mock 写单 refined.json；`_patch_etl` mock 写 4 文件；状态机断言改 `pending_etl` |
| `tests/orchestration/test_master.py` | 状态机断言、调度逻辑 |
| `tests/orchestration/test_failure_recovery.py` | 死归档路径 |
| `tests/orchestration/test_stage_timestamps_naming_backoff.py` | 输出命名断言改 `<stem>.json`（gdr） / `<stem>_refined.{...}.json`（etl） |
| `tests/orchestration/test_queue.py` | Task 字段、`mark_gdr_done` 单参、`mark_etl_done` 4 参 |
| `tests/orchestration/test_health.py` | 状态计数改双计数 |
| `tests/orchestration/test_failure_handler.py` | SELECT 列名 |
| `tests/orchestration/test_legacy_db_migrated_with_new_columns.py` | **删除**（旧库兼容不再需要） |
| `gdr/tests/test_runner_load_session.py` | 接受 C2 形态；`from_trajectory` 覆盖 |
| `gdr/tests/test_save_session_v2.py`（**新**） | `save_session_v2` 写 4 文件全量测试 |
| `gdr/tests/test_save_refined_session.py`（**新**） | `save_refined_session` 写单 C2 文件测试 |
| `gdr/tests/test_consistency_and_writeback.py` | metadata 落盘断言改读 refined 单文件 |
| `gdr/tests/test_incomplete_session_detection.py` | 路径断言 |
| **新** `gdr/tests/test_parser_from_trajectory.py` | `gdr.parsers.from_trajectory` 单测 |
| **新** `etl/tests/test_parsers_refined_session.py` | `etl.parsers.load_refined_session` 单测 |
| **新** `etl/tests/test_writers_split_4_views.py` | `render_to_4_views` 单测：C2 → 4 视图 |

### 1.4 删除清单（旧架构残留）

| 对象 | 处理 |
|---|---|
| `gdr/pipeline/runner.py::_apply_usage_prune` | 删除（含 etl 强引用 import） |
| `gdr/config/settings.py::enable_usage_prune` | 删除字段 |
| `gdr/config/settings.py::qf_chat_template_path` | 删除字段 |
| `gdr/domain/schema.py::save_session`（旧单文件版） | 删除（被 `save_refined_session` 取代） |
| `output/qf_out/` 全部存量 | 一次性脚本 `scripts/purge_qf_out.py` 清空 |
| `output/refine_data/*.json` 旧 4 视图存量 | 一次性脚本 `scripts/purge_legacy_refined.py` 清空 |
| `simulate_serve/infrastructure/run_repository.py::export` 写 `all_runs.v2.jsonl` / `distill_dataset.v2.jsonl` | 删除 |
| `simulate_serve` 输出 `output/datasets/all_runs.v2.jsonl` / `distill_dataset.v2.jsonl` | 不再生效；旧文件保留 30 天后清理 |
| SQLite `tasks` 表旧 schema 迁移测试 | 删除 |

## 2. 执行顺序（一次性直切）

按以下顺序串行执行；任一步失败即停，回滚按 §3。

1. **新建入口层**
   - `gdr/parsers/__init__.py` + `gdr/parsers/README.md`
   - `etl/parsers/__init__.py`
   - `etl/writers/__init__.py` + `etl/writers/split_4_views.py`

2. **改 gdr domain / pipeline**
   - `gdr/domain/schema.py` — 删 `save_session`，新增 `save_session_v2` / `save_refined_session`
   - `gdr/pipeline/runner.py` — 删 `_apply_usage_prune`，改 `_resolve_output`，改 `_process_one_file` 末尾
   - `gdr/config/settings.py` — 删两个字段

3. **改 orchestration**
   - `orchestration/queue/schema.sql` + `sqlite_queue.py`
   - `orchestration/workers/qf_worker.py` → `etl_worker.py`（改名 + 重写）
   - `orchestration/workers/gdr_worker.py`
   - `orchestration/workers/base_worker.py`（最小改动：etl_worker 走 `self._last_outputs`）
   - `orchestration/master.py`
   - `orchestration/failure_handler.py`

4. **改 config**
   - `config/config.yaml` — 删 `qf_output_dir`；新增 `refined_dir`；删 `gdr.enable_usage_prune` / `gdr.qf_chat_template_path`

5. **改 simulate_serve**
   - `simulate_serve/infrastructure/run_repository.py` — 删 `export` 写 dataset 段

6. **改测试**（按 §1.3）
   - 修改既有 11 个测试
   - 新增 4 个测试
   - 删除 1 个旧 schema 迁移测试

7. **跑全测**
   - `uv run python -m pytest -q`
   - 关注 orchestration / gdr / etl 三块的失败信号

8. **清存量**
   - `python scripts/purge_qf_out.py` —— 清 `output/qf_out/`
   - `python scripts/purge_legacy_refined.py` —— 清 `output/refine_data/` 旧 4 视图
   - 删 `output/datasets/all_runs.v2.jsonl` / `distill_dataset.v2.jsonl`

9. **更新文档**
   - `CLAUDE.md` —— 增加 "Pipeline 流程" 段
   - `docs/设计方案/gdr-plan.md` —— 主流程时序第 ⑫ 步从"四视图"改"单 C2 refined Session"
   - `docs/refactor-development-progress.md` —— 追加新架构落地条目
   - `docs/project-notes.md` —— 追加架构变更条目

## 3. 回滚

由于是直切新架构（无双跑），回滚路径是 git revert：

- 若 §1–§5 任何一步代码改动未通过全测 → revert 对应提交，重做该步
- 若 §6 测试改动发现问题 → revert 测试提交，定位问题测试
- 若 §8 清存量后发现仍有依赖 → 数据已删，git 历史恢复

不提供 hot-patch 回滚（无 feature flag，无 legacy 兼容路径）。

## 4. 验收清单

- [ ] `gdr/parsers/from_trajectory` 单测覆盖 trajectory → Session 全分支
- [ ] `etl/parsers/load_refined_session` 单测覆盖 C2 → Session 全分支
- [ ] `etl/writers/render_to_4_views` 端到端单测：C2 fixture → 4 文件存在且 schema 正确
- [ ] `gdr/pipeline/runner.py` 不再 `import etl.qwenformat.transform` / `usage_prune`
- [ ] `gdr/config/settings.py` 不含 `enable_usage_prune` / `qf_chat_template_path`
- [ ] SQLite `_init_schema` 检测到旧库时 DROP 重建（手动构造旧库验证）
- [ ] `orchestration/master.py` 双阶段调度闭环（mock worker 跑通 pending → done）
- [ ] `uv run python -m pytest -q` 全绿
- [ ] `output/qf_out/` 为空；`output/refine_data/` 全是 etl 阶段产物（4 文件形态）
- [ ] `output/refined/` 全部为单 C2 文件形态
- [ ] 监控指标建立：gdr / etl 各阶段 NonRetryableError 占比、阶段耗时
- [ ] `CLAUDE.md` / `docs/设计方案/gdr-plan.md` / `docs/refactor-development-progress.md` 同步更新到位

## 5. 与既有方案的兼容性

| 既有文档 | 处理 |
|---|---|
| `docs/设计方案/agent-trajectory-format.md` | 引用；C1 与其同源 |
| `docs/设计方案/refined_split_plan.md` | **已改写** —— 改为 etl 写 4 视图的新方案 |
| `docs/设计方案/cot-sft-trajectory-data-spec.md` | 引用；C3 与其 §1 链路一致 |
| `docs/设计方案/gdr-plan.md` | 主流程时序 §⑫ 步从"四视图"改"单 C2 refined Session" |
| `docs/设计方案/refine-data-quality-p0-fixes.md` | 引用 |
| `docs/project-notes.md` | 追加新架构条目 |
| `docs/refactor-development-progress.md` | 追加新架构落地条目 |