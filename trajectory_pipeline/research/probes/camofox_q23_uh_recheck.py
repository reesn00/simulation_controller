"""**Q-23 现场取证**：把负样本池里**存量**的 ``unreachable_hard`` 逐条重开。

为什么要有它
------------
M-44 裁定（owner，2026-10-27）把 ``unreachable_hard`` 拆成两支：

- **真·站点不可达**（DNS 解析不出来 / 连接被拒，**机器码**说了算）⇒ 进池；
- **这次没打开**（超时 / 打断 / abort / 空响应 / 建页失败 / 会话失效）⇒
  ``unresolved``（**不进池**）。

新口径**只影响新行**（并池是写入时的动作，不回填历史存档）⇒ 池里那批
**改判之前**写进去的存量行还在。台账 **Q-23** 记的就是这半截：
**出池得逐条单独复核**（**打得开 ⇒ 出池**）。

判据
----
复核的产出是**站点当下的可达性**，据此定去留：

==========================  ==========================================
复核结果                     处置
==========================  ==========================================
``opens``                   **证伪** ⇒ 出池（站点可达，标签不成立）
``site_unreachable``        **标签站得住** ⇒ 留在池里
（每次尝试都拿到真·站点不可达的机器码）
其余（证明不了）              **fail-closed** ⇒ 出池（池里那条断言
                             「站点不可达」没能被复现 ⇒ 撤）
==========================  ==========================================

⚠️ **fail-closed 是对「断言」fail-closed，不是对「站点」**：撤掉的行不是
被销毁，只是离开负样本池（降级成 ``unresolved``）⇒ 下一次采集会按**新口径**
重新判它一遍。

现场取证的三条硬规矩
--------------------
1. **每条开一张新页**（同页连着导航时，上一次失败的错误页还没落定，下一次
   导航就把它打断 ⇒ 拿到的失败形态是上一条的，不是本条的）。
2. **每条最多试 N 次**（默认 3）：只试一次的结论会被**单次传输层抖动**带走
   （增量九十四实测过同一条 URL 在 ``NS_ERROR_NET_RESET`` 与
   ``is interrupted by another navigation`` 之间**交替**）。
3. **执行层故障不算站点读数**：坏的是我们这一侧（会话失效 / 建页失败）⇒
   单独记 ``session_fault``，不参与「打得开 / 站点不可达」的判定。

只读、不改任何产物
------------------
读池（``negative.jsonl``）与存档目录；**只写本探针自己的 JSON**。
不改池、不改存档、不并池。跑法（**必须在仓库根**，**联网**）::

    uv run python -m trajectory_pipeline.research.probes.camofox_q23_uh_recheck

⚠️ **不能与采集批次并行**——上游 ``lock_queue`` 会销毁别人的页 ⇒
现场取证与批次互斥。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import re
import sys
import time
from typing import Any

from trajectory_pipeline.executor.browser.camofox_driver import CamofoxDriver
from trajectory_pipeline.executor.browser.camofox_session import CamofoxSession
from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    DriverSessionError,
    DriverUnreachableError,
)

#: ``trajectory_pipeline/``（本文件在 ``trajectory_pipeline/research/probes/``）。
ROOT = pathlib.Path(__file__).resolve().parents[2]
#: P1 存档目录（池与存档同根）。
ARCHIVE_DIR = ROOT / "output" / "pipeline"
#: 负样本池（append-only 资产）。
POOL_PATH = ARCHIVE_DIR / "negative.jsonl"
#: 本探针的产物。
OUT_PATH = pathlib.Path(__file__).with_name("camofox_q23_uh_recheck.json")

#: 待复核的分支——台账 Q-23 点名的这一支。
TARGET_BRANCH = "unreachable_hard"

#: **浏览器/内核自己给的网络错误码**（散文短语一律不收——本地化 + 版本漂移）。
NS_ERROR_RE = re.compile(r"\bNS_ERROR_[A-Z_]+\b")
#: 「被另一个导航打断」的**目标 URL**——它是**真·站点不可达**的反证：
#: 目标是真实站点 URL ⇒ 站点**响应了**并把导航改道了。
INTERRUPTED_BY_RE = re.compile(
    r"is interrupted by another navigation to\s+\"([^\"]*)\""
)

#: 每条最多试几次（只试一次会被单次抖动带走）。
DEFAULT_ATTEMPTS = 3
#: 第二读之前等多久（秒）——4 秒是 2026-10-23 实测出来的值。
DEFAULT_SETTLE = 4.0
#: 每次尝试之间的间隔（秒）——失败后立刻重试容易踩到同一个上游故障。
RETRY_GAP = 2.0

#: 关键词 → 命中说明什么（**只给次数，不给结论**）。
KEYWORDS: dict[str, str] = {
    "开通会员": "付费墙信号（要钱）",
    "续费会员": "付费墙信号（要钱）",
    "开通": "付费墙信号（要钱）",
    "VIP": "付费墙信号（要钱）",
    "会员": "付费墙信号（要钱）",
    "付费": "付费墙信号（要钱）",
    "购买": "付费墙信号（要钱）",
    "订阅": "付费墙信号（要钱）",
    "登录": "登录墙信号（要登录）",
    "未登录": "登录墙信号（要登录）",
    "请先登录": "登录墙信号（要登录）",
    "当前设备不支持播放": "地区限制 / 播放器不可用",
    "70017001": "地区限制错误码",
    "正在播放": "播放页（成功）",
    "预告": "预告片",
    "试看": "试看片段",
    "视频不存在": "404 / 视频不存在",
    "页面不存在": "404 / 页面不存在",
    "该视频": "视频不可用（不存在 / 已删除 / 私有）",
}


def _p(*parts: Any) -> None:
    """控制台只打 ASCII——Windows 控制台是 GBK，中文会变乱码。"""
    msg = " ".join(str(x) for x in parts)
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        print(msg.encode(enc, "backslashreplace").decode(enc))
    except (LookupError, UnicodeError):
        print(msg.encode("ascii", "backslashreplace").decode("ascii"))


def _hits(body: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for kw in KEYWORDS:
        n = body.count(kw)
        if n:
            out[kw] = n
    return out


# ── 池与存档的读取（只读）────────────────────────────────────────


def _pool_rows() -> list[dict[str, Any]]:
    """池里 ``unreachable_hard`` 的行（**只读**）。"""
    out: list[dict[str, Any]] = []
    if not POOL_PATH.exists():
        return out
    for line in POOL_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and str(row.get("branch")) == TARGET_BRANCH:
            out.append(row)
    return out


def _archive_outcomes() -> dict[str, list[dict[str, Any]]]:
    """URL → 存档里命中它的 outcome（``存档名`` + ``分支``）。

    ⚠️ 只取**原档**（``*__*.json``）：复核档是 ``review --apply`` 合并出来的，
    改它会被下一次合并覆盖回去。
    """
    index: dict[str, list[dict[str, Any]]] = {}
    if not ARCHIVE_DIR.is_dir():
        return index
    for path in sorted(ARCHIVE_DIR.glob("*.json")):
        if ".reviewed." in path.name or "__" not in path.name:
            continue
        try:
            arch = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(arch, dict):
            continue
        for out in arch.get("outcomes") or ():
            if not isinstance(out, dict):
                continue
            url = str(out.get("url") or "")
            if not url:
                continue
            index.setdefault(url, []).append({
                "archive": path.name[: -len(".json")],
                "branch": str(out.get("branch")),
                "evidence_head": str(out.get("evidence") or "").split("\n\n")[0][:160],
            })
    return index


# ── 现场取证 ────────────────────────────────────────────────────


async def _read(driver: Any) -> dict[str, Any]:
    """读一次页面。**只记事实**；读失败记失败，不猜。"""
    try:
        obs = await driver.observe(max_chars=4000)
    except Exception as exc:  # noqa: BLE001 —— 单站失败不该带崩探针
        return {"error": f"{type(exc).__name__}: {exc}"}
    body = obs.body_text or ""
    return {
        "observed_url": obs.url,
        "page_title": obs.page_title,
        "body_len": len(body),
        "body_head": body[:300],
        "elements_total": obs.elements_total,
        "video_tag_count": obs.video_tag_count,
        "iframe_count": obs.iframe_count,
        "degraded": list(obs.degraded),
        "body_source": obs.body_source,
        "keywords": _hits(body),
    }


async def _attempt(driver: Any, url: str, *, wait_until: str,
                   settle: float) -> dict[str, Any]:
    """**一次**尝试：开新页 → 导航 → 读两遍。

    ⚠️ **每条开新页**：同页连着导航时，上一次失败的错误页还没落定，
    下一次导航就把它打断 ⇒ 失败形态会归到上一条头上。
    """
    t0 = time.monotonic()
    rec: dict[str, Any] = {"url": url, "wait_until": wait_until}
    try:
        tab = await driver.new_tab()
    except BaseException as exc:  # noqa: BLE001
        rec.update({
            "kind": "session_fault", "stage": "new_tab",
            "error": f"{type(exc).__name__}: {exc}",
        })
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        return rec
    try:
        await driver.goto(url, wait_until=wait_until)
    except DriverUnreachableError as exc:
        # **真·站点不可达**——机器码说了算（驱动的归口判据 nav_failure.py）。
        rec.update({
            "kind": "site_unreachable",
            "code": getattr(exc, "code", "") or "",
            "error": f"{type(exc).__name__}: {exc}",
            "ns_error_codes": sorted(set(NS_ERROR_RE.findall(str(exc)))),
        })
    except DriverSessionError as exc:
        # **坏的是我们这一侧**——不是站点读数。
        rec.update({
            "kind": "session_fault", "stage": "goto",
            "error": f"{type(exc).__name__}: {exc}",
        })
    except DriverError as exc:
        # **这次没打开**——证明不了站点不可达。
        rec.update({
            "kind": "not_open",
            "error": f"{type(exc).__name__}: {exc}",
            "ns_error_codes": sorted(set(NS_ERROR_RE.findall(str(exc)))),
        })
        hit = INTERRUPTED_BY_RE.search(str(exc))
        if hit:
            rec["interrupted_by"] = hit.group(1)
    except BaseException as exc:  # noqa: BLE001
        rec.update({"kind": "not_open", "error": f"{type(exc).__name__}: {exc}"})
    else:
        rec["kind"] = "ok"
        rec["first"] = await _read(driver)
        if settle > 0:
            await asyncio.sleep(settle)
        rec["settled"] = await _read(driver)
    rec["elapsed_s"] = round(time.monotonic() - t0, 2)
    # 收掉这一页 ⇒ 下一条从干净状态开始。
    try:
        await driver.close_tab(tab)
    except BaseException:  # noqa: BLE001 —— 收页失败不改变本次读数
        pass
    return rec


def _verdict(attempts: list[dict[str, Any]]) -> tuple[str, str]:
    """把若干次尝试压成**一个裁定**（口径见模块 docstring）。"""
    kinds = [a.get("kind") for a in attempts]
    if not kinds:
        return "no_attempt", "没跑成"
    if "ok" in kinds:
        return "opens", "打得开（至少一次导航成功并读到页面）"
    site = [a for a in attempts if a.get("kind") == "site_unreachable"]
    if site and len(site) == len(attempts):
        codes = sorted({str(a.get("code") or "") for a in site})
        return "site_unreachable", f"每次尝试都拿到真·站点不可达机器码 {codes}"
    if any(k == "session_fault" for k in kinds):
        return "session_fault", "执行层故障，未取到站点读数"
    if site:
        return ("not_proven",
                f"混合：{len(site)}/{len(attempts)} 次拿到不可达机器码，"
                f"其余不是 ⇒ 复现不成立")
    return "not_proven", "打不开，但机器码证明不了站点不可达"


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    pool_rows = _pool_rows()
    archive_index = _archive_outcomes()

    # 目标 = 池里这批行的 **distinct URL**（站点性质是**按 URL** 判的，
    # 同一 URL 在 N 个存档里出现只该判一次）。
    targets: list[str] = []
    rows_by_url: dict[str, list[dict[str, Any]]] = {}
    for row in pool_rows:
        url = str(row.get("url") or "")
        if not url:
            continue
        if url not in rows_by_url:
            rows_by_url[url] = []
            targets.append(url)
        rows_by_url[url].append({
            "task_id": str(row.get("task_id") or ""),
            "run_id": str(row.get("run_id") or ""),
            "branch": str(row.get("branch") or ""),
            "evidence_head": str(row.get("evidence") or "").split("\n\n")[0][:160],
        })

    report: dict[str, Any] = {
        "what": "Q-23 现场取证：池里存量 unreachable_hard 逐条重开",
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "driver": "camofox",
        "attempts_per_target": args.attempts,
        "settle_seconds": args.settle,
        "pool_row_count": len(pool_rows),
        "target_count": len(targets),
        "targets": [],
        "results": [],
    }

    session = await CamofoxSession.launch()
    async with session:
        report["fingerprint"] = session.fingerprint
        driver = CamofoxDriver(session)
        for idx, url in enumerate(targets, 1):
            attempts: list[dict[str, Any]] = []
            for i in range(args.attempts):
                # 第一次等 ``load``，之后换 ``domcontentloaded``——有的站点
                # 会中断 ``load`` 导航（实测 ixigua：NS_BINDING_ABORTED）。
                wait_until = "load" if i % 2 == 0 else "domcontentloaded"
                attempts.append(await _attempt(driver, url, wait_until=wait_until,
                                              settle=args.settle))
                if attempts[-1].get("kind") == "ok":
                    break  # 打得开 ⇒ 定案，不必再试
                if attempts[-1].get("kind") == "site_unreachable":
                    # 机器码已经证明不可达，但**单次不足以定案** ⇒ 继续试，
                    # 看它复现不复现（增量九十四实测过两种形态交替出现）。
                    if i + 1 < args.attempts:
                        await asyncio.sleep(RETRY_GAP)
                    continue
                if attempts[-1].get("kind") == "session_fault":
                    await asyncio.sleep(RETRY_GAP)
            verdict, why = _verdict(attempts)
            report["results"].append({
                "url": url,
                "verdict": verdict,
                "verdict_reason": why,
                "attempts": attempts,
            })
            report["targets"].append({
                "url": url,
                "pool_rows": rows_by_url[url],
                "archive_outcomes": archive_index.get(url, []),
            })
            _p(f"[{idx}/{len(targets)}] {verdict:16s} {url[:90]}")

    summary: dict[str, int] = {}
    for row in report["results"]:
        summary[row["verdict"]] = summary.get(row["verdict"], 0) + 1
    report["summary"] = summary
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Q-23 现场取证：逐条复核池里存量的 unreachable_hard")
    ap.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS,
                    help=f"每条最多试几次（缺省 {DEFAULT_ATTEMPTS}）")
    ap.add_argument("--settle", type=float, default=DEFAULT_SETTLE,
                    help=f"第二读之前等几秒（缺省 {DEFAULT_SETTLE}）")
    ap.add_argument("--json", default=None, help="落盘路径（缺省本探针同名 .json）")
    args = ap.parse_args(argv)

    report = asyncio.run(_run(args))
    out = pathlib.Path(args.json) if args.json else OUT_PATH
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _p(f"wrote {out}")
    for k, v in sorted(report["summary"].items()):
        _p(f"  {k:18s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
