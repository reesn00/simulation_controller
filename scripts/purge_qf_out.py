"""purge_qf_out — 清空旧 qf_out 目录.

新架构不再生成 ``output/qf_out/``；执行此脚本一次性清空存量。

用法：
    python scripts/purge_qf_out.py [--dry-run] [--output-dir output/qf_out]
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/qf_out"),
        help="要清空的 qf_out 目录（默认 output/qf_out）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只列出将被删除的内容，不实际删除",
    )
    args = p.parse_args()

    target = args.output_dir
    if not target.exists():
        print(f"目录不存在，无需操作：{target}")
        return 0

    files = sorted(target.rglob("*"))
    files = [f for f in files if f.is_file()]
    total_bytes = sum(f.stat().st_size for f in files)
    print(f"目标目录：{target}")
    print(f"待删除文件：{len(files)} 个，共 {total_bytes / 1024:.1f} KB")
    for f in files[:20]:
        print(f"  - {f.relative_to(target)}")
    if len(files) > 20:
        print(f"  ... 另 {len(files) - 20} 个文件")

    if args.dry_run:
        print("[dry-run] 未实际删除")
        return 0

    if files:
        resp = input(f"确认删除 {target} 全部内容？[y/N] ")
        if resp.strip().lower() != "y":
            print("已取消")
            return 1
        shutil.rmtree(target)
        print(f"已删除：{target}")
    else:
        print("目录为空，无需删除")
    return 0


if __name__ == "__main__":
    sys.exit(main())