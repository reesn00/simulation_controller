"""感知层回放的测试。

回放的价值全在**它是不是真的在跑真实观察**，而不在代码行数。所以重点是：

1. **P1 字典 → ``Observation`` 不丢东西也不编东西**——回放喂进去的是
   存档原文，丢一个 ``video_tag_count`` 就等于把「这站判不判得出来」算反了。
2. **分歧形态里 ``None`` 与 ``False`` 必须分开**——「答了 False」和「判不出来」
   是两件事，合并之后 W1 的缺口会被算成「W1 判这站不行」，
   而真实情况是 W1 根本没判。
3. **不许算准确率**（见 replay 模块 docstring：rule 的基线本身 1/3 是错的）。
4. **不 import executor / assembler**：它把 P1 当数据文件读，不是流水线的一环。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from trajectory_pipeline.perception import replay
from trajectory_pipeline.perception.base import Decision, Observation, Q
from trajectory_pipeline.perception.replay import (
    Verdict, cases_of, observation_of, replay_archive, summarize,
)
from trajectory_pipeline.perception.rule_perceptor import RulePerceptor

REPLAY_SRC = Path(replay.__file__)


# ── 语料 ───────────────────────────────────────────────────────────

def _obs_json(url="https://a.test/x", **kw):
    base = {
        "url": url, "page_title": "标题", "body_text": "正文内容",
        "body_source": "inner_text", "truncated": False, "raw_len": 4,
        "stripped_ratio": 1.0, "degraded": [], "video_tag_count": 0,
        "iframe_count": 0, "interactive_elements": [], "links": [],
    }
    base.update(kw)
    return base


def _arc(**over):
    base = {
        "task_id": "T001", "title": "功夫", "query": "功夫 在线观看",
        "search_url": "https://s.test",
        "search_observation": _obs_json("https://s.test"),
        "search_blocked": "", "steps": [], "visits": [], "outcomes": [],
    }
    base.update(over)
    return base


def _write(tmp_path, arc, name="T001__abc12345.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
    return p


def _dec(answer, source="rule", evidence="e") -> Decision:
    return Decision(question=Q.PLAYER_OK, answer=answer, confidence=0.8,
                    evidence=evidence, source=source)


# ── 1 · 存档 → Observation ─────────────────────────────────────────

class TestObservationOf:

    def test_全文优先(self):
        o = observation_of(_obs_json(body_text="全文", body_preview="预览"))
        assert o.body_text == "全文"

    def test_老存档只有预览也认(self):
        # 老存档只有 400 字符 body_preview。全当空页面的话，
        # LLM 版会把「没采到」讲成一条通顺的错结论。
        o = observation_of({"url": "u", "body_preview": "预览正文"})
        assert o.body_text == "预览正文"

    def test_计数字段原样带过去(self):
        o = observation_of(_obs_json(video_tag_count=3, iframe_count=15))
        assert (o.video_tag_count, o.iframe_count) == (3, 15)

    def test_交互元素与链接重建(self):
        o = observation_of(_obs_json(
            interactive_elements=[{"ref": "e7", "tag": "button", "label": "播放"}],
            links=[{"text": "第1集", "href": "/ep1"}],
        ))
        assert o.interactive_elements[0].ref == "e7"
        assert o.interactive_elements[0].label == "播放"
        assert o.links[0].href == "/ep1"

    def test_缺字段不抛异常(self):
        # 存档残缺是 integrity 的活；叠在一起会让一份老存档直接跑不动
        o = observation_of({})
        assert o.url == "" and o.body_text == "" and o.video_tag_count == 0

    def test_degraded原样带过去(self):
        o = observation_of(_obs_json(degraded=["links_empty"]))
        assert o.degraded == ("links_empty",)


# ── 2 · 单元切分 ───────────────────────────────────────────────────

class TestCasesOf:

    def test_搜索观察问判断点一(self, tmp_path):
        cases = cases_of(_write(tmp_path, _arc()))
        assert [c.question for c in cases] == [Q.SELECT_PLAY_SITES]

    def test_站点观察问二三四(self, tmp_path):
        arc = _arc(visits=[{"url": "https://a.test/x", "landed_url": "https://a.test/x",
                            "success": False, "steps": [], "notes": [],
                            "site_obs": _obs_json(),
                            "player_obs": _obs_json(video_tag_count=1)}])
        got = [(c.question, c.unit) for c in cases_of(_write(tmp_path, arc))]
        assert got == [
            (Q.SELECT_PLAY_SITES, "search"),
            (Q.IS_REACHABLE, "site-1"),
            (Q.FIND_PLAY_CONTROL, "site-1"),
            (Q.PLAYER_OK, "site-1"),
        ]

    def test_没player_obs就不问判断点四(self, tmp_path):
        arc = _arc(visits=[{"url": "https://a.test/x", "landed_url": "https://a.test/x",
                            "success": False, "steps": [], "notes": [],
                            "site_obs": _obs_json(), "player_obs": None}])
        assert Q.PLAYER_OK not in [c.question for c in cases_of(_write(tmp_path, arc))]

    def test_被反爬拦的run零单元(self, tmp_path):
        # 拿验证码页去问模型，只会得到一条关于验证码页的「模型该怎么做」，
        # 看着通顺、毫无价值。
        arc = _arc(search_blocked="captcha", search_observation=_obs_json())
        assert cases_of(_write(tmp_path, arc)) == []

    def test_正文降级要标出来(self, tmp_path):
        obs = {k: v for k, v in _obs_json().items() if k != "body_text"}
        obs["body_preview"] = "只有预览"
        arc = _arc(search_observation=obs)
        assert cases_of(_write(tmp_path, arc))[0].body_degraded is True

    def test_只把真问过的那题记成recorded(self, tmp_path):
        arc = _arc(visits=[{"url": "https://a.test/x", "landed_url": "https://a.test/x",
                            "success": False, "steps": [], "notes": [],
                            "site_obs": _obs_json(), "player_obs": _obs_json()}],
                   outcomes=[{"url": "https://a.test/x",
                              "branch": "no_play_control",
                              "question": Q.FIND_PLAY_CONTROL, "source": "rule",
                              "evidence": "没有播放控件"}])
        by_q = {c.question: c for c in cases_of(_write(tmp_path, arc))}
        assert by_q[Q.FIND_PLAY_CONTROL].recorded_branch == "no_play_control"
        # 存档每个访问点只记最终 outcome 一个问题，其余题「没记录」≠「没问过」
        assert by_q[Q.IS_REACHABLE].recorded_question == ""

    def test_候选url与落地url不等仍能配上outcome(self, tmp_path):
        arc = _arc(visits=[{"url": "http://iqiyi.test/x",
                            "landed_url": "https://iqiyi.test/x",
                            "success": False, "steps": [], "notes": [],
                            "site_obs": _obs_json(), "player_obs": None}],
                   outcomes=[{"url": "https://iqiyi.test/x", "branch": "no_play_control",
                              "question": Q.FIND_PLAY_CONTROL, "source": "rule",
                              "evidence": "e"}])
        rec = [c.recorded_branch for c in cases_of(_write(tmp_path, arc))]
        assert rec.count("no_play_control") == 1


# ── 3 · 分歧形态 ───────────────────────────────────────────────────

def _cmp(answers):
    c = replay.Case(archive="a.json", task_id="T001", title="功夫", unit="site-1",
                    question=Q.PLAYER_OK, obs=Observation("", "", ""))
    return replay.Comparison(case=c, decisions=tuple(
        (f"p{i}", _dec(a)) for i, a in enumerate(answers)))


class TestVerdict:

    def test_答案相同是一致(self):
        assert _cmp([True, True]).verdict is Verdict.AGREE

    def test_答案不同是分歧(self):
        assert _cmp([True, False]).verdict is Verdict.DIVERGE

    def test_一个None一个True不是一致(self):
        # 「答了 False / True」与「判不出来」是两件事。合并之后
        # W1 的缺口会被算成「W1 判这站不行」，而真实情况是 W1 根本没判。
        assert _cmp([True, None]).verdict is Verdict.ONE_SILENT

    def test_两个None是都判不出来(self):
        assert _cmp([None, None]).verdict is Verdict.BOTH_SILENT

    def test_标签把None显式印出来不省略(self):
        assert _cmp([None, False]).label() == "p0=None  p1=False"

    def test_只看单边时恒为一致或沉默(self):
        # 单实现回放（默认的 rule-only）不该把「两方」当分歧
        assert _cmp([True]).verdict is Verdict.AGREE
        assert _cmp([None]).verdict is Verdict.BOTH_SILENT


# ── 4 · 回放 ───────────────────────────────────────────────────────

class TestReplay:

    def test_规则版单跑不需要任何后端(self, tmp_path):
        # 这是回放现在就能用起来的原因：--perceptor rule 完全离线
        comps = replay_archive(_write(tmp_path, _arc()), {"rule": RulePerceptor()})
        assert len(comps) == 1
        assert comps[0].verdict is Verdict.BOTH_SILENT   # ① 规则版判不出来

    def test_空实现表报错(self, tmp_path):
        with pytest.raises(ValueError):
            replay_archive(_write(tmp_path, _arc()), {})

    def test_片名不进decide(self, tmp_path):
        # decide 的签名只有 (question, obs)；片名在**构造期**绑进感知器。
        # 传进去会让两个实现的签名悄悄分叉。
        import inspect
        sig = inspect.signature(RulePerceptor.decide)
        assert list(sig.parameters) == ["self", "question", "obs"]


class TestSummarize:

    def test_统计答得出来的次数(self):
        comps = [
            _cmp([True, True]), _cmp([True, False]),   # p0/p1 都答了
            _cmp([True, None]),                        # 只有 p0 答了
            _cmp([None, None]),                        # 都没答
        ]
        s = summarize(comps)
        assert s.total == 4
        assert s.answered == {"p0": 3, "p1": 2}
        assert s.by_verdict["agree"] == 1
        assert s.by_verdict["diverge"] == 1
        assert s.by_verdict["one_silent"] == 1
        assert s.by_verdict["both_silent"] == 1

    def test_输出里必须留着没有准确率的警告(self):
        # 这段警告是本模块的**核心约束**（rule 基线本身 1/3 是假阳性）。
        # 它被删掉的时候没人会注意到，所以钉住。
        from trajectory_pipeline.perception.replay import format_summary
        out = format_summary(summarize([_cmp([True, None])]), ["p0", "p1"])
        assert "没有准确率" in out
        assert "人工标注集" in out

    def test_零单元的存档不炸(self):
        from trajectory_pipeline.perception.replay import format_summary
        out = format_summary(summarize([]), ["rule"])
        assert "没有可回放的单元" in out


# ── 5 · 边界纪律 ───────────────────────────────────────────────────

class TestNoPipelineImport:
    """回放把 P1 当**数据文件**读，不是流水线的一环。

    import 了 executor 就等于让感知层知道控制流的存在，而那正是
    「插件退化成耦合」的第一步；import 了 assembler 则会让回放的
    降级口径跟着 P2 一起漂，而它本来就该独立看存档。
    """

    @pytest.mark.parametrize("forbidden", ["executor", "assembler", "taskgen"])
    def test_不import(self, forbidden):
        tree = ast.parse(REPLAY_SRC.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(forbidden in n for n in names), names


class TestAgainstRealArchives:
    """有真实存档时跑一遍——**这是唯一能验「字段名没猜错」的测试**。

    离线 fixture 是我们自己造的，规则和 fixture 一起错，测试照样绿
    （CLAUDE.md 明写这条）。真实存档里的键名与本模块假设的一致，
    才说明回放真的读得懂 P1。产物目录不入库，没有就 skip。
    """

    def test_读真实存档(self):
        from trajectory_pipeline.executor.archive import DEFAULT_ROOT

        picked = [p for p in DEFAULT_ROOT.glob("*__*.json") if "reviewed" not in p.name]
        if not picked:
            pytest.skip("没有真实 P1 存档（output/pipeline/ 是产物目录，未入库）")
        cases = cases_of(picked[0])
        assert cases
        for c in cases:
            assert c.obs.url, f"{c.unit} 的观察连 url 都没有——字段名猜错了？"
            assert isinstance(c.obs.video_tag_count, int)
        # 规则版在真实数据上答得出几道，是现在最该知道的数；
        # 这里只钉住「跑得完 + 不全灭也不全活」这个形状
        comps = replay_archive(picked[0], {"rule": RulePerceptor()})
        s = summarize(comps)
        assert 0 <= s.answered.get("rule", 0) <= s.total