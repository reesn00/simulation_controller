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

⚠️ `per_site_timeout_s` **连 `--help` 都查不到**（CLI 不设，走
`RunConfig` 默认 90.0），而它是批次级断言 B-4（异常处置）唯一的时限判据。

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
| `video_tag_count` | `int` | 覆盖 `<video>` + `<audio>`。规则版 `PLAYER_OK` 的**唯一**判据 |
| `iframe_count` | `int` | **不作判据**，只作排查线索 |
| `elements_total` | `int` | **采到的**交互元素条数（驱动层 limit 截断后）。落盘条数被裁到 80；两者成对落盘 |
| `links_total` | `int` | 同上，链接裁到 50 |
| `interactive_elements` | `list[{ref,tag,label}]` | `ref` 是**会话内句柄**，只存在于 P1；`label` 裁到 120 字符 |
| `links` | `list[{text,href}]` | `text` 裁到 80 字符 |

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
{"action": {...}, "observation": {...} | null, "error": ""}
```

| 字段 | 语义 |
|---|---|
| `action.tool` | `goto` / `click` / `new_tab` |
| `action.params` | `goto`/`new_tab` 是 `{"url": ...}`；`click` 是 `{}` |
| `action.origin` | `"model"`（模型会选）/ `"infrastructure"`（执行器自己做的会话隔离） |
| `action.target` | **只有 click 有**，`{tag, label}` 语义化目标。**没有 `ref`** |
| `observation` | 上一个动作的观察回执。**`observe` 不是动作**——记成动作会让模型学出「调用 observe」 |
| `error` | 非空 = 动作本身失败。与 `observation: null` 是两回事：前者是证据，后者是采集失败 |

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
