"""evaluator CLI: 子命令入口。

子命令:
  build-pairs        程序化生成训练对 (按 module 分桶)
  train-probe        LoRA SFT 训练单个探针 (refined | original)
  evaluate           双维评测 (probe_refined vs probe_original)
  feedback-loop      执行反馈回路直到收敛
  full               一站式: build → train ×2 → evaluate → feedback

示例:
  python -m evaluator.cli build-pairs --n-tool 200 --n-thought 200 --n-obs 200
  python -m evaluator.cli train-probe --train-data ./data/sft_pairs/tool_fixer.jsonl \\
      --output-dir ./probe_out --probe-name refined
  python -m evaluator.cli evaluate --runner-refined mock_refined --runner-original mock_original
  python -m evaluator.cli feedback-loop --report ./evaluator_output/report.json
"""
import argparse
import json
import logging
import sys
from pathlib import Path

from config import Settings, load_tools
from evaluator.report import EvalReport
from evaluator.probe import ProbeConfig, train_probe
from evaluator.dual_eval import (
    LlamaCppProbeRunner, HFProbeRunner, MockProbeRunner, run_dual_eval,
)
from evaluator.feedback import run_feedback_loop
from data import build_all_pairs, save_pairs_by_module

log = logging.getLogger(__name__)


def _load_eval_set(path: Path) -> list[dict]:
    items = []
    if not path.exists():
        return items
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def _make_runner(kind: str, path: Path | None, cfg: Settings):
    if kind == "mock_refined":
        return MockProbeRunner(mode="refined")
    if kind == "mock_original":
        return MockProbeRunner(mode="original")
    if path is None:
        raise ValueError(f"--probe-* path required for runner kind={kind}")
    if kind == "gguf":
        return LlamaCppProbeRunner(path, cfg)
    if kind == "hf":
        return HFProbeRunner(path, cfg)
    raise ValueError(f"unknown runner kind={kind}")


# === 子命令实现 ===

def cmd_build_pairs(args, cfg: Settings) -> None:
    tools, hallu = load_tools(cfg.tools_config_path, cfg.qwenpaw_agent_json, cfg.tool_source)
    pairs = build_all_pairs(
        tools, hallu,
        n_tool=args.n_tool, n_thought=args.n_thought, n_obs=args.n_obs,
    )
    out_paths = save_pairs_by_module(pairs, cfg.probe_train_data_dir)
    for m, p in out_paths.items():
        log.info("  %s: %s (%d pairs)", m, p, len(pairs[m]))
    print(json.dumps(
        {m: str(p) for m, p in out_paths.items()},
        ensure_ascii=False, indent=2,
    ))


def cmd_train_probe(args, cfg: Settings) -> None:
    probe_cfg = ProbeConfig(
        base_model_name=args.base_model or cfg.probe_base_model_name,
        train_data_path=args.train_data,
        lora_r=cfg.probe_lora_r,
        lora_alpha=cfg.probe_lora_alpha,
        epochs=cfg.probe_epochs,
        batch_size=cfg.probe_batch_size,
        learning_rate=cfg.probe_learning_rate,
        output_dir=args.output_dir,
        probe_name=args.probe_name,
    )
    out = train_probe(probe_cfg)
    log.info("probe %s merged dir: %s", args.probe_name, out)
    print(str(out))


def cmd_evaluate(args, cfg: Settings) -> None:
    eval_set = _load_eval_set(args.eval_set or cfg.eval_set_path)
    if not eval_set:
        log.error("eval set empty, abort (run build-pairs first or pass --eval-set)")
        sys.exit(1)

    runner_refined = _make_runner(args.runner_refined, args.probe_refined, cfg)
    runner_original = _make_runner(args.runner_original, args.probe_original, cfg)

    report = run_dual_eval(eval_set, runner_refined, runner_original, cfg)

    out_path = args.output or cfg.evaluator_output_dir / "report.json"
    report.save(out_path)
    log.info("report saved to %s", out_path)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


def cmd_feedback_loop(args, cfg: Settings) -> None:
    report_path = args.report or cfg.evaluator_output_dir / "report.json"
    if not report_path.exists():
        log.error("report not found: %s (run evaluate first)", report_path)
        sys.exit(1)
    report = EvalReport.load(report_path)

    eval_set = _load_eval_set(args.eval_set or cfg.eval_set_path)
    if not eval_set:
        log.error("eval set empty, abort (run build-pairs first or pass --eval-set)")
        sys.exit(1)

    runner_original = _make_runner(args.runner_original, args.probe_original, cfg)
    runner_refined = _make_runner(args.runner_refined, args.probe_refined, cfg)

    converged = run_feedback_loop(cfg, report, eval_set, runner_refined, runner_original)
    report.save(report_path)
    log.info("feedback loop converged=%s", converged)
    print(json.dumps({
        "converged": converged,
        "failing_module": report.failing_module,
        "threshold_passed": report.threshold_passed,
    }, ensure_ascii=False, indent=2))
    sys.exit(0 if converged else 2)


def cmd_full(args, cfg: Settings) -> None:
    """一站式: build-pairs → train ×2 → evaluate → feedback-loop。"""
    log.info("=== full pipeline: build-pairs ===")
    cmd_build_pairs(args, cfg)

    log.info("=== full pipeline: train refined probe ===")
    cmd_train_probe(argparse.Namespace(
        base_model=None,
        train_data=cfg.probe_train_data_dir / "tool_fixer.jsonl",
        output_dir=cfg.evaluator_output_dir,
        probe_name="refined",
    ), cfg)

    log.info("=== full pipeline: train original probe ===")
    # Original probe 必须保留原始缺陷分布，使用 *_broken.jsonl 训练对
    cmd_train_probe(argparse.Namespace(
        base_model=None,
        train_data=cfg.probe_train_data_dir / "tool_fixer_broken.jsonl",
        output_dir=cfg.evaluator_output_dir,
        probe_name="original",
    ), cfg)

    log.info("=== full pipeline: evaluate ===")
    cmd_evaluate(args, cfg)

    log.info("=== full pipeline: feedback-loop ===")
    cmd_feedback_loop(argparse.Namespace(report=None), cfg)


# === 入口 ===

def main():
    parser = argparse.ArgumentParser(description="GDR 评估器 (设计文档 §13)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build-pairs", help="程序化生成 SFT 训练对 (按 module 分桶)")
    p.add_argument("--n-tool", type=int, default=200)
    p.add_argument("--n-thought", type=int, default=200)
    p.add_argument("--n-obs", type=int, default=200)

    p = sub.add_parser("train-probe", help="LoRA SFT 训练单个探针")
    p.add_argument("--base-model", help="HF repo 名 (默认走 cfg.probe_base_model_name)")
    p.add_argument("--train-data", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--probe-name", required=True, choices=["refined", "original"])

    p = sub.add_parser("evaluate", help="双维评测")
    p.add_argument("--eval-set", type=Path, help="eval JSONL (默认 cfg.eval_set_path)")
    p.add_argument("--probe-refined", type=Path, help="refined 探针路径 (gguf 或 HF dir)")
    p.add_argument("--probe-original", type=Path, help="original 探针路径 (gguf 或 HF dir)")
    p.add_argument("--runner-refined", default="mock_refined",
                   choices=["mock_refined", "gguf", "hf"])
    p.add_argument("--runner-original", default="mock_original",
                   choices=["mock_original", "gguf", "hf"])
    p.add_argument("--output", type=Path, help="report 输出路径")

    p = sub.add_parser("feedback-loop", help="执行反馈回路直到收敛")
    p.add_argument("--report", type=Path, help="report JSON 路径 (默认 cfg.evaluator_output_dir/report.json)")
    p.add_argument("--eval-set", type=Path, help="eval JSONL (默认 cfg.eval_set_path)")
    p.add_argument("--probe-refined", type=Path, help="refined 探针路径 (gguf 或 HF dir)")
    p.add_argument("--probe-original", type=Path, help="original 探针路径 (gguf 或 HF dir)")
    p.add_argument("--runner-refined", default="mock_refined",
                   choices=["mock_refined", "gguf", "hf"])
    p.add_argument("--runner-original", default="mock_original",
                   choices=["mock_original", "gguf", "hf"])

    p = sub.add_parser("full", help="build → train ×2 → evaluate → feedback")
    p.add_argument("--n-tool", type=int, default=200)
    p.add_argument("--n-thought", type=int, default=200)
    p.add_argument("--n-obs", type=int, default=200)
    p.add_argument("--eval-set", type=Path, help="eval JSONL (默认 cfg.eval_set_path)")
    p.add_argument("--probe-refined", type=Path)
    p.add_argument("--probe-original", type=Path)
    p.add_argument("--runner-refined", default="mock_refined",
                   choices=["mock_refined", "gguf", "hf"])
    p.add_argument("--runner-original", default="mock_original",
                   choices=["mock_original", "gguf", "hf"])
    p.add_argument("--output", type=Path)

    args = parser.parse_args()

    cfg = Settings()
    from infrastructure import setup_logger
    setup_logger(cfg.log_dir)

    log.info("evaluator CLI starting: cmd=%s", args.cmd)

    if args.cmd == "build-pairs":
        cmd_build_pairs(args, cfg)
    elif args.cmd == "train-probe":
        cmd_train_probe(args, cfg)
    elif args.cmd == "evaluate":
        cmd_evaluate(args, cfg)
    elif args.cmd == "feedback-loop":
        cmd_feedback_loop(args, cfg)
    elif args.cmd == "full":
        cmd_full(args, cfg)


if __name__ == "__main__":
    main()