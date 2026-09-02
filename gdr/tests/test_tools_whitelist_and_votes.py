"""回归: tools_config_path 锚定 / 空白名单降级防护 / router 弃权票聚合告警。

背景 (2026-09-02 运行日志): master 从仓库根运行, './config/tools.yaml' 按 CWD
解析失败 → 空白名单 → 一切工具名被级联误判 TOOL_HALLUCINATED → 真实 web_search
调用的 block 以 tool_fix_exhausted 误杀。
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

from config import Settings
from domain import DefectTag, Message, Session, ToolcallBlock
from refiners.tool_fixer import refine
from routing.router import Router
from pipeline.runner import _l1_sanity_check


def _web_search_block() -> ToolcallBlock:
    return ToolcallBlock(
        type="toolcall", id="tc1", name="web_search",
        input='{"q": "x"}', state="finished",
    )


# ---------------------------------------------------------------------------
# #1a: tools_config_path 锚定 gdr 根, 不依赖 CWD
# ---------------------------------------------------------------------------


def test_tools_config_path_anchored_to_gdr_root():
    s = Settings(tools_config_path="./config/tools.yaml")
    assert Path(s.tools_config_path).is_absolute()
    assert Path(s.tools_config_path).exists()


def test_tools_config_path_absolute_override_preserved(tmp_path):
    custom = tmp_path / "my_tools.yaml"
    s = Settings(tools_config_path=custom)
    assert Path(s.tools_config_path) == custom


# ---------------------------------------------------------------------------
# #1d: 空白名单 = 降级态, 三处名称校验必须跳过
# ---------------------------------------------------------------------------


def test_rule_layer_skips_name_check_on_empty_whitelist():
    r = Router()
    blk = _web_search_block()
    assert DefectTag.TOOL_HALLUCINATED not in r._rule_layer_toolcall(blk, [], set())
    # 非空白名单仍正常校验
    assert DefectTag.TOOL_HALLUCINATED in r._rule_layer_toolcall(blk, ["browser"], set())
    assert DefectTag.TOOL_HALLUCINATED not in r._rule_layer_toolcall(blk, ["web_search"], set())


def test_tool_fixer_empty_whitelist_no_false_discard(cfg):
    blk = _web_search_block()
    with patch("infrastructure.LlamaCppClient") as mock_llm:
        mock_llm.get.return_value.chat.return_value = (
            '{"name": "web_search", "input": "{}"}', None,
        )
        out = refine(blk, {}, [], set(), ["tool_hallucinated"], cfg)
    assert out is not None, "空白名单不得把修复结果误判为 exhausted 丢弃"
    assert out["name"] == "web_search"


def test_l1_sanity_check_empty_whitelist_skips_name_check():
    session = Session(session_id="s", messages=[
        Message(role="assistant", id="a1", blocks=[
            {"type": "toolcall", "id": "tc1", "name": "web_search",
             "input": '{"q": "x"}', "state": "finished"},
        ]),
    ])
    assert _l1_sanity_check(session, [], 2000) is True
    assert _l1_sanity_check(session, ["browser"], 2000) is False


def test_tools_yaml_whitelists_remote_web_search():
    """tools.yaml 必须包含远端 QwenPaw 实际使用的 web_search, 防级联误杀回归。"""
    import yaml
    from config.settings import YAML_FILE

    tools_path = Path(YAML_FILE).parent / "tools.yaml"
    data = yaml.safe_load(tools_path.read_text(encoding="utf-8"))
    assert "web_search" in data["tools"]


# ---------------------------------------------------------------------------
# #4: LLM 评审弃权率聚合告警 (全弃权 = 评审层失效, ERROR 浮出)
# ---------------------------------------------------------------------------

_BLOCK_INFO = {
    "block_id": "b1", "block_type": "thinking", "content": "x",
    "msg_idx": 0, "block_idx": 0,
}


def test_llm_layer_all_abstain_logs_error(cfg, monkeypatch, caplog):
    cfg = cfg.model_copy(update={"enable_llm_layer": True})
    r = Router()
    monkeypatch.setattr(Router, "_single_vote", lambda *a, **k: None)
    with caplog.at_level(logging.WARNING):
        out = r._llm_layer([dict(_BLOCK_INFO)], session=None, cfg=cfg)
    assert out == {}
    assert any(
        rec.levelname == "ERROR" and "abstained" in rec.message for rec in caplog.records
    ), "全弃权必须以 ERROR 浮出而非逐块 warning 淹没"


def test_llm_layer_partial_abstain_logs_warning(cfg, monkeypatch, caplog):
    cfg = cfg.model_copy(update={"enable_llm_layer": True})
    r = Router()
    calls = {"n": 0}

    def _flaky(*a, **k):
        calls["n"] += 1
        return None if calls["n"] == 1 else False

    monkeypatch.setattr(Router, "_single_vote", _flaky)
    blocks = [dict(_BLOCK_INFO, block_id=f"b{i}") for i in range(2)]
    with caplog.at_level(logging.WARNING):
        out = r._llm_layer(blocks, session=None, cfg=cfg)
    assert out == {}
    recs = [r_ for r_ in caplog.records if "abstained" in r_.message]
    assert recs and recs[0].levelname == "WARNING", "部分弃权用 warning"
