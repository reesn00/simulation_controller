# 工具调用说明

> **对象**：项目依赖的所有外部/内部工具——不限于浏览器执行层。
> **分类规则**：按**工具形态**（浏览器 MCP / LLM / 外部 CLI / HTTP 桥 / ...）分大类。
> **每条工具**按五段式记录：参数表 / 返回形态 / 失败语义 / 驱动侧入口 / 通用陷阱。
> **当前已收录**：浏览器 MCP（obscura 参考实现）· **浏览器 · camofox-browser（MCP 适配器）——现行生产执行层** · Agent 会话工具（QwenPaw 运行时 28 个）。其余类别为占位，按落地节奏追加。
> 跨文档：[01-架构总览 · 执行层](01-架构总览.md) · [04-运行手册](04-运行手册.md)

---

## 阅读约定

每条工具按 **五段式** 记录：

| 段 | 内容 |
|---|---|
| 参数表 | 字段名 / 类型 / 必填 / 取值 / 说明 |
| 返回 | 当前参考实现的**实测形态**（NDJSON / 单行 JSON / 文本 / 数字……），以及 `text_head` 例子 |
| 失败语义 | 哪些情况下抛、哪些情况下静默返回空——决定上层 fail-closed vs 业务事实的分流 |
| 驱动侧入口 | 哪个驱动方法包装它（跨实现时入口名仍稳定） |
| 通用陷阱 | 不依赖具体实现的契约级注意点（ref 稳定窗口 / `isTrusted` / 等） |

**红线工具**（永久禁）单列在末节，与具体形态无关——任何走 `PageDriver` 协议或类似桥路的实现都必须把这部分屏蔽。

---

## ⚠️ 通用陷阱（适用于几乎所有外部工具）

1. **入参 schema 不可信** —— 不少外部服务（obscura 等）把 schema 放自定义键里，标准清单取不到入参类型。**入参名只能靠探针取证**。每个新工具进场前先跑对应探针，落盘到 `output/pipeline/`。
2. **错误有两种形态** —— 协议层 error=true，以及正文以 `Error:` 开头但 error=false。驱动层的 `looks_like_error` 必须把两者折叠为同一种业务失败，否则症状会和"页面没渲染""元素不存在"长得一模一样。
3. **传输失败 ≠ 业务失败** —— 进程退出 / 超时 / 网络断是传输故障，驱动层抛专用异常，上层须 fail-closed（I4）；元素不存在、记录找不到等是业务事实，**不抛**，仅返回空。
4. **凭据纪律**（CLAUDE.md 红线）—— key / Cookie / Authorization Header 走环境变量，代码里不出现字面量；`__repr__` 不吐凭据；测试不复制真凭据到 fixture。
5. **ref / handle** —— 任何会话内稳定句柄（ref / tab_id / handle）**不进训练数据、不喂给 LLM**——跨会话失效，样本永远不可回放。

---

## 工具分类索引

| 类别 | 当前参考实现 | 状态 |
|---|---|---|
| [浏览器 MCP](#浏览器-mcp) | `obscura-mcp` 0.2.4 | **本节收录**（只读存量，2026-10-22 起不再产出） |
| [浏览器 · camofox-browser](#浏览器--camofox-browsermcp-适配器) | `camofox-browser` 1.18.1 + `camofox-browser-mcp`（**MCP stdio 适配器**，11 个 tool） | **本节收录**·**现行生产执行层** |
| [浏览器 · Camoufox](#浏览器--camoufoxplaywright-进程内-已退役--只读历史2026-10-26) | `camoufox` 0.5.8（Playwright **进程内**，非 MCP） | **已退役（2026-10-26）· 只读历史** |
| [Agent 会话工具](#agent-会话工具qwenpaw-运行时28-个) | 会话内 `tools` 模板集（28 个） | **本节收录** |
| LLM 提供商调用 | 站内 `LLMConfig` 走 OpenAI-compatible HTTP | 待落地；详见 [04-运行手册 §环境变量](04-运行手册.md) |
| 媒体探测 CLI | `ffprobe` 进程（`executor/probe/ffprobe.py`） | 待落地 |
| 标签平台 HTTP 桥 | Label Studio（`evaluation/ls_bridge/`） | 待落地；详见 [01-架构总览 §LS](01-架构总览.md) |
| VLM 图像复核 | 站内 `MultimVLMReviewer`（HTTP） | 待落地 |
| ... | ... | 按落地节奏追加 |

---

# 浏览器 MCP

> **本节详细记录浏览器 MCP 工具的入参 / 返回 / 陷阱**。
> 当前参考实现：`obscura-mcp` 0.2.4（协议 `2024-11-05`，stdio 传输，启动命令 `obscura mcp --stealth`）。
> 工具总数 37（`mcp_probe.py` 列清单，`tool_count` 在握手时核对；漂移即升级断点）。
> 驱动侧实现：[`executor/browser/obscura_driver.py`](../executor/browser/obscura_driver.py)
> 传输层：[`mcp_client.py`](../executor/browser/mcp_client.py) · 协议层：[`page_driver.py`](../executor/browser/page_driver.py)
> 取证脚本：[`mcp_probe.py`](../executor/browser/mcp_probe.py) · [`probe_returns.py`](../executor/browser/probe_returns.py)

---

## ⚠️ 浏览器 MCP 专属陷阱

1. **`inputSchema` 全部为 `null`** —— obscura 把 schema 放自定义 `input_schema` 键里，标准 MCP `list_tools` 取不到入参类型。**入参名只能靠探针取证**——已踩过的两个：
   - `browser_extract` 入参键叫 `schema`，**不叫 `fields`**（写成 `fields` 上游回 `Error: Missing schema object`，被 `on_error="empty"` 吞成空字典，症状和"选择器失效/页面没渲染/选择器名写错"长得一模一样）
   - `[]` 是**入参约定**、**返回的键不带 `[]`**（`titles[]` → `titles`）
2. **stealth build 必须带 `--stealth`** —— `from_env(stealth=False)` 走 `("mcp",)`，非 stealth build 也能用；stealth build 漏传 `--stealth` 会带着反检测降级运行，行为不可预期。
3. **`isTrusted` 警告** —— 合成事件 `isTrusted=false`；obscura stealth 下内部产生的事件 `isTrusted=true`。**点击一律走专用 click 原语，不要 evaluate 伪造点击**（CLAUDE.md I6）。

---

## 原语 1 · 导航与生命周期

### `goto(url, *, wait_until="load")` → `browser_navigate`（参考实现）

| 参数 | 类型 | 必填 | 取值 | 说明 |
|---|---|---|---|---|
| `url` | `string` | ✅ | 任意合法 URL | 目标地址 |
| `waitUntil` | `string` | ❌ | `load` \| `domcontentloaded` \| `networkidle0` | 默认 `load`；不传时不带进 args |

**返回**（obscura 实测）：`text("Navigated to <url> — \"<title>\"")`
**失败语义**：网络错误 / 页面崩溃 → 驱动层 `DriverError`；上层须 fail-closed，**不得**当成"页面是空的"继续走
**驱动侧入口**：`PageDriver.goto`
**通用陷阱**：URL 跳转后的 `URL: / Title:` 前缀在某些实现里会出现在 snapshot 正文里，snapshot 解析器要能剥离

### `close()` → `browser_close`（参考实现）
**无参。** 关整条 MCP 连接，**必须可重复调用**，关闭失败不得掩盖主流程异常。
**驱动侧入口**：`PageDriver.close`

---

## 原语 2 · 内容读取

### `snapshot(*, max_chars=None)` → `browser_snapshot`（参考实现）

| 参数 | 类型 | 必填 | 取值 | 说明 |
|---|---|---|---|---|
| `max_chars` | `integer` | ❌ | 正整数 | 正文字符预算 |

**返回形态**（obscura 实测）：
```
URL: https://example.com/
Title: Example Domain

<正文>

N interactive element(s) registered. Call browser_interactive_elements to list, or pass `ref` to browser_click/browser_fill/browser_type.
```

**通用陷阱**：
- **CSS 抢光预算**（obscura 实测：百度首页 6000 字符预算里 16675 字符都是 `<style>`）——任何浏览器 MCP 给的"可读正文"都不能保证不带 CSS；驱动层必须 fallback 到 `evaluate(innerText)`，再 fallback 到 `evaluate(textContent)` 剥节点
- 末尾"交互元素提示行"由 snapshot 解析器过滤
- `URL: / Title:` 前缀是人为加的，由 `parse_snapshot` 分离

**失败语义**：`Error: <message>` 文本（`isError` 仍可能为 `False`）；驱动层抛 `DriverError`
**驱动侧入口**：`PageDriver.snapshot(*, degraded)`

### `browser_markdown`（参考实现备用）
**当前驱动未包装**。Markdown 渲染，链接格式 `[text](url)`，不带 URL/Title 前缀。**仅在主 snapshot 被 CSS 严重污染且 `innerText` fallback 完全失败时**启用。

### `evaluate(expression)` → `browser_evaluate`（参考实现）

| 参数 | 类型 | 必填 | 取值 | 说明 |
|---|---|---|---|---|
| `expression` | `string` | ✅ | 任意 JS 表达式 | 执行结果 `toString()` 返回 |

**返回形态**（obscura 实测）：
- 字符串返回 → 原样文本
- 对象 / 数字 → 取决于表达式是否 `JSON.stringify`（obscura 即使不 stringify，`document.querySelectorAll('a').length` 也返回 `"1"` 字符串——这是 JS `toString` 的行为）

**驱动侧三套用法**（与具体实现无关，是契约级需求）：
1. **取主路径正文**：`"document.body.innerText"`
2. **取 fallback 正文**：克隆 body、剥 `<script>/<style>/<noscript>/<template>`、再 `textContent`（[90-增量编年史 · 增量四十三](../计划进展/90-增量编年史.md)）
3. **通用 evaluate**：返回值能 `json.loads` 就解析，不能就原样返回

**通用陷阱**：异常表达式 → `Error: <message>`

---

## 原语 3 · 结构化抽取

### `extract(fields)` → `browser_extract`（参考实现键名陷阱）

| 参数（参考实现） | 类型 | 必填 | 说明 |
|---|---|---|---|
| `schema` | `object` | ✅ | **`Record<string, string>`——键名是 `schema` 不是 `fields`**。每个参考实现可能不同，必须取证实测 |

**字段命名约定**（透传上游语义，不重命名）：
| 形态 | 例 | 行为 |
|---|---|---|
| 普通 | `"h2"` | 取首个匹配的文本 |
| 属性 | `"a@href"` | 取属性而非文本 |
| 数组 | `"titles[]"` | 取所有匹配，值为数组（**返回的键名通常不带 `[]`**） |

**通用陷阱**：选择器失效 / 页面没渲染 / 字段名拼错 → 抽不到 → 返回空映射，**不抛异常**——那是页面事实，由上层决定 fail-closed 或换判据
**驱动侧入口**：`PageDriver.extract`

---

## 原语 4 · 探测

### `count(selector)` → `browser_count`（参考实现）

| 参数 | 类型 | 必填 | 取值 | 说明 |
|---|---|---|---|---|
| `selector` | `string` | ✅ | 任意 CSS 选择器 | 例：`"video, audio"`、`"iframe"`、`"a"` |

**返回**：JSON 数字（**不是文本**），如 `"1"` 或 `"0"`——但**有些实现可能返回文本形态**，解析器必须接受两种

**通用陷阱**：探测失败 → 驱动侧 `on_error="zero"`，返回 `"0"` 并记 `degraded`，**不抛**

### `links(*, limit=200)` → `browser_links`（参考实现）

| 参数 | 类型 | 必填 | 取值 | 说明 |
|---|---|---|---|---|
| `limit` | `integer` | ❌ | 正整数 | 最大链接数 |

**返回形态**（obscura 实测）：
- NDJSON：`{"text":"...","href":"..."}` 每行一个对象
- 例外实现可能返回**单行 JSON 或带哨兵**（obscura 空页哨兵 `"No links found."`）

**通用陷阱**：
- bing 这类搜索结果页：`browser_links` 给的是**面包屑**而非片名——任何实现都可能踩这条；判断点 ① 改走 `extract` + `RESULT_SELECTORS` 取 `li.b_algo h2 a` 的 title/href。详见 [01-架构总览 §判断点 ①](01-架构总览.md) 与[避坑指南 §5.11](02-避坑指南.md)
- 写回 `links[].text` 时只按原始 href 查会一条都命不中——同一批的 `extract` 给的 URL 与 snapshot 拿到的 href 可能形态不一致（obscura 实测：解包 vs `/link?url=...` 包裹）

**驱动侧入口**：`PageDriver.links`

### `interactive(*, limit=200)` → `browser_interactive_elements`（参考实现）

| 参数 | 类型 | 必填 | 取值 | 说明 |
|---|---|---|---|---|
| `limit` | `integer` | ❌ | 正整数 | 最大元素数 |

**返回形态**（obscura 实测列式文本）：
```
ref=e1    a                      "Learn more"
```

**通用陷阱**：
- ref 是 **会话内稳定**（任何实现都应保证这一点；不保证 ref 的实现要按 `select` 兜底）
- 不同实现的列式布局可能不同——解析器要按空格分隔切**三列**，但允许 label 含转义引号
- **不把 ref 喂给 LLM**（见通用陷阱 §5）

**驱动侧入口**：`PageDriver.interactive`

---

## 原语 5 · 交互

> ⚠️ **本节是 obscura 的实现口径**——它是**只读存量**面
> （2026-10-22 起不再产出），**现行生产执行层是 camofox-browser**。
> **目标口径见** [camofox-browser · 目标编码：`role` + `name`](#目标编码role--namecamofox-面)
> （生产面；旧面见[已退役的 Camoufox 章 · 原语 5](#原语-5--元素定位与点击)）：
> D12 之后目标编码从 `{ref, selector}` 变成 **`{role, name}`**，
> 动作面不再接受会话句柄。改代码前先看清自己在改哪一层的口径。

### `click(*, ref=None, selector=None)` → `browser_click`（参考实现）

| 参数 | 类型 | 必填 | 取值 | 说明 |
|---|---|---|---|---|
| `ref` | `string` | 二选一 | 交互元素稳定 ref（来自 `interactive`） | 优先 |
| `selector` | `string` | 二选一 | 任意 CSS 选择器 | 兜底 |

**返回**（obscura 实测）：`"Clicked '<target>'"`

**通用陷阱**：元素不存在时**不抛异常**——那是业务事实（"播放按钮不在页面上"），驱动侧 `on_error="empty"` 静默吞掉；上层自然走 `answer=None` 分支，**不中断采集**

### `fill(*, text, ref=None, selector=None, role=None, name=None)`（旧名 `type_text`）
向输入框**设值**（先清空再写，不是追加）。

**参数按「面」分家（D12，同名不同参）**：

| 面 | 参数 | 实现 |
|---|---|---|
| obscura | `ref` \| `selector`（二选一） | `browser_fill` |
| camoufox（**已退役**） | `role` + `name` | `locator.fill(text)`（`exact=True`） |
| **camofox（现行）** | `role` + `name` | `camofox_type`（上游默认 `mode='fill'` ⇒ **设值**） |

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `text` | `string` | ✅ | 要写入的内容 |
| `ref` \| `selector` | `string` | obscura 面必填 | 同 `click` |
| `role` + `name` | `string` | camoufox / camofox 面必填 | 同 `click`；`name` 是 `aria_snapshot` 给出的可访问名 |

**驱动侧选择原因**：搜索场景必须"**设值**"——往已有关键词的搜索框里追加会产生错误 query。obscura 同时提供 `browser_fill`（设值）与 `browser_type`（追加），**两个实现都做设值**。

> ⚠️ **旧名 `type_text` 已改掉**（2026-10-20，[设计方案 04 §4.6](设计方案/04-camoufox执行层替换方案.md)）：那个名字暗示「逐字追加」，与两个实现的行为都不符——名字撒谎，迟早有人照着它写个真追加。语义层（`actions.TOOLS`）与驱动层现在同名，中间那张翻译表已删除。
>
> ⚠️ **`<select>` 不归本方法**：playwright 的 `fill` 只对可编辑元素有效。`combobox` 因此不在枚举词表「要填」的那一档里。

### `press(key, *, role=None, name=None)`（旧名 `press_key`）

两种形态：

| 形态 | 调用 | 面 |
|---|---|---|
| 不带目标 | `page.keyboard.press(key)` / `browser_press_key` | 三面都有 |
| 带 `role`/`name` | `locator.press(key)` | **camoufox 面**（已退役） |
| 带 `role`/`name` 或不带 | **驱动缓存合并**（重发同文本 + `pressEnter=true`） | **camofox 面（现行）· 只支持 `Enter`**——别的键**能力降级**（`press_unsupported`），obscura 面收到 `role`/`name` 抛 `ValueError` |

| 参数 | 类型 | 必填 | 取值 |
|---|---|---|---|
| `key` | `string` | ✅ | 任意 `KeyboardEvent.key`，如 `"Enter"`、`"Escape"`、`"ArrowDown"` |

> **搜索场景要带目标**：`fill` 之后在搜索框上按回车，与在页面级敲回车不是一回事——后者会把「搜索框没定位到」变成「在随便哪儿敲了下回车」，症状被抹平。冒烟里用的就是带目标那种（`check --action-smoke`）。

---

## 原语 6 · Tab 管理

### `new_tab(url=None)` → `browser_tab_new`（参考实现）

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | `string` | ❌ | 不传则开空 tab |

**返回**：tab id 文本（解析规则由调用方决定）

**通用陷阱**：浏览器 MCP 每条连接通常对应一个引擎 isolate（如 obscura = V8），**多站点遍历应复用同一连接并开 tab 切换**，重连会丢失整个 isolate 状态

### `close_tab(tab_id=None)` → `browser_tab_close`（参考实现）
关闭 tab。`None` = 当前活动页。失败静默（驱动侧 `on_error="empty"`）。

---

## 原语 7 · 调试与取证

### `network_requests()` → `browser_network_requests`（参考实现）
**无参。** 列出当前页已发起的网络请求。

**返回形态**（obscura 实测）：
```
[200] GET https://example.com/ (577B)
[200] GET https://example.com/s.js (1963B)
```

**驱动侧使用情况**：未包装，`probe_returns.py` 在取证时用于反爬识别 / 白名单验证

### `screenshot()` → `browser_take_screenshot`（参考实现推测存在）
obscura 0.2.4 推测存在该原语。**当前 `VLMReviewResult.screenshots` 字段为 `tuple[str, ...] = ()` 空元组**——是契约公开状态，等原语稳定暴露再启用。详见 [01-架构总览 §VLM 截图字段预留](01-架构总览.md)

---

## 🚫 红线工具：任何浏览器 MCP 实现永久禁

`PageDriver` 协议**必须**屏蔽这五类原语——不是"暂时没需求"，是**永不提供**：一旦暴露，将来某次"需要登录态"的临时需求会把 Profile 写进 P1 存档，而存档是要推给 Label Studio 的。

| 原语类别 | 触碰内容 | 不暴露原因 |
|---|---|---|
| `get_cookies` | Cookie | 同上 |
| `set_cookie` | Cookie | 同上 |
| `clear_cookies` | Cookie | 同上 |
| `set_storage_state` | 浏览器 Profile | localStorage / indexedDB / sessionStorage |
| `get_storage_state` | 浏览器 Profile | 同上 |

**纪律**（CLAUDE.md 红线节）：
- 凭据不得提交、打包、复制到测试、文档或日志
- 不保存 raw CoT / Cookie / Authorization Header / 浏览器 Profile
- 真出现登录墙站点：判 negative 并记 `login_wall_or_blocked`，**不**绕过登录——与失败分支表的设计一致

**实现检测方式**：obscura 已建 `FORBIDDEN_TOOLS = frozenset({...})` + `_assert_tool_allowed()` 闸门，新实现要按相同模式建表 + 拦截点统一走 `PageDriver._call`

---

## 📊 当前参考实现（obscura-mcp 0.2.4）工具覆盖

| 工具 | 驱动侧 | 落点 |
|---|---|---|
| `browser_navigate` | ✅ `goto` | 导航 |
| `browser_snapshot` | ✅ `snapshot` | 主路径正文 |
| `browser_markdown` | ❌（备用） | 同义但被 snapshot + innerText 覆盖 |
| `browser_evaluate` | ✅ `evaluate` + 内部两处 | JS 执行 + 正文 fallback |
| `browser_extract` | ✅ `extract` | 结构化字段抽取 |
| `browser_count` | ✅ `count` / `_count_or_zero` | 选择器计数 |
| `browser_links` | ✅ `links` | 链接列表 |
| `browser_interactive_elements` | ✅ `interactive` | 交互元素 ref |
| `browser_click` | ✅ `click` | 点击（ref 优先） |
| `browser_fill` | ✅ `fill`（旧名 `type_text`） | 输入框设值 |
| `browser_type` | ❌（被 fill 替代） | 输入框追加 |
| `browser_press_key` | ✅ `press`（旧名 `press_key`） | 键盘事件 |
| `browser_tab_new` | ✅ `new_tab` | 新建标签 |
| `browser_tab_close` | ✅ `close_tab` | 关闭标签 |
| `browser_network_requests` | ❌（仅取证） | 网络请求 |
| `browser_get_cookies` 等 5 个 | 🚫 红线 | 永久禁 |
| `browser_close` | ✅ `close` | 关整条 stdio |
| `browser_take_screenshot` | ❌（预留） | 等原语稳定启用 |
| 其余 ~14 个 | ❌ | 多为 stealth / 调试增强 |

**37 个 tool 中驱动层可见约 14 个**。升级时核对 `tool_count` 是否漂移；漂移不一定断点，但要走完 `mcp_probe.py` + `probe_returns.py` 两步取证才确认旧实现仍可用。

---

## 🛠️ 浏览器 MCP 调试与回归

| 命令 | 用途 |
|---|---|
| `uv run python trajectory_pipeline/executor/browser/mcp_probe.py --exe <path>` | 列 tool + 入参 schema，落盘 `output/pipeline/obscura_tools.json` |
| `uv run python trajectory_pipeline/executor/browser/mcp_probe.py --exe <path> --smoke <url>` | 冒烟：真调一次 `navigate` 原语 |
| `uv run python trajectory_pipeline/executor/browser/probe_returns.py --url <url>` | 实测各 tool 的**真实返回格式**（NDJSON vs 单行 JSON vs 文本），落盘 `output/pipeline/obscura_returns.json` |
| `uv run python -m trajectory_pipeline.executor.cli check` | 探浏览器能力自检（**唯一需真实浏览器的入口**） |

**重跑时机**：
- 接入新实现 / obscura 升级 → 跑两个探针对比 `tool_count` 与返回形态
- 升完发现"抽不到标题"/"links 拿不到"等异常 → 跑 `probe_returns.py` 对比 NDJSON / 单行 JSON
- 第一次接入新机器 → 两个都跑一次再 `cli check`

---

## ⚡ 快速调用模板（obscura 参考实现）

```python
from trajectory_pipeline.executor.browser.mcp_client import McpClient

async with McpClient.from_env() as mc:    # 读 OBSCURA_EXE 环境变量
    # 列工具
    tools = await mc.list_tools()

    # 导航
    r = await mc.call("browser_navigate", {"url": "https://example.com"})
    # r.text: 'Navigated to https://example.com/ — "Example Domain"'

    # 取快照（可选字符预算）
    r = await mc.call("browser_snapshot", {"max_chars": 6000})
    # r.text: 'URL: ...\nTitle: ...\n\n<body>...'

    # JS 求值
    r = await mc.call("browser_evaluate", {"expression": "document.title"})
    # r.text: 'Example Domain'

    # 交互元素
    r = await mc.call("browser_interactive_elements", {"limit": 50})
    # r.text: 'ref=e1    a                      "Learn more"'

    # 点击（ref 优先）
    r = await mc.call("browser_click", {"ref": "e1"})
    # r.text: "Clicked 'e1'"

    # 网络请求（取证用）
    r = await mc.call("browser_network_requests", {})
    # r.text: '[200] GET https://example.com/ (577B)\n...'
```

---

# 浏览器 · camofox-browser（MCP 适配器）

> **形态与上一节又不同**：obscura 走 **JSON-RPC over stdio**、工具名由上游所有（37 个）；
> **camofox-browser 自带一个 MCP stdio 适配器**（`camofox-browser-mcp`，**11 个 tool**），
> 而**浏览器本身由上游的 REST 服务持有**——**本仓库不启动浏览器**。
> 进程拓扑：
> `Python 进程 → MCP stdio → camofox-browser-mcp（Node）→ HTTP → camofox-browser REST（:9377）→ Camoufox 内核`。
>
> ⚠️ **浏览器内核仍是 camoufox**（上游依赖 `camoufox-js`）——
> **换的是接入方式（Playwright 直连 → 上游 REST + MCP 适配器），不是内核**。
> ⇒ 指纹能力仍在**内核**里；**收窄的是可配置面**（`humanize` / `geoip` / `locale` /
> `headless` 这些**由上游决定，本仓库配不了**，只能**读回实测值**）。
>
> **参考实现**：`camofox-browser` 1.18.1（MCP 握手 `serverInfo` 实测）· 适配器命令 `camofox-browser-mcp` · 协议 `2024-11-05` · **11 个 tool**
> **依赖手册**（11 个 tool 的参数与返回形状）：[依赖说明/camofox-browser 使用说明.md](依赖说明/camofox-browser%20使用说明.md)
> **接入方案**（owner 裁定与逐条判断）：[设计方案 21 · camofox-browser 执行层接入方案](设计方案/21-camofox-browser执行层接入方案.md)
> **驱动侧实现**：[`executor/browser/camofox_driver.py`](../executor/browser/camofox_driver.py) · [`executor/browser/camofox_session.py`](../executor/browser/camofox_session.py)
> **传输层**：[`executor/browser/mcp_client.py`](../executor/browser/mcp_client.py) · **快照解析**：[`executor/browser/aria_snapshot.py`](../executor/browser/aria_snapshot.py)

---

## 环境变量（**只有一个是本仓库读的**）

| 变量 | 谁读 | 说明 |
|---|---|---|
| `CAMOFOX_MCP_EXE` | **本仓库** | 适配器可执行文件的**位置**（**不是凭据**）。缺省走 `PATH` 找 `camofox-browser-mcp`；解析不出来**当场报错**（缺件即报错，不猜） |
| `CAMOFOX_BASE_URL` | 上游适配器 | REST 服务地址，缺省 `http://localhost:9377` |
| `CAMOFOX_USER_ID` / `CAMOFOX_SESSION_KEY` | 上游适配器 | 会话隔离标识。**默认不设**——适配器自己生成 `mcp-<uuid>`，于是「一条 MCP 通道 = 一个隔离会话」天然成立 |
| `CAMOFOX_ACCESS_KEY` / `CAMOFOX_API_KEY` | 上游适配器 | 🚫 **凭据——只由子进程继承，本仓库一个字节都不经手**，**绝不进存档 / 测试 / 文档 / 日志** |

> ⚠️ **`CAMOFOX_ACCESS_KEY` 不配会怎样**：上游 REST 设了它而 MCP 侧没配 ⇒
> **除 Cookie 导入外的所有工具调用都被拒**（401 / 403）。
> 而 `CAMOFOX_API_KEY` 只影响 `camofox_import_cookies`——**那一条本项目永不实现**（红线）。

---

## ⚠️ camofox 专属陷阱（动手前先读这十条）

1. **快照自带截图** —— MCP 契约**硬编码** `includeScreenshot=true`，
   而 MCP tool 的入参里**没有这个开关** ⇒ **每次快照都有一张 base64 PNG 穿过 stdio**，
   驱动**丢弃 `screenshot` 字段**（只取文本块）。**这是已知成本不是 bug**——要彻底消掉得改上游
   （owner 裁定：**不改上游**）⇒ **带宽问题留着**，但它**不进观察**。
2. **快照是 80k 字符窗口** —— 超了返回 `hasMore` / `nextOffset` ⇒ **必须翻页收齐再合并**。
   只读第一页 ⇒ 元素清单**缺一截**；读一半就过滤 ⇒ 「真同名」在**半页**上算 ⇒ 点错。
3. **`[eN]` 的位置不固定** —— `- heading "示例" [e1] [level=1]`（playwright 的属性后缀会跟在后面）；
   **名字本身也可能含 `[e3]`** ⇒ 取 ref 必须**按 `\[e\d+\]` 的形状在「行尾段」里找 token**，
   既不能假定它紧跟 name，也**不能在整行里搜**（会取到名字里的那个 ⇒ 点到另一个元素，**报告上看不出异常**）。
4. **名字走子串匹配，没有 `exact`** —— 上游定位是
   `page.getByRole(role, {name}).nth(nth)` ⇒ Playwright 的 name 匹配是
   **大小写不敏感 + 子串**。⇒ **工具声明里不许写「精确匹配」**——
   写「精确」就是教模型一个引擎不保证的性质，而它在报告上看不出异常。
5. **`press` 只支持回车** —— 上游**没有独立的「按键」tool**，唯一能提交表单的路径是
   `camofox_type(..., pressEnter=true)`。别的键、或对别的元素按键 ⇒ 本层**做不到**（见下文 A2 口径）。
6. **`tabId` 由服务端发** —— 不是本地自增的 `c1` / `c2`；**两套 id 不可互换**（同旧面的纪律）。
7. **REST 服务是常驻的 ⇒ 跨 Run 会状态泄漏** —— 上一条 Run 的标签页 / 登录态会泄到下一条。
   ⇒ **一个 Run 一份会话**；`close` 的语义是「**关掉本 session 的所有 tab**」，**不是「关浏览器」」。
8. **错误三态要按 HTTP 状态码重推** —— playwright 那套「异常类型」判据没了，
   **唯一还能判别错误种类的信号是错误文本里的 HTTP 状态码**：
   | 状态码 | 含义 | 处置 |
   |---|---|---|
   | **422** | `strict mode violation`（选择器命中多个） | **页面事实 / 定位问题**——不是能力不可用 |
   | **409** | `page_changed` | 页面在动作途中变了 ⇒ 重来（读数自检那套） |
   | **503** | `session_expired` | **能力不可用**——上游会话失效，要重启 REST 服务 |
   | **401 / 403** | auth | **配置错误**——`CAMOFOX_ACCESS_KEY` 两侧不一致 |
9. **`--headful` 已作废** —— 浏览器由上游 REST 服务持有 ⇒ **有头 / 无头不由本仓库决定**。
   旧开关**退役**：传了**报错退 2**（留着这个开关只会让读数看起来「切了口径」，而实际没切）。
10. **REST 服务没起 ⇒ 一屏 tool 错误** —— MCP 适配器**只做转发**，
    服务没起时**每个** tool 都失败，症状与「站点有问题」完全同形
    ⇒ 会话层 `launch` 里**先预检**（列一次 tab），失败就**当场说清「REST 服务没起」**。

---

## 本项目内的实际调用形态（camofox 面）

### 目标编码：`role` + `name`（camofox 面）

模型看到的仍然是 `{role, name}`——与旧 camoufox 面**同方言**（`aria_role`），
所以 P1/P2/P3 的键名投影、`target_params` 的派发、`agent_loop` 的动作组装**一个字都不用改**。

| 编码 | 对应调用 | 可移植 | 判断 |
|---|---|---|---|
| `ref`（会话内句柄） | 无 | ❌ 换 session 即失效 | ✗ |
| `{tag, label}` | `locator("button:has-text('立即播放')")` | ⚠️ 半（`<input>` 命不中） | ✗ |
| **`{role, name}`** | 上游 `getByRole(role, {name}).nth(nth)` | ✅ | ✓ **采用** |

⚠️ **语义与旧面不同**：旧面是 `get_by_role(role, name=name, exact=True)`（**精确**），
本面是**大小写不敏感子串匹配**（上游没有 `exact`）⇒
**「唯一可寻址」这件事从「引擎保证」变成「本项目保证」**。

⇒ **本项目在枚举期剔掉三类**，剔完清单里给出的每个 `(role, name)` 才**唯一可寻址**：

| 剔掉的理由 | 判定 | 为什么必须剔 |
|---|---|---|
| **真同名整组** | 同一 `(role, name)` 在整份 YAML 里出现 ≥ 2 次 ⇒ **整组丢** | 训练样本里只记 `{role, name}`，重放时命中哪一个是**赌**。与旧面同一条规则，但**理由不同**（旧面是 strict mode 报错，本面是模型分不清） |
| **名字是别人的子串**（**本面特有**） | 同 `role` 的**更早**节点的名字**包含**本名 ⇒ 剔 | 上游的 `nth` 按**精确同名**计数，而 `getByRole(role,{name})` 命中的是**含该子串**的所有元素 ⇒ 两者对不上时 `.nth(nth)` 会落到**另一个元素**上——**点错，且报告上看不出异常**。判据用 **YAML 序**（与上游自己编号的口径一致） |
| **上游没给句柄**（**本面特有**） | 该行**没有 `[eN]` 标注** ⇒ 剔 | 上游对一部分节点不分配句柄（名字命中 `SKIP_PATTERNS`（datepicker / calendar / date）、节点数超 `MAX_SNAPSHOT_NODES`=500、或该行不在上游的 `INTERACTIVE_ROLES` 里）⇒ 那些节点**点不到**，留在动作面里就是给模型一个**假的可点目标** |

> ⚠️ **「真同名整组丢」这条不许去掉**——它同时兜住了上游自己的一个坑：
> 上游的**注记**与它的**句柄表统计口径不同**（注记按**整份 YAML** 全局计数，
> 句柄表对 iframe 用**每帧独立**计数）⇒ 主框架与 iframe 出现同名 `role+name` 时，
> 注记会把主框架那一行标上 **iframe 的句柄** ⇒ 点它会点进 iframe。
> ⇒ **但这种情形下 YAML 里 `(role, name)` 出现两次** ⇒ 被「真同名整组丢」整组丢掉
> ⇒ **永远进不了动作面**。
> ⇒ **推论（不可改）**：只要这条规则还在，活到动作面的 `(role, name)` 就是整份 YAML 里唯一的
> ⇒ 上游的 `nth` 在所有口径下都是 0 ⇒ 注记一定正确。**去掉它，注记就会开始指错元素。**

**筛选阶梯的分工**：共享阶梯 `element_basis.ladder_stages` 管**前三档**
（role 词表 → 无名 → 真同名整组），**一个字都不许为某一个面改**；
**截断必须排在最后**（先截再剔会把「本来能留的」挤掉）；
**「名字子串歧义」与「上游没给句柄」两档是本面在共享阶梯之后自己加的**
（`CAMOFOX_FACE_STAGES = ("unref_dropped", "name_ambiguous")`）。

> ⚠️ **口径差异要记**：`elements_total` 在本面与 obscura / camoufox 面**不是同一个东西**
> （那两面 = 整页 ARIA 树过了词表的那些；本面 = **翻页收齐后**过了词表的那些）。
> 「面 ≠ 面可比」——不要拿两面的数字直接比。

### 协议原语 → 上游 tool 映射表（13 原语逐条）

| 协议原语 | 上游 tool | 补法 / 备注 |
|---|---|---|
| `goto` | `camofox_navigate` | 直接对应。**第一页是「建页带 URL」**（上游 `camofox_create_tab` 自带导航，比「先建空白页再导航」少一次往返） |
| `snapshot` | `camofox_snapshot` | 返回**多字段**（`url` / `snapshot` / `totalChars` / `hasMore` / `nextOffset` / `screenshot` …）⇒ 取 `snapshot` 文本，**丢弃 `screenshot` 字段** |
| `evaluate` | `camofox_evaluate` | **返回形态不同**：要解**两层**（MCP text → JSON → `{ok, result}` → `result`）；旧面拿的是 Python 原值（**不做 JSON 解包**）⇒ **口径差异要显式记** |
| `click` | `camofox_click` | 经两段映射：`{role,name}` → 我们的 `eN` → 上游的 `eM`。上游**没有** `role+name` 定位，只吃 `ref` 或 `selector` |
| `fill` | `camofox_type` | `ref` + `text`（+ `pressEnter`）。上游默认 `mode='fill'` ⇒ **设值**（先清空再写）**不是追加** |
| `press` | ❌ 无 | **驱动缓存合并（A2）**：重发 `camofox_type(ref, 同 text, pressEnter=true)`——见下 |
| `interactive` | `camofox_snapshot` **派生** | 解析 aria YAML 的 `[eN]` 标注；**翻页收齐 → 合并 → 过滤 → 编号**（我们的 `e1..eN` 按**留下的顺序**编，与旧面同一条规矩） |
| `links` | ❌ 无 | 由 `camofox_evaluate` 跑链接 JS。**保留「按 href 去重」**——这是**跨实现可比性硬口径**，不许因为换了实现就松 |
| `count` | ❌ 无 | 由 `camofox_evaluate` 跑 `querySelectorAll(...).length` |
| `extract` | ❌ 无 | 由 `camofox_evaluate` **逐字段**跑（上游**没有**「一次调用抽多字段」的等价物）。文本取 `innerText \|\| textContent`；属性取**解析后的绝对值** |
| `new_tab` / `close_tab` | `camofox_create_tab` / `camofox_close_tab` | **tabId 由服务端发**，不再是本地自增 `c1/c2` ⇒ `last_tab` 的口径要改 |
| `observe` | 组合 | `snapshot + links + interactive + count(video,audio) + count(iframe)`。`degraded` **记能力名**不抄 tool 名（`links` / `interactive` / `count:<selector>` / `body_text` / `nav_unsettled` / `press_unsupported`） |
| `close` | ❌ 无 | **Node 服务生命周期 ≠ 浏览器 tab** ⇒ `close` = 「**关掉本 session 的所有 tab**」，不是「关浏览器」 |

**11 个 tool 里 4 个不进 driver**：

| tool | 为什么不进 |
|---|---|
| `camofox_scroll` | **协议无滚动原语**（不实现；上游自己也说懒加载 / 虚拟滚动页面上滚轮事件无效） |
| `camofox_screenshot` | **非模型可见**——但**可给 VLM 复核链**（这条链终于能接上） |
| `camofox_list_tabs` | **与 tab 账目合并**（会话层已经维护标签页栈） |
| `camofox_import_cookies` | 🚫 **凭据红线——永不实现**（见下文红线节） |

### `press` 的口径（A2：驱动缓存合并，**不改上游**）

上游没有「按键」这个 tool ⇒ **唯一**能提交表单的路径是 `camofox_type(..., pressEnter=true)`。
所以驱动**记住最近一次 `fill` 的目标与文本**，收到 `press("Enter", 同目标)` 时**重发**那条 `type` 并附 `pressEnter=true`。

| 情形 | 处置 |
|---|---|
| `key != "Enter"` | **能力降级**（`press_unsupported`）——本层做不到 |
| 目标与上次 `fill` 不同 / 从没 `fill` 过 | **能力降级**——没有可重发的文本 |
| 其余 | 重发同文本的 `type` + `pressEnter=true` |

> ⚠️ **重发是安全的**：上游 `camofox_type` 默认 `mode='fill'` ⇒ `locator.fill(text)` 是**设值**
> （先清空再写）**不是追加** ⇒ 重发不会把文本写两遍。
>
> ⚠️ **`press_unsupported` 不是「元素不存在」**：前者是**能力不可用**，后者是**页面事实**——
> 两者的正确处置相反，混起来就是一条**理由通顺的假负样本**。
> 它会被带进下一次 `observe` 的 `degraded` ⇒ **下游不判**（fail-closed）。

### 快照分页：合并的三条口径

上游的窗口是「**头部 + 截断标记行 + 尾部**」，**每一页都重复同一段尾部**（整份 YAML 的最后 5k 字符）：

1. **尾部只取一次** —— 否则合并后尾部重复 N 遍；
2. **最后一页的头部可能已经盖住尾部** ⇒ 尾部**按总长**裁一刀（`totalChars - len(tail)`），
   **不靠内容猜重叠**（内容猜会在「结尾恰好重复」的页面上裁掉真内容）；
3. **页与页的边界可能切在行中间** ⇒ 把上一块末尾的**不完整行**挪到下一块前面，那一行才不会丢。

> ⚠️ **分页期间不许动作**：上游在 `offset>0` 时**复用缓存的快照**（**不刷新 ref**），
> 所以**一次分页读取内部是自洽的**；但**下一次** `offset=0` 的读取会**重新编号** ref
> ⇒ 「**读完再动作**」安全，「**读一半就动作**」不是。
> ⇒ 驱动在翻页**前后各读一次文档代号**（`performance.timeOrigin`），
> 变了就记 `nav_unsettled`——**这份元素清单横跨两个文档**，不可信。

### 三层分工（camofox 面）

| 层 | 文件 | 职责 |
|---|---|---|
| **传输层** | `executor/browser/mcp_client.py` | **通用 MCP stdio 客户端**——**不认识任何 tool 名**（obscura 专属常量留在 obscura 侧） |
| **会话层** | `executor/browser/camofox_session.py` | MCP 子进程生命周期 · **标签页栈** · 指纹**读回** · **一个 Run 一份会话** |
| **驱动层** | `executor/browser/camofox_driver.py` | `PageDriver` 协议实现；**目标编码 `{role,name}`**；`eN` **只在驱动内部消化** |
| **装配层** | `executor/browser/driver_factory.py` | 装配唯一入口（`DEFAULT_KIND = "camofox"`） |
| **上层** | `executor/steps/` · `perception/` · `orchestrator.py` | 控制流与感知——❌ **不认 camofox，也不认任何浏览器** |

### 会话层骨架（项目风格 · 真实实现）

> 真实实现：[`executor/browser/camofox_session.py`](../executor/browser/camofox_session.py)。
> 下面是它的**真实形状**的骨架。

```python
# trajectory_pipeline/executor/browser/camofox_session.py
"""camofox-browser 的执行层会话——**MCP stdio 通道 + 标签页栈 + 指纹读数**。"""

CAMOFOX_BASE_URL_DEFAULT = "http://localhost:9377"
CAMOFOX_EXE_DEFAULT = "camofox-browser-mcp"       # 走 PATH 查（上游文档给宿主注册的就是这个命令名）
CAMOFOX_EXE_ENV_VAR = "CAMOFOX_MCP_EXE"           # ⚠️ 可执行文件的**位置**，不是凭据
FORBIDDEN_TOOLS = frozenset({"camofox_import_cookies"})   # 🚫 硬拦，不是「不用」


class CamofoxSession:
    """camofox-browser 的一次执行层会话（**一个 Run 一份**）。"""

    def __init__(self, client: McpClient, *, base_url: str) -> None:
        self._client = client
        self._pages: list[str] = []            # 标签页**栈**（上游给的 tabId），栈顶 = 当前页
        self._fingerprint: dict[str, Any] = {} # 指纹读数（**实测**）。空 dict = **没测到**
        self._readings: dict[str, Any] = {}    # 诊断读数（**不进批次元数据**）

    @classmethod
    async def launch(cls, *, exe="", base_url="", timeout=DEFAULT_TIMEOUT_S) -> "CamofoxSession":
        command = resolve_executable(
            exe or os.environ.get(CAMOFOX_EXE_ENV_VAR, "").strip() or CAMOFOX_EXE_DEFAULT,
            env_var=CAMOFOX_EXE_ENV_VAR,
            hint="安装：npm i -g @askjo/camofox-browser-mcp（或 cd camofox-browser/mcp && npm link）",
        )
        url = base_url or os.environ.get("CAMOFOX_BASE_URL", "").strip() or CAMOFOX_BASE_URL_DEFAULT
        # ⚠️ **env 传 None**：子进程**继承**本进程环境 ⇒ 凭据天然到位，
        # 而本模块**一个字节都不经手**（凭据不出进程）。
        client = McpClient(command, args=(), timeout=timeout, env=None)
        await client.__aenter__()
        session = cls(client, base_url=url)
        try:
            await session._preflight()      # 列一次 tab：把「REST 服务没起」与「站点有问题」分开
        except Exception:
            await session.close()
            raise
        return session

    async def call(self, tool: str, args: Mapping[str, Any] | None = None) -> Any:
        """调一个 MCP tool，返回**解析后的 JSON**。**解析不了就抛**——形状变了就该炸穿。"""
        if tool in FORBIDDEN_TOOLS:
            raise RuntimeError("camofox tool 被禁用：导入 cookie 会把登录态带进采集面。")
        result = await self._client.call(tool, dict(args or {}))
        if result.is_error:
            raise DriverError(f"camofox tool {tool} 返回错误: {result.text[:400]}")
        return json.loads((result.text or "").strip())

    async def snapshot(self, tab_id: str, offset: int | None = None) -> Any:
        """取可访问性快照（**带 ``[eN]`` 句柄的注记 YAML**）。``offset`` 是上游的分页偏移。"""
        args: dict[str, Any] = {"tabId": tab_id}
        if offset is not None:
            args["offset"] = int(offset)
        return await self.call(TOOL_SNAPSHOT, args)
```

### 驱动层骨架（项目风格 · 真实实现）

> 真实实现：[`executor/browser/camofox_driver.py`](../executor/browser/camofox_driver.py)。
> 下面是它的**真实形状**的骨架。

```python
# trajectory_pipeline/executor/browser/camofox_driver.py
class CamofoxDriver:
    """camofox-browser 的 :class:`PageDriver` 实现（MCP 工具面）。"""

    name = "camofox"

    def __init__(self, session: CamofoxSession, *, collect_element_basis: bool = False) -> None:
        self._session = session
        #: 我们的 ``e1..eN`` → ``(role, name)``。只在采集面内部活着。
        self._refs: dict[str, tuple[str, str]] = {}
        #: 我们的 ``eN`` → camofox 的 ``eM``。**发动作时用**——训练数据里出去的还是 ``{role, name}``。
        self._camo_refs: dict[str, str] = {}
        #: ``(role, name)`` → 我们的 ``eN``。动作时按语义目标查它。
        self._target_index: dict[tuple[str, str], str] = {}
        #: 页面上的 ``(role, name)`` → **被本面剔掉的原因**（让「页面上没有」与
        #: 「页面上有但本面不可寻址」分得开——两者的正确处置相反）。
        self._dropped: dict[tuple[str, str], str] = {}
        #: **上一次 ``fill`` 的缓存**——``press`` 的合并依据（A2）。
        self._fill_cache: dict[str, Any] | None = None

    async def interactive(self, *, limit: int = DEFAULT_LIMIT) -> tuple[InteractiveElement, ...]:
        nodes = await self._read_annotated_nodes()      # ★ 翻页收齐再合并
        pairs = [(n.role, n.name or "") for n in nodes]
        unique, ledger = ladder_stages(pairs, role_vocab=ROLE_VOCAB)   # 共享阶梯（前三档）
        # …本面在共享阶梯**之后**再加两档：无上游句柄（unref） / 名字子串歧义（name_ambiguous）…
        # 剩下的按**留下的顺序**编我们的 ``e1..eN``（与旧面同一条规矩）

    async def click(self, *, ref=None, selector=None, role=None, name=None) -> None:
        target = self._locate("click", role=role, name=name, ref=ref, selector=selector)
        if target is None:
            return                                   # 业务事实（元素不在页面上）→ **不抛**
        camo_ref = self._camo_refs[target]
        await self._do("click", lambda: self._session.click(self._tab, camo_ref),
                       role=role, name=name)

    async def press(self, key: str, *, role=None, name=None) -> None:
        """按键。**本层只支持「在填过的那个输入框上按回车」**（A2）。"""
        if str(key) != "Enter":
            self._degrade_press("press", role, name, f"key:{key}")     # 能力不可用
            return
        cache = self._fill_cache
        want_role = role if role is not None else str((cache or {}).get("role") or "")
        want_name = name if name is not None else str((cache or {}).get("name") or "")
        if not cache or cache.get("role") != want_role or cache.get("name") != want_name:
            self._degrade_press("press", want_role, want_name, "no_cached_text")
            return
        # 重发同文本 + pressEnter=true ⇒ 上游的 type 是**设值**，重发安全
        await self._do("press", lambda: self._session.type_text(
            self._tab, camo_ref, str(cache.get("text") or ""), press_enter=True), ...)
```

---

## 🔴 红线：camofox 侧永久禁用的能力

> 与[浏览器 MCP 节的红线工具](#-红线工具任何浏览器-mcp-实现永久禁)同一批——
> **换实现不等于换红线**，本面的**等价物必须同样堵死**。

| 禁项 | camofox 侧对应 | 处置 |
|---|---|---|
| **导入 cookie** | `camofox_import_cookies` | 🚫 **永不实现**——`FORBIDDEN_TOOLS` **硬拦**（不是不用，是不许调）。导入 cookie 等于把登录态带进采集面，而本项目的样本必须「裸奔」才有归因意义 |
| 读 / 写 cookie | 同上（上游无独立读 cookie 的 tool） | **不包装** |
| 浏览器 Profile | — | 本会话**不落盘**，退出即弃 |
| 凭据 | `CAMOFOX_ACCESS_KEY` / `CAMOFOX_API_KEY` | **只由子进程继承，本仓库不碰**；`base_url` **只留 origin**（去路径 / 查询串 / **用户信息**） |
| 截图进工具声明 | `camofox_screenshot` / 快照自带的 `screenshot` 字段 | **不进**（非模型可见）；快照自带的那个**丢弃**——观察里混进视觉数据会污染感知层口径 |

**这些不是「暂时没需求」，是永不提供**——一旦暴露，将来某次「需要登录态」的临时需求
就会把登录态写进 P1 存档，而存档是要推给 Label Studio 的。
真出现登录墙站点，正确做法是**判 negative 并记 `login_wall_or_blocked`**，而不是绕过登录。

---

## ⚡ 快速调用模板（camofox 面 · MCP stdio 适配器）

```python
from trajectory_pipeline.executor.browser.camofox_session import CamofoxSession
from trajectory_pipeline.executor.browser.camofox_driver import CamofoxDriver

# ⚠️ 上游 REST 服务要**另外**起（cd camofox-browser && npm start，默认 :9377）
session = await CamofoxSession.launch()          # 缺件即报错；REST 没起也当场说清
try:
    driver = CamofoxDriver(session)

    await driver.goto("https://www.bing.com")      # 导航（第一页 = 建页带 URL）

    elements = await driver.interactive(limit=50)   # ★ 翻页收齐 + 三档过滤
    # [InteractiveElement(ref='e1', role='button', name='立即播放'), ...]

    await driver.click(role="button", name="立即播放")       # 目标编码：role + name

    await driver.fill(role="textbox", name="搜索", text="功夫")   # 设值，不是追加
    await driver.press("Enter")                              # ★ 驱动缓存合并：重发同文本 + 回车

    obs = await driver.observe()                             # snapshot + links + interactive + count
finally:
    await driver.close()                                     # 关掉本 session 的所有 tab
```

> **批注**：
> - `CamofoxSession.launch()` 是**类方法**——它会起 MCP 子进程并**预检** REST 服务；
>   **一个 Run 一次会话**，跨 Run 复用会把上一条样本的会话状态带进下一条。
> - `driver.click` / `fill` / `press` **只收 `role` + `name`**；
>   收到 `ref` / `selector`（obscura 面的词汇）会**抛 `ValueError`**——
>   拿错面说明调用点接了错实现，**必须炸穿**（`DriverError` 会被吃成 `unreachable_hard`，
>   接线 bug 就变成一条理由通顺的负样本）。
> - `press("Enter")` **不带目标**也行——`agent_loop` 派发 `press` 时就不带目标，
>   正好落在「最近一次 fill」的缓存合并口径上。
> - 探针：`uv run python -m trajectory_pipeline.executor.browser.probe_observe --driver camofox`
>   （真实 `goto + observe`，把 `Observation` 落盘并断言契约）。

---

# 浏览器 · Camoufox（Playwright 进程内）· 已退役 · 只读历史（2026-10-26）

> 🔴 **⚠️ 本章已退役（2026-10-26）——只读历史，不得照抄**
>
> **现行执行层是 camofox-browser（MCP 适配器）**，不是本章记的 camoufox（Playwright 进程内）。
> 接入方案见[设计方案 21 · camofox-browser 执行层接入方案](设计方案/21-camofox-browser执行层接入方案.md)；
> 现行章见[浏览器 · camofox-browser（MCP 适配器）](#浏览器--camofox-browsermcp-适配器)。
>
> **本章一个字都没删**——它记的是**旧执行层**的形态（Playwright 直连、**仓库自己起窗口**、
> 指纹参数由本仓库配），以及**当时的取证过程与踩过的坑**（依赖状态实测、调用形态、
> 十条专属陷阱、生命周期、反检测能力那几节）。
> **那些坑在[避坑指南](02-避坑指南.md)里仍被引用**——删掉就会让那些引用指向虚空。
>
> ⚠️ **但下面的事实描述的是 2026-10-26 之前的世界**：
> `camoufox` / `playwright` **已从主依赖移除**；
> `camoufox_{driver,session,probe}.py` 与 `probe_f5_name.py` **已删**；
> `--driver camoufox` **不再是合法选项**；
> `cloverlabs-camoufox` **只留在 `legacy` extra**（只服务 v1 冻结树，且同名抢 import 名）。
> **owner 原文：「禁止把原有的 camoufox 接回来，可以做清理原有的 camoufox 代码与内容。」**
>
> ⚠️ **本章里指向 `camoufox_driver.py` / `camoufox_session.py` / `camoufox_probe.py`
> 的那几个代码链接会点不开**——那三个文件**已随实现一起删**。
> **留着是故意的**：它们记的是**当时**的落点；删掉链接等于改写历史。
> 要看现行实现，请看上一章的[camofox-browser 章](#浏览器--camofox-browsermcp-适配器)。

> **形态与上一节不同**：浏览器 MCP 走 **JSON-RPC over stdio**，工具名由上游所有（37 个）；
> camoufox 走 **Playwright 协议（进程内）**，操作面是 Playwright 的 `Browser` / `BrowserContext` / `Page` / `Locator`。
> 两者**共享同一个上层契约** [`PageDriver`](../executor/browser/page_driver.py)——**驱动侧入口名跨实现稳定**；
> 本节记的是 camoufox 侧的**具体调用、参数与陷阱**。
>
> **参考实现**：`camoufox` 0.5.8 · `playwright` 1.62.0 · Firefox `v156.0.1-beta.36`（2026-10-20 本机实测）
> **依赖手册**（装 / 配 / 验收判据）：[依赖说明/camoufox_弃用.md](依赖说明/camoufox_弃用.md)
> **对接方案**（分阶段落地）：[设计方案 04 · Camoufox 执行层替换方案](设计方案/04-camoufox执行层替换方案.md)
> **取证脚本**（可重跑）：[`executor/browser/camoufox_probe.py`](../executor/browser/camoufox_probe.py)

---

## ⚠️ Camoufox 专属陷阱（动手前先读这十条）

1. **指纹不在 `firefox_user_prefs`，也不在 `launch_options()` 的返回顶层**——
   它被编码进 `env["CAMOU_CONFIG_1..N"]`（**分片的一个大 JSON**，实测 6 段 / 43 键）。
   `opts["navigator.userAgent"]` 取到的是 `None`，而「取不到」与「指纹没生成」同形。正确读法见[依赖说明 §4.3](依赖说明/camoufox_弃用.md)。
2. **绕过 `AsyncCamoufox` / `launch_server` 直接用 playwright 起浏览器，指纹会整个丢掉**——
   `opts["env"]` 必须传给 playwright 的 launch。
3. **`page.evaluate` 默认跑 isolated world**——DOM 共享，但**页面 JS 的全局变量读不到**（`window.__x` → `null`）。
   要读 main world 必须 `main_world_eval=True` + 表达式加 `"mw:"` 前缀，否则 playwright 直接报
   `Main world evaluation is disabled`。
4. **`screen` 逐 launch 变化**（实测 1920×1080 → 1536×864 → 1440×900）——
   viewport **逐 Run 漂移**，响应式站点的内容分布因此不可归因。**必须显式钉 `screen=` / `window=`**。
5. **`data:` URL 不能当探针页**——URL 里的 `#` 是 fragment 分隔符，payload 在首个 `#` 处截断，
   `<script>` 块**根本不执行**，探针会**静默给出错误结论**。用 `page.set_content()`。
6. **「元素不存在」与「页面卡住」都是 `TimeoutError`**——同型，无法靠异常类型区分。
   而「元素被替换（handle 失效）」是 `Error`，两者可区分。判定办法见[原语 5](#原语-5--元素定位与点击)。
7. **`isTrusted`**——真实点击 `true`，`evaluate` 伪造点击 `false`。**点击一律走 Locator，不要 evaluate 伪造**。
8. **`debug=True` 在 GBK 控制台必崩**——指纹字体清单含韩文字体名，`pprint` 打不出。`PYTHONIOENCODING=utf-8` 或 `chcp 65001`。
9. **`geoip=True` 会额外发一次公网请求**查出口 IP（经代理则经代理发），并据此对齐时区 / 经纬度 / locale / WebRTC IP。
   挂了代理**必须**开 geoip，否则「代理换了国家、浏览器还宣称原国家」是典型识别特征。
10. **`persistent_context=True` / `user_data_dir` 一律不开**（红线：不保存浏览器 Profile）。
    `launch_server` 本身会拒这两个参数（Playwright 无法把持久化 Context 暴露到 ws endpoint）。

---

## 本项目内的实际调用形态（**动作面必须与 camoufox 原样一致**）

> **上一节记的是 camoufox 的底层能力面**（官方 Python API）。
> 本项目**按这套能力面执行浏览器操作**——
> **动作的调用形态与 camoufox 原始调用 1:1**，不另造一套项目自有的动作词汇表。
> 采集（取证）那部分才是项目自有代码。

### ★ 为什么动作面不许改写

> **执行层用什么浏览器，训练侧的动作词汇表就长什么样。**
> 动作一旦被改写成项目自有的语义形态，**轨迹数据就只在本项目里成立**——
> 而本项目的产物是要给**别的** agent 用的。

本项目原来的动作形态是 `click(tag="button", label="立即播放")`——那是 **obscura 时代**的产物
（obscura 的 tool 返回文本，`{tag,label}` 是从文本里反推出来的）。
但**没有任何 camoufox API 能对应它**：
拿到 camoufox 的 agent 收到这个调用，**执行不了**→ 这批数据就废了。

| 动作 | camoufox 原始调用 | 参数 |
|---|---|---|
| `goto` | `await page.goto(url)` | `url` |
| `click` | `await page.get_by_role(role, name=name, exact=True).click()` | `role`, `name` |
| `fill` | `await page.get_by_role(role, name=name, exact=True).fill(text)` | `role`, `name`, `text` |
| `press` | `await page.keyboard.press(key)` / `locator.press(key)` | `key`（+ 可选 `role`/`name`） |
| `new_tab` | `ctx.new_page()` + `page.goto(url)` | `url` |
| `close_tab` | `await page.close()` | — |

**动作名一个都不改**（`goto`/`click`/`fill`/`press`/`new_tab`/`close_tab`）——
它们本来就与 camoufox 的方法名一一对应。
**要改的是参数形状**：`click(tag, label)` → `click(role, name)`。

> ✅ **协议层的方法名已改（2026-10-20，P2）**：`PageDriver.type_text` → **`fill`**、
> `PageDriver.press_key` → **`press`**。`obscura_driver` / `camoufox_driver` /
> `agent_loop._apply` / `steps/site_search` 与全部假 driver 同步，
> **`agent_loop` 里那张 `fill → type_text` 的翻译表已删除**。
> 理由同 D12——**`type_text` 这个名字在撒谎**：它暗示「逐字追加」，
> 而两个实现做的都是**设值**（先清空再写）。名字留着，
> 下次有人照着 `type_text` 写个真的追加就又错一次。
>
> **两侧同名不同参**：camoufox 面收 `role`/`name`，obscura 面收 `ref`/`selector`，
> **各自收到对方那面时抛 `ValueError`**（不是 `DriverError`——后者会被
> `steps/aggregator` 吃成 `unreachable_hard`，接线 bug 就变成理由通顺的负样本）。
> 见 [设计方案 04 §4.6](设计方案/04-camoufox执行层替换方案.md)。

### 目标编码：`role + name`

`ref`（会话内句柄）不可移植；`{tag,label}` 没有对应的 camoufox API
（且 `<input>` 无文本内容，`locator("input:has-text(...)")` **永不命中**）。
**只剩 camoufox 一等公民的 `get_by_role(role, name=name)`**。

| 编码 | 对应调用 | 可移植 | 判断 |
|---|---|---|---|
| `ref` | 无 | ❌ 换 session 即失效 | ✗ |
| `{tag, label}` | `locator("button:has-text('立即播放')")` | ⚠️ 半（input 命不中） | ✗ |
| **`{role, name}`** | `get_by_role("button", name="立即播放")` | ✅ | ✓ **采用** |

`name` 走**可访问名（accessible name）**算法，所以
`<input placeholder="搜索">` 的 name 就是「搜索」，
`<button><img alt="播放"></button>` 的 name 就是「播放」——
**输入框与图标按钮都覆盖得到**。

**ARIA role 词表（必须短）**：`button` / `link` / `textbox` / `searchbox` /
`combobox` / `checkbox` / `radio` / `tab`。
词表之外的 role **不枚举**——宁可少列，不要列一堆用不到、且各自带一堆边界情况的。

> ⚠️ **F5（待实测，最可能翻车的一处）**：采集层用 JS 近似算的 accessible name
> **未必等于 playwright 内部算的那个**（`aria-labelledby` 引用、`aria-label` 覆盖、
> `alt`/`title`/`placeholder` 回退……）→ `get_by_role(role, name=)` 找不到元素，
> 症状是「点了没反应」，而它**与「元素真不存在」完全同形且不报错**。
> 三条候选路径与兜底见[设计方案 04 §4.5.3](设计方案/04-camoufox执行层替换方案.md)。

### strict mode 是白送的 fail-closed

playwright 的定位器默认 **strict mode**：命中多个元素时**报错并列出候选**，不做「取第一个」。

| 情形 | playwright 行为 | 处置 |
|---|---|---|
| 命中 0 个 | `TimeoutError` | `count()` 预判 → 业务事实 → 静默 → 上层转 `unresolved` |
| 命中 1 个 | 正常执行 | ✅ |
| 命中多个 | **报错 + 列候选** | 由**代码**按文本匹配挑（不是让 LLM 猜）；选不出 → fail-closed |

**这是 camoufox 相对 obscura 的净增益**——obscura 的 `browser_click` 遇到多个候选时行为未测过。

### 三层分工

| 层 | 文件（拟） | 职责 | 认不认 camoufox |
|---|---|---|---|
| **会话层** | `executor/browser/camoufox_session.py` | 浏览器进程生命周期 · 指纹配置 · **一个 Run 一次 launch** | ✅ 唯一认 camoufox API 的一层 |
| **驱动层** | `executor/browser/camoufox_driver.py` | **动作面 = camoufox 原样调用**；采集面产出 `Observation` | ✅ |
| **装配层** | `executor/browser/driver_factory.py` | **装配唯一入口**（现在还不存在，见[设计方案 04 §2.3](设计方案/04-camoufox执行层替换方案.md)） | ✅ 选哪个实现 |
| **上层** | `executor/steps/` · `perception/` · `orchestrator.py` | 控制流与感知 | ❌ **不认 camoufox，也不认任何浏览器** |

> **⚠️ 换执行层后 `steps/` 会动**（`driver.click(ref=…)` → `driver.click(role=…, name=…)`）。
> **动的是接线，不是控制流**——判断点、分支、终止条件、失败样本入池规则一个字都不许跟着动。
> 判断全在 `perception/`（`Perceptor.decide`），那里一个字都不改。
> 见[设计方案 04 §2.4](设计方案/04-camoufox执行层替换方案.md)。

### 会话层骨架（项目风格）

> ⚠️ **本节是骨架，不是现状**（2026-10-20 起）：真实实现在
> [`executor/browser/camoufox_session.py`](../executor/browser/camoufox_session.py)，
> 与骨架的差异只有一处但很关键——**页面是栈**（`self._pages: list` + `page` 取栈顶，
> 带补页/弹栈/失败收页三条语义），不是下面骨架里的 `self._page`。
> 为什么非栈不可、四条语义各堵哪个坑，见下文「原语 6 · Tab / 页面管理」的
> 「★ 页面是**栈**」小节。

```python
# trajectory_pipeline/executor/browser/camoufox_session.py
"""camoufox 会话层——浏览器进程的身份与生命周期。

**一个 Run = 一次 launch**：身份与 viewport 在 Run 内恒定，跨 Run 才重抽
（见[设计方案 04 §4.3](设计方案/04-camoufox执行层替换方案.md)）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from camoufox.async_api import AsyncCamoufox
from camoufox.addons import DefaultAddons
from camoufox.fingerprints import Screen


@dataclass(frozen=True, slots=True)
class CamoufoxProfile:
    """一次 launch 的指纹配置——**必须原样落进批次元数据**。

    不记的话，某天换了 locale 或屏幕尺寸，批次分布会跳变，
    而读存档的人会读成「质量变好了」（CLAUDE.md「并池是写入时的动作」）。
    """

    os: str = "windows"          # 与宿主真实 OS 一致（不传则逐 launch 随机）
    locale: str = "zh-CN"        # 与出口 IP 地区一致
    screen: tuple[int, int] = (1920, 1080)
    window: tuple[int, int] = (1920, 1080)
    humanize: bool = True
    geoip: bool = False          # ⚠️ 需 camoufox[geoip] extra，默认关
    main_world_eval: bool = False   # 默认关——开了扩大采集面，见陷阱 3

    def launch_kwargs(self) -> dict[str, Any]:
        w, h = self.screen
        ww, wh = self.window
        return {
            "os": self.os,
            "locale": self.locale,
            "humanize": self.humanize,
            "geoip": self.geoip,
            "main_world_eval": self.main_world_eval,
            # uBO 默认会装（见「反检测能力」节）——它可能挡掉播放器的
            # 广告 CDN 请求 → 播放器起不来 → **假负样本**。本项目显式排除。
            "exclude_addons": [DefaultAddons.UBO],
            # 钉死几何：fpgen 默认逐 launch 抽不同 screen，viewport 漂移
            # 会让响应式站点的内容分布不可归因（实测 1920×1080/1536×864/1440×900）。
            # ``screen`` 约束**指纹里的**屏幕，``window`` 才是**实际视口**
            # ——两个都要钉，缺一不可。
            "screen": Screen(w, w, h, h),
            "window": (ww, wh),
        }


class CamoufoxSession:
    """持有浏览器进程与 context。**跨 tab 复用**（同一身份遍历多站）。"""

    def __init__(self, profile: CamoufoxProfile) -> None:
        self._profile = profile
        self._cm: Any = None
        self._browser: Any = None
        self._ctx: Any = None

    async def __aenter__(self) -> "CamoufoxSession":
        self._cm = AsyncCamoufox(**self._profile.launch_kwargs())
        self._browser = await self._cm.__aenter__()
        # new_context 自动 no_viewport=True（camoufox 的 attach_no_viewport_default 补丁）
        self._ctx = await self._browser.new_context()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._cm is not None:
            await self._cm.__aexit__(*exc_info)
        self._cm = self._browser = self._ctx = None
```

### 驱动层骨架（**动作面 = camoufox 原样调用**）

> ⚠️ **本节是骨架，不是现状**（2026-10-20 起）：真实实现在
> [`executor/browser/camoufox_driver.py`](../executor/browser/camoufox_driver.py)，
> 协议十四条原语**已全部落地**（P3-β）。下面的代码保留是为了记「为什么长这样」，
> 与现状有三处已知差异，**读代码以文件为准**：
> ① `click`/`fill`/`press` 是**三态**（`count()` 预判：0 → 静默当业务事实、1 → 操作、
> ≥2 → strict 报 `DriverError`），不是骨架里的「0 就 return」两态；
> ② 元素定位一律 `exact=True`；③ 页面是**栈**（`self._pages`）而不是 `self._page`。

```python
# trajectory_pipeline/executor/browser/camoufox_driver.py
"""camoufox 驱动——**动作面与 camoufox 的原始调用 1:1**。

两条线要分清：

- **动作面**（``goto`` / ``click`` / ``fill`` / ``press`` / ``new_tab`` / ``close_tab``）
  ——进动作流、进训练工具声明。**必须与 camoufox 的调用形态一致**，
  否则轨迹数据脱离本项目就执行不了（见本节开头）。
- **采集面**（``snapshot`` / ``interactive`` / ``count`` / ``links`` / ``extract``
  / ``evaluate`` / ``observe``）——不进动作流、模型看不到，
  是**本项目的取证格式**，格式知识全在这一层。

与 obscura 版的分工：obscura 是 tool 返回的文本形态，camoufox 是 playwright 的原生值。
"""

from __future__ import annotations

from typing import Any, Mapping

from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.executor.dom import parse_body_text
from trajectory_pipeline.perception.base import InteractiveElement, Observation


class CamoufoxDriver:
    """camoufox（Playwright 进程内）驱动。"""

    name = "camoufox"

    def __init__(self, session: Any) -> None:
        self._session = session
        self._page: Any = None
        self._refs: dict[str, Any] = {}   # ref → ElementHandle（**仅采集面**用）

    # ── 动作面：每行都能在裸 camoufox 里原样执行 ────────────────

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        """↔ ``await page.goto(url)``"""
        try:
            await self._page.goto(url, wait_until=wait_until)
        except Exception as exc:                      # noqa: BLE001
            # 导航失败是**能力不可用**——上层须 fail-closed，
            # 不得当成「页面是空的」继续走。
            raise DriverError(f"camoufox 导航失败: {type(exc).__name__}: {exc}") from None

    async def click(self, *, role: str, name: str) -> None:
        """↔ ``await page.get_by_role(role, name=name).click()``

        ⚠️ **不用 ref、不用 tag+label**——那两个都没有 camoufox 原生对应物。
        strict mode 命中多个会抛错并列候选；命中 0 个是 ``TimeoutError``
        （与「页面卡住」同型，须 ``count()`` 预判，见设计方案 04 §4.2）。
        """
        loc = self._page.get_by_role(role, name=name)
        if await loc.count() == 0:
            return          # 元素不存在 = 业务事实，静默；上层转 unresolved
        await loc.click()

    async def fill(self, *, role: str, name: str, text: str) -> None:
        """↔ ``await page.get_by_role(role, name=name).fill(text)``

        ``fill`` 是**设值**不是追加——往已有关键词的框里追加会产生错误 query。
        """
        loc = self._page.get_by_role(role, name=name)
        if await loc.count() == 0:
            return
        await loc.fill(text)

    async def press(self, key: str, *, role: str = "", name: str = "") -> None:
        """↔ ``await page.keyboard.press(key)``（无目标）
        ↔ ``await page.get_by_role(role, name=name).press(key)``（有目标）"""
        if role and name:
            await self._page.get_by_role(role, name=name).press(key)
        else:
            await self._page.keyboard.press(key)

    # ── 采集面：项目自有，模型看不到 ────────────────────────────

    async def snapshot(self, *, max_chars: int | None = None,
                       degraded: list[str] | None = None) -> Any:
        inner = await self._page.evaluate("document.body.innerText")
        body = parse_body_text(inner or "", max_chars=max_chars)
        if not body and degraded is not None:
            # innerText 偶发空（JS-heavy 站 layout 计算失败）→ fallback 到
            # textContent，**但必须剥掉 script/style/noscript/template**，
            # 否则会把 JS/CSS 当正文采进来 → 越过 sufficiency 闸门 → 假恢复。
            fallback = await self._page.evaluate(TEXT_CONTENT_EXPR)
            body = parse_body_text(fallback or "", max_chars=max_chars)
            if not body:
                degraded.append("body_text")
        ...
```

> ⚠️ **驱动层自己不做 `humanize` / `geoip` / 身份管理**——那是会话层的职责。
> 驱动层只做「把 playwright 调用包成协议原语」+ **红线闸门**
> （`context.cookies()` / `storage_state()` / `add_cookies()` 一律不包装）。

### 依赖状态（**已接入**）

| 事实 | 状态 |
|---|---|
| v2 主依赖 | `httpx` / `mcp` / `pyyaml` / `pydantic` + **`camoufox==0.5.8`** + **`playwright==1.62.0`**（2026-10-20 声明，**钉死版本**） |
| venv 实际装的 | `camoufox` 0.5.8（**daijro 上游**，dist 名就是 `camoufox`）+ `playwright` 1.62.0 |
| `pyproject.toml` 里的 `cloverlabs-camoufox==0.6.0` | 在 **`[project.optional-dependencies].legacy`** 里——**那是 v1 归档树用的**（cloverlabs 的 **fork**，包名都不同），`uv sync` 默认**不装** |

**两条纪律**：
1. **主依赖钉版本，不要用范围**，**不要**用 legacy extra 里那个——
   指纹由 fpgen 生成，版本一动指纹就动，而批次分布的可归因性建立在「指纹只随配置变、
   不随升级变」上；`cloverlabs-camoufox` 与 `camoufox` 是**两个不同的包**，参数名与 API 面可能有差异。
2. **不要 import v1 的 camoufox 用法**（CLAUDE.md 纪律 2：存量只读参考）——
   需要就在新树重写，照着读一遍即可。

> ⚠️ `playwright` 是 camoufox 的传递依赖，**仍要直接声明**：`camoufox_driver.py`
> 直接 import 它的异常类型。直接依赖不直接声明，上游哪天换实现，报错会出现在 import 期
> 而不是测试里。

---

## 生命周期 · 打开浏览器

### `AsyncCamoufox(**launch_options)` → `Browser`（推荐）

```python
from camoufox.async_api import AsyncCamoufox

async with AsyncCamoufox(os="windows", geoip=True, humanize=True, headless=False) as browser:
    ctx = await browser.new_context()        # 自动 no_viewport=True
    page = await ctx.new_page()
    await page.goto("https://example.com")
```

同步版：`from camoufox.sync_api import Camoufox`（同名同参）。
`async with` 退出时**自动关浏览器 + 关 playwright 进程**——异常路径也会回收（源码 `AsyncCamoufox.__aexit__` 里 `finally` 保证）。

> ⚠️ **本项目的口径是 `headless=True`**（2026-10-21 起，无人值守批次）。
> 上面示例与参数表里的 `headless=False` 是 **camoufox 库自己的默认值**，
> **不是本项目的推荐配置**——照抄会落回 headful 那条会超时的路
> （窗口不可渲染时单次点击 5–13s）。理由与实测见
> [依赖说明 camoufox §7.1.1](依赖说明/camoufox_弃用.md)。

| 参数 | 类型 | 必填 | 取值 / 默认 | 说明 |
|---|---|---|---|---|
| `os` | `str \| list[str]` | ❌ | `"windows"` / `"macos"` / `"linux"`；默认**三选一随机** | 指纹的目标 OS。**应与宿主真实 OS 一致**（字体清单 / GPU / 核数都按它生成） |
| `geoip` | `bool \| str` | ❌ | `True` / 出口 IP 字符串 / `False` | 一个开关对齐**时区 + 经纬度 + locale 语言与地区 + WebRTC IP**。`True` 会**发公网请求**查自己的出口 IP |
| `geoip_db` | `str` | ❌ | 如 `"GeoIP AIO by daijro"` | 不传则用 `camoufox set --geoip` 选的那个 |
| `humanize` | `bool \| float` | ❌ | `True` / 最大秒数（默认最长 1.5s） | 鼠标轨迹人性化。关掉等于瞬移 |
| `locale` | `str \| list[str]` | ❌ | 如 `"zh-CN"`；默认**随机抽地区** | **必须与出口 IP 的地区一致**，否则自相矛盾（实测默认配置就是「中国时区 + 全英文语言」） |
| `headless` | `bool \| "virtual"` | ❌ | 默认 `False` | `"virtual"` = Xvfb，**仅 Linux**。无头会收窄屏幕尺寸去凑 |
| `screen` | `Screen` | ❌ | `Screen(min_width, max_width, min_height, max_height)` | **约束**生成屏幕尺寸的范围（不是写死尺寸） |
| `window` | `tuple[int, int]` | ❌ | 如 `(1920, 1080)` | 固定窗口尺寸，并按屏幕**居中**、反算 inner 尺寸以保持自洽 |
| `fingerprint` | `dict` | ❌ | fpgen 指纹 | 自带指纹会触发 `custom_fingerprint` 警告，**非必要不传** |
| `fingerprint_preset` | `bool \| dict` | ❌ | `True` = 随机内置真实预设；`dict` = 钉死该预设 | 真实设备预设，身份跨 launch 稳定。**会与其它批次关联**，谨慎 |
| `proxy` | `dict` | ❌ | `{"server": "http://…"}` | 与 `geoip=True` 成对出现 |
| `webgl_config` | `tuple[str, str]` | ❌ | `(vendor, renderer)` | 钉死 GPU 伙伴。**必须先 `os`**，且组合必须来自 `camoufox.webgl.firefox_gpus(os)`，否则 `ValueError` |
| `block_images` / `block_webgl` / `block_webrtc` | `bool` | ❌ | 默认 `False` | **保持 False**。拦图已被报告为大型 WAF 的检测诱因；很多 WAF 查 WebGL 是否可用 |
| `disable_coop` | `bool` | ❌ | 默认 `False` | 关 COOP 是为了能点跨域 iframe 里的 Turnstile checkbox，**本身可被识别** |
| `custom_fonts_only` | `bool` | ❌ | 默认 `False` | 保持 False。伪装 OS 却抽掉该 OS 字体 = 自相矛盾 |
| `exclude_addons` | `list[DefaultAddons]` | ❌ | 如 `[DefaultAddons.UBO]` | 默认装 uBlock Origin（真实用户画像的一部分），仅站点被插件弄坏才排除 |
| `addons` / `fonts` | `list[str]` | ❌ | 插件路径 / 字体族名 | 字体是**追加**到该 OS 默认字体集 |
| `ff_version` | `int` | ❌ | 不传 | 伪装 Firefox 版本极易被识别，**不传**（UA 由真实二进制版本推出） |
| `enable_cache` | `bool` | ❌ | 默认关 | 真实用户有缓存，可按采集需要开（吃内存） |
| `main_world_eval` | `bool` | ❌ | 默认 `False` | 允许主世界执行脚本，用法 `page.evaluate("mw:" + js)` |
| `persistent_context` | `bool` | ❌ | **禁止开** | 返回类型随之从 `Browser` 变 `BrowserContext`；**违反不保存 Profile 的红线** |
| `executable_path` / `browser` | `str \| Path` | ❌ | 二进制路径 / 版本号 | 不传则用 active 版本 |
| `firefox_user_prefs` / `args` / `env` | `dict` / `list` / `dict` | ❌ | 透传 | prefs 手调会破坏自洽，慎用 |
| `pin_cpu_cores` | `bool` | ❌ | 默认 `False` | 开着会真占 CPU 并**串行化并发** |
| `i_know_what_im_doing` | `bool` | ❌ | `False` | **唯一作用是把泄漏警告静音**——除非确知在做什么，别开 |
| `debug` | `bool` | ❌ | `False` | 打印下发配置。**GBK 控制台会崩**（见陷阱 8） |
| `virtual_display` | `str` | ❌ | 如 `":99"` | Xvfb 显示号 |

**返回**：`Browser`（`persistent_context=False`，默认）；`persistent_context=True` 时是 `BrowserContext`。
**失败语义**：浏览器未装 → `launch_path()` **自动下载**（联网）；参数非法（如 `webgl_config` 不在 `firefox_gpus` 里）→ `ValueError`；
launch 失败 → playwright 异常，**上层须 fail-closed**。
**驱动侧入口**：`PageDriver.close` / 构造期。
**通用陷阱**：`geoip` 与 `locale` 都要与**真实出口 IP 的地区**一致；三方（时区 / locale / IP）任一打架就是「不可能的设备」。

### `camoufox.server.launch_server(**kwargs)` → Playwright ws endpoint

```python
from camoufox.server import launch_server
launch_server(os="windows", geoip=True, humanize=True, headless=False)
# stdout: Websocket endpoint: ws://[::1]:59065/<token>
```

**与进程内 API 的取舍**：server 模式多一个进程、endpoint 只在 stdout 打一次（端口随机、token 在路径里），
但**可以用 `playwright.firefox.connect(ws)` 从别的进程连**。
**CLI 的 `camoufox server` 一个 stealth 参数都不暴露**（源码 `__main__.py` 是 `launch_server()` 裸调用），
要配参数只能自己包一层。

**⚠️ ws 客户端必须自己补 `no_viewport=True`**——进程内 API 有 `attach_no_viewport_default` 补丁，
**走 ws 的裸 Playwright 客户端没有**：不补则 `innerWidth/Height` 恒为 `1280×720`、`dpr` 被压成 `1`
（每次会话同一个常数，可跨会话关联）。

### `AsyncNewContext(browser, ...)` → 独立身份的 context

```python
from camoufox import AsyncNewContext
ctx = await AsyncNewContext(browser, preset=None, os="windows", webrtc_ip=None,
                            proxy=None, geolocation=None, **context_kwargs)
```

在**同一浏览器**里开一个**独立身份**的 context（per-context 指纹）。
注意与 `browser.new_context()` 的区别：后者是普通 playwright context（**继承浏览器身份**），
前者是**新身份**。多站遍历要「同一身份换 tab」时用前者，要「换身份」时用后者。

---

## 原语 1 · 打开指定网站（导航）

### `goto(url, *, wait_until="load")` → `page.goto(...)`

| 参数 | 类型 | 必填 | 取值 | 说明 |
|---|---|---|---|---|
| `url` | `str` | ✅ | 任意合法 URL | 目标地址 |
| `wait_until` | `str` | ❌ | `"load"`（默认）\| `"domcontentloaded"`\| `"networkidle"`\| `"commit"` | 与 MCP 侧同名同义 |
| `timeout` | `float` | ❌ | 毫秒，默认 30000 | 导航超时 |

```python
await page.goto("https://example.com", wait_until="domcontentloaded")
```

**返回**：`Response | None`（`Response.url` / `.status` 可读；`wait_until="commit"` 时可能为 `None`）。
**失败语义**：超时 / DNS / 连接被拒 → `playwright.TimeoutError` / `Error`。
**上层须 fail-closed**：不得当成「页面是空的」继续走——那会把网络故障误判成「站点没内容」。
**驱动侧入口**：`PageDriver.goto`
**通用陷阱**：
- `page.url` 是**跳转后**的最终 URL（重定向已跟随）；要原始 URL 得看 `Response.url` 或 `request.redirected_from`。
- `wait_until="load"` 等到所有资源；JS-heavy 站点（iqiyi / ixigua / sohu / bilibili）建议 `"domcontentloaded"` 再显式等元素。

---

## 原语 2 · 网站内容获取

### `snapshot(*, max_chars=None)` → `page.title()` + `page.url` + `page.evaluate("document.body.innerText")`

```python
title = await page.title()
url   = page.url
body  = await page.evaluate("document.body.innerText")
```

**返回形态**：Playwright **直接给结构化值**（`str` / `dict` / `list`），**无 JSON 字符串**——
这是与 MCP 侧最大的形态差异：obscura 要 `json.loads` 还原，camoufox 不用。
**驱动侧入口**：`PageDriver.snapshot(*, degraded)`

**通用陷阱**：
- **正文主路径用 `innerText`**，不是 `page.content()`——后者是**渲染后的 HTML 源码**，
  含 `<style>` / `<script>`，正是 obscura 侧「CSS 抢光预算」的同一个坑。
- **`innerText` 偶发返回空**（JS-heavy 站因 layout 计算失败）——需要 fallback 到 `textContent`，
  且 fallback **必须剥掉 `script/style/noscript/template`**（实测：裸 `textContent` 会把 2651 字符的
  JS/CSS 当正文采进来，正文非空 → 越过 sufficiency 闸门 → **假恢复**）。
  obscura 侧的 `TEXT_CONTENT_EXPR`（clone + 删节点）可直接复用。
- **`textContent` 不折叠空白**，仍要走 `parse_body_text` 清理。

### `evaluate(expression)` → `page.evaluate(expression, arg=None)`

| 参数 | 类型 | 必填 | 取值 | 说明 |
|---|---|---|---|---|
| `expression` | `str \| JSHandle` | ✅ | JS 表达式或函数体 | 以函数体形式传时自动收到 `arg` |
| `arg` | `Any` | ❌ | 任意可序列化值 | 第二个参数 |

```python
n = await page.evaluate("document.querySelectorAll('a').length")          # → int
rows = await page.eval_on_selector_all("a", "els => els.map(e => [e.innerText, e.href])")
```

**返回形态**：**原生 Python 值**（`int` / `str` / `list` / `dict` / `None`）。
**失败语义**：表达式抛错 → playwright `Error`（含 JS 堆栈）；**不会静默返回空**。
**驱动侧入口**：`PageDriver.evaluate`

**⚠️ world 隔离（陷阱 3）**：
- 默认 **isolated world** → **读得到 DOM，读不到页面 JS 的全局变量**。
  要读 `window.__playerState` 这类状态，必须 `main_world_eval=True` + 表达式加 `"mw:"` 前缀：
  ```python
  # 启动时：AsyncCamoufox(main_world_eval=True)
  state = await page.evaluate("mw:window.__playerState")
  ```
- `mw:` 前缀**不能返回句柄**（playwright 明确报 `cannot return a handle`），也不能传元素/JS handle 作参数。
- **建议默认关**：开了扩大采集面，且**开了必须记进批次元数据**（否则「这批为什么读到了播放器状态」无法解释）。

### `extract(fields)` → `page.eval_on_selector_all(...)`（逐字段）

camoufox **没有** `browser_extract` 这种「一次调用抽多字段」的等价物，需逐字段 `eval_on_selector_all`。
**必须复刻 obscura 的两条入参约定**，否则会静默失效（症状与「抽不到标题」完全同形）：

| 形态 | 例 | 行为 |
|---|---|---|
| 普通 | `"h2"` | 取首个匹配的文本；**没有匹配就不写这个键**（不写空串——空串会被下游读成「抽到了，值是空」） |
| 属性 | `"a@href"` | 取**属性**而非文本（`@` 用 `rpartition` 切，选择器里含 `@` 也安全） |
| 数组 | `"titles[]"` | 取所有匹配，值为**数组**，无匹配时是**空数组**（**返回的键名不带 `[]`**） |

```python
# 文本：innerText 优先，空了退 textContent —— 见下方陷阱，这条不能省
_TXT = "els => els.map(e => ((e.innerText || e.textContent || '')).trim())"
titles = await page.eval_on_selector_all("li.b_algo h2 a", _TXT)
# 属性
hrefs  = await page.eval_on_selector_all("a", "els => els.map(e => e.getAttribute('href'))")
```

**失败语义**：选择器失效 / 页面没渲染 → 返回**空数组**，**不抛异常**——那是页面事实。
选择器语法错误 → playwright `Error` → 驱动抛 `DriverError`（**这是能力故障，不是「页面没内容」**）。
**驱动侧入口**：`PageDriver.extract`
**⚠️ 取文本必须 `innerText || textContent`**（2026-10-20 本机实测）：
bing 结果页 `li.b_algo h2 a` 的 **10 个锚 `innerText` 全是空串**，标题只在 `textContent` 里。
只取 `innerText` 的症状是「抽到 10 个空标题」→ `steps/search.result_titles` 判长度不等或为空
→ 判断点 ① 退回 fail-closed：**看着像选择器随改版失效，其实是取文本的路窄了**。
反过来，`textContent` 单用会把 `<script>` 里的话当文本——所以是**优先 + 兜底**，不是替换。

### `count(selector)` → `page.locator(selector).count()`

```python
n = await page.locator("video, audio").count()     # 缺失选择器 → 0，不报错
```

**返回**：`int`（**匹配总数，不受 `limit` 截断**——与 MCP 侧的 `browser_count` 一致）。
**失败语义**：选择器语法错误 → playwright `Error`（不是 0）。
**驱动侧入口**：`PageDriver.count`

### `links(*, limit=200)` → `page.eval_on_selector_all("a", ...)`

```python
_LINKS_JS = ("els => els.filter(e => (e.getAttribute('href') || '').trim())"
             ".map(e => [(e.innerText || e.textContent || '').trim(), e.href])")
raw = await page.eval_on_selector_all("a", _LINKS_JS)   # → list[[text, href]]
```

**返回**：`list[tuple[str, str]]`（文本, href）。

**⚠️ 必须按 href 去重（保留第一次出现的那条），且去重在前、截断在后**——这不是洁癖，是口径：
实测同一对搜索页，obscura 的 `browser_links` **恒 `total == distinct`**（`131/131`、`33/33`），
camoufox 不去重是 `168/133`、`48/35`——**`links_total` 高 28–37%**。
那个数要进 D-2 口径（`Observation.elements_total` 的注释解释了为什么「采到的条数」必须落盘），
两面不同义，并池比出来的分布就是假的。顺序反了（先截断再去重）条数同样对不上。

**⚠️ 与 MCP 侧同形陷阱**：`links()` 的 `text` 对搜索结果页往往**不是标题**
（实测 bing 只给面包屑 `qq.com https://v.qq.com › cover`），
而「标题 ↔ URL 的对应」正是判断点 ① 的全部判据 → **必须走 `extract` 抽标题**。
（obscura 的 `links[].text` 是 `host + path` 拼的面包屑；camoufox 这里是**锚文本**——
锚文本为空时才退化成面包屑形状，两面**不是同一回事**，别拿它对齐。）

### `observe()` → `snapshot` + `links` + `interactive` + 两个 `count` 的聚合

采集面唯一的**聚合原语**：一次产出完整的 `Observation`。
**字段一个都不少填**（`elements_total` / `links_total` 也是它填的），
`degraded` 是**确定性降级账**——某一块能力不可用就记一条能力名，**不静默当作「页面没东西」**：

| 块 | 失败后进 `degraded` 的字符串 | 该字段值 |
|---|---|---|
| `snapshot` | **不记，直接抛 `DriverError`**（I4：观察失败必须显现，无观察不能判分支） | — |
| `links` | `"links"` | `()` |
| `interactive` | `"interactive"` | `()` |
| `count(MEDIA_SELECTOR)` | `"count:video, audio"` | `0` |
| `count(IFRAME_SELECTOR)` | `"count:iframe"` | `0` |

**⚠️ 与 obscura 的不同义**：obscura 往 `degraded` 里塞的是**工具名**（`browser_links`…），
camoufox 塞的是**能力名**（`links` / `interactive` / `count:video, audio`）。
下游（`gatekeeper` / `branches` / `gates` / `probe_observe`）只测**非空**，所以现在无害；
但**拿两份 `degraded` 做集合比较会全不相等**，P5 双跑对账时别踩。
**驱动侧入口**：`PageDriver.observe`
**通用陷阱**：`MEDIA_SELECTOR` / `IFRAME_SELECTOR` 两个选择器**定义在 `executor/dom.py`**，
两个实现共用同一对——分头定义会让 `video_tag_count` 变成「同名不同义」，
而报告上看不出异常。

---

## 原语 3 · 页面搜索（页内搜索框填词 + 提交）

> 这是最典型的「网站搜索」动作：定位搜索框 → 填词 → 触发搜索（回车或点按钮）。

```python
# ① 定位搜索框——按 role / placeholder / name 任一
box = page.get_by_role("textbox", name="搜索")
box = page.get_by_placeholder("搜索")
box = page.locator("input[name='wd']")

# ② 填词（★用 fill，不要 type——往已有关键词的框里追加会产生错误 query）
await box.fill("功夫")

# ③ 提交——回车
await box.press("Enter")
#   或点搜索按钮
await page.get_by_role("button", name="搜索").click()

# ④ 等结果
await page.wait_for_load_state("domcontentloaded")
```

| 动作 | 调用 | 说明 |
|---|---|---|
| 按 role 定位 | `page.get_by_role("textbox", name=...)` | **可访问性角色**，最稳（跨改版存活） |
| 按可见文本定位 | `page.get_by_text("搜索")` | 文本匹配 |
| 按占位符定位 | `page.get_by_placeholder("搜索")` | |
| 按 name 定位 | `page.get_by_label("搜索")` | 关联 `<label>` |
| 按 CSS 定位 | `page.locator("input[name='wd']")` | 最脆，改版即失效 |
| 填值 | `locator.fill(text)` | **设值**（清空后写） |
| 逐字输入 | `locator.press_sequentially(text, delay=...)` | 拟人化逐字输入 |
| 回车 | `locator.press("Enter")` | |
| 等元素出现 | `await locator.wait_for(state="visible", timeout=...)` | |

**返回**：`fill` / `press` 返回 `None`；`wait_for` 在超时前未出现则抛 `TimeoutError`。
**失败语义**：
- 元素**不存在** → `TimeoutError`（**与「页面卡住」同型**，见陷阱 6）
- 元素**存在但被遮挡 / 不可见** → `TimeoutError`（actionability 检查失败，日志会写
  `element is visible, enabled and stable` / `<html> intercepts pointer events`）
**驱动侧入口**：`CamoufoxDriver.fill(*, role, name, text)` / `CamoufoxDriver.press(key)`
**通用陷阱**：
- **一律 `fill` 不 `type`**——MCP 侧 `browser_fill`（设值）与 `browser_type`（追加）语义不同，
  搜索场景要「设值」，追加会产生错误 query。
- `strict` 模式：locator 匹配**多个元素时抛错**（不是取第一个）。这比静默取第一个安全。

---

## 原语 4 · 页面搜索链接并指定点击

> 找一条链接（按文本 / href / 位置）并点它——与「网站内元素点击」的区别是**目标是一条链接**。

```python
# 按可见文本点链接
await page.get_by_role("link", name="功夫 第一集").click()
# 按 href 前缀点
await page.locator("a[href*='/video/']").first.click()
# 按文本内容点
await page.get_by_text("立即观看", exact=True).click()
# 先读出所有候选，再按序号点（需要人工/LLM 决策时）
links = await page.eval_on_selector_all("a", "els => els.map(e => [e.innerText, e.href])")
await page.locator("a").nth(3).click()
```

**返回**：`click()` 返回 `None`。
**失败语义**：匹配 0 个 → `TimeoutError`；匹配多个（未用 `.first` / `.nth`）→ **strict violation 抛错**。
**驱动侧入口**：`CamoufoxDriver.click(*, role, name)`
**通用陷阱**：
- **`.first` / `.nth` 是妥协**——它们把「选错元素」的错误从「抛错」降级成「静默点错」。
  更好的做法是**先取候选列表、让上层决策、再用唯一选择器点**。
- 点链接会**触发导航**；要等新页面用 `page.wait_for_load_state(...)` 或等 URL 变化。
- 新标签打开的链接（`target="_blank"`）**不会改变当前 page**——需监听 `context.on("page")`。

---

## 原语 5 · 元素定位与点击

### 定位器（Locator）——**本项目动作面只用这一种**

obscura 的 `ref=e1` 会话内句柄在 Playwright 里**没有对应物**。
camoufox 侧的对应概念是 **`Locator`（惰性查询，每次操作时重新解析）**：

| 定位方式 | 调用 | 特点 |
|---|---|---|
| CSS 选择器 | `page.locator("css")` | 最直接，但最脆 |
| 文本 | `page.get_by_text("...")` | |
| **role + 名称** | `page.get_by_role("button", name="播放")` | ★**本项目动作面只用这个**（可访问性树跨改版存活，且 name 走可访问名算法） |
| placeholder / label / title | `page.get_by_placeholder(...)` / `get_by_label(...)` / `get_by_title(...)` | |
| 测试属性 | `page.get_by_test_id("...")` | 需站点配合 |
| 文本片段 | `page.get_by_text("...", exact=False)` | |
| 元素句柄 | `await page.query_selector("...")` | **一次性快照**（见下） |
| 位置 | `page.locator("a").nth(3)` | 妥协，见原语 4 |

**★ 为什么动作面只认 `get_by_role`**（设计方案 04 §4.5）：
动作要进训练工具声明，而**进训练数据的动作必须能在裸 camoufox 里原样执行**。
`get_by_role(role, name=name)` 是 camoufox 的一等公民；
`{tag,label}` 没有对应 API（且 `<input>` 用 `:has-text` 永不命中），
`ref` 是会话句柄、换 session 即失效。

**⚠️ ref 缓存的选型（方案 §4.1）**：
采集面需要「先枚举出可交互元素并给它们编号」（`PageDriver.interactive` → `Observation.interactive_elements[].ref`），
感知层再把 `ref` 回传给执行器去定位那个元素。两条路：

| | **Locator 缓存** | **ElementHandle 缓存** |
|---|---|---|
| 存什么 | `ref → (选择器, 序号)` | `ref → ElementHandle` |
| 页面变化后 | 重新解析，**可能指向别的元素**（静默点错） | 句柄**失效**，抛 `Error`（明确失败） |
| DOM 污染 | 无 | 无 |
| 反检测 | 无痕迹 | 无痕迹 |
| **能不能进训练数据** | ❌ 会话句柄 | ❌ 会话句柄 |

**⚠️ D12 之后：这两条都不进动作面。**
句柄机制**只留在采集面内部**（供感知层按 `ref` 回传定位），
**动作面另走 `role + name`**。
原先「推荐 ElementHandle」的结论**只对采集面成立**——那里要的是「失败明确」，
而动作面要的是「可移植」。

**两条都不要用 `data-tp-ref="e1"` 之类的 DOM 属性注入**：
站点 JS 枚举一下 attribute 就能看到满页标记，**用反检测浏览器实现一个反检测泄漏点是自相矛盾**。

### `click(*, role, name)` → `page.get_by_role(role, name=name).click()`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `role` | `str` | ✅ | ARIA role：`button` / `link` / `textbox` / `searchbox` / `combobox` / `checkbox` / `radio` / `tab` |
| `name` | `str` | ✅ | 元素的**可访问名**（`<input placeholder>` 取 placeholder，图标按钮取 `alt`） |
| `timeout` | `float` | ❌ | 毫秒，默认 30000 |
| `force` | `bool` | ❌ | 跳过 actionability 检查。**慎用**——会点中被遮挡的元素 |
| `no_wait_after` | `bool` | ❌ | 不等导航完成 |
| `button` | `str` | ❌ | `"left"` / `"right"` / `"middle"` |
| `modifiers` | `list[str]` | ❌ | `"Alt"` / `"Control"` / `"ControlOrMeta"` / `"Meta"` / `"Shift"` |
| `position` | `dict` | ❌ | `{"x":…, "y":…}`，点击元素内的相对坐标 |
| `delay` | `float` | ❌ | 毫秒，按下与抬起之间的间隔 |

**返回**：`None`。
**失败语义**（playwright 的三种，须分开处理）：

| 情形 | 异常 | 语义 | 上层应对 |
|---|---|---|---|
| 元素**不存在** | `TimeoutError` | **业务事实** | 静默返回 → 上层转 `answer=None` / `unresolved` |
| 元素**在点击过程中被替换** | `TimeoutError`（重试到超时） | **业务事实** | 同上（playwright 会自动重试，日志写 `element was detached from the DOM, retrying`） |
| 元素在但**点不动**（被遮挡 / 页面卡住） | `TimeoutError` | **能力不可用** | 抛 `DriverError` → **fail-closed** |
| **命中多个** | strict violation `Error` | **不猜** | 报错并列候选 → 由**代码**按文本匹配挑；选不出 → fail-closed |

> ⚠️ **动作面没有句柄了**——D12 之后不存 `ElementHandle`，
> 所以「句柄失效」这条（obscura 侧会抛 `Error`）**不再出现**：
> 元素被替换时 playwright 重新解析定位器，找到就继续，找不到就是上面的
> 「不存在」或「点不动」。**少一种失败模式，不是多一种。**

**「不存在」与「卡住」同型（陷阱 6）** —— 用 **`count()` 预判**做确定性区分：

```python
loc = page.get_by_role(role, name=name)
if await loc.count() == 0:
    return None          # 业务事实：元素不存在，静默
await loc.click(timeout=...)   # 仍超时 → DriverError（能力不可用）
```

**这不靠解析异常消息**——playwright 的消息文本会随版本变。
**驱动侧入口**：`CamoufoxDriver.click(*, role, name)`
**通用陷阱**：
- **`isTrusted`（陷阱 7）**：真实点击 `true` / `evaluate` 伪造 `false`。
  **点击一律走 Locator**（CLAUDE.md I6）。
- **点击有 actionability 检查 + 自动重试**：元素在点击过程中被替换时会重试直到超时 →
  `TimeoutError`（日志写 `element was detached from the DOM, retrying`）。
- playwright 的 click 是**坐标点击**，会滚动到可视区——`<html> intercepts pointer events`
  说明有东西盖住了目标（遮罩层 / 弹窗），**不是**选择器错。
- ⚠️ **F5**：采集层算的 accessible name 若与 playwright 算的不一致，
  `get_by_role(role, name=)` **找不到元素**，症状与「元素不存在」同形且**不报错**。
  见[设计方案 04 §4.5.3](设计方案/04-camoufox执行层替换方案.md)。

### `fill(*, role, name, text)` → `page.get_by_role(role, name=name).fill(text)`

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `role` | `str` | ✅ | ARIA role（`textbox` / `searchbox` / `combobox` …） |
| `name` | `str` | ✅ | 输入框的**可访问名**（`<input>` 无 `label` 时取 `placeholder`） |
| `text` | `str` | ✅ | 要填的文本 |
| `timeout` | `float` | ❌ | 毫秒 |

```python
await page.get_by_role("searchbox", name="搜索").fill("功夫")
```

**返回**：`None`。**失败语义**：同 `click`（`TimeoutError` / `Error` / strict violation）。
**驱动侧入口**：`CamoufoxDriver.fill(*, role, name, text)`
**通用陷阱**：
- **用 `fill` 不用 `type`**（见原语 3）——`fill` 是**设值**（先清空再写），
  往已有关键词的框里追加会产生错误 query。
- `fill` 只对 `<input>`/`<textarea>`/contenteditable 有效；**对 `<button>` 调用会报**。
- ⚠️ **F5 同样适用**：`name` 算错 → 找不到框 → 静默（见上）。

### `press(key)` → `page.keyboard.press(key)`

| 参数 | 类型 | 必填 | 取值 | 说明 |
|---|---|---|---|---|
| `key` | `str` | ✅ | `"Enter"` / `"Tab"` / `"Escape"` / `"ArrowDown"` / `"Control+a"` … | playwright 键名 |

```python
await page.keyboard.press("Enter")
await page.keyboard.press("Control+a")
```

**返回**：`None`。**失败语义**：按键本身不校验元素存在；**焦点在哪就在哪打字**——
焦点不在搜索框时 `press("Enter")` 可能触发别的东西。**先 `fill` 再 `press`**。
**驱动侧入口**：`CamoufoxDriver.press(key)`

**⚠️ 有目标 / 无目标两种形态**（D12 记录这两种，因为它们进训练数据的形状不同）：

| 形态 | camoufox 原样调用 | 何时用 |
|---|---|---|
| **无目标** | `await page.keyboard.press(key)` | 焦点已在目标上（紧跟 `fill` 之后） |
| **有目标** | `await page.get_by_role(role, name=name).press(key)` | 要显式指定作用元素 |

有目标形态会先解析元素 → 元素不存在时**静默返回**（同 `click` 口径），
无目标形态**永远不会失败**（焦点在哪就在哪）。
**训练侧只暴露有目标形态**——无目标形态把「焦点在哪」这个隐含前提留在了动作之外，
模型学不到也复现不了。

---

## 原语 6 · Tab / 页面管理

| 动作 | 调用 | 说明 |
|---|---|---|
| 新开页面 | `await ctx.new_page()` | 对应 `PageDriver.new_tab`（**压栈**并成为当前页） |
| 切页 | `page.bring_to_front()` | |
| 关页 | `await page.close()` | 对应 `PageDriver.close_tab`（**弹栈**，回上一页） |
| 列出页 | `ctx.pages` | 实现里另有一层**栈**，见下 |
| 监听新页 | `ctx.on("page", handler)` | `target="_blank"` 的链接走这里 |
| 弹窗 | `page.on("dialog", handler)` | **不处理会卡住页面**——`alert` 阻塞 JS |

**⚠️ 语义差**：obscura 是「**单 browser 多 tab**」，playwright 是「**多 context 多身份**」。
`ctx.new_page()` 与 `browser.new_page()` 都能开页，但后者会**新建一个 context**（= 换身份）。
**多站遍历要「同一身份换 tab」**，用 `ctx.new_page()`。

### ★ 页面是**栈**，不是一个 `page` 字段（P3-β 实测定的）

`steps/link_check` 的形态是「开 tab → 观察 → 关 tab → **回中间页继续采**」。
实现里 `CamoufoxSession` 持 `self._pages: list` + `page` 取**栈顶**，三条配套语义不能省：

| 情形 | 处置 | 省掉的症状 |
|---|---|---|
| `close_tab()` 关当前页 | 弹栈，当前页自动回到上一页 | 无处可回 |
| 关的是**最后一页** | **先开一张空白页再关**，栈永不为空 | 后续 `observe` 全在读已关闭句柄：看着像「页面读不到」，其实是我们自己关空了 |
| `new_tab(url)` 导航失败 | **把这个新页收掉再抛** `DriverError` | 留着它，当前页停在 `about:blank`——一次外链失败连带弄丢中间页的证据 |
| `close_tab(tab_id)` 认不出 id | **抛 `DriverError`**，不静默 | **关错页不报错**，后面全错但没人知道 |

**tab 句柄是会话内的**（`c1` / `c2`…），与 obscura 的服务端 tab id **不可互换**；
现有调用点只用「当前页」语义，跨实现复用 id 会静默关错页（P5 双跑时注意）。

---

## 原语 7 · 等待与调试

| 用途 | 调用 | 说明 |
|---|---|---|
| 等元素 | `await locator.wait_for(state="visible")` | `"attached"` / `"visible"` / `"hidden"` |
| 等 URL | `await page.wait_for_url("**/search**")` | glob 模式 |
| 等加载态 | `await page.wait_for_load_state("domcontentloaded")` | `"load"` / `"networkidle"` |
| 等响应 | `await page.expect_response("...")` | 播放器接口常靠它 |
| 等请求 | `await page.expect_request(...)` | |
| 截图 | `await page.screenshot(path=..., full_page=True)` | **取证用**；截图属产物，按批次归档 |
| 网络请求 | `page.on("request"/"response", ...)` | 采集网络层证据 |
| 控制台 | `page.on("console", ...)` / `page.on("pageerror", ...)` | **JS 报错是强判据** |

**失败语义**：所有 `wait_for*` 超时 → `TimeoutError`。
**通用陷阱**：`networkidle` 在有长连接的站点（播放器轮询）**永远等不到**——用 `domcontentloaded` + 显式等元素。

---

## 🔴 红线：camoufox 侧永久禁用的能力

> 与 [浏览器 MCP 节的红线工具](#-红线工具任何浏览器-mcp-实现永久禁) 同一批，
> camoufox 侧的**等价物必须同样堵死**——换实现不等于换红线。

| 禁项 | camoufox / playwright 侧对应 | 处置 |
|---|---|---|
| 读 cookie | `context.cookies()` / `context.storage_state()` | **不包装** |
| 写 cookie | `context.add_cookies()` / `context.clear_cookies()` | **不包装** |
| 浏览器 Profile | `persistent_context=True` / `user_data_dir` | **不包装**（launch 时就不开） |

**这些不是「暂时没需求」，是永不提供**——一旦暴露，将来某次「需要登录态」的临时需求
就会把 profile 写进 P1 存档，而存档是要推给 Label Studio 的。
真出现登录墙站点，正确做法是**判 negative 并记 `login_wall_or_blocked`**，而不是绕过登录。

---

## ⚡ 快速调用模板（camoufox 官方用法 · 与本项目用法的差异见批注）

```python
from camoufox.async_api import AsyncCamoufox
from camoufox.addons import DefaultAddons
from camoufox.fingerprints import Screen

async with AsyncCamoufox(
    os="windows", geoip=True, humanize=True, headless=False,
    screen=Screen(1920, 1920, 1080, 1080),   # 钉死几何，否则逐 Run 漂移
    window=(1920, 1080),                     # 实际视口也要钉——screen 只管指纹
    exclude_addons=[DefaultAddons.UBO],      # 本项目加：uBO 可能挡播放器请求
) as browser:
    ctx = await browser.new_context()        # 自动 no_viewport=True
    page = await ctx.new_page()

    # 打开指定网站
    await page.goto("https://www.bing.com", wait_until="domcontentloaded")

    # 网站内搜索：填词 + 回车
    await page.get_by_role("textbox").fill("功夫")
    await page.get_by_role("textbox").press("Enter")
    await page.wait_for_load_state("domcontentloaded")

    # 页面搜索链接并点击
    rows = await page.eval_on_selector_all("li.b_algo h2 a",
              "els => els.map(e => [e.innerText, e.href])")
    await page.locator("li.b_algo h2 a").first.click()

    # 网站内容获取
    title = await page.title()
    body  = await page.evaluate("document.body.innerText")
    vids  = await page.locator("video, audio").count()
```

> **批注（与本项目实际用法的差异）**：
> - `geoip=True` 是**官方演示写法**，它需要 `camoufox[geoip]` extra
>   （`maxminddb`）且会**外发一次公网 IP 探测请求**。
>   本项目默认 `geoip=False`（当前 venv 未装 extra，传 True 会直接抛异常）。
> - `exclude_addons` 是**本项目加的**，官方默认会装 uBO。
> - `window` 也是**本项目加的**：`screen` 只约束指纹里的屏幕尺寸，
>   实际视口由 `window` 决定（`new_context()` 的 `no_viewport=True` 让视口=窗口尺寸）。
> - 本项目**不直接用这段代码**——它展示的是底层能力面；
>   项目里请走[本项目的实际调用形态](#本项目内的实际调用形态动作面必须与-camoufox-原样一致)——
>   **动作面与 camoufox 原始调用 1:1**，别照抄这段裸 API（它没钉几何、没排 uBO、没定 `geoip`）。

---

## 🛡️ 反检测能力：来源、边界与本项目的反检查口径

> **这一节回答两个问题**：camoufox 的「反检测」到底由什么提供？
> 换成它之后，本项目的「反检查」指标会不会变好？——**第二个问题目前没有答案**，
> 下面说清楚为什么没有、以及在什么条件下才算有。

### 能力来源：五层，缺一层就漏一项

| 层 | 在哪 | 提供什么 | 默认开吗 |
|---|---|---|---|
| **Firefox 补丁层** | camoufox 自带的 patched Firefox 二进制（C++） | 抹掉 Firefox 自身的自动化/指纹泄漏特征（`navigator.webdriver` 类痕迹、CDP 痕迹、window 特征等）——**这一层是 camoufox 的本体** | ✅ 随浏览器发行 |
| **fpgen 指纹生成** | `fingerprints.py` + `fpgen_model.py` | 合成一套**从未存在过但完全自洽**的设备身份（navigator / screen / WebGL / canvas / audio / 字体 / 语音 / media devices） | ✅ 默认走 fpgen（`fingerprint_preset` 不传时） |
| **coherence 一致性** | `coherence.py` | **跨字段**一致性：navigator、GPU、屏幕、字体、语音、媒体设备是**分别采样**的，拼起来可能是一台不存在的机器（Apple M1 配 2 核、Intel GPU 配刘海屏…）。这一层专门修这种「零件都对、组合荒唐」的破绽 | ✅ **无条件执行**（`launch_options` 末尾 `coherence.apply(config, target_os)`） |
| **GeoIP 对齐** | `geolocation.py` | 按**出口 IP** 对齐经纬度 / 时区 / locale / WebRTC IP——时区与 IP 不符是反爬最常抓的一条 | ❌ **默认关**（`geoip=None`） |
| **humanize 拟人** | 浏览器补丁层 | 鼠标轨迹拟人化（默认移动耗时上限 1.5s），抹掉「瞬移到按钮」的机器特征 | ❌ **默认关**（`humanize=None`） |

**uBlock Origin**：`launch_options` 无条件调用 `add_default_addons()`，
而 `DefaultAddons` 枚举里**只有 UBO** —— 即**每次 launch 都会装 uBO**。

> ⚠️ **这对本项目是个坑，不是好事**：uBO 会拦广告 CDN，
> 视频站的播放器初始化请求（尤其广告/预取分片）**可能被一并拦掉** →
> 播放器起不来 → 我们的感知层判「无播放控件」→ **写出理由通顺的假负样本**。
> 处置：`exclude_addons=[DefaultAddons.UBO]`（见上文 `CamoufoxProfile`）。

### ⚠️ 三个反检测参数**默认全是关的**（实测源码，不是文档转述）

`launch_options` 的签名里三者是 `Optional[...] = None`，
而源码里都是 `if xxx:` 才生效——**不显式传就等于没开**：

| 参数 | 默认 | 不传的后果 | 本项目处置 |
|---|---|---|---|
| `humanize` | `None` → **关** | 鼠标瞬移，行为特征裸露 | ✅ 显式 `True` |
| `geoip` | `None` → **关** | 时区/经纬度不与出口 IP 对齐 | ⚠️ 见下 |
| `main_world_eval` | `None` → **关** | 主世界脚本读不到页面 JS 全局 | ✅ 显式 `False` |

**`geoip=True` 还有一个硬前提**：需要 `camoufox[geoip]` extra（`maxminddb`）。
**当前 venv 里 `maxminddb` 未安装** → `ALLOW_GEOIP=False` → `geoip_allowed()` 直接
抛 `NotInstalledGeoIPExtra`。所以本项目**默认 `geoip=False`**；
将来要开，须先 `uv pip install "camoufox[geoip]"` 并 `camoufox set --geoip` 装库。

另注：`geoip=True` 会**主动外发一次请求**探测公网 IP
（`public_ip()` 打 api.ipify.org / checkip.amazonaws.com 等）——
这是个对外行为，接入时要在部署环境确认出口策略。

### 调用方式与反检测能力的关系

**反检测能力来自「浏览器 + 参数」，不来自「怎么调」。**
但**前提是参数得传对**——这里有一个硬约束：

> **绕过 `AsyncCamoufox` / `launch_server` 直接 `playwright.firefox.launch()`，
> 指纹会整个丢失。**
> 因为指纹不是传给浏览器的启动参数，而是经 `env["CAMOU_CONFIG_1..N"]`
> **分片 JSON** 送进补丁层的（见[依赖说明 §4.3](依赖说明/camoufox_弃用.md)），
> 再由 `launch_options` 组装。裸起 playwright 就没有这一步。

所以本项目的三层封装里，**会话层必须走 `AsyncCamoufox`**，
驱动层拿到的 `page` 才是带指纹的浏览器里的页面。

### 本项目的「反检查」口径

本项目要防的**不是**通用指纹检测，而是**中文视频站的反爬拦截**——
即：腾讯视频 / 爱奇艺 / 优酷 等站的滑块、验证码、风险页、空壳页。

| | 状态 |
|---|---|
| camoufox **具备**反检测能力 | ✅ 上面五层机制确实存在，机制层面成立 |
| camoufox 在**本项目目标站点**上的拦截率 | ❌ **未实测** |
| camoufox **相对 obscura** 的拦截率对比 | ❌ **未实测** |
| 实测口径 | 同一批任务、同一天、同一出口 IP，各跑 N 次，比**被拦截次数**与**误判为负样本的条数** |

> **不实测就不能换。** 换执行层是**高风险改动**（要重写 13 个原语 + 换掉
> 所有格式解析），而收益只是「反爬更强」这一个假设。
> 这个对比已写成[设计方案 04 §6.1](设计方案/04-camoufox执行层替换方案.md) 的
> **go/no-go 验收门槛**：**不达标就不换**，obscura 继续当执行层。

### 与 obscura 的关系（**不是「从无到有」**）

obscura 的 MCP server 也有 `--stealth` 开关。**两者的差别是强度与可控性，
不是「一个有、一个无」**：

| | obscura | camoufox |
|---|---|---|
| 形态 | Rust 无头引擎 + MCP（37 个 tool，**协议外**） | patched Firefox + Playwright（**协议内**） |
| 反检测参数 | 一个 `--stealth` 开关，**不可调** | 30+ 个参数**逐项可控** |
| 指纹 | 固定 | fpgen 逐 launch 生成，可钉死可复现 |
| 采集粒度 | tool 返回的**文本**（NDJSON / 列式文本，需解析） | playwright **原生值**（无需解析） |
| 换来的代价 | —— | 要重写驱动层 + 换掉全部 `parse_*` 解析器 |

**换 camoufox 的真正理由不是「反检测」，是「可控 + 原生值」**；
「反检测更强」是**待验证的附带收益**。
把两者混为一谈，会在验收时用错误的指标下结论。

---

# LLM 提供商调用

> **状态**：待落地。运行入口已在 `LLMConfig` 与 perceptor 层建好，工具级细节（各家 base URL / model 名 / 超时）按需追加。
> 详见：[04-运行手册 §环境变量](04-运行手册.md) 与 [01-架构总览 §perceptor](01-架构总览.md)。

待补条目（占位）：
- [ ] OpenAI-compatible HTTP（站内主路径，承接 `TRAJECTORY_LLM_*` 环境变量）
- [ ] 备份 / 多模型切换（若引入第二家）

---

# 媒体探测 CLI

> **状态**：待落地。当前用 `ffprobe` 做 m3u8 / mp4 媒体指纹，详见 `executor/probe/ffprobe.py`。

待补条目（占位）：
- [ ] `ffprobe` 子命令与归一化输出

---

# 标签平台 HTTP 桥

> **状态**：待落地。当前 LS 桥走 `evaluation/ls_bridge/`，HTTP 接口细节按落地节奏追加。

待补条目（占位）：
- [ ] Label Studio `POST /api/projects/:id/import` 等接口
- [ ] 鉴权（API token via env，仅 `__repr__` 不吐）

---

# VLM 图像复核

> **状态**：待落地。`MultimVLMReviewer` 走 HTTP 调 VLM，输入是 base64 / 路径双形态，详见 `executor/review/vlm_reviewer.py`。

待补条目（占位）：
- [ ] 入参结构（图像编码 / 提示词模板）
- [ ] 返回结构与解析容错

---

# Agent 会话工具（QwenPaw 运行时，28 个）

> **来源**：`archive/v1/etl/qwenformat/templates/tools/*.txt`，28 个文件 / 41,465 字节。
> 这些文本由 `etl/qwenformat/tool_templates.render_tool_definition` 从 C1 事件流的
> `model_request.payload.tools`（OpenAI function schema）渲染而来，是**会话内 tools 的完整内容**。
> **口径**：`model_request` 每轮都带一份 tools，**末次非空胜出**（`etl/qwenformat/load.py`）；
> 28 个模板是历次运行的去重并集，单次会话拿到的通常是其中的子集。
> **v2 用法**：任务生成 / 样本组装要往 SFT 样本里注入 tools 时，以本表为权威来源；
> **不要 import `archive/v1`**（CLAUDE.md 开发纪律 2），需要就在新树重建。
> **截断**：落 `<base>.messages.json` / `.openai.json` 时受 `tools_payload_max`（默认 64）限制；
> `meta.json` 的 `tools_declared` **只有名字**，不是完整内容（`gdr/domain/schema.py`）。

---

## ⚠️ 本节专属约定

1. **`anyOf: [X, null]` + `default: null` = 可选可空**，下表记作 `X | null`。不传与传 `null` 等价。
2. **LLM 误序列化容错是普遍设计**：布尔参数常写成 `bool | string | integer`（`"false"` 可接受），
   数值参数常写成 `number | integer | string`（`"1800"` 可接受）。
   **歧义值（`"maybe"` 之类）直接返回 ERROR**，不猜——与站内 I4 fail-closed 同一条纪律。
3. **相对路径基准不统一**——`read_file` / `write_file` / `edit_file` 从 *project directory* 解析，
   `append_file` 从 *WORKING_DIR* 解析，`execute_shell_command` 不传 `cwd` 时是 *agent workspace*。
   上游措辞如此，**v2 不要替它归一到单一口径**，否则合成出的调用参数会与真实分布不符。
4. **参数 schema 里几乎没有 `format`/`enum` 约束**——取值合法性靠工具自身校验并返回 ERROR。
   合成样本时**不要把"必填"误解为"必定出现"**：模型实际会漏参，这是真实分布的一部分。

---

## 1 · 文件与工作区（6）

### `read_file`

> Read a text file. Relative paths resolve from the project directory. Use start_line/end_line to read a specific line range (output includes line numbers). Omit both to read from the start. If output is truncated, the tail says which start_line to resume from. Images, PDFs and other binaries come back as unusable bytes rather than an error. Use view_image for images.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `file_path` | `string` | ✅ | — | 文件路径 |
| `start_line` | `string \| integer \| null` | ❌ | `null` | 首行（1-based，含）。**接受十进制字符串**以兼容 tool-call 序列化 |
| `end_line` | `string \| integer \| null` | ❌ | `null` | 末行（1-based，含）。同上 |

**使用**：输出带行号；截断时尾部给 resume 用的 `start_line`。**二进制不报错**——图片/PDF 会返回无意义字节，图片必须走 `view_image`。

### `write_file`

> Create or overwrite a file. Relative paths resolve from the project directory.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `file_path` | `string` | ✅ | — | 文件路径 |
| `content` | `string` | ✅ | — | 要写入的内容 |

### `append_file`

> Append content to the end of a file. Relative paths resolve from WORKING_DIR.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `file_path` | `string` | ✅ | — | 文件路径（基准是 **WORKING_DIR**） |
| `content` | `string` | ✅ | — | 追加内容 |

### `edit_file`

> Find-and-replace text in a file. All occurrences of old_text are replaced with new_text. Relative paths resolve from the project directory.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `file_path` | `string` | ✅ | — | 文件路径 |
| `old_text` | `string` | ✅ | — | **精确文本**（字面量，非正则） |
| `new_text` | `string` | ✅ | — | 替换文本 |

**使用**：**全部出现都替换**，不是首个匹配。想只改一处就必须把 `old_text` 写到足够唯一。

### `glob_search`

> Find files matching a glob pattern (e.g. `"*.py"`, `"**/*.json"`). Relative paths resolve from the project directory.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `pattern` | `string` | ✅ | — | glob 模式 |
| `path` | `string \| null` | ❌ | `null` | 搜索根目录；缺省为 project directory |

### `grep_search`

> Search file contents by pattern, recursively. Relative paths resolve from the project directory. Output format: `path:line_number: content`.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `pattern` | `string` | ✅ | — | 搜索串；`is_regex=true` 时按正则 |
| `path` | `string \| null` | ❌ | `null` | 文件或目录；缺省为 project directory |
| `is_regex` | `boolean` | ❌ | `false` | 是否按正则解释 `pattern` |
| `case_sensitive` | `boolean` | ❌ | `true` | 大小写敏感 |
| `context_lines` | `integer` | ❌ | `0` | 前后文行数（同 `grep -C`）；**上限 5** |
| `include_pattern` | `string \| null` | ❌ | `null` | 只搜**文件名**匹配该 glob 的文件（如 `"*.py"`） |
| `show_file` | `boolean` | ❌ | `true` | 每行是否带文件路径前缀 |

**使用**：`show_file=false` 时单文件输出 `line_number:> content`，多文件按文件分组、路径只打一次、组间 `---` 分隔。

---

## 2 · 命令执行（1）

### `execute_shell_command`

> Execute a shell command and return its output. Each call runs in a fresh subprocess — `cd`, `export`, `source`, etc. do NOT persist. Pass `cwd=` or chain in one call (`cd /repo && pytest`). Check the 'Default Shell' field to determine which shell is active… Do not use nohup or other commands to start long-running background processes.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `command` | `string` | ✅ | — | 要执行的 shell 命令 |
| `timeout` | `number` | ❌ | `60.0` | 最大执行秒数 |
| `cwd` | `string(path) \| null` | ❌ | `null` | 工作目录；缺省为 agent workspace |
| `sandbox_config` | `object \| null` | ❌ | `null` | 由治理策略编译出的沙箱配置（挂载权限 + 网络限制）；**schema 未公开** |

**使用**：每次调用是**全新子进程**，环境变量 / 工作目录都不继承；长驻进程禁止 `nohup`，不可避免时必须显式重定向 stdin/stdout/stderr。shell 语法随会话的 *Default Shell* 字段变（bash / PowerShell / cmd.exe）。

---

## 3 · 浏览器与媒体（6）

### `browser`

> Run Browser SDK code in the caller's session-scoped kernel.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `code` | `string` | ✅ | — | Browser SDK 代码 |

**与 v2 的关系**：这是 v1「远端 agent 自带浏览器」的入口，**不是** `PageDriver`。v2 用 `executor/browser/` 的 obscura 原语，两者不可混用。

### `web_fetch`

> Fetch content from a specified URL and return its contents in a readable format. Read-only; no side effects. Authentication not supported; binary content (media / PDFs) not supported. If it errors or returns empty, fall back to `execute_shell_command` with curl.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `url` | `string` | ✅ | — | 完整合法 URL；内容会转成可读文本 |

**使用**：需要登录态 → 直接 ERROR，**不绕过**（与红线一致）。静态资源 / 非网页 URL 走 `curl`。

### `web_search`

> Search the web for real-time information about any topic. Returns summarized information from search results and relevant URLs. Uses a free API with rate limits — on network/quota error, fall back to `execute_shell_command` with curl.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `search_term` | `string` | ✅ | — | 检索词；技术查询建议带版本号 / 日期 |

### `desktop_screenshot`

> Capture a screenshot of the entire desktop (all monitors) or a single window. Full-screen uses the mss library on all platforms. On macOS, `capture_window=True` uses the system screencapture tool.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `path` | `string` | ❌ | `""` | 保存路径；空 = 当前项目目录；PNG 输出需 `.png` 结尾 |
| `capture_window` | `boolean` | ❌ | `false` | 仅 macOS 生效（用户点选窗口）；Windows/Linux **忽略**，只能全屏 |

### `view_image`

> Load an image file into the LLM context so the model can see it. Accepts an HTTP(S) URL for online images — remote images are downloaded, validated, and frozen before entering the model context. When the model does not support multimodal, the image is still returned (for the user/frontend) plus a text hint; the downstream media-stripping pipeline removes the ImageBlock.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `image_path` | `string` | ✅ | — | 本地路径或 HTTP(S) URL |

### `view_video`

> Load a video file into the LLM context. Unlike remote images, an HTTP(S) video URL is passed directly to the model **without downloading or freezing**. Durable remote video handling is outside current scope.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `video_path` | `string` | ✅ | — | 本地路径或 HTTP(S) URL |

**与 `view_image` 的差别**（合成样本时易错）：远程图片会被下载+校验+冻结，**远程视频直接透传**，不进冻结层。

---

## 4 · 会话历史与记忆（2）

### `recall_history`

> Recall your recorded conversation history (raw turns) — structured. Read back verbatim turns from the durable log: this session's turns that scrolled out of context (seq spans come from the `[context compressed]` map) plus earlier sessions. Pick an op: `expand` / `search` / `recall_tool` / `days_between`.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `op` | `string` | ✅ | `expand` \| `search` \| `recall_tool` \| `days_between` | 操作类型（无默认值，必填） |
| `lo` | `integer \| string \| null` | ❌ | `null` | **expand only** — 区间首个正 seq；接受十进制字符串 |
| `hi` | `integer \| string \| null` | ❌ | `null` | **expand only** — 区间末个正 seq；同上 |
| `query` | `string \| null` | ❌ | `null` | **search only** — 关键词查询，支持 `OR`；**用关键词不要整句**，所有词都要命中 |
| `k` | `integer` | ❌ | `10` | **search only** — 最大命中数 |
| `kind` | `string \| null` | ❌ | `null` | **search only** — 行类型过滤：`model_turn` \| `tool_result` |
| `all_agents` | `boolean` | ❌ | `false` | **search only** — 跨所有 agent 历史 |
| `session_id` | `string \| null` | ❌ | `null` | **search only** — 钉住一个会话（如 `"cron:<job>"`）；优先级高于 `agent_id` |
| `agent_id` | `string \| null` | ❌ | `null` | **search only** — 钉住一个 agent 的历史 |
| `created_on` | `string \| null` | ❌ | `null` | **search only** — 精确 ISO 日期 `YYYY-MM-DD` |
| `created_from` | `string \| null` | ❌ | `null` | **search only** — 起始日期（含） |
| `created_to` | `string \| null` | ❌ | `null` | **search only** — 结束日期（含） |
| `tool_call_id` | `string \| null` | ❌ | `null` | **recall_tool only** — 要重读的 tool call |
| `start` | `string \| null` | ❌ | `null` | **days_between only** — 严格 ISO 起始日期/时间戳 |
| `end` | `string \| null` | ❌ | `null` | **days_between only** — 严格 ISO 结束日期/时间戳 |
| `inclusive` | `boolean` | ❌ | `false` | **days_between only** — 两端都计入（方向不变） |
| `cursor` | `string \| null` | ❌ | `null` | 上一页返回的不透明续页游标 |

**使用（这几条是行为契约，不是建议）**：
- `expand` 返回的是**显式页**，永不静默截断；续页必须把 `next_cursor` **原样**回传。
- **cursor 绑定原始参数与结果快照**——改 query / 范围 / 过滤 / k 都会失败。**同一 cursor 永不重试**。
- **当前进行中的 turn 永远不会命中**（它已经在模型眼前）。
- 空结果是**显式声明**的，代表该区间/查询确实没有内容，不是异常。
- `days_between` 返回 `end - start` 的有符号日历天差，**不分页**。
- 日期过滤用源消息的 `created_at`，可以配空 `query` 使用。
- 超大 tool 输出若被存到 DB 外，`search` 会返回带 `file_path` 与邻近命中行的结果。

### `memory_search`

> Search memory files semantically. Use before answering questions about prior work, decisions, dates, people, preferences, or todos. Returns top relevant snippets with file paths and line numbers. When a reranker is enabled, over-fetches (`max_results × candidate_multiplier`), reranks, caps back to `max_results`.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `query` | `string` | ✅ | — | 语义检索语句 |
| `max_results` | `integer` | ❌ | `5` | 返回条数上限 |
| `min_score` | `number` | ❌ | `0` | 相关性下限；**正常使用保持 0** |

**`min_score` 为什么不能随便调**：ReMe 检索可能把 BM25 与 fused 分数混在一起，两者尺度不同——抬高阈值会把**有效的关键词命中**一起滤掉。

---

## 5 · 多 agent 协作（5）

### `list_agents`

> List all configured agents from the QwenPaw service.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `base_url` | `string \| null` | ❌ | `null` | 服务地址；缺省用配置值（无 description） |

**使用**：`chat_with_agent` / `submit_to_agent` 的 `to_agent` **必须是本工具返回的 agent ID**。

### `chat_with_agent`

> Send a foreground message to another configured agent. Waits for the target agent to finish and returns the final text in a single tool result.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `to_agent` | `string` | ✅ | — | 目标 agent ID（来自 `list_agents`） |
| `text` | `string` | ✅ | — | 消息正文 |
| `session_id` | `string \| null` | ❌ | `null` | 续用已有会话；缺省自动生成新 ID |
| `timeout` | `integer` | ❌ | `300` | 前台等待秒数；调用方应预估目标耗时以少踩超时 |

### `submit_to_agent`

> Submit a background message to another configured agent. Returns immediately with task metadata instead of waiting.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `to_agent` | `string` | ✅ | — | 目标 agent ID |
| `text` | `string` | ✅ | — | 作为后台任务执行的消息正文 |
| `session_id` | `string \| null` | ❌ | `null` | 续用已有会话 |
| `task_timeout` | `number \| integer \| string \| null` | ❌ | `null` | 任务超时秒数；缺省由后端 `POST /console/chat/task` 取 `DEFAULT_STREAM_TASK_TIMEOUT_SECONDS`（3600）；给了必须为正 |

### `check_agent_task`

> Check the status of a background inter-agent task. Returns the lifecycle state while running, or the final response / failure message when done.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `task_id` | `string` | ✅ | — | `submit_to_agent` 返回的后台任务 ID |

### `spawn_subagent`

> Spawn an ephemeral subagent within the CURRENT workspace. The subagent starts a **fresh conversation context** — it does NOT inherit the parent's dialogue history, accumulated state, or in-progress tool calls. Shares the same agent identity, workspace, and by default the full tool/skill set.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `task` | `string` | ✅ | — | 子任务描述，成为子 agent 会话里**唯一的 user message**；用 `batch` 时**必须传空串** |
| `fork` | `boolean \| string \| integer` | ❌ | `false` | 两个独立特性：① 继承父会话持久化状态的副本；② 若项目是 git 仓库，子 agent 在独立 worktree 里工作。字符串布尔可接受，**歧义值 ERROR** |
| `background` | `boolean \| string \| integer` | ❌ | `false` | `true` → 立刻返回 `task_id`。子 agent 通常跑**分钟级**，**至少等 30s** 再首次 `check_agent_task`，之后 30–60s 一次。只起一个时优先前台 |
| `timeout` | `integer \| number \| string \| null` | ❌ | `null` | 秒。前台缺省 `DEFAULT_SPAWN_FOREGROUND_TIMEOUT_SECONDS`（600）；后台缺省由服务端 `DEFAULT_STREAM_TASK_TIMEOUT_SECONDS`（3600）。**batch 模式完全忽略** |
| `allowed_tools` | `array<string> \| string \| null` | ❌ | `null` | 工具白名单：`null` = 继承全量，`[]` = 全禁。JSON 数组字符串可接受。batch 模式忽略 |
| `skills` | `array<string> \| string \| null` | ❌ | `null` | Skill 白名单：`null` = 继承 workspace 全部。JSON 数组字符串可接受。batch 模式忽略 |
| `batch` | `array<object> \| string \| null` | ❌ | `null` | 批量模式的任务规格列表；每项**必须含 `task`**，可选 `fork` / `timeout` / `allowed_tools` / `skills`；顶层 `fork`/`timeout`/`allowed_tools`/`skills`/`background` 在此模式下被忽略。**上限 `MAX_SPAWN_BATCH_SIZE`=10，并发 `MAX_SPAWN_BATCH_CONCURRENCY`=3**；全部按后台任务跑 |

**与 `chat_with_agent` 的区别**：`chat_with_agent` 打的是**另一个已配置 agent**；`spawn_subagent` 是在**当前 agent 身份**下起一个一次性 worker。

---

## 6 · Skill 管理（2）

### `Skill`

> Retrieve a skill within the conversation. When users asks you to perform tasks, check if any of the available skills match.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `skill` | `string` | ✅ | — | **精确**的 skill 名（非模糊匹配） |

### `materialize_skill`

> Persist a confirmed skill proposal into the workspace. Runs format validation and the security scanner, writes `SKILL.md` plus the manifest entry, and enables the skill.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `name` | `string` | ✅ | — | 归一化后的 skill 目录名；`/make-skill` 流程中**必须等于 `plan.name`** |
| `description` | `string` | ✅ | — | SKILL.md frontmatter 的触发串（`Use this skill when …`）；**≤ ~200 字符**，要覆盖同义词/相邻说法以免未来 agent 不触发 |
| `body` | `string` | ✅ | — | SKILL.md 正文，**不含 frontmatter** |
| `extra_files` | `object<string,string> \| string \| null` | ❌ | `null` | 随 SKILL.md 一起落盘的附加文件。传 dict 或 JSON 对象字符串；key 是相对路径（如 `"scripts/batch.json"`），value 是文件内容。用于打包 skill 引用的 `run_tool_batch` JSON 或 helper 脚本 |

---

## 7 · 环境与会话（3）

### `get_current_time`

> Get the current time in format `%Y-%m-%d %H:%M:%S TZ (Day)`, e.g. `"2026-02-13 19:30:45 Asia/Shanghai (Friday)"`.

**无参数。** 用户问时间、或后续操作需要当前时间时调用。

### `set_user_timezone`

> Set the user timezone. Only call this tool when the user explicitly asks to change their timezone.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `timezone_name` | `string` | ✅ | IANA 名，如 `Asia/Shanghai` / `America/New_York` / `Europe/London` / `UTC` | 用户时区 |

### `get_token_usage`

> Query LLM token usage over the past N days.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `days` | `integer` | ❌ | `30` | 回溯天数 |
| `model_name` | `string \| null` | ❌ | `null` | 按模型名过滤 |
| `provider_id` | `string \| null` | ❌ | `null` | 按 provider 过滤 |

---

## 8 · 批处理与交付（2）

### `run_tool_batch`

> Execute a batch of tool calls from a JSON file. Load `actions` from a JSON batch file and execute them sequentially.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `actions` | `array<object> \| string \| null` | ❌ | `null` | 直接内联动作数组（也接受 JSON 字符串） |
| `file_path` | `string` | ❌ | `""` | JSON 批处理文件的**绝对路径** |
| `args` | `object \| string \| null` | ❌ | `null` | 替换批处理文件里 `${args.<name>}` 占位符的值 |
| `stop_on_error` | `boolean` | ❌ | `true` | 所有步骤的默认遇错即停行为 |
| `last_only` | `boolean` | ❌ | `false` | 只返回 `ok` + `last_step_result` + 去重后的末段文本与非文本块，省 token |
| `maxstep` | `integer` | ❌ | `500` | 应用控制流跳转后的**最大执行步数**，防死循环 |

**批处理文件结构**：`{"actions": [...]}` 或纯数组。每个动作：

| 键 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `tool_name` | `string` | ✅ | 已注册工具名，或内置控制流 `label` / `goto` / `set_var` |
| `arguments` | `object` | ❌ | 该工具的关键字参数 |
| `stop_on_error` | `bool` | ❌ | 覆盖本步的遇错行为 |
| `wait` | `float` | ❌ | 本步执行后睡眠秒数 |

**内置控制流**：

| `tool_name` | 参数 | 语义 |
|---|---|---|
| `label` | `name` (str, ✅) | 定义跳转目标标签 |
| `goto` | `label` (str, ✅)、`condition` (str, ❌) | 条件跳转。`condition` 省略/空 = 无条件；支持 `true`、`false`、`1>2`、`i<5`、`${vars.i}<${steps.0.value}` |
| `set_var` | `expr` (str, ✅) | 标量赋值 / 受限算术：`i=0`、`i=i+1`、`i=${vars.i}+1`、`i=(${vars.i}+1)*2`。**不是通用字符串模板赋值语言** |

**占位符（必须带花括号，以便在混合内容如 shell 命令里无歧义）**：
- `${args.<name>}` —— 运行时参数；**每个被引用的参数都必须提供**（文件里没有占位符时 `args` 可省略）
- `${steps.<index>.<path>}` —— 按**动作在 JSON 里的零基位置**引用，**不是执行次数**；循环回跳时同一动作仍是同一 `steps.<index>`，解析到它**最近一次**执行的结果
- `${vars.<name>}` —— `set_var` 创建的运行时变量

**使用**：`set_var` + `goto` + `label` 组合可做循环（遍历动态列表、条件重试、单轮协调多变量）。脚本稳定后设 `last_only=True` 省 token——为此应把有用输出汇总到文件，或在动作列表末尾放一个 summary 步。

### `send_file_to_user`

> Send a file to the user.

| 参数 | 类型 | 必填 | 默认/取值 | 说明 |
|---|---|---|---|---|
| `file_path` | `string` | ✅ | — | 要发送的文件路径 |

---

## 9 · 探索模式（1）

### `activate_f1_exploration_mode`

> Activate F1 exploration mode. Call this when an email cannot be classified by the triage tree (`MAIL_TRIAGE.md`) and you need to attempt handling it with per-tool user approval.

**无参数。**

**使用（上游写死的行为契约）**：激活后分两阶段——先 **ANALYZE**（从收件人视角给出简要计划），再 **ACT**（每个 tool call 前用一句话说明理由）。系统会自动拦截后续所有 tool call 并把理由+动作展示给用户审批，本轮内持续有效。**不要在 chat 输出里自己请求批准**；被拒时工具返回拒绝消息，换方案重试。

---

## 📊 28 个工具的落点对照

| 域 | 工具 | 典型合成场景 |
|---|---|---|
| 文件与工作区 | `read_file` `write_file` `append_file` `edit_file` `glob_search` `grep_search` | 改脚本 / 查配置 / 定位代码 |
| 命令执行 | `execute_shell_command` | 跑测试 / `curl` 兜底 / `ffprobe` |
| 浏览器与媒体 | `browser` `web_fetch` `web_search` `desktop_screenshot` `view_image` `view_video` | 抓页面 / 看截图 / 复核画面 |
| 会话历史与记忆 | `recall_history` `memory_search` | 回溯掉出上下文的 turn / 查历史决定 |
| 多 agent 协作 | `list_agents` `chat_with_agent` `submit_to_agent` `check_agent_task` `spawn_subagent` | 派子任务 / 后台长任务 |
| Skill 管理 | `Skill` `materialize_skill` | 取用 / 落盘 skill |
| 环境与会话 | `get_current_time` `set_user_timezone` `get_token_usage` | 时间锚定 / 用量查询 |
| 批处理与交付 | `run_tool_batch` `send_file_to_user` | 批量执行 + 交付产物 |
| 探索模式 | `activate_f1_exploration_mode` | triage 失效时的兜底流程 |

**一句话**：这 28 个是 **agent 侧**能力；[浏览器 MCP](#浏览器-mcp) 那 37 个是 **v2 执行层**能力。两套不要混写——v1 的 `browser`（Browser SDK 代码）与 v2 的 `PageDriver` 原语（`goto` / `snapshot` / `extract` …）没有任何对应关系。

---

# 训练侧工具声明（`toolcatalog`）

> 这一节回答一个具体问题：**上面这些工具，哪些进了训练数据里的
> `tools[]`？** 完整定义在
> [`trajectory_pipeline/toolcatalog/`](../toolcatalog/)——那是**唯一真源**，
> P2 的 `six.tools` / P3 的 `openai.tools` / T 的 `openai.tools` 三处都是
> 它的投影。

## 13 条的构成

| 组 | 条数 | 工具 | 性质 |
|---|---|---|---|
| **已用动作** | 4 | `goto` `click` `fill` `press` | 执行端真发得出，且有存档兜底 |
| **能力声明** | 7 | `web_search` `web_fetch` `view_image` `desktop_screenshot` `get_current_time` `read_file` `glob_search` | **本项目没有任何真实调用产生过它们**——模型被告知存在，仅此而已 |
| **基础设施动作** | 2 | `new_tab` `close_tab` | 执行器自己的会话管理。**留在 P1/P2 里**（删了无法回放），**不进模型可见集** |

**模型可见 = 11 条**（4 已用 + 7 能力声明）；`new_tab` / `close_tab` 不给模型看——
让模型看到一条它永远不该发、也不该学的 `tool_calls`，就是教它输出越权动作。

## 挑选准则

能力声明那 7 条不是随手挑的，三条准则同时满足：

1. **语义与「帮用户找在线观看资源」相邻**——教模型去找资源，不是去做代码任务；
2. **v2 概念上可实现**（不是「v1 有所以抄过来」）；
3. **不碰红线、不碰文件系统写入、不碰多 agent 协作**。

第 3 条排掉了一批：`write_file` / `append_file` / `edit_file` / `run_tool_batch` /
`materialize_skill` / `spawn_subagent` / `submit_to_agent` / `chat_with_agent` /
`list_agents` / `check_agent_task` / `execute_shell_command`。

⚠️ **`execute_shell_command` 是刻意排除的**——它与 `read_file` / `glob_search`
二选一。留 shell 的话，模型会把「找资源」变成「写脚本跑 curl」；而 `read_file` +
`glob_search` 覆盖了「看本地有什么」的需求，不引入代码执行。

## 红线改写规则

上游原文**不能**直接抄进训练数据。三类改写：

| 上游原文 | 改写成 | 为什么 |
|---|---|---|
| `click(ref=...)` / `snapshot()` 返回的句柄 | 语义化的 **`{role, name}`**（D12：原为 `{tag, label}`） | 句柄是**会话内**有效、跨会话失效的随机数，进了训练数据等于让模型学一个随机数（CLAUDE.md 红线） |
| `browser_navigate` / `browser_click` / MCP 协议名 | `goto` / `click` + 行为契约 | 模型要学的是「遇到这样的页面该怎么办」，不是「怎么驱动某个浏览器」 |
| `web_search` 兜底「退到 `execute_shell_command` 跑 curl」 | 「失败就如实说搜不了」 | 本工具集**不含 shell**——这是**能力集变更导致的改写**，不是改语义 |
| Cookie / Profile / Authorization / `get_storage_state` | 不出现 | 永久禁（见[红线工具](#-红线工具任何浏览器-mcp-实现永久禁)） |
| v1 的 `browser`（Browser SDK 代码） | 不出现 | 与 v2 的 `PageDriver` 原语没有任何对应关系，**两套不混写** |

**说明文字本身是行为契约**：写清「什么时候会失败」「失败了该怎么办」。
旧口径的说明只有「打开一个网址。」——模型据此不知道 navigate 失败时该换路
还是该重试，于是学出「重复点同一个」这类动作。

### ★ D12：目标编码从 `{tag, label}` 改成 `{role, name}`

**改的是「模型学到的目标编码」这一层，不是「上游原文怎么描述元素」这一层。**

| | 旧口径（obscura 时代） | 新口径（D12） |
|---|---|---|
| 目标编码 | `{tag: "button", label: "播放"}` | `{role: "button", name: "播放"}` |
| 落到 API | `page.locator("button:has-text('播放')")` | `getByRole(role, {name})`（camoufox 面是 `get_by_role(role, name=name, exact=True)`） |
| 能进裸执行层执行 | ❌ 无对应 API | ✅ 原生 |
| `<input>` 怎么表达 | tag 恒为 `input`，label 取 placeholder | role `textbox`/`searchbox`，name 取 placeholder |
| 图标按钮 | 只能给空 label | name 取 `alt` |

**为什么必改**：`{tag,label}` 是本项目自造的语义形态，
模型学到后在裸执行层环境里**发不出**这个动作——轨迹数据脱离本项目不成立
（[设计方案 04 §0.1](设计方案/04-camoufox执行层替换方案.md)）。

**「不提实现名」这条红线要改口径**（P4）：
禁的是**前任执行层**（obscura / MCP / 句柄 / isolate），**当前执行层 camofox
要点名**——「按 role 与 name 定位」是模型必须会用的语义，不是实现细节。
⚠️ 但 **camofox 面不许写「精确匹配」**：上游是 `getByRole(role, {name}).nth(nth)`，
**没有 `exact`** ⇒ 名字走**大小写不敏感子串匹配**；写「精确」就是教模型一个
引擎不保证的性质，而它在报告上看不出异常。**唯一性由本项目在枚举期保证**
（见[camofox-browser 章 · 目标编码](#目标编码role--namecamofox-面)）。
改完要给契约测试加一条**正向断言**（camofox 至少出现一次），
否则哪天有人「顺手改回抽象措辞」没人拦。

## 参数 `required` 按真实存档统计

`required` 会进训练数据的 function 定义，模型会学着必填。所以它是**数据的
口径**而不是**设计的愿望**：

| 工具 | 参数 | 出现率 | `required` |
|---|---|---|---|
| `goto` | `url` | 377/377 | ✅ |
| `click` | `tag` / `label` | 91/91 | ✅ |
| `fill` | `text` / `tag` / `label` | 5/5 | ✅ |
| `press` | `key` | 20/20 | ✅ |
| `press` | `tag` / `label` | 6/20（30%） | ❌ 全局按键，不指向元素 |

「出现率」按**参数键是否在场**算，不按值是否非空算（`click` 的 `label` 键
91 次都在，其中 1 次值是空串——那个按钮没有可见文字）。另有护栏：required
参数的非空值比例须 ≥ 90%。

> ⚠️ **旧口径的 `click` 是空参数**（`properties: {} / required: []`）——
> 而真实调用里是 `{"tag": ..., "label": ...}`。声明与调用对不上，训出来的
> 模型会发空参数的点击。

> ⚠️ **上表是 obscura 时代的存档统计，D12 之后必须重算**：
> 参数名从 `tag`/`label` 变成 `role`/`name`，出现率会跟着变
> （`name` 的空串率、`press` 有无目标的分布都会改）。
> **旧批次与新批次不许并池**——两套编码混在一份存档里，
> `required` 的统计就失去意义（CLAUDE.md「并池是写入时的动作」）。

---

## 相关文档

- [01-架构总览 §D7 执行层](01-架构总览.md)——为什么用 obscura、放弃 Playwright MCP + Camoufox 的决定
- [01-架构总览 §PageDriver 协议](01-架构总览.md)——浏览器原语稳定契约
- [01-架构总览 §ref / Prompt 红线](01-架构总览.md)——ref 不入训练数据、提示词不提浏览器/MCP/ref
- [04-运行手册](04-运行手册.md)——`cli check` / `cli probe` / 环境变量全表
- [设计方案 00-总体方案 §3.2](设计方案/00-总体方案.md)——PageDriver 原语 ↔ 浏览器 tool 映射的设计基线
- [避坑指南 §5.11–§5.13](02-避坑指南.md)——`browser_links` 面包屑陷阱 / `browser_extract` 键名陷阱 / `--title` 缺省陷阱
