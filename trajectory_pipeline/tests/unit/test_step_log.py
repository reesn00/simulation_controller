"""``steps[]`` 的**单步耗时**（2026-10-21）。

为什么要有这个字段：camoufox 面的点击超时与「页面没有播放器」
在证据上**同形**——都是一条 ``error`` 非空的步骤。分不出「慢了一点」
与「慢到离谱」，就只能靠猜；而猜错的代价是**理由通顺的假负样本**
进了负样本池。

口径钉死在这三条上：

1. **计时挂在 ``act`` / ``settle`` 这一对方法上**，不是每个调用点自己测。
   调用点有八处，漏一处就少一条耗时证据——而漏掉的那处往往正是
   「动作失败」那条（最该留证据的）。
2. **整步耗时**（动作 + 随后的观察），不是「动作单独那几秒」。
   失败路径上动作一抛就收口，整步 ≈ 动作耗时——而失败路径正是要诊断的那条。
3. **``None`` ≠ 0**。``None`` 是「这一步没走到 settle」（中途异常），
   0 是「真的瞬间完成」。两者混同就是把「没记到」读成「很快」。
4. **计时用 ``time.perf_counter``，不是 ``time.monotonic``**。Windows 上
   ``monotonic`` 走 ``GetTickCount64``，**分辨率 15.625ms**——量 30ms
   可能读出 15ms，于是「耗时刻度」这件事在 Windows 上是**假的**。
   这不是洁癖：``test_耗时_真的量到了_时间`` 就是被这条绊过——它时红时绿。
"""

from __future__ import annotations

import time

from trajectory_pipeline.executor.actions import Action, StepLog


def _action(tool: str = "click") -> Action:
    return Action(tool=tool, params={})


class Test单步耗时:
    def test_act_之后_耗时还是空(self):
        """动作发出去了但还没 settle → 没有耗时。

        这时填 0 就等于说「瞬间完成」——而真相是「还没结束」。
        """
        log = StepLog()
        log.act(_action())
        assert log.steps[0].duration_ms is None

    def test_settle_之后_有耗时(self):
        log = StepLog()
        index = log.act(_action())
        log.settle(index)
        assert log.steps[0].duration_ms is not None
        assert log.steps[0].duration_ms >= 0

    def test_耗时_真的量到了_时间(self):
        """sleep 30ms → 耗时至少 30ms。

        这条挡的是「填了个常数」的实现——那种实现下所有步的耗时都一样，
        于是「分不清 5s 和 13s」这个原始需求根本没被满足。
        """
        log = StepLog()
        index = log.act(_action())
        time.sleep(0.03)
        log.settle(index)
        assert log.steps[0].duration_ms is not None
        assert log.steps[0].duration_ms >= 30

    def test_第二次_settle_不改写_第一次的耗时(self):
        """这一步的「结束时刻」是**第一次**收口那一刻。

        有的流程会先 settle(error) 再 settle(obs)；耗时要是跟着第二次走，
        读出来的是「到最终收口隔了多久」——不是这一步的耗时。
        """
        log = StepLog()
        index = log.act(_action())
        time.sleep(0.02)
        log.settle(index, error="点了但点不动")
        first = log.steps[0].duration_ms
        time.sleep(0.02)
        log.settle(index)
        assert log.steps[0].duration_ms == first

    def test_落盘形状带_duration_ms(self):
        """P1 的 ``steps[]`` 元素要真的带上这个字段。

        不落盘 = 字段只活在内存里，事后读存档照样分不出快慢。
        """
        log = StepLog()
        index = log.act(_action())
        log.settle(index, error="点了但点不动")
        payload = log.steps[0].to_json(lambda obs: None)
        assert payload["duration_ms"] == log.steps[0].duration_ms
        assert "duration_ms" in payload


class Test与观察的关系:
    def test_成功路径的耗时_含观察(self):
        """成功路径上整步耗时 = 动作 + 观察。

        这是口径的**已知代价**（见类文档第 2 条）：它不是「动作单独那几秒」。
        文档写清楚了，测试就把这条写死，免得后人把它当成动作耗时读。
        """
        log = StepLog()
        index = log.act(_action())
        time.sleep(0.02)
        log.settle(index, observation=object())     # type: ignore[arg-type]
        assert log.steps[0].duration_ms is not None

    def test_失败路径的耗时_约等于动作耗时(self):
        """动作一抛就 settle ⇒ 整步 ≈ 动作耗时。

        而这正是「5s 还是 13s」要分辨的那条路径。
        """
        log = StepLog()
        index = log.act(_action())
        time.sleep(0.02)
        log.settle(index, error="Locator.click: Timeout 15000ms exceeded")
        assert log.steps[0].duration_ms is not None
        assert log.steps[0].duration_ms >= 20
