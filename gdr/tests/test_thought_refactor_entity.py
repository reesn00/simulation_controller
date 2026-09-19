"""回归测试 thought_refactor 实体守恒 (修复 P1.1)。

原 _extract_entities 把引号内的整句 ("m looking up the specific URLs…")
视为必须保留的实体, 9B/32B 重写时自然重组这些句子片段被判 entity loss,
导致整 block 被丢弃. 新实现只保留高置信度实体 (URL / 路径 / 短标识符 /
工具名 / CamelCase / 数字 ID), 引号长句不再视作实体.
"""
from __future__ import annotations

import pytest

from refiners import thought_refactor as tr


def test_extract_keeps_url():
    text = "see https://tv.cctv.com/2013/04/12/VIDA1365761375108643.shtml for source"
    ents = tr._extract_entities(text)
    assert any(e.startswith("https://") for e in ents)


def test_extract_keeps_tool_names():
    text = "use browser and execute_shell_command to gather data"
    ents = {e.lower() for e in ents_orig(text)}
    assert "browser" in ents
    assert "execute_shell_command" in ents


def test_extract_keeps_camel_case():
    text = "CamelCaseId and YouTubeChannel are valid"
    ents = tr._extract_entities(text)
    assert "CamelCaseId" in ents
    assert "YouTubeChannel" in ents


def test_extract_keeps_numeric_id():
    text = "version v1.2.3 and year 1964 and big 50"
    ents = tr._extract_entities(text)
    assert "1964" in ents or "50" in ents


def test_extract_ignores_long_quoted_sentence():
    """原 bug: 引号里的整句被视作实体. 修复后只保留有结构特征的短标识符."""
    text = (
        "m looking up the specific URLs for both the CCTV and Youku pages "
        "to see what content is available and whether it s copyright protection "
        "would have expired in 2015 (50 years from 1964), making it public domain"
    )
    ents = tr._extract_entities(text)
    # 整句不该出现在实体集合里
    joined = " ".join(ents)
    assert "looking up the specific URLs" not in joined
    assert "available and whether it" not in joined


def test_extract_ignores_chinese_quoted_sentence():
    """中文引号长句同样不应被当成实体."""
    text = "用户问题：'合法在线免费观看' 应该如何处理？"
    ents = tr._extract_entities(text)
    joined = " ".join(ents)
    assert "合法在线免费观看" not in joined


def test_extract_keeps_short_quoted_identifier():
    """短且有结构特征 (含下划线/连字符/数字) 的引号标识符仍视为实体."""
    text = "use the 'tavily_search' tool with 'api-key-2024' config"
    ents = tr._extract_entities(text)
    assert any("tavily_search" in e for e in ents)
    assert any("api-key-2024" in e for e in ents)


def test_entities_preserved_case_insensitive():
    """CamelCase / 工具名大小写不敏感比对 (refiner 仅大小写变形不应误判).

    真实场景: refiner 改写时把 CamelCase 标识符大小写变形, 或工具名变大小写,
    不应被判 entity_loss. 这里 TavilySearch 在 orig, 改写后变成小写
    tavilysearch (但仍是同一 token), 不应判 missing.
    """
    # 直接构造 orig/refined entity 集合 (覆盖 _extract_entities 自身的差异)
    orig = {"TavilySearch", "browser"}
    refined = {"tavilysearch", "browser"}
    preserved, missing = tr._entities_preserved(orig, refined)
    assert preserved, f"missing: {missing}"


def test_entities_preserved_url_substring():
    """URL 子串匹配 (refiner 偶尔在尾部加斜杠或去 query) 不应误判."""
    orig = tr._extract_entities("see https://example.com/path?q=1 for source")
    refined = tr._extract_entities("see https://example.com/path for source")
    preserved, missing = tr._entities_preserved(orig, refined)
    assert preserved, f"missing: {missing}"


def test_entities_preserved_strict():
    """真正丢失的 URL 仍能识别."""
    orig = tr._extract_entities("see https://example.com/path for source")
    refined = tr._extract_entities("see generic page for source")
    preserved, missing = tr._entities_preserved(orig, refined)
    assert not preserved
    assert any("example.com" in m for m in missing)


# ---------------------------------------------------------------------------
# P1.7: 撤销 P1.6 的 host 弹性豁免. URL entity 必须严格匹配, host 变化
# (www↔m / http↔https 等"mobile 标准化") 视为 entity loss — 训练数据要求
# reasoning 链与 final text URL 逐字一致, host 改了 reasoning 上下文就
# 矛盾. prompt 已硬约束 LLM 不改实体, 检测严格化是双重保险.
# ---------------------------------------------------------------------------


def test_entities_preserved_strict_on_www_to_mobile():
    """www ↔ m host 切换必须判 entity loss.

    修复方向从 P1.6 反转: P1.6 把这种情况判 preserved (放宽), 但这掩盖了
    LLM 改写时违反"实体不可改"约束的隐性 bug. 严格化后这种违规会被检测
    抓住 → retry → 仍违规 → discard, 防止坏训练数据流入.
    """
    orig = tr._extract_entities(
        "Confirmed: https://www.iqiyi.com/a_19rrk2hct9.html is live"
    )
    refined = tr._extract_entities(
        "Confirmed: https://m.iqiyi.com/a_19rrk2hct9.html is live"
    )
    preserved, missing = tr._entities_preserved(orig, refined)
    assert not preserved, (
        "host 变化 (www↔m) 必须判 entity loss, 不能放宽 (P1.7 撤销 P1.6)"
    )
    assert any("iqiyi.com/a_19rrk2hct9" in m for m in missing)


def test_entities_preserved_strict_on_http_to_https():
    """scheme 变化 (http ↔ https) 同样判 entity loss."""
    orig = tr._extract_entities("see http://example.com/path for source")
    refined = tr._extract_entities("see https://example.com/path for source")
    preserved, missing = tr._entities_preserved(orig, refined)
    assert not preserved, "scheme 切换应判 entity loss (P1.7 严格化)"
    assert any("example.com/path" in m for m in missing)


def test_entities_preserved_query_appended_still_preserved():
    """query 追加 (?utm=xxx) 仍 preserved, 历史 prefix 兜底兼容.

    refiner 在 orig URL 末尾加 ?utm_source / ?from=mobile 等 tracking 参数
    不算 URL 丢失, reasoning 链仍指向同一资源. 这一行为在 P1.6 之前已
    存在, P1.7 保留.
    """
    orig_ents = {"https://example.com/path"}
    refined_ents = {"https://example.com/path?utm=x"}
    preserved, missing = tr._entities_preserved(orig_ents, refined_ents)
    assert preserved, (
        f"query 追加 (refined 包含 orig 前缀) 应 preserved, missing={missing}"
    )
    assert not missing


def test_entities_preserved_strict_on_path_change():
    """path 不同 → missing (路径差异常被视为不同 URL)."""
    orig_ents = {"https://example.com/path_a"}
    refined_ents = {"https://example.com/path_b"}
    preserved, missing = tr._entities_preserved(orig_ents, refined_ents)
    assert not preserved
    assert any("path_a" in m for m in missing)


def test_entities_preserved_url_fully_removed_still_detected():
    """URL 完全删掉仍判 missing."""
    orig = tr._extract_entities("see https://example.com/path for source")
    refined = tr._extract_entities("see generic page for source")
    preserved, missing = tr._entities_preserved(orig, refined)
    assert not preserved
    assert any("example.com" in m for m in missing)


def ents_orig(text: str) -> list[str]:
    return list(tr._extract_entities(text))
