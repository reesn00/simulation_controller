"""探针二：点击挂死的**因果链**——输入有没有送到内容、rAF 有没有被节流。

第一轮（``probe_new_tab_click.py``）把原先的说法打翻了三条：

- **会话首页也会挂**（局部页上 5s 超时），不是「新开页才挂」；
- ``bring_to_front()`` 之后**就能点**、``force=True`` 也能点；
- ``headless=True`` 时**不挂**。

同时它自己有个坑：计数读数是 ``undefined``。原因是 ``main_world_eval=False``
——``page.evaluate`` 跑在**独立 world**，看不见页面主 world 的 ``window.__clicks``。
本探针改用 **DOM 属性**当计数器（DOM 跨 world 共享），于是「事件有没有送到」
变成了可读的数。

三条要量的东西：

1. **送达**：`data-pd`（pointerdown）涨没涨。挂死而 ``pd=0`` = 事件没送到内容，
   不是「点击处理太慢」；
2. **前台/可见性**：``requestAnimationFrame`` 的实际帧率 + ``setTimeout`` 漂移。
   Firefox 对**非前台窗口**把 rAF 节流到 ~1Hz——而 camoufox 的 ``humanize``
   靠 rAF 动画驱动鼠标轨迹：**rAF 停 = 鼠标动画不结束 = click 不返回**；
3. **开关**：``humanize`` 开/关、headful/headless、``bring_to_front`` 前后，
   同一页各量一次。

用法（必须在仓库根跑）::

    uv run python trajectory_pipeline/research/probes/probe_click_delivery.py

产物：同目录 ``click_delivery.json``。
"""

from __future__ import annotations

import asyncio
import json
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any, Awaitable, Callable

from camoufox import AsyncCamoufox, DefaultAddons
from camoufox.fingerprints import Screen
from playwright.async_api import TimeoutError as PWTimeout

from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession

ACTION_MS = 1500
HARD_WAIT = 30.0
OUT = Path(__file__).with_name("click_delivery.json")

#: 页面主 world 的脚本把计数写进 DOM 属性——**跨 world 可见**，这是本轮的关键修正。
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title></head>
<body style="margin:0">
<button id="b" style="position:fixed;left:0;top:0;width:100%;height:100%;
font-size:28px;z-index:2">立即播放</button>
<script>
const root = document.documentElement;
root.setAttribute('data-clicks', '0');
root.setAttribute('data-pd', '0');
root.setAttribute('data-kd', '0');
root.setAttribute('data-scripts', String(document.scripts.length));
document.getElementById('b').addEventListener('click', () => {
  root.setAttribute('data-clicks', String(+root.getAttribute('data-clicks') + 1));
});
document.addEventListener('pointerdown', () => {
  root.setAttribute('data-pd', String(+root.getAttribute('data-pd') + 1));
}, true);
document.addEventListener('keydown', () => {
  root.setAttribute('data-kd', String(+root.getAttribute('data-kd') + 1));
}, true);
</script></body></html>"""

COUNTERS_JS = """() => {
  const r = document.documentElement;
  return {
    clicks: r.getAttribute('data-clicks'),
    pd: r.getAttribute('data-pd'),
    kd: r.getAttribute('data-kd'),
    scripts: r.getAttribute('data-scripts'),
    vis: document.visibilityState,
    focus: document.hasFocus(),
    screenXY: [window.screenX, window.screenY],
    outer: [window.outerWidth, window.outerHeight],
  };
}"""

#: rAF 帧数 + setTimeout 漂移。两个一起量才能区分「rAF 单独被节流」与
#: 「整个页面被冻住」——前者是 Firefox 对非前台窗口的正常行为。
RATE_JS = """() => new Promise(resolve => {
  const t0 = performance.now();
  let frames = 0;
  const tick = () => {
    frames += 1;
    if (performance.now() - t0 < 600) requestAnimationFrame(tick);
  };
  setTimeout(() => {
    const elapsed = Math.round(performance.now() - t0);
    resolve({raf_frames: frames, raf_ms: elapsed,
             timer_drift_ms: elapsed - 600});
  }, 600);
  requestAnimationFrame(tick);
})"""

RESULTS: list[dict[str, Any]] = []


def _brief(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:240]


class Rec:
    def __init__(self, name: str) -> None:
        self.name = name
        self.steps: list[dict[str, Any]] = []

    def value(self, label: str, value: Any) -> None:
        self.steps.append({"step": label, "kind": "value", "value": value})

    def probe(self, label: str, value: dict[str, Any]) -> None:
        self.steps.append({"step": label, "kind": "read", **value})

    def outcome(self, label: str, status: str, t0: float, **extra: Any) -> None:
        self.steps.append({"step": label, "kind": "action", "status": status,
                           "ms": round((time.perf_counter() - t0) * 1000), **extra})

    def done(self) -> None:
        RESULTS.append({"scenario": self.name, "steps": self.steps})


async def _try(rec: Rec, label: str, fn: Callable[[], Awaitable[Any]]) -> None:
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(fn(), timeout=HARD_WAIT)
    except asyncio.TimeoutError:
        rec.outcome(label, "hard_timeout", t0)
        return
    except PWTimeout as exc:
        rec.outcome(label, "pw_timeout", t0, msg=_brief(exc))
        return
    except Exception as exc:                          # noqa: BLE001
        rec.outcome(label, f"error:{type(exc).__name__}", t0, msg=_brief(exc))
        return
    rec.outcome(label, "ok", t0)


async def _read(rec: Rec, label: str, page: Any) -> dict[str, Any]:
    try:
        v = dict(await page.evaluate(COUNTERS_JS))
    except Exception as exc:                          # noqa: BLE001
        v = {"error": _brief(exc)}
    rec.probe(label, v)
    return v


async def _rate(rec: Rec, label: str, page: Any) -> None:
    try:
        v = await asyncio.wait_for(page.evaluate(RATE_JS), timeout=HARD_WAIT)
        rec.probe(label, dict(v))
    except Exception as exc:                          # noqa: BLE001
        rec.probe(label, {"error": _brief(exc)})


async def _click(page: Any, *, force: bool = False) -> None:
    """**按生产口径点**：``get_by_role(role, name, exact=True)``（driver.click 的形状）。"""
    await page.get_by_role("button", name="立即播放", exact=True).click(
        timeout=ACTION_MS, force=force)


async def _fill_page(session_page: Any, title: str) -> None:
    await session_page.set_content(PAGE.replace("__TITLE__", title))


# ── 场景 ────────────────────────────────────────────────────────


async def sc_headful_page_flow(session: CamoufoxSession, rec: Rec) -> None:
    """单页：先量帧率与送达，再点；然后 ``bring_to_front`` 再量再点。

    「挂死时 ``pd`` 是 0，``bring_to_front`` 后 ``pd`` 涨了」——这一条就把
    「输入没送到内容」钉死；帧率则回答「为什么没送到」。
    """
    p = session.page
    await _fill_page(p, "one")
    await _read(rec, "before", p)
    await _rate(rec, "rate_before", p)
    await _try(rec, "click_before", lambda: _click(p))
    await _read(rec, "after_click_before", p)

    await _try(rec, "bring_to_front", p.bring_to_front)
    await _read(rec, "after_front", p)
    await _rate(rec, "rate_after_front", p)
    await _try(rec, "click_after_front", lambda: _click(p))
    await _read(rec, "after_click_front", p)


async def sc_two_pages(session: CamoufoxSession, rec: Rec) -> None:
    """两页：p2 开出来之后（p1 已前台过）——重点看 ``screenXY`` 是否相同。

    ``screenXY`` 不同 = 两页是**两个窗口**（不是同一窗口的两个 tab），
    那么「新 tab 挂」这个说法本身就要改。
    """
    p1 = session.page
    await _fill_page(p1, "first")
    await _try(rec, "p1_bring_to_front", p1.bring_to_front)
    await _try(rec, "p1_click", lambda: _click(p1))
    await _read(rec, "p1_after_click", p1)

    p2 = await session.new_page()
    await _fill_page(p2, "second")
    r2 = await _read(rec, "p2_before", p2)
    await _rate(rec, "p2_rate_before", p2)
    await _try(rec, "p2_click_before", lambda: _click(p2))
    await _read(rec, "p2_after_click_before", p2)

    await _try(rec, "p2_bring_to_front", p2.bring_to_front)
    await _rate(rec, "p2_rate_after_front", p2)
    await _try(rec, "p2_click_after_front", lambda: _click(p2))
    r3 = await _read(rec, "p2_after_click_front", p2)
    rec.value("same_window", r2.get("screenXY") == r3.get("screenXY"))


async def sc_keyboard_delivery(session: CamoufoxSession, rec: Rec) -> None:
    """键盘通道：不经过指针拦截器。焦点给了按钮再按 Enter。

    键盘落地而鼠标不落地 → 问题在**指针派发/拦截**这一侧，不在「页面收不到事件」。
    """
    p = session.page
    await _fill_page(p, "kbd")
    await _try(rec, "focus", lambda: p.get_by_role(
        "button", name="立即播放", exact=True).focus())
    await _try(rec, "press_enter", lambda: p.keyboard.press("Enter"))
    await _read(rec, "after_enter", p)
    await _try(rec, "bring_to_front", p.bring_to_front)
    await _try(rec, "press_enter_after_front", lambda: p.keyboard.press("Enter"))
    await _read(rec, "after_enter_front", p)


async def _bare_run(rec: Rec, *, headless: bool, humanize: bool) -> None:
    """不走项目封装：``AsyncCamoufox`` 直接起，只测 humanize 这一个开关。"""
    manager = AsyncCamoufox(
        os="windows", screen=Screen(min_width=1920, max_width=1920,
                                    min_height=1080, max_height=1080),
        window=(1920, 1080), locale="zh-CN", humanize=humanize,
        geoip=False, main_world_eval=False,
        exclude_addons=[DefaultAddons.UBO], headless=headless,
    )
    browser = await manager.__aenter__()
    try:
        ctx = await browser.new_context()
        p = await ctx.new_page()
        await _fill_page(p, f"humanize={humanize} headless={headless}")
        await _read(rec, "before", p)
        await _rate(rec, "rate_before", p)
        await _try(rec, "click_before", lambda: _click(p))
        await _read(rec, "after_click_before", p)
        await _try(rec, "bring_to_front", p.bring_to_front)
        await _try(rec, "click_after_front", lambda: _click(p))
        await _read(rec, "after_click_front", p)
    finally:
        await manager.__aexit__(None, None, None)


async def main() -> None:
    env = {
        "playwright": version("playwright"),
        "camoufox": version("camoufox"),
    }
    print("env:", env, flush=True)

    plan: list[tuple[str, Any]] = [
        ("headful_page_flow", _wrap_session(sc_headful_page_flow, headless=False)),
        ("two_pages", _wrap_session(sc_two_pages, headless=False)),
        ("keyboard_delivery", _wrap_session(sc_keyboard_delivery, headless=False)),
        ("headless_page_flow", _wrap_session(sc_headful_page_flow, headless=True)),
        ("bare_humanize_on_headful",
         _wrap_bare(headless=False, humanize=True)),
        ("bare_humanize_off_headful",
         _wrap_bare(headless=False, humanize=False)),
        ("bare_humanize_on_headless",
         _wrap_bare(headless=True, humanize=True)),
    ]
    for name, runner in plan:
        rec = Rec(name)
        print(f"-- {name} ...", flush=True)
        try:
            await runner(rec)
        except Exception as exc:                      # noqa: BLE001
            rec.outcome("scenario", f"failed:{type(exc).__name__}",
                        time.perf_counter(), msg=_brief(exc))
        rec.done()
        for s in rec.steps:
            if s["kind"] == "action":
                print(f"     {s['step']:24s} {s['status']:14s} {s.get('ms')}ms",
                      flush=True)

    OUT.write_text(json.dumps({"env": env, "results": RESULTS},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n-> {OUT}")


def _wrap_session(fn: Callable[[CamoufoxSession, Rec], Awaitable[None]],
                  *, headless: bool) -> Callable[[Rec], Awaitable[None]]:
    async def run(rec: Rec) -> None:
        rec.value("headless", headless)
        session = await CamoufoxSession.launch(headless=headless)
        try:
            await fn(session, rec)
        finally:
            await session.close()
    return run


def _wrap_bare(*, headless: bool, humanize: bool) -> Callable[[Rec], Awaitable[None]]:
    async def run(rec: Rec) -> None:
        rec.value("headless", headless)
        rec.value("humanize", humanize)
        await _bare_run(rec, headless=headless, humanize=humanize)
    return run


if __name__ == "__main__":
    asyncio.run(main())