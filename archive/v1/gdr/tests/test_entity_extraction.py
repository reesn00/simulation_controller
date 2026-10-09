"""实体抽取分语种分发 + jieba 集成测试。

覆盖:
  - Unicode 块快检: 日文 / 韩文 / 中文 / 英文 互不串扰
  - _extract_entities 语种路由: 日韩跳过 jieba, 中文走 jieba
  - jieba 缺包时降级到旧 1~4 字窗口
  - enable_jieba_entity_extraction=False 显式禁用
  - 通用正则部分 (引号串 / 工具名 / 数字) 跨语种稳定
  - 防御: 超长文本不崩
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.context_understanding import (
    _CJK_UNIFIED,
    _extract_entities,
    _contains_cjk_char,
    _has_non_chinese_cjk,
    _jieba_available,
)


# ---------------------------------------------------------------------------
# 语种快检: 单元
# ---------------------------------------------------------------------------


class TestLanguageDetection:
    def test_japanese_hiragana_detected(self):
        assert _has_non_chinese_cjk("こんにちは") is True

    def test_japanese_katakana_detected(self):
        assert _has_non_chinese_cjk("ブラウザ") is True

    def test_japanese_mixed_detected(self):
        # 平假名 + 片假名混排
        assert _has_non_chinese_cjk("カタカナとひらがな") is True

    def test_korean_syllables_detected(self):
        assert _has_non_chinese_cjk("안녕하세요") is True

    def test_korean_jamo_detected(self):
        # Hangul Jamo (U+1100..U+11FF) 不在常用 Syllables 块
        assert _has_non_chinese_cjk("ᄀᄁᄂ") is True

    def test_chinese_alone_not_detected(self):
        assert _has_non_chinese_cjk("北京是中国的首都") is False

    def test_english_not_detected(self):
        assert _has_non_chinese_cjk("hello world") is False

    def test_numbers_not_detected(self):
        assert _has_non_chinese_cjk("12345 67.89") is False

    def test_empty_not_detected(self):
        assert _has_non_chinese_cjk("") is False

    def test_chinese_with_english_mix_not_detected(self):
        # 中文 + 英文 + 数字, 无日韩, 视为中文路径
        assert _has_non_chinese_cjk("使用 browser 工具搜索北京") is False


class TestContainsCjkChar:
    def test_chinese_yes(self):
        assert _contains_cjk_char("北京") is True

    def test_english_no(self):
        assert _contains_cjk_char("hello") is False

    def test_japanese_no(self):
        # 日文不在 CJK Unified 主块, 返回 False
        assert _contains_cjk_char("こんにちは") is False

    def test_empty_no(self):
        assert _contains_cjk_char("") is False


class TestJiebaProbe:
    def test_jieba_probe_returns_bool(self):
        # 实际环境探测; 测试本身不会因 jieba 缺包失败
        result = _jieba_available()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _extract_entities: 通用部分
# ---------------------------------------------------------------------------


class TestUniversalExtractors:
    """引号串 / 已知工具名 / 数字 / 英文驼峰 在所有语种路径下都应工作。"""

    def test_double_quoted_strings(self):
        e = _extract_entities('参数是 "url" 与 "path" 还有 "x"')
        assert "url" in e
        assert "path" in e
        assert "x" in e

    def test_single_quoted_strings(self):
        e = _extract_entities("path is '/tmp/x'")
        assert "/tmp/x" in e

    def test_known_tool_names(self):
        e = _extract_entities("调用 browser 与 execute_shell_command")
        assert "browser" in e
        assert "execute_shell_command" in e

    def test_common_field_names(self):
        e = _extract_entities("the url and file_path are set")
        assert "url" in e
        assert "file_path" in e

    def test_numbers_with_decimals(self):
        e = _extract_entities("价格 9.99 美元, 共 100 件")
        assert "9.99" in e
        assert "100" in e

    def test_empty_text(self):
        assert _extract_entities("") == set()

    def test_universal_in_japanese_context(self):
        """日文文本中, 通用正则部分仍应抽到引号串 / 工具名 / 数字。"""
        text = 'ブラウザで "url" を取得, 価格 9.99 ドル'
        e = _extract_entities(text)
        assert "url" in e
        assert "9.99" in e
        # 工具名 browser 不在日文正文中, 不必出现; 数字 9.99 一定出现


# ---------------------------------------------------------------------------
# _extract_entities: 语种路由
# ---------------------------------------------------------------------------


class TestLanguageDispatch:
    def test_japanese_text_skips_jieba(self):
        """日文文本不应进入 jieba 路径, jieba 即使可用也不被调用。"""
        text = "ブラウザで東京の天気を確認してください"
        with patch("core.context_understanding._extract_entities_jieba") as mock_jieba:
            e = _extract_entities(text)
        mock_jieba.assert_not_called()
        # 不应含 CJK Unified 表意字符 (日文不在 CJK Unified 主块)
        for ent in e:
            for ch in ent:
                assert not (_CJK_UNIFIED.start <= ord(ch) < _CJK_UNIFIED.stop), \
                    f"日文路径不应产出 CJK Unified 字符: {ent}"

    def test_korean_text_skips_jieba(self):
        text = "서울에서 날씨를 확인하세요"
        with patch("core.context_understanding._extract_entities_jieba") as mock_jieba:
            e = _extract_entities(text)
        mock_jieba.assert_not_called()

    def test_chinese_with_japanese_marker_skips_jieba(self):
        """中日混排时检测到假名即整条走日文路径。"""
        text = "请帮我搜索 ブラウザ 然后告诉我结果"
        with patch("core.context_understanding._extract_entities_jieba") as mock_jieba:
            e = _extract_entities(text)
        mock_jieba.assert_not_called()

    def test_chinese_text_uses_jieba_when_available(self):
        """纯中文文本应触发 jieba 路径 (假设 jieba 已安装)。"""
        if not _jieba_available():
            pytest.skip("jieba not installed in test env")
        with patch("core.context_understanding._extract_entities_jieba") as mock_jieba:
            e = _extract_entities("北京是中国的首都, 用户决定去百度搜索")
        mock_jieba.assert_called_once()
        # 集合非空即视为 jieba 路径正常
        assert len(e) > 0

    def test_chinese_text_fallback_when_jieba_unavailable(self):
        """jieba 缺包时, 中文文本走旧 1~4 字窗口兜底。"""
        text = "北京是中国的首都用户决定搜索百度"
        # 模拟 jieba 不可用: 直接 patch _extract_entities_jieba 不产出 CJK
        with patch(
            "core.context_understanding._extract_entities_jieba",
            lambda *a, **k: None,  # 不添加 CJK 实体, 模拟 jieba 抽空
        ):
            e = _extract_entities(text)
        # 旧窗口兜底: 1~4 字贪婪匹配, 真实产出为 "北京是中" / "国的首都" 等
        # 验证集合含 CJK 字符且非空
        assert any(_CJK_UNIFIED.start <= ord(c) < _CJK_UNIFIED.stop for ent in e for c in ent)
        assert len(e) >= 2  # 至少抽到多个 CJK 窗口

    def test_cfg_disable_jieba_falls_back_to_window(self):
        """enable_jieba_entity_extraction=False 强制走旧 1~4 字窗口。"""
        cfg = SimpleNamespace(enable_jieba_entity_extraction=False)
        text = "北京是中国的首都, 用户决定搜索百度"
        with patch("core.context_understanding._extract_entities_jieba") as mock_jieba:
            e = _extract_entities(text, cfg=cfg)
        mock_jieba.assert_not_called()
        # 旧窗口抽到 CJK 串字符
        assert any(_CJK_UNIFIED.start <= ord(c) < _CJK_UNIFIED.stop for ent in e for c in ent)
        assert len(e) >= 2


# ---------------------------------------------------------------------------
# 防御
# ---------------------------------------------------------------------------


class TestDefensiveBehavior:
    def test_extremely_long_text_does_not_crash(self):
        """超长文本 (超过 _REGEX_LIMIT) 应被截断而不崩。"""
        text = "北京" * 300_000  # 60 万字符
        # 不抛异常
        e = _extract_entities(text)
        # 即使被截断也应返回 set
        assert isinstance(e, set)

    def test_mixed_language_with_quotes_and_tools(self):
        """中日混排 + 引号串 + 工具调用: 通用部分仍工作。"""
        text = 'call browser with "url=http://x" 日本語テスト 確認してください'
        e = _extract_entities(text)
        assert "browser" in e
        assert "url" in e
        # 双引号串整体抽出 (原 regex 行为): url=http://x 作为一个 entity
        assert "url=http://x" in e


# ---------------------------------------------------------------------------
# jieba mock 模式下的契约
# ---------------------------------------------------------------------------


class TestJiebaMocked:
    """在 jieba 不可用的环境中, 通过 mock 验证行为契约。"""

    def test_jieba_unavailable_chinese_path_still_returns_entities(self):
        """即使 jieba 不可用, 中文文本仍能抽到 CJK 实体 (旧窗口兜底)。"""
        # 模拟 jieba 探测返回 False + jieba.analyse 抽空
        with patch("core.context_understanding._jieba_available", return_value=False), \
             patch("core.context_understanding._extract_entities_jieba", lambda *a, **k: None):
            text = "北京是中国的首都, 用户决定搜索百度搜索"
            e = _extract_entities(text)
        # 旧窗口兜底: 1~4 字贪婪, 含 CJK 字符, 非空集合
        assert any(_CJK_UNIFIED.start <= ord(c) < _CJK_UNIFIED.stop for ent in e for c in ent)
        assert len(e) >= 2

    def test_jieba_mocked_extract_tags_invoked(self):
        """jieba 可用时, extract_tags 应被调用并写入 entities。"""
        # 用 mock 替换 jieba.analyse.extract_tags
        mock_extract = patch(
            "jieba.analyse.extract_tags",
            return_value=["北京", "百度", "搜索", "决定"],
        )
        if not _jieba_available():
            pytest.skip("jieba not installed")
        with mock_extract:
            text = "北京是中国的首都, 用户决定搜索百度搜索"
            e = _extract_entities(text)
        # 至少含 jieba 给出的关键词
        for kw in ["北京", "百度", "搜索"]:
            assert kw in e, f"jieba 路径应产出关键词 {kw}, 实际 entities={e}"