# refined 文件拆分方案（gdr → etl 新架构版）

> etl 阶段把 gdr 输出的 C2 refined Session 拆成 4 份独立视图文件，按视图分离，
> SQLite 队列字段同步拆分。本方案已与用户对齐决策，待执行。

## 1. 背景与定位

新架构：`simulation server → gdr → etl`。gdr 只产 C2 refined Session（单文件），
etl 拿 C2 做格式整理后拆 4 视图。C2 形态见
[docs/contracts/C2-refined-session.md](../contracts/C2-refined-session.md)；C3
形态见 [docs/contracts/C3-final-sft-views.md](../contracts/C3-final-sft-views.md)。

本文件只关注 etl 阶段的"4 视图拆分写入"动作。

## 2. 决策汇总

| 项 | 决定 |
|---|---|
| 4 视图的生产者 | **etl（etl_worker）**，不再由 gdr 产出 |
| 4 视图的写入函数 | `gdr.domain.schema.save_session_v2(session, base_path)`（etl 借用 gdr 的实现） |
| gdr 的输出 | 单文件 `output/refined/<TXXX>__<session_id>.json`（C2） |
| 视图形态 | 裸视图，不带公共头（除 schema_version） |
| 审计 metadata | 拆成 4 份，多一个 `.meta.json` 存全部审计 |
| 原文件 | 删除，不保留兼容 |
| SQLite 队列字段 | `gdr_refined_path` + 4 个 `etl_*_path` |
| qwenjina 扩展名 | `.txt`（纯文本，喂训练不用反序列化） |
| SQLite 表迁移 | 直接删除重建新表，不做 ALTER / 迁移 |
| 存量 qf_out / 旧 refined 4 视图 | **直接删除**，无兼容需求 |
| messages.json 形态 | 统一 `{"messages": [...]}` 包装对象（非裸数组） |
| session_id 摆放 | 放 meta.json 里，不在顶层；messages.json / openai.json 顶层也带 |
| qf_text 缺失 | qwenjina.txt 跳过不写，不报错、不写空文件 |

## 3. 目标产物（C3）

```
<stem>_refined.messages.json   ← {"schema_version": "sft_views.v1", "session_id": "...", "messages": [...], "tools": [...]}
<stem>_refined.openai.json     ← {"schema_version": "sft_views.v1", "session_id": "...", "openai_messages": [...], "tools": [...]}
<stem>_refined.qwenjina.txt    ← 纯文本（Qwen3 chat_template 渲染）；qf_text 缺失则不写
<stem>_refined.meta.json       ← {"schema_version": "sft_views.v1", "session_id": "...",
                                  original_session_id, refined_version,
                                  refine_history, validation_summary, policy_decisions,
                                  modified_blocks, edit_status_summary,
                                  folded_failed_toolresults, routing_abstentions,
                                  unknown_tool_names, judge_discard, timeout_partial_save,
                                  qf_stats, qf_rendered_at, tools, ...}
```

`<stem>` 沿用 `<TXXX>__<session_id>` 形态；`_refined` 是 etl 阶段的固定后缀，
由 etl_worker 派生。4 份文件共用同一 stem，仅尾缀与扩展名不同。

## 4. 逐文件修改清单

### 4.1 etl 模块

#### `etl/writers/split_4_views.py`（新文件）

etl 阶段调用 `gdr.domain.schema.save_session_v2(session, base_path)` 写 4
视图。本文件是该写入动作的 etl 包装层：

```python
# etl/writers/split_4_views.py
from pathlib import Path
from gdr.domain.schema import Session, save_session_v2
from etl.parsers.refined_session import load_refined_session
from etl.qwenformat.usage_prune import collect_usage, prune_session_in_place
from etl.qwenformat.transform import build_chat_env, load_chat_template, trajectory_to_session_with_openai_metadata
from etl.qwenformat.system_prompt import partition_system_prompt, save_section_templates
from etl.qwenformat.tool_templates import save_tool_templates
from etl.qwenformat.tool_output_summarizer import ToolOutputSummarizer

def render_to_4_views(c2_path: Path, base_path: Path, settings) -> dict[str, Path]:
    """C2 → etl 处理 → C3 4 视图。返回 4 路径 dict。"""
    session = load_refined_session(c2_path)

    # 1. usage_prune（裁 system/tools + 重渲染 qf_text）
    collect_usage(session.model_dump())
    prune_session_in_place(
        session.model_dump(),
        settings=settings,
        template_str=load_chat_template(settings.qf_chat_template_path),
        env=build_chat_env(),
    )

    # 2. transform（openai_messages + qf_stats + qf_rendered_at）
    trajectory_to_session_with_openai_metadata(
        session.model_dump(),
        template_str=load_chat_template(settings.qf_chat_template_path),
        env=build_chat_env(),
    )

    # 3. system prompt partition + tool templates
    partition_system_prompt(session.model_dump())
    save_section_templates(session.model_dump())
    save_tool_templates(session.model_dump())

    # 4. tool output summarizer（L0 规则 + 可选 L1 LLM 锚点）
    summarizer = ToolOutputSummarizer.from_settings(settings)
    summarizer.summarize_record(session.model_dump())

    # 5. 拆 4 视图（借用 gdr.domain.schema 的实现）
    return save_session_v2(session, base_path)
```

约定：etl **不自己实现** 4 视图拆分的具体逻辑；统一通过
`gdr.domain.schema.save_session_v2` 落地，保证 C3 schema 演进由 gdr domain
统一控制。

### 4.2 gdr 模块

#### `gdr/domain/schema.py`

| 位置 | 改动 |
|---|---|
| `load_session(path)` | **保留**：从 C2 文件读 Session（向后兼容；但生产环境不再直接调用，因为新流水线 gdr → etl 走的是内存对象，不经文件 load） |
| `save_session(session, output_path)` | **删除**（旧单文件形态废弃；`save_refined_session` 取代） |
| `save_session_v2(session, base_path)` | **新增**：拆 4 视图写入函数（沿用 `docs/contracts/C3-final-sft-views.md` 规范） |
| `save_refined_session(session, path)` | **新增**：gdr 写 C2 单文件的入口 |
| `SessionOutputs` dataclass | 字段扩展（messages / openai / qwenjina / meta 4 路径） |

`save_session_v2` 内容抽取规则（与 C3 §3-§6 对齐）：

- `messages.json` ← `{"schema_version": "sft_views.v1", "session_id": session.session_id, "messages": [...], "tools": [...]}`
- `openai.json` ← `{"schema_version": "sft_views.v1", "session_id": session.session_id, "openai_messages": session.metadata["openai_messages"], "tools": [...]}`
- `qwenjina.txt` ← `session.metadata["qf_text"]`；为空/缺失则跳过不写
- `meta.json` ← `{"schema_version": "sft_views.v1", "session_id": session.session_id, **session.metadata}`（metadata 全量 + session_id 顶层）

返回值：4 个写入路径的 dict（`{"messages": Path, "openai": Path, "qwenjina": Path | None, "meta": Path}`）。

#### `gdr/pipeline/runner.py`

| 位置 | 改动 |
|---|---|
| `_resolve_output` (line 741) | 返回单路径 `<batch_output_dir>/<input_stem>.json`（无 `_refined` 后缀） |
| `_process_one_file` (line 649/671) | 末尾 `save_session(...)` → `save_refined_session(result, output_path)` |
| 返回值 (line 675) | `{"output": str(output_path)}` 形态不变（单文件） |
| `_worker_process_file` (line 693) | 透传新返回值结构 |
| `_apply_usage_prune` (line 1064) | **整段删除**（含 `from etl.qwenformat.transform import build_chat_env, load_chat_template` 等 import） |

### 4.3 orchestration 模块

#### `orchestration/queue/schema.sql`

```sql
-- 旧
qf_output_path TEXT,        -- 删除
gdr_output_path TEXT,       -- 改名为 gdr_refined_path
gdr_messages_path TEXT,     -- 删除（不再由 gdr 写）
gdr_openai_path TEXT,       -- 删除
gdr_qwenjina_path TEXT,     -- 删除
gdr_meta_path TEXT,         -- 删除

-- 新
gdr_refined_path TEXT,      -- 单 C2 refined Session 路径
etl_messages_path TEXT,     -- C3 messages.json
etl_openai_path TEXT,       -- C3 openai.json
etl_qwenjina_path TEXT,     -- C3 qwenjina.txt（可空）
etl_meta_path TEXT,         -- C3 meta.json
```

`_init_schema` 检测到旧列存在则 DROP TABLE tasks 并重建（按"直接删除重建"决策）。

#### `orchestration/queue/sqlite_queue.py`

- `Task` dataclass 字段：
  - 删除：`qf_output_path`、`gdr_messages_path`、`gdr_openai_path`、`gdr_qwenjina_path`、`gdr_meta_path`
  - 重命名：`gdr_output_path` → `gdr_refined_path`
  - 新增：`etl_messages_path`、`etl_openai_path`、`etl_qwenjina_path`、`etl_meta_path`
- `_row_to_task` / `_pull_n` / `list_tasks_for_batch` / `get`：SELECT / RETURNING 列表同步
- `mark_gdr_done(task_id, *, gdr_refined_path)` 单参版本（替代旧 4 参版本）
- 新增 `mark_etl_done(task_id, *, etl_messages_path, etl_openai_path, etl_qwenjina_path, etl_meta_path)`
- `_init_schema`：增加旧表检测 + DROP 重建逻辑

#### `orchestration/workers/qf_worker.py` → `etl_worker.py`

- 文件**改名** + 类名 `QfWorker` → `EtlWorker`
- `process(task)` 改：
  - 输入：不再是 trajectory；改读 `task.gdr_refined_path`（C2）
  - 入口：`from etl.writers.split_4_views import render_to_4_views`
  - 调用链：`render_to_4_views(task.gdr_refined_path, base_path, settings)`
  - 输出：4 路径回写队列 `mark_etl_done(...)`
  - `_output_name(task, session_id, suffix="_refined")` 生成 base_path

#### `orchestration/workers/gdr_worker.py`

- `process(task)` 改：
  - 输入：读 `task.src_path`（trajectory JSON），不再读 qf_output_path
  - 入口：`from gdr.parsers import from_trajectory`
  - 调用链：`from_trajectory(task.src_path)` → `_process_one_file`
  - 输出：单 `gdr_refined_path` 回写队列 `mark_gdr_done(...)`
  - `_output_name(task, session_id, suffix="")`（不带 `_refined`）

#### `orchestration/workers/base_worker.py:53,57`

- 契约 `process -> Path` + `mark_done(task, output: Path)` 不变
- gdr_worker 返回单 Path → mark_done 透传
- etl_worker 返回主路径（messages.json）→ 其余 3 路径存 `self._last_outputs`，
  mark_done 从实例取（沿用 `docs/contracts/migration-plan.md §4.3 方案 A`）

#### `orchestration/failure_handler.py`

- SELECT 列表：`gdr_output_path` → `gdr_refined_path` + 4 个 `etl_*_path`
- 归档逻辑：每个非空路径 move 到 dead_dir
- 保留 INCOMPLETE / DEAD_INDEX.jsonl 行为

#### `orchestration/master.py`

- 调度逻辑：`pending → gdr_processing` → `pending_etl` → `etl_processing` → `done`
- 阶段切换按 state 分发到 gdr / etl worker pool
- 失败处理：单阶段失败只回退到 error，不传染下一阶段
- batch_tracker 聚合维度按 (gdr_done_count, etl_done_count) 双计数

### 4.4 测试改动

| 测试文件 | 改动点 |
|---|---|
| `tests/orchestration/test_gdr_worker.py` | mock 返回值改 `gdr_refined_path`；`_process_one_file` mock 输入改 trajectory |
| `tests/orchestration/test_qf_worker.py` | **改名为** `test_etl_worker.py`；输入 fixture 改 C2 形态；mock `from_trajectory` 改 `load_refined_session` |
| `tests/orchestration/test_smoke_3task.py` | `_patch_gdr` mock 改写单 refined.json；`_patch_etl` mock 写 4 文件；状态机断言改 `pending_etl` |
| `tests/orchestration/test_master.py` | 状态机断言、调度逻辑改 |
| `tests/orchestration/test_failure_recovery.py` | 死归档路径恢复测试改 |
| `tests/orchestration/test_stage_timestamps_naming_backoff.py` | 输出命名断言改 `<stem>.json`（gdr） / `<stem>_refined.{...}.json`（etl） |
| `tests/orchestration/test_queue.py` | Task 字段、`mark_gdr_done` 单参、`mark_etl_done` 4 参 |
| `tests/orchestration/test_health.py` | 状态计数改双计数 |
| `tests/orchestration/test_failure_handler.py` | SELECT 列名改 |
| `gdr/tests/test_runner_load_session.py` | `load_session` 改为接受 C2 形态；`from_trajectory` 单测覆盖 |
| `gdr/tests/test_save_session_v2.py`（**新文件**） | `save_session_v2` 写 4 文件的全量测试（messages.json / openai.json / qwenjina.txt / meta.json） |
| `gdr/tests/test_save_refined_session.py`（**新文件**） | `save_refined_session` 写单 C2 文件的测试 |
| `gdr/tests/test_consistency_and_writeback.py` | metadata 落盘断言改读 refined 单文件 |
| `gdr/tests/test_incomplete_session_detection.py` | 路径断言确认 |
| **新文件** `gdr/tests/test_parser_from_trajectory.py` | `gdr.parsers.from_trajectory` 单测：trajectory → Session |
| **新文件** `etl/tests/test_parsers_refined_session.py` | `etl.parsers.load_refined_session` 单测：C2 → Session |
| **新文件** `etl/tests/test_writers_split_4_views.py` | etl.writers.render_to_4_views 单测：C2 → 4 视图 |

### 4.5 不用改 / 删除

- **删除** `gdr/pipeline/runner.py::_apply_usage_prune` 整段（含 etl 强引用 import）
- **删除** `gdr/config/settings.py::enable_usage_prune` 字段
- **删除** `gdr/config/settings.py::qf_chat_template_path` 字段（chat_template 是 etl 关注）
- **删除** `simulate_serve/infrastructure/run_repository.py::export` 写 `all_runs.v2.jsonl` / `distill_dataset.v2.jsonl` 的代码（C3 已取代）
- **删除** `output/qf_out/` 全部存量（一次性脚本 `scripts/purge_qf_out.py`）
- **删除** `output/refine_data/*.json` 旧 4 视图存量（一次性脚本 `scripts/purge_legacy_refined.py`）
- **删除** 旧 `load_trajectory` 路径（`gdr/domain/schema.py` 中的 fallback，gdr 改用 `gdr.parsers.from_trajectory`）
- **删除** SQLite `tasks` 表旧 schema 迁移测试（`test_legacy_db_migrated_with_new_columns`，已不再需要）

## 5. 契约决策：process / mark_done 返回类型

`BaseWorker` 契约 `process(task) -> Path` + `mark_done(task, output: Path)`。

- gdr 写单 C2 → 返回单 Path → mark_done 透传
- etl 写 4 视图 → 返回主路径（messages.json），其余 3 路径存 `self._last_outputs`，
  mark_done 从实例取

etl_worker 不改 `BaseWorker` 契约本身；契约改动局部化。

## 6. 风险点

1. **gdr.parsers 漂移**：trajectory → Session 与旧 qf_out → Session 行为必须等价；
   单测覆盖（`test_parser_from_trajectory.py`）。
2. **etl 借用 gdr.domain.schema.save_session_v2**：反向依赖；约定仅导入
   `gdr.domain.io` 子模块（如果后续抽离），不动 schema。
3. **SQLite 旧库 DROP 重建**：首次启动会 DROP 全表；按"直接删除重建"决策可接受。
4. **qf_text 缺失**：部分 fixture 可能没造 `qf_text`，etl 阶段 qwenjina.txt 不写，
   测试断言其存在会失败——fixture 需补 `qf_text` 或断言改"不要求存在"。
5. **`_output_name` suffix 语义**：gdr 写 `<stem>.json`（无后缀），etl 写
   `<stem>_refined.{...}`（4 后缀）；两 worker 各自派生，base_worker 不动。

## 7. 执行顺序

1. 新建 `gdr/parsers/__init__.py` + `gdr/parsers/README.md`
2. `gdr/domain/schema.py` — 新增 `save_session_v2` / `save_refined_session`；删除旧 `save_session`
3. `gdr/pipeline/runner.py` — 删除 `_apply_usage_prune`；`_resolve_output` 改单路径；`_process_one_file` 改 `save_refined_session`
4. `gdr/config/settings.py` — 删除 `enable_usage_prune` / `qf_chat_template_path`
5. 新建 `etl/parsers/refined_session.py` + `etl/parsers/__init__.py`
6. 新建 `etl/writers/split_4_views.py`
7. `orchestration/queue/schema.sql` + `sqlite_queue.py` — 新表 + Task + mark_gdr_done(单参) + mark_etl_done(4参)
8. `orchestration/workers/qf_worker.py` → `etl_worker.py`（改名 + 重写）
9. `orchestration/workers/gdr_worker.py` — 改 trajectory 输入 + gdr.parsers
10. `orchestration/master.py` — 新状态机 + 双计数
11. `orchestration/failure_handler.py` — SELECT 列改
12. `config/config.yaml` — 删除 `qf_output_dir`；新增 `refined_dir`；删除 `gdr.enable_usage_prune` / `gdr.qf_chat_template_path`
13. 测试全改 + 新增（见 §4.4）
14. `simulate_serve/infrastructure/run_repository.py` 删除 export 写 `all_runs.v2.jsonl` / `distill_dataset.v2.jsonl` 代码
15. 跑 `uv run python -m pytest -q` 全绿
16. 一次性脚本 `scripts/purge_qf_out.py` + `scripts/purge_legacy_refined.py` 清存量
17. 更新 `CLAUDE.md` / `docs/设计方案/gdr-plan.md` / `docs/refactor-development-progress.md`

## 8. 实施条件评估（已核实，2026-09-22）

结论：**具备实施条件，无阻碍性前置依赖。** 方案引用的文件/行号/签名/字段名
与当前代码完全一致，无错位。

### 8.1 核实结果

| 引用点 | 现状 |
|---|---|
| `gdr/domain/schema.py::save_session(session, output_path)` | 单参输出，符合 |
| `gdr/pipeline/runner.py:649/671/675/741` | `_process_one_file` / `save_session` 调用 / `{"output":...}` / `_resolve_output` 均在位 |
| `gdr/pipeline/runner.py:1064 _apply_usage_prune` | 在位（含 etl 引用），待删除 |
| `gdr/config/settings.py::enable_usage_prune / qf_chat_template_path` | 在位，待删除 |
| `orchestration/queue/schema.sql:15 gdr_output_path` | 在位 |
| `sqlite_queue.py` Task / `_row_to_task` / `_pull_n` / `mark_gdr_done` / `list_tasks_for_batch` / `get` | 6 处全部命中方案行号 |
| `gdr_worker.py:81/108/120`、`base_worker.py:53/57`、`failure_handler.py:56/72` | 全部命中 |

### 8.2 改动闭环完整性

- grep 确认 `gdr_output_path` 在 orchestration 非测试代码中仅有 3 个消费者
  （`sqlite_queue` / `gdr_worker` / `failure_handler`），方案已全覆盖；
  `master` / `daemon` / `health` / `qf_worker` / `base_worker` 退避逻辑均不引用。
- `_apply_usage_prune` 在 orchestration 非测试代码中无外部调用；删除是安全的。

### 8.3 补充发现（需纳入改动清单，原方案未列出）

1. `gdr/tests/test_consistency_and_writeback.py:282/296` — 测试直接
   `from domain import save_session` 并按旧签名 `save_session(session, input_path)`
   调用；拆分后签名与输出形态变更，该调用点也需改（§4.4 已列）。
2. `gdr/domain/__init__.py` 导出 `save_session` — 改签名后保持同名导出即可；
   `save_session_v2` / `save_refined_session` 同模块导出。
3. `etl/qwenformat/usage_prune.collect_usage` 当前接收 raw dict；etl 改用 Session
   后需先 `.model_dump()` 再传——`render_to_4_views` 已统一处理。
4. `scripts/export_qf_sft/` —— 该脚本从旧 `*_refined.messages.json` 导 SFT JSONL；
   新流程下输入路径不变（仍是 `output/refine_data/*_refined.messages.json`），
   脚本无需改；只需在文档里注明"输入已切到 etl 阶段产物"。

### 8.4 契约决策确认

采纳方案 A：`process` 返回主路径（messages.json），4 路径存 `self._last_outputs`；
`mark_done` 从实例取其余 3 路径。改动局部化，不波及 base_worker 契约。

## 9. 执行结果（2026-09-22）

按本方案逐项落地，结果如下：

- `gdr/parsers/`（C1 入口）/ `etl/parsers/`（C2 入口）/ `etl/writers/`（C3 写 4 视图）三目录已建
- `gdr/pipeline/runner.py::_apply_usage_prune` 整段删除；`gdr/config/settings.py::enable_usage_prune` / `qf_chat_template_path` 已删除
- `gdr/domain/schema.py::save_session`（旧单文件版）已删除；`save_refined_session`（C2）/ `save_session_v2`（C3）已新增
- SQLite `tasks` 表 `gdr_output_path` → `gdr_refined_path`；新增 4 个 `etl_*_path`；删除 `qf_output_path`
- `orchestration/workers/qf_worker.py` → `etl_worker.py`（改名 + 重写）
- `orchestration/master.py` 状态机：pending → gdr_processing → pending_etl → etl_processing → done
- 验证：`uv run python -m pytest -q` 全绿（475 / 475 通过）
- 清理：`scripts/purge_qf_out.py` 已清空 `output/qf_out/`（旧 1 文件 / 182KB）；`scripts/purge_legacy_refined.py` 已清空 `output/refine_data/` 旧 4 视图（4 文件 / 57KB），旁路 jsonl 保留
- §8.3.4 注：`scripts/export_qf_sft/` 输入路径未变，仍读 `output/refine_data/*_refined.messages.json`，无需改