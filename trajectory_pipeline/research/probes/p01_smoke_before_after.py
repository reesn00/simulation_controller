"""P0-1 验证：同一存档，S3 前 vs 后，首步 click 的定位结果。

S3 前：initial_obs 是**入口页**观察（site_obs），agent 拿它的元素清单选目标，
       而 new_tab 之后驱动内的定位表已被重置 ⇒ 查不到 ⇒ 静默 not_found。
S3 后：initial_obs 是**新 tab** 的观察 ⇒ 目标必然在新页的清单里。

本脚本只比对「目标名在新 tab 清单里 vs 在入口页清单里」，不跑浏览器。
"""
import json
import sys

d = json.load(open(sys.argv[1], encoding="utf-8"))
for i, v in enumerate(d.get("visits") or ()):
    steps = v.get("steps") or ()
    site_obs = v.get("site_obs") or {}
    nt = None
    for s in steps:
        if (s.get("action") or {}).get("tool") == "new_tab":
            nt = s
            break
    if nt is None:
        continue
    new_obs = nt.get("observation") or {}
    first = None
    for s in steps[steps.index(nt) + 1:]:
        a = s.get("action") or {}
        if (a.get("origin") or "model") == "infrastructure":
            continue
        first = s
        break
    if first is None:
        continue
    tgt = ((first.get("action") or {}).get("target") or {})
    if (first.get("action") or {}).get("tool") not in ("click", "fill"):
        continue
    tn = (tgt.get("name") or "").strip()

    def names_of(o):
        return {
            str(e.get("name") or "").strip()
            for e in (o.get("interactive_elements") or ())
            if isinstance(e, dict)
        }

    in_new = tn in names_of(new_obs)
    in_site = tn in names_of(site_obs)
    print(f"visit#{i} {str(v.get('url'))[:48]}")
    print(f"  target={tn!r}")
    print(f"  in new_tab elements : {in_new}   ({len(names_of(new_obs))} names)")
    print(f"  in site_obs elements: {in_site}   ({len(names_of(site_obs))} names)")
    if in_new and not in_site:
        print("  => S3 前必然 not_found（静默空转）；S3 后命中")
