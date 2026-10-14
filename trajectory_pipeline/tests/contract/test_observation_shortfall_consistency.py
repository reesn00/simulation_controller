"""四个 ``_observation_shortfall`` 实现之间的**决策边界一致性**契约。

## 为什么需要这条契约

``observation_shortfall`` 散落在四处：

  - :func:`trajectory_pipeline.rationale.gates.observation_shortfall`
    ——事前判据（rationale/gates.py 闸门三查）
  - :func:`trajectory_pipeline.evaluation.scorers.code_scorer._observation_shortfall`
    ——code judge 侧（D-2 / D-4 / D-5 / D-6）
  - :func:`trajectory_pipeline.evaluation.scorers.d3_scorer._observation_shortfall`
    ——LLM judge 侧（D-3）
  - :func:`trajectory_pipeline.evaluation.scorers.d9_scorer._observation_shortfall`
    ——LLM judge 侧（D-9）

四份代码**刻意分开实现**——rationale 不能 import evaluation
（``evaluation → rationale`` 单向依赖方向，方案 10-§四）。
但「能不能判」必须**口径一致**，否则 D-9 闸门过 + d9_scorer
undetermined（或反之），同一条样本在两处会有相反的处理路径。

## 一致性原则（不是口径核对）

契约盯的是**决策边界**——「同一份观察，四处是否同判
能 / 不能」——**不**盯**字面措辞**。每处可以有自己的措辞
（gates.py 在 trunc 消息里夹了「CSS 会抢光预算」的额外说明，
code_scorer 在 degraded 消息里强调「缺项无法与『真没有』区分」，
d3/d9 走中性版本），但**同输入必须同判定**。
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from trajectory_pipeline.evaluation.scorers import (
    code_scorer,
    d3_scorer,
    d9_scorer,
)
from trajectory_pipeline.rationale import gates


# ── fixture：覆盖 decision boundary 的全部必需场景 ─────────────────


def _obs(**kw: Any) -> Mapping[str, Any]:
    """一份「可判」观察的默认形态。kw 覆盖字段即可触发各边界。"""
    base = {
        "body_text": "页面有 video 标签 + iframe 内嵌",
        "interactive_elements": [{"tag": "a", "label": "链接0"}],
        "elements_total": 1,
        "links": [],
        "links_total": 0,
        "degraded": [],
        "truncated": False,
    }
    base.update(kw)
    return base


# 同一份观察的「可判」形态——四处必须都返回空串
_CAN_JUDGE = _obs()

# 同一份观察的 5 种「不可判」形态——四处必须都返回非空串
# （字面措辞各实现可不同）
_CANT_JUDGE_CASES = [
    pytest.param(_obs(degraded=["video_tag_count"]), id="degraded-非空"),
    pytest.param(_obs(truncated=True), id="truncated-真"),
    pytest.param(
        _obs(interactive_elements=[{"tag": "a", "label": "L"}], elements_total=5),
        id="elements-被裁剪",
    ),
    pytest.param(_obs(body_text="", interactive_elements=[]), id="body-与-elements-皆空"),
]


# ── helpers ────────────────────────────────────────────────────────


def _can_judge(s: str) -> bool:
    """空串 = 能判，非空 = 不能判。"""
    return s == ""


def _judge(obs: Any | None) -> dict[str, str]:
    """跑四个实现，记下 ``can_judge`` + reason。"""
    return {
        "gates": gates.observation_shortfall(obs),
        "code_scorer": code_scorer._observation_shortfall(obs),  # type: ignore[arg-type]
        "d3_scorer": d3_scorer._observation_shortfall(obs),      # type: ignore[arg-type]
        "d9_scorer": d9_scorer._observation_shortfall(obs),      # type: ignore[arg-type]
    }


# ── 契约 1：可判样本四处一致 ──────────────────────────────────────


class Test可判_边界一致:
    """完整可判的观察——四处必须**都返回空串**。"""

    def test_完整可判_四处都空串(self):
        results = _judge(_CAN_JUDGE)
        for name, r in results.items():
            assert r == "", (
                f"{name}._observation_shortfall 在完整可判样本上返回非空"
                f"：{r!r}——决策边界漂了，与本契约冲突"
            )


# ── 契约 2：不可判样本四处一致 ────────────────────────────────────


class Test不可判_边界一致:
    """同一份观察在某条判据上不充分时——四处必须**都返回非空**。"""

    @pytest.mark.parametrize("obs", _CANT_JUDGE_CASES)
    def test_不可判_四处都非空(self, obs: Any):
        results = _judge(obs)
        for name, r in results.items():
            assert r != "", (
                f"{name}._observation_shortfall 在不可判样本上返回空串"
                f"——决策边界漂了；这条样本会在该模块被错判为可判"
            )


# ── 契约 3：决策向量两两一致（最严版本） ─────────────────────────


class Test决策向量_两两一致:
    """把多处实现两两对比，决策向量完全一致。

    任何一项决策不一致都视为一处漂移，需修口径（而不是修本测试）。
    """

    @pytest.mark.parametrize("obs", [_CAN_JUDGE, *_CANT_JUDGE_CASES])
    def test_两两_同判(self, obs: Any):
        results = _judge(obs)
        keys = list(results)
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                a, b = keys[i], keys[j]
                same = _can_judge(results[a]) == _can_judge(results[b])
                assert same, (
                    f"{a} 与 {b} 在同一份观察上决策不一致：\n"
                    f"  {a}={results[a]!r}\n"
                    f"  {b}={results[b]!r}\n"
                    f"——『能 / 不能判』口径漂了，需要修对应实现（不要再"
                    f"改本测试）。"
                )

# 注：``obs=None`` 不在本契约覆盖范围——
# gates.py / d3_scorer / d9_scorer 都返回 ``"没有观察"``（rationale 闸门
# 与 LLM judge 都会传 None 进去），但 ``code_scorer._observation_shortfall``
# **不**做 None 守卫（直接 ``obs.get('degraded')``）——它只在观测字典非空
# 时被调用（code judge 的 callers 永远不会传 None）——这是已知的能力差异，
# 不是决策漂移。如需对齐，单独开一个「code_scorer None 守卫」增量，本契
# 约不背这个锅。
