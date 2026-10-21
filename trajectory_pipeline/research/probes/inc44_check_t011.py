# -*- coding: utf-8 -*-
"""查 T011 v.qq.com/error.html 的金标 + 该行为何没进 A/B 金标对齐集。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

rows = []
for f in ["calibration.jsonl", "holdout.jsonl"]:
    p = Path("trajectory_pipeline/evaluation/golden/data/v3") / f
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))

for r in rows:
    if r.get("source_kind") == "p2_sample" or not r.get("golden"):
        continue
    u = (r.get("url") or "").lower()
    if "error.html" in u or r["task_id"] == "T011":
        g = r.get("golden") or {}
        print(
            f"{r['task_id']:8s} {r['golden_id'][:34]:34s} "
            f"金标={g.get('label'):20s} url={r.get('url')}"
        )

# 再看 A/B 里 T011 的两轮 outcome key
import io
rec = Path(__file__).resolve().parent.parent / "recollect"
for p in [rec / "rounds" / "inc43_backup.jsonl", rec / "outcomes.jsonl"]:
    for line in io.open(p, encoding="utf-8"):
        if not line.strip():
            continue
        o = json.loads(line)
        if "error.html" in (o.get("url") or ""):
            print(f"{p.name[:34]:34s} task={o['task_id']:8s} branch={o.get('branch')}")
