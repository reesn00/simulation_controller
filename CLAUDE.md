# CLAUDE.md

## 项目

采集**可归因**的 Agent 浏览器操作轨迹：任务生成 → 代码执行 → 语义感知 → 样本组装 → 评估。
核心主张：**代码层做护栏、LLM层做探索、persona 管输入分布**。

仓库里有**两代架构并存**：

| | 位置 | 状态 |
|---|---|---|
| **v2 轨迹合成管线** | `trajectory_pipeline/` | **当前唯一开发目标**。全部新功能、新修复写在这里 |
| **v1 存量管线** | `archive/v1/`（原 `simulate_serve/` `orchestration/` `gdr/` `etl/` `label_studio/` `tool_runtime/` `data_refiner/`，2026-10-09 归档） | **冻结**。只读参考：可读代码抄思路，**不可 import、不可修改** |

一句话区别：v1 是「**远端 QwenPaw agent 当驾驶员**，本地只当模拟用户 + 裁判」；
v2 把那颗心脏换掉——「**本地 agent runtime 当探索者、本地代码当护栏、camoufox 当浏览器**」。
v2 要采集的是**可归因的证据**，而 v1 的观察经远端 LLM 转述后锚会被削弱。

---

## ⚠️ 开发纪律（先读这一节，违反即视为架构回退）

1. **新代码一律写入 `trajectory_pipeline/`。** 任何新模块、新 CLI、新测试、新文档都落在新树内。
2. **存量仅供功能参考——禁止 import、禁止修改。**（存量代码归档至 `archive/v1/`，纪律不变。）需要存量某个能力时的正确姿势是**照着读一遍然后在新树重写**，而不是 `from simulate_serve.x import y`。
3. **两处例外（不是破例）**：仓库根 `pyproject.toml`（`testpaths` 必须含 `trajectory_pipeline/tests`）与 `.gitignore`（`trajectory_pipeline/output/`）。这两处是构建/门禁接线，改它们是为了让新树能被门禁覆盖、产物不入库。
4. **跨树/外部能力走进程边界**：MCP 协议 / HTTP，与 Python 导入边无关。这条让「不 import 存量」与「能复用外部能力」同时成立。
5. **产物隔离**：新树产物全部落 `trajectory_pipeline/output/pipeline/`。
6. **训练侧工具声明只有一个真源**：[`trajectory_pipeline/toolcatalog/`](trajectory_pipeline/toolcatalog/)。P2 的 `six.tools`、P3 的 `openai.tools`、T 的 `openai.tools` 三处都是它的**投影**——**不许手抄第二份**（抄两份必然漂：旧口径下 `click` 的 function 定义是空参数，训出来的模型会发空参数的点击）。它挂在 `trajectory_pipeline/` 顶层而不是 `assembler/` 下，是因为 **P3 与 T 互不 import**（AST 强制），工具清单得有个两边都能用的地方。改工具清单先改 catalog，再 `cli build --overwrite` + `cli task-view --all --overwrite` 重建产物。
7. **工具声明按「面」取，面由 P1 的 `driver` 一对一折算**（2026-10-20 方案 15 §5.5）。`click` 在 obscura 面是 `{tag,label}`、在 camoufox 面是 `{role,name}`——**同名不同参**，拿错面就是「声明与真实调用对不上」，而报告上看不出异常。面**不手填**（P1 `RunRecord.driver` → `surface_of_driver()` → P2 顶层 `tool_surface`），**认不出的 driver 直接抛**——不猜、不静默回落。

### 跨两棵树的红线

- **凭据不得提交、打包、复制到测试、文档或日志。**
- **不保存 raw CoT / Cookie / Authorization Header / 浏览器 Profile。**「思维链」按加工状态分两类：**raw CoT**（未经 gdr 精修的原始输出，受此红线约束）与 **refined CoT**（经 `thought_refactor` 精修的训练制品，**不受限**——CoT SFT 需要它，不要「修复」掉）。
- **fail-closed**：能力不可用时返回 `answer=None`，**绝不猜**；且 **`None` 不中断采集，只有 `False` 才中断**——fail-closed 管的是「结论」不是「采集」。
- **禁止投票/集成**，分歧必须暴露给人看。

### 控制流铁律：LLM 层做探索，代码层做护栏

**探索权在 agent runtime，护栏与调度在代码。两者分界是五样东西——一样都不许交出去：**

| | **执行器（代码）** | **agent runtime（LLM）** |
|---|---|---|
| 拥有 | 浏览器会话、红线闸门、预算、拦截与处置、分支记账、终局验收、落盘 | 动作选择、**看什么**、终局自报 |
| 没有 | 动作选择权 | 分支决定权、成功判定权、会话管理权、批次调度权 |

**代码必须保留的五样（归因地基）**：步数/llm_calls/超时预算 · 终止判定 · 处置映射 · 分支记账（22 类封闭集，agent 永不直接产分支）· fail-closed。

**感知层不再是「传感器」**，退位成三块静态资产：判断题文本（进 agent 任务指令）· 分支记账映射表（不动）· 规则检测器（进 gatekeeper）。它在热路径上只剩一个岗位——**终局验收**（④ `PLAYER_OK`：agent 报「到了」，代码判「真到了吗、播放器起没起」）。

**观察由谁发起，决定它进不进动作流**：执行器推的回执 = 观察（不进训练 `tool_calls`）；agent 主动拉的读 = 动作（进）。

> 旧表述「LLM 是传感器，不是驾驶员」**已作废**——A1/A2/A2b 把站内导航整段交给 agent 之后它就不成立了。完整重定义（谁发起观察、感知层七不变式的处置、工具声明的新形态）见[设计方案 15](trajectory_pipeline/docs/设计方案/15-执行器与agent-runtime重新定义.md)；现状口径见[架构总览 · 控制流与判断点](trajectory_pipeline/docs/01-架构总览.md)。

### 三条容易踩塌的判断

这三条不是风格偏好，是踩过之后写进规范的：

- **每条分支都必须有对应样本入库**——负样本不是副产品。反过来，`unresolved` / `trailer_suspect` / `player_unverified` **不是负样本**，混进池里会污染它。
- **并池是写入时的动作，不是读取时的推导**——新分支（如 `player_unverified`）**不回填历史存档**。批次边界前后分布会跳变，不记批次元数据就会读成「质量变好了」。
- **贴标签前核前置事实**（`branches.BRANCH_PRECONDITION`）——分支名是结论的标签，而定义里带前提。前置事实不成立就降级为 `unresolved`。**这张表要短**，长得跟分支表一样长就没人维护了。

---

## 常用命令

**命令清单只在 [常用命令速查](trajectory_pipeline/docs/08-常用命令速查.md) 维护一份**（一行一条，用的时候再读）。
每条命令的「联网与否 / 作用 / 参数」、环境变量全表、门禁与输出目录、故障排查见 [运行手册](trajectory_pipeline/docs/04-运行手册.md)。
**本文件不抄命令**——抄一份就多一处会漂的地方，要跑命令读上面那份。

> ⚠️ `run` 的 `--title` **缺省即报错**，不拿 `--task-id` 顶替：少了它会搜成 `T001 在线观看`（**轮胎** T001），产出整批理由通顺的假负样本且**零报错**。`OBSCURA_EXE` / `TRAJECTORY_LLM_*` 同样**缺失即报错，不猜**。见[避坑指南 §5.13](trajectory_pipeline/docs/02-避坑指南.md)。

**每次真实运行后都要逐条打开存档读 evidence 核对**——固定动作不是可选项（`cli check-archives` 是它的机械版；**判断「结论对不对」不在门禁里**）。

---

## 文档

### 项目指引（`trajectory_pipeline/docs/`）

| 文档 | 内容 |
|---|---|
| [08-常用命令速查.md](trajectory_pipeline/docs/08-常用命令速查.md) | **命令清单的唯一真源**——一行一条、复制即用，附「缺了就报错不猜」的必填项表。要跑命令先读它 |
| [01-架构总览.md](trajectory_pipeline/docs/01-架构总览.md) | 模块地图 + 依赖方向、控制流与四个判断点、失败分支全集、**camoufox 执行层**（obscura 保留为回溯路径）、感知层七不变式、persona 六维、模块 5/6、**存量 v1 各目录职责** |
| [03-数据契约.md](trajectory_pipeline/docs/03-数据契约.md) | P1/P2/P3 契约表、`steps[]` 三条口径、P1 三处「不给就不可判定」的字段 |
| [04-运行手册.md](trajectory_pipeline/docs/04-运行手册.md) | 每条命令「联网与否 / 作用 / 参数」口径表 + 环境变量全表、门禁与输出目录、`replay` 口径、故障排查（**命令清单本身在 08 号**） |
| [05-现状与验收.md](trajectory_pipeline/docs/05-现状与验收.md) | **索引页**——内容已拆到 [`计划进展/`](trajectory_pipeline/docs/计划进展/)（2026-10-15） |
| [计划进展/](trajectory_pipeline/docs/计划进展/) | **跑到哪儿了、接下来做什么**，按 9 个功能域分册（会过时，用前先重跑命令核对；**不含历史**）。[00-总览](trajectory_pipeline/docs/计划进展/00-总览.md) 是入口，[10-TODO台账](trajectory_pipeline/docs/计划进展/10-TODO台账.md) 是**未闭环项唯一汇总**（每条带记录锚点），[90-增量编年史](trajectory_pipeline/docs/计划进展/90-增量编年史.md) 存增量五十～七十八 |
| [06-端到端流程.md](trajectory_pipeline/docs/06-端到端流程.md) | **一条数据从骨架走到训练样本的完整路径**：四条命令线、P1/P2/P3 流转、模块边界、执行时序 |
| [07-人工复核与判读手册.md](trajectory_pipeline/docs/07-人工复核与判读手册.md) | **人工判读的操作手册**：现在有几件事要人判、每件的「看哪个文件 → 怎么判 → 填哪 → 确认后记哪 → 跑哪条命令」、以及「人工确认完记录哪里」 |
| [02-避坑指南.md](trajectory_pipeline/docs/02-避坑指南.md) | **9 类 74 条**，症状写成终端里真正会看到的那句话，可全文搜索 |
| [tools.md](trajectory_pipeline/docs/tools.md) | **工具调用说明**（总目录）：按工具形态分大类——浏览器 MCP / **Agent 会话工具（QwenPaw 运行时 28 个）** / LLM / 媒体 CLI / 标签平台 HTTP / VLM 等；已收录浏览器 MCP（obscura 参考实现）与会话内 tools 全集（名/参数/取值/描述）；新工具按相同五段式追加；动 `ObscuraDriver` / `mcp_client` / `McpClient.list_tools` 之前必读 |
| [research/](trajectory_pipeline/research/) | **研究产物与复算脚本**（`recollect/` 直采主线 · `retest/` 重测 · `probes/` 探针与 A/B · `ab_baseline/` · `smoke/`）。放**证据**不放结论：脚本 + 它读的 `.jsonl`。2026-10-20 由仓库根 `tmp/`、`tmp_*` 迁入；**必须在仓库根跑**；`outcomes.jsonl` 不可平凡重建（站点已漂移）。[README](trajectory_pipeline/research/README.md) 是入口 |
| [依赖说明/](trajectory_pipeline/docs/依赖说明/) | **外部依赖使用手册**（一依赖一文件）：CLI 全表 / Python API / 关键参数 / **本机实测记录与判据**。已收录 [camoufox.md](trajectory_pipeline/docs/依赖说明/camoufox.md)（stealth 参数、server 模式、**§11 默认值真相**——`humanize`/`geoip` 默认关、`os` 默认随机、uBO 默认装）；**注意它与 [tools.md](trajectory_pipeline/docs/tools.md) 分工不同**——tools.md 记「协议/入参/返回」，这里记「依赖怎么装怎么配怎么验」；D11 修订后执行层目标已换，但**本目录不构成选型变更** |

### 设计方案（`trajectory_pipeline/docs/设计方案/`）

- [00-总体方案.md](trajectory_pipeline/docs/设计方案/00-总体方案.md) —— v2 设计基线：决策 D1–D10、模块设计、四个判断点、失败分支、契约、里程碑、风险
- [01-模块3-可替换感知层.md](trajectory_pipeline/docs/设计方案/01-模块3-可替换感知层.md) —— 插件化机制完整规格 + 七不变式
- [02-rubric与代码对齐.md](trajectory_pipeline/docs/设计方案/02-rubric与代码对齐.md) —— 评分标准 `RUB-VID-PLAY-001` 逐维度 ↔ 新树代码的对齐账。**改 rubric 或改控制流之前先读**——它记的是两侧哪里已经错位，而错位不会自己显形
- [03-rubric修订与实施方案.md](trajectory_pipeline/docs/设计方案/03-rubric修订与实施方案.md) —— rubric v1.1 逐条改稿清单 + 五个代码变更（点文件、点函数）。**§9 是实施结果**（哪条做了、哪条没做、每条由哪个测试封着、方案漏掉了什么）。**要动 `branches.py` / `observation_view.py` / `integrity.py` 的先读**
- [04-camoufox执行层替换方案.md](trajectory_pipeline/docs/设计方案/04-camoufox执行层替换方案.md) —— **D11 修订 D7**：camoufox 取代 obscura 成为唯一执行层。含实测取证（F1–F4）、13 原语映射表、三个难点（ref 机制 / 错误三态 / 身份钉死）、两份红线、P0–P6 落地阶段。⚠️ **§6.1 的 go/no-go 门槛已被 owner 裁定覆盖（2026-10-22）**——生产链默认执行层**已切 camoufox**（`DEFAULT_KIND = "camoufox"`）；§6.1 顶部有覆盖块，六条读数的新地位写在那里。**obscura 没删**（`--driver obscura` 显式点名，工具面 `obscura-1` 只读不再产出）
- [15-执行器与agent-runtime重新定义.md](trajectory_pipeline/docs/设计方案/15-执行器与agent-runtime重新定义.md) —— **D-A6–D-A11**：**LLM 层做探索、代码层做护栏**。感知层退位成三块静态资产，七不变式里 I3（幂等）与 I6（ref 可溯源）退役；工具声明 = agent 工具集 = camoufox 调用面（含采集面 4 条）；含方言轴与批次元数据、落地路线 R0–R5、待决项 O-1–O-4
- [在线视频场景rubric.md](trajectory_pipeline/docs/设计方案/在线视频场景rubric.md) —— 评分标准本体

### 存量 v1（均在 `archive/v1/` 下，按需查阅）

`docs/orchestration-design.md`、`docs/observability-label-studio.md`、`docs/label-studio-playbook.md`、`docs/label-studio-annotation-export.md`、`docs/设计方案/`、`docs/contracts/`、`gdr/docs/`。目录职责见[架构总览](trajectory_pipeline/docs/01-架构总览.md)末节。