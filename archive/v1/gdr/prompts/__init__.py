import json
import re
import yaml
from pathlib import Path
from jinja2 import Template

_PROMPTS_DIR = Path(__file__).parent


def load_prompt(prompt_name: str) -> dict:
    path = _PROMPTS_DIR / f"{prompt_name}.yaml"
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def render_prompt(template: str, **kwargs) -> str:
    return Template(template).render(**kwargs)


def load_and_render(prompt_name: str, section: str, **kwargs) -> str:
    data = load_prompt(prompt_name)
    template = data.get(section, "")
    return render_prompt(template, **kwargs)


_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?```", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def parse_json_object(text: str) -> dict:
    """Robustly extract the first JSON object from a model output.

    Strips <think>...</think> blocks and ```json fences, then locates the
    first balanced { ... } region and parses it. Returns {} on failure.
    """
    if not text:
        return {}
    cleaned = _THINK_RE.sub("", text)
    for match in _FENCE_RE.finditer(cleaned):
        candidate = match.group(1).strip()
        try:
            return json.loads(candidate)
        except Exception:
            continue
    decoder = json.JSONDecoder()
    for i, ch in enumerate(cleaned):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(cleaned, i)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    try:
        return json.loads(cleaned)
    except Exception:
        return {}