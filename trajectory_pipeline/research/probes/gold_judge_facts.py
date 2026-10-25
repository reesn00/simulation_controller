"""**软维度金标判读的紧凑事实表**（91 条全量判读用）。

**为什么要有这个脚本**（2026-10-24，软维度金标填表）

`gold_worksheet_dump.py` 是「一条一条看」的工具：正文、每步 observation、
每维锚点全摆出来——适合细看一条，但**91 条读不动**（同一份存档的
8–9 个 visit 会被重复打印 9 遍）。

这个脚本换一种排法：

- **先按存档分组**（同一 `task_id__run_id` 的多个 unit 共享同一串 visit），
  存档级事实（页面标题 / 媒体计数 / 关键词形态）**只打一遍**；
- 每条样本只打**属于它自己的那几行**：分支、证据、D-2 的实体引用、
  D-6 的四个必填项核对结果。

它仍是**只给事实、不判分**——判档是人的事（`goldwork` 手册 §7.2）。

**用法**（必须在仓库根跑）::

    uv run python trajectory_pipeline/research/probes/gold_judge_facts.py
    uv run python trajectory_pipeline/research/probes/gold_judge_facts.py --only T001
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
CANDIDATES = os.path.join(
    REPO, "trajectory_pipeline", "evaluation", "golden", "data", "candidates.jsonl"
)
ANNOTATIONS = os.path.join(
    REPO, "trajectory_pipeline", "evaluation", "golden", "data", "annotations.jsonl"
)

#: 页面形态关键词——判「这页是什么形态」用，**不做**任何自动判分。
FORMS = (
    "开通会员",
    "开通VIP",
    "VIP",
    "会员",
    "试看",
    "免费",
    "付费",
    "订阅",
    "当前设备不支持播放",
    "错误码",
    "正在播放",
    "预告",
    "片段",
    "登录",
    "Just a moment",
    "安全验证",
    "404",
    "不见了",
    "地区",
)


def _load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _p3_sample(record: dict) -> dict | None:
    """P3 样本——D-2 的「实体引用」与 D-6 的「片名」只在这里。"""
    if not os.path.isdir(SAMPLES_DIR):
        return None
    prefix = "%s__%s__" % (record.get("task_id"), record.get("run_id"))
    unit_url = record.get("unit_url") or ""
    for name in sorted(os.listdir(SAMPLES_DIR)):
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        with open(os.path.join(SAMPLES_DIR, name), encoding="utf-8") as fh:
            sample = json.load(fh)
        if (sample.get("outcome") or {}).get("url") == unit_url:
            return sample
    return None


def _short(url: str, keep: int = 46) -> str:
    url = str(url or "")
    url = re.sub(r"^https?://(www\.)?", "", url)
    return url if len(url) <= keep else url[:keep] + "…"


def _hits(text: str) -> str:
    text = text or ""
    return ",".join(form for form in FORMS if form in text) or "-"


def _archive_facts(arch: dict) -> list[str]:
    """存档级事实：每个 visit 一行（媒体计数 / 标题 / 形态关键词）。"""
    lines: list[str] = []
    for idx, visit in enumerate(arch.get("visits", []) or []):
        obs = visit.get("site_obs") or visit.get("player_obs")
        if not isinstance(obs, dict):
            lines.append("  v%-2d %-46s 观察=None" % (idx, _short(visit.get("url"))))
            continue
        body = str(obs.get("body_text") or "")
        lines.append(
            "  v%-2d %-46s video=%s iframe=%s el=%s/%s trunc=%s title=%s"
            % (
                idx,
                _short(visit.get("url")),
                obs.get("video_tag_count"),
                obs.get("iframe_count"),
                len(obs.get("interactive_elements") or []),
                obs.get("elements_total"),
                "T" if obs.get("truncated") else "-",
                _short(obs.get("page_title"), 44),
            )
        )
        forms = _hits(body + " " + str(obs.get("page_title") or ""))
        if forms != "-":
            lines.append("       形态词: %s" % forms)
    return lines


def _d2_search(sample: dict | None, arch: dict) -> list[str]:
    """D-2 的机械动作：动作引用的实体 → 观察里检索得到吗。"""
    if not sample:
        return ["  D2: 无 P3 样本"]
    haystacks: list[tuple[str, str]] = []
    for visit in arch.get("visits", []) or []:
        for name in ("site_obs", "player_obs"):
            obs = visit.get(name)
            if isinstance(obs, dict):
                haystacks.append(
                    (
                        "v%s/%s" % (visit.get("url"), name),
                        str(obs.get("body_text") or "")
                        + "\n"
                        + str(obs.get("page_title") or "")
                        + "\n"
                        + "\n".join(
                            str(el.get("label") or "") for el in (obs.get("interactive_elements") or [])
                        ),
                    )
                )
    out: list[str] = []
    seen: set[str] = set()
    for action in (sample.get("six") or {}).get("actions") or []:
        target = action.get("target") or {}
        needle = target.get("label") or target.get("name") or ""
        if needle and needle not in seen:
            seen.add(needle)
            hits = [where for where, text in haystacks if needle in text]
            if hits:
                out.append("  D2 实体「%s」→ 整串命中 %d 处" % (needle[:70], len(hits)))
                continue
            tokens = [t for t in re.split(r"[\s·|、,，]+", needle) if len(t) >= 3]
            if not tokens:
                out.append("  D2 实体「%s」→ 观察里检索不到" % needle[:70])
                continue
            missing = [t for t in tokens if not any(t in text for _, text in haystacks)]
            if missing:
                out.append(
                    "  D2 实体「%s」→ 缺片段: %s" % (needle[:70], "/".join(missing[:4]))
                )
            else:
                out.append("  D2 实体「%s」→ 整串不命中但逐段全命中（DOM 拼行）" % needle[:70])
        url = (action.get("params") or {}).get("url")
        if url and ("url:" + url) not in seen:
            seen.add("url:" + url)
            in_obs = any(url in text for _, text in haystacks)
            out.append(
                "  D2 URL %s → %s"
                % (_short(url, 40), "URL 出现在观察里" if in_obs else "URL 不在观察正文（是动作参数）")
            )
    if not out:
        out.append("  D2: 无实体引用可检（无 click/带参动作）")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="软维度金标判读的紧凑事实表（只给事实，不判分）")
    ap.add_argument("--only", default="", help="只打印 golden_id 含该串的样本")
    args = ap.parse_args(argv)

    rows = _load(WORKSHEET)
    groups: dict[str, dict[str, dict]] = {}
    for row in rows:
        groups.setdefault(row["golden_id"], {})[row["dimension"]] = row

    machine = {c["golden_id"]: c for c in _load(CANDIDATES)}
    human = {a["golden_id"]: a for a in _load(ANNOTATIONS)}

    by_archive: dict[str, list[str]] = {}
    for gid in groups:
        if args.only and args.only not in gid:
            continue
        record = groups[gid]["d5"]["record"]
        by_archive.setdefault(record.get("archive") or "", []).append(gid)

    for archive_path, gids in by_archive.items():
        arch = json.load(open(archive_path, encoding="utf-8"))
        print("=" * 100)
        print("存档 %s" % os.path.basename(archive_path))
        print("  目标=%s | 查询=%s" % (arch.get("title"), _short(arch.get("query"), 50)))
        for line in _archive_facts(arch):
            print(line)
        for gid in sorted(gids):
            group = groups[gid]
            record = group["d5"]["record"]
            outcome = next(
                (o for o in arch.get("outcomes", []) or [] if o.get("url") == record.get("unit_url")),
                {},
            )
            sample = _p3_sample(record)
            p3_outcome = (sample or {}).get("outcome") or {}
            ann = human.get(gid) or {}
            scores = (machine.get(gid) or {}).get("dimension_scores") or {}
            print("  --- %s" % gid)
            print(
                "      D1金标=%-22s 存档分支=%-22s 机器 d2/d4/d5/d6=%s/%s/%s/%s"
                % (
                    ann.get("label") or "-",
                    outcome.get("branch") or "success",
                    scores.get("d2"),
                    scores.get("d4"),
                    scores.get("d5"),
                    scores.get("d6"),
                )
            )
            print(
                "      unit=%s | reached=%s | 播放页=%s | question=%s"
                % (
                    _short(record.get("unit_url")),
                    record.get("reached_play_page"),
                    _short(record.get("play_page_url"), 40) or "(空)",
                    outcome.get("question") or "-",
                )
            )
            print("      证据: %s" % str(outcome.get("evidence") or "")[:200].replace("\n", " "))
            for line in _d2_search(sample, arch):
                print(line)
            title = (sample or {}).get("title")
            body_all = " ".join(
                str((v.get("site_obs") or v.get("player_obs") or {}).get("body_text") or "")
                for v in arch.get("visits", []) or []
                if isinstance(v.get("site_obs") or v.get("player_obs"), dict)
            )
            print(
                "      D6 四项: 站点URL=%s 播放页URL=%s 片名=%s(%s) 证据=%s | 播放页URL∈观察=%s 片名∈正文=%s"
                % (
                    "有" if record.get("site_url") else "缺",
                    ("有" if record.get("play_page_url") else "不适用/缺"),
                    "有" if title else "缺",
                    title or "-",
                    "有" if outcome.get("evidence") else "缺",
                    "是" if record.get("play_page_url") and record.get("play_page_url") in body_all else "否",
                    "是" if title and title in body_all else "否",
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
