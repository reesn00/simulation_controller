# 16 · 评估器与 rubric 的双向校准回路

**上游**：[`11-外部方案对账.md`](11-外部方案对账.md)（黄金集校准循环 · 四条铁律）· [`02-rubric与代码对齐.md`](02-rubric与代码对齐.md)（两侧对齐账）· [`03-rubric修订与实施方案.md`](03-rubric修订与实施方案.md)（rubric v1.1 改稿）· [`10-judge接入方案.md`](10-judge接入方案.md)（D-3 / D-9 软维度）
**状态**：草案 v2.8 · 2026-10-24 · **部分实施**：**R0 全闭环**（C-01 / C-02 / C-03 / C-04 / C-15），**R0.5 全闭环**（C-17 / C-08 / C-09），**R1 全闭环**（C-05 / C-06 / C-16 / C-19 / **C-07 ✅**——第一份分诊档已由人填完 33/33），**R2 全闭环**（C-12 ✅ / C-18 ✅ / **C-10 ✅ / C-11 ✅**），**贯穿项全闭环**（**C-13 ✅ / C-14 ✅**），见 [§9](#9-任务执行计划)。**v2.5 起「分诊档还是空的」这句话作废**：33 行全部由人签（`triage_by=reesn`），`bucket_clear` 判据**第一次拿到真实输入**。**v2.6 记 D-5 恒真修复在回路上留下一处口径缺口**（pred 侧取自冻结行 ⇒ 评分器改动不进快照），这就是台账 M-29。**v2.7 记 D-C4 / D-C6 / D-C7 已裁定**（[§2.2](#22-待-owner-拍板的决策)）——三条裁定各有后果，其中一条**推翻了 D-C9 的后半句**。**D-C5 / D-C8 / D-C9 / D-C10 / D-C11 / D-C4 / D-C6 / D-C7 已裁定**；D-C2 / D-C3 随 C-05 落地为**代码里的封闭集与身份路由**（不再待裁）。**v2.3 补上 C-06 缺的「读现场」那一半**（`worklist show` + `evaluation/calibration/evidence.py`）——工作表给的是「结论 + 一句话证据」，**不是现场**，而 D-10 的判据是「对照黄金集**预存档**的站点核验结论」：没有现场，人判不了桶。**v2.4 把现场搬进人填的那张表**（`worksheet.jsonl` 每行带 `scene` 栏 + 尺子另落 `v{N}/clauses.md`）——「有现场但要另开文件、跑另一条命令」和「没有现场」判起来是一回事：**表里没有可判的东西，人就判不了桶**。**v2.8 补上软维度金标的写入链**（工作表 `goldwork` / 只写分数 `annotate --dimensions-only` / 进新版本 `freeze --refresh-gold`）——**它补的不是新能力，是一条断掉的链**：这条链不通，`dimension_scores` 就永远填不进去（`freeze --from-version` 是「行内容不动」）⇒ **三分之二的读数是空的**（v8 快照 844/1133 `ungraded`）。⚠️ **读数仍未变**：v8 表 **364 格待填 364**——**工具齐了，填表是人做的**，别把「工具齐了」读成「读数有了」
**性质**：**机制设计**。不改 rubric 语义、不改分支表、不改 P1/P2/P3 契约。

**一句话**：**把「不一致」从一份报表上的数字，变成一个可以被指认、被修复、被复查的对象。**

---

## 0. 目标与一处框架修正

### 0.1 目标（用户原话）

> 由每次评估结果对比黄金数据集分，推动调整评估器与 rubric 准则；目标是让评估器的逻辑或 LLM 提示词策略优化得更加贴合评判准则，也让 rubric 准则调整得更加高质量。

### 0.2 框架修正：一次只动一侧

目标是对的，但**操作上不能"同时改两边"**——改了两边、数字上去了，无法判断是谁的功劳。可归因性是本项目最值钱的资产（[15 号 §0 D-A10](15-执行器与agent-runtime重新定义.md)），校准回路不能成为它的例外。

正确形态：

> **一条分歧 → 归属到一个责任方 → 一次只改那一方 → 用净改善验证 → 记清是哪次改动修好了它。**

"两侧都变好"是长期结果，不是单次动作。

### 0.3 目标的可检验翻译

| 目标 | 可检验形式 |
|---|---|
| 每次评估都推动调整 | 每条不一致都有归属（归属率 100%，无 `other` 桶）|
| 评估器更贴合准则 | 每条评估器改动能指回具体分歧，且 `净改善 = 修对 − 弄坏 > 0` |
| rubric 更高质量 | 每条 rubric 改动能指回具体分歧 + 人裁记录 + 责任区分声明 |
| 可复查 | 每个冻结版本记录 `rubric_version` / `evaluator_version` / `holdout_runs`；分歧 ↔ 变更双向可查 |
| **触发可量化**（v1.3）| 每条触发信号有「**读数命令 → 阈值 → 触发后动作**」三段，读数是命令输出而非人翻看（[§5.4](#54-量化触发v13-新增)）|
| **修改到条款**（v1.3）| 每次 rubric 变更只列条款号；**未列条款的分数必须逐位不变**（[§5.5](#55-修改粒度条款级v13-新增)）|
| **防越改越偏**（v1.3）| 每次改版对固定回归集重跑：`regressed == 0` 且 `net > 0` 才许合（[§5.6](#56-回归集黄金校准集v13-新增)）|

---

## 1. 现状核对：三个洞

### 1.1 分歧没有沉淀成对象

`cmd_agreement`（[`evaluation/golden/builder.py:1012`](../../evaluation/golden/builder.py)）输出的是**聚合数**：口径 C 一致率、`classification_report` 的 per_class 计数、软维度的 `n / agreed / skipped`（[:285](../../evaluation/golden/builder.py)）。

**逐条不一致没有落盘。** 后果：

- 无法回答"这次改动修好了哪几条、弄坏了哪几条"——H1-α 那次修 4 条 FN、误伤 6 条 SUCCESS（[11 号附录 A](11-外部方案对账.md)）靠的是附录文章复述，不是数据；
- 无法分诊，也就无法知道"rubric 缺陷"与"评估器缺陷"各占多少。

### 1.2 rubric 没有版本号进代码

- 全树**没有** `RUBRIC_VERSION` 常量；`rubric v1.2` 只活在 docstring（[`d3_scorer.py:5`](../../evaluation/scorers/d3_scorer.py)）与测试函数名里；
- `v{N}/manifest.json` 与 `agreement.json` 只有 `label_schema_version`，**没有 rubric 版本**。

后果：**"v6 这批 74.7% 是哪版尺子量出来的"，系统答不出来。** 而 rubric 已经改到 v1.2，下一轮再改就无从对账。

### 1.3 改动没有反向溯源

- rubric 变更记录写的是"依据：用户裁定"（[rubric 变更记录](在线视频场景rubric.md)），**不指向具体分歧行**；
- INC32–INC43 的评估器改动（分支关键词 / `rule_perceptor` / few-shot）**没有挂到具体不一致条目**。

后果：同一批偏差可能被反复投入同一个错误假设，而上一轮的结论只存在于编年史文章里。

---

## 2. 设计原则与待裁决策

### 2.1 四条原则

| # | 原则 | 理由 |
|---|---|---|
| **P1** | **分歧是对象，不是噪声** | 只有落盘的条目才能被指认、复查、挂接改动 |
| **P2** | **一次只改一侧** | 两侧同改 → 归因断裂 |
| **P3** | **两侧的证据门槛不对称** | 评估器能自证（重跑净改善可量化）；rubric 不能自证（尺子不能给自己打分）|
| **P4** | **不做自动调参、不做投票/集成** | 沿用 CLAUDE.md 红线；人推开环 |

### 2.2 待 owner 拍板的决策

> **2026-10-22：D-C8 / D-C9 / D-C10 已按建议裁定（owner）。** 其余 D-C1～D-C7 仍待拍板，但都不阻塞 C-02 / C-15。
> **2026-10-23：D-C11 已裁定（owner 选「冻结时闸门」，不接按 push 的 CI）**；D-C1～D-C4 / D-C6 / D-C7 随 C-05 / C-10 / C-14 落地时一并裁。
> **2026-10-23（v1.9）：D-C2 / D-C3 随 C-05 落地**——不再是"建议"，是 `buckets.py` 的 import 期封顶与 `triage.validate_rows` 的身份路由（契约测试逐桶封着）。
> **D-C1 的第一半也落了**（卡留在 `output/`，分诊档进版本控制）。
>
> **2026-10-23（v2.7）：D-C4 / D-C6 / D-C7 已裁定（owner）——三条各有后果，其中一条推翻了 D-C9 的后半句。**
>
> | 裁定 | 内容 | 后果 |
> |---|---|---|
> | **D-C7** | **rubric 变更强制连带改 prompt** | C-10 落地为「锚点表 + prompt 从表渲染 + 契约测试逐档比对 rubric 文档」。**改 rubric 某档而不改表 ⇒ 测试红** |
> | **D-C4** | **合并等价类不需 owner 签** | C-13 的「owner 签」那一半**作废**；「无人记账」那一半**保留**——每个多标签等价类配一张**代码侧注记**（规范标签 / 被并标签 / 丢了什么细分 / 为什么并），注记与表**逐标签对齐**，对不上即红。**没有签名字段** |
> | **D-C6** | **阈值调整不算 rubric 级变更** | C-14 从「按 rubric 级变更处理」改成**阈值自有其面**：唯一真源 `evaluation/scorers/thresholds.py` + 各处 `thresholds.value("<name>")` 取值 + 契约测试盯「表不腐 / 不留字面量 / 与 rubric 版本互不 import」。**不升 rubric 版本、不进 rubric 变更记录、不设签名门** |
>
> ⚠️ **D-C6 推翻了 D-C9 的后半句**：D-C9 原写「新线第一批只出读数，**定线本身按 rubric 级变更（D-C6）走**」——D-C6 既已裁定「阈值调整不算 rubric 级变更」，**「定线走 rubric 变更流程」这条随之作废**：定线不进 rubric 变更记录，只进阈值表（`status` 从「只出读数」改成「已定线」）。两条裁定咬在这里，写出来免得后人在 D-C9 里读到一条已经作废的路径。

| # | 议题 | 建议 | 理由 |
|---|---|---|---|
| **D-C1** | 分歧卡是否纳入版本控制 | **不纳入**，落 `output/pipeline/calibration/`；**人填的分诊档纳入** `evaluation/golden/data/triage/` | 派生数据可再生（派生自冻结档）；人工裁定是资产，丢了不可重建（与 `annotations.jsonl` 同理）|
| **D-C2** ✅ | 桶表长度 | **封顶 6 桶**，超出的情形一律留空待人判 | `BRANCH_PRECONDITION` 的教训：表长到跟分支表一样长就没人维护 |
| **D-C3** ✅ | 谁可以填 `rubric_*` / `gold_*` 桶 | **只有人类 id**；agent 填的写入一律拒绝 | 尺子与真值的裁定权不外放 |
| **D-C4** ✅ | 等价类变更是否需 owner 签 | **否** | 合并等价类是"放水"最省力的路径，且现在无人记账——**签字那一半被裁掉，记账那一半留着**：改成**代码侧注记**（每个多标签类一张：规范标签 / 被并标签 / 丢了什么细分 / 为什么并），注记与 `EQUIV_CLASS` **逐标签对齐**，对不上即红（C-13） |
| **D-C5** ✅ | holdout 是否真封存 | **是**，但需先改代码（当前 D-10 分母含 holdout）| 不改则"持有集只跑一次"只是纸面纪律。**已裁定 2026-10-22**（随 C-03 一并落地：头条分母改为只算调参池）|
| **D-C6** ✅ | 阈值调整的性质 | **不算 rubric 级变更** | 「差一点点」要防，但防它的不是签名——是**阈值自有其面**：唯一真源 + 各处只从表取值 + 契约测试盯「表不腐 / 不留字面量 / 与 rubric 版本互不 import」（C-14）。**不升 rubric 版本、不进 rubric 变更记录、不设签名门** |
| **D-C7** ✅ | rubric 变更是否强制连带改 prompt | **是**，由 contract test 盯 | 否则 prompt 与准则各走各的（现在就各走各的）。落地形态：锚点表 + **prompt 从表渲染** + 契约测试**逐档比对 rubric 文档**——改 rubric 某档而不改表 ⇒ 红（C-10） |
| **D-C8** ✅ | 回归集从哪划 | **从 calibration 划出固定 60 条**（v6 现有 191 条），冻结、**永不作调参依据** | 从 holdout 划会每版都烧一次，破坏「只跑一次」（D-C5）；新增标注最干净但要人时。⚠️ 代价：调参池 191 → 131。**已裁定 2026-10-22** |
| **D-C9** ✅ | 触发阈值谁定线 | **已有线沿用**（D-10 ≥0.90 / 硬 kappa ≥0.70 / 软加权 ≥0.50 / B-3 ≥85%）；**新线第一批只出读数**，定线本身按 rubric 级变更（D-C6）走 | 拍脑袋的阈值会变成新的「差一点」借口（[§6](#6-防放水的五条硬规矩) 第 3 条）。**已裁定 2026-10-22**。⚠️ **v2.7 修正**：后半句「定线走 rubric 级变更（D-C6）」**已被 D-C6 裁定推翻**——定线只进阈值表（`status` 改「已定线」），**不进 rubric 变更记录**。⚠️ **2026-10-24 上岗线重定**：D-10 **≥0.90 → ≥0.80**（owner 裁定，真源 `evaluation/scorers/thresholds.py` 的 `d10_min_agreement`）——本行记的是**裁定当时**的线，现役线以阈值表为准 |
| **D-C10** ✅ | 分层抽样口径 | **3 层 + 1 对照层**、固定配额、固定 seed、落 `stratum` 与抽样权重；**分层样本上的 kappa 不做总体外推**，只报层内读数 | 分层样本不是随机样本，外推会把层内比例当总体比例。**已裁定 2026-10-22** |
| **D-C11** ✅ | 校准闸门挂在哪（每次 push vs 每次 freeze） | **冻结时闸门**：`gate` 在 `freeze` 之后跑，**不接按 push 的 CI**；将来远端复算接的也是 `gate` | 快照的 pred 侧取自**冻结行**、不由当前代码重算 ⇒ 同一版对同一版恒为净改善 0 ⇒ 按 push 的门永远红，必然被绕过，而绕过过的门比没有门更糟。**已裁定 2026-10-23**（owner）|

每条决策阻塞哪些任务、哪些任务可以不等裁定先开工，见 [§9 任务执行计划](#9-任务执行计划)。

---

## 3. 分歧卡（DisagreementRecord）

### 3.1 契约

一条 `(golden_id, dimension)` 一条记录。**只读派生，不回写冻结档**。

| 字段组 | 字段 | 说明 |
|---|---|---|
| 身份 | `golden_id` / `golden_version` / `split` / `dimension` | `split ∈ {calibration, regression, holdout}`；`dimension ∈ d2..d10` |
| **条款** | **`rubric_ref`** | 唯一必填的条款号（`D-3.5` / `G-7` / `B-3`）。**分诊、改版、回归、抽样全按它聚**——`dimension` 太粗（一个 dim 有 5 档）|
| 金标侧 | `gold_value` / `gold_anchor` / `gold_note` | `gold_anchor` = 金标自己引用的锚点（通常 == `rubric_ref`）|
| 预测侧 | `pred_value` / `pred_reasons` / `pred_evidence` / `pred_provider` | `pred_reasons` = scorer `reasons` 原文；`pred_provider` = `code:…` 或 `llm:<model>/<prompt_version>` |
| 差异 | `delta_kind` | `label_mismatch`（字面不等）/ `eq_class_mismatch`（等价类也不等）/ `tier_gap`（软维度档差 > tolerance）|
| 分诊 | `bucket` / `responsibility` / `triage_by` / `triage_at` / `triage_evidence` | `triage_evidence` **必填**：为什么不是别的桶 |
| **抽样** | `stratum` / `sample_weight` | 只有进了人工抽样的行才填；`stratum` 见 [§5.7](#57-人工抽样按分歧类型分层v13-新增) |
| 处置 | `action` / `change_ref` / `status` / `verify_run` / `net_delta` | `change_ref` = commit / rubric 版本 / 变更记录行；`net_delta` = `{fixed, regressed}` |
| **动机链** | `change_kind` / `expected_effect` | 修改类型（新增 / 修改表述 / 废弃 / 调锚点分档 / **调权重**）+ **可证伪**的预期效果（"`D-3.5` 分歧 12 → ≤4"）。写进 rubric 变更记录（[§5.8](#58-动机链三个必填栏v13-新增)）|

### 3.2 `delta_kind` 为什么必须拆三态

"字面不等"与"语义不等"是两种病：

- `label_mismatch`：`subscription_wall` vs `paywall` —— 等价类相同，D-10 **不算错**；但 rubic 细分可能仍需对齐；
- `eq_class_mismatch`：`PAYWALL` vs `UNKNOWN` —— D-10 **算错**，是真分歧；
- `tier_gap`：软维度 1–5 档差 —— 与硬分类不同分母，不能混进同一个比率。

现在 `agreement.json` 把这些混在一个分母里，**分诊第一步就该拆开**。

### 3.3 状态机

```
open ──分诊──> assigned ──改动──> fixed_pending_verify ──重跑──> verified
  │            │                                            └────> reopened（净改善 ≤0）
  │            └──> accepted_exception ──到期──> open
  └──> escalated（同桶连续两轮不清零 → systematic）
```

`accepted_exception` 是**状态不是桶**。混进桶表就会变成万能垃圾桶。

---

## 4. 六桶归属：封闭集与前置事实

| bucket | 前置事实（不成立就不能贴） | 责任方 | 谁能判 |
|---|---|---|---|
| `evaluator_logic` | 按 rubric 明写条款应得另一个值，金标与准则均无歧义 | 代码判据 | 人（低门槛，可由 agent 预填建议值）|
| `evaluator_prompt` | LLM judge 输出与锚点冲突：该给 1 档给了 5 档 / 引用观察外来源 / evidence 无锚 | prompt · few-shot | 人 |
| `rubric_ambiguous` | 两条条款对同一情形给出不同结论，或条款没定义该情形 | **rubric** | **只能人** |
| `rubric_missing` | 准则完全没覆盖这种形态（新分支 / 新页面类型）| **rubric** | **只能人** |
| `gold_defect` | 人工标注与 rubric 明写条款冲突（标注员读错尺子）| **金标** | **只能人 + 走修正窗口** |
| `evidence_missing` | 存档不足以判（采集降级 / 裁剪 / 无观察）| 采集层 | 人（可由 `degraded` 字段预填）|

**fail-closed**：不认识的情形 → 留空 → `open`，**不贴桶**。与判据⑤ 的 `?` 同款（[07-人工复核手册 §2.3](../07-人工复核与判读手册.md)）。

**桶表不许长到第 7 项**（D-C2）。真要加，先裁掉一个。

> **v1.9 已落**（[C-05](#c-05-实施结果2026-10-23分支-featcalibration-loop)）：这张表在 `evaluation/calibration/buckets.py` 里——
> `BUCKETS` 顺序即上表顺序、`MAX_BUCKETS = 6` 在 import 期封顶、前置事实落 `BUCKET_PRECONDITION`、
> 「责任方」落 `BUCKET_OWNER`、「谁能判」由 `HUMAN_ONLY = {b for b in BUCKETS if b.startswith(("rubric_","gold_"))}`
> **按前缀推导**（不手抄第二份名单，否则加桶时会漂）。「可由 agent 预填建议值」这一列**不是权限**，
> 是 C-06 的预填落点（卡上的 `suggested_*`）——分诊档本身对 agent 身份一律拒写。

---

## 5. 两条回路：证据门槛不对称

### 5.1 内环 · 评估器回路（bucket ∈ {logic, prompt}）

改动后在同一版 calibration 上重跑，三条硬验收：

| # | 判据 | 说明 |
|---|---|---|
| 1 | **净改善 > 0** | `修复条数 − 反转条数 > 0`。不是"改了多少条" |
| 2 | **无类别回归** | per_class 任一 F1 下降超 tolerance 即驳回。总数上升但 `PAYWALL` recall 掉了不算改善 |
| 3 | **同桶清零检验** | 改完后**该桶分歧必须归零**。不归零 ⇒ 假设错了，比没改更危险 |

第 1、3 条把 H1-α 的人肉推演（修 4 / 伤 6 / 净 −2 / 撤回）变成机械判定。

### 5.2 外环 · rubric 回路（bucket ∈ {ambiguous, missing}）

四条强制，缺一条不许合：

| # | 要求 | 落点 |
|---|---|---|
| 1 | **发新版本**（v1.3…），旧版注释保留 | rubric 文末变更记录 |
| 2 | **责任区分声明**：必须写"准则是哪种情形下判不了"，**禁止**以"评估器做不到"为唯一理由放宽 | 变更记录必填栏 |
| 3 | **重打 dry-run**：新 rubric 下重打受影响 gold 行，**不覆盖**冻结档，先报"多少行会变"；变了的走修正窗口 → 新冻结版本 | `builder freeze --version N+1` |
| 4 | **连带改 prompt**：rubric 某档语义变了，对应 judge 锚点必须同批改 | contract test（见 §7）|

### 5.3 为什么门槛必须不对称

**评估器改动有独立证据源，rubric 改动没有。**

- 评估器改完在 calibration 上重跑，净改善是客观的；
- "这条准则该不该这么写"只能由人裁——把 rubric 变更放进自动化回路，等于让被测量者参与修订尺子。

这就是[铁律 4「人推开环」](11-外部方案对账.md)的本意。写成代码约束就是：`triage_by` 在 `rubric_*` / `gold_*` 上必须是人类 id（D-C3）。

### 5.4 量化触发（v1.3 新增）

回路不能靠"人翻一遍差异然后决定改哪"。改成：**命令先出读数 → 读数越线 → 才开一张卡**。

先钉一个结构事实：**三方不是两两都能比**。

| 对比 | 有意义的条款 | 现状 |
|---|---|---|
| 代码 ↔ 人工 | `D-2` / `D-4`（检测部分）/ `D-5` / `D-6` / `D-10` | **有**：`agreement` 口径 C（D-10 黄金集一致率）|
| LLM ↔ 人工 | `D-3` / `D-4`（语义部分）/ `D-9` | **半有**：`cohen_kappa` / `linear_weighted_kappa` 已在 [`builder.py:174/200`](../../evaluation/golden/builder.py)，但只被 `consistency`（test-retest 自相关）用；judge-vs-human 未接，且 D-3/D-9 的逐行 pred 不落黄金集行 |
| 代码 ↔ LLM | **只有 `D-4`**（唯一双评分者条款；`D-10` 是代码断言，没有 LLM 侧）| **算不出**：两侧 pred 从不同时落盘（overlay-scout 只写代码维度，d3/d9 走 `evaluation.cli --include-llm-judge` 出批内产物）|

⇒ **「代码评估与 LLM 语义评估的分歧率」只能在 `D-4` 上定义**。其余条款要说分歧，只能跟人工比。硬推广到全部维度，会造出一个分母不存在的指标。

四类信号：

| # | 信号 | 读数 | 阈值 | 越线后 |
|---|---|---|---|---|
| T-1 | 两评分者分歧率（条款级）| 需 C-15 快照 + C-16 聚合 | 单条款 `eq_class_mismatch` ≥ 10% **且 n ≥ 20**（暂定，先出读数）| 开 `evaluator_prompt` / `rubric_ambiguous` 预填卡 |
| T-2 | judge ↔ 人工一致性 | `cohen_kappa`（硬）/ `linear_weighted_kappa`（软），需 C-16 接通 judge-vs-human | 上岗线沿用：硬 ≥0.70 / 软 ≥0.50；**下降** = 相邻两版 Δ ≤ −0.05 | 触发 [§5.6](#56-回归集黄金校准集v13-新增) 回归检查 |
| T-3 | 理由含糊率（按条款）| LLM 侧 evidence **不含观察原文片段**的比例 | 单条款 ≥ 30% | `evaluator_prompt`（few-shot 缺干净实例）|
| T-4 | 评分分布退化 | 按条款的取值分布 | 众数占比 ≥ 80% 或不同取值 ≤ 2 | `rubric_ambiguous`（条款不可区分）|

两条纪律：

- **读数先于阈值**。新线的第一批只出读数、不出"合格/不合格"（沿用黄金集软维度"首次跑只发 README 不准出数字"的处置）；**定线只改阈值表的 `status`（`只出读数` → `已定线`），不进 rubric 变更记录**（D-C6 裁定；D-C9 原写「定线走 rubric 级变更」那一半**已作废**）。
- **触发 ≠ 归因**。越线只说明"该看一眼"，改哪一侧仍由 [§4](#4-六桶归属封闭集与前置事实) 判。

> **已落（v2.2，C-16）**：机械版 = `evaluation/calibration/triggers.py`（读数全部从 C-15
> 快照派生）。三条落法：**`judged` 恒为 `None`**——四条信号里只有 T-2 有已定的
> 上岗线，其余三条连「按候选线会不会越」都只标不判（D-C9）；
> **`ungraded` 不进分母、`n = 0` 的条款不出读数**，`coverage` 单列「一条都判不了」
> 的条款（`0 条 ≠ 0`，避坑指南 §8.22）；**一条都测不到就退 2**，退出码里
> **没有 1**——本命令没有「合格」这个结论可给。
> **今天的真实读数**：T-1 在 D-10 上 = 33/128 = 25.8%（按候选线会越线且 n 够）；
> T-4 在 D-10 上**未退化**（pred 众数 `unresolved` 37.5% / 13 个取值）；
> **T-2 / T-3 测不成**（judge 逐行 pred 不落盘 + 软维度金标 0 条，两条独立理由）；
> ⚠️ **D-2 / D-4 / D-5 / D-6 四条款一条都判不了**（全 `ungraded`）——
> 这把「先填软维度金标」从散活变成了回路的硬前置。

### 5.5 修改粒度：条款级（v1.3 新增）

rubric 里**已经有稳定的条款号**（`G-1..G-7` / `D-2..D-10` 及各档锚点 `D-3.5` / `B-1..B-4`），不用新造体系。缺的是**让代码与产物引用它**：

- `rubric_version.DIM_CLAUSES`（dimension + 档位 → 条款号）做唯一真源；prompt 锚点表（[§7](#7-把-prompt-从散文升级成锚点表)）与分歧卡 `rubric_ref` 都从它投影，**不许各写一份**。
- 三条硬规矩：
  1. **一次改版只动列出的条款**——未列条款的分数必须**逐位不变**（机械 diff，CI 红即驳回）；
  2. **禁止整份重写**——整份重写让历史分数失去可比性，漂移也不可控；
  3. **只增不删**（现有治理规则）⇒「删除」的实现是标 `deprecated` + 保留行，不是物理删。
- **影响面必须写进改版说明**：改 `D-3.5` ⇒ 旧金标在 `D-3.5` 上**不可比**（rubric 自己在 v1.0→v1.1 记过这条教训）。⇒ 回归判据只在**未变条款**上算，变了的条款走 dry-run 重打 + 修正窗口（[§5.2](#52-外环--rubric-回路bucket--ambiguous-missing) 第 3 条）。
- ⚠️ **「调权重」当前是空类**：本 rubric 没有权重体系（`G-7` 明写批次级与样本级不可相加）。列进 enum 是为了将来引入权重表时不必改契约，**不是现在有东西可调**。

> **已落（v2.1，C-18）**：规矩 1 的机械版 = `rubric_changelog audit --before N --after N`
> ——白名单取自变更记录新加的 `条款` 列，逐条款比两版快照的 `(golden_id, dimension)`；
> 未列条款一动即退 1。规矩 3（只增不删）落在 `变更类型` 的 `废弃` 上：登记一行
> 而不是把旧行抹掉，历史行**只增不删**。规矩 2（禁整份重写）没有做成禁令，做成了
> **显式退化**：首版写通配 `*`，一旦出现通配守卫就断言不了任何东西，报告里
> `wildcard=True` 如实印出来——**假装它在守比不守更危险**。

### 5.6 回归集（黄金校准集，v1.3 新增）

| 集 | 干什么 | 谁能改 | 频率 |
|---|---|---|---|
| calibration（调参池）| 内环迭代：改判据 / prompt 后在这里看净改善 | 随改动重跑 | 每次改动 |
| **regression（回归集）** | **门禁**：每次改版重跑，出 `fixed / regressed / unchanged` 三类计数 | **金标冻结**；改标签 = 新版本 + 人裁（同 `gold_defect` 窗口）| 每次改动 |
| holdout | 最终评估 | 冻结 | 每个 rubric 版本**一次**（D-C5）|

**判定**：`regressed > 0` → 驳回（回滚或修订）；`net = fixed − regressed > 0` 才可合。

**这一条是防「rubric 越改越偏」的主护栏，排在分诊之前做**——分诊告诉你该改哪儿，回归集告诉你改坏了没有；没有后者，前者的每次改动都不可证伪。

两个前提，缺一这条就做不成：

1. **全量逐行 pred 快照**。只有分歧卡不够——"上一轮对、这一轮错"的那一行，**上一轮根本不在卡里**。快照按 `(v{N}, rubric_version, evaluator_version)` 落盘（C-15）。
2. **条款级切分**（§5.5）：判据只算未变条款。

⚠️ **回归集会被调参吃掉**：反复看同一批 60 条、按其逐条结果改 prompt，等于把回归集变成第二个调参池。处置：回归集**只向 owner 出逐条明细**，改判据的人只看汇总计数（`fixed/regressed` 各几条、落在哪些条款），不看"哪几条错了"。

### 5.7 人工抽样：按分歧类型分层（v1.3 新增）

**按分歧类型分层抽样，不随机抽**——随机抽样把有限的人工标注量摊薄在最不需要看的地方。

| 层 | 入层条件（机械可算）| 想暴露什么 |
|---|---|---|
| **S1** 两评分者打架 | D-4 上代码与 LLM 不同，或 LLM 与人工不同等价类 | 判据 / prompt 谁离得远 |
| **S2** 理由含糊 | evidence **不含观察原文片段**，或落在"泛泛描述"档（`D-3` 锚点 4 / `D-9` `vague`）| 条款是否给了可执行判据 |
| **S3** 中间段 | 软维度 = 3 分，或硬分类 `ambiguous` | 最难判的区间——rubric 的边界往往在这里漏 |
| **S0** 对照 | 两侧一致且落在两端 | **只看分歧会高估问题**；S0 用来估「一致但都错」的假阴性 |

三条纪律：

1. **固定 seed + 固定配额**（每轮 40 条：S1/S2/S3 = 10/10/10，S0 = 10），抽样可复现；
2. `stratum` 与 `sample_weight` 落进标注档；**分层样本上的 kappa 只作层内读数，不外推总体**（D-C10）；
3. **抽样是"发现新缺陷"，分诊是"处理已落盘分歧"**——两件事分开记账，混在一起会让人以为那 73 条都靠抽样发现。

⚠️ **不能用「LLM 自评 confidence」当置信度层**：`d3_scorer` / `d9_scorer` 的 `confidence` 字段是**写死的常量**（`CONFIDENCE_JUDGED=0.8` / `CONFIDENCE_NONE`），模块 docstring 明写不采信模型自报值（自报普遍虚高，须经验校准后才可用于阈值）。要"低置信层"，只能用**可观测代理**：证据是否引用观察原文、是否 fail-closed。

> **已落（v2.2，C-19）**：机械版 = `evaluation/calibration/sampling.py`，入层判据
> 全部机械可算（`STRATA` 的 `why` 就是判据原文，人不参与分层——一条判据要靠人读
> 一遍才知道算不算，那它就不是判据，是意见）。**四条实现口径**：
> ① **配额缺口如实报不补**（`short_by`，不跨层借人）；
> ② **可复现靠「固定种子 + 稳定排序 + SHA-256 定序」而不是 RNG**——
> `random.sample` 跨 Python 版本可能换结果，冻结档要的是换台机器跑出同一份；
> ③ **一条只入一层**（优先级 S1 → S3 → S2 → S0），层间重叠**写进报告**
> `overlap`——重叠是事实，藏起来就成了「四层互斥」这个假前提；
> ④ **`sample.jsonl` 不覆盖 `worksheet.jsonl`**（判读**谁**的清单 vs 分歧卡的
> 判读表，两样东西）。
> **今天的真实读数**：S1 总体 **33** / S0 总体 **32**（各取满 10），
> **S2 与 S3 总体都是 0 ⇒ 实抽 20 条、缺 20**。⚠️ S3 缺是因为软维度全
> `ungraded` **且 `ambiguous` 不在 `EQUIV_CLASS` 里**（两条臂都是空的，设计如此——
> fail-closed：等价类表没有它就不能假装能判它）；S2 缺是因为 judge 逐行理由不落盘。
> **抽不满是读数不是 bug**：它说明人工判读只看得见 S0/S1——**回路这半边看不见
> S2/S3 的质量**。

### 5.8 动机链：三个必填栏（v1.3 新增）

rubric 变更记录现在是四列（版本 / 日期 / 变更内容 / 修订人），补三列：

| 列 | 内容 | 为什么 |
|---|---|---|
| `disagreement_id` | 触发这次修改的分歧卡 id（**≥1 条**）| 现在写"依据：用户裁定"，查不到是哪批数据逼出来的 |
| `change_kind` | 新增 / 修改表述 / 废弃 / 调锚点分档 / 调权重（空类，见 §5.5）| 排查"某类数据评分突变"时，「修改表述」与「调锚点分档」的路径完全不同 |
| `expected_effect` | **可证伪**的预期（"`D-3.5` 分歧 12 → ≤4"）| 没有预期就只能看总数涨没涨，无法判断这次改版是否达到目的 |

`expected_effect` 由回归集读数核对：达到 → 关；未达到但没弄坏 → 记 `accepted_exception`（[§3.3](#33-状态机)）；未达到且有弄坏 → 回滚。

> **已落（v2.1，C-12 + C-18）**：三栏是**同一张表的四列**，最终落地形态在
> `docs/设计方案/在线视频场景rubric.md` 的变更记录表里（列名用中文：
> `条款` / `变更类型` / `预期效果` / `分歧卡` / `责任区分声明`），
> 由 `evaluation/scorers/rubric_changelog.py` 解析校验。两处与本节原设想不同：
> ① 多了一列 **`条款`**（`D-*` / `G-*` / `B-*` / 派生根 / `*`）——没有它，
> 「未列条款逐位不变」就没有白名单；② **追溯线前向**：`TRACEABLE_SINCE = "1.3"`
> 之前的老行允许 `—`（那几版本来就没有卡），不补假卡号。

---

## 6. 防放水的五条硬规矩

| # | 规矩 | 现状 |
|---|---|---|
| 1 | **金标不回改**：`gold_defect` 只能开新版本 + 修正窗口，旧版留档 | ✅ 已有（铁律 2）|
| 2 | **等价类合并 = 放水高危**：改 `EQUIV_CLASS` 使 D-10 上升的，必须单列并说明"合并后不可见的细分"，**代码侧注记**（D-C4 裁定：**不需 owner 签**）| ✅ **已落（C-13，2026-10-23）**：`alignment.EQUIV_CLASS_NOTES` 每个**多标签**类一张注记（规范标签 / 被并标签 / 丢了什么细分 / 为什么并）+ `invisible_details()`（清单）+ `validate_merge_notes()`（与表**逐标签**比对，漏注记 / 偷合并 ⇒ 红）。**没有签名字段，但有机器核对** |
| 3 | **阈值不随数字走**：`D10_THRESHOLD` / 软维度阈值不许因"差一点"下调 | ✅ **已落（C-14，2026-10-23）**：唯一真源 `evaluation/scorers/thresholds.py` + 各处 `thresholds.value("<name>")` 取值 + 契约测试盯「表不腐 / 不留字面量 / 与 rubric 版本互不 import」。**调阈值不升 rubric 版本**（D-C6 裁定）——防放水的力气花在**表和测试**上，不花在签名上 |
| 4 | **holdout 真封存**：内环只在 calibration 迭代；每个 rubric 版本 holdout 只跑一次，记录 `holdout_runs` 与 gap（>5pp 回调）| ❌ **当前不成立**：`cmd_agreement` 把 calibration + holdout 一起算进 D-10 分母（[`builder.py:1019-1038`](../../evaluation/golden/builder.py)）；只有软维度才 calibration 优先（[:313](../../evaluation/golden/builder.py)）|
| 5 | **回归集不作调参依据**：回归集金标冻结，改它 = 新版本 + 人裁；改判据的人**只看汇总计数、不看逐条明细**（[§5.6](#56-回归集黄金校准集v13-新增)）| ❌ **空白**——回归集本身还没有（C-17）|

第 2 条值得单独强调：**最省力的涨点方式永远是合并等价类**——它不需要改任何判据，只需要让两个标签"看起来一样"。而这恰好是让尺子变钝。现在这条路上没有任何路标。

---

## 7. 把 prompt 从散文升级成锚点表

现状：`D3_SYSTEM_PROMPT`（[`d3_scorer.py:231`](../../evaluation/scorers/d3_scorer.py)）是一整段散文，锚点语义散在 docstring 与 `reasons` 字符串（"rubric §D-3 锚点 2"是**手写文本**，改 rubric 时没人会去同步）。`D9_FEW_SHOT_EXAMPLES`（[:399](../../evaluation/scorers/d9_scorer.py)）的六条示例也没有标注"教的是哪一档"。

建议形态：

```python
RUBRIC_VERSION = "1.2"

#: 每档一条，来源标 rubric 条款号。prompt 由这里渲染，不许另写一份散文。
D3_ANCHORS: dict[int, tuple[str, str]] = {
    5: ("D-3.5", "category 判对 + evidence 引用观察具体短语"),
    4: ("D-3.4", "category 判对 + evidence 泛泛描述"),
    3: ("D-3.3", "category == ambiguous（判「选择进入验证」）"),
    2: ("D-3.2", "category 不在契约 5 类；playable 但 subtype 飘出 4 类"),
    1: ("D-3.1", "观察充分 + LLM 没给答案（fail-closed + 该站本可判）"),
}

#: few-shot 每条挂 verdict + 锚点，人工挑例时能回答"这条教的是哪一档"。
D9_FEW_SHOT_EXAMPLES: tuple[dict, ...] = (
    {"verdict": "anchored", "anchor": "D-9.5", ...},
    ...
)
```

收益（纯结构改造，不重跑数据）：

- 改 rubric 某档 → 对应 anchor 不改 → **contract test 红**，drift 无处藏；
- few-shot 池的覆盖度可枚举（哪一档还没有干净实例）；
- `pred_provider` 里的 `prompt_version` 可以用锚点表的 hash，分歧卡由此能回答"这批分歧是旧 prompt 还是新 prompt 判的"。

---

## 8. 落点清单（遵循 v2 新树纪律）

| 文件 | 动作 | 说明 |
|---|---|---|
| `evaluation/calibration/__init__.py` | 新增 | 包说明：只读派生，不回写冻结档 |
| `evaluation/calibration/disagreement.py` | 新增 | 从 `v{N}/*.jsonl` + `candidates.jsonl` 派生分歧卡；**只读** |
| `evaluation/calibration/buckets.py` | 新增（v1.9，已落）| 6 桶封闭集 + `BUCKET_PRECONDITION` + `BUCKET_OWNER`；未登记桶 `KeyError`；`MAX_BUCKETS` 在 **import 期**封顶 |
| `evaluation/calibration/triage.py` | 新增（v1.9，已落）| 读人填分诊档；**`validate_rows` 是唯一写入口**——校验 `triage_by` 非 agent（rubric/gold 桶）+ 判据必填 + 维度/桶/状态合法 |
| `evaluation/calibration/verify.py` | 新增（v1.7，已落）| 两轮快照差分 → **四条判据**（净改善 / 无类回归 / 同桶清零 / 回归集 `regressed == 0`）+ 回归集三类计数；退出码 **0 通过 / 1 驳回 / 2 输入不可比** |
| `evaluation/calibration/snapshot.py` | 新增（v1.3）| **全量逐行 pred 快照**，按 `(v{N}, rubric_version, evaluator_version)` 落盘——回归判定与 T-1/T-4 读数的共同底座 |
| `evaluation/calibration/triggers.py` | 新增（v2.2，已落）| **触发读数唯一出处**（C-16）：T-1～T-4 全从 C-15 快照派生 · `line_set` 区分「已有线」与「候选线」 · `judged` **恒为 `None`**（D-C9 读数先行）· `ungraded` 不进分母 + `coverage` 单列「一条都判不了」的条款 · `load_judge_pairs` 缺 `golden_id` **直接抛不猜键** · CLI **只有 0 / 2，没有 1** |
| `evaluation/calibration/sampling.py` | 新增（v2.2，已落）| **人工判读抽样唯一出处**（C-19）：S0–S3 入层判据全部机械可算（`STRATA.why`）· 配额 10×4 + `short_by` **缺口不补** · `_order_key` 用 **SHA-256 定序 + 固定种子（不用 RNG）**· 一条只入一层、层间重叠写进 `overlap` · `sample_weight = N_h/n_h`（**不外推总体**，D-C10）· 落 `v{N}/sample.jsonl`，**不覆盖 `worksheet.jsonl`** |
| `evaluation/calibration/worklist.py` | 新增（v2.0，已落；**v2.4 增 `scene` 栏 + `clauses.md`**）| **首轮分诊工作表**：卡 × 冻结行 → 机器可确认的事实 + **至多一条**建议桶；行里**没有 `bucket` 键**；`PREFILL_RULES` 是**唯一真源**（import 期 `check_rules` 拒掉指向 `HUMAN_ONLY` 的规则）；工作表**已存在不覆盖**（人填到一半被冲掉是这里最贵的损坏）。**v2.4**：`build` 把**现场读数写进工作表每行的 `scene` 栏**（`build_scenes` / `build_worksheet(rows, scenes)`），并另落**尺子** `v{N}/clauses.md`；`_rows_by_id` 是**唯一一处**「卡 ↔ 冻结行」配对逻辑（两处各写一遍 ⇒ 现场配到别的行上而不报错） |
| `evaluation/calibration/evidence.py` | 新增（v2.3，已落；**v2.4 增 `scene_summary` / `clauses_markdown`**）| **读现场**（`worklist show` 的那一半）：`parse_clauses` / `load_clauses`（条款原文**从 rubric 文档现读**，不抄第二份；**解析不出条款就抛**，不返回空表）· `locate_sample`（**复用 builder 的同一条查找路径**：`split_archive` + 按 url 匹配）· `observation_digest`（**P1 / P2 两种字段形状都读**，只认一种会把另一种读成空值）· `card_digest` / `render_card` / `render_compact`。**只读**；定位不到**说定位不到**（并存候选），**不返回空读数**。**v2.4**：`scene_summary`（工作表 `scene` 栏的来源：`status` **四态** `located` / `located_no_steps` / `not_found` / `not_read`——「没找到」与「没去读」归一个布尔就分不清是「存档里没有」还是「压根没查」；`body_from` 标出正文来自第几步，因为正文常来自**更早**的一步）· `clauses_markdown`（把条款原文拼成**一把**尺子——尺子是同一把，每行各带一份会让同一个文件长 N 倍） |
| `evaluation/scorers/rubric_version.py` | 新增（**v2.7 扩**）| `RUBRIC_VERSION` + 各 judge 的 `ANCHORS` 表 + **`DIM_CLAUSES`**（dimension + 档位 → 条款号，唯一真源）。**v2.7**：加 `ANCHOR_TABLES`（逐档释义）+ `anchors_drift()`（现读 rubric 文档比对）+ `render_anchor_block()`（prompt 从表渲染）+ `code_scorer_version()` / `scorer_fingerprint()` / `llm_part()`（**内容**指纹，不是 git commit——commit 不含工作树未提交的改动）|
| `evaluation/scorers/thresholds.py` | 新增（v2.7，C-14）| **阈值唯一真源**：11 条，每条带值 / 定线状态 / 依据 / 使用处；`value()` 对未登记的名字 **fail-closed 抛 `KeyError`**（不返回默认值——「差一点点调线」正是从一个默认 0.0 开始的）|
| `evaluation/golden/alignment.py` | 改（v2.7，C-13）| 加 `EQUIV_CLASS_NOTES`（每个**多标签**类一张注记）+ `invisible_details()`（**「合并后不可见细分」清单**）+ `validate_merge_notes()`（与表**逐标签**比对）。**无签名字段**（D-C4 裁定）|
| `evaluation/scorers/d3_scorer.py` / `d9_scorer.py` | 改（**v2.7 已落**）| prompt 由锚点表渲染；few-shot 挂 `anchor`；D-9 六条 verdict 措辞从表渲染（原先是手抄）|
| `evaluation/golden/splits.py` | 新增（v1.6，已落）| 三档词汇唯一真源：档名 / 文件名 / 头条档位 / 回归集条数与种子 |
| `evaluation/golden/builder.py` | 改 | `agreement` 增 `--emit-disagreements`；`freeze` 增 `--from-version` / `--regression`；`manifest` / `agreement.json` 增 `rubric_version` / `evaluator_version` / `holdout_runs`；D-10 分母改为 calibration（holdout 单独出数）|
| `evaluation/golden/data/triage/` | 新增目录（v1.9，已落）| 人填分诊档入版本控制（D-C1）；`README.md` 记字段表与六桶 |
| `evaluation/scorers/rubric_changelog.py` | 新增（v2.1，已落）| **变更记录唯一解析与校验入口**（C-12 + C-18）：解析 rubric 里的九列表（**不落第二份 JSONL**）· `validate_entries` 六组规则 · `clause_invariance` 逐条款 diff 守卫 · `measurable`（受检条款没配上 = 测不成 → 退 2）· CLI `check` / `audit` |
| `docs/设计方案/在线视频场景rubric.md` | 改（v2.1）| 变更记录表 **4 列 → 9 列** + 列契约一节（逐列判据 / 前向追溯线 / 审计命令）。⚠️ **这是改 rubric 文档本身**，所以升 `RUBRIC_VERSION` 时必须同步加一行 |
| `tests/unit/test_rubric_changelog.py` | 新增（v2.1，已落，61 条）| 解析 / 列契约 / 条款词表 / 变更类型封闭集 / 动机链三态 / 借口驳回 / 区间语义 / diff 守卫（含通配、未配对、金标动、空跑）/ 卡号核验（含**一条都没引时不印 ✓**）/ CLI 三态 |
| `tests/contract/test_rubric_traceability.py` | 新增（v1.9，部分已落：③④）| ①每条 rubric 变更含 `disagreement_id` + `change_kind` + `expected_effect`（[§5.8](#58-动机链三个必填栏v13-新增)）②每个 prompt 锚点可映射到 `DIM_CLAUSES` ③桶表 ≤ 6 ④`triage_by` 权限 ⑤**改版后未列条款的分数逐位不变**（[§5.5](#55-修改粒度条款级v13-新增)）。**①⑤ 已落（v2.1，实为 20 条）**；**② 已全落（v2.7，C-10）**——除了「维度 → 条款」，现在还逐档核「锚点表 ↔ rubric 文档」「prompt 里那段是渲染来的」「few-shot 每条挂着表里有的锚点」「D-9 六条 verdict 措辞覆盖 rubric 档位释义」 |
| `tests/contract/test_calibration_provenance.py` | 新增（v2.7，C-11 / C-13 / C-14）| 归因面的不变式：pred 出处带版本、卡带尺子面指纹、**版本没记到时可比性判不出来（不猜）**；等价类合并记账与表逐标签对齐；阈值表唯一真源（每条都被用到 / 使用处不留字面量 / 与 rubric 版本互不 import）|
| `tests/unit/test_golden_three_split.py` | 新增（v1.6，已落）| 三档词汇 / 回归切片 / 继承式重冻结 / 头条口径 / 指纹守卫 |
| `tests/unit/test_calibration_verify.py` | 新增（v1.7，已落，29 条）| H1-α 回放必驳回 / 总数涨而单类塌 / 净改善为 0 / 回归集反转 / 判不了 ≠ 修好 / 同桶清零三种结局 / 换档·重切·键重复·schema 不同一律退 2 |
| `tests/unit/test_calibration_triage.py` | 新增（v1.9，已落，33 条）| 六桶封闭集 / 前置事实 / 身份识别（含 `agent:` 前缀与 `agent-smith` 反例）/ 字段校验 / 读写幂等 / CLI 三态 / `verify.analyze` 再校验 |
| `tests/unit/test_calibration_evidence.py` | 新增（v2.3，已落 25 条；**v2.4 增 6 条**）| 条款块**不串味**（D-10 不吞「## 批次级断言表」）· 条款文档读不到**就抛**（不返回空表）· P1 / P2 两种观察形状都读得出 · 定位的六个分支（命中 / url 对不上→列候选 / 存档不存在 / 负样本池 / 不认识的 `source_kind` / 缺字段）· 动作与观察**按下标配对** · 渲染的「读不到现场」与「条款不可用」标记 · CLI 三态 + **v7 真数据回归**（33 张卡，定位不到的必须明说）。**v2.4**：`scene_summary` 四态 + `body_from` · `clauses_markdown`（查不到的条款**明说不可用**）· `build_scenes`（按 `golden_id` 配对；配不上时明说**版本错配**） |
| `tests/unit/test_calibration_*` | 新增 | 净改善计算 / 同桶清零 / 分诊 fail-closed |

**不新增顶层 CLI**：挂在 `evaluation.golden.builder` 与 `evaluation.cli` 的既有命名空间下（沿用 [08 号方案 §3.1](08-阶段4七层定位与黄金数据集rubric门禁方案.md) 的取舍）。
⚠️ **一处偏离（v1.7）**：`verify` 是**模块级** CLI（`python -m trajectory_pipeline.evaluation.calibration.verify`），与 `ls_bridge` / `scout` 同形——**没挂进 `builder` 的 subparser**。理由是这个命令要能返回 **1（驳回）与 2（测不成）两个不同的非零码**，而 `builder.main` 把一切错误统一吃成 2；挂进去就得为了一个码改 builder 的错误语义，代价比一个模块入口大。

---

## 9. 任务执行计划

### 9.1 排序依据与阶段映射

排序依据只有一条：**不先做这条，后面每条都无从验证**（不是工作量，也不是紧迫感）。

| 阶段 | 覆盖任务 | 一句话 |
|---|---|---|
| **R0** | C-01 ✅ · C-02 ✅ · C-03 ✅ · C-04 ✅ · C-15 ✅ | 让数字可归因 |
| **R0.5**（v1.3 提前）| **C-17 ✅ · C-08 ✅ · C-09 ✅** | **防越改越偏——护栏先于分诊** |
| **R1** | **C-05 ✅ · C-06 ✅ · C-16 ✅ · C-19 ✅ · C-07 ✅** | 归属与抽样，给出投入方向。**v2.5 起 R1 全闭环**：分诊档 33/33 由人填完，归属分布第一次出真实比例 |
| **R2** | **C-10 ✅ · C-11 ✅ · C-18 ✅**（C-12 ✅ 也属这一段）| 锁住两侧不再漂（条款级）。**v2.7：R2 全闭环** |
| 贯穿 | **C-13 ✅ · C-14 ✅** | 治理散活，随时插。**v2.7：贯穿项也全闭环**（两条都随 D-C4 / D-C6 的裁定一起落）|

**R0.5 为什么被提前**（v1.3）：回归门是**唯一能在改动前就判"这次改坏了没有"**的机制。没有它，R1 的每次分诊改动都不可证伪——只能看总数涨没涨，而总数涨了可能是等价类合并（[§6](#6-防放水的五条硬规矩) 第 2 条）。它也不依赖六桶：只要**快照 + 冻结切片**两样。

### 9.2 P0 · 让数字可归因

| ID | 事项 | 依赖 | 验收 | 成本 | 需拍板 |
|---|---|---|---|---|---|
| **C-01** ✅ | `RUBRIC_VERSION` + `evaluator_version`（prompt hash）+ `holdout_runs` 写进 `manifest.json` / `agreement.json` | — | 每个 `v{N}/` 能回答"哪版尺子量的"；不破 `label_schema_version` | 0.5d | 否 |
| **C-02** ✅ | 分歧卡派生：`agreement --emit-disagreements`（**只读，不回写冻结档**）；卡上带 `rubric_ref`（投影自 `DIM_CLAUSES`）| C-01 | 现有 73 条 D-10 不一致逐条落盘，带 `delta_kind` 三态 + 条款号 | 1d | 否 |
| **C-03** ✅ | **基线对齐**：INC43 的 76.5% 投影是否转正 | — | 二选一：以 v6（74.7%）为准 / 写回 `code.branch` → freeze v7。**裁定：以 v6 为准**（76.5% 只作历史读数），v7 = v6 行原样 + 三档切分 | 0.5–1d；**无现成写回脚本**（`research/recollect/` 只有 `measure.py` / `visit.py`），要新写 | ✅ 已裁定 2026-10-22 |
| **C-04** ✅ | D-10 分母拆分：主数只算 calibration，holdout 旁置 | C-03 | 头条数字分母 289 → 188（站点级；191 是行数含 3 条轨迹），**必须同时保留旧口径读数**，否则历史对比断裂 | 0.5d | ✅ 已裁定（D-C5）|
| **C-15** ✅ | **全量逐行 pred 快照**：每冻结版本 × 每条款一行，`(v{N}, rubric_version, evaluator_version)` 落盘 | C-02 | 任一版本能回答"这一行上一版判了什么"；只读派生，不回写冻结档 | 0.5d | 否 |

**C-15 是 C-02 的补半条，不是附加项**：分歧卡只记"这一轮不一致"，而"上一轮对、这一轮错"的那一行**上一轮根本不在卡里**。没有全量快照，[§5.6](#56-回归集黄金校准集v13-新增) 的回归判定与 [§5.4](#54-量化触发v13-新增) 的 T-1/T-4 读数都做不了。

**C-03 为什么排这么前**：R1 分诊只能对**一个确定的基线**做。现在文档写 76.5%、冻结档是 74.7%，不对齐就分诊，等于对着来源不明的数字做归属。

#### C-01 实施结果（2026-10-22，分支 `feat/calibration-loop`）

**落点**：

| 文件 | 改动 |
|---|---|
| `evaluation/scorers/rubric_version.py`（新） | `RUBRIC_VERSION` · `documented_rubric_version()`（读 rubric 文档 `**版本**: vX`，解析不出抛 `ValueError`）· `evaluator_version()`（12 位 sha256） |
| `evaluation/golden/builder.py` | `cmd_freeze` 写 `rubric_version` / `evaluator_version` / `holdout_runs` 进 `manifest.json`，`versions.jsonl` 索引带 `rubric_version`；新增 `_load_manifest()`；`cmd_agreement` 打印「尺子」行，JSON 加 `rubric_version` / `evaluator_version` / `evaluator_version_at_freeze` / `code_branch_provenance` / `holdout_runs` / `rows_by_split` / `d10_denominator` |
| `tests/unit/test_rubric_version.py`（新，9 条） | 常量↔文档一致 · 全角冒号容忍 · 解析失败 fail-closed · 指纹稳定且 12 位 hex · 改 rubric 版本指纹必变 · freeze 三字段且不破 `label_schema_version` · agreement 新字段 · 老冻结档不拿当前值顶替 · 分母含 holdout 时告警 |

**三条口径（不是实现细节，是以后读报表的前提）**：

1. **老冻结档不补写**。v1–v6 的 `manifest.json` 没有这两个字段，`agreement` 就报「rubric 版本未记录（v6 及更早冻结档无此字段）」，JSON 给空串。拿当前值顶上去等于把新尺子的编号贴在旧数字旁边。
2. **指纹只覆盖 D-3 / D-9 的 prompt 状态**（`d3_prompt` / 类别 / 子类 / `d9_prompt` / 判定集 / few-shot）。代码评分器 `d2/d4/d5/d6` 的版本由 git commit 表征，**不给它们造假版本号**——假版本号比没有版本号更难查。
3. **D10 的 pred 侧不是当前评分器**。它是冻结行里采集时写下的 `code.branch`（JSON 用 `code_branch_provenance: "frozen_row"` 明示）。把 `evaluator_version` 当成 D10 的来源是这类报表最常见的误读。

**已知未做（留给后继）**：`holdout_runs` 现在恒为 `0`——封存纪律在 C-04（D-C5）落地前不成立。manifest 里 `0` 的意思是「没封过」，**不是**「封了 0 条」。

#### C-02 + C-15 实施结果（2026-10-22，分支 `feat/calibration-loop`）

两条一起落地，因为**卡是快照的视图**——先有全量快照，卡才筛得出来；分开做必然要写两遍同一套判据。

**落点**：

| 文件 | 改动 |
|---|---|
| `evaluation/calibration/__init__.py`（新包） | 说明分工：`snapshot.py` 是全量逐行预测（C-15），`disagreement.py` 是它的视图（C-02）；派生数据 → `output/pipeline/calibration/v{N}/`（不入库、可重建），人工分诊 → `evaluation/golden/data/triage/`（入库） |
| `evaluation/calibration/snapshot.py`（新） | `build_snapshot()` · `summarize_snapshot()`（by_verdict / by_split / by_dimension / `d10{n,agreed}`）· `write_snapshot()`（**无时间戳，重算逐字节相同**）· `load_snapshot()`；五态封闭集 `agree / detail_mismatch / disagree / tier_gap / ungraded`，`DIFF_VERDICTS` = 中间三态（差异态）；D10 走 `is_equivalent`（**未登记标签抛 `KeyError`，不吞**） |
| `evaluation/calibration/disagreement.py`（新） | `cards_from_snapshot()`（只取差异三态）· `summarize_cards()`（by_delta_kind / by_dimension / by_split）· `write_cards()` / `read_cards()`；`disagreement_id = v{N}:{golden_id}:{dimension}`；**分诊与处置槽位全空、`status="open"`** |
| `evaluation/scorers/rubric_version.py` | 加 `DIM_CLAUSES`（9 维 → 条款号，`d7→B-2` / `d8→B-4` 是批次级）· `TIERED_DIMS` · `clause_of()`（未知维 `KeyError`、档位越界 `ValueError`；非分档维忽略档位） |
| `evaluation/golden/builder.py` | `_emit_calibration_artifacts()` + `agreement --emit-disagreements [DIR]`（缺省 `output/pipeline/calibration`）；**无 `--version` 直接抛 `GoldenError`** |
| `tests/unit/test_calibration_snapshot.py`（新，17 条） | 见下 |

**v6 实测读数（2026-10-22，冻结版 v6 原地重算，不改冻结档）**：

```
口径 C：216/289 = 74.7%
快照 856 条：agree=215 / detail_mismatch=1 / disagree=73 / ungraded=567
分歧卡 74 张：eq_class_mismatch=73 / label_mismatch=1
  唯一 label_mismatch：E017__a2878b23__95b7b892  trailer_only → trailer_suspect
```

三条对得上，说明快照与 `cmd_agreement` **同分母同判据**（216 = d10 `agreed`，289 = `n`）。**`ungraded` 567 条不进分母**——它们是一侧没给分的维度，记 0 会凭空造错。

**混淆块前五**（分诊第一轮的直接输入）：`subscription_wall→unresolved 19` · `component_unverified→login_wall_or_blocked 11` · `success→unresolved 8` · `subscription_wall→login_wall_or_blocked 5` · `login_wall_or_blocked→unresolved 4`。

**四条口径**：

1. **`ungraded` 不是分歧**。五态里只有差异三态进卡；`None ≠ 0`，一侧缺分就记 `ungraded`，既不进分母也不进卡——否则「这门题没判」会被读成「判错了」。
2. **卡不含回归信息，快照才含**。上一轮对、这一轮错的行上一轮根本不在卡里；§5.6 的 `regressed == 0` 只能从快照算。
3. **`detail_mismatch` 与 `disagree` 分开记账**。前者是等价类内换标签（`broken` ↔ `unreachable_hard` 这类按 `SPLIT_CONTENT_VS_REACH` 刻意拆开的），后者是跨等价类，**混成一个数就等于放水**。
4. **`tier_gap` 在容差内也记幅度**。`verdict` 仍是 `agree`，但差距值留下——T-4（中间段讨论）要用它，事后补不回来。

**已知未做（留给后继）**：卡上的 `split` 目前恒为 `calibration`（v6 还没切回归集，C-17 落地后才出现 `regression`）；`stratum` / `sample_weight` 恒空（C-19 填）；`change_ref` / `net_delta` 恒空（C-08 验证回填）。

> ⚠️ **上面这两段已被 C-03 / C-04 / C-17 局部推翻**：从 v7 起，快照收三档、卡只从调参池出（回归集与持有集旁置）。原文保留是为了留着"当时以为卡上会出现 `regression`"这个判断——它错了，错在把"快照收什么"和"分诊吃哪一档"当成一回事。

#### C-03 + C-04 + C-17 实施结果（2026-10-22，分支 `feat/calibration-loop`）

三条合成**一次继承式重冻结**，因为 C-03/C-04/C-17 都要动冻结产物：分三次冻就是把同一件事冻三遍，`versions.jsonl` 里躺着三个只差口径的版本号。

**裁定**：C-03 走「以 v6 冻结档为准」（216/289 = 74.7%），INC43 的 76.5% 只作历史读数；D-C5 一并落下（持有集真封存）。

**落点**：

| 文件 | 改动 |
|---|---|
| `evaluation/golden/splits.py`（新） | 三档词汇的**唯一真源**：`SPLIT_CALIBRATION/REGRESSION/HOLDOUT` · `SPLITS` · `SPLIT_FILES` · `HEADLINE_SPLIT` · `SIDE_SPLITS` · `REGRESSION_SIZE=60` · `REGRESSION_SEED` · `split_of_file()`（认不出返回空串，**不猜**）|
| `evaluation/golden/builder.py` | `freeze --from-version N`（继承式重冻结：行原样带过来，不读 candidates/annotations）· `--regression K` · `--regression-seed`；`_regression_slice()`（站点级 + 按标签分层 + 确定性）· `_rows_fingerprint()`（整行 sha256 前 12 位，**剔 `__` 运行时标注**）；`cmd_agreement` 三档读取、头条只认调参池、旧口径同时输出、回归集指纹守卫 |
| `evaluation/calibration/snapshot.py` | `summarize_snapshot` 出 `d10`（头条档）/ `d10_by_split` / `d10_all_splits` 三个数 |
| `evaluation/calibration/disagreement.py` | 卡只从**调参池**出：`SIDE_SPLITS`（回归集 + 持有集）不进分诊桌 |
| `tests/unit/test_golden_three_split.py`（新，15 条） | 见下 |

**真数据读数（2026-10-22，v6 → v7）**：

```
v6：calibration 191 行（站点级 188）/ holdout 103 行（站点级 101）
v7：calibration 131 行（站点级 128）/ regression 60 / holdout 103（站点级 101）
全员逐行相同：v6(191+103) == v7(131+60+103)，294 行，逐字节可比

agreement v6：头条 139/188 = 73.9%   · 旧口径 216/289 = 74.7%
agreement v7：头条  95/128 = 74.2%   · 旧口径 216/289 = 74.7%   ← 与 v6 报表逐字对齐
快照 v7：856 条，d10_by_split = calibration 95/128 · regression 44/60 · holdout 77/101
分歧卡 v7：33 张（v6 是 49 张；回归集那 16 条分歧 + 持有集 25 条不再进分诊桌）
回归集指纹：2ebb270e64c8（seed=20261022）
回归集标签分布：覆盖 12 类中的 11 类（缺 trailer_suspect——全库只有 1 条，切不进去）
  unresolved 15 · success 8 · subscription_wall 8 · unreachable_hard 8 · not_play_site 7
  component_unverified 4 · blocked 3 · no_play_control 3 · broken 2 · login_wall_or_blocked 1 · trailer_only 1
```

**五条口径**：

1. **191 / 131 是行数，D-10 只认站点级（188 / 128）**。三个数都不是笔误：`191` 含 3 条轨迹级行，它们没有 D-10 标签。[§9.7](#97-已裁定的三条对数字的影响2026-10-22) 已改正。
2. **继承式重冻结不重切**。`--from-version` 只搬行、不洗牌；重新洗牌会让 v7 与 v6 不是同一批行，C-03 定的「以 74.7% 为准」立刻失去对照意义。逐行相等由 `test_继承式重冻结_行原样带过来` 钉住。
3. **回归集一经冻结不得重切**，且带**整行内容指纹**（不是 id 列表指纹）。改 id 挡不住"把金标改成与模型一致"这种最省力的作弊——那正是 `regressed` 会永远为 0 的唯一原因。
4. **分诊池 = 调参池**。回归集分歧归 `verify`（C-08），持有集分歧归最终验收。这不是洁癖：拿回归集调参等于毁掉回归门，拿持有集调参等于烧掉封存——两者都是"看一眼就回不去"。
5. **持有集只报条数**。`d10_by_split.holdout` 落在 `output/` 的派生快照里（gitignored、供 C-08 审计），**不进任何报表头条**；真要跑就 +1 `holdout_runs`。

**已知未做**：`holdout_runs` 仍是手动计数（没有"解封"命令能自动 +1，因为解封本身是 owner 动作）；~~C-08 的 `verify` 还没写~~ → **已补（v1.7 见 [§9.4](#94-p2--内环验证器v13-提前到-r05)）**：回归集现在能出 `fixed / regressed / unchanged` 三类计数（v7 vs v7 实测 `反转 0 · 仍对 44 · 仍错 16`），「44/60 一致」那个读法不再是唯一口径。

**测试（15 条，全量 3525 通过）**：档位词汇唯一真源 · 未登记档名返回空串 · 回归切片确定性 / 只收站点级 / 按标签分层（稀有标签 ≥1）· 切不下报错 · k=0 不切 · 继承式重冻结行原样带过来 + `holdout_runs` 不归零 + 指纹 12 位 + `derived_from` 记录 · 回归集原样带过来 · 回归集不得重切 · 继承不存在的版本报错 · 不切时不写空文件 · 头条不含旁置档 + 旧口径同时输出 · 回归集被改过当场报错 · 快照三档全收但卡只出调参池。

**测试（17 条，全量 3510 通过）**：快照↔agreement 同分母 · `p2_sample` 不进 · `detail_mismatch` 不计作 D10 错 · `eq_class_mismatch` 计数 · `agree`/`ungraded` 不产卡 · `tier_gap` 带 `rubric_ref="D-4.5"` 与理由 · 容差内记 `tier_gap=1` 但 verdict 仍 `agree` · 单侧缺 → `ungraded` 不进分母 · 未登记维 → 空 `rubric_ref` + 「条款号不可用」注记 · 未登记标签抛 `KeyError` · 无 `--version` 抛错且不建目录 · 槽位全空且 `status=="open"` · 重算逐字节相同 · 记两把尺子版本（老档为空串）· `clause_of` 投影矩阵 · `DIM_CLAUSES` 覆盖 `SOFT_DIM_NAMES ∪ {d10}`。

### 9.3 P1 · 归属与抽样（这一步给出投入方向）

| ID | 事项 | 依赖 | 验收 | 成本 | 需拍板 |
|---|---|---|---|---|---|
| **C-05** ✅ | 六桶封闭集 + `BUCKET_PRECONDITION` + `triage.py` 校验 | C-02 | 桶表 ≤ 6；`rubric_*` / `gold_*` 由 agent 填 → 拒绝写入 | 1d | **是**（D-C2 / D-C3）|
| **C-06** ✅ | 首轮分诊 worklist（机器预填建议值 + 人在同一处确认或改） | C-05 | 归属率 100%，无 `other` | 1–2d 人时 | 否 |
| **C-07** ✅ | 归属分布报告：**rubric 缺陷 vs 评估器缺陷比例** | C-06 | 能回答"下一手该改尺子还是改判据" | 0.5d | 否 |
| **C-16** ✅ | 触发读数 `triggers`：T-1～T-4 四类（[§5.4](#54-量化触发v13-新增)），**先只出读数** | C-15 | 每条信号有读数 + `n`；阈值列为空时命令**不判合格**，不许默认通过 | 1d | ✅ 已拍板（D-C9：读数先行、线后定）|
| **C-19** ✅ | 分层抽样器（S0–S3 固定配额 + seed），产出人工 worklist | C-15 | 同一输入两次抽样逐行相同（可复现）；每层行数 == 配额（40 = 10×4）| 0.5d | ✅ 已拍板（D-C10）|

**C-07 是整条线的决策点**：这个比例决定 H-02 后续投 LLM prompt、重采集，还是改 rubric。
**2026-10-23 已出第一组读数**（33 张卡由人一条条签完，见[下文 C-07 实施结果](#c-07-实施结果2026-10-23分支-main)）：
**评估器 12 · 金标 14 · 采集 7 · rubric 0**——即「这一轮没有证据支持先改 rubric」。

**C-16 与 C-19 一并落地**（2026-10-23）：同一份 C-15 快照的两半——先出读数（越线才值得看），
再决定判读谁（分层抽而不是只抽分歧），见[下文实施结果](#c-16--c-19-实施结果2026-10-23分支-main)。

#### C-05 实施结果（2026-10-23，分支 `feat/calibration-loop`）

**一句话**：分诊从「在聊天里说一句『这是评分器的问题』」变成**一条带签名、带判据、进版本控制的记录**，而桶表自己是封闭的——贴不进去时工具会拦住人，不让人临时造筐。

**落点**：

| 文件 | 改动 |
|---|---|
| `evaluation/calibration/buckets.py` | 新增。`BUCKETS`（六项，§4 顺序）· `MAX_BUCKETS = 6` 且 **import 期**检查 · `BUCKET_PRECONDITION`（每桶 ≥1 句，写清「什么不算这一桶」）· `BUCKET_OWNER`（code / prompt / rubric / gold / collector）· `BUCKET_LOOP`（evaluator / rubric / gold / collection）· `HUMAN_ONLY_PREFIXES = ("rubric_", "gold_")` → `HUMAN_ONLY` **由前缀推导**，不手抄第二份名单 |
| `evaluation/calibration/triage.py` | 新增。`actor_kind`（机器身份名单 + `agent:` 等命名空间前缀）· `validate_row` / `validate_rows`（**唯一校验入口**：签名 → 缺字段 → 桶 → 维度 → D-C3 权限 → 责任推导 → 状态）· `load_triage`（`None` 透传 / `[]` 报错 / 缺档报错）· `add_rows`（默认 dry-run、默认拒覆盖、`_same_triage` 做幂等只比 bucket/by/evidence，忽略写入时间）· `summarize` · CLI `check` / `add` |
| `evaluation/calibration/verify.py` | 改。**删掉自己的 `load_triage`**，改为 import `triage` 那一个；`analyze` 里**再校验一遍**（`load_triage` 是 CLI 入口，`analyze` 是公开 API——两条路都要过同一套规矩）；`main` 同时吃 `TriageError` → 退 2 |
| `evaluation/calibration/gate.py` | 改。`main` 的异常集合加上 `TriageError`（分诊表坏 = 测不成，不是驳回）|
| `evaluation/golden/data/triage/README.md` | 新增。字段表（含派生字段 `responsibility`）、六桶、`triage_by` 的机器身份拒绝、`check` / `add` 用法、`--replace` + git 即审计 |
| `tests/unit/test_calibration_triage.py` | 新增，**33 条**：桶表封闭集与推导 / 身份（`AGENT` / `llm:qwen` / `agent:coder` / `bot` 全拒，`agent-smith` / `Qwen` 当人）/ 字段校验 / 读写幂等与 `--replace` / CLI 三态 / `verify.analyze` 再校验 |
| `tests/contract/test_rubric_traceability.py` | 新增，**11 条**：桶表封顶六项（含精确顺序）· 禁 `other` / `unknown` / `misc` / `accepted_exception` / `none` / 空串 · 每桶都有前置事实 / owner / loop · `HUMAN_ONLY` 由前缀推导且 agent 逐桶被拒 · 签名与证据必填 · **写入口唯一**（`inspect.getsource` 断言 `add_rows` 里 `validate_rows(` 出现两次） |

**验收对照**：

| 计划验收 | 怎么封的 |
|---|---|
| 桶表 ≤ 6 | `len(BUCKETS) > MAX_BUCKETS` 在 **import 期** `raise`——加第七桶必须动常量，动不了就是不该加；契约测试同时断言 `len == 6` 与顺序 |
| `rubric_*` / `gold_*` 由 agent 填 → 拒绝写入 | `validate_row` 先认身份再判桶，命中 `TriageError`；**CLI 与 `analyze` 两条路都拒**。契约测试对每个 `HUMAN_ONLY` 桶逐个跑 agent 名 |
| 无 `other` | 契约测试把 `other` / `unknown` / `misc` / `accepted_exception` / `none` / `""` 逐个喂 `require_bucket`，全 `KeyError` |
| 证据可复核 | 缺 `triage_evidence` 的报错文案里带「为什么不是别的桶」，逼的是判据不是结论 |

**三条口径写进了代码注释与文档**：

- **`accepted_exception` 是状态不是桶**（`STATUSES` 里，不在 `BUCKETS` 里）。混成一个字段就会出现「因为先放过所以不归任何人」的死角；
- **身份识别要显式**，不能靠子串猜：`ACTORS` 名单 + 命名空间前缀两张表。猜错两个方向都贵——把人当机器，人的判断进不了档；把机器当人，D-C3 被静默绕过；
- **`triage_at` 不参与幂等判断**。同一行重贴一次应当是 `skipped` 而不是新增一行——比较整行会因写入时间不同而没有一次命中。

**已知未做**：分诊档现在还是空的，真实首轮要等 C-06（worklist + agent 预填建议值）。C-06 的预填只能落在卡上的 `suggested_*`，**不能**写进分诊档——这条路由 `actor_kind` 封着，不是靠约定。

#### C-06 实施结果（2026-10-23，分支 `main`）

**一句话**：「33 张卡摊在桌上等人一条条读」变成**一张已经贴好事实与建议的表**——
而建议那一列在代码里压根没有 `bucket` 这个键，所以它想冒充裁定也冒充不了。

**落点**：

| 文件 | 改动 |
|---|---|
| `evaluation/calibration/worklist.py`（新，~430 行）| `PrefillRule` / `PREFILL_RULES`（注册表）· `check_rules()`（**import 期**守卫：规则桶必须在封闭集内且不碰 `HUMAN_ONLY`）· `facts_of()`（冻结行 → 机器可确认的事实）· `suggest()`（命中 / 不命中 / 多桶冲突三态）· `build_worklist()`（卡 × 冻结行，**行里没有 `bucket` 键**）· `build_worksheet()` · `summarize_worklist()` · CLI `build` / `status` |
| `evaluation/calibration/triage.py` | `KEPT_FIELDS` / `strip_row()`（工作表 → 裁定档只留裁定字段）· `incomplete()` · `import_worksheet()`（**半填整批拒**，写之前仍过 `add_rows`）+ CLI `import` |
| `tests/unit/test_calibration_worklist.py`（新，34 条）| 见下 |
| `tests/contract/test_rubric_traceability.py` | +3 条：预填规则不许指向 human-only 桶 · 预填签名是机器身份 · **`build_worklist` 源码里没有 `"bucket"`、`worklist` 模块不碰 `add_rows`** |

**预填规则（当前一条）**：`R-COLLECTOR-REFUSED` → `evidence_missing`。两个条件缺一
不可——预测侧就是 fail-closed 的 `unresolved` 分支，**且**证据里有一句采集器的拒判
声明（`COLLECTOR_REFUSAL_MARKERS`：采集降级 / 未选中该站点 / 遍历超时 /
导航失败（传输层）/ 哨兵无法区分）。只要求后者不够：一条证据提到"超时"不等于这次
判定的依据全是超时。

**`evaluator_logic` 的预填：留空，是结论不是遗漏。** 能机械确认的只有半步——
`code.source == "rule"` ⇒ **`evaluator_prompt` 按定义不成立**（那一桶的前置事实是
"LLM judge 输出与锚点冲突"，规则判据里没有 LLM 参与）。够不着的另半步是"所以一定是
代码判据"：`rubric_ambiguous` / `rubric_missing` / `gold_defect` 全都还活着，而它们
恰恰是"条款读得出两种结论 / 没写 / 金标读错"——机器不读条款分不出来。规则实现留在
`_detect_rule_decided`（不注册），要真做得先让判据自己落「按哪条条款判的」+ 该条款里
可机械核的那部分（`DIM_CLAUSES` 的反向使用），属于另一条改动。

**四条口径**：

1. **建议不是裁定，而且是结构性保证**：worklist 行里没有 `bucket` 键（不是留空），
   契约测试用 `inspect.getsource` 钉住 `build_worklist` 不产这一列、`worklist` 模块
   不碰 `add_rows`。`suggested_by = "agent:worklist"` 走 triage 同一套身份词汇——
   把建议值抄进分诊档会在 `validate_rows` 当场被拒。
2. **机器只报事实、不猜结论**：`facts` 四样（`pred_origin` / `reached_play_page` /
   `fallback_used` / `gold_reviewed_by` + 命中的拒判原话）来自**冻结行**，卡上没有。
   没有规则命中的 21 条里，人靠这四样就能判掉一大半——这正是"低门槛"的来源。
3. **冲突不平均**：两条规则指向不同桶时不给建议（fail-closed），两条并排列出。
   v7 里没有这种卡，路径由单测用假规则覆盖。
4. **工作表不覆盖**：`build` 见 `worksheet.jsonl` 已存在就跳过（`--overwrite` 才重写）。
   人填到一半被重算冲掉是这类流程最贵的损坏；而**分诊档才是资产**，工作表只是表单。

**真数据读数（2026-10-23，v7，`worklist build` 那一刻）**：

```
33 张卡 → 有建议 12（全部 evidence_missing）· 待人判 21
  R-COLLECTOR-REFUSED   12   采集降级 6 / 未选中站点 4 / 遍历超时 1 / 哨兵无法区分 1
  R-PRED-FROM-RULE       0   ← 未注册（理由见上）
冲突 0 条 · 建议落在 human-only 桶 0 条
覆盖率 0/33 ← 这是 build 当天（v2.0）的读数，**已被 v2.5 的表填完覆盖**
```

**已知未做（留给后继）**：

- **`evaluator_logic` 预填**（见上，缺可机械核的条款信号）——**表填完之后这条仍开着**：
  33 行里 `evaluator_logic` 11 条全靠人读现场判出来的（机器一条也没预填），
  下一轮它们仍会是一整批待人工的卡。

#### C-07 实施结果（2026-10-23，分支 `main`）

**一句话**：这条任务是**唯一一条不需要新代码的 C-xx**——C-05 / C-06 已经把「贴桶的地方」
和「造表的地方」都造好了，C-07 要的是**人把 33 张卡读完并签下名字**。签完了，归属
分布第一次不是猜测；同时**闸门的第四条判据第一次拿到真实输入**。

**落点**（没有新模块，闭环靠三样已有的东西真跑一遍）：

| 东西 | 作用 |
|---|---|
| `evaluation/golden/data/triage/triage.jsonl`（**新入库**，33 行）| 人填的分诊档：`triage_by=reesn` × 33 · `status=assigned` × 33 |
| `calibration.worklist status --version 7` | 出归属分布 + 建议命中率（exit 0 = 全覆盖）|
| `calibration.verify --triage … --bucket evaluator_logic` | 第四条判据 `bucket_clear` 的真实读数 |

**归属分布（v7 调参池 33 张卡，全部 d10）**：

```
分诊进度 33/33 = 100% · 机器建议 12 条（接受 6 / 改判 6）
  evaluator_logic     11  code      ← 评估器（代码判据）错
  evaluator_prompt     1  prompt    ← 评估器（LLM 提示词）错
  rubric_ambiguous     0  rubric    ← 只能人填
  rubric_missing       0  rubric    ← 只能人填
  gold_defect         14  gold      ← 金标自己错
  evidence_missing     7  collector ← 采集/证据缺
```

**这一组读数授权说什么、不授权说什么**：

1. **「先改 rubric」在这一轮没有证据**：`rubric_ambiguous` + `rubric_missing` = 0。
   但**不许读成「rubric 没问题」**——这 0 是「在 33 张卡、只有 d10 一个维度上没人判出
   rubric 缺陷」，而 d02–d09 九个分数维度**一条卡都没有**（软维度无金标，`ungraded`）。
   尺子的问题要等软维度金标填出来才看得见（§9.8 第三条杠杆）；
2. **最大的单一账目是金标（14/33 = 42%）**：金标错比评估器错更多，这直接改变 M-30 的
   优先级——先别急着改判据，先核金标（口径见 [台账 §M-30](../计划进展/10-TODO台账.md#m-30--金标在同一个-url-上不自洽19131)）；
3. **机器建议的 12 条全是 `evidence_missing`，人接受 6 / 改判 6**：一半被改判，正是
   「预填只是提示」的口径在真数据上的落点。其中 5 张「现场定位不到」的卡里，
   **2 张仍被判成 `evaluator_logic`**——**读不到现场 ≠ 采集的错**：页面压根没变成站点
   样本，而代码照样给出了标签，那是代码的账。**同名事实不能直接翻译成桶**；
4. **`bucket_clear` 第一次说话**（`verify` 同版对同版，只验通路）：
   `❌ bucket_clear evaluator_logic 残留 11`（exit 1 驳回）。另外三条：
   `❌ net_improvement 净改善 0（配对 128）`——**同版对同版恒 0，是设计如此**（D-C11）；
   `✅ class_regression 9 类无 F1 下降`；`✅ regression_regressed_zero 0/60`。
   软维度 d2 18 / d4 89 / d5 90 / d6 45 **全 `判不了`**——金标的洞在这一屏上最刺眼。

**遗留（本会话未做）**：

- **`evaluator_logic` 仍无预填**：11 条全靠人读现场。下一轮还会是一整批人工卡；
- **只有一轮数据**：分布是 v7 单版的横截面，不是趋势。第二次真实判读才是趋势的起点；
- **四条判据的读数都是「同版对同版」**：它们的真实意义只在**两版之间**——所以上面那次
  `verify` 是**通路自检**（`net_improvement` 恒 0 是 D-C11 的结构性后果，不是「没有改善」），
  **不是一次合格判定**。第一次真判要等 v8 冻结且两次冻结之间有实际改动；
- **改了评分器本轮读数不会动**：pred 侧取自冻结行（M-29 / §11.2），修 D-5 这类改动
  在重冻结刷新分数之前**对这张表没有影响**。

**记录（§9.9 闭环第 3 条）**：本节 · [10-TODO台账](../计划进展/10-TODO台账.md)（§校准回路：分诊 33/33 + 分布）· [`data/triage/README.md`](../../evaluation/golden/data/triage/README.md)（档本体与字段表）。
**三条齐**（实现 = 已有的 `worklist status` / `verify` 真跑 · 测试 = `test_calibration_triage` 33 条 +
`test_calibration_worklist` 39 条 + `test_calibration_verify` 全部通过 · 记录 = 本节）

#### C-12 + C-18 实施结果（2026-10-23，分支 `main`）

**一句话**：rubric 的改动从「散文里说一句『这次只改了 D-5』」变成**一张能机械核的表
＋ 一道能机械证明的守卫**——而这个守卫最狠的一条判据是它**会承认自己没跑到**
（受检条款上一条都没配上 → 退 2，不印 ✓）。

**为什么两条一起做**：C-12 要的 `disagreement_id` / 责任区分声明与 C-18 要的
`change_kind` / `expected_effect` 是**同一张表的四列**，而「未列条款逐位不变」的
白名单正是 C-18 新加的 `条款` 列。分两次改同一张表，中间那版必然有一列是手填的孤儿。

**落点**：

| 文件 | 改动 |
|---|---|
| `docs/设计方案/在线视频场景rubric.md` | 变更记录表 **4 列 → 9 列**（版本 / 日期 / 条款 / 变更类型 / 变更内容 / 预期效果 / 分歧卡 / 责任区分声明 / 修订人），四行历史原样保留语义、补列处填 `—`；新增 [变更记录列契约](在线视频场景rubric.md#变更记录列契约方案-16-c-12--c-18) 一节（逐列判据 + 两条前向规则 + 审计命令）|
| `evaluation/scorers/rubric_changelog.py`（新，~950 行）| `parse_entries()`（**解析 markdown，不落第二份 JSONL**：表头必须逐字等于九列、行内不许出现裸 `\|`、版本格正则、多值按 `，、,;；` 切）· `validate_entries()`（六组规则）· `is_falsifiable()` / `is_excuse_only()` · `check_current()` · `changed_clauses_between()`（左开右闭区间）· `clause_scores()` / `clause_invariance()`（**diff 守卫**）· `verify_card_ids()` · CLI `check` / `audit` |
| `tests/unit/test_rubric_changelog.py`（新，**61 条**）| 见下 |
| `tests/contract/test_rubric_traceability.py` | **+6 条**（合计 20）：真实文档合契约 · 升版本三处同时落地 · 条款词表与文档三族一致 · 每个维度都写得岀条款号 · `调权重` 是空类 · 派生根与 `verify` 同源 |

**九列里真正新增约束的四列**：

| 列 | 硬要求 | 不满足时 |
|---|---|---|
| `变更类型` | 封闭集五项：`新增` / `修改表述` / `废弃` / `调锚点分档` / `调权重` | `check` 退 2（**没有「其他」**，贴不进去 = 该扩表，同 §4 六桶）|
| `条款` | 必须是已知条款（`G-*` / `D-*` / `B-*` / 派生根 `D-3.5` / 通配 `*`）| 退 2；**它同时是 `audit` 的白名单** |
| `预期效果` | 可证伪：带数字或比较方向（`→` / `<=` / `≥`），或明写「逐位不变 / 零变化」 | 「让判断更准」这类 → 退 2 |
| `分歧卡` / `责任区分声明` | 自 `TRACEABLE_SINCE = 1.3` 起必填；卡号形如 `v7:P000__r1__h:d10`；责任区分 ≥6 字且**不许只写「已修复」**（含 `EXCUSE_PHRASES` 的判为借口，驳回）| 退 2；`--verify-cards` 连卡存在性一起核（卡文件缺 → 报「核不了」，**不是通过**）|

**验收对照**：

| 计划验收 | 怎么封的 |
|---|---|
| 每条改版可指回分歧行（C-12）| `_CARD_ID_RE` 校验格式 + `verify_card_ids()` 去 `v{N}/disagreements.jsonl` 逐条核存在性；**前向追溯线**只豁免 1.3 之前的老行，不伪造历史 |
| 以「评估器做不到」为由 → 驳回（C-12）| `EXCUSE_PHRASES` 命中即 `is_excuse_only()`；单测三条不同措辞的借口全拒，三条正当理由（`rubric_ambiguous` / `rubric_missing` / 表述歧义）全过 |
| 改一条款时能机械证明其他条款分数没动（C-18）| `audit --before N --after N`：`changed_clauses_between()` 取区间内**所有行的 `条款` 并集**当白名单，逐条款比两版快照的 `(golden_id, dimension)`；**未列条款一动 → 退 1**，报告里逐条列 `clause / key / before / after` |
| **「0 条样本 ≠ 一切正常」**（C-18 的暗礁）| `clause_invariance()` 出 `measurable`；假时 `audit` **退 2**、印「这条判据测不成」并列出「快照里出现过的条款」。⚠️ 这条是**冒烟时踩出来的**：`freeze --from-version` 把两版分数原样带过来，守卫会印 `✓` 而一条都没比——见[避坑指南 §8.21](../02-避坑指南.md)。**同一形态在 `check --verify-cards` 上又出现一次**（真文档 4 行全是 `—` ⇒ 核了 0 条却印 ✓），已改成印「没有任何卡号可核」|

**真数据读数（2026-10-23）**：

```
$ rubric_changelog check
rubric 变更记录 · 4 行 / 3 个版本 · 当前 RUBRIC_VERSION = 1.2
追溯线 TRACEABLE_SINCE = 1.3（自该版起：预期效果 / 分歧卡 / 责任区分声明必填）
  v1.0  新增             *
  v1.1  新增, 修改表述, 调锚点分档  D-1..D-8, D-10, G-7
  v1.2  修改表述          D-5
  v1.2（补记，2026-10-17） 修改表述  B-1
exit=0

$ rubric_changelog check --verify-cards
… ⚠ 没有任何卡号可核（追溯线 1.3 之前的行允许填 `—`）——**核不了 ≠ 核过了**
exit=0        ← 不判红（1.3 之前判红就是死门），但**绝不印 ✓**

$ rubric_changelog audit --before 7 --after 8
错误：after 快照不存在 …/calibration/v8/snapshot.json
先跑 agreement --version 8 --emit-disagreements
exit=2        ← v8 还没冻结，这道守卫**今天测不成**，不拿「没发现差异」当通过
```

**冒烟取证**（真实 v7 快照的两个副本，冒充 v70 = 1.1 / v72 = 1.2）：
`1.1 → 1.2` 的改动集 = `{D-5, B-1}` ⇒ 受检 4 个条款 / 配对条目 **648 条**；
手工把一条 D-10 的 pred 从 `success` 改成 `blocked`（**D-10 未列**）→ 退 1 并指名
到 `(golden_id, dimension)`。**先发现这个守卫是空跑的**，才补的 `measurable`。

**四条口径**：

1. **唯一真源是那张 markdown 表**，解析它、不落第二份 JSONL。抄两份必然漂
   （CLAUDE.md 纪律 6 的老账）；而变更记录恰恰是**人改得最勤**的东西，漂起来没人对账；
2. **白名单取自 `条款` 列，不取自「我记得改了哪条」**。改了 `D-3` 时 `D-3.5`
   自动一并算「已列」（`_base_of` 双向覆盖）——派生根不是独立条款；
3. **通配 `*`（整版重写）让守卫退化成「什么都不断言」，且如实印出来**：报告里
   `wildcard=True`、`enforced_clauses` 为空。假装它在守比不守更危险；
4. **金标被重判只报不驳**：快的 `gold` 侧变了（`gold_defect` 窗口内的正常动作，
   C-05 刚给了它一个桶）单独进 `gold_moved`，不混进「pred 动了」。混在一起会让
   每一次正当的金标修正都被读成「rubric 改坏了」。

**已知未做（留给后继）**：

- **`调权重` 是空类**：仓里没有权重表（`WEIGHT_TABLE_EXISTS = False`），这个
  变更类型登记了却**跑不到**。契约测试钉住「它是空类」这一事实，等真有加权
  评分时再补它的可证伪判据——现在硬编一个就是假装有；
- **`audit` 今天没有可比的一对版本**（v7 是最新）。真实第一次跑要等下一次
  `freeze` + `agreement --emit-disagreements`；
- **一处文档缺陷留给人**：rubric 第 172 行「见批次级断言表 `B-10`」——`B-10`
  不是条款（批次级只有 `B-1`..`B-4`），是笔误。**修它就等于改 rubric 正文**，
  要升 `RUBRIC_VERSION` 并自己加一行变更记录，所以没顺手动（顺手改正是这类表
  最典型的腐化方式）；
- **`责任区分声明` 的「≥6 字 + 不含借口词」是形态判据，不是质量判据**：它只能
  拦住「已修复」这种空话，拦不住「写得长但没判据」。真要判质量得人读——与
  `triage` 的证据栏同一个层级的问题。

#### C-16 + C-19 实施结果（2026-10-23，分支 `main`）

**一句话**：**读数与抽样这两半都通了**——`triggers` 回答「有没有值得看的条款」，
`sampling` 回答「该让人看哪些」。而两条命令最诚实的地方都是**它们会喊出自己
没测到什么**：前者喊「4 个条款一条都判不了」，后者喊「缺 20 条，没凑」。

**为什么两条一起做**：C-19 的抽样池就是 C-16 的读数面（都从 C-15 快照派生，
都按 `rubric_ref` 聚），而**只看分歧会把问题估大**这件事，是 C-16 的 T-1 越线
之后才必须解决的——于是先读数、后抽样连成一步。

**落点**：

| 文件 | 改动 |
|---|---|
| `evaluation/calibration/triggers.py`（新）| `SIGNALS`（四条的 `line_set` 区分「已有线」与「候选线」）· `clause_stats()`（`ungraded` 单记不进分母）· `t1_disagreement_rate()` / `t4_distribution()` · `t2_judge_human()`（复用 `builder.cohen_kappa` / `linear_weighted_kappa`，**不重写 kappa**）· `t3_vague_rate()` · `load_judge_pairs()`（缺键**直接抛不猜**）· `_coverage_note()` · `evaluate()` / `render()` · CLI |
| `evaluation/calibration/sampling.py`（新）| `STRATA`（S0–S3，`why` 即判据）· `assign_strata()`（一条只入一层 + 重叠计数）· `_order_key()`（**SHA-256 定序，不用 RNG**）· `_draw()`（**只在本层抽，缺口不补**）· `build_sample()` · CLI |
| `tests/unit/test_calibration_triggers_sampling.py`（新，**36 条**）| 见下 |

**验收对照**：

| 计划验收 | 怎么封的 |
|---|---|
| 每条信号有读数 + `n`（C-16）| `clause_stats()` 的 `n` 是**可判**条目数并随读数一起出；每条读数带 `clause / n` |
| **阈值列为空时命令不判合格，不许默认通过**（C-16）| `SignalSpec.line_set=False` ⇒ `below_line` 恒为 `None`；报告里 `judged` **恒为 `None`**；`render` 第一行就印「本命令不判合格/不合格」。单测两条：`test_每条信号恒不判合格` / `test_线没定的信号不报低于线` |
| 同一输入两次抽样**逐行相同**（C-19）| `_order_key()` = `sha256(seed ␀ golden_id ␀ dimension)` + 稳定兜底键，**不碰 RNG**（`random.sample` 跨 Python 版本可能换结果，冻结档要复算得出来）。单测 `test_同一输入两次抽样逐行相同` + `test_cli_两次落盘逐行相同` |
| 每层行数 == 配额（40 = 10×4）（C-19）| 单测 `test_候选够的层恰好取满10不够的层如实报缺口`；**实际抽不满**（见下）⇒ 落成「== 配额 **或** `short_by` 如实报，不跨层借人」 |
| **层内读数不外推总体**（D-C10）| `sample_weight = N_h / n_h`，`render` 顶上印「`sample_weight` 只是『这条有多重』，不是总体估计量」；单测 `test_层内读数不外推总体这条写在渲染里` |
| **「0 条样本 ≠ 一切正常」** | 两个 CLI 各有一条：**一条都测不到 / 一条都抽不到都退 2**。`triggers` 的退出码里**没有 1**——本命令没有「合格」这个结论可给，1 会被读成「驳回」 |

**真数据读数（2026-10-23，v7 头条档 128 条）**：

```
$ calibration.triggers --version 7
⚠ 还有 4 个条款**一条都判不了**（ungraded 不进分母，不是 0）：D-2(18) D-4(89) D-5(90) D-6(45)
✓ T-1  D-10: 分歧 33/128 = 25.8% ⚠按候选线会越线        ← n 够（≥20）
➖ T-2  测不成：judge 逐行 pred 不落盘（行键是 sample_id，与 golden_id 配不上）
➖ T-3  测不成：同上 + 「观察原文片段」在产物里取不到
✓ T-4  D-10 · pred 众数 unresolved 37.5% / 13 个取值 · gold 24.2% / 12 个取值
exit=0        ← 有读数就是 0；「测了 2 条」不等于「4 条信号齐了」

$ calibration.sampling --version 7
S1 打架   总体 33 / 配额 10 / 实抽 10 ✓（权重 3.3）
S3 中间段 总体  0 / 配额 10 / 实抽  0 ⚠缺 10   ← 软维度全 ungraded；ambiguous 不在 EQUIV_CLASS
S2 含糊   总体  0 / 配额 10 / 实抽  0 ⚠缺 10   ← judge 理由不落盘
S0 对照   总体 32 / 配额 10 / 实抽 10 ✓（权重 3.2）
实抽 20 条（缺口 20）· 覆盖 20 个不同样本
exit=0        ← **抽不满不是 bug、也不许凑**
```

**五条口径**：

1. **触发 ≠ 归因**：越线只说明「该看一眼」，改哪一侧仍由六桶判。所以 `triggers`
   **不产出桶、不碰 `triage`**——把它接进分诊就等于把「触发」偷偷升级成「归因」；
2. **`ungraded` 不进分母，`n = 0` 的条款不出读数**：出 `rate = 0` 等于把
   「没数据」写成「零分歧」，而那正是 T-1 该抓的东西。`coverage` 单列漏掉的条款，
   因为**只报测到的条款，报表会被读成「五条条款都测过了」**（[避坑指南 §8.22](../02-避坑指南.md)）；
3. **T-2 / T-3 的两条理由要分开说**：「软维度金标 0 条」与「judge 逐行 pred 不落盘」
   是两个工程，**补错那个等于白干**。`--judge-report` 的行缺 `golden_id` /
   `dimension` 时**直接退 2**——按 `sample_id` / url 猜键会让 kappa 建在配不上的
   分母上，而 kappa 对配对样本量极敏感；
4. **入层判据全部机械可算**：`STRATA.why` 就是判据原文，**人不参与分层**。
   一条判据要靠人读一遍才知道算不算，那它就不是判据，是意见；
5. **层间重叠是事实，不藏**：一条只入一层（优先级 S1 → S3 → S2 → S0，信息量最大、
   最稀缺的先挑，S0 兜底），重叠计数写进报告 `overlap`——藏起来就成了
   「四层互斥」这个假前提（[避坑指南 §8.23](../02-避坑指南.md)）。

**已知未做（留给后继）**：

- **软维度金标未填是回路的硬前置**：v7 的 D-2 / D-4 / D-5 / D-6 全 `ungraded`
  ⇒ 条款级读数只覆盖 D-10，且 S2 / S3 两层抽不出人。**人工判读只看得见 S0/S1
  ——回路这半边看不见 S2/S3 的质量**。这条把 L-04 / M-18 从「填个金标」升级成
  「不填则三分之二的读数是空的」；
- **T-2 / T-3 的输入还不存在**：`evaluation.cli --json` 的行键是 `sample_id`，
  与快照的 `golden_id` **配不上**。要么让 judge 逐行 pred 带上 `golden_id`，
  要么在快照侧补一个连接键——两条都属 C-11 / M-10 的范围，本次没做；
- **T-1 的候选线（10% / n≥20）未定**：所以它连「越线」都不报，只标
  `over_candidate_line`。**今天 25.8% 按候选线会越线**——但那不是「越了」，
  线还是候选线，定线走 rubric 级变更（D-C9）；
- **S0「两端」的定义是实现选择**：`SUCCESS` / `REACH_FAIL` / `BROKEN_CONTENT`
  （最不容易含混的两端，用作对照层才估得出假阴性）。D-C10 拍板的是「3 层 + 1
  对照层 + 配额 + seed + 不外推」，**没拍板入层成员**——换判据 = 改常量 + 重跑，
  抽样可复现靠种子与稳定排序，所以换判据会被 diff 出来。

### 9.4 P2 · 内环验证器（v1.3 提前到 R0.5）

| ID | 事项 | 依赖 | 验收 | 成本 | 需拍板 |
|---|---|---|---|---|---|
| **C-17** ✅ | **回归集划分与冻结**：从 calibration 切固定 60 条 → `split=regression`，金标冻结 + 配套 diff 守卫 | C-03 | 切片写进冻结档；`split` 三态（calibration / regression / holdout）贯通口径与报表；头条分母见 [§9.7](#97-已裁定的三条对数字的影响2026-10-22) | 0.5d | ✅ 已拍板（D-C8：60 条）|
| **C-08** ✅ | `verify.py`：净改善 > 0 / 无类回归 / 同桶清零 / **回归集 `regressed == 0`** | C-02 · C-15 · C-17 | 用 H1-α 那次（修 4 / 伤 6）回放，必须自动判"驳回" | 1d | 否 |
| **C-09** ✅ | **冻结时闸门**（`gate.py`：定基线 → 重算两版快照 → 交 `verify`；退出码 0/1/2）——v1.8 按 owner 裁定，不接 GitHub Actions（见 [§9.4](#94-p2--内环验证器v13-提前到-r05) 实施结果） | C-08 | 净改善 ≤ 0 **或** `regressed > 0` 时红；**没有基线 = 测不成（退 2）** | 0.5d | **是**（D-C11：门挂 freeze，不挂 push）|

**C-08 的第四项判据是 v1.3 加的**：前三条只看"总数变好没有"，第四条才看"有没有把原来对的改错"。H1-α 那次正是被第四条否掉的（修 4 / 伤 6）——只有前三条时，净改善 −2 也能靠"同桶清零"的松动解释过去。

#### C-08 实施结果（2026-10-23，分支 `feat/calibration-loop`）

**一句话**：「这次改动修好了哪几条、弄坏了哪几条、够不够格合入」现在是一条命令 + 一个退出码。

**落点**：

| 文件 | 改动 |
|---|---|
| `evaluation/calibration/verify.py`（新，~700 行）| `analyze()`（纯函数：两份快照 → 报告 + 四条判据 + 门结论）· `diff_entries()`（五桶差分）· `_per_class()`（等价类 P/R/F1 两轮对比）· `render()` · `main()`（**0 通过 / 1 驳回 / 2 输入不可比**）|
| `tests/unit/test_calibration_verify.py`（新，29 条）| 见下 |

**记录（§9.9 闭环第 3 条）**：[08-常用命令速查 · 黄金集与校准回路](../08-常用命令速查.md)（命令一行一条 + 必填项表的「退出码 1 与 2 不许混报」）· [04-运行手册 · `calibration.verify` 两轮对比](../04-运行手册.md#calibrationverify-两轮对比方案-16-c-08)（四条判据 / 参数 / 退出码 / 三条口径）· [02-避坑指南 §8.18](../02-避坑指南.md)（`n/a 不算通过`、`测不成`不是`回退`）+ 自查表一行。**三条齐 → 按 §9.9 算闭环**（实现 / 29 条测试真跑 / 三处记录）。

**四条判据的落地口径**（每条都在报告里有独立状态，`pass` / `fail` / `not_evaluable`）：

| 判据 | 怎么算 | 判不了的处置 |
|---|---|---|
| `net_improvement` | 调参池 `d10` **配对行**上的 `修好 − 反转 > 0`（严格大于；净 0 不过）| 一条都没配上 → `not_evaluable` → **驳回** |
| `class_regression` | **等价类**逐类 F1（与 `agreement` 的下钻口径 D 同一份 `classification_report`），任一 `ΔF1 < -容差` 即驳回；默认容差 0（放宽属 D-C6 阈值变更）| 没有一类有可比基线 → `not_evaluable` → 驳回 |
| `bucket_clear` | 本轮调参池里目标桶的残留分歧必须为 0；**且分诊表必须覆盖本轮全部分歧** | 没给 `--triage` → `not_evaluable` **且不参与判定**（打印「n/a，不算通过」）；覆盖不全 → `not_evaluable` → 驳回 |
| `regression_regressed_zero` | 回归集 `regressed == 0`（配对行）| 本轮没有回归集 → `not_evaluable` → 驳回（不能按"零回归"放行）|

**五条口径**（都是这一轮踩出来的，不是设计时的推断）：

1. **五桶，不是三桶**。计划要的 `fixed / regressed / unchanged` 之外，`still_wrong`（两轮都错）与 `unverifiable`（任一轮判不了）**必须单列**——前者的缺席会被读成"分母变小了"，后者按 §2.5 的纪律绝不许记成"修好"。报告的「三类计数」标题行仍按计划口径写。
2. **配对键是 `(golden_id, dimension)`**，没有第二个键可选：`golden_id` 里带 run id ⇒ **重采集的行配不上**。配不上的逐条计数（`only_baseline` / `only_current` + id 列表）。这一条同时意味着**现在判不了"改评估器 → 重采集"那种回路**——见下面「已知未做」。
3. **不可比的输入一律退 2，而不是给个数**：键重复（同一行同一维两条判定）、换过档的行、回归集键集或金标变了、快照 schema 不同、缺 `--triage` 却给了 `--bucket`。**1 与 2 分开**是关键——CI 若把工具故障读成回退，第一次误报就会让门被绕开。
4. **同桶清零的"0 残留"可能是假的**：只判「目标桶残留 0」时，分诊表没覆盖的那部分完全可能在目标桶里。故要求**覆盖全部分歧**才认账。
5. **`detail_mismatch` 算对、`tier_gap` 算错**（与 D-10 口径 C、软维度容差一致）。`VERDICT_SIDES` 覆盖 `snapshot.VERDICTS` 全集，**快照那边长出新状态而这里没归类时导入即炸**。

**真数据读数（2026-10-23）**：

```
v7 vs v7（同一份快照，等价于"什么都没改"）
  头条：配对 128 · 修好 0 · 反转 0 · 净 +0 · 仍对 95 · 仍错 33
  回归集：配对 60 · 反转 0 · 仍对 44 · 仍错 16     ← 44/60 一致，与 C-17 那条读数同源
  判据：net_improvement ❌ / class_regression ✅（9 类无下降）/ bucket_clear ➖ / regression ✅
  退出码 1（驳回：净改善 0）——**这正是它该有的行为**：没改东西就没有"改善"可主张
v6 vs v7（跨切档）
  退出码 2：「60 行（186 条判定）在两轮之间换了档」——C-17 切档后逐档不可比，
  命令拒绝而不是给出一个跨档净改善（186 = 60 行 × d10 + 各软维度条目）
软维度：d2/d4/d5/d6 全部 `判不了`（黄金集还没填软维度金标）→ 正好说明"软维度只出读数不设线"
```

**H1-α 回放**（`test_H1α回放_修4伤6_必须驳回`）：35 条 PAYWALL（baseline 判对 7）+ 36 条 SUCCESS，修 4 条 FN、误伤 6 条 SUCCESS ⇒ `fixed=4 / regressed=6 / net=-2`，**两条独立理由同时驳回**（净改善 ≤ 0、SUCCESS 的 F1 下降）。⚠️ 这是按 [11 号附录 A](11-外部方案对账.md) 的读数**重建的夹具**——那次实验发生在快照机制存在之前，没有可回放的历史快照；回放的是**结论形态**，不是那份数据。

**已知未做（留给后继）**：

- **「改评估器 → 重采集」这条回路现在还判不了**：快照的 pred 侧取自**冻结行**的 `code.branch`（`snapshot.PROVIDER_FROZEN_BRANCH`，不由当前评分器重算），而重采集会换 run id ⇒ 按行一条都配不上。要支持它得给快照加**案件身份**（`task_id` + `url`，schema v2）并按案件配对，还要定「同一案件采了两次谁算数」的规则（现在只能 `KeyError`）——那是**另一次改动**，别在 verify 里塞一个隐式回落。
- `bucket_clear` 的输入（分诊表）来自 C-05，目前还没有；给了 `--triage` 也只是能算，桶表本体（≤6 桶封闭集）仍未落地。
- 快照没有记 `regression_fingerprint`：回归集的键集/金标守卫在 verify 里**重算**（两轮键集相同 + 金标逐条相同），与 `manifest` 里的整行指纹是两道独立防线，不互相顶替。

#### C-09 实施结果（2026-10-23，分支 `feat/calibration-loop`）

**一句话**：`verify` 现在**有人替它跑**——`freeze` 完跑 `gate`，退出码就是这一版能不能采纳。

**形态（D-C11，owner 2026-10-23 裁定）**：**冻结时闸门**，不接 GitHub Actions。
仓库本来就没有 CI 配置，而更要紧的是结构原因：快照的 pred 侧取自冻结行，
同一版对同一版恒为净改善 0 ⇒ 按 push 跑的门**永远红**，第一次误报就会让人绕过它。
能被判的只有「这一版比上一版好」，触发点自然是 `freeze`。

**落点**：

| 文件 | 改动 |
|---|---|
| `evaluation/calibration/gate.py`（新，~330 行）| `list_versions()`（只认有 `manifest.json` 或任一档样本文件的 `v{N}/`）· `resolve_versions()`（三条预检）· `ensure_snapshot()`（缺省**每次重算**，`--no-emit` 才读盘）· `run()`（返回 `(报告, 退出码)`，测不成抛 `GateError`）· `main()` |
| `tests/unit/test_calibration_gate.py`（新，20 条）| 见下 |
| `evaluation/calibration/verify.py`| `_load_triage` → `load_triage`（public）：闸门的 `--triage` 走**同一份**加载器，不抄第二份 |

**闸门自己的五条预检**（全部退 2，**不是驳回**——判据根本没跑）：

| 预检 | 为什么不能放行 |
|---|---|
| 一个冻结版都没有 | 先 `freeze` |
| 本轮是**首版** | 没有比的对象；自造「空基线」会让净改善恒 0 退 1，看起来像驳回，实际什么都没判 |
| 基线与本轮**同一版** | 那不是比，是没比（预检就该拦，不该等到报告里出现一个 0）|
| 基线**晚于**本轮 | 反着比，正负号会反 |
| 冻结档出不了快照（头条分母空 / 回归集被改过 / 尺子版本不认识）| 输入不可信，不是结论 |

**三条口径**：

1. **每次重算两版快照，不复用盘上的旧 `snapshot.json`**。快照不含时间戳、同输入
   逐字节相同 ⇒ 重算零噪声；而复用会把上一轮的结论当这一轮的
   （`test_每次重算快照_不复用盘上的旧文件` 就是拿"本轮改回原样"打的这个点）。
2. **出快照与人工跑 `agreement --emit-disagreements` 是同一份实现**（进程内调用
   `cmd_agreement`，输出重定向掉）——`test_闸门出快照与人工跑agreement是同一份实现`
   逐字节比对两边产出的 `snapshot.json`，防的是「闸门自己拼一份」这种会漂的做法。
3. **`gate_meta` 进报告**：`baseline_version` / `current_version` / 两条快照路径 /
   `emitted` / 三码表。下次有人问「这个绿是哪两版比出来的」，答案在文件里，
   不在命令行历史里。

**真数据读数（2026-10-23）**：

```
gate                       （缺省：v6 → v7）
  退出码 2：60 行（186 条判定）在两轮之间换了档 … + 「第一对可比的是本轮与它的**后继**」
gate --current-version 7 --baseline-version 7
  退出码 2：基线与本轮都是 v7 —— 同一版本对同一版本恒等于净改善 0，这不是驳回也不是通过
gate --current-version 99
  退出码 2：v99 不是冻结版本（现有：v1…v7）
```

**现状是对的**：黄金集最新两版（v6 → v7）之间正好是 C-17 切档，**今天没有可判的一对**；
闸门第一次真正判，是 **v8 冻结之后（v7 → v8）**。这不是缺陷，是切档的必然后果——
放宽切档守卫才是缺陷。

**已知未做（留给后继）**：

- **没接任何 CI 主机**（D-C11 的形态结论）：远端复算要等有 CI 时再接，接的也是 `gate`，
  且退出码 1 与 2 **分开报**；
- `bucket_clear` 的输入仍是 C-05 的产物（闸门已把 `--triage` / `--bucket` 透传好，
  给了就算、不给就 `n/a 不算通过`）；
- 闸门不负责「冻结该不该发生」——`freeze` 的校验（回归集不得重切 / 指纹）在 builder 里，
  闸门只判**冻结之后这一版比上一版好没好**。

### 9.5 P3 · prompt ↔ rubric 对齐

| ID | 事项 | 依赖 | 验收 | 成本 | 需拍板 |
|---|---|---|---|---|---|
| **C-10** ✅ | D3 / D9 锚点表化，few-shot 挂 `anchor` | — | 改 rubric 某档而 anchor 未改 → contract test 红 | 1–2d | ✅ **已裁**（D-C7 = 强制连带改 prompt）|
| **C-11** ✅ | `pred_provider` 记 prompt hash | C-10 | 分歧卡能回答"旧 prompt 还是新 prompt 判的" | 0.5d | 否 |
| **C-12** ✅ | rubric 变更记录加 `disagreement_id` + 责任区分声明栏 | C-02 | 每条改版可指回分歧行；以"评估器做不到"为由 → 驳回 | 0.5d | 否 |
| **C-18** ✅ | **变更记录条款化**：rubric 变更记录加 `change_kind` / `expected_effect` 两列 + 「未列条款逐位不变」diff 守卫 | C-02 | 改一条款时能**机械证明**其他条款分数没动 | 1d | 否 |

> **C-12 与 C-18 是同一张表**（2026-10-23 一并落地，见[下文实施结果](#c-12--c-18-实施结果2026-10-23分支-main)）：
> 四列同表、`条款` 列同时当 diff 守卫的白名单，分开做中间那版必有一列是孤儿。

**`rubric_ref` 与 `DIM_CLAUSES` 划在 C-02 实现范围内**，不留给 C-18：卡派出来若没有条款号，后面每张卡都要回填一次，回填必然漂。

### 9.6 P4 · 治理散活

| ID | 事项 | 依赖 | 验收 | 成本 | 需拍板 |
|---|---|---|---|---|---|
| **C-13** ✅ | 等价类变更**记账** + "合并后不可见细分"清单 | — | 改 `EQUIV_CLASS` 不补注记 → contract test 红 | 0.5d | ✅ **已裁**（D-C4 = **不需 owner 签**；签那一半作废，记账那一半保留）|
| **C-14** ✅ | 阈值**唯一真源** + 各处只从表取值 | — | 使用处留字面量 / 表里有条目没人用 → contract test 红 | 0.5d | ✅ **已裁**（D-C6 = **阈值调整不算 rubric 级变更**）|

#### C-10 + C-11 实施结果（2026-10-23，分支 `main`）

**为什么两条一起做**：C-11 的验收是「分歧卡能回答『旧 prompt 还是新 prompt 判的』」，
而 C-10 恰好是「prompt 现在由表渲染」——**没有 C-10，C-11 记下来的版本号是散文
的版本号**（改散文不影响指纹里任何一个字节，锚点表进了指纹才看得见）。
两条都落地后，「哪一档的锚点变了」与「哪一版 prompt 判的」是同一把尺子量出来的。

| 落点 | 内容 |
|---|---|
| `evaluation/scorers/rubric_version.py` | 新增 `ANCHOR_TABLES`（D-3 / D-9 **逐档**释义，逐字抄自 rubric 文档）+ `anchors_drift()`（现读文档比对，**改档不改表 ⇒ 返回非空**）+ `render_anchor_block()`（prompt 段从表渲染）+ `anchor_clauses()`（条款号由 `DIM_CLAUSES` **投影**，不手抄）+ `code_scorer_version()` / `scorer_fingerprint()` / `llm_part()` |
| `evaluation/scorers/d3_scorer.py` · `d9_scorer.py` | D-3 prompt 末尾 = `render_anchor_block("d3")`；D-9 的六条 verdict 措辞从 `D9_VERDICT_TEXT` 表**渲染**（原先是手抄）；`validate_few_shot_anchors()` 校验 few-shot 每条都挂着表里有的锚点 |
| `evaluation/calibration/snapshot.py` | `pred_provider` 改成 **`<kind>@<version>`**；`row_scorer_version()` 从冻结行读版本，**没记到写 `unrecorded`，不拿当前指纹顶替** |
| `evaluation/calibration/disagreement.py` | 卡上多三栏：`evaluator_version`（快照的尺子面指纹）· `pred_provider_version` · `pred_prompt_version`；`prompt_comparability()` 给**三态**答案 |
| `evaluation/golden/builder.py` | `overlay-scout` 回填时把 `scorer_version` 写进行里（**谁打的分，行里就得记着谁打的**）；没样本的行**显式写空**，不让上一轮的版本留在行里 |
| `tests/contract/test_rubric_traceability.py` | C-10 五条不变式（逐档对齐 / 条款号投影 / prompt 渲染 / few-shot 锚点 / D-9 措辞覆盖档位释义） |
| `tests/contract/test_calibration_provenance.py` | C-11 / C-13 / C-14 二十条 |

**验收怎么算过**：不是「加了表」就算过，是**改坏它就红**——
`tests/mutation_check.py` 里配了三条变异（改锚点表 / 改 provider 版本段 /
改等价类分组），跑出来 56/56 全部变红。

**已知未做（留给后继）**：**冻结行 v1–v8 没有 `scorer_version`**（那时还不写这栏）
⇒ 存量数据的卡一律 `unrecorded` ⇒ **「旧 prompt 还是新 prompt 判的」对存量
数据答不出来**。这是**如实标出的缺口**，不是假装答得出来——`prompt_comparability()`
返回 `None` 而不是猜一个「一样」。要补这个缺口得重跑 `overlay-scout`（那会重挂
分数并改变读数），**本轮没做**：v1–v8 冻结档一字不许改。
✅ **2026-10-24 已补**（走**档外台账 + 面感知**那条路，不改冻结档、不改
`candidates.jsonl`）——见 [C-11 补齐实施结果](#c-11-补齐实施结果2026-10-24分支-main)。

⚠️ **本次改动改了 D-3 / D-9 的 prompt 文本** ⇒ 评估器指纹从 `9b3ec1c46545` 变成
`1046e67994aa` ⇒ **这个指纹还没有跑过真实 LLM 验证**（~~本机无凭据~~——**这句
作废：凭据是在的，是当时没跑**）。**下一版冻结前，D-3 / D-9 必须各跑一次真实
A/B**——否则新指纹是「算出来的」，不是「验过的」。
✅ **2026-10-24 已跑**（候选全体：D-3 247 条 / D-9 8 条，三臂 + 噪声地板）——
见 [D-3 / D-9 提示词 A/B 实施结果](#d-3--d-9-提示词-ab-实施结果2026-10-24分支-main)。

#### C-13 + C-14 实施结果（2026-10-23，分支 `main`）

**为什么两条一起做**：两条都是「治理散活」，各改各的面（等价类 vs 阈值），
但**同一个毛病**——原先都靠人记得，所以都不设签名门而改成**机器可核的面**。

| 落点 | 内容 |
|---|---|
| `evaluation/golden/alignment.py` | 新增 `EQUIV_CLASS_NOTES`（每个**多标签**类一张：规范标签 / 被并标签 / **丢了什么细分** / 为什么并）+ `class_labels()`（派生）+ `invisible_details()`（**「合并后不可见细分」清单**）+ `validate_merge_notes()`（与表**逐标签**比对） |
| `evaluation/scorers/thresholds.py`（新） | 阈值**唯一真源**：11 条，每条带 `value` / `status`（已定线 / 只出读数）/ `basis`（哪来的）/ `where`（谁在用）；`value()` 对未登记的名字 **fail-closed 抛 `KeyError`** |
| `evaluation/calibration/triggers.py` · `verify.py` · `gate.py` · `evaluation/golden/builder.py` | 四条信号线 / 类回归容差 / D-10 上岗线 / 软维度档差容忍 / 一致性命令打印的上岗线，**全部改从 `thresholds.value("<name>")` 取**，不再各写一份字面量 |

**两条裁定的形状差别**（写出来免得后人把它们看成同一件事）：

- **D-C4（不需 owner 签）** ⇒ C-13 **没有签名字段**。防「无人记账」靠的是
  **代码侧注记 + 契约测试**：往等价类里塞标签而不补注记 ⇒ 测试红。
- **D-C6（不算 rubric 级变更）** ⇒ C-14 **不升 rubric 版本、不进 rubric 变更记录、
  不设签名门**。防「差一点点调线」靠的是**唯一真源 + 契约测试**：使用处留字面量 ⇒
  红；表里有条目没人用 ⇒ 红；两个模块互相 import ⇒ 红（机械证明「阈值不是
  rubric 级变更」）。

**⚠️ D-C6 与 D-C9 咬在一起**：D-C9 原写「新线第一批只出读数，**定线本身按 rubric
级变更（D-C6）走**」——**这句作废**：定线只改阈值表的 `status`（只出读数 → 已定线），
**不进 rubric 变更记录**。两条裁定互相推翻的那半句，已在 §2.2 表里就地标注。

#### C-11 补齐实施结果（2026-10-24，分支 `main`）——**版本级出处台账 + 面感知可比性**

C-11 落地时留了**一条尾巴**：冻结行 v1–v8 一行都没有 `scorer_version`，而冻结档
**一字不许改** ⇒ 「这批 pred 是哪版评分器打的」对存量数据答不出来
（`unrecorded`，返回 `None`，**不猜**）。

**补法不是重跑 `overlay-scout`**：那条路会把**当前**评分器的读数写回
`candidates.jsonl`——那是**改数**（而且改的是中央文件）。M-29 之所以存在，
正是因为「分数过期」这件事一个字都不报。⇒ 走**第三条路**：

    冻结档不动 · 中央文件不动 · 只在**档外**落一份出处登记（版本级台账）

**台账怎么答「这版是哪版评分器打的」**：分三个**面**（face）——这是这一轮最要紧
的一件事。**面不是同一件事，混起来就是 C-11 那半个缺口**：

| 面 | 谁产出该维的 pred | 怎么认定 |
|---|---|---|
| `code` | D-2 / D-4 / D-5 / D-6（**代码**评分器）| 行里记了指纹 ⇒ 直接读；没记 ⇒ **重算**：用当前代码面重跑该版的样本，逐行比对存档里的读数 |
| `llm` | D-3 / D-9（**LLM 裁判**）| 行里记了 prompt 指纹 ⇒ 直接读；没记 ⇒ 不可判定 |
| `collection` | D-10（**采集侧** `code.branch`）| 判定侧版本**从未记录** ⇒ **不适用**（不是「不可判定」）|

原来的 `prompt_comparability()` **一律拿 prompt 面去比** ⇒ 对代码面的行
**问错了问题**，对 D-10 的行**问了个不适用的问题**——而问错问题的答案看起来
完全正常。

| 落点 | 内容 |
|---|---|
| `evaluation/calibration/provenance.py`（新）| `DIM_FACES`（维 → 面，**唯一真源**，不手抄第二份）· `face_of_dimension()`（未登记维度 ⇒ `unknown`，**不猜**）· `face_version_of()` · 五个状态（`recorded` / `reproduced` / `diverged` / `unrecorded` / `not_applicable`）· 台账读写（**缺文件 ⇒ `{}`**；**行读坏 ⇒ 抛 `ValueError`**——半份台账比没有台账更坏）|
| `evaluation/scorers/rubric_version.py` | 加 `code_part()`（从 `code:…|llm:…` 取**代码面**那段；取不到 ⇒ `unrecorded`，不返回空串）|
| `evaluation/calibration/snapshot.py` | 快照条目增**面三栏**（`pred_face` / `pred_face_version` / `pred_face_status`）；软维度的版本解析走三路：行里记了 ⇒ `recorded`；台账 `reproduced` ⇒ 补出版本；台账 `diverged` ⇒ **已知不可比**；都没有 ⇒ `unrecorded`——**不拿当前指纹顶替** |
| `evaluation/calibration/disagreement.py` | `prompt_comparability()` **改名** `pred_comparability()`（**不保留旧别名**：一个函数一个意思）+ 新增 `pred_comparability_reason()`（**三态答案必须带人话理由**——没有理由的三态是半份答案）+ **两栏写进卡里**（`pred_comparability` / `pred_comparability_reason`）|
| `evaluation/calibration/worklist.py` | ⚠️ **顺带修掉一个潜伏 bug**：`_detect_rule_decided` 拿**整串** `pred_provider` 比常量，而 C-11 给它加了 `@版本` 后缀 ⇒ **永远不相等** ⇒ 那个判定**就算被启用也不会触发**（它本来就不在 `PREFILL_RULES` 里，是**双重失效**——改一处等于没改）。改用 `provider_kind()` 比**种类段**。另：可比性三态 + 理由**一路带到工作表**（人判桶看的是工作表，停在卡上就等于没送到）|
| `evaluation/golden/builder.py` | `_recompute_code_face()`（**重算取证**：逐行比对存档读数，比不了的行**不进分母**）· 新子命令 `provenance`（**混面 ⇒ 退 2**，不许硬写）· `cmd_freeze` 的 manifest 增 `scorer_fingerprint` · `cmd_agreement` / `_emit_calibration_artifacts` 把台账与代码面指纹传进快照 |
| `evaluation/golden/data/scorer_provenance.jsonl`（新，8 行）| 版本级台账本体：**v1–v8 各一行**，逐面记 `version` / `status` / `comparable` / `evidence` / `note` |
| `tests/contract/test_provenance_registry.py`（新，31 条）| 见下 |

**重算取证的读数**（2026-10-24，`provenance` 命令跑出来的）：

| 版本 | 代码面 | 状态 | 可比性 | 取证 |
|---|---|---|---|---|
| v1–v5 | — | `unrecorded` | **不可判定** | 本版没有可比对的软维度读数（`golden.dimension_scores` 为空 ⇒ 存量卡的 pred 全是 D-10 采集面）|
| **v6 / v7** | — | **`diverged`** | **False（已知不可比）** | `rows_compared 213` · `rows_matched 178` · `rows_diverged 35`；逐维 **d5 {compared 211, diverged 31}** · d4 {compared 207, diverged 4} · d2 / d6 全一致。⇒ **M-29 那条「d5 恒真」现在有了机器可读的版本** |
| **v8** | `28cbe88eb7e6` | **`reproduced`** | **True（可比）** | 216 行 × 4 维**逐条全一致** |

`llm` 面：v7 / v8 = `9b3ec1c46545`（从 manifest 现读，状态 `recorded`）；v1–v6 =
`unrecorded`。⚠️ **但它的可比性是 `None`**——`overlay-scout` 只回填
d2 / d4 / d5 / d6，**归档里没有 LLM 产的 pred** ⇒ **这一面压根没参与本版 pred**
（「版本知道，但这一面不适用」）。
`collection` 面：判定侧版本从未被指纹化 ⇒ 版本 `null` / 状态 `unrecorded` /
可比性 `None`；**重采会换 `run_id`，不是同一总体** ⇒ 不能拿新判的来比。

**四条口径（写出来免得后人把它们看成同一件事）**：

1. **效力边界，不夸大**：「当前代码面逐行复现了本版读数」证明的是**读数层面的
   一致**，**不是**「当时跑的就是这份源码」——源码当时长什么样没记，谁也证不了。
   但可比性要的正是前者（拿这批读数跟当前读数对账，前提是「当前评分面能复现
   它们」）。
2. **「已知不可比」≠「不可判定」**：复现不出来 ⇒ **False**（确定的）；什么都没记到
   ⇒ **None**（答不出来）。两者混起来，「不可比」这一档就在报表里**消失**了。
3. **冻结档一字未改**：v1–v8 的 `calibration/holdout/regression.jsonl` 与
   `manifest.json` **全部零改动**，`candidates.jsonl` 也没动。台账是**可重算的
   旁路账**，落在 `golden/data/` 根下（**不进 `v{N}/`**——那一层是冻结档）。
4. **面感知不是加字段，是换问题**：`pred_comparability` 拿 `pred_face` 决定比
   哪一面；代码面的卡比**代码面版本**，prompt 面的卡比 **prompt 面版本**，
   采集侧的卡**永远 `None`**（理由写明「不适用」）。
5. **结论与理由必须**落在卡上**：光有那两个函数**等于没答**——答案只活在
   「会调用它的地方」，而**没有那种地方**（全仓库零调用方）。卡是**给人看的
   产物** ⇒ `pred_comparability`（三态）+ `pred_comparability_reason`（人话）
   **同卡两栏**，读的人不必先知道有这两个函数。
   ⚠️ 这个坑本仓库踩过：一个没生效的检查和一个真红，**在结果里长得一样**。
   ⇒ 再往前一跳：**工作表**（`worklist build` 出的那张）也带上这两栏——
   **人判桶时看的是工作表**，停在卡上等于没送到。

**端到端读数（`agreement --version N --emit-disagreements` 实跑）**：
v6 出 49 张卡 / v7 出 33 张 / v8 出 33 张，**全部是 D-10 采集侧卡**
⇒ 可比性 `None` + 理由「不适用」。⇒ **代码面的 `diverged` / `reproduced`
落在快照条目上，不落在卡上**——因为软维度（D-2/D-4/D-5/D-6）的 pred 与金标
基本一致（`agree`），**不出卡**。这不矛盾：**卡是「有差异的行」，快照是
「全部行」**；版本级的 `diverged` / `reproduced` 结论在**台账**
（`scorer_provenance.jsonl`）里，三处各答各的那一问。

**验收怎么算过**：`tests/contract/test_provenance_registry.py` **31 条** ·
`tests/mutation_check.py` 新增 **10 条**变异（`采集侧pred被当成可比` ·
`已知不可比被降级成不可判定` · `不同版也被当成可比` · `台账复现结论不补出版本` ·
`重算比对恒真` · `认不出的维度被塞进代码面` · **`可比性结论不写进卡`** ·
**`可比性理由不写进卡`** · **`行数被填成指纹数`** ·
**`可比性没走到工作表`**）⇒ **66/66 全部变红**。

#### D-3 / D-9 提示词 A/B 实施结果（2026-10-24，分支 `main`）

C-10 落地时留下一句：「⚠️ 本次改动改了 D-3 / D-9 的 prompt 文本 ⇒ 评估器指纹从
`9b3ec1c46545` 变成 `1046e67994aa` ⇒ **这个指纹还没有跑过真实 LLM 验证**」。
那句话里的「本机无凭据」**作废**——凭据是在的，是当时没跑。这一节是那次
**该跑而没跑的 A/B**。

**先说方法怎么保证测的是同一件事**（这一节比读数重要）：

| 环节 | 做法 | 为什么 |
|---|---|---|
| **旧版提示词怎么来** | **重建**：把现版里的锚点块**抠掉**（`render_anchor_block` 的确定性输出）| 靠记忆写一份「旧的」＝测的不是同一件事 |
| **重建对不对** | **指纹逐位核验**：用**C-10 之前那套算法**（`parts` 少 `anchors` 键）算重建版的指纹，**必须等于 v8 manifest 现读的 `evaluator_version`**（`9b3ec1c46545`），对不上**当场停** | 指纹是 prompt 正文的 sha256 ⇒ 对上就是**逐字节还原**（C-10 的算法本身变了，拿新算法核旧提示词必然对不上）|
| **三臂** | `old`（重建旧版）· `new`（现版）· `new2`（**现版再跑一遍** = **噪声地板**）| 温度 0 **不保证**跨次调用确定 ⇒ 没有地板就分不清「提示词改的」与「模型抖的」|
| **样本** | **候选全体**，不是抽样：D-3 **247 条**（全部可判样本，跨 50 份存档）、D-9 **8 条**（全部触发真实闸门的样本）| 抽样能说的只有「在被抽到的那几条上」|
| **凭据** | 只从 `TRAJECTORY_LLM_*` 读，**不打印不落盘** | 红线 |

**读数**（产物：`research/probes/prompt_ab_anchor.{json,jsonl,log}`）：

| 维 | n | 判出一致率 old↔new | **噪声地板** new↔new2 | 分数一致率 old↔new ※ | 读法 |
|---|---|---|---|---|---|
| **D-3** | **247**（全体）| **95.95%**（10 条不同）| **94.33%**（14 条不同）| 96.36% | old↔new 的分歧率（4.05%）**低于**地板（5.67%）⇒ **分歧没超出噪声** |
| **D-9** | **8**（全体）| **87.50%**（1 条不同）| **87.50%**（1 条不同）| 87.50% | 那 1 条**同时**是地板分歧 ⇒ **可归因于锚点块的 = 0 条** |

⚠️ **D-9 的 n=8 不是抽样小，是总体就这么小**（能触发 D-9 闸门的样本全库只有
8 条）⇒ 它能给的**上限**就是「**未观测到效应**」，**不是**「无效应」。

※ **「分数一致率」那一列不是「准确率一致率」**——`score_d3` 不与任何金标比对，
详见下面那条「**M-33**」小节。

**逐条读 evidence**（本仓库的硬规矩：不逐条读就不算核对过）——D-3 的 10 条
分歧里，**5 条落在噪声地板内**（可归因 = 0），**另 5 条地板没分歧**。

**判读规则**（读的是 evidence 原文，不读分数）：新标签**是否符合类别散文的定义**、
**且观察证据支持它** ⇒ 「新版更准」；两个标签**在定义下同时成立** ⇒ 「方向不明」。

| 行 | old → new | 地板 | 判读（读的是 evidence 原文）|
|---|---|---|---|
| `E006__2729220b__site-5` | ambiguous(3) → **irrelevant**(4) | 同 new | **新版更准**：old 的 evidence 只有「Sorry, you have been blocked」，它**没认出这是什么页**；新版靠 URL 路径认出「济州美食博客」⇒ 类别散文里 `irrelevant` 写着「电商、博客、论坛其他主题」 |
| `E023__4d175424__site-3` | ambiguous(3) → **playable**(5, 会员制) | 同 new | **新版更准**（但依据是**站点形态推断**）：新 evidence 写「会员方案 / 全 5 季 / 集数 62」⇒ 类别散文的 `playable` **含付费子分类**（`subscription` = 需订阅/付费）⇒ 归 `playable` 合定义；old 盯着 `video=0 iframe=0` 说「说不清」，而 **`ambiguous` 的定义是「既可能是导航聚合也可能含内嵌播放器」**——这一页不属那种。⚠️ 观察里**确实没有播放控件** |
| `T030__ae6553b1__site-10` | playable(5, 免费) → **nav_aggregator**(5) | 同 new | ⚠️ **方向不明**：见下——**这行与 `T048` 那行互判相反**，单看这一行说「新版更准」站不住 |
| `T057__c39e54c7__site-2` | ambiguous(3) → **irrelevant**(5) | 同 new | **新版更准**：**404 错误页**（title=404错误页面，body=返回首页）⇒ 确定 irrelevant；old 说「说不清」 |
| `T048__0680dc4a__site-10` | nav_aggregator(5) → **playable**(5, 免费) | 同 new | ⚠️ **方向不明，且暴露一条口径问题**：见下 |

⇒ **5 条里 3 条新版更准、2 条方向不明（那一对互判相反）、0 条更差**。
**锚点块没有制造退化**；它把一批原本「说不清 / 判错」的判成了更具体的档
（old 判 `ambiguous` 的 **51** 条里 **4 条**在新版被判成具体类别，其中 **3 条**是候选效应行）。
⚠️ **机制只说到这里为止**：锚点块里**只有档位释义、没有类别词表**（见下面那条开口）
⇒ 它**不能**直接教模型挑哪个类别 ⇒「新版更准」**不能读成「锚点块教得更准」**，
只能读成「**这一批样本上**新版更准」。

**但逐条读读出一条真的口径问题**（不是 A/B 的副产品，是它照出来的）：

> `T030__ae6553b1__site-10` 与 `T048__0680dc4a__site-10` **是同一个 URL**
> （`http://56dy.com/movie/2842`，同一任务「功夫」的两份存档）——两份**观察逐字段相同**
> （`video_tag_count=0`、`iframe_count=0`、`body_len=1553`、`links_total=45`、
> 同一标题），**只差一个轮播计数**（今日更新"92"部影片 / "87"部影片）。
> **新版把前者判成 `nav_aggregator`、后者判成 `playable`**，**互判相反**。
> ⚠️ **旧版在这两条上也互判相反**（旧版 #1=playable、#2=nav_aggregator）
> ⇒ **锚点块既没造成这个不一致，也没修好它**——**同一版提示词对同一页给了两个标签**。
>
> ⇒ 这是 **D-3 rubric 的一条开口**，而且**比「界线没定死」更具体**：
> **类别散文里 `playable` 与 `nav_aggregator` 的定义在这一类页面上同时成立**——
> `playable` = 「能直接播放目标视频的站点」（经「量子云」外链**能看**），
> `nav_aggregator` = 「把用户引到外部播放站，**自身不存正片**」（`video=0 iframe=0`、
> **无内嵌播放器**）⇒ **散文没有区分「站内播放」与「经外链跳转播放」**。
> ⚠️ **而且没有任何机械面拦得住它漂**：[rubric D-3 卡](在线视频场景rubric.md) 里
> `playable` / `nav_aggregator` / `review` / `irrelevant` **零出现**（卡里是
> 「播放站 / 导航站 / 无关站」**三**说法），词表的唯一定义处是 `d3_scorer.py` 的
> `frozenset` + **手写散文**；`anchors_drift()` 只核**档位文本** ⇒ **词表可以无声漂移**。
> **不归 A/B 管**（那是 rubric 变更，走 §5.5 条款级流程），但它是真的，
> **已记进 [TODO 台账](../计划进展/10-TODO台账.md)**（**Q-14** 拍口径 ·
> **M-32** 词表的机械面 · **M-33** 顺带钉死的读数口径）。

**D-9 那 1 条**（`T040__7b37c195__site-3`）：old=hindsight(3)、new=systematic(2)、
new2=hindsight(3) ⇒ **地板也分歧** ⇒ **归因不到锚点块**（读 evidence 也确实是
hindsight 与 systematic 之间的细微差别，不是「锚点把判断带歪了」）。

#### ⚠️ 顺带钉死一条读数口径（M-33）：「分数一致率」**不是**「准确率一致率」

上表那列「分数一致率」**不能读成「两边都判对了」**。`score_d3`
（`d3_scorer.py` 第 471–477 行）**全程不与任何金标比对**——它把
「`category ∈ D3_CATEGORIES`」当作「判对」，evidence 具体即 5 档；
**理由串却写着**「category **判对** + evidence 引用观察具体短语——rubric §D-3 锚点 5」。
而 rubric 的 5 档是「**判对**，且选中/排除理由引用了观察中的具体依据」、判据栏写的是
「**对照人工类别标注（黄金集预标）**」⇒ **「人工类别标注」那一栏至今是空的**
（[对齐账 §2 D-3 缺口②](02-rubric与代码对齐.md)）⇒ **评分理由高报了它检查过的东西**。

⇒ **分数一致率 96.36% 只说明**：两边都**落在契约 5 类内**、且 evidence 的具体程度相同。
⇒ **已记进 [TODO 台账](../计划进展/10-TODO台账.md)**（**M-33**：理由文案要先改，
分数语义要等人工类别标注——**不是改文案能解决的**）。

**结论（不夸大）**：

1. ✅ **新指纹 `1046e67994aa` 不是「算出来的」，是「验过的」**：在**候选全体**上，
   锚点块带来的读数漂移**不超过噪声地板**（D-3 分歧率 4.05% < 地板 5.67%；
   D-9 可归因 0 条）。
2. ⚠️ **可说的只是「未观测到退化」**：D-3 的 5 条候选效应分歧里 **3 条向好、2 条方向不明**；
   D-9 的总体只有 8 条。**「没测出退化」≠「证明没退化」**——但这一轮测的是**全体**，不是抽样。
3. ⚠️ **本轮没有、也不该有**「锚点块让 D-3 更准」的结论：那 5 条分歧**方向不一致**
   （3 条向好、2 条方向不明且互斥），**且机制未验证**（锚点块里没有类别词表）⇒
   只能说「**这一批样本上**新版更准」。它确实让 D-3 **更愿意给出具体档**
   （`ambiguous` 那一栏 old 51 条里 4 条被解析掉了），**代价是
   `playable` ↔ `nav_aggregator` 那条界线更不稳**（见上面那条开口）。

#### 软维度金标写入链实施结果（2026-10-24，分支 `main`）

**这一段补的不是新能力，是一条断掉的链**。回路的两半（读数 / 抽样 / 分诊 / 闸门）
都建完了，但**三分之二的读数是空的**：`annotations.jsonl` 的 `dimension_scores`
一条都没填 ⇒ v8 快照 1133 条里 **844 条 `ungraded`**、条款级一致率只覆盖 D-10、
T-2 / T-3 恒测不成、抽样 S2 / S3 两层**总体为 0**。

**为什么一直没填**：缺的三样东西，缺一样这件事就做不成——

| 缺 | 表现 | 落点 |
|---|---|---|
| 一张能填的**表** | 只有 `annotations.jsonl`（整行 JSON）；判 D-2/D-4/D-5/D-6 **要看现场**，表里没有现场，人就只剩 pred 可抄 | `calibration/goldwork.py` · `goldwork worksheet --version N`（每格 `anchors` / `scene` / **`record`**（含 `archive` 路径与 `archive_exists`）/ `pred_*` / `existing`） |
| 一条**只写分数**的写入路径 | `annotate` 要 `label` 必填（补分数的人得伪造一个标签），且 `dimension_scores: null` 会把已有分数整行抹掉 | `golden/dim_scores.py` · `annotate --dimensions-only` |
| 一个**进新版本**的出口 | `freeze --from-version` 是「行内容不动」⇒ 今天填的分数**永远进不了新版本** | `builder._refresh_gold` · `freeze --refresh-gold` |

**三条设计决定**（每条都在挡一类「看起来正常」的错）：

1. **`null` 是结论、空串是没填、整栏省掉是错误**。三者进快照都是 `ungraded`，
   但覆盖率分得开；把「没填」读成「判不了」，半张表会被静默写成一堆 `null`
   （`group_fills` 因此对**缺 `gold_score` 键**的行直接抛，而不是 `dict.get` 成 None）；
2. **判过没判过看键在不在，不看值**。第一版拿 `previous.get(dim)` 判断，
   `{"d5": None}` 与 `{}` 因此等价 ⇒ 人判的「判不了」**写不进去**（变化检测读成
   unchanged）、也**拦不住覆盖**（`null` 值被当成没填）——这条是写完第一轮测试
   才发现的；
3. **金标变动 = 新基线**（§10「改金标要新版本 + 人裁」）。`freeze --refresh-gold`
   落 `gold_refresh.baseline_reset=true`，`gate.assert_gold_comparable` 读到就
   **退 2**（下一对可比是 vN → vN+1）。不这么做，填完金标顺手比一版会读到
   「净改善 +N」——**那 N 是尺子换了**，而报告上看不出异常。

**只换一栏**：`--refresh-gold` 只动 `golden.dimension_scores`；`golden.label` /
`note` / `reviewed_by` / `code` / pred 侧**一个字不动**（Q-11「按访问」⇒ 不重标，
这条路上没有东西被重判）。谁填的分数记在 `dimension_reviewed_by` /
`dimension_reviewed_at`，**不覆盖标签的原署名人**——那是两笔账。

**读数（2026-10-24，v8 调参池）**：表 **364 格**（91 行 `scored` × 4 维）；
**37 行无样本不进表**（按维度单列 `no_sample`）；现场 **94/94 定位到存档**；
**待填 364 / 已填 0 / 人判判不了 0**——**工具齐了，填表是人做的**。

| 测试 | 条数 | 封的是 |
|---|---|---|
| `tests/unit/test_golden_dim_scores.py` | 34 | 格子语义（空串 / null / 整栏省掉）· 白名单 · `True` 是 `int` 子类 · 只补已有标注 · 必须有人签 · 幂等与 `--force` · 只动分数那几栏 |
| `tests/unit/test_calibration_goldwork.py` | 23 | 维度集从 `DIM_FACES` 投影（不手抄）· 一格五样上下文（含 `record.archive` 口径与 `archive_exists`）· 无样本单列 · 内容键预填（L-19）· 确定性 · 表不写 annotations · CLI 不覆盖 / 空档退 2 |
| `tests/unit/test_golden_dim_writeback.py` | 11 | `annotate --dimensions-only`（标签一字不碰 / 空格退 2 / 没人签退 2 / `--force`）· `freeze --refresh-gold`（只换一栏 / 逐行可比 / 刷空退 2 / 落 `baseline_reset`）|
| `tests/unit/test_calibration_gate.py` | +2 | 重挂过金标 ⇒ 退 2 且提示下一对可比；**基线**刚重挂过 ⇒ 它跟后继正是可比的那一对 |

### 9.7 已裁定的三条对数字的影响（2026-10-22）

D-C8 / D-C9 / D-C10 已按建议定，**三处数字因此确定下来**，写在这里免得以后把 191 与 131 读成矛盾：

| 动作 | 头条 D-10 分母 | 说明 |
|---|---|---|
| 现状（v6 报告口径）| **289** | calibration + holdout 混算（C-04 要修的就是这个）|
| C-04 落地后（v6 重报）| **188** | 只算 calibration 的**站点级**行（`v6/calibration.jsonl` 有 191 行，其中 3 条是轨迹级）|
| C-17 再切回归集 60 条后（v7）| **128** | 调参池 = 188 − 60；回归集 60 条**单独出数**，不与头条混算 |

> **2026-10-22 更正**：本表原写 `191` / `131`，那是**行数**；D-10 的分母只认站点级行（`source_kind != "p2_sample"` 且已标注），所以正确读数是 `188` / `128`。两个数都对，但混用会让"分母从 191 掉到 128"看起来少了 63 条，而实际切走的回归集是 60 条。三个数在任何报表里都要写清是行数还是站点级。

回归集这 60 条**只出三类计数**（`fixed` / `regressed` / `unchanged`），**不出 D-10 百分比**——出了就会被拿去跟头条比，而两者分母不同义。这三类计数要**两轮快照差分**才出得来，属于 C-08（✅ v1.7 已落：`verify` 打的就是这三类 + 两侧单列）。

### 9.8 依赖链

```
C-01 ✅ ─> C-02 ✅ ─> C-15 ✅ ─┬─> C-16 ✅ ─> C-19 ✅   （读数与抽样，v2.2 落地）
                               ├─> C-08 ✅ ─> C-09 ✅   （回归门）
                               └─> C-05 ✅ ─> C-06 ✅ ─> C-07 ──> （决定 H-02 方向）
C-03 ✅ ─┬─> C-04 ✅
         └─> C-17 ✅ ──────────> C-08 ✅
C-10 ─> C-11          （可与 R1 并行，纯结构改造）
C-12 ✅ / C-18 ✅      （变更记录九列 + 条款级 diff 守卫，已落）
C-13 / C-14           （散活，随时插）
```

**第一周建议（v1.8）**：C-01 ✅ → C-02 ✅ + C-15 ✅ → C-03 ✅ + C-04 ✅ + C-17 ✅（一次重冻结拿到三档）→ C-08 ✅（拿到回归门）→ C-09 ✅（门有人跑）。**R0 与 R0.5 清空**，且不欠裁定。D-C8 / D-C9 / D-C10 已于 2026-10-22 裁定，D-C5 随 C-03 一并落地。**v1.9：C-05 ✅ 落地，R1 的桶表这一段清空。** **v2.0：C-06 ✅ 落地，表能造、能校验、能算覆盖率；填表仍是人做的，所以 C-07 仍未开。** **v2.1：C-12 ✅ + C-18 ✅ 落地（R2 的第一段），rubric 改动从此可机械证明「只动了列出来的条款」——而这道守卫今天「测不成」（还没有可比的两版快照），如实退 2。** **v2.2：C-16 ✅ + C-19 ✅ 落地（R1 的另一半）：R1 现在只剩 C-07，而它等人填表。两条新命令都把「没测到什么」喊出来**——`triggers` 喊「4 个条款一条都判不了」，`sampling` 喊「缺 20 条，没凑」。

**下一步 · 三条可并行**（R1 已清空：C-05 / C-06 / C-16 / C-19 / **C-07 ✅**；R2 一半已落地）：

- **C-07 已闭环（2026-10-23）**：33 张卡由人签完（`triage_by=reesn` × 33），
  分布 = **评估器 12 · 金标 14 · 采集 7 · rubric 0**，`bucket_clear` 第一次有真实输入
  （`evaluator_logic` 残留 11）。**下一步不是重跑它，而是按它的读数分活**：
  金标 14 条最重（M-30 那一族）、评估器 11+1 条次之（M-26 / M-29 那一族）、
  采集 7 条最轻。**再等一轮分诊才有趋势**——现在只有 v7 单版横截面；
- **C-10 → C-11（锚点表化 + prompt hash）**：R2 剩下的那半，同样不依赖分诊。
  C-11 还是 **T-2 / T-3 的前置**——judge 逐行 pred 不落 `golden_id`，kappa 那两条
  就永远测不成；
- **填软维度金标**（不是 C-xx 编号，是 L-04 / M-18 那条前置事实）：v7 的
  D-2 / D-4 / D-5 / D-6 全 `ungraded`，于是条款级读数只覆盖 D-10、S2/S3 两层
  抽不出人。**它现在是「三分之二的读数是空的」那根杠杆**，比再开一个 C-xx 划算。

⚠️ 闸门现在**四条判据全都能说话**了：第四条「同桶清零」**第一次拿到真实输入**
（分诊档 33/33），实测 `❌ bucket_clear evaluator_logic 残留 11`（exit 1 驳回）。
`n/a 不算通过` 的口径不变——**`n/a` 只是"这次判不了"，不是"这次过了"**；
反过来也一样：**`bucket_clear` 打 ✅ 只说明目标桶清零且分诊覆盖完整，不说明别处没变差**
（另外三条判据各管一段）。⚠️ 第四条判据仍然**跨版才有意义**：同版对同版时
`net_improvement` 恒为 0（D-C11 的结构性后果），拿它单打一屏就会读成"什么都没改善"。

⚠️ **第四条判据的影子**（v2.6 记录，即台账 **M-29**）：`bucket_clear` 判的是
**快照里的 pred**，而快照的 pred 侧取自**冻结行**（`code:frozen_row`）——所以
**改评分器代码不会改变任何一版快照的 pred**，除非重新冻结并刷新分数。换句话说，
「修好 D-5 ⇒ 下轮分歧变少」这条路今天**是断的**：不是判据不灵，是数据没换。
M-29 的修法（继承式重冻结 + `--refresh-scores` 按 `golden_id` 重算冻结行的分数）
就是补这一段；D-5 的恒真修复本身只改了代码，**回路还看不见它**。

⚠️ C-09 的形态定案（2026-10-23，owner 选「冻结时闸门」而非 GitHub Actions）：门挂在 `freeze` 之后，**不挂 push**。理由是结构性的——快照的 pred 侧取自冻结行，同一版对同一版恒为净改善 0，按 push 跑的门永远红。将来若真接 CI（远端复算），接的也是 `gate`，且**退出码 1 与 2 必须分开报**、基线必须是**同一份切分**的两版。

**为什么不是先出归属分布**：回归门晚三天，但之后每次改动都可证伪；反过来先分诊，第一轮改动就没人能验——而"改完数字涨了"恰恰是最容易自我欺骗的形态。**先有护栏，再做诊断。**

### 9.9 闭环判定

每条任务算"完成"，三条都要满足（沿用 [10-TODO台账](../计划进展/10-TODO台账.md) 的判定标准）：

1. 代码里能指到实现（文件 + 行）；
2. 有测试封着它，且**真的会跑**（不是 skip）；
3. 有记录（本节 / 台账 `C-xx` 条目）。

**只满足 1 的是"写完了"，不叫闭环。**

**未闭环项的活页版在 [10-TODO台账 · §校准回路（方案 16）](../计划进展/10-TODO台账.md#校准回路方案-16-未闭环)**
（2026-10-23 起）：`C-` 编号与台账的 `H/M/L` 不混——前者是**回路内部的施工顺序**，
后者是**全域的阻塞程度**；判定闭环用的是同三条标准。本文 §9 记"做完了什么、换来了什么口径"，
台账记"还差什么、下一件是哪条"——**两边都不抄对方的活**，改一侧时顺手核另一侧还成不成立。

### 9.10 与 H-02 的关系

C-01～C-19 **不直接提升 D-10**，它们做的是"让 D-10 的每一 pp 都能被指认"，以及"让每次改动改坏了没有能被判出来"。
D-10 从 76.5% 到 90% 仍要靠 H-02 的三条路（LLM prompt 改善 / 重采集 / 新分支黄金集），
但**哪条该先走、走完算不算数，由 C-07 与 C-08 判定**——这正是本方案对主线的全部价值。

---

## 10. 验收信号（机械可查）

| 信号 | 期望 |
|---|---|
| 分歧归属率 | **100%**，无 `other` 桶。**桶表本身已封闭**（C-05 ✅：`MAX_BUCKETS = 6` import 期封顶、`other` 不在 `BUCKETS` 里）——所以「无 `other`」现在是**结构上不可能**，不再是纪律要求。**已达成（C-07 ✅，2026-10-23）：v7 为 33/33 = 100%**，机械版 = `calibration.worklist status --version N` 退 0（退 1 = 还有未分诊，退 2 = 测不成）|
| rubric 变更 | 每条 ↔ **≥1 条分歧** + 人裁记录 + 责任区分声明（**机械版 = `rubric_changelog check [--verify-cards]`，C-12 ✅**；「评估器做不到」为由 = 借口词命中，驳回）|
| 内环改动 | 净改善 > 0 且无类回归；否则红（**机械版 = `verify` 退出码 1；有人跑它 = `gate`，挂 freeze**）|
| 版本元数据 | 每个冻结版本有 `rubric_version` + `evaluator_version` + `holdout_runs` |
| 桶收敛 | 同桶连续两轮不清零 → `escalated`，进 [10-TODO台账](../计划进展/10-TODO台账.md) |
| 等价类变更 | 每次合并都有**代码侧注记** + "合并后不可见的细分"清单（**D-C4 裁定：不需 owner 签**）⇒ **机械版 = `alignment.validate_merge_notes()`（C-13 ✅，2026-10-23）：注记与表逐标签对齐，对不上即红** |
| **触发信号**（v1.3）| T-1～T-4 都能一条命令出读数并带 `n`；**阈值未定线时命令报"未定线"，不默认通过**。**已落（C-16 ✅）= `calibration.triggers --version N`**：报告里 `judged` **恒为 `None`**（退出码只有 `0` / `2`，**没有 1**——本命令没有「合格」这个结论可给）；`n = 0` 的条款不出读数、`coverage` 单列「一条都判不了」的条款（`0 条 ≠ 0`）|
| **条款级可比性**（v1.3）| 改版后未列条款的分数**逐位不变**（机械 diff，CI 红即驳回）。**已落（C-18 ✅）= `rubric_changelog audit --before N --after N`**，退出码 1 有条款动 / 2 测不成（含**受检条款一条都没配上**——`0 条样本 ≠ 一切正常`）|
| **回归集**（v1.3）| 每次改动 `regressed == 0` 且 `net > 0`；回归集金标本身变动 = 新版本 + 人裁（**机械版 = `verify` 的第二条/第四条判据**，金标变动直接退 2）|
| **分层抽样**（v1.3）| 每轮人工样本带 `stratum` + seed，可复现；层内读数不冒充总体。**已落（C-19 ✅）= `calibration.sampling --version N`**：判据全部机械可算 + **SHA-256 定序（非 RNG）**⇒ 同一输入两次抽样逐行相同；配额缺口写 `short_by`、**不跨层借人**；`sample_weight = N_h/n_h` 只说「这条有多重」，**不做总体外推** |

---

## 11. 不做的事与风险

### 11.1 不做

- **不做自动调参**：没有改参入口是特性不是缺陷（[11 号 §3.5 铁律 4](11-外部方案对账.md)）；
- **不做投票 / 集成**：judge 与代码分歧要暴露，不平均（CLAUDE.md 红线）；
- **不让桶表长到第 7 项**；
- **不改 P1/P2/P3 契约与 22 类分支封闭集**（[15 号 D-A10](15-执行器与agent-runtime重新定义.md)）。

### 11.2 风险

| 风险 | 表现 | 缓解 |
|---|---|---|
| **人成为瓶颈** | 第一轮 73 条要人分诊（**实况 v2.5：v7 一轮 33 张卡**；`evaluator_logic` 11 条**没有一条预填**，是最贵的那一段）| `evaluator_logic` / `evidence_missing` 可由机器预填建议值（`suggested_by=agent`，人只确认）；`rubric_*` / `gold_*` 永远留空待人填。与 INC37 `auto-validator-l3l4` 四档裁决同构：人和机器的活分开记账 |
| **把 D-10 当唯一目标** | 最省力路径永远是合并等价类 | 加覆盖度约束：进步必须同时满足"等价类内细分不变差" |
| **rubric 变更被稀释** | 每次为提高数字改一条边角条款 | 外环四条强制 + 责任区分声明 + 新版本冻结 |
| **分诊质量不可测** | 人随手贴桶 | `triage_evidence` 必填；抽检 20% 复核；`gold_defect` 与 `rubric_*` 桶需二次签 |
| **新机制与主线脱节** | 方案落了但没人跑 | 把"分歧归属率 100%"写进 R1 验收；不进 CI 的机制都会烂尾 |
| **回归集被调参吃掉**（v1.3）| 反复看同一批 60 条、按其逐条结果改 prompt ⇒ 回归集变成第二个调参池，过拟合后门禁形同虚设 | 回归集逐条明细**只给 owner**；改判据的人只看汇总计数与条款分布 |
| **分层样本冒充总体**（v1.3）| 在分层抽样上算 kappa 当总体值 | 报层内读数 + `sample_weight`；**不做总体外推**（D-C10）|
| **触发阈值自己漂**（v1.3）| "差一点"就把线调低，触发机制变成装饰 | **阈值唯一真源 + 各处只从表取值 + 契约测试盯「表不腐 / 不留字面量」**（C-14 ✅，2026-10-23）；⚠️ **D-C6 裁定「阈值调整不算 rubric 级变更」**⇒ 阈值**不**升 rubric 版本、**不**进 rubric 变更记录——防漂的力气花在**表与测试**上，**不是**签名上（D-C9 原写「定线走 rubric 级变更」那一半已作废）|
| **条款号与实际判据脱钩**（v1.3）| `rubric_ref` 是手填字符串，改了条款没人改映射 | `DIM_CLAUSES` 唯一真源 + contract test（锚点与条款双向可映射）|
| **评分器改了但回路看不见**（v2.6，= 台账 M-29）| 快照 pred 侧取自**冻结行**（`code:frozen_row`），而代码评分器**故意不带版本号**（C-01 口径 2）⇒ 改了评分器，任何一版快照的 pred 都不动，`bucket_clear` 的下一次读数与改动无关。表现为「修了但数字没变」，会被读成修错了 | 继承式重冻结 + `--refresh-scores`（按 `golden_id` 重算冻结行分数，缺 id 一律 fail-closed）；**并把它写进 freeze 的 manifest**（`score_refresh` 前后对照 + 三态计数），否则刷新与没刷新看起来一样 |
| **持有集不独立**（v2.6，= 台账 M-30 / Q-12）| 切档是**按行不按 URL**：289 站点行只对应 131 个 URL，**25 个 URL 跨档**（16 个三档全占），持有集 101 行里 **65 行落在跨档 URL 上** ⇒ 「持有集数字」不是独立样本的读数，D-C5 的封存纪律在 URL 层面被绕过 | 切档单位改 URL 级（Q-12 待 owner 裁）；在此之前**每条持有集读数都要带这句限定**，且回归集逐条明细不外发（已在做）|

---

## 12. 变更记录

| 日期 | 版本 | 说明 |
|---|---|---|
| 2026-10-22 | v1 | 初稿。现状核对（三个洞）+ 分歧卡契约 + 六桶封判 + 双回路门槛不对称 + 四条防放水规矩 + R0–R3 落地顺序 |
| 2026-10-22 | v1.1 | **§9 展开为任务执行计划**：R0–R3 细化为 `C-01`–`C-14` 十四条，带依赖、验收、成本、拍板归属；补依赖链图、第一周建议、闭环判定三条标准、与 H-02 的关系（本方案不直接提 D-10，只让每一 pp 可指认）|
| 2026-10-22 | v1.2 | **C-01 实施并闭环**：新增 `evaluation/scorers/rubric_version.py`，`cmd_freeze` / `cmd_agreement` 落尺子三字段，补 9 条契约测试（全量 3493 条通过）。记录三条口径（老档不补写 / 指纹只覆盖 D-3·D-9 / D10 pred 侧来自冻结行）与 `holdout_runs` 暂恒为 0 的原因 |
| 2026-10-22 | v1.3 | **补五处机制**：① [§5.4](#54-量化触发v13-新增) 量化触发 T-1～T-4；② [§5.5](#55-修改粒度条款级v13-新增) 条款级修改粒度（`rubric_ref` / `DIM_CLAUSES` / 未列条款逐位不变）；③ [§5.6](#56-回归集黄金校准集v13-新增) 回归集（黄金校准集）与 `regressed == 0` 门禁，**排到分诊之前**（R0.5）；④ [§5.8](#58-动机链三个必填栏v13-新增) 变更记录三列动机链；⑤ [§5.7](#57-人工抽样按分歧类型分层v13-新增) 分层抽样 S0–S3。新增任务 C-15～C-19、决策 D-C8～D-C10。**两处对原需求的修正**：代码↔LLM 分歧率只能定义在 D-4（其余条款只有一个评分者）；"LLM 自评置信度"不可用（scorer 里是写死常量），改用证据锚定代理 |
| 2026-10-22 | v1.4 | **D-C8 / D-C9 / D-C10 按建议裁定**：回归集 = calibration 切 60 条（调参池 191 → 131）；新触发阈值只出读数、定线走 rubric 级变更；分层抽样 40 条/轮（S1/S2/S3/S0 = 10×4）+ 固定 seed + 不外推总体。新增 [§9.7](#97-已裁定的三条对数字的影响2026-10-22) 记录三处分母（289 → 191 → 131），C-16 / C-17 / C-19 不再等拍板。⚠️ **v2.7 追记**：「定线走 rubric 级变更」那一半**已被 D-C6 推翻**（定线只改阈值表的 `status`）|
| 2026-10-22 | v1.5 | **C-02 + C-15 实施并闭环**：新增 `evaluation/calibration/{snapshot,disagreement}.py` 与 `--emit-disagreements`（只读派生，缺 `--version` 即抛），`rubric_version.py` 加 `DIM_CLAUSES` / `clause_of`，补 17 条契约测试（全量 3510 条通过）。v6 实测：快照 856 条（agree=215 / detail_mismatch=1 / disagree=73 / ungraded=567）、分歧卡 74 张（eq_class_mismatch=73 / label_mismatch=1），与 `口径 C：216/289 = 74.7%` 同分母对得上。记录四条口径（`ungraded` 不是分歧 / 卡不含回归信息 / `detail_mismatch` 与 `disagree` 分开记账 / 容差内 `tier_gap` 也记幅度）|
| 2026-10-22 | v1.6 | **C-03 + C-04 + C-17 实施并闭环，R0 清空**：owner 裁定「以 v6 冻结档为准（74.7%）」，D-C5 一并落地。新增 `evaluation/golden/splits.py`（三档词汇唯一真源），`freeze` 增 `--from-version`（继承式重冻结）与 `--regression`，`agreement` 头条只认调参池 + 旧口径同时输出 + 回归集整行指纹守卫；**v7 = v6 行原样 + 三档切分**（131/60/103 行，站点级 128/60/101）。补 15 条测试（全量 3525 通过）。**两处更正**：① [§9.7](#97-已裁定的三条对数字的影响2026-10-22) 的 191/131 是行数，D-10 分母只认站点级（188/128）；② v1.5 那条"卡上 `split` 会出现 `regression`"的判断是错的——分诊池 = 调参池，回归集与持有集都不出卡（v6 卡数 74 → 49，v7 33）|
| 2026-10-23 | v1.7 | **C-08 实施并闭环，R0.5 只剩 C-09**：新增 `evaluation/calibration/verify.py`（两轮快照差分 → 四条判据 → **退出码 0/1/2**）与 29 条测试（全量 3554 通过）。**五条口径**：① 五桶（三类计数 + `still_wrong` / `unverifiable` 单列）；② 配对键只有 `(golden_id, dimension)`，重采集的行配不上（**"改评估器 → 重采集"这条回路现在还判不了**，需快照加案件身份，属后继改动）；③ 不可比输入一律退 2（键重复 / 换过档 / 回归集键集或金标变了 / schema 不同），**1 ≠ 2**；④ 同桶清零要求分诊表覆盖本轮全部分歧，否则 0 残留不可信；⑤ `detail_mismatch` 算对、`tier_gap` 算错。实测：v7 vs v7 → 净 0、退出码 1（**没改东西就没有"改善"可主张**）；v6 vs v7 → 退 2（60 行跨切档）。H1-α 回放（修 4 / 伤 6）两条独立理由同时驳回。记录三处：命令速查（新增「黄金集与校准回路」一节 + 必填项一行）· 运行手册（`calibration.verify` 两轮对比）· 避坑指南 §8.18 + 自查表一行 |
| 2026-10-23 | v1.8 | **C-09 实施并闭环，R0.5 清空**：形态由 owner 裁定为**冻结时闸门**（新决策 **D-C11**，不接 GitHub Actions）——快照 pred 侧取自冻结行 ⇒ 同版对同版恒为净改善 0 ⇒ 按 push 的门必然永远红。新增 `evaluation/calibration/gate.py`（定基线 → **每次重算**两版快照 → 交 `verify`；执行码 0/1/2）与 20 条测试（全量 3574 通过），`verify._load_triage` 提为 public `load_triage`（分诊表走同一份加载器）。**五条预检全退 2**：无冻结版 / 首版无基线 / 基线与本轮同版 / 基线晚于本轮 / 冻结档出不了快照。三条口径：每次重算不复用旧快照 · 出快照与人工跑 `agreement` 是同一份实现（逐字节比对测试）· `gate_meta` 进报告。实测：缺省 v6→v7 退 2（跨 C-17 切档，提示"第一对可比的是本轮与它的后继"）；同版对同版退 2；不存在的版本退 2。**今天没有可判的一对，第一次真正判是 v8 冻结之后**。记录三处：命令速查 + 运行手册（`calibration.gate` 冻结时闸门 + 门禁节）· 避坑指南 §8.18 补闸门三条预检 + 自查表 · 架构总览补 `gate.py` |
| 2026-10-23 | v1.9 | **C-05 实施并闭环，R1 的桶表段清空**：新增 `evaluation/calibration/buckets.py`（六桶封闭集 + `MAX_BUCKETS` import 期封顶 + `BUCKET_PRECONDITION` / `BUCKET_OWNER` / `BUCKET_LOOP`，`HUMAN_ONLY` 由 `("rubric_","gold_")` 前缀**推导**）与 `evaluation/calibration/triage.py`（`validate_rows` 是**唯一写入口**；身份 → 缺字段 → 桶 → 维度 → D-C3 → 责任 → 状态；默认 dry-run、默认拒覆盖、幂等只比 bucket/by/evidence），`evaluation/golden/data/triage/README.md` 记字段表。**验证器不再自带加载器**：`verify.py` 的 `load_triage` 删掉改 import triage 那一份，`analyze` 里**再校验一遍**（CLI 与公开 API 两条路同一套规矩），`gate.main` 同时吃 `TriageError` → 退 2。测试 +44（triage 33 + 契约 11，全量 3618 通过）。**三条口径**：① `accepted_exception` 是状态不是桶（`STATUSES` 里，不在 `BUCKETS` 里）；② 身份识别用**显式名单 + 命名空间前缀**两张表，不用子串猜（`agent-smith` / `Qwen` 当人）；③ `triage_at` 不参与幂等判断。**C-05 落地的不是"同桶清零通过"，是"分诊档有资格被填"**——档里还是空的，`verify --triage` 仍打 `n/a`。记录四处：命令速查 + 运行手册（`calibration.triage` 六桶表 + 三条硬规矩）· 避坑指南 §8.19 + 自查表 · 架构总览补 `buckets.py` / `triage.py` / `data/triage/` |
| 2026-10-23 | v2.0 | **C-06 实施并闭环**：新增 `evaluation/calibration/worklist.py`（**机器预填建议值 + 人在同一处确认或改**，三层分离：派生 worklist → 人填 worksheet → 版本化分诊档；`PREFILL_RULES` 是**唯一真源**，import 期 `check_rules` 拒绝指向 `HUMAN_ONLY` 的规则；worklist 行**没有 `bucket` 键**——建议值走 `suggested_bucket`，结论列留白），`triage.py` 增 `import_worksheet` / `strip_row` / `incomplete`（**空表与半填都拒**，导入时剥掉上下文只留六字段 + 建议值）与 `triage import` 子命令。测试 +37（worklist 34 + 契约 3，全量 3670 通过）。**v7 实测：33 张卡，有建议 12（全为 `evidence_missing`——采集降级 6 / 未选中站点 4 / 遍历超时 1 / 哨兵无法区分 1），待人判 21，冲突 0，覆盖率 0/33。** **两条口径**：① `evaluator_logic` **故意不预填**——"不是提示词"推不出"一定是代码逻辑"（`rubric_*` / `gold_defect` 仍在候选里），登记那种规则就是猜；② 建议值不许变成结论——契约测试用 `inspect.getsource` 封着"worklist 产不出 `bucket`、也调不到 `add_rows`"。记录四处：命令速查 + 运行手册（`worklist build/status` + `triage import` 五步流程）· 避坑指南 §8.20 + 自查表 · 架构总览补 `worklist.py` · `data/triage/README.md` 补批次流程 |
| 2026-10-23 | v2.1 | **C-12 + C-18 实施并闭环（R2 的第一段）**：rubric 变更记录表 **4 列 → 9 列**（新增 变更类型 / 预期效果 / 分歧卡 / 责任区分声明，并补「修订人」），配一节**列契约**；新增 `evaluation/scorers/rubric_changelog.py`（**解析 markdown 当唯一真源，不落第二份 JSONL**）：`check` 校列契约 + 变更类型封闭集 + 动机链必填 + 卡号存在性，`audit --before N --after N` 用 `条款` 列当白名单逐条款比两版快照。测试 +67（单测 61 + 契约 6，全量 **3737 通过**）。**三条口径**：① **`measurable`——受检条款一条都没配上 = 测不成 → 退 2**，因为 `freeze --from-version` 把分数原样带过来时守卫会印 `✓` 而一条都没比（避坑指南 §8.21，冒烟时踩出来的）；② **白名单取自 `条款` 列**，改 `D-3` 时 `D-3.5` 自动算已列（派生根双向覆盖），通配 `*` 则如实退化成「什么都不断言」；③ **追溯线前向**：`TRACEABLE_SINCE = "1.3"` 之前的老行允许填 `—`，不补假卡号。**今天 `audit` 测不成**（v8 未冻结，如实退 2）；`调权重` 是**空类**（无权重表），登记了但跑不到。记录四处：命令速查 + 运行手册（`rubric_changelog` 两命令 + 五条口径）· 避坑指南 §8.21 + 自查表 · 架构总览补 `rubric_changelog.py` |
| 2026-10-23 | v2.2 | **C-16 + C-19 实施并闭环（R1 的另一半）**：新增 `evaluation/calibration/triggers.py`（**触发读数唯一出处**：T-1～T-4 全从 C-15 快照派生 · `line_set` 区分「已有线」与「候选线」· `judged` **恒为 `None`** · `ungraded` 不进分母 + `coverage` 单列「一条都判不了」的条款 · `load_judge_pairs` 缺 `golden_id` **直接抛不猜键** · CLI **只有 0 / 2，没有 1**）与 `evaluation/calibration/sampling.py`（**人工判读抽样唯一出处**：S0–S3 判据全部机械可算 · 配额 10×4 + `short_by` **缺口不补** · `_order_key` 用 **SHA-256 定序 + 固定种子（不用 RNG）**· 一条只入一层、重叠写进 `overlap` · 落 `sample.jsonl` **不覆盖 `worksheet.jsonl`**）。测试 +36（全量 **3773 通过**）。**四条口径**：① **触发 ≠ 归因**——`triggers` 不产出桶、不碰 `triage`；② **`ungraded` 不进分母、`n = 0` 的条款不出读数**（出 `rate = 0` 等于把「没数据」写成「零分歧」）——避坑指南 §8.22；③ **可复现靠哈希定序不用 RNG**（`random.sample` 跨 Python 版本可能换结果，冻结档要复算得出来）；④ **缺口是读数不是 bug**，跨层借人会让 40 条看着齐了其实同质——避坑指南 §8.23。**真数据读数（v7 头条档 128 条）**：T-1 D-10 = **33/128 = 25.8%**（按候选线会越线且 n 够）· T-4 D-10 **未退化**（pred 众数 `unresolved` 37.5% / 13 个取值）· **T-2 / T-3 测不成**（judge 逐行 pred 不落盘 + 观察原文取不到，两条独立理由）· **S1 总体 33 / S0 总体 32 各取满 10，S2 与 S3 总体 0 ⇒ 实抽 20 条、缺 20**。⚠️ **D-2 / D-4 / D-5 / D-6 四条款一条都判不了**（全 `ungraded`）——软维度金标从此是回路的硬前置。记录五处：命令速查 + 运行手册（两条命令 + 十一口径）· 避坑指南 §8.22 / §8.23 + 自查表两行 · 架构总览补 `triggers.py` / `sampling.py` |
| 2026-10-23 | v2.3 | **C-06 补上「读现场」那一半**（C-07 仍等人填表）：新增 `evaluation/calibration/evidence.py`（**只读**）与 `worklist show` 子命令。**为什么必须有**：工作表给的是「结论 + 一句话证据」，**不是现场**；D-10 的判据原文是「对照黄金集**预存档**的站点核验结论」——预存档不在表里，于是「这个页面到底是不是播放站」答不了，桶也就贴不了。**四条口径**：① **复用 builder 的同一条查找路径**（`split_archive` + 按 url 匹配），**不自己猜文件路径**——两份查找逻辑必然漂，而漂了不报错，只会读到别的版本的观察；② **条款原文从 rubric 文档现读**（`parse_clauses` 按「下一条款头 / markdown 标题 / `---` 谁先到谁算」截断，**不抄第二份到代码里**）——D-10 只说「结论与 ground truth 一致」（比**什么**），「该给哪个分支标签」在 D-5 决策树、「什么算播放组件」在 D-4，**尺子分两处写着**，只给 D-10 人就拿不到尺子；③ **定位不到就说定位不到**（并存下存档里现有的单元与 url 作候选），**不返回空读数**——空读数会被读成「页面里没有 video 标签」，那是把「没找到」当成「看到了没有」；④ **只给事实，不给结论**——桶仍由人填，`triage.validate_rows` 仍是唯一写入口。测试 +25（新单测 25 条，含 **v7 真数据回归**：33 张卡，定位不到的必须明说），全量 **3798 通过**。**v7 实测**：33 张卡里 **28 张定位到存档、5 张明说定位不到**（5 张全是「存档里没有匹配该 url 的站点样本」——那个 url 压根没被访问成站点样本，**不是「现场是空的」**）。**卡 1 读数**：代码侧 `branch=null ⇒ 记成功`、`source=llm`，evidence 是「存在 `<video>/<audio>` 标签 ×6（iframe=10）——代码层事实，不问 LLM」；而那 6 个 `<video>` 标签是**第三步点击广告链接之后才出现**的（正文同时被注入「烈焰觉醒网页版 | 零氪打金传奇 | 立即体验 | 广告 ✕」），页面本身是**网易号美食图文**（济州岛美食攻略）⇒ **代码短路把广告里的视频当成了播放器**（M-26 那一族的**新变体**：正文里没有付费形态词——注入进来的是「烈焰觉醒…立即体验 | 广告 ✕」——所以 M-26 的**付费形态词表**那版**抓不到**它；**只有「短路整体改成 `answer=None`」（永远不许媒体计数许诺成功）那版能拦住**。⚠️ M-26 现在那句「只在正文没有任何付费形态时才保留 True」仍留着同一个尾巴，**照字面读也拦不住** ⇒ 已补记到台账 M-26）。记录四处：命令速查（`worklist show` 两条 + 必填项一行）· 运行手册（`worklist show` 一节 + 三条口径）· 架构总览补 `evidence.py` · `data/triage/README.md` 补「读现场」第 3 步 |
| 2026-10-23 | v2.4 | **把现场搬进人填的那张表**（C-07 仍等人填表；C-06 的第三步）：`worklist build` 现在把**现场读数写进 `worksheet.jsonl` 每一行的 `scene` 栏**，并另落**尺子** `v{N}/clauses.md`（D-10 / D-4 / D-5 条款原文，**现读**生成）。**为什么**：v2.3 把现场做成了**另一条命令**（`worklist show`）——「有现场但要另开文件、跑另一条命令」和「没有现场」在判读时是**同一件事**：表里没有可判的东西，人就判不了桶。**四条口径**：① **`scene` 是只读上下文，不进分诊档**（`triage.strip_row` 只留裁定字段）——它是「判这一条的依据」，重算会变；抄进裁定档就等于让今天的裁定引用着下一版的观察；② **`status` 分四态**（`located` / `located_no_steps` / `not_found` / `not_read`）——「没找到」与「没去读」归成一个布尔，就分不清是「存档里没有」还是「压根没查」，而这两种的修法完全不同；③ **`body_from` 标出正文来自第几步**——正文常来自**更早**的一步（最后一步常常是没读到正文的那步），不标出来，人会默认是最后一步，于是读到一个不对的页面的正文；④ **尺子单独一份**——尺子是**同一把**（D-4 / D-5 / 各卡自己的条款），每行各带一份会让同一个文件长 N 倍；条款查不到时**明说不可用**，不静默少一条。**配对只有一条路径**：`_rows_by_id` 是「卡 ↔ 冻结行」的唯一配对逻辑——两处各写一遍 ⇒ 现场配到别的行上**而不报错**（只表现为「现场对不上」，人会以为是站点变了）。测试 +13（单测 13 条：工作表 `scene` 栏 / 现场不抄进分诊档 / 带 `scene` 照样能导入 / CLI 出尺子 / `scene_summary` 四态 / `body_from` / `clauses_markdown` / `build_scenes` 配对），全量 **3813 通过**。**v7 实测**：33 张卡的 `scene` 栏，**28 条 `located`** / 5 条 `not_found`（「存档里没有匹配该 url 的站点样本」——那个 url 压根没被访问成站点样本，**不是「现场是空的」**）。记录五处：命令速查（`worklist build` 一行 + 必填项一行）· 运行手册（`worksheet.jsonl` 的 `scene` 栏表 + 五条口径）· 避坑指南 §8.24 补纪律 + 自查表 · 人工判读手册 §5.3 改成「现场就在你填的那张表里」· `data/triage/README.md` 第 3 步 |
| 2026-10-23 | v2.5 | **C-07 实施并闭环，R1 清空**（**没有新代码**——C-05 给了贴桶的地方、C-06 给了造表的地方，C-07 要的是人把 33 张卡读完）：`evaluation/golden/data/triage/triage.jsonl` 首次入库 33 行，`triage_by=reesn` × 33 / `status=assigned` × 33，`worklist status` 退 0（33/33 = 100%，机器建议 12 条 · 接受 6 / 改判 6）。**归属分布（全部 d10）**：`evaluator_logic` 11 code · `evaluator_prompt` 1 prompt · `gold_defect` 14 gold · `evidence_missing` 7 collector · **`rubric_ambiguous` + `rubric_missing` = 0**。**三条口径**：① **「rubric 缺陷 0」不等于「rubric 没问题」**——它只是「33 张卡、只有 d10 一个维度上没人判出尺子缺陷」，d2–d9 九个分数维度**一条卡都没有**（软维度无金标），尺子的问题要等软维度金标；② **最大的单一账目是金标 14/33 = 42%**，比评估器（12）更重 ⇒ 改判据之前先核金标（M-30 那一族因此提优先级）；③ **读不到现场 ≠ 采集的错**——5 张 `not_found` 的卡里 2 张仍被判 `evaluator_logic`（页面没变成站点样本、代码却照样给了标签，那是代码的账），同名事实不能直接翻成桶。**闸门第四条判据第一次拿到真实输入**：`verify --triage … --bucket evaluator_logic` 退 1，`❌ bucket_clear evaluator_logic 残留 11`，同时 `❌ net_improvement 净改善 0（配对 128，同版对同版恒 0，D-C11 的结构性后果）` · `✅ class_regression 9 类无 F1 下降` · `✅ regression_regressed_zero 0/60`；软维度 d2 18 / d4 89 / d5 90 / d6 45 **全 `判不了`**。全量 **3834 通过**。记录三处：方案本节（§9.3 结果块）· 台账「§校准回路」分布 · `data/triage/README.md` |
| 2026-10-23 | v2.6 | **D-5 恒真修复 + 记下回路看不见它的那个缺口（= 台账 M-29）**：`perception/rule_perceptor.py` / `evaluation/scorers/d5_scorer.py` 的第四条检查改成**只读 `observations[].body_text`**（不再拿页面标题、标签计数等代码层事实代替"正文里有没有付费形态"），`tests/unit/test_d5_scorer.py` 70 条通过。**为什么不是 C-xx**：这是 rubric 对齐（方案 03）落在 D-5 上的债，但它**在回路上留下一个洞**——快照的 pred 侧取自**冻结行**（`code:frozen_row`），而代码评分器**故意不带版本号**（C-01 口径 2：代码评分器由 git commit 定版，不造一个假版本号）⇒ **改评分器不改变任何一版快照的 pred**，`bucket_clear` 的下一次读数与这次修复**无关**，表现为「修了但数字没变」。修法已写进 [台账 §M-29](../计划进展/10-TODO台账.md#m-29--d-5-的-v7-快照-pred-是修复前的别拿它当基线)：继承式重冻结 + `--refresh-scores`（按 `golden_id` 重算 `dimension_scores` / `scores_detail` / `status`，**缺 id 一律 fail-closed**，manifest 落 `score_refresh` 前后对照）。同时把 M-30 发现的**持有集不独立**记为 §11.2 风险（切档按行不按 URL：289 行 → 131 URL、25 个 URL 跨档、持有集 101 行里 65 行落在跨档 URL 上；决策项 Q-11 / Q-12 已登记台账） |
| 2026-10-23 | v2.7 | **D-C4 / D-C6 / D-C7 三条裁定落地（owner）⇒ C-10 / C-11 / C-13 / C-14 全闭环，R2 与贯穿项清空**：① **D-C7 = rubric 变更强制连带改 prompt** ⇒ `rubric_version.py` 加 `ANCHOR_TABLES`（D-3 / D-9 逐档释义，逐字抄自 rubric 文档）+ `anchors_drift()`（现读文档比对，**改档不改表 ⇒ 非空**）+ `render_anchor_block()`；D-3 / D-9 的 prompt 改为**从表渲染**（D-9 六条 verdict 措辞从 `D9_VERDICT_TEXT` 渲染，原先是手抄）。② **D-C4 = 合并等价类不需 owner 签** ⇒ C-13 的「owner 签」**作废**，「无人记账」**保留**：`alignment.py` 加 `EQUIV_CLASS_NOTES`（每个**多标签**类一张：规范标签 / 被并标签 / **丢了什么细分** / 为什么并）+ `invisible_details()`（**「合并后不可见细分」清单**）+ `validate_merge_notes()`（与表**逐标签**比对），**无签名字段**。③ **D-C6 = 阈值调整不算 rubric 级变更** ⇒ C-14 从「按 rubric 级变更处理」改成**阈值自有其面**：新文件 `evaluation/scorers/thresholds.py` 是唯一真源（11 条，带值 / 定线状态 / 依据 / 使用处），`triggers.py` / `verify.py` / `gate.py` / `builder.py` 四处**全部改从 `thresholds.value("<name>")` 取**，不再各写一份字面量。**⚠️ D-C6 推翻 D-C9 的后半句**（「定线走 rubric 变更流程」作废——定线只改阈值表的 `status`）。**D-C11（C-11）**：pred 出处改成 `<kind>@<version>`，卡上多 `evaluator_version` / `pred_provider_version` / `pred_prompt_version` 三栏，`prompt_comparability()` 给**三态**答案（同版 True / 旧版 False / **没记到 None——不猜**）；`overlay-scout` 回填时把 `scorer_version`（**内容**指纹，不是 git commit）写进行里。**已知未做**：冻结行 v1–v8 没有 `scorer_version` ⇒ 存量数据答不出「旧 prompt 还是新 prompt」（**如实标 `unrecorded`，不假装答得出来**）；**D-3 / D-9 prompt 变了 ⇒ 指纹 `9b3ec1c46545` → `1046e67994aa`，尚未跑真实 LLM 验证**（本机无凭据），**下一版冻结前必须各跑一次真实 A/B**。测试：新增 `tests/contract/test_calibration_provenance.py` 20 条 + `test_rubric_traceability.py` 6 条；**变异测试 56/56 全部变红**（新配 5 条：改锚点表 / 改 provider 版本段 / 改等价类分组 / 阈值改回字面量 / 阈值表 import rubric_version）；全量 **3806 通过**（2 条失败是本机缺 `playwright` 的环境问题，与本次改动无关）。另修 `tests/mutation_check.py` 的两个工具缺陷：**行尾副作用**（`write_text` 在 Windows 上会把 LF 文件翻成 CRLF ⇒ 改成按字节读写还原）与**多节点选择器失效**（`run()` 把整个 selector 当一个 argv 元素传下去 ⇒ pytest 退 4「用法错误」，变更看起来像「没红」；改成 `*selector.split()`）|
| 2026-10-24 | v2.8 | **软维度金标的写入链**（`calibration/goldwork.py` · `golden/dim_scores.py` · `builder._annotate_dimensions_only` / `_refresh_gold` · `gate.assert_gold_comparable`）：这一节补的是**一条断掉的链**，不是新能力——① **`goldwork worksheet`** 出一格一行的填表盘（`(golden_id, dimension)`），每格带 **rubric 锚点原文 + P1 现场 + `record`（记了什么 + `archive` 存档路径，与 `evidence.locate_sample` 同一条 `{task_id}__{run_id}.json` 口径）+ 当前 pred 与理由**，已填值按内容键预填（L-19）——**D-6 核的是「记录字段完整性」，被核的字段自己不在表里，人就得自己拼存档路径**；② **`annotate --dimensions-only`** 只合并分数，`label` / 注 / 标签署名人**一字不碰**（整行模式要 `label` 必填 ⇒ 补分数的人得伪造一个标签，且 `dimension_scores: null` 会抹掉已有分数）；③ **`freeze --refresh-gold`** 把分数带进新版本（只换 `golden.dimension_scores`），manifest 落 `gold_refresh.baseline_reset=true` ⇒ **金标变动 = 新基线**，`gate` 读到就退 2（下一对可比是 vN → vN+1）。**三条口径**：`null` 是结论 / 空串是没填 / 整栏省掉是**错误**（三者进快照都是 `ungraded`，但覆盖率要分得开）；判过没判过**看键在不在不看值**（第一版拿 `.get()` 判断 ⇒ `{"d5": null}` 与 `{}` 等价 ⇒ 人判的「判不了」写不进去也拦不住覆盖，写完第一轮测试才发现）；没人签不写。**读数（v8 调参池）**：表 **364 格**（91 行 `scored` × D-2/D-4/D-5/D-6）、**37 行无样本不进表**（按维度单列）、现场 **94/94 定位到存档**、**待填 364 / 已填 0**——**填表是人做的**。测试 +68（`test_golden_dim_scores` 34 · `test_calibration_goldwork` 21 · `test_golden_dim_writeback` 11 · `test_calibration_gate` +2），全量 **3994 通过**。记录六处：方案本节 · 命令速查（三条命令 + 三行必填项）· 运行手册（新一节 + 闸门预检表一行）· 人工判读手册（§1 加第 H 件事 + 新 §7 全流程）· 域文档 §07（新小节）· 台账（§校准回路现状 + M-34）|
