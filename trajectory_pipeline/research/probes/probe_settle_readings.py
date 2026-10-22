"""读「页面有没有落定」的现场读数——**读存档，不联网**。

这个探针回答一个问题：**存档里的观察有多少是「读到一半」的**。

判据（2026-10-22 定的，见 ``docs/计划进展/90-增量编年史.md`` 增量八十）：

- **过渡态**：URL 非空而 title 为空 ⇒ 新文档 commit 了但还没解析完，
  这一份观察**不是**页面事实（实测 bilibili 播放页就是「URL 有、title 空、
  媒体计数两读一致（1/1）」——那份读数**是好的**，所以这条只当**信号**
  不当**判据**）。
- **读数打架**：同一 URL 的两次读数，媒体元素**变少** ⇒ 后一次读到了一半
  （变多 = 页面还在加载，是合法的）。

⚠️ **它只报读数，不下结论**——结论归人（CLAUDE.md：「判断『结论对不对』
不在门禁里」）。

用法（**必须在仓库根跑**）::

    python trajectory_pipeline/research/probes/probe_settle_readings.py [存档目录] [driver]

    # ★ **量「修复前」那一代时要排除批次子目录**：
    #   探针是 `rglob`（递归）扫的，而批次子目录就躺在存档目录底下
    #   （`camoufox_prod2/`）⇒ 不排除就把**两代混进一张表**。
    python trajectory_pipeline/research/probes/probe_settle_readings.py \
        trajectory_pipeline/output/pipeline camoufox --exclude camoufox_prod2
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path("trajectory_pipeline/output/pipeline")


def _utf8_console() -> None:
    """把标准输出改成 utf-8（Windows 控制台默认 GBK：中文花屏、``⇒`` 直接抛）。

    探针的读数是要给人看的，打不出来就等于没量。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):    # pragma: no cover - 老解释器
            pass


def _obs_of(node: object):
    """递归找出所有 ``observation`` 字典。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("observation", "obs") and isinstance(value, dict):
                yield value
            yield from _obs_of(value)
    elif isinstance(node, list):
        for item in node:
            yield from _obs_of(item)


def _media(obs: dict) -> int:
    return int(obs.get("video_tag_count") or 0)


def _frames(obs: dict) -> int:
    return int(obs.get("iframe_count") or 0)


def main(argv: list[str]) -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(description="存档里的「读到一半」读数")
    ap.add_argument("root", nargs="?", default=str(ROOT), help="存档目录")
    ap.add_argument("driver", nargs="?", default="", help="只统计这个 driver")
    ap.add_argument("--exclude", action="append", default=[],
                    help="排除掉**含这个目录名**的路径（可多次）。"
                         "批次子目录（camoufox_prod2）就躺在存档目录底下，"
                         "不排除就把两代混进一张表")
    # ⚠️ **``parse_args`` 传进去的整个列表都会被当参数解析**（含第 0 项）
    # ——它只在不传参时才自己剥掉 ``sys.argv[0]``。这里统一按
    # 「argv 是完整 argv」处理，剥掉第 0 项。
    args = ap.parse_args(list(argv)[1:] if argv else [])

    root = Path(args.root)
    only_driver = args.driver
    excluded = set(args.exclude)
    if not root.exists():
        print(f"目录不存在: {root}")
        return 1

    archives = sorted(p for p in root.rglob("*.json")
                      if p.is_file() and "_backup" not in p.parts
                      and not (excluded & set(p.parts)))
    by_driver: Counter[str] = Counter()
    obs_total = 0
    unsettled: list[tuple[str, str]] = []
    conflicts: list[tuple[str, str]] = []
    degraded_nav: list[tuple[str, str]] = []
    per_archive: list[tuple[str, int, int]] = []

    for path in archives:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        driver = str(data.get("driver") or "")
        if only_driver and driver != only_driver:
            continue
        by_driver[driver] += 1

        seen: dict[str, list[tuple[int, int]]] = {}
        n_obs = 0
        n_unsettled = 0
        for obs in _obs_of(data):
            if not obs.get("url"):
                continue
            n_obs += 1
            obs_total += 1
            if not str(obs.get("page_title") or ""):
                n_unsettled += 1
                unsettled.append((path.name, str(obs.get("url"))[:70]))
            if any("nav_unsettled" in str(d) for d in (obs.get("degraded") or ())):
                degraded_nav.append((path.name, str(obs.get("url"))[:70]))
            seen.setdefault(str(obs.get("url")), []).append(
                (_media(obs), _frames(obs)))
        for url, seq in seen.items():
            for prev, cur in zip(seq, seq[1:]):
                if cur[0] < prev[0] or cur[1] < prev[1]:
                    conflicts.append((path.name, url[:70]))
        if n_obs:
            per_archive.append((path.name, n_obs, n_unsettled))

    print(f"目录: {root}")
    print(f"存档: {len(archives)}  按 driver: {dict(by_driver)}")
    print(f"观察（带 URL 的）: {obs_total}")
    print(f"过渡态（URL 有 / title 空）: {len(unsettled)}"
          f"  ({len(unsettled) * 100 // max(obs_total, 1)}%)")
    print(f"读数打架（同 URL 后一次变少）: {len(conflicts)}")
    print(f"带 nav_unsettled 降级的观察: {len(degraded_nav)}")
    if unsettled:
        print("\n-- 过渡态明细（前 15 条）--")
        for name, url in unsettled[:15]:
            print(f"  {name}  {url}")
    if conflicts:
        print("\n-- 打架明细（前 15 条）--")
        for name, url in conflicts[:15]:
            print(f"  {name}  {url}")
    worst = sorted(per_archive, key=lambda r: -r[2])[:10]
    if worst:
        print("\n-- 过渡态最多的存档（前 10）--")
        for name, n_obs, n_bad in worst:
            print(f"  {name}  {n_bad}/{n_obs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
