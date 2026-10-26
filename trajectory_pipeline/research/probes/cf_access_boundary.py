"""**R0 现场取证**：CF 挑战页那批行，现场再开一遍——到底是站点问题还是采集面问题。

**为什么要这个脚本**（2026-10-26，方案 20 §5 R0）

方案 20 的结论是「观测面缺一根轴」，但那是**从存档读出来的**。
存档只能说「当时这一读被拦了」，说不了「今天还拦不拦」。
而 O-a（`blocked` 出池）与 Q-17（`unreachable_hard` 出池）都是**会缩小分母**的动作，
按本仓纪律，**动分母之前先取证**（M-30 的姿势）：

======================  ============================================
现场读数                它对「哪一面成立」说了什么
======================  ============================================
拿到站点页面、有内容    **采集面那一轮被拦**（挑战是随机的）⇒ 存档那读是采集侧账
仍被 CF 拦              B 面事实成立，但**仍不能**据此说站点没有正片
导航失败 / 超时         又一次采集侧失败，仍不构成站点结论
======================  ============================================

**只记事实，不下结论**：脚本不写 P1、不改代码、不并池、不动金标，
输出只有一份自己的 JSON（`--json`）。

**目标从哪儿来**：默认**不手抄 URL 清单**——扫存档找「页面标题是挑战页」的访问，
用 `body_text` 首行（CF 会把落地主机名写在正文第一行）去对 `outcomes[].url`，
对上的那条 outcome 的 URL 就是要重采的站点地址。对不上的**列出来但不猜**
（`unmatched`，宁可少采一条，也不拿百度跳转链接当站点）。

**每轮两读**（与 `m30_live_check` 同口径）：`first` = 导航一返回就读，
`settled` = 再等 `--settle` 秒读同一页。差值就是「读太早」的量。

**用法**（必须在仓库根跑；**联网**）::

    uv run python -m trajectory_pipeline.research.probes.cf_access_boundary \\
        --json trajectory_pipeline/research/probes/cf_access_boundary.json

    # 只采这几条 / 换存档目录
    uv run python -m trajectory_pipeline.research.probes.cf_access_boundary --url https://…
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
from typing import Any, Mapping

#: 挑战页的标题特征（与 `gatekeeper._CF_TITLE_MARKERS` 同源；这里放宽一点，
#: 因为探针要找的是「那次访问落到了挑战页」这个**事实**，不是要复刻判定）。
CF_TITLE = re.compile(
    r"just a moment|attention required|access denied|"
    r"you have been blocked|performing security verification|"
    r"cf-chl|challenge-platform",
    re.I,
)

ARCHIVE_DIR = pathlib.Path("trajectory_pipeline/output/pipeline")


def _p(msg: str = "") -> None:
    print(msg, flush=True)


# ── 目标发现（从存档读，不手抄）─────────────────────────────────────


def _host_of(site_obs: Mapping[str, Any]) -> str:
    """CF 把落地主机名写在正文第一行——用它把 visit 对到 outcome。"""
    body = str(site_obs.get("body_text") or "").strip()
    if not body:
        return ""
    first = body.splitlines()[0].strip()
    return first if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", first, re.I) else ""


def discover(archive_dir: pathlib.Path) -> dict[str, Any]:
    """扫存档，找出「落在挑战页上」的访问，并尽量对回站点 URL。

    :return: ``{"targets": [...], "unmatched": [...], "scanned": n}``；
        每条 target 带**存档侧事实**（分支 / 证据 / 存档名），供现场读数对照。
    """
    targets: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    scanned = 0
    seen: set[tuple[str, str]] = set()

    for path in sorted(archive_dir.glob("*.json")):
        if ".reviewed" in path.name or not path.is_file():
            continue
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 —— 坏档不该带崩取证
            continue
        if not isinstance(blob, Mapping):
            continue
        scanned += 1
        outcomes = blob.get("outcomes") or ()

        for visit in blob.get("visits") or ():
            if not isinstance(visit, Mapping):
                continue
            site_obs = visit.get("site_obs") or {}
            title = str(site_obs.get("page_title") or "")
            if not CF_TITLE.search(title):
                continue
            host = _host_of(site_obs)
            hit = next(
                (
                    oc for oc in outcomes
                    if host and host in str(oc.get("url") or "")
                ),
                None,
            )
            if hit is None:
                unmatched.append({
                    "archive": path.name,
                    "page_title": title,
                    "host": host,
                    "visit_url": visit.get("url"),
                })
                continue
            url = str(hit.get("url") or "")
            key = (path.name, url)
            if key in seen:
                continue
            seen.add(key)
            targets.append({
                "archive": path.name,
                "url": url,
                "page_title": title,
                "archived_branch": hit.get("branch"),
                "archived_question": hit.get("question"),
                "archived_evidence": hit.get("evidence"),
            })

    return {"targets": targets, "unmatched": unmatched, "scanned": scanned}


# ── 现场采集（与 m30_live_check 同口径）─────────────────────────────


async def _read(driver: Any, detect_block: Any) -> dict[str, Any]:
    """读一次页面。**只记事实**；读失败记失败，不猜。"""
    try:
        obs = await driver.observe(max_chars=4000)
    except Exception as exc:  # noqa: BLE001 —— 单站失败不该带崩取证
        return {"error": f"{type(exc).__name__}: {exc}"}
    body = obs.body_text or ""
    return {
        "observed_url": obs.url,
        "page_title": obs.page_title,
        "body_len": len(body),
        "body_head": body[:300],
        # 「这一读算不算被拦」用**仓库自己的判据**，不另写一套关键词表
        "detect_block": detect_block(obs.url, obs.page_title, body),
        "video_tag_count": obs.video_tag_count,
        "iframe_count": obs.iframe_count,
        "elements_total": obs.elements_total,
        "degraded": list(obs.degraded),
        "body_source": obs.body_source,
    }


async def _probe(
    driver: Any, url: str, *, settle_seconds: float, detect_block: Any
) -> dict[str, Any]:
    """采一遍——**两读**（立刻 / 等渲染），失败就记失败，不猜。"""
    wait_until = "load"
    try:
        await driver.goto(url, wait_until="load")
    except Exception as exc:  # noqa: BLE001
        first_err = f"{type(exc).__name__}: {exc}"
        # 有的站点会中断 `load` 导航（ixigua：NS_BINDING_ABORTED）⇒ 换一种等待重来
        wait_until = "domcontentloaded"
        try:
            await driver.goto(url, wait_until=wait_until)
        except Exception as exc2:  # noqa: BLE001
            return {
                "url": url,
                "wait_until": wait_until,
                "error": f"{type(exc2).__name__}: {exc2}",
                "first_error": first_err,
            }

    first = await _read(driver, detect_block)
    if settle_seconds > 0:
        await asyncio.sleep(settle_seconds)
    settled = await _read(driver, detect_block)
    return {
        "url": url,
        "wait_until": wait_until,
        "settle_seconds": settle_seconds,
        "first": first,
        "settled": settled,
    }


def _verdict_of(round_result: Mapping[str, Any]) -> str:
    """这一轮**现场**落哪一类事实——只按读数分类，不按期望。"""
    if round_result.get("error"):
        return "采集失败"
    settled = round_result.get("settled") or {}
    if settled.get("error"):
        return "采集失败"
    if settled.get("detect_block"):
        return f"仍被拦（{settled['detect_block']}）"
    if not settled.get("body_len"):
        return "拿到了页面但正文空"
    return "可进入"


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from trajectory_pipeline.executor.browser.camoufox_driver import CamoufoxDriver
    from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession
    from trajectory_pipeline.executor.dom import detect_block

    found = discover(pathlib.Path(args.archive_dir))
    targets: list[dict[str, Any]] = found["targets"]
    if args.url:
        targets = [
            {"archive": "(命令行)", "url": u, "page_title": "",
             "archived_branch": "", "archived_question": "", "archived_evidence": ""}
            for u in args.url
        ]

    report: dict[str, Any] = {
        "driver": "camoufox",
        "headless": not args.headful,
        "settle_seconds": args.settle,
        "rounds": args.rounds,
        "scanned_archives": found["scanned"],
        "unmatched": found["unmatched"],
        "results": [],
    }

    session = await CamoufoxSession.launch(headless=not args.headful)
    async with session:
        # 观测面身份必须记下来——没有它，这份取证也不能跟别的批次比
        report["fingerprint"] = session.fingerprint
        driver = CamoufoxDriver(session)
        for t in targets:
            entry = dict(t)
            entry["rounds"] = []
            for i in range(args.rounds):
                r = await _probe(
                    driver, t["url"],
                    settle_seconds=args.settle, detect_block=detect_block,
                )
                r["round"] = i + 1
                r["verdict"] = _verdict_of(r)
                entry["rounds"].append(r)
                _p(f"  [{i + 1}/{args.rounds}] {t['url'][:72]} → {r['verdict']}")
            report["results"].append(entry)
    return report


def _print(report: Mapping[str, Any]) -> None:
    _p(f"扫描存档 {report['scanned_archives']} 份；"
       f"对不上的访问 {len(report['unmatched'])} 条（不猜，见 JSON）")
    _p(f"driver = {report['driver']}  headless = {report['headless']}  "
       f"rounds = {report['rounds']}  settle = {report['settle_seconds']}s")
    _p(f"fingerprint = {json.dumps(report.get('fingerprint'), ensure_ascii=False)}")

    tally: dict[str, int] = {}
    for entry in report["results"]:
        _p(f"\n=== {entry['url']}")
        _p(f"   存档: branch={entry.get('archived_branch')!r}  "
           f"evidence={(entry.get('archived_evidence') or '')[:90]}")
        for r in entry["rounds"]:
            tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
            if r.get("error"):
                _p(f"   round {r['round']}: 采集失败 {r['error']}")
                continue
            s = r["settled"]
            _p(f"   round {r['round']}: {r['verdict']}  "
               f"title={s.get('page_title')!r} body={s.get('body_len')}ch "
               f"video={s.get('video_tag_count')} iframe={s.get('iframe_count')} "
               f"detect_block={s.get('detect_block')!r}")

    _p("\n── 现场读数汇总（按轮计）──")
    for k, v in sorted(tally.items(), key=lambda x: -x[1]):
        _p(f"   {k:<28}{v:>4}")
    _p("\n⚠️ 「可进入」只说明**采集面这次进去了**；它不证明那一轮存档是假负样本，"
       "\n   也不把站点结论改成「能看」——站点有没有正片仍由终局验收 / 人工复核定。")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 非 tty 环境不支持就算了
        pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--archive-dir", default=str(ARCHIVE_DIR))
    ap.add_argument("--url", action="append", help="只采这些（可重复）；缺省从存档发现")
    ap.add_argument("--rounds", type=int, default=2, help="重采轮数（默认 2，两次独立导航）")
    ap.add_argument("--settle", type=float, default=4.0, help="等渲染秒数")
    ap.add_argument("--headful", action="store_true", help="开有头浏览器看现场")
    ap.add_argument("--json", default="", help="把报告写到这个文件")
    args = ap.parse_args(argv)

    report = asyncio.run(_run(args))
    _print(report)
    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _p(f"\n报告已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())