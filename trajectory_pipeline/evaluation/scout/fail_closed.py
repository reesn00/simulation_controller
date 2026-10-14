"""fail-closed 白名单镜像。

设计上**不**重复字面量。本模块只从 evaluation 包内的私有镜像
:mod:`trajectory_pipeline.evaluation._fail_closed_notes` 取白名单,
由 :mod:`trajectory_pipeline.executor.arbitration` 与
:mod:`trajectory_pipeline.executor.review_queue` 维护唯一真源。

契约测试 ``tests/contract/test_shared_literal_sync.py`` 用 AST 断言
本模块**不含**这五条字符串字面量 —— 任何人想偷懒把字面量复制过来,
那个测试就会红。
"""

from __future__ import annotations

from trajectory_pipeline.evaluation._fail_closed_notes import is_fail_closed_note


__all__ = ["is_fail_closed_note"]

# is_fail_closed_note 由 evaluation._fail_closed_notes 镜像真源。
