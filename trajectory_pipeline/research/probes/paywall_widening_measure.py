"""**Q-20 ② 的实测**：`paywall_evidence` 的「同句」放宽到多宽。

**做什么**：把三个候选放宽方案（不抄第二份词表，只改**切句/取语境**的粒度）
在**盘上全部存档正文**上跑一遍，量四个数：

1. **新增命中**（放宽引入的候选假阳）——有多少正文的判据结论从「空串」变成「命中」；
2. **丢失命中**（放宽误伤）——放宽必须**是超集**，原来命中的不许掉（=0）；
3. **验收样例读数**——iqiyi 那段当场正文必须**从空串变成命中**；
   v.qq / youku **必须仍然是空串**；
4. **金标回归**——金标候选集里判 `subscription_wall` / `login_required` 的行，
   它对应的正文在放宽后**不许掉命中**。

**为什么不靠论证**：台账 §M-37 详解明写「**放宽幅度只能靠实测定，不能靠论证定**」
——v.qq 的语境词「完整版」来自**片名**，离导航栏 CTA 只隔十几个换行，
窗口开多大就会假阳 ⇒ 这个数只有实跑才知道。

**输入**（都在本机，不联网）：
- `trajectory_pipeline/output/pipeline/samples/*.json` —— 存档正文（`body_text`）
- `trajectory_pipeline/evaluation/golden/data/candidates.jsonl` —— 金标候选集

**输出**：`trajectory_pipeline/research/probes/paywall_widening_measure.json`

⚠️ **必须在仓库根跑**（路径按仓库根算）。
⚠️ **不含凭据**：这里只有页面 `inner_text` 与判据读数。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]  # ← trajectory_pipeline/
sys.path.insert(0, str(ROOT.parent))

from trajectory_pipeline.perception.rule_perceptor import (  # noqa: E402
    PAYWALL_CONTEXT_WORDS,
    PAYWALL_CTA_PHRASES,
    PAYWALL_ERROR_CONTEXT_WORDS,
    PAYWALL_ERROR_GATE_PHRASES,
    PAYWALL_GATE_PHRASES,
    _PAYWALL_UNIT_SPLIT,
    _context_outside,
    _find_paywall_hit,
    paywall_evidence,
)

SAMPLES = ROOT / "output" / "pipeline" / "samples"
GOLD = ROOT / "evaluation" / "golden" / "data" / "candidates.jsonl"
OUT = ROOT / "research" / "probes" / "paywall_widening_measure.json"

#: **验收样例**（现场重采 + 存档当场正文都核过，见 camofox_r5_step6_archive_vs_live.json）
ACCEPT = {
    "iqiyi": {
        "file": "T054__1c9b6260__site-5.json",
        "want": "hit",
        "why": "当场正文有教科书级门禁句「试看 05:44 后结束，观看完整视频请 开通VIP会员」"
               "——放宽后必须从空串变成命中（否则接核会假降级真付费墙）",
    },
    "youku": {
        "file": "T054__1c9b6260__site-8.json",
        "want": "empty",
        "why": "现场正文只有营销（首3月12元/立即开通）+「由于安全原因，请 登录 后继续观看」"
               "——不是付费墙 ⇒ 放宽后必须仍是空串",
    },
    "v.qq": {
        "file": "E022__01fcb42b__site-9.json",
        "want": "empty",
        "why": "正文只有广告位「VIP可关闭该广告」+营销「新客VIP专享特惠！/开通会员」；"
               "语境词「完整版」来自**片名**「三傻大闹宝莱坞完整版」⇒ 放宽后必须仍是空串",
    },
    "bilibili": {
        "file": "E022__01fcb42b__site-8.json",
        "want": "any",
        "why": "真登录墙；`login_required` 只有散文前置、无代码 ⇒ 付费墙判据对它本就空串",
    },
}


# ── 三个候选放宽方案 ────────────────────────────────────────────────────
#
# 都**只改 L2 层的粒度**（怎么切句、语境词允许落在哪），**不动词表**
# （词表一改就是口径变更 ⇒ 按 D-C7 要连带改 prompt + 重跑 A/B）。
#
# ⚠️ **L1（门禁短语）与 L1.5（报错短语 + 会员电商信号）两层保持原样**——
# 它们与「同句」无关，动它们是另一件事（Q-20 ② 只问 L2 的「同句」放宽到多宽）。
# 基线 = 现行：句单元按 `[\n\r。！？；;!?]+` 切，CTA 与语境词必须**同单元**。

#: **W1 · 换行不算句界**（只在真正的句读处切）——DOM 节点边界不再打败「同句」。
_W1_SPLIT = re.compile(r"[。！？；;!?]+")


def _units(text: str, split: re.Pattern[str]) -> list[str]:
    return [u for u in split.split(text) if u]


def _l1_or_l15(text: str) -> tuple[str, str]:
    """L1（无条件门禁短语）与 L1.5（报错短语 + 会员电商信号）——**各变体共用**。"""
    low = text.lower()
    for phrase in PAYWALL_GATE_PHRASES:
        if phrase.lower() in low:
            return phrase, ""
    for phrase in PAYWALL_ERROR_GATE_PHRASES:
        if phrase.lower() in low:
            for ctx in PAYWALL_ERROR_CONTEXT_WORDS:
                if ctx.lower() in low:
                    return phrase, ctx
            break
    return "", ""


def _hit_with_split(text: str, split: re.Pattern[str]) -> tuple[str, str]:
    """按给定切句规则跑一遍 L2（CTA + 同单元语境词）。返回 (cta, context)。"""
    for unit in _units(text, split):
        ulow = unit.lower()
        for cta in PAYWALL_CTA_PHRASES:
            clow = cta.lower()
            pos = ulow.find(clow)
            while pos >= 0:
                ctx = _context_outside(ulow, pos, pos + len(clow))
                if ctx:
                    return cta, ctx
                pos = ulow.find(clow, pos + 1)
    return "", ""


def w1_hit(text: str) -> tuple[str, str]:
    """**W1**：换行不切句，只按句读切 ⇒ CTA 与语境词只要在同一个「句读块」里就行。"""
    hit = _l1_or_l15(text)
    if hit[0]:
        return hit
    return _hit_with_split(text, _W1_SPLIT)


def _w_window_hit(text: str, window: int) -> tuple[str, str]:
    """保留现行切句，但允许语境词落在 CTA 单元的**相邻 ±window 单元**内。"""
    hit = _l1_or_l15(text)
    if hit[0]:
        return hit
    units = _units(text, _PAYWALL_UNIT_SPLIT)
    ulows = [u.lower() for u in units]
    for i, ulow in enumerate(ulows):
        for cta in PAYWALL_CTA_PHRASES:
            clow = cta.lower()
            pos = ulow.find(clow)
            while pos >= 0:
                ctx = _context_outside(ulow, pos, pos + len(clow))
                if ctx:
                    return cta, ctx
                for j in range(max(0, i - window), min(len(ulows), i + window + 1)):
                    if j == i:
                        continue
                    for word in PAYWALL_CONTEXT_WORDS:
                        if word.lower() in ulows[j]:
                            return cta, word
                pos = ulow.find(clow, pos + 1)
    return "", ""


def w2_hit(text: str) -> tuple[str, str]:
    """**W2**：邻窗 **±1**——「DOM 换行把一句门禁命题切成 2 个短节点」这个具体形态。"""
    return _w_window_hit(text, 1)


def w3_hit(text: str) -> tuple[str, str]:
    """**W3**：邻窗 **±3**（看代价随窗口增大的曲线）。"""
    return _w_window_hit(text, 3)


VARIANTS = {
    "baseline": lambda t: (_find_paywall_hit(t),),
    "W1_换行不切句": w1_hit,
    "W2_邻窗±1": w2_hit,
    "W3_邻窗±3": w3_hit,
}


def _is_hit(res: object) -> bool:
    if isinstance(res, tuple) and len(res) == 1:
        return res[0] is not None
    cta, _ctx = res  # type: ignore[misc]
    return bool(cta)


def _describe(res: object) -> str:
    if isinstance(res, tuple) and len(res) == 1:
        hit = res[0]
        if hit is None:
            return ""
        return f"{hit.phrase}" + (f"+{hit.context}" if hit.context else "")
    cta, ctx = res  # type: ignore[misc]
    return f"{cta}+{ctx}" if cta else ""


def walk_bodies(obj: object, acc: list[str]) -> None:
    """收集所有嵌套 dict 里的 ``body_text``（非空）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "body_text" and isinstance(v, str) and v.strip():
                acc.append(v)
            walk_bodies(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            walk_bodies(v, acc)


def main() -> None:
    # ── 1. 存档正文全集 ──
    bodies: list[tuple[str, str]] = []  # (来源文件, 正文)
    for p in sorted(SAMPLES.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        acc: list[str] = []
        walk_bodies(d, acc)
        for b in acc:
            bodies.append((p.name, b))
    print(f"存档正文样本：{len(bodies)} 段（来自 {len({f for f, _ in bodies})} 个文件）")

    # ── 2. 全量重判 ──
    per_variant: dict[str, dict[str, int]] = {}
    detail: dict[str, list[dict[str, object]]] = {}
    for name, fn in VARIANTS.items():
        hit_files: set[str] = set()
        hit_count = 0
        rows: list[dict[str, object]] = []
        for fname, body in bodies:
            res = fn(body)
            if _is_hit(res):
                hit_count += 1
                hit_files.add(fname)
                rows.append({"file": fname, "hit": _describe(res)})
        per_variant[name] = {
            "命中段数": hit_count,
            "命中文件数": len(hit_files),
        }
        detail[name] = rows

    base_files = {r["file"] for r in detail["baseline"]}
    delta: dict[str, object] = {}
    for name in VARIANTS:
        if name == "baseline":
            continue
        cur_files = {r["file"] for r in detail[name]}
        gained = cur_files - base_files
        lost = base_files - cur_files
        delta[name] = {
            "新增命中文件数": len(gained),
            "丢失命中文件数": len(lost),
            "新增命中文件": sorted(gained)[:40],
            "丢失命中文件": sorted(lost)[:40],
        }
        print(f"  {name:14s} 命中 {per_variant[name]['命中段数']:4d} 段 / "
              f"{per_variant[name]['命中文件数']:3d} 文件 | "
              f"新增 {len(gained):3d} 丢失 {len(lost):3d}")

    # ── 3. 验收样例 ──
    accept_rows = []
    for site, spec in ACCEPT.items():
        p = SAMPLES / spec["file"]
        if not p.exists():
            accept_rows.append({"site": site, "file": spec["file"], "missing": True})
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        acc: list[str] = []
        walk_bodies(d, acc)
        reads = {}
        for name, fn in VARIANTS.items():
            hits = [_describe(fn(b)) for b in acc]
            hits = [h for h in hits if h]
            reads[name] = hits
        accept_rows.append({
            "site": site,
            "file": spec["file"],
            "want": spec["want"],
            "why": spec["why"],
            "reads": reads,
        })
        print(f"  验收 {site:9s} baseline={reads['baseline'] or '（空串）'!s:40s} "
              f"W1={reads['W1_换行不切句'] or '（空串）'!s:40s} "
              f"W2={reads['W2_邻窗±1'] or '（空串）'!s:40s} "
              f"W3={reads['W3_邻窗±3'] or '（空串）'!s}")

    # ── 4. 金标回归 ──
    gold_rows = []
    if GOLD.exists():
        for line in GOLD.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            code = r.get("code") or {}
            if isinstance(code, str):
                try:
                    code = eval(code, {"__builtins__": {}})  # noqa: S307 - 存档里是 repr
                except Exception:
                    code = {}
            branch = (code or {}).get("branch") or ""
            if branch in ("subscription_wall", "login_required"):
                gold_rows.append({
                    "golden_id": r.get("golden_id"),
                    "url": r.get("url"),
                    "branch": branch,
                    "gold_value": r.get("golden"),
                })
    print(f"金标候选集里判 subscription_wall/login_required 的行：{len(gold_rows)}")

    # 按 URL 找存档正文（子串匹配文件名不够 ⇒ 按存档里的 outcome.url 反查）
    url_to_bodies: dict[str, list[str]] = {}
    for fname, body in bodies:
        url_to_bodies.setdefault(fname, []).append(body)
    gold_by_url: dict[str, list[dict[str, object]]] = {}
    for g in gold_rows:
        gold_by_url.setdefault(str(g["url"]), []).append(g)

    gold_reads = []
    for fname, _b in bodies:
        pass
    # 反查：逐个存档文件读 outcome.url
    url_index: dict[str, list[str]] = {}
    for p in sorted(SAMPLES.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        acc: list[str] = []
        walk_bodies(d, acc)
        if not acc:
            continue
        outs = d.get("outcomes") if isinstance(d.get("outcomes"), list) else []
        o = d.get("outcome") or {}
        urls = []
        if isinstance(o, dict) and o.get("url"):
            urls.append(o["url"])
        for x in outs:
            if isinstance(x, dict) and x.get("url"):
                urls.append(x["url"])
        for u in urls:
            url_index.setdefault(u, []).append(p.name)

    gold_detail = []
    for g in gold_rows:
        url = str(g["url"])
        files = url_index.get(url, [])
        reads = {name: [] for name in VARIANTS}
        for fname in files:
            p = SAMPLES / fname
            d = json.loads(p.read_text(encoding="utf-8"))
            acc: list[str] = []
            walk_bodies(d, acc)
            for b in acc:
                for name, fn in VARIANTS.items():
                    h = _describe(fn(b))
                    if h:
                        reads[name].append(h)
        gold_detail.append({
            "golden_id": g["golden_id"],
            "url": url,
            "branch": g["branch"],
            "gold_value": g["gold_value"],
            "archive_files": files,
            "reads": reads,
        })
    lost_gold = [
        g for g in gold_detail
        if g["reads"]["baseline"] and not g["reads"]["W1_换行不切句"]
    ]
    print(f"金标回归：基线命中的行 {sum(1 for g in gold_detail if g['reads']['baseline'])} / "
          f"{len(gold_detail)}；W1 下丢失 {len(lost_gold)} 行")

    doc = {
        "what": "Q-20 ② 的实测：`paywall_evidence` 的「同句」放宽到多宽",
        "when": "2026-10-27",
        "how": "research/probes/paywall_widening_measure.py；"
               "只改**切句/取语境粒度**，不动词表；"
               "基线 = trajectory_pipeline.perception.rule_perceptor.paywall_evidence",
        "note": "无凭据、无 Cookie：这里只有页面 inner_text 与判据读数",
        "corpus": {
            "存档正文件数": len({f for f, _ in bodies}),
            "正文段数": len(bodies),
        },
        "variants": {
            "baseline": "现行：句单元按 [\\n\\r。！？；;!?]+ 切，CTA 与语境词须同单元",
            "W1_换行不切句": "只在句读处切（换行不算句界）⇒ DOM 节点边界不再打败「同句」",
            "W2_邻窗±1": "保留现行切句，语境词允许落在 CTA 单元的相邻单元（±1）",
            "W3_邻窗±3": "同上但窗口 ±3（看代价随窗口增大的曲线）",
        },
        "totals": per_variant,
        "delta_vs_baseline": delta,
        "acceptance": accept_rows,
        "gold_regression": {
            "金标候选行数": len(gold_detail),
            "基线命中行数": sum(1 for g in gold_detail if g["reads"]["baseline"]),
            "W1 下丢失行数": len(lost_gold),
            "rows": gold_detail,
        },
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written:", OUT, OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
