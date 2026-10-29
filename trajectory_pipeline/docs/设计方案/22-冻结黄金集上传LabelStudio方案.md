# 22-冻结黄金集上传 Label Studio 方案

> 状态：**owner 已裁定（2026-10-29）· R0 已实施（离线部分全部就绪）**。
> 本文是「黄金集 → Label Studio」这条链路的唯一设计真源：**事实基线**（量出来的）、
> **任务数据模型**、**每版一项目的项目/幂等口径**、**落点文件**、**风险**、**路线**。
>
> **owner 裁定（2026-10-29，原文口径）**：
> ① **存量只上传 v14**（v1..v13 不上 LS）；**以后增量版本每个都上传**。
> ② **一致行也传**。
> ③ **holdout 也传**。
> ④ **LS 端 annotator 身份单列一栏**（`manual-review` 是 owner 署的名，两套署名不合并）。
>
> 配套阅读：[01-架构总览](../01-架构总览.md)（模块地图）·
> [07-人工复核与判读手册](../07-人工复核与判读手册.md)（现在有几件事要人判）·
> [08-常用命令速查](../08-常用命令速查.md)（命令清单的唯一真源）·
> [在线视频场景rubric](在线视频场景rubric.md)（尺子本体）

---

## 0. 速览

### 0.1 一句话

**把冻结的黄金集连同「评估内容」推到 Label Studio，每个冻结版本一个独立项目**——
人在标注页上**同时看得见**代码判定、金标、维度分数、现场读数与尺子，给出裁决；
裁决落成可复核的记录。

**存量只推 v14 一个版本；此后每次冻结出新版本（v15、v16…）都推一个项目。**

### 0.2 为什么现在做（三个缺口）

| # | 缺口 | 证据 |
|---|---|---|
| **G1** | **七成的金标从没被人看过一眼。** v14 的 294 条金标里，**只有 82 条（28%）是人署的名**（`manual-review` 66 + `reesn` 16），另 **212 条（72%）是自动化写的**（`auto-validator-l3l4-v1-no-text-signal` 153 + `flagged-mismatch` 30 + `ai-assisted-inc33` 18 + `weak-confirmed` 9 + `confirmed` 2）。v3 时还是 100% 人署名，**v7 起大批换成机器直签** | `golden/data/v14/*.jsonl` 的 `golden.reviewed_by` 分布（§1.3） |
| **G2** | **「现场」只在分歧卡里。** `calibration.worklist` 的现场读数只覆盖分歧卡（v14 共 **32 张**：d10 23 + d5 9，涉及 **28 个 `golden_id`**）——**294 行里有 266 行没有任何人看过的现场记录**；其中最扎眼的是 **179 条一致行**：两侧都判成同一个标签，**而那个标签是自动化写的**。金标只在 jsonl 里躺着 | `output/pipeline/calibration/v14/disagreements.jsonl` 行数 + 「自动化署名 ∩ 严格一致」计数（§1.2 的量法） |
| **G3** | **版本谱系在 LS 端不可见。** 现有 LS 项目只有一个（id=77「trajectory-review」，装的是 scout 轨迹）。「v13 改了 26 条代码判定」「v10 改了 14 条金标」这些事实，人只能去 diff jsonl 才发现 | §2 版本差异矩阵 |

### 0.3 三条不做（划边界，越界即架构回退）

1. **不回流金标。** LS 标注**不自动写** `annotations.jsonl`。金标变更是**独立一步**
   （走 `golden.builder annotate` 整行模式，要 `access_surface` + `observed_at` + 署名），
   且「金标变动 = 新基线」⇒ 要重冻结出新版本（v14→v15）。LS 的产出定位是
   **人工复核记录**（落 output 区，不入真源）。
2. **不动真源区。** `golden/data/` 一个字不改；上传**只读**。
3. **不 import 存量。** 新代码全在 **`trajectory_pipeline/evaluation/golden_ls/`**（纪律 2）。

   ⚠️ **落点不是 `ls_bridge/`**（初稿写的是那儿，实施时改了，理由是硬的）：
   `tests/contract/test_ls_bridge_contract.py` 把 `ls_bridge` 的依赖锁死在
   `{ls_bridge, scout.aggregate, scout.types, storage, storage.read_p3,
   storage.task_views}` —— **不含** `executor` / `evaluation.golden.*` /
   `evaluation.calibration.*`。而黄金集这一路**必须**读这三样：分支表
   （22 类封闭集）、冻结档与等价类、现场读数。塞进去就得撑开那道白名单，
   scout 轨迹那一路的依赖边界就松了。

   ⇒ 另立 `evaluation/golden_ls/` 包，依赖方向**单向**
   （`golden_ls → ls_bridge` / `golden` / `calibration` / `executor.branches`），
   **`ls_bridge` 那边一个字都不动**（只有 `client.py` 加了两个方法：
   `create_project` 的 `description` 参数、`export_project_json`）。

### 0.4 一条范围裁剪要写在明处（owner 裁定的直接后果）

**存量只推 v14 ⇒ v1..v13 当时的判定（那些与 v14 不同的金标与代码判定）永久不复核。**
v14 全量上传能覆盖的是**「这些 URL 今天该怎么判」**，覆盖不了**「当年那一版的判法对不对」**。

⇒ 这是**已知不覆盖项**，不是「不需要」。写在这里是为了以后没人误读成
「历史版本都复核过了」。

---

## 1. 事实基线

> 全部是本机量出来的，量法写在每行后面。**重跑这些命令即可复核**，不必信本文。

### 1.1 端与档

| 事实 | 值 | 量法 |
|---|---|---|
| LS 地址 | `http://127.0.0.1:8099`，`/health` = UP | `ls_bridge status` |
| 凭据 | `TRAJECTORY_LS_API_KEY` 已设（**只走环境变量**） | `ls_bridge status` |
| 现有项目 | id=77「trajectory-review」（scout 轨迹复核，**不是**本方案的项目） | `ls_bridge status` |
| 冻结档谱系 | v1..v14（`versions.jsonl` 14 行） | `golden/data/versions.jsonl` |
| 每版行数 | v1/v2 = 277（calibration 180 + holdout 97，**无 regression**）；v3–v6 = 294（191+103）；v7–v14 = 294（**131 + 60 + 103**） | `golden/data/v{N}/` 三档行数 |
| 行内 `golden_id` 唯一性 | 每版 294 条**全唯一**（v1 277 条全唯一） | 逐版去重计数 |
| 三档档名真源 | `golden/splits.py` 的 `SPLITS` / `SPLIT_FILES`（**不手抄档名字面量**） | — |

### 1.2 两侧一致率（`golden.label` vs `code.branch`）

| 版本 | 一致 | 不一致 | 一致率 |
|---|---|---|---|
| v1 | 206 | 71 | 74.4% |
| v3 | 189 | 105 | 64.3% |
| v7 | 189 | 105 | 64.3% |
| **v14** | **201** | **93** | **68.4%** |

⇒ **v14 有 93 条分歧行、201 条一致行**。**裁定②：两者都传。**
一致行不是「不用看」——G1 里那 212 条自动化署名的金标**大量落在一致行里**
（两侧都判成同一个标签，但那个标签可能是机器写的、没被人看过一眼）。

⚠️ **这张表的 68.4% 是「逐字相等」的口径**（`golden.label == code.branch`，
分母是**全部 294 行**）。**门禁用的是另一个口径**——D-10 口径 C（语义等价类，
分母是站点级 289 行）读数是 **230/289 = 79.6%**。两个数**都对**，问的不是
同一件事（口径 C 把 `blocked` 与 `login_wall_or_blocked` 这类语义相同、
字面不同的也算一致）。**推之前两个都报**，各标各的分母（见 §7 的报告口径）。

### 1.3 金标署名（v14，**本方案最有力的论据**）

| 署名 | 条数 | 性质 |
|---|---|---|
| `auto-validator-l3l4-v1-no-text-signal` | 153 | 机器直签（无文本信号） |
| `manual-review` | 66 | **owner 署的名**（人核过） |
| `auto-validator-l3l4-v1-flagged-mismatch` | 30 | 机器直签（标记不一致） |
| `ai-assisted-inc33` | 18 | AI 辅助 |
| `reesn` | 16 | 人署名 |
| `auto-validator-l3l4-v1-weak-confirmed` | 9 | 机器直签（弱确认） |
| `auto-validator-l3l4-v1-confirmed` | 2 | 机器直签（确认） |

**人核 = 82 / 294（28%）；自动化 = 212 / 294（72%）。**

⚠️ **「人核」的口径是仓库自己的封条集合**，不是「看着像人写的就算」：
`evaluation/golden/signatures.py` 的 `HUMAN_SIGNATURES = {"manual-review", "reesn"}`
——与 `tests/unit/test_inc37_validator_pass.py` 的封条集合**逐字一致**
（契约测试钉着，抄两份必然漂）。

* **`ai-assisted-inc33` 不算人核**——AI 辅助写的，没人逐条看过；
* **不在集合里 ⇒ 算自动化**（fail-closed：新冒出来的署名先当「没人看过」）；
* ⚠️ 初稿这里写的是「人署名 100 / 机器署名 194」，**是把 `ai-assisted-inc33`
  的 18 条算成人核了**——与仓库封条口径不符，已按封条集合改正。

v3 是 100% 人署名（`ai-assisted-unconfirmed` / `manual-review` / `ai-assisted-inc33` 三类），
**v7 起大批换成机器直签**——这与 §2 里 v7 的「切分 + 尺子双变」是同一批改造的副作用。

⇒ LS 标注页**必须把 `reviewed_by` 摆在最显眼处**（§3.2 区块①）：人看到
「这条金标是机器写的」和「这条是 owner 核过的」，判读投入该不一样。

### 1.4 现场与轨迹的可得率

| 资源 | v14 可得率 | 说明 |
|---|---|---|
| **现场读数**（每步动作 + 观察读数 + 正文预览） | **216 / 294 = 73%** | 走 `calibration.evidence.locate_sample`（按 `outcome.url` 匹配站点样本）。读不到的 78 条 = **72 条「存档里没有匹配该 url 的站点样本」**（多半是站点漂移 / 多 unit）+ **6 条 `negative_pool`（本来就没有可切的存档）** |
| **T 视图**（per-Run 完整任务视图） | **288 / 294 = 98%** | 按 `{task_id}__{run_id}` 配对，主树 `output/pipeline/task_views/` 有 145 份 |

> ⚠️ **现场只有 73%**——所以「带评估内容」的准确说法是：**现场**（73% 有，读不到要把
> 「读不到 + 为什么 + 候选」显式写进去）与**轨迹**（98% 有）**各自独立可得**，
> 不是「都有」。**空读数会被读成「页面里没有 video 标签」**（`evidence.py` 模块
> docstring 的原话）——这是本方案最容易犯、也最贵的错。

### 1.5 逐版字段集不同（导出必须版本容错）

| 版本 | 顶层键 |
|---|---|
| v1 / v2 | `code` `golden` `golden_id` `persona` `play_page_url` `retest` `run_id` `sample_id` `site_url` `source_archive` `source_kind` `task_id` `url` |
| v3–v6 | 同上 + `code._inc33_fix` |
| v7–v13 | 同上 + **顶层 `dimension_scores` / `scores_detail` / `status`** |
| v14 | 同上 + **`golden_id_legacy`**（id 迁移 6 行：旧 id 留在这里） |

⇒ 导出器**不能假设字段齐全**：缺键时**显式写「该版本没有此字段」**，既不崩、
也不给空值充数。

---

## 2. 版本上传规矩（owner 已裁定）

### 2.1 存量：只推 v14

**v1..v13 不上 LS。** 它们作为**只读历史**留在 `golden/data/`，一个字不改。

### 2.2 增量：每冻结一版就推一版

**「冻结版本 = 一个 LS 项目」从此是常规动作**，冻结纪律变成两步：

```
uv run python -m trajectory_pipeline.evaluation.golden.builder freeze --version 15 --from-version 14 ...
uv run python -m trajectory_pipeline.evaluation.golden_ls upload --version 15
```

⇒ **可执行性要求**：台账要能回答「**冻结了几版、推了几版、欠几版**」
（`golden_ls projects` / `golden_ls pending`，§5）。

⚠️ **`pending` 只算 v14 及以上**（裁定 O-1）：v1..v13 **不上 LS**，**不算欠账**。
把它们列进欠账清单等于发一条**会违反 owner 裁定**的指令——有人照着清单就能把
历史全推上去，而历史版本的判定与 v14 不同，复核了也并不到当前基线上。
（`projects.UPLOAD_BASELINE_VERSION = 14`；裁定变了就改这一个数。）

### 2.3 版本差异矩阵（**作用已变**）

> 量法：逐版与上一版按 `golden_id` 对齐，比 `golden.label` / `golden.dimension_scores` /
> `code.branch` / `code.evidence` / 顶层 `dimension_scores` / 行集合。

**裁定①之后，这张表不再用于「选版」，用于两件事**：
① 记录**哪些历史版本的裁定从未被人看过**（v1..v13 的机器署名金标永远停在 jsonl 里）；
② 给**增量版本**的变更标记提供参照（§2.4）。

| 版本 | 相对上一版的变化 | 存量是否上传 | 增量版要人看的重点 |
|---|---|---|---|
| **v1** | 基线 277 行 | ⛔ | — |
| **v2** | **金标改 75 条**、代码分支改 14 条 | ⛔ | — |
| **v3** | **+17 行**、代码改 3 条 | ⛔ | — |
| v4 | **零** | ⛔ | — |
| v5 | **零** | ⛔ | — |
| **v6** | 顶层 `dimension_scores` 改 **213 条** | ⛔ | — |
| **v7** | 内容零变，但**切分变了**（calibration 191→131、**+regression 60**）+ **rubric 1.2** + 金标署名转机器直签 | ⛔ | — |
| **v8** | 顶层 `dimension_scores` 改 **188 条** | ⛔ | — |
| **v9** | **`golden.dimension_scores` 改 91 条**（`--refresh-gold` ⇒ **金标变动 = 新基线**） | ⛔ | — |
| **v10** | **金标改 14 条** | ⛔ | — |
| v11 | 顶层 `dimension_scores` 改 3 条 | ⛔ | — |
| v12 | 金标改 2 条 | ⛔ | — |
| **v13** | **代码分支 + 证据改 26 条** | ⛔ | — |
| **v14** | **+6/-6 行**（id 迁移）、`label_schema_version` 2→**3**、`access_surface` 全 `unrecorded` | ✅ **唯一存量** | 全量 294 条 |

**存量欠账（明写）**：v1..v13 的历史判定永不复核（§0.4）。

### 2.4 增量版本的**变更标记**（增量上传要人看的重点）

增量版本默认**推该版全量**（保持项目独立完整、可独立复核），但每条 task 带一个
**相对上一版的变更标记**，人一眼知道哪几行是新的：

| 标记 | 含义 | 人该做什么 |
|---|---|---|
| `changed` | 金标或代码判定相对上一版变了 | **重点看** |
| `unchanged` | 内容与上一版一致 | 可跳过（但任务仍在，可抽查） |
| `new` | 上一版没有这一行（新增标注） | 看 |
| `id_migrated` | id 迁移（`golden_id_legacy` 非空） | 看（键变了，内容没变） |

⇒ 提供 `--only-changed` 供省人工（只推 `changed`/`new`/`id_migrated`）。

⚠️ **「金标变了」包括观测面与观测时刻**（2026-10-29 补，方案 20 §R2 把
`golden.access_surface` / `golden.observed_at` 加进了金标块）——不算进去 ⇒
页面上一行显示着「human-browser · 2026-10-12」、标记却写着 `unchanged`
⇒ **人看到的与标记对不上**。⚠️ **但不含 `reviewed_by` / `reviewed_at` /
`note`**：那三栏每次重新标注都动 ⇒ 放进来 ⇒ 标记动辄半版都是 `changed`
⇒ 「重点看」这个词就废了（**判据要宽，宽在「人看到的东西变了」上，不宽在
「时间戳动了」上**）。

---

## 3. LS 任务的数据模型

### 3.1 粒度：**一个 `golden_id` = 一条 LS task**

- 黄金集的行是 **URL 级裁定**（`golden_id` = `{task_id}__{run_id}__{hash}`），
  一行一个结论——正是要人复核的东西。
- 同一 run 内可能有多条 `golden_id`（不同站点）⇒ 各自独立一条 task。
- 跨版本同一 `golden_id` **会重复出现在不同项目里**——这是**有意的**：不同冻结
  版本的金标/代码判定可能不同，正是要并排看的。
- `data.sample_id = golden_id` ⇒ 直接复用现成的 `PushIndex` 判重
  （LS 1.23 **既不认字符串 inner_id、也不按 inner_id 去重**，本地台账是唯一防线）。

### 3.2 `task.data` 的九个区块

> **顺序 = 人读的顺序**（同 `evidence.scene_summary` 的口径：结论在前，现场在后，
> 长正文最后）。

| # | 区块 | 内容 | 来源 |
|---|---|---|---|
| ① | **版本与署名** | 冻结版本号 / `frozen_at` / `label_schema_version` / `rubric_version` / `evaluator_version` / **金标署名 `reviewed_by` + `reviewed_at`**（机器署名要显眼）/ **相对上一版的变更标记**（§2.4） | `manifest.json` + 行内 |
| ② | **站点与来源** | `url` / `site_url` / `play_page_url` / `task_id` / `run_id` / `golden_id`（+ v14 的 `golden_id_legacy`）/ `source_kind` / `status` / **所属档 `split`**（calibration/regression/holdout） | 行内 |
| ③ | **金标（人工真值）** | `label` + `label_label` + `note` | 行内 `golden` |
| ④ | **代码判定** | `branch` + **`branch_label`（取行内的！**§3.4**）** + `question` + `source` + `reached_play_page` + `fallback_used` + `evidence` 原文 | 行内 `code` |
| ⑤ | **维度分数** | 顶层 `dimension_scores` + `scores_detail`（v7 起才有；v1–v6 显式写「该版本没有此字段」） | 行内 |
| ⑥ | **现场读数** | 页面标题 + 每步（动作 → 观察读数）+ 正文预览 + **定位说明**（怎么定位到的 / 为什么读不到 + 候选） | `calibration.evidence.card_digest` / `scene_summary` |
| ⑦ | **尺子** | rubric 条款原文（卡自己的条款 + D-4 + D-5）+ **分支标签表 + 分支前置事实表** | `calibration.evidence.load_clauses`（现读）+ `executor.branches`（现读） |
| ⑧ | **轨迹（T 视图）** | per-Run 完整任务视图的 messages / openai / meta（渲染文本 + 原始视图） | `ls_bridge.exporter.read_task_view` |
| ⑨ | **persona** | 六维 persona | 行内 |

**⑦ 为什么现读而不抄进 label_config**：分支表（22 类）、前置事实表、rubric 条款
**都是代码/文档里的真源**。抄第二份必然漂（CLAUDE.md 纪律 6 的老账）⇒ 一律
**渲染时现读**（条款原文已经这么做了，`clauses_markdown` 的注释原话）。

### 3.3 `label_config`：**由代码渲染，不手抄**

现状是静态 XML `label_configs/trajectory_review_v2.xml`（scout 轨迹复核用）。
黄金集这一路**另渲染一份** `golden_review_v1`：

- **结构性控件**（手写部分，永远稳定）：隐藏锚点 `task_anchor` / `task`
  （LS 1.23 实测：`<TextArea>` 的 `toName` 必须指向真实存在的 tag，缺了直接
  `Validation failed on : 'toName' is a required property`；隐藏只能靠
  `<View style="display:none">`）· 九个区块的只读 `<TextArea>` · 裁决 `<Choices>` ·
  理由 `<TextArea>`。
- **分支选项**（**从 `executor.branches.BRANCH_LABELS` 现读渲染成 `<Choice>`**）：
  22 类封闭集做成下拉，比自由文本可靠——自由文本会拼出非法分支名。

**裁决控件设计**：

| 控件 | 取值 | 必填 |
|---|---|---|
| `verdict`（单选） | `agree` 代码判定与金标一致 / `disagree` 不一致 / `unverifiable` 现场读不到、判不了 | **是** |
| `human_branch`（单选） | 22 类分支（现读渲染）+ `not_applicable` 不适用 | 否（`disagree` 时必填；LS 1.23 无条件必填，只能写在标题里） |
| `reason`（文本） | 理由 | 否 |
| `note`（文本） | 备注 | 否 |

⚠️ **不推 `predictions`（不预填裁决）**。预填裁决等于给答案；标注员看到机器已经
填好的框会照抄——**「机器立场」只能作为只读展示，不能进可填控件**。

⚠️ **署名不设自填栏**：LS 端 annotator 身份由 LS 自己记录（登录态），
**导出时单列一栏**（裁定④，§4.4）——自填栏会造出第二套署名，与 LS 的对不上。

### 3.4 分支标签文本：**取行内的 `code.branch_label`，不取现行 `BRANCH_LABELS`**

`executor/branches.py` 的注释写明：`unreachable_hard` 的口径在 2026-10-27 被
owner 收窄过（M-44），而**「历史存档与黄金集里那 41 行仍带旧文本——不回填」**。

⇒ 用**现行** `BRANCH_LABELS` 去解释**历史**版本的判定，就是拿新尺子量旧活。
⇒ **本条的标签文本取行内的 `code.branch_label`**（那是当时判的），现行表只作
「22 类封闭集」的参照清单放在区块⑦里，**并注明口径代**（`executor.branch_eras`
的 `era_for_date`，按 `golden.reviewed_at` 定位）。

---

## 4. 项目、幂等与台账

### 4.1 一版一项目

| 项 | 口径 |
|---|---|
| 标题 | **`golden-v{N}`**（确定性；`find_project_by_title` 是**精确匹配**，不做模糊） |
| 项目 id | 落**本地台账** `output/ls_bridge/golden_projects.jsonl`（append-only：`version` / `project_id` / `title` / `label_config_sha` / `frozen_at` / `evaluator_version` / `counts` / `splits`） |
| 为什么必须有台账 | `TRAJECTORY_LS_PROJECT_ID` 是**单值**（给 scout 项目 id=77 用的）⇒ 每版一个 id **没有环境变量可放**，台账是必需的，不是可选的 |
| label_config | 渲染一份；**落一份到 output** 留底（可核对、可复现） |
| 项目 description | 写版本元信息（`frozen_at` / `label_schema_version` / `rubric_version` / 条数）——`client.create_project` 现在**硬编码**了 description，要加参数 |

### 4.2 幂等

- `PushIndex` 按 **project_id** 分文件（`push_index__<pid>.jsonl`）⇒ 每版一份，
  天然不串味；`sample_id = golden_id` ⇒ 同版重跑不重复推。
- **冻结档是不可变的**（`frozen_at` 固定）⇒ 内容不会变 ⇒ `sample_id = golden_id` 足够。
  若内容真变了，**那是新版本号 ⇒ 新项目**，不是改老项目。

### 4.3 分档策略：**三档全传**（裁定③）

| 档 | 默认 | 说明 |
|---|---|---|
| calibration | ✅ 传 | 调参池，D-10 头条读数在这里 |
| regression | ✅ 传 | 回归集，防退化 |
| **holdout** | ✅ **传**（裁定③） | 见下面的**补偿三条** |

**裁定③的连带后果与补偿**（holdout 进 LS ⇒ 它不再是「没人对它形成过判断」的持有集）：

1. **金标侧不受影响**——holdout 的金标**不被 LS 裁定改写**（§0.3 不回流）⇒
   它仍可作最终评估基准。
2. **评估器侧要隔离**——**不得用 holdout 行的裁定去调评估器**（prompt / 规则 /
   阈值）。调参只用 `calibration` + `regression` 两档的裁定。
3. **导出记录带 `split` 字段**，回流筛选用 `split ∈ {calibration, regression}`；
   holdout 行的裁定只作**金标质量抽查**，不驱动任何改动。
4. 台账记 `splits` 列表（三档全传时是三档），**不设 `--include-holdout` 闸门**
   ——需要排除时用 `--exclude-holdout`。

### 4.4 导出记录：**两套署名分列**（裁定④）

`golden_ls export-annotations --version N` 落
`output/pipeline/calibration/v{N}/ls_annotations.jsonl`，每行：

| 字段 | 来源 | 说明 |
|---|---|---|
| `golden_id` | `task.data` | **键**（不靠 LS task id 反查） |
| `ls_task_id` | LS | LS 侧 task id |
| `split` | `task.data` | calibration / regression / holdout（补偿③用） |
| `verdict` / `human_branch` / `reason` / `note` | annotation.result（按 `from_name` 取） | 人的裁决四栏 |
| **`annotator`** | **LS 的 annotation** | **裁定④：单列一栏**——LS 侧复核人身份（email / id） |
| **`annotator_kind`** | 由 `annotator` 判定 | `human` / `unknown`（LS 账号可能是机器账号） |
| `annotated_at` | annotation 的 `created_at` / `updated_at` | 复核时刻 |
| **`gold.reviewed_by`** | **原金标署名**（`manual-review` = owner 署的名） | **与 `annotator` 并列，不合并** |
| `gold.reviewed_at` | 原金标署名时刻 | — |

⚠️ **两套署名不是一回事，不能互相替代**：
`manual-review` / `reesn` 是**金标侧**的署名（谁当初定了这个标签）；
`annotator` 是**复核侧**的署名（谁在 LS 上复核了它）。
合并成一栏 ⇒ 「owner 复核过」和「owner 当初定的金标」就分不清了。

---

## 5. 命令

新增 **`trajectory_pipeline.evaluation.golden_ls`** 命令组（**独立 `python -m`
入口，不是 `ls_bridge golden` 子命令**——理由见 §0.3 第 3 条：`ls_bridge` 的
依赖白名单锁死了它；混在一起也会让两个用途的项目互相顶）：

```
uv run python -m trajectory_pipeline.evaluation.golden_ls projects          # 台账视图（版本 → 项目 id → 条数）
uv run python -m trajectory_pipeline.evaluation.golden_ls pending           # 冻结了但没推的版本（欠账）
uv run python -m trajectory_pipeline.evaluation.golden_ls init-project --version 14
uv run python -m trajectory_pipeline.evaluation.golden_ls upload --version 14 --dry-run
uv run python -m trajectory_pipeline.evaluation.golden_ls upload --version 14
uv run python -m trajectory_pipeline.evaluation.golden_ls upload --version 15 --only-changed
uv run python -m trajectory_pipeline.evaluation.golden_ls upload --version 14 --exclude-holdout
uv run python -m trajectory_pipeline.evaluation.golden_ls export-annotations --version 14
```

| 参数 | 缺省 | 说明 |
|---|---|---|
| `--version` | **必填** | 冻结版本号；版本不存在 ⇒ **退 2**（不猜） |
| `--url` / `--api-key` | 环境变量 | LS 地址与凭据；**缺凭据 ⇒ 退 2**（不猜、不静默跳过） |
| `--golden-root` | 包内 `evaluation/golden/data` | 黄金集真源根（**包内相对**，不跟 cwd 走） |
| `--ledger` | 包内 `output/ls_bridge/golden_projects.jsonl` | 项目台账路径 |
| `--splits` | `calibration,regression,holdout` | 三档全传（裁定③）；档名取 `golden/splits.py` 的 `SPLITS`，**不手抄字面量**；**拼错 ⇒ 退 2** |
| `--exclude-holdout` | 关 | 不传 holdout（默认是**传**的）；与 `--splits` 同给时**减法优先** |
| `--only-changed` | 关 | 只推相对上一版有变化的行（`changed`/`new`/`id_migrated`）——增量版本省人工用 |
| `--p2-root` | 包内 `output/pipeline/` | 现场读数的存档根（**本 worktree 里是空的**——存档在主树，要指过去，见 §7.1） |
| `--task-views-root` | 包内 `output/pipeline/task_views/` | T 视图根，同上 |
| `--golden-ids` | 无 | **只推点名的那几条**（逗号分隔，试点用）；**点错的 id ⇒ 退 2**（不静默「少推几条」），重复 id 只推一次 |
| `--limit` | 无 | 取样调试 |
| `--batch-size` | `settings.upload.batch_size`（50） | 单批推送条数；**接上了 `push_batch` 读的那个值**（不许挂而不生效） |
| `--dry-run` | 关 | 只构建计划 + 打印分档/一致率/现场可得率/变更分布，**不推** |
| `--force` | 关 | 超安全闸（复用 `upload.dry_run_skip_threshold`） |

**产出落哪**（全在 gitignored 的 output 区，符合纪律 5）：

| 产物 | 路径 |
|---|---|
| 推送计划（含每条 task 的 data 预览与内容指纹） | `output/pipeline/calibration/v{N}/ls_upload_plan.jsonl` |
| 项目台账 | `output/ls_bridge/golden_projects.jsonl` |
| 判重台账 | `output/ls_bridge/push_index__<pid>.jsonl`（复用现成机制） |
| 渲染出的 label_config 留底 | `output/pipeline/calibration/v{N}/label_config.xml` |
| **人工复核记录**（LS 标注导出，署名分列） | `output/pipeline/calibration/v{N}/ls_annotations.jsonl` |

---

## 6. 落点文件清单（**实施后的实际落点**，2026-10-29）

> ⚠️ 初稿把代码放在 `ls_bridge/` 下（§0.3 第 3 条已说明为什么改）。
> 下表是**实际落地**的清单。

### 6.1 新增包 `trajectory_pipeline/evaluation/golden_ls/`

| 文件 | 内容 |
|---|---|
| `__init__.py` | 包说明：依赖方向单向 + 三条贯穿纪律（读不到就说读不到 / 缺字段与没测是两回事 / 不回流） |
| `__main__.py` | `python -m trajectory_pipeline.evaluation.golden_ls` 入口 |
| `dataset.py` | 读冻结档（manifest / 三档行 / 版本列表）；**只读**，复用 `calibration.worklist.load_frozen_rows` |
| `label_config.py` | **渲染** `golden_review_v1` 的 XML（分支选项**现读** `BRANCH_LABELS`）+ `label_config_sha` |
| `exporter.py` | 冻结行 → `task.data` 的九个区块（**纯函数、不联网**）+ 凭据扫描 + 逐行读数 `GoldenExportResult` |
| `diff.py` | 相对上一版的变更标记（`changed`/`unchanged`/`new`/`id_migrated`）+ `--only-changed` 过滤 |
| `report.py` | 推送计划的读数视图（分档 / **两个一致率口径** / 现场与 T 视图可得率 / 署名分布 / 变更分布）+ 人读文本 |
| `projects.py` | 项目台账（append-only JSONL）+ 版本 → 项目 id 解析 + **`pending`（冻结了没推的版本）** |
| `verdicts.py` | LS 标注导出 → `ls_annotations.jsonl`（**两套署名分列**，§4.4）。⚠️ **R1 实测补**：LS 1.23 的标注对象**没有 `created_by`** ⇒ 身份要从 `created_username` 取（§7.3） |
| `cli.py` | 命令行入口（`projects` / `pending` / `init-project` / `upload` / `export-annotations`） |

> ⚠️ **模块不叫 `annotations`**：`__init__.py` 里的
> `from __future__ import annotations` **真的会执行**，把包命名空间里的
> `annotations` 绑成 `__future__._Feature`，**同名子模块被顶掉**——
> `from ...golden_ls import annotations` 拿到的是那个 `_Feature`，报错信息
> （`'_Feature' object has no attribute 'parse_export'`）完全指不到原因。
> ⇒ 模块叫 `verdicts`（契约测试钉着这条）。

### 6.2 其他改动

| 文件 | 动作 | 内容 |
|---|---|---|
| `trajectory_pipeline/evaluation/golden/signatures.py` | **新增** | **人类署名的唯一真源**：`HUMAN_SIGNATURES = {"manual-review", "reesn"}` + `classify_signature`（不在集合里 ⇒ 自动化，fail-closed） |
| `trajectory_pipeline/evaluation/ls_bridge/client.py` | **改** | ① `create_project` 加 `description` 参数（原先硬编码 scout 的话术）② 加 `list_project_tasks` + `list_task_annotations` + `export_project_json`——导出标注用（原先 client 里没有任何读 annotation 的方法）。⚠️ **R1 实测把初稿的端点打翻了**（§7.2）；⚠️ **身份字段只在一个端点里有 ⇒ 逐条端点必查 + 两边对账**（§7.3） |
| `trajectory_pipeline/evaluation/calibration/evidence.py` | **改** | 抽出 `_archive_and_steps`（**配对逻辑只有这一份**）+ 加 `row_digest`（行级现场读数，不依赖分歧卡） |
| `trajectory_pipeline/evaluation/ls_bridge/scan.py` | **不改** | 初稿说要「扩到 task.data 全部字符串值」——**现成的 `scan_for_credentials(data, view="task_data")` 已经是把整个 dict 序列化后扫**，覆盖每一个字符串值。**不需要改**（改了也是第二份逻辑） |
| `trajectory_pipeline/evaluation/ls_bridge/exporter.py` | **不改** | `push_batch` 直接复用（**不推 prediction**，见 §3.3） |
| `trajectory_pipeline/evaluation/ls_bridge/cli.py` | **不改** | 不在 `ls_bridge` 里挂子命令（§0.3 第 3 条） |

### 6.3 测试与文档

| 文件 | 动作 | 内容 |
|---|---|---|
| `trajectory_pipeline/tests/unit/test_golden_ls_export.py` | **新增** | 导出纯函数单测（九个区块 / 读不到≠没有 / 三种维度分数状态 / label_config / 署名分类 / 凭据拒推 / 报表口径 / 口径代） |
| `trajectory_pipeline/tests/unit/test_golden_ls_diff.py` | **新增** | 变更标记四态 + `--only-changed` + `content_key` 边界 |
| `trajectory_pipeline/tests/unit/test_golden_ls_cli.py` | **新增** | 不联网的 CLI 行为（档名拼错退 2 / 上一版按版本号 / `--batch-size` 真的生效 / `--golden-ids` 点错退 2 / 产物按字节写 / `projects`·`pending` 无凭据也能跑） |
| `trajectory_pipeline/tests/unit/test_ls_bridge_client_tasks.py` | **新增** | 读 task / annotation 的端点口径（**翻页取全** / 不截断 / **task 来源是分页端点**（export 端点只给已标注的）/ **身份字段从逐条端点补上** / **条数对不上记进 `mismatches`** / 标注挂在 task 里也认）——**R1 实测**打翻了初稿的端点假设（§7.2 / §7.3） |
| `trajectory_pipeline/tests/contract/test_golden_ls_contract.py` | **新增** | 契约八条（见 §7） |
| `trajectory_pipeline/docs/08-常用命令速查.md` | **改**（R4 ✅ 2026-10-29） | 增「黄金集上传 LS」段（命令清单的唯一真源）；**`freeze` 那段旁边加一句「冻结完推 LS」**（§2.2 两步纪律） |
| `trajectory_pipeline/docs/07-人工复核与判读手册.md` | **改**（R4 ✅ 2026-10-29） | 增一节：LS 是第几种复核面、怎么用、**两套署名的区别**、回流哪一步 |
| `trajectory_pipeline/docs/计划进展/10-TODO台账.md` | **改**（R4 ✅ 2026-10-29） | 增未闭环项（每条带记录锚点），含「冻结 → 上传」两步纪律的落地项（**M-50–M-53** + 详解段） |

---

## 7. 测试与门禁（**R0 已落地**）

| 门禁 | 内容 | 落在哪 |
|---|---|---|
| 单测 | 导出纯函数：版本容错（v1 无 `dimension_scores` / v14 有 `golden_id_legacy`）/ 现场读不到时**显式写「读不到 + 为什么 + 候选」**（不是空串）/ 分支文本取行内 `branch_label` | `tests/unit/test_golden_ls_export.py` |
| 单测 | 变更标记：`changed`/`unchanged`/`new`/`id_migrated` 四态判对；**没有可比对象 ⇒ 全判 `new`**（不是 `unchanged`——两者后果相反）；`--only-changed` 过滤后条数对 | `tests/unit/test_golden_ls_diff.py` |
| 单测 | CLI 不联网的部分：档名拼错退 2 / 上一版按版本号 / `--batch-size` 真的生效 / `projects`·`pending` 无凭据也能跑 | `tests/unit/test_golden_ls_cli.py` |
| 契约 A1 | **导入方向单向**：`golden_ls → ls_bridge` 可以；`ls_bridge` **不许**反向 import `golden` / `executor`（§0.3 第 3 条的机器版） | `tests/contract/test_golden_ls_contract.py` |
| 契约 A2 | `data` 里出现的控件名 ⊆ 渲染 XML 里的控件名；九个块都有 `$key` 占位；`toName` 指向真实存在的 tag；`<Choices>` 指向**带 value 的** `<Text>`；不用 `alias=`；只用已注册标签 | 同上 |
| 契约 A3 | 同一版本内 `golden_id` 唯一；`sample_id` = `golden_id`；三档行数对得上 | 同上 |
| 契约 A4 | **导出记录里 `annotator` 与 `gold.reviewed_by` 两栏都在**（裁定④）——合成一栏即失败；**没复核的 task 也出一行**（漏掉会让覆盖率读成 100%） | 同上 |
| 契约 A5 | **缺字段显式标注而非空值**（现场读不到 / T 视图读不到 / 维度分数三种状态） | 同上 |
| 契约 A6 | **人类署名集合只有一个真源**：`golden/signatures.py` 与 `tests/unit/test_inc37_validator_pass.py` 的封条集合**逐字一致** | 同上 |
| 契约 A7 | 凭据扫描覆盖 task.data 全文；命中即 **fail-closed 拒推** | 同上 |
| 契约 A8 | **模块名不撞 `__future__` 特性名**（`annotations` 那个坑，见 §6.1） | 同上 |
| 单测 | **`pending` 只算 ≥v14**：v1..v13 不算欠账（裁定 O-1，§2.2） | `tests/unit/test_golden_ls_cli.py` |
| 门禁 | 存量只读（AST 门禁已有，天然满足） | — |
| 单测 | **LS 端点的实测口径**（§7.2 / §7.3）：翻页取全 / 不静默截断 / **task 来源是分页端点** / **身份字段从逐条端点补上** / **条数对不上记进 `mismatches`** / 标注挂在 task 里也认 / **比内容不比 sha** | `tests/unit/test_ls_bridge_client_tasks.py`、`tests/unit/test_golden_ls_export.py`、契约 A4 |
| **实测** | **推后回读**：任务条数 / 九条区块齐全 / LS 端 label_config 内容一致 / 留底 sha 与台账一致 / **一条 prediction 都没有** / **标注条数** / **LS 自称条数与读到的一致** / **每条标注配得回它的 task** / **每条标注都有复核人身份** / **两端点对得上账** | `research/probes/golden_ls_r1_readback.py`（**R1 已过，十项全绿**） |
| **实测** | **标注页实视**——`validate/` 绿灯不算数（`project_manager.validate_label_config` 的原话），必须人打开标注页看一遍渲染 | **R1 已过**（owner 判定「可正常观看 / 勾选 / 文本输入 / 提交」，并实提一条裁定；记录见 §11.3.1） |

**报告口径**（`golden_ls.report`，推送前打印）：
分档分布 · **严格一致**（全量 294 行逐字相等 = 201/294 = 68.4%）
· **D-10 口径 C**（站点级 289 行等价类 = 230/289 = 79.6%，头条 calibration
106/128 = 82.8%）· 现场可得率（216/294 = 73.5%）· T 视图可得率（288/294 = 98.0%）
· 署名分布（原始值 + 人核/自动化分类）· 变更分布（id_migrated 6 / unchanged 288）。
**两个一致率口径都报，各标各的分母**——只报一个就是误导（§1.2 的注）。

### 7.1 ⚠️ 在 worktree 里跑：**数据在主树，产物在本树**

`output/` 是 gitignored 的（纪律 5）⇒ **worktree 里没有 P1 存档、也没有
T 视图**（2026-10-29 实测：本 worktree 的 `output/pipeline/` 下 0 个
`*.json` 存档、0 个 `task_views/`；主树
`C:/Users/klpc/workspace/code/items/simulation_controller/` 下有 248 个存档
与 145 个 T 视图）。

⇒ **不指 `--p2-root` / `--task-views-root` 的后果是「静默的全零」**：

| 读数 | 指对了 | 没指（worktree 默认） |
|---|---|---|
| 现场可得率 | **216/294 = 73.5%** | **0/294 = 0.0%** |
| T 视图可得率 | **288/294 = 98.0%** | **0/294 = 0.0%** |

⚠️ **两个读数都是「合法的」**——0% 不触发任何报错、不触发任何门禁，
推送也照样成功，**推上去的每一条 task 都写着「没读到现场」**。人打开标注页
看到的是一片「读不到现场」，而那不是现场读不到，是**指错了根**。

⇒ **本方案在 worktree 里跑必须显式指两个根**：

```
--p2-root        C:/Users/klpc/workspace/code/items/simulation_controller/trajectory_pipeline/output/pipeline
--task-views-root 同上
```

（推出去的内容**进 LS**，不留在本树 ⇒ 产物落哪不影响 LS 端；判读时读的是
LS 上的内容。）

**判据**：dry-run 打印的「现场可得率」**必须是 73.5%**。若打印 0.0% ⇒
**根指错了**，别推。（这正是「报告口径」那一段存在的意义——**读数是唯一
能看出这件事的地方**。）

### 7.2 ⚠️ LS 1.23.0 的**端点实测**（R1 打翻了初稿的两个假设）

初稿是**照 v1 的记忆**写的端点，没在真机上打过。R1 一跑就发现**两处不成立**：

| 端点 | 初稿以为 | **实测（1.23.0）** | 后果 |
|---|---|---|---|
| `GET /api/projects/{id}/export?exportType=JSON` | `{"tasks": [...], "annotations": [...]}` | 返回 **list**，且**只含已标注的 task**（一条标注都没有时是 `[]`） | 初稿按 dict 写并对非 dict 抛错 ⇒ **「导出人工裁定」这条路一开始就是不通的**（只是没真跑过就没人发现） |
| `GET /api/projects/{id}/tasks?page_size=N` | 能取全部 task | **不分页**（`page_size` 只是截断，且倒序） | 一版 294 条只能拿到前 N 条，剩下的**静默少掉**——而「少了」读起来跟「就这么多」一模一样 |

⇒ 改成 **`GET /api/tasks?project={id}&page_size=N&page=P`**（返回
`{"tasks": [...], "total": N}`，**支持 `page`** ⇒ 翻页取全）。

⚠️ **第三条，也是最阴的一条**：**任务列表里的 `annotations` 数组恒为空**
（实测 `total_annotations=1` 而 `annotations=[]`）⇒ **从任务列表里读标注
永远读到 0 条**，而那读起来跟「一条都没复核」**一模一样**（静默的错）。
真正带标注的是 **`GET /api/tasks/{id}/annotations`**（逐条端点）。
⚠️ 端点名别猜：`/api/annotations?task=3` 与 `/api/annotations?project=78`
在 LS 1.23 里**都是 404**。

⚠️ **还有一条**：LS 存 XML 时**去掉了末尾的换行**。
⇒ 「LS 端那份 == 渲染结果」**比内容、不比 sha**（`label_config_matches`）。
拿 sha 去比会**必然不等**（服务端那份少一个 `\n`）⇒ 把「配置没变」报成
「配置漂了」——**那种误报会让人白白重推一遍**。

⚠️ **这三条的共同教训**：**端点是猜的，行为是实测的**。R1 里凡是「只在
文档里读过、没在真机上打过」的端点，都要重测一遍——**包括看起来最无害的
那个**（`get_project` 就很无害，而它恰好是唯一没出岔子的）。

### 7.3 ⚠️ 标注的**身份字段只在一个端点里有**（R1 实测，2026-10-29）

同一条标注，**两个端点给的字段集不一样**——而缺的那个字段**恰好是裁定④
要的那个**：

| 字段 | `/api/projects/{id}/export`（批量） | `/api/tasks/{id}/annotations`（逐条） |
|---|---|---|
| `id` / `task` / `result` | 有 | 有 |
| **`created_username`** | **没有**（实测键集里压根不存在） | **有**（实测形态 `" reesn@qq.com, 1"`，即「账号, 用户 id」） |
| `completed_by` | 有（**只有数字 id**） | 有 |

⇒ **只读批量端点 ⇒ 署名栏读出来是「1」**（数字 id），而不是账号。
而 `verdicts._annotator_identity` 初稿只读 `created_by`（LS 1.23 的标注
对象里**没有这个键**）⇒ **署名列恒为「（不可用）」** ⇒ **裁定④形同虚设**，
而且**不报错**——导出照样出 5 行，只是那一栏全是「不可用」。

⇒ **改法**（两处都要改，缺一处就白改）：
1. `client.export_project_json`：**每条有标注的 task 都去逐条端点取一份**，
   两边**按 annotation id 合并**（逐条那份补上批量那份缺的字段）；
   **条数与任务列表说的 `total_annotations` 对不上 ⇒ 记进 `mismatches`**
   （不静默、不静默吞）——少报比多报更难发现：它读起来像「没复核」。
2. `verdicts._annotator_identity`：`created_by`（dict/str/int）→
   **`created_username`**（取逗号前那段）→ `completed_by` / `updated_by`。

⚠️ **顺带一条，正好否掉一个吓人的猜法**：**没填的 TextArea 在结果里
整个不存在**（不是空串、不是 `null`）⇒ 「理由栏是空的」= **没填**，
**不是「输入被吞了」**。
依据是**存量 v1 的实测记录**（`archive/v1/docs/label-studio-annotation-export.md`
§4.1）：**同一台 LS 1.23、同一个配方**（`toName="task_anchor"` + 锚点藏在
`<View style="display:none">` 里），`criterion_note` / `revise_notes`
**没填时整列不存在，填了就有内容**（v1 真收到过
`media.playable: 实际给了可播放链接` 这种人手写的自由文本）。
⇒ 本项目的 `reason` / `note` 是**同一个配方** ⇒ **输入不会被吞**。

✅ **2026-10-29 补上「阳性对照」**（本项目**自己的数据**，R2 推送前补的）：
owner 在 task 4（`E006__2729220b__e9b9542b`）上选了 `disagree` + 分支
`not_play_site`，**理由栏真写了一句**「人工可访问，代码可能被拦截」
⇒ 回读**原样读出** `reason = 人工可访问，代码可能被拦截`（`note` 没填 ⇒
整条不存在）⇒ **「填了就有」在本项目上实测过了**，不再只靠 v1 的文档背书。
⇒ 「空理由 = 没填，不是被吞」**从「推断」升级成「实测结论」**。

> ⚠️ **v1 那条实测记录里还留着一句同源的警告**，本项目照抄了配方，
> 就得连它的验收动作一起抄：**「改完必须在标注页点一次并提交，确认 region
> source 没被 `display:none` 掐断」**（v1 在那里踩过一次「点一下整个标注区
> 崩成空白页」，而**那次 `validate/` 是报绿的**）。本项目的验收动作就是
> §11 的实视清单 + 回读探针（`research/probes/golden_ls_r1_readback.py`）。

---

## 8. 风险与对策

| # | 风险 | 对策 |
|---|---|---|
| **R1** | **LS 1.23 静默丢 prediction**（控件名不符 ⇒ `201 {"created": 0}`） | 本方案**不推预测**（§3.3）⇒ 这条风险只剩「控件名不符 ⇒ 提交报错」，实测即见 |
| **R2** | **holdout 泄漏**（裁定③后**默认可见**） | 金标侧不受影响（不回流）；**评估器侧禁用 holdout 裁定调参**；导出带 `split`，回流只取 `calibration`+`regression`（§4.3 补偿三条） |
| **R3** | **人工成本**（v14 294 条；每个增量版本再 +294） | 增量版本提供 `--only-changed`；一致行也要能看（裁定② + G1 的机器署名金标在里面） |
| **R4** | **现场只有 73%** | 读不到就**显式写「读不到 + 为什么 + 候选」**；⚠️ **绝不写空串**——空读数会被读成「页面里没有 video 标签」 |
| **R5** | **口径漂移**（分支标签文本换代） | 取行内 `branch_label` + 标口径代（§3.4） |
| **R6** | **逐版字段集不同** | 导出器版本容错：缺键显式标注，不崩也不充空值（§1.5） |
| **R7** | **LS 端项目 id 撞车** | 标题 `golden-v{N}` 精确匹配 + **本地台账**记 version→pid（§4.1） |
| **R8** | **人工复核结果无处落** | `golden_ls export-annotations` 落 `ls_annotations.jsonl`（output 区，**不入真源**），**两套署名分列** |
| **R9** | **LS 标注被当成金标** | 文档与台账双处写明：**LS 标注 ≠ 金标**；回流要人走 `builder annotate` 整行模式（带 `access_surface` + `observed_at`）⇒ 且**金标变动 = 新基线** ⇒ 重冻结出 v15 |
| **R10** | **「冻结了没人推」**（增量上传是纪律，不是自动的） | `golden_ls pending` 列出未推版本；**08 速查的 `freeze` 段旁边写「冻结完推 LS」**；TODO 台账留一条闭环项 |
| **R11** | **存量历史判定永不复核**（裁定①的代价） | 明写为**已知不覆盖项**（§0.4），不让后来人误读成「都复核过了」 |

---

## 9. 路线

| 阶段 | 内容 | 完成判据 | 状态 |
|---|---|---|---|
| **R0** | 渲染器 + 导出器 + 变更标记 + 单测/契约（**不联网**） | `pytest` 通过；`--dry-run` 出计划且分档/一致率/现场可得率/变更分布对得上 §1 | ✅ **2026-10-29 完成**：全量 `4502 passed, 45 skipped`；v14 dry-run 读数 131/60/103 · 严格 201/294=68.4% · 口径C 230/289=79.6% · 现场 216/294=73.5% · T 视图 288/294=98.0% · 署名 82/212 · 变更 id_migrated 6 / unchanged 288 |
| **R1** | **只推 v14 一个版本**，先 3–5 条人工试点 | 标注页**实视**通过（渲染对、尺子在、能判）；试点裁定可用 | ✅ **2026-10-29**：项目已建（`golden-v14`，pid=78）；**5 条试点已推**（`tasks_pushed=5`）；**人工实视通过**（owner 判定「可正常观看 / 勾选 / 文本输入 / 提交」，并**实提了一条裁定**——task 3 = `T001__d063ddf4__106df089`，`agree` + `trailer_only`，署名 `reesn@qq.com`）；**回读探针十项全过**（`research/probes/golden_ls_r1_readback.py`：条数 / 九条区块 / LS 端 label_config 内容一致 / 留底 sha 对台账 / **0 prediction** / 标注条数 / LS 自称条数 / 配得回 task / **有复核人身份** / 两端点对得上账）；⚠️ **实测打翻初稿三处端点假设**（§7.2 / §7.3，已修） |
| **R2** | v14 全量（**三档 294 条**，裁定③） | 推送台账齐；重跑不重复；导出 `ls_annotations.jsonl` 可读、**两套署名分列** | ✅ **2026-10-29**：**v14 全量 294 条已落在 pid=78**（一版一项目，**没新开项目**）——`tasks_pushed=289` + `skipped_duplicate=5`（本地判重台账按 `sample_id = golden_id`）+ `batches=6`。⚠️ **推送前先补了「不一致」路径**：owner 在 task 4（`E006__2729220b__e9b9542b`）上选了 `disagree` + `not_play_site` + 理由「人工可访问，代码可能被拦截」⇒ 回读确认 `reason` 落库（§11.3.1）⇒ **这条路是通的，不是纸面能力**。⚠️ **踩了一个坑**：**worktree 里 `output/` 是空的** ⇒ 第一次 dry-run 的现场/轨迹读数是 **0/294 = 0.0%** ⇒ 必须显式指 `--p2-root` / `--task-views-root` 到主树，否则推上去的是**空块**（§7.1）。**判据**：回读探针 `--expect 294 --expect-annotations 2` **十项全绿（exit 0）**；**增量一百** |
| **R3** | **增量版本上传通路跑通**（拿下一个冻结版本 v15 试推） | v15 项目独立；变更标记对；`--only-changed` 过滤条数对；`golden_ls pending` 归零 | ✅ **2026-10-29**：v15 项目独立（**pid=79**，title `golden-v15`，label_config sha `376f822addb1`）⇒ `--only-changed` 实推 **7 条**（`changed` 1 + `id_migrated` 6，**与标记逐条对得上**）⇒ 全量补推 `tasks_pushed=287` + `skipped_duplicate=7` = **294** ⇒ `pending --strict` **欠账归零（退 0）** ⇒ 回读探针 **十项全绿（exit 0）**。⚠️ **顺带修了一处判据**：`content_key` 原本**不含** `golden.access_surface` / `golden.observed_at` ⇒ E006 那两栏变了却标 `unchanged` ⇒ **页面显示与标记对不上** ⇒ 已补这两栏（**只补这两栏**：不补 `reviewed_at`/`reviewed_by`/`note` ⇒ 那三栏每次重新标注都动 ⇒ 放进来标记就废了）。⚠️ **不影响既有版本对**（v13/v14 的 294 行一个都没带这两栏 ⇒ 已推的 v14 项目不变） |
| **R4** | **「冻结 → 上传」两步纪律进文档** | 08 速查 `freeze` 段旁写明；TODO 台账闭环项落地 | ✅ **2026-10-29**：08 速查新增「**黄金集上传 LS**」命令段 + `freeze` 那行后的「🔗 冻结完就推 LS」；07 手册新增 **§8「冻结黄金集 · LS 复核面」**（第 8 种复核面：怎么用 / **两套署名的区别** / 回流哪一步 / 常见坑）；TODO 台账新增 **M-50–M-53** 四条（含详解段）。✅ **补上「看得见」的那一半**（owner 裁定①「每个增量版本都上传」+ ③「holdout 也上传」⇒ 欠账不能只靠人记）：**M-53 已落**——`golden_ls pending --strict`（欠账非空 ⇒ **退 1**；是**状态**不是用法错误 ⇒ 不是 2）+ `builder freeze` 收尾**提醒**一句。⚠️ **提醒只改 stdout、不改退出码**：把「上不上 LS」变成冻结的前置条件 ⇒ 人为了过门禁就得建项目 ⇒ 裁定③与「攒几版一起上」的自由度会被挤掉——**看得见 ≠ 卡得住** |
| **R5** | 回流链路（**单独一步，不与上传混做**） | 人确认后的裁定走 `builder annotate`（只用 calibration+regression）⇒ 重冻结 v15+ ⇒ gate 退 2 生效 | ✅ **2026-10-29（确认型那条路实跑过一遍）**：新模块 `evaluation/golden_ls/reflow.py`（LS 裁定 → **工作表** → 写回 `annotations.jsonl`，写回走 `builder annotate` **整行模式**，**不另写一套合并逻辑**）。⭐ **两类裁定两条路**：**确认型**（标签不变 ⇒ 只补观测面 ⇒ `freeze --refresh-surface`，**不换尺子**、本版与上一版仍逐行可比）/ **改判型**（标签要改 ⇒ `freeze --refresh-label`，**换尺子 ⇒ 新基线 ⇒ gate 退 2 是对的**）⇒ **「金标变动 = 新基线」只对改判型成立**——为确认型在 builder 里**新开一个出口** `freeze --refresh-surface`（只重挂 `golden.access_surface` + `golden.observed_at`，落 `baseline_reset=false`，C-17 回归集指纹不变）。**实跑**：LS 侧 5 条裁定 → **2 条确认**（T001 / E006）+ 3 条跳过（`not_reviewed` ×2 · holdout ×1）⇒ **E006 写回**（面 `human-browser` + 时刻 = 同一次人判的 `reviewed_at`）⇒ **v15 冻结**：294 行里**只有 1 行变了**，采集面 `unrecorded: 294` → `unrecorded: 293 / human-browser: 1`，回归集指纹 `3418ebd05a4c` **未变**。⚠️ **T001 被扣下（fail-closed）**：面与时刻**推不出来**——P1 存档里既没有 `driver` 也**没有任何采集时刻**（里面的 `2026-07-15 14:00:24` 是页面自己的「更新时间」、`2026-10-28` 是 Q-23 现场复核＝**另一个观测事件**）⇒ 留在工作表里下轮接着办。⚠️ **gate 对 v15 退 1**：净改善 0（修好 0 − 反转 0）——v15 **只动了金标的观测面，pred 侧一行没动** ⇒ 配对读数与 v14 逐条相同 ⇒ 这是**「什么都没变」那一类**，不是「变坏了」。⚠️ **改判型那条路代码齐了但没有真实裁定走过**（现有 5 条裁定里没有一条是「不等价的 disagree」）⇒ 它只在单测里被走过 |

---

## 10. 裁定记录（owner，2026-10-29）

| # | 问题 | **裁定** | 落到本文哪节 |
|---|---|---|---|
| **O-1** | 上传哪几版？ | **存量只上传 v14；以后增量版本每个都上传** | §2.1 / §2.2 / §2.4；连带影响见 §0.4（R11） |
| **O-2** | 一致行也传吗？ | **传** | §1.2 / §3.2 区块①②；`--only-changed` 另给 |
| **O-3** | `holdout` 传不传？ | **也传** | §4.3（默认三档全传 + 补偿三条；R2 重写） |
| **O-4** | LS 标注回不回金标？ | **不回**（本方案范围外，R5 单独做） | §0.3 第 1 条 / R9 |
| **O-5** | LS 标注员身份怎么记？ | **单列一栏**（`manual-review` = owner 署的名，两套署名不合并） | §3.3 末 / §4.4 / 契约测试 |

---

## 11. R1 人工实视清单（**`validate/` 绿灯不算数**）

LS 1.23 的 `validate_label_config` 只查**结构**（控件名对不对、`toName` 指得
到吗）。它**不查**「人看不看得懂」「尺子在不在」「判得了判不了」——而这
三样才是这批 task 存在的理由。

⇒ **打开 <http://127.0.0.1:8099/projects/78> 的标注页，逐条看下面 5 行。**
（`golden-v14` 项目，pid=78。）

### 11.1 试点这 5 条**各自要验什么**

| # | golden_id | 档 | 覆盖的路径 | **这一条要验的** |
|---|---|---|---|---|
| 1 | `T001__d063ddf4__106df089` | calibration | 自动化署名 · 一致 · **现场读到了** · 维度分**部分为空**（d2/d5=None） | 现场区块能不能看出「有观察、有控件计数」；空维度是不是显式的「空」而不是「0」 |
| 2 | `E006__2729220b__e9b9542b` | calibration | **人核署名**（`manual-review`）· **金标与代码不一致**（金标 `not_play_site` vs 代码 `unresolved`）· 现场读到 | **「不一致」这件事在页面上看得出吗**；`用了兜底=True` / `LLM 不可用（http:403）` 要看得见（这才是分歧的成因） |
| 3 | `T012__8c050356__e287b390` | calibration | **现场没读到**（存档里 url 对不上）· 维度分空 | 页面明写「**没读到现场**（不是页面里没有播放控件）」+ 列出存档现有单元与 url ⇒ 人能分清「没访问过」和「url 对不上」 |
| 4 | `T024__b54092ee__a96ea853` | **holdout** | holdout 档也在（裁定③）· 现场没读到 | holdout **在同一个项目里可见**（这是裁定的预期，不是泄漏——泄漏的口径在 §4.3） |
| 5 | `P015__59a2ea71__f2bf859d__neg_broken` | regression | **负样本池**（没有存档）· **变更标记 `id_migrated`**（旧 id `...neg161`） | 「旧 golden_id」在页面上看得见（id 迁移要知道迁到哪）；负样本池那条明确写「没有可切的存档 ⇒ 读不到现场」 |

### 11.2 每条都要看的 8 项

| # | 看什么 | 怎么算**不通过** |
|---|---|---|
| 1 | **区块齐不齐** | 九个区块（版本 / 站点 / 金标 / 代码 / 维度分 / 现场 / rubric / 变更 / 档）少任何一个，或顺序乱 |
| 2 | **尺子在不在** | 页面里能直接读到 D-10 / D-4 / D-5 / D-6 的**判据与评分锚**——不必跳去别的页面才能判 |
| 3 | **「没读到」是不是显式的** | 出现**空的**观察读数、**空字符串**、或「没读到」被写成「页面里没有 video 标签」⇒ **不通过**（R4 的老坑） |
| 4 | **空维度 ≠ 0** | `d2: null` 要显示成「空 / 没测」，不能显示成 `0` 或 `5` |
| 5 | **署名** | 页面**只显示金标侧那一套署名**（`manual-review` / `auto-validator-…`），**没有自填署名栏**——这是设计（§3.3 末：署名不设自填栏，否则造出第二套署名）。LS 侧的复核人身份由 **LS 自己**记，**导出时**才单列（§4.4）。⇒ 页面上**看不到** LS 署名字段**是正常的**，不是缺件 |
| 6 | **能判** | 每条都能在**裁决下拉**里选出一个：`agree` 代码判定与金标一致 / `disagree` 不一致 / `unverifiable` 判不了（**3 个**）。选了 `disagree` 才需在**分支下拉**里给一个（**22 类封闭集 + `not_applicable`**，取自 `BRANCH_LABELS`，**现读不手抄**） |
| 7 | **提交不报错** | 填完提交**不报**「控件名不符」（LS 1.23 会**静默丢**不匹配的 prediction ⇒ `201 {"created": 0}`，这是 R1 风险）。✅ **R1 已验**：本方案**不推 prediction**，回读时每条 task 的 `predictions` 恒为空 |
| 8 | **隐藏锚点不露相** | `sample_id` / `split` / `version` 这些锚在 `<View style="display:none">` 里 ⇒ 页面上**不该**看见一堆乱码字段（能看见不影响判读，但说明隐藏没生效） |

### 11.3 判读完记哪

试点裁定**不进金标**（裁定④ / R9）：本轮只验「这批 task 能不能判」，
**不产裁定结论**。判读完在**本节**记一行「哪几条通过 / 哪条卡在第几项」，
再决定 R2（全量 294 条）能不能开。

> ⚠️ **不要用「提交成功」代替「判得了」**——提交成功只说明控件名对，
> 不说明尺子在、也不说明人判得下去。

#### 11.3.1 **R1 的实视结果（2026-10-29）**

| 项 | 结果 |
|---|---|
| **实视方式** | owner 在 <http://127.0.0.1:8099/projects/78> 的标注页人工过了一遍 |
| **owner 的判定** | **「可以正常观看与勾选、文本输入、提交」** |
| **实提的裁定** | task 3 = `T001__d063ddf4__106df089`（calibration · 现场 located · 维度分部分为空）⇒ 裁决 `agree` + 分支 `trailer_only` |
| **回读到的裁定** | `verdict=agree` / `human_branch=trailer_only` / **`annotator=reesn@qq.com`**（`annotator_kind=human`）/ `annotated_at=2026-10-29T05:42:48Z` / `annotation_count=1` |
| **两套署名是否分列** | ✅ 该行 `gold.reviewed_by = auto-validator-l3l4-v1-no-text-signal`（**机器署名的金标**），与 LS 侧 `annotator = reesn@qq.com` **分列**（裁定④）——**正是「owner 复核过」与「owner 当初定的金标」分得清的那一栏** |
| **机器回读十项** | ✅ 全过（探针 `research/probes/golden_ls_r1_readback.py`，证据 `golden_ls_r1_readback.json`） |

⚠️ **本轮实视的覆盖度要说实话**：

* **实提裁定的只有 5 条里的 1 条**（task 3）；
* **11.2 的 8 项**里，owner 的判定是**整体性的**（「能看 / 能勾 / 能打字 /
  能提交」），**逐条 × 逐项的核对表没有留痕**；
* ⚠️ **「不一致」那条路（`disagree` + 写理由）本轮没走**——task 3 恰好是
  `agree`。⇒ **R2 之前建议补一次**：在**同项目里已有的** task 4
  （`E006__2729220b__e9b9542b`，金标 `not_play_site` vs 代码 `unresolved`）
  上选一次「不一致」并**在理由里写一句** ⇒ 回读确认 `reason` 落库。
  **不用重推**——那 5 条试点已经在项目里了。

  ✅ **补过了（2026-10-29，R2 推送前）**：owner 在 task 4 上选了
  **`disagree` + 分支 `not_play_site`**，理由栏写了**「人工可访问，代码可能被拦截」**
  ⇒ 回读**原样读出** `verdict=disagree` / `human_branch=not_play_site` /
  `reason=人工可访问，代码可能被拦截`（`note` 没填 ⇒ 整条不存在）/
  `annotator=reesn@qq.com` ⇒ **「不一致 + 写理由」这条路走通了**，
  且**「填了就有」在本项目上拿到了阳性对照**（§7.3 末）。
  ⚠️ **这条裁定的含义要说清**：LS 裁决①的定义是「**代码判定**与金标一致（不用改）」
  ⇒ 选 `disagree` = **代码判定错了**；owner 给的 `not_play_site`
  **与金标一致**（金标就是 `not_play_site`，`manual-review` 签的）⇒ 这一条 =
  **「金标对、代码的 `unresolved` 错」**，**不是「金标要改」**。
  ⚠️ **仍然不进金标**（裁定④ / R9）——要回流得走 `builder annotate` 整行模式，
  且这条的 `access_surface`（人工面）与 `observed_at`（2026-10-29）得由人确认后写进去。
* ⚠️ **「理由栏会不会吞字」已有独立依据**（§7.3 末）：**没填的 TextArea 在
  结果里整个不存在** ⇒ 空理由 = 没填；且 v1 在**同一个配方**上实测过
  「填了就有」⇒ **不是被吞**。

⚠️ **这批试点的裁定**（task 3 那条）**不进金标**（裁定④ / R9）：它只是
「这批 task 能判」的证据。要把它变成金标，得走 `golden.builder annotate`
整行模式（带 `access_surface` + `observed_at` + 署名）⇒ 且**金标变动 =
新基线** ⇒ 重冻结出 v15（§0.3 第 1 条 / R5 / R9）。
