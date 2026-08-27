import json
import logging
from infrastructure import LlamaCppClient

log = logging.getLogger(__name__)


def check(original_block, refined_content: dict, cfg) -> dict:
    from prompts import load_and_render

    block_type = original_block.get("type", "") if isinstance(original_block, dict) else getattr(original_block, "type", "")

    if block_type == "thinking":
        orig = original_block.get("thinking", "") if isinstance(original_block, dict) else getattr(original_block, "thinking", "")
        ref = refined_content.get("thinking", "")
    elif block_type == "toolcall":
        orig = json.dumps({
            "name": original_block.get("name", "") if isinstance(original_block, dict) else getattr(original_block, "name", ""),
            "input": original_block.get("input", "") if isinstance(original_block, dict) else getattr(original_block, "input", ""),
        }, ensure_ascii=False)
        ref = json.dumps(refined_content, ensure_ascii=False)
    elif block_type == "toolresult":
        orig = original_block.get("output_text", "") if isinstance(original_block, dict) else getattr(original_block, "output_text", "")
        ref = refined_content.get("output_text", "")
    else:
        return {"verdict": "pass", "score": 10, "reason": "unsupported block type"}

    prompt = load_and_render(
        "judge", "system",
    )
    prompt += "\n\n" + load_and_render(
        "judge", "user",
        block_type=block_type,
        original=str(orig),
        refined=str(ref),
        context="",
    )

    try:
        from prompts import parse_json_object
        client = LlamaCppClient.get(cfg.judge_model, cfg=cfg, timeout=cfg.l3_timeout_s)
        system_prompt = load_and_render("judge", "system")
        user_prompt = load_and_render(
            "judge", "user",
            block_type=block_type,
            original=str(orig),
            refined=str(ref),
            context="",
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        text, meta = client.chat(messages, max_tokens=512, temperature=0.0)
        result = parse_json_object(text)
        verdict = result.get("verdict", "fail")
        score = result.get("score", 0)
        reason = result.get("reason", "")
        return {"verdict": verdict, "score": score, "reason": reason}
    except Exception as e:
        log.warning("L3 judge failed: %s", e)
        return {"verdict": "fail", "score": 0, "reason": str(e)}