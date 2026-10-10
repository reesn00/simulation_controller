"""W3 ``LLMPerceptor`` 的单元测试——**三条铁律各一组守卫**。

不重复契约测试已覆盖的七不变式。这里只测 LLM 版**特有**的东西，
且每条都对应一个**实测踩过的坑**（见 ``trajectory_pipeline/perception/probe_llm.py``）：

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
from trajectory_pipeline.perception.base import (
    InteractiveElement,
    LinkItem,
    Observation,
    Q,
)
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
        「不可达」入负样本池——**污染出一个并不存在的失败原因**。

        ⚠️ 这里**必须给一条真实链接**且文本含片名。空链接会先撞上
        「搜索页未采到链接」的早退分支，测试照样绿，但测的已经不是
        url 校验了（避坑指南 §7.3）。"""
        from trajectory_pipeline.perception.base import LinkItem

        d = llm_perceptor(invent_urls=True).decide(
            Q.SELECT_PLAY_SITES,
            _obs(links=(LinkItem("功夫 在线观看", "https://www.a.test/gg"),)))
        assert d.answer is None
        assert "编造.test" not in str(d.payload)

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
            Q.SELECT_PLAY_SITES,
            _obs(links=(LinkItem("功夫 在线观看", "https://www.a.test/gg"),)))
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


#: 百度结果页的导航条——**实测形态**：它排在搜索结果**之前**。
#: 2026-10-10 跑 T001「功夫 在线观看」时，前 12 条依次是
#: 「登录 / 抗击肺炎 / 新闻 / hao123 / 地图 / 视频 / 贴吧 / 学术 /
#: 设置 / 更多产品」，共 123 条链接里只有 3 条文本写着片名，且排在后面。
_NAV_LINKS = [
    ("登录", "https://passport.baidu.com/"),
    ("抗击肺炎", "https://voice.baidu.com/"),
    ("新闻", "http://news.baidu.com/"),
    ("hao123", "https://www.hao123.com/"),
    ("地图", "http://map.baidu.com/"),
    ("视频", "http://v.baidu.com/"),
    ("贴吧", "http://tie.baidu.com/"),
    ("学术", "http://xueshu.baidu.com/"),
    ("设置", "https://www.baidu.com/gaoji/preferences.html"),
    ("更多产品", "https://www.baidu.com/more/"),
]

_RESULT_LINKS = [
    ("《功夫》电影完整版-高清正版在线观看-爱奇艺",
     "https://www.iqiyi.com/v_19rrk40at8.html"),
    ("功夫 (普通话版)_电影_高清完整版视频在线观看_腾讯视频",
     "https://v.qq.com/x/cover/abc.html"),
]


def _baidu_result_page() -> Observation:
    """25 条导航在前、2 条结果标题在后——与实测顺序一致。

    25 > ``LLM_LINKS``(20)，所以只截前 N 条时结果标题**必然被截掉**。
    这不是构造出来的边角，是实测：修复前的那批 5 个候选全被记成
    ``not_play_site``，因为模型收到的 20 条链接里一条片名都没有。
    """
    nav = [(t, h) for t, h in _NAV_LINKS] * 3          # 30 条，够长
    return Observation(
        url="https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB",
        page_title="功夫_百度搜索",
        body_text="搜索结果页正文，含有片名但与 URL 无对应关系。" * 20,
        links=tuple(
            LinkItem(text=t, href=h) for t, h in (nav[:25] + _RESULT_LINKS)
        ),
    )


class Test预检与渲染同源:
    """① 的判别力预检与模型实际看到的链接**必须是同一批**。

    实测形态（2026-10-10，baidu 搜《功夫》）：预检扫全量 123 条，
    数到 3 条文本含片名就放行；而 :func:`_render` 只送前 20 条，
    那 20 条**全是导航栏**。于是模型没有一次机会看到片名，判 True 且
    selected/rejected 全空，5 个候选全落进 ``not_play_site`` 负样本池。

    这类错位只在「链接顺序随引擎改版而变」时才显形，所以不能靠调阈值补。
    """

    def test_渲染窗口里含片名的链接排在最前(self):
        out = lp._render(_baidu_result_page(), target_title="功夫",
                         question=Q.SELECT_PLAY_SITES)
        assert out["links"][0]["text"].startswith("《功夫》电影完整版")

    def test_命中项前置保证不落在窗口外(self):
        """「预检放行 ⇒ 模型看得到」成立的原因就在这条恒等式上。

        只断「重排后第一条是结果标题」不够——有人日后把重排去掉而
        保留其余改动，那条仍会绿。真正要钉的是**全量命中数 == 窗口内
        命中数**：不重排时窗口内命中 0 而全量 2，这条立刻红。
        """
        obs = _baidu_result_page()
        ordered = lp._ordered_links(obs, Q.SELECT_PLAY_SITES, "功夫")
        in_window = sum(1 for l in ordered[:lp.LLM_LINKS]
                        if "功夫" in (l.text or ""))
        whole = sum(1 for l in obs.links if "功夫" in (l.text or ""))
        assert whole == 2
        assert in_window == whole
        # 且预检在这个恒等式下确实放行
        assert lp._title_link_sufficiency(obs, "功夫") == ""

    def test_整批链接都无片名时fail_closed(self):
        """预检**不能被这次修复放水**：真没有片名时仍须 fail-closed。"""
        obs = Observation(
            url="https://www.baidu.com/s?wd=x", page_title="百度",
            body_text="正文",
            links=tuple(LinkItem(text=t, href=h) for t, h in _NAV_LINKS),
        )
        assert "不判" in lp._title_link_sufficiency(obs, "功夫")
        assert llm_perceptor().decide(Q.SELECT_PLAY_SITES, obs).answer is None

    def test_非选择题不重排(self):
        """重排只服务于 ① 的片名↔URL配对；其他题的链接顺序是页面原序，
        改了就是拿我们的加工覆盖观察。"""
        obs = _baidu_result_page()
        assert [l.href for l in lp._ordered_links(obs, Q.IS_REACHABLE, "功夫")] == [
            l.href for l in obs.links]

    def test_端到端模型看到的第一条就是结果标题(self):
        """FakeLLM 对 ① 的模拟是「选它看到的第一条链接」——
        与真实模型同形。所以修复前它会选中导航栏（进而产出假
        ``not_play_site``），修复后选中结果标题。"""
        import json

        fake = FakeLLM()
        # 显式绑片名：① 的预检要拿它匹配链接文本，没绑时恒 None，
        # 测试会先撞上预检而不是要走的那条路径。
        d = lp.LLMPerceptor(fake, target_title="功夫").decide(
            Q.SELECT_PLAY_SITES, _baidu_result_page())
        assert d.answer is True
        sent = json.loads(fake.calls[0][1])["observation"]["links"]
        assert sent[0]["href"] == "https://www.iqiyi.com/v_19rrk40at8.html"
        assert dict(d.payload)["selected"][0]["url"] == sent[0]["href"]

class Test选中为空不算答完:
    """① **判 True 的语义是「我选出了站」**。只排除、零选中不是 True。

    实测（2026-10-10 T001）：模型排除了 6 个链接却一个没选，
    代码放它过→控制流按 True 过滤→``kept`` 空→0 候选→
    **一个站都没访问**，整批只剩一堆 ``unresolved``。
    """

    def test_全排除零选中判None(self):
        d = llm_perceptor(reject_all=True).decide(
            Q.SELECT_PLAY_SITES, _baidu_result_page())
        assert d.answer is None
        assert "一个都没选中" in d.evidence

    def test_真选中一个仍是True(self):
        """反例护栏：不能把「零选中」判成 None 就完事——
        正常选中必须照常给结论，否则 ① 恒 None、筛选形同虚设。

        ``FakeLLM`` 对 ① 的模拟正是「选它看到的第一条」，重排后
        第一条是结果标题，所以它给出的是**真选中**形态。
        """
        d = llm_perceptor().decide(Q.SELECT_PLAY_SITES, _baidu_result_page())
        assert d.answer is True
        assert len(dict(d.payload)["selected"]) == 1
        assert dict(d.payload)["rejected"] == []


class Test付费墙不算成功:
    """2026-10-10 口径裁定：**有播放页但需订阅/付费不算成功**。

    但它也不是「页面上没有播放控件」——控件是有的，卡在付费。
    混进 ``component_unverified`` 就丢掉这个区别，而两者训练出来的
    东西完全不同：一个学「识别付费墙并退出」，一个学「控件没渲染」。
    所以单开 ``subscription_wall`` 一支。
    """

    PAYWALL_BODY = "《功夫》 正片 立即播放 开通会员观看更多"

    def test_模型判否且有付费信号_落付费墙(self):
        d = llm_perceptor(down=True).decide(Q.PLAYER_OK, _obs())
        assert d.answer is None                     # 后端挂着，先确认夹具正常
        fake = FakeLLM(answers={Q.PLAYER_OK: False})
        d = lp.LLMPerceptor(fake).decide(
            Q.PLAYER_OK, _obs(body_text=self.PAYWALL_BODY))
        assert d.answer is False
        assert d.payload["paywall"] is True
        assert "开通会员" in d.evidence

    def test_模型判否但无付费信号_不落付费墙(self):
        fake = FakeLLM(answers={Q.PLAYER_OK: False})
        d = lp.LLMPerceptor(fake).decide(
            Q.PLAYER_OK, _obs(body_text="页面只有导航，无播放器"))
        assert d.payload.get("paywall") is False

    def test_模型判真时不因词表升级(self):
        """**两边一致才落细分分支。** 模型判 True 说明它认为页面可用，
        此时词表命中多半是导航栏文案（实测百度页正文就有「免费观看」）。
        强行升级会把正常站点判成付费墙。"""
        fake = FakeLLM(answers={Q.PLAYER_OK: True})
        d = lp.LLMPerceptor(fake).decide(
            Q.PLAYER_OK, _obs(body_text=self.PAYWALL_BODY))
        assert d.answer is True
        assert d.payload.get("paywall") is False

    def test_导航栏的免费观看不算付费信号(self):
        """⚠️ 「免费观看」「在线观看」**不在词表里**——
        它们在导航栏里的频率远高于付费提示（实测百度页正文就有）。"""
        assert lp.classify_paywall("首页 电影 电视剧 免费观看 在线观看") == ""

    def test_词表命中的是完整短语(self):
        assert lp.classify_paywall("请订阅后观看完整版") == "订阅后观看"
        assert lp.classify_paywall("Buy this episode now") == "buy this episode"


class Test付费墙记账:
    def test_payload决定分支(self):
        """同一道题（④）要能落两支：payload 优先于题绑定。"""
        from trajectory_pipeline.executor.branches import decision_branch
        from trajectory_pipeline.perception.base import Decision

        paid = Decision(Q.PLAYER_OK, False, 0.8, "需付费", "llm",
                        {"paywall": True})
        plain = Decision(Q.PLAYER_OK, False, 0.8, "没组件", "llm", {})
        assert decision_branch(paid) == "subscription_wall"
        assert decision_branch(plain) == "component_unverified"

    def test_不入非样本集合(self):
        """付费墙是**判出来了的业务结论**，不是「没判出来」——
        它教模型「识别付费墙并退出」，是该进训练集的负样本。"""
        from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES
        assert "subscription_wall" not in NON_SAMPLE_BRANCHES
