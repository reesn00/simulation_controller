# 待办清单 · Label Studio 集成

> 这里只记**没有改、或改了但没把握**的项。已经改掉的写在各自文档里，不占这个清单。
> 排序按「阻塞真实使用」排，不按发现顺序。
> 每项四栏：**现状 / 为什么没动 / 怎么验 / 影响**。
> 怎么建探针、信号怎么选见 [`label-studio-playbook.md`](label-studio-playbook.md)。
> 最后更新：2026-09-30

---

## P0 · 阻塞

### 1. ~~主项目 `trajectory-sft-quality` 在 LS 上不存在~~ ✅ **2026-09-30 已解决**

已建出 **project 30**，`label_config` 与本地逐字一致（7480 vs 7481 字符，仅差一个
尾换行）。并已推入第一条真实样本 **task 9**（T001，6 条 criterion），台账
`output/label_studio/push_index__30.jsonl` 写入成功，predictions 落上 1 条。

> 排查中一度怀疑 `list_recent_tasks` 的 `/api/projects/{id}/tasks` 端点 404 会导致
> 台账写不进去（去重失效、预标注挂不上）。**已排除**：404 只发生在**空项目**上
> （`"total": 0` 时该路由返回 404），而 `push_batch` 是先 `import_tasks` 再
> `list_recent_tasks`，推的时候项目里已经有 task 了 —— 实测推送时该请求返回 200，
> 台账正常写入。推送路径碰不到空项目状态。

### 2. ~~`perItem` 没有产生逐条结果~~ ✅ **2026-09-30 定性并改掉**

**结论：逐项控件这条路在 LS 1.23 上走不通，已放弃，改为「整块判定 + 自由文本点名」。**

三轮一次性探针定性（探针项目已全部删除）：

| 探针 | 配置 | 结果 |
|---|---|---|
| 31 | `perRegion` + 无 `Filter` | 1 组 |
| 32 | `perItem` + 无 `Filter` | 1 组 |
| 33 | `perRegion` + 有 `Filter` | 冗余，未测 |
| 34 | `List` + `perRegion` | **什么都不渲染** |
| 35 | `List` + `perRegion` + `visibleWhen` | 同上 |

- **属性名不是原因**：31 和 32 结果相同。`<Filter>` 也洗清——32 根本没有 Filter 照样 1 组。
- **根因**（ctx7 查到的官方定义）：`perRegion="true"` **不是「渲染 N 份控件」，是「当前选中的那个 region 适用此控件」**。前提是锚点先产生 6 个 region，而 `<Text value="$列表">` 只产生**一个**——6 条被 `,` 连成一整段文本（这同时让「逐条核对」连读都读不下去）。
- **`<Chat>` 不适用**：官方文档明说直接 import 进 `chat` 数组的消息**不可选**，只有标注员新加的或走 prediction 格式的才可用于评分。
- **`<List>` 在本机不渲染**：官方文档说它正好对路（对象数组 → 逐项 region），但本机 1.23 上不报 "not registered" 也不出列表——**比报错更隐蔽**，已加进标签黑名单静态挡住。

**已实施的替代方案**：

- `task.data["criteria"]`（列表）→ `criteria_text`（换行分隔的单串，6 条各占一行）
- 清单改用 `TextArea` 展示块，**保证换行可读**（不再被 `,` 连成一段）
- `criterion_verdict` 改三选一：`all_agree` / `some_disagree` / `none_agree`
- `criterion_note` 的 placeholder 给出 `criterion_id: 理由` 的写法示例

**代价**：归属变成自由文本，机器没法直接按 criterion_id 聚合。
**换来**：这一区**真的留得下痕**，而判据校准本来就是人对着这几行看。

**部署状态**：project 30 的 label_config 已 PATCH，新样本已推（task 1）。旧 task 9 上 0 条人工标注，已删。

### 3. ~~选项的中文标签（`html=`）~~ ✅ **2026-09-30 浏览器实测通过**

每个 `<Choice>` 加了 `html="中文"`，`value` 保持机器键。**标注页截图确认四组选项
都显示中文**（每一条都认同 / 有哪几条不认同 / 自动判定整体不可信、通过 / 需修改 /
拒绝…），`value` 未动，导出键不变。

> `criterion_verdict` 那三条的 `hint` 一并删了——它和 `html` 文字几乎一字不差，
> 只贡献了标签后面三个角标 `[1][2][3]`。`content_revision` / `overall_decision`
> 的 `hint` 带的是标签里没有的信息（下游该怎么用），保留。

> **已排除的方案：`alias=`。** 服务端 `parsed_label_config` 实测，
> `<Choice value="KEY_C" alias="显示C"/>` 会让 LS 认的选项标识从 `KEY_C`
> **变成** `显示C`（`control_weights` 里也是 `{'显示C': 1.0}`），前端点一下提交的
> 就是中文。这个错没有任何信号：validate 报绿、import 201、标注能提交，只有读
> 导出时才发现对不上。**别用 alias 改显示。** 已加静态测试挡住。


---

## P1 · 标注界面

### 4. ~~`editable="true"` 能不能就地改已提交的值~~ ✅ **2026-09-30 探针实测通过，已改进生产**

探针 project 37（`zz-probe-readonly`）D=`editable="false"` vs E=`editable="true"
maxSubmissions="1"`，**E 提交后能就地继续编辑，D 不能**。展示块 7 个（`audit_view`
/ `scorecard_view` / `risk_hints` / `criteria_view` / `messages_view` /
`qf_text_view` / `metadata_view`）全部由 `false` 改 `true`，已同步 project 30。

这一改把「改完不要点第二次 Add」从**必须小心**变成**有第二条路**——以前修一个
错字只能再 Add 一次，于是多存一条一模一样的 submission（`risk_hints` 实测踩过）。

> `rows > 1` 因此**从"顺便"变成"必须保留"**：Add 按钮在 `rows="1"` 时隐藏，
> 提交入口就没了。已在 `test_display_controls_are_textareas_on_the_anchor` 里钉住。

> **`transcription="true"` 没有试，也不打算试。** 查官方文档发现它是配 `perRegion`
> 用来标记「转写某个 region / 媒体」的语义——我们没有 region，它多半是空转。
> 不值得为验证一个大概率无用的属性占用一次浏览器交互。

### 5. ~~`<Text>` + `<Style>` 的结构性只读~~ ✅ **2026-09-30 配方验通，但决定不做**

探针 project 37 实测两块都成立：

- **`<Text>` 是非控件** —— `parsed_label_config` 里根本不列出它，即挂不上
  submission。这就是"结构上只读"的机制。
- **`<Style>` 在本机 1.23 可用，且 `.htx-text` 类名是对的** —— 探针 C 那块
  6 行带缩进的 JSON 完整保住了。此前"`<Style>` 未验证"的顾虑解除。

**但不做**，理由是「展示块可改」本来就是设计决定（审查本就要纠错），不是被工具
能力卡住的。两件事性质不同，别混：

- 第 4 项解决的是「**我自己的**提交写错了怎么修」→ 该做，已做
- 第 5 项解决的是「**机器生成的**内容不许人改」→ 与既有决定冲突，不做

配方已写进 `label_configs/trajectory_review.xml` 的注释，将来要改可直接照抄：

```xml
<Style>.htx-text { white-space: pre-wrap; }</Style>
```

⚠️ 即便要做也**换不掉 `risk_hints`** —— 它是 predictions 预标注的唯一落点，
换成 `<Text>` 会让整条预测被静默丢弃（`201 {"created": 0}`）。上限是 7 块里 6 块。

---

## P2 · 规模

### 6. 单样本 1.93 MB，1000 条约 1.88 GB

实测 project 27 task 6（一条真实 C3）：

| 部分 | 字节 | 说明 |
|---|---|---|
| `task.data` | 1,312,161 | 其中 **640,760（49%）是不绑任何 UI 控件的结构化原值**：`messages` / `metadata` / `openai` / `scorecard` |
| `annotations` | 709,125 | 几乎全是 5 个展示块提交的值（`messages_view` + `qf_text_view` + `metadata_view` + `scorecard_view` + `audit_view`） |
| **合计** | **2,021,286 ≈ 1.93 MB** | |

| | |
|---|---|
| **为什么没动** | 两处删减都有代价：展示块的提交是「修正稿」——**删了就等于不要标注员的纠正**（已决策接受可改）；结构化原值是给「下游脚本」留的，且 R11 凭据扫描当前扫的是 `data` 里的这些字段，改成扫源文件要动 `export_batch` / `export_one` 的扫描入口。 |
| **怎么验** | 不需要验，是取舍。真要压：`task.data` 里 4 个结构化原值改从源文件读（省 49%），`metadata_text` / `messages_text` 与结构化原值二选一。 |
| **影响** | 1000 条以内无所谓。**上万条**时 LS 自己的库会吃不消，且导出文件会大到难以 `jq`。 |

---

## P3 · 清理

### 7. ~~task 6 那条被改坏的标注~~ ✅ **已随 project 27 一起消失，无需处理**

`messages_view` 少了 168 个字符（system prompt 里的 `# Protected execution contract`
整段），是探索「能不能改」时删的。它在 probe project 27 的 task 6 上，而 27 早已
删除——本条曾长期挂在待办里，实际已经不成立。

### 8. ~~probe 项目 29 / 36 / 37~~ ✅ **2026-09-30 全部删除**

`zz-probe-D3-two-criteria`（29）、`zz-probe-alias-html`（36，为验 `html=` vs
`alias=`）、`zz-probe-readonly`（37，为验 `editable="true"` 与 `<Text>`+`<Style>`）。
连同 27 / 28 / 31 / 32 / 33 / 34 / 35，**LS 上现在只剩主项目 30**。

> 这条待办记录曾经本身就是错的：29 在去删时已经返回 **404**，说明早就没了（多半
> 是更早某次清理顺手删的，待办没跟上）。**待办里"待清理"的东西要先查再写**——
> 记一条已经不存在的资源，比不记还糟。

### 9. ~~task 9 上的评分卡还是旧的 JSON 形态~~ ✅ **2026-09-30 已重推**

`scorecard_text` 从 `json.dumps(indent=2)`（5679 字符，打开停在 evidence 数组中段）
改成渲染后的可读文本（2684 字符，结论先行 + 维度分行 + 依据逐条，见
`observability-label-studio.md` §3.4）。展示块内容在 import 时就定死，改代码不会
回改已推的 task，所以做了一次重置：确认旧 task 9 上 **0 条人工标注**（只有 1 条预标注）
→ 删 task → 删台账那一行 → PATCH 新 label_config → 重推。新 task 是 **task 1**
（LS 在空项目上重新编号）。

> ⚠️ **重置的顺序不能反**：只删 LS 端而不删台账，`push_batch` 会判重跳过，那条样本
> 再也推不回来。脚本里是先查 `total_annotations`，非 0 就中止——**人的工作优先于配置刷新**。

### 10. LS e2e 测试从未跑过

`tests/label_studio/test_e2e_with_real_ls.py` 6 条全 skip（要 `LS_E2E=1` + 8099 在跑）。**修好的 label_config 只在浏览器手工验过，从没走过真实推送路径**——推送侧与配置侧的一致性目前靠单测 + 人工点。

```powershell
$env:LS_E2E = "1"; uv run python -m pytest tests/label_studio/test_e2e_with_real_ls.py -q
```

---

## 已核实 · 无需处理

- **`config/config.yaml` 的 label_studio 段已改对**：`hook.enabled: true`、
  `hook_timeout_seconds: 30`、`on_failure` 已删、`filter_min_training_value_score: 0.0`
  （低分样本不会被 upload 整条跳过，与「低分也推」的设计一致）。
- **导出选型已实测**：`JSON_MIN` 是每个 annotation 一行的扁平行，推荐用它；
  网络流传的三条描述（"只保留 from_name/to_name"、剔 `lead_time`、可按
  `was_cancelled` 过滤）**与本机不符**。详见
  [`label-studio-annotation-export.md`](label-studio-annotation-export.md) §2。
