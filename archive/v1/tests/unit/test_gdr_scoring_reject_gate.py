"""gdr ``scoring_reject`` 门控回归 (2026-09-29).

背景: ``process_one`` 的 step 23 (free_quality 独立式 reject 门控) 曾写成
``return {"input": str(input_path), "status": "scoring_reject"}`` —— 但

1. ``input_path`` 在 ``process_one`` 作用域内**不存在** (它收的是 ``Session``,
   路径是 ``_process_one_file`` 的形参), 一旦命中就抛 ``NameError``;
2. 该 ``NameError`` 被 ``process_one`` 外层 ``except Exception`` 吞掉 → ``return None``,
   样本被静默丢弃, **违反 CLAUDE.md「结构合格但评分低的轨迹一律不进死信/不丢」**;
3. 即使补上 ``input_path``, 返回 dict 仍违反 ``process_one -> Session | None`` 契约,
   调用方会把 dict 当 Session 跑 incomplete 检测 + ``save_refined_session``, 把 dict 当 C2 写盘。

正确契约: ``process_one`` 返 ``None`` 并在 ``session.metadata`` 打
``scoring_reject`` 标记, 由 ``_process_one_file`` 读回并组装 status dict。

本文件锁两段:
- Test A: ``process_one`` 命中 reject 时返回 ``None`` (不抛 NameError / 不返 dict)
- Test B: ``_process_one_file`` 派发出 ``status="scoring_reject"``, 不写 C2,
  且不误入 ``judge_low.jsonl``

全部零 LLM / 零网络: 重路径 (context_understanding / fold / router / reassemble /
free_quality evaluate) 全部 monkeypatch 打桩。
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from gdr.config.settings import Settings
from gdr.domain.schema import Message, MessageHealth, Session, TextBlock
from gdr.pipeline import runner as R


def _patch_module_attr(
    monkeypatch: pytest.MonkeyPatch, mod_name: str, attr: str, value,
) -> None:
    """给 ``mod_name.attr`` 打桩, 兼容模块尚未 import 的情况.

    ``process_one`` 里有若干函数内延迟 import (``from core.user_intent import
    heuristic_user_intent``), 只能从 ``sys.modules`` 侧替换。
    """
    try:
        mod = importlib.import_module(mod_name)
    except Exception:
        mod = None
    if mod is None:
        mod = types.ModuleType(mod_name)
        parent_name, _, leaf = mod_name.rpartition(".")
        if parent_name:
            parent = importlib.import_module(parent_name)
            monkeypatch.setitem(sys.modules, mod_name, mod)
            setattr(parent, leaf, mod)
        else:
            monkeypatch.setitem(sys.modules, mod_name, mod)
    monkeypatch.setattr(mod, attr, value, raising=False)


# ---------------------------------------------------------------------------
# 最小 Session / Settings
# ---------------------------------------------------------------------------


def _make_session() -> Session:
    """一条 user + 一条 assistant (含 text block) 的最小 session."""
    return Session(
        session_id="s-reject-1",
        messages=[
            Message(role="user", id="m0", blocks=[]),
            Message(
                role="assistant",
                id="m1",
                blocks=[TextBlock(type="text", id="b0", text="这是最终回复。")],
            ),
        ],
    )


def _make_settings(tmp_path: Path, **over) -> Settings:
    base = dict(
        llm_base_url="http://localhost:0/v1",
        llm_api_key="x",
        main_model="m",
        tool_model="m",
        judge_model="m",
        # 关掉不需要的重路径, 只留 free_quality (reject 门控依赖它)
        enable_context_understanding=False,
        enable_trajectory_compare=False,
        usage_prune_enabled=False,
        scoring_reject_audit_enabled=True,
        scoring_reject_output_path=str(tmp_path / "audit" / "scoring_reject.jsonl"),
        judge_low_output_path=str(tmp_path / "audit" / "judge_low.jsonl"),
        incomplete_detection_enabled=False,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> dict:
    """把 ``process_one`` 里所有重路径打桩, 放行到 step 23 的 free_quality 门控.

    Returns:
        记录调用次数的 dict, 用于断言"该跑的跑了 / 不该跑的没跑"。
    """
    calls: dict = {}

    # --- 前置门: 硬过滤放行 ---
    monkeypatch.setattr(R, "_hard_filter_session", lambda s, c: True)

    # --- 1. 上下文理解 (cfg 已关, 不会进; 留桩防回归) ---
    monkeypatch.setattr(
        R, "light_health_score_for_session",
        lambda s, c: (calls.__setitem__("light_health", calls.get("light_health", 0) + 1), {})[1],
    )
    monkeypatch.setattr(R, "fold_failed_toolresults", lambda *a, **k: 0)
    monkeypatch.setattr(R, "fold_repeated_thinking", lambda *a, **k: 0)

    # heuristic_user_intent 是函数内延迟 import, 打桩到 sys.modules
    _patch_module_attr(monkeypatch, "core.user_intent", "heuristic_user_intent",
                       lambda *a, **k: "用户主旨")

    # --- 2. Router.tag: 一个缺陷 + 一条健康消息 ---
    # 必须产出非空 defects_index: 否则 policy_decisions / repair_items 都空,
    # 流程会在 gdr.early_exit 提前 return session, 根本到不了 free_quality 门控.
    # 注意 tag 挂在本测试造的 SimpleNamespace 上 (普通函数属性, 不绑 self),
    # 签名按 process_one 的实参顺序写。
    def _fake_tag(session, tool_names, hallu_apis, cfg, **kw):
        calls["router"] = calls.get("router", 0) + 1
        defects = {"b0": [R.DefectTag.TEXT_FACT_HALLUCINATION]}
        health = [MessageHealth(msg_idx=1, msg_id="m1", health_score=1.0, is_healthy=True)]
        return defects, health, []

    monkeypatch.setattr(R, "Router", lambda *a, **k: SimpleNamespace(tag=_fake_tag))

    # 强制 DEFER_TO_HUMAN: policy_decisions 非空 (过 early_exit 闸门) 但
    # repair_items 保持空, 从而不会真的调 refiner / LLM.
    monkeypatch.setattr(
        R, "decide_policy",
        lambda block, defects, view, retry_exhausted=False, cfg=None: R.RefinementPolicy.DEFER_TO_HUMAN,
    )
    monkeypatch.setattr(R, "policy_reason", lambda policy, defects, view: "stub")

    # --- 3. reassemble: 原样返回入参 session (与真实实现一致) ---
    def _fake_reassemble(session, *a, **k):
        calls["reassemble"] = calls.get("reassemble", 0) + 1
        session.metadata = session.metadata or {}
        return session

    monkeypatch.setattr(R, "reassemble", _fake_reassemble)

    # --- 4. free_quality evaluate: 判 reject ---
    class _Reject:
        decision = "reject"
        redline_violation = True
        redline_labels = ["pii_email"]
        absolute_quality_score = 2
        absolute_quality_fail_reasons = ["absolute_quality_low"]

        def model_dump(self, mode="python"):
            return {"decision": "reject", "absolute_quality_score": 2}

    def _fake_free_eval(session, cfg):
        calls["free_eval"] = calls.get("free_eval", 0) + 1
        return _Reject()

    # ``free_quality`` 是函数内延迟 import, 打桩到 sys.modules 上
    _patch_module_attr(monkeypatch, "validators.free_quality", "evaluate", _fake_free_eval)

    return calls


# ---------------------------------------------------------------------------
# Test A: process_one 的返回契约
# ---------------------------------------------------------------------------


def test_process_one_returns_none_on_free_quality_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_pipeline: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """命中 free_quality reject 时 ``process_one`` 必须**干净地**返 ``None``.

    回归点: 曾返 ``{"input": str(input_path), ...}`` → ``input_path`` 不在作用域 →
    ``NameError`` → 被 ``process_one`` 外层 ``except Exception`` 吞掉 → 同样返
    ``None``, 但日志里留下一条 ``pipeline error``。光断言返回值区分不了这两种
    ``None``, 所以必须同时断言"没有异常被吞"。
    """
    cfg = _make_settings(tmp_path)
    session = _make_session()

    with caplog.at_level("ERROR", logger="gdr.pipeline.runner"):
        out = R.process_one(session, cfg, tool_names=[], hallu_apis=set())

    # 关键: 不是 dict, 是 None —— 契约被遵守
    assert out is None, (
        f"process_one 在 reject 路径必须返回 None (Session|None 契约), "
        f"实际返回 {type(out).__name__}: {out!r}"
    )
    # 关键: 没有异常被外层 except 静默吞掉
    swallowed = [r for r in caplog.records if "pipeline error" in r.getMessage()]
    assert not swallowed, (
        f"process_one 内部抛异常并被外层 except 吞掉 —— 样本静默丢失: "
        f"{[r.getMessage() for r in swallowed]}"
    )
    # reject 门控确实被走到
    assert stub_pipeline.get("free_eval", 0) == 1, "free_quality evaluate 未被调用"
    # 标记落在 session 上, 供 _process_one_file 读回
    assert (session.metadata or {}).get("scoring_reject") is True
    # audit 队列已落 (不丢数据)
    audit = tmp_path / "audit" / "scoring_reject.jsonl"
    assert audit.exists(), "scoring_reject 审计队列未落盘"


def test_process_one_reject_does_not_write_c2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_pipeline: dict,
) -> None:
    """reject 时不调用 save_refined_session (不写 C2)."""
    calls = {"save": 0}
    monkeypatch.setattr(
        R, "save_refined_session",
        lambda s, p: (calls.__setitem__("save", calls["save"] + 1), p)[1],
    )
    cfg = _make_settings(tmp_path)
    out = R.process_one(_make_session(), cfg, tool_names=[], hallu_apis=set())
    assert out is None
    assert calls["save"] == 0


# ---------------------------------------------------------------------------
# Test C: 决策层 for blk_idx 循环的 REPAIR_IN_PLACE 落底缩进
# ---------------------------------------------------------------------------


def test_repair_in_place_fallthrough_is_inside_block_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_pipeline: dict,
) -> None:
    """REPAIR_IN_PLACE 落底必须在 ``for blk_idx`` 循环**内**, 每 block 一次.

    回归点: 该落底曾被错缩进到循环外 (与 ``for blk_idx`` 平级), 导致:
      1. ``defects_index`` 里没有该 block 的条目时 ``decision`` 从未赋值 →
         ``UnboundLocalError`` → 被 ``process_one`` 外层 ``except`` 吞掉 →
         **整条 session 静默丢失**;
      2. 每条 assistant message 只产 1 个 repair_item (用的是最后一个 block 的
         变量), 且 ``policy_decisions`` 把最后一个 block 的 decision 重复追加;
      3. 若最后一个 block 走了 PRUNE/DEFER 的 ``continue``, 该 PRUNE 掉的 block
         仍会被塞进 repair_items → 决策层与 refiner 双记。
    """
    # 造 2 个 block, 都判 REPAIR_IN_PLACE
    session = _make_session()
    session.messages[1].blocks = [
        TextBlock(type="text", id="b0", text="第一个 block。"),
        TextBlock(type="text", id="b1", text="第二个 block。"),
    ]
    monkeypatch.setattr(
        R, "decide_policy",
        lambda block, defects, view, retry_exhausted=False, cfg=None: R.RefinementPolicy.REPAIR_IN_PLACE,
    )
    monkeypatch.setattr(R, "policy_reason", lambda policy, defects, view: "stub")

    def _fake_tag(session_, tool_names, hallu_apis, cfg, **kw):
        defects = {b.id: [R.DefectTag.TEXT_FACT_HALLUCINATION] for b in session_.messages[1].blocks}
        return defects, [MessageHealth(msg_idx=1, msg_id="m1", health_score=1.0, is_healthy=True)], []

    monkeypatch.setattr(R, "Router", lambda *a, **k: SimpleNamespace(tag=_fake_tag))

    prepared: list[str] = []
    monkeypatch.setattr(
        R, "_prepare_repair_item",
        lambda block, block_type, block_id, defects, bi, context: (
            prepared.append(block_id), type("Item", (), {"block_id": block_id})()
        )[1],
    )
    # 放行到 reassemble; 关掉 free_quality 以免本测试混入 scoring_reject 语义
    monkeypatch.setattr(R, "_run_repairs", lambda *a, **k: [])
    monkeypatch.setattr(
        R, "_l1_sanity_check", lambda *a, **k: True,
    )
    monkeypatch.setattr(R, "reassemble", lambda s, *a, **k: s)

    cfg = _make_settings(tmp_path, enable_free_quality=False)

    out = R.process_one(session, cfg, tool_names=[], hallu_apis=set())

    # 关键 1: 不再抛 UnboundLocalError (若抛, process_one 会静默 return None)
    assert out is not None, (
        "process_one 返回 None —— 多半是决策层循环外访问未赋值的 decision "
        "触发 UnboundLocalError 被外层 except 吞掉 (整条 session 静默丢失)"
    )
    # 关键 2: 两个 block 各产一个 repair_item (而不是每 message 一个)
    assert prepared == ["b0", "b1"], (
        f"REPAIR_IN_PLACE 落底应逐 block 生效, 实际调用序 {prepared!r} "
        f"(单元素 = 落底错缩进到了 for blk_idx 循环外)"
    )


# ---------------------------------------------------------------------------
# Test B: _process_one_file 的 status 派发
# ---------------------------------------------------------------------------


def test_process_one_file_dispatches_scoring_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``process_one`` 打标记返 None 后, ``_process_one_file`` 派发 scoring_reject.

    回归点: 修复前这里会落到 ``judge_discard`` 分支 (audit_reason 错) 或
    抛 NameError; 修复后必须拿到 ``status="scoring_reject"`` 且不误入 judge_low。
    """
    src = tmp_path / "T001__s1.json"
    src.write_text("{}", encoding="utf-8")
    out_path = tmp_path / "refined" / "T001__s1.json"

    # from_trajectory 造一个带标记的 session (模拟 process_one 内部行为)
    session = _make_session()
    session.metadata = {"scoring_reject": True}
    monkeypatch.setattr(R, "from_trajectory", lambda p: session)

    judge_low_calls = {"n": 0}
    monkeypatch.setattr(
        R, "_append_judge_low_queue",
        lambda s, c: judge_low_calls.__setitem__("n", judge_low_calls["n"] + 1),
    )
    save_calls = {"n": 0}
    monkeypatch.setattr(
        R, "save_refined_session",
        lambda s, p: (save_calls.__setitem__("n", save_calls["n"] + 1), p)[1],
    )
    # process_one 整体打桩: 打标记 + 返 None (与修复后的真实行为一致)
    monkeypatch.setattr(
        R, "process_one",
        lambda s, *a, **k: (s.metadata.__setitem__("scoring_reject", True), None)[1],
    )

    result = R._process_one_file(src, out_path, _make_settings(tmp_path))

    assert result["status"] == "scoring_reject", (
        f"期望 scoring_reject, 实际 {result.get('status')!r} (会被 orchestration "
        f"误判为 judge_discard → audit_reason 错)"
    )
    assert result["input"] == str(src), "status dict 缺 input 字段 (input_path 回归)"
    assert save_calls["n"] == 0, "scoring_reject 不应写 C2"
    assert judge_low_calls["n"] == 0, "scoring_reject 不应误入 judge_low.jsonl"


def test_process_one_file_dispatch_keeps_judge_discard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """对照组: 无 scoring_reject 标记时仍走 judge_discard (不回归)。"""
    src = tmp_path / "T001__s2.json"
    src.write_text("{}", encoding="utf-8")
    out_path = tmp_path / "refined" / "T001__s2.json"

    session = _make_session()
    monkeypatch.setattr(R, "from_trajectory", lambda p: session)

    judge_low_calls = {"n": 0}
    monkeypatch.setattr(
        R, "_append_judge_low_queue",
        lambda s, c: judge_low_calls.__setitem__("n", judge_low_calls["n"] + 1),
    )
    monkeypatch.setattr(R, "process_one", lambda s, *a, **k: None)

    result = R._process_one_file(src, out_path, _make_settings(tmp_path))

    assert result["status"] == "judge_discard"
    assert judge_low_calls["n"] == 1, "judge_discard 应仍落 judge_low 队列"


def test_process_one_file_success_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """对照组: 正常成功路径不受影响 (仍写 C2, status=success)."""
    src = tmp_path / "T001__s3.json"
    src.write_text("{}", encoding="utf-8")
    out_path = tmp_path / "refined" / "T001__s3.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    session = _make_session()
    monkeypatch.setattr(R, "from_trajectory", lambda p: session)
    monkeypatch.setattr(R, "process_one", lambda s, *a, **k: s)
    monkeypatch.setattr(R, "_append_deferred_queue", lambda s, c: None)
    monkeypatch.setattr(R, "save_refined_session", lambda s, p: p)

    result = R._process_one_file(src, out_path, _make_settings(tmp_path))

    assert result["status"] == "success"
    assert result["output"] == str(out_path)
