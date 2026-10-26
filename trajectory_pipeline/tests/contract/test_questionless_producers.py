"""**无题记账点**与 D-5 配对表的对账（2026-10-26 实测踩到）。

起因
----
agent 路径（方案 15「代码层做护栏」）上，拦截是**代码**判的
（:mod:`trajectory_pipeline.executor.gatekeeper`），**没有判断点**——
于是这类 outcome 的 ``question`` 恒为空。而
:data:`trajectory_pipeline.evaluation.scorers.d5_scorer.QUESTIONLESS_PAIRS`
当时只认 ``("", "not_play_site")`` 一种无题组合 ⇒ T054 那 3 条
``("", "subscription_wall")``（``evidence="paywall 二次拦截"``）
被 D-5 判成「分支走错」。

**那不是数据坏了，是配对表少了一类产地。**

为什么要这条测试
----------------
配对表里的「无题」那一半是**手写**的（只有 gatekeeper 那部分从执行层
推导），所以它会漂：执行层新增一个无题记账点，配对表不会自己知道。
而漂了的症状是**判反**——把正确的行判成「分支走错」。D-5 的判反比
不判更贵（模块头原话），所以这里**反过来盯执行层**。

做法：AST 扫 ``executor/`` 里所有 ``record(...)`` 调用，按
:meth:`trajectory_pipeline.executor.branches.RunLedger.record` 的签名
判「有没有题」——

- 第 2 个位置参数是分支；
- **没有** ``decision=`` 关键字 ⇒ ``question`` 恒为空串。

然后按分支**能不能静态确定**分两路：

- **静态**（字面量，或模块级常量）：那个分支必须在 D-5 的无题放行集合里；
- **动态**（``outcome.branch or UNRESOLVED`` 这类表达式）：静态查不出
  分支名，**不猜**——改查「这个调用点登记过吗」。登记的那几处，
  分支来源唯一（agent 收口映射），由 :mod:`trajectory_pipeline.executor.agent_loop`
  的 :data:`~trajectory_pipeline.executor.agent_loop.GATEKEEPER_DECIDED_BRANCHES`
  兜住。

新增无题记账点而忘了改 D-5 配对表 ⇒ 这里当场红。
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

from trajectory_pipeline.evaluation.scorers import d5_scorer
from trajectory_pipeline.executor import branches


EXECUTOR_ROOT = pathlib.Path(branches.__file__).resolve().parent

#: 允许「question 为空」的分支全集 = 代码专属分支 ∪ 配对表里问号为空的那些。
#:
#: 前者（:data:`d5_scorer.CODE_ONLY_BRANCHES`）是「只由代码产生、名字
#: 不与任何判断点冲突」的分支——它们天生没有 question 可配对；
#: 后者是「同名于某个判断点、但那条路径上是由代码判的」——
#: 按**组合**放行，不整条塞进 CODE_ONLY（否则有题那一侧的配对检查
#: 会被整个跳过）。
ALLOWED_QUESTIONLESS: frozenset[str] = frozenset(
    d5_scorer.CODE_ONLY_BRANCHES | d5_scorer.QUESTION_TO_BRANCHES[""]
)

#: **动态分支**的无题记账点登记：函数名 → 分支来源说明。
#:
#: 这几处记账的分支名在**运行期**才知道（``outcome.branch``），
#: 静态扫不出来。登记它们不是为了「放过」——是为了让「新增一处动态
#: 无题记账」必须**显式登记**一次，而不是悄悄多出一类产地。
#:
#: 分支来源唯一：agent 收口映射
#: (:data:`trajectory_pipeline.executor.agent_loop.GATEKEEPER_DECIDED_BRANCHES`)。
DYNAMIC_QUESTIONLESS_PRODUCERS: dict[str, str] = {
    "agent_visit": (
        "agent 站内导航被 gatekeeper 收口 ⇒ 分支来自收口映射"
    ),
    "_visit": (
        "agent 聚合站/文章找入口被收口 ⇒ 分支来自收口映射"
        "（``steps/agent_intermediate.py`` 的共用实现）"
    ),
}


# ── 扫描器 ────────────────────────────────────────────────────────


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(EXECUTOR_ROOT.parent).with_suffix("")
    return "trajectory_pipeline." + ".".join(rel.parts)


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """往上找最近的那个 ``FunctionDef`` / ``AsyncFunctionDef``。"""
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
    return "<模块层>"


def _scan() -> tuple[list[tuple[str, str, str, int]], list[tuple[str, str, int]]]:
    """扫 ``executor/`` 的记账调用点。

    返回 ``(静态, 动态)``：静态项是 ``(文件, 函数, 分支, 行号)``，
    动态项是 ``(文件, 函数, 行号)``。
    """
    static: list[tuple[str, str, str, int]] = []
    dynamic: list[tuple[str, str, int]] = []

    for path in sorted(EXECUTOR_ROOT.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):        # pragma: no cover
            continue
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        # 模块级常量（``UNRESOLVED = "unresolved"`` 那种）拿来解析
        # ``Name`` 形态的分支。解析不出来就**不猜**——归入动态。
        try:
            module = importlib.import_module(_module_name(path))
            namespace = vars(module)
        except Exception:                      # pragma: no cover
            namespace = {}

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "record"):
                continue
            # 收件人限死为账本：``ledger.record(...)`` 与
            # ``RunLedger.record(...)`` 里的 ``self.record(...)``。
            # 收件人是个属性（``self.dead_loop.record(...)``）⇒ **不是**
            # 账本记账（那是死循环探测器的同名方法，签名完全不同）。
            if not (isinstance(func.value, ast.Name)
                    and func.value.id in {"ledger", "self"}):
                continue
            if len(node.args) < 2:
                continue
            # 有 decision ⇒ question 有值 ⇒ 这不是「无题记账点」
            if any(kw.arg == "decision" for kw in node.keywords):
                continue
            branch_node = node.args[1]
            branch: str | None = None
            if isinstance(branch_node, ast.Constant) and isinstance(
                branch_node.value, str
            ):
                branch = branch_node.value
            elif isinstance(branch_node, ast.Name):
                value = namespace.get(branch_node.id)
                if isinstance(value, str):
                    branch = value
            where = _enclosing_function(node, parents)
            if branch is not None:
                static.append((path.name, where, branch, node.lineno))
            else:
                dynamic.append((path.name, where, node.lineno))
    return static, dynamic


_STATIC, _DYNAMIC = _scan()


# ── 1. 静态无题记账点：分支必须在放行集合里 ────────────────────────


def test_静态无题记账点的分支都在放行集合里():
    """**每一个**不带 ``decision=`` 的字面量分支记账，都得在 D-5 的
    无题放行集合里。

    不在 ⇒ 该分支会以「question 为空」落成「老批次缺字段」的不可判定，
    而真实成因是**配对表少了一类产地**——两种病都叫「不判」，但只有
    一种能修。
    """
    bad = [
        f"{f}:{ln} {where}() → {branch!r}"
        for f, where, branch, ln in _STATIC
        if branch not in ALLOWED_QUESTIONLESS
    ]
    assert not bad, (
        "这些无题记账点的分支不在 D-5 无题放行集合里：\n  "
        + "\n  ".join(bad)
        + f"\n放行集合 = {sorted(ALLOWED_QUESTIONLESS)}"
    )


def test_扫到东西了():
    """**这条不是形式主义**——扫描器扫空了，下面所有断言都是空的绿。

    扫空的成因通常是「记账入口改了签名」（``record`` 不再是第 2 个
    位置参数是分支）。那种情况该红：扫描器跟不上了，钉住它的断言
    全部失效。
    """
    assert _STATIC, (
        "一个静态无题记账点都没扫到——扫描器跟记账入口的签名脱节了？"
        "（``record(url, branch, evidence, *, decision=...)``）"
    )
    assert _DYNAMIC, (
        "一个动态无题记账点都没扫到——agent 收口那两处换了写法？"
    )


# ── 2. 动态无题记账点：必须登记过 ────────────────────────────────


def test_动态无题记账点都登记过():
    """分支名静态查不出来的那几处，**必须**在
    :data:`DYNAMIC_QUESTIONLESS_PRODUCERS` 里登记。

    登记 = 承认「这里会多出一类无题产地」，并把它交给
    :data:`trajectory_pipeline.executor.agent_loop.GATEKEEPER_DECIDED_BRANCHES`
    这一个来源。**不登记**就是悄悄多产地 ⇒ 配对表迟早对不上。
    """
    unknown = sorted({
        f"{f}:{ln} {where}()"
        for f, where, ln in _DYNAMIC
        if where not in DYNAMIC_QUESTIONLESS_PRODUCERS
    })
    assert not unknown, (
        "这些无题记账点的分支名静态查不出来，却没登记：\n  "
        + "\n  ".join(unknown)
        + "\n要么登记进 DYNAMIC_QUESTIONLESS_PRODUCERS，"
        "要么改成静态可查（分支来自常量或字面量）"
    )


def test_登记的动态产地确实存在():
    """登记项不许指向已经不存在的函数——否则登记会变成免死金牌。"""
    seen = {where for _f, where, _ln in _DYNAMIC}
    missing = sorted(set(DYNAMIC_QUESTIONLESS_PRODUCERS) - seen)
    assert not missing, (
        f"登记了但扫描不到：{missing}——函数改名/挪位置了？"
    )


# ── 3. 配对表与执行层同源 ─────────────────────────────────────────


def test_无题放行集合盖住gatekeeper判定的分支():
    """gatekeeper 收口映射里的每一支，都得能以「无题」出现在盘上。

    真源是执行层的收口映射（延迟导入推导），这里核的是**配对表真的
    接住了**——推导对了、表没更新，照样会漏。
    """
    from trajectory_pipeline.executor.agent_loop import (
        GATEKEEPER_DECIDED_BRANCHES,
    )

    missing = sorted(
        b for b in GATEKEEPER_DECIDED_BRANCHES
        if b not in ALLOWED_QUESTIONLESS
    )
    assert not missing, (
        f"gatekeeper 能判出的这些分支没进无题放行集合：{missing}"
    )


def test_gatekeeper收口集合就是那三支():
    """**把取值钉死**——收口映射改了分支，这里当场红，要人重新想一遍。

    改它不是「顺手」的事：多一支就多一类无题产地，池子里会多出一
    种「没有判断题支撑的负标签」。
    """
    from trajectory_pipeline.executor.agent_loop import (
        GATEKEEPER_DECIDED_BRANCHES,
    )

    assert GATEKEEPER_DECIDED_BRANCHES == frozenset({
        "login_required", "subscription_wall", "blocked",
    }), (
        "gatekeeper 收口分支集合变了——多一支就多一类无题产地。"
        "确认过之后再改这里，并同步 d5_scorer 的配对表与本测试。"
    )


# ── 4. D-5 侧的落点 ─────────────────────────────────────────────


@pytest.mark.parametrize("branch", [
    "subscription_wall", "login_required", "blocked",
])
def test_无题分支不被当成老批次缺字段(branch):
    """无题但**合法**的分支，不该落成「老批次缺 question 字段」的不可判定。

    那个诊断是**错的**：老批次是「当时没记这个字段」，而这几支是
    「本来就没有判断点」。混起来的后果是：真缺陷（配对表缺产地）被
    读成「历史数据问题」，没人去修。
    """
    from trajectory_pipeline.evaluation.scorers.d5_scorer import score_d5

    sample = {
        "outcome": {
            "branch": branch, "question": "",
            "reached_play_page": False,
        },
        "six": {"observations": []},
    }
    verdict = score_d5(sample)
    reasons = " ".join(verdict.reasons)
    assert "老批次" not in reasons, (
        f"{branch} 被当成老批次缺字段了——那是错的诊断：{verdict.reasons}"
    )
    assert not verdict.wrong_nodes, (
        f"{branch} 被判错了：{verdict.wrong_nodes}"
    )
