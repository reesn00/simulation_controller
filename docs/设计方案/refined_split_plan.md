# refined 文件拆分方案

> 把 gdr 阶段产出的单个 `<stem>_refined.json` 拆成 4 份独立文件，按视图分离，
> SQLite 队列字段同步拆分。本方案已与用户对齐决策，待执行。

## 1. 背景

当前 gdr 阶段把整个 Session 写进单个 `_refined.json`，文件内同时包含：

- `messages`（顶层，blocks 结构化视图）
- `metadata.openai_messages`（OpenAI function-calling 视图）
- `metadata.qf_text`（Qwen3 chat_template 渲染纯文本）
- `metadata` 里还混着审计字段（refine_history / validation_summary / policy_decisions / ...）
  与渲染附属（tools / qf_stats / qf_rendered_at）

三视图混在一个文件里，下游消费方各取所需时要反序列化整份大 JSON，且审计与视图耦合。
本方案把视图与审计元数据拆成独立文件，按命名约定直连下游。

## 2. 决策汇总

| 项 | 决定 |
|---|---|
| 作用范围 | 改 gdr 输出逻辑，以后所有 refined 都拆（非一次性脚本） |
| 视图形态 | 裸视图，不带公共头 |
| 审计 metadata | 拆成 4 份，多一个 `.meta.json` 存全部审计 |
| 原文件 | 删除，不保留兼容 |
| SQLite 队列字段 | `gdr_output_path` 拆成 4 个字段 |
| qwenjina 扩展名 | `.txt`（纯文本，喂训练不用反序列化） |
| SQLite 表迁移 | 直接删除重建新表，不做 ALTER / 迁移 |
| 存量 refined 文件 | 不用管，无需兼容 |
| messages.json 形态 | 统一 `{"messages": [...]}` 包装对象（非裸数组） |
| session_id 摆放 | 放 meta.json 里，不在顶层 |
| qf_text 缺失 | qwenjina.txt 跳过不写，不报错、不写空文件 |

## 3. 目标产物

```
<stem>_refined.messages.json   ← {"messages": [Message, ...]}（blocks 视图）
<stem>_refined.openai.json     ← {"openai_messages": [{role, content/tool_calls}, ...]}
<stem>_refined.qwenjina.txt    ← 纯文本（Qwen3 chat_template 渲染）；qf_text 缺失则不写
<stem>_refined.meta.json       ← {session_id, original_session_id, refined_version,
                                  refine_history, validation_summary, policy_decisions,
                                  modified_blocks, edit_status_summary,
                                  folded_failed_toolresults, routing_abstentions,
                                  unknown_tool_names, judge_discard, timeout_partial_save,
                                  qf_stats, qf_rendered_at, tools, ...}
```

`<stem>` 沿用现有命名前缀（含 `task_id__session_id` 形态），由
`base_worker._output_name` 生成；4 份文件共用同一 stem，仅尾缀与扩展名不同。

## 4. 逐文件修改清单

### 4.1 gdr 模块

#### `gdr/domain/schema.py:212` `save_session`

- 改签名：`save_session(session, base_path: Path)` → 以 `base_path` 为 stem，
  在其父目录下写 4 个文件
- 旧 `save_session` 删除（用户选删原文件，无需兼容）
- 内容抽取规则：
  - `messages.json` ← `{"messages": [m.model_dump() for m in session.messages]}`
  - `openai.json` ← `{"openai_messages": session.metadata.get("openai_messages", [])}`
  - `qwenjina.txt` ← `session.metadata.get("qf_text")`；为空/缺失则跳过不写
  - `meta.json` ← `session.metadata` 全量，并补 `session_id` 字段
- 返回值：4 个写入路径的 dict / dataclass，供上游回写队列

#### `gdr/pipeline/runner.py:741` `_resolve_output`

- 返回 base path（去掉 `.json` 后缀的 stem），或返回 4 路径的 dataclass
- 命名：`<batch_output_dir>/<input_stem>_refined` 作为 base

#### `gdr/pipeline/runner.py:649` `_process_one_file`

- line 671 `save_session(result, output_path)` → 改调拆分写入
- 返回值 line 675 `{"output": str(output_path)}` →
  `{"outputs": {"messages":..., "openai":..., "qwenjina":..., "meta":...}}`
- `status` 字段保留

#### `gdr/pipeline/runner.py:693` `_worker_process_file`

- 透传新返回值结构（Pool worker 入口，无需改逻辑，只随返回值形态变）

### 4.2 orchestration 模块

#### `orchestration/queue/schema.sql:15`

- `gdr_output_path TEXT` 列 → 拆成 4 列：
  ```sql
  gdr_messages_path  TEXT,
  gdr_openai_path    TEXT,
  gdr_qwenjina_path  TEXT,
  gdr_meta_path      TEXT,
  ```
- `CREATE TABLE IF NOT EXISTS` 对已存在表不补列；按决策"直接删除重建"，
  `_init_schema` 需在检测到旧列存在时 DROP TABLE tasks 并重建
  （或要求使用者删 db 文件后重开）

#### `orchestration/queue/sqlite_queue.py`

- line 50-67 `Task` dataclass：`gdr_output_path: str|None` →
  4 个字段 `gdr_messages_path / gdr_openai_path / gdr_qwenjina_path / gdr_meta_path`
- line 148-166 `_row_to_task`：row 映射改 4 字段
- line 278-298 `_pull_n` 的 RETURNING 列表：`gdr_output_path` → 4 列
- line 362-378 `mark_gdr_done` 签名：
  `mark_gdr_done(task_id, *, gdr_messages_path, gdr_openai_path, gdr_qwenjina_path, gdr_meta_path)`
  UPDATE SET 4 列
- line 622-636 `list_tasks_for_batch` SELECT 列表改 4 列
- line 671-684 `get` SELECT 列表改 4 列
- `_init_schema` line 133-142：增加旧表检测 + DROP 重建逻辑
  （检测 `gdr_output_path` 列存在则 `DROP TABLE tasks` 后重建）

#### `orchestration/workers/gdr_worker.py`

- line 7-8 文档字符串更新
- line 81 `self._output_name(task, session_id, suffix="_refined")` →
  生成 base name（不带扩展名），再派生 4 个完整路径
- line 100 `_process_one_file` 返回值解包 4 路径
- line 108 `return out_path` → 返回 4 路径的 dataclass / dict
  （`BaseWorker.process` 契约是 `-> Path`，需改为 `-> GdrOutputs` 或
   保持返回主路径 messages + 侧带通道；见 §5 契约讨论）
- line 114-120 `mark_done` → 传 4 路径给 `mark_gdr_done`
- `_output_name` helper（`base_worker.py:99`）：gdr 需 4 后缀，
  qf 仍单文件；考虑给 `_output_name` 加多后缀支持或在 gdr_worker 内派生

#### `orchestration/workers/base_worker.py:53,57`

- `process` 返回类型 `-> Path` 与 `mark_done(task, output: Path)` 契约：
  gdr 拆 4 份后返回的不止一个 Path。方案：保持 `process -> Path`（返回
  messages 主路径），`mark_done` 内部从 worker 实例取其余 3 路径
  （worker 在 `process` 里把 4 路径存到 `self._last_outputs`）；
  或改契约为 `process -> Any`、`mark_done(task, output: Any)`。
  倾向前者，改动小且不破坏 qf worker。

#### `orchestration/failure_handler.py:56,72`

- line 56 SELECT 列表：`gdr_output_path` → 4 列
- line 72 `for col in (...)` 循环：改成遍历 4 个新列名
- 归档逻辑不变（每个非空路径都 move 到 dead_dir）

#### `orchestration/config_loader.py`

- 不用改（`gdr_output_dir` 不变）

### 4.3 测试（断言全改）

| 文件 | 改动点 |
|---|---|
| `tests/orchestration/test_gdr_worker.py` | line 110/127-133 mock 返回值改 `outputs` 形态；line 145 `out_path == gdr_out / "s1_refined.json"` → 4 路径；line 244 `refreshed.gdr_output_path` → 4 字段；line 373 `(gdr_out / "late_refined.json").exists()` → 4 文件 |
| `tests/orchestration/test_smoke_3task.py` | line 43-47 `_patch_gdr` mock 写 4 文件 + `mark_gdr_done` 4 路径；line 142-145 `glob("*_refined.json")` → 4 命名 × 3 task |
| `tests/orchestration/test_master.py` | line 146/149/355 out 断言 + `mark_gdr_done` 调用 |
| `tests/orchestration/test_failure_recovery.py` | line 91-95 `_patch_gdr` mock 写 4 文件 + `mark_gdr_done` 4 路径 |
| `tests/orchestration/test_stage_timestamps_naming_backoff.py` | line 135/137 `mark_gdr_done(gdr_output_path=...)` → 4 路径；line 228 `_output_name(suffix="_refined")` 断言 → base name 或 4 后缀；line 166-198 旧库迁移测试：旧库带 `gdr_output_path` 列，按"直接删除重建"决策，该测试应改为"旧库打开后 tasks 被 DROP 重建"或删除该迁移测试 |
| `tests/orchestration/test_queue.py:186` | `mark_gdr_done(gdr_output_path=...)` → 4 路径 |
| `tests/orchestration/test_health.py:25` | `mark_gdr_done(gdr_output_path=...)` → 4 路径 |
| `tests/orchestration/test_failure_handler.py:22` | `mark_gdr_done(gdr_output_path=...)` → 4 路径 |
| `gdr/tests/test_runner_load_session.py:44,58` | `out_path = .../s1_refined.json` → 4 路径；`out_path.exists()` → 4 文件存在 |
| `gdr/tests/test_consistency_and_writeback.py:130-341` | metadata 落盘断言 → 改读 `meta.json`；`session.metadata` 访问 → 从 meta.json 反序列化 |
| `gdr/tests/test_tools_whitelist_and_votes.py:183,202` | `out.metadata.get("unknown_tool_names")` → 读 `meta.json` |

### 4.4 不用改

- `etl/qwenformat/transform.py`：它在 qf 阶段产生 `openai_messages` / `qf_text`
  写进 metadata，gdr 读入时已有；拆分在 gdr 输出阶段做，transform 不动
- `orchestration/workers/qf_worker.py`：qf 阶段仍单文件输出，不涉及
- `orchestration/workers/base_worker.py` 退避/重试逻辑：不涉及路径形态

## 5. 契约讨论：process / mark_done 返回类型

`BaseWorker` 契约 `process(task) -> Path` + `mark_done(task, output: Path)`。
gdr 拆 4 份后返回不止一个 Path。两个方案：

- **A（小改）**：`process` 仍返回主路径（messages），4 路径存 `self._last_outputs`；
  `mark_done` 从实例取其余 3 路径。qf worker 不受影响。
- **B（改契约）**：`process -> Any`、`mark_done(task, output: Any)`，
  gdr 返回 dataclass，qf 返回 Path。

倾向 **A**，改动局部化，不波及 qf worker 与 base_worker 契约。

## 6. 风险点

1. **SQLite 旧库检测**：`_init_schema` 需可靠检测 `gdr_output_path` 列存在并 DROP。
   若用户 db 文件残留，首次打开会 DROP 全表（含在途 task）——按决策可接受。
2. **`test_legacy_db_migrated_with_new_columns`**：该测试假设旧库幂等 ALTER 补列，
   与"直接删除重建"决策冲突，需改为重建语义或删除。
3. **mock 返回值形态**：多处测试 mock `_process_one_file` 返回
   `{"status": "success", "output": ...}`，改返回 `{"outputs": {...}}` 后这些 mock
   全要同步改，否则 gdr_worker 解包失败。
4. **qf_text 缺失**：部分测试 fixture 可能没造 `qf_text`，拆分后 qwenjina.txt 不写，
   断言其存在会失败——需在 fixture 里补 `qf_text` 或断言改为"不要求存在"。
5. **`_output_name` suffix 语义**：当前返回带 `.json` 的完整文件名，gdr 拆 4 份需
   base name（无扩展名）派生 4 后缀。qf 仍用旧形态。需在 gdr_worker 内派生，
   不改 `_output_name` 本身（避免影响 qf）。

## 7. 执行顺序

1. `gdr/domain/schema.py` — `save_session` 改写 4 份
2. `gdr/pipeline/runner.py` — `_resolve_output` + `_process_one_file` 返回值
3. `orchestration/queue/schema.sql` + `sqlite_queue.py` — 表结构 + Task + mark_gdr_done
4. `orchestration/workers/gdr_worker.py` — process + mark_done + 派生 4 路径
5. `orchestration/failure_handler.py` — SELECT + 路径读取
6. 测试全改
7. 跑 `uv run python -m pytest -q` 验证（注意既有测试间污染，用修改前后失败数对比）

## 8. 实施条件评估（已核实，2026-09-09）

结论：**具备实施条件，无阻碍性前置依赖。** 方案引用的文件/行号/签名/字段名
与当前代码完全一致，无错位。

### 8.1 核实结果

| 引用点 | 现状 |
|---|---|
| `gdr/domain/schema.py:212 save_session(session, output_path)` | 单参输出，符合 |
| `gdr/pipeline/runner.py:649/671/675/741` | `_process_one_file` / `save_session` 调用 / `{"output":...}` / `_resolve_output` 均在位 |
| `orchestration/queue/schema.sql:15 gdr_output_path` | 在 |
| `sqlite_queue.py` Task / `_row_to_task` / `_pull_n` / `mark_gdr_done` / `list_tasks_for_batch` / `get` | 6 处全部命中方案行号 |
| `gdr_worker.py:81/108/120`、`base_worker.py:53/57`、`failure_handler.py:56/72` | 全部命中 |

### 8.2 改动闭环完整性

- grep 确认 `gdr_output_path` 在 orchestration 非测试代码中仅有 3 个消费者
  （`sqlite_queue` / `gdr_worker` / `failure_handler`），方案已全覆盖；
  `master` / `daemon` / `health` / `qf_worker` / `base_worker` 退避逻辑均不引用。
- `_refined.json` 无任何代码内下游；`etl/qwenformat/transform.py` 是 qf 阶段
  **产生** `openai_messages` / `qf_text`，不读 gdr 输出，§4.4「不用改」成立。

### 8.3 补充发现（需纳入改动清单，原方案未列出）

1. `gdr/tests/test_consistency_and_writeback.py:282/296` — 测试直接
   `from domain import save_session` 并按旧签名 `save_session(session, input_path)`
   调用；拆分后签名与输出形态变更，该调用点也需改（§4.3 只提了 metadata
   断言改读 meta.json，漏了此调用点）。
2. `gdr/domain/__init__.py` 导出 `save_session` — 改签名后保持同名导出即可，
   无需额外改（确认即可）。

### 8.4 契约决策确认

采纳方案 A：`process` 返回 messages 主路径，4 路径存 `self._last_outputs`；
`mark_done` 从实例取其余 3 路径。改动局部化，不波及 qf worker 与 base_worker 契约。
