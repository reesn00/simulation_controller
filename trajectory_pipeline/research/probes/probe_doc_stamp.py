"""**文档代号**能不能区分「换了文档」与「只换了 URL」——现场量一下。

问题（2026-10-22）：主框架 ``framenavigated`` 对**同文档导航**
（pushState / 改 query）也会触发。而 SPA 页面（bilibili 等）会在播放时
改 URL ⇒ 观察窗口里撞上一次就被误判成「读到一半」⇒ 白白重来，
连续撞上就误记 ``nav_unsettled`` ⇒ **把好观察当半成品丢掉**。

候选判据：``performance.timeOrigin``——**每个文档一个常量**
（同文档内恒定、跨文档必变；同文档导航不改变它）。

⚠️ **两个未知数，本探针就是去量的**：

1. ``evaluate`` 跑在 **isolated world**（``main_world_eval=False``）——
   隔离世界的 ``performance.timeOrigin`` 是不是**文档的**那个？
2. 同文档导航（pushState）会不会改变它？

用法（**必须在仓库根跑**）::

    python trajectory_pipeline/research/probes/probe_doc_stamp.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from trajectory_pipeline.executor.browser.camoufox_driver import (
    DOC_STAMP_EXPR,
    CamoufoxDriver,
)
from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession

#: 取文档代号。**从驱动那边 import，不抄第二份**——抄两份必然漂，
#: 而漂了的症状是「代号一直在变」（每次读都是新文档），而且**很难查**：
#: 探针量的是驱动的判据，判据换了探针也得跟着换。
STAMP_EXPR = DOC_STAMP_EXPR


def _utf8_console() -> None:
    """把标准输出改成 utf-8。

    Windows 控制台默认 GBK：中文**花屏**，``⇒`` 这类符号**直接抛
    ``UnicodeEncodeError``**（探针的读数就在最后一步打不出来）。
    探针的输出是要给人看的，所以这里统一改掉。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):    # pragma: no cover - 老解释器
            pass


async def _run() -> list[dict]:
    out: list[dict] = []
    session = await CamoufoxSession.launch(headless=True)
    async with session:
        driver = CamoufoxDriver(session)
        await driver.goto("https://www.baidu.com/")
        a = await driver.evaluate(STAMP_EXPR)
        b = await driver.evaluate(STAMP_EXPR)
        out.append({"step": "同文档连读两次", "a": a, "b": b, "same": a == b})

        # 同文档导航：只改 query，不换文档。
        url_before = driver._page.url
        await driver.evaluate("() => { history.pushState({}, '', '?tp_probe=1'); }")
        url_after = driver._page.url
        c = await driver.evaluate(STAMP_EXPR)
        out.append({
            "step": "同文档 pushState 之后", "a": a, "b": c, "same": a == c,
            # ⚠️ **URL 必须真的变了**——不然这条量的是「什么都没发生」，
            # 结论 ``same=True`` 就是**空转**出来的（探针自己骗自己）。
            "url_changed": url_before != url_after,
            "url_after": url_after[:90],
        })

        # 真导航：换文档。
        await driver.goto("https://www.bilibili.com/")
        d = await driver.evaluate(STAMP_EXPR)
        out.append({"step": "真导航之后", "a": a, "b": d, "same": a == d})

        # 同一文档内再读两次（隔离世界会不会每次新建？）
        e = await driver.evaluate(STAMP_EXPR)
        out.append({"step": "新文档内连读两次", "a": d, "b": e, "same": d == e})
    return out


def main() -> int:
    _utf8_console()
    rows = asyncio.run(_run())
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    print()
    print("判读：")
    print("  ① 同文档连读两次 ⇒ same=True（否则隔离世界每次新建，代号不可用）")
    print("  ② pushState 之后 ⇒ same=True（这是它比 framenavigated 强的地方）")
    print("     ⚠️ 同时要看到 url_changed=True——URL 没变就是「什么都没发生」，")
    print("        那这条量的是空转，结论不作数。")
    print("  ③ 真导航之后   ⇒ same=False（变了才说明它认得出换文档）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
