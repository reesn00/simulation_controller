"""**现场取证**：camofox 面在搜索页上「读到一半」（``nav_unsettled``）的成因。

为什么要有这个探针
------------------
2026-10-27 那批 10 条（``plan-camoufox-prod1-rest.json``）在 camofox 面上
**每一条都 0 候选**：``环节① 取回候选 0 个（links=99，过滤记账 {'degraded': 1}）``，
而存档里 ``search_observation.degraded == ['nav_unsettled']``。

现象本身是**对的**（代码层 fail-closed：读到半个文档就不判），
但**成因**必须分清——两种处置完全不同：

- 「页面一直在换文档」⇒ **等**（重试 / 等落定），是**时序**问题；
- 「文档代号读不到」⇒ **能力**问题（执行上下文被销毁），是**上游**问题。

这两种在 P1 存档里**同形**（都只写 ``nav_unsettled``），
所以必须靠**现场**读数分——**存档里没有的东西，就去现场拿**。

⚠️ **只读、不改任何产物**。跑法（**必须在仓库根**）::

    uv run python -m trajectory_pipeline.research.probes.camofox_nav_probe \\
        --url "https://www.baidu.com/s?wd=功夫 在线观看"

⚠️ **联网**（真浏览器页）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from trajectory_pipeline.executor.browser.camofox_driver import CamofoxDriver
from trajectory_pipeline.executor.browser.camofox_session import CamofoxSession
from trajectory_pipeline.executor.browser.driver_factory import make_driver


def _out(payload: dict[str, Any]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


async def _probe(url: str, *, settle_wait: float) -> dict[str, Any]:
    timeline: list[dict[str, Any]] = []
    t0 = time.monotonic()

    def mark(label: str, stamp: str, extra: dict[str, Any] | None = None) -> None:
        row = {
            "at_ms": round((time.monotonic() - t0) * 1000),
            "label": label,
            "stamp": stamp,
        }
        if extra:
            row.update(extra)
        timeline.append(row)

    session = await CamofoxSession.launch()
    async with session:
        driver = make_driver(kind="camofox", camofox_session=session)
        assert isinstance(driver, CamofoxDriver)

        pre_goto = await driver._doc_stamp()
        mark("goto 前", pre_goto)
        await driver.goto(url)
        post_goto = await driver._doc_stamp()
        mark("goto 后", post_goto, {"nav": dict(getattr(driver, "last_action", {}))})

        if settle_wait:
            await asyncio.sleep(settle_wait)
            mark("静置后", await driver._doc_stamp(),
                 {"waited_s": settle_wait})

        # ── 逐步重演 observe() 的读序，**每一步之后取一次代号** ──
        stamp_before = await driver._doc_stamp()
        mark("观察前", stamp_before)

        degraded: list[str] = []
        snap = await driver.snapshot(max_chars=6000, degraded=degraded)
        mark("snapshot 后", await driver._doc_stamp(),
             {"body_len": len(snap.body), "body_source": snap.body_source,
              "degraded": list(degraded)})

        links = await driver.links()
        mark("links 后", await driver._doc_stamp(), {"links": len(links)})

        elements = await driver.interactive()
        mark("interactive 后", await driver._doc_stamp(),
             {"elements": len(elements),
              "enumeration": dict(getattr(driver, "last_enumeration", {}))})

        stamp_after = await driver._doc_stamp()
        mark("观察后", stamp_after)

        return {
            "url": url,
            "timeline": timeline,
            "torn": (not stamp_before) or (not stamp_after)
                    or stamp_before != stamp_after,
            "stamp_before": stamp_before,
            "stamp_after": stamp_after,
            "degraded": list(degraded),
        }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="camofox nav_unsettled 现场取证")
    ap.add_argument("--url", required=True, help="要探的页面")
    ap.add_argument("--settle-wait", type=float, default=0.0,
                    help="goto 之后静置的秒数（0 = 不等）")
    args = ap.parse_args(argv)
    result = asyncio.run(_probe(args.url, settle_wait=args.settle_wait))
    _out(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
