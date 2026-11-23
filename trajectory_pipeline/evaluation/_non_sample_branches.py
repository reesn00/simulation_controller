"""非样本分支私有镜像。

``scout/policy.py`` 用到的「没判出来 ⇒ 不进 positive」口径真源在
:mod:`trajectory_pipeline.executor.branches.NON_SAMPLE_BRANCHES`。

设计动机:
    scout 子包刻意不 import executor（契约 ``test_scout_contract`` 盯着，
    ``ast.walk`` 连函数内延迟 import 一起抓），所以分支镜像必须落在
    evaluation 包内、由 scout 引用、且**不**手抄字面量——手抄就会与执行层
    漂成两本规则书。本模块就是那个镜像器，与 ``_fail_closed_notes`` 同款。
"""

from __future__ import annotations

from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES


def is_non_sample_branch(branch: str | None) -> bool:
    """分支是不是「没判出来」的非样本分支。

    返回 ``True`` 的结论即便四维全 5（各维都不管辖记 N/A）也**不**进
    positive——它没有验证通过的结论。``None``（成功）与旧产物缺键
    （``.get`` 返 ``None``）都返 ``False``。分支集合真源在执行层，本模块
    只镜像不手抄。
    """
    return branch in NON_SAMPLE_BRANCHES


__all__ = ["is_non_sample_branch", "NON_SAMPLE_BRANCHES"]