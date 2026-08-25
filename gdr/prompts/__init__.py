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