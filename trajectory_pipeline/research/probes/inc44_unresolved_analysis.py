# -*- coding: utf-8 -*-
"""INC44 前研：unresolved 簇（36/79）构成分析——按站点/证据/降级状态归类。

数据源：research/recollect/outcomes.jsonl（INC43 轮 75 对直采，与
research/recollect/measure.py 同口径：90 行并集直访命中 79 行）。
只做归类，不发请求。
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, ".")
from trajectory_pipeline.evaluation.golden.alignment import is_equivalent

SUCCESS = "success"


def load_rows():
    rows = []
    for f in ["calibration.jsonl", "holdout.jsonl"]:
        p = Path("trajectory_pipeline/evaluation/golden/data/v3") / f
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main():
    outcomes = {}
    src = Path(__file__).resolve().parent.parent / "recollect" / "outcomes.jsonl"
    for line in src.read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line)
            outcomes.setdefault((o["task_id"], o["url"]), o)

    rows = load_rows()
    site = [r for r in rows if r["source_kind"] != "p2_sample" and r.get("golden")]
    br = lambda r: (r.get("code") or {}).get("branch") or SUCCESS
    gl = lambda r: (r.get("golden") or {}).get("label") or SUCCESS
    union = [r for r in site
             if (not is_equivalent(gl(r), br(r)))
             or ("采集降级" in ((r.get("code") or {}).get("evidence") or ""))]

    unresolved = []
    for r in union:
        o = outcomes.get((r["task_id"], r["url"]))
        if o is None:
            continue
        nb = o.get("branch") or SUCCESS
        if nb == "unresolved":
            unresolved.append((r, o))

    print(f"直访命中且新分支=unresolved：{len(unresolved)} 行\n")

    # 按域名归类
    from urllib.parse import urlparse
    by_site = defaultdict(list)
    for r, o in unresolved:
        host = urlparse(o["url"]).netloc or o["url"][:30]
        by_site[host].append((r, o))

    print("== 按站点 ==")
    for host, items in sorted(by_site.items(), key=lambda kv: -len(kv[1])):
        golds = Counter(gl(r) for r, _ in items)
        print(f"  {host:45s} {len(items):2d} 行  金标分布 {dict(golds)}")

    # 按证据形态归类
    print("\n== 按证据形态（evidence 关键段）==")
    sig = Counter()
    for r, o in unresolved:
        ev = o.get("evidence") or ""
        if "采集降级" in ev:
            # 找出降级字段
            import re
            m = re.search(r"采集降级（(\[.*?\])）", ev)
            sig[f"降级 {m.group(1) if m else '?'}"] += 1
        elif "正文过短" in ev or "空" in ev.split("；")[0]:
            sig["正文空/过短（真渲染失败）"] += 1
        elif "判定异常" in ev:
            sig["判定异常兜底"] += 1
        else:
            sig["LLM 判 null / 其他"] += 1
    for k, v in sig.most_common():
        print(f"  {v:2d}  {k}")

    # 逐行明细
    print("\n== 逐行明细（golden_id | 金标 | 证据摘要）==")
    for r, o in sorted(unresolved, key=lambda x: urlparse(x[1]["url"]).netloc):
        ev = (o.get("evidence") or "").replace("\n", " ")[:110]
        print(f"  {gl(r):20s} {r['golden_id'][:38]:38s} {ev}")

    # 金标对齐角度：unresolved 里金标是什么
    print("\n== 金标视角：这 36 行的金标如果实现，D-10 能到多少 ==")
    golds = Counter(gl(r) for r, _ in unresolved)
    for g, c in golds.most_common():
        print(f"  {g:20s} {c}")
    # 全 79 直访命中行的金标 vs 新分支全错配数
    mismatched = sum(
        1 for r in union
        if (o := outcomes.get((r["task_id"], r["url"])))
        and not is_equivalent(gl(r), o.get("branch") or SUCCESS)
    )
    print(f"\n79 直访命中行中新分支仍错配：{mismatched}（unresolved 占 36，其余 {mismatched-36}）")


if __name__ == "__main__":
    main()
