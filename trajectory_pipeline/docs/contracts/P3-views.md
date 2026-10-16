# P3 训练视图 · 字段级契约

**产出方**：渲染 [`assembler/views.py`](../../assembler/views.py)（`to_messages` / `to_openai_messages` / `to_meta`），落盘收拢在 `storage/`（`write_p3` / `read_p3`）
**消费方**：训练框架（`messages.json` 保底形态 / `openai.json` function-calling 形态）、人工复核与批次筛选（`meta.json`）
**落点**：`trajectory_pipeline/output/pipeline/views/<sample_id>_{messages,openai,meta}.json`

> 本文以 **`views.py` 的实际输出**为准，不是设计意图的转述。
> 由 `tests/contract/test_p3_contract.py` 盯住——**代码加了字段而本文没跟上
> （或反过来）会红**。
>
> 与 P1/P2 的纪律一脉相承：**交接面是格式，不是对象**。
> P3 的输入是 **P2 的 JSON dict**，不是
> :class:`~trajectory_pipeline.assembler.schema.Sample`——P2 便宜、P3 更便宜，
> 重建一批 P3 必须能脱离 `executor` 与 `builder` 的当前版本独立完成。
> 由 `tests/contract/test_p3_contract.py::TestNoExecutorImport` 用 AST 强制。

---

## 三条总口径

1. **三份视图各管一件事**：`messages.json` 是任何 chat 框架都能吃的
   `{role, content}` 序列（**不做工具调用强绑定**，训练框架不认识
   `tool_calls` 时退回读它）；`openai.json` 是精确的 function-calling 形态
   （assistant 带 `tool_calls`、观察独立成 `role: "tool"` 消息靠
   `tool_call_id` 关联）；`meta.json` 是元数据（**训练代码不看它，
   人工复核与批次筛选必须看它**）。
2. **以 `sample_id` 为唯一文件主键**：三份文件名
   `<sample_id>_{messages,openai,meta}.json`。`sample_id` 与 P2 完全一致
   （`<task_id>__<run_id>__<unit>`），排序即时间序，从 id 就能反查 P1，
   不必开文件。
3. **降级样本照常渲染，缺件必须看得见**：`degraded_from` 非空的样本是
   真实数据只是缺件，渲染照常；但 `meta.json` 必须把降级、rationale
   缺失、闸门状态**显式**记下来——因为降级静默渲染等于让缺件不可见。

---

## 文件名与视图名

| 常量 | 值 | 说明 |
|---|---|---|
| `VIEW_NAMES` | `("messages", "openai", "meta")` | 三份视图的后缀名，与 `sample_id` 拼成文件名 |
| `DEFAULT_VIEWS_DIRNAME` | `"views"` | 目录名；`views/` 与 `samples/` 同根（`output/pipeline/`） |

---

## `messages.json`（通用形态）

顶层是**纯数组**：

```json
[{"role": "system", "content": "…"},
 {"role": "user", "content": "…"},
 {"role": "assistant", "content": "[思考段]\n[动作] goto(url=https://…)"},
 {"role": "user", "content": "标题：…\n正文：…"}]
```

| 消息 | 形状 | 规则 |
|---|---|---|
| `role: "system"` | `{"role", "content"}` | 有 `system_prompt` 才发；空则不发 |
| `role: "user"`（首轮） | `{"role", "content"}` | 有 `user_prompt` 才发；**空 `user_prompt` 不发空消息**（见判断 4） |
| `role: "assistant"` | `{"role", "content"}` | 每步动作一条；`content` = 该步思考段（若有）+ `[动作] <tool>(…)` |
| `role: "user"`（观察回执） | `{"role", "content"}` | 动作与观察一一配对；**观察不足时显式发占位** `（本步没有取到页面内容）`（判断 8）；观察多于动作时多出的照实补上 |
| `role: "assistant"`（收尾） | `{"role", "content"}` | **每条轨迹以「任务总结」终结**（判断 7）：从 `outcome` 与 unit 级 `reflection.rationale.conclusion` 确定性派生，无动作（INC46 起不再读末步 rationale） |

工具调用在这里**降级成纯文本** `[动作] goto(url=…)`——这份视图的定位是
「训练框架不认识 tool_calls 时的保底」，要精确的函数调用形态读 `openai.json`。
`role` 只有四种取值：`system` / `user` / `assistant` / `tool`（见各消息行）。

### 动作行（`_action_line`）

- 格式：`[动作] <tool>`；有参数时 `[动作] <tool>(k1=v1, k2=v2)`
- `click` 的语义目标 `{tag, label}` **并进参数**（`tag=…, label=…`），
  不塞嵌套字段——见判断 3。
- `params` 为空且无 target 时只有 `[动作] <tool>`。

---

## `openai.json`（function-calling 形态）

顶层是对象：

```json
{"sample_id": "T001__b98b3e56__site-5",
 "messages": [...],
 "tools": [...]}
```

| 键 | 说明 |
|---|---|
| `sample_id` | 与 P2 一致，**排序即时间序** |
| `messages` | 对话序列（见下） |
| `tools` | 模型看得见的工具声明（**不含** `new_tab`，见判断 2） |

### `messages` 的元素

| 消息 | 形状 | 规则 |
|---|---|---|
| `role: "system"` | `{"role", "content"}` | 有 `system_prompt` 才发 |
| `role: "user"`（首轮） | `{"role", "content"}` | 有 `user_prompt` 才发；空不发 |
| `role: "assistant"`（每步） | `{"role", "content", "tool_calls": [...]}` | `tool_calls` 恰好一条；`content` 是该步思考段（无则 JSON null） |
| `role: "tool"`（观察） | `{"role", "tool_call_id", "content"}` | `tool_call_id` 与对应 assistant 的 `id` 相同；观察与动作一一配对，**缺观察显式发占位回执**（判断 8） |
| `role: "assistant"`（收尾） | `{"role", "content"}` | 「任务总结」（判断 7），**没有 `tool_calls`**；空轨迹（无可见模型动作）不发 |

- **`tool_call_id` 由可见步序号派生**（`call_0` / `call_1` / …），不取自
  P1 的任何字段——P1 里没有可用的调用 id，而序号是确定的、不是猜的
  （与 `builder` 用 `sample_id` 反查源存档同款：能从数据算出来的不另找来源）。
  `origin == "infrastructure"` 的步整步跳过后序号仍**稠密**——
  消息流里不存在的动作不该占号（判断 2）。
- **观察是 `role: "tool"` 不是 `role: "user"`**（判断 1）：`tool_call_id`
  让模型学到「这条观察是对**那个**动作的回应」；退成 user 消息就把关联
  抹平了，而关联正是 agent 轨迹的训练信号。

### `tool_calls` 元素的形状

```json
{"id": "call_0", "type": "function",
 "function": {"name": "goto", "arguments": {"url": "https://…"}}}
```

| 字段 | 说明 |
|---|---|
| `id` | `call_<step>`，与观察消息的 `tool_call_id` **互相咬合** |
| `type` | 恒为 `"function"` |
| `function` | 内层对象 |
| `name` | 内层于 `function`：工具名 |
| `arguments` | 内层于 `function`：**一个 dict**——`params` 与 `target` **平铺合并**（判断 3）。`click` 没有 params、语义目标就在 target 里；`goto` 的 url 在 params 里。两者键空间不冲突，合并即可，**不造嵌套字段** |

### `tools` 元素的形状

```json
{"type": "function",
 "function": {"name": "goto",
              "description": "…",
              "parameters": {"type": "object",
                             "properties": {"url": {"type": "string"}},
                             "required": ["url"]}}}
```

| 字段 | 说明 |
|---|---|
| `type` | 恒为 `"function"` |
| `function` | 内层对象 |
| `name` | 内层于 `function`：工具名 |
| `description` | 内层于 `function`：说明文字（来自 P2 六件套的 `instruction`） |
| `parameters` | 内层于 `function`：`{"type": "object", "properties": {...}, "required": [...]}` |
| `properties` | 内层于 `parameters`：每个参数 `{"type": "string"}`——P2 里存的是参数名元组，本模块刻意不编造参数类型（理由见下） |
| `required` | 内层于 `parameters`：顶层必填参数名列表（本工具内与 `properties` 键一致） |

- **每个参数都给 `{"type": "string"}`**——P2 里存的是参数名元组，本模块
  刻意**不编造每步的参数类型**：凭空补类型会让训练出来的模型以为参数
  校验是训练出来的，而真实执行层的校验在别处。
- **可见性是 `ToolSpec.visible_to_model` 的复现**：`name == "new_tab"` 的
  不进 `tools`（判断 2）；其余全部声明。可见性判定与
  `assembler/schema.py` 的 `TOOL_SPECS` 必须同步，由契约测试盯住。

---

## `meta.json`（元数据）

顶层是对象，四组职责：**来源**（从 id 反查 P1）、**可用性**（能否进训练集）、
**结论**（批次级断言原料）、**可回放性自查**（动作与观察是否成对）。

| 键 | 类型 | 说明 |
|---|---|---|
| `sample_id` | `str` | 与 P2 一致 |
| `task_id` | `str` | 切片轴 |
| `unit` | `str` | `"search"` 或 `site-N` |
| `title` | `str` | 任务标题 |
| `source_archive` | `str` | 据以生成的 P1 文件名（不含目录），批次可回溯 |
| `gate` | `object` | **闸门三值原样透传**（`passed` / `failed` / `not_run`）——`not_run` 是「没测过」不是「通过」 |
| `rationale_missing` | `bool` | 整条一条 rationale 都没写成。与 `gate` **分开记**：`gate=passed` 但这里为 true ⇒ 只有动作没有思考 |
| `degraded_from` | `list[str]` | 非空 = 这条是降级重建的，别与新版样本同等看待 |
| `outcome` | 对象或 null | 指回 P1 的那一条（含 `branch`）；无则 JSON null |
| `provenance` | `object` | 采样切片轴（P2 的 `six.provenance` 原样） |
| `step_count` | `int` | 动作条数 |
| `observation_count` | `int` | 观察条数 |
| `infrastructure_actions` | `list[str]` | 执行器自己做的动作（`origin=infrastructure`，如 `new_tab`）的工具名清单 |
| `observation_complete` | `bool` | `observation_count >= step_count`。动作多于观察 ⇒ 末步没取到观察；消息视图显式发占位回执（判断 8），但批次筛选仍要能一眼分出「观察齐全」与「有步没采到」 |
| `missing_observation_count` | `int` | 缺失观察占位（P2 `MISSING_OBSERVATION`，`degraded` 含 `observation_missing`）的条数。占位多的样本观察面薄，复核优先级该高 |
| `closing_summary` | `bool` | 是否带「任务总结」收尾（判断 7）。false 即「这条一个可见模型动作都没有」 |
| `rationale_steps` | `list[{step_index, status}]` | **逐条**列出每步 rationale 的状态——不合成一个计数：合成之后就分不出「缺中间那步」与「缺最后那步」，而两者的处置不同 |

### `rationale_steps` 元素

```json
{"step_index": 0, "status": "written"}
```

`status` 带 P2 的三态（`written` / `unavailable` / `empty`，原样透传）。
`step_index` 取**条目自带值**（动作的步号），不按列表位置重编——
`align_rationales` 的产出是稀疏序列（中间步没写成就跳号），重编会把
「缺中间那步」抹成「缺最后那步」，而两者的处置不同。

---

## 观察渲染（`render_observation`）

观察文本（`messages` 的 user 回执与 `openai` 的 tool 消息的 `content`）
按以下结构排版：

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

三条渲染规则：

1. **空观察渲染成 `（本步没有取到页面内容）`，不是空字符串。**
   一条 `content: ""` 的 tool 消息在训练时会被当作「页面真的什么都没有」，
   而观察为空有三种成因（没采到 / 被拦 / 真的空），空字符串会把它们抹平。
   P2 的缺失占位（`degraded` 含 `observation_missing`）同此渲染——
   占位真值为真只是为了不被下游真值过滤丢掉，语义仍是「没采到」。
2. **如实报账，不静默省略**：观察带 `degraded` 时末尾追加
   `[采集降级: …]`；带 `truncated` 时追加 `[内容不完整: 上游截断/本次裁剪]`。
   模型要学会看到「这块信息不完整」时说判不了，而不是当成页面上真没有
   （fail-closed 在训练侧的对应物）。
3. **这是排版不是裁剪**：P2 里已经是训练态（`observation_view.render`
   已裁到 2000/40/30），这里只做可读性排版，不再动内容。

---

## 八条关键判断（都是「看起来可以随便选、选错就静默毁训练数据」的那种）

1. **观察是 `role: "tool"`，不是 `role: "user"`。** `tool_call_id` 让模型
   学到「这条观察是对**那个**动作的回应」。退成 user 消息就把关联抹平了，
   而关联正是 agent 轨迹的训练信号。
2. **`origin == "infrastructure"` 的步整步不进消息视图。**
   `new_tab` 是执行器自己的会话隔离动作，模型永远不该输出它。早先口径是
   「保留在动作流里，只从工具声明里滤掉」，理由是可回放——但训练视图的
   任务是教模型**它该做的动作**：让模型看到一条它永远不会发、也不该发的
   assistant `tool_calls`，就是教它输出越权动作（2026-10-15 拍板反转）。
   可回放的完整动作流仍在 P2 的 `six.actions` 里原样留着，消息视图按
   `_visible_steps` **整步跳过**（动作连同它的观察与思考），`meta` 的
   `infrastructure_actions` 留痕。工具声明侧照旧由
   `ToolSpec.visible_to_model` 唯一决定。
3. **`click` 的语义目标并进 `arguments`。** `ActionView` 是
   `{tool, params, target, origin}` 四字段，而 function-calling 的
   `arguments` 是**一个 dict**。所以 `target` 的 `{tag, label}` 平铺进
   `arguments`，而不是塞进嵌套字段——后者会让训练出来的模型学会往
   `arguments.target.label` 里填，而真实 API 要的是平铺。
4. **空 `user_prompt` **不发** user 消息，并在 meta 记缺件。** 这与
   `observation_view` 的「空的就是空的，空本身是信号」方向相反，理由：
   观察为空是**合法事实**（页面上确实什么都没有），而用户一句话都不说
   **不是合法事实**——用户开口就是一句诉求。手工路径下它为空是**没采到**，
   发一条空 user 消息会教模型「用户可以什么都不说」。
5. **降级样本照常渲染，但 `meta` 必须显眼。** `degraded_from` 非空的样本
   是真实数据只是缺件；因为降级就拒绝渲染等于把一半数据扔了，因为降级就
   静默渲染等于让缺件不可见。
6. **`rationales` 与动作逐步对齐，缺失就是缺失。** 第 `i` 条只配第 `i` 个
   动作。某一步没写成时**该步没有思考段**（assistant 的 `content` 只有动作
   或为 JSON null），而整条一条都没写成时 meta 记 `rationale_missing` ——
   「流水线没长出来」与「模型这轮没说话」的区分靠这个标记（与 `GateMark`
   的 `not_run` 同一思路）。**绝不补占位文字**：编一段思考会直接污染
   CoT SFT。查找按 `step_index` 建字典（2026-10-15 改），不按列表位置——
   `align_rationales` 的产出是稀疏序列，按位置取会在缺一条时整体串位。

   ⚠️ **INC46（2026-10-15）rationale 的口径是「动作前动机」**：第 `i` 条
   写的是执行第 `i` 个动作**之前**的动机与预期，只依据前 `i` 条观察与
   任务要求，**不得**描述或评价本步结果。渲染顺序不变
   （`rationale[i]` 仍配在 `tool_call(action[i])` 之前）——新口径下这个
   顺序因果自然成立。闸门可见窗口随之改为 `[:step_index]`，本步观察算
   「后见」；step 0 窗口为空，实体核查显式早退，不判幻觉。
7. **每条轨迹以「任务总结」收尾。** 动作流结束后确定性合成一条
   **没有 `tool_calls` 的 assistant 消息**（`_closing_text`）：内容只从
   `outcome`（分支 → 处置词表 `_CLOSING_DISPOSITION`）与 unit 级
   `reflection.rationale.conclusion` 派生，不编新事实。没有收尾的轨迹会教模型
   「发完最后一个动作就结束」，而真实的助手行为是对用户有个交代
   （2026-10-15 拍板补入——此前 302/304 条样本全部以动作终结）。

   ⚠️ **INC46（2026-10-15）结论来源改了**：原来是「末可见步的
   `rationale.conclusion`」。rationale 改成**动作前动机**之后，末步写的是
   「为什么点这一下」——拿它当收尾，整条消息流会以一句「我打算点开看看」
   结束。没有 `reflection` 时退回**纯 disposition 话术**，不拿预期冒充结论。
   处置词表是渲染侧**自己声明**的，与 `executor.branches.BRANCH_LABELS`
   键集的同步由契约测试盯（不 import executor）。**空轨迹不编总结**：
   一个可见动作都没有的样本没有可总结的事。
8. **缺观察的步显式发占位回执，不留悬空 `tool_calls`。** 末步失败没取到
   观察时也发一条「（本步没有取到页面内容）」的 tool 消息——一个发出去
   就没回音的 `tool_calls` 是不合法的 function-calling 序列，训练框架读它
   行为未定义。「没采到」与「页面真空」的区分靠占位文本与 meta 的
   `missing_observation_count`，不是靠消息缺席（2026-10-15 改，原为
   「少一条就少发一条」）。

---

## 与 P1/P2 的衔接

| 数据 | 落点 | P3 用什么 | 不干什么 |
|---|---|---|---|
| P1 存档 | `output/pipeline/<task_id>__<run_id>.json` | **不直接读**——只经 P2 间接承接 | 不读仓库根 `output/`（D8 隔离） |
| P2 样本 | `output/pipeline/samples/<sample_id>.json` | **唯一输入**：`six` 七件套 + `gate` + `outcome` + `degraded_from` 等 | 不 import `executor`、不 import `builder`（交接面是格式） |
| P3 视图 | `output/pipeline/views/<sample_id>_{messages,openai,meta}.json` | 本文描述的三种形状 | 重建 P3 时**不依赖** executor/builder 的当前版本 |

落盘规则（与 `builder.py` 同款）：**写原子化**、样本级错误只进 `skipped`
不打断整批、`overwrite` 默认不覆盖已存在的视图。

---

## 相关文档

- [数据契约](../03-数据契约.md) —— P1/P2/P3 的分工与切分口径
- [架构总览](../01-架构总览.md) —— 模块地图与控制流
- [P1 契约](P1-observation.md) / [P2 契约](P2-sample.md) —— 上游两层的字段级契约