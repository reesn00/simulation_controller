# -*- coding: utf-8 -*-
"""INC42：分类 15 对工作表的 shortfall 原因——定向修复用。"""
import json
import pathlib
import sys

sys.path.insert(0, ".")

from trajectory_pipeline.evaluation.scorers.d9_scorer import _observation_shortfall

WS = pathlib.Path("tmp/inc40_retest_worksheet_data.jsonl")
pairs = [json.loads(l) for l in WS.read_text(encoding="utf-8").splitlines() if l.strip()]

print(f"{'golden_id':40s} {'step':4s} shortfall 原因")
for p in pairs:
    gid = f"{p['golden_id']}"
    if p["kind"] == "MISS" or not p.get("rationale"):
        print(f"{gid:40s} {p['step']:<4d} (无数据)")
        continue
    o = p["observation"]
    r = _observation_shortfall(o)
    label = r if r else "可判"
    print(f"{gid:40s} {p['step']:<4d} {label}")
    if r:
        print(f"{'':45s}truncated={o.get('truncated')!r} degraded={o.get('degraded')} "
              f"elems={len(o.get('interactive_elements') or [])}/{o.get('elements_total')} "
              f"body_len={len(o.get('body_text') or '')}")
