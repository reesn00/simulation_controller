"""**现场取证探针**：把存档里那条观察**重新采一遍**，与存档读数并排摆出来。

**为什么要有这个脚本**（2026-10-21，批次第 5 项「人判两条」）

人判判据⑤（负样本成因）时有一类事实**存档里读不出来**：
**「这一页当时到底返回了什么」**。存档存的是**观察**，
而观察可能是「**上一个文档的残留**」——URL 已经是目标页，
DOM 还停在跳转前那一页。2026-10-21 实测在 7 条爱奇艺负样本上撞到过：

- 存档里 ``obs.url`` = ``https://www.iqiyi.com/v_19rrk40ajc.html…``
- 存档里 ``obs.page_title`` = **百度 SERP 的标题**、``body_text`` = **6 个字**
- ``degraded`` = ``[]`` ⇒ **采集层认为它采到了**

「URL 是 iqiyi、文档是百度」有两种成因，**处置完全相反**：

==========================  ==========================================
成因                          处置
==========================  ==========================================
站点**真的**返回了这页        负样本成立（另判 W1/W3/W4）
采集层读到了**跳转前的残留**   **采集层缺陷**——与判断点无关，
                              要修的是「goto 之后没有确认文档已换」
==========================  ==========================================

**分不清这两者，就不该动判断点的阈值**（台账 H-15）。

**本脚本只给事实，不给结论**（同 ``cli probe`` 的口径）：
每个 URL 现场采一遍，把 ``url / page_title / body_len / body 头 /
elements_total / degraded / video / iframe`` 打出来；
给了 ``--archive-dir`` 就把**同 URL 的存档读数并排摆出**。

**用法**（必须在仓库根跑；**联网**）：

    uv run python -m trajectory_pipeline.research.probes.inc73_field_check \\
        --driver camoufox \\
        --url "https://www.iqiyi.com/v_19rrk40ajc.html?vfm=2008_aldbd&fc=828fb30b722f3164&fv=p_02_01" \\
        --archive-dir trajectory_pipeline/output/pipeline/dualrun2/camoufox \\
        --json trajectory_pipeline/research/probes/inc73_field_check.json

**只写探针自己的 JSON**：不写 P1、不改代码、不并池。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any, Mapping

from trajectory_pipeline.research.probes.inc72_paired_compare import (  # noqa: E402
    load_runs,
    norm_url,
)

#: 正文头打印长度——够看出「这是不是目标页的正文」，又不至于刷屏。
_HEAD = 160


def _safe(text: Any) -> str:
    """按控制台编码转义（Windows 控制台是 GBK，见避坑指南 §6.3）。"""
    s = str(text)
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return s.encode(enc, "backslashreplace").decode(enc)
    except (LookupError, UnicodeError):
        return s.encode("ascii", "backslashreplace").decode("ascii")


def _p(*parts: Any) -> None:
    print(" ".join(_safe(x) for x in parts))


def _archived(archive_dir: pathlib.Path) -> dict[str, dict[str, Any]]:
    """存档目录 → ``落地 URL 归一化键`` → 当时的观察读数。

    ⚠️ **取的是「当时那一次」的读数**，不是最后一次——
    同一页可能被访问多次（baidu 跳转 + 直连），键相同时**先到先得**
    （与配对比较脚本同一口径）。
    """
    index: dict[str, dict[str, Any]] = {}
    for rec in load_runs(archive_dir):
        for v in rec.get("visits") or ():
            if not isinstance(v, Mapping):
                continue
            obs = v.get("site_obs")
            if not isinstance(obs, Mapping):
                continue
            key = norm_url(str(v.get("landed_url") or ""))
            if not key or key in index:
                continue
            index[key] = {
                "url": obs.get("url"),
                "page_title": obs.get("page_title"),
                "body_len": obs.get("body_len"),
                "body_head": str(obs.get("body_text") or "")[:_HEAD],
                "elements_total": obs.get("elements_total"),
                "video_tag_count": obs.get("video_tag_count"),
                "iframe_count": obs.get("iframe_count"),
                "degraded": list(obs.get("degraded") or ()),
                "body_source": obs.get("body_source"),
            }
    return index


async def _probe(driver: Any, url: str) -> dict[str, Any]:
    """采一遍——**只记事实**。失败就记失败，不猜。"""
    try:
        await driver.goto(url)
        obs = await driver.observe()
    except Exception as exc:  # noqa: BLE001 —— 探针不该被单站失败带崩
        return {"url": url, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "url": url,
        "observed_url": obs.url,
        "page_title": obs.page_title,
        "body_len": len(obs.body_text),
        "body_head": obs.body_text[:_HEAD],
        "elements_total": obs.elements_total,
        "video_tag_count": obs.video_tag_count,
        "iframe_count": obs.iframe_count,
        "degraded": list(obs.degraded),
        "body_source": obs.body_source,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "driver": args.driver,
        "server": None,
        "archive_dir": args.archive_dir,
        "results": [],
    }
    archived = _archived(pathlib.Path(args.archive_dir)) if args.archive_dir else {}

    if args.driver == "camoufox":
        from trajectory_pipeline.executor.browser.camoufox_driver import CamoufoxDriver
        from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession

        session = await CamoufoxSession.launch(headless=not args.headful)
        async with session:
            report["server"] = {"name": "camoufox", "fingerprint": session.fingerprint}
            driver = CamoufoxDriver(session)
            for url in args.url:
                res = await _probe(driver, url)
                res["archived"] = archived.get(norm_url(url))
                report["results"].append(res)
    else:
        from trajectory_pipeline.executor.browser.obscura_driver import ObscuraDriver
        from trajectory_pipeline.executor.mcp_client import McpClient

        try:
            client = McpClient.from_env()
        except RuntimeError as exc:
            _p(f"跳过：{exc}")
            return report
        async with client:
            report["server"] = {"name": "obscura"}
            driver = ObscuraDriver(client)
            for url in args.url:
                res = await _probe(driver, url)
                res["archived"] = archived.get(norm_url(url))
                report["results"].append(res)
    return report


def _print(report: Mapping[str, Any]) -> None:
    _p(f"driver = {report['driver']}")
    for r in report["results"]:
        _p(f"\n-- {r['url']}")
        if r.get("error"):
            _p(f"   采集失败: {r['error']}")
            continue
        _p(f"   现场 url   : {r['observed_url']}")
        _p(f"   现场 title : {r['page_title']}")
        _p(f"   现场 body  : {r['body_len']}ch  头: {r['body_head']}")
        _p(f"   现场元素   : total={r['elements_total']} video={r['video_tag_count']} "
           f"iframe={r['iframe_count']} degraded={r['degraded']}")
        a = r.get("archived")
        if not a:
            _p("   存档       : (同 URL 没找到存档读数)")
            continue
        _p(f"   存档 url   : {a.get('url')}")
        _p(f"   存档 title : {a.get('page_title')}")
        _p(f"   存档 body  : {a.get('body_len')}ch  头: {a.get('body_head')}")
        _p(f"   存档元素   : total={a.get('elements_total')} video={a.get('video_tag_count')} "
           f"iframe={a.get('iframe_count')} degraded={a.get('degraded')}")
        same = (str(a.get("page_title") or "") == str(r.get("page_title") or "")
                and a.get("body_len") == r.get("body_len"))
        _p(f"   => 现场与存档 {'一致' if same else '**不一致**'}")
        if not same:
            _p("      **不一致 = 站点当时返回的东西与现在不同，或当时读到的是残留**"
               "——成因要靠人看，本探针不下结论。")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="现场取证：重新采一遍，与存档读数并排")
    ap.add_argument("--url", action="append", default=[], help="要采的 URL（可给多次）")
    ap.add_argument("--driver", choices=("camoufox", "obscura"), default="camoufox")
    ap.add_argument("--headful", action="store_true", help="有头浏览器（缺省无头）")
    ap.add_argument("--archive-dir", default=None, help="存档目录（给了就并排摆读数）")
    ap.add_argument("--json", default=None, help="落盘路径")
    args = ap.parse_args(argv)

    if not args.url:
        _p("没给 --url")
        return 2

    report = asyncio.run(_run(args))
    _print(report)
    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        _p(f"\n落盘: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
