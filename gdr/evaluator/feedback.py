"""反馈回路: 定位 failing module + 数据增强 + 自动重训 + 重新评估。

流程 (设计文档 §13):
  1. 读取 EvalReport (含 retention / removal 子指标)
  2. 找出最差的子维度对应的 module (thought_refactor / tool_fixer / obs_denoiser)
  3. 调用 augment_pairs_for_module() 增广该模块样本
  4. 追加到对应 jsonl 文件
  5. 自动重训 refined 探针
  6. 重新执行 dual_eval 更新 report
  5. 重复直到 threshold_passed 或达 max_feedback_iterations
"""
import logging
from pathlib import Path

from config import Settings, load_tools
from evaluator.report import EvalReport
from evaluator.probe import ProbeConfig, train_probe
from evaluator.dual_eval import run_dual_eval, HFProbeRunner
from data import augment_pairs_for_module, append_pairs

log = logging.getLogger(__name__)


def identify_failing_module(report: EvalReport) -> str:
    """启发式: 综合 retention + removal 子指标, 锁定表现最差的模块。

    返回: 'thought_refactor' | 'tool_fixer' | 'obs_denoiser' | 'all_ok'
    """
    # thought_refactor 维度: retention.thought_fact_consistency
    # tool_fixer 维度: retention.tool_selection_accuracy + removal.broken_recovery
    # obs_denoiser 维度: removal.noise_robustness + removal.debug_leak_suppression
    candidates = {
        "thought_refactor": report.retention.thought_fact_consistency,
        "tool_fixer": (report.retention.tool_selection_accuracy + report.removal.broken_to_correct_recovery) / 2,
        "obs_denoiser": (report.removal.noise_robustness + report.removal.debug_leak_suppression) / 2,
    }
    worst_name = min(candidates, key=candidates.get)
    worst_score = candidates[worst_name]

    # 阈值: 任一模块分数 < 0.5 即视为 failing
    if worst_score >= 0.5:
        return "all_ok"
    log.warning(
        "failing module=%s (score=%.3f); per-module=%s",
        worst_name, worst_score, candidates,
    )
    return worst_name


def _make_refined_probe_config(cfg: Settings) -> ProbeConfig:
    return ProbeConfig(
        base_model_name=cfg.probe_base_model_name,
        train_data_path=cfg.probe_train_data_dir / "tool_fixer.jsonl",
        lora_r=cfg.probe_lora_r,
        lora_alpha=cfg.probe_lora_alpha,
        epochs=cfg.probe_epochs,
        batch_size=cfg.probe_batch_size,
        learning_rate=cfg.probe_learning_rate,
        output_dir=cfg.evaluator_output_dir,
        probe_name="refined",
    )


def feedback_loop_iteration(
    report: EvalReport, cfg: Settings, iteration: int,
    eval_set: list[dict], runner_original,
) -> tuple[dict, EvalReport]:
    """单轮反馈: 定位 → 增广 → 重训 refined 探针 → 重新评估。

    返回 (action dict, 更新后的 report)。
    """
    module = identify_failing_module(report)
    report.failing_module = module

    if module == "all_ok":
        log.info("feedback loop: all modules above threshold, no augmentation")
        return {"action": "none", "module": "all_ok", "iteration": iteration, "augmented_pairs": 0}, report

    tool_names, hallu_apis = load_tools(cfg.tools_config_path, cfg.qwenpaw_agent_json, cfg.tool_source)
    n_extra = max(100, int(cfg.eval_set_size * 0.5))
    new_pairs = augment_pairs_for_module(module, tool_names, hallu_apis, n_extra, mode="repaired")

    target_path = cfg.probe_train_data_dir / f"{module}.jsonl"
    append_pairs(target_path, new_pairs)

    log.info(
        "feedback iteration %d: failing=%s, augmented %d pairs → %s",
        iteration, module, n_extra, target_path,
    )

    # 自动重训 refined 探针
    probe_cfg = _make_refined_probe_config(cfg)
    merged_dir = train_probe(probe_cfg)
    runner_refined = HFProbeRunner(merged_dir, cfg)

    # 重新评估并更新 report
    report = run_dual_eval(eval_set, runner_refined, runner_original, cfg)

    return {
        "action": "augment_and_retrain",
        "module": module,
        "augmented_pairs": n_extra,
        "target_train_file": str(target_path),
        "iteration": iteration,
        "merged_dir": str(merged_dir),
    }, report


def run_feedback_loop(
    cfg: Settings, report: EvalReport,
    eval_set: list[dict], runner_refined, runner_original,
) -> bool:
    """驱动整个反馈回路, 收敛条件: report.threshold_passed 或 max_feedback_iterations 触顶。

    Returns: True 表示已收敛 (含 all_ok 或达最大轮数); False 表示尚未收敛。
    """
    for it in range(1, cfg.max_feedback_iterations + 1):
        log.info("feedback loop iteration %d/%d", it, cfg.max_feedback_iterations)

        if report.threshold_passed:
            log.info("threshold passed at iteration %d, loop converges", it)
            report.failing_module = "all_ok"
            return True

        action, report = feedback_loop_iteration(report, cfg, it, eval_set, runner_original)
        log.info("iteration %d action: %s", it, action)

        if action["action"] == "none":
            report.failing_module = "all_ok"
            return True

        log.info(
            "iteration %d done: retention_passed=%s, removal_passed=%s",
            it, report.retention_passed, report.removal_passed,
        )

    log.warning(
        "feedback loop exhausted %d iterations without threshold passing; failing_module=%s",
        cfg.max_feedback_iterations, report.failing_module,
    )
    return False