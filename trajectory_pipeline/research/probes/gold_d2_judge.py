"""**D-2（观察-动作一致性）金标判读的机械部分**。

**为什么要有这个脚本**（2026-10-24，软维度金标填表）

D-2 的判据是「动作引用的实体在前文观察存档中真实存在」。这里有三处
**口径分歧**，脚本把两边都摆出来，判档由人做（`goldwork` 手册 §7.2）：

1. **观察集合**：机器评分器（`evaluation/scorers/code_scorer.py`）看的是
   **P2 样本的 observations**＝该决策单元 visit 的 ``steps[].observation``
   （逐步对位，含 80 元素 + 全文 + links）。**不是** P3 训练视图里的
   ``six.observations``——那份是给模型看的投影（40 元素 / 2000 字截断）。
2. **检索面**：机器只查 ``interactive_elements``（元素列表）；rubric 说的是
   「在观察存档中检索」，存档里还有 ``body_text`` / ``page_title`` /
   ``links[].text``。元素在正文里出现（DOM 把多行可见文字拼成一行、
   或元素没进元素列表但文字在页面上）**同样算「真实存在」**。
3. **查不到 ≠ 引用不存在**：观察被裁剪/降级/截断时查不到，按 rubric v1.1
   边界判「不可判定（存档裁剪）」——**不扣分，也不给 5**。

**用法**（必须在仓库根跑）::

    uv run python trajectory_pipeline/research/probes/gold_d2_judge.py
    uv run python trajectory_pipeline/research/probes/gold_d2_judge.py --only T001
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, REPO)

from trajectory_pipeline.common.elements import element_name, element_role  # noqa: E402

WORKSHEET = os.path.join(
    REPO, "trajectory_pipeline", "output", "pipeline", "calibration", "v8", "gold_worksheet.jsonl"
)
SAMPLES_DIR = os.path.join(REPO, "trajectory_pipeline", "output", "pipeline", "samples")
#: 存档侧对 label 的截断长度（与 ``code_scorer.LABEL_TRUNCATED_AT`` 同源）。
LABEL_TRUNCATED_AT = 120


def _load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _sample_file(record: dict) -> tuple[str, dict] | None:
    """P3 训练样本文件（含 six 视图）+ 文件名里的 unit 段。"""
    prefix = "%s__%s__" % (record.get("task_id"), record.get("run_id"))
    unit_url = record.get("unit_url") or ""
    for name in sorted(os.listdir(SAMPLES_DIR)):
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        with open(os.path.join(SAMPLES_DIR, name), encoding="utf-8") as fh:
            sample = json.load(fh)
        if (sample.get("outcome") or {}).get("url") == unit_url:
            return name[: -len(".json")].split("__")[-1], sample
    return None


def _arch_of(record: dict) -> dict | None:
    path = record.get("archive") or ""
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(REPO, path)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _truncated(value) -> bool:
    if isinstance(value, dict):
        return any(bool(v) for v in value.values())
    return bool(value)


def _shortfall(obs: dict) -> str:
    """这份观察**能不能支撑**「某元素不存在」的结论（空串 = 能）。"""
    if not isinstance(obs, dict) or not obs:
        return "观察缺失"
    if obs.get("degraded"):
        return "降级 %s" % list(obs["degraded"])
    if _truncated(obs.get("truncated")):
        return "截断 %s" % obs.get("truncated")
    els = list(obs.get("interactive_elements") or ())
    lks = list(obs.get("links") or ())
    if int(obs.get("elements_total") or 0) > len(els):
        return "元素被裁剪（留 %d / 采到 %d）" % (len(els), obs.get("elements_total"))
    if int(obs.get("links_total") or 0) > len(lks):
        return "链接被裁剪（留 %d / 采到 %d）" % (len(lks), obs.get("links_total"))
    if not els and not lks and not str(obs.get("body_text") or "").strip():
        return "观察三项全空"
    return ""


def _hay(obs: dict) -> str:
    """观察的**全文检索面**：正文 + 标题 + 元素标签 + 链接文字。"""
    if not isinstance(obs, dict):
        return ""
    parts = [str(obs.get("body_text") or ""), str(obs.get("page_title") or "")]
    parts += [element_name(el) or "" for el in (obs.get("interactive_elements") or ())]
    parts += [str(lk.get("text") or "") for lk in (obs.get("links") or ())]
    return "\n".join(parts)


def _same_refs(obs: dict, needle: str) -> list[str]:
    return [
        str(el.get("ref") or "")
        for el in (obs.get("interactive_elements") or ())
        if (element_name(el) or "")[:LABEL_TRUNCATED_AT] == needle
    ]


def judge_unit(arch: dict, unit: str) -> list[dict]:
    """该决策单元的 click 引用逐条判定（只给事实，不给分数）。"""
    if unit == "search":
        obss = [arch.get("search_observation") or {}]
        steps = list(arch.get("steps") or [])
    else:
        m = re.match(r"site-(\d+)$", unit)
        if not m:
            return []
        idx = int(m.group(1)) - 1
        visits = list(arch.get("visits") or ())
        if idx >= len(visits):
            return []
        visit = visits[idx]
        steps = list(visit.get("steps") or [])
        obss = [s.get("observation") or {} for s in steps]
    out: list[dict] = []
    for step in steps:
        action = step.get("action") or {}
        if action.get("tool") != "click":
            continue
        if action.get("origin") not in (None, "model"):
            continue
        target = action.get("target") or {}
        label = element_name(target) or ""
        role = element_role(target) or ""
        if not label:
            out.append({"label": "", "kind": "no_target", "note": "执行器没解出目标（target=None）"})
            continue
        needle = label[:LABEL_TRUNCATED_AT]
        hits = [o for o in obss if isinstance(o, dict) and needle in _hay(o)]
        refs = sorted({r for o in hits for r in _same_refs(o, needle)})
        blocking = sorted({s for o in obss for s in [_shortfall(o)] if s})
        if hits:
            kind = "ambiguous" if len(refs) >= 2 else "found"
            note = "命中 %d 份观察；同名元素 ref %d 个%s" % (
                len(hits), len(refs), ("（%s）" % ",".join(refs)) if refs else "（元素列表里没有，只在正文/链接里）")
        elif not blocking:
            kind = "missing"
            note = "本单元的观察均完整（无降级/截断/裁剪），仍查不到"
        else:
            kind = "undetermined"
            note = "查不到且观察有短板：" + "；".join(blocking)
        out.append({"label": label, "kind": kind, "note": note})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="D-2 金标判读的机械部分（只给事实，不判分）")
    ap.add_argument("--only", default="", help="只处理 golden_id 含该串的样本")
    args = ap.parse_args(argv)

    rows = _load(WORKSHEET)
    records: dict[str, dict] = {}
    for row in rows:
        records.setdefault(row["golden_id"], {})[row["dimension"]] = row

    for gid in sorted(records):
        if args.only and args.only not in gid:
            continue
        record = records[gid]["d5"]["record"]
        found = _sample_file(record)
        arch = _arch_of(record) or {}
        unit = found[0] if found else "?"
        items = judge_unit(arch, unit) if arch else []
        print("--- %s  unit=%s" % (gid, unit))
        if not items:
            print("    无模型可见的 click 引用（只有 goto/new_tab）⇒ 本维在本样本内无适用项")
        for item in items:
            if item["kind"] == "no_target":
                print("    引用: 目标为空 ⇒ 存档完整性问题（不扣分）")
            else:
                print("    引用「%s」→ %s（%s）" % (item["label"][:44], item["kind"], item["note"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
