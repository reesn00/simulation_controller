"""etl.qwenformat.tool_output_summarizer: 工具返回内容的精简 (SFT 数据构建).

分层策略 (按确认的设计, 无硬截断层):

    L0 规则预清洗
        只清理确定性、无语义的字符级/行级噪声, 不碰内容本身:
        emoji / Private Use Area 字形 / variation selector / ZWJ / ANSI 转义 /
        制表符 (→ 2 空格) / CRLF 归一 / 行尾空白 / 连续重复行 / 多空行折叠.
        不截断、不删整段、不按关键词过滤 —— 语义判断全部留给 L1.

    L1 LLM 锚点摘要
        以「用户 query」为任务相关性主锚点, 「tool_input」为辅助锚点,
        让小型 instruct 模型从冗长返回中提取与任务直接相关的内容.
        ``assistant_response`` 不进摘要 prompt, 只在质量门做覆盖性校验.

    失败兜底: 保留完整内容
        LLM 调用失败 / 质量门不通过 → 保留 L0 清洗后的完整原始内容.
        宁可多留, 不可错删 —— 保留完整内容好于粗糙截断.

质量门 (校验可信性, 不校验长度):
    1. 格式: LLM 输出 JSON 可解析, summary 非空;
       relevant=true 时 kept_facts 必须非空.
    2. 忠实性: kept_facts 的核心片段在 raw 中命中率 >= faith_threshold,
       否则判为幻觉风险.
    3. 覆盖性 (可选): assistant_response 中出现且来自 raw 的 URL / 数字,
       必须出现在 summary 中.

结构化工具 (``STRUCTURED_TOOLS``: shell / 文件读写 / 搜索) 不走 LLM,
默认原样保留 + L0 清洗 —— 这些输出本身是代码/日志, 完整内容优于摘要.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from etl.qwenformat.load import (
    SessionRecord,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# L0 规则预清洗
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+")
_NUMBER_RE = re.compile(r"\d{2,}")


def _is_droppable_char(ch: str) -> bool:
    """emoji / PUA / variation selector / ZWJ 等无语义字形."""
    cp = ord(ch)
    if ch in ("\n", "\t"):
        return False
    if 0xE000 <= cp <= 0xF8FF:          # Private Use Area (如 )
        return True
    if 0xFE00 <= cp <= 0xFE0F:          # variation selectors
        return True
    if cp == 0x200D:                    # zero width joiner
        return True
    cat = unicodedata.category(ch)
    if cat == "So":                     # emoji / 其他符号
        return True
    if cat == "Cf":                     # 不可见格式控制字符
        return True
    if cat in ("Cc", "Cs"):             # 其他控制字符
        return True
    return False


def clean_l0(text: str) -> tuple[str, int]:
    """L0 规则预清洗. 返回 (cleaned, removed_chars).

    幂等纯函数; 对任意工具输出都可安全执行.
    """
    if not text:
        return text or "", 0
    original_len = len(text)

    s = _ANSI_RE.sub("", text)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\t", "  ")
    s = "".join(ch for ch in s if not _is_droppable_char(ch))

    lines = [line.rstrip() for line in s.split("\n")]
    # 连续重复行去重 (空行不重置 prev, 样本里的演员表重复块由空行分隔) + 多空行折叠
    out_lines: list[str] = []
    prev = None
    blank_run = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank_run += 1
            if blank_run > 1:
                continue
            out_lines.append("")
            continue
        blank_run = 0
        if prev is not None and stripped == prev:
            # 若已追加了空行, 把它撤回, 避免 dup 删除后留下悬挂空行
            if out_lines and out_lines[-1] == "":
                out_lines.pop()
                blank_run = 1
            continue
        out_lines.append(line)
        prev = stripped
    s = "\n".join(out_lines).strip("\n")
    return s, max(original_len - len(s), 0)


# ---------------------------------------------------------------------------
# 上下文 / 协议
# ---------------------------------------------------------------------------

STRUCTURED_TOOLS: frozenset[str] = frozenset({
    "execute_shell_command",
    "write_file",
    "read_file",
    "edit_file",
    "grep_search",
    "glob_search",
})


@dataclass(frozen=True)
class ToolOutputContext:
    """L1 摘要的锚点上下文."""

    user_query: str = ""                     # 主锚点: 本轮 turn_start.input_text
    tool_input: Optional[dict[str, Any]] = None  # 辅助锚点: 工具调用参数
    tool_call_id: str = ""
    assistant_response: str = ""             # 仅质量门覆盖性校验使用


class ToolOutputSummarizer(Protocol):
    def summarize(self, tool_name: str, raw_output: str, ctx: ToolOutputContext) -> str:
        """返回精简后的输出. 实现必须保证失败时返回完整内容 (L0 之后)."""
        ...


# ---------------------------------------------------------------------------
# Prompt / 解析 / 质量门
# ---------------------------------------------------------------------------

_SUMMARY_PROMPT = """你是数据清洗助手，为 Agent SFT 训练数据精简工具返回内容。

【用户任务】
{user_query}

【工具调用】
工具: {tool_name}
参数: {tool_input}

【工具原始返回】
{raw_output}

请从用户任务的视角审视这段返回：
1. 只保留与完成用户任务直接相关的内容（被保留的内容应能回答或支撑用户的问题）
2. 去除与任务无关的页面噪声：导航栏、广告、登录提示、长目录/剧集列表、版权页脚、重复 UI 文案
3. 保持原文事实的准确性，禁止添加原文没有的信息，禁止改写数字、日期、URL
4. 如果整段返回与任务无关，原样保留（不要强行编造关联）

以 JSON 输出（不要多余文字）：
{{"summary": "精简后的文本", "kept_facts": ["保留的关键事实1", "..."], "relevant": true/false}}"""

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

# kept_facts 忠实性校验时取事实串的核前缀长度
_FACT_CORE_LEN = 60


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _parse_llm_json(text: str) -> Optional[dict[str, Any]]:
    """从 LLM 输出中提取 JSON 对象."""
    if not text:
        return None
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _fact_hit(fact: str, raw_norm: str) -> bool:
    core = _normalize_for_match(fact)[:_FACT_CORE_LEN]
    if not core:
        return False
    if core in raw_norm:
        return True
    # 5 字 shingle 命中 70% 也算命中 (容错 LLM 轻微改写连接词)
    if len(core) >= 5:
        shingles = {core[i:i + 5] for i in range(len(core) - 4)}
        if shingles:
            hits = sum(1 for sh in shingles if sh in raw_norm)
            return hits / len(shingles) >= 0.7
    return False


@dataclass
class GateResult:
    passed: bool
    reason: str = ""


def _gate_format(obj: Optional[dict[str, Any]]) -> GateResult:
    """质量门 1: JSON 格式与必备字段."""
    if obj is None:
        return GateResult(False, "unparseable_json")
    summary = obj.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return GateResult(False, "empty_summary")
    relevant = bool(obj.get("relevant", True))
    kept = obj.get("kept_facts")
    if relevant and (not isinstance(kept, list) or not kept):
        return GateResult(False, "empty_kept_facts")
    return GateResult(True)


def _gate_faithfulness(
    kept_facts: list[Any], raw_output: str, threshold: float,
) -> GateResult:
    """质量门 2: kept_facts 在 raw 中的命中率."""
    facts = [f for f in kept_facts if isinstance(f, str) and f.strip()]
    if not facts:
        return GateResult(True)
    raw_norm = _normalize_for_match(raw_output)
    hits = sum(1 for f in facts if _fact_hit(f, raw_norm))
    ratio = hits / len(facts)
    if ratio < threshold:
        return GateResult(False, f"faithfulness={ratio:.2f}<{threshold:.2f}")
    return GateResult(True)


def _gate_coverage(summary: str, raw_output: str, assistant_response: str) -> GateResult:
    """质量门 3: assistant 引用且来自 raw 的 URL / 数字必须出现在 summary."""
    if not assistant_response:
        return GateResult(True)
    anchors = set(_URL_RE.findall(assistant_response))
    anchors |= set(_NUMBER_RE.findall(assistant_response))
    missing = [
        a for a in anchors
        if a in raw_output and _normalize_for_match(a) not in _normalize_for_match(summary)
    ]
    if missing:
        return GateResult(False, f"coverage_missing={missing[:3]}")
    return GateResult(True)


# ---------------------------------------------------------------------------
# LLM 锚点摘要器
# ---------------------------------------------------------------------------

LLMCaller = Callable[[str], str]
"""输入完整 user prompt, 返回模型原始文本输出."""


def _default_llm_caller(
    base_url: str, api_key: str, model: str, timeout: float,
) -> LLMCaller:
    """构造 OpenAI 兼容 /chat/completions 调用器 (同步 httpx)."""
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def call(prompt: str) -> str:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 4096,
        }
        last_exc: Optional[Exception] = None
        for _ in range(2):
            try:
                resp = httpx.post(url, json=body, headers=headers, timeout=timeout)
                resp.raise_for_status()
                data = resp.json()
                return str(data["choices"][0]["message"]["content"] or "")
            except Exception as exc:  # noqa: BLE001 - 失败走兜底
                last_exc = exc
        raise RuntimeError(f"summarizer LLM call failed: {last_exc}")

    return call


class LLMAnchoredSummarizer:
    """L0 规则预清洗 + L1 LLM 锚点摘要 + 质量门, 失败保留完整内容.

    Args:
        llm_caller: 可注入的 LLM 调用器 (测试用 mock).
        threshold_chars: L0 后短于该长度的输出跳过 LLM, 直接保留.
        faith_threshold: kept_facts 忠实性命中率下限.
        cache_dir: 摘要缓存目录 (sha256(raw+锚点).json); None 表示不缓存.
    """

    def __init__(
        self,
        *,
        llm_caller: LLMCaller,
        threshold_chars: int = 800,
        faith_threshold: float = 0.7,
        cache_dir: Optional[Path] = None,
        stats: Optional[dict[str, int]] = None,
    ) -> None:
        self._llm_caller = llm_caller
        self._threshold_chars = threshold_chars
        self._faith_threshold = faith_threshold
        self._cache_dir = Path(cache_dir) if cache_dir else None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._stats = stats if stats is not None else {}

    # ------------------------------------------------------------------
    # env 构造
    # ------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        *,
        cache_dir: Optional[Path] = None,
        stats: Optional[dict[str, int]] = None,
    ) -> Optional["LLMAnchoredSummarizer"]:
        """按 env 构造; QF_SUMMARIZER_ENABLED 未开或配置不全时返回 None.

        环境变量:
            QF_SUMMARIZER_ENABLED        "1"/"true" 开启 (默认关)
            QF_SUMMARIZER_BASE_URL       OpenAI 兼容端点
            QF_SUMMARIZER_API_KEY
            QF_SUMMARIZER_MODEL
            QF_SUMMARIZER_THRESHOLD_CHARS  默认 800
        """
        enabled = os.environ.get("QF_SUMMARIZER_ENABLED", "").lower() in ("1", "true", "yes")
        if not enabled:
            return None
        base_url = os.environ.get("QF_SUMMARIZER_BASE_URL", "")
        model = os.environ.get("QF_SUMMARIZER_MODEL", "")
        api_key = os.environ.get("QF_SUMMARIZER_API_KEY", "")
        if not base_url or not model:
            logger.warning("QF_SUMMARIZER_ENABLED=1 but base_url/model missing; summarizer disabled")
            return None
        threshold = int(os.environ.get("QF_SUMMARIZER_THRESHOLD_CHARS", "800"))
        return cls(
            llm_caller=_default_llm_caller(base_url, api_key, model, timeout=60.0),
            threshold_chars=threshold,
            cache_dir=cache_dir,
            stats=stats,
        )

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------

    def _cache_key(self, raw_output: str, ctx: ToolOutputContext) -> str:
        h = hashlib.sha256()
        h.update(raw_output.encode("utf-8"))
        h.update(b"\x00")
        h.update(ctx.user_query.encode("utf-8"))
        h.update(b"\x00")
        h.update(json.dumps(ctx.tool_input, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        return h.hexdigest()

    def _cache_get(self, key: str) -> Optional[str]:
        if not self._cache_dir:
            return None
        path = self._cache_dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        summary = data.get("summary")
        return summary if isinstance(summary, str) and summary else None

    def _cache_put(self, key: str, summary: str) -> None:
        if not self._cache_dir:
            return
        path = self._cache_dir / f"{key}.json"
        path.write_text(
            json.dumps({"summary": summary}, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def _bump(self, key: str, value: int = 1) -> None:
        self._stats[key] = self._stats.get(key, 0) + value

    def summarize(self, tool_name: str, raw_output: str, ctx: ToolOutputContext) -> str:
        l0_cleaned, removed = clean_l0(raw_output)
        if removed:
            self._bump("l0_chars_removed", removed)
        self._bump("tool_output_chars_before", len(raw_output))

        if not l0_cleaned.strip():
            self._bump("tool_summaries_empty")
            self._bump("tool_output_chars_after", 0)
            return ""

        # 结构化工具: 输出本身是代码/日志, 完整内容优于摘要
        if tool_name in STRUCTURED_TOOLS:
            self._bump("tool_summaries_structured")
            self._bump("tool_output_chars_after", len(l0_cleaned))
            return l0_cleaned

        # 短输出: 不值得走 LLM
        if len(l0_cleaned) < self._threshold_chars:
            self._bump("tool_summaries_skipped_short")
            self._bump("tool_output_chars_after", len(l0_cleaned))
            return l0_cleaned

        cache_key = self._cache_key(l0_cleaned, ctx)
        cached = self._cache_get(cache_key)
        if cached is not None:
            self._bump("tool_summaries_cache_hit")
            self._bump("tool_output_chars_after", len(cached))
            return cached

        # L1: LLM 锚点摘要
        prompt = _SUMMARY_PROMPT.format(
            user_query=ctx.user_query or "(无)",
            tool_name=tool_name,
            tool_input=json.dumps(ctx.tool_input, ensure_ascii=False)
            if ctx.tool_input is not None else "(无)",
            raw_output=l0_cleaned,
        )
        try:
            llm_text = self._llm_caller(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool output summarization failed (%s); keep full content", exc)
            self._bump("tool_summaries_fallback")
            self._bump("tool_output_chars_after", len(l0_cleaned))
            return l0_cleaned

        obj = _parse_llm_json(llm_text)
        if obj is not None and obj.get("relevant") is False:
            # 模型判断整段与任务无关 → 按 prompt 约定原样保留 (允许空 summary)
            self._bump("tool_summaries_irrelevant")
            self._bump("tool_output_chars_after", len(l0_cleaned))
            return l0_cleaned
        gate = _gate_format(obj)
        if gate.passed:
            gate = _gate_faithfulness(
                obj.get("kept_facts") or [], l0_cleaned, self._faith_threshold,
            )
            if not gate.passed:
                self._bump("faithfulness_violations")
        if gate.passed:
            gate = _gate_coverage(obj.get("summary", ""), l0_cleaned, ctx.assistant_response)
        if not gate.passed:
            logger.info("tool output gate failed (%s); keep full content", gate.reason)
            self._bump("tool_summaries_gate_failed")
            self._bump("tool_output_chars_after", len(l0_cleaned))
            return l0_cleaned

        summary = str(obj["summary"]).strip()
        self._cache_put(cache_key, summary)
        self._bump("tool_summaries_llm")
        self._bump("tool_output_chars_after", len(summary))
        return summary


# ---------------------------------------------------------------------------
# SessionRecord 级遍历
# ---------------------------------------------------------------------------


def summarize_record(
    record: SessionRecord,
    summarizer: ToolOutputSummarizer,
    *,
    stats: Optional[dict[str, int]] = None,
) -> dict[str, int]:
    """对 record 中所有 ToolResultBlock 执行摘要 (就地修改).

    - ``raw_output`` 存入 ``block.metadata["raw_output"]`` 供审计;
      摘要写入 ``block.output_text``.
    - 锚点: ``user_query`` 取首个 user message 文本;
      ``tool_input`` 由同 id ToolCallBlock.input 解析;
      ``assistant_response`` 取最后一个含文本的 assistant message (仅质量门).
    """
    local_stats: dict[str, int] = stats if stats is not None else {}

    user_query = ""
    for m in record.messages:
        if m.role == "user":
            texts = [b.text for b in m.blocks if isinstance(b, TextBlock)]
            user_query = "".join(texts)
            if user_query:
                break

    assistant_response = ""
    for m in reversed(record.messages):
        if m.role == "assistant":
            texts = [b.text for b in m.blocks if isinstance(b, TextBlock)]
            if texts:
                assistant_response = "".join(texts)
                break

    call_inputs: dict[str, Optional[dict[str, Any]]] = {}
    for m in record.messages:
        if m.role != "assistant":
            continue
        for b in m.blocks:
            if isinstance(b, ToolCallBlock):
                try:
                    parsed = json.loads(b.input)
                    call_inputs[b.id] = parsed if isinstance(parsed, dict) else None
                except (json.JSONDecodeError, ValueError):
                    call_inputs[b.id] = None

    for m in record.messages:
        for b in getattr(m, "blocks", []):
            if not isinstance(b, ToolResultBlock):
                continue
            raw = b.output_text or ""
            if not raw.strip():
                continue
            b.metadata["raw_output"] = raw
            ctx = ToolOutputContext(
                user_query=user_query,
                tool_input=call_inputs.get(b.id),
                tool_call_id=b.id,
                assistant_response=assistant_response,
            )
            b.output_text = summarizer.summarize(b.name, raw, ctx)
            local_stats["tool_results_processed"] = local_stats.get("tool_results_processed", 0) + 1

    # 汇总 summarizer 内部统计
    inner = getattr(summarizer, "_stats", None)
    if isinstance(inner, dict):
        for k, v in inner.items():
            local_stats[k] = local_stats.get(k, 0) + v
        inner.clear()
    return local_stats
