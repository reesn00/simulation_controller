# Rubric v1.1 修订与代码实施方案

**上游**: [`02-rubric与代码对齐.md`](02-rubric与代码对齐.md)（对齐账，诊断）
**对象**: [`RUB-VID-PLAY-001` v1.0](在线视频场景rubric.md) → v1.1
**日期**: 2026-10-09

---

## 0. 本文档是什么

对齐报告回答「**哪里错位了**」，本文档回答「**改成什么、按什么顺序改**」。

它是可执行的：**§3 每一行都能落到 rubric 原文的某一句，
§4 每一项都点名了文件与函数，§7 给每项配一条能机械发现它没做的检查。**

它不是设计提案——四条决策已经定了，本文只做落地拆解。

---

## 1. 已裁的四条决策

| # | 决策 | 依据 |
|---|---|---|
| **A** | D-6 必填去掉「集数」，改「片名」 | 集数只活在 taskgen 骨架、不进 executor，W1 只跑 `single_title`。做成必填会产出一批空值，而空必填在 D-6 下判 3 档——**评分标准会系统性惩罚所有 W1 样本** |
| **B** | D-5「有组件但无法确认可用」**新增独立分支** | 与 `unresolved` 责任方不同：一个是网站证据不足，一个是代码能力不足。混同则复核队列无法分工 |
| **C** | D-8 管辖层 **LLM 裁判 → 代码断言**，锚定按「一次一判」重写 | 重试次数/后续动作/止损锚定全是代码可测；且 v2 全链零重试，现锚定永不触发，该维恒为 4–5 档 |
| **D** | **样本级 7 维 + 批次级 3 维**，数据质量交给 `check-archives` 门禁 | 批次级维度在样本级打分无意义；D-2 的数据质量兜底不该压在标注员身上 |

---

## 2. 对决策 D 的一处修正：D-2 应当留在样本级

§5.2 原先把 D-1/D-2/D-7/D-8 一并划给批次级。**D-2 不该跟着走**：

D-2「动作引用的实体在前文观察存档中真实存在」是**逐轨迹可变的**——
`target=None`（ref 溯不到 `interactive_elements`，I6 被违反）、
label 落在 `[:80]` 裁剪之外、观察降级导致 `interactive_elements` 为空，
这三种都只发生在**特定站点**上，不是流水线的常量。

| 维度 | 逐轨迹是否可变 | 归属 |
|---|---|---|
| D-1 工具调用 | ✗ 由代码生成，恒合规 | 批次级 |
| **D-2 观察-动作一致** | **✓ 按站点变** | **样本级** |
| D-7 检索覆盖 | ✗ 覆盖率是批次的比率 | 批次级 |
| D-8 终止止损 | ✗ 一次一判，控制流统一处理 | 批次级 |

修正后：

```
样本级（7）  D-2  D-3  D-4  D-5★  D-6  D-9  D-10★
批次级（3）  D-1  D-7  D-8
```

**一票否决项仍是 2 个（D-5★ / D-10★）**，与 §5.3 承诺的一致，不受影响。

> 这次修正顺带证伪了我原先的说法「D-2 是流水线的结构属性」——
> 它是**四条里唯一真的逐轨迹可变的**，也是唯一带 P0 缺口的那条。

---

## 3. rubric v1.1 改稿清单

逐条给出「改哪句 → 改成什么 → 为什么」。

### 3.1 工具动作空间（D-1）

| | |
|---|---|
| 现 | 正例：`search("剧名 第3集 在线播放")` → `open(url_A)` → `click(btn_play)`<br>反例：`open("剧名第3集")`（搜索词当 URL） |
| 改 | 正例：`goto(search_url)` → `goto(url_A)` → `click(target)`<br>反例：`click(target=None)`（判定说有控件却拿不到语义目标，I6 被违反） |
|  why | `search` / `open` **不在动作空间**（真实是 `goto`/`click`/`new_tab`）。原反例「搜索词当 URL」在 v2 里**永远不会发生**——检索走 `goto(search_url)`，没有把检索串当 URL 提交这条路径。留着一个永不触发的反例，标注员会拿它当活跃扣分项 |

### 3.2 评分单位拆分（D-1/D-7/D-8 → 批次级）

评分记录单拆成两张：

- **样本级评分单（7 维）** —— 逐条轨迹 1–5 分
- **批次级断言表（3 维）** —— 每批一份，出「计数 + 通过率」，**不出 1–5**

批次级三维的具体形态：

| 维度 | 断言内容 | 数据源 |
|---|---|---|
| D-1 | 全批动作的 `tool` ∈ 声明动作空间、`params` 合 schema | P1 `steps[].action` + `actions.TOOLS` |
| D-7 | 候选处置覆盖率 = 有 outcome 的候选 / `run_config.max_candidates` 限定内的候选 | P1 `candidates` + `outcomes` + `run_config` |
| D-8 | 异常处置合规率 = 合规轨迹 / 有异常分支的轨迹 | P1 `outcomes[].branch` + `steps[]` |

### 3.3 元素裁剪的处理规则（D-2）

新增边界规则，并**依赖变更 B**：

> **边界（v1.1 新增）**：观察的 `elements_total` / `links_total` 大于
> 存档中实际保留的条数时，目标查不到应判「**不可判定（存档裁剪）**」，
> **不判「引用了不存在的实体」**。判后者等于把采集器的取舍
> 记成轨迹的缺陷。

**这条规则在 v1.0 里写了但不可执行**——`[:80]` 裁剪不落总量，
「被裁掉」与「不存在」在数据上完全同形。这是 v1.1 里唯一一条
**不补数据就必然产生反向错判**的规则。

### 3.4 组件判据与已修复行为（D-4）

- **正例**保留 `class="player-container"`，但注明该判据**依赖变更 E**（player 信号采集）；未落地前正例只用 `<video>`。
- **反例**加标注：「存档仅含广告 iframe → 有组件，记录」——**已知并已修复**（iframe 不作判据），保留作边界示范，**不作活跃扣分项**。

### 3.5 决策树与分支名对齐（D-5）

```
非播放站/无关站        → 排除 ✅              not_play_site
是播放站，无播放入口    → 排除 ✅              no_play_control
播放页无 video/组件    → 排除 ✅              component_unverified
有组件但无法确认可用    → 标记"未验证" ✅      player_unverified   ← v1.1 新增
有组件且可确认          → 记录 ✅              branch=None
```

每个节点右侧标注**代码分支名**。v1.0 的决策树是纯语义描述，
标注员要去读源码才知道「未验证」落在哪个分支上。

### 3.6 必填字段（D-6）

| 字段 | v1.0 | v1.1 | 依赖 |
|---|---|---|---|
| 站点 URL | ✓ | ✓ | — |
| 播放页 URL | ✓ | ✓ | **变更 D**（`site_url` / `play_page_url`） |
| 目标内容标识（剧名+**集数**） | ✓ | 片名（**去集数**） | 决策 A |
| 验证证据 | ✓ | ✓ | — |

### 3.7 管辖层与锚定重写（D-8）

| | |
|---|---|
| 现 | 管辖层 **LLM 裁判**；锚定围绕重试次数（`重试≤1`、`死循环 ≥3 次`、`超时后连续 open 五次`） |
| 改 | 管辖层 **代码断言**；批次级断言 =「每条异常都被记成分支（未静默丢弃）+ 立即转向下一站 + `evidence` 锚定观察」 |

**为什么不留原锚定**：v2 控制流一次一判（`reachability.probe` 单次 `goto`、
`_visit_guarded` 单次超时兜底），「连续 open 同一 URL 五次」无路径可走。
原锚定在该维上恒为 4–5 档——花 LLM 成本买一个不会扣分的指标。

### 3.8 批次级比率归位（D-3 / D-10）

v1.0 的内部矛盾：这三维锚定是批次级比率（`≥90%`、`>20% 错`），
而评分记录单是样本级 1–5——单站点上「≥90%」的分母没有定义。

v1.1 的切法：**比率只出现在批次级**。

| 维度 | 样本级锚（1–5） | 批次级 |
|---|---|---|
| D-3 | 判对 / 判错 / 未判 | 与人工类别标注的一致率 |
| D-10 | 与 ground truth 一致 / 不一致 | 错率 |

D-7 整体在批次级，比率天然归属，无需拆分。

### 3.9 一票否决项（G-1）

D-1★ 随维度移入批次级，样本级一票否决 = **D-5★ / D-10★**（2 个）。

G-1 表述从「维度 1/5/10」改为「**样本级维度 5/10**」。
批次级不参与一票否决——它一票就是整批作废，粒度不对。

### 3.10 不改的部分

- **D-9** 全维不动（模块 4 未落地，与 rubric 无关）
- **G-1/G-2/G-3/G-4/G-5/G-6** 六条全局规则全部保留，本报告逐条核过
- **文档治理**（版本、生效日期、负责人、变更记录）保留，变更记录表追加 v1.1 行

---

## 4. 代码改动清单

按「加字段」与「改口径」分两组——**加字段向后兼容，改口径影响历史数据的重解释**。

### 4.1 阶段一：加字段（互相独立，可并行）

#### 变更 B — 观察元素/链接总量【P0】

| 文件 | 改法 |
|---|---|
| [`perception/base.py`](../../perception/base.py) | `Observation` 加 `elements_total: int = 0`、`links_total: int = 0` |
| [`executor/browser/obscura_driver.py`](../../executor/browser/obscura_driver.py) | 与 `video_tag_count` / `iframe_count` 同处填真实总数 |
| [`executor/orchestrator.py`](../../executor/orchestrator.py) | `_obs_json` 输出两个新键 |

**不进训练视图**：`assembler/observation_view.py` 的 `render` 是**键白名单**式
（不是透传），新键不会自动流到 P2/P3。这条已核对过。

#### 变更 C — `RunConfig` 落盘【P0】

| 文件 | 改法 |
|---|---|
| [`executor/orchestrator.py`](../../executor/orchestrator.py) | `RunRecord` 加 `run_config: dict[str, Any] = field(default_factory=dict)`；`to_json` 输出；`Orchestrator.run` 用 `dataclasses.asdict(self._cfg)` 填 |

顺带把 `per_site_timeout_s` 落盘——它**连 `--help` 都查不到**
（CLI 不设，走 `RunConfig` 默认 90.0），而它是 D-8 唯一的时限判据。

#### 变更 D — outcome 补两个 URL

| 文件 | 改法 |
|---|---|
| [`executor/branches.py`](../../executor/branches.py) | `SiteOutcome` 加 `site_url: str = ""`、`play_page_url: str = ""`；`RunLedger.record` 加对应关键字参数；`to_json` 输出 |
| [`executor/steps/visit.py`](../../executor/steps/visit.py) | 每次 `record` 传 `site_url=candidate.url`；`reached=True` 的分支传 `play_page_url=result.player_obs.url` |

现状是 `outcome.url` **一字段三语义**（导航失败=候选 URL、找控件失败=`landed_url`、
到播放页=`player_obs.url`），读档的人必须回查控制流才知道它指哪一层。

#### 变更 E — player 信号采集【P2，可延后】

`Observation` 加 `player_signals: tuple[str, ...]`，
`obscura_driver` 用 `browser_evaluate` 抓 player 容器 class。

**为什么优先级低**：它让 D-4 正例的判据有数据源，但 D-4 的**错误**档
（判有组件而存档无播放元素）在变更 B 之后已可判——缺的是「假阴性」那一侧。
可以等 `LLMPerceptor` 在真实端点上跑通后再做。

### 4.2 阶段二：改口径（分支体系）

#### 变更 A — 新增 `player_unverified` 分支

| 文件 | 改法 |
|---|---|
| [`perception/questions.py`](../../perception/questions.py) | `CODE_ONLY_BRANCHES` 加 `"player_unverified"`；`all_branches()` docstring 的「8 条」改「9 条」 |
| [`executor/branches.py`](../../executor/branches.py) | `BRANCH_LABELS` 加词条；**`NON_SAMPLE_BRANCHES` 加它**；模块 docstring 与 `record()` 的报错文案「注册表 8 条」改 9 |
| [`executor/steps/visit.py`](../../executor/steps/visit.py) | 环节⑦：`player.answer is None` **先于** `record_from` 分流 → 记 `player_unverified`；`answer is False` 仍走 `record_from` → `component_unverified` |
| [`executor/review_queue.py`](../../executor/review_queue.py) | `REVIEW_BRANCHES` 加 `"player_unverified"` |
| `tests/unit/test_branches.py`、`test_visit.py`、`test_review_queue.py` | 分支全集 8→9；`NON_SAMPLE_BRANCHES` 断言；环节⑦ 的 `None` 路由 |

**分流条件为什么是「环节⑦ + reached」**：环节①–④ 的 `None` 仍是 `unresolved`
（连播放页都没到，说的是「能不能到」）；只有**点到了播放控件并取到快照**、
仍判不出来的，才是「确认有组件、确认不了可用」。

**净影响评估（这条决定了变更 A 的风险等级）**：

| 产物 | 是否变化 | 原因 |
|---|---|---|
| `negative.jsonl` | **不变** | `unresolved` 与 `player_unverified` **都不是负样本**，都不并池 |
| 复核队列条数 | **不变** | `unresolved` 本就在 `REVIEW_BRANCHES` 里，加同一个分支不增不减 |
| 复核队列标签 | **变好** | 从「感知层未给出结论（fail-closed）」变成「有播放组件但确认不了可用」——复核员看到的是**更准**的描述 |
| `report` 分支分布 | 变 | `unresolved` 减少、`player_unverified` 增加，`missing_branches` 会列出后者 |
| 历史存档 | **不回填** | 见下 |

**为什么不回填旧存档**：项目纪律是「并池是写入时的动作，不是读取时的推导」。
旧存档里的 `unresolved` 保持原样，`player_unverified` 只对新批次生效。
`report` 因此在批次边界两侧不可比——**批次元数据要记下这条**，
否则半年后对比两个批次的人会以为 `unresolved` 变少了是质量退步。

---

## 5. 产物形状变化

| 产物 | 变化 | 影响面 |
|---|---|---|
| P1 | 加 `run_config`、`elements_total`、`links_total`、outcome 的 `site_url`/`play_page_url` | `docs/contracts/` 的 P1 契约文档**待写**，本轮无处可对；`report` 的站点分布口径不变 |
| `negative.jsonl` | **不变**（见 §4.2） | — |
| 复核队列 | 分支取值域 +1 | 标注指引要同步（不是代码问题） |
| P2 / P3 | **不变**（新键不进训练视图） | — |

**P1 契约文档为空是一个已知缺口**（CLAUDE.md 已记「P1/P2/P3 三份待写」）。
本轮变更把 P1 形状又推进一步，**建议在改代码的同一批里把 P1 契约写出来**——
否则 `assembler/schema.py` 的 `degraded_from` 口径与 `integrity.py` 的
`degraded_marker_map` 会各自漂移（两者由 `TestDegradedMarkersMatchSplit` 双向盯着，
但那张表只盯「缺件标记」，盯不住新增字段）。

---

## 6. 执行顺序

```text
阶段 0  rubric v1.1 改稿（纯文档，零风险，先把口径钉死）
          ↓
阶段 1  变更 B / C / D / E  —— 加字段，互相独立可并行
          ↓
阶段 2  变更 A —— 改分支口径（影响 report 与复核队列取值域）
          ↓
阶段 3  evaluation/scorers/code_scorer.py 的 D-2 断言（依赖变更 B）
```

**为什么 0 在前**：rubric 改稿是纯文档，没有回归风险，且它决定后面
每项代码改动「为什么要做」。反过来做，等于按旧口径写完再推翻。

**为什么 A 单独放最后**：前四项是**加字段**（向后兼容，老存档仍能读），
A 是**改分类**（影响 `by_branch` 分布与复核队列取值域）。混在一批里，
出问题时无法判断是哪一类改动引入的。

---

## 7. 每条改动的机械验证

**每条改动配一条「没做就一定会被发现」的检查**——沿用项目既有做法：
静态的不靠纪律，靠契约测试与 AST。

| 改动 | 检查 | 落在 |
|---|---|---|
| B | `Observation.elements_total` 非零且 ≥ 保留条数；裁剪发生时必有记录 | 新单测 `test_obscura_driver.py` + 契约 `test_archive.py` |
| B | D-2 打分器在 `total > 保留条数` 且查不到时输出「不可判定」而非「不存在」 | `evaluation/scorers/` 新单测（阶段 3） |
| C | 存档缺 `run_config` → `check-archives` 报 **DEGRADED** | `executor/integrity.py` 加码 + `degraded_marker_map()` 同步（`schema.py` 也要加对应标记，**两张表由 `TestDegradedMarkersMatchSplit` 盯着**） |
| D | `reached=True` 的 outcome 必须有 `play_page_url` | 新单测 `test_branches.py` |
| E | player 信号采集降级时 `degraded` 带痕迹 | 新单测 |
| A | 分支全集 == 9 且 `NON_SAMPLE_BRANCHES` 含 `player_unverified` | `test_branches.py`；`questions.all_branches()` 已是契约 |
| A | 环节⑦ 的 `None` 必落 `player_unverified`（不是 `unresolved`） | `test_visit.py` |
| 全局 | 存档形状变化后 `check-archives` 仍全绿 | `uv run python -m trajectory_pipeline.executor.cli check-archives` |

**门禁要跟一条**：新增 DEGRADED 码后必须跑全量
`uv run python -m pytest -q`（三棵树都跑，不是只跑 `trajectory_pipeline/tests`）。

---

## 8. 本方案不做的事

- **不动 rubric 的 G-1～G-6** —— 六条全局规则逐条核过，与代码无冲突
- **不动 D-9** —— 模块 4 未落地，与 rubric 无关
- **不建 `evaluation/golden/`** —— 它是独立的一块（地基已齐，见对齐报告 §2.9），
  本方案只解决「P1 形状够不够评分用」
- **不回填历史存档** —— 纪律问题，不是工作量问题（§4.2）
- **不引入投票/集成** —— 生产链上禁止，分歧一律暴露（CLAUDE.md 红线）

---

## 附：三处「这条如果不改就会静默出错」

写在这里是因为它们都是**做对了也看不出来**的那类：

1. **变更 B 不做 → D-2 判反**。模型幻觉出的按钮与被裁掉的真按钮在数据上同形，
   两种相反的错判产出同一个分数。评分器本身看起来完全正常。
2. **变更 C 不做 → D-7 分母漂移**。`--max-candidates 5` 与默认 20 跑出的
   5/5 与 5/20 在 P1 里一模一样，两个批次覆盖率差 4 倍而存档看不出差别。
3. **变更 A 不回填历史 → 两个批次不可比**。半年后看报表，
   `unresolved` 从 12 降到 0、`player_unverified` 从 0 升到 12，
   不记批次边界就会读成「fail-closed 修好了」，而真相只是换了分类。

---

## 9. 实施结果（2026-10-09）

阶段 0–3 已落地。`uv run python -m pytest -q` → **841 passed**（门禁现只跑
`trajectory_pipeline/tests`，存量两棵树已移到 `archive/v1/`，见下方「口径变更」）。

| 改动 | 落点 | 机械验证（测试名） |
|---|---|---|
| B `elements_total` / `links_total` | `perception/base.py`、`obscura_driver.py`、`orchestrator._obs_json` | `TestTotalsAccounting`（3 条）、`TestRunRecordAudit::test_元素落盘被裁时总量字段仍在` / `…按落盘条数兜底`、`TestCandidateSourceAudit::test_元素总量随观察落盘` |
| C `run_config` 落盘 | `RunRecord.run_config`、`to_json`、`Orchestrator.run` | `TestRunConfigSnapshot`（5 条）、`TestOrchestrator::test_运行参数由_run_自己填`、`TestRunRecordAudit::test_运行参数进存档` / `…为空对象`、`TestCandidateSourceAudit::test_运行参数随存档落盘` / `…为空对象而非缺键` |
| D 三层 URL | `SiteOutcome.site_url` / `play_page_url` + 全部 `record` 调用点 | `TestOutcomeUrls`（3 条）、`TestVisitFlow::test_每条outcome都带候选地址` / `…未到播放页时不编播放页地址`、`TestPlayerUnverifiedAtStep7::test_三支都带候选地址与播放页地址` |
| A `player_unverified` | `questions.py`、`branches.py`、`visit.py` 环节⑦、`review_queue.py` | `TestPlayerUnverified`（6 条）、`TestPlayerUnverifiedAtStep7`（6 条）、`test_前置环节的_none_记_unresolved`（原名「一律记」已改——环节⑦ 不再落 unresolved） |
| 阶段 3 D-2 打分器 | 新增 `evaluation/scorers/code_scorer.py` | `TestJudgeRef`(8) + `TestUndetermined`(9) + `TestScoring`(8) + `TestNoScoreWhenUndetermined`(5) + `TestGotoIsOutScope`(3) + `TestOnRealArchives`(3) |

### 变更 E（player 信号采集）**未做**

计划里定的是 P2 可延后，本轮确实没做。`TestTotalsAccounting` 与
`code_scorer` 都不依赖它——D-4 的**错误**档（判有组件而存档无播放元素）
在变更 B 之后已可判，缺的只是「假阴性」那一侧。等 `LLMPerceptor`
在真实端点上跑通一批再做。

### 三处「做对了也看不出来」的实际状态

| | 状态 |
|---|---|
| 变更 B 不做 → D-2 判反 | **已封**。去掉裁剪判定会红 6 条测试（`TestUndetermined` 两条 + `TestNoScoreWhenUndetermined` 四条） |
| 变更 C 不做 → D-7 分母漂移 | **已封**。去掉 `run()` 里的 `asdict` 会红 `TestOrchestrator::test_运行参数由_run_自己填`；删掉 `_obs_json` 的两个键会红 3 条 |
| 变更 A 不回填历史 → 两批次不可比 | **未封，也不该由测试封**。这是批次元数据问题。`RunLedger.summary()` 的 docstring 已写警告，**批次元数据要人工记** |

### 口径变更（本轮顺带发现，与方案无关但影响 CLAUDE.md）

存量已整体移到 `archive/v1/`，`pyproject.toml` 的 `testpaths` 随之改成
`["trajectory_pipeline/tests"]`——**门禁现在只跑新树**。CLAUDE.md 原写
「三棵树都跑」已过期。存量两棵树的测试仍然可跑（`uv run pytest archive/v1/tests`），
但不再受门禁约束，这是重排带来的、与本次 rubric 工作无关的既成事实。

### 实施中发现的两处方案没写到的问题

1. **`record()` 的「注册表 9 条」报错文案**只是文案，但计划里把它与
   `all_branches()` 的 docstring 放在同一行，实际分布在三个文件
   （`branches.py` 模块 docstring、`record()` 报错串、`questions.all_branches()`）。
   漏改任何一处都不会被现有测试抓到——它们没有断言这句话的措辞。
   **教训**：分支数量这类计数一旦写进文案，就该有一条测试断言它。
   本轮只做到了把 `test_branches.py` 里的硬编码 8 改成相对
   `len(questions.all_branches())`，文案本身仍无门禁。
2. **`test_obscura_driver.py` 的 `ELEMENTS` fixture 是坏的**：写成
   `e1\ta\t"电影"`，而实测列式文本是 `ref=e1<多空格>a<多空格>"电影"`，
   于是解析结果是空元组——**空元组与「采集失败」在数据上同形**。
   既有测试没断言元素条数，所以它一直是绿的。修好后
   `TestTotalsAccounting` 才立得住。这条印证 CLAUDE.md 那句
   「fixture 是我们自己造的，规则和 fixture 一起错，测试照样绿」。
