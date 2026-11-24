import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
for i, v in enumerate(d.get("visits") or ()):
    steps = v.get("steps") or ()
    nt = None
    for s in steps:
        if (s.get("action") or {}).get("tool") == "new_tab":
            nt = s
            break
    if nt is None:
        continue
    obs = nt.get("observation") or {}
    els = obs.get("interactive_elements") or ()
    els = list(els)
    # names available on the page the agent first sees
    names = set()
    for e in els:
        if isinstance(e, dict):
            nm = e.get("name")
            if nm:
                names.add(str(nm).strip())
    # first model-originated action AFTER new_tab
    first = None
    for s in steps[steps.index(nt) + 1:]:
        a = s.get("action") or {}
        if (a.get("origin") or "model") == "infrastructure":
            continue
        first = s
        break
    print(f"--- visit#{i} {str(v.get('url'))[:52]}")
    print(f"    new_tab obs: elements={len(els)} names={len(names)}")
    if first is None:
        print("    no model action after new_tab")
        continue
    a = first.get("action") or {}
    tgt = a.get("target") or {}
    print(f"    first action: {a.get('tool')} target={tgt} origin={a.get('origin')}")
    print(f"    step error: {first.get('error')}")
    if tgt:
        tn = (tgt.get("name") or "").strip()
        print(f"    target name in new_tab names? {tn in names} ({tn!r})")
