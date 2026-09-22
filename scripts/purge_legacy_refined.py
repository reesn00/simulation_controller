"""purge_legacy_refined — 清空旧 4 视图 refine_data 存量.

新架构下 4 视图由 etl 阶段产出（不再由 gdr 产出）；存量 ``output/refine_data/``
下的旧文件是旧架构残留。执行此脚本一次性清空。

注意：旁路 jsonl（incomplete.jsonl / judge_low.jsonl / deferred.jsonl /
routing_low.jsonl）由 gdr 阶段产出，新架构仍由 gdr 写，**不在清理范围内**。
本脚本仅清理 ``*_refined.{messages,openai,qwenjina.txt,meta}.json``
4 视图主体文件。

用法：
    python scripts/purge_legacy_refined.py [--dry-run]
        [--refine-data-dir output/refine_data]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


_REFINED_SUFFIXES = (".messages.json", ".openai.json", ".qwenjina.txt", ".meta.json")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--refine-data-dir",
        type=Path,
        default=Path("output/refine_data"),
        help="refine_data 目录（默认 output/refine_data）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出将被删除的内容，不实际删除",
    )
    args = p.parse_args()

    target = args.refine_data_dir
    if not target.exists():
        print(f"目录不存在，无需操作：{target}")
        return 0

    # 仅匹配 ``<stem>_refined.{messages|openai|qwenjina|meta}.json`` 形态
    legacy_files = sorted(
        f for f in target.iterdir()
        if f.is_file() and any(f.name.endswith(s) for s in _REFINED_SUFFIXES)
        and "_refined" in f.name
    )
    total_bytes = sum(f.stat().st_size for f in legacy_files)
    print(f"目标目录：{target}")
    print(f"待清理旧 4 视图文件：{len(legacy_files)} 个，共 {total_bytes / 1024:.1f} KB")
    for f in legacy_files[:20]:
        print(f"  - {f.name}")
    if len(legacy_files) > 20:
        print(f"  ... 另 {len(legacy_files) - 20} 个文件")
    print()
    print("旁路 jsonl（incomplete / judge_low / deferred / routing_low）保留。")

    if args.dry_run:
        print("[dry-run] 未实际删除")
        return 0

    if legacy_files:
        resp = input(f"确认删除以上 {len(legacy_files)} 个旧 4 视图文件？[y/N] ")
        if resp.strip().lower() != "y":
            print("已取消")
            return 1
        for f in legacy_files:
            f.unlink()
        print(f"已删除 {len(legacy_files)} 个文件")
    else:
        print("无旧 4 视图文件，无需删除")
    return 0


if __name__ == "__main__":
    sys.exit(main())