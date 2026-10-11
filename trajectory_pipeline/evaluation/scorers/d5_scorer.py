"""代码断言层评分器 —— **D-5「记录/排除决策正确性」**（★ 样本级一票否决）。

## 为什么 D-5 是纯代码层

rubric v1.1 把 D-5 的管辖层定为**代码断言**，判据是一条**决策树**，
每个节点都标注了代码分支名::

    非播放站/无关站        → 排除 ✅  branch=not_play_site
    是播放站，无播放入口    → 排除 ✅  branch=no_play_control
    播放页无 video/组件    → 排除 ✅  branch=component_unverified
    有组件但无法确认可用    → 标记"未验证" ✅  branch=player_unverified
    有组件且可确认          → 记录 ✅  branch=null

这条树**不需要人来对照**——每个节点都是一个可机械验证的谓词。
它要核的是三件事，每件都能从存档里算出来：

1. **分支与判断点是否配套**（``question`` ↔ ``branch``）。
   判断点 ② 判出来的结论不可能是 ``no_play_control``——那是判断点 ③ 的分支。
   对不上就是「分支走错」（锚点 3）。
2. **结论的前提是否成立**（``reached_play_page`` ↔ 分支）。
   ``component_unverified`` 的定义是「**有 video 标签**但播放器未正常加载」，
   而 ``player_unverified`` / 成功记录的**前提是走到了播放页**。
   前提不成立却贴了这条标签 = rubric 锚点 4 的「1 处应『未验证』却直接记录/排除」。
3. **有没有跳步**（``branch is None`` 但 ``reached_play_page`` 为假）。
   这是 rubric 锚点 1 明写的反例：「首页见『立即观看』字样直接记录，
   未进播放页」——**看 URL 直接给结论，没有中间过程**。

## 三条不能含糊的地方

**「没到播放页」不等于「不能给结论」。**
判断点 ①②③ 的结论**本来就**发生在到达播放页之前
（``not_play_site`` / ``login_wall_or_blocked`` / ``no_play_control``），
所以它们 ``reached_play_page=False`` 是**正确**的。
只有 ④ 的分支才要求 ``reached_play_page=True``。
把这条搞反的后果是：整批负样本会被判成「跳步」，而那正是判定逻辑本身
的样子——**评分器判反比不判更贵**。

**一票否决只在 1 分触发。**
rubric G-1：样本级一票否决项得 **1 分** → 判不合格。
3 分的「分支走错」不触发——它还没到结论错的地步。

**「查不了」与「错了」分开。**
存档缺 ``question`` / ``reached_play_page``（老批次）时判**不可判定**，
不判 3 分更不判 1 分：拿一个空字段去比对，得到的结论是**采集器缺字段**
造成的，不是轨迹错了——与 :mod:`trajectory_pipeline.evaluation.scorers.code_scorer`
的 D-2 同一口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from trajectory_pipeline.assembler.schema import Sample

#: 判断点 → 该判断点在**判 False** 时允许产生的分支。
#:
#: **从 :data:`trajectory_pipeline.perception.questions.REGISTRY` 推导**，
#: 不在这里手写字面量。手写那张表的代价在实测里立刻兑现了：
#: 第一版漏了 ``unresolved``（它经 :meth:`RunLedger.record_from` 从
#: **任何**判断点产生，是 fail-closed 的通用出口），
#: 于是 48 条 ``find_play_control → unresolved`` 全被判成「分支走错」，
#: 把**正确**的结论扣到 3 分。判定逻辑与它的约束不同源时，
#: 两边会安静地互相打架（避坑指南 §1.1）。
#:
#: 这里叠加三条注册表**没有**表达的规则：
#:
#: 1. ``unresolved`` 是通用出口——``record_from`` 在 ``answer is None`` 时
#:    **一律**记它，与判断点无关；
#: 2. ``unreachable_hard`` / ``trailer_only`` 由**代码**判定不经 LLM，
#:    所以它们**没有** question，不参与「分支↔判断点」配对；
#: 3. ``component_unverified`` 另有一个 ``find_play_control`` 的产地
#:    （``visit.py`` 环节④「answer=True 却无 ref」时显式记账），
#:    注册表里那个是 ``player.ok`` 的 —— 注册表只登记**判 False** 的路径。
UNRESOLVED = "unresolved"

#: 由代码判定、**不经 ``negative_branch`` 登记**的分支：没有对应的
#: question 可配对，所以不参与「分支↔判断点」配对。
#:
#: - ``unreachable_hard`` / ``trailer_only`` —— 控制流/词表直接判定，
#:   连 LLM 都不经过，question 本就是空的；
#: - ``trailer_suspect`` —— 词表判不准的**人工兜底**分支，与
#:   ``trailer_only`` 同样由代码在环节③ 分流（见避坑指南 §6.5 的
#:   「两个判断并列，suspect 优先」）。它**不是**负样本，
#:   所以也不在注册表的 ``negative_branch`` 里——那份表只登记负分支。
CODE_ONLY_BRANCHES: frozenset[str] = frozenset(
    {
        "unreachable_hard",
        "trailer_only",
        "trailer_suspect",
    }
)

#: 代码层**确定性事实核**（不经 LLM）产生的 ``(question, branch)`` 组合，
#: 其中 ``question`` 恒为空——核的判据全部在代码里，没有判断题可配对。
#:
#: 与 :data:`CODE_ONLY_BRANCHES` 的分工：那组分支**只**由代码产生、名字
#: 本身不与任何判断点冲突；这里的分支（``not_play_site``）**同名于**判断点
#: ① 的负分支（``select.play_sites`` 判 False 时也记它），不能整条塞进
#: ``CODE_ONLY_BRANCHES``——否则 ``("select.play_sites", "not_play_site")``
#: 的配对检查会被跳过，而那一侧正是需要核「分支走错」的地方。
#:
#: 目前唯一成员来自 ``executor.orchestrator._drop_site_homepages``：搜索
#: 结果里 path 为 ``/`` 的 URL 在**任何判断点之前**就被代码判定为站点
#: 首页（结构上不可能是播放页），直接记 ``not_play_site`` 且不带
#: ``question``。新增同类的确定性事实核时必须同步这里——
#: ``tests/unit/test_d5_scorer.py::Test同源`` 盯着真实存档里出现的每一对。
QUESTIONLESS_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("", "not_play_site"),
    }
)


def _build_question_to_branches() -> Mapping[str, frozenset[str]]:
    """从判断题注册表推导分支映射。**单一出处**在 ``questions.py``。

    注册表的 ``negative_branch`` 只登记**判 False** 的路径，所以从它
    推导会漏掉三条同样合法的分支，各有各的原因：

    - ``unresolved`` —— ``record_from`` 在 ``answer is None`` 时**一律**记它，
      与判断点无关（fail-closed 的通用出口）；
    - ``player_unverified`` —— 走 ``record_unresolved_reached``，**不经过**
      ``record_from``，所以压根不查 ``negative_branch``；
    - ``component_unverified`` 在 ``find_play_control`` 侧 —— 环节④
      「answer=True 却无 ref」的 I6 违约记账，显式指定分支；
    - ``subscription_wall``（2026-10-10）—— ④ 的**细分结论**，
      由 ``Decision.payload["paywall"]`` 选用而不是题绑定，
      所以它同样不出现在 ``negative_branch`` 里（口径见
      :data:`trajectory_pipeline.perception.questions.PAY_WALL`）。

    ⚠️ **这张表漏一条的代价与当年一样**：第一版手写字面量漏了
    ``unresolved``，**54 条正确结论被判成「分支走错」**。新增分支时
    必须同步这里——``tests/unit/test_d5_scorer.py::Test同源`` 盯着
    真实存档里出现的每个 (question, branch) 组合。
    """
    from trajectory_pipeline.perception import questions

    out: dict[str, set[str]] = {}
    for qid, q in questions.REGISTRY.items():
        allowed = set()
        if q.negative_branch:
            allowed.add(q.negative_branch)
        allowed.add(UNRESOLVED)  # 通用 fail-closed 出口
        if qid == questions.Q.PLAYER_OK:
            allowed.add("player_unverified")  # record_unresolved_reached
            allowed.add(questions.PAY_WALL)  # payload 选用的细分结论
        if qid == questions.Q.FIND_PLAY_CONTROL:
            allowed.add("component_unverified")  # I6 违约记账
        out[qid] = allowed
    # 代码层确定性事实核的分支：question 为空、不经 LLM，没有判断点可配对。
    # 按 (question, branch) 组合放行——分支名可能与注册表负分支重名
    # （``not_play_site`` 也由判断点① 判 False 产生），所以不能整条塞进
    # :data:`CODE_ONLY_BRANCHES`，那样会把配对检查整个跳过。
    for q, b in QUESTIONLESS_PAIRS:
        allowed = out.setdefault(q, set())
        allowed.add(b)
        allowed.add(UNRESOLVED)  # 与注册表条目同口径的出口
    return {k: frozenset(v) for k, v in out.items()}


QUESTION_TO_BRANCHES: Mapping[str, frozenset[str]] = _build_question_to_branches()

#: ``unresolved`` 是**通用 fail-closed 出口**，不是任何判断点的专属分支：
#: :meth:`RunLedger.record_from` 在 ``answer is None`` 时一律记它。
#:
#: 所以它**按判断点分两种语义**（避坑指南 §5.17 刻意不合并的那三支之一）：
#:
#:     环节①②③ 的 unresolved   连播放页都没到 ——「能不能到」未决（代码能力不足）
#:     环节④   的 unresolved     已到播放页     ——「能不能用」未决（页面证据不足）
#:
#: **只有后者要求 ``reached_play_page=True``**。第一版把 ``unresolved``
#: 整条塞进 :data:`REQUIRES_PLAY_PAGE`，于是实测 61 条
#: ``find_play_control → unresolved``（正常的 fail-closed 记账）
#: 全被判成「前提不成立」——**把正确结论扣到 3 分**，
#: 与文档里那句「评分器判反比不判更贵」正是同一件事。
REQUIRES_PLAY_PAGE: frozenset[str] = frozenset({"player_unverified"})

#: 仅当分支与判断点**同时**匹配时，才要求 ``reached_play_page=True``。
#: 键是二元组 ``(branch, question)``。
REQUIRES_PLAY_PAGE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("component_unverified", "player.ok"),
        ("unresolved", "player.ok"),
        ("player_unverified", "player.ok"),
        # 「有播放页但需付费」同样要求**确实到了播放页**——
        # 站点页上的「开通会员」是导航栏文案，不是付费墙。
        ("subscription_wall", "player.ok"),
    }
)

#: 三处检查的节点名。**用常量而不在调用点硬编码**——下面按节点名
#: 反查（``next(c for c in wrong if c.node == _NODE_NO_SKIP)``），
#: 两处各写一个字符串就会让那个反查抛 ``StopIteration``。
#: 而它抛出时的症状是「本条样本评不出来」，看起来像数据问题，不像代码问题。
_NODE_BRANCH = "分支与判断点配套"
_NODE_REACHED = "结论的前置事实"
_NODE_NO_SKIP = "无跳步结论"

#: 判定结果类型。``undetermined`` 与 ``scored`` 分开，不共用 ``score=None``。
VerdictKind = Literal["scored", "undetermined"]

#: 一处检查的结果。
CheckKind = Literal["ok", "wrong", "skipped"]


@dataclass(frozen=True, slots=True)
class TreeCheck:
    """决策树上一处检查。``why`` 必须写清依据——

    rubric 说「不可判定项越多，说明采集越该改，而不是轨迹越该扣分」，
    而读报告的人只有看到理由才知道该改采集器的哪一处。
    """

    node: str
    kind: CheckKind
    why: str

    def to_json(self) -> dict[str, Any]:
        return {"node": self.node, "kind": self.kind, "why": self.why}


@dataclass(frozen=True, slots=True)
class D5Verdict:
    """D-5 的判定结果。``score is None`` 时 ``kind`` 必为 ``undetermined``。"""

    score: int | None
    kind: VerdictKind
    checks: tuple[TreeCheck, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def wrong_nodes(self) -> tuple[TreeCheck, ...]:
        return tuple(c for c in self.checks if c.kind == "wrong")

    @property
    def is_veto(self) -> bool:
        """是否触发 G-1 样本级一票否决。**只有 1 分触发**——
        3 分的「分支走错」还没到结论错的地步。"""
        return self.score == 1

    def to_json(self) -> dict[str, Any]:
        return {
            "dimension": "D-5",
            "score": self.score,
            "kind": self.kind,
            "veto": self.is_veto,
            "checks": [c.to_json() for c in self.checks],
            "reasons": list(self.reasons),
        }


# ── 字段读取（兼容 dataclass 与 P2 字典）─────────────────────────


def _outcome_of(sample: Any) -> Any:
    """取样本的 ``outcome``。**``Sample`` 对象与 P2 字典都要吃**。

    只用 ``getattr`` 会让「传 P2 字典」的调用路径恒取到 ``None``，
    于是每条样本都判成「没有决策可判」——**不可判定看起来像一条正常结果**，
    报表上只是「分数都没出」，没人会去查为什么。
    （同 :func:`trajectory_pipeline.evaluation.scorers.code_scorer._label_of`
    的 dataclass / Mapping 双取。）
    """
    if isinstance(sample, Mapping):
        return sample.get("outcome")
    return getattr(sample, "outcome", None)


def _of(obj: Any, key: str, default: Any = "") -> Any:
    """读一个字段，``OutcomeRef``（dataclass）与 P2 字典都吃。"""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


# ── 三处检查 ──────────────────────────────────────────────────────


def _requires_play_page(branch: str | None, question: str) -> bool:
    """这个 ``(分支, 判断点)`` 组合是否以「已站在播放页上」为前提。

    按**二元组**判，不按分支一刀切。理由见两张表的注释：
    ``component_unverified`` 与 ``unresolved`` 各有两个判断点能产出，
    而其中只有 ``player.ok`` 那侧发生在播放页上。
    """
    if branch is None:
        return True  # 记成功必然要求走到播放页
    return (branch, question) in REQUIRES_PLAY_PAGE_PAIRS


def _check_branch_matches_question(question: str, branch: str | None) -> TreeCheck:
    """检查 1：分支与判断点是否配套。

    ``branch is None``（成功记录）只在判断点 ④ 成立——前三个判断点判 True
    的含义是「继续往下走」，不是「这个站成了」。走到最后记成功，
    结论必然出自 ④。
    """
    if branch is None:
        if question == "player.ok":
            return TreeCheck(
                _NODE_BRANCH, "ok", f"{question} 判 True → 记录成功（branch=null）"
            )
        return TreeCheck(
            _NODE_BRANCH,
            "wrong",
            f"记成功（branch=null）的结论出自 {question or '（未知判断点）'}——"
            f"只有判断点 player.ok 能得出「记录成功」",
        )
    if branch in CODE_ONLY_BRANCHES:
        # 代码判定分支**没有** question 可配对（不经 LLM）。不核它——
        # 拿一个本就不该存在的字段去比，得到的是「采集器没填」，
        # 不是「分支走错」。
        return TreeCheck(
            _NODE_BRANCH, "ok", f"{branch} 由代码判定（不经 LLM），无 question 可配对"
        )
    allowed = QUESTION_TO_BRANCHES.get(question)
    if allowed is None:
        return TreeCheck(
            _NODE_BRANCH,
            "skipped",
            f"判断点 {question or '（缺失）'} 不在决策树注册表里，无法核对分支",
        )
    if branch in allowed:
        return TreeCheck(_NODE_BRANCH, "ok", f"{question} → {branch}（决策树允许）")
    return TreeCheck(
        _NODE_BRANCH,
        "wrong",
        f"{question} 产出了 {branch}，而该判断点在决策树上只对应 "
        f"{sorted(allowed)}——分支走错",
    )


def _check_reached_precondition(
    branch: str | None, question: str, reached: bool
) -> TreeCheck:
    """检查 2：结论的前提是否成立。

    ``player_unverified`` 的整个意义是「有组件、确认不了能不能用」，
    而「有组件」只有在**站在播放页上**才谈得上——
    ``reached_play_page=False`` 时这条标签是凭空贴的。
    """
    if not _requires_play_page(branch, question):
        return TreeCheck(
            _NODE_REACHED,
            "ok",
            f"{branch or '记录成功'} 在到达播放页**之前**判定，"
            f"reached_play_page={str(reached).lower()} 是正确的"
            if not reached
            else f"{branch} 已到播放页，且该分支的前置事实成立",
        )
    if reached:
        return TreeCheck(
            _NODE_REACHED, "ok", f"{branch} 要求站在播放页上，而确实走到了"
        )
    return TreeCheck(
        _NODE_REACHED,
        "wrong",
        f"记了 {branch}，但没有到达播放页——该分支的定义以「已站在"
        f"播放页上」为前提（rubric D-5 边界规则）",
    )


def _check_no_skip(branch: str | None, reached: bool) -> TreeCheck:
    """检查 3：有没有跳步。

    rubric 锚点 1 的反例原文：「首页见『立即观看』字样直接记录，未进播放页」。
    形态就是 **判成功 + 没到播放页**——中间过程整段没有。
    """
    if branch is not None or reached:
        return TreeCheck(_NODE_NO_SKIP, "ok", "有中间过程，不是看 URL 直接下结论")
    return TreeCheck(
        _NODE_NO_SKIP,
        "wrong",
        "判成功（branch=null）但从未到达播放页——rubric D-5 反例："
        "「首页见『立即观看』字样直接记录，未进播放页」",
    )


# ── 打分 ──────────────────────────────────────────────────────────


def score_d5(sample: Sample | Any) -> D5Verdict:
    """D-5 打分。输入一条 P2 样本。

    锚点（rubric v1.1 D-5）::

        5 = 决策树每步分支全对
        4 = 决策正确，1 处应"未验证"却直接记录/排除
        3 = 1 处分支走错，未影响最终结论
        2 = 最终结论错误，过程部分正确
        1 = 结论错误且由错误判据链导致；或跳步结论（看 URL 直接记录）

    **跳步与「前提不成立」都压到 1 分**：两者都是「结论的判据链断了」，
    而不是「链条走错一步」。而 3 分那一档在 rubric 里的限定语是
    「**未影响最终结论**」——跳步的前提恰恰是最终结论没有过程支撑。
    """
    outcome = _outcome_of(sample)
    if outcome is None:
        # 搜索单元本来就没有 outcome（``split_archive`` 的口径）。
        # 那不是「决策错了」，是**这一条没有决策可判**——不可判定。
        return D5Verdict(
            None,
            "undetermined",
            (),
            ("本条样本没有 outcome（搜索阶段不记站点级结论）——无决策可判",),
        )

    branch = _of(outcome, "branch", None)
    question = str(_of(outcome, "question", "") or "")
    reached = bool(_of(outcome, "reached_play_page", False))

    # 代码判定分支的 question 本就是空的——**先放它过去**。
    # 顺序反了（先判 question 缺失）会让这几条全部落成「不可判定」，
    # 而它们是最该能判的几条：控制流自己判的，判据就在存档里。
    # ``not_play_site`` 的站点首页核（:data:`QUESTIONLESS_PAIRS`）同理：
    # 它是代码层确定性事实核，question 恒为空，却**不能**整条塞进
    # :data:`CODE_ONLY_BRANCHES`（否则同名判断点① 的配对检查会被跳过），
    # 所以这里按组合额外放行——它与「老批次缺 question」是两回事。
    if (
        branch not in CODE_ONLY_BRANCHES
        and (question, branch) not in QUESTIONLESS_PAIRS
        and not question
    ):
        # 老批次：P1 有 question 但 P2 侧的 OutcomeRef 是补字段之前切的。
        # 判 3 分会是**拿采集器的缺件当轨迹的错**——与 D-2 的口径一致。
        return D5Verdict(
            None,
            "undetermined",
            (),
            (
                "outcome 缺 question 字段（2026-10-10 之前切的样本）——"
                "决策树无法核对分支与判断点的配套关系，先重跑 build 再判",
            ),
        )

    checks = (
        _check_branch_matches_question(question, branch),
        _check_reached_precondition(branch, question, reached),
        _check_no_skip(branch, reached),
    )

    wrong = [c for c in checks if c.kind == "wrong"]
    reasons = list(dict.fromkeys(c.why for c in checks if c.kind == "skipped"))

    if not wrong:
        return D5Verdict(5, "scored", checks, tuple(reasons))

    # 跳步 = 锚点 1；其余单处错 = 锚点 3（「未影响最终结论」——
    # 判据链错在中间那一环，但结论本身仍与观察一致）。
    #
    # `any(...)` 而不是 `next(...)`：后者在没找到时抛 StopIteration，
    # 而那个异常从评分器冒到调用方时，症状是「本条样本评不出来」——
    # 看起来像数据问题，不像代码问题（实测踩过一次）。
    if any(c.node == _NODE_NO_SKIP for c in wrong):
        return D5Verdict(1, "scored", checks, tuple(reasons))
    if len(wrong) == 1:
        return D5Verdict(3, "scored", checks, tuple(reasons))
    # 两处及以上：分支走错**且**前提不成立 → 结论的判据链已断。
    return D5Verdict(1, "scored", checks, tuple(reasons))


__all__ = [
    "VerdictKind",
    "CheckKind",
    "TreeCheck",
    "D5Verdict",
    "QUESTION_TO_BRANCHES",
    "REQUIRES_PLAY_PAGE",
    "score_d5",
]
