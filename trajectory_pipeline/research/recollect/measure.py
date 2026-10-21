# -*- coding: utf-8 -*-
"""重采后追溯测量：直采 outcomes 按 (task_id, url) 回填，重算 D-10 投影。

口径与 builder.cmd_agreement 完全一致：
- 等价类判定（alignment.EQUIV_CLASS）
- branch None → success 兜底
- 排除 p2_sample
分母 = 站点级 289；并集外的行保持原判（不重采不受影响）。
select.play_sites 的 9 行不在直采范围，同样保持原判——单列报告。
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, ".")
from trajectory_pipeline.evaluation.golden.alignment import is_equivalent

HERE = Path(__file__).resolve().parent
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
    p = HERE / "outcomes.jsonl"
    if not p.exists():
        print(f"{p} 不存在——先跑同目录 visit.py（需 OBSCURA_EXE + 真浏览器 + 联网）")
        return 1
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line)
            outcomes.setdefault((o["task_id"], o["url"]), o)  # 首条胜出，同 _scan_archives

    rows = load_rows()
    site = [r for r in rows if r["source_kind"] != "p2_sample" and r.get("golden")]
    br = lambda r: (r.get("code") or {}).get("branch") or SUCCESS
    gl = lambda r: (r.get("golden") or {}).get("label") or SUCCESS
    union = [r for r in site
             if (not is_equivalent(gl(r), br(r)))
             or ("采集降级" in ((r.get("code") or {}).get("evidence") or ""))]

    matched = unmatched = fixed = broken = 0
    degraded_fixed = 0
    detail = []
    for r in union:
        old_branch = br(r)
        gold = gl(r)
        o = outcomes.get((r["task_id"], r["url"]))
        if o is None:
            unmatched += 1
            detail.append((r["golden_id"], gold, old_branch, "(未直访)", ""))
            continue
        matched += 1
        new_branch = o.get("branch") or SUCCESS
        was_ok = is_equivalent(gold, old_branch)
        now_ok = is_equivalent(gold, new_branch)
        if not was_ok and now_ok:
            fixed += 1
        elif was_ok and not now_ok:
            broken += 1
        if "采集降级" in ((r.get("code") or {}).get("evidence") or ""):
            if "采集降级" not in (o.get("evidence") or ""):
                degraded_fixed += 1
        detail.append((r["golden_id"], gold, old_branch, new_branch,
                       "降级→" + ("已消除" if "采集降级" not in (o.get("evidence") or "") else "仍在")))

    agree_all = sum(1 for r in site if is_equivalent(gl(r), br(r)))
    new_agree = agree_all + fixed - broken
    print(f"90 行并集：直访命中 {matched} | 未直访（select.play_sites 等）{unmatched}")
    print(f"分支修复（不一致→一致）{fixed} | 分支反转（一致→不一致）{broken}")
    print(f"降级样本 evidence 恢复：{degraded_fixed}/{sum(1 for r in union if '采集降级' in ((r.get('code') or {}).get('evidence') or ''))}")
    print(f"\nD-10 投影：{agree_all}/{len(site)} = {agree_all/len(site):.1%}"
          f" → {new_agree}/{len(site)} = {new_agree/len(site):.1%}（阈值 90%）")
    print("\n新分支分布（直访命中行）:", dict(Counter(
        d[3] for d in detail if d[3] not in ("(未直访)",)).most_common()))
    print("\n逐行明细（golden_id | 金标 | 旧分支 => 新分支）:")
    for gid, gold, old, new, note in detail:
        if new == "(未直访)":
            print(f"     {gid}  {gold} : {old} => （未直访）")
            continue
        mark = "✅" if old != new and is_equivalent(gold, new) and not is_equivalent(gold, old) else ("⚠" if old != new and not is_equivalent(gold, new) and is_equivalent(gold, old) else "  ")
        print(f"  {mark} {gid}  {gold} : {old} => {new} {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
