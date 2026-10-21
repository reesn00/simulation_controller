"""camoufox 进程会话——**一个 Run = 一次 launch**。

为什么单列一层而不是让 driver 自己 ``AsyncCamoufox(...)``：
身份与 viewport 是**跨原语共享的状态**（D11 / 方案 §4.3）。driver 每调一次
原语就自己开一次浏览器，等于每个原语换一次指纹——同一 Run 里的不同站点
从此不可比，响应式站点的内容变化也无法归因到站点本身。所以 launch 的
生命周期挂在 Run 上，driver 只是它的使用者。

## 钉死的东西（不钉就没法归因，见方案 §4.3）

| 项 | 值 | 为什么 |
|---|---|---|
| ``os`` | ``"windows"`` | 不传时 fpgen 在 windows/macos/linux 里**随机挑**，字体/核数/GPU 跟着漂 |
| ``screen`` | 1920×1080 | 约束**指纹里**报的屏幕尺寸 |
| ``window`` | 1920×1080 | 才是**实际视口**——``new_context()`` 默认 ``no_viewport=True``，只钉 screen 视口仍会漂 |
| ``humanize`` | ``True`` | 默认**关**，不开等于鼠标瞬移 |
| ``headless`` | ``True`` | **2026-10-21 起**：无人值守走无头。headful 窗口不可渲染时（最小化 / 息屏 / 锁屏 / 遮挡）单次点击要 5–13s，超出动作预算——见 :meth:`launch` |
| ``geoip`` | ``False`` | 默认关；开了会外发公网 IP 探测，且批次与出口 IP 绑定 |
| ``main_world_eval`` | ``False`` | 默认关；开了扩大采集面（能读页面自己 ``window`` 上的状态） |
| ``exclude_addons`` | ``[DefaultAddons.UBO]`` | 默认**装** uBO，它会拦广告 CDN，可能连带拦掉播放器初始化 → 假负样本 |

**不 pin ``fingerprint_preset``**：预设是「真实设备」，跨 Run 复用同一台设备
会让一批样本带着同一指纹，反而是关联特征。每次重抽 + 钉死几何更稳。
**不开 ``persistent_context=True``**：会落浏览器 Profile，撞 CLAUDE.md 红线。

## 指纹读数

``fingerprint`` 是**实际读回来的**几何与时区（不是请求值），字段与方案 §4.3
要求的批次元数据一致；``readings`` 是更全的现场读数（UA / webdriver / 核数…），
只用于 ``check`` 打印。**两者都不含 Cookie / 登录态**——本模块不碰这些。
"""

from __future__ import annotations

from typing import Any

from camoufox import AsyncCamoufox, DefaultAddons
from camoufox.fingerprints import Screen

#: 与宿主一致。**显式给**——不传时 fpgen 随机挑 OS。
DEFAULT_OS = "windows"

#: 目标场景是中文站点，钉死避免 locale 逐 Run 漂移。
DEFAULT_LOCALE = "zh-CN"

#: 屏幕尺寸下限=上限=1920×1080，即**钉死**。两个都要给：fpgen 把缺失的边界
#: 当作无约束，只给 max 仍会抽出更小的屏。
DEFAULT_SCREEN = Screen(
    min_width=1920, max_width=1920, min_height=1080, max_height=1080
)

#: 窗口（外框）尺寸。``no_viewport`` 下视口由窗口尺寸决定，故这是**实际生效**
#: 的那一半；真实 inner 尺寸与它不等（浏览器 chrome 占高），所以运行时读一次
#: 回填进 ``fingerprint["window"]``。
DEFAULT_WINDOW = (1920, 1080)

#: 现场指纹读数。只读 window/document/screen/navigator——**不读 localStorage /
#: cookie / 任何持久化状态**，与红线一致。
_FINGERPRINT_JS = """() => ({
  ua: navigator.userAgent, oscpu: navigator.oscpu,
  platform: navigator.platform, webdriver: navigator.webdriver,
  languages: navigator.languages, cores: navigator.hardwareConcurrency,
  intl: Intl.DateTimeFormat().resolvedOptions().locale,
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  screen: [screen.width, screen.height],
  window: [innerWidth, innerHeight],
  dpr: devicePixelRatio,
})"""


class CamoufoxSession:
    """一次 launch 的句柄：browser + 一个 context + 当前 page。

    用 :meth:`launch` 构造（异步），用 :meth:`close` 或 ``async with`` 释放。
    """

    name = "camoufox"

    def __init__(self, manager: AsyncCamoufox, browser: Any,
                 context: Any, page: Any, *, readings: dict[str, Any],
                 headless: bool = True) -> None:
        self._manager = manager
        self._browser = browser
        self._context = context
        #: 有头还是无头。**请求值**（不可回读），但它是批次元数据的一项——
        #: 无人值守走无头（2026-10-21 口径反转），headful 只在「人守着桌面」时用。
        self._headless = headless
        #: 页面**栈**：栈顶 = 当前活动页。用栈而不是单个 ``_page``，是因为
        #: 外链校验（``steps/link_check``）要「开 tab → 观察 → 关 tab」后
        #: **回到中间页**继续采；一个 `_page` 字段关完就无处可回。
        self._pages: list[Any] = [page]
        self._readings = readings
        self._closed = False

    # ── 构造 / 释放 ──────────────────────────────────────────────

    @classmethod
    async def launch(cls, *, headless: bool = True) -> CamoufoxSession:
        """起浏览器、开一个 context、读一次指纹。

        ``headless=True`` 是缺省，**这是 2026-10-21 的一次口径反转**
        （决策依据见[设计方案 04 §4.9.5](../../../docs/设计方案/04-camoufox执行层替换方案.md)）：
        原来有意选 ``headless=False``——「无头模式本身是一类检测信号」。
        反转的理由是**无人值守**：headful 窗口一旦不可渲染（最小化 / 息屏 /
        锁屏 / 被完全遮挡），Firefox 会降频渲染与输入派发，
        页内 ``requestAnimationFrame`` 滞后从 4–17ms 涨到 902ms–62s，
        单次点击随之从 0.4–1.5s 涨到 **5–13s**——而动作预算只有 5s，
        于是**一半的点击超时**，下游读到的却是「页面没有播放器」
        （理由通顺的假负样本）。

        六轮探针（``research/probes/probe_click_*.py``）证伪了四个别的猜测：
        不是挂死、不是「新开的 tab」、``humanize`` 只是放大器、Firefox 偏好也治不住。
        真因只有一个：**窗口不可渲染**。

        所以现在：**无人值守走无头**（34–71ms，从不慢），
        「人守着的桌面」要 headful 就显式传 ``headless=False``。

        ⚠️ **代价是指纹口径变了**，所以 ``headless`` 必须进批次元数据
        （:attr:`fingerprint` 里有一项）——否则「这批为什么快 / 为什么慢」
        事后解释不了，跨批次的分布跳变会被读成质量变化。
        """
        manager = AsyncCamoufox(
            os=DEFAULT_OS,
            screen=DEFAULT_SCREEN,
            window=DEFAULT_WINDOW,
            locale=DEFAULT_LOCALE,
            humanize=True,
            geoip=False,
            main_world_eval=False,
            exclude_addons=[DefaultAddons.UBO],
            headless=headless,
        )
        browser = await manager.__aenter__()
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto("about:blank")
            readings = dict(await page.evaluate(_FINGERPRINT_JS))
        except BaseException:
            # launch 半途失败必须把 browser/playwright 一起收掉，否则进程泄漏
            await manager.__aexit__(None, None, None)
            raise
        return cls(manager, browser, context, page, readings=readings,
                   headless=headless)

    async def close(self) -> None:
        """幂等关闭。**只关自己起的东西**——不碰外部进程。"""
        if self._closed:
            return
        self._closed = True
        await self._manager.__aexit__(None, None, None)

    async def __aenter__(self) -> CamoufoxSession:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # ── 页面 ─────────────────────────────────────────────────────

    @property
    def page(self) -> Any:
        """当前活动页（栈顶）。**永远非空**——见 :meth:`close_page` 的补页逻辑。"""
        return self._pages[-1]

    @property
    def pages(self) -> tuple[Any, ...]:
        """当前存活的全部页面，栈底在前（诊断用）。"""
        return tuple(self._pages)

    @property
    def context(self) -> Any:
        return self._context

    async def new_page(self, *, url: str | None = None) -> Any:
        """开一张新页并**切成当前页**；给了 ``url`` 就顺路导航。

        导航失败**不在这里收拾**：调用方（``CamoufoxDriver.new_tab``）负责
        把这张页关掉再抛——失败留一张空页会让后续 ``observe`` 读错页。
        """
        page = await self._context.new_page()
        self._pages.append(page)
        if url:
            await page.goto(url, wait_until="load")
        return page

    async def close_page(self, page: Any) -> bool:
        """关掉指定页；**关的是当前页就回到上一页**。返回是否真的关了。

        两条不能省：

        - **只剩一页时先补一张空白页再关**。``page`` 属性必须永远可用——
          否则关掉唯一页之后，下一条 ``observe`` 拿到的是个已关闭的句柄，
          症状是「页面读不到」（会被当成能力故障），而真相是「我们自己关空了」。
        - **关的不是当前页就不动栈顶**。外链校验里可能出现「关后台页」的调用，
          这时把栈顶换掉会让下一步读到错的页，且看不出来。
        """
        if page not in self._pages:
            return False
        if len(self._pages) == 1:
            replacement = await self._context.new_page()
            self._pages.append(replacement)
        self._pages.remove(page)
        await page.close()
        return True

    # ── 指纹 ─────────────────────────────────────────────────────

    @property
    def readings(self) -> dict[str, Any]:
        """完整现场读数（``check`` 打印用）。"""
        return dict(self._readings)

    @property
    def fingerprint(self) -> dict[str, Any]:
        """批次元数据要记的那九项（方案 §4.3，2026-10-21 加 ``headless``）。

        ``os`` / ``geoip`` / ``locale`` / ``humanize`` / ``headless`` 是**请求值**——
        它们没有可读回来的形态；``screen`` / ``window`` / ``dpr`` /
        ``timezone`` 是**实测值**（spoof 之后浏览器自己报的）。
        请求值与实测值混在一处但标了来源，是因为批次元数据的用途是
        「事后能解释分布跳变」，而分布只认实测。

        ⚠️ **``headless`` 是请求值，不是实测值**——但它**必须记**：
        无头和有头采到的行为分布不一样（点击 34–71ms vs 5–13s，
        见 :meth:`launch` 的口径反转说明），不记就解释不了「这批为什么快」。
        """
        r = self._readings
        return {
            "os": DEFAULT_OS,
            "geoip": False,
            "locale": DEFAULT_LOCALE,
            "humanize": True,
            "headless": self._headless,
            "screen": list(r.get("screen") or []),
            "window": list(r.get("window") or []),
            "dpr": r.get("dpr"),
            "timezone": r.get("timezone"),
        }