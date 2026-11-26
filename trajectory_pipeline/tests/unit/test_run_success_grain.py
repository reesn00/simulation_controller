"""``RunRecord.succeeded`` 的**粒度**——任务级取末条结论（13 号 §P0-3）。

两个口径必须分开，谁也不冒充谁：

- **任务级**（:attr:`RunRecord.succeeded`）= **末条结论**成功，与 P2 的
  任务标签 ``outcomes[-1]`` 同源（裁定 ②）。
- **站点级**（:attr:`RunRecord.site_success_count` / ``ledger.summary()``）
  = ``branch is None`` 的结论条数。

⚠️ **旧口径 ``any(v.success for v in visits)`` 的破坏性在这里钉住**：
一条「先开到播放页、后来又在站内换页失败」的轨迹会被算成功，而 P2 标签
是失败 ⇒ **报表结论与训练标签反向**，且偏差一路传进 persona 切片
「哪类 persona 表现好」的结论里。
"""

from __future__ import annotations

from trajectory_pipeline.executor.branches import RunLedger, SiteOutcome
from trajectory_pipeline.executor.orchestrator import RunConfig, RunRecord
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.executor.steps.visit import VisitResult


def _visit(idx: int, *, success: bool) -> VisitResult:
    """一条 ``visits[]``——**旧口径读的就是这个 ``success`` 位**。"""
    return VisitResult(
        candidate=Candidate(url=f"https://v{idx}.test/", text="t", rank=idx),
        success=success,
    )


def _record(
    outcomes: list[SiteOutcome],
    *,
    visits: list[VisitResult] | None = None,
) -> RunRecord:
    """``visits`` 缺省按结论条数补齐，且**成功位与结论一致**。

    ⚠️ **必须填 ``visits``**：旧口径 ``any(v.success for v in visits)``
    读的是它，只造 ``outcomes`` 会让旧口径在「空 visits」上恒得 False，
    于是「中途成功最后失败」那条用例在旧口径下**也过**——门禁就成了假的
    （2026-11-25 实测：变异后它没红，正是这个原因）。
    """
    if visits is None:
        visits = [
            _visit(i, success=(o.branch is None)) for i, o in enumerate(outcomes)
        ]
    ledger = RunLedger(task_id="T1", outcomes=outcomes)
    return RunRecord(
        task_id="T1",
        title="t",
        query="q",
        search_url="https://s.test/",
        ledger=ledger,
        visits=visits,
    )


def _ok(url: str) -> SiteOutcome:
    """一条**成功**结论：``branch is None``。"""
    return SiteOutcome(url=url, branch=None, evidence="到了播放页", reached=True)


def _fail(url: str, branch: str = "no_play_control") -> SiteOutcome:
    return SiteOutcome(url=url, branch=branch, evidence="没找到播放控件")


class Test任务级取末条结论:
    def test_中途成功最后失败_任务级是失败(self):
        """**这条就是 P0-3 的原症状**：旧口径会报 True。"""
        rec = _record([
            _ok("https://a.test/"),
            _fail("https://b.test/"),
        ])
        assert rec.succeeded is False

    def test_中途失败最后成功_任务级是成功(self):
        rec = _record([
            _fail("https://a.test/"),
            _ok("https://b.test/"),
        ])
        assert rec.succeeded is True

    def test_一条结论都没有不算成功(self):
        rec = _record([])
        assert rec.succeeded is False
        assert rec.lead_outcome is None

    def test_没有账本不算成功(self):
        rec = RunRecord(task_id="T1", title="t", query="q", search_url="https://s.test/")
        assert rec.succeeded is False
        assert rec.lead_outcome is None


class Test站点级另开一列:
    def test_站点级成功数是全量计入(self):
        """同一条轨迹：任务级失败，但站点级确实是 2 个成功。"""
        rec = _record([
            _ok("https://a.test/"),
            _ok("https://b.test/"),
            _fail("https://c.test/"),
        ])
        assert rec.succeeded is False          # 末条失败
        assert rec.site_success_count == 2     # 站点级照实记

    def test_站点级不读visits的success位(self):
        """站点级口径是「结论的 branch」，不是 ``visits[i].success``——
        后者是**遍历过程**的成功位（页开出来了、动作跑完了），与结论无关。"""
        rec = _record([_fail("https://a.test/")])
        assert rec.site_success_count == 0

    def test_全成功时两个口径一致(self):
        rec = _record([_ok("https://a.test/"), _ok("https://b.test/")])
        assert rec.succeeded is True
        assert rec.site_success_count == 2


class Test任务级与P2标签同源:
    def test_lead_outcome就是末条(self):
        last = _fail("https://z.test/", branch="login_required")
        rec = _record([_ok("https://a.test/"), last])
        assert rec.lead_outcome is last
