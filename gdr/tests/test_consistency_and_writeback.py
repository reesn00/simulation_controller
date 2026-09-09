"""回归测试: 一致性校验真增量 (O(N)) 与 refine_records 按 block_id 写回。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.context_understanding import GlobalState
from domain import BlockIndex, BlockRefineRecord, StepEditStatus
from reassembly.reassembler import (
    reassemble, _validate_edit_consistency, _lost_critical_fields,
)


class _StubCU:
    """模拟 ContextUnderstanding 中 _validate_edit_consistency 用到的接口。

    update_state_chunk 只做一次调用 (真增量); 计数器用于断言调用次数。
    """

    def __init__(self, num_chunks: int):
        self._num_chunks = num_chunks
        self.calls: list[int] = []
        self._snapshots = {ci: GlobalState() for ci in range(num_chunks)}
        self._chunks = {ci: [f"b{ci}"] for ci in range(num_chunks)}

    @property
    def num_chunks(self) -> int:
        return self._num_chunks

    @property
    def chunk_blocks(self) -> dict[int, list[str]]:
        return self._chunks

    @property
    def state_snapshots(self) -> dict[int, GlobalState]:
        return self._snapshots

    @property
    def state_tracking_calls(self) -> int:
        return len(self.calls)

    def snapshot_at(self, chunk_idx: int):
        return self._snapshots.get(chunk_idx)

    def chunk_of_block(self, block_id: str):
        return int(block_id[1:])

    def update_state_chunk(self, session, current_state, chunk_idx: int, cfg=None):
        self.calls.append(chunk_idx)
        return GlobalState()


def _record(block_id: str, msg_idx: int = 0) -> BlockRefineRecord:
    return BlockRefineRecord(
        block_index=BlockIndex(msg_idx=msg_idx, block_idx=0, block_id=block_id, block_type="thinking"),
        module="thought_refactor",
        original_content={"thinking": "old"},
        refined_content={"thinking": "new"},
        attempts=1,
        result="success",
        validation_results=[],
    )


def test_consistency_is_incremental_not_quadratic():
    """编辑在 chunk 1, 4 个 chunk: 应只更新 chunk 1/2/3 各一次 (3 次调用)。

    旧实现 state_after(ci) 从 chunk 0 重放: 2+3+4 = 9 次调用。
    """
    cu = _StubCU(num_chunks=4)
    records = [_record("b1"), _record("b2")]
    _validate_edit_consistency(session=None, refine_records=records, cu=cu, cfg=None)
    assert cu.calls == [1, 2, 3]


def test_consistency_budget_marks_needs_review():
    """预算耗尽时, 剩余 chunk 的成功编辑标记 needs_review 而不是无限调用。"""
    cu = _StubCU(num_chunks=10)

    class _Cfg:
        consistency_max_llm_calls = 2
        consistency_rollback_on_entity_loss = True

    records = [_record("b0"), _record("b9")]
    _validate_edit_consistency(session=None, refine_records=records, cu=cu, cfg=_Cfg())
    # 预算 2 → 更新 chunk 0/1 后耗尽, chunk >= 2 的成功编辑标记 needs_review
    assert cu.calls == [0, 1]
    assert records[0].edit_status != StepEditStatus.NEEDS_REVIEW   # chunk 0 已校验
    assert records[1].edit_status == StepEditStatus.NEEDS_REVIEW   # chunk 9 未校验


def test_reassemble_writeback_by_id_after_prune(cfg):
    """剪枝删除了同消息中更早的块后, 按 block_idx 写回会越界崩溃 (历史 bug)。

    现按 block_id 定位: t1 被 PRUNE 移除, t2 的精修内容应正确写回 t2。
    """
    from domain import Session, Message

    session = Session(session_id="prune-writeback", messages=[
        Message(role="assistant", id="msg-0", blocks=[
            {"type": "thinking", "id": "t1", "thinking": "prune me"},
            {"type": "thinking", "id": "t2", "thinking": "long" * 300},
        ]),
    ])
    records = [
        BlockRefineRecord(
            block_index=BlockIndex(msg_idx=0, block_idx=1, block_id="t2", block_type="thinking"),
            module="thought_refactor",
            original_content={"thinking": "long" * 300},
            refined_content={"thinking": "refined content"},
            attempts=1,
            result="success",
            validation_results=[],
        ),
    ]
    with patch("infrastructure.LlamaCppClient") as mock_llm:
        mock_llm.get.return_value.chat.return_value = ('{"score": 9}', None)
        result = reassemble(
            session, records, health_scores=[], cfg=cfg,
            policy_decisions=[], prune_block_ids={"t1"}, deferred_block_ids=set(),
        )
    assert result is not None
    blocks = result.messages[0].blocks
    # Message schema 将 dict 块转为 Pydantic 模型, 用属性访问断言
    assert [b.id for b in blocks] == ["t2"]
    assert blocks[0].thinking == "refined content"


def test_early_return_attaches_metadata(cfg):
    """无缺陷早退路径也应产出统一 metadata (refine_history/validation_summary)。"""
    from unittest.mock import patch
    from domain import Session, Message
    from pipeline.runner import process_one

    session = Session(session_id="clean", messages=[
        Message(role="user", id="u1", blocks=[]),
        Message(role="assistant", id="a1", blocks=[
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"q": "x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"},
        ]),
    ])
    with patch("infrastructure.LlamaCppClient"):
        result = process_one(session, cfg, ["browser"], set())
    assert result is not None
    md = result.metadata
    assert md.get("refine_history") == []
    assert "validation_summary" in md
    assert md.get("refined_version") == "v2"


def test_prune_only_decisions_reach_reassemble(cfg):
    """只有 PRUNE 决策、无精修记录时, 剪枝必须被应用且 metadata 落盘 (不被早退丢弃)。"""
    from unittest.mock import patch
    from domain import Session, Message
    from pipeline.runner import process_one

    session = Session(session_id="prune-only", messages=[
        Message(role="user", id="u1", blocks=[]),
        Message(role="assistant", id="a1", blocks=[
            {"type": "thinking", "id": "th1", "thinking": "a" * 600},
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"q": "x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"},
        ]),
    ])
    with patch("infrastructure.LlamaCppClient") as mock_llm:
        mock_llm.get.return_value.chat.return_value = ('{"score": 9}', None)
        result = process_one(session, cfg, ["browser"], set())
    assert result is not None
    blocks = result.messages[1].blocks
    assert [b.id for b in blocks] == ["tc1", "tc1"], "thought_too_long thinking 应被 PRUNE"
    dec = result.metadata["policy_decisions"]
    assert dec and dec[0]["policy"] == "prune_block"


def test_judge_low_score_discards_session(cfg):
    """终检 judge 分数低于 judge_min_score: 不进主输出 (None); 数据由
    _process_one_file 转 judge_low.jsonl 审核通道 (见 test_judge_low_session_exported_to_review_channel)。"""
    from unittest.mock import patch
    from domain import Session, Message
    from pipeline.runner import process_one

    session = Session(session_id="low-score", messages=[
        Message(role="user", id="u1", blocks=[]),
        Message(role="assistant", id="a1", blocks=[
            {"type": "thinking", "id": "th1", "thinking": "a" * 600},
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"q": "x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"},
        ]),
    ])
    with patch("infrastructure.LlamaCppClient") as mock_llm:
        mock_llm.get.return_value.chat.return_value = ('{"score": 2}', None)
        result = process_one(session, cfg, ["browser"], set())
    assert result is None
    assert session.metadata.get("judge_discard") == {"score": 2, "min_score": 7}


# ---------------------------------------------------------------------------
# 结构严重不可用硬过滤 (用户主旨: 只有数据本身不可用才丢弃)
# ---------------------------------------------------------------------------


def test_structural_filter_discards_user_only_session(cfg):
    from domain import Session, Message
    from pipeline.runner import _hard_filter_session

    session = Session(session_id="user-only", messages=[
        Message(role="user", id="u1", blocks=[]),
        Message(role="user", id="u2", blocks=[]),
    ])
    assert _hard_filter_session(session, cfg) is False


def test_structural_filter_discards_empty_assistant_shell(cfg):
    from domain import Session, Message
    from pipeline.runner import _hard_filter_session

    session = Session(session_id="empty-shell", messages=[
        Message(role="user", id="u1", blocks=[]),
        Message(role="assistant", id="a1", blocks=[]),
    ])
    assert _hard_filter_session(session, cfg) is False


def test_structural_filter_discards_lone_failed_assistant(cfg):
    """单条 assistant、仅含失败工具调用且无任何 thinking/text 内容 → 不可用。"""
    from domain import Session, Message
    from pipeline.runner import _hard_filter_session

    session = Session(session_id="lone-fail", messages=[
        Message(role="user", id="u1", blocks=[]),
        Message(role="assistant", id="a1", blocks=[
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"q": "x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "", "state": "error"},
        ]),
    ])
    assert _hard_filter_session(session, cfg) is False


def test_structural_filter_keeps_thin_but_recoverable(cfg):
    """保守边界: 单条 assistant 但存在成功 toolresult 或 thinking → 必须放行。"""
    from domain import Session, Message
    from pipeline.runner import _hard_filter_session

    with_success = Session(session_id="thin-ok", messages=[
        Message(role="user", id="u1", blocks=[]),
        Message(role="assistant", id="a1", blocks=[
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"q": "x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"},
        ]),
    ])
    with_thinking = Session(session_id="thin-think", messages=[
        Message(role="user", id="u1", blocks=[]),
        Message(role="assistant", id="a1", blocks=[
            {"type": "thinking", "id": "th1", "thinking": "让我想想"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "", "state": "error"},
        ]),
    ])
    assert _hard_filter_session(with_success, cfg) is True
    assert _hard_filter_session(with_thinking, cfg) is True


def test_structural_unusable_discarded_before_any_llm(cfg):
    """硬丢弃发生在零 LLM 阶段: 不消耗任何模型调用。"""
    from unittest.mock import patch
    from domain import Session, Message
    from pipeline.runner import process_one

    session = Session(session_id="dead-session", messages=[
        Message(role="user", id="u1", blocks=[]),
    ])
    with patch("infrastructure.LlamaCppClient") as mock_llm:
        result = process_one(session, cfg, ["browser"], set())
    assert result is None
    assert not mock_llm.get.called, "结构过滤应在任何 LLM 调用之前短路"


def test_judge_low_session_exported_to_review_channel(cfg, tmp_path):
    """judge 低分: 主输出不落盘, 但完整 session 写入 judge_low.jsonl (数据不丢)。"""
    import json
    from unittest.mock import patch
    from domain import Session, Message, save_session
    from pipeline.runner import _process_one_file

    cfg.judge_low_output_path = tmp_path / "judge_low.jsonl"
    input_path = tmp_path / "in.json"
    output_path = tmp_path / "out.json"
    session = Session(session_id="low-score-file", messages=[
        Message(role="user", id="u1", blocks=[]),
        Message(role="assistant", id="a1", blocks=[
            {"type": "thinking", "id": "th1", "thinking": "a" * 600},
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"q": "x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"},
        ]),
    ])
    save_session(session, input_path)
    with patch("infrastructure.LlamaCppClient") as mock_llm:
        mock_llm.get.return_value.chat.return_value = ('{"score": 2}', None)
        status = _process_one_file(input_path, output_path, cfg)

    assert status["status"] == "discard"
    assert not output_path.exists(), "低分 session 不得进入主输出"
    record = json.loads(cfg.judge_low_output_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["session_id"] == "low-score-file"
    assert record["judge"]["score"] == 2
    assert record["session"]["session_id"] == "low-score-file", "审核通道须含完整 session"


def test_judge_discard_not_resurrected_by_timeout_fallback(cfg, monkeypatch):
    """judge 判死 (低分) 的 session 即使耗时接近超时, 也不得被 runner 兜底复活。"""
    import time as _time
    from unittest.mock import patch
    from domain import Session, Message
    from pipeline.runner import process_one

    class _Clock:
        now = 1000.0

    clock = _Clock()
    monkeypatch.setattr(_time, "perf_counter", lambda: clock.now)

    session = Session(session_id="late-low-score", messages=[
        Message(role="user", id="u1", blocks=[]),
        Message(role="assistant", id="a1", blocks=[
            {"type": "thinking", "id": "th1", "thinking": "a" * 600},
            {"type": "toolcall", "id": "tc1", "name": "browser", "input": '{"q": "x"}', "state": "finished"},
            {"type": "toolresult", "id": "tc1", "name": "browser", "output_text": "ok", "state": "success"},
        ]),
    ])

    def _fake_chat(*args, **kwargs):
        # judge 返回时把时钟拨到预算耗尽: elapsed > 0.8 * session_timeout_s
        clock.now = 1000.0 + cfg.session_timeout_s
        return ('{"score": 2}', None)

    with patch("infrastructure.LlamaCppClient") as mock_llm:
        mock_llm.get.return_value.chat.side_effect = _fake_chat
        result = process_one(session, cfg, ["browser"], set())

    assert result is None
    assert "timeout_partial_save" not in session.metadata, "judge 判死的 session 不得被超时兜底复活"


# ---------------------------------------------------------------------------
# 一致性校验语义化比较 + 回滚前 LLM 复核 (修复摘要改写漂移造成的批量误回滚)
# ---------------------------------------------------------------------------


def test_lost_fields_reworded_constraint_not_flagged():
    """约束换说法但语义等价 → 不算丢失 (旧实现精确字符串差集会误报)。"""
    before = GlobalState(critical_constraints=["必须在 9 月 10 日前完成支付"])
    after = GlobalState(critical_constraints=["支付须在 9 月 10 日前完成"])
    assert _lost_critical_fields(before, after) == []


def test_lost_fields_contained_constraint_not_flagged():
    before = GlobalState(critical_constraints=["不得泄露用户手机号"])
    after = GlobalState(critical_constraints=["不得泄露用户手机号等隐私信息"])
    assert _lost_critical_fields(before, after) == []


def test_lost_fields_genuine_constraint_drop_flagged():
    before = GlobalState(critical_constraints=["必须使用 HTTPS 上传"])
    after = GlobalState(critical_constraints=[])
    assert _lost_critical_fields(before, after) == ["critical_constraints"]


def test_lost_fields_entity_key_reworded_not_flagged():
    """实体键名轻微改写 (订单号→订单编号) 不算丢失。"""
    before = GlobalState(key_entities={"订单号": "A-123"})
    after = GlobalState(key_entities={"订单编号": "A-123"})
    assert _lost_critical_fields(before, after) == []


def test_lost_fields_archived_entity_exempt():
    """实体已从 key_entities 移除, 但其值出现在 archived_actions 中 → 豁免。

    旧实现 set(key_entities) - set(archived_actions) 命名空间不相交, 从未生效。
    """
    before = GlobalState(key_entities={"订单号": "A-123"})
    after = GlobalState(archived_actions=["已完成订单 A-123 的退款"])
    assert _lost_critical_fields(before, after) == []


def test_lost_fields_genuine_entity_drop_flagged():
    before = GlobalState(key_entities={"订单号": "A-123"})
    after = GlobalState()
    assert _lost_critical_fields(before, after) == ["key_entities"]


def test_lost_fields_task_goal_whitespace_normalized():
    before = GlobalState(task_goal="帮用户退款")
    after = GlobalState(task_goal="  帮用户退款\n")
    assert _lost_critical_fields(before, after) == []


class _DriftCU(_StubCU):
    """编辑前快照含关键实体, 重算后缺失 (模拟两遍 LLM 摘要的裁量漂移)。"""

    def __init__(self, num_chunks: int):
        super().__init__(num_chunks)
        for ci in range(num_chunks):
            self._snapshots[ci] = GlobalState(key_entities={"订单号": "A-123"})

    def update_state_chunk(self, session, current_state, chunk_idx: int, cfg=None):
        self.calls.append(chunk_idx)
        return GlobalState()


class _ConfirmCfg:
    consistency_max_llm_calls = 10
    consistency_rollback_on_entity_loss = True
    consistency_semantic_confirm = True
    consistency_constraint_similarity = 0.6


def test_unconfirmed_loss_keeps_edits():
    """复核判定为摘要漂移 (未确认丢失) → 保留编辑, 不回滚。"""
    cu = _DriftCU(num_chunks=2)
    records = [_record("b0"), _record("b1")]
    with patch("reassembly.reassembler._confirm_loss_with_llm", return_value=False):
        out = _validate_edit_consistency(None, records, cu, _ConfirmCfg())
    assert all(r.result == "success" for r in out)


def test_confirmed_loss_rolls_back_and_restores_block():
    """复核确认真丢失 → 回滚该 chunk 编辑并把 block 恢复为 original_content。"""
    from domain import Session, Message

    session = Session(session_id="rb", messages=[
        Message(role="assistant", id="a1", blocks=[
            {"type": "thinking", "id": "b0", "thinking": "new"},
        ]),
    ])
    cu = _DriftCU(num_chunks=1)
    rec = _record("b0")
    with patch("reassembly.reassembler._confirm_loss_with_llm", return_value=True):
        out = _validate_edit_consistency(session, [rec], cu, _ConfirmCfg())
    assert out[0].result == "rollback"
    assert out[0].edit_status == StepEditStatus.ROLLBACK
    assert out[0].refined_content is None
    assert session.messages[0].blocks[0].thinking == "old"


def test_semantic_confirm_disabled_rolls_back_directly():
    """关闭复核时保持旧的直接回滚行为。"""
    from domain import Session, Message

    class _NoConfirmCfg(_ConfirmCfg):
        consistency_semantic_confirm = False

    session = Session(session_id="rb2", messages=[
        Message(role="assistant", id="a1", blocks=[
            {"type": "thinking", "id": "b0", "thinking": "new"},
        ]),
    ])
    cu = _DriftCU(num_chunks=1)
    rec = _record("b0")
    with patch("reassembly.reassembler._confirm_loss_with_llm") as mock_confirm:
        out = _validate_edit_consistency(session, [rec], cu, _NoConfirmCfg())
    assert not mock_confirm.called
    assert out[0].result == "rollback"
