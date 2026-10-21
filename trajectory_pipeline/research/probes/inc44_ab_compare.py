# -*- coding: utf-8 -*-
"""INC44 A/B：INC43 轮 vs INC44 轮 75 对直采分支对比 + 簇 A 9 行专项核对。"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")


def load(p):
    d = {}
    for line in io.open(p, encoding="utf-8"):
        if line.strip():
            o = json.loads(line)
            d.setdefault((o["task_id"], o["url"]), o)
    return d


def main():
    rec = Path(__file__).resolve().parent.parent / "recollect"
    old = load(rec / "rounds" / "inc43_backup.jsonl")
    new = load(rec / "outcomes.jsonl")
    diff = [
        (k, old[k], new[k])
        for k in old
        if k in new
        and (old[k].get("branch") or "success") != (new[k].get("branch") or "success")
    ]
    print(f"A/B 分支变化 {len(diff)} 对（INC43 -> INC44）:")
    for k, o, n in diff:
        print(
            f"  {k[0][:12]:12s} {k[1][:48]:48s} "
            f"{o.get('branch') or 'success':22s} -> {n.get('branch') or 'success'}"
        )
        ev = (n.get("evidence") or "").replace("\n", " ")
        if "[付费墙救济]" in ev or "[记账降级]" in ev:
            print(f"      {ev[:110]}")

    # 簇 A 专项：上轮 9 行「记账降级」unresolved
    print("\n== 簇 A 专项（INC43 轮含 [记账降级] 的行）==")
    cluster_a = [k for k, o in old.items() if "[记账降级]" in (o.get("evidence") or "")]
    print(f"上轮记账降级行：{len(cluster_a)}")
    fixed = still = gone = 0
    for k in cluster_a:
        o, n = old[k], new.get(k)
        if n is None:
            gone += 1
            continue
        ob, nb = o.get("branch") or "success", n.get("branch") or "success"
        mark = "✅" if nb != "unresolved" else "⚠️ 仍 unresolved"
        if nb != "unresolved":
            fixed += 1
        else:
            still += 1
        ev = (n.get("evidence") or "").replace("\n", " ")
        rescue = "[付费墙救济]" in ev
        print(f"  {mark} {k[0][:12]:12s} {ob} -> {nb}  救济标记={rescue}")
        if rescue or nb == "subscription_wall":
            print(f"      {ev[:110]}")
    print(f"\n簇 A 脱离 unresolved：{fixed} | 仍 unresolved：{still} | 未直访：{gone}")


if __name__ == "__main__":
    main()
