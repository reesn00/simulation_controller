# 16 · 评估器与 rubric 的双向校准回路

**上游**：[`11-外部方案对账.md`](11-外部方案对账.md)（黄金集校准循环 · 四条铁律）· [`02-rubric与代码对齐.md`](02-rubric与代码对齐.md)（两侧对齐账）· [`03-rubric修订与实施方案.md`](03-rubric修订与实施方案.md)（rubric v1.1 改稿）· [`10-judge接入方案.md`](10-judge接入方案.md)（D-3 / D-9 软维度）
**状态**：草案 v1.9 · 2026-10-23 · **部分实施**：**R0 全闭环**（C-01 / C-02 / C-03 / C-04 / C-15），**R0.5 全闭环**（C-17 / C-08 / C-09），**R1 已开**（C-05 ✅），下一段是 **C-06**（见 [§9](#9-任务执行计划)）。**D-C5 / D-C8 / D-C9 / D-C10 / D-C11 已裁定**（[§2.2](#22-待-owner-拍板的决策)）；D-C2 / D-C3 随 C-05 落地为**代码里的封闭集与身份路由**（不再待裁）
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
> **D-C1 的第一半也落了**（卡留在 `output/`，分诊档进版本控制）。剩下待裁的只有 D-C4 / D-C6 / D-C7（都在 C-10 / C-11 / C-14 上）。

| # | 议题 | 建议 | 理由 |
|---|---|---|---|
| **D-C1** | 分歧卡是否纳入版本控制 | **不纳入**，落 `output/pipeline/calibration/`；**人填的分诊档纳入** `evaluation/golden/data/triage/` | 派生数据可再生（派生自冻结档）；人工裁定是资产，丢了不可重建（与 `annotations.jsonl` 同理）|
| **D-C2** ✅ | 桶表长度 | **封顶 6 桶**，超出的情形一律留空待人判 | `BRANCH_PRECONDITION` 的教训：表长到跟分支表一样长就没人维护 |
| **D-C3** ✅ | 谁可以填 `rubric_*` / `gold_*` 桶 | **只有人类 id**；agent 填的写入一律拒绝 | 尺子与真值的裁定权不外放 |
| **D-C4** | 等价类变更是否需 owner 签 | **是** | 合并等价类是"放水"最省力的路径，且现在无人记账 |
| **D-C5** ✅ | holdout 是否真封存 | **是**，但需先改代码（当前 D-10 分母含 holdout）| 不改则"持有集只跑一次"只是纸面纪律。**已裁定 2026-10-22**（随 C-03 一并落地：头条分母改为只算调参池）|
| **D-C6** | 阈值调整的性质 | **按 rubric 级变更处理**（人裁 + 记录 + 版本）| 否则"差一点点"会成为长期借口 |
| **D-C7** | rubric 变更是否强制连带改 prompt | **是**，由 contract test 盯 | 否则 prompt 与准则各走各的（现在就各走各的）|
| **D-C8** ✅ | 回归集从哪划 | **从 calibration 划出固定 60 条**（v6 现有 191 条），冻结、**永不作调参依据** | 从 holdout 划会每版都烧一次，破坏「只跑一次」（D-C5）；新增标注最干净但要人时。⚠️ 代价：调参池 191 → 131。**已裁定 2026-10-22** |
| **D-C9** ✅ | 触发阈值谁定线 | **已有线沿用**（D-10 ≥0.90 / 硬 kappa ≥0.70 / 软加权 ≥0.50 / B-3 ≥85%）；**新线第一批只出读数**，定线本身按 rubric 级变更（D-C6）走 | 拍脑袋的阈值会变成新的「差一点」借口（[§6](#6-防放水的五条硬规矩) 第 3 条）。**已裁定 2026-10-22** |
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

- **读数先于阈值**。新线的第一批只出读数、不出"合格/不合格"（沿用黄金集软维度"首次跑只发 README 不准出数字"的处置）；定线本身按 rubric 级变更走（D-C6 / D-C9）。
- **触发 ≠ 归因**。越线只说明"该看一眼"，改哪一侧仍由 [§4](#4-六桶归属封闭集与前置事实) 判。

### 5.5 修改粒度：条款级（v1.3 新增）

rubric 里**已经有稳定的条款号**（`G-1..G-7` / `D-2..D-10` 及各档锚点 `D-3.5` / `B-1..B-4`），不用新造体系。缺的是**让代码与产物引用它**：

- `rubric_version.DIM_CLAUSES`（dimension + 档位 → 条款号）做唯一真源；prompt 锚点表（[§7](#7-把-prompt-从散文升级成锚点表)）与分歧卡 `rubric_ref` 都从它投影，**不许各写一份**。
- 三条硬规矩：
  1. **一次改版只动列出的条款**——未列条款的分数必须**逐位不变**（机械 diff，CI 红即驳回）；
  2. **禁止整份重写**——整份重写让历史分数失去可比性，漂移也不可控；
  3. **只增不删**（现有治理规则）⇒「删除」的实现是标 `deprecated` + 保留行，不是物理删。
- **影响面必须写进改版说明**：改 `D-3.5` ⇒ 旧金标在 `D-3.5` 上**不可比**（rubric 自己在 v1.0→v1.1 记过这条教训）。⇒ 回归判据只在**未变条款**上算，变了的条款走 dry-run 重打 + 修正窗口（[§5.2](#52-外环--rubric-回路bucket--ambiguous-missing) 第 3 条）。
- ⚠️ **「调权重」当前是空类**：本 rubric 没有权重体系（`G-7` 明写批次级与样本级不可相加）。列进 enum 是为了将来引入权重表时不必改契约，**不是现在有东西可调**。

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

### 5.8 动机链：三个必填栏（v1.3 新增）

rubric 变更记录现在是四列（版本 / 日期 / 变更内容 / 修订人），补三列：

| 列 | 内容 | 为什么 |
|---|---|---|
| `disagreement_id` | 触发这次修改的分歧卡 id（**≥1 条**）| 现在写"依据：用户裁定"，查不到是哪批数据逼出来的 |
| `change_kind` | 新增 / 修改表述 / 废弃 / 调锚点分档 / 调权重（空类，见 §5.5）| 排查"某类数据评分突变"时，「修改表述」与「调锚点分档」的路径完全不同 |
| `expected_effect` | **可证伪**的预期（"`D-3.5` 分歧 12 → ≤4"）| 没有预期就只能看总数涨没涨，无法判断这次改版是否达到目的 |

`expected_effect` 由回归集读数核对：达到 → 关；未达到但没弄坏 → 记 `accepted_exception`（[§3.3](#33-状态机)）；未达到且有弄坏 → 回滚。

---

## 6. 防放水的五条硬规矩

| # | 规矩 | 现状 |
|---|---|---|
| 1 | **金标不回改**：`gold_defect` 只能开新版本 + 修正窗口，旧版留档 | ✅ 已有（铁律 2）|
| 2 | **等价类合并 = 放水高危**：改 `EQUIV_CLASS` 使 D-10 上升的，必须单列并说明"合并后不可见的细分"，owner 签 | ❌ **空白**——现在无法区分 D-10 增量里有多少来自判定变好、多少来自等价类扩展 |
| 3 | **阈值不随数字走**：`D10_THRESHOLD` / 软维度阈值不许因"差一点"下调 | ⚠️ 无守卫，靠自觉 |
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
| `evaluation/calibration/triggers.py` | 新增（v1.3）| T-1～T-4 四类读数 + 阈值表（阈值缺失时只出读数，不判合格）|
| `evaluation/calibration/sampling.py` | 新增（v1.3）| 分层抽样（S0–S3、固定 seed、`sample_weight`，可复现）|
| `evaluation/scorers/rubric_version.py` | 新增 | `RUBRIC_VERSION` + 各 judge 的 `ANCHORS` 表 + **`DIM_CLAUSES`**（dimension + 档位 → 条款号，唯一真源）|
| `evaluation/scorers/d3_scorer.py` / `d9_scorer.py` | 改 | prompt 由锚点表渲染；few-shot 挂 `anchor` |
| `evaluation/golden/splits.py` | 新增（v1.6，已落）| 三档词汇唯一真源：档名 / 文件名 / 头条档位 / 回归集条数与种子 |
| `evaluation/golden/builder.py` | 改 | `agreement` 增 `--emit-disagreements`；`freeze` 增 `--from-version` / `--regression`；`manifest` / `agreement.json` 增 `rubric_version` / `evaluator_version` / `holdout_runs`；D-10 分母改为 calibration（holdout 单独出数）|
| `evaluation/golden/data/triage/` | 新增目录（v1.9，已落）| 人填分诊档入版本控制（D-C1）；`README.md` 记字段表与六桶 |
| `tests/contract/test_rubric_traceability.py` | 新增（v1.9，部分已落：③④）| ①每条 rubric 变更含 `disagreement_id` + `change_kind` + `expected_effect`（[§5.8](#58-动机链三个必填栏v13-新增)）②每个 prompt 锚点可映射到 `DIM_CLAUSES` ③桶表 ≤ 6 ④`triage_by` 权限 ⑤**改版后未列条款的分数逐位不变**（[§5.5](#55-修改粒度条款级v13-新增)）。①②⑤ 等 C-10 / C-11 |
| `tests/unit/test_golden_three_split.py` | 新增（v1.6，已落）| 三档词汇 / 回归切片 / 继承式重冻结 / 头条口径 / 指纹守卫 |
| `tests/unit/test_calibration_verify.py` | 新增（v1.7，已落，29 条）| H1-α 回放必驳回 / 总数涨而单类塌 / 净改善为 0 / 回归集反转 / 判不了 ≠ 修好 / 同桶清零三种结局 / 换档·重切·键重复·schema 不同一律退 2 |
| `tests/unit/test_calibration_triage.py` | 新增（v1.9，已落，33 条）| 六桶封闭集 / 前置事实 / 身份识别（含 `agent:` 前缀与 `agent-smith` 反例）/ 字段校验 / 读写幂等 / CLI 三态 / `verify.analyze` 再校验 |
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
| **R1** | **C-05 ✅** · C-06 · C-07 · **C-16** · **C-19** | 归属与抽样，给出投入方向 |
| **R2** | C-10 · C-11 · **C-18** | 锁住两侧不再漂（条款级）|
| 贯穿 | C-13 · C-14 | 治理散活，随时插 |

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
| **C-06** | 首轮分诊 worklist（含 agent 预填 `evaluator_logic` / `evidence_missing` 建议值） | C-05 | 归属率 100%，无 `other` | 1–2d 人时 | 否 |
| **C-07** | 归属分布报告：**rubric 缺陷 vs 评估器缺陷比例** | C-06 | 能回答"下一手该改尺子还是改判据" | 0.5d | 否 |
| **C-16** | 触发读数 `triggers`：T-1～T-4 四类（[§5.4](#54-量化触发v13-新增)），**先只出读数** | C-15 | 每条信号有读数 + `n`；阈值列为空时命令**不判合格**，不许默认通过 | 1d | ✅ 已拍板（D-C9：读数先行、线后定）|
| **C-19** | 分层抽样器（S0–S3 固定配额 + seed），产出人工 worklist | C-15 | 同一输入两次抽样逐行相同（可复现）；每层行数 == 配额（40 = 10×4）| 0.5d | ✅ 已拍板（D-C10）|

**C-07 是整条线的决策点**：这个比例现在没人知道，而它决定 H-02 后续投 LLM prompt、重采集，还是改 rubric。

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

**记录（§9.9 闭环第 3 条）**：[08-常用命令速查 · 黄金集与校准回路](../08-常用命令速查.md)（命令一行一条 + 必填项表的「退出码 1 与 2 不许混报」）· [04-运行手册 · `calibration.verify` 两轮对比](../04-运行手册.md#calibrationverify-两轮对比方案-16-c-08)（四条判据 / 参数 / 退出码 / 三条口径）· [02-避坑指南 §8.17](../02-避坑指南.md)（`n/a 不算通过`、`测不成`不是`回退`）+ 自查表一行。**三条齐 → 按 §9.9 算闭环**（实现 / 29 条测试真跑 / 三处记录）。

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
| **C-10** | D3 / D9 锚点表化，few-shot 挂 `anchor` | — | 改 rubric 某档而 anchor 未改 → contract test 红 | 1–2d | **是**（D-C7）|
| **C-11** | `pred_provider` 记 prompt hash | C-10 | 分歧卡能回答"旧 prompt 还是新 prompt 判的" | 0.5d | 否 |
| **C-12** | rubric 变更记录加 `disagreement_id` + 责任区分声明栏 | C-02 | 每条改版可指回分歧行；以"评估器做不到"为由 → 驳回 | 0.5d | 否 |
| **C-18** | **变更记录条款化**：rubric 变更记录加 `change_kind` / `expected_effect` 两列 + 「未列条款逐位不变」diff 守卫 | C-02 | 改一条款时能**机械证明**其他条款分数没动 | 1d | 否 |

**`rubric_ref` 与 `DIM_CLAUSES` 划在 C-02 实现范围内**，不留给 C-18：卡派出来若没有条款号，后面每张卡都要回填一次，回填必然漂。

### 9.6 P4 · 治理散活

| ID | 事项 | 依赖 | 验收 | 成本 | 需拍板 |
|---|---|---|---|---|---|
| **C-13** | 等价类变更 owner 签 + "合并后不可见细分"清单 | — | `EQUIV_CLASS` 每改一次有签 | 0.5d | **是**（D-C4）|
| **C-14** | 阈值调整按 rubric 级变更（文档 + 流程） | — | 调阈值需人裁 + 版本 | 0.5d | **是**（D-C6）|

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
C-01 ✅ ─> C-02 ✅ ─> C-15 ✅ ─┬─> C-16 ─> C-19         （读数与抽样）
                               ├─> C-08 ✅ ─> C-09 ✅   （回归门）
                               └─> C-05 ✅ ─> C-06 ─> C-07 ──> （决定 H-02 方向）
C-03 ✅ ─┬─> C-04 ✅
         └─> C-17 ✅ ──────────> C-08 ✅
C-10 ─> C-11 / C-18   （可与 R1 并行，纯结构改造）
C-12 / C-13 / C-14    （散活，随时插）
```

**第一周建议（v1.8）**：C-01 ✅ → C-02 ✅ + C-15 ✅ → C-03 ✅ + C-04 ✅ + C-17 ✅（一次重冻结拿到三档）→ C-08 ✅（拿到回归门）→ C-09 ✅（门有人跑）。**R0 与 R0.5 清空**，且不欠裁定。D-C8 / D-C9 / D-C10 已于 2026-10-22 裁定，D-C5 随 C-03 一并落地。**v1.9：C-05 ✅ 落地，R1 的桶表这一段清空。**

**下一步是 R1 · C-06**（首轮分诊 worklist：给分歧卡逐条挂上「建议桶 + 建议判据」的机器预填，人只确认或改）：

- **为什么现在能做**：C-05 把「桶能写在哪、谁有资格写」定死了（`triage.validate_rows` 是唯一写入口，`actor_kind` 按身份路由）。C-06 的预填**只能落在卡上的 `suggested_*`**，不写进分诊档——这条路由已经封着，不用靠约定；
- **C-06 的验收「归属率 100%、无 `other`」现在才说得清**：没有封闭集时「100%」是个空话，因为没有筐外的选项；
- **之后 C-07 出「rubric 缺陷 vs 评估器缺陷」比例**——那才是决定 H-02 往哪投的那一步。

⚠️ 闸门现在**仍只在三条判据上说话**（净改善 / 类回归 / 回归集）。第四条「同桶清零」的**输入**（分诊档）已经能读能校验了，但档里还是空的——所以 `verify --triage` 仍打 `n/a`。`n/a 不算通过` 的口径不变：**C-05 落地的不是通过，是「有资格被填」**。真实的第一份分诊要等 C-06 把人排进来。

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
| 分歧归属率 | **100%**，无 `other` 桶。**桶表本身已封闭**（C-05 ✅：`MAX_BUCKETS = 6` import 期封顶、`other` 不在 `BUCKETS` 里）——所以「无 `other`」现在是**结构上不可能**，不再是纪律要求 |
| rubric 变更 | 每条 ↔ **≥1 条分歧** + 人裁记录 + 责任区分声明 |
| 内环改动 | 净改善 > 0 且无类回归；否则红（**机械版 = `verify` 退出码 1；有人跑它 = `gate`，挂 freeze**）|
| 版本元数据 | 每个冻结版本有 `rubric_version` + `evaluator_version` + `holdout_runs` |
| 桶收敛 | 同桶连续两轮不清零 → `escalated`，进 [10-TODO台账](../计划进展/10-TODO台账.md) |
| 等价类变更 | 每次合并都有 owner 签 + "合并后不可见的细分"清单 |
| **触发信号**（v1.3）| T-1～T-4 都能一条命令出读数并带 `n`；**阈值未定线时命令报"未定线"，不默认通过** |
| **条款级可比性**（v1.3）| 改版后未列条款的分数**逐位不变**（机械 diff，CI 红即驳回）|
| **回归集**（v1.3）| 每次改动 `regressed == 0` 且 `net > 0`；回归集金标本身变动 = 新版本 + 人裁（**机械版 = `verify` 的第二条/第四条判据**，金标变动直接退 2）|
| **分层抽样**（v1.3）| 每轮人工样本带 `stratum` + seed，可复现；层内读数不冒充总体 |

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
| **人成为瓶颈** | 第一轮 73 条要人分诊 | `evaluator_logic` / `evidence_missing` 可由机器预填建议值（`suggested_by=agent`，人只确认）；`rubric_*` / `gold_*` 永远留空待人填。与 INC37 `auto-validator-l3l4` 四档裁决同构：人和机器的活分开记账 |
| **把 D-10 当唯一目标** | 最省力路径永远是合并等价类 | 加覆盖度约束：进步必须同时满足"等价类内细分不变差" |
| **rubric 变更被稀释** | 每次为提高数字改一条边角条款 | 外环四条强制 + 责任区分声明 + 新版本冻结 |
| **分诊质量不可测** | 人随手贴桶 | `triage_evidence` 必填；抽检 20% 复核；`gold_defect` 与 `rubric_*` 桶需二次签 |
| **新机制与主线脱节** | 方案落了但没人跑 | 把"分歧归属率 100%"写进 R1 验收；不进 CI 的机制都会烂尾 |
| **回归集被调参吃掉**（v1.3）| 反复看同一批 60 条、按其逐条结果改 prompt ⇒ 回归集变成第二个调参池，过拟合后门禁形同虚设 | 回归集逐条明细**只给 owner**；改判据的人只看汇总计数与条款分布 |
| **分层样本冒充总体**（v1.3）| 在分层抽样上算 kappa 当总体值 | 报层内读数 + `sample_weight`；**不做总体外推**（D-C10）|
| **触发阈值自己漂**（v1.3）| "差一点"就把线调低，触发机制变成装饰 | 阈值按 rubric 级变更（D-C6 / D-C9）；调线必须有版本记录 |
| **条款号与实际判据脱钩**（v1.3）| `rubric_ref` 是手填字符串，改了条款没人改映射 | `DIM_CLAUSES` 唯一真源 + contract test（锚点与条款双向可映射）|

---

## 12. 变更记录

| 日期 | 版本 | 说明 |
|---|---|---|
| 2026-10-22 | v1 | 初稿。现状核对（三个洞）+ 分歧卡契约 + 六桶封判 + 双回路门槛不对称 + 四条防放水规矩 + R0–R3 落地顺序 |
| 2026-10-22 | v1.1 | **§9 展开为任务执行计划**：R0–R3 细化为 `C-01`–`C-14` 十四条，带依赖、验收、成本、拍板归属；补依赖链图、第一周建议、闭环判定三条标准、与 H-02 的关系（本方案不直接提 D-10，只让每一 pp 可指认）|
| 2026-10-22 | v1.2 | **C-01 实施并闭环**：新增 `evaluation/scorers/rubric_version.py`，`cmd_freeze` / `cmd_agreement` 落尺子三字段，补 9 条契约测试（全量 3493 条通过）。记录三条口径（老档不补写 / 指纹只覆盖 D-3·D-9 / D10 pred 侧来自冻结行）与 `holdout_runs` 暂恒为 0 的原因 |
| 2026-10-22 | v1.3 | **补五处机制**：① [§5.4](#54-量化触发v13-新增) 量化触发 T-1～T-4；② [§5.5](#55-修改粒度条款级v13-新增) 条款级修改粒度（`rubric_ref` / `DIM_CLAUSES` / 未列条款逐位不变）；③ [§5.6](#56-回归集黄金校准集v13-新增) 回归集（黄金校准集）与 `regressed == 0` 门禁，**排到分诊之前**（R0.5）；④ [§5.8](#58-动机链三个必填栏v13-新增) 变更记录三列动机链；⑤ [§5.7](#57-人工抽样按分歧类型分层v13-新增) 分层抽样 S0–S3。新增任务 C-15～C-19、决策 D-C8～D-C10。**两处对原需求的修正**：代码↔LLM 分歧率只能定义在 D-4（其余条款只有一个评分者）；"LLM 自评置信度"不可用（scorer 里是写死常量），改用证据锚定代理 |
| 2026-10-22 | v1.4 | **D-C8 / D-C9 / D-C10 按建议裁定**：回归集 = calibration 切 60 条（调参池 191 → 131）；新触发阈值只出读数、定线走 rubric 级变更；分层抽样 40 条/轮（S1/S2/S3/S0 = 10×4）+ 固定 seed + 不外推总体。新增 [§9.7](#97-已裁定的三条对数字的影响2026-10-22) 记录三处分母（289 → 191 → 131），C-16 / C-17 / C-19 不再等拍板 |
| 2026-10-22 | v1.5 | **C-02 + C-15 实施并闭环**：新增 `evaluation/calibration/{snapshot,disagreement}.py` 与 `--emit-disagreements`（只读派生，缺 `--version` 即抛），`rubric_version.py` 加 `DIM_CLAUSES` / `clause_of`，补 17 条契约测试（全量 3510 条通过）。v6 实测：快照 856 条（agree=215 / detail_mismatch=1 / disagree=73 / ungraded=567）、分歧卡 74 张（eq_class_mismatch=73 / label_mismatch=1），与 `口径 C：216/289 = 74.7%` 同分母对得上。记录四条口径（`ungraded` 不是分歧 / 卡不含回归信息 / `detail_mismatch` 与 `disagree` 分开记账 / 容差内 `tier_gap` 也记幅度）|
| 2026-10-22 | v1.6 | **C-03 + C-04 + C-17 实施并闭环，R0 清空**：owner 裁定「以 v6 冻结档为准（74.7%）」，D-C5 一并落地。新增 `evaluation/golden/splits.py`（三档词汇唯一真源），`freeze` 增 `--from-version`（继承式重冻结）与 `--regression`，`agreement` 头条只认调参池 + 旧口径同时输出 + 回归集整行指纹守卫；**v7 = v6 行原样 + 三档切分**（131/60/103 行，站点级 128/60/101）。补 15 条测试（全量 3525 通过）。**两处更正**：① [§9.7](#97-已裁定的三条对数字的影响2026-10-22) 的 191/131 是行数，D-10 分母只认站点级（188/128）；② v1.5 那条"卡上 `split` 会出现 `regression`"的判断是错的——分诊池 = 调参池，回归集与持有集都不出卡（v6 卡数 74 → 49，v7 33）|
| 2026-10-23 | v1.7 | **C-08 实施并闭环，R0.5 只剩 C-09**：新增 `evaluation/calibration/verify.py`（两轮快照差分 → 四条判据 → **退出码 0/1/2**）与 29 条测试（全量 3554 通过）。**五条口径**：① 五桶（三类计数 + `still_wrong` / `unverifiable` 单列）；② 配对键只有 `(golden_id, dimension)`，重采集的行配不上（**"改评估器 → 重采集"这条回路现在还判不了**，需快照加案件身份，属后继改动）；③ 不可比输入一律退 2（键重复 / 换过档 / 回归集键集或金标变了 / schema 不同），**1 ≠ 2**；④ 同桶清零要求分诊表覆盖本轮全部分歧，否则 0 残留不可信；⑤ `detail_mismatch` 算对、`tier_gap` 算错。实测：v7 vs v7 → 净 0、退出码 1（**没改东西就没有"改善"可主张**）；v6 vs v7 → 退 2（60 行跨切档）。H1-α 回放（修 4 / 伤 6）两条独立理由同时驳回。记录三处：命令速查（新增「黄金集与校准回路」一节 + 必填项一行）· 运行手册（`calibration.verify` 两轮对比）· 避坑指南 §8.17 + 自查表一行 |
| 2026-10-23 | v1.8 | **C-09 实施并闭环，R0.5 清空**：形态由 owner 裁定为**冻结时闸门**（新决策 **D-C11**，不接 GitHub Actions）——快照 pred 侧取自冻结行 ⇒ 同版对同版恒为净改善 0 ⇒ 按 push 的门必然永远红。新增 `evaluation/calibration/gate.py`（定基线 → **每次重算**两版快照 → 交 `verify`；执行码 0/1/2）与 20 条测试（全量 3574 通过），`verify._load_triage` 提为 public `load_triage`（分诊表走同一份加载器）。**五条预检全退 2**：无冻结版 / 首版无基线 / 基线与本轮同版 / 基线晚于本轮 / 冻结档出不了快照。三条口径：每次重算不复用旧快照 · 出快照与人工跑 `agreement` 是同一份实现（逐字节比对测试）· `gate_meta` 进报告。实测：缺省 v6→v7 退 2（跨 C-17 切档，提示"第一对可比的是本轮与它的后继"）；同版对同版退 2；不存在的版本退 2。**今天没有可判的一对，第一次真正判是 v8 冻结之后**。记录三处：命令速查 + 运行手册（`calibration.gate` 冻结时闸门 + 门禁节）· 避坑指南 §8.17 补闸门三条预检 + 自查表 · 架构总览补 `gate.py` |
| 2026-10-23 | v1.9 | **C-05 实施并闭环，R1 的桶表段清空**：新增 `evaluation/calibration/buckets.py`（六桶封闭集 + `MAX_BUCKETS` import 期封顶 + `BUCKET_PRECONDITION` / `BUCKET_OWNER` / `BUCKET_LOOP`，`HUMAN_ONLY` 由 `("rubric_","gold_")` 前缀**推导**）与 `evaluation/calibration/triage.py`（`validate_rows` 是**唯一写入口**；身份 → 缺字段 → 桶 → 维度 → D-C3 → 责任 → 状态；默认 dry-run、默认拒覆盖、幂等只比 bucket/by/evidence），`evaluation/golden/data/triage/README.md` 记字段表。**验证器不再自带加载器**：`verify.py` 的 `load_triage` 删掉改 import triage 那一份，`analyze` 里**再校验一遍**（CLI 与公开 API 两条路同一套规矩），`gate.main` 同时吃 `TriageError` → 退 2。测试 +44（triage 33 + 契约 11，全量 3618 通过）。**三条口径**：① `accepted_exception` 是状态不是桶（`STATUSES` 里，不在 `BUCKETS` 里）；② 身份识别用**显式名单 + 命名空间前缀**两张表，不用子串猜（`agent-smith` / `Qwen` 当人）；③ `triage_at` 不参与幂等判断。**C-05 落地的不是"同桶清零通过"，是"分诊档有资格被填"**——档里还是空的，`verify --triage` 仍打 `n/a`。记录四处：命令速查 + 运行手册（`calibration.triage` 六桶表 + 三条硬规矩）· 避坑指南 §8.18 + 自查表 · 架构总览补 `buckets.py` / `triage.py` / `data/triage/` |