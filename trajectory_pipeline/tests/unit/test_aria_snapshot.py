"""``aria_snapshot()`` 文本解析的口径测试——**不联网、不开浏览器**。

这个解析器只有一件事要做：把 playwright 吐的文本读回 ``(role, name)``。
它写错的后果**不报错**——元素列表少几项、或名字里多带一个字，
下游照样能跑，只是**定位不到**或**定位到错的东西**。所以每条用例
都对着一行真实格式的坑（见 :mod:`...browser.aria_snapshot` 的模块 docstring）。

以下用例的文本取自实测输出（``playwright 1.62.0``）的形态，不是假想格式。
"""

from __future__ import annotations

from trajectory_pipeline.executor.browser.aria_snapshot import AriaNode, parse_nodes

#: 一段真实页面的形态：嵌套、无名字、行内文本、状态后缀、属性行、重复名字全都有。
SAMPLE = """\
- generic [active] [ref=e1]:
  - banner:
    - link "首页":
      - /url: /
  - button "立即播放"
  - button "关闭": X
  - textbox "站内搜索"
  - textbox
  - combobox:
    - option "全部" [selected]
  - button "详情"
  - button "详情"
  - checkbox "记住我" [checked]
  - text: 一段不属于任何控件的话
"""


class TestParseNodes:
    def test_嵌套节点也要解析(self):
        """真实页面的可访问性树几乎全是嵌套的；只认顶格会漏掉绝大多数元素。"""
        nodes = parse_nodes(SAMPLE)
        roles = [n.role for n in nodes]
        assert "link" in roles and "button" in roles
        assert AriaNode(role="link", name="首页") in nodes

    def test_行内文本不算名字(self):
        """``- button "关闭": X`` 的 ``X`` 是页面文本，不是名字的一部分。

        把它并进 name，动作面拿 ``name="关闭: X"`` 去定位必然 0 命中——
        而 0 命中不报错，只表现为「点了没反应」。
        """
        nodes = parse_nodes('- button "关闭": X')
        assert nodes == (AriaNode(role="button", name="关闭"),)

    def test_引号内按_JSON_转义还原(self):
        nodes = parse_nodes(r'- button "say \"hi\" \\ 结束"')
        assert nodes[0].name == 'say "hi" \\ 结束'

    def test_状态后缀不影响名字(self):
        nodes = parse_nodes('- checkbox "记住我" [checked]')
        assert nodes == (AriaNode(role="checkbox", name="记住我"),)

    def test_没有可访问名时是_None_而不是空串(self):
        """``- link:`` / ``- combobox:`` 都没有名字。两者的执行期结局相同
        （定位不到），但诊断上「没有名字」和「名字是空串」要分开。"""
        nodes = parse_nodes("- link:\n- combobox:")
        assert nodes == (
            AriaNode(role="link", name=None),
            AriaNode(role="combobox", name=None),
        )

    def test_role_不吃冒号(self):
        """``- combobox:`` 的 role 是 ``combobox``。吃进冒号会让它变成一个
        词表里没有的 role，于是**无声消失**。"""
        assert parse_nodes("- combobox:")[0].role == "combobox"

    def test_属性行不崩(self):
        """``- /url: /b`` 的「role」是 ``/url``，不在词表里会被过滤——
        但解析不能崩，否则整页元素列表消失。"""
        nodes = parse_nodes("- link \"详情\":\n  - /url: /b")
        assert nodes[0] == AriaNode(role="link", name="详情")
        assert AriaNode(role="/url", name=None) in nodes

    def test_空文本给空元组(self):
        assert parse_nodes("") == ()
        assert parse_nodes("   \n\n  ") == ()

    def test_转义还原失败不抛(self):
        """还原失败说明 playwright 换了转义口径：原样给出比抛异常好——
        抛掉会让一整页元素消失，而多数名字不含需要转义的字符。"""
        nodes = parse_nodes(r'- button "a\qb"')
        assert nodes[0].name == r"a\qb"

    def test_重复名字原样保留(self):
        """**去重不是解析器的职责**。parse 只读数，同名整组丢弃是采集面
        （``CamoufoxDriver.interactive``）的决定，且要计数给人看。"""
        nodes = parse_nodes('- button "详情"\n- button "详情"')
        assert nodes == (
            AriaNode(role="button", name="详情"),
            AriaNode(role="button", name="详情"),
        )