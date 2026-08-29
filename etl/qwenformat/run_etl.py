"""Run ETL: sft_openai.json -> Qwen3 SFT 训练格式 (JSONL)。

用法:
    python run_etl.py [--input input_data/sft_openai.json] \
                      [--output output_data/sft_qwen3.jsonl] \
                      [--template chat_template.jinja]
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter

from transform import build_chat_env, load_chat_template, transform_sample

DEFAULT_TEMPLATE_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "chat_template.jinja"),
    r"F:\modelscope\Qwen\Qwen3.5-9B\chat_template.jinja.txt",
]


def find_template(explicit: str | None) -> str:
    if explicit:
        return explicit
    for cand in DEFAULT_TEMPLATE_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    raise FileNotFoundError("未找到 chat template，请用 --template 指定路径")


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=os.path.join(here, "input_data", "sft_openai.json"))
    parser.add_argument("--output", default=os.path.join(here, "output_data", "sft_qwen3.jsonl"))
    parser.add_argument("--template", default=None, help="chat template jinja 文件路径")
    args = parser.parse_args()

    template_path = find_template(args.template)
    template_str = load_chat_template(template_path)
    env = build_chat_env()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    stats: Counter[str] = Counter()
    out_records: list[dict] = []
    for sample in data:
        rec = transform_sample(sample, template_str, env, stats)
        stats["samples"] += 1
        stats["rendered_chars"] += len(rec["text"])
        out_records.append(rec)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        for rec in out_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"template : {template_path}")
    print(f"input    : {args.input}")
    print(f"output   : {args.output} ({len(out_records)} samples)")
    print("stats    :")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
