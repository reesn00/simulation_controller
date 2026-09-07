import json
import re
import logging
from domain import ThinkingBlock, RefineLogEntry
from prompts import parse_json_object

log = logging.getLogger(__name__)


# 修复 P1.1 (thought_refactor entity_loss 误判): 原正则会把引号里的整句
# ("m looking up the specific URLs…"/"合法在线免费观看") 视为必须保留的
# 实体, 而 9B/32B 重写时自然重组这些句子片段, 导致 entity loss 误命中、
# block 被强制丢弃. 新的实体集合只保留**高置信度硬约束实体** (URL /
# 文件路径 / 短标识符 / 工具名 / CamelCase 专名 / 数字 ID), 引号长句片段
# 不再被纳入实体集合.

_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
_FILE_PATH_PATTERN = re.compile(
    r"(?:"
    r"/[A-Za-z0-9_\-.]{3,}[^\s\"'<>]*"   # /usr/bin (unix 绝对路径, 首段 ≥3 字符)
    r"|~[\\/][^\s\"'<>]{2,}"              # ~/foo 或 ~\foo
    r"|[A-Za-z]:[\\](?!/)[^\s\"'<>]{2,}"  # C:\Users (Windows, 排除 C:/foo URL)
    r")"
)
_SHORT_QUOTED_IDENT = re.compile(r"['\"`]([A-Za-z0-9_\-./]{3,40})['\"`]")
_CAMEL_CASE_PATTERN = re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b")
_NUMERIC_ID_PATTERN = re.compile(r"\b\d{2,}\b")
_TOOL_NAME_PATTERN = re.compile(
    r"\b(browser|execute_shell_command|write_file|read_file|"
    r"search_file|list_files|glob|grep|tavily_search|"
    r"batch_web_search|web_extraction)\b",
    re.IGNORECASE,
)


def _extract_entities(text: str) -> set[str]:
    """抽取高置信度实体集合 (URL / 路径 / 短标识符 / 工具名 / CamelCase /
    数字 ID). 句子片段、常见停用词、引号长段均不再视为实体, 避免 9B/32B
    重写时因合理改写被判 entity_loss.

    实现细节: 先抽取 URL, 把 URL 占用的字符范围屏蔽, 再扫 file_path, 避免
    ``/example.com/path`` 这种 URL 内片段被误识别为 Unix 绝对路径.
    """
    entities: set[str] = set()

    # 1) URL — 优先级最高, 后续扫描要排除这些位置
    url_spans: list[tuple[int, int]] = []
    for m in _URL_PATTERN.finditer(text):
        entities.add(m.group(0))
        url_spans.append((m.start(), m.end()))

    def _in_url(pos: int) -> bool:
        return any(s <= pos < e for s, e in url_spans)

    # 2) 文件路径 — 仅扫 URL 之外的字符范围
    for m in _FILE_PATH_PATTERN.finditer(text):
        if _in_url(m.start()):
            continue
        cand = m.group(0)
        if len(cand) >= 4:
            entities.add(cand)

    # 3) 短引号标识符 (含结构特征才视为实体)
    for m in _SHORT_QUOTED_IDENT.finditer(text):
        ident = m.group(1)
        if any(ch in ident for ch in "_-/") or any(c.isdigit() for c in ident):
            entities.add(ident)
        else:
            if ident.islower() and "_" in ident:
                entities.add(ident.lower())

    # 4) CamelCase 专名 (URL 范围内也要扫 — 排除只是为了文件路径不误判)
    for m in _CAMEL_CASE_PATTERN.finditer(text):
        entities.add(m.group(1))

    # 5) 工具名 (内置白名单)
    for m in _TOOL_NAME_PATTERN.finditer(text):
        entities.add(m.group(1).lower())

    # 6) 数字 ID (≥2 位)
    for m in _NUMERIC_ID_PATTERN.finditer(text):
        entities.add(m.group(0))

    return entities


def _entities_preserved(orig: set[str], refined: set[str]) -> tuple[bool, set[str]]:
    """比较实体集合是否被保留. 对 URL/路径等结构化实体做精确比对; 对
    CamelCase 专名 / 工具名 / 数字 ID 则允许大小写不敏感比对, 减少
    误判. 返回 (是否保留, 缺失实体).
    """
    missing: set[str] = set()
    refined_lower = {e.lower() for e in refined}
    for ent in orig:
        if ent in refined:
            continue
        # 大小写不敏感 fallback (CamelCase / 工具名常见)
        if ent.lower() in refined_lower:
            continue
        # URL 子串匹配 (refiner 偶尔在尾部加斜杠或去 query)
        if ent.startswith(("http://", "https://")):
            if any(ent.startswith(p) or p.startswith(ent) for p in refined if p.startswith(("http://", "https://"))):
                continue
        missing.add(ent)
    return not missing, missing


def refine(block: ThinkingBlock, context: dict, defects: list[str], cfg) -> str | None:
    from prompts import load_and_render
    from infrastructure import LlamaCppClient

    has_defect = any(
        d in defects for d in ["thought_too_short", "thought_too_long", "thought_broken_logic"]
    )
    if not has_defect:
        return block.thinking

    system_prompt = load_and_render("thought", "system")
    user_prompt = load_and_render(
        "thought", "user",
        original_thinking=block.thinking,
        context=json.dumps(context, ensure_ascii=False),
        defects=", ".join(defects),
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_error = None
    for attempt in range(cfg.max_retries_9b):
        retry_messages = messages
        if attempt > 0:
            retry_messages = list(messages) + [
                {"role": "user", "content": "上轮你只给了分析说明, 没有输出 JSON。请立即输出一个 ```json {\"refined_thought\": \"...\"} ``` 代码块, refined_thought 必须是修正后的 Thought 文本。"}
            ]
        try:
            client = LlamaCppClient.get(cfg.main_model, cfg=cfg, timeout=cfg.llm_timeout_s)
            text, meta = client.chat(retry_messages, max_tokens=1536)
            result = parse_json_object(text)
            refined = result.get("refined_thought", "")
            if not refined:
                raise ValueError("empty refined_thought")
            # 修复 P1.2: 在 thought_max_len 基础上加 grace_pct% 余量,
            # 避免 501 vs 500 这类单字符临界误杀.
            _grace = max(1, int(cfg.thought_max_len * getattr(cfg, "thought_max_len_grace_pct", 10) / 100))
            _max_with_grace = cfg.thought_max_len + _grace
            if len(refined) < cfg.thought_min_len or len(refined) > _max_with_grace:
                raise ValueError(f"length out of range: {len(refined)}")
            orig_entities = _extract_entities(block.thinking)
            new_entities = _extract_entities(refined)
            preserved, missing = _entities_preserved(orig_entities, new_entities)
            if not preserved:
                log.warning("entity loss in block %s: %s", block.id, sorted(missing)[:8])
                raise ValueError(f"entity loss: {sorted(missing)[:8]}")
            return refined
        except Exception as e:
            last_error = str(e)
            log.debug("thought_refactor attempt %d failed: %s; raw=%r", attempt + 1, e, text[:400] if isinstance(text, str) else text)
            continue

    try:
        log.warning("escalation to 32B for block %s", block.id)
        client = LlamaCppClient.get(cfg.tool_model, cfg=cfg, timeout=cfg.llm_timeout_s)
        text, meta = client.chat(messages, max_tokens=1536)
        result = parse_json_object(text)
        refined = result.get("refined_thought", "")
        # 修复 P1.2: 32B 升级路径同样应用 grace 上限.
        _grace = max(1, int(cfg.thought_max_len * getattr(cfg, "thought_max_len_grace_pct", 10) / 100))
        _max_with_grace = cfg.thought_max_len + _grace
        if refined and cfg.thought_min_len <= len(refined) <= _max_with_grace:
            orig_entities = _extract_entities(block.thinking)
            new_entities = _extract_entities(refined)
            preserved, _missing = _entities_preserved(orig_entities, new_entities)
            if preserved:
                return refined
    except Exception as e:
        last_error = str(e)

    log.error("discard block %s, reason=thought_refactor_exhausted: %s", block.id, last_error)
    return None