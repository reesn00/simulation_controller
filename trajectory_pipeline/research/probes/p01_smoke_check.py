import json
import sys
from collections import Counter

path = sys.argv[1]
d = json.load(open(path, encoding="utf-8"))
print("run:", {k: d.get(k) for k in ("executor_mode", "driver", "elapsed_ms", "batch_id")})
visits = d.get("visits") or ()
print("visits:", len(visits))
print("outcomes:", Counter(o.get("branch") for o in (d.get("outcomes") or ())))
print()
print("== new_tab steps: is observation filled? ==")
tot = 0
filled = 0
for v in visits:
    for s in v.get("steps") or ():
        a = s.get("action") or {}
        if a.get("tool") != "new_tab":
            continue
        tot += 1
        o = s.get("observation")
        is_placeholder = isinstance(o, dict) and "observation_missing" in (o.get("degraded") or ())
        ok = bool(o) and not is_placeholder
        if ok:
            filled += 1
        if tot <= 3:
            body = o.get("body") if isinstance(o, dict) else None
            els = o.get("elements") if isinstance(o, dict) else None
            print(f"  url={str(v.get('url'))[:56]}")
            print(f"    keys={sorted((o or {}).keys())[:8]} body_len={len(str(body or ''))} els={len(els or [])} placeholder={is_placeholder}")
print(f"new_tab steps: {tot}, non-empty observation: {filled}")
print()
print("== exit signals ==")
sig = Counter()
for v in visits:
    s = v.get("exit_signal")
    if s:
        sig[s] += 1
for o in d.get("outcomes") or ():
    if o.get("exit_signal"):
        sig["outcome:" + str(o["exit_signal"])] += 1
print(dict(sig) or "(none)")
print("stale_context anywhere:", "stale_context" in json.dumps(d, ensure_ascii=False))
