"""``agent_loop._think_payload`` 与 ``WriteResult.to_json()`` 的形状契约（A3-3）。

agent 模式把每步 ``think`` 装进 P1 ``steps[i].rationale`` 槽位，让 agent 批
在 P2 长出 rationale → 闸门有得查 → D-9 有分可产（§6.4 拍板「D-9 纳入
A3 验收」的落链点）。executor **不 import rationale**（依赖方向纪律，
与 ``rationale_writer`` 注入同款），于是「payload 形状 = WriteResult 形状」
这件事不能在代码里同源地表达——由本测试两侧各 import 一次、逐键比对钉住。
形状漂移（哪一侧改了键）在这里红，而不是在真实批次上静默错位。
"""

from __future__ import annotations

from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.agent_loop import _think_payload
from trajectory_pipeline.rationale.writer import Draft, WriteResult


def test_形状与WriteResult的written形态全等():
    """四键（status / rationale / reason / degraded）逐键同形。

    ``reason`` 值刻意不同（writer 成功写 ``""``，native think 写
    ``"native-think"``——那是来源标记，不是形状差异），故只比键不比值；
    ``rationale`` 子对象比全等。
    """
    got = _think_payload("页面上有「立即观看」，点它")
    ref = WriteResult(
        status="written", draft=Draft(text="页面上有「立即观看」，点它"),
    ).to_json()
    assert set(got) == set(ref)
    assert got["status"] == "written"
    assert got["rationale"] == ref["rationale"]
    assert got["degraded"] == ref["degraded"]
    assert got["reason"] == "native-think"   # 来源标记：writer 恒为 ""


def test_文本原样进text结论恒空():
    """think 是动作前动机的一句话原文：进 ``text``，``conclusion`` 恒空。

    conclusion 是 writer 面向用户的总结位——native think 没有「总结」
    这个概念，拿 think 冒充总结会让 P3 收尾话术（views 读 conclusion）
    把一句动机渲染成「任务总结」。
    """
    got = _think_payload("  看到了播放按钮  ")
    assert got["rationale"] == {"text": "  看到了播放按钮  ", "conclusion": ""}


# ═══════════════════════════════════════════════════════════════════════
# R0：来源标记固定 + 原生 think 不被覆盖
# ═══════════════════════════════════════════════════════════════════════


def test_reason取值可辨两个来源():
    """``reason`` 的**成功形态**只有两个值，失败形态必须带原因。

    - 原生 think：``actions.NATIVE_THINK_REASON``（``"native-think"``）；
    - writer 成功：``""``（历史口径，不改）；
    - writer 失败：非空错误文本——「没写成」不能长成「模型没说话」。
    """
    native = _think_payload("页面上有播放按钮")
    writer_ok = WriteResult(
        status="written", draft=Draft(text="页面上有播放按钮"),
    ).to_json()
    writer_bad = WriteResult(status="unavailable", reason="LLM 不可用").to_json()

    assert actions.NATIVE_THINK_REASON == "native-think"
    assert native["status"] == "written"
    assert native["reason"] == actions.NATIVE_THINK_REASON
    assert writer_ok["status"] == "written"
    assert writer_ok["reason"] == ""
    assert writer_bad["status"] != "written"
    assert writer_bad["reason"], "失败形态必须带原因，不能与 writer 成功同形"


def test_先挂native_think再补记仍是native_think():
    """R0-3：``attach_rationale`` 的守卫——原生 think 是决策当时写下的
    证据，任何事后补记都不得覆盖它。"""
    log = actions.StepLog()
    index = log.act(actions.goto("https://x.test/"))
    native = _think_payload("先在搜索结果里挑一个像正片的链接")
    log.attach_rationale(index, native)

    log.attach_rationale(index, WriteResult(
        status="written", draft=Draft(text="事后补写的理由"),
    ).to_json())

    assert log.steps[index].rationale == native
    assert log.steps[index].rationale["reason"] == actions.NATIVE_THINK_REASON


def test_writer产物仍可被重新生成():
    """守卫只挡原生 think：writer 自己的产物（``reason=""``）仍可重写——
    那是再生路径，与「覆盖另一个来源的证据」不是一回事。"""
    log = actions.StepLog()
    index = log.act(actions.goto("https://x.test/"))
    first = WriteResult(status="written", draft=Draft(text="第一版")).to_json()
    second = WriteResult(status="written", draft=Draft(text="第二版")).to_json()
    log.attach_rationale(index, first)
    log.attach_rationale(index, second)
    assert log.steps[index].rationale == second
