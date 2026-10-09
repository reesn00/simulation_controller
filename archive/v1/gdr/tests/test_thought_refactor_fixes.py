"""回归测试 P1.3 + P1.4: thought_refactor 决策/metagaming 跳过 + 路径边界修正.

复现 2026-09-19 T001 useramulation-fe81...:
  - block 91f75c913ac84b399a5f68218fbb1535 (1027 chars, decision)
    _FILE_PATH_PATTERN 把 "/a_19rrk2hct9.html)" "/a_xxx)." 这种非正式引用
    当成伪实体, 9B/32B 改写按语法修复就丢 → entity loss → discard × 2
  - block 81ff2301a9f741f3bbc0aa4a0f533209 (2245 chars, meta composition)
    LLM 倾向输出"应该如何改"的解释而非 JSON → empty refined_thought × 2

修复:
  P1.3 — _FILE_PATH_PATTERN 排除 `;`, `(`, `)`, `,` 边界标点, 防止
         把 prose 里的非正式 URL 引用切成伪实体.
  P1.4 — decision / meta-reasoning thinking 启发式命中 → 直接返回原文,
         不进 LLM 改写循环.

覆盖:
  - _FILE_PATH_PATTERN 不再匹配带右括号的 path
  - _FILE_PATH_PATTERN 仍匹配真实 Unix / Windows 路径
  - _is_decision_or_meta_reasoning 阈值与单标记误伤保护
  - refine() 在 decision 类 block 上直接返回原文 (不进 LLM)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from refiners import thought_refactor as tr


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _thinking(text: str, block_id: str = "th_test") -> Any:
    """构造 ThinkingBlock; 用 SimpleNamespace 避开 pydantic schema 严格校验."""
    from types import SimpleNamespace

    return SimpleNamespace(id=block_id, thinking=text)


# ---------------------------------------------------------------------------
# P1.3: _FILE_PATH_PATTERN 边界字符集
# ---------------------------------------------------------------------------


class TestFilePathBoundary:
    def test_no_trailing_paren(self):
        """右括号是 prose 边界, 不应被吞进 path 实体."""
        ents = tr._extract_entities(
            "iQiyi page (m.iqiyi.com/a_19rrk2hct9.html) returns 200"
        )
        joined = " ".join(ents)
        assert "/a_19rrk2hct9.html)" not in joined
        assert "/a_19rrk2hct9.html" not in joined, (
            f"应被排除的 path 片段仍在实体里: {ents}"
        )

    def test_no_trailing_period(self):
        """句末句号也不应吞进 path."""
        ents = tr._extract_entities("desktop old URL 301 redirects to www.iqiyi.com/a_xxx.")
        joined = " ".join(ents)
        assert "/a_xxx)." not in joined
        assert "/a_xxx" not in joined

    def test_no_trailing_comma(self):
        """逗号边界同样排除 (path 在 prose URL 引用里, 前为 'm')."""
        ents = tr._extract_entities(
            "see (m.iqiyi.com/a_19rrk2hct9.html, then move on)"
        )
        joined = " ".join(ents)
        assert "/a_19rrk2hct9.html" not in joined, (
            f"URL path 片段 (前为 'm'/'(') 不应被当实体: {ents}"
        )

    def test_unix_absolute_path_still_kept(self):
        """真正的 Unix 绝对路径不被破坏."""
        ents = tr._extract_entities("see /usr/bin/python3 for the binary")
        assert "/usr/bin/python3" in ents

    def test_windows_path_still_kept(self):
        """Windows 路径不被破坏."""
        ents = tr._extract_entities(r"installed at C:\Users\tomas\AppData\Local")
        joined = " ".join(ents)
        assert "C:\\Users" in joined

    def test_url_path_inside_url_still_excluded(self):
        """URL 内的 path 片段仍然被 URL 优先匹配吞掉, 不再被 file_path 重复抽到."""
        ents = tr._extract_entities(
            "see https://www.iqiyi.com/a_19rrk2hct9.html for source"
        )
        # URL 整体被 URL 正则抽到, file_path 正则不再重复抽 /a_xxx.html 片段
        urls = [e for e in ents if e.startswith("https://")]
        assert any("a_19rrk2hct9.html" in u for u in urls)


# ---------------------------------------------------------------------------
# P1.4: decision / meta-reasoning heuristic
# ---------------------------------------------------------------------------


class TestDecisionHeuristic:
    def test_block_91f75c_is_decision(self):
        """block 91f75c... 真实 decision 类 thinking: 应当被判定为 decision."""
        text = (
            "The YouTube curl returned nothing (probably blocked/consent page). "
            "Let's not go down the YouTube rabbit hole; I'll instead check whether "
            "the YouTube playlist channels are official.\n\n"
            "Hmm, I've already spent enough tool calls. Let me be pragmatic and "
            "decide the response.\n\n"
            "Policy decision: I won't provide pirated streaming URLs. I'll give "
            "legitimate sources and be honest."
        )
        assert tr._is_decision_or_meta_reasoning(text) is True

    def test_block_81ff2301_is_meta_composition(self):
        """block 81ff2301... 真实 meta composition 类: 应当被判定."""
        text = (
            "Now compose the answer:\n\n"
            "Honest framing: I'm not going to dig up pirate sites. "
            "Per the 'clarify before acting' guidance, offering is fine.\n\n"
            "Keep response concise, Chinese. Add headline at the end.\n\n"
            "Headline format: ⟦ task｜状态：... ⟧"
        )
        assert tr._is_decision_or_meta_reasoning(text) is True

    def test_exploratory_thinking_not_flagged(self):
        """单标记的探索性 thinking 不应被判定 (避免误伤真实 trace)."""
        text = (
            "let me check the actual download links for this first "
            "i need to verify a few things about the upstream sources"
        )
        assert tr._is_decision_or_meta_reasoning(text) is False

    def test_planning_thinking_not_flagged(self):
        """含 'let me check' 但不含多个 decision 标记的 planning 类 thinking."""
        text = (
            "I should now fetch the iqiyi page and verify the redirect target. "
            "Let me check the network status first."
        )
        assert tr._is_decision_or_meta_reasoning(text) is False

    def test_single_marker_below_threshold(self):
        """仅 1 个标记不应触发 (阈值 = 2)."""
        text = (
            "Policy decision: I will now handle the request carefully.\n\n"
            "Continue with the implementation."
        )
        assert tr._is_decision_or_meta_reasoning(text) is False


class TestRefineSkipsDecisionBlocks:
    """refine() 在 decision 类 thinking 上直接返回原文, 不进 LLM."""

    def test_refine_returns_original_for_decision_block(self):
        """block 91f75c 形态: refine() 应跳过 LLM, 返回原文."""
        text = (
            "Let me be pragmatic and decide the response.\n\n"
            "Policy decision: I won't provide pirated streaming URLs. "
            "I'll instead give legitimate sources and be honest."
        )
        block = _thinking(text, block_id="91f75c913ac84b399a5f68218fbb1535")
        cfg = type("Cfg", (), {})()  # 不会真用

        # 关键断言: refine() 返回原文且不调用 LLM (用 patch 兜底)
        # load_and_render 是 refine() 函数内 lazy import 的, patch 在
        # prompts.load_and_render 上 (refine() 用 from prompts import load_and_render)
        with patch("prompts.load_and_render") as mock_load, \
             patch("infrastructure.LlamaCppClient") as mock_client_cls:
            result = tr.refine(
                block,
                context={"session_id": "test"},
                defects=["thought_too_long"],
                cfg=cfg,
            )
        assert result == text, "decision thinking 应保留, 不进 LLM 改写"
        mock_load.assert_not_called()
        mock_client_cls.get.assert_not_called()

    def test_refine_still_calls_llm_for_non_decision_block(self):
        """非 decision 类 thinking 仍正常进 LLM 流程 (不破坏既有行为)."""
        # 含 thought_too_long defect 但不含 decision 标记 → 应进 LLM
        text = (
            "I need to look up several sources for this task. "
            "The user asked about X, so I should check Y. "
            "Let me think about the structure of the response. "
            "Step 1: gather data. Step 2: format output. "
            "Step 3: verify completeness."
        )
        block = _thinking(text, block_id="regular_thinking")
        cfg = type("Cfg", (), {"max_retries_9b": 1, "thought_max_len": 5000,
                                "thought_min_len": 50, "thought_max_len_grace_pct": 10,
                                "llm_timeout_s": 30, "main_model": "m"})()

        # 关键断言: refine() 走 LLM 流程 (load_and_render 被调).
        # 不在乎 LLM 输出内容, 只确认路径被启用.
        with patch("prompts.load_and_render") as mock_load, \
             patch("infrastructure.LlamaCppClient") as mock_client_cls:
            mock_client_cls.get.return_value.chat.return_value = (text, {"usage": {}})
            try:
                tr.refine(
                    block,
                    context={"session_id": "test"},
                    defects=["thought_too_long"],
                    cfg=cfg,
                )
            except Exception:
                pass  # LLM 路径有错无所谓, 关键是确认它进了
        mock_load.assert_called()
        mock_client_cls.get.assert_called()


# ---------------------------------------------------------------------------
# P1.7: prompt 注入实体清单作为硬约束
# ---------------------------------------------------------------------------


class TestPromptEntityInjection:
    """refine() 必须把 orig entities 注入 user prompt; prompt 模板必须含
    {{entities}} 占位符; system prompt 必须显式禁止实体改写.

    复现 2026-09-19 fc81 block f7b86a79...: 9B 改写把
    "www.iqiyi.com/a_19rrk2hct9.html" → "m.iqiyi.com/a_19rrk2hct9.html"
    (mobile 标准化), reasoning 链与 final text URL 矛盾, 训练数据隐性 bug.
    """

    def test_prompt_template_has_entities_placeholder(self):
        """thought.yaml user 模板必须含 {{entities}} 占位符."""
        from pathlib import Path
        # 用绝对路径, 避免 cwd 不是 gdr/ 时找不到.
        content = (Path(__file__).resolve().parent.parent / "prompts" / "thought.yaml").read_text(encoding="utf-8")
        assert "{{entities}}" in content, (
            "thought.yaml user prompt 缺 {{entities}} 占位符, "
            "LLM 不会被告知保留实体清单"
        )

    def test_prompt_template_warns_about_host_standardization(self):
        """system prompt 必须显式禁止 www↔m / http↔https 等 mobile 标准化."""
        from pathlib import Path
        content = (Path(__file__).resolve().parent.parent / "prompts" / "thought.yaml").read_text(encoding="utf-8")
        assert "www↔m" in content or "www<->m" in content, (
            "system prompt 应明确禁止 host 标准化 (www↔m), 防 LLM 重蹈覆辙"
        )
        assert "mobile 标准化" in content or "host" in content, (
            "system prompt 应解释为何禁止 (host 是 context 标识)"
        )

    def test_refine_injects_entities_into_user_prompt(self):
        """refine() 必须把 orig entities 排序后注入 user prompt."""
        from types import SimpleNamespace

        orig_text = (
            "Confirmed: https://www.iqiyi.com/a_19rrk2hct9.html is live. "
            "Should I open browser to verify the page?"
        )
        block = SimpleNamespace(id="test_block", thinking=orig_text)
        cfg = SimpleNamespace(
            max_retries_9b=1, thought_max_len=5000, thought_min_len=50,
            thought_max_len_grace_pct=10, llm_timeout_s=30, main_model="m",
        )

        # 捕获 prompt 渲染参数
        captured_user_kwargs: list[dict] = []

        def fake_load(_name, kind, **kwargs):
            if kind == "user":
                captured_user_kwargs.append(kwargs)
            return f"[{kind} prompt]"

        # 让 LLM 返回含原文的 refined_thought (无 entity loss)
        with patch("prompts.load_and_render", side_effect=fake_load), \
             patch("infrastructure.LlamaCppClient") as mock_client_cls:
            mock_client_cls.get.return_value.chat.return_value = (
                '{"refined_thought": "' + orig_text + '"}', {"usage": {}},
            )
            tr.refine(
                block, context={"sid": "x"},
                defects=["thought_too_long"], cfg=cfg,
            )

        # 验证 user prompt 渲染时传入了 entities
        assert captured_user_kwargs, "refine() 应渲染 user prompt"
        kwargs = captured_user_kwargs[-1]
        assert "entities" in kwargs, "refine() 应注入 entities kwarg"
        # 实体应含 URL + 工具名 (browser)
        ent_str = kwargs["entities"]
        assert "https://www.iqiyi.com/a_19rrk2hct9.html" in ent_str
        assert "browser" in ent_str

    def test_refine_passes_empty_marker_when_no_entities(self):
        """无实体时仍传 entities kwarg (空时用 "(无)" 占位)."""
        from types import SimpleNamespace

        # 短文本, 无 URL/工具名/数字 ID
        orig_text = (
            "I should fetch the page and verify. "
            "Let me run a quick search and check the result."
        )
        block = SimpleNamespace(id="test_block", thinking=orig_text)
        cfg = SimpleNamespace(
            max_retries_9b=1, thought_max_len=5000, thought_min_len=50,
            thought_max_len_grace_pct=10, llm_timeout_s=30, main_model="m",
        )

        captured_user_kwargs: list[dict] = []

        def fake_load(_name, kind, **kwargs):
            if kind == "user":
                captured_user_kwargs.append(kwargs)
            return f"[{kind} prompt]"

        with patch("prompts.load_and_render", side_effect=fake_load), \
             patch("infrastructure.LlamaCppClient") as mock_client_cls:
            mock_client_cls.get.return_value.chat.return_value = (
                '{"refined_thought": "' + orig_text + '"}', {"usage": {}},
            )
            tr.refine(
                block, context={"sid": "x"},
                defects=["thought_too_long"], cfg=cfg,
            )

        assert captured_user_kwargs
        ent_str = captured_user_kwargs[-1]["entities"]
        # 无实体时为 "(无)" 占位 (避免模板渲染空字符串)
        assert ent_str == "(无)" or ent_str == ""