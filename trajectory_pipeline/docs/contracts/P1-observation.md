# P1 观察存档 · 字段级契约

**产出方**：[`executor/orchestrator.py::RunRecord.to_json()`](../../executor/orchestrator.py)
**消费方**：`cli report` / `cli check-archives` / `perception.replay` / `assembler.schema.split_archive`
**落点**：`trajectory_pipeline/output/pipeline/<task_id>__<run_id>.json`（平铺单文件）

> 本文以 **`to_json()` 的实际返回**为准，不是设计意图的转述。
> 由 `tests/contract/test_p1_contract.py::Test字段集一致` 盯住——
> **代码加了字段而本文没跟上（或反过来）会红**。
> 这正是 2026-10-09 那轮的教训：`run_config` / `elements_total` /
> `links_total` / `site_url` / `play_page_url` 五个字段落地时
> `TestDegradedMarkersMatchSplit` 全绿（它只盯**缺件标记**），
> 契约文档却还是空的——**那张表盯不住新增字段**。

### `schema_version`：为什么这一栏是「缺了会怎样」而不是「是什么」

它是**写入时**记录的格式版本，于是「这批数据是哪一批」能从存档本身读出来。

⚠️ **历史存档不回填**，这是刻意的：没有依据断定一份没版本号的存档属于哪一批，
而回填就是猜——猜错的代价是一个**静默错误**的批次归属。
现存 38 份全都没有这个键，它们是**未标注**，不是「v1」。
评估侧据此分档：认得版本就按版本分，认不出来才退回 `run_config` 缺件代理，
**并把那份代理标注为代理**（见 `evaluation.cli._batch_basis`）。

⚠️ **代理标注了，但它切的那一轴是「字段齐不齐」不是批次**（2026-10-10
实测：现存 38 份全无版本号，其中 16 份缺 `run_config`，而那 16 份的 74 个
`outcome` 一个都没有 `site_url` / `play_page_url` 键，另 22 份的 131 个
全都有——相关性 100%）。所以无版本号时的档名按判据起
（`未标版本·缺 run_config` / `未标版本`），**不叫「旧批次 / 当前批次」**。
「为什么不可判定」另有一张事实表 `evaluation.cli._print_markers`——
它读 `p1_missing_fields`，那是键在不在 P1 里，不需要推断。

改这个值等于宣告一批存档与另一批不可比，所以**只在破坏性变更时加**：
加字段、补可选字段不动它。

---

## 三条总口径

1. **观察是真实回放，不是 LLM 转述。** 观察里的每一个字符都来自
   `document.body.innerText`（主来源）或 `browser_snapshot`（URL/Title 与
   诊断锚点）。这条一旦破，rationale 就没有锚。
2. **正文落全文，不落摘要。** `body_text` 是完整正文（实测 1525 字符量级），
   早期只留 400 字符 `body_preview`，已废——站点会下线改版、被反爬时重抓
   不回来，而落在预览之外的实体被判成幻觉时，那**不是幻觉，是没存**。
   老存档里两种键可能并存，读侧按 [`assembler/schema.py::_obs_missing`](../../assembler/schema.py)
   区分。
3. **不含任何页面句柄与凭据。** 观察里**有** `ref`（`e5` 形），但它是
   obscura 的**会话内句柄**，只存在于 P1；**动作里没有 ref**（结构保证，
   见 [`executor/actions.py`](../../executor/actions.py)），P2/P3 也不带它。
   Cookie / Authorization / 浏览器 profile 一律不落盘（红线，见 `obscura_driver.FORBIDDEN_TOOLS`）。

---

## 顶层字段

| 字段 | 类型 | 缺了会怎样 |
|---|---|---|
| `schema_version` | `str` | **P1 格式版本**（写入时记，见 `orchestrator.SCHEMA_VERSION`）。缺它 ⇒ 这份存档**没有版本身份**——不是「旧版」，是「不知道是哪一批」 |
| `task_id` | `str` | **缺即 FATAL**，无法归组也无法回查任务定义 |
| `title` | `str` | 判断点 ① 的**唯一判据来源**。空/编号会让 ① 在错误前提上自信作答 |
| `query` | `str` | 实际拿去搜的检索串。与 `user_prompt` 是两件事 |
| `user_prompt` | `str` | 用户**开口说的那句**（persona 渲染后）。空串 = 手工路径；缺键 = 老存档 → `DEGRADED` |
| `search_url` | `str` | |
| `perceptor` | `"rule" \| "llm"` | 哪一版感知实现跑的。灰度只在构造期，所以这是批次级属性 |
| `candidate_source` | `"heuristic" \| "perceptor"` | 候选来源。`heuristic` = 代码全跑；`perceptor` = 判断点 ① 选过 |
| `result_titles` | `int` | `RESULT_SELECTORS` 取回的结果标题数。0 = 输入不足，① 恒 fail-closed |
| `search_blocked` | `str` | **运行级**反爬状态（如 `captcha`）。非空 ⇒ 不产出搜索单元样本 |
| `gatekeeper_events` | `list[{stage,verdict,reason,disposition,blocked_surface?,attempt?}]` | **Gatekeeper 事件账本**（2026-10-16 方案 12 阶段 G / D-A3 最小账本）。`verdict` 取值封闭于 `executor/gatekeeper.py::Verdict` 七值；`disposition.kind ∈ {inject_guidance, escalate_model, fail_closed}`——静默自愈已按 D-A2 否决，**每一档都可见**。与 `search_blocked` 并存不替代：后者是冻结口径的运行级字段，这里是统一账本（A2 起 agent 每步拦截都记）。空 = 本次运行无拦截事件。**新增可选字段，不动 `schema_version`**。<br>**两栏盖戳（2026-10-26 方案 20 §R3）**：`blocked_surface`（哪张采集面被拦，取值同 `golden.access_surface`；面**从 `driver` 折算**、采集点不手填）+ `attempt`（**同一 verdict 在本次运行里是第几次**，1 起）。只盖 `verdict ∈ gatekeeper.BLOCK_VERDICTS` 的事件（`blocked_cf` / `captcha` / `login_wall` / `paywall` 四条页面侧访问控制）——`dead_loop` 是**行为失败**不算被拦，升配/驳回两档没有 verdict。全 run 被拦几次 = **有 `blocked_surface` 的行数**；按 verdict 分组就是分桶计数（02 避坑 §3.7 要求真·反爬与登录墙分开报）。认不出的 `driver` 记 `unrecorded` **不抛**（落盘边界，见 `orchestrator._access_surface_of`） |
| `executor_mode` | `"scripted" \| "agent"` | **执行器模式**（2026-10-17 方案 13 §6.1，A2a 埋点；A3 转正为 `RunConfig` 字段）。`scripted` = 七段固定序列，`agent` = 站内自主导航（agent_loop）。**批次级属性**：一批要么全 scripted 要么全 agent，模式在构造期定下、候选内不 fallback（混合轨迹不可归因）。它是 A/B 两批存档在磁盘上**唯一**能区分这两者的字段——两批的 `steps` 同形（六值动作空间）、`visits` 同形，没了它只能靠文件名猜。老存档无此键（可选字段纪律，不动 `schema_version`） |
| `driver` | `"obscura" \| "camoufox" \| "camofox"` | **执行层**（2026-10-20 方案 15 §7 R1）。与 `executor_mode` **正交**：「哪条链跑的」与「哪个浏览器跑的」是两个归因轴，缺一个读档人就分不清。它还是**工具面**的唯一来源——目标编码（`{tag,label}` vs `{role,name}`）由执行层决定，训练侧的声明按它折算（`toolcatalog.surface_of_driver`）。空串 = **没记**（老存档没有这个键，而那批真实跑的就是 obscura），不是「未知执行层」。**2026-10-22 起生产链默认 = `camoufox`**（owner 裁定）→ **2026-10-26 起默认 = `camofox`**（owner 裁定，见[设计方案 21](../设计方案/21-camofox-browser执行层接入方案.md)）：camoufox 执行层**已撤下**（实现文件已删、`playwright` 已从主依赖移除，**禁止接回来**），改走 **camofox-browser 自带的 MCP stdio 适配器**（`camofox-browser-mcp`）。⚠️ **`camoufox` 仍在取值域里**：存量存档里就有这个值（盘上 14 份顶层 P1 存档记着它），删掉取值域会让读存档的人以为它不可能出现——它**已退役、只读存量**，由 `toolcatalog.LEGACY_DRIVER_SURFACE` 折算回 `camoufox-1` 面。**新增可选字段，不动 `schema_version`** |
| `fingerprint` | `{str: Any}` | **执行层指纹读数**（2026-10-22 P6）。⚠️ **形状随执行层变，盘上两个形状并存**——**九键**（camoufox 期，存量存档就是它）：`os` / `screen` / `window` / `dpr` / `humanize` / `geoip` / `main_world_eval` / `headless` / `locale`，其中 `screen`/`window`/`dpr` 是**实测值**，其余是**请求值**；**十一键**（camofox 期，2026-10-26 起）：`user_agent` / `platform` / `language` / `languages` / `timezone` / `screen` / `window` / `dpr` / `webdriver`（**实测值**）+ `engine`（恒为 `"camoufox"`——**浏览器内核还是 camoufox**，换的只是接入方式 ⇒ **跨批次可比**）+ `base_url`（REST origin，**已剥掉 path / query / userinfo**）。⚠️ 九键里的**请求值**（`os` / `geoip` / `locale` / `humanize` / `headless`）在十一键形状里**读不到了**：它们是上游服务的配置，本仓库**没有资格替它声明**，填一个就是编数据。⚠️ **launch 时测的，事后补不回来**——所以必须跟 `driver` 一起进存档；事后再去问浏览器已经没有这一批的读数了。**空 dict = 没记**（obscura 是远端 MCP，看不见窗口尺寸），与「记了但是空」在下游是两回事。**新增可选字段，不动 `schema_version`** |
| `llm_calls` | `list[{ts,caller,candidate,model,latency_ms,prompt_tokens,completion_tokens,error}]` | **LLM 调用账本**（2026-10-17 方案 13 §3.6，A2a-4）。agent 循环每次问模型记一行。**记账范围只圈 agent_loop**：感知层十一题的调用不汇入（§3.6 的 A2a 决策，感知层侵入换不来收益）。`prompt/completion_tokens` 为 `null` = 后端没给 usage，**不是 0**——「没记到」与「没花」在成本分母上是两个事实。`ts` 粒度是**候选级**：循环体不引入时间源，由 orchestrator 在候选访问收口时统一加盖（同候选各条共享一戳；候选内次序看 list 顺序，单条耗时看 `latency_ms`）；超时/异常收口的候选可能带着没盖戳的残条落盘。scripted 批恒空 list。**新增可选字段，不动 `schema_version`** |
| `candidate_filter` | `{str: int}` | 剔除**原因计数**：`excluded_host:<root>` / `duplicate_host` / `impossible_content:gov.cn` |
| `run_config` | `{str: Any}` | **`RunConfig` 全量快照**。缺它 `--max-candidates 5` 与默认 20 跑出的两批在存档里一模一样，而覆盖完整性差 4 倍 → `DEGRADED` |
| `provenance` | `{str: Any}` | persona 六维等切片轴。**空 dict 的含义固定为「不是 taskgen 跑的」** |
| `elapsed_ms` | `int` | |
| `warnings` | `list[str]` | 人读的运行期告警（如「判断点 ① 选出 7/7 个候选，0 个记 not_play_site」） |
| `search_observation` | `Observation \| null` | 搜索页观察。被反爬拦时是验证码页 |
| `steps` | `list[ActionStep]` | 搜索阶段动作流 |
| `candidates` | `list[{url,text,rank,host}]` | 取候选的**结果**。`heuristic_candidates` 的分母 |
| `visits` | `list[Visit]` | 每个访问过的站点 |
| `ledger` | `LedgerSummary \| null` | 分支分布 + 覆盖率 |
| `outcomes` | `list[SiteOutcome]` | 每个站点的最终归宿 |

### `run_config` 的字段

`engine` / `max_candidates` / `max_chars` / `stop_after_success` / **`per_site_timeout_s`**。

⚠️ `per_site_timeout_s` 是批次级断言 B-4（异常处置）唯一的时限判据。

**2026-10-22 变化**：默认值 90.0 → **180.0**，且**已暴露到 CLI**
（`run --per-site-timeout`，不传就走源码默认）。

- 为什么涨：90 是 **obscura 时代**的数。camoufox 单次点击实测上界
  13.3s（真人鼠标轨迹回放），8 步 ≈ 122s——90s 会**必然触顶**。
- 触顶的后果不是「超时」这么轻：它会被记成 `unreachable_hard`，
  也就是**一条理由通顺的假负样本**。假负样本进了池，模型学到的
  是「这类站点了不得」——而真相是「我们没等够」。
- 180 ≈ 实测上界的 1.5 倍；再慢就该先看是不是站点本身的问题。

⚠️ **180 是 camoufox 那一支的标定值**——2026-10-26 执行层换成 camofox-browser
（走上游 REST 服务）之后，**它的延迟特性还没量过**：camofox 期跑的是**另一条
链路**，延迟分布与 camoufox 直连**不可比**。所以 180 到了 camofox 期是
**保守值，不是标定值**——先按它跑，等量过再改（改的是**默认值**，不是
存量存档里的值）。

### `ledger`（`RunLedger.summary()`）

| 字段 | 含义 |
|---|---|
| `total_sites` | 有 outcome 的访问点数 |
| `succeeded` | `branch is None` 的计数 |
| `negative_samples` | `is_negative_sample` 为真的计数 |
| `by_branch` | `{分支名: 条数}` |
| `missing_branches` | **九条分支里这批一条都没产出的是哪几条**。比数量更能说明问题：数量 0 可能只是样本少，`missing` 直接指向「这条路径没接上」 |
| `heuristic_candidates` | 代码启发式给出的候选数 |

> ⚠️ **跨批次比较 `by_branch` 前先看批次边界**：`player_unverified` 是
> 2026-10-09 新增分支，历史存档里的等价情形仍记 `unresolved`（**不回填**，
> 纪律：并池是写入时的动作，不是读取时的推导）。边界之前 `unresolved` 高、
> 之后 `player_unverified` 高，不记批次就会读成「fail-closed 修好了」。

---

## `Observation`（出现在 `search_observation` / `steps[].observation` / `visits[].{site_obs,player_obs}`）

| 字段 | 类型 | 语义 |
|---|---|---|
| `url` | `str` | **实际落在哪**。与候选 url 可能不同（`http→https` 跳转） |
| `page_title` | `str` | |
| `body_text` | `str` | **全文**。老存档可能是 `body_preview`（400 字符） |
| `body_len` | `int` | `len(body_text)`，与正文同源 |
| `body_source` | `"inner_text" \| "snapshot"` | **可审计字段**。`"snapshot"` 意味着正文是被 CSS 抢过预算的残骸，落在它上面的语义判定可信度更低 |
| `truncated` | `bool` | 上游 `max_chars` 预算用尽。**必须显式**——不然会把「实体落在截断外」误判成幻觉 |
| `raw_len` | `int` | 预处理前的正文长度 |
| `stripped_ratio` | `float` | 剥离 CSS/JS 后的保留比例；`<0.5` 说明页面脏 |
| `degraded` | `list[str]` | 本次**没采到**的项。**fail-closed 的前提**：没有它，「没采到交互元素」与「页面上确实没有」在数据里完全一样。另含**采到了但不可读**的两项 `body_mojibake` / `title_mojibake`（解码损坏，见下） |
| `video_tag_count` | `int` | 覆盖 `<video>` + `<audio>`。**观察事实，不作判据**——媒体计数只是「有播放组件」的**必要条件**，不许据此判「能播」（2026-10-23 · M-26，见 [02 感知与判定域 §M-26](../计划进展/02-感知与判定域.md)） |
| `iframe_count` | `int` | **不作判据**，只作排查线索 |
| `elements_total` | `int` | **采到的**交互元素条数（驱动层 limit 截断后）。落盘条数被裁到 80；两者成对落盘。⚠️ **camoufox 下这个数已经过三层过滤**（词表外 role / 无名字 / 真同名整组丢弃）——过滤账目见下面的 `element_basis.face`。⚠️ **camofox 下过了五层**：共享那三层之外**本面再多两档**（`unref_dropped` 无上游句柄 / `name_ambiguous` 名字子串歧义）——面专属的档位**只在本面出现**，别的面没有这两档。⚠️⚠️ **跨驱动比这个数（§6.1 的 30% 判据）不成立**：各面数的是**不同的集合**（obscura = 上游工具给的交互元素；camoufox = ARIA 树里过了词表的那些；camofox = 再过了本面那两档的），差值混着「枚举基础不同」与「过滤规则不同」两件事。**可比的那个数在 `element_basis.shared`** |
| `links_total` | `int` | 同上，链接裁到 50 |
| `interactive_elements` | `list[元素]`，形状**逐方言**（见下） | `ref` 是**会话内句柄**，只存在于 P1；text 槽裁到 120 字符 |
| ↑ 的键名 | obscura 面 `{ref,tag,label}`／camoufox 面 `{ref,role,name}`／**camofox 面 `{ref,role,name}`** | **P4 起按方言投影**（`driver` → 工具面 → 方言，只有一条轴，见 [设计方案 04 §4.8](../设计方案/04-camoufox执行层替换方案.md)；camofox 期见[设计方案 21](../设计方案/21-camofox-browser执行层接入方案.md)）。槽位名统一、**值域不统一**：obscura 面的 kind 槽装 HTML 标签名（`button`/`input`），camoufox 面与 camofox 面装 ARIA role（`button`/`textbox`）——`input` 与 `textbox` 在两套词表里对不上，**不可混池**。⚠️ **两个 aria 面的键名与词表相同，但声明不同**（camofox 面的定位走的是上游的 `getByRole(role, {name}).nth(nth)`，**子串匹配**）——声明层的差别见 P2 的工具面一节。键名由 `Observation.element_vocabulary` 决定（驱动实现写、回放按键名恢复），读档侧统一走 `common/elements.py` 的 `element_role`/`element_name` |
| `links` | `list[{text,href}]` | `text` 裁到 80 字符 |
| `element_basis` | `dict \| null` | **跨面可比的元素账目**（设计方案 04 §4.9.6，2026-10-21）。**新增的一列，不改 `elements_total`**。结构 `{"shared": {...}, "face": {...}}`——**两本账平级**：`shared` 是**同一段 JS + 同一把尺子**在两个浏览器上各跑一次的结果（**按构造可比**），`face` 是本面自己的枚举账目（obscura 面只有 `nodes`/`kept` 两条，给不出「role 命中几条 / 无名丢掉几条」——**缺就是缺，不填 0**）。档位名见 `executor/browser/element_basis.py` 的 `LADDER_STAGES` / `SHARED_BASE_STAGES`。**`null` = 这一批没开 `--element-basis`**，不是「页面上没有元素」——这两种「没有」必须分开读。由 `cli run --element-basis` / `cli check --element-basis` 显式开启（**默认关**：多一次 evaluate 往返，是测量仪器不是生产链必需项）。⚠️ **空的账不落盘**：某一本账没测到时**缺键**，不落 `{}`——空账会被读成「测了，什么都没有」 |
| `element_basis_error` | `str \| null` | **共享口径没测到的原因**（2026-10-21）。⚠️ **它与 `degraded` 分开**——`degraded` 是**内容块**的降级通道，下游只判它非空（`steps/search.extract_candidates` 见到降级就**不取候选**、`gatekeeper` 见到降级就拦），把**测量失败**混进去等于让一把坏尺子**作废整批数据**。三种状态分开读：`element_basis=null` 且本列 `null` → **没开开关**；`element_basis` 缺 `shared` 键且本列非空 → **开了但没测到**（face 账目仍在）；`element_basis` 完整 → **测到了** |

### `elements_total` / `links_total` 为什么是 P0

落盘条数被裁到 80/50，而**总量不落盘**会让两种**相反**的错在数据上同形：

```text
目标真的存在于第 81 个元素之后   → 查不到
目标根本不存在（模型幻觉出来的） → 查不到
```

两者产出同一个「查不到」，评分器于是把**采集器的取舍**记成**轨迹的缺陷**。
rubric v1.1 D-2 的边界规则要求这种情况判「不可判定（存档裁剪）」——
**这是唯一一条不补数据就必然产生反向错判的规则**。

---

## `ActionStep`（`steps[]` 的元素）

```json
{"action": {...}, "observation": {...} | null, "error": "", "duration_ms": 1234 | null}
```

| 字段 | 语义 |
|---|---|
| `action.tool` | `goto` / `click` / `fill` / `press` / `new_tab` / `close_tab`（封闭六值，与 `executor/actions.py` 的 `TOOLS` 同源；`observe` 不是动作） |
| `action.params` | `goto`/`new_tab` 是 `{"url": ...}`；`fill` 是 `{"text": ...}`；`press` 是 `{"key": ...}`；`click`/`close_tab` 是 `{}` |
| `action.origin` | `"model"`（模型会选）/ `"infrastructure"`（执行器自己做的会话隔离：`new_tab` / `close_tab`） |
| `action.target` | `click`/`fill` 的语义化目标 `{tag, label}`（obscura 面）／`{role, name}`（camoufox 面）／`{role, name}`（camofox 面），**没有 `ref`**；目标为 `None` 时照实记——那是 I6 被违反的现场，不编目标补上。`press` 可带：回车等全局按键提交时 `null`（点「搜索」按钮提交走 `click`，不走这里）。`goto`/`new_tab`/`close_tab` 无此字段 |
| `observation` | 上一个动作的观察回执。**`observe` 不是动作**——记成动作会让模型学出「调用 observe」 |
| `error` | 非空 = 动作本身失败。与 `observation: null` 是两回事：前者是证据，后者是采集失败 |
| `rationale` | 本步的思考+结论（`WriteResult.to_json()` 形状：`status` / `rationale:{text,conclusion}` / `reason` / `degraded`）。**scripted 形态** = rationale writer 产物（2026-10-17 INC46 起为动作前动机，`reason=""`）；**agent 形态** = 循环的原生 think（A3-3 / INC58，`reason="native-think"`——来源标记，两形态在 P1 里可区分）。`null` = 该步没有思考文本（执行器自己的步、或 scripted 未接 writer），**不与「写了但为空」相混** |
| `duration_ms` | **本步的墙钟耗时（毫秒）**，从动作发起到观察/失败回填（2026-10-21 起）。**不是动作单独那几秒**——成功路径上它含随后的观察耗时；失败路径上动作一抛就收口，整步 ≈ 动作耗时。**为什么必须有**：camoufox 面的点击超时（5s）与「页面没有播放器」在证据上同形，都是一条 `error` 非空的步骤，没有耗时字段就分不出「慢了一点」与「慢到离谱」，只能靠猜。`null` = 这一步没走到收口（中途异常），**不与「0 毫秒」相混**。**新增可选字段，不动 `schema_version`** |

### `action.tool` 六值的来历

`goto` / `click` / `new_tab` 是 direct 路径的三件套；`fill`（把片名填进搜索框）与 `press`（回车提交检索）自 2026-10-16 路径 B/C 的站内搜索链获得真实产地，`close_tab` 同轮随外链校验（`new_tab` 开、看完关）获得产地。**语义层与驱动层自 2026-10-20 起同名**（驱动侧旧名 `type_text`/`press_key` 已随 D12 改掉，见[设计方案 04 §4.6](../设计方案/04-camoufox执行层替换方案.md)）。P2/P3 链路早已渲染 fill/press 步——契约文字此前停在旧三值，是**文档没跟上代码**，不是契约变更（A2a-0 / INC49 收口；文档 ↔ 代码的双向同步由 `test_p1_contract.py` 的漂移守卫钉住）。

---

## `Visit`（`visits[]` 的元素）

| 字段 | 语义 |
|---|---|
| `url` | **候选链接**（搜索结果给的地址） |
| `landed_url` | **实际落地的地址**。与 `url` 不等时（`http→https` 跳转）outcome 要按它配对 |
| `success` | 与 `outcomes[].branch is None` 是**两个独立来源**，打架时门禁报 `success_branch_mismatch` |
| `notes` | 人读的该站备注（如 `["IS_REACHABLE=False"]`） |
| `steps` | 该站点阶段的动作流 |
| `site_obs` | 判断点 ②③ 的输入 |
| `player_obs` | 判断点 ④ 的输入。未走到播放页时为 `null` |

---

## `SiteOutcome`（`outcomes[]` 的元素）

| 字段 | 语义 |
|---|---|
| `url` | ⚠️ **一字段三语义**：导航失败=候选 URL、找控件失败=`landed_url`、到播放页=`player_obs.url` |
| `site_url` | 候选链接。**恒有值**。rubric D-6「站点 URL」 |
| `play_page_url` | 播放页地址。未走到播放页时为空。rubric D-6「播放页 URL」 |
| `branch` | `null` = **成功**。非空取九条分支之一 |
| `branch_label` | 分支的中文标签（`null` 时为「成功」） |
| `evidence` | 判据原文。**空即 FATAL**——报表上这个数字背后什么都没有 |
| `question` | 产出这条结论的判断题 id |
| `source` | `"rule"` / `"llm"` / `"human"`（人工复核回填） |
| `fallback_used` | 走了降级路径。⚠️ 存在性判定带这个标记，**成功率高不代表能正常播放** |
| `reached_play_page` | 是否走到了播放页 |
| `is_negative_sample` | `branch` 非空且 ∉ `NON_SAMPLE_BRANCHES` |
| `branch_before_downgrade` | 人工降级前的分支。**只有被 `cli downgrade` 处置过的记录才有** |
| `branch_before_relabel` | 人工**改判**前的分支（**最初**那一个，链式改判不覆盖）。**只有被 `cli relabel` 处置过的记录才有** |
| `success_downgraded_by` | `visits[]` 上的降级留痕，见下 |

> 三个 URL 字段**不合并**是刻意的：合并后读档的人必须回查控制流才知道
> 它指哪一层，而丢了其中任何一个，另一个再也恢复不了。

### 人工降级的两个留痕（`cli downgrade` 写）

降级**改的是结论**，所以它必须留下「原来是什么」与「凭什么改」：

| 位置 | 形状 | 谁读它 |
|---|---|---|
| `outcomes[].branch_before_downgrade` | `"no_play_control"` 这样的原分支 | 读档的人、回测 |
| `outcomes[].evidence` | `[原判 <分支>] <原正文>\n\n<降级理由>` | 读档的人 |
| `visits[].success_downgraded_by` | 落款 | `check-archives` 的 `success_branch_mismatch` |

**「成功」在存档里记在两处**：`outcome.branch is None` 与 `visits[i].success`。
降级必须同时翻这两处——只翻一处就留下 `success=True 但 branch=unresolved`，
而门禁 `check-archives` 的 `success_branch_mismatch` 正查这个。

> ⚠️ **`evidence` 的 `[原判 …]` 前缀是幂等判据**：`base_evidence()` 剥掉前缀
> 与 `\n\n` 之后的一切，所以「已处置过的记录」按**戳**识别，不按 `branch` 值。
> 现有 38 份原档大多是本命令落地之前处置的，**没有**这个前缀。

### 人工改判的留痕（`cli relabel` 写，2026-10-21 H-16 修复②）

改判**也改结论**（只是**留在**负样本池里），所以同样留痕：

| 位置 | 形状 | 谁读它 |
|---|---|---|
| `outcomes[].branch_before_relabel` | `"subscription_wall"` 这样的**原判**（链式 A→B→C 之后它仍是 A） | 读档的人、回测 |
| `outcomes[].evidence` | `<原正文>\n\n[改判 <旧> → <新>] <理由>` | 读档的人 |
| `outcomes[].source` | `"human"` | 报表的「人工介入比例」 |
| 负样本池行 | `branch` / `branch_label` / `source` / `evidence` **同步改** | **训练侧吃的是池行**（`evaluation.golden.builder`） |

⚠️ **戳刻意不同于降级**：降级是 `[原判 X] `（且**剥掉**前缀与 `\n\n` 之后
的一切），改判是 `[改判 A → B]` **追加在正文之后**。复用同一个前缀会让
第二次改判被当成「已处置」跳过，链式改判就断了。
改判的幂等判据是**目标分支值**（已经是目标分支就跳过）。

⚠️ **改判必须同步改负样本池**：训练侧读的是池行的 `branch`，
不是存档；而门禁 `integrity.check_archive` 查的是「存档里每条真负样本的
`(task_id, url, branch)` **在不在池里**」——只改一边 ⇒ 门禁报
`negative_not_pooled`（DEGRADED），而报告上只多一行「没并池」，
看不出是改判写了一半。

---

## 九条失败分支

| 判断点 | 分支 | 进负样本池 | 需 LLM |
|---|---|---|---|
| ① | `not_play_site` | ✅ | ✅ |
| ② | `unreachable_hard` | ✅ | ❌ 代码判定 |
| ② | `login_wall_or_blocked` | ✅ | ✅ |
| ③ | `no_play_control` | ✅ | ✅ |
| ③ | `trailer_only` | ✅ | ❌ 代码判定（词表） |
| ③ | `trailer_suspect` | ❌ 人工兜底 | ➖ |
| ④ | `component_unverified` | ✅ | ✅ |
| ④ | `player_unverified` | ❌ 人工复核 | ➖ |
| ④ | `unresolved` | ❌ 人工复核 | ➖ |
| ⑦ | `site_search_routed` | ❌ 路由记账（**不入负样本池**，2026-10-16 路径 B/C） | ❌ 代码判定 |

**每条分支都必须有对应样本入库**——负样本不是副产品，是强制产出。
反过来，后四条**不是负样本**：前三条混进池里会污染「这里真的看不了」
这条训练信号；`site_search_routed` 更冤——它记的是「派发了子候选、
处理还在继续」，结论由子候选自己的 visit 记。

⚠️ `search_blocked` **不进**这套体系：拦截发生在取候选之前，
根本没有 outcome 可记。它单开运行级字段。

---

## 三档门禁（`cli check-archives`）

| 档 | 触发 | 含义 |
|---|---|---|
| `FATAL` | `not_utf8` / `bad_json` / `missing_task_id` / `evidence_mojibake` / `evidence_empty` / `success_without_media`（rule 版）/ `negative_contradicted_by_success` | 不可信，**只能重跑** |
| `DEGRADED` | `steps_missing` / `user_prompt_missing` / `run_config_missing` / `body_preview_only` / `no_body` / `outcome_missing` / `success_branch_mismatch` / `mojibake`（观察内）/ `negative_not_pooled` | 缺件，能读但不能直接当训练数据 |
| `INFO` | `success_without_media`（llm 版） | 值得看一眼 |

DEGRADED 码与 P2 的 `degraded_from` 标记由
[`integrity.degraded_marker_map()`](../../executor/integrity.py) 映射，
两张表由 `tests/unit/test_integrity.py::TestDegradedMarkersMatchSplit` 双向盯着。

> ⚠️ **`mojibake` 刻意不在那张映射表里**（有测试钉住）。该表答的是
> 「P1 里**没有的键**」，而解码损坏是「键在、内容坏了」——混进去会让
> 「正文被截短」与「正文是烂的」在 `degraded_from` 里长得一样，
> 而后者更糟（整段正文都不可信，不只是截断区外）。
>
> 损坏的记录路径在**采集期**：`Observation.degraded` 里的
> `body_mojibake` / `title_mojibake`，由
> [`executor/dom.py::is_mojibake`](../../executor/dom.py) 判定。
> 两者**分开记**是因为可只坏其一（实测坏站 tv.sohu.com 就是标题与
> 正文中间一段坏）。
> 成因实测见[避坑指南 §6.5](../02-避坑指南.md)。

---

## 相关文档

- [数据契约](../03-数据契约.md) —— P1/P2/P3 的分工与切分口径
- [架构总览](../01-架构总览.md) —— 模块地图与控制流
- [避坑指南](../02-避坑指南.md) —— 症状写成终端里真正会看到的那句话
- P2 契约 / P3 契约 —— 待写（`assembler/builder.py` 与 `assembler/views.py` 已落地）
