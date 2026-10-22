# 16 · 评估器与 rubric 的双向校准回路

**上游**：[`11-外部方案对账.md`](11-外部方案对账.md)（黄金集校准循环 · 四条铁律）· [`02-rubric与代码对齐.md`](02-rubric与代码对齐.md)（两侧对齐账）· [`03-rubric修订与实施方案.md`](03-rubric修订与实施方案.md)（rubric v1.1 改稿）· [`10-judge接入方案.md`](10-judge接入方案.md)（D-3 / D-9 软维度）
**状态**：草案 v1 · 2026-10-22 · **未实施**
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

---

## 1. 现状核对：三个洞

### 1.1 分歧没有沉淀成对象

`cmd_agreement`（[`evaluation/golden/builder.py:975`](../../evaluation/golden/builder.py)）输出的是**聚合数**：口径 C 一致率、`classification_report` 的 per_class 计数、软维度的 `n / agreed / skipped`（[:277](../../evaluation/golden/builder.py)）。

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

| # | 议题 | 建议 | 理由 |
|---|---|---|---|
| **D-C1** | 分歧卡是否纳入版本控制 | **不纳入**，落 `output/pipeline/calibration/`；**人填的分诊档纳入** `evaluation/golden/data/triage/` | 派生数据可再生（派生自冻结档）；人工裁定是资产，丢了不可重建（与 `annotations.jsonl` 同理）|
| **D-C2** | 桶表长度 | **封顶 6 桶**，超出的情形一律留空待人判 | `BRANCH_PRECONDITION` 的教训：表长到跟分支表一样长就没人维护 |
| **D-C3** | 谁可以填 `rubric_*` / `gold_*` 桶 | **只有人类 id**；agent 填的写入一律拒绝 | 尺子与真值的裁定权不外放 |
| **D-C4** | 等价类变更是否需 owner 签 | **是** | 合并等价类是"放水"最省力的路径，且现在无人记账 |
| **D-C5** | holdout 是否真封存 | **是**，但需先改代码（当前 D-10 分母含 holdout）| 不改则"持有集只跑一次"只是纸面纪律 |
| **D-C6** | 阈值调整的性质 | **按 rubric 级变更处理**（人裁 + 记录 + 版本）| 否则"差一点点"会成为长期借口 |
| **D-C7** | rubric 变更是否强制连带改 prompt | **是**，由 contract test 盯 | 否则 prompt 与准则各走各的（现在就各走各的）|

---

## 3. 分歧卡（DisagreementRecord）

### 3.1 契约

一条 `(golden_id, dimension)` 一条记录。**只读派生，不回写冻结档**。

| 字段组 | 字段 | 说明 |
|---|---|---|
| 身份 | `golden_id` / `golden_version` / `split` / `dimension` | `split ∈ {calibration, holdout}`；`dimension ∈ d2..d10` |
| 金标侧 | `gold_value` / `gold_anchor` / `gold_note` | `gold_anchor` = rubric 条款号（如 `D-3.5`）|
| 预测侧 | `pred_value` / `pred_reasons` / `pred_evidence` / `pred_provider` | `pred_reasons` = scorer `reasons` 原文；`pred_provider` = `code:…` 或 `llm:<model>/<prompt_version>` |
| 差异 | `delta_kind` | `label_mismatch`（字面不等）/ `eq_class_mismatch`（等价类也不等）/ `tier_gap`（软维度档差 > tolerance）|
| 分诊 | `bucket` / `responsibility` / `triage_by` / `triage_at` / `triage_evidence` | `triage_evidence` **必填**：为什么不是别的桶 |
| 处置 | `action` / `change_ref` / `status` / `verify_run` / `net_delta` | `change_ref` = commit / rubric 版本 / 变更记录行；`net_delta` = `{fixed, regressed}` |

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

---

## 6. 防放水的四条硬规矩

| # | 规矩 | 现状 |
|---|---|---|
| 1 | **金标不回改**：`gold_defect` 只能开新版本 + 修正窗口，旧版留档 | ✅ 已有（铁律 2）|
| 2 | **等价类合并 = 放水高危**：改 `EQUIV_CLASS` 使 D-10 上升的，必须单列并说明"合并后不可见的细分"，owner 签 | ❌ **空白**——现在无法区分 D-10 增量里有多少来自判定变好、多少来自等价类扩展 |
| 3 | **阈值不随数字走**：`D10_THRESHOLD` / 软维度阈值不许因"差一点"下调 | ⚠️ 无守卫，靠自觉 |
| 4 | **holdout 真封存**：内环只在 calibration 迭代；每个 rubric 版本 holdout 只跑一次，记录 `holdout_runs` 与 gap（>5pp 回调）| ❌ **当前不成立**：`cmd_agreement` 把 calibration + holdout 一起算进 D-10 分母（[`builder.py:981-1000`](../../evaluation/golden/builder.py)）；只有软维度才 calibration 优先（[:305](../../evaluation/golden/builder.py)）|

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
| `evaluation/calibration/buckets.py` | 新增 | 6 桶封闭集 + `BUCKET_PRECONDITION` + `BUCKET_OWNER`；未登记桶 `KeyError` |
| `evaluation/calibration/triage.py` | 新增 | 读人填分诊档；校验 `triage_by` 非 agent（rubric/gold 桶）+ 理由必填 |
| `evaluation/calibration/verify.py` | 新增 | 内环三条判据（净改善 / 无类回归 / 同桶清零）|
| `evaluation/scorers/rubric_version.py` | 新增 | `RUBRIC_VERSION` + 各 judge 的 `ANCHORS` 表 |
| `evaluation/scorers/d3_scorer.py` / `d9_scorer.py` | 改 | prompt 由锚点表渲染；few-shot 挂 `anchor` |
| `evaluation/golden/builder.py` | 改 | `agreement` 增 `--emit-disagreements`；`manifest` / `agreement.json` 增 `rubric_version` / `evaluator_version` / `holdout_runs`；D-10 分母改为 calibration（holdout 单独出数）|
| `evaluation/golden/data/triage/` | 新增目录 | 人填分诊档入版本控制（D-C1）|
| `tests/contract/test_rubric_traceability.py` | 新增 | ①每条 rubric 变更含 `disagreement_id` ②每个 prompt 锚点可映射到 rubric 条款 ③桶表 ≤ 6 ④`triage_by` 权限 |
| `tests/unit/test_calibration_*` | 新增 | 净改善计算 / 同桶清零 / 分诊 fail-closed |

**不新增顶层 CLI**：挂在 `evaluation.golden.builder` 与 `evaluation.cli` 的既有命名空间下（沿用 [08 号方案 §3.1](08-阶段4七层定位与黄金数据集rubric门禁方案.md) 的取舍）。

---

## 9. 落地顺序

| 阶段 | 内容 | 为什么排这里 |
|---|---|---|
| **R0**（半天）| ①分歧卡派生 ②`rubric_version` / `evaluator_version` 进 manifest 与 agreement ③D-10 分母修正（holdout 旁置）| **先把"这批数是哪版尺子量的"补上**，不然后面全是无根数 |
| **R1**（1–2 天）| 6 桶分诊表 + 人填 worklist（沿用 [07-人工复核手册](../07-人工复核与判读手册.md) 形态），对现有 73 条 D-10 不一致跑第一轮 | **第一次给出"rubric 缺陷 vs 评估器缺陷"的比例**——现在没人知道这个数，而它决定后续投入方向 |
| **R2** | 内环验证器：净改善 / 无类回归 / 同桶清零，进 CI | 把 H1-α 的人肉推演变成机械判定 |
| **R3** | prompt 锚点表化 + contract test；rubric 变更记录加 `disagreement_id` | 锁住两侧不再漂 |

**R1 是最容易被跳过、也最该先做的一步**：没有归属分布就动手改，等于继续靠直觉选 H1-α 还是 H1-β。

---

## 10. 验收信号（机械可查）

| 信号 | 期望 |
|---|---|
| 分歧归属率 | **100%**，无 `other` 桶 |
| rubric 变更 | 每条 ↔ **≥1 条分歧** + 人裁记录 + 责任区分声明 |
| 内环改动 | 净改善 > 0 且无类回归；否则 CI 红 |
| 版本元数据 | 每个冻结版本有 `rubric_version` + `evaluator_version` + `holdout_runs` |
| 桶收敛 | 同桶连续两轮不清零 → `escalated`，进 [10-TODO台账](../计划进展/10-TODO台账.md) |
| 等价类变更 | 每次合并都有 owner 签 + "合并后不可见的细分"清单 |

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

---

## 12. 变更记录

| 日期 | 版本 | 说明 |
|---|---|---|
| 2026-10-22 | v1 | 初稿。现状核对（三个洞）+ 分歧卡契约 + 六桶封判 + 双回路门槛不对称 + 四条防放水规矩 + R0–R3 落地顺序 |