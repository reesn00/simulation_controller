# P2 · 样本契约

> 交接面是**格式**，不是 Python 对象——所以这份文档描述的是
> `samples/<id>.json` 的字段，任何一侧改形状都应在此同步。
> 「读 P1 不 import `executor`」这条纪律由
> `tests/contract/test_p2_contract.py::TestNoExecutorImport` 用 AST 强制。

## 切条粒度

**一条 = 一个决策单元**：搜索阶段一条，每个站点一条。
不把「选哪个站」与「这个站行不行」压进同一次预测。

## 顶层结构

```json
{
  "sample_id": "T001__b98b3e56__site-5",
  "task_id": "T001",
  "unit": "site-5",
  "title": "功夫",
  "source_archive": "T001__b98b3e56.json",
  "tool_surface": "obscura-1",
  "six": { ...七件套... },
  "gate": { "status": "passed", "reasons": ["逐条核查 1/1 步通过"] },
  "rationale_missing": false,
  "rationale_era": "pre_action",
  "outcome": { ... },
  "reflection": { ... } | null,
  "degraded_from": []
}
```

`sample_id` = `<task_id>__<run_id>__<unit>`，**排序即时间序**，
且从 id 就能反查源存档，不必开文件。

| 顶层键 | 说明 |
|---|---|
| `sample_id` | `<task_id>__<run_id>__<unit>` |
| `task_id` / `unit` / `title` | 切片轴；`unit` 是 `"search"` 或 `site-N` |
| `source_archive` | 据以生成的 P1 **文件名**（不含目录），批次可回溯 |
| `tool_surface` | **工具面**（2026-10-20 方案 15 §7 R1）：这份样本的动作空间属于哪个方言（`obscura-1` / `camoufox-1` / **`camofox-1`**）。来自源 P1 的 `driver` 字段，见下节。⚠️ **三个面 ≠ 三套方言**——方言只有两套词表 |


## 工具面（`tool_surface`）——方言轴

**一份工具声明有两个身份**：执行层**叫哪些名字、收哪些参数**（工具面），
以及它**属于哪个方言**（方言轴）。后者**不是第二个独立轴**——
它由前者**一对一折算**出来（`driver` → 面），所以不需要在 P2 里手填。

为什么要分面：`click` 在 obscura 面是 `click(tag, label)`、
在 camoufox 面与 camofox 面是 `click(role, name)`——**同名不同参**
（两个 aria 面之间则是**参数相同、说明不同**，见下）。
一份 P2 样本只能属于一个面（它记录的是**真跑过**的动作），
而渲染期拿错面就会渲染出与真实调用对不上的声明，
训出来的模型会发「有时按标签点、有时按角色点」的点击——**报告上看不出异常**。

### 三个面（2026-10-26 起）

| 面 | 状态 | 方言 | 目标编码 | 读原语 |
|---|---|---|---|---|
| `obscura-1` | 只读存量（2026-10-22 起不再产出） | `html_tag` | `{tag,label}` | 无 |
| `camoufox-1` | **已退役，只读存量**（2026-10-26） | `aria_role` | `{role,name}` | 有 |
| `camofox-1` | **生产面**（2026-10-26 起） | `aria_role` | `{role,name}` | 有 |

⚠️ **退役不等于失忆；删的是实现，不是证据**。camoufox 执行层已撤下
（实现文件已删、playwright 已从主依赖移除，**禁止接回来**），
但 catalog 里**两张表并存**：`DRIVER_SURFACE`（`obscura` / `camofox`
——**新存档只能落在这两面**）与 `LEGACY_DRIVER_SURFACE`（`camoufox`
——**只为读得懂存量**）。所以盘上 430 份 `camoufox-1` 的 P2 样本与
14 份顶层 `driver="camoufox"` 的 P1 存档**照常渲染、照常切**，
而**新跑的批次落不到旧面上**。

⚠️ **方言轴松了一格**：从「一面一词表」改成「**键名互不相同**，词表可相同」
——三个面 = **两套词表**：`obscura-1` 独自用 `html_tag`，
`camoufox-1` 与 `camofox-1` **共用** `aria_role`。
两个 aria 面的**键名与词表相同，但声明必须不同**：camoufox 面的定位写的是
`get_by_role(role, name=name, exact=True)`（**精确匹配**），
camofox 面的上游走的是 `page.getByRole(role, {name}).nth(nth)`
（**大小写不敏感子串**）⇒ **camofox 面的声明里不许出现 exact**
——写「精确」就是教模型一个**引擎不保证**的性质，而它在报告上看不出异常
（见[设计方案 21](../设计方案/21-camofox-browser执行层接入方案.md)；
契约测试 `test_两张aria面的同名工具说明不同` 钉住这一条）。

- **空串 = 这条 P2 没有工具面身份**（2026-10-20 之前建的）。
  读档时按默认面 `obscura-1` 渲染——那是那批样本**真实跑出来**的面，
  不是「兜底」。
- **认不出的 `driver` 直接抛**（不猜、不静默回落）。`driver` 是执行层
  自己的名字，认不出意味着代码与归档口径分叉了——那属于要人看的事。
  ⚠️ **已退役但有存量存档的实现名不抛**——它折算回自己的旧面
  （`camoufox` → `camoufox-1`）。
- 面的定义与投影在 [`trajectory_pipeline/toolcatalog/`](../../toolcatalog/)，
  契约由 `tests/contract/test_tool_catalog_contract.py` 逐面盯。


## 六件套（`six`）

| 键 | 类型 | 说明 |
|---|---|---|
| `system_prompt` | `str` | 常量，**随样本冻结**——改一次历史样本之间就不可比 |
| `tools` | `list` | 训练侧**工具声明全集**（`toolcatalog` **当前面**的投影：obscura 面 13 条、camoufox 面 16 条、**camofox 面 16 条**；面由顶层 `tool_surface` 定）。**含 `new_tab`/`close_tab`**——它们留在动作流里（删了无法回放）；**模型可见集是渲染时按 `visibility` 过滤出来的子集**（obscura 面 11 条、camoufox 面 14 条、**camofox 面 14 条**），过滤发生在 P3/T 的 openai 视图里，不在 P2。⚠️ 「仅 visible 的那些」是 2026-10-20 之前的**错误描述**（代码一直写的是全集）——见本文档的「口径修正」一节 |
| `user_prompt` | `str` | persona 渲染后的**用户原话**，不是检索式 |
| `provenance` | `object` | 采样切片轴，**控制流不读它** |
| `actions` | `list` | ④ 动作+参数。**没有 `ref`**——会话句柄不可回放 |
| `observations` | `list` | ⑤ 观察原文（可长于训练态渲染） |
| `rationales` | `list` | ⑥ 思考+结论，**与 `actions` 按 `step_index` 对齐**（见下） |
| `reflection` | `object` \| `null` | INC46：**unit 级终局反思**（与 `rationales` 平级，不属于任何一步）。形状同 `WriteResult.to_json`（`status` / `rationale` / `reason` / `degraded`），于是与 `steps[i].rationale` 同构。`null` = 没生成（writer 未接 / 后端不可用 / 生成失败）——与「生成了但内容为空」不同 |

### ⑥ `rationales`（2026-10-10 由单值 `rationale` 改来）

```json
{"step_index": 0, "text": "...", "conclusion": "...",
 "status": "written", "reason": ""}
```

- **与动作逐步对齐**。执行期逐步生成，压成一条就对不上动作，
  而时序核查本来就需要 `step_index`。
- **缺一条不补空串、不挪位**：按出现顺序猜会在中间缺一条时整体错位，
  而错位的 rationale 会被实体核查当成凭空引入的实体——**凭空造出负样本**。
- `status` 原样带 P1 的三态（`written` / `unavailable` / `empty`）。
  后两者 `text` **留空**，不合成占位：「模型这轮没说话」与「压根没生成」
  必须分得开。

### `gate`（三值）

| 状态 | 含义 |
|---|---|
| `passed` | 逐条核查过，无 hard 发现 |
| `failed` | 有 hard 发现，`reasons` **定位到第几步** |
| `not_run` | **没判**——不是「通过」。两种成因：①没有可核查的 rationale；②rationale 是**旧口径**（见下节 `rationale_era`），新闸门的可见窗口对它不适用 |

`rationale_missing`（一条都没写成）与 `gate` **分开记**：
`gate=passed` 但 `rationale_missing=true` 意味着样本只有动作没有思考，
报告上看 `gate` 是看不出来的。

### `rationale_era`（INC46 分代标记）

`pre_action` / `post_hoc` / `""`，三值与 `rationale_missing` 互补而非重复：

| 值 | 含义 |
|---|---|
| `pre_action` | 第 `i` 步的文本是**执行之前的动机**（INC46，2026-10-15 起）。闸门按可见窗口 `observations[:i]` 真跑 |
| `post_hoc` | 第 `i` 步的文本是**看完本步页面之后的反思**（INC46 之前）。它描述 `observation[i]`，而 P3 把它渲染在 `action[i]` **之前**——那是训练在教未卜先知。**这份文本不能按新窗口核**，所以闸门记 `not_run`：不判通过、也不判失败 |
| `""` | 一条 `written` 都没有（= `rationale_missing`）。没什么可判 |

**判据是结构，不是内容**：看 P1 单元（搜索单元 = 存档顶层、站点单元 =
`visits[i]`）有没有 **`reflection` 键**——INC46 的两条写路径都必写该键
（哪怕值是 `null`），INC46 之前从不写。正文里没有可机器判定的时代印记，
**不能靠读内容猜**。

为什么必须分开记：混批训练一批「有时能未卜先知、有时不能」的数据，
比全错更难发现——它在报表上表现为「分数略有下降」。要同口径出批，
`build --rationale-era pre_action` 只收新口径样本（其余逐条列进 skipped）。

### `degraded_from`

P1 里**没有**的键（老存档缺 `steps` / `user_prompt` / `run_config` / 全文正文…）。
非空即表示这条是**降级重建**的，**别当成与新存档同等的样本混着用**。

> 「键不存在」与「字段为空」必须分得开——两者在 `to_json` 之后都渲染成 `""`。

## `outcome`

指回 P1 的那一条。样本里**不复制 evidence 原文**，只留指针——
P1 是真值源，复制就产生第二份需要同步的副本。

`branch is None` 表示**成功**（P1 的 ledger 只记失败分支），
所以「无分支」不是「没结论」。

`p1_missing_fields` 逐条记「P1 里压根没有这个键」——
老存档缺字段时，那一条**不可判定**而不是「字段缺失」。

---

## 口径修正 · `six.tools` 写的是**全集**（2026-10-20）

本文档早期写「`tools` = 仅 `visible_to_model()` 的那些」。**那是错的**——
代码一直写的是**全集**（13 条，含 `new_tab` / `close_tab`），
文档与代码长期不符，而**两边都绿**——因为没有测试盯这一条。
⚠️ 这两个数是**默认面 `obscura-1` 的数**（下一段的 11 条同理）：
aria 面（camoufox / camofox）是 16 条全集 / 14 条可见。

为什么全集是对的：`new_tab` / `close_tab` 是真发生过的动作，
**删了 P2 就无法回放**（动作流里有它们，但工具清单里没有，
读样本的人会以为这两步是凭空冒出来的）。而**模型可见集是渲染期
才筛出来的子集**（11 条）——P3 与 T 的 openai 视图按 catalog 的
`visibility` 字段过滤，`new_tab` / `close_tab` 不进。

改这条时留了痕迹：契约测试
`tests/contract/test_tool_catalog_contract.py::Test三处一致` 现在盯
「落盘 P2 的工具集 == catalog 名字集」，且 `required` 口径由真实存档
统计重跑校验——口径写在文档里、写在代码里、由测试重算，三处对不上就红。