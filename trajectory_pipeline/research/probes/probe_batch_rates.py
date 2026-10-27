"""**一批存档**的判据①（真·反爬拦截率）与判据②（``body_text``）——读存档，不联网。

这个探针回答 H-08 的尾巴：**新批次**上重取那两个数。

为什么不能直接拿 `cli report` 的数：它给的是**合并口径**的拦截率
（反爬 + 登录墙），而 §6.1 明确要求**真·反爬**口径——实测两个口径
会给出**相反**的结论（A 19.15% / B 34.09% 合并口径看着 B 更差，
拆开看真·反爬是 A 14.89% / B 9.09%）。

⚠️ **它按「面」筛，不按 ``driver`` 字段筛**：存量 obscura 批次**没记
``driver``**（该字段 2026-10-22 才进 P1），而它们真实跑的就是 obscura
⇒ 按字段筛会把存量批次全筛掉。两代混在一起取数是最坏的一种数
（既不是旧批次的数，也不是新批次的数）。

⚠️ **它只报读数，不下结论**——「结论对不对」不在门禁里（CLAUDE.md）。

⚠️ **``--surface`` 的默认值是 ``camoufox-1``**——那是**盘上存量批次**的面
（camoufox-1 已于 2026-10-26 退役，**只读不再产出**）；**现行生产面是
``camofox-1``**。读新批次要显式点名 ``--surface camofox-1``。

用法（**必须在仓库根跑**）::

    python trajectory_pipeline/research/probes/probe_batch_rates.py \
        --label 修复前=trajectory_pipeline/output/pipeline --surface camoufox-1 \
        --label 修复后=trajectory_pipeline/output/pipeline/camoufox_prod2 --surface camoufox-1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trajectory_pipeline.executor import dual_run


def _utf8_console() -> None:
    """把标准输出改成 utf-8（Windows 控制台默认 GBK：中文花屏、``⇒`` 直接抛）。

    探针的读数是要给人看的，打不出来就等于没量。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):    # pragma: no cover - 老解释器
            pass


def _one(label: str, path: Path, surface: str) -> dict:
    """一个目录的读数。**面筛在 summarize 里做**（口径只有一份）。"""
    s = dual_run.summarize(label, path, surface=surface)
    return {
        "label": label,
        "path": str(path),
        "surface": surface,
        "runs": s.runs,
        "drivers": dict(s.drivers),
        "observations": s.observations,
        "outcomes_total": s.outcomes_total,
        # ── 判据①：拦截率三个口径 ──
        "antibot_rate": s.antibot_rate,      # 真·反爬（反爬 + 登录墙分开算）
        "loginwall_rate": s.loginwall_rate,  # 登录墙（不是反爬）
        "block_rate": s.block_rate,          # 合并口径（§6.1 说它会骗人）
        "antibot_outcomes": s.antibot_outcomes,
        "loginwall_outcomes": s.loginwall_outcomes,
        "blocked_outcomes": s.blocked_outcomes,
        "search_blocked_runs": s.search_blocked_runs,
        "branch_counts": dict(s.branch_counts),
        # ── 判据②：正文长度 ──
        "body_len_median": s.body_len_median,
        "body_len_median_by_family": dict(s.body_len_median_by_family),
        "observations_by_family": dict(s.observations_by_family),
        "elements_median": s.elements_median,
    }


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(description="一批存档的判据①/② 读数")
    ap.add_argument("--label", action="append", default=[],
                    help="``名字=目录``（可多次）。例：``修复后=output/pipeline/camoufox_prod2``")
    ap.add_argument("--surface", default="camoufox-1",
                    help="只统计这个面的存档（默认 camoufox-1）。"
                         "⚠️ **默认值指的是盘上存量批次**（camoufox-1 是"
                         "**已退役**的只读面）；现行生产面是 **camofox-1**，"
                         "读新批次要显式点名它")
    ap.add_argument("--out", default="", help="把读数写成 JSON")
    args = ap.parse_args(argv)
    if not args.label:
        ap.error("至少给一个 --label")

    rows = []
    for item in args.label:
        name, _, raw = item.partition("=")
        path = Path(raw or name)
        if not path.exists():
            print(f"[skip] 目录不存在: {path}")
            continue
        row = _one(name or str(path), path, args.surface)
        rows.append(row)

    for row in rows:
        print(json.dumps(row, ensure_ascii=False, indent=2))
        print()

    if len(rows) >= 2:
        print("-- 差值（后者 − 前者）--")
        a, b = rows[0], rows[-1]
        for key in ("antibot_rate", "loginwall_rate", "block_rate", "body_len_median"):
            va, vb = a.get(key), b.get(key)
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                print(f"  {key:<20} {va:>10.2f} → {vb:>10.2f}  ({vb - va:+.2f})")
            else:
                print(f"  {key:<20} {str(va):>10} → {str(vb):>10}")
        print("  ⚠️ **两批看的不是同一批页面**（站点会漂）⇒ 差值不可直接当因果。")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
        print(f"已写 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
