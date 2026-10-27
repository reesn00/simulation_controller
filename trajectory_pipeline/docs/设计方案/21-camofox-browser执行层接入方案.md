# 21-camofox-browser 执行层接入方案

> 状态：**已裁定 + 已实施**（2026-10-26）。
> 本文是**接入 camofox-browser 的唯一设计真源**：owner 裁定、逐条判断、
> 落点文件与验收口径。**梳理稿的历史版本**（2026-10-26 上午）留了骨架，
> 本文把 §9「待裁定清单」全部落定，并把「梳理时的事实」与「裁定后的事实」
> 分开写——**后来的事实不覆盖前面的事实**（那是编年史的纪律）。
>
> **配套阅读**：[依赖说明/camofox-browser 使用说明.md](../依赖说明/camofox-browser%20使用说明.md)
> （上游 11 个 tool 的参数与返回形状）·
> [依赖说明/camoufox_弃用.md](../依赖说明/camoufox_弃用.md)（旧执行层的历史手册）。

---

## 0. 速览

### 0.1 一句话

**执行层从 camoufox 换成 camofox-browser**，经它**原生自带的 MCP stdio 适配器**
（`camofox-browser-mcp`，11 个 tool）接入，沿用既有 `PageDriver` 协议与
`toolcatalog` 工具面纪律。**目标编码不变**（仍是 `{role,name}`）——换的是
实现，不是定位子句。

### 0.2 owner 裁定（原文，逐字记录）

> **A1** 工具面增第三张 camofox-1；并且把原来的 camoufox-1 删除。
> **A2** press 驱动缓存合并，不要去改上游 `C:\Users\klpc\workspace\tool\camofox-browser` 源码。
> 确认目标是新增 camofox-browser mcp 接入项目使用，**禁止把原有的 camoufox 接回来**，
> 可以做清理原有的 camoufox 代码与内容。

三条约束的落地口径：

| 裁定 | 落地 | 依据 |
|---|---|---|
| **A1-a 增第三张面** | `toolcatalog` 新增 `SURFACE_CAMOFOX = "camofox-1"` | [§3 D3](#d3-工具面三张面各按实现分) |
| **A1-b 删 `camoufox-1`** | **「不再产出 + 仍读得懂」**（见下） | [§3 D3](#d3-工具面三张面各按实现分) |
| **A2 press 驱动缓存合并** | `camofox_driver.press` 记「最近一次 fill 的目标」，回车时重发同文本 + `pressEnter=true` | [§3 D5](#d5-press-走驱动缓存合并a2) |
| **A2 不改上游** | 上游源码**零改动**；缺口全在本仓库驱动层补 | [§3 D4](#d4-五处协议缺口怎么补) |
| **禁止接回 camoufox** | `camoufox_{driver,session,probe}.py` **已删**；`playwright` **已从主依赖移除**；`cloverlabs-camoufox` 只留在 `legacy` extra（只服务 v1 冻结树） | [§5](#5-落点文件清单) |

> ⚠️ **「删掉 camoufox-1」不是「删掉那 430 份样本」**。
> 盘上有 **430 份已建的 P2 样本**带着 `tool_surface="camoufox-1"`，还有
> **14 份 P1 存档（顶层 `{task}__{run}.json`；连 `.reviewed.json` 复核副本与探针产物
> 共 29 个文件带该标记）**记着 `driver="camoufox"`。面删了，那些样本渲染不出来、
> 那些存档切不出样本 ⇒ **复核链当场断掉**。
> ⇒ 本文的口径是：**退役不等于失忆；删的是实现，不是证据。**
> - **不再产出**：`toolcatalog.DRIVER_SURFACE` 里**没有** `camoufox` 键 ⇒
>   新存档落不出 `camoufox-1`；
> - **仍然读得懂**：`toolcatalog.LEGACY_DRIVER_SURFACE` 让存量的
>   `driver="camoufox"` 存档仍折算回 `camoufox-1`。
>
> 两者**不是**一回事：前者是「不再生产」，后者是「仍可回放」。
> 把「不再生产」做成「读不出来」，删的是证据，不是旧实现。
> （同一口径在采集面那一轴也成立：`access_surface.LEGACY_DRIVER_SURFACE`。）

### 0.3 三个最要紧的判断

| # | 判断 | 影响面 |
|---|---|---|
| **D1** | **目标编码仍是 `role`+`name`，camofox 的 `eN` ref 只在驱动内部消化** | 决定训练数据长什么样（ref 绝不能进训练数据） |
| **D2** | camofox 的快照**就是 Playwright 的 aria YAML**（同一套格式），现有解析器可复用 | 决定方言轴要不要动 |
| **D3** | 11 个 tool ↔ 13 个协议原语**有 5 处缺口**（links / interactive / count / extract / press） | 决定工作量与「不可实现」的降级口径 |

### 0.4 依赖外部条件

- Node ≥ 22；
- `camofox-browser` 的 REST 服务在 **9377** 跑着（`npm start`）；
- `camofox-browser-mcp` 命令**可解析**（`npm i -g @askjo/camofox-browser-mcp`
  或 `cd camofox-browser/mcp && npm link`）。

⚠️ **本仓库不启动浏览器**——浏览器由上游 REST 服务持有。
⇒ **有头/无头不由本仓库决定**（`--headful` 已撤，见 [§4 R4](#r4-收尾与接线)）。

---

## 1. 起点（梳理时的事实，2026-10-26 上午）

### 1.1 当时是「断裂态」

| 事实 | 位置 |
|---|---|
| `camoufox==0.5.8` 已从依赖移除 | `pyproject.toml` |
| `camoufox_session.py` 仍 `from camoufox import AsyncCamoufox, DefaultAddons` | 模块层 import ⇒ 缺包 = **collection error**，不是 skip |
| `playwright==1.62.0` 仍留在主依赖 | `camoufox_driver.py` 直接 import `playwright.async_api.Error` |
| `DEFAULT_KIND = "camoufox"` | `executor/browser/driver_factory.py` |

⇒ 那时**不是「换掉一个在用的实现」，是「把停掉的实现接回来」**——没有回退压力，
但也没有在跑的基线可对拍。

### 1.2 代码里没有任何 camofox 痕迹

`trajectory_pipeline/` 的**代码**（不含 `docs/`）里 `camofox` 命中 **0 处**。
camofox-browser 当时**只以一份使用说明的形式存在**：
`docs/依赖说明/camofox-browser 使用说明.md`（含 11 个 tool 名、REST 9377、
`CAMOFOX_*` 环境变量表）。

### 1.3 camofox-browser 是什么

```
camofox-browser-mcp (Node, stdio)   ← 我们的 Python 进程起它
        │  JSON-RPC over stdio
        ▼
REST 服务 http://localhost:9377      ← 浏览器守护进程（常驻）
        │  HTTP + Bearer
        ▼
Camoufox 浏览器内核（Playwright 驱动）  ← 真正看网页的东西
```

- MCP 适配器**只做转发，不启动浏览器**（源码头 `mcp/server.mjs`）。
- MCP 侧 11 个 tool（`mcp/lib/tool-contracts.mjs` 是唯一真源，REST 与 MCP 共用）。
- 服务端配置在**服务端**（`CAMOFOX_PORT` / `CAMOFOX_ACCESS_KEY` / `CAMOFOX_API_KEY` /
  `CAMOFOX_USER_ID` / `CAMOFOX_SESSION_KEY`），Python 侧**读不到请求值**。

### 1.4 它的快照长什么样（关键事实）

上游的快照路由：

1. 建 ref 表，每条记 `{role, name, nth}`；
2. `getAriaSnapshot(page)` → **`page.locator('body').ariaSnapshot()`**（Playwright 原生）
   + 各 iframe 的 aria YAML **追加**在后面；
3. 把 YAML 按行扫，命中交互 role 的行**在行尾插一个 ` [eN]` 标注**；
4. `windowSnapshot(annotatedYaml, 0)` → **80k 字符窗口**，
   返回 `{url, snapshot, structure, refsCount, truncated, totalChars, hasMore, nextOffset}`；
5. MCP 契约**硬编码** `includeScreenshot=true` ⇒ **每次快照都带一张 base64 PNG**。

标注后的行长这样：

```
- heading "示例" [e1] [level=1]
- button "立即播放" [e2]
- link "在线观看" [e3]
- textbox "搜索视频" [e4]
```

⇒ **两件事因此成立**：

- **解析器可复用**：`executor/browser/aria_snapshot.py` 的 `_HEAD_RE` 解析
  `- role "name"` 形态，`[eN]` 是后缀、不吃进 role/name ⇒ **解析结果与 camoufox 面同构**。
  ⚠️ 但 `[eN]` 的**位置不固定**（playwright 的 `[level=1]` 之类属性后缀会跟在它后面），
  所以取 ref 必须**按 `\[e\d+\]` 的形状找 token**，不能假定它紧跟 name。
- **方言可以沿用 `aria_role`**：key 仍是 `role`/`name`，词表仍是 ARIA role。

---

## 2. 目标架构

### 2.1 进程拓扑

```
Python 执行器（trajectory_pipeline/executor）
  │
  │  CamofoxSession  ── 起/收 MCP stdio 子进程 + tab 账目 + 指纹读取
  │  CamofoxDriver   ── PageDriver 协议实现
  ▼
McpClient（既有传输层，只泛化了入口）
  │  stdio JSON-RPC
  ▼
camofox-browser-mcp（Node）
  │  HTTP
  ▼
camofox-browser REST（:9377） → Camoufox 内核
```

### 2.2 分层落点

| 层 | 落点 | 复用情况 |
|---|---|---|
| **传输层** | `executor/browser/mcp_client.py` | **几乎原样复用**——它已经是通用 MCP stdio 客户端，只是 `from_env()` 里硬编码了 obscura 的 `OBSCURA_EXE` 与 `("mcp","--stealth")`；现已泛化成「按 env var 取 exe」 |
| **工具语义层** | 新建 `executor/browser/camofox_driver.py` | **全新**——11 个 tool 名、参数、返回格式的知识放这里（**不放 `mcp_client.py`**，那里明写「不认识任何 tool 名」） |
| **会话层** | 新建 `executor/browser/camofox_session.py` | 对应已删的 `camoufox_session.py` 的位置（进程生命周期 + 指纹 + tab 栈） |
| **协议层** | `executor/browser/page_driver.py` | **不动** |

### 2.3 边界（明确不做什么）

- **不 import 存量 v1**（`archive/v1/`）——照读照抄思路，新树内重写。
- **不改 `PageDriver` 协议**来迁就 camofox 的 tool 形状；缺口在**实现侧**补。
- **不把 camofox 的 11 个 tool 暴露给模型**——模型看到的工具集仍由
  `toolcatalog` 投影（`click`/`fill`/`goto`/`press`/`new_tab`/`close_tab`）。
- **不做 REST 直连**：owner 已定「走 MCP」。
- **不改上游源码**（A2 裁定）：`C:\Users\klpc\workspace\tool\camofox-browser` **零改动**。

---

## 3. 设计判断

### D1 目标编码保持 `role`+`name`，`eN` ref 内部化

**问题**：`camofox_click` 只吃 `ref` 或 `selector`，**没有 `role`+`name` 定位**。
而 `role`+`name` 正是本项目 `aria_role` 方言的动作面。

**不能选 ref**：ref 是「会话内有效、跨会话失效」的句柄——
`toolcatalog` 的 `FORBIDDEN_COMMON` 明令 `ref`/`refs`/`handle` 等不得出现在工具声明里，
理由是「进训练数据等于让模型学随机数」。
**不能选 selector**：CSS 选择器位置耦合，跨会话不稳定；且命中多个会 **422 strict mode**。

**解法（两段映射）**：

```
agent ──(role,name)──▶ CamofoxDriver._refs         ours:  "e7" → ("button","立即播放")
                            │
                            ▼
                        _camo_refs                    camofox: "e7" → "e2"
                            │
                            ▼
                        camofox_click(tabId, ref="e2")
```

- `_refs`（我们自己的 ref → `(role,name)`）**沿用 camoufox 面的编号口径**
  （过了过滤阶梯之后按顺序编 `e1..eN`）⇒ `agent_loop` 的 I6 白名单、观测渲染、
  `last_action` 记账**全部零改动**。
- `_camo_refs`（我们的 ref → camofox 的 `eN`）**只活在驱动内部**。
- **两层 ref 都不进训练数据**：训练侧看到的是 `role`+`name`。

### D2 方言沿用 `aria_role`；枚举基础多一档

**方言**：`camofox-1` 面用 `aria_role`（`role`/`name`），与 `camoufox-1` **同方言**。
`common/elements.py` 的 `ELEMENT_VOCABULARIES` 是封闭集，**不加第三个词表**。
⇒ **三张面 → 两个词表**（obscura 单独一组，两张 aria 面同组）：
契约测试 `test_element_dialect.py::test_方言按词表分组且obscura单独一组` 钉住。

**枚举基础多一档（口径差异，必须记）**：

- camofox 的 ref 标注**只给交互 role**（上游 `INTERACTIVE_ROLES` 13 项），
  且名字命中 `SKIP_PATTERNS`（`datepicker` / `calendar` / `date`）的行**不给标注**
  ⇒ **存在「过了词表但没有 ref」的行**。
- 我们的 `filter_ladder` 会把它们留下 ⇒ **动作面上会出现点不了的元素**。
  ⇒ **必须改成「只保留带 `[eN]` 标注的行」**。
- ⇒ 面的账目基础从 `nodes` 变成 `nodes` + `annotated`（带 ref 的节点数），
  并新增 **`unref_dropped`** 这一档（`CAMOFOX_FACE_STAGES = ("unref_dropped", "name_ambiguous")`）。
  ⚠️ `filter_ladder` 是**两把尺子共用**的，不能动它——过滤放在**枚举基础**那一层。

**不变式（必须写进代码注释，否则以后有人「优化」过滤规则会踩雷）**：

> camofox 的 `[eN]` 标注，在「`(role,name)` 在整页唯一」时**一定正确**。
>
> 理由：上游建 ref 表时主帧用 `seenCounts`（主帧内计数）、
> **每个 iframe 各自独立一份** `seenCounts`；
> 而标注环节用**单一全局** `annotationCounts`。
> ⇒ 主帧与 iframe 出现同名 `role+name` 时，iframe 那行**可能被标错或不标**。
> ⇒ **但** `filter_ladder` 把「真同名整组」**整组丢弃** ⇒
> 进动作面的 `(role,name)` 必在全页唯一 ⇒ 两套计数一致（都是 0）⇒ 标注正确。
>
> **结论：本项目的过滤阶梯天然免疫这个上游缺陷。去掉「真同名整组丢弃」这条规则就会开始点错元素。**

### D3 工具面：三张面，各按实现分

**A1 裁定：新增第三张 `camofox-1`，删掉 `camoufox-1`。** 落地口径（§0.2 已述）：
「不再产出 + 仍读得懂」。

`toolcatalog` 里的三张面：

| 面 | 状态 | 方言 | 目标编码 | 读原语 |
|---|---|---|---|---|
| `obscura-1` | **只读存量**（2026-10-22 起不再产出） | `html_tag` | `{tag,label}` | 无 |
| `camoufox-1` | **已退役，只读存量**（2026-10-26） | `aria_role` | `{role,name}` | 有 |
| `camofox-1` | **生产面**（2026-10-26 起） | `aria_role` | `{role,name}` | 有 |

**为什么按实现分而不是按方言分**：工具声明是**给模型看的**，「这个面背后怎么定位」
必须如实。两张 aria 面虽然**键名与词表相同**，但**定位语义不同**：

| | `camoufox-1` | `camofox-1` |
|---|---|---|
| 定位机制 | `get_by_role(role, name=name, exact=True)` | 上游 `page.getByRole(role, {name}).nth(nth)` |
| 名字匹配 | **精确** | **大小写不敏感子串**（上游没有 `exact`） |
| 声明里能写「精确匹配」吗 | 能 | **不能**——写「精确」就是教模型一个引擎不保证的性质，而它在报告上看不出异常 |

⇒ **两面必须各有一份声明**。契约测试 `test_两张aria面的同名工具说明不同` 钉住：
参数相同可以，**说明文字必须不同**，且 camofox 面的说明里**不许出现 `exact`**。

本面**自己**保证唯一性：枚举期把「真同名整组」「名字是别人的子串」
「上游没给句柄」三类剔掉 ⇒ **清单里给出的每个 `(role, name)` 都是唯一可寻址的**。

**方言轴的定义被放宽**：从「一面一词表」改成「**键名互不相同**，词表可相同」。
新增的一致性断言：「同词表的面键名必相同」。

### D4 五处协议缺口怎么补

| 协议原语 | camofox tool | 补法 |
|---|---|---|
| `goto` | `camofox_navigate` | 直接对应 |
| `snapshot` | `camofox_snapshot` | 直接对应，但**返回多字段**，要取 `snapshot` 文本、**丢掉 `screenshot` 字段** |
| `evaluate` | `camofox_evaluate` | **返回形态不同**：camoufox 面拿到的是 Python 原值（**不做 JSON 解包**），camofox 面要解两层（MCP text → JSON → `{ok,result}` → `result`）⇒ **口径差异要显式记** |
| `click` | `camofox_click` | 经 D1 的两段映射 |
| `fill` | `camofox_type` | `ref`/`selector` + `text` + `pressEnter` |
| `new_tab` / `close_tab` | `camofox_create_tab` / `camofox_close_tab` | tabId 由**服务端**发，**不再是本地自增 `c1/c2`** ⇒ `last_tab` 的 `tab`/`pages` 口径要改 |
| `links` | ❌ 无 | 由 `camofox_evaluate` 跑链接 JS（**与 `SHARED_DOM_JS` 同一路数**，保留「按 href 去重」这条跨实现可比性硬口径） |
| `count` | ❌ 无 | 由 `camofox_evaluate` 跑 |
| `extract` | ❌ 无 | 由 `camofox_evaluate` 跑 |
| `interactive` | ❌ 无 | 由 `camofox_snapshot` 的 aria YAML 派生（解析器已有） |
| `press` | ❌ 无 | **见 A2（[§3 D5](#d5-press-走驱动缓存合并a2)）** |
| `observe` | 组合 | 组合出来的 `degraded` 名要重定（见 D8） |
| `close` | ❌ 无 | **Node 服务生命周期 ≠ 浏览器 tab** ⇒ `close` 变成「关掉本 session 的所有 tab」，不是「关浏览器」 |

**11 个 tool 里 4 个不进 driver**：`camofox_scroll`（协议无滚动原语）、
`camofox_screenshot`（可给 VLM 复核链路，非模型可见）、
`camofox_list_tabs`（与 `new_tab`/`close_tab` 的 tab 账目合并）、
`camofox_import_cookies`（红线，D9）。

### D5 press 走驱动缓存合并（A2）

**裁定**：A2-a，**驱动缓存合并**。**不改上游源码**。

`agent_loop.AGENT_TOOLS = ("goto","click","fill","press")`——`press` 在训练工具集里，
但 camofox **没有独立的 press 工具**（`camofox_type` 只带 `pressEnter` 布尔位）。

**做法**：驱动记住「最近一次 `fill` 的 `(目标, text)`」；收到 `press("Enter", 同目标)` 时，
**重发** `camofox_type(ref, 同 text, pressEnter=true)`。

**为什么重发是安全的**：上游 `camofox_type` 默认 `mode='fill'`（**设值**不是追加）
⇒ 重发同一份文本不会变成「填两遍」。

**降级口径**：目标不在「最近一次 fill」上、或 `key != "Enter"` ⇒ 记
`press_unsupported` 降级（**不是**「元素不存在」——那是页面事实，这是**能力不可用**）。
`press_unsupported` 会被带进下一次 `observe` 的 `degraded` ⇒ **下游不判**（fail-closed）。

**声明侧**：`camofox-1` 面的 `press` 说明**必须说清只有回车**——
「本层用 camofox 在刚填过文本的那个输入框上按回车：别的键、或对别的元素按键，
这一层做不到，按下去不会生效。」
⚠️ 契约测试有一条**正向断言**：带目标编码的工具**必须点名本面**
（camofox 面点名 `camofox`、camoufox 面点名 `camoufox`/`get_by_role`）——
否则有人「顺手」把措辞改回抽象说法，测试不会红，而数据会静默地退回到
「脱离本项目不成立」的状态。

### D6 枚举基础：快照分页改变了「数得全」这件事

camofox 的快照是 **80k 字符窗口**，返回 `hasMore` / `nextOffset`。
⇒ 页面长时**一次快照看不到全页**。

- **必须翻页**：`interactive()` 要循环 `camofox_snapshot(offset=nextOffset)`
  直到 `hasMore=false`，**收齐所有页**再合并、过滤、编号。
- ⚠️ **翻页的两个坑**：
  1. `offset>0` 走**缓存**（**不刷新 refs**）⇒ 页与页之间 ref 稳定；
     但缓存若为空则**回落到整页刷新** ⇒ **ref 会重编号** ⇒
     **翻页过程中只解析、不动作**，`_camo_refs` 必须在**收齐后统一建立**。
  2. 翻页期间页面若变了，缓存与当前页不一致 ⇒
     翻页前后各读一次**文档代号**（`performance.timeOrigin`）
     ⇒ 不一致 ⇒ 记 `nav_unsettled` 降级（**沿用现有降级名**）。
- ⇒ **`elements_total` 在 camofox 面与 camoufox 面不是同一个东西**
  （camoufox 面 = 整页 ARIA 树过了词表的那些；camofox 面 = 翻页收齐后过了词表的那些）。
  方案 20 的「面 ≠ 面可比」原则同样适用。

**文档代号三态**（沿用 camoufox 面的同一判据）：空串 = 没读到 /
`"none"` = 页面没 performance / 值 = 文档代号。

### D7 指纹：九项变十一项

camoufox 面的九项是**请求值**（`os`/`geoip`/`locale`/`humanize`/`headless`）与
**实测值**的混合。camofox 的配置在**服务端**，Python 侧读不到请求值
⇒ 只能读实测值：

| 项 | 处置 |
|---|---|
| `os` / `geoip` / `locale` / `humanize` / `headless` | **删**（读不到；服务端的配置我们无权声明） |
| `screen` / `window` / `dpr` / `timezone` | 保留（`camofox_evaluate` 读得到） |
| `ua` | **不进**——它比现状粒度更细（可关联标识），现状也没有 ⇒ 不扩大存档里的身份信息面 |
| `language` | **进**（`zh-CN` 是本仓固定配置，值得记） |

⇒ 实测侧落九项：`user_agent` / `platform` / `language` / `languages` /
`timezone` / `screen` / `window` / `dpr` / `webdriver`；
另加两项**本仓库能说清的**：

- **`engine = "camoufox"`**——⚠️ **浏览器内核没换**：camofox-browser 底层依赖
  `camoufox-js`（上游 `package.json`），**换的是接入方式**
  （playwright 直连 → 上游 REST + MCP 适配器）⇒ 这一项**跨批次可比**：
  拿它与旧 camoufox 批次的 `engine` 对读，是同一件事。
- **`base_url`**——REST 服务的 **origin**（已去掉路径、查询串与**用户信息**）：
  凭据不许进存档（红线），所以只留 origin。

⇒ **键集合是公开契约**（`orchestrator.py` 的 docstring + `cli.py` 的打印 +
`tests/unit/test_camofox_switch.py`）⇒ 三处同步改。

> ⚠️ 顺带修一处**旧的不一致**：`orchestrator.py` 的 docstring 说九项含
> `main_world_eval`，而 `camoufox_session.py` 实际返回 `timezone`（**没有**
> `main_world_eval`）——迁移时一并修。

### D8 降级名口径

camoufox 面的原则：**记能力名，不抄 tool 名**
（obscura 面记 tool 名 `browser_links`，camoufox 面记 `links`）。
⇒ camofox 面**继续记能力名**：`links` / `interactive` / `count:<selector>` /
`body_text` / `body_mojibake` / `title_mojibake` / `nav_unsettled`。

**新增**（camofox 特有）：`unref_dropped`（枚举口径，见 D2）、
`press_unsupported`（A2 的能力缺口）、`snapshot_paged`（翻页读不稳）。

> ⚠️ 下游（`gatekeeper`/`branches`/`gates`）**只判 `degraded` 是否为空**，
> 不认具体字符串 ⇒ 新增降级名不破坏下游。

### D9 红线

| 能力 | 处置 | 理由 |
|---|---|---|
| `camofox_import_cookies` | **不实现**（`FORBIDDEN_TOOLS` 里直接禁） | 凭据红线。`FORBIDDEN_COMMON` 已禁 `cookie`/`authorization`/`token`/`password` 等词 |
| `camofox_screenshot` | **不进工具声明**（非模型可见） | 但**可给 VLM 复核链路**——camofox 有截图 tool，这条链终于能接上 |
| 快照自带的截图 | **丢弃 `screenshot` 字段** | 观察里混进视觉数据会污染感知层口径；且它占带宽（每页一张 base64 PNG） |
| `camofox_scroll` | **不实现** | 协议无滚动原语 |
| `CAMOFOX_ACCESS_KEY` / `CAMOFOX_API_KEY` | **只由子进程继承，本模块不碰** | 凭据不出进程，更不进存档 |
| `base_url` | **只留 origin**（去路径/查询串/用户信息） | 同上 |

---

## 4. 落点（R0–R5）

### R0 探针与口径裁定

探针脚本放在 `trajectory_pipeline/research/probes/`，**放证据不放结论**。

### R1 传输层 + 会话层 + 驱动骨架

| 文件 | 动作 |
|---|---|
| `executor/browser/mcp_client.py` | 泛化 `from_env`（`OBSCURA_EXE`/`--stealth` 留在 obscura 侧），加一个按 env var 取 exe 的通用入口；**传输层仍然不认识任何 tool 名** |
| `executor/browser/camofox_session.py` | **新建**：MCP 进程生命周期、tab 账目、`userId`/`sessionKey`、`fingerprint` 读取、`close` 语义 |
| `executor/browser/camofox_driver.py` | **新建**：`PageDriver` 原语 + `name = "camofox"` + `fingerprint` + `_refs`/`_camo_refs` 两段映射 |
| `executor/browser/aria_snapshot.py` | 扩展：解析时**同时取出 `[eN]`**（按 `\[e\d+\]` 形状找 token，不假定位置） |

### R2 缺口补法与降级口径

- `links` / `count` / `extract` 三条由 `camofox_evaluate` 派生（**保留「按 href 去重」**）。
- `press` 按 A2 落驱动缓存合并。
- `interactive()` 改成**多页收集 → 合并 → 过滤 → 编号**的事务。
- 降级名白名单 + `unref_dropped` 档位。

### R3 面与声明

- `toolcatalog`：增 `camofox-1` 面 + `LEGACY_DRIVER_SURFACE`；
  **重写 `instruction` 的点名**（camofox 面点名 `camofox`、**不写 `exact`**）；
  `FORBIDDEN_PER_SURFACE` 改成按面分开的禁用词表。
- `executor/browser/access_surface.py`：`SURFACE_CAMOFOX = "camofox-plain"` +
  `DRIVER_SURFACE` 加项 + `LEGACY_DRIVER_SURFACE`。
- `executor/orchestrator.py`：`fingerprint` 形状 + docstring 修不一致 + `AgentBudget` 超时**重新标定**。
- `executor/actions.py`：方言沿用 `aria_role` ⇒ **零改动**（`target_params` 不用加第三支）。

### R4 收尾与接线

- `executor/cli.py`：`_launch_camofox` / `_execution_layer_missing_reason` /
  `_check_camofox` / `_camofox_action_smoke` / `--driver` choices。
- `executor/browser/driver_factory.py`：`DEFAULT_KIND = "camofox"` /
  `SUPPORTED_KINDS = ("obscura", "camofox")` / `make_driver`。
- **camoufox 侧删除**（A1-b + 「禁止接回来」）：
  `camoufox_driver.py` / `camoufox_session.py` / `camoufox_probe.py` /
  `probe_f5_name.py` **已删**；`--driver camoufox` **不再是一个选项**。
- `pyproject.toml`：`playwright` **已摘掉**（`camoufox_driver.py` import
  `playwright.async_api.Error` 是它留在主依赖的唯一理由，那个模块已退场）；
  `cloverlabs-camoufox` **只留在 `legacy` extra**（只服务 v1 冻结树）。
- ⚠️ **`--headful` 语义变更**：camofox **没有 headless 参数**，浏览器由上游 REST
  服务持有 ⇒ **有头/无头不由本仓库决定**。
  ⇒ 旧 `--headful` **不再是「切执行层模式」的开关**：
  `cli` 侧**退役**它并给明确报错（`_HEADFUL_RETIRED_HINT`，退出码 2），
  探针侧（`m30_live_check.py`）**直接删掉该开关**。
  理由：留着这个开关只会让读数看起来「切了口径」，而实际没切。

### R5 实跑验收

- 真跑一批，**逐条打开存档读 evidence 核对**（CLAUDE.md 的固定动作，不是可选项）。
- `cli check-archives` 是它的机械版；**「结论对不对」不在门禁里**。

---

## 5. 落点文件清单

### 5.1 新建（生产代码 2 个）

| 文件 | 职责 |
|---|---|
| `trajectory_pipeline/executor/browser/camofox_session.py` | MCP 会话：进程生命周期、tab 账目、`fingerprint` 读取、`close` 语义 |
| `trajectory_pipeline/executor/browser/camofox_driver.py` | `PageDriver` 协议实现 + `name = "camofox"` + `fingerprint` + `_refs`/`_camo_refs` |

### 5.2 改（生产代码）

| 文件 | 改什么 |
|---|---|
| `executor/browser/driver_factory.py` | `DEFAULT_KIND = "camofox"` / `SUPPORTED_KINDS` / `make_driver` 形参与分支 |
| `executor/browser/mcp_client.py` | 泛化 exe/args 入口（obscura 专属常量留在原处） |
| `executor/browser/aria_snapshot.py` | 加「带标注解析」能力 |
| `executor/browser/access_surface.py` | `SURFACE_CAMOFOX = "camofox-plain"` + `ACCESS_SURFACES` + `DRIVER_SURFACE` + `LEGACY_DRIVER_SURFACE` |
| `executor/browser/element_basis.py` | `ROLE_VOCAB` 复核 + `unref_dropped` 档位；`filter_ladder` 与 `SHARED_DOM_JS` **不动** |
| `executor/orchestrator.py` | `fingerprint` 形状 + docstring 不一致 + `AgentBudget` 超时重标 + `RunRecord.driver` 值域说明 |
| `executor/cli.py` | launch/预检/检查/冒烟四块 + `--driver` choices + `--headful` 退役 + 环境变量 |
| `executor/dual_run.py` | `summarize` 的三面口径说明 |
| `toolcatalog/__init__.py` | 增 `camofox-1` 面 + `LEGACY_DRIVER_SURFACE` + **重写 instruction 点名** + `FORBIDDEN_PER_SURFACE` + `DIALECTS` |
| `evaluation/access_surface.py` | 再导出新采集面（**不另写第二份**） |
| `evaluation/golden/builder.py` | 报错文案里的面代号列表 |
| `pyproject.toml` | 摘 `playwright`；`legacy` extra 的注释改口径 |

### 5.3 删（camoufox 实现本体）

`executor/browser/camoufox_driver.py` · `camoufox_session.py` · `camoufox_probe.py` ·
`probe_f5_name.py` —— **全部已删**。

⚠️ **删的是实现，不是证据**：
- `toolcatalog.SURFACE_CAMOUFOX` **留着**（430 份 P2 样本要用）；
- `access_surface.SURFACE_CAMOUFOX` **留着**（存量账本行的 `blocked_surface` 要用）；
- 两处各配一张 `LEGACY_DRIVER_SURFACE` 让存量 `driver="camoufox"` 仍折算得出来。

### 5.4 只读不改

`executor/browser/page_driver.py` · `executor/actions.py`（方言沿用则零改动）·
`executor/agent_loop.py`（A2 落驱动层则零改动）· `perception/*` · `assembler/*` ·
`rationale/*` · `common/elements.py` · `llm/*` · `taskgen/*` · `storage/*` ·
`research/**/*.jsonl`（历史快照）

> `perception/`·`assembler/`·`rationale/` 里的 camoufox 叙述**全是注释**，
> 代码本身只认 `element_vocabulary` / `tool_surface` 两个抽象 ⇒ **表面无关**。

### 5.5 树外

| 位置 | 改什么 |
|---|---|
| `C:\Users\klpc\workspace\tool\camofox-browser` | **零改动**（A2 裁定） |
| `docs/设计方案/04-camoufox执行层替换方案.md` | 加覆盖块（结论被反转，**不删历史**） |

---

## 6. 测试

### 6.1 新建

| 文件 | 封着什么 |
|---|---|
| `tests/unit/test_camofox_driver.py` | 驱动语义（口径级，**不含** playwright 实现细节） |
| `tests/unit/test_camofox_session.py` | 会话生命周期、指纹形状、**不含身份态**（安全红线） |
| `tests/unit/test_camofox_switch.py` | 三张面的登记、「`make_driver` 每处显式传 `kind`」、「`--driver` 缺省 == `DEFAULT_KIND`」 |

### 6.2 改

| 文件 | 改什么 |
|---|---|
| `tests/contract/test_tool_catalog_contract.py` | 三面表；`FORBIDDEN_PER_SURFACE` 改成按面分；**「目标编码的工具必须点名本面」**；**「两张 aria 面的同名工具说明不同」** |
| `tests/contract/test_element_dialect.py` | `OTHER_SURFACE_KEYS` 加 camofox；「方言按词表分组且 obscura 单独一组」；「退役的 camoufox 仍折得出面与方言」 |
| `tests/unit/test_driver_factory.py` | `DEFAULT_KIND` / `SUPPORTED_KINDS` 键集 / AST 守卫 |
| `tests/unit/test_golden_access_surface.py` | `DRIVER_SURFACE` 键集 == `SUPPORTED_KINDS`；**「退役的 camoufox 仍读得懂但不再产出」** |
| `tests/unit/test_cli_batch_resilience.py` | 「起浏览器不重试」（camofox 侧**没有**重试）；「执行层缺件」文案 |
| `tests/unit/test_element_basis.py` | 补 camofox 侧的假会话 |
| `tests/unit/test_p0_equivalence.py` · `tests/unit/test_plan.py` | 文案 |

### 6.3 删

`tests/unit/test_camoufox_driver.py` · `test_camoufox_session.py` · `test_camoufox_switch.py`
—— 随实现一起退场（**不是**「因为测试难写就删」，是「被测对象不存在了」）。

### 6.4 缺口测试（要新写）

- 降级名**白名单**测试（现在没有注册表）。
- `unref_dropped` 档位的**账目恒等式**。
- D2 不变式：`(role,name)` 唯一 ⇒ 标注正确（iframe 场景）。
- `press` 的能力降级路径（A2）。

---

## 7. 文档影响

### 7.1 口径类（必须改）

| 文档 | 改什么 |
|---|---|
| `设计方案/04-camoufox执行层替换方案.md`（1896 行） | **结论被反转**：加覆盖块（与 §6.1 已有覆盖块同一手法），**不删历史** |
| `01-架构总览.md` | 执行层、工具面、失败分支全集 |
| `03-数据契约.md` + `contracts/{P1,P2,P3,T}` | `driver` 值域、`fingerprint` 形状、`tool_surface` 取值、`access_surface` 取值 |
| `tools.md` | camofox 的 11 个 tool 补录；**「Camoufox 专属陷阱」章要改口径** |
| `08-常用命令速查.md` | 命令的 `--driver`；必填项表加 `CAMOFOX_MCP_EXE` |
| `04-运行手册.md` | 环境变量全表（加 `CAMOFOX_MCP_EXE` / `CAMOFOX_BASE_URL`） |
| `02-避坑指南.md` | camoufox 专属那几条改成 camofox 口径 |
| `07-人工复核与判读手册.md` | 面代号 |
| `设计方案/20` | `access_surface` 取值表加 `camofox-plain` |
| `计划进展/10-TODO台账.md` | §停点整节重写 |

### 7.2 追加（历史类，**append only**）

- `计划进展/90-增量编年史.md`：**追加增量九十三**。
  **不改历史条目**——「并池是写入时的动作」同源：历史批次的事实不因为换了执行层而改写。

### 7.3 断链（**改名留下的，14 处**）

`camoufox.md` → `camoufox_弃用.md` 之后，下列 14 处链接指向不存在的文件：

```
docs/04-运行手册.md:21, 22
docs/tools.md:411, 421, 773, 1387
docs/计划进展/10-TODO台账.md:264
docs/计划进展/90-增量编年史.md:2956
docs/设计方案/04-camoufox执行层替换方案.md:93, 492, 497, 501, 577, 1783
```

⇒ **逐条判**：讲 camoufox 怎么装的指弃用手册，讲 camofox 怎么用的指新手册。

✅ **已逐条改完**（2026-10-26）：全仓 `依赖说明/camoufox.md` 的引用 **0 条残留**——
讲 camoufox 装法的指 `camoufox_弃用.md`，讲现行执行层的指
`camofox-browser%20使用说明.md`。

⚠️ **另一类断链留着不动**（**故意**）：`camoufox_driver.py` /
`camoufox_session.py` / `camoufox_probe.py` / `probe_f5_name.py`
**随实现一起删了**，而 `tools.md` 的「Camoufox 章」「设计方案 04 正文」
里指向它们的链接**现在点不开**。**留着是故意的**——那些链接记的是**当时**的落点，
删掉等于改写历史。两处（`tools.md` 的章首覆盖块、`设计方案 04` 的封面覆盖块）
都写明了这一点。

---

## 8. 风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| **R1** | **快照自带截图**（MCP 契约硬编码 `includeScreenshot=true`） | 每页一张 base64 PNG ⇒ 带宽/内存翻倍 | 驱动**丢弃 `screenshot` 字段**；要彻底消掉需**改上游契约**（A2 已裁定不改上游 ⇒ 带宽问题留着） |
| **R2** | 快照**分页**改变了「数得全」的含义 | `elements_total` 口径变 | 翻页收齐 + 前后核文档代号（D6） |
| **R3** | 11 个 tool 无 `press`/`count`/`links`/`extract`/`interactive` | 协议有、实现缺 | 缺 `press` 走 A2；其余由 `evaluate` 派生（D4） |
| **R4** | `camofox_import_cookies` 存在 | 凭据红线 | **不实现**（D9） |
| **R5** | 方案 04 §0.2 断言「MCP 封装层是指纹能力的衰减层」 | 新决策**正面撞上这条旧结论** | 正面回答：camofox-browser **本身就是 MCP 适配器**——但它包的是 **Camoufox 内核**（指纹能力在**内核**里，不在 MCP 封装里），衰减的是**可配置面**（`humanize`/`geoip`/`locale` 等参数不再由本仓控制）⇒ 结论是「**指纹能力保留、可配置面收窄**」，不是「指纹能力衰减」 |
| **R6** | 服务是**常驻**的，跨 Run 复用 ⇒ 状态泄漏 | 上一条 Run 的登录态/tab 泄到下一条 | `CAMOFOX_USER_ID` 按 Run 隔离；`close` 语义重新定为「关本 session 的所有 tab」 |
| **R7** | `503 session_expired` / `401`/`403` 的错误三态要重推 | 原来的判据（playwright 异常类型）没了 | 从 **HTTP 状态码**重推：422 strict / 409 page_changed / 503 session_expired / 401·403 auth |
| **R8** | **上游 REST 服务不在本仓库的控制面内** | 它没起 ⇒ 本仓库起不了浏览器 | `cli._execution_layer_missing_reason` **只查适配器可执行文件**（不查 REST，那要连一次）⇒ 缺件时**启动前退 2**、给明确安装指令，不逐条刷 ImportError |

---

## 附：核对过的依据

| 结论 | 依据 |
|---|---|
| 11 个 tool 的参数与 REST 路由 | `C:\Users\klpc\workspace\tool\camofox-browser\mcp\lib\tool-contracts.mjs` |
| 快照的 ref 标注与分页 | 同上 `server.js` 的快照路由与 `lib/snapshot.js` |
| ref 表的 per-frame 计数 | `server.js`（`nth is per-frame for locator resolution` 注释） |
| `SKIP_PATTERNS` / `INTERACTIVE_ROLES` | `server.js` / `lib/interactive-roles.js` |
| 快照窗口 80k | `lib/snapshot.js`（`MAX_SNAPSHOT_CHARS=80000`） |
| MCP 环境变量 | `mcp/lib/config.mjs`、`mcp/server.mjs` |
| 协议与现状 | `executor/browser/{page_driver,driver_factory,access_surface,element_basis,aria_snapshot,mcp_client}.py` |
| 工具面纪律 | `toolcatalog/__init__.py` |
| 依赖摘除 | `pyproject.toml`（`playwright` 已移除） |
