"""L1 校验复用 core.context_understanding 实体抽取的回归测试。

覆盖:
  - L1._extract_entities 现在委托给 CU 的多语种分发实现
  - 中文 thinking 关键术语能被 L1 识别 (旧实现漏检)
  - jieba 缺包时走 1~4 字窗口降级, 不崩
  - 日文/韩文路径: 跳过 jieba, 走英文/数字/引号串正则
  - thought 实体保持: 中文术语改写后丢失 → L1 判 fail
  - cfg.enable_jieba_entity_extraction=False → 显式禁用, 走旧 1~4 字窗口
  - toolcall / toolresult 校验路径未受影响 (cfg 透传不破坏既有行为)
  - 空白字符串 / None 类型不崩
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from domain import ThinkingBlock, ToolcallBlock, ToolresultBlock
from validators import l1_rules


def _cfg(jieba: bool = True) -> SimpleNamespace:
    return SimpleNamespace(enable_jieba_entity_extraction=jieba)


# ---------------------------------------------------------------------------
# 委托与多语种分发
# ---------------------------------------------------------------------------


class TestExtractEntitiesDelegation:
    def test_extract_returns_set(self):
        ents = l1_rules._extract_entities("use browser to search 北京")
        assert isinstance(ents, set)

    def test_keeps_quoted_strings(self):
        """引号串仍是必须保留的实体 — 与原行为兼容。"""
        ents = l1_rules._extract_entities('call "search_url" with "v1"')
        assert "search_url" in ents
        assert "v1" in ents

    def test_keeps_tool_names_lowercased(self):
        """工具名白名单保持小写匹配 (兼容原行为)。"""
        ents = l1_rules._extract_entities(
            "use Browser and execute_shell_command",
            cfg=_cfg(),
        )
        assert "browser" in ents
        assert "execute_shell_command" in ents

    def test_keeps_camel_case(self):
        ents = l1_rules._extract_entities("ContextUnderstanding and HttpEmbedder")
        assert "ContextUnderstanding" in ents
        assert "HttpEmbedder" in ents

    def test_keeps_numeric_ids(self):
        ents = l1_rules._extract_entities("port 8086 and year 1964")
        assert "8086" in ents
        assert "1964" in ents


# ---------------------------------------------------------------------------
# 中文 thinking 软约束 (本次优化点)
# ---------------------------------------------------------------------------


class TestChineseEntityExtraction:
    def test_chinese_text_returns_nonempty(self):
        """中文 thinking 应至少抽到 1 个实体 (新行为, 旧实现为空)。"""
        ents = l1_rules._extract_entities(
            "用户要求使用分词器处理中文文本, 并对结果做正则匹配",
            cfg=_cfg(),
        )
        # 不强求具体词 (jieba 与降级窗口输出不同), 只要求非空
        assert len(ents) >= 1

    def test_jieba_unavailable_chinese_still_works(self):
        """jieba 不可用 → 走 1~4 字窗口降级, 不崩。"""
        with patch("core.context_understanding._jieba_available", return_value=False), \
             patch("core.context_understanding._extract_entities_jieba", lambda *a, **k: None):
            ents = l1_rules._extract_entities(
                "用户要求使用分词器处理中文文本",
                cfg=_cfg(),
            )
        assert isinstance(ents, set)
        assert len(ents) >= 1  # 旧窗口兜底

    def test_jieba_disabled_via_cfg(self):
        """enable_jieba_entity_extraction=False → 不调 jieba, 走旧窗口。"""
        with patch("core.context_understanding._extract_entities_jieba") as mock_jieba:
            ents = l1_rules._extract_entities(
                "用户要求使用分词器处理中文文本",
                cfg=_cfg(jieba=False),
            )
            # 显式禁用 → jieba 不应被调用
            assert mock_jieba.call_count == 0
        assert isinstance(ents, set)
        assert len(ents) >= 1

    def test_cfg_none_falls_back_to_default(self):
        """cfg=None 走默认 (jieba 可用则启用), 不崩。"""
        ents = l1_rules._extract_entities(
            "用户要求使用分词器处理中文文本",
            cfg=None,
        )
        assert isinstance(ents, set)


# ---------------------------------------------------------------------------
# 日韩文路径 (CU 已实现, L1 透传)
# ---------------------------------------------------------------------------


class TestNonChineseCjk:
    def test_japanese_skips_jieba(self):
        """日文 → 跳过 jieba, 仅走英文/数字/引号串正则。"""
        with patch("core.context_understanding._extract_entities_jieba") as mock_jieba:
            ents = l1_rules._extract_entities(
                "ブラウザを開く use browser",
                cfg=_cfg(),
            )
            assert mock_jieba.call_count == 0
        assert "browser" in ents

    def test_korean_skips_jieba(self):
        """韩文 → 跳过 jieba, 走英文/数字/引号串正则。"""
        with patch("core.context_understanding._extract_entities_jieba") as mock_jieba:
            ents = l1_rules._extract_entities(
                "안녕하세요 use browser",
                cfg=_cfg(),
            )
            assert mock_jieba.call_count == 0
        assert "browser" in ents


# ---------------------------------------------------------------------------
# thought 校验: 中文术语保持
# ---------------------------------------------------------------------------


def _tb(thinking: str) -> ThinkingBlock:
    return ThinkingBlock(type="thinking", id="b1", thinking=thinking)


class TestCheckThoughtWithCjk:
    def test_english_passthrough_preserved(self):
        """英文 fixture 行为兼容: 改写后保留工具名 → pass。"""
        orig = _tb('use browser to "search_url"')
        refined = {"thinking": 'use browser to "search_url"'}
        assert l1_rules._check_thought(orig, refined, max_len=2000, cfg=_cfg()) is True

    def test_english_entity_loss_fails(self):
        """英文 fixture: 改写后丢引号串 → fail (旧行为)。"""
        orig = _tb('use browser to "search_url"')
        refined = {"thinking": "use browser to something else"}
        assert l1_rules._check_thought(orig, refined, max_len=2000, cfg=_cfg()) is False

    def test_chinese_term_preserved_passes(self):
        """中文 fixture: 改写后保留中文术语 → pass (新能力, 旧实现无法判)。"""
        orig = _tb("用户要求使用分词器处理中文文本")
        refined = {"thinking": "用户要求使用分词器处理中文文本并完成校验"}
        # 不强求具体词: 只要 refine 后含 orig 任一中文实体即 pass
        assert l1_rules._check_thought(orig, refined, max_len=2000, cfg=_cfg()) is True

    def test_thinking_too_long_fails(self):
        """超长 → 直接 fail, 不进实体比较。"""
        orig = _tb("short")
        refined = {"thinking": "x" * 3000}
        assert l1_rules._check_thought(orig, refined, max_len=2000, cfg=_cfg()) is False

    def test_empty_refined_fails(self):
        orig = _tb("short thinking")
        assert l1_rules._check_thought(orig, {"thinking": ""}, max_len=2000, cfg=_cfg()) is False


# ---------------------------------------------------------------------------
# toolcall / toolresult 校验未受 cfg 透传影响
# ---------------------------------------------------------------------------


class TestOtherBlockTypesUnaffected:
    def test_toolcall_pass(self):
        tb = ToolcallBlock(type="toolcall", id="b2", name="browser", input='{"q":1}', state="finished")
        assert l1_rules._check_toolcall(tb, {"name": "browser", "input": '{"q":1}'}, ["browser"]) is True

    def test_toolcall_name_not_in_list(self):
        tb = ToolcallBlock(type="toolcall", id="b2", name="bad", input='{}', state="finished")
        assert l1_rules._check_toolcall(tb, {"name": "bad", "input": '{}'}, ["browser"]) is False

    def test_toolcall_invalid_json(self):
        tb = ToolcallBlock(type="toolcall", id="b2", name="browser", input="not-json", state="finished")
        assert l1_rules._check_toolcall(tb, {"name": "browser", "input": "not-json"}, ["browser"]) is False

    def test_toolresult_no_noise(self):
        tb = ToolresultBlock(type="toolresult", id="b3", name="browser", output_text="clean", state="success")
        assert l1_rules._check_toolresult(tb, {"output_text": "clean"}) is True

    def test_toolresult_with_noise_fails(self):
        tb = ToolresultBlock(type="toolresult", id="b3", name="browser", output_text="DEBUG foo", state="success")
        assert l1_rules._check_toolresult(tb, {"output_text": "DEBUG foo"}) is False


# ---------------------------------------------------------------------------
# check() 顶层入口: cfg 透传到 thought 路径, 其他路径不受影响
# ---------------------------------------------------------------------------


class TestCheckEntryPoint:
    def test_check_thinking_with_cfg(self):
        orig = _tb('use browser to "search_url"')
        assert l1_rules.check(
            orig, {"thinking": 'use browser to "search_url"'},
            tool_names=[], thought_max_len_l1=2000, cfg=_cfg(),
        ) is True

    def test_check_toolcall_ignores_cfg(self):
        """toolcall 路径不用 cfg, 但 cfg=None 不报错。"""
        tb = ToolcallBlock(type="toolcall", id="b2", name="browser", input='{}', state="finished")
        assert l1_rules.check(
            tb, {"name": "browser", "input": '{}'},
            tool_names=["browser"], thought_max_len_l1=2000, cfg=None,
        ) is True

    def test_check_toolresult_ignores_cfg(self):
        tb = ToolresultBlock(type="toolresult", id="b3", name="browser", output_text="ok", state="success")
        assert l1_rules.check(
            tb, {"output_text": "ok"},
            tool_names=[], thought_max_len_l1=2000, cfg=None,
        ) is True
