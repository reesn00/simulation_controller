"""evaluator: 效用评估闭环 (设计文档 §13)。

子模块:
  probe        LoRA SFT 训练探针模型
  dual_eval    双维 (保留性 / 剔除性) 评测
  feedback     反馈回路: 定位 failing module + 数据增强 + 重训
  report       EvalReport 数据类与序列化
  cli          命令行入口 (build-pairs / train-probe / evaluate / feedback-loop / full)

依赖 (pyproject [sft] 可选):
  torch, transformers, peft, trl, datasets, accelerate
"""
from evaluator.report import EvalReport, RetentionMetrics, RemovalMetrics
from evaluator.probe import ProbeConfig, train_probe
from evaluator.dual_eval import (
    ProbeRunner,
    LlamaCppProbeRunner, HFProbeRunner, MockProbeRunner,
    run_dual_eval,
)
from evaluator.feedback import (
    identify_failing_module,
    feedback_loop_iteration,
    run_feedback_loop,
)

__all__ = [
    "EvalReport", "RetentionMetrics", "RemovalMetrics",
    "ProbeConfig", "train_probe",
    "ProbeRunner", "LlamaCppProbeRunner", "HFProbeRunner", "MockProbeRunner",
    "run_dual_eval",
    "identify_failing_module", "feedback_loop_iteration", "run_feedback_loop",
]