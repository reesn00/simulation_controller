import json
import logging
from domain import ToolcallBlock
from prompts import parse_json_object

log = logging.getLogger(__name__)

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "input": {"type": "string"},
    },
    "required": ["name", "input"],
}


def refine(
    block: ToolcallBlock, context: dict,
    tool_names: list[str], hallu_apis: set[str],
    defects: list[str], cfg,
) -> dict | None:
    from prompts import load_and_render
    from infrastructure import LlamaCppClient

    has_defect = any(
        d in defects for d in [
            "tool_json_invalid", "tool_hallucinated", "api_hallucination",
            "tool_wrong_selection", "repetitive_call",
        ]
    )
    if not has_defect:
        return {"name": block.name, "input": block.input}

    system_prompt = load_and_render(
        "tool", "system",
        tool_names=tool_names,
        hallu_apis=list(hallu_apis),
    )
    user_prompt = load_and_render(
        "tool", "user",
        original_action={"name": block.name, "input": block.input},
        context=json.dumps(context, ensure_ascii=False),
        defects=defects,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_error = None
    for attempt in range(cfg.max_retries_9b):
        retry_messages = messages
        if attempt > 0:
            retry_messages = list(messages) + [
                {"role": "user", "content": "上轮你只给了分析说明, 没有输出 JSON。请立即输出一个 ```json {\"name\": \"...\", \"input\": \"...\"} ``` 代码块, name 必须是可用工具之一, input 必须是合法 JSON 字符串。"}
            ]
        try:
            client = LlamaCppClient.get(cfg.main_model, cfg=cfg, timeout=cfg.llm_timeout_s)
            text, meta = client.chat(retry_messages, grammar_json_schema=OUTPUT_SCHEMA, max_tokens=1536)
            result = parse_json_object(text)
            name = result.get("name", "")
            inp = result.get("input", "")
            if name not in tool_names:
                raise ValueError(f"tool name not in allowed list: {name}")
            try:
                json.loads(inp)
            except Exception:
                raise ValueError("repaired input is not valid JSON")
            inp_lower = inp.lower()
            for api in hallu_apis:
                if api.lower() in inp_lower:
                    raise ValueError(f"hallucinated API still present: {api}")
            return {"name": name, "input": inp}
        except Exception as e:
            last_error = str(e)
            log.debug("tool_fixer attempt %d failed: %s; raw=%r", attempt + 1, e, text[:400] if isinstance(text, str) else text)
            continue

    try:
        log.warning("escalation to 32B for block %s", block.id)
        client = LlamaCppClient.get(cfg.tool_model, cfg=cfg, timeout=cfg.llm_timeout_s)
        text, meta = client.chat(messages, grammar_json_schema=OUTPUT_SCHEMA, max_tokens=1536)
        result = parse_json_object(text)
        name = result.get("name", "")
        inp = result.get("input", "")
        if name in tool_names:
            try:
                json.loads(inp)
                inp_lower = inp.lower()
                if not any(api.lower() in inp_lower for api in hallu_apis):
                    return {"name": name, "input": inp}
            except Exception:
                pass
    except Exception as e:
        last_error = str(e)

    log.error("discard block %s, reason=tool_fix_exhausted: %s", block.id, last_error)
    return None