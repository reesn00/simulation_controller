# -*- coding: utf-8 -*-
"""INC42：分析修复后的 judge 重测结果——覆盖率 + A/B 稳定性 + undetermined 原因。"""
import json
import pathlib

SRC = pathlib.Path(__file__).resolve().parent / "inc41_judge_retest.jsonl"
rows = [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines() if l.strip()]

by_pair = {}
for r in rows:
    if r.get("round") == "-":
        continue
    by_pair.setdefault((r["golden_id"], r["step"]), {})[r["round"]] = r

judged = 0
ab_stable = 0
print("=== 逐对结果 ===")
for (gid, step), rr in sorted(by_pair.items()):
    a, b = rr.get("A"), rr.get("B")
    va = a.get("verdict") or a.get("d9_kind") or a.get("kind", "?")
    vb = b.get("verdict") or b.get("d9_kind") or b.get("kind", "?")
    scored = a.get("verdict") is not None
    if scored:
        judged += 1
    stable = (a.get("verdict") == b.get("verdict")) and (a.get("kind") == b.get("kind"))
    if stable:
        ab_stable += 1
    mark = "scored" if scored else "undet  "
    print(f"{gid:38s} step={step:<3d} {mark}  A={va:20s} B={vb:20s} {'✓AB一致' if stable else '✗AB分歧'}")

total = len(by_pair)
print(f"\n=== 汇总 ===")
print(f"可判对总数（排除 no_data）: {total}")
print(f"judge 有效覆盖（出 verdict）: {judged}/{total} （INC41 修复前 1/{total}）")
print(f"A/B 两轮判定完全一致: {ab_stable}/{total}")

print(f"\n=== undetermined 原因 ===")
seen = set()
for (gid, step), rr in sorted(by_pair.items()):
    a = rr.get("A")
    if a and a.get("verdict") is None:
        reason = (a.get("reasons") or ["?"])[0][:150]
        print(f"{gid:38s} step={step:<3d} {reason}")
