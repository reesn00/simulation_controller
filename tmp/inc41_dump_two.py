# -*- coding: utf-8 -*-
"""INC41：补齐 E023 / E001 两锚点的 round=2 判据面。"""
import json
import pathlib

a = json.loads(pathlib.Path("trajectory_pipeline/output/pipeline/E023__4d175424.json").read_text(encoding="utf-8"))
print("E023 visits:", len(a.get("visits") or []))
for v in a.get("visits") or []:
    print("  visit url:", (v.get("url") or "")[:70], "| steps:", len(v.get("steps") or []))
    for j, s in enumerate(v.get("steps") or []):
        o = s.get("observation") or {}
        print(f"    step{j}: title={(o.get('page_title') or '')[:50]!r} video={o.get('video_tag_count')} degraded={o.get('degraded')}")
        rat = s.get("rationale") or {}
        rtext = (rat.get("rationale") or {}).get("text", "") if isinstance(rat.get("rationale"), dict) else ""
        if rtext:
            print(f"      rationale[:150]: {rtext[:150]}")

d = json.loads(pathlib.Path("trajectory_pipeline/output/pipeline/samples/E001__57bbf995__site-2.json").read_text(encoding="utf-8"))
six = d.get("six") or {}
print()
print("E001 site-2 keys:", list(d.keys()))
print("outcome:", json.dumps(d.get("outcome") or {}, ensure_ascii=False)[:400])
for j, (o, r) in enumerate(zip(six.get("observations") or [], six.get("rationales") or [])):
    rat = r if isinstance(r, str) else (r or {}).get("text", str(r))
    print(f"  step{j}: title={(o or {}).get('page_title', '')[:60]!r} video={(o or {}).get('video_tag_count')} degraded={(o or {}).get('degraded')}")
    print(f"    rationale[:180]: {str(rat)[:180]}")
