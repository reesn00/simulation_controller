"""**现场取证**：拆开「建页」与「导航」之后，导航还能不能正常跑。

为什么要有这个探针
------------------
2026-10-27 那批 10 条在 camofox 面上出了一条**假负样本**
（T048 的 iqiyi 被记成 ``unreachable_hard``，而同一批 T038 打开了同一个 URL）。

根因在**上游把两件事塞进了一个路由**：``POST /tabs`` 的 30 秒预算
**同时**管着「建页」和「导航」⇒ 这个路由失败**证明不了站点打不开**。

⇒ 修法的前提是：**建页（空白页）与导航能不能分成两次调用？**
上一个探针（``camofox_route_split_probe``）已测出**能**（空白页建出来是
``about:blank``）。本探针测**剩下那一半**：拆开之后导航还正不正常——
上游的 ``navigate`` 路由要求那一页**已注册在会话里**，
所以要确认「空白页 → 导航」这条链路上报回来的东西（``httpStatus`` /
``navigationOk`` / 落地 URL）与原来「建页带 URL」**一样**。

⚠️ **只读、不改任何产物**。跑法（**必须在仓库根**）::

    uv run python -m trajectory_pipeline.research.probes.camofox_split_nav_probe \\
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

from trajectory_pipeline.executor.browser.camofox_session import (
    CamofoxSession,
    TOOL_CREATE_TAB,
    TOOL_NAVIGATE,
)


def _out(payload: dict[str, Any]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True, help="要导航过去的 URL")
    args = ap.parse_args(argv)

    results: list[dict[str, Any]] = []
    session = await CamofoxSession.launch()
    async with session:
        # ── ① 空白页（**不带 url**）───────────────────────────────
        t0 = time.monotonic()
        blank = await session.call(TOOL_CREATE_TAB, {})
        results.append({
            "step": "① 建空白页",
            "elapsed_s": round(time.monotonic() - t0, 2),
            "payload": blank,
        })
        tab_id = blank.get("tabId") if isinstance(blank, dict) else None

        # ── ② 在这张空白页上导航 ─────────────────────────────────
        t0 = time.monotonic()
        nav = await session.call(TOOL_NAVIGATE, {"tabId": tab_id, "url": args.url})
        results.append({
            "step": "② 空白页 → 导航",
            "elapsed_s": round(time.monotonic() - t0, 2),
            "payload": nav,
        })

        # ── ③ 同一张页再导航一次（**第二次**路由往返）─────────────
        t0 = time.monotonic()
        nav2 = await session.call(TOOL_NAVIGATE, {"tabId": tab_id, "url": args.url})
        results.append({
            "step": "③ 同一页再导航一次",
            "elapsed_s": round(time.monotonic() - t0, 2),
            "payload": nav2,
        })

        # ── ④ 收尾：上游还开着哪些页 ─────────────────────────────
        try:
            tabs = await session.list_tabs()
        except Exception:  # noqa: BLE001
            tabs = []
        results.append({"step": "④ 上游还开着的页", "tabs": tabs})

    _out({"url": args.url, "results": results})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
