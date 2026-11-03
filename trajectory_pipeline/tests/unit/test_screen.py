"""训练步筛选（``assembler.screen``，方案 23 R3-1 / R3-2）。

两条口径，盯的是**同一种安静的坏**：

- **死循环步**（相邻同动作 + 观察逐字相同）留在训练集里，教的是
  「没反应就再点一次」；
- **重复动作**（同一 unit 内做第二遍）本身可能是合法的（滚动加载、
  换词重搜），所以只标记不剔——但标记必须能定位到具体哪一步，
  合成一个计数就查不出「重复的是哪一下」。
"""

from __future__ import annotations

from trajectory_pipeline.assembler import screen


def _step(
    index: int,
    *,
    unit: str = "site-1",
    tool: str = "click",
    params: dict | None = None,
    target: dict | None = None,
    observation: str = "同一份页面",
) -> screen.Step:
    return screen.Step(
        step_index=index,
        unit=unit,
        action={"tool": tool, "params": params or {}, "target": target or {}},
        observation=observation,
    )


class Test动作签名:
    def test_参数顺序不同仍是同一个动作(self):
        """签名对键排序——渲染行的参数顺序依赖 dict 插入序，直接比会漏。"""
        a = _step(0, params={"url": "https://x.test", "lang": "zh"})
        b = _step(1, params={"lang": "zh", "url": "https://x.test"})
        assert screen.action_key(a.action) == screen.action_key(b.action)

    def test_target与params平铺后比较(self):
        a = _step(0, tool="click", target={"role": "link", "name": "播放"})
        b = screen.Step(
            step_index=1,
            unit="site-1",
            action={
                "tool": "click",
                "params": {"role": "link", "name": "播放"},
                "target": {},
            },
            observation="",
        )
        assert screen.action_key(a.action) == screen.action_key(b.action)

    def test_不同工具不同签名(self):
        a = _step(0, tool="click")
        b = _step(1, tool="press")
        assert screen.action_key(a.action) != screen.action_key(b.action)

    def test_空值槽不参与签名(self):
        a = _step(0, target={"role": "link", "name": "播放"})
        b = _step(1, target={"role": "link", "name": "播放", "label": ""})
        assert screen.action_key(a.action) == screen.action_key(b.action)


class Test死循环剔除:
    def test_相邻同动作同观察只留第一步(self):
        result = screen.screen([_step(0), _step(1)])
        assert [s.step_index for s in result.steps] == [0]
        assert result.deduped == (1,)

    def test_连续三步只留第一步(self):
        """比较对象是**上一条保留的步**，A/A/A 的后两步都与第一步比。"""
        result = screen.screen([_step(0), _step(1), _step(2)])
        assert [s.step_index for s in result.steps] == [0]
        assert result.deduped == (1, 2)

    def test_同动作但观察变了要保留(self):
        """页面刷新 / 滚动加载后是同动作不同观察——不是死循环。"""
        result = screen.screen([
            _step(0, observation="第一屏"),
            _step(1, observation="第二屏"),
        ])
        assert [s.step_index for s in result.steps] == [0, 1]
        assert result.deduped == ()

    def test_同观察但动作变了要保留(self):
        result = screen.screen([
            _step(0, tool="click", target={"name": "登录"}),
            _step(1, tool="press", params={"key": "Enter"}),
        ])
        assert [s.step_index for s in result.steps] == [0, 1]
        assert result.deduped == ()

    def test_跨unit不剔(self):
        """换了一个站再做同一个动作，是「换站访问」不是原地打转。"""
        result = screen.screen([
            _step(0, unit="site-1"),
            _step(1, unit="site-2"),
        ])
        assert [s.step_index for s in result.steps] == [0, 1]
        assert result.deduped == ()

    def test_剔除后保留步顺序不变(self):
        result = screen.screen([
            _step(0, observation="甲"),
            _step(1, observation="甲"),
            _step(2, tool="press", params={"key": "Enter"}, observation="乙"),
        ])
        assert [s.step_index for s in result.steps] == [0, 2]
        assert result.deduped == (1,)


class Test重复动作标记:
    def test_同一unit第二次起标记(self):
        result = screen.screen([
            _step(0, observation="甲"),
            _step(1, observation="乙"),
        ])
        assert result.repeats == ({
            "step_index": 1, "unit": "site-1", "tool": "click", "occurrence": 2,
        },)

    def test_不同unit各自从第一次算(self):
        result = screen.screen([
            _step(0, unit="site-1"),
            _step(1, unit="site-2"),
        ])
        assert result.repeats == ()

    def test_重复但不相邻也标记(self):
        """A/B/A：中间隔了一步，仍是同一 unit 内的重复。"""
        result = screen.screen([
            _step(0, observation="甲"),
            _step(1, tool="press", params={"key": "Enter"}, observation="乙"),
            _step(2, observation="丙"),
        ])
        assert [r["step_index"] for r in result.repeats] == [2]
        assert result.repeats[0]["occurrence"] == 2

    def test_被剔的死循环步不进重复标记(self):
        """只统计留在训练集里的步——被剔的由 ``deduped`` 交代。"""
        result = screen.screen([_step(0), _step(1), _step(2)])
        assert result.deduped == (1, 2)
        assert result.repeats == ()

    def test_第三次出现记occurrence3(self):
        result = screen.screen([
            _step(0, observation="甲"),
            _step(1, observation="乙"),
            _step(2, observation="丙"),
        ])
        assert [r["occurrence"] for r in result.repeats] == [2, 3]


class Test空输入:
    def test_没有步(self):
        result = screen.screen([])
        assert result.steps == ()
        assert result.deduped == ()
        assert result.repeats == ()