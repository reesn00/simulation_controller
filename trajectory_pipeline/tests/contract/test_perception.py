"""模块 3 的**契约测试**——七不变式。

与 ``tests/unit/`` 的分工：
    unit/         测某个函数在这个输入下返回什么
    contract/     测**任何** Perceptor 实现（含将来的 LLMPerceptor）都必须满足什么

所以这里的夹具是**参数化**的：今天只灌 :class:`RulePerceptor`，
W3 写完 :class:`LLMPerceptor` 后往 ``IMPLEMENTATIONS`` 里加一行，
八条不变式自动对它生效——这是「切实现时 executor 的 git diff 必须为空」
的前提：不变式由测试强制，而不是靠自觉。

七不变式（方案 01 号文档 §3）：
    I1 evidence 可溯源 / I2 confidence∈[0,1] / I3 幂等 / I4 fail-closed
    I5 无副作用 / I6 decision 型题 payload 完整且 ref 可溯源 / I7 answer=True 时 payload 非空
"""

from __future__ import annotations

import copy
import json

import pytest

from trajectory_pipeline.perception import questions
from trajectory_pipeline.perception.base import Decision, InteractiveElement, Observation, Q
from trajectory_pipeline.perception.rule_perceptor import (
    RulePerceptor,
    classify_trailer,
    is_trailer_only,
    is_trailer_suspect,
)
from trajectory_pipeline.tests.fakes import llm_perceptor

#: 契约适用的实现。
#:
#: LLM 版接的是**假后端**（``tests/fakes.py``）——契约测试绝不打真网络：
#: 它要的是七不变式**每次都成立**，而真后端会超时、限流、换版本漂输出，
#: 那些都会让门禁变成随机失败。LLM 的语义能力由真实验证负责，
#: 见 ``output/pipeline/llm_probe.py``。
#:
#: 注意 ``functools.partial`` 而不是裸类：契约测试用 ``impl()`` 无参构造，
#: 而 ``LLMPerceptor`` 需要一个 client。
IMPLEMENTATIONS = [
    pytest.param(RulePerceptor, id="rule"),
    pytest.param(lambda: llm_perceptor(), id="llm"),
]

ALL_QUESTIONS = list(questions.REGISTRY)


def obs(**kwargs) -> Observation:
    """构造观察。默认给一个「有内容但无播放控件」的页面。"""
    base = dict(url="https://x.test/", page_title="T", body_text="正文")
    base.update(kwargs)
    return Observation(**base)


def elements(*pairs: tuple[str, str, str]) -> tuple[InteractiveElement, ...]:
    return tuple(InteractiveElement(ref=r, tag=t, label=l) for r, t, l in pairs)


# ═══════════════════════════════════════════════════════════════════════
# 注册表本身的契约
# ═══════════════════════════════════════════════════════════════════════


class TestRegistry:
    def test_每道题都绑定了负分支(self):
        """不加这道约束，「我判 false 归哪支」就会散进控制流然后漏掉一支。"""
        for qid, spec in questions.REGISTRY.items():
            assert spec.negative_branch, f"{qid} 未绑定 negative_branch"
            assert spec.id == qid, f"注册表 key 与 Question.id 不一致: {qid}"

    def test_失败分支全集是八条(self):
        assert questions.all_branches() == {
            "not_play_site", "login_wall_or_blocked", "no_play_control",
            "component_unverified",                       # 四道题各一支
            "unreachable_hard", "trailer_only",           # 代码判定
            "trailer_suspect", "unresolved",              # 兜底
        }

    def test_未知_id_抛_KeyError(self):
        """返回 None 会让调用方的 `if q is None` 把「拼错 id」当成
        「这题不需要判定」静默跳过——最难查的一类 bug。"""
        with pytest.raises(KeyError):
            questions.get("不存在的题")

    def test_置信阈值在合法区间(self):
        for spec in questions.REGISTRY.values():
            assert 0.0 <= spec.confidence_threshold <= 1.0


# ═══════════════════════════════════════════════════════════════════════
# 七不变式 —— 对所有实现强制
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("impl", IMPLEMENTATIONS)
class TestInvariants:
    def test_i1_evidence_可溯源(self, impl):
        """evidence 必须指向**具体判据**（ref / 数量 / 词表命中），
        而不是「我认为」。空 evidence 或复述答案的一律不合格。"""
        cases = [
            (Q.FIND_PLAY_CONTROL, obs(interactive_elements=elements(("e1", "a", "播放")))),
            (Q.FIND_PLAY_CONTROL, obs(interactive_elements=elements(("e1", "a", "首页")))),
            (Q.PLAYER_OK, obs(video_tag_count=1)),
            (Q.PLAYER_OK, obs(video_tag_count=0, iframe_count=0)),
        ]
        for qid, o in cases:
            d = impl().decide(qid, o)
            assert d.evidence.strip(), f"{qid} evidence 为空"
            assert len(d.evidence) > 5, f"{qid} evidence 过短，不足以溯源"

    def test_i2_confidence_在零一之间(self, impl):
        for qid in ALL_QUESTIONS:
            for o in (obs(), obs(interactive_elements=elements(("e1", "a", "播放"))),
                      obs(video_tag_count=3, iframe_count=1)):
                c = impl().decide(qid, o).confidence
                assert 0.0 <= c <= 1.0, f"{qid} confidence 越界: {c}"

    def test_i3_幂等(self, impl):
        """同一 (question, obs) 重复判定必须同答案。

        不幂等 = 判分不可复现 = 同一份素材两次评测两个结论。
        """
        cases = [
            (Q.FIND_PLAY_CONTROL, obs(interactive_elements=elements(
                ("e1", "a", "预告片"), ("e2", "a", "正片")))),
            (Q.PLAYER_OK, obs(video_tag_count=2)),
        ]
        for qid, o in cases:
            answers = {impl().decide(qid, o).answer for _ in range(5)}
            assert len(answers) == 1, f"{qid} 不幂等: {answers}"

    def test_i4_能力不可用时_answer_为_none_而非_false(self, impl):
        """**False 是一个结论**，说「这里没有播放控件」；
        None 是「我判不出来」。把二者合并会让负样本池被不确定性污染，
        且这种污染在报表上看不出来（都表现为「判了 false」）。

        注意前提：这里的「不可用」指的是**采集降级**。
        「采集成功 + 页面上确实 0 个交互元素」是**确定性事实**，判 False 合法，
        见下面的 ``test_i4_采集成功时判_false_是合法结论``。
        """
        for qid in ALL_QUESTIONS:
            o = obs(degraded=("browser_interactive_elements", "browser_links"))
            d = impl().decide(qid, o)
            assert d.answer is not False, (
                f"{qid} 在采集降级下判了 False——应 fail-closed 返回 None"
            )

    def test_i4_采集成功时判_false_是合法结论(self, impl):
        """反例护栏：**别把 I4 读成「永不判 False」**。
        采集成功且页面确实没有播放控件时，False 是货真价实的结论，
        强行返回 None 会让负样本池永远空——那不是 fail-closed，是躺平。"""
        d = impl().decide(
            Q.FIND_PLAY_CONTROL,
            obs(interactive_elements=elements(("e1", "a", "首页"), ("e2", "a", "登录"))),
        )
        assert d.answer is False
        assert questions.negative_branch_of(Q.FIND_PLAY_CONTROL) == "no_play_control"

    def test_i5_无副作用(self, impl):
        """decide 不得改动传入的 obs（Observation 是 frozen，但内部对象
        仍可能被就地改）。"""
        o = obs(interactive_elements=elements(("e1", "a", "播放")),
                links=(), video_tag_count=1)
        before = copy.deepcopy(o)
        p = impl()
        for qid in ALL_QUESTIONS:
            p.decide(qid, o)
        assert o == before, f"decide 改动了传入的 Observation（{qid}）"

    def test_i6_decision_型题_payload_完整且_ref_可溯源(self, impl):
        """decision 型题（① ③）必须给出 ref，且该 ref 必须**真的存在于**
        obs 里——否则代码拿着它去 click 会点空或点错。"""
        for qid, spec in questions.REGISTRY.items():
            if spec.answer_type != "decision":
                continue
            o = obs(interactive_elements=elements(
                ("e1", "a", "首页"), ("e7", "button", "立即播放")))
            d = impl().decide(qid, o)
            if not d.answer:
                continue
            ref = d.payload.get("ref", "")
            if not ref:
                continue                       # trailer_only 场景本就无 ref
            known = {e.ref for e in o.interactive_elements}
            assert ref in known, f"{qid} 回传的 ref={ref!r} 不在观察里（已知 {known}）"

    def test_i7_answer_true_时_payload_非空(self, impl):
        """answer=True 必须带 payload——控制流要靠它行动
        （① 要 url 列表，③ 要 ref，④ 要 media_count）。"""
        for qid in ALL_QUESTIONS:
            o = obs(interactive_elements=elements(("e7", "button", "立即播放")),
                    video_tag_count=1, iframe_count=1)
            d = impl().decide(qid, o)
            if d.answer is True:
                assert dict(d.payload), f"{qid} answer=True 但 payload 为空"

    def test_从不向调用方抛异常(self, impl):
        """契约要求失败一律转 answer=None。上层控制流没有 try/except，
        感知层抛异常会直接把整批运行打断。"""
        p = impl()
        for qid in ALL_QUESTIONS:
            for o in (obs(), obs(body_text=""), obs(video_tag_count=-1)):
                d = p.decide(qid, o)
                assert isinstance(d, Decision)


# ═══════════════════════════════════════════════════════════════════════
# 业务规则：预告片（用户明确的判据）
# ═══════════════════════════════════════════════════════════════════════


class TestTrailerRule:
    """用户业务规则原文：播放按钮或剧集按钮上的文本是「预告片」时，
    说明播放资源不存在。"""

    @pytest.mark.parametrize("label", [
        "预告片", "预告", "抢先看", "影视预告", "第 1 集 预告片",
        "预告视频", "花絮", "片花", "Trailer", "official trailer",
        "TEASER", "Preview", "先导片",
    ])
    def test_命中(self, label):
        assert is_trailer_only(label) is True

    @pytest.mark.parametrize("label", [
        "播放", "立即播放", "正片", "第 3 集", "免费观看", "在线观看",
        "高清", "抢先看正片", "预告 · 第 1 季 正片", "", "登录",
    ])
    def test_未命中(self, label):
        assert is_trailer_only(label) is False

    def test_预告片_误杀边界(self):
        """「预告」二字可能出现在**非预告资源**的按钮上。
        误杀代价高于漏杀：漏杀只是多走一次播放页拿到 component_unverified，
        误杀是把一条正样本标成 trailer_only 直接丢弃。"""
        # 正片旁证优先于预告词——这是第一道防线
        assert is_trailer_only("预告 · 正片") is False
        assert is_trailer_only("抢先看正片") is False

    def test_解说页链接判_suspect_而非_only(self):
        """已知的能力上限：「观看 trailer 解析」这类**解说页链接**，在词表层
        与真正的预告控件无法区分（都是「含 trailer 二字的短文本」）。

        刻意**不堆排除词表**——那只会把一种误判换成另一种。判成 ``suspect``
        交人工兜底（失败分支表的 ``trailer_suspect``），W3 由 LLM 判语义。"""
        assert classify_trailer("观看 trailer 解析") == "suspect"
        assert is_trailer_suspect("观看 trailer 解析") is True
        assert is_trailer_only("观看 trailer 解析") is False

    def test_整体即预告词判_only(self):
        assert classify_trailer("预告片") == "only"
        assert classify_trailer("Trailer") == "only"
        assert classify_trailer("第 1 集 预告片") == "only"     # 修饰词不算实义残留
        assert classify_trailer("预告 · 正片") == "none"

    def test_全预告时返回_trailer_only_而不误杀(self):
        """候选里混着预告和正片时必须选正片——反过来会把好站点误杀。"""
        p = RulePerceptor()
        d = p.decide(Q.FIND_PLAY_CONTROL, obs(interactive_elements=elements(
            ("e1", "a", "预告片"), ("e2", "a", "第 1 季 正片"))))
        assert d.answer is True
        assert d.payload["trailer_only"] is False
        assert d.payload["ref"] == "e2"

    def test_只有预告时标_trailer_only(self):
        p = RulePerceptor()
        d = p.decide(Q.FIND_PLAY_CONTROL, obs(interactive_elements=elements(
            ("e1", "a", "预告片"), ("e2", "button", "抢先看"))))
        assert d.answer is True
        assert d.payload["trailer_only"] is True
        assert d.payload["ref"] == ""       # 无正片控件，代码不该去点预告

    def test_播放历史不算播放控件(self):
        p = RulePerceptor()
        d = p.decide(Q.FIND_PLAY_CONTROL, obs(interactive_elements=elements(
            ("e1", "a", "播放历史"), ("e2", "a", "观看记录"))))
        assert d.answer is False             # 这是真结论：页面确实没有播放入口
        assert questions.negative_branch_of(Q.FIND_PLAY_CONTROL) == "no_play_control"


# ═══════════════════════════════════════════════════════════════════════
# 真实站点回归：2026-10-08 端到端实测出的两个假阳性
# ═══════════════════════════════════════════════════════════════════════


class TestRealSiteRegressions:
    """下面两条都是**真实跑出来的**，不是推演出来的。

    假阳性的危害比假阴性大：它会进 P1 存档、被当成「规则版判对了」，
    之后所有基于这批素材的统计基线都建在错的数上。
    """

    def test_星期四不算剧集控件(self):
        """hao123 首页的「星**期**四」曾被判成剧集项。

        早期版本用单字词表 ``("集","话","期","章","回")``，而这五个字在中文
        日常用语里密度高到毫无区分度（星期 / 期间 / 对话 / 机会）。
        剧集项必须带结构——「第 N 集」或 ``EP01`` / ``S01E02`` 编号形态。"""
        for label in ("星期四", "每日期间", "对话", "机会", "返回首页"):
            p = RulePerceptor()
            d = p.decide(Q.FIND_PLAY_CONTROL, obs(interactive_elements=elements(
                ("e1", "a", label))))
            assert d.answer is False, f"{label!r} 被误判成播放控件"

    def test_带结构的剧集项仍要命中(self):
        """收紧不是把召回一起砍掉——有编号形态的剧集项仍应命中。
        「全24集」与「全 24 集」两种写法都要过：UI 文案里都常见，
        正则里量词之间写死相邻会在带空格的那一半上直接漏掉。"""
        for label in ("第 12 集", "第3话", "全 24 集", "全24集", "正片",
                      "连载至 30 集", "第 1 季 正片", "EP05", "S01E02"):
            p = RulePerceptor()
            d = p.decide(Q.FIND_PLAY_CONTROL, obs(interactive_elements=elements(
                ("e1", "a", label))))
            assert d.answer is True, f"{label!r} 未命中播放控件"

    def test_导航站的满屏iframe_不算播放器(self):
        """hao123（纯导航站）实测 ``iframe_count=15``，曾被判成播放页。

        iframe 可以是广告 / 埋点 / 地图 / 天气，**数量与「是不是播放器」无关**。
        真要判得看 src 与尺寸，那是启发式阈值——拿不准的东西不该当判据。"""
        d = RulePerceptor().decide(Q.PLAYER_OK, obs(video_tag_count=0, iframe_count=15))
        assert d.answer is None, "只有 iframe 时判 True 就是把导航站当播放页"
        assert d.payload["iframe_count"] == 15      # 诊断信息仍然留着
        assert d.payload["media_count"] == 0

    def test_有video标签时判_true(self):
        d = RulePerceptor().decide(Q.PLAYER_OK, obs(video_tag_count=1, iframe_count=15))
        assert d.answer is True

    def test_零个交互元素不判无播放控件(self):
        """实测 iqiyi / ixigua / sohu 三站都是「正文 0 字符 + 元素 0 个」。

        obscura 对空页面返回**哨兵文本** ``No interactive elements on this
        page.``——不是错误，所以 :attr:`Observation.degraded` 抓不到它。
        而视频站的播放按钮几乎必然是 JS 渲染的，采到 0 个元素的绝大多数
        情况是「页面还没渲染完」。把它判成 False，三站全被写成
        ``no_play_control`` 负样本——**采集失败被写成了业务结论**。
        """
        d = RulePerceptor().decide(Q.FIND_PLAY_CONTROL, obs(
            interactive_elements=(), body_text=""))
        assert d.answer is None, "0 个元素被当成了「页面确实没有播放控件」"
        assert d.fallback_used is True

    def test_有元素但没命中才是真负样本(self):
        """与上一条互为对照：不能把「判 False」这条路整个封死。
        采到了元素、只是都不像播放控件——那才是货真价实的 no_play_control。"""
        d = RulePerceptor().decide(Q.FIND_PLAY_CONTROL, obs(
            interactive_elements=elements(("e1", "a", "登录"), ("e2", "a", "注册")),
            body_text="请登录"))
        assert d.answer is False

    def test_页面已有video时不判无播放控件(self):
        """实测 tv.sohu.com：title=「功夫 - 搜狐视频」、``video_tag_count=1``，
        200 个元素没一个命中词表，于是被写成 no_play_control——
        一个**能看**的站点进了负样本池。

        负样本池的全部价值是「这里真的看不了」。页面自带 ``<video>`` 时，
        「没有播放控件」这句话与观察自相矛盾，只能说「按钮没认出来」。"""
        d = RulePerceptor().decide(Q.FIND_PLAY_CONTROL, obs(
            interactive_elements=elements(
                ("e1", "a", "首页"), ("e2", "a", "登录"), ("e3", "a", "下载客户端")),
            video_tag_count=1))
        assert d.answer is None, "有 <video> 的页面被判成「没有播放控件」"
        assert d.fallback_used is True

    def test_没有video时_no_play_control_照旧(self):
        """与上一条互为对照：没有 video 佐证时，真负样本还得能出得来。"""
        d = RulePerceptor().decide(Q.FIND_PLAY_CONTROL, obs(
            interactive_elements=elements(("e1", "a", "首页"), ("e2", "a", "登录")),
            video_tag_count=0))
        assert d.answer is False
        assert d.fallback_used is False


# ═══════════════════════════════════════════════════════════════════════
# W1 的能力边界必须显式可见
# ═══════════════════════════════════════════════════════════════════════


class TestW1CapabilityBoundary:
    def test_语义题_fail_closed(self):
        """W1 规则版不具备语义能力，必须返回 None。
        真的判了 True/False 才是 bug——那会造出有代码背书的假信号。"""
        p = RulePerceptor()
        for qid in (Q.SELECT_PLAY_SITES, Q.IS_REACHABLE):
            d = p.decide(qid, obs(body_text="请登录后观看"))
            assert d.answer is None, f"{qid} 在规则版里判了 {d.answer}"
            assert d.fallback_used is True

    def test_播放器无组件时返回_none_而非_false(self):
        """JS/canvas 播放器不产生 <video>——把「没测到」记成「没有」
        会把好站点误杀成 component_unverified 负样本。"""
        d = RulePerceptor().decide(Q.PLAYER_OK, obs(video_tag_count=0, iframe_count=0))
        assert d.answer is None

    def test_存在性判定标了_fallback(self):
        """media_count>0 只是存在性，不是「能播」。这个信号必须可被下游识别，
        否则人工复核会把规则版的正样本当成语义判定结果。"""
        d = RulePerceptor().decide(Q.PLAYER_OK, obs(video_tag_count=1))
        assert d.answer is True
        assert d.fallback_used is True

    def test_decision_可_json_序列化(self):
        """P1 存档要求 Decision 直落盘；不可序列化会在写盘时才炸。"""
        p = RulePerceptor()
        d = p.decide(Q.FIND_PLAY_CONTROL, obs(interactive_elements=elements(
            ("e1", "a", "播放"))))
        json.dumps({"answer": d.answer, "payload": dict(d.payload),
                    "evidence": d.evidence}, ensure_ascii=False)