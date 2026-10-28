"""**采集侧账读数**（方案 20 §R3）——从 P1 存档 + 黄金集**离线**算出来。

为什么要有这个探针
--------------------
方案 20 §R3 把「**采集面被拦**」和「**站点性质**」分成**两本账**：

- **站点性质** ⇒ 负样本池（金标侧的断言）；
- **采集面被拦** ⇒ **采集侧账**（`blocked` / `unreachable_hard`（Q-17 待裁）
  / `site_search_routed`）⇒ **不进池、不进金标**。

R3 落地时给闸门事件账本补了三栏：`blocked_surface`（**在哪张采集面上被拦**）·
`attempt`（同 verdict 内第几次）· `elapsed_ms`。**面只从 `RunRecord.driver`
折算**，采集点**不手填**。

⚠️ **跨面那本账现在是「结构上」为 0，不是「这批没拦到」**：
金标侧（冻结档 + 候选集）**整列没有 `access_surface`** ⇒ 金标面的观测面
一律 `unrecorded` ⇒ **面 ≠ 面 ⇒ 说不清** ⇒ 跨面分歧卡/账**按构造就是 0**。
⇒ 本探针把这件事**当成读数印出来**（附证据：哪几个文件、几行、几列），
**不拿 0 说「没拦到」**——那是两回事。

⚠️ **只读、不改任何产物**。跑法（**必须在仓库根**）::

    uv run python -m trajectory_pipeline.research.probes.camofox_collection_account \
      --root trajectory_pipeline/output/pipeline \
      --run 0caf6d0b --run 058fb16a --json-out <path>.json

⚠️ **完全离线**（只读 JSON）⇒ **可以和真跑批次并发**，
但**批次正在写盘时读到的可能是不完整的一档** ⇒ 要判批次请**跑完之后**再跑。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

#: Windows 控制台默认 GBK ⇒ 中文读数会炸（``UnicodeEncodeError``）。
try:  # pragma: no cover - 只在有控制台时生效
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # pragma: no cover
    pass

#: 存档文件名形如 ``{task_id}__{run_id}.json``（run_id 是 8 位十六进制）。
ARCHIVE_SUFFIX = ".json"

#: 复核档（``*.reviewed.json``）是**人工改判过**的那一份 ⇒ 不算「真跑那一份」。
REVIEWED_SUFFIX = ".reviewed.json"


def _archives(root: Path, runs: list[str]) -> list[Path]:
    """挑存档：``--run`` 给了就只挑那几个 run_id，否则全目录。"""
    out: list[Path] = []
    for path in sorted(root.glob(f"*{ARCHIVE_SUFFIX}")):
        if path.name.endswith(REVIEWED_SUFFIX):
            continue
        if runs and not any(r in path.name for r in runs):
            continue
        out.append(path)
    return out


def _account_for_archive(path: Path) -> dict[str, Any]:
    """一档存档的采集侧账读数。

    账的来源是**闸门事件账本** ``gatekeeper_events[]``（不是池）：
    方案 20 §R3 裁定 ``blocked`` **不进池** ⇒ 池里看不到被拦的笔数 ⇒
    只能从存档的闸门账本读。
    """
    d = json.loads(path.read_text(encoding="utf-8"))
    events = d.get("gatekeeper_events") or []
    outcomes = d.get("outcomes") or []

    stamped = [e for e in events if e.get("blocked_surface")]
    by_surface = Counter(str(e.get("blocked_surface")) for e in stamped)
    by_verdict = Counter(str(e.get("verdict")) for e in stamped)
    #: verdict × attempt 交叉表（方案 20 §R3：「attempt 按 verdict 各数各的」）
    verdict_attempt: Counter[tuple[str, str]] = Counter(
        (str(e.get("verdict")), str(e.get("attempt"))) for e in stamped
    )
    #: 收口（fail_closed）⇒ 这一笔换来了一个 outcome 分支
    closed = [
        e for e in stamped
        if (e.get("disposition") or {}).get("kind") == "fail_closed"
    ]
    branch_of = Counter(
        str((e.get("disposition") or {}).get("branch")) for e in closed
    )
    outcome_branch = Counter(str(o.get("branch")) for o in outcomes)

    return {
        "archive": path.name,
        "task_id": d.get("task_id"),
        "run_id": d.get("run_id"),
        "driver": d.get("driver"),
        "batch_id": d.get("batch_id"),
        "elapsed_ms": d.get("elapsed_ms"),
        "events_total": len(events),
        "events_stamped": len(stamped),
        "by_surface": dict(by_surface),
        "by_verdict": dict(by_verdict),
        "verdict_x_attempt": {f"{v}#{a}": n for (v, a), n in sorted(verdict_attempt.items())},
        "closed_branches": dict(branch_of),
        "outcome_branches": dict(outcome_branch),
    }


def _gold_side(golden_root: Path) -> dict[str, Any]:
    """金标侧的面分布——**跨面账为什么是 0** 的证据。

    查三处：最新冻结档（``versions.jsonl`` 最后一版）· 候选集 · 人核标注。
    """
    out: dict[str, Any] = {}
    versions = golden_root / "versions.jsonl"
    latest = None
    if versions.exists():
        rows = [json.loads(l) for l in versions.read_text(encoding="utf-8").splitlines() if l.strip()]
        if rows:
            latest = rows[-1]
            out["latest_version"] = latest
    if latest is not None:
        vdir = golden_root / f"v{int(latest.get('version', 0))}"
        for name in ("calibration.jsonl", "holdout.jsonl", "regression.jsonl"):
            p = vdir / name
            if not p.exists():
                continue
            rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
            out.setdefault("frozen", {})[name] = {
                "rows": len(rows),
                "access_surface": dict(Counter(str(r.get("access_surface")) for r in rows)),
                "has_key": sum(1 for r in rows if "access_surface" in r),
            }
    cand = golden_root / "candidates.jsonl"
    if cand.exists():
        rows = [json.loads(l) for l in cand.read_text(encoding="utf-8").splitlines() if l.strip()]
        out["candidates"] = {
            "rows": len(rows),
            "access_surface": dict(Counter(str(r.get("access_surface")) for r in rows)),
            "has_key": sum(1 for r in rows if "access_surface" in r),
        }
    ann = golden_root / "annotations.jsonl"
    if ann.exists():
        rows = [json.loads(l) for l in ann.read_text(encoding="utf-8").splitlines() if l.strip()]
        out["annotations"] = {
            "rows": len(rows),
            "access_surface": dict(Counter(str(r.get("access_surface")) for r in rows)),
            "has_key": sum(1 for r in rows if "access_surface" in r),
        }
    return out


def _pool_side(root: Path) -> dict[str, Any]:
    """池里**不该**有 ``blocked``（方案 20 §R3：采集侧账不进池）。

    ⚠️ **要分两代读**：R1（2026-10-26）之后**新写**的池行不再带 ``blocked``
    （``branches.COLLECTION_SIDE_BRANCHES`` 挡在
    ``SiteOutcome.is_negative_sample`` 那一层）⇒ **带 ``driver`` 的行 = 新写**；
    ``driver`` 为空的 = **2026-10-26 之前写的存量行** ⇒ 那些**从没被撤过**
    （**并池是写入时的动作，不回填历史存档**）⇒ **存量欠账 ≠ 代码漏挡**。

    ⇒ 两个数分开报：**新写漏挡**（该是 0）· **存量未撤**（欠账）。
    """
    pool = root / "negative.jsonl"
    if not pool.exists():
        return {"rows": 0, "branches": {}, "blocked_new": 0, "blocked_legacy": 0}
    rows = [json.loads(l) for l in pool.read_text(encoding="utf-8").splitlines() if l.strip()]
    branches = Counter(str(r.get("branch")) for r in rows)
    blocked = [r for r in rows if r.get("branch") == "blocked"]
    return {
        "rows": len(rows),
        "branches": dict(branches),
        "blocked_total": len(blocked),
        #: 带 driver ⇒ R1 之后写的 ⇒ **该是 0**（有就是代码漏挡）
        "blocked_new": sum(1 for r in blocked if r.get("driver")),
        #: driver 为空 ⇒ R1 之前的存量 ⇒ 方案 20 §R1 明写「历史池行一律不动」
        "blocked_legacy": sum(1 for r in blocked if not r.get("driver")),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="采集侧账读数（方案 20 §R3）")
    ap.add_argument("--root", default="trajectory_pipeline/output/pipeline",
                    help="管线产物根（P1 存档 + 负样本池）")
    ap.add_argument("--golden-root", default="trajectory_pipeline/evaluation/golden/data",
                    help="黄金集数据根（读金标侧的面分布）")
    ap.add_argument("--run", action="append", default=None,
                    help="只看这些 run_id（可重复）；缺省 = 全目录")
    ap.add_argument("--json-out", default=None, help="读数落盘路径")
    args = ap.parse_args(argv)

    root = Path(args.root)
    golden_root = Path(args.golden_root)
    runs = args.run or []

    per = [_account_for_archive(p) for p in _archives(root, runs)]
    agg_surface = Counter()
    agg_verdict = Counter()
    agg_va: Counter[str] = Counter()
    for a in per:
        agg_surface.update(a["by_surface"])
        agg_verdict.update(a["by_verdict"])
        agg_va.update(a["verdict_x_attempt"])

    report: dict[str, Any] = {
        "root": str(root),
        "archives_scanned": len(per),
        "per_archive": per,
        "aggregate": {
            "by_surface": dict(agg_surface),
            "by_verdict": dict(agg_verdict),
            "verdict_x_attempt": dict(sorted(agg_va.items())),
        },
        "gold_side": _gold_side(golden_root),
        "pool_side": _pool_side(root),
    }
    #: 跨面账为什么是 0 —— **结构性**（金标侧没记面）还是**这批没拦到**（读数）？
    gold_surfaces = set()
    for block in report["gold_side"].values():
        if isinstance(block, dict) and "access_surface" in block:
            gold_surfaces.update(block["access_surface"])
    report["cross_surface_comparable"] = sorted(s for s in gold_surfaces if s and s != "None")
    report["cross_surface_note"] = (
        "金标侧整列没有 access_surface ⇒ 面一律 unrecorded ⇒ "
        "跨面账按构造为 0（不是「这批没拦到」）"
        if not report["cross_surface_comparable"]
        else "金标侧记了面 ⇒ 跨面可比性存在，读数见上"
    )

    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"→ 已写 {p}")

    print(f"== 单面采集侧账（{len(per)} 档）==")
    for a in per:
        print(f"  {a['archive']:26s} driver={a['driver']} "
              f"打点 {a['events_stamped']}/{a['events_total']} "
              f"面={a['by_surface']} verdict={a['by_verdict']}")
        if a["verdict_x_attempt"]:
            print(f"      verdict×attempt: {a['verdict_x_attempt']}")
        print(f"      收口分支: {a['closed_branches']} | outcome 分支: {a['outcome_branches']}")
    print(f"  合计面分布: {report['aggregate']['by_surface']}")
    print(f"  合计 verdict: {report['aggregate']['by_verdict']}")
    print(f"  合计 verdict×attempt: {report['aggregate']['verdict_x_attempt']}")
    print("== 跨面账 ==")
    print(f"  金标侧可比的面: {report['cross_surface_comparable'] or '（无 ⇒ 结构上不可判定）'}")
    print(f"  {report['cross_surface_note']}")
    print("== 池侧核对（blocked 不该进池）==")
    ps = report["pool_side"]
    print(f"  池 {ps['rows']} 行 | blocked 共 {ps.get('blocked_total', 0)} 行 = "
          f"新写 {ps.get('blocked_new', 0)}（R1 之后 ⇒ 该是 0）+ "
          f"存量 {ps.get('blocked_legacy', 0)}（R1 之前 ⇒ 方案 20 §R1 明写「历史池行一律不动」）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
