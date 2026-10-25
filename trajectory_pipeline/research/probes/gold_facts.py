"""**软维度金标的事实底稿**（2026-10-24）。

**它是什么**：把 91 条校准样本的判读所需事实**从存档里重算一遍**，落成
一份 JSON（`output/pipeline/calibration/v8/_gold_facts.json`）＋一段可读 dump：

- `record`：工作表里的只读上下文（`unit_url` / `site_url` / `play_page_url` /
  `reached_play_page` / `archive`）；
- `outcome`：**与本样本对应的那条** `outcomes[]`（分支 / 证据原文 / 问答点）；
  ⚠️ 同一个 `url` 可能在存档里有**两条** outcome（同一单元被访问两次、
  重定向到不同落地页），这时按 `site_url` 再对一次；
- `media`：该单元最后一步观察的媒体信号（`video_tag_count` / `iframe_count` /
  元素与链接的保留比 / 截断 / 降级）；
- `title` / `title_in_obs`：存档顶层 `title`（D-6 的片名）与其**能否在观察里
  检索到**（D-6 的「值与观察一致」）；
- `d2`：D-2 的机械事实（`gold_d2_judge.judge_unit`）；
- `stale_pred`：`pred_branch`（快照）≠ 存档分支 ⇒ 「底料已 relabel」。

**为什么要有它**：判读表写在填表脚本里，而工作表本身在 `output/` 下
（gitignored）——不把事实底稿固定下来，改判一次就再也查不出「当初看的是
哪句原文」。**这份底稿就是判读的凭据。**

**用法**（必须在仓库根跑）::

    uv run python trajectory_pipeline/research/probes/gold_facts.py          # 写 JSON + 打 dump
    uv run python trajectory_pipeline/research/probes/gold_facts.py --dump T040__c9982a1c__5c2c73cf
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gold_d2_judge import _arch_of, _sample_file, judge_unit  # noqa: E402

WORKSHEET = os.path.join(
    REPO, "trajectory_pipeline", "output", "pipeline", "calibration", "v8", "gold_worksheet.jsonl"
)
OUT_JSON = os.path.join(
    REPO, "trajectory_pipeline", "output", "pipeline", "calibration", "v8", "_gold_facts.json"
)


def _load_rows() -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with open(WORKSHEET, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            rows.setdefault(row["golden_id"], {})[row["dimension"]] = row
    return rows


def _observations(arch: dict, unit: str) -> list[dict]:
    if unit == "search":
        return [arch.get("search_observation") or {}]
    m = re.match(r"site-(\d+)$", unit or "")
    if not m:
        return []
    idx = int(m.group(1)) - 1
    visits = list(arch.get("visits") or ())
    if idx >= len(visits):
        return []
    return [s.get("observation") or {} for s in (visits[idx].get("steps") or ())]


def _pick_outcome(arch: dict, record: dict) -> tuple[dict, list[dict]]:
    """挑出与本样本对应的那条 outcome（同 url 的多条按 site_url 再对一次）。"""
    unit_url = record.get("unit_url") or ""
    site_url = record.get("site_url") or ""
    same = [o for o in (arch.get("outcomes") or ()) if (o.get("url") or "") == unit_url]
    if not same:
        return {}, same
    exact = [o for o in same if (o.get("site_url") or "") == site_url]
    if exact:
        return exact[0], same
    return same[0], same


def _title_in_obs(arch: dict, unit: str, title: str) -> bool:
    needle = (title or "").strip("《》〈〉\"' ")
    if not needle:
        return False
    hay = "\n".join(
        str(o.get("body_text") or "") + "\n" + str(o.get("page_title") or "")
        for o in _observations(arch, unit)
        if isinstance(o, dict)
    )
    return needle in hay


def _media(obs: dict) -> dict:
    els = list(obs.get("interactive_elements") or ())
    lks = list(obs.get("links") or ())
    return {
        "video_tag_count": obs.get("video_tag_count"),
        "iframe_count": obs.get("iframe_count"),
        "elements_kept": len(els),
        "elements_total": obs.get("elements_total"),
        "links_kept": len(lks),
        "links_total": obs.get("links_total"),
        "truncated": obs.get("truncated"),
        "degraded": obs.get("degraded"),
        "page_title": str(obs.get("page_title") or ""),
    }


def build() -> dict:
    out: dict[str, dict] = {}
    for gid, by_dim in sorted(_load_rows().items()):
        record = by_dim["d5"]["record"]
        arch = _arch_of(record) or {}
        found = _sample_file(record)
        unit = found[0] if found else ""
        obss = _observations(arch, unit)
        outcome, same = _pick_outcome(arch, record)
        title = str(arch.get("title") or "")
        pred_branch = str(by_dim["d5"].get("pred_branch") or "")
        arch_branch = str(outcome.get("branch") or "")
        out[gid] = {
            "unit": unit,
            "title": title,
            "user_prompt": str(arch.get("user_prompt") or ""),
            "query": str(arch.get("query") or ""),
            "record": {
                "unit_url": record.get("unit_url"),
                "site_url": record.get("site_url"),
                "play_page_url": record.get("play_page_url"),
                "reached_play_page": record.get("reached_play_page"),
            },
            "outcome": {
                "branch": outcome.get("branch"),
                "evidence": outcome.get("evidence") or "",
                "play_page_url": outcome.get("play_page_url") or "",
                "reached_play_page": outcome.get("reached_play_page"),
                "question": outcome.get("question") or "",
                "source": outcome.get("source") or "",
                "is_negative_sample": outcome.get("is_negative_sample"),
                "fallback_used": outcome.get("fallback_used"),
            },
            "outcome_ambiguous": len(same) > 1,
            "outcome_candidates": [
                {"branch": o.get("branch"), "site_url": o.get("site_url")} for o in same
            ],
            "media_last": _media(obss[-1] if obss else {}),
            "media_first": _media(obss[0] if obss else {}),
            "title_in_obs": _title_in_obs(arch, unit, title),
            "d2": judge_unit(arch, unit) if arch else [],
            "gold_label": by_dim["d5"].get("gold_label"),
            "pred_branch": pred_branch,
            "stale_pred": pred_branch != arch_branch,
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="软维度金标的事实底稿")
    ap.add_argument("--dump", default="", help="只打印某个 golden_id 的事实")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass

    facts = build()
    if args.dump:
        for gid in facts:
            if args.dump in gid:
                print(json.dumps({gid: facts[gid]}, ensure_ascii=False, indent=1))
        return 0

    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(facts, fh, ensure_ascii=False, indent=1)
    stale = [g for g, f in facts.items() if f["stale_pred"]]
    amb = [g for g, f in facts.items() if f["outcome_ambiguous"]]
    branches: dict[str, int] = {}
    for f in facts.values():
        b = str(f["outcome"]["branch"])
        branches[b] = branches.get(b, 0) + 1
    print("→ %s（%d 条）" % (OUT_JSON, len(facts)))
    print("存档分支分布：%s" % dict(sorted(branches.items(), key=lambda kv: -kv[1])))
    print("底料已 relabel（pred_branch ≠ 存档 branch）：%d 条 %s" % (len(stale), stale))
    print("同 url 多条 outcome（按 site_url 对齐）：%d 条 %s" % (len(amb), amb))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
