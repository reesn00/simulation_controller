"""``broken`` 的**前置事实核**（H-49 邻居项 · 2026-11-26）。

判据唯一所有者：:func:`branches.error_page_evidence`（结构事实——
没有可交互元素、没有 video 标签）。两处消费它：

1. :func:`branches.terminal_shape_branch`——agent 收口路径（Q-25 甲）；
2. :func:`branches.precondition_failed` 的 ``broken`` 支——**贴不贴标签**
   （scripted 与 agent 两条路径共用）。

⚠️ **补之前的样子**：判据本身早就在、也实测过（1224 个观察里正文命中
``not_found`` 的 49 条全是真错误页、假阳 0），但它**只挂在收口路径上**。
agent 的门禁折算（``agent_loop._FALLCLOSED_BRANCH``）与 ② 的
``not_found`` 折成分支时**走不到它** ⇒ ``broken`` 在贴标签这一侧仍是裸折算。
"""

from __future__ import annotations

from trajectory_pipeline.executor.branches import (
    RunLedger,
    error_page_evidence,
    precondition_failed,
)
from trajectory_pipeline.perception.base import Decision, Observation


def _obs(body: str, **kw) -> Observation:
    base = {"url": "https://x.test/v/1", "page_title": "404", "body_text": body}
    base.update(kw)
    return Observation(**base)  # type: ignore[arg-type]


def _decision_404() -> Decision:
    """② 判不可达、payload 指 404 ⇒ ``decision_branch`` 落 ``broken``。"""
    return Decision(
        question="is_reachable",
        answer=False,
        confidence=0.9,
        evidence="页面 404",
        source="llm",
        payload={"access_control_type": "not_found"},
    )


class Test判据本身:
    def test_错误页结构成立(self):
        assert error_page_evidence(_obs("404 Not Found nginx")) != ""

    def test_有元素就不是错误页(self):
        assert error_page_evidence(_obs("404 Not Found", elements_total=12)) == ""

    def test_有视频就不是错误页(self):
        assert error_page_evidence(_obs("404", video_tag_count=1)) == ""

    def test_降级观察不成立(self):
        assert error_page_evidence(_obs("404", degraded=("press",))) == ""

    def test_空观察不成立(self):
        assert error_page_evidence(None) == ""


class Test分支前置事实核:
    def test_真错误页_不降级(self):
        out = RunLedger("T001").record_from(
            "https://x.test/v/1", _decision_404(), obs=_obs("404 Not Found nginx"),
        )
        assert out.branch == "broken"

    def test_正文提到404但页面正常_降级unresolved(self):
        """这是**补核要挡的那一类**：详情页正文里提到 404（角标 / 页脚
        错误码说明），元素一堆——它不是错误页。"""
        out = RunLedger("T001").record_from(
            "https://x.test/v/1",
            _decision_404(),
            obs=_obs("该片已下架（错误码 404），推荐以下影片", elements_total=48),
        )
        assert out.branch == "unresolved"
        assert "[记账降级]" in out.evidence

    def test_两处同一判据同结论(self):
        """判据只有一份：贴标签侧与收口侧**同一输入同结论**。"""
        obs = _obs("404 Not Found nginx")
        assert precondition_failed("broken", obs, decision=_decision_404()) == ""
        assert error_page_evidence(obs) != ""

    def test_没有正文时不判_不是不成立(self):
        """「连正文都没读到」⇒ 核无从成立（放行），不记成「页面干净」。
        与 ``obs is None`` 放行同一条口径。"""
        assert precondition_failed(
            "broken", _obs(""), decision=_decision_404()
        ) == ""
