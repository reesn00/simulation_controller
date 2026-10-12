# URL播放黄金集与轨迹黄金集标签对齐方案

> 状态：设计稿（未实施）
> 日期：2026-10-12
> 关联：`url_video_playback_golden_dataset_plan.md`（新体系方案）、`evaluation/golden/builder.py` 与 `perception/questions.py` / `executor/branches.py`（现有轨迹黄金集）
> 目标：让「URL 视频可播放性校验 · 黄金数据集」的 9 个 `final_label` 与轨迹合成体系已有的「4 判断点 × 10 失败分支」在**同一套标签注册表**上收敛，避免两套 golden 并存互相污染，并给出迁移路径与代码改造点。

---

## 0. 背景与目标

新方案（`url_video_playback_golden_dataset_plan.md`，下称**新体系**）定义了一套以「这个 URL 能不能正常播放」为最终结论的标签体系：9 个 `final_label` + 6 个 `paywall_type` 子标签 + 元数据字段。而仓库里已跑通、已冻结 v1/v2 的轨迹合成黄金集（`evaluation/golden/`，下称**现有体系**）用的是「4 个判断点 × 10 条失败分支 + success」的标签体系。

两套体系**不是平行世界**：现有体系的 `SiteOutcome`（`executor/branches.py`，docstring「一个站点的最终归宿」）本身就是**站点级结论**，与新体系「一条 URL 一个结论」粒度一致。之所以标签对不上，是因为两边按不同维度切分：

- 现有体系按「**怎么失败的路径**」分类：失败发生在哪个判断点（不是播放站 / 不可达 / 无控件 / 播放器不可用）。
- 新体系按「**为什么不能播的原因**」分类：付费 / 登录 / 地区阻断 / 内容损坏 / 无视频。

同一个事实（比如「优酷《功夫》页卡在开通会员」）在现有体系是 `subscription_wall`（④ PLAYER_OK 的细分），在新体系是 `paywall`（付费墙）——语义同义、命名不同。另有一批标签（`partial_free` / `broken` / `login_required` / `blocked` 的拆分）现有体系没有，需要新增或拆分。

**对齐原则**（沿用现有体系既有纪律）：

1. **单一注册表收敛**：分支名只有一个出处（`perception/questions.py::all_branches()` + `executor/branches.py::BRANCH_LABELS`），新体系标签作为「等价视图」挂进去，不另起一套枚举。
2. **不虚构、不静默覆盖**：历史 v1/v2 冻结版本不动，不重标、不回填拆分；跨批次比较看批次边界（`RunLedger.summary` 既有口径）。
3. **人工是审核者不是执行者**：新体系 L5 仲裁 ↔ 现有复核队列（`review_queue.py`）同构对接，裁定枚举显式扩展，不引入自由文本裁定。
4. **口径变更留痕**：Q1–Q4 的每一条裁定写进增量档案（`docs/07-增量档案.md`），D10/D9 阈值不变。

---

## 1. 两套体系的定位差异

### 1.1 新体系（URL 可播放性校验）

- 输入：**明确给定的 URL 列表**（假设都指向视频/可播放候选）。
- 结论：站点级 `final_label`，9 类：
  `playable` / `ads_then_playable` / `paywall` / `login_required` / `partial_free` / `blocked` / `broken` / `empty` / `unknown`。
- 判断信号偏**媒体层**：`timeupdate` 推进、帧解码、`MediaError`、`currentTime` 停截、价格文案、试看时长。
- 三级流水线：L0–L3 机器初筛（静态探测 → 媒体解析 → 浏览器信号 → 规则）→ L4 VLM 复核 → L5–L7 仲裁/人工/回流。
- 边界 case（方案 §7）：登录墙 ≠ 付费墙、广告中间态、地区+付费叠加、混合内容。

### 1.2 现有体系（轨迹合成黄金集）

- 输入：搜索候选链接（可能**不是**播放站——hao123 导航站也进候选）。
- 结论：**决策单元级**判断点 × 失败分支，落成站点级 outcome：
  - 4 判断点（`perception/questions.py::REGISTRY`）：① `SELECT_PLAY_SITES` → `not_play_site`；② `IS_REACHABLE` → `login_wall_or_blocked`；③ `FIND_PLAY_CONTROL` → `no_play_control`；④ `PLAYER_OK` → `component_unverified`。
  - 代码直判分支 6 条（`CODE_ONLY_BRANCHES`）：`unreachable_hard` / `trailer_only` / `trailer_suspect` / `unresolved` / `player_unverified` / `subscription_wall`。
  - 成功 = `branch=None`（黄金标签 `success`；`builder.SUCCESS_LABEL`）。
  - 合计 10 条分支 + success = 11 类（v2 manifest 实测 11 类都在）。
- 判断信号偏**交互/页面层**：页面形态、控件存在性、`<video>`/iframe 计数；媒体信号（`media_probe`）只有登记无实现。
- 复核队列（`executor/review_queue.py`）：`REVIEW_BRANCHES = {unresolved, trailer_suspect, player_unverified}` 3 支进人工；`VERDICTS` 7 个裁定枚举。

### 1.3 关键洞察

- 两者都产**站点级结论** → 标签域可以收敛到同一注册表。
- 现有体系「怎么失败」的切分粒度更细（10 分支 vs 9 标签），中间很多是**一对多**关系：新体系一个 `final_label` 在现有体系里分散在多个分支（如 `empty` ↔ `no_play_control` + `component_unverified`）。
- 有一批新体系标签（`login_required` / `blocked` / `broken` / `partial_free` / `ads_then_playable`）现有体系没有对应或只有模糊归属——这是对齐要解决的主要矛盾。
- 现有体系已有、新体系没有的（`not_play_site` / `trailer_only` / `trailer_suspect`）**保留**：新体系输入假设是视频 URL，但真实批次里仍会混入非播放页（导航站/错误页），丢弃这层语义会让负样本池退化。

---

## 2. 语义映射表

### 2.1 新 → 旧（final_label → 现有分支）

| 新体系 final_label | 现有体系分支 | 对应程度 | 处理 |
|---|---|---|---|
| `playable` | `success`（branch=null） | **完全对应** | 等价 |
| `ads_then_playable` | `success` | 部分（缺广告标记） | 等价 + `ads_present=true` 元数据（Q3 已裁定 2026-10-12） |
| `paywall` | `subscription_wall` | **完全对应**（2026-10-10 口径已冻结） | 等价 + `paywall_type` / `price` / `paywall_evidence` 元数据 |
| `login_required` | `login_wall_or_blocked`（的子集） | 部分（被合并） | **拆分**：`login_required` 独立分支 |
| `blocked` | `login_wall_or_blocked`（验证码/地区/反爬部分） | 部分（被合并） | **拆分**：`blocked` 独立分支；robots 等环境阻断并入 |
| `partial_free` | 无（最近 `subscription_wall`：试看后弹付费） | 不可直接映射 | **新增分支** `partial_free` |
| `broken` | 无（404 → `login_wall_or_blocked` 误归；媒体损坏 → `component_unverified`/`player_unverified` 模糊） | 不可直接映射 | **新增分支** `broken`，同时修正 404 归错类 |
| `empty` | `no_play_control`（无控件）+ `component_unverified`（点开无组件） | 部分（分散两分支） | 等价于两分支的并集，不新增；D10 等价类合并 |
| `unknown` | `unresolved` / `player_unverified` / `trailer_suspect` | **高度一致**（都是「没判出来」→ 进人工） | 等价；保留现有 3 支细分（责任方不同） |

### 2.2 旧 → 新（现有分支 → final_label）

| 现有体系分支 | 新体系 final_label | 说明 |
|---|---|---|
| `success` | `playable`；广告后播放另记 `ads_present=true`（Q3 已裁定） | 等价 |
| `subscription_wall` | `paywall`（`partial_free` 若试看形态） | 等价；部分场景经 `paywall_type=preview/trial` 细分 |
| `login_wall_or_blocked` | `login_required` ∪ `blocked` | 旧合并态，拆不回精确分类 → **历史不回填**，仅等价类对齐 |
| `unreachable_hard` | `blocked`（网络/DNS 环境面）或 `broken`（超时见 §4.3） | 现有只判 DriverError；新体系把「导航失败」拆环境/内容 |
| `not_play_site` | 无直接对应（输入假设不同）；极端场景 ≈ `empty` | 保留现有分支（任务语义：该片不在此站） |
| `no_play_control` | `empty` | 等价类 |
| `component_unverified` | `empty` | 等价类 |
| `trailer_only` / `trailer_suspect` | 无（新体系无预告片专项标签） | 保留现有分支；对应新体系「试看/预告后付费」场景可归 `partial_free`/`paywall` 等价类 |
| `unresolved` / `player_unverified` | `unknown` | 等价类 |

### 2.3 不可直接映射项与处理决定

| 项 | 为什么不可直接映射 | 处理决定 |
|---|---|---|
| `partial_free`（试看/预览后付费） | 现有 `subscription_wall` 语义是「需订阅/付费」，未区分「部分免费」 | **新增独立分支**；与 `subscription_wall` 用 `paywall_type` 区分（`trial`/`preview` vs `paid_subscription`/`rental`/`purchase`） |
| `broken`（404 / 媒体不可解码 / duration=0） | 现有 404 被 `IS_REACHABLE` 判 False 后误落 `login_wall_or_blocked`（prompt 把 404 与登录墙并列）；媒体损坏落 `component_unverified` 语义也不准 | **新增独立分支**；404/解码失败/duration=0 归入 |
| `login_required` vs `blocked` | 现有合并为 `login_wall_or_blocked` 一支，因为对「轨迹是否成功」两者都不可达；但新体系 Q2/Q4 需要细分「内容 vs 环境」 | **拆分**；新增两分支，旧合并态保留为历史兼容 |
| `ads_then_playable` | 现有 `success` 不含广告信息 | 不新增分支；`success` + `ads_present` 元数据标记（Q3 已裁定 2026-10-12：广告后能播算通过） |
| `empty` | 现有一分为二（交互层无控件 / 媒体层无组件） | 不新增；D10 等价类把 `no_play_control`+`component_unverified` 认作 `empty` |
| `unknown` | 现有已拆 3 支（按责任方：代码能力 / 页面证据 / 词表） | 保留 3 支（比 `unknown` 更细，对复核队列分工有价值） |

---

## 3. 目标标签域（收敛后的注册表）

### 3.1 新增 4 条分支

| 新分支 | 归属判断点 | 定义 | 判定来源 |
|---|---|---|---|
| `login_required` | ② `IS_REACHABLE` | 登录后可免费观看；agent 不含登录态 → 不通过（Q2） | 规则词表「登录后观看」+ 无价格信息；`payload.access_control_type=login` |
| `blocked` | ② `IS_REACHABLE` | 地区限制 / 验证码 / 反爬 / robots 等环境阻断（Q4） | 验证码 iframe、地区提示文案、反爬挑战页；`payload.access_control_type=region\|captcha\|anti_bot` |
| `broken` | ②（404）/ ⑦ `PLAYER_OK`（媒体损坏） | 内容性损坏：404、媒体流不可解码、`duration=0`、黑屏静帧 | L0/L1 静态探测 + `media_probe` 的 `media_error` / `black_screen` / duration |
| `partial_free` | ④ `PLAYER_OK` | 有免费部分（试看/预告/会员专享抢先看），完整内容需付费 | 试看 N 分钟弹付费、会员专享标注；`payload.paywall_type=preview\|trial` |

归属用 `_BRANCH_QUESTION`（`perception/questions.py`）登记：每条分支唯一归属一个判断点，保持不变式（D-5 的 `question_of_branch` 继续可用）。

### 3.2 停用与兼容

- `login_wall_or_blocked` **停用**：不再由代码路径产生；仍在 `BRANCH_LABELS` 保留（历史存档展示用，标注 `deprecated`）。v1/v2 冻结数据**不动、不回填**。
- `all_branches()` 从 10 条变为 13 条（10 − 1 + 4）；`NON_SAMPLE_BRANCHES` 保持 3 支不变（新增 4 支都是**真负样本**，直接进负样本池）。
- `BRANCH_LABELS` 合计 14 条（含 deprecated）+ `success` = 标注域 15 个合法值；`builder._load_branch_labels()` 从 `BRANCH_LABELS` 派生，新增分支自动进入 annotate 校验，无需改 builder 常理逻辑。

### 3.3 判断点与分支的完整对照（更新后）

| 判断点 | negative_branch（不变） | 细分分支（经 payload） | 代码直判分支 |
|---|---|---|---|
| ① `SELECT_PLAY_SITES` | `not_play_site` | — | — |
| ② `IS_REACHABLE` | `login_wall_or_blocked`（→ 停用） | `login_required`（login）/ `blocked`（region/captcha/anti_bot）/ `broken`（not_found） | `unreachable_hard`（DriverError） |
| ③ `FIND_PLAY_CONTROL` | `no_play_control` | — | `trailer_only` / `trailer_suspect` |
| ④ `PLAYER_OK` | `component_unverified` | `subscription_wall`（paywall）+ `partial_free`（preview/trial） | `player_unverified`（到播放页判不了） |
| —（通用） | — | — | `unresolved`（fail-closed） |

`decision_branch()`（`executor/branches.py`）扩展规则：`payload.access_control_type` 优先于题绑定（②）；`payload.paywall_type` 中 `preview/trial` 落 `partial_free`，其余落 `subscription_wall`（④）；`payload` 均缺失时保持现状。

---

## 4. 仲裁规则

### 4.1 L5 路由 ↔ 现有复核队列

新体系 L5 的人工路由与现有 `review_queue.py` 机制**同构**，直接映射：

| 新体系 L5 路由 | 现有体系对应机制 | 说明 |
|---|---|---|
| 规则 = VLM 且置信度 ≥ 0.9 → 自动入 golden | `annotations_suggestions` 高置信路径（confidence=high，`黄金集人工复核工作单-254条.xlsx`） | 自动通过 = 不进 REVIEW_BRANCHES |
| 规则 = VLM 但置信度 0.6–0.9 → 抽样 2–5% 人工复核 | 现有工作单的 confidence 字段分流（低置信 needs_review，`-23条.xlsx`） | 抽样校准是同一件事：按置信度抽子集人工复核，统计推翻率 |
| 规则 ≠ VLM / 任一方 unknown / 边界类 → 人工终审 | `REVIEW_BRANCHES = {unresolved, trailer_suspect, player_unverified}` | 现有 3 支天然是「需人工」集合；`unknown` 等价类并入 |
| 人工队列内部排序：分歧 > unknown > 边界类 > 高置信 | `prefill()` / `settle_same_site()` 的优先级逻辑 | 可复用「结论档压过没证据档」的规则 |

### 4.2 VERDICTS 扩展（review_queue.py）

现有 7 个裁定枚举外，新增 4 个（**枚举显式扩展，不用自由文本**——保持「复核员写什么算什么」的禁令）：

```python
VERDICTS = {
    "confirm_success": "人工确认：这里确实能看（记为成功样本）",
    "confirm_negative": "人工确认：这里确实看不了（记为负样本）",
    "confirm_not_play_site": "人工确认：这压根不是该片的可观看站（记 not_play_site 负样本）",
    "confirm_paywall": "人工确认：有播放页但需订阅/付费（记 subscription_wall 负样本）",
    "confirm_login": "人工确认：登录后可免费看，agent 无登录态（记 login_required 负样本）",      # 新增
    "confirm_blocked": "人工确认：地区/验证码/反爬等环境阻断（记 blocked 负样本）",              # 新增
    "confirm_broken": "人工确认：页面/媒体损坏（404、解码失败、duration=0，记 broken 负样本）",  # 新增
    "confirm_partial_free": "人工确认：部分免费，完整内容需付费（记 partial_free 负样本）",      # 新增
    "trailer": "人工确认：只有预告片（记为 trailer_only 负样本）",
    "need_browser": "存档证据不足，需人工开浏览器再看（暂不裁定）",
    "skip": "跳过（不可复核，如已失效页面）",
}
```

- `_WANTED_BRANCH` 同步 +4：`confirm_login → "login_required"`、`confirm_blocked → "blocked"`、`confirm_broken → "broken"`、`confirm_partial_free → "partial_free"`。
- `confirm_negative` 的按 `reached` 分岔不变（`component_unverified` / `no_play_control`）。
- 跨判断点守卫（`_fits` + `question_of_branch`）照常生效：新分支都有唯一归属，`confirm_broken` 贴到 ② 或 ⑦ 之外会降级为 `site_verdict`——这是 2026-10-10 踩过的坑，新分支必须走同一校验。

### 4.3 D10 口径 C 升级为等价类一致

现有 `builder.cmd_agreement` 的口径 C 是**字符串相等**（`y_true == y_pred` 逐条 `t == p`）。新增分支后，新代码输出与历史 golden 标签必然不相等（如代码判 `blocked`、v2 标注是 `login_wall_or_blocked`），D10 会**假性暴跌**，不是模型变差而是口径变了。

**处理**：新建一处等价类映射（建议 `evaluation/golden/alignment.py`，与分支注册表一样「一个出处」，避免散落）：

```python
# 语义等价类（以现有体系为锚，新体系标签挂入）
EQUIV_CLASS = {
    # 能播
    "success":              "SUCCESS",
    "playable":             "SUCCESS",
    "ads_then_playable":    "SUCCESS",
    # 付费
    "subscription_wall":    "PAYWALL",
    "paywall":              "PAYWALL",
    "partial_free":         "PAYWALL",
    # 访问阻断
    "login_wall_or_blocked": "ACCESS_BLOCKED",   # 历史合并态
    "login_required":        "ACCESS_BLOCKED",
    "blocked":               "ACCESS_BLOCKED",
    # 无播放资源
    "no_play_control":      "NO_PLAY_RESOURCE",
    "component_unverified": "NO_PLAY_RESOURCE",
    "empty":                "NO_PLAY_RESOURCE",
    # 内容/网络损坏
    "unreachable_hard":     "REACH_FAIL",
    "broken":               "BROKEN_CONTENT",    # 与 REACH_FAIL 分离：内容 vs 环境
    # 不是播放站 / 预告
    "not_play_site":        "NOT_PLAY_SITE",
    "trailer_only":         "TRAILER",
    "trailer_suspect":      "TRAILER",
    # 未判明
    "unresolved":           "UNKNOWN",
    "player_unverified":    "UNKNOWN",
    "unknown":              "UNKNOWN",
}
```

- 口径 C 改为 `equiv_class(true) == equiv_class(pred)`；`classification_report` 基于等价类标签做（分支路由下钻 D 仍用原始标签，看细分 err）。
- **D10_THRESHOLD 0.90 不变**（阈值不动，口径说明写进 manifest 与增量档案）。
- 明确记录：`broken` 与 `unreachable_hard` **不是**同一等价类——一个是「内容损坏」（页面/媒体在但坏），一个是「环境导航失败」（连页面都没到）；新体系 L0 的 HTTP 404/403 初筛只会落 `broken`，不落 `unreachable_hard`（后者现有语义是 DriverError，刻意不用 HTTP 状态码当判据——对齐时保留这条现状，`broken` 走新的静态探测通道）。

### 4.4 抽样校准与 D9

- 新体系「每轮分层抽样 3–8% 人工复核，记录推翻率」就是现有黄金集的 **D9 test-retest + 抽样复核**：分层维度（域名/视频源类型/地区）可作为 `dimension_scores` 的软维度一并录入（`annotations.jsonl` 已支持 `dimension_scores` 1–5）。
- 推翻率（override rate）对应现有「人工与代码分歧率」：`apply_verdicts` 已保留 `branch_before_review` 与 `review_*` 字段，分歧率可测，无需新机制。

---

## 5. Schema 扩展

### 5.1 code 字段扩展（builder.py `_scan_archives` / `_scan_negative_pool` 的 `code` dict）

新增全可选字段（缺省 None/空，不破坏现有解析与契约测试）：

```python
code = {
    # 既有字段（不变）
    "branch": ..., "branch_label": ..., "question": ..., "evidence": ...,
    "source": ..., "fallback_used": ..., "reached_play_page": ...,
    # 新增（新体系信号层；media_probe 落地后填入）
    "media_signal": {          # L2 播放信号（mcp_probe 的 EXPECTED_PRIMITIVES media_probe）
        "play_started": bool | None,
        "timeupdate_progress": bool | None,
        "current_time_sec": float | None,
        "frames_decoded": bool | None,
        "black_screen": bool | None,
        "media_error": str | None,       # MediaError.code / null
    },
    "paywall_type": str | None,          # paid_subscription | rental | purchase | trial | preview | none | unknown
    "paywall_evidence": str | None,      # 命中的付费原文（如"开通VIP · 连续包月¥25"）
    "price": float | None,
    "currency": str | None,              # CNY / USD / ...
    "playable_before_paywall": bool | None,
    "preview_duration_sec": int | None,
    "ads_present": bool | None,          # Q3 已裁定（2026-10-12）：广告后能播 = success + ads_present=true
    "access_control_type": str | None,   # login | region | captcha | anti_bot | not_found（② 的 payload 细分）
}
```

新体系 Schema（方案 §5 的 `final_url / landing_status / player_type / media_urls / rule_label / vlm_label / final_label ...`）**不整体复制进 golden**——golden 只收「可复核、可训练」的子集：`media_signal` + 付费/广告元数据。规则与 VLM 的中间判据（`rule_label` / `vlm_label` / 截图）进复核队列的 `ReviewItem`（见 7.3），不进冻结样本。

### 5.2 annotate / freeze 不变式

- `annotate` 的 label 校验从 `BRANCH_LABELS + success` 派生 → 新增 4 分支 + deprecated 兼容**自动生效**，无需改 builder 的合法值常量。
- `freeze` 的分层切分键 = `golden.label`（不变）；新分支作为普通 label 参与分层。
- `versions.jsonl` / `manifest.json` 增加 `label_schema_version` 字段（记录「11 类 → 15 类」口径变更），跨版本对比 D10 前先核对 schema 版本。

---

## 6. 迁移路径

### 阶段 0 · 口径冻结（1–2 天）

把 Q1–Q4 的现有等价定义落成正式条目，写进 `docs/07-增量档案.md`：

| 问题 | 裁定 |
|---|---|
| Q1 付费算不算不可播放 | **算不可播放**（已裁定 2026-10-10：有播放页但需订阅/付费 → `subscription_wall`） |
| Q2 登录后免费算不算通过 | agent 任务不含登录态 → **不通过**（`login_required` 负类；与现有 `IS_REACHABLE` 口径一致） |
| Q3 前贴广告/试看算不算正常播放 | **已裁定（2026-10-12）**：广告后能播 = success + `ads_present=true`；试看后付费 = `partial_free` 负类 |
| Q4 地区/验证码如何归类 | **环境阻断**（`blocked`），与内容损坏 `broken` 分离 |

产出：本方案评审通过 + Q3 裁定记录。

### 阶段 1 · 标签域扩展（3–5 天）

- `perception/questions.py`：`CODE_ONLY_BRANCHES` +4；②/④ prompt 更新（细分来源词表）；`_BRANCH_QUESTION` 补 4 条归属；`PAY_WALL` 语义注释扩为「付费墙大类（subscription_wall + partial_free）」。
- `executor/branches.py`：`BRANCH_LABELS` +4、`login_wall_or_blocked` 标 deprecated；`NON_SAMPLE_BRANCHES` 不变；`decision_branch()` 加 payload 细分；`BRANCH_PRECONDITION` 补 `broken`/`partial_free` 的前置事实（`broken` 的 404 无需「到了播放页」前置，媒体损坏需）。
- 新增 `alignment.py` 等价类（§4.3）。
- 全部新分支补测试：`test_branches`（分支名合法、precondition）、`test_d5_scorer`（同源映射、`question_of_branch`）、builder annotate 校验。

### 阶段 2 · 历史数据兼容（1–2 天，与阶段 1 并行后半）

- v1/v2 冻结**不动**。
- `cmd_agreement` 升级为等价类口径后，历史 golden 与新代码输出的比较自动可用。
- `candidates.jsonl` 中旧 `login_wall_or_blocked` 候选**不回填**为 `login_required`/`blocked`（不虚构：旧存档的 evidence 大概率分不出是登录还是地区）。

### 阶段 3 · 复核队列与工作单对齐（2–3 天）

- `executor/review_queue.py`：`VERDICTS` +4、`_WANTED_BRANCH` +4。
- `ReviewItem` 增加字段：`media_signal`、`paywall_type`、截图路径列表（`screenshots: tuple[str, ...]`，配合新体系 L2 定时截图与 L4 VLM）。
- `.dumate/xlsx-golden_review_*/helpers/build_worklist.py`：`LABELS` 枚举同步 15 类（13 活跃 + success + deprecated 历史），`HEADERS` 按需加 `final_label` / `paywall_type` 列。

### 阶段 4 · 新流水线信号接入（与新体系 P1–P4 实施并行）

- `mcp_probe.py` 的 `media_probe` 从「登记预期」落地为实现（`count` + `evaluate` 组合：`currentTime` 推进、`MediaError`、帧差分）→ 填 `code.media_signal`。
- L0/L1：静态可达性 + 媒体解析 → 只产出 `broken`/`blocked` 初筛（**不复用** `reachability.py` 的口径——它刻意不用 HTTP 状态码，新通道独立）。
- L4 VLM 的截图链路：截图进 `ReviewItem.screenshots`，VLM 结构化输出（`label/paywall_type/evidence/price/confidence`）作为 `annotations_suggestions` 的新来源。

**验收条件**（每个阶段结束时）：

- 阶段 1 结束：`all_branches()`==13、`BRANCH_LABELS` 15 值、`test_branches` / `test_d5_scorer` 全绿。
- 阶段 2 结束：对 v2 跑 `agreement --version 2`，等价类口径下结果与旧口径相同或更好（回归说明）；新增 `label_schema_version` 字段。
- 阶段 3 结束：新增 4 裁定可回填、`question_of_branch` 守卫生效、工作单枚举同步。
- 阶段 4 结束：`media_signal` 在真实 P1 存档出现；L0 初筛的 `broken` 判定与人工复核推翻率 < 10%（首轮指标，与新体系 §6 一致）。

---

## 7. 代码改造点清单

| 文件 | 改动 | 对应阶段 |
|---|---|---|
| `perception/questions.py` | `CODE_ONLY_BRANCHES` +4；②/④ prompt 更新；`_BRANCH_QUESTION` +4；`PAY_WALL` 注释扩为付费墙大类 | 1 |
| `executor/branches.py` | `BRANCH_LABELS` +4 + deprecated；`decision_branch()` payload 细分（`access_control_type` / `paywall_type`）；`BRANCH_PRECONDITION` 补齐 | 1 |
| `evaluation/golden/alignment.py`（新） | 等价类映射（一个出处）；`cmd_agreement` 改为等价类比较 | 1–2 |
| `evaluation/golden/builder.py` | `code` dict 扩展（§5.1）；`manifest` 加 `label_schema_version` | 1–2 |
| `executor/review_queue.py` | `VERDICTS` +4；`_WANTED_BRANCH` +4；`ReviewItem` + `media_signal`/`paywall_type`/`screenshots` | 3 |
| `executor/browser/mcp_probe.py` | `media_probe` 实现（video 计数 + currentTime 推进 + media_error） | 4 |
| `docs/03-数据契约.md` | 负样本池/复核队列字段补新字段；加 golden 契约说明 | 1–3 |
| `.dumate/xlsx-golden_review_*/helpers/build_worklist.py` | `LABELS` 枚举同步 15 类；`HEADERS` 加新列 | 3 |

**不改**：`executor/steps/reachability.py`（保持 DriverError 判据，`broken` 走新静态通道）；`NON_SAMPLE_BRANCHES`（新增 4 支均为负样本）；v1/v2 冻结数据。

---

## 8. 风险与边界

1. **双轨污染**：若新体系另起一套 golden（`final_label` 独立存储、独立抽样），两套数据集互相不可比，D10 失去意义。本方案用「单一注册表 + 等价视图」从结构上消除。
2. **D10 假性下降**：字符串相等 → 等价类（§4.3）。若不做，新增 4 分支当天 D10 指标就「变差」，会误导「模型退化」的结论。
3. **404 历史误归**：现有 `login_wall_or_blocked` 里混了 404 页。历史不回填（避免虚构），新数据经 `broken` 修正；`BRANCH_LABELS` deprecated 注释要写明。
4. **Q3 广告口径（已裁定 2026-10-12）**：广告后能播 = success + `ads_present=true`（正样本）；试看后付费 = `partial_free`（负样本）。`ads_then_playable` 全记成功会扩大正样本池，需在采集时区分「纯前贴广告」与「试看后付费」：前者 `ads_present=true`，后者 `paywall_type=preview/trial` 落 `partial_free`。
5. **登录墙拆分的前置事实**：`login_required`/`blocked` 需要「确实到了目标页」的证据（复用 `BRANCH_PRECONDITION` 同页守卫），避免把导航栏文案当付费墙/登录墙——2026-10-10 同页守卫踩过坑，新分支必须同步实现。
6. **跨批次比较**：阶段 1 之后的分支分布与 v2 不可直接比（`RunLedger.summary` 已有警告），报表要带 `label_schema_version` 与批次边界。