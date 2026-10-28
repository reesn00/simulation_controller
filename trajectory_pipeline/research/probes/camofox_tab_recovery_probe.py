"""**现场取证**：上游把当前页销毁之后，驱动能不能自愈。

为什么要有这个探针
------------------
2026-10-27 那批 10 条（``plan-camoufox-prod1-rest.json``）在 camofox 面上
出了**第四个零报错缺陷**：一个候选**连炸 9 次**，9 次的失败原因一字不差是

    导航失败（传输层）: camofox 导航失败: camofox tool camofox_navigate
    返回错误: camofox error: 404: {"error":"Tab not found"}

而这 9 次全被记成 ``unreachable_hard``（「站点打不开」）⇒
**九条理由通顺的假负样本进了池**，而那个站点**一次都没被试过**。

根因（读上游 ``server.js`` 得到，**上游不许改**）：上游在**操作超时 /
连续超时 / 页崩溃 / 锁队列超时**时**销毁那一页**
（``destroyTab`` / ``destroyTimedOutTab``），而**销毁不通知客户端** ⇒
客户端手里的 ``tabId`` 成了死引用 ⇒ 再导航就是 404，**每一次都 404**。

本探针**在真上游上复现这个状态**并量修好之后的行为：
把当前页**在上游那一侧**关掉（本地栈照旧攥着它），
然后看驱动会不会换一张新页、用同一个 URL 重来一次。

⚠️ **只读、不改任何产物**。跑法（**必须在仓库根**）::

    uv run python -m trajectory_pipeline.research.probes.camofox_tab_recovery_probe

⚠️ **联网**（真浏览器页）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from trajectory_pipeline.executor.browser.camofox_driver import CamofoxDriver
from trajectory_pipeline.executor.browser.camofox_session import (
    TOOL_CLOSE_TAB,
    CamofoxSession,
)

#: 探针用的目标页（这批批次里跑得通的一个）。
PROBE_URL = "https://www.baidu.com/"


def _out(payload: dict[str, Any]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


async def _probe(url: str) -> dict[str, Any]:
    session = await CamofoxSession.launch()
    async with session:
        driver = CamofoxDriver(session)
        rows: list[dict[str, Any]] = []

        # ── ① 先正常开一页 ──────────────────────────────────────────
        await driver.goto(url)
        first_tab = session.current
        rows.append({
            "step": "① 正常导航",
            "tab": first_tab,
            "stack": list(session.pages),
            "recoveries": len(driver.recoveries),
        })

        # ── ② 在**上游那一侧**把这一页关掉 ─────────────────────────
        # ⚠️ 走的是**裸 tool 调用**：`close_page` 会顺手把本地栈也摘掉，
        # 而这里要的正是「上游没了、本地还攥着」那个状态。
        await session.call(TOOL_CLOSE_TAB, {"tabId": first_tab})
        rows.append({
            "step": "② 在上游关掉这一页（本地栈照旧攥着）",
            "tab": first_tab,
            "stack": list(session.pages),
            "note": "本地栈还攥着它——这正是上游销毁页之后的真实状态",
        })

        # ── ③ 再导航一次：修好之后应当换新页重来 ──────────────────
        ok = True
        error = ""
        try:
            await driver.goto(url)
        except Exception as exc:      # noqa: BLE001 —— 探针要看见失败
            ok = False
            error = f"{type(exc).__name__}: {exc}"
        rows.append({
            "step": "③ 再导航同一个 URL",
            "ok": ok,
            "error": error,
            "stack": list(session.pages),
            "recoveries": len(driver.recoveries),
        })

        # ── ④ 自愈之后那一页**真的加载出来了吗** ───────────────────
        title = ""
        body_len = 0
        if ok:
            snap = await driver.snapshot()
            title = snap.title
            body_len = len(snap.body or "")

        return {
            "url": url,
            "rows": rows,
            "recoveries": driver.recoveries,
            "final_tab": session.current,
            "final_stack": list(session.pages),
            "title": title,
            "body_len": body_len,
            "verdict": (
                "自愈成功：上游那一页被关掉之后，驱动换了一张新页、"
                "用同一个 URL 重来一次，页面真的加载出来了"
                if ok and driver.recoveries else
                "自愈失败（见上面各步）"
            ),
        }


async def _main() -> int:
    result = await _probe(PROBE_URL)
    _out(result)
    return 0 if result["recoveries"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
