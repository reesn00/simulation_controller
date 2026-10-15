# -*- coding: utf-8 -*-
"""INC40 联动 #1 铺底：导出 12 条 D9 retest 锚点对应的 (observation, rationale)
数据对，生成 2026-10-27 round=2 执行日的人工 verdict 核实工作单数据。

映射链：golden url（= outcome.url 最终落点）→ outcome.site_url（导航入口）
→ 存档 visits 里 url == site_url 的 visit → 该 visit 下带 rationale 的 step。
P2 sample 锚点（E001__57bbf995__site-2）直接读 samples/*.json。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

queue = [json.loads(l) for l in Path(
    "trajectory_pipeline/evaluation/golden/data/d9_retest_queue.jsonl"
).read_text(encoding="utf-8").splitlines() if l.strip()]

pipe = Path("trajectory_pipeline/output/pipeline")

out = []
for q in queue:
    gid = q["golden_id"]
    if "__site-" in gid:  # P2 sample
        d = json.loads((pipe / "samples" / f"{gid}.json").read_text(encoding="utf-8"))
        six = d["six"]
        for i, (o, r) in enumerate(zip(six["observations"], six["rationales"])):
            out.append({
                "golden_id": gid, "kind": "p2_sample", "step": i,
                "url": (d.get("outcome") or {}).get("url", ""),
                "round_1_label": q["round_1_label"],
                "observation": o, "rationale": r,
            })
        continue
    task, run, _ = gid.rsplit("__", 2)
    a = json.loads((pipe / f"{task}__{run}.json").read_text(encoding="utf-8"))
    # golden url → outcome 行 → site_url → visit
    site_url = None
    for o in a.get("outcomes") or []:
        if o.get("url") == q["url"]:
            site_url = o.get("site_url") or o.get("url")
            break
    visit = None
    if site_url:
        for v in a.get("visits") or []:
            if v.get("url") == site_url:
                visit = v
                break
    if visit is None:
        out.append({"golden_id": gid, "kind": "MISS", "step": -1,
                    "url": q.get("url", ""), "round_1_label": q["round_1_label"],
                    "observation": None, "rationale": None})
        continue
    for i, s in enumerate(visit.get("steps") or []):
        if s.get("rationale") and s.get("observation"):
            rat = s["rationale"]["rationale"] or {}
            out.append({
                "golden_id": gid, "kind": "archive_visit", "step": i,
                "url": q["url"],
                "round_1_label": q["round_1_label"],
                "observation": s["observation"],
                "rationale": {"text": rat.get("text", ""),
                              "conclusion": rat.get("conclusion", "")},
            })

Path("tmp_inc40_retest_worksheet_data.jsonl").write_text(
    "\n".join(json.dumps(o, ensure_ascii=False) for o in out), encoding="utf-8")

miss = [o for o in out if o["kind"] == "MISS"]
print(f"12 条锚点 → {len(out)} 个 (obs, rationale) 对；未命中 {len(miss)}")
for m in miss:
    print("  MISS:", m["golden_id"], m["url"][:60])
from collections import Counter
print("kind:", dict(Counter(o["kind"] for o in out)))
# 每锚点多少对
per = Counter(o["golden_id"] for o in out)
print("每锚点对数:", dict(per))
