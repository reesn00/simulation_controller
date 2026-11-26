"""``login_required`` 的**前置事实核**（H-26 缺口② / 13 号 §P1-6 尾注）。

判据唯一所有者在感知层：:func:`rule_perceptor.login_gate_evidence`
（条件式语序 + 排除已登录提示）。三处消费它：

1. ``executor.gatekeeper.is_login_wall``——**拦不拦这页**；
2. ``executor.branches.precondition_failed`` 的 ``login_required`` 支
   ——**贴不贴这条负标签**（scripted 与 agent 两条路径共用）；
3. agent 路径的 ``_guarded_branch`` 经 ② 生效。

⚠️ **为什么不能用 ``login_gate_phrase``**（它一直在感知层）：那个函数只查
``ACCESS_HINTS.login`` 短语的空白容忍版，「请登录」单独出现就命中。2026-11-25
实测存量 2080 个观察正文里含登录短语的 22 个**全部**是导航 / 弹幕孤立词
⇒ 拿它当核等于没核。
"""

from __future__ import annotations

from trajectory_pipeline.executor.branches import (
    RunLedger,
    precondition_failed,
)
from trajectory_pipeline.perception.base import Decision, Observation
from trajectory_pipeline.perception.rule_perceptor import login_gate_evidence


def _obs(body: str) -> Observation:
    return Observation(
        url="https://x.test/play", page_title="播放页", body_text=body,
    )


def _decision(*, quotes: tuple[str, ...] = ()) -> Decision:
    """④ PLAYER_OK 判负、payload 指 login ⇒ ``decision_branch`` 落
    ``login_required``（与 agent 路径上门禁折算到同一支）。"""
    return Decision(
        question="player.ok",
        answer=False,
        confidence=0.9,
        evidence="页面要登录",
        source="llm",
        payload={"access_control_type": "login", "page_quotes": list(quotes)},
    )


class Test判据本身:
    def test_条件式语序成立(self):
        assert login_gate_evidence("请登录后再观看本片") != ""
        assert login_gate_evidence("登录后才能继续播放") != ""

    def test_导航栏孤立词不成立(self):
        assert login_gate_evidence("首页 导航 未登录 会员 点播厅 请登录") == ""

    def test_已登录用户提示不成立(self):
        assert login_gate_evidence("若您已是会员，请登录观看>") == ""

    def test_空正文不成立(self):
        assert login_gate_evidence("") == ""

    def test_描述能指认判据(self):
        """返回的是**描述**不是布尔值——evidence 必须说得清凭什么。"""
        assert "登录门语序" in login_gate_evidence("请登录后再观看本片")


class Test分支前置事实核:
    def test_真登录门_不降级(self):
        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/play",
            _decision(),
            obs=_obs("请登录后再观看本片，未登录用户只能试看六分钟"),
        )
        assert out.branch == "login_required"

    def test_导航栏登录词_降级unresolved(self):
        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/play",
            _decision(),
            obs=_obs("首页 导航 未登录 会员 点播厅 请登录"),
        )
        assert out.branch == "unresolved"

    def test_模型引文也算证据(self):
        """与 ``subscription_wall`` 同款：输入源是**页面原文**（quote），
        不是模型自述。正文被裁短时引文仍能作证。"""
        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/play",
            _decision(quotes=("请登录后继续观看",)),
            obs=_obs("加载中…"),
        )
        assert out.branch == "login_required"

    def test_核成立的判定不等于拦截判定的重复(self):
        """两处同一函数、**同一输入**必须同结论——这条钉住「没有第二份实现」。"""
        body = "登录后方可观看完整版"
        assert precondition_failed("login_required", _obs(body), decision=_decision()) == ""
        assert login_gate_evidence(body) != ""
