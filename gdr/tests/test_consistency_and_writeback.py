"""回归测试: 一致性校验真增量 (O(N)) 与 refine_records 按 block_id 写回。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.context_understanding import GlobalState
from domain import BlockIndex, BlockRefineRecord, StepEditStatus
from reassembly.reassembler import reassemble, _validate_edit_consistency


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
