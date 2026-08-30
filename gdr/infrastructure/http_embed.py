"""HTTP embedding client for llama.cpp server (and other OpenAI-compatible endpoints).

The L2 semantic validator used to depend on sentence-transformers + BGE-M3 weights,
which required downloading multi-GB model files and was network-fragile. This
module replaces that with plain HTTP calls to an embedding endpoint
(`POST /v1/embeddings`) such as the llama.cpp server's `--embedding` mode.

API surface (single dependency of the rest of the codebase):

    embedder = HttpEmbedder.from_settings(cfg)   # or build explicitly
    vec = embedder.embed("hello")                # -> list[float]
    vecs = embedder.embed_batch([t1, t2])        # -> list[list[float]]
    sim = HttpEmbedder.cosine(v1, v2)            # pure-python, no numpy

Design notes:
- One ``HttpEmbedder`` instance per process is enough; it caches the discovered
  embedding dimension on first response. A module-level ``get_embedder(cfg)``
  factory returns the singleton, so repeated ``validate_block`` calls do not
  re-connect or re-probe dim.
- Server is expected to return **already-normalized** embeddings (llama.cpp does
  this by default with `-ngl 999 --embedding`). ``cosine`` falls back to
  on-the-fly normalization if a non-unit vector arrives.
"""
from __future__ import annotations

import math
import threading
import time
import logging
from typing import Optional

log = logging.getLogger(__name__)

_EMBEDDER_LOCK = threading.Lock()
_EMBEDDER_SINGLE: Optional["HttpEmbedder"] = None


def _get_http_client():
    """Lazily import httpx so that the module is importable without httpx."""
    try:
        import httpx  # type: ignore
    except ImportError as e:
        raise ImportError(
            "HttpEmbedder requires 'httpx'. Install with: pip install httpx>=0.27"
        ) from e
    return httpx


class Embedder:
    """Minimal embedding-client interface used by validators and evaluator.

    Concrete implementations only need ``embed``, ``embed_batch``, and
    ``cosine`` (the last one can be a staticmethod). Kept as a class (not a
    Protocol) so callers can ``isinstance``-check cheaply.
    """

    def embed(self, text: str) -> list[float]:  # pragma: no cover - interface
        raise NotImplementedError

    def embed_batch(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - interface
        raise NotImplementedError

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:  # pragma: no cover - interface
        raise NotImplementedError


class HttpEmbedder(Embedder):
    """Embedding client for OpenAI-compatible ``POST /v1/embeddings`` endpoints.

    Tested against llama.cpp server ``--embedding`` (v5-nano-retrieval Q6_K
    returns 768-dim normalized vectors). Also works against vLLM, Ollama's
    OpenAI shim, and text-embeddings-inference.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 30.0,
        max_batch: int = 32,
        max_retries: int = 3,
        expected_dim: Optional[int] = None,
        max_input_chars: int = 6000,
    ):
        self.base_url = base_url.rstrip("/")
        # llama.cpp's /v1/embeddings lives at <base>/embeddings; OpenAI path is /v1/embeddings.
        # We accept both "http://host:port/v1" and "http://host:port" + caller adding /v1 themselves.
        if self.base_url.endswith("/v1"):
            self._endpoint = self.base_url + "/embeddings"
        else:
            self._endpoint = self.base_url + "/v1/embeddings"

        self.model = model
        self.timeout = timeout
        self.max_batch = max_batch
        self.max_retries = max_retries
        self._expected_dim = expected_dim  # 若设置, 首响维度不符立即报错
        # 超长输入会超过服务端 n_ctx (llama.cpp 返回 400 exceed_context_size_error),
        # 截断到安全字符预算 —— L2 相似度对比用前缀已足够
        self._max_input_chars = max(1, int(max_input_chars))

        self._dim: Optional[int] = None
        self._dim_lock = threading.Lock()

        httpx = _get_http_client()
        self._client = httpx.Client(timeout=timeout)

    # ---- factory ----------------------------------------------------------

    @classmethod
    def from_settings(cls, cfg) -> "HttpEmbedder":
        """Build from a ``config.Settings`` instance."""
        url = getattr(cfg, "embedding_endpoint_url", None)
        model = getattr(cfg, "embedding_endpoint_model", None)
        if not url or not model:
            raise RuntimeError(
                "Embedding endpoint not configured. Set 'embedding_endpoint_url' "
                "(e.g. http://127.0.0.1:8086/v1) and 'embedding_endpoint_model' "
                "(e.g. v5-nano-retrieval) in gdr_config.yaml or GDR_* env vars."
            )
        return cls(
            base_url=url,
            model=model,
            timeout=float(getattr(cfg, "embedding_timeout_s", 30)),
            max_batch=int(getattr(cfg, "embedding_max_batch", 32)),
            expected_dim=getattr(cfg, "embedding_expected_dim", None),
            max_input_chars=int(getattr(cfg, "embedding_max_input_chars", 6000)),
        )

    # ---- health -----------------------------------------------------------

    def health_check(self) -> bool:
        """Lightweight probe: encode a single short string."""
        try:
            vec = self.embed("health-check")
            return bool(vec) and len(vec) > 0
        except Exception as e:
            log.warning("embedding health check failed: %s", e)
            return False

    # ---- core -------------------------------------------------------------

    def _post(self, payload: dict) -> list[list[float]]:
        """POST to the embeddings endpoint with retry+backoff. Returns vectors."""
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.post(self._endpoint, json=payload)
                if resp.status_code >= 400:
                    # 4xx/5xx 一律带响应体, 否则只剩 "400 Bad Request" 无法定位拒绝原因
                    raise RuntimeError(
                        f"embedding endpoint {resp.status_code}: {resp.text[:200]}"
                    )
                body = resp.json()
                # OpenAI shape: {"data": [{"embedding": [...], "index": i}, ...]}
                data = body.get("data") or []
                vecs: list[list[float]] = []
                for item in sorted(data, key=lambda x: x.get("index", 0)):
                    vecs.append([float(x) for x in item["embedding"]])
                return vecs
            except Exception as e:  # network / parse / 5xx
                last_exc = e
                sleep_s = min(2 ** attempt, 8)
                log.warning(
                    "embedding request failed (attempt %d/%d): %s; retrying in %.1fs",
                    attempt + 1, self.max_retries, e, sleep_s,
                )
                time.sleep(sleep_s)
        raise RuntimeError(f"embedding endpoint unreachable after {self.max_retries} retries: {last_exc}")

    def _lock_dim(self, vec: list[float]) -> int:
        actual = len(vec)
        if self._dim is None:
            with self._dim_lock:
                if self._dim is None:
                    if self._expected_dim is not None and actual != self._expected_dim:
                        raise RuntimeError(
                            f"embedding dim mismatch on first response: "
                            f"got {actual}, expected {self._expected_dim} "
                            f"(check cfg.embedding_expected_dim vs endpoint model)"
                        )
                    self._dim = actual
        if actual != self._dim:
            raise RuntimeError(
                f"embedding dim mismatch: got {actual}, expected {self._dim}"
            )
        return self._dim

    def _truncate(self, text: str) -> str:
        if len(text) <= self._max_input_chars:
            return text
        log.debug(
            "embedding input truncated: %d -> %d chars (server n_ctx protection)",
            len(text), self._max_input_chars,
        )
        return text[: self._max_input_chars]

    def embed(self, text: str) -> list[float]:
        if not text:
            return []
        vecs = self._post({
            "model": self.model,
            "input": self._truncate(text),
        })
        if not vecs:
            return []
        self._lock_dim(vecs[0])
        return vecs[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), self.max_batch):
            chunk = [self._truncate(t) for t in texts[i : i + self.max_batch]]
            vecs = self._post({"model": self.model, "input": chunk})
            if vecs and self._dim is None:
                self._lock_dim(vecs[0])
            for v in vecs:
                if v:
                    self._lock_dim(v)
                else:
                    v = [0.0] * (self._dim or 0)
                out.append(v)
        return out

    # ---- cosine -----------------------------------------------------------

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        """Pure-python cosine similarity.

        Most llama.cpp / OpenAI-compatible embedding endpoints already return
        L2-normalized vectors; in that case the cosine is just the dot product.
        If a vector is not normalized (|v| deviates from 1 by > 1e-3), normalize
        on the fly. Returns 0.0 for empty / mismatched inputs.
        """
        if not a or not b:
            return 0.0
        n = min(len(a), len(b))
        if n == 0:
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for i in range(n):
            ai = float(a[i])
            bi = float(b[i])
            dot += ai * bi
            na += ai * ai
            nb += bi * bi
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        # On-the-fly normalization (cheap and idempotent for already-normed vectors).
        denom = math.sqrt(na) * math.sqrt(nb)
        if denom == 0.0:
            return 0.0
        sim = dot / denom
        # Clamp to [-1, 1] to defend against fp drift.
        if sim > 1.0:
            return 1.0
        if sim < -1.0:
            return -1.0
        return sim

    # ---- dim --------------------------------------------------------------

    @property
    def dim(self) -> Optional[int]:
        return self._dim

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"HttpEmbedder(endpoint={self._endpoint}, model={self.model!r}, dim={self._dim})"


def get_embedder(cfg) -> HttpEmbedder:
    """Return a process-singleton ``HttpEmbedder`` built from ``cfg``.

    Re-invocation with the same target endpoint/model returns the cached
    instance, so validators and the evaluator share one HTTP client.
    """
    global _EMBEDDER_SINGLE
    if _EMBEDDER_SINGLE is not None:
        return _EMBEDDER_SINGLE
    with _EMBEDDER_LOCK:
        if _EMBEDDER_SINGLE is None:
            _EMBEDDER_SINGLE = HttpEmbedder.from_settings(cfg)
    return _EMBEDDER_SINGLE


def reset_embedder_singleton() -> None:
    """Test hook: clear the cached singleton so the next call rebuilds it."""
    global _EMBEDDER_SINGLE
    with _EMBEDDER_LOCK:
        _EMBEDDER_SINGLE = None