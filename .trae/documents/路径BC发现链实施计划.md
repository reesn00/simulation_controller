# 路径 B/C 发现链实施计划（聚合站发现 + 文章引荐）

## Context

对照外部方案（`docs/设计方案/video_agent_trajectory_synthesis_plan.md` §1.2），项目当前只实现了路径 A（搜索引擎直搜）。用户裁定本轮：

- **纯 Python**，不引入 Agent Runtime（维持代码控制流 + LLM 感知插件架构，不推翻 D7）
- **实现路径 B**（影视聚合站发现）与**路径 C**（文章/社区引荐），含**外链校验**与**跳转确认页检查**
- **纳入最小站内搜索**（否则首页型候选会产出「这个站看不了」的错误负样本——§5.14 前置事实污染）
- **单路径 per run**（`--discovery-path` 三选一，默认 direct 完全向后兼容，批次可归因）

核心思路：B/C 只是**候选来源不同**，产出的 Candidate 复用现有环节② visit 链、分支记账、P1/P2/P3 组装。新增的语义判断全部走现有感知题注册表模式（decision 型带 ref/编号白名单、bool 型带 payload 细分）。

## 三条路径的目标形态

```text
A direct（现状不动）:  "{片名} 在线观看" → ① SELECT_PLAY_SITES → 候选 → 首页候选丢弃 → visit
B aggregator:         通用聚合查询(与片名无关) → ①B 选聚合站 → 逐个访问聚合站
                      → ⑤ IS_AGGREGATOR 确认 → ⑥ SELECT_SITE_ENTRIES 选影视站入口
                      → 入口逐个过 resolve_referral(外链校验) → Candidates
C article:            "{片名} 在线观看"(与A同查询) → ①C 选文章/社区页 → 逐个访问
                      → ⑥C SELECT_REFERRAL_LINKS 选正文外链
                      → 外链逐个过 resolve_referral(含跳转确认页检查) → Candidates
B/C 候选统一进环节②:  深层链接 → 标准 visit；首页型候选 → 跳过 FIND_PLAY_CONTROL
                      （首页控件是推荐位内容，与目标片名无关）→ 站内搜索
                      （找搜索框→填片名→提交→结果页复用①选题）→ 结果作为子候选标准 visit
```

## 新增感知题（6 道，全部登记 `perception/questions.py` REGISTRY）

| Q 常量 | 型 | 输入观察 | negative_branch | 复用模式 |
|---|---|---|---|---|
| `SELECT_AGGREGATOR_RESULTS` | decision（selected/rejected，编号回指） | 搜索页 | `not_aggregator` | ①模式；预检只查链接非空（**无片名判据**，`_title_link_sufficiency` 不适用） |
| `IS_AGGREGATOR` | bool | 聚合站页 | `not_aggregator` | ②模式（页面级确认） |
| `SELECT_SITE_ENTRIES` | decision（区分正式入口/介绍页/广告） | 聚合站页 | `aggregator_no_entries` | ①模式 + href 白名单 |
| `SELECT_ARTICLE_RESULTS` | decision | 搜索页 | `not_article` | ①模式 |
| `SELECT_REFERRAL_LINKS` | decision | 文章页 | `article_no_links` | ①模式 + href 白名单 |
| `FIND_SITE_SEARCH` | decision（payload `{ref, why}`） | 站点首页 | `no_site_search` | ③模式 + ref 白名单（`_find_play_control` 同款） |

- `perception/base.py`：`Q` 加 6 常量
- `llm_perceptor.py`：CONTRACTS 加 6 题输出契约 + handler dict 挂 6 题 + `_sufficiency` 预检（SELECT_* 系列查链接非空；FIND_SITE_SEARCH 查 `interactive_elements` 非空——零元素 fail-closed 与 ③ 同口径）
- `rule_perceptor.py`：**不挂 handler**（新题全语义，W1 一律 None fail-closed，与 ①② 现状一致；仅加注释说明）
- `questions.py` 底部 `_BRANCH_QUESTION` 反向映射登记 6 题（d5_scorer 的映射从这里自动推导，**勿手写**）

## 新增分支（7 条，`executor/branches.py`）

| 分支 | 语义 | 入池 |
|---|---|---|
| `not_aggregator` | 聚合站候选实际不是聚合站 | ✅ |
| `aggregator_no_entries` | 是聚合站但无正式影视站入口 | ✅ |
| `not_article` | 搜索结果候选不是文章/社区页 | ✅ |
| `article_no_links` | 文章页无可引荐外链 | ✅ |
| `no_site_search` | 首页型候选无站内搜索框 | ✅ |
| `site_search_no_results` | 站内搜索无结果 | ✅ |
| `site_search_routed` | 首页经站内搜索成功派发子候选（路由记账，非结论） | ❌ NON_SAMPLE |

- `BRANCH_LABELS` + `NON_SAMPLE_BRANCHES` 同步；`RunLedger.record` 的报错文案 13→20
- **不新增 BRANCH_PRECONDITION**：aggregator_no_entries 只在 IS_AGGREGATOR∈{True,None} 后才会被控制流问到（结构保证）；article_no_links 的降级观察由 `_sufficiency` fail-closed 兜住。前置事实表要短（架构总览明文警告）

## 新模块（`executor/steps/`）

### 1. `link_check.py` — 外链校验（B/C 共用）

```python
async def resolve_referral(driver, link_href, *, base_host, max_chars) -> ReferralResolution
# ReferralResolution: status ∈ {direct, redirected, confirm_page, dropped},
#                      url(解析后目标), evidence, note
```

- `new_tab(link_href)` → `observe` → `close_tab`（动作记入所属中间页的 StepLog）
- **跳转确认页判定（纯代码）**：词表 `CONFIRM_PAGE_HINTS`（「即将跳转/跳转至/继续访问/点击进入/离开本站/安全提示」等）+ `_extract_redirect_target(body_text)` URL 正则提取目标
- 确认页但提不出目标 → `dropped`（记 `candidate_filter["confirm_page_no_target"]`，**不是负分支**——外链不是站点结论）
- 目标校验：http(s)、host ≠ 中间页 host、不过 `exclusion_reason` 结构性剔除、非引擎自域
- 直接落地 → candidate = `landed_url`（重定向由落地观测解析），`source_href` 保留原始链接

### 2. `site_search.py` — 最小站内搜索

```python
async def site_search(driver, perceptor, ledger, steplog, candidate, title, *, max_chars) -> SiteSearchResult
# SiteSearchResult: status ∈ {routed, no_box, no_results, unresolved},
#                   results_obs, selected: list[Candidate]
```

- `FIND_SITE_SEARCH`（decision 回 ref）→ `fill(ref, title)` → `press Enter` → 重新 observe
- 页面未变化（url/元素数/正文同页判定，复用 `branches._same_page` 粗粒度特征）→ 尝试点「搜索」按钮（interactive 词表）→ 仍同页 → `unresolved`（提交失败≠没结果）
- 结果页：**复用 `Q.SELECT_PLAY_SITES`**（站内搜索结果也是「提供在线观看该作品的站点」列表，①的语义原样成立；None 时启发式全量，与 A 一致）→ 选中项构造成 Candidate（`origin="site_search"`）
- 需要新动作：`actions.py` TOOLS 加 `"fill"`、`"press"`（含 text/key 参数的 Action 构造器；P3 动作空间 literal 同步）

### 3. `aggregator.py` — 聚合站访问链

```python
async def visit_aggregator(driver, perceptor, ledger, record_steps, candidate, *, max_chars) -> IntermediateResult
```

- goto + observe（可达性复用 `reachability.probe`；超时由 orchestrator `_visit_guarded` 同款包裹）
- `IS_AGGREGATOR`：False → 记 `not_aggregator`（负样本）；None → 继续（fail-closed 不中断，与 ①② 同口径）
- `SELECT_SITE_ENTRIES`：selected 的 href → 逐个 `resolve_referral` → Candidates（`origin="aggregator"`）

### 4. `article.py` — 文章页访问链（同构）

- `SELECT_REFERRAL_LINKS`：selected 外链 → 逐个 `resolve_referral`（**跳转确认页检查发生在这里**）→ Candidates（`origin="article"`）

## 控制流改造（`executor/orchestrator.py`）

- `RunConfig` 增：`discovery_path: str = "direct"`、`max_intermediates: int = 3`、`max_referral_links: int = 5`
- `run()` 环节①按 `discovery_path` 分叉：
  - direct：现状原样（含 `_drop_site_homepages`）
  - aggregator：`search_step.aggregator_query()`（新增通用模板表 `AGGREGATOR_QUERIES = ("影视聚合站", "影视网站导航", "在线影视资源站大全")`，按 task 轮换；**片名仍必填**——站内搜索要用，只是检索词不用它）→ ①B → `_visit_intermediates` 循环（≤ max_intermediates）→ 入口 resolve → Candidates
  - article：与 A 同查询 → ①C → 文章候选 → `_visit_intermediates`（≤ max_intermediates）→ 每页外链 ≤ max_referral_links → resolve → Candidates
- **`_drop_site_homepages` 仅 direct 生效**：B/C 的首页型候选改走站内搜索
- 环节②：visit 前判首页型（`_is_site_homepage`）且 path ∈ {aggregator, article} → `visit_site(..., allow_site_search=True, title=title)`；`VisitResult.search.selected` 的子候选追加进遍历队列（受 `max_candidates` 总量约束，去重按 host 跨中间页累计）
- `RunRecord` 增：`discovery_path`、`intermediates: list[IntermediateResult]`（role/url/landed_url/obs/decision 摘要/提取的候选/steps）；`Candidate` 增 `origin: str = "search"`、`parent: str = ""`（frozen 带默认值，向后兼容）；`to_json` 同步
- 预算耗尽（intermediates/links 上限）→ warnings 记账，正常终态（不重试）
- **本轮不做**：弹窗关闭（obscura links/interactive 是 DOM 级，弹窗不阻断提取；误点风险大于收益，显式记为 out-of-scope）

## 组装层（`assembler/`）

- `schema.py::split_archive`：中间页 = 独立决策单元（`agg-N` / `article-N`），结论形态对齐现有 search 单元（选择即结论；负向时从 ledger 取 `not_aggregator` 等分支）；站内搜索的子候选 visit 是普通 `site-N` 单元（P1 visits 列表天然容纳）
- `views.py`：训练动作空间 literal 加 `fill` / `press`
- 契约文档同步：`docs/contracts/P1-observation.md`（intermediates 段、分支表 13→20、fill/press 动作）、`P2-sample.md`（unit 枚举、origin 字段）、`P3-views.md`（动作空间）、`03-数据契约.md`

## CLI（`executor/cli.py`）

- run 命令增 `--discovery-path {direct,aggregator,article}`（默认 direct）、`--max-intermediates`、`--max-referral-links`；`_run_plan` 透传

## 测试

- `tests/contract/test_perception.py`：ALL_QUESTIONS 自动覆盖 6 新题的 I2/I4/I5/I7；补 I1/I3/I6 显式 case；**分支全集断言 13→20**
- `tests/fakes.py`：FakeLLM 补 6 题应答
- `tests/unit/test_branches.py`：新分支 record / NON_SAMPLE 集合 / UnknownBranch
- 新 steps 单测各一文件；重点：`_extract_redirect_target` 纯函数（词表 + URL 正则 + 排除中间页自域）、`_same_page` 提交失败判定、orchestrator 三路径分叉、homepage 丢弃仅 direct
- 契约测试：P1 intermediates 段、P2 unit 枚举、P3 TOOLS literal（AST 读那个字面量的既有测试自动盯）
- `d5_scorer`：question→branch 映射自动来自 `_BRANCH_QUESTION`，注册后补对照 case

## 文档同步

`01-架构总览`（判断点表 + 路径分叉图）、`03-数据契约`、`06-端到端流程`、`90-增量编年史`（新增量条目）、`02-rubric与代码对齐`（新判断点/分支登记差异；rubric v1.1 正文修订可单独一轮，先在文档对账防错位）

## 实施顺序（每步门禁保持绿）

1. 感知层：6 题 + llm handler + fakes + 契约测试（先有判据再有生成）
2. 分支层：7 分支注册 + 标签 + NON_SAMPLE + `_BRANCH_QUESTION` + d5 映射 + 测试
3. steps 层：`link_check.py` → `site_search.py` → `aggregator.py` / `article.py` + 单测
4. orchestrator + RunConfig/RunRecord + P1 intermediates + 环节①分叉 + 环节②站内搜索接线
5. assembler units + views 动作空间 + 契约文档 + 契约测试
6. CLI 参数 + plan 透传
7. 文档同步
8. 全量门禁 + 冒烟

## 验证

- 全量 pytest 门禁：基线 2328 passed（`test_xlsx_backfill` 1 条存量环境失败恒定剔除）
- direct 回归：默认参数跑一个 direct run，行为与改动前一致（零变化是硬要求）
- B 冒烟：`--discovery-path aggregator --title 功夫 --perceptor llm --max-intermediates 1 --max-candidates 2`，检查 P1 落盘 intermediates 段、`not_aggregator`/`aggregator_no_entries`/`site_search_*` 记账、负样本入池正确
- C 冒烟：`--discovery-path article` 同款小批次，重点核跳转确认页目标提取的 evidence 与 `candidate_filter["confirm_page_no_target"]` 记账
- 每步后跑全量门禁，最终增量条目写入 `90-增量编年史`
