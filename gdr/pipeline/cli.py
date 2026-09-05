"""pipeline CLI: argparse 入口。"""
import argparse
import logging
from pathlib import Path

from config import Settings
from infrastructure import setup_logger
from pipeline.runner import run

log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="GDR Agent 数据精修流水线")
    parser.add_argument("--input", type=Path, help="输入 trajectory JSONL 文件路径 (单文件模式)")
    parser.add_argument("--output", type=Path, help="输出 JSON 文件路径 (单文件模式)")
    parser.add_argument("--batch-input-dir", type=Path, help="批量输入目录 (扫描 *.jsonl)")
    parser.add_argument("--batch-output-dir", type=Path, help="批量输出目录")
    parser.add_argument("--workers", type=int, help="并行 worker 数 (1=顺序)")
    parser.add_argument("--max-files", type=int, help="限制处理的输入文件数量 (默认全部)")
    parser.add_argument("--tools-config", type=Path, help="tools.yaml 路径")
    parser.add_argument("--log-dir", type=Path, help="日志目录")
    args = parser.parse_args()

    cfg = Settings()
    if args.input:
        cfg.input_path = args.input
    if args.output:
        cfg.output_path = args.output
    if args.batch_input_dir:
        cfg.batch_input_dir = args.batch_input_dir
    if args.batch_output_dir:
        cfg.batch_output_dir = args.batch_output_dir
    elif args.batch_input_dir and not cfg.batch_output_dir:
        cfg.batch_output_dir = args.batch_input_dir / "refine_data"
    if args.workers is not None:
        cfg.workers = args.workers
    if args.max_files is not None:
        cfg.max_files = args.max_files
    if args.tools_config:
        cfg.tools_config_path = args.tools_config
    if args.log_dir:
        cfg.log_dir = args.log_dir

    setup_logger(cfg.log_dir)
    log.info(
        "GDR pipeline starting: input=%s, batch_dir=%s, output=%s, workers=%d",
        cfg.input_path, cfg.batch_input_dir, cfg.output_path, cfg.workers,
    )

    stats = run(cfg)
    log.info("GDR pipeline finished: %s", stats)


if __name__ == "__main__":
    main()