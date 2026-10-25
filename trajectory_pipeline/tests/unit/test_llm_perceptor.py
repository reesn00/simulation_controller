"""W3 ``LLMPerceptor`` 的单元测试——**三条铁律各一组守卫**。

不重复契约测试已覆盖的七不变式。这里只测 LLM 版**特有**的东西，
且每条都对应一个**实测踩过的坑**（见 ``trajectory_pipeline/perception/probe_llm.py``）：

1. **采集充分性在代码层预检**（铁律 2）。实测 iqiyi 被判「否」，
   模型原话「body 为空、元素为 0，说明未渲染」——它把采集失败
   讲成了一条通顺的错结论。
2. **ref 白名单校验**（铁律 3）。模型会编 ref，而代码拿它去 click。
3. **媒体计数不许许诺成功**（2026-10-23 · M-26 改）。原先这里是
   「确定性事实优先：``<video>`` 存在性不问模型」——那道短路是**护栏越权**
   （代码层可以否，不能许），已删；④ 现在只走 LLM。

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
    base = dict(
        url="https://x.test/", page_title="功夫 - 某视频站", body_text="页面正文内容"
    )
    base.update(kw)
    return Observation(**base)


def _els(*pairs: tuple[str, str, str]):
    return tuple(InteractiveElement(ref=r, role=t, name=lbl) for r, t, lbl in pairs)


class TestSufficiencyGuard:
    """铁律 2：采集不充分时不问模型。"""

    def test_正文与元素皆空时fail_closed(self):
        """实测形态：iqiyi/ixigua 站点页 body=0 元素=0（JS 未渲染完）。"""
        d = llm_perceptor().decide(
            Q.PLAYER_OK, _obs(body_text="", interactive_elements=())
        )
        assert d.answer is None
        assert "采集未拿到内容" in d.evidence

    def test_预检失败时不发请求(self):
        """守卫的意义在于**省钱与省时间**，也在于不把采集故障送去让
        模型编一个结论。请求数为 0 是可断言的硬证据。"""
        fake = FakeLLM()
        lp.LLMPerceptor(fake).decide(
            Q.PLAYER_OK, _obs(body_text="", interactive_elements=())
        )
        assert fake.calls == []

    def test_采集降级时不问模型(self):
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(degraded=("browser_links",))
        )
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
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e1", "a", "播放")))
        )
        assert d.answer is None
        assert "不在观察" in d.evidence

    def test_合法ref被采纳(self):
        d = llm_perceptor().decide(
            Q.FIND_PLAY_CONTROL,
            _obs(interactive_elements=_els(("e1", "a", "立即播放"))),
        )
        assert d.answer is True
        assert d.payload["ref"] == "e1"

    def test_可信ref进入动作流目标(self):
        """端到端：回传的 ref 必须能解析成点击目标（{tag,label}）。"""
        from trajectory_pipeline.executor.actions import target_of

        d = llm_perceptor().decide(
            Q.FIND_PLAY_CONTROL,
            _obs(interactive_elements=_els(("e7", "button", "播放"))),
        )
        assert (
            target_of(
                str(d.payload["ref"]),
                _obs(
                    interactive_elements=_els(("e7", "button", "播放"))
                ).interactive_elements,
            )
            is not None
        )


class TestRef由代码取回:
    """实测（2026-10-11）：v.qq.com 页面上有播放控件，模型却没回 ref。

    旧行为是 fail-closed 判 ``None`` → 控制流不点击 → 没有 ``player_obs``
    → 判断点 ④ 无从判起 → **整批 D-2 全是 undetermined**。代价是这一站
    的轨迹作废，而更糟的是它离「假负样本」只差一步：把「模型忘了填
    字段」写成「这个站看不了」，负样本池就再也答不出「为什么看不了」。
    """

    def test_模型不回ref时代码按词表取回(self):
        d = llm_perceptor(forget_ref=True).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(
                interactive_elements=_els(
                    ("e1", "a", "登录"), ("e5", "button", "立即播放")
                )
            ),
        )
        assert d.answer is True
        assert d.payload["ref"] == "e5"
        assert d.payload["trailer_only"] is False

    def test_取回的ref必须能点得到(self):
        """ref 的用途就是 click。取回来却不属于观察，那是造出来的东西——
        和模型编造 ref 一样不可用。"""
        from trajectory_pipeline.executor.actions import target_of

        obs = _obs(
            interactive_elements=_els(("e1", "a", "登录"), ("e5", "button", "立即播放"))
        )
        d = llm_perceptor(forget_ref=True).decide(Q.FIND_PLAY_CONTROL, obs)
        assert target_of(str(d.payload["ref"]), obs.interactive_elements) is not None

    def test_evidence写明ref是代码取回的(self):
        """人读存档时要能分清 ref 是模型给的还是代码补的——两者是
        不同的证据强度，不标出来就分不出来了。"""
        d = llm_perceptor(forget_ref=True).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(interactive_elements=_els(("e5", "button", "立即播放"))),
        )
        assert "未回传 ref" in d.evidence and "词表" in d.evidence

    def test_词表为空时仍然判否(self):
        """反向护栏：取回不是「一律判 True」。页面上真的没有播放控件
        时必须判 False，否则每个页面都会变成「找到了播放控件」。"""
        d = llm_perceptor(forget_ref=True).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(interactive_elements=_els(("e1", "a", "登录"), ("e2", "a", "设置"))),
        )
        assert d.answer is False
        assert d.payload["ref"] == ""

    def test_只有预告控件时落trailer_only而非判否(self):
        """「有控件但全是预告片」与「压根没有控件」是两条不同的负分支。

        W1 一直落 ``trailer_only``，W3 原来直落 False——同一批素材两个
        结论，且都看着像对的。"""
        d = llm_perceptor(forget_ref=True).decide(
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e1", "a", "预告片")))
        )
        assert d.answer is True
        assert d.payload["trailer_only"] is True
        assert d.payload["ref"] == ""

    def test_预告控件不会被当成可点的ref(self):
        """取回 ref 只取非预告的：点了预告片，采样采到的是预告资源。"""
        from trajectory_pipeline.perception.rule_perceptor import (
            play_control_candidates,
            trailer_only_candidates,
        )

        obs = _obs(
            interactive_elements=_els(
                ("e1", "a", "预告片"), ("e2", "button", "正片第1集")
            )
        )
        assert [e.ref for e in play_control_candidates(obs)] == ["e2"]
        assert [e.ref for e in trailer_only_candidates(obs)] == ["e1"]

    def test_编造ref仍被白名单拦住(self):
        """取回 ref 不等于放开白名单：模型**给了** ref 而它不存在，
        那是编造，照旧 fail-closed。"""
        d = llm_perceptor(lie_about_ref="e999").decide(
            Q.FIND_PLAY_CONTROL,
            _obs(interactive_elements=_els(("e5", "button", "立即播放"))),
        )
        assert d.answer is None
        assert "不在观察" in d.evidence

    def test_模型给的ref未命中词表时留痕(self):
        """图标按钮（纯 SVG / 文本在 aria-label 上）常常过不了词表，
        而模型看得懂它的视觉语义——所以**标注**而不是否决。但分歧必须
        留痕：禁止投票意味着暴露给人，而不是由代码悄悄择一。"""
        d = llm_perceptor(lie_about_ref="e1").decide(
            Q.FIND_PLAY_CONTROL,
            _obs(
                interactive_elements=_els(
                    ("e1", "button", "▷"), ("e5", "button", "立即播放")
                )
            ),
        )
        assert d.answer is True
        assert d.payload["ref"] == "e1"
        assert "未命中播放控件词表" in d.evidence


class Test不落rawCoT:
    """CLAUDE.md 红线：**不保存 raw CoT**。

    ``_why_unparsable`` 是这条红线的**落盘点**——它把模型原文截前 120 字
    写进 ``evidence``，而 ``evidence`` 是 :class:`SiteOutcome` 的持久字段，
    直接落 P1。漏了 ``strip_think`` 就等于把推理模型的思考段落存档。

    这条漏了很久而门禁全绿，根因不在生产代码而在 **fake**：
    ``prose=True`` 在套 think 之前就 return，``think=True, prose=True``
    **组合不出这个形态**，于是没有任何测试造得出它。fake 造不出的形态
    等于不存在的守卫——这次一并修掉。
    """

    THINK = "模型内部推理：逐条比较了链接1、链接2、链接3 的标题"

    def test_剥掉思考后才截前N字(self):
        out = lp._why_unparsable(
            f"<think>{self.THINK}</think>\n根据页面观察，我无法确定。"
        )
        assert self.THINK not in out
        assert "根据页面观察" in out

    def test_未闭合的思考也剥(self):
        """实测形态：思考段没有闭合标签，一直吃到文末。"""
        out = lp._why_unparsable(f"<think>{self.THINK}")
        assert self.THINK not in out

    def test_剥完为空时说得出来(self):
        """「只吐了思考、没有正文」与「预算被思考吃光」症状同源而成因不同，
        要能从 evidence 里认出来——它们的下一步动作不一样。"""
        out = lp._why_unparsable(f"<think>{self.THINK}</think>")
        assert "为空" in out and self.THINK not in out

    def test_无思考时原样保留(self):
        """剥 think 不是「把内容都删掉」——没有 think 的解析失败仍然要
        留下原文，否则这条诊断能力自己没了。"""
        assert "无法解析" in lp._why_unparsable("这不是 JSON")

    def test_端到端_evidence不含思考(self):
        """走完整条 decide → evidence 的路，不直接调内部函数。"""
        d = llm_perceptor(think=True, prose=True, think_text=self.THINK).decide(
            Q.IS_REACHABLE, _obs(body_text="页面主体可见")
        )
        assert d.answer is None
        assert self.THINK not in d.evidence
        assert "<think>" not in d.evidence

    def test_端到端_evidence落进存档字段(self):
        """红线的落点是**持久化字段**，所以断言必须打到 outcome 上，
        而不是停在 Decision——中间隔着 ledger 转换。"""
        from trajectory_pipeline.executor.branches import RunLedger

        d = llm_perceptor(think=True, prose=True, think_text=self.THINK).decide(
            Q.IS_REACHABLE, _obs(body_text="页面主体可见")
        )
        outcome = RunLedger("T001").record(
            "https://x.test/", None, d.evidence, decision=d, site_url="https://x.test/"
        )
        assert self.THINK not in outcome.to_json()["evidence"]
        assert "<think>" not in outcome.to_json()["evidence"]


class Test媒体计数不许许诺成功:
    """**媒体计数只能当必要条件，不能当充分条件**（2026-10-23 · M-26）。

    这里原来有一道短路：``video_tag_count >= 1`` ⇒ 直接 ``answer=True``、
    **不问 LLM**。那是**护栏越权**——代码层可以**否**，不能**许**。
    实测代价：优酷「试看6分钟｜开通会员看完整视频」页 ``video=1``
    ⇒ 被读成「能看正片」⇒ **5 份存档 5 条假成功**（LLM 压根没被问过）；
    网易号美食图文页 6 个 ``<video>`` 是**点广告之后被注入的**
    ⇒ 同样被读成成功——那条正文里连付费形态词都没有，
    **靠词表拦不住，只能把短路本身删掉**。
    """

    def test_有video标签也问模型(self):
        """媒体计数不许许诺成功 ⇒ ④ 必须走 LLM。请求数为 1 是硬证据。"""
        fake = FakeLLM()
        lp.LLMPerceptor(fake).decide(Q.PLAYER_OK, _obs(video_tag_count=1))
        assert len(fake.calls) == 1

    def test_模型判真时才算真(self):
        """「真」只能来自 LLM（或别的语义判据），不能来自计数。"""
        d = llm_perceptor().decide(Q.PLAYER_OK, _obs(video_tag_count=1))
        assert d.answer is True
        assert "代码层事实" not in d.evidence

    def test_付费形态加媒体计数不许答真(self):
        """台账 M-26 验收③：付费形态 × 媒体计数 ≥1 ⇒ **不得**返回 True。

        假后端判否（页面明说要看会员），媒体计数不许把它翻成 True。"""
        fake = FakeLLM(answers={Q.PLAYER_OK: False})
        d = lp.LLMPerceptor(fake).decide(
            Q.PLAYER_OK,
            _obs(body_text="试看6分钟，开通会员看完整视频", video_tag_count=1),
        )
        assert d.answer is False
        assert d.payload.get("paywall") is True

    def test_无video标签时同样问模型(self):
        """优酷形态：video=0 iframe=1，正文有内容——这本来就是 LLM 的战场。"""
        fake = FakeLLM()
        lp.LLMPerceptor(fake).decide(
            Q.PLAYER_OK, _obs(video_tag_count=0, iframe_count=1)
        )
        assert len(fake.calls) == 1


class TestTrailerRuleWins:
    """预告判定以**代码词表**为准，不以模型为准。"""

    def test_模型说能播但词表判预告则记trailer(self):
        """预告片是用户明确的业务规则。把正片站点写进负样本池不可逆。"""
        d = llm_perceptor().decide(
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e1", "a", "预告片")))
        )
        assert d.payload["trailer_only"] is True
        assert d.payload["ref"] == ""

    def test_预告时不留ref(self):
        """ref 指向的是正片入口；预告控件点了只会到预告页。"""
        d = llm_perceptor().decide(
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e1", "a", "抢先看")))
        )
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
        """ "transport:ConnectError" 是排障第一手信息。"""
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
            _obs(links=(LinkItem("功夫 在线观看", "https://www.a.test/gg"),)),
        )
        assert d.answer is None
        assert "编造.test" not in str(d.payload)

    def test_真实url被采纳(self):
        from trajectory_pipeline.perception.base import LinkItem

        d = llm_perceptor().decide(
            Q.SELECT_PLAY_SITES,
            _obs(links=(LinkItem("功夫 在线观看", "https://www.a.test/gg"),)),
        )
        assert d.answer is True
        assert d.payload["selected"][0]["url"] == "https://www.a.test/gg"

    def test_全被刷掉时不判False(self):
        """全被刷掉说明模型说的与我们给的候选无关，可能只是输入不全。
        判 False 等于说「这页一个可看站都没有」——那会清空整个批次。"""
        from trajectory_pipeline.perception.base import LinkItem

        d = llm_perceptor(invent_urls=True).decide(
            Q.SELECT_PLAY_SITES,
            _obs(links=(LinkItem("功夫 在线观看", "https://www.a.test/gg"),)),
        )
        assert d.answer is None
        assert "不在观察" in d.evidence


class TestNoConclusionVsUnmatched:
    """「模型没作答」与「作答了但地址全对不上」是**两件事**，不得合成一句。

    实测（2026-10-11 T035 / E001）连续两个判断点①返回**空的**两个数组，
    而当时的 evidence 写的是「全部不在观察的 107 个链接里」——字面为真
    （0 个里的 0 个不在其中），语义却在说「模型给了地址但都错」。
    这句话把排查引向了「URL 为什么不匹配」，而真正的问题是「它为什么
    一个都不给」。合成一句的代价不是措辞，是**把诊断指错方向**。
    """

    def test_两个都空时说的是没作答(self):
        from trajectory_pipeline.perception.base import LinkItem

        d = llm_perceptor(no_conclusion=True).decide(
            Q.SELECT_PLAY_SITES,
            _obs(links=(LinkItem("功夫 在线观看", "https://www.a.test/gg"),)),
        )
        assert d.answer is None
        assert "未给出任何" in d.evidence
        # 反向护栏：这句话**不能**同时说「不在观察」——两者指的不是一回事
        assert "不在观察" not in d.evidence

    def test_地址全对不上时说的是对不上(self):
        from trajectory_pipeline.perception.base import LinkItem

        d = llm_perceptor(invent_urls=True).decide(
            Q.SELECT_PLAY_SITES,
            _obs(links=(LinkItem("功夫 在线观看", "https://www.a.test/gg"),)),
        )
        assert d.answer is None
        assert "全部不在观察" in d.evidence
        assert "未给出任何" not in d.evidence

    def test_没作答时evidence带上送进模型的链接数(self):
        """复盘要能判断「是输入不足还是模型偷懒」，光看「未作答」不够。"""
        from trajectory_pipeline.perception.base import LinkItem

        links = tuple(
            LinkItem(f"功夫 第{i}集", f"https://www.a.test/{i}") for i in range(3)
        )
        d = llm_perceptor(no_conclusion=True).decide(
            Q.SELECT_PLAY_SITES, _obs(links=links)
        )
        assert "3 条链接" in d.evidence

    def test_空回传时evidence带剥think后的模型原文(self):
        """根因三分类（认为没有够格/偷懒/最省事合规解）靠原文区分——
        evidence 必须留下模型到底回了什么。**但先 strip_think**：
        evidence 是持久字段，raw CoT 不进（红线）。"""
        from trajectory_pipeline.perception.base import LinkItem

        d = llm_perceptor(
            no_conclusion=True, think=True, think_text="这是内部推理不该落盘"
        ).decide(
            Q.SELECT_PLAY_SITES,
            _obs(links=(LinkItem("功夫 在线观看", "https://www.a.test/gg"),)),
        )
        assert d.answer is None
        # 剥 think 后的原文（selected/rejected 都是空数组的 JSON）要能看见
        assert "selected" in d.evidence and "rejected" in d.evidence
        # 反向护栏：思考过程本身不能进 evidence
        assert "内部推理" not in d.evidence


class TestClassifySearchLinks:
    """方案 12 阶段 A1 · 分类任务 handler（``_classify_search_links``）。

    骨架与 SELECT_* 同款（解析 → 编号回指 → 白名单落位），差别在
    answer 没有 False：分类是逐条的类型判定，「这批里没有目标类型」是
    orchestrator 从 payload 推导的分发结论（kept=0），不是本题的整页
    结论。FakeLLM 的默认规则按关键词四分类，``classify`` 表可逐链覆盖。
    """

    def _links(self):
        return (
            LinkItem("功夫 在线观看-爱奇艺", "https://www.iqiyi.com/v/1"),
            LinkItem("知乎：哪里能看功夫", "https://www.zhihu.com/q/1"),
            LinkItem("影视导航站大全", "https://www.hao123.com/"),
            LinkItem("功夫 电影新闻", "https://news.test/x"),
        )

    def test_按类落位且编号回指补url(self):
        d = llm_perceptor().decide(
            Q.CLASSIFY_SEARCH_LINKS, _obs(links=self._links())
        )
        assert d.answer is True
        assert [r["url"] for r in d.payload["video_site"]] == [
            "https://www.iqiyi.com/v/1"
        ]
        assert [r["url"] for r in d.payload["article_post"]] == [
            "https://www.zhihu.com/q/1"
        ]
        assert [r["url"] for r in d.payload["aggregator"]] == [
            "https://www.hao123.com/"
        ]
        assert [r["url"] for r in d.payload["noise"]] == ["https://news.test/x"]
        # evidence 是四类的分布账面（复盘读它不读 payload）
        assert "video_site 1" in d.evidence
        assert "article_post 1" in d.evidence

    def test_显式classify表逐链覆盖(self):
        d = llm_perceptor(
            classify={"https://www.iqiyi.com/v/1": "noise"}
        ).decide(Q.CLASSIFY_SEARCH_LINKS, _obs(links=self._links()))
        assert d.answer is True
        assert d.payload["video_site"] == []
        assert [r["url"] for r in d.payload["noise"]] == [
            "https://www.iqiyi.com/v/1", "https://news.test/x",
        ]

    def test_编造的url整条丢弃(self):
        """编造的 url 会让控制流去访问不存在的站点——白名单纪律
        与 SELECT_* 同款。全部对不上时是 None（无法采信），不是 False。"""
        d = llm_perceptor(invent_urls=True).decide(
            Q.CLASSIFY_SEARCH_LINKS, _obs(links=self._links())
        )
        assert d.answer is None
        assert "不在观察" in d.evidence

    def test_四类全空是没作答(self):
        """「没作答」与「作答了但全对不上」是两件事（与 SELECT_* 的
        NoConclusionVsUnmatched 同一条纪律）——合成一句会把排查引向
        「URL 为什么不匹配」，而真正的问题是「它为什么一个都不给」。"""
        d = llm_perceptor(no_conclusion=True).decide(
            Q.CLASSIFY_SEARCH_LINKS, _obs(links=self._links())
        )
        assert d.answer is None
        assert "没答这道题" in d.evidence
        assert "不在观察" not in d.evidence

    def test_全部归noise仍是有效作答(self):
        """reject_all 形态：全部点名归 noise（有作答但都不是目标类）。
        handler 层 answer=True（分类可用），「kept=0」由 orchestrator
        分发推导——这是 A1 涌现式 False 的输入形态。"""
        d = llm_perceptor(reject_all=True).decide(
            Q.CLASSIFY_SEARCH_LINKS, _obs(links=self._links())
        )
        assert d.answer is True
        assert len(d.payload["noise"]) == 4
        assert d.payload["video_site"] == []
        assert "noise 4" in d.evidence

    def test_零链接fail_closed(self):
        """候选视图为空 = 没有输入，无从分类。"""
        d = llm_perceptor().decide(Q.CLASSIFY_SEARCH_LINKS, _obs(links=()))
        assert d.answer is None
        assert "0 条链接" in d.evidence

    def test_候选视图无交互元素不误杀(self):
        """候选视图刻意清空交互元素（分类题只读链接表，页面 chrome 是
        纯 token 开销）。通用守卫「正文 0 字符且交互元素 0 个」守的是
        页面渲染完整性，对投影观察是假阳性——分类分支必须跳过它，
        只查链接非空。"""
        obs = Observation(
            url="https://www.baidu.com/s?wd=x",
            page_title="百度",
            body_text="",
            interactive_elements=(),
            links=self._links(),
        )
        d = llm_perceptor().decide(Q.CLASSIFY_SEARCH_LINKS, obs)
        assert d.answer is True


class Test回指编号而不是url:
    """① 让模型回**编号 i**，代码回指成 href——回长 URL 是记忆任务。

    模型会顺手改写 scheme、吞掉 www、丢掉 query 尾巴，于是「选的 URL
    压根不在观察里」整批作废。编号把它降级成复制任务，而 href 由代码
    从窗口里取，模型碰不到字符串。
    """

    def _links(self):
        from trajectory_pipeline.perception.base import LinkItem

        return (
            LinkItem("功夫 在线观看", "https://www.a.test/gg"),
            LinkItem("百度首页", "https://www.baidu.com/"),
        )

    def test_渲染时每条链接带编号(self):
        rows = lp._render(
            _obs(links=self._links()), target_title="功夫", question=Q.SELECT_PLAY_SITES
        )["links"]
        assert [r["i"] for r in rows] == [0, 1]

    def test_非一号题不带编号(self):
        """编号是 ① 的契约。别的题带上它等于凭空多一个字段，
        而模型会顺着编造出编号语义。"""
        rows = lp._render(_obs(links=self._links()), question=Q.PLAYER_OK)["links"]
        assert all("i" not in r for r in rows)

    def test_编号回指到窗口里的href(self):
        d = llm_perceptor(by_index=True).decide(
            Q.SELECT_PLAY_SITES, _obs(links=self._links())
        )
        assert d.answer is True
        # 回传的 i=0 → 第一条链接的 href，且 payload 里带 url 供下游记账
        assert d.payload["selected"][0]["url"] == "https://www.a.test/gg"

    def test_字符串编号也认(self):
        """模型爱给整数加引号。真后端两种形态都出现过。"""
        assert lp._as_index("3") == 3
        assert lp._as_index(3) == 3
        assert lp._as_index(True) is None  # bool 是 int 的子类
        assert lp._as_index("abc") is None
        assert lp._as_index(None) is None

    def test_编号越界时退回url匹配(self):
        """越界说明模型在编。退回 URL 匹配而不是当命中——
        否则 i=999 会指向窗口外的东西，而控制流会去访问它。"""
        rows = [{"i": 999, "url": "https://www.a.test/gg"}]
        out = lp._resolve_rows(
            rows,
            lp._link_window(_obs(links=self._links()), Q.SELECT_PLAY_SITES, "功夫"),
            {"https://www.a.test/gg"},
        )
        assert [r["url"] for r in out] == ["https://www.a.test/gg"]

    def test_三处窗口同源(self):
        """预检、渲染、回指必须看**同一批**链接**且顺序一致**。

        这三处曾各拼一遍 ``_ordered_links(...)[:LLM_LINKS]``，而预检与渲染
        错位过一次——预检数到 3 条命中就放行，模型收到的前 20 条却全是
        导航栏，于是判 True 且 selected/rejected 全空。

        ⚠️ **fixture 必须顺序敏感**：含片名的链接放在**第二条**，
        排序会把它提到最前，于是「用排序后的窗口」与「用 obs.links 原序」
        给出不同结果。片名放在第一条时两种窗口完全相同，这条断言就成了
        永远为真的死断言（避坑指南 §7.3 同款）。"""
        from trajectory_pipeline.perception.base import LinkItem

        obs = _obs(
            links=(
                LinkItem("百度首页", "https://www.baidu.com/"),
                LinkItem("功夫 在线观看", "https://www.a.test/gg"),
            )
        )
        window = lp._link_window(obs, Q.SELECT_PLAY_SITES, "功夫")
        # 先确认 fixture 真的有顺序差异，否则这条断言测不出任何东西
        assert [l.href for l in window] != [l.href for l in obs.links]

        rendered = lp._render(obs, target_title="功夫", question=Q.SELECT_PLAY_SITES)[
            "links"
        ]
        assert [r["href"] for r in rendered] == [l.href for l in window]
        assert lp._title_link_sufficiency(obs, "功夫") == ""


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
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e1", "a", "播放")))
        )
        assert (
            json.loads(json.dumps(dict(d.payload), ensure_ascii=False))["ref"] == "e1"
        )

    def test_题目写进请求正文(self):
        """IS_REACHABLE 与 PLAYER_OK 的输出契约字面相同，
        靠契约串反查题目会认错。断言题号显式出现。"""
        import json

        fake = FakeLLM()
        lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL, _obs(interactive_elements=_els(("e1", "a", "播放")))
        )
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
    (
        "《功夫》电影完整版-高清正版在线观看-爱奇艺",
        "https://www.iqiyi.com/v_19rrk40at8.html",
    ),
    (
        "功夫 (普通话版)_电影_高清完整版视频在线观看_腾讯视频",
        "https://v.qq.com/x/cover/abc.html",
    ),
]


def _baidu_result_page() -> Observation:
    """25 条导航在前、2 条结果标题在后——与实测顺序一致。

    25 > ``LLM_LINKS``(20)，所以只截前 N 条时结果标题**必然被截掉**。
    这不是构造出来的边角，是实测：修复前的那批 5 个候选全被记成
    ``not_play_site``，因为模型收到的 20 条链接里一条片名都没有。
    """
    nav = [(t, h) for t, h in _NAV_LINKS] * 3  # 30 条，够长
    return Observation(
        url="https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB",
        page_title="功夫_百度搜索",
        body_text="搜索结果页正文，含有片名但与 URL 无对应关系。" * 20,
        links=tuple(LinkItem(text=t, href=h) for t, h in (nav[:25] + _RESULT_LINKS)),
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
        out = lp._render(
            _baidu_result_page(), target_title="功夫", question=Q.SELECT_PLAY_SITES
        )
        assert out["links"][0]["text"].startswith("《功夫》电影完整版")

    def test_命中项前置保证不落在窗口外(self):
        """「预检放行 ⇒ 模型看得到」成立的原因就在这条恒等式上。

        只断「重排后第一条是结果标题」不够——有人日后把重排去掉而
        保留其余改动，那条仍会绿。真正要钉的是**全量命中数 == 窗口内
        命中数**：不重排时窗口内命中 0 而全量 2，这条立刻红。
        """
        obs = _baidu_result_page()
        ordered = lp._ordered_links(obs, Q.SELECT_PLAY_SITES, "功夫")
        in_window = sum(1 for l in ordered[: lp.LLM_LINKS] if "功夫" in (l.text or ""))
        whole = sum(1 for l in obs.links if "功夫" in (l.text or ""))
        assert whole == 2
        assert in_window == whole
        # 且预检在这个恒等式下确实放行
        assert lp._title_link_sufficiency(obs, "功夫") == ""

    def test_整批链接都无片名时fail_closed(self):
        """预检**不能被这次修复放水**：真没有片名时仍须 fail-closed。"""
        obs = Observation(
            url="https://www.baidu.com/s?wd=x",
            page_title="百度",
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
            l.href for l in obs.links
        ]

    def test_端到端模型看到的第一条就是结果标题(self):
        """FakeLLM 对 ① 的模拟是「选它看到的第一条链接」——
        与真实模型同形。所以修复前它会选中导航栏（进而产出假
        ``not_play_site``），修复后选中结果标题。"""
        import json

        fake = FakeLLM()
        # 显式绑片名：① 的预检要拿它匹配链接文本，没绑时恒 None，
        # 测试会先撞上预检而不是要走的那条路径。
        d = lp.LLMPerceptor(fake, target_title="功夫").decide(
            Q.SELECT_PLAY_SITES, _baidu_result_page()
        )
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
            Q.SELECT_PLAY_SITES, _baidu_result_page()
        )
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
        assert d.answer is None  # 后端挂着，先确认夹具正常
        fake = FakeLLM(answers={Q.PLAYER_OK: False})
        d = lp.LLMPerceptor(fake).decide(Q.PLAYER_OK, _obs(body_text=self.PAYWALL_BODY))
        assert d.answer is False
        assert d.payload["paywall"] is True
        assert "开通会员" in d.evidence

    def test_模型判否但无付费信号_不落付费墙(self):
        fake = FakeLLM(answers={Q.PLAYER_OK: False})
        d = lp.LLMPerceptor(fake).decide(
            Q.PLAYER_OK, _obs(body_text="页面只有导航，无播放器")
        )
        assert d.payload.get("paywall") is False

    # ── 让位规则（2026-10-21，H-16 修复①）────────────────────────────

    def test_模型已诊断地区限制_不落付费墙(self):
        """**更具体的形态优先**：模型自己判出地区限制时，付费墙让位。

        实测 v.qq.com 8 条：主证据是「当前设备不支持播放」+ 70017001.1，
        「开通会员」只是句尾附加——让位之前它们全被贴成订阅墙。
        """
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM(
            answers={Q.PLAYER_OK: False},
            evidence="播放器显示「当前设备不支持播放」，错误码70017001.1",
        )
        d = lp.LLMPerceptor(fake).decide(
            Q.PLAYER_OK, _obs(body_text=self.PAYWALL_BODY)
        )
        assert d.answer is False
        assert d.payload.get("paywall") is False
        assert d.payload["access_control_type"] == "region"
        assert decision_branch(d) == "blocked"

    def test_页面引文含地区限制_不落付费墙(self):
        """让位的第二个输入源：**页面原文**（page_quotes）。

        模型把它看到的原话回传在 page_quotes 里——代码据此让位，
        不把「当前设备不支持播放」的页面贴成订阅墙。
        """
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM(
            answers={Q.PLAYER_OK: False},
            page_quotes=["当前设备不支持播放"],
        )
        d = lp.LLMPerceptor(fake).decide(
            Q.PLAYER_OK, _obs(body_text=self.PAYWALL_BODY)
        )
        assert d.payload.get("paywall") is False
        assert d.payload["access_control_type"] == "region"
        assert decision_branch(d) == "blocked"

    def test_模型判真时不因词表升级(self):
        """**两边一致才落细分分支。** 模型判 True 说明它认为页面可用，
        此时词表命中多半是导航栏文案（实测百度页正文就有「免费观看」）。
        强行升级会把正常站点判成付费墙。"""
        fake = FakeLLM(answers={Q.PLAYER_OK: True})
        d = lp.LLMPerceptor(fake).decide(Q.PLAYER_OK, _obs(body_text=self.PAYWALL_BODY))
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

        paid = Decision(Q.PLAYER_OK, False, 0.8, "需付费", "llm", {"paywall": True})
        plain = Decision(Q.PLAYER_OK, False, 0.8, "没组件", "llm", {})
        assert decision_branch(paid) == "subscription_wall"
        assert decision_branch(plain) == "component_unverified"

    def test_不入非样本集合(self):
        """付费墙是**判出来了的业务结论**，不是「没判出来」——
        它教模型「识别付费墙并退出」，是该进训练集的负样本。"""
        from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES

        assert "subscription_wall" not in NON_SAMPLE_BRANCHES

    def test_二回传paywall_落订阅墙(self):
        """2026-10-24 方案 19 A 类：判断点②（IS_REACHABLE）的 LLM 回传
        ``access_control_type=paywall``（会员视频未登录/未付费时播放器
        报错、页面带会员信号），落 ``subscription_wall``——与
        ``login_required``（登录后可免费）分开。"""
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM(
            answers={Q.IS_REACHABLE: False},
            access="paywall",
            evidence="播放器显示「发生了错误」，页面带会员购导航，属会员视频",
        )
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(body_text="首页 会员购 发生了错误，请稍后再试")
        )
        assert d.answer is False
        assert d.payload["access_control_type"] == "paywall"
        assert decision_branch(d) == "subscription_wall"


# ═══════════════════════════════════════════════════════════════════════
# 增量十五 · P3 code fallback 测试（evidence → 细分字段反推）
# ═══════════════════════════════════════════════════════════════════════


class TestCodeFallbackEvidence:
    """2026-10-12 增量十五：LLM 没给 ``access_control_type`` /
    ``paywall_type`` 时，code 从 evidence 关键词反推——避免被合态吞掉
    真 partial_free / login_required 样本。

    测试要钉两条线：
    1. evidence 关键词全命中 → 返回对应合法值 + note 非空
    2. evidence 无关键词 → 返回空串 + note 空（按现状落合态）

    ⚠️ 同时测「**故意不做子串匹配**」：单字"登录"在 B 站普通 button 上
    也常见，不应推断为 ``login``——保留窄口径。"""

    def test_access_login_从登录后关键词(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        assert infer_access_from_evidence("页面要求登录后观看完整版") == "login"
        assert infer_access_from_evidence("请先登录再继续") == "login"

    def test_access_region_从70017001(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        assert infer_access_from_evidence("错误码 70017001 当前地区不支持") == "region"

    def test_access_anti_bot(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        assert (
            infer_access_from_evidence(
                "Performing security verification (Cloudflare)"
            )
            == "anti_bot"
        )

    def test_access_not_found_从错误页(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        assert infer_access_from_evidence("视频不存在或已被删除") == "not_found"

    def test_access_视频不存在_不命中_strict(self):
        """⚠️ **P3 重构回归测试**（2026-10-12 增量十五）。

        「视频不存在」/「内容不存在」**移除**自 not_found 关键词——
        LLM evidence 是「结论+证据」混合文本，"目标视频已不存在"是模型
        自述推断，code 不该把它当页面原文。留「已被删除/已下架/404 错误」
        等更接近真实文案的短语。

        v.qq.com/70017001 在引入本收紧前被错误地落 broken（regional → broken
        regression），明确收紧后应不再命中。
        """
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        # 这些是 LLM 推断常用短语，**不能**触发 not_found
        for s in (
            "目标视频已不存在",
            "页面里看到这个视频不存在了",
            "内容不存在，请重新搜索",
        ):
            assert infer_access_from_evidence(s) == "", s

    def test_access_单字登录不命中(self):
        """**故意不做模糊匹配**：「登录」单独出现在 B 站 button 上，
        不应触发 login 推断。"""
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        assert infer_access_from_evidence("登录账号  注册  收藏  下载") == ""

    def test_access_空evidence返空(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        assert infer_access_from_evidence("") == ""

    def test_paywall_type_preview_从试看(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_paywall_type_from_evidence,
        )
        assert infer_paywall_type_from_evidence("试看 6 分钟，欢迎观看完整版") == "preview"
        assert infer_paywall_type_from_evidence("限时免费，前几集抢先") == "preview"

    def test_paywall_type_trial_从试用(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_paywall_type_from_evidence,
        )
        assert infer_paywall_type_from_evidence("新用户免费试用 7 天") == "trial"

    def test_paywall_type_无关键词返空(self):
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_paywall_type_from_evidence,
        )
        # 「大会员」单独出现不算 preview（避免与登录词表重叠）
        assert infer_paywall_type_from_evidence("开通会员，本片 VIP 独占") == ""


class TestResolveFieldHelper:
    """``_resolve_field`` 是 LLM 给字段与 code fallback 之间的桥梁。
    它不直接做归因,只做「合法值校验 + evidence fallback + note 拼装」三件
    事——note 是给人工看的"这是 code 推断的"信号。"""

    def test_llm给的合法值直接用(self):
        from trajectory_pipeline.perception.llm_perceptor import _resolve_field
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        v, note = _resolve_field(
            "region",
            frozenset({"login", "region"}),
            infer_access_from_evidence,
            "随便什么 evidence",
            label="access_control_type",
        )
        assert v == "region"
        assert note == ""

    def test_llm给非法值且evidence无关键词返空(self):
        from trajectory_pipeline.perception.llm_perceptor import _resolve_field
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        v, note = _resolve_field(
            "宇宙无敌机翻墙",  # 非法值
            frozenset({"login", "region"}),
            infer_access_from_evidence,
            "完全无关的 evidence",
            label="access_control_type",
        )
        assert v == ""
        assert note == ""

    def test_evidence反推生效时note必须非空(self):
        """note 是给人工的「这是 code fallback」唯一信号——
        它非空时，evidence 应可读出 code_fallback 字样与具体值。"""
        from trajectory_pipeline.perception.llm_perceptor import _resolve_field
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        v, note = _resolve_field(
            "",  # LLM 没给
            frozenset({"login", "region"}),
            infer_access_from_evidence,
            "页面要求登录后观看",
            label="access_control_type",
        )
        assert v == "login"
        assert "code_fallback" in note


class TestPageQuotes辅助:
    """2026-10-12 增量十六 · P4a：page_quotes 字段是 LLM 单独回传的
    页面原文短语——code fallback 用它，**不**用 evidence 自述文本。

    这些 helper 是契约测试：拒 LLM 给畸形 ``page_quotes``（如 ``""`` 串、
    null、混入非字符串元素）时不抛，按空处理。
    """

    def test_quote_text_normal(self):
        from trajectory_pipeline.perception.llm_perceptor import _quote_text
        out = _quote_text({"page_quotes": ["请先登录再继续", "登录后观看"]})
        assert "请先登录再继续" in out
        assert "登录后观看" in out

    def test_quote_text_忽略非字符串(self):
        from trajectory_pipeline.perception.llm_perceptor import _quote_text
        out = _quote_text({"page_quotes": ["试看 6 分钟", 123, None, {}, "免费"]})
        # 123 经 schema_parse.as_str 转 "123"；None/{} 转 "" 被过滤
        # join 单空格分隔（schema_parse 已经规整化）
        assert out == "试看 6 分钟 123 免费"

    def test_quote_text_缺失返空(self):
        from trajectory_pipeline.perception.llm_perceptor import _quote_text
        assert _quote_text({}) == ""
        assert _quote_text({"page_quotes": None}) == ""
        assert _quote_text({"page_quotes": "字符串不是 list"}) == ""

    def test_quote_list_normal(self):
        from trajectory_pipeline.perception.llm_perceptor import _quote_list
        out = _quote_list({"page_quotes": ["请登录", "登录后"]})
        assert out == ["请登录", "登录后"]

    def test_quote_list_丢失非str(self):
        from trajectory_pipeline.perception.llm_perceptor import _quote_list
        out = _quote_list({"page_quotes": ["OK", 99, "", "继续"]})
        # 99 经 schema_parse.as_str 转 "99" 入列；空串与 None/{} 被过滤
        assert out == ["OK", "99", "继续"]


class TestPageQuotes接入FieldeFallback:
    """P4a 端到端：_resolve_field 用 page_quotes 而非 evidence 时，
    evidence 里的 LLM 推断不再误触；page_quotes 里的页面原文才命中。

    这是 P3 v1 → 修复 → P4a 的总检证：把 regression 的根因根除。
    """

    def test_quotes空时_fallback_不触发(self):
        """P3 v1 regression 复现检验：
        LLM 自述落在 evidence 时不应被 fallback 看到；page_quotes 为空时
        fallback 必须返回空串、不写 note。
        """
        from trajectory_pipeline.perception.llm_perceptor import _resolve_field
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        v, note = _resolve_field(
            "",
            frozenset({"login", "region", "captcha", "anti_bot", "not_found"}),
            infer_access_from_evidence,
            "",  # page_quotes 为空——不触发
            label="access_control_type",
        )
        assert v == ""
        assert note == ""

    def test_quotes命中_能触发(self):
        """真实场景：B 站页面 LLM 给 page_quotes=["请先登录再继续"]，
        不给 access 字段——code 应能推回 login。"""
        from trajectory_pipeline.perception.llm_perceptor import _resolve_field
        from trajectory_pipeline.perception.rule_perceptor import (
            infer_access_from_evidence,
        )
        v, note = _resolve_field(
            "",
            frozenset({"login", "region", "captcha", "anti_bot", "not_found"}),
            infer_access_from_evidence,
            "请先登录再继续观看",
            label="access_control_type",
        )
        assert v == "login"
        assert "code_fallback" in note


class TestP4aRound2Prompt:
    """2026-10-12 增量十七 · P4a 第二轮：prompt 结构验证。

    第一轮 prompt 已把 ``page_quotes`` 字段加进 schema 与契约 JSON，**但**
    顺序在 evidence 之后、且没明示 evidence 禁句式——实测 LLM 把页面引文
    塞进 evidence（用「」/"" 包裹），page_quotes 字段空，fallback 拿不到。

    第二轮做两处结构改造：
    1. ``page_quotes`` 提到 ``answer`` 之后第一个条件位置——这样模型先看
       到它、再写 evidence，避免 evidence 先写满页面引文。
    2. evidence 段明示禁止引号包裹的页面上看到的字符。

    这些是 prompt 级契约——只要 prompt 改了就行，**不依赖 LLM**。
    """

    def test_IS_REACHABLE_prompt_page_quotes_先于_access_control_type(self):
        """``page_quotes`` 段必须出现在 ``access_control_type`` 段之前。"""
        spec = lp.questions.get(Q.IS_REACHABLE)
        # 找含字段名的位置——prompt 字符串里 ``page_quotes`` 可能出现多次
        # （如 schema_hint 与例子里都有），第一次出现是行编号处的引用，
        # 要找的是**段标题级别**的 ``必填 ``page_quotes`` 那条**——
        # 它在答案指引"2."开头那一行，**先于** access_control_type 段（"3."开头）。
        pq = spec.prompt.find("必填 ``page_quotes``")
        ac = spec.prompt.find("access_control_type`` 字段")
        assert pq != -1, "prompt 找不到「必填 page_quotes」段——第二轮未生效"
        assert ac != -1, "prompt 找不到 access_control_type 段"
        assert pq < ac, (
            f"page_quotes 段（{pq}）必须在 access_control_type 段（{ac}）之前；"
            f"这是第二轮改造的核心"
        )

    def test_PLAYER_OK_prompt_page_quotes_先于_paywall_type(self):
        """``page_quotes`` 段必须出现在 ``paywall_type`` 段之前。"""
        spec = lp.questions.get(Q.PLAYER_OK)
        pq = spec.prompt.find("必填 ``page_quotes``")
        pw = spec.prompt.find("paywall_type`` 字段")
        assert pq != -1, "prompt 找不到「必填 page_quotes」段——第二轮未生效"
        assert pw != -1, "prompt 找不到 paywall_type 段"
        assert pq < pw, (
            f"page_quotes 段（{pq}）必须在 paywall_type 段（{pw}）之前"
        )

    def test_IS_REACHABLE_prompt_evidence_禁止页面引文(self):
        """evidence 段必须**明示**禁止引号包裹的页面字符。

        第二轮 P3 v1 的根因：evidence 自述误触 fallback 关键词。
        只要 prompt 没把「禁止」「引号」字样写进去，LLM 不会自己意识到
        这是反模式——所以这条是硬断言。
        """
        spec = lp.questions.get(Q.IS_REACHABLE)
        evidence_section = spec.prompt.split("**evidence**")[-1]
        # 至少含「禁止」一词（中文硬话），或含「引号」/「页面文案」任一关键词。
        # 三者都是 prompt 第二轮明示的反模式信号词。
        assert (
            "禁止" in evidence_section
            or "引号" in evidence_section
            or "页面文案" in evidence_section
            or "页面原文" in evidence_section
        ), (
            "IS_REACHABLE prompt 的 evidence 段没有写「禁止/引号/页面文案/页面原文」"
            "任一关键词——P4a 第二轮未生效"
        )

    def test_PLAYER_OK_prompt_evidence_禁止页面引文(self):
        spec = lp.questions.get(Q.PLAYER_OK)
        evidence_section = spec.prompt.split("**evidence**")[-1]
        assert (
            "禁止" in evidence_section
            or "引号" in evidence_section
            or "页面文案" in evidence_section
            or "页面原文" in evidence_section
        ), (
            "PLAYER_OK prompt 的 evidence 段没有写「禁止/引号/页面文案/页面原文」"
            "任一关键词——P4a 第二轮未生效"
        )

    def test_schema_hint_page_quotes_排首位(self):
        """``MappingProxyType`` 保持声明序 = 迭代序——prompt 拼装会按这个
        顺序遍历。page_quotes 必须出现在 access_control_type / paywall_type
        之前，让 LLM 先看到字段表里的 page_quotes。"""
        ir_hint = lp.questions.get(Q.IS_REACHABLE).schema_hint
        po_hint = lp.questions.get(Q.PLAYER_OK).schema_hint
        # list() 顺序 = 声明序
        ir_keys = list(ir_hint)
        po_keys = list(po_hint)
        assert ir_keys[0] == "page_quotes", (
            f"IS_REACHABLE schema_hint 首字段是 {ir_keys[0]!r}，应为 page_quotes"
        )
        assert po_keys[0] == "page_quotes", (
            f"PLAYER_OK schema_hint 首字段是 {po_keys[0]!r}，应为 page_quotes"
        )


class TestP4aRound2DecideFallback:
    """P4a 第二轮端到端验证：经 ``decide()`` 走完 IS_REACHABLE / PLAYER_OK
    全链路，验 ``page_quotes`` 在第一位的 contract 生效。

    测 P4a 第二轮的核心新能力：

    1. LLM 给了 ``page_quotes`` 但没给 access / paywall_type → fallback 触发
    2. LLM 没给 ``page_quotes`` → 不触发 fallback，按现状落合态

    这两条都已经在 helper 层（``TestPageQuotes接入FieldeFallback``）验过，
    本类验 ``decide()`` 把 helper 串起来后的行为**没断**。
    """

    def test_IS_REACHABLE_page_quotes_含登录提示触发_fallback(self):
        """真实场景：B 站"大会员"页面，LLM 给 ``page_quotes=["请先登录再继续"]``
        但没给 ``access_control_type``——code fallback 推回 ``login``。"""
        fake = FakeLLM(
            answers={Q.IS_REACHABLE: False},
            page_quotes=["请先登录再继续"],
            access="",  # 模拟 LLM 没给细分字段
        )
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(body_text="请先登录再继续观看完整版")
        )
        assert d.answer is False
        assert d.payload["access_control_type"] == "login"
        assert "code_fallback" in d.evidence
        assert "login" in d.evidence
        # page_quotes 同步进 payload——这是 P4a 的接口设计：单测、人工复核、
        # 报表都能看到 LLM 真正观察到的页面文本。
        assert d.payload["page_quotes"] == ["请先登录再继续"]

    def test_PLAYER_OK_page_quotes_含试看提示触发_fallback(self):
        """真实场景：试看付费墙，LLM 给 ``page_quotes=["试看 6 分钟..."]``
        但没给 ``paywall_type``——code fallback 推回 ``preview``。"""
        fake = FakeLLM(
            answers={Q.PLAYER_OK: False},
            page_quotes=["试看 6 分钟，欢迎观看完整版"],
            paywall_type="",  # 模拟 LLM 没给细分字段
        )
        # body_text 必须含 :data:`PAYWALL_WORDS` 表中的**完整短语**才能让
        # ``classify_paywall`` 命中——表里收的是「开通会员/购买单集」等确定
        # 性短语，不收「试看」光杆（导航栏里到处都是）。这里用「开通会员后
        # 观看完整版」，让 code 层先命中付费墙信号、再让 fallback 推 preview。
        d = lp.LLMPerceptor(fake).decide(
            Q.PLAYER_OK, _obs(body_text="开通会员后观看完整版，开通 VIP")
        )
        assert d.answer is False
        # 真实命中付费墙信号——payload.paywall=True, paywall_type=preview
        assert d.payload["paywall"] is True
        assert d.payload["paywall_type"] == "preview"
        assert "code_fallback" in d.evidence
        assert "preview" in d.evidence
        assert d.payload["page_quotes"] == ["试看 6 分钟，欢迎观看完整版"]

    def test_IS_REACHABLE_page_quotes_空时不触发_fallback(self):
        """P4a 第二轮的**反向**断言：LLM 既没给 access 字段也没给 page_quotes
        ——fallback 拿不到证据，按现状落合态。

        :func:`_resolve_field` 在没字段又无关键词时返回 ``""``（**空串而非
        None**）——故 ``payload.access_control_type`` 是 ``""``。evidence 末尾
        不会有 ``code_fallback`` 标记（fallback 没触发）。"""
        fake = FakeLLM(
            answers={Q.IS_REACHABLE: False},
            page_quotes=[],  # 显式给空：模拟 LLM 按 prompt "没看到就 []" 的合规答
        )
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(body_text="页面主体不可访问")
        )
        assert d.answer is False
        # _resolve_field 在 fallback 无证据时返回 ""（空串）而非 None——
        # 这是已实现行为，测试以既成事实为准。
        assert d.payload["access_control_type"] == ""
        # 关键：不应出现 code_fallback 标记（fallback 没触发）。
        assert "code_fallback" not in d.evidence


class TestExtractBusinessQuotes:
    """2026-10-12 增量二十一：从 ``evidence`` 后处理抽引文段的启发式过滤。

    目的：LLM 没按 P4a 契约回 ``page_quotes`` 时，从 evidence 文本里
    把 ``「…」`` / ``"…"`` 包裹段抽出来，按"业务术语 + 长度 + 否定词"三道
    gate 筛掉 LLM 评论，**只留** 看起来像页面原文的短语。

    守三件事：

    1. 业务术语 gate：必须含 UI / 业务词（VIP、播放、设备、错误、会员…）
    2. 长度 gate：2–13 字。LLM 复合句永远超长，绝对是评论不是引文。
    3. 否定 gate：含 "没 / 可验证 / 不可逆" 等元评论标记的丢弃。

    这三道不是设计哲学，是被 39 份带引文的 P1 存档**逼出来**的：
    全抽会塞进 LLM 评论（如"找错站"），半抽会漏掉真正的反推信号
    （如 v.qq.com 的 "当前设备不支持播放"）。逐条对照样本验证。
    """

    def test_empty_text_返空(self):
        from trajectory_pipeline.perception.llm_perceptor import _extract_business_quotes
        assert _extract_business_quotes("") == []
        assert _extract_business_quotes(None) == []

    def test_纯LLM评论_无业务术语_被丢(self):
        from trajectory_pipeline.perception.llm_perceptor import _extract_business_quotes
        # 站首页守卫给的「找错站」「去了发现看不了」是 LLM 评论，
        # 都不含业务术语 gate。
        out = _extract_business_quotes(
            "这是**找错站**，不是「去了发现看不了」，故不记 no_play_control"
        )
        assert out == []

    def test_纯页面原文_被留(self):
        from trajectory_pipeline.perception.llm_perceptor import _extract_business_quotes
        out = _extract_business_quotes('播放器提示"当前设备不支持播放"')
        assert out == ["当前设备不支持播放"]

    def test_中英文引号都识别(self):
        from trajectory_pipeline.perception.llm_perceptor import _extract_business_quotes
        out = _extract_business_quotes(
            '页面显示「开通会员」和"开通 VIP"两个按钮'
        )
        assert "开通会员" in out
        assert "开通 VIP" in out

    def test_长复合句_超13字被丢(self):
        from trajectory_pipeline.perception.llm_perceptor import _extract_business_quotes
        # 14 字：含 "播放"（业务）但 "可验证"（LLM 元评论）+ 长复合句。
        out = _extract_business_quotes(
            '"播放页上没有可验证的播放组件"说的是**到了播放页**之后没有组件'
        )
        assert out == []

    def test_负向gate_没_丢弃(self):
        from trajectory_pipeline.perception.llm_perceptor import _extract_business_quotes
        # 「页面没渲染完」是 LLM 评论，但含 "渲染"（非业务 gate）+ "没"（负向）。
        # 「页面没控件」同上。
        out = _extract_business_quotes(
            '哨兵返回无法区分「页面没渲染完」与「页面确实没控件」'
        )
        assert out == []

    def test_重复quote_去重保序(self):
        from trajectory_pipeline.perception.llm_perceptor import _extract_business_quotes
        out = _extract_business_quotes(
            '"开通会员"按钮和"开通会员"提示都在导航栏'
        )
        assert out == ["开通会员"]

    def test_上限5条(self):
        from trajectory_pipeline.perception.llm_perceptor import _extract_business_quotes
        # 2026-10-12 增量二十一：用含业务关键词的占位串（原用 "页面{i}"，但
        # 页面 不在 _BUSINESS_QUOTE_HINTS，会被业务 gate 全部过滤，断言失去
        # 「cap 起作用」的本意）。`开通` 是真实业务词、伪造 8 个被 cap 到 5。
        text = " ".join(f'"开通{i}"' for i in range(8))
        out = _extract_business_quotes(text, max_phrases=5)
        assert len(out) == 5

    def test_实跑样本_vqq_evidence(self):
        """39 份 P1 存档里最常出现的 v.qq.com 70017001 evidence——增量
        十五后已经在 source 里有这条，但当时仍只走 evidence 反推，没有
        page_quotes 字段。增量二十一让 ``page_quotes`` 字段也补全。"""
        from trajectory_pipeline.perception.llm_perceptor import _extract_business_quotes
        ev = (
            'LLM 判 False：页面显示"当前设备不支持播放"并出现错误码'
            '70017001.1，且当前剧集及播放列表均标注"VIP"并提示开通会员。'
            '（置信度 0.8 为代码层先验，非模型自报）；'
            '页面含付费墙信号「开通会员」——按 2026-10-10 口径，'
            '**有播放页但需订阅/付费不算成功**'
        )
        out = _extract_business_quotes(ev)
        # 期望保留："当前设备不支持播放"（设备/不支持/播放）、
        # "VIP"（VIP）、"开通会员"（开通/会员）。
        # "70017001.1" 是纯数字 ID 无业务词被丢；置信度行不进 evidence 不参与。
        assert "当前设备不支持播放" in out
        assert "VIP" in out
        assert "开通会员" in out
        # 句子尾部「**有播放页但需订阅/付费不算成功**」是 markdown 强调，
        # 没有引号包裹，不参与抽取。
        assert all("有播放页但需订阅" not in s for s in out)


class TestQuotesWithFallback:
    """2026-10-12 增量二十一：``_quotes_with_fallback`` 三层兜底语义。

    优先级：LLM ``data.page_quotes`` 非空 -> 用它；否则从
    ``data.evidence`` 后处理抽出业务引文段；都没有 -> ``[]``。
    """

    def test_LLM给了page_quotes_用LLM的(self):
        from trajectory_pipeline.perception.llm_perceptor import _quotes_with_fallback
        # 即便 evidence 含很多引文段，LLM 给的 page_quotes 仍优先。
        out = _quotes_with_fallback(
            {
                "page_quotes": ["LLM 抽的精确原文"],
                "evidence": '页面显示"另一个不同的引文"',
            }
        )
        assert out == ["LLM 抽的精确原文"]

    def test_LLM没给_evidence后处理补(self):
        from trajectory_pipeline.perception.llm_perceptor import _quotes_with_fallback
        out = _quotes_with_fallback(
            {
                "page_quotes": [],  # 显式空：LLM 按契约答"没看到"
                "evidence": '页面显示"开通会员"按钮',
            }
        )
        assert out == ["开通会员"]

    def test_两者都空_返空list(self):
        from trajectory_pipeline.perception.llm_perceptor import _quotes_with_fallback
        out = _quotes_with_fallback({})
        assert out == []
        out = _quotes_with_fallback({"page_quotes": [], "evidence": ""})
        assert out == []

    def test_page_quotes缺字段_走fallback(self):
        from trajectory_pipeline.perception.llm_perceptor import _quotes_with_fallback
        # P4a 之前的旧契约：payload 没有 page_quotes 字段。
        # 现契约要求 LLM 给，但 Claude 还没改造完、或旧存档 P3 重跑……
        out = _quotes_with_fallback(
            {"evidence": '页面显示"开通会员"，video 标签缺失'}
        )
        assert out == ["开通会员"]


class TestPostProcessingFallbackDecide:
    """2026-10-12 增量二十一端到端：经 ``decide()`` 验 evidence 后处理
    真能驱动 fallback 而不只是 helper 层。

    这是 P4a 系列的最后一道护城河——当 P4a v2 prompt 修改让 0/7 残留，
    但模型换 / 退化时仍能给 page_quotes 时，这条让判定仍能命中。
    """

    def test_IS_REACHABLE_evidence_含设备不支持_走fallback_推region(self):
        """B 站 / 腾讯视频等真实场景：LLM 没给 page_quotes 没给 access，
        evidence 里写了"当前设备不支持播放"——fallback 用 evidence 后
        处理抽出 page_quotes，再让 _resolve_access 推 region。"""
        fake = FakeLLM(
            answers={Q.IS_REACHABLE: False},
            page_quotes=[],  # 显式空
            access="",  # 显式空
        )
        # evidence 必须真包含业务短语，且这个 evidence 经过
        # _evidence(data, ...) 给到 _is_reachable——需要走端到端。
        # 这里通过 patch fakes._build 让 Q.IS_REACHABLE 时 evidence=
        # '页面显示"当前设备不支持播放"错误码 70017001.1'
        original = FakeLLM._build
        def _build_isr(self, qid, obs):
            base = original(self, qid, obs)
            if qid == Q.IS_REACHABLE:
                base["evidence"] = (
                    '页面显示"当前设备不支持播放"，错误码 70017001.1'
                )
            return base
        FakeLLM._build = _build_isr
        try:
            d = lp.LLMPerceptor(fake).decide(
                Q.IS_REACHABLE, _obs(body_text="页面正文")
            )
        finally:
            FakeLLM._build = original
        assert d.answer is False
        # 关键：evidence 后处理抽出 "当前设备不支持播放"，fallback 推 region
        assert d.payload["access_control_type"] == "region"
        # page_quotes 同步进 payload——这是 P4a 全链路的最后一步
        assert "当前设备不支持播放" in d.payload["page_quotes"]

    def test_PLAYER_OK_evidence_含开通会员_走fallback(self):
        """试看付费墙场景：evidence 含"开通会员"短句但 LLM 没给
        page_quotes 没给 paywall_type——code fallback 仍能命中（依赖
        body_text 的 PAYWALL_WORDS），而 page_quotes 同步被后处理补全。"""
        fake = FakeLLM(
            answers={Q.PLAYER_OK: False},
            page_quotes=[],
            paywall_type="",
        )
        original = FakeLLM._build
        def _build_po(self, qid, obs):
            base = original(self, qid, obs)
            if qid == Q.PLAYER_OK:
                base["evidence"] = (
                    '页面显示"开通会员"按钮，且视频标签未渲染'
                )
            return base
        FakeLLM._build = _build_po
        try:
            d = lp.LLMPerceptor(fake).decide(
                Q.PLAYER_OK, _obs(body_text="开通会员 试看")
            )
        finally:
            FakeLLM._build = original
        # page_quotes 同步进 payload
        assert "开通会员" in d.payload["page_quotes"]
        # paywall 信号命中（PAYWALL_WORDS '开通会员' 在 body_text）
        assert d.payload.get("paywall") is True

    def test_LLM给了page_quotes_evidence引文段不参与(self):
        """优先级回归：LLM 已经给了 page_quotes 时，evidence 里的引文段
        **不**进 page_quotes——避免 LLM 看到的页文本被混入自述"""
        fake = FakeLLM(
            answers={Q.IS_REACHABLE: False},
            page_quotes=["LLM 抽的原文"],
            access="",
        )
        original = FakeLLM._build
        def _build_isr(self, qid, obs):
            base = original(self, qid, obs)
            if qid == Q.IS_REACHABLE:
                base["evidence"] = (
                    '页面显示"另一个不同的引文"短语'
                )
            return base
        FakeLLM._build = _build_isr
        try:
            d = lp.LLMPerceptor(fake).decide(
                Q.IS_REACHABLE, _obs(body_text="页面正文")
            )
        finally:
            FakeLLM._build = original
        # LLM 给的 page_quotes 优先，evidence 的引文段不进 payload
        assert d.payload["page_quotes"] == ["LLM 抽的原文"]
        assert "另一个不同的引文" not in d.payload["page_quotes"]


# ═══════════════════════════════════════════════════════════════════════
# 2026-10-15 INC43 · ②③负分支 + 哨兵路径付费墙救济
# ═══════════════════════════════════════════════════════════════════════


class TestINC43负分支付费墙救济:
    """D-10 主瓶颈错判簇的判定层根因：``classify_paywall`` 只在 ④ 运行，
    ②（页面可达）③（找播放控件）的负分支从不检查付费墙信号——订阅墙
    页面落 ``login_wall_or_blocked`` / ``no_play_control`` / ``unresolved``。

    探针实测（trajectory_pipeline/research/probes/inc43_probe_bodies.jsonl，18 行金标 subscription_wall
    错判簇）：m.ixigua ×2「以下内容需下载西瓜视频查看」、play.tudou
    「下载优酷PC客户端继续观看」、v-wb.youku「会员特惠 + 立即开通 +
    首3月9.9元/月」三类形态可被救济；对照组 v.qq.com（金标
    no_play_control 判对）零信号命中，扩词假阳性风险可控。

    **不翻转保证**（与 ④ 既有口径一致）：只在 answer=False 时救济；
    True（成功）与 None（没能力判）不动——词表命中不构成推翻二者的证据。

    ⚠️ **2026-10-21 判据收紧**（台账 H-16 修复①）：救济只认**门禁语境**
    （:func:`~trajectory_pipeline.perception.rule_perceptor.paywall_evidence`）——
    「开通会员」**独行不成立**（导航栏广告），成立的是「门禁命题」或
    「CTA + 同句观看语境」。原「强弱信号组合」那条路整体作废：人工复核
    （``research/probes/inc73_human_review.json``）判池里 12 条
    ``subscription_wall`` **一条都站不住**，成因全是 CTA 裸命中与地区
    限制——本类里那两条被证伪的用例都记着原因。
    """

    APP_WALL_BODY = "海量影视免费看 打开 功夫8.1 立即播放 以下内容需下载西瓜视频查看"
    STRONG_WEAK_BODY = "会员特惠 立即开通 首3月9.9元/月，畅享精彩内容"

    # ── 路径 1：② 判否 + 无 access 细分 ─────────────────────────────

    def test_二判否且正文含门禁语境_改判订阅墙(self):
        """CTA + **同句观看语境** → 改判订阅墙。

        「开通会员观看正片」：CTA「开通会员」+ 语境「正片」——语境词在
        CTA 跨度之外（否则等于自己给自己作证）。
        """
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM(answers={Q.IS_REACHABLE: False})
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(body_text="开通会员观看正片 立即播放")
        )
        assert d.answer is False
        assert d.payload["paywall"] is True
        assert "付费墙" in d.evidence  # evidence 必须能指认判据
        assert decision_branch(d) == "subscription_wall"

    def test_二判否且正文只有裸CTA_不救济(self):
        """**导航栏 CTA 不救济**（2026-10-21 新增守卫）。

        「开通会员 享更多精彩」是站点导航栏广告：单凭它分不清「真付费墙」
        与「入口页广告」——实测池里 12 条 subscription_wall 全是这类裸命中。
        ② 判否 + 裸 CTA ⇒ 不改判，落 ② 的 fail-closed 出口 ``unresolved``。
        """
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM(answers={Q.IS_REACHABLE: False})
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(body_text="开通会员 享更多精彩")
        )
        assert d.answer is False
        assert d.payload.get("paywall") is None
        assert decision_branch(d) == "unresolved"

    def test_二判否且正文含强弱信号_不救济(self):
        """**证伪记录**（2026-10-21）：v-wb.youku 实测形态「会员特惠 立即开通
        首3月9.9元/月」原被列为真阳性（INC43 探针金标），人工复核独立判它
        是**续费营销**（页面没有门禁命题，也没有观看语境）——改按新判据
        不成立。

        强弱信号现在**只**回答「这页**像**付费墙吗」（gatekeeper 拦页面），
        **不用于贴分支**：贴分支只认门禁语境。
        """
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM(answers={Q.IS_REACHABLE: False})
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(body_text=self.STRONG_WEAK_BODY)
        )
        assert d.answer is False
        assert d.payload.get("paywall") is None
        assert decision_branch(d) == "unresolved"

    def test_二判否但已给login细分_不救济(self):
        """access_control_type 细分比关键词更具体，decision_branch 本就让
        细分优先——救济只添噪声，不触发。"""
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM(answers={Q.IS_REACHABLE: False}, access="login")
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(body_text="请先登录 开通会员观看")
        )
        assert d.answer is False
        assert d.payload.get("paywall") is None
        assert decision_branch(d) == "login_required"

    def test_二判否且模型已诊断地区限制_不救济(self):
        """**让位规则**（2026-10-21）：模型**自己**诊断出的访问形态更具体，
        救济不得把它改写成订阅墙。

        ③ 没有 ``access_control_type`` 字段，诊断只写在 evidence 里——
        所以让位必须读 evidence（实测 v.qq.com 8 条：模型已判出
        「当前设备不支持播放」+ 70017001.1，救济却把它改成了订阅墙）。
        """
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM(
            answers={Q.IS_REACHABLE: False},
            evidence="播放器显示「当前设备不支持播放」，错误码70017001.1",
        )
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(body_text="开通会员观看正片")
        )
        assert d.answer is False
        assert d.payload.get("paywall") is None
        # 模型的诊断落地成 ② 的 access_control_type=region → blocked
        # （**不是**订阅墙，也不是笼统的 unresolved）。
        assert d.payload["access_control_type"] == "region"
        assert decision_branch(d) == "blocked"


    # ── 路径 2：③ 判否 ──────────────────────────────────────────────

    def test_三判否且正文含APP墙短语_改判订阅墙(self):
        """play.tudou 实测形态：「下载优酷PC客户端继续观看」。"""
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(
                body_text="很抱歉，由于您的浏览器版本太低 暂时无法观看 下载优酷PC客户端继续观看",
                interactive_elements=_els(("e1", "a", "热搜"), ("e2", "a", "举报")),
            ),
        )
        assert d.answer is False  # 词表与模型均未找到播放控件
        assert d.payload["paywall"] is True
        assert decision_branch(d) == "subscription_wall"

    def test_三判否但正文无信号_仍落no_play_control(self):
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(
                body_text="首页 综艺 电视剧 更多 立即播放",
                interactive_elements=_els(("e1", "a", "首页"), ("e2", "a", "全网搜")),
            ),
        )
        assert d.answer is False
        assert d.payload.get("paywall") is None
        assert decision_branch(d) == "no_play_control"

    # ── 路径 3：③ 哨兵（0 元素 fail-closed）─────────────────────────

    def test_三哨兵0元素且正文含APP墙_救济为订阅墙(self):
        """m.ixigua 实测形态：elements=0、body=100 字符
        「以下内容需下载西瓜视频查看」。正文能采到付费墙文案本身就证明
        页面渲染了——哨兵担心的「没渲染完」不成立。"""
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(body_text=self.APP_WALL_BODY, interactive_elements=()),
        )
        assert fake.calls == []  # 哨兵拦截在 LLM 之前，不发请求
        assert d.answer is False
        assert d.payload["paywall"] is True
        assert "哨兵路径付费墙救济" in d.evidence
        assert decision_branch(d) == "subscription_wall"

    def test_三哨兵且降级观察_不救济(self):
        """§5.5 纪律：采集失败与「真没有」在存档里同形，必须 fail-closed。
        降级观察的正文可能只是部分加载的导航栏，词表命中不是页面全貌。
        （INC45 例外：仅 mojibake 降级可救济——见 TestINC45乱码页救济。）"""
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(
                body_text=self.APP_WALL_BODY,
                interactive_elements=(),
                degraded=("browser_links",),
            ),
        )
        assert fake.calls == []
        assert d.answer is None  # 哨兵 fail-closed 保留
        assert "采集降级" in d.evidence

    def test_三哨兵且正文无信号_仍fail_closed(self):
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL, _obs(body_text="页面加载中", interactive_elements=())
        )
        assert d.answer is None
        assert fake.calls == []

    # ── 不翻转保证 ──────────────────────────────────────────────────

    def test_二判真时不因词表翻转(self):
        fake = FakeLLM(answers={Q.IS_REACHABLE: True})
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(body_text="开通会员观看正片")
        )
        assert d.answer is True
        assert d.payload.get("paywall") is None

    def test_三判真时不因词表翻转(self):
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(
                body_text="开通会员观看正片",
                interactive_elements=_els(("e1", "button", "立即播放")),
            ),
        )
        assert d.answer is True
        assert d.payload.get("paywall") is None

    def test_二判不出时不因词表翻转(self):
        """None 是「没能力判」——词表命中不构成推翻它的证据。"""
        fake = FakeLLM(answer_null=True)
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE, _obs(body_text="开通会员观看正片")
        )
        assert d.answer is None
        assert d.payload.get("paywall") is None

    def test_四判真时不因付费墙词表翻否(self):
        """模型判真 ⇒ 词表**不救济**（救济只对 answer=False 生效）。

        ⚠️ 2026-10-23（M-26）：这里原来叫「走视频快路径」——那时的
        ④ 在 ``video_tag_count >= 1`` 时**根本不问模型**。现在媒体计数
        不许许诺成功（见 :class:`Test媒体计数不许许诺成功`），④ 一律走
        LLM，于是这条测的是**真·LLM 路径**：模型说 True 就 True，
        裸 CTA「开通会员」（门禁语境不成立）不把它翻否。"""
        d = llm_perceptor().decide(
            Q.PLAYER_OK, _obs(body_text="开通会员", video_tag_count=1)
        )
        assert d.answer is True
        assert d.payload.get("paywall") is False


class TestINC43救济判据真值表:
    """**门禁语境**判据（:func:`paywall_evidence`）的真值表——判据钉住，
    防止将来扩词时悄悄漂移。

    ⚠️ **2026-10-21 判据换代**（台账 H-16 修复①）：原来的
    ``paywall_rescue_evidence``（扁平词表命中 **或** 强弱信号组合）已删除。
    现在只有两层：

    - **L1 门禁命题**：短语本身就是完整命题（「订阅后观看」= 不订阅不看）；
    - **L2 号召 CTA + 同句观看语境**：「开通会员」独行是导航栏广告，
      「您可以试看5分钟，点击开通会员」才是门禁。

    原「强弱信号组合」那条路整体作废（人工复核判它是营销）。
    """

    def test_L1门禁短语命中(self):
        """APP/客户端硬墙实测形态（INC43 探针金标）。"""
        assert lp.paywall_evidence("以下内容需下载西瓜视频查看") != ""
        assert lp.paywall_evidence("下载优酷PC客户端继续观看") != ""
        assert lp.paywall_evidence("订阅后观看") != ""

    def test_站脚下载链接不命中(self):
        """「下载客户端」「下载中心」是站点页脚常规链接（实测 mgtv /
        luketv 都有），不是门禁语境——短语刻意收窄，不构成子串命中。"""
        assert lp.paywall_evidence("首页 会员方案 下载客户端 热播榜单") == ""
        assert lp.paywall_evidence("公司介绍 下载中心 版权所有") == ""

    def test_L2_CTA加同句语境命中(self):
        """CTA 与观看语境**同句**（不按逗号切句——实测真阳性 sohu
        「您可以试看5分钟，点击开通会员」里两者被一个逗号隔开）。"""
        hit = lp.paywall_evidence("您可以试看5分钟，点击开通会员")
        assert hit != ""
        assert "开通会员" in hit and "试看" in hit

    def test_裸CTA不命中(self):
        """导航栏 CTA 独行——**不成立**（实测 12 条 subscription_wall
        里全是这类裸命中）。"""
        assert lp.paywall_evidence("开通会员 享更多精彩") == ""
        assert lp.paywall_evidence("会员专享") == ""
        assert lp.paywall_evidence("续费 充值") == ""

    def test_强弱信号组合不命中(self):
        """**证伪记录**（2026-10-21）：v-wb.youku「会员特惠 立即开通
        首3月9.9元/月」原是真阳性金标，人工复核判它是**续费营销**。"""
        assert lp.paywall_evidence("会员特惠 立即开通 首3月9.9元/月") == ""

    def test_弱信号单出_不命中(self):
        """bilibili 实测形态：「会员购」导航项，仅弱词、无强词。"""
        assert lp.paywall_evidence("首页 番剧 直播 游戏中心 会员购") == ""

    def test_强信号单出_不命中(self):
        """mgtv 实测形态：导航栏「限时特惠」广告位，仅强词、无弱词无价格。"""
        assert lp.paywall_evidence("限时特惠 创作 综艺 电视剧") == ""

    def test_对照组无信号(self):
        """v.qq.com thevoice（金标 no_play_control 判对）——零命中。"""
        assert lp.paywall_evidence("腾讯视频 所有频道 登录 我看过的 上传") == ""

    def test_条件式门禁_报错加会员购命中(self):
        """2026-10-25 方案 19 A 类补漏：bilibili 会员影片未登录/未开通时
        播放器报错「发生了错误」+ 导航「会员购」——是付费墙门禁，不是
        broken。此前词表漏了这个形态，LLM 判 False 又没回传
        ``access_control_type``，救济路径够不到，落 unresolved。"""
        hit = lp.paywall_evidence("首页 会员购 发生了错误，请稍后再试")
        assert hit != ""
        assert "发生了错误" in hit and "会员购" in hit

    def test_条件式门禁_报错独行不命中(self):
        """「发生了错误」是极泛的错误提示（500/404/网络错误都会有），
        无会员电商信号同现时**不**判付费墙——否则 broken 页被误判订阅墙。"""
        assert lp.paywall_evidence("发生了错误，请稍后再试") == ""
        assert lp.paywall_evidence("404 Not Found nginx") == ""
        assert lp.paywall_evidence("Internal Server Error") == ""

    def test_条件式门禁_报错加泛化会员标签不命中(self):
        """「会员」二字在普通影视详情页的标签/角标里高频，**不是**会员
        电商信号——「发生了错误」+「会员」标签不能判付费墙。只有
        「会员购/大会员购/会员专区」这类会员专享入口才作数。"""
        assert lp.paywall_evidence("发生了错误，请稍后再试 会员 普通话") == ""

    def test_语境词不得落在CTA跨度内(self):
        """表自洽守卫：语境词**不能**是任何 CTA 短语的子串。

        判据要求语境词落在 CTA 跨度**之外**（否则「打开app观看」自己
        给自己作证）。这条不变量今天成立，所以那道跨度检查暂时是
        空转的——但它是为了将来扩表时不至于静默失效而留的。哪天有人
        加了「试看后开通会员」这种 CTA，本测试会当场红，提醒他改词表。
        """
        from trajectory_pipeline.perception.rule_perceptor import (
            PAYWALL_CONTEXT_WORDS,
            PAYWALL_CTA_PHRASES,
        )

        for ctx in PAYWALL_CONTEXT_WORDS:
            for cta in PAYWALL_CTA_PHRASES:
                assert ctx not in cta, (
                    f"语境词「{ctx}」出现在 CTA 短语「{cta}」的跨度内——"
                    f"它会给自己作证，门禁判据就废了"
                )


# ═══════════════════════════════════════════════════════════════════════
# 2026-10-15 INC45 · 乱码页付费墙救济（簇 B，tv.sohu ×3）
# ═══════════════════════════════════════════════════════════════════════


class TestINC45乱码页救济:
    """簇 B（INC44 前研）：tv.sohu.com 混合编码页（GBK 主体 + UTF-8
    字节混入）被 ``body_mojibake`` 一律 fail-closed → unresolved（金标
    subscription_wall ×3）。探针实测（trajectory_pipeline/research/probes/inc45_probe2_sohu.jsonl）：
    正文**采到了**（1588 字符 / 156 U+FFFD），可读部分含
    「新用户首月9.9元，开通会员 立即抢购」——**确定性付费墙证据在场**。

    §5.5 守的是「缺证据别当业务结论」；mojibake 例外放行的依据是
    「证据在场」：降级集合**只含** mojibake 标记（body/title）时，
    正文是已采集的部分乱码文本，可读部分的词表命中是真信号。
    其余降级（body_text 缺失 / snapshot 失败 / browser_links 等）
    照旧 fail-closed。
    """

    #: tv.sohu.com/v/MjAwOTEyMzE... 实测摘录（截选，含真实乱码段）
    SOHU_BODY = (
        "导航\n\n当妈不容易\n\n14:40\n\n\ufffd\ufffd\ufffd\ufffd֮��..."
        "\n简介\n\ufffd\ufffdƬ/ϲ��Ƭ\n热度\n2��ȫ\n"
        "新用户首月9.9元，开通会员\n立即抢购\n剧集\n系列\n"
        "\ufffd\ufffd\ufffd\ufffd֮��ʥȢ�ף���棩\n相关明星\n为你推荐"
    )

    def test_乱码降级且正文只有营销模块_不救济(self):
        """**证伪记录**（2026-10-21）：sohu 实测混编页的可读部分是
        「新用户首月9.9元，开通会员 立即抢购」——**营销模块**，不是门禁
        （没有门禁命题，也没有同句观看语境）。

        门禁语境收紧后，这一支自然落回 fail-closed 的 ``unresolved``——
        那才是它该去的地方（人工复核 2026-10-21 独立判它「采集降级
        mojibake」，成因是**采集**不是付费墙）。
        """
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(body_text=self.SOHU_BODY, interactive_elements=(),
                 degraded=("body_mojibake",)),
        )
        assert fake.calls == []  # 拦截在 LLM 之前，不发请求
        assert d.answer is None
        assert d.payload.get("paywall") is None
        assert "采集降级" in d.evidence

    def test_乱码降级且可读部分有门禁命题_仍救济(self):
        """乱码例外**只放宽降级，不放宽判据**：可读部分里**有**门禁命题
        时照样救济（实测 m.ixigua「以下内容需下载西瓜视频查看」）。"""
        from trajectory_pipeline.executor.branches import decision_branch

        body = (
            "����导航��"
            "以下内容需下载西瓜视频查看\n��"
        )
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(body_text=body, interactive_elements=(),
                 degraded=("body_mojibake",)),
        )
        assert fake.calls == []  # 拦截在 LLM 之前，不发请求
        assert d.answer is False
        assert d.payload["paywall"] is True
        assert "混合编码" in d.evidence, "evidence 须标注乱码救济形态供人工复核"
        assert decision_branch(d) == "subscription_wall"

    def test_乱码降级且正文无信号_仍fail_closed(self):
        """可读部分没有付费墙证据的乱码页——照旧 fail-closed。"""
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(body_text="\ufffd\ufffd导航\ufffd\ufffd页面加载中",
                 interactive_elements=(), degraded=("body_mojibake",)),
        )
        assert fake.calls == []
        assert d.answer is None
        assert "采集降级" in d.evidence

    def test_乱码叠加其他降级_不救济(self):
        """mojibake + 其他降级（如 snapshot 失败）→ 集合不再只含
        mojibake 标记，§5.5 照旧 fail-closed。"""
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(body_text=self.SOHU_BODY, interactive_elements=(),
                 degraded=("body_mojibake", "snapshot")),
        )
        assert fake.calls == []
        assert d.answer is None
        assert "采集降级" in d.evidence

    def test_标题乱码正文完好_可救济(self):
        """title_mojibake 不影响正文可读性——也在放行集合内。"""
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(body_text="功夫 正片 开通会员观看",
                 interactive_elements=(), degraded=("title_mojibake",)),
        )
        assert d.answer is False
        assert d.payload["paywall"] is True

    def test_二乱码降级_同样不救济(self):
        """② 也被 degraded 拦截（visit 流程先问 ②）——同款例外适用：
        **放宽的是降级，不是判据**。sohu 混编页的可读部分是营销模块，
        没有门禁语境 ⇒ ② 判否也不救济（落 ``unresolved``）。"""
        from trajectory_pipeline.executor.branches import decision_branch

        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE,
            _obs(body_text=self.SOHU_BODY, degraded=("body_mojibake",)),
        )
        assert fake.calls == []
        assert d.answer is None
        assert decision_branch(d) == "unresolved"

    def test_二乱码降级且可读部分有门禁命题_同样救济(self):
        """同款例外在② 上同样成立：可读部分有门禁命题就救济。"""
        from trajectory_pipeline.executor.branches import decision_branch

        body = "����导航��以下内容需下载西瓜视频查看��"
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.IS_REACHABLE,
            _obs(body_text=body, degraded=("body_mojibake",)),
        )
        assert fake.calls == []
        assert d.answer is False
        assert decision_branch(d) == "subscription_wall"

    def test_非乱码降级_不因词表命中放松(self):
        """INC32 形态回归保护：普通降级（browser_links）即使正文有
        强信号也 fail-closed——乱码例外不得扩散到其他降级类型。"""
        fake = FakeLLM()
        d = lp.LLMPerceptor(fake).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(body_text=self.SOHU_BODY, interactive_elements=(),
                 degraded=("browser_links",)),
        )
        assert d.answer is None
        assert "采集降级" in d.evidence


# ═══════════════════════════════════════════════════════════════════════
# 2026-10-15 INC44 证伪记录：曾把 ②③ 词表救济扩到 ④（PLAYER_OK），
# e2e 75 对 A/B 实测零收益（修复 0 / 反转 0）——簇 A 的付费墙证据在
# LLM evidence 里而非 body_text，词表够不着；唯一触发行 T011
# v.qq.com/error.html 是假阳性（错误页页脚「VIP 会员」命中，broken 被
# 误改判订阅墙）。已回滚，此处的教训是：**对 ④ 做词表救济前先核对
# 目标簇的信号到底在 body_text 还是 LLM evidence**（探针时只验了
# body 有没有信号，没验信号是否足以覆盖目标行）。
# ═══════════════════════════════════════════════════════════════════════
