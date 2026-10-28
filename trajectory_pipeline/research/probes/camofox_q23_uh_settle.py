"""Q-23 补测：对**机器码没定论**的 URL 追加尝试。

⚠️ **为什么要加这一轮**：29 条 URL 里 17 条**第一轮就打开**（定案），
剩下 12 条是「没打开」。第一轮只试了 3 次，而**同站不同尺**是不行的——
``video.friday.tw`` 的两条 URL 第一轮读数**形态不同**（一条混了拒连、
一条全是超时），若只给「混了的那条」加试，就等于**按结果决定给谁加试**。

⇒ 本轮给**全部未定的 URL** 统一加试（``--attempts`` 次），
让每条 URL 的取证量一致 ⇒ 判据可以统一套用。

⚠️ **判据用执行器自己那把尺**（:mod:`executor.steps.reachability`）：
执行器**单次**导航里，机器码证明（DNS 解析不出来 / 连接被拒）⇒
``unreachable_hard``；其余（超时 / 打断 / abort / 空响应 / 建页失败 /
会话失效）⇒ ``unresolved``（不进池）。
⇒ 复核用的就是同一把尺：**这条 URL 有没有出现过真·不可达机器码、
且从未打开** ⇒ 站点不可达；否则 ⇒ 这次没打开。

⚠️ **不投票**：本脚本只**记录**每次尝试的机器码，**判据交给处置脚本**
（``q23_dispose_uh_rows.py``）与台账——不做多数决、不做复现率阈值。

本脚本**只读不写池**，产出 ``camofox_q23_uh_settle.json``。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys
import time
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from trajectory_pipeline.executor.browser.camofox_driver import CamofoxDriver  # noqa: E402
from trajectory_pipeline.executor.browser.camofox_session import CamofoxSession  # noqa: E402
from trajectory_pipeline.executor.browser.page_driver import (  # noqa: E402
    DriverError,
    DriverSessionError,
    DriverUnreachableError,
)

HERE = pathlib.Path(__file__).resolve().parent
RECHECK = HERE / "camofox_q23_uh_recheck.json"

#: 默认加试的 URL——第一轮里**没打开**的那些（打开的直接定案，不加试）。
#: ⚠️ **按「第一轮打不开」筛，不按「第一轮读数难解释」筛**——后者就是
#: 「按结果决定给谁加试」。
DEFAULT_TARGETS: tuple[str, ...] = ()

ATTEMPTS = 6
RETRY_GAP = 2.0
NS_ERROR_RE = re.compile(r"NS_ERROR_[A-Z_]+|SSL_ERROR_[A-Z_]+")
OUT_PATH = HERE / "camofox_q23_uh_settle.json"


async def _attempt(driver: Any, url: str, *, wait_until: str) -> dict[str, Any]:
    """**一次**尝试：开新页 → 导航 → 收页（口径同主探针）。"""
    t0 = time.monotonic()
    rec: dict[str, Any] = {"url": url, "wait_until": wait_until}
    try:
        tab = await driver.new_tab()
    except BaseException as exc:  # noqa: BLE001
        rec.update({"kind": "session_fault", "stage": "new_tab",
                    "error": f"{type(exc).__name__}: {exc}"})
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        return rec
    try:
        await driver.goto(url, wait_until=wait_until)
    except DriverUnreachableError as exc:
        rec.update({
            "kind": "site_unreachable",
            "code": getattr(exc, "code", "") or "",
            "error": f"{type(exc).__name__}: {exc}",
            "ns_error_codes": sorted(set(NS_ERROR_RE.findall(str(exc)))),
        })
    except DriverSessionError as exc:
        rec.update({"kind": "session_fault", "stage": "goto",
                    "error": f"{type(exc).__name__}: {exc}"})
    except DriverError as exc:
        rec.update({
            "kind": "not_open",
            "error": f"{type(exc).__name__}: {exc}",
            "ns_error_codes": sorted(set(NS_ERROR_RE.findall(str(exc)))),
        })
    except BaseException as exc:  # noqa: BLE001
        rec.update({"kind": "not_open", "error": f"{type(exc).__name__}: {exc}"})
    else:
        rec["kind"] = "ok"
        # ⚠️ **成功也要读一遍页面**：理由里要写「落地哪个站」——
        # 「打得开」不带落地 URL，等于让人再跑一遍才能核实。
        try:
            obs = await driver.observe(max_chars=4000)
        except BaseException as exc:  # noqa: BLE001
            rec["read_error"] = f"{type(exc).__name__}: {exc}"
        else:
            body = obs.body_text or ""
            rec["first"] = {
                "observed_url": obs.url,
                "page_title": obs.page_title,
                "body_len": len(body),
                "body_head": body[:300],
                "video_tag_count": obs.video_tag_count,
                "iframe_count": obs.iframe_count,
            }
    rec["elapsed_s"] = round(time.monotonic() - t0, 2)
    try:
        await driver.close_tab(tab)
    except BaseException:  # noqa: BLE001
        pass
    return rec


async def _run(targets: list[str], attempts_each: int) -> dict[str, Any]:
    report: dict[str, Any] = {
        "what": "Q-23 补测：给第一轮没打开的 URL 统一追加尝试（取证量一致 ⇒ 判据统一）",
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "driver": "camofox",
        "attempts_per_target": attempts_each,
        "targets": list(targets),
        "results": [],
    }
    session = await CamofoxSession.launch()
    async with session:
        report["fingerprint"] = session.fingerprint
        driver = CamofoxDriver(session)
        for url in targets:
            attempts: list[dict[str, Any]] = []
            for i in range(attempts_each):
                wait_until = "load" if i % 2 == 0 else "domcontentloaded"
                attempts.append(await _attempt(driver, url, wait_until=wait_until))
                if attempts[-1].get("kind") == "ok":
                    break
                if i + 1 < attempts_each:
                    await asyncio.sleep(RETRY_GAP)
            kinds = [a.get("kind") for a in attempts]
            hits = sum(1 for k in kinds if k == "site_unreachable")
            codes = sorted({str(a.get("code") or "") for a in attempts
                            if a.get("kind") == "site_unreachable"})
            report["results"].append({
                "url": url,
                "attempts": attempts,
                "kind_histogram": {k: kinds.count(k) for k in sorted(set(kinds))},
                "unreachable_code_hits": hits,
                "unreachable_codes": codes,
            })
            print(f"{url[:100]}\n  {report['results'][-1]['kind_histogram']} "
                  f"不可达码命中={hits} {codes}", flush=True)
    return report


def _default_targets() -> list[str]:
    """第一轮**没打开**的那些 URL（打开的直接定案，不加试）。"""
    if not RECHECK.exists():
        return []
    doc = json.loads(RECHECK.read_text(encoding="utf-8"))
    out = []
    for row in doc.get("results") or ():
        kinds = {str(a.get("kind")) for a in (row.get("attempts") or ())}
        if "ok" in kinds:
            continue
        out.append(str(row.get("url") or ""))
    return [u for u in out if u]


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Q-23 补测：加试")
    ap.add_argument("--attempts", type=int, default=ATTEMPTS,
                    help=f"每条加试几次（缺省 {ATTEMPTS}）")
    ap.add_argument("--url", nargs="*", default=None,
                    help="指定 URL（缺省＝第一轮没打开的全部）")
    args = ap.parse_args()
    targets = list(args.url) if args.url else _default_targets()
    print(f"加试 {len(targets)} 条 URL × {args.attempts} 次")
    report = asyncio.run(_run(targets, args.attempts))
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
