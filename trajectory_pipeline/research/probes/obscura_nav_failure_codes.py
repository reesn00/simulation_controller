"""**现场取证**：obscura 面的导航失败**原始原文**（不截断）。

为什么要有这个探针
------------------
M-44 裁定要把 ``unreachable_hard`` 收窄成「**真·站点不可达**」——
只有**机器码能证明站点本身不可达**的形态才留，其余（超时 / 打断 /
建页失败）降 ``unresolved``（不进池）。

判据建在**实测原文**上，不建在推测上。camofox 面已经实测完
（见 ``camofox_nav_failure_codes.json``），本探针补 **obscura 面**。

obscura 面的两个特殊之处
-------------------------
1. **上游不截断，但驱动层截断**——:meth:`ObscuraDriver._call` 把错误正文
   砍到 **200 字**（``text.strip()[:200]``）。而 obscura 的错误形态是
   Rust/reqwest 风格的长句，**200 字刚好砍掉关键部分**::

       Error: Network error: Network error: <url> error sending request for
       uri (<url>): client error (Connect) (source: Some(Error {

   ⇒ **判据必须在截断之前判**。本探针**绕过驱动层**，直接调 MCP tool，
   拿**完整原文**。
2. **词汇是 reqwest 的，不是 Chromium 的**——生产存档里 34 行
   ``client error (Connect)`` 覆盖了 DNS 失败 / 拒连 / TLS 握手失败
   **三种**原因，**光看这个形态分不开** ⇒ 必须看 ``source:`` 里的
   细分 kind。

⚠️ **只读、不改任何产物**（只写本探针自己的 ``.json``）。跑法
（**必须在仓库根**）::

    uv run python -m trajectory_pipeline.research.probes.obscura_nav_failure_codes

⚠️ **联网**（真浏览器页）；超时要等满导航预算（约 30 秒）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from trajectory_pipeline.executor.browser.mcp_client import McpClient

#: DNS 一定解析不出来的域名（``.invalid`` 是 RFC 2606 保留 TLD，永不解析）。
DEAD_HOST = "http://this-domain-should-not-exist-zzq7x9.invalid/"

#: 可达主机上的**关闭端口** ⇒ 期望 TCP RST。
REFUSED_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("127.0.0.1:9（本机 discard）", "http://127.0.0.1:9/"),
    ("127.0.0.1:1（本机 tcpmux）", "http://127.0.0.1:1/"),
)

#: 不可路由 IP ⇒ 等满导航预算 ⇒ 超时。
HANGING_HOST = "http://10.255.255.1:81/"

#: 正常站点（证明判据没把采集链打断）。
LIVE_HOST = "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB%20%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B"

#: reqwest / Rust ``io::Error`` 的 kind 词表——**机器词**，不是散文。
#: 实测原文里长这样（``source: Some(Error { kind: NotFound, …``）。
RUST_IO_KIND_RE = re.compile(r"\bkind:\s*([A-Za-z_]+)")
#: Rust 标准库 DNS 失败的固定短语。
DNS_PHRASE_RE = re.compile(
    r"(failed to lookup address information|"
    r"name or service not known|"
    r"nodename nor servname provided|"
    r"no such host)",
    re.I,
)


def _out(payload: dict[str, Any]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


async def _try_navigate(client: McpClient, label: str, url: str) -> dict[str, Any]:
    """跑一次导航，**把失败原文原样记下**（不截断）。

    ⚠️ **每例都开一张新页**：同页连着导航时，上一次失败的错误页还没落定，
    下一次导航就把它打断 ⇒ 拿到的是「被上一例打断」，不是本例的失败形态。
    """
    t0 = time.monotonic()
    row: dict[str, Any] = {"label": label, "url": url}
    try:
        await client.call("browser_tab_new", {})
    except BaseException as exc:  # noqa: BLE001
        row["建页失败"] = str(exc)[:400]
        row["elapsed_s"] = round(time.monotonic() - t0, 2)
        return row
    try:
        result = await client.call("browser_navigate", {"url": url})
    except BaseException as exc:  # noqa: BLE001 — 探针要把失败形状原样记下
        row["ok"] = False
        row["exc"] = type(exc).__name__
        row["message"] = str(exc)
        row["原文长度"] = len(str(exc))
    else:
        row["ok"] = not result.is_error
        row["is_error"] = result.is_error
        row["原文"] = result.text
        row["原文长度"] = len(result.text)
    text = row.get("message") or row.get("原文") or ""
    row["rust_io_kind"] = sorted(set(RUST_IO_KIND_RE.findall(text)))
    row["dns_phrase"] = bool(DNS_PHRASE_RE.search(text))
    row["elapsed_s"] = round(time.monotonic() - t0, 2)
    return row


async def main() -> int:
    results: list[dict[str, Any]] = []
    # ⚠️ **必须显式放行内网地址**：obscura 有 SSRF 护栏，默认**拦掉**
    # ``127.0.0.1`` / ``10.*`` ⇒ 本机 discard 端口与不可路由 IP 两个用例
    # 会被护栏挡在**连接之前**，拿到的是「护栏拒绝」而不是网络失败形态。
    # 护栏拒绝本身**不是**站点性质（是采集侧策略）⇒ 不能当判据样本。
    exe = os.environ.get("OBSCURA_EXE", "").strip()
    if not exe:
        raise RuntimeError("未设置 OBSCURA_EXE，无法定位 obscura 可执行文件")
    client = McpClient(
        exe, ("mcp", "--stealth"), 120.0,
        env={"OBSCURA_ALLOW_PRIVATE_NETWORK": "1"},
    )
    async with client:
        results.append(await _try_navigate(client, "① 正常站点", LIVE_HOST))
        results.append(await _try_navigate(client, "② DNS 解析失败", DEAD_HOST))
        for label, url in REFUSED_CANDIDATES:
            results.append(await _try_navigate(client, f"③ 连接被拒候选 · {label}", url))
        results.append(await _try_navigate(client, "④ 超时（不可路由 IP）", HANGING_HOST))

    payload = {
        "说明": (
            "obscura 面：**绕过驱动层 200 字截断**，直接调 MCP tool 拿完整原文。"
            "「连接被拒」现场是否触发取决于那一跳有没有真的回 RST。"
        ),
        "results": results,
    }
    out_path = Path(__file__).with_name("obscura_nav_failure_codes.json")
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _out(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
