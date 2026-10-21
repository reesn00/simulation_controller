# 04 · Camoufox 执行层替换方案

> **状态**：设计稿（2026-10-20）。本文是**决策修订 D11 的提案**，不是已实现的说明。
> 文中「实测」= 本轮在���机跑出来的（探针 [`camoufox_probe.py`](../../executor/browser/camoufox_probe.py)，
> 2026-10-20，camoufox 0.5.8 / playwright 1.62.0 / Firefox 156.0.1-beta.36）；
> 「推断」= 尚未验证，实施时必须先验。
> **本轮只做取证，未写实现。**

---

## 0. 决策修订

| | 原决策 | 修订 |
|---|---|---|
| 编号 | D7 | **D11**（修订 D7） |
| 执行层 | obscura（MCP / stdio / 37 tools / Rust 无头） | **camoufox（裸 Playwright API / Firefox 指纹浏览器）** |
| 传输层 | MCP stdio | **Playwright 协议**（进程内，不经 MCP） |
| obscura 地位 | 唯一执行层 | **退为参考实现**（保留在树内，不进生产链） |
| 编号 | — | **D12**（新增，见下） |
| 动作词汇表 | `PageDriver` 原语名 + 语义目标 `{tag, label}` | **camoufox 的调用面**：`goto` / `click(role,name)` / `fill(role,name,text)` / `press(key)` |
| 训练侧工具声明 | **不提实现名**（obscura 时代的红线） | **点名 camoufox**——执行层就是它，不点名等于教模型一个它见不到的接口 |

### 0.1 D12：动作词汇表必须等于执行层的调用面

**D11 只换了执行层的实现，没换动作的形状——那是不够的。**

`PageDriver` + 语义目标 `{tag, label}` 是 **obscura 时代**的产物：obscura 的 tool 返回文本，
`{tag, label}` 是为了从文本里把「点了哪个」反推出来（obscura 的 `ref` 是会话内句柄，不能进数据）。
这个改写有个致命后果：

> **轨迹里的动作只在本项目里成立。**
> 本项目的产物是要给**别的** agent 用的。那个 agent 手上只有 camoufox——
> 它收到 `click(tag="button", label="立即播放")`，**没有任何 camoufox API 能对应**，
> 这批数据就废了。模型学到的是「本项目怎么点」，不是「浏览器怎么点」。

**原则（写成可引用的纪律）**：

> **执行层用什么浏览器，训练侧的动作词汇表就长什么样。**
> 动作一旦被改写成项目自有的语义形态，轨迹数据就只在本项目里成立。

**不点名 camoufox 也不对**。原来的「不提实现名」红线是为 obscura 的 MCP 形态写的——
那时执行层是 obscura，提 obscura 的 tool 名等于教模型去驱动一个它见不到的浏览器。
现在执行层就是 camoufox，工具的**形状**已经是它的调用面；
description 里不写清 `get_by_role` 的语义，模型就学不会「匹配到多个元素时怎么办」——
而那正是执行期最常见的失败。

**D12 落在两处**：
1. **动作词汇表**（`executor/actions.py` + `toolcatalog/`）→ §2.2
2. **协议的动作面**（`page_driver.py`）→ §3

### 0.2 为什么现在改 D7

D7 放弃 camoufox 的语境是「MCP 生态里 obscura 更顺、37 个 tool 会话式够用」。
本轮要加 camoufox 的理由变了——**可控 + 原生值**（30+ 个 stealth 参数逐项可调、
返回结构化值而非文本），**反检测强度是待验证的附带收益而非主要理由**（§6.1 的 go/no-go 门槛）。
**保留 D7 的原结论会被后来人读成「camoufox 已被否决」，所以必须显式改写，不能顺手加。**

**为什么选裸 Playwright API 而非 camoufox-mcp**：MCP 封装层是指纹能力的**衰减层**。
引入 camoufox 的初衷是可控与反检测，走第三方 MCP 封装等于自带一个衰减器。

---

## 1. 实测取证（本轮结论）

探针在包内（`executor/browser/camoufox_probe.py`），**可重跑**——camoufox / playwright 升级后重跑，
看结论是否漂移。以下均为实测原值。

### 1.1 指纹确实落地

```json
{"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0",
 "oscpu": "Windows NT 10.0; Win64; x64", "platform": "Win32",
 "wd": false, "langs": ["zh-CN", "zh"], "cores": 16,
 "intl": "zh-Hans-CN", "tz": "Asia/Shanghai",
 "screen": [1440, 900], "inner": [1425, 760], "dpr": 1.5}
```

`webdriver=false`、UA/oscpu/platform 三者同 OS、langs/intl/tz 同地区——[依赖说明 §8.2](../依赖说明/camoufox.md)
的判据表全部满足。

### 1.2 四个必须写进设计的实测事实

| # | 实测 | 后果 |
|---|---|---|
| **F1** | **`screen` 逐 launch 变化**：三次 launch 分别 1920×1080 / 1536×864 / 1440×900 | 响应式站点的内容分布**逐 Run 漂移**。obscura 的 viewport 是固定 1280×720（恒定），camoufox 默认**每批不同**——分布变化无法归因到站点变化。**必须显式钉 `screen=` / `window=`**（见 §4.3） |
| **F2** | `page.evaluate` 默认跑 **isolated world**：`window.__mw` 读出来是 `null`；`mw:` 前缀需 `main_world_eval=True`，否则报 `Main world evaluation is disabled` | `evaluate` 原语的**语义边界**：DOM 共享（元素、innerText 都能取），但**页面 JS 的全局状态读不到**。判断点涉及播放器状态时必须开 `main_world_eval` |
| **F3** | 真实点击 `isTrusted` = **`"true"`**；`evaluate` 伪造点击 = **`"false"`** | camoufox 满足 [page_driver.py:88](../executor/browser/page_driver.py#L88) 的「点击必须 trusted」约束（真实输入事件，非合成）。obscura 靠 stealth patch，camoufox 靠**真浏览器**，两者都满足 |
| **F4** | 元素**被替换**后：handle `isConnected=false`，`handle.click()` 抛 **Error**；元素**不存在**：`locator.click()` / `fill()` 抛 **TimeoutError** | 两类语义**可区分**（Error vs TimeoutError）。但「元素不存在」与「页面卡住」**同型**（都是 TimeoutError）——须用 `count()` 预判（见 §4.2） |

### 1.3 其余实测（协议映射的底账）

| 项 | 实测 | 与 obscura 的一致性 |
|---|---|---|
| `new_context()` 默认 | `viewport_size is None` → **no_viewport 补丁生效** | obscura 无此概念 |
| `count(缺失选择器)` | `0`，不报错 | ✅ 一致 |
| `links` / `extract` | 直接给结构化数组 | ✅ **obscura 的 NDJSON / 列式文本解析全部作废** |
| `textContent` | **含 `<script>` 正文** | ⚠️ obscura 的 `TEXT_CONTENT_EXPR`（clone + 删 script/style）**仍需要** |
| `innerText` | 真实换行保留 | ✅ 一致 |
| `fill()` / `keyboard.press()` | 正常 | ✅ 一致 |
| `ctx.new_page()` | 独立 page，同 context | 对应 obscura `new_tab` |

### 1.4 探针自身的两个坑（记录下来，免得后人重踩）

1. **`data:` URL 不能用**——URL 里的 `#` 是 fragment 分隔符，payload 在首个 `#` 处截断，
   `<script>` 块根本不执行，探针会**静默给出错误结论**（实测：`isTrusted` 两个值都是 `null`、
   待替换按钮纹丝不动）。必须用 `page.set_content()`。
2. **`isTrusted` 回传要用 DOM 属性，不能用 `window` 全局变量**——F2 的隔离会让 `window.__x` 读成 `null`，
   分不清「不是 trusted」与「没点到」。

---

### 1.5 F5（**离线段，2026-10-20 实测**）：采集层算的 name 与 playwright 算的 name

探针：`executor/browser/probe_f5_name.py`（包内，可重跑；离线构造页，25 个用例，
camoufox 0.5.8 / playwright 1.62.0）。**这一节只覆盖 §4.5.3 的离线一半**——
真实页面的命中率仍未测。

| # | 实测 | 后果 |
|---|---|---|
| **F5-a** | **role 侧零落差**：25/25 用例的 ``js_role`` 与 ``aria_snapshot`` 里的 role **一字不差**（含 `div[role=button]`、`input[type=search]`→`searchbox`、`select`→`combobox`、`a[href]`→`link`） | §4.5.2 那张短词表在离线上**可以直接用**。落差全在 name 侧 |
| **F5-b** | **朴素链立不住**：`aria-label \|\| innerText \|\| placeholder \|\| title \|\| alt` 在「可唯一定位」的 23 个用例上只命中 **15（65%）**；其中 3 例**产出空 name**（图标按钮 / 图片链接 / submit——它取的是元素**自己的** `alt`，取不到子 `<img>` 的） | 方案的朴素链**不能直接实现**。空 name 不只是「命中不了」，见 F5-c |
| **F5-c** | **空 name 静默失败**：`get_by_role(role, name="")` → **`count == 0`，不抛异常** | 与「元素不存在」**完全同形**（F4）→ 图标按钮会被记成「按钮不存在」→ `unresolved` → 假负样本。**这就是 F5 的死亡路径，已坐实** |
| **F5-d** | **加料 JS 与 playwright 等价**：补齐 5 条规则后，同一 23 个用例 **23/23 命中**，且 name 逐个与 `aria_snapshot` 相同 | §4.5.3 **路径 2 在原理上可行**——不必先上 `aria_snapshot`。但 5 条规则是固定维护成本（见下） |
| **F5-e** | **代价**：42 节点页，JS 枚举 **6–11ms** vs `page.locator("body").aria_snapshot()` **26–32ms / 579B**（≈3–4×，每节点 ≈0.6–0.8ms；逐次抖动可达 2×，探针取 3 次最小值） | **常数开销下界，不是真实页面比例**。真实页面的绝对值必须在第二段量——「快照体积大、单页开销高」在此之前只是推断 |
| **F5-f** | **真同名 → strict mode 多命中**：两个 `<button>关闭</button>` → `count == 2`，朴素/加料/权威三方**全都只能命中 2** | 这不是编码缺陷，是页面事实；任何编码都不能唯一定位 → §4.5.4 的 fail-closed 是正解 |

**F5-d 的 5 条规则**（谁实现采集面的 name 就照这张单子，别自己推）：

1. `aria-labelledby` 引用解析（多个 id **用空格连接**各被引元素的文本）；
2. `aria-hidden="true"` 子树**排除**出文本（朴素链会把它并进来：`×删除条目` vs 真值 `删除条目`）；
3. `<input>` 的 **`title` 优先于 `placeholder`**（HTML-AAM 顺序；朴素链**反了**）；
4. 子 `<img>` 的 `alt` **参与拼接且不插空格**（`图标` + `附加文本` → `图标附加文本`）；
5. 拼接规则：**块级子元素之间插空格，行内子元素之间不插**（`下载`/`客户端` → `下载 客户端`；
   `立即`/`查看` → `立即查看`）。第 5 条复刻的是**布局**语义，探针里靠一张 20 项块级标签表实现——
   **这就是路径 2 的真实成本**：等于自己维护一份 accname 实现，跟着浏览器行为一起漂。

> **离线段不产生路径选择结论。** 它把「哪条路要付什么」变成实测：
> 路径 2 = 5 条规则 + 布局表；路径 1 = 免费但对每个页面付一次快照（下界 ≈4× 枚举开销 + 体积）。
> **第二段（真实页面）必须同时量这两侧**——只量朴素链会得出「JS 不行」的错误结论，
> 而 F5-d 已经证明 JS 补到 5 条规则后与 playwright 完全一致。
>
> **离线段也测不出「元素真不存在」这个干扰项**：本页每个元素都是 JS 亲自枚举出来的，
> 它一定存在，所以 `count == 0` 只能是算错。真实页面上两者同形，必须靠第二段区分。

---

### 1.6 F5（**真实页面段，2026-10-20 实测**）：≥95% 判据的结果

探针：`executor/browser/probe_f5_name.py`（同一支，第二段）。跑法：

```powershell
uv run python -m trajectory_pipeline.executor.browser.probe_f5_name `
  --from-archive trajectory_pipeline/output/pipeline --url-limit 6 --limit 40 --headless
```

6 个真实 URL（v.qq、百度知道、iqiyi ×4，取自 P1 存档的落地 URL），每页前 40 个枚举元素，
共 **240 行**；camoufox 0.5.8 / playwright 1.62.0；`unexplained = 0`。

**分母三层，缺一层就会把结论读歪**：

| 分母 | 值 | 含义 |
|---|---|---|
| 枚举总数 | 240 | 采集层 `INTERACTIVE_SEL` 看到的全部元素 |
| `eligible` | 106 | 确认可寻址（进入任一命名桶） |
| `unique_name_eligible` | **91** | `eligible` 去掉真同名（`multi` 15）——**判据用这个** |

| 臂 | 命中 / 91 | 率 | 说明 |
|---|---|---|---|
| 朴素链（§4.5.3 原链） | 80 | **87.9%** | 不可用 |
| 加料 JS（§1.5 的 5 条规则） | 80 | **87.9%** | **几乎一条没多救**（两次跑分别多救 0 / 1 条） |
| 加料 JS + 伪元素内容 | **88** | **96.7%** | ✅ 过线 |
| playwright 自己的 name | **91** | **100%** | ✅ 过线（同源，构造上不会错） |

逐桶（240 行）：

| 桶 | n | 含义 |
|---|---|---|
| `ok` | 80 | 朴素链唯一定位 |
| `ok_plus_css_only` | 8 | 只有补了 `::before/::after` 的 `content` 才唯一定位 |
| `multi` | 15 | **真同名**（如 iqiyi 每页 3 个 `50集全 电视剧`、百度知道 4 个 `#` 锚链接） |
| `name_wrong_pw_ok` | 3 | playwright 的 name 能定位 ⇒ 近似算错（1 个是作者写错 `role`） |
| `no_accessible_name` | **97** | 有 role 节点但**没有可访问名**（其中**只有 7 个可见**） |
| `hidden_from_a11y` | 29 | 要 `include_hidden=True` 才命中 ⇒ 元素没进可访问性树 |
| `not_in_a11y_tree` | 4 | `aria_snapshot` 空 |
| `stale_element` | 4 | 测量期间 DOM 变了（CSS 路径不再唯一） |
| `unexplained` | **0** | 没有解释不了的 |

| # | 实测 | 后果 |
|---|---|---|
| **F5-g** | **41% 的枚举元素没有可访问名**（97/240），其中 **90 个不可见**（只有 7 个可见） | 采集层**必须先过可见性再枚举**，否则分母里灌进一半「本来就不该出现在交互列表里的隐藏导航链」。剩下的可见无名字元素（logo 空链接、空 `<input>`）只能由 §4.5.3 路径 3 兜底或直接不提供 |
| **F5-h** | **§1.5 的 5 条加料规则在真实页面上几乎不产生增益**（80 vs 80；前一次 79 vs 80）；落差**全部**是 CSS 生成内容：8 个图标链接的 playwright name 是 `\ue608网页`（`::before{content:"\e608"}` 的图标字体字形），而 `innerText` 只有 `网页` | 离线「加料 23/23」是**边界用例选出来的**，不能外推成「加料差不多够用」。真实页面上要么补伪元素内容，要么别自己算（见 F5-i） |
| **F5-i** | **playwright 自己的 name 在唯一名元素上 100/100** | **名字必须由 playwright 算。** 采集层用 `aria_snapshot` 取名，是唯一在构造上不会错的路径；剩下的 3 条失败全是「作者写错 role」这类事前不可枚举的残余 |
| **F5-j** | **真同名占 6.3%**（15/240，`multi`） | 不是编码缺陷，是页面事实；strict mode fail-closed（§4.5.4）是正解。**把 `multi` 算进命中率分母会让任何编码都「差一点」**——这是分母纪律，不是算法 |
| **F5-k** | **真实页面的快照开销**：`page.locator("body").aria_snapshot()` **57–165ms**（839–1511 节点；另一次 4898 节点页 70ms），JS 枚举 **24–30ms** | 每页一次快照的代价是**几十到一百多毫秒**，对「每步一次观察」的采集节奏完全可接受。§1.5 的 3–4× 是常数下界，不是真实比例 |
| **F5-l** | **`get_by_role` 的默认 `name` 是 substring + 忽略大小写**：同一批元素用默认口径 `106/106 = 100%` 命中 | **执行期必须钉 `exact=True`**。默认口径下 P-1 判据是**空的**（什么都命中），而且会把 `\ue608网页`（`::before{content:"\e608"}` 的图标字体字形） 用 `网页` 命中——**多命中与错命中一起被掩盖** |
| **F5-m** | **逐次读数有 ±5pp 抖动**：同一命令两次跑，`no_accessible_name` 88 / 97，`multi` 13 / 15，`ok` 92 / 80 | 这类比例**只能报一位小数**，第三位是噪声。要把数字当结论，先跑两次看是否同侧 |

**判据结论：达标，且推荐路径 1。**

- 路径 1（`aria_snapshot` 取名）= **100%**，同源；
- 路径 2′（JS + 伪元素内容）= **96.7%**，过线但残余（作者 role 写错、
  `content` 为 `url()`/`counter()`/`attr()`）**事前不可枚举**——那正是 D12 最怕的
  「理由通顺的假负样本」；
- 路径 2（§1.5 的 5 条规则，不补伪元素）= **87.9%，不达标**。

差的 3.3% 买不到确定性，而路径 1 的代价只是每页一次几十毫秒的快照。
**D12 继续，P0 之前把采集面的 name 来源改成快照**（§4.5.3）。

---

## 2. 形态：三层怎么变

现有三层（[01-架构总览](../01-架构总览.md)）：

| 文件 | 角色 | camoufox 之后 |
|---|---|---|
| `page_driver.py` | **协议** | **动作面改写为 camoufox 形态**（D12，§3）；采集面不动 |
| `mcp_client.py` | MCP stdio 传输层 | **退为参考**（obscura 专用） |
| `obscura_driver.py` | obscura 接入 + 红线闸门 | **退为参考实现**（不再满足改写后的动作面，**不删**，作为 obscura 形态的存档） |
| — | — | **新增** `camoufox_driver.py`（接入 + 红线闸门） |
| — | — | **新增** `camoufox_session.py`（Playwright 生命周期 / 身份 / 指纹配置） |
| — | — | **新增** `driver_factory.py`（装配唯一入口） |
| `mcp_probe.py` 等 4 个探针 | obscura 取证 | **新增** `camoufox_probe.py`（已建，见 §1） |

### 2.1 协议分成两面——只有动作面变

`PageDriver` 原本 13 个原语其实是**两类东西**，D12 只动其中一类：

| 面 | 原语 | 变不变 | 理由 |
|---|---|---|---|
| **动作面**（模型会选、进动作流、进训练工具声明） | `goto` / `click` / `fill` / `press` / `new_tab` / `close_tab`（`fill`/`press` 旧名 `type_text`/`press_key`，**已改名**，§2.2） | ★**变** | 必须长成 camoufox 的调用面（D12） |
| **采集面**（执行器自己取证用，**不进动作流**） | `snapshot` / `interactive` / `count` / `links` / `extract` / `evaluate` / `observe` | **不变** | 产出 `Observation` 给感知层，是**本项目的取证格式**，与浏览器无关 |

**分界线就是「进不进动作流」**——这条线画得清楚，D12 就只波及动作面。

### 2.2 动作词汇表（camoufox 形态）——D12 的落点

**动作面 1:1 对应 camoufox 的原始调用**：

| 动作 | camoufox 原始调用 | 参数 | 备注 |
|---|---|---|---|
| `goto` | `await page.goto(url)` | `url` | 已是 1:1 |
| `click` | `await page.get_by_role(role, name=name).click()` | `role`, `name` | ★**目标编码变了**（§4.5） |
| `fill` | `await page.get_by_role(role, name=name).fill(text)` | `role`, `name`, `text` | ★同上 |
| `press` | `await page.keyboard.press(key)` | `key`（+ 可选 `role`/`name`） | 带目标时：`await page.get_by_role(role, name=name).press(key)` |
| `new_tab` | `ctx.new_page()` + `page.goto(url)` | `url` | 基础设施动作，模型不可见 |
| `close_tab` | `await page.close()` | — | 基础设施动作，模型不可见 |

**动作名一个都不改**（`goto`/`click`/`fill`/`press`/`new_tab`/`close_tab`）——
它们本来就与 camoufox 的方法名一一对应（`page.goto` / `locator.click` / `locator.fill` /
`keyboard.press` / `page.close`）。
**要改的是参数形状**：`click(tag, label)` → `click(role, name)`。

> **动作名「不改」指的是动作流/toolcatalog 里的 tool 名**（`actions.TOOLS`
> 已经是 `fill` / `press`，本来就跟 camoufox 对得上）。
> 但 `PageDriver` 协议的方法名**曾经**是 obscura 时代的 `type_text` / `press_key`——
> **协议层也要一起改名**，理由同 D12：
>
> | 协议方法（改名前） | 协议方法（D12 之后） | camoufox 侧 |
> |---|---|---|
> | `type_text(text, ref=…)` | **`fill(role=…, name=…, text=…)`** | `locator.fill(text)` |
> | `press_key(key)` | **`press(key)`** | `keyboard.press(key)` |
>
> **`type_text` 这个名字本身就在撒谎**——它暗示「逐字追加」，
> 而 camoufox 的 `fill` 是**设值**（先清空再写）。
> 改名之前 `obscura_driver` 在 `type_text` 入口里做了一次翻译
> （tools.md 原语 5：「新实现若只提供 `type` 也要翻译成『先清空再写』」）——
> 名字留着，下次有人照着 `type_text` 写个真的追加就又错一次。
>
> ✅ **改名已落地（P2，2026-10-20，§4.6）**：协议方法现在是 `fill` / `press`，
> `obscura_driver` / `camoufox_driver` / `agent_loop._apply` / `steps/site_search`
> 四处调用点与全部假 driver 同步。**两侧同名不同参**：camoufox 面收 `role`/`name`，
> obscura 面收 `ref`/`selector`，**各自收到对方那面时抛 `ValueError`**
> （不抛 `DriverError`——那会被 `steps/aggregator` 吃成 `unreachable_hard`，
> 接线 bug 就变成一条理由通顺的负样本）。

**采集面不进动作流，所以不进训练工具声明**——`observe` / `snapshot` / `count` 这些
是执行器取证用的中间物，模型永远看不到，也不需要能执行。

> ⚠️ **上面这句是 push 模式的产物，2026-10-20 起作废**（[设计方案 15](15-执行器与agent-runtime重新定义.md) D-A7 / D-A9）：
> pull 模式下**由 agent 主动发起的读就是动作**——`snapshot` / `interactive` / `links`
> 已经进 `camoufox-1` 工具面（模型可见，**只进声明、还没接上**，R3 才落地）。
> 「模型永远看不到」在 push 模式下成立，**在 pull 模式下不成立**。
> `observe` / `count` / `evaluate` 仍然不进声明——那是**执行器自己的取证中间物**，
> 区别在于「谁发起」：执行器发起 = 回执（不进动作流），agent 发起 = 动作（进）。

### 2.3 `driver_factory.py`——**P0 已落地（2026-10-20）**

[obscura_driver.py:1](../executor/browser/obscura_driver.py#L1) 的 docstring 一直承诺「将来换浏览器，只改本文件 + `factory`」，
但 **factory 从未存在**：cli.py 里曾有 **6 处**硬编码 `ObscuraDriver(client)`
（`_run_manual` / `_run_plan` / `cmd_check` / `_probe_one` / proberun / mediabrowse）。

现已收敛到 [`driver_factory.py`](../executor/browser/driver_factory.py)：`make_driver(client, *, kind=DEFAULT_KIND)`，
6 处调用点全部改成 `make_driver(client)`。**对 obscura 是纯重构、零行为变化**，
并且它现在是「以后还能换回 obscura / 加第三个实现」的唯一挂点。

两条设计决定，都是踩过之后写的：

- **认不出实现名就 `ValueError`，不回落**。静默回落到默认实现，会让「配错了」
  与「配对了」产出**同一份 P1 存档**——混池时看不出来。
- **`FakePageDriver` 不进 factory**。它是测试替身，不是执行层实现；做成一个 `kind`
  等于给「生产链跑假驱动」留一条正路，而这种错**不报错**，只是数据全假。

**门禁**：[`tests/unit/test_driver_factory.py`](../tests/unit/test_driver_factory.py) 用 AST 扫全树——
`executor/browser/` 之外（`tests/`、`research/` 豁免）**不许 import 或构造具体驱动类**，
第 7 处硬编码在那里红，而不是在换实现那天才被发现。mediabrowse / plan 的接线测试也一并改成
patch `make_driver`，顺带把「cli 必须走装配入口」钉住。

**P1 的增量（2026-10-20）**：factory 多了 `kind="camoufox"` 分支，签名变成
`make_driver(client=None, *, kind=DEFAULT_KIND, camoufox_session=None)`。
**`DEFAULT_KIND` 仍是 `"obscura"`**——生产链一行没改，camoufox 只能由
`check --driver camoufox` 显式点名。两处「缺句柄就报错不代建」都写了单测：
obscura 缺 `McpClient`、camoufox 缺 `CamoufoxSession` 各自 `ValueError`。
camoufox 分支的 `import` 是**延迟的**：它没装不该让生产链 import 失败。
AST 门禁的类名集合同步加上 `CamoufoxDriver`。

⚠️ **P0 的「存档逐字节同形」判据没能在本机执行**——`run` 需要 `OBSCURA_EXE`
（P0 当时本工作区未配）。代偿证据：改动只有 6 行构造 + 1 行 import；接线测试覆盖连接生命周期
（连接复用 1 次 / `async with` 进出 / 每 URL 一次 `goto`）。**后来 `OBSCURA_EXE` 已配上
（`check` 连到 obscura 0.2.4）**，补跑可行，但 P0 前的基线存档得另找。

### 2.4 D12 之后，「换执行层不动上层」这条要改口径

D11 的说法是「协议不动 → `steps/` / `perception/` / `orchestrator.py` 全不碰」。
D12 之后这句**不再成立**，但**主张本身没塌**——塌的是「一个字符都不改」这种说法：

| 层 | D12 之后 | 理由 |
|---|---|---|
| `perception/` | **不动** | 判断全在感知层（`Perceptor.decide`），与浏览器无关 |
| `orchestrator.py` | **不动**（除 `actions.` 调用点的参数） | 控制流（遍历谁、何时终止、失败是否入池）全是代码，与浏览器无关 |
| `steps/` | ★**改几行调用签名** | `driver.click(ref=ref)` → `driver.click(role=..., name=...)`；**控制流不变** |

**「代码层做护栏」这条主张的兑现方式变了**：
从「换执行层零改动」变成「**换执行层只改事实层的接线，不改判断层与控制流**」。
后者其实更准确——D11 那句「零改动」本身就有点可疑：
`ref` 机制在 obscura 与 camoufox 下根本不是一回事（obscura 的 `ref` 是 tool 返回的句柄，
camoufox 没有这个概念），所以「13 原语一个不改」在 `click` 这一格上从一开始就是假的。

> **给后人留一句**：D12 之后再看这个方案，若发现 `steps/` 动了——
> **那是接线，不是控制流**。判断点、分支、终止条件、失败样本入池规则一个字都不许跟着动。

---

## 3. 原语映射表

**判据变了**：动作面的原语**签名与语义都要变成 camoufox 形态**（D12），
采集面的原语签名不变、只换实现。

### 3.1 动作面（进动作流、进训练工具声明）

| 原语 | obscura（现状） | camoufox（目标） | 落差 |
|---|---|---|---|
| `goto` | `browser_navigate` | `page.goto(url, wait_until=...)` | 无——**已是 1:1** |
| `click` | `browser_click`（`ref` 会话句柄） | `page.get_by_role(role, name=name, exact=True).click()` | ✅ **camoufox 面已落地（P2，§4.6）**；obscura 面仍在跑 `ref`。★★**目标编码换掉**（§4.5）。**禁止** `evaluate` 伪造（F3 已证 isTrusted=false） |
| `fill` | `browser_fill`（`ref`） | `page.get_by_role(role, name=name, exact=True).fill(text)` | ✅ 同上 |
| `press`（原名 `press_key`） | `browser_press_key` | `page.keyboard.press(key)` / `locator.press(key)` | ✅ 落地（P2，§4.6）。**按名字走时**两种写法都在用：`fill` 后提交是 `locator.press`（焦点已在框上，等价于按在该元素上） |
| `new_tab` | `browser_tab_new` | `ctx.new_page()` | ★注意 obscura 是「单 browser 多 tab」、playwright 是「多 context 多身份」——**多站遍历的口径要统一** |
| `close_tab` | `browser_tab_close` | `page.close()` | 无 |

> **动作名不变、参数变**。`click` 从「ref 定位」变成「role+name 定位」——
> 这不是实现细节，是**契约变更**：`ref` 是会话内句柄（换 session 即失效），
> `role+name` 是**可移植的页面事实**。D12 就是要把它换掉。

### 3.2 采集面（不进动作流，模型看不到）

> ⚠️ **「模型看不到」只对「执行器发起的读」成立**（2026-10-20 方案 15 D-A7）：
> agent 主动发起的 `snapshot` / `interactive` / `links` **要进动作流、进训练
> `tool_calls`**（`camoufox-1` 工具面已含这三条）。本节的**原语映射表仍然有效**
> ——它讲的是「执行器怎么取观察」，与「谁发起」无关。

| 原语 | obscura（现状） | camoufox（目标） | 落差 |
|---|---|---|---|
| `snapshot` | `browser_snapshot` + `browser_evaluate(innerText)` | `page.title()` / `page.url` + `evaluate(innerText)`；**不走 `parse_snapshot`** | ★`body_source`/`truncated`/`raw_len`/`stripped_ratio` **必须同口径**，否则 D-2 打分漂移 |
| `links` | `browser_links`（NDJSON 解析） | `eval_on_selector_all("a", ...)` | ★obscura 的 NDJSON 解析器**作废** |
| `interactive` | `browser_interactive_elements`（列式文本解析） | **自建**：按 **ARIA role** 枚举 + accessible name | ★★最大工作量，见 §4.1 + §4.5 |
| `count` | `browser_count` | `locator(sel).count()` | 无 |
| `extract` | `browser_extract`（`schema` 键） | 逐字段 `eval_on_selector_all` | ★须复刻 obscura 的 **`[]`=取全部 / `@attr`=取属性**两条约定——漏了会**静默失效**，症状与 [dom.py:73](../executor/dom.py#L73) 那条「抽不到标题」完全同形 |
| `evaluate` | `browser_evaluate` | `page.evaluate` | ★isolated world 边界，见 F2 |
| `observe` | 组合采集 | 组合采集 | ★`Observation` 各字段同口径 |

### 3.3 `parse_*` 解析器的去留

`dom.py` 里的解析器分两类，**去留不同**：

- **obscura 专有、随替换作废**：`parse_links`（NDJSON）、`parse_interactive_elements`（列式文本）、
  `parse_snapshot`（`URL:`/`Title:` 前缀剥离）、`parse_count`（obscura 的返回形态）。
  → 移入 `obscura_driver.py` 私有，或留作参考实现的一部分。
- **跨实现通用、必须保留**：`parse_body_text`（innerText 空白规范化）、`parse_extract`（JSON 解析）、
  `is_mojibake`、`looks_like_error`（**要按 playwright 语义重写**，见 §4.2）。
- **obscura 的 fallback 表达式要搬**：`TEXT_CONTENT_EXPR`（clone + 删 script/style/noscript/template）——
  实测 camoufox 的 `textContent` 同样含 script 正文，**不搬会重演「假恢复」**。

---

## 4. 四个真正的难点

### 4.1 ref 机制——**不是新造，是拆掉**

obscura 的 `interactive()` 返回 `e1/e2` 会话内句柄，`click(ref=...)` 直接定位。
Playwright 没有这个概念。[visit.py:206](../executor/steps/visit.py#L206)、
[site_search.py:296](../executor/steps/site_search.py#L296)、
[site_search.py:463](../executor/steps/site_search.py#L463) 都在用 ref。

> ⚠️ **D12 改了这件事的性质**。原先的方案是「ElementHandle 缓存」——
> 造一个和 obscura 的 `ref` 等价的会话句柄机制。
> **D12 之后这条路是错的**：`ref` 之所以进不了训练数据，正是因为它是会话内句柄
> （[actions.py:22](../executor/actions.py#L22) 的注释说得很清楚）。
> 造一个 camoufox 版的 `ref`，等于把同一个毛病换个实现再犯一遍——
> 数据照样出不了本项目。

**改成：动作面直接用 `role + name` 定位**（§4.5），**不造句柄机制**。
`ref` 只留在**采集面**内部（`Observation.interactive_elements` 里的 `ref` 字段
供判断点 ③ 回传用），**不进动作流**。

**代价**：`role+name` 定位**不如 handle 稳**——同名元素会 strict mode 报错。
这正是要显式设计的（§4.5）。

**为什么不用 `data-tp-ref="e1"` DOM 属性注入**：
camoufox 是**反检测浏览器**，站点 JS 枚举一下 attribute 就能看到满页 `data-tp-ref`——
这与 camoufox 本身在做的事直接对冲。**用一个反检测浏览器去实现一个反检测泄漏点，是自相矛盾。**

**采集面要不要留句柄缓存**：`Observation.interactive_elements` 带 `ref`，
判断点 ③ 要回传它（[perception/base.py:42](../perception/base.py#L42)）。
**可以**在采集面内部维护 `ref → handle` 缓存供**感知层定位**用——
**但它绝不能进动作流**（[actions.py:22](../executor/actions.py#L22) 的理由仍然成立），
**也不能进训练工具声明**。
动作面另走 `role+name`（§4.5）。

**失效边界已实测（F4）**：元素被替换 → handle `isConnected=false`，`click()` 抛 `Error`。
这个语义**比 obscura 更明确**——obscura 的 ref 在页面变化后的行为没测过。
处理：handle 失效 = **业务事实**（元素已不在页面上）→ 静默返回 → 上层转 `unresolved`，
与 obscura 的「元素不存在时静默返回」一致（[obscura_driver.py:275](../executor/browser/obscura_driver.py#L275)）。

**未验证**：obscura 的 `interactive` 与 camoufox 的枚举规则**不同**（可交互元素的定义域、role/name 提取规则），
因此 `Observation.elements_total` 在两个实现下**不同义**。替换后要重新标定这条的语义。

### 4.2 错误三态的映射——`looks_like_error` 要重写

obscura 的错误有两种形态（[dom.py:438](../executor/dom.py#L438)）：`isError=True`，
或正文以 `Error:` 开头但 `isError=False`（业务事实）。

playwright 侧是**异常**：
- 元素**不存在** → `TimeoutError`
- 元素**已脱离 DOM** → `Error`（F4）
- 传输/浏览器崩溃 → 其它异常

**关键歧义**：「元素不存在」与「页面卡住」**都是 `TimeoutError`**（F4）——
一个该转 `unresolved`（业务事实），一个该转 `DriverError`（能力不可用，fail-closed）。

**解法：先 `count()` 再操作**。
`count == 0` → 元素不存在 → 业务事实 → 静默返回（`on_error="empty"` 语义）；
`count > 0` 但操作超时 → 能力不可用 → `DriverError`。
**这是确定性区分，不靠解析异常消息**（playwright 的消息文本会随版本变）。

**代价**：每次 click/fill 多一次往返。**可接受**——正确性优先于往返数。
且 `count` 结果可以缓存到下一次 `interactive()`（同一页内元素集不变）。

> ✅ **已按此落地（P2，2026-10-20，§4.6）**，一处与上段的「缓存」不同：
> **不做跨动作缓存**——`{role,name}` 是页面级定位，导航后旧名字不失效而是**静默命中新页同名元素**；
> 缓存把这条风险放大成「拿上一页的结论点这一页」。三态与 `count` 失败的处置见 §4.6 的表。

### 4.3 身份与 viewport——必须钉死，否则分布不可归因

F1：camoufox 每次 launch 抽全新身份，**`screen` 尺寸逐次不同**。

**方案**：
- **一个 Run = 一次 launch**（与 obscura「多站遍历复用连接」同构）→ 同 Run 内身份与 viewport 恒定。
- **显式钉 `screen=` / `window=`**（如 `Screen(1920, 1080)` + `window=(1920, 1080)`）→
  消除逐 Run 漂移，否则响应式站点的内容分布变化**无法归因到站点变化**。
  > ⚠️ **两个都要钉，且各管一件事**（[依赖说明 §11.4](../依赖说明/camoufox.md)）：
  > `screen=` 约束**指纹里**的屏幕尺寸，`window=` 才是**实际视口**——
  > `new_context()` 默认 `no_viewport=True`（视口 = 窗口尺寸），只钉 `screen` 视口仍会漂。
- **不 pin 身份预设**（`fingerprint_preset`）：预设是「真实设备」，跨 Run 复用同一台设备会让
  一批样本带着同一指纹，反而是关联特征。**每次重抽 + 钉死几何**是更稳的组合。
- **`persistent_context=True` 一律不开**（[依赖说明 §4.1](../依赖说明/camoufox.md) 已标「技术上可行但纪律上不做」）。
- **`os=` 显式钉 `"windows"`**（与宿主一致）：不传时 fpgen 在
  `["windows","macos","linux"]` 里**随机挑**，字体/GPU/核数会跟着漂。

**批次元数据必增**（对齐 CLAUDE.md「并池是写入时的动作」与 [依赖说明 §9](../依赖说明/camoufox.md)）：

```
driver = "camoufox"
fingerprint: { os, geoip, locale, timezone, humanize, headless, screen, window, dpr }
```

**不记的话**，某天换了 locale 或 screen 尺寸，批次分布会跳变，而读存档的人会读成「质量变好了」。

> ⚠️ **`headless` 是 2026-10-21 加的第九项**（口径反转带来的，见 §4.9.5）。
> 它是**请求值**（读不回来），但它决定「这批为什么快」——
> 无头 34–71ms vs 有头不可渲染时 5–13s。不记就解释不了分布跳变。

#### 4.3.1 实测读数（P1，2026-10-20，`camoufox 0.5.8` / `firefox 156`）

> ⚠️ **下面这组读数是 2026-10-20 在 `headless=False` 下量的**。
> 2026-10-21 口径反转成「无人值守走无头」（§4.9.5）之后，
> **无头口径下的指纹读数要重测**——尤其是 `window`（内高 940/948 摆动）
> 与 `dpr`（1.5）会不会变。**重测前，上表只对 headful 口径成立。**
> 这一条本身也是「`headless` 必须进批次元数据」的最好理由：
> 换了执行窗口，同一台机器给出的几何读数就可能不同。

同机连续 **8 次** `check --driver camoufox`，读的是浏览器自己报的值（不是请求值）：

| 字段 | 实测（8 次） | 结论 |
|---|---|---|
| `screen` | `[1920, 1080]` ×8 | ✅ `Screen(min=max)` **钉得住**（F1 的逐次漂移已消除） |
| `window`（inner） | 宽恒 `1905`；高 **`940` ×5 / `948` ×3** | ⚠️ **`window=` 钉的是外框**：视口 = 外框 − chrome，且**内高逐次摆动 8px** |
| `dpr` | `1.5` ×8 | 由 fpgen 给，未钉；进批次元数据 |
| `timezone` | `Asia/Shanghai` ×8 | 由 `locale` 推导，稳定 |
| `ua` / `webdriver` / `cores` | `Firefox/156.0` / `False` / `16` | 稳定 |

**结论要写准**：钉死 `screen`/`window` 之后，**屏幕尺寸完全稳定，视口宽度稳定，视口高度仍有 ±8px（0.85%）的抖动**
——这 8px 只可能来自 fpgen 为「外框 1080」随机挑的浏览器 chrome 高度。量级上远小于响应式断点，
**不影响归因**；但它证明「钉 `window=` 就等于钉视口」这句话是**错的**，所以：

1. 批次元数据必须记**实测** `window`/`dpr`（[`camoufox_session.py`](../../executor/browser/camoufox_session.py)
   的 `fingerprint` 就是这么实现的：`screen`/`window`/`dpr`/`timezone` 全取读数）；
2. 真要钉死视口，得在 `new_context(viewport=…)` 上钉——**当前不擅自加**：它会与 camoufox 的
   `no_viewport` 语义冲突（视口尺寸与它 spoof 的 `window.outer*` 可能对不上，那本身是指纹破绽），
   属于未验证改动。列进 P5 前的待办（§9）。

一个顺带的对照：`example.com` 上 camoufox 采到 `body=917ch`，
与 obscura 同页 `body=917ch` **完全一致**（正文源都是 `innerText`，口径未漂）。

### 4.4 三个「默认关」与一个「默认装」——不显式写就等于没开

源码复核结论（[依赖说明 §11](../依赖说明/camoufox.md)）：

| 项 | 默认 | 本项目处置 | 理由 |
|---|---|---|---|
| `humanize` | **关** | ✅ 显式 `True` | 不开等于鼠标瞬移，行为特征裸露 |
| `geoip` | **关**（且需 `maxminddb`） | ⚠️ 默认**保持关** | 当前 venv 缺 `maxminddb` → `geoip=True` **直接抛 `NotInstalledGeoIPExtra`**。要开须先 `uv pip install "camoufox[geoip]"`。**不因「反检测更强」而强行开**——它会外发一次公网 IP 探测请求，且与出口 IP 绑定后批次就不自由了 |
| `main_world_eval` | **关** | ✅ 显式 `False` | 开了扩大采集面（见 §5） |
| `addons` | **默认装 uBO** | ✅ `exclude_addons=[DefaultAddons.UBO]` | uBO 拦广告 CDN，**可能连带拦掉播放器初始化/预取请求** → 播放器起不来 → 判「无播放控件」→ **假负样本**。宁可少一层画像，不可少一层真值 |

### 4.5 目标编码：`role + name`——D12 落地的难点

动作面不用 `ref`（会话句柄，不可移植）、不用 `tag+label`（camoufox 没有对应 API），
那就只剩 camoufox **原生**的定位方式：`page.get_by_role(role, name=name)`。

#### 4.5.1 三种编码的取舍

| 编码 | 对应的 camoufox 调用 | 可移植 | 判断 |
|---|---|---|---|
| `ref`（会话句柄） | 无 | ❌ 换 session 即失效 | ✗ 旧方案的错 |
| `{tag, label}` | `locator("button:has-text('立即播放')")` | ⚠️ 半 | ✗ `<input>` 无文本内容 → `:has-text` **永不命中** |
| **`{role, name}`** | `get_by_role("button", name="立即播放")` | ✅ | ✓ **采用** |

`{role, name}` 是 playwright/camoufox 的**一等公民**：
`name` 走**可访问名（accessible name）**算法，所以 `<input placeholder="搜索">` 的 name 就是「搜索」，
`<button><img alt="播放"></button>` 的 name 就是「播放」——**输入框与图标按钮都覆盖得到**。

#### 4.5.2 ARIA role 词表——**必须短**

role 词表是**枚举侧与判定侧共用的**一张表，长的表没人维护（CLAUDE.md 的老教训）。
只收本场景真用得到的：

| role | 覆盖 | 备注 |
|---|---|---|
| `button` | `<button>`、`role=button`、部分点击态 `<div>` | 播放按钮、提交按钮 |
| `link` | `<a href>` | 搜索结果、影片条目 |
| `textbox` | `<input type=text>`、`<textarea>` | 站内搜索框 |
| `searchbox` | `<input type=search>` | 站内搜索框 |
| `combobox` | `<select>`、带 `role=combobox` 的输入 | 筛选器 |
| `checkbox` / `radio` | 单选多选 | 少见但真实存在 |
| `tab` | 选项卡 | 少见但真实存在 |

**收窄口径**：词表之外的 role **不枚举**——宁可少列，
也不要列一堆本项目用不到、且各自带一堆边界情况的 role。
`SEARCH_BOX_TAGS` 最终改成 `{"textbox","searchbox"}`。**P3-α 先落的是并集**
（`{"input","textarea"}` ∪ `{"textbox","searchbox"}`）：两代 driver 同时存在期间，
只留一套会让另一条执行层**静默取回 0 个候选**——与「这页没有搜索框」同形。
收窄到单套是 P4 的事（§4.5.6 偏离 2）。

#### 4.5.3 ★ F5（**两段都已验，2026-10-20**）：采集层算的 name 未必是 playwright 算的 name

**这是本方案最可能翻车的一处。** playwright 的可访问名算法比「取 innerText」复杂：
`aria-labelledby` 引用、`aria-label` 覆盖、`alt`/`title`/`placeholder` 回退、
以及「可见文本要拼起来」的规则。

采集层若用 JS 近似算（`aria-label || innerText || placeholder || title || alt`），
**算出来的 name 可能与 playwright 内部的不一致** →
`get_by_role(role, name=采集层算的)` **找不到元素** →
表现为「点了但没反应」，而这个症状与「元素真不存在」**完全同形**（F4）。

**实测已落地，离线段见 §1.5（F5-a ~ F5-f），真实页面段见 §1.6（F5-g ~ F5-m）**。四句话：

- **role 侧零落差**（25/25 一致）→ §4.5.2 的短词表可用，落差全在 name 侧；
- **朴素链不可实现**：65% 命中，且图标按钮**产出空 name**，而空 name 的执行期结局是
  `count == 0` **不报错**——与「元素不存在」同形，**就是这条死亡路径**；
- **加料 JS 在离线等价、在真实页面上几乎不增益**：离线 5 条规则补到 23/23，
  真实页面上比朴素链只多救 **0–1 条**（§1.6 F5-h）——离线用例挑的是「规则能救的边界」，
  真实页面的落差是 CSS 生成内容，规则救不了；
- **playwright 自己的 name 在真实页面唯一名元素上 100%**（§1.6 F5-i）→
  **判据达标，推荐路径 1**。

**三条候选路径（真实页面段的读数已标在后面）**：

1. **权威优先**：用 playwright 自己的 `locator.aria_snapshot()` 取 role+name——
   它输出可访问性树，name 是 playwright 自己算的，**与定位器同源**。
   代价：**每页一次快照**，实测真实页面 **57–165ms**（839–1511 节点，§1.6 F5-k），
   相对 JS 枚举的 24–30ms 是 2–6×，但对手均几十秒的一步操作可以忽略。
   **真实页面命中率 100%（91/91 唯一名元素）→ 推荐就用它。**
2. **JS 近似**：**不是原来那条朴素链**，而是 §1.5 的 5 条规则版。
   离线 23/23，**真实页面只有 87.9%**（加料比朴素多救 0–1 条，§1.6 F5-h）；
   再补一层 `::before/::after` 的 `content` 才到 96.7%——过线，
   但 `content` 还能是 `url()`/`counter()`/`attr()`，残余事先不可枚举。
3. **退化兜底**：`get_by_role(role)`（不带 name）→ 若命中多个，playwright 的
   **strict mode** 会抛错并**列出候选** → 由**代码**按文本匹配挑一个
   （这是代码做的确定性选择，**不是让 LLM 猜**）→ 仍不确定则 fail-closed 记 `unresolved`。
   ⚠️ 实测候选池不小：**同 role 候选数的中位数是 17**（button 一档）——
   这条路只适合兜底，不适合当主路。它真正的用武之地是 **41% 没有可访问名的元素**
   （§1.6 F5-g），以及 `multi` 那种 6.3% 的真同名。

> **采集面的口径（D12 的实现约束）**：枚举仍走 DOM（要 ref / 几何 / 可见性——
> **可见性必须过滤**，否则 41% 的隐藏无名字元素会灌进交互列表，§1.6 F5-g），
> **name 一律取 playwright 的结论**，不许自己重算。这条把「两边算法漂移」
> 从设计里彻底删掉，代价只是每页一次快照。执行期定位**必须 `exact=True`**——
> 默认是 substring + 忽略大小写，实测那会让命中率变成 100%（判据变空），
> 且用 `网页` 能命中 `\ue608网页`（§1.6 F5-l）。

#### 4.5.4 strict mode 是白送的 fail-closed

playwright 的定位器默认 **strict mode**：命中多个元素时**报错并列出候选**，
不做「取第一个」。这正好是本项目要的行为：
**不猜**（CLAUDE.md 的 fail-closed）。

| 情形 | playwright 行为 | 本项目处置 |
|---|---|---|
| 命中 0 个 | `TimeoutError`（F4） | `count()` 预判 → 业务事实 → 静默 → 上层转 `unresolved` |
| 命中 1 个 | 正常执行 | ✅ |
| 命中多个 | **strict mode 报错 + 列候选** | 按 §4.5.3 路径 3 由代码选；选不出 → fail-closed |

**这条能力是 camoufox 相对 obscura 的净增益**：
obscura 的 `browser_click` 遇到多个候选时行为未测过。

> ✅ **落地口径（P2，2026-10-20，§4.6）与上面这张表有一处刻意偏离**：
> 多命中**不交给 strict mode 去报错**，而是 `count()` 先算、`>1` 就**不动手**记 `ambiguous`。
> 理由是 strict mode 的报错和「元素不存在」的报错在 playwright 里**长得一样**
> （都是 `TimeoutError`，F4 已测过），靠解析消息分三态是在赌文案。
> 行为等价（都是 fail-closed、都不猜），差别只在**证据**：`last_action.outcome`
> 让「点不了是因为同名」和「点不了是因为没有」在存档里分得开。
> 「由代码按文本挑一个」（§4.5.3 路径 3）**仍未实现**——落地前 `ambiguous` 一律不动作。

#### 4.5.5 契约迁移面（D12 动到的东西）

| 位置 | 现在 | 之后 |
|---|---|---|
| `executor/browser/page_driver.py` 协议 | ✅ **已改（P2，§4.6）**：`click(ref=,selector=,role=,name=)` / `fill(text,ref=,selector=,role=,name=)` / `press(key,*,role=,name=)`——**同名不同参**，面由实现认，跨面抛 `ValueError` | `click(role,name)` / **`fill(role,name,text)`** / **`press(key)`**（方法名一并改成 camoufox 形态，见 §2.2） |
| `executor/actions.py` `ActionTarget` | `{tag, label}` | `{role, name}` |
| `perception/base.py` `InteractiveElement` | `{ref, tag, label}` | `{ref, role, name}`（`ref` 保留在采集面） |
| `perception/rule_perceptor.py` `SEARCH_BOX_TAGS` | `{"input","textarea"}` | `{"textbox","searchbox"}` |
| `perception/llm_perceptor.py` 提示词与 payload | `tag` / `label` | `role` / `name` |
| `toolcatalog/` `click` / `fill` | `click(tag,label)` / `fill(text,tag,label)` | `click(role,name)` / `fill(role,name,text)` |
| P1 `steps[].action.target` | `{"tag":…,"label":…}` | `{"role":…,"name":…}` |
| `tests/contract/test_tool_catalog_contract.py` 禁用词表 | 禁「实现名」 | **改口径**：禁 obscura/MCP/句柄，**允许 camoufox/get_by_role** |

> ⚠️ **已归档的 361 份 P2 样本是 obscura 时代的**，`target` 形状是 `{tag,label}`。
> **新旧两批不可混池**（口径不同义）——这与「实现名要落进批次元数据」是同一条纪律。
> `toolcatalog` 的 `required` 口径也是按盘上样本的出现率统计的
> （[toolcatalog/__init__.py:59](../toolcatalog/__init__.py#L59)），
> **换编码后必须重算**，否则 `test_tool_catalog_contract.py::Test参数口径与真实存档一致` 会红。
> → 这条要进 P5 的迁移清单，**不能等门禁红才发现**。

#### 4.5.6 P3-α 落地（2026-10-20）：`interactive()` 与三处**已知偏离**

实现：[`camoufox_driver.py`](../../executor/browser/camoufox_driver.py) 的 `interactive()`
+ 纯解析器 [`aria_snapshot.py`](../../executor/browser/aria_snapshot.py)（不碰浏览器，可离线单测）。

**做法：一页一次 `page.aria_snapshot()`，role+name 全取它的结论。** 这是量出来的，不是选的
（200 个按钮/链接的合成页，本机实测）：

| 做法 | 成本 | 结论 |
|---|---|---|
| 每元素 `locator.aria_snapshot()` | 3.3ms × N | 慢且 N 次往返 |
| 每元素 `is_visible()` | 5.8ms × N | **最贵的一项** |
| 每元素 `evaluate(tagName)` | 3.6ms × N | 只为拿 HTML 标签 |
| 一页一次 `page.aria_snapshot()` | **51ms / 页** | ✅ 采用（205 元素逐元素方案合计 **4.8s**） |

**三处偏离上面那条「采集面口径」，逐条记账（都不是漏做）**：

1. **可见性没有逐元素过滤**（上一条 blockquote 要求「可见性必须过滤」）。
   `aria_snapshot` 本身已不含 `display:none` / `aria-hidden` / `visibility:hidden`；
   残余的不可见元素**若无名字会被下一条过滤掉**，有名字的仍可定位（是噪声，不是坏目标）。
   代价是 5.8ms×N，故不做。**这是成本换来的缺口**：若 P5 双跑时
   `interactive_elements` 条数中位数差 > 30%（§6.1 判据），先补这一项再谈其它。
2. **`tag` 字段装的是 ARIA role**（`button`/`textbox`…），不是 HTML 标签名。
   `InteractiveElement` 的字段名留到 P4 一并改成 `role`/`name`——现在改会让
   v1 归档数据与新数据**混池**。下游两个消费点已按「标签名 ∪ role」放宽：
   [`steps/site_search._SEARCHABLE_TAGS`](../../executor/steps/site_search.py) 只补 `link`
   （role 下的提交控件本来就叫 `button`；`textbox` 点下去**不会提交**，
   收进来会把「回车失败后的第二条路」变成「点一下搜索框然后什么都没发生」）；
   [`perception/rule_perceptor.SEARCH_BOX_TAGS`](../../perception/rule_perceptor.py)
   补 `textbox`/`searchbox` 而**不收 `combobox`**（playwright 给 `<select>` 的 role 就是
   `combobox`，往 select 里 `fill` 必失败——「取回一个填不进去的 ref」比取回 0 个更坏）。
   两套并集是**过渡态**——**P4 已收窄成逐面单套**（2026-10-20，见 §4.8）：
   `_SEARCHABLE_WORDS` / `SEARCH_BOX_WORDS` 变成 `{html_tag: …, aria_role: …}` 两张表，
   按 `Observation.element_vocabulary` 取，**认不出的词表直接抛**（不回落成并集）。
3. **无名与真同名整组丢弃**。两类都**定位不到**：无名 → `get_by_role(role, name="")` 命中 0
   且不报错；同名 → strict mode 报错。丢弃数量记在 `last_enumeration`
   （`nodes` / `role_matched` / `nameless_dropped` / `ambiguous_dropped` / `kept` / `truncated`）——
   **「页面上真没有可点目标」与「全被过滤了」在观察里同形，只有这份账目分得开。**
   这不是可选的诊断字段：`check --driver camoufox` 每次都打它。

**真实页面读数（2026-10-20）**：`example.com` → `nodes=8 / kept=1`（唯一元素是
`link "Learn more"`，与肉眼所见一致）；`www.baidu.com` → `nodes=101 / role_matched=34 /
nameless_dropped=0 / ambiguous_dropped=0 / kept=34`（导航栏 + 热搜榜，肉眼可逐条核对）。

> ★**新发现（进 P2 的设计输入）**：`textbox` 的可访问名可能是**动态内容**——
> 百度搜索框枚举出的是 `textbox "湖北广水市委书记崔传金被查"`，那是当时的
> 热搜占位词，不是输入框的固定标签。推论两条：
> ① `{role, name}` **只在同一次枚举内有效**，跨页/跨时序复用名字会静默失配；
> ② P2 的 `count() → 操作` 预判必须**按当次枚举的名字现算**，
> 不能拿存档里上一次的名字去定位。名字不稳定这件事本身**不需要额外处理**——
> 定位不到会走 §4.5.4 的「命中 0 个」分支，是显式失败而不是错点。

### 4.6 P2 动作面落地（2026-10-20）：`click` / `fill` / `press`

实现：[`camoufox_driver.py`](../../executor/browser/camoufox_driver.py) 的三个动作原语 +
协议改名（[`page_driver.py`](../../executor/browser/page_driver.py)：`type_text` → **`fill`**、
`press_key` → **`press`**）。动作名与 camoufox 的调用面**逐字一致**，语义层与驱动层之间
那张翻译表**已删除**（它存在过一天，就在 `agent_loop._apply` 里把 `fill` 译成 `type_text`）。

**三态映射（`count()` 预判，不解析异常消息）**：

| 情形 | 动作 | 返回 | `last_action.outcome` |
|---|---|---|---|
| `count == 0` | 不操作 | `None` | `not_found`（业务事实） |
| `count > 1`（真同名） | **不操作** | `None` | `ambiguous` |
| `count` 本身报错 | — | 抛 `DriverError` | — |
| 操作抛错（元素在页上） | — | 抛 `DriverError` | — |

四个边界都是刻意选的：

1. **`count` 失败抛而不折叠成 0**——与同文件的 `count(selector)`「失败返回 0」**有意不一致**。
   那条是对齐 obscura 的旧口径（P1 的目标是行为可比），这条是判断的前置：
   把故障折叠成 0，就再也分不出「页面卡住」与「元素不存在」。
2. **真同名不点**。方案 §4.5.3 的「路径 3 由代码按文本挑一个」还没落地，落地前
   选哪个都是赌，而**点击不可逆**。记账、返回、让上层看得到。
3. **词表外 role / 空 name 直接判 `not_found`，不问浏览器**——playwright 对未知 role
   抛 `ValueError`，那会被误读成能力故障；空 name 则命中 0 且不报错（F5-c 的死亡路径）。
4. **跨面参数抛 `ValueError`，不抛 `DriverError`**。`ref`/`selector` 是 obscura 面的词汇，
   拿它调 camoufox 是**接线错误**；而 `DriverError` 会被 `steps/aggregator` 捕获成
   `unreachable_hard`——于是一个接线 bug 变成一条**理由通顺的负样本**。接线错误必须炸穿。
   对称地，`ObscuraDriver` 收到 `role`/`name` 也抛 `ValueError`（它的上游会静默忽略多给的参数，
   于是「按 role+name 点」退化成「什么都没指定」）。

**验收读数（真浏览器，2026-10-20）**：`check --driver camoufox --action-smoke`

| 页面 | 动作 | 结果 |
|---|---|---|
| `example.com` | `click(link "Learn more")` | `ok`，页面 → `iana.org/help/example-domains` |
| `example.com` | `click(button "__不存在__")` | `not_found`（不抛） |
| `www.baidu.com` | `fill(textbox <热搜词>, "冒烟测试")` + `press("Enter")` | 两个都 `ok`，页面 → `/s?…wd=冒烟测试` |
| `www.baidu.com`（结果页） | `click(link "百度首页")` | `ok` + `settle: ok`；**跳不跳不稳定**——4 次里 2 次 2s 内跳到首页（`'百度一下，你就知道'`），2 次没有（URL 不变 / 只多了 `rsv_jmp=fail`）。另做对照探针（`goto` 到结果页再点同一个链接）**3/3 都在 0.4s 内跳**，所以差异更可能与「页面是怎么到达的」有关（fill+press 提交 vs goto），**未定论**。冒烟把点击前/后的 URL 与标题每次都打出来，并显式标注「URL 未变」——**不拿它当成功率** |

> ★**这次冒烟抓到的第一手事实（进 R3/R4 的设计输入）：`role+name` 是页面级定位，
> 导航之后不失效——它会静默点中新页的同名元素。**
>
> 第一次跑的顺序是「fill+press → 直接点上一页枚举出的 `link "新闻"`」，结果返回
> `not_found`：搜索结果页上没有「新闻」这个链接。**返回 `not_found` 只是运气好**——
> 如果新页恰好也有同名链接，它会**点下去且记 `ok`**。对比 `ref`：ref 是会话句柄，
> 导航后必然失效报错（F4）。
>
> 所以纪律是：**任何一次导航（含 fill+press 提交）之后必须重新枚举，才允许下一次动作**。
> 这条不是「更稳一点」的建议，是 `role+name` 相对 `ref` **丢掉的那道保护**；
> 冒烟里的「页面已变 → 重新枚举后再点」就是它的示范实现。
> 同理，§4.5.6 那条「`{role,name}` 只在同一次枚举内有效」的动作侧版本就是这句。

> ★★**同一次冒烟的第二手事实，比上面那条更隐蔽：`click()` 返回时不保证导航已经开始。**
>
> 为了让冒烟把「点击后是什么样」读准，先加了「连续两次 `(url,title)` 读数一致 = 读稳」。
> 结果它报告「读稳了」，而**读到的是整个旧页**——旧页在导航开始之前**天然稳定**，
> 所以「稳」这个判据在这里恒真。0.5s 后页面才跳到首页。更早一版更糟：
> 读到的是**过渡态**（`title == ''`，页面上明明有标题）。
>
> 正确的判据只能是**「URL 相对动作前变了没有」**，且要分两段：
> ①等 URL 变化（导航开始），②再等 `(url,title)` 连续两次一致**且 title 非空**。
> 第二段里那个「title 非空」的条件是第三个版本才补的——**空标题本身也能连读两次
> 一致**，只要求一致仍会把过渡态交出去。它只是「新文档 commit 了」的廉价代理，
> **不是判据**：真有页面没有 `<title>`，所以预算耗尽照实返回空标题并由调用方打提示。
> 冒烟的 `_after_action_snapshot()` 就是这个两段式，读数与次数都打出来；
> 单测 `test_cli_action_snapshot.py` 把三种错法各钉一条。
>
> **这条对整条链的影响不止冒烟**：动作之后立刻 `interactive()` 会枚举到**旧页**，
> 于是下一步拿着旧页的名字去动作——与上一条（旧名字点中新页）是同一个坑的
> 两个入口：**一个在「名字过期」，一个在「读得太早」**。
> 避坑指南 §8.8 从症状侧记这两条。

### 4.6.1 动作超时：等的是**动作**，不是**页面加载**（2026-10-20 实测修正）

`ACTION_TIMEOUT_MS = 5000` 第一版**让一半的点击直接失败**——冒烟里连跑三次，
两次报：

```
camoufox 动作失败（click）: Locator.click: Timeout 5000ms exceeded.
  - locator resolved to <a href="/" class="toindex">百度首页</a>
  - attempting click action
    - waiting for element to be visible, enabled and stable
```

**看起来像页面故障，其实是我们自己在等一个没人需要的信号。** 现场取证（探针逐个量）：

| 量的是什么 | 读数 |
|---|---|
| `count` / `is_visible` / `is_enabled` | 1 / `True` / `True` |
| 包围盒 12 次采样（每 100ms） | **只变 1 次**（首帧之后恒定 `(1719.33, 24, 52, 24)`）→ 稳定 |
| 该点上 `elementFromPoint` | 就是这个 `<a>` → 没被遮挡 |
| `locator.click()`（默认等待，超时 30s） | **30s 用满仍失败**，日志停在 `performing click action` |
| `locator.click(no_wait_after=True)` | **1.1s**，页面确实跳到 `https://www.baidu.com/` |
| `goto("https://www.baidu.com/")` 同一地址 | **0.3s** |

也就是说：**动作本身 1.1s 完成，剩下 9–13s 是 playwright 在等「这次点击触发的
导航完成」**——而它等的那个「完成」比 `goto` 的 `load` 苛刻得多（多半挂在页面里
某个慢 iframe 上）。把两件事塞进同一个 5s 预算，症状就是「点击随机失败」。

**处置**：`click` / `press` 传 `no_wait_after=True`，动作返回后由 `_settle()`
等一次 `domcontentloaded`（上限 **2s**，常量 `NAV_SETTLE_TIMEOUT_MS`）。
理由三条：

1. **我们要的是「新页面能不能被观察」，不是「页面加载完没完」**——后者由站点
   决定，等它等于把预算交给对方；
2. **有界**：2s 等不到就返回，结果记进 `last_action["settle"]` = `ok`/`timeout`
   ——**等不到要说出来**，不能只有「动作成功」一个字段（那样下一步读到旧页就
   无法归因，见 §4.6 末段与避坑指南 §8.8）；
3. **等不到 dom ready 不抛**：点击确实发出去了，这是页面事实，不是动作失败。

> ⚠️ 这条**改的是动作语义**，不是调参数：obscura 面的 `click` 返回时页面是什么
> 状态，`browser_click` 没有明说；P5 双跑比较分支分布时，两边的「动作返回后
> 页面处于什么状态」**必须一起对**，否则差异会被读成「站点变了」。

**修完的读数**（同一冒烟连跑 4 次）：`4/4` 记 `{'outcome': 'ok', 'settle': 'ok'}`，
**0 次超时**（修前 4 次里 2–3 次超时）。但其中 **2/4 在 2s 内 URL 没变**——
对照探针（`goto` 到结果页再点同一链接，不经过 fill+press）**3/3 在 0.4s 内跳转**。
所以「点了但没跳」是**站点行为**且可能与「页面是怎么到达的」有关，**未定论**；
冒烟如实打印「URL 未变」，**不据此判成功率**——这正是 `last_action` 只回答
「调用成功没有」而不回答「点对没有」的原因（§4.6 末段）。

---

## 4.7 P3-β 落地（2026-10-20）：采集面剩余五条 + 两面读数并排

`links` / `extract` / `new_tab` / `close_tab` / `observe` 全部落地，**协议里的原语齐了**。
本节只记**量出来的、与设计不一样的地方**——照抄接口说明的部分在代码注释里。

### 4.7.1 标签页是**栈**，不是「一个 `page` 字段」

`steps/link_check` 的形态是「开 tab → 观察 → 关 tab → **回中间页继续采**」。
一个 `_page` 关完就无处可回，`CamoufoxSession` 因此持页面栈：

| 动作 | 行为 |
|---|---|
| `new_tab(url)` | 开页、压栈、切成当前页，返回**本驱动发的**句柄 `c1`/`c2`…（不是 obscura 的服务端 tab id，两面不可互换） |
| `close_tab()` | 关当前页并自动回上一页 |
| `close_tab(id)` | 认不出就**抛**；不猜一个关掉（关错页后 `observe` 会读到别的页，**不报错**） |
| 只剩一页时关 | **先补一张空白页再关**：`page` 必须永远可用，否则后续 `observe` 在读已关闭句柄，症状是「页面读不到」，真相是「我们自己关空了」 |
| 开页导航失败 | 把新页**收掉再抛**：留着它当前页就停在 `about:blank`，一次外链失败会连带把中间页的证据弄丢 |

真浏览器冒烟（2026-10-20，`example.com` / `example.org` / 一个不存在域名）四条全对：
`new_tab` → `pages=2` 且当前页是新的；`close_tab(c1)` → 回 `example.com`；
关最后一页 → 留下 `about:blank` 且**能继续 `goto`**；开坏页 → `DriverError`
且 `pages` 仍是 1、当前页仍是原页。

### 4.7.2 `links` 必须**按 href 去重**——否则 `links_total` 两面不同义

同一对搜索页实测（obscura 的 `browser_links` vs camoufox 的 `a[href]`）：

| 页面 | obscura | camoufox（不去重） | camoufox（去重后） |
|---|---|---|---|
| baidu 结果页 | `total=131 / distinct=131` | `168 / 133` | 133 |
| bing 结果页 | `total=33 / distinct=33` | `48 / 35` | 35 |
| baidu 首页（另一个页面） | 50 | — | 51 |

obscura 那边 `total == distinct` **恒成立**——它自己就按 href 去重。不去重时 camoufox 的
`links_total` 比它高 **28–37%**，而 `links_total` 要进 D-2 口径（`Observation.elements_total`
的注释说明了为什么这两个数必须能对上）：**两边不同义，并池比出来的分布就是假的**。
去重保留**第一次**出现的那条（与 `steps/search.result_titles` 的口径一致）。

> ⚠️ `links[].text` 两面**仍然不同义**：obscura 给的是「域名 + 路径」拼的面包屑，
> camoufox 给的是锚自身的文本。P5 双跑比 `candidate text` 分布时要单列这条；
> 判断点 ① 不受影响（它走 `extract`）。

### 4.7.3 `extract` 取文本必须带 `textContent` 兜底（实测：`innerText` 会给空串）

2026-10-20，bing 结果页，camoufox：`li.b_algo h2 a` 的 **10 个锚 `innerText` 全是空串**，
而 `textContent` 就是完整标题。只取 `innerText` 的症状是「抽到 10 个空标题」→
`result_titles` 判「长度不等或为空」→ 判断点 ① 退回 fail-closed：
**看着像选择器随改版失效，其实是取文本的路窄了**。

处置：文本一律 `innerText || textContent`（`links` 与 `extract` 同一口径）。
代价是 `textContent` 会把隐藏文本也取进来——对「取标题」这件事，带噪声的标题
远好过空串。**修完读数**：bing `raw 数组 10/10 → 配成 {url:title} 10 对、10 条标题含片名`。

### 4.7.4 两面 `Observation` 并排（P2 判据后半条的**测量结果**）

同一脚本（`probe_observe --driver {obscura,camoufox}`）、同一批 URL、同一套契约断言：

| 字段 | obscura | camoufox | 判读 |
|---|---|---|---|
| `url` / `page_title` | 一致 | 一致 | ✅ |
| `body_text`（example.com） | 917 | 917 | ✅ |
| `body_text`（baidu 首页） | 445 | 424 | ✅ −4.7% |
| `links` / `links_total`（baidu 首页） | 50 | 51 | ✅ 去重后同量级 |
| `video_tag_count` / `iframe_count` | 1 / 0 | 1 / 0 | ✅ |
| `degraded` | `[]` | `[]` | ✅ |
| ★`interactive_elements` / `elements_total`（baidu 首页） | **74** | **34** | ❌ **不同义，−54%** |
| `raw_len` | 1105 / 6220 | 917 / 432 | ❌ 不同义（obscura 的 raw 含快照正文） |

结论要分两半说：

- **字段齐、语义齐**：`observe` 该填的字段两边都填了，且 `degraded` 的**用途**
  （非空即 fail-closed）一致；契约断言两边都全绿。
- ★**`elements_total` 与 `raw_len` 不同义**，这是**已知且预期**的
  （`interactive` 的枚举规则本来就不一样：词表、无名丢弃、真同名丢弃）。
  但 `74 vs 34` **超出 §6.1 的 30% 门槛**，所以 P5 的「`interactive_elements` 条数
  中位差 ≤ 30%」这条**已经知道会红**——处置在 P4/P5：要么把 obscura 侧的枚举也收敛到
  `role+name` 词表（P3 的另一半活），要么在批次元数据里把两套口径显式分开、
  不并池比条数。**不许**把它当作「camoufox 采得少」的缺陷直接改枚举。

### 4.7.5 两条验收线各自到哪

| 判据 | 状态 |
|---|---|
| 「`steps/` 全部走通」 | ⏳ **未验**——离线接线已通，但真跑 `steps/` 要一次完整 `run`（要 LLM key + 批次），没做 |
| 「`search` 步骤的标题抽取与 obscura 逐条对齐」 | ✅ **已验**（`probe_result_titles --driver …`）：bing 两边都是 `10/10 对、10 条含片名`，baidu 两边都是 `8/8 对、8 条含片名`；两边 `@href` 都返回**包裹形态**的 `baidu.com/link?url=…`（与 `steps/search.title_for` 里那句「obscura 给的是解包后的 URL」**不一致**——那句是 2026-10-10 在另一种页面变体上测的，今天两边同形） |

---

## 4.8 P4 契约迁移落地（2026-10-20）：**键名是方言的投影**

### 4.8.1 一句话

字段名统一成槽位 `role` / `name`，**值域不统一**——obscura 面的 `role` 槽里装的
仍然是 HTML 标签名（`input` / `button`），camoufox 面装的是 ARIA role
（`textbox` / `button`）。落盘与提示词的**键名**由方言投影出来：obscura 面写
`{tag, label}`（与历史 361 份 P2 样本逐字节同形），camoufox 面写 `{role, name}`。

### 4.8.2 为什么不是「统一改名」

P4 的字面任务看起来是改名，但直接改会把两件事一起做坏：

1. **写错语义**：obscura 面的 `tag` 是 HTML 标签名。把它改叫 `role` 之后，一条
   `{"role": "a[role=pagedescription]"}` 会让读档人以为那是 ARIA role——
   **名字对了，意思是错的**，而且报告上完全看不出来。
2. **写坏存量**：盘上 361 份 P2 样本 + 75 份 P1 存档全是 `{tag, label}`。要么
   整批重写（重算 `required` 的分母跟着变，等于换了一批数据），要么键名按方言
   投影——**只有后者能在「老数据不动」的前提下把新方言引进来**。

所以 P4 的产出不是一次改名，是**一条轴 + 两处投影**：

```
driver（P1 记） → surface_of_driver → 面的方言 DIALECTS → {kind_key, text_key}
                                                    ↘ 元素值域（词表）也由方言决定
```

### 4.8.3 落地清单（代码）

| 位置 | 内容 |
|---|---|
| [`common/elements.py`](../../common/elements.py) | **新文件**：`ELEMENT_VOCAB_*` / `ELEMENT_KEYS` / `element_role` / `element_name` / `element_json` / `element_keys` / `vocabulary_of_archive_elements`。放 `common` 而不是 `perception.base`：`rationale`、`assembler`、T 都有「不 import 上游」的硬规矩，而**读一个存档键名是纯格式的事**（`test_writer.py::TestNoUpstreamImport` 直接把这条钉住） |
| `toolcatalog` | `Dialect(vocabulary, kind_key, text_key)` + `DIALECTS`（逐面）+ `dialect_of(surface)`（认不出抛）。方言**唯一来源**在这里 |
| `perception/base.py` | `InteractiveElement.ref/role/name`（槽位名）；`Observation.element_vocabulary`（**驱动实现写、回放按键名恢复**）。`element_*` 函数搬去 `common/elements.py`，本模块**不转发**——两个导入路径迟早各自漂 |
| `executor/actions.py` | `ActionTarget.role/name`，`to_json(vocabulary)`；`target_of` **只搬槽位、不翻译值**（`input` → `textbox` 要猜 `<input type>`，猜错就是声明与调用对不上） |
| `executor/orchestrator.py` | `_obs_json(obs)` **从观察自己取方言**（`obs.element_vocabulary`），不由调用方传；`_vocabulary_of_run(record)` 首选观察、无观察时才折 `driver` |
| `perception/rule_perceptor.py`、`executor/steps/site_search.py` | `SEARCH_BOX_WORDS` / `_SEARCHABLE_WORDS` 改成**逐面两套**，认不出的词汇表直接抛（P3-α 的并集是过渡手段，P4 收窄） |
| `assembler/observation_view.py`、`rationale/writer.py`、`d3_scorer.py`、`review_queue.py`、`replay.py`、`probe_llm.py` | 读档侧统一走 `element_role` / `element_name`；渲染侧统一走 `element_line`（`<tag> label` / `[role] name`）/ `element_json` |
| `tests/contract/test_element_dialect.py` | **新契约测试**（20 条）：一条轴的三张表逐面相等、投影逐面、认不出就抛、老存档仍是老键名 |

### 4.8.4 落地后**仍然没闭环**的（不要读成已完成）

| 项 | 事实 |
|---|---|
| **`required` 按新批次重算** | **做不了**：`output/pipeline/` 里没有任何 `driver=camoufox` 的 P2 样本（`probe_observe_camoufox.json` 是探针产物，不是 run 存档）。`Test参数口径与真实存档一致` 在 camoufox 面因此整体 skip（理由是「没有真实存档」）。**camoufox 第一批 P2 落盘后第一件事就是重算**——`test_element_dialect.py::Test存量口径` 会在那一刻变红并给出提示，避免「悄悄沿用声明值」 |
| 「`steps/` 全部走通」 | 仍未验（要一次完整 `run`，需 LLM key + 批次） |
| `elements_total` 74 vs 34 | 仍超 §6.1 的 30% 门槛；P4 只是把两套口径**分开**，没有让它们同义 |
| `_after_action_snapshot` | 仍只在 `cli`（不在 `steps/`），与 P3-β 时一致 |

### 4.8.5 一处被这次迁移顺手抓出来的旧错

`orchestrator._obs_json` 原先由调用方传 `vocabulary`。测试立刻抓到：`camoufox` 面
的观察配默认 `html_tag` 传下去，写出的键名是 `{tag, label}`——**值是对的、键是错的**，
而报告上看不出任何异常。改成**从观察自己取**（`obs.element_vocabulary`）之后这类
错在结构上不可能再发生：传错的机会只存在于「谁来定方言」，而那个答案只有一个。

---

## 4.9 P5 落地（2026-10-20）：**切换机制 + 双跑判据都已就位，但判据没过**

### 4.9.1 一句话

P5 的三块**机制**落地了：① 动作派发按面（执行路径第一次真的能跑 camoufox）；
② `run` / `run-plan` 有 `--driver`（双跑有了入口，默认仍是 obscura）；
③ §6.1 的判据变成可执行命令 `cli compare-drivers`。
**但第一轮真双跑判 `fail`**，按 §6.1「超出则不许切换」，**`DEFAULT_KIND` 保持
`obscura`**——这不是没做完，是证据说现在不能换。

### 4.9.2 代码落地清单

| 位置 | 内容 |
|---|---|
| [`executor/actions.py`](../../executor/actions.py) | **`target_params(vocabulary, target, ref=…)`**——动作参数的**唯一派发点**：`html_tag` 面给 `{ref}`、`aria_role` 面给 `{role,name}`；`aria_role` 面缺目标抛 `UnaddressableTarget`（**不**退回 ref：那一面不认），认不出的词汇表抛 `ValueError`（**不**回落） |
| `executor/steps/visit.py`、`steps/site_search.py` | 三处点击/填词改走 `target_params` | 
| `executor/agent_loop.py` | `_apply` 收 `target` + `vocabulary`，同样走 `target_params`——agent 的 wire format 仍是 `ref`（R3/R4 才迁移），但**驱动层收到的**是对面的参数 |
| `executor/cli.py` | `run` / `run-plan` 加 `--driver {obscura,camoufox}`（默认 = `DEFAULT_KIND`）；`_driver_for_run` 定生命周期：**obscura 整批一条连接复用（行为不变）**、**camoufox 一个 Run 一次 launch** |
| [`executor/dual_run.py`](../../executor/dual_run.py) | **新文件**：§6.1 的对账实现。分支占比（分母 = 出过分支的**站点数**）、正文长度中位数、`elements_total` 中位数、拦截率读数、负样本原文清单；`verdict` 只可能是 `pass`/`fail`/`incomplete`，**不合成加权分** |
| `cli compare-drivers` | `--a`（基线）/ `--b`（候选）/ `--json`；exit 0 过、1 不达标、**2 有判不了的**（「判不了」不等于「过了」） |

### 4.9.3 第一轮真双跑（2026-10-20，DR01《让子弹飞》，各 1 次）

```text
A obscura  1 份存档 · 28 个观察 · 面 obscura-1   成功 1 / 13 站
B camoufox 1 份存档 · 20 个观察 · 面 camoufox-1   成功 0 / 13 站
判定 fail
  分支分布          fail（6 支超 5pp：blocked / component_unverified /
                          login_wall_or_blocked / no_play_control /
                          unreachable_hard / unresolved）
  body_text 中位数  fail  814 → 398.5（−51%）
  elements 中位数   fail  105.5 → 31.5（−70%）
  拦截率            25.0% vs 23.1%（读数，§6.1 不构成否决）
```

产物：`output/pipeline/dualrun/{obscura,camoufox}/`、`dualrun/report.json`
（后者 gitignored）。**方言链在真实运行里成立**：camoufox 存档的元素键是
`{ref, role, name}`、obscura 是 `{ref, tag, label}`，`driver` 各自如实落盘。

### 4.9.4 第一轮暴露的两个问题（比差值本身重要）

1. ★**3 次点击全部 `Timeout 5000ms exceeded`**，obscura 同一批站点点得动。
   **步数记账里看得见**（`steps[i].error` 非空），但下游读到的是
   `component_unverified`——正是 §6.1 说的「理由通顺的假负样本」风险：
   **点击没做成被读成了页面事实**。

   **已定位（2026-10-20，六轮探针）：不是挂死，是慢过超时；触发条件是
   headful 窗口不可渲染（最小化/息屏/锁屏/被完全遮挡）。**

   | 读数 | 窗口可见 | 窗口最小化 |
   |---|---|---|
   | 同一次 `locator.click` | 0.4–1.5s | **5–13s** |
   | `humanize=False` | 82ms | **9.1s** |
   | `humanize=0.3`（maxTime） | 386ms | **9.1s** |
   | `headless=True` | 34–71ms | 不适用（从不慢） |
   | 页内 `requestAnimationFrame` 滞后 | 4–17ms | 902ms →**62s**（一帧没画） |

   四条把原先的猜测逐个排掉：

   - **事件其实落地了**：超时那次点击之后页内 `pointerdown`/`click` 计数**涨了**
     → 不是「点不动」，是「确认回来之前超时」。这也意味着**超时的点击不能重试**
     （重试 = 点两次）；
   - **不是「新开的 tab」**：headful 下**会话首页同样超时**；当初「首页 0.74s
     点得动」只是那次点击恰好落在窗口还可见的时候。`headless=True` 从不慢；
   - **不是 `humanize`**：它只把 9.1s 放大到 13.3s，关掉治不了；
   - **不是我们这层**：裸 `AsyncCamoufox` 直接复现；
     `widget.windows.window_occlusion_tracking.enabled=False` 与
     `dom.min_background_timeout_value` / `background_throttling_max_budget`
     **都治不住**（最小化仍 5.1–9.4s）。

   → **它是环境×依赖的交互，不是 camoufox 的功能缺陷**。所以在它解决之前，
   「一个 Run 里开 tab 遍历多站」这条主路径**在无人值守的 headful 批次里不可用**，
   而 `headless=True` 能跑通但换掉了指纹口径（§4.3 有意选 headful）。
   这是个**要记账的选择**，见避坑指南 §8.13。证据在
   [`research/probes/probe_click_occluded.py`](../../research/probes/probe_click_occluded.py)
   等六个探针与同目录 `.json`。
2. **`elements_total` 差 70%**：P3-β 记过的 74 vs 34 在真批次里是 105.5 vs 31.5。
   机制清楚（ARIA 枚举 vs DOM 枚举），但**下游判断点 ① 吃的是这份列表**——
   列表短一半，判断的依据就少一半。§6.1 说「超出则回 P2/P3 查」，查的是
   **这条因果**，不是把阈值调宽。

### 4.9.5 因此 P5 **不算完成**

`DEFAULT_KIND` 仍是 `obscura`。P5 的完成条件是 §6.1 的六条判据：四条机械
（已可跑）、两条人判（负样本成因 / 动作可移植性）。现在四条里三条 fail。
点击超时的**根因已定位**（§4.9.4），所以剩下的不是「查」，是「选」。

**「选」的结果（2026-10-21，owner 拍板）：走 A——无人值守批次跑 `headless=True`。**

| | A：headless | B：headful + 超时提到 15s |
|---|---|---|
| 单次点击 | **34–71ms，从不慢** | 可见 62–1052ms；**不可渲染时 5–13s** |
| 依赖桌面状态 | **否** | **是**（不锁屏 / 不息屏 / 不最小化 / 不遮挡） |
| 指纹口径 | 变（`headless` 进批次元数据） | 不变 |
| 无人值守可行性 | **成立** | **不保证**（跑一夜 / 合盖 / 远程桌面断开都会触发） |
| 未测风险 | **拦截率未实测**（§6.1 的否决位） | 无 |

**为什么是 A**：B 的前提在真无人值守场景里**保证不了**——「机器不能锁屏 /
息屏 / 最小化 / 遮挡」正是无人值守的定义；而 A 差的是两个数量级（34–71ms
vs 5–13s），不是「慢一点」。口径改动也**有地方装**：`headless` 是
`fingerprint` 的一个取值（§4.3），批次元数据本来就记 fingerprint 实测值，
所以它是**既有维度的新取值，不是新分叉轴**。

**A 的前置门（必须先量）**：headless 下的**反爬拦截率**。§6.1 写明
「显著变高」是否决项，而这是 headless 唯一没测过的维度。
**先跑一次 headless 双跑看拦截率读数**——不显著变高就落 A。

#### 4.9.5.1 口径反转带来的三处代码改动（2026-10-21 已落地）

| 改动 | 位置 | 内容 |
|---|---|---|
| **默认无头** | [`camoufox_session.py`](../../executor/browser/camoufox_session.py) | `launch(headless: bool = True)`。**这是对 §4.3「有意选 headful」的反转**，理由见上。有头保留为显式 `launch(headless=False)`，给「人守着的桌面」 |
| **`headful` 开关** | [`executor/cli.py`](../../executor/cli.py) | `run` / `check` 加 `--headful`。**缺省无头**——两种口径都记进批次元数据 |
| **动作预算 5s → 15s** | [`camoufox_driver.py`](../../executor/browser/camoufox_driver.py) | `ACTION_TIMEOUT_MS = 15000`。盖住实测上界 **13323ms**（`research/probes/click_occluded.json`）。**无头口径下 34–71ms 永远碰不到上限**，所以放宽只影响有头那条路 |

**顺带落地的两件（与口径无关，A/B 都要做）**：

1. **超时后不重试**（已在驱动层与 orchestrator 层各有一道：
   `orchestrator` 的「单次 tool 失败不重跑」）。理由是探针实测
   **超时那次点击的事件照样落地**（页内 `pointerdown` 计数上涨）——
   重试 = 点两次。已加测试钉住。
2. **`steps[]` 记单步耗时**（`duration_ms`）。理由：超时的点击与
   「页面没有播放器」在证据上**同形**（都是一条 `error` 非空的步骤），
   没有耗时字段就分不出 5s 与 13s，事后只能靠猜。口径是**整步**
   （动作 + 随后的观察），失败路径上 ≈ 动作耗时。

#### 4.9.5.2 口径定了**不等于** P5 完成

判据仍然没过，且**下一批的第一件事不是重跑双跑**——是先把
`elements_total` 的**口径**统一（见 §4.9.6）。

### 4.9.6 ★ 两面 `elements_total` **不同义**——−70% 现在不是一个可解释的量

这一条是 2026-10-21 梳理时挖出来的，**它排在重跑双跑之前**。

§6.1 的判据②③是「`body_text` 中位数差 ≤30%」「`interactive_elements`
中位数差 ≤30%」。但 §4.7.4 早就记了：两面的 `elements_total` **不同义**
（obscura = DOM 元素数，camoufox = ARIA 枚举 kept 数）。于是：

```
obscura 105.5  vs  camoufox 31.5   →  −70%
```

这个差值里有**两层不可比**：

1. **枚举基础不同**——DOM 全量 vs ARIA 可访问性树；
2. **过滤规则不同**——camoufox 侧词表外 role / 无可访问名 / 真同名整组丢
   （§4.5.6 记的三处已知偏离）。

所以「查因果（判断点 ① 的输入短一半 → 分支怎么变）」这句话，
**在口径统一之前是查不通的**——查的是一个还没被定义成同一个东西的差值。

**要做的是「让两面可比」，不是「调阈值」**：

- 枚举基础对齐（obscura 面也出 ARIA 可访问名，或 camoufox 面也报 DOM 总数）；
- 过滤规则对齐（可见性 / 词表 / 无名三档）。

> ★ **这跟方案 15 的 R3 是同一件事**：R3 要「给 obscura 面加采集面」
> （把 obscura 的 `interactive` 输出也换成 `{role, name}`）。
> **H-08 与 M-21 是同一件事的两半**——台账把它们分在两个域文档里，
> 会被当成两件事排期。做完它，双跑的判据②③才有意义。

**下一批的顺序**（依赖顺序，不是优先级堆砌）：

1. **统一 `elements_total` 口径**（本节）——与 A/B 无关，现在做不会白做；
2. 顺手补两条便宜的尾巴：P0 的「存档逐字节同形」（现在 `OBSCURA_EXE` 已配，验得了）；
   契约测试的 glob 改成递归（`dualrun/` 下的存档现在看不见）；
3. **headless 拦截率实测**（A 的前置门）；
4. **重跑双跑**：≥3 task、同 `--max-candidates`、含 headless 拦截率读数；
5. **人判两条**（负样本成因 / 动作可移植性）——`compare-drivers` 只能报
   `needs_manual`，这两条是机器判不了的；
6. 四条机械判据 + 两条人判都过，再改 `DEFAULT_KIND` 那**一行**；
7. 之后才是 P6 批次元数据落盘（§4.3 那九项现在只在 driver 层，
   **还没进 P1 存档**）与方案 15 的 R3–R5。

---

## 5. 红线：两份，不是一份

obscura 侧用 `FORBIDDEN_TOOLS` + `_assert_tool_allowed` 堵死 5 个 cookie/storage tool
（[obscura_driver.py:87](../executor/browser/obscura_driver.py#L87)），**所有 tool 调用统一走 `_call` 是唯一入口**。

camoufox 侧有**等价的一组**，必须同样堵死，且同样走**唯一入口**：

| 禁项 | camoufox/playwright 侧对应 | 处置 |
|---|---|---|
| cookie 读取 | `context.cookies()` / `context.storage_state()` | **不包装** |
| cookie 写入 | `context.add_cookies()` / `context.clear_cookies()` | **不包装** |
| 浏览器 Profile | `persistent_context=True` / `user_data_dir` | **不包装**（launch 时就不开） |

**纪律要点**：这些不是「暂时没需求」，是**永不提供**。
一旦暴露，将来某次「需要登录态」的临时需求就会把 profile 写进 P1 存档，而存档是要推给 Label Studio 的。
正确做法是判 negative 并记 `login_wall_or_blocked`——与失败分支表一致。

**另两条 camoufox 特有的**：
- `main_world_eval=True` → `evaluate` 具备读页面 JS 全局状态的能力（F2）。
  这不碰隐私红线，但**扩大了采集面**。建议**默认关**，仅在确需读播放器状态时按需开，
  且**开了必须记进批次元数据**（否则「这批为什么读到了播放器状态」无法解释）。
- `humanize` → 鼠标轨迹带随机延迟。**它会让每步耗时变长**（默认最长 1.5s/次），
  批量采集的墙钟时间会明显上升，**且必须重估超时预算**（`mcp_client` 的 60s 是 obscura 的口径）。

### 5.1 ★ 训练侧的红线要改口径（D12 带来的）

[toolcatalog/__init__.py:3](../toolcatalog/__init__.py#L3) 的模块 docstring 写着
「**不提实现名**（obscura / MCP / 句柄 / isolate）——模型学到的是
『遇到这样的页面该怎么办』，不是『怎么驱动某个浏览器』」。

**这条红线是为 obscura 写的，D12 之后它必须改口径**：

| | 原口径 | D12 之后 | 理由 |
|---|---|---|---|
| 工具**形状** | obscura 的语义形态（`click(tag,label)`） | **camoufox 的调用面**（`click(role,name)`） | 执行层就是 camoufox，形状必须能原样执行 |
| description 里**点名** | ❌ 禁实现名 | ✅ **点名 camoufox / `get_by_role`** | 不点名，模型学不会「匹配到多个元素时怎么办」——而那是执行期最常见的失败 |
| description 里**禁** | 实现名 | **仍禁**：obscura / MCP / `ref` 句柄 / Cookie / Profile / Authorization | 这三条与浏览器无关，**永久禁** |

**改的不是「能不能提浏览器」，是「提哪个浏览器」**：
禁的是**已经不是执行层**的那个（obscura），允许的是**正是执行层**的那个（camoufox）。

> **顺带一条自省**：原来的「不提实现名」出发点是对的（别教模型驱动它见不到的浏览器），
> 但它顺带把**执行层是什么**这个信息也藏了——而执行层是什么，
> 恰恰决定了动作词汇表该长什么样。**藏错了地方。**
> 正确的说法是「只说**当前执行层**的调用面，不说历史实现，也不说红线能力」。

**契约测试要跟着改**：[tests/contract/test_tool_catalog_contract.py](../tests/contract/test_tool_catalog_contract.py)
的禁用词表目前盯着 obscura/MCP/句柄。改完之后它**要新增一条正向断言**：
**声明里必须出现 camoufox 或 `get_by_role`**——
否则哪天有人「顺手」把措辞改回抽象说法，测试不会红，而数据会静默地退回到
「脱离本项目不成立」的状态。

---

## 6. 落地阶段

> **P-1 是 D12 的生死门**，不能往后排。F5（§4.5.3）验不过，
> `role+name` 这套编码就立不住，后面五个阶段全白做。

| 阶段 | 内容 | 完成判据 |
|---|---|---|
| **P-1** | **验 F5**：探针里对比「采集层算的 name」与「playwright 算的 name」，在同一批真实页面上量偏差。**两段都跑完（§1.5 离线 / §1.6 真实页面）** | **能定位**（`get_by_role(role, name=…, exact=True)` 在**唯一名元素**上命中率 ≥ 95%）：**已达标**——权威 100%、JS+伪元素 96.7%、纯 JS 87.9%。**D12 继续；P0 之前把采集面 name 来源改成 `aria_snapshot`**（§4.5.3）。分母纪律：真同名（6.3%）与无名字元素（41%）**不进判据分母**，但要单列 |
| **P0** | 抽 `driver_factory.py`，6 处硬编码收敛进去。**不改行为**。**已落地（2026-10-20，§2.3）** | 门禁全绿 ✅（`3065 passed`，唯一红是既有 xlsx 环境项）；AST 门禁盯第 7 处硬编码 ✅；`run` 的存档与 P0 前逐字节同形 ⏳ **本机无法验**（缺 `OBSCURA_EXE`），换到有 obscura 的机器上补跑 |
| **P1** | `camoufox_session.py` + `camoufox_driver.py` 骨架（`goto`/`snapshot`/`count`/`evaluate`/`close`）。**已落地（2026-10-20）**——会话层钉死 `os`/`screen`/`window`/`humanize`/`geoip=off`/`main_world_eval=off`/排除 uBO，并回读实测指纹；骨架其余原语**显式抛 `NotImplementedError`（不返回空值）** | ✅ `check --driver camoufox` 起真实浏览器并打印指纹读数（**8 次实测**见 §4.3.1：`screen`/`dpr` 稳定，**视口高 ±8px 抖动**）；默认实现仍是 obscura，生产链零变化；门禁 `3152 passed`（唯一红是既有 xlsx 环境项） |
| **P2** | **动作面按 camoufox 形态**：`click(role,name)` / `fill(role,name,text)` / `press(key)` + **协议方法名一并改**（`type_text`→`fill`、`press_key`→`press`，见 §2.2）+ **三态错误映射**（§4.2）+ **strict mode 处理**（§4.5.4）。**已落地（2026-10-20，§4.6）** | ✅ **动作的 camoufox 调用可原样执行**——真浏览器冒烟三个原语全 `ok`（`example.com` 点击后跳转、`baidu` fill+press 提交搜索）且 `not_found` 不抛；单元测试封住三态映射与跨面拒绝。**后半条已测（P3-β 收口，§4.7.4）：字段齐、契约断言全绿，但三处口径不同义**（`elements_total` 74 vs 34、`raw_len` 含不含快照、`links[].text` 面包屑 vs 锚文本）——所以判据**判"已测量"而非"同口径"**，差异留给 P4/P5 重标定 |
| **P3-α** | 采集面第一块：`interactive`（**一页一次 `page.aria_snapshot()`**，role+name 取 playwright 结论）+ 纯解析器 `browser/aria_snapshot.py` + 下游两处词表放宽。**已落地（2026-10-20，§4.5.6）** | ✅ `check --driver camoufox --probe-url …` 打出元素表与过滤账目（`example.com` kept=1 / `baidu` nodes=101→kept=34）；`aria_snapshot` 解析 10 条格式坑用例全绿；**三处偏离已记账**（可见性未逐元素过滤 / `tag` 装 role / 无名与真同名整组丢） |
| **P3-β** | 采集面剩余：`links`/`extract`（复刻 `[]`/`@attr`）+ `new_tab`/`close_tab` + `observe`。**已落地（2026-10-20，§4.7）**——标签页做成**栈**（关当前页回上一页、只剩一页先补空白页、开页失败收掉新页）；`links` **按 href 去重**（obscura 恒 `total==distinct`，不去重则 `links_total` 高 28–37%）；`extract` 取文本 `innerText \|\| textContent`（实测 bing 结果页 10 个锚 `innerText` 全空） | ✅ **`search` 步骤的标题抽取与 obscura 逐条对齐**（`probe_result_titles --driver …`：bing `10/10` 对、baidu `8/8` 对，两边同形）；✅ 真浏览器上四条标签页语义全对；✅ 两面 `observe` 用同一脚本同一套契约断言，**字段全绿**。⏳ **「`steps/` 全部走通」未验**（要一次完整 `run`，需 key + 批次）；⚠️ **`elements_total` 两面不同义**（baidu 首页 `74 vs 34`，−54%，**已超 §6.1 的 30% 门槛**，处置在 P4/P5，见 §4.7.4） |
| **P4** | ★**契约迁移**：`actions.py` / `perception/`（`role`/`name` 词表）/ `toolcatalog/` / 契约测试禁用词表。**已落地（2026-10-20，§4.8）**——字段名统一为槽位 `{role, name}`、键名**按方言投影**（obscura 面仍写 `{tag, label}`，老存档逐字节不变），词表收窄成逐面两套且**认不出就抛** | ✅ 新旧样本**标记为不同口径**：存档键名 / 提示词写法 / `tool_surface` 三处都分得开，老存档一条没动（`test_element_dialect.py` 逐条扫盘盯住）；❌ **`required` 无法按新批次重算**——camoufox 面**一条 P2 样本都没落盘**，没有可统计的分母（同一原因让 `Test参数口径与真实存档一致` 在 camoufox 面**整体 skip**）。这条不是没做，是**做不了**，见 §4.8.4 |
| **P5** | 切换生产链：CLI 默认走 camoufox，obscura 降为 `--driver obscura`。**机制已落地（2026-10-20，§4.9）**——动作派发按面（`actions.target_params`）+ `run --driver`（每 Run 一次 launch）+ `cli compare-drivers`；**但切换未做：第一轮真双跑判 fail**。★**2026-10-21 口径已定**：无人值守走 `headless=True`（§4.9.5），配套 `ACTION_TIMEOUT_MS` 5s→**15s**、超时不重试、`steps[]` 记 `duration_ms` | ❌ **判据未过**：分支分布 6 支超 5pp、`body_text` 中位数 −51%、`elements_total` 中位数 −70%（−30% 门槛）。★**判据②③的差值本身还不可比**——两面 `elements_total` **不同义**（DOM 全量 vs ARIA kept），**要先统一口径**（§4.9.6），再谈因果。点击超时的根因与口径都已解决（§4.9.4 / §4.9.5）。`DEFAULT_KIND` 仍 `obscura` |
| **P6** | 批次元数据（`driver` + `fingerprint` + **动作编码版本**）+ 依赖说明修订 + D7→D11→D12 改写。⚠️ **2026-10-21：口径反转让这一项提前了**——`headless` 已在 `fingerprint` 里（第九项），但**整份 fingerprint 还只在 driver 层，没进 P1 存档** | 存档里能查到 fingerprint 字段与编码版本 |

> **P4 为什么不能省**：契约一改，`toolcatalog` 的 `required` 口径（按盘上样本出现率统计）
> 与 `test_tool_catalog_contract.py::Test参数口径与真实存档一致` 会立刻对不上。
> **要么同批改完，要么先关掉那条断言**——不能让它红着过。

### 6.1 P5 的验收判据（必须双跑）

同一批任务，obscura 与 camoufox 各跑一遍，**逐条对比**：

| 判据 | 要求 |
|---|---|
| 分支分布 | 分支集合一致，**比例差 ≤ 5 个百分点**；超出则**不许切换**，回 P2/P3 查 |
| `body_text` 长度 | 中位数差 ≤ 30% |
| `interactive_elements` 条数 | 中位数差 ≤ 30% |
| 负样本成因 | **逐条人工读**——最贵的静默是「理由通顺的假负样本」 |
| 采集体感 | 目标站点的反爬拦截率 camoufox **低于** obscura（**这是待验证的附带收益，不达标不构成否决**——见下） |
| ★**动作可移植性** | 从 P1 的 `steps[]` 里**逐条取出动作，按 camoufox 原始调用重放**，能在**脱离本项目**的裸 camoufox 环境里跑通——**这是 D12 的验收线，不过就不算成功** |

> **关于「采集体感」这一行的判据口径**：D11 初稿写的是「明显低于，**这是本次替换的主要目的，不达标就不换**」。
> **这条改口径了**：替换的主要理由是**可控 + 原生值**（§0.2），
> 反检测更强是**附带收益**。若拦截率没改善，替换**仍然成立**——
> 因为拿到的是 30+ 个可调参数与原生值，而代价（重写 13 原语 + 换掉全部解析器）是**一次性的**。
> 但若拦截率**显著变高**，那就是**否决项**。
>
> **别用错的指标下结论**：把「反检测」当主要目的，会在拦截率没改善时误判成「替换失败」，
> 而实际上替换带来的其它收益（可控性、原生值、动作可移植）都没被计入。

> 这条对标 CLAUDE.md「**每次真实运行后都要逐条打开存档读 evidence 核对**」——
> 设计缺陷离线测试测不出来（fixture 是自己造的，规则和 fixture 一起错，测试照样绿）。

---

## 7. 风险登记

| 风险 | 概率 | 后果 | 缓解 |
|---|---|---|---|
| `interactive` 枚举规则与 obscura 不同 → `elements_total` 不同义 | 高 | D-2 打分口径漂移 | P2 重新标定；契约测试同口径断言 |
| `extract` 漏复刻 `[]`/`@attr` | 中 | 标题抽取静默失效 → 假负样本 | 契约测试覆盖两种后缀；**症状同形，必须单独测** |
| viewport 漂移（F1） | 高 | 分布不可归因 | 显式钉 `screen`/`window`；批次元数据 |
| `evaluate` isolated world（F2） | 中 | 读不到播放器状态 → 判断点 ① 失效 | 默认关 `main_world_eval`；确需则开并记录 |
| 元素不存在 / 页面卡住同型（F4） | 中 | 基础设施故障被写成业务负样本 | `count()` 预判（§4.2） |
| obscura 独有的采集能力 camoufox 没有 | 中 | 某些站采不到 | `probe` 逐站对比；缺失则记 `unresolved` 不猜 |
| **uBO 默认装** → 拦掉播放器请求 | 中 | **假负样本**（播放器起不来被判「无播放控件」） | `exclude_addons=[DefaultAddons.UBO]`；P4 对比时专门看这条 |
| `geoip=True` 缺 `maxminddb` → 启动即抛 | 高 | 直接起不来 | 默认**关**；要开先 `uv pip install "camoufox[geoip]"` |
| `humanize` 拉长墙钟 → 超时预算失配 | 中 | 批量超时 | **已量化（§4.9.4）**：窗口可见时 0.4–1.5s，`humanize=False` 0.08–0.12s；**但它不是主因**，放大的是「窗口不可渲染」（9.1s→13.3s） |
| ★**headful 窗口不可渲染**（最小化/息屏/锁屏/遮挡）→ 单次动作 5–13s → 5s 超时 | **高**（无人值守批次几乎必然） | 整批点击失败，且**失败被读成 `component_unverified`**（理由通顺的假负样本） | ✅ **已处置（2026-10-21）**：无人值守批次默认走 `headless=True`（34–71ms，从不慢）；`ACTION_TIMEOUT_MS` 提到 **15s**（盖住上界 13323ms）；**超时的点击禁止重试**（事件可能已落地，已加测试）；`steps[]` 记单步耗时 `duration_ms`。⚠️ 代价是指纹口径变了 → `headless` 进批次元数据 |
| 双实现长期并存导致口径漂移 | 中 | 数据不可比 | obscura **降为参考实现，不进生产链**；混池禁止（§8） |
| ★**F5：采集层算的 name ≠ playwright 算的 name** | **高** | **动作点了没反应**——且与「元素不存在」同形，**不报错** | **P-1 前置验**（§6）；三条候选路径（§4.5.3）+ strict mode 兜底 |
| ★**契约迁移漏一处** | 中 | 新旧样本混池，`required` 口径失真 | P4 一次性改齐；`toolcatalog` 是唯一真源，**不许手抄第二份** |
| ★**动作可移植性没验** | 中 | 数据白采——训出来的 agent 在别处跑不了 | P5 验收线：脱离本项目重放（§6.1） |
| role 词表膨胀 | 中 | 枚举变慢、边界情况变多 | 词表**必须短**（§4.5.2），表一长就没人维护 |

---

## 8. 与「禁止投票/集成」的关系

CLAUDE.md 写着「禁止投票/集成（生产链上）」。

- **禁止**：两个实现同时跑同一站，然后取交集 / 投票 / 择优 → 明令禁止。
- **允许**：显式选路（某个站走 camoufox，另一个走 obscura）→ 那是**路由**不是投票。
- **但**：**禁止把两个实现的样本混进同一个池做分布统计**。
  `interactive` 的元素集定义不同 → `elements_total` 不同义 → 混池算出的分布是假的。
  选路必须把**实现名落进批次元数据**。

替换完成后这条自动消解（只有一个实现）——**这是「替换」相对「并存」的一个额外好处**。

---

## 9. 待办（实施时先做这些）

**已做（2026-10-20）**：

- [x] 改 [依赖说明/camoufox.md §4.3](../依赖说明/camoufox.md) 的 **`launch_options` 读法**——
      实测 `cfg["navigator.userAgent"]` **取不到**（返回 `None`）：指纹不在
      `firefox_user_prefs`、也不在返回顶层，而是编码进 `env["CAMOU_CONFIG_1..6"]`
      （分片 JSON）。正确读法是把分片拼起来还原 config。
      **这条错误读法会让「离线核对 stealth 配置」直接失效。**
- [x] 补 [tools.md](../tools.md) 的 camoufox 调用面（Playwright 原语 + `mw:` 前缀 + `main_world_eval`）
- [x] 决定 `main_world_eval` 的默认值与开关口径 → **默认关**，确需才开且记批次元数据
- [x] 决定 `humanize` 的默认时长与超时预算 → `humanize=True`（默认最长 1.5s/次），超时预算待 P4 重估
- [x] 改 [01-架构总览](../01-架构总览.md) 执行层章节（D7 → D11），
      删掉「放弃 Playwright MCP + Camoufox」那句；补记两个执行层并存的过渡期实况
- [x] **D12 定案**：动作词汇表 = camoufox 的调用面；目标编码 = `role + name`；
      训练侧工具声明**点名 camoufox**（§0.1 / §2.2 / §4.5 / §5.1）
- [x] 改 [tools.md](../tools.md)：补「动作词汇表（camoufox 形态）」与「目标编码」两节
- [x] 改 [tools.md](../tools.md) **camoufox · 原语 5**：删掉「推荐 ElementHandle 缓存」
      的动作面结论（原语 5 的 `click`/`type_text` 签名改成 `click(role,name)` / `fill(role,name,text)`）；
      在 obscura 侧的「原语 5 · 交互」加一行「本节是现役口径」的指向
- [x] 改 [tools.md](../tools.md) **训练侧工具声明节**：加「★ D12：目标编码从
      `{tag,label}` 改成 `{role,name}`」对照表；红线改写规则里点明
      「不提实现名」要改口径（禁 obscura、允许 camoufox），并写明
      `required` 统计**必须按新批次重算**
- [x] 改 [01-架构总览](../01-架构总览.md)：`page_driver.py` 行补 D12 分面说明、
      「换执行层不动上层」改成「**改动只在接线层，不在控制流**」、
      执行层章节补 D12 段、writer 边界处补「感知层不点名浏览器 vs 训练侧点名 camoufox」
      的**不对称**说明
- [x] 改 [10-TODO台账](../计划进展/10-TODO台账.md) H-08：阶段号从「P0–P4」改成
      「P-1 一个没动」，并写明 **P-1 是 D12 的生死门**

**待做**：

- [x] ★**P-1 验 F5 · 离线段**（§1.5 / §4.5.3）——探针 `executor/browser/probe_f5_name.py`
      已建并跑通：role 侧零落差、朴素链 65% 且产出空 name（执行期 `count == 0` 不报错）、
      加料 5 条规则与 playwright 等价、快照开销下界 ≈4×。
- [x] ★**P-1 验 F5 · 真实页面段**（§1.6，2026-10-20）：6 个真实 URL / 240 行。
      **达标，推荐路径 1**——playwright 自己的 name 在唯一名元素上 **91/91 = 100%**；
      JS 近似 **87.9%**（加料只多救 0–1 条）；JS 再补 `::before/::after` 的 `content` 才到 **96.7%**。
      同时坐实四条实现约束：**41% 元素没有可访问名**（其中 90 个不可见 → 枚举必须先过可见性）、
      **真同名 6.3%**（strict mode fail-closed）、**每页快照 57–165ms**、**`exact=True` 必须钉死**
      （默认 substring 口径下命中率 100%，判据变空）
- [x] ★**采集面改口径**（P3-α，2026-10-20，§4.5.6）：`interactive` 枚举后
      **name 全部取 `aria_snapshot` 的结论**，采集层不重算 ✅；
      无名字 / 真同名**整组丢弃并计数**（`last_enumeration`）✅。
      ⚠️ **「枚举前先过可见性」没有做**（5.8ms×N，见 §4.5.6 偏离 1）——
      `aria_snapshot` 本身不含 `display:none`/`aria-hidden`，残余的靠「无名字即丢」兜住；
      P5 双跑若 `interactive_elements` 条数中位差 > 30% 先补这一项
- [x] **执行期定位钉 `exact=True`**（P2，2026-10-20，§4.6）：定位器唯一构造点
      在 `camoufox_driver._locate`，`get_by_role(role, name=name, exact=True)`
      写死并带注释；跨面参数（`ref`/`selector`）在同一处抛 `ValueError`。
      单测 `TestActionFace::test_定位必须带_exact` 封住
- [x] ★**P2 的设计输入：`{role,name}` 只在同一次枚举内有效**（§4.5.6 末段）。
      `textbox` 的可访问名可能是**动态内容**（百度搜索框的名字是当时的热搜占位词），
      故 `count() → 操作` 的预判必须按**当次枚举**的名字现算；
      不许拿存档里上一次的名字去定位（失配时表现与「元素不存在」同形）。
      **动作侧已确认并升级为更硬的纪律（§4.6 末段）**：`role+name` 是**页面级**定位，
      导航后**不会失效**——它会静默点中新页的同名元素（对比 `ref` 必然报错）。
      故**任何一次导航之后必须重新枚举**，冒烟 `--action-smoke` 里是这个实现
- [x] **P3-β**（2026-10-20，§4.7）：`links` / `extract`（复刻 obscura 的 `[]`/`@attr` 口径）/
      `new_tab` / `close_tab` / `observe` 全部落地，`steps/` 的接线层面通了。
      **三条被实测改写的口径**：① 标签页做成**栈**（一个 `page` 字段关完无处可回）；
      ② `links` **按 href 去重**（obscura 恒 `total==distinct`，不去重 `links_total` 高 28–37%）；
      ③ `extract` 取文本 `innerText || textContent`（bing 结果页 10 个锚 `innerText` 全空）。
      ⏳ 仍未验：**一次完整 `run` 走通 `steps/`**（要 key + 批次）；
      ⚠️ 新记账：`elements_total` 两面 `74 vs 34`（−54%），**已超 §6.1 的 30% 门槛**。
- [ ] ★**契约迁移**（§4.5.5，P4）：`actions.py` 的 `ActionTarget`、
      `perception/base.py` 的 `InteractiveElement`、`rule_perceptor.SEARCH_BOX_TAGS`、
      `llm_perceptor` 的提示词与 payload、`toolcatalog` 的 `click`/`fill`、
      `test_tool_catalog_contract` 的禁用词表（**改口径 + 加正向断言**）
- [x] ★**协议方法名改**（§2.2，P2，2026-10-20）：`PageDriver.type_text` → `fill`、
      `PageDriver.press_key` → `press`；`obscura_driver` / `camoufox_driver` /
      `agent_loop._apply` / `steps/site_search` / 全部假 driver 一并改。
      `agent_loop` 里那张「`fill` → `type_text`」的翻译表**已删除**（语义层与驱动层同名）。
      **改名的理由是 `type_text` 在撒谎**（暗示追加，实际是设值）
- [ ] **`toolcatalog` 的 `required` 口径重算**（按 camoufox 新批次）——
      否则 `Test参数口径与真实存档一致` 会红。**新旧样本标记为不同口径，不混池**
- [x] `driver_factory.py`（P0，2026-10-20，§2.3）：6 处硬编码收敛到 `make_driver`；
      未知实现名报错不回落；`FakePageDriver` 不收进 factory；
      新增 AST 门禁 `tests/unit/test_driver_factory.py`
- [ ] **P0 的存档同形复核**：到配了 `OBSCURA_EXE` 的机器上跑一次 `run`，
      与 P0 前的存档比哈希（**注意：本工作区后来配上了 `OBSCURA_EXE`**，
      `check` 已能连上 obscura 0.2.4——补跑现在可行，但 P0 前的基线存档得另找）
- [x] **P1 会话层与骨架**（2026-10-20）：`executor/browser/camoufox_session.py`
      （一个 Run 一次 launch、钉死身份与几何、回读实测指纹）+
      `executor/browser/camoufox_driver.py`（`goto`/`snapshot`/`count`/`evaluate`/`close`；
      其余原语抛 `NotImplementedError`）。`sample` 三级降级链（innerText → textContent → HTML）
      与 `body_source` 标记沿用 obscura 口径。单测 `tests/unit/test_camoufox_driver.py`
      （假 page，不联网）+ `test_driver_factory.py` 扩到盯 `CamoufoxDriver` 的第 2 处硬编码。
- [x] ★**v2 主依赖声明 camoufox**（2026-10-20）：`pyproject.toml` 主依赖加
      `camoufox==0.5.8` + `playwright==1.62.0`（**钉死版本，不用范围**——指纹由
      fpgen 生成，版本一动指纹就动，批次可归因性建立在「指纹只随配置变」上）。
      不用 legacy extra 里的 `cloverlabs-camoufox==0.6.0`（**v1 fork，包名不同**）。
      `uv lock` 增量 4 个包（camoufox / fpgen / indexed-zstd / zstandard），纯新增。
      ⚠️ **连带发现**：`uv sync` 会**清掉未声明的手装包**——`openpyxl` 因此被卸，
      两个测试文件直接 collection error。已补进 dev 组（生产代码不用它）。
      `tests/unit/test_camoufox_driver.py` 的 `pytest.importorskip` 也随之删掉。
- [ ] **`geoip` 处置**：默认**保持关**（当前 venv 缺 `maxminddb`，开了会抛异常）。
      若 P5 判定「时区/出口 IP 不对齐是拦截主因」→ 先 `uv pip install "camoufox[geoip]"`
      + `camoufox set --geoip`，并**把外发 IP 探测请求**纳入部署环境的出口策略
- [ ] **uBO 排除**：`exclude_addons=[DefaultAddons.UBO]` 写进会话层配置，
      并在 P5 对比里**专门看一条**：uBO 开着时播放器是否起不来（假负样本的来源）
- [ ] **视口高度抖动（P5 前）**：8 次实测内高在 `940`/`948` 之间摆（§4.3.1）——
      `window=` 钉不住视口。要么接受（0.85%，远小于响应式断点），要么
      `new_context(viewport=…)` 钉死**并重验指纹自洽**（外框 spoof 与视口要能对上，
      否则本身是破绽）。**不许在没验的情况下改成 viewport**
- [ ] 契约测试参数化到两个实现（照 perception 的 `IMPLEMENTATIONS` 模式）
- [x] `check` 命令能探 camoufox（`--driver camoufox`，P1）。**默认仍是 obscura**——
      生产链在 P5 之前不许换实现；`--driver` 是显式点名，不是默认值变更
