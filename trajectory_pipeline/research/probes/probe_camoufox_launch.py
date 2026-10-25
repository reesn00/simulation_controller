# -*- coding: utf-8 -*-
"""最小 camoufox launch 分步探针：定位 launch 卡在哪一步。"""
import asyncio
import sys
import time

sys.path.insert(0, ".")
from camoufox import AsyncCamoufox, DefaultAddons
from camoufox.fingerprints import Screen


async def step(name, coro):
    t0 = time.monotonic()
    print(f"  -> {name} ...", flush=True)
    try:
        r = await asyncio.wait_for(coro, timeout=60)
        print(f"  <- {name} ok ({time.monotonic()-t0:.1f}s)", flush=True)
        return r
    except Exception as e:
        print(f"  !! {name} FAIL ({time.monotonic()-t0:.1f}s): {type(e).__name__}: {e}", flush=True)
        raise


async def main():
    print("=== start ===", flush=True)
    t0 = time.monotonic()
    manager = AsyncCamoufox(
        os="windows",
        screen=Screen(min_width=1920, max_width=1920, min_height=1080, max_height=1080),
        window=(1920, 1080),
        locale="zh-CN",
        humanize=True,
        geoip=False,
        main_world_eval=False,
        exclude_addons=[DefaultAddons.UBO],
        headless=True,
    )
    print(f"manager 构造 ok ({time.monotonic()-t0:.1f}s)", flush=True)
    browser = await step("manager.__aenter__", manager.__aenter__())
    print("browser 拿到", flush=True)
    context = await step("browser.new_context", browser.new_context())
    page = await step("context.new_page", context.new_page())
    await step("page.goto about:blank", page.goto("about:blank"))
    r = await step("page.evaluate fingerprint", page.evaluate("() => ({ua: navigator.userAgent})"))
    print("fingerprint:", r, flush=True)
    await step("manager.__aexit__", manager.__aexit__(None, None, None))
    print(f"=== done ({time.monotonic()-t0:.1f}s) ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
