#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Unsloth 对 Qwen3.5 做 LoRA SFT，训练 export_qf_sft.py --mode unsloth 导出的数据。

依据 https://unsloth.ai/docs/models/qwen3.5/fine-tune：
- Qwen3.5 需要 transformers v5；稠密模型用 FastLanguageModel，MoE 用 FastModel
- Qwen3.5 不推荐 load_in_4bit（QLoRA），用 load_in_16bit=True
- 数据集为单列 text；用 train_on_responses_only 只对 assistant 回复计算损失

用法:
    python scripts/export_qf_sft/train_unsloth.py \
        --data scripts/export_qf_sft/output/*.sft_unsloth.jsonl
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

MAX_SEQ_LENGTH = 8192  # agent 轨迹较长，至少 8k；OOM 时降为 4096


def main() -> None:
    parser = argparse.ArgumentParser(description="Unsloth Qwen3.5 LoRA SFT")
    parser.add_argument("--data", nargs="+", required=True, help="unsloth 模式导出的 jsonl（支持 glob）")
    parser.add_argument("--model", default="unsloth/Qwen3.5-4B",
                        help="模型名（unsloth/Qwen3.5-4B、Qwen/Qwen3.5-9B 等；MoE 用 --moe）")
    parser.add_argument("--moe", action="store_true", help="MoE 模型时使用 FastModel（如 35B-A3B）")
    parser.add_argument("--full-finetune", action="store_true", help="全量微调（显存约为 LoRA 的 4 倍）")
    parser.add_argument("--output", default="qwen35_sft_lora", help="LoRA 适配器输出目录")
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=4)
    args = parser.parse_args()

    from unsloth import FastLanguageModel, FastModel
    import torch
    from datasets import load_dataset
    from trl import SFTConfig, SFTTrainer
    from unsloth.chat_templates import train_on_responses_only

    files = [f for pattern in args.data for f in glob.glob(pattern)]
    if not files:
        raise FileNotFoundError(f"未找到训练数据: {args.data}")
    print(f"训练数据: {files}")

    loader = FastModel if args.moe else FastLanguageModel
    model, tokenizer = loader.from_pretrained(
        model_name=args.model,
        max_seq_length=MAX_SEQ_LENGTH,
        load_in_4bit=False,           # Qwen3.5 官方不建议 4bit QLoRA
        load_in_16bit=True,
        full_finetuning=args.full_finetune,
    )

    if not args.full_finetune:
        model = loader.get_peft_model(
            model,
            r=16,
            target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                            "gate_proj", "up_proj", "down_proj"],
            lora_alpha=16,
            lora_dropout=0,
            bias="none",
            use_gradient_checkpointing="unsloth",
            random_state=3407,
            max_seq_length=MAX_SEQ_LENGTH,
        )

    dataset = load_dataset("json", data_files=files, split="train")

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        args=SFTConfig(
            dataset_text_field="text",
            max_seq_length=MAX_SEQ_LENGTH,
            per_device_train_batch_size=args.batch_size,
            gradient_accumulation_steps=args.grad_accum,
            warmup_steps=10,
            max_steps=args.max_steps,
            logging_steps=1,
            save_steps=100,
            output_dir=args.output + "_ckpt",
            optim="adamw_8bit",
            weight_decay=0.01,
            lr_scheduler_type="cosine",
            learning_rate=2e-4,
            seed=3407,
            dataset_num_proc=2,
            packing=False,
        ),
    )

    # 只对 assistant 回复部分计算损失；Qwen3.5 是 ChatML 模板
    trainer = train_on_responses_only(
        trainer,
        instruction_part="<|im_start|>user\n",
        response_part="<|im_start|>assistant\n",
    )

    # 自检: 抽一条样本看 mask 是否正确
    tokenizer.decode(trainer.train_dataset[0]["input_ids"])
    space = tokenizer(" ", add_special_tokens=False).input_ids[0]
    print("=== 仅 assistant 部分带 label ===")
    print(tokenizer.decode([space if x == -100 else x for x in trainer.train_dataset[0]["labels"]]))

    gpu_stats = torch.cuda.get_device_properties(0)
    print(f"GPU: {gpu_stats.name}, {gpu_stats.total_memory / 1e9:.1f} GB")
    trainer.train()

    # 保存 LoRA 适配器
    model.save_pretrained(args.output)
    tokenizer.save_pretrained(args.output)
    # 合并导出 16bit（供 vLLM 等使用）:
    # model.save_pretrained_merged("qwen35_sft_merged", tokenizer, save_method="merged_16bit")


if __name__ == "__main__":
    main()
