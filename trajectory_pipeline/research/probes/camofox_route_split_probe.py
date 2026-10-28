"""**现场取证**：上游「建页」与「导航」两个路由的失败形状能不能分开。

为什么要有这个探针
------------------
2026-10-27 那批 10 条（``plan-camoufox-prod1-rest.json``）在 camofox 面上
出了一条**假负样本**：T048 的 iqiyi 被记成 ``unreachable_hard``
（「导航失败（传输层）…tab create timed out after 30000ms」），
而同一批的 T038 **打开了同一个 iqiyi URL** ⇒ 站点是通的 ⇒ 那条判错了。

根因在**上游的路由划分**（``server.js``）：

- ``POST /tabs``（``camofox_create_tab``）——
  **建页 + 导航**塞在**同一个 30 秒预算**里
  （``withTimeout(..., tabCreateRequestTimeoutMs(), 'tab create')``，server.js:3051）。
  ⇒ 这个路由失败**证明不了站点打不开**（也可能是建页本身卡住）。
- ``POST /tabs/:tabId/navigate``（``camofox_navigate``）——
  **只做导航**，预算 ``navigationRequestTimeoutMs()``（35000ms）。
  ⇒ 这个路由失败**才是**「这个 URL 打开不了」。

⚠️ **两种失败在错误体里同形**（实测，2026-10-27）：
DNS 失败与建页超时都返回 **HTTP 500** + ``{"error":…, "retryable":false}``，
**没有** ``code`` / ``recovery`` ⇒ **光看错误体分不开**。

⇒ 唯一能分开的是**走哪个路由**。本探针测三件事：

1. ``camofox_create_tab`` **不给 url** 能不能建出**空白页**（上游
   ``server.js:2990`` 的 ``if (url)`` 说明可以不导航）；
2. 空白页 + ``camofox_navigate`` 打 DNS 不存在的域名 → 错误形状；
3. 建页（带 url）打同一个域名 → 错误形状（**对照**）。

⚠️ **只读、不改任何产物**。跑法（**必须在仓库根**）::

    uv run python -m trajectory_pipeline.research.probes.camofox_route_split_probe

⚠️ **联网**（真浏览器页）；第 3 步会等满 30 秒超时。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from trajectory_pipeline.executor.browser.camofox_session import (
    CamofoxSession,
    CamofoxToolError,
    TOOL_CREATE_TAB,
    TOOL_NAVIGATE,
)

#: **DNS 一定解析不出来**的域名（``.invalid`` 是 RFC 2606 保留 TLD，永不解析）。
DEAD_HOST = "http://this-domain-should-not-exist-zzq7x9.invalid/"

#: **连不上但会挂住**的地址（不可路由 IP）⇒ 触发 30 秒导航超时。
HANGING_HOST = "http://10.255.255.1:81/"


def _out(payload: dict[str, Any]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _shape(exc: BaseException) -> dict[str, Any]:
    """把一次失败压成**可比的结构**（不猜、不补默认值）。"""
    row: dict[str, Any] = {"exc": type(exc).__name__, "message": str(exc)[:300]}
    if isinstance(exc, CamofoxToolError):
        row.update(
            tool=exc.tool,
            status=exc.status,
            code=exc.code,
            retryable=exc.retryable,
            recovery=exc.recovery,
            body=exc.body,
        )
    return row


async def _timed(label: str, coro: Any) -> tuple[dict[str, Any], Any]:
    t0 = time.monotonic()
    value: Any = None
    try:
        value = await coro
    except BaseException as exc:  # noqa: BLE001 — 探针要把失败形状原样记下
        row = _shape(exc)
        row["ok"] = False
    else:
        row = {"ok": True, "value": value}
    row["label"] = label
    row["elapsed_s"] = round(time.monotonic() - t0, 2)
    return row, value


async def main() -> int:
    results: list[dict[str, Any]] = []
    session = await CamofoxSession.launch()
    async with session:
        # ── ① 空白页建页（**不给 url**）────────────────────────────
        row, tab = await _timed(
            "① camofox_create_tab（无 url）",
            session.call(TOOL_CREATE_TAB, {}),
        )
        results.append(row)
        blank_tab = tab.get("tabId") if isinstance(tab, dict) else None

        # ── ② 空白页 + 导航到 DNS 不存在的域名 ─────────────────────
        if blank_tab:
            row, _ = await _timed(
                "② camofox_navigate → DNS 不存在",
                session.call(TOOL_NAVIGATE, {"tabId": blank_tab, "url": DEAD_HOST}),
            )
            results.append(row)
        else:
            results.append({"label": "② camofox_navigate → DNS 不存在", "skipped": "没有空白页"})

        # ── ③ 建页（**带 url**）打同一个域名 → 对照 ─────────────────
        row, _ = await _timed(
            "③ camofox_create_tab（带 DNS 不存在的 url）",
            session.call(TOOL_CREATE_TAB, {"url": DEAD_HOST}),
        )
        results.append(row)

        # ── ④ 空白页 + 导航到挂住地址 → 导航超时长什么样 ─────────────
        row, _ = await _timed(
            "④ camofox_navigate → 不可路由 IP（等 30s 超时）",
            session.call(TOOL_NAVIGATE, {"tabId": blank_tab, "url": HANGING_HOST})
            if blank_tab
            else asyncio.sleep(0),
        )
        results.append(row)

        # ── ⑤ 建页（带 url）打挂住地址 → 建页超时长什么样 ────────────
        row, _ = await _timed(
            "⑤ camofox_create_tab（带挂住的 url）",
            session.call(TOOL_CREATE_TAB, {"url": HANGING_HOST}),
        )
        results.append(row)

        # ── ⑥ 收尾：把还开着的页关掉 ─────────────────────────────
        try:
            tabs = await session.list_tabs()
        except Exception:  # noqa: BLE001
            tabs = []
        results.append({"label": "收尾", "上游还开着": tabs})

    _out({"results": results})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
