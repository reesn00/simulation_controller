"""D-2 打分器（``evaluation/scorers/code_scorer.py``）单测。

**这个模块存在的全部理由是「不判反」**。它要区分的三种情形在存档里
长得一模一样或极其相似：

    页面真没有这个元素  |  元素落在裁剪区外  |  这一项根本没采到

前一种是轨迹的缺陷（扣分），后两种是采集的缺陷（**不扣分，先修采集器**）。
把这三者混为一谈，得到的是一个「看起来很严谨」的分数，而它把采集器
的问题记在了数据头上——而那批数据正是用来改进采集器的。

所以本文件的重点不是「打分算得对」，是**不可判定的分支够不够多**。
"""

from __future__ import annotations

from typing import Any

import pytest

from trajectory_pipeline.assembler import schema
from trajectory_pipeline.assembler.schema import ActionView, GateMark, Sample
from trajectory_pipeline.evaluation.scorers.code_scorer import (
    LABEL_TRUNCATED_AT,
    judge_ref,
    score_d2,
)


# ═══════════════════════════════════════════════════════════════════════
# 语料
# ═══════════════════════════════════════════════════════════════════════


def _obs(**kw) -> dict[str, Any]:
    """默认是一份**完整、无降级、未裁剪**的观察。

    这三点默认值是刻意的：它们是「可以判『不存在』」的前提。
    默认就带降级的话，所有「missing」用例都会被不可判定吞掉，
    而那条分支正是最该被单独测出来的。
    """
    base = {
        "url": "https://x.test/movie", "page_title": "功夫",
        "body_text": "电影详情", "degraded": [], "truncated": False,
        "raw_len": 4, "max_chars": None,
        "video_tag_count": 0, "iframe_count": 0,
        "elements_total": 0, "links_total": 0,
        "interactive_elements": [], "links": [],
    }
    base.update(kw)
    if not base["elements_total"]:
        base["elements_total"] = len(base["interactive_elements"])
    if not base["links_total"]:
        base["links_total"] = len(base["links"])
    return base


def _el(ref: str, tag: str, label: str) -> dict[str, str]:
    return {"ref": ref, "tag": tag, "label": label}


def _click(label: str, tag: str = "button", origin: str = "model") -> ActionView:
    return ActionView(tool="click", params={}, origin=origin,
                      target={"tag": tag, "label": label})


def _sample(*actions: ActionView, observations=(), **kw) -> Sample:
    base = dict(
        sample_id="T001__a__site-1", task_id="T001", unit="site-1",
        title="功夫", system_prompt=schema.SYSTEM_PROMPT, tools=schema.TOOL_SPECS,
        user_prompt="有没有能看正片的", provenance={"genre": "喜剧"},
        actions=tuple(actions), observations=tuple(observations),
        rationale=None, gate=GateMark(status="not_run", reasons=("未跑",)),
        outcome=None,
    )
    base.update(kw)
    return Sample(**base)


# ═══════════════════════════════════════════════════════════════════════
# 单条引用
# ═══════════════════════════════════════════════════════════════════════


class TestJudgeRef:
    def test_命中唯一元素(self):
        obs = _obs(interactive_elements=[_el("e5", "button", "立即播放")])
        f = judge_ref("立即播放", "button", [obs])
        assert f.kind == "found"

    def test_同名两处判含糊(self):
        """``ActionView`` **刻意不带 ref**（会话内句柄进训练数据等于让模型
        学随机数），所以同名即无法指明——rubric 的 4/3 分正是这个。"""
        obs = _obs(interactive_elements=[_el("e5", "button", "播放"),
                                        _el("e9", "a", "播放")])
        assert judge_ref("播放", "button", [obs]).kind == "ambiguous"

    def test_完整采集且查不到判不存在(self):
        """**只有**观察完整时才能这么判。这是本模块最容易写错的一处：
        把它默认掉，全批都会变成 1 分，且分数「看起来很严谨」。"""
        assert judge_ref("立即播放", "button", [_obs()]).kind == "missing"

    def test_tag不符不改变判定(self):
        """tag 来自 DOM 预处理，换一次渲染就可能变；label 一致且唯一时
        引用并没有歧义。只记一笔，不扣分。"""
        obs = _obs(interactive_elements=[_el("e5", "a", "立即播放")])
        f = judge_ref("立即播放", "button", [obs])
        assert f.kind == "found" and "tag" in f.reason

    def test_空label判不可判定而非不存在(self):
        """``target=None`` 是执行器没解出目标（I6 存疑），
        那是存档完整性问题——方向与「引用了不存在的东西」相反。"""
        assert judge_ref("", "", [_obs()]).kind == "undetermined"

    def test_超长label按截断后比对(self):
        """动作里是**完整** label，存档里只留前 120 字。
        方向搞反（全等比全长）会把一条真实存在的引用判成不存在。"""
        long_label = "立即" + "观看" * 100          # > 120 字
        obs = _obs(interactive_elements=[
            _el("e5", "button", long_label[:LABEL_TRUNCATED_AT])])
        assert judge_ref(long_label, "button", [obs]).kind == "found"


class TestUndetermined:
    """不可判定的**五个入口**。少一个就有一条路径会判反。"""

    def test_元素被裁剪(self):
        """变更 B 存在的全部理由：没有 ``elements_total`` 就分不出
        「被裁掉」与「不存在」。"""
        obs = _obs(interactive_elements=[_el("e5", "button", "立即播放")],
                   elements_total=300)
        f = judge_ref("某个被裁掉的按钮", "button", [obs])
        assert f.kind == "undetermined"
        assert "裁剪" in f.reason

    def test_链接被裁剪也算(self):
        """判定依据是**这一份观察能不能支撑**「不存在」，
        元素够不够不是唯一条件。"""
        obs = _obs(links=[{"text": "a", "href": "https://x.test/a"}],
                   links_total=900)
        assert judge_ref("某个被裁掉的链接", "a", [obs]).kind == "undetermined"

    def test_采集降级(self):
        """iqiyi/ixigua/sohu 三站的实测形态：正文 0 字 + 元素 0 个。
        驱动层记了 ``degraded`` 才没被当成「页面真没有控件」。"""
        obs = _obs(degraded=["browser_interactive_elements"])
        assert judge_ref("立即播放", "button", [obs]).kind == "undetermined"

    def test_正文被截断(self):
        assert judge_ref("立即播放", "button",
                         [_obs(truncated=True)]).kind == "undetermined"

    def test_观察三项全空(self):
        obs = _obs(body_text="")
        assert judge_ref("立即播放", "button", [obs]).kind == "undetermined"

    def test_理由必须点名是哪个观察(self):
        obs = _obs(url="https://x.test/play/e5", degraded=["browser_evaluate"])
        f = judge_ref("立即播放", "button", [obs])
        assert "https://x.test/play/e5" in f.reason, \
            "读报告的人只有看到是哪一页，才能去改那一页的采集"

    def test_任一观察完整且命中即为found(self):
        """站点页被裁、播放页完整且命中 → 不是不可判定。
        否则「站点页元素多」会把每条都拖成不可判定。"""
        site = _obs(url="https://x.test/movie", elements_total=300)
        player = _obs(url="https://x.test/play/e5",
                      interactive_elements=[_el("e5", "button", "立即播放")])
        assert judge_ref("立即播放", "button", [site, player]).kind == "found"

    def test_同一控件出现在两份观察里不算含糊(self):
        """点击前后快照里 ``ref=e5`` 都在（控件没换页时很常见）。
        不按 ref 去重就会把「命中一条」数成两条同名 → 误判 4 分 / 3 分。"""
        site = _obs(url="https://x.test/movie",
                    interactive_elements=[_el("e5", "button", "立即播放")])
        player = _obs(url="https://x.test/play/e5",
                      interactive_elements=[_el("e5", "button", "立即播放")])
        assert judge_ref("立即播放", "button", [site, player]).kind == "found"

    def test_两份观察里的不同ref同名仍是含糊(self):
        """去重按 ref，不按 label——两条**真的不同**的元素同名，
        模型无从指明（``ActionView`` 刻意不带 ref）。"""
        site = _obs(url="https://x.test/movie",
                    interactive_elements=[_el("e5", "button", "播放")])
        player = _obs(url="https://x.test/play/e5",
                      interactive_elements=[_el("e7", "a", "播放")])
        assert judge_ref("播放", "button", [site, player]).kind == "ambiguous"


# ═══════════════════════════════════════════════════════════════════════
# 打分
# ═══════════════════════════════════════════════════════════════════════


class TestScoring:
    OBS = [_obs(interactive_elements=[_el("e5", "button", "立即播放"),
                                     _el("e9", "a", "播放"),
                                     _el("e10", "a", "播放")])]

    def _v(self, *labels):
        return score_d2(_sample(*[_click(x) for x in labels],
                                observations=self.OBS))

    def test_全部命中得5(self):
        assert self._v("立即播放").score == 5

    def test_一处含糊得4(self):
        assert self._v("立即播放", "播放").score == 4

    def test_两处含糊得3(self):
        assert self._v("立即播放", "播放", "播放").score == 3

    def test_一处不存在得2(self):
        assert self._v("立即播放", "分享").score == 2

    def test_两处不存在得1(self):
        assert self._v("立即播放", "分享", "举报").score == 1

    def test_不存在优先于含糊(self):
        """混在一起时先报含糊，那条**真缺失**就消失了——
        而它是唯一一条需要人去查的。"""
        assert self._v("播放", "分享").score == 2

    def test_kind标为scored(self):
        assert self._v("立即播放").kind == "scored"

    def test_可序列化(self):
        import json

        json.dumps(self._v("立即播放", "分享").to_json(), ensure_ascii=False)


class TestNoScoreWhenUndetermined:
    def test_无可校验引用不出分(self):
        """**「没查」不等于「全对」**。只导航就被拦下的样本没有任何
        ``click``，给它 5 分等于凭空白发了通行证——
        与 ``gate.status`` 三值同源（见 code_scorer 模块 docstring）。"""
        v = score_d2(_sample(observations=[_obs()]))
        assert v.score is None and v.kind == "undetermined"

    def test_全是new_tab的样本不出分(self):
        from trajectory_pipeline.assembler.schema import ActionView

        v = score_d2(_sample(ActionView(tool="new_tab", params={"url": "u"},
                                        origin="infrastructure"),
                             observations=[_obs()]))
        assert v.score is None

    def test_只有不可判定项时不出分(self):
        obs = _obs(elements_total=300)
        v = score_d2(_sample(_click("被裁掉的按钮"), observations=[obs]))
        assert v.score is None and v.kind == "undetermined"

    def test_完整观察能定案时不被裁剪拖成不可判定(self):
        """``site_obs`` 完整、``player_obs`` 被裁：站点页没这一条**已经是结论**，
        裁剪不能把它拖成不可判定——否则一个完整观察就能让整条样本失去分数，
        而 D-2 的备注要的恰恰相反（不可判定多了要改采集，不是扣轨迹）。"""
        site = _obs(url="https://x.test/movie",
                    interactive_elements=[_el("e5", "button", "立即播放")])
        player = _obs(url="https://x.test/play/e5", elements_total=300)
        v = score_d2(_sample(_click("立即播放"), _click("分享"),
                             observations=[site, player]))
        assert v.kind == "scored" and v.score == 2

    def test_缺失项要写明哪些观察没能作证(self):
        """结论是 `missing` 不代表「全都查过了」。被裁的那一份没参与作证，
        不写出来的话读的人会以为这是无懈可击的否定。"""
        site = _obs(url="https://x.test/movie",
                    interactive_elements=[_el("e5", "button", "立即播放")])
        player = _obs(url="https://x.test/play/e5", elements_total=300)
        v = score_d2(_sample(_click("立即播放"), _click("分享"),
                             observations=[site, player]))
        assert any("裁剪" in r.reason for r in v.refs if r.kind == "missing")

    def test_命中与不可判定混合时不出5分(self):
        """「全部实体可找到」是 5 分的定义。有 1 条**没能判定**，
        「全部」就不成立——出 5 分等于给一条没查完的数据发通行证。"""
        obs = _obs(interactive_elements=[_el("e5", "button", "立即播放")],
                   elements_total=300)
        v = score_d2(_sample(_click("立即播放"), _click("某个按钮"),
                             observations=[obs]))
        assert v.score is None and v.kind == "undetermined"
        assert v.checked == 1, "命中那条仍然算查过了"

    def test_理由指向该修采集而非该扣轨迹(self):
        obs = _obs(elements_total=300)
        v = score_d2(_sample(_click("某按钮"), observations=[obs]))
        assert any("修采集" in r for r in v.reasons)


class TestGotoIsOutOfScope:
    """``goto`` 的 URL **不在本模块校验**。

    它要核的「这个 URL 在搜索页链接里吗」需要**另一条样本**的观察
    （决策单元切分的结果，见 ``assembler/schema.py``）。在本样本内查它
    必然查不到，于是每条 ``goto`` 都记一条不存在，全批恒 1 分。
    恒定分数没有区分力——这也是 D-1 被划到批次级 B-1 的同一个理由。
    """

    def test_只有goto的样本不出分(self):
        from trajectory_pipeline.assembler.schema import ActionView

        v = score_d2(_sample(
            ActionView(tool="goto", params={"url": "https://www.baidu.com/s?wd=x"}),
            ActionView(tool="goto", params={"url": "https://x.test/movie"}),
            observations=[_obs()]))
        assert v.score is None

    def test_goto不参与missing计数(self):
        from trajectory_pipeline.assembler.schema import ActionView

        v = score_d2(_sample(
            ActionView(tool="goto", params={"url": "https://elsewhere.test/"}),
            _click("立即播放"), observations=[self_obs()]))
        assert v.score == 5

    def test_new_tab不计入模型动作(self):
        """``new_tab`` 是执行器的会话隔离动作（``origin="infrastructure"``），
        拿它的行为去扣模型的分是张冠李戴。"""
        from trajectory_pipeline.assembler.schema import ActionView

        v = score_d2(_sample(
            ActionView(tool="new_tab", params={"url": "u"},
                       origin="infrastructure"),
            _click("立即播放"), observations=[self_obs()]))
        assert v.score == 5
        assert v.checked == 1


def self_obs():
    return _obs(interactive_elements=[_el("e5", "button", "立即播放")])


# ═══════════════════════════════════════════════════════════════════════
# 真实存档
#
# 离线 fixture 是我们自己造的，规则和 fixture 一起错，测试照样绿
# （CLAUDE.md 明写这条）。这里唯一要证明的是「打分器读得懂真 P1 的形状」——
# 键名猜错时它会静默地判「查不到」，而那正是 D-2 最不该出现的输出。
# 产物目录不入库，没有就 skip。
# ═══════════════════════════════════════════════════════════════════════


class TestOnRealArchives:
    def _archives(self):
        from trajectory_pipeline.executor.archive import DEFAULT_ROOT

        picked = [p for p in DEFAULT_ROOT.glob("*__*.json")
                  if "reviewed" not in p.name]
        if not picked:
            pytest.skip("没有真实 P1 存档（output/pipeline/ 是产物目录，未入库）")
        return picked

    def test_真存档切得开且打分器跑得完(self):
        from trajectory_pipeline.assembler import schema

        for path in self._archives():
            for sample in schema.split_archive(path):
                verdict = score_d2(sample)
                assert verdict.kind in ("scored", "undetermined"), path.name
                assert verdict.score is None or 1 <= verdict.score <= 5, path.name

    def test_老存档没有动作流时不可判定而非满分(self):
        """现存真实存档全部跑在 ``steps[]`` 落地**之前**。它们没有任何
        模型动作——判 5 分等于凭空白发了通行证，而那批数据正是要用来
        补动作流的，不能因为「看起来干净」就当通过了。"""
        from trajectory_pipeline.assembler import schema

        scored = []
        for path in self._archives():
            for sample in schema.split_archive(path):
                if sample.degraded_from and "steps" in sample.degraded_from:
                    scored.append(score_d2(sample))
        if not scored:
            pytest.skip("现有真实存档都已带 steps[]，没有老形状可验")
        assert all(v.kind == "undetermined" for v in scored)
        assert all(v.score is None for v in scored)

    def test_真存档里不出现凭据(self):
        """打分器只读不写，但它的 ``reason`` 会把观察里的 label 原样带出来。
        若红线扫描器漏了某处，这条比单测更早暴露。"""
        from trajectory_pipeline.assembler import schema
        from trajectory_pipeline.executor.archive import scan_credentials

        for path in self._archives():
            for sample in schema.split_archive(path):
                blob = repr(score_d2(sample).to_json())
                assert not scan_credentials(blob), path.name