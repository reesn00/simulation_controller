"""``unreachable_hard`` 的**产地契约**（M-44 落地，2026-10-27）。

为什么要有这条测试
----------------
同一个洞踩了两次，形状完全一样：

* **M-43** 把「建页与导航塞进同一个路由」拆成两次调用 ⇒ 归属判据变成结构性的，
  但「**建页失败 ⇒ 记成导航失败**」这个映射**还在** ⇒ 机制没修，
  同一个洞留在新代码里。
* **M-44** 把三类成因拆开、改用**机器码**判据（DNS 失败 / 连接被拒）
  ⇒ 判据落在一处了，但**没有一道门禁**守着「``unreachable_hard`` 只在
  机器码证明时记」⇒ 下一次有人改代码，同样的洞会再开一次。

⚠️ **而回归的症状在报表上看不出来**：超时记成 ``unreachable_hard``
在报表上只是「又多了一条负样本」——分布动一点，谁也不会去查它。
⇒ 门禁必须**在代码层**守，不能等报表。

这条测试守两件事
----------------
1. **产地唯一**——``"unreachable_hard"`` 这个字面量在 ``executor/`` 里
   **只允许**出现在四个文件里（两个产地 + 一个分支标签定义 + 一处代记录），
   **多一处当场红**。
2. **守卫在位**——两个记账产地都必须在 ``proven_unreachable`` 守卫之下
   （AST 核：祖先链里有一个 ``if``，其判定式引用了 ``proven_unreachable``）。
   ⇒ 有人把守卫删掉、或再加一处无条件记 ``unreachable_hard``，这里当场红。

⚠️ **为什么守在代码层而不是数据层**
------------------------------------
数据层要判「这份存档是新口径还是旧口径」，而 **P1 存档不带时间戳**
（实测：``steps`` / ``visits`` / ``outcomes`` / ``run_config`` / ``provenance``
全无时间字段）⇒ 只能按文件 mtime 判，那是**代理不是事实**
（见 :data:`~trajectory_pipeline.executor.branch_eras.M_TIME_CAVEAT`），
且**复核一次就重写 mtime** ⇒ 旧存档被误升代 ⇒ 门禁要么误报要么漏报。
⇒ **代码层才是护栏该在的地方**（CLAUDE.md：代码层做护栏、LLM 层做探索）。
数据侧靠「逐条打开存档读 evidence」那套固定动作兜（人读，机器不猜）。

两条链的**行为**对称性（超时 ⇒ ``unresolved`` / DNS ⇒ ``unreachable_hard``）
也在这里测——AST 只能核「形状」，核不了「记的分支对不对」。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from trajectory_pipeline.executor import branches
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.browser.page_driver import (
    DriverError,
    DriverSessionError,
    DriverUnreachableError,
)
from trajectory_pipeline.executor.steps import aggregator as aggregator_step
from trajectory_pipeline.executor.steps import search as search_step
from trajectory_pipeline.executor.steps.search import engine_redirect_engine
from trajectory_pipeline.executor.steps.visit import VisitResult, enter_site


EXECUTOR_ROOT = pathlib.Path(branches.__file__).resolve().parent

#: 允许出现 ``"unreachable_hard"`` **字面量**的文件 —— 逐个说清为什么。
#:
#: ⚠️ 这里管的是**字符串字面量**（AST 里的 ``ast.Constant``），
#: **注释与散文里的提及不在其中**——注释在 AST 里根本不是节点。
ALLOWED_LITERAL_FILES: dict[str, str] = {
    "branches.py": "分支标签定义（``BRANCH_LABELS``）——**定义**，不是产地",
    "branch_eras.py": "代的历史记录（``changed_branches``）——记「哪版规则改的」",
    "steps/visit.py": "**产地①**（visit 链的进站段 ``enter_site``）",
    "steps/aggregator.py": "**产地②**（中间页链 ``probe_and_observe``）",
}

#: 唯一允许的**记账产地**（分支实参是字面量 ``"unreachable_hard"`` 的 ``record`` 调用点）。
EXPECTED_PRODUCERS: frozenset[str] = frozenset({
    "steps/visit.py",
    "steps/aggregator.py",
})


# ═══════════════════════════════════════════════════════════════════════
# 扫描器
# ═══════════════════════════════════════════════════════════════════════


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(EXECUTOR_ROOT).as_posix()


def _parse(path: pathlib.Path) -> tuple[ast.AST, dict[ast.AST, ast.AST]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return tree, parents


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """全文里的**文档字符串**节点 id 集合。

    文档字符串是 ``ast.Expr`` 包着的字符串常量，且必须是某个 body 的
    **第一句**。它们是**散文**，不是代码——``"unreachable_hard"`` 出现在
    文档字符串里是**说明**，不是**产地**。
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if not isinstance(body, list) or not body:
                continue
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                out.add(id(first.value))
    return out


def _literal_files_with_branch() -> set[str]:
    """出现过 ``"unreachable_hard"`` **非文档字符串**字面量的文件集合。"""
    files: set[str] = set()
    for path in sorted(EXECUTOR_ROOT.rglob("*.py")):
        tree, _ = _parse(path)
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value == "unreachable_hard"
                and id(node) not in docs
            ):
                files.add(_rel(path))
                break
    return files


def _record_producers() -> list[tuple[str, ast.Call, dict[ast.AST, ast.AST]]]:
    """扫出所有「分支实参是字面量 ``"unreachable_hard"``」的 ``record`` 调用点。"""
    out: list[tuple[str, ast.Call, dict[ast.AST, ast.AST]]] = []
    for path in sorted(EXECUTOR_ROOT.rglob("*.py")):
        tree, parents = _parse(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name != "record":
                continue
            branch: ast.expr | None = None
            if len(node.args) >= 2:
                branch = node.args[1]
            for kw in node.keywords:
                if kw.arg == "branch":
                    branch = kw.value
            if (
                isinstance(branch, ast.Constant)
                and branch.value == "unreachable_hard"
            ):
                out.append((_rel(path), node, parents))
    return out


def _mentions_proven_unreachable(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "proven_unreachable":
            return True
        if isinstance(sub, ast.Name) and sub.id == "proven_unreachable":
            return True
    return False


def _guarded_by_proof(
    node: ast.Call, parents: dict[ast.AST, ast.AST]
) -> bool:
    """祖先链里有没有一个 ``if``，其判定式引用了 ``proven_unreachable``。"""
    cur: ast.AST = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.If) and _mentions_proven_unreachable(cur.test):
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# 判据一：产地唯一（AST）
# ═══════════════════════════════════════════════════════════════════════


def test_记账产地只有那两处():
    """``unreachable_hard`` **只准**在两个记账点被记。

    多一处 ⇒ 这条测试红 ⇒ 那处要么登记进 :data:`EXPECTED_PRODUCERS`
    并说明为什么它也受 ``proven_unreachable`` 守卫，要么改掉。
    """
    producers = {rel for rel, _call, _p in _record_producers()}
    assert producers == set(EXPECTED_PRODUCERS), (
        "unreachable_hard 的记账产地变了："
        f"实际 {sorted(producers)}，期望 {sorted(EXPECTED_PRODUCERS)}"
    )


def test_每个产地都被proven_unreachable守着():
    """⚠️ **核心判据**：两处记账都必须在 ``proven_unreachable`` 守卫之下。

    守卫被删 / 改成别的条件 ⇒ 这里红。
    """
    unguarded = [
        rel for rel, call, parents in _record_producers()
        if not _guarded_by_proof(call, parents)
    ]
    assert not unguarded, (
        "这些记账点没有 proven_unreachable 守卫——"
        f"「这次没打开」会被写成「站点不可达」（M-44 的老洞）：{unguarded}"
    )


def test_字面量不出现在白名单之外的文件():
    """``"unreachable_hard"`` 这个**字面量**只准出现在四个文件里。

    ⚠️ 管的是**代码**里的字面量（注释与文档字符串不算）——
    散文里提到这个分支名是**说明**，不是**产地**。
    """
    files = _literal_files_with_branch()
    extra = files - set(ALLOWED_LITERAL_FILES)
    assert not extra, (
        f"这些文件里出现了 \"unreachable_hard\" 字面量，但不在白名单里：{sorted(extra)}"
        "——新出现的记账路径必须登记进 ALLOWED_LITERAL_FILES 并说明理由"
    )


# ═══════════════════════════════════════════════════════════════════════
# 判据二：两条链的行为对称（M-44 的原话「两条记分链同口径」）
# ═══════════════════════════════════════════════════════════════════════


class _导航失败Driver:
    """导航按指定形态失败的最小 driver。

    只实现 ``goto``——两条链在**导航失败**时就收口返回了，
    后面的 ``observe`` / ``links`` 都不会被调到。
    """

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.visited: list[str] = []

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        self.visited.append(url)
        raise self._exc


_CAND = search_step.Candidate(url="https://x.test/movie", text="电影", rank=1)

#: 四种「**这次没打开**」的形态 + 两种「**真·站点不可达**」的形态。
#:
#: ⚠️ 前四种的原文取自 ``research/probes/*_nav_failure_codes.json``
#: （现场抓的），不是照文档编的。
_这次没打开: dict[str, Exception] = {
    "超时": DriverError("page.goto: Timeout 30000ms exceeded"),
    "打断": DriverError(
        'page.goto: Navigation to "http://x.test/" is interrupted by '
        'another navigation to "http://y.test/"'
    ),
    "建页失败": DriverError(
        "camofox tool camofox_create_tab 返回错误: camofox error: 500"
    ),
    "会话失效": DriverSessionError(
        "camofox 会话失效（换页重试也没成功）: 404"
    ),
}

_真不可达: dict[str, Exception] = {
    "DNS解析不出来": DriverUnreachableError(
        "page.goto: NS_ERROR_UNKNOWN_HOST", code="NS_ERROR_UNKNOWN_HOST"
    ),
    "连接被拒": DriverUnreachableError(
        "page.goto: NS_ERROR_CONNECTION_REFUSED",
        code="NS_ERROR_CONNECTION_REFUSED",
    ),
}


async def _visit_chain(exc: Exception) -> str:
    """走 visit 链的进站段，返回它记的分支。"""
    ledger = RunLedger("T001")
    result = VisitResult(candidate=_CAND)
    await enter_site(_导航失败Driver(exc), ledger, result)
    assert ledger.outcomes, "这条链至少要记一条 outcome"
    return str(ledger.outcomes[0].branch)


async def _aggregator_chain(exc: Exception) -> str:
    """走中间页链，返回它记的分支。"""
    ledger = RunLedger("T001")
    result = aggregator_step.IntermediateResult(
        role=aggregator_step.ROLE_AGGREGATOR, candidate=_CAND
    )
    await aggregator_step.probe_and_observe(_导航失败Driver(exc), ledger, result)
    assert ledger.outcomes, "这条链至少要记一条 outcome"
    return str(ledger.outcomes[0].branch)


@pytest.mark.parametrize("形态", sorted(_这次没打开))
async def test_这次没打开两条链都记unresolved(形态):
    """⚠️ **「这次没打开」不是「站点不可达」**（M-44 裁定，owner 2026-10-27）。

    超时 / 打断 / 建页失败 / 会话失效**证明不了**站点不可达
    ⇒ 两条链都记 ``unresolved``（**不进负样本池**）。
    """
    for chain in (_visit_chain, _aggregator_chain):
        assert await chain(_这次没打开[形态]) == "unresolved", (
            f"{形态} 在 {'visit' if chain is _visit_chain else 'aggregator'} "
            "链上被记成了站点不可达"
        )


@pytest.mark.parametrize("形态", sorted(_真不可达))
async def test_真站点不可达两条链都记unreachable_hard(形态):
    """**机器码证明**站点不可达 ⇒ 两条链都记 ``unreachable_hard``。

    ⚠️ 真负样本要留住，**不许**被顺带降级成 ``unresolved``——
    降了就等于把「这里真的看不了」这个训练信号扔掉。
    """
    for chain in (_visit_chain, _aggregator_chain):
        assert await chain(_真不可达[形态]) == "unreachable_hard", (
            f"{形态} 在 {'visit' if chain is _visit_chain else 'aggregator'} "
            "链上没被记成站点不可达"
        )


async def test_这次没打开不进池():
    """``unresolved`` **不进负样本池**——池里那行的断言是「这里真的看不了」。

    而「这次没打开」不是这个事实 ⇒ 混进池就是**污染训练信号**。
    """
    ledger = RunLedger("T001")
    result = VisitResult(candidate=_CAND)
    await enter_site(_导航失败Driver(_这次没打开["超时"]), ledger, result)
    out = ledger.outcomes[0]
    assert out.is_negative_sample is False


# ═══════════════════════════════════════════════════════════════════════
# 判据三：跳转链上的**归因对象**（Q-24 甲 · owner 裁定 2026-10-28）
# ═══════════════════════════════════════════════════════════════════════
#
# 上面两条问的是「打不开这件事是什么性质」；这一条问「**这个结论说的是谁**」。
#
# 百度结果给的 ``http://www.baidu.com/link?url=<密文>`` **不是站点**，是搜索
# 引擎的中转端点。导航到它失败时，机器码证明的是「**这条中转链**打不开」，
# 那个影视站**一次都没被识别过**（``landed_url`` 为空、call log 只有一跳）
# ⇒ 「这个站真的看不了」这句话**没有可指的对象** ⇒ 不配记
# ``unreachable_hard``（那是**站点性质**、进负样本池）。
#
# ⚠️ **机器码原文必须照录进 reason**（不丢证据）：判据从「站点不可达」降成
# 「归因对象不明」，但「确实拒连了」这个事实要留在存档里。
#
# 实测代价（2026-10-28 · M-47）：那批 14 行新增池行里 **6 行（43%）** 是
# 百度跳转链，全池 **41/407（10.1%）**。

#: 百度结果给的**中转链**（形态照抄实测存档：密文是 base64url 串）。
_百度中转链 = (
    "http://www.baidu.com/link?url="
    "TRj-DTQOgh8_n5pkc_N5jhk5RZsWvgq5H1VuqbeBKo-wgUB6BQ3_oLM2kThDlK7b"
)

#: 直站（不是中转链）——同样拒连，**该记的还得记**。
_直站 = "https://movie.test/v/31445"


async def _chain_branch(url: str, exc: Exception) -> tuple[str, str]:
    """走 visit 链的进站段，返回 ``(分支, 证据)``。"""
    ledger = RunLedger("T001")
    result = VisitResult(
        candidate=search_step.Candidate(url=url, text="电影", rank=1)
    )
    await enter_site(_导航失败Driver(exc), ledger, result)
    assert ledger.outcomes, "这条链至少要记一条 outcome"
    out = ledger.outcomes[0]
    return str(out.branch), out.evidence


@pytest.mark.parametrize("形态", sorted(_真不可达))
async def test_跳转链上的拒连不记站点不可达(形态):
    """⚠️ **归因对象不明 ⇒ 不配记 ``unreachable_hard``**（Q-24 甲）。

    机器码再硬也只证明「这条链打不开」——那个影视站**一次都没被试过**。
    """
    branch, evidence = await _chain_branch(_百度中转链, _真不可达[形态])
    assert branch == "unresolved", (
        f"{形态}：中转链上的失败被记成了站点不可达——"
        "那是把「这条链打不开」写成「那个站看不了」"
    )
    # 机器码原文照录（证据不丢）
    assert "NS_ERROR" in evidence
    assert "结果中转链" in evidence


@pytest.mark.parametrize("形态", sorted(_真不可达))
async def test_直站拒连仍记站点不可达(形态):
    """⚠️ **不误伤**：直站拒连**照旧**记 ``unreachable_hard``。

    护栏只拦「归因对象不明」那一种，不该把真负样本顺带降级——
    降了就等于把「这里真的看不了」这个训练信号扔掉。
    """
    branch, _evidence = await _chain_branch(_直站, _真不可达[形态])
    assert branch == "unreachable_hard"


async def test_跳转链上的失败不进池():
    """降级成 ``unresolved`` 之后**不进负样本池**——池里那行的断言是
    「这里真的看不了」，而中转链失败证明不了这件事。"""
    ledger = RunLedger("T001")
    result = VisitResult(
        candidate=search_step.Candidate(url=_百度中转链, text="电影", rank=1)
    )
    await enter_site(
        _导航失败Driver(_真不可达["连接被拒"]), ledger, result
    )
    assert ledger.outcomes[0].is_negative_sample is False


class Test中转链识别:
    """:func:`~trajectory_pipeline.executor.steps.search.engine_redirect_engine`
    的口径——**纯结构判断**（引擎 host + 路由 path + ``url=`` 参数键）。

    ⚠️ **host 必须精确匹配**：``evil-www.baidu.com`` 也是 ``www.baidu.com``
    的**后缀**，而那个 host 是**别的站**——把它当中转端点，就又拿别人的
    失败替那个站背锅了。
    """

    def test_认得百度中转链(self):
        assert engine_redirect_engine(_百度中转链) == "baidu"

    def test_长得像的域名不算(self):
        assert engine_redirect_engine(
            "https://evil-www.baidu.com/link?url=abc"
        ) == ""

    def test_不是那个路由不算(self):
        assert engine_redirect_engine("http://www.baidu.com/other?url=abc") == ""

    def test_缺url参数不算(self):
        assert engine_redirect_engine("http://www.baidu.com/link") == ""

    def test_普通站点不算(self):
        assert engine_redirect_engine("https://movie.test/link?url=abc") == ""
