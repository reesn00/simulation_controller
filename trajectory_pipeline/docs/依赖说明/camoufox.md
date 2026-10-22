# Camoufox 使用说明

> **定位**：依赖使用手册 + 本机实测记录。**架构决策在别处**——v2 生产链的执行层**自 2026-10-22 owner 裁定起就是 camoufox**（`DEFAULT_KIND = "camoufox"`，见 [设计方案 04 §6.1 覆盖块](../设计方案/04-camoufox执行层替换方案.md) 与[编年史 增量七十九](../计划进展/90-增量编年史.md)）；本文只记「这个依赖怎么装、怎么配、怎么验」，**不重复**决策本身。本文不改变控制流，也不提供新的驱动入口——驱动入口是 [`executor/browser/driver_factory.py`](../../executor/browser/driver_factory.py) 的 `make_driver`。**obscura 没被删**：它是存量 P1/P2 产物的方言（`obscura-1` 面**只读不再产出**）与口径回溯参照系。
> **适用版本**：camoufox `0.5.8` · playwright `1.62.0` · 浏览器 `v156.0.1-beta.36`（2026-10-20 本机实测）
> **写作口径**：凡是标「实测」的，都是在本机跑出来、能重跑验证的；凡是标「源码」的，都以 `.venv/Lib/site-packages/camoufox/` 里这份 0.5.8 为准，**不以线上文档为准**（版本漂移会改行为）。
> 交叉：[tools.md](../tools.md)（工具调用总目录）· [04-运行手册](../04-运行手册.md)（命令与环境变量）· [02-避坑指南](../02-避坑指南.md)

---

## 0. 结论速查

| 事项 | 结论 |
|---|---|
| 装浏览器 | `camoufox fetch`（**自动**带 uBO 插件、fpgen 模型、浏览器本体，共约 2.5 GB；**GeoIP 库只在装了 `camoufox[geoip]` 时才下载**——见 §1 注 3） |
| 启动服务 | `camoufox server` —— **没有任何 stealth 参数可传**，要配就绕开 CLI（见 §3.2） |
| 客户端连接 | `playwright.firefox.connect(ws)`，**不是** `connect_over_cdp` |
| 客户端必补 | `new_context(no_viewport=True)`，否则 viewport 恒为 1280×720 且 `dpr` 被打成 1（实测，见 §6.2） |
| 离线核对配置 | 指纹在 `env["CAMOU_CONFIG_1..N"]` **分片 JSON** 里，**不在** `firefox_user_prefs`、**不在**返回顶层（见 §4.3） |
| 必开的 stealth 开关 | `humanize=True`（默认**关**）· `os="windows"`（默认**随机**三选一）· `geoip=True`（默认**关**，且**当前 venv 缺 `maxminddb` 会直接抛异常**） |
| 最容易被忽略的自洽点 | **时区与 `navigator.languages` 必须同区**；默认配置不保证（实测，见 §6.1） |
| 三个已踩坑 | ① 默认配置 locale 与时区打架 ② server 模式 viewport/dpr 被写死 ③ `debug=True` 在 GBK 控制台必崩 |

---

## 1. 本机安装状态

`camoufox version` 实测输出（2026-10-20）：

```
Python Packages
  Camoufox      v0.5.8        fpgen   v1.3.0        Playwright  v1.62.0
Browser
  Active              official/stable
  Current browser     v156.0.1-beta.36
  Build date          Oct 6
  SHA256              096fd4832a7a
  Installed           Yes
GeoIP
  Database            GeoIP AIO by daijro        Updated  2026-10-20 09:33
Storage
  Install path        C:\Users\klpc\AppData\Local\camoufox\camoufox\Cache
  Browser(s) size     2.4 GB                     GeoIP size  115.4 MB
```

**四条与使用相关的推论**：

1. `Install path` 是**用户级缓存**，不在仓库里、也不在 `.venv` 里 → 换仓库/重建 venv 不用重下浏览器；反之删 `.venv` 也不影响它。
2. 浏览器版本是 `156.0.1-beta.36`，**而 UA 会自报 `Firefox/156.0`**——这个版本号由 `resolve_verstr()` 从实际二进制读出来（`camoufox/utils.py`），所以不要用 `ff_version` 去「对齐」它，那只会制造不一致（`warnings.yml::ff_version`）。
3. ⚠️ **GeoIP 有两层，缺一不可**（2026-10-20 复核，推翻此前「装好即可用」的说法）：
   - **数据文件**：本机**已装**（`Cache/geoip/mmdb/geoip aio by daijro-combined.mmdb`，115.4 MB，`camoufox version` 显示 Updated 2026-10-20 09:33）。
   - **Python 读取器 `maxminddb`**：**当前 venv 未安装** → `geolocation.py` 顶部 `try: import maxminddb / except ImportError: ALLOW_GEOIP = False` → `ALLOW_GEOIP=False`。
   - 后果：`geoip=True` 会在 `launch_options` 里 `geoip_allowed()` 处**直接抛 `NotInstalledGeoIPExtra`**，**根本走不到查库那步**。
     查库也要 `maxminddb`（`geolocation.py::get_geolocation` 里是无条件 `import maxminddb`）。
   - 且 `camoufox fetch` 的 GeoIP 下载本身是**有条件的**（`__main__.py`：`if ALLOW_GEOIP: download_mmdb()`）——extra 没装时 fetch **静默跳过**，不报错。
   - **要开 geoip 的完整步骤**：`uv pip install "camoufox[geoip]"`（装 `maxminddb`）→ 数据文件本机已有，或 `camoufox set --geoip` 选源后 `camoufox fetch` 补下。
   - 另注：`geoip=True` 会**外发一次公网 IP 探测请求**（`ip.py::public_ip`），且查不到 IP 会抛 `UnknownIPLocation`。
4. PowerShell 里的 `(useramulation)` **就是本仓库的 `.venv`**（`pyvenv.cfg` 里 `prompt = useramulation`）→ CLI 与 `python` API 用的是同一份 camoufox，不会出现「命令行能跑、代码里找不到」。

---

## 2. CLI 命令全表

`camoufox --help` 列出的命令，逐条落到「它到底干什么 / 会不会联网 / 会不会删东西」：

| 命令 | 作用 | 联网 | 破坏性 | 备注 |
|---|---|---|---|---|
| `camoufox fetch [VERSION]` | 安装当前 active 版本（或指定版本） | 是 | 覆盖旧数据 | 先 `Cleaning old data...`；顺带装 uBO / fpgen 模型；**GeoIP 库仅在 `maxminddb` 已装时才下**（`if ALLOW_GEOIP: download_mmdb()`，否则静默跳过） |
| `camoufox set` | 交互式选版本 + 设置 → 并 fetch | 是 | 无 | 非交互写法：`camoufox set official/stable`（取该通道最新）、`camoufox set official/stable/134.0.2-beta.20`（钉死版本） |
| `camoufox set --geoip` | 交互式选 GeoIP 数据源 | 是 | 无 | **只能交互**，没有「传名字」的写法 |
| `camoufox set --release` | 丢掉显式版本选择，回到本 release 配对的浏览器 | 是 | 无 | 排查「版本对不上」时用 |
| `camoufox list [installed\|all]` | 列已装 / 可装版本 | 读缓存 | 无 | `--path` 显示全路径 |
| `camoufox active` | 打印当前 active 版本 | 否 | 无 | 只读 |
| `camoufox path` | 打印安装目录 | 否 | 无 | 只读 |
| `camoufox version` | 版本 + 浏览器 + GeoIP + 存储 | 否 | 无 | **开工前第一条命令** |
| `camoufox sync` | 从远端仓库同步可用版本清单 | 是 | 无 | `--spoof-os [auto\|mac\|win\|lin]`、`--spoof-arch [...]` |
| `camoufox remove [VERSION_PATH]` | 删下载的数据 | 否 | **是** | 默认删全部；`--select` 交互选单个版本，`-y` 跳过确认 |
| `camoufox test [URL]` | 打开 Playwright inspector | 取决于 URL | 无 | `--executable-path` 可指定二进制 |
| `camoufox gui` | 图形管理器 | 是 | 同 set | 需 `pip install 'camoufox[gui]'`（PySide6），本机未装 |
| `camoufox server` | 起 Playwright server，打印 ws endpoint | 否* | 无 | *浏览器未装时会自动下载 → 首次可能联网 |

---

## 3. 启动服务

### 3.1 CLI 这条路：能跑，但没有旋钮

实测（有头，全默认）：

```powershell
camoufox server
# → Launching server...
# → Server launched: 5.558s
# → Websocket endpoint: ws://[::1]:59065/0d6af0de63fbb787b11e0888faaee8f3
```

三个事实：

1. **endpoint 只在 stdout 打一次**，端口随机、token 在路径里。要程序化消费就必须解析这行输出（或自己写 launcher，见 §3.2）。
2. **子命令一个参数都没有**——`camoufox/__main__.py:880` 是 `launch_server()` 裸调用，`launch_server` 的签名允许全套 stealth 参数但 CLI 一个都没暴露。所以「用 CLI 起 + 配 stealth」这件事在当前版本**做不到**。
3. **它不接受持久化 profile**：`launch_server` 对 `persistent_context` / `user_data_dir` 直接抛 `ValueError`（`camoufox/server.py:55`，理由是 Playwright 无法把持久化 Context 通过 ws 暴露出去）。

### 3.2 带 stealth 参数起服务：自己包一层

```python
# 与 camoufox server 行为一致，但参数可控
from camoufox.server import launch_server

launch_server(
    os="windows",      # 与宿主一致：字体 / GPU / 核数才对得上
    geoip=True,        # ★ 时区 + 经纬度 + locale 一把对齐
    humanize=True,     # 鼠标轨迹人性化（默认最长 1.5s；传浮点改时长）
    headless=False,    # 有头；headless='virtual' 只有 Linux（Xvfb）有
    enable_cache=True,
)
```

要点：

- `launch_server` **会继承 stdout**（`server.py::_serve` 用 `subprocess.Popen` 不接管输出），所以 endpoint 照旧打在**你进程的 stdout** 上；想拿到它，要么在调用方重定向 fd、要么干脆别用 server 模式（§4 的进程内 API 连 endpoint 都不需要）。
- `geoip=True` 会**额外发一次公网请求**查自己的出口 IP（`ip.py::public_ip`，候选 `api.ipify.org` / `checkip.amazonaws.com` / `ipinfo.io` …）。想省掉这一次，就填死 `geoip="x.x.x.x"`。
- 挂了代理就**必须**开 geoip，否则触发 `warnings.yml::proxy_without_geoip`——「代理换了国家、浏览器还宣称原国家」是最典型的自动识别特征。

### 3.3 客户端连接（两个必须知道的点）

```python
from playwright.async_api import async_playwright

async with async_playwright() as p:
    browser = await p.firefox.connect(ws_endpoint)   # 是 firefox，不是 chromium / CDP
    ctx = await browser.new_context(no_viewport=True)  # ★ 见 §6.2
```

- **`firefox.connect`**：服务端是 Playwright 的 `firefox.launchServer`（`camoufox/launchServer.js`）。
- **`no_viewport=True` 不能省**：不补这一行，`innerWidth/innerHeight` 恒为 `1280×720`、`devicePixelRatio` 被压成 `1`（实测对照见 §6.2）。进程内 API 帮你在 `new_page`/`new_context` 上打了这个默认值（`utils.py::attach_no_viewport_default`），**走 ws 的裸 Playwright 客户端没有这个补丁**。

---

## 4. 进程内 Python API

日常更推荐这条路——不用管 endpoint、不用多进程、参数与 server 完全相同。

### 4.1 两个上下文管理器

```python
from camoufox.sync_api import Camoufox          # 同步
from camoufox.async_api import AsyncCamoufox    # 异步

with Camoufox(os="windows", geoip=True, humanize=True, headless=False) as browser:
    page = browser.new_page()          # 默认 no_viewport=True（补丁已打）
    page.goto("https://example.com")

async with AsyncCamoufox(os="windows", geoip=True) as browser:
    page = await browser.new_page()
```

- 返回类型取决于 `persistent_context`：默认 `False` → 返回 `Browser`；`persistent_context=True` → 返回 `BrowserContext`（此时可用 `user_data_dir`）。
- **只有这条路支持持久化 profile**。注意与本仓库红线的关系：**不保存浏览器 Profile**，所以 `persistent_context=True` 属于「技术上可行但纪律上不做」——写脚本时不要顺手打开。

### 4.2 手动接管已有 playwright

```python
from camoufox import AsyncNewBrowser, AsyncNewContext, NewBrowser, NewContext
```

`AsyncNewBrowser(playwright, ...)` 复用你已经起好的 `Playwright` 实例；`NewContext` 用于在同一浏览器里再开一个**独立身份**的 context（`fingerprints.py::generate_context_fingerprint` 支持 per-context 指纹）。

### 4.3 只取配置、不启浏览器

```python
import json, re
from camoufox import launch_options

opts = launch_options(os="windows", geoip=True, humanize=True)   # 纯 dict，不起进程

# ⚠️ 指纹**不在** opts 顶层，也**不在** opts["firefox_user_prefs"] 里——
# 它被编码进 opts["env"]["CAMOU_CONFIG_1..N"]（分片的一个大 JSON）。
# 直接读 opts["navigator.userAgent"] 会拿到 None（KeyError / None），
# 而「拿到 None」与「指纹没生成」长得一模一样，所以这条读法是错的。

def _rebuild(prefix: str) -> dict:
    """把 env 里的分片按编号拼回一个 JSON 对象。"""
    parts = []
    for key, value in opts["env"].items():
        m = re.fullmatch(re.escape(prefix) + r"_(\d+)", key)
        if m:
            parts.append((int(m.group(1)), value))
    parts.sort()
    return json.loads("".join(v for _, v in parts))

cfg = _rebuild("CAMOU_CONFIG")
print(cfg["navigator.userAgent"])     # → Mozilla/5.0 (... rv:156.0) ... Firefox/156.0
print(cfg.get("timezone"), cfg.get("locale:region"), cfg.get("humanize"))
print(cfg.get("webGl:vendor"), cfg.get("webGl:renderer"))
```

**实测（2026-10-20，camoufox 0.5.8）**：分片共 6 段，按编号顺序拼接后可 `json.loads`，
还原出 43 个键的 config；UA / `humanize` / `webGl:vendor` 均取得到。
`firefox_user_prefs` 里只有 10 个**非指纹** pref（滚动条样式、存储配额、DNT/GPC、
`intl.locale.requested` 等）——`navigator.userAgent` / `timezone` / `webgl.vendor` **全都不在那里**。

这是**离线核对 stealth 配置**的正确姿势——查 UA / 时区 / locale / humanize / WebGL
有没有按预期下发，不需要开窗口。注意：`geoip=True` 在 `launch_options` 阶段就会发那次公网请求。

> **要拿到最终生效值，仍以浏览器内读数为准**：离线 config 是「打算下发什么」，
> 浏览器里的 `navigator.userAgent` 是「实际生效什么」——§8.2 的判据表按后者核。

### 4.4 指纹相关的可调用面

| 入口 | 用途 |
|---|---|
| `camoufox.fingerprints.Screen(min_width, max_width, min_height, max_height)` | 约束生成屏幕尺寸的范围（不是写死尺寸） |
| `camoufox.fingerprints.generate_fingerprint(window, screen, os)` | 直接生成一份指纹 dict |
| `camoufox.fingerprints.get_random_preset(os, ff_version)` / `from_preset(preset, ff_version, salt)` | 取内置**真实设备预设** / 转成 `CAMOU_CONFIG` |
| `camoufox.webgl.firefox_gpus(target_os)` | 该 OS 上 fpgen 记录过的 `(vendor, renderer)` 组合；`webgl_config` 只接受这里的值 |

**身份稳定性**由 `launch_options` 里的 `identity_salt` 决定（`fingerprints.py:174`）：传了 `fingerprint` / `fingerprint_preset=dict` / 自带 `navigator.userAgent` 的 config → salt 稳定，字音/字体/GPU 噪声种子跟着这份身份走；什么都不传 → 每次 launch 抽一个**全新身份**（含新 salt）。

---

## 5. stealth 参数表（按收益排序）

「为什么」一栏出自库自带的泄漏清单 `warnings.yml` 与跨字段自洽校验 `coherence.py`。

### 5.1 主要矛盾（前三条决定成败）

| 参数 | 推荐值 | 为什么 |
|---|---|---|
| `geoip` | `True` 或填死出口 IP | 一个开关对齐**时区 / 经纬度 / locale 语言与地区 / WebRTC IP**。不开则 locale 地区是随机抽的，会与本机时区、与出口 IP 三方打架（§6.1 实测）。⚠️ **默认关**，且需 `camoufox[geoip]`（`maxminddb`），本机当前未装 → 传 `True` 会抛 `NotInstalledGeoIPExtra`（§1 注 3） |
| `humanize` | `True`（或浮点秒数） | 关掉等于鼠标瞬移；`True` 走默认最长 1.5s。⚠️ **默认关**。★ **它只人化「鼠标移动」，不人化「键盘输入」**（上游源码 `PageHandler.js` 只拦 `mousemove`，注释原话：*"Keyboard/typing input is NOT humanized"*）——所以 owner 裁定里「模拟鼠标移动、键盘输入」的说法**只有前半句成立**。本项目的动作面三条原语里**只有 `click` 走鼠标路径**：`fill` 是 `locator.fill(text)`（**直接设值，根本不打字**）、`press` 是 `page.keyboard.press(key)` ⇒ **两者都拿不到 humanize 的收益** |
| `os` | 与本机真实 OS 一致（本机 `"windows"`） | 字体清单、GPU、核数都按它生成；⚠️ **默认是三选一随机**，抽到 mac/linux 就与宿主真实字体对不上 |

### 5.2 别把已经对的默认值弄坏

| 参数 | 推荐 | 原因 |
|---|---|---|
| `block_images` | 保持 `False` | `warnings.yml::block_images`：拦图已被报告为大型 WAF 的检测诱因 |
| `block_webgl` | 保持 `False` | 很多 WAF 会查 WebGL 是否可用 |
| `custom_fonts_only` | 保持 `False` | 伪装 OS 却抽掉该 OS 字体 = 明显自相矛盾 |
| `disable_coop` | 仅必要时开 | 关 COOP 是为了能点跨域 iframe 里的 Turnstile checkbox，本身可被识别 |
| `addons` / `exclude_addons` | **本项目排除 uBO**：`exclude_addons=[DefaultAddons.UBO]` | 默认**会装** uBlock Origin（`launch_options` 无条件调 `add_default_addons()`，而 `DefaultAddons` 枚举里只有 UBO）。它是真实用户画像的一部分，但**对本项目是采集风险**：uBO 会拦广告 CDN，视频站的播放器初始化/预取请求可能一并被拦 → 播放器起不来 → 感知层判「无播放控件」→ **写出理由通顺的假负样本**。宁可少一层画像，不可少一层真值 |
| `pin_cpu_cores` | 保持 `False` | 开着会真占 CPU 并串行化并发；「报宿主核数」本身也是自洽的（实测 16 核 = 宿主） |
| `ff_version` | 不传 | 伪装 Firefox 版本极易被识别；要轮换就换 camoufox 版本 |

### 5.3 手别贱（会被 warning 拦下的项）

| 不要手动设 | 正确做法 |
|---|---|
| `navigator.*`（含 UA） | 交给 fpgen；UA 由真实二进制版本推出 |
| `screen.*` / `window.*` / `viewport` | 交给 fpgen；要约束就用 `screen=Screen(...)` / `window=(w, h)` |
| `geolocation.*` | 用 `geoip=`，否则「声称在 A 国、IP 在 B 国」 |
| `locale.*` | 用 `locale=` 参数（含地区，如 `"zh-CN"`） |
| `headers.User-Agent` | 不设 |
| 自带 `fingerprint` | 非必要不传（`warnings.yml::custom_fingerprint`） |

### 5.4 其余可选

| 参数 | 说明 |
|---|---|
| `proxy={"server": "http://…"}` | 与 `geoip=True` 成对出现 |
| `enable_cache` | 默认为关；真实用户有缓存，可按采集需要开（吃内存） |
| `headless` | 有头最稳；无头会让屏幕尺寸被 `get_screen_cons(headless)` 收窄去凑；`'virtual'` = Xvfb，**仅 Linux** |
| `webgl_config=(vendor, renderer)` | 钉死 GPU 伙伴；必须先 `os`，且组合必须来自 `camoufox.webgl.firefox_gpus(os)`，否则 `ValueError` |
| `fingerprint_preset` | `True` = 随机挑一个内置真实预设；传 dict = 钉死该预设（身份跨 launch 稳定） |
| `main_world_eval` | 允许主世界执行脚本；用法是 `page.evaluate("mw:" + script)` |
| `i_know_what_im_doing` | **唯一作用是把上面这些泄漏警告静音**——除非你确实知道自己在做什么，否则别开 |
| `env` / `args` / `firefox_user_prefs` | 透传给浏览器；`prefs` 手调会破坏自洽，慎用 |

---

## 6. 三个实测坑（本机可复现）

### 6.1 坑一：默认配置时区与语言打架

`camoufox server` 原样启动（无 locale、无 geoip），连接后实测：

```json
{"langs": ["en-US", "en"], "intl": "en-US", "tz": "Asia/Shanghai"}
```

**中国时区 + 全英文语言**——两个字段各自内部自洽，凑一起就是一个「不可能的设备」。根因是 `warnings.yml::no_region` 说的那件事：没传 locale 地区时 Camoufox 会**自己抽一个地区**。

**修法**（两种都行，不要同时用错值）：

```python
launch_server(geoip=True)    # 让出口 IP 决定：实测 → locale zh/CN + timezone Asia/Shanghai
launch_server(locale="zh-CN")  # 手动指定；必须与出口 IP 的地区一致，否则等于主动暴露矛盾
```

修正后实测：`langs ["zh-CN","zh"]` · `intl "zh-Hans-CN"` · `tz "Asia/Shanghai"` —— 三者对齐。

### 6.2 坑二：server 模式 viewport 恒为 1280×720，dpr 被压成 1

同一 server、同一身份，两种 context 对照（实测）：

```
default       {"inner": [1280, 720],  "outer": [1295, 813], "screen": [1920, 1080], "dpr": 1}
no_viewport   {"inner": [1905, 940],  "outer": [1920, 1032], "screen": [1920, 1080], "dpr": 1.5}
```

**后果有两层**：① 每次会话的 `innerWidth/Height` 是同一个常数（可跨会话关联）；② `inner`/`dpr` 与 spoof 出去的 `screen` 对不上。

根因：Playwright 默认给 context 套 1280×720 viewport；进程内 API 用 `attach_no_viewport_default`（`camoufox/utils.py:714`）把默认改成 `no_viewport=True`，**ws 客户端没有这个补丁**。

**修法**：客户端 `new_context(no_viewport=True)` / `new_page(no_viewport=True)`；需要固定视口时显式给一个与 `screen` 匹配的 `viewport=`。

### 6.3 坑三：`debug=True` 在 GBK 控制台必崩

```
UnicodeEncodeError: 'gbk' codec can't encode character '\ub9d1' in position 1
  File "...camoufox/utils.py", line 1533, in launch_options
    pprint(config)
```

指纹的字体清单里带韩文字体名（`맑은 고딕`），`pprint` 打到 GBK stdout 直接抛异常、**进程退出、服务根本没起来**。

**修法**：先 `$env:PYTHONIOENCODING="utf-8"`（或 `chcp 65001`）。`debug=True` 值得用——它是唯一能看到 fpgen 到底下发了什么的手段（§8 的判据表就是照它的输出核对的）。

---

## 7. 推荐配置（可直接抄）

> **三处「不抄就会踩」的默认值**（2026-10-20 源码复核，详见 §11）：
> `humanize` / `geoip` **默认关**；`os` **默认在 windows/macos/linux 里随机挑**；
> `addons` **默认会装 uBlock Origin**。
> 下面这份已经把该显式写的都显式写了。

**服务端**（要 CLI 没有的旋钮，只能这样起）：

```python
from camoufox.addons import DefaultAddons
from camoufox.server import launch_server

launch_server(
    os="windows",                 # 不传则随机挑 OS
    humanize=True,                # 默认关
    headless=True,                # ⚠️ 2026-10-21 起默认无头（§7.1.1）；服务端这条路
                                  #    没有人守着的桌面，只能无头
    enable_cache=True,
    exclude_addons=[DefaultAddons.UBO],   # 默认会装 uBO——会挡播放器请求
    # geoip=True,                 # ⚠️ 需 `camoufox[geoip]`（maxminddb），本机当前未装
    # debug=True,   # 排查用；开之前先 PYTHONIOENCODING=utf-8
)
```

**客户端**：

```python
browser = await playwright.firefox.connect(ws_endpoint)
ctx = await browser.new_context(no_viewport=True)
```

**进程内等价写法**（省掉 endpoint 与两段进程，日常首选）：

```python
from camoufox.addons import DefaultAddons

async with AsyncCamoufox(
    os="windows", humanize=True, headless=True,   # ⚠️ 2026-10-21 起默认无头，见 §7.1.1
    exclude_addons=[DefaultAddons.UBO],
    # geoip=True,                 # 同上，需 maxminddb
) as browser:
    page = await browser.new_page()
```

---

## 7.1 ⚠️ headful 的硬约束：**窗口必须可渲染**（2026-10-20 实测）

`headless=False` 曾是本项目的**有意选择**（方案 §4.3：无头本身是一类检测信号）。
但它带来一条必须记账的代价，六轮探针（`research/probes/probe_click_*.py`）量到的：

| 状态 | 同一次 `locator.click` | 页内 `rAF` 滞后 |
|---|---|---|
| 窗口可见 | **0.4–1.5s** | 4–17ms |
| 窗口最小化 / 息屏 / 锁屏 / 完全遮挡 | **5–13s** | 902ms → **62s**（一帧没画） |
| `headless=True` | 34–71ms | 4–17ms |

- **慢的不是 `humanize`**：最小化时 `humanize=False` 仍 **9.1s**、`humanize=0.3` 也 9.1s；
  它只把 9.1s 放大到 13.3s；
- **Firefox 偏好治不住**：`widget.windows.window_occlusion_tracking.enabled=False`、
  `dom.min_background_timeout_value` / `dom.timeout.background_throttling_max_budget`
  三项都试过，最小化后仍 5.1–9.4s；
- **超时的点击不等于没点**：页内 `pointerdown`/`click` 计数照样涨。
  **所以绝不能重试**——重试就是点两次。

**因此**：无人值守跑 headful 批次，必须保证桌面可渲染（不息屏、不锁屏、窗口不最小化、
不被全屏窗口完全覆盖）；做不到就 `headless=True`，**并把这条记进批次元数据**——
换指纹口径是会影响分布的事，不能悄悄改。

### 7.1.1 ★ 口径反转：**无人值守默认走无头**（2026-10-21 owner 拍板）

**缺省从 `headless=False` 改成 `headless=True`**，这是对上面 §7.1 那条
「有意选择 headful」的**反转**，理由与代价：

| | headful | headless |
|---|---|---|
| 单次点击 | 可见 0.4–1.5s；**不可渲染时 5–13s** | **34–71ms，从不慢** |
| 依赖桌面状态 | **是**（不锁屏 / 不息屏 / 不最小化 / 不遮挡） | **否** |
| 无人值守可行性 | **不保证**（跑一夜 / 合盖 / 远程桌面断开都会触发） | **成立** |
| 代价 | — | **指纹口径变了** → `headless` 进批次元数据（`fingerprint` 第九项） |

「保证桌面可渲染」在真无人值守场景里**保证不了**——那正是无人值守的定义。
所以默认无头，有头保留为显式 `launch(headless=False)` / CLI `--headful`，
给「人守着的桌面」那条路。

**两处配套**：

- `ACTION_TIMEOUT_MS` 5s → **15s**（盖住实测上界 13323ms）。
  无头口径下 34–71ms **永远碰不到上限**，所以放宽只影响有头那条路；
- ⚠️ **无头口径下的指纹读数要重测**——§4.3.1 那组读数（内高 940/948 摆动、
  `dpr` 1.5）是 **headful 下**量的。无头下视口与 dpr 会不会变，**还没测过**。

**A 的前置门**：headless 下的**反爬拦截率**（方案 §6.1：显著变高是否决项），
**还没测**。

---

## 8. 复验流程（换机器 / 升级版本后照这个再跑一遍）

### 8.1 步骤

```powershell
camoufox version          # ① 浏览器 / GeoIP / 包版本是否就绪
camoufox server           # ② 起服务，记下 ws endpoint（要配参数见 §7）
# ③ 另开终端，用下面这段探针连上去读数
```

```python
# probe.py —— 只读、不写任何文件
import asyncio, json, sys
from playwright.async_api import async_playwright

JS = """() => ({
  ua: navigator.userAgent, oscpu: navigator.oscpu, platform: navigator.platform,
  wd: navigator.webdriver, langs: navigator.languages, cores: navigator.hardwareConcurrency,
  intl: Intl.DateTimeFormat().resolvedOptions().locale,
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  screen: [screen.width, screen.height], inner: [innerWidth, innerHeight],
  dpr: devicePixelRatio,
  gl: (() => { const c = document.createElement('canvas').getContext('webgl');
    const d = c && c.getExtension('WEBGL_debug_renderer_info');
    return c ? (d ? c.getParameter(d.UNMASKED_RENDERER_WEBGL) : c.getParameter(c.RENDERER)) : null; })(),
})"""

async def main(ws):
    async with async_playwright() as p:
        b = await p.firefox.connect(ws)
        ctx = await b.new_context(no_viewport=True)
        pg = await ctx.new_page()
        await pg.goto("https://example.com", wait_until="domcontentloaded")
        print(json.dumps(await pg.evaluate(JS), ensure_ascii=False))
        await b.close()

asyncio.run(main(sys.argv[1]))
```

```powershell
$env:PYTHONIOENCODING="utf-8"; python probe.py "ws://[::1]:端口/token"
```

### 8.2 判据（全部满足才算「这一天没退化」）

| 判据 | 期望 | 不满足的含义 |
|---|---|---|
| `navigator.webdriver` | `false` | 自动化标记泄漏，**阻塞级** |
| UA 里的 `rv:` / `Firefox/` 版本 | 与 `camoufox version` 的浏览器版本主号一致 | 版本冒充 |
| `oscpu` / `platform` 与 UA | 三者同 OS | 指纹自相矛盾 |
| `langs` / `intl` 与 `tz` | 同一地区（如 zh-CN ↔ Asia/Shanghai） | 见 §6.1，**默认配置就会踩** |
| `inner` | **不等于** `1280×720` 常数 | 漏了 `no_viewport=True` |
| `dpr` | 与宿主显示缩放一致（本机 150% → 1.5） | viewport 覆盖了 dpr |
| `screen` 与 `inner` | inner ≤ screen，且尺寸关系合理 | 窗口与屏幕打架 |
| `gl` | 非空，且 `(vendor, renderer)` 在 `firefox_gpus(os)` 里 | WebGL 兜底/异常 |

> **本仓库口径**：这类「读证据核对」属于人读，不进 `pytest` 门禁——与 [CLAUDE.md](../../../CLAUDE.md)「每次真实运行后逐条读 evidence」同一条纪律。

---

## 9. 与仓库纪律的交叉点

| 仓库规矩 | 与本依赖的关系 |
|---|---|
| **不保存浏览器 Profile**（跨树红线） | server 模式**天然符合**（`launch_server` 拒绝持久化）；进程内 `persistent_context=True` 则显式违反，不要开 |
| **批次元数据必留**（并池是写入时动作） | 指纹身份每次 launch 重抽（未 pin 时），且**身份绑在浏览器进程上**（实测：同 server 重连身份不变）。采集记录里要落当时用的 `os` / `geoip` / `locale` / 是否 pin 预设，否则分布变化无法归因 |
| **禁止投票/集成** | 不适用：Camoufox 只是单一实现，不构成「多实现投票」 |
| **D7 = obscura** | 本文**不改变**执行层选型。要在 v2 里用 Camoufox，必须另开决策，而不是顺手 `import` |
| **D11 = 修订 D7** | 2026-10-20 决策修订：**camoufox 取代 obscura 成为唯一执行层**。见[设计方案 04](../设计方案/04-camoufox执行层替换方案.md) |

---

## 附：本机实测记录（2026-10-20）

| 项 | 默认 `camoufox server` | §7 推荐配置 |
|---|---|---|
| 启动耗时 | 5.558 s | 与默认同级 |
| UA | `Firefox/156.0` on `Windows NT 10.0` | 同 |
| `oscpu` / `platform` | `Windows NT 10.0; Win64; x64` / `Win32` | 同 |
| `webdriver` | `false` | `false` |
| `hardwareConcurrency` | 16 | 16 |
| `screen` | 2560×1440，avail 2560×1410，dpr 1 | 1920×1080，avail 1920×1032 |
| `inner`（默认 context） | 1280×720 | 1280×720（同一坑） |
| `inner`（`no_viewport`） | — | 1905×940 |
| `dpr`（默认 / no_viewport） | 1 / 1.5 | 1 / 1.5 |
| `langs` | `["en-US","en"]` ✘ | `["zh-CN","zh"]` ✔ |
| `intl` | `en-US` ✘ | `zh-Hans-CN` ✔ |
| `tz` | `Asia/Shanghai` | `Asia/Shanghai` |
| 地理位置 | 未下发 | 28.2 / 112.96667（geoip 推出） |
| `humanize` | 无 | `true` |
| WebGL | `ANGLE (NVIDIA, GTX 980 …), or similar` | `ANGLE (AMD, Radeon HD 3200 …), or similar` |
| 页面加载 | example.com HTTP 200 + 截图正常 | 同 |

**三条读数解释**：

1. `", or similar"` **不是**泄漏——那是 Firefox 自己对 `ANGLE`/nouveau 驱动串的归纳写法，`coherence.py` 里直接拿它当示例。
2. GPU 会抽到老卡（Radeon HD 3200 配 16 核）是 fpgen 池里组合有限的固有现象；要固定就只能 `webgl_config` 钉 `firefox_gpus(os)` 里的某一对。
3. ⚠️ **「地理位置」那一行的读数当时依赖 `geoip=True` 可用**（即 `maxminddb` 当时已装）。
   **当前 venv 缺 `maxminddb`**，照 §7 的配置重跑会抛 `NotInstalledGeoIPExtra`——
   要复现这行读数，先 `uv pip install "camoufox[geoip]"`（见 §1 注 3 / §11.5）。

---

## 10. 与 v2 执行层对接的实测（2026-10-20，进程内 API）

> 探针：[`executor/browser/camoufox_probe.py`](../../executor/browser/camoufox_probe.py)（包内，可重跑）。
> 面向 [设计方案 04](../设计方案/04-camoufox执行层替换方案.md) 的取证。
> 配置：`AsyncCamoufox(os="windows", humanize=True, headless=False, locale="zh-CN")`。

### 10.1 四条对方案有决定性影响的事实

| # | 实测 | 含义 |
|---|---|---|
| **F1** | **`screen` 逐 launch 变化**：1920×1080 → 1536×864 → 1440×900 | viewport **逐 Run 漂移**。响应式站点的内容分布因此不可归因。**必须显式钉 `screen=` / `window=`** |
| **F2** | `page.evaluate` 默认 **isolated world**：`window.__mw` 读出 `null`；`mw:` 前缀报 `Main world evaluation is disabled`，须 `main_world_eval=True` 才可用（此时 `mw:window.__mw` → `"from-main-world"`） | DOM 共享、**页面 JS 全局状态读不到**。要读播放器状态必须开 `main_world_eval` |
| **F3** | 真实点击 `isTrusted` = **`"true"`**；`evaluate` 伪造点击 = **`"false"`** | camoufox 靠**真浏览器真输入**满足「事件必须 trusted」，与 obscura 靠 stealth patch 等价 |
| **F4** | 元素**被替换** → handle `isConnected=false`，`click()` 抛 **Error**；元素**不存在** → `click()`/`fill()` 抛 **TimeoutError** | 两类语义**可区分**（Error vs TimeoutError）；但「不存在」与「页面卡住」**同型**，须另判 |

### 10.2 协议语义实测（camoufox → `PageDriver` 原语的底账）

| 项 | 实测值 | 与 obscura |
|---|---|---|
| `new_context()` 默认 viewport | `None` → **no_viewport 补丁生效** | obscura 无此概念 |
| `count(缺失选择器)` | `0`，不报错 | ✅ 一致 |
| `links` / `extract` | 直接给结构化数组 | ✅ obscura 的 NDJSON / 列式文本解析**作废** |
| `textContent` | **含 `<script>` 正文** | ⚠️ obscura 的 `TEXT_CONTENT_EXPR`（clone + 删 script/style）**仍需要** |
| `innerText` | 真实换行保留 | ✅ 一致 |
| `locator.fill()` / `keyboard.press()` | 正常 | ✅ 一致 |
| `ctx.new_page()` | 独立 page，同 context | 对应 obscura `new_tab` |

### 10.3 探针自身的两个坑（写下来免得后人重踩）

1. **`data:` URL 不能用**——URL 里的 `#` 是 fragment 分隔符，payload 在首个 `#` 处截断，
   `<script>` 块**根本不执行**，探针会**静默给出错误结论**（实测：`isTrusted` 两个值都是 `null`、
   待替换按钮纹丝不动）。必须用 `page.set_content()`。
2. **`isTrusted` 回传要用 DOM 属性，不能用 `window` 全局变量**——F2 的 world 隔离会让
   `window.__x` 读成 `null`，分不清「不是 trusted」与「没点到」。

### 10.4 指纹配置的下发通道（与 §4.3 同源）

`launch_options()` 的返回里，指纹**不在**顶层、**不在** `firefox_user_prefs`，
而是编码进 `env["CAMOU_CONFIG_1..N"]`（分片的一个大 JSON，实测 6 段 / 43 键）。
`firefox_user_prefs` 里只有 10 个**非指纹** pref。

**推论**：**绕过 `AsyncCamoufox` / `launch_server` 直接用 playwright 起浏览器，
指纹会整个丢掉**——`cfg["env"]` 必须传给 playwright 的 launch。

### 10.5 共享元素口径在 camoufox 侧怎么跑（`--element-basis`，2026-10-21）

**唯一真源**：[`executor/browser/element_basis.py`](../../executor/browser/element_basis.py)
里的 `SHARED_DOM_JS` + `filter_ladder()`。**两面跑同一段 JS**：
obscura 走 `browser_evaluate`，camoufox 走 `page.evaluate`。

| 事实 | 说明 |
|---|---|
| **隔离世界不影响它** | F2 的 `main_world_eval` 关着 → `page.evaluate` 在 isolated world 里跑。但这段 JS **只读 DOM**（`querySelectorAll` / `getAttribute` / `getComputedStyle`），**DOM 在两个 world 之间是共享的** → 读得到。**它不读页面 JS 全局**，所以 `main_world_eval` 不用开 |
| **返回形状** | JS 用 `JSON.stringify` 返回字符串（obscura 的 `browser_evaluate` 返回的是 `toString()`，对象会变 `[object Object]`——tools.md 记过）。driver 侧 `json.loads` 后交给 `parse_shared_basis()` |
| **手算的 role/name，不是引擎算的** | 共享口径的 `nameOf` 是本仓库手写的链，**与 playwright `aria_snapshot` 的一致率实测 87.9%**（设计方案 04 §4.5.3）。所以**共享口径的绝对值 ≠ camoufox 面的生产值**——共享口径回答的是「**同一个页面在两个浏览器上差多少**」，不是「camoufox 的元素表对不对」。**两者别混** |
| **测量上限 500** | 共享口径是**一次测量**、不是生产枚举；生产枚举的 200 条上限（D-2「被裁掉 vs 不存在」判据）在这里不适用。超了记 `truncated`，**不装作没有** |
| **默认关** | `--element-basis` 显式开启（多一次 evaluate 往返）。它是**测量仪器**，不是生产链必需项——`elements_total` 一个字都没改 |

**怎么用**：双跑两侧**都要开**，否则 `compare-drivers` 的共享口径判据永远是 `incomplete`。

```powershell
uv run python -m trajectory_pipeline.executor.cli check --driver camoufox --element-basis --probe-url https://www.baidu.com/
uv run python -m trajectory_pipeline.executor.cli check --driver obscura  --element-basis --probe-url https://www.baidu.com/
# 两个读数直接并排比（shared 那一行才可比；face 那一行本来就是两回事）
```

**读数缺了怎么读**（2026-10-21，共享口径落盘后）：

| 存档里看到 | 含义 | 处置 |
|---|---|---|
| `element_basis.shared` **有** | 测到了 | 两面直接比 |
| `element_basis.shared` **缺键** + `element_basis_error` **有值** | **没测到**（页面没起来 / JS 返回不合法），**原因在 `element_basis_error` 里** | 看原因；**别把「缺键」读成「这一档是 0」** |
| `element_basis.shared` **缺键** + `element_basis_error` **是 `null`** | 这一批**没开** `--element-basis` | 判据会是 `incomplete`——**判不了 ≠ 一样** |
| `element_basis` **整个是 `null`** | 同上（没开开关） | 同上 |

⚠️ **face 账目照旧给**：它不依赖共享口径，共享口径测失败时它**仍在**
（`element_basis.face`）。**只有 `shared` 会缺**。

⚠️ **测量失败不会让整批作废**——失败走 `Observation.element_basis_error`
这条**独立通道**，**不进 `degraded`**（`degraded` 是内容块的降级通道，
下游只判它非空 ⇒ 会不取候选 ⇒ 一把坏尺子能作废整批数据）。

### 10.6 venv 里 pyyaml 缺文件时的修法（L-13，2026-10-21 实测）

**症状**：`import camoufox` 报
`ImportError: cannot import name 'CLoader' from 'yaml' (unknown location)`。

**为什么是它先炸**：`camoufox/pkgman.py` 里 `from yaml import CLoader, load`——
camoufox 的一条**传递依赖**先把 `yaml` 拖进来。

⚠️ **`import yaml` 会「成功」，这是假象**：`yaml/__init__.py` 没了以后，
`yaml/` 目录退化成**命名空间包**（namespace package）——
`import yaml` 返回一个空壳，只有 `from yaml import CLoader` 才炸。
**所以别拿「import yaml 过了」当修好的证据**，要验 `from yaml import CLoader`。

**诊断按 RECORD 走，别猜**：包自己的 `*.dist-info/RECORD` 记着每个文件的
sha256 与大小，对一遍就知道**缺了哪几个、哪些被改过**：

```bash
# 逐条核对：缺失 / 哈希不符
.venv/Scripts/python.exe - << 'PY'
import base64, csv, hashlib, pathlib
sp = pathlib.Path(".venv/Lib/site-packages")
for r in csv.reader((sp/"pyyaml-6.0.3.dist-info"/"RECORD").read_text(encoding="utf-8").splitlines()):
    if not r or not r[1].startswith("sha256="): continue
    p = sp/r[0]
    if not p.exists(): print("缺失", r[0]); continue
    got = base64.urlsafe_b64encode(hashlib.sha256(p.read_bytes()).digest()).rstrip(b"=").decode()
    if got != r[1][7:]: print("哈希不符", r[0])
PY
```

**修法（离线、不动别的包）**：

```bash
# ① Windows 上**加载中的 .pyd 删不掉**（os error 5 = 拒绝访问）：
#    先把坏目录**重命名**挪开——Windows 允许重命名一个内含打开文件的目录，
#    路径因此腾空，进程照常跑（它已经把模块读进内存了）。
mv .venv/Lib/site-packages/yaml .venv/Lib/site-packages/yaml__broken_20261021
# ② 再让 uv 从**本地缓存**装一份全新的（--offline = 不联网；不写 --reinstall
#    的话 uv 认为版本没变、什么都不做）
uv sync --offline --group dev --reinstall-package pyyaml
```

**验证三件套**（缺一不可）：

```bash
.venv/Scripts/python.exe -c "from yaml import CLoader, safe_load; print(CLoader, safe_load('a: 1'))"
.venv/Scripts/python.exe -c "import camoufox; print(camoufox.__file__)"
# ＋ 按 RECORD 复核一遍（上面那段诊断脚本，期望零输出）
```

⚠️ **挪走的目录还占着旧 DLL**，等占它的进程退出后再删——
本机当时是 2026-10-20 留下的 `cf_launch.py` 残留进程（脚本本身已从仓库删掉）
占着 `_yaml.cp312-win_amd64.pyd`，所以第一次 `uv sync` 报
「failed to remove file ... 拒绝访问」；**重命名**这一步就是为了绕开它。

⚠️ **别用 `pip install` 修**——那会走网络解析依赖树；`uv sync --offline`
用的是锁文件 + 本地缓存，是本仓库的既定姿势。

---

## 11. 默认值的真相（2026-10-20 源码复核，**推翻几处文档转述**）

> 这一节逐条对着 `.venv/Lib/site-packages/camoufox/utils.py` 的
> `launch_options()` 源码核过，**不是**照抄官方文档。
> 起因：写「推荐配置」时发现照官方示例抄会直接崩。

### 11.1 三个反检测参数**默认全是关的**

`launch_options()` 的签名里三者是 `Optional[...] = None`，源码里都是 `if xxx:` 才生效——
**不显式传就等于没开**：

| 参数 | 源码位置 | 默认 | 不开的后果 |
|---|---|---|---|
| `humanize` | `utils.py` `if humanize:` → `set_into(config, 'humanize', True)` | ❌ 关 | 鼠标瞬移，行为特征裸露 |
| `geoip` | `utils.py` `if geoip:` → `geoip_allowed()` → 查库 | ❌ 关 | 时区/经纬度不与出口 IP 对齐 |
| `main_world_eval` | `utils.py` `if main_world_eval:` | ❌ 关 | 主世界脚本读不到页面 JS 全局 |

> **别被 `fingerprint` 骗了**：fpgen 生成的**设备指纹**默认是开的（`fingerprint_preset`
> 不传时走 `generate_fingerprint()`），
> 但 **humanize（行为）与 geoip（位置对齐）是另外两回事，默认都不开**。

### 11.2 `os` 默认**随机**

`generate_fingerprint(screen=..., window=..., os=os)` 的 `os` 不传时，
fpgen 在 `["windows", "macos", "linux"]` 里**随机挑**（docstring 写明）。
字体清单、GPU 池、核数都按它生成——**抽到 mac/linux 就与宿主真实环境对不上**。
本项目宿主是 Windows，故显式 `os="windows"`。

### 11.3 uBlock Origin **默认会装**

`launch_options()` 里 `add_default_addons(addons, exclude_addons)` 是**无条件调用**，
而 `DefaultAddons` 枚举**只有 UBO** 一项 → 每次 launch 都装 uBO。

**对本项目是采集风险**（不是画像收益）：uBO 拦广告 CDN，
视频站的播放器初始化/预取请求可能被一并拦掉 →
播放器起不来 → 感知层判「无播放控件」→ **写出理由通顺的假负样本**。

处置：`exclude_addons=[DefaultAddons.UBO]`（见 §5.2）。

### 11.4 `screen` 与 `window` 管的是**两件事**

| 参数 | 管什么 | 不传的后果 |
|---|---|---|
| `screen=Screen(min_w, max_w, min_h, max_h)` | **指纹里**的屏幕尺寸 | 逐 launch 抽不同（实测 F1：1920×1080 → 1536×864 → 1440×900） |
| `window=(w, h)` | **实际窗口/视口**尺寸 | 有显示器时 `get_screen_cons()` 按显示器上界抽 min，仍随机 |

`new_context()` 默认 `no_viewport=True`（视口 = 窗口尺寸），
所以**要固定视口必须传 `window=`，只传 `screen=` 不够**——两个都要钉。

### 11.5 GeoIP 的两层前置（详见 §1 注 3）

- 数据文件：已装 ✓
- Python 读取器 `maxminddb`：**未装** ✗ → `ALLOW_GEOIP=False` → `geoip=True` **直接抛异常**

### 11.6 ★ `humanize` **只人化鼠标移动，不人化键盘输入**（2026-10-22 源码复核）

上游 camoufox 源码对 `humanize` 的定义原话是
*"Humanize the mouse movement, but not the keyboard input"* /
*"Keyboard/typing input is NOT humanized"*。

⇒ **三条后果**（都直接落在本项目的动作面上）：

| 后果 | 说明 |
|---|---|
| **`fill` / `press` 拿不到 humanize 的收益** | 动作面有三条原语（`click` / `fill` / `press`），**只有 `click` 走鼠标路径**。`fill`（往输入框打字）与 `press`（敲回车）**仍是瞬时的** |
| **「humanize 拖慢点击」的说法要分清** | 它拖慢的是**点击**（鼠标轨迹回放，最长 1.5s），**不是页面加载**。页面加载慢的成因是 patched Firefox 本身 + 指纹注入，**不是 humanize**（§7.1 那张表：「慢的不是 `humanize`」——最小化时 `humanize=False` 仍 9.1s） |
| **headless + humanize 是正确组合** | 有头在窗口不可渲染时（最小化/息屏/锁屏/遮挡）单次点击要 **5–13s**，会把一半的点击打成超时假负样本（§7.1 / §7.1.1）。⇒ **无人值守走无头**（`headless=True`），`--headful` 显式切回 |

⚠️ **owner 裁定原文里「模拟鼠标移动、键盘输入」的说法只有前半句成立**——
记在这里，免得后人照那句话去设计「打字也要人化」的方案。

### 11.7 ★ 生产口径的指纹读数（2026-10-22 实测，`headless=True`）

v2 生产链的执行层自 2026-10-22 起是 camoufox（见[设计方案 04 §4.3.1](../设计方案/04-camoufox执行层替换方案.md)），
配置是 `camoufox_session.py::CamoufoxSession.launch` 里那一支：
`os="windows"` · `screen=Screen(1920×1080)` · `window=(1920, 1080)` ·
`locale="zh-CN"` · `humanize=True` · `geoip=False` · `main_world_eval=False` ·
排除 uBO · `headless=True`。

**5 次 `check --driver camoufox` 的实测读数**（读回来的，不是请求值）：

| 字段 | 值 | 稳定性 |
|---|---|---|
| `os` | `windows` | 请求值·恒定 |
| `locale` | `zh-CN` | 请求值·恒定 |
| `humanize` | `True` | 请求值·恒定 |
| `geoip` | `False` | 请求值·恒定（**本机缺 `maxminddb`，开 `geoip=True` 会直接抛**，见 §11.5） |
| `headless` | `True` | 请求值·恒定 |
| `screen` | `[1920, 1080]` | **实测·5/5 恒定** ✅ |
| `window`（inner） | `[1920, 947]` ×2 / `[1920, 955]` ×3 | 实测·**宽恒 1920，高摆动 8px** |
| `dpr` | `1` | 实测·5/5 恒定 |
| `timezone` | `Asia/Shanghai` | 实测·5/5 恒定 |

配套现场读数（诊断用，**不进批次元数据**）：`ua` = `Firefox/156.0 on Windows NT 10.0` ·
`oscpu` = `Windows NT 10.0; Win64; x64` · `platform` = `Win32` ·
**`webdriver` = `False`** ✅ · `cores` = 16 ·
`languages` = `["zh-CN","zh"]` · `intl` = `zh-Hans-CN` ·
`timezone` = `Asia/Shanghai`（**与 locale 同区 ✅**——§6.1 那个「不可能的设备」没发生）。

⚠️ **两条别踩的坑**：

1. **`window` 记的是 inner，不是请求值。** 请求值是 `window=(1920, 1080)`
   （外框），实测 inner 是 `[1920, 947/955]`——**视口 = 外框 − chrome**。
   而 chrome 高度逐次微变 ⇒ **同一份配置每批的实测几何都不一样**，
   所以批次元数据**只认实测值**。
2. **headful 与 headless 的读数不同，别混用**：headful 实测内宽恒 `1905`、
   `dpr=1.5`、内高 `940/948`；headless 实测内宽恒 `1920`、`dpr=1`、
   内高 `947/955`。**拿 headful 的数去比 headless 的存档会读成「指纹异常」**。

⚠️ **2026-10-22 生产批次补记：内高的摆动比上面记的 8px 宽得多**。
`check` 探针 5 次量到 `947`/`955`（摆动 8px），但**第一批生产批**
里读到的内高是 **`909` / `947` / `955` / `965`——跨度 56px**（8 条：909×1、
947×3、955×2、965×1）。
⚠️ **第一批那 12 份存档已整代回滚**（增量八十～八十一）⇒ 它的目录
`output/pipeline/camoufox_prod1/` **已不在盘上**，读数只剩现场日志
`_camoufox_prod1.log`。
✅ **第二批（修复后，当前生产数据）的读数**——12 份存档的 `fingerprint.window`
逐条读出来：**`947`×9 / `955`×2 / `965`×1 ⇒ 跨度 18px**（内宽恒 1920，
其余八项全恒定）。⇒ **909 只在第一批出现过，第二批 12 条一条都没有**
⇒ **56px 与 18px 两代都存在** ⇒ **「摆动 8px」确实不是上界，
但摆动幅度是逐批的** ⇒ **批次元数据只认实测值**这条读法不变。

⇒ **「摆动 8px」是探针口径的读数，生产口径已经把它推翻**。
**别把它当上界**（那会让 `909` 被读成「指纹异常」，而它其实是常态）。
⇒ 读法不变：**只认实测值**。该批其余八项**全部恒定**
（`os` / `geoip` / `locale` / `humanize` / `headless` / `screen` /
`dpr` / `timezone`）⇒ **漂的只有 chrome 高度（⇒ inner 高），
不是窗口尺寸本身**。

⇒ **这正是「指纹必须落进 P1 存档」的直接理由**：指纹只在 launch
那一刻确定，**事后补不回来**（`RunRecord.fingerprint`），而它决定
「这批为什么快 / 为什么慢」「这批的分布跳变是站点变了还是指纹漂了」。
**上面这批读数就是现场证明**：不落盘，909 与 965 会被读成两个不同环境，
落了就只是「这批 chrome 偏高」——而后者不需要任何人去查。

---

## 12. ★ 读稳：「换没换文档」的判据怎么取（2026-10-22 实测 + 源码）

> 这一节记的是**执行层自己的读数纪律**：动作之后怎么知道「读到的是新页还是旧页」。
> 症状与治法在[避坑指南 §8.16](../02-避坑指南.md)，这里只记**依赖侧的事实**
> （哪些信号在 camoufox 上可用、哪些不可用、怎么验）。

### 12.1 为什么需要它：`observe()` 是**多次独立往返**

`observe()` 至少四次往返（url/title → body → links → 交互元素 → 计数），
而**导航是异步 commit 的**——commit 落在两次往返之间，那份观察就是**两页的拼接**。
实测症状：同一页、同一动作位置，**两次独立运行 `iframe_count` 读出 3 与 0**
（那个 0 不是页面事实）；下游因此打出**理由通顺的假负样本**。

### 12.2 候选判据的实测对照（**别用错那一代**）

| 候选 | 实测行为 | 结论 |
|---|---|---|
| `page.on("framenavigated")` | **同文档导航（pushState / 改 query）也触发** | ❌ SPA 页面（bilibili 等）播放时改 URL ⇒ 被误判成「换了文档」⇒ 观察白白重来、连续撞上还误记 `nav_unsettled` ⇒ **把好观察当半成品丢掉** |
| **`performance.timeOrigin`** | 同文档连读两次**恒定**；pushState 后**不变**；真导航后**变** | ✅ **每个文档一个常量**，**pushState 免疫** |

⇒ **判据必须能分「换了文档」与「只换了 URL」**。

### 12.3 ★ 两种「读不到」必须分开

| 读数 | 含义 | 处置 |
|---|---|---|
| `""`（空串） | **读失败**——执行上下文被销毁 ⇒ **正处在换文档的路上** | **继续等**（那正是要等的情形）；把它当成「没变」就是**提前返回 = 没等** |
| `"none"` | 页面**没有 `performance`**——读得到且恒定 | **不是读不稳**：永不误伤，照常等完，结果只能是 `same` |

⚠️ 混成一个「读不到」⇒ **每个没有 `performance` 的页面都会把观察降级成半成品**。

### 12.4 在隔离世界（`main_world_eval=False`）里读得到吗？

**读得到**，且语义是**文档的**那个（不是隔离世界自己新建的）：
`probe_doc_stamp.py` 实测四条——同文档连读两次 `same`；pushState 后 `same`
（**且 `url_changed=true`**，证明 pushState 真的发生了，不是空转）；真导航后**变了**；
新文档内连读两次 `same`。

⇒ 探针与驱动**共用同一个表达式常量**（`camoufox_driver.DOC_STAMP_EXPR`），
**不抄第二份**——抄两份必然漂，而漂了的症状是「代号一直在变」（每次读都是新文档），
**很难查**。

### 12.5 现在的口径（代码位置）

| 环节 | 位置 | 行为 |
|---|---|---|
| 取文档代号 | `camoufox_driver._doc_stamp` | 返回 `str`；异常 ⇒ `""` |
| 动作之后等 commit | `camoufox_driver._await_commit` | 有界（`NAV_COMMIT_TIMEOUT_MS = 2000`）；返回 `new` / `same` / `unknown` |
| 动作之后等 DOM | `camoufox_driver._settle` → `_await_ready` | 有界（`NAV_SETTLE_TIMEOUT_MS = 2000`）；结果记 `last_action["settle"]` |
| 观察自检 | `camoufox_driver.observe` | 观察前后各取一次代号，跨了文档就重来（至多 `OBSERVE_RETRY = 2` 次），仍不稳记 `degraded += "nav_unsettled"` ⇒ 下游**不判**（fail-closed） |

⚠️ **`fill` 也要等**：页面脚本可能因输入触发跳转，而 agent 链里 `fill` 之后
**下一步就是观察**——不等的代价是拿旧页的名字去点新页，**点错不可逆**。

⚠️ **owner 裁定「速度次要，可接受增加等待时间」**——每条动作多等至多 2s，
这是**有意付的代价**，不是可以顺手优化掉的。

### 12.6 复验

```powershell
# ① 判据本身（联网）：pushState 免疫 + 真导航能认出来
uv run python trajectory_pipeline/research/probes/probe_doc_stamp.py
# ② 现场：动作之后 nav=new / settle=ok，stamp 在观察期间不变
uv run python trajectory_pipeline/research/probes/probe_nav_commit.py --url https://www.bilibili.com/
# ③ 批量（离线）：存档里还有多少「读到一半」
uv run python trajectory_pipeline/research/probes/probe_settle_readings.py trajectory_pipeline/output/pipeline camoufox
```