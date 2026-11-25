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

### `intermediates[]` 不进样本（P0-5 · 2026-11-25 · **契约，不是遗漏**）

P1 的 B/C 中间页（聚合站页 / 文章页，`intermediates[]`）**整段不进 P2**，
因而也不进 P3/T。它缺的是：

- `intermediates[i].steps` **不占 `step_index`**，样本里没有这几步；
- 中间页的 outcome（`not_aggregator` / `aggregator_no_entries` / `article_no_links`）
  只在 P1 `outcomes[]` 里，**不进 `site_outcomes`**。

⚠️ **它与 `orchestrator._rationale_history` 不一致，而这个不一致是有意保留的**：
写后续站点 rationale 时，agent **看得见**中间页的步（最近 40 步窗口含
`intermediates[].steps`），而样本里没有那几步。⇒ **两边必须一起改或都不改**：
只删 rationale 的窗口，agent 在 B/C 上会对着空历史写动机（方案 23 §1.3 反对的
正是这个）；只加 P2 单元，要同时动 P2 切条 / P3 / T 渲染。

⚠️ **实测规模是 0**（2026-11-25 扫 841 份存档）：带 `intermediates` 的只有 2 份
（同一份的重复拷贝）且都是 **scripted** 模式——中间页的步只有 `goto`，没有 agent
动作；「agent 模式 + B/C 路径」的真实存档 **0 份**。⇒ 这是**潜在**损失，不是已发生
的损失。**接入的前置条件是补一批 agent + B/C 的真实存档**，先量清楚丢掉的信息有多重
（`research/probes/p05_intermediate_scale.py` / `p05_dump_intermediate.py`）。

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
| `observations` | `list` | ⑤ **训练态观察**（`capped` 模式 = 规则裁剪；`semantic` 模式 = **模型选**，方案 26；形状见下节）。P1 全文只在源存档里 |
| `rationales` | `list` | ⑥ 思考+结论，**与 `actions` 按 `step_index` 对齐**（见下） |

⚠️ **`reflection` 不在 `six` 里**——它是**顶层键**（`six` 只有上面这 7 个）。
早期版本的本文档把它列在「六件套」表里，而代码一直写在顶层 ⇒ 文档与代码
不符（两边都绿，因为没有测试盯这一条）。**2026-10-30 订正**：见下面的
「`reflection`」一节。

### ⑤ `observations`（训练态，方案 23 R1）

P2 落的是**训练态观察**——不是 P1 全文；全文仍只在源存档里。

⚠️ **观察必须由装配层渲染后再落盘**（H-41 · 方案 26 §6.3 #10）：
`assembler.schema.Sample.to_json()` 现在**默认不写观察**
（`observations=None` ⇒ `six.observations = []`），必须**显式传**
`observations=`。P2 落盘的**唯一**路径是
`assembler.builder.build_from_archive`（它先渲染再传）。

为什么：**裁剪**（`capped`）与**绕过**（直出 P1 全文）在产物上**长得
一模一样**——都是「正文比 320 长」——但根因完全相反：一个是**规则层裁
的**（有 `limits` 留痕），一个是**压根没裁**（无留痕）。排查时无从下手。

每条观察的字段：

| 键 | 类型 | 说明 |
|---|---|---|
| `url` / `page_title` | `str` | 原样 |
| `read` | `str` | **这条观察是「谁要的」**（方案 23 R4-1）：`""` = 执行器推的整页回执；`snapshot` / `interactive` / `links` = 模型主动读的那一块。非空时**只有被读的块有内容**，其余字段为空/0 表示「这次没读」，不是「页面上没有」 |
| `body_text` | `str` | **训练态正文**。`capped` 模式：≤ 320 字（读步 ≤ 2000，见下）；`semantic` 模式：**没有字符硬顶**（模型选中的行**逐字重建**，一个字都不截）。两种模式都按原行序保留，不是重排后的摘要 |
| `body_source` | `str` | `body_text` / `body_preview_only`（老存档只有前 400 字符）/ `missing` |
| `body_chars` | `int` | 正文**实际**字数 |
| `body_refined` | `bool` | 这条正文是**模型选行**裁出来的（方案 25 / 26），不是「页面前 N 字」。为 false 时 = 走的规则裁剪 |
| `body_refined_lines` | `int` | 精炼后正文里**留下的行数**（未精炼时为 0） |
| `body_refined_dropped` | `int` | 候选行里**没进最终正文**的行数（模型丢的 + 硬预算裁的）。与 `limits.body_lines_dropped` 是**两笔不同的账**，不混算 |
| `elements_refined` | `bool` | 交互元素是**模型选**出来的（仅 `semantic` 模式为 true） |
| `elements_refined_count` | `int` | 模型选中的元素**条数** |
| `links_refined` | `bool` | 链接是**模型选**出来的（仅 `semantic` 模式为 true） |
| `links_refined_count` | `int` | 模型选中的链接**条数** |
| `view_mode` | `str` | **渲染模式**：`capped`（规则裁剪，默认）/ `semantic`（模型选，**不截**）。⚠️ 两种模式的 `limits` 口径**不完全一样** ⇒ **两种模式的批次不可混在一张表里比** |
| `semantic_fallback` | `bool` | `semantic` 模式**没走通**（无旁挂 / 旧协议 / 对不上原文）⇒ 整条**回退规则裁剪**。这条标记独立于 `body_refined`——读步（正文不渲染）也会「没有正文精炼」，但那不是回退 |
| `truncated` | `{source, view}` | **两支各有各的意思**（H-41 · 方案 26 §6.3 #9）：`source` = **上游采集侧**截断（P1 的 dom 层预算用尽）；`view` = **本层训练态渲染**裁剪。⚠️ `semantic` 模式下 `view` **恒 false**（本层一个字都没截）；**回退时强制 true**（诚实记账：那条其实是规则裁剪的） |
| `degraded` | `list[str]` | **采集降级**（这块信息没采到），如 `observation_missing` / links_empty |
| `interactive_elements` | `list` | `capped` 模式：≤ 10 条（读步 ≤ 50），去噪去重；`semantic` 模式：**没有条数硬顶**（模型选中的**逐字重建**）。键名跟方言走（`{tag,label}` / `{role,name}`），**不带 `ref`** |
| `links` | `list` | `capped` 模式：≤ 4 条（读步 ≤ 50），去不可导航项与噪声；`semantic` 模式：**没有条数硬顶**。`{text, href}` |
| `video_tag_count` / `iframe_count` | `int` | 原样 |
| `action_error` | `str` | **本步动作失败原因**（P1 `steps[i].error`），空串 = 没报错 |
| `limits` | `object` | 本层**全部**裁剪事实，见下 |

`limits` 的六个计数 + 一个布尔（`None` = 未裁，与「裁了 0 条」区分开）：

- `body_chars`：**正文的实际长度**（H-41 口径变更）——本层真动了正文时
  才记。此前记的是**上限常量** ⇒ **账实不符**（keep 行无条件全量入选，
  它们本身超上限时正文会比上限长：实测正文 900 字、账上写着 320）。
  「动没动过」由 `cropped` 与 `truncated.view` 表达——**长度**归 `limits`，
  **信号**归 `truncated`；
- `elements` / `links`：**上限触发**时记下限（普通步 10 / 4，读步 50 / 50——
  见 `observation_view.VIEW_*` 与 `VIEW_READ_*`）；
- `body_lines_dropped` / `elements_dropped` / `links_dropped`：**噪声与去重**丢掉的条数；
- `cropped`：任一项非空即为真。

⚠️ **`body_chars` 的口径变了**（上限值 → 实际长度）⇒ **新旧批次不可混在
一张表里比**（方案 26 §6.5）。

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

#### 精炼分支（方案 25）：换一种裁法，不是少裁一点

`limits` 与 `truncated` **不动**（7 键 / 2 键是硬契约，见下）；精炼的事实
**另立三个字段**（`body_refined` / `body_refined_lines` /
`body_refined_dropped`）。

正文有两条来路：

| 来路 | 触发 | `body_refined` |
|---|---|---|
| **规则裁剪** | 默认（没给旁挂 / 旁挂坏 / 旁挂里没这条） | false |
| **模型选行** | 旁挂里能按**键**找到这条 ⇒ **`capped` 按正文哈希**（老协议只选正文）/ **`semantic` 按候选集哈希**（正文 + 元素候选 + 链接候选） | true |

⚠️ **`semantic` 模式的键是「候选集哈希」不是「正文哈希」**（H-41 阶段 3）：
三段协议选的是**正文 + 元素 + 链接** ⇒ 键必须覆盖那三样。**同一份正文**在
一条轨迹里可能被看到多次，而那几次的元素 / 链接集合可以不同（动态页面、
镜像站）⇒ 只按正文定位 ⇒ **A 步的选择被贴到 B 步上** ⇒ 渲染层逐字核对不过
⇒ **整条回退**（实测 Tier B 第一次：13 条回退里 **12 条**是这一条 ⇒ 达标率
87.7% < 95%）⇒ 键改为**候选集哈希**（旁挂升版 `refine.v4`，旧档读侧**显式
作废**）。

模型选行这一路的四条口径：

1. **只可能漏，不可能造**。模型只报**行号**，最终文本由
   `observation_view.render` **从 P1 原文行重建** ⇒ 模型加不了一个原文
   没有的词。这不是提示词约束出来的，是**结构**出来的。
2. **对不上就回退**。保留的行不在原文里（旁挂与正文不同源）⇒ **整条**
   作废，走规则裁剪（与没精炼时**逐字一致**，零回归）。不「差不多地」
   用一半。
3. **保真不放宽**。「下一步动作目标无条件保留」在精炼分支里同样成立——
   渲染层会**自己把目标行补回来**。
4. ~~**预算仍归规则层**。模型只决定「哪些行相关」，多少字放得下仍由
   `observation_view` 的硬顶决定（普通步 320 / 读步 2000）。~~
   ⚠️ **这条已被方案 26 撤回**（H-41 阶段 2）：硬顶本身就是**被禁的那种
   截断**。语义模式（`semantic`）下**没有字符硬顶**；`capped` 模式仍
   有——它是语义模式的**回退路径**（回退时输出与没精炼时**逐字一致**）。

⚠️ **`truncated` 仍是 2 键，但两支的语义在 H-41 被重新划清**
（方案 26 §6.3 #9）：`source` = **上游采集侧**截断；`view` = **本层
训练态渲染**裁剪。精炼是「换了一种裁法」，不是「裁了多少」，所以
**不进 `truncated`**——加第三个键会破掉 `rationale/gates.py` 里
`_truncated_flag` 的判读。

⚠️ **`limits` 里也不进精炼的账**：`limits.body_lines_dropped` 记的是
**规则剔掉的**（导航 / 页脚 / 重复），`body_refined_dropped` 记的是
**模型丢掉的**（不相关）。两笔账混算就看不出「裁掉的是噪声还是内容」，
而这两者的后果完全不同（前者无害，后者要人复核）。

#### 语义渲染分支（方案 26）：**不截**的那一条路

`view_mode` = `semantic` 时，正文 / 交互元素 / 链接**三样都由模型选**，
渲染层按模型的选择**逐字重建**（`common/view_candidates.py` 是候选集的
**唯一真源**——精炼侧与渲染侧调**同一个函数**，否则「第 7 号」不是同
一个东西）。

| 口径 | `capped` | `semantic` |
|---|---|---|
| 留哪些 | **规则层**（相关度 + 硬顶） | **模型**（只报编号） |
| 有没有硬顶 | 有（320 / 10 / 4） | **没有** |
| `truncated.view` | 本层真裁了才为真 | **恒 false** |
| 元素 / 链接 | 按相关度取前 N 条 | **模型选**，`elements_refined` / `links_refined` 留痕 |

⚠️ **语义渲染是「全有或全无」**：正文 / 元素 / 链接**三段**里任一段
对不上原文 ⇒ **整条**回退 `capped`。

⚠️ **回退时整条回退「纯规则裁剪」**（H-41 阶段 2）：老协议（`select.v1`）
只选了正文，元素 / 链接**压根没被选过** ⇒ 拿它渲语义模式会造出
「正文是模型选的、元素是规则选的」那种**混口径**观察 ⇒ 不可用时**整条**
回退，输出与**没精炼时逐字一致**（方案 26 §0 A3）。

⚠️ **`truncated.view` 在回退时强制为 true**（诚实记账）：那一条其实是
**规则裁剪**出来的，不能让人以为它也是模型挑的。

⚠️ **语义模式下 `limits.body_lines_dropped` 恒为 `None`**——噪声行**也
摆给模型了**（`filter_noise=False`），噪声由**模型**推翻，规则层没剔
任何行。

设计：[方案 25 §4](../../设计方案/25-训练语料正文的LLM语义精炼.md)、
[方案 26 §6](../../设计方案/26-禁止内容截断与LLM语义保留改造方案.md)。

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

## 缺件负样本线（fail-closed 第三类信号 · 方案 26 §6.4 · H-41 阶段 3）

⚠️ **这一节的样本不在主池里**。主池是 `samples/`，这条线落在
`failclosed_samples/`（**与 `samples/` 平级、不同名**）——里面的观察是
**故意缺件**造出来的，混进主池就是往训练集里塞假事实。

**为什么要造**：语义渲染把 `truncated.view` 变成**恒假**（本层一个字都
没截，正文是**模型自己挑过**的行）⇒ `[内容不完整: 本次裁剪]` 这个标记
在语义模式下不再出现。而那个标记**有功能**——它是教模型「看到缺件就说
判不了」的载体 ⇒ 标记没了 ⇒ 模型被教成「永远敢下结论」。
方案 26 §6.4 给了三条迁移去处：采集降级（`[采集降级: …]`）、
`observation_missing` 占位、**故意给不完整观察的负样本线**——前两类真实
数据里本来就有，第三类**得造**。

**造出来的东西**（与主池样本**同构**，同一套渲染、同一套收尾话术）：

| 差别 | 做法 |
|---|---|
| **缺件那一步的观察不完整** | 三种方式：`body_truncated`（正文被上游砍掉尾部）/ `elements_missing`（交互元素表没采到）/ `body_missing`（正文没采到） |
| **缺件标记必须是真的** | 回执里**如实**出现 `[内容不完整: 上游截断]` 或 `[采集降级: …]`——标记是假的 ⇒ 这条线教的是「标记可以乱写」 |
| **轨迹在缺件那一步截断** | 后面的动作与观察都拿掉——fail-closed 的 agent 是「看到缺件就停并如实说」 |
| **结论改判成 fail-closed** | `outcome.branch` = `unresolved` ⇒ 期望答案由它**派生**（不手写），于是那句「无法确认能否播放，不予记录」与主池里的 fail-closed 样本**逐字同款** |
| **别的站点保留真实结论** | 只有**缺件那一步对应的站点**改判；别的站点确实被访问过、确实被排除过，一并改成「判不了」＝**改写真实事实** |

**顶层键**（比主池多两个，其余同构）：

| 键 | 类型 | 说明 |
|---|---|---|
| `failclosed_cut_mode` | `str` | **缺件方式**：`body_truncated` / `elements_missing` / `body_missing` |
| `failclosed_cut_step` | `int` | **缺件发生在第几步**（轨迹内全局步号，与 `actions` / `observations` 对齐） |

**留痕**：「这条是造出来的」记在**既有键** `degraded_from` 里（追加
`failclosed_line`）——读样本的人本来就会看它 ⇒ 不新开一个顶层标记键。
⚠️ **`sample_id` 与原样本不同**（`<原 id>__fc-<缺件方式>`）——同 id 会
**覆盖**主池里那条真实样本，而文件系统的行为是**静默覆盖**：读的人只会
看到少了一条。

⚠️ **`degraded_from` 里出现 `failclosed_line` 的样本不得进主池训练**：
它们的观察是**人为阉割过的**，混着训练就是在教模型「这个站点没有播放
控件」。训练方要**显式挑** `failclosed_samples/` 才算用到它。

⚠️ **期望答案不是手写的**：它由 `outcome.branch = "unresolved"` 经
`assembler.closing.closing_for_run` **派生**。手写一份话术 ⇒ 两份话术
会漂 ⇒ 训练语料里出现「两种说法」⇒ 模型学到「看情况换说法」。

落点与造法：`assembler/failclosed.py`（**唯一真源**）、
`assembler/builder.py::build_failclosed_from_archive` /
`build_failclosed_all`、命令行 `cli build --stage failclosed`。
契约测试：`tests/unit/test_failclosed_line.py`。

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