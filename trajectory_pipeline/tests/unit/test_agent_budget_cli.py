"""``--max-steps`` → ``AgentBudget`` 的注入通道（2026-11-27 · 编年史 §三）。

**为什么要有这个通道**：``p08_bc_agent_20261125`` 那批 5 个真跑进 agent 的
中间页全部「走满 8 步、候选 0、零失败步」，三条 ``links`` 全落在第 9 步
⇒ 短板是 ``AgentBudget.max_steps=8``。但那个数**不可从命令行注入**，
要验证「12 够不够」必须改生产默认值——而 8 是按任务②的实测形态定的，
改它等于一次改掉所有批次的形态。

⇒ 补一条**可注入但不改默认**的通道，并把「这批给的是几」落进
``run_config``。这组门禁盯的是三件事：

1. 缺省 ⇒ **交付默认值**（8 / 12），不猜、不放大；
2. 显式给 ⇒ ``llm_calls`` **跟着联动**（步数还有、模型已经调不动，
   与步数用完的症状同形，必须一起放大）；
3. 非法值 ⇒ **报错**，不产出一批「一步都没走、看起来正常的空批」。
"""

from __future__ import annotations

import argparse

import pytest

from trajectory_pipeline.executor.agent_loop import AgentBudget
from trajectory_pipeline.executor.cli import (
    _budget_from_args,
    _max_steps_from_args,
)
from trajectory_pipeline.executor.orchestrator import RunConfig


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


class Test缺省交付默认值:
    def test_不给max_steps就用AgentBudget默认(self):
        b = _budget_from_args(_args(max_steps=None))
        # 与 AgentBudget 自己比，而不是写死 8——写死会在有人改默认时
        # 变成第二条真源
        assert b == AgentBudget()
        assert b.max_steps == 8 and b.max_llm_calls == 12

    def test_属性压根不存在时也走默认(self):
        # 手工路径与计划路径的 args 是同一个 Namespace，但测试与老调用点
        # 可能没有这个键；缺键不该炸，该走默认
        assert _budget_from_args(_args()) == AgentBudget()


class Test显式给值:
    def test_步数与llm_calls联动(self):
        b = _budget_from_args(_args(max_steps=12))
        assert b.max_steps == 12
        # 12 × 1.5 = 18：**不是沿用 12**。沿用会让「步数还有、模型调不动」
        assert b.max_llm_calls == 18

    def test_联动关系与默认值同源(self):
        # 默认 8 / 12 就是 8 × 1.5；显式给 8 必须得到同一组数
        assert _budget_from_args(_args(max_steps=8)) == AgentBudget()

    def test_奇数步数向下取整不越界(self):
        b = _budget_from_args(_args(max_steps=3))
        assert b.max_llm_calls == 4  # int(4.5) = 4


class Test非法值:
    @pytest.mark.parametrize("bad", [0, -1, -8])
    def test_非正数报错(self, bad):
        with pytest.raises(ValueError, match="正数"):
            _budget_from_args(_args(max_steps=bad))


class Test落进run_config:
    def test_agent_max_steps缺省是None而不是8(self):
        # **None 与 8 不是一回事**：None = 「没显式指定，用的是默认」，
        # 8 = 「这批显式要了 8」。混成一个数就分不出「默认批」与「试过 8 的批」
        assert RunConfig().agent_max_steps is None

    def test_显式值原样落盘(self):
        cfg = RunConfig(agent_max_steps=12)
        assert cfg.agent_max_steps == 12
        # run_config 是 asdict(cfg)：键必须在，否则读档人看不出这批给了几
        import dataclasses

        assert dataclasses.asdict(cfg)["agent_max_steps"] == 12

class Test不崩在缺属性上:
    """⚠️ **回归记录（2026-11-27 当场踩到）**：`cmd_run` 里曾直接写
    `agent_max_steps=args.max_steps`，而全树有 **21 个单测文件**自己构造
    `Namespace` 去调它、**里面没有这个字段** ⇒ `AttributeError` 让
    **25 条**单测失败，症状看起来像「改坏了 cmd_run」而不是「取参数取窄了」。

    ⇒ 取参数一律走 :func:_max_steps_from_args（内部 `getattr`），
    缺属性**落到默认值 `None`**，不整批崩。
    """

    def test_Namespace没有max_steps也不崩(self):
        assert _max_steps_from_args(_args()) is None

    def test_与_budget_from_args_对同一个缺失属性口径一致(self):
        # 两处取参数若口径不一，就会出现「预算给了 12、run_config 记 None」
        # ——报告上完全同形，事后查不出这批到底给了几
        missing = _args()
        assert _max_steps_from_args(missing) is None
        assert _budget_from_args(missing) == AgentBudget()
