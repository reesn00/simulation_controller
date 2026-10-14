"""共享字面量源唯一性测试。

scout 端的 fail-closed 白名单与 executor 端的 L5/AUTO 白名单
字面量必须保持单一真源;本测试拦截任何在 scout 端偷写字面量的提交。
"""

from __future__ import annotations

from pathlib import Path

import pytest

EXECUTOR_FAIL_CLOSED_PATH = Path(
    "trajectory_pipeline/executor/arbitration.py"
)
EXECUTOR_AUTO_REVIEW_PATH = Path(
    "trajectory_pipeline/executor/review_queue.py"
)
SCOUT_FAIL_CLOSED_PATH = Path(
    "trajectory_pipeline/evaluation/scout/fail_closed.py"
)
EVAL_MIRROR_PATH = Path(
    "trajectory_pipeline/evaluation/_fail_closed_notes.py"
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


#: 5 个 fail-closed / auto-review 字符串。这是从两个白名单合并去重的结果,
#: **只用于断言另一端是否写死了**;真源是 ``arbitration.L5_FAIL_CLOSED_NOTES`` 与
#: ``review_queue.AUTO_REVIEW_NOTES``。
SHARED_FAIL_CLOSED_TOKENS = (
    "credential_leak", "driver_missing", "timeout", "driver_error",
    "vlm_error",
)


class TestL5FailClosedUniqueSource:
    def test_真源包含全部5项(self) -> None:
        src_arb = _read(EXECUTOR_FAIL_CLOSED_PATH)
        src_rev = _read(EXECUTOR_AUTO_REVIEW_PATH)
        for tok in SHARED_FAIL_CLOSED_TOKENS:
            assert tok in src_arb or tok in src_rev, (
                f"真源少了 {tok!r};executor 端需要把它加到 L5_FAIL_CLOSED_NOTES 或 AUTO_REVIEW_NOTES"
            )


class TestEvalMirrorDoesNotRedefine:
    """evaluation 包内的镜像模块也不能写字面量 —— 它必须纯 import。

    锁这条:executor 是唯一真源,任何 evaluation 侧的私有镜像都只能
    通过 ``from ... import L5_FAIL_CLOSED_NOTES / AUTO_REVIEW_NOTES``
    镜像。
    """

    @pytest.mark.parametrize("tok", SHARED_FAIL_CLOSED_TOKENS)
    def test_镜像模块无字面量(self, tok: str) -> None:
        src = _read(EVAL_MIRROR_PATH)
        assert tok not in src, (
            f"{tok!r} 出现在 evaluation/_fail_closed_notes.py;"
            "镜像模块也只能 import,不能复制字面量"
        )


class TestScoutFailClosedDoesNotRedefine:
    @pytest.mark.parametrize("tok", SHARED_FAIL_CLOSED_TOKENS)
    def test_scout端无字面量(self, tok: str) -> None:
        src = _read(SCOUT_FAIL_CLOSED_PATH)
        assert tok not in src, (
            f"{tok!r} 出现在 scout/fail_closed.py;"
            "只能通过 from ... import L5_FAIL_CLOSED_NOTES / AUTO_REVIEW_NOTES 镜像"
        )
