# TODO 台账 · 全域未闭环项汇总

> **这一份是 TODO 的唯一汇总入口。** 只收**未闭环**的事；已经闭环的留在
> 各域文档与编年史里，**不进台账**——台账里出现已闭环项，就说明它开始骗人了。
>
> **快照日期：2026-10-20**（2026-10-21 补记 H-08 / Q-05 / M-19 / M-21 / L-12）。数据来源 =
> [00-总览](00-总览.md) 的优先级表
> + 各域文档的「⚠️ 本域已知未闭环项」+ [90-增量编年史](90-增量编年史.md)
> 增量五十七～六十九 + 代码实测（`git ls-files` / `wc -l` / 各条对应的源码行）。
>
> **每条一行，四栏**：`事项` / `记录锚点`（证据在哪，点进去能核对）/ `现在差什么`。
> **不带锚点的 TODO 不收**——「记得还有件事」这种条目没人能复核，等于没记。

---

## 怎么用这份台账

| 你的目的 | 看哪 |
|---|---|
| 找下一件该干的活 | §高 → §中，按 `00-总览` 的「阻塞程度 × 成本」排 |
| 怀疑某条已经做完了 | 顺着「记录锚点」进域文档 / 编年史 / 源码行核对 |
| 想知道某条为什么还没做 | 锚点那条下面通常写着「为什么没动」 |
| 想知道能不能做 | §⛔ 不能做——**试之前先看，别重蹈** |
| owner 要拍板什么 | §待 owner 拍板 |
| 数字过期了 | §台账自身的保质期，重跑那几条命令 |

**判定「闭环」的标准**（三条都满足才算）：

1. 代码里能指到实现（文件 + 行）；
2. 有测试封着它（测试名能说出来，且**真的会跑**，不是 skip）；
3. 有记录（编年史增量号 或 域文档的「已闭环 + 判定依据」）。

只满足 1 的是「写完了」，不叫闭环——v1 的 `regulation` 与 `usage_prune` 都栽在这上面。

---

## 高 · 阻塞主线指标

| ID | 事项 | 域 | 记录锚点（证据） | 现在差什么 |
|---|---|---|---|---|
| **H-01** | `unresolved` 剩余桶重新归因（原 36/79 = 46%） | 01 采集 / 02 感知 | [01 §未闭环](01-采集执行域.md#本域已知未闭环项) · [02 §INC44 分层](02-感知与判定域.md) · 编年史 §增量四十四 | 簇 C 已被**增量六十·方案 X** 修掉（净修 46 行，[`branches.py:593`](../../executor/branches.py#L593)），**旧的 46% 已不作数，必须重测**才能知道剩下的是谁 |
| **H-02** | D-10 代码层 vs 黄金集一致率 76.5% → ≥90% | 07 评估 | [07 §D-10](07-评估与黄金集域.md) · [00 §四条主线](00-总览.md#一句话现状) | 缺 13.5pp；簇 1 type A（component_unverified 漏 VIP 强信号）+ type C（collection fail 8 条），要 **LLM prompt 改善 + 重采集两条路都跑** |
| **H-03** | mojibake（charset）防护**从未被真实站点触发** | 01 采集 / 05 rationale | [05 §mojibake 防护](05-rationale与闸门域.md) · 编年史 §增量四十五 · `research/probes/inc45_probe_sohu.py` | 防护代码已落、变异测试封着，但**没有一次真实命中**。证伪动作：跑 `tv.sohu.com` 真采一次 |
| **H-04** | JS-heavy 站真渲染失败（iqiyi；w.mgtv / sohu 反爬时变） | 01 采集 | [01 §窗口与候选集合](01-采集执行域.md) · [02 §INC43 探针](02-感知与判定域.md) · `research/probes/inc43_probe_bodies.jsonl` | 15 行判对但仍 `unresolved`，属**采集层能力边界**，不是判定规则能救的 |
| **H-05** | 31 条 `flagged_mismatch` **进不了 L6 复核队列** | 04 探针 / 08 存档 | [04 §未闭环](04-探针七层域.md#本域已知未闭环项) · [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | ⚠️ **不是「触发器没写」**：`dispatch_l6`（INC36）已在，挂在两处主循环末尾（[`review_queue.py:842`](../../executor/review_queue.py#L842)）。缺的是**派发条件**——只认 L5 反思档与 fail-closed 兜底，`auto-validator-l3l4-v1-flagged-mismatch` 桶**不在其中**（该串在 `review_queue.py` 零命中，2026-10-20 核） |
| **H-06** | 判断点 ② 让错误页通过（bilibili「发生了错误」判 True） | 02 感知 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) · 避坑指南 §5 类 | **未修且是设计题**：没有可靠的代码层事实能区分「错误页」与「正常页」。要么改 ② 的输出契约强调错误页，要么接受 ③ 兜住它（现状） |
| **H-07** | `partial_free` / `login_required` 黄金集**各 0 条** | 03 标签 / 07 评估 | [03 §MIN_PER_BRANCH](03-标签与分支域.md) · [07 §未闭环](07-评估与黄金集域.md) | a 路径已证实跑不出（INC33）；b 路径追溯 `login_wall_or_blocked` **17 条**待做 |
| **H-08** | D-11 camoufox 执行层替换：**P-1/P0/P1/P2/P3/P4 已落地；P5 机制已落地但判据未过 → 未切换；执行口径已定为 headless（2026-10-21）；P6 未开工**（含 **D12** 动作词汇表对齐） | 01 采集 / 跨层 | [设计方案/04-camoufox](../设计方案/04-camoufox执行层替换方案.md) §6 落地阶段 | 方案定稿、**P-0 起一个没动**，**生产链仍跑 obscura**。**P-1 两段都跑完**（探针 `executor/browser/probe_f5_name.py`）：离线段（[§1.5](../设计方案/04-camoufox执行层替换方案.md)）role 侧零落差、朴素链 65% 且图标按钮产出空 name（`get_by_role(name="")` → `count == 0` **不报错**，与「元素不存在」同形）；**真实页面段（[§1.6](../设计方案/04-camoufox执行层替换方案.md)，6 URL / 240 行）判据达标**——权威 name **91/91 = 100%**、JS+伪元素 96.7%、纯 JS 87.9%。⚠️ **D12 生死门已过，但实现口径被这次实测改了两处**：① 采集面 **name 取 `aria_snapshot`，不许自己算**；② 枚举**先过可见性**（41% 元素没有可访问名，其中 90/97 不可见）+ 定位器**钉 `exact=True`**（默认 substring 口径下命中率 100%，判据会变空）。**P0 已落地**（`driver_factory.py`：6 处硬编码 `ObscuraDriver(client)` 收敛到 `make_driver`，零行为变化；AST 门禁盯第 7 处）。⏳ **P0 的「存档逐字节同形」没验**——P0 当时本机缺 `OBSCURA_EXE`（后来配上了，基线存档得另找）。**P1 已落地（2026-10-20）**：`camoufox_session.py`（一个 Run 一次 launch，钉死 `os`/`screen`/`window`/`humanize`/`geoip` 关/`main_world_eval` 关/排除 uBO，**回读实测指纹**）+ `camoufox_driver.py`（只落 `goto`/`snapshot`/`count`/`evaluate`/`close`，其余原语**抛 `NotImplementedError` 不返回空**）；`check --driver camoufox` 可探并打印读数，**`DEFAULT_KIND` 仍是 obscura，生产链零变化**。实测两处要注意（[§4.3.1](../设计方案/04-camoufox执行层替换方案.md)）：`screen`/`dpr` 钉得住，但 **`window=` 钉的是外框**——8 次实测内宽恒 `1905`、**内高在 `940`/`948` 间摆动**（5:3）→ 批次元数据只信实测值；真要钉死视口得改 `new_context(viewport=…)` **并重验指纹自洽**，P5 前定。§6.1 反爬拦截率仍是切换的 go/no-go 门槛，**不达标就不换生产链**。**D12 的「声明侧」已落地**（2026-10-20，增量六十四）：工具面轴（`obscura-1` / `camoufox-1`）进 `toolcatalog`，面由 P1 的 `driver` 一对一折算；**P3-α 已落地（2026-10-20）**：`camoufox_driver.interactive()` 一页一次 `page.aria_snapshot()`（51ms/页，逐元素方案 200 元素要 4.8s），role+name 全取 playwright 结论，三类丢弃（词表外 role / 无名字 / 真同名整组）记进 `last_enumeration`；纯解析器 `browser/aria_snapshot.py` 离线可测。`check --driver camoufox` 打元素表+账目（`example.com` kept=1 / `baidu` nodes=101→kept=34）。⚠️ 三处**已知偏离**记在 [§4.5.6](../设计方案/04-camoufox执行层替换方案.md)：可见性未逐元素过滤（5.8ms×N）、`tag` 装的是 ARIA role（下游两处词表已放宽成并集，P4 收窄）、新发现 `{role,name}` **只在同一次枚举内有效**（`textbox` 的名字可能是动态占位词）。**P2 动作面已落地（2026-10-20，[§4.6](../设计方案/04-camoufox执行层替换方案.md)）**：`click`/`fill`/`press` 三个原语 + **协议改名**（`type_text`→`fill`、`press_key`→`press`，语义层与驱动层同名，`agent_loop` 里那张翻译表已删）；三态**先 `count()` 再操作**（0 → 静默 `not_found`、>1 → `ambiguous` 不动作、`count` 报错 → `DriverError`），定位钉 `exact=True`，**跨面参数抛 `ValueError`**（`DriverError` 会被 `steps/aggregator` 吃成 `unreachable_hard`，接线 bug 就变成理由通顺的负样本）。真浏览器冒烟三原语全 `ok`（`check --action-smoke`）。★**新事实**：`role+name` 是**页面级**定位，**导航后不失效而是可能静默点中新页同名元素**（对照 `ref` 必然报错）→ **任何导航之后必须重新枚举**。★**动作超时被实测改写**：`ACTION_TIMEOUT_MS=5000` 让一半点击超时（`Locator.click: Timeout 5000ms exceeded`），现场取证证明元素在/可见/可用/包围盒稳定、`elementFromPoint` 就是它，而 `click(no_wait_after=True)` 只要 1.1s、`goto` 同址 0.3s——**9–13s 是 playwright 在等「这次点击触发的导航完成」**。处置：`click`/`press` 关掉 `no_wait_after`，之后 `_settle()` 有界等 `domcontentloaded`（2s，`NAV_SETTLE_TIMEOUT_MS`），结果记 `last_action["settle"]`=`ok`/`timeout`（等不到不抛但要说出来）。修后连跑 4 次 **0 超时**；但 2/4 点击后 2s 内 URL 未变（对照探针 `goto` 路径 3/3 在 0.4s 内跳）→ **「点了但没跳」是站点行为，未定论**，`ok` 只代表调用成功。**P3-β 已落地（2026-10-20，[§4.7](../设计方案/04-camoufox执行层替换方案.md)）**：`links`/`extract`/`new_tab`/`close_tab`/`observe` 全部落地，**协议原语齐了**，`_todo` 已删。三条被实测改写的口径：**标签页做成栈**（一个 `page` 字段「开 tab → 观察 → 关 tab → 回中间页」无处可回；只剩一页时先补空白页、开页失败收掉新页——真浏览器四条语义全对）· **`links` 按 href 去重**（obscura 的 `browser_links` 恒 `total==distinct`（131/131、33/33），不去重时 camoufox `links_total` 高 28–37%，而它要进 D-2 口径）· **`extract` 取文本 `innerText‖textContent`**（bing 结果页 `li.b_algo h2 a` 的 10 个锚 **`innerText` 全空**，只取它 → 「抽到 10 个空标题」→ ① 退回 fail-closed，看着像选择器失效）。★**两面 `Observation` 已并排测**（`probe_observe --driver {obscura,camoufox}`，同脚本同契约断言）：`url`/`title`/`body`/`links`/`video`/`iframe`/`degraded` 全部对齐且两边契约全绿，但 **`elements_total` 74 vs 34（−54%）**、`raw_len` 含不含快照、`links[].text` 面包屑 vs 锚文本——**三处不同义**，故 P2 后半条判「已测量」而非「同口径」。★**`search` 标题抽取已逐条对齐**（`probe_result_titles --driver …`：bing 两边 `10/10` 对、baidu 两边 `8/8` 对且 8 条含片名）；顺带推翻一句旧记录：两边 `@href` 今天都返回**包裹形态** `baidu.com/link?url=…`，`steps/search.title_for` 里「obscura 给解包后的 URL」那句是另一种页面变体上的旧测量。**P4 契约迁移已落地（2026-10-20，[§4.8](../设计方案/04-camoufox执行层替换方案.md)，增量六十七）**：`element` 的键名不再全局手抄，改成**按方言投影**——落盘/进 prompt 时由 `common/elements.py` 的 `ELEMENT_KEYS` 一对一折算（obscura-1 面 `{tag,label}`、camoufox-1 面 `{role,name}`），**槽位统一（role/name）、值域不统一（html_tag vs aria_role）、不互译、不混池**；`Observation.element_vocabulary` 由驱动实现自己置，replay 从存档键反推（`vocabulary_of_archive_elements`），perceptor 词表按面收窄（**认不出就抛**，不回落）。20 条契约测试封着（`tests/contract/test_element_dialect.py`，含「`DIALECTS`/`SURFACES`/`ELEMENT_KEYS` 三表同键」与「`{role,name}` 不得出现在存量 P1/P2 产物里」）。★**`toolcatalog` 的 `required` 无法按新批次重算——做不了**：`output/pipeline/` 里 camoufox-1 面的 P2 样本**一条都没有**（那两处 camoufox/obscura 文件是 `probe_observe*` 探针产物、不是 run 存档），口径仍是上一批实测值；`Test存量口径` 会在第一个 camoufox 批次落盘时变红并提示重算。**P5 机制已落地、判据未过所以没切换（2026-10-20，增量六十八）**：① 动作派发按面（`actions.target_params`：`html_tag`→`{ref}` / `aria_role`→`{role,name}`，`aria` 面缺目标抛 `UnaddressableTarget` 而**不退回 ref**）；② `run`/`run-plan` 加 `--driver`（obscura 整批一条连接、行为不变；camoufox **一 Run 一次 launch**）；③ §6.1 判据变成命令 `cli compare-drivers`（`executor/dual_run.py`，`verdict` 只有 pass/fail/incomplete，**不合成加权分**）。**一次完整 `run` 已走通 camoufox**（DR01《让子弹飞》：13 站、20 个观察，元素键 `{ref,role,name}` 落盘正确）。**但第一轮真双跑判 fail**：分支分布 6 支超 5pp · `body_text` 中位数 814→398.5（−51%）· `elements_total` 中位数 105.5→31.5（−70%）；拦截率 25.0% vs 23.1%（读数，§6.1 不构成否决）。★**点击 3/3 超时：已定位（同日六轮探针，推翻当时的「新开 tab」说法）**——真变量是**窗口不可渲染**（最小化/息屏/锁屏/完全遮挡）：同一次 `locator.click` 窗口可见 **0.4–1.5s**、最小化 **5–13s**；`humanize=False` 82ms → **9.1s**、`humanize=0.3` 386ms → **9.1s**、`headless=True` 34–71ms **从不慢**；页内 `rAF` 滞后 4–17ms → **902ms–62s**（一帧没画）。四条证伪：**① 超时那次点击的事件照样落地**（页内 `pointerdown` 计数涨）→ 不是挂死是慢过超时；② headful 下**会话首页同样超时**（「首页 0.74s」只是那时窗口还可见）；③ `humanize` 只是放大器（9.1→13.3s），关掉治不了；④ 关 `widget.windows.window_occlusion_tracking.enabled`、调 `dom.min_background_timeout_value` / `background_throttling_max_budget` **都治不住**（最小化仍 5.1–9.4s）。⚠️ **别与 P2 那次 9–13s 混为一谈**：那次的处置是 `no_wait_after=True` + 有界 `_settle`（等导航），而本轮在**没有任何导航的局部页**上同样量到 9.1s——**两个机制，量级撞在一起**。后果仍是「点击没做成」被下游读成 `component_unverified`（理由通顺的假负样本），且**超时的点击不能重试**（事件可能已落地，重试=点两次）；`steps[]` 不记单步耗时，事后分不出 5s 与 13s。因此 `DEFAULT_KIND` **仍是 obscura**，见 [方案 04 §4.9.4](../设计方案/04-camoufox执行层替换方案.md)。**仍未闭环**：① ★**判据②③的差值不可比**（2026-10-21 梳理挖出）——两面 `elements_total` **根本不是同一个东西**（obscura = DOM 全量，camoufox = ARIA 枚举 kept 数；过滤规则也不同，§4.5.6 三处偏离）。所以「−70%」**不是可解释的量**，阈值调宽调窄都没意义。**先统一口径**（枚举基础 + 过滤规则对齐）**再谈因果**——⚠️ **这跟 M-21 是同一件事**（方案 15 R3「给 obscura 面加采集面」），台账分在两个域文档里会被当成两件事排期；② **headless 拦截率未实测**——口径已定为无人值守走 `headless=True`（2026-10-21 owner 拍板，增量六十九），**这是 A 的前置门**（§6.1：显著变高是否决项），一次双跑的读数；③ 双跑样本量只有 1 个 task × 1 次，候选集本身的随机性盖过差异，结论要 ≥3 个 task 复跑才稳；④ **人判两条**（负样本成因 / 动作可移植性）——`compare-drivers` 只报 `needs_manual`，机器判不了；⑤ `toolcatalog` 采集面 3 条 `exercised=False` 仍未变（没在 camoufox 存档上跑过 `build`）；⑥ `required` 按新批次重算仍做不了（同一原因；且契约测试的 glob **不递归**，`dualrun/` 下的存档看不见 → 那 5 条是 skip 不是 pass）；⑦ **P6 未开工**（批次元数据 `driver`+`fingerprint`+动作编码版本）——⚠️ **口径反转让它提前了**：`headless` 已在 `fingerprint` 里（第九项），但**整份 fingerprint 还只在 driver 层，没进 P1 存档**。★**已闭环的部分**（2026-10-21）：点击超时的**口径**已定（无人值守走 `headless=True`）、`ACTION_TIMEOUT_MS` 5s→**15s**（盖住实测上界 13323ms）、**超时不重试**（事件可能已落地，加测试钉住）、**`steps[]` 记单步耗时** `duration_ms`（⚠️ 计时器换成了 `time.perf_counter`——Windows 上 `time.monotonic` 刻度 15.6ms，量出来的耗时是假的；见 [L-12](#低--记着别丢)）
| **H-09** | 簇 C 修复的尾巴：题绑定精度 / D-10 投影 / 7 类回归 | 02 感知 / 04 探针 | 编年史 §增量六十「仍未闭环项」 · [04 §增量六十](04-探针七层域.md) | 46 行里 8 行真因是 `not_play_site`、2 行是 `subscription_wall`，被默认题绑定压成 `component_unverified`；**动态派分支触碰「信 LLM 自报」边界**，见 Q-02 |

---

## 中 · 该做、没阻塞主线

| ID | 事项 | 域 | 记录锚点（证据） | 现在差什么 |
|---|---|---|---|---|
| **M-01** | 「窗口 ≠ 候选集合」修复**未实施** | 01 采集 | [设计方案/04-候选提取与窗口同源修复方案](../设计方案/04-候选提取与窗口同源修复方案.md) · [01 §判断点①根因](01-采集执行域.md) | 根因已定位、方案已写；要动 `_link_window` 与 `extract_candidates` 的接线，**动的是判断点 ① 的核心**，所以一直压后 |
| **M-02** | 存量 rationale **64 档 / 480 条未重生成** | 05 rationale | [05 §风险与未做](05-rationale与闸门域.md) · 编年史 §增量四十六 | 仍是事后反思口径；`regenerate-rationales` 已就绪（≈796 次 LLM 调用），**卡后端配额** |
| **M-03** | `executor/cli.py` God 文件拆分 | 09 纪律 | [09 §`cli.py` God 文件拆分](09-门禁与纪律域.md) · 实测 `wc -l` = **3148 行**（文档写 2978，已漂） | 纠正版三阶段方案已评审；**阶段 0 的门禁接线不可跳过**——AST 白名单不扫树，搬完门禁对新文件静默失效 |
| **M-05** | 157 条 `no_text_signal` 的「证据丰富化」 | 04 探针 / 08 存档 | [04 §未闭环](04-探针七层域.md#本域已知未闭环项) · [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | `evidence` 短到 L3/L4 读不出信号；下一批 proberun 应优先覆盖 |
| **M-06** | L4 VLM 任务级上下文（D14.6） | 04 探针 / 06 组装 | [04 §L4 任务级上下文](04-探针七层域.md#本域已知未闭环项) · [设计方案/14 §5](../设计方案/14-完整任务视图与LS上传.md) | **前置条件已满足**：T 视图已落地（[`assembler/task_view.py`](../../assembler/task_view.py)），可以给 `VLMReviewInput` 加 `task_context`（2026-10-20 核：该字段**尚不存在**）。第一步**只加字段，不改决策树** |
| **M-07** | 旧 per-unit LS 上传路径弃用（`--legacy-unit-trajectory`） | 08 存档 | [04 §未闭环](04-探针七层域.md#本域已知未闭环项) · [设计方案/14 §9](../设计方案/14-完整任务视图与LS上传.md) | ⚠️ **域文档「ls_bridge 均未入库」已过时**：12 个文件在 `git ls-files` 里。实际剩余 = **过渡期到 2026-11-09 后删旧路径** + 真实 LS 项目上的端到端验证 |
| **M-08** | judge 输出非确定性（E014 step0 翻转 anchored↔mild_hallucination） | 05 / 07 | [05 §未闭环](05-rationale与闸门域.md#本域已知未闭环项) · [07 §未闭环](07-评估与黄金集域.md#本域已知未闭环项) | 加 `temperature=0` 或多数投票；属 `llm/client` 层**独立批**，别混进判定批 |
| **M-09** | D-3 一致率 66.7% + `d3_scorer` 是否也做 capping 两档 | 07 评估 | [07 §D-3/D-9](07-评估与黄金集域.md) | 需 few-shot prompt 调优后才能定阈值；capping 待 D-3 覆盖率实测后再定（INC42 只做了 d9） |
| **M-10** | `rule` vs `llm` 分歧率比对**未做** | 02 感知 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) | `replay --perceptor both` 需要正文全文存档，新批次已具备条件；跑前先 `check`（模型名写错时每题返回 `None`，症状像「LLM 判不了」实为 404/403） |
| **M-11** | `CONFIDENCE_JUDGED = 0.8` 是**未校准先验** | 02 感知 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) | 拿到标注集后按题校准；现在是**拿常数当概率用** |
| **M-12** | `trailer_only` 词表覆盖率与误杀边界未摸底 | 02 感知 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) | 只在 `trailer_suspect` 上跑到过，`trailer_only` **一次都没触发** |
| **M-13** | `player_unverified` 在真实数据里 **0 次出现** | 02 感知 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) · [branches.py](../../executor/branches.py) | 分支代码通了，但现有存档都跑在它落地之前——**没被真实数据检验过** |
| **M-14** | 规则版 `PLAYER_OK` 对真实视频站正样本召回 **0** | 02 感知 / 06 组装 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) | 只认 `<video>`/`<audio>`，真实站全是 iframe/JS 播放器；**W1 正样本只能靠人工复核**（`review --write` → 填 `verdict` → `--apply`） |
| **M-15** | 存储侧只有**第一批 5 条样本** | 06 组装 | [06 §未闭环](06-组装域.md#本域已知未闭环项) | D-2 报表「0/5 不可判定」的根因是**一条 click 都没有**，不是缺字段；正样本需人工复核补 |
| **M-16** | `evaluation.cli` 0 数据时不报警 | 09 纪律 | [09 §未闭环](09-门禁与纪律域.md#本域已知未闭环项) | 「0 份存档 → 0 条样本」与一切正常**同形**；判据应是「读到 0 份存档」而不是「0 条样本」。2026-10-20 核：`evaluation/cli.py` 里无此判据 |
| **M-17** | `03-数据契约 §G1 黄金集契约` 仍按旧 10 分支标 | 03 标签 | [03 §未闭环](03-标签与分支域.md#本域已知未闭环项) | 新 4 分支要**主动构造**而不是「自然跑出」（增量十四 §五），契约没跟着改 |
| **M-18** | D-9 retest round=2 队列的真人重测 | 08 存档 | [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | κ = 0.176 是 agent 口径；要真 test-retest κ 得人工盲打后 `--force` 覆盖 |
| **M-19** | 编年史**缺增量六十一**（代码里多处引用它） | 09 纪律 | 90 §增量六十二 文首「编号说明」 | `archive.write` 的 `run_id`/`batch_id` 注入、`rationale` 字段结构变化都引用了「增量六十一」，**那份增量从未写进编年史**——补记或正式作废，别让它悬着。（编年史现已到**增量六十九**，六十一仍缺） |
| **M-20** | 上半年表列的域文档过时行**反向同步** | 09 纪律 | 本文 §域文档里这几行已经过时 | 台账已把 9 条矛盾挑明，但域文档还写着旧话；**下一批回头改域文档**，否则下一个人还是会照旧的那份做 |
| **M-21** | **方案 15 的 R3 / R4 / R5 未开工**——agent runtime 改 pull 模式、感知层退位、P1 契约迁移 | 01 采集 / 02 感知 / 跨层 | [设计方案/15 §7](../设计方案/15-执行器与agent-runtime重新定义.md) · 编年史 §增量六十四 | **R1 / R2 已落地**（工具面轴：catalog 逐面、契约逐面判、`driver` / `tool_surface` 三处字段），**R3 起未动**：① 采集面 3 条**只进声明没接上**（`exercised=False`），agent runtime 仍是 push 模式（执行器推回执）；② `ref` 机制与 I6 白名单**还在**，`{tag,label}` 目标编码**还在**；③ 感知层**六道站内题仍在热路径**（它只剩终局验收一个岗位才对）；④ 新批次**还没打新工具面**（R5）。⚠️ 这三阶段是**破坏性**的（I3/I6 退役、`ActionTarget` 换形），owner 已授权。⚠️ **R3 开工前要先钉一条**：采集面**工具名**是协议级（两面可共用），**返回内容**是面级（obscura 的 `interactive` 给 `{ref,tag,label}`、camoufox 给 `{role,name}`）——采集面读到的与动作面能定位的**必须同一套词汇**，否则是把 push 模式的老毛病换马甲。给 obscura 面加采集面 = 方案 04 P3 的活（把 obscura 的 `interactive` 输出也换成 `{role,name}`）。★ **2026-10-21：这与 H-08 的「判据②③不可比」是同一件事**——统一 `elements_total` 口径正是「给 obscura 面加采集面」。**两处分开记会被当成两件事排期**，做的时候按一件事做 |

---

## 低 · 记着，别丢

| ID | 事项 | 域 | 记录锚点 | 现在差什么 |
|---|---|---|---|---|
| **L-01** | L4 图像输入待 obscura 暴露截图原语 | 04 探针 | [04 §未闭环](04-探针七层域.md#本域已知未闭环项) | 届时填 `VLMReviewResult.screenshots` + prompt 加 `image` 切 GPT-4V / Claude Vision；**实现层不动** |
| **L-02** | 真视觉 KPI 未复跑 | 04 探针 | [04 §未闭环](04-探针七层域.md#本域已知未闭环项) | 等 OpenAI / Anthropic key 配齐，跑 v.qq.com 12 URL 对比 multim 与 heuristic 的 L4 label 分布 |
| **L-03** | 三类 verdict（hindsight/systematic/misleading）**0 干净实例** | 07 评估 | [07 §未闭环](07-评估与黄金集域.md#本域已知未闭环项) | 只能靠 rationale writer 变体实验或新人工复核批次补；**合成假例被禁止**（INC40/41 两批确认） |
| **L-04** | D-9 一致率未实测 | 07 评估 | [07 §未闭环](07-评估与黄金集域.md#本域已知未闭环项) | 池激活后在带 rationale 的存档上跑 `--include-llm-judge` 量 verdict 分布 |
| **L-05** | 变异测试只在特定批次跑过 | 09 纪律 | [09 §未闭环](09-门禁与纪律域.md#本域已知未闭环项) | 40/40、41/41、封 mojibake 阈值 3 处；**不是每次增量都跑** |
| **L-06** | `tests/contract/tools/` 不进 CI | 09 纪律 | [09 §未闭环](09-门禁与纪律域.md#本域已知未闭环项) | `observation_shortfall_wording_diff.py` 是 reviewer 视角人工 drift 巡检，**不是机器判等**——故意的，别接进 CI |
| **L-07** | `schema_version` 跨版本对比前先核对 | 06 组装 | [06 §未闭环](06-组装域.md#本域已知未闭环项) | `freeze manifest` / `versions.jsonl` 写 `label_schema_version` |
| **L-08** | 下一次清空 `output/pipeline/` 会让 **21 条测试重新 skip** | 06 组装 / 09 纪律 | [09 §22 条 skip](09-门禁与纪律域.md#22-条-skip-是怎么来的) | 清空前先读那节；skip 是**静默失效**的老坑 |
| **L-09** | `code.media_signal` 目前仅登记预期 | 03 标签 | [03 §未闭环](03-标签与分支域.md#本域已知未闭环项) | 阶段 4 才落地实现，**别当已经能产出** |
| **L-10** | m.ixigua 金标语义分叉 | 02 / 08 | [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | `subscription_wall` vs `component_unverified` 同站不同行，**需上游金标复核，非代码可修** |
| **L-11** | 7 类单页落地 URL 在生产采集中若有新样本需逐条核对；`partial_free` nav-bar CTA 真例 4 批 0 命中 | 04 探针 | 编年史 §增量六十「仍未闭环项」 · `test_单页落地URL_4条分支放行` | 防回潮由测试兜底，但没有真例就只是**没被证伪** |
| **L-12** | 四处 `time.monotonic()` 计时**在 Windows 上是粗刻度**（15.625ms） | 09 纪律 | 编年史 §增量六十九「顺带第三件」 · `rule_perceptor.py:853/874` · `llm_perceptor.py:1115/1138` · `orchestrator.py:912/957/1015/1081/1148` · `mcp_client.py:205` | Windows 上 `monotonic` 走 `GetTickCount64`，**分辨率 15.625ms**（`perf_counter` 走 `QueryPerformanceCounter`，1e-07）。已核：**`latency_ms` / `elapsed_ms` 只进账本、不进任何阈值** → 是**遥测精度问题，不是判定错误**。`StepLog` 已换（那个守卫测试时红时绿）；剩下四处**独立一批**再动，不挡 P5 |

---

## 待 owner 拍板（不是「还没做」，是「不能自己决定」）

| ID | 决策 | 锚点 | 两条路的代价 |
|---|---|---|---|
| **Q-01** | 簇 A 出路：`page_quotes` 是否纳入救济输入源 | [00 §不能做](00-总览.md) · 编年史 §增量四十四 | 纳入 = 「信 LLM 自报」，**触碰纪律边界**；不纳入 = 转攻采集层（H-03/H-04） |
| **Q-02** | 簇 C 题绑定是否按 LLM reason 动态派分支 | 编年史 §增量六十 · [04 §建议优先级](04-探针七层域.md) | 同上——动态派分支 = 让 LLM 决定贴哪个标签。**方案 Z（LLM self-reported 门）默认禁**，除非 owner 明批 |
| **Q-03** | m.ixigua 金标语义（H/L-10）谁来复核 | [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | 上游标注问题，代码层无解 |
| **Q-04** | 存量 rationale 重生成的**配额**何时给（M-02） | [05 §风险与未做](05-rationale与闸门域.md) | ≈796 次 LLM 调用；不给配额就一直停在「事后反思口径」 |
| **Q-05** | D-11 camoufox 是否过 go/no-go（H-08） | [设计方案/04 §6.1](../设计方案/04-camoufox执行层替换方案.md) | 拦截率不达标就不换；换了要重标定 `elements_total` 口径（D-2 打分口径漂移风险）。✅ **子问题已闭（2026-10-21）**：执行口径定为**无人值守走 `headless=True`**（增量六十九）。**go/no-go 本身仍未闭**——六条判据没过，且判据②③的差值**还不可比** |
| **Q-06** | 旧 per-unit LS 路径 2026-11-09 到期后删还是续（M-07） | [设计方案/14 §9](../设计方案/14-完整任务视图与LS上传.md) | 删 = 依赖方断；续 = 两套口径继续并存，**并池口径分歧会有人读成质量变化** |
| **Q-07** | `partial_free` nav-bar 形态在黄金集里有没有真例（决定方案 W/X 边界，H-07 前置） | 编年史 §增量五十八～六十 · [04 §建议优先级](04-探针七层域.md) | 有真例 → 词表 e2e 先跑；没有 → 方案 X 已够 |

---

## 环境 / 外部依赖（不是代码题，别当 bug 修）

| 症 | 真因 | 出处 |
|---|---|---|
| `test_xlsx_backfill` fail | 254 份 xlsx 里 23 份不在本工作区 | [09 §当前状态](09-门禁与纪律域.md) |
| `uv sync` 后测试 `ModuleNotFoundError: openpyxl` | `uv sync` **会卸掉没写进 `pyproject.toml` 的手装包**（2026-10-20 声明 camoufox 时踩到）。已把 `openpyxl` 补进 dev 组；**以后再手装任何测试要用的包，先写进 dev 组再 `uv add`** | [设计方案/04 §9](../设计方案/04-camoufox执行层替换方案.md) |
| `run` / `check` 报 `OBSCURA_EXE` 缺失 | 环境变量未设；**缺失即报错不猜路径**是设计 | [04-运行手册](../04-运行手册.md) |
| `--perceptor llm` 全线 `None` | `TRAJECTORY_LLM_*` 未设，或模型名写错返回 404/403（症状同形） | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) · `cli check` |
| obscura 拒访 `127.0.0.1` | 默认开 SSRF guard，**安全特性非缺陷** | [01 §未闭环](01-采集执行域.md#本域已知未闭环项) |
| 簇 C e2e 复现做不了 | 需联网 + 真实浏览器；离线只能做代码侧分析 | 编年史 §增量五十七 |

---

## ⛔ 不能做 / 不该做（试之前先看）

摘自 [00-总览](00-总览.md) 与 [08 §不能做](08-存档治理与回流域.md)——**这里只是索引，理由去原文读**：

- **合成 D-9 few-shot 假例**：三类 verdict 结构性缺素材，合成被禁止（INC40/41）。
- **把 `page_quotes` 纳入救济输入源**：等于信 LLM 自报，需 owner 拍板（Q-01）。
- **用「该站 body 有付费墙信号」当探针结论**：必须**逐行验目标行**（INC43 的错就在这个粒度）。
- **修改 xlsx 人工复核工作单**：它是人类输入，追溯只改 `annotations.jsonl`。
- **把 LLM 建议写成 `review_label`**：那是把 `ai-assisted-unconfirmed` 伪装成 `manual-review`，违反 D-9 边界③。
- **手工抄第二份工具清单**：真源只有 [`toolcatalog/`](../../toolcatalog/)（增量六十二）。
- **读仓库根 `output/`**：新树产物一律 `trajectory_pipeline/output/pipeline/`（D8）。
- **`import` 存量 `archive/v1/`**：只读参考，照着读再重写。

---

## 记录文档地图 · 「这个结论记在哪」

| 想找什么 | 去哪 | 备注 |
|---|---|---|
| 某条设计**为什么**长成现在这样、哪次改的 | [90-增量编年史](90-增量编年史.md)（增量五十～六十九） | 切换到 agent runtime 执行器**之后**的批次；切换前的批次已删除 |
| 某块**现在跑到哪**、差什么 | 本台账 + [01](01-采集执行域.md)～[09](09-门禁与纪律域.md) 域文档 | 域文档管「现状叙述」，台账管「待办清单」 |
| 字段级契约 | [contracts/](../contracts/)（P1/P2/P3/T）+ [03-数据契约](../03-数据契约.md) | 双向测试盯着，改字段先改契约 |
| **未实施**的方案构想 | [设计方案/](../设计方案/) | 已实施的在编年史，不在方案里 |
| 踩过的坑、症状原文 | [02-避坑指南](../02-避坑指南.md)（9 类 75 条） | 按终端里的原话搜 |
| 命令、环境变量、产物目录 | [04-运行手册](../04-运行手册.md) | 联网与否也在这 |
| 门禁/测试封着什么 | [09-门禁与纪律域](09-门禁与纪律域.md) + `trajectory_pipeline/tests/` | 「绿灯 ≠ 验过」那节是前提 |
| 工具协议 / 入参 | [tools.md](../tools.md) · [依赖说明/](../依赖说明/) | 前者记协议，后者记怎么装怎么验 |

---

## 域文档里这几行**已经过时**（2026-10-20 核，别照着读）

台账的作用之一是**盯住域文档的腐烂**。以下行写得比现实旧——**以本台账为准**，域文档修不修看下一批：

| 域文档原文 | 现在的真相 | 怎么核对 |
|---|---|---|
| 04/08「`ls_bridge` 均未入库」 | **12 个文件已入库**（`client`/`exporter`/`cli`/`scan`/`scorecard`/`settings`/`push_index`/`project_manager`/`errors`/`__init__`/`__main__`/`label_configs`） | `git ls-files trajectory_pipeline/evaluation/ls_bridge/` |
| 04/08「L7 LS 双向桥未启」 | 主路径 `upload` 已通（D14.5），剩**旧 per-unit 弃用**（M-07） | 编年史 §增量五十四～五十六 |
| 04「L6 人工复核触发器未接」 | **钩子早已接**（`dispatch_l6`，挂在 CLI 两处主循环末尾）；缺的是把 `flagged_mismatch` 桶纳入派发条件（H-05） | [`review_queue.py:842`](../../executor/review_queue.py#L842) · 编年史 §增量三十六（xlsx 工作单审计） |
| 02「簇 C 根因未定位」 | 已定位并修（**增量六十·方案 X**，净修 46 行） | [`branches.py:587-593`](../../executor/branches.py#L587-L593) · `test_单页落地URL_4条分支放行` |
| 00「下一步：簇 C 根因定位（高）」 | 已做；剩 H-09 三条尾巴 | 同上 |
| 00「门禁 2368 passed / 1 failed」 | **2026-10-21 实测 3265 passed / 1 failed / 15 skipped**（failed 仍是 `test_xlsx_backfill` 环境缺失）；00 是 2026-10-15 快照，增量六十记 2954 | `uv run python -m pytest -q` |
| 00「编年史（增量二十八～四十四，1275 行）」 | 现到**增量六十九**；**编年史的实际范围是增量五十～六十九**（增量二十八～四十九已删，`07-增量档案.md`（增量一～二十七）也已删）。✅ **CLAUDE.md / 00-总览 / 05-现状与验收三处的「增量二十八～六十八」已于 2026-10-21 改掉** | `grep -n "^## " 90-增量编年史.md` |
| 00「完整清单与清空前的全部实测数字在**增量档案**的『附录 A–D』四节里」 | ⚠️ **悬空引用**——`07-增量档案.md`（4162 行）已删除，那四节不在了 | `git show HEAD:trajectory_pipeline/docs/07-增量档案.md` |
| 09「`cli.py` 2978 行」 | **3148 行** | `wc -l trajectory_pipeline/executor/cli.py` |
| 方案 14 §9 全 ⏳ | Phase 1/2/3/4/6 已落，Phase 5 是过渡期 | 编年史 §增量五十二～五十六 |

> 这张表**不是**让人现在去改那几份域文档——是让人**别拿它们当事实**。
> 域文档的「未闭环项」表按设计就该跟着本台账走；本台账先立起来，下一批反向同步。

---

## 台账自身的保质期

数字会烂，**锚点不会**。过期时按下面重跑，别照着旧数字做判断：

```powershell
uv run python -m pytest -q                                   # 门禁通过数 / fail 清单
uv run python -m trajectory_pipeline.executor.cli check-archives   # 存档完整性
uv run python -m trajectory_pipeline.executor.cli report --out trajectory_pipeline/output/pipeline
uv run python -m trajectory_pipeline.evaluation.cli          # D-2/D-4/D-5/D-6 汇总
git ls-files trajectory_pipeline/evaluation/ls_bridge/       # M-07 一类「入库没有」的事实
```

**维护规程（三条，别发明第四条）：**

1. **新增 TODO**：先写所属域文档的「⚠️ 本域已知未闭环项」，**再**在本台账加一行，
   「记录锚点」指回域文档 §。反过来写（只进台账）会让域文档变成空壳。
2. **闭环**：台账**删行**；域文档改成「已闭环 + 判定依据（测试名 / 增量号）」；
   编年史记一笔。三处不同步，下一批就会有人重做一遍。
3. **不做进度叙述**：「怎么走到现在的」是编年史的活，台账只写「还差什么」。
   台账一旦开始讲故事，就会和 00-总览 一样退化成一份过期的快照。