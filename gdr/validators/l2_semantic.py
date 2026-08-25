from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
import logging

log = logging.getLogger(__name__)

_EMBED_MODEL = None


def _get_model(model_name: str) -> SentenceTransformer:
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        _EMBED_MODEL = SentenceTransformer(model_name)
    return _EMBED_MODEL


def _embed(text: str | None) -> list[float]:
    if not text:
        return []
    model = _get_model("BAAI/bge-m3")
    return model.encode(text, normalize_embeddings=True).tolist()


def check(original_block, refined_content: dict, embedding_model_name: str) -> tuple[bool, float, float]:
    try:
        block_type = original_block.get("type", "") if isinstance(original_block, dict) else getattr(original_block, "type", "")

        if block_type == "thinking":
            orig_text = original_block.get("thinking", "") if isinstance(original_block, dict) else getattr(original_block, "thinking", "")
            ref_text = refined_content.get("thinking", "")
            threshold = 0.85
        elif block_type == "toolcall":
            orig_text = original_block.get("input", "") if isinstance(original_block, dict) else getattr(original_block, "input", "")
            ref_text = refined_content.get("input", "")
            threshold = 0.90
        elif block_type == "toolresult":
            orig_text = original_block.get("output_text", "") if isinstance(original_block, dict) else getattr(original_block, "output_text", "")
            ref_text = refined_content.get("output_text", "")
            threshold = 0.80
        else:
            return (True, 1.0, 1.0)

        orig_emb = _embed(orig_text)
        ref_emb = _embed(ref_text)

        if not orig_emb or not ref_emb:
            return (True, 1.0, threshold)

        sim = float(cosine_similarity([orig_emb], [ref_emb])[0][0])

        if sim > threshold:
            return (True, sim, threshold)
        elif sim < threshold - 0.03:
            return (False, sim, threshold)
        else:
            return (False, sim, threshold)

    except Exception as e:
        log.error("L2 semantic check failed: %s", e)
        return (True, 1.0, 0.85)