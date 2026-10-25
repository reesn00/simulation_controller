"""**软维度金标填表现场 dump**：把一格（或一组样本）判分要看的**全部事实**摆出来。

**为什么要有这个脚本**（2026-10-24，软维度金标填表）

填 D-2 / D-4 / D-5 / D-6 要同时看**三处**东西：工作表里的 rubric 锚点与
现场摘要、P1 存档里的分支与证据原文、存档里的页面结构化信号。它们分属两个
文件、两种形状（JSONL 行 vs 嵌套 JSON），人每次手工拼一遍容易漏——而漏掉的
那一处恰好是判错档的地方：

- 漏 **存档顶层 `title`/`user_prompt`/`query`** ⇒ 把 `scene.title`（**页面标题**）
  当成目标片名去核 D-6；
- 漏 **`outcomes[]` 的 `branch`/`evidence` 原文** ⇒ 只对着 `pred_*` 判，等于照抄；
- 漏 **`visits[].steps[].action` 的实体引用** ⇒ D-2 根本没得判。

**这个脚本只给事实，不给结论**：不判档、不建议分数、不推分支。
判分是人的事（`goldwork` 手册 §7.2）。它只保证「该看的都摆出来了」。

**用法**（必须在仓库根跑）::

    uv run python trajectory_pipeline/research/probes/gold_worksheet_dump.py \
        --golden-id E014__f50809fb__30062587 T001__ab5b6296__1487aa47
    uv run python trajectory_pipeline/research/probes/gold_worksheet_dump.py --all --dim d5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
WORKSHEET = os.path.join(
    REPO, "trajectory_pipeline", "output", "pipeline", "calibration", "v8", "gold_worksheet.jsonl"
)
SAMPLES_DIR = os.path.join(REPO, "trajectory_pipeline", "output", "pipeline", "samples")
DIMS = ("d2", "d4", "d5", "d6")


def _load(path: str) -> list[dict]:
    if not os.path.exists(path):
        sys.exit("工作表不在：%s\n先跑：uv run python -m trajectory_pipeline.evaluation.calibration.goldwork worksheet --version 8" % path)
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _p3_sample(record: dict) -> dict | None:
    """P3 样本（训练制品）——**D-2 的「实体引用」与 D-6 的「片名」只在这里**。

    存档的 ``step.action`` 里**没有** ``target``（只有 tool/params/origin），
    而 D-2 判的「动作引用的实体」恰恰是那个 ``target``；D-6 判的
    「目标内容标识（片名）」是 ``Sample.title``。两处都只能从 P3 产物读。
    """
    if not os.path.isdir(SAMPLES_DIR):
        return None
    prefix = "%s__%s__" % (record.get("task_id"), record.get("run_id"))
    unit_url = record.get("unit_url") or ""
    for name in sorted(os.listdir(SAMPLES_DIR)):
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        with open(os.path.join(SAMPLES_DIR, name), encoding="utf-8") as fh:
            sample = json.load(fh)
        outcome = sample.get("outcome") or {}
        if outcome.get("url") == unit_url:
            return sample
    return None



def _archive_of(record: dict) -> dict | None:
    path = record.get("archive") or ""
    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(REPO, path)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _outcome_of(arch: dict, unit_url: str) -> dict | None:
    for outcome in arch.get("outcomes", []) or []:
        if outcome.get("url") == unit_url:
            return outcome
    return None


def _clip(text: str, limit: int) -> str:
    text = str(text or "").replace("\r\n", "\n")
    if len(text) <= limit:
        return text
    return text[:limit] + "…（截断，共 %d 字）" % len(text)


def _dump_one(gid: str, rows: list[dict], body_limit: int) -> None:
    by_dim = {r["dimension"]: r for r in rows}
    d5 = by_dim.get("d5") or next(iter(rows))
    record = d5.get("record") or {}
    arch = _archive_of(record)
    outcome = _outcome_of(arch or {}, record.get("unit_url") or "")

    print("=" * 100)
    print("样本 %s" % gid)
    print("  task/run       : %s / %s" % (record.get("task_id"), record.get("run_id")))
    print("  unit_url       : %s" % record.get("unit_url"))
    print("  site_url       : %s" % record.get("site_url"))
    print("  play_page_url  : %s" % (record.get("play_page_url") or "（空）"))
    print("  reached_play   : %s" % record.get("reached_play_page"))
    print("  gold_label     : %s  ← 这是 D-1 的分支名，**不是**这四维的分数" % d5.get("gold_label"))
    if arch is None:
        print("  !! 存档读不到：%s" % record.get("archive"))
        return

    print("\n-- 存档顶层（目标内容标识在**这里**，`scene.title` 是页面标题不是片名） --")
    print("  title      : %s" % _clip(arch.get("title"), 200))
    print("  query      : %s" % _clip(arch.get("query"), 200))
    print("  user_prompt: %s" % _clip(arch.get("user_prompt"), 300))

    if outcome is not None:
        print("\n-- outcomes[url==unit_url]（D-5 的分支与证据 / D-6 的验证证据原文） --")
        for key in (
            "branch",
            "branch_label",
            "question",
            "source",
            "is_negative_sample",
            "fallback_used",
            "site_url",
            "play_page_url",
            "reached_play_page",
        ):
            print("  %-20s: %s" % (key, outcome.get(key)))
        print("  %-20s: %s" % ("evidence", _clip(outcome.get("evidence"), 1200)))
    else:
        print("\n  !! outcomes 里没有 url==unit_url 的那条")

    sample = _p3_sample(record)
    if sample is not None:
        print("\n-- P3 样本（D-2 的「实体引用」= actions[].target；D-6 的「片名」= title） --")
        print("  sample_id   : %s" % sample.get("sample_id"))
        print("  title       : %s  ← 目标内容标识（片名）" % sample.get("title"))
        print("  tool_surface: %s" % sample.get("tool_surface"))
        print("  degraded_from: %s" % (sample.get("degraded_from") or []))
        print("  rationale_missing: %s" % sample.get("rationale_missing"))
        p3_outcome = sample.get("outcome") or {}
        print("  outcome 键  : %s" % sorted(p3_outcome))
        print("  outcome.site_url / play_page_url / reached: %s | %s | %s"
              % (p3_outcome.get("site_url"), p3_outcome.get("play_page_url"), p3_outcome.get("reached_play_page")))
        print("  actions（实体引用在 target）:")
        for action in (sample.get("six") or {}).get("actions") or []:
            print("      %s target=%s params=%s" % (
                action.get("tool"),
                json.dumps(action.get("target"), ensure_ascii=False),
                _clip(json.dumps(action.get("params"), ensure_ascii=False), 160),
            ))
        # D-2 的机械动作：把动作引用的实体拿到观察里检索
        haystacks = []
        for visit in arch.get("visits", []) or []:
            for name in ("site_obs", "player_obs"):
                obs = visit.get(name)
                if isinstance(obs, dict):
                    haystacks.append(("%s/%s" % (visit.get("url"), name),
                                      str(obs.get("body_text") or "") + "\n" + str(obs.get("page_title") or "")))
        print("  D-2 检索（动作实体 → 观察里找得到吗）：")
        seen_targets = set()
        for action in (sample.get("six") or {}).get("actions") or []:
            target = action.get("target") or {}
            needle = target.get("label") or target.get("name") or ""
            if not needle or needle in seen_targets:
                continue
            seen_targets.add(needle)
            hits = [where for where, text in haystacks if needle in text]
            if hits:
                print("      「%s」(%s) → 整串命中 %d 处：%s" % (
                    _clip(needle, 120), target.get("tag") or target.get("role") or "?",
                    len(hits), " / ".join(hits[:3])))
                continue
            # 整串不命中 ≠ 实体不在：DOM 标签常把多行可见文字**拼成一行**
            # （「综艺热播榜No.1」+「职业体验 · 真人秀 · 观察」+「胡军李乃文…」
            #  在正文里是三行）⇒ 逐段再检一次。
            tokens = [t for t in re.split(r"[\s·|]+", needle) if len(t) >= 3]
            if not tokens:
                print("      「%s」(%s) → **观察里检索不到**" % (_clip(needle, 120), target.get("tag") or "?"))
                continue
            missing = [t for t in tokens if not any(t in text for _, text in haystacks)]
            if missing:
                print("      「%s」(%s) → **缺这些片段：%s**（其余片段可检索到）"
                      % (_clip(needle, 120), target.get("tag") or "?", " / ".join(missing[:5])))
            else:
                print("      「%s」(%s) → **整串不命中但逐段全部命中**（DOM 把多行文字拼成一行）"
                      % (_clip(needle, 120), target.get("tag") or "?"))
        for action in (sample.get("six") or {}).get("actions") or []:
            url = (action.get("params") or {}).get("url")
            if not url:
                continue
            key = "url:" + url
            if key in seen_targets:
                continue
            seen_targets.add(key)
            print("      URL %s → %s" % (_clip(url, 90), "动作的 URL 直接来自搜索/观察" ))
            break
    else:
        print("\n  !! P3 样本找不到（%s 下没有 %s__*__*.json 命中 unit_url）" % (SAMPLES_DIR, prefix))

    print("\n-- visits（动作里的实体引用 ↔ 观察；页面结构化信号） --")
    for idx, visit in enumerate(arch.get("visits", []) or []):
        print("  [visit %d] url=%s success=%s" % (idx, visit.get("url"), visit.get("success")))
        if visit.get("landed_url"):
            print("      landed_url=%s" % visit.get("landed_url"))
        for step in visit.get("steps", []) or []:
            action = step.get("action") or {}
            print("      step tool=%s params=%s" % (action.get("tool"), _clip(json.dumps(action.get("params"), ensure_ascii=False), 240)))
            if step.get("rationale"):
                print("           rationale: %s" % _clip(step.get("rationale"), 240))
            if step.get("observation"):
                print("           observation: %s" % _clip(step.get("observation"), 400))
            if step.get("error"):
                print("           error: %s" % _clip(step.get("error"), 240))
        for name in ("site_obs", "player_obs"):
            obs = visit.get(name)
            if obs is None:
                print("      %s: None" % name)
                continue
            print(
                "      %s: video=%s iframe=%s elements=%s/%s links=%s truncated=%s body_source=%s degraded=%s"
                % (
                    name,
                    obs.get("video_tag_count"),
                    obs.get("iframe_count"),
                    obs.get("elements_total"),
                    len(obs.get("interactive_elements") or []),
                    obs.get("links_total"),
                    obs.get("truncated"),
                    obs.get("body_source"),
                    obs.get("degraded"),
                )
            )
            print("           page_title: %s" % _clip(obs.get("page_title"), 160))
            print("           body_text : %s" % _clip(obs.get("body_text"), body_limit))

    for dim in DIMS:
        row = by_dim.get(dim)
        if row is None:
            continue
        print("\n-- 格子 %s（rubric_ref=%s） --" % (dim, row.get("rubric_ref")))
        anchors = row.get("anchors") or {}
        for tier in sorted(anchors):
            print("  档 %s: %s" % (tier, anchors[tier]))
        print("  pred_value  : %s" % row.get("pred_value"))
        print("  pred_branch : %s" % (row.get("pred_branch") or "（空）"))
        for reason in row.get("pred_reasons") or []:
            print("  pred_reason : %s" % _clip(reason, 400))
        scene = row.get("scene") or {}
        print("  scene.found : %s (%s)" % (scene.get("found"), scene.get("status")))
        print("  scene.title : %s  ← 页面标题" % _clip(scene.get("title"), 160))
        for step in scene.get("steps") or []:
            print("  scene.step  : %s" % _clip(step, 300))
        if scene.get("body"):
            print("  scene.body  : %s" % _clip(scene.get("body"), body_limit))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="软维度金标填表现场 dump（只给事实，不判分）")
    ap.add_argument("--worksheet", default=WORKSHEET, help="工作表路径")
    ap.add_argument("--golden-id", nargs="+", help="要看的 golden_id（可多个）")
    ap.add_argument("--all", action="store_true", help="全部样本")
    ap.add_argument("--dim", choices=DIMS, help="配合 --all：只看某一维")
    ap.add_argument("--body-limit", type=int, default=900, help="正文截断长度")
    args = ap.parse_args(argv)

    rows = _load(args.worksheet)
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["golden_id"], []).append(row)

    if args.all:
        wanted = sorted(groups)
    else:
        wanted = list(args.golden_id or [])
        missing = [g for g in wanted if g not in groups]
        if missing:
            sys.exit("工作表里没有这些 golden_id：%s" % ", ".join(missing))

    for gid in wanted:
        group = groups[gid]
        if args.dim:
            group = [r for r in group if r["dimension"] == args.dim] or group
        _dump_one(gid, group, args.body_limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
