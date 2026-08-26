import json
import re
import logging
from domain import ToolresultBlock

log = logging.getLogger(__name__)

_MARKDOWN_PATTERN = re.compile(r"```|###|\*\*|---")
_JSON_PREFIX_PATTERN = re.compile(r"^\s*[\{\[]")


def refine(block: ToolresultBlock, context: dict, defects: list[str], cfg) -> str | None:
    from prompts import load_and_render
    from infrastructure import LlamaCppClient

    has_defect = any(
        d in defects for d in ["obs_noise", "obs_debug_leak"]
    )
    if not has_defect:
        return block.output_text

    prompt = load_and_render(
        "obs", "system",
    )
    prompt += "\n\n" + load_and_render(
        "obs", "user",
        context=json.dumps(context, ensure_ascii=False),
        original_observation=block.output_text,
    )

    last_error = None
    for attempt in range(cfg.max_retries_9b):
        try:
            client = LlamaCppClient.get(cfg.main_model, cfg=cfg, timeout=cfg.llm_timeout_s)
            text, meta = client.generate(prompt, max_tokens=512)
            text = text.strip()

            if not text:
                raise ValueError("empty output")

            ratio = len(text) / max(len(block.output_text), 1)
            if ratio > cfg.max_compression_ratio:
                raise ValueError(f"compression ratio {ratio:.2f} exceeds {cfg.max_compression_ratio}")

            if _MARKDOWN_PATTERN.search(text):
                raise ValueError("output contains markdown formatting")

            if _JSON_PREFIX_PATTERN.match(text):
                raise ValueError("output appears to be JSON")

            return text
        except Exception as e:
            last_error = str(e)
            log.debug("obs_denoiser attempt %d failed: %s", attempt + 1, e)
            continue

    try:
        log.warning("escalation to 32B for block %s", block.id)
        client = LlamaCppClient.get(cfg.tool_model, cfg=cfg, timeout=cfg.llm_timeout_s)
        text, meta = client.generate(prompt, max_tokens=512)
        text = text.strip()
        if text:
            ratio = len(text) / max(len(block.output_text), 1)
            if ratio <= cfg.max_compression_ratio and not _MARKDOWN_PATTERN.search(text):
                return text
    except Exception as e:
        last_error = str(e)

    log.error("discard block %s, reason=obs_denoise_exhausted: %s", block.id, last_error)
    return None