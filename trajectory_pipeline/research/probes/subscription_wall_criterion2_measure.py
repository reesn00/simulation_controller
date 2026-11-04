"""**Q-20 ② 的续集**：给 ``subscription_wall`` 补第二条可执行判据。

**做什么**：把候选的「判据二 · 订阅价目门禁」在**盘上全部存档正文**上跑一遍，
量四个数：

1. **找回**——原 ``subscription_wall`` 的存档行里，有多少行在
   :func:`paywall_evidence` 下是空串、但在判据二下变成命中
   （这正是第 2 项改动的副作用：真付费墙落 ``unresolved`` ⇒ 不进池）；
2. **误伤**——**非** ``subscription_wall`` 的存档行里，有多少行被判据二
   新命中（这是把假阳放进池里的代价）；
3. **验收样例读数**——owner 圈定的样例逐条读数；
4. **登录门排除**——同一页同时写了「登录后继续观看」时，判据二必须**不成立**
   （那是 ``login_required``，不是 ``subscription_wall``）。

**为什么不靠论证**：台账 §M-37 详解明写「**放宽幅度只能靠实测定，不能靠论证定**」。
判据二比判据一宽得多（页面级三要素同现），它的假阳面只能靠跑盘上
2339 条存档行来量。

⚠️ **判据口径与生产同源**：判据一 / 判据二 / 登录门短语**全部从**
``trajectory_pipeline.perception.rule_perceptor`` **import**，本脚本不另抄
一份词表——抄一份就等于量的是另一套口径。

**输入**（都在本机，不联网）：
- ``output/pipeline/*.json`` —— 生产根存档（``outcomes`` + ``visits``）
- ``output/pipeline/_backup_pre_m58/samples/*.json`` —— 逐站存档正文
- ``research/probes/camofox_r5_step6_live_check.json`` —— 现场重采正文

**输出**：``research/probes/subscription_wall_criterion2_measure.json``

⚠️ **必须在仓库根跑**（路径按仓库根算）。
⚠️ **不含凭据**：这里只有页面 ``inner_text`` 与判据读数。
"""
from __future__ import annotations

import ast
import collections
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]  # ← trajectory_pipeline/
sys.path.insert(0, str(ROOT.parent))

from trajectory_pipeline.perception.rule_perceptor import (  # noqa: E402
    LOGIN_GATE_PHRASES,
    PAYWALL_BENEFIT_WORDS,
    PAYWALL_PRICE_ACTIVATION_PHRASES,
    login_gate_phrase,
    paywall_evidence,
    paywall_price_evidence,
)

PIPELINE = ROOT / "output" / "pipeline"
ARCHIVES = PIPELINE
SAMPLES = PIPELINE / "_backup_pre_m58" / "samples"
LIVE = ROOT / "research" / "probes" / "camofox_r5_step6_live_check.json"
GOLD = ROOT / "evaluation" / "golden" / "data" / "candidates.jsonl"
OUT = ROOT / "research" / "probes" / "subscription_wall_criterion2_measure.json"

# ── 判据二 · 口径全部来自生产实现 ──────────────────────────────────────
#
# 判据一（:func:`paywall_evidence`）要求「CTA 与观看语境**同句**」。
# 判据二放宽到**页面级三要素同现**：
#   ① 订阅价目（:data:`PRICE_PATTERN`）
#   ② 开通动作（:data:`PAYWALL_PRICE_ACTIVATION_PHRASES`）
#   ③ 权益承诺（:data:`PAYWALL_BENEFIT_WORDS`）
# 再加一条**登录门排除**：页面明写「登录后继续观看」⇒ 那是登录墙。
#
# ⚠️ 本脚本**不另抄一份词表**：判据一 / 判据二 / 登录门短语都从
# ``perception.rule_perceptor`` import——抄一份就等于量的是另一套口径，
# 实测结论会与生产判据对不上。


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url or "").split("/")[0]


def _bodies_of_archive(d: dict) -> list[tuple[str, str]]:
    """存档里所有 ``(落地域名, 正文)``。"""
    out: list[tuple[str, str]] = []
    for v in (d.get("visits") or []):
        if not isinstance(v, dict):
            continue
        url = v.get("landed_url") or v.get("url") or ""
        for s in (v.get("steps") or []):
            ob = (s.get("observation") or {}) if isinstance(s, dict) else {}
            bt = ob.get("body_text")
            if isinstance(bt, str) and bt.strip():
                out.append((url, bt))
    return out


def _collect_sample_bodies(p: pathlib.Path) -> list[str]:
    d = json.loads(p.read_text(encoding="utf-8"))
    acc: list[str] = []

    def walk(o: object) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "body_text" and isinstance(v, str) and v.strip():
                    acc.append(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(d)
    return acc


def main() -> None:
    # ── 1. 存档行全集（生产根） ──
    rows: list[dict[str, object]] = []
    for p in sorted(ARCHIVES.glob("*.json")):
        if p.name.startswith("_"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(d.get("outcomes"), list):
            continue
        bodies = _bodies_of_archive(d)
        by_url: dict[str, list[str]] = {}
        for url, bt in bodies:
            by_url.setdefault(url, []).append(bt)
        for o in d["outcomes"]:
            if not isinstance(o, dict):
                continue
            url = str(o.get("url") or "")
            rows.append({
                "file": p.name,
                "url": url,
                "host": _host(url),
                "branch": o.get("branch"),
                "evidence": str(o.get("evidence") or "")[:120],
                "bodies": by_url.get(url, []),
                "all_bodies": [bt for _u, bt in bodies],
            })
    print(f"存档行：{len(rows)}（来自 {len({r['file'] for r in rows})} 个存档文件）")

    # ── 2. 判据一 vs 判据二 ──
    stats: dict[str, dict[str, int]] = {}
    recovered: list[dict[str, object]] = []      # 判据二**独有点**（判据一空串）
    false_pos: list[dict[str, object]] = []      # 判据二命中但原分支不是订阅墙
    login_blocked: list[dict[str, object]] = []   # 三要素齐但被登录门排除挡下
    for r in rows:
        bodies: list[str] = r["bodies"] or r["all_bodies"]  # type: ignore[assignment]
        best1 = best2 = best2_raw = ""
        login_phrase = ""
        for b in bodies:
            e1 = paywall_evidence(b)
            e2 = paywall_price_evidence(b)
            e2raw = paywall_price_evidence(b, exclude_login_gate=False)
            best1 = best1 or e1
            best2 = best2 or e2
            best2_raw = best2_raw or e2raw
            login_phrase = login_phrase or login_gate_phrase(b)
        if best2 and not best1:
            recovered.append({
                "file": r["file"], "url": r["url"], "host": r["host"],
                "branch": r["branch"], "evidence": r["evidence"],
                "判据二": best2,
            })
        if best2_raw and not best2:
            login_blocked.append({
                "file": r["file"], "url": r["url"], "host": r["host"],
                "branch": r["branch"], "evidence": r["evidence"],
                "判据二(无登录排除)": best2_raw,
                "登录门": login_phrase,
            })
        if best2 and r["branch"] != "subscription_wall":
            false_pos.append({
                "file": r["file"], "url": r["url"], "host": r["host"],
                "branch": r["branch"], "判据二": best2,
            })
        key = str(r["branch"])
        stats.setdefault(key, {"行数": 0, "判据一命中": 0, "判据二命中": 0})
        s = stats[key]
        s["行数"] += 1
        s["判据一命中"] += 1 if best1 else 0
        s["判据二命中"] += 1 if best2 else 0

    print("\n按分支（原存档分支 → 判据一/判据二命中数）：")
    for k in sorted(stats, key=lambda x: -stats[x]["行数"]):
        s = stats[k]
        print(f"  {k:22s} {s['行数']:5d} 行 | 判据一 {s['判据一命中']:4d} "
              f"| 判据二 {s['判据二命中']:4d}")

    sw = [r for r in rows if r["branch"] == "subscription_wall"]
    sw_miss = [r for r in sw
               if not any(paywall_evidence(b) for b in (r["bodies"] or r["all_bodies"]))]
    print(f"\n原 subscription_wall {len(sw)} 行；判据一下空串 {len(sw_miss)} 行")
    print("判据二找回（判据一空串 & 判据二命中 & 原分支是 subscription_wall）："
          f"{sum(1 for x in recovered if x['branch'] == 'subscription_wall')} 行")
    print("  找回按站点："
          f"{collections.Counter(str(x['host']) for x in recovered if x['branch'] == 'subscription_wall').most_common(12)}")
    print(f"判据二误伤（原分支不是 subscription_wall 却命中）：{len(false_pos)} 行")
    print(f"  误伤按分支：{collections.Counter(str(x['branch']) for x in false_pos).most_common()}")
    print(f"  误伤按站点：{collections.Counter(str(x['host']) for x in false_pos).most_common(12)}")
    print(f"登录门排除挡下（三要素齐、但页面是登录墙）：{len(login_blocked)} 行")
    print(f"  按站点：{collections.Counter(str(x['host']) for x in login_blocked).most_common(12)}")

    # ── 3. 验收样例（owner 圈定） ──
    accept = [
        {
            "site": "iqiyi",
            "file": "T054__1c9b6260__site-5.json",
            "want": "hit",
            "why": "教科书级门禁句「试看 05:44 后结束，观看完整视频请 开通VIP会员」"
                   "——判据一（现行）在这行 DOM 换行切句下读数是空串",
        },
        {
            "site": "youku",
            "file": "T054__1c9b6260__site-8.json",
            "want": "hit",
            "why": "第 2 项的副作用样本：真付费墙但正文只写「首3月每月12元/立即开通」"
                   "（没写「订阅后观看」那种措辞）⇒ 判据一读数空串",
        },
        {
            "site": "youku_live",
            "file": "live:results[3].settled",
            "want": "empty",
            "why": "同一页的**现场**正文多了「由于安全原因，请 登录 后继续观看」"
                   "⇒ 登录门，不是订阅墙 ⇒ 判据二必须被登录门排除挡下",
        },
        {
            "site": "v.qq",
            "file": "E022__01fcb42b__site-9.json",
            "want": "empty",
            "why": "正文只有广告位「VIP可关闭该广告」+营销「新客VIP专享特惠！/开通会员」，"
                   "**没有价目**；语境词「完整版」来自片名 ⇒ 判据二必须不成立",
        },
        {
            "site": "fun.tv",
            "file": "live:results[4].settled",
            "want": "hit",
            "why": "第 2 项的副作用样本之二：「黄金会员首月低至12元 / 新朋友，开通享精彩内容 / "
                   "开通会员」——有价目有开通有权益承诺，无登录门",
        },
        {
            "site": "bilibili",
            "file": "E022__01fcb42b__site-8.json",
            "want": "any",
            "why": "真登录墙；判据一对它本就空串（散文前置、无代码）",
        },
    ]
    live_data = json.loads(LIVE.read_text(encoding="utf-8"))
    live_bodies = {
        "live:results[3].settled": live_data["results"][3]["settled"]["body_full"],
        "live:results[4].settled": live_data["results"][4]["settled"]["body_full"],
    }
    accept_rows = []
    print("\n验收样例读数：")
    for spec in accept:
        if spec["file"].startswith("live:"):
            bodies = [live_bodies[spec["file"]]]
            src = spec["file"]
        else:
            p = SAMPLES / spec["file"]
            if not p.exists():
                accept_rows.append({**spec, "missing": True})
                continue
            bodies = _collect_sample_bodies(p)
            src = spec["file"]
        reads = {
            "判据一": sorted({paywall_evidence(b) for b in bodies if paywall_evidence(b)}),
            "判据二": sorted({paywall_price_evidence(b) for b in bodies
                              if paywall_price_evidence(b)}),
            "判据二(无登录排除)": sorted(
                {paywall_price_evidence(b, exclude_login_gate=False) for b in bodies
                 if paywall_price_evidence(b, exclude_login_gate=False)}),
            "登录门": sorted({login_gate_phrase(b) for b in bodies if login_gate_phrase(b)}),
        }
        ok = (reads["判据二"] != []) if spec["want"] == "hit" else (
            reads["判据二"] == [] if spec["want"] == "empty" else True)
        accept_rows.append({**spec, "reads": reads, "符合预期": ok})
        print(f"  {spec['site']:14s} want={spec['want']:6s} "
              f"判据一={reads['判据一'] or '（空串）'} | 判据二={reads['判据二'] or '（空串）'} "
              f"| 登录门={reads['登录门'] or '—'} | {'OK' if ok else '✗不符'}")

    # ── 4. 金标回归 ──
    gold_rows: list[dict[str, object]] = []
    if GOLD.exists():
        for line in GOLD.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            code = r.get("code")
            if isinstance(code, str):
                try:
                    code = ast.literal_eval(code)
                except (ValueError, SyntaxError):
                    code = {}
            branch = (code or {}).get("branch") or ""
            if branch == "subscription_wall":
                gold_rows.append({"golden_id": r.get("golden_id"),
                                  "url": r.get("url"), "branch": branch})
    url_index: dict[str, list[str]] = {}
    for r in rows:
        url_index.setdefault(str(r["url"]), []).append(str(r["file"]))
    gold_detail = []
    for g in gold_rows:
        url = str(g["url"])
        reads = {"判据一": [], "判据二": []}
        for fname in url_index.get(url, []):
            p = ARCHIVES / fname
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for _u, bt in _bodies_of_archive(d):
                e1 = paywall_evidence(bt)
                e2 = paywall_price_evidence(bt)
                if e1:
                    reads["判据一"].append(e1)
                if e2:
                    reads["判据二"].append(e2)
        gold_detail.append({**g, "reads": reads})
    lost = [g for g in gold_detail if g["reads"]["判据一"] and not g["reads"]["判据二"]]
    print(f"\n金标 subscription_wall 候选行：{len(gold_detail)}；"
          f"判据一命中 {sum(1 for g in gold_detail if g['reads']['判据一'])}；"
          f"判据二丢失 {len(lost)} 行")

    doc = {
        "what": "给 subscription_wall 补第二条可执行判据（判据二 · 订阅价目门禁）的实测",
        "when": "2026-11-04",
        "how": "research/probes/subscription_wall_criterion2_measure.py；"
               "判据一 / 判据二 / 登录门全部从 trajectory_pipeline.perception."
               "rule_perceptor import（口径与生产同源，不另抄一份）",
        "note": "无凭据、无 Cookie：这里只有页面 inner_text 与判据读数",
        "判据二词表": {
            "价目": "PRICE_PATTERN（复用）",
            "开通动作": list(PAYWALL_PRICE_ACTIVATION_PHRASES),
            "权益承诺": list(PAYWALL_BENEFIT_WORDS),
            "登录门排除": list(LOGIN_GATE_PHRASES),
        },
        "corpus": {
            "存档行": len(rows),
            "存档文件": len({r["file"] for r in rows}),
        },
        "by_branch": stats,
        "判据一空串的 subscription_wall 行": len(sw_miss),
        "找回": [x for x in recovered if x["branch"] == "subscription_wall"],
        "找回_其他分支": [x for x in recovered if x["branch"] != "subscription_wall"],
        "误伤": false_pos,
        "登录门排除挡下": login_blocked,
        "acceptance": accept_rows,
        "gold_regression": {
            "候选行数": len(gold_detail),
            "判据一命中行数": sum(1 for g in gold_detail if g["reads"]["判据一"]),
            "判据二丢失行数": len(lost),
            "rows": gold_detail,
        },
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written:", OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
