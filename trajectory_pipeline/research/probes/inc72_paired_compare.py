"""配对比较：按**落地 URL** 把双跑两面的 outcome 配成对，再比分支。

**为什么要有这个脚本**（2026-10-21 实测逼出来的）：

`compare-drivers` 的判据 ① 比的是**两批样本的分支占比**。但两批样本
**看的不是同一批页面**——百度跳转链接的 token 每次都变，且两个执行层
**抽正文的能力不同**（实测：obscura 在爱奇艺这类客户端渲染的 SPA 上
`body_len=0`，camoufox 能抽到）。于是占比差里混着两样东西：

1. 站点质量差（要测的）
2. 观测能力差（**污染**）

**本脚本按落地 URL 配对**，把「同一个页面，两面各判成什么」摊开，
让人能一眼看出哪些分歧是**观测能力**造成的。

**用法**（必须在仓库根跑）：

    uv run python trajectory_pipeline/research/probes/inc72_paired_compare.py \
        --a trajectory_pipeline/output/pipeline/dualrun2/obscura \
        --b trajectory_pipeline/output/pipeline/dualrun2/camoufox \
        --json trajectory_pipeline/research/probes/inc72_paired.json

**只读**：不写 P1、不改代码、不联网。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from collections import Counter, defaultdict
from typing import Any, Mapping
from urllib.parse import urlsplit

#: 落地 URL 归一化：去协议、去 ``www.``、去查询串与锚点、去掉尾斜杠。
#: 查询串要去——爱奇艺/1905 的 ``fr=`` / ``__hz=`` 每次都不一样，
#: 留着就配不上对。
_QUERY = re.compile(r"[?#].*$")
_WWW = re.compile(r"^www\.")


def norm_url(url: str) -> str:
    """落地 URL → 配对键。空 URL 归一成空串（不参与配对）。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    if not parts.netloc:
        return url.strip().lower()
    host = _WWW.sub("", parts.netloc.lower())
    path = _QUERY.sub("", parts.path or "").rstrip("/")
    return f"{host}{path}"


def load_runs(path: pathlib.Path) -> list[dict[str, Any]]:
    """读一个存档目录里的全部 P1 存档（按形状认，不靠文件名）。"""
    out: list[dict[str, Any]] = []
    for f in sorted(path.glob("*.json")):
        if f.name == "negative.jsonl":
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(d, Mapping):
            continue
        if "run_config" not in d or not ("steps" in d or "visits" in d):
            continue
        out.append(dict(d))
    return out


def _side_rows(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """一侧的每个 task → 若干行 ``{key, branch, degraded, body_len, ...}``。

    一行对应**一次落地观察**（`visits[]`）。分支从 ``outcomes[]`` 挂上来
    ——outcome 带最终 URL 与判定，visit 带落地 URL 与观察读数，
    靠落地 URL 归一化后对接。
    """
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        task = str(rec.get("task_id") or "")
        outcome_by_key: dict[str, Mapping[str, Any]] = {}
        for oc in rec.get("outcomes") or ():
            if not isinstance(oc, Mapping):
                continue
            key = norm_url(str(oc.get("url") or oc.get("site_url") or ""))
            if key:
                outcome_by_key.setdefault(key, oc)
        for v in rec.get("visits") or ():
            if not isinstance(v, Mapping):
                continue
            landed = str(v.get("landed_url") or "")
            key = norm_url(landed)
            oc = outcome_by_key.get(key, {})
            obs = v.get("site_obs") or {}
            if not isinstance(obs, Mapping):
                obs = {}
            by_task[task].append({
                "key": key,
                "landed_url": landed,
                "branch": str(oc.get("branch") or ""),
                "branch_label": str(oc.get("branch_label") or ""),
                "evidence": str(oc.get("evidence") or "")[:400],
                "degraded": list(obs.get("degraded") or ()),
                "body_len": obs.get("body_len"),
                "elements_total": obs.get("elements_total"),
                "is_negative_sample": bool(oc.get("is_negative_sample")),
            })
    return by_task


def compare(a_dir: pathlib.Path, b_dir: pathlib.Path) -> dict[str, Any]:
    a_rows = _side_rows(load_runs(a_dir))
    b_rows = _side_rows(load_runs(b_dir))
    tasks = sorted(set(a_rows) | set(b_rows))

    report: dict[str, Any] = {"a": str(a_dir), "b": str(b_dir), "tasks": {}}
    totals = Counter()
    for task in tasks:
        a_list = a_rows.get(task, [])
        b_list = b_rows.get(task, [])
        a_by_key = {r["key"]: r for r in a_list if r["key"]}
        b_by_key = {r["key"]: r for r in b_list if r["key"]}
        shared = sorted(set(a_by_key) & set(b_by_key))
        only_a = sorted(set(a_by_key) - set(b_by_key))
        only_b = sorted(set(b_by_key) - set(a_by_key))

        matrix: Counter = Counter()
        diffs: list[dict[str, Any]] = []
        degraded_a = degraded_b = 0
        for k in shared:
            ra, rb = a_by_key[k], b_by_key[k]
            matrix[(ra["branch"] or "(无分支=成功)", rb["branch"] or "(无分支=成功)")] += 1
            if ra["branch"] != rb["branch"]:
                diffs.append({
                    "key": k,
                    "a_branch": ra["branch"], "b_branch": rb["branch"],
                    "a_degraded": ra["degraded"], "b_degraded": rb["degraded"],
                    "a_body_len": ra["body_len"], "b_body_len": rb["body_len"],
                    "a_evidence": ra["evidence"][:200], "b_evidence": rb["evidence"][:200],
                })
            if ra["degraded"]:
                degraded_a += 1
            if rb["degraded"]:
                degraded_b += 1

        report["tasks"][task] = {
            "paired": len(shared),
            "only_a": only_a,
            "only_b": only_b,
            "a_rows": len(a_list), "b_rows": len(b_list),
            "a_degraded_of_paired": degraded_a,
            "b_degraded_of_paired": degraded_b,
            "branch_matrix": {f"{x} | {y}": n for (x, y), n in sorted(matrix.items())},
            "branch_disagreements": diffs,
        }
        totals["paired"] += len(shared)
        totals["only_a"] += len(only_a)
        totals["only_b"] += len(only_b)
        totals["disagree"] += len(diffs)
        totals["a_degraded"] += degraded_a
        totals["b_degraded"] += degraded_b
        for (x, y), n in matrix.items():
            totals[f"m::{x} | {y}"] += n

    report["totals"] = {k: v for k, v in totals.items()}
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按落地 URL 配对的双跑比较")
    ap.add_argument("--a", required=True, help="A 侧存档目录")
    ap.add_argument("--b", required=True, help="B 侧存档目录")
    ap.add_argument("--json", default=None, help="报告落盘路径")
    args = ap.parse_args(argv)

    a_dir, b_dir = pathlib.Path(args.a), pathlib.Path(args.b)
    if not a_dir.is_dir() or not b_dir.is_dir():
        print(f"目录不存在：{a_dir} / {b_dir}", file=sys.stderr)
        return 2

    report = compare(a_dir, b_dir)
    print(f"A = {a_dir}\nB = {b_dir}")
    for task, t in report["tasks"].items():
        print(f"\n== {task} ==")
        print(f"  配对 {t['paired']} | 只在 A {len(t['only_a'])} | 只在 B {len(t['only_b'])}")
        print(f"  配对里带降级的：A {t['a_degraded_of_paired']} / B {t['b_degraded_of_paired']}")
        for k, n in t["branch_matrix"].items():
            print(f"    {k:<52} ×{n}")
        for d in t["branch_disagreements"]:
            print(f"    ! 分歧 {d['key']}")
            print(f"        A: {d['a_branch']} degraded={d['a_degraded']} body={d['a_body_len']}")
            print(f"        B: {d['b_branch']} degraded={d['b_degraded']} body={d['b_body_len']}")
    print("\n总计:", json.dumps(report["totals"], ensure_ascii=False, indent=2))

    if args.json:
        out = pathlib.Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n落盘: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
