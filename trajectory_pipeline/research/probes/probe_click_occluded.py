"""探针六：**人为把窗口最小化**，验「停帧 → humanize 动画不推进 → 点击秒级」。

前五轮的结论已经收敛成一条假设，只差把它**逼出来**：

| 轮 | 读数 |
|---|---|
| 一/二 | 超时的点击里事件照样落地（`data-pd`/`data-clicks` 涨）→ 不是挂死，是慢 |
| 三 | `timeout=60s` 时全部返回：headful+humanize 3.0s / 6.0s，headful+humanize=False 118ms |
| 四 | 同一会话里 `page.mouse.click` 7s→25s→32s→61s，同期页内 `data-raf` 滞后 **62 秒**（一帧没画） |
| 五 | 窗口正常渲染时同一动作只要 0.4–1.5s（含跨屏位移）→ 上一轮的「慢」不是位移造成的 |

所以「慢」的触发条件是**窗口没有在画**（被遮挡 / 最小化 / 桌面非交互）。
`humanize` 的鼠标轨迹是浏览器侧补丁（``humanize:maxTime`` 默认 1.5s），
它的动画靠页面推进：**rAF 停 → 动画不结束 → 一次点击几十秒 → 超 5s 超时**。

本轮直接验证：**把我们自己起的浏览器窗口最小化**（只动自己起的那个，
按页面标题认窗口），量三组：

1. 正常 → 最小化 → 恢复，各点一次（humanize 开）；
2. 同一流程 humanize 关：若最小化期间仍然快，就说明**放大器是 humanize**；
3. humanize=0.3（显式 maxTime）：最大时长能不能封住这个时延。

⚠️ 只最小化/恢复**本探针自己启动的**窗口，标题带 ``PROBE_MARK``；不碰任何别的窗口。

用法（仓库根）::

    uv run python trajectory_pipeline/research/probes/probe_click_occluded.py
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
OUT = Path(__file__).with_name("click_occluded.json")

PROBE_MARK = "PROBE-MARK-click-occluded"

PAGE = f"""<!doctype html><html><head><meta charset="utf-8">
<title>{PROBE_MARK}</title>
<style>body{{margin:0}} button{{position:fixed;width:260px;height:120px;font-size:20px}}
#a{{left:0;top:0}} #b{{right:0;bottom:0}}</style></head><body>
<button id="a" role="button">按钮甲</button>
<button id="b" role="button">按钮乙</button>
<script>
const root = document.documentElement;
const put = (k, v) => root.setAttribute(k, String(v));
put('data-clicks', 0); put('data-pd', 0);
let frames = 0, lastFrame = -1;
const tick = t => {{ frames += 1; lastFrame = t; requestAnimationFrame(tick); }};
requestAnimationFrame(tick);
setInterval(() => {{
  put('data-raf', frames + ':' + Math.round(lastFrame) + ':' + Math.round(performance.now()));
}}, 200);
document.addEventListener('pointerdown', () => put('data-pd', +root.getAttribute('data-pd') + 1), true);
</script></body></html>"""

READ_JS = """() => {
  const r = document.documentElement;
  const raf = (r.getAttribute('data-raf') || '').split(':').map(Number);
  return {
    pd: +(r.getAttribute('data-pd') || 0),
    raf_lag_ms: Math.round((raf[2] || 0) - (raf[1] || 0)),
  };
}"""

SW_MINIMIZE = 6
SW_RESTORE = 9

user32 = ctypes.windll.user32
_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

RESULTS: list[dict[str, Any]] = []


def _find_window(marker: str) -> int | None:
    """按标题找**我们自己那个**窗口句柄。找不到返回 None（不猜）。"""
    hit: list[int] = []

    def cb(hwnd: int, _lparam: int) -> bool:
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


async def _read(page: Any) -> dict[str, Any]:
    try:
        return dict(await page.evaluate(READ_JS))
    except Exception as exc:                      # noqa: BLE001
        return {"error": _brief(exc)}


async def _click(page: Any, name: str) -> int:
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(
            page.get_by_role("button", name=name, exact=True).click(timeout=ACTION_MS),
            timeout=HARD_WAIT)
    except (PWTimeout, asyncio.TimeoutError):
        return -1
    return round((time.perf_counter() - t0) * 1000)


async def _sample(page: Any, rec: dict[str, Any], label: str, name: str) -> None:
    before = await _read(page)
    ms = await _click(page, name)
    after = await _read(page)
    rec["rows"].append({
        "phase": label, "target": name, "ms": ms,
        "landed": int(after.get("pd", 0)) - int(before.get("pd", 0)),
        "raf_lag_before": before.get("raf_lag_ms"),
        "raf_lag_after": after.get("raf_lag_ms"),
    })


async def _run(name: str, *, headless: bool, humanize: Any) -> None:
    rec: dict[str, Any] = {"scenario": name, "headless": headless,
                           "humanize": humanize, "rows": []}
    manager = AsyncCamoufox(
        os="windows", screen=Screen(min_width=1920, max_width=1920,
                                    min_height=1080, max_height=1080),
        window=(1920, 1080), locale="zh-CN", humanize=humanize,
        geoip=False, main_world_eval=False,
        exclude_addons=[DefaultAddons.UBO], headless=headless,
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

        await _sample(page, rec, "normal_before", "按钮甲")

        if headless or hwnd is None:
            rec["note"] = "headless 或没找到窗口——跳过最小化这步"
        else:
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            await asyncio.sleep(3.0)            # 让停帧真的发生
            await _sample(page, rec, "minimized", "按钮乙")
            user32.ShowWindow(hwnd, SW_RESTORE)
            await asyncio.sleep(1.0)
            await _sample(page, rec, "restored", "按钮甲")
    finally:
        if hwnd is not None:
            user32.ShowWindow(hwnd, SW_RESTORE)   # 无论如何恢复
        await manager.__aexit__(None, None, None)
    RESULTS.append(rec)
    print(f"-- {name} (hwnd={rec.get('hwnd_found')})", flush=True)
    for r in rec["rows"]:
        print(f"     {r['phase']:14s} {r['target']:6s} {r['ms']:7d}ms "
              f"landed={r['landed']} raf_lag {r['raf_lag_before']}"
              f"->{r['raf_lag_after']}", flush=True)


async def main() -> None:
    env = {"playwright": version("playwright"), "camoufox": version("camoufox")}
    print("env:", env, flush=True)
    plan = [
        ("headful_humanize_on", dict(headless=False, humanize=True)),
        ("headful_humanize_off", dict(headless=False, humanize=False)),
        ("headful_humanize_max0.3", dict(headless=False, humanize=0.3)),
    ]
    for name, kw in plan:
        try:
            await _run(name, **kw)
        except Exception as exc:                  # noqa: BLE001
            RESULTS.append({"scenario": name, "error": _brief(exc)})
            print(f"-- {name}: FAILED {_brief(exc)}", flush=True)
    OUT.write_text(json.dumps({"env": env, "results": RESULTS},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())