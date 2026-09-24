# Agent 轨迹数据两层评分系统设计（契合本项目）

> 场景：浏览器在线视频网址检索与整理的 agent 轨迹数据（LLM 修改后）评分。
> 参考方案：用户提供的对比式 + 独立式两层评分方法论。
> 本文档在该方法论基础上，对齐本项目 gdr 精修管道的现有能力，给出**复用 / 新建 / 改造**的落地设计，不另起炉灶。

---

## 〇、结论先行

参考方案的两层结构（对比式 → 精修 → 独立式终审）与本项目现有管道的分层方向**一致**，但粒度与覆盖面存在三处缺口：

| 维度 | 参考方案要求 | 项目现状 | 缺口 |
|---|---|---|---|
| 对比式粒度 | **轨迹级**（原始轨迹 vs 修改后轨迹并排） | 仅**块级**（L1/L2/L3，original_block vs refined_content） | 需把对比式从块级提升到轨迹级 |
| 独立式红线 | 红线合规一票否决（侵权/隐私/注入/ToS） | **无红线 validator** | 需新建 |
| 质量分制 | 1-5 分 + 子分门槛（executability/action_obs_alignment 均 ≥4） | [0,1] 连续 + tier（easy/medium/hard） | 需加 1-5 映射 + 子分门槛 |
| diff 分类 | required_change / incidental_change / regression | **无** | 需新建轨迹级 diff 分类器 |
| 金标/漂移 | 金标集 + 锚点 + 漂移监控 | **无** | 需新建 |

**改造原则**：扩展现有 gdr 管道，不新增并行评分系统。第一层对比式插入 `runner.py` step 6（reassemble 终审）之前，fail 携带 diff 回到 step 9 精修；第二层独立式作为 step 6 之后的最终门控。

---

## 一、现状分析（项目已有能力）

### 1.1 块级对比式校验（已有，可复用）

`gdr/validators/__init__.py:10` `validate_block` 级联 L1→L2→L3，均为 **reference-based**：

| 层 | 文件 | 机制 | 输出 |
|---|---|---|---|
| L1 | `gdr/validators/l1_rules.py:65` | 确定性规则（实体集合 ⊆） | `bool` |
| L2 | `gdr/validators/l2_semantic.py:55` | 嵌入余弦相似度（阈值 thinking≥0.85/toolcall≥0.90/toolresult≥0.80） | `(passed, sim, threshold)` |
| L3 | `gdr/validators/l3_judge.py:8` | LLM 评审（orig vs refined） | `{verdict, score:0-10, reason}` |

数据载体 `BlockRefineRecord`（`gdr/domain/schema.py:61`）已同时保留 `original_content` + `refined_content`，是块级原 vs 修的现成载体。

### 1.2 轨迹级独立式评分（已有，可复用 + 扩展）

- **reassembler 终 judge**（`gdr/reassembly/reassembler.py:955`）：reference-free，输出 0-10 分 + `intent_fulfillment ∈ {0,1,2}`。
- **quality_scorer**（`gdr/core/quality_scorer.py:160`）：零 LLM 组合函数，7 维加权（health/judge/intent/modified/diversity/noise/depth）→ `training_value_score ∈ [0,1]` + `complexity_tier ∈ {easy,medium,hard}`。

### 1.3 可复用的现成机制

| 现成能力 | 复用为 | 位置 |
|---|---|---|
| Verdict 四态 + `aggregate_results`（FAIL>ERROR>INCONCLUSIVE>PASS） | 两层评分聚合层 | `simulate_serve/domain/validation.py:9,50` |
| `CriterionResult` / `ValidationReport` schema | 评分结果载体 | `validation.py:24,35` |
| reassembly 跨 toolcall 配对扫描 | action-obs 对齐检查（breakpoints） | `reassembler.py:536-554`、`retry_loop_clip.py:113-122` |
| `BlockContextView`（referenced_by/importance/is_transition_point） | 轨迹结构特征 | `gdr/core/context_understanding.py:275` |
| `MessageHealth`（health_score + is_healthy） | 消息级健康评分 | `gdr/domain/schema.py:75` |
| `DefectTag` 14 种 | 对齐断裂点标签 | `gdr/domain/schema.py:10` |

### 1.4 缺口（需新建）

1. **轨迹级对比式评分器**：original_session vs refined_session 整体对比（现有仅块级）。
2. **轨迹级 diff 分类器**：required_change / incidental_change / regression 三类判定。
3. **红线合规 validator**：侵权站点 / 隐私泄露 / prompt 注入 / ToS 违规。
4. **1-5 分制映射 + 子分门槛**：executability / action_obs_alignment 单科 ≥4。
5. **金标集 / 锚点集 / 漂移监控**：配置 + 监控脚本。
6. **并排 diff 工具**：结构化展示修改点（现仅 `difflib.SequenceMatcher` 相似度）。

---

## 二、契合项目的两层设计

### 2.1 第一层：对比式评分（Reference-based，轨迹级）

**定位**：在块级 L1/L2/L3 之上新增轨迹级对比层，回答"改写是否改坏了"，输出可解释修改点驱动精修管道。

#### 维度 1：任务意图保真度（Fidelity）

评什么：修改后轨迹是否保留原始轨迹的——
- 核心任务目标（如"检索某剧集合法观看源并整理成清单"）
- 目标视频集合 / 关键网址集合（增删是否合理、是否丢失目标结果）
- 检索策略骨架（使用了哪些站点、搜索路径多样性）

评法：两版并排，评审模型分别抽取「任务目标陈述 + 关键操作链 + 最终结果集」三要素，逐要素比对。**保真/失真判断 + 失真点列表**，不做绝对打分。

**复用**：
- 任务目标陈述 ← `gdr/core/user_intent.py:58` `heuristic_user_intent`（已有）
- 关键操作链 ← `BlockContextView.is_transition_point` + `key_decisions`（`context_understanding.py:275`）
- 最终结果集 ← `simulate_serve/validation/claims.py:19` `extract_claims`（已抽 url + list_item）

#### 维度 2：指令遵循度（Instruction Adherence）—— 核心新增

评什么：是否只做了要求的修改（替换失效链接/脱敏/统一格式），未做无关改动。

评法：对 action 序列做**结构化 diff**（按 step 编号对齐），逐项判断每个差异属于：
- `required_change`（指令要求的改动）✅
- `incidental_change`（无指令依据的改动）⚠️
- `regression`（把原来对的地方改坏了）❌

**实现**：
- 块级 diff 数据源 ← `BlockRefineRecord`（已存 original_content + refined_content + edit_status）
- 轨迹级聚合 ← 新建 `gdr/validators/l4_trajectory_diff.py`，遍历所有 BlockRefineRecord，按 BlockIndex 排序产出 step_range 级 diff_summary
- 分类判定 ← 需**指令上下文**（本次精修的指令模板，如"替换失效播放链接"）。指令上下文从 `gdr/config/settings.py` 的 refiner 配置 + `BlockRefineRecord.refine_log` 推断
- regression 判定 ← 块级 L1/L2/L3 已有 verdict，`edit_status=ROLLBACK` 或 L3 verdict=fail 即 regression 候选

#### 维度 3：轨迹逻辑增益（Trajectory Coherence Delta）

评什么：相对原始版，action-observation 对齐性、操作因果链完整性是升是降。

评法：成对比较的相对判断（improved/unchanged/degraded）+ 断裂点定位。

**复用**：
- action-obs 对齐检查 ← 扩展 `reassembler.py:536-554` 跨 toolcall 配对扫描：每 toolcall 必有同 id toolresult，且 toolresult.state=success；缺配对或 state=error 即断裂点
- 因果链完整性 ← `BlockContextView.depends_on` + `referenced_by`（`context_understanding.py:275`），断引用即断裂点
- 相对判断 ← 对比 original_session 与 refined_session 的断裂点数量

#### 对比式输出 Schema（新增 `TrajectoryCompareResult`）

```python
# gdr/domain/scoring_schema.py（新建）
class FidelityVerdict(BaseModel):
    verdict: Literal["faithful", "degraded"]
    lost_elements: list[str]
    preserved_core: list[str]

class DiffItem(BaseModel):
    step_range: str          # "12-15" / "23"
    type: Literal["required_change", "incidental_change", "regression"]
    note: str

class InstructionAdherence(BaseModel):
    diff_summary: list[DiffItem]
    score: Literal["pass", "review", "fail"]

class Breakpoint(BaseModel):
    step: int
    issue: str

class TrajectoryCompareResult(BaseModel):
    pair_id: str
    fidelity: FidelityVerdict
    instruction_adherence: InstructionAdherence
    coherence_delta: Literal["improved", "unchanged", "degraded"]
    breakpoints: list[Breakpoint]
    overall: Literal["pass", "fail"]  # pass→独立终审, fail→精修管道
```

### 2.2 第二层：独立式评分（Reference-free，轨迹级）

**定位**：训练集最终准入门控，回答"这份数据本身能不能用"。扩展 reassembler 终 judge + quality_scorer，新增红线合规。

#### 维度 1：红线合规（二值 + 标签）—— 核心新增

不看原始版，直接对修改后轨迹判断：
- 是否泄露用户隐私信息（账号、Cookie、历史记录中的个人信息未脱敏）
- 是否包含 prompt 注入或诱导性内容（观察页中的恶意指令被保留进轨迹）
- 轨迹中 agent 行为是否违反站点 ToS（如绕过付费墙的操作被示范）

输出：`violation: none | {type, step_location, evidence}`。**红线零违规，一票否决**。

**实现**：
- 新建 `gdr/validators/redline.py`，零 LLM 规则层 + 可选 LLM 复核
- 侵权站点 ← 黑名单 URL 模式（配置 `gdr.scoring.redline.piracy_url_patterns`）
- 隐私泄露 ← 正则（账号/Cookie/手机号/邮箱）扫所有 ToolresultBlock.output_text + TextBlock.text
- prompt 注入 ← 检测 ToolresultBlock.output_text 中的指令性语句（"ignore previous"/"system:" 等）
- ToS 违规 ← 操作模式黑名单（如 `click` 付费墙 bypass 的 DOM selector 模式）
- 接入聚合 ← 复用 `aggregate_results`，红线 violation → Verdict.FAIL，强制一票否决

#### 维度 2：绝对质量分（1-5 分，训练集放行依据）

对修改后轨迹独立打分，评分锚点：

| 分值 | 轨迹可执行性 | action-obs 对齐 | 最终整理结果质量 | 语言/格式 |
|---|---|---|---|---|
| 5 | 全部操作在真实浏览器中可复现 | 每步 action 有对应页面状态支撑 | 结果完整准确、来源合法、结构清晰 | 自然、一致 |
| 4 | 个别页面动态内容有合理容差 | 无断裂 | 结果准确但来源覆盖不全 | 基本自然 |
| 3 | 主流程可执行，辅助步骤存疑 | 1-2 处弱对齐 | 结果部分正确 | 有明显格式噪音 |
| ≤2 | 关键 URL 幻觉 / 操作不可复现 | 多处断裂 | 结果错误或缺失 | 不合格 |

**实现**：
- **不废弃 quality_scorer 的 [0,1] 分**，新增映射函数 `[0,1] → 1-5`（线性映射 + tier 校准：easy→5/4、medium→4/3、hard→3/2/1）
- 子分对齐参考方案四维：
  - `executability` ← 新建，扫 ToolcallBlock.input 中的 URL 是否在 ToolresultBlock.output_text 中出现（导航/搜索结果验证）
  - `action_obs_alignment` ← 复用 reassembly 配对扫描断裂点计数 → 5 - 断裂数
  - `result_quality` ← 复用 reassembler 终 judge 的 intent_fulfillment（0/1/2 → 3/4/5）+ `extract_claims` 命中率
  - `language` ← 复用 L2 嵌入相似度均值 + meta_tag_contamination 检测
- **子分门槛**：executability ≥4 且 action_obs_alignment ≥4，不允许单科短板用总分弥补

#### 维度 3：任务完成度（并入质量分）

最终产物（视频网址清单）是否正确回应任务目标：检索是否命中、链接是否可达、整理结构是否可直接使用。

**复用**：`simulate_serve/validation/pipeline.py:21` `ValidationPipeline` 的 6 个确定性 validator（keyword/format/fields/count/url_syntax/constraint）+ `extract_claims`。这部分**已是任务验收现成能力**，直接调用 task.criteria 对 refined session 最终 text 块验收。

#### 独立式输出 Schema（新增 `TrajectoryFreeResult`）

```python
# gdr/domain/scoring_schema.py（新建）
class RedlineViolation(BaseModel):
    type: Literal["piracy", "privacy", "prompt_injection", "tos_violation", "sensitive_content"]
    step_location: int
    evidence: str

class RedlineResult(BaseModel):
    violation: bool
    labels: list[RedlineViolation]

class AbsoluteQuality(BaseModel):
    score: int  # 1-5
    subscores: dict[Literal["executability", "action_obs_alignment", "result_quality", "language"], int]
    fail_reasons: list[str]

class TrajectoryFreeResult(BaseModel):
    traj_id: str
    redline: RedlineResult
    absolute_quality: AbsoluteQuality
    decision: Literal["accept", "reject", "resample"]
```

---

## 三、管道接入点（契合现有 runner.py 时序）

现有 `gdr/pipeline/runner.py:289` `process_one` 时序（step -1 → 7）。两层评分插入位置：

```text
┌─ step -1~5：硬过滤 → 健康分 → CU → fold → retry_loop_clip → retrack → user_intent → router → policy → 精修 ─┐
│                                                                                                              │
▼                                                                                                              │
step 5.5【新增】第一层对比式评审（TrajectoryCompareResult）                                                      │
  │  输入：original_session（from_trajectory 原始） + refined_session（当前） + 指令上下文                         │
  │  fail/review ──────────────────────────────────────────────────────────────┐                               │
  │  pass                                                                       │                               │
  ▼                                                                            ▼                               │
step 6 reassemble 终审（现有）                                          step 5.6 回 step 9 精修                │
  │  携带 TrajectoryCompareResult.diff_summary + breakpoints 做定向重写 ◄────────┘                               │
  ▼                                                                                                              │
step 6.5【新增】第二层独立式评审（TrajectoryFreeResult）                                                          │
  │  红线一票否决 → reject                                                                                       │
  │  绝对质量分 <4 或子分门槛未达 → reject/resample                                                              │
  │  accept                                                                                                      │
  ▼                                                                                                              │
step 7 落盘 C2 refined Session（现有，metadata 新增 scoring 字段）                                                 ┘
```

**关键设计**：
- 第一层 fail **不直接淘汰**，携带 diff_summary + breakpoints 回到 step 9 精修（复用现有 refiner 重试 + 升级机制），最多 `max_retries_9b` 轮后仍 fail 才淘汰。这对应参考方案"不过关进精修"。
- 第二层是**最终门控**，reject 即淘汰，resample 标记待重采样。这对应参考方案"放行/退回"。
- 原始轨迹保留：`from_trajectory` 加载的 original_session 在整个 process_one 期间保持不变，对比式评分随时可取。

---

## 四、模块清单（新建 / 改造）

### 4.1 新建

| 路径 | 职责 | LLM |
|---|---|---|
| `gdr/domain/scoring_schema.py` | `TrajectoryCompareResult` / `TrajectoryFreeResult` 及子模型 | 零 |
| `gdr/validators/l4_trajectory_compare.py` | 轨迹级对比式评分（fidelity + instruction_adherence + coherence_delta） | 1 次（fidelity 三要素抽取 + 比对） |
| `gdr/validators/l4_diff_classifier.py` | diff 分类器（required/incidental/regression 判定） | 0-1 次（规则优先，模糊项 LLM 兜底） |
| `gdr/validators/redline.py` | 红线合规 validator（侵权/隐私/注入/ToS） | 0-1 次（规则优先，可疑项 LLM 复核） |
| `gdr/validators/free_quality.py` | 独立式绝对质量分（1-5 + 子分门槛） | 0（组合函数，复用 quality_scorer + reassembler judge） |
| `gdr/evaluator/golden_set/` | 金标集 + 锚点集 + 漂移监控 | 0 |
| `gdr/evaluator/drift_monitor.py` | 漂移监控脚本（锚点分稳定性） | 0 |
| `gdr/prompts/trajectory_compare.yaml` | 轨迹级对比式评审 prompt | — |
| `gdr/prompts/redline.yaml` | 红线复核 prompt | — |

### 4.2 改造

| 路径 | 改动 |
|---|---|
| `gdr/pipeline/runner.py` | step 5.5 / 6.5 插入两层评分；step 5.6 fail 回路；metadata 写入 scoring 字段 |
| `gdr/domain/schema.py` | `Session.metadata` 新增 `trajectory_compare: TrajectoryCompareResult` + `trajectory_free: TrajectoryFreeResult` |
| `gdr/core/quality_scorer.py` | 新增 `[0,1] → 1-5` 映射函数；子分拆分（executability/action_obs_alignment） |
| `gdr/reassembly/reassembler.py` | 配对扫描扩展为对齐完整性校验，输出 breakpoints |
| `gdr/config/settings.py` | 新增 `scoring` 配置 section |
| `config/config.example.yaml` | 新增 `gdr.scoring` section |

---

## 五、配置项设计

`config/config.example.yaml` 新增 `gdr.scoring` section：

```yaml
gdr:
  scoring:
    # 第一层对比式
    enable_trajectory_compare: true
    compare_fidelity_llm: true          # fidelity 三要素抽取用 LLM
    compare_diff_classifier: "rule_first"  # rule_first / llm_only / hybrid
    compare_coherence_reuse_reassembly: true  # 复用 reassembly 配对扫描

    # 第二层独立式
    enable_free_quality: true
    enable_redline: true
    redline:
      piracy_url_patterns: []           # 侵权站点 URL 正则黑名单
      privacy_patterns: []              # 隐私正则（账号/Cookie/手机号/邮箱）
      prompt_injection_patterns: []     # 注入性语句
      tos_violation_selectors: []       # ToS 违规 DOM selector 模式
      llm_review_suspicious: true       # 可疑项 LLM 复核
    absolute_quality:
      min_score: 4                      # 放行阈值 ≥4/5
      min_subscore_executability: 4     # 子分门槛
      min_subscore_action_obs: 4
      score_mapping: "linear_tier"      # [0,1]→1-5 映射策略

    # 金标 / 漂移
    golden_set:
      enabled: false
      path: "data/golden_trajectories/"
      anchor_count: 50                  # 锚点轨迹数
      drift_threshold: 0.3             # 漂移告警阈值（轨迹场景收紧）
      drift_action: "alert"            # alert / rollback_model / recalibrate
      probe_pairs_per_batch: 20        # 每批混入探针对数
```

---

## 六、落地流程

### 第一步：金标集

抽样 200–300 条轨迹，人工同时完成两套打分（对比式 diff 判断 + 独立式质量分）。用于：
- 校准评审 prompt（评审与人工一致率 <75% 时迭代 prompt）
- 设定独立式阈值
- **额外校准 action-obs 对齐判断的人工一致率**（轨迹评审中模型最容易和人类分歧的点）

存放 `data/golden_trajectories/`，每条含 `original_session` + `refined_session` + `human_compare_result` + `human_free_result`。

### 第二步：阈值

- **对比式**：`instruction_adherence = pass` 且无 regression 类型 diff、`coherence_delta ≠ degraded` → 进入终审。
- **独立式门控**（唯一放行依据）：
  - 红线合规 = 零违规（一票否决）
  - 绝对质量分 ≥ 4/5
  - `executability` 与 `action_obs_alignment` 子分均 ≥ 4（不允许单科短板用总分弥补）

### 第三步：漂移监控

- 金标集中固定 50 条锚点轨迹随每批评审，监控锚点分稳定性。
- 漂移 > 0.3 分（比文本场景收紧，因轨迹评分方差更大）即触发金标差值修正或评审模型版本回滚。
- 对比式评审风险较低（相对判断），但仍建议每批混入 20 条"已知答案"的探针对（如人工构造一条故意破坏对齐的轨迹）验证评审灵敏度。

---

## 七、为什么必须两层组合（本场景具体化）

| 只用一层 | 致命盲区 |
|---|---|
| 只做对比式 | 改写模型持续输出"比原始好一点、但 URL 大量幻觉或触版权红线"的轨迹——对比式永远显示"改进"，红线和绝对准入线却持续失守 |
| 只做独立式 | 轨迹绝对打分方差极大（同一条轨迹两次评分可差 2 分），无法定位问题是原始固有还是改写引入，精修管道拿不到"哪一步改坏了"的线索 |

**组合优势**：对比式利用原始轨迹这个天然参照物，提供稳定性 + 可解释的修改点定位；独立式补齐绝对质量与合规的最终答案。轨迹数据的幻觉 URL、对齐断裂、红线站点三类高风险问题，恰好被两层分别覆盖：前两类由对比式定位、独立式兜底，红线由独立式独占把关。

> **配套文档**：[`etl-prune-frontload.md`](etl-prune-frontload.md)
> ——本方案中**评分门控的真正生效**与**etl 结构裁剪的前移**在该姊妹篇详细展开。
> 简言之：独立式 `reject` 在 gdr 末尾阻断 C2 落盘，对比式 `fail` 在 etl 入口打 `compare_warn` 标记；
> `usage_prune`（系统裁剪 / tools 裁剪 / 本机路径泛化）从 etl 前移到 gdr step 22，
> 让 C2 天然已是训练就绪形态。

---

## 八、与参考方案的差异说明

本方案在参考方案方法论上做以下项目化调整：

| 参考方案 | 本方案调整 | 原因 |
|---|---|---|
| 对比式从轨迹级起步 | 块级 L1/L2/L3 保留 + 新增轨迹级 L4 | 项目已有块级对比式，废弃浪费；块级是轨迹级的输入 |
| 1-5 分制全新建 | quality_scorer [0,1] + 映射函数 → 1-5 | 项目已有 7 维组合评分，复用 + 映射比重建稳 |
| 独立式任务完成度单列 | 并入绝对质量分 result_quality 子分 | 项目已有 ValidationPipeline 任务验收，直接调用 |
| 红线全新建 | 红线新建但接入 aggregate_results | 复用 Verdict 四态聚合，一票否决现成 |
| 金标集独立 | 复用 `gdr/evaluator/` 目录 | 项目已有 dual_eval / feedback 评估框架，金标是其自然扩展 |
| 对比式 fail → 精修管道 | fail 回 step 9 refiner，携带 diff | 项目已有 refiner 重试 + 升级机制，不另建精修管道 |

---

## 九、风险与缓解

| 风险 | 缓解 |
|---|---|
| 轨迹级对比式 LLM 调用增加成本 | fidelity 三要素抽取复用 user_intent + claims，仅比对步用 LLM；diff 分类规则优先，模糊项才 LLM |
| 1-5 分映射失真 | 金标集校准映射函数；保留 [0,1] 原值在 metadata 供审计 |
| 红线黑名单不全 | 规则层 + LLM 复核可疑项；黑名单配置化，可热更新 |
| 漂移监控误报 | 锚点 50 条 + 漂移阈值 0.3 收紧；探针对 20 条/批交叉验证 |
| 第一层 fail 回路死循环 | 复用 `max_retries_9b` 上限，耗尽即淘汰，不无限重试 |

---

## 十、验收标准

- [ ] `gdr/domain/scoring_schema.py` 定义 `TrajectoryCompareResult` / `TrajectoryFreeResult`
- [ ] `gdr/validators/l4_trajectory_compare.py` 输出符合 schema，fidelity/instruction_adherence/coherence_delta 三维度齐全
- [ ] `gdr/validators/redline.py` 四类红线（侵权/隐私/注入/ToS）均有规则 + LLM 复核路径
- [ ] `gdr/validators/free_quality.py` 1-5 分 + 子分门槛（executability/action_obs_alignment ≥4）生效
- [ ] `gdr/pipeline/runner.py` step 5.5/6.5 插入，fail 回路不死循环，metadata 写入 scoring 字段
- [ ] `config/config.example.yaml` `gdr.scoring` section 完整
- [ ] 金标集 200-300 条，评审与人工一致率 ≥75%
- [ ] 漂移监控锚点 50 条，阈值 0.3 触发告警
- [ ] 单元测试覆盖：对比式三维度、红线四类、质量分映射、管道接入、fail 回路
