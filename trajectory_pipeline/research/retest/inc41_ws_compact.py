# -*- coding: utf-8 -*-
"""INC41：工作表 15 对紧凑视图——rationale 全文 + observation 关键面，
供三类 verdict（hindsight/systematic/misleading）分析判据用。"""
import json
import pathlib

WS = pathlib.Path(__file__).resolve().parent / "inc40_retest_worksheet_data.jsonl"
pairs = [json.loads(l) for l in WS.read_text(encoding="utf-8").splitlines() if l.strip()]

for p in pairs:
    print("=" * 100)
    print(f"{p['golden_id']}  step={p['step']}  kind={p['kind']}")
    if p["kind"] == "MISS" or not p.get("rationale"):
        print("  (无数据)")
        continue
    o, r = p["observation"], p["rationale"]
    print(f"  url: {o.get('url', '')[:90]}")
    print(f"  page_title: {o.get('page_title', '')}")
    print(f"  video_tag={o.get('video_tag_count')} iframe={o.get('iframe_count')} "
          f"elements={len(o.get('interactive_elements') or [])}/{o.get('elements_total')} "
          f"degraded={o.get('degraded')} truncated={o.get('truncated')}")
    body = o.get("body_text") or ""
    print(f"  body_len={len(body)}")
    print(f"  RATIONALE: {r.get('text', '')}")
    print(f"  CONCLUSION: {r.get('conclusion', '')}")
