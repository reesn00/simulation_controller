"""动作派发按面（P5）：一个动作要发出去，参数名必须与**当前面**一致。

挡住的三类静默：

1. ``aria_role`` 面发 ``ref=`` —— 驱动显式拒绝（``ValueError``），看着像
   接线 bug，实际是派发点漏了方言；
2. ``html_tag`` 面发 ``role=/name=`` —— 同上；
3. **两个都发** —— 两个实现都会拒绝，但拒绝原因说的是「跨面参数」，
   与真实原因（派发点不知道自己该发哪套）差着一层。

所以本文件同时钉「发对了」与「只发一套」。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.common.elements import (
    ELEMENT_VOCAB_ARIA_ROLE,
    ELEMENT_VOCAB_HTML_TAG,
)
from trajectory_pipeline.executor import actions
from trajectory_pipeline.executor.actions import ActionTarget
from trajectory_pipeline.executor.agent_loop import ActionInstruction, _apply


class _Recorder:
    """两套参数都收的驱动替身——**只记账，不判合法性**。

    合法性由真驱动判（跨面抛 ``ValueError``），这里要看的是
    「派发点究竟发了哪套参数」。
    """

    name = "recorder"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        self.calls.append(("goto", {"url": url}))

    async def click(self, **kwargs) -> None:
        self.calls.append(("click", kwargs))

    async def fill(self, **kwargs) -> None:
        self.calls.append(("fill", kwargs))

    async def press(self, key: str, **kwargs) -> None:
        self.calls.append(("press", {"key": key, **kwargs}))


class Test目标参数:
    def test_html标签面只发ref(self):
        params = actions.target_params(
            ELEMENT_VOCAB_HTML_TAG,
            ActionTarget(role="button", name="播放"),
            ref="e3",
        )
        assert params == {"ref": "e3"}
        # 「只发一套」：语义目标不进 obscura 面的调用
        assert "role" not in params and "name" not in params

    def test_aria面只发role和name(self):
        params = actions.target_params(
            ELEMENT_VOCAB_ARIA_ROLE,
            ActionTarget(role="textbox", name="搜索"),
            ref="e3",
        )
        assert params == {"role": "textbox", "name": "搜索"}
        # ref 是会话句柄，**不进** aria 面的调用
        assert "ref" not in params

    def test_值不翻译(self):
        """跨面不改值：``textbox`` 不会变成 ``input``，反之亦然。

        值域是方言的一部分（P4 §4.8）：翻译要靠猜 ``<input>`` 的 type，
        猜错就是「声明与真实调用对不上」。
        """
        assert actions.target_params(
            ELEMENT_VOCAB_ARIA_ROLE, ActionTarget(role="textbox", name="x")
        )["role"] == "textbox"
        assert actions.target_params(
            ELEMENT_VOCAB_HTML_TAG, ActionTarget(role="input", name="x"), ref="e1"
        ) == {"ref": "e1"}

    def test_aria面没有语义目标时不发动作(self):
        """没有 target 就没有 ``role+name`` —— 这是 ``UnaddressableTarget``，
        **不是**「退回 ref，反正 ref 也有」：aria 面不认 ref。"""
        with pytest.raises(actions.UnaddressableTarget):
            actions.target_params(ELEMENT_VOCAB_ARIA_ROLE, None, ref="e3")

    def test_html标签面没有语义目标仍可执行(self):
        """obscura 面有 ref 就够——目标缺失只影响 ``steps[]`` 记账。"""
        assert actions.target_params(
            ELEMENT_VOCAB_HTML_TAG, None, ref="e3"
        ) == {"ref": "e3"}

    def test_认不出的词汇表直接抛(self):
        with pytest.raises(ValueError, match="认不出的元素词汇表"):
            actions.target_params("dom_tag", ActionTarget(role="a", name="x"),
                                  ref="e1")


class Testagent派发:
    """agent 循环里 ref 仍是指令的字段，但**驱动层收到的**是方言参数。"""

    async def test_click按观察的词汇表派发(self):
        driver = _Recorder()
        instr = ActionInstruction(tool="click", ref="e2")
        await _apply(
            driver, instr,
            target=ActionTarget(role="link", name="立即播放"),
            vocabulary=ELEMENT_VOCAB_ARIA_ROLE,
        )
        assert driver.calls == [("click", {"role": "link", "name": "立即播放"})]

    async def test_fill按观察的词汇表派发(self):
        driver = _Recorder()
        instr = ActionInstruction(tool="fill", ref="e7", params={"text": "功夫"})
        await _apply(
            driver, instr,
            target=ActionTarget(role="textbox", name="搜索"),
            vocabulary=ELEMENT_VOCAB_ARIA_ROLE,
        )
        assert driver.calls == [
            ("fill", {"text": "功夫", "role": "textbox", "name": "搜索"})
        ]

    async def test_obscura面仍发ref(self):
        driver = _Recorder()
        instr = ActionInstruction(tool="click", ref="e2")
        await _apply(
            driver, instr,
            target=ActionTarget(role="button", name="播放"),
            vocabulary=ELEMENT_VOCAB_HTML_TAG,
        )
        assert driver.calls == [("click", {"ref": "e2"})]

    async def test_aria面缺目标时抛而不是静默不发(self):
        driver = _Recorder()
        with pytest.raises(actions.UnaddressableTarget):
            await _apply(
                driver,
                ActionInstruction(tool="click", ref=""),
                target=None,
                vocabulary=ELEMENT_VOCAB_ARIA_ROLE,
            )
        assert driver.calls == []

    async def test_press不带目标面别无关(self):
        """回车提交是页面级按键：两面同形，不需要目标参数。"""
        driver = _Recorder()
        await _apply(driver, ActionInstruction(tool="press", params={"key": "Enter"}))
        assert driver.calls == [("press", {"key": "Enter"})]