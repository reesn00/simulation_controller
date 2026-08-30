"""训练完成后基于 基础模型 + LoRA 适配器 的问答推理测试脚本。

用法：
    python infer.py                 # 跑内置测试问题
    python infer.py -i              # 进入交互式问答
    python infer.py -q "你的问题"    # 单条提问
"""
import os

from unsloth import FastLanguageModel
import torch

# ============ 0. 路径配置 ============
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 训练脚本 main.py 保存的 LoRA 目录（含 adapter_config.json，
# 其中的 base 模型路径指向 /mnt/f/modelscope/Qwen/Qwen3.5-9B，需保持可访问）
LORA_DIR = os.path.join(_SCRIPT_DIR, "qwen3.5-9b-sft-lora")

MAX_SEQ_LENGTH = 4096  # 与训练保持一致；推理输入较短，无需调大

# 内置测试问题（含训练语料相关与通用问题，便于对比微调效果）
TEST_QUESTIONS = [
    "你好，请简单介绍一下你自己。",
    "什么是 LoRA 微调？它和全量微调相比有什么优缺点？",
    "用 Python 写一个判断字符串是否为回文的函数。",
]


def load_model():
    # 直接传 LoRA 目录：unsloth 会根据 adapter_config.json 自动加载基础模型并挂载 LoRA
    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name = LORA_DIR,
        max_seq_length = MAX_SEQ_LENGTH,
        load_in_4bit = True,
    )
    FastLanguageModel.for_inference(model)  # 切换到 2 倍速推理模式
    return model, tokenizer


def chat(model, tokenizer, messages, max_new_tokens=1024, enable_thinking=False):
    """按训练时的 chat template 渲染并生成回答。

    Qwen3.5 是思考模型，简单问答默认关闭思考（enable_thinking=False）
    以便快速得到回答；--think 时开启并自动剥离 <think> 段。
    """
    prompt = tokenizer.apply_chat_template(
        messages, tokenize=False, add_generation_prompt=True,
        enable_thinking=enable_thinking,
    )
    # Qwen3.5 的 tokenizer 是多模态 processor，位置参数第一个是 images，
    # 必须用 text= 关键字传文本，否则纯文本会被当成图片路径解析而报错
    inputs = tokenizer(text=prompt, return_tensors="pt").to(model.device)

    with torch.no_grad():
        output = model.generate(
            **inputs,
            max_new_tokens = max_new_tokens,
            temperature = 0.7,
            top_p = 0.8,
            do_sample = True,
            repetition_penalty = 1.05,
        )
    # 截掉输入部分，只解码新生成的 token
    response = tokenizer.decode(
        output[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True
    ).strip()
    if enable_thinking:
        # 剥离思考段，只保留正式回答
        if "</think>" in response:
            response = response.split("</think>", 1)[1].strip()
    return response


def main():
    import argparse
    parser = argparse.ArgumentParser(description="基础模型 + LoRA 问答测试")
    parser.add_argument("-q", "--question", help="单条提问")
    parser.add_argument("-i", "--interactive", action="store_true", help="交互式问答")
    parser.add_argument("--think", action="store_true",
                        help="开启思考模式（生成更慢，token 上限建议调大）")
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    args = parser.parse_args()

    model, tokenizer = load_model()

    if args.question:
        answer = chat(model, tokenizer, [{"role": "user", "content": args.question}],
                      args.max_new_tokens, args.think)
        print(f"\n问题：{args.question}\n回答：{answer}")
        return

    if args.interactive:
        print("交互式问答（输入 quit / exit 退出）")
        messages = []
        while True:
            try:
                user_input = input("\n你：").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not user_input or user_input.lower() in ("quit", "exit"):
                break
            messages.append({"role": "user", "content": user_input})
            answer = chat(model, tokenizer, messages, args.max_new_tokens, args.think)
            print(f"助手：{answer}")
            messages.append({"role": "assistant", "content": answer})
        return

    # 默认：跑内置测试问题
    for q in TEST_QUESTIONS:
        print("=" * 60)
        print(f"问题：{q}")
        answer = chat(model, tokenizer, [{"role": "user", "content": q}],
                      args.max_new_tokens, args.think)
        print(f"回答：{answer}")


if __name__ == "__main__":
    main()
