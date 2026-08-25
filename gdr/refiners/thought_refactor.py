import json
import re
import logging
from domain import ThinkingBlock, RefineLogEntry

log = logging.getLogger(__name__)


def _extract_entities(text: str) -> set[str]:
    entities = set()
    for match in re.finditer(r'"([^"]+)"', text):
        entities.add(match.group(1))
    for match in re.finditer(r"'([^']+)'", text):
        entities.add(match.group(1))
    for match in re.finditer(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b", text):
        entities.add(match.group(1))
    for match in re.finditer(r"\b(browser|execute_shell_command|write_file|read_file|search_file|list_files|glob|grep|tavily_search)\b", text, re.IGNORECASE):
        entities.add(match.group(1).lower())
    for match in re.finditer(r"\b(url|file_path|command|content|code|input|query|name)\b", text, re.IGNORECASE):
        entities.add(match.group(1).lower())
    return entities


def refine(block: ThinkingBlock, context: dict, defects: list[str], cfg) -> str | None:
    from prompts import load_and_render
    from infrastructure import LlamaCppClient

    has_defect = any(
        d in defects for d in ["thought_too_short", "thought_too_long", "thought_broken_logic"]
    )
    if not has_defect:
        return block.thinking

    prompt = load_and_render(
        "thought", "task",
        original_thinking=block.thinking,
        context=json.dumps(context, ensure_ascii=False),
        defects=", ".join(defects),
    )

    last_error = None
    for attempt in range(cfg.max_retries_9b):
        try:
            client = LlamaCppClient.get(cfg.main_model, cfg=cfg, timeout_s=cfg.llm_timeout_s)
            text, meta = client.generate(prompt, max_tokens=600)
            result = json.loads(text)
            refined = result.get("refined_thought", "")
            if not refined:
                raise ValueError("empty refined_thought")
            if len(refined) < cfg.thought_min_len or len(refined) > cfg.thought_max_len:
                raise ValueError(f"length out of range: {len(refined)}")
            orig_entities = _extract_entities(block.thinking)
            new_entities = _extract_entities(refined)
            if not orig_entities.issubset(new_entities):
                missing = orig_entities - new_entities
                log.warning("entity loss in block %s: %s", block.id, missing)
                raise ValueError(f"entity loss: {missing}")
            return refined
        except Exception as e:
            last_error = str(e)
            log.debug("thought_refactor attempt %d failed: %s", attempt + 1, e)
            continue

    try:
        log.warning("escalation to 32B for block %s", block.id)
        client = LlamaCppClient.get(cfg.tool_model, cfg=cfg, timeout_s=cfg.llm_timeout_s)
        text, meta = client.generate(prompt, max_tokens=600)
        result = json.loads(text)
        refined = result.get("refined_thought", "")
        if refined and cfg.thought_min_len <= len(refined) <= cfg.thought_max_len:
            orig_entities = _extract_entities(block.thinking)
            new_entities = _extract_entities(refined)
            if orig_entities.issubset(new_entities):
                return refined
    except Exception as e:
        last_error = str(e)

    log.error("discard block %s, reason=thought_refactor_exhausted: %s", block.id, last_error)
    return None