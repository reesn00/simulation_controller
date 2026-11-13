"""persona → 首轮表述的**代码级渲染**。不经 LLM。

为什么不用 LLM 渲染
--------------------
方案 §3.1.3 的流水线里 LLM 只负责 :mod:`rewriter` 的"润色"，
而**可确定的部分由代码先渲染完**（片名槽位、是否附标准）。原因是分工：
persona 管的是**输入分布**，而分布是可以用查表精确构造的；
让 LLM 去渲染，等于把一条本可以用字符串比较验证的规则
变成不可复现的抽样——同一个画像两次渲染出两句不同的话，
切片轴就再也解释不清"到底哪一句喂给了模型"。

渲染结构
--------
::

    [背景句] + [核心要求]

两段各自对应一个 persona 维度，且**都只加在核心要求之外或两端**。
核心要求本身（``skeleton.initial_request``）只允许一处改动：片名的**指代方式**。
之所以只动片名，是因为它正是 ``task_specificity`` 维度存在的意义——
不动它，这一维就是纯装饰。

⚠️ **这里曾有四段**（``[背景句] + [核心要求] + [催促句] + [语气词]``），
催促句与语气词两段已随 ``urgency`` / ``verbal_style`` 两维删除
（2026-11-11）——**时间限定与句尾语气词对 agent prompt 没有意义**，
且它们会变成假切片轴。理由见
:data:`~trajectory_pipeline.taskgen.persona.lexicon.OPENERS` 之后的删除说明。

⚠️ **核心要求必须取 ``initial_request`` 而不是 ``intent.goal``**，
两者措辞不等价。实测 T001：
``initial_request``「找到电视剧《武林外传》**全集**在线观看…」 vs
``goal``「找到电视剧《武林外传》在线观看…」——**``goal`` 里少了「全集」**。

而判分约束是从 ``initial_request`` 推导的（见
:func:`~trajectory_pipeline.taskgen.skeleton._derive_constraints`）。
拿 ``goal`` 去渲染，等于"按 A 推导约束、按 B 生成文本"，
探针于是必然判失败——首次跑就有 75% 的样本被 :mod:`normalizer` 退回，
persona 特征全部失效，而表象只是"归一通过率偏低"，看不出根因。
**渲染源与约束推导源必须同源**，这和词表同源是同一条纪律。
``goal`` 只作元数据，不进渲染。

兼容性矩阵：为什么有些组合必须拦
--------------------------------
persona 的 ``task_specificity`` 与骨架的 ``retrieval_mode`` **可能语义冲突**，
冲突时**不能**随手挑一个渲染，得记下来：

======================  ==========================  ======================
persona 偏好            骨架                        结果
======================  ==========================  ======================
指名                    有片名                      正常
半指代                  有片名                      **downgraded**→指名
纯描述                  有片名                      **incompatible**
指名                    无片名（aggregate 等）      **downgraded**→半指代
======================  ==========================  ======================

「纯描述 × 指名片名」是**逻辑不可能**：用户说不出片名，
却要求找到那一部的播放资源。唯一正确的响应是先澄清，
而 W1 是单步直搜、没有澄清环节——所以这条组合**整条排除**。
假装能渲染，只会产出一批"用户压根没提片名、却检索了那部片子"的轨迹，
而它在切片表里会被当成"纯描述 persona 的表现"。

**W1 的实际表达力只有「指名」一档。** 半指代降级的原因是它需要说出
与**任务内容**相关的线索（"去年那部讲南极的"），而查表渲染器读不懂骨架；
唯一能拿出来的参照物是片名本身，那等于半指名。详见
:data:`~trajectory_pipeline.taskgen.persona.lexicon.SPECIFICITY_RENDER`。

降级与排除都必须**落 provenance**：不记的话，切片表会把
``task_specificity=半指代`` 的分母算成"画像库里的条数"，
而实际喂进去的表述全是指名——统计与事实脱钩，
且现象是"半指代维度表现正常"，比没有这个维度更糟。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final, Mapping

from trajectory_pipeline.taskgen.persona.lexicon import (
    OPENERS,
    SPECIFICITY_RENDER,
)
from trajectory_pipeline.taskgen.persona.schema import PersonaProfile
from trajectory_pipeline.taskgen.skeleton import TaskSkeleton

#: 判定「不可渲染」的组合。用显式集合而不是散在 if 里的两次判断，
#: 是为了让 :func:`render` 的调用方（采样器）能**提前**过滤，
#: 而不必先渲染一遍再读 ``compatibility`` 字段。
INCOMPATIBLE: Final = frozenset({("纯描述", "single_title")})

#: 片名槽位。骨架 goal 里片名已被 :func:`skeleton.extract_title` 认出，
#: 渲染时按 ``《…》`` 形态定位回来。
_TITLE_SLOT: Final = re.compile(r"《[^》]+》")


@dataclass(frozen=True, slots=True)
class RenderResult:
    """渲染产出 + **完整的落地说明**。

    ``applied`` 与 ``compatibility`` 不是调试信息，是 provenance 的一部分：
    读样本的人必须能回答"这个人设是怎么变成这句话的"，
    以及"这个人设和这个任务本来合不合适"。
    """

    prompt_text: str
    search_query: str
    compatibility: str                       # ok / downgraded / incompatible
    applied_specificity: str
    requested_specificity: str
    applied: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.compatibility != "incompatible"

    def provenance_extra(self) -> dict[str, Any]:
        """并进 :class:`~trajectory_pipeline.taskgen.skeleton.TaskInstance`
        的 ``provenance``。键名与 persona 的维度名对齐，便于切片直接按列取。"""
        return {
            "requested_specificity": self.requested_specificity,
            "actual_specificity": self.applied_specificity,
            "compatibility": self.compatibility,
            "rendered_by": "code:renderer",     # 明确不是 LLM 生成的
            **self.applied,
        }


def is_compatible(persona: PersonaProfile, skeleton: TaskSkeleton) -> bool:
    """这条画像能不能配这个骨架。采样器用它在**渲染前**过滤。"""
    return (persona.task_specificity, skeleton.retrieval_mode) not in INCOMPATIBLE


def compatible_count(
    profiles: tuple[PersonaProfile, ...], skeletons: tuple[TaskSkeleton, ...]
) -> int:
    """可配对数。采样前报一次，避免跑完才发现大半组合被拦。"""
    return sum(1 for p in profiles for s in skeletons if is_compatible(p, s))


def _pick(options: tuple[str, ...], salt: str) -> str:
    """从候选片段里**确定性**选一个。

    用 ``sha256`` 而不是内置 ``hash()``：后者对 str 的加盐是每进程随机的，
    同一份输入在两次运行里会渲染出不同的话，重放就对不上。
    """
    if not options:
        return ""
    import hashlib

    digest = hashlib.sha256(salt.encode("utf-8")).digest()
    return options[digest[0] % len(options)]


def _apply_specificity(
    goal: str, title: str | None, persona: PersonaProfile, *, salt: str
) -> tuple[str, str, tuple[str, ...]]:
    """按 persona 偏好改写片名的**指代方式**。

    返回 ``(改写后的核心要求, 实际 specificity, 降级说明)``。

    **W1 实际只能表达「指名」**（理由见
    :data:`~trajectory_pipeline.taskgen.persona.lexicon.SPECIFICITY_RENDER`）：
    半指代要说出与任务内容相关的品类线索，而查表渲染器读不懂骨架，
    唯一能拿出来的参照物就是片名本身——那是半指名，不是半指代。
    所以这里把半指代**降级为指名**并把原因带出去，而不是伪装成已实现。
    """
    requested = persona.task_specificity

    # 无片名的骨架：片名槽位不存在，三档都只能按描述走。
    if title is None or _TITLE_SLOT.search(goal) is None:
        if requested == "指名":
            return goal, "半指代", ("骨架无可用片名，指名降级为半指代",)
        return goal, "纯描述", ("骨架无可用片名，只能按描述表述",)

    # 指名：原样保留。骨架本来就带片名，这里不做任何替换，
    # 免得"重写"产生与骨架不一致的片名形态。
    if requested == "半指代":
        return goal, "指名", (
            "半指代需按任务内容给线索，查表渲染器读不懂骨架 → 降级为指名"
            "（要真正实现半指代需 W3 的 LLM 改写器）",
        )
    return goal, "指名", ()


def render(
    skeleton: TaskSkeleton, persona: PersonaProfile, *, seed: int = 0
) -> RenderResult:
    """骨架 + 画像 → 首轮表述与检索式。

    **不做归一检查**——那是 :mod:`normalizer` 的职责，两者分开是刻意的：
    渲染器只负责"怎么拼"，检查器只负责"拼完还对不对"。
    合成一个函数的话，改写失败与渲染失败会混成同一种错误，
    而这两者的处置完全不同（前者退回原文，后者直接崩）。
    """
    salt = f"{seed}|{skeleton.task_id}|{persona.persona_id}"

    if not is_compatible(persona, skeleton):
        return RenderResult(
            prompt_text=skeleton.initial_request,
            search_query="",
            compatibility="incompatible",
            applied_specificity="",
            requested_specificity=persona.task_specificity,
            notes=("纯描述人设无法指名到具体片名，而该任务是单片检索；"
                   "唯一正确响应是先澄清，W1 无澄清环节——整条排除",),
        )

    core, actual_spec, spec_notes = _apply_specificity(
        skeleton.initial_request, skeleton.title, persona, salt=salt
    )
    compat = "ok" if actual_spec == persona.task_specificity else "downgraded"

    opener = _pick(OPENERS.get(persona.persona_presence, ()), salt + "|open")

    # ⚠️ ``genre`` **刻意不进表述**——它是检索侧维度，不是「用户要什么」。
    # 早期版本写成 ``"我想找{genre}，{goal}"``，实测产出过「我想先确认电影
    # **类似《功夫》那样的动作片**的版权方…」（T027）——persona 的品类偏好
    # 被冒充成了用户需求，而这类污染**不报错**：表述读着通顺，normalizer 的
    # 探针也只查判分要求有没有丢。
    # 根因是**层级错配**（见 :data:`lexicon.OPENERS` 的说明）：查表渲染器
    # 读不懂骨架，**没有**「这个任务确实在做该品类检索」的判据。
    # ⇒ 这里给空串是**有意为之**，不是漏写；反回归测试
    # ``tests/unit/test_taskgen.py::test_genre不注入表述`` 逐词条守着它。
    genre_word = ""
    text = opener.format(genre=genre_word, goal=core) if opener else core

    applied: dict[str, str] = {}
    if opener:
        applied["opener"] = "persona_presence"
    if actual_spec != "指名":
        applied["specificity_form"] = actual_spec
    # genre 明确记「未落到表述」——写进 provenance 是为了让人能区分
    # 「这一维没生效（有意为之）」与「这一维忘了做（漏了）」。
    applied["genre_in_prompt"] = "no:检索侧维度，不改用户表述"
    applied["standard_hint"] = "attached" if persona.has_standard else "bare"

    return RenderResult(
        prompt_text=text,
        search_query=_search_query(skeleton),
        compatibility=compat,
        applied_specificity=actual_spec,
        requested_specificity=persona.task_specificity,
        applied=applied,
        notes=spec_notes,
    )


def _search_query(skeleton: TaskSkeleton) -> str:
    """检索式。

    ⚠️ **始终用片名，与 persona 的指代方式无关**。这是有意的：
    检索是**代码执行层**的动作，它必须能真的定位到那个站的播放页；
    persona 改的是**用户怎么说**，不是世界的事实。
    把"纯描述人设"直接贯彻到检索式上，会得到一条指向不存在作品的查询——
    代码里那个空 ``title`` 不是可以顺延的巧合，它是"用户没说片名"的真实后果。

    无片名的任务返回空串，调用方**必须**自行处理（见
    :func:`~trajectory_pipeline.taskgen.skeleton.TaskSkeleton.runnable_in_w1`）。
    """
    return f"{skeleton.title} 在线观看" if skeleton.title else ""