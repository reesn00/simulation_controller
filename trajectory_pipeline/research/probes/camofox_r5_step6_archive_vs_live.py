"""停点第 ⑥ 步「人判存量那 4 条 agent 负样本」的**存档 vs 现场**对读证据生成器。

**做什么**：把 4 条 agent 负样本的**存档正文**（当场，agent 跑那批时采到的
`body_text`）与**今天的现场重采正文**并排放在一起，各自跑一遍仓库自己的
判据 :func:`trajectory_pipeline.perception.rule_perceptor.paywall_evidence`，
输出成一份可入库的 `.json`。

**为什么要有它**：台账 §M-37 详解记着「iqiyi 那条**当场**就有教科书级门禁句
『试看 05:44 后结束，观看完整视频请 开通VIP会员』，但仓库自己的
`paywall_evidence` 对那段正文**也**判不出来（DOM 换行把句子切开了）」——
这条读数**只存在于存档里**（存档目录不进库），所以要把正文原样落一份证据。

**输入**（都在本机，不联网）：
- `trajectory_pipeline/output/pipeline/samples/<archive>__site-N.json` —— 存档正文
- `trajectory_pipeline/research/probes/camofox_r5_step6_live_check.json` —— 现场重采

**输出**：`trajectory_pipeline/research/probes/camofox_r5_step6_archive_vs_live.json`

⚠️ **必须在仓库根跑**（路径按仓库根算）。
⚠️ **不含凭据**：这里只有页面 `inner_text` 与判据读数。
"""
import json
import pathlib

from trajectory_pipeline.perception.rule_perceptor import paywall_evidence

ROOT = pathlib.Path(__file__).resolve().parents[2]  # ← trajectory_pipeline/
OUT = ROOT / "output" / "pipeline"
PROBES = ROOT / "research" / "probes"

#: (存档文件, 站点, 记号)
FILES = [
    ("samples/E022__01fcb42b__site-8.json", "bilibili", "BV1ibptz2Enz"),
    ("samples/E022__01fcb42b__site-9.json", "v.qq", "q3242ihse1h"),
    ("samples/T054__1c9b6260__site-4.json", "iqiyi", "v_19rrk40at8"),
    ("samples/T054__1c9b6260__site-5.json", "iqiyi", "v_19rrk40at8"),
    ("samples/T054__1c9b6260__site-8.json", "youku", "XMTQwNzMzMTc1Mg"),
]


def walk(obj, acc):
    """收集所有嵌套 dict 里的 ``body_text`` / ``page_title``（存档的观测层）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("body_text", "page_title") and isinstance(v, str):
                acc.setdefault(k, []).append(v)
            walk(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            walk(v, acc)


def main():
    live_path = PROBES / "camofox_r5_step6_live_check.json"
    if not live_path.exists():
        print(f"缺现场证据：{live_path}")
        return
    live = json.loads(live_path.read_text(encoding="utf-8"))
    live_by = {r["url"]: r for r in live.get("results", [])}

    records = []
    for f, site, tag in FILES:
        p = OUT / f
        if not p.exists():
            print(f"跳过（存档不在本机）：{f}")
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        acc = {}
        walk(d, acc)
        o = d.get("outcome") or {}
        url = o.get("url") or ""
        lv = live_by.get(url) or {}
        settled = lv.get("settled") or {}

        bodies = []
        titles = acc.get("page_title", [])
        for i, b in enumerate(acc.get("body_text", [])):
            if not b.strip():
                continue
            bodies.append({
                "index": i,
                "page_title": titles[i] if i < len(titles) else "",
                "body_len": len(b),
                "paywall_evidence": paywall_evidence(b),
                "body_text": b,
            })

        records.append({
            "site": site,
            "tag": tag,
            "archive_file": f,
            "outcome": {
                "url": url,
                "branch": o.get("branch"),
                "source": o.get("source"),
                "fallback_used": o.get("fallback_used"),
                "question": o.get("question"),
                "reached_play_page": o.get("reached_play_page"),
                "play_page_url": o.get("play_page_url"),
            },
            "archive_bodies": bodies,
            "live_2026_10_27": {
                "url": lv.get("url"),
                "error": lv.get("error"),
                "page_title": settled.get("page_title"),
                "body_len": settled.get("body_len"),
                "paywall_evidence": paywall_evidence(settled.get("body_full") or ""),
                "body_text": settled.get("body_full") or "",
            },
        })

    doc = {
        "what": "停点第 ⑥ 步「人判存量那 4 条 agent 负样本」的存档 vs 现场对读证据",
        "when": "2026-10-27（现场重采：camofox 执行层）",
        "how": "research/probes/m30_live_check.py（现场） + 存档 samples/*.json 的 body_text（当场）；"
               "判据一律用仓库自己的 trajectory_pipeline.perception.rule_perceptor.paywall_evidence",
        "note": "无凭据、无 Cookie、无 Authorization：这里只有页面 inner_text 与判据读数",
        "records": records,
    }

    dst = PROBES / "camofox_r5_step6_archive_vs_live.json"
    dst.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print("written:", dst, dst.stat().st_size, "bytes")
    for r in records:
        print(f"  {r['site']:9s} {r['tag']:16s} archive bodies={len(r['archive_bodies'])} "
              f"live_ev={r['live_2026_10_27']['paywall_evidence']!r}")
        for b in r["archive_bodies"]:
            print(f"      archive body#{b['index']} len={b['body_len']:5d} ev={b['paywall_evidence']!r}")


if __name__ == "__main__":
    main()
