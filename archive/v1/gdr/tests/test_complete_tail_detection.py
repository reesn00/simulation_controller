"""F3-D fix: _is_text_incomplete 增加结构闭合信号与尾部语义收尾词.

设计原则:
  - 结构闭合 (markdown 表格/分隔线/code fence/bracket pair) 是"出现即完整"
    的强信号, 优先于字符级末尾标点判断
  - 中文方括号闭合 (⟦⟧/【】 等) 不再要求提前注册白名单 — 配对出现即完整
  - 尾部 100 字符含 "总结/结论/已完成/下一步/锚点" 等视为完整
  - 旧行为 (前缀 marker + 末尾标点) 保留, 误判可通过 _has_structural_close
    或尾部语义收尾词绕过
"""

from __future__ import annotations

import pytest

from pipeline.runner import _is_text_incomplete, _has_structural_close


# ---------------------------------------------------------------------------
# _has_structural_close: 结构闭合信号
# ---------------------------------------------------------------------------


class TestHasStructuralClose:
    def test_markdown_table_row(self):
        """表格行 (|开头 |结尾) 视为结构闭合."""
        assert _has_structural_close(
            "**结论**\n\n| 平台 | 状态 |\n|---|---|\n| 央视网 | 404 |"
        ) is True

    def test_markdown_horizontal_rule(self):
        """--- 分隔线视为结构闭合."""
        assert _has_structural_close(
            "第一段说明\n\n---\n\n## 结论\n已确认。"
        ) is True

    def test_code_fence_close(self):
        """尾部含 ``` 视为 code fence 闭合."""
        text = "示例代码:\n\n```python\nprint(1)\n```"
        assert _has_structural_close(text) is True

    def test_colon_fence_close(self):
        """尾部 ::: 视为 colon-fenced block 闭合."""
        assert _has_structural_close(
            "::: warning\n不要泄露数据\n:::"
        ) is True

    def test_bracket_pair_math(self):
        """⟦...⟧ 配对且 close 在尾部 → 完整."""
        text = (
            "排查结论: 各平台均已下架\n\n"
            "⟦ 武林外传全集观看源排查｜已完成｜锚点: VIDA1354531513618469 ⟧"
        )
        assert _has_structural_close(text) is True

    def test_bracket_pair_chinese_square(self):
        """【...】 配对且 close 在尾部 → 完整."""
        text = "【结论】已确认全部平台下架"
        assert _has_structural_close(text) is True

    def test_bracket_pair_unclosed_returns_false(self):
        """只有 open 没有 close → 不算完整."""
        text = "⟦ 武林外传全集观看源排查｜状态: 进行中"
        assert _has_structural_close(text) is False

    def test_no_close_returns_false(self):
        """无任何结构闭合 → False."""
        text = "这是一段普通的文字, 没有表格, 没有分隔线, 也没有 code fence"
        assert _has_structural_close(text) is False


# ---------------------------------------------------------------------------
# _is_text_incomplete: F3-D 新增行为
# ---------------------------------------------------------------------------


class TestCompleteViaStructuralClose:
    """结构闭合应绕过原有末尾标点判断."""

    def test_t001_real_artifact_not_flagged(self):
        """T001 实际 trajectory 末段 (⟦...⟧ 收尾) 不再判 incomplete.

        复现 acbada8d 与 dfceb063 的启发式误判。
        """
        text = (
            "清理临时脚本的指令被系统按高危拦掉了（`del` 被判为可能删数据），"
            "我就不重试了；探测脚本还留在工作区，需要的话你手动删。\n\n"
            "---\n\n"
            "## 结论：现在**不存在**合法的《武林外传》电视剧全集免费在线播放地址\n\n"
            "不是我没翻到——是这部剧正处于**版权空窗期**...\n\n"
            "| 平台 | 实测结果 |\n|---|---|\n"
            "| **央视网** | HTTP 404 |\n| **爱奇艺** | 跳首页 |\n\n"
            "⟦ 武林外传全集观看源排查｜状态：已完成——央视网/爱奇艺 HTTP 404 "
            "或跳首页；结论=当前无合法免费在线源，第81集仅存DVD版｜"
            "下一步：待用户确认是否建 cron 监控平台恢复上线｜"
            "锚点：VIDA1354531513618469、VIDE1406399770522952 ⟧"
        )
        assert _is_text_incomplete(text) is False

    def test_markdown_report_complete(self):
        """含表格 + 分隔线的报告视为完整, 即使末尾不是标准标点."""
        text = (
            "排查报告\n\n"
            "| 平台 | 状态 |\n|---|---|\n"
            "| A | OK |\n| B | 404 |\n\n"
            "---"
        )
        assert _is_text_incomplete(text) is False

    def test_code_block_complete(self):
        """含 code fence 闭合的报告视为完整."""
        text = (
            "示例输出:\n\n"
            "```json\n{\"status\": \"ok\"}\n```"
        )
        assert _is_text_incomplete(text) is False


class TestCompleteViaSemanticTail:
    """尾部含 "总结/结论/已完成/下一步/锚点" 等视为完整."""

    def test_tail_summary_keyword(self):
        """尾部含 "总结" 字样 → 完整."""
        text = "经过 8 平台逐一排查, 结论是当前无免费在线源. 排查过程总结如下."
        assert _is_text_incomplete(text) is False

    def test_tail_next_step_keyword(self):
        """尾部含 "下一步" → 完整."""
        text = "排查结束. 下一步: 等用户确认是否需要建定时任务监控平台恢复"
        assert _is_text_incomplete(text) is False

    def test_tail_anchor_keyword(self):
        """尾部含 "锚点" → 完整."""
        text = "锚点: VIDA1354531513618469、ep191020、北京联盟影业"
        assert _is_text_incomplete(text) is False

    def test_tail_done_keyword(self):
        """尾部含 "已完成" → 完整."""
        text = "排查已完成, 全部平台已实测完毕, 报告如上所述"
        assert _is_text_incomplete(text) is False


# ---------------------------------------------------------------------------
# 回归: 旧的不完整信号仍然有效
# ---------------------------------------------------------------------------


class TestIncompleteStillFlagged:
    def test_ellipsis_still_flagged(self):
        """省略号仍判 incomplete."""
        assert _is_text_incomplete("let me check the actual download links...") is True

    def test_continue_marker_prefix_still_flagged(self):
        """前缀含 "让我" 仍判 incomplete (旧行为)."""
        text = (
            "让我先确认一下用户的真实意图是什么. 用户说想要武林外传全集免费"
            "在线观看, 但可能需要进一步澄清."
        )
        assert _is_text_incomplete(text) is True

    def test_long_no_punct_no_structural_close(self):
        """长文无标点 + 无结构闭合 + 无尾部收尾词 → 仍判 incomplete."""
        text = "let me first clarify one thing about the request the user wants free full streaming links for unlicensed content"
        assert _is_text_incomplete(text) is True

    def test_empty_still_flagged(self):
        assert _is_text_incomplete("") is True
        assert _is_text_incomplete("   ") is True

    def test_short_text_without_punct_not_flagged(self):
        """短文本 (<30 chars) 容忍, 旧行为保留."""
        assert _is_text_incomplete("ok") is False
        assert _is_text_incomplete("好的") is False


# ---------------------------------------------------------------------------
# 边界: 短文本不应触发新逻辑
# ---------------------------------------------------------------------------


class TestShortTextNotAffected:
    def test_short_structural_close_skipped(self):
        """<30 chars 短文本不进入结构闭合判断分支."""
        # 即使短文本含表格行, 也按短文本规则 (30 chars) 跳过结构判断
        assert _is_text_incomplete("| a | b |") is False  # 短文本直接 False (非空)

    def test_short_semantic_tail_skipped(self):
        """<30 chars 短文本不进入语义收尾词判断."""
        assert _is_text_incomplete("总结") is False
        assert _is_text_incomplete("下一步") is False
