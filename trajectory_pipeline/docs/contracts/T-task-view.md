# T · 完整任务视图契约

> 交接面是**格式**，不是 Python 对象——本文描述 `task_views/<task_id>__<run_id>.json` 的字段，
> 任何一侧改形状都应在此同步。
> **渲染源是 P1**，不从 P2 反推（D14.3，见[设计方案 14](../设计方案/14-完整任务视图与LS上传.md)）。
> 「T 渲染不读 P2」由 `tests/contract/test_t_contract.py::TestNoP2Import` 用 AST 强制。

## 与 P3 的对照

| 维度 | P3 (per-unit) | T (per-run) |
|---|---|---|
| 主键 | `<task_id>__<run_id>__<unit>` | `<task_id>__<run_id>` |
| 落点 | `output/pipeline/views/<id>_{messages,openai,meta}.json` | `output/pipeline/task_views/<task_id>__<run_id>.json` |
| 消费方 | SFT 训练 | LS 上传 / L4 任务级复核 / 人工复核 |
| 渲染源 | P2（per-unit） | P1（per-run） |
| 切条逻辑 | `split_archive` 一个决策单元一条 | **不切条**——per-run 完整一份 |
| `step_index` | unit 内（0..n-1） | 全 run 重编（0..N-1） |
| `tool_call_id` | `call_<step>` per unit | `call_<step>` 全局稠密重编 |
| 缺件标记 | `degraded_from`（per-unit 缺件） | `degraded_from`（整条任务缺件，集合并集） |

---

## 顶层结构

```json
{
  "trajectory_id": "T001__b98b3e56",
  "task_id": "T001",
  "run_id": "b98b3e56",
  "title": "功夫",
  "schema_version": "task-view.v1",
  "messages": [ ... ],
  "openai":   { "trajectory_id": "...", "messages": [...], "tools": [...] },
  "meta":     { ... }
}
```

| 顶层键 | 类型 | 说明 |
|---|---|---|
| `trajectory_id` | str | `<task_id>__<run_id>` —— 排序即时间序，从 id 反查 P1 |
| `task_id` / `run_id` / `title` | str | 切片轴 |
| `schema_version` | str | T 格式版本（写入时记录），与 P1 `schema_version` 同款纪律 |
| `messages` | array | 通用 `{role, content}` 序列 |
| `openai` | object | function-calling 形态（带 `trajectory_id` / `messages` / `tools`） |
| `meta` | object | 元数据 |

---

## `messages` 数组

按时间顺序拼接 search 单元 + 全部 site-N 单元的 assistant/user 交替。

### 消息类型与顺序

| 顺序 | `role` | 来源 | 规则 |
|---|---|---|---|
| 1 | `system` | P1 `SYSTEM_PROMPT` | 空则不发（与 P3 同款） |
| 2 | `user` | P1 `user_prompt`（persona 渲染后） | **只发一次**；空 `user_prompt` 不发空消息（与 P3 判断 4 同款） |
| 3..N | `assistant` / `user` 交替 | P1 顶层 `steps[]` + `visits[i].steps[]`（按时间序） | 每步一条；assistant 内容 = `[动作] <tool>(…)` + 该步 `rationale`；user 内容 = 该步观察渲染文本（见 §3） |
| 末尾 | `assistant` | 任务级 summary | **没有 `tool_calls`**；内容从 `outcomes[]` + `ledger.by_branch` + 全局 outcome 聚合派生 |

### 拼接顺序与时序

- search 单元的 `steps[]` **整段前置** —— 紧接 user_prompt；
- 之后按 `visits[]` 数组顺序拼接 `visits[i].steps[]`；
- **跨 unit `step_index` 全局重编**：search 单元 N 步 → 全局 0..N-1；site-1 单元 M 步 → 全局 N..N+M-1；……

### 动作行格式

与 P3 `_action_line` 同款：
- `[动作] <tool>(k1=v1, k2=v2)`
- `click` 语义目标 `{tag, label}` 并进 `arguments`（不嵌套，与 P3 判断 3 同款）
- `params` 为空且无 target 时只有 `[动作] <tool>`
- `origin == "infrastructure"` 的步整步跳过（动作连同它的观察与思考；与 P3 判断 2 同款）

### 观察渲染

P3 `render_observation` 同款排版：

```text
标题：<page_title>
网址：<url>

正文：
<body_text>

可交互元素：
  - <tag> <label>
…

链接：
  - <text> → <href>
…

video 标签 <n> 个，iframe <n> 个
```

三条规则与 P3 一致：① 空观察渲染成 `（本步没有取到页面内容）`，不空串；② 带 `degraded` 时末尾追加 `[采集降级: …]`；带 `truncated` 时追加 `[内容不完整: …]`；③ T 不动裁剪（裁剪在 P3 侧完成；T 从 P1 拿的是**取证态全文**，按 P3 渲染口径裁剪到训练态）。

### 任务级 summary（末尾 assistant 消息）

**确定性合成，无模板**。内容来源：

1. `outcomes[]` 聚合 → 处置词表（与 `_CLOSING_DISPOSITION` 同款，自行声明）；
2. `ledger.by_branch` 摘要（如「3 站访问 · 1 成功 · 2 失败」）；
3. **不读 unit 级 `reflection`**——T 层级高于 P1 顶层 / visits 一层的 reflection ；
5. **空 trajectory**（即零可见模型动作）不编总结。

判定规则（与 P3 判断 7 同款）：

- 任一 `outcomes[i].branch is None` ⇒ success 处置词；
- `outcomes[i].branch is not None` ⇒ 失败处置词（取首个失败 unit 的分支名进词表）；
- 全部 `outcomes[]` 为空 ⇒ "无可访问站点" 处置词。

---

## `openai` 对象

顶层对象 `{"trajectory_id", "messages", "tools"}`。

| 键 | 类型 | 说明 |
|---|---|---|
| `trajectory_id` | str | 与顶层一致 |
| `messages` | array | 同款 role 序列（见 §messages） |
| `tools` | array | 与 P3 同款 tool 声明，`name == "new_tab"` 不进 |

### `messages` 元素

| 角色 | 形状 | 规则 |
|---|---|---|
| `system` | `{role, content}` | 同 P3 |
| `user`（首轮） | `{role, content}` | 同 P3；只发一次 |
| `assistant`（每步） | `{role, content, tool_calls: [...]}` | `tool_calls` 恰好一条；`content` 是该步思考段（无则 JSON null） |
| `tool`（观察） | `{role, tool_call_id, content}` | `tool_call_id` 与对应 assistant 的 `id` 相同 |
| `assistant`（收尾） | `{role, content}` | 任务级 summary，**没有 `tool_calls`** |

### `tool_call_id` 全局稠密重编（D14.3 + P3 判断 2）

| 来源 | 动作索引 | 全局 `tool_call_id` |
|---|---|---|
| search 单元 steps[0..N-1] | 0..N-1 | `call_0` ... `call_<N-1>` |
| site-1 单元 steps[0..M-1] | N..N+M-1 | `call_<N>` ... `call_<N+M-1>` |
| ... | | |
| site-K 单元最后一步 | N+M+...-1 | `call_<N+M+...-1>` |

跳过 `origin == "infrastructure"` 的步后**序号仍稠密**——消息流里不存在的动作不该占号。

### `tool_calls` 元素

与 P3 同款：

```json
{"id": "call_0", "type": "function",
 "function": {"name": "goto", "arguments": {"url": "https://…"}}}
```

`arguments` 是 `params` 与 `target` 平铺合并的 dict（与 P3 判断 3 同款）。

### `tools` 元素

与 P3 同款——`{type: "function", function: {name, description, parameters}}`；每个参数都给 `{"type": "string"}`；可见性由 `ToolSpec.visible_to_model()` 决定（`new_tab` 不进）。

---

## `meta` 对象

### 来源/切片轴

| 键 | 类型 | 说明 |
|---|---|---|
| `trajectory_id` | str | 与顶层一致 |
| `task_id` / `run_id` / `title` | str | 切片轴 |
| `source_archive` | str | 据以生成的 P1 文件名（不含目录） |
| `schema_version` | str | T 格式版本 |

### 单元级汇总（**T 独有，P3 没有**）

| 键 | 类型 | 说明 |
|---|---|---|
| `unit_count` | int | 单元数（search + 全部 site-N） |
| `unit_sample_ids` | list[str] | 各单元的 P3 `sample_id` —— **从 T 反查 P3 的入口** |
| `unit_outcomes` | list[object] | 每个 unit 的 outcome 摘要（**完整不省略**） |
| `ledger_summary` | object | P1 `ledger` 原样（`total_sites` / `succeeded` / `negative_samples` / `by_branch` / `missing_branches` / `heuristic_candidates`） |
| `task_gate` | object | 整条任务的闸门汇总：`passed` 当且仅当全部 unit `gate=passed` 且无 degraded；否则 `failed` |
| `gate_breakdown` | list[{unit, gate, rationale_missing}] | 逐 unit 闸门状态 |

### `unit_outcomes` 元素

```json
{"unit": "site-2",
 "branch": "no_play_control" | null,
 "branch_label": "找不到播放控件" | "成功",
 "evidence": "...",
 "site_url": "...",
 "play_page_url": "..."}
```

`branch is None` = 成功（与 P1 ledger 同款口径）。

### 可用性 / 结论

| 键 | 类型 | 说明 |
|---|---|---|
| `gate` | object | **任务级**闸门（与 `task_gate` 同义，保留以保持 P3 字段对齐） |
| `rationale_missing` | bool | 整条任务**任一** step 缺 rationale 记 true |
| `degraded_from` | list[str] | 整条任务缺件（集合并集：任一 unit 缺件计入） |
| `overall_branch` | str \| null | 任务级聚合分支：`success` / `failed_<x>` / `unknown`（详细聚合策略见设计方案 14 §4.3） |

### 可回放性自查

| 键 | 类型 | 说明 |
|---|---|---|
| `step_count` | int | 全局动作条数（**不是 per-unit**） |
| `observation_count` | int | 全局观察条数 |
| `infrastructure_actions` | list[str] | 全局执行器自己的动作工具名（去重后） |
| `observation_complete` | bool | `observation_count >= step_count` |
| `missing_observation_count` | int | 全局缺失观察（observation=None 走 ``observation_view.render`` 输出的 ``degraded=["observation_missing"]`` 形态）条数 |
| `closing_summary` | bool | 是否带任务级收尾 summary |
| `rationale_steps` | list[{step_index, status}] | **全局**逐条列出每步 rationale 状态（**不合成计数**） |

### `rationale_steps` 元素

```json
{"step_index": 0, "status": "written"}
```

`status` 带三态（`written` / `unavailable` / `empty`，与 P2 §rationales 同款）；`step_index` 取**全局重编**值（不取 P1 的 unit 内位序）。

---

## 缺失与降级

### 与 P3 的口径差异

| 维度 | P3 (per-unit) | T (per-run) |
|---|---|---|
| `degraded_from` 来源 | 单 unit 缺件键集合 | **集合并集**：任一 unit 缺件计入 |
| `task_gate` 计算 | `passed` 当且仅当本 unit 闸门通过 | `passed` 当且仅当**全部** unit 闸门通过且 `degraded_from` 为空 |
| 缺观察处理 | 显示**该 unit** 的回执 | 显示**全局**的回执；meta `missing_observation_count` 是全局计数 |

### 行为合约：缺件必须看得见

- **P1 没有的键**记进 `degraded_from`（与 P2 同款：`steps` / `user_prompt` / `run_config` / 全文正文等）；
- **P1 字段缺失但不阻断渲染**：仍渲染出 T 视图，缺件必须**显式可见**（`degraded_from` / `gate=not_run` / `meta.task_gate=failed`）；
- **fail-closed 在训练侧的对应物**：与 P3 同款——降级带 `[采集降级: …]` 后缀，模型要学会看到「这块信息不完整」时说判不了。

---

## 任务级 summary 处置词表（`_CLOSING_DISPOSITION`）

T 视图**自行声明**该词表，**不 import executor**。键集与 `executor.branches.BRANCH_LABELS` 同步由契约测试盯（与 P3 同款纪律）：

```python
_CLOSING_DISPOSITION: dict[str, str] = {
    "no_play_site": "搜索结果里没有匹配的资源站点。",
    "unreachable_hard": "尝试访问的站点不可达。",
    "login_wall_or_blocked": "...",
    # ... 其他分支对应处置词
}
```

未命中分支的 word 槽用 default："任务未能完成。"

成功处置词（``success``）**不在该词表里**——T 视图的成功处置是手写字符串
``记录该网址为可播放资源：<url>``，不通过 ``_CLOSING_DISPOSITION`` 查表。

`_REQUIRED_DISPOSITION_KEYS` 是同一份键集的字面字符串元组，作用是给一个
「不读 dict、靠 import 触发 KeyError」的早期失败点：词表与 `BRANCH_LABELS`
新增分支时漏改这里、运行到 `_closing_text` 才报 `KeyError`，就太晚了。

## 系统提示词与工具声明（`_TASK_SYSTEM_PROMPT` / `_TASK_TOOL_SPECS`）

T 视图**自行声明**系统提示词与工具空间，**不 import `assembler.schema`**（T 不读
P2，D14.3）。字面字符串与 `assembler.schema.SYSTEM_PROMPT` / `assembler.schema.TOOL_SPECS`
**逐字一致**——由契约测试 `tests/contract/test_t_contract.py::Test常量同步` 盯住：

- 改了 schema 的 prompt，T 这边**必须**同步改；
- 改了 `TOOL_SPECS` 的工具名/参数/指令，T 这边**必须**同步改。

这条同步纪律很关键——T 视图与 P3 是**两种不同的渲染**（T 是 P1 的另一份渲染，
P3 是 P2 的另一份渲染），如果改了一边忘改另一边，LS 上传视图与 SFT 训练
视图用的就是不同 prompt，模型看到的「系统」飘忽不定。

---

## 与现有契约的衔接

| 数据 | T 用什么 | 不干什么 |
|---|---|---|
| P1 存档 | **直接读** —— 唯一渲染源（D14.3） | 不读仓库根 `output/`（D8） |
| P2 样本 | **不读** —— `TestNoP2Import` AST 强制 | 不 import `assembler.schema` |
| P3 视图 | **不读** —— T 与 P3 是 P1 / P2 的对称渲染 | 不 import `assembler.views` |
| T 视图 | 本文描述的形态 | 重建时不依赖 executor / builder / views 当前版本 |

---

## 相关文档

- [设计方案 14](../设计方案/14-完整任务视图与LS上传.md) —— D14.1–D14.6 决策基线
- [P1 契约](P1-observation.md) —— T 的渲染源
- [P2 契约](P2-sample.md) —— P3 的渲染源
- [P3 契约](P3-views.md) —— SFT 训练视图
- [数据契约](../03-数据契约.md) —— 四层契约的分工