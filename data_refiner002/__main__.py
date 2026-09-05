"""CLI 入口: uv run python -m data_refiner [--input DIR] [--output DIR]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from refiner.runner import run  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="data_refiner", description="agent 合成数据剪裁")
    parser.add_argument("--input", default="data_refiner/input_data", help="输入目录")
    parser.add_argument("--output", default="data_refiner/output_data", help="输出目录")
    parser.add_argument("--log-dir", default="data_refiner/logs", help="日志目录")
    parser.add_argument(
        "--thinking-min",
        type=int,
        default=20,
        help="thinking 过短阈值（字符数，含），默认 20",
    )
    parser.add_argument(
        "--thinking-max",
        type=int,
        default=4000,
        help="thinking 过长阈值（字符数，含），默认 4000",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最多处理的文件数量（按文件名顺序），默认全部处理",
    )
    parser.add_argument("--dry-run", action="store_true", help="只分析不写剪裁结果文件")
    args = parser.parse_args(argv)

    base = Path.cwd()
    input_dir = Path(args.input)
    output_dir = Path(args.output)
    log_dir = Path(args.log_dir)
    if not input_dir.is_absolute():
        input_dir = base / input_dir
    if not output_dir.is_absolute():
        output_dir = base / output_dir
    if not log_dir.is_absolute():
        log_dir = base / log_dir

    summary = run(
        input_dir=input_dir,
        output_dir=output_dir,
        log_dir=log_dir,
        thinking_min_chars=args.thinking_min,
        thinking_max_chars=args.thinking_max,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    print(f"处理完成: {summary['total_files']} 个文件, 无效 {summary['invalid_files']} 个, "
          f"剪裁 {summary['trimmed_files']} 个, 无变化 {summary['unchanged_files']} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
