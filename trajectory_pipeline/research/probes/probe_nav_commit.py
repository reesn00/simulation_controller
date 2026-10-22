"""**动作之后页面落没落定**的现场读数（2026-10-22 新增探针）。

这个探针验的是驱动层那三件事：

1. 点击**触发了导航**时，驱动等到了新文档 commit（``last_action["nav"]=="new"``），
   且**观察落在新页上**（不是「旧文档正在被替换」的那一瞬）；
2. 点击**没触发导航**时，账上写 ``same``——不是 ``new``（那会把「没跳页」
   说成「跳了」）；
3. 观察侧**读数自检**：读数跨了文档就重来（``nav_unsettled`` 降级）。

⚠️ **它验不了「那个 0 是不是页面事实」**——那要人读现场。
它只报读数：**标题空不空、降级里有没有 ``nav_unsettled``、导航计数变没变**。

⚠️ **必须在仓库根跑**（要 import 项目包）。

用法::

    python trajectory_pipeline/research/probes/probe_nav_commit.py --url https://www.baidu.com/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from trajectory_pipeline.executor.browser.camoufox_driver import (
    DEGRADED_NAV_UNSETTLED,
    CamoufoxDriver,
)
from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession


def _p(text: str) -> None:
    print(text, flush=True)


def _utf8_console() -> None:
    """把标准输出改成 utf-8（Windows 控制台默认 GBK：中文花屏、``⇒`` 直接抛）。

    探针的读数是要给人看的，打不出来就等于没量。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):    # pragma: no cover - 老解释器
            pass


async def _probe(driver: CamoufoxDriver, url: str, *, click_name: str) -> dict[str, Any]:
    """进站 → 观察 → 点一个东西 → 再观察。返回读数。"""
    out: dict[str, Any] = {"url": url, "click_name": click_name}
    t0 = time.perf_counter()
    await driver.goto(url)
    out["goto_s"] = round(time.perf_counter() - t0, 2)

    before = await driver.observe()
    out["before"] = {
        "url": before.url,
        "title": before.page_title,
        "video": before.video_tag_count,
        "iframe": before.iframe_count,
        "degraded": list(before.degraded),
    }

    # 挑一个能点的：优先「播放/立即观看」，否则第一个 link/button。
    picked = None
    for el in before.interactive_elements:
        if el.role not in ("link", "button"):
            continue
        if click_name and click_name not in el.name:
            continue
        picked = el
        break
    if picked is None:
        out["error"] = "本页没有可点的 link/button"
        return out

    t1 = time.perf_counter()
    await driver.click(role=picked.role, name=picked.name)
    out["click_s"] = round(time.perf_counter() - t1, 2)
    out["action"] = dict(driver.last_action)

    # 观察前后各取一次**文档代号**（私有的，但探针就是量驱动的内部判据，
    # 走公开接口就量不到「代号变没变」了）。
    stamp_before = await driver._doc_stamp()
    after = await driver.observe()
    stamp_after = await driver._doc_stamp()
    out["stamp"] = {
        "before": stamp_before,
        "after": stamp_after,
        "changed": bool(stamp_before) and bool(stamp_after)
        and stamp_before != stamp_after,
    }
    out["after"] = {
        "url": after.url,
        "title": after.page_title,
        "video": after.video_tag_count,
        "iframe": after.iframe_count,
        "degraded": list(after.degraded),
    }
    out["title_empty"] = bool(after.url) and not after.page_title
    out["nav_unsettled"] = DEGRADED_NAV_UNSETTLED in after.degraded
    return out


async def _run(args: argparse.Namespace) -> list[dict[str, Any]]:
    session = await CamoufoxSession.launch(headless=not args.headful)
    results: list[dict[str, Any]] = []
    async with session:
        driver = CamoufoxDriver(session)
        for url in args.url:
            try:
                res = await _probe(driver, url, click_name=args.click_name)
            except Exception as exc:            # 现场坏了就照实记
                res = {"url": url, "error": f"{type(exc).__name__}: {exc}"}
            results.append(res)
            _p(json.dumps(res, ensure_ascii=False, indent=2))
    return results


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(description="动作之后页面落没落定的现场读数")
    ap.add_argument("--url", action="append", default=[],
                    help="要探的地址（可多次）")
    ap.add_argument("--click-name", default="",
                    help="只点名字含这个串的元素（缺省点第一个 link/button）")
    ap.add_argument("--headful", action="store_true", help="有头浏览器（缺省无头）")
    ap.add_argument("--out", default="", help="把读数写成 JSON")
    args = ap.parse_args(argv)
    if not args.url:
        ap.error("至少给一个 --url")

    results = asyncio.run(_run(args))
    bad_title = sum(1 for r in results if r.get("title_empty"))
    unsettled = sum(1 for r in results if r.get("nav_unsettled"))
    _p(f"\n合计 {len(results)} 条：标题空 {bad_title} · nav_unsettled {unsettled}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=2)
        _p(f"已写 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
