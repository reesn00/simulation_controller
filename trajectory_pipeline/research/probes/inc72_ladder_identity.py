"""共享账本的**逐观察恒等式核对**——看梯子各档能不能自己对上。

**为什么要有这个脚本**（2026-10-21 实测逼出来的）：

`compare-drivers` 的归因表把**每一档各自的中位数**摆成一列，
看上去像一条可以相减的链（``candidates 52 → hidden 3 → visible 31``）。
但**各档中位数是各自独立取出来的**，来自**不同的观察**——
所以那条链**相减是不成立的**（52 − 3 = 49 ≠ 31）。

这不一定是 bug：可能是各档**分布都偏斜**，中位数各说各话。
但要下结论就得先分清这两种可能：

1. **账本本身不自洽**——某个观察里各档对不上 ⇒ 真的 bug；
2. **账本自洽，只是中位数不可加**——各观察逐条都对得上，
   只是「取中位数」这个动作把流量关系压平了。

本脚本按**逐观察**核对恒等式，把两种可能分开：

- 逐观察恒等式 ``kept + truncated == role_matched - nameless - ambiguous``
  （截断口径；limit=500 时 ``truncated`` 恒为 0）；
- 逐观察恒等式 ``candidates == hidden + visible``；
- 逐观察 ``visible == role_matched + role_unmatched``。

**用法**（必须在仓库根跑）：

    uv run --offline python -m trajectory_pipeline.research.probes.inc72_ladder_identity \
        --dirs trajectory_pipeline/output/pipeline/dualrun2/obscura \
                trajectory_pipeline/output/pipeline/dualrun2/camoufox \
        [--json trajectory_pipeline/research/probes/inc72_ladder_identity.json]

**只读**：不写 P1、不改代码、不联网。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Mapping

# 观察槽的枚举**直接用生产代码的那个**——探针必须跟报告读**同一批观察**，
# 少读一个槽就会得出「中位数对不上」这种假矛盾（2026-10-21 实测踩过：
# 只读 visits[].site_obs 得 43 条，报告读全部槽得 114 条）。
from trajectory_pipeline.executor.dual_run import _observations


def _slot_labels(record: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """把存档摊成 ``(槽位名, 观察)`` 列表——槽位名只用于分组统计。"""
    out: list[tuple[str, Mapping[str, Any]]] = []
    obs = record.get("search_observation")
    if isinstance(obs, Mapping):
        out.append(("search", obs))
    for i, step in enumerate(record.get("steps") or ()):
        o = (step or {}).get("observation")
        if isinstance(o, Mapping):
            out.append((f"step{i}", o))
    for vi, visit in enumerate(record.get("visits") or ()):
        visit = visit or {}
        for key in ("site_obs", "player_obs"):
            o = visit.get(key)
            if isinstance(o, Mapping):
                out.append((f"visit{vi}.{key}", o))
        for si, step in enumerate(visit.get("steps") or ()):
            o = (step or {}).get("observation")
            if isinstance(o, Mapping):
                out.append((f"visit{vi}.step{si}", o))
    return out


def load_observations(path: pathlib.Path) -> list[dict[str, Any]]:
    """读一个存档目录里的全部 P1 存档，返回每个观察的 ``element_basis.shared``。"""
    out: list[dict[str, Any]] = []
    for f in sorted(path.glob("*.json")):
        if f.name == "negative.jsonl":
            continue
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(rec, Mapping) or "run_config" not in rec:
            continue
        task = str(rec.get("task_id") or f.stem)
        slots = _slot_labels(rec)
        # 用生产代码的枚举核一遍：槽位集合必须一致，否则探针读的样本 ≠ 报告的样本
        prod_count = sum(1 for _ in _observations(rec))
        if prod_count != len(slots):
            print(f"! {f.name} 槽位数不一致：探针 {len(slots)} vs 生产 {prod_count}",
                  file=sys.stderr)
        for slot, obs in slots:
            basis = obs.get("element_basis")
            if not isinstance(basis, Mapping):
                continue
            shared = basis.get("shared")
            if not isinstance(shared, Mapping):
                continue
            out.append({
                "file": f.name,
                "task": task,
                "slot": slot,
                "landed_url": str(obs.get("url") or obs.get("landed_url") or ""),
                "shared": {k: int(val) for k, val in shared.items()
                           if isinstance(val, int) and not isinstance(val, bool)},
            })
    return out


def _slot_family(slot: str) -> str:
    """槽位 → 族。同一族里的页面**形态相近**，混在一起取中位数会互相稀释。"""
    if slot == "search":
        return "search"
    if slot.startswith("visit"):
        if slot.endswith(".site_obs"):
            return "site_obs"
        if slot.endswith(".player_obs"):
            return "player_obs"
        return "visit_step"
    return "step"


def _check(obs: list[dict[str, Any]], label: str) -> dict[str, Any]:
    """对一个目录的观察逐条核对恒等式，返回核对结果。"""
    violations: Counter = Counter()
    samples: dict[str, list[str]] = {}
    for o in obs:
        s = o["shared"]

        def bad(tag: str) -> None:
            violations[tag] += 1
            samples.setdefault(tag, []).append(
                f"{o['file']} {o['landed_url'][:60]} {s}"
            )

        cand, hid, vis = s.get("candidates"), s.get("hidden"), s.get("visible")
        if None not in (cand, hid, vis) and cand != hid + vis:
            bad("candidates != hidden + visible")
        rm, ru = s.get("role_matched"), s.get("role_unmatched")
        if None not in (vis, rm, ru) and vis != rm + ru:
            bad("visible != role_matched + role_unmatched")
        kept, trunc = s.get("kept"), s.get("truncated")
        nam, amb = s.get("nameless_dropped"), s.get("ambiguous_dropped")
        if None not in (rm, nam, amb, kept, trunc) and kept + trunc != rm - nam - amb:
            bad("kept + truncated != role_matched - nameless - ambiguous")

    # 各档的分布——说明「中位数不可加」到底长什么样
    dist: dict[str, Any] = {}
    for stage in ("candidates", "hidden", "visible", "role_matched",
                  "role_unmatched", "nameless_dropped", "ambiguous_dropped",
                  "kept", "truncated"):
        vals = [o["shared"][stage] for o in obs if stage in o["shared"]]
        if not vals:
            continue
        dist[stage] = {
            "n": len(vals),
            "min": min(vals),
            "median": float(statistics.median(vals)),
            "max": max(vals),
            "mean": round(sum(vals) / len(vals), 2),
        }
    # 「kept / role_matched」的**逐观察比值**中位数——比两个中位数相除更接近真实流失
    ratios = [
        o["shared"]["kept"] / o["shared"]["role_matched"]
        for o in obs
        if o["shared"].get("role_matched")
    ]
    # 按**槽位族**分开看——「整体中位数」是谁主导的，一眼可见
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for o in obs:
        families[_slot_family(o["slot"])].append(o)
    by_family: dict[str, Any] = {}
    for fam, items in sorted(families.items()):
        entry: dict[str, Any] = {"n": len(items)}
        for stage in ("candidates", "visible", "role_matched", "kept"):
            vals = [i["shared"][stage] for i in items if stage in i["shared"]]
            if vals:
                entry[stage + "_median"] = float(statistics.median(vals))
        by_family[fam] = entry
    return {
        "side": label,
        "observations": len(obs),
        "violations": dict(violations),
        "violation_samples": {k: v[:3] for k, v in samples.items()},
        "stage_distribution": dist,
        "by_slot_family": by_family,
        "kept_over_role_matched_median": (
            round(statistics.median(ratios), 4) if ratios else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="共享账本逐观察恒等式核对")
    ap.add_argument("--dirs", nargs="+", required=True, help="存档目录（一个或多个）")
    ap.add_argument("--json", default=None, help="结果落盘路径")
    args = ap.parse_args(argv)

    report: dict[str, Any] = {"sides": []}
    for d in args.dirs:
        path = pathlib.Path(d)
        if not path.is_dir():
            print(f"目录不存在：{path}", file=sys.stderr)
            return 2
        obs = load_observations(path)
        side = _check(obs, path.name)
        report["sides"].append(side)
        print(f"\n===== {path.name} =====")
        print(f"带共享账的观察：{side['observations']}")
        if side["violations"]:
            print("恒等式违例：")
            for k, n in side["violations"].items():
                print(f"  ! {k} ×{n}")
                for s in side["violation_samples"].get(k, []):
                    print(f"      {s}")
        else:
            print("恒等式：逐条都对得上（无违例）")
        print("各档分布：")
        for stage, d2 in side["stage_distribution"].items():
            print(f"  {stage:<20} n={d2['n']:<4} min={d2['min']:<4} "
                  f"median={d2['median']:<8} mean={d2['mean']:<8} max={d2['max']}")
        print(f"逐观察 kept/role_matched 的中位数：{side['kept_over_role_matched_median']}")
        print("按槽位族分开：")
        for fam, e in side["by_slot_family"].items():
            print(f"  {fam:<12} n={e['n']:<4} "
                  + " ".join(f"{k}={v}" for k, v in e.items() if k != "n"))

    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n落盘: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
