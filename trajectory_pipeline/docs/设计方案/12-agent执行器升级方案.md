# 12 · Agent 执行器升级方案（最小自研循环）

**外部参考**：agentruntime-plan（用户 2026-10-16 提供的五层架构：AgentScope Runtime / LangGraph / 外部 Gatekeeper / Obscura MCP / Langfuse；原稿已随 2026-10-25 废弃文本清理删除）

**对照对象**：`trajectory_pipeline/` v2 新树（截至 2026-10-16 快照，门禁 2411 passed；路径 B/C 发现链已落地）

**性质**：**方案文档，分阶段实施**。把「纯代码控制固定逻辑」升级为「agent 管强语义任务 + 代码管流程引导与异常控制」，产物契约（P1/P2/P3、22 分支封闭集）**一字不动**。

**一句话**：agent 是驾驶员，代码是护栏，**代码参与任务流程引导、失败处理**；归因、记账、收口仍在代码。

> ⚠️ **立场表述沿革（2026-10-20）**：上面的「agent 是驾驶员，代码是护栏」与
> §0 D-A5 拍板**保留原文作历史沿革**，不改写——它记的是 2026-10-16 的判断。
> 现役口径已被[设计方案 15](15-执行器与agent-runtime重新定义.md) D-A6 取代：
> **代码层做护栏、LLM 层做探索、persona 管输入分布**——探索权整体交给
> agent runtime，感知层退位成三块静态资产，只留终局验收。

---

## 0. 拍板记录（2026-10-16）

| # | 决策点 | 拍板 | 理由 / 影响 |
|---|---|---|---|
| D-A1 | Agent 框架选型 | **最小自研循环**（`executor/agent_loop.py`，约 300 行），**暂缓 LangGraph** | 外层图骨架（22 分支 + 预算 + 记账）已存在且是实战资产，LangGraph 与之重复；断点重试不靠状态快照，靠 P1 原子落盘 |
| D-A2 | L1 静默自愈 | **否决，整层删除** | 合成轨迹数据工程中静默处理不合适：「被拦→修复→重试」是**训练信号本身**，静默抹掉等于造假轨迹；也违背 `RunRecord.search_blocked` 的设计哲学（「被拦」与「没素材」在报表上永远分得开）。处置从四档减为三档（见 §4） |
| D-A3 | Langfuse | **暂不引入** | 仅保留最小事件账本：`RunRecord` 加 `llm_calls[]` + `gatekeeper_events[]` 两个数组，将来要接零重构 |
| D-A4 | 确定性 | 接受有限丧失，四条缓解（见 §5） | agent 语义决策天然非确定；结构决策留代码保持确定 |
| D-A5 | 立场表述 | 「agent 是驾驶员，代码是护栏，代码参与任务流程引导、失败处理；归因、记账、收口仍在代码」 | 替代架构总览第 5 行「LLM 是传感器不是驾驶员」——**agent 模式落地时（A2 起）同步修订**；fail-closed、负样本纯度、三支「没判出来」分立的评估纪律原样保留 |

---

## 1. 参考方案五层 × 项目现状对照

| 参考方案层 | 项目现状 | 判定 |
|---|---|---|
| 浏览器执行层（Obscura MCP / 预留 browser-use） | ✅ **已满足**：[`page_driver.py`](../../executor/browser/page_driver.py) 协议（原子原语，无业务语义）+ [`mcp_client.py`](../../executor/browser/mcp_client.py)（JSON-RPC stdio）+ [`obscura_driver.py`](../../executor/browser/obscura_driver.py)（`--stealth` + `FORBIDDEN_TOOLS` 红线闸门） | 不动 |
| 外部判断与恢复控制层（Gatekeeper） | 🟡 **半成品**：规则散落三处——[`dom.py`](../../executor/dom.py) `detect_block`（CF/反爬）、[`rule_perceptor.py`](../../perception/rule_perceptor.py)（付费墙强弱词表 / ACCESS_HINTS）、[`branches.py`](../../executor/branches.py) `_same_page`（前置事实守卫）。**缺**：统一拦截点、行为死循环检测、分层恢复策略 | **优先级最高，阶段 G 先做** |
| 认知与状态流转层（LangGraph） | ❌ [`orchestrator.py`](../../executor/orchestrator.py) 纯代码固定序列（probe→感知→click→复观），LLM 只答十道单步判断题、从不链式决策 | **本次升级主体**（按 D-A1 用最小自研循环，不用 LangGraph） |
| 宿主与沙箱层（AgentScope Runtime） | 项目是批处理轨迹合成管线，非在线服务；会话管理 / 沙箱隔离需求不成立 | **不引入**（并发规模化后再议） |
| 可观测层（Langfuse） | [`llm/client.py`](../../llm/client.py) 仅记 `last_error` + `latency`，无 token / 成本 | **后置**（按 D-A3 暂不引入，只做最小账本） |

**核心适配判断**：参考方案为「生产级在线 Agent 服务」设计；本项目是「可归因轨迹合成管线」。照搬五层会破坏项目最值钱的资产——**22 条失败分支封闭集、P1/P2/P3 产物契约、fail-closed 纪律**。适配原则：**agent 接管「传感器 + 导航」，代码保留「流程引导 + 记账 + 收口」**。

---

## 2. 职责再划分

### 2.1 交给 agent 的：当前用代码硬编码的强语义判断（8 处）

| # | 现状硬编码 | 代码位置 | agent 化后 |
|---|---|---|---|
| 1 | 搜索结果链接剔除词表 `EXCLUDED_HOST_ROOTS`（zhihu/douban/weibo 当「结构性排除」） | [`search.py:60`](../../executor/steps/search.py) | 词表拆两半：**结构噪声**（引擎自身 / gov.cn / 统计域）留代码；**语义信源**（知乎推荐贴、豆瓣、微博）转为 agent 分类维度 `article_post`——知乎推荐贴进池正是路径 C 的扩展 |
| 2 | 判断点① `SELECT_PLAY_SITES` 单题过滤 | [`questions.py`](../../perception/questions.py) REGISTRY | 升级为 **agent 任务①（链接分类）**：输出 `{video_site / aggregator / article_post / noise}` 分类清单 + 理由，分类决定分发路径 |
| 3 | `pin_candidates_first` 20 条感知窗口 hack | [`search.py:496`](../../executor/steps/search.py) | agent 分批看全量链接，机制性补丁整体消解 |
| 4 | 聚合词固定三条轮换 `AGGREGATOR_QUERIES`（crc32 选词） | [`search.py:160-174`](../../executor/steps/search.py) | **保留代码发起**（确定性要求），agent 在失败时可生成补充检索词 |
| 5 | 站内找控件固定序列（FIND_PLAY_CONTROL 回 ref → 代码 click） | [`visit.py`](../../executor/steps/visit.py) | **agent 任务②（站内自主导航）**：进站 → 找片 / 聚合栏 → 进详情页 → 找控件 → 点击，多步自主 |
| 6 | B/C 六道感知题（IS_AGGREGATOR / SELECT_SITE_ENTRIES / SELECT_REFERRAL_LINKS…） | [`questions.py`](../../perception/questions.py) | 内化进 agent 任务②③④的提示词素材；其 `negative_branch` 知识保留为**分支记账映射表** |
| 7 | 确认页 / 搜索按钮词表（`CONFIRM_PAGE_HINTS` / `SEARCH_BUTTON_HINTS`） | [`link_check.py`](../../executor/steps/link_check.py) / [`site_search.py`](../../executor/steps/site_search.py) | agent 语义识别，词表退为 gatekeeper 仲裁兜底 |
| 8 | L5 仲裁权重表 + 付费墙强弱词表三判据 | [`arbitration.py`](../../executor/arbitration.py) / [`rule_perceptor.py`](../../perception/rule_perceptor.py) | **保留代码**（「确定性事实优先」口径），但词表救济链（INC43-45 那串）不再需要扩展——agent 的 `page_quotes` 天然是语义证据 |

### 2.2 留在代码的：流程引导与异常收口（不变）

- **外层图骨架**：初始搜索（query 模板 + `RESULT_SELECTORS` 抽取）、候选分发调度、预算（`max_candidates / max_intermediates / max_referral_links` + 新增 **agent 步数预算**）、`_visit_guarded` 超时兜底
- **Gatekeeper（收拢后）**：CF / 验证码 / 登录墙 / 付费墙判定、**行为死循环检测（新增**：同域名连续重复动作滑动窗口、连续空正文）、观察降级判定
- **分支记账**：agent 的放弃 / 失败必须收敛回 **22 分支封闭集**（agent 说不清的进 `unresolved`，不入负样本池——三支「没判出来」纪律沿用）
- **fail-closed 重述**：LLM 不可用 / 预算耗尽 / agent 死循环 → 代码收口 `unresolved` 或 fallback 回 scripted 路径；**agent 自报「我不确定」≠ fail-closed**
- **I6 不变式保留**：agent 工具层强制校验 click 的 ref 必须来自最近一次 observe——模型会编 ref，点错不可逆
- **隐私红线**：`FORBIDDEN_TOOLS` 照旧；agent 工具声明 = assembler 冻结的 `ToolSpec.visible_to_model()` 集（`new_tab` 留动作流不进声明）

---

## 3. 端到端新流程（视频在线检索场景）

用户场景原文：初始浏览器搜索「在线电影、电视剧、纪录片***」仍保留代码固定控制；返回的搜索页内容 + 提示词交给 agent 分析哪些是视频网站、聚合网站、知乎推荐贴等链接项；获取到链接后依次交给 agent 任务，让 agent 自主在视频站、聚合网站、知乎文章查找指定视频、视频聚合栏再进某视频网站找视频、文章内找视频站进入再找视频；过程中的失败、拦截、登录、付费、异常在 agent 外部加判断代码与代码控制模块引导。

```text
代码（不变）                agent 任务                    代码 Gatekeeper（每步拦截）
─────────────              ─────────                    ─────────────────────────
构造查询+goto 引擎
抽标题↔URL 对 ──────►  任务① 链接分类
                          「这页哪些是视频站/      ◄── detect_block：CF/反爬在
                           聚合站/知乎推荐贴？」         进 agent 之前短路
按分类分发：              输出 typed 分类+理由
 video_site ────────►  任务② 站内找片
 aggregator ────────►  任务③ 聚合站探索          ◄── 登录/付费墙 → 阻断数据灌入
 article_post ──────►  任务④ 文章找引荐链接            + 注入标准引导指令
                          （多步自主：goto/observe/      「该站需付费已被拦截，
                           click/type，步数预算≤N」       请换站」→ agent 下一轮
                              │
                              ▼                        ◄── 死循环 → 注入重规划指令
                     每步产出 StepLog                       连续失败 → 模型升配
                     （action/observation/思考）
                              │
代码收口：分支记账（22 分支不变）→ P1 → P2 → P3（契约不动）
```

两个关键收益：

1. **agent 循环原生产出 P3**：真实 agent 的 `tool_calls + 观察 + 思考文本` 恰好就是 P3 `openai.json` 视图的格式——从「事后合成 rationale」变为「原生 agentic 轨迹」，rationale writer 对 agent 模式样本退位为闸门三查的核对器。agent 工具声明与训练动作空间同源（`visible_to_model()`），轨迹教的就是这套动作。
2. **消解当前主瓶颈**：D-10 的 unresolved 簇 46% 主因是「body 采集反爬降级 + JS-heavy 真渲染失败」——固定四步序列在复杂 SPA 上必失败，多步自主导航 + 死循环检测 + 模型升配正是对这三类的针对性修复。

---

## 4. Gatekeeper 设计（阶段 G 产物）

### 4.1 统一拦截点

新建 `executor/gatekeeper.py`：`intercept(observation) -> Verdict`，Verdict 取值 `normal | blocked_cf | captcha | login_wall | paywall | dead_loop | degraded`。

迁移三处既有规则（`detect_block` / 付费墙词表 / `_same_page`），**新增**滑动窗口死循环检测（同域名连续多步动作重复、连续提取有效正文为空）。

### 4.2 三档处置（L1 静默自愈已按 D-A2 删除）

| 处置 | 动作 | 轨迹可见性 |
|---|---|---|
| `InjectGuidance` | 阻断页面数据灌入 agent + 向任务状态注入标准引导指令（如「目标站点存在付费墙已被拦截，请换站」）+ agent 重规划 | **完全可见**：拦截事件 + 引导文案都落 `RunRecord` |
| `EscalateModel` | 切换高阶后端（`TRAJECTORY_LLM_ESCALATE_*`）接管后续步骤 | 可见：记升配触发点 |
| `FailClosed` | 收口 `unresolved` / fallback scripted，分支记账 | 可见 |

> ⚠️ **为什么删 L1**：参考方案的「换 IP / 清指纹静默重试」对在线服务是对的（用户体验优先），对轨迹合成是错的——被拦截时的**正确行为本身就是要教的训练信号**，静默修复会让轨迹呈现「从未发生拦截」的假象。附带好处：obscura 不暴露代理原语、`FORBIDDEN_TOOLS` 禁 cookie 工具（隐私红线，存档推 Label Studio 不能破）都不再构成落地障碍。

---

## 5. 确定性问题（D-A4）

### 5.1 现状机制

[`search.py:167-174`](../../executor/steps/search.py) 的 `aggregator_query()` 用 `zlib.crc32(seed) % len(AGGREGATOR_QUERIES)` 按 task_id 稳定选词（刻意不用每进程随机的内置 `hash`）。这不是孤例，而是一条贯穿性纪律：**所有代码侧决策都是输入的纯函数**——候选保持引擎原序不重排、判断点 None 不中断遍历、分发顺序确定。代码注释原话：「『这个 task 用了哪条词』必须可以从 task_id 重算出来——存档可归因的前提是确定性」。

它支撑三件事：

1. **重跑可比**：同 task 跑两遍，差异只能来自页面时变，不能来自代码侧——INC43 的 75 对 URL 直采 A/B 对比能成立，前提就是这个。
2. **失败归因**：看到存档里一个异常结论，能反推「代码当时走了哪条路」；批次间系统性差异可以干净地归因到代码版本（git diff），而不是随机性。
3. **黄金集对齐**：golden alignment 的 EQUIV_CLASS 推导隐含「结论可从结构重算」的假设。

### 5.2 agent 化后丢的是哪一段

crc32 选词本身留在代码（确定性保留），真正非确定的是 **agent 的语义决策**——同一搜索页，agent 这轮选 A 站、下轮选 B 站，后续整条轨迹分叉。两个放大器：

- LLM 单步就有非确定性（INC42 实证：同一输入两轮 `anchored↔mild_hallucination` 翻转）；
- 多步级联——单步 5% 的分歧在 10 步链条上就是完全不同的路径，结论都能不同（一次选中 A 站成功、一次选中 B 站付费墙）。

于是：重跑差异 = 页面 + LLM 双重来源，A/B 锚点失效；失败只能靠存档复现、不能靠重放；批次对比噪声地板抬高。

### 5.3 四条缓解（成本递增）

1. **决策分层**：结构决策（选词、候选分发顺序、预算、终止判定）留代码保持确定；只有页面内语义决策交给 agent。
2. `temperature=0`：压不住推理链随机性，但能压输出形态漂移（`llm/client.py` 补参数）。
3. **归因锚点迁移**：从「可从 task_id 重算」改为「存档全保真」——P1 原子落盘本来就是项目强项，agent 模式下存档就是唯一真相源。A/B 对比口径从「单次重跑」改为「同 task N 次重跑取分布」。
4. `RunRecord.executor_mode` 分批记账，比对时永远知道这批是 agent 批。

> 实质：scripted 模式的确定性本来就只覆盖「代码决策段」（页面时变一直存在），agent 化只是把非确定性从页面侧扩展到决策侧。损失的是**代码决策段的可重算性**，补救靠项目本来就最强的落盘纪律。mutation_check / 重放可比性在 agent 批次上的口径要重新定义（A3 收口项）。

---

## 6. 分阶段落地路线

### 阶段 G：Gatekeeper 收拢（纯代码重构，零行为变化，先行）✅ 已落地（INC47，2026-10-16）

- 新建 `executor/gatekeeper.py`：统一 `intercept()` + 三档处置对象化（§4）
- 迁入三处既有规则，新增死循环检测
- gatekeeper 事件进 `RunRecord` 新字段（`schema_version` 升级走契约提案）
- **验收**：2411 门禁全绿（仅存量 xlsx 环境缺失 1 失败恒定剔除）——落地实录见[增量编年史 INC47](../计划进展/90-增量编年史.md)

### 阶段 A1：搜索页链接分类 agent 化 ✅ 已落地（INC48，2026-10-16）

- `EXCLUDED_HOST_ROOTS` 拆分：结构噪声留代码，zhihu/douban/weibo 移出、转 agent 分类维度
- 判断点① 升级为 agent 分类任务，复用现 `SELECT_PLAY_SITES` 的 schema_hint 做提示词底稿
- 同步项：`d5_scorer` 等价类、golden alignment 的 `ROUTED` 类口径复核（知乎直排入口是新增量）
- **验收**：A/B 对比 scripted 基线的①判断准确率；`pin_candidates_first` 删除后无回归

> **落地注（INC48，实录见[增量编年史](../计划进展/90-增量编年史.md)）**——与原计划的三处差异：
> ①新题 `CLASSIFY_SEARCH_LINKS` **一道统一三条发现路径**（direct/B/C 各取目标类分发），不是「复用 ① 的 schema_hint」——四分类一次给出每条链接的类型判定，比三道二元筛选题信息密度高，且分发语义天然覆盖 B/C；②`SELECT_PLAY_SITES` **保留未删**（`site_search.py` 站内搜索结果页复用 + 金标重放）；③`not_play_site` 随之成为**多产地分支**（① 老档 / ①.5 新档），倒逼 `questions_of_branch` 复数归属查询 + `review_queue._fits` 任一产地命中（`not_aggregator` 同批受益）。单元门禁 2450 passed, 10 skipped（1 失败 = 存量 xlsx 恒定剔除）；「A/B 对比①判断准确率」需真实 LLM 批次，与 golden alignment 口径复核一并留待 A3 灰度验收（ROUTED 等价类已由路径 B/C 批次落地，单元门禁含同源测试）。

### 阶段 A2：站内自主导航 agent 化 ✅ 已落地（A2a：INC49–INC54；A2b：INC53，2026-10-17）

> **细化设计已拍板（2026-10-16 owner）并成文**：[13-A2A3-agent循环与双模灰度实施设计.md](13-A2A3-agent循环与双模灰度实施设计.md)——四项拍板：①JSON 动作指令（不用原生 tool_calls）；②A2a（基建+任务②）/A2b（任务③④）切分；③P1 动作枚举存量脱节前置收口（契约三值 vs 代码六值实锤）；④visit_site scripted 序列保留不删。本节为目标概述，实施以 13 号文档为准。

- 新建 `executor/agent_loop.py`：`agent_loop(task, tools, budget, gatekeeper) -> list[StepLog]`，工具 = PageDriver 原语子集 + ref 白名单校验
- `visit_site` 固定序列替换为 agent 任务②③④；`llm/client.py` 补 `temperature` 参数与 token / 成本记录（`llm_calls[]`）
- 模型升配：`TRAJECTORY_LLM_ESCALATE_*` 第二后端配置，gatekeeper 触发
- **验收**：agent 步数预算内完成率；unresolved 簇占比对比；D-2/D-5/D-6 在 agent 模式样本上的得分不降

> **落地注（INC49–INC54，实录见[增量编年史](../计划进展/90-增量编年史.md)）**——
> A2a 基建与任务②（INC51/52）、`chat_messages` 多轮出口（INC50）、
> `llm_calls[]` 账本 + ESCALATE 三键（INC54）、A2b 任务③④（INC53）。
> 验收三条**全部留待 A3**（需真实 LLM 批次）：步数预算内完成率、
> unresolved 簇对比、D-2/D-5/D-6 agent 样本得分。结构性口径三处已钉：
> ①「agent 永不直接产分支」——自报放弃一律 `unresolved`，负样本只收
> 代码判出来的拦截事实；②`executor_mode` 批次级记账（`RunRecord` 字段，
> 默认 scripted 刻意非 auto，要 agent 没后端构造期报错）；③记账范围
> 只圈 agent_loop（感知层十一题不汇入，O 阶段再扩）。

### 阶段 A3：双模灰度与评估收口

> **细化设计见 13 号文档 §6**（2026-10-16 判据锚点调研结论：样本级 D-2/D-5/D-6 不锚 scripted 序列形状、天然兼容；真正要新做的是批次级 goto URL 溯源断言——agent 的 goto 是 LLM 决定的，「比对两个代码产物」变成「核查可溯源性」）。

- `RunConfig.executor_mode: scripted | agent`（沿用「灰度只在构造期」纪律，`RunRecord` 分开记账）
- agent 失败 fallback scripted / `unresolved`；同批 A/B 跑黄金集对齐
- D-2/D-5 判据在 agent 批次上的适用性逐条核对（判据锚在 scripted 动作流上，如 goto URL 溯源、决策树分支映射——**这是 A2/A3 最重的活**）
- 架构总览第 5 行立场表述修订（D-A5）

### 阶段 O：可观测（暂缓，按 D-A3）

- 最小版先行：`RunRecord` 加 `llm_calls[]`（model/token/cost/latency）+ `gatekeeper_events[]` 两个数组——参考方案的 Langfuse trace 树就是这两份账本的**投影**，照 [`ls_bridge/exporter.py`](../../evaluation/ls_bridge/exporter.py) 的既有模式写导出器即可，将来接 Langfuse 零重构

---

## 7. 风险清单

| 风险 | 影响 | 缓解 |
|---|---|---|
| 评估判据锚在 scripted 动作流（D-2 goto URL 溯源、D-5 决策树映射） | agent 批次可能整批不可判定 | A3 逐条核对判据适用性，先跑小批 |
| agent 步数失控 / token 成本失控 | 批次预算爆炸 | 步数预算硬上限 + `llm_calls[]` 记账 + `_BudgetExhausted` 收口 `unresolved` |
| agent 编 ref / 点错不可逆 | 污染存档、误伤真实站点 | I6 白名单校验强制（工具层，非提示词层） |
| 站场表述漂移（「LLM 是传感器」已被 A2 事实推翻但文档未改） | 新读者按旧立场改代码 | A2 落地即修架构总览，本文 D-A5 钉住新表述 |
| 词表 / 词表救济链（INC43-45）与 agent 语义判定双轨并存期口径打架 | 同一页面两套结论 | 仲裁规则不变：确定性事实优先（预告片 / 付费墙词表等）> LLM 语义；gatekeeper 是唯一拦截出口。⚠️ **2026-10-23 · M-26**：`video_tag_count` **不再**是「确定性事实」——媒体计数只是必要条件，不许据此判成功（见 [02 感知与判定域 §M-26](../计划进展/02-感知与判定域.md)） |

---

## 相关文档

- agentruntime-plan —— 外部参考方案（五层架构原稿；原稿已随 2026-10-25 废弃文本清理删除）
- [架构总览](../01-架构总览.md) —— 现行架构、判断点、失败分支全集
- [`11-外部方案对账.md`](11-外部方案对账.md) —— 上一轮外部方案对账（代码控制流版本）
- [P3-views 契约](../contracts/P3-views.md) —— agent 循环原生产出的目标视图形态
