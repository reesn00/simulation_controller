"""探针五：**位移**是不是主因——对角两个按钮交替点。

前四轮把范围收窄到三条：

1. 不是「新开 tab 才挂」：headful 下**首页同样会挂**，`headless=True` 从不挂；
2. 不是「事件送不到」：超时的点击里 `data-pd`/`data-clicks` **照样涨**；
3. `page.mouse.*` 在 headful+humanize 下 7s → 25s → 32s → 61s，同期页内
   ``data-raf`` 滞后到 **62 秒**——那段时间页面**一帧没画**。

`humanize` 是 camoufox 浏览器侧补丁（``humanize:maxTime`` 默认 1.5s，
文档写「跨窗口移动最多 1.5 秒」）。它的动画靠页面/合成器推进，于是：
**窗口被遮挡 → rAF 停 → 动画不结束 → 一次跨屏移动要几十秒**。

本轮把这一条变成可判定的：

- 页面上放**对角两个按钮**，交替点。若位移是主因，同一次会话内
  「原地再点」毫秒级、「跨屏移动」秒级——**与按钮是谁无关**；
- `headless=True` 作对照（没有合成器节流）；
- `humanize=0.3`（显式给 maxTime）作对照：**maxTime 能不能封住这个时延**，
  直接决定修法是「调 maxTime」还是「必须保证窗口在前台」；
- ``bring_to_front`` 每点前一次，作为「保住前台」的备选修法。

用法（仓库根）::

    uv run python trajectory_pipeline/research/probes/probe_click_distance.py

产物：同目录 ``click_distance.json``。
"""

from __future__ import annotations

import asyncio
import json
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

from camoufox import AsyncCamoufox, DefaultAddons
from camoufox.fingerprints import Screen
from playwright.async_api import TimeoutError as PWTimeout

#: 20s：比生产的 5s 宽松，比第三轮的 60s 收敛——要把「秒级」的分布量出来，
#: 又不至于一个场景跑十分钟。
ACTION_MS = 20_000
HARD_WAIT = 45.0
OUT = Path(__file__).with_name("click_distance.json")

#: 对角两个按钮：一个贴左上、一个贴右下，中间跨整屏。
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>two corners</title>
<style>
  body { margin: 0; }
  button { position: fixed; width: 260px; height: 120px; font-size: 20px; }
  #tl { left: 0; top: 0; }
  #br { right: 0; bottom: 0; }
</style></head><body>
<button id="tl" role="button">左上按钮</button>
<button id="br" role="button">右下按钮</button>
<script>
const root = document.documentElement;
const put = (k, v) => root.setAttribute(k, String(v));
put('data-clicks', 0); put('data-pd', 0); put('data-moves', 0);
let frames = 0, lastFrame = -1;
const tick = t => { frames += 1; lastFrame = t; requestAnimationFrame(tick); };
requestAnimationFrame(tick);
setInterval(() => {
  put('data-raf', frames + ':' + Math.round(lastFrame) + ':' + Math.round(performance.now()));
}, 200);
document.addEventListener('click', () => put('data-clicks', +root.getAttribute('data-clicks') + 1), true);
document.addEventListener('pointerdown', () => put('data-pd', +root.getAttribute('data-pd') + 1), true);
document.addEventListener('mousemove', () => put('data-moves', +root.getAttribute('data-moves') + 1), true);
</script></body></html>"""

READ_JS = """() => {
  const r = document.documentElement;
  const raf = (r.getAttribute('data-raf') || '').split(':').map(Number);
  return {
    clicks: +(r.getAttribute('data-clicks') || 0),
    pd: +(r.getAttribute('data-pd') || 0),
    moves: +(r.getAttribute('data-moves') || 0),
    raf_lag_ms: Math.round((raf[2] || 0) - (raf[1] || 0)),
  };
}"""

RESULTS: list[dict[str, Any]] = []


def _brief(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300]


async def _read(page: Any) -> dict[str, Any]:
    try:
        return dict(await page.evaluate(READ_JS))
    except Exception as exc:                      # noqa: BLE001
        return {"error": _brief(exc)}


async def _click(page: Any, name: str) -> int:
    """返回耗时毫秒；超时返回 -1（并**不抛出**，否则一个场景就断了）。"""
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(
            page.get_by_role("button", name=name, exact=True).click(
                timeout=ACTION_MS),
            timeout=HARD_WAIT)
    except (PWTimeout, asyncio.TimeoutError):
        return -1
    return round((time.perf_counter() - t0) * 1000)


async def _run(rec_name: str, *, headless: bool, humanize: Any,
               front_each: bool) -> None:
    rec: dict[str, Any] = {"scenario": rec_name, "headless": headless,
                           "humanize": humanize, "rows": []}
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
        await page.set_content(PAGE)
        await asyncio.sleep(0.5)
        # 交替点：左上(→右下→左上…)，即**每一次都是跨屏位移**
        for i, name in enumerate(["左上按钮", "右下按钮"] * 2):
            if front_each:
                await page.bring_to_front()
            before = await _read(page)
            ms = await _click(page, name)
            after = await _read(page)
            rec["rows"].append({
                "round": i, "target": name, "ms": ms,
                "landed": int(after.get("pd", 0)) - int(before.get("pd", 0)),
                "moves_delta": int(after.get("moves", 0)) - int(before.get("moves", 0)),
                "raf_lag_before": before.get("raf_lag_ms"),
                "raf_lag_after": after.get("raf_lag_ms"),
            })
    finally:
        await manager.__aexit__(None, None, None)
    RESULTS.append(rec)
    print(f"-- {rec_name}", flush=True)
    for r in rec["rows"]:
        print(f"     {r['target']:6s} {r['ms']:7d}ms landed={r['landed']} "
              f"mousemove={r['moves_delta']} raf_lag {r['raf_lag_before']}"
              f"->{r['raf_lag_after']}", flush=True)


async def main() -> None:
    env = {"playwright": version("playwright"), "camoufox": version("camoufox")}
    print("env:", env, flush=True)
    plan: list[tuple[str, dict[str, Any]]] = [
        ("headful_humanize_on", dict(headless=False, humanize=True, front_each=False)),
        ("headful_humanize_max0.3", dict(headless=False, humanize=0.3, front_each=False)),
        ("headful_humanize_on_front", dict(headless=False, humanize=True, front_each=True)),
        ("headless_humanize_on", dict(headless=True, humanize=True, front_each=False)),
        ("headful_humanize_off", dict(headless=False, humanize=False, front_each=False)),
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