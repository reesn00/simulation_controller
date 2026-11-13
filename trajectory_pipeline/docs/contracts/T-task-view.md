# T · 完整任务视图契约

> 交接面是**格式**，不是 Python 对象——本文描述 `task_views/<task_id>__<run_id>.json` 的字段，
> 任何一侧改形状都应在此同步。
> **渲染源是 P1**，不从 P2 反推（D14.3，见[设计方案 14](../设计方案/14-完整任务视图与LS上传.md)）。
> 「T 渲染不读 P2」由 `tests/contract/test_t_contract.py::Test模块图::test_task_view不读P2` 用 AST 强制。
>
> ⚠️ **2026-10-30 · 方案 23（per-Run）**：T 本来就是 per-Run，但**P3 也改成
> per-Run 了** ⇒ T 的 `trajectory_id` 与 P2 的 `sample_id` **逐字相同**
> （也与 P1 存档名相同）。任务级结论同步换成裁定 ②「**看最后一个结论**」。

## 与 P3 的对照

> ⚠️ **2026-10-30 · 方案 23（per-Run）**：**P3 也改成 per-Run 了**——
> 以前 P3 是「一个决策单元一条样本」，现在是「一个任务一条轨迹」。
> ⇒ 两边的**主键逐字相同**，且都与 P1 存档名相同。

| 维度 | P3（per-Run） | T（per-Run） |
|---|---|---|
| 主键 | `<task_id>__<run_id>` | `<task_id>__<run_id>` —— **与 P3 逐字相同** |
| 落点 | `output/pipeline/views/<id>_{messages,openai,meta}.json` | `output/pipeline/task_views/<task_id>__<run_id>.json` |
| 消费方 | SFT 训练 + **LS ⑩ 块展示**（2026-11-10 起） | LS 上传（⑧）/ L4 任务级复核 / 人工复核 |
| 渲染源 | P2（per-Run 样本） | P1（per-Run 存档） |
| 切条逻辑 | `split_archive` **0 或 1 条**（一个 Run 一条轨迹） | **不切条**——per-run 完整一份 |
| `step_index` | 轨迹内全局（0..N-1，整条流摊平） | 全 run 重编（0..N-1） |
| `tool_call_id` | `call_<step>` 全局稠密 | `call_<step>` 全局稠密重编 |
| 缺件标记 | `degraded_from`（**轨迹级**缺件） | `degraded_from`（**恒为 `[]`**，见下） |
| 任务级结论 | `outcome`（裁定 ②：末条结论） | `overall_branch`（**同一条口径**） |
| 站点级明细 | `site_outcomes`（带全局步号区间） | `unit_outcomes`（按访问点枚举，带 `matched`） |

## 分池（方案 23 R3-3）

与 P3 同款：交付集**只含 `camofox-1`**，旧面按 O-3 进「只读存量」归档
（**移动而非删除**）：

```
output/pipeline/task_views/<task_id>__<run_id>.json              # 交付面
output/pipeline/legacy/<surface>/task_views/<task_id>__<run_id>.json  # 只读存量
output/pipeline/pool_meta.json                                   # 批次账
```

面从**源存档的 `driver`** 折算（`meta.tool_surface`）——认不出的 driver
直接抛（`surface_of_driver` 的老纪律：不猜）。`--all` 重建时旧面 P1 仍
可渲染，但落进它自己的 legacy 池：重建既有证据 ≠ 新增交付样本。

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
| `trajectory_id` | str | `<task_id>__<run_id>` —— 排序即时间序，从 id 反查 P1。⚠️ **per-Run 之后它与 P2 的 `sample_id` 逐字相同**（也与 P1 存档名相同）——这是 per-Run 化才**新成立**的性质：以前 T 是 `<task>__<run>`、P3 是 `<task>__<run>__<unit>`，两边对不上，要从 T 反查 P3 得靠 `unit_sample_ids` 拼 |
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
| 1 | `system` | T 自声明的 `_TASK_SYSTEM_PROMPT`（与 `assembler.schema.SYSTEM_PROMPT` 逐字一致） | 空则不发（与 P3 同款） |
| 2 | `user` | P1 `user_prompt`（persona 渲染后） | **只发一次**；空 `user_prompt` 不发空消息（与 P3 判断 4 同款） |
| 3..N | `assistant` / `user` 交替 | P1 顶层 `steps[]` + `visits[i].steps[]`（按时间序） | 每步一条；assistant 内容 = 该步 `rationale`（思考 + 结论，缺则不补空串）在前、`[动作] <tool>(…)` 在后；user 内容 = 该步观察渲染文本（见 §3） |
| 末尾 | `assistant` | 任务级 summary | **没有 `tool_calls`**；内容从**任务级结论**（裁定 ②：末条结论，剔后取）+ 站点级 outcome 聚合派生 |

### 拼接顺序与时序

- search 单元的 `steps[]` **整段前置** —— 紧接 user_prompt；
- 之后按 `visits[]` 数组顺序拼接 `visits[i].steps[]`；
- **跨 unit `step_index` 全局重编**：search 单元 N 步 → 全局 0..N-1；site-1 单元 M 步 → 全局 N..N+M-1；……

### 动作行格式

与 P3 `_action_line` 同款：
- `[动作] <tool>(k1=v1, k2=v2)`
- `click` 语义目标并进 `arguments`（不嵌套，与 P3 判断 3 同款）；键名跟着方言走：obscura 面 `{tag, label}`、camoufox 面 `{role, name}`、**camofox 面 `{role, name}`**。`arguments` 在**消息视图的 `tool_calls` 里是 JSON 字符串**（方案 23 R3-4），本行描述的是拼平后的键集合——`[动作]` 行是纯文本，不涉及序列化
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
  - <tag> <label>        ← obscura 面（历史批次全是这一种）
  - [role] name          ← camoufox 面
  - [role] name          ← camofox 面（2026-10-26 起的生产面，与 camoufox 面同形）
…

链接：
  - <text> → <href>
…

video 标签 <n> 个，iframe <n> 个
```

三条规则与 P3 一致：① 空观察渲染成 `（本步没有取到页面内容）`，不空串；② 带 `degraded` 时末尾追加 `[采集降级: …]`；带 `truncated` 时追加 `[内容不完整: …]`；带 `action_error`（P1 `steps[i].error`）时追加 `[本步动作无效: …]`——**与采集降级分列**（方案 23 R1-2）；③ T 从 P1 的**取证态全文**出发，自己跑一遍与 P2 同款的裁剪（`observation_view.render`，普通步 **320/10/4**、读步 **2000/50/50** + 下一步动作目标 `keep`），因为 T 不读 P2。

观察带 `body_refined` 时追加 `[内容已精简: 保留 N 行]`（N 取 `body_refined_lines`）——与 P3 **逐字同款**，排在 `[采集降级: …]` 之后、`[内容不完整: …]` 之前（方案 25）。⚠️ 这一条**不能省**：正文被精简过这件事模型从文本里看不出来，不说破就是在训练侧教出来的幻觉。T 与 P3 是**两份实现、逐字同款**，由 `tests/contract/test_t_contract.py::TestRenderObservationSync` 盯着。

**读步回执**（方案 23 R4-1）：`observation.read` 非空时排版切换成
`[读取: 正文|可交互元素|链接]` + 只排版被读的那一块，**不排 counts**
（读步的 `video_tag_count` / `iframe_count` 恒为 0，排出来是假计数）；
空块排成「（空）」，与「没读」区分开。与 P3 的
`views.render_observation` 是两份实现、逐字同款——`Test投影一致` 里有一条
带读步的用例盯着。

**精炼过的正文**（方案 25）：T 的正文也可以是**模型选行**裁出来的
（模型只报行号，文本由 `observation_view.render` **从 P1 原文重建**，
见 [P2-sample.md](P2-sample.md) 的「精炼分支」一节）。T **自己读旁挂**
（`--refined-dir`），**不经过 P2**——与「T 不读 P2」那条纪律同款：
T 是 P1 的另一份渲染，交接面是格式不是对象。T 与 P3 的
`[内容已精简: 保留 N 行]` 标记**逐字同款**。

### 任务级 summary（末尾 assistant 消息）

**与 P3 收尾是同一个函数**（`assembler/closing.py::closing_text`，方案 23
R2-2）：同一套模板、同一份原因词表、**同一组事实**——整条 run 的站点
清单、同一个决定性结论、同一个轨迹级 reflection 结论 ⇒ P3 与 T 的末条
assistant **逐字相同**（不再是「同款模板、不同事实」）。

内容来源：

1. **任务级结论**（裁定 ②：末条结论，**剔后**取）→ 面向用户的答复：成功给可播放网址；失败给已排除的站点与原因（只写 host，不带协议）；**不写分支枚举名、不写分支计数**（R2-1）；
2. **不读 `ledger.by_branch`**——分支分布是并池口径，不是助手语言；
3. **轨迹级结论**（2026-11-04）：取**末个写成单元**的 `reflection`
   （`rationale.conclusion`，剔后取）——P1 没有轨迹级 reflection，取法
   真源在 `assembler/closing.py::last_written_reflection`，P2 的
   `reflection` 字段与 T 的收尾走**同一份判断**；
4. **空 trajectory**（即零可见模型动作）不编总结。

判定规则（裁定 ②，**看最后一个结论**——与 P3 判断 7、与 P2 的任务级标签
**同一条**）：

- **末条**结论（**剔后**的末条，见下）`branch is None` ⇒ 成功句（带网址）；
- **末条**结论 `branch is not None` ⇒ 失败句（已排除的站点与原因）；
- 访问过站点但**一条结论都没有** ⇒ 「没有找到可访问站点」；
- 未知分支 → 「原因未能确认，排除」，**不写分支名**。

⚠️ **2026-10-30 · 方案 23**：处置词从「任一成功 ⇒ 成功」改成**最后一个
结论**（裁定 ②）。旧口径会把「中途成功过、后来又失败」的轨迹报成成功——
而那正是训练侧要学的东西。

⚠️ **2026-11-03 · re-base 到 per-Run**：这里的「末条」是**剔后**的末条——
访问级剔除（R4-2）剔掉的访问点，它的结论**不参与**任务级判定。
判据在 `assembler/excluded.py::excluded_outcome_indices`（按 **URL** 配对
给下标，不按位置）。

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
 "function": {"name": "goto", "arguments": "{\"url\": \"https://…\"}"}}
```

`arguments` 是 **JSON 字符串**（OpenAI Chat Completions 契约，方案 23 R3-4），
内容是 `params` 与 `target` 平铺合并的对象（与 P3 判断 3 同款）。
⚠️ 落成 dict 会让 `json.loads` 的下游崩，或更糟——静默按 Python 对象用掉。

### `tools` 元素

与 P3 同款——`{type: "function", function: {name, description, parameters}}`。

- **唯一真源是 [`trajectory_pipeline/toolcatalog/`](../../toolcatalog/)**，T 与 P3
  从同一处取，所以两边的工具声明**逐字一致**（2026-10-20 起不再各抄一份）。
- **按面取**（2026-10-20 方案 15 §7 R1）：T 读 P1，所以面从源存档的
  `driver` 折算（`meta.tool_surface`）。同名工具在三个面里参数不同
  （`click` 是 `tag`/`label` 还是 `role`/`name`）——面错了就是**声明与真实
  调用对不上**，而报告上看不出异常。
  ⚠️ **两个 aria 面的说明文字也不同**（参数相同、说明不同：camofox 面的
  说明里不许出现 exact）——`description` 同样从 catalog 按面取。
- 每个参数给 `{"type": "string", "description": "..."}`——参数语义来自 catalog。
- `required` 按**真实存档的出现率**定（`press` 的 `key` 是 100%、`click` 的
  `tag`/`label` 只有 30% ⇒ 不必填）。
- 可见性由 catalog 的 `visibility` 字段决定：`new_tab` / `close_tab` 是执行器
  自己的会话管理动作，不进。

---

## 两段投影一致性（方案 23 R5-2 / O-5）

`messages` 与 `openai.messages` **必须讲同一个故事**——同一动作序列 /
同一观察内容 / 同一收尾，差异只允许**形态**（纯文本 vs function-calling）。
两段都发：LS 标注员看 `messages`，训练侧喂 `openai.messages`；一旦分叉，
两边看到两个故事，而报表上都是「导出成功」。

逐条对齐口径（实现唯一真源：`trajectory_pipeline/projection.py`）：

| 比较项 | `messages` 侧 | `openai.messages` 侧 |
|---|---|---|
| 动作数 / 工具名序列 | 含 `[动作] ` 的 assistant，取前缀后的工具名 | 带 `tool_calls` 的 assistant，取 `function.name` |
| `tool_call_id` | —（纯文本无 id） | 每步 `tool_calls` **恰好一条**；id 必须按顺序稠密 `call_0..call_n`；观察的 `tool_call_id` 同号 |
| 每步思考 | `[动作]` 之前的文本 | assistant `content`（strip 后） |
| 每步观察 | 该动作的下一条 `role=user` | 该动作的下一条 `role=tool` |
| 收尾 | 末条无动作的 assistant | 末条无 `tool_calls` 的 assistant |

**两处校验，同一个实现**：

- **落盘前**：`build_task_view` 渲染完两段先比，不一致 ⇒ `written=0`、
  `skipped=("投影不一致: …")`，**一份都不写**（fail-closed 不写半份）。
- **导出前**：ls_bridge.exporter.export_trajectory_one 读回 T 视图后再比，
  不一致 ⇒ 抛 `ProjectionMismatch`，export_batch 记
  `skipped=(tid, "两段投影不一致: …")`——**不并入**「T 视图缺失」，
  缺件与内容分叉根因不同，报表要能分开读。

校验逻辑挂在 `trajectory_pipeline/projection.py`（顶层跨层地基）：
ls_bridge 是 evaluation 层，契约测试禁止它 import assembler；T 也不该
反向依赖 evaluation。两边 import 同一函数，**不各写一份**。

---

## `meta` 对象

### 来源/切片轴

| 键 | 类型 | 说明 |
|---|---|---|
| `trajectory_id` | str | 与顶层一致 |
| `task_id` / `run_id` / `title` | str | 切片轴 |
| `source_archive` | str | 据以生成的 P1 文件名（不含目录）。**由调用方注入**（批量路径 `path.name`、单文件路径 `p.name`）；archive dict 里没有自己的文件名，猜不出来——拿不到时为 `""`（「不知道」不冒充文件名），见方案 23 R2-4 |
| `schema_version` | str | T 格式版本 |
| `tool_surface` | str | **工具面**（2026-10-20 方案 15 §7 R1）：这份轨迹的动作空间属于哪个方言（`obscura-1` / `camoufox-1` / **`camofox-1`**）。T 读 P1，所以面从源存档的 `driver` 折算。⚠️ **三个面 ≠ 三套方言**——方言只有两套词表（见 P2 契约 · 工具面一节） |

### 单元级汇总（T 独有的一层；P3 的对应物是 `site_outcomes`）

| 键 | 类型 | 说明 |
|---|---|---|
| `unit_count` | int | 单元数（search + **保留下来的** site-N；被访问级剔除的不算）——**T 仍是按单元读的**（P1 顶层 `steps[]` + `visits[i].steps[]`），但单元**不是交付粒度** |
| `unit_sample_ids` | list[str] | P3 的 `sample_id` 列表 —— **从 T 反查 P3 的入口**。⚠️ **per-Run 之后只有一项**：`<task_id>__<run_id>`（就是这条轨迹自己的 P3 样本 id）。键名保留 `unit_sample_ids` 是为了不牵动下游读档脚本。剔掉的 unit 不在里面 |
| `unit_outcomes` | list[object] | 每个访问点的 outcome 摘要（**完整不省略**；剔掉的访问点不在里面） |
| `excluded_units` | list[{unit, platform}] | **访问级剔除账**（方案 23 A10 / R4-2(b)）：这些 unit 访问了任务声明排除的平台，已从本视图与 P2 训练集里剔掉。空列表 = 没剔。**剔了要看得见**——否则「这条 run 怎么少了一段」只能靠人翻存档 |
| `ledger_summary` | object | P1 `ledger` 原样（`total_sites` / `succeeded` / `negative_samples` / `by_branch` / `missing_branches` / `heuristic_candidates`）。⚠️ 它是**采集侧的账**，不随训练侧剔除变化 |
| `task_gate` | object | **任务级**闸门（裁定 ②）：**剔后**末条结论分支为空 ⇒ `passed`；非空 ⇒ `failed`；一个站点结论都没有 ⇒ `not_run`（判不出来，不算通过） |
| `gate_breakdown` | list[{unit, gate, rationale_missing}] | 逐访问点闸门状态。`gate` 三值：配到了结论且分支为空 ⇒ `passed`；配到了且分支非空 ⇒ `failed`；**没配到结论 ⇒ `not_run`**（缺件，不是成功）。剔掉的访问点不在里面 |

> **判据与 P2 同一份**：T 读 P1 的**逐步** `steps[]`（不读 P2 的
> `Sample.observations`——搜索单元那份只有一张汇总快照），判据在
> `assembler/excluded.py`。两边各自实现调用、结果必须一致；
> `tests/unit/test_excluded.py::TestTaskViewExclusion` 钉着「一处剔一处留」
> 这个静默错误。

### `unit_outcomes` 元素

```json
{"unit": "site-2",
 "matched": true,
 "branch": "no_play_control" | null,
 "branch_label": "找不到播放控件" | "成功",
 "evidence": "...",
 "site_url": "...",
 "play_page_url": "..."}
```

| 元素键 | 含义 |
|---|---|
| `unit` | `site-N`（访问序号，从 1 起） |
| `matched` | **配到结论了没有**——`false` 是**缺件**，**不是**成功 |
| `branch` | 分支名；**为空 = 成功**（与 P1 ledger 同款口径） |
| `branch_label` / `evidence` / `site_url` / `play_page_url` | P1 outcome 的对应字段原样 |

⚠️ **2026-10-30 · 方案 23：配对规则换了**。以前 T **按位置**配
（`visits[i]` ↔ `outcomes[i]`），而那两条序列**不是一一对应**的（孤儿记账、
候选阶段记账、多轮访问）——实测在 109/127 份真实存档（86%）上与 P2 的 URL
配对给出**不同**的结论，其中一份把**成功报成了失败**。现在统一到
`trajectory_pipeline/outcomerules`（**候选 → 落地 → 播放页**逐档试，每档匹
`site_url` **或** `url`，同档取末条）——**T 与 P2 与评估侧读的是同一份口径**。

### 可用性 / 结论

| 键 | 类型 | 说明 |
|---|---|---|
| `gate` | object | **任务级**闸门（与 `task_gate` 同义，保留以保持 P3 字段对齐） |
| `rationale_missing` | bool | 整条轨迹**一条** rationale 都没写成时为 true（**缺一条不算**——那是 `rationale_steps` 里少一行的口径，两件事分开记） |
| `degraded_from` | list[str] | 整条任务缺件（集合并集：任一访问点缺件计入）。**从 P1 事实现算**（方案 23 R2-4），词表与 P2 同款且顺序固定：`user_prompt` / `run_config` / `steps` / `no_body` / `body_preview_only` / `outcome_missing` / `success_branch_mismatch`。`observation_missing` 不在这里（记 `missing_observation_count`），`rationale_missing` 是独立字段。⚠️ **访问级剔除（R4-2）掉的访问点不参与缺件账**——它的缺陷不会进训练集，记进来只会让「这条轨迹还缺什么」指向一段不存在的语料 |
| `overall_branch` | str | **任务级分支**（裁定 ②：**剔后**末条结论）：`success`（末条结论分支为空）/ **末条结论的分支名** / `unresolved`（一个站点结论都没有 ⇒ 判不出来，不是成功） |

### 可回放性自查

| 键 | 类型 | 说明 |
|---|---|---|
| `step_count` | int | 全局动作条数（**不是 per-unit**） |
| `observation_count` | int | 全局观察条数 |
| `infrastructure_actions` | list[str] | 全局执行器自己的动作工具名（去重后） |
| `tools_declared` | list[str] | **全局**模型可见工具清单（来自 catalog）。**含本条没用到的**——工具空间是固定的，不随 run 变 |
| `tools_unused` | list[str] | `tools_declared` 里**这条轨迹没用过**的工具名。⚠️ 不显眼时，读 T 的人会把**能力声明**当成**已验证的动作**。逐个列出（不合成计数） |
| `observation_complete` | bool | `observation_count >= step_count` |
| `missing_observation_count` | int | 全局缺失观察（observation=None 走 ``observation_view.render`` 输出的 ``degraded=["observation_missing"]`` 形态）条数 |
| `closing_summary` | bool | 是否带任务级收尾 summary |
| `deduped_step_count` | int | 两段消息里被剔出的**死循环步**条数（方案 23 R3-1）。口径 = 相邻同动作 + 观察逐字相同，**unit 内**比较 |
| `deduped_steps` | list[int] | 被剔步的**全局原始步号**（与 `rationale_steps.step_index` 同一坐标系），逐条列出 |
| `repeat_step_count` | int | 留在两段消息里的**重复动作**条数（方案 23 R3-2）。被剔的步不算 |
| `repeat_actions` | list[{step_index, unit, tool, occurrence}] | 同一 unit 内同一动作出现 ≥2 次时逐条标记，其中 occurrence 从 2 起。**只标不剔** |
| `rationale_steps` | list[{step_index, status}] | **全局**逐条列出每步 rationale 状态（**不合成计数**） |

### `rationale_steps` 元素

```json
{"step_index": 0, "status": "written"}
```

`status` 带三态（`written` / `unavailable` / `empty`，与 P2 §rationales 同款）；`step_index` 取**全局重编**值（不取 P1 的 unit 内位序）。

---

## 缺失与降级

### 与 P3 的口径差异

| 维度 | P3（per-Run） | T（per-Run） |
|---|---|---|
| `degraded_from` 来源 | **整条轨迹**的缺件键集合 | **整条轨迹**的缺件键集合（**从 P1 事实现算**，方案 23 R2-4；被访问级剔除的访问点不参与） |
| 任务级闸门 | `gate`：P2 的闸门三值**原样透传** | `task_gate` / `gate`：**末条结论**分支为空 ⇒ `passed`，非空 ⇒ `failed`，一个结论都没有 ⇒ `not_run` |
| 逐点闸门 | `site_outcomes` 只带步号区间（不逐点给闸门） | `gate_breakdown` 逐访问点给 `gate`（**没配到结论 ⇒ `not_run`**，不是 `passed`） |
| 缺观察处理 | 显示**全局**的回执 | 显示**全局**的回执；meta `missing_observation_count` 是全局计数 |

### 行为合约：缺件必须看得见

- **P1 没有的键**由 **P2** 记进 `degraded_from`（与 P2 同款：`steps` /
  `user_prompt` / `run_config` / 全文正文等）；**T 视图从 P1 事实现算同一层**
  （方案 23 R2-4，词表与顺序同 P2），缺件在 T 侧另有 `gate` / `task_gate` /
  `gate_breakdown` **显式可见**；
- **P1 字段缺失但不阻断渲染**：仍渲染出 T 视图，缺件必须**显式可见**
  （`gate=not_run` / `meta.task_gate=failed` / `gate_breakdown` 里那条
  `not_run`）；
- **fail-closed 在训练侧的对应物**：与 P3 同款——降级带 `[采集降级: …]` 后缀，模型要学会看到「这块信息不完整」时说判不了。

---

## 任务级 summary 原因词表（`CLOSING_DISPOSITION`）

**字面量的真源在 `assembler/closing.py`**（方案 23 R2，P3 与 T 共用）；
`task_view._CLOSING_DISPOSITION` 是该 dict 的**别名**（`is` 同一对象，
不再是两份手抄），键集与 `executor.branches.BRANCH_LABELS` 的同步仍由
契约测试盯（不 import executor，交接面是格式不是 import）。

```python
CLOSING_DISPOSITION: dict[str, str] = {
    "not_play_site": "不是播放站点，排除",
    "unreachable_hard": "导航失败，排除",
    "no_play_control": "页面上没有播放控件，排除",
    "blocked": "地区限制/验证码/反爬阻断，排除",
    "login_wall_or_blocked": "登录墙/验证码/地区限制，排除",  # deprecated，存量仍读得懂
    # ... 其他分支对应**面向用户的排除原因**（共 21 条，键集与 executor 的分支标签一一对应）
}
```

- 未命中分支 → `UNKNOWN_REASON`（「原因未能确认，排除」），**不写分支名**；
- 成功（`branch is None`）**不查表**——成功是「找到了《片名》的可播放网址：<url>」；
- 词表值**不是**内部处置语（旧口径的「不予记录」「标记未验证」），而是
  用户能听懂的原因；这句话会直接进训练语料与 LS 视图。

`_REQUIRED_DISPOSITION_KEYS` 是同一份键集的字面字符串元组，作用是给一个
「不读 dict、靠 import 触发 KeyError」的早期失败点：词表与 `BRANCH_LABELS`
新增分支时漏改这里、运行到 `_closing_text` 才报 `KeyError`，就太晚了。

## 系统提示词与工具声明（`_TASK_SYSTEM_PROMPT` / `_TASK_TOOL_SPECS`）

T 视图**不 import `assembler.schema`**（T 不读 P2，D14.3），因此两样东西
各自有来源：

- **系统提示词**：`task_view.py::_TASK_SYSTEM_PROMPT` **自己声明**，
  字面字符串与 `assembler.schema.SYSTEM_PROMPT` **逐字一致**——由契约测试
  `tests/contract/test_t_contract.py::Test常量同步::test_SYSTEM_PROMPT与schema一致` 盯住。
  改了 schema 的 prompt，T 这边**必须**同步改。
- **工具声明**：⚠️ **不再自己抄**（2026-10-20 改）。以前 T 抄了一份
  `_TASK_TOOL_SPECS`，靠「复现一次 + 契约测试盯同步」防漂移；工具从 6 条
  扩到 13 条、每条多两个字段之后，手抄两份必漂。现在 `_TASK_TOOL_SPECS`
  **从 `trajectory_pipeline/toolcatalog` 派生**——那份是唯一真源，P2 / P3 / T
  三方都从它取，**漂移在结构上不可能**。catalog 是跨层地基（不 import 任何
  功能模块），取它**不违反**「T 不 import `assembler.schema`」。

这条纪律很关键——T 视图与 P3 是**两种不同的渲染**（T 是 P1 的另一份渲染，
P3 是 P2 的另一份渲染），如果改了一边忘改另一边，LS 上传视图与 SFT 训练
视图用的就是不同 prompt，模型看到的「系统」飘忽不定。

---

## 与现有契约的衔接

| 数据 | T 用什么 | 不干什么 |
|---|---|---|
| P1 存档 | **直接读** —— 唯一渲染源（D14.3） | 不读仓库根 `output/`（D8） |
| P2 样本 | **不读** —— `Test模块图::test_task_view不读P2` AST 强制 | 不 import `assembler.schema` |
| P3 视图 | **不读** —— T 与 P3 是 P1 / P2 的对称渲染 | 不 import `assembler.views` |
| T 视图 | 本文描述的形态 | 重建时不依赖 executor / builder / views 当前版本 |

---

## 相关文档

- [设计方案 14](../设计方案/14-完整任务视图与LS上传.md) —— D14.1–D14.6 决策基线
- [设计方案 23](../设计方案/23-每任务一轨迹.md) —— per-Run 改造（owner 裁定 ①②③）：粒度改成「一个任务一条轨迹」、任务级结论看最后一个、站点级明细留在轨迹内
- [P1 契约](P1-observation.md) —— T 的渲染源
- [P2 契约](P2-sample.md) —— P3 的渲染源
- [P3 契约](P3-views.md) —— SFT 训练视图
- [数据契约](../03-数据契约.md) —— 四层契约的分工