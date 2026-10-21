"""camoufox 执行层对接探针——**设计期取证**，回答对接方案里的待定项。

为什么需要它：``camoufox`` 是裸 Playwright API，没有 MCP 的 ``tools/list``
清单可比对，``PageDriver`` 的 13 个原语到 camoufox 的映射**只能实测**——
凭 camoufox 文档或 Playwright 文档推测，写出来的多半是错的。

本探针**只读**：不写仓库任何目录，产物只打印到 stdout。
用 ``data:`` URL 测协议语义（无网络依赖、可复现），用真实站点测正文采集。

跑法::

    uv run python -m trajectory_pipeline.executor.browser.camoufox_probe

可重跑：camoufox / playwright 升级后重跑，看 :data:`CHECKS` 的结论是否漂移。
与 ``mcp_probe.py`` 同惯例——**探针必须在包内**（产物目录被 .gitignore 挡掉，
放那儿等于换台机器就没了）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from camoufox.async_api import AsyncCamoufox

#: 协议语义探针页。刻意内联：外网不可复现，且隔离了网络噪声。
#:
#: ⚠️ **必须走 :meth:`page.set_content`，不能用 ``data:`` URL**——
#: data: URL 里的 ``#`` 是 fragment 分隔符，``id="b1"`` 里的 ``#`` 不在
#: HTML 里但在 URL 里，整段 payload 会在首个 ``#`` 处被截断，
#: 于是 ``<script>`` 块根本不执行，探针会静默给出错误结论
#: （实测踩过：``isTrusted`` 两个值都是 ``null``、待替换按钮纹丝不动）。
#: ``set_content`` 没有这层 URL 解析，HTML 字符串原样落地。
#:
#: ``#host`` 里的按钮 500ms 后被整块替换，用来测 ElementHandle 的存活语义。
#: ``#stable`` 里的按钮**永不被替换**——isTrusted 的测试目标必须是稳定的，
#: 否则会与替换竞争（实测踩过：click 报 "element was detached from the DOM"，
#: 读到的 ``__trusted`` 自然是 null，分不清「不是 trusted」与「没点到」）。
PROBE_HTML = """<html><head><title>probe</title></head><body>
<h1>Hello</h1>
<a href="https://example.com/a">Link A</a>
<a href="https://example.com/b">Link B</a>
<input id="q" type="text" placeholder="search">
<div id="stable"><button id="s1" onclick="document.title='clicked'">Stable</button></div>
<div id="host"><button id="b1">Doomed</button></div>
<script>
document.addEventListener('click', function(e){
  if (e.target && e.target.id === 's1') {
    document.body.setAttribute('data-trusted', String(e.isTrusted));
    document.body.setAttribute('data-clicks', '1');
  }
}, true);
setTimeout(function(){
  document.getElementById('host').innerHTML =
    '<button id="b2">Rebuilt</button>';
}, 500);
</script>
</body></html>"""


def _dump(obj: Any) -> str:
    """ASCII 安全序列化——Windows 控制台是 GBK，Kotlin/韩文字面量会崩。"""
    return json.dumps(obj, ensure_ascii=True, default=str)


async def _probe_protocol_semantics() -> dict[str, Any]:
    """协议语义：evaluate / click / fill / press_key / handle 存活 / 异常形态。"""
    out: dict[str, Any] = {}
    async with AsyncCamoufox(
        os="windows", humanize=True, headless=False, locale="zh-CN"
    ) as browser:
        ctx = await browser.new_context()
        page = await ctx.new_page()
        out["viewport_size_is_none"] = page.viewport_size is None

        await page.set_content(PROBE_HTML)
        out["title"] = await page.title()

        # ── evaluate：innerText / textContent ──────────────────────
        out["innerText"] = await page.evaluate("document.body.innerText")
        out["textContent_has_script"] = "setTimeout" in (
            await page.evaluate("document.body.textContent")
        )

        # ── links：a 标签的 href + 文本 ────────────────────────────
        out["links"] = await page.eval_on_selector_all(
            "a", "els => els.map(e => [e.innerText, e.href])"
        )

        # ── count ──────────────────────────────────────────────────
        out["count_a"] = await page.locator("a").count()
        out["count_missing"] = await page.locator("div.nope").count()

        # ── ElementHandle 存活语义（本探针最关键的一项）────────────
        handle = await page.query_selector("#b1")
        out["handle_obtained"] = handle is not None
        await page.wait_for_timeout(900)          # 按钮已被替换
        out["handle_still_attached"] = (
            await handle.evaluate("e => e.isConnected") if handle else None
        )
        # 脱离 DOM 的 handle 还能不能 click？——决定 ref 缓存的失效边界
        try:
            await handle.click(timeout=2000)
            out["detached_handle_click"] = "ok"
        except Exception as exc:                  # noqa: BLE001
            out["detached_handle_click"] = type(exc).__name__

        # ── isTrusted：真实点击 vs evaluate 伪造 ──────────────────
        # 用 **DOM 属性**回传，而不是 window 全局变量——playwright 的
        # evaluate 默认跑 isolated world，读不到 main world 的全局变量
        # （实测：window.__x 恒为 undefined）。DOM 是两个 world 共享的，
        # 属性是唯一可靠的回传通道。
        await page.set_content(PROBE_HTML)
        await page.locator("#s1").click(timeout=5000)
        out["isTrusted_real_click"] = await page.evaluate(
            "document.body.getAttribute('data-trusted')"
        )
        await page.set_content(PROBE_HTML)
        await page.evaluate("document.getElementById('s1').click()")
        out["isTrusted_evaluate_click"] = await page.evaluate(
            "document.body.getAttribute('data-trusted')"
        )
        out["clicks_after_evaluate"] = await page.evaluate(
            "document.body.getAttribute('data-clicks')"
        )

        # ── fill + press_key ──────────────────────────────────────
        await page.set_content(PROBE_HTML)
        await page.locator("#q").fill("hello")
        out["fill_value"] = await page.locator("#q").input_value()
        await page.keyboard.press("Enter")
        out["press_key_ok"] = True

        # ── 元素不存在的异常形态（fail-closed 映射的依据）──────────
        try:
            await page.locator("#does-not-exist").click(timeout=1500)
            out["missing_click"] = "no-exception"
        except Exception as exc:                  # noqa: BLE001
            out["missing_click"] = type(exc).__name__
        try:
            await page.locator("#does-not-exist").fill("x", timeout=1500)
            out["missing_fill"] = "no-exception"
        except Exception as exc:                  # noqa: BLE001
            out["missing_fill"] = type(exc).__name__

        # ── extract 复刻：'a@href' 取属性 / 'a[]' 取全部 ──────────
        out["extract_attr"] = await page.eval_on_selector_all(
            "a", "els => els.map(e => e.getAttribute('href'))"
        )
        out["extract_text"] = await page.eval_on_selector_all(
            "a", "els => els.map(e => e.innerText)"
        )

        # ── new_page 语义（对照 obscura 的 new_tab）────────────────
        page2 = await ctx.new_page()
        out["new_page_count"] = len(ctx.pages)
        out["new_page_is_distinct"] = page2 is not page
        await page2.close()
        out["after_close_count"] = len(ctx.pages)
        await ctx.close()
    return out


async def _probe_fingerprint() -> dict[str, Any]:
    """指纹落地验证：进程内 API 真的把 fpgen 的配置下发了没有。"""
    js = """() => ({
      ua: navigator.userAgent, oscpu: navigator.oscpu,
      platform: navigator.platform, wd: navigator.webdriver,
      langs: navigator.languages, cores: navigator.hardwareConcurrency,
      intl: Intl.DateTimeFormat().resolvedOptions().locale,
      tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
      screen: [screen.width, screen.height],
      inner: [innerWidth, innerHeight], dpr: devicePixelRatio,
    })"""
    out: dict[str, Any] = {}
    async with AsyncCamoufox(
        os="windows", humanize=True, headless=False, locale="zh-CN"
    ) as browser:
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto("data:text/html,<html><body>x</body></html>")
        out["fingerprint"] = await page.evaluate(js)
    return out


async def _probe_world_isolation() -> dict[str, Any]:
    """evaluate 跑在哪个 world——决定「页面 JS 的状态」能不能读。

    playwright 的 ``evaluate`` 默认在 **isolated world** 执行：DOM 共享，
    但 ``window`` 上的全局变量是另一份，读不到。要读 main world 的状态
    必须开 camoufox 的 ``main_world_eval``（用法：``page.evaluate("mw:" + js)``）。

    这个开关直接决定 ``PageDriver.evaluate`` 原语的语义边界，
    故单列一项实测。
    """
    out: dict[str, Any] = {}
    html = ("<html><body><script>window.__mw = 'from-main-world';"
            "</script></body></html>")
    for flag in (False, True):
        async with AsyncCamoufox(
            os="windows", headless=False, locale="zh-CN", main_world_eval=flag
        ) as browser:
            ctx = await browser.new_context()
            page = await ctx.new_page()
            await page.set_content(html)
            key = f"main_world_eval={flag}"
            item: dict[str, Any] = {
                "plain_evaluate_reads_window": await page.evaluate(
                    "window.__mw === undefined ? null : window.__mw"
                ),
            }
            try:
                item["mw_prefix_reads_window"] = await page.evaluate(
                    "mw:window.__mw === undefined ? null : window.__mw"
                )
            except Exception as exc:              # noqa: BLE001
                # 开关关着时 playwright 直接拒绝 "mw:" 前缀——报错本身就是结论
                item["mw_prefix_reads_window"] = f"{type(exc).__name__}: {exc}"
            out[key] = item
    return out


async def main() -> int:
    print("=== 1. 指纹落地 ===")
    print(_dump(await _probe_fingerprint()))
    print()
    print("=== 2. world 隔离 ===")
    print(_dump(await _probe_world_isolation()))
    print()
    print("=== 3. 协议语义 ===")
    print(_dump(await _probe_protocol_semantics()))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
