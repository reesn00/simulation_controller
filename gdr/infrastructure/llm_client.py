"""LLM client implemented over HTTP OpenAI-compatible chat-completions API.

The historical class name ``LlamaCppClient`` is kept to minimize import churn,
but this implementation no longer loads local GGUF files; it talks to an
external endpoint (vLLM, llama.cpp server, Ollama, etc.) via plain HTTP.
"""
from threading import Lock, Semaphore
from concurrent.futures import TimeoutError as FuturesTimeout
from concurrent.futures import ThreadPoolExecutor
import json
import time
import logging

log = logging.getLogger(__name__)

# 限制并发推理请求数，避免单个超时任务持续占用底层计算导致后续请求饿死
_MAX_CONCURRENT_GENERATIONS = 4
_generation_sem = Semaphore(_MAX_CONCURRENT_GENERATIONS)


def set_generation_concurrency(n: int) -> None:
    """调整并发生成上限并重建信号量。

    只应在进程启动阶段、任何 LLM 调用发出之前调用 (非线程安全);
    runner 在主进程与每个 Pool worker 初始化时各调用一次。
    """
    global _MAX_CONCURRENT_GENERATIONS, _generation_sem
    if n < 1:
        return
    _MAX_CONCURRENT_GENERATIONS = n
    _generation_sem = Semaphore(n)


def _get_http_client():
    """Lazily import httpx so that the module can still be imported when httpx is absent."""
    try:
        import httpx  # type: ignore
    except ImportError as e:
        raise ImportError(
            "HTTP LLM client requires 'httpx'. Install with: pip install httpx>=0.27"
        ) from e
    return httpx


class LlamaCppClient:
    _instances: dict[str, "LlamaCppClient"] = {}
    _lock = Lock()

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        httpx = _get_http_client()
        self._client = httpx.Client(timeout=timeout)

    @classmethod
    def get(
        cls,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
        cfg=None,
    ) -> "LlamaCppClient":
        """Get or create a singleton client.

        ``cfg`` is the preferred source for base_url / api_key / timeout.
        Explicit arguments override cfg when provided.
        ``model`` may be a model name (preferred) or a historical Path-like string.
        """
        from pathlib import Path

        if cfg is not None:
            if base_url is None:
                base_url = cfg.llm_base_url
            if api_key is None:
                api_key = cfg.llm_api_key
            if timeout is None:
                timeout = getattr(cfg, "llm_timeout_s", 120)

        base_url = base_url or "http://localhost:8000/v1"
        api_key = api_key or "not-needed"
        timeout = timeout if timeout is not None else 120.0

        # Accept either a model name or a GGUF path for backwards compatibility.
        model_name = str(model)
        if model_name.endswith(".gguf") or "/" in model_name or "\\" in model_name:
            model_name = Path(model_name).stem

        key = f"{base_url}|{model_name}"
        with cls._lock:
            if key not in cls._instances:
                log.info("creating HTTP LLM client: base_url=%s model=%s", base_url, model_name)
                cls._instances[key] = cls(base_url, api_key, model_name, timeout)
            return cls._instances[key]

    def _post_chat_completions(self, payload: dict, timeout_s: float | None = None) -> dict:
        httpx = _get_http_client()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"
        effective_timeout = timeout_s if timeout_s is not None else self.timeout

        with _generation_sem:
            try:
                resp = self._client.post(url, headers=headers, json=payload, timeout=effective_timeout)
                resp.raise_for_status()
                return resp.json()
            except httpx.TimeoutException as e:
                raise TimeoutError(f"LLM request exceeded {effective_timeout}s timeout") from e
            except httpx.HTTPStatusError as e:
                raise RuntimeError(f"LLM request failed: {e.response.status_code} {e.response.text}") from e

    def generate(
        self,
        prompt: str,
        grammar_json_schema: dict | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout_s: int | None = None,
    ) -> tuple[str, dict]:
        """Generate a completion for a single user prompt over HTTP OpenAI API.

        ``grammar_json_schema`` is translated into ``response_format`` of type
        ``json_schema`` when the backend supports it; otherwise it is appended as
        an explicit instruction in the prompt as a fallback.
        """
        messages = [{"role": "user", "content": prompt}]
        return self.chat(
            messages,
            grammar_json_schema=grammar_json_schema,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
        )

    def chat(
        self,
        messages: list[dict],
        grammar_json_schema: dict | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout_s: int | None = None,
    ) -> tuple[str, dict]:
        t0 = time.perf_counter()

        content = messages[-1].get("content", "") if messages else ""
        if grammar_json_schema and not content.endswith(json.dumps(grammar_json_schema)):
            # Fallback instruction in case the backend does not support json_schema response_format.
            messages[-1]["content"] = (
                content
                + "\n\nYou must output valid JSON conforming to this schema:\n"
                + json.dumps(grammar_json_schema, ensure_ascii=False)
            )

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if grammar_json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "schema": grammar_json_schema,
                    "strict": True,
                },
            }

        data = self._post_chat_completions(payload, timeout_s=timeout_s)

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        text = message.get("content", "")
        finish = choice.get("finish_reason", "")
        if not text:
            log.warning(
                "empty llm response: model=%s finish_reason=%s payload_keys=%s raw_choice_keys=%s",
                self.model, finish, list(payload.keys()), list(choice.keys()),
            )

        usage = data.get("usage", {})
        meta = {
            "model": self.model,
            "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "latency_s": round(time.perf_counter() - t0, 3),
            "timed_out": False,
        }
        return text, meta