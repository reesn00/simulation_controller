"""**动作重放探针**（台账 M-25）——判据⑥「动作可移植性」的**材料来源**。

**为什么要有这个脚本**（2026-10-21，批次第 5 项「人判两条」）

判据⑥ 问的是：「换执行层之后，**动作**还能不能照样发出去？」
它不是问「模型会不会点错」，而是问「**动作面本身**可不可移植」。
机器判不了——`compare-drivers` 对它只能报 `needs_manual`。
而人判要看的东西**必须先有读数**：没有读数，人判只能对着
「动作成功率」这种粗指标点头，而那正是判据⑥ 想拆开的东西。

**判据⑥ 的口径（本脚本实现的读数）**：

    从 P1 存档取 ``steps[]`` 里的元素定位动作
        → 在**裸 camoufox**（新会话、无历史状态）里重放
        → 记每步的**定位结果**（0 个 / 恰好 1 个 / >1 个）

⚠️ **重放只定位，不点击**。点击**不可逆**（可能点进另一个视频、
可能触发付费流程），而可移植性问的是「**还能不能定位到**」。
定位读数走的是生产链**同一条判定路径**
（:meth:`CamoufoxDriver.resolve` → :meth:`CamoufoxDriver._locate`），
**不另写一份**——否则探针与生产链的定位口径会漂，
而漂了之后「重放定位率」就不再是生产链的读数（判据⑥ 判错了对象）。

**归因靠三列并排**（只看「定位率」会把两件事混成一件）：

=================  ==================  ==========================
重放结果             当时在阶梯里吗          结论
=================  ==================  ==========================
``ok``（1 个）       —                     **可移植**
``not_found``       ``True``              **名字脆**（真问题）
``not_found``       ``False``             **本来就没这条**（不是重放的问题）
``not_found``       — 且**没有基线**        **归因不了**（不硬塞）
``ambiguous``（>1） —                     **同名不可判定**（生产链 fail-closed）
=================  ==================  ==========================

「页面漂」= 重放时页面标题/正文长度与存档当时**不一致**
⇒ 站点改了版或换了内容，**不是名字的锅**。这一列不读，漂移会被
误记成「不可移植」，而真相是「测的是另一个页面」。

⚠️ **「没有基线」不等于「没漂」**：旧批次存档既没标题也没正文长度，
那种条目**没有可比基线**——漂移记 ``null``（未知），
**不许**记 ``False``。把「没测出来」写成「没问题」是与 §3.7 同源的错。

**用法**（必须在仓库根跑；**联网**——要起真浏览器）：

    uv run python -m trajectory_pipeline.research.probes.inc73_action_replay \\
        --archive trajectory_pipeline/output/pipeline/dualrun2/camoufox \\
        --archive trajectory_pipeline/output/pipeline/dualrun/camoufox \\
        --json trajectory_pipeline/output/pipeline/inc73_replay.json

**只写探针自己的 JSON**：不写 P1、不改代码、不并池。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any, Mapping

# ⚠️ **同源纪律**：动作行**只有一份实现**，在人判工作表脚本里。
#    重放探针与人判工作表必须对「哪些动作、在哪个页面、叫什么」取得一致——
#    两边各算一遍的话，工作表上标的行与探针重放的行会**悄悄错位**，
#    症状是「人判判的那条不在探针结果里」。
from trajectory_pipeline.research.probes.inc72_paired_compare import (  # noqa: E402
    load_runs,
)
from trajectory_pipeline.research.probes.inc73_human_review import (  # noqa: E402
    _action_rows,
)

#: 单页导航 + 定位的**总**上限（秒）。超了记 ``error``，不猜。
#:
#: ⚠️ 这不是动作预算（5s）——动作预算是「点一次」的上限，
#: 这里管的是「开一次页 + 枚举 + 定位」整段。给 45s 是因为
#: 影视站普遍有慢 iframe（`load` 事件迟迟不来），而
#: **超时是比「定位不到」弱得多的读数**——宁可多等，别把慢当没找到。
PER_ACTION_TIMEOUT_S = 45.0


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


def collect_actions(dirs: list[pathlib.Path]) -> list[dict[str, Any]]:
    """从存档目录收集元素定位动作行（camoufox 面才有 role+name 词汇）。"""
    rows: list[dict[str, Any]] = []
    for d in dirs:
        for rec in load_runs(d):
            rows += _action_rows(rec, src=str(d))
    return rows


async def _replay_one(driver: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    """重放一条动作——**只定位，不点击**。"""
    url = str(row.get("visit_landed_url") or "")
    role = str(row.get("role") or "")
    name = str(row.get("name") or "")
    out: dict[str, Any] = {
        "visit_landed_url": url,
        "tool": row.get("tool"),
        "role": role,
        "name": name,
        "archived_page_title": row.get("archived_page_title"),
        "archived_body_len": row.get("archived_body_len"),
        "archived_elements_total": row.get("archived_elements_total"),
        "archived_in_ladder": row.get("archived_in_ladder"),
        "capture_error": row.get("error"),
    }
    if not url:
        out["verdict"] = "no_url"
        out["error"] = "动作行没有落地 URL（重放无从下手）"
        return out
    try:
        await asyncio.wait_for(
            _replay_inner(driver, out, role, name),
            timeout=PER_ACTION_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        out["error"] = f"重放超时（>{PER_ACTION_TIMEOUT_S:.0f}s）"
        out["verdict"] = "timeout"
    except Exception as exc:  # noqa: BLE001 —— 探针不该被单条失败带崩
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["verdict"] = "error"
    return out


async def _replay_inner(
    driver: Any, out: dict[str, Any], role: str, name: str
) -> None:
    """导航 → 读页面事实 → 定位 → 归因。"""
    await driver.goto(str(out["visit_landed_url"]))
    obs = await driver.observe()
    out["observed_url"] = obs.url
    out["page_title"] = obs.page_title
    out["body_len"] = len(obs.body_text)
    out["elements_total"] = obs.elements_total

    # 页面漂了没有——⚠️ **「没有基线」不等于「没漂」**。
    # 旧批次存档没有标题也没有正文长度（`archived_body_len` 缺失），
    # 那种条目**根本没有可比基线**，把漂移记成 False 就是在说
    # 「没测出来 = 没问题」——与 §3.7 犯的是同一个错。
    arch_title = str(out.get("archived_page_title") or "")
    arch_len = out.get("archived_body_len")
    try:
        arch_len_i = int(arch_len) if arch_len is not None else None
    except (TypeError, ValueError):
        arch_len_i = None
    live_title = str(obs.page_title or "")
    live_len = len(obs.body_text)
    # **空标题不是基线**——采集时标题没抓到（很常见），拿它跟现场标题比
    # 只会得出「漂了」，而真相是「当时就没这个读数」。
    drift_title = bool(arch_title) and live_title != arch_title
    drift_body = arch_len_i is not None and arch_len_i != live_len
    baseline = bool(arch_title) or arch_len_i is not None
    if not baseline:
        out["page_drift"] = None
        out["baseline_available"] = False
    else:
        out["page_drift"] = bool(drift_title or drift_body)
        out["baseline_available"] = True
        out["drift_reason"] = "+".join(
            k for k, v in (("title", drift_title), ("body", drift_body)) if v
        ) or None

    res = await driver.resolve(role=role, name=name)
    out["outcome"] = res.get("outcome")
    out["matches"] = res.get("matches")

    outcome = str(res.get("outcome"))
    in_ladder = bool(out.get("archived_in_ladder"))
    drift = out.get("page_drift")
    if outcome == "ok":
        out["verdict"] = "portable"
    elif outcome == "ambiguous":
        out["verdict"] = "ambiguous"
    elif outcome == "not_found":
        if drift is True:
            out["verdict"] = "not_found_page_drift"
        elif drift is None:
            # 没有基线 ⇒ **归因不了**。不许硬塞进「名字脆」或「本来就没这条」——
            # 那两个都是**有基线时**才成立的结论。
            out["verdict"] = "not_found_unattributable"
        elif not in_ladder:
            out["verdict"] = "not_found_never_collected"
        else:
            out["verdict"] = "not_found_name_fragile"
    else:
        out["verdict"] = f"unknown:{outcome}"


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    from trajectory_pipeline.executor.browser.camoufox_driver import CamoufoxDriver
    from trajectory_pipeline.executor.browser.camoufox_session import CamoufoxSession

    rows = collect_actions([pathlib.Path(d) for d in args.archive])
    if args.limit:
        rows = rows[: args.limit]

    report: dict[str, Any] = {
        "driver": "camoufox",
        "headless": not args.headful,
        "per_action_timeout_s": PER_ACTION_TIMEOUT_S,
        "actions_total": len(rows),
        "results": [],
    }
    if not rows:
        report["summary"] = {"total": 0, "portable": 0}
        return report

    session = await CamoufoxSession.launch(headless=not args.headful)
    async with session:
        report["server"] = {"name": "camoufox", "fingerprint": session.fingerprint}
        driver = CamoufoxDriver(session)
        for i, row in enumerate(rows, start=1):
            res = await _replay_one(driver, row)
            res["index"] = i
            res["src"] = row.get("src")
            res["task_id"] = row.get("task_id")
            res["run_id"] = row.get("run_id")
            res["name_check"] = row.get("name_check")
            report["results"].append(res)
    report["summary"] = _summarize(report["results"])
    return report


def _summarize(results: list[Mapping[str, Any]]) -> dict[str, Any]:
    """汇总——**分母写清楚**。

    ⚠️ 「可移植率」的分母**不能是全部动作**：
    超时/出错的条目**没有读数**，把它们算进分母等于
    把「没测出来」读成「不可移植」——那正是判据⑥ 要拆的混淆。
    分母 = **成功拿到定位读数**的条目数。
    """
    verdicts: dict[str, int] = {}
    for r in results:
        v = str(r.get("verdict") or "unknown")
        verdicts[v] = verdicts.get(v, 0) + 1
    measured = [
        r for r in results
        if str(r.get("verdict")) in
        ("portable", "ambiguous", "not_found_page_drift",
         "not_found_never_collected", "not_found_name_fragile",
         "not_found_unattributable")
    ]
    portable = sum(1 for r in measured if r.get("verdict") == "portable")
    drift = sum(1 for r in measured if r.get("page_drift") is True)
    drift_unknown = sum(1 for r in measured if r.get("page_drift") is None)
    return {
        "total": len(results),
        "measured": len(measured),
        "unmeasured": len(results) - len(measured),
        "portable": portable,
        "portable_rate_of_measured": (portable / len(measured)) if measured else None,
        "page_drift": drift,
        "page_drift_unknown": drift_unknown,
        "by_verdict": verdicts,
    }


def _print(report: Mapping[str, Any]) -> None:
    s = report.get("summary") or {}
    _p(f"动作总数 {s.get('total')}  有读数 {s.get('measured')}  无读数 {s.get('unmeasured')}")
    rate = s.get("portable_rate_of_measured")
    if rate is not None:
        _p(f"可移植率（分母=有读数的） {rate:.0%}   页面漂移 {s.get('page_drift')} 条")
    _p(f"分布: {s.get('by_verdict')}")
    for r in report.get("results") or ():
        _p(f"\n-- [{r.get('index')}] {r.get('tool')} role={r.get('role')!r} "
           f"name={r.get('name')!r}")
        _p(f"   落地页   : {r.get('visit_landed_url')}")
        _p(f"   现场     : title={r.get('page_title')!r} body={r.get('body_len')}ch "
           f"elements={r.get('elements_total')}")
        _p(f"   存档     : title={r.get('archived_page_title')!r} "
           f"body={r.get('archived_body_len')}ch 当时在阶梯里={r.get('archived_in_ladder')}")
        _p(f"   定位     : outcome={r.get('outcome')} matches={r.get('matches')}")
        if r.get("error"):
            _p(f"   错误     : {r.get('error')}")
        _p(f"   => {r.get('verdict')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="动作重放探针：裸 camoufox 里重放存档里的元素定位动作，"
                    "记每步定位结果（判据⑥ 的材料）")
    ap.add_argument("--archive", action="append", default=[],
                    help="camoufox 面存档目录（可给多次）")
    ap.add_argument("--limit", type=int, default=0, help="只重放前 N 条")
    ap.add_argument("--headful", action="store_true", help="有头浏览器（缺省无头）")
    ap.add_argument("--json", default=None, help="落盘路径")
    args = ap.parse_args(argv)

    if not args.archive:
        _p("没给 --archive")
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
