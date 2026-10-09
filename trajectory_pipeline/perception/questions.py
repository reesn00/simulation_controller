"""判断题注册表——**一道题与它的负样本分支的绑定关系只在这里定义一次**。

为什么值得单开一个模块：四个判断点如果各写各的，「我判 false 时这条样本归到
哪个失败分支」这件事就会散落在控制流里，然后**迟早漏掉一条**——负样本池少一支，
训练数据里就少一类失败原因，而这种缺失是静默的：报表上分部数字都在，
就是有一支永远是 0。:attr:`Question.negative_branch` 把这个绑定提到类型层面，
新增失败分支时必须同步填，否则构造时就报错。

W1 阶段只有 :data:`TRAILER_ONLY_NEEDS_LLM = False` 的题能确定性判定，
其余返回 ``answer=None``（I4 fail-closed）——**不猜**。
这不是「没实现」，是 W1 的正确行为：宁可这一批全是 unresolved，
也不能让规则版假装能判断语义。
"""

from __future__ import annotations

from types import MappingProxyType

from trajectory_pipeline.perception.base import Q, Question

#: W1 规则版能确定性判定的题只有一道：**按钮文本是不是「预告片」**。
#: 它是纯文本匹配，不需要理解页面语义。其余三题都要语义，W1 一律 fail-closed。
TRAILER_ONLY_NEEDS_LLM = False


REGISTRY: MappingProxyType[str, Question] = MappingProxyType(
    {
        Q.SELECT_PLAY_SITES: Question(
            id=Q.SELECT_PLAY_SITES,
            prompt=(
                "以下是从搜索结果页取回的链接。"
                "选出其中**提供在线观看该作品**的站点，"
                "并说明选择理由；不选的要说明排除理由。"
            ),
            answer_type="decision",
            schema_hint=MappingProxyType(
                {
                    "selected": [{"url": "str", "title": "str", "why": "str"}],
                    "rejected": [{"url": "str", "reason": "str"}],
                }
            ),
            # selected **和** rejected 都要落盘——只存 selected 就无法复核
            # 「为什么把爱奇艺排掉了」，负样本池会退化成「没被选中的都算失败」。
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="not_play_site",
        ),
        Q.IS_REACHABLE: Question(
            id=Q.IS_REACHABLE,
            prompt=(
                "这个页面是否**正常打开了内容**？"
                "登录墙、验证码、404、地区限制、空白页都算不可达。"
                "注意：内容里**提到**「请登录」不代表不可达——"
                "要判断的是页面主体是不是被挡住了。"
            ),
            answer_type="bool",
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="login_wall_or_blocked",
        ),
        Q.FIND_PLAY_CONTROL: Question(
            id=Q.FIND_PLAY_CONTROL,
            prompt=(
                "页面上是否存在能开始播放的控件（剧集列表项 / 播放按钮 / 播放器）？"
                "**必须回传该控件的 ref**，代码要用它点击，不要自己找。"
                "如果唯一候选的文本是「预告片」/「预告」，把 trailer_only 置为 true。"
            ),
            answer_type="decision",
            schema_hint=MappingProxyType(
                {"ref": "str", "trailer_only": "bool", "why": "str"}
            ),
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="no_play_control",
        ),
        Q.PLAYER_OK: Question(
            id=Q.PLAYER_OK,
            prompt=(
                "这是点击播放控件后到达的页面。"
                "结合代码测得的媒体元素数量，判断它**是不是一个能正常播放的播放器页**。"
            ),
            answer_type="bool",
            confidence_threshold=0.7,
            evidence_required=True,
            negative_branch="component_unverified",
        ),
    }
)

#: 不属于任何一道 Question 的分支——它们由**代码**判定或兜底，不经 LLM。
#: 挂在注册表里是为了让「失败分支全集」有唯一出处（``executor/branches.py`` 校验）。
#:
#: 四类来源：
#:   ``unreachable_hard``  导航失败，代码直接判（``DriverError`` → I4）
#:   ``trailer_only``      预告片词表命中，代码判
#:   ``trailer_suspect``   疑似预告但词表判不准，**留给人工兜底**
#:   ``unresolved``        能力不可用，fail-closed 的落点（I4）
CODE_ONLY_BRANCHES = frozenset({
    "unreachable_hard", "trailer_only", "trailer_suspect", "unresolved",
})


def get(question_id: str) -> Question:
    """取题定义。未知 id 直接 ``KeyError``——**不返回 None**。

    返回 None 会让调用方的 ``if q is None`` 分支把「拼错 id」当成
    「这题不需要判定」静默跳过，是最难查的一类 bug。
    """
    return REGISTRY[question_id]


def negative_branch_of(question_id: str) -> str:
    """取题判 false 时的失败分支——负样本池的入池依据。"""
    return get(question_id).negative_branch or ""


def all_branches() -> frozenset[str]:
    """失败分支全集（8 条）。

    这是契约的一部分：``executor/branches.py`` 落盘时按这个集合校验，
    出现集合外的分支名即报错，避免「分支静默消失」。
    """
    from_question = {q.negative_branch for q in REGISTRY.values() if q.negative_branch}
    return frozenset(from_question | CODE_ONLY_BRANCHES)