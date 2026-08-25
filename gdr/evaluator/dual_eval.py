"""双维评测 (设计文档 §13)。

保留性 (retention):
  - task_completion_proxy    instruction→output 嵌入相似度均值
  - tool_selection_accuracy  tool_fixer 模块正确产出 gold tool_name 比例
  - thought_fact_consistency thought_refactor 模块 refined_thought 实体守恒率
  阈值: refined.overall / original.overall ≥ cfg.retention_threshold

剔除性 (removal):
  - broken_to_correct_recovery  给出 broken input 时, 输出与 gold 严格匹配比例
  - noise_robustness            obs_denoiser 输出不含 DEBUG/Traceback 关键词比例
  - debug_leak_suppression      obs_debug_leak 子集的不含 DEBUG 比例
  阈值: refined.overall - original.overall ≥ cfg.removal_threshold
"""
import json
import re
import logging
from typing import Protocol
from pathlib import Path
from datetime import datetime, timezone

from evaluator.report import EvalReport, RetentionMetrics, RemovalMetrics

log = logging.getLogger(__name__)


# === ProbeRunner 接口 + 三种实现 ===

class ProbeRunner(Protocol):
    def run(self, instruction: str) -> str: ...


class MockProbeRunner:
    """确定性探针, 仅供 smoke test 使用 (无需模型)。

    refined 模式: 模拟正确修复 (产出 gold-style 输出)
    original 模式: 模拟保留原缺陷 (产出 broken-style 输出)
    """
    def __init__(self, mode: str = "refined"):
        self.mode = mode
        self.call_count = 0

    def run(self, instruction: str) -> str:
        self.call_count += 1
        if self.mode == "refined":
            if "tool_fixer" in instruction:
                return json.dumps({"name": "browser", "input": "{\"url\": \"https://example.com\"}"})
            elif "thought_refactor" in instruction:
                return json.dumps({"refined_thought": "我决定调用 browser 工具访问页面, 因为这是最直接的路径。"})
            elif "obs_denoiser" in instruction:
                return json.dumps({"output_text": "页面加载成功, 包含目标信息。"})
        else:  # original
            if "tool_fixer" in instruction:
                return json.dumps({"name": "tavily_search", "input": "broken"})
            elif "thought_refactor" in instruction:
                return json.dumps({"refined_thought": "好"})
            elif "obs_denoiser" in instruction:
                return json.dumps({"output_text": "[DEBUG] trace\nTraceback ...\n[API_MISUSE]"})
        return "{}"


class LlamaCppProbeRunner:
    """通过 LlamaCppClient 跑 gguf 格式探针。"""
    def __init__(self, gguf_path: Path, cfg):
        from infrastructure import LlamaCppClient
        # Accept either a gguf path or a model name. If a path is supplied, fall back to
        # the configured main_model because the HTTP backend does not need a path.
        model = str(gguf_path)
        if model.endswith(".gguf") or "/" in model or "\\" in model:
            from pathlib import Path
            model = Path(model).stem or cfg.main_model
        self.client = LlamaCppClient.get(model, cfg=cfg, timeout_s=cfg.llm_timeout_s)
        self.cfg = cfg

    def run(self, instruction: str) -> str:
        prompt = (
            f"<|im_start|>user\n你是 GDR 探针模型。\n{instruction}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        text, _ = self.client.generate(
            prompt, max_tokens=512, timeout_s=self.cfg.llm_timeout_s,
        )
        return text


class HFProbeRunner:
    """通过 transformers 跑 HF 格式探针 (合并后的)。"""
    def __init__(self, hf_model_dir: Path, cfg):
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as e:
            raise ImportError("HF probe runner requires transformers + torch") from e
        self.tokenizer = AutoTokenizer.from_pretrained(str(hf_model_dir), trust_remote_code=True)
        self.model = AutoModelForCausalLM.from_pretrained(
            str(hf_model_dir),
            torch_dtype=torch.float16,
            device_map="auto",
            trust_remote_code=True,
        )
        self.cfg = cfg

    def run(self, instruction: str) -> str:
        import torch
        prompt = (
            f"<|im_start|>user\n你是 GDR 探针模型。\n{instruction}<|im_end|>\n"
            f"<|im_start|>assistant\n"
        )
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        with torch.no_grad():
            out = self.model.generate(
                **inputs,
                max_new_tokens=512,
                do_sample=False,
                pad_token_id=self.tokenizer.eos_token_id,
            )
        text = self.tokenizer.decode(
            out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True,
        )
        return text


# === 子指标计算 ===

_NOISE_PATTERN = re.compile(r"DEBUG|Traceback|\[API_MISUSE\]|FATAL|ModuleNotFoundError")


def _extract_entities(text: str) -> set[str]:
    entities = set()
    for m in re.finditer(r'"([^"]+)"', text):
        entities.add(m.group(1))
    for m in re.finditer(r"'([^']+)'", text):
        entities.add(m.group(1))
    for m in re.finditer(r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', text):
        entities.add(m.group(1))
    return entities


def _make_instruction(sample: dict) -> str:
    return json.dumps(sample.get("input", {}), ensure_ascii=False)


def _make_gold_str(sample: dict) -> str:
    return json.dumps(sample.get("output", {}), ensure_ascii=False, sort_keys=True)


def _compute_task_completion_proxy(eval_set: list[dict], runner: ProbeRunner) -> float:
    """嵌入相似度均值 (sentence-transformers, BGE-M3)。"""
    try:
        from sentence_transformers import SentenceTransformer
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError:
        log.warning("sentence-transformers not available, task_completion_proxy=0")
        return 0.0
    model = SentenceTransformer("BAAI/bge-m3")
    sims = []
    for s in eval_set:
        gold = _make_gold_str(s)
        instr = _make_instruction(s)
        out = runner.run(instr)
        emb = model.encode([gold, out], normalize_embeddings=True)
        sims.append(float(cosine_similarity([emb[0]], [emb[1]])[0][0]))
    return sum(sims) / max(len(sims), 1)


def _compute_tool_selection_accuracy(eval_set: list[dict], runner: ProbeRunner) -> float:
    pairs = [s for s in eval_set if s.get("module") == "tool_fixer"]
    if not pairs:
        return 1.0
    correct = 0
    for s in pairs:
        gold_name = s.get("output", {}).get("name", "")
        out = runner.run(_make_instruction(s))
        try:
            if json.loads(out).get("name") == gold_name:
                correct += 1
        except Exception:
            pass
    return correct / len(pairs)


def _compute_thought_fact_consistency(eval_set: list[dict], runner: ProbeRunner) -> float:
    pairs = [s for s in eval_set if s.get("module") == "thought_refactor"]
    if not pairs:
        return 1.0
    consistent = 0
    for s in pairs:
        orig = s.get("input", {}).get("thinking", "")
        out = runner.run(_make_instruction(s))
        try:
            refined = json.loads(out).get("refined_thought", "")
            if not orig or _extract_entities(orig).issubset(_extract_entities(refined)):
                consistent += 1
        except Exception:
            pass
    return consistent / len(pairs)


def _compute_broken_recovery(eval_set: list[dict], runner: ProbeRunner) -> float:
    if not eval_set:
        return 0.0
    correct = 0
    for s in eval_set:
        gold = _make_gold_str(s)
        out = runner.run(_make_instruction(s))
        if out.strip() == gold.strip():
            correct += 1
    return correct / len(eval_set)


def _compute_noise_robustness(eval_set: list[dict], runner: ProbeRunner) -> float:
    pairs = [s for s in eval_set if s.get("module") == "obs_denoiser"]
    if not pairs:
        return 1.0
    clean = 0
    for s in pairs:
        out = runner.run(_make_instruction(s))
        if not _NOISE_PATTERN.search(out):
            clean += 1
    return clean / len(pairs)


def _compute_debug_leak_suppression(eval_set: list[dict], runner: ProbeRunner) -> float:
    pairs = [s for s in eval_set
             if s.get("module") == "obs_denoiser" and s.get("defect_type") == "obs_debug_leak"]
    if not pairs:
        return 1.0
    clean = 0
    for s in pairs:
        out = runner.run(_make_instruction(s))
        if not _NOISE_PATTERN.search(out):
            clean += 1
    return clean / len(pairs)


def _aggregate_retention(eval_set: list[dict], runner: ProbeRunner) -> RetentionMetrics:
    m = RetentionMetrics(
        task_completion_proxy=_compute_task_completion_proxy(eval_set, runner),
        tool_selection_accuracy=_compute_tool_selection_accuracy(eval_set, runner),
        thought_fact_consistency=_compute_thought_fact_consistency(eval_set, runner),
    )
    m.overall = m.task_completion_proxy * 0.4 + m.tool_selection_accuracy * 0.4 + m.thought_fact_consistency * 0.2
    return m


def _aggregate_removal(eval_set: list[dict], runner: ProbeRunner) -> RemovalMetrics:
    m = RemovalMetrics(
        broken_to_correct_recovery=_compute_broken_recovery(eval_set, runner),
        noise_robustness=_compute_noise_robustness(eval_set, runner),
        debug_leak_suppression=_compute_debug_leak_suppression(eval_set, runner),
    )
    m.overall = (
        m.broken_to_correct_recovery * 0.5
        + m.noise_robustness * 0.3
        + m.debug_leak_suppression * 0.2
    )
    return m


# === 主入口 ===

def run_dual_eval(
    eval_set: list[dict],
    runner_refined: ProbeRunner,
    runner_original: ProbeRunner,
    cfg,
) -> EvalReport:
    """对每个 runner 都跑一遍 6 个子指标, 然后按阈值规则判定。

    retention 通过: refined.overall / original.overall ≥ cfg.retention_threshold
    removal 通过:   refined.overall - original.overall ≥ cfg.removal_threshold
    """
    log.info("running dual evaluation on %d samples", len(eval_set))

    retention = _aggregate_retention(eval_set, runner_refined)
    removal = _aggregate_removal(eval_set, runner_refined)

    retention_orig = _aggregate_retention(eval_set, runner_original)
    removal_orig = _aggregate_removal(eval_set, runner_original)

    retention_ratio = retention.overall / max(retention_orig.overall, 1e-6)
    retention_passed = retention_ratio >= cfg.retention_threshold

    removal_improvement = removal.overall - removal_orig.overall
    removal_passed = removal_improvement >= cfg.removal_threshold

    report = EvalReport(
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        eval_set_size=len(eval_set),
        retention=retention,
        removal=removal,
        retention_orig_overall=round(retention_orig.overall, 4),
        removal_orig_overall=round(removal_orig.overall, 4),
        retention_passed=retention_passed,
        removal_passed=removal_passed,
        failing_module="",  # 由 feedback step 填充
        threshold_passed=retention_passed and removal_passed,
    )

    log.info(
        "eval done: retention=%.3f (orig=%.3f, ratio=%.3f, threshold=%.2f, passed=%s), "
        "removal=%.3f (orig=%.3f, improvement=%+.3f, threshold=%.2f, passed=%s)",
        retention.overall, retention_orig.overall, retention_ratio,
        cfg.retention_threshold, retention_passed,
        removal.overall, removal_orig.overall, removal_improvement,
        cfg.removal_threshold, removal_passed,
    )
    return report