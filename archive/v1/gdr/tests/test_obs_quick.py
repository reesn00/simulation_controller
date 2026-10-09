"""Quick standalone test for obs_denoiser prompt + LLM response quality."""
from prompts import load_and_render
from infrastructure import LlamaCppClient
from gdr.config.settings import Settings

cfg = Settings()
obs_text = (
    "Command failed with exit code 1.\n"
    "[stderr]\n"
    "Traceback (most recent call last):\n"
    '  File "C:/Users/shenr/.qwenpaw/workspaces/default/search_movies.py", line 3, in <module>\n'
    "    from qwen_paw.core import Browser\n"
    "ModuleNotFoundError: No module named 'qwen_paw'\n"
    "DEBUG: connection pool stats: 8 active\n"
    "INFO: retry attempt 1\n"
    "[API_MISUSE] /v1/browser.open invalid arg\n"
)
sys_p = load_and_render("obs", "system")
usr_p = load_and_render("obs", "user", context="{}", original_observation=obs_text)
messages = [{"role": "system", "content": sys_p}, {"role": "user", "content": usr_p}]
client = LlamaCppClient.get(cfg.main_model, cfg=cfg, timeout=60)
text, _ = client.chat(messages, max_tokens=512)
print("ORIG LEN:", len(obs_text), "RAW LEN:", len(text), "RATIO:", round(len(text) / len(obs_text), 2))
print("---")
print(text)