"""**现场取证**：修好之后，**失败落在哪个路由 ⇒ 那就是归属判据**。

为什么要有这个探针
------------------
2026-10-27 那批 10 条在 camofox 面上出了一条**假负样本**
（T048 的 iqiyi 被记成 ``unreachable_hard``，而同一批 T038 **打开了
同一个 iqiyi URL** ⇒ 站点是通的）。

根因在**上游把两件事塞进了一个路由**：``POST /tabs``
（``camofox_create_tab``）的 30 秒预算**同时**管着「建页」与「导航」
⇒ 那个路由失败**证明不了站点打不开**。而实测两种失败**在错误体里
同形**（都是 HTTP 500 + ``{"error":…,"retryable":false}``、**没有**
``code`` / ``recovery``）⇒ 光看错误体分不开。

⇒ 修法：**建页与导航拆成两次调用**（建空白页 + 导航）⇒
归属判据变成**走哪个路由**：

===============  ==========================  ======================
失败落在         归属                        分支
===============  ==========================  ======================
建页路由          **执行层**（页面里没有站点）  ``unresolved``（不进池）
导航路由          **站点事实**（DNS/超时/证书） ``unreachable_hard``
===============  ==========================  ======================

本探针用**生产驱动**（``CamofoxDriver.goto``）跑三条真实路径，
打印每一条抛出来的**异常类型**——因为「归因」这件事**没有门禁**，
只能现场看：

- ① 正常站点 ⇒ 不抛，且**观察读得到正文**（证明拆开没把采集链打断）；
- ② DNS 不存在的域名 ⇒ **普通** ``DriverError``（站点事实）；
- ③ 挂住不可路由的 IP ⇒ **普通** ``DriverError``（等满导航预算）。

⚠️ **「建页失败 ⇒ 执行层」这一条现场触发不到**（建空白页几乎不会失败）
⇒ 它由单测覆盖（``test_camofox_driver.py::Test归因``），本报告里标
「未实测」——**不拿单测冒充现场读数**。

⚠️ **只读、不改任何产物**。跑法（**必须在仓库根**）::

    uv run python -m trajectory_pipeline.research.probes.camofox_attribution_probe

⚠️ **联网**（真浏览器页）；第 ③ 步要等满 35 秒导航预算。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from trajectory_pipeline.executor.browser.camofox_driver import CamofoxDriver
from trajectory_pipeline.executor.browser.camofox_session import CamofoxSession
from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    DriverSessionError,
)

#: **DNS 一定解析不出来**的域名（``.invalid`` 是 RFC 2606 保留 TLD，永不解析）。
DEAD_HOST = "http://this-domain-should-not-exist-zzq7x9.invalid/"

#: **连不上但会挂住**的地址（不可路由 IP）⇒ 触发导航超时。
HANGING_HOST = "http://10.255.255.1:81/"

#: 正常站点（用来证明「拆开建页与导航」没把采集链打断）。
LIVE_HOST = "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB%20%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B"


def _out(payload: dict[str, Any]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _classify(exc: BaseException) -> dict[str, Any]:
    """把一次失败压成**归属结论**（判据是**异常类型**，不是错误文本）。"""
    if isinstance(exc, DriverSessionError):
        归属 = "执行层故障 ⇒ unresolved（不进负样本池）"
    elif isinstance(exc, DriverError):
        归属 = "站点传输层事实 ⇒ unreachable_hard（真负样本）"
    else:
        归属 = "不是驱动层异常（另说）"
    return {"exc": type(exc).__name__, "归属": 归属, "message": str(exc)[:300]}


async def _timed(label: str, coro: Any) -> dict[str, Any]:
    t0 = time.monotonic()
    row: dict[str, Any] = {"label": label}
    try:
        value = await coro
    except BaseException as exc:  # noqa: BLE001 — 探针要把失败形状原样记下
        row.update(_classify(exc))
        row["ok"] = False
    else:
        row["ok"] = True
        row["value"] = value
    row["elapsed_s"] = round(time.monotonic() - t0, 2)
    return row


async def main() -> int:
    results: list[dict[str, Any]] = []
    session = await CamofoxSession.launch()
    async with session:
        driver = CamofoxDriver(session)

        # ── ① 正常站点：导航 + 观察（证明拆开没打断采集链）──────────
        t0 = time.monotonic()
        try:
            await driver.goto(LIVE_HOST)
        except BaseException as exc:  # noqa: BLE001
            results.append({"label": "① 正常站点 · 导航", **_classify(exc),
                            "ok": False, "elapsed_s": round(time.monotonic() - t0, 2)})
        else:
            obs = await driver.observe()
            results.append({
                "label": "① 正常站点 · 导航 + 观察",
                "ok": True,
                "elapsed_s": round(time.monotonic() - t0, 2),
                "落地 URL": obs.url,
                "标题": obs.page_title,
                "正文长度": len(obs.body_text or ""),
                "降级": list(obs.degraded or []),
                "换页自愈": driver.recoveries,
            })

        # ── ② DNS 不存在的域名 ⇒ 站点事实 ────────────────────────
        results.append(await _timed(
            "② 导航到 DNS 不存在的域名（应当是站点事实）",
            driver.goto(DEAD_HOST),
        ))

        # ── ③ 挂住不可路由的 IP ⇒ 站点事实（等满导航预算）─────────
        results.append(await _timed(
            "③ 导航到不可路由 IP（应当是站点事实）",
            driver.goto(HANGING_HOST),
        ))

        # ── ④ 收尾：上游还开着哪些页 ─────────────────────────────
        try:
            tabs = await session.list_tabs()
        except Exception:  # noqa: BLE001
            tabs = []
        results.append({"label": "④ 上游还开着的页", "tabs": tabs})

    _out({"results": results})
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
