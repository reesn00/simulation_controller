"""**软维度金标判读的现场查看器**（2026-10-24）。

**它是什么**：把某条校准样本的**存档现场**摊开成一段可读的文本——
存档顶层 `title` / `user_prompt` / `query`、该决策单元（`site-N` / `search`）
的**每一步动作与观察**（页面标题、正文、元素清单摘要）、以及该 URL 的
`outcomes[]` 原文（分支 / 证据 / 播放页 URL / 是否到达播放页）。

**为什么要有这个脚本**：D-4/D-5/D-6 的判读要「看现场」——判「组件在不在」
要看正文原话与媒体信号，判「分支该不该落」要看证据字符串与前置事实。
工作表里的 `scene` 是**摘要**，不足以核这些；把现场摊平成文本，
判读才可复算、可复查（工作表本身在 `output/` 下，gitignored）。

**用法**（必须在仓库根跑）::

    uv run python trajectory_pipeline/research/probes/gold_peek.py T040__c9982a1c__5c2c73cf
    uv run python trajectory_pipeline/research/probes/gold_peek.py --body 1200 T024__b54092ee__cac9ea9f
    uv run python trajectory_pipeline/research/probes/gold_peek.py --all-v      # 只列有分支的单元
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
from trajectory_pipeline.common.elements import element_name  # noqa: E402

WORKSHEET = os.path.join(
    REPO, "trajectory_pipeline", "output", "pipeline", "calibration", "v8", "gold_worksheet.jsonl"
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


def _obs_lines(obs: dict) -> list[str]:
    if not isinstance(obs, dict) or not obs:
        return ["      （无观察）"]
    out = []
    title = str(obs.get("page_title") or "")
    body = str(obs.get("body_text") or "")
    media = "vid=%s ifr=%s el=%s/%s lk=%s/%s tr=%s deg=%s" % (
        obs.get("video_tag_count"), obs.get("iframe_count"),
        len(obs.get("interactive_elements") or ()), obs.get("elements_total"),
        len(obs.get("links") or ()), obs.get("links_total"),
        obs.get("truncated"), obs.get("degraded"),
    )
    out.append("      页面标题：%s" % (title or "(空)"))
    out.append("      媒体信号：%s" % media)
    if body:
        out.append("      正文：%s" % body.replace("\n", " / ")[:900])
    els = [element_name(el) or "" for el in (obs.get("interactive_elements") or ())]
    if els:
        out.append("      元素(%d)：%s" % (len(els), " | ".join(e[:26] for e in els[:40])))
    return out


def _peek(gid: str, body_len: int) -> str:
    rows = _load_rows()
    if gid not in rows:
        return "!! 工作表里没有 %s" % gid
    record = rows[gid]["d5"]["record"]
    arch = _arch_of(record) or {}
    found = _sample_file(record)
    unit = found[0] if found else "?"
    lines = ["=== %s  unit=%s" % (gid, unit)]
    lines.append("  目标片名(存档顶层 title)=%r" % arch.get("title"))
    lines.append("  user_prompt=%r" % (arch.get("user_prompt") or "")[:120])
    lines.append("  query=%r" % (arch.get("query") or ""))
    for o in arch.get("outcomes") or ():
        if (o.get("url") or "") != (record.get("unit_url") or ""):
            continue
        lines.append("  ── outcome ──")
        lines.append("    branch=%r  reached_play_page=%r  fallback_used=%r" % (
            o.get("branch"), o.get("reached_play_page"), o.get("fallback_used")))
        lines.append("    site_url=%r" % (o.get("site_url") or ""))
        lines.append("    play_page_url=%r" % (o.get("play_page_url") or ""))
        lines.append("    evidence=%s" % (o.get("evidence") or ""))
        lines.append("    question=%r  source=%r  is_negative_sample=%r" % (
            o.get("question"), o.get("source"), o.get("is_negative_sample")))
    lines.append("  ── 单元现场 ──")
    if unit == "search":
        steps = list(arch.get("steps") or ())
        obss = [arch.get("search_observation") or {}]
    else:
        m = re.match(r"site-(\d+)$", unit or "")
        if not m:
            return "\n".join(lines + ["  （unit 解析不出来）"])
        idx = int(m.group(1)) - 1
        visits = list(arch.get("visits") or ())
        if idx >= len(visits):
            return "\n".join(lines + ["  （该 visit 不存在）"])
        steps = list((visits[idx].get("steps") or ()))
        obss = [s.get("observation") or {} for s in steps]
    for i, step in enumerate(steps):
        act = step.get("action") or {}
        tgt = act.get("target") or {}
        lines.append("    [%d] %s %s" % (
            i, act.get("tool"),
            (element_name(tgt) or json.dumps(tgt, ensure_ascii=False))[:90] if tgt else ""))
        for l in _obs_lines(obss[i] if i < len(obss) else {}):
            lines.append(l)
    for it in judge_unit(arch, unit):
        lines.append("    · D-2 事实：引用「%s」→ %s（%s）" % (
            (it.get("label") or "(空)")[:44], it.get("kind"), it.get("note")))
    if body_len and body_len != 900:
        lines.append("  （正文已按 --body 长度截断）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="看一条样本的存档现场")
    ap.add_argument("gids", nargs="*", help="golden_id（可多个；支持前缀匹配）")
    ap.add_argument("--body", type=int, default=900, help="正文截断长度")
    ap.add_argument("--all-v", action="store_true", help="列出工作表里全部 golden_id")
    args = ap.parse_args(argv)
    try:  # Windows 控制台默认 GBK，正文里有生僻字会炸
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass
    if args.all_v:
        for gid in sorted(_load_rows()):
            print(gid)
        return 0
    if not args.gids:
        ap.error("给至少一个 golden_id")
    rows = _load_rows()
    for gid in args.gids:
        hits = [g for g in rows if g == gid or gid in g]
        if not hits:
            print("!! 没匹配到 %s" % gid)
            continue
        for h in hits:
            print(_peek(h, args.body))
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
