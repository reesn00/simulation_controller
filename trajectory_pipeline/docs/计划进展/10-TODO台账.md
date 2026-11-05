# TODO 台账 · 全域未闭环项汇总

> **2026-11-03 · 按 owner 指令裁剪（只留阻塞主线）**：本册原 **4500 行**，现**只保留「§高 · 阻塞主线指标」**
> 的全表（**H-01～H-27**）与各 H 条的**详解**（§停点 · §2026-10-21 的 H-10～H-13 · §2026-10-22 的 H-18 ·
> §2026-11-02「训练语料整改」的 H-21～H-25）。其余内容（**中 / 低 / 校准回路 / 各按日期的「新增开口项详解」里的
> M·L·Q 条目 / 门禁读数 / 过程补记 / 环境依赖 / ⛔ 不能做 / 记录文档地图 / 台账保质期**）**正文已删**；
> 仅**六个被外部文档锚点引用的章节**在文末留了**标题占位**（防 404）。被删正文可从
> `git log -p -- 10-TODO台账.md` 取回。⚠️ 本册口径不变：**只收未闭环项**。

## 怎么用这份台账

| 你的目的 | 看哪 |
|---|---|
| 当前**阻塞主线**的问题、各条差什么 | **§高 · 阻塞主线指标** |
| 某条 H 的**详解** | 顺着该条的「记录锚点」，或本文 `§H-xx` / §停点 |
| 已被裁掉章节的位置（防锚点 404） | 文末「已裁章节标题占位」 |

**判定「闭环」的标准**（三条都满足才算）：① 代码里能指到实现（文件 + 行）；② 有测试封着它（测试名能说出来，且**真的会跑**，不是 skip）；③ 有记录（编年史增量号 或 域文档的「已闭环 + 判定依据」）。

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
| ② H-17 那 7 条假负样本的「转正」路 | **成** | `downgrade` + `pool-drop` **两步**已落 + 契约测试 `tests/contract/test_pool_exit_sequence.py` 封着；**实际处置待人执行**（操作手册见 [07 §2.7](../07-人工复核与判读手册.md)） |
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
| **H-01** | `unresolved` 剩余桶重新归因（原 36/79 = 46%） | 01 采集 / 02 感知 | [01 §未闭环](01-采集执行域.md#本域已知未闭环项) · [02 §INC44 分层](02-感知与判定域.md) · 编年史 §增量四十四 · 编年史 §增量九十八 · 编年史 §增量一百三 · [`agent_loop.py::give_up`](../../executor/agent_loop.py#L927) · [`rule_perceptor.infer_access_defect`](../../perception/rule_perceptor.py#L655) · [`rule_perceptor.classify_paywall`](../../perception/rule_perceptor.py#L240) | 簇 C 已被**增量六十·方案 X** 修掉（净修 46 行，[`branches.py:593`](../../executor/branches.py#L593)），**旧的 46% 已不作数，必须重测**才能知道剩下是谁。⭐ **2026-10-28 那次重测做了**（M-46 批次 9 个任务全跑完）：**当前代（agent/camofox）40 条** `unresolved` 属于「**机器本可以判出来**」——**子族① 36 条**（页面正文里就有终局形态，`infer_access_defect` 命中，**全是 404 页**；正文**抓到了**、`degraded=[]`；规则词表里明写 `404 Not Found` / `nginx`，**注释举的例子正是 `kmslxsh.com`**），**子族② 4 条**（`classify_paywall` 对 agent 叙述判命中）。⚠️ **子族② 的数完全取决于口径**：裸词表（叙述里有「付费墙/VIP/会员/订阅」）⇒ **29 条**，其中 **27 条是「裸词表命中但规范判据不命中」**⇒ **原因不是词表窄，是 `classify_paywall` 压根是为页面正文写的、拿去判 agent 叙述就是用错了地方**。**一条根因**：[`give_up`](../../executor/agent_loop.py#L927) **一律落 `unresolved`、不查任何判据**（对比 `close()` 查 verdict 映射表）⇒ **真负样本不进池**。⚠️ **代际回退**：旧代（LLM 感知层）把同一个 404 判成 `broken` ⇒ **进池 9 行**；当前代 **25 次全 `unresolved` ⇒ 一行没进池** ⇒ **不是一直不行，是换代后退了**。⚠️ **E002 逐 URL 对照**：同任务同 URL 有 **4 条 `subscription_wall` → `unresolved`**（旧批判墙并进池、新批落 unresolved）⇒ ⚠️ **池里那几行还在**（append-only 不回填 ⇒ **训练数据没丢**），**丢的是新档的断言** ⇒ 哪天真按新档重建池就没了。✅ **owner 已裁并已实施（2026-10-28 · Q-25 甲）**：[`give_up`](../../executor/agent_loop.py#L927) 收口前先查**最后一页自己的正文**的终局形态（[`branches.py::terminal_shape_branch`](../../executor/branches.py)）⇒ 命中落那条分支、不命中仍落 `unresolved`；⚠️ **agent 自述不算证据**、⚠️ **命中不等于对**（仍要人核）。回放读数（全库 **210 条「agent 放弃」visit**）：命中 **30 条**（**28 `broken` / 2 `blocked`**，落在 **8 个落地页**）、**归因对象不明 12 条**、未命中 **168 条**（其中 **5 条正文命中但前置事实不成立** ⇒ 降级 `unresolved`）。⚠️ **回放不是重跑**（读的是旧存档、用的是新判据）⇒ 下一批真跑才有新读数<br>⭐ **2026-10-29 真跑读数已出**（批次 `guard-rerun-2026-10-28` · 19 个 give-up 命中任务全跑完 · 串行 · 约 7.5 小时）：**79** 条「agent 放弃收口」visit ⇒ **护栏判据命中 17**（**15** `broken` + **2** 前置事实不成立降级）+ **6** 归因对象不明 + **56** 不命中 ⇒ **本条台账问的「机器本可以判出来」那批，现在真的落进了分支**（**15** 条 `broken` **全部在池里**，池 **407 → 437**，+30 行全是本批 run 写的、其中 9 行是护栏落的）。⭐ **上线前后读数差**（同 task_id + 同 url）**57** 条分支不同 ⇒ **其中只有 15 条是护栏效应**（全是 `unresolved` → `broken`）⇒ 其余 **42 条混着站点漂移**，**不是护栏的功也不是过**。⚠️ **仍有 3 件等人裁**（E021 的 host 级 403 / E014 同 URL 两代分歧 / T011 同 URL 出两支）⇒ 详见 [Q-25](10-TODO台账.md) 行与 [编年史 §增量一百](90-增量编年史.md#增量一百--两条护栏的第一次真跑guard-rerun-2026-10-28--19-个任务--q-25-甲真命中-15-条--q-24-甲一次没触发--顺带撞出同-url-出两支)<br>⭐ **2026-10-29 · T011 已裁并已实施**（owner 裁定「改判成 `broken` 再并池」）：⚠️ **`cli relabel` 改不了 `unresolved`**（它只取**负样本分支**，而 `unresolved` ∈ `NON_SAMPLE_BRANCHES`）⇒ **唯一合规路径是人工复核通道**（手册 §4.5 · `cli review --verdicts … --apply`，**落盘时自动并池**）。⭐ **现场取证**（`research/probes/q24_q25_guard_kmslxsh_probe.txt`）：该 URL · `/v/17807/…` · 站点根 **3/3** 返回「404 Not Found\nnginx」、`elements_total=0` · `video_tag_count=0` · `degraded=[]` ⇒ **前置事实成立** ⇒ 当初那条观察若没带 `press_unsupported`，护栏本就会落 `broken`。⇒ `T011__78621699`：`unresolved` → **`broken`** · `source=human` · `branch_before_review=unresolved` 保留；池 **437 → 438**。⚠️ **执行这一条撞出两个机制缺口（都修了，各有测试）**：① **`review_queue._fits` 对无题产地一律拒** ⇒ **Q-25 甲落的 `broken`（`question` 恒为空）人裁通道够不着**（执行层认的组合人核层不认 = 两本规则书）⇒ 改为**只**给 `TERMINAL_SHAPE_BRANCHES`（`broken`/`blocked`，**从 `branches.py` 导入不手抄**）开口子，**判断点类分支空 question 仍拒**；✅ **owner 已追认（2026-10-29）**：这两处机制改动是**执行这一条裁定的必要条件**（改的是**复核通道的准入与范围**，**护栏判据一行没动**）；② **`review --apply` 不加限定会把每份原档重算成复核档** ⇒ **2026-10-24 那轮 78 份人裁定会被原档静默覆盖** ⇒ 改为**读生效档**（有 `.reviewed.json` 就以它为底）+ 新增 **`--only`** 钉死范围。⇒ **残余**：同 URL 另有 **27** 条 `unresolved` 分布在 **20** 个任务上（**全裁只新增 6 行**池行：E028 · T012 · T028 · T033 · T036 · T040；另 21 条撞键去重）⇒ 全裁后 kmslxsh 的 **50** 条 outcome 行里 **47** 条落 `broken`（另 3 条 `login_wall_or_blocked` 不变、`unresolved` 清零）、**T011 同 URL 两支**消除 ⇒ **待 owner 再裁**（本轮授权只覆盖 T011 这一行）。⚠️ **门禁退出码更正**：`cli check-archives` 实测 **exit 1**（46 条 `negative_contradicted_by_success` 是 `Severity.FATAL` ⇒ `report.ok=False`）——此前记的「exit 0」是**量错了**（`$?` 取的是管道末端 `tail` 的状态）；那 46 条**全是存量**（`subscription_wall` 32 · `partial_free` 13 · `component_unverified` 1）⇒ **0 条**落在 `broken` / kmslxsh 上；归档级另有 **3 条 `negative_not_pooled`（DEGRADED，非 FATAL）**＝ `pool-add` 干跑那 8 条里的 3 条（E021 被判成功**矛盾** / E023 · T054 池内同址**已有别的分支**）⇒ **存量的人工分歧，不是本轮引入**<br>⭐ **2026-10-29 · E021 已用浏览器实看，并立了「换面会不会变」这把尺子**（owner：「人工用浏览器看一眼，给出完整 url」）：**完整 url** = `https://m.xacjs.com/weihu/1416804.html`（全库 `m.xacjs.com` 只有这 **1** 个站点地址；两条 run 的跳转链 `http://www.baidu.com/link?url=fF_mLv2j_CGIezOu0HDKjCUXNi-A4CGCwu2Mjv0oxuNwjw-fNI_xPjOI2XhyakIr` / `http://www.baidu.com/link?url=DXsjd367aDwviTqDGDsY6rQWXVhhSn5fWgdj3mg0G1jcoxhSjzGl30Y5JEwzxLMR`）。**生产面实看**（camofox，**5** 个路径）**5/5 = 「403 Forbidden / nginx」**、元素 0 · video 0 · `降级=[]`（留痕 `research/probes/q24_q25_guard_host403_recheck_2026-10-29.txt`）⇒ 前置事实成立。⭐ **新工具：面依赖检查**（`research/probes/q24_q25_guard_facecheck.py` —— 同一台机器、同一出口 IP，**只换请求头**）：`chrome`/`bare`/`referer` 三种客户端 × **7** 个 xacjs URL ⇒ **21/21 HTTP 403**；⚠️ **同机同出口的对照站 `m.dinggetv.com` 同刻 200 OK** ⇒ 403 是**站点对本机出口**的拒绝、不是本地网络问题（留痕 `…_facecheck_xacjs_2026-10-29.txt`）⇒ **判读口径三选一**：**换面不变** ⇒ 站点的事实（真负样本成立）· **换面变了** ⇒ **我们这个面的事实**，不能外推成「站点看不了」· **换面仍是拒绝** ⇒ **始终没看到站点页面** ⇒ fail-closed、**不是**真负样本。⭐ **把池里全部 10 个 `broken` 唯一 URL 都查了一遍**（**30** 次请求 · `…_facecheck_pool_broken_2026-10-29.txt`）⇒ kmslxsh ✓ / wanminggangguan ✓ **不变（站点事实）**；`v.qq.com/error.html`·`bilibili.com/error.html`（URL 本身就是错误页）✓；`104.143.94.74/guocanju/wulinwaichuan/`·`bilibili.com/bangumi/play/ep693415`（普通客户端 200，但裁的是 `player.ok` 级「媒体解不了」）**不是反证** ⇒ **两行站不住**：`m.xacjs.com/weihu/1416804.html`（403 ⇒ fail-closed ⇒ `broken` 不成立）、**`m.dinggetv.com/dingge/84421.html`（E014 —— 普通客户端拿到 HTTP 200 + **80 KB** 真页面《…大话西游之大圣娶亲国语版…免费在线观看全集》⇒ 旧代那条 `broken` 是**面产物** ⇒ ⚠️ **先前「E014 不用动」的结论是错的**，那是没换面的前提下说的）**。⇒ **两条待 owner 批的处置**（E021 与 E014 出池）：`cli downgrade`（→ `unresolved`，理由进 evidence）＋ `cli pool-drop`（写审计日志）——⚠️ **复核通道够不着**：`apply_verdicts` 只对 `REVIEW_BRANCHES` 生效，`broken` ∉ ⇒ 那行会被原样忽略；`cli relabel --to blocked` 又拒（`blocked` 是采集侧分支、不是真负样本）⇒ **出池两步是唯一合规路径**。⇒ **判据层还开着一个口**：`ACCESS_DEFECT_KINDS` 里**没有 403**（`region`/`captcha`/`anti_bot`/`not_found`）⇒ 403 错误页含 `nginx` ⇒ 被 `not_found` 旁证命中 ⇒ 落 `broken`（**采集侧的东西进了真负样本池**）⇒ **今天再跑一遍 E021 还会再写一行错的** ⇒ 「加一类 `forbidden` ⇒ 403 落 `blocked`（不进池）」是**另一个 owner 决定**，**判据一行没动**。⇒ **三件的人核状态（截至 2026-10-29）**：**T011 已裁已实施**（见上）· **E021 已取证、待 owner 批出池** · **E014 已翻案、待 owner 批出池** ⇒ 详见 [编年史 增量一百 §八](90-增量编年史.md)<br>⭐ **2026-10-30 · 这条线现在同时压着 D-10 上岗线的分母**（新增交叉引用，不改本条定性）：D-10 改绑到「**调参池 × 人工署名子集 × 三态口径**」后，调参池 37 行人核行里 **19 行代码侧留 `unresolved` ⇒ 进不了分母**——而**这 19 行的金标 19/19 条全有结论**（人亲自定性）⇒ **不是判错，是代码没判出来** ⇒ 上岗线分母只剩 **18 行**（读数 **16/18 = 88.9%**、弃权 **19/37 = 51.4%**）。⇒ **D-10 的下一步不是调线，是把本条的桶填掉、把分母做厚** ⇒ 详见 [H-02](10-TODO台账.md) 与 [07 §D-10](07-评估与黄金集域.md) |
| **H-02** | ✅ **已闭（2026-10-25 v13 冻结档，2026-10-27 复读）** D-10 代码层 vs 黄金集一致率 → ≥80% | 07 评估 | [07 §D-10](07-评估与黄金集域.md) · [00 §四条主线](00-总览.md#一句话现状) · 编年史 §增量一百三 | ✅ **已达标**：头条 **82.8%（106/128，调参池口径 C）≥ 80% 上岗线**（上岗线真源 `evaluation/scorers/thresholds.py` 的 `d10_min_agreement`，owner 2026-10-24 从 0.90 下调）。**涨幅落在采集侧判定**：v13 冻结时 `code_refresh` 从 `candidates.jsonl` 重挂 **26 行** `code.branch`（manifest `code_refresh.changed_rows=26`），`disagree` 68 → 59。⚠️ **旧口径（三档合计）79.6% 仍差 0.4pp**——上岗判定以头条为准（C-04），别拿三档数当头条。**原诊断留档**：簇 1 type A（component_unverified 漏 VIP 强信号）+ type C（collection fail 8 条）两条路都跑过；⚠️ **先跑哪条不是拍脑袋定的**——**C-07 已出数（2026-10-23）**：`gold_defect` 14 / 评估器 12（`evaluator_logic` 11 + `evaluator_prompt` 1）/ 采集 7 / `rubric_*` 0 ⇒ **先核金标（14，属 M-30），再修评估器（12，属 M-26 / M-29），最后才是重采集（7）；「改 rubric」在这一轮没有证据**。⚠️ 但这份分布只覆盖 `d10` 一个条款、单版横截面，**别当成趋势**；并且评分器改了要等重冻结刷新分数（M-29），否则下一轮读数与改动无关。**残余偏差**仍挂 M-26 / M-29 / M-30<br>⚠️ **2026-10-30 口径变更后的注记（本条状态未擅自改）**：上面那句「已闭 / 已达标」是在**旧绑定对象**（口径 C × 调参池全体）下下的；**现役绑定对象 = 调参池 × 人工署名子集 × 三态口径**（分母 = `agree` + `disagree`，**弃权单列不进分母**），现读数 **88.9%（16/18）**、弃权 **19/37 = 51.4%**、**阈值 0.80 数值未动**（变的是绑定对象，见 `thresholds.py` 的 `scope` 栏）。⇒ **本条是否按新口径重开，待 owner 裁定**；**旧读数 82.8%（106/128）保留作跨版本对比用，不再是上岗线的判定依据** ⇒ 详见 [编年史 §增量一百三](90-增量编年史.md) |
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
| **H-18** | ★**门禁 `success_branch_mismatch` 报的是假阳性**——`_match_outcome` 对同一个 URL 取**第一条** outcome，而站点阶段与播放阶段**各记一条** ⇒ 拿错了那条 | 09 纪律 | [`integrity.py:233`](../../executor/integrity.py#L233) · [`integrity.py:345`](../../executor/integrity.py#L345) · 编年史 §增量八十一「仍未闭环项」 | 实测 2 份（`T001__67a05faa` / `T054__93f110b6`）：`T001` 是**存量 obscura 存档**⇒ **既存缺陷**，新批次只是让它显形。修法：同 URL 多条 outcome 时，**按 `reached_play_page` / 阶段**挑那条对应的（而不是第一条）⇒ 门禁是查「自相矛盾」，**不能自己挑错一条再报矛盾**。**修法与验收见本文 §H-18** |
| **H-19** | ★**（2026-10-24 新增）复核队列的覆盖面只到 `REVIEW_BRANCHES` 三支 ⇒「记成功」的假正样本与 `trailer_only` 的错负样本**机制看不见** | 08 存档 / 03 标签 | [07 手册 §4.6 本轮裁定记录](../07-人工复核与判读手册.md) · `executor/review_queue.py` 的 `REVIEW_BRANCHES` · 池 `output/pipeline/negative.jsonl`（260 行）· 样本 `output/pipeline/samples/E006__2729220b__site-4.json` | **2026-10-24 把 224 行复核队列逐条裁完时撞出来的**：队列只收 `{unresolved, trailer_suspect, player_unverified}` ⇒ **分支不在这三支的行压根不进队列**。已实测两类漏网：① **假正样本**——`E006/2729220b` 的网易号《济州岛美食攻略》（`163.com`）记的是**成功**，已进训练样本集（`outcome.branch=null` · `gate.status=passed`），而**它自己的 rationale 反着说**（「当前页面不是目标资源页」「目前未找到该片合集的可播放网址」）⇒ 记录的成功与 agent 自报**互相矛盾**；同 run 的 `koreabycar.com`（同类美食博客）落 `unresolved` ⇒ **进了队列被判掉** ⇒ **同一个坑，一边修得了一边修不了**；② **错负样本**——`T001/d063ddf4` 的央视网《武林外传》第 1 集**正片页**落 `trailer_only`，是**池里唯一那 1 条**（教模型「正片页 = 只有预告」，误杀，见 **M-12**）。⇒ **不是「没判完」，是「机制判不到」**：处置要么扩 `REVIEW_BRANCHES`（把「记成功」与 `trailer_only` 纳入复核面）、要么另开「正样本抽检」通道 ⇒ **都改代码 ⇒ 等 owner 拍板**。⚠️ 池里另有 **8 条 `v.qq.com` 的 `subscription_wall`**（H-16 那批 8 条，**同样不在三支里**）⇒ 扩面时一并纳入。⚠️ **2026-10-27 订正一处**：「队列只收三支」在**代码侧已经不成立**——`blocked` 已于 2026-10-26（方案 20 §R1）扩进 `REVIEW_BRANCHES`（`executor/review_queue.py:192`）。而盘上那份 `output/pipeline/review_queue.jsonl` 是 **10-24 18:31 导出的旧稿**（224 行、**行行带人裁定** ⇒ **不能覆盖重导**）⇒ **「队列里没有 `blocked` 行」是稿子旧，不是机制缺**：2026-10-27 只读重导实测 `待复核 515 条 = unresolved 431 · blocked 70 · player_unverified 8 · trailer_suspect 6` ⇒ **`blocked` 已经进面**。⇒ **仍等 owner 拍板的还是那两条**：① 「记成功」的**假正样本**（机制判不到）② `trailer_only` 的**错负样本**（M-12） |

| **H-20** | ★**（2026-10-26 新增）执行层换 camofox-browser ⇒ 采集链还没在新面上真跑过**（第一梯队卡在这） | 01 采集 / 09 纪律 | 本文 **§停点** · [设计方案 21](../设计方案/21-camofox-browser执行层接入方案.md) · `pyproject.toml` 依赖段（`playwright` 已摘） · 编年史 §增量九十三 · 编年史 §增量九十四 | **代码与门禁已全部落地**（`camofox_session.py` / `camofox_driver.py` / 三张面 / `LEGACY_DRIVER_SURFACE`），**门禁绿**。**环境已通**（`camofox-browser-mcp` 适配器 + 上游 REST `:9377`，**本仓库不启动浏览器**）⇒ **第 4 轮那 10 条已在 camofox 面上真跑过**（批次 `plan-camoufox-prod1`）⇒ ✅ **camofox-1 面的 `click`/`fill`/`press` 三条已按真实调用转正**（`exercised=True`；`required` 口径**逐条按真实键在场率校**：`click` 的 `role`/`name` 100% ⇒ 都必填 · `fill` 三个键 100% ⇒ 都必填 · `press` **只有 `key` 在场** ⇒ `role`/`name` 保持可选）。⚠️ **但第 4 轮自己带出了五个「零报错」的缺陷**（§停点 · R5 实跑那一段的五个缺陷）⇒ **卡点从「没跑过」变成「跑 → 读 → 修 → 清 → 再跑」这个循环还没走完**（已走四轮；**第 5 轮在跑**）⇒ 剩下的：**逐条打开存档读 evidence**（缺陷⑤ 就是这么读出来的）+ **重建产物与门禁** + **采集侧账** + **人判存量那 4 条 agent 负样本**。⭐ **2026-10-27 更新：第 5 轮已跑完**（砍到 **6 条**跑完，理由见编年史 §十一 · 5-1-1）⇒ **缺陷⑤的修法在 6 档真数据上站住**（**建页路由失败 0 次**；T048 用时 5972 s → 1619 s）⇒ **逐条读 evidence 已做**（读出 **M-43** / **M-44** 两个新开口项）⇒ **重建产物 + 门禁已做**（`build --overwrite` → `task-view --all --overwrite` → `check-archives`；`4252 passed` 全绿）⇒ **采集侧账已跑**（6 档 **61** 条打点、**单面** `camofox-plain`、跨面**结构上**为 0）⇒ **并发批的 6 条 `unreachable_hard` 已逐条单独复核并出池**（池 331 → 325）。⇒ **剩下唯一的 owner 动作**：**人判存量那 4 条 agent 负样本**（材料已备，见 §停点 ⑥）。⚠️ **camoufox 不许接回来**（owner 裁定）——`camoufox-1` 面**只读不再产出**，但存量 430 份 P2 样本 + 14 份顶层 P1 存档**仍读得懂**（`LEGACY_DRIVER_SURFACE`） |
| **H-21** | ★**（2026-11-02 新增）交付的「思考」不是 agent 的原生 think**——事后补记**无条件覆盖**，生产面 **0 条 `native-think`** | 05 rationale | [05 §未闭环](05-rationale与闸门域.md#本域已知未闭环项) · [方案 23 §1.2 / §3 R0](../设计方案/23-训练轨迹语料整改方案.md#12-思考的来源原生-think-一条都没留下) · [`agent_loop.py:1313`](../../executor/agent_loop.py#L1313) · [`orchestrator.py:1305`](../../executor/orchestrator.py#L1305) · [`actions.py:318`](../../executor/actions.py#L318) | 生产面 4852 步：`reason==""`（writer 产物）**4850** · writer 失败标记 **2** · **`native-think` 0**。三行链路：决策时挂原生 think → 访问结束后**对每个候选站无条件** `_write_rationales` → `attach_rationale` 是 `replace()`、**无守卫** ⇒ agent 模式的原生 think **必被顶掉**。⇒ 交付语料 = agent 选的动作 + **另一个 LLM 事后配的理由**。修法 R0-1/R0-3（补记跳过已有 native-think + 加守卫）；`reason` 保持可辨两个来源。验收 **A1**：新批次 agent 模式 native-think 占比 **≈100%**。✅ **R0-1/R0-3/R0-4 代码已实施（2026-11-02，见下 §H-21）；A1 待新批次** |
| **H-22** | ★**（2026-11-02 新增）「没有历史 / 首次操作」与它自己的上下文打架**——补记的 history 是**站点单元级**，交付的是 **run 级**对话 | 05 rationale | [05 §未闭环](05-rationale与闸门域.md#本域已知未闭环项) · [方案 23 §1.3 / §3 R0](../设计方案/23-训练轨迹语料整改方案.md#13-没有历史--首次操作与上下文自相矛盾) · [`orchestrator.py:1421`](../../executor/orchestrator.py#L1421) · [`agent_visit.py:97`](../../executor/steps/agent_visit.py#L97) | 生产面 **235** 步含「没有历史/首次」措辞，其中 **225 步前面已有 ≥1 条观察、202 步前面已有 ≥3 条**（全三面 **346**）。成因：`_write_rationales` 传 `steps[:index]`（**当前单元**）+ 每个候选站新建 AgentLoop（`messages` 从零起）⇒ 补记者看不到前面的站点；组装时单元被拼成一条 run 级对话 ⇒ **两件事被缝在一起**。修法 R0-2：history 改 run 级（摘要式，别带全文）**或**单元首步禁写这类措辞。验收 **A2**：「无历史措辞 × 上下文确有历史」= **0**。✅ **R0-2 代码已实施（2026-11-02，见下 §H-22）；A2 待新批次** |
| **H-23** | ★**（2026-11-02 新增）终局验收不读最终回复** ⇒ **44/49（89.8%）**的生产样本不满足自己的 `output_contract`，`gate` 仍判 `passed` | 09 纪律 | [09 §未闭环](09-门禁与纪律域.md#本域已知未闭环项) · [方案 23 §1.4 / §3 R2](../设计方案/23-训练轨迹语料整改方案.md#14-终局答复) · [`P3-views.md:63`](../contracts/P3-views.md) · `assembler/schema.py`（闸门段） | `output_contract.min_urls ≥ 1` 的 camofox-1 样本 **49** 份，最终答复含 URL 的只有 **5** ⇒ **44/49 违约**；`gate` 判 `passed` **102/131**、`observation_complete` **65/65**（它量的是观察齐不齐，不是答复兑不兑现）。根因是**契约明文**：判定 7 只判 `outcome`、不读回复文本 ⇒ **不是实现 bug，是这个设计不适合当训练目标**。改判定 7 属**明文设计变更** ⇒ ✅ **Q-27 已裁（2026-11-02）：要读** ⇒ 修法 R2-3 可实施。验收 **A4**：契约要求 ≥1 URL 的样本含 URL 率 **100%**（否则改答复 / 降级 / 剔出训练集——**不许「判通过但没兑现」**）|
| **H-24** | ★**（2026-11-02 新增）收尾是内部记账语言**——分支枚举名 + 计数 + **deprecated 分支**进了训练语料，且 P3/T 两侧不是同一句话 | 06 组装 | [06 §未闭环](06-组装域.md#本域已知未闭环项) · [方案 23 §1.4 / §3 R2](../设计方案/23-训练轨迹语料整改方案.md#14-终局答复) · [`views.py:422`](../../assembler/views.py#L422) · [`task_view.py:370`](../../assembler/task_view.py#L370) · [`branches.py:80`](../../executor/branches.py#L80) | P3 **131/131** · T **65/65** 末条 assistant 都是代码合成的「任务总结：…处置：…分支分布：…」；`login_wall_or_blocked` 这类 **deprecated 分支**也在里头；**两侧刻意不同**（P3 有 reflection 结论无分支分布，T 反之，INC46）⇒ 同一条轨迹在**两份交付物里有两个结尾**。修法 R2-1/R2-2：收尾改「**面向用户的答复**（找不到 + 已排除的站点 + 原因）」，两侧同一句话——✅ **O-5 要求两段投影一致 ⇒ 收尾必须同一句话**（交付要求，不是排版偏好）。验收 **A3**：训练集收尾里分支枚举名出现次数 = **0**。**✅ 代码已落地（2026-11-02，R2-1/R2-2/R2-3）**：`assembler/closing.py` 单一真源、两侧同一函数、R2-3 接进 P2 `gate`；A3 已取数 ✓（131/131 = 0），**A4 待新批次**；存量 44 条违约样本未回填 |
| **H-25** | ★**（2026-11-02 新增）观察整页快照 + 死循环步**：噪声压倒信息、重复动作在教「再点一次」 | 06 组装 | [06 §未闭环](06-组装域.md#本域已知未闭环项) · [方案 23 §1.5 / §3 R1·R3](../设计方案/23-训练轨迹语料整改方案.md#15-观察与动作) · `assembler/observation_view.py` · [`views.py:123`](../../assembler/views.py#L123) · [`task_view.py:180`](../../assembler/task_view.py#L180) | 单条回执 1000~3000 字、任务相关不足 10%；「相邻同动作 + 观察逐字相同」**469** 处，有重复动作的视图 **64/65**、重复步 **1558/4071 ≈ 38%**，`goto` 1703 次只有 **1183** 个唯一 URL，59/65 视图重复同一 `goto` ≥3 次（最多 **19** 次）。修法 R1：回执改「标题 / 网址 / 任务相关摘要 / 限量元素表 / 限量链接」，**P1 存档全文一个字不动**（存档是证据，文本是语料）；R3-1/R3-2：死循环步剔出或显式标记。⚠️ **降级标记保留**（`[采集降级: body_text]` / 「本步没有取到页面内容」是 fail-closed 的一部分）。验收 **A5/A6/A7**：回执中位数 ≤ **800** 字 · 相邻同动作同类步 = **0** · 重复步占比 ≤ **10%**（A7 口径见 Q-31）。✅ **R1 + R3-1/R3-2 代码已落地（2026-11-02）**：A5 ✓（3026→772 字）· A6 ✓（存量重算残留 0）· ✅ **A7 ✓**（owner 2026-11-02 裁定方案 A：窄口径「同动作 + 同观察 / 保留步」P3 **7.0%** / T **2.5%**，均 ≤10%；宽口径 35.7% / 21.3% 只作审计基线；见下 §H-25 与 **Q-31**） |

| **H-26** | ★**（2026-11-04 新增）agent 路径的前置事实核已补上 ⇒ **两处判据缺口因此显形**：真付费墙会被降级 · 登录墙没有「可执行」的前置事实核** | 02 感知 / 03 标签 | 编年史 §增量一百二十三 · [`agent_loop.py::_guarded_branch`](../../executor/agent_loop.py) · [`branches.py::precondition_failed`](../../executor/branches.py) · [`test_agent_loop.py::Test分支前置事实核`](../../tests/unit/test_agent_loop.py) | ✅ **代码已实施**（2026-11-04）：agent 路径的分支归因现在**过一遍** `branches.precondition_failed`——**scripted 路径的同一份判据**（原来只有 `RunLedger.record_from` 核，agent 路径不走它 ⇒ CLAUDE.md「贴标签前核前置事实」在 agent 路径上**一直是空的**）⇒ 门禁的「**像**付费墙」（强弱关键词）不再直接折成 `subscription_wall`（结论口径要**门禁语境**）⇒ **导航栏挂着「开通VIP会员」的免费页不再被记成「这里看不了」**；不成立 ⇒ 落 `unresolved` + 追一句「`[记账降级] 本该记 X，但…`」⇒ 门禁事件分支 / `AgentOutcome.branch` / 台账行**三者一致**。**新增 5 条单测**（导航栏 CTA ⇒ `unresolved` · 真付费墙仍 `subscription_wall` · 分支一致 · 理由进 `reason` · 登录墙不受影响）<br>⚠️ **两个缺口**：① **真付费墙也会被降级**（**2026-11-04 已闭**——判据二「订阅价目门禁」落地，见本行末尾）——`paywall_evidence` 只认 L1 门禁短语 / L2 CTA+同句观看语境 / L1.5 错误+会员 commerce，**实测对部分真付费墙返回空**（youku《功夫》· fun.tv）⇒ 这些站现在落 `unresolved`（**不进池**）⇒ **「每条分支都必须有对应样本入库」这条纪律在付费墙这一支上暂时是反的**（真负样本被降成非负样本）；② **`login_required` 没有「可执行」的前置事实核**——今天只有两条分支真的在核：`subscription_wall`（门禁语境 `paywall_evidence`）与 `component_unverified`（同页守卫）；`BRANCH_PRECONDITION` 里 `login_required` / `blocked` / `partial_free` / `broken` **只有文案、没有可执行核**（2026-10-19 增量六十·方案 X **刻意**窄的：当时守卫覆盖 5 支，实测 **46** 次触发**全是误伤** ⇒ 窄到只剩 1 支）⇒ **代价**：`login_required` 在 agent 路径上**仍是裸折算**（门禁判「像登录墙」就直接贴分支）<br>✅ **缺口①已闭（2026-11-04）**：owner 拍「给这一支补第二条可执行判据」⇒ **判据二 · 订阅价目门禁**落地（[`rule_perceptor.py::paywall_price_evidence`](../../perception/rule_perceptor.py)：**页面级三要素同现**——订阅价目 + 开通动作 + 权益承诺，**外加登录门排除**）⇒ 前置事实核改认**判据一 ∪ 判据二**（[`subscription_wall_evidence`](../../perception/rule_perceptor.py)）⇒ youku《功夫》· fun.tv 这类「价目 + 开通 + 权益」但**不写「订阅后观看」措辞**的真付费墙**重新贴得上** `subscription_wall`（实测找回 **67** 行：youku **45** · iqiyi **15** · fun.tv **4** · mgtv **3**）。⚠️ **判据二不进 `paywall_evidence`**——H-16 修复① 的收紧口径（纯营销模块不得算付费墙）**原样不动** ⇒ 救济路径与评分器口径不变；判据二**只**服务于 `subscription_wall` 的前置事实核<br>⚠️ **缺口②仍开着**：`login_required` 在 agent 路径上仍是裸折算 |
| **H-27** | ★**（2026-11-04 新增）改分支却**没改中文标签** ⇒ 存量 **137** 条记录自相矛盾（**三个写者**漏了三个）· 工具已修 · 存量已回填 · **LS 两条卡仍是旧的** | 08 存档治理 | 编年史 §增量一百二十三 · [`downgrade.py::apply_downgrades`](../../executor/downgrade.py) · [`relabel.py::_relabel_outcome`](../../executor/relabel.py) · [`review_queue.py::apply_verdicts`](../../executor/review_queue.py) · [`label_sync.py`](../../executor/label_sync.py) · [`test_label_sync.py`](../../tests/unit/test_label_sync.py) | ✅ **三个写者全修 + 存量已回填**（2026-11-04）：改 `branch` 的写者有**三个**——`downgrade.apply_downgrades` · `relabel._relabel_outcome` · `review_queue.apply_verdicts`——**三处都漏了** `branch_label` ⇒ 记录变成「分支说 A、标签写着 B」的**自相矛盾**，而下游取的是**行内**的 `branch_label`（`golden/builder._code_block` → `golden_ls/exporter`）⇒ 矛盾一路传到 LS 的**人核卡**上：**人看到的是「付费墙」，机器读到的分支却是「未判定」**。⇒ 三处现在**共用** `branches.branch_label_of(branch)`（`None` → 「成功」）——**标签的取法只有一处**。⚠️ **原标签不丢**：`branch_before_downgrade` / `branch_before_relabel` / `branch_before_review` 记着原分支，中文名按 `BRANCH_LABELS` 可还原<br>✅ **存量已回填**（新增 `executor/label_sync.py` + `cli sync-labels`，**默认 dry-run**）：生产根 **137** 条（按层：原档 **61** + 复核档 **76**；按来源：降级/改判 **108** + 人裁 **29**）· 72 份文件；双跑研究档另 **8** 条 ⇒ **生产根复扫 0 条残留**（**328** 条「分支被工具改过」的记录全部同源）、池内 **0 条同源矛盾**、T 视图 **155** 份 / **1178** 条 `unit_outcomes` **0 条**同源矛盾（另有 **34** 处**口径文本漂移**，**按惯例不回填**）。⚠️ **判据是「分支被工具改过」**（三个留痕键），**不是**「标签文本与当前 `BRANCH_LABELS` 对不上」——后者是**口径演进**（`unreachable_hard` 2026-10-27 收窄 · `login_wall_or_blocked` 2026-10-12 加 deprecated 后缀）⇒ 按 `executor/branch_eras.py` 的 e6 **不回填**（那 47 条没动 · 原档 47 / 两层镜像 94）⇒ **备份目录也没动**（`q23_trial` 151 · `q23_backup_2026-10-28` 37 · `pipeline/_backup_pre_motivation` 27——备份是当时状态的快照）<br>⚠️ **LS 项目 77 的两条卡推在改判前**：**601** = `T001__7d5a03bd`（推于 2026-11-02）· **665** = `T001__61eabe2e`（推于 2026-11-04T02:24Z）⇒ 卡上的 `meta_text` 仍写着那条 `subscription_wall` ⇒ **重推 / purge 都是对外动作**（且 `purge` 连标注一起删）⇒ **本轮没动**，等 owner 裁<br>✅ **owner 已裁并已执行（2026-11-04）**：**就地重推**（`PATCH /api/tasks/{id}` + `PATCH /api/predictions/{id}`）⇒ **不删卡、不新建卡**（task id 601 / 665 与标注归属不变 · 项目仍 **77** 条 · **0** 条重复）⇒ 两张卡 `site-8` 现在读 **`unresolved`**（卡面里剩下的 `subscription_wall` 字样只在**审计留痕**中）· 601 的 `status` 因当前 `scout/r51` 档是 undetermined 而翻成 **`quarantine`**（= 当前产物本身的样子）· 台账 `push_index__77.jsonl` 追加 2 行（append-only） |
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

---

## 2026-10-22 新增开口项详解

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

---

## 2026-11-02 新增开口项详解（训练语料整改 · 方案 23）

> 这一组**同源**：一次对**已合成训练语料**的只读审计（**没跑管线、没推 LS**），
> 逐环读 `system prompt → user prompt → llm thinking → assistant → tool call →
> tool result → llm result`。**读数与量法全在**
> [设计方案 23](../设计方案/23-训练轨迹语料整改方案.md)（**§1 事实基线**每行带量法，
> 重跑即可复核）。⚠️ 这一组只记**产物侧**的缺陷；执行层的探索权归属、
> 评估侧口径都不在整改范围内（方案 23 §0.3 / §8）。
>
> **数据根**：主树 `output/pipeline/`（**worktree 里的 `output/` 是空的** ⇒
> 读数必须指回主树，否则分母会静默变成 0%）。审计用的临时脚本**用完即删**、不进产品。
>
> ✅ **Q-26～Q-31 已于 2026-11-02 由 owner 裁定**（O-1 还给 agent / O-2 要读 /
> O-3 分池 / O-4 只接 per-Run / O-5 两段都发且一致 / **A7 改绑窄口径**）⇒ 下方 H/M 项里原先
> 「等裁定」的三处（**H-23 / M-67 / M-69**）已解锁；**R0 + R1 + R2-1～R2-4 +
> R3-1～R3-4 + R4-1 + R4-2 + R4-3 + R5-2 代码已于 2026-11-02 实施**（A1/A2/A4 ✅，
> ✅ **A13 已在新批次真跑取数**，✅ A7 口径已裁定并按新口径取数（**Q-31**），A8/A9/A11
> 待重建产物后取数——**既有产物不回填**；**A10 可在新批次上复核**），
> **R5-1 已在隔离目录重建 + dry-run（不写 LS，2026-11-02）**：65 份 T 视图 /
> **65 条 per-Run 行**（桶分布全 `quarantine`）/ A10 违约 0。
>
> ✅ **真实 LS 推送已完成（2026-11-02，owner 授权）**：项目 **77
> `trajectory-review`** 现有 **76 条 per-Run 行**（推 65 条 + A13 新批次 8 条 +
> 烟测 1 条 + 2 条 10-19 老行），**逐条能回到 P1 存档**（**从 LS 回读 76 行实核，
> 76/76 都落得到**）⇒ **A15 达成**；
> 金标项目 `golden-v14/v15` **一行未动**。⚠️ 推上去的全是 `quarantine`
> （机器**缺判**，不是「机器判合格」）⇒ LS 上是**人核队列**。
>
> ✅ **A13 已在新批次真跑取数（2026-11-02，owner 授权）**：新批次跑在**隔离目录**
> `output/a13/batch`（camofox 面、agent 模式）——**8 份存档 / 713 步 / 读调用 224**
> （interactive 90 · snapshot 80 · links 54，**读步占 31.4%**，每档 15–51 次；
> 老批次 68 份存档恒 0 ⇒ 回填不了），**同一页面不同工具的回执可区分**
> （62 个落地页里 **41 个用过 ≥2 种读工具**，每个 (页面, 工具) 组合
> 只有唯一回执形状、冲突 0）；新批次 T 视图 `tools_unused` 从 **10 降到 7**
> ——三条读原语**已不在其中**。详见 **M-67「A13 实施状态」**。

### H-21 · 原生 think 被事后补记无条件覆盖

**代码事实**（三行，缺一不可）：

| 位置 | 事实 |
|---|---|
| [`agent_loop.py:1313`](../../executor/agent_loop.py#L1313) | agent 决策时 `attach_rationale(index, _think_payload(instr.think))` ⇒ `reason="native-think"` |
| [`orchestrator.py:1305`](../../executor/orchestrator.py#L1305) | 访问结束后 `await self._write_rationales(...)`——**对每个候选站无条件调用，不分 `executor_mode`** |
| [`actions.py:318`](../../executor/actions.py#L318) | `attach_rationale` 是 `replace(...)`，**没有「已有则不写」的守卫** |

**读数**：生产面（camofox-1，`executor_mode=agent` **66/66**）4852 步里
`reason == "native-think"` **0 条**；writer 产物 **4850**、writer 失败标记 **2**。
P2 的 `rationale_era=pre_action` **65/65**——**口径写着「动作前」，来源却是动作后补写**。

**影响**：交付语料的「思考」= agent 选的动作 + **另一个 LLM 事后配的理由**。
训练侧学到的是「编一套理由解释已发生的动作」，不是探索者的决策过程。

**修复建议**（R0）：① `_write_rationales` **跳过已有 `native-think` 的步**
（补记只兜 scripted 路径）；② `attach_rationale` 加守卫（双保险）；
③ `reason` 字段保持可辨两个来源（`native-think` / `""` / 失败标记），
取值集合由**契约测试**钉住。

**验收条件**（A1）：新批次 agent 模式 P1 里 `reason == "native-think"` 的步占比 **≈100%**；
契约测试：先挂 native-think 再补记 ⇒ **仍是 native-think**。

**实施状态（2026-11-02）**：R0-1 / R0-3 / R0-4 **代码已落地，未闭环**——
本条的 A1 要新批次读数，旧存档（0 条）不回填。

| 落点 | 做法 |
|---|---|
| [`orchestrator.py`](../../executor/orchestrator.py) `_write_rationales` | 跳过 `actions.is_native_think(step.rationale)` 的步（R0-1） |
| [`actions.py`](../../executor/actions.py) `StepLog.attach_rationale` | `is_native_think` ⇒ 直接 return（R0-3 双保险）；writer 自己的产物（`reason=""`）仍可重写 |
| [`executor/cli.py`](../../executor/cli.py) `_pending_steps` | **离线旁路**：`regenerate-rationales` 两种口径都先剔除 `native-think` 步——该命令直接改 JSON，绕过 `attach_rationale` |
| `actions.NATIVE_THINK_REASON` | 单一常量（R0-4），`agent_loop` / `orchestrator` / CLI 全引用它 |

契约测试 `test_think_payload_contract.py` 钉住 `reason` 取值集合（成功只有
`"native-think"` / `""`，失败必须带原因）；变异测试三条（补记覆盖原生 think /
history 退回单元级 / `attach_rationale` 覆盖原生 think）实测 **RED**。

### H-22 · 「没有历史」与它自己的上下文打架

**代码事实**：[`orchestrator.py:1421-1425`](../../executor/orchestrator.py#L1421) 给补记传
`history=[(动作, 观察), … for s in steps[:index]]`，而 `steps` 是**当前站点单元**的 StepLog；
[`agent_visit.py:97-102`](../../executor/steps/agent_visit.py#L97) **每个候选站新建 AgentLoop**
（`self.messages` 从零起）⇒ 补记者**看不到本 run 前面的站点**。
组装时 `to_task_messages` / `to_messages` 又把各单元拼成一条 **run 级**对话。

**读数**：生产面 **235** 步含「没有历史 / 首次操作」措辞，其中 **225（95.7%）前面
已有 ≥1 条观察消息、202 步前面已有 ≥3 条**；全三面合计 **346**。
高频片段：`首次操作` **135** · `没有历史` **61** · `没有页面观察` **22** · `没有页面内容` **19**。

**影响**：模型被教「无视对话历史、每步重新开始」——**与它正在读的那段对话直接冲突**。
这不是措辞问题，是**两个作用域被缝在一起**（补记按单元、交付按 run）。

**修复建议**（R0-2）：history 改 **run 级**（摘要式：只带 `(动作, 观察要点)`，不带全文），
**或**在单元首步**禁写「首次 / 没有历史」这类措辞**。⚠️ 别动「每站一个 AgentLoop」
——那是设计（一个站一个会话）。

**验收条件**（A2）：「无历史措辞 × 上下文确有历史」= **0**。

**实施状态（2026-11-02）**：R0-2 **代码已落地，未闭环**——A2 要新批次读数，
旧存档（235 步）不回填。两处改动：

- [`orchestrator.py`](../../executor/orchestrator.py) 新增 `_rationale_history(record)`：
  history 改 **run 级**（搜索步 → 中间页 → 已完成站点），站点单元调用点传
  `history_prefix`；窗口 `RATIONALE_HISTORY_MAX_STEPS = 40` 步。
  体积由 writer 既有的 `render_history` 兜住（**只有最近 5 步带**瘦身观察，
  正文另有 `HISTORY_BODY_CHARS` 截断）⇒ 方案风险 R-2 不需要另写摘要层。
- [`rationale/writer.py`](../../rationale/writer.py) `SYSTEM` 增第 5 条：历史里
  有此前步骤时**不得**把本步说成「首次操作 / 没有历史」。

⚠️ 「每站一个 `AgentLoop`」**未动**——那是设计（一个站一个会话），改的是
**补记者看到的历史**，不是 agent 的会话边界。

### H-23 · 终局验收不读最终回复

**代码事实**：[`docs/contracts/P3-views.md:63`](../contracts/P3-views.md) 判定 7 只判
`outcome`、**不读回复文本**——这是**契约里的明文设计**，不是实现 bug。

**读数**：camofox-1 里 `output_contract.min_urls ≥ 1` 的样本 **49** 份，最终答复含 URL 的
只有 **5** ⇒ **44/49 = 89.8% 违约**；而 `gate` 判 `passed` **102/131**、
`observation_complete` **65/65**（它量的是观察齐不齐，不是答复兑不兑现）。

**影响**：`output_contract` 在训练语料里**只是装饰**。模型学到「任务说必须给网址，
不给也能通过」。**这是本文这一组里唯一需要动契约文档的一条**。

**修复建议**（R2-3）：终局验收**读最终回复**——契约要求 ≥1 URL 而答复没有 ⇒
**不许判 `passed`**；不满足的要么改答复、要么降级、要么剔出训练集。
✅ **Q-27 已裁（2026-11-02）：要读** ⇒ 按本步改判定 7（不再等裁定）。

**验收条件**（A4）：契约要求 ≥1 URL 的样本，最终答复含 URL 率 **100%**。

### H-24 · 收尾是内部记账语言

**代码事实**：[`assembler/views.py:422-470`](../../assembler/views.py#L422)（P3）与
[`assembler/task_view.py:370-430`](../../assembler/task_view.py#L370)（T）各合成一段收尾——
P3 `任务总结：{reflection.conclusion} 处置：{disposition}。`、
T `任务总结：处置：{disposition}。分支分布：{by_branch}。`。**两侧刻意不同**（INC46）。

**读数**：末条 assistant 是代码合成的「任务总结」——P3 **131/131**、T **65/65**；
[`branches.py:80`](../../executor/branches.py#L80) 的 **deprecated** 分支
`login_wall_or_blocked` 直接出现在 T 的收尾里（例 `login_wall_or_blocked=2 · no_play_control=1 · …`）。

**影响**：分支枚举名 + 计数是**并池决策口径**，不是助手语言；deprecated 分支
更不该教给模型。两侧不一致还让「同一条轨迹」在两份交付物里有**两个结尾**。

**修复建议**（R2-1/R2-2）：收尾改**面向用户的答复**（找不到 + 已排除的站点 + 原因），
**不出现分支枚举名 / 计数 / deprecated 分支**；两侧**同一句话**，差异只放附加信息
（✅ **O-5 已裁：两段投影必须一致 ⇒ 收尾同一句话是交付要求**，不是排版偏好）。

**验收条件**（A3）：训练集收尾里分支枚举名出现次数 = **0**；P3 与 T 末条 assistant 语义一致。

**实施状态（2026-11-02）**：✅ **R2-1/R2-2/R2-3 代码已落地**。
- **[`assembler/closing.py`](../../assembler/closing.py) 是收尾文本唯一真源**：
  `closing_text(facts)` / `closing_for_run(...)`；P3（`views._closing_text`）与
  T（`task_view._closing_text`）调**同一个函数**，per-Run 之后**输入事实也
  一样**（整条 run 的站点清单 + 同一个决定性结论 + 同一个轨迹级 reflection
  结论）⇒ 两侧**逐字相同**。轨迹级结论的取法（**末个写成单元**的
  `reflection`，剔后取）真源在 `closing.last_written_reflection`——P2 的
  `reflection` 字段与 T 的收尾走同一份判断（2026-11-04 补：此前 T 不读
  reflection，LS 卡面收尾缺模型结论）。两侧的
  `_CLOSING_DISPOSITION` 变成 `closing.CLOSING_DISPOSITION` 的**别名**（同一对象）。
- **文本形态**：成功「找到了《片名》的可播放网址：<url>。」；失败「没有找到
  《片名》的可播放网址，已排除的站点：a.com（原因）、…。」——**只写 host**
  （失败答复里带 `http` 会让「答复含可播放网址」的验收失去意义）；
  未知分支 →「原因未能确认，排除」，**不写分支名**；**分支计数 / 分支分布不再输出**。
- **R2-3 接进 P2 `gate`**（`schema._apply_output_contract` + 新模块
  [`assembler/contracts.py`](../../assembler/contracts.py) 按 `task_id` 读
  `taskgen/data/tasks.yaml` 的 `output_contract`）：站点单元 + 合同
  `min_urls=N` + 收尾答复里可交付网址 **< N** ⇒ `passed` 降 `failed`
  （理由「终局答复未兑现输出契约：合同要求至少 N 个网址，答复里只有 M 个」）；
  只降 `passed`（`failed` / `not_run` 不改写）；任务库读不到 / 未知 task /
  合同无 `min_urls` ⇒ **判据不适用，判定原样**；搜索单元不判。
- **存量读数（只读，未落盘）**：camofox-1 合同样本 **49** 条中 **44** 条
  的答复没有可交付网址（5 条有）——与审计 **44/49** 是同一批；这 44 条
  在新口径下会由 `passed` 降 `failed`（方案 23 风险 R-3 的预期）。
  P3 **131/131**、T **131/131** 收尾里分支枚举名 = **0**、`分支分布` = **0**。
- **门禁**：全量 **4654 passed / 45 skipped**；新增
  [`tests/unit/test_closing.py`](../../tests/unit/test_closing.py) 30 条 +
  R2-3 用例 7 条（`test_assembler.py::Test终局验收读回复`）+ P3/T 契约各若干。
- **未闭环**：**A4 未取数**（存量 P2/P3/T 未重建、不回填）；A3 已取数 ✓。

### H-25 · 观察整页快照 + 死循环步

**代码事实**：回执由 [`assembler/observation_view.py`](../../assembler/observation_view.py)、
[`views.py:123-185`](../../assembler/views.py#L123)、
[`task_view.py:180-234`](../../assembler/task_view.py#L180) 渲染整页
（标题 + 网址 + 正文 + 元素表 + 链接全量）。

**读数**：单条回执 **1000~3000** 字、任务相关不足 **10%**；「相邻同动作 + 观察逐字相同」
**469** 处；有重复动作的视图 **64/65**、重复步 **1558/4071 ≈ 38%**；
`goto` **1703** 次里只有 **1183** 个唯一 URL，59/65 视图重复同一 `goto` ≥3 次（最多 **19** 次）。

**影响**：噪声压倒信息；重复动作在教「再点一次」。

**修复建议**（R1 / R3-1 / R3-2）：回执改「标题 / 网址 / **任务相关正文摘要** /
限量元素表 / 限量链接」，导航噪声（热搜榜 / 备案号 / 页脚 / 广告 / 推荐流）**不进训练文本**；
「相邻同动作 + 观察逐字相同」的步**剔出或显式标记**；同一 unit 内重复动作 ≥2 次即标记。
⚠️ **P1 存档全文一个字不动**（存档是证据，文本是语料）；
⚠️ **降级标记保留**（`[采集降级: body_text]` / 「本步没有取到页面内容」是 fail-closed 的一部分）。

**验收条件**（A5/A6/A7）：单条回执中位数 ≤ **800** 字 · 相邻同动作同类步 = **0** ·
**同 unit 内同动作 + 观察逐字相同的重复步 / 保留步 ≤ 10%**（A7 口径于
2026-11-02 由 owner 裁定为方案 A，见 **Q-31**；宽口径 38% 只作审计基线）。

**实施状态（2026-11-02）**：✅ **R1-1/R1-2/R1-3 + R3-1/R3-2 代码已落地**。
- **[`assembler/observation_view.py`](../../assembler/observation_view.py) 重写**：
  正文摘要 **320** 字 / 元素 **10** / 链接 **4**；剔噪三规则（整行导航词
  `NAV_LINE_WORDS` · 备案/页脚/广告 `NOISE_LINE_RE` · 短热搜编号 `HOT_LIST_RE`）；
  正文按「`keep` > 片名 > 播放/元数据关键词」占预算、按原行序拼回。
  `limits` 扩成六计数 + `cropped`（上限 vs 噪声/去重分列，`None` = 未裁）。
- **动作目标保真**：`builder._observation_keeps` 把下一个可见动作的目标
  （点击名 / 导航网址）算成 `keep`，`render` 无条件保留——否则训练样本里会
  出现「点了一个观察里没有的东西」。T 不读 P2，`task_view` 自己再算一遍。
- **R1-2 动作无效**：`Sample.step_errors` ← P1 `steps[i].error` →
  观察 dict `action_error`（**不混进 `degraded`**）→ P3/T 渲染
  `[本步动作无效: …]`（前 120 字）。
- **读数（A5）**：camofox-1 **4071** 条可见步（改前/改后同一批、排除
  infrastructure）回执中位 **3026 → 772** 字（p75 883 / p90 1319），
  ≤ **800** ✓；成分中位 head 84 / body 320 / 元素 160 / 链接 161。
- **目标保真**：带 `keep` 的 **2821** 条：`kept` **1978** · **LOST_FROM_RAW 0** ·
  `never_observable` **843**（目标本来就不在观察里，不是被裁）。
- **门禁**：4609 passed / 45 skipped；新增 `test_observation_limits.py` **15** 条 +
  P3/T 契约各 **1** 条。
- **R3-1 / R3-2 代码**（2026-11-02 同批）：新增
  [`assembler/screen.py`](../../assembler/screen.py)（动作签名规范化 + 相邻死循环
  剔出 + unit 内重复标记）；P3 `views._screened_steps` 与 T
  `_screened_task_steps` 接入，**两段投影共用同一份筛选结果**（O-5）。
  meta 新增 `deduped_step_count` / `deduped_steps` / `repeat_step_count` /
  `repeat_actions` 四栏（逐条列原始步号，不合成计数）。
- **读数（camofox-1，65 份，临时脚本重算存量 P2/P1，未落盘）**：
  - **P3**：可见步 **4071** · 原重复步 **1558（38.3%）** · 剔死循环 **161** ·
    保留 **3910** · 筛后重复步 **1397（35.7%）** · **残留相邻死循环 0**（A6 ✓）。
  - **T**：可见步 **4071** · 剔死循环 **143** · 保留 **3928** · 筛后重复步
    **838（21.3%）** · **残留相邻死循环 0**（A6 ✓）。
  - 两段剔的步数不同是**观察渲染不同**（T 的 `keep` 依赖下一步动作），不是
    各自算错；两段各自的「残留死循环」都是 0。
- ✅ **A7 ✓（2026-11-02 owner 裁定方案 A，见 Q-31）**：口径改绑**窄口径**
  「同一 unit 内同动作且观察逐字相同（不要求相邻）的重复步 / **保留步**」，
  目标仍 ≤10%。**存量读数（同一批 65 份，临时脚本重算，未落盘）**：
  - **P3**：保留步 **3910** · 窄重复 **274** ⇒ **7.0%** ✓
    （原件数 435/4071 = 10.7%，其中相邻 161 已由 R3-1 剔）；
  - **T**：保留步 **3928** · 窄重复 **98** ⇒ **2.5%** ✓
    （原件数 241/4071 = 5.9%，其中相邻 143 已剔）。
  - **宽口径**「同动作不看观察」**35.7%（P3）/ 21.3%（T）** 保留为**审计基线**，
    不再是 A7 验收线——1558 条重复里大部分是**同动作不同观察**（页面变了），
    删掉会**破坏观察因果链**（下一步看到的前置观察会变）。R3-2 仍**只标记不剔**
    （`repeat_actions` 按宽口径逐条记，供人核与批次筛选用）。
- **未闭环**：存量 P2/P3/T 产物**未回填**——新口径在下次
  `cli build --overwrite` / `cli task-view --all --overwrite` 后生效。
- **门禁（R1+R3 合计）**：**4681 passed / 45 skipped**；新增
  `test_screen.py` **17** 条 + P3/T 侧筛选用例各 **5** 条。

---

**已裁章节标题占位（正文已删，防锚点 404；正文见 `git log -p -- 10-TODO台账.md`）** —— 以下标题是**外部文档的锚点引用目标**：

## 2026-11-04 数据质量复核（第 1、2 项）· 新增开口项详解

> **触发**：合成数据质量复核报告里的**第 1、2 项**（第 3 项「输出契约 / 点击歧义 /
> 裁剪冗余」owner 说暂缓）。**这一轮既改代码又动产物**——完整经过见
> [编年史 §增量一百二十三](90-增量编年史.md)。

### H-26 · agent 路径的前置事实核补上了，判据缺口同时显形

**已闭的那半边（代码 + 测试 + 记录三件齐）**：

| 项 | 内容 |
|---|---|
| 实现 | [`agent_loop.py::_guarded_branch`](../../executor/agent_loop.py)（折算分支 → 过 `branches.precondition_failed` → 不成立就落 `unresolved` + 追「[记账降级] …」）· 落点统一在 `_close_on_verdict` |
| 判据单一真源 | [`branches.py::precondition_failed`](../../executor/branches.py)（原 `_precondition_failed`，本轮**改成公开**——它现在有**两个**调用方；scripted 路径的 `RunLedger.record_from` 与 agent 路径共用**同一份**） |
| 测试 | [`tests/unit/test_agent_loop.py::Test分支前置事实核`](../../tests/unit/test_agent_loop.py) **5 条**：导航栏 CTA ⇒ `unresolved` · 真付费墙仍 `subscription_wall` · 门禁事件分支 == outcome 分支 · 降级理由进 `reason` · 登录墙不受影响 |
| 记录 | 编年史 §增量一百二十三 §一 |

**还开着的两半**（**不因代码落地而闭**）：

- **① 真付费墙会被降级**：`paywall_evidence`（结论口径）比 `has_strong_paywall_signal`
  （拦截口径）严得多 ⇒ **实测对部分真付费墙返回空**（youku《功夫》· fun.tv）
  ⇒ 这些站现在落 `unresolved` ⇒ **不进池** ⇒ **真负样本被降成非负样本**。
  ⚠️ 这与 CLAUDE.md「**每条分支都必须有对应样本入库**——负样本不是副产品」是**直接冲突**的
  ⇒ 要么放宽 `paywall_evidence`（但那正是 H-16 修复①**特意收紧**过的），
  要么给「付费墙」单开一条**更窄的结论口径**——**两个方向都改判据，需 owner 拍**。
  ✅ **owner 已拍并已落地（2026-11-04）**：走第二条路——**给「付费墙」单开一条更窄的结论口径**
  （判据二），**不动** `paywall_evidence`。详见 [§H-26 补记](#h-26--agent-路径的前置事实核补上了判据缺口同时显形)。
- **② `login_required` 没有「可执行」的前置事实核**：今天真的在核的只有两条分支——
  `subscription_wall`（门禁语境 `paywall_evidence`）与 `component_unverified`（同页守卫）；
  `BRANCH_PRECONDITION` 里 `login_required` / `blocked` / `partial_free` / `broken`
  **只有文案、没有可执行核**（2026-10-19 增量六十·方案 X **刻意**窄的：守卫当时覆盖 5 支，
  实测 **46** 次触发**全是误伤** ⇒ 窄到只剩 1 支）⇒ **代价**：`login_required` 在 agent
  路径上**仍是裸折算**。⚠️ **仍开着**（owner 本轮只裁了付费墙这一支）。

### H-26 补记 · 判据二「订阅价目门禁」落地（2026-11-04 · owner 拍板）

**owner 原话**：「第 2 项的副作用，给这一支补第二条可执行判据。」
⇒ 走的是「**给付费墙单开一条更窄的结论口径**」，**不是**放宽 `paywall_evidence`。

| 项 | 内容 |
|---|---|
| 判据二 | [`rule_perceptor.py::paywall_price_evidence`](../../perception/rule_perceptor.py)：**页面级三要素同现**——① 订阅价目（`PRICE_PATTERN`）② 开通动作（`PAYWALL_PRICE_ACTIVATION_PHRASES`）③ 权益承诺（`PAYWALL_BENEFIT_WORDS`）；**少一样都不成立**。**外加登录门排除**：页面明写「请 登录 后继续观看」⇒ 那是登录墙（`login_required`），判据二**不成立** |
| 生效面 | **只**在 `subscription_wall` 的前置事实核（[`branches.py::precondition_failed`](../../executor/branches.py)）——判据一 ∪ 判据二（[`subscription_wall_evidence`](../../perception/rule_perceptor.py)） |
| 不动的部分 | `paywall_evidence`（**H-16 修复① 的收紧口径原样**）⇒ LLM 救济路径与评分器口径**不变**；纯营销模块（实测「新用户首月9.9元，开通会员 立即抢购」）**不得**靠判据二变成付费墙——这条被单测钉住 |
| 测试 | [`tests/unit/test_subscription_wall_criterion2.py`](../../tests/unit/test_subscription_wall_criterion2.py) **13 条**：五个验收样例（youku《功夫》· iqiyi《琅琊榜》· fun.tv · youku 现场版 · v.qq）· 登录门排除 · 营销不成立 · 前置事实核端到端 |
| 实测 | [`research/probes/subscription_wall_criterion2_measure.py`](../../research/probes/subscription_wall_criterion2_measure.py)（**判据口径与生产同源**，脚本从 `rule_perceptor` import，不另抄词表）⇒ 存档 **2339** 行：原 `subscription_wall` **196** 行中判据一**空串 145** 行 ⇒ **判据二找回 67** 行（youku 45 · iqiyi 15 · fun.tv 4 · mgtv 3）· **登录门排除挡下 21** 行 · 验收样例 **6/6** 符合预期 · 金标 `subscription_wall` **119** 行：并集覆盖 **113**（判据一 101 · 判据二**新增 12** · **0 条丢失**） |
| 误伤读数 | 判据二在**非** `subscription_wall` 的存档行上命中 **557** 行（iqiyi **112** · baidu **102** · hao123 **73** · sohu **64** …）——⚠️ **这些行压根不会走到 `subscription_wall` 的前置事实核**（它们已经是 `unresolved` / `not_play_site` / 搜索引擎落地页）⇒ **不是真实假阳面**；真实假阳面只有「本来就是订阅墙却判据二不成立」的 **14** 行（且它们**仍由判据一覆盖**） |


### H-27 · 改分支却没改中文标签（三个写者全修 · 存量已回填 · LS 两条卡是旧的）

- **工具已修**（**三个写者**，不是一个）：`downgrade.apply_downgrades` ·
  `relabel._relabel_outcome` · `review_queue.apply_verdicts` **都改 `branch_label`**，
  且**共用** `branches.branch_label_of(branch)`（`None` → 「成功」）——
  标签的取法只有一处，三个写者各抄一份就会漂。⇒ 单测：
  [`test_downgrade.py::Test处置::test_标签跟着分支走`](../../tests/unit/test_downgrade.py) ·
  [`test_relabel.py::Test处置::test_存档侧标签也跟着改`](../../tests/unit/test_relabel.py) ·
  [`test_review_queue.py::TestApplyVerdicts::test_人裁改了分支标签也跟着改`](../../tests/unit/test_review_queue.py)。
- **存量已回填**：新增 [`executor/label_sync.py`](../../executor/label_sync.py)
  + `cli sync-labels`（**默认 dry-run**）⇒ 生产根 **137** 条（按层：原档 **61** +
  复核档 **76**；按来源：降级/改判 **108** + 人裁 **29**）·
  72 份文件；双跑研究档 `dualrun*/camoufox` 另 **8** 条 ⇒ **生产根复扫 0 条残留**、
  **池内 0 条同源矛盾**、**T 视图 155 份 / 1178 条 `unit_outcomes` 0 条同源矛盾**
  （另有 **34** 处**口径文本漂移**，**按惯例不回填**）。
  ⚠️ **判据是「分支被工具改过」**（`branch_before_downgrade` / `branch_before_relabel` /
  `branch_before_review` 三个留痕键），**不是**「标签文本与当前 `BRANCH_LABELS` 对不上」——
  后者是**口径演进**（`unreachable_hard` 2026-10-27 收窄 · `login_wall_or_blocked`
  2026-10-12 加 deprecated 后缀）⇒ 按 `executor/branch_eras.py` 的 e6 **不回填**
  ⇒ 那 **47** 条**没动**（原档 47 · 复核档同样 47 ⇒ 两层镜像 **94**）；**备份目录**（`q23_trial` 151 · `q23_backup_2026-10-28` 37 ·
  `pipeline/_backup_pre_motivation` 27）也**没动**——备份是当时状态的快照。
  ⚠️ 与「历史存档**不回填**」的既有惯例不同：这里不是**口径换代**，是**工具自己造的自相矛盾**
  （分支与标签不同源）⇒ 修它是**修事实**，不是改口径。
- **LS 项目 77 的两条卡是改判前推的**：**601**（`T001__7d5a03bd` · 2026-11-02）·
  **665**（`T001__61eabe2e` · 2026-11-04T02:24Z）⇒ 卡上 `meta_text` 仍写着那条
  `subscription_wall`。**重推 / purge 都是对外动作**（`purge` 连标注一起删）⇒
  **本轮没动**，等 owner 裁。
  ✅ **owner 已裁并已执行（2026-11-04）**：「LS 项目 77 的两条卡是改判前推的，重推。」
  ⇒ **就地重推**（`PATCH /api/tasks/{id}` 覆盖 `data`，`PATCH /api/predictions/{id}`
  覆盖风险提示）⇒ **不删卡、不新建卡** ⇒ task id（601 / 665）与标注归属不变、
  项目仍是 **77** 条（**0 条重复** sample_id）⇒ 两张卡的 `site-8` 分支现在读 **`unresolved`**
  （卡面 `meta_text` 里剩下的 `subscription_wall` 字样只在**审计留痕**里：
  「`[原判 subscription_wall]` paywall 二次拦截…降为 unresolved…」）。
  ⚠️ **601 的卡面口径也翻了**：当前 `scout/r51` 聚合档是 **undetermined**
  （65 条 per-Run 行全是「任一 unit score=None ⇒ 任务级 None」）⇒ 卡面 `status`
  由 `positive` 翻成 **`quarantine`**、评分卡改成「四维未判出，请人工复核」——
  这是**当前产物本身的样子**，不是手工改写。台账
  `push_index__77.jsonl` 追加了 2 行（append-only，同 `sample_id` / 同 id）。

## 2026-11-05 执行耗时压缩（T002 单条 130.5s 复盘）

> 触发：owner 要求「审查这条数据执行时间消耗，分析哪些时间可以压缩、去掉」，
> 并按 P0 → P1 → P2 顺序落地。基线：`T002__4b181a6d` 一条 130.5s
> （分类 ~38s、reasoning 8~9s/步、搜索补记与反思串行、基础设施步也补写
> rationale）。本节记**已做 / 未做 / 为什么**。⚠️ **收益读数不在这里预先填**——
> 要等下一批真实运行后逐条读 `llm_calls` 才算数。

### 已落地

| 项 | 内容 | 落点 | 测试 |
|---|---|---|---|
| **P0-1** | `llm_calls` 从「只圈 agent_loop」扩到**全 caller**（`agent_loop` / `percept` / `rationale` / `reflection`）：带 `latency_ms` / token / `error` / `started_at`；**失败调用也记**；`ts` 单元级盖戳 + `_seal` 兜底 | `executor/orchestrator.py`（`_record_writer_call` / `_bind_perception_candidate` / 盖戳）· `perception/llm_perceptor.py`（`_ask` 逐次记账）· `rationale/writer.py`（`WriteResult` 成本读数，**不进 `to_json`**）· `executor/cli.py`（write/reflect 拆客户端 + `last_call`）· `llm/client.py`（`last_latency_ms` / `last_usage` / `last_call_error`） | `test_visit.py::test_写手调用都进llm_calls账本` · `test_llm_perceptor.py::Test调用账本` · `test_writer.py::Test调用读数` |
| **P0-2** | 基础设施步（`origin="infrastructure"`，如 `new_tab`）**不生成 rationale**，落显式 `status="skipped"`；`gate_of` 把它排除出「没写成」、单列理由、分母也排除 | `executor/orchestrator.py::_write_rationales` · `assembler/schema.py::gate_of` | `test_visit.py::test_每一步都生成` · `test_assembler.py::Test基础设施步的rationale豁免` |
| **P0-3** | 非终局动作**强制原生 think**：提示词规则 + `_validate` 校验；缺失时与工具/参数/ref 问题**合并成一条引导**，不额外烧重试 | `executor/agent_loop.py`（`_GUIDANCE_NO_THINK` / `_validate` / `_rules_of`） | `test_agent_loop.py::Test非终局动作必须带think` · `Test记账范围只认非终局动作` |
| **P0-4** | 搜索单元的逐步 rationale / 终局 reflection 与**分类**并发：三者都扔 worker 线程（`asyncio.to_thread`）；分类不在线程里跑会**堵住事件循环**，任务根本起不来。早退路径逐个 `await` 收口 | `executor/orchestrator.py`（`create_task` + `classify_task` + `finally`） | `test_visit.py::test_分类与补记真并发`（线程事件判据，不靠 sleep） |
| **P1-5** | `CLASSIFY_SEARCH_LINKS` 的 `schema_hint` 去掉 `title`——解析端 `_resolve_rows` 从不读它，逐行复述标题只烧输出 token | `perception/questions.py` | `test_llm_perceptor.py::test_分类题不要模型逐行复述标题` |
| 顺带 | `camofox-1` 读原语转 `exercised=True`（T002 真实调用已落盘）；退役面 `camoufox-1` 用 `dataclasses.replace` 保持 `False`——`exercised` 是**逐面**口径，声明体仍同源 | `toolcatalog/__init__.py` | `tests/contract/test_tool_catalog_contract.py`（185 条全绿） |

### 未做 / 为什么

- **P1-6 资源阻断（图片/字体/媒体不加载）：上游不可用。** camofox-browser 的
  11 tool MCP 面没有 block/route 工具；`goto` 的 `wait_until` 在本仓库调用面
  **根本没传**，改它也阻断不了子资源。要做得改上游或换 REST 直连自己管 route，
  不是本树单方面能做对的事。
- **P1-7 点击后观察的等待：不改。** 那 8.65s 含**必须**的点击后观察
  （`_settle` 有界等 commit 2s + 读页）。砍掉等的是「读到一半」——
  2026-10-22 实测过渡态 15% 的教训；观察是采集目标，不是开销。
- **P2 观察压缩/分页：方案 23 R0 未裁定前不动。** owner 已裁「观察选择权
  还给 agent」（O-1），但 R0–R5 本体未实施；单方面压缩会改训练契约。

## 校准回路（方案 16）· 未闭环

### Q-14 · D-3 的 `playable` ↔ `nav_aggregator` 两个定义在这一类页面上同时成立

### M-29 · D-5 的 v7 快照 pred 是修复前的，别拿它当基线

### M-30 · 金标在同一个 URL 上不自洽（19/131）

### M-67 · 采集面的观察选择权与任务约束都没落地

## 2026-10-30 软维度金标：回归集首判 + 「判不了」回真源（补记）
