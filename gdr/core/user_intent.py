"""P0-1.1 + P0-1.3 共用的 user_intent 抽取层。

提供两种互补的抽取方式:
  - ``heuristic_user_intent``  零 LLM, 截断首条 user 消息前 N 字符.
    Router 阶段 (judge/reassembler 之前) 用它跑 TOOL_OFF_TOPIC 嵌入层判定.
  - ``extract_user_intent_llm``  1 次轻量 LLM 调用, 把首条 user 消息压缩为
    1~3 句话的意图摘要. Reassembler 阶段用它注入终 judge prompt, 让 judge
    同时给出 ``intent_fulfillment_score`` (0/1/2).

两种结果互不冲突, 都挂到 ``session.metadata``:
  - ``metadata.user_intent_heuristic``  router 阶段留下的截断原文 (审计)
  - ``metadata.user_intent``             LLM 抽取的精确意图 (judge 注入用)
"""
from __future__ import annotations

import logging
from typing import Any

from domain import Session

log = logging.getLogger(__name__)


def _first_user_text(session: Session) -> str | None:
    """取首条 user 消息的纯文本内容。

    Message.blocks 可能不存在 (user 消息通常无 blocks), 也可能含 text /
    其他类型. 这里取任意 text 类 block 的 text 字段; 若完全无 text block
    则从其他块字段 (input/thinking) 兜底; 都没有返回 None.
    """
    for msg in session.messages:
        if getattr(msg, "role", "") != "user":
            continue
        # 优先取 text block
        for b in msg.blocks:
            btype = b.get("type", "") if isinstance(b, dict) else getattr(b, "type", "")
            if btype == "text":
                text = b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "")
                text = (text or "").strip()
                if text:
                    return text
        # 兜底: 任意 block 的可读字段
        for b in msg.blocks:
            btype = b.get("type", "") if isinstance(b, dict) else getattr(b, "type", "")
            candidate = ""
            if btype == "thinking":
                candidate = b.get("thinking", "") if isinstance(b, dict) else getattr(b, "thinking", "")
            elif btype == "toolcall":
                candidate = b.get("input", "") if isinstance(b, dict) else getattr(b, "input", "")
            elif btype == "toolresult":
                candidate = b.get("output_text", "") if isinstance(b, dict) else getattr(b, "output_text", "")
            if candidate and candidate.strip():
                return candidate.strip()
        return None  # 已遍历 user 消息但无任何可读字段
    return None


def heuristic_user_intent(
    session: Session,
    *,
    max_chars: int = 1500,
    min_chars: int = 20,
) -> str | None:
    """零 LLM 启发式: 截断首条 user 消息前 ``max_chars`` 字符。

    适用场景: Router 阶段 (judge / LLM user_intent 之前) 给 TOOL_OFF_TOPIC
    嵌入层做意图参考. 短到不足以表达意图的 user 消息 (``< min_chars``)
    返回 None —— caller 应跳过嵌入检测, 避免无意义误判.

    Returns:
        截断后的 user 文本 (str); 不满足最小长度或无 user 消息返回 None.
    """
    if max_chars <= 0:
        return None
    text = _first_user_text(session)
    if not text:
        return None
    text = text.strip()
    if len(text) < min_chars:
        return None
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars].rstrip()
    log.debug(
        "heuristic_user_intent: truncated %d -> %d chars",
        len(text), len(truncated),
    )
    return truncated


def extract_user_intent_llm(session: Session, cfg: Any) -> str:
    """LLM 抽取: 把首条 user 消息压缩为 1~3 句话意图摘要。

    调用 ``prompts/user_intent.yaml`` (system + user 模板), 期望返回 JSON
    ``{"intent": "..."}``. 失败一律降级到 ``heuristic_user_intent``, 永远
    返回非空字符串 (除非连首条 user 消息都没有, 此时返回空串).

    触发配置:
      cfg.enable_user_intent_extraction (bool, 默认 True)
      cfg.user_intent_max_chars        (送 LLM 的原文上限, 默认 1500)
      cfg.user_intent_min_chars_for_extract (默认 20)
      cfg.user_intent_model            (None=走 main_model, 默认 None)
      cfg.user_intent_max_tokens       (LLM 输出预算, 默认 1024)
    """
    if not getattr(cfg, "enable_user_intent_extraction", True):
        return heuristic_user_intent(
            session,
            max_chars=int(getattr(cfg, "user_intent_max_chars", 1500)),
            min_chars=int(getattr(cfg, "user_intent_min_chars_for_extract", 20)),
        ) or ""

    max_chars = int(getattr(cfg, "user_intent_max_chars", 1500))
    min_chars = int(getattr(cfg, "user_intent_min_chars_for_extract", 20))
    model = getattr(cfg, "user_intent_model", None) or cfg.main_model
    max_tokens = int(getattr(cfg, "user_intent_max_tokens", 1024))

    raw = heuristic_user_intent(session, max_chars=max_chars, min_chars=0)
    if not raw:
        log.debug(
            "extract_user_intent_llm: no first-user text in session %s",
            getattr(session, "session_id", "?"),
        )
        return ""
    if len(raw) < min_chars:
        log.debug(
            "extract_user_intent_llm: first-user too short (%d < %d), returning raw",
            len(raw), min_chars,
        )
        return raw

    try:
        from infrastructure import LlamaCppClient
        from prompts import load_and_render, parse_json_object

        system_prompt = load_and_render("user_intent", "system")
        user_prompt = load_and_render(
            "user_intent", "user",
            user_message=raw,
        )
        client = LlamaCppClient.get(
            model, cfg=cfg, timeout=getattr(cfg, "llm_timeout_s", 120),
        )
        text, _ = client.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens, temperature=0.0,
            timeout_s=getattr(cfg, "llm_timeout_s", 120),
        )
        result = parse_json_object(text)
        intent = str(result.get("intent", "") or "").strip()
        if not intent:
            log.warning(
                "extract_user_intent_llm: parse OK but empty intent; falling back to heuristic"
            )
            return raw
        log.debug(
            "extract_user_intent_llm: %d -> %d chars intent",
            len(raw), len(intent),
        )
        return intent
    except Exception as e:
        log.warning(
            "extract_user_intent_llm failed (%s); falling back to heuristic",
            e,
        )
        return raw