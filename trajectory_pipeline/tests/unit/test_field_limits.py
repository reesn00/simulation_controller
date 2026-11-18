"""落盘 / 投影字段的长度上限——**唯一真源**（H-41）。

同一份字段原先在三处各写各的（P1 落盘 120 / 感知层 60 / 复核页 60）⇒
「**模型看到的名字**」与「**存档里的名字**」不是同一个字符串。

元素名是 rationale 引用的**主要锚点**：模型引用了一个存档里查不到的
字符串 ⇒ 训练出来的 rationale 指向一个**不存在的目标**。而这**不报任何错**
——两份数据各自都合法，只是说的不是同一件事（症状是「模型总在引用不存在
的元素」，排查时却找不到任何一处不一致，因为压根没有「一致」这个概念）。

⇒ 这一组测试盯的是「**四处同值**」与「**真的没被裁**」。
"""

from __future__ import annotations

from trajectory_pipeline.common import field_limits
from trajectory_pipeline.common.elements import element_name
from trajectory_pipeline.executor import dom, orchestrator
from trajectory_pipeline.executor.browser import camofox_driver, obscura_driver
from trajectory_pipeline.perception import llm_perceptor
from trajectory_pipeline.perception.base import (
    InteractiveElement,
    LinkItem,
    Observation,
)


def _obs(name: str = "立即播放", text: str = "正片") -> Observation:
    return Observation(
        url="https://x.test/movie",
        page_title="让子弹飞",
        body_text="正文",
        interactive_elements=(InteractiveElement(ref="e1", role="button",
                                                name=name),),
        links=(LinkItem(text=text, href="https://x.test/play"),),
    )


class Test四处同值:
    def test_执行端转发到地基常量(self):
        """``executor`` 的两个常量是**转发**，不是各写一份数字。"""
        assert orchestrator.OBS_ELEMENT_NAME_LIMIT == (
            field_limits.ELEMENT_NAME_LIMIT
        )
        assert orchestrator.OBS_LINK_TEXT_LIMIT == field_limits.LINK_TEXT_LIMIT

    def test_地基常量就是那三个数(self):
        """⚠️ 改这些数会改**存档形状**（P1 落盘）⇒ 必须是有意识的改动，
        不是一个「随手调大」。"""
        assert field_limits.ELEMENT_NAME_LIMIT == 120
        assert field_limits.LINK_TEXT_LIMIT == 80


class Test条数四层同值:
    """元素 / 链接的**条数**上限：采集 → 落盘 → 送模型**三层同值**。

    ⚠️ **为什么必须同值**（H-41）：D-2 评分器靠「**落盘了几条**」与
    「**采到了几条**」的差判断「目标是不是可能被裁掉了」——那个差
    **只有在两处对齐时才有意义**。三层不齐时它会**静默地把裁剪当成
    不存在** ⇒ 评分器在残缺事实上给结论 ⇒ 凭空造出负样本。

    ⚠️ 这不是「三条断言」而是**一条不变量**：任何一层单独改，都得回来
    改这里（或改地基常量）。
    """

    def test_元素条数三层同值(self):
        assert obscura_driver.DEFAULT_LIMIT == (
            field_limits.ELEMENT_COUNT_LIMIT
        )
        assert camofox_driver.DEFAULT_LIMIT == field_limits.ELEMENT_COUNT_LIMIT
        assert orchestrator.OBS_ELEMENT_LIMIT == field_limits.ELEMENT_COUNT_LIMIT
        assert llm_perceptor.LLM_ELEMENTS == field_limits.ELEMENT_COUNT_LIMIT

    def test_链接条数三层同值(self):
        assert orchestrator.OBS_LINK_LIMIT == field_limits.LINK_COUNT_LIMIT
        assert llm_perceptor.LLM_LINKS == field_limits.LINK_COUNT_LIMIT

    def test_读步上限与普通步同值(self):
        """读原语（``interactive`` / ``links``）的落盘上限。

        ⚠️ 以前是「读 200 / 普通 80」——**普通步那条窄**，于是读到的第
        81–200 条元素变成「模型看过、证据里没有」。现在普通步也到 200
        ⇒ 两边同值 ⇒ 那条「需要解释的差异」消失。
        """
        assert orchestrator.OBS_READ_ELEMENT_LIMIT == (
            field_limits.ELEMENT_COUNT_LIMIT
        )
        assert orchestrator.OBS_READ_LINK_LIMIT == field_limits.LINK_COUNT_LIMIT

    def test_正文预算采集层与送模型层同值(self):
        """正文字符预算：采集层（``browser_snapshot`` 的 max_chars）与
        送模型那一层（判定输入）同值。

        ⚠️ 不齐的后果是**方向相反的两处**：采集层比模型层小 ⇒ 模型永远
        看不到全量（它连输入都不全，谈不上「选核心内容」）；模型层比
        采集层小 ⇒ 模型在残骸上判 ⇒ 标签错 ⇒ 「训练出来的模型说这个站
        没有正片」，而页面上明明有。
        """
        assert dom.DEFAULT_MAX_CHARS == field_limits.BODY_BUDGET_CHARS
        assert llm_perceptor.LLM_BODY_CHARS == field_limits.BODY_BUDGET_CHARS

    def test_分批宽度不超过窗口(self):
        """⚠️ **不变量是「批 ≤ 窗口」，不是「批 == 窗口」**（H-41）。

        窗口（``LLM_LINKS``）抬到 200，而分批宽度（``PERCEPTION_LINK_WINDOW``）
        仍是 20——**批比窗口小**是安全的（这一批一定完整送得出去）；
        **批比窗口大**才危险（那一批会被感知层静默裁掉，而构造保证说
        「模型看得到全部候选」）。
        """
        assert orchestrator.PERCEPTION_LINK_WINDOW <= llm_perceptor.LLM_LINKS


class Test模型看到的名字:
    def test_元素名不再被裁到60(self):
        """感知层原先裁 60 字、P1 落盘裁 120 字 ⇒ 模型看到的名字与存档里
        的**不是同一个字符串**。"""
        name = "立即播放" + "超长标题" * 12      # 60 字以上、120 字以内
        out = llm_perceptor._render(_obs(name=name))
        assert element_name(out["interactive_elements"][0]) == name

    def test_链接文字不再被裁到60(self):
        text = "正片" + "高清完整版" * 8          # 60 字以上、80 字以内
        out = llm_perceptor._render(_obs(text=text))
        assert out["links"][0]["text"] == text

    def test_真的超上限时仍然裁(self):
        """「统一口径」不是「取消上限」——超了还是得裁（只是裁的位置一样）。"""
        name = "x" * 200
        out = llm_perceptor._render(_obs(name=name))
        assert len(element_name(out["interactive_elements"][0])) == (
            field_limits.ELEMENT_NAME_LIMIT
        )
