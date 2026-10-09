"""gdr/refiners/retry_loop_prompt_tune: 命令行运行 prompt 调优评估.

用法:
    # 1. 用默认 settings (LLM 来自 cfg) 跑评估
    uv run python -m gdr.refiners.retry_loop_prompt_tune

    # 2. 指定输出文件
    uv run python -m gdr.refiners.retry_loop_prompt_tune \\
        --output output/prompt_eval_report.md

    # 3. 仅用某类别样本
    uv run python -m gdr.refiners.retry_loop_prompt_tune \\
        --category clear_retry

工作流:
    1. 跑本脚本得到 baseline 准确率
    2. 改 retry_loop_clip.py::_CLIP_PROMPT
    3. 重跑本脚本, 对比准确率与错判样本
    4. 迭代直到准确率达目标 (≥ 90%)

输出:
    Markdown 报告写入 --output, 默认 stdout.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from gdr.refiners.retry_loop_ground_truth import GROUND_TRUTH_SAMPLES, samples_by_category
from gdr.refiners.retry_loop_prompt_eval import evaluate_prompt, format_report

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Retry loop clip prompt evaluation")
    p.add_argument("--output", type=Path, default=None,
                   help="报告输出文件路径 (默认 stdout)")
    p.add_argument("--category", type=str, default=None,
                   choices=["clear_retry", "not_retry", "edge_case", "real_world"],
                   help="仅评估某一类别")
    args = p.parse_args(argv)

    samples = (
        samples_by_category(args.category)
        if args.category
        else GROUND_TRUTH_SAMPLES
    )

    # 构造 LLM 客户端 (复用 cfg.main_model + cfg)
    try:
        from infrastructure import LlamaCppClient
        from config import Settings
        cfg = Settings()
    except Exception as e:
        print(f"无法加载 Settings: {e}", file=sys.stderr)
        return 1

    client = LlamaCppClient.get(
        cfg.main_model, cfg=cfg,
        timeout=int(getattr(cfg, "retry_loop_clip_llm_timeout_s", 60)),
    )

    result = evaluate_prompt(samples, client)
    report = format_report(result)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        print(f"报告已写入: {args.output}", file=sys.stderr)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
