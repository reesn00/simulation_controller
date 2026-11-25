import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
print("keys:", [k for k in d if k in (
    "executor_mode", "discovery_path", "driver", "path", "task_id",
    "intermediates", "visits", "steps", "outcomes")])
print("executor_mode:", d.get("executor_mode"), "discovery_path:", d.get("discovery_path"))
print("steps(top):", len(d.get("steps") or ()))
print("visits:", len(d.get("visits") or ()))
print("intermediates:", len(d.get("intermediates") or ()))
print()
for i, ir in enumerate(d.get("intermediates") or (), 1):
    st = ir.get("steps") or ()
    print(f"--- intermediate#{i} role={ir.get('role')} url={str(ir.get('url'))[:60]}")
    print(f"    steps={len(st)} notes={ir.get('notes')}")
    print(f"    is_intermediate={ (ir.get('is_intermediate') or {}).get('answer') if isinstance(ir.get('is_intermediate'), dict) else ir.get('is_intermediate')}")
    print(f"    entries={ (ir.get('entries') or {}).get('answer') if isinstance(ir.get('entries'), dict) else ir.get('entries')}")
    print(f"    candidates={len(ir.get('candidates') or ())}")
    for s in st:
        a = s.get("action") or {}
        print(f"      step: tool={a.get('tool')} origin={a.get('origin')} "
              f"target={a.get('target')} err={s.get('error')!r} "
              f"obs={'yes' if s.get('observation') else 'no'} "
              f"rationale={str(s.get('rationale') or '')[:60]!r}")
print()
print("outcomes:", [(o.get("branch"), str(o.get("url"))[:40]) for o in (d.get("outcomes") or ())])
