# TODO 台账 · 全域未闭环项汇总

> **这一份是 TODO 的唯一汇总入口。** 只收**未闭环**的事；已经闭环的留在
> 各域文档与编年史里，**不进台账**——台账里出现已闭环项，就说明它开始骗人了。
>
> **快照日期：2026-10-23**（**同日补入[校准回路（方案 16）一节](#校准回路方案-16-未闭环)**：
> C-06 / C-07 / C-10～C-14 / C-16 / C-18 / C-19 十条未闭环项 + Q-08～Q-10 三条待裁
> （D-C4 / D-C6 / D-C7）。台账此前**完全没收过 `C-xx`**——方案 16 是独立的施工序列，
> 但它阻塞 H-02 的方向判定，所以必须在这里可见；
> 2026-10-21 补记 H-08 / Q-05 / M-19 / M-21 / L-12 / L-13；
> 同批补入质量审查 H-10～H-13 / M-22～M-24 / L-14～L-15；
> **再补入第二轮双跑后的 H-14 / H-15 / M-25 / L-16**；
> **2026-10-22 补记 owner 裁定切 camoufox（增量七十九）：H-08 / Q-05 闭 + M-21 缩小**；
> **2026-10-22 补记「读到一半」（增量八十）：H-08 / Q-05 尾巴重开**——第一批批次实测出
> **15% 的观察是半读** ⇒ 判据①/② 要等**修复后的第二批**再取）。
> 数据来源 =
> [00-总览](00-总览.md) 的优先级表
> + 各域文档的「⚠️ 本域已知未闭环项」+ [90-增量编年史](90-增量编年史.md)
> 增量五十七～七十九 + [方案 16](../设计方案/16-评估器与rubric双向校准回路方案.md) 的任务表
> + 代码实测（`git ls-files` / `wc -l` / 各条对应的源码行）。
>
> **每条一行，四栏**：`事项` / `记录锚点`（证据在哪，点进去能核对）/ `现在差什么`。
> **不带锚点的 TODO 不收**——「记得还有件事」这种条目没人能复核，等于没记。

---

## 怎么用这份台账

| 你的目的 | 看哪 |
|---|---|
| 找下一件该干的活 | §高 → §中，按 `00-总览` 的「阻塞程度 × 成本」排 |
| **校准回路（方案 16）卡在哪、下一件是哪个 `C-xx`** | [§校准回路（方案 16）](#校准回路方案-16-未闭环)——**它是 H-02 方向判定的上游** |
| 怀疑某条已经做完了 | 顺着「记录锚点」进域文档 / 编年史 / 源码行核对 |
| 想知道某条为什么还没做 | 锚点那条下面通常写着「为什么没动」 |
| 想知道能不能做 | §⛔ 不能做——**试之前先看，别重蹈** |
| owner 要拍板什么 | §待 owner 拍板 |
| 数字过期了 | §台账自身的保质期，重跑那几条命令 |

**判定「闭环」的标准**（三条都满足才算）：

1. 代码里能指到实现（文件 + 行）；
2. 有测试封着它（测试名能说出来，且**真的会跑**，不是 skip）；
3. 有记录（编年史增量号 或 域文档的「已闭环 + 判定依据」；`C-xx` 记在
   [方案 16 §9](../设计方案/16-评估器与rubric双向校准回路方案.md#9-任务执行计划) 的实施结果块里）。

只满足 1 的是「写完了」，不叫闭环——v1 的 `regulation` 与 `usage_prune` 都栽在这上面。

---

## 高 · 阻塞主线指标

| ID | 事项 | 域 | 记录锚点（证据） | 现在差什么 |
|---|---|---|---|---|
| **H-01** | `unresolved` 剩余桶重新归因（原 36/79 = 46%） | 01 采集 / 02 感知 | [01 §未闭环](01-采集执行域.md#本域已知未闭环项) · [02 §INC44 分层](02-感知与判定域.md) · 编年史 §增量四十四 | 簇 C 已被**增量六十·方案 X** 修掉（净修 46 行，[`branches.py:593`](../../executor/branches.py#L593)），**旧的 46% 已不作数，必须重测**才能知道剩下的是谁 |
| **H-02** | D-10 代码层 vs 黄金集一致率 76.5% → ≥90% | 07 评估 | [07 §D-10](07-评估与黄金集域.md) · [00 §四条主线](00-总览.md#一句话现状) | 缺 13.5pp；簇 1 type A（component_unverified 漏 VIP 强信号）+ type C（collection fail 8 条），要 **LLM prompt 改善 + 重采集两条路都跑**。⚠️ **先跑哪条不是拍脑袋定的**——由[校准回路 §C-07](#校准回路方案-16-未闭环)（rubric 缺陷 vs 评估器缺陷的归属比例）判；在那之前动任一条都是**没数据的选择** |
| **H-03** | mojibake（charset）防护**从未被真实站点触发** | 01 采集 / 05 rationale | [05 §mojibake 防护](05-rationale与闸门域.md) · 编年史 §增量四十五 · `research/probes/inc45_probe_sohu.py` | 防护代码已落、变异测试封着，但**没有一次真实命中**。证伪动作：跑 `tv.sohu.com` 真采一次 |
| **H-04** | JS-heavy 站真渲染失败（iqiyi；w.mgtv / sohu 反爬时变） | 01 采集 | [01 §窗口与候选集合](01-采集执行域.md) · [02 §INC43 探针](02-感知与判定域.md) · `research/probes/inc43_probe_bodies.jsonl` | 15 行判对但仍 `unresolved`，属**采集层能力边界**，不是判定规则能救的 |
| **H-05** | 31 条 `flagged_mismatch` **进不了 L6 复核队列** | 04 探针 / 08 存档 | [04 §未闭环](04-探针七层域.md#本域已知未闭环项) · [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | ⚠️ **不是「触发器没写」**：`dispatch_l6`（INC36）已在，挂在两处主循环末尾（[`review_queue.py:842`](../../executor/review_queue.py#L842)）。缺的是**派发条件**——只认 L5 反思档与 fail-closed 兜底，`auto-validator-l3l4-v1-flagged-mismatch` 桶**不在其中**（该串在 `review_queue.py` 零命中，2026-10-20 核） |
| **H-06** | 判断点 ② 让错误页通过（bilibili「发生了错误」判 True） | 02 感知 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) · 避坑指南 §5 类 | **未修且是设计题**：没有可靠的代码层事实能区分「错误页」与「正常页」。要么改 ② 的输出契约强调错误页，要么接受 ③ 兜住它（现状） |
| **H-07** | `partial_free` / `login_required` 黄金集**各 0 条** | 03 标签 / 07 评估 | [03 §MIN_PER_BRANCH](03-标签与分支域.md) · [07 §未闭环](07-评估与黄金集域.md) | a 路径已证实跑不出（INC33）；b 路径追溯 `login_wall_or_blocked` **17 条**待做 |
| **H-08** | D-11 camoufox 执行层替换：**P-1/P0/P1/P2/P3/P4 已落地；P5 机制已落地但判据未过 → 未切换；执行口径已定为 headless（2026-10-21）；P6 未开工**（含 **D12** 动作词汇表对齐） | 01 采集 / 跨层 | [设计方案/04-camoufox](../设计方案/04-camoufox执行层替换方案.md) §6 落地阶段 | 方案定稿、**P-0 起一个没动**，**生产链仍跑 obscura**。**P-1 两段都跑完**（探针 `executor/browser/probe_f5_name.py`）：离线段（[§1.5](../设计方案/04-camoufox执行层替换方案.md)）role 侧零落差、朴素链 65% 且图标按钮产出空 name（`get_by_role(name="")` → `count == 0` **不报错**，与「元素不存在」同形）；**真实页面段（[§1.6](../设计方案/04-camoufox执行层替换方案.md)，6 URL / 240 行）判据达标**——权威 name **91/91 = 100%**、JS+伪元素 96.7%、纯 JS 87.9%。⚠️ **D12 生死门已过，但实现口径被这次实测改了两处**：① 采集面 **name 取 `aria_snapshot`，不许自己算**；② 枚举**先过可见性**（41% 元素没有可访问名，其中 90/97 不可见）+ 定位器**钉 `exact=True`**（默认 substring 口径下命中率 100%，判据会变空）。**P0 已落地**（`driver_factory.py`：6 处硬编码 `ObscuraDriver(client)` 收敛到 `make_driver`，零行为变化；AST 门禁盯第 7 处）。⏳ **P0 的「存档逐字节同形」没验**——P0 当时本机缺 `OBSCURA_EXE`（后来配上了，基线存档得另找）。✅ **已闭（2026-10-21，增量七十一）——但判据换了**：旧判据（跑两遍比字节）**作废**（活站会漂，它分不清「重构改了行为」与「站点漂了」，且没有分母），换成**离线确定性**的「同一份输入 ⇒ 同一份存档」——把驱动录下来的转录喂进两条装配路径比规范化后的存档，`tests/unit/test_p0_equivalence.py`，**5 条全绿 + 过变异测试**（改坏实现 ⇒ 4/5 红）。**P1 已落地（2026-10-20）**：`camoufox_session.py`（一个 Run 一次 launch，钉死 `os`/`screen`/`window`/`humanize`/`geoip` 关/`main_world_eval` 关/排除 uBO，**回读实测指纹**）+ `camoufox_driver.py`（只落 `goto`/`snapshot`/`count`/`evaluate`/`close`，其余原语**抛 `NotImplementedError` 不返回空**）；`check --driver camoufox` 可探并打印读数，**`DEFAULT_KIND` 仍是 obscura，生产链零变化**。实测两处要注意（[§4.3.1](../设计方案/04-camoufox执行层替换方案.md)）：`screen`/`dpr` 钉得住，但 **`window=` 钉的是外框**——8 次实测内宽恒 `1905`、**内高在 `940`/`948` 间摆动**（5:3）→ 批次元数据只信实测值；真要钉死视口得改 `new_context(viewport=…)` **并重验指纹自洽**，P5 前定。§6.1 反爬拦截率仍是切换的 go/no-go 门槛，**不达标就不换生产链**。**D12 的「声明侧」已落地**（2026-10-20，增量六十四）：工具面轴（`obscura-1` / `camoufox-1`）进 `toolcatalog`，面由 P1 的 `driver` 一对一折算；**P3-α 已落地（2026-10-20）**：`camoufox_driver.interactive()` 一页一次 `page.aria_snapshot()`（51ms/页，逐元素方案 200 元素要 4.8s），role+name 全取 playwright 结论，三类丢弃（词表外 role / 无名字 / 真同名整组）记进 `last_enumeration`；纯解析器 `browser/aria_snapshot.py` 离线可测。`check --driver camoufox` 打元素表+账目（`example.com` kept=1 / `baidu` nodes=101→kept=34）。⚠️ 三处**已知偏离**记在 [§4.5.6](../设计方案/04-camoufox执行层替换方案.md)：可见性未逐元素过滤（5.8ms×N）、`tag` 装的是 ARIA role（下游两处词表已放宽成并集，P4 收窄）、新发现 `{role,name}` **只在同一次枚举内有效**（`textbox` 的名字可能是动态占位词）。**P2 动作面已落地（2026-10-20，[§4.6](../设计方案/04-camoufox执行层替换方案.md)）**：`click`/`fill`/`press` 三个原语 + **协议改名**（`type_text`→`fill`、`press_key`→`press`，语义层与驱动层同名，`agent_loop` 里那张翻译表已删）；三态**先 `count()` 再操作**（0 → 静默 `not_found`、>1 → `ambiguous` 不动作、`count` 报错 → `DriverError`），定位钉 `exact=True`，**跨面参数抛 `ValueError`**（`DriverError` 会被 `steps/aggregator` 吃成 `unreachable_hard`，接线 bug 就变成理由通顺的负样本）。真浏览器冒烟三原语全 `ok`（`check --action-smoke`）。★**新事实**：`role+name` 是**页面级**定位，**导航后不失效而是可能静默点中新页同名元素**（对照 `ref` 必然报错）→ **任何导航之后必须重新枚举**。★**动作超时被实测改写**：`ACTION_TIMEOUT_MS=5000` 让一半点击超时（`Locator.click: Timeout 5000ms exceeded`），现场取证证明元素在/可见/可用/包围盒稳定、`elementFromPoint` 就是它，而 `click(no_wait_after=True)` 只要 1.1s、`goto` 同址 0.3s——**9–13s 是 playwright 在等「这次点击触发的导航完成」**。处置：`click`/`press` 关掉 `no_wait_after`，之后 `_settle()` 有界等 `domcontentloaded`（2s，`NAV_SETTLE_TIMEOUT_MS`），结果记 `last_action["settle"]`=`ok`/`timeout`（等不到不抛但要说出来）。修后连跑 4 次 **0 超时**；但 2/4 点击后 2s 内 URL 未变（对照探针 `goto` 路径 3/3 在 0.4s 内跳）→ **「点了但没跳」是站点行为，未定论**，`ok` 只代表调用成功。**P3-β 已落地（2026-10-20，[§4.7](../设计方案/04-camoufox执行层替换方案.md)）**：`links`/`extract`/`new_tab`/`close_tab`/`observe` 全部落地，**协议原语齐了**，`_todo` 已删。三条被实测改写的口径：**标签页做成栈**（一个 `page` 字段「开 tab → 观察 → 关 tab → 回中间页」无处可回；只剩一页时先补空白页、开页失败收掉新页——真浏览器四条语义全对）· **`links` 按 href 去重**（obscura 的 `browser_links` 恒 `total==distinct`（131/131、33/33），不去重时 camoufox `links_total` 高 28–37%，而它要进 D-2 口径）· **`extract` 取文本 `innerText‖textContent`**（bing 结果页 `li.b_algo h2 a` 的 10 个锚 **`innerText` 全空**，只取它 → 「抽到 10 个空标题」→ ① 退回 fail-closed，看着像选择器失效）。★**两面 `Observation` 已并排测**（`probe_observe --driver {obscura,camoufox}`，同脚本同契约断言）：`url`/`title`/`body`/`links`/`video`/`iframe`/`degraded` 全部对齐且两边契约全绿，但 **`elements_total` 74 vs 34（−54%）**、`raw_len` 含不含快照、`links[].text` 面包屑 vs 锚文本——**三处不同义**，故 P2 后半条判「已测量」而非「同口径」。★**`search` 标题抽取已逐条对齐**（`probe_result_titles --driver …`：bing 两边 `10/10` 对、baidu 两边 `8/8` 对且 8 条含片名）；顺带推翻一句旧记录：两边 `@href` 今天都返回**包裹形态** `baidu.com/link?url=…`，`steps/search.title_for` 里「obscura 给解包后的 URL」那句是另一种页面变体上的旧测量。**P4 契约迁移已落地（2026-10-20，[§4.8](../设计方案/04-camoufox执行层替换方案.md)，增量六十七）**：`element` 的键名不再全局手抄，改成**按方言投影**——落盘/进 prompt 时由 `common/elements.py` 的 `ELEMENT_KEYS` 一对一折算（obscura-1 面 `{tag,label}`、camoufox-1 面 `{role,name}`），**槽位统一（role/name）、值域不统一（html_tag vs aria_role）、不互译、不混池**；`Observation.element_vocabulary` 由驱动实现自己置，replay 从存档键反推（`vocabulary_of_archive_elements`），perceptor 词表按面收窄（**认不出就抛**，不回落）。20 条契约测试封着（`tests/contract/test_element_dialect.py`，含「`DIALECTS`/`SURFACES`/`ELEMENT_KEYS` 三表同键」与「`{role,name}` 不得出现在存量 P1/P2 产物里」）。★**`toolcatalog` 的 `required` 无法按新批次重算——做不了**：`output/pipeline/` 里 camoufox-1 面的 P2 样本**一条都没有**（那两处 camoufox/obscura 文件是 `probe_observe*` 探针产物、不是 run 存档），口径仍是上一批实测值；`Test存量口径` 会在第一个 camoufox 批次落盘时变红并提示重算。**P5 机制已落地、判据未过所以没切换（2026-10-20，增量六十八）**：① 动作派发按面（`actions.target_params`：`html_tag`→`{ref}` / `aria_role`→`{role,name}`，`aria` 面缺目标抛 `UnaddressableTarget` 而**不退回 ref**）；② `run`/`run-plan` 加 `--driver`（obscura 整批一条连接、行为不变；camoufox **一 Run 一次 launch**）；③ §6.1 判据变成命令 `cli compare-drivers`（`executor/dual_run.py`，`verdict` 只有 pass/fail/incomplete，**不合成加权分**）。**一次完整 `run` 已走通 camoufox**（DR01《让子弹飞》：13 站、20 个观察，元素键 `{ref,role,name}` 落盘正确）。**但第一轮真双跑判 fail**：分支分布 6 支超 5pp · `body_text` 中位数 814→398.5（−51%）· `elements_total` 中位数 105.5→31.5（−70%）；拦截率 25.0% vs 23.1%（读数，§6.1 不构成否决）。★**点击 3/3 超时：已定位（同日六轮探针，推翻当时的「新开 tab」说法）**——真变量是**窗口不可渲染**（最小化/息屏/锁屏/完全遮挡）：同一次 `locator.click` 窗口可见 **0.4–1.5s**、最小化 **5–13s**；`humanize=False` 82ms → **9.1s**、`humanize=0.3` 386ms → **9.1s**、`headless=True` 34–71ms **从不慢**；页内 `rAF` 滞后 4–17ms → **902ms–62s**（一帧没画）。四条证伪：**① 超时那次点击的事件照样落地**（页内 `pointerdown` 计数涨）→ 不是挂死是慢过超时；② headful 下**会话首页同样超时**（「首页 0.74s」只是那时窗口还可见）；③ `humanize` 只是放大器（9.1→13.3s），关掉治不了；④ 关 `widget.windows.window_occlusion_tracking.enabled`、调 `dom.min_background_timeout_value` / `background_throttling_max_budget` **都治不住**（最小化仍 5.1–9.4s）。⚠️ **别与 P2 那次 9–13s 混为一谈**：那次的处置是 `no_wait_after=True` + 有界 `_settle`（等导航），而本轮在**没有任何导航的局部页**上同样量到 9.1s——**两个机制，量级撞在一起**。后果仍是「点击没做成」被下游读成 `component_unverified`（理由通顺的假负样本），且**超时的点击不能重试**（事件可能已落地，重试=点两次）；`steps[]` 不记单步耗时，事后分不出 5s 与 13s。因此 `DEFAULT_KIND` **仍是 obscura**，见 [方案 04 §4.9.4](../设计方案/04-camoufox执行层替换方案.md)。**仍未闭环**：① ★**判据②③的差值不可比**（2026-10-21 梳理挖出）——两面 `elements_total` **根本不是同一个东西**（obscura = DOM 全量，camoufox = ARIA 枚举 kept 数；过滤规则也不同，§4.5.6 三处偏离）。所以「−70%」**不是可解释的量**，阈值调宽调窄都没意义。✅ **口径已统一（2026-10-21，增量七十）**：新增**共享口径**（同一段 JS `SHARED_DOM_JS` + 同一把尺子 `filter_ladder`，唯一真源在 `executor/browser/element_basis.py`）+ `Observation.element_basis` 新列（**`elements_total` 一个字没改**）；`compare-drivers` 判据③拆成「面内口径（跨面 `incomplete`）」+「共享口径（承载 pass/fail）」，并给**归因表**（两本账逐档对相，差值落在哪一档一眼可见）。✅ **第二轮双跑已跑（2026-10-21，增量七十二）——读数齐了**：headless、3 task × 2 面、开 `--element-basis`；判定仍 `fail`，但**产出是五个「怎么读数」的认识**：① 拦截率口径把登录墙算成反爬（拆开后**结论相反**：真·反爬 A 14.89% / B 9.09%）；② obscura 抽不出客户端渲染页正文 ⇒ A 侧 60% 的站点是「没测到」；③ 两批样本只重叠 37/50 ⇒ 占比口径不可直接比；④ 判据②③ 的中位数是**混合中位数**，按族分列后**会翻号**（播放页族是 A 更好，60 vs 35）；⑤ B 面 9 条 `login_wall_or_blocked` 是**「理由通顺的假负样本」**（判断点② 的守卫门槛太锐 ⇒ 新增 H-15）。**已改工具**：拦截率拆桶、配对比较、按族分列、归因表「不可相减」注释。⚠️ **彻底统一 `elements_total` 的语义仍要动 obscura 的方言**（= 方案 15 R3，见 M-21），那是另一件事；⚠️ **彻底统一 `elements_total` 的语义仍要动 obscura 的方言**（= 方案 15 R3，见 M-21），那是另一件事；✅ **headless 拦截率已实测（2026-10-21，增量七十二）——A 的前置门过了**：真·反爬口径 B **9.09%** < A **14.89%**（配对子集 B 8.11% < A 13.51%）⇒ **不显著变高，不构成否决项**。⚠️ **必须用「真·反爬」口径**——合并口径（反爬 + 登录墙）给出**相反**的结论（A 19.15% / B 34.09%）；✅ **双跑样本量已提到 3 task × 各 1 次**（增量七十二：DR01/DR02/DR03，同 `--max-candidates 20`）；⚠️ **但「3 个 task」不等于读数稳了**——第三轮识别的两个混淆（候选集只重叠一半、抽正文能力不同）说明**样本量不是主要矛盾，口径才是**（H-14）；④ **人判两条**（负样本成因 / 动作可移植性）——`compare-drivers` 只报 `needs_manual`，机器判不了。**材料已备好**（增量七十二）：负样本侧**已抓到 9 条「理由通顺的假负样本」**（B 面把「谁都没读到内容的页面」写成 `login_wall_or_blocked` ⇒ H-15，材料在 `research/probes/inc72_paired_compare.py`）；动作可移植性侧**连工具都还没有**（M-25），结构上大概率可移植（name 取自 playwright 自己的 `aria_snapshot`），没验的是重放时还能不能定位到；⑤ `toolcatalog` 采集面 3 条 `exercised=False` 仍未变（没在 camoufox 存档上跑过 `build`）；⑥ `required` 按新批次重算仍做不了（**整批 camoufox-1 面的 P2 样本还是不够**）。✅ **递归扫描那一半已闭（2026-10-21，增量七十一）**：契约测试的 glob 改成**扫整棵子树 + 按形状认**（P2 = 顶层有 `six`；P1 = 顶层有 `run_config` 且有 `steps`/`visits`）+ 按 `run_id`/`sample_id` 去重（备份副本不重复计分母）——`dualrun/` 下的存档**从此进视野**，那 5 条 camoufox 面的断言**从 skip 变成真判**。⚠️ 递归扫描当场抓出一条**谎话**：`toolcatalog` 里 camoufox-1 面的 `click` 标着 `exercised=False`、注释写着「camoufox 面一次都没跑过」，而 `dualrun/camoufox/` 里**有 3 次 `{role,name}` 点击**——已改成 `True` 并重写注释；⑦ **P6 未开工**（批次元数据 `driver`+`fingerprint`+动作编码版本）——⚠️ **口径反转让它提前了**：`headless` 已在 `fingerprint` 里（第九项），但**整份 fingerprint 还只在 driver 层，没进 P1 存档**。★**已闭环的部分**（2026-10-21）：点击超时的**口径**已定（无人值守走 `headless=True`）、`ACTION_TIMEOUT_MS` 5s→**15s**（盖住实测上界 13323ms）、**超时不重试**（事件可能已落地，加测试钉住）、**`steps[]` 记单步耗时** `duration_ms`（⚠️ 计时器换成了 `time.perf_counter`——Windows 上 `time.monotonic` 刻度 15.6ms，量出来的耗时是假的；见 [L-12](#低--记着别丢)）、**判据②③的口径统一**（增量七十：共享口径 + 归因表）、**P0 的等价性门禁**（增量七十一：判据换成「同一份输入 ⇒ 同一份存档」+ 过变异测试）、**契约测试递归扫描**（增量七十一：按形状认 + 去重）、★**测量失败不再污染 `degraded`**（增量七十一：`element_basis_error` 独立通道——否则一把坏尺子会作废整批数据）。✅ **已闭（2026-10-22，增量七十九）——owner 裁定绕过 §6.1 直接切**：owner 判「**不再双跑**，选定 camoufox 做默认浏览器调用主力——**反爬拦截与页面内容获取是主要，速度次要**，可接受增加等待时间」。⇒ **`DEFAULT_KIND` 已改成 `"camoufox"`**（改这一处就切生产链）；配套三件**不做也会出事**的已一并落地：① **P6 指纹进 P1**（`RunRecord.fingerprint` + `to_json` 落盘 + 编排器从 `driver.fingerprint` 取值）——指纹**只在 launch 那一刻确定，事后补不回来**，而 `window=` 钉的是**外框**（8 次实测内宽恒 1905、内高 940/948 摆动）⇒ 不记它，批次分布的变化会被读成「站点变了」；② **`per_site_timeout_s` 90 → 180**（90 是 obscura 时代的数；camoufox 单次点击实测上界 13.3s × 8 步 ≈ 122s ⇒ 90s **必然触顶**，触顶会被记成 `unreachable_hard`——**理由通顺的假负样本**）并**暴露到 CLI**（`--per-site-timeout`，非正数即报错）；③ **`proberun` 补记 `driver` 与 `fingerprint`**（此前一条都不记 ⇒ 会被折算成 obscura-1 面渲染，而真实调用是 camoufox 面——**声明与真实调用对不上**）。**探针三条命令一起接**（`probe` / `proberun` / `mediabrowse` 各加 `--driver` 与 `--headful`）。**obscura 没删**——它是存量 P1/P2 产物的方言（`obscura-1` 面**只读不再产出**）与口径回溯参照系。★**切换后自查又抓出两处「默认值搬家」的漏**（**手工路径不炸、另一条路径炸**，只测过手工路径的用例发现不了）：① **计划路径**（`run --plan --driver obscura`）的 `make_driver` 没钉 `kind` ⇒ `ValueError: camoufox 实现需要 CamoufoxSession，未提供`——**批量生产入口直接炸**；② **`check --driver` 缺省**把 `"obscura"` 硬编码在自己的 parser 里 = **第二份隐式约定** ⇒ 自检默认探的**不是生产面**，读数不能外推。两处都已修，并各配一条**通用守卫**（扫全文件的 AST 断言 + 扫全 parser 的缺省断言）——要防的是**下一处**漏写。✅ **第一批 camoufox 真实批次已开跑（2026-10-22）**——`run --plan plan-camoufox-prod1.json`（12 条 · seed 20261022 · `--element-basis` · LLM 感知层），落 `output/pipeline/camoufox_prod1/`。**接线冒烟先行**（单条 15 站 / 16 outcome / 560s）：`driver=camoufox`、`fingerprint` 九项全落盘、面折算 `camoufox-1`、`per_site_timeout_s=180`——「自检过 ≠ 批次能跑」那两条未覆盖面（LLM 感知层 + 站点遍历）都通了。⚠️ **两条必须随批记账的口径**：① **片名分布高度重复**——骨架库 98 条里 **38 条是《功夫》**（唯一片名仅 35 个），本批 12 条里 7 条也是 ⇒ **拦截率的分母不独立**，读数不能当「12 个独立站点分布」；② **LLM 后端会回 529**（冒烟 4 次 / 批次内多次），已确认被 `max_retries=2` 的 5xx 重试吸收——**重试用尽才 fail-closed 记 `unresolved`**，所以冒烟里 6 条 `unresolved` 都带着实打实的 LLM 判词，**不是**后端抖动伪装出来的。⚠️ **`llm_calls` 在 scripted 批次恒空是设计如此**（账本只圈 agent_loop，感知层走 `Decision.latency_ms`）——别把它读成「LLM 没被调用」。★ **跑这批时又抓出「递归扫描」漏了三处**（增量七十一只改了 `tool_catalog` 一处）：`test_element_dialect._run_records` / `test_p1_contract._real_archives` / `test_p2_contract._real_archives` 全是 `glob("*.json")` **只扫顶层** ⇒ **「存量口径那三条是假绿」**（新批次落子目录压根没进视野）；另有 `_ARCHIVE_NAME` 只放行**两段名** ⇒ 盘上 **1444 个 P2 样本被全挡** ⇒ `_observed_from_p2` 恒 0 ⇒ **`required` 一直在按 P1 算**，而它文档写着「统计源选 P2 样本」。四处已修，并配**两条通用守卫**（按语义扫 AST 抓「读产物目录却只扫一层」+ 归档名正则必须放行三段名）。⚠️ **第一批落盘后 `Test存量口径` 会红**——现已**真的红**（2 条，指名 `SMOKE01` 是 camoufox-1 面）——**要重算 `required`，不是把门禁改松**；⚠️ **`build` 切出 camoufox 面 P2 样本后**`test_现有P2样本里元素键名仍是tag_label` **也会红**，那是 M-21 记的存量口径债。✅ **第一批 camoufox 真实批次已跑完（12/12，退出码 0）**——负样本池 **181 → 260 行（0 重复）**、P2 重建到 **543 份**（361 obscura-1 + 182 camoufox-1）、**`required` 重算后无需改动**（`click` 的 `{role,name}` 59 次调用、键 100% 在、值 100% 非空；`goto`/`new_tab` 的 `url` 100%）。★ **但逐条读 evidence 读出「读到一半」**（增量八十）：**同一页、同一动作位置、两次独立运行，`iframe_count` 读出 3 与 0** ⇒ 那个 0 **不是页面事实**。机制：`observe` 是 4 次以上独立往返而导航**异步 commit**，原先 `_settle` 等的 `domcontentloaded` 等的是**当前文档**（旧文档早就是 loaded ⇒ 那句等待**恒真**）。⇒ 治法两条（都在驱动层）：**动作之后先等新文档 commit 再观察**（有界 2s，`click`/`fill`/`press` 一律等——owner 裁定「速度次要」）+ **观察侧读数自检**（跨了文档就重来 2 次，仍不稳记 `degraded += "nav_unsettled"` ⇒ 下游**不判**）。★ **判据换了一代**：导航事件 `framenavigated` **被实测否掉**（它对 pushState 也触发 ⇒ SPA 改 URL 会被误判成「换了文档」⇒ **把好观察当半成品丢掉**）⇒ 换成**文档代号** `performance.timeOrigin`（每文档一个常量、pushState 免疫；实测四条全过）。**修复前基线**（`research/probes/probe_settle_readings.py`）：camoufox 24 份存档 / 388 条带 URL 的观察 ⇒ **过渡态 59（15%）**、**读数打架 6**。⇒ **第二批（修复后，同一份 12 条计划）在跑**，用它量「15% / 6 条」是否清零，并**在它上面重取判据①（真·反爬拦截率）与判据②（`body_text`）**——⚠️ **在修复前的批次上取那两个数是量错对象**（半读正文拉低 `body_text`、半读页面被误读成「没测到」⇒ 算进拦截）。⚠️ **第一批那 79 行池（修复前）的去向**待定：硬证据只点到 1 行，但「读到旧页」那一类**读数冲突是隐形的** ⇒ 按「两代不并池」处置。⚠️ **obscura 面没有对应机制**（只读存量面，不再产出）⇒ 不补 |
| **H-09** | 簇 C 修复的尾巴：题绑定精度 / D-10 投影 / 7 类回归 | 02 感知 / 04 探针 | 编年史 §增量六十「仍未闭环项」 · [04 §增量六十](04-探针七层域.md) | 46 行里 8 行真因是 `not_play_site`、2 行是 `subscription_wall`，被默认题绑定压成 `component_unverified`；**动态派分支触碰「信 LLM 自报」边界**，见 Q-02 |
| **H-10** | `compare-drivers` 将候选侧缺账当成 0 | 01 采集 / 09 纪律 | [`dual_run.py:347`](../../executor/dual_run.py#L347) · [`test_dual_run.py`](../../tests/unit/test_dual_run.py) | `_ratio_diff()` 使用 `(cand or 0.0)`：`_ratio_diff(10.0, None)` 返回 `1.0`，而基线缺失返回 `None`，两侧语义不对称。候选没采到应判 `incomplete`，不能当成候选计数为 0 后判 `fail`；需补单边缺账测试，详见本文 §H-10 |
| **H-11** | `compare-drivers` 的「同面」判定比较计数，不比较面集合 | 01 采集 / 09 纪律 | [`dual_run.py:361`](../../executor/dual_run.py#L361) · [`test_dual_run.py`](../../tests/unit/test_dual_run.py) | `_same_surface()` 直接比较 `Mapping[str, int]`：同一个面跑 2 次对 1 次会判不同面；两侧具有相同的混合面计数却会判同面。应要求每侧恰好一个已知识别面且面名相同，详见本文 §H-11 |
| **H-12** | camoufox 枚举成功但元素全被过滤时丢失 face 账 | 01 采集 | [`camoufox_driver.py:955`](../../executor/browser/camoufox_driver.py#L955) · [`test_camoufox_driver.py`](../../tests/unit/test_camoufox_driver.py) | `face_ledger = dict(self.last_enumeration) if elements else {}` 把「枚举成功、`kept=0`」误当成「没有枚举账」。过滤档位被清空后，下游无法区分「页面确实无元素」与「元素全被过滤」；应按当前页是否成功枚举决定是否取账，详见本文 §H-12 |
| **H-13** | 声明支持 Python 3.10，实际语法要求 3.12+ | 09 纪律 | [`pyproject.toml:5`](../../../pyproject.toml#L5) · [`arbitration.py:219`](../../executor/arbitration.py#L219) | `requires-python = ">=3.10"`，但该行 f-string 表达式含反斜杠；Python 3.10 编译报 `f-string expression part cannot include a backslash`。需选择重写表达式并补 3.10 门禁，或将最低版本明确提高到 `>=3.12`，详见本文 §H-13 |
| **H-14** | ★**判据①「分支分布」在换层场景没有可比性**——A 侧 60% 的站点是「没测到」 | 01 采集 / 09 纪律 | [设计方案 04 §4.9.8.3–4](../设计方案/04-camoufox执行层替换方案.md) · [`dual_run.py`](../../executor/dual_run.py) · `research/probes/inc72_paired_compare.py` | 第二轮双跑实测：**obscura 在爱奇艺这类客户端渲染站上正文一个字都抽不出**（`body_len=0` + `degraded=['body_text']`，19 条），于是 A 的 47 个站点里 **28 个是 `unresolved`**；而两批样本**看的不是同一批页面**（按落地 URL 配对只重叠 **37/50**）。⇒ 占比差里混着「站点质量差」与「观测能力差」两件事。**缺的那一刀**：分母只取「**两面都测到的页面**」（配对子集已现成，`pair_pages` 已在 `compare-drivers` 里跑着），且 A 侧 `unresolved` 的页面要单独列而不是并进占比 |
| **H-15** | ★**判断点② 的兼容回落把「不是墙」兜成了「墙」** ⇒ 10 条 `login_wall_or_blocked` 假负样本 | 02 感知 | [设计方案 04 §4.9.8.6](../设计方案/04-camoufox执行层替换方案.md) · `perception/llm_perceptor.py::_is_reachable` 末段 + `executor/branches.py::decision_branch`（payload 缺失 → 保持合并态） | **诊断已改（2026-10-21 增量七十三，现场重采证伪）**：原诊断是「② 的『采集未拿到内容』守卫门槛太锐（6 字符 ≠ 空 ⇒ 守卫不触发）」。**现场重采推翻了它的前提**——那 10 条的页面**今天原样还在**（现场与存档逐字一致），**不是读到残留，采集层如实采到了**（`degraded=[]` 是对的）。⇒ **真正的问题是分支映射越权**：② 答 False 且**没填 `access_control_type`**、`page_quotes` 也无阻断关键词 ⇒ `decision_branch` 走「payload 缺失 → 保持题绑定的合并态」⇒ 落进 `login_wall_or_blocked`——而**墙 / 验证码 / 反爬这三个前提一个都没观测到**。**这正是纪律里写着的「贴标签前核前置事实」被绕过了**（`BRANCH_PRECONDITION` 里根本没有 `login_wall_or_blocked` 这一条）。**正解不是调阈值**（阈值怎么调都改变不了「站点只返回了 6 个字」这个事实），而是**兜底改成 fail-closed 落 `unresolved`**。★★ **判据⑤「人判负样本成因」的材料与结论已备齐（2026-10-21 增量七十四）**：工作表 `inc73_human_review`（表一 39 条 B 面负样本，**封闭集** W1 真·墙/反爬 · W2 采集没拿到内容 · W3 不是播放页 · W4 页面坏了 · ? 说不清）**已按分支摆开**。那 10 条**逐条读出来的结论**：**LLM 自己的 evidence 就没提「登录/验证码/反爬」**（写的是「未加载出目标内容」「仅显示加载提示」「页面仅显示通用标题和即将开始的提示」）⇒ **W1 不成立**；**W2 也不成立**（现场与存档逐字一致 ⇒ 不是采集没读到，是页面当时就那样）；**A 面（obscura）对同一批 URL 落的正是 `unresolved`**（两边分歧本身就是证据）。⇒ **这 10 条在词表 W1–W4 里没有归类**——它们是「**站点返回了占位/不可用页，但没有任何拦截特征**」，正确落点是 `unresolved`（fail-closed），**不该进负样本池**（**10/39 = 26% 的池子是分支越权造出来的**）。✅ **owner 已拍板（2026-10-21 增量七十五）：兜底落 `unresolved`，**不开新支**。** 裁定理由两条：① 「站点返回了占位/不可用页」**不是一个新现象**——它就是「没判出来」，`unresolved` 的语义装得下，**新开一支等于给「说不清」发一支专用分支**，而 fail-closed 的口径就是「说不清就落 unresolved」；② **分支是 22 类封闭集**，加一支要连带改 `all_branches()` / 覆盖追踪 / rubric 对齐账（[设计方案/02](../设计方案/02-rubric与代码对齐.md)），代价与收益不成比例。**已落地**：`executor/branches.py::decision_branch` 的兜底改为「`fallback in DEPRECATED_BRANCHES` ⇒ 落 `unresolved`」——**按语义判（合并态不再兜底），不按题号判**；④ 的 `component_unverified` **刻意不动**（它不是合并态，且它的前提事实是成立的）。门禁：`test_branches.py` 新增 3 条（兜底落 `unresolved` / 带细分 payload 仍落细分分支 / 兜底不产生负样本）+ `test_visit.py` 3 条改写。⚠️ **裁定是前向的**：那 10 条**已写进存档的 `login_wall_or_blocked` 还在池里**（`negative.jsonl`），要清得走 `cli downgrade`（只改 P1，**不删池行**）或 `cli purge-run`（删存档 + 池行）——**owner 尚未就这 10 条的历史数据拍板**。⚠️ **原问法已作废**：「爱奇艺那 7 条算不算 `not_play_site`」——**不算**：它们 **video=2（有播放器标签）**，是播放页，只是正文被占位符盖住。★**判据⑤ 的成因词表已改口径（2026-10-21 增量七十六）**：旧的 **W1–W4 作废**（W1 把登录墙与反爬合在一处，而付费墙/无播放控件/组件不可验证/只有预告/导航失败**全都没有位置**；实测 10 条 `login_wall_or_blocked` 里 2 条是 404、2 条是 Cloudflare 挑战、1 条是 App 限定，**0 条能确证是登录墙**）。新词表 = **站点侧 10 项（取值就是分支名）+ 我们侧 5 项（动作码 / 非样本分支）+ `?`**，判据改为「**这条是不是站点明确说了看不了**」⇒ 站点侧留池、我们侧出池、`?` 留空。**那 10 条在新词表里的落点是 `unresolved`（没判出来）**，不是 `not_collected`（现场与存档逐字一致 ⇒ 不是「没读到」）。★**首轮人判已把这 10 条逐条判完（2026-10-21 增量七十七）**：**7 条落 `subscription_wall`**（iqiyi——人工看过是「有资源，但要登录付费才能看」）+ **3 条落 `unreachable_hard`**（`chusuiwenhua` ×2 / `9999pian`——「网页无法访问」）。⇒ **没有一条是墙**，与 H-15 的裁定一致；但同时说明**这 10 条是应该进池的真负样本**（只是标签错了）⇒ 它们要回池**必须先有「改分支」的路**（H-16 / H-17 记的缺口） |
| **H-16** | ✅ **已闭（2026-10-21，增量七十八）**——两处都修了：① **判据换代**（救济只认**门禁语境**）② **补路**（「改分支」有了） | 02 感知 / 08 存档 | `perception/rule_perceptor.py::paywall_evidence`（新）+ `perception/llm_perceptor.py::_diagnosed_access`（新）+ `executor/relabel.py`（新）+ `executor/cli.py::cmd_relabel`（新） · [07-人工复核与判读手册 §2.7 / §6.4](../../docs/07-人工复核与判读手册.md) · `research/probes/inc73_human_review.json` | **原症状**：`paywall_rescue_evidence` 只要求**一个完整短语**命中（`PAYWALL_WORDS` 里就有「开通会员」「续费」「打开app观看」），**不看上下文**；命中后 `_rescued_paywall_decision` 把 ②③ 的负分支**改判**成 `subscription_wall`——**连 LLM 已经诊断清楚的「地区限制」都被改掉**。**实测 12 条逐条看过，0 条是真付费墙**（8 条 `v.qq.com` 主证据是「当前设备不支持播放」+ 错误码 **70017001.1** = 地区限制，真因 `blocked`）。**修复①（判据换代，已落地）**：救济判据 `paywall_rescue_evidence` **删除**，换成**门禁语境**判据 `paywall_evidence`（两层：L1 门禁命题 / L2 CTA + **同句**观看语境）——裸 CTA（「开通会员 享更多精彩」「续费 充值」）**一律不成立**；再加**让位规则**：LLM 已给出结构化细分（② 的 `access_control_type`）**或** evidence 里有**环境阻断形态**（地区/验证码/反爬/404）时**一律不救济**（登录形态不参与让位——导航栏「请登录」与「开通会员」一样高频）；`subscription_wall` 补 `BRANCH_PRECONDITION` 前置事实核验（页面**明说**不给钱/不订阅就看不了正片）。⚠️ **登录形态不参与 evidence 让位**是刻意留的口子。**修复②（补路，已落地）**：新增 `cli relabel`（`executor/relabel.py`）——把「**判定成立但贴错支**」的记录换到另一个**真负样本**分支，**仍留在池里**；处置动作在模块、判据在命令行（与 `downgrade` 同一分工）；落盘时**存档 + 复核档 + 池行一起改**（训练侧吃的是池行；门禁查的是「键在不在池里」）；撞键**并入**不新建行；**默认 dry-run**、`--reason` 必填、凭据闸门挂入口。**仍未闭**：① **历史数据是否照 dry-run 清单执行改判**——**owner 未拍板**，本轮只交付路与清单（8 条 `subscription_wall` → `blocked`，其中 2 条并入已有行；**未 `--apply`**）；② `cause = success`（**根本不是负样本**，要转正）**仍然无路**——`relabel` 的目标必须是真负样本，接不了那一格（台账 **H-17**） |
| **H-17** | ★**首轮人判揭出：池里 7 条是「假负样本」**（资源存在、我们没走到播放页），其中 **5 条机器标签是 `component_unverified`** ⇒ **同页守卫在那 5 条上没拦住**；而 **A 面（obscura）对同一批 URL 记的是 `unresolved`** —— 同一个网站、同一批 URL，两面归属不同 | 02 感知 / 09 纪律 | `executor/gatekeeper.py::_same_page` · `executor/steps/visit.py:216` · `executor/steps/visit.py:228` · `research/probes/inc73_human_review.json` | **首轮人判已判完 39/39（2026-10-21 增量七十七）**：cause 分布 `subscription_wall` 13 / `not_play_site` 12 / ★**`success` 7** / `unreachable_hard` 4 / `broken` 2 / `blocked` 1；**留池（派生）32 / 出池 7**；标签对账：cause == branch 只有 **10/39**，剔除 10 条「已弃用合并态」（`login_wall_or_blocked`）后 **19 条真·标签错**。**`success` 是新造的第三类成因**（「不是负样本」）——⚠️ **与分支体系里的 `success`（`branch is None`＝「视频播出来了」）同名不同义**，词表里必须写清，且有运行时守卫不许 `success` 进 `BRANCH_LABELS`。**7 条的机器分支**：`component_unverified` ×5 / `blocked` ×1（`yn-mg`——现场是 Cloudflare 挑战页，**人工与存档读数不一致，待看**）/ `unreachable_hard` ×1（baidu 跳转链接，A 面没配对到）。★**5 条 `component_unverified` 有两条根因（逐条开存档核过）**：① **「点击失败」路径（3 条：hfzhifei / yrhbcl / jijikb）**——`visit.py:216` 的「点击 ref=… 失败：`Locator.click: Timeout 5000ms exceeded`」**直接记 `component_unverified`**（`reached=False`，**连播放页都没到**）；这条路上**没有点击后观察 ⇒ 同页守卫结构性无从触发**。而「点击失败」就是「我们没点进去」的同义反复 ⇒ **这 3 条必然是假负样本**（人工判 `success` 完全正确）。② **同页守卫的硬判据（2 条：56dy / hnsyzg）**——点击**成功**了，但 `_same_page`（`gatekeeper.py:166-182`）判「不是同一个页面」：**56dy 元素数 37→38（差 1）**、**hnsyzg 正文 1472→1468（差 4 字）**。两次 URL 相同、`video=0`/`iframe=0` 相同，**明明是同一页**，只是页面动态让某个粗粒度特征变了 ±一点 ⇒ 守卫放行 ⇒ 负标签留下。⚠️ **与该函数自己的 docstring 自相矛盾**：它写「正文会因懒加载微调」却把正文当**逐字相等**的硬判据；元素数更不稳定（一个浮层/一个 hover 态就是一个）却没人提，也当**逐个相等**。⇒ **A/B 差别已定位**：obscura 面那几次两次观察**三项全同** ⇒ 守卫触发 ⇒ 降级 `unresolved`；camoufox 面元素枚举与 DOM 面元素集合不同（**会多出/少掉一个**）⇒ 守卫放行。**正解**（待 owner 拍板，**没动生产代码**）：① 「点击失败」与「点击后取不到快照」两条路**不许记 `component_unverified`** ⇒ 落 `unresolved`（前置事实「到了播放页」压根不成立）；② `_same_page` 的元素数/正文改**容差相等**（元素数相对差 ≤10%、正文去空白后前缀一致），让「同一页的两次快照」能被判成同页。⚠️ **处置是「转正」不是「降级」**：这 7 条**本来就不该在池里**，要把它们从 `negative.jsonl` 拿回来并回正样本池，比「改标签」更重——而**这条路不存在**（同 H-16 的缺口：`downgrade` 只能落 `unresolved`，`review --apply` 只管 `REVIEW_BRANCHES`）。**已落地（只动研究层与文档，没动生产代码）**：词表加 `success`（第三类「不是负样本」，共 **17 项**）；「是否仍留池」那一列改**派生**（人填版与规则漂了 **4 行**——**同一个 URL 在两批里给出相反的留池值**），人填原值留 `keep_as_negative_human` 审计副本，冲突在 `keep_conflicts` 点名；★**顺带修掉「重生成工作表会把 4 条留池冲突静默抹掉」**——沿用逻辑**分不清「留池列是人填的」还是「派生的」**（两个时代用**同一个字段名**）⇒ 把**派生值**当人的值写进审计列 ⇒ 冲突 **4 → 0**，而汇总「看着正常」；现按**「审计列在不在」分两代**（旧表那一列就是人填的），并认得出**人对派生列的手改**（沿用但进冲突表点名）⇒ **重生成已幂等**（实测 39 行 / 沿用 117 格 / 冲突 4 / 逐格差异 0），测试钉在 `tests/unit/test_inc73_probes.py::Test沿用与重生成`。★**同类第二个坑**：`research/README.md` 那条命令**少列了一个 B 面存档目录**（漏 `dualrun/camoufox`）⇒ 照文档跑 **39 行变 28 行**，而落盘**覆盖**旧表 ⇒ **11 行人判消失**——同样是**静默**失败。⇒ README / 速查 / 手册三处都补了「`--a/--b` 要给全，**落盘前后各对一次行数(39)与冲突数(4)**」。⚠️ **「改分支」那条路已落地**（`executor/relabel.py`＝`cli relabel`，H-16 修复②，2026-10-21 增量七十八）——它管的是「**仍是负样本**、只是标签贴错」；而这 7 条**根本不是负样本**（要的是**转正**）⇒ **转正那条路仍然不存在**（`relabel` 的目标分支**必须仍是真负样本**，接不了这一格） |

---

## 中 · 该做、没阻塞主线

| ID | 事项 | 域 | 记录锚点（证据） | 现在差什么 |
|---|---|---|---|---|
| **M-01** | 「窗口 ≠ 候选集合」修复**未实施** | 01 采集 | [设计方案/04-候选提取与窗口同源修复方案](../设计方案/04-候选提取与窗口同源修复方案.md) · [01 §判断点①根因](01-采集执行域.md) | 根因已定位、方案已写；要动 `_link_window` 与 `extract_candidates` 的接线，**动的是判断点 ① 的核心**，所以一直压后 |
| **M-02** | 存量 rationale **64 档 / 480 条未重生成** | 05 rationale | [05 §风险与未做](05-rationale与闸门域.md) · 编年史 §增量四十六 | 仍是事后反思口径；`regenerate-rationales` 已就绪（≈796 次 LLM 调用），**卡后端配额** |
| **M-03** | `executor/cli.py` God 文件拆分 | 09 纪律 | [09 §`cli.py` God 文件拆分](09-门禁与纪律域.md) · 2026-10-21 实测 **3535 行**（文档写 2978，已漂） | 纠正版三阶段方案已评审；**阶段 0 的门禁接线不可跳过**——AST 白名单不扫树，搬完门禁对新文件静默失效。同批架构审查另记：`orchestrator.py` 1696 行、`llm_perceptor.py` 1712 行、`agent_loop.py` 1596 行、`evaluation/golden/builder.py` 1512 行；优先拆 `cli.py`，其余按变更频率再排 |
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
| **M-19** | 编年史**缺增量六十一**（代码里多处引用它） | 09 纪律 | 90 §增量六十二 文首「编号说明」 | `archive.write` 的 `run_id`/`batch_id` 注入、`rationale` 字段结构变化都引用了「增量六十一」，**那份增量从未写进编年史**——补记或正式作废，别让它悬着。（编年史现已到**增量七十**，六十一仍缺） |
| **M-20** | 上半年表列的域文档过时行**反向同步** | 09 纪律 | 本文 §域文档里这几行已经过时 | 台账已把 9 条矛盾挑明，但域文档还写着旧话；**下一批回头改域文档**，否则下一个人还是会照旧的那份做 |
| **M-21** | **方案 15 的 R3 / R4 / R5 未开工**——agent runtime 改 pull 模式、感知层退位、P1 契约迁移 | 01 采集 / 02 感知 / 跨层 | [设计方案/15 §7](../设计方案/15-执行器与agent-runtime重新定义.md) · 编年史 §增量六十四 | **R1 / R2 已落地**（工具面轴：catalog 逐面、契约逐面判、`driver` / `tool_surface` 三处字段），**R3 起未动**：① 采集面 3 条**只进声明没接上**（`exercised=False`），agent runtime 仍是 push 模式（执行器推回执）；② `ref` 机制与 I6 白名单**还在**，`{tag,label}` 目标编码**还在**；③ 感知层**六道站内题仍在热路径**（它只剩终局验收一个岗位才对）；④ 新批次**还没打新工具面**（R5）。⚠️ 这三阶段是**破坏性**的（I3/I6 退役、`ActionTarget` 换形），owner 已授权。⚠️ **R3 开工前要先钉一条**：采集面**工具名**是协议级（两面可共用），**返回内容**是面级（obscura 的 `interactive` 给 `{ref,tag,label}`、camoufox 给 `{role,name}`）——采集面读到的与动作面能定位的**必须同一套词汇**，否则是把 push 模式的老毛病换马甲。给 obscura 面加采集面 = 方案 04 P3 的活（把 obscura 的 `interactive` 输出也换成 `{role,name}`）。★ **2026-10-21：这与 H-08 的「判据②③不可比」是同一件事**——统一 `elements_total` 口径正是「给 obscura 面加采集面」。**两处分开记会被当成两件事排期**，做的时候按一件事做。✅ **口径那一半已落地**（增量七十：共享口径 `SHARED_DOM_JS` + `filter_ladder` + `Observation.element_basis` 新列，`compare-drivers` 的判据③拆成「面内口径 `incomplete`」+「共享口径承载 pass/fail」+ 归因表）；⚠️ **语义统一那一半仍在**——`elements_total` 仍是两个义，**彻底统一得动 obscura 的方言**（把 `interactive` 输出换成 `{role,name}`），那是 R3 的活。⭐ **2026-10-22（增量七十九）R3 的范围缩小了**：owner 裁定生产链默认执行层切 camoufox ⇒ **采集面现在只有一个生产面**（camoufox）⇒ 「**给 obscura 面加采集面**」（把 obscura 的 `interactive` 输出也换成 `{role,name}`）**已不再需要**——obscura 面降为**只读存量面**（`obscura-1` 面不再产出新样本），它的 `elements_total` 是多少**不影响新数据**。⇒ **R3 的剩余工作缩到**：① agent runtime 改 pull 模式（执行器推回执 → agent 主动拉）；② 感知层退位（六道站内题撤出热路径，只留终局验收）；③ `ref` 机制与 I6 白名单退役。⚠️ **但「统一 `elements_total` 语义」这件事并没有消失**——它从「R3 的活」变成「**存量数据的口径债**」：存量 obscura 批次的 `elements_total` 与新 camoufox 批次**不可比**（这正是 D-2 打分口径漂移风险的来源，Q-05 记的那条）。**混池即错**，所以那批存量要么标批次边界、要么不进训练池 |
| **M-22** | 共享口径的 `pass/fail` 缺少最小样本与任务配对约束 | 01 采集 / 09 纪律 | [`dual_run.py:487`](../../executor/dual_run.py#L487) · [`dual_run.py:301`](../../executor/dual_run.py#L301) | 当前只要两侧 `shared.kept` 中位数存在即可判 `pass/fail`，没有最低覆盖率、缺账阈值或同 task 配对要求。大量观察里只有 1 条共享样本，也可能决定整批判据；需明确门槛并补单样本、任务错配、低覆盖测试，详见本文 §M-22 |
| **M-23** | `executor` 只依赖感知协议的架构约束未被代码兑现 | 01 采集 / 02 感知 / 09 纪律 | [架构总览 §依赖约束](../01-架构总览.md#L86) · [`branches.py:33`](../../executor/branches.py#L33) · [`gatekeeper.py:58`](../../executor/gatekeeper.py#L58) | 文档要求 `executor` 不依赖任何 `perception` 实现，但 `branches.py` / `gatekeeper.py` 直接引用 `perception.rule_perceptor` 的词表与纯函数。需把共享知识移到 `common`、协议或独立地基，或正式修订约束并加入 AST 门禁，详见本文 §M-23 |
| **M-24** | ✅**已收口（2026-10-21）：xlsx 工作单删除，语义矛盾随参照物一起消失** | 09 纪律 | [09 §xlsx 测试清退](09-门禁与纪律域.md) · [`test_xlsx_backfill.py`](../../tests/unit/test_xlsx_backfill.py) · [`test_inc37_validator_pass.py`](../../tests/unit/test_inc37_validator_pass.py) | 原症状：同一份缺失工作单在 fixture 中走 `skip`，存在性测试却硬失败，INC37 又选择跳过，干净 clone 既可能红也可能静默少测。**处置**：两份 xlsx 已被人为删除且不可恢复（`.dumate/` 只剩 2026-10-12 早期快照，推不出终版），所以「xlsx 是发布必需还是可选审计输入」这个问题**没有可执行的答案了**——按「可选外部证据已永久缺失」处理：删掉 11 条拿工作单当参照物的测试（1 条永久 fail + 10 条永久 skip），保留 5 条纯 `annotations.jsonl` 断言。门禁 **0 failed / 0 skipped**（总条数当时 3398→3439，因工作区并发编辑）。详见本文 §M-24 |
| **M-25** | ✅**判据⑥ 的工具已建并已跑出第一手读数**（2026-10-21 增量七十四）——剩下的是**材料窄**，不是没工具 | 01 采集 | [设计方案 04 §4.9.8.8](../设计方案/04-camoufox执行层替换方案.md) · [`inc73_action_replay.py`](../../research/probes/inc73_action_replay.py) · `executor/browser/camoufox_driver.py::resolve` | **工具已建**：`inc73_action_replay`（**联网**，从存档取 `steps[]` → 在裸 camoufox 里重放 → 记每步定位结果），判定走**生产链同一条路径**（新加的 `CamoufoxDriver.resolve()`：只定位不点击，走 `_locate` 的三态 `ok`/`not_found`/`ambiguous`）——探针另写一份的话「重放定位率」就不是生产链的读数。归因**三列并排**（否则把两件事混成一件）：重放结果 × **当时在不在采集面阶梯里** × **页面漂没漂**（漂 = 站点改了版，**不是名字的锅**）。**读数（16 条动作 / 13 站）**：**16/16 全部定位到（`matches=1`）**，其中 **8 条页面已漂**（标题或正文长度变了）却仍定位得到 ⇒ **动作面在 camoufox 上可移植**；**旧批次那 3 条 `click` 超时不是可移植性问题**——重放时同样目标仍定位到（超时是**窗口不可渲染**那条，已由 headless 口径反转处置）。⚠️ **材料还窄**：① **全是 `click`，没有 `fill`/`press`**；② 动作都发生在**影视详情页**上，没有「搜索框填词 + 回车」那类；③ 3 条旧存档**没有可比基线**（无标题也无正文长度）⇒ 漂移记 `null`（未知）**不许记「没漂」**。⇒ **判据⑥ 现在的读数只覆盖「在详情页点一个链接/按钮」** |

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
| **L-13** | **venv 里几个包的文件被删了**，门禁会因为「装不上」而不是「写错了」变红 | 09 纪律 | 编年史 §增量七十「门禁」 | 2026-10-21 实测：`.venv/Lib/site-packages` 里 `python-dotenv` / `pyyaml` 的 `yaml/__init__.py` / `pyjwt` / `python-multipart` 的**文件**不见了，而 `.dist-info` 还在——**`uv sync` 只看 `.dist-info` 判定「已装」，于是不重装**，症状是 `ModuleNotFoundError: dotenv` 与 `cannot import name 'CLoader' from 'yaml'`。修法：`uv sync --offline --group dev --reinstall-package <名字>`（**整包 `--reinstall` 会被权限门拦下**）。⚠️ **pyyaml 的 `.pyd` 被本机的 python 进程占着**，rename 报「拒绝访问」——绕法是 `PYTHONPATH` 指到 uv 缓存里那个已解包的目录（`archive-v0/<hash>`）。**先看是不是环境问题，再去改代码** |
| **L-14** | `test_executor_mode` 依赖开发者本机 LLM 环境变量 | 09 纪律 | [`test_executor_mode.py:245`](../../tests/unit/test_executor_mode.py#L245) · [`test_executor_mode.py:263`](../../tests/unit/test_executor_mode.py#L263) | 同文件已有 `clean_llm_env`，但该用例未使用。保留本机六个 LLM 环境变量时通过；清空后实测 `46 passed / 1 failed`。异常参数应稳定落到空模式，不应由宿主机是否配置模型决定，详见本文 §L-14 |
| **L-15** | replay 的 `element_basis` 清理与 fail-closed 文案不一致 | 02 感知 / 09 纪律 | [`replay.py:169`](../../perception/replay.py#L169) | 文档与注释声称形状不对时整体返回 `None`、不返回半截账，但 `{"shared":"bad","face":{"nodes":9}}` 实际返回 `{"face":{"nodes":9}}`。需明确选择「整份拒绝」或「允许部分账」，并让实现、测试和注释一致，详见本文 §L-15 |
| **L-16** | venv 里留下一个 `yaml__broken_20261021` 目录待清 | 09 纪律 | 编年史 §增量七十二「顺带修好的环境项（L-13）」 · [依赖说明/camoufox.md §10.6](../依赖说明/camoufox.md) | 2026-10-21 修 L-13 时，Windows 不许删被占用的 `.pyd`（`拒绝访问，os error 5`），**但允许改名**——于是把 `yaml/` 改名为 `yaml__broken_20261021` 再 `uv sync` 装回。残留目录**惰性**（名字不是 `yaml`，import 不到），但**该删**——等本机那两个残留的 `cf_launch.py` 进程退出后删掉 |

---

## 校准回路（方案 16）· 未闭环

[方案 16](../设计方案/16-评估器与rubric双向校准回路方案.md) 的 `C-xx` 与上表的 `H/M/L` **同一套判定标准**
（代码可指 + 测试封着且真的会跑 + 有记录），**但编号不混**：`C-` 是回路内部的施工顺序，
`H/M/L` 是全域的阻塞程度。**已闭环的 C-01～C-05 / C-08 / C-09 / C-15 / C-17 不进台账**
（闭环项留在[方案 16 §9](../设计方案/16-评估器与rubric双向校准回路方案.md#9-任务执行计划)的实施结果块里，
台账里出现已闭环项就说明它开始骗人了）。下表**只收没做完的**。

**现状一句话（2026-10-23）**：**护栏段（R0 + R0.5）已清空**——能出快照、能判两轮、能退 `0/1/2`、
桶表封闭且分诊档有唯一写入口。**卡在「没有分诊数据」**：`verify` 的第四条判据（同桶清零）
打 `n/a`，`gate` 的第一次真正判定要等 v8 冻结。⇒ **下一件是 C-06**。

| ID | 事项 | 依赖 | 记录锚点（证据） | 现在差什么 |
|---|---|---|---|---|
| **C-06** | **首轮分诊 worklist**：给每张分歧卡挂上机器建议（`evaluator_logic` / `evidence_missing`）+ 人在同一处确认或改 | C-05 ✅ | [方案 16 §9.3](../设计方案/16-评估器与rubric双向校准回路方案.md#9-任务执行计划) · [`calibration/buckets.py`](../../evaluation/calibration/buckets.py) · [`calibration/triage.py`](../../evaluation/calibration/triage.py) | **工作表还没有**。⚠️ 预填只能落**卡上的 `suggested_*`**，**不许**写分诊档——`triage.actor_kind` 会按机器身份拒写 `rubric_*` / `gold_defect`（D-C3）。验收是「归属率 100%、无 `other`」，而 `other` 现在**结构上不存在**（封闭集）⇒ 真正要验的是**每行都有桶且桶的前置事实成立** |
| **C-07** | **归属分布报告**：rubric 缺陷 vs 评估器缺陷的比例 | C-06 | [方案 16 §9.3](../设计方案/16-评估器与rubric双向校准回路方案.md#9-任务执行计划) | **它是 H-02 的决策点**（下一手改尺子还是改判据），但没有分诊数据就没数可出。**别拿「分歧数量」冒充「归属比例」** |
| **C-16** | 触发读数 `triggers`：T-1～T-4 四类（先只出读数，不定线） | C-15 ✅ | [方案 16 §5.4](../设计方案/16-评估器与rubric双向校准回路方案.md#54-量化触发v13-新增) | `triggers.py` 未写。**阈值列必须留空时命令报「未定线」**——不许默认通过（D-C9） |
| **C-19** | 分层抽样器（S0–S3 固定配额 + seed） | C-15 ✅ | [方案 16 §5.7](../设计方案/16-评估器与rubric双向校准回路方案.md#57-人工抽样按分歧类型分层v13-新增) · [§9.3](../设计方案/16-评估器与rubric双向校准回路方案.md#9-任务执行计划) | `sampling.py` 未写。验收两条：同一输入两次抽样**逐行相同**；每层行数 == 配额（40 = 10×4）。**分层样本上的 kappa 不做总体外推**（D-C10）|
| **C-10** | D3 / D9 **锚点表化**，few-shot 挂 `anchor` | **等 Q-08（D-C7）** | [方案 16 §7](../设计方案/16-评估器与rubric双向校准回路方案.md#7-把-prompt-从散文升级成锚点表) · [§9.5](../设计方案/16-评估器与rubric双向校准回路方案.md) · [`scorers/rubric_version.py`](../../evaluation/scorers/rubric_version.py) | prompt 现在还是散文；`ANCHORS` 表与 `DIM_CLAUSES` 已在 `rubric_version.py` 里，**缺的是让 scorer 从表渲染 + 契约测试盯「改档不改 anchor 就红」** |
| **C-11** | `pred_provider` 记 **prompt hash** | C-10 | [方案 16 §9.5](../设计方案/16-评估器与rubric双向校准回路方案.md) · [`scorers/rubric_version.py`](../../evaluation/scorers/rubric_version.py) | 分歧卡现在还回答不了「这条是旧 prompt 判的还是新 prompt 判的」——`evaluator_version` 只记了源码指纹，不记 prompt 变没变 |
| **C-12** | rubric 变更记录加 `disagreement_id` + **责任区分声明**栏 | C-02 ✅ | [方案 16 §5.8](../设计方案/16-评估器与rubric双向校准回路方案.md#58-动机链三个必填栏v13-新增) · [§9.5](../设计方案/16-评估器与rubric双向校准回路方案.md) | 现在能从变更指到版本号，**指不回具体那一行分歧**；「以『评估器做不到』为由改 rubric」目前没有栏位可驳回 |
| **C-18** | **变更记录条款化**：加 `change_kind` / `expected_effect` + 「未列条款逐位不变」diff 守卫 | C-02 ✅ | [方案 16 §5.5](../设计方案/16-评估器与rubric双向校准回路方案.md#55-修改粒度条款级v13-新增) · [§9.5](../设计方案/16-评估器与rubric双向校准回路方案.md) | 改一条款时**无法机械证明其他条款的分数没动**——这是最便宜的放水路径 |
| **C-13** | `EQUIV_CLASS` 变更 **owner 签** + 「合并后不可见细分」清单 | **等 Q-09（D-C4）** | [方案 16 §9.6](../设计方案/16-评估器与rubric双向校准回路方案.md) · [`golden/builder.py`](../../evaluation/golden/builder.py) | 合并等价类是**最省力的提分路径**，而现在改一次没有任何签名与记账 |
| **C-14** | 阈值调整按 **rubric 级变更**（文档 + 流程） | **等 Q-10（D-C6）** | [方案 16 §9.6](../设计方案/16-评估器与rubric双向校准回路方案.md) | 「差一点点就把线调低」现在没有任何阻力 |

---

## 2026-10-21 质量审查项详解

本轮审查基于当前 `main` 工作树，包含已有未提交改动。审查基线如下：

- 全量门禁：`uv run --python 3.14 --isolated --frozen pytest -q trajectory_pipeline/tests` → **3295 passed / 15 skipped / 1 failed / 2 warnings**，耗时 37.21s。
- 唯一门禁失败：`test_xlsx_backfill.py::TestManualReviewProvenance::test_至少一份xlsx存在`，原因是仓库根的两份 xlsx 工作单均不存在。
  > ⚠️ 这是**审查当时的基线快照**。同日晚些时候该失败已随 xlsx 测试清退消失
  > （见 §M-24 收口），门禁现为 **0 failed / 0 skipped**——上面这行数字**不可复现**。
- 环境隔离复测：清空六个 LLM 环境变量后，`test_executor_mode.py` → **46 passed / 1 failed**。
- 兼容性复测：Python 3.10 编译 `arbitration.py` 报 f-string 反斜杠语法错误；当前测试实际运行在 Python 3.14。

### H-10 · 候选侧缺账被当成 0

**代码事实**：[`_ratio_diff()`](../../executor/dual_run.py#L347) 只在 `base` 为空时返回 `None`，候选为空时却通过 `(cand or 0.0)` 代入 0。因此 `_ratio_diff(10.0, None) == 1.0`，而反向 `_ratio_diff(None, 10.0) == None`。

**影响**：`None` 的业务含义是「没有采到、不可判定」，0 的含义是「采到了，数量为零」。两者混用会把数据缺口判成候选能力退化，最终把应返回 `incomplete` 的双跑结果误判为 `fail`，形成假负样本或错误 go/no-go 结论。

**修复建议**：在计算差值前同时检查 `base` 与 `cand`；任一侧为 `None` 都返回 `None`。同时保留 `base == 0` 不可判的规则。

**验收条件**：增加参数化测试，至少覆盖 `(10, None)`、`(None, 10)`、`(None, None)`、`(0, 10)`、`(10, 0)`；前四组返回 `None`，最后一组返回 `1.0`；并断言候选缺账时整条判据为 `incomplete`。

### H-11 · 面比较把计数当集合

**代码事实**：[`_same_surface()`](../../executor/dual_run.py#L361) 直接比较两侧的 `SideSummary.surfaces`，而该字段是 `Mapping[str, int]`。`{"obscura-1": 2}` 与 `{"obscura-1": 1}` 会被判为不同面；`{"obscura-1": 1, "camoufox-1": 1}` 两侧相同却会被判为同面。

**影响**：面内元素中位数是否可比取决于运行次数，而不是浏览器面本身，可能造成两类错误：同一面的两个目录被拒绝比较，或混合目录被当成同一口径直接比较。后者会把不可解释的差值写进判据。

**修复建议**：先提取每侧出现的面集合。只有每侧恰好一个已知识别面，且两个面相等时才返回 `True`；空面、未知面、任一侧多面均返回 `False`。计数仅用于报告，不参与同面判定。

**验收条件**：覆盖同面不同计数、同面同计数、两侧相同混合面、单侧混合面、未知面、空面六类输入；并断言混合面场景下元素差异判据为 `incomplete`。

### H-12 · camoufox 空枚举丢失过滤账

**代码事实**：[`observe()`](../../executor/browser/camoufox_driver.py#L948) 成功调用 `interactive()` 后，用 `if elements` 决定是否读取 [`last_enumeration`](../../executor/browser/camoufox_driver.py#L955)。当页面元素被词表、空名称或同名规则全部过滤时，`elements == ()`，但 `last_enumeration` 中仍有 `nodes`、各过滤档位和 `kept=0`，这些账目会被丢弃。

**影响**：`Observation.element_basis.face` 变成空账，与「本次没有执行枚举」或「枚举失败」同形。归因表无法证明元素是页面不存在，还是被过滤规则全部拦掉，跨面质量判断会失去关键证据。

**修复建议**：将「是否成功枚举」与「返回元素是否非空」拆开。可以先用 `try/except DriverError` 标记失败，成功分支无条件复制当前页的 `last_enumeration`；失败分支禁止复用上一页账目。

**验收条件**：模拟 `interactive()` 成功返回 `()`，同时 `last_enumeration={"nodes": 5, "role_unmatched": 5, "kept": 0}`，断言观察中的 `face` 账完整保留、`degraded` 不含 `interactive`，且归因结果能显示 `role_unmatched=5`。

### H-13 · Python 版本声明与实际语法不一致

**代码事实**：[`pyproject.toml`](../../../pyproject.toml#L5) 声明 `requires-python = ">=3.10"`，但 [`arbitration.py`](../../executor/arbitration.py#L219) 在 f-string 表达式内使用了含反斜杠转义的字符串。Python 3.10/3.11 不允许该写法，Python 3.12 才放宽。

**影响**：依赖解析会把包安装到 Python 3.10/3.11 环境，但导入模块时发生 `SyntaxError`。这不是运行期降级，而是包元数据错误，CI 只跑 3.14 无法发现。

**修复建议**：二选一并写明决策：保持 3.10 支持，则把 f-string 内部表达式提前算成局部变量；只支持 3.12+，则把 `requires-python` 改为 `>=3.12`，同步文档和 CI。

**验收条件**：若声明 3.10，加入 3.10 语法编译门禁并全量跑关键契约；若声明 3.12，加入版本元数据测试，禁止再次出现声明低于实际语法要求的组合。

### M-22 · 共享口径缺少最小覆盖与配对约束

**代码事实**：[`compare-drivers`](../../executor/dual_run.py#L487) 读取两侧 `shared_ledger["kept"]` 后直接按 30% 阈值判定；[观察汇总](../../executor/dual_run.py#L301) 只累计共享样本数，没有把覆盖率、task 交集或配对完整性送入判据。两本账可以来自不同 task，也可以各自只有一条共享观察。

**影响**：共享口径原本用于解决跨面可比问题，但样本极少时中位数没有统计意义。若 A 的唯一样本来自 task 1、B 的唯一样本来自 task 2，比较的甚至不是同一页面，仍可能产出 `pass/fail`。

**修复建议**：至少加入三项约束：每侧共享观察覆盖率下限、参与判定的 task 必须双侧都有有效共享账、单个 task 的最小观察数。未达门槛统一返回 `incomplete`，并在详情中报告覆盖率与缺失 task。

**验收条件**：覆盖「两侧各一条且 task 相同」「各一条但 task 不同」「一侧覆盖 1/100」「双侧 task 交集为空」四种场景，全部不应产出 `pass/fail`，除非配置明确允许低覆盖诊断模式。

### M-23 · `executor` 对感知实现的架构依赖

**代码事实**：[架构总览](../../docs/01-架构总览.md#L86) 将“`executor` 只依赖 `perception` 协议”列为不可破约束；实际 [`branches.py`](../../executor/branches.py#L33) 和 [`gatekeeper.py`](../../executor/gatekeeper.py#L58) 直接 import `perception.rule_perceptor` 的 `has_strong_paywall_signal`、`ACCESS_HINTS` 等实现细节。

**影响**：规则版与 LLM 版切换不再只受协议影响。感知实现调整词表或纯函数签名时会直接破坏 executor；依赖收拢文档与代码事实不一致，后续替换者也会误以为边界已经封闭。

**修复建议**：优先把无状态词表和纯函数移到 `common/` 或独立协议地基，由 executor 与 perception 共同依赖；若业务上必须保留在 perception，则修订架构约束，明确哪些纯函数属于稳定协议，并增加 AST 门禁防止继续扩大。

**验收条件**：AST 门禁能覆盖三类导入：executor 导入 perception 实现、perception 导入 executor、executor 导入 llm。修复后文档描述、门禁规则和实际 import 图一致。

### M-24 · xlsx 门禁语义不一致 —— ✅ 已收口（2026-10-21）

**代码事实（问题当时）**：`xlsx_by_id` fixture 在 xlsx 缺失时 `skip`，
`test_至少一份xlsx存在` 对同一事实硬失败，`test_inc37_validator_pass.py`
的 `xlsx_pending_ids` 又选择 `skip`。

**影响**：干净 clone 中门禁同时表达「缺文件可跳过」和「缺文件必须失败」。
用户无法判断该红是环境缺件还是发布阻断；后续测试也容易被 skip 静默掩盖。

**收口方式（不是修，是参照物消失）**：两份 xlsx 工作单已被人为删除且
**不可恢复**——`.dumate/` 里只有 2026-10-12 的早期快照（254 行
`review_label` 全空、23 行只填 5 条），重建不出终版，因此
「xlsx 是发布必需还是可选审计输入」**已经没有可执行的答案**。
按「可选外部证据已永久缺失」处置：

| 动作 | 内容 |
|---|---|
| 删 11 条 | `test_xlsx_backfill.py` 6 条（含那条硬失败的 `test_至少一份xlsx存在`）+ `test_inc37_validator_pass.py` 5 条 |
| 留 5 条 | 纯 `annotations.jsonl` 断言：INC32/INC33 追溯钉、manual-review==77、round 294+12、INC37 分布容差、inc33==18 |
| 连带 | 两文件不再 import `openpyxl`（依赖仍留 dev 组，见 `pyproject.toml` 注释）；文档 8 处「1 failed 是 xlsx 环境项」同步改写 |

**验收（已过）**：全量门禁里 **xlsx 相关的 fail / skip 归 0**（当时总条数 3398→3443，因工作区并发编辑而浮动；同期另有并发编辑带来的红灯，与本次清退无关，见 09 §当前状态）；
「两份都不存在」「仅一份存在」「两份存在」三种状态**都走同一条路径**
（没有 xlsx 代码了），语义矛盾从结构上不可能复现。

**代价（写在明处）**：77 条 manual-review 与人工工作单的 1:1 关系、
199 条 pending 的升级完整性、31 条 flagged 的 `INC37_NOTE` 三条封条**已丢失**。
要恢复只能重导一份工作单，或退一步用「冻结基线」（把当前
`annotations.jsonl` 的 77 行冻成入库 JSON）——但那只是防漂移，
**不再证明来源是真人**，做之前先明确它守的是什么。

### L-14 · 测试依赖宿主机 LLM 环境变量

**代码事实**：`Test参数门` 已定义 [`clean_llm_env` fixture](../../tests/unit/test_executor_mode.py#L263)，但 [`test_拼错则挡下且不静默跑成scripted`](../../tests/unit/test_executor_mode.py#L245) 没有使用。该用例只设置 `TRAJECTORY_EXECUTOR_MODE`，其余 LLM 变量继承宿主机环境。

**影响**：同一提交在不同开发机上结果不同。清空 LLM 环境变量后得到 `46 passed / 1 failed`，说明参数解析测试实际上混入了后端配置状态，不能作为稳定门禁。

**修复建议**：给该用例加 `clean_llm_env`，或在测试类级使用 autouse fixture 清空所有相关环境变量。需要测试后端可用性时，再由用例显式设置。

**验收条件**：在全部 LLM 环境变量为空、全部配置、部分配置三种宿主环境下运行同一组测试，结果一致；并保留参数优先级和环境变量回退的独立测试。

### L-15 · replay 返回半截账

**代码事实**：[`_element_basis_of()`](../../perception/replay.py#L169) 的注释承诺形状不对时返回 `None`、不返回半截账；实现却逐侧 `continue`。输入 `{"shared": "bad", "face": {"nodes": 9}}` 会返回仅含 `face` 的映射。

**影响**：下游可能把半截账当成“已完整测量”，从而在缺少共享口径时仍使用面内账，或让归因报表误以为某一侧不存在。该问题与 H-10 的缺账语义相互放大。

**修复建议**：若采用严格 fail-closed，任一已出现的侧账类型错误就返回 `None`；若业务确实允许部分账，则改为显式状态对象，记录哪一侧缺失及原因，不能只返回残缺 `Mapping`。

**验收条件**：覆盖完整账、缺单侧、单侧类型错误、单个档位非整数、空映射五类输入；断言返回值与事先声明的完整/部分语义一致，并增加 P1→replay round-trip 测试。

---

## 待 owner 拍板（不是「还没做」，是「不能自己决定」）

| ID | 决策 | 锚点 | 两条路的代价 |
|---|---|---|---|
| **Q-01** | 簇 A 出路：`page_quotes` 是否纳入救济输入源 | [00 §不能做](00-总览.md) · 编年史 §增量四十四 | 纳入 = 「信 LLM 自报」，**触碰纪律边界**；不纳入 = 转攻采集层（H-03/H-04） |
| **Q-02** | 簇 C 题绑定是否按 LLM reason 动态派分支 | 编年史 §增量六十 · [04 §建议优先级](04-探针七层域.md) | 同上——动态派分支 = 让 LLM 决定贴哪个标签。**方案 Z（LLM self-reported 门）默认禁**，除非 owner 明批 |
| **Q-03** | m.ixigua 金标语义（H/L-10）谁来复核 | [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | 上游标注问题，代码层无解 |
| **Q-04** | 存量 rationale 重生成的**配额**何时给（M-02） | [05 §风险与未做](05-rationale与闸门域.md) | ≈796 次 LLM 调用；不给配额就一直停在「事后反思口径」 |
| **Q-05** | D-11 camoufox 是否过 go/no-go（H-08） | [设计方案/04 §6.1](../设计方案/04-camoufox执行层替换方案.md) | 拦截率不达标就不换；换了要重标定 `elements_total` 口径（D-2 打分口径漂移风险）。✅ **子问题已闭（2026-10-21）**：执行口径定为**无人值守走 `headless=True`**（增量六十九）。⚠️ **go/no-go 本身仍未闭**——六条判据没过。**判据②③的「差值不可比」已闭**（2026-10-21，增量七十：共享口径 + 归因表落地）。✅ **读数也拿到了（增量七十二）**：headless、3 task × 2 面、开 `--element-basis`——**判定仍 `fail`**。★**但「fail」现在要分着读**：① **headless 拦截率这条前置门已过**（真·反爬 B 9.09% < A 14.89%，配对子集同向）⇒ **不构成否决项**；② 判据①⑤⑥ **还没闭**：①缺可比口径（H-14）、⑤人判（已抓到 9 条假账，H-15）、⑥**连工具都没有**（M-25）。⭐ **owner 要拍的是**：在这些闭环之前，`DEFAULT_KIND` 是不是继续按 §6.1「不许切换」执行（当前是）——**还是先做 H-14/H-15/M-25 这三件**。⚠️ 增量七十一那条修正是**这次读数的前置**：共享口径测失败时旧代码把失败混进 `Observation.degraded`（⇒ `extract_candidates` 不取候选 ⇒ 整批零样本且零报错），现在失败走 `element_basis_error` 独立通道。✅ **已闭（2026-10-22，增量七十九）——go/no-go 被 owner 裁定绕过**：owner 判「**不再双跑**，选定 camoufox 做默认浏览器调用主力——**反爬拦截与页面内容获取是主要，速度次要**，可接受增加等待时间」。⇒ §6.1 那六条判据**不再是切换的门槛**（它们是「双跑可比」路线下的门槛，而 owner 判的是「不双跑了，直接换」）。⚠️ **但覆盖 ≠ 六条读数作废**：① 真·反爬 B **9.09%** < A **14.89%**、② obscura 抽不出客户端渲染页正文（A 侧 60% 的站点「没测到」）、⑥ 动作可移植 16/16 —— **三条支持裁定的方向**；③ `elements_total` 两面**不同义** ⇒ **不可混池**这条**继续有效**；④ 合并口径（反爬+登录墙）A 19.15% / B 34.09% 与①**相反**——⚠️ **owner 的裁定针对「被拦截」，按①的真·反爬口径 camoufox 更好**，④ 里混的登录墙**不是浏览器能解决的**（那要账号），不该算进「浏览器好不好」的账；⑤ 是**分支体系**的问题，与执行层无关。⇒ 落地：`DEFAULT_KIND = "camoufox"`（`driver_factory.py`），`per_site_timeout_s` 90→180 并暴露 `--per-site-timeout`，P6 指纹进 P1，探针三条命令一起接。⚠️ **尾巴两件**：① ~~**第一批 camoufox 真实批次还没跑**~~ ✅ **已跑**（2026-10-22，12/12，退出码 0；池 181→260、P2 543 份、**`required` 重算后无需改动**）——⚠️ **判据①（真·反爬拦截率）与判据②（`body_text`）不在这批上取**：那批是**修复前**的产物（**15% 的观察是「读到一半」**，增量八十）⇒ **要等第二批（修复后）再取这两个数**；② ~~**第一批落盘后 `Test存量口径` 会红** ⇒ **重算 `required`**~~ ✅ **已重算、无需改动**（`Test存量口径` 改成**逐面各自核**——期望值不能取 `toolcatalog.CATALOG`，那是 obscura 面的定义集，用它核 camoufox 面**必然不等**） |
| **Q-06** | 旧 per-unit LS 路径 2026-11-09 到期后删还是续（M-07） | [设计方案/14 §9](../设计方案/14-完整任务视图与LS上传.md) | 删 = 依赖方断；续 = 两套口径继续并存，**并池口径分歧会有人读成质量变化** |
| **Q-07** | `partial_free` nav-bar 形态在黄金集里有没有真例（决定方案 W/X 边界，H-07 前置） | 编年史 §增量五十八～六十 · [04 §建议优先级](04-探针七层域.md) | 有真例 → 词表 e2e 先跑；没有 → 方案 X 已够 |
| **Q-08** | **rubric 变更是否强制连带改 prompt**（方案 16 **D-C7**，阻塞 C-10） | [方案 16 §2.2](../设计方案/16-评估器与rubric双向校准回路方案.md) | 强制 = prompt 与准则不再各走各的，但每改一条款都要重跑 `agreement`（人时）；不强制 = **改档不改 anchor 时没有机器能发现**，评分口径悄悄分叉（现在就各走各的）|
| **Q-09** | **合并等价类是否需 owner 签**（方案 16 **D-C4**，阻塞 C-13） | [方案 16 §2.2](../设计方案/16-评估器与rubric双向校准回路方案.md) · [`golden/builder.py`](../../evaluation/golden/builder.py) | 要签 = 多一道人工程序；不签 = **最省力的提分路径无人记账**（合并等价类能让 D-10 涨，而「合并后不可见的细分」永远查不回来）|
| **Q-10** | **阈值调整算不算 rubric 级变更**（方案 16 **D-C6**，阻塞 C-14） | [方案 16 §2.2](../设计方案/16-评估器与rubric双向校准回路方案.md) | 算 = 调线要人裁 + 版本记录；不算 = 「差一点点」会成为长期借口，**触发机制变成装饰** |

---

## 环境 / 外部依赖（不是代码题，别当 bug 修）

| 症 | 真因 | 出处 |
|---|---|---|
| ~~`test_xlsx_backfill` fail~~ | ✅ **已消失**（2026-10-21）：两份 xlsx 已被删除且不可恢复，依赖它们的 11 条测试清退、5 条纯 annotations 断言保留，**门禁现为 0 failed** | [09 §xlsx 测试清退](09-门禁与纪律域.md) |
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
- **改人工复核端的 label 去凑自动化结果**：`manual-review` 那 77 行是人类输入，追溯只改 `annotations.jsonl`。（原规矩是「不许改 xlsx 工作单」，那两份表 2026-10-21 已删除，M-24 已收口。）
- **把 LLM 建议写成 `review_label`**：那是把 `ai-assisted-unconfirmed` 伪装成 `manual-review`，违反 D-9 边界③。
- **手工抄第二份工具清单**：真源只有 [`toolcatalog/`](../../toolcatalog/)（增量六十二）。
- **读仓库根 `output/`**：新树产物一律 `trajectory_pipeline/output/pipeline/`（D8）。
- **`import` 存量 `archive/v1/`**：只读参考，照着读再重写。

---

## 记录文档地图 · 「这个结论记在哪」

| 想找什么 | 去哪 | 备注 |
|---|---|---|
| 某条设计**为什么**长成现在这样、哪次改的 | [90-增量编年史](90-增量编年史.md)（增量五十～七十八） | 切换到 agent runtime 执行器**之后**的批次；切换前的批次已删除 |
| 某块**现在跑到哪**、差什么 | 本台账 + [01](01-采集执行域.md)～[09](09-门禁与纪律域.md) 域文档 | 域文档管「现状叙述」，台账管「待办清单」 |
| 字段级契约 | [contracts/](../contracts/)（P1/P2/P3/T）+ [03-数据契约](../03-数据契约.md) | 双向测试盯着，改字段先改契约 |
| **未实施**的方案构想 | [设计方案/](../设计方案/) | 已实施的在编年史，不在方案里。**例外：方案 16（校准回路）是活文档**——实施结果块就写在它自己的 §9 里，`C-xx` 的闭环口径也在那儿 |
| **校准回路**（快照 / 分歧 / 分诊 / 两轮验证 / 闸门）跑到哪、怎么跑 | [方案 16 §9](../设计方案/16-评估器与rubric双向校准回路方案.md#9-任务执行计划)（做完的）· [本台账 §校准回路](#校准回路方案-16-未闭环)（没做的）· [04-运行手册](../04-运行手册.md)（命令） | 三条判据（净改善 / 类回归 / 回归集）+ 第四条（同桶清零）的输入是分诊档 |
| 踩过的坑、症状原文 | [02-避坑指南](../02-避坑指南.md)（9 类） | 按终端里的原话搜。**这里不再写条数**——它每次加一条就漂一次，而「9 类 72 条」在 2026-10-23 核时已是 77 条 |
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
| 00「门禁 2368 passed / 1 failed」 | **2026-10-21 实测 3373 passed / 1 failed / 10 skipped**（增量七十六；failed 仍是 `test_xlsx_backfill` 环境缺失；**上一轮是 3361 passed / 1 failed / 10 skipped**（同增量，增 `CAUSE_KEEP` 5 条测试前）；**上一轮是 3357 passed / 1 failed / 10 skipped**（增量七十四）；再上一轮的 66 errors 是 L-13 的 `pyyaml` 缺 `__init__.py`，**增量七十二已修好**——0 errors）；**再上一轮是 3334 passed / 1 failed / 10 skipped**（增量七十三）；**再上一轮是 3295 passed / 1 failed / 15 skipped**（增量七十）；00 是 2026-10-15 快照，增量六十记 2954）｜✅ **增量七十七时的实测是 3393 passed / 1 failed / 10 skipped**（唯一 failure 仍是 `test_xlsx_backfill` 环境项）——中途一度读到 **26 failed**，那是工作区里**未提交的 H-16 付费墙口径改动**的**旧断言**（旧测试仍按扁平词表口径写），那批改动与测试同步合上后已回到 1 failed；**本增量自己的 37 条探针测试全绿** ｜⚠️ **2026-10-21 最新：0 failed / 0 skipped**（总条数 3398→3439 浮动，见 09）——xlsx 测试清退（§M-24 收口）后**那个 1 failed 已经不存在**，上面整串「1 failed」都是历史读数 | `uv run python -m pytest -q` |
| 00「编年史（增量二十八～四十四，1275 行）」 | 现到**增量七十八**；**编年史的实际范围是增量五十～七十八**（增量二十八～四十九已删，`07-增量档案.md`（增量一～二十七）也已删）。✅ **CLAUDE.md / 00-总览 / 05-现状与验收三处的「增量二十八～六十八」已于 2026-10-21 改掉**；**「增量五十～六十九」→「增量五十～七十」同批改掉（增量七十落地）**；**「增量五十～七十」→「增量五十～七十一」→「增量五十～七十二」→「增量五十～七十四」→「增量五十～七十五」→「增量五十～七十六」→「增量五十～七十七」→「增量五十～七十八」各改一次（增量七十一 / 七十二 / 七十四 / 七十五 / 七十六 / 七十七 / 七十八落地）** | `grep -n "^## " 90-增量编年史.md` |
| 00「完整清单与清空前的全部实测数字在**增量档案**的『附录 A–D』四节里」 | ⚠️ **悬空引用**——`07-增量档案.md`（4162 行）已删除，那四节不在了 | `git show HEAD:trajectory_pipeline/docs/07-增量档案.md` |
| 09「`cli.py` 2978 行」 | **3535 行**（2026-10-21 复核） | `(Get-Content trajectory_pipeline/executor/cli.py).Count` |
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
