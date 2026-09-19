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
# 修复 P1.3 (再加固): path 前面不能紧跟字母数字 / 点 / 左括号 —— 防止 prose 里
# 的非正式 URL path 片段 (如 "m.iqiyi.com/a_19rrk2hct9.html)" "/a_xxx).") 被误
# 当成 Unix 路径. 真实 Unix 绝对路径前面通常是空格/标点/行首; 真实 Windows
# 路径独立分支不受此约束. 同时贪婪部分仍然排除 `;`, `(`, `)`, `,` 等 URL
# 边界标点, 兜底.
_FILE_PATH_PATTERN = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9.(])/[A-Za-z0-9_\-.]{3,}[^\s\"'<>();,]*"  # /usr/bin
    r"|~[\\/][^\s\"'<>();,]{2,}"                                # ~/foo
    r"|[A-Za-z]:[\\](?!/)[^\s\"'<>();,]{2,}"                    # C:\Users
    r")"
)

# 修复 P1.4: decision / meta-reasoning 类 thinking 强制跳过 thought_refactor.
# 这类内容核心是 policy decision ("let me be pragmatic and decide...") 或
# composition planning ("now compose the answer..."), 改写既无必要又会反复
# 失败: 9B/32B 倾向输出 "解释如何改" 而非 JSON, 或按语法正确化丢掉伪实体.
# 关键词命中数 ≥ _DECISION_HIT_THRESHOLD 才触发跳过, 防止 "let me check..."
# 这类真实探索性 thinking 被误伤.
_DECISION_MARKERS = (
    "let me decide",
    "let me be pragmatic",
    "let me just",
    "policy decision",
    "i'll instead",
    "i will instead",
    "now compose",
    "compose the answer",
    "compose the response",
    "headline format",
    "i'll offer",
    "rather than just",
    "per the ",
)
_DECISION_HIT_THRESHOLD = 2


def _is_decision_or_meta_reasoning(text: str) -> bool:
    """启发式: thinking 含 ≥2 个 decision / meta-reasoning 标记 → 跳过 LLM 改写.

    单个标记不足以判定 ("let me check the actual download links for this first"
    也是真实探索, 不能判 decision); 多个标记重合才足以认为是决策/组合阶段.
    """
    lowered = text.lower()
    hits = sum(1 for m in _DECISION_MARKERS if m in lowered)
    return hits >= _DECISION_HIT_THRESHOLD
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
    """比较实体集合是否被保留. 严格匹配: 实体原字面值必须在 refined 中
    出现. 修复 P1.7: 撤销 P1.6 的 host 弹性豁免 — 训练数据要求 reasoning
    链与 final text URL 逐字一致 (www.iqiyi.com ≠ m.iqiyi.com 是不同
    context), LLM 改写时禁止 host 标准化; 既然 prompt 已硬约束, 这里
    检测严格化即可, 双重保险.

    对 URL 仍保留 prefix 兜底 (refined 含 orig 路径或反之), 因 LLM 在 URL
    末尾加 query / 去 query 算保留; 但 host 变化必须判 missing.

    对 CamelCase / 工具名保留大小写不敏感比对 (LLM 改写时常大小写变形).
    返回 (是否保留, 缺失实体).
    """
    missing: set[str] = set()
    refined_lower = {e.lower() for e in refined}
    for ent in orig:
        if ent in refined:
            continue
        # 大小写不敏感 fallback (CamelCase / 工具名常见)
        if ent.lower() in refined_lower:
            continue
        # URL prefix 兜底: refiner 偶尔在 orig URL 末尾加 ?query 或去 query
        # 算保留. 但 host 必须一致 — P1.7 修复后这点已由 prompt 硬约束.
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

    # 修复 P1.4: decision / meta-reasoning 类 thinking 直接保留原文, 不进 LLM.
    # 这类内容的核心是 policy decision / composition planning, LLM 改写要么
    # 输出"如何改"的解释 (empty refined_thought), 要么按语法修复丢伪实体
    # (entity loss), 反复失败. 保留原文 → judge 仍能基于原 thinking 评分.
    if _is_decision_or_meta_reasoning(block.thinking):
        log.info(
            "skipping thought_refactor (decision/meta-reasoning) for block %s",
            block.id,
        )
        return block.thinking

    system_prompt = load_and_render("thought", "system")
    # 修复 P1.7: 显式注入实体清单作为硬约束, 让 LLM 在改写时逐字保留
    # (URL host / 工具名 / 数字 ID 等). 不注入时 LLM 会"善意"地做 mobile
    # 标准化 (www↔m) 等导致 reasoning 链与 final text URL 不一致, 训练
    # 数据隐性 bug.
    orig_entities_set = _extract_entities(block.thinking)
    orig_entities_sorted = sorted(orig_entities_set)
    user_prompt = load_and_render(
        "thought", "user",
        original_thinking=block.thinking,
        context=json.dumps(context, ensure_ascii=False),
        defects=", ".join(defects),
        entities=", ".join(orig_entities_sorted) if orig_entities_sorted else "(无)",
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
            new_entities = _extract_entities(refined)
            preserved, missing = _entities_preserved(orig_entities_set, new_entities)
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
            new_entities = _extract_entities(refined)
            preserved, _missing = _entities_preserved(orig_entities_set, new_entities)
            if preserved:
                return refined
    except Exception as e:
        last_error = str(e)

    log.error("discard block %s, reason=thought_refactor_exhausted: %s", block.id, last_error)
    return None