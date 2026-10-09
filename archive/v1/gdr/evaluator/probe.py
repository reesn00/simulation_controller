"""LoRA SFT 训练探针模型 (probe_refined / probe_original)。

依赖 ([sft] 可选): torch, transformers, peft, trl, datasets
缺失时给出明确错误信息, 不会静默失败。
"""
import json
import logging
from pathlib import Path
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class ProbeConfig:
    base_model_name: str = "Qwen/Qwen3.5-9B-Instruct"
    train_data_path: Path = Path("./data/sft_pairs.jsonl")
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    epochs: int = 3
    batch_size: int = 4
    learning_rate: float = 2e-4
    max_seq_len: int = 2048
    output_dir: Path = Path("./probe_out")
    probe_name: str = "probe"  # 'refined' or 'original'
    target_modules: tuple = ("q_proj", "k_proj", "v_proj", "o_proj")


def format_sample(sample: dict) -> dict:
    """Qwen 风格的 user/assistant 模板, 渲染为 SFTTrainer 期望的 text 字段。"""
    module = sample.get("module", "")
    defect = sample.get("defect_type", "")
    inp = json.dumps(sample.get("input", {}), ensure_ascii=False)
    out = json.dumps(sample.get("output", {}), ensure_ascii=False)
    instruction = (
        f"你是 GDR Agent 数据精修器的探针模型。\n"
        f"针对模块 {module} 的缺陷 {defect}, 输入:\n{inp}\n"
        f"请输出精修后的 JSON:"
    )
    text = (
        f"<|im_start|>user\n{instruction}<|im_end|>\n"
        f"<|im_start|>assistant\n{out}<|im_end|>"
    )
    return {"text": text}


def load_pairs(path: Path) -> list[dict]:
    pairs = []
    if not path.exists():
        raise FileNotFoundError(f"train data not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                pairs.append(json.loads(line))
    return pairs


def train_probe(cfg: ProbeConfig) -> Path:
    """LoRA SFT 训练。返回 merged model 目录路径 (供后续 gguf 导出或 HF 直接推理)。"""
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from datasets import Dataset
        from peft import LoraConfig
        from trl import SFTTrainer, SFTConfig
    except ImportError as e:
        raise ImportError(
            "probe training requires [sft] extras: "
            "pip install '.[sft]' (transformers, peft, trl, datasets, torch)"
        ) from e

    log.info("loading base model %s", cfg.base_model_name)
    tokenizer = AutoTokenizer.from_pretrained(cfg.base_model_name, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = AutoModelForCausalLM.from_pretrained(
        cfg.base_model_name,
        torch_dtype=torch.float16,
        device_map="auto",
        trust_remote_code=True,
    )

    lora_config = LoraConfig(
        r=cfg.lora_r,
        lora_alpha=cfg.lora_alpha,
        lora_dropout=cfg.lora_dropout,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=list(cfg.target_modules),
    )

    pairs = load_pairs(cfg.train_data_path)
    log.info("loaded %d pairs from %s", len(pairs), cfg.train_data_path)
    dataset = Dataset.from_list([format_sample(p) for p in pairs])

    output_dir = cfg.output_dir / cfg.probe_name
    output_dir.mkdir(parents=True, exist_ok=True)

    sft_config = SFTConfig(
        output_dir=str(output_dir),
        num_train_epochs=cfg.epochs,
        per_device_train_batch_size=cfg.batch_size,
        learning_rate=cfg.learning_rate,
        max_length=cfg.max_seq_len,
        save_strategy="epoch",
        logging_steps=10,
        report_to="none",
        bf16=False,
        fp16=True,
        gradient_checkpointing=True,
    )

    trainer = SFTTrainer(
        model=model,
        args=sft_config,
        train_dataset=dataset,
        peft_config=lora_config,
        processing_class=tokenizer,
    )
    log.info("starting SFT training: epochs=%d, lr=%.2e", cfg.epochs, cfg.learning_rate)
    trainer.train()

    log.info("merging LoRA into base for downstream export")
    merged = trainer.model.merge_and_unload()
    merged_dir = output_dir / "merged"
    merged_dir.mkdir(exist_ok=True)
    merged.save_pretrained(str(merged_dir), safe_serialization=True)
    tokenizer.save_pretrained(str(merged_dir))
    log.info("merged probe saved to %s", merged_dir)
    return merged_dir