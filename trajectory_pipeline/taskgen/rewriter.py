"""LLM 改写层——**接口定死，W1 不接实现**。

W1 的表述全部由 :mod:`trajectory_pipeline.taskgen.persona.renderer`
的代码渲染产出，本模块在 W1 阶段是空的。这不是"还没来得及写"，
是刻意的**两期替换**（与感知层同一策略）：

    接口一次定死 → W1 用 Null（fail-closed） → W3 接 LLMPerceptor 时换实现

为什么先定死
------------
LLM 改写一旦晚于控制流接入，就会被当成"顺手加的"而绕过归一检查——
而归一检查（:mod:`normalizer`）恰恰是"只改表述不改判分"的守卫。
先把协议和"改写结果必须过 normalizer"这条调用约定定下来，
W3 换实现时才有地方可挂。

W3 接入时必须一并落实的两件事
------------------------------
1. 输出**必须**经 :func:`trajectory_pipeline.taskgen.normalizer.normalize`，
   且这个依赖要写在实现里而不是靠调用方记得——靠记得的纪律一定会被绕过。
2. 本项目 LLM 后端**不强制 json_schema**，模型会漂字段名、套 think、
   首轮跑偏（存量的同类教训）。所以 W3 的实现必须自带
   **别名容错 + 重试 + fail-soft**，且用真实端点验一次。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from trajectory_pipeline.taskgen.persona.schema import PersonaProfile
from trajectory_pipeline.taskgen.skeleton import TaskSkeleton


@runtime_checkable
class Rewriter(Protocol):
    """任务表述改写器。

    契约：
      - **只改表述**，不得触碰判分标准（``skeleton.criterion_texts``、
        ``output_contract``、``excluded_platforms`` 一律只读）
      - **不得抛异常**到调用方——失败返回原文（fail-soft），
        因为改写是锦上添花，失败不该毁掉一个本来可用的任务实例
      - 幂等：同一 ``(skeleton, persona, seed)`` 重复调用应给出等价结果
    """

    name: str

    def rewrite(
        self, skeleton: TaskSkeleton, persona: PersonaProfile, *, seed: int = 0
    ) -> str:
        """返回改写后的**首轮表述**。失败返回 ``skeleton.initial_request``。"""
        ...


class NullRewriter:
    """W1 实现：不做任何改写，原样返回。

    ``name`` 取 ``"null"`` 而不是伪装成 ``"rule"``——存档里必须能一眼看出
    这批表述是**骨架原文**而非改写产物。混淆这两者的后果是：将来接了 LLM
    再回头看这批老数据，会把它当成"改写效果"的基线。
    """

    name = "null"

    def rewrite(
        self, skeleton: TaskSkeleton, persona: PersonaProfile, *, seed: int = 0
    ) -> str:
        return skeleton.initial_request

    def health(self) -> tuple[bool, str]:
        """自述能力边界。``--check`` 类命令据此显示"W1 表述未经 LLM 润色"。"""
        return True, "W1 未接 LLM：表述由代码渲染器产出，判分保真由 normalizer 强制"