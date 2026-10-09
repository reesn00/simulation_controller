"""P0-1.3: TOOL_OFF_TOPIC 检测 + policy 决策 + load_tools 4-tuple。

覆盖:
  - load_tools 4-tuple 返回 (新字段 tool_descriptions + off_topic_blacklist)
  - Router._rule_layer_tool_off_topic 规则层 (黑名单) + 嵌入层 (mock)
  - core.policy.decide_policy 对 TOOL_OFF_TOPIC 决策
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from config.settings import load_tools
from core.policy import decide_policy, RefinementPolicy
from domain import DefectTag, ToolcallBlock
from routing.router import Router


def _write_yaml(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "tools.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_tools_returns_4tuple(tmp_path: Path):
    yaml = (
        "extra_tools:\n  - browser\n  - web_search\n"
        "hallucinated_apis:\n  - browser.evaluate\n"
        "off_topic_blacklist:\n  - weather_check\n  - calendar_lookup\n"
        "tool_descriptions:\n"
        "  browser: 浏览网页, 抓取页面内容.\n"
        "  web_search: 关键词搜索, 返回网页摘要.\n"
    )
    p = _write_yaml(tmp_path, yaml)
    names, hallu, descs, blacklist = load_tools(p, tool_source="manual")
    assert set(names) >= {"browser", "web_search"}
    assert "browser.evaluate" in hallu
    assert descs.get("browser") == "浏览网页, 抓取页面内容."
    assert "weather_check" in blacklist
    assert "calendar_lookup" in blacklist


def test_load_tools_off_topic_blacklist_empty_when_missing(tmp_path: Path):
    p = _write_yaml(tmp_path, "extra_tools:\n  - browser\n")
    _, _, descs, blacklist = load_tools(p, tool_source="manual")
    assert descs == {}
    assert blacklist == set()


def test_rule_layer_off_topic_blacklist_hit():
    """命中 off_topic_blacklist 即打 tag, 不走嵌入层。"""
    r = Router()
    block = ToolcallBlock(
        type="toolcall", id="tc1",
        name="weather_check", input="{}", state="finished",
    )
    cfg = SimpleNamespace(
        enable_tool_off_topic_detection=True,
        tool_off_topic_use_blacklist=True,
        tool_off_topic_use_embedding=True,
    )
    tags = r._rule_layer_tool_off_topic(
        block,
        tool_descriptions={},
        off_topic_blacklist={"weather_check"},
        user_intent_heuristic="查询天气",
        cfg=cfg,
        embed_cache={},
    )
    assert DefectTag.TOOL_OFF_TOPIC in tags


def test_rule_layer_off_topic_no_hit_when_disabled():
    """cfg.tool_off_topic_use_blacklist=False 时跳过规则层。"""
    r = Router()
    block = ToolcallBlock(
        type="toolcall", id="tc1",
        name="weather_check", input="{}", state="finished",
    )
    cfg = SimpleNamespace(
        enable_tool_off_topic_detection=True,
        tool_off_topic_use_blacklist=False,
        tool_off_topic_use_embedding=False,  # 双层都关 → 永不打 tag
    )
    tags = r._rule_layer_tool_off_topic(
        block,
        tool_descriptions={},
        off_topic_blacklist={"weather_check"},
        user_intent_heuristic="查询天气",
        cfg=cfg,
        embed_cache={},
    )
    assert tags == []


def test_rule_layer_off_topic_embed_low_similarity_triggers():
    """user_intent 与 tool_desc 余弦相似度 < 阈值 → 打 tag。"""
    r = Router()
    block = ToolcallBlock(
        type="toolcall", id="tc1",
        name="calendar_lookup", input="{}", state="finished",
    )

    # 构造两个完全正交的向量 (点积 0, cosine 0 < 0.30)
    a = [1.0] + [0.0] * 767
    b = [0.0, 1.0] + [0.0] * 766

    class FakeEmbedder:
        def embed(self, text):
            return a if "weather" in text else b

        @staticmethod
        def cosine(x, y):
            # 真正的余弦: 0/1 = 0
            return 0.0

    cfg = SimpleNamespace(
        enable_tool_off_topic_detection=True,
        tool_off_topic_use_blacklist=True,
        tool_off_topic_use_embedding=True,
        tool_off_topic_embed_threshold=0.30,
        embedding_endpoint_url="http://x", embedding_endpoint_model="m",
    )
    fake_module = mock.MagicMock()
    fake_module.HttpEmbedder = FakeEmbedder
    fake_module.get_embedder = lambda c: FakeEmbedder()

    with mock.patch.dict("sys.modules", {"infrastructure.http_embed": fake_module}):
        tags = r._rule_layer_tool_off_topic(
            block,
            tool_descriptions={"calendar_lookup": "查看日历"},
            off_topic_blacklist=set(),
            user_intent_heuristic="查询北京天气",
            cfg=cfg,
            embed_cache={},
        )
    assert DefectTag.TOOL_OFF_TOPIC in tags


def test_rule_layer_off_topic_embed_high_similarity_skipped():
    """高相似度 → 不打 tag。"""
    r = Router()
    block = ToolcallBlock(
        type="toolcall", id="tc1",
        name="web_search", input="{}", state="finished",
    )

    v = [1.0] + [0.0] * 767

    class FakeEmbedder:
        def embed(self, text):
            return v

        @staticmethod
        def cosine(x, y):
            return 1.0  # 完全相等

    cfg = SimpleNamespace(
        enable_tool_off_topic_detection=True,
        tool_off_topic_use_blacklist=False,
        tool_off_topic_use_embedding=True,
        tool_off_topic_embed_threshold=0.30,
        embedding_endpoint_url="http://x", embedding_endpoint_model="m",
    )
    fake_module = mock.MagicMock()
    fake_module.HttpEmbedder = FakeEmbedder
    fake_module.get_embedder = lambda c: FakeEmbedder()

    with mock.patch.dict("sys.modules", {"infrastructure.http_embed": fake_module}):
        tags = r._rule_layer_tool_off_topic(
            block,
            tool_descriptions={"web_search": "搜索"},
            off_topic_blacklist=set(),
            user_intent_heuristic="搜索资料",
            cfg=cfg,
            embed_cache={},
        )
    assert DefectTag.TOOL_OFF_TOPIC not in tags


def test_rule_layer_off_topic_embed_skipped_when_no_description():
    """tool 没有描述 → 嵌入层跳过, 不打 tag (避免无依据误判)。"""
    r = Router()
    block = ToolcallBlock(
        type="toolcall", id="tc1",
        name="unknown_tool", input="{}", state="finished",
    )
    cfg = SimpleNamespace(
        enable_tool_off_topic_detection=True,
        tool_off_topic_use_blacklist=False,
        tool_off_topic_use_embedding=True,
        tool_off_topic_embed_threshold=0.30,
        embedding_endpoint_url="http://x", embedding_endpoint_model="m",
    )
    tags = r._rule_layer_tool_off_topic(
        block,
        tool_descriptions={},  # 未知工具, 无描述
        off_topic_blacklist=set(),
        user_intent_heuristic="查询天气",
        cfg=cfg,
        embed_cache={},
    )
    assert tags == []


def test_rule_layer_off_topic_embed_skipped_when_no_user_intent():
    """user_intent 缺失 → 嵌入层跳过。"""
    r = Router()
    block = ToolcallBlock(
        type="toolcall", id="tc1",
        name="web_search", input="{}", state="finished",
    )
    cfg = SimpleNamespace(
        enable_tool_off_topic_detection=True,
        tool_off_topic_use_blacklist=False,
        tool_off_topic_use_embedding=True,
        tool_off_topic_embed_threshold=0.30,
    )
    tags = r._rule_layer_tool_off_topic(
        block,
        tool_descriptions={"web_search": "搜索"},
        off_topic_blacklist=set(),
        user_intent_heuristic=None,  # 无 user_intent
        cfg=cfg,
        embed_cache={},
    )
    assert tags == []


def test_decide_policy_off_topic_default_prune_block():
    """TOOL_OFF_TOPIC + 无引用 → PRUNE_BLOCK。"""
    cfg = SimpleNamespace(enable_policy_layer=True, policy_defer_on_exhausted=True)
    view = SimpleNamespace(referenced_by=[], block_type="toolcall")
    p = decide_policy(
        block=None,
        defects=[DefectTag.TOOL_OFF_TOPIC],
        context_view=view,
        retry_exhausted=False,
        cfg=cfg,
    )
    assert p == RefinementPolicy.PRUNE_BLOCK


def test_decide_policy_off_topic_referenced_defers():
    """TOOL_OFF_TOPIC + 被后续引用 → DEFER_TO_HUMAN (删会断链)。"""
    cfg = SimpleNamespace(enable_policy_layer=True, policy_defer_on_exhausted=True)
    view = SimpleNamespace(referenced_by=["th1"], block_type="toolcall")
    p = decide_policy(
        block=None,
        defects=[DefectTag.TOOL_OFF_TOPIC],
        context_view=view,
        retry_exhausted=False,
        cfg=cfg,
    )
    assert p == RefinementPolicy.DEFER_TO_HUMAN