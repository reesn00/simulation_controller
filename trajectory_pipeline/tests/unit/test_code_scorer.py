"""D-2 打分器（``evaluation/scorers/code_scorer.py``）单测。

rubric **v1.4** 起，D-2 判的是「**前后动作顺序正常、连贯**」，判法分两段：

1. **代码筛**（:func:`screen_d2`）——机械提取时序疑点，不产出分数；
2. **LLM 判语义**（:func:`score_d2`）——逐疑点判真伪、补漏、标严重度。

「点击目标在不在观察的元素清单里」**不再是本维判据**。本文件的重点因此
从「引用找没找到」换成三件事：

- **筛得对**：缺观察 / goto 落点不符 / 同页连续重复各自只在自己该触发时触发；
- **不判反**：事实残缺（无模型动作 / 某步缺观察）与能力缺失（无 LLM /
  输出不可解析 / 不合契约）一律 ``undetermined``，**不扣轨迹的分**；
- **落档对**：只有**真断裂**计数，且「实质」优先于「轻微」。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from trajectory_pipeline.assembler import schema
from trajectory_pipeline.assembler.schema import ActionView, GateMark, Sample
from trajectory_pipeline.evaluation.scorers.code_scorer import (
    BODY_PREVIEW_CHARS,
    SEVERITIES,
    BreakFinding,
    _score_of,
    screen_d2,
    score_d2,
)


# ═══════════════════════════════════════════════════════════════════════
# 语料与替身
# ═══════════════════════════════════════════════════════════════════════


def _obs(**kw) -> dict[str, Any]:
    """默认是一份**可用**的观察（正文 + URL 非空）。"""
    base = {
        "url": "https://x.test/movie", "page_title": "功夫",
        "body_text": "电影详情", "degraded": [], "truncated": False,
        "video_tag_count": 0, "iframe_count": 0,
        "interactive_elements": [], "links": [],
    }
    base.update(kw)
    return base


def _act(tool: str, params: dict | None = None,
         target: dict | None = None, origin: str = "model") -> ActionView:
    return ActionView(tool=tool, params=params or {}, target=target,
                      origin=origin)


def _goto(url: str) -> ActionView:
    return _act("goto", {"url": url})


def _click(label: str = "播放") -> ActionView:
    return _act("click", target={"tag": "button", "label": label})


def _sample(*actions: ActionView, observations=()) -> Sample:
    return Sample(
        sample_id="T001__a", task_id="T001",
        title="功夫", system_prompt=schema.SYSTEM_PROMPT, tools=schema.TOOL_SPECS,
        user_prompt="有没有能看正片的", provenance={"genre": "喜剧"},
        actions=tuple(actions), observations=tuple(observations),
        gate=GateMark(status="not_run", reasons=("未跑",)),
        outcome=None,
    )


class _LLM:
    """假 LLM：原样回一段文本，并记下收到的 prompt。"""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if isinstance(self.response, str):
            return self.response
        return json.dumps(self.response, ensure_ascii=False)


def _ok_points(n: int) -> list[dict[str, Any]]:
    return [{"index": i, "is_break": False, "reason": ""} for i in range(n)]


def _ok(n: int) -> dict[str, Any]:
    return {"points": _ok_points(n), "new_breaks": []}


def _one_point_sample() -> Sample:
    """1 个时序疑点：goto 没落到目标页。"""
    return _sample(
        _goto("https://x.test/movie"),
        observations=[_obs(url="https://other.test/login", page_title="登录")],
    )


def _two_point_sample() -> Sample:
    """2 个疑点：同页连续重复（第 2 步）+ goto 落点不符（第 3 步）。"""
    return _sample(
        _click("播放"), _click("播放"),
        _goto("https://x.test/a"),
        observations=[
            _obs(url="https://x.test/movie"),
            _obs(url="https://x.test/movie"),
            _obs(url="https://other.test/b", page_title="被拦"),
        ],
    )


def _three_point_sample() -> Sample:
    """3 个疑点：三次 goto 全部落到别的主机。"""
    return _sample(
        _goto("https://a.test/x"), _goto("https://c.test/x"),
        _goto("https://e.test/x"),
        observations=[
            _obs(url="https://b.test/y"), _obs(url="https://d.test/y"),
            _obs(url="https://f.test/y"),
        ],
    )


def _no_point_sample() -> Sample:
    return _sample(_goto("https://x.test/movie"),
                   observations=[_obs(url="https://x.test/movie")])


# ═══════════════════════════════════════════════════════════════════════
# 第一段：代码筛
# ═══════════════════════════════════════════════════════════════════════


class TestScreen:
    def test_没有模型动作判不可判定(self):
        """执行器的 ``new_tab`` 不算模型动作——没有动作链可判，
        且**「没有可判对象」不等于「全程连贯」**。"""
        screen = screen_d2(_sample(
            _act("new_tab", {"url": "u"}, origin="infrastructure"),
            observations=[_obs()],
        ))
        assert screen.kind == "undetermined" and screen.steps == ()
        assert any("不等于" in r for r in screen.reasons)

    def test_缺观察判不可判定且不扣分(self):
        """缺观察是**采集事实残缺**，不是轨迹不连贯——整条不出分。"""
        screen = screen_d2(_sample(_goto("https://x.test/movie")))
        assert screen.kind == "undetermined"
        assert any("第 1 步" in r and "采集事实残缺" in r for r in screen.reasons)

    def test_观察三项全空判不可判定(self):
        screen = screen_d2(_sample(
            _goto("https://x.test/movie"),
            observations=[_obs(url="", page_title="", body_text="")],
        ))
        assert screen.kind == "undetermined"
        assert any("三项全空" in r for r in screen.reasons)

    def test_观察降级但非空仍可筛(self):
        """新判据不要求元素清单完整：只要这一步「看到了什么」还在，
        就能判顺序。降级 / 截断不再是不可判定的理由（旧口径才需要）。"""
        screen = screen_d2(_sample(
            _goto("https://x.test/movie"),
            observations=[_obs(url="https://x.test/movie",
                               degraded=["browser_interactive_elements"],
                               truncated=True)],
        ))
        assert screen.kind == "screenable" and screen.points == ()

    def test_执行器动作不占模型动作链但观察仍按原下标对齐(self):
        """``sample.actions`` 与 ``observations`` 按位对齐（含执行器动作）。
        先过滤再按下标取观察会错位一格——这条钉住修正。"""
        sample = _sample(
            _act("new_tab", {"url": "u"}, origin="infrastructure"),
            _goto("https://x.test/landed"),
            observations=[
                _obs(url="https://x.test/start"),
                _obs(url="https://x.test/landed"),
            ],
        )
        screen = screen_d2(sample)
        assert screen.kind == "screenable"
        assert [d.step for d in screen.steps] == [1]
        assert screen.steps[0].url == "https://x.test/landed"
        assert screen.points == ()

    def test_非对象观察不提前过滤(self):
        """``observations`` 里的非对象项必须留在原下标上——先过滤会让后一步
        读到前一步的观察，而那份观察看起来完全正常。"""
        sample = _sample(
            _goto("https://x.test/a"),
            _goto("https://x.test/b"),
            observations=[
                None,
                _obs(url="https://x.test/b"),
            ],
        )
        screen = screen_d2(sample)
        # 第 1 步（下标 0）缺观察 ⇒ 整条不可判定，不能滑到下标 1 去
        assert screen.kind == "undetermined"
        assert any("第 1 步" in r for r in screen.reasons)

    def test_goto落点一致无疑点(self):
        screen = screen_d2(_no_point_sample())
        assert screen.kind == "screenable" and screen.steps
        assert screen.points == ()

    def test_goto忽略scheme与query(self):
        """http→https、query 参数变化都是站点常态，不记疑点。"""
        screen = screen_d2(_sample(
            _goto("http://x.test/movie?from=search"),
            observations=[_obs(url="https://x.test/movie/?utm=1")],
        ))
        assert screen.points == ()

    def test_goto落点不符记实质疑点(self):
        screen = screen_d2(_one_point_sample())
        assert len(screen.points) == 1
        p = screen.points[0]
        assert p.code == "goto_not_landed" and p.severity == "material"
        assert p.step == 0 and "other.test/login" in p.reason

    def test_连续重复动作只记一处疑点(self):
        """G-4：连续相同调用按一次计——整段只在段首记一处。"""
        sample = _sample(
            _click("播放"), _click("播放"), _click("播放"),
            observations=[_obs(url="https://x.test/movie")] * 3,
        )
        screen = screen_d2(sample)
        repeats = [p for p in screen.points if p.code == "repeat_action"]
        assert len(repeats) == 1 and repeats[0].severity == "mild"
        assert repeats[0].step == 1

    def test_相同动作换页面不算原地空转(self):
        sample = _sample(
            _click("播放"), _click("播放"),
            observations=[_obs(url="https://x.test/movie"),
                          _obs(url="https://x.test/play/1")],
        )
        assert screen_d2(sample).points == ()

    def test_点击目标不在观察里不再产生疑点(self):
        """v1.4 去掉「点击目标存在性」：元素清单为空、target 查不到，
        只要正文与 URL 在，就不记任何疑点。"""
        sample = _sample(
            _act("click", target={"tag": "a", "label": "不存在的按钮"}),
            observations=[_obs(url="https://x.test/", body_text="欢迎光临",
                               interactive_elements=[])],
        )
        screen = screen_d2(sample)
        assert screen.kind == "screenable" and screen.points == ()

    def test_正文摘录有长度上限(self):
        screen = screen_d2(_sample(
            _goto("https://x.test/movie"),
            observations=[_obs(url="https://x.test/movie",
                               body_text="长" * (BODY_PREVIEW_CHARS + 100))],
        ))
        body = screen.steps[0].body
        assert len(body) <= BODY_PREVIEW_CHARS + 10
        assert body.endswith("…[已截断]")

    def test_代码筛不产出分数(self):
        """筛只回答「有哪些疑点」，分数归第二段——两段不是一个判据。"""
        screen = screen_d2(_one_point_sample())
        assert not hasattr(screen, "score")


# ═══════════════════════════════════════════════════════════════════════
# 第二段：LLM 输出契约（fail-closed）
# ═══════════════════════════════════════════════════════════════════════


def _verdict(sample: Sample, response: Any):
    llm = _LLM(response)
    return score_d2(sample, llm_client=llm), llm


class TestFailClosed:
    def test_未提供client不出分(self):
        screen = screen_d2(_one_point_sample())
        v = score_d2(_one_point_sample())
        assert v.kind == "undetermined" and v.score is None
        assert len(v.points) == len(screen.points)
        assert any("未提供 LLM client" in r for r in v.reasons)

    def test_空返回不出分(self):
        v, _ = _verdict(_one_point_sample(), "")
        assert v.kind == "undetermined" and any("LLM 返回空" in r for r in v.reasons)

    def test_不是JSON不出分(self):
        v, _ = _verdict(_one_point_sample(), "这不是 JSON")
        assert v.kind == "undetermined" and any("parse_fail" in r for r in v.reasons)

    def test_JSON不是对象不出分(self):
        v, _ = _verdict(_one_point_sample(), "[1, 2]")
        assert v.kind == "undetermined" and any("不是对象" in r for r in v.reasons)

    def test_points不是列表不出分(self):
        v, _ = _verdict(_one_point_sample(), {"points": {}})
        assert v.kind == "undetermined" and any("缺少 points" in r for r in v.reasons)

    def test_points数量不符不出分(self):
        v, _ = _verdict(_one_point_sample(), {"points": []})
        assert v.kind == "undetermined" and any("数量" in r for r in v.reasons)

    def test_index越界不出分(self):
        v, _ = _verdict(_one_point_sample(), {
            "points": [{"index": 1, "is_break": False}],
        })
        assert v.kind == "undetermined" and any("越界" in r for r in v.reasons)

    def test_index重复不出分(self):
        v, _ = _verdict(_two_point_sample(), {
            "points": [{"index": 0, "is_break": False},
                       {"index": 0, "is_break": False}],
        })
        assert v.kind == "undetermined" and any("重复" in r for r in v.reasons)

    def test_is_break不是布尔不出分(self):
        v, _ = _verdict(_one_point_sample(), {
            "points": [{"index": 0, "is_break": "yes"}],
        })
        assert v.kind == "undetermined" and any("不是布尔" in r for r in v.reasons)

    def test_断裂严重度不在契约内不出分(self):
        v, _ = _verdict(_one_point_sample(), {
            "points": [{"index": 0, "is_break": True,
                        "severity": "severe", "reason": "r"}],
        })
        assert v.kind == "undetermined" and any("severity" in r for r in v.reasons)

    def test_断裂没写reason不出分(self):
        v, _ = _verdict(_one_point_sample(), {
            "points": [{"index": 0, "is_break": True,
                        "severity": "mild", "reason": ""}],
        })
        assert v.kind == "undetermined" and any("没写 reason" in r for r in v.reasons)

    def test_new_breaks不是列表不出分(self):
        v, _ = _verdict(_one_point_sample(), {
            "points": _ok_points(1), "new_breaks": {},
        })
        assert v.kind == "undetermined" and any("new_breaks 不是列表" in r
                                               for r in v.reasons)

    def test_new_breaks步号越界不出分(self):
        v, _ = _verdict(_one_point_sample(), {
            "points": _ok_points(1),
            "new_breaks": [{"step": 99, "severity": "mild", "reason": "r"}],
        })
        assert v.kind == "undetermined" and any("步号" in r for r in v.reasons)

    def test_new_breaks步号从1起(self):
        """契约里 ``step`` 是「第 N 步」的 N；写 0 不是合法步号。"""
        v, _ = _verdict(_one_point_sample(), {
            "points": _ok_points(1),
            "new_breaks": [{"step": 0, "severity": "mild", "reason": "r"}],
        })
        assert v.kind == "undetermined" and any("从 1 起" in r for r in v.reasons)

    def test_new_breaks严重度不在契约内不出分(self):
        v, _ = _verdict(_one_point_sample(), {
            "points": _ok_points(1),
            "new_breaks": [{"step": 1, "severity": "severe", "reason": "r"}],
        })
        assert v.kind == "undetermined" and any("severity" in r for r in v.reasons)

    def test_new_breaks没写reason不出分(self):
        v, _ = _verdict(_one_point_sample(), {
            "points": _ok_points(1),
            "new_breaks": [{"step": 1, "severity": "mild", "reason": ""}],
        })
        assert v.kind == "undetermined" and any("没写 reason" in r for r in v.reasons)

    def test_合法输出才出分(self):
        v, _ = _verdict(_one_point_sample(), {
            "points": [{"index": 0, "is_break": True,
                        "severity": "mild", "reason": "重试了一次"}],
            "new_breaks": [],
        })
        assert v.kind == "scored" and v.score == 4


# ═══════════════════════════════════════════════════════════════════════
# 落档：按真断裂处数，实质优先
# ═══════════════════════════════════════════════════════════════════════


def _b(severity: str, step: int = 0) -> BreakFinding:
    return BreakFinding(step, severity, "r", "code")  # type: ignore[arg-type]


class TestScoreOf:
    def test_五档映射(self):
        assert _score_of([]) == 5
        assert _score_of([_b("mild")]) == 4
        assert _score_of([_b("mild"), _b("mild")]) == 3
        assert _score_of([_b("material")]) == 2
        assert _score_of([_b("material"), _b("material")]) == 1

    def test_实质优先于轻微(self):
        """一条实质 + 两条轻微 ⇒ 2，不是 3——报 3 会让那条实质断裂消失。"""
        assert _score_of([
            _b("material"), _b("mild"), _b("mild"),
        ]) == 2


@pytest.mark.parametrize(
    ("sample_fn", "response", "expected"),
    [
        (_no_point_sample, _ok(0), 5),
        (_one_point_sample, {"points": [{"index": 0, "is_break": True,
                                        "severity": "mild", "reason": "重试"}],
                             "new_breaks": []}, 4),
        (_two_point_sample, {"points": [
            {"index": 0, "is_break": True, "severity": "mild", "reason": "重复"},
            {"index": 1, "is_break": True, "severity": "mild", "reason": "绕路"},
        ], "new_breaks": []}, 3),
        (_one_point_sample, {"points": [{"index": 0, "is_break": True,
                                        "severity": "material",
                                        "reason": "落到登录页"}],
                             "new_breaks": []}, 2),
        (_three_point_sample, {"points": [
            {"index": 0, "is_break": True, "severity": "material", "reason": "a"},
            {"index": 1, "is_break": False},
            {"index": 2, "is_break": True, "severity": "material", "reason": "c"},
        ], "new_breaks": []}, 1),
    ],
)
def test_端到端落档(sample_fn, response, expected):
    v, _ = _verdict(sample_fn(), response)
    assert v.kind == "scored" and v.score == expected


class TestNewBreaks:
    def test_LLM补出的断裂也计数(self):
        v, _ = _verdict(_no_point_sample(), {
            "points": [],
            "new_breaks": [{"step": 1, "severity": "material",
                            "reason": "动作链跳段"}],
        })
        assert v.kind == "scored" and v.score == 2
        assert v.breaks[0].source == "llm" and v.breaks[0].step == 0

    def test_补出的两条轻微落3(self):
        v, _ = _verdict(_no_point_sample(), {
            "points": [],
            "new_breaks": [
                {"step": 1, "severity": "mild", "reason": "a"},
                {"step": 1, "severity": "mild", "reason": "b"},
            ],
        })
        assert v.score == 3

    def test_补出的步号按全局下标落档(self):
        """带执行器动作时，``sample.actions`` 的全局下标才是报告里的步号。"""
        sample = _sample(
            _act("new_tab", {"url": "u"}, origin="infrastructure"),
            _goto("https://x.test/movie"),
            observations=[_obs(url="https://x.test/start"),
                          _obs(url="https://x.test/movie")],
        )
        v, _ = _verdict(sample, {
            "points": [],
            "new_breaks": [{"step": 2, "severity": "mild", "reason": "r"}],
        })
        assert v.score == 4
        assert v.breaks[0].step == 1


# ═══════════════════════════════════════════════════════════════════════
# 理由与 prompt
# ═══════════════════════════════════════════════════════════════════════


class TestReasons:
    def test_0处断裂的理由(self):
        v, _ = _verdict(_no_point_sample(), _ok(0))
        assert v.reasons == ("0 处断裂：动作链全程连贯——rubric §D-2 锚点 5",)

    def test_断裂理由带步号严重度与来源(self):
        v, _ = _verdict(_one_point_sample(), {
            "points": [{"index": 0, "is_break": True,
                        "severity": "material", "reason": "落到了登录页"}],
            "new_breaks": [],
        })
        assert v.reasons == ("第 1 步（code）：material——落到了登录页",)

    def test_所有评分器的理由都不是被拆过的字符(self):
        """``list(verdict.reasons)`` 的每个元素都得是**整句**。

        这条性质断言跨四个评分器——「括号里写字符串忘尾逗号」在任何一处
        复发都会变红。
        """
        from trajectory_pipeline.evaluation.scorers import (
            d4_scorer, d5_scorer, d6_scorer,
        )

        def _check(fn, v):
            assert isinstance(v.reasons, tuple), (
                f"{fn.__name__}: reasons 不是 tuple（隐式拼接字符串？）")
            for r in v.reasons:
                assert isinstance(r, str), f"{fn.__name__}: 元素不是 str"
                assert len(r) > 1, (
                    f"{fn.__name__}: 理由被拆成单字符了——{r!r}")
                assert not r.startswith(" ") and not r.endswith(" "), (
                    f"{fn.__name__}: 理由首尾带空白——同样是被拆过的形态")

        plain = _sample(_click("立即播放"), observations=[_obs()])
        for fn in (d4_scorer.score_d4, d5_scorer.score_d5, d6_scorer.score_d6):
            for s in (plain, _sample()):
                _check(fn, fn(s))

        cases = [
            (_sample(), None),                    # 无模型动作 → undetermined
            (_no_point_sample(), _LLM(_ok(0))),   # 0 处断裂 → scored
            (_one_point_sample(), _LLM(_ok(1))),  # 有疑点但判误报 → scored
        ]
        for sample, llm in cases:
            _check(score_d2, score_d2(sample, llm_client=llm))


class TestPrompt:
    def test_prompt的锚点段从表渲染(self):
        from trajectory_pipeline.evaluation.scorers import rubric_version
        from trajectory_pipeline.evaluation.scorers.code_scorer import (
            D2_SYSTEM_PROMPT,
        )

        assert rubric_version.render_anchor_block("d2") in D2_SYSTEM_PROMPT

    def test_prompt明确不核对点击目标(self):
        from trajectory_pipeline.evaluation.scorers.code_scorer import (
            D2_SYSTEM_PROMPT,
        )

        assert "不要" in D2_SYSTEM_PROMPT
        assert "点击目标的名称" in D2_SYSTEM_PROMPT
        assert "不是本维判据" in D2_SYSTEM_PROMPT

    def test_user_prompt只给事实(self):
        sample = _two_point_sample()
        screen = screen_d2(sample)
        v, llm = _verdict(sample, _ok(len(screen.points)))
        assert v.kind == "scored"
        system, user = llm.calls[0]
        assert system.startswith("你是一名轨迹连贯性判读员")
        assert "## 动作链" in user and "### 第 1 步" in user
        assert "任务：有没有能看正片的" in user
        assert "code=repeat_action" in user and "code=goto_not_landed" in user
        assert "index=0" in user and f"index={len(screen.points) - 1}" in user

    def test_可换一版system_prompt(self):
        sample = _no_point_sample()
        llm = _LLM(_ok(0))
        v = score_d2(sample, llm_client=llm, system_prompt="自定义提示")
        assert v.kind == "scored" and llm.calls[0][0] == "自定义提示"

    def test_结果可序列化(self):
        v, _ = _verdict(_one_point_sample(), _ok(1))
        json.dumps(v.to_json(), ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# 真实存档
#
# 离线 fixture 是我们自己造的，规则和 fixture 一起错，测试照样绿
# （CLAUDE.md 明写这条）。这里要证明的是「打分器读得懂真 P1 的形状」——
# 键名猜错时它会静默地判「查不到 / 不可判定」，而报告上看不出异常。
# 产物目录不入库，没有就 skip。
# ═══════════════════════════════════════════════════════════════════════


class TestOnRealArchives:
    def _archives(self):
        from trajectory_pipeline.executor.archive import DEFAULT_ROOT

        if not DEFAULT_ROOT.exists():
            pytest.skip("没有真实 P1 存档（output/pipeline/ 是产物目录，未入库）")
        picked = [p for p in DEFAULT_ROOT.glob("*__*.json")
                  if "reviewed" not in p.name]
        if not picked:
            pytest.skip("没有真实 P1 存档（output/pipeline/ 是产物目录，未入库）")
        return picked

    def test_真存档切得开且筛得完(self):
        from trajectory_pipeline.assembler import schema as _schema

        for path in self._archives():
            for sample in _schema.split_archive(path):
                screen = screen_d2(sample)
                assert screen.kind in ("screenable", "undetermined"), path.name
                for p in screen.points:
                    assert p.severity in SEVERITIES, path.name
                # 复原的步号必须严格递增，且在样本动作范围内
                assert len(screen.steps) <= len(sample.actions), path.name
                assert [d.step for d in screen.steps] == sorted(
                    d.step for d in screen.steps), path.name

    def test_无LLM时一律不出分(self):
        """没有语义判读就没有结论：代码筛再干净也不许直接发 5 分。"""
        from trajectory_pipeline.assembler import schema as _schema

        checked = 0
        for path in self._archives():
            for sample in _schema.split_archive(path):
                v = score_d2(sample)
                assert v.kind == "undetermined" and v.score is None, path.name
                checked += 1
        if not checked:
            pytest.skip("真实存档没切出样本")

    def test_判无断裂时筛得开的都出分(self):
        """真样本走完整两段：筛得开 → 假 LLM 判无误报 → 5 分；
        筛不开（事实残缺）→ 不出分。两头都不许静默。"""
        from trajectory_pipeline.assembler import schema as _schema

        checked = 0
        for path in self._archives():
            for sample in _schema.split_archive(path):
                screen = screen_d2(sample)
                llm = _LLM(_ok(len(screen.points)))
                v = score_d2(sample, llm_client=llm)
                if screen.kind == "undetermined":
                    assert v.kind == "undetermined" and v.score is None, path.name
                    continue
                assert v.kind == "scored" and v.score == 5, path.name
                checked += 1
        if not checked:
            pytest.skip("真实存档里没有可筛样本")

    def test_真存档里不出现凭据(self):
        """打分器只读不写，但它的 ``reason`` 会把 URL 原样带出来。
        若红线扫描器漏了某处，这条比单测更早暴露。"""
        from trajectory_pipeline.assembler import schema as _schema
        from trajectory_pipeline.executor.archive import scan_credentials

        for path in self._archives():
            for sample in _schema.split_archive(path):
                blob = repr(screen_d2(sample).to_json())
                assert not scan_credentials(blob), path.name