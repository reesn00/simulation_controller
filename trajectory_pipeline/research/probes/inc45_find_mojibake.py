# -*- coding: utf-8 -*-
"""簇 B 前研：mojibake 3 行的 URL、evidence、与金标。"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")


def main():
    outcomes = {}
    src = Path(__file__).resolve().parent.parent / "recollect" / "outcomes.jsonl"
    for line in io.open(src, encoding="utf-8"):
        if line.strip():
            o = json.loads(line)
            outcomes.setdefault((o["task_id"], o["url"]), o)

    rows = []
    for f in ["calibration.jsonl", "holdout.jsonl"]:
        p = Path("trajectory_pipeline/evaluation/golden/data/v3") / f
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    site = [r for r in rows if r["source_kind"] != "p2_sample" and r.get("golden")]

    for r in site:
        k = (r["task_id"], r.get("url") or "")
        o = outcomes.get(k)
        if o is None:
            continue
        ev = o.get("evidence") or ""
        if "mojibake" in ev or "乱码" in ev or "\ufffd" in ev:
            g = (r.get("golden") or {}).get("label")
            print(f"金标={g:20s} {r['golden_id'][:36]}")
            print(f"  task={o['task_id']} url={o['url']}")
            print(f"  branch={o.get('branch')}")
            print(f"  evidence={ev[:200]}")
            print(f"  U+FFFD in evidence: {ev.count(chr(0xfffd))}")
            print()


if __name__ == "__main__":
    main()
