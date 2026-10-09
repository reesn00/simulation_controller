"""EvalReport 数据类与序列化。"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
import json


@dataclass
class RetentionMetrics:
    """保留性: 精修后训练模型相对原始训练模型的能力保留度。

    子指标 (各 [0, 1]):
      task_completion_proxy    instruction → expected_output 的嵌入相似度均值
      tool_selection_accuracy  tool_fixer 模块正确产出 gold tool_name 的比例
      thought_fact_consistency thought_refactor 模块产出 refined_thought 的实体守恒率

    overall = 0.4 * task + 0.4 * tool + 0.2 * thought
    """
    task_completion_proxy: float = 0.0
    tool_selection_accuracy: float = 0.0
    thought_fact_consistency: float = 0.0
    overall: float = 0.0


@dataclass
class RemovalMetrics:
    """剔除性: 精修后训练模型相对原始训练模型的缺陷消除能力。

    子指标:
      broken_to_correct_recovery  给出 broken input 后, 输出与 gold output 严格匹配的比例
      noise_robustness            obs_denoiser 输出不含噪声关键词的比例
      debug_leak_suppression      obs_debug_leak 子集的不含 DEBUG 关键词比例

    overall = 0.5 * recovery + 0.3 * noise + 0.2 * debug
    """
    broken_to_correct_recovery: float = 0.0
    noise_robustness: float = 0.0
    debug_leak_suppression: float = 0.0
    overall: float = 0.0


@dataclass
class EvalReport:
    timestamp: str = ""
    eval_set_size: int = 0
    retention: RetentionMetrics = field(default_factory=RetentionMetrics)
    removal: RemovalMetrics = field(default_factory=RemovalMetrics)
    retention_orig_overall: float = 0.0  # 用于阈值判断 (refined/original 比值)
    removal_orig_overall: float = 0.0
    retention_passed: bool = False
    removal_passed: bool = False
    failing_module: str = ""
    threshold_passed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> "EvalReport":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            timestamp=data.get("timestamp", ""),
            eval_set_size=data.get("eval_set_size", 0),
            retention=RetentionMetrics(**data.get("retention", {})),
            removal=RemovalMetrics(**data.get("removal", {})),
            retention_orig_overall=data.get("retention_orig_overall", 0.0),
            removal_orig_overall=data.get("removal_orig_overall", 0.0),
            retention_passed=data.get("retention_passed", False),
            removal_passed=data.get("removal_passed", False),
            failing_module=data.get("failing_module", ""),
            threshold_passed=data.get("threshold_passed", False),
        )