# camofox-browser 使用说明

面向 AI Agent 的反检测无头浏览器服务器 camofox-browser 的安装、启动、MCP 接入与工具使用说明。

> 📌 **本文是「这个依赖怎么装怎么配怎么验」的手册**（上游视角）。
> **本项目怎么用它**（执行层接线、口径、落差清单）见
> [设计方案 21 · camofox-browser 执行层接入方案](../设计方案/21-camofox-browser执行层接入方案.md)；
> **协议/入参/返回**那一层见 [tools.md](../tools.md)。
> **分工与 [camoufox_弃用.md](camoufox_弃用.md) 的差别**：那一份是**已退役依赖**的手册。
>
> ⚠️ **两条纪律**（owner 裁定，2026-10-26）：
> ① **不要改上游源码**（`C:\Users\klpc\workspace\tool\camofox-browser`）——
>    需要新能力就在**本项目的驱动侧**做（例：`press` 靠**驱动缓存合并**，不改上游）；
> ② **本仓库只跟它说 MCP**——`playwright` 已从主依赖移除；
>    **浏览器内核仍是 camoufox**（上游依赖 `camoufox-js`），但那是**上游的依赖**，不是本仓库的。

## 目录

- [1 环境要求](#1-环境要求)
- [2 安装与首次启动](#2-安装与首次启动)
  - [2.1 从源码安装](#21-从源码安装)
  - [2.2 通过 npm 运行](#22-通过-npm-运行)
  - [2.3 Docker 部署](#23-docker-部署)
  - [2.4 Windows 下的构建脚本](#24-windows-下的构建脚本)
  - [2.5 使用外部 Camoufox 可执行文件](#25-使用外部-camoufox-可执行文件)
- [3 启动 REST 服务与健康检查](#3-启动-rest-服务与健康检查)
- [4 MCP 接入](#4-mcp-接入)
  - [4.1 安装 camofox-browser-mcp](#41-安装-camofox-browser-mcp)
  - [4.2 在宿主中注册](#42-在宿主中注册)
  - [4.3 验证接入](#43-验证接入)
  - [4.4 MCP 环境变量](#44-mcp-环境变量)
  - [4.5 本机探测验证记录](#45-本机探测验证记录)
- [5 工具参考](#5-工具参考)
  - [5.1 工具总览](#51-工具总览)
  - [5.2 关键参数说明](#52-关键参数说明)
  - [5.3 搜索宏](#53-搜索宏)
- [6 典型使用流程](#6-典型使用流程)
- [7 常见问题排查](#7-常见问题排查)

---

# 1 环境要求

- 运行时：Node.js 22 或更高版本（`package.json` 的 engines 要求明确为 `>=22`）。本机实测运行于 Node v22.22.3，可正常运行。
- 磁盘与网络：首次执行 `npm install` 会下载 Camoufox 浏览器内核，体积约 300MB，需要可访问官方 GitHub releases 的网络环境。
- 可选依赖 yt-dlp：用于 YouTube 字幕提取的快路径。未安装时会回退到较慢的浏览器拦截方案，仅影响字幕相关能力。
- 容器与部署（可选）：Docker、Fly.io、Railway 任一即可，本地开发无需容器。
- 端口：默认占用 9377，需确保该端口空闲。

# 2 安装与首次启动

## 2.1 从源码安装

```bash
git clone https://github.com/jo-inc/camofox-browser && cd camofox-browser
npm install   # 首次运行会下载 Camoufox（约 300MB）
npm start     # 启动后访问 http://localhost:9377
```
本机使用此方式，项目位置C:\Users\klpc\workspace\tool\camofox-browser

安装脚本会在自身执行前临时取消 `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD` 环境变量，避免因为外部导出了该变量而静默跳过内核下载并在运行时崩溃。

## 2.2 通过 npm 运行

```bash
npx @askjo/camofox-browser
```

## 2.3 Docker 部署

仓库自带 `Makefile`，会自动识别 CPU 架构，并在 Docker 构建之前预先下载 Camoufox 与 yt-dlp，使重建从约 3 分钟缩短到约 30 秒。

```bash
make up      # 构建并启动（M1/M2 自动用 aarch64，Intel 用 x86_64）
make down    # 停止并删除容器
make reset   # 强制干净重建
make fetch   # 只下载二进制
```

不要直接执行 `docker build`。Dockerfile 通过 bind mount 从 `dist/` 取预下载的二进制，必须先用 `make up`（或 `make fetch` 后再 `make build`）完成下载。

## 2.4 Windows 下的构建脚本

Windows 没有 `make`，改用仓库自带的 PowerShell 脚本：

```powershell
.\build.ps1 up       # 构建并启动
.\build.ps1 down     # 停止并删除
.\build.ps1 build    # 只构建镜像
.\build.ps1 reset    # 强制干净重建
.\build.ps1 fetch    # 只下载二进制
.\build.ps1 up -Arch x86_64
```

推荐 PowerShell 7 及以上（pwsh），Windows PowerShell 5.1 也可用，同时需要开启 WSL2 后端的 Docker Desktop。若克隆后出现 `sh: not found` 或 `set: Illegal option -` 报错，说明 `.sh` 脚本行尾不是 LF，可按仓库 README 提供的一行命令把脚本转换为 LF 行尾；仓库的 `.gitattributes` 已强制 `.sh` 使用 LF，后续克隆会自动处理。

## 2.5 使用外部 Camoufox 可执行文件

在 `npm install` 之前以及启动服务时设置 `CAMOUFOX_EXECUTABLE`，即可跳过内置下载并直接启动指定的可执行文件，适用于 NixOS 等路径场景；兼容别名是 `CAMOUFOX_EXECUTABLE_PATH` 与 `CAMOFOX_EXECUTABLE_PATH`。该可执行文件必须来自一个完整包含 `properties.json`、`version.json` 与 `fontconfig/` 的 Camoufox 发行包。

确需脱离网络或自管二进制时，优先使用 `CAMOUFOX_EXECUTABLE`；否则可用 `npm install --ignore-scripts`（对所有依赖跳过生命周期脚本，最粗暴），或更精细地使用 `npm install --omit=optional` 加一条手动 `npx camoufox-js fetch`。

# 3 启动 REST 服务与健康检查

REST 服务默认监听 9377 端口，可通过 `CAMOFOX_PORT` 或 `PORT` 覆盖。服务就绪后可做以下检查：

```bash
curl -s http://127.0.0.1:9377/health
# {"ok":true,"engine":"camoufox","browserConnected":true,"browserRunning":true,...}
```

浏览器为懒启动：服务刚启动时 `browserConnected` 为 false，首次创建标签页后变为 true。健康检查响应不含任何密钥信息。

常用端点概览如下。

| 方法与路径 | 说明 |
| --- | --- |
| POST /tabs | 创建标签页并打开初始 URL |
| GET /tabs?userId=X | 列出该用户打开的标签页 |
| GET /tabs/:id/snapshot | 获取无障碍快照，支持 includeScreenshot 与 offset 参数 |
| POST /tabs/:id/click | 按元素引用或 CSS 选择器点击 |
| POST /tabs/:id/type | 向元素输入文本 |
| POST /tabs/:id/navigate | 跳转到 URL 或使用搜索宏 |
| POST /tabs/:id/evaluate | 在页面上下文执行 JavaScript |
| GET /tabs/:id/screenshot | 截取页面截图 |
| DELETE /tabs/:id | 关闭标签页 |
| GET /health | 健康检查 |
| GET /openapi.json | OpenAPI 规范 |
| GET /docs | 交互式 API 文档 |

# 4 MCP 接入

MCP 适配器是一个基于 stdio 的独立服务器，支持 Claude Code、Codex CLI、agy、Cursor、opencode 等任何兼容 MCP 的宿主。它只做转发，不启动浏览器，因此接入 MCP 不需要处于 camofox-browser 源码目录内。

## 4.1 安装 camofox-browser-mcp

先让 `camofox-browser-mcp` 命令可在任意目录解析，四种方式任选其一：

```bash
# 方式 A：npm link（有源码目录，能带上本地改动）
cd camofox-browser && npm link

# 方式 B：全局安装（无需源码目录）
npm install -g @askjo/camofox-browser

# 方式 C：npx（不安装，直接运行已发布的独立适配器）
npx -y @askjo/camofox-browser-mcp

# 方式 D：只装 MCP 适配器（最轻量，不引入核心服务依赖）
git clone https://github.com/jo-inc/camofox-browser && cd camofox-browser/mcp
npm install   # 只安装 @modelcontextprotocol/sdk（约 24MB，无 postinstall）
npm link
```

方式 D 不安装 camoufox-js、playwright-core、express 与约 300MB 的浏览器内核，适合 REST 服务已在本地或远端运行、只需要 stdio 适配器的场景。

## 4.2 在宿主中注册

各宿主使用同一个可执行文件和相同的 MCP 协议，区别只在配置文件。以下是常用宿主的配置片段。

Claude Code（命令行方式）：

```bash
claude mcp add camofox-browser -s user -- camofox-browser-mcp
```

Claude Code（配置文件 `~/.claude.json` 或项目内 `.mcp.json`）：

```json
{
  "mcpServers": {
    "camofox-browser": {
      "command": "camofox-browser-mcp"
    }
  }
}
```

Cursor（全局 `~/.cursor/mcp.json` 或项目内 `.cursor/mcp.json`）：

```json
{
  "mcpServers": {
    "camofox-browser": {
      "command": "camofox-browser-mcp"
    }
  }
}
```

Codex CLI（`~/.codex/config.toml`）：

```toml
[mcp_servers.camofox-browser]
command = "camofox-browser-mcp"
env = { CAMOFOX_BASE_URL = "http://localhost:9377" }
```

opencode（项目根 `opencode.json` 或全局 `~/.config/opencode/opencode.json`）：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "camofox-browser": {
      "type": "local",
      "command": ["camofox-browser-mcp"]
    }
  }
}
```

agy / Antigravity（`~/.gemini/config/mcp_config.json` 或工作区 `.agents/mcp_config.json`）采用与 Cursor 相同的 `mcpServers` 结构。

需要 Cookie 导入或指向非默认 REST 地址时，追加环境变量。以 Claude Code 为例：

```bash
claude mcp add camofox-browser -s user \
  --env CAMOFOX_BASE_URL=http://localhost:9377 \
  --env CAMOFOX_API_KEY=<key> \
  -- camofox-browser-mcp
```

若无法解析可执行文件，可用 `node /绝对路径/mcp/server.mjs` 作为回退，但这种写法依赖路径，离开源码目录会失效，应优先使用 `camofox-browser-mcp` 命令。

## 4.3 验证接入

| 宿主 | 验证方式 |
| --- | --- |
| Claude Code | 执行 /mcp，看到 camofox-browser 显示已连接 |
| Cursor | 设置中的 MCP 面板显示绿色状态 |
| opencode | 执行 opencode mcp list |
| Codex CLI | 启动 codex 后查看 MCP 服务器列表 |
| agy | 在 agy 命令行打开 /mcp 浮层查看 |

正常接入后应能看到 11 个工具：`camofox_create_tab`、`camofox_snapshot`、`camofox_click`、`camofox_type`、`camofox_navigate`、`camofox_scroll`、`camofox_screenshot`、`camofox_evaluate`、`camofox_list_tabs`、`camofox_close_tab`、`camofox_import_cookies`。

## 4.4 MCP 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| CAMOFOX_BASE_URL | http://localhost:9377 | REST 服务地址 |
| CAMOFOX_USER_ID | mcp-随机值 | 会话隔离标识，一个 MCP 服务器对应一个 camofox 会话 |
| CAMOFOX_SESSION_KEY | default | 用户内部的标签页分区 |
| CAMOFOX_ACCESS_KEY | 无 | REST 服务全局鉴权令牌，设置后自动以 Bearer 头转发到每次调用，须与服务端一致 |
| CAMOFOX_API_KEY | 无 | Cookie 导入用的密钥，仅影响 camofox_import_cookies |

当 REST 服务设置了 `CAMOFOX_ACCESS_KEY` 时，MCP 侧必须配置同名值，否则除 Cookie 导入外的所有工具调用都会被拒绝。

## 4.5 本机探测验证记录

未接入宿主时，可用仓库自带脚本直接驱动 MCP 适配器，复核 MCP 功能是否可用：

```bash
# 单次工具调用
node scripts/mcp-call.mjs camofox_create_tab '{"url":"https://example.com"}'

# 在单个 MCP 进程内按顺序执行多个调用，脚本会自动把上一轮结果注入下一轮所需的参数（如 tabId）
node scripts/mcp-run.mjs '[["camofox_create_tab",{"url":"https://example.com"}],["camofox_snapshot",{}]]'
```

本次探测链路为 MCP stdio 适配器 → REST 服务（9377）→ Camoufox 浏览器，结果如下。

| 探测项 | 结果 |
| --- | --- |
| stdio 握手 initialize | 正常，返回 serverInfo camofox-browser / 1.18.1，protocolVersion 2025-11-25（2026-10-27 复核） |
| tools/list | 返回 11 个工具，与契约 mcp/lib/tool-contracts.mjs 数量一致 |
| camofox_create_tab | 通过，新建标签页并返回 tabId |
| camofox_snapshot | 通过，返回无障碍快照与元素引用 |
| camofox_evaluate | 通过，在页面上下文执行 JavaScript 并返回结果 |
| camofox_screenshot | 通过，成功截取页面截图 |
| camofox_list_tabs | 通过，正确列出已打开的标签页 |
| camofox_close_tab | 通过，关闭后 health 中 activeTabs 归零 |

探测结束后测试标签页已清理，health 显示 activeTabs 为 0。其余工具（camofox_click、camofox_type、camofox_navigate、camofox_scroll、camofox_import_cookies）本次未单独发起调用，但它们与上述工具共用同一份契约 mcp/lib/tool-contracts.mjs，行为由该契约统一约束。

# 5 工具参考

## 5.1 工具总览

11 个工具的名称、用途与参数如下表，与契约文件 `mcp/lib/tool-contracts.mjs` 一致。

| 工具 | 用途 | 必填参数 | 可选参数 |
| --- | --- | --- | --- |
| camofox_create_tab | 新建标签页并打开初始 URL，返回 tabId | url | 无 |
| camofox_snapshot | 获取无障碍快照与元素引用，并附带截图 | tabId | offset |
| camofox_click | 按元素引用或 CSS 选择器点击元素 | tabId | ref、selector |
| camofox_type | 向指定元素输入文本 | tabId、text | ref、selector、pressEnter |
| camofox_navigate | 跳转到 URL，或使用搜索宏检索 | tabId | url、macro、query |
| camofox_scroll | 按方向滚动页面 | tabId、direction | amount |
| camofox_screenshot | 单独截取页面截图 | tabId | 无 |
| camofox_close_tab | 关闭标签页 | tabId | 无 |
| camofox_evaluate | 在页面上下文执行 JavaScript 并返回结果 | tabId、expression | 无 |
| camofox_list_tabs | 列出当前会话打开的全部标签页 | 无 | 无 |
| camofox_import_cookies | 导入 Netscape 格式 cookie 文件 | cookiesPath | domainSuffix |

## 5.2 关键参数说明

- 元素引用：`camofox_snapshot` 返回的 e1、e2 一类引用优于 CSS 选择器。选择器若命中多个元素会返回 422 strict mode violation，此时应重新快照并按引用点击。
- 快照分页：页面过长时快照会被截断，但保留分页信息。若返回 hasMore 为 true 并带有 nextOffset，用该偏移再次调用 `camofox_snapshot` 以读取后续内容。
- 输入回车：`camofox_type` 的 pressEnter 为 true 时，输入完成后自动按回车。
- 导航宏：`camofox_navigate` 的 macro 为枚举值，取值见搜索宏列表；配 query 给出检索词。使用宏时无需再传 url。
- 滚动：`camofox_scroll` 的 direction 取值为 up、down、left、right，amount 为像素数。在懒加载或虚拟滚动页面该工具可能无效，建议改用 `camofox_evaluate` 配合 window.scrollTo。
- 执行脚本：`camofox_evaluate` 的 expression 在页面上下文求值并返回结果，适合抽取数据、读取页面状态或调用页面 API；请求体上限由 CAMOFOX_EVALUATE_MAX_BODY_SIZE 控制，默认 1mb。
  ⚠️ **expression 必须「求得出值」**——上游把它交给 Playwright 的
  `page.evaluate(expression)`（上游 `server.js` 的 evaluate 路由），
  而 **Playwright 收到字符串时按「表达式」求值**（不是当函数体）。
  ⇒ **写成一个没被调用的函数字面量（`() => …` / `function …`）会得到
  一个函数对象**，序列化后是 `undefined` ⇒ 响应里**没有 `result` 键**
  ⇒ 调用方读到的值是 `None`/`undefined`（**「没测到」**）。
  - ✅ 要写成**立即调用的 IIFE**：
    `(function(){return String(document.title);})()`
  - ❌ 不要写成 `() => String(document.title)`（**求出来是函数对象，不是标题**）
  - ⚠️ 这个失败模式**没有报错**：上游照常返回 `{"ok": true, ...}`，
    只是 `result` 不存在 ⇒ **调用方必须区分「求出了 undefined」与「没求到」**
    （`result` 键不存在 = 没测到；`result` 存在且为 `null` = 页面里就是 null）。
- Cookie 导入：cookiesPath 是相对于服务端 cookie 目录的相对路径，越界路径会被拒绝；domainSuffix 用于只导入指定域名后缀的 cookie。

## 5.3 搜索宏

MCP 的 `camofox_navigate` 工具支持以下宏：

@google_search、@youtube_search、@amazon_search、@reddit_search、@wikipedia_search、@twitter_search、@yelp_search、@spotify_search、@netflix_search、@linkedin_search、@instagram_search、@tiktok_search、@twitch_search

其中 Reddit 宏会直接返回 JSON，无需解析 HTML：`@reddit_search` 检索整个 Reddit 并返回 25 条结果。REST 侧文档另列出 `@reddit_subreddit`（浏览指定子版块，例如查询 programming 对应 /r/programming.json），但该宏未包含在 MCP 工具的枚举值中，使用 MCP 时请以工具枚举范围为准。

# 6 典型使用流程

所有交互遵循同一节奏——先快照再操作：

1. 调用 `camofox_create_tab`，传入 url，拿到 tabId。
2. 调用 `camofox_snapshot`，拿到带元素引用的页面内容。
3. 用引用调用 `camofox_click` 或 `camofox_type` 完成交互。
4. 再次调用 `camofox_snapshot` 读取新状态，必要时重复第 3 至第 4 步。
5. 结束后调用 `camofox_close_tab` 关闭标签页。

以命令行直接驱动 REST 服务为例：

```bash
# 创建标签页
curl -X POST http://localhost:9377/tabs \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent1","sessionKey":"task1","url":"https://example.com"}'

# 取快照
curl "http://localhost:9377/tabs/TAB_ID/snapshot?userId=agent1"

# 按引用点击
curl -X POST http://localhost:9377/tabs/TAB_ID/click \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent1","ref":"e1"}'

# 使用搜索宏导航
curl -X POST http://localhost:9377/tabs/TAB_ID/navigate \
  -H 'Content-Type: application/json' \
  -d '{"userId":"agent1","macro":"@google_search","query":"best coffee beans"}'
```

# 7 常见问题排查

| 现象 | 原因与处理 |
| --- | --- |
| 出现 503 session_expired 或 tab create timed out | 服务端浏览器会话已失效，通常由此前一次失败调用引起；重启 npm start |
| camofox_scroll 返回 ok 但页面不滚动 | 懒加载或虚拟滚动页面的正常现象，服务端滚轮事件在此无效；改用 camofox_evaluate 配合 window.scrollTo 或 scrollBy |
| 点击返回 422 strict mode violation | CSS 选择器命中多个元素；重新快照后按元素引用点击 |
| camofox_import_cookies 返回 403 | 服务端与 MCP 侧的 CAMOFOX_API_KEY 不一致，或访问远端服务但未配置该密钥 |
| 所有工具调用返回 401 或 403 | 服务端设置了 CAMOFOX_ACCESS_KEY；在 MCP 侧设置同一密钥，之后会自动以 Bearer 头转发 |
| Cookie 导入始终不可用 | 默认关闭；需在服务端与宿主同时设置 CAMOFOX_API_KEY |
| 端口不是预期的 9377 | 仓库 run.sh 中默认端口写的是 3000，与实际不符；以服务端环境变量 CAMOFOX_PORT 或 PORT 为准 |
| **evaluate 返回里没有 `result` 键**（读到的值恒为 `undefined`） | expression 被当**表达式**求值，而写成了**没被调用的函数字面量**（`() => …`）⇒ 求出来是函数对象 ⇒ 序列化后是 `undefined`。改写成**立即调用的 IIFE**（`(function(){return …;})()`）。**不报错**——上游照常 `{"ok":true}` |
| 排查时版本号看起来不一致 | 仓库根 camofox.config.json 中的 version 写为 1.6.0，而实际包版本为 1.18.1；该字段与包版本无关，勿被误导 |
