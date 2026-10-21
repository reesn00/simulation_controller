# -*- coding: utf-8 -*-
"""INC41：导出 12 条 retest 锚点的判据面（outcome 证据 + visit 摘要），
供 round=2 盲打重判使用。不输出 round_1_label，保持盲态。"""
import json
import pathlib
import sys

sys.path.insert(0, ".")

from trajectory_pipeline.evaluation.golden.builder import _load_branch_labels

labels = _load_branch_labels()
print("=== 标签注册表 ===")
for k, v in labels.items():
    print(f"  {k}: {v}")

queue = [
    json.loads(l)
    for l in pathlib.Path(
        "trajectory_pipeline/evaluation/golden/data/d9_retest_queue.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    if l.strip()
]

print()
print("=== 12 锚点判据面（盲打：不看 round_1_label） ===")
for q in queue:
    gid = q["golden_id"]
    if "__site-" in gid:
        d = json.loads(
            pathlib.Path(
                f"trajectory_pipeline/output/pipeline/samples/{gid}.json"
            ).read_text(encoding="utf-8")
        )
        oc = d.get("outcome") or {}
        print(f"\n[{gid}] (p2_sample)")
        print(f"  outcome: {json.dumps(oc, ensure_ascii=False)[:500]}")
        continue
    task, run, _ = gid.rsplit("__", 2)
    a = json.loads(
        pathlib.Path(
            f"trajectory_pipeline/output/pipeline/{task}__{run}.json"
        ).read_text(encoding="utf-8")
    )
    for o in a.get("outcomes") or []:
        if o.get("url") == q["url"]:
            print(f"\n[{gid}]")
            print(f"  url: {o.get('url')}")
            print(f"  code_branch: {o.get('branch')} ({o.get('branch_label')})")
            print(f"  source: {o.get('source')} | fallback: {o.get('fallback_used')}")
            print(f"  evidence: {o.get('evidence', '')[:200]}")
            print(f"  play_page_url: {o.get('play_page_url', '')!r} reached: {o.get('reached_play_page')}")
            # 对应 visit 的末步观察摘要
            site = o.get("site_url") or o.get("url")
            for v in a.get("visits") or []:
                if v.get("url") == site:
                    steps = v.get("steps") or []
                    if steps:
                        s = steps[-1].get("observation") or {}
                        print(f"  visit_steps: {len(steps)} | 末步 page_title: {s.get('page_title', '')[:80]}")
                        print(f"    video_tag={s.get('video_tag_count')} iframe={s.get('iframe_count')} degraded={s.get('degraded')}")
                        bt = (s.get("body_text") or "")[:150].replace(chr(10), " / ")
                        print(f"    body[:150]: {bt}")
                    break
            break
