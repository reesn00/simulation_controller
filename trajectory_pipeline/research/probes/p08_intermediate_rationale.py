"""P0-8 探针：中间页 agent 的逐步动作 + 动机，看它为什么不报 links。

跑法（在 trajectory_pipeline/ 下）：
    uv run python research/probes/p08_intermediate_rationale.py <存档.json> [中间页序号]

回答：agent 是「没东西可报」还是「没想到要报」——这两件事在存档里
只差一行 rationale，而它们对应的修法完全相反。
"""
from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    d = json.load(open(argv[0], encoding="utf-8"))
    only = int(argv[1]) if len(argv) > 1 else 0
    print(f"存档 {argv[0]}  executor_mode={d.get('executor_mode')!r}")
    for i, ir in enumerate(d.get("intermediates") or (), 1):
        if only and i != only:
            continue
        url = (ir.get("candidate") or {}).get("url") or "?"
        print(f"\n{'=' * 70}\n# 中间页 {i}  {url[:78]}")
        print(f"  steps={len(ir.get('steps') or ())} candidates={len(ir.get('candidates') or ())}")
        for n in ir.get("notes") or ():
            print(f"  note: {n[:160]}")
        for j, s in enumerate(ir.get("steps") or (), 1):
            a = s.get("action") or {}
            tool = a.get("tool") or "?"
            params = a.get("params") or {}
            brief = {k: (str(v)[:34]) for k, v in params.items()}
            print(f"\n  [{j}] {tool} {brief}")
            if s.get("target"):
                print(f"      target={str(s.get('target'))[:60]}")
            if s.get("error"):
                print(f"      ⚠️ error={str(s.get('error'))[:110]}")
            r = s.get("rationale") or {}
            if isinstance(r, dict):
                body = (r.get("rationale") or {})
                text = body.get("text") if isinstance(body, dict) else str(body)
                concl = body.get("conclusion") if isinstance(body, dict) else None
                src = r.get("reason")
            else:
                text, concl, src = str(r), None, None
            if text:
                print(f"      💭 [{src}] {(text or '').strip()[:300]}")
            if concl:
                print(f"      ➡️ 结论: {str(concl)[:200]}")
        # 终局：agent 最后说了什么
        for s in (ir.get("steps") or ())[-1:]:
            fin = s.get("final") or s.get("answer") or s.get("reply")
            if fin:
                print(f"\n  终局答复: {str(fin)[:300]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
