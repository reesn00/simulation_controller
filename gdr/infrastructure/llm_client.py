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
            instance = cls._instances[key]
        # PR 3 (Commit 3b/9): 让下游 helper 能拿到 cfg (retry_loop_clip 子 generation
        # span 需要 cfg 调工厂 get_client). 每次 get 调用都覆盖, 兼容 cfg 重置场景.
        if cfg is not None:
            try:
                instance.cfg = cfg  # type: ignore[attr-defined]
            except Exception:
                pass
        return instance

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
        # PR 3 (Commit 9): per-LLM-span hook. 仅当
        # ``langfuse_gdr_per_llm_span=True`` 时, 在 chat 入口起 generation span.
        # 默认 false (避免 100+ spans/session 配额爆炸); 启用时建议配合
        # ``sample_rate=0.1`` 进一步降采样. factory 缺 client 时 hook 立即
        # yield None, 业务流程不受影响.
        _lf_per_llm_span_cm = None
        if getattr(self, "cfg", None) is not None and getattr(
            self.cfg, "langfuse_gdr_per_llm_span", False
        ):
            try:
                from simulate_serve.observability.langfuse_client import (
                    get_client as _lf_get_client_for_chat,
                )
                from gdr.observability.llm_hook import maybe_llm_span
                _lf_client = _lf_get_client_for_chat(self.cfg)
                if _lf_client is not None:
                    _lf_per_llm_span_cm = maybe_llm_span(
                        _lf_client,
                        name=f"gdr.llm.{self.model}",
                        model=self.model,
                        messages=list(messages),
                        metadata={
                            "max_tokens": int(max_tokens),
                            "temperature": float(temperature),
                        },
                    )
            except Exception as exc:  # pragma: no cover - fail-safe
                log.debug("per_llm_span hook skipped: %s", exc)
                _lf_per_llm_span_cm = None

        # 进入 span (如果有); ``with`` 即使 cm 是 None 也走业务路径
        if _lf_per_llm_span_cm is not None:
            with _lf_per_llm_span_cm:
                return self._chat_inner(
                    messages, grammar_json_schema, max_tokens,
                    temperature, timeout_s,
                )
        return self._chat_inner(
            messages, grammar_json_schema, max_tokens,
            temperature, timeout_s,
        )

    def _chat_inner(
        self,
        messages: list[dict],
        grammar_json_schema: dict | None,
        max_tokens: int,
        temperature: float,
        timeout_s: int | None,
    ) -> tuple[str, dict]:
        """``chat`` 内部实现: 与 per-LLM-span hook 解耦, 便于嵌套调用。"""
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
        # reasoning 模型兜底: 某些 OpenAI 兼容端点 (DeepSeek / 部分 vLLM 配置)
        # 把思考链放在 message.reasoning_content 字段, 而 content 仅承载最终
        # 答案. content 为空但 reasoning_content 非空时, 优先尝试把
        # reasoning_content 作为答案用 — 多数情况下 reasoning_content 末尾
        # 已经包含 JSON 答案; 少数只放纯思考时仍会走到 parse_json_object 失败
        # 的旧路径, 但至少有了一次机会, 不再白丢。
        reasoning_text = message.get("reasoning_content", "") or ""
        if not text and reasoning_text:
            text = reasoning_text
            log.info(
                "llm response: content empty, fell back to reasoning_content "
                "(model=%s, reasoning_len=%d)",
                self.model, len(reasoning_text),
            )
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
            # 用于审计: 是否走了 reasoning_content 兜底 (True/False), 调用方
            # 可写入 session.metadata / audit log, 便于排查 reasoning 模型
            # "思考塞满 budget" 的现场。
            "used_reasoning_content_fallback": bool(
                reasoning_text and not message.get("content")
            ),
        }
        return text, meta