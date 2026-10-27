# URL 级金标冲突清单（M-30）

> **机器生成的事实块 + 人填的两列结论**。事实块由 `agreement --emit-url-conflicts` 刷新；**「定性」「裁定依据」两列由人填，机器重跑不会抹掉**。
>
> ⚠️ **「现场重采」那几条依据是 2026-10-23 的 camoufox 面读数**——
> **2026-10-26 执行层已换成 camofox-browser**（owner 裁定，camoufox 那一支**已删**、
> **接不回来**）⇒ **照着那些依据重跑要换 `--driver camofox`**，
> 而**换面读数会不同**（aria 两面元素枚举同族但定位语义不同：
> camoufox 是**精确**匹配、camofox 是**子串**匹配）。
> **本清单里已有的裁定结论一个字不改**——它记的是**当时那一次访问**的事实
> （Q-11「按访问」）。

> 生成命令：`python -m trajectory_pipeline.evaluation.golden.builder agreement --version 7 --emit-url-conflicts trajectory_pipeline\evaluation\golden\data\url_gold_conflicts.md`

> 带 d10 金标的 URL **131** 个，其中 **19** 个带多于一种标签；**本清单只列这多头的那些**（单头的没有冲突可定性）。
> 切档按**行**不按 URL：跨档 URL **25** 个；持有集 **65/101** 行与调参池/回归集同 URL（Q-12 裁定按行切 ⇒ 泄漏保留，所有持有集读数须带此附注）。

> **「定性」三选一**：`漂移可解释` / `不可能共存` / `待查`。证据不够就填「待查」——**不许用排除法断言**（同一 URL 两次访问结果不同可能是站点漂移、地区、登录态，那是真事实）。

---

## 1 · `https://m.ixigua.com/video/6582085495839261192`

- **行数**：14
- **金标分布**：unresolved 11 / broken 3
- **档位**：calibration / holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**两种不同的失败，不矛盾。`unresolved` 11 条证据原话「采到 0 个交互元素（正文 100 字符）——哨兵返回无法区分『页面没渲染完』与『页面确实没控件』，不判」，`fallback_used=true`；`broken` 3 条里 T029__0a800a3f 是传输层导航失败（obscura `Network error: error sending request for uri`）、T033__53ce4b49 与 T025__7141c8de 正文 21 字符只有「Internal Server Error」。⇒ 一个是采集没拿到，一个是站点自己挂了。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `T029__0a800a3f__db330971` | calibration | broken | broken | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | rule | False | False | 是 | INC33_NOTE: 同步 INC32 D10 偏差修缮（簇 2 unreachable_hard → broken）。INC32 仅改 v2/holdout/calibration.jsonl，没改 annotations.jsonl；v3 freeze 时需同步，否则 broken 标签丢失。; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T033__53ce4b49__db330971` | calibration | broken | broken | auto-validator-l3l4-v1-weak-confirmed | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | INC33_NOTE: 同步 INC32 D10 偏差修缮（簇 2 unreachable_hard → broken）。INC32 仅改 v2/holdout/calibration.jsonl，没改 annotations.jsonl；v3 freeze 时需同步，否则 broken 标签丢失。; INC37_NOTE: weak_confirmed; L4[unknown(0.30) -> -]; L3[broken(0.85)] |
| `T014__acaa3ef1__db330971` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T028__d76e1085__db330971` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T031__3af4e21b__db330971` | regression | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T031__de0c1df5__db330971` | regression | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T037__e33453ed__db330971` | regression | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T040__7b37c195__db330971` | regression | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T040__c9982a1c__db330971` | regression | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T025__7141c8de__db330971` | holdout | broken | broken | auto-validator-l3l4-v1-weak-confirmed | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | INC33_NOTE: 同步 INC32 D10 偏差修缮（簇 2 unreachable_hard → broken）。INC32 仅改 v2/holdout/calibration.jsonl，没改 annotations.jsonl；v3 freeze 时需同步，否则 broken 标签丢失。; INC37_NOTE: weak_confirmed; L4[unknown(0.30) -> -]; L3[broken(0.85)] |
| `T011__443c5ec0__db330971` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T036__10313caf__db330971` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T018__ad4a6065__db330971` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T012__8c050356__db330971` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |

---

## 2 · `https://v-wb.youku.com/v_show/id_XNDE0NjYzODAzNg==.html?vid=XNDE0NjYzODAzNg==`

- **行数**：14
- **金标分布**：success 8 / no_play_control 3 / subscription_wall 3
- **档位**：calibration / holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**14 行共用同一份正文（body_len 1432–1448、iframe=1、video=0），分歧全在同一句话怎么读：「续费会员」。`subscription_wall` 3 条据「页面明确显示『续费会员』」；`success` 8 条据「检测到 1 个播放器 iframe，且『续费会员』与『历史』『客户端』『登录』同属顶部导航，未见要求开通会员的明确提示」（这批 gold 自己带 `L4[paywall(0.85) -> subscription_wall]`）；`no_play_control` 3 条据「未提供原文为『播放』的按钮、剧集列表项或带 ref 的播放器控件」。⇒ 页面没自相矛盾，是「导航栏的『续费会员』算不算付费墙信号」这条判据边界上有两种读法。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `T040__c9982a1c__5c2c73cf` | calibration | success | success | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | LLM 判 True：页面正在播放且有播放器 iframe、无订阅/付费限制，archive outcome 为正样本; INC37_NOTE: flagged_mismatch; L4[paywall(0.85) -> subscription_wall] |
| `T040__7b37c195__5c2c73cf` | calibration | no_play_control | no_play_control | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T014__acaa3ef1__5c2c73cf` | calibration | subscription_wall | subscription_wall | auto-validator-l3l4-v1-confirmed | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: confirmed; L4[paywall(0.85) -> subscription_wall] |
| `T011__443c5ec0__5c2c73cf` | regression | success | success | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | LLM 判 True：页面正在播放且有播放器 iframe、无订阅/付费限制，archive outcome 为正样本; INC37_NOTE: flagged_mismatch; L4[paywall(0.85) -> subscription_wall] |
| `T018__ad4a6065__5c2c73cf` | regression | success | success | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | LLM 判 True：页面正在播放且有播放器 iframe、无订阅/付费限制，archive outcome 为正样本; INC37_NOTE: flagged_mismatch; L4[paywall(0.85) -> subscription_wall] |
| `T025__7141c8de__5c2c73cf` | regression | success | success | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | LLM 判 True：页面正在播放且有播放器 iframe、无订阅/付费限制，archive outcome 为正样本; INC37_NOTE: flagged_mismatch; L4[paywall(0.85) -> subscription_wall] |
| `T028__d76e1085__5c2c73cf` | regression | success | success | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | LLM 判 True：页面正在播放且有播放器 iframe、无订阅/付费限制，archive outcome 为正样本; INC37_NOTE: flagged_mismatch; L4[paywall(0.85) -> subscription_wall] |
| `T029__0a800a3f__5c2c73cf` | regression | success | success | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | LLM 判 True：页面正在播放且有播放器 iframe、无订阅/付费限制，archive outcome 为正样本; INC37_NOTE: flagged_mismatch; L4[paywall(0.85) -> subscription_wall] |
| `T037__e33453ed__5c2c73cf` | holdout | success | success | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | LLM 判 True：页面正在播放且有播放器 iframe、无订阅/付费限制，archive outcome 为正样本; INC37_NOTE: flagged_mismatch; L4[paywall(0.85) -> subscription_wall] |
| `T031__de0c1df5__5c2c73cf` | holdout | success | success | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | LLM 判 True：页面正在播放且有播放器 iframe、无订阅/付费限制，archive outcome 为正样本; INC37_NOTE: flagged_mismatch; L4[paywall(0.85) -> subscription_wall] |
| `T031__3af4e21b__5c2c73cf` | holdout | no_play_control | no_play_control | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T033__53ce4b49__5c2c73cf` | holdout | no_play_control | no_play_control | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T012__8c050356__5c2c73cf` | holdout | subscription_wall | unresolved | manual-review | 2026-10-12T02:12:57+00:00 | llm | True | False | 是 |  |
| `T036__10313caf__5c2c73cf` | holdout | subscription_wall | subscription_wall | auto-validator-l3l4-v1-confirmed | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: confirmed; L4[paywall(0.85) -> subscription_wall] |

---

## 3 · `https://www.bilibili.com/bangumi/play/ep313572`

- **行数**：14
- **金标分布**：component_unverified 10 / subscription_wall 4
- **档位**：calibration / holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**14 行同一份正文（body_len 1531–1539、iframe=1、video=0），「会员」与「发生了错误」**在每一份正文里同时出现**。`subscription_wall` 4 条的证据原话是「当前正片明确标注『会员 粤语版』，**且**页面显示『发生了错误，请稍后再试』」；`component_unverified` 10 条据「播放器区域明确显示『发生了错误，请稍后再试』，且未检测到 video 标签」。⇒ 「要钱」与「播放器组件没验出来」是同一页面的两层事实，不是互斥。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `T012__8c050356__22e0fb36` | calibration | subscription_wall | unresolved | manual-review | 2026-10-12T02:12:57+00:00 | llm | True | False | 是 |  |
| `T014__acaa3ef1__22e0fb36` | calibration | component_unverified | login_wall_or_blocked | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | 页面为播放器错误/兼容性问题（播放组件不可用），非登录墙; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T029__0a800a3f__22e0fb36` | calibration | component_unverified | login_wall_or_blocked | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | 页面为播放器错误/兼容性问题（播放组件不可用），非登录墙; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T028__d76e1085__22e0fb36` | calibration | component_unverified | component_unverified | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T031__de0c1df5__22e0fb36` | calibration | component_unverified | login_wall_or_blocked | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | 页面为播放器错误/兼容性问题（播放组件不可用），非登录墙; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T040__c9982a1c__22e0fb36` | calibration | component_unverified | login_wall_or_blocked | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | 页面为播放器错误/兼容性问题（播放组件不可用），非登录墙; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T025__7141c8de__22e0fb36` | regression | component_unverified | login_wall_or_blocked | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | 页面为播放器错误/兼容性问题（播放组件不可用），非登录墙; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T033__53ce4b49__22e0fb36` | regression | subscription_wall | unresolved | manual-review | 2026-10-12T02:12:57+00:00 | llm | True | False | 是 |  |
| `T036__10313caf__22e0fb36` | regression | subscription_wall | unresolved | manual-review | 2026-10-12T02:12:57+00:00 | llm | True | False | 是 |  |
| `T040__7b37c195__22e0fb36` | regression | component_unverified | login_wall_or_blocked | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | 页面为播放器错误/兼容性问题（播放组件不可用），非登录墙; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T018__ad4a6065__22e0fb36` | holdout | subscription_wall | unresolved | manual-review | 2026-10-12T02:12:57+00:00 | llm | True | False | 是 |  |
| `T031__3af4e21b__22e0fb36` | holdout | component_unverified | login_wall_or_blocked | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | 页面为播放器错误/兼容性问题（播放组件不可用），非登录墙; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T037__e33453ed__22e0fb36` | holdout | component_unverified | component_unverified | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T011__443c5ec0__22e0fb36` | holdout | component_unverified | login_wall_or_blocked | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | 页面为播放器错误/兼容性问题（播放组件不可用），非登录墙; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |

---

## 4 · `https://cmgo.com/web/zh-CN/m/movie/cmgo655_%E5%8A%9F%E5%A4%AB`

- **行数**：13
- **金标分布**：not_play_site 10 / unreachable_hard 3
- **档位**：calibration / holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**`not_play_site` 10 条走判断点 ①（证据原话「页面标题主要表明其为观看电影所用 App 的影片页」，body_len=0 只见到标题）；`unreachable_hard` 3 条证据是传输层失败「导航失败（传输层）: obscura tool browser_navigate 失败: Error: Network error: navigation exceeded 30000ms deadline」。⇒ 一次是页面打开了但它只是 App 落地页，一次是页面根本没打开。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `T012__8c050356__e287b390` | calibration | not_play_site | not_play_site | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T014__acaa3ef1__e287b390` | calibration | not_play_site | not_play_site | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T031__3af4e21b__e287b390` | calibration | not_play_site | not_play_site | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T018__ad4a6065__e287b390` | calibration | not_play_site | not_play_site | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T011__443c5ec0__e287b390` | calibration | not_play_site | not_play_site | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T036__10313caf__e287b390` | regression | unreachable_hard | unreachable_hard | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | rule | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T037__e33453ed__e287b390` | regression | not_play_site | not_play_site | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T040__7b37c195__e287b390` | regression | unreachable_hard | unreachable_hard | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | rule | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T029__0a800a3f__e287b390` | holdout | not_play_site | not_play_site | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T040__c9982a1c__e287b390` | holdout | not_play_site | not_play_site | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T033__53ce4b49__e287b390` | holdout | not_play_site | not_play_site | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T028__d76e1085__e287b390` | holdout | not_play_site | not_play_site | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T031__de0c1df5__e287b390` | holdout | unreachable_hard | unreachable_hard | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | rule | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |

---

## 5 · `https://www.hao123.com/`

- **行数**：10
- **金标分布**：unresolved 7 / not_play_site 3
- **档位**：calibration / holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**两派判据层级不同。`not_play_site` 2 条走结构判据（src=rule，证据「候选 URL 的 path 为空或 `/`（https://www.hao123.com/）——是站点首页，结构上不可能是某部作品的播放页」；注意 E001__57bbf995 这条分支却是 `no_play_control`）；`unresolved` 7 条是无证据不判（证据「判断点 ① 未选中该站点，而模型未把它列入 rejected……**无证据不记负样本**」，body_len=0）。⇒ 一个说这个 URL 结构上不是播放页，一个说没证据所以不判——不是两种互斥结论。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `TPIN02__1cf5deca__910a8b86` | calibration | not_play_site | not_play_site | manual-review | 2026-10-12T04:01:21+00:00 | rule | False | False | 否 |  |
| `TPIN01__9c38dc2e__910a8b86` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T025__0d6d185e__910a8b86` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T035__3ab75598__910a8b86` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `TPIN02__1a5e526d__910a8b86` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T011__d1d70da2__910a8b86` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T001__ab5b6296__910a8b86` | regression | not_play_site | not_play_site | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | rule | False | False | 否 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `E001__57bbf995__910a8b86` | holdout | not_play_site | no_play_control | manual-review | 2026-10-12T04:01:21+00:00 | llm | False | False | 是 |  |
| `T013__dade3d10__910a8b86` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T001__67a05faa__910a8b86` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |

---

## 6 · `https://v.qq.com/x/cover/uiq0rxuywu508qr/o001241efrp.html`

- **行数**：9
- **金标分布**：no_play_control 5 / component_unverified 2 / blocked 1 / subscription_wall 1
- **档位**：calibration / holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**现场重采（camoufox，2026-10-23，两轮：`trajectory_pipeline/research/probes/m30_live_check.json` / `trajectory_pipeline/research/probes/m30_live_check_run2.json`；每站两读＝「导航一返回就观察」＋「等 4 秒再观察」）**：**两读差得极大**——第 1 轮 **81 字 → 3683 字**，第 2 轮 **4000 字 → 4000 字**（4000＝探针 `max_chars` 上限）。81 字那一读只有导航壳（「会员专区」），与存档 `T011__443c5ec0` 的 **84 字**逐字同形 ⇒ **存档那读是「观察时机太早」的半成品**，不是站点漂移。渲染完成后是完整页，原话：「该视频因版权方安全保密级别较高，**当前浏览器不支持加密播放**」「错误码：**70013080.14**」「下载PC客户端观看」，另有裸 CTA「开通会员」「新客VIP专享特惠！」。**仓库判据跑在现场正文上**：`paywall_evidence` ＝ **空串**（L1 门禁短语无命中；L2 CTA「开通会员」所在句单元里没有 试看/解锁/正片/全集/完整版/观看完整/看完/本片 任一语境词 ⇒ 裸 CTA；按 `BRANCH_PRECONDITION["subscription_wall"]`「挂着『开通会员』字样**不算**，那是导航栏广告」⇒ **不成立**）；`infer_access_defect` ＝ 空串（DRM 形态不在「地区/验证码/反爬/404」里）。⇒ **定性：漂移可解释**（9 行的多头各判各自那一读）。⇒ **裁定（该改成哪个标签）**：本行 `subscription_wall` **不成立**，真因是 **DRM 加密播放不可用**（70013080.14 ≠ 70017001.1 地区码）⇒ **应改 `component_unverified`**（到了播放页、组件不可用），**不是** `blocked`（`blocked` ＝ 地区限制/验证码/反爬）。⚠️ **改标未执行**（Q-11 已裁定「按访问」⇒ 不重标；v7 冻结档一个字不许改）⇒ 本条**交上游标注流程**。另 5 条 `no_play_control` 判在 81–84 字那一读上（同一病根：观察时机太早；渲染完成后有播放器 iframe=3）——**不在本轮 10 行范围内**，留给人核。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `T037__e33453ed__f9895792` | calibration | no_play_control | no_play_control | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T018__ad4a6065__f9895792` | calibration | no_play_control | no_play_control | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `P015__792b0ed7__f9895792__neg157` | calibration | blocked | blocked | ai-assisted-inc33 | 2026-10-13T20:00:00+08:00 | llm | False | False | 是 | INC33_NOTE: v.qq.com region 70017001 数字特征稳定识别，P033 真 obscura 跑 6/6 命中；现有 archive outcome 4 条（E021/E023/T024/T042）+ 负样本池 5 条同源。 |
| `T031__de0c1df5__f9895792` | calibration | component_unverified | component_unverified | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T031__3af4e21b__f9895792` | calibration | component_unverified | login_wall_or_blocked | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | 页面为播放器错误/兼容性问题（播放组件不可用），非登录墙; INC37_NOTE: flagged_mismatch; L4[blocked(0.85) -> blocked]; L3[blocked(0.85)] |
| `T036__10313caf__f9895792` | regression | no_play_control | no_play_control | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T040__c9982a1c__f9895792` | regression | no_play_control | no_play_control | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T040__7b37c195__f9895792` | holdout | no_play_control | no_play_control | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T011__443c5ec0__f9895792` | holdout | subscription_wall | login_wall_or_blocked | manual-review | 2026-10-12T02:12:57+00:00 | llm | False | False | 是 |  |

---

## 7 · `https://v.qq.com/error.html`

- **行数**：7
- **金标分布**：broken 6 / component_unverified 1
- **档位**：calibration / holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**7 行全是同一个错误页（证据原话「页面标题为『那条视频不见了-腾讯视频』，正文明确提示视频已不存在」）。`broken` 6 条与 `component_unverified` 1 条（T025__7141c8de，note 为 flagged_mismatch / `L3[broken(0.85)]`）讲的是同一个页面形态，只是判读归到了不同标签。⇒ error.html 是通用错误页 URL，同 URL 不同次落不同内容是站点行为。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `T029__0a800a3f__965be8ec` | calibration | broken | broken | auto-validator-l3l4-v1-weak-confirmed | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | INC33_NOTE: 同步 INC32 D10 偏差修缮（簇 2 unreachable_hard → broken）。INC32 仅改 v2/holdout/calibration.jsonl，没改 annotations.jsonl；v3 freeze 时需同步，否则 broken 标签丢失。; INC37_NOTE: weak_confirmed; L4[unknown(0.30) -> -]; L3[broken(0.85)] |
| `T014__acaa3ef1__965be8ec` | calibration | broken | broken | auto-validator-l3l4-v1-weak-confirmed | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | INC33_NOTE: 同步 INC32 D10 偏差修缮（簇 2 unreachable_hard → broken）。INC32 仅改 v2/holdout/calibration.jsonl，没改 annotations.jsonl；v3 freeze 时需同步，否则 broken 标签丢失。; INC37_NOTE: weak_confirmed; L4[unknown(0.30) -> -]; L3[broken(0.85)] |
| `T028__d76e1085__965be8ec` | calibration | broken | broken | auto-validator-l3l4-v1-weak-confirmed | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | INC33_NOTE: 同步 INC32 D10 偏差修缮（簇 2 unreachable_hard → broken）。INC32 仅改 v2/holdout/calibration.jsonl，没改 annotations.jsonl；v3 freeze 时需同步，否则 broken 标签丢失。; INC37_NOTE: weak_confirmed; L4[unknown(0.30) -> -]; L3[broken(0.85)] |
| `P033__973522fc__965be8ec` | regression | broken | broken | ai-assisted-inc33 | 2026-10-13T20:00:00+08:00 | llm | False | False | 是 | INC33_NOTE: 404/5xx 错误页内容损坏，evidence 显示明确的错误页内容。 |
| `T033__53ce4b49__965be8ec` | holdout | broken | broken | auto-validator-l3l4-v1-weak-confirmed | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | INC33_NOTE: 同步 INC32 D10 偏差修缮（簇 2 unreachable_hard → broken）。INC32 仅改 v2/holdout/calibration.jsonl，没改 annotations.jsonl；v3 freeze 时需同步，否则 broken 标签丢失。; INC37_NOTE: weak_confirmed; L4[unknown(0.30) -> -]; L3[broken(0.85)] |
| `T012__8c050356__965be8ec` | holdout | broken | broken | auto-validator-l3l4-v1-weak-confirmed | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | INC33_NOTE: 同步 INC32 D10 偏差修缮（簇 2 unreachable_hard → broken）。INC32 仅改 v2/holdout/calibration.jsonl，没改 annotations.jsonl；v3 freeze 时需同步，否则 broken 标签丢失。; INC37_NOTE: weak_confirmed; L4[unknown(0.30) -> -]; L3[broken(0.85)] |
| `T025__7141c8de__965be8ec` | holdout | component_unverified | component_unverified | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: flagged_mismatch; L4[unknown(0.30) -> -]; L3[broken(0.85)] |

---

## 8 · `https://www.ixigua.com/6532729724869804557?utm_source=baidu_lvideo`

- **行数**：7
- **金标分布**：unresolved 5 / login_wall_or_blocked 1 / trailer_only 1
- **档位**：calibration / holdout / regression（跨档）
- **定性**：不可能共存
- **裁定依据**：**现场重采（camoufox，2026-10-23，两轮：`trajectory_pipeline/research/probes/m30_live_check.json` / `trajectory_pipeline/research/probes/m30_live_check_run2.json`；每站两读＝「导航一返回就观察」＋「等 4 秒再观察」）**：两轮落定后**都是 626 字**，title「功夫 - 电影 - 高清完整版在线观看 - 西瓜视频」，**video=1**，原话：「**打开App看完整内容**」「**打开App观看**」「付费」「海量影视免费看」「打开西瓜视频，免费看海量影视」；第 2 轮的第一读落在跳转前（www.ixigua.com，**0 字**），落定后落到 `m.ixigua.com/video/…&wid_try=1` ⇒ **重定向没落定就观察 = 读到空页**。**仓库判据跑在现场正文上**：`paywall_evidence` ＝ **空串**（CTA「打开app观看」与语境词「完整内容」**被换行拆成两个句单元**——切句按换行/句读切）；`infer_access_from_evidence` ＝ **空串**（**正文里没有登录提示** ⇒ `login_wall_or_blocked` 不成立）；**没有预告控件** ⇒ `trailer_only` 不成立。两个标签**互斥**（一个说卡在登录/拦截，一个说只有预告、正片不存在），而两次访问**都没有正文**（存档 `TPIN02__1cf5deca` body=0、`TPIN02__1a5e526d` 根本没有这一页的观察）⇒ **没有「两次页面形态不同」这个解释** ⇒ **定性：不可能共存**。⇒ **裁定（该改成哪个标签）**：**两行都改**。页面事实是「**App 端独占 ＋ 付费导流**」⇒ 语义上落 **`subscription_wall`**；但**机器判据看不见它**——`PAYWALL_GATE_PHRASES` 注释里 m.ixigua 的已知形态是「以下内容需下载西瓜视频查看」，今天改成「打开App看完整内容」，词表**过期** ⇒ **词表缺口**（另开代码改动，本轮**不改词表**）。若不认「打开App看完整内容」与「打开app观看」等价 ⇒ 按 fail-closed **两行都落 `unresolved`**（不判比乱判好）——这是本条唯一的开放项。另 5 条 `unresolved`（模型未选中该链接、无 rejected 证据）是正确姿势，不动。⚠️ **改标未执行**（Q-11「按访问」⇒ 不重标）⇒ 本条**交上游标注流程**（台账 Q-03「m.ixigua 金标语义谁来复核」正是这条）。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `T011__d1d70da2__ff7185a5` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T035__3ab75598__ff7185a5` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `TPIN01__9c38dc2e__ff7185a5` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T025__0d6d185e__ff7185a5` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `TPIN02__1cf5deca__ff7185a5` | regression | trailer_only | unresolved | manual-review | 2026-10-12T04:01:21+00:00 | llm | False | True | 是 |  |
| `TPIN02__1a5e526d__ff7185a5` | holdout | login_wall_or_blocked | unresolved | manual-review | 2026-10-12T04:01:21+00:00 | llm | False | False | 否 |  |
| `T013__dade3d10__ff7185a5` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |

---

## 9 · `http://tv.sohu.com/v/MjAxNTAxMTkvbjQwNzg5MTgxOC5zaHRtbA==.html?txid=2501c78c3e10c8cdcbc7f701c70db188`

- **行数**：6
- **金标分布**：unresolved 5 / subscription_wall 1
- **档位**：holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**现场重采（camoufox，2026-10-23，两轮：`trajectory_pipeline/research/probes/m30_live_check.json` / `trajectory_pipeline/research/probes/m30_live_check_run2.json`；每站两读＝「导航一返回就观察」＋「等 4 秒再观察」）**：两轮**都是 489 字 → 1704/1709 字**，title「功夫 - 搜狐视频」，落定后 **video=1**，正文是**完整播放页**：进度条「00:00 / **01:39:26**」（正片时长）、「倍速」「客户端 1080P」「跳过片头片尾」「画面色彩调整」「分享」「评论1.3万」……⇒ **正片在、控件齐全**。页面里的「开通会员」出现在**「会员影院 / 新用户首月仅9.9元 / 院线大片 观看免广 / 热剧抢先看 / 顶级画质 / 开通会员」这个促销块**里，另有一批**周边片**标着「会员」（10间敢死队／捕风追影／默杀／独行月球）——**那不是正片的门禁**。**仓库判据跑在现场正文上**：`paywall_evidence` ＝ **空串**、`has_strong_paywall_signal` ＝ **False**（两轮正文都**没有**门禁语境词）。⚠️ 「您可以试看5分钟，点击开通会员」这句只在 2026-10-23 第一次临时重采里出现过一次，**两轮都复现不出来** ⇒ 它**不是稳定形态**，不能当判据。⇒ **定性：漂移可解释**（2026-10-12 那一读与今天的页面形态不同；按 Q-11「按访问」各自都是那一次的真事实）。⇒ **裁定（该改成哪个标签）**：本行 `subscription_wall` **在今天的页面形态下不成立**（正片可播、会员只是促销块与周边片标）⇒ **建议改 `unresolved`**（fail-closed：现场只证伪了「不是付费墙」，**没有真播过 ⇒ 证不出 `success`**）。若人认定 2026-10-12 那次访问确实是试看门 ⇒ 那就**保留**（按访问口径），但**当时那一读的正文/依据必须补上**（存档里根本没有这一页的任何观察，6 行全是「模型未选中该链接、无 rejected 证据」）。另 5 条 `unresolved` 是 fail-closed，不动。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `T013__dade3d10__f73ce362` | regression | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `TPIN01__9c38dc2e__f73ce362` | regression | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `TPIN02__1a5e526d__f73ce362` | holdout | subscription_wall | unresolved | manual-review | 2026-10-12T04:01:21+00:00 | llm | False | False | 否 |  |
| `T025__0d6d185e__f73ce362` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T035__3ab75598__f73ce362` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T011__d1d70da2__f73ce362` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |

---

## 10 · `http://v.youku.com/v_show/id_XMTQwNzMzMTc1Mg==.html?source=baidu&refer=sousuotoufang_market.qrwang_00002944_000000_QJFFvi_19031900`

- **行数**：6
- **金标分布**：unresolved 5 / login_wall_or_blocked 1
- **档位**：calibration / holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**现场重采（camoufox，2026-10-23，两轮：`trajectory_pipeline/research/probes/m30_live_check.json` / `trajectory_pipeline/research/probes/m30_live_check_run2.json`；每站两读＝「导航一返回就观察」＋「等 4 秒再观察」）**：两轮落定后**都是 1922 字**，title「功夫-电影-高清完整正版视频在线观看-优酷」，iframe=1、**video=0**，原话：「**由于安全原因，请 登录 后继续观看**」「登录」「登录/注册」「立即开通」「SVIP会员10天后到期」「续费享首月18元」「立即续费」「正片」「VIP」（周边视频区有两条「预告」——**是周边视频，不是预告控件**）。⇒ **页面有登录门原话**（要求登录才能继续观看）⇒ 本行 `login_wall_or_blocked` **在语义上站得住**。**仓库判据跑在现场正文上**：`infer_access_from_evidence` ＝ **空串**、`access_evidence(kind='login')` ＝ **空串**——**尽管页面明写「请 登录 后继续观看」**，因为词表是「请登录」「登录后继续」而**页面里这两个词中间夹了空格** ⇒ **归一化缺口**（词表/归一化，不是金标错）。`paywall_evidence` ＝ 空串（裸「续费」CTA 独行，句单元里没有语境词）。⇒ **定性：漂移可解释**（导航型「登录」 vs 门禁型「登录后继续观看」两层事实；周边的「预告」层）。⇒ **裁定（该改成哪个标签）**：**保留 `login_wall_or_blocked`**（现场登录门原话在），把裁定依据换成上面那句原话；**机器判据认不出它**（夹空格）⇒ 要机器复现这一形态，得改 `ACCESS_HINTS` 的归一化（中文之间的空格）——**属代码侧另开一条**，本轮不动词表。⚠️ 若人认为「由于安全原因，请登录后继续观看」属**风控/反爬**而非登录墙 ⇒ 那应改 `blocked`（同属合并态）——**合并态内改标签不影响本节的定性**。另 5 条 `unresolved` 不动。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `TPIN02__1a5e526d__db4899cd` | calibration | login_wall_or_blocked | unresolved | manual-review | 2026-10-12T04:01:21+00:00 | llm | False | False | 否 |  |
| `T035__3ab75598__db4899cd` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T013__dade3d10__db4899cd` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T025__0d6d185e__db4899cd` | regression | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `TPIN01__9c38dc2e__db4899cd` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `T011__d1d70da2__db4899cd` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 否 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |

---

## 11 · `https://v.qq.com/x/cover/o9ltjk5yc26z6w9/d0013gg2l63.html`

- **行数**：5
- **金标分布**：blocked 4 / subscription_wall 1
- **档位**：calibration / holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**5 行共用同一份正文（body_len 1320–1352、iframe=5、video=0），正文里**同时**有「当前设备不支持播放 / 错误码 70017001.1」**和**「VIP / 开通 / 特惠」。`blocked` 3 条据「播放器显示『当前设备不支持播放』，并明确报错『错误码：70017001.1』，属于地区限制」；`subscription_wall` 1 条（T024__b54092ee）据「所选集标注为 VIP、要求开通会员」——同一次访问的两层。本仓库既有口径判地区码优先（`rule_perceptor.py` / `d5_scorer.py`：「『当前设备不支持播放』+ 70017001.1 地区限制，『开通会员』只是句尾附加」）。⇒ 页面不自相矛盾，是两派各取一层。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `T024__b54092ee__10990df1` | calibration | subscription_wall | subscription_wall | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: flagged_mismatch; L4[blocked(0.85) -> blocked]; L3[blocked(0.85)] |
| `T024__6240da8e__10990df1` | calibration | blocked | blocked | ai-assisted-inc33 | 2026-10-13T20:00:00+08:00 | llm | False | False | 是 | INC33_NOTE: v.qq.com region 70017001 数字特征稳定识别，P033 真 obscura 跑 6/6 命中；现有 archive outcome 4 条（E021/E023/T024/T042）+ 负样本池 5 条同源。 |
| `P001__2e0cec8b__10990df1__neg153` | calibration | blocked | blocked | ai-assisted-inc33 | 2026-10-13T20:00:00+08:00 | llm | False | False | 是 | INC33_NOTE: v.qq.com region 70017001 数字特征稳定识别，P033 真 obscura 跑 6/6 命中；现有 archive outcome 4 条（E021/E023/T024/T042）+ 负样本池 5 条同源。 |
| `P033__9491c34b__10990df1` | regression | blocked | blocked | ai-assisted-inc33 | 2026-10-13T20:00:00+08:00 | llm | False | False | 是 | INC33_NOTE: v.qq.com region 70017001 数字特征稳定识别，P033 真 obscura 跑 6/6 命中；现有 archive outcome 4 条（E021/E023/T024/T042）+ 负样本池 5 条同源。 |
| `P015__3fda4d75__10990df1__neg158` | holdout | blocked | blocked | ai-assisted-inc33 | 2026-10-13T20:00:00+08:00 | llm | False | False | 是 | INC33_NOTE: v.qq.com region 70017001 数字特征稳定识别，P033 真 obscura 跑 6/6 命中；现有 archive outcome 4 条（E021/E023/T024/T042）+ 负样本池 5 条同源。 |

---

## 12 · `https://v.qq.com/x/cover/8lvnb7aiio1crpd/i002515xdcm.html`

- **行数**：4
- **金标分布**：blocked 3 / subscription_wall 1
- **档位**：calibration / holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**4 行共用同一份正文（body_len 1399–1432、iframe=5、video=0），「当前设备不支持播放 / 错误码 70017001.1」与「开通会员 / VIP」同时在上面。`blocked` 2 条据「明确显示『错误码：70017001.1』，属于地区限制」；`subscription_wall` 1 条（E021__94a1392f）证据原话「页面显示『当前设备不支持播放』且错误码70017001.1，**同时**该集标注『VIP』并提示『开通会员』」。⇒ 同页两层，不是互斥。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `E021__129c3fae__13837008` | calibration | blocked | blocked | ai-assisted-inc33 | 2026-10-13T20:00:00+08:00 | llm | False | False | 是 | INC33_NOTE: v.qq.com region 70017001 数字特征稳定识别，P033 真 obscura 跑 6/6 命中；现有 archive outcome 4 条（E021/E023/T024/T042）+ 负样本池 5 条同源。 |
| `E021__94a1392f__13837008` | regression | subscription_wall | subscription_wall | manual-review | 2026-10-12T04:01:21+00:00 | llm | True | False | 是 |  |
| `P033__f65b179e__13837008` | regression | blocked | blocked | ai-assisted-inc33 | 2026-10-13T20:00:00+08:00 | llm | False | False | 是 | INC33_NOTE: v.qq.com region 70017001 数字特征稳定识别，P033 真 obscura 跑 6/6 命中；现有 archive outcome 4 条（E021/E023/T024/T042）+ 负样本池 5 条同源。 |
| `P001__e5b60ff7__13837008__neg152` | holdout | blocked | blocked | ai-assisted-inc33 | 2026-10-13T20:00:00+08:00 | llm | False | False | 是 | INC33_NOTE: v.qq.com region 70017001 数字特征稳定识别，P033 真 obscura 跑 6/6 命中；现有 archive outcome 4 条（E021/E023/T024/T042）+ 负样本池 5 条同源。 |

---

## 13 · `https://m.chusuiwenhua.com/bq/2te.html`

- **行数**：3
- **金标分布**：unreachable_hard 2 / unresolved 1
- **档位**：calibration / holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**现场重采（camoufox，2026-10-23，两轮：`trajectory_pipeline/research/probes/m30_live_check.json` / `trajectory_pipeline/research/probes/m30_live_check_run2.json`；每站两读＝「导航一返回就观察」＋「等 4 秒再观察」）**：**两轮导航都失败**——`Page.goto: NS_ERROR_NET_RESET`（`load` 与 `domcontentloaded` 各试一次）⇒ 站点**连接被重置**；存档那两读是浏览器自己的错误页（title「**网页无法访问**」、正文 6 字）。⇒ 站点**确实不可达**，两条 `unreachable_hard` **成立**；行内 evidence 写「采集降级（['body_text']），证据不完整，不判」是**采集层没拿到正文**的降级说明，不是「站点可达」的否定 ⇒ 与标签不矛盾。⇒ **定性：漂移可解释**。⇒ **裁定（该改成哪个标签）**：两条 `unreachable_hard` **保留不改标**，把裁定依据换成「现场两轮导航均 `NS_ERROR_NET_RESET`；存档那读是浏览器错误页『网页无法访问』」；第三条 `unresolved` 是 fail-closed，不动。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `T025__0d6d185e__bf4b6ae1` | calibration | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `TPIN02__1a5e526d__bf4b6ae1` | regression | unreachable_hard | unresolved | manual-review | 2026-10-12T04:01:21+00:00 | llm | False | True | 是 |  |
| `TPIN02__1cf5deca__bf4b6ae1` | holdout | unreachable_hard | unresolved | manual-review | 2026-10-12T04:01:21+00:00 | llm | False | True | 是 |  |

---

## 14 · `https://m.ixigua.com/video/6582085495839261192?utm_source=sogou_lvideo&utm_medium=sogou_referral&utm_campaign=cooperation`

- **行数**：3
- **金标分布**：subscription_wall 2 / no_play_control 1
- **档位**：calibration / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**3 行同一份正文（唯一可交互控件是评论按钮「去抢沙发」，无 video / iframe）。`no_play_control` 1 条据「页面仅有『去抢沙发』按钮，没有可开始播放的控件」；`subscription_wall` 2 条据「页面仅显示影片信息，并明确提示『付费』『打开App观看』，且没有 video 或 iframe，未呈现可播放内容」。⇒ 这页同时是「没有网页播放控件」和「提示付费」，两层事实并存；两条人工金标（TPIN02__1cf5deca / T025__0d6d185e）note 空，但证据里确有「付费」字样。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `TPIN02__1a5e526d__417199e7` | calibration | no_play_control | no_play_control | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |
| `TPIN02__1cf5deca__417199e7` | calibration | subscription_wall | login_wall_or_blocked | manual-review | 2026-10-12T02:12:57+00:00 | llm | False | False | 是 |  |
| `T025__0d6d185e__417199e7` | regression | subscription_wall | login_wall_or_blocked | manual-review | 2026-10-12T02:12:57+00:00 | llm | False | False | 是 |  |

---

## 15 · `https://www.iqiyi.com/v_19rrk40at8.html?vfm=2008_aldbd&fc=828fb30b722f3164&fv=p_02_01`

- **行数**：3
- **金标分布**：subscription_wall 2 / unresolved 1
- **档位**：calibration / holdout（跨档）
- **定性**：漂移可解释
- **裁定依据**：**现场重采（camoufox，2026-10-23，两轮：`trajectory_pipeline/research/probes/m30_live_check.json` / `trajectory_pipeline/research/probes/m30_live_check_run2.json`；每站两读＝「导航一返回就观察」＋「等 4 秒再观察」）**：**两轮都是「立刻读 6 字 → 等 4 秒 471/475 字」**——立刻读只有「精彩即将开始」＋爱奇艺**通用标题**，等 4 秒后渲染出影片页：title「**功夫（粤语）-电影-完整版视频在线观看-爱奇艺**」、「功夫（粤语）**100分钟**」「详情」「9.1」「分」「2004年」「加追」「下载」「分享」，另有会员位「**黄金会员首月低至12元**」「新朋友，开通享精彩内容」「**开通会员**」「黄金关闭此广告」「**客户端流畅播放**」。⇒ **「取不到正文」是观察时机问题，不是站点问题** ⇒ 原「待查」**解除**。**仓库判据跑在现场正文上**：`paywall_evidence` ＝ **空串**（CTA「开通会员」独行；语境词「完整版」只在 **title** 里，而 `paywall_evidence` **只看 body**）⇒ **词表缺口**（不看 title）。⇒ **定性：漂移可解释**（两条 `subscription_wall` ＋ 一条 `unresolved` **可以同时成立**：有的访问读到了正文判了墙，有的访问没读到正文落 fail-closed）。⇒ **裁定（该改成哪个标签）**：两条 `subscription_wall` **保留**（影片页在、会员位在、「客户端流畅播放」在；且与 2026-10-12 首轮人判「有资源，但要登录付费才能看」、2026-10-10 口径「有播放页需付费不算成功」一致），把裁定依据换成上面这组现场原话。⚠️ **第三次人判（2026-10-15 `agent-inc41`）给的是 `player_unverified`**（「爱奇艺播放页 URL + video_tag=1，body 采集降级无法核实是否《功夫》及可播性」）——**但 `player_unverified` 不是负样本**（不该进池），且按 Q-11「按访问」两个访问各自贴各自的标签就是允许的 ⇒ **本轮不改标**。第三条 `unresolved` 是 fail-closed，不动。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `TPIN02__1cf5deca__ad5971d1` | calibration | subscription_wall | unresolved | manual-review | 2026-10-12T04:01:21+00:00 | llm | False | True | 是 |  |
| `TPIN02__1a5e526d__ad5971d1` | calibration | subscription_wall | unresolved | manual-review | 2026-10-12T04:01:21+00:00 | llm | False | True | 是 |  |
| `T025__0d6d185e__ad5971d1` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | True | 是 | 采集降级/零交互元素，evidence 不完整，保持 fail-closed; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |

---

## 16 · `https://tv.cctv.com/2014/07/27/VIDE1406399770522952.shtml?srcfrom=baidualading&event2=bdtg_pc_hkafjzpq`

- **行数**：2
- **金标分布**：success 1 / unresolved 1
- **档位**：calibration / holdout（跨档）
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**两次**不同的运行**（`T001__ab5b6296` 与 `T001__67a05faa`）落同一个 URL。calibration 那条金标 `success`：正文命中「正在播放《武林外传》第1集」、iframe=1、video=1、`reached_play_page=true`；holdout 那条金标 `unresolved`：`code.evidence` 是「判断点 ① 未选中该站点……**无证据不记负样本**」，审核人是 auto-validator `no_text_signal`（**不是人工改标**，note 非空）。⇒ 一次采到了、一次模型压根没选中这个链接——两次之间页面没有矛盾。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `T001__ab5b6296__eed8ef5e` | calibration | success | success | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | LLM 判 True：页面正在播放且有播放器 iframe、无订阅/付费限制，archive outcome 为正样本; INC37_NOTE: flagged_mismatch; L4[paywall(0.85) -> subscription_wall] |
| `T001__67a05faa__eed8ef5e` | holdout | unresolved | unresolved | auto-validator-l3l4-v1-no-text-signal | 2026-10-14T01:11:41+00:00 | llm | False | False | 是 | 模型未选中该链接、无 rejected 证据，fail-closed 无证据不判; INC37_NOTE: no_text_signal; L4[unknown(0.30) -> -] |

---

## 17 · `https://v.qq.com/x/cover/mzc00100cecmyc8.html`

- **行数**：2
- **金标分布**：blocked 1 / subscription_wall 1
- **档位**：holdout / regression（跨档）
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**2 行共用同一份正文（body_len 1211–1252、iframe=5、video=0）。`blocked` 1 条（P033__8b4287d0）据「页面主体已加载，但视频播放受到地区限制」；`subscription_wall` 1 条（E023__4d175424）证据原话「播放器提示『当前设备不支持播放』并显示错误码70017001.1，**且**页面明确标注『新客VIP专享特惠』『开通会员』」。⇒ 同一次访问同时命中两层信号。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `P033__8b4287d0__7754b3d2` | regression | blocked | blocked | ai-assisted-inc33 | 2026-10-13T20:00:00+08:00 | llm | False | False | 是 | INC33_NOTE: v.qq.com region 70017001 数字特征稳定识别，P033 真 obscura 跑 6/6 命中；现有 archive outcome 4 条（E021/E023/T024/T042）+ 负样本池 5 条同源。 |
| `E023__4d175424__7754b3d2` | holdout | subscription_wall | subscription_wall | manual-review | 2026-10-12T04:01:21+00:00 | llm | True | False | 是 |  |

---

## 18 · `https://v.qq.com/x/cover/zr5a67l333ehzu9/d0032x85stp.html`

- **行数**：2
- **金标分布**：blocked 1 / subscription_wall 1
- **档位**：holdout
- **定性**：漂移可解释
- **裁定依据**：**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**2 行共用同一份正文（body_len 1390–1393、iframe=5、video=0）。`blocked` 1 条（P033__6beb8e05）据「视频播放被特定地区错误码拦截」；`subscription_wall` 1 条（E017__a2878b23）证据原话「页面明确显示『新客VIP专享特惠』和『开通会员』，**且**播放器提示『当前设备不支持播放』并报错70017001.1」（该行分支是 `unresolved`，带记账降级说明）。⇒ 同页两层，不是互斥。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `E017__a2878b23__7fd1a252` | holdout | subscription_wall | unresolved | manual-review | 2026-10-12T02:12:57+00:00 | llm | True | False | 是 |  |
| `P033__6beb8e05__7fd1a252` | holdout | blocked | blocked | ai-assisted-inc33 | 2026-10-13T20:00:00+08:00 | llm | False | False | 是 | INC33_NOTE: v.qq.com region 70017001 数字特征稳定识别，P033 真 obscura 跑 6/6 命中；现有 archive outcome 4 条（E021/E023/T024/T042）+ 负样本池 5 条同源。 |

---

## 19 · `https://v.qq.com/x/page/s3237561yvx.html`

- **行数**：2
- **金标分布**：subscription_wall 1 / trailer_only 1
- **档位**：calibration / holdout（跨档）
- **定性**：漂移可解释
- **裁定依据**：**现场重采（camoufox，2026-10-23，两轮：`trajectory_pipeline/research/probes/m30_live_check.json` / `trajectory_pipeline/research/probes/m30_live_check_run2.json`；每站两读＝「导航一返回就观察」＋「等 4 秒再观察」）**：两轮都是「立刻读 100/86 字 → 等 4 秒 484/533 字」——立刻读只有导航壳（「会员专区」），落定后是 VIP 页：「开通会员」「新客VIP专享特惠！」「新客特惠开VIP」「好剧热综持续更新，会员畅享精彩内容」「登录」「登录参与讨论」。存档那一读（`TPIN02__1cf5deca`，**945 字**）是**另一种形态**：「**当前设备不支持播放**」×2 ＋「**70017001**」×2 ＋「错误码」×2 ⇒ **地区限制**；现场这两轮**没有复现出 70017001**（出口 IP / 地区不同就会不同，这是真事实）。⇒ 同一 URL 的两次访问是**两种页面形态**：地区限制态 vs VIP 短片段 ⇒ **定性：漂移可解释**。**仓库判据跑在现场正文上**：`paywall_evidence` ＝ **空串**（裸「开通会员」＋无同句观看语境）⇒ `subscription_wall` **不成立**；`classify_trailer` 要求控件文本含 预告片/预告/抢先看/片花/片段/试看片段，而页面正文里的「预告」是**周边视频区**的 ⇒ **机器判不出 `trailer_only`**（词表缺口）。⇒ **裁定（该改成哪个标签）**：本行 `trailer_only` **保留**——它自己那一读的存档正文写着「当前设备不支持播放」＋70017001.1 ⇒ **正片确实不可得**（「只有预告片、播放资源不存在」的语义成立；原因是地区限制，不是「只有预告」）。⚠️ **同一 URL 的另一行 `T025__0d6d185e`（`subscription_wall`）已被现场证伪**（裸 CTA；它自己的 note 就写着 `L4[blocked(0.85) -> blocked]`，真因是地区限制 ⇒ 应改 `blocked`）——但**那一行不在本轮 10 行范围内**（金标＝代码分支、note 非空）⇒ **本轮不改标**，只把标签存疑记在这里。

| golden_id | 档位 | 金标 | 代码分支 | 审核人 | 审核时间 | code.source | reached_play_page | fallback_used | 在盘上 | 金标备注 |
|---|---|---|---|---|---|---|---|---|---|---|
| `TPIN02__1cf5deca__3dcdbc14` | calibration | trailer_only | subscription_wall | manual-review | 2026-10-12T04:01:21+00:00 | llm | True | False | 是 |  |
| `T025__0d6d185e__3dcdbc14` | holdout | subscription_wall | subscription_wall | auto-validator-l3l4-v1-flagged-mismatch | 2026-10-14T01:11:41+00:00 | llm | True | False | 是 | evidence 与分支定义一致，保持自动分支; INC37_NOTE: flagged_mismatch; L4[blocked(0.85) -> blocked]; L3[blocked(0.85)] |

---
