"""W3 ``LLMPerceptor`` 的单元测试——**三条铁律各一组守卫**。

不重复契约测试已覆盖的七不变式。这里只测 LLM 版**特有**的东西，
且每条都对应一个**实测踩过的坑**（见 ``output/pipeline/llm_probe.py``）：

1. **采集充分性在代码层预检**（铁律 2）。实测 iqiyi 被判「否」，
   模型原话「body 为空、元素为 0，说明未渲染」——它把采集失败
   讲成了一条通顺的错结论。
2. **ref 白名单校验**（铁律 3）。模型会编 ref，而代码拿它去 click。
3. **确定性事实优先**：``<video>`` 存在性不问模型。

外加实测到的三种坏输出形态（think 包裹 / markdown fence / 纯散文）
必须仍能解析——否则一批数据会静默全落 None。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.perception import llm_perceptor as lp
from trajectory_pipeline.perception.base import InteractiveElement, Observation, Q
from trajectory_pipeline.tests.fakes import FakeLLM, llm_perceptor


def _obs(**kw) -> Observation:
    base = dict(url="https://x.test/", page_title="功夫 - 某视频站", body_text="页面正文内容")
    base.update(kw)
    return Observation(**base)


def _els(*pairs: tuple[str, str, str]):
    return tuple(InteractiveElement(ref=r, tag=t, label=lbl) for r, t, lbl in pairs)


class TestSufficiencyGuard:
    """铁律 2：采集不充分时不问模型。"""

    def test_正文与元素皆空时fail_closed(self):
        """实测形态：iqiyi/ixigua 站点页 body=0 元素=0（JS 未渲染完）。"""
        d = llm_perceptor().decide(Q.PLAYER_OK, _obs(body_text="", interactive_elements=()))
        assert d.answer is None
        assert "采集未拿到内容" in d.evidence

    def test_预检失败时不发请求(self):
        """守卫的意义在于**省钱与省时间**，也在于不把采集故障送去让
        模型编一个结论。请求数为 0 是可断言的硬证据。"""
        fake = FakeLLM()
        lp.LLMPerceptor(fake).decide(Q.PLAYER_OK, _obs(body_text="", interactive_elements=()))
        assert fake.calls == []

    def test_采集降级时不问模型(self):
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(degraded=("browser_links",)))
        assert d.answer is None
        assert fake.calls == []

    def test_有正文就放行(self):
        """反例护栏：预检不是「一律不问」。有正文就必须问，
        否则 LLM 版退化成规则版，白接。"""
        fake = FakeLLM()
        lp.LLMPerceptor(fake).decide(Q.IS_REACHABLE, _obs(body_text="有内容"))
        assert len(fake.calls) == 1

    def test_搜索页无链接时不问模型(self):
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(Q.SELECT_PLAY_SITES, _obs(links=()))
        assert d.answer is None and fake.calls == []


class TestRefWhitelist:
    """铁律 3：ref 必须在观察里。"""

    def test_编造的ref被拒(self):
        """编造的 ref 会让代码拿着它去 click——点错不可逆。"""
        d = llm_perceptor(lie_about_ref="e999").decide(
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e1", "a", "播放"))))
        assert d.answer is None
        assert "不在观察" in d.evidence

    def test_合法ref被采纳(self):
        d = llm_perceptor().decide(
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e1", "a", "立即播放"))))
        assert d.answer is True
        assert d.payload["ref"] == "e1"

    def test_可信ref进入动作流目标(self):
        """端到端：回传的 ref 必须能解析成点击目标（{tag,label}）。"""
        from trajectory_pipeline.executor.actions import target_of

        d = llm_perceptor().decide(
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e7", "button", "播放"))))
        assert target_of(str(d.payload["ref"]),
                         _obs(interactive_elements=_els(("e7", "button", "播放"))).interactive_elements
                         ) is not None


class TestDeterministicFirst:
    """铁律 1：确定性事实不问模型。"""

    def test_有video标签直接判真且不发请求(self):
        """实测反例：iqiyi video=1 却被 LLM 判否，理由是「未渲染」。
        存在性是代码事实，问模型只会多一个出错机会。"""
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(Q.PLAYER_OK, _obs(video_tag_count=1))
        assert d.answer is True
        assert fake.calls == []
        assert "代码层事实" in d.evidence

    def test_存在性判定标了fallback(self):
        """存在 ≠ 能正常播放。下游不能把它当语义判定。"""
        d = llm_perceptor().decide(Q.PLAYER_OK, _obs(video_tag_count=2))
        assert d.fallback_used is True

    def test_无video标签时问模型(self):
        """优酷形态：video=0 iframe=1，正文有内容——这才是 LLM 的战场。"""
        fake = FakeLLM()
        lp.LLMPerceptor(fake).decide(Q.PLAYER_OK, _obs(video_tag_count=0, iframe_count=1))
        assert len(fake.calls) == 1


class TestTrailerRuleWins:
    """预告判定以**代码词表**为准，不以模型为准。"""

    def test_模型说能播但词表判预告则记trailer(self):
        """预告片是用户明确的业务规则。把正片站点写进负样本池不可逆。"""
        d = llm_perceptor().decide(
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e1", "a", "预告片"))))
        assert d.payload["trailer_only"] is True
        assert d.payload["ref"] == ""

    def test_预告时不留ref(self):
        """ref 指向的是正片入口；预告控件点了只会到预告页。"""
        d = llm_perceptor().decide(
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e1", "a", "抢先看"))))
        assert d.payload.get("ref", "") == ""


class TestBadOutputs:
    """实测到的三种坏输出形态必须仍能解析。"""

    @pytest.mark.parametrize("flag", ["think", "fence", "prose"])
    def test_坏形态下fail_closed而非崩溃(self, flag):
        """散文形态下解析失败 → None（不是抛异常）。契约「从不抛」兜住了它，
        这里断言的是**结论正确**：解析不出来就不能给结论。"""
        d = llm_perceptor(**{flag: True}).decide(Q.IS_REACHABLE, _obs())
        assert d.answer in (True, False, None)
        assert isinstance(d.evidence, str) and d.evidence

    def test_think包裹能解析出结论(self):
        """think 包裹是可解析的（:func:`strip_think` 处理）。
        且 raw CoT **不进 evidence** —— 红线：不保存 raw CoT。"""
        d = llm_perceptor(think=True).decide(Q.IS_REACHABLE, _obs())
        assert d.answer is True
        assert "<think>" not in d.evidence
        assert "让我看看" not in d.evidence

    def test_fence包裹能解析出结论(self):
        d = llm_perceptor(fence=True).decide(Q.IS_REACHABLE, _obs())
        assert d.answer is True

    def test_散文形态给出可读的失败原因(self):
        """evidence 要能指导下一步：「是格式问题」与「是后端挂了」不同。"""
        d = llm_perceptor(prose=True).decide(Q.IS_REACHABLE, _obs())
        assert d.answer is None
        assert "无法解析" in d.evidence


class TestUnavailable:
    def test_后端挂了fail_closed(self):
        d = llm_perceptor(down=True).decide(Q.IS_REACHABLE, _obs())
        assert d.answer is None
        assert "LLM 不可用" in d.evidence

    def test_失败原因带后端诊断(self):
        """"transport:ConnectError" 是排障第一手信息。"""
        d = llm_perceptor(down=True).decide(Q.IS_REACHABLE, _obs())
        assert "transport" in d.evidence


class TestSelectSites:
    def test_编造的url被拒(self):
        """编造的 url 会让控制流去访问不存在的站点，而那次访问会以
        「不可达」入负样本池——**污染出一个并不存在的失败原因**。"""
        d = llm_perceptor(invent_urls=True).decide(
            Q.SELECT_PLAY_SITES, _obs(links=(), ))
        assert d.answer is None

    def test_真实url被采纳(self):
        from trajectory_pipeline.perception.base import LinkItem

        d = llm_perceptor().decide(
            Q.SELECT_PLAY_SITES,
            _obs(links=(LinkItem("功夫 在线观看", "https://www.a.test/gg"),)))
        assert d.answer is True
        assert d.payload["selected"][0]["url"] == "https://www.a.test/gg"

    def test_全被刷掉时不判False(self):
        """全被刷掉说明模型说的与我们给的候选无关，可能只是输入不全。
        判 False 等于说「这页一个可看站都没有」——那会清空整个批次。"""
        from trajectory_pipeline.perception.base import LinkItem

        d = llm_perceptor(invent_urls=True).decide(
            Q.SELECT_PLAY_SITES, _obs(links=(LinkItem("x", "https://www.a.test/gg"),)))
        assert d.answer is None
        assert "不在观察" in d.evidence


class TestDecisionShape:
    def test_来源标llm(self):
        """存档要能分清「这条结论是谁给的」。"""
        assert llm_perceptor().decide(Q.IS_REACHABLE, _obs()).source == "llm"

    def test_confidence不是模型自报(self):
        """llm/__init__.py 记着「自报 confidence 普遍虚高」。
        本实现给代码层先验，且 evidence 里写明。"""
        d = llm_perceptor().decide(Q.IS_REACHABLE, _obs())
        assert d.confidence == lp.CONFIDENCE_JUDGED
        assert "非模型自报" in d.evidence

    def test_无结论时confidence为零(self):
        """0 表示「没有结论」，不是「很不确定地认为否」。"""
        assert llm_perceptor(down=True).decide(Q.IS_REACHABLE, _obs()).confidence == 0.0

    def test_decision可序列化(self):
        """``Decision.payload`` 是 ``MappingProxyType``（不可变契约），
        落盘前要转成 dict。断言的是**内容**可序列化，不是对象本身——
        ``json.dumps(mappingproxy)`` 直接抛 TypeError，这是设计而非缺陷。"""
        import json

        d = llm_perceptor().decide(
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e1", "a", "播放"))))
        assert json.loads(json.dumps(dict(d.payload), ensure_ascii=False))["ref"] == "e1"

    def test_题目写进请求正文(self):
        """IS_REACHABLE 与 PLAYER_OK 的输出契约字面相同，
        靠契约串反查题目会认错。断言题号显式出现。"""
        import json

        fake = FakeLLM()
        lp.LLMPerceptor(fake).decide(Q.FIND_PLAY_CONTROL,
                                     _obs(interactive_elements=_els(("e1", "a", "播放"))))
        assert json.loads(fake.calls[0][1])["question"] == Q.FIND_PLAY_CONTROL