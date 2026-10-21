"""探针三：**挂死还是慢到超时**——把超时放到 60s，量真实完成时间。

第二轮（``probe_click_delivery.py``）已经把话说清楚一半：

- 超时的那次点击**事件其实落地了**（``data-pd`` / ``data-clicks`` 涨）；
- 键盘 17ms 就完成，鼠标要 1.5s+（``force=True`` 又只需 ~0.9s）；
- headful 期间 rAF 有整段**一帧不跑**（``raf_frames=0``），而 ``bring_to_front``
  有一次花了 **20.9 秒**。

于是两种解释必须分开：**(甲) 真的挂住不返回**；**(乙) 只是慢，慢过超时**。
判据很直接——把 ``timeout`` 放到 60s，看它到底多久返回、返回时事件落地了没有。
甲：永远不返回，只能外层兜底掐断；乙：会返回，且 ``ms`` 就是真实时延。

同时量三样旁证：

- **rAF 是否在跑**：页面里常驻 rAF 计数写进 ``data-raf``（``计数:最后一帧:现在``），
  读一次就知道「那段时间窗口有没有在渲染」——Firefox 对**被遮挡/非前台窗口**
  会停帧，而停帧期间输入处理也会被拖住；
- **鼠标原语 vs 定位器点击**：``page.mouse.click`` 不走命中拦截器，
  两者对比就能算出「拦截器等待」占多少；
- **``bring_to_front`` 本身的耗时**：它是浏览器响应度的温度计。

用法（仓库根）::

    uv run python trajectory_pipeline/research/probes/probe_click_latency.py

产物：同目录 ``click_latency.json``。
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

ACTION_MS = 60_000          # 本轮的关键：**给足时间**，让「慢」自己现形
HARD_WAIT = 90.0
OUT = Path(__file__).with_name("click_latency.json")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title></head>
<body style="margin:0">
<button id="b" style="position:fixed;left:0;top:0;width:100%;height:100%;
font-size:28px;z-index:2">立即播放</button>
<script>
const root = document.documentElement;
const put = (k, v) => root.setAttribute(k, String(v));
put('data-clicks', 0); put('data-pd', 0); put('data-kd', 0);
let frames = 0, lastFrame = -1;
const tick = t => { frames += 1; lastFrame = t; requestAnimationFrame(tick); };
requestAnimationFrame(tick);
setInterval(() => {
  put('data-raf', frames + ':' + Math.round(lastFrame) + ':' + Math.round(performance.now()));
}, 200);
document.getElementById('b').addEventListener('click',
  () => put('data-clicks', +root.getAttribute('data-clicks') + 1));
document.addEventListener('pointerdown',
  () => put('data-pd', +root.getAttribute('data-pd') + 1), true);
document.addEventListener('keydown',
  () => put('data-kd', +root.getAttribute('data-kd') + 1), true);
</script></body></html>"""

READ_JS = """() => {
  const r = document.documentElement;
  const raf = (r.getAttribute('data-raf') || '').split(':').map(Number);
  return {
    clicks: +(r.getAttribute('data-clicks') || 0),
    pd: +(r.getAttribute('data-pd') || 0),
    kd: +(r.getAttribute('data-kd') || 0),
    raf_frames: raf[0] || 0,
    raf_stalled_ms: Math.round((raf[2] || 0) - (raf[1] || 0)),
    vis: document.visibilityState,
    focus: document.hasFocus(),
  };
}"""

RESULTS: list[dict[str, Any]] = []


def _brief(exc: BaseException, *, full: bool = False) -> str:
    s = " ".join(str(exc).split())
    return s if full else s[:300]


class Rec:
    def __init__(self, name: str) -> None:
        self.name = name
        self.steps: list[dict[str, Any]] = []

    def value(self, label: str, value: Any) -> None:
        self.steps.append({"step": label, "kind": "value", "value": value})

    def read(self, label: str, value: dict[str, Any]) -> None:
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
    except PWTimeout as exc:                      # 60s 都没回来 = 甲
        rec.outcome(label, "pw_timeout", t0, msg=_brief(exc, full=True))
        return
    except Exception as exc:                      # noqa: BLE001
        rec.outcome(label, f"error:{type(exc).__name__}", t0, msg=_brief(exc))
        return
    rec.outcome(label, "ok", t0)


async def _read(rec: Rec, label: str, page: Any) -> dict[str, Any]:
    try:
        v = dict(await page.evaluate(READ_JS))
    except Exception as exc:                      # noqa: BLE001
        v = {"error": _brief(exc)}
    rec.read(label, v)
    return v


async def _locator_click(page: Any, *, force: bool = False) -> None:
    await page.get_by_role("button", name="立即播放", exact=True).click(
        timeout=ACTION_MS, force=force)


async def _bare(rec: Rec, *, headless: bool, humanize: bool,
                body: Callable[[Any, Rec], Awaitable[None]]) -> None:
    rec.value("headless", headless)
    rec.value("humanize", humanize)
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
        page = await ctx.new_page()
        await page.set_content(PAGE.replace("__TITLE__", rec.name))
        await body(page, rec)
    finally:
        await manager.__aexit__(None, None, None)


async def _flow_click(page: Any, rec: Rec) -> None:
    """公共流程：静置 → 读 → 定位器点击（60s）→ 读 → force 点击 → 读。"""
    await asyncio.sleep(0.4)          # 让 data-raf 至少写一轮
    await _read(rec, "before", page)
    await _try(rec, "locator_click_60s", lambda: _locator_click(page))
    await _read(rec, "after_locator_click", page)
    await _try(rec, "locator_click_force_60s",
               lambda: _locator_click(page, force=True))
    await _read(rec, "after_force_click", page)


async def _flow_mouse(page: Any, rec: Rec) -> None:
    """不走拦截器的鼠标点击：``page.mouse.click`` 直接派发。"""
    await asyncio.sleep(0.4)
    await _read(rec, "before", page)
    await _try(rec, "mouse_click_raw",
               lambda: page.mouse.click(800, 500, timeout=ACTION_MS))
    await _read(rec, "after_mouse_click", page)


async def _flow_bring_to_front(page: Any, rec: Rec) -> None:
    await asyncio.sleep(0.4)
    await _read(rec, "before", page)
    await _try(rec, "bring_to_front", page.bring_to_front)
    await _read(rec, "after_front", page)
    await _try(rec, "locator_click_60s", lambda: _locator_click(page))
    await _read(rec, "after_click", page)


async def _flow_keyboard(page: Any, rec: Rec) -> None:
    """对照：键盘通道在同一页上要多久。"""
    await asyncio.sleep(0.4)
    await _read(rec, "before", page)
    await _try(rec, "focus", lambda: page.get_by_role(
        "button", name="立即播放", exact=True).focus())
    await _try(rec, "press_enter", lambda: page.keyboard.press("Enter"))
    await _read(rec, "after_enter", page)


FLOWS: dict[str, Callable[[Any, Rec], Awaitable[None]]] = {
    "locator_click": _flow_click,
    "raw_mouse": _flow_mouse,
    "bring_to_front": _flow_bring_to_front,
    "keyboard": _flow_keyboard,
}


async def main() -> None:
    env = {"playwright": version("playwright"), "camoufox": version("camoufox")}
    print("env:", env, flush=True)

    plan = [
        ("headful_humanize_on_click", dict(headless=False, humanize=True),
         "locator_click"),
        ("headful_humanize_off_click", dict(headless=False, humanize=False),
         "locator_click"),
        ("headless_humanize_on_click", dict(headless=True, humanize=True),
         "locator_click"),
        ("headful_humanize_on_raw_mouse", dict(headless=False, humanize=True),
         "raw_mouse"),
        ("headful_humanize_off_raw_mouse", dict(headless=False, humanize=False),
         "raw_mouse"),
        ("headful_bring_to_front", dict(headless=False, humanize=True),
         "bring_to_front"),
        ("headful_keyboard", dict(headless=False, humanize=True),
         "keyboard"),
    ]
    for name, kw, flow in plan:
        rec = Rec(name)
        print(f"-- {name} ...", flush=True)
        try:
            await _bare(rec, body=FLOWS[flow], **kw)
        except Exception as exc:                  # noqa: BLE001
            rec.outcome("scenario", f"failed:{type(exc).__name__}",
                        time.perf_counter(), msg=_brief(exc))
        rec.done()
        for s in rec.steps:
            if s["kind"] == "action":
                print(f"     {s['step']:26s} {s['status']:12s} {s.get('ms')}ms",
                      flush=True)

    OUT.write_text(json.dumps({"env": env, "results": RESULTS},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())