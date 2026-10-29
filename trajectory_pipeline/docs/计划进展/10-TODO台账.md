# TODO 台账 · 全域未闭环项汇总

> **这一份是 TODO 的唯一汇总入口。** 只收**未闭环**的事；已经闭环的留在
> 各域文档与编年史里，**不进台账**——台账里出现已闭环项，就说明它开始骗人了。
>
> **快照日期：2026-10-23**（**同日再补 M-29 / M-30**：D-5 恒真修完之后，
> 台账要跟上两件事——① **v7 快照的 d5 pred 还是修复前的**（208 条全 5，
> 见 §M-29）；② 复算 d10 金标发现**金标在 URL 粒度上就不自洽**
> （19/131 个 URL、33 张卡里 11 张，见 §M-30），L-10 的范围被低估。
> **同日 M-26 / M-27 也已闭环（2026-10-23）**：M-26（媒体计数不再许诺成功）见
> [02 感知与判定域](02-感知与判定域.md) · M-27（点击失败记 `unresolved`，采集缺件
> ≠ 业务负样本）见 [03 标签与分支域](03-标签与分支域.md)——两单都已移出本台账。
> **两单均已闭环（2026-10-23）**：M-29（v8 快照的 d5 pred 已是当前评分器的输出，
> 见 §M-29）· M-30（19 个 URL 的定性全部定稿 = 18 漂移可解释 / 1 不可能共存 / 0 待查，
> 见 §M-30 现场重采）。当时的施工单是**过程件**，两单完成后已删除；
> **改法、期望读数、禁区都折进了 §M-29 / §M-30 两节**。
> M-30 同批复算还翻出一条没入过档的
> **切档按行不按 URL**（65/101 持有集行与调参池同 URL）⇒ 新增 **Q-11 / Q-12**（均已裁定）。
> D-5 恒真**本身已闭环**（第四条检查【结论的证据核】，测试
> `test_d5_scorer.py::Test证据核` 封着，记录在
> [对齐账 §2.4](../设计方案/02-rubric与代码对齐.md) 与
> [避坑 §1.3c/§1.3d](../02-避坑指南.md)）——**闭环的不进台账**，只有它
> 留下的两个尾巴进；
> **同日补入[校准回路（方案 16）一节](#校准回路方案-16-未闭环)**：
> **C-05 / C-06 / C-12 / C-18 / C-16 / C-19 / C-07 已闭环、当日移出**；**2026-10-23 再闭 C-10 / C-11 / C-13 / C-14 并移出**（三条裁定 D-C4 / D-C6 / D-C7 同日落地，**方案 16 的 C-xx 全部清空**），
> 余 Q-01～Q-07 / Q-13 待裁（Q-08～Q-12 已闭）。台账此前**完全没收过 `C-xx`**——方案 16 是独立的施工序列，
> 但它阻塞 H-02 的方向判定，所以必须在这里可见；
> 2026-10-21 补记 H-08 / Q-05 / M-19 / M-21 / L-12 / L-13；
> 同批补入质量审查 H-10～H-13 / M-22～M-24 / L-14～L-15；
> **再补入第二轮双跑后的 H-14 / H-15 / M-25 / L-16**；
> **2026-10-22 补记 owner 裁定切 camoufox（增量七十九）：H-08 / Q-05 闭 + M-21 缩小**；
> **2026-10-22 补记「读到一半」（增量八十）：H-08 / Q-05 尾巴重开**——第一批批次实测出
> **15% 的观察是半读** ⇒ 判据①/② 要等**修复后的第二批**再取）。
> **2026-10-22 补记第二批跑完（增量八十一）：H-08 / Q-05 尾巴收口**——判据①/②
> 已取（真·反爬 **8.55%**、`body_text` **800.5**）、修复前那一代**整代回滚**；
> **新补入 H-18 / M-26 / M-27 / M-28 / L-18 / L-17**——本批逐条读 evidence 读出的
> **三处代码层缺陷**（媒体计数短路 / 点击失败记成业务分支 / `unreachable_hard`
> 的分类口径）、**一处门禁假阳性**与**一处证据链缺环**（`last_action` 没进 P1）。
> **六条已按 [§2026-10-22 新增开口项详解](#2026-10-22-新增开口项详解) 补上
> 「代码事实 / 影响 / 修复建议 / 验收条件」**——下一位动手的人不必重新推导一遍）。
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

## ⏸ 停点（2026-10-26 · 执行层换 camofox-browser）

> **一句话**：第一梯队的**代码、门禁、数据**都已落盘并合并进 `main`；
> **唯一没做完的是「再真跑一批」**——执行层已从 camoufox 换成
> **camofox-browser**（owner 裁定，走它自带的 MCP 适配器），
> 代码侧全部落地并过门禁，**但还差一次真跑**。
> 下面四段是**交接面**：现状 / 进度 / 设计 / 修改方案，
> 外加**要人拍板的口径题**与**跑通前必须重跑的验证项**。
>
> ⚠️ **这一节 2026-10-26 整节重写过**：上午那版写的是
> 「**恢复 camoufox**」（`uv pip install camoufox==0.5.8 && camoufox fetch`），
> 那条路**已被 owner 明确禁止**（原文：「**禁止把原有的 camoufox 接回来**，
> 可以做清理原有的 camoufox 代码与内容」）⇒ **上午那版的步骤 1 作废，
> 照着做就是回退**。
>
> ⚠️ **2026-10-27 更新：真跑已经开始了，但连撞五个「零报错」的缺陷**——
> 详见下面 §停点 · **R5 实跑那一段的五个缺陷**。
> **五个都只有「逐条打开存档读 evidence」看得见**（门禁全绿、测试全绿）。
> ⇒ 停点的形状没变（**还是差一次真跑**），但**「差一次真跑」这句话的含金量变了**：
> 它不是「跑一下就完」，是「**跑 → 读 → 发现缺陷 → 修 → 清 → 再跑**」。
>
> ⚠️ **2026-10-27 追加：第 5 轮拆成**两条流并发**，并**砍到 6 条**（owner 建议）**——
> A 流 `plan-camoufox-prod1-rest.json`（**只跑 T038 · T048 · E028**）·
> B 流 `plan-camoufox-prod1-rest-b.json`（**T057 · E014**）；
> 连同已跑的 T004 ⇒ **这批 6 条**。**砍掉的 4 条**（T044 · T018 · T030 · T025）
> 是**同一个标题《功夫》、同一条检索式**的 persona 变体 ⇒ R5 的验收标准是
> **结构性**的（指纹 / 降级归位 / 失败落在哪个路由 / 同 URL 不矛盾）⇒
> **与 persona 无关 ⇒ 对验收零增量**；代价是这批的 persona 格从 12 降到 8
> （这批是 12 条小样 ⇒ 试点性质，**可逆**：被砍的还在计划文件里）。
> **`batch_id` 两边都是 `plan-camoufox-prod1`**（拆的是跑法，不是批次身份）。
> 并发**为什么安全**（每个 Run 各起一个 MCP 适配器 ⇒ 上游各拿各的会话）
> 与**并发带来的唯一真风险**（资源挤 ⇒ 导航 30～35 s 硬超时可能被打满 ⇒
> **假 `unreachable_hard`**）见[编年史 §5-1](90-增量编年史.md)。
> ⇒ **铁律：并发批里每一条 `unreachable_hard` 都要单独复核**，不许因为
> 「它是站点打不开」就放过。**探针 / 人核与批次互斥**（上游 `lock_queue`
> 会销毁别人的页）⇒ **并发的是两条批次流，不是批次 + 探针**。
>
> ⚠️ **2026-10-27 晚：M-44 口径落地 ⇒ 生产语料主力的 28 个任务开始**串行重跑**
> （批次 `rerun-prod-m44-2026-10-27`，计划
> `output/pipeline/plan_rerun_prod_m44.json`，**28 条 / 28 个不同 `task_id`**）。
> 触发它的是一次**语料审查**：盘上 89 份 P1 存档里**只有 9 份在现行面
> （camofox）+ agent 形态**下、覆盖 **6** 个 `task_id` ⇒
> **其余 28 个任务在现行面上是空的**（逐条读数见
> [编年史 §增量九十五](90-增量编年史.md)）。
> ⚠️ **串行不是保守，是硬要求**：上游 `lock_queue` 会销毁别人的页 ⇒
> 并发跑 = 互相毁页（第 4 轮那 9 条假负样本就是这么来的）。
> ⇒ **本批跑完仍要走那一套固定动作**（逐条打开存档读 evidence → 重建产物 →
> 门禁 → 并池干跑），**且新批次的人判读数要按代分开看**
> （**e6** 是 M-44 的分代边界——D-10 一致率**跨代不可直接比**）。

### 现状

**环境（停点的成因）**

| 读数 | 值 | 说明 |
|---|---|---|
| 执行层 | **camofox-browser** | owner 裁定（2026-10-26）；**camoufox 不再是执行层** |
| `camofox-browser-mcp` | **已装（2026-10-27 19:24 实测在 PATH）** | `npm i -g @askjo/camofox-browser-mcp`（或 `cd camofox-browser/mcp && npm link`）；也可设 `CAMOFOX_MCP_EXE=<绝对路径>` |
| REST 服务 `:9377` | **已起（2026-10-27 19:24 实测 HTTP 200）** | `cd camofox-browser && npm start`（**本仓库不启动浏览器**——浏览器由上游服务持有） |
| `camoufox`（venv） | **未安装**（**且不许装**） | owner 裁定「禁止接回来」；`cloverlabs-camoufox` 只留在 `legacy` extra，**只服务 v1 冻结树**，还**同名抢 import 名** |
| `playwright` | **已从主依赖移除** | 它留在主依赖的唯一理由是 `camoufox_driver.py` import `playwright.async_api.Error`；那个模块已删 ⇒ **留着就是「声明与代码不一致」** |
| 内存 / 提交 / 磁盘 | 物理可用 9.0 GB · 提交可用 12.2 GB · C 盘可用 595 GB | **不是资源问题**（实测 2026-10-26 15:30） |
| `DEFAULT_KIND` | `"camofox"` | ⇒ **生产链默认执行层是 camofox**；`obscura` 回溯路要 `OBSCURA_EXE` |

**上午那个症状（2026-10-26 15:18 那批 10 条 agent 批）**：每条起浏览器都撞

```
playwright._impl._errors.TimeoutError: BrowserType.launch: Timeout 180000ms exceeded
Crash Annotation GraphicsCriticalError: |[0][GFX1-]: RenderCompositorSWGL failed mapping default framebuffer
```

**每次启动烧 6 分钟**（重试一次再挂）⇒ 10 条一条没跑成，已终止。

⚠️ **别把它读成「代码坏了」**——同一份代码在 12:56 / 13:21 / 13:57 跑成了
E022 / T054（**agent 链**真跑，`driver=camoufox`），存档就在盘上
（`E022__01fcb42b` / `T054__1c9b6260`）。
⚠️ **也别把它读成「camoufox 这条路还有救」**——那 6 分钟是 **playwright 直连起
Camoufox 内核**的代价（窗口不可渲染 ⇒ 启动超时）。camofox 那条路**浏览器由上游
REST 服务持有**，本仓库不碰起浏览器这件事 ⇒ **这个失败模式整体消失**。
⇒ 下午换成 camofox 时**顺手退役了「起浏览器重试」**：camofox 侧起不来只有两种
原因（适配器不可执行 / REST 没起），**都不是偶发** ⇒ 重试只是把同一个失败
重复一遍再烧时间。

### 进度（第一梯队三项）

| 项 | 状态 | 差什么 |
|---|---|---|
| ① 真跑一批，让 R1–R3 上真数据 | **半程** | R1/R2/R3 的**代码与门禁**全落地并已合进 `main`；**第 4 轮那 10 条已在 camofox 面上跑成**（run_id `734b7723` / `cd571143` / `a933bea6` …，批次 `plan-camoufox-prod1`）⇒ **camofox-1 面的工具声明已按真实调用转正**（`click`/`fill`/`press` 三条 `exercised=True`）；⚠️ **但那一批自己带出缺陷⑤**（假 `unreachable_hard`）⇒ **第 5 轮在跑**（拆开建页与导航之后重跑，同一条命令）⇒ **读数待补** |
| ② H-17 那 7 条假负样本的「转正」路 | **成** | `downgrade` + `pool-drop` **两步**已落 + 契约测试 `tests/contract/test_pool_exit_sequence.py` 封着；**实际处置待人执行**（操作手册见 [07 §2.7](07-人工复核与判读手册.md)） |
| ③ 文档尾巴（H-03 三处 / M-19 增量六十一 / 5 个 `tmp_probe_*.py`） | **成** | 已随 `879899a` 合进 `main` |
| ④ **执行层换 camofox** | **代码与门禁成 · 真跑已跑过一轮** | 设计见 [设计方案 21](../设计方案/21-camofox-browser执行层接入方案.md)；**环境已通**（适配器 + REST `:9377`）⇒ **真跑跑成过**；**差的是「跑 → 读 → 发现缺陷 → 修 → 清 → 再跑」这个循环走完**（已走四轮，见下 §R5 实跑那一段的五个缺陷） |

### 设计（已定，**别再改**）

1. **面由 P1 的 `driver` 一对一折算**——面**不手填**，认不出的 driver 直接抛。
   本轮实测：agent 链存档带 `driver=camoufox` ⇒ 采集侧账的 **pred 面不再是
   `unrecorded`**（这是 R3 那条「账要等下一次真跑」的前置条件）。
2. **「无题产地」有三类**（本轮补上第三类）：站点首页核 / 采集侧 `blocked` /
   **gatekeeper 代码判定的收口分支**。D-5 的配对表**从执行层推导**，不手抄——
   手抄必然漂，而漂了的症状是**判反**（把对的判成错），比不判更贵。
3. **一条没跑成不许毁整批** + **批次身份不再退化成常数**：`batch_id` 取计划里的
   字段（缺省退文件名，**绝不取 `--task-id`**——那个 argparse 默认值是 `"T001"`，
   曾让 12 条那批的存档 `batch_id` 全变成 `"T001"`，而那批根本不含 T001）。
4. **（2026-10-26 新增）三张面，两个词表**：`obscura-1`（只读存量）/
   `camoufox-1`（**已退役，只读存量**）/ `camofox-1`（**生产面**）。
   前两张**只读不再产出**（`DRIVER_SURFACE` 里没有它们的驱动名），
   但**仍读得懂**（`LEGACY_DRIVER_SURFACE`）⇒ **退役不等于失忆；
   删的是实现，不是证据**（430 份 P2 样本 + 14 份顶层 P1 存档靠这条活着）。
   ⚠️ `camoufox-1` 与 `camofox-1` **同方言**（`aria_role`）但**不同声明**：
   camoufox 面说 `get_by_role(role, name=name, exact=True)`（**精确**），
   camofox 面上游是 `getByRole(role, {name}).nth(nth)`（**子串**）⇒
   camofox 面的说明里**不许写 `exact`**。

### 修改方案（下一步按这个序做）

1. **起上游 REST 服务**：`cd camofox-browser && npm start`（默认端口 9377）。
   ⚠️ **本仓库不启动浏览器**——浏览器由上游服务持有，
   **有头/无头不由本仓库决定**（`--headful` 已退役，见 [方案 21 §R4](../设计方案/21-camofox-browser执行层接入方案.md#r4-收尾与接线)）。
2. **装 MCP 适配器**：`npm i -g @askjo/camofox-browser-mcp`
   （或 `cd camofox-browser/mcp && npm link`）；也可设
   `CAMOFOX_MCP_EXE=<绝对路径>`。
   ⇒ 两者都齐了才跑得起来：**适配器在 PATH 上**（或由 env var 指到）**+
   REST 服务在 9377**。缺任一个 ⇒ `cli` **启动前退 2** 并给明确安装指令
   （`cli._execution_layer_missing_reason`），**不逐条刷 ImportError**。
3. **重跑那 10 条**：`research/ab_baseline/plan-camoufox-prod1-rest.json`
   （T004 / T038 / T048 / E028 / T044 / T057 / T018 / T030 / T025 / E014），
   批次身份 `plan-camoufox-prod1`。
   ⚠️ **这批的批次身份仍叫 `camoufox`**——那是**计划文件名**，
   改它会改掉与旧批次的可比性口径（`batch_id` 是历史键）⇒ **别顺手改**。
4. **重建产物 + 门禁**：`build --overwrite` → `task-view --all --overwrite` →
   `check-archives` → **逐条打开存档读 evidence**（「结论对不对」不在门禁里）。
5. **跑采集侧账**（方案 20 §R3）：新候选行的 pred 面不再是 `unrecorded` ⇒
   账第一次可能非空；**但金标侧仍是 `unrecorded`** ⇒ 跨面可判定性仍多为「不可判定」
   ——那是 R2「旧行不回填」的必然结果，**不是缺陷**。
   ⚠️ **新批次的 pred 面会是 `camofox-plain`**，而存量那批是 `camoufox-plain`
   ⇒ **两者不是同一张面** ⇒ 跨面比较仍多判「不可判定」，**这是对的**
   （方案 20 §R3 的纪律：面 ≠ 面就说不清）。
6. **人判存量那 4 条 agent 负样本**（iqiyi / youku / v.qq.com / bilibili）——
   **本轮已把四份存档的页面原文逐条读过了**（读数见 [§M-37 详解](#m-37--agent-路径贴的分支标签绕过了前置事实核)）：
   **bilibili（登录墙）与 iqiyi（门禁句）两条标签站得住**；
   **youku（续费营销 CTA）与 v.qq（广告页）两条「很可能错」**——
   ⚠️ 但那只是**文本级读数，不是现场重采** ⇒ 改判要等现场看（§Q-20 ⑥）。
   ⚠️ **别急着把它们改判 `unresolved`**：`unresolved` 的意思是「**没判出来**」，
   不是「判为不是墙」⇒ 那是**另一种定性**，同样要证据。
   ⚠️ **现场重采的脚本已换成 camofox**（`research/probes/m30_live_check.py`）。
   ⏳ **2026-10-27 更新**：**现场重采已做**（camofox，4 条全部重开并用仓库自己的
   `paywall_evidence` 判据对读）⇒ 读数见 [§M-37 详解](#m-37--agent-路径贴的分支标签绕过了前置事实核)
   与 [编年史 §十一 · 5-5](90-增量编年史.md)：
   **bilibili 站得住**（真登录墙）；**v.qq 站不住**（只有营销 + 广告位、无门禁句，播放器 02:09 是短片）；
   **youku 最重**（现场是**登录墙**，与池里的 `subscription_wall`、复核档的 `partial_free`
   **两处打架**）；**iqiyi 站得住**（⭐ **复核更正**：存档 `T054__1c9b6260__site-5` 的正文
   **当场就有**「试看 05:44 后结束，观看完整视频请 开通VIP会员」+「99分钟」正片 ⇒ **原读数说的
   就是同一个 URL**，不是另一个；今天重采只取到营销是**页面状态漂移**，不能推翻当场读数。
   ⚠️ 顺带查出**判据缺口**：`paywall_evidence` 对那段当场正文**也**返回空串（DOM 换行切开 ⇒ §Q-20 ②））。
   ⇒ **材料已备，剩下的是 owner 的定性**（§Q-20 ⑥ / ①）。
   ⭐⭐ **2026-10-27 owner 已裁并已执行**（`cli relabel --apply`，改判 **4 条记录**）：
   **① v.qq 留池打标，改用预告片标 `trailer_only`**（落地页只有 2 分 09 秒短片，不是目标正片）；
   **② youku 改判 `login_required`**（现场正文是「由于安全原因，请 登录 后继续观看」⇒ 登录墙）。
   ⇒ 池 **325 行数量不变**（`subscription_wall` 46→42 · `login_required` 9→12 · `trailer_only` 1→2），
   改判行的 evidence 带「[改判 subscription_wall → …]」且 `source=human`。
   ⚠️ **同 URL 的第 4 行没改成**（`TPIN02` 那行，工具按 D-5 归属闸**主动拒了**）⇒ **仍挂
   `subscription_wall`** ⇒ 要 owner 再说一句（留 / 改 / 出池）。
   ⚠️ **两条口径开口留给 owner**：`trailer_only` 的**宽口径**（仓库的 `classify_trailer`
   对 v.qq 判 `none`/`suspect`，**永远不会**产 `trailer_only`）＋ ② 的放宽方案与 owner 的
   youku 定性**打架** ⇒ 详见 [§Q-20 详解](#q-20-详解--与-与-owner-2026-10-27-要的详情--问题--原因--可选做法四段)。

### 跑通前必须重跑的验证项（**逐条打勾，别攒着**）

- [ ] **适配器可解析**：`camofox-browser-mcp` 在 PATH 上，或 `CAMOFOX_MCP_EXE`
      指向一个**存在且可执行**的文件 ⇒ `cli` 的缺件预检会**启动前退 2**，
      **不会**让你跑起来才撞 ImportError。
- [ ] **REST 服务在 9377**：`cd camofox-browser && npm start` ⇒
      `cli check --driver camofox` 能起会话并读回**实测指纹**。
      ⚠️ **指纹形状与 camoufox 那支不同**（九项 → 十一项；`os`/`geoip`/`locale`/
      `humanize`/`headless` **读不到**——服务端的配置本仓库无权声明）⇒
      **别拿旧口径对读**（见 [方案 21 §D7](../设计方案/21-camofox-browser执行层接入方案.md#d7-指纹九项变十一项)）。
- [ ] **`tests/unit/test_camofox_session.py` / `test_camofox_driver.py` / `test_camofox_switch.py`**：
      随实现新建，**不是 `importorskip`** ⇒ 门禁里就是真跑的。
- [ ] **`tests/unit/test_cli_batch_resilience.py::Test起浏览器不重试`**：
      钉的是「camofox 侧失败一次就抛」（**不重试**）——别把它改回重试。
- [ ] **那 10 条批次**：跑完按「逐条打开存档读 evidence」核对，**不只看门禁**。
- [ ] **D-5 配对表**：`GATEKEEPER_DECIDED_BRANCHES` 若随执行层变过，
      契约测试 `tests/contract/test_questionless_producers.py` 会当场红。
- [ ] **工具面声明**：`toolcatalog` 的 `camoufox-1` 面 `fill` / `press` 转正依据是
      E022 存档（2 次 fill / 9 次 press）——**若那两份存档被清出 `output/pipeline/`，
      契约测试会红**（`test_标成已验证的工具在真实存档里出现过`）。
      ✅ **camofox-1 面的三条（`click`/`fill`/`press`）已转正**（2026-10-27）：
      真实 camofox 批次落盘后，契约测试**当场红并指名了要改哪三条**
      （`test_标成能力声明的工具不该在真实存档里出现`）⇒ 按真实调用把
      `exercised` 转 `True`，`required` 口径**逐条按真实键在场率校**：
      `click` 的 `role`/`name` **100%** ⇒ 都必填；`fill` 的三个键 **100%**
      ⇒ 都必填；`press` **只有 `key` 在场**（agent 循环派发 `press` 时
      **不带目标**）⇒ `role`/`name` 保持可选。
- [ ] **并池那一条**（M-40 新接线）：重跑批次后跑
      `cli pool-add --out trajectory_pipeline/output/pipeline`（**dry-run**）
      ⇒ **清单应为空**（真跑的负样本走 `P1Archive.write`，已并池）；
      若不为空 ⇒ 说明有写入点漏了并池，**逐条看它打印的拒并理由**。
- [ ] ⚠️ **新的**：重跑那批的**每一档**都要核这三条（**门禁不管**）：
      ① `fingerprint` **非空**（11 项）⇒ 空 dict 是「**没记到**」不是「指纹为空」；
      ② 每个观察的 `degraded` **都能对上那一步的动作** ⇒ 对不上就是
      **陈旧状态跨页**（[增量九十四](90-增量编年史.md) 缺陷③）；
      ③ **同一档里没有一字不差重复的失败原因** ⇒ 有就是**死页级联**
      （缺陷④），且**执行层会话失效不许记 `unreachable_hard`**（要记
      `unresolved`）——**真·站点打不开不会九条一字不差**。

### R5 实跑那一段的五个缺陷（2026-10-27，**已修**）

**五个都是「零报错」的**——门禁全绿、测试全绿、退出码 0，
**只有「逐条打开存档读 evidence」看得见**。

| # | 缺陷 | 症状 | 修在哪 | 守卫 |
|---|---|---|---|---|
| ① | 送上游的表达式**求不出值**（写了函数字面量，Playwright 按表达式求值 ⇒ 拿不到 `result`） | 整批 **0 候选** | `camofox_driver.py` 改成 IIFE | `test_camofox_driver.py::TestObserve`（两条，含「不是函数字面量」形状判据） |
| ② | 指纹在**导航之后**才有，存档记录在**导航之前**构造 ⇒ 恒空 | 每档 `fingerprint == {}` | `orchestrator.py` + `cli.py`（**两个构造点都补**） | `test_visit.py::Test指纹补读`（2）+ `test_camofox_driver.py::Test指纹时机`（2） |
| ③ | 页级状态**不跨页**（`last_action` 记上一页的动作结果，却拿去判这一页有没有降级） | **18% 的观察**挂着假降级 ⇒ 进 agent 输入 + 静默关掉实体核查闸门 | `camofox_driver.py::_reset_page_state`（六个页级状态一起清） | `test_camofox_driver.py::Test降级不跨页`（2，**做过变异**） |
| ④ | **上游销毁了那一页且不通知客户端** ⇒ 客户端 `tabId` 成死引用 ⇒ 再导航永远 404 | **同一个候选连炸 9 次**，9 条一字不差的「站点打不开」进了负样本池 | `camofox_session.py`（`CamofoxToolError` 解析上游 `code`/`retryable`/`recovery`）+ `camofox_driver.py::_navigate`（丢死页→开新页→**同一个 URL 重来**）+ `reachability.py`/`visit.py`（会话失效 ⇒ `unresolved`，**不**记 `unreachable_hard`） | `test_camofox_driver.py::Test换页自愈`（6）+ `test_camofox_session.py::Test上游错误体的结构`（5）+ `test_visit.py::test_执行层会话失效不记_unreachable_hard`（1） |
| ⑤ | **上游把「建页」与「导航」塞进同一个路由** ⇒ **那个路由失败证明不了站点打不开**（建页路由的失败与导航路由的失败**在错误体里字面同形**） | 一条**假 `unreachable_hard`**：`T048` 的 iqiyi 记「导航失败（网络/DNS/超时）」，而**同一批 T038 打开过同一个站**、**老批 T054 真打开过同一个 URL** | `camofox_driver.py::_open_blank_and_navigate`（**建页与导航拆成两次调用** ⇒ 归属判据变成「**走哪个路由**」）+ `camofox_session.py::CamofoxToolError.execution_fault`（上游 `recovery="retry"` 那一族 ⇒ 执行层故障） | `test_camofox_driver.py::Test归因`（**7**）+ `test_camofox_session.py::Test上游错误体的结构`（**+2**）· 现场：`research/probes/camofox_attribution_probe.py` |

⚠️ **缺陷④的处置全在本仓**（owner 裁定 **A2：上游不许改**）：
适配层把上游的结构化错误**解析出来**（不是猜英文短语）⇒
驱动只对**导航**自愈（动作目标是**页级 ref**，换页后重放会**点错元素**）
⇒ 自愈次数进存档的**运行级 warning**（**不许静默**）。
⚠️ **「换页也失败」记 `unresolved`**——`unresolved` **不是负样本**，
**采集失败不是站点结论**（那是 22 类封闭集里**已有**的一支，**没新开**）。

⚠️ **处置是「停 → 修 → 清 → 重跑」，不是「将就着交」**：
坏存档**一落盘就把负样本并进池**（并池是写入时的动作）⇒
**不清池，重跑那批的行会被判成「已在池里」而跳过**。
⚠️ **purge 的粒度是 run_id**，不是 task_id。

⚠️ **重跑不是「复制上一遍」**：环节① 的「direct 首页核」是 **LLM 分类**
⇒ 同一批两轮的候选数**不一样**（第 2 轮 9 个进环节②，第 3 轮 8 个）
⇒ **分支分布也会跟着变** ⇒ 跨轮比较**不能当同批复现**。

---

## 高 · 阻塞主线指标

| ID | 事项 | 域 | 记录锚点（证据） | 现在差什么 |
|---|---|---|---|---|
| **H-01** | `unresolved` 剩余桶重新归因（原 36/79 = 46%） | 01 采集 / 02 感知 | [01 §未闭环](01-采集执行域.md#本域已知未闭环项) · [02 §INC44 分层](02-感知与判定域.md) · 编年史 §增量四十四 · 编年史 §增量九十八 · [`agent_loop.py::give_up`](../../executor/agent_loop.py#L927) · [`rule_perceptor.infer_access_defect`](../../perception/rule_perceptor.py#L655) · [`rule_perceptor.classify_paywall`](../../perception/rule_perceptor.py#L240) | 簇 C 已被**增量六十·方案 X** 修掉（净修 46 行，[`branches.py:593`](../../executor/branches.py#L593)），**旧的 46% 已不作数，必须重测**才能知道剩下是谁。⭐ **2026-10-28 那次重测做了**（M-46 批次 9 个任务全跑完）：**当前代（agent/camofox）40 条** `unresolved` 属于「**机器本可以判出来**」——**子族① 36 条**（页面正文里就有终局形态，`infer_access_defect` 命中，**全是 404 页**；正文**抓到了**、`degraded=[]`；规则词表里明写 `404 Not Found` / `nginx`，**注释举的例子正是 `kmslxsh.com`**），**子族② 4 条**（`classify_paywall` 对 agent 叙述判命中）。⚠️ **子族② 的数完全取决于口径**：裸词表（叙述里有「付费墙/VIP/会员/订阅」）⇒ **29 条**，其中 **27 条是「裸词表命中但规范判据不命中」**⇒ **原因不是词表窄，是 `classify_paywall` 压根是为页面正文写的、拿去判 agent 叙述就是用错了地方**。**一条根因**：[`give_up`](../../executor/agent_loop.py#L927) **一律落 `unresolved`、不查任何判据**（对比 `close()` 查 verdict 映射表）⇒ **真负样本不进池**。⚠️ **代际回退**：旧代（LLM 感知层）把同一个 404 判成 `broken` ⇒ **进池 9 行**；当前代 **25 次全 `unresolved` ⇒ 一行没进池** ⇒ **不是一直不行，是换代后退了**。⚠️ **E002 逐 URL 对照**：同任务同 URL 有 **4 条 `subscription_wall` → `unresolved`**（旧批判墙并进池、新批落 unresolved）⇒ ⚠️ **池里那几行还在**（append-only 不回填 ⇒ **训练数据没丢**），**丢的是新档的断言** ⇒ 哪天真按新档重建池就没了。✅ **owner 已裁并已实施（2026-10-28 · Q-25 甲）**：[`give_up`](../../executor/agent_loop.py#L927) 收口前先查**最后一页自己的正文**的终局形态（[`branches.py::terminal_shape_branch`](../../executor/branches.py)）⇒ 命中落那条分支、不命中仍落 `unresolved`；⚠️ **agent 自述不算证据**、⚠️ **命中不等于对**（仍要人核）。回放读数（全库 **210 条「agent 放弃」visit**）：命中 **30 条**（**28 `broken` / 2 `blocked`**，落在 **8 个落地页**）、**归因对象不明 12 条**、未命中 **168 条**（其中 **5 条正文命中但前置事实不成立** ⇒ 降级 `unresolved`）。⚠️ **回放不是重跑**（读的是旧存档、用的是新判据）⇒ 下一批真跑才有新读数<br>⭐ **2026-10-29 真跑读数已出**（批次 `guard-rerun-2026-10-28` · 19 个 give-up 命中任务全跑完 · 串行 · 约 7.5 小时）：**79** 条「agent 放弃收口」visit ⇒ **护栏判据命中 17**（**15** `broken` + **2** 前置事实不成立降级）+ **6** 归因对象不明 + **56** 不命中 ⇒ **本条台账问的「机器本可以判出来」那批，现在真的落进了分支**（**15** 条 `broken` **全部在池里**，池 **407 → 437**，+30 行全是本批 run 写的、其中 9 行是护栏落的）。⭐ **上线前后读数差**（同 task_id + 同 url）**57** 条分支不同 ⇒ **其中只有 15 条是护栏效应**（全是 `unresolved` → `broken`）⇒ 其余 **42 条混着站点漂移**，**不是护栏的功也不是过**。⚠️ **仍有 3 件等人裁**（E021 的 host 级 403 / E014 同 URL 两代分歧 / T011 同 URL 出两支）⇒ 详见 [Q-25](10-TODO台账.md) 行与 [编年史 §增量一百](90-增量编年史.md#增量一百--两条护栏的第一次真跑guard-rerun-2026-10-28--19-个任务--q-25-甲真命中-15-条--q-24-甲一次没触发--顺带撞出同-url-出两支)<br>⭐ **2026-10-29 · T011 已裁并已实施**（owner 裁定「改判成 `broken` 再并池」）：⚠️ **`cli relabel` 改不了 `unresolved`**（它只取**负样本分支**，而 `unresolved` ∈ `NON_SAMPLE_BRANCHES`）⇒ **唯一合规路径是人工复核通道**（手册 §4.5 · `cli review --verdicts … --apply`，**落盘时自动并池**）。⭐ **现场取证**（`research/probes/q24_q25_guard_kmslxsh_probe.txt`）：该 URL · `/v/17807/…` · 站点根 **3/3** 返回「404 Not Found\nnginx」、`elements_total=0` · `video_tag_count=0` · `degraded=[]` ⇒ **前置事实成立** ⇒ 当初那条观察若没带 `press_unsupported`，护栏本就会落 `broken`。⇒ `T011__78621699`：`unresolved` → **`broken`** · `source=human` · `branch_before_review=unresolved` 保留；池 **437 → 438**。⚠️ **执行这一条撞出两个机制缺口（都修了，各有测试）**：① **`review_queue._fits` 对无题产地一律拒** ⇒ **Q-25 甲落的 `broken`（`question` 恒为空）人裁通道够不着**（执行层认的组合人核层不认 = 两本规则书）⇒ 改为**只**给 `TERMINAL_SHAPE_BRANCHES`（`broken`/`blocked`，**从 `branches.py` 导入不手抄**）开口子，**判断点类分支空 question 仍拒**；✅ **owner 已追认（2026-10-29）**：这两处机制改动是**执行这一条裁定的必要条件**（改的是**复核通道的准入与范围**，**护栏判据一行没动**）；② **`review --apply` 不加限定会把每份原档重算成复核档** ⇒ **2026-10-24 那轮 78 份人裁定会被原档静默覆盖** ⇒ 改为**读生效档**（有 `.reviewed.json` 就以它为底）+ 新增 **`--only`** 钉死范围。⇒ **残余**：同 URL 另有 **27** 条 `unresolved` 分布在 **20** 个任务上（**全裁只新增 6 行**池行：E028 · T012 · T028 · T033 · T036 · T040；另 21 条撞键去重）⇒ 全裁后 kmslxsh 的 **50** 条 outcome 行里 **47** 条落 `broken`（另 3 条 `login_wall_or_blocked` 不变、`unresolved` 清零）、**T011 同 URL 两支**消除 ⇒ **待 owner 再裁**（本轮授权只覆盖 T011 这一行）。⚠️ **门禁退出码更正**：`cli check-archives` 实测 **exit 1**（46 条 `negative_contradicted_by_success` 是 `Severity.FATAL` ⇒ `report.ok=False`）——此前记的「exit 0」是**量错了**（`$?` 取的是管道末端 `tail` 的状态）；那 46 条**全是存量**（`subscription_wall` 32 · `partial_free` 13 · `component_unverified` 1）⇒ **0 条**落在 `broken` / kmslxsh 上；归档级另有 **3 条 `negative_not_pooled`（DEGRADED，非 FATAL）**＝ `pool-add` 干跑那 8 条里的 3 条（E021 被判成功**矛盾** / E023 · T054 池内同址**已有别的分支**）⇒ **存量的人工分歧，不是本轮引入**<br>⭐ **2026-10-29 · E021 已用浏览器实看，并立了「换面会不会变」这把尺子**（owner：「人工用浏览器看一眼，给出完整 url」）：**完整 url** = `https://m.xacjs.com/weihu/1416804.html`（全库 `m.xacjs.com` 只有这 **1** 个站点地址；两条 run 的跳转链 `http://www.baidu.com/link?url=fF_mLv2j_CGIezOu0HDKjCUXNi-A4CGCwu2Mjv0oxuNwjw-fNI_xPjOI2XhyakIr` / `http://www.baidu.com/link?url=DXsjd367aDwviTqDGDsY6rQWXVhhSn5fWgdj3mg0G1jcoxhSjzGl30Y5JEwzxLMR`）。**生产面实看**（camofox，**5** 个路径）**5/5 = 「403 Forbidden / nginx」**、元素 0 · video 0 · `降级=[]`（留痕 `research/probes/q24_q25_guard_host403_recheck_2026-10-29.txt`）⇒ 前置事实成立。⭐ **新工具：面依赖检查**（`research/probes/q24_q25_guard_facecheck.py` —— 同一台机器、同一出口 IP，**只换请求头**）：`chrome`/`bare`/`referer` 三种客户端 × **7** 个 xacjs URL ⇒ **21/21 HTTP 403**；⚠️ **同机同出口的对照站 `m.dinggetv.com` 同刻 200 OK** ⇒ 403 是**站点对本机出口**的拒绝、不是本地网络问题（留痕 `…_facecheck_xacjs_2026-10-29.txt`）⇒ **判读口径三选一**：**换面不变** ⇒ 站点的事实（真负样本成立）· **换面变了** ⇒ **我们这个面的事实**，不能外推成「站点看不了」· **换面仍是拒绝** ⇒ **始终没看到站点页面** ⇒ fail-closed、**不是**真负样本。⭐ **把池里全部 10 个 `broken` 唯一 URL 都查了一遍**（**30** 次请求 · `…_facecheck_pool_broken_2026-10-29.txt`）⇒ kmslxsh ✓ / wanminggangguan ✓ **不变（站点事实）**；`v.qq.com/error.html`·`bilibili.com/error.html`（URL 本身就是错误页）✓；`104.143.94.74/guocanju/wulinwaichuan/`·`bilibili.com/bangumi/play/ep693415`（普通客户端 200，但裁的是 `player.ok` 级「媒体解不了」）**不是反证** ⇒ **两行站不住**：`m.xacjs.com/weihu/1416804.html`（403 ⇒ fail-closed ⇒ `broken` 不成立）、**`m.dinggetv.com/dingge/84421.html`（E014 —— 普通客户端拿到 HTTP 200 + **80 KB** 真页面《…大话西游之大圣娶亲国语版…免费在线观看全集》⇒ 旧代那条 `broken` 是**面产物** ⇒ ⚠️ **先前「E014 不用动」的结论是错的**，那是没换面的前提下说的）**。⇒ **两条待 owner 批的处置**（E021 与 E014 出池）：`cli downgrade`（→ `unresolved`，理由进 evidence）＋ `cli pool-drop`（写审计日志）——⚠️ **复核通道够不着**：`apply_verdicts` 只对 `REVIEW_BRANCHES` 生效，`broken` ∉ ⇒ 那行会被原样忽略；`cli relabel --to blocked` 又拒（`blocked` 是采集侧分支、不是真负样本）⇒ **出池两步是唯一合规路径**。⇒ **判据层还开着一个口**：`ACCESS_DEFECT_KINDS` 里**没有 403**（`region`/`captcha`/`anti_bot`/`not_found`）⇒ 403 错误页含 `nginx` ⇒ 被 `not_found` 旁证命中 ⇒ 落 `broken`（**采集侧的东西进了真负样本池**）⇒ **今天再跑一遍 E021 还会再写一行错的** ⇒ 「加一类 `forbidden` ⇒ 403 落 `blocked`（不进池）」是**另一个 owner 决定**，**判据一行没动**。⇒ **三件的人核状态（截至 2026-10-29）**：**T011 已裁已实施**（见上）· **E021 已取证、待 owner 批出池** · **E014 已翻案、待 owner 批出池** ⇒ 详见 [编年史 增量一百 §八](90-增量编年史.md) |
| **H-02** | ✅ **已闭（2026-10-25 v13 冻结档，2026-10-27 复读）** D-10 代码层 vs 黄金集一致率 → ≥80% | 07 评估 | [07 §D-10](07-评估与黄金集域.md) · [00 §四条主线](00-总览.md#一句话现状) | ✅ **已达标**：头条 **82.8%（106/128，调参池口径 C）≥ 80% 上岗线**（上岗线真源 `evaluation/scorers/thresholds.py` 的 `d10_min_agreement`，owner 2026-10-24 从 0.90 下调）。**涨幅落在采集侧判定**：v13 冻结时 `code_refresh` 从 `candidates.jsonl` 重挂 **26 行** `code.branch`（manifest `code_refresh.changed_rows=26`），`disagree` 68 → 59。⚠️ **旧口径（三档合计）79.6% 仍差 0.4pp**——上岗判定以头条为准（C-04），别拿三档数当头条。**原诊断留档**：簇 1 type A（component_unverified 漏 VIP 强信号）+ type C（collection fail 8 条）两条路都跑过；⚠️ **先跑哪条不是拍脑袋定的**——**C-07 已出数（2026-10-23）**：`gold_defect` 14 / 评估器 12（`evaluator_logic` 11 + `evaluator_prompt` 1）/ 采集 7 / `rubric_*` 0 ⇒ **先核金标（14，属 M-30），再修评估器（12，属 M-26 / M-29），最后才是重采集（7）；「改 rubric」在这一轮没有证据**。⚠️ 但这份分布只覆盖 `d10` 一个条款、单版横截面，**别当成趋势**；并且评分器改了要等重冻结刷新分数（M-29），否则下一轮读数与改动无关。**残余偏差**仍挂 M-26 / M-29 / M-30 |
| **H-03** | ✅ **已闭（2026-10-26）** mojibake（charset）防护**已被真实站点触发**——**且触发点在浏览器端，采集层修不了** | 01 采集 / 05 rationale | 编年史 §增量九一 · [避坑指南 §6.5](../02-避坑指南.md) · [00 §簇 B](00-总览.md#下一步做什么全域汇总) · `research/probes/probe_obscura_gbk2.py`（commit `d01ee0d`） | **证伪动作已完成**（2026-10-25，`tv.sohu.com` 真采）：sohu 的 HTML **字节本身混合损坏**（声明 GBK、夹无效字节）⇒ 浏览器按 GBK 解码即产出 U+FFFD ⇒ **不是采集层 charset 能修的，换任何执行层都救不回**（浏览器 DOM 里**已经是坏的**）⇒ **「跑一次真采」这个动作做完了，结论是「修不了」**。⚠️ **防护不撤**（`executor/dom.py::is_mojibake` + `degraded` 降级）——它仍是唯一挡住「坏正文进感知层 → 凭空造出理由通顺的假负样本」的那道闸。⚠️ **残留开口**：2026-10-10「HTTP 直取字节按 GBK 严格解码 0 替换符」与 2026-10-25「浏览器里字节是坏的」**对不上，未区分** ⇒ 要告站点得**先重取一次字节**。~~⚠️ **尾巴（文档回填）**~~ ✅ **已回填（2026-10-26）**：[05 §mojibake 防护](05-rationale与闸门域.md) 三处定点全改（①:20 表格行 ②:37–40 成因段换成两代读数并列、旧「经 MCP 传出」假说标为无独立证据 ③:285 未闭环表行改成「已触发 + 残留开口」），另**多改两处**（不在原清单里）：[01 §采集执行域](01-采集执行域.md):23 同一格旧话、[02 §感知与判定域](02-感知与判定域.md):261「转攻簇 B 采采集层 charset 修复」这条**已作废的路**。⚠️ **本行仍留台账**，只为一件事：**残留开口**——2026-10-10 的 HTTP 直取读数是好的、2026-10-25 浏览器里是坏的，**两代对不上、未区分**；对外告站点前**先重取一次字节**（同一 URL 两次取到的是不是同一批字节、有没有夹无效字节） |
| **H-04** | JS-heavy 站真渲染失败（iqiyi；w.mgtv / sohu 反爬时变） | 01 采集 | [01 §窗口与候选集合](01-采集执行域.md) · [02 §INC43 探针](02-感知与判定域.md) · `research/probes/inc43_probe_bodies.jsonl` | 15 行判对但仍 `unresolved`，属**采集层能力边界**，不是判定规则能救的 |
| **H-05** | **30** 条 `flagged_mismatch` **进不了 L6 复核队列** | 04 探针 / 08 存档 | [04 §未闭环](04-探针七层域.md#本域已知未闭环项) · [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | ⚠️ **不是「触发器没写」**：`dispatch_l6`（INC36）已在，挂在**三处**主循环末尾（[`review_queue.py:855`](../../executor/review_queue.py#L855) · `cli.py:593` / `:725` / `:1960`）。缺的是**派发条件**——只认 L5 反思档与 fail-closed 兜底，`auto-validator-l3l4-v1-flagged-mismatch` 桶**不在其中**（该串在 `review_queue.py` 零命中，2026-10-20 核）。⚠️ **原写 31 条**——2026-10-25 二次金标重核里**有 1 条已被人工判完**（转 `reesn`）⇒ 该桶现存 **30** 条 |
| **H-06** | 判断点 ② 让错误页通过（bilibili「发生了错误」判 True） | 02 感知 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) · 避坑指南 §5 类 | **未修且是设计题**：没有可靠的代码层事实能区分「错误页」与「正常页」。要么改 ② 的输出契约强调错误页，要么接受 ③ 兜住它（现状） |
| **H-07** | `partial_free` / `login_required` 黄金集**各 0 条** | 03 标签 / 07 评估 | [03 §MIN_PER_BRANCH](03-标签与分支域.md) · [07 §未闭环](07-评估与黄金集域.md) | a 路径已证实跑不出（INC33）；b 路径追溯 `login_wall_or_blocked` **17 条**待做 |
| **H-08** | D-11 camoufox 执行层替换：**P-1/P0/P1/P2/P3/P4 已落地；P5 机制已落地但判据未过 → 未切换；执行口径已定为 headless（2026-10-21）；P6 未开工**（含 **D12** 动作词汇表对齐） | 01 采集 / 跨层 | [设计方案/04-camoufox](../设计方案/04-camoufox执行层替换方案.md) §6 落地阶段 | 方案定稿、**P-0 起一个没动**，**生产链仍跑 obscura**。**P-1 两段都跑完**（探针 `executor/browser/probe_f5_name.py`）：离线段（[§1.5](../设计方案/04-camoufox执行层替换方案.md)）role 侧零落差、朴素链 65% 且图标按钮产出空 name（`get_by_role(name="")` → `count == 0` **不报错**，与「元素不存在」同形）；**真实页面段（[§1.6](../设计方案/04-camoufox执行层替换方案.md)，6 URL / 240 行）判据达标**——权威 name **91/91 = 100%**、JS+伪元素 96.7%、纯 JS 87.9%。⚠️ **D12 生死门已过，但实现口径被这次实测改了两处**：① 采集面 **name 取 `aria_snapshot`，不许自己算**；② 枚举**先过可见性**（41% 元素没有可访问名，其中 90/97 不可见）+ 定位器**钉 `exact=True`**（默认 substring 口径下命中率 100%，判据会变空）。**P0 已落地**（`driver_factory.py`：6 处硬编码 `ObscuraDriver(client)` 收敛到 `make_driver`，零行为变化；AST 门禁盯第 7 处）。⏳ **P0 的「存档逐字节同形」没验**——P0 当时本机缺 `OBSCURA_EXE`（后来配上了，基线存档得另找）。✅ **已闭（2026-10-21，增量七十一）——但判据换了**：旧判据（跑两遍比字节）**作废**（活站会漂，它分不清「重构改了行为」与「站点漂了」，且没有分母），换成**离线确定性**的「同一份输入 ⇒ 同一份存档」——把驱动录下来的转录喂进两条装配路径比规范化后的存档，`tests/unit/test_p0_equivalence.py`，**5 条全绿 + 过变异测试**（改坏实现 ⇒ 4/5 红）。**P1 已落地（2026-10-20）**：`camoufox_session.py`（一个 Run 一次 launch，钉死 `os`/`screen`/`window`/`humanize`/`geoip` 关/`main_world_eval` 关/排除 uBO，**回读实测指纹**）+ `camoufox_driver.py`（只落 `goto`/`snapshot`/`count`/`evaluate`/`close`，其余原语**抛 `NotImplementedError` 不返回空**）；`check --driver camoufox` 可探并打印读数，**`DEFAULT_KIND` 仍是 obscura，生产链零变化**。实测两处要注意（[§4.3.1](../设计方案/04-camoufox执行层替换方案.md)）：`screen`/`dpr` 钉得住，但 **`window=` 钉的是外框**——8 次实测内宽恒 `1905`、**内高在 `940`/`948` 间摆动**（5:3）→ 批次元数据只信实测值；真要钉死视口得改 `new_context(viewport=…)` **并重验指纹自洽**，P5 前定。§6.1 反爬拦截率仍是切换的 go/no-go 门槛，**不达标就不换生产链**。**D12 的「声明侧」已落地**（2026-10-20，增量六十四）：工具面轴（`obscura-1` / `camoufox-1`）进 `toolcatalog`，面由 P1 的 `driver` 一对一折算；**P3-α 已落地（2026-10-20）**：`camoufox_driver.interactive()` 一页一次 `page.aria_snapshot()`（51ms/页，逐元素方案 200 元素要 4.8s），role+name 全取 playwright 结论，三类丢弃（词表外 role / 无名字 / 真同名整组）记进 `last_enumeration`；纯解析器 `browser/aria_snapshot.py` 离线可测。`check --driver camoufox` 打元素表+账目（`example.com` kept=1 / `baidu` nodes=101→kept=34）。⚠️ 三处**已知偏离**记在 [§4.5.6](../设计方案/04-camoufox执行层替换方案.md)：可见性未逐元素过滤（5.8ms×N）、`tag` 装的是 ARIA role（下游两处词表已放宽成并集，P4 收窄）、新发现 `{role,name}` **只在同一次枚举内有效**（`textbox` 的名字可能是动态占位词）。**P2 动作面已落地（2026-10-20，[§4.6](../设计方案/04-camoufox执行层替换方案.md)）**：`click`/`fill`/`press` 三个原语 + **协议改名**（`type_text`→`fill`、`press_key`→`press`，语义层与驱动层同名，`agent_loop` 里那张翻译表已删）；三态**先 `count()` 再操作**（0 → 静默 `not_found`、>1 → `ambiguous` 不动作、`count` 报错 → `DriverError`），定位钉 `exact=True`，**跨面参数抛 `ValueError`**（`DriverError` 会被 `steps/aggregator` 吃成 `unreachable_hard`，接线 bug 就变成理由通顺的负样本）。真浏览器冒烟三原语全 `ok`（`check --action-smoke`）。★**新事实**：`role+name` 是**页面级**定位，**导航后不失效而是可能静默点中新页同名元素**（对照 `ref` 必然报错）→ **任何导航之后必须重新枚举**。★**动作超时被实测改写**：`ACTION_TIMEOUT_MS=5000` 让一半点击超时（`Locator.click: Timeout 5000ms exceeded`），现场取证证明元素在/可见/可用/包围盒稳定、`elementFromPoint` 就是它，而 `click(no_wait_after=True)` 只要 1.1s、`goto` 同址 0.3s——**9–13s 是 playwright 在等「这次点击触发的导航完成」**。处置：`click`/`press` 关掉 `no_wait_after`，之后 `_settle()` 有界等 `domcontentloaded`（2s，`NAV_SETTLE_TIMEOUT_MS`），结果记 `last_action["settle"]`=`ok`/`timeout`（等不到不抛但要说出来）。修后连跑 4 次 **0 超时**；但 2/4 点击后 2s 内 URL 未变（对照探针 `goto` 路径 3/3 在 0.4s 内跳）→ **「点了但没跳」是站点行为，未定论**，`ok` 只代表调用成功。**P3-β 已落地（2026-10-20，[§4.7](../设计方案/04-camoufox执行层替换方案.md)）**：`links`/`extract`/`new_tab`/`close_tab`/`observe` 全部落地，**协议原语齐了**，`_todo` 已删。三条被实测改写的口径：**标签页做成栈**（一个 `page` 字段「开 tab → 观察 → 关 tab → 回中间页」无处可回；只剩一页时先补空白页、开页失败收掉新页——真浏览器四条语义全对）· **`links` 按 href 去重**（obscura 的 `browser_links` 恒 `total==distinct`（131/131、33/33），不去重时 camoufox `links_total` 高 28–37%，而它要进 D-2 口径）· **`extract` 取文本 `innerText‖textContent`**（bing 结果页 `li.b_algo h2 a` 的 10 个锚 **`innerText` 全空**，只取它 → 「抽到 10 个空标题」→ ① 退回 fail-closed，看着像选择器失效）。★**两面 `Observation` 已并排测**（`probe_observe --driver {obscura,camoufox}`，同脚本同契约断言）：`url`/`title`/`body`/`links`/`video`/`iframe`/`degraded` 全部对齐且两边契约全绿，但 **`elements_total` 74 vs 34（−54%）**、`raw_len` 含不含快照、`links[].text` 面包屑 vs 锚文本——**三处不同义**，故 P2 后半条判「已测量」而非「同口径」。★**`search` 标题抽取已逐条对齐**（`probe_result_titles --driver …`：bing 两边 `10/10` 对、baidu 两边 `8/8` 对且 8 条含片名）；顺带推翻一句旧记录：两边 `@href` 今天都返回**包裹形态** `baidu.com/link?url=…`，`steps/search.title_for` 里「obscura 给解包后的 URL」那句是另一种页面变体上的旧测量。**P4 契约迁移已落地（2026-10-20，[§4.8](../设计方案/04-camoufox执行层替换方案.md)，增量六十七）**：`element` 的键名不再全局手抄，改成**按方言投影**——落盘/进 prompt 时由 `common/elements.py` 的 `ELEMENT_KEYS` 一对一折算（obscura-1 面 `{tag,label}`、camoufox-1 面 `{role,name}`），**槽位统一（role/name）、值域不统一（html_tag vs aria_role）、不互译、不混池**；`Observation.element_vocabulary` 由驱动实现自己置，replay 从存档键反推（`vocabulary_of_archive_elements`），perceptor 词表按面收窄（**认不出就抛**，不回落）。20 条契约测试封着（`tests/contract/test_element_dialect.py`，含「`DIALECTS`/`SURFACES`/`ELEMENT_KEYS` 三表同键」与「`{role,name}` 不得出现在存量 P1/P2 产物里」）。★**`toolcatalog` 的 `required` 无法按新批次重算——做不了**：`output/pipeline/` 里 camoufox-1 面的 P2 样本**一条都没有**（那两处 camoufox/obscura 文件是 `probe_observe*` 探针产物、不是 run 存档），口径仍是上一批实测值；`Test存量口径` 会在第一个 camoufox 批次落盘时变红并提示重算。**P5 机制已落地、判据未过所以没切换（2026-10-20，增量六十八）**：① 动作派发按面（`actions.target_params`：`html_tag`→`{ref}` / `aria_role`→`{role,name}`，`aria` 面缺目标抛 `UnaddressableTarget` 而**不退回 ref**）；② `run`/`run-plan` 加 `--driver`（obscura 整批一条连接、行为不变；camoufox **一 Run 一次 launch**）；③ §6.1 判据变成命令 `cli compare-drivers`（`executor/dual_run.py`，`verdict` 只有 pass/fail/incomplete，**不合成加权分**）。**一次完整 `run` 已走通 camoufox**（DR01《让子弹飞》：13 站、20 个观察，元素键 `{ref,role,name}` 落盘正确）。**但第一轮真双跑判 fail**：分支分布 6 支超 5pp · `body_text` 中位数 814→398.5（−51%）· `elements_total` 中位数 105.5→31.5（−70%）；拦截率 25.0% vs 23.1%（读数，§6.1 不构成否决）。★**点击 3/3 超时：已定位（同日六轮探针，推翻当时的「新开 tab」说法）**——真变量是**窗口不可渲染**（最小化/息屏/锁屏/完全遮挡）：同一次 `locator.click` 窗口可见 **0.4–1.5s**、最小化 **5–13s**；`humanize=False` 82ms → **9.1s**、`humanize=0.3` 386ms → **9.1s**、`headless=True` 34–71ms **从不慢**；页内 `rAF` 滞后 4–17ms → **902ms–62s**（一帧没画）。四条证伪：**① 超时那次点击的事件照样落地**（页内 `pointerdown` 计数涨）→ 不是挂死是慢过超时；② headful 下**会话首页同样超时**（「首页 0.74s」只是那时窗口还可见）；③ `humanize` 只是放大器（9.1→13.3s），关掉治不了；④ 关 `widget.windows.window_occlusion_tracking.enabled`、调 `dom.min_background_timeout_value` / `background_throttling_max_budget` **都治不住**（最小化仍 5.1–9.4s）。⚠️ **别与 P2 那次 9–13s 混为一谈**：那次的处置是 `no_wait_after=True` + 有界 `_settle`（等导航），而本轮在**没有任何导航的局部页**上同样量到 9.1s——**两个机制，量级撞在一起**。后果仍是「点击没做成」被下游读成 `component_unverified`（理由通顺的假负样本），且**超时的点击不能重试**（事件可能已落地，重试=点两次）；`steps[]` 不记单步耗时，事后分不出 5s 与 13s。因此 `DEFAULT_KIND` **仍是 obscura**，见 [方案 04 §4.9.4](../设计方案/04-camoufox执行层替换方案.md)。**仍未闭环**：① ★**判据②③的差值不可比**（2026-10-21 梳理挖出）——两面 `elements_total` **根本不是同一个东西**（obscura = DOM 全量，camoufox = ARIA 枚举 kept 数；过滤规则也不同，§4.5.6 三处偏离）。所以「−70%」**不是可解释的量**，阈值调宽调窄都没意义。✅ **口径已统一（2026-10-21，增量七十）**：新增**共享口径**（同一段 JS `SHARED_DOM_JS` + 同一把尺子 `filter_ladder`，唯一真源在 `executor/browser/element_basis.py`）+ `Observation.element_basis` 新列（**`elements_total` 一个字没改**）；`compare-drivers` 判据③拆成「面内口径（跨面 `incomplete`）」+「共享口径（承载 pass/fail）」，并给**归因表**（两本账逐档对相，差值落在哪一档一眼可见）。✅ **第二轮双跑已跑（2026-10-21，增量七十二）——读数齐了**：headless、3 task × 2 面、开 `--element-basis`；判定仍 `fail`，但**产出是五个「怎么读数」的认识**：① 拦截率口径把登录墙算成反爬（拆开后**结论相反**：真·反爬 A 14.89% / B 9.09%）；② obscura 抽不出客户端渲染页正文 ⇒ A 侧 60% 的站点是「没测到」；③ 两批样本只重叠 37/50 ⇒ 占比口径不可直接比；④ 判据②③ 的中位数是**混合中位数**，按族分列后**会翻号**（播放页族是 A 更好，60 vs 35）；⑤ B 面 9 条 `login_wall_or_blocked` 是**「理由通顺的假负样本」**（判断点② 的守卫门槛太锐 ⇒ 新增 H-15）。**已改工具**：拦截率拆桶、配对比较、按族分列、归因表「不可相减」注释。⚠️ **彻底统一 `elements_total` 的语义仍要动 obscura 的方言**（= 方案 15 R3，见 M-21），那是另一件事；⚠️ **彻底统一 `elements_total` 的语义仍要动 obscura 的方言**（= 方案 15 R3，见 M-21），那是另一件事；✅ **headless 拦截率已实测（2026-10-21，增量七十二）——A 的前置门过了**：真·反爬口径 B **9.09%** < A **14.89%**（配对子集 B 8.11% < A 13.51%）⇒ **不显著变高，不构成否决项**。⚠️ **必须用「真·反爬」口径**——合并口径（反爬 + 登录墙）给出**相反**的结论（A 19.15% / B 34.09%）；✅ **双跑样本量已提到 3 task × 各 1 次**（增量七十二：DR01/DR02/DR03，同 `--max-candidates 20`）；⚠️ **但「3 个 task」不等于读数稳了**——第三轮识别的两个混淆（候选集只重叠一半、抽正文能力不同）说明**样本量不是主要矛盾，口径才是**（H-14）；④ **人判两条**（负样本成因 / 动作可移植性）——`compare-drivers` 只报 `needs_manual`，机器判不了。**材料已备好**（增量七十二）：负样本侧**已抓到 9 条「理由通顺的假负样本」**（B 面把「谁都没读到内容的页面」写成 `login_wall_or_blocked` ⇒ H-15，材料在 `research/probes/inc72_paired_compare.py`）；动作可移植性侧**连工具都还没有**（M-25），结构上大概率可移植（name 取自 playwright 自己的 `aria_snapshot`），没验的是重放时还能不能定位到；⑤ `toolcatalog` 采集面 3 条 `exercised=False` 仍未变（没在 camoufox 存档上跑过 `build`）；⑥ `required` 按新批次重算仍做不了（**整批 camoufox-1 面的 P2 样本还是不够**）。✅ **递归扫描那一半已闭（2026-10-21，增量七十一）**：契约测试的 glob 改成**扫整棵子树 + 按形状认**（P2 = 顶层有 `six`；P1 = 顶层有 `run_config` 且有 `steps`/`visits`）+ 按 `run_id`/`sample_id` 去重（备份副本不重复计分母）——`dualrun/` 下的存档**从此进视野**，那 5 条 camoufox 面的断言**从 skip 变成真判**。⚠️ 递归扫描当场抓出一条**谎话**：`toolcatalog` 里 camoufox-1 面的 `click` 标着 `exercised=False`、注释写着「camoufox 面一次都没跑过」，而 `dualrun/camoufox/` 里**有 3 次 `{role,name}` 点击**——已改成 `True` 并重写注释；⑦ **P6 未开工**（批次元数据 `driver`+`fingerprint`+动作编码版本）——⚠️ **口径反转让它提前了**：`headless` 已在 `fingerprint` 里（第九项），但**整份 fingerprint 还只在 driver 层，没进 P1 存档**。★**已闭环的部分**（2026-10-21）：点击超时的**口径**已定（无人值守走 `headless=True`）、`ACTION_TIMEOUT_MS` 5s→**15s**（盖住实测上界 13323ms）、**超时不重试**（事件可能已落地，加测试钉住）、**`steps[]` 记单步耗时** `duration_ms`（⚠️ 计时器换成了 `time.perf_counter`——Windows 上 `time.monotonic` 刻度 15.6ms，量出来的耗时是假的；见 [L-12](#低--记着别丢)）、**判据②③的口径统一**（增量七十：共享口径 + 归因表）、**P0 的等价性门禁**（增量七十一：判据换成「同一份输入 ⇒ 同一份存档」+ 过变异测试）、**契约测试递归扫描**（增量七十一：按形状认 + 去重）、★**测量失败不再污染 `degraded`**（增量七十一：`element_basis_error` 独立通道——否则一把坏尺子会作废整批数据）。✅ **已闭（2026-10-22，增量七十九）——owner 裁定绕过 §6.1 直接切**：owner 判「**不再双跑**，选定 camoufox 做默认浏览器调用主力——**反爬拦截与页面内容获取是主要，速度次要**，可接受增加等待时间」。⇒ **`DEFAULT_KIND` 已改成 `"camoufox"`**（改这一处就切生产链）；配套三件**不做也会出事**的已一并落地：① **P6 指纹进 P1**（`RunRecord.fingerprint` + `to_json` 落盘 + 编排器从 `driver.fingerprint` 取值）——指纹**只在 launch 那一刻确定，事后补不回来**，而 `window=` 钉的是**外框**（8 次实测内宽恒 1905、内高 940/948 摆动）⇒ 不记它，批次分布的变化会被读成「站点变了」；② **`per_site_timeout_s` 90 → 180**（90 是 obscura 时代的数；camoufox 单次点击实测上界 13.3s × 8 步 ≈ 122s ⇒ 90s **必然触顶**，触顶会被记成 `unreachable_hard`——**理由通顺的假负样本**）并**暴露到 CLI**（`--per-site-timeout`，非正数即报错）；③ **`proberun` 补记 `driver` 与 `fingerprint`**（此前一条都不记 ⇒ 会被折算成 obscura-1 面渲染，而真实调用是 camoufox 面——**声明与真实调用对不上**）。**探针三条命令一起接**（`probe` / `proberun` / `mediabrowse` 各加 `--driver` 与 `--headful`）。**obscura 没删**——它是存量 P1/P2 产物的方言（`obscura-1` 面**只读不再产出**）与口径回溯参照系。★**切换后自查又抓出两处「默认值搬家」的漏**（**手工路径不炸、另一条路径炸**，只测过手工路径的用例发现不了）：① **计划路径**（`run --plan --driver obscura`）的 `make_driver` 没钉 `kind` ⇒ `ValueError: camoufox 实现需要 CamoufoxSession，未提供`——**批量生产入口直接炸**；② **`check --driver` 缺省**把 `"obscura"` 硬编码在自己的 parser 里 = **第二份隐式约定** ⇒ 自检默认探的**不是生产面**，读数不能外推。两处都已修，并各配一条**通用守卫**（扫全文件的 AST 断言 + 扫全 parser 的缺省断言）——要防的是**下一处**漏写。✅ **第一批 camoufox 真实批次已开跑（2026-10-22）**——`run --plan plan-camoufox-prod1.json`（12 条 · seed 20261022 · `--element-basis` · LLM 感知层），落 `output/pipeline/camoufox_prod1/`。**接线冒烟先行**（单条 15 站 / 16 outcome / 560s）：`driver=camoufox`、`fingerprint` 九项全落盘、面折算 `camoufox-1`、`per_site_timeout_s=180`——「自检过 ≠ 批次能跑」那两条未覆盖面（LLM 感知层 + 站点遍历）都通了。⚠️ **两条必须随批记账的口径**：① **片名分布高度重复**——骨架库 98 条里 **38 条是《功夫》**（唯一片名仅 35 个），本批 12 条里 7 条也是 ⇒ **拦截率的分母不独立**，读数不能当「12 个独立站点分布」；② **LLM 后端会回 529**（冒烟 4 次 / 批次内多次），已确认被 `max_retries=2` 的 5xx 重试吸收——**重试用尽才 fail-closed 记 `unresolved`**，所以冒烟里 6 条 `unresolved` 都带着实打实的 LLM 判词，**不是**后端抖动伪装出来的。⚠️ **`llm_calls` 在 scripted 批次恒空是设计如此**（账本只圈 agent_loop，感知层走 `Decision.latency_ms`）——别把它读成「LLM 没被调用」。★ **跑这批时又抓出「递归扫描」漏了三处**（增量七十一只改了 `tool_catalog` 一处）：`test_element_dialect._run_records` / `test_p1_contract._real_archives` / `test_p2_contract._real_archives` 全是 `glob("*.json")` **只扫顶层** ⇒ **「存量口径那三条是假绿」**（新批次落子目录压根没进视野）；另有 `_ARCHIVE_NAME` 只放行**两段名** ⇒ 盘上 **1444 个 P2 样本被全挡** ⇒ `_observed_from_p2` 恒 0 ⇒ **`required` 一直在按 P1 算**，而它文档写着「统计源选 P2 样本」。四处已修，并配**两条通用守卫**（按语义扫 AST 抓「读产物目录却只扫一层」+ 归档名正则必须放行三段名）。⚠️ **第一批落盘后 `Test存量口径` 会红**——现已**真的红**（2 条，指名 `SMOKE01` 是 camoufox-1 面）——**要重算 `required`，不是把门禁改松**；⚠️ **`build` 切出 camoufox 面 P2 样本后**`test_现有P2样本里元素键名仍是tag_label` **也会红**，那是 M-21 记的存量口径债。✅ **第一批 camoufox 真实批次已跑完（12/12，退出码 0）**——负样本池 **181 → 260 行（0 重复）**、P2 重建到 **543 份**（361 obscura-1 + 182 camoufox-1）、**`required` 重算后无需改动**（`click` 的 `{role,name}` 59 次调用、键 100% 在、值 100% 非空；`goto`/`new_tab` 的 `url` 100%）。★ **但逐条读 evidence 读出「读到一半」**（增量八十）：**同一页、同一动作位置、两次独立运行，`iframe_count` 读出 3 与 0** ⇒ 那个 0 **不是页面事实**。机制：`observe` 是 4 次以上独立往返而导航**异步 commit**，原先 `_settle` 等的 `domcontentloaded` 等的是**当前文档**（旧文档早就是 loaded ⇒ 那句等待**恒真**）。⇒ 治法两条（都在驱动层）：**动作之后先等新文档 commit 再观察**（有界 2s，`click`/`fill`/`press` 一律等——owner 裁定「速度次要」）+ **观察侧读数自检**（跨了文档就重来 2 次，仍不稳记 `degraded += "nav_unsettled"` ⇒ 下游**不判**）。★ **判据换了一代**：导航事件 `framenavigated` **被实测否掉**（它对 pushState 也触发 ⇒ SPA 改 URL 会被误判成「换了文档」⇒ **把好观察当半成品丢掉**）⇒ 换成**文档代号** `performance.timeOrigin`（每文档一个常量、pushState 免疫；实测四条全过）。**修复前基线**（`research/probes/probe_settle_readings.py`）：camoufox 24 份存档 / 388 条带 URL 的观察 ⇒ **过渡态 59（15%）**、**读数打架 6**。⇒ **第二批（修复后，同一份 12 条计划）已跑完**，读数齐了（增量八十一）：**「读到一半」清零**（过渡态 15% ⇒ **3%**、打架 6 ⇒ **1**、`nav_unsettled` **0**；且剩下的 8 条**全落在 ixigua 一个站**上 ⇒ 是站点属性不是半读）；**判据①/② 已在这批上重取**（真·反爬 **8.55%**、`body_text` 中位数 **800.5**）——⚠️ **取数必须在处置之后**（5 条假成功降级 ⇒ 分母 147→152）；✅ **第一批那 79 行池（修复前）已整代回滚**（12 份存档 + 79 行池 + 182 份派生产物）⇒ **「两代不并池」已执行**，⚠️ **回滚与并池的顺序不能反**（共用去重键 ⇒ 先并后回 = 新批次那行被静默吞掉）。⚠️ **obscura 面没有对应机制**（只读存量面，不再产出）⇒ 不补。★ **本批又读出 7 条要人工处置的判定**（增量八十一）：5 条优酷**假成功**（**代码层媒体计数在付费墙上短路**、LLM 压根没被问）+ 1 条 Cloudflare 旧层结论 + 1 条**点击失败**被记成业务分支 ⇒ 三处**代码层没修**，见新增条目 |
| **H-09** | 簇 C 修复的尾巴：题绑定精度 / D-10 投影 / 7 类回归 | 02 感知 / 04 探针 | 编年史 §增量六十「仍未闭环项」 · [04 §增量六十](04-探针七层域.md) | 46 行里 8 行真因是 `not_play_site`、2 行是 `subscription_wall`，被默认题绑定压成 `component_unverified`；**动态派分支触碰「信 LLM 自报」边界**，见 Q-02 |
| **H-10** | `compare-drivers` 将候选侧缺账当成 0 | 01 采集 / 09 纪律 | [`dual_run.py:347`](../../executor/dual_run.py#L347) · [`test_dual_run.py`](../../tests/unit/test_dual_run.py) | `_ratio_diff()` 使用 `(cand or 0.0)`：`_ratio_diff(10.0, None)` 返回 `1.0`，而基线缺失返回 `None`，两侧语义不对称。候选没采到应判 `incomplete`，不能当成候选计数为 0 后判 `fail`；需补单边缺账测试，详见本文 §H-10 |
| **H-11** | `compare-drivers` 的「同面」判定比较计数，不比较面集合 | 01 采集 / 09 纪律 | [`dual_run.py:361`](../../executor/dual_run.py#L361) · [`test_dual_run.py`](../../tests/unit/test_dual_run.py) | `_same_surface()` 直接比较 `Mapping[str, int]`：同一个面跑 2 次对 1 次会判不同面；两侧具有相同的混合面计数却会判同面。应要求每侧恰好一个已知识别面且面名相同，详见本文 §H-11 |
| **H-12** | camoufox 枚举成功但元素全被过滤时丢失 face 账 | 01 采集 | [`camofox_driver.py:1211`](../../executor/browser/camofox_driver.py#L1211)（⚠️ 2026-10-26 camoufox 实现已删 ⇒ **同一处代码原样搬到了 camofox 驱动**；`test_camoufox_driver.py` 随实现一起删了） | `face_ledger = dict(self.last_enumeration) if elements else {}` 把「枚举成功、`kept=0`」误当成「没有枚举账」。过滤档位被清空后，下游无法区分「页面确实无元素」与「元素全被过滤」；应按当前页是否成功枚举决定是否取账，详见本文 §H-12 |
| **H-13** | 声明支持 Python 3.10，实际语法要求 3.12+ | 09 纪律 | [`pyproject.toml:5`](../../../pyproject.toml#L5) · [`arbitration.py:219`](../../executor/arbitration.py#L219) | `requires-python = ">=3.10"`，但该行 f-string 表达式含反斜杠；Python 3.10 编译报 `f-string expression part cannot include a backslash`。需选择重写表达式并补 3.10 门禁，或将最低版本明确提高到 `>=3.12`，详见本文 §H-13 |
| **H-14** | ★**判据①「分支分布」在换层场景没有可比性**——A 侧 60% 的站点是「没测到」 | 01 采集 / 09 纪律 | [设计方案 04 §4.9.8.3–4](../设计方案/04-camoufox执行层替换方案.md) · [`dual_run.py`](../../executor/dual_run.py) · `research/probes/inc72_paired_compare.py` | 第二轮双跑实测：**obscura 在爱奇艺这类客户端渲染站上正文一个字都抽不出**（`body_len=0` + `degraded=['body_text']`，19 条），于是 A 的 47 个站点里 **28 个是 `unresolved`**；而两批样本**看的不是同一批页面**（按落地 URL 配对只重叠 **37/50**）。⇒ 占比差里混着「站点质量差」与「观测能力差」两件事。**缺的那一刀**：分母只取「**两面都测到的页面**」（配对子集已现成，`pair_pages` 已在 `compare-drivers` 里跑着），且 A 侧 `unresolved` 的页面要单独列而不是并进占比 |
| **H-15** | ★**判断点② 的兼容回落把「不是墙」兜成了「墙」** ⇒ 10 条 `login_wall_or_blocked` 假负样本 | 02 感知 | [设计方案 04 §4.9.8.6](../设计方案/04-camoufox执行层替换方案.md) · `perception/llm_perceptor.py::_is_reachable` 末段 + `executor/branches.py::decision_branch`（payload 缺失 → 保持合并态） | **诊断已改（2026-10-21 增量七十三，现场重采证伪）**：原诊断是「② 的『采集未拿到内容』守卫门槛太锐（6 字符 ≠ 空 ⇒ 守卫不触发）」。**现场重采推翻了它的前提**——那 10 条的页面**今天原样还在**（现场与存档逐字一致），**不是读到残留，采集层如实采到了**（`degraded=[]` 是对的）。⇒ **真正的问题是分支映射越权**：② 答 False 且**没填 `access_control_type`**、`page_quotes` 也无阻断关键词 ⇒ `decision_branch` 走「payload 缺失 → 保持题绑定的合并态」⇒ 落进 `login_wall_or_blocked`——而**墙 / 验证码 / 反爬这三个前提一个都没观测到**。**这正是纪律里写着的「贴标签前核前置事实」被绕过了**（`BRANCH_PRECONDITION` 里根本没有 `login_wall_or_blocked` 这一条）。**正解不是调阈值**（阈值怎么调都改变不了「站点只返回了 6 个字」这个事实），而是**兜底改成 fail-closed 落 `unresolved`**。★★ **判据⑤「人判负样本成因」的材料与结论已备齐（2026-10-21 增量七十四）**：工作表 `inc73_human_review`（表一 39 条 B 面负样本，**封闭集** W1 真·墙/反爬 · W2 采集没拿到内容 · W3 不是播放页 · W4 页面坏了 · ? 说不清）**已按分支摆开**。那 10 条**逐条读出来的结论**：**LLM 自己的 evidence 就没提「登录/验证码/反爬」**（写的是「未加载出目标内容」「仅显示加载提示」「页面仅显示通用标题和即将开始的提示」）⇒ **W1 不成立**；**W2 也不成立**（现场与存档逐字一致 ⇒ 不是采集没读到，是页面当时就那样）；**A 面（obscura）对同一批 URL 落的正是 `unresolved`**（两边分歧本身就是证据）。⇒ **这 10 条在词表 W1–W4 里没有归类**——它们是「**站点返回了占位/不可用页，但没有任何拦截特征**」，正确落点是 `unresolved`（fail-closed），**不该进负样本池**（**10/39 = 26% 的池子是分支越权造出来的**）。✅ **owner 已拍板（2026-10-21 增量七十五）：兜底落 `unresolved`，**不开新支**。** 裁定理由两条：① 「站点返回了占位/不可用页」**不是一个新现象**——它就是「没判出来」，`unresolved` 的语义装得下，**新开一支等于给「说不清」发一支专用分支**，而 fail-closed 的口径就是「说不清就落 unresolved」；② **分支是 22 类封闭集**，加一支要连带改 `all_branches()` / 覆盖追踪 / rubric 对齐账（[设计方案/02](../设计方案/02-rubric与代码对齐.md)），代价与收益不成比例。**已落地**：`executor/branches.py::decision_branch` 的兜底改为「`fallback in DEPRECATED_BRANCHES` ⇒ 落 `unresolved`」——**按语义判（合并态不再兜底），不按题号判**；④ 的 `component_unverified` **刻意不动**（它不是合并态，且它的前提事实是成立的）。门禁：`test_branches.py` 新增 3 条（兜底落 `unresolved` / 带细分 payload 仍落细分分支 / 兜底不产生负样本）+ `test_visit.py` 3 条改写。⚠️ **裁定是前向的**：那 10 条**已写进存档的 `login_wall_or_blocked` 还在池里**（`negative.jsonl`），要清得走 `cli downgrade`（只改 P1，**不删池行**）或 `cli purge-run`（删存档 + 池行）——**owner 尚未就这 10 条的历史数据拍板**。⚠️ **原问法已作废**：「爱奇艺那 7 条算不算 `not_play_site`」——**不算**：它们 **video=2（有播放器标签）**，是播放页，只是正文被占位符盖住。★**判据⑤ 的成因词表已改口径（2026-10-21 增量七十六）**：旧的 **W1–W4 作废**（W1 把登录墙与反爬合在一处，而付费墙/无播放控件/组件不可验证/只有预告/导航失败**全都没有位置**；实测 10 条 `login_wall_or_blocked` 里 2 条是 404、2 条是 Cloudflare 挑战、1 条是 App 限定，**0 条能确证是登录墙**）。新词表 = **站点侧 10 项（取值就是分支名）+ 我们侧 5 项（动作码 / 非样本分支）+ `?`**，判据改为「**这条是不是站点明确说了看不了**」⇒ 站点侧留池、我们侧出池、`?` 留空。**那 10 条在新词表里的落点是 `unresolved`（没判出来）**，不是 `not_collected`（现场与存档逐字一致 ⇒ 不是「没读到」）。★**首轮人判已把这 10 条逐条判完（2026-10-21 增量七十七）**：**7 条落 `subscription_wall`**（iqiyi——人工看过是「有资源，但要登录付费才能看」）+ **3 条落 `unreachable_hard`**（`chusuiwenhua` ×2 / `9999pian`——「网页无法访问」）。⇒ **没有一条是墙**，与 H-15 的裁定一致；但同时说明**这 10 条是应该进池的真负样本**（只是标签错了）⇒ 它们要回池**必须先有「改分支」的路**（H-16 / H-17 记的缺口） |
| **H-16** | ✅ **已闭（2026-10-21，增量七十八）**——两处都修了：① **判据换代**（救济只认**门禁语境**）② **补路**（「改分支」有了） | 02 感知 / 08 存档 | `perception/rule_perceptor.py::paywall_evidence`（新）+ `perception/llm_perceptor.py::_diagnosed_access`（新）+ `executor/relabel.py`（新）+ `executor/cli.py::cmd_relabel`（新） · [07-人工复核与判读手册 §2.7 / §6.4](../../docs/07-人工复核与判读手册.md) · `research/probes/inc73_human_review.json` | **原症状**：`paywall_rescue_evidence` 只要求**一个完整短语**命中（`PAYWALL_WORDS` 里就有「开通会员」「续费」「打开app观看」），**不看上下文**；命中后 `_rescued_paywall_decision` 把 ②③ 的负分支**改判**成 `subscription_wall`——**连 LLM 已经诊断清楚的「地区限制」都被改掉**。**实测 12 条逐条看过，0 条是真付费墙**（8 条 `v.qq.com` 主证据是「当前设备不支持播放」+ 错误码 **70017001.1** = 地区限制，真因 `blocked`）。**修复①（判据换代，已落地）**：救济判据 `paywall_rescue_evidence` **删除**，换成**门禁语境**判据 `paywall_evidence`（两层：L1 门禁命题 / L2 CTA + **同句**观看语境）——裸 CTA（「开通会员 享更多精彩」「续费 充值」）**一律不成立**；再加**让位规则**：LLM 已给出结构化细分（② 的 `access_control_type`）**或** evidence 里有**环境阻断形态**（地区/验证码/反爬/404）时**一律不救济**（登录形态不参与让位——导航栏「请登录」与「开通会员」一样高频）；`subscription_wall` 补 `BRANCH_PRECONDITION` 前置事实核验（页面**明说**不给钱/不订阅就看不了正片）。⚠️ **登录形态不参与 evidence 让位**是刻意留的口子。**修复②（补路，已落地）**：新增 `cli relabel`（`executor/relabel.py`）——把「**判定成立但贴错支**」的记录换到另一个**真负样本**分支，**仍留在池里**；处置动作在模块、判据在命令行（与 `downgrade` 同一分工）；落盘时**存档 + 复核档 + 池行一起改**（训练侧吃的是池行；门禁查的是「键在不在池里」）；撞键**并入**不新建行；**默认 dry-run**、`--reason` 必填、凭据闸门挂入口。**仍未闭**：① **历史数据是否照 dry-run 清单执行改判**——**owner 未拍板**，本轮只交付路与清单（8 条 `subscription_wall` → `blocked`，其中 2 条并入已有行；**未 `--apply`**）；② `cause = success` 那一格 `relabel` 确实接不了（它的目标必须是真负样本）——但**处置不是「转正」**，是**出池两步**（`downgrade` + `pool-drop`），2026-10-26 在 H-17 里写清并补了契约测试（台账 **H-17**） |
| **H-17** | ★**首轮人判揭出：池里 7 条是「假负样本」**（资源存在、我们没走到播放页），其中 **5 条机器标签是 `component_unverified`** ⇒ **同页守卫在那 5 条上没拦住**；而 **A 面（obscura）对同一批 URL 记的是 `unresolved`** —— 同一个网站、同一批 URL，两面归属不同 | 02 感知 / 09 纪律 | `executor/gatekeeper.py::_same_page` · `executor/steps/visit.py:216` · `executor/steps/visit.py:228` · `research/probes/inc73_human_review.json` | **首轮人判已判完 39/39（2026-10-21 增量七十七）**：cause 分布 `subscription_wall` 13 / `not_play_site` 12 / ★**`success` 7** / `unreachable_hard` 4 / `broken` 2 / `blocked` 1；**留池（派生）32 / 出池 7**；标签对账：cause == branch 只有 **10/39**，剔除 10 条「已弃用合并态」（`login_wall_or_blocked`）后 **19 条真·标签错**。**`success` 是新造的第三类成因**（「不是负样本」）——⚠️ **与分支体系里的 `success`（`branch is None`＝「视频播出来了」）同名不同义**，词表里必须写清，且有运行时守卫不许 `success` 进 `BRANCH_LABELS`。**7 条的机器分支**：`component_unverified` ×5 / `blocked` ×1（`yn-mg`——现场是 Cloudflare 挑战页，**人工与存档读数不一致，待看**）/ `unreachable_hard` ×1（baidu 跳转链接，A 面没配对到）。★**5 条 `component_unverified` 有两条根因（逐条开存档核过）**：① **「点击失败」路径（3 条：hfzhifei / yrhbcl / jijikb）**——`visit.py:216` 的「点击 ref=… 失败：`Locator.click: Timeout 5000ms exceeded`」**直接记 `component_unverified`**（`reached=False`，**连播放页都没到**）；这条路上**没有点击后观察 ⇒ 同页守卫结构性无从触发**。而「点击失败」就是「我们没点进去」的同义反复 ⇒ **这 3 条必然是假负样本**（人工判 `success` 完全正确）。② **同页守卫的硬判据（2 条：56dy / hnsyzg）**——点击**成功**了，但 `_same_page`（`gatekeeper.py:166-182`）判「不是同一个页面」：**56dy 元素数 37→38（差 1）**、**hnsyzg 正文 1472→1468（差 4 字）**。两次 URL 相同、`video=0`/`iframe=0` 相同，**明明是同一页**，只是页面动态让某个粗粒度特征变了 ±一点 ⇒ 守卫放行 ⇒ 负标签留下。⚠️ **与该函数自己的 docstring 自相矛盾**：它写「正文会因懒加载微调」却把正文当**逐字相等**的硬判据；元素数更不稳定（一个浮层/一个 hover 态就是一个）却没人提，也当**逐个相等**。⇒ **A/B 差别已定位**：obscura 面那几次两次观察**三项全同** ⇒ 守卫触发 ⇒ 降级 `unresolved`；camoufox 面元素枚举与 DOM 面元素集合不同（**会多出/少掉一个**）⇒ 守卫放行。**正解**（待 owner 拍板，**没动生产代码**）：① 「点击失败」与「点击后取不到快照」两条路**不许记 `component_unverified`** ⇒ 落 `unresolved`（前置事实「到了播放页」压根不成立）；② `_same_page` 的元素数/正文改**容差相等**（元素数相对差 ≤10%、正文去空白后前缀一致），让「同一页的两次快照」能被判成同页。⚠️ **处置口径 2026-10-26 纠正（原写「要转正、无路」，两头都不对）**：① **「转正」是错的目标**——这 7 份存档里**没有任何成功证据**（`reached_play_page=False`、`visits[].success=False`、evidence 写「点击失败 / 同一页面」），改成 `branch=None` 讨来的正样本是**假正样本**（教模型「点击失败也算成功」），比假负样本更坏；只改 `branch` 会被 `success_branch_mismatch` 抓住，连事实一起改就是**伪造采集事实**（`downgrade` 坑 4）。要一条真正的正样本**只能重跑那条 URL**。② **「无路」也不成立**：处置是**出池两步**——`cli downgrade --url … --apply`（存档落 `unresolved`，采集事实一律不动）→ `cli pool-drop --url … --apply`（撤池行，原行+理由进 `pool_dropped.jsonl`）；**顺序不能反**（反了门禁当场报 `negative_not_pooled`，看起来像撤错了）。契约测试钉住顺序与「转正≠改 branch」两条：`tests/contract/test_pool_exit_sequence.py`（5 条）。③ ★**顺带修掉一个真的在生产目录上漏扫的 bug**：`executor/downgrade.py::apply_to_root` 的遍历原先是 `glob("T*__*.json")`——只认 `T` 前缀。实测（2026-10-26）生产根目录 78 份存档里 **28 份是 `E`/`P` 前缀**（`E005__e35b7455.json` 这类），**双跑那批判全是 `DR01__`** ⇒ 这 7 条**根本没有命令能碰**，而 CLI 打印的是「没有匹配的 outcome —— 归档可能已被降过（幂等），或判据不匹配」：**判据被冤枉**，读的人会去改判据。现改为走 `archive.select_archives`（「什么算存档」只有一处），只额外折回原档；真数据 dry-run 实证 `E005__e35b7455.json` 已可见；`tests/unit/test_downgrade.py::Test目录遍历` 加 2 条（非 T 前缀 / 探针取证不算存档）。④ ⚠️**「池里 7 条」这个说法也不准**：那 7 条落在 `output/pipeline/dualrun{,2}/camoufox/negative.jsonl`（`DR01`–`DR03`），**生产池 `negative.jsonl` 一条都没有**；生产池里的 **28 条 `component_unverified` 是另一批**记录（同机制、不同批）——那才是要靠下面正解①② 从源头堵的。**已落地（只动研究层与文档，没动生产代码）**：词表加 `success`（第三类「不是负样本」，共 **17 项**）；「是否仍留池」那一列改**派生**（人填版与规则漂了 **4 行**——**同一个 URL 在两批里给出相反的留池值**），人填原值留 `keep_as_negative_human` 审计副本，冲突在 `keep_conflicts` 点名；★**顺带修掉「重生成工作表会把 4 条留池冲突静默抹掉」**——沿用逻辑**分不清「留池列是人填的」还是「派生的」**（两个时代用**同一个字段名**）⇒ 把**派生值**当人的值写进审计列 ⇒ 冲突 **4 → 0**，而汇总「看着正常」；现按**「审计列在不在」分两代**（旧表那一列就是人填的），并认得出**人对派生列的手改**（沿用但进冲突表点名）⇒ **重生成已幂等**（实测 39 行 / 沿用 117 格 / 冲突 4 / 逐格差异 0），测试钉在 `tests/unit/test_inc73_probes.py::Test沿用与重生成`。★**同类第二个坑**：`research/README.md` 那条命令**少列了一个 B 面存档目录**（漏 `dualrun/camoufox`）⇒ 照文档跑 **39 行变 28 行**，而落盘**覆盖**旧表 ⇒ **11 行人判消失**——同样是**静默**失败。⇒ README / 速查 / 手册三处都补了「`--a/--b` 要给全，**落盘前后各对一次行数(39)与冲突数(4)**」。⚠️ **「改分支」那条路已落地**（`executor/relabel.py`＝`cli relabel`，H-16 修复②，2026-10-21 增量七十八）——它管的是「**仍是负样本**、只是标签贴错」；这 7 条**根本不是负样本** ⇒ 走的是**出池两步**（见上），不是 `relabel`。**本行仍未闭的只剩机制侧正解①②**（「点击失败」路径不许记 `component_unverified` / `_same_page` 改容差相等）——**待 owner 拍板，没动生产代码**；处置路径这一侧 2026-10-26 已闭 |

| **H-19** | ★**（2026-10-24 新增）复核队列的覆盖面只到 `REVIEW_BRANCHES` 三支 ⇒「记成功」的假正样本与 `trailer_only` 的错负样本**机制看不见** | 08 存档 / 03 标签 | [07 手册 §4.6 本轮裁定记录](../07-人工复核与判读手册.md) · `executor/review_queue.py` 的 `REVIEW_BRANCHES` · 池 `output/pipeline/negative.jsonl`（260 行）· 样本 `output/pipeline/samples/E006__2729220b__site-4.json` | **2026-10-24 把 224 行复核队列逐条裁完时撞出来的**：队列只收 `{unresolved, trailer_suspect, player_unverified}` ⇒ **分支不在这三支的行压根不进队列**。已实测两类漏网：① **假正样本**——`E006/2729220b` 的网易号《济州岛美食攻略》（`163.com`）记的是**成功**，已进训练样本集（`outcome.branch=null` · `gate.status=passed`），而**它自己的 rationale 反着说**（「当前页面不是目标资源页」「目前未找到该片合集的可播放网址」）⇒ 记录的成功与 agent 自报**互相矛盾**；同 run 的 `koreabycar.com`（同类美食博客）落 `unresolved` ⇒ **进了队列被判掉** ⇒ **同一个坑，一边修得了一边修不了**；② **错负样本**——`T001/d063ddf4` 的央视网《武林外传》第 1 集**正片页**落 `trailer_only`，是**池里唯一那 1 条**（教模型「正片页 = 只有预告」，误杀，见 **M-12**）。⇒ **不是「没判完」，是「机制判不到」**：处置要么扩 `REVIEW_BRANCHES`（把「记成功」与 `trailer_only` 纳入复核面）、要么另开「正样本抽检」通道 ⇒ **都改代码 ⇒ 等 owner 拍板**。⚠️ 池里另有 **8 条 `v.qq.com` 的 `subscription_wall`**（H-16 那批 8 条，**同样不在三支里**）⇒ 扩面时一并纳入。⚠️ **2026-10-27 订正一处**：「队列只收三支」在**代码侧已经不成立**——`blocked` 已于 2026-10-26（方案 20 §R1）扩进 `REVIEW_BRANCHES`（`executor/review_queue.py:192`）。而盘上那份 `output/pipeline/review_queue.jsonl` 是 **10-24 18:31 导出的旧稿**（224 行、**行行带人裁定** ⇒ **不能覆盖重导**）⇒ **「队列里没有 `blocked` 行」是稿子旧，不是机制缺**：2026-10-27 只读重导实测 `待复核 515 条 = unresolved 431 · blocked 70 · player_unverified 8 · trailer_suspect 6` ⇒ **`blocked` 已经进面**。⇒ **仍等 owner 拍板的还是那两条**：① 「记成功」的**假正样本**（机制判不到）② `trailer_only` 的**错负样本**（M-12） |

| **H-20** | ★**（2026-10-26 新增）执行层换 camofox-browser ⇒ 采集链还没在新面上真跑过**（第一梯队卡在这） | 01 采集 / 09 纪律 | 本文 **§停点** · [设计方案 21](../设计方案/21-camofox-browser执行层接入方案.md) · `pyproject.toml` 依赖段（`playwright` 已摘） · 编年史 §增量九十三 · 编年史 §增量九十四 | **代码与门禁已全部落地**（`camofox_session.py` / `camofox_driver.py` / 三张面 / `LEGACY_DRIVER_SURFACE`），**门禁绿**。**环境已通**（`camofox-browser-mcp` 适配器 + 上游 REST `:9377`，**本仓库不启动浏览器**）⇒ **第 4 轮那 10 条已在 camofox 面上真跑过**（批次 `plan-camoufox-prod1`）⇒ ✅ **camofox-1 面的 `click`/`fill`/`press` 三条已按真实调用转正**（`exercised=True`；`required` 口径**逐条按真实键在场率校**：`click` 的 `role`/`name` 100% ⇒ 都必填 · `fill` 三个键 100% ⇒ 都必填 · `press` **只有 `key` 在场** ⇒ `role`/`name` 保持可选）。⚠️ **但第 4 轮自己带出了五个「零报错」的缺陷**（§停点 · R5 实跑那一段的五个缺陷）⇒ **卡点从「没跑过」变成「跑 → 读 → 修 → 清 → 再跑」这个循环还没走完**（已走四轮；**第 5 轮在跑**）⇒ 剩下的：**逐条打开存档读 evidence**（缺陷⑤ 就是这么读出来的）+ **重建产物与门禁** + **采集侧账** + **人判存量那 4 条 agent 负样本**。⭐ **2026-10-27 更新：第 5 轮已跑完**（砍到 **6 条**跑完，理由见编年史 §十一 · 5-1-1）⇒ **缺陷⑤的修法在 6 档真数据上站住**（**建页路由失败 0 次**；T048 用时 5972 s → 1619 s）⇒ **逐条读 evidence 已做**（读出 **M-43** / **M-44** 两个新开口项）⇒ **重建产物 + 门禁已做**（`build --overwrite` → `task-view --all --overwrite` → `check-archives`；`4252 passed` 全绿）⇒ **采集侧账已跑**（6 档 **61** 条打点、**单面** `camofox-plain`、跨面**结构上**为 0）⇒ **并发批的 6 条 `unreachable_hard` 已逐条单独复核并出池**（池 331 → 325）。⇒ **剩下唯一的 owner 动作**：**人判存量那 4 条 agent 负样本**（材料已备，见 §停点 ⑥）。⚠️ **camoufox 不许接回来**（owner 裁定）——`camoufox-1` 面**只读不再产出**，但存量 430 份 P2 样本 + 14 份顶层 P1 存档**仍读得懂**（`LEGACY_DRIVER_SURFACE`） |

---

## 中 · 该做、没阻塞主线

| ID | 事项 | 域 | 记录锚点（证据） | 现在差什么 |
|---|---|---|---|---|
| **M-01** | 「窗口 ≠ 候选集合」修复**未实施** | 01 采集 | [设计方案/05-候选提取与窗口同源修复方案](../设计方案/05-候选提取与窗口同源修复方案.md) · [01 §判断点①根因](01-采集执行域.md) | 根因已定位、方案已写；要动 `_link_window` 与 `extract_candidates` 的接线，**动的是判断点 ① 的核心**，所以一直压后 |
| **M-02** | 存量 rationale **63 档 / 458 步未重生成**（2026-10-25 实测，原写 64 档 / 480 条） | 05 rationale | [05 §INC46 收尾](05-rationale与闸门域.md) · 编年史 §增量四十六 | 仍是事后反思口径；`regenerate-rationales --only-old` 已就绪（≈**767** 次 LLM 调用），**卡后端配额**。**护栏已落（2026-10-25）**：P2 逐条标 `rationale_era`、旧代闸门一律 `not_run`（不判通过也不判失败）、`build --rationale-era pre_action` 可出同口径批次 ⇒ **这批数据不再是隐性污染，只是没升级** |
| **M-03** | `executor/cli.py` God 文件拆分 | 09 纪律 | [09 §`cli.py` God 文件拆分](09-门禁与纪律域.md) · **2026-10-26 实测 4009 行**（文档写 2978；**2026-10-21 记的 3535 也已漂**） | 纠正版三阶段方案已评审；**阶段 0 的门禁接线不可跳过**——AST 白名单不扫树，搬完门禁对新文件静默失效。同批架构审查另记（**2026-10-26 重测**）：`orchestrator.py` **1781** 行 · `llm_perceptor.py` **1835** 行 · `agent_loop.py` **1596** 行（未变）· `evaluation/golden/builder.py` **3469** 行（2026-10-21 记的 1512 已**翻倍多**——方案 16 的校准回路几乎全落在这一个文件里 ⇒ **它现在比 `cli.py` 更该拆**；⚠️ 但那条工作线**正在动它**，**别与它抢**）；优先拆 `cli.py`，其余按变更频率再排 |
| **M-05** | 153 条 `no_text_signal` 的「证据丰富化」 | 04 探针 / 08 存档 | [04 §未闭环](04-探针七层域.md#本域已知未闭环项) · [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | `evidence` 短到 L3/L4 读不出信号；下一批 proberun 应优先覆盖 |
| **M-06** | L4 VLM 任务级上下文（D14.6） | 04 探针 / 06 组装 | [04 §L4 任务级上下文](04-探针七层域.md#本域已知未闭环项) · [设计方案/14 §5](../设计方案/14-完整任务视图与LS上传.md) | **前置条件已满足**：T 视图已落地（[`assembler/task_view.py`](../../assembler/task_view.py)），可以给 `VLMReviewInput` 加 `task_context`（2026-10-20 核：该字段**尚不存在**）。第一步**只加字段，不改决策树** |
| **M-07** | 旧 per-unit LS 上传路径弃用（`--legacy-unit-trajectory`） | 08 存档 | [04 §未闭环](04-探针七层域.md#本域已知未闭环项) · [设计方案/14 §9](../设计方案/14-完整任务视图与LS上传.md) | ⚠️ **域文档「ls_bridge 均未入库」已过时**：12 个文件在 `git ls-files` 里。实际剩余 = **过渡期到 2026-11-09 后删旧路径** + 真实 LS 项目上的端到端验证 |
| **M-08** | judge 输出非确定性（E014 step0 翻转 anchored↔mild_hallucination） | 05 / 07 | [05 §未闭环](05-rationale与闸门域.md#本域已知未闭环项) · [07 §未闭环](07-评估与黄金集域.md#本域已知未闭环项) | 加 `temperature=0` 或多数投票；属 `llm/client` 层**独立批**，别混进判定批 |
| **M-09** | D-3 一致率 66.7% + `d3_scorer` 是否也做 capping 两档 | 07 评估 | [07 §D-3/D-9](07-评估与黄金集域.md) | 需 few-shot prompt 调优后才能定阈值；capping 待 D-3 覆盖率实测后再定（INC42 只做了 d9） |
| **M-10** | `rule` vs `llm` 分歧率比对**未做** | 02 感知 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) | `replay --perceptor both` 需要正文全文存档，新批次已具备条件；跑前先 `check`（模型名写错时每题返回 `None`，症状像「LLM 判不了」实为 404/403） |
| **M-11** | `CONFIDENCE_JUDGED = 0.8` 是**未校准先验** | 02 感知 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) | 拿到标注集后按题校准；现在是**拿常数当概率用** |
| **M-12** | `trailer_only` 词表覆盖率与误杀边界未摸底 | 02 感知 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) · [07 手册 §4.6](../07-人工复核与判读手册.md) | ⚠️ **2026-10-24 订正**：本条原写「`trailer_only` **一次都没触发**」——**已过期**：负样本池 260 行里**有 1 条** `trailer_only`（`T001__d063ddf4__106df089` = 央视网《武林外传》第 1 集**正片页** `shaoer.cctv.com/2014/02/05/VIDE1425362695693304.shtml`，`evidence` 写「1 个候选控件全为预告：`['片段']`」）⇒ **误杀边界已经踩到过一次**：正片页的「片段」页签被判成预告。⚠️ **它还是池里唯一那 1 条** ⇒ 教模型「正片页 = 只有预告」。⚠️ **它进不了复核队列**（`trailer_only` ∉ `REVIEW_BRANCHES`）⇒ 机器自己看不见 ⇒ 台账 **H-19** |
| **M-13** | `player_unverified` 在真实数据里 **0 次出现** | 02 感知 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) · [branches.py](../../executor/branches.py) | 分支代码通了，但现有存档都跑在它落地之前——**没被真实数据检验过** |
| **M-14** | 规则版 `PLAYER_OK` 对真实视频站正样本召回 **0** | 02 感知 / 06 组装 | [02 §未闭环](02-感知与判定域.md#本域已知未闭环项) | **媒体计数不再许诺成功**（2026-10-23 · M-26：护栏可以**否**、不能**许**）⇒ 规则版 ④ **一律 `None`**，连标签在的页面也不判成功；真实站又多是 iframe/JS 播放器。**W1 正样本只能靠人工复核**（`review --write` → 填 `verdict` → `--apply`）。⚠️ 召回为 0 的**成因变了**（原先「只认 `<video>`/`<audio>`」），结论没变 |
| **M-15** | 存储侧只有**第一批 5 条样本** | 06 组装 | [06 §未闭环](06-组装域.md#本域已知未闭环项) | D-2 报表「0/5 不可判定」的根因是**一条 click 都没有**，不是缺字段；正样本需人工复核补 |
| **M-16** | `evaluation.cli` 0 数据时不报警 | 09 纪律 | [09 §未闭环](09-门禁与纪律域.md#本域已知未闭环项) | 「0 份存档 → 0 条样本」与一切正常**同形**；判据应是「读到 0 份存档」而不是「0 条样本」。2026-10-20 核：`evaluation/cli.py` 里无此判据 |
| **M-17** | ✅ **已闭（2026-10-26）**：`03-数据契约 §G1 黄金集契约` 的旧分支数（14 key / 15 类 / `all_branches()`=13）**已回填**为 21 key / 22 类 / 20 | 03 标签 | [编年史 增量九十一](../90-增量编年史.md) | 新 4 分支要**主动构造**而不是「自然跑出」（增量十四 §五）——**这一半仍未闭环**（是**采集面**的事，不是**文档面**的） |
| **M-18** | D-9 retest round=2 队列的真人重测 | 08 存档 | [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | κ = 0.176 是 agent 口径；要真 test-retest κ 得人工盲打后 `--force` 覆盖 |
| **M-20** | ✅ **已闭（2026-10-26）**：上半年表列的域文档过时行**已反向同步** | 09 纪律 | [编年史 增量九十一](../90-增量编年史.md) §六/§七 | 2026-10-26 那轮「文档滞后于代码」梳理把域文档逐条回填了（分支数 / 标签宇宙 / 守卫 / 产物数 / L6·L7 状态 / obscura 口径 / 人工行数 / 桶分布）；⚠️ **回填的是口径，域文档里仍未闭环的实质问题一条没少** |
| **M-21** | **方案 15 的 R3 / R4 / R5 未开工**——agent runtime 改 pull 模式、感知层退位、P1 契约迁移 | 01 采集 / 02 感知 / 跨层 | [设计方案/15 §7](../设计方案/15-执行器与agent-runtime重新定义.md) · 编年史 §增量六十四 | **R1 / R2 已落地**（工具面轴：catalog 逐面、契约逐面判、`driver` / `tool_surface` 三处字段），**R3 起未动**：① 采集面 3 条**只进声明没接上**（`exercised=False`），agent runtime 仍是 push 模式（执行器推回执）；② `ref` 机制与 I6 白名单**还在**，`{tag,label}` 目标编码**还在**；③ 感知层**六道站内题仍在热路径**（它只剩终局验收一个岗位才对）；④ 新批次**还没打新工具面**（R5）。⚠️ 这三阶段是**破坏性**的（I3/I6 退役、`ActionTarget` 换形），owner 已授权。⚠️ **R3 开工前要先钉一条**：采集面**工具名**是协议级（两面可共用），**返回内容**是面级（obscura 的 `interactive` 给 `{ref,tag,label}`、camoufox 给 `{role,name}`）——采集面读到的与动作面能定位的**必须同一套词汇**，否则是把 push 模式的老毛病换马甲。给 obscura 面加采集面 = 方案 04 P3 的活（把 obscura 的 `interactive` 输出也换成 `{role,name}`）。★ **2026-10-21：这与 H-08 的「判据②③不可比」是同一件事**——统一 `elements_total` 口径正是「给 obscura 面加采集面」。**两处分开记会被当成两件事排期**，做的时候按一件事做。✅ **口径那一半已落地**（增量七十：共享口径 `SHARED_DOM_JS` + `filter_ladder` + `Observation.element_basis` 新列，`compare-drivers` 的判据③拆成「面内口径 `incomplete`」+「共享口径承载 pass/fail」+ 归因表）；⚠️ **语义统一那一半仍在**——`elements_total` 仍是两个义，**彻底统一得动 obscura 的方言**（把 `interactive` 输出换成 `{role,name}`），那是 R3 的活。⭐ **2026-10-22（增量七十九）R3 的范围缩小了**：owner 裁定生产链默认执行层切 camoufox ⇒ **采集面现在只有一个生产面**（camoufox）⇒ 「**给 obscura 面加采集面**」（把 obscura 的 `interactive` 输出也换成 `{role,name}`）**已不再需要**——obscura 面降为**只读存量面**（`obscura-1` 面不再产出新样本），它的 `elements_total` 是多少**不影响新数据**。⇒ **R3 的剩余工作缩到**：① agent runtime 改 pull 模式（执行器推回执 → agent 主动拉）；② 感知层退位（六道站内题撤出热路径，只留终局验收）；③ `ref` 机制与 I6 白名单退役。⚠️ **但「统一 `elements_total` 语义」这件事并没有消失**——它从「R3 的活」变成「**存量数据的口径债**」：存量 obscura 批次的 `elements_total` 与新 camoufox 批次**不可比**（这正是 D-2 打分口径漂移风险的来源，Q-05 记的那条）。**混池即错**，所以那批存量要么标批次边界、要么不进训练池 |
| **M-22** | 共享口径的 `pass/fail` 缺少最小样本与任务配对约束 | 01 采集 / 09 纪律 | [`dual_run.py:487`](../../executor/dual_run.py#L487) · [`dual_run.py:301`](../../executor/dual_run.py#L301) | 当前只要两侧 `shared.kept` 中位数存在即可判 `pass/fail`，没有最低覆盖率、缺账阈值或同 task 配对要求。大量观察里只有 1 条共享样本，也可能决定整批判据；需明确门槛并补单样本、任务错配、低覆盖测试，详见本文 §M-22 |
| **M-23** | `executor` 只依赖感知协议的架构约束未被代码兑现 | 01 采集 / 02 感知 / 09 纪律 | [架构总览 §依赖约束](../01-架构总览.md#L86) · [`branches.py:33`](../../executor/branches.py#L33) · [`gatekeeper.py:58`](../../executor/gatekeeper.py#L58) | 文档要求 `executor` 不依赖任何 `perception` 实现，但 `branches.py` / `gatekeeper.py` 直接引用 `perception.rule_perceptor` 的词表与纯函数。需把共享知识移到 `common`、协议或独立地基，或正式修订约束并加入 AST 门禁，详见本文 §M-23 |
| **M-24** | ✅**已收口（2026-10-21）：xlsx 工作单删除，语义矛盾随参照物一起消失** | 09 纪律 | [09 §xlsx 测试清退](09-门禁与纪律域.md) · [`test_xlsx_backfill.py`](../../tests/unit/test_xlsx_backfill.py) · [`test_inc37_validator_pass.py`](../../tests/unit/test_inc37_validator_pass.py) | 原症状：同一份缺失工作单在 fixture 中走 `skip`，存在性测试却硬失败，INC37 又选择跳过，干净 clone 既可能红也可能静默少测。**处置**：两份 xlsx 已被人为删除且不可恢复（`.dumate/` 只剩 2026-10-12 早期快照，推不出终版），所以「xlsx 是发布必需还是可选审计输入」这个问题**没有可执行的答案了**——按「可选外部证据已永久缺失」处理：删掉 11 条拿工作单当参照物的测试（1 条永久 fail + 10 条永久 skip），保留 5 条纯 `annotations.jsonl` 断言。门禁 **0 failed / 0 skipped**（总条数当时 3398→3439，因工作区并发编辑）。详见本文 §M-24 |
| **M-25** | ✅**判据⑥ 的工具已建并已跑出第一手读数**（2026-10-21 增量七十四）——剩下的是**材料窄**，不是没工具 | 01 采集 | [设计方案 04 §4.9.8.8](../设计方案/04-camoufox执行层替换方案.md) · [`inc73_action_replay.py`](../../research/probes/inc73_action_replay.py) · `executor/browser/camofox_driver.py::resolve` | **工具已建**：`inc73_action_replay`（**联网**，从存档取 `steps[]` → 在裸 camofox 里重放 → 记每步定位结果），判定走**生产链同一条路径**（`CamofoxDriver.resolve()`：只定位不点击，走 `_locate` 的三态 `ok`/`not_found`/`ambiguous`——⚠️ 2026-10-26 camoufox 实现已删，**这条路径原样搬到了 camofox 驱动**）——探针另写一份的话「重放定位率」就不是生产链的读数。归因**三列并排**（否则把两件事混成一件）：重放结果 × **当时在不在采集面阶梯里** × **页面漂没漂**（漂 = 站点改了版，**不是名字的锅**）。**读数（16 条动作 / 13 站）**：**16/16 全部定位到（`matches=1`）**，其中 **8 条页面已漂**（标题或正文长度变了）却仍定位得到 ⇒ **动作面在 camoufox 上可移植**；**旧批次那 3 条 `click` 超时不是可移植性问题**——重放时同样目标仍定位到（超时是**窗口不可渲染**那条，已由 headless 口径反转处置）。⚠️ **材料还窄**：① **全是 `click`，没有 `fill`/`press`**；② 动作都发生在**影视详情页**上，没有「搜索框填词 + 回车」那类；③ 3 条旧存档**没有可比基线**（无标题也无正文长度）⇒ 漂移记 `null`（未知）**不许记「没漂」**。⇒ **判据⑥ 现在的读数只覆盖「在详情页点一个链接/按钮」** |

| **H-18** | ★**门禁 `success_branch_mismatch` 报的是假阳性**——`_match_outcome` 对同一个 URL 取**第一条** outcome，而站点阶段与播放阶段**各记一条** ⇒ 拿错了那条 | 09 纪律 | [`integrity.py:233`](../../executor/integrity.py#L233) · [`integrity.py:345`](../../executor/integrity.py#L345) · 编年史 §增量八十一「仍未闭环项」 | 实测 2 份（`T001__67a05faa` / `T054__93f110b6`）：`T001` 是**存量 obscura 存档**⇒ **既存缺陷**，新批次只是让它显形。修法：同 URL 多条 outcome 时，**按 `reached_play_page` / 阶段**挑那条对应的（而不是第一条）⇒ 门禁是查「自相矛盾」，**不能自己挑错一条再报矛盾**。**修法与验收见本文 §H-18** |
| **M-28** | ★**`unreachable_hard` 是采集失败，却进了负样本池**——22 类分支里**没有「采集失败」这一类** | 03 标签 / 08 存档 | 编年史 §增量八十一「仍未闭环项」 · `branches.BRANCH_PRECONDITION` | 本批 **13 条**：传输层 `Page.goto` 失败（`baidu.com/link?url=…` 跳转挂掉）⇒ **采集失败**不是业务结论，但它们**照进了负样本池**（教模型「这种站不可看」）。口径**还没定**：「采集失败」要不要单列一类、还是并进 `unresolved`（不进池）？⇒ **先别动，先记账**（现有 13 条在池里是**已入库的事实**）。**修法与验收见本文 §M-28** |
| **M-29** | ✅ **已闭环（2026-10-23）**——**v8 快照的 d5 pred 已是当前评分器的输出** | 07 评估 / 校准回路 | `output/pipeline/calibration/v8/snapshot.json`（`dimension=d5` 的 **211 条**：`5×178 / 2×7 / null×26`）· 本文 §M-29 | D-5 恒真已修（第四条检查 · 结论的证据核，见 [对齐账 §2.4](../设计方案/02-rubric与代码对齐.md)）。**v7 快照那份 208 条全 5 保持原样**（改历史快照 = 事后调基线）⇒ 另冻 **v8**：继承 v7 的切分（131/60/103，`derived_from.version=7`，`holdout_runs=0`）、**pred 侧按当前评分器重挂**。**独立重算核对**（不经 candidates，直接从 P1 存档现算 `score_d5`）：211 条**逐条 0 不一致**，分布与快照同；当场 `evaluation.cli --dimension d5` 的「2 分 8」与快照 7 条 + 1 条（不在冻结档）一一对应。**d10 与 v7 逐条一致**（289 条）· 分歧卡仍 **33 张**（11 张落冲突 URL）· **v7 文件零改动**。**改了三处**：`overlay-scout` 弃权写 `null`（四维键齐全，没样本才 `{}`）· `freeze --refresh-scores`（缺 id 退 2、`golden`/`code` 不动）· 继承式冻结不给 `--refresh-scores` 时 stderr 喊一声；另加 `agreement` 遇已存在的 `snapshot.json` **退 2**（要重算加 `--force-snapshot`） |
| **M-30** | ★**（2026-10-23 新增）金标在「同一个 URL」上不自洽**——不是个别站，是 **19/131** 个 URL 的 d10 金标带了多于一种标签 | 03 标签 / 07 评估 / 08 存档 | 复算：`snapshot.json` 的 d10 `gold_value` 按 `candidates.jsonl` 的 `url` 归并（脚本见 §M-30） | 33 张 v7 分歧卡里 **11 张**落在这 19 个 URL 上。最硬的两族：`bilibili.com/bangumi/play/ep313572` = `subscription_wall` **4** / `component_unverified` **10**；`cmgo.com/…/cmgo655_功夫` = `not_play_site` **10** / `unreachable_hard` **3**。⚠️ **URL 级冲突 ≠ 金标全错**：同一 URL 两次访问一次成功一次 `unresolved` 是**真事实**（站点漂移），必须先排除；但 `subscription_wall` vs `component_unverified` 这类**同一页面不可能同时成立**。⇒ 立项做「同 URL 金标裁定」（按时刻/批次归并 or 重新标注），**在此之前这 11 张卡的分歧读数不能直接进 H-02 的方向判定**。L-10 只记了 m.ixigua 一例——**范围要放宽**。⚠️ **2026-10-23 复核后收窄了「差什么」**：33 张卡已 **100% 人工分诊**（冲突 URL 上 5 `gold_defect` / 4 `evaluator_logic` / 2 `evidence_missing`）⇒ 缺的**不是逐卡判桶，是 URL 级定性**。★ 同一批复算还翻出一条**没入过档的**：切档是按**行**不是按 URL——289 条站点级冻结行只落在 131 个 URL 上，**25 个 URL 跨档**，**65/101 持有集行与调参池/回归集同 URL**（持有集独立性被污染）。两条一起挂在 **Q-11 / Q-12**，**owner 已于 2026-10-23 裁定**：**Q-11 = 按访问**（金标粒度按访问 ⇒ 多头标签是**正常事实**，只标注不判缺陷）、**Q-12 = 按行**（不重切 ⇒ 跨档泄漏保留，所有持有集读数须带附注）。✅ **机器侧已落地**（2026-10-23）：`calibration/urlconflict.py` + `agreement` 的「URL 级金标读数」+ `--json-out` 的 `url_gold_conflicts` + `--emit-url-conflicts` 清单（事实块机器刷、**两列结论人填、重跑不抹掉**）；读数 19/131、跨档 25、11/33、泄漏 65/101 已复算并落盘。✅ **人侧已收口（owner 2026-10-23 确认）**：19 个 URL 的定性**全部定稿 = 18 漂移可解释 / 1 不可能共存 / 0 待查**（清单 `golden/data/url_gold_conflicts.md`；**7 条经浏览器现场重采**、**12 条据 P1 存档读数**，依据列各自标注了来源）；机器重跑后**逐字不变**。⇒ **M-30 的 URL 级定性闭环**（剩下的是把「不可能共存」那几条交上游标注流程，见 §M-30 现场重采）。▶ 过程件（施工单）已删除，结论折进本文 §M-29 / §M-30 两节 |
| **M-31** | ★**（2026-10-23 新增）`perception/replay.py` 的「旧判定」列在 23% 的 ④ 回放单元上静默变空** | 02 感知 | [`replay.py:230`](../../perception/replay.py#L230) · 本文 §M-31 | `_outcome_for` 只拿 `visit.url` / `landed_url` 两个键做精确串匹配，而 outcome 的 `url` 是**最终观察 URL**（换路径 22 / 带 fragment 7 / 换 query 2）⇒ 全库 **135** 条 ④ 回放单元里 **31 条取不到**，`recorded_question` / `recorded_branch` / `recorded_source` **三个字段一起变空**。**症状是静默的**：空列**看着像「这条没有存档记录」**，而真相是「存档里有，只是没对上键」。⇒ 还跟 §H-18 同族（同一 URL 多条 outcome 时取第一条 ⇒ 可能取到**另一道题**的结论）。**修法**：按 (URL 规范化, question) 匹配，取不到要**显式说取不到** |
| **M-32** | ★**（2026-10-24 新增）D-3 的五类词表没有 rubric 文档出处** ⇒ `anchors_drift()` 拦不住它漂——**C-10 消灭的是「档位文本手抄」，词表是同一毛病的另一半** | 07 评估 / 09 纪律 | [`rubric_version.py`](../../evaluation/scorers/rubric_version.py) `ANCHOR_TABLES` / `anchors_drift()` · [`d3_scorer.py`](../../evaluation/scorers/d3_scorer.py) `D3_CATEGORIES` / `D3_SYSTEM_PROMPT` · [rubric D-3 卡](../设计方案/在线视频场景rubric.md) · 本文 §Q-14 | 实测：`ANCHOR_TABLES["d3"]` 里**只有档位文本**（5/4/3/2/1），**五类词表一个都没有**；`anchors_drift()` 逐档核的也是**档位文本** ⇒ **词表改了不会红**。而词表的**唯一定义处**是 `d3_scorer.py` 的 `frozenset` + **手写散文**（「nav_aggregator：导航/聚合站——把用户引到外部播放站，自身不存正片。」）——**这正是 C-10 要消灭的「第二份手抄」**。⚠️ 契约测试 `test_5类_契约冻结` 把代码冻在**字面量**上、**不是冻在 rubric 上** ⇒ rubric 改了它照样绿。⇒ **修法**：词表进 `rubric_version.py`（与 `ANCHOR_TABLES` 同表族）+ prompt 从表渲染（**这一步现在就能做**）；**rubric 文档侧的对照面等 Q-14 定口径**——Q-14 走 A 路（文档收下这五类）⇒ 对照面可建；走 B 路（明写「类别口径在评分器」）⇒ 把 D-C7 的管辖边界写死，**别留两套口径并存** |
| **M-33** | ★**（2026-10-24 新增）D-3 的 5/4 档判的是「落在契约 5 类」，rubric 说的「判对」是「与人工类别标注一致」——而人工类别标注不存在** ⇒ 代码把「没查」写成了「判对」 | 07 评估 | [`d3_scorer.py`](../../evaluation/scorers/d3_scorer.py) `score_d3`（第 471–477 行）· [rubric D-3 卡「判据」栏](../设计方案/在线视频场景rubric.md) · [对齐账 §2 D-3 缺口②](../设计方案/02-rubric与代码对齐.md) · 本文 §Q-14 | 实测：`score_d3` **全程不与任何金标比对**——`category ∈ D3_CATEGORIES` 即「判对」，evidence 具体即 5 档；**理由串却写着**「category **判对** + evidence 引用观察具体短语——rubric §D-3 锚点 5」。而 rubric 的 5 档是「**判对**，且选中/排除理由引用了观察中的具体依据」、2 档是「**判错**（播放站判成非播放站，或反之）」，判据栏写的是「**对照人工类别标注（黄金集预标）**」⇒ **「人工类别标注」那一栏至今是空的** ⇒ **评分理由高报了它检查过的东西**。⇒ **两条腿**：① **理由文案立刻改**（写「category 落在契约 5 类；**未与人工类别标注比对**」）——文案不修，**每一条 D-3 判定的 reason 都带着这句不成立的话**；② **分数语义本身**要等人工类别标注（对齐账缺口② / L-04 / M-18），**不是改文案能解决的** |
| **M-34** | ★**（2026-10-24 填表完成）软维度金标 364 格已填**——AI 判读、人终裁；**它仍是「三分之二读数为空」的根因**（填的是 v8 那张表，读数要等 v9→v10 那一对才动） | 07 评估 | [07 手册 §7.2.1 判读口径 / §7.2.2 待裁开口 / §7.7 人终裁流程](../07-人工复核与判读手册.md) · [域文档 §07](07-评估与黄金集域.md) · [`research/probes/gold_fill_soft_dims.py`](../../research/probes/gold_fill_soft_dims.py)（判读表·凭据）· [`gold_facts.py`](../../research/probes/gold_facts.py)（事实底稿）· [`gold_peek.py`](../../research/probes/gold_peek.py)（看现场）· 表 `trajectory_pipeline/output/pipeline/calibration/v8/gold_worksheet.jsonl` | **已做（2026-10-24）**：364 格**填完**——待填 0 / 已填 **210** / 人判判不了 **154**；**已导入** `annotations.jsonl`（91 条；初版署名 `claude-ai`，**同轮二次复核后改签 `reesn`**）⇒ **已重挂进 v9**（`freeze --version 9 --from-version 8 --refresh-gold`，逐格核对**与工作表 0 处不一致**）⇒ `gate` 按预期**退 2**（金标重挂过 ⇒ 下一对可比是 **v9 → v10**）。<br>**人判分布**：D-2 5×20 / 4×5 / 判不了 66；D-4 5×38 / **4×10** / 判不了 43；D-5 5×59 / **4×9** / **3×17** / **2×4** / **1×2**；D-6 5×45 / **3×1** / 判不了 45。**人机一致率**（`agreement --version 9`，容忍 ±1）：d2 **18/18** · d4 **43/43** · d5 **69/80 = 86.2%** · d6 **45/45**。⚠️ d2/d4/d6 的 100% **不可读成「机器准」**：分母只算两边都出分的行（`dim_agreement` 剔 null）⇒ d2 分母只剩 **18**（机器 73/91 出不了分）、d4 的机器 5 分是**白送**的（非 ④ 的结论一律 `skipped` ⇒ 5 分）、d6 只差「值不符 vs 观察没提」一格。**有信息量的只有 d5 的 11 格超差**。<br>**⚠️ 本条同时新立了 6 条 rubric / 机器侧缺口**（详见 §M-34 补记）：① **D-4 边界行与锚点自相矛盾**（「直接判无排除 = 3 档」vs 锚点「2 处」）⇒ 判读按锚点取 **4 档**（手册 §7.2.1 ③）；② **D-5 决策树只有 6 个节点，91 条里 62 条落在没有节点的分支上**（`unresolved` 24 / `unreachable_hard` 17 / `login_wall_or_blocked` 8 / `broken` 6 / `blocked` 5 / `trailer_only` 1 / `trailer_suspect` 1）；③ **机器 D-5 从不产出 4 档**（`d5_scorer.score_d5` 把「前提不成立」并进 3 档）⇒ rubric 的 4 档锚点在机器侧**没有落点**；④ **机器 D-2 的检索面只有 `interactive_elements`** ⇒ 元素在正文里出现也算「真实存在」（rubric 说的是「在**观察存档**中检索」）⇒ 73/91 出不了分，一大半是这条差异；⑤ **机器 D-4 只管判断点 ④** ⇒ ~78% 的行**白拿 5 分** ⇒ 一致率虚高；⑥ **D-6「值不符」与「观察没提」在机器侧同形**（都判 `undetermined`）⇒ 网易号那格只能人判。<br>**⚠️ 5 处待主人裁的开口**（手册 §7.2.2）：优酷 v_show 判据边界（M-30 §2 三读法，**4 行**）／西瓜「打开App观看」≡「打开App看完整内容」（M-30 §8 开放项，**2 行**）／`unreachable_hard` 入不入负样本池（**18 行**）／第三方站策驰影院入不入池（**1 行**）／腾讯专题页 4 个 iframe **要现场重采**（**1 行**）。⇒ **这 5 处裁完，对应格子的档位要改**（改判要 `--force`）。<br>**⚠️ 最大的限制：金标是 AI 判的**（初版署名 `claude-ai`；**2026-10-24 同轮二次复核**——364 格逐格按 rubric 锚点 + 手册 §7.2.1 口径 + P1 存档事实重核，**分数一处没改**，只把署名改成 `reesn`）⇒ **金标与评估器同源 ⇒ 一致率天然偏高**。判读表与评分器虽是两套独立实现，但**口径是同一次思考定的** ⇒ **独立性打折**。⇒ **主人提交前必须过一遍**（手册 §7.2.1 是判读依据的完整清单 · §7.5.2 是二次复核记录）。<br>**原任务背景（保留）**：v8 首版 `goldwork worksheet --version 8` 出表 **364 格**（调参池 91 行 `status=scored` × 4 维），**37 行无样本不进表**；填表前 v8 快照 **844/1133 `ungraded`**、条款级一致率只覆盖 D-10、T-2/T-3 恒测不成、抽样 S2/S3 总体为 0 ⇒ **填表 ≠ 读数动了**：v9 与 v8 **不可比**，要等 v10 才有可比的一对。⚠️ **填表口径原有四处错**（已随本次落进手册 §7.2.1）：D-4 档位方向、D-5 不只六个节点、D-6 片名取存档顶层、`gold_label` 不是这四维的分数；另 **7/91 条 `pred_branch` ≠ 存档 `branch`**（P1 人工 relabel 过、快照没更新）⇒ **底料以存档为准**、**不重跑底料**。 |
| **M-35** | ✅ **已闭环（2026-10-24）**——D-5 评分器只核「自洽」，从不核「分支是不是决策树上那个节点」⇒ 凡它驳不倒的分支就**白送 5 分** | 07 评估 | [`d5_scorer.py::score_d5`](../../evaluation/scorers/d5_scorer.py) · `evaluation/golden/data/triage/triage.jsonl`（11 张 d5 卡）· [07 手册 §7.5.3](../07-人工复核与判读手册.md) · 本文 §M-34 补记 G-2 / G-3 · [方案 17](../设计方案/17-M35-D5评分器树上节点核对方案.md) · 编年史 §增量九十 | **施工已完成（2026-10-24，三拍板点 owner 已裁：P-1 = A 档补树 4 节点 / P-2 = 先补树再改检查 / P-3 = 对照金标在校准回路已存在）**：① **修法②（rubric 文案）**——`在线视频场景rubric.md` 锚点 5 补上机器能力边界（只核四件事，正确性由人工/黄金集承担，判不出落 undetermined）；② **G-2 补树（A 档）**——决策树 6 → **10 节点**，新增 `unreachable_hard`（传输层失败）· `unresolved`（代码能力不足）· `blocked`（地区/DRM/反爬）· `broken`（内容缺失），均入「标记未决/排除」落点；③ **修法①（代码）**——`d5_scorer.py` 新增 `TREE_BRANCHES` 常量（10 节点，不含弃用态 `login_wall_or_blocked` 与 `trailer_*`），`_check_branch_matches_question` 里 `CODE_ONLY_BRANCHES` 从「无条件 `ok`」改为「在树上 → `ok` / 不在树上 → `undetermined`」（**不判错，但不再白送 5 分**）；同步改 3 条打红断言 + 新增「树上缺口」不可判定来源。**验证：`test_d5_scorer.py` 70 passed + 契约/校准 246 passed = 316 全绿**。<br>⚠️ **原根因（保留作记录）**：`d5_scorer` 的四处检查全过 ⇒ 给 5 分；「分支↔判断点」对 `CODE_ONLY_BRANCHES` 直接放行，「证据核」对不声称拦截/付费的分支判「本条不管辖」⇒ 凡驳不倒就满分，而 rubric 锚点 5「决策树每步分支全对」这个断言从没验过。**两形态（11 张）**：A 分支名不在树上（6 张）/ B 在树上落错节点（5 张），含 3 张 1–2 档。<br>⚠️ **仍待办（非本条的活）**：`login_wall_or_blocked`(8)/`trailer_only`(1)/`trailer_suspect`(1) 共 10 条**继续落 undetermined**，未上树，另行立项；**v10 冻结未跑**（owner 在真实数据上执行 `freeze --version 10` 后读数才动）。原「两条修法都改代码」：`d5_scorer` 的四处检查（分支↔判断点 · 已到播放页 · 未跳步 · 结论的证据核）**全过 ⇒ 给 5 分**；其中「分支↔判断点」那一核对 `CODE_ONLY_BRANCHES`（`unreachable_hard`/`trailer_only`/`trailer_suspect`/`site_search_no_results`/`site_search_routed`）**直接放行**，「证据核」对不声称拦截/付费形态的分支**判「本条不管辖」** ⇒ **凡它驳不倒的分支就满分**。而 rubric 锚点 5 的字面要求是「**决策树每步分支全对**」——**这个断言它从来没验过**。已实测（2026-10-24 逐处复算）的两种形态：① **形态 A：存档分支名压根不在树上**（6 张）——`T024`（`trailer_suspect`，正确落点 `player_unverified`）、`T001/106df089`（**正片页**落 `trailer_only`）、`T012`（落 `unresolved`，该落 `subscription_wall`）、`T033`/`T025`（404 落 `login_wall_or_blocked`）、`T031`（地区限制落 `login_wall_or_blocked`）；② **形态 B：分支名在树上但落错节点**（5 张）——`E006` 网易号文章落**成功**（`null`）、该落 `not_play_site`；`E005`/`T001-921485c8` 爱奇艺频道页落 `component_unverified`、该落 `no_play_control`；`T028` 会员页落 `component_unverified`、该落 `subscription_wall`；`TPIN02` 西瓜「打开App观看」落 `no_play_control`、该落 `subscription_wall`。**两形态的机器放行路径不同**：形态 A 里「分支↔判断点」那一核**压根没在核**——`trailer_only`/`trailer_suspect` 属 `CODE_ONLY_BRANCHES` ⇒ **直接放行**，`unresolved` 是 fail-closed 通用出口（被加进**每个** question 的允许集）⇒ 放行，`login_wall_or_blocked` 是 `site.is_reachable` 的允许分支 ⇒ 放行，且证据核对 404 / 地区限制正文判「与合并态相容」⇒ **ok**；形态 B 里那一核**核错了问题**（只问「相容吗」、从不问「该落哪个节点」），且证据核对这 5 张**全部 skipped**（`component_unverified`/`no_play_control`/`null` 都不声称拦截/付费形态）。⚠️ **11 张卡里有 3 张是 1–2 档**（`E006` / `T001-106df089` = **1 档**，`T012` = **2 档**；其余 8 张 = 3 档）⇒ **不是「措辞问题」**。⇒ **两条修法**：① 评分器**增一处检查**（把「分支↔判断点」从「放行」改成「比对本行事实对应的树上节点」）；② **改 rubric 锚点 5 的措辞**，承认它只核自洽。⚠️ **两条路都会改 d5 一致率的基线** ⇒ 与「下一对可比是 **v9 → v10**」**一起排**；⚠️ **与 G-2（树只有 6 个节点、62/91 落无节点分支）同族**——**先补树还是先补检查，两件事的次序要 owner 定** |
| **M-36** | ★**（2026-10-26 新增）「人工能进、agent 被拦」：`blocked` 把采集失败写成了站点结论** ⇒ **R1/R2/R3 已闭环，R4 未开工 | 03 标签 / 08 存档 / 07 评估 | [设计方案 20](../设计方案/20-反爬拦截与金标观测面.md)（§2 实测 · §3.5 四条负样本判据 · §5 路线 · §6 拍板）· [`branches.py`](../../executor/branches.py) `COLLECTION_SIDE_BRANCHES` · [`steps/visit.py`](../../executor/steps/visit.py) `_record_challenge` · [`review_queue.py`](../../executor/review_queue.py) `confirm_collection_blocked` · [`research/probes/cf_access_boundary.py`](../../research/probes/cf_access_boundary.py)（R0 取证） | **实测**：94 份存档里 20 次 CF 挑战页访问 → `blocked` 10 / `login_wall_or_blocked` 4 / **`no_play_control` 4（假负样本）** / `unresolved` 1；池 `negative.jsonl` 260 行里 12 行落在这些主机上。**R0 现场取证（同 camoufox 指纹、同一天、10 目标 × 2 轮）**：**12 可进入 / 8 仍被拦** ⇒ 单次拦截对「这站能不能看」**零信息**，所以它既不能进负样本池、也不能当站点结论。**owner 已裁（§6）**：O-a `blocked` 出池 · O-b 进复核队列 · O-c 暂不上 stealth/代理 · O-d 金标加 `access_surface`/`observed_at` · O-e 只人工能进的站留下并标 `access_difficulty=4`。<br>**✅ R1 已闭环（2026-10-26）**：① `branches.COLLECTION_SIDE_BRANCHES = {"blocked"}`，`SiteOutcome.is_negative_sample` 两段排除（**不并进** `NON_SAMPLE_BRANCHES`——那张表是「无题的兜底分支」，`test_branches` 钉死不能与 `questions.negative_branch` 相交）；② `steps/visit.py::_record_challenge` 在**任何 `Perceptor` 之前**拦挑战页（①.5 进站 / ⑦.0 播放页两处），断了「CF 页 ⇒ `no_play_control`」这条产出路径；③ `REVIEW_BRANCHES += blocked` + 新裁定 `confirm_collection_blocked`（写 `collection_status`，不动 `branch`），`confirm_blocked` 的 `is_negative_sample` 恒 False；④ 连带：`builder.NEGATIVE_SAMPLE_BRANCHES` 去掉 `blocked`、`integrity` 并池核对同源、`cli` 域名报表加「拦」列且只被拦过的域名不进黑名单、`relabel.check_target` 拒绝 `blocked`。（测试：`test_branches::Test采集侧账` · `test_visit::Test挑战页先于语义判断` · `test_visit::TestDomainReach拦截列` · `test_review_queue` 三条 · `test_relabel::test_采集侧分支被拒` · `test_builder` 同步断言。）**盘面：负样本池 260 → 227**（`negative.jsonl` 实测 `blocked` 33 行；历史存档与历史池行不回填）。<br>**✅ R2 已闭环（2026-10-26 同批）**：`evaluation/access_surface.py` 持有词表（`human-browser` / `camoufox-plain` / `obscura-plain`，**不叫 default**——本仓库固定配置里 humanize 开、locale 钉、geoip 关，叫 default 会把三处反例藏进名字里）；`annotate` 整行模式**必填** `access_surface` + `observed_at`（缺栏/认不出/非 ISO 一律退 2，**不按默认面补**）；冻结侧 `golden.access_surface` + `manifest.access_surface`（`by_surface`/`declared`/`unrecorded`）+ 单独喊「N 行面未声明」；`same_surface()` 任一侧未声明时返回 `None` **不是 `False`**（`False` 是「两面不同」的结论，会把「没记」读成「跨面分歧」）。**现状 294 行金标全部 `unrecorded`**（旧行不回填，下一版新写的才带面）。（测试：`test_golden_access_surface.py` 14 条。）<br>**✅ R3 已闭环（2026-10-26 同批）**：① **预测侧的面**有了——`builder._scan_archives` / `_scan_negative_pool` 从存档 `driver` 折算行顶层 `access_surface`（负样本池行自本批起写入时带 `driver`，历史行不回填），快照每条 entry 记 `gold_access_surface` / `pred_access_surface` / `surface_comparable` 三栏（`SNAPSHOT_SCHEMA_VERSION` **升 2**，旧快照跨 schema 被 `verify._check_comparable` 拒）；② **采集侧账**——`cards_and_accounts_from_snapshot` 把「跨面 **且** pred ∈ `COLLECTION_SIDE_BRANCHES`」的分流进 `collection_accounts.jsonl`（`loop=collection` / 桶 `evidence_missing` / `owner=collector`，按 `golden_id` 聚合），`agreement` 报表**单列拦截笔数 + 分桶面读数**，且**排在 D10 头条与下钻口径 D 之前**（02 避坑 §3.7）；**`None`（任一侧没记面）不是 `False`**，照旧出卡；③ **d5 检查 5**——`blocked` 加前置事实「拦截发生在站点侧」，CDN 中间页模板（`edge_challenge_evidence`，只收固定模板、**不收**「安全验证」这类泛化词）或没留正文 ⇒ `undetermined`（不出分）；④ **账本补两栏**——`gatekeeper_events` 的 `blocked_surface` / `attempt`，只盖 `gatekeeper.BLOCK_VERDICTS` 四条（`dead_loop` 是行为失败，不算被拦），`attempt` 按 verdict 各数各的。<br>**现状读数**：`agreement --version 13` 实跑 ⇒ **拦截笔数 0 / 不可判定 289**（旧冻结档两侧都没记面，与 R2 的 294 行面未声明一致——**账要等下批真跑 + 新标注带面才有内容**）；d5 检查 5 在 522 条 P2 样本上：27 条 `blocked` 里 **11 条 ok**（腾讯「当前设备不支持播放 / 70017001.1」站点自述地区）、**16 条 undetermined**（CF 中间页 / 没留正文）；整体分布 `5 分 389→378` / `不可判定 125→136` ⇒ **11 条从 5 分变成不出分**（另 5 条本来就不可判定，只是理由换了）。（测试：`test_calibration_collection_accounts.py` 22 条 · `test_d5_scorer::Test采集侧拦截` 8 条 · `test_visit` 账本断言同步。）⚠️ **`elapsed_ms` 没加**（§5.3 列过三列，只落两个）：`agent_loop._record` 刻意不带时间源，硬凑一列会把「谁的时间」埋进数据里。<br>· **还没做**：**R4** 任务池 `access_difficulty` 轴。⚠️ **与 Q-17 / M-28 同族但不同题**：`unreachable_hard`（池内 52 行）**仍在池里**——它确实也是采集侧事实，但出池与否 owner 未裁，**别顺手一起改** |

| **M-37** | ★**（2026-10-26 新增）agent 路径贴的分支标签**绕过了前置事实核** ⇒ 4 条已进负样本池** | 02 感知 / 03 标签 | 本文 **§M-37 详解** · `executor/steps/agent_visit.py:134`（裸 `ledger.record`，**无 `decision`** ⇒ 无题） · `executor/branches.py:370`（`record_from` 里**唯一**可执行的前置事实核） · `perception/rule_perceptor.py::has_strong_paywall_signal`（docstring 明写「**不用于贴分支**」） | agent 路径用**强弱信号**（拦截口径）贴了 `subscription_wall` / `login_required` 标签——**两套判据不许互相顶替**（[01 架构总览](01-架构总览.md)）：强弱信号管「这页**像**付费墙吗」，门禁语境管「这页**是不是**付费墙」。**没有成文裁定说 agent 路径免检** ⇒ 这是缺口不是设计。**待人拍板**（§Q-20）——三个修法见详解，**推荐甲**（记账前跑一次前置核，失败降级 `unresolved`），但**必须先补词表**（iqiyi 那条人判过是真的）。⭐ **2026-10-27 补两条**：① **缺口定位到行**——`agent_loop.py:1347`（`reason = f"{verdict.value} 二次拦截"`）→ `agent_loop.py:455`（`_branch_of(verdict)` 查表得分支名）→ **`agent_visit.py:134` 裸 `ledger.record(url, branch, reason, site_url=url)`（没有 `decision=` ⇒ 不走 `branches.py:370` 的 `_precondition_failed`）** ⇒ **拦截口径**（`gatekeeper.is_paywall` → `has_strong_paywall_signal` 的强弱信号表）被**直接当成分支标签**贴出去 ⇒ `BRANCH_PRECONDITION` 里 `subscription_wall` 的门禁语境前置**一次都没跑过**。② **停点第 ⑥ 步那 4 条已现场重采（camofox）**，用仓库自己的 `paywall_evidence` 判据对读：**bilibili `BV1ibptz2Enz` 站得住**（现场「请先登录或注册」「试看30秒」⇒ 真登录墙）；**v.qq `q3242ihse1h` 站不住**（`paywall_evidence` **空串**：只有「新客VIP专享特惠」这类营销 + 广告位「VIP可关闭该广告」，**无门禁句**；播放器只有 00:00 / 02:09，是短片不是正片）；**youku `XMTQwNzMzMTc1Mg` 最重**——现场是「**由于安全原因，请 登录 后继续观看**」⇒ **登录墙**，与池里的 `subscription_wall` 和已复核档的 `partial_free` **两处打架**（M-41 那一族）；**iqiyi `v_19rrk40at8` 站得住**（⭐ **2026-10-27 复核更正**：存档 `T054__1c9b6260__site-5` 的正文**当场就有**「试看 05:44 后结束，观看完整视频请 开通VIP会员」+「99分钟」正片 ⇒ **原读数说的就是同一个 URL**（不是另一个 URL），**标签当时是对的**；今天重采只取到营销是**页面状态漂移**，**不能推翻当场的读数**。⚠️ **顺带查出判据缺口**：仓库自己的 `paywall_evidence` 对**那段当场正文**也返回**空串**——`_PAYWALL_UNIT_SPLIT` 按**换行**切句，「观看完整视频请」与「开通VIP会员」落进**两个句单元**，而「观看完整」是要求**同句**的语境词 ⇒ **该修的是判据**（§Q-20 ②），不是这一行）。⇒ **4 条里 2 站得住（bilibili、iqiyi）/ 2 站不住（v.qq、youku）** ⇒ ~~定性是 owner 的，代码一行不改~~ ⭐ **2026-10-27 owner 已裁并已执行**：**v.qq 留池打标改用 `trailer_only`** ＋ **youku 改判 `login_required`**（走 `cli relabel --apply`，改判 4 条记录；池 325 行**数量不变**，分支分布 `subscription_wall` 46→42 · `login_required` 9→12 · `trailer_only` 1→2；evidence 带「[改判 …]」且 `source=human`）⇒ **代码仍一行不改**（判据没动）⇒ ⚠️ **两条口径开口留给 owner**（`trailer_only` 的宽口径 ＋ W2 放宽与 youku 定性打架）⇒ 见 **§Q-20 详解「附带开口 A / B」**。⭐ **2026-10-27 补第三条 · 爆炸半径**：盘上池里 evidence 带「**二次拦截**」的**共 30 行**（改判后的构成：`subscription_wall`·`rule` **20** ＋ `login_required`·`rule` **6** ＋ `login_required`·`human` **3** ＋ `trailer_only`·`human` **1**；⇒ **仍由机器贴的 26 行 = 池的 8.0%**；任务分布 E022 2 / T004 7 / T038 1 / T048 3 / T054 2 / T057 7 / E028 6 / E014 2）⇒ **全部走同一个绕过口**（`agent_visit.py:134` 裸 `record`）⇒ 修法甲（把前置事实核接回 agent 路）**一次就动这 30 行**；已现场核过的 3 条 `subscription_wall` 里 **2 条站不住、1 条站得住**（iqiyi，判据缺口所致）⇒ **接核之前必须先修判据**（§Q-20 ②） |
| **M-38** | ★**（2026-10-26 新增）池的去重键 `(task_id, url, branch)` 让**后跑的 agent 行被老脚本行盖住** ⇒ 那两行的 `driver` 停在 `None`** | 03 标签 / 08 存档 | 本文 **§停点 · 进度**（迁移时实测：9 条 agent 池行只并进去 **7** 条） · `executor/archive.py:316`（去重键） · `executor/archive.py:338`（`_negative_row` 落 `driver`） | agent 链那 2 条 `not_play_site`（hao123）**与脚本链老行同键** ⇒ 池里留的是老行（`driver=None`），**agent 面信息丢失**。R3 说「池行要能回答『这条负样本是在哪张采集面上得出来的』」——这条对那 2 行**不成立**。**差的是**：去重键要不要带 `driver`／`run_id`（**改了会影响存量 260+7 行的键空间**）——**属口径题，要人拍板**。⚠️ **2026-10-27 补一个更要紧的面向**（第 4 轮 camofox 批实测，池 299 行）：去重键**带 `branch`** ⇒ **同 task、同 URL、不同分支 ⇒ 两条都留在池里**（不是「盖住」，是「**并存**」）⇒ **同一个 URL 在同一个 task 里带着两个标签进训练集**。全池扫出 **18 个 `(task_id,url)` 键有多于一行**（15 条是存量行、**3 条涉及本批**），逐对核**等价类**（`alignment.is_equivalent`）：① `T004`+`v.qq.com/x/page/a3069hcjg17.html`：**`no_play_control`(camoufox 面) vs `subscription_wall`(camofox 面) ⇒ 不等价**；② `T004`+`vo.youku.com/v_show/id_XNDgzODkwODE5Ng==.html`：**`component_unverified`(camoufox 面) vs `login_required`(camofox 面) ⇒ 不等价**；③ `T038`+`v.youku.com/v_show/id_XMTQwNzMzMTc1Mg==.html`：**`partial_free`(人判) vs `subscription_wall`(camofox 面) ⇒ 同等价类**（不是矛盾，但同一 URL 仍挂两个标签）。⇒ **①②是真矛盾**：**两张面对同一个 URL 给了互斥的结论**，而池把两条都收了 ⇒ 训练侧吃到**自相矛盾的标签**。⚠️ **禁止投票/集成** ⇒ 这两条**不替人合并、也不投票**；⇒ **「去重键要不要带 `driver`」这道口径题从「丢元数据」升级成「丢矛盾」**，**更要人拍板**（改键 = 动存量 299 行的键空间） |
| **M-39** | ★**（2026-10-26 新增）闸门口径与工具面口径**没有分代轴** ⇒ 读数跳变无处可记 | 05 rationale / 09 纪律 | 本文 **§M-39 详解** · `assembler/schema.py::rationale_era_of`（**只有** `rationale_era` 一条时代轴） · `trajectory_pipeline/toolcatalog/__init__.py`（`exercised` 逐面） · 编年史 §增量九十二 | 本轮两道改动都**改了口径**（排版归一 ⇒ 2 条样本 `failed`→`passed`；`fill`/`press` 转正 ⇒ 声明面变了），而 P2 / P3 / T 三层产物**只有 `rationale_era` 一条轴**。CLAUDE.md 明写「批次边界前后分布会跳变，**不记批次元数据就会读成「质量变好了」**」⇒ **要么给产物加一条口径轴，要么在批次元数据里写明**。盘上现状：**已按新口径重建过**（2026-10-26 11:26–11:29），但**没有任何字段记着「这批是排版归一后的口径」** |
| **M-40** | ★**（2026-10-26 新增）人工复核改判出的真负样本**从来没并过池** ⇒ 训练集里压根没有这批负样本 | 03 标签 / 08 存档 | 本文 **§M-40 详解** · `executor/review_queue.py::apply_verdicts`（改判写档，**不并池**） · `executor/pool_add.py`（本轮新写） · `executor/cli.py::cmd_pool_add` | `cli review --apply` 把 `unresolved` 改判成 `partial_free` / `subscription_wall` / `component_unverified` 等**真负样本**并写进复核档，**却从不并池** ⇒ 门禁报 `negative_not_pooled`（实测 **30 条** = 29 条人工补判 + 1 条历史欠账）。而**训练侧吃的是池**（`evaluation/golden.builder._scan_negative_pool`），不是存档 ⇒ 那 29 条负样本**没进训练集**。本轮已补 `executor/pool_add.py` + `cli pool-add` + `review --apply` 接线：**已并 25 条**（24 条人工裁定 + 1 条代码旧账），门禁 `negative_not_pooled` **30 → 4**；剩 4 条**有分歧**（E021 同 URL 有成功记录 / E023·T011·T054 池内留着手工改判的旧行）——**不替人投票**，处置路见 §M-40 详解 |
| **M-41** | ★**（2026-10-27 新增）池里同一 `(task_id, url)` 挂**两个不同的负分支** ⇒ 训练信号自相矛盾，**而门禁看不见** | 03 标签 / 08 存档 | 本文 **§M-41 详解** · `executor/archive.py:316`（去重键含 `branch` ⇒ 不同分支**各存一行**） · `executor/pool_add.py::collect_candidates`（人工那条路**会**拒并 `stale_branch`） · `evaluation/golden/builder.py::_scan_negative_pool` | 池的去重键是 `(task_id, url, branch)` ⇒ **同一个 task 的同一个站，两批给出不同分支 ⇒ 两行并存**。camofox 新批实测：**T004 的 `vo.youku.com` 在老 camoufox 批是 `component_unverified`、新批是 `subscription_wall`**（同 task、同 URL、**两个矛盾标签**）。而门禁 `check-archives` 只查「池行 ↔ 成功存档」的矛盾（`negative_contradicted_by_success`），**不查「池行 ↔ 池行」**⇒ **看不见**。⚠️ **两条写入路径口径不一致**：`P1Archive.write`（自动）**不查**，`cli pool-add`（人工回填）**查并拒并**。**要人拍板**：哪些分支对算「矛盾」（`blocked` vs `login_wall_or_blocked` 是**两层**拦截，**不是**矛盾；`component_unverified` vs `subscription_wall` 是**同一层**的两个互斥结论，**是**矛盾）——判据要落在 `branches.BRANCH_PRECONDITION` 那张表上，**不替人投票** |
| **M-42** | ★**（2026-10-27 新增）池行的 `golden_id` 里嵌的是**行号**不是**身份** ⇒ 池一增删，**人核标签就静默掉线** | 07 评估 / 08 存档 | 本文 **§M-42 详解** · `evaluation/golden/builder.py::_scan_negative_pool`（`__neg{i:02d}`，`i` = 池文件行号） · `evaluation/golden/data/annotations.jsonl`（**6 条** `__negNNN` 标注） | 跑采集侧账时撞出来：池行 id 形如 `{task}__{run}__{hash}__neg151`，那个 `151` 是**它在 `negative.jsonl` 里的行号** ⇒ 池里**任何一次增删**都会让它后面所有行 id 前移/后移 ⇒ **人工标注是按 `golden_id` 挂的** ⇒ 挂不上。**实测 6/6 全掉**：本轮两次 purge 之后，池里这 6 行是 `neg151/152/153/156/157/160`，标注里记的是 `neg152/153/154/157/158/161`（**整体差 1**）。**证据还在**（task/run/URL 都没变，**只有序号变了**）⇒ 不是「标注丢了」，是**键漂了**。⚠️ **现在还没炸**，因为入库的 `candidates.jsonl` 是**旧扫描**留下的（老序号还挂得上）⇒ **下一次重建候选集就炸**：重建后这 6 条被 `site` 过滤剔掉 ⇒ **D10 分母 289 → 283**，而报表上**看不出原因**。⭐ **2026-10-27 本轮把因果链查实了**：把 committed `candidates.jsonl` 的 **10 条 `__neg` id 与现行 builder 逐条对** ⇒ **Δ ≡ −1**（10 条**无一例外**）⇒ 池在构建之后、**池第 152 行之前**被撤过**恰好一行** ⇒ 而那行就是 **2026-10-22 撤掉的 TPIN02 `yn-mg.cn/playid/71884`**（它在池第 5 行附近 ⇒ 之后的行全体 +1）。⇒ **`candidates.jsonl` 自 2026-10-14 首次提交起就没重建过**（那 10 个 id 一直是 `neg152`–`neg161`）。⇒ **builder 的下标口径没变过**（一直是 0 基 `enumerate(rows)`）⇒ **不是代码改了，是池变了**。✅ **本轮撤的 6 行都在池第 300 行之后** ⇒ **没制造新的掉队**（本轮之前就已经掉队 6 条）。⇒ **本轮不重建 `candidates.jsonl`**（重建会把 10 个 id 全改成 −1 ⇒ 那 6 条人核标注被冲掉 ⇒ D10 分母 289 → 283）——**重建的理由远小于代价**。**要人拍板**：后缀改**分支**（`__neg_{branch}`）还是另找一个身份口径？**那 6 条人核标注的键要不要跟着改**（标签内容不动，只改键）——**改键 = 动人核数据 ⇒ 要人签**（§Q-22）。⚠️ **迁移期要双 id 并存**：改口径会让**全部 357 条候选的 id 全变** ⇒ 旧 id 得留一段（或建 `id_alias` 映射表）。**不替人投票**：现在**代码一行不改、标注一行不改**，只把缺陷摆上台面 |
| **M-43** | ★**（2026-10-27 新增）上游把「建页」与「导航」塞进**同一个路由** ⇒ **那个路由失败证明不了站点打不开** ⇒ 一条**假 `unreachable_hard`** 进了池 | 01 采集 / 03 标签 | 本文 **§M-43 详解** · `executor/browser/camofox_driver.py::_open_blank_and_navigate`（**建页与导航拆成两次调用**）· `executor/browser/camofox_session.py::CamofoxToolError.execution_fault` · 上游 `server.js`（`POST /tabs` 的 `withTimeout` **同时**管建页与导航；`if (url)` 才导航） · `research/probes/camofox_attribution_probe.py`（现场取证） | 第 4 轮 `T048` 的 iqiyi 记「**导航失败（网络/DNS/超时）**」，而**失败落在建页路由上**（`camofox_create_tab` 超时）⇒ **那条 URL 压根没被导航过** ⇒ 站点没被试过 ⇒ **采集侧的故障被写成了站点的性质**。三条互相独立的反证：① **老批 `T054__93f110b6` 真打开过同一个 URL**（`reached_play_page=true` · 「存在 `<video>`/`<audio>` 标签 ×2」）；② **同一批 `T038` 进到了 iqiyi 《功夫》正片页**；③ **同一批的 `http://` 变体**落的是「站点遍历超时（>180.0s）」⇒ `unresolved`（**没进池**）。⇒ **修法**：**建页与导航拆成两次调用**（建**空白页** + 再导航）⇒ 归属判据变成**结构性的**（**走哪个路由**），**不是**猜错误文本：**建页**路由失败 ⇒ `unresolved`（**不进池**）／**导航**路由失败 ⇒ `unreachable_hard`（真负样本）。⚠️ **顺带补上一个潜伏缺陷**：上游 `recovery="retry"` 那一族（`session_expired`/`concurrency_timeout`/`browser_launch_timeout`）是**会话**坏了 ⇒ 缺陷④那版的解析**不认识** ⇒ 会记成 `unreachable_hard`（已补 `execution_fault`；**实测这一族还没触发过**）。✅ **存量那一条已撤（2026-10-27）**：池 `negative.jsonl` 第 **300** 行（run_id `a933bea6`）**已出池**——先 `cli downgrade`（存档侧改 `unresolved`，原判留在 `branch_before_downgrade` + evidence 的 `[原判 unreachable_hard] ` 前缀后面），再 `cli pool-drop`（`pool_dropped.jsonl` 记原行全文）；**顺序没反**。⇒ 依据是**单独重开同一 URL 打得开**（爱奇艺《功夫》页，506 ch）⇒ 那条 URL **从没被导航过**（失败在**建页路由**）⇒ 站点性质未被测到 ⇒ 行不成立。⚠️ **重跑不回填历史存档**（**并池是写入时的动作**）⇒ 这条**只能这样撤**，不能靠重跑 |
| **M-44** | ★**（2026-10-27 新增）「导航失败」这一支太粗 ⇒ **采集侧的故障被写成站点的性质**（`unreachable_hard` 的分支名是「站点不可达」，但它收了三种**不是站点不可达**的失败） | 01 采集 / 03 标签 | 本文 **§M-44 详解** · `executor/browser/camofox_driver.py::_navigate`（错误原文进 evidence） · `executor/branches.py::BRANCH_PRECONDITION` · `executor/steps/visit.py`（失败 ⇒ 记哪支） · 上游 `server.js`（`page.goto` 的错误原样透传） | 第 5 轮**逐条单独复核**时把三种失败形态摊开了：① **建页路由失败**（M-43：`camofox_create_tab` 超时 ⇒ **导航请求从未发出**）；② **`page.goto: Navigation … is interrupted by another navigation to …`**（E028 两条 baidu 链接 ⇒ **重定向确实发生了**，打断消息里直接写出落点 `www.ctwlpt.com/voddetail/38550.html`）；③ **`NS_ERROR_NET_RESET`**（连接被重置，与 ② 交替出现）。⇒ **三种都落同一支 `unreachable_hard`**（分支标签「导航失败（网络/DNS/超时）」）⇒ 池里那条断言是「**这里真的看不了**」，而事实是「**这一次没打开**」⇒ **训练信号被污染**（教「这个站看不了」，其实只是慢 / 被打断 / 建页失败）。⚠️ **M-43 只修了「建页与导航塞进一个路由」，没修「建页失败 ⇒ 记成导航失败」这个映射** ⇒ 同一个洞留在了新代码里。⇒ **修法方向**（**不替人投票，等 owner 裁定**）：把 `unreachable_hard` 拆成「**站点不可达**（DNS 失败 / 连接被拒）」与「**这次没打开**（超时 / 打断 / 建页失败）」，后者降 `unresolved`（**不进池**）——⚠️ **并池是写入时的动作** ⇒ 改判**只影响新行**，存量 54 条要出池另走路（**Q-23**）——⭐ **裁定已落地（Q-17）+ 现场证据已到手（2026-10-28）：判据触发 20 次、20 次全部正确降级到 `unresolved`，`unreachable_hard` 0 条** ⇒ 详见本文 **§M-44 详解**（已闭环段）与[编年史 §七](90-增量编年史.md) |

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
| **L-10** | m.ixigua 金标语义分叉 | 02 / 08 | [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | ⚠️ **2026-10-23 订正**：本条原写「`subscription_wall` vs `component_unverified` 同站不同行」，**那对标签在 `bilibili.com/bangumi/play/ep313572`**（见 M-30）；**m.ixigua 实际的两派是 `broken` 3 / `unresolved` 11**。⚠️ **同类不止 m.ixigua**：2026-10-23 复算 d10 金标，**19/131 个 URL** 带多于一种标签、33 张分歧卡里 **11 张**落在其上 ⇒ 范围与修法见 **M-30**，本条降为它的一个实例。✅ **M-30 已裁定 Q-11 = 按访问** ⇒ **不重标、不必上游复核**；m.ixigua 两派是「采集降级（unresolved，`fallback_used=true`）」vs「INC33 批量修缮后的 broken」⇒ **漂移可解释**（**owner 已确认**，2026-10-23；清单 §1 的依据＝P1 存档读数） |
| **L-11** | 7 类单页落地 URL 在生产采集中若有新样本需逐条核对；`partial_free` nav-bar CTA 真例 4 批 0 命中 | 04 探针 | 编年史 §增量六十「仍未闭环项」 · `test_单页落地URL_4条分支放行` | 防回潮由测试兜底，但没有真例就只是**没被证伪** |
| **L-12** | 四处 `time.monotonic()` 计时**在 Windows 上是粗刻度**（15.625ms） | 09 纪律 | 编年史 §增量六十九「顺带第三件」 · `rule_perceptor.py:853/874` · `llm_perceptor.py:1115/1138` · `orchestrator.py:912/957/1015/1081/1148` · `mcp_client.py:205` | Windows 上 `monotonic` 走 `GetTickCount64`，**分辨率 15.625ms**（`perf_counter` 走 `QueryPerformanceCounter`，1e-07）。已核：**`latency_ms` / `elapsed_ms` 只进账本、不进任何阈值** → 是**遥测精度问题，不是判定错误**。`StepLog` 已换（那个守卫测试时红时绿）；剩下四处**独立一批**再动，不挡 P5 |
| **L-13** | **venv 里几个包的文件被删了**，门禁会因为「装不上」而不是「写错了」变红 | 09 纪律 | 编年史 §增量七十「门禁」 | 2026-10-21 实测：`.venv/Lib/site-packages` 里 `python-dotenv` / `pyyaml` 的 `yaml/__init__.py` / `pyjwt` / `python-multipart` 的**文件**不见了，而 `.dist-info` 还在——**`uv sync` 只看 `.dist-info` 判定「已装」，于是不重装**，症状是 `ModuleNotFoundError: dotenv` 与 `cannot import name 'CLoader' from 'yaml'`。修法：`uv sync --offline --group dev --reinstall-package <名字>`（**整包 `--reinstall` 会被权限门拦下**）。⚠️ **pyyaml 的 `.pyd` 被本机的 python 进程占着**，rename 报「拒绝访问」——绕法是 `PYTHONPATH` 指到 uv 缓存里那个已解包的目录（`archive-v0/<hash>`）。**先看是不是环境问题，再去改代码** |
| **L-14** | `test_executor_mode` 依赖开发者本机 LLM 环境变量 | 09 纪律 | [`test_executor_mode.py:245`](../../tests/unit/test_executor_mode.py#L245) · [`test_executor_mode.py:263`](../../tests/unit/test_executor_mode.py#L263) | 同文件已有 `clean_llm_env`，但该用例未使用。保留本机六个 LLM 环境变量时通过；清空后实测 `46 passed / 1 failed`。异常参数应稳定落到空模式，不应由宿主机是否配置模型决定，详见本文 §L-14 |
| **L-15** | replay 的 `element_basis` 清理与 fail-closed 文案不一致 | 02 感知 / 09 纪律 | [`replay.py:169`](../../perception/replay.py#L169) | 文档与注释声称形状不对时整体返回 `None`、不返回半截账，但 `{"shared":"bad","face":{"nodes":9}}` 实际返回 `{"face":{"nodes":9}}`。需明确选择「整份拒绝」或「允许部分账」，并让实现、测试和注释一致，详见本文 §L-15 |
| **L-16** | venv 里留下一个 `yaml__broken_20261021` 目录待清 | 09 纪律 | 编年史 §增量七十二「顺带修好的环境项（L-13）」 · [依赖说明/camoufox_弃用.md §10.6](../依赖说明/camoufox_弃用.md) | 2026-10-21 修 L-13 时，Windows 不许删被占用的 `.pyd`（`拒绝访问，os error 5`），**但允许改名**——于是把 `yaml/` 改名为 `yaml__broken_20261021` 再 `uv sync` 装回。残留目录**惰性**（名字不是 `yaml`，import 不到），但**该删**——等本机那两个残留的 `cf_launch.py` 进程退出后删掉 |
| **L-17** | **「这次动作有没有导航」事后查不到**——驱动的 `last_action`（`nav`/`settle`）没落进 P1 存档 | 01 采集 / 06 组装 | [`camofox_driver.py::_settle`](../../executor/browser/camofox_driver.py)（⚠️ 2026-10-26 camoufox 实现已删，**同一条 `_settle` 在 camofox 驱动里**） （`last_action["nav"]` / `["settle"]`）· 编年史 §增量八十一「仍未闭环项」 | 实测：修复后的 camoufox 存档里 `last_action` / `settle` / `nav` / `doc_stamp` **一个都不出现**（顶层键与 `steps[]` 都没有）⇒ **只剩 `duration_ms` 一个可见痕迹**（点击 2799ms）。这不是小事：整个「读到一半」的归因链（增量八十）靠的就是「动作 → 导航 → 观察」三者的对应关系，而**存下来的只有动作与观察，中间那一步没了**。⇒ 要么给 `ActionStep` 加一个 `nav`/`settle` 字段（**P1 契约改动**，`docs/contracts/P1-observation.md` 要跟着改），要么在动作层把「有没有换文档」编码进 `params`（⚠️ **不能**塞 `params`——那是工具声明的投影，混入非声明字段就与 `toolcatalog` 对不上）。**本次不在这批验证中途改契约**（在跑的批次吃不到，改了还要重建 P2）。**修法与验收见本文 §L-17** |
| **L-18** | **「命令修好」不等于「历史数据修好」**——`purge-run` 修好之后，**修复之前回滚过的 run 要重新收一遍** | 09 纪律 | 编年史 §增量八十一 §九 · [`cli.py` `cmd_purge_run`](../../executor/cli.py) | 2026-10-22 实测：盘上 `samples/` 里有 **19 条 P2 样本的 `source_archive` 已经不在了**（`E006__3a06f63d` / `T014__200a9529` / `T020__03794f2f` 三个**早先被回滚过**的 run）⇒ 它们的僵尸**一直躺在训练集里等着被上传给 LS**。本次用修好的 `cli purge-run` 收掉了（19 条 P2 + 72 份 P3 视图）⇒ **凡是修好之前回滚过的 run，都要重新收一遍**。⚠️ 同款「修好 ≠ 修好历史」的还有**剩下的 1 条「读数打架」**（`T054` 的 `iqiyi.com/v_19rrk40ajc.html`，媒体计数变少）——**未定性**，先记着。**修法与验收见本文 §L-18** |
| **L-19** | **分诊工作表按「版本」出、人的裁定按「内容」签** ⇒ 换一版重跑会把同一批卡再递给人一遍 | 07 评估 | 本文 §M-29 的禁区一节（`worklist build --version 8` 会把同一批卡再递给人一遍）· [`calibration/worklist.py`](../../evaluation/calibration/worklist.py) `worklist_path` / `_cmd_build` / `build_worksheet` · [`calibration/triage.py`](../../evaluation/calibration/triage.py) `add_rows` / `validate_rows` | **读码实测（2026-10-23）**：① `worklist.jsonl` / `worksheet.jsonl` 落在 `v{N}/` 下（**版本键**），而人的裁定住在**单一** `golden/data/triage/triage.jsonl`、键是 `(golden_id, dimension)`（**内容键**）；② `_cmd_build` 调 `summarize_worklist(worklist)` **不传 `triage_rows`** ⇒ 统计里 33 张卡**全算「待人判」**；③ `build_worksheet` 把 `bucket` / `triage_by` / `triage_evidence` 三栏**写成空**（只给机器建议 `suggested_bucket`）⇒ 同一批卡换个版本号再出一次表，**人会被再递一次、三栏全空**。⚠️ **再填一遍的代价不止白填**：`import_worksheet` 要求三栏全填才收 ⇒ 填完导入时 `add_rows` 用 `_same_triage`（比 `bucket`/`triage_by`/`triage_evidence`）判——**同键同理由 ⇒ 幂等跳过**；**同键但理由措辞不同 ⇒ 报错「已有分诊…默认不覆盖」**；人若改加 `--replace` ⇒ **原来那 33 条人工签名被重打的替换掉**。⇒ **要定的**：分诊工作表按**内容键**出（同一批卡跨版本只判一次、工作表预填既有裁定、覆盖率按内容算），还是按**版本键**出（每版一份表，但人重复劳动 + `--replace` 陷阱）？⚠️ **M-29 冻结 v8 后第一条命令就是 `agreement --version 8 --emit-disagreements`** ⇒ 若之后跑 `worklist build --version 8`，撞的就是这条。**建议**：按内容键——`summarize_worklist` **已经支持** `triage_rows`（覆盖率算得对），缺的只是①`_cmd_build` 把 `triage_rows` 传进去 ②`build_worksheet` 把三栏**预填**既有裁定（预填 ≠ 代人决定：人改判仍留痕） |

| **L-20** | 三条同族的小尾巴：**①** `meta.source_archive` 承诺了却**从没注入**（盘上 78 份 T 视图全为空串）· **②** `cli task-view --p1` 与 `--all` **不对称**（legacy 档 `--p1 <run>.reviewed.json` 必然 skipped）· **③** `.reviewed` 这条规则已有**第 4 份拷贝**（`task_view.py`） | 06 组装 / 08 存档 | `assembler/task_view.py:850-851`（承诺）· `executor/cli.py`（`--p1` 分支无回填无 `.reviewed` 处理）· `executor/archive.py:102`（`REVIEWED_SUFFIX` 真源） · `assembler/schema.py:644` · `assembler/builder.py:383` | ① 本轮把「读哪一份 P1（复核档优先）」定成重要口径，产物里**却没有字段记录读的是哪一份**——**口径没落进产物就等于没有口径**。③ 的正解是把常量下沉到 `trajectory_pipeline/common/`（**受 AST 契约限制**：`assembler` 不能 import `executor`，`common` 是两边都能 import 的低层）。三条都不阻塞主线 |

---

## 校准回路（方案 16）· 未闭环

[方案 16](../设计方案/16-评估器与rubric双向校准回路方案.md) 的 `C-xx` 与上表的 `H/M/L` **同一套判定标准**
（代码可指 + 测试封着且真的会跑 + 有记录），**但编号不混**：`C-` 是回路内部的施工顺序，
`H/M/L` 是全域的阻塞程度。**已闭环的 C-01～C-06 / C-08 / C-09 / C-12 / C-15 / C-17 / C-18 不进台账**
（闭环项留在[方案 16 §9](../设计方案/16-评估器与rubric双向校准回路方案.md#9-任务执行计划)的实施结果块里，
台账里出现已闭环项就说明它开始骗人了）。下表**只收没做完的**。

**现状一句话（2026-10-23，C-07 闭环后）**：**R0 + R0.5 + R1 全通了**——
`triggers` 出 T-1～T-4 读数、`sampling` 出 S0–S3 判读清单（复现靠 SHA-256 定序），
护栏段（R0 + R0.5）已清空，R2 的「改尺子留痕」也通了。
**分诊档已于 2026-10-23 填完（C-07 闭环，当日移出）**：33/33 全部由人签
（`triage_by=reesn`），分布 = **评估器 12（`evaluator_logic` 11 / `evaluator_prompt` 1）·
金标 14（`gold_defect`）· 采集 7（`evidence_missing`）· rubric 0**；
`worklist status` 退 0，机器建议 12 条里人**接受 6 / 改判 6**。

**2026-10-24 更新（v9 那 44 张分诊卡也判完了）**：`worklist build --version 9` 出
**44 张**（33 d10 + **11 d5 软维度卡**）⇒ **44/44 = 100%**（`worklist status --version 9`
**退 0**）。**33 张 d10 沿用 v7 的裁定**（同键同理由 ⇒ 导入时**幂等跳过**，不重复签），
**11 张 d5 新判**：**全落 `evaluator_logic`**（逐张证据见
`golden/data/triage/triage.jsonl` 的 `triage_evidence`）。
**分诊档合计 44 行**：评估器 **22**（`evaluator_logic`）· prompt **1**（`evaluator_prompt`）·
金标 **14**（`gold_defect`）· 采集 **7**（`evidence_missing`）· rubric **0**
（**按回路**：evaluator 23 / gold 14 / collection 7）；
机器建议 12 条里人**接受 6 / 改判 6**。
⚠️ **11 张 d5 的根因只有一个**（不是 11 条各错各的）⇒ 已立 **M-35**：
`d5_scorer` **只核自洽、从不核「分支是不是树上那个节点」** ⇒ 凡它驳不倒的分支
**白送 5 分**。⚠️ 软维度金标那条「**一条没填**」也**已过期**——2026-10-24 填完
**364 格**并**二次复核**（分数 0 处改动、署名改 `reesn`，见 §M-34 补记）。
**这一组读数授权排活顺序**：**金标 14 条（M-30 那一族）> 评估器 12 条（M-26 / M-29 那一族）> 采集 7 条**。
**不授权**的是「rubric 没问题」——`rubric_*` = 0 只是「33 张卡、只有 d10 一个维度上没人判出尺子缺陷」，
d2–d9 九个分数维度**一条卡都没有**（软维度无金标）。`verify --triage --bucket evaluator_logic`
现退 1 并打 `❌ bucket_clear 残留 11`——**闸门第四条判据第一次有真实输入**。
**两处仍然卡在「数据还没到位」**：① 那道 diff 守卫**今天测不成**——
没有可比的两版快照（v8 未冻结），`audit` 如实退 2；② **软维度金标一条没填**
（D-2 / D-4 / D-5 / D-6 全 `ungraded`）——于是条款级读数只覆盖 D-10，
T-2 / T-3 恒测不成，抽样盘里 S2 / S3 两层总体为 0，**实抽 20 条、缺 20**。
⚠️ 第 ② 条最值得先动：它不是「多一组读数」，而是**三分之二的读数是空的**，
且人工判读只看得见 S0 / S1。⇒ **下一件二选一**：**填软维度金标**（L-04 / M-18，
不属 C-xx 但杠杆最大）/ ~~C-10 → C-11~~（**2026-10-23 已闭**）。
**（2026-10-24 更新：第 ② 条选了，而且它的三条通路都建好了**——`goldwork worksheet`
出表 / `annotate --dimensions-only` 只写分数 / `freeze --refresh-gold` 带进新版本，
见 [域文档 §07「软维度金标：写入链已建」](07-评估与黄金集域.md) 与
[方案 16 §9.5](../设计方案/16-评估器与rubric双向校准回路方案.md#软维度金标写入链实施结果2026-10-24分支-main)。
**但读数一格都没变**：v8 调参池 **364 格待填 364** ⇒ 上面那些「全 `ungraded`」的读数
**仍然是现在的事实**。**填表是人做的**（M-34），别把「工具齐了」读成「读数有了」。）
⇒ **C-11 的前置已解除**：judge 逐行 pred 现在落 `golden_id`，pred 侧带评分面
指纹（`code:…|llm:…`），分歧卡可回答「旧 prompt 还是新 prompt 判的」——
**答不出来的形态是 `unrecorded`（不可判定），不是猜**。
**（2026-10-24 补：这条问句后来被拆成三个——「哪一面」「那一面是哪版」「怎么定的」。
代码面 / prompt 面 / 采集侧**各比各的**；存量 v1–v8 的版本**由档外台账 + 重算取证**
补上：**v6/v7 已知不可比 · v8 可比 · v1–v5 不可判定**。）**
⚠️ 另有一处**回路断点**（M-29）：快照 pred 侧取自冻结行、代码评分器不带版本号 ⇒
**改评分器不改变任何一版快照的 pred**，「修好了」在重冻结刷新分数之前读不出来。

| ID | 事项 | 依赖 | 记录锚点（证据） | 现在差什么 |
|---|---|---|---|---|
| （本表已空）| — | — | — | **C-10 / C-11 / C-13 / C-14 已于 2026-10-23 闭环并移出**（三条裁定 D-C4 / D-C6 / D-C7 落地，见下方说明）|

> **C-10 / C-11 / C-13 / C-14（2026-10-23）闭环并举出**：三条裁定（D-C7 强制连带改 prompt · D-C4 合并不需 owner 签 · D-C6 阈值不算 rubric 级变更）同日落地，**R2 与「贯穿」两段清空**，方案 16 §9.5 / §9.6 的实施结果块是完整口径。
> **C-10**：锚点表 + prompt 从表渲染 + 契约测试逐档核 rubric 文档（改档不改表 ⇒ 红）。**C-11**：pred 出处改 `<kind>@<version>`，卡上多三栏版本，`prompt_comparability()` 给**三态**答案（**没记到 ⇒ `None`，不猜**）。**C-13**：等价类**注记**（无签名字段）+ 「合并后不可见细分」清单，与表逐标签对齐。**C-14**：阈值**唯一真源**（新文件 `evaluation/scorers/thresholds.py`）+ 四处使用点全改从表取值。
> ⚠️ **两条尾巴随闭环一起记下**：① **冻结行 v1–v8 没有 `scorer_version`** ⇒ 存量数据的卡一律 `unrecorded` ⇒ **「旧 prompt 还是新 prompt 判的」对存量数据答不出来**（**如实标出，不假装答得出来**；补它要重跑 `overlay-scout`，那会重挂分数并改变读数 ⇒ 本轮没做）；② **D-3 / D-9 的 prompt 变了 ⇒ 指纹 `9b3ec1c46545` → `1046e67994aa`，尚未跑真实 LLM 验证**（本机无凭据）⇒ **下一版冻结前各跑一次真实 A/B**。
>
> ✅ **两条尾巴 2026-10-24 已闭**（C-11 补齐，方案 16 §9.5 实施结果块是完整口径）：① 走**档外**的**版本级出处台账** `golden/data/scorer_provenance.jsonl`（**冻结档与 `candidates.jsonl` 一个字不动**）+ **面感知**可比性（`prompt_comparability` 改名 `pred_comparability`）——**v6/v7 已知不可比 / v8 可比 / v1–v5 不可判定**；且**结论与理由都写进卡里**（`pred_comparability` / `pred_comparability_reason` 两栏）——光有函数**等于没答**（第一版两个函数零调用方，卡上还是答不出来）；② **真实 LLM 三臂 A/B 已跑**（脚本 [`research/probes/prompt_ab_anchor.py`](../../research/probes/prompt_ab_anchor.py)，**旧版提示词靠指纹重建并逐位核验** = `9b3ec1c46545`），读数见 [方案 16 §9.5](../设计方案/16-评估器与rubric双向校准回路方案.md#95-p3--prompt--rubric-对齐) 与域文档 [§07](07-评估与黄金集域.md)。
>
> 📊 **②的读数（2026-10-24）**——三臂 = 旧版提示词 / 新版 / **新版复跑（噪声底）**；候选**取全体**（D-3 观察充分、D-9 真实闸门触发）：
> **D-3 n=247**：判出一致率 **旧↔新 95.95%（10 条分歧）** vs **噪声底 94.33%（14 条分歧）** ⇒ **分歧数低于噪声底 ⇒ 没测出可归因的版本效应**；**D-9 n=8**（**就是全体**）：旧↔新 **87.50%（1 条分歧）**，噪声底 **87.50%（1 条分歧）**，**且是同一条**（`T040__7b37c195__site-3`：旧 `hindsight` / 新 `systematic` / 新版复跑又回到 `hindsight`）⇒ **该分歧整条落在运行噪声内 ⇒ 0 条可归因**。
> ⚠️ **D-9 的 n=8 就是总体** ⇒ 这条只支持「**未观测到效应**」，**不支持「证明了没效应」**（D-C7 的义务是「下一版冻结前跑一次」，**跑过就算履行**，但结论只能是「没测出退化」）。
> ⚠️ **D-3 那 10 条方向不一致**：5 条候选效应行（新版与新版复跑同判、旧版不同判）里 **3 条新版更准**（`E006__2729220b__site-5` 博客文章页、`E057__c39e54c7__site-2` 的 404 错误页——旧版都判成了 `ambiguous`；`E023__4d175424__site-3` 归 `playable` 合 `playable` 的付费子分类定义——**但依据是站点形态推断，观察里没有播放控件**）、**2 条方向不明**（`T030__ae6553b1__site-10` 与 `T048__0680dc4a__site-10` **互判相反**）⇒ **不能说「锚点块让 D-3 更准了」**（**且机制未验证**：锚点块里只有档位释义、**没有类别词表**）。
> ⚠️ **「分数一致率 96.36%」不是「准确率一致率」**——`score_d3` 不与任何金标比对（见 **M-33**）⇒ 该读数只说明两边都**落在契约 5 类内**、且 evidence 具体程度相同。
> ⇒ **A/B 顺带读出一条 D-3 的 rubric 开口**（**同一 URL 被两版提示词判成相反标签**，是**提示词无关**的不稳定）⇒ 已立 **Q-14**（owner 拍口径）+ **M-32**（五类词表无 rubric 出处、机械面拦不住）+ **M-33**（评分理由写「判对」但没比对金标）——**详见本文 §Q-14**。


> **C-07（归属分布）2026-10-23 闭环并举出**：33 张卡由人签完（`triage_by=reesn`），分布 = 评估器 12 · 金标 14 · 采集 7 · rubric 0；`bucket_clear` 因此第一次拿到真实输入。过程与口径见 [方案 16 §9.3 · C-07 实施结果](../设计方案/16-评估器与rubric双向校准回路方案.md#c-07-实施结果2026-10-23分支-main)（含一条踩过的坑：C-06 交付时说的「工具就绪」说早了——表里没有现场，人就判不了桶，补了 `worklist show` 与 `scene` 栏两轮才真能判）。

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

### H-12 · camoufox 空枚举丢失过滤账（⚠️ **同一处缺陷在 camofox 驱动里还在**）

**代码事实**：[`observe()`](../../executor/browser/camofox_driver.py#L1168) 成功调用 `interactive()` 后，用 `if elements` 决定是否读取 [`last_enumeration`](../../executor/browser/camofox_driver.py#L1211)（⚠️ 2026-10-26 camoufox 实现已删，**这处逻辑原样搬到了 camofox 驱动**）。当页面元素被词表、空名称或同名规则全部过滤时，`elements == ()`，但 `last_enumeration` 中仍有 `nodes`、各过滤档位和 `kept=0`，这些账目会被丢弃。

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
| 留 5 条 | 纯 `annotations.jsonl` 断言：INC32/INC33 追溯钉、**人工合计 == 82**（原钉 `manual-review == 77`，2026-10-25 二次金标重核后 66 + 16 `reesn`）、round 294+12、INC37 分布容差、inc33==18 |
| 连带 | 两文件不再 import `openpyxl`（依赖仍留 dev 组，见 `pyproject.toml` 注释）；文档 8 处「1 failed 是 xlsx 环境项」同步改写 |

**验收（已过）**：全量门禁里 **xlsx 相关的 fail / skip 归 0**（当时总条数 3398→3443，因工作区并发编辑而浮动；同期另有并发编辑带来的红灯，与本次清退无关，见 09 §当前状态）；
「两份都不存在」「仅一份存在」「两份存在」三种状态**都走同一条路径**
（没有 xlsx 代码了），语义矛盾从结构上不可能复现。

**代价（写在明处）**：77 条 manual-review 与人工工作单的 1:1 关系、
199 条 pending 的升级完整性、30 条 flagged 的 `INC37_NOTE` 三条封条**已丢失**。
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

## 2026-10-22 新增开口项详解

**六条**：M-26 · M-27 · M-28 · H-18 · L-17 · L-18。

⚠️ **其中 M-26 / M-27 已于 2026-10-23 闭环并移出本台账**（按「闭环三处
同步」规程：台账删行 ⇒ 结论落各域文档 + 编年史）——M-26 见
[02 感知与判定域](02-感知与判定域.md)，M-27 见
[03 标签与分支域](03-标签与分支域.md)，读数与判定依据见
[编年史 增量八十三](90-增量编年史.md)。⇒ 下文只剩**四条**。

这六条是**第二批 camoufox 真实批次逐条读 evidence 读出来的**——不是质量审查项，
也不是别人的债。台账表里那几行只到「现在差什么」；这里按
[§2026-10-21 质量审查项详解](#2026-10-21-质量审查项详解) 的同一格式补上
**代码事实 / 影响 / 修复建议 / 验收条件**——**下一位动手的人不必重新推导一遍**。

⚠️ **共同背景**：这批的 7 条判定是**人工降级**救回来的（5 条优酷假成功 +
1 条 Cloudflare 旧层结论 + 1 条点击失败），**当时代码层一处没修** ⇒
**下一批还会犯同样的错**。⇒ 这六条不是「文档债」，是**会持续产出假样本的活 bug**。

★ **2026-10-23 补记**：**M-26 / M-27 两条代码层缺陷已修**（媒体计数不再许诺成功 /
点击失败记 `unresolved`）⇒ 剩下的**四条仍是活 bug**。

### M-28 · `unreachable_hard` 是采集失败，却进了负样本池

**代码事实**：传输层 `Page.goto` 失败（实测 `baidu.com/link?url=…` 跳转挂掉）
⇒ 记 `unreachable_hard`。它**在 22 类分支封闭集里**，且**不在**
[`NON_SAMPLE_BRANCHES`](../../executor/branches.py#L98) ⇒ 照进负样本池。
**22 类里没有「采集失败」这一类**。

**影响**：教模型「这种站不可看」——但**不可看是站点事实，我们这次是没采到**。
两者混在同一个桶里，D-2 打分与黄金集对齐都会读成前者。本批 **13 条**。

**修复建议**：**先别动**（口径是 owner 题）。三个候选：
① 「采集失败」单列一类（要动 22 类封闭集 + `all_branches()` + rubric 对齐账，
代价大）；② 并进 `unresolved`（不进池，但**丢掉**了「这个 URL 确实打不开」
这个信息）；③ **保留分支，但在池行上标 `collection_failed`**，训练侧排除。
⇒ **建议 ③**：信息不丢、训练侧可过滤、封闭集不动。

**验收条件**：owner 拍板后写一条单测锁住「采集失败不进训练样本」；
若取 ③，还要一条契约测试锁住池行新字段（**池行是训练侧吃的口径**，
改它要同步 P2 契约）。

### H-18 · 门禁 `success_branch_mismatch` 是假阳性

**代码事实**：[`integrity.py:233`](../../executor/integrity.py#L233) 的
`_match_outcome()` 按 URL 找 outcome，**返回第一条匹配的**。但**站点阶段与
播放阶段各记一条 outcome**，URL 常常相同 ⇒ 门禁拿到的可能是站点阶段那条
（`unresolved`），而 `visits[i].success` 说的是播放阶段 ⇒
`succeeded != (outcome.branch is None)` ⇒ 报 `success_branch_mismatch`
（取在 [`integrity.py:345`](../../executor/integrity.py#L345)，
报在 `:356`）。

**影响**：门禁是查「自相矛盾」的工具，它**自己挑错一条再报矛盾** ⇒ 假阳性；
而且这类会让人去改**本来正确**的数据。实测 2 份（`T001__67a05faa` /
`T054__93f110b6`）——**T001 是存量 obscura 存档 ⇒ 既存缺陷**，新批次只是
让它显形。

**修复建议**：同 URL 多条 outcome 时按**阶段**挑那条对应的，而不是第一条。
outcome 带 `site_url` / `play_page_url` ⇒ 站点阶段那条 `play_page_url` 为空、
播放阶段那条 `site_url` 为空 ⇒ **优先取 `play_page_url == visit.play_page_url`
的那条**，取不到再退到 `site_url == visit.url` 的那条。

**验收条件**：① T001 / T054 两份存量存档重跑 ⇒ `success_branch_mismatch`
**归 0**；② 补构造用例（同一 URL 两条 outcome，一站一播）⇒ 断言取到
**播放阶段**那条；③ **不得**为了让门禁变绿去改这两份存档的数据。

### L-17 · 「这次动作有没有导航」事后查不到

**代码事实**：`CamofoxDriver.last_action` 记 `op`/`role`/`name`/`outcome`
（[`camofox_driver.py:694`](../../executor/browser/camofox_driver.py#L694) 起），
并在 `_settle` 里写 `nav` 与 `settle`
（[`:823-832`](../../executor/browser/camofox_driver.py#L823)），
但 `RunRecord.to_json()` **不落它**。实测修复后的 camoufox 存档里
`last_action` / `settle` / `nav` / `doc_stamp` **一个都不出现**（顶层键与
`steps[]` 都没有）⇒ 只剩 `duration_ms` 一个可见痕迹（点击 2799ms）。

**影响**：增量八十那套「读到一半」的归因链（**动作 → 导航 → 观察**）
**存下来的只有动作与观察，中间那一步没了** ⇒ 事后分不出「动作没导航」
与「动作导航了但没等到」。这是**证据链缺一环**，不是观感问题。

**修复建议**：给 `ActionStep` 加 `nav` / `settle` 两个字段（**P1 契约改动**
⇒ [`docs/contracts/P1-observation.md`](../contracts/P1-observation.md)
跟着改 + 契约测试双向）。⚠️ **不能塞进 `params`**——那是工具声明的投影，
混入非声明字段就与 `toolcatalog` 对不上。⚠️ **本批吃不到**：改契约要重建
P2 ⇒ **下一批才有**这两列。

**验收条件**：① `tests/contract/test_p1_contract.py` 加断言
（`steps[].nav` ∈ {`new`,`same`,`unknown`}、`steps[].settle` ∈ {`ok`,`timeout`}）；
② 跑一批真实 camoufox ⇒ 存档里**逐条可查**；③ 老存档（无这两字段）
**不报错**（兼容，不许把存量判成 DEGRADED）。

### L-18 · 「命令修好」不等于「历史数据修好」

**代码事实**：`cli purge-run` 修好之后，**修复之前回滚过的 run** 的僵尸
派生产物还在盘上——本次收掉 **19 条 P2 + 72 份 P3**（三个 run：
`E006__3a06f63d` / `T014__200a9529` / `T020__03794f2f`）。

**影响**：僵尸 P2 **一直躺在训练集里等着被上传给 LS**。

**修复建议**：① 收尾动作：**凡是修好之前回滚过的 run，都要重新收一遍**
（本次已收）；② **加一道常规门禁**：`toolcatalog` 契约测试里已有
「源存档已回滚 ⇒ 孤儿」的告警
（[`test_tool_catalog_contract.py:706`](../../tests/contract/test_tool_catalog_contract.py#L706)
现在只 `warnings.warn`，仍放行）⇒ **升级成失败**，或至少让 `check-archives`
报一条 FATAL ⇒ **「修好之后不会再攒」**。

**验收条件**：故意造一条孤儿 P2 ⇒ 门禁红。
⚠️ 同款「修好 ≠ 修好历史」的还有**剩下的 1 条「读数打架」**（T054 的
`iqiyi.com/v_19rrk40ajc.html`，媒体计数变少）——**未定性**，先记着。

---

## 2026-10-23 新增开口项详解

### M-29 · D-5 的 v7 快照 pred 是修复前的，别拿它当基线

> ✅ **已闭环（2026-10-23）**。下面保留「为什么是问题」与「怎么修的」；
> 施工单是过程件，两单都完成后已删除。

**代码事实**：`output/pipeline/calibration/v7/snapshot.json` 里
`dimension=d5` 的 **208 条**全部 `pred_value = 5`、`pred_provider =
code:row.dimension_scores`、`verdict = ungraded`（金标空）。那是 2026-10
冻结时由 `overlay-scout` 写进 `candidates.jsonl` 的旧分数。

**为什么是问题**：D-5 恒真已在 2026-10-23 修掉（第四条检查【结论的证据核】，
[对齐账 §2.4](../设计方案/02-rubric与代码对齐.md)），现跑 522 条 =
**5 分 397 / 2 分 8 / 不可判定 117**。快照与现行评分器**已经不是同一个东西**。
⇒ `calibration.verify` 的 pred 侧取自 `row.dimension_scores`（冻结值），
若有人把新 pred 与原快照比，读出来是「净改善 0」——**看起来像评分器没变化**。

**为什么三条捷径都不行**（当时逐条排除，省得白跑）：

| 捷径 | 为什么不成立 |
|---|---|
| `agreement --version 7` 重跑 | 读的就是 v7 那批旧分数，结果一模一样；而且**静默覆盖**历史快照（**现在**已加守卫，见下） |
| `freeze --version 8 --from-version 7` | 继承式冻结的设计就是「**行内容不动**」——旧分数一起带过来，v8 的 d5 仍全是 5 |
| 不带 `--from-version` 的 `freeze --version 8` | 重新洗牌切分（`_stratified_split`），回归集/持有集全变，「v7 与 v8 逐行可比」当场没了，C-17 的门禁自证为空 |

⇒ 必须补一个能力：**继承切分、但把 pred 侧按当前代码重算**。

**改法与落点**（代码已进 `trajectory_pipeline/evaluation/golden/builder.py`）：

1. **`overlay-scout` 弃权维度写 `null`、四维键齐全**（没样本才留 `{}`）。
   原来只写成功打分的维度 ⇒「**评分器弃权**」与「**压根没测**」在快照里
   **长得一模一样**（都是条目消失），弃权行无声消失。键齐全 = 有样本，
   值 `null` = 该维不可判定（下游 `dim_agreement(None, p)` 返回 `None` 不计入
   分母，与「0 分」不是一回事）。同时把没样本的行**显式写空**（原来用
   `setdefault`，上一轮的旧分数会继续躺在行里）。
2. **`freeze --refresh-scores`**：继承切分，按 `golden_id` 从
   `candidates.jsonl` 重挂 `dimension_scores` / `scores_detail` / `status`
   三个字段；**`golden` 与 `code` 一个字不动**。**缺 id ⇒ 退 2 并列出全部缺失
   id**，绝不「找不到就留着旧分数」。manifest 落 `score_refresh`
   （`per_dim` 前后分布 + `tally` 三态计数）。
3. **防复发**：继承式冻结不给 `--refresh-scores` 时 **stderr 喊一声**——
   M-29 之所以存在，是因为「带过来的分数过期」这件事**一个字都不报**。
4. **`agreement --emit-disagreements` 遇已存在的 `snapshot.json` 退 2**
   （要重算加 `--force-snapshot`）：那份文件是「上一轮判了什么」的唯一凭据，
   重跑一次就把它重新盖成今天的 `evaluator_version`。
   ⚠️ `calibration.gate` 自己那条重算路径**自带**这个许可——闸门的活儿就是
   每次从冻结档重算，复用盘上那份等于拿旧答案判新版本。

**实跑读数**（2026-10-23，全部与预期读数逐字对上）：

| 检查点 | 读数 |
|---|---|
| `overlay-scout` 真跑 | `scored=257 / missing=90 / skipped=10`；d5 = `{5:218, 2:8, null:31}`；`candidates.jsonl` 的语义 diff **只动了三个评分类字段**（`code.branch` 分布不变） |
| `freeze --version 8 --from-version 7 --refresh-scores` | **294 行**（调参池 131 / 回归集 60 / 持有 103），`derived_from.version=7`，`holdout_runs=0`，`score_refresh.rows_matched=294 / rows_missing=0` |
| v8 快照的 d5 | **211 条 = 5×178 / 2×7 / null×26**（分母 289 条站点级行；另 **78 行无样本** ⇒ 无该维条目） |
| v8 快照的 d10 | **与 v7 逐条一致**（289 条）——pred 侧是冻结行 `code.branch`，这条路上没有任何东西被重算 |
| 分歧卡 | 仍 **33 张**（11 张落冲突 URL） |
| v7 文件 | `git status` 里 `data/v7/` **零改动** |

**独立重算核对**（不经 `candidates.jsonl`，直接从 P1 存档现算 `score_d5`）：
211 条**逐条 0 不一致**，分布与快照同；当场 `evaluation.cli --dimension d5`
的「2 分 8」与快照的 7 条 + 1 条（不在冻结档）**一一对应**
（`E014__f50809fb` / `E021__94a1392f` / `E021__dba5c958` / `E023__4d175424` /
`T024__b54092ee` / `T025__0d6d185e` / `TPIN02__1cf5deca` ×2）。
⇒ **验收条件满足**（快照三档齐 + 与当场评分器逐档对得上）。

**已闭环的一半 · 逐条正文核对**（2026-10-23）：M-29 要求的「7 条 2 分逐条读
正文」**已做完**，逐条记录见 [对齐账 §2.4](../设计方案/02-rubric与代码对齐.md)。
读数：**8 条（7 条在 v7 冻结档，第 8 条无 round=1 标注故不进档）全部落第 ① 档**
——正文里是**环境阻断**（region「70017001」+「当前设备不支持播放」）、
**没有**付费门禁形态 ⇒ **D-5 判 2 分正确**：不是评分器判反，也不是词表不全
⇒ **不改 `d5_scorer`、不补词表**。复算脚本
`research/probes/probe_m29_d5_two.py`（离线，仓库根跑）。
⚠️ 同批暴露的**分支贴法缺陷**（这 8 条正解应是 `blocked` 而非
`subscription_wall`）**不在 M-29 范围**，待另开工单。

### M-30 · 金标在同一个 URL 上不自洽（19/131）

> ✅ **已闭环（2026-10-23）**——19 个 URL 的定性全部定稿，明细与依据见本节
> 「现场重采」一小节；施工单是过程件，两单完成后已删除。

> ✅ **已裁定（2026-10-23）：Q-11 = 按访问（per-visit）、Q-12 = 按行。**
> ⇒ **不重标、不废弃行、不重切**；多头标签与跨档泄漏都**保留**，义务从
> 「修数据」变成「**在读数里标出来**」+「**持有集读数必须带附注**」。
>
> ✅ **机器侧已落地**：`calibration/urlconflict.py` · `agreement` 的
> 「URL 级金标读数」· `--json-out` 的 `url_gold_conflicts` ·
> `--emit-url-conflicts` 清单（**事实块机器刷、两列结论人填、重跑不抹掉**）。
> 读数（已复算、已落盘）：多头 **19/131** 个 URL · 跨档 **25** 个 ·
> 分歧卡 **11/33** 张落其上 · 持有集泄漏 **65/101** 行。
>
> ✅ **人侧已收口（owner 2026-10-23 确认）**：19 个 URL 的定性**全部定稿**。
> **agent 已逐条打开 P1 存档读过正文**
> （`output/pipeline/{task_id}__{run_id}.json` 的 `visits[].site_obs.body_text` 与
> `outcomes[].evidence`），**并对那 10 条待核行对应的 7 个 URL 做了现场重采**
> （camoufox 真的把页面重新打开看，**两轮独立重采**，见下面「现场重采」小节）。
> **定稿读数：18 条「漂移可解释」/ 1 条「不可能共存」（ixigua）/ 0 条「待查」**。
> **证据来源分两档**（依据列各自标了，读者一眼能分）：
> **7 条现场重采**（§6 / §8 / §9 / §10 / §13 / §15 / §19，依据以「**现场重采**」开头）
> · **12 条 P1 存档读数**（依据以「**（依据来源：P1 存档读数 + 仓库判据；本条未做浏览器现场重采）**」开头）。
> 清单重跑**不抹掉**已填结论（回读按 URL 匹配，不按行号）——已实测：重跑后
> **19 条定性 + 19 条依据逐字不变**。
> **剩下**：把「不可能共存」那条（ixigua）**交上游标注流程**（与 L-10 / Q-03 同性质：
> 金标问题不是代码可修）；**M-30 的 URL 级定性到此闭环**。
>
> ⚠️ **读完正文 + 现场重采后的一条硬读数**：多头标签的成因只有三类：
> **①访问间现场不同**（采集降级 vs 正常采集、传输层超时 vs 页面打开、
> 模型选中 vs 没选中）· **②同一页两层事实**（v.qq 5 个 URL 上「70017001 地区限制」与「开通会员/VIP」
> **在同一份正文里同时出现**；bilibili 上「会员 粤语版」与「发生了错误」同页共存）·
> **③同一句话两种判读**（youku 上「续费会员」当付费墙 vs 当导航文案）。
> ⇒ **原 7 条「待查」里那 10 行的真问题不是「页面矛盾」，而是「那条金标自己的 `code.evidence`
> 不支持它的标签」**——全是**人工改标、`note` 全空、`金标 ≠ 代码分支** 的行：
> 证据写「判断点 ① 未选中该站点……无证据不记负样本」却贴了 `subscription_wall` /
> `login_wall_or_blocked`（sohu / v.youku / www.ixigua）；证据写「采集降级（['body_text']），
> 证据不完整，不判」却贴了 `subscription_wall` / `unreachable_hard` / `trailer_only`
> （iqiyi / chusuiwenhua / www.ixigua）；证据写「页面仅显示腾讯视频导航和两组『00:00』…」
> 却贴了 `subscription_wall`（v.qq uiq0rxuywu508qr）；证据写「70017001.1 + 开通会员」
> 却贴了 `trailer_only`（v.qq x/page）。**9 行落在 `TPIN02` 这一个任务上**——像是按
> 「URL 属于哪一类」贴的标，而不是按那次访问的证据。**这才是要人核的东西**。
>
> ⚠️ **现场重采把那 10 行里的 4 行推翻了**（§M-30 现场重采，**两轮独立重采**）——**逐行算账**：
> **6 行现场证实原标签成立、保留不改标**
> （v.youku 的 `login_wall_or_blocked`——现场原话「**由于安全原因，请 登录 后继续观看**」；
> chusuiwenhua 的 `unreachable_hard` ×2——两轮导航均 `NS_ERROR_NET_RESET`；
> iqiyi 的 `subscription_wall` ×2——渲染后影片页在、「黄金会员首月低至12元」「开通会员」
> 「客户端流畅播放」在；v.qq x/page 的 `trailer_only`——它自己那一读的存档正文写着
> 「当前设备不支持播放」＋70017001.1）
> · **4 行现场证伪、建议改标**
> （v.qq cover 的 `subscription_wall` ⇒ 应改 `component_unverified`（真因是 DRM 70013080.14，
> **不是** `blocked`）；ixigua 的 `trailer_only` 与 `login_wall_or_blocked` ⇒ 两行都改
> （语义上落 `subscription_wall`，但机器判据看不见 ⇒ 词表缺口；不认措辞等价则按
> fail-closed 落 `unresolved`）；sohu 的 `subscription_wall` ⇒ 建议改 `unresolved`
> （今天那页是**完整播放页**，会员只是促销块与周边片标））
> ⇒ **10 行里 6 行能原样保留、4 行建议改标**。
> ⚠️ **改标一律未执行**（Q-11 已裁定「按访问」⇒ 不重标；v7 冻结档一个字不许改）
> ⇒ **全部交上游标注流程**。
> ⚠️ **另有被现场证伪但不在本轮 10 行范围内**的同 URL 其他行（v.qq cover 的 5 条
> `no_play_control`——判在「观察时机太早」那一读上；v.qq x/page 的 `T025__0d6d185e`
> ——它自己的 note 就写着 `L4[blocked(0.85) -> blocked]`）⇒ **只把标签存疑记在清单里，
> 本轮不改标**。

#### M-30 现场重采（2026-10-23）

**为什么还要跑这一趟**：上面那些依据全是**存档里的**——存档记的是**当时那一次**的观察，
而观察可能是「上一个文档的残留」，也可能是「**页面还没渲染完就观察了**」。
这两种成因的处置完全相反（一个要改数据，一个要改采集层），
**分不清就不该动金标**（H-15 同一口径）。

**手段**（脚本在 `trajectory_pipeline/research/probes/m30_live_check.py`，
读数落 `trajectory_pipeline/research/probes/m30_live_check.json`（第 1 轮）与
`trajectory_pipeline/research/probes/m30_live_check_run2.json`（第 2 轮）；**联网**）：

```bash
# 必须在仓库根跑
uv run python -m trajectory_pipeline.research.probes.m30_live_check \
    --json trajectory_pipeline/research/probes/m30_live_check.json
uv run python -m trajectory_pipeline.research.probes.m30_live_check \
    --json trajectory_pipeline/research/probes/m30_live_check_run2.json
```

它把那 7 个 URL 用 **camofox（现行生产执行层，`DEFAULT_KIND="camofox"`）** 重新打开一遍，
把**现场读数**与**存档读数**并排摆出来，并对一组与金标标签直接相关的关键词打命中。

⚠️ **两轮是独立重采，不是同一份复制**——两轮一致才算数；不一致的按「**两次访问的差异**」论。
⚠️ 脚本有两处兜底，都是现场踩出来的：**①导航回落**（`wait_until="load"` 被站点打断时
自动退回 `domcontentloaded` 重试一次——sohu 两轮都靠这条才拿到正文；ixigua 第 2 轮第一读
落在重定向途中 ⇒ **0 字**）；**②每站读两次**（「导航一返回就观察」＋「等 `--settle` 秒再观察」，
默认 4.0s）——**只看第一读会把「页面还没渲染完」误当成「页面就那样」**。

**决定性实验（同页、同会话、两读 × 两轮）**：
「**立刻观察**」vs「**等 4 秒再观察**」——**R1 / R2 ＝ 两轮独立重采**：

| URL | 立刻观察（R1 / R2） | 等 4 秒（R1 / R2） | 结论 |
|---|---|---|---|
| `v.qq.com/x/cover/uiq0rxuywu508qr/…` | **81 字 / 4000 字**，title「腾讯视频」/「功夫(普通话版)_…_完整版…腾讯视频」，iframe=1 / 5 | **3683 字 / 4000 字**，iframe=3 / 5 | 存档那 **84 字**是「**观察时机太早**」的半成品，**不是站点漂移**（R2 连第一读都已是完整页 ⇒ 渲染快慢随轮次变） |
| `www.iqiyi.com/v_19rrk40at8.html` | **6 字 / 6 字**（「精彩即将开始」＋爱奇艺**通用标题**） | **471 字 / 475 字**，title「功夫（粤语）-电影-完整版视频在线观看-爱奇艺」 | 「取不到正文」**是观察时机问题，不是站点问题** ⇒ 原「待查」**解除** |
| `www.ixigua.com/6532729724869804557` | **216 字 / 0 字**（R2 落在重定向途中，title 空） | **626 字 / 626 字** | 重定向没落定就观察 ＝ **读到空页**；落定后两轮**完全一致** |
| `tv.sohu.com/v/MjAx…` | **489 字 / 489 字** | **1704 字 / 1709 字**，video=1 | 渲染后是**完整播放页**（正片 01:39:26 + 控件齐全） |
| `v.youku.com/v_show/id_XMTQwNzMzMTc1Mg==` | **1922 字 / 1814 字** | **1922 字 / 1922 字** | 落定后**两轮同形**（含「由于安全原因，请 登录 后继续观看」） |
| `v.qq.com/x/page/s3237561yvx.html` | **100 字 / 86 字**（title「腾讯视频」） | **484 字 / 533 字** | 同一 URL 的**两种形态**：地区限制态（存档那读）vs VIP 短片段（现场两轮） |
| `m.chusuiwenhua.com/bq/2te.html` | **导航失败 `NS_ERROR_NET_RESET`**（两轮；`load` 与 `domcontentloaded` 各试一次） | — | 站点**确实不可达** ⇒ 存档那读是浏览器错误页「网页无法访问」 |

**7 个 URL 的现场结论**（清单里对应节的「定性」「裁定依据」已按这些读数改写）：

| URL | 现场事实（两轮） | 定性 | 仓库判据跑在现场正文上 | 对那 10 行的建议 |
|---|---|---|---|---|
| `v.qq.com/x/cover/uiq0rxuywu508qr/…` | 完整页 3683/4000 字：「该视频因版权方安全保密级别较高，**当前浏览器不支持加密播放**」「错误码 **70013080.14**」「下载PC客户端观看」＋裸 CTA「开通会员」「新客VIP专享特惠！」 | 漂移可解释 | `paywall_evidence` ＝ **空串**（L1 无命中；L2 的「开通会员」所在句单元里没有语境词 ⇒ 裸 CTA）⇒ `subscription_wall` **不成立**；`infer_access_defect` ＝ 空串（**DRM 形态不在词表里**） | 该行 `subscription_wall` **不成立** ⇒ **应改 `component_unverified`**（真因是 DRM，**不是** `blocked`——后者是地区/验证码/反爬） |
| `www.ixigua.com/6532729724869804557` | 落定后 **626 字**（两轮一致）、video=1：「**打开App看完整内容**」「付费」「**打开App观看**」；**无登录提示、无预告控件** | **不可能共存** | `paywall_evidence` ＝ **空串**（CTA「打开app观看」与语境词「完整内容」**被换行拆成两个句单元**）；`infer_access_from_evidence` ＝ 空串 ⇒ `login_wall_or_blocked` **不成立** | 两个标签互斥且都不被现场支持 ⇒ **两行都改**（语义上落 `subscription_wall`；**不认措辞等价就按 fail-closed 落 `unresolved`**） |
| `tv.sohu.com/v/MjAx…` | 落定后 **1704/1709 字**、video=1：进度条「00:00 / **01:39:26**」「倍速」「客户端 1080P」「跳过片头片尾」——**完整播放页**；「开通会员」在**「会员影院 / 新用户首月仅9.9元 / 院线大片 / 观看免广 / 热剧抢先看 / 顶级画质 / 开通会员」促销块**里，另有一批**周边片**标着「会员」 | 漂移可解释 | `paywall_evidence` ＝ **空串**、`has_strong_paywall_signal` ＝ **False**（两轮正文都没有门禁语境词） | 该行 `subscription_wall` **在今天的形态下不成立** ⇒ **建议改 `unresolved`**（现场只证伪了「不是付费墙」，**没真播过 ⇒ 证不出 `success`**） |
| `v.youku.com/v_show/id_XMTQwNzMzMTc1Mg==` | 落定后 **1922 字**（两轮同形）、iframe=1：「**由于安全原因，请 登录 后继续观看**」「登录/注册」「立即开通」「SVIP会员10天后到期」「续费享首月18元」「立即续费」「正片」「VIP」；周边视频区有 2 条「预告」（**是周边视频，不是预告控件**） | 漂移可解释 | `infer_access_from_evidence` ＝ **空串**、`access_evidence(kind='login')` ＝ **空串**——**尽管页面明写登录门**：词表是「请登录」「登录后继续」，而页面里**中文之间夹了空格** ⇒ **归一化缺口，不是金标错** | 该行 `login_wall_or_blocked` **保留**（现场登录门原话在），**把依据换成那句原话** |
| `m.chusuiwenhua.com/bq/2te.html` | 两轮导航均 **`NS_ERROR_NET_RESET`**；存档那两读是浏览器错误页（title「**网页无法访问**」、正文 6 字） | 漂移可解释 | 站点不可达 ⇒ `unreachable_hard` **成立** | 两条 `unreachable_hard` **保留不改标**，依据换成「两轮导航均 `NS_ERROR_NET_RESET`」 |
| `www.iqiyi.com/v_19rrk40at8.html` | 立刻读 **6 字**（「精彩即将开始」）→ 等 4 秒 **471/475 字**：影片页「功夫（粤语）**100分钟**」「详情」「9.1」「分」「2004年」「加追」「下载」「分享」＋会员位「**黄金会员首月低至12元**」「新朋友，开通享精彩内容」「**开通会员**」「黄金关闭此广告」「**客户端流畅播放**」 | 漂移可解释 | `paywall_evidence` ＝ **空串**（CTA「开通会员」独行；语境词「完整版」只在 **title** 里，而它**只看 body**）⇒ **词表缺口** | 两条 `subscription_wall` **保留**（影片页在、会员位在、「客户端流畅播放」在；与 2026-10-12 首轮人判「有资源，但要登录付费才能看」一致）⇒ 原「待查」**解除** |
| `v.qq.com/x/page/s3237561yvx.html` | 立刻读 **100/86 字**（title「腾讯视频」）→ 等 4 秒 **484/533 字**：VIP 页「开通会员」「新客VIP专享特惠！」「好剧热综持续更新，会员畅享精彩内容」；**两轮都没复现出 70017001** | 漂移可解释 | `paywall_evidence` ＝ **空串**；`classify_trailer` 要求控件文本含「预告片/预告/抢先看/片花/片段/试看片段」，而正文里的「预告」是**周边视频区**的 ⇒ **机器判不出 `trailer_only`** | 该行 `trailer_only` **保留**——它自己那一读的存档正文写着「**当前设备不支持播放**」＋**70017001.1** ⇒ 正片确实不可得 |

⚠️ **sohu 的「您可以试看5分钟，点击开通会员」**：只在 2026-10-23 一次**临时**重采里出现过一次，
**两轮正式重采都复现不出来**（两轮正文里「试看」零命中）⇒ **不是稳定形态，不能当判据**。

**判据口径**：付费墙一律走 **`perception/rule_perceptor.paywall_evidence`（唯一入口）**——
现场正文跑它：**7 条全部返回空串**（sohu 曾经命中过一次，但那句复现不出来，不算数）。
`BRANCH_PRECONDITION["subscription_wall"]` 明写「页面上挂着『开通会员』字样**不算**，那是导航栏广告」，
所以**裸 CTA 不足以支撑 `subscription_wall`**——这是这次现场取证推翻的标签的主要来源。
⚠️ **别把 `has_strong_paywall_signal` 当贴分支的判据**：它的 docstring 明写
「**不用于贴分支**」——它答的是「这页**像**付费墙吗」（拦页面 / 同页守卫用），
贴分支答的是「这页**是不是**付费墙」（`paywall_evidence`）。
现场实测它在 v.qq cover / youku / v.qq x/page 上是 `True`，
**拿它反推分支会得出与 `paywall_evidence` 相反的结论**。

⚠️ **「机器判不出来」≠「金标错了」**——现场取证顺带查出 **5 处词表缺口**，
**本轮不改词表**（另开代码改动），但人判时不能拿「机器没判出来」当「金标错」的证据：

| # | 缺口 | 现场证据 |
|---|---|---|
| 1 | `paywall_evidence` **只看 body 不看 title** | iqiyi 的语境词「完整版」只在 title 里（body 的 CTA「开通会员」是独行） |
| 2 | 句单元按**换行**切 ⇒ CTA 与语境词被拆开 | ixigua「打开App看完整内容」（含语境词）与「打开App观看」（含 CTA）**在两行上** |
| 3 | `ACCESS_HINTS` 认不出**中文之间夹空格**的短语 | youku「由于安全原因，请 登录 后继续观看」——表里是「请登录」「登录后继续」 |
| 4 | App 墙文案已改，词表**过期** | 表里 m.ixigua 旧形态是「以下内容需下载西瓜视频查看」，今天改成「打开App看完整内容」 |
| 5 | **DRM 形态不在任何词表里** | v.qq「当前浏览器不支持加密播放」/70013080.14（`ACCESS_DEFECT_KINDS` 只有 region/captcha/anti_bot/not_found） |

**代码事实**：把 `v7/snapshot.json` 的 d10 `gold_value` 按 URL 归并
（URL 取自 `golden/data/candidates.jsonl` 的 `golden_id → url`）：

```python
# 复算脚本（在仓库根跑）
import json, collections, pathlib
snap = json.loads(pathlib.Path(
    "trajectory_pipeline/output/pipeline/calibration/v7/snapshot.json"
).read_text(encoding="utf-8"))
url = {}
for line in pathlib.Path(
        "trajectory_pipeline/evaluation/golden/data/candidates.jsonl"
).read_text(encoding="utf-8").splitlines():
    if line.strip():
        r = json.loads(line); url[r["golden_id"]] = r.get("url") or ""
g = collections.defaultdict(set)
for e in snap["entries"]:
    if e["dimension"] == "d10" and e.get("gold_value"):
        g[url.get(e["golden_id"], "?")].add(e["gold_value"])
print(len(g), sum(len(v) > 1 for v in g.values()))   # 131 19
```

结果：**131 个 URL 带 d10 金标，其中 19 个带了多于一种标签**；
33 张 v7 分歧卡里 **11 张**落在这 19 个上。

**必须先排除的那一半**：同一 URL 两次访问结果不同（一次 `success`、
一次 `unresolved`）是**真实事实**（站点漂移、地区、登录态），
不是标注错误。所以判据不是「标签种类 > 1」，而是**「同一页面不可能
同时成立」**：

| URL | 标签分布 | 性质（**2026-10-23 逐条读过 P1 存档正文后更新**） |
|---|---|---|
| `bilibili.com/bangumi/play/ep313572` | `subscription_wall` 4 / `component_unverified` 10 | **漂移可解释**（agent 建议）——原判「硬矛盾」；**读完正文后不成立**：14 行**同一份正文**（body_len 1531–1539）里「会员 粤语版」与「发生了错误，请稍后再试」**同时出现**，四条 `subscription_wall` 的证据原话就是「…**且**页面显示『发生了错误』」⇒ 同一页面的两层事实，不是互斥 |
| `cmgo.com/…/cmgo655_功夫` | `not_play_site` 10 / `unreachable_hard` 3 | **漂移可解释**（agent 建议）——原判「硬矛盾」；读完正文后确认工单 §2.2 的猜测：3 条 `unreachable_hard` 的证据是**传输层导航失败**（`navigation exceeded 30000ms deadline`），10 条 `not_play_site` 是**页面打开了但只是 App 落地页** |
| `v.qq.com/x/cover/uiq0rxuywu508qr/o001241efrp.html` | 4 种（`no_play_control` 5 / `component_unverified` 2 / `blocked` 1 / `subscription_wall` 1） | **漂移可解释**（agent 建议，**现场重采后**）——**三种观察形态并存**：81–84 字导航壳（=「**观察时机太早**」，现场立刻观察复现）／160 字带「70017001.1」／3686 字完整页。`subscription_wall` 那条人工金标（T011__443c5ec0）**证据不支持自己的标签**（证据写「未出现影片标题、简介或实际播放内容」、note 空）⇒ 现场跑 `paywall_evidence` 也是空串（裸 CTA），**建议改 `component_unverified`**（真因是 DRM「不支持加密播放」错误码 70013080.14） |

**影响**：分歧卡里在这些 URL 上的 11 张，「evaluator 错」与「金标错」
两个方向都能解释——**拿它们判 H-02 的方向（rubric 还是 evaluator 的锅）
会把噪声当证据**。L-10 只记过 m.ixigua 一例，范围明显低估。

**修复建议**：① 先按上表把 19 个 URL 逐个定性（漂移可解释 / 不可能共存 / 待查）；
② 「不可能共存」那几条**仍然要人判出来并写进清单**——Q-11 裁定按访问不等于
免掉定性：**标签是事实，定性是判断**，两者都要留痕（将来若改按 URL 重标，
清单就是入口）；
③ **不在 `triage.jsonl` 加 `same_url_conflict` 字段**——那 33 行已 100% 人工
签名，六桶是封闭集，加字段会让 `triage check` 与「33/33」这个读数同时失真。
改走**派生读数**：`agreement` 的「URL 级金标读数」已经能一眼数出 **11/33**
张卡落在这 19 个 URL 上。

**验收条件**：19 个 URL 每个都有书面定性（清单 `golden/data/url_gold_conflicts.md`
的「定性」「裁定依据」两列**无空**）；分歧卡落在冲突 URL 上的条数在
`agreement` 的读数里能一眼数出（现为 11/33）。

### M-31 · `perception/replay.py` 的「旧判定」列在 23% 的 ④ 回放单元上静默变空

**代码事实**：[`replay.py:230`](../../perception/replay.py#L230) 的
`_outcome_for()` 只拿 **`visit["url"]` 与 `visit["landed_url"]`** 两个键做
**精确串匹配**去找存档里的 outcome。但 outcome 的 `url` 字段是**最终观察到的
URL**（`player_obs.url`），它与那两个键**经常不是同一个串**。

**影响（2026-10-23 实测）**：全库 **135** 条 ④ 回放单元里，**104 条**取到
`recorded_question`，**31 条（23%）**取不到 ⇒ `recorded_question` /
`recorded_branch` / `recorded_source` **三个字段一起变成空**。
**症状是静默的**：回放报告里那一列**看着像「这条没有存档记录」**，
而真相是「**存档里有记录，只是没对上键**」——前者是事实，后者是缺陷。
三种成因（按条数）：

| 成因 | 条数 | 例 |
|---|---|---|
| **路径不同**（落地域名与最终观察页不是一页） | 22 | `v.qq.com/x/cover/fse52rd4klx7qn2.html` → 观察到的却是 `v.qq.com/x/search/?q=` |
| **fragment 不同**（最终 URL 带 `#pycon`，落地域名不带） | 7 | `56dy.com/movie/2842` vs `56dy.com/movie/2842#pycon` |
| **query 串不同**（跳转后多了 `?spm_id_from=…`） | 2 | `tv.cctv.com/…shtml?srcfrom=baidualading…` |

⇒ **它跟 §H-18 是同一族病**（同一个 URL 有多条 outcome 时取**第一条**）：
`_outcome_for` 还不按 `question` 匹配 ⇒ 站点阶段与播放阶段**各记一条**时，
取到的那条**可能属于另一道题**。**两处一起修才对**：键要认最终观察 URL，
命中后还要按 `question` 挑。

**为什么本批不动**：① 修它会改 `cli replay` 的输出语义（那列会从空变成有值），
而 `replay` 是 M-10（rule vs llm 分歧率）与校准回路的读数工具——**改口径要单独
立项**；② M-26 的边界是「不顺手做别的」（见 [02 域 §M-26](02-感知与判定域.md)）。

**修复建议**：`_outcome_for` 改成**按 (URL 规范化, question) 匹配**——
URL 规范化 = 去 fragment、统一 scheme 大小写、排序 query；命中多条时
**按 question 精确取**，取不到就**显式说「取不到」**（不许静默留空，
空列会被读成「没有存档记录」）。

**验收条件**：① 135 条 ④ 回放单元里 `recorded_question` 取到的条数
**= 135**（或对每条取不到的都给出**可解释的原因**，不许静默空）；
② 同一 URL 多条 outcome 时**按 question 配对**（与 H-18 同源）；
③ `cli replay` 的输出变化**逐条核过**（旧版空列的行现在显示什么）。

### Q-13 · ④ 的提示词把媒体计数抬成正面证据 ⇒ 需 owner 重新裁定口径

**这不是「还没做」，是「不能自己决定」**：改 ④ 的提示词 = 改 rubric 口径
（M-26 的边界明写「不许动 `perception/questions.py` 的 PLAYER_OK prompt」——见 [02 域 §M-26](02-感知与判定域.md)）。

**代码事实**：[`questions.py`](../../perception/questions.py) 的 `Q.PLAYER_OK`
提示词第一句就是——
「这是点击播放控件后到达的页面。**结合代码测得的媒体元素数量**，判断它
**是不是一个能正常播放的播放器页**。」

**为什么现在提出来**：M-26 把**代码层**那道「`video_tag_count >= 1` ⇒ 直接答真」
的短路删了（[02 域 §M-26](02-感知与判定域.md)），但**提示词**仍把计数摆在判据的第一位。⇒ 计数**没退出
判断，只是换了个入口**——从「代码替模型下结论」变成「模型照提示词下结论」。

**实测读数**（2026-10-23，网易号美食图文页：6 个 `<video>` 是点广告后注入的）：
连问 **8 次 = 7 次 True / 1 次 False**；其中一次 6 连问 **6/6 True**，
evidence 全是「检测到多个视频媒体元素，且未见付费门禁」。
⇒ **这条假成功没被 M-26 消除**——代码不再越权，但**模型仍被同一个数骗**。

**两条路的代价**：

| 路线 | 做法 | 代价 |
|---|---|---|
| **A · 降级计数的措辞** | 提示词里把媒体元素数量从「判据」改成「**排除性线索**」（有它**不能**证明能播，没有它**也不能**证明不能播） | 改的是**提示词** ⇒ 要重跑一批验证 ④ 的判得出来率；可能让真·播放页的召回掉一截（要实测） |
| **B · 不给计数** | ④ 的 prompt 里**不出现** `media_count` / `iframe_count` | ⚠️ **代价大**：真实视频站**多是 iframe/JS 播放器**（`xdjdxx` / `yn-mg` 那两条 `video=0、iframe=3` 是**真播放页**）⇒ 不给计数，模型只能靠正文与标题判断 ⇒ 召回可能塌 |

⇒ **owner 需拍的是**：④ 的判据里**媒体计数还留不留、留的话算不算正面证据**。
⚠️ 附带一条读数：后端 `temperature=0.0` 仍**不是逐字确定**（同一观察 8 次翻了
1 次面）⇒ 与 §M-08「judge 输出非确定性」同族；**禁止投票/集成**，分歧必须暴露。

### Q-14 · D-3 的 `playable` ↔ `nav_aggregator` 两个定义在这一类页面上同时成立

**这不是「还没做」，是「不能自己决定」**：改 D-3 的类别口径 = 改 rubric 口径，
按 **D-C7**（Q-08 已裁定「rubric 变更强制连带改 prompt」）要走 [方案 16 §5.5](../设计方案/16-评估器与rubric双向校准回路方案.md) 的**条款级流程**。

**读数（2026-10-24，真实 LLM 三臂 A/B）**：脚本 [`research/probes/prompt_ab_anchor.py`](../../research/probes/prompt_ab_anchor.py)，
D-3 候选**全体 247** 条，三臂 = 旧版提示词（指纹 `9b3ec1c46545`，**按 v8 manifest 的 `evaluator_version` 逐位重建核验**）/ 新版（`1046e67994aa`）/ 新版复跑（**噪声底**）。
判出一致率：**旧↔新 95.95%（10 条分歧）** vs **新版↔新版复跑 94.33%（14 条分歧）**
⇒ **旧↔新的分歧数（10）低于噪声底（14）** ⇒ **没测出可归因的版本效应**。

**读出这条开口的两行**（同一 URL、同一任务「功夫」的两份存档）：

| 存档 | 页面证据 | 旧版 | 新版 | 新版复跑 |
|---|---|---|---|---|
| `T030__ae6553b1__site-10` | `video_tag_count=0`、`iframe_count=0`、`body_len=1553`、`links_total=45`、标题「功夫 (2004)高清在线播放-我乐电影」，外链「立即播放 / 在线播放,无需安装播放器 / 量子云 / 迅雷下载 / 手机MP4下载」 | `playable` | `nav_aggregator` | `nav_aggregator` |
| `T048__0680dc4a__site-10` | **逐字段相同**，只差一个轮播计数（今日更新"92"部影片 / "87"部影片） | `nav_aggregator` | `playable` | `playable` |

⇒ **同一版提示词、同一页、两个相反标签**（新版那两行正是 `playable` ↔ `nav_aggregator` 对调）。
两版的理由串显示机制：**模型跟着自己引用的那个短语走**——引「立即播放 / 在线播放,无需安装播放器」⇒ `playable`；
引「量子云 / 迅雷下载」⇒ `nav_aggregator`；**新版那两条理由里都写着 `video_tag_count=0、iframe_count=0`**，
但**没有任何规则**能让「无内嵌播放器 + 有外链播放入口」归到某一类 ⇒ **它在 rubric 里没有条款可依**。

⚠️ **比「界线没定死」更具体的说法**：**类别散文里 `playable` 与 `nav_aggregator` 的定义在这一类页面上同时成立**——
`playable` = 「能直接播放目标视频的站点」（经「量子云」外链**能看**）；
`nav_aggregator` = 「把用户引到外部播放站，**自身不存正片**」（`video=0 iframe=0`、**无内嵌播放器**）
⇒ **散文没有区分「站内播放」与「经外链跳转播放」**。⇒ 这**不是**「两档之间忘了划线」，
是**两档的适用范围本身重叠** ⇒ owner 要拍的是**改哪一档的定义**（或加一条判别式），不是补一条边界说明。

⚠️ **为什么这条不是「锚点块造成的」**：同一 URL 在**两版**提示词下都不稳定（只是翻的方向相反）⇒
**是提示词无关的不稳定**。锚点块既没造成它，也**没修好**它——**A/B 的口径管不了这个**（那是 rubric 变更，不归 A/B）。

⚠️ **而且它没有机械面拦着**：[rubric D-3 卡](../设计方案/在线视频场景rubric.md) 里
`playable` / `nav_aggregator` / `review` / `irrelevant` **零出现**（卡里是「播放站 / 导航站 / 无关站」**三**说法），
词表的唯一定义处是 [`d3_scorer.py`](../../evaluation/scorers/d3_scorer.py) 的 `frozenset` + **手写散文**；
`anchors_drift()` 只核**档位文本** ⇒ **词表可以无声漂移**。这条拆成 **M-32**（词表搬家 + 机械面），
**Q-14 只拍口径**。

⚠️ **顺带把一条读数口径钉死**（⇒ **M-33**）：`score_d3` **不与任何金标比对**，
「category 判对」在代码里 = 「落在契约 5 类」⇒ **A/B 的「分数一致率 96.36%」不能读成「准确率一致」**，
它只说明两边都落在契约内、且 evidence 的具体程度相同。

**两条路的代价**：

| 路线 | 做法 | 代价 |
|---|---|---|
| **A · 词表与判别式进 rubric** | D-3 卡写明五类 + 一条**判别式**（「站内播放」还是「经外链跳转播放」）——**或者改某一档的定义本身**；走 §5.5 条款级（owner 签）+ 按 D-C7 **连带改 prompt** | 改 rubric ⇒ 升版本号 + 记变更记录 + **重跑 A/B**（prompt 指纹会再变）；且**要定线就得有可判的证据**——这一类的现场样本不多（本次只撞到 1 个 URL、2 份存档） |
| **B · 词表留在评分器** | rubric D-3 卡补一句「本维度只管档位，类别口径在评分器」；M-32 只做「词表进 `rubric_version.py` + prompt 从表渲染 + 自洽测试」 | 类别口径**不受 D-C7 管辖** ⇒ 以后改它**没人拦**；且 rubric 与代码两套词表并存，**读 rubric 的人看不出 D-3 到底判什么** |

⇒ **owner 需拍的是**：**D-3 的五类词表要不要进 rubric 文档**，以及**「有外链播放入口、无内嵌播放器」算 `playable` 还是 `nav_aggregator`**。

---

## 2026-10-26 新增开口项详解

> 这一节的四条（M-37 / M-38 / M-39 + 停点）都来自 **2026-10-26 那一轮真实批次**：
> 那是**第一次用 camoufox 跑 agent 链**（`plan-camoufox-prod1-rest.json`），
> 也是**第一次把 agent 链的负样本并进池子**。
> ⇒ 这批读数是**真生产数据**，不是探针。

### M-37 · agent 路径贴的分支标签**绕过了前置事实核**

#### 机制（代码事实，可机械复现）

同一个账本，**两条记账路径，前置事实核只有一条走到**：

| | scripted 路 | **agent 路** |
|---|---|---|
| 入口 | `judge_player` → `ledger.record_from(decision)` | `executor/steps/agent_visit.py:134` 裸 `ledger.record(url, branch, reason, site_url=url)` |
| 带 `decision=` 吗 | 带 ⇒ `question` 有值 | **不带** ⇒ `question=""` |
| 走 `_precondition_failed` 吗 | **走**（`executor/branches.py:370`，全仓**唯一**可执行调用点） | **不走** |

⇒ agent 路径贴负标签时，**前置事实核一次都没跑过**。

#### 谁替它贴的标签：闸门，用的是**拦截口径**

- `executor/agent_loop.py` 的 `GATEKEEPER_DECIDED_BRANCHES = {login_required, subscription_wall, blocked}`
  —— 闸门判定 → 收口映射 → `fail_closed` 处置带 `branch=`；
- 闸门的判据是 `executor/gatekeeper.py::is_paywall` / `is_login_wall`，
  后者落到 `perception/rule_perceptor.py::has_strong_paywall_signal`；
- 而那个函数的 docstring **明写**：「⚠️ **不用于贴分支**（2026-10-21）：
  它答的是「这页**像**付费墙吗」，用来**拦页面**与**放行同页守卫**；
  贴分支要答「这页**是不是**付费墙」，那是 `paywall_evidence` 的门禁语境判据。」

⇒ **两套判据被 agent 路径混着用**：拦截口径判出的结论，
被**直接**当成分支标签贴了出去，**没换算**到门禁语境口径。

#### 盘上实测（4 条已进池）

| 池行 | 分支 | 页面原文（从存档正文里读出来的） | 判读 |
|---|---|---|---|
| E022 / bilibili | `login_required` | 「登录 免费享高清视频 / 试看30秒 / 0:00:13 / 4:13:58 / **请先登录或注册**」 | ✅ **标签对**（真登录墙）——但它是**碰巧**对的：`login_required` 在 `BRANCH_PRECONDITION` 里只有**散文**、**没有代码**（见下） |
| T054 / iqiyi | `subscription_wall` | 「试看 / 05:44 / 后结束，观看完整视频请 / **开通VIP会员**」 | ✅ **标签对**（教科书级门禁句）——但 `paywall_evidence` **判不出来**（见下） |
| T054 / youku | `subscription_wall` | 「首3月每月12元 / 立即开通 / … / 正片 / VIP」 | ⚠️ **标签很可能错**（续费营销 CTA + 播放页，**没有门禁句**） |
| E022 / v.qq | `subscription_wall` | 「**VIP可关闭该广告** / 新客VIP专享特惠！/ 会员畅享精彩内容 / 开通会员」 | ⚠️ **标签很可能错**（**广告 + 会员营销**页，「VIP可关闭该广告」是广告位说明） |

⚠️ **两条「很可能错」的读数是文本级的，不是人判的**——现场重采断网（camoufox 已卸），
**没资格下定性**。但**不重判**它们自己也会进池 ⇒ 见「修法」里的**丙**。
> ⚠️ **2026-10-26 补**：断网的原因是**当时那个执行层（camoufox）已卸**；
> **现行执行层是 camofox**（`DEFAULT_KIND="camofox"`），**重采这条路是通的**
> （`research/probes/m30_live_check.py` 已改走 camofox）⇒ 这两条要定性，
> **先按 [07 手册](07-人工复核与判读手册.md) 的判据⑥ 在 camofox 面上重采一遍**。

#### 为什么现在**不能**直接把闸开（这是本条最要紧的一段）

把 `_precondition_failed` 接到 agent 路径上，**今天就会出 3 条假降级**。
实测在盘上存档的正文上跑 `perception/rule_perceptor.paywall_evidence`：

| 页面 | `has_strong_paywall_signal`（拦截口径） | `paywall_evidence`（贴分支口径） | 后果 |
|---|---|---|---|
| iqiyi（T054 step4，659 字） | **True** | **空串** | **假降级**：真付费墙被降成 `unresolved` |
| youku（T054 step0，1814 字） | True | 空串 | 碰巧对（本来就该降） |
| v.qq（E022 step2，1030 字） | True | 空串 | 碰巧对 |
| sohu（T054 step0，1781 字） | True | **命中**「付费墙 CTA「开通会员」+ 同句观看语境「试看」」 | ⚠️ **反向问题**：这条页**有**「您可以试看5分钟，点击开通会员」，**是真付费墙**，但它被记成 `player.ok=True`（假成功）——**这是另一个洞**（Q-13 / M-26 同族） |

**为什么 iqiyi 判不出来**（可机械复现）：

```
观测正文（DOM 节点按 \n 拼的）：
  … 试看 \n 05:44 \n 后结束，观看完整视频请 \n 开通VIP会员 \n 功夫（粤语） …
```

`paywall_evidence` 的 L2 判据要求 CTA 短语与观看语境词**同句**，
而「句」是按 `[\n\r。！？；;!?]+` 切的 ⇒
「观看完整视频请」与「开通VIP会员」**被 DOM 换行切成了两个单元** ⇒
`_context_outside` 在 CTA 所在单元里找不到「观看完整」/「试看」⇒ **不成立**。

⇒ **「同句」这个要求本身被 DOM 节点边界打败了**：真实页面里
「一句门禁命题」会被拆成 2–4 个短节点。

⚠️ **但放宽有代价，v.qq 就是代价的证据**：v.qq 的语境词「完整版」来自
**视频标题**「三傻大闹宝莱坞完整版」，与导航栏「开通会员」隔了 10 来个单元。
放宽成「同窗（相邻单元）」在 v.qq 上未必误判，但**一旦窗口开大**就会。
⇒ **放宽幅度只能靠实测定，不能靠论证定**（见「修法」的验收判据）。

#### 三个修法

| | 做法 | 代价 | 判读 |
|---|---|---|---|
| **甲** | **记账前先跑一次前置事实核**，不成立降级 `unresolved`（把 `agent_visit.py:134` 那处裸 `record` 改成走一个「先核再记」的入口） | **必须先补判据**——今天直接接上去会假降级 iqiyi；且「同句→多宽」要实测定 | ⭐ **推荐**，但**分两步**：① 先补判据（拆句问题 + 「立即开通」这类 CTA + 标题里的「完整版」要排掉）② 再接核。**在 ① 做完前，agent 路径的负样本应视为「未过核」** |
| **乙** | agent 路径**不贴负标签**，一律记 `unresolved` + 把闸门判定原文写进 evidence ⇒ 全部交人工 | agent 路径**产不出负样本**（采集量掉一半） | 保守但**明确不推荐**：pool 的意义是「这里真的看不了」，而 `unresolved` 不入池 ⇒ 采集链的产出直接腰斩 |
| **丙** | 承认 agent 路径**单开一套**「强信号即可贴」的口径 | 放弃「贴标签前核前置事实」这条纪律；**而强信号在 youku / v.qq 上已经误判**（营销 + 广告页） | ❌ **明确不推荐**：这两条正好是这条纪律要防的那类假负样本 |

#### 验收判据（补判据那一步的**硬指标**，缺一不算修好）

取 4 条 agent 池行的正文 + 现有金标里 `subscription_wall` / `login_required` 的行，
做一次全量重判，**三个数都要对**：

1. **iqiyi 那条仍然成立**（不许假降级）——「同句」放宽后它必须命中；
2. **youku / v.qq 那两条不再成立**（降级 `unresolved`，即**不许进池**）；
3. **金标里已判 `subscription_wall` 的行不掉**（词表改动不许伤旧金标）。

⚠️ 判据 3 是**必做**的：词表一改就是口径变更，
按 D-C7 得**连带改 prompt**、并重跑 A/B（[Q-08](10-TODO台账.md#待-owner-拍板不是还没做是不能自己决定) 的义务）。

#### 与既有设计的关系（不要误伤）

- `tests/contract/test_questionless_producers.py` **已经**把 agent 收口那三支
  登记成「合法的无题产地」，并把 `GATEKEEPER_DECIDED_BRANCHES`
  **钉死**成 `{login_required, subscription_wall, blocked}`。
  ⇒ **那条测试没错**：「无题」是设计（agent 路径没有判断点），
  本条说的是**另一件事**——**无题不等于免检**。
  该测试验的是「产地登记了没有」，**不验「标签贴对没有」**。
- `BRANCH_PRECONDITION` 里 `login_required` / `blocked` / `partial_free`
  三条**只有散文、没有代码**（`branches.py:496-538`）⇒
  这三条分支在**哪条路径上都没有前置事实核**。
  ⇒ 甲方案要连带把「散文条目」升级成「可执行判据」，
  **否则甲只修了 `subscription_wall` 一支**。

### M-38 · 池的去重键 `(task_id, url, branch)` 让**后跑的 agent 行被老脚本行盖住**

**代码事实**：`executor/archive.py:316` 的去重键是 `(task_id, url, branch)`，
**不含 `driver`**。迁移实测：agent 链那批里 **9 条**要并进池，
实际只并进去 **7 条**——2 条 `not_play_site`（hao123）**与脚本链老行同键**，
池里留的是**老行**（`driver` 字段压根没有 ⇒ 读成 `None`）。

**为什么这不是小事**：方案 20 R3 承诺「池行要能回答『这条负样本是在哪张采集面上
得出来的』」——对那 2 行**不成立**。而 R1/R2 的整个立论就是
「**人进得去、agent 被拦**这件事要能在数据里看出来」。

⇒ **要拍的是**：去重键要不要带 `driver`／`run_id`。
**改键会影响存量 260+7 行的键空间**（老行没 `driver` ⇒ 键要能兼容缺字段），
属**口径题**，不是代码题 ⇒ 进 Q-20。

### M-39 · 闸门口径与工具面口径**没有分代轴** ⇒ 读数跳变无处可记

**代码事实**：P1/P2/P3 三层产物里**只有一条**时代轴
——`assembler/schema.py::rationale_era_of`（`pre_action` / `post_hoc` / `""`）。
2026-10-26 这一轮**改了两处口径**，两条轴都**不记**：

| 改了什么 | 口径性质 | 读数影响 | 产物里有没有字段记着 |
|---|---|---|---|
| `rationale/lexicon.py::match_key`（NFKC + 去空白） | **闸门判据**变了 | 2 条样本 `failed` → `passed` | ❌ 没有 |
| `toolcatalog` 的 `fill` / `press` 转正（`exercised=True`） | **工具声明面**变了 | 训练侧 `six.tools` / `openai.tools` 少两个空声明 | ❌ 没有（catalog 里有，但产物里没有 era 标记） |

**为什么这要紧**：CLAUDE.md 写着「批次边界前后分布会跳变，
**不记批次元数据就会读成「质量变好了」**」。
这轮**恰好**踩在这句话上：2 条样本从 `failed` 变 `passed`，
**读数上就是「质量变好了」**——而真相是**判据变了**。

⇒ **要么给产物加一条口径轴，要么在批次元数据里写明**。
盘上现状：**已按新口径重建过**（2026-10-26 11:26–11:29），
但**没有任何字段记着「这批是排版归一后的口径」** ⇒
**下次有人拿这批与上一批比分布，读到的会是「质量变好了」**。

⚠️ 这与「**并池是写入时的动作**」是同一条纪律的两个面：
池行是**写入时的快照**、**不回填**；那产物里的时代标记也应当是
**写入时的快照**——**先有口径、后有数据**，反过来就补不回来了。

### M-40 · 人工复核改判出的真负样本**从来没并过池** ⇒ 训练集里没有它们

**代码事实**：`executor/review_queue.py::apply_verdicts` 会把人工裁定写进
复核档——包括把 `unresolved` 改判成 `partial_free` / `subscription_wall` /
`component_unverified` / `not_play_site` / `broken` 这些**真负样本分支**
（实测复核档里的 `review_verdict` 取值：`confirm_paywall` /
`confirm_partial_free` / `confirm_negative` / `confirm_not_play_site` /
`confirm_broken`）。**但它不并池**——池只有一条写入路：
`executor/archive.py::P1Archive.write`（跑完一条 run 时）。

⇒ **人工改判出的负样本没有池行**。门禁 `check-archives` 报
`negative_not_pooled`（Severity.DEGRADED）——**实测 30 条**：

| 来源 | 条数 | 说明 |
|---|---|---|
| **人工补判**（复核档里有、原档没有） | 29 | 人工把 `unresolved` 改判成真负样本，**从没并池** |
| 历史欠账（原档就有、池里没有） | 1 | `P033` 的 `broken`（`v.qq.com/error.html`）——旧档，没并过 |

**为什么这不是小事**：**训练侧吃的是池**（`evaluation/golden.builder._scan_negative_pool`
读的就是 `negative.jsonl` 的行），**不是存档**。⇒ 那 29 条人工判定的负样本
**压根没进训练集**——「每条分支都必须有对应样本入库，负样本不是副产品」
这条硬要求，在**人工复核这条路上断了**。

**三条既有处置路都补不上**：

| 路 | 为什么不行 |
|---|---|
| `cli downgrade` | 目标是 `unresolved`，方向是「**移出**池」 |
| `cli review --apply` | 只受理 `REVIEW_BRANCHES`（`unresolved` / `trailer_suspect` / `player_unverified`）——**真负样本不在其列** |
| `cli relabel` | 前提是这条**已经在池里**（`in_pool`）——它是「换标签」，不是「新建」 |

#### 本轮修法（已落地）

1. **新模块 `executor/pool_add.py`**——扫门禁口径的存档集（顶层、复核档优先），
   找出「该进池但还没进池」的真负样本，**逐条给判定**；
2. **新命令 `cli pool-add`**（默认 dry-run，`--apply` 落盘）；
3. **`review --apply` 接线**——回填完就把新出现的真负样本并池
   （「**并池是写入时的动作**」：回填**就是**一次写入事件）。
   ⚠️ **接线本身有测试**（`tests/unit/test_pool_add.py::Test回填接线`）——
   **模块有测试 ≠ 接线有测试**：那 29 条就是「模块对了、接线没接」；
   并池失败时**回填不崩**（回填已落盘 ⇒ 打印 warn + 指向 `cli pool-add`）；
4. **池的写路径收成一处**（`P1Archive.append_pool_rows`）——追加、坏行不牵连、
   凭据 fail-closed 三条纪律只在一处；
5. **文档四处同步**（命令清单只有一个真源，操作员手册是入口）：
   [08-常用命令速查](../../08-常用命令速查.md)（命令行 + 必填项表）·
   [04-运行手册](../../04-运行手册.md)（口径表 + 「并池」一节）·
   [07-人工复核与判读手册](../../07-人工复核与判读手册.md)（§4.5 并池提示 ·
   §2.7 处置路 · §6 处置命令从六个变七个 + §6.7）·
   [02-避坑指南 §5.3b](../../02-避坑指南.md)（这一类坑的**通用教训**：
   新增「写存档」的入口时，先问**「它会不会产出一条真负样本？」**）。

#### 三条拒并条件（**分歧不投票**）

CLAUDE.md 写着「禁止投票/集成，分歧必须暴露给人看」⇒ 命中就**跳过并打印**
冲突的那份存档与它的 evidence，**不替人投**：

| 拒并 | 触发 | 为什么不能并 | 处置路 |
|---|---|---|---|
| `quarantined` | URL 在 `quarantine.jsonl` | 整条 URL 已被人工判为拒收 | 已处置（门禁也跳过它） |
| `contradicted` | 同 URL 在**别的存档**里被判成功 | 并进去就造出「这里看不了」正对着「这里能看」——训练数据里最毒的一种错 | 人工裁定后 `cli relabel` / `cli pool-drop` / 补 quarantine |
| `stale_branch` | 池里**同 `(task_id, url)`** 已有别的分支 | 人工改判的**旧行**还留着；并新行会让同址挂两个分支，读到哪条取决于顺序 | `cli relabel`（它两边一起改） |

#### 一处必须补的细节：人工的理由要折进 `evidence`

复核档里 outcome 的 `evidence` 保留的是**代码原判**的正文
（这是对的——「只改分支，不改原始证据」才能测人工与代码的分歧率），
而**人的理由在 `review_note` 里**。而训练侧**只读 `evidence`** ⇒ 不折进去，
训练样本里就会出现「evidence 说『**无证据不记负样本**：判成 `not_play_site`
就是把「模型没答」写成「这个站看不了』」配一个负样本标签——**理由与标签互相打脸**。

⇒ 并池时 `evidence` **追加**一段 `[人工裁定 · confirm_not_play_site · reesn] …`
（折法与 `executor/relabel` 同款：**追加，不覆盖**），并另带三个字段
`review_verdict` / `reviewed_by` / `branch_before_review`（**历史池行不回填**）。

#### 本轮实测结果

- **并进 25 条**（24 条人工裁定 + 1 条代码旧账 `P033`）——
  24 条人工裁定**逐条读过 `review_note`**（都带正文原话，如
  「选集区原话『正片 | 会员 | 粤语版』」），**无一条是理由通顺的假负样本**；
- **跳过 9 条**：5 条已拒收（`v-wb.youku` 那 7 条假负样本之一）+ 1 条矛盾
  （`E021` 的 bilibili 列表页：另一份存档判成功）+ 3 条旧行
  （`E023` / `T011` / `T054`）；
- 门禁 `negative_not_pooled` **30 → 4**（剩的 4 条就是上面那 4 条有分歧的）；
- 负样本池 **267 → 292 行**。

⚠️ **那 4 条不是「修好了」**，是「**分歧被摆到台面上**」——处置权在人：
`E021` 那条是**跨 run 的真矛盾**（同 task 同 URL，一份判
`component_unverified`、一份判成功），`E023` / `T011` / `T054` 三条是
**人工改判后池里留着的旧行**（`cli relabel` 能一次改两边）。

#### 验收判据

1. `cli pool-add` 跑第二遍 ⇒ **清单为空**（幂等）；
2. `cli check-archives` ⇒ `negative_not_pooled` **只剩那 4 条**有分歧的；
3. 池里新行能答出「**这条标签是谁判的**」（`source=human` + `review_verdict` +
   `reviewed_by` + `review_note` 折进 `evidence`）——
   **训练侧只读 `evidence`**，理由不折进去就等于没记；
4. **`review --apply` 的接线有测试**（`tests/unit/test_pool_add.py::Test回填接线`）
   ——「模块对了但接线没接」正是那 29 条的成因，**接线不测就还会再犯**。

### M-41 · 池里同一 `(task_id, url)` 挂**两个不同的负分支** ⇒ 门禁看不见的自我矛盾

**现象**（2026-10-27，camofox 新批实测）：T004 的
`https://vo.youku.com/v_show/id_XNDgzODkwODE5Ng==.html?...` 在池里有**两行**：

| 来源 | 分支 | evidence |
|---|---|---|
| 老 camoufox 批 `T004__b441d5aa` | `component_unverified` | （组件未验证） |
| 新 camofox 批 `T004__b758be9a` | `subscription_wall` | `paywall 二次拦截` |

⇒ **同一个 task、同一个站、两个互斥的结论**，两行**都在池里**。

**根因**：池的去重键是 `(task_id, url, branch)`（M-38 那张表）
⇒ **分支不同 ⇒ 键不同 ⇒ 两行并存**。
而训练侧吃的是**池**（`evaluation/golden/builder.py::_scan_negative_pool`）
⇒ **同一个切片里出现两条互相打架的标签**。

⚠️ **门禁看不见**：`cli check-archives` 查的是「池行 ↔ **成功**存档」的矛盾
（`negative_contradicted_by_success`），**不查「池行 ↔ 池行」**
⇒ 这一类**一个都不报**（实测全库 **16 对** `(task_id, url)` 挂多个分支）。

⚠️ **两条写入路径口径不一致**（这才是能溜进来的原因）：

| 路径 | 遇到「同 (task,url) 已有别的分支」 | 实现在 |
|---|---|---|
| `P1Archive.write` → `_append_negatives`（**自动**，真跑那条） | **照写**（只按 `(task_id,url,branch)` 去重） | `executor/archive.py:289` |
| `cli pool-add` → `collect_candidates`（**人工回填**） | **拒并**，理由 `stale_branch` | `executor/pool_add.py` |

⇒ 同一条规则，**自动那条不查、人工那条查** ⇒ 分歧**从自动那条进来了**。

⚠️ **但「两个分支」不都是矛盾**：判定要落到
`branches.BRANCH_PRECONDITION`（贴标签前核的那张前置事实表）：

| 组合 | 是不是矛盾 | 为什么 |
|---|---|---|
| `blocked` vs `login_wall_or_blocked` | **不是** | **两层**拦截（反爬 vs 登录）——两次观测到的是不同的东西 |
| `component_unverified` vs `subscription_wall` | **是** | **同一层**（都声称「这页为什么看不了」）的两个互斥结论 |
| `not_play_site` vs `unreachable_hard` | **不是** | 一个是「站点性质不对」，一个是「这次没访问到」 |

⇒ **判据不能由我定**——**哪两个分支算互斥**是**口径题**，
要落在 `BRANCH_PRECONDITION` 那张表上（那张表本来就管「贴标签前核前置事实」）。
**不替人投票**：现在**一行都不动**（**分歧摆到台面上**，与 M-40 那 4 条同一处置）。

**差的是**：① 互斥分支对的那张表；② 自动写入路径要不要也查这条；
③ 门禁要不要加一节「池行 ↔ 池行」。**三条都属口径题，要人拍板**（见 §Q-21）。

### M-42 · 池行的 `golden_id` 里嵌了**行号** ⇒ 池一改，人核标签就掉线

**现象**（2026-10-27，**跑采集侧账时撞见的**）：要跑采集侧账（§停点 · 步骤 5）
就得让候选行带上 `access_surface`（方案 20 §R3 加的那一栏）⇒ 只能重建
`candidates.jsonl` ⇒ **重建前先试算一遍**，试算出来两件事：

| 试算项 | 读数 |
|---|---|
| 入库的 `candidates.jsonl` | **357 行**，**每行都没有 `access_surface` 键**（那栏是 R3 之后才加的，老扫描没有） |
| 按当前盘上原料重建 | **1157 行**（存档 577 + 孤立池行 30 + P2 样例 550） |
| 重建会**丢**的已标注行 | **6 条**——**全是池行** |

**根因**：`_scan_negative_pool` 给池行造的 id 是

```
{task_id}__{run_id}__{url_hash}__neg{i:02d}
```

那个 `i` 是 `enumerate(rows)` 的下标 ⇒ **它在 `negative.jsonl` 里的行号**
⇒ **id 里嵌的是「计数」不是「身份」**。

⇒ **池里任何一次增删**，都会让它**后面所有行**的 id 前移/后移
⇒ **人工标注是按 `golden_id` 挂的** ⇒ **挂不上 ⇒ 标签掉线**。

**实测（6/6 全掉）**：本轮两次 purge 之后——

| | id |
|---|---|
| 池里现在这 6 行 | `neg151` / `neg152` / `neg153` / `neg156` / `neg157` / `neg160` |
| `annotations.jsonl` 里记的 | `neg152` / `neg153` / `neg154` / `neg157` / `neg158` / `neg161` |

**整体差 1**（少了一行 ⇒ 后面全部前移一位）。
**证据一条没少**：task / run / URL 全没变，**只有序号变了**
⇒ 这不是「标注丢了」，是**键漂了**（5 条 `blocked` + 1 条 `broken`）。

⭐ **2026-10-27 把「少的那一行」查实了**（不再只是「整体差 1」的现象）：

| 步骤 | 做法 | 读数 |
|---|---|---|
| ① | 把 committed `candidates.jsonl` 的 **10 条 `__neg` id** 与**现行 builder** 逐条对 | **Δ ≡ −1**，10 条**无一例外** |
| ② | 查 builder 的下标口径有没有变过 | **没变**——自 2026-10-14 首次提交起就是 `enumerate(rows)` 的 **0 基** |
| ③ | 查 committed 文件里这 10 个 id 是什么时候定的 | **2026-10-14 首次提交起就是 `neg152`–`neg161`** ⇒ 它们是在**池里还留着某一行**的时候算出来的 |
| ④ | 那一行是谁 | **2026-10-22 撤掉的 TPIN02 `yn-mg.cn/playid/71884`**（`pool_dropped.jsonl` 第 0 条）——它在池第 5 行附近 ⇒ **之后的行全体 +1** |

⇒ **不是代码改了，是池变了** ⇒ **成因链完整**：
`candidates.jsonl` 建好后池被抽掉一行 ⇒ 后面所有 id 漂一位 ⇒
`candidates.jsonl` **没重建** ⇒ 人核标注**挂不上**。

✅ **本轮（2026-10-27 第 5 轮）撤的 6 行都在池第 300 行之后**
⇒ **没制造新的掉队**（本轮之前就已经掉队 6 条）。
⇒ **本轮不重建 `candidates.jsonl`**（重建 ⇒ 10 个 id 全改成 −1 ⇒ 那 6 条标注被冲掉
⇒ D10 分母 289 → 283）——**重建的理由远小于代价**。

⚠️ **为什么现在还没炸**：入库的 `candidates.jsonl` 是**旧扫描**留下的，
里面记的还是**老序号** ⇒ 现在还挂得上。
⇒ **下一次重建候选集就炸**：重建后这 6 条挂不上 ⇒ 被 `site` 过滤剔掉
⇒ **D10 分母 289 → 283**，而报表上**看不出原因**（只是「条数少了」）。

**为什么当初要加这个后缀**：同 `(task_id, run_id, url)` 可以挂**不同分支**的两行
（实测池里有 **2 组**：`T025/0d6d185e` 与 `TPIN02/1cf5deca`，
各挂 `login_wall_or_blocked` + `no_play_control`）⇒ **需要后缀**。
⚠️ 但**后缀不该是行号**——行号是**位置**，位置会动。

**候选修法（未实施）**：后缀改用**分支**——分支本身就在池的去重键里
（`(task_id, url, branch)`）⇒ 同 `(task, run, url)` 内**分支唯一** ⇒ 天然去重：

```
{task_id}__{run_id}__{url_hash}__neg_{branch}
```

⇒ id 只依赖**身份**，池增删**不影响**它。
⚠️ **连带**：改了 id ⇒ 那 6 条人核标注的 key 也要跟着改
（**标签内容一个字不动，只改键**）⇒ **改键 = 动人核数据 ⇒ 要人签**。

⇒ **处置**（与 M-40 / M-41 同一口径）：**代码一行不改、标注一行不改**，
把缺陷与修法摆上台面 ⇒ **§Q-22** 等人拍板。

### M-43 · 上游把「建页」与「导航」塞进同一个路由 ⇒ **那个路由失败证明不了站点打不开**

#### 现象（2026-10-27，**读第 4 轮 `T048` 那份存档时撞出来的**）

第 4 轮 `T048`（run_id `a933bea6`）有一条 `unreachable_hard`，
`branch_label` 写着「**导航失败（网络/DNS/超时）**」，而 evidence 是

```
导航失败（传输层）: camofox 导航失败: camofox tool camofox_create_tab 返回错误:
camofox error: 500: {"error":"tab create timed out after 30000ms","retryable":false}
```

⇒ **标签说「导航失败」，失败点却在 `camofox_create_tab`（建页）上**。
**零报错**：`unreachable_hard` 是一条**正规**分支 ⇒ 门禁不报、测试不报、
退出码 0、理由还「通顺」。

#### 为什么它**不是**站点事实——三条互相独立的读数

| 读数 | 出处 | 说明 |
|---|---|---|
| ① **老批真打开过同一个 URL** | `T054__93f110b6`（camoufox 面） | 同一 URL ⇒ `branch=null` · `reached_play_page=true` · `play_page_url` 落回 iqiyi 正片页 · 「存在 `<video>`/`<audio>` 标签 ×2（iframe=0）——代码层事实，不问 LLM」 |
| ② **同一批 iqiyi 是通的** | 第 4 轮 `T038`（run_id `cd571143`） | `https://` 变体（经 baidu 跳转）⇒「**已进入《功夫》正片页并触发播放**，但页面持续显示"播放异常"」⇒ **进到了正片页** |
| ③ **同一批的 `http://` 变体** | 同上 | 落的是「站点遍历超时（>180.0s）」⇒ **`unresolved`**（**没进池**）⇒ 同一批里同一个 URL **两种结论** |

⇒ 而**这一条**恰恰是**建页路由**上的一次超时 ⇒ **那条 URL 一次都没被导航过**
⇒ **站点压根没被试过** ⇒ 这是**采集侧的故障被写成了站点的性质**。

#### 根因（读上游 `server.js` 得到的；⚠️ **上游不许改**，owner 裁定 **A2**）

- 上游 `POST /tabs`（`camofox_create_tab`）把**建页与导航塞进同一个
  `withTimeout(..., tabCreateRequestTimeoutMs())`**（`server.js:3051`）
  ⇒ **这个路由失败，同时可能是「建页失败」也可能是「导航失败」**；
- 上游 `POST /tabs/:tabId/navigate`（`camofox_navigate`）**只管导航**；
- ⚠️ **两者在错误体里字面同形**：实测建页路由超时与导航路由 DNS 失败
  **都是** HTTP 500 + `{"error":…,"retryable":false}`、**没有** `code`/`recovery`
  ⇒ **光看错误体分不开** ⇒ **唯一能分开的是「走哪个路由」**。

⇒ **缺陷④的换页自愈救不了这一条**：自愈是「换一张新页 + 同一个 URL 重来」，
而**建页路由每次都失败** ⇒ 每次都超时 ⇒ 这一条的形状与缺陷④那「9 条一字不差」
的级联**一模一样**（**同样零报错**）。

#### 修法：**建页与导航拆成两次调用** ⇒ 归属判据变成**结构性的**

上游 `POST /tabs` 的 body 里 **`url` 缺省就不导航**（`if (url)`，`server.js:2990`）；
MCP server **不校验 `required`**（`mcp/server.mjs:87` 的 `args || {}`）⇒
`JSON.stringify` 丢掉 `undefined` ⇒ `camofox_create_tab` **不带 url**
⇒ 建一张 `about:blank` ⇒ 于是**建页失败**与**导航失败**落在**两条不同的路由**上。

| 失败落在 | 归属 | 分支 |
|---|---|---|
| **建页**路由 | **执行层**（那一页里**根本没有站点 URL** ⇒ 站点不可能是原因） | `unresolved`（**不进池**） |
| **导航**路由 | **站点事实**（DNS / 超时 / 证书） | `unreachable_hard`（真负样本） |

⚠️ **「换页也失败」vs「换页成功、导航失败」不能记成同一个分支**：
判据是「**那条 URL 有没有被真正导航过**」，不是「失败长什么样」。
⚠️ **`new_tab` 刻意没拆**——它的失败**不会**进分支归属
（`visit.py::open_tab` 与 `link_check.py` 都把它吃掉）⇒ 拆了只会让
「当前页停在 `about:blank`」这个隐患更容易发生。

**守卫**：`tests/unit/test_camofox_driver.py::Test归因`（**7 条**）+
`tests/unit/test_camofox_session.py::Test上游错误体的结构`（**+2 条**）。
**现场取证**：`research/probes/camofox_attribution_probe.py`——
用**生产驱动**跑三条真实路径并打印**异常类型**（**归因没有门禁**，只能现场看）：
① 正常站点 ⇒ 导航 + 观察成功（**3.72s**，正文 **2923 字**，**无降级**）
⇒ **拆开没把采集链打断**；② DNS 不存在的域名 ⇒ **普通 `DriverError`**（站点事实）；
③ 不可路由 IP ⇒ **普通 `DriverError`**（**21 秒**后失败；导航路由预算 **35 秒**）。
⚠️ **「建页失败 ⇒ 执行层」现场触发不到**（建空白页几乎不会失败）⇒ 标「**未实测**」，
**不拿单测冒充现场读数**。

⚠️ **顺带补上一个潜伏缺陷**：上游 `lib/browser-errors.js` 的 `browserErrorRecovery`
里另有一族 **`recovery="retry"`**（`session_expired` / `concurrency_timeout` /
`browser_launch_timeout`）⇒ 那是**会话**坏了 ⇒ **我们这边的问题**
⇒ 缺陷④那版的解析**不认识**这一族 ⇒ 死了会话会被记成 `unreachable_hard`。
已补 `CamofoxToolError.execution_fault`（判据是上游**自己给的** `recovery`/`code`）。
⚠️ **这一族实测还没触发过**（`negative.jsonl` 里那三个 code 与 503 **零命中**）
⇒ 属**潜伏缺陷**，不是已发生的事。

#### 存量那一条的处置：**出池两步**（不是「重跑就好」）

⚠️ **重跑不回填历史存档**，也**不会**自动撤那条池行（**并池是写入时的动作**）
⇒ 处置与 H-17 那 7 条**同一条路**，**顺序不能反**：

```
cli downgrade --url "http://www.iqiyi.com/v_19rrk40ajc.html?vfm=2008_aldbd&fc=828fb30b722f3164&fv=p_02_01" --apply
cli pool-drop  --url "http://www.iqiyi.com/v_19rrk40ajc.html?vfm=2008_aldbd&fc=828fb30b722f3164&fv=p_02_01" --apply \
                --reason "M-43：建页路由失败不是站点事实"
```

⚠️ **与 M-42 的交互（已核）**：那条池行在 `negative.jsonl` 的第 **300** 行，
而 **6 条人核标注挂的行是 `neg151…neg160`（在它前面）** ⇒ **撤第 300 行
不动那 6 个键** ⇒ **这一步不必等 §Q-22 拍板**。
⚠️ **反过来说**：**任何在 `neg160` 之后的增删都会动那 6 个键** ⇒
**Q-22 没拍板之前，别在池的后半段做增删**。

⚠️ **本条不动 `unreachable_hard` 这支的口径**：**真·站点打不开**
（导航路由失败）**该不该留在池里**是 **§Q-17**
（**已于 2026-10-27 由 owner 裁定 ⇒ 见 M-44**）⇒
**本条只保证「不是站点事实的失败不进池」**，**不替人投票**改那支的进出。

### M-44 · 「导航失败」这一支太粗 ⇒ **采集侧的故障被写成站点的性质**

**现象**（2026-10-27，第 5 轮**逐条单独复核**时摊开的）：`unreachable_hard`
这一个分支名下面装了**三种**成因完全不同的失败，而它们**不是同一件事**：

| # | 失败形态 | 原文 | 事实是什么 | 是不是站点不可达 |
|---|---|---|---|---|
| ① | **建页路由失败** | `camofox tool camofox_create_tab 返回错误: …`（M-43） | **导航请求从未发出** | ❌ **不是**——URL 压根没被试过 |
| ② | **导航被二次导航打断** | `page.goto: Navigation to "…" is interrupted by another navigation to "https://www.ctwlpt.com/voddetail/38550.html"`（E028） | **重定向确实发生了**（打断消息里直接写出落点） | ❌ **不是**——站点可达，是**跳转链中途**断了 |
| ③ | **连接被重置** | `page.goto: NS_ERROR_NET_RESET`（E028，与 ② **交替**出现） | 传输层断了 | ⚠️ **待裁**（可能仍是站点侧） |

⇒ 三者**全部**落 `unreachable_hard`（分支标签「导航失败（网络/DNS/超时）」）
⇒ 进池 ⇒ 池里那一行的断言是「**这里真的看不了**」
⇒ 而 ① ② 的事实是「**这一次没打开**」⇒ **训练信号被污染**
（教模型「这个站看不了」，其实只是慢 / 被打断 / 建页失败）。

⚠️ **M-43 只修了一半**：它把「建页与导航塞进同一个路由」**拆成两次调用**
⇒ **归属判据变成结构性的**（走哪个路由）⇒ 但**「建页失败 ⇒ 记成导航失败」这个映射还在**
⇒ 同一个洞留在了新代码里（本轮 T048 的 `…/v_19rrk40at8…` 那条就是建页路由失败
⇒ M-43 那条已出池；**机制没修**）。

**为什么不能顺手改**（与 Q-17 / Q-23 同一个道理）：
「这次没打开」与「站点不可达」的分界**不是本仓库能单方面定的**——
DNS 失败 / 连接被拒算不可达（那也是「这一次」的观测），
超时 / 打断 / 建页失败算「这次没打开」⇒ **裁定权在 owner**（**Q-17 / Q-23**）。

**修法方向**（**待裁**）：把 `unreachable_hard` 拆成两支——
「**站点不可达**」（DNS 失败 / 连接被拒）与「**这次没打开**」（超时 / 打断 / 建页失败），
后者落 `unresolved`（**不进池**，也不是「判为不是墙」）。
⚠️ **并池是写入时的动作，不回填历史存档** ⇒ **改判只影响新行**；
存量 **54 条** 要出池得另走路（**Q-23**：逐条单独复核 ⇒ 打得开 ⇒ 出池）。
⇒ **代码一行不改**，等 owner 裁定。

---

**✅ 2026-10-27 裁定（owner；Q-17 / Q-23 一并裁）**：「**先裁再跑**」——
把「**这次没打开**」（超时 / 打断 / 建页失败 / 会话失效 / 快照失败）
从 `unreachable_hard` **降成 `unresolved`**（**不进池**），
只留「**真·站点不可达**」（**机器码**证明：DNS 解析不出来 / 连接被拒）。
⇒ 因为「并池是写入时的动作、不回填历史存档」⇒ **改判只影响新行** ⇒
**跑之前改最省事**；存量 **54 条** 另走 **Q-23** 出池（逐条单独复核）。

**实施结果（2026-10-27，同日落地）**：

| 落点 | 做了什么 |
|---|---|
| `executor/browser/nav_failure.py`（**新**） | **判据单点**：两个面各自的机器码词表 → 一个**跨面统一**的语义判据 `site_unreachable()`（命中 `dns_failure` / `connection_refused`） |
| `executor/browser/page_driver.py` | 新增 `DriverUnreachableError`（带 `.code` 机器码，进 evidence）；`DriverError` / `DriverSessionError` 的口径随 M-44 收窄 |
| `executor/browser/camofox_driver.py` / `obscura_driver.py` | 导航失败时**先判机器码** ⇒ 命中抛 `DriverUnreachableError`，否则抛普通 `DriverError` |
| `executor/steps/reachability.py` | `Reachability` 增 `proven_unreachable`（**事实字段**，不是结论） |
| `executor/steps/visit.py` / `executor/steps/aggregator.py` | 两条记分链**同口径**：`proven_unreachable` ⇒ `unreachable_hard`，否则 ⇒ `unresolved`（**不进池**） |
| `executor/branches.py` | 分支标签改「**站点不可达（DNS 解析失败 / 连接被拒）**」（旧文案「导航失败（网络/DNS/超时）」在 M-44 之后是**一句假的话**） |
| `executor/branch_eras.py` | 新增 **e6** 代（`since=2026-10-27`）：`changed_branches=("unreachable_hard","unresolved")` |
| `evaluation/scorers/d5_scorer.py` | 决策树改成与代码现实一致；⚠️ **树改了、rubric 文档还是 v1.2 ⇒ 错位显式记在** [方案 02 §2.4](../设计方案/02-rubric与代码对齐.md) |

**判据的机器码是实测出来的，不是抄的**：两面各一个探针
（`research/probes/{camofox,obscura}_nav_failure_codes.py`）现场打四类用例——
DNS 解析不出来 / 连接被拒 / 超时 / 打断——**两个面的语义对齐**
（camofox 面 `NS_ERROR_UNKNOWN_HOST` / `NS_ERROR_CONNECTION_REFUSED`；
obscura 面 `ConnectError("dns error")` / `kind: ConnectionRefused`），
而 `NS_ERROR_NET_TIMEOUT` / `Timeout 30000ms exceeded` / `kind: TimedOut`
**两侧都不算**「真·站点不可达」⇒ 落 `unresolved`。

**门禁**：`4275 passed`（含 12 条新判据测试 + 5 条 recording 路径改判测试）。

⭐ **2026-10-27 补一道「产地契约」**（`tests/contract/test_unreachable_hard_producers.py`，
**10 条**）：`"unreachable_hard"` 这个**字面量**在 `executor/` 里**只准**出现在四个
文件里（两个记账产地 + `BRANCH_LABELS` 定义 + 代记录）；两个记账点都必须在
`proven_unreachable` 守卫之下（**AST 核**：祖先链里有一个 `if`，其判定式引用了
`proven_unreachable`）；两条链对**同一失败形态**必须给**同一个分支**。

⇒ **为什么要补这道**：**M-43 与 M-44 是同一个洞踩了两次**——M-43 把「建页与导航
塞进同一个路由」拆成两次调用（归属判据变成结构性的），但「**建页失败 ⇒ 记成
导航失败**」这个映射**还在** ⇒ 机制没修；M-44 把判据收成机器码，却**没留门禁**
⇒ 下次改代码同样的洞会再开一次，**而报表上看不出来**（超时记成
`unreachable_hard` 在报表上只是「又多了一条负样本」，分布动一点没人会去查）。

⚠️ **为什么守在代码层而不是数据层**：数据层要判「这份存档是**新口径**还是
**旧口径**」，而 **P1 存档不带时间戳**（实测 `steps` / `visits` / `outcomes` /
`run_config` / `provenance` 全无时间字段）⇒ 只能按**文件 mtime** 判，那是**代理
不是事实**（`branch_eras.M_TIME_CAVEAT`），且**复核一次就重写 mtime** ⇒ 旧存档
被误升代 ⇒ 门禁要么误报要么漏报。
⇒ **代码层才是护栏该在的地方**（CLAUDE.md：代码层做护栏、LLM 层做探索）。
数据侧靠「**逐条打开存档读 evidence**」那套固定动作兜（人读，机器不猜）。

⇒ **变异验证（不是假护栏）**：把守卫在**内存里**删掉
（`if reach.proven_unreachable:` → `if True:`）⇒ 契约测试当场红 ⇒ 护栏有效。
（验证脚本跑完即删；**不写回磁盘**——批次在跑，改执行层代码会让读数不可归因。）

✅ **已闭环（2026-10-28 05:50）：M-44 判据的现场证据到手** —— 判据**触发 20 次、
20 次全部正确降级到 `unresolved`**，`unreachable_hard` **0 条**

（闭环依据：续跑的 17 份批次存档逐条读 evidence；完整读数见
[编年史 §七](90-增量编年史.md)。）

| 读数 | 值 |
|---|---|
| M-44 判据 reason 命中 | **20 条**（11 条「导航失败（这次没打开·证明不了站点不可达）」+ 9 条「执行层会话失效（不是站点打不开）」） |
| 落 `unresolved` | **20 / 20** |
| 落 `unreachable_hard` | **0** |
| 「**真·站点不可达**」前缀命中 | **0 次** |
| 批次层（**28 份**）`unreachable_hard` | **0**（`unresolved` 197） |
| 池里 `unreachable_hard` | **54**，**全是存量** ⇒ M-44 批次贡献 **0** |

**机器码**：`NS_ERROR_NET_RESET` 2 · `page.goto: Timeout 30000ms` 7 ·
`HTTP 502` 1 · `navigate timed out after 35000ms` 1 · `tab create timed out after 30000ms` 9
⇒ **没有一条是 DNS 解析失败 / 连接被拒** ⇒ 按新口径**全部**该落 `unresolved`
⇒ **代码判得对**。⭐ 按**旧**口径这 20 条**会全部进池** ⇒ 教模型「这站看不了」，
而事实只是「这次没打开」——**这正是 M-44 要治的污染**。

⭐ **§五 那张「E017 重跑落盘后的必做核对清单」作废**（前提「唯一触发过判据的那条
没落盘」已不成立）：E017 本轮**两条**判据命中都是 `NS_ERROR_NET_RESET`
⇒ 落 `unresolved` **正确**；本轮 `unreachable_hard` **0 条**，
上一批那条 `FkNf6BD5y` 候选**没出现在本轮候选集里**（该串全语料 **0 处**）
⇒ ⚠️ **这不等于「它现在能打开」**，只能说明**本轮没撞上它**。

⭐ **可复用的结论：判据的触发面在结构上窄，但进站失败很常见 ⇒ 判据不空转**——

| 失败发生在哪里 | 走不走判据 | 落哪 |
|---|---|---|
| **进站**（候选站点根 URL，`visit.enter_site`，**两条链共用**） | ✅ 走 | 机器码证明 ⇒ `unreachable_hard`；否则 `unresolved` |
| **中间页**（`aggregator.probe_and_observe`） | ✅ 走——但 **`intermediates` 恒为 `[]`** ⇒ 这条路**从未执行过** | — |
| **agent 站内导航**（agent 自己点的深链） | ❌ **不走** | 错误进 `steps`，agent 自己用语义写 evidence ⇒ 多落 `unresolved`（**不进池**，效果对，但**不是判据的功劳**） |

⇒ 门禁（契约测试 + 变异验证）证明「**代码里守得住**」；
**现场那 20 次证明「判对了」** ⇒ 两者合起来，M-44 闭环。
✅ **已闭环（2026-10-28 09:50）**：**Q-23 做完了**——池里**存量那 54 条**
`unreachable_hard`（改判**之前**写进去的，**不回填**）已**逐条真浏览器复核**
⇒ **40 条出池**（降级 `unresolved` + 撤池行）、**14 条留池**（拿到真·不可达机器码）
⇒ 池里 `unreachable_hard` **54 → 14**。完整读数见
[编年史 §增量九十七](90-增量编年史.md)。


⚠️ **两条踩过的坑，别再踩**：

1. **判据必须在截断之前判**——obscura 的 `dns error` 机器码落在第 **~200 字**，
   而驱动的错误文本**截断到 200 字** ⇒ 截断后再判**认不出来**
   （存量 **17** 条 `client error (Connect)` 行就是这么丢的证据）。
   ⇒ 分类在 `_call` 里**先于** `text[:200]` 执行。
2. **标签文本不是契约**——`BRANCH_LABELS` 的**值**不参与任何契约比对
   （契约冻的是分支**名**，见 `evaluation/golden/builder.py::LABEL_UNIVERSES`）
   ⇒ 改文案不破契约；**存量金标行留着旧字面量不回填**
   （v13 三档共 **43** 行：calibration 20 / holdout 14 / regression 9；
   历史版本各自快照，**本来就不该跟**）⇒ 分代边界由 **e6** 记。

### M-45 · **凭据闸门的一次误报炸掉了整批 16 条** —— 两个缺陷叠在一起

**现象**（2026-10-28 00:30）：28 条的 M-44 重跑批次跑到**第 12 条**（E017）时抛
`CredentialLeak` ⇒ 异常从 `archive.write` **一路冒到 `main`**
⇒ **整批只跑成 11 条**，剩下 **16** 条全丢，**且不留痕**
（进程直接死 ⇒ 连失败清单都没有 ⇒ 那 16 条在批次报表上长得和「没采过」一模一样）。

⇒ 这是**两个缺陷叠在一起**，缺一个都炸不出来：

| # | 缺陷 | 位置 | 单独存在时的后果 |
|---|---|---|---|
| ① | **韧性缺陷**：`archive.write` 在 per-entry 的 try/except **之外**（那个 try 只护住 `orch.run`）⇒ **一条存档的闸门能炸整批** | `executor/cli.py::_run_plan` | 与「浏览器起不来」同款缺陷：2026-10-26 已修过一次（起浏览器失败只毁那一条），**写盘这条路漏了** |
| ② | **误报缺陷**：`sk-[A-Za-z0-9]{16,}` **不锚定左边界** ⇒ **百度跳转链接的 base64url 参数**（字符集 `A-Za-z0-9-_`）里**必然偶发**出现 `sk-` + 一串字母数字 | `executor/archive.py::_CREDENTIAL_PATTERNS` | 一条正常存档被当成含凭据 ⇒ **被拒写盘**（那条采集结果全丢，E017 跑了 **18 分钟**） |

⭐ **取证是零成本的**：存档没落盘，但**候选 URL 在批次日志里**
⇒ 不必重跑就能定位——把日志里的候选 URL 逐条喂给扫描器：

```
命中：http://www.baidu.com/link?url=FkNf6BD5y-B6dG1spuolNhomLxeySk-LgjTzmjWV4aeHAPQn6GLyXpw_...
形态：len=27 shape=AaXAa2Aa3A29a2A4a9A2aAa2 prev='_'
```

⇒ 命中的是 ``Lxey``**`Sk-LgjTzmjWV4aeHAPQn6GLy`**——**嵌在 base64 串中间**，
前邻是字母数字 ⇒ **纯误报**，与凭据无关。

⚠️ **为什么「嵌在词里」就能判误报、而真 key 不能**：**真 key 总是独立的一个 token**
（前面是空格 / 引号 / `=` / `/`），而嵌在更长词里的 `sk-` 是**编码串的一段**。
⇒ **左边界**（`(?<![A-Za-z0-9])`）恰好分开这两者。

**处置（三件，都已落地）**：

| 落点 | 做了什么 |
|---|---|
| `executor/archive.py::_CREDENTIAL_PATTERNS` | `sk-` 与 `sk-ant-` 两条**加左边界** ⇒ 只放宽「嵌在词里」那一种形态，**真 key 照拦**（前邻是空格 / 引号 / `=` / `/` 都拦） |
| `executor/cli.py::_run_plan` | `archive.write` **纳入 per-entry 保护** ⇒ 凭据闸门**不再炸整批**；失败理由说清是「**内容**命中凭据特征」不是「**能力**」问题（两者处置完全不同，混在一列里会被读成同一件事） |
| `executor/archive.py::credential_shapes` | **脱敏形态**（长度 + 字符类别构成 + 前邻符号）——⚠️ **排错要形态不要原文**：`credential_samples` 给的是**匹配原文**，那**就是凭据本身**，放进日志就等于把凭据复制出去（红线） |

⚠️ **残留风险（明写）**：「**宁可误报**」的口径被**局部**放宽了——只对 `sk-` 两条的
「嵌在更长词里」那一种形态。真 key 形态（前邻非字母数字）**照拦**，
但「某个真 key 恰好被塞进一个更长的字母数字串里」这种形态**现在不拦了**
（实测**没遇到过**，且那种东西本来也不像 key）。
⇒ 这条放宽是**有据的**（今天的现场样本），不是「放松纪律换跑通」。

**门禁**：`4321 passed`（新增 **韧性 1** 条 + **脱敏形态 11** 条 +
**左边界 12** 条）。⚠️ 左边界那条把**今天真实毒化整批的样本**写进了测试
（`tests/contract/test_archive.py::Test左边界::BASE64_PARAM`）⇒ 同一个洞不会再开。

⚠️ **顺带查出一件待查的事**：E017 里有**一条 `unreachable_hard`**
（百度跳转链接 `FkNf6BD5y-...` 那条）——按 M-44 新口径，**只有 DNS 失败 /
连接被拒**才配记它 ⇒ **它的 evidence 得看**。
⚠️ 那条存档**没落盘**（被凭据闸门拦的）⇒ **要等 E017 重跑落盘后才能判**
⇒ 续跑计划**把 E017 排在第 1 位**（`plan_from_corpus.py` 按「有无现行面存档」
自动识别 ⇒ 没落盘 ⇒ 仍算必要重跑 ✔）。

⭐ **2026-10-28 01:05 补一条现场证据**（这条把「判据有没有在现场跑过」
从「不知道」变成「跑过，但证据随存档一起丢了」）：

```
rerun_prod_m44.log:2423  [E017] 环节② http://www.baidu.com/link?url=FkNf6BD5y-…
                          → unreachable_hard
rerun_prod_m44.log:2417  [E017] 环节② …otP-tNOat15j9aGre… → unresolved
rerun_prod_m44.log:2420  [E017] 环节② …qp0QHW_UzJhUhkzx… → unresolved
```

⇒ **M-44 判据确实在生产面上触发过并分流了**（不是空转）；
⚠️ 但**日志只记分支名、不记 reason** ⇒ 机器码**只能靠重跑读 evidence 才知道**
⇒ 这就是**未闭环的那一步**（见 M-44 节末的核对清单）。
⚠️ 也因此：拿「落盘 11 条里 `unreachable_hard` 0 条」去论证判据生效或没生效，
**两边都不成立**——**唯一触发判据的那条不在那 11 条里**。

⇒ **续跑已启动**（2026-10-28 01:10）：`plan_rerun_prod_m44_cont.json`
**17 条 / 0 跳过**，**E017 排第 1 位**（日志
`trajectory_pipeline/output/pipeline/rerun_prod_m44_cont.log`），
已确认起跑：E017 取回候选 19 → 保留 16 → 16 个进环节②。

### M-46 · **硬故障任务重跑**（增量九十六 §7-4 那 26 条 ⇒ **9 个任务**）—— ⚠️ **B4 得拆成两半**

**触发**：增量九十六 §7-4 把 28 份批次存档的 **428** 条 outcome 按「结论是不是
采集侧造成的」分了六族，结论是 **B1 / B2 / B4 共 26 条**属于「**同参数重跑就会不同**」
⇒ 落在 **11 个任务**上 ⇒ 那 11 个任务**必要重跑**。

⚠️ **B4 不是一个族，是两个**（把它当一族是个错）：

| 子族 | 证据首段 | 性质 | 该怎么办 |
|---|---|---|---|
| **B4a** | `后端未作答（http:500）` | **瞬时**后端故障 | ✅ **重跑**——同参数重跑就会不同 |
| **B4b** | `后端未作答（budget:max_tokens 用尽…）` | **输出撞了 token 上限** | ⚠️ **先调参**——同一页同参数重跑**大概率同样撞**；证据原文自己写着「调大 `TRAJECTORY_LLM_MAX_TOKENS`」 |

⇒ **真·值得重跑的**：B1 **11** 条 + B2 **10** 条 + B4a **2** 条 = **23** 条，
落在 **9 个任务**上（**E021 9** · T025 3 · E017 2 · T018 2 · T031 2 · T054 2 ·
E002 1 · T024 1 · T042 1）。
⇒ **E001 / E005 显式跳过**（硬故障只有 B4b ⇒ 进 `skipped` 并点名
`needs_param_tuning`——**不静默丢**）。

**26 条逐条读过 evidence 的核对结果**（分类不是靠猜）：

| 族 | 条数 | 实际形态 |
|---|---|---|
| B1 | 11 | 导航超时（`page.goto: Timeout 30000ms` / `navigate timed out after 35000ms`）· `NS_ERROR_NET_RESET` · `HTTP 502` |
| B2 | 10 | **9 × `tab create timed out after 30000ms`**（E021 全是这条 ⇒ 执行层建页失败）· 1 × `404: Tab not found`（取不到快照） |
| B4a | 2 | `http:500`（T025 / T042） |
| B4b | 3 | `budget:max_tokens 用尽`（E001 / E005 / T042） |

**判据单点**：`research/recollect/plan_hardfail_rerun.py::hard_kind()`
——判据是**证据首段的固定文案**（采集侧写死的，不随站点变）⇒ 比解析自由文本稳。

**实例来源**：**批次存档本身**反推（同一 `task_id` / 片名 / 检索式 / 用户原话）
⇒ 新旧两份可**逐行对照**；实例字段的拼装与「能不能跑」的六条硬条件
**import** `plan_from_corpus.py`（**一份判据两处用**）。

**计划**：`output/pipeline/plan_rerun_hardfail.json` —— **9 条 / 0 跳过**，
批次身份 **`rerun-hardfail-2026-10-28`**（⚠️ **与 M-44 那批不同**——
批次边界前后分布会跳变，批次元数据就是记这件事的 ⇒ 别把两批的读数混着比）。

**跑法（串行）**：

```powershell
uv run python -m trajectory_pipeline.executor.cli run \
  --plan trajectory_pipeline/output/pipeline/plan_rerun_hardfail.json \
  --executor-mode agent --driver camofox --perceptor llm --log-level info
```

⚠️ **串行是硬要求**——上游 `lock_queue` 会**销毁别人的页** ⇒ 并发跑 = 互相毁页。
⚠️ **起跑前**先 `cli check --driver camofox` 探一次会话层（指纹 + 元素枚举）。

**未闭环**：跑完仍要走那一套固定动作（**逐条打开存档读 evidence** → 重建产物 →
门禁 → 并池干跑），且**新批次的人判读数要按代分开看**（**e6** 是 M-44 的分代边界）。

⭐ **还欠一条 owner 裁定**：**B4b 那 3 条**（E001 1 · E005 1 · T042 1）要不要
**调大 `TRAJECTORY_LLM_MAX_TOKENS` 后再跑**？⚠️ **调参会改 `run_config`**
⇒ 与存量档案**不可直接比**（`max_tokens` 影响 ④ 的判定）⇒ 要么**单开一批**
（批次身份另起、读数分开看），要么**先不调**。
⇒ **本轮先不调**（不改存量批次的可比性）——这是**我的判断，不是裁定**。

⭐ **2026-10-28 · M-46 批次跑完（9/9），固定动作已走完** —— 详见编年史
**§增量九十八**。摘录（**同任务集对照**才可比——重跑批专挑有硬故障的任务跑的）：

| 项 | 读数 |
|---|---|
| 批次 | `rerun-hardfail-2026-10-28` · 10:18 → 14:00 · **串行** · 9/9 落盘零失败 |
| 同尺 | `run_config` 逐项 **9/9 零差异**（含 `per_site_timeout_s: 180.0`）· 红线扫描 **9/9 零命中** |
| **失败模式** | B2 建页超时 **10 → 0**（清零）· B4a 后端 500 **2 → 0**（清零）· **遍历超时 0 → 6**（新生）· B1 11 → 12 · 环境级阻断 3 → 5 · 结论类 111 → 114（两批各 138 条 outcome） |
| 遍历超时率 | 本批 **6/138 = 4.3%** · `plan-camoufox-prod1` 11/134 = **8.2%** · M-44 批 7/428 = 1.6% ⇒ **camofox 既有形态，不是本批引入**；根因 **6 条里 5 条撞在百度跳转链上**（全库 32 条 / 24 条跳转链） |
| 新旧对照（24 条旧硬故障） | `no_conclusion` 11 · `still_hard` 5 · `negative` 4 · `ambiguous` 2 · `success` 1 · `not_covered` 1；⚠️ **「变成真负样本」那 4 条里 3 条靠标题软证据认出来的** |
| **池** | **393 → 407**（+14）：本批 9 个任务 **41 条真负样本 outcome ⇒ 14 条新并 + 27 条去重命中**（去重键 `(task_id,url,branch)`）；⚠️ **新增 14 行里 6 行（43%）是百度跳转链**（M-47 的实测代价）；全池 **41/407（10.1%）** URL 是跳转链 |
| 门禁 | 退出码 **1**（不变）· 真负样本 418 → **459**（+41，与 outcome 数分毫不差）· FATAL `negative_contradicted_by_success` **37 → 41（+4，本批引入：E017 ×2 · T042 ×2 · 同一形态：爱奇艺同 URL 既判墙又判成功）** · `success_branch_mismatch` 8 → 12 · `negative_not_pooled` **3（不变）** |
| 产物 | build ⇒ **126** 存档 / **1227** 条 P2 / **3681** 份 P3 · task-view ⇒ **126** 份 T 视图 |
| 测试 | **4368 passed** |

⚠️ **B4b 躲不掉**：T054 这轮**又撞了一次** `budget:max_tokens` ⇒ **同参数大概率同样撞**
那条判断被实测证实 ⇒ **调参那件事必须做**，且**要单开一批**（`run_config` 变了
⇒ 与存量档案不可直接比）。⚠️ **本轮仍不调**（不替人投票）。

### M-47 · `unreachable_hard` 判在**跳转链**上 ⇒ 「站点不可达」**指向了错的对象**

**触发**：M-46 那批（`rerun-hardfail-2026-10-28`）跑到 E017，新档
`E017__039d3c57.json` 里出现一条 `unreachable_hard`，**URL 是百度跳转链**
（`http://www.baidu.com/link?url=tvgYh…`）。

**证据原文**（机器码命中是真的）：

```
导航失败（真·站点不可达·机器码 NS_ERROR_CONNECTION_REFUSED）: camofox 导航失败:
camofox tool camofox_navigate 返回错误: camofox error: 500:
{"error":"page.goto: NS_ERROR_CONNECTION_REFUSED\nCall log:\n  - navigating to
\"http://www.baidu.com/link?url=tvgYh…\", waiting until \"commit\"\n","retryable":false}
```

⚠️ **call log 里只有一跳**（导航停在百度自己身上，**没有**解析后的目标站），
且该条 `landed_url` **为空** ⇒ **从头到尾没解析出那个影视站是谁**。

**问题**：`unreachable_hard` 的断言是「**这个站**真的看不了」（站点性质 ⇒ 进负样本池）。
这一条对不上：

- 被导航的 URL 是**一次性跳转链**（每次检索都不同 ⇒ 池行里的 URL **本身无效**）；
- 归因对象（那个影视站）**从未被识别** ⇒ 「不可达」这个结论**没有可指的对象**。

**量**（实测，不是估的）：全部存档里 `unreachable_hard` outcome 共 **18** 条，
其中 URL 是跳转链的**只有这 1 条** ⇒ **单点新形态，不是系统性缺口**
（Q-23 那轮把存量 16 条跳转链的行逐条复核掉了，留池的 14 条**全是直站**
⇒ 这个形态**第一次**出现）。

**判据现状**：`executor/steps/reachability.py::probe` 只接 `DriverUnreachableError`
⇒ `proven_unreachable=True` ⇒ 配记 `unreachable_hard`。
⚠️ **机器码那层是对的**（`nav_failure.py::site_unreachable` 判的是「是不是真·不可达」，
语义跨面统一）⇒ **缺的不是机器码，是一道「归因对象在不在」的护栏**。

**两条路**（**不替人投票**）：

- **甲（推荐）**：在 `probe()` 里加一条**归因对象**护栏——被导航的 URL 是搜索引擎
  跳转链**且**未解析出落点（`landed_url` 空）⇒ `DriverUnreachableError`
  **不配记** `proven_unreachable` ⇒ 降 `unresolved`（**不进池**）。
  理由：fail-closed——**证明不了对象的结论不许当站点性质**。
  ⚠️ 代价：机器码明明命中了却降级 ⇒「连接被拒」这个**站点事实**会被丢掉
  ⇒ 建议把机器码**原文照录进 reason**（像 Q-23 留池那 14 条那样），别让它连同分支一起消失。
- **乙**：不动判据，只把这 1 条池行**出池**（`cli pool-drop`）⇒ 但那是治标，
  下批还会出同形态的行。

⚠️ **本轮不改代码**：这是**分支口径**的裁定（与 M-44 / Q-17 同级），
且批次还在跑 ⇒ **代码一行不改、池一行不动**，缺陷摆上台面。

⭐ **2026-10-28 批次跑完后的实测代价**（M-46 那批 9 个任务全跑完）：

| 项 | 读数 |
|---|---|
| 池增长 | **393 → 407**（+14 行） |
| **新增 14 行里百度跳转链占** | **6 行（43%）**：E017 的 2 行 `not_play_site` + 1 行 `unreachable_hard` · E002 的 1 行 · T042 的 2 行 |
| 全池口径 | **41 / 407（10.1%）** 的池行 URL 是跳转链（40 `not_play_site` + 1 `unreachable_hard`） |
| ⚠️ **去重键拦不住的那一种** | T042 那两行**是同一个百度知道帖子的两个不同跳转 URL**（前缀相同、参数不同）⇒ `(task_id,url,branch)` 拦不住「**同一个站换了个跳转链**」⇒ 这是 M-47 的**第二个面** |
| 遍历超时 | 本批 **6 条里 5 条撞在跳转链上**（全库 32 条里 24 条是跳转链）⇒ **同一根因的另一面**：180s 是在**解析跳转链**时吃掉的，不是那个影视站慢 |

⇒ **归因面**（`unreachable_hard` 指错对象）与**成本面**（遍历超时白烧 180s）
**是两件事**：遍历超时 ⇒ `unresolved` ⇒ **不污染池**；`unreachable_hard` ⇒ **进池**
⇒ **污染标签**。⇒ **甲方案（归因对象护栏）治的是后者**；前者要另治（跳转链本身）。

**锚点**：`executor/steps/reachability.py::probe` ·
`executor/browser/nav_failure.py::site_unreachable()` · 存档 `E017__039d3c57.json` ·
本批 `output/pipeline/negative.jsonl`（`run_id=039d3c57` 那条）·
同族 **M-28** / **M-43** / **M-44** / **Q-17** / **Q-23** ·
编年史 **§增量九十八 §六**。

| ID | 决策 | 锚点 | 两条路的代价 |
|---|---|---|---|
| **Q-01** | 簇 A 出路：`page_quotes` 是否纳入救济输入源 | [00 §不能做](00-总览.md) · 编年史 §增量四十四 | 纳入 = 「信 LLM 自报」，**触碰纪律边界**；不纳入 = 转攻采集层（H-03/H-04） |
| **Q-02** | 簇 C 题绑定是否按 LLM reason 动态派分支 | 编年史 §增量六十 · [04 §建议优先级](04-探针七层域.md) | 同上——动态派分支 = 让 LLM 决定贴哪个标签。**方案 Z（LLM self-reported 门）默认禁**，除非 owner 明批 |
| **Q-03** | m.ixigua 金标语义（H/L-10）谁来复核 | [08 §未闭环](08-存档治理与回流域.md#本域已知未闭环项) | 上游标注问题，代码层无解 |
| **Q-04** | 存量 rationale 重生成的**配额**何时给（M-02） | [05 §INC46 收尾](05-rationale与闸门域.md) | ≈**767** 次 LLM 调用（2026-10-25 实测）；不给配额就一直停在「事后反思口径」。**但不再阻塞阅读**：旧代样本已逐条标 `rationale_era=post_hoc`、闸门 `not_run`，可先出 `--rationale-era pre_action` 的同口径批次 |
| **Q-05** | D-11 camoufox 是否过 go/no-go（H-08） | [设计方案/04 §6.1](../设计方案/04-camoufox执行层替换方案.md) | 拦截率不达标就不换；换了要重标定 `elements_total` 口径（D-2 打分口径漂移风险）。✅ **子问题已闭（2026-10-21）**：执行口径定为**无人值守走 `headless=True`**（增量六十九）。⚠️ **go/no-go 本身仍未闭**——六条判据没过。**判据②③的「差值不可比」已闭**（2026-10-21，增量七十：共享口径 + 归因表落地）。✅ **读数也拿到了（增量七十二）**：headless、3 task × 2 面、开 `--element-basis`——**判定仍 `fail`**。★**但「fail」现在要分着读**：① **headless 拦截率这条前置门已过**（真·反爬 B 9.09% < A 14.89%，配对子集同向）⇒ **不构成否决项**；② 判据①⑤⑥ **还没闭**：①缺可比口径（H-14）、⑤人判（已抓到 9 条假账，H-15）、⑥**连工具都没有**（M-25）。⭐ **owner 要拍的是**：在这些闭环之前，`DEFAULT_KIND` 是不是继续按 §6.1「不许切换」执行（当前是）——**还是先做 H-14/H-15/M-25 这三件**。⚠️ 增量七十一那条修正是**这次读数的前置**：共享口径测失败时旧代码把失败混进 `Observation.degraded`（⇒ `extract_candidates` 不取候选 ⇒ 整批零样本且零报错），现在失败走 `element_basis_error` 独立通道。✅ **已闭（2026-10-22，增量七十九）——go/no-go 被 owner 裁定绕过**：owner 判「**不再双跑**，选定 camoufox 做默认浏览器调用主力——**反爬拦截与页面内容获取是主要，速度次要**，可接受增加等待时间」。⇒ §6.1 那六条判据**不再是切换的门槛**（它们是「双跑可比」路线下的门槛，而 owner 判的是「不双跑了，直接换」）。⚠️ **但覆盖 ≠ 六条读数作废**：① 真·反爬 B **9.09%** < A **14.89%**、② obscura 抽不出客户端渲染页正文（A 侧 60% 的站点「没测到」）、⑥ 动作可移植 16/16 —— **三条支持裁定的方向**；③ `elements_total` 两面**不同义** ⇒ **不可混池**这条**继续有效**；④ 合并口径（反爬+登录墙）A 19.15% / B 34.09% 与①**相反**——⚠️ **owner 的裁定针对「被拦截」，按①的真·反爬口径 camoufox 更好**，④ 里混的登录墙**不是浏览器能解决的**（那要账号），不该算进「浏览器好不好」的账；⑤ 是**分支体系**的问题，与执行层无关。⇒ 落地：`DEFAULT_KIND = "camoufox"`（`driver_factory.py`），`per_site_timeout_s` 90→180 并暴露 `--per-site-timeout`，P6 指纹进 P1，探针三条命令一起接。⚠️ **尾巴两件**：① ~~**第一批 camoufox 真实批次还没跑**~~ ✅ **两批都跑了**（2026-10-22，12/12，退出码 0）——**第一批（修复前）已整代回滚**（增量八十～八十一：15% 的观察是「读到一半」⇒ 不能当训练数据），**第二批（修复后）才是当前盘上的生产数据**；✅ **判据①（真·反爬拦截率）与判据②（`body_text`）已在第二批上取**（**8.55%** / **800.5**）——⚠️ **必须取在处置之后**（5 条假成功降级 ⇒ 分母 147→152 ⇒ 先取数后处置就是旧分母）；② ~~**第一批落盘后 `Test存量口径` 会红** ⇒ **重算 `required`**~~ ✅ **已重算、无需改动**（`Test存量口径` 改成**逐面各自核**——期望值不能取 `toolcatalog.CATALOG`，那是 obscura 面的定义集，用它核 camoufox 面**必然不等**；第二批 180 条 camoufox-1 样本上 `click` 的 `{role,name}` 53/53、键 100% 在、值 100% 非空） |
| **Q-06** | 旧 per-unit LS 路径 2026-11-09 到期后删还是续（M-07） | [设计方案/14 §9](../设计方案/14-完整任务视图与LS上传.md) | 删 = 依赖方断；续 = 两套口径继续并存，**并池口径分歧会有人读成质量变化** |
| **Q-07** | `partial_free` nav-bar 形态在黄金集里有没有真例（决定方案 W/X 边界，H-07 前置） | 编年史 §增量五十八～六十 · [04 §建议优先级](04-探针七层域.md) | 有真例 → 词表 e2e 先跑；没有 → 方案 X 已够 |
| **Q-08** | ✅ **已裁定（2026-10-23，owner）：rubric 变更强制连带改 prompt**（方案 16 **D-C7**，解锁 C-10） | [方案 16 §2.2](../设计方案/16-评估器与rubric双向校准回路方案.md) · [§9.5 实施结果](../设计方案/16-评估器与rubric双向校准回路方案.md#c-10--c-11-实施结果2026-10-23分支-main) | 裁定「强制」⇒ **C-10 已闭环**：`rubric_version.py` 加 `ANCHOR_TABLES`（D-3 / D-9 逐档释义，逐字抄自 rubric 文档）+ `anchors_drift()`（现读文档比对，**改档不改表 ⇒ 非空**）+ `render_anchor_block()`；D-3 / D-9 的 prompt 改为**从表渲染**（D-9 六条 verdict 措辞从表渲染，原先是手抄）。**留下的义务**：① 改 rubric 某档**必须**同时改锚点表（契约测试逐档核）；② **prompt 变了 ⇒ 指纹变**（`9b3ec1c46545` → `1046e67994aa`），**下一版冻结前 D-3 / D-9 各跑一次真实 A/B**（✅ **2026-10-24 已跑**，见 [方案 16 §9.5 实施结果](../设计方案/16-评估器与rubric双向校准回路方案.md#95-p3--prompt--rubric-对齐)；⚠️ 原注「本机无凭据」**作废**——凭据是在的，是当时没跑）|
| **Q-09** | ✅ **已裁定（2026-10-23，owner）：合并等价类不需 owner 签**（方案 16 **D-C4**，解锁 C-13） | [方案 16 §2.2](../设计方案/16-评估器与rubric双向校准回路方案.md) · [§9.6 实施结果](../设计方案/16-评估器与rubric双向校准回路方案.md#c-13--c-14-实施结果2026-10-23分支-main) | 裁定「不需签」⇒ **C-13 的「owner 签」那一半作废**，「无人记账」那一半**保留**：`alignment.py` 加 `EQUIV_CLASS_NOTES`（每个**多标签**类一张注记：规范标签 / 被并标签 / **丢了什么细分** / 为什么并）+ `invisible_details()`（**「合并后不可见细分」清单**）+ `validate_merge_notes()`（与表**逐标签**比对）。**留下的义务**：改 `EQUIV_CLASS` **必须**同时补注记（漏了就红）——**没有签名字段，但有机器核对** |
| **Q-10** | ✅ **已裁定（2026-10-23，owner）：阈值调整不算 rubric 级变更**（方案 16 **D-C6**，解锁 C-14） | [方案 16 §2.2](../设计方案/16-评估器与rubric双向校准回路方案.md) · [§9.6 实施结果](../设计方案/16-评估器与rubric双向校准回路方案.md#c-13--c-14-实施结果2026-10-23分支-main) | 裁定「不算」⇒ **C-14 换了形状**：不升 rubric 版本、不进 rubric 变更记录、不设签名门；改由**阈值自有其面**——新文件 `evaluation/scorers/thresholds.py` 是唯一真源（11 条，带值 / 定线状态 / 依据 / 使用处），`triggers.py` / `verify.py` / `gate.py` / `builder.py` 全部改从 `thresholds.value("<name>")` 取。**留下的义务**：① 改阈值**只改表一处**（契约测试盯「使用处不留字面量」「表里有条目没人用」）；② ⚠️ **D-C9 的后半句随之作废**——「定线走 rubric 变更流程」**不再是路径**，定线只改表的 `status`（只出读数 → 已定线）|
| **Q-11** | ✅ **已裁定（2026-10-23）：金标粒度 = 按访问（per-visit）**（M-30 前置） | 本文 §M-30 现场重采 | 裁定「按访问」⇒ 19/131 个 URL 的多头标签是**正常事实**（站点漂移 / 地区 / 登录态），只在读数里标出来、**不判缺陷**。⇒ **不重标、不废弃行、不做 `freeze --refresh-gold`**（那条能力因此不需要存在）。代价：同一 URL 上的分歧读数天生带噪声——落其上的分歧卡**不能单独**用来判 H-02 的方向 |
| **Q-12** | ✅ **已裁定（2026-10-23）：切档 = 按行**（M-30 附带发现：289 条站点级冻结行只落在 131 个 URL 上，25 个 URL 跨档，**65/101 持有集行与调参池/回归集同 URL**） | 本文 §M-30 现场重采 · [07 评估](07-评估与黄金集域.md) | 裁定「按行」⇒ **不重切**（C-17 本也不许在继承式冻结里重切回归集），跨档泄漏**保留**：25 个跨档 URL、持有集 65/101 行与调参池/回归集同 URL。⇒ 义务：**所有持有集读数必须带附注**——「持有集 101 条站点级」不是 101 条独立证据。已落：`agreement` 的「URL 级金标读数」（**数字真源**）+ `verify` 持有集读数的附注（指向真源，不重算——快照里没有 URL） |
| **Q-13** | ★**（2026-10-23 新增）④ 的提示词把媒体计数抬成正面证据**——M-26 删了代码层短路，**计数没退出判断，只换了入口** | 02 感知 · [`questions.py`](../../perception/questions.py) 的 `Q.PLAYER_OK` · 本文 §Q-13 | 提示词第一句就写着「**结合代码测得的媒体元素数量**，判断它是不是一个能正常播放的播放器页」⇒ 模型**照提示词办事**。实测：网易号美食图文页（6 个 `<video>` 是点广告后注入的）连问 **8 次 = 7 True / 1 False**（6 连问 6/6 True）⇒ **那条假成功没被 M-26 消除**。⚠️ **不能自己决定**：改 ④ 提示词 = 改 rubric 口径（M-26 禁区）。**两条路**：**A** 把计数从「判据」降级成「排除性线索」（措辞改 ⇒ 要重跑 ④ 召回）；**B** 不给计数（⚠️ 真实视频站**多是 iframe/JS 播放器**，`xdjdxx`/`yn-mg` 那两条 `video=0、iframe=3` 是**真播放页** ⇒ 召回可能塌） |
| **Q-14** | ★**（2026-10-24 新增）D-3 的 `playable` ↔ `nav_aggregator` 两个定义在这一类页面上同时成立**——而且**五类词表根本没有 rubric 文档出处** | 07 评估 / rubric · 本文 §Q-14 · `research/probes/prompt_ab_anchor.jsonl`（`T030__ae6553b1__site-10` / `T048__0680dc4a__site-10`）· [`d3_scorer.py`](../../evaluation/scorers/d3_scorer.py) `D3_CATEGORIES` / `D3_SYSTEM_PROMPT` · [rubric D-3 卡](../设计方案/在线视频场景rubric.md) | 同一 URL `http://56dy.com/movie/2842`（两份存档 T030 / T048，同一任务「功夫」）**观察逐字段相同**（`video_tag_count=0`、`iframe_count=0`、`body_len=1553`、`links_total=45`、同一标题，**只差一个轮播计数**）⇒ **旧版提示词下判成相反**（`playable` / `nav_aggregator`）**新版提示词下也判成相反**（`nav_aggregator` / `playable`）——**同一版提示词对同一页给了两个标签**。⇒ 不稳定是**提示词无关**的：锚点块既没造成它、也没修好它。⚠️ **根因不是「忘了划线」，是两个定义重叠**：`playable` = 「能直接播放目标视频的站点」（经「量子云」外链**能看**）、`nav_aggregator` = 「把用户引到外部播放站，**自身不存正片**」（**无内嵌播放器**）⇒ **散文没有区分「站内播放」与「经外链跳转播放」**。⚠️ **rubric 的 D-3 卡压根没写这五类**（`playable` / `nav_aggregator` / `review` / `irrelevant` 零出现，卡里是「播放站/导航站/无关站」三说法）⇒ **没有条款可指**。**两条路**：**A** 把五类词表 + 一条**判别式**（「站内播放」还是「经外链跳转播放」）写进 rubric D-3 卡（**改 rubric ⇒ 走 §5.5 条款级流程，owner 签**；且按 D-C7 **连带改 prompt**）；**B** 认定 D-3 的类别只存在于评分器（则 rubric 文档要补一句「本维度只管档位、类别口径在评分器」，并把 D-C7 的管辖边界写死——否则 rubric 与代码两套口径并存） |

| **Q-15** | ★**（2026-10-24 新增）优酷 v_show 的判据边界**——同一份正文被读成三种落点 | 07 评估 / rubric · 手册 §7.2.2 O-a · 本文 §M-30 · M-30 §2 | 页面事实：`iframe=1`、`video=0`、导航栏有「续费会员」。⇒ **两种读法**：①（rubric 读法）iframe 是判据 ⇒ 有组件可确认 ⇒ 落成功；② 同页有 iframe ⇒ 组件只是**候选** ⇒ 应落**未验证**。M-30 §2 明写「这条判据边界上有两种读法」、owner 未收敛。**影响 4 行**。⚠️ 这与 M-30 已定性的 19 个 URL **不是同一件事**（那 19 个定的是 D-10 分支标签） |
| **Q-16** | ★**（2026-10-24 新增）西瓜「打开App观看」≡「打开App看完整内容」吗** | 07 评估 / rubric · 手册 §7.2.2 O-b · M-30 §8 | 页面正文原话「打开西瓜视频，看完整高清内容」「付费」「**打开App观看**」。认 ⇒ 落 `subscription_wall`（排除）；不认 ⇒ 落 `unresolved`（未决、不入池）⇒ **结论就变了**。**影响 2 行** |
| **Q-17** | ✅ **已裁定（2026-10-27，owner；Q-17 / Q-23 一并裁）：「先裁再跑」——「这次没打开」（超时 / 打断 / 建页失败 / 会话失效 / 快照失败）从 `unreachable_hard` 降成 `unresolved`（**不进池**），只留「**真·站点不可达**」（**机器码**证明：DNS 解析不出来 / 连接被拒）** | 03 标签 / 08 存档 · 手册 §7.2.2 O-c · 本文 **M-28**（同族）· H-15 / H-17 · **本文 M-44（裁定 + 实施结果）** · [方案 02 §2.4](../设计方案/02-rubric与代码对齐.md) | H-15 / H-17 的口径是「采集失败不该进负样本池」；而 `unreachable_hard` **不在** `REVIEW_BRANCHES` 里 ⇒ **采集侧的故障被装进「站点不可达」** ⇒ 池里那行的断言是「这里真的看不了」，而事实只是「这次没打开」⇒ **污染训练信号**（教模型「这个站看不了」，其实只是慢 / 被打断 / 建页失败）。⇒ 因为「**并池是写入时的动作、不回填历史存档**」⇒ **改判只影响新行** ⇒ **跑之前改最省事** ⇒ **先裁再跑**；**存量 54 条另走 Q-23 出池**（逐条单独复核）。**落地口径**：判据单点 `executor/browser/nav_failure.py::site_unreachable()`（跨面统一，两面机器码实测对齐），两条记分链（`visit` / `aggregator`）同口径；分支标签改「**站点不可达（DNS 解析失败 / 连接被拒）**」。⚠️ **rubric v1.2 的 D-5 决策树与代码树已错位** ⇒ 显式记在方案 02 §2.4；**本轮不升 rubric 版本**（v1.3 需 owner 签核） |
| **Q-18** | ★**（2026-10-24 新增）第三方影视站（策驰影院）入不入池** | 03 标签 / rubric · 手册 §7.2.2 O-d | rubric 决策树没有这种站点形态；按 **G-2**（禁止引入内容合规/版权考量）**档位不变**（本轮 5/5）⇒ 只剩「入不入池」一个决定。**影响 1 行** |
| **Q-19** | ★**（2026-10-24 新增）腾讯《中国好声音》专题页的 4 个 iframe 是不是播放器**——**要现场重采** | 07 评估 / 08 存档 · 手册 §7.2.2 O-e | 存档只记「4 个 iframe（性质没核）」+ 元素被裁（80/200）⇒ **否定断言撑不住**（本轮记 4 档）。重采后：确认是播放器 ⇒ 落 `player_unverified`（未决、不入池）⇒ 结论变 ⇒ 2 档；确认无 ⇒ 5 档。**影响 1 行** |
| **Q-20** | ★★**（2026-10-26 新增）agent 路径那一整套口径**——**八个子问题，一并拍** | 02 感知 / 03 标签 / 05 rationale / 08 存档 · 本文 **§M-37 / §M-38 / §M-39 详解** · 本文 **§Q-20 详解（① ② 的「详情/问题/原因/可选做法」四段 + 实测读数）** · `executor/steps/agent_visit.py:134` · `executor/branches.py:370` · `executor/archive.py:316` · `perception/rule_perceptor.py::paywall_evidence` | **① agent 路要不要接前置事实核**（甲：接，先补判据 / 乙：不贴，全交人工 / 丙：单开一套 ⇒ **推荐甲**）· **② `paywall_evidence` 的「同句」放宽到多宽**（iqiyi 门禁句被 DOM 换行切开 ⇒ 现在判不出来；v.qq 的「完整版」在片名里 ⇒ 放宽会假阳——⭐ **2026-10-27 已坐实**：`_PAYWALL_UNIT_SPLIT` 按**换行**切句 ⇒ 存档 `T054__1c9b6260__site-5` 正文里「后结束，观看完整视频请」与「开通VIP会员」落进**两个句单元** ⇒ `paywall_evidence` 返回**空串**；**验收样例** = 改完判据后这段正文必须**从空串变成命中**）· **③ `login_required` / `blocked` / `partial_free` 的散文前置要不要升级成可执行判据**（不升级 ⇒ 甲只修一支）· **④ 池的去重键要不要带 `driver` / `run_id`**（影响存量 260+7 行的键空间）· **⑤ 产物要不要加口径轴**（M-39：现在只有 `rationale_era` 一条）· **⑥ youku / v.qq 那两条标签怎么办**（移出池 / 改判 `unresolved` / 留待人判——⭐ **2026-10-27 已现场重采完毕（camofox）**：v.qq 只有营销 + 广告位「VIP可关闭该广告」、播放器 02:09 短片 ⇒ `subscription_wall` **站不住**；youku 现场是「由于安全原因，请 登录 后继续观看」⇒ **登录墙** ⇒ **站不住** ⇒ **只剩 owner 的定性**——⭐⭐ **owner 已裁（2026-10-27）并已执行**：**v.qq 留池打标、改用预告片标 `trailer_only`**（落地页只有 2 分 09 秒短片，不是目标正片）＋ **youku 改判 `login_required`**（现场是登录墙）⇒ 走 `cli relabel --apply`：改判 **4 条记录**（v.qq 1 + youku 3）⇒ 池 325 行**数量不变**（`subscription_wall` 46→42 · `login_required` 9→12 · `trailer_only` 1→2），evidence 带「[改判 …]」且 `source=human`；⚠️ **同 URL 的第 4 行没改成**（`TPIN02` 那行，工具按 D-5 归属闸**主动拒了**）⇒ **仍挂 `subscription_wall`** ⇒ **要 owner 再说一句**（留 / 改 / 出池）——见 §Q-20 详解「附带开口 B」；⚠️ **另带出两条口径开口**：`trailer_only` 的**宽口径**（仓库的 `classify_trailer` 对 v.qq 判 `none`/`suspect`，**永远不会**产 `trailer_only`）＋ W2 放宽会把 owner 刚裁的 3 行 youku 判成「有付费墙证据」⇒ **② 与 owner 的定性打架**——见 §Q-20 详解「附带开口 A」）· **⑦ iqiyi 那条保不保**（⭐ **2026-10-27 复核**：存档 `T054__1c9b6260__site-5` 的正文**当场就有**「试看 05:44 后结束，观看完整视频请 开通VIP会员」+「99分钟」正片 ⇒ **就是同一个 URL**（不是另一个 URL），**标签当时是对的 ⇒ 方向是「保」**；但仓库自己的 `paywall_evidence` 对那段正文**也判不出来**（DOM 换行切开 ⇒ §Q-20 ②）⇒ **这条同时是补判据的验收样例**）· **⑧ 改词表要连带改 prompt + 重跑 A/B，批不批**（D-C7 义务，见 Q-08） | ① 不接 ⇒ agent 路径**产出的负标签一律没过核**，池里会持续混进营销/广告页（youku / v.qq 就是现成例子）· ② 放宽不足 ⇒ iqiyi 这类**真付费墙被假降级**（真负样本流失）；放宽过度 ⇒ 导航栏 CTA 复活成付费墙（v.qq 的「完整版」是片名）· ⑥ 直接改判 `unresolved` ⇒ 这两条**从池里消失**（`unresolved` 不入池）——**但那也是一次未经现场复核的定性**（`unresolved` 的含义是「没判出来」，不是「判为不是墙」）⇒ ⭐ **2026-10-27：现场重采已做完**（camofox 执行层，v.qq / youku **当场与今天都站不住**）⇒ **现在只剩 owner 的定性**（移出池 / 改判 `unresolved` / 留待人判）；**并池是写入时的动作，不回填历史** ⇒ 若改判要**新写一行**，不能改旧行 |
| **Q-21** | ★**（2026-10-27 新增）池里同一 `(task_id, url)` 挂两个不同的负分支**——**哪些分支对算「矛盾」？自动写入路径要不要也查？门禁要不要加一节？** | 03 标签 / 08 存档 · 本文 **§M-41 详解** · `executor/archive.py:316`（去重键含 `branch`） · `executor/pool_add.py::collect_candidates`（人工那条路拒并 `stale_branch`） · `branches.BRANCH_PRECONDITION`（前置事实那张表） | 实测：T004 的 `vo.youku.com` 同 task 同 URL，老 camoufox 批记 `component_unverified`、新 camofox 批记 `subscription_wall` ⇒ **两条互斥标签同在池里**，而门禁**不查「池行 ↔ 池行」**⇒ 全库 **16 对**看不见。⚠️ **不是所有多分支都是矛盾**：`blocked` vs `login_wall_or_blocked` 是**两层**拦截（不是）；`component_unverified` vs `subscription_wall` 是**同一层**的互斥结论（是）。**三条**：① **互斥分支对的那张表**放哪（建议落 `BRANCH_PRECONDITION`，那张表本来就管「贴标签前核前置事实」）· ② 自动写入路径要不要也查（不查 ⇒ 分歧从自动那条进来；查 ⇒ 要定「拒并还是并但打标」）· ③ 门禁要不要加一节「池行 ↔ 池行」。**不替人投票**：现在**一行都不动**（分歧摆上台面） |
| **Q-22** | ★**（2026-10-27 新增）池行 `golden_id` 的行号后缀要不要改成分支后缀？那 **6 条掉线**的人核标签要不要跟着改键？** | 07 评估 / 08 存档 · 本文 **§M-42 详解** · `evaluation/golden/builder.py::_scan_negative_pool`（`__neg{i:02d}`，`i` = 池文件行号） · `evaluation/golden/data/annotations.jsonl`（**6 条** `__negNNN` 标注） · `evaluation/golden/data/candidates.jsonl`（**旧扫描**，还挂得上） | 实测：id 里的 `{i}` 是**池文件里的行号** ⇒ 池一增删，后面所有行 id 前移/后移 ⇒ **6 条人核标注（6/6 全掉）静默失联**（池里 `neg151…neg160`，标注里 `neg152…neg161`，**整体差 1**；证据一条没少，**只有序号变了**）。⚠️ **现在还没炸**（入库的 `candidates.jsonl` 还是旧扫描，老序号还挂得上）⇒ **下一次重建候选集就炸**：这 6 条被 `site` 过滤剔掉 ⇒ **D10 分母 289 → 283**，报表上**看不出原因**。**两条**：① **后缀改成分支**（`__neg_{branch}`——分支在同 `(task,run,url)` 内唯一 ⇒ 天然去重；id 只依赖身份 ⇒ 池增删不动它）还是**另找一个身份口径**（如带上 `run_id`）？② **那 6 条标注的键要不要跟着改**（**标签内容一个字不动，只改键**）——**改键 = 动人核数据 ⇒ 要人签**；不改 ⇒ 下次重建就掉线。⚠️ **不替人投票**：现在**代码一行不改、标注一行不改**（键不改、缺陷摆上台面）。⭐ **2026-10-27 补**：**成因链已查实**（committed `candidates.jsonl` 的 10 条 `__neg` id 与现行 builder 逐条对 ⇒ **Δ ≡ −1**、10 条无一例外；builder 下标口径**没变过**；那 10 个 id **自 2026-10-14 起就是 `neg152`–`neg161`** ⇒ 它们算出来时池里**还留着 TPIN02 `yn-mg.cn/playid/71884` 那行**——**2026-10-22 撤掉它** ⇒ 后面全体 −1 ⇒ `candidates.jsonl` 没重建 ⇒ 标注挂不上）⇒ **不是代码改的，是池改的**。⇒ **本轮撤的 6 行都在池第 300 行之后 ⇒ 没制造新的掉队** ⇒ **本轮不重建**。⇒ ⚠️ **改口径会让全部 357 条候选的 id 全变** ⇒ **迁移期要双 id 并存**（旧 id 留一段，或建 `id_alias` 映射表）<br>✅ **2026-10-28 已闭环（owner 签）**：owner 裁「金标候选集重切（**v14**）」+「改键 = 动人核数据 ⇒ 需人签，owner 此消息即为签」⇒ **v14 = 键迁移版**已冻结（`evaluation/golden/data/v14/`）：后缀从**行号**换成**分支**（`__neg151` → `__neg_blocked`）⇒ id 只依赖身份，**池增删不再让后面所有行漂一位**；映射表 `data/id_alias.json`（**10 条**）⇒ **迁移期双 id 并存**（旧 id 留在 `golden_id_legacy`，历史版本仍读得懂）；**6 条人核标注只改键、标签一个字未动**；候选集重建 **357 → 2495 行**；**继承式冻结**（`--from-version 13` ⇒ 切分原样带过来、**回归集不重切**，C-17 不破）；**不给** `--refresh-code`（给了会怎样见 §M-48）。⚠️ **门禁要适配**：配对键改成**优先取 `golden_id_legacy`**（`calibration/verify.py::_entry_key` + `calibration/snapshot.py::_entry` 把旧 id 带进快照）⇒ 换键的那 6 行跨版本仍配得上（回归集里有 1 条）⇒ **不修就是 `VerifyError` 退 2 = 测不成**。**读数**：D10 头条 **106/128 = 82.8%**（与 v13 **相同**——键迁移不动内容）· 旧口径 230/289 = 79.6% · 门禁 v13→v14 **未配对 0/0**。⚠️ **门禁 verdict = 驳回（exit 1）**，原因是 `net_improvement` = 0 —— **纯键迁移版本什么都没试**，「净改善 0」在这类版本上**不是质量退步，是「没做事」** ⇒ 这是**类别不匹配**，**不靠放宽门禁「修掉」**（放宽就把真退步也放过了）⇒ 记在台账里让人知道。详见本文 **§M-48** |
| **Q-23** | ✅ **已闭环（2026-10-28 09:50）**：口径问题 2026-10-27 已裁定（随 Q-17 一并裁）；**「存量出池」那一半本轮做完**——池里 **54 条** `unreachable_hard` **逐条真浏览器复核** ⇒ **40 条出池**（降级 `unresolved` + 撤池行）、**14 条留池**（真·不可达机器码）⇒ 池里 `unreachable_hard` **54 → 14** | 03 标签 / 08 存档 · 本文 **§十一（读数）** · **本文 M-44（裁定 + 实施结果）** · `executor/branches.py::BRANCH_PRECONDITION` · `executor/browser/nav_failure.py::site_unreachable()`（**裁定后的判据单点**） · `research/probes/{camofox_q23_uh_recheck,camofox_q23_uh_settle,q23_dispose_uh_rows,q23_annotate_kept_rows}.py` | 第 5 轮**同一个 URL 两条分支**：`http://www.iqiyi.com/v_19rrk40at8.html…` 在 **T038（单独跑）**落 `unresolved`（站点遍历超时 >180 s ⇒ **页面打得开**），在 **T048（与另一条流并发）**落 **`unreachable_hard`**（导航 30 s 超时 ⇒ **进池**）；ixigua 同款。⇒ **导航超时证明的是「这次没打开」，不是「站点不可达」**（DNS 失败 / 连接被拒才是不可达）。⚠️ **并发只是候选成因**（站点限流同样是）⇒ **先单独重开那几条**：打得开 ⇒ **出池**（池第 **308 / 309** 行，出池前）；仍超时 ⇒ 事实成立。⇒ **更大的那条**：现池 **56 条** `unreachable_hard`（39 直站 + 17 百度跳转）**都受这条影响** ⇒ **要不要把「导航超时」降成 `unresolved`（不进池）**？**不替人投票**：代码一行不改、池一行不动，等 owner 裁定。<br>⭐ **2026-10-27 本轮已按铁律逐条单独复核完（第 5 轮那 5 条 + M-43 那 1 条 = 6 条，**全部证伪**）**：3 条**单独重开打得开**（T004 豆瓣片单 / T048 爱奇艺《功夫（粤语）》/ T048 西瓜视频《功夫》）＋ 2 条**跳转链活着**（E028 两条 baidu 链接：打断消息里点名落点 `www.ctwlpt.com/voddetail/38550.html` 与 `…/vodplay/38550-1-1.html`；三轮重开**交替**报 `NS_ERROR_NET_RESET` 与 `Navigation … is interrupted by another navigation to …`）＋ M-43 那条**失败在建页路由、导航根本没发出去**。⇒ **处置已执行**：**6 次降级 + 6 次撤池**（先 `cli downgrade` 后 `cli pool-drop`，`pool_dropped.jsonl` 有原行全文）⇒ **池 331 → 325**，`unreachable_hard` **60 → 54**（16 百度跳转 + 38 直站）。⇒ **那 56 条的系统性口径问题已于 2026-10-27 由 owner 裁定**（**降级规则**：超时 / 打断 / 建页失败 ⇒ `unresolved` 不进池；只留 DNS 失败 / 连接被拒 ⇒ `unreachable_hard`；**只影响新行** ⇒ **已实施**，见 **Q-17** 与 **M-44 实施结果**）⇒ ⭐ **2026-10-28「存量出池」那一半做完了（Q-23 闭环）**：**29 个 URL / 54 条池行**逐条**真开浏览器**复核（camofox 面；`lock_queue` 独占 ⇒ 与采集批次错开跑的）⇒ **共 125 次尝试**（① 轮 29 URL × ≤3 次 = 53 次；② 轮给**全部没打开的 12 个 URL** 统一加试 6 次 = 72 次——⚠️ **按「第一轮打不开」筛，不按「读数难解释」筛**，后者就是「按结果决定给谁加试」）⇒ **判据用的就是执行器自己那把尺**（`executor/steps/reachability.py::probe`：单次导航里**机器码**证明 DNS 解析不出来 / 连接被拒 ⇒ `unreachable_hard`；其余（超时 / 打断 / 空响应 / 建页失败 / 会话失效）⇒ `unresolved`）⇒ **裁定**：`opens` **17** 个 URL / **31 行**（打得开 ⇒ 「站点不可达」不成立）· `not_proven` **9** 个 URL / **9 行**（9 次尝试全是 `NS_ERROR_NET_EMPTY_RESPONSE` / `NS_ERROR_NET_RESET` / `SSL_ERROR_BAD_CERT_DOMAIN` / 超时 ⇒ **证明不了**）· `site_unreachable` **3** 个 URL / **14 行**（`NS_ERROR_CONNECTION_REFUSED` 出现过 ⇒ 站点性质成立）⇒ **40 条出池 + 14 条留池**。⚠️ **不投票**：机器码的**原始次数照录进 reason**（「×4 / ×5」——⚠️ 初版按**字段**累加写成「×8」，那是**同一次尝试的码在 `code` 与 `ns_error_codes` 两个字段里各记了一遍** ⇒ 已改成**按尝试去重计数**，**理由文本是审计记录，不能带这个错**），**不设复现率阈值** ⇒ 「出现过一次拒连」就是一次站点事实，与执行器单次导航同口径。⇒ **处置顺序照旧**（先 `cli downgrade` 后 `cli pool-drop`，**每条 dry-run 与 apply 的命中数必须 1:1**）⇒ **两层都落**（原档 + 复核档：门禁与报表读的是复核档，只改原档会当场报 `negative_not_pooled`）⇒ ⚠️ **降级必须两层都落**这条已进避坑指南 **§8.36**（只改原档 ⇒ 门禁读复核档 ⇒ 当场报 `negative_not_pooled`）。⇒ **留池那 14 行补了机器码读数**（原 evidence 是 obscura 面的旧文案 + **200 字截断** ⇒ `site_unreachable()` **命中 0 条** ⇒ 不补的话留池行与刚撤掉的行**长得一模一样**，审计链断在池文件里）。⇒ **门禁读数与基线逐项一致**（`negative_not_pooled` **4 → 3**、其余七项不变、`exit=1` 不变）。⚠️ **顺带浮出一条待人判的**：撤池腾空了 (T011, trtag) 的槽位 ⇒ 一条**人裁过的**真负样本（`confirm_not_play_site` · reesn）**这才第一次能并池** ⇒ 已并（池 **432 → 393**，并进去的是那条人裁的，不是新判的）。⚠️ **冻结的金标候选集因此多了 24 个指向空池行的键**（**实测**：`research/probes/gold_candidate_drift.py` ⇒ 候选集 357 行 / 322 个去重键；漂移**撤池前 183 键 / 204 行** ⇒ **撤池后 207 键 / 230 行**，★**本轮新增 24 键 / 26 行，全是 `unreachable_hard`**）⇒ **重切候选集要人签**（§8.32：改键 = 动人核数据）⇒ **本轮不动**。 |

### M-48 · 金标候选集重切 **v14（键迁移版）** —— 键不再随池增删漂移；但「刷新 code」会把 D10 打到 **64.8%**

**触发**：owner 2026-10-28 裁「金标候选集重切（**v14**）」+「改键 = 动人核数据 ⇒ 需人签，
owner 此消息即为签」⇒ **Q-22 闭环**（后缀从**行号**换成**分支** + 那 6 条标注按映射改键）。

**做法**（`evaluation/golden/builder.py`）：

| 环节 | 做法 |
|---|---|
| id 口径 | 池行 id 后缀 `__neg151`（**池文件行号**）⇒ `__neg_{branch}`（**分支**）⇒ id 只依赖**身份**，池增删不再让后面所有行漂一位 |
| 迁移期双 id | 映射表 `evaluation/golden/data/id_alias.json`（**10 条**）⇒ 冻结行带 `golden_id_legacy`（旧 id）⇒ **历史版本仍读得懂** |
| 人核标注 | **6 条**按映射**只改键**（`annotations.jsonl`）⇒ **标签内容一个字未动**（owner 签的就是这个） |
| 候选集 | 重建 **357 → 2495 行**（存档 + 池 + P2 样例全扫）；`cmd_candidates` 现在**拒绝**在「标注会挂不上」时重建（**exit 2**） |
| 冻结 | `freeze --from-version 13`（**继承式** ⇒ 切分原样带过来、**回归集不重切**，C-17 不破）· **不给** `--refresh-code`（见下） |
| 门禁适配 | 配对键 `calibration/verify.py::_entry_key` 改成**优先取 `golden_id_legacy`**；`calibration/snapshot.py::_entry` 把旧 id 带进快照条目 ⇒ 换键的那 6 行跨版本仍配得上（回归集里有 1 条）⚠️ 不修就是 `VerifyError` **退 2 = 测不成** |

**v14 读数**（`evaluation/golden/data/v14/`）：

| 项 | 读数 |
|---|---|
| 冻结 | v14 · 继承自 v13 · `seed=42` · 调参池 **131**（44.6%）/ 回归集 **60** / 持有 **103**（35.0%）· 回归集指纹 `3418ebd05a4c` |
| manifest | `id_migration.enabled=true` · `rows_rekeyed=6` · `code_refresh.enabled=false` · gold / label / score 三项 refresh 全 false |
| D10 头条 | **106/128 = 82.8%**（与 v13 **相同**——键迁移不动内容） |
| D10 旧口径（三档合计） | 230/289 = 79.6% |
| 门禁 v13→v14 | 头条**配对 128** · 回归集 **60** · **未配对 0/0**（换键的 6 行跨版本配得上） |
| 门禁 verdict | ❌ **驳回（exit 1）**：`net_improvement` = 0（修好 0 − 反转 0） |
| 测试 | **4418 passed** |

⚠️ **门禁那条驳回要人知道**：**纯键迁移版本什么都没试** ⇒ 「净改善 0」在这类版本上
**不是质量退步，是「没做事」** ⇒ 这是**类别不匹配**，**不靠放宽门禁「修掉」**
（放宽就把真退步也放过了）⇒ 记在台账里，不改门禁。

⭐ **如果当时给了 `--refresh-code`，会怎样**（实测，`research/probes/gold_refresh_code_impact.py`）：

| 项 | 不刷（v14 现状） | 刷 |
|---|---|---|
| D10 头条（128 条） | **106 = 82.8%** | **83 = 64.8%** ⇒ **−18.0pp，跌破 80% 上岗线** |
| D10 旧口径（289 条） | 230 = 79.6% | 188 = 65.1% ⇒ −14.5pp |
| pred 变了的行 | — | **62 条**：改好 **2** · 改坏 **44** · 仍不一致 **16** |

⚠️ **改坏的那 44 条分三族，没有一族是「刷新更准」**：

| 族 | 条数 | 成因 | 谁来判 |
|---|---|---|---|
| **A** | **11** | **人核当时就写明了「存档分支名走错」**（注记原话：「这几行的 D-10 金标就是 broken ⇒ 存档落 `login_wall_or_blocked` ⇒ **分支名走错**」）⇒ 刷进来 = 把**已知错**的分支名灌回 D10 | 不用人判——**别刷** |
| **B** | **22** | **金标陈旧**：owner 2026-10-28 的 **Q-23 现场真浏览器复核**已把「真·站点不可达」推翻（站点打得开 ⇒ 存档分支改判 `unresolved`、那批池行已出池），而**冻结金标仍写 `unreachable_hard`** | **要人改金标**（改标签本身，不是改键） |
| **C** | **11** | **口径漂移**：会员页在存档里落 `login_wall_or_blocked`（ACCESS_BLOCKED 类），金标落 `subscription_wall`（PAYWALL 类）⇒ **两个等价类** ⇒ D10 判它不一致 | **要人裁口径**（「会员 / 播放器报错」算付费墙还是登录墙） |

⇒ **结论**：v14 **不给** `--refresh-code` 是对的——code 是 pred 不是金标，**但今天重扫出来的
pred 是「全部存档的 branch 名」**：含旧代 obscura / LLM 感知层存档的旧命名，还含已被现场复核
推翻的行 ⇒ **它不是「更准的 pred」，是「今天的 pred」**。

**锚点**：`evaluation/golden/builder.py`（`_scan_negative_pool` / `cmd_rekey` / `cmd_freeze`）·
`evaluation/golden/data/id_alias.json` · `evaluation/golden/data/v14/` ·
`evaluation/calibration/verify.py::_entry_key` · `evaluation/calibration/snapshot.py::_entry` ·
`research/probes/{gold_candidate_rekey,gold_refresh_code_impact}.py` ·
`tests/unit/test_golden_id_rekey.py` · 编年史 §增量九十九 · 同族 **M-42** / **Q-22** / **H-02**

### M-49 · 爱奇艺同 URL **既判墙又判成功** ⇒ 本批 **+4 条 FATAL** —— 要人定性的不是「标签写错了」

**现象**：M-46 那批跑完，门禁 `negative_contradicted_by_success` **37 → 41（+4）**，
四条全是爱奇艺**同 URL** 既判墙又判成功：

| 池行 | URL | 分支 | 证据 | 同 URL 的「成功」记录 |
|---|---|---|---|---|
| E017 / 29682a60 | `iqiyi.com/v_19rrcuke28.html` | `subscription_wall` | 「paywall 二次拦截」 | **E017__039d3c57**（LLM 判 True：页面存在视频元素和完整播放器控件，**未见要求订阅后才能观看的明确提示**）——**同一次跑里，成功在前、墙在后** |
| E028 / 62ca8043 | 同上 | `subscription_wall` | 「paywall 二次拦截」 | 同上（**旧池行**，本批才被那条成功记录撞上） |
| T042 / 75a2643b | `iqiyi.com/v_19rr7q1u1o.html` | `subscription_wall` | 「paywall 二次拦截」 | **T042__75a2643b 自己**：**墙在前、成功在后**（LLM 判 True：页面包含可用的视频播放器与完整时长信息，**会员文案仅为推广**） |
| T042 / 1dd019f0 | 同上 | **`component_unverified`** | LLM 判 False：页面仅提供约 5 分 46 秒试看，观看完整视频需开通 VIP 会员，**属试看型付费墙** | 同上 |

⚠️ **这不是「标签写错了」**：同一个 URL 在**不同 run**（甚至**同一次跑的先后**）给出两种结论
⇒ 是**会话状态 / 站点行为差异**，不是标注笔误。

⚠️ **全库范围**（实测）：池 **407** 行里「**paywall 二次拦截**」共 **81 行**
（v.qq.com 25 · **iqiyi 23** · youku 20 · 其余 13）⇒ **这不是爱奇艺一家的毛病，
是「VIP 试看站」这一族**。爱奇艺池行共 **28** 行（`subscription_wall` 23 ·
`component_unverified` 3 · `not_play_site` 2）。

⭐ **要人定性的三件事**（**不投票**：一条成功记录 + 一条墙记录**不是两份独立证据**，
多数票在这里是假的）：

| # | 要人定什么 | 为什么机器定不了 | 判了之后动什么 |
|---|---|---|---|
| **①** | **这两个 URL 的页面，正片到底能不能看**——「试看 5 分 46 秒 + 需开通 VIP」算订阅墙还是可播？ | 三条读数**互相矛盾**（成功 2 · 墙 2 · 组件未验证 1），而且**全是模型 / 规则对页面的转述**，不是页面本身 | 需**现场真浏览器复核**（与 Q-23 同性质）。判「墙」⇒ 那 23 行留池、**5 条 success 记录要降级**（它们现在会当**正样本**进训练集）；判「可播」⇒ 那 23 行**出池**（`cli pool-drop`） |
| **②** | **金标粒度**：同 URL 的多头条目按**访问**定还是按 **URL** 定？ | 已有裁定 **Q-11「金标粒度按访问」** ⇒ 那么 FATAL 检查的前提「同 URL 一正一负 = 自相矛盾」**就不成立** ⇒ **这条 FATAL 判据本身要不要改**（改成「同一次访问内才算矛盾」）是**口径裁定** | 若改判据 ⇒ `executor/integrity.py::_flag_contradicted_negatives` 的配对口径要动 ⇒ **门禁项会变**（37→41 这类读数随之变） |
| **③** | **T042/1dd019f0 的 `component_unverified` 归错分支** | 按 `BRANCH_PRECONDITION`：`component_unverified` 的前置事实是「到了播放页**且没有可验证的播放组件**」——而**页面明说「需开通 VIP」⇒ 付费门禁成立 ⇒ 组件在不在不是判据** ⇒ 应落 `subscription_wall`（`partial_free` 也不对：表里「试看 N 分钟 / 前 N 集免费」明确归 `subscription_wall`）⇒ **这条是规则侧漏核**（LLM 路径的 payload 直接给了 `component_unverified`，前置事实没被执行到），**不是人核内容** | 人确认口径即可 ⇒ **代码按前置事实改判**（不需要重跑） |

⚠️ **连带**：① 若判「爱奇艺 = 订阅墙」，那两条 success 记录（`E017__039d3c57` /
`T042__75a2643b`）**必须降级**——它们现在会作为**正样本**进训练集（比负样本污染更直接）；
⚠️ **改判 = 动数据 ⇒ 需人签**。

**锚点**：门禁 `executor/integrity.py::_flag_contradicted_negatives` ·
`executor/branches.py::BRANCH_PRECONDITION` · 存档 `E017__039d3c57.json` /
`T042__75a2643b.json` / `T042__1dd019f0.json` / `E017__29682a60.json` / `E028__62ca8043.json` ·
`output/pipeline/negative.jsonl`（爱奇艺 28 行）· 编年史 §增量九十九 ·
同族 **M-41**（同 URL 多分支）/ **Q-11** / **Q-23** / **H-01**

| **Q-24** | ★**（2026-10-28 新增）机器码命中在**搜索引擎跳转链**上时，算不算「真·站点不可达」？** | 03 标签 / 08 存档 · 本文 **§M-47 详解** · `executor/steps/reachability.py::probe` · `executor/browser/nav_failure.py::site_unreachable()` · 存档 `E017__039d3c57.json` · 编年史 §增量九十八 §六 | 实测：M-46 批次里 E017 出了一条 `unreachable_hard`，**URL 是百度跳转链**、call log **只有一跳**、`landed_url` **为空** ⇒ **归因对象（那个影视站）从未被识别** ⇒ 「不可达」没有可指的对象，且池行 URL 是一次性跳转链（**本身无效**）。⚠️ 全库 `unreachable_hard` 共 18 条，跳转链的**只此 1 条** ⇒ 单点新形态。**甲（推荐）**：`probe()` 加「归因对象」护栏——跳转链且未解析出落点 ⇒ 不配记 `proven_unreachable` ⇒ 降 `unresolved`（不进池）；⚠️ 代价 = 机器码命中的「连接被拒」这个**站点事实**被丢掉 ⇒ 机器码原文要照录进 reason。**乙**：只出池这 1 条 ⇒ 治标，下批还会出。⚠️ **不替人投票**：本轮代码一行不改、池一行不动<br>⭐ **2026-10-28 批次跑完后的实测代价**（M-46 那批 9 个任务全跑完）：池 **393 → 407**（**+14 行**），**新增 14 行里 6 行（43%）是百度跳转链**（E017 ×2 `not_play_site` + T042 ×2 `not_play_site` 等）⇒ 全池 **41 / 407（10.1%）**的池行 URL 是跳转链（40 `not_play_site` + 1 `unreachable_hard`）⇒ **这不是「一条新形态」，是已经成规模的一面**。⚠️ **去重键拦不住它**：去重键是 `(task_id, url, branch)`（`archive.py::_append_negatives`）⇒ **同一个站换了一条跳转链，键就不同 ⇒ 又是一行新池行**（T042 那两行就是同一个百度知道帖子的**两个不同跳转 URL**）⇒ **M-47 有第二个面**：不只是「归因对象错了」，还有「**同一个站的记录会按跳转链的次数重复堆积**」⇒ 甲方案只治前者，后者要另治（跳转链本身：要么解析落点后按落点去重，要么不入池）<br>✅ **2026-10-28 已实施（owner 裁「甲」）**：`executor/steps/reachability.py::probe` 加「**归因对象**」护栏——被导航的 URL 是**搜索引擎的结果中转链**（新函数 `executor/steps/search.py::engine_redirect_engine`：**引擎 host 精确匹配** + 路由 path + `url=` 参数键，**不解密、不猜内容**）⇒ **不配记** `proven_unreachable` ⇒ 降 `unresolved`（**不进池**）；**机器码原文照录进 reason**（「确实拒连了」这个事实留在存档里，不随分支一起消失）。⭐ **回放实测**（`research/probes/q24_q25_guard_replay.py`，全库 **223** 份存档）：`unreachable_hard` outcome 共 **35 条**，URL 是中转链的**只此 1 条**（`E017__039d3c57` 的 baidu 链）⇒ **这条护栏当前只动 1 行**——但它是**先例**（同形态再出现就不进池了）。⚠️ **护栏只治「归因对象错」**，**不治**「同一个站换条跳转链就多一行池行」（去重键 `(task_id,url,branch)` 拦不住）⇒ 那一面见 §M-47，另治<br>⭐ **2026-10-29 · 第一次真跑读数**（批次 `guard-rerun-2026-10-28`，19 个 give-up 命中任务 · 串行 · 约 7.5 小时）：本批 outcome **287** 条 ⇒ 导航/会话类失败 **25** 条（**13** 会话失效档 + **12**「这次没打开」兜底档）⇒ **机器码证明不可达（`DriverUnreachableError`）= 0 条** ⇒ ⚠️ **Q-24 甲的触发条件一次没出现 ⇒ 护栏真跑触发 0 次**（**既没被触发、也没被证伪** ⇒ **它的真跑读数仍然是空的**）。落在**百度中转链**上的失败 **14** 条 ⇒ **全部落 `unresolved` ⇒ 中转链一行没进池** —— ⚠️ 但**那是被 M-44 更宽的两档兜住的**（会话失效 / 这次没打开），**不是**被 Q-24 甲的归因对象判据兜住的 ⇒ **「效果达到了」≠「护栏起了作用」**。⇒ 要等「机器码证明不可达 **且** URL 是中转链」这个形态真的出现，Q-24 甲才有真跑读数 |
| **Q-25** | ★**（2026-10-28 新增）「agent 自报放弃 / 预算耗尽」收口时，**要不要先查终局形态**（404 / 付费墙 / 环境阻断）再落 `unresolved`？** | 02 感知 / 03 标签 · 本文 **H-01**（重测读数） · 编年史 §增量九十八 · `executor/agent_loop.py::give_up`（:927） · `executor/agent_loop.py::close`（:948） · `perception/rule_perceptor.py::infer_access_defect` · `perception/rule_perceptor.py::classify_paywall` | 实测（**当前代** agent/camofox）：**40 条** `unresolved` 属于「**机器本可以判出来**」——**子族① 36 条**：页面**正文**里就有终局形态（`infer_access_defect` 命中，**全是 404 页**；正文**抓到了**、`degraded=[]`；规则词表里明写 `404 Not Found` / `nginx`，**注释举的例子正是 `kmslxsh.com`**）；**子族② 4 条**：agent 叙述里引用了门禁短语（`classify_paywall` 对那段叙述判命中）。**根因一条**：[`give_up`](../../executor/agent_loop.py#L927) **一律落 `unresolved`、不查任何判据**（[`close()`](../../executor/agent_loop.py#L948) 查 verdict 映射表 ⇒ 那条路查得到）⇒ **机器可判的形态被丢弃 ⇒ 真负样本不进池**。⚠️ **代际回退**：旧代（LLM 感知层）把同一个 404 判成 `broken` ⇒ **进池 9 行**；当前代 **25 次全 `unresolved` ⇒ 一行没进池** ⇒ 不是「一直不行」，是「换代后退了」。⭐ **子族② 的数完全取决于口径——这是本轮量出来的**（**同一条 `unresolved`，三种口径差 27 条**）：**规范判据 `classify_paywall` ⇒ 4 条** · **裸词表**（叙述里出现「付费墙/VIP/会员/订阅」）⇒ **29 条** · **「裸词表命中但规范判据不命中」⇒ 27 条**（几乎全是 E002 的爱奇艺/腾讯叙述：「VIP付费墙挡住了视频」「页面被会员付费墙遮挡」）⇒ **差在「判据用错了地方」，不是「词表太窄」**：`classify_paywall` 是**为页面正文写的**（判据形态 = CTA「开通会员」+ **同句**观看语境），拿它去判 agent 叙述（模型自述）**压根不该命中** ⇒ **乙方案不是「有风险」，是「判据用错地方」**（拿叙述当页面证据 = 把词表注释自己警告的坑再踩一遍：注释明写「LLM evidence 是『结论+证据』混合，『视频不存在』经常是**模型自述结论而非页面文案**」）⇒ **子族①（36 条）是唯一「机器本可判」的族**；子族② 要修得**另设口径**（人核 / 二次观察 / agent 自报带引文），**不是放宽 `classify_paywall`**。**甲（推荐·只修子族①）**：收口前对**最后一页正文**跑一次 `infer_access_defect`，命中就落那条分支（404 ⇒ `broken`），不命中仍落 `unresolved` ⇒ **只动机器可判的形态，且只用页面自己的正文**；⚠️ 代价 = `infer_access_defect` 的**假阳**（正文出现「Not Found」等词但页面正常）会把正常页判成 `broken` ⇒ 要配前置事实核验。**乙**：子族②也修 ⇒ ⚠️ **不建议**（见上：判据用错地方；且叙述是模型自述）⇒ 要修得另设口径（人核 / 二次观察 / agent 自报带引文）。**丙**：不改，接受「没确认播放 ⇒ 没结论」。⚠️ **不替人投票**：本轮代码一行不改、池一行不动<br>✅ **2026-10-28 已实施（owner 裁「甲」）**：新函数 `executor/branches.py::terminal_shape_branch` —— 收口前对**最后一页的正文**跑一次 `infer_access_defect`，命中就落那条分支（404 ⇒ `broken`）、不命中仍落 `unresolved`；**只用页面自己的正文**，**agent 叙述不作为证据**（子族②不修，理由见上：判据用错地方）。**两道护栏**：① **前置事实**（`broken` 是**进池**的分支 ⇒ 判错就是**训练信号被污染** ⇒ 要「**零交互元素 + 零 video 标签 + 无降级项**」的**结构事实**，不靠词表也不靠长度阈值）② **归因对象**（agent 能 `goto` **任意 URL** ⇒ 收口时它看着的那一页**可能压根不是候选站** ⇒ 主机对不上就落 `unresolved`；⚠️ 比主机要拿**解析后的候选站**比、**不是**拿候选 URL 比——候选 URL 常是搜索中转链，拿它比会把**全部真阳性**判成「不明」）。接入点两处：`agent_loop.py::give_up`（agent 自述放弃）与 `agent_loop.py::_close_by_page`（**步数 / llm_calls 预算耗尽**——那两处 agent **真的把这一页看过**了）；⚠️ **「后端未作答」「动作指令连续解析失败」那两处不动**（那是**我们自己的故障**，算成页面负样本会让池里混进「我们坏了」的样本，还会盖掉「该重跑」这个信号）。⭐ **回放实测**（同上探针，全库 **223** 份存档）：「agent 放弃收口」的 visit 共 **210 条**（**207** 自述放弃 + **3** 预算耗尽）⇒ **护栏命中 30 条（去重 8 个落地页）**：**28 `broken`**（404 / nginx）＋ **2 `blocked`**（地区限制）；**12 条「归因对象不明」** ⇒ 仍落 `unresolved`；**168 条不命中**（其中 **5 条正文命中但前置事实不成立**——`degraded=['press_unsupported']` ⇒ 正文不是完整页面 ⇒ **降级而不是改判**）⇒ 仍落 `unresolved`。⚠️ **捞出来的那 30 条仍要人核**——护栏只保证「机器**可判**」，不保证「判**对**」<br>⭐ **2026-10-29 · 第一次真跑读数**（批次 `guard-rerun-2026-10-28`，19 个 give-up 命中任务 · 串行 · 约 7.5 小时）：本批「agent 放弃收口」的 visit **79** 条（**78** 自述放弃 + **1** 步数预算耗尽）⇒ **护栏判据命中 17**（**15** 落 `broken` + **2** 前置事实不成立降级 `unresolved`〔`degraded=['press_unsupported']`〕）+ **6** 条归因对象不明 + **56** 条不命中 ⇒ **护栏真跑触发了**（落在 **4 个落地页**：kmslxsh 12 · wanminggangguan 2 · xacjs 1 · dinggetv 1）。**15 条 `broken` 全部在池里**。池 **407 → 437（+30）**，那 **30 行全是本批 run 写的**（按 `run_id` 反查）——其中 **9 行**是护栏落的 `broken`，其余 21 行是本批的其它判定 ⇒ ⚠️ 那 15 条里有 **6 条**撞旧代老行被去重（`source=llm` · T018 · T025 · T030 · T044 · T048 · T054）⇒ **池里留的是旧代 LLM 的 evidence**，看池行看不出来源是护栏。⭐ **上线前后差**（同 task_id + 同 url，两批都有记录 183 条 ⇒ 分支不同 **57** 条）里**只有 15 条是护栏效应**（全是 `unresolved` → `broken`）⇒ 其余 **42 条混着站点漂移**，**不能算在护栏头上**。⚠️ **门禁当场红了**：Q-25 甲落 `broken` 时 **question 恒为空**（代码层确定性事实核）⇒ 是一类**新的「无题产地」**，而 `d5_scorer.QUESTIONLESS_PAIRS` **漏登记** ⇒ `test_d5_scorer.py::Test同源` 把 `("", "broken")` 判成「映射外的组合」（`1 failed, 4417 passed`）⇒ ✅ **已修**（**从执行层推导**，不手抄：`executor/branches.py::TERMINAL_SHAPE_BRANCHES` = `{ACCESS_DEFECT_BRANCH[k] for k in ACCESS_DEFECT_KINDS}` ⇒ {blocked, broken}；`d5_scorer._terminal_shape_questionless_pairs()`）⇒ 门禁 **4418 passed**。补登记后复算：**15 条全 5 分**，且 5 分是**证据核从页面正文独立复核**出来的（不是读护栏的理由文本）⇒ ⚠️ **但 5 分只说「标签与页面一致」，那 15 行仍要人核**——3 件待裁见 [编年史 §增量一百 §五](90-增量编年史.md#增量一百--两条护栏的第一次真跑guard-rerun-2026-10-28--19-个任务--q-25-甲真命中-15-条--q-24-甲一次没触发--顺带撞出同-url-出两支)：**E021** 的 host 级 403（`broken` 进池 vs `blocked` 不进池；⭐ 现场取证：该 host **5 个路径全是 403** ⇒ host 级，留痕 `research/probes/q24_q25_guard_host403_probe.txt`）、**E014** 同 URL 两代分歧（池说 broken / 最新存档说 unresolved）、**T011** 同 URL 出两支<br>⭐ **2026-10-29 · 第 3 件（T011）已裁并已实施**（owner 裁定「改判成 `broken` 再并池」）：⚠️ **`cli relabel` 改不了 `unresolved`**（它只取**负样本分支**，`unresolved` ∈ `NON_SAMPLE_BRANCHES`）⇒ **唯一合规路径是人工复核通道**（手册 §4.5 · `cli review --verdicts … --apply`，**落盘时自动并池**）。⭐ **现场取证**（留痕 `research/probes/q24_q25_guard_kmslxsh_probe.txt`）：该 URL · `/v/17807/…` · 站点根 **3/3** 返回「404 Not Found\nnginx」、`elements_total=0` · `video_tag_count=0` · `degraded=[]` ⇒ **前置事实成立** ⇒ 当初那条观察若没带 `press_unsupported`，护栏本就会落 `broken` ⇒ 人核的是「**站点确实死了**」这个**站点事实**，不是「护栏判得对」。⇒ `T011__78621699`：`unresolved` → **`broken`** · `source=human` · `branch_before_review=unresolved` 保留；池 **437 → 438**；P2 样本 `T011__78621699__site-10.json` 同步（`branch=broken` · `question=""`）。⚠️ **执行这一条撞出两个机制缺口（都修了，各有测试）**：① **`review_queue._fits` 对无题产地一律拒** ⇒ **Q-25 甲落的 `broken`（`question` 恒为空）人裁通道够不着**（执行层认的组合人核层不认 = 两本规则书）⇒ 改为**只**给 `TERMINAL_SHAPE_BRANCHES`（`broken`/`blocked`，**从 `branches.py` 导入不手抄**）开口子，**判断点类分支空 question 仍拒**；✅ **owner 已追认（2026-10-29）**：这两处机制改动是**执行这一条裁定的必要条件**（改的是**复核通道的准入与范围**，**护栏判据一行没动**）；② **`review --apply` 不加限定会把每份原档重算成复核档** ⇒ **2026-10-24 那轮 78 份人裁定会被原档静默覆盖** ⇒ 改为**读生效档**（有 `.reviewed.json` 就以它为底）+ 新增 **`--only`** 钉死范围（**裁第二轮必须用它**）。⇒ **残余**：同 URL 另有 **27** 条 `unresolved` 分布在 **20** 个任务上（**全裁只新增 6 行**池行：E028 · T012 · T028 · T033 · T036 · T040；另 21 条撞键去重）⇒ 全裁后 kmslxsh 的 **50** 条 outcome 行里 **47** 条落 `broken`（另 3 条 `login_wall_or_blocked` 不变、`unresolved` 清零）、**T011 同 URL 两支**消除 ⇒ **待 owner 再裁**（本轮授权只覆盖 T011 这一行）。⚠️ **门禁退出码更正**：`cli check-archives` 实测 **exit 1**（46 条 `negative_contradicted_by_success` 是 `Severity.FATAL` ⇒ `report.ok=False`）——此前 §增量一百 记的「exit 0」是**量错了**（`$?` 取的是管道末端 `tail` 的状态）；那 46 条**全是存量**（`subscription_wall` 32 · `partial_free` 13 · `component_unverified` 1）⇒ **0 条**落在 `broken` / kmslxsh 上；归档级另有 **3 条 `negative_not_pooled`（DEGRADED，非 FATAL）**＝ `pool-add` 干跑那 8 条里的 3 条（E021 被判成功**矛盾** / E023 · T054 池内同址**已有别的分支**）⇒ **存量的人工分歧，不是本轮引入**<br>⭐ **2026-10-29 · E021 已用浏览器实看，并立了「换面会不会变」这把尺子**（owner：「人工用浏览器看一眼，给出完整 url」）：**完整 url** = `https://m.xacjs.com/weihu/1416804.html`（全库 `m.xacjs.com` 只有这 **1** 个站点地址；两条 run 的跳转链 `http://www.baidu.com/link?url=fF_mLv2j_CGIezOu0HDKjCUXNi-A4CGCwu2Mjv0oxuNwjw-fNI_xPjOI2XhyakIr` / `http://www.baidu.com/link?url=DXsjd367aDwviTqDGDsY6rQWXVhhSn5fWgdj3mg0G1jcoxhSjzGl30Y5JEwzxLMR`）。**生产面实看**（camofox，**5** 个路径）**5/5 = 「403 Forbidden / nginx」**、元素 0 · video 0 · `降级=[]`（留痕 `research/probes/q24_q25_guard_host403_recheck_2026-10-29.txt`）⇒ 前置事实成立。⭐ **新工具：面依赖检查**（`research/probes/q24_q25_guard_facecheck.py` —— 同一台机器、同一出口 IP，**只换请求头**）：`chrome`/`bare`/`referer` 三种客户端 × **7** 个 xacjs URL ⇒ **21/21 HTTP 403**；⚠️ **同机同出口的对照站 `m.dinggetv.com` 同刻 200 OK** ⇒ 403 是**站点对本机出口**的拒绝、不是本地网络问题（留痕 `…_facecheck_xacjs_2026-10-29.txt`）⇒ **判读口径三选一**：**换面不变** ⇒ 站点的事实（真负样本成立）· **换面变了** ⇒ **我们这个面的事实**，不能外推成「站点看不了」· **换面仍是拒绝** ⇒ **始终没看到站点页面** ⇒ fail-closed、**不是**真负样本。⭐ **把池里全部 10 个 `broken` 唯一 URL 都查了一遍**（**30** 次请求 · `…_facecheck_pool_broken_2026-10-29.txt`）⇒ kmslxsh ✓ / wanminggangguan ✓ **不变（站点事实）**；`v.qq.com/error.html`·`bilibili.com/error.html`（URL 本身就是错误页）✓；`104.143.94.74/guocanju/wulinwaichuan/`·`bilibili.com/bangumi/play/ep693415`（普通客户端 200，但裁的是 `player.ok` 级「媒体解不了」）**不是反证** ⇒ **两行站不住**：`m.xacjs.com/weihu/1416804.html`（403 ⇒ fail-closed ⇒ `broken` 不成立）、**`m.dinggetv.com/dingge/84421.html`（E014 —— 普通客户端拿到 HTTP 200 + **80 KB** 真页面《…大话西游之大圣娶亲国语版…免费在线观看全集》⇒ 旧代那条 `broken` 是**面产物** ⇒ ⚠️ **先前「E014 不用动」的结论是错的**，那是没换面的前提下说的）**。⇒ **两条待 owner 批的处置**（E021 与 E014 出池）：`cli downgrade`（→ `unresolved`，理由进 evidence）＋ `cli pool-drop`（写审计日志）——⚠️ **复核通道够不着**：`apply_verdicts` 只对 `REVIEW_BRANCHES` 生效，`broken` ∉ ⇒ 那行会被原样忽略；`cli relabel --to blocked` 又拒（`blocked` 是采集侧分支、不是真负样本）⇒ **出池两步是唯一合规路径**。⇒ **判据层还开着一个口**：`ACCESS_DEFECT_KINDS` 里**没有 403**（`region`/`captcha`/`anti_bot`/`not_found`）⇒ 403 错误页含 `nginx` ⇒ 被 `not_found` 旁证命中 ⇒ 落 `broken`（**采集侧的东西进了真负样本池**）⇒ **今天再跑一遍 E021 还会再写一行错的** ⇒ 「加一类 `forbidden` ⇒ 403 落 `blocked`（不进池）」是**另一个 owner 决定**，**判据一行没动**。⇒ **三件的人核状态（截至 2026-10-29）**：**T011 已裁已实施**（见上）· **E021 已取证、待 owner 批出池** · **E014 已翻案、待 owner 批出池** ⇒ 详见 [编年史 增量一百 §八](90-增量编年史.md) |

---

## Q-20 详解 · ① 与 ②（owner 2026-10-27 要的「详情 / 问题 / 原因 / 可选做法」四段）

> **为什么单开一节**：§M-37 详解记的是**机制**（缺什么、为什么缺），
> 而 Q-20 ① / ② 要 owner 拍的是**要不要修、修多宽** ⇒ 要的是**取舍账**，不是机制复述。
>
> ⭐ **本节的读数全部是实测**（§M-37 详解原话：「**放宽幅度只能靠实测定，不能靠论证定**」）
> ⇒ 复算脚本 `research/probes/paywall_widening_measure.py`
> ⇒ 读数落盘 `research/probes/paywall_widening_measure.json`
> （**1160 段存档正文** + **22 行**金标候选；**不联网、不含凭据**）。
>
> ⚠️ **代码一行不改、池一行不动**——本节只摆事实与做法，**等 owner 裁定**。

### ① agent 路要不要接前置事实核

#### 详情

**同一个账本，两条记账路径，前置事实核只有一条走到**：

| | scripted 路 | **agent 路** |
|---|---|---|
| 入口 | `judge_player` → `ledger.record_from(decision)` | `executor/steps/agent_visit.py:134` 裸 `ledger.record(url, outcome.branch, reason, site_url=url)` |
| 带 `decision=` 吗 | 带 ⇒ `question` 有值 | **不带** ⇒ `question=""` |
| 走 `_precondition_failed` 吗 | **走**（`executor/branches.py:370`，全仓**唯一**可执行调用点） | **不走** |

**agent 路的标签是闸门给的**：`executor/agent_loop.py:455` 的 `_branch_of(verdict)` 查
`_FALLCLOSED_BRANCH` 表 ⇒ 判据是 `executor/gatekeeper.py::is_paywall` / `is_login_wall`
⇒ `perception/rule_perceptor.py::has_strong_paywall_signal`（**强弱信号表**）。
那个函数的 docstring 自己写着「⚠️ **不用于贴分支**」⇒
**拦截口径的结论被直接当成分支标签贴了出去**。

**盘上现状**（2026-10-27 实测，池 325 行）：evidence 带「**二次拦截**」的 **30 行**
⇒ 这 30 行**全部**走这个绕过口：

| 分支 | 行数 | `source` | 备注 |
|---|---|---|---|
| `subscription_wall` | 20 | `rule` | 机器贴的 |
| `login_required` | 6 | `rule` | 机器贴的 |
| `login_required` | 3 | `human` | 2026-10-27 owner 改判来的（evidence 仍留原「二次拦截」字样） |
| `trailer_only` | 1 | `human` | 2026-10-27 owner 改判来的（同上） |

⇒ **仍由机器贴出来的 26 行** = 池的 **8.0%**；改判过的 4 行分支已换、字样还在。

#### 问题

1. **agent 路径的负标签一律没过核** ⇒ 池里混进「营销页 / 广告页」——
   v.qq 那条现场是广告位 + 会员营销，youku 那条现场是**登录墙** + 续费营销，
   而这两条在 2026-10-27 之前**都标着 `subscription_wall`**。
2. **同一个池里两种口径混存**（scripted 过的、agent 没过的）⇒
   从产物上看不出来（`source` 都是 `rule`），门禁里也**没有任何一节在查这件事**。
3. **`BRANCH_PRECONDITION` 里 `login_required` / `blocked` / `partial_free`
   三条只有散文、没有代码** ⇒ 这三条分支**在哪条路径上都没有核**
   ⇒ 甲方案**只接得动 `subscription_wall` 一支**（③ 是它的前置）。
4. **接核的时点被 ② 卡死**：今天直接把 `_precondition_failed` 接到 agent 路，
   iqiyi 那条**当场**带教科书门禁句的正文会被**假降级**成 `unresolved`
   （真负样本流失）⇒ **必须先补判据**。

#### 原因

1. **「无题」被当成了「免检」**：agent 路径设计上没有判断题（探索权在 agent runtime）
   ⇒ `question=""` ⇒ 而 `_precondition_failed` 恰恰**挂在有题的那条记账入口上**
   ⇒ 「不贴题」顺带把「不核事实」也免了。
   ⚠️ **没有任何成文裁定说过 agent 路免检**——这是**默认行为**，不是**决定**。
2. **闸门那条路被设计成可以直接给分支**：
   `GATEKEEPER_DECIDED_BRANCHES = {login_required, subscription_wall, blocked}`
   （契约测试 `tests/contract/test_questionless_producers.py` 把它钉死了）
   ⇒ 「谁给标签」与「标签有没有核」被拆成两件事，而第二件**没人做**。
3. **口径有两套且从没被强制对齐**：
   `has_strong_paywall_signal`（强弱信号，答「这页**像**付费墙吗」，用来**拦页面**）
   与 `paywall_evidence`（门禁语境，答「这页**是不是**付费墙」，用来**贴分支**）
   ——**同一个仓库里两套口径**，`rule_perceptor` 模块头那段注释自己写着
   「这是这类错判能活这么久的原因」。

#### 可选做法

| | 做法 | 代价 | 实测读数 | 判读 |
|---|---|---|---|---|
| **甲** | 记账前先跑一次前置事实核，不成立降级 `unresolved`（把 `agent_visit.py:134` 那处裸 `record` 改成走一个「先核再记」的入口） | **必须先补判据**（②），否则假降级 iqiyi；且 **③ 不做就只修一支** | 「二次拦截」那 30 行在 ② 的 **W2** 下：**14 行活下来 / 16 行降级**（降级里有 owner 已裁的 v.qq 广告页 5 行 ⇒ 降级**对**；也有 bilibili 登录墙 3 行 ⇒ 若 ③ 误用付费墙判据去核 `login_required` 会**假降级**） | ⭐ **推荐**，但**序不能乱**：① → ② → ③ |
| **乙** | agent 路**不贴负标签**，一律记 `unresolved` + 把闸门判定原文写进 evidence ⇒ 全部交人工 | agent 路**产不出负样本**（采集量掉一半）；`unresolved` **不入池** ⇒ 池只能靠 scripted 路长 | 同上 30 行**全部**降级 | ❌ **不推荐**：池的意义是「这里真的看不了」，而 `unresolved` 的含义是「**没判出来**」，不是「判为不是墙」 |
| **丙** | 承认 agent 路**单开一套**「强信号即可贴」的口径 | 放弃「贴标签前核前置事实」这条纪律 | 「强弱信号」在 youku / v.qq 上**已经误判**（这两条就是它贴出来的）⇒ 丙等于把**已知错误制度化** | ❌ **明确不推荐** |

**验收判据**（沿用 §M-37 详解那三条，**补**第 4 条）：

1. iqiyi 那条**仍然成立**（不许假降级）；
2. youku / v.qq 那两条**不再成立**（降级 `unresolved` ⇒ 不进池）；
3. 金标里已判 `subscription_wall` / `login_required` 的行**不掉**（词表改动不许伤旧金标）；
4. ⭐ **补**：「二次拦截」那 **30 行逐条**在新判据下重跑，出一张「活下来 / 降级」的对照表
   ⇒ **不替人投票**：活下来的仍要人核一遍——**尤其那 14 行里 owner 刚裁成 `login_required`
   的 3 行 youku**（**判据说订阅墙、owner 说登录墙**，见下节 ② 的「附带开口 B」）。

### ② `paywall_evidence` 的「同句」放宽到多宽

#### 详情

**判据真源**：`perception/rule_perceptor.py::_find_paywall_hit`，三层：

- **L1** `PAYWALL_GATE_PHRASES`（门禁短语本身是完整命题 ⇒ **无条件命中**）；
- **L2** `PAYWALL_CTA_PHRASES`（CTA）**必须配同句**的 `PAYWALL_CONTEXT_WORDS`
  （观看语境词）⇒ 裸 CTA（「开通会员」独行）**不算**；
- **L1.5** `PAYWALL_ERROR_GATE_PHRASES` + `PAYWALL_ERROR_CONTEXT_WORDS`
  （播放器报错 + 会员电商信号**同页同现**）。

**「同句」是怎么定的**：`_PAYWALL_UNIT_SPLIT = re.compile(r"[\n\r。！？；;!?]+")`
⇒ **换行也算切句符**；`_context_outside` 要求语境词落在
**CTA 所在的那个句单元内**（且不在 CTA 短语自己的跨度内）。

⭐ **实测**（存档 `T054__1c9b6260__site-5` 的**当场**正文，DOM 拼出来就是这样）：

```
试看
05:44
后结束，观看完整视频请
开通VIP会员
功夫（粤语） 99分钟 客户端流畅播放 …
```

⇒ 「观看完整」与「开通VIP会员」被**换行**切成两个单元
⇒ `paywall_evidence` 返回**空串**
⇒ **教科书级门禁句判不出来**（`PAYWALL_CTA_PHRASES` 里「开通vip」本来就有，
缺的只是「同句」）。

#### 问题

1. **真阳性漏判** ⇒ 甲方案的验收判据 1 直接过不了（iqiyi 会被假降级）。
2. **「同句」这个要求本身被 DOM 节点边界打败**：真实页面里一句门禁命题
   会被拆成 2–4 个短节点——**不是 iqiyi 一家**
   （全库实测 1160 段正文，基线只命中 **79 段 / 65 个文件**）。
3. **放宽有反向代价，而且代价不均匀**：
   - v.qq 的语境词「完整版」来自**片名**「三傻大闹宝莱坞完整版」，
     与导航栏 CTA 隔十几个换行 ⇒ 窗口开大就会假阳；
   - 「**纯续费营销**」按 `BRANCH_PRECONDITION` 明写**不是**付费墙
     （人工复核 2026-10-21：池里 12 条 `subscription_wall` 没有一条站得住，
     成因全是 CTA 裸命中与地区限制）
     ⇒ 任何把「续费 + 全集/正片」凑到一起的放宽，都会把营销页复活成付费墙。

#### 原因

1. **切句单位选的是「渲染单元」（DOM 节点边界）而不是「语义单元」**——
   一句门禁命题在 DOM 里是 2–4 个节点，在人读起来是一句。
2. L2 的设计前提是「同句里既有 CTA 又有观看语境 ⇒ 是门禁」，
   本意是**排除裸 CTA**（导航栏广告）；
   DOM 一拆，**这个排除手段与真阳性被同一个原因打中**
   ⇒ 排除手段失效不是设计错误，是**粒度选错**。
3. **词表本身没问题**——缺的不是词，是「CTA 与语境词在哪儿挨着」的规则。

#### 可选做法（三个候选，只改**粒度**、**不动词表**）

> ⚠️ **词表一改就是口径变更** ⇒ 按 D-C7 要连带改 prompt + 重跑 A/B（⑧）。
> 下面三个都**只动粒度**（怎么切句 / 语境词允许落在哪），**绕开这条义务**。
> 复算：`research/probes/paywall_widening_measure.py`
> ⇒ 读数：`research/probes/paywall_widening_measure.json`

| | 做法 | 新增命中（文件） | 丢失命中 | iqiyi 验收 | youku / v.qq 验收 | 判读 |
|---|---|---|---|---|---|---|
| **基线** | 现行：按 `[\n\r。！？；;!?]+` 切句，语境词须**同单元** | 79 段 / 65 文件 | — | ❌ **空串** | ✅ 空串 | — |
| **W1** | **换行不算句界**（只在句读处切）⇒ 整页变成大块 | **+36** | **0** | ✅ 命中 | ✅ 空串 | ❌ **不推荐**：多出来的 36 个里 **30 个**是 youku(v-wb) 与 ixigua，命中词全是「**打开app观看** + 看完/完整内容」「**续费** + 全集/正片」⇒ ① 把「纯续费营销」复活成付费墙 ② 顺带把 **Q-16**（「打开App观看」≡「打开App看完整内容」吗，**还挂着**）**一起答了** |
| **W2** | **邻窗 ±1**：保留现行切句，语境词允许落在 CTA 单元的**相邻单元** | **+6** | **0** | ✅ 命中 | ✅ 空串 | ⭐ **推荐** |
| **W3** | 邻窗 **±3** | +6 | **0** | ✅ 命中 | ✅ 空串 | 与 W2 **完全相同**（多带 **0** 个）⇒ **窗口旋钮在 ±1 就饱和**，不必开更大 |

> ⚠️ **「youku / v.qq 验收」列只针对那两个验收样例**
> （`T054__1c9b6260__site-8` 与 `E022__01fcb42b__site-9`）——
> **同一个 youku URL 的别的存档**在**基线**下**就已经**命中「有付费墙证据」
> ⇒ 见下面「附带要求 2」，别把这一列读成「W2 下 youku 全都干净」。

**W2 那 6 个新增命中是什么**（逐个读过正文）：

| 文件 | 页面 | W2 命中词 | 判读 |
|---|---|---|---|
| `T054__1c9b6260__site-5` | iqiyi `v_19rrk40at8` | 开通vip + **观看完整** | ✅ **验收样例本人**——命中词正是被换行切开的那句「后结束，观看完整视频请 / 开通VIP会员」 |
| `E014__6761bdce__site-2/3` | iqiyi `v_19rrkqj66k` | 开通vip + **观看完整** | ✅ 同款门禁句 |
| `E028__62ca8043__site-3` | iqiyi `v_19rrcuke28` | 开通vip + **观看完整** | ✅ 同款 |
| `T057__058fb16a__site-8` | iqiyi `v_1nvdvo4f38k` | 开通会员 + **解锁** | ⚠️ 「解锁」取自**营销行**（形如「解锁会员权益」）⇒ **结论对、证据指错** |
| `E028__62ca8043__site-6` | mgtv `/b/328192/6609060` | 开通vip + **解锁** | ⚠️ 同上：真门禁句是「**免费试看5分钟，会员可看完整版**」，但 W2 的证据指向「免前贴片广告，解锁会员权益」那行 |

⇒ **W2 带来两个附带要求**：

1. **evidence 必须带原句**（现在只写「命中哪个短语 + 哪个语境词」，
   人**核不了指错行**）⇒ 属 ③「散文升级成可执行判据」那一步的**产出格式**要求；
2. ⭐ **「同一个 youku URL 分不开」的根因是 ③，不是 ②**——**基线**就已经把那个 URL
   判成「有付费墙证据」了，**W2 一个都没加**（W2 的 6 个新增全是 iqiyi/mgtv）：
   - **该 youku URL 的 5 份**存档正文含「**试看6分钟 / 开通会员看完整视频**」那段 ⇒ CTA「开通会员」与
     语境词「**看完**」落在**同一单元**（「开通会员看完整视频」）⇒ **基线**就命中；
     其中 **4 份**对应池里的 `partial_free` 行（evidence 记的是代码层事实：存在 `<video>` ×1）。
   - owner 刚裁成 `login_required` 的 **3 份**存档（run `1c9b6260` / `cd571143` / `a933bea6`）
     正文**没有**这段（读数**空串**）⇒ owner 现场重采看到的是
     「**由于安全原因，请 登录 后继续观看**」那一版。
   - ⇒ **同一 URL、两种渲染形态、两种读数** ⇒ 那个 URL 才会同时挂上 `login_required`
     （owner 的裁法）与 `subscription_wall` / `partial_free`（机器判、更早的人判）。
   - ⇒ ⇒ **② 不能单独上**：只要 ③ 没给 `login_required` 一条**自己的**可执行判据，
     「登录墙 + 会员营销」的页面在付费墙判据下就**同时**成立 ⇒ ② 放宽到多宽都会撞上这个洞。
   - ⚠️ **顺带暴露一个词表级事实**：语境词「**看完**」是在「开通会员看**完**整视频」里
     **跨词边界**命中的（「看」+「完」）⇒ 语境词表有隐形的**子串**风险
     （属 ③ 的范围，**不是** ② 的粒度问题）。
   - ⇒ 这也解释了**为什么 ② 与 ① 要一起拍**：① 甲的落地效果**取决于**② 放宽到多宽，
     而② 放宽到多宽又**取决于**③ 的分支判据边界。

**金标回归**（金标候选集里判 `subscription_wall` / `login_required` 的 **22 行**）：

- 基线命中 **10** 行 ⇒ W1 / W2 / W3 下**丢失 0 行**（放宽是**超集**）✅；
- ⚠️ 但「基线只命中 **10/22**」本身也是读数：另 **12 行**在基线下正文里
  就抓不到门禁证据 ⇒ 那 12 行当初是**人判**的（人工看页面）
  ⇒ 它们是**判据的召回缺口**，不是误判。

### ⚠️ 两条要 owner 补裁的附带开口（①② 带出来的，不是它们本身）

**开口 A · `trailer_only` 的口径比分支名宽**（owner 2026-10-27 用它给 v.qq 打标时带出来的）

- owner 的定性：「落地页只有 2 分 09 秒短片《三傻大闹宝莱坞6》，不是目标正片 ⇒ 用预告片标」。
- 但仓库自己的口径：`classify_trailer` 判的是**控件文本**（短标签），
  `TRAILER_KEYWORDS` = 预告片 / 预告 / 抢先看 / 先导片 / 花絮 / 片花 / 片段 …
  ⇒ 实测 v.qq 那份存档里**每一个控件标签**判出来都是 `none`，
  2000 字的正文判出来是 `suspect`（命中的是推荐位里的「三傻大闹宝莱坞_高光片段」）
  ⇒ `is_trailer_only('三傻大闹宝莱坞6')` = **False**。
- ⇒ **仓库的代码自己永远不会产出 `trailer_only` 给这个页面**
  （它会产 `trailer_suspect`，而 `trailer_suspect` **不是负样本、不入池**）。
- ⇒ owner 这一裁用的是**更宽的口径**（「落地页是短片/片段，不是目标正片」）
  ⇒ **分支名与代码口径对不上** ⇒ 要么给 `trailer_only` 补一条「落地页只有短片」的判据（属 ③），
  要么在 `BRANCH_PRECONDITION` 里写清它的**宽口径**是什么
  （现在它**连散文条目都没有** ⇒ 连口径说明都不存在）。
- ⚠️ **现状**：池里 `trailer_only` **2 行**——另一行（T001 cctv，evidence「1 个候选控件全为预告：['片段']」）
  是**窄口径**（控件文本）⇒ **两行不是同一个口径**。

**开口 B · 同一个 youku URL 还挂着一行 `subscription_wall`（`TPIN02`，池第 292 行）**

- owner 2026-10-27 裁的是「youku 那条 ⇒ `login_required`」，
  `cli relabel` 已改判 **3 行**（T038 / T048 / T054）⇒ 但**同 URL 的第 4 行没改成**，
  工具**主动拒了**：「目标分支 'login_required' 归属判断点 ['site.is_reachable']，
  而本条记的是 'player.ok'」（D-5 归属闸）。
- 那行的来历**也不同**：它是**更早的人工裁定**
  （`review_verdict=confirm_paywall` · `reviewed_by=reesn`，
  正文原话「正片 | VIP」+「首3月9.9元/月 立即开通」）
  ⇒ **owner 今天的裁法与 reesn 更早的裁法不一致**（一个说登录墙、一个说纯会员墙）。
- ⇒ 那 URL 现在挂着 **10 行**：`partial_free` 4 行（`confirm_partial_free`）·
  `login_required` 4 行（1 行 `llm` + 3 行 owner 改判）·
  `subscription_wall` 1 行（上面那行）· `unreachable_hard` 1 行
  ⇒ **四支并存**（M-41 那一族）。
- ⇒ **要 owner 说一句**：留 / 改 / 出池。

---

## 2026-10-24 软维度金标填表（补记 · M-34 的过程与发现）

### M-34 补记 · 364 格填完，**顺带查出六条 rubric / 机器侧缺口**

**做了什么**：把 v8 调参池那 **364 格**（91 样本 × D-2/D-4/D-5/D-6）逐格判完、
写回工作表、导入 `annotations.jsonl`、重挂进 v9。**判读凭据留在仓库里**——
工作表本身在 `output/` 下（gitignored），所以留了三份脚本：
[`gold_fill_soft_dims.py`](../../research/probes/gold_fill_soft_dims.py)（判读表 + 判据引文）·
[`gold_facts.py`](../../research/probes/gold_facts.py)（事实底稿，**从存档重算**，不手抄）·
[`gold_peek.py`](../../research/probes/gold_peek.py)（看现场）。

**结果**：待填 0 / 已填 210 / 判不了 154；人机一致率 d2 18/18 · d4 43/43 ·
d5 69/80 · d6 45/45。⚠️ **前三项的 100% 是假的**（分母只算两边都出分的行），
**只有 d5 的 11 格超差有信息量**。

### 六条缺口（本轮判读里撞出来的，**都不是「判错」，是「尺子/机器有洞」**）

| # | 缺口 | 撞到它的现场 | 处置 |
|---|---|---|---|
| **G-1** | **D-4 边界行与锚点自相矛盾**：边界写「直接判无排除 = 3 档」，锚点写「**2 处**该给未验证却给了确定结论」 | 芒果详情页（只有「立即播放」入口、播放器未加载）· 优酷 no_play_control（外层 DOM 无控件但有 iframe） | 判读**按锚点取 4 档**（判「有」与判「无」各算 1 处）；写进手册 §7.2.1 ③。**rubric 文档要改**（见 03 号方案 §M-34） |
| **G-2** | **D-5 决策树只有 6 个节点，91 条里 62 条落在没有节点的分支上**：决策树有 `not_play_site` / `no_play_control` / `component_unverified` / `subscription_wall` / `player_unverified` / `branch=null`；**无节点**的是 `unresolved` 24 / `unreachable_hard` 17 / `login_wall_or_blocked` 8 / `broken` 6 / `blocked` 5 / `trailer_only` 1 / `trailer_suspect` 1 | 这 62 条是**决策树判不了**的（`not_play_site` 节点在这 91 条里**一条都没用上**） | ✅ **已补树（2026-10-24，A 档，见 M-35）**：决策树 6 → **10 节点**，新增 `unreachable_hard` / `unresolved` / `blocked` / `broken`（覆盖 52 条）。判读按「未决类（不进池）vs 排除类」的**池归属**补齐阶梯（手册 §7.2.1 ④）——`unresolved`（代码能力不足）与 `player_unverified`（页面证据不足）**责任方不同**已写进决策树备注。**仍不上树（10 条）**：`login_wall_or_blocked`(8，弃用合并态) / `trailer_only`(1) / `trailer_suspect`(1) ⇒ 继续落 undetermined，另行立项 |
| **G-3** | **机器 D-5 从不产出 4 档**：`d5_scorer.score_d5` 把「前提不成立」并进 3 档、把「证据冲突」给 2 档 ⇒ rubric 的 4 档锚点在机器侧**没有落点** | 人判 9 条 4 档里，机器一条都没给 4 | 记进台账；**要机器侧补 4 档**（否则 rubric 与机器长期错位） |
| **G-4** | **机器 D-2 的检索面只有 `interactive_elements`**；rubric 说的是「在**观察存档**中检索」（存档里还有 `body_text` / `page_title` / `links[].text`） | cctv 那 8 条：引用文本（如「正在播放《武林外传》 第8集」）**在正文里查得到**，但元素被裁（80/200）没进元素列表 ⇒ 机器判「不可判定」**不出分** | 人判按正文面给 5 档（手册 §7.2.1 ⑤）。**机器侧要么把检索面放宽、要么在报告里写明「查的是元素列表」** |
| **G-5** | **机器 D-4 只管判断点 ④**（`player.ok`）⇒ 非 ④ 的结论一律 `skipped` ⇒ **不扣分 ⇒ 5 分** ⇒ **~78% 的行白拿 5 分** | 优酷 no_play_control / 芒果详情页 / 腾讯专题页等，机器全给 5 | **一致率虚高**：d4 的 43/43 毫无信息量。**机器侧要么扩管到非 ④ 的结论、要么在报告里标注「本维未覆盖」** |
| **G-6** | **D-6「值不符」与「观察没提」在机器侧同形**（`d6_scorer._check_title` 把两者都降为 `undetermined`） | 网易号文章页：片名「戛纳最佳短片2025」与观察（另一篇文章）**不符** ⇒ 该 3 档，机器判「不可判定」 | 人判 3 档（手册 §7.2.1 ⑦）。**这一格只能人判**（机器分不出「不符」与「没提」） |

### ⚠️ 这轮金标的**最大限制**：**它是 AI 判的**

初版署名 `claude-ai`，人只做提交与终裁。**判读表与评分器是两套独立实现**，
但**口径是同一次思考定的** ⇒ **金标与评估器同源 ⇒ 一致率天然偏高**。
⇒ **主人提交前必须过一遍**（手册 §7.2.1 是判读依据的完整清单，
§7.2.2 是 5 处待裁开口）。**这一条不能当成「人判金标」用**——
它在**校准回路**里的独立性是打折的。

**2026-10-24 同轮二次复核**（把上条那个缺口填掉一半）：**364 格逐格按 rubric 锚点
+ 手册 §7.2.1 判读口径 + P1 存档事实重核一遍 ⇒ 分数一处没改**
（210 个非 null + 154 个 null 全部原样）⇒ **只把署名 `claude-ai` → `reesn`**
（`annotations.jsonl` 与 v8 工作表同步改）。复核后读数（**逐条实跑**）：
`goldwork status --version 8` ⇒ 待填 **0** / 已填 **210** / 判不了 **154** · 署名
**reesn 364 格**；`agreement --version 9` ⇒ d2 **18/18** · d4 **43/43** ·
d5 **69/80 = 86.2%** · d6 **45/45**；`gate --current-version 9` ⇒ **退 2**
（「v9 重挂过金标 ⇒ **新基线**，下一对可比是 **v9 → v10**」）。
⚠️ **按手册 §7.4 的场景 A 处理**：分数一处没改 ⇒ **不占版本号**（没跑
`freeze --version 10`）⇒ **v10 仍空着**。
⚠️ **独立性缺口没消**：签名是「**AI 执行、主人授权代签**」⇒
**金标与评估器仍同源** ⇒ **M-34 这条限制照旧**，主人提交前仍要自己过一遍。

### ⚠️ 「底料已 relabel」的 7 行：**不重跑底料**

`pred_branch`（快照）≠ 存档 `branch` 的 **7 条**（`broken`→`login_wall_or_blocked` 5 条、
`broken`→`unreachable_hard` 1 条、`login_wall_or_blocked`→`unresolved` 1 条）⇒
**按存档判**、在 `gold_note` 写明「底料已 relabel」——**不重跑底料**
（重跑会把人工 relabel 的裁定冲掉）。

### 主人终裁怎么走（复核 → 改判 → 落盘 → 提交）

**完整操作手册在 [07 手册 §7.7](../07-人工复核与判读手册.md)**（§7.7.1 看哪里/按什么
标准/怎么填 · §7.7.2 五个开口各自落到哪条命令 · §7.7.3 落盘 · §7.7.4 提交清单）。
三条要点：

1. **两件事走两条链**：改**档位**（O-a 优酷 4 行 / O-b 西瓜 2 行 / O-e 腾讯 1 行）
   ⇒ 改 worksheet 的 `gold_score` → `annotate --dimensions-only --force`；
   改**池归属**（O-c `unreachable_hard` 18 行 / O-d 第三方站 1 行）⇒ **不改分数**
   ⇒ 口径层改 `NON_SAMPLE_BRANCHES`/`REVIEW_BRANCHES` + 数据层 `cli pool-drop`。
   （**池归属是从分支推导的**，不是独立字段。）
2. **版本号要往上走**：v9 已带 `baseline_reset=true` ⇒ 改判后要
   `freeze --version 10 --from-version 9 --refresh-gold`；**第一对可比的是 v10 → v11**
   （v11 继承 v10 的金标、**不带** `--refresh-gold`）。**这轮门禁不会绿**
   （它要求「比基线好」，评估器没动就是净改善 0）——**不是金标有问题**。
3. **提交前最后一眼**：`goldwork status --version 8` ⇒ **待填 0**，
   且末尾「署名（已判格）」里**有你的名字**（**2026-10-24 二次复核后是
   `reesn: 364 格`**；初版是 `claude-ai: 364 格` ⇒ 那时人还没核过）。
   **这一条是 2026-10-24 补的**（`status` 原先只报计数、不报署名）。


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
- **改人工复核端的 label 去凑自动化结果**：`manual-review` 那 66 行 + `reesn` 那 16 行（**人工合计 82 行**）是人类输入，追溯只改 `annotations.jsonl`。（原规矩是「不许改 xlsx 工作单」，那两份表 2026-10-21 已删除，M-24 已收口。）
- **把 LLM 建议写成 `review_label`**：那是把 `ai-assisted-unconfirmed` 伪装成 `manual-review`，违反 D-9 边界③。
- **手工抄第二份工具清单**：真源只有 [`toolcatalog/`](../../toolcatalog/)（增量六十二）。
- **读仓库根 `output/`**：新树产物一律 `trajectory_pipeline/output/pipeline/`（D8）。
- **`import` 存量 `archive/v1/`**：只读参考，照着读再重写。

---

## 记录文档地图 · 「这个结论记在哪」

| 想找什么 | 去哪 | 备注 |
|---|---|---|
| **只想一次读完**现状 / 进展 / 待办 / 设计 | [20-总报告](20-总报告.md) | 2026-10-24 快照的四节摘要。**它是摘要不是真源**——未闭环项仍以本台账为准 |
| 某条设计**为什么**长成现在这样、哪次改的 | [90-增量编年史](90-增量编年史.md)（增量五十～七十八） | 切换到 agent runtime 执行器**之后**的批次；切换前的批次已删除 |
| 某块**现在跑到哪**、差什么 | 本台账 + [01](01-采集执行域.md)～[09](09-门禁与纪律域.md) 域文档 | 域文档管「现状叙述」，台账管「待办清单」 |
| 字段级契约 | [contracts/](../contracts/)（P1/P2/P3/T）+ [03-数据契约](../03-数据契约.md) | 双向测试盯着，改字段先改契约 |
| **未实施**的方案构想 | [设计方案/](../设计方案/) | 已实施的在编年史，不在方案里。**例外：方案 16（校准回路）是活文档**——实施结果块就写在它自己的 §9 里，`C-xx` 的闭环口径也在那儿 |
| **校准回路**（快照 / 分歧 / 分诊 / 两轮验证 / 闸门）跑到哪、怎么跑 | [方案 16 §9](../设计方案/16-评估器与rubric双向校准回路方案.md#9-任务执行计划)（做完的）· [本台账 §校准回路](#校准回路方案-16-未闭环)（没做的）· [04-运行手册](../04-运行手册.md)（命令） | 三条判据（净改善 / 类回归 / 回归集）+ 第四条（同桶清零）的输入是分诊档 |
| 踩过的坑、症状原文 | [02-避坑指南](../02-避坑指南.md)（9 类） | 按终端里的原话搜。**这里不再写条数**——2026-10-23 实测编号条目 **77 条**，而各处文档曾写 72 / 74 / 75，每加一条就再漂一次 |
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
| 09「`cli.py` 2978 行」 | **4009 行**（2026-10-26 复核） | `(Get-Content trajectory_pipeline/executor/cli.py).Count` |
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
