import os

from unsloth import FastLanguageModel
import torch

# ============ 0. 路径配置 ============
# 当前在 WSL2 中运行，Windows 的 F:\modelscope\Qwen\Qwen3.5-9B 挂载为 /mnt/f/...
# 若直接在 Windows 上运行，改成 r"F:\modelscope\Qwen\Qwen3.5-9B"
MODEL_PATH = "/mnt/f/modelscope/Qwen/Qwen3.5-9B"
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(_SCRIPT_DIR, "input_data", "sft_qwen3.jsonl")
OUTPUT_DIR = os.path.join(_SCRIPT_DIR, "outputs")

# 数据集渲染后 token 长度：min 1.4k / 中位数 45k / max 122k，49 条中 43 条超过 8192。
# 16G 显存下取 32768；超过部分会被截断。显存不够可降到 16384，更充裕可升到 65536。
MAX_SEQ_LENGTH = 4096

# ============ 1. 加载模型 ============
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name = MODEL_PATH,
    max_seq_length = MAX_SEQ_LENGTH,
    load_in_4bit = True,               # QLoRA 4bit 量化，省显存；显存充足可设 False
    # token = "hf_...",                # 私有模型需要填
)

# ============ 2. 添加 LoRA 适配器 ============
model = FastLanguageModel.get_peft_model(
    model,
    r = 16,                            # LoRA 秩，16/32/64 可调
    target_modules = [
        "q_proj", "k_proj", "v_proj", "o_proj",
        "gate_proj", "up_proj", "down_proj",
    ],
    lora_alpha = 16,
    lora_dropout = 0,
    bias = "none",
    use_gradient_checkpointing = "unsloth",  # 省显存黑科技
    random_state = 3407,
)

# ============ 3. 准备数据集 ============
from datasets import load_dataset

# 不再用 unsloth 的 "qwen3" 模板覆盖：本地模型自带 chat_template.jinja，
# 已原生支持 tools / tool_calls / reasoning_content，与 Qwen3.5 的训练格式一致。
dataset = load_dataset("json", data_files=DATA_FILE, split="train")

def formatting_prompts_func(examples):
    texts = [
        tokenizer.apply_chat_template(
            convo, tools=tools, tokenize=False, add_generation_prompt=False
        )
        for convo, tools in zip(examples["messages"], examples["tools"])
    ]
    return {"text": texts}

dataset = dataset.map(
    formatting_prompts_func,
    batched=True,
    remove_columns=[c for c in dataset.column_names if c != "text"],
)

# 查看一条样本确认格式正确
print(dataset[0]["text"][:500])

# ============ 4. 训练 ============
from trl import SFTTrainer, SFTConfig

trainer = SFTTrainer(
    model = model,
    processing_class = tokenizer,      # trl 1.x 已从 tokenizer 改名 processing_class
    train_dataset = dataset,
    args = SFTConfig(
        max_length = MAX_SEQ_LENGTH,   # trl 1.x 已从 max_seq_length 改名 max_length
        dataset_text_field = "text",
        per_device_train_batch_size = 1,    # 序列很长，16G 显存只能 batch=1
        gradient_accumulation_steps = 2,    # 有效 batch = 8
        warmup_steps = 10,
        num_train_epochs = 3,               # 或用 max_steps
        learning_rate = 2e-4,
        fp16 = not torch.cuda.is_bf16_supported(),
        bf16 = torch.cuda.is_bf16_supported(),
        logging_steps = 10,
        optim = "adamw_8bit",
        weight_decay = 0.01,
        lr_scheduler_type = "cosine",
        seed = 3407,
        output_dir = OUTPUT_DIR,
        report_to = "none",                 # 可改为 "wandb"
    ),
)

trainer.train()

# ============ 5. 保存模型 ============
lora_dir = os.path.join(_SCRIPT_DIR, "qwen3.5-9b-sft-lora")
model.save_pretrained(lora_dir)            # 只保存 LoRA 权重
tokenizer.save_pretrained(lora_dir)

# 合并导出完整模型（可选，16bit）
# model.save_pretrained_merged(os.path.join(_SCRIPT_DIR, "qwen3.5-9b-sft-merged"), tokenizer, save_method="merged_16bit")
