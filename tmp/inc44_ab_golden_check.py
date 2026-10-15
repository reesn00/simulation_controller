# -*- coding: utf-8 -*-
"""INC44 A/B 变化行金标核对：量化 ④ 救济的净收益/净代价。"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from trajectory_pipeline.evaluation.golden.alignment import is_equivalent


def load_outcomes(p):
    d = {}
    for line in io.open(p, encoding="utf-8"):
        if line.strip():
            o = json.loads(line)
            d.setdefault((o["task_id"], o["url"]), o)
    return d


def main():
    old = load_outcomes("tmp/recollect_outcomes_inc43_backup.jsonl")
    new = load_outcomes("tmp_recollect_outcomes.jsonl")

    rows = []
    for f in ["calibration.jsonl", "holdout.jsonl"]:
        p = Path("trajectory_pipeline/evaluation/golden/data/v3") / f
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    by_key = {}
    for r in rows:
        if r["source_kind"] == "p2_sample" or not r.get("golden"):
            continue
        by_key.setdefault((r["task_id"], r["url"]), r)
    gl = lambda r: (r.get("golden") or {}).get("label") or "success"

    diff = [
        (k, old[k], new[k])
        for k in old
        if k in new and k in by_key
        and (old[k].get("branch") or "success") != (new[k].get("branch") or "success")
    ]
    print(f"A/B 变化且金标可对齐：{len(diff)} 行")
    tp = fp = tn = fn = 0
    for k, o, n in diff:
        g = gl(by_key[k])
        ob, nb = o.get("branch") or "success", n.get("branch") or "success"
        was_ok, now_ok = is_equivalent(g, ob), is_equivalent(g, nb)
        if not was_ok and now_ok:
            tp += 1; mark = "✅修复"
        elif was_ok and not now_ok:
            fp += 1; mark = "❌反转"
        elif not was_ok and not now_ok:
            fn += 1; mark = "➖错->错"
        else:
            tn += 1; mark = "⚠️对->对(换档)"
        print(f"  {mark} 金标={g:20s} {ob:22s} -> {nb}  {k[0][:10]} {k[1][:40]}")
    print(f"\n修复 {tp} | 反转 {fp} | 错->错 {fn} | 对->对 {tn}")


if __name__ == "__main__":
    main()
