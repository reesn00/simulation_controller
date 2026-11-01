"""probe: **一条轨迹跨着哪几档** + **它的头部站点在哪一档**（方案 23 取证）。

回答三个问题（每个答案都会改变「轨迹级标注归哪一档」的口径）：

1. 一条轨迹的站点**跨着哪几档**？（实测：50 条里 28 条三档全跨）
2. 按**最严档**归并（holdout > regression > calibration）⇒ 多少条轨迹
   的裁定**不许回流**？（决定裁定③「只上传不回流」在轨迹级怎么落）
3. 按**头部站点**归并（裁定② 的末条结论所在的那个站）⇒ 多少条能回流？

⚠️ 本探针**只出读数，不出结论**——口径由 owner 裁定。

用法（**必须在仓库根跑**）::

    python -m trajectory_pipeline.research.probes.per_run_split_membership v16 \
        --archives <P1 存档根>
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys

from trajectory_pipeline.outcomerules import site_entries

DATA = pathlib.Path(__file__).resolve().parents[2] / "evaluation" / "golden" / "data"
SPLIT_FILES = ("calibration.jsonl", "regression.jsonl", "holdout.jsonl")


def load(version: str) -> list[dict]:
    """某一版的三档行（带 ``__split__``）。"""
    rows: list[dict] = []
    base = DATA / version
    for name in SPLIT_FILES:
        path = base / name
        if not path.exists():
            continue
        split = name[: -len(".jsonl")]
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row["__split__"] = split
            rows.append(row)
    return rows


def headline_split(
    archive: dict, split_of_url: dict[tuple[str, str], str], task: str, run: str
) -> tuple[str, str]:
    """这条轨迹的**头部站点**（裁定② 的末条结论所在站）及其档位。

    ⚠️ **``run_id`` 不在存档里**——它就是文件名（``<task>__<run>.json``）
    ⇒ 由调用方从文件名取，传进来。

    ⚠️ **键的形状必须与调用方一致**：调用方的 ``split_of_url`` 用
    ``(task__run, url)`` 两段键（``tid`` 就是 ``"<task>__<run>"``）⇒
    这里若按 ``(task, run, url)`` 三段去查，**永远查不到** ⇒
    「全部配不到」这个读数会是**键形状不匹配的假象**，不是数据事实。
    （2026-11-01 实测踩过：改成三段键 ⇒ 报「44/50 配不到」；改回两段键
    ⇒ 实为 **41/44 可配**。⇒ 这类读数**必须能对着源码复核**。）

    Returns:
        ``(split, url)``；配不到 ⇒ ``("", "")``。
    """
    tid = f"{task}__{run}"
    outcomes = archive.get("outcomes") or []
    if not outcomes:
        return "", ""
    last_index = len(outcomes) - 1
    for entry in site_entries(archive):
        if entry.outcome_index != last_index:
            continue
        split = split_of_url.get((tid, entry.url), "")
        if not split:
            # 末条 outcome 的 url 可能不是「被访问点配到过」的那条 ⇒ 退回
            # 「该 url 的末条 outcome」这条 entry（口径 C 第 2 步）。
            for other in site_entries(archive):
                if other.url == entry.url:
                    split = split_of_url.get((tid, other.url), "")
                    if split:
                        return split, other.url
        return split, entry.url
    return "", ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("version", nargs="?", default="v16")
    ap.add_argument(
        "--archives",
        default=None,
        help="P1 存档根（其下是 <task>__<run>.json）",
    )
    args = ap.parse_args()

    rows = load(args.version)
    by_traj: dict[str, list[str]] = collections.defaultdict(list)
    split_of_url: dict[tuple[str, str, str], str] = {}
    for row in rows:
        tid = f"{row.get('task_id') or ''}__{row.get('run_id') or ''}"
        by_traj[tid].append(str(row.get("__split__") or ""))
        split_of_url[(tid, str(row.get("url") or ""))] = str(row.get("__split__") or "")

    combos = collections.Counter(tuple(sorted(set(v))) for v in by_traj.values())
    print(f"=== {args.version} · 站点行 {len(rows)} 条 · 轨迹 {len(by_traj)} 条 ===")
    print("档位集合分布（站点集合去重后）：")
    for combo, n in sorted(combos.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {'+'.join(combo) or '（空）':<40} {n}")

    strict = collections.Counter()
    for combo, n in combos.items():
        if "holdout" in combo:
            strict["holdout"] += n
        elif "regression" in combo:
            strict["regression"] += n
        else:
            strict["calibration"] += n
    print("\n按「最严档」归并（holdout > regression > calibration）：")
    for key in ("calibration", "regression", "holdout"):
        print(f"  {key:<12} {strict[key]}")

    if not args.archives:
        print("\n（没给 --archives ⇒ 头部站点口径没算）")
        return 0

    root = pathlib.Path(args.archives)
    head = collections.Counter()
    missing: list[str] = []
    no_split = collections.Counter()
    frozen_urls = {str(row.get("url") or "") for row in rows}
    for tid in sorted(by_traj):
        task, _, run = tid.partition("__")
        path = root / f"{task}__{run}.json"
        if not path.exists():
            missing.append(tid)
            continue
        archive = json.loads(path.read_text(encoding="utf-8"))
        split, url = headline_split(archive, split_of_url, task, run)
        head[split or "（配不到）"] += 1
        if not split:
            # 配不到有两种**不同**的原因 ⇒ 别混成一个数：
            # ① 这个 url 在冻结集里**压根没有**（没被标注过）
            # ② 这个 url 在冻结集里有，但**不属于这条轨迹**（同 url 换了 run）
            no_split[
                "url 在冻结集里没有"
                if url not in frozen_urls
                else "url 在冻结集里但不属于这条轨迹"
            ] += 1
    print("\n按「头部站点」归并（裁定② 的末条结论所在站）：")
    for key, n in sorted(head.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {key:<12} {n}")
    if no_split:
        print("  配不到的原因分解：")
        for key, n in sorted(no_split.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"    {key:<32} {n}")
    if missing:
        print(f"⚠ 存档读不到的轨迹 {len(missing)} 条: {missing[:5]}")
        if len(missing) == len(by_traj):
            # ⚠️ **fail-closed**：一条都读不到 ⇒ 存档根指错了（或 worktree 的
            # `output/pipeline/` 是空的）⇒ 下面的「头部站点口径」读数是假的
            # ⇒ 显式退 2，不装成读数。
            print(
                f"\n❌ 存档根 {root} 里**一条轨迹都读不到** ⇒ "
                "「按头部站点定档」的读数是假的（不是「全配不到」）。\n"
                "⚠️ **worktree 里 `output/pipeline/` 是空的**（数据在主树）⇒ "
                "把 `--archives` 指成主树的那个绝对路径。",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
