"""探针四：同一动作**重复采样**——把「慢」的分布和遮挡因素摊平。

第三轮结论是「不是挂死，是慢过超时」：timeout 放到 60s 后全部返回，
headful+humanize 要 3.0s / 6.0s，而 headful+humanize=False 只要 118ms / 39ms。
但第二轮里 humanize=False 也超时过（1.5s），说明**方差很大**——单点读数不够。

本轮每个组合连做若干次，并同时读两样旁证：

- ``data-raf``：``计数:最后一帧:现在``。``现在就快追上最后一帧``= 窗口在渲染；
  ``差了几秒``= 这段时间**停帧**（被遮挡/非前台），输入处理会跟着被拖；
- ``data-pd``：事件到没到。**超时但 pd 涨了**是这轮最要命的一条——
  它意味着「报失败的动作其实点成功了」，重试就会点两次。

``page.mouse.click`` 不走命中拦截器，与 ``locator.click`` 配对，
用来算「拦截器确认」到底占多少。

用法（仓库根）::

    uv run python trajectory_pipeline/research/probes/probe_click_repeat.py

产物：同目录 ``click_repeat.json``。
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any, Awaitable, Callable

from camoufox import AsyncCamoufox, DefaultAddons
from camoufox.fingerprints import Screen
from playwright.async_api import TimeoutError as PWTimeout

#: 与生产一致（``camoufox_driver.ACTION_TIMEOUT_MS``）——本轮量的是
#: **在生产的超时口径下，多少次会超**，所以不放宽。
ACTION_MS = 5000
HARD_WAIT = 90.0
ROUNDS = 5
OUT = Path(__file__).with_name("click_repeat.json")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title></head>
<body style="margin:0">
<button id="b" style="position:fixed;left:0;top:0;width:100%;height:100%;
font-size:28px;z-index:2">立即播放</button>
<script>
const root = document.documentElement;
const put = (k, v) => root.setAttribute(k, String(v));
put('data-clicks', 0); put('data-pd', 0);
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
</script></body></html>"""

READ_JS = """() => {
  const r = document.documentElement;
  const raf = (r.getAttribute('data-raf') || '').split(':').map(Number);
  return {
    clicks: +(r.getAttribute('data-clicks') || 0),
    pd: +(r.getAttribute('data-pd') || 0),
    raf_frames: raf[0] || 0,
    raf_lag_ms: Math.round((raf[2] || 0) - (raf[1] || 0)),
  };
}"""

RESULTS: list[dict[str, Any]] = []


def _brief(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300]


class Rec:
    def __init__(self, name: str) -> None:
        self.name = name
        self.rows: list[dict[str, Any]] = []

    def row(self, **kw: Any) -> None:
        self.rows.append(kw)

    def done(self) -> None:
        RESULTS.append({"scenario": self.name, "rows": self.rows})


async def _action(rec: Rec, label: str, kind: str, round_no: int,
                  fn: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    t0 = time.perf_counter()
    status = "ok"
    msg = ""
    try:
        await asyncio.wait_for(fn(), timeout=HARD_WAIT)
    except asyncio.TimeoutError:
        status = "hard_timeout"
    except PWTimeout as exc:
        status, msg = "pw_timeout", _brief(exc)
    except Exception as exc:                      # noqa: BLE001
        status, msg = f"error:{type(exc).__name__}", _brief(exc)
    ms = round((time.perf_counter() - t0) * 1000)
    rec.row(label=label, kind=kind, round=round_no, status=status, ms=ms, msg=msg)
    return {"status": status, "ms": ms}


async def _read(page: Any) -> dict[str, Any]:
    try:
        return dict(await page.evaluate(READ_JS))
    except Exception as exc:                      # noqa: BLE001
        return {"error": _brief(exc)}


async def _locator_click(page: Any) -> None:
    await page.get_by_role("button", name="立即播放", exact=True).click(
        timeout=ACTION_MS)


async def _raw_mouse_click(page: Any) -> None:
    #: ⚠️ ``Mouse.click`` **没有** timeout 参数（第三轮踩过：TypeError）。
    await page.mouse.click(800, 500)


async def _run_combo(rec: Rec, *, headless: bool, humanize: bool) -> None:
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
        await page.set_content(PAGE.replace(
            "__TITLE__", f"hl={headless} hum={humanize}"))
        await asyncio.sleep(0.4)
        for i in range(ROUNDS):
            before = await _read(page)
            r = await _action(rec, "locator_click", "locator", i,
                              lambda: _locator_click(page))
            after = await _read(page)
            rec.row(label=f"locator_click@{i}", kind="delta", round=i,
                    status=r["status"], ms=r["ms"],
                    landed=int(after.get("pd", 0)) - int(before.get("pd", 0)),
                    clicks_delta=int(after.get("clicks", 0)) - int(before.get("clicks", 0)),
                    raf_lag_before=before.get("raf_lag_ms"),
                    raf_lag_after=after.get("raf_lag_ms"))
        for i in range(ROUNDS):
            before = await _read(page)
            r = await _action(rec, "raw_mouse_click", "mouse", i,
                              lambda: _raw_mouse_click(page))
            after = await _read(page)
            await asyncio.sleep(0.2)              # 让页面计数器落地再读
            after2 = await _read(page)
            rec.row(label=f"raw_mouse_click@{i}", kind="delta", round=i,
                    status=r["status"], ms=r["ms"],
                    landed=int(after2.get("pd", 0)) - int(before.get("pd", 0)),
                    clicks_delta=int(after2.get("clicks", 0)) - int(before.get("clicks", 0)),
                    raf_lag_before=before.get("raf_lag_ms"),
                    raf_lag_after=after2.get("raf_lag_ms"))
    finally:
        await manager.__aexit__(None, None, None)


def _summary(rec: Rec) -> None:
    for kind in ("locator", "mouse"):
        ok = [r["ms"] for r in rec.rows if r["kind"] == kind and r["status"] == "ok"]
        to = [r for r in rec.rows if r["kind"] == kind and r["status"] != "ok"]
        landed_on_timeout = [r for r in to if r.get("landed", 0) > 0]
        if not ok and not to:
            continue
        med = round(statistics.median(ok)) if ok else None
        print(f"     {kind:8s} ok={len(ok)} timeout={len(to)} "
              f"median_ok={med}ms max_ok={max(ok) if ok else None}ms "
              f"**超时但事件落地={len(landed_on_timeout)}**", flush=True)


async def main() -> None:
    env = {"playwright": version("playwright"), "camoufox": version("camoufox")}
    print("env:", env, flush=True)
    combos = [
        ("headful_humanize_on", dict(headless=False, humanize=True)),
        ("headful_humanize_off", dict(headless=False, humanize=False)),
        ("headless_humanize_on", dict(headless=True, humanize=True)),
        ("headless_humanize_off", dict(headless=True, humanize=False)),
    ]
    for name, kw in combos:
        rec = Rec(name)
        print(f"-- {name} (rounds={ROUNDS}) ...", flush=True)
        try:
            await _run_combo(rec, **kw)
        except Exception as exc:                  # noqa: BLE001
            rec.row(label="scenario", kind="error",
                    status=f"failed:{type(exc).__name__}", msg=_brief(exc))
        rec.done()
        _summary(rec)

    OUT.write_text(json.dumps({"env": env, "results": RESULTS},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    asyncio.run(main())