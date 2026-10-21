"""探针七：换 **Firefox 偏好**去治「窗口不可见 → 点击秒级」，看能不能治住。

第六轮把因果钉死了：窗口**最小化**时同一次点击 humanize 开 13.3s、
humanize 关 **9.1s**、``maxTime=0.3`` **9.1s**；恢复窗口后 1.09s / 82ms / 386ms。
所以放大器不是 humanize（它只把 9s 拉到 13s），而是 **Firefox 在窗口不可见时
把整个窗口的行/渲染/输入处理都节流甚至挂起**。

于是修法有两条路，本探针各验一次：

1. **关掉 Windows 遮挡检测**（``widget.windows.window_occlusion_tracking.enabled``）：
   让 Firefox 不把「被挡住的窗口」当成可以停掉的对象。**保持 headful**
   （headless 是检测信号，方案 §4.3 有意不用）；
2. **放宽后台节流**（``dom.min_background_timeout_value`` /
   ``dom.timeout.background_throttling_max_budget``）：如果瓶颈在定时器，
   这一组会让 9s 掉下来；如果瓶颈在渲染/输入，它不动。

判定很直接：**最小化窗口后点一次，看是多少毫秒**。基线 = 9100–13300ms。

用法（仓库根）::

    uv run python trajectory_pipeline/research/probes/probe_click_occlusion_prefs.py
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import time
from ctypes import wintypes
from importlib.metadata import version
from pathlib import Path
from typing import Any

from camoufox import AsyncCamoufox, DefaultAddons
from camoufox.fingerprints import Screen
from playwright.async_api import TimeoutError as PWTimeout

ACTION_MS = 20_000
HARD_WAIT = 45.0
OUT = Path(__file__).with_name("click_occlusion_prefs.json")
PROBE_MARK = "PROBE-MARK-occlusion-prefs"

PAGE = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{PROBE_MARK}</title>
<style>body{{margin:0}} button{{position:fixed;width:260px;height:120px;font-size:20px}}
#a{{left:0;top:0}} #b{{right:0;bottom:0}}</style></head><body>
<button id="a" role="button">按钮甲</button><button id="b" role="button">按钮乙</button>
<script>
const root = document.documentElement;
root.setAttribute('data-pd', '0');
document.addEventListener('pointerdown',
  () => root.setAttribute('data-pd', +root.getAttribute('data-pd') + 1), true);
</script></body></html>"""

SW_MINIMIZE, SW_RESTORE = 6, 9
user32 = ctypes.windll.user32
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

RESULTS: list[dict[str, Any]] = []


def _find_window(marker: str) -> int | None:
    hit: list[int] = []

    def cb(hwnd: int, _l: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if marker in buf.value:
                hit.append(int(hwnd))
                return False
        return True

    user32.EnumWindows(_EnumProc(cb), 0)
    return hit[0] if hit else None


def _brief(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300]


async def _click(page: Any, name: str) -> int:
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(
            page.get_by_role("button", name=name, exact=True).click(timeout=ACTION_MS),
            timeout=HARD_WAIT)
    except (PWTimeout, asyncio.TimeoutError):
        return -1
    return round((time.perf_counter() - t0) * 1000)


async def _pd(page: Any) -> int:
    try:
        return int(await page.evaluate(
            "() => +(document.documentElement.getAttribute('data-pd') || 0)"))
    except Exception:                             # noqa: BLE001
        return -1


async def _run(name: str, prefs: dict[str, Any] | None) -> None:
    rec: dict[str, Any] = {"scenario": name, "prefs": prefs or {}, "rows": []}
    manager = AsyncCamoufox(
        os="windows", screen=Screen(min_width=1920, max_width=1920,
                                    min_height=1080, max_height=1080),
        window=(1920, 1080), locale="zh-CN", humanize=True,
        geoip=False, main_world_eval=False,
        exclude_addons=[DefaultAddons.UBO], headless=False,
        firefox_user_prefs=prefs or None,
    )
    browser = await manager.__aenter__()
    hwnd: int | None = None
    try:
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.set_content(PAGE)
        await asyncio.sleep(0.6)
        hwnd = _find_window(PROBE_MARK)
        rec["hwnd_found"] = hwnd is not None
        if hwnd is None:
            return
        rec["rows"].append({"phase": "visible", "ms": await _click(page, "按钮甲")})
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        await asyncio.sleep(3.0)
        ms = await _click(page, "按钮乙")
        rec["rows"].append({"phase": "minimized", "ms": ms,
                            "pd_after": await _pd(page)})
        user32.ShowWindow(hwnd, SW_RESTORE)
        await asyncio.sleep(1.0)
        rec["rows"].append({"phase": "restored", "ms": await _click(page, "按钮甲")})
    finally:
        if hwnd is not None:
            user32.ShowWindow(hwnd, SW_RESTORE)
        await manager.__aexit__(None, None, None)
    RESULTS.append(rec)
    print(f"-- {name}: " + ", ".join(
        f"{r['phase']}={r['ms']}ms" for r in rec["rows"]), flush=True)


async def main() -> None:
    env = {"playwright": version("playwright"), "camoufox": version("camoufox")}
    print("env:", env, flush=True)
    plan: list[tuple[str, dict[str, Any] | None]] = [
        ("baseline_no_prefs", None),
        ("occlusion_tracking_off", {
            "widget.windows.window_occlusion_tracking.enabled": False,
        }),
        ("background_throttling_off", {
            "dom.min_background_timeout_value": 4,
            "dom.timeout.background_throttling_max_budget": -1,
        }),
        ("both_off", {
            "widget.windows.window_occlusion_tracking.enabled": False,
            "dom.min_background_timeout_value": 4,
            "dom.timeout.background_throttling_max_budget": -1,
        }),
    ]
    for name, prefs in plan:
        try:
            await _run(name, prefs)
        except Exception as exc:                  # noqa: BLE001
            RESULTS.append({"scenario": name, "error": _brief(exc)})
            print(f"-- {name}: FAILED {_brief(exc)}", flush=True)
    OUT.write_text(json.dumps({"env": env, "results": RESULTS},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())