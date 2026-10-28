"""**现场取证**：M-44 裁定的判据必须建在**实测错误原文**上。

为什么要有这个探针
------------------
M-44 要把 ``unreachable_hard`` 拆成两支：

- **站点确实不可达**（DNS 失败 / 连接被拒）⇒ 留在 ``unreachable_hard``
  （真·站点性质，进负样本池）；
- **这次没打开**（超时 / 打断 / 建页失败 / 其它传输层失败）⇒ 降
  ``unresolved``（**不进池**）。

问题：**上游对这几类失败给的结构完全一样**——HTTP 500 +
``{"error": …, "retryable": false}``，**没有** ``code`` / ``recovery``
（上游 ``classifyError`` 对它们返回 ``'unknown'``）⇒ 光看错误体分不开。

⇒ 唯一可用的判据是**错误原文里的网络错误码**（``page.goto: NS_ERROR_*``）。
但「码 ⇒ 归属」这张表**不能靠猜**：必须把每种失败形态的**原文**打出来，
逐条比对，才知道哪些码真的存在、长什么样。

本探针用**生产驱动**（``CamofoxDriver.goto``）跑四条真实路径，
打印每一条的**异常类型 + 原始消息 + 上游错误体**：

=====  ==========================  ==========================
编号   目标                          期望形态
=====  ==========================  ==========================
①      正常站点                      不抛（证明判据不影响采集链）
②      DNS 解析不出来（``.invalid``）  DNS 失败
③      可达主机的关闭端口             连接被拒
④      不可路由 IP                   超时
=====  ==========================  ==========================

⚠️ **「连接被重置」现场触发不到**（需要上游恰好在导航途中断链）⇒ 该形态的
原文取自**第 5 轮的存档 error 原文**（``camofox_r5_uh_recheck_r1.json``），
本报告里标「**非本次实测**」——**不拿别处的读数冒充本次现场读数**。

⚠️ **只读、不改任何产物**（只写本探针自己的 ``.json``）。跑法
（**必须在仓库根**）::

    uv run python -m trajectory_pipeline.research.probes.camofox_nav_failure_codes

⚠️ **联网**（真浏览器页）；第 ④ 步要等满导航预算。
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from trajectory_pipeline.executor.browser.camofox_driver import CamofoxDriver
from trajectory_pipeline.executor.browser.camofox_session import CamofoxSession

#: DNS 一定解析不出来的域名（``.invalid`` 是 RFC 2606 保留 TLD，永不解析）。
DEAD_HOST = "http://this-domain-should-not-exist-zzq7x9.invalid/"

#: **可达主机上的关闭端口** ⇒ 期望 TCP RST ⇒ 连接被拒。
#: ⚠️ **实测打脸**（2026-10-27）：``example.com:81`` 给的不是 RST 而是
#: ``page.goto: Timeout 30000ms exceeded``（30.01 s）——中间设备把 SYN
#: 丢了，浏览器等满预算 ⇒ **从外面看是超时**。
#: ⇒ 「连接被拒」能不能现场触发，取决于**那一跳有没有真的回 RST**。
REFUSED_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("example.com:81（公网丢包）", "http://example.com:81/"),
    ("www.baidu.com:81（公网主机·关闭端口）", "http://www.baidu.com:81/"),
    ("1.1.1.1:81（公网 IP·关闭端口）", "http://1.1.1.1:81/"),
    ("127.0.0.1:9（本机 discard）", "http://127.0.0.1:9/"),
    ("127.0.0.1:1（本机 tcpmux）", "http://127.0.0.1:1/"),
)

#: 不可路由 IP ⇒ 等满导航预算 ⇒ 超时。
HANGING_HOST = "http://10.255.255.1:81/"

#: 正常站点（用来证明判据没把采集链打断）。
LIVE_HOST = "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB%20%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B"


def _out(payload: dict[str, Any]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


#: **浏览器网络错误码**的提取正则——只认 ``NS_ERROR_*`` 这类**机器码**，
#: 不认散文短语（上游对「超时 / 打断 / DNS」给的结构完全一样，见模块文档）。
NS_ERROR_RE = re.compile(r"\bNS_ERROR_[A-Z_]+\b")

#: Chromium 面（obscura）的等价码——同一语义、另一套词汇。
#: ⚠️ 本次**未实测**（obscura 面本轮未跑）⇒ 只作对照，不作判据。
CHROMIUM_ERROR_RE = re.compile(r"\bnet::ERR_[A-Z_]+\b")

#: 「被另一个导航打断」的**目标 URL**——定性这一形态的**关键**：
#:
#: - 目标是 ``about:neterror`` ⇒ 浏览器的**网络错误页** ⇒ 站点没打开；
#: - 目标是**真实站点 URL** ⇒ 站点**响应了**并把导航改道了（重定向）
#:   ⇒ 「这次没打开」但**站点是通的**。
#:
#: 两者都归「这次没打开」，但**证据含义完全不同**，必须分开记。
INTERRUPTED_BY_RE = re.compile(
    r"is interrupted by another navigation to\s+\"([^\"]*)\""
)


def _shape(exc: BaseException) -> dict[str, Any]:
    """把一次失败压成**可核对的形状**：类型 + 原文 + 上游错误体。"""
    message = str(exc)
    row: dict[str, Any] = {
        "exc": type(exc).__name__,
        "message": message,
        #: 原文里出现的**网络错误码**（若有）——判据表要按这些码建。
        "ns_error_codes": sorted(set(NS_ERROR_RE.findall(message))),
        "chromium_error_codes": sorted(set(CHROMIUM_ERROR_RE.findall(message))),
    }
    hit = INTERRUPTED_BY_RE.search(message)
    if hit:
        row["interrupted_by"] = hit.group(1)
    # 上游结构化错误体（``CamofoxToolError.body``）——有就带上，没有就不编。
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        row["upstream_body"] = body
    for attr in ("code", "retryable", "recovery"):
        value = getattr(exc, attr, None)
        if value is not None:
            row[attr] = value
    return row


async def _try_navigate(
    driver: CamofoxDriver, label: str, url: str
) -> dict[str, Any]:
    """跑一次导航，**把失败形状原样记下**。

    ⚠️ **每例都开一张新页**（2026-10-27 实测打脸后改）：同页连着导航时，
    **上一次失败的错误页还没落定，下一次导航就把它打断了** ⇒ 拿到的
    是「被上一例打断」，不是本例的失败形态（实测：三个「连接被拒」候选
    全部给出打断形态，且耗时 0.03 s——那不是网络往返，是本地竞态）。
    ⇒ 每例独立开页 ⇒ 各例互不污染。
    """
    t0 = time.monotonic()
    row: dict[str, Any] = {"label": label, "url": url}
    try:
        await driver.new_tab()
    except BaseException as exc:  # noqa: BLE001
        row["ok"] = False
        row["建页失败"] = str(exc)[:400]
        row["elapsed_s"] = round(time.monotonic() - t0, 2)
        return row
    try:
        await driver.goto(url)
    except BaseException as exc:  # noqa: BLE001 — 探针要把失败形状原样记下
        row["ok"] = False
        row.update(_shape(exc))
    else:
        row["ok"] = True
        obs = await driver.observe()
        row["落地 URL"] = obs.url
        row["正文长度"] = len(obs.body_text or "")
    row["elapsed_s"] = round(time.monotonic() - t0, 2)
    return row


async def main() -> int:
    results: list[dict[str, Any]] = []
    session = await CamofoxSession.launch()
    async with session:
        driver = CamofoxDriver(session)

        results.append(await _try_navigate(driver, "① 正常站点", LIVE_HOST))
        results.append(await _try_navigate(driver, "② DNS 解析失败", DEAD_HOST))
        for label, url in REFUSED_CANDIDATES:
            results.append(await _try_navigate(driver, f"③ 连接被拒候选 · {label}", url))
        results.append(await _try_navigate(driver, "④ 超时（不可路由 IP）", HANGING_HOST))

    payload = {
        "说明": (
            "④ 要等满导航预算（约 30–35 秒）。"
            "「连接被重置」现场触发不到，"
            "其原文取自第 5 轮存档 error（camofox_r5_uh_recheck_r1.json），"
            "非本次实测。"
        ),
        "results": results,
    }
    # 证据落盘（research 目录规矩：脚本 + 它读的 .jsonl/.json）。
    out_path = Path(__file__).with_name("camofox_nav_failure_codes.json")
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _out(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
