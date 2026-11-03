# P2 · 样本契约

> 交接面是**格式**，不是 Python 对象——所以这份文档描述的是
> `samples/<id>.json` 的字段，任何一侧改形状都应在此同步。
> 「读 P1 不 import `executor`」这条纪律由
> `tests/contract/test_p2_contract.py::TestNoExecutorImport` 用 AST 强制。

## 切条粒度

**一条 = 一条轨迹**（一个任务的一次运行）。⚠️ **2026-10-30 · 方案 23 改了粒度**：
以前是「**一条 = 一个决策单元**」（搜索阶段一条、每个站点一条），现在
**一份 P1 存档切出 0 或 1 条**——决策单元仍在 P1 里（`schema.py::_units_of`），
但它**不再是样本的粒度**，只作为**轨迹内的分段**与 `site_outcomes` 的来源。

- **0 条**：搜索阶段被反爬拦（`search_blocked` 非空）或搜索页采集失败 ⇒
  那一步压根没进入决策，切成样本只会教它对着验证码页编答案。
- **1 条**：其余情况。

⚠️ **动作流是摊平的**（搜索阶段 → 站点 1 → 站点 2 → …）：`step_index` 是
**轨迹内全局序号**，不是「每个单元各自从 0 数起」——后者会让第 5 步的
rationale 在字典查找里撞上搜索段的第 5 步，**静默丢思考**。

## 访问级剔除（方案 23 A10 / R4-2(b)）

任务库（`tasks.yaml`）里的 `excluded_platforms` 在 v1 是**答案级**约束
（不许把某平台当答案）；2026-11-02 起在 v2 是**访问级**约束——
**训练集里访问被排除平台的步必须是 0**。owner 裁定走 **(b) 数据层剔除**
（不是答案级 enforce，也不是采集层拦截）。

- **判据**：`assembler/excluded.py`。平台 → 域名表（`PLATFORM_DOMAINS`），
  按 hostname `==` 或 `endswith("." + domain)` 匹配——子串匹配会把仿冒域
  也剔掉，是一条静默的错。域名表与 `tasks.yaml` 的平台名一一对应。
- **证据是「步」**：模型动作的 `goto` 网址 + **逐步** `observation.url`。
  `origin="infrastructure"` 的 `new_tab` / `close_tab` 不算访问（那是执行器
  的会话管理，不是模型选的路径）；但跳转后的页面若真被看到，紧随其后的
  观察会把它记下来（观察 URL 仍参与判定）。
- **判定必须读 P1 原始 `steps[]`**，不能读 `Sample.observations`：搜索单元
  的后者只有一张汇总 `search_observation`（与动作不等长），拿它判会漏掉
  「搜索阶段点进被排除平台」那一类。
- **整条 unit 剔，不是只剔命中步**。只剔命中步会把一个 unit 截成前半段：
  前面的步还在为这个平台服务，outcome 却已经被扔掉——因果断裂。
- **编号不重排**：`site-3` 被剔后剩下的仍叫 `site-1` / `site-2` / `site-4`
  （`sample_id` 与 T 的 `unit_sample_ids` 共用这套编号，重排会让两边对不上）。
- **任务没声明 `excluded_platforms` 就不判**（不猜）。
- **P1 存档一个字不动**——剔的是训练样本，证据仍在存档里。

落点：`schema.split_archive_report` 返回 `SplitResult(samples, excluded)`
（`split_archive` 仍是老形态的 `list`）；`builder` 把剔除条数与明细记进
`BuildReport.total_excluded_units` / `ArchiveBuildResult.excluded_detail`。
⚠️ **`excluded` 是账不是失败**：剔了多少条要在 `cli build` 的输出与报告里
看得见，否则「这批样本怎么少了」只能靠人翻存档。

## 分池（方案 23 R3-3）

交付集**只含 `camofox-1`**；旧面（`obscura-1` / `camoufox-1`）按 O-3
进「只读存量」归档（**移动而非删除**）：

```
output/pipeline/samples/<sample_id>.json                      # 交付面
output/pipeline/legacy/<surface>/samples/<sample_id>.json     # 只读存量
output/pipeline/pool_meta.json                                # 批次账
```

分池是**写入时的动作**：`build_all` 先把已存在的非交付面样本挪进 legacy，
再按每条样本自己的 `tool_surface` 落盘。读取端不需要再按字段过滤。
`pool_meta.json` 记各面计数——**池变小不是质量变好**（风险 R-5），
分母变小时先看这一段。

## 顶层结构

```json
{
  "sample_id": "T001__b98b3e56",
  "task_id": "T001",
  "title": "功夫",
  "source_archive": "T001__b98b3e56.json",
  "tool_surface": "camofox-1",
  "six": { ...七件套... },
  "gate": { "status": "passed", "reasons": ["逐条核查 1/1 步通过"] },
  "rationale_missing": false,
  "rationale_era": "pre_action",
  "outcome": { ... },
  "site_outcomes": [ { ... } ],
  "reflection": { ... } | null,
  "degraded_from": []
}
```

`sample_id` = `<task_id>__<run_id>`，**排序即时间序**，
且从 id 就能反查源存档，不必开文件。
⚠️ **per-Run 之后它与 P1 存档名逐字相同**（`T001__b98b3e56.json` ⇒
`sample_id = "T001__b98b3e56"`）——T 视图的 `trajectory_id` 也是这个形式，
两边**终于**对齐了。

⚠️ **旧的 per-unit id（`<task>__<run>__<unit>`）仍然读得懂**：轨迹 id 取
**前两段** ⇒ 老样本不会报错。⚠️ **但那是历史口径**——「读到的是哪一代」
要靠 `unit` 键在不在来分（见下节 `unit` 的说明）。

| 顶层键 | 说明 |
|---|---|
| `sample_id` | `<task_id>__<run_id>`（与 P1 存档名逐字相同） |
| `task_id` / `title` | 切片轴。⚠️ **`unit` 键整个没了**——决策单元不再是样本的粒度；`unit` 这个键名**只出现在历史存档里**，仍读得懂（读出来是历史口径的单元名），但**新产出的样本不再有它** |
| `source_archive` | 据以生成的 P1 **文件名**（不含目录），批次可回溯 |
| `tool_surface` | **工具面**（2026-10-20 方案 15 §7 R1）：这份样本的动作空间属于哪个方言（`obscura-1` / `camoufox-1` / **`camofox-1`**）。来自源 P1 的 `driver` 字段，见下节。⚠️ **三个面 ≠ 三套方言**——方言只有两套词表 |
| `outcome` | **任务级结论**（裁定 ②：看最后一个结论）——见「`outcome`」一节 |
| `site_outcomes` | **轨迹内的站点级明细**（裁定 ③）——见「`site_outcomes`」一节 |
| `reflection` | **轨迹级终局反思**（取末个单元的那一份）——见「`reflection`」一节 |


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
| `observations` | `list` | ⑤ **训练态观察**（已裁，方案 23 R1；形状见下节）。P1 全文只在源存档里 |
| `rationales` | `list` | ⑥ 思考+结论，**与 `actions` 按 `step_index` 对齐**（见下） |

⚠️ **`reflection` 不在 `six` 里**——它是**顶层键**（`six` 只有上面这 7 个）。
早期版本的本文档把它列在「六件套」表里，而代码一直写在顶层 ⇒ 文档与代码
不符（两边都绿，因为没有测试盯这一条）。**2026-10-30 订正**：见下面的
「`reflection`」一节。

### ⑤ `observations`（训练态，方案 23 R1）

P2 落的是**训练态观察**——不是 P1 全文；全文仍只在源存档里。
每条观察的字段：

| 键 | 类型 | 说明 |
|---|---|---|
| `url` / `page_title` | `str` | 原样 |
| `read` | `str` | **这条观察是「谁要的」**（方案 23 R4-1）：`""` = 执行器推的整页回执；`snapshot` / `interactive` / `links` = 模型主动读的那一块。非空时**只有被读的块有内容**，其余字段为空/0 表示「这次没读」，不是「页面上没有」 |
| `body_text` | `str` | **任务相关正文摘要**，≤ 320 字（读步 ≤ 2000，见下）；按原行序保留，不是重排后的摘要 |
| `body_source` | `str` | `body_text` / `body_preview_only`（老存档只有前 400 字符）/ `missing` |
| `body_chars` | `int` | 摘要实际字数 |
| `truncated` | `{source, view}` | `source` = P1 的 dom 层预算用尽；`view` = 本层裁剪触顶（正文/元素/链接任一） |
| `degraded` | `list[str]` | **采集降级**（这块信息没采到），如 `observation_missing` / links_empty |
| `interactive_elements` | `list` | ≤ 10 条（读步 ≤ 50），去噪去重；键名跟方言走（`{tag,label}` / `{role,name}`），**不带 `ref`** |
| `links` | `list` | ≤ 4 条（读步 ≤ 50），去不可导航项与噪声；`{text, href}` |
| `video_tag_count` / `iframe_count` | `int` | 原样 |
| `action_error` | `str` | **本步动作失败原因**（P1 `steps[i].error`），空串 = 没报错 |
| `limits` | `object` | 本层**全部**裁剪事实，见下 |

`limits` 的六个计数 + 一个布尔（`None` = 未裁，与「裁了 0 条」区分开）：

- `body_chars` / `elements` / `links`：**上限触发**时记下限（普通步 320 / 10 / 4，
  读步 2000 / 50 / 50——见 `observation_view.VIEW_*` 与 `VIEW_READ_*`）；
- `body_lines_dropped` / `elements_dropped` / `links_dropped`：**噪声与去重**丢掉的条数；
- `cropped`：任一项非空即为真。

**读步的窗口为什么更宽**：读原语的存在意义就是「整页回执被压缩后看不见
的东西，模型要能主动去拉」。还按 320/10/4 压，`snapshot` 读回来的正文与
普通回执没有差别（`snapshot` 与普通回执就只剩一个 `read` 标记之差），
读这一步等于白走。

两条硬口径：

1. **下一步动作的目标无条件保留**（`render(..., keep=...)`）。限量若把点击名 /
   导航网址裁掉，训练样本里就会出现「点了一个观察里没有的东西」——那是在
   教模型编目标。`keep` 命中项即使属于导航词、即使排在第 30 位，也进观察。
2. **`action_error` 与 `degraded` 分列**。「动作白做」和「信息没采到」是
   两件事；混成一个标记，模型会把点击失败学成「页面没内容」。

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
| `passed` | 逐条核查过，无 hard 发现，**且终局答复兑现了输出契约** |
| `failed` | 有 hard 发现（`reasons` **定位到第几步**），**或终局答复没兑现契约** |
| `not_run` | **没判**——不是「通过」。两种成因：①没有可核查的 rationale；②rationale 是**旧口径**（见下节 `rationale_era`），新闸门的可见窗口对它不适用 |

**终局验收读最终回复**（方案 23 R2-3，owner 裁定 O-2）：任务库
`output_contract.min_urls` 要求 N 个网址，而这条样本的收尾答复
（P3/T 渲染的末条 assistant，同一个函数产出）里**可交付网址少于 N 个**
⇒ 不许判 `passed`，降为 `failed`，理由写
「终局答复未兑现输出契约：合同要求至少 N 个网址，答复里只有 M 个」。

- 判据读**文本**，不读 `outcome` 字段：字段里有网址 ≠ 答复里说了；
- 只作用于**站点单元**（`outcome is not None`）且有可见模型动作的样本
  ——搜索单元的收尾是「接下来核验候选」，不是任务答复；
- 只降 `passed`：`failed` 已是失败，`not_run` 是「没判」，都不被改写；
- 任务库读不到 / 未知 `task_id` / 合同没写 `min_urls` ⇒ 判据**不适用**，
  判定原样（fail-closed：不适用 ≠ 判通过）。

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

⚠️ **per-Run 之后它是**轨迹级**的集合**（整条轨迹的缺件），不再是「某个单元
的缺件」——缺件在整条轨迹上汇总一次。

> 「键不存在」与「字段为空」必须分得开——两者在 `to_json` 之后都渲染成 `""`。

## `outcome`（**任务级**结论）

⚠️ **2026-10-30 · 方案 23 裁定 ②：看最后一个结论。** 它是**整条轨迹**的
结论（末条 outcome），**不是**某一个站点的——逐站点的结论在
`site_outcomes` 里。

指回 P1 的那一条。样本里**不复制 evidence 原文**，只留指针——
P1 是真值源，复制就产生第二份需要同步的副本。

`branch is None` 表示**成功**（P1 的 ledger 只记失败分支），
所以「无分支」不是「没结论」。

`p1_missing_fields` 逐条记「P1 里压根没有这个键」——
老存档缺字段时，那一条**不可判定**而不是「字段缺失」。

⚠️ **旧口径是「任一 outcome 成功 ⇒ 整条成功」**——那会把「中途成功过、
后来又失败」的轨迹报成成功，而那正是训练侧要学的东西。**现在统一看末条。**

## `site_outcomes`（**轨迹内的站点级明细**）

裁定 ③：**站点级结论不再被切成独立的样本，但也不能丢**——它回答
「这条轨迹里到底在哪一步走不通」。**训练侧只消费 `outcome`（任务级标签）**，
这一层是**给人看的**。

每条一个访问点（`schema.py::SiteOutcomeView`）：

```json
{"index": 1, "url": "https://x.test", "landed_url": "https://x.test",
 "outcome": {"url": "https://x.test", "branch": "no_play_control"},
 "step_start": 3, "step_end": 9, "outcome_index": 4}
```

| 元素字段 | 含义 |
|---|---|
| `index` | 访问序号（从 1 起，与 P1 的 `visits[i]` 同序） |
| `url` / `landed_url` | 候选链接 / 落地后的地址（两者可能不同） |
| `outcome` | **这个访问点自己的**结论。`null` = **没配到**（缺件），**不是**成功——成功是 `outcome.branch` 为空 |
| `step_start` / `step_end` | 这个访问点占**全局步号**的区间 `[起点, 终点)`——空区间 = 该站点没有动作步 |
| `outcome_index` | 配到的结论在 P1 `outcomes[]` 里的下标（`-1` = 没配到） |

⚠️ **为什么要带步号区间**：站点级结论回到轨迹里之后，「这个站点是轨迹里
哪一段」必须能直接回答——靠 url 反推是**猜**（两个站点跳到同一个地址时会
撞，而撞了报告上看不出异常）。区间是**共有事实**，不是推出来的。

⚠️ **`outcome_index` 是候选集站点行与访问点之间的唯一对齐身份**：候选行按
**url** 建键、访问点按**访问顺序**枚举，两边光靠 url 会撞。

⚠️ **`index` 是访问序号（从 1 起，恒 ≥ 1，没有 0）**——它与**候选集站点行**里
那个**同义**字段**不能互相顶替**：候选行那个多带一个「`0` = 孤儿记账」的语义
（那条 outcome 没被任何访问点配到，压根没有访问序号）。⇒ 两个键名的对照、
以及「别顺手改名」的坑，见[避坑指南 §8.48](../02-避坑指南.md)。

## `reflection`（**轨迹级终局反思**）

形状同 `WriteResult.to_json`（`status` / `rationale` / `reason` / `degraded`），
于是与 `steps[i].rationale` 同构。`null` = 没生成（writer 未接 / 后端不可用 /
生成失败）——与「生成了但内容为空」不同。

⚠️ **P1 里没有轨迹级反思**——`reflection` 是 **unit 级**的（搜索阶段在存档
顶层、站点单元在 `visits[i]` 里）。这里取**末个单元**的那一份（裁定 ②
「看最后的结论」同款）：它说的是模型对**最后核验的那个站点**的判断，
而那正是这条轨迹的落点。搜索阶段的反思说的是「找到了哪些候选」，不是结论。

取不到（有反思的单元一个都没有）→ `None`。「没有反思」与「有反思但内容是
空的」在下游是两回事，合成一个空 dict 就把前者记成了后者。

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

---

## 口径修正 · 粒度从「一个决策单元」改成「一条轨迹」（2026-10-30 · 方案 23）

本文档早期写「**一条 = 一个决策单元**：搜索阶段一条，每个站点一条」。
**那是旧口径**——现在**一份 P1 存档切出 0 或 1 条**，一个任务一条轨迹。

连带变的三处（**都不许靠旧 id 猜**）：

| | 旧 | 新 |
|---|---|---|
| `sample_id` | `<task_id>__<run_id>__<unit>` | `<task_id>__<run_id>`（**与 P1 存档名逐字相同**） |
| `unit` 键 | `"search"` / `"site-N"` | **整个没了**（决策单元退成轨迹内的分段） |
| `outcome` | 某一个单元的结论 | **任务级**结论（裁定 ②：末条结论） |

⚠️ **旧样本仍然读得懂**：轨迹 id 取 `sample_id` 的**前两段** ⇒ 老数据不会
报错、也不会被拒。**这正是危险处**——读数不报错，但口径是历史口径。
⇒ **分辨办法**：新样本**没有 `unit` 键**；且 `site_outcomes` 非空时是
per-Run 形状。

⚠️ **别把两条轴混了**：**分档 / 测量仍然是站点级**（冻结档文件与 D-10 的
分母**一个字节都不改**）——per-Run 改的是**交付粒度**，不是测量单元。
设计见[设计方案 23](../设计方案/23-每任务一轨迹.md)。

改这条时留了痕迹：契约测试
`tests/contract/test_p2_contract.py`（`sample_id` 形态 · `unit` 键的消失 ·
`site_outcomes` 的形状）与 `tests/unit/test_assembler.py` 盯住。