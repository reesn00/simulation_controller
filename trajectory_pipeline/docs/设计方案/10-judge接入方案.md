# 评判器（judge）接入方案

> **版本**: v0.1 · **起草**: 2026-10-14 · **状态**: 待评审

## 〇、范围与前置

### 范围

本方案回答两个问题：

1. 当前架构上有没有「评判器」（judge）这一职能？
2. 若没有，按「judge 与 rubric 互相配合」的设计原则，怎么把它建起来？

### 不在范围

- D-10 阈值 74.7% < 90% 的 systematic bias 根因（INC32 后续路径：LLM prompt 改善 + 重采集）
- L4 真视觉 LLM 评测的 KPI 复跑（已增量三十四落地，独立成批）
- LS 双向桥 L7 回流（`evaluation/ls_bridge/` 只有 `__init__.py`）
- D-3 / D-9 之外的 LLM 维度复检层

### 前置条件

- v3 黄金集已冻：`191 校准 / 103 持有 / seed=42`（增量三十三）
- 28 条手动复核人工 + INC37 验证器裁决：`ai-assisted-unconfirmed == 0`，199 条 `auto-validator-l3l4-v1-*` 前缀（增量三十七）
- 增量三十五 D9 retest 队列铺底 12 条锚点（≥2026-10-27 触发 round=2）
- LLM 后端配置齐：`TRAJECTORY_LLM_{BASE_URL,MODEL,API_KEY,TIMEOUT_S,MAX_TOKENS}`（`llm/client.py` 第 36–40 行）
- rubric v1.2 已发（决策树新增 `subscription_wall` 一支）
- 阶段 4 L0–L5 七层全闭环，L4 真视觉 `MultimVLMReviewer` 已接（增量三十四）

---

## 一、现状：用户描述的 judge 路径 vs 项目已有

### 1.1 用户原话拆解为四条性质不同的诉求

| # | 诉求 | 工程对应 |
|---|---|---|
| A | **judge 初始创建**——根据 rubric 评分准则设计方案产生 | D-3 / D-9 的 LLM 评分层**未建** |
| B | **rubric 与 judge 配合**——judge 在黄金集上跑一遍、与黄金集自有标注对比 | `cmd_agreement` 已跑（口径 C：语义等价类一致）**只对硬分类**算；**软维度 1–5 没接通** |
| C | **judge 与 rubric 趋同**——调到 judge 能在黄金集上打出与黄金集自有标注相同的分数 | 阈值 `D10_THRESHOLD=0.90`，v3 实测 74.7%，未达；调参以 `alignment.py` 单一映射出口、`label_schema_version` 隔离口径落 |
| D | **judge 上岗后**——在新合成任务上打分与 rubric 高度一致 | judge 未建，`evaluation.cli` 默认入口接不进 |

### 1.2 项目里三条「准 judge」机制（已存在、未拆出 judge 名号）

| 层 | 文件 | 角色 | 是否复用作「judge」 |
|---|---|---|---|
| **代码 judge** | `evaluation/scorers/{code,d4,d5,d6}_scorer.py` | D-2 / D-4 / D-5 / D-6 评分 | ✅ 已经是 judge |
| **闸门 judge** | `rationale/gates.py` | D-9 三查（实体 / 因果 / 时序） | ⚠️ **部分**：被 rubric §6.2+ 闸门引用混淆 |
| **D-10 校准回路** | `evaluation/golden/builder.cmd_agreement` | 硬分类 ↔ 黄金集 label 一致率 | ✅ 已经在做 rubric↔judge 对齐 |
| scout 筛选器 | `evaluation/scout/{policy,io,cli,types}.py` | 自动评分 → 三分类（positive/negative/quarantine） | ❌ **不是 judge**：是 judge 的下游消费者（接 4 个 scorer 的 verdict，产 `ScoutTag`） |
| 真视觉 L4 | `executor/review/{vlm_reviewer,multim_vlm}.py` | LLM/视觉复核（阶段 4） | ❌ 是阶段 4 媒体复核层，不是 rubric 评分器 |

> **scout 不是 judge**——它读 4 个 scorer 的 verdict 做三分类（`tests/unit/test_scout_policy.py` 27 条 + `test_scout_contract`）。理解这点才能把「补 judge」和「补 scout」分开。

### 1.3 缺口

- **A**：未闭合
- **B**：半闭环（硬分类通，软维度未通）
- **C**：半闭环（硬分类阈值 74.7% < 90%；软维度连数都没有）
- **D**：未闭合

---

## 二、设计基线（6 条不能违反的纪律）

每条都对应项目里现有约束或踩过的坑。

1. **三态而非两态**——`score=None` ≠ `score=0`，前者进 quarantine 不入池，后者入出分档。`D2Verdict/D4Verdict/D5Verdict/D6Verdict.kind: "scored"|"undetermined"` 是统一形态。LLM judge 必须同款。
2. **失败是返回值，不是异常**——`LLMClient.chat()` 返回 `None` 而非抛（`llm/client.py` 第 268 行）。judge 入口永不抛：模型不可用 / 解析失败 / 拒答，全部走「判不出来」出口。
3. **不破坏依赖方向**——`evaluation → executor / assembler / perception` 单向（`docs/01-架构总览.md` 第 26–41 行）。LLM judge 必须仍走 `evaluation → perception` 的协议，不反向 import executor。
4. **raw CoT 不落盘**（CLAUDE.md 红线）。LLM judge 写出的 evidence 不能含 `<think>` 残段——复用 `llm/schema_parse.strip_think` 在 evidence 拼装前剥净。
5. **DIMENSION 与样本归属一对一**——D-3 / D-9 在样本级评分卡上各占一格，**judge 输出 1 个 score 而不是多个**。同 D-2/D-4/D-5/D-6 一条 `*Verdict` 的形态。
6. **judge 不投票、不集成**——rubric G-1/G-7 与 CLAUDE.md「禁止投票/集成」红线。生产链上分歧必须暴露给人看，**不能由 judge 内部悄悄择一**。

---

## 三、方案主体：三步走（按依赖排序）

### 第一步 · D-10 扩维（接通「软维度一致率」）

#### 目的

把 `cmd_agreement` 从「硬分类一致率」扩到「7 维 1–5 一致率」，让 rubric 与 judge 配合的闭环对软维度也通——不补这步，LLM judge 上岗后没东西与黄金集对账。

#### 数据源（已存在，无需新建）

- `evaluation/golden/data/v3/calibration.jsonl` + `holdout.jsonl`：`dimension_scores: {d3: 4, d9: 5, ...}`（黄金集 manifest `by_label` 已有，`builder.py` 早已写落盘的字段格式）
- v3 已冻结：`191 校准 / 103 持有 / seed=42`，by_label 全 8 label 覆盖（`docs/05-现状与验收.md` 第 819 行）

#### 形态

```jsonc
// annotations.jsonl 每行已带（INC37 之后 ai-assisted-unconfirmed == 0）
{
  "golden_id": "...",
  "round": 1,
  "label": "subscription_wall",
  "dimension_scores": {
    "d3": 4,                    // 软维度 1-5
    "d9": 5,
    "rationale_consistency": 4  // 已存在的 D-9 软维度
  }
}
```

#### 接入点

`evaluation/cli.py::evaluate` 已逐维度跑 SCORERS（`d2/d4/d5/d6`）。**扩 dim=3/D-9 后走另一条路径**：软维度直接读 P1 的 verdict，**不再调 scorer**，而是拿 `assembler.schema.Sample` 的 `score_*` 字段或新打的 LLM verdict；与黄金集 `dimension_scores[d]` 按「±1 一致为 true」算一致率。

#### 判定公式

- 硬分类沿用 `is_equivalent(t, p)`（`alignment.py`）
- 软维度新增 `dim_agreement(t, p)`：`|t - p| ≤ 1` 计一致，否则不一致（阈值可由 `cmd_agreement --dim-tolerance` 控制，默认 1）

#### 输出

```
v3 D10 一致率（口径 C · 等价类）：
  硬分类：216/289 = 74.7%（未达 90% 阈值）
  软维度 d3：8/12 = 66.7%（N=12，calibration 中带 dimension_scores.d3 的条数）
  软维度 d9：11/12 = 91.7%
```

#### Contract 守门（与 INC36/INC37 同款 7 条守门纪律）

- `test_cmd_agreement_dim_coverage` — 软维度名在 `d2..d9` 子集
- `test_cmd_agreement_dim_tolerance_default_1` — 默认 ±1
- `test_cmd_agreement_soft_dimension_空样本不参与` — N=0 时不报 0/0
- `test_cmd_agreement_soft_dimension_与硬分类口径分开` — 软维度不一致不传染硬分类
- `test_cmd_agreement_软维度读_v3_calibration_优先于_holdout` — 校准优先
- `test_cmd_agreement_manifest_schema_version_不破` — 软维度接入不动 `label_schema_version`
- `test_cmd_agreement_输出_JSON_含_soft_dims_键` — 报表可机器读

#### 风险

- 黄金集 `dimension_scores` 实际填的密度——若 v3 calibration 50 条 manual-review 全填，N≥12 上线即可；若密度低，第一轮只发 README 不准出数字。
- dim_tolerance 不是 5 档而是 1-5 软一致——与 D-10 阈值 0.90 的口径冲突（1 档差算 50% 一致而非 100%），需要先在 v3 50 条上跑出分布，**首次跑不设定阈值**，只报分布。

---

### 第二步 · D-3 LLM judge 落地

#### 目的

rubric §D-3「播放站识别正确性」管辖层 = LLM 裁判（`docs/设计方案/在线视频场景rubric.md` 第 66 行）——`docs/05-现状与验收.md` 第 410 行明写「LLM 判定层未建」。补 LLM judge 是补这唯一一条 LLM 维度的样本级评分。

#### 形态

新 `evaluation/scorers/d3_scorer.py`，仿 `d4_scorer.py`（最贴近 D-3 形态——都是混合层：代码判定 + LLM 复检）。**不过 D-3 没有「代码层」，全是 LLM**——所以结构比 d4_scorer 简单。

#### 协议先行——与 `evaluation/scorers/__init__.py` 同款

```python
@dataclass(frozen=True, slots=True)
class D3Verdict:
    score: int | None
    kind: VerdictKind
    category: str | None       # 「playable」/「nav_aggregator」/「review」/「irrelevant」/「ambiguous」
    evidence: str
    confidence: float | None     # 不采信模型自报，CONFIDENCE_JUDGED = 0.8
    reasons: tuple[str, ...] = ()

    @property
    def is_veto(self): return self.score == 1  # 1 档触发 G-1

    def to_json(self): ...
```

#### 输入

P2 sample（`assembler.schema.Sample` 对象，吃过的）+ 站点 URL。

#### 判据（rubric §D-3 锚点 + 边界规则）

| 锚点档 | 含义 |
|---|---|
| 5 | 类别判对 + 理由引用观察里的具体依据 |
| 4 | 类别判对，理由为泛泛描述 |
| 3 | ambiguous 站处置正确（判「选择进入验证」）但 ambiguous 未单列 |
| 2 | 类别判错（播放站判成非播放站或反之）|
| 1 | 未判（fail-closed）且该站本可判（→ 一票否决）|

#### prompt 契约

沿用 `llm_perceptor.CONTRACTS` 同款——后端不强制 json_schema：

```jsonc
{
  "category": "playable|nav_aggregator|review|irrelevant|ambiguous",
  "playable_subtype": "free|login_required|partial_free|subscription",  // 仅 playable 时必填
  "evidence": "依据引用观察原文，不含页面引用以外的猜测",
  "confidence": 0.0-1.0  // 不采信
}
```

#### judge 主入口

```python
def score_d3(sample: Sample, *, llm_client: LLMClient | None = None) -> D3Verdict:
    """D-3 打分。

    Args:
        sample: P2 样本（与 d4/d5/d6 同款 dataclass | Mapping 都吃）。
        llm_client: LLM 后端；为 None = 不在 judge 链路（让 cmd_agreement 可空跑）。
    """
```

#### 黄金集校准回路

1. **首次跑全量**——对 v3 calibration 191 条站点级候选跑 `score_d3`，**不调参**，收集分布。
2. **分桶调 prompt**——按 `category` 5 类 × `evidence_empty` 两桶分析：哪一类有系统性偏（"playable 判 nav_aggregator" 占多数）。
3. **few-shot 注入**——从 v3 manual-review **66** 条里挑 6-8 条做 few-shot prompt（人工合计 **82** 条，含 16 条 `reesn` 署名）（**不能从 ai-assisted* 桶挑**——违反 D-9 边界③）。
4. **黄金集对账**——`cmd_agreement --soft-dim d3` 跑出 D-3 软维度一致率；以**先跑出分布再定阈值**的口径走，**首次阈值不写死**。
5. **周轮复跑**——D-9 retest 14 天复盘机制同款：judge 改 prompt 后，跑校准集算一致率，若下降就回滚。

#### Contract 守门（参考 `test_d4_scorer.py` 现有结构）

8 条 contract 测试：

- `test_d3_judge_5分_依据引用观察` — 满档判对且理由有原文
- `test_d3_judge_2分_类别判错` — playable 判 nav_aggregator
- `test_d3_judge_1分_未判且可判` — fail-closed + 该站有视频 → 一票否决
- `test_d3_judge_ambiguous_边界` — ambiguous 站判对入 3 档
- `test_d3_judge_不可判定` — observation 三项全空 → `kind=undetermined`（**不走 1 档**）
- `test_d3_judge_无_LLM_client_不抛` — pass `llm_client=None` 时返 `undetermined` 不报
- `test_d3_judge_rubric_判据引用rubric_v1.2` — 7 维名称、判定阈值与 rubric v1.2 §D-3 对齐
- `test_d3_judge_soft_cot_red_line` — evidence 写完前 `strip_think`（CLAUDE.md 红线）

#### 与现有模块关系

- **复用** `LLMClient` + `schema_parse`（`llm/__init__.py` re-export）——judge 不自己建客户端
- **复用** `gates.observation_shortfall`（`rationale/gates.py` 第 115 行）——d3_scorer 独立实现保持依赖方向；同款口径用 `tests/contract/test_observation_shortfall_consistency.py` 双向盯住（与 `code_scorer._observation_shortfall` 同款纪律）
- **不 import** `executor` / `assembler` —— `evaluation → executor` 反向依赖红线（`docs/01-架构总览.md` 第 36–41 行）

---

### 第三步 · D-9 LLM judge 拆出

#### 目的

`rationale/gates.py` 是 D-9 的「代码三查」（实体 / 因果 / 时序），纯代码、零 LLM。rubric §D-9 管辖层写的是「LLM 裁判（一致性闸门复检）」——意思是闸门**之上**还有 LLM 复检层，**当前 LLM 复检层没拆出来**。

#### 形态

`evaluation/scorers/d9_scorer.py`，仿 `d3_scorer` 同款协议。

#### 与 gates.py 的分工

- **gates.py** 是**事前判据**（实体是否在观察里、时序是否超前）——纯代码、零成本
- **d9_scorer** 是**事后复检**（rationale 写得是否「通顺且不自相矛盾」）——LLM 复检

#### 判据（rubric §D-9 锚点）

| 锚点档 | 含义 |
|---|---|
| 5 | 全部锚定；不确定处如实表达 |
| 4 | 1 处措辞含糊，无事实错误 |
| 3 | 1 处轻微幻觉（不影响动作）或 1 处后见之明 |
| 2 | 系统性不符或套话替代推理 |
| 1 | 思考编造关键事实并驱动错误动作（与 D-2 联动） |

#### 触发条件

- `gate.status == "failed"` → 强制走 LLM 复检（硬事实有问题，看 LLM 怎么圆）
- `gate.status == "passed" + undeterminable 非空` → 走 LLM 复检（不能全判的抓出）
- `gate.status == "passed" + undeterminable 空` → **不走** LLM 复检（成本/收益双因子都不达标）

> **为什么不是所有场景都触发**：rationale 平均 800–1500 token / 次，3 查全过且无可判性短板的 rationale 没有 LLM 复检的余地——**全跑一遍是浪费钱**，实测 38 份存档里有 32 份 `passed + undeterminable 空`，都跑 LLM 是把单条样本成本翻一倍。

#### prompt 契约

```jsonc
{
  "verdict": "anchored|vague|mild_hallucination|hindsight|systematic|misleading",
  "evidence": "引用观察 + rationale 原文中的具体短语",
  "confidence": 0.0-1.0
}
```

#### 输入

P2 sample + `gate=passed/failed/not_run` + `gate.undeterminable` + `gate.findings`

#### Contract 守门

7 条 contract 测试：

- `test_d9_judge_5分_全锚定`
- `test_d9_judge_3分_后见之明`（措辞像「事后才知道」）
- `test_d9_judge_1分_编造关键事实`（与 D-2 联动核查：rationale 引用了 click 但 obs 里 click=None）
- `test_d9_judge_gate_failed_强制触发` — `gate.status == "failed"` 必须调 LLM
- `test_d9_judge_gate_passed_且_undeterminable_空_不触发` — 不调 LLM（成本控制）
- `test_d9_judge_observation_shortfall_走_undetermined` — 与 gates.py 同源
- `test_d9_judge_red_line_strip_think` — evidence 拼装前 `strip_think`

#### 风险

- rationale 写入与复检的耦合——rationale 落 P1 后才能复检，所以**复检滞后一个批次**；第一版只对带 rationale 的样本打分（v3 +之后批次），老批次 `gate=not_run` 不判。
- LLM judge prompt 反复调参的成本——和 D-3 同款，「先跑出分布再定阈值」。

---

## 四、与现有模块的依赖关系

```text
evaluation/scorers/d3_scorer.py    ← 新增 · LLM judge
evaluation/scorers/d9_scorer.py    ← 新增 · LLM judge（与 d3 同款形态）
evaluation/golden/builder.py        ← cmd_agreement 扩 dim
evaluation/cli.py                   ← SCORERS 注册新三键（--include-llm-judge）
                                          ↑ 默认关，--include-llm-judge 打开
rationale/gates.py                  ← 不动（d9_scorer 复用 observation_shortfall，
                                          独立实现保持依赖方向）
perception/llm_perceptor.py         ← 不动（已建，按契约复用）
llm/client.py                       ← 不动（judge 不自己建客户端）
executor/, assembler/               ← 不动（依赖方向红线）
```

### 依赖方向修正（必加）

新依赖方向：`evaluation/scorers/d9_scorer.py → rationale/gates.py`。这条新方向**只读**（仅 `observation_shortfall` 一个函数），与 `gates.py` 模块 docstring「刻意与 `code_scorer._observation_shortfall` **同款判据但分开实现**——rationale 不 import evaluation（依赖方向），而两处口径靠 `tests/contract/` 对齐」的纪律**直接冲突**——`d9_scorer` 在 evaluation 侧，import rationale 边的 helper 是反向依赖。

**修正方案**（必加）：d3_scorer / d9_scorer **不 import rationale**，自己实现 `_observation_shortfall`（`tests/contract/test_observation_shortfall_consistency.py` 双向盯住两处口径，与 code_scorer 同款）。

---

## 五、风险与边界

| 风险 | 处置 |
|---|---|
| 软维度 1–5 一致率阈值未定 | **首次跑只出分布**，不写死阈值 |
| LLM judge prompt 工程成本（与 D-3 LLM Perceptor 同源） | 复用 `LLMClient` + `schema_parse`，复用 `_ask_json` 的 page_quotes + fallback 链路——LLM prompt 第二轮 8/10 page quote 命中率（增量二十一）已成路径 |
| LLM judge 与现有代码 judge 的关系混淆 | 三态（`score=None` ≠ `score=0`）+ `kind: scored/undetermined/not_run` 同款形态；现有 4 个 scorer 不动 |
| rationale 复检与闸门边界混淆 | d9_scorer 入口**先**调 gates.gate；闸门**事后**判，judge**先**判——判据口径不同：闸门看「事实是否有错」，judge 看「写得是否通顺不自相矛盾」 |
| D-10 < 90% 阈值（v3 74.7%）未解 | **不在本方案范围**——INC32 簇 1 type A/C、NO_PLAY_RESOURCE→ACCESS_BLOCKED 等 systematic bias 是 LLM prompt 改善 + 重采集（`docs/05-现状与验收.md` 第 859–861 行），与 judge 上岗正交 |
| LLM judge 不接进 evaluation/cli 默认入口 | `--include-llm-judge` 关，默认只跑 4 个代码 judge；与现有 `evaluation.cli` 不破（CLAUDE.md 红线「生产链上禁止投票/集成」） |
| 大批量跑评测的延迟（LLM 每条样本 1-3s） | D-9 仅在 `gate.failed` 或 `gate.passed + undeterminable 非空` 触发，**预期 30-40% 样本走 LLM**，其余走快路径 |
| 双向 contract 缺位 | 与 code_scorer 同款——`tests/contract/test_observation_shortfall_consistency.py` 双向盯住 d3/d9 与 gates.py 的 `_observation_shortfall` 口径 |
| 黄金集 few-shot 池污染 | v3 manual-review **66** 条（人工合计 **82** 条，含 16 条 `reesn` 署名）是金标准，但**不能从 `ai-assisted*` 桶挑**（违反 D-9 边界③「单人系统性偏差测不出」）；82 条够 6-8 条 few-shot 用，但若要扩到 20 条少，需新一批人工复核 |

---

## 六、落地顺序（按依赖 + ROI 排）

按「先接通闭环 → 再补 LLM judge」的依赖序：

| 步骤 | 内容 | 文件 | 关键判据 |
|---|---|---|---|
| **第一步** | D-10 扩维 | `evaluation/cli.py` cmd_agreement + `evaluation/golden/builder.py` | v3 50 条 manual-review 上跑出软维度 d3/d9 分布；首次不发阈值 |
| **第二步** | D-3 LLM judge | `evaluation/scorers/d3_scorer.py` + `tests/unit/test_d3_scorer.py`（~12 条 contract） | v3 calibration 191 条跑出 d3 一致率；首批 few-shot 从 manual-review **66** 条（人工合计 82 条）挑 |
| **第三步** | D-9 LLM judge | `evaluation/scorers/d9_scorer.py` + `tests/unit/test_d9_scorer.py`（~10 条 contract） | 与 `gates.observation_shortfall` 双向 contract 测试（同 `code_scorer` 同款口径） |

**前置**：第一步必须先于第二、三步——没有 D-10 软维度一致率，D-3/D-9 上岗后没东西对账，**judge 与 rubric 配合的闭环**就只走硬分类那一半。

---

## 七、已知后续路径（不在本批范围）

1. **D-9 LLM 复检只对带 rationale 的样本**——老批次 `gate=not_run` 不判；待 38 份存档 + 后续批次带 rationale 后才有完整数据集。
2. **few-shot 池**——v3 manual-review **66** 条（人工合计 **82** 条，含 16 条 `reesn` 署名）是金标准，但**不能从 `ai-assisted*` 桶挑**（违反 D-9 边界③）；82 条够 6-8 条 few-shot 用，若要扩到 20 条少，需新一批人工复核。
3. **D-3 prompt 迭代**——v3 calibration 跑出来后按「系统性偏」分桶调 prompt；这与 `perception/llm_perceptor` 的 P0-P4a 调 prompt 同源（增量二十一），可直接套用成熟链路。
4. **judge 与 gold 的「双向 contract」**——本方案只做「gold 是真值，judge 校准」这一向；反向「judge 改 gold」未启（INC32 / INC33 都是这种**单向编辑**纪律：gold 改 label 是追溯，禁止 judge 反向改）。
5. **`cmd_agreement` 输出接进 CI**——D-10 软维度一致率上线后，CI 门禁可加一条「软维度 d3 ≥ X%」——阈值首次跑出分布后再定，**不写死**。
6. **D-3 LLM judge 与 scout 联动**——scout 读 4 个 scorer 的 verdict 做三分类；D-3 上岗后 scout 的 `d3_*` tag 接入同款 pattern（评估 score 维度、判 `d3_failed`、调判定标签）。独立批。
8. **D-9 LLM judge 与闸门的「同一 caller 走两条路」**——d9_scorer 与 gates.gate 共享同一入口（`Sample` + observations），但各自返回 verdict；后续 assembler/build 时可将两者 verdict 合并进 P2 的 `gate` 字段，独立批。

---

## 八、要点回顾（与用户原话逐句对账）

| 用户原话 | 本方案对应 |
|---|---|
| **judge 初始创建可以根据 rubric 评分准则设计方案产生** | D-3 / D-9 LLM judge 按 v1.2 §D-3 / §D-9 锚点 + 边界规则设计 prompt；与 `d5_scorer` 同款「决策树逐节点判」形态 |
| **通过对黄金数据集评分并与黄金数据集自有标注分对比调整逻辑趋同** | `cmd_agreement --soft-dim d3/d9` 算出 LLM judge 在 v3 黄金集上的一致率；按 systematic bias 分桶调 prompt |
| **评判器与 rubric 是相互配合让评分器能在黄金数据集上打出与其相同分数** | judge 协议 + rubric 锚点双向对齐（contract 测试盯住）；`tests/unit/test_d3_scorer.py::test_d3_judge_rubric_判据引用rubric_v1.2` 守住 |
| **一致后评判器在其他合成任务上打分才会高度一致准从 rubric 评分准则** | 一致率达到阈值（首次跑出分布再定）→ `evaluation.cli --include-llm-judge` 默认接入（CLAUDE.md 红线「禁止投票/集成」下显式开关） |
| **当前架构是否缺少评判器 judge** | **judge 职能已有 2 个**（代码 judge 4 个 + 闸门 judge）；**LLM judge 0 个**（D-3 + D-9 复检层）；**judge 与 rubric 闭环对硬分类通、对软维度未通** |

---

## 九、文档治理

| 字段 | 内容 |
|---|---|
| 评审 | 待评审 |
| 落地执行 | 已落地 |
| 上次重读 | 2026-10-14 |
| 关联设计 | 总体方案 §5.2 / §5.3；02-rubric与代码对齐 §2.9；03-rubric修订与实施方案；08-阶段 4 七层定位 |
| 关联增量 | 三十三（v3 冻结）/ 三十五（D9 retest queue 铺底）/ 三十七（验证器裁决）/ 三十八（三步走全部落地） |
| 变更规则 | 只增不删；每次变更升版本号并记录于下表；黄金集同步重打受影响维度 |

### 变更记录

| 版本 | 日期 | 变更内容 | 修订人 |
|---|---|---|---|
| v0.1 | 2026-10-14 | 初稿：现状 + 设计基线 + 三步走 + 落地顺序 | — |
| v0.2 | 2026-10-14 | 三步走全部落地（代码 + 41 条新增 contract + 全量 2165 passed） | — |

---

## 相关文档

- [在线视频场景rubric.md](在线视频场景rubric.md) —— 评分标准本体 v1.2
- [02-rubric与代码对齐.md](02-rubric与代码对齐.md) —— 维度对齐账
- [00-总体方案.md](00-总体方案.md) §5.2 / §5.3 —— 黄金集规格与一致性测量
- [08-阶段4七层定位与黄金数据集rubric门禁方案.md](08-阶段4七层定位与黄金数据集rubric门禁方案.md) —— 阶段 4 七层现状
- [09-rubric-gate改造方案v04-实施细节.md](09-rubric-gate改造方案v04-实施细节.md) —— scout 落地形态
