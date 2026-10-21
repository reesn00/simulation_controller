# -*- coding: utf-8 -*-
"""INC43 A/B：对比 INC42 时代直采（research/recollect/rounds/inc42_backup.jsonl）
与 INC43 修复后直采（research/recollect/outcomes.jsonl），隔离本轮判定层收益。

- 两轮都是 URL 直访、同一 75 (task,url) 对，唯一差异是 INC43 三处修复
  （textContent 剥离 script、②③负分支救济、APP墙词表）
- 逐对对比分支变化，并按金标归类：修复 / 反转 / 无变化
- 单独统计金标 subscription_wall 簇的两轮命中率
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from trajectory_pipeline.evaluation.golden.alignment import is_equivalent

ROOT = Path(".")
HERE = Path(__file__).resolve().parent
RECOLLECT = HERE.parent / "recollect"


def load_outcomes(p: Path) -> dict:
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            out[(r["task_id"], r["url"])] = r["branch"]
    return out


def equiv(gold: str, pred: str | None) -> bool:
    # 个别 outcome 的 branch 为 None（异常兜底）——按不等价处理
    return bool(pred) and is_equivalent(gold, pred)


def load_golden_rows():
    rows = []
    for f in ["calibration.jsonl", "holdout.jsonl"]:
        p = ROOT / "trajectory_pipeline/evaluation/golden/data/v3" / f
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return {r["golden_id"]: r for r in rows if r["source_kind"] != "p2_sample" and r.get("golden")}


def main() -> None:
    old = load_outcomes(RECOLLECT / "rounds" / "inc42_backup.jsonl")
    new = load_outcomes(RECOLLECT / "outcomes.jsonl")
    manifest = {
        m["task_id"]: m
        for m in json.loads(
            (RECOLLECT / "manifest.json").read_text(encoding="utf-8")
        )
    }
    golden = load_golden_rows()

    # o.url 是跳转后实际页 URL（T031 v.qq.com 新轮被重定向到 error.html），
    # 个别对会漂移——按 task 内出现顺序配对更稳。
    from collections import defaultdict

    old_by_task, new_by_task = defaultdict(list), defaultdict(list)
    for (t, u), b in old.items():
        old_by_task[t].append((u, b))
    for (t, u), b in new.items():
        new_by_task[t].append((u, b))
    assert set(old_by_task) == set(new_by_task)
    joined = {}  # 序号 -> (task, old_url, old_branch, new_url, new_branch)
    for t in sorted(old_by_task):
        assert len(old_by_task[t]) == len(new_by_task[t]), f"{t} 行数不一致"
        for i, ((ou, ob), (nu, nb)) in enumerate(zip(old_by_task[t], new_by_task[t])):
            joined[(t, i)] = (t, ou, ob, nu, nb)

    # manifest: task_id -> golden 行（含 url 清单）。构造 (task, i) -> (gid, row)
    pair_gold = {}
    for gid, row in golden.items():
        tid = row["task_id"]
        if tid not in manifest:
            continue
        for i, u in enumerate(manifest[tid].get("urls", []) or []):
            pair_gold[(tid, i)] = (gid, row)

    changed, fixes, reversals = [], [], []
    sub_old_hit = sub_new_hit = sub_total = 0
    for key in sorted(joined):
        t, ou, ob, nu, nb = joined[key]
        if ob != nb:
            changed.append((t, ou, nu, ob, nb))
        g = pair_gold.get(key)
        if g:
            gid, row = g
            gold = (row.get("golden") or {}).get("label") or "success"
            if gold == "subscription_wall":
                sub_total += 1
                sub_old_hit += equiv(gold, ob)
                sub_new_hit += equiv(gold, nb)
            if not equiv(gold, ob) and equiv(gold, nb):
                fixes.append((gid, gold, ob, nb))
            elif equiv(gold, ob) and not equiv(gold, nb):
                reversals.append((gid, gold, ob, nb))

    print(f"两轮共同对数: {len(joined)}")
    print(f"分支发生变化: {len(changed)}")
    for tid, ou, nu, ob, nb in changed:
        print(f"  {tid} {ou[:60]:60s} {ob} => {nb}  (新轮URL: {nu[:50]})")
    print(f"\nINC43 净修复: {len(fixes)} | 净反转: {len(reversals)}")
    for gid, gold, ob, nb in fixes:
        print(f"  ✅ {gid}  {gold}: {ob} => {nb}")
    for gid, gold, ob, nb in reversals:
        print(f"  ❌ {gid}  {gold}: {ob} => {nb}")

    print(
        f"\n金标 subscription_wall 簇（直访覆盖 {sub_total} 行）: "
        f"INC42 轮命中 {sub_old_hit}/{sub_total} -> INC43 轮命中 {sub_new_hit}/{sub_total}"
    )


if __name__ == "__main__":
    main()
