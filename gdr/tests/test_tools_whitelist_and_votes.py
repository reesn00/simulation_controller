"""回归: tools_config_path 锚定 / 空白名单降级防护 / 白名单来源自动化 /
漂移报告 / router 弃权票聚合告警。

背景 (2026-09-02 运行日志): master 从仓库根运行, './config/tools.yaml' 按 CWD
解析失败 → 空白名单 → 一切工具名被级联误判 TOOL_HALLUCINATED → 真实 web_search
调用的 block 以 tool_fix_exhausted 误杀。后续又撞上手工白名单过期: QwenPaw
运行时动态工具 Skill (31 次真实调用) 不在手工名单里被误判 → 白名单来源改为
agent.json 自动解析 ∪ tools.yaml extra_tools 补充, 并加会话级漂移报告。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from config import Settings, load_tools
from domain import DefectTag, Message, Session, ToolcallBlock
from refiners.tool_fixer import refine
from routing.router import Router
from pipeline.runner import _collect_unknown_tool_names, _l1_sanity_check, process_one


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


def test_tools_yaml_extra_tools_cover_dynamic_skill_and_manual_floor():
    """extra_tools 必须覆盖 agent.json 之外的运行时动态工具 Skill
    (qf_out 统计: 31 次真实调用), 以及 auto 源失效时的 web_search 手工底座。"""
    import yaml
    from config.settings import YAML_FILE

    tools_path = Path(YAML_FILE).parent / "tools.yaml"
    data = yaml.safe_load(tools_path.read_text(encoding="utf-8"))
    assert "Skill" in data["extra_tools"]
    assert "web_search" in data["extra_tools"]
    # 幻觉 API 黑名单始终手工维护, 不得随来源重构丢失
    assert "browser.evaluate" in data["hallucinated_apis"]


# ---------------------------------------------------------------------------
# 白名单来源自动化: agent.json 权威源 ∪ extra_tools 补充
# ---------------------------------------------------------------------------


def _write_agent_json(path: Path, enabled=("browser", "web_search"), disabled=("read_file",)) -> Path:
    data = {"tools": {"builtin_tools": {
        **{name: {"name": name, "enabled": True} for name in enabled},
        **{name: {"name": name, "enabled": False} for name in disabled},
    }}}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_tools_merges_agent_json_with_extra_tools(tmp_path):
    agent = _write_agent_json(tmp_path / "agent.json")
    yaml_path = tmp_path / "tools.yaml"
    yaml_path.write_text(
        "extra_tools:\n  - Skill\nhallucinated_apis:\n  - browser.evaluate\n",
        encoding="utf-8",
    )
    tools, hallu = load_tools(yaml_path, agent, tool_source="auto")
    assert "browser" in tools and "web_search" in tools
    assert "read_file" not in tools, "disabled 的 builtin 工具不得进白名单"
    assert "Skill" in tools, "extra_tools 必须补充 agent.json 之外的动态工具"
    assert hallu == {"browser.evaluate"}


def test_load_tools_auto_falls_back_to_manual_when_agent_json_missing(tmp_path):
    yaml_path = tmp_path / "tools.yaml"
    yaml_path.write_text("extra_tools:\n  - Skill\n  - web_search\n", encoding="utf-8")
    tools, _ = load_tools(yaml_path, tmp_path / "missing.json", tool_source="auto")
    assert tools == ["Skill", "web_search"]


def test_load_tools_manual_source_ignores_agent_json(tmp_path):
    agent = _write_agent_json(tmp_path / "agent.json")
    yaml_path = tmp_path / "tools.yaml"
    yaml_path.write_text("extra_tools:\n  - browser\n", encoding="utf-8")
    tools, _ = load_tools(yaml_path, agent, tool_source="manual")
    assert tools == ["browser"]


def test_load_tools_off_disables_name_checks_but_keeps_hallu_apis(tmp_path):
    yaml_path = tmp_path / "tools.yaml"
    yaml_path.write_text(
        "extra_tools:\n  - browser\nhallucinated_apis:\n  - browser.evaluate\n",
        encoding="utf-8",
    )
    tools, hallu = load_tools(yaml_path, tool_source="off")
    assert tools == []
    assert hallu == {"browser.evaluate"}


def test_load_tools_degrades_to_empty_when_all_sources_missing(tmp_path):
    tools, _ = load_tools(
        tmp_path / "missing.yaml", tmp_path / "missing.json", tool_source="auto",
    )
    assert tools == [], "全源失效必须降级为空白名单 (跳过名称校验), 不得误杀真实数据"


def test_settings_default_tool_source_is_auto():
    s = Settings()
    assert s.tool_source == "auto"
    assert str(s.qwenpaw_agent_json).endswith("agent.json")


# ---------------------------------------------------------------------------
# 白名单漂移报告: 会话中出现但不在白名单里的工具名只告警+记录, 不参与判定
# ---------------------------------------------------------------------------


def test_collect_unknown_tool_names():
    session = Session(session_id="s", messages=[
        Message(role="assistant", id="a1", blocks=[
            {"type": "toolcall", "id": "tc1", "name": "Skill", "input": "{}", "state": "finished"},
            {"type": "toolcall", "id": "tc2", "name": "browser", "input": "{}", "state": "finished"},
        ]),
        Message(role="user", id="u1", blocks=[]),
    ])
    assert _collect_unknown_tool_names(session, ["browser"]) == {"Skill"}
    assert _collect_unknown_tool_names(session, ["browser", "Skill"]) == set()
    assert _collect_unknown_tool_names(session, []) == set(), "空白名单降级态不产生漂移噪声"


def test_process_one_reports_unknown_tool_names_in_metadata(cfg):
    """漂移信息必须写进输出 metadata 并以 warning 浮出, 而非静默误判。"""
    session = Session(session_id="s-drift", messages=[
        Message(role="assistant", id="a1", blocks=[
            {"type": "thinking", "id": "th1", "thinking": "use the dynamic tool now"},
            {"type": "toolcall", "id": "tc1", "name": "Skill", "input": '{"name": "docx"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "Skill", "output_text": "ok", "state": "success"},
            {"type": "text", "id": "tx1", "text": "task finished"},
        ]),
    ])
    with patch("pipeline.runner.fold_failed_toolresults", return_value=0), \
         patch("pipeline.runner.fold_repeated_thinking", return_value=0), \
         patch("pipeline.runner.build_context_for_session", return_value=MagicMock()), \
         patch("pipeline.runner.Router") as mock_router_cls:
        mock_router = MagicMock()
        mock_router.tag.return_value = ({}, [])
        mock_router_cls.return_value = mock_router
        out = process_one(session, cfg, ["browser"], set())
    assert out is not None
    assert out.metadata.get("unknown_tool_names") == ["Skill"]


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
