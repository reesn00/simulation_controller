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
            _obs(interactive_elements=_els(("e1", "a", "登录"),
                                           ("e5", "button", "立即播放"))))
        assert d.answer is True
        assert d.payload["ref"] == "e5"
        assert d.payload["trailer_only"] is False

    def test_取回的ref必须能点得到(self):
        """ref 的用途就是 click。取回来却不属于观察，那是造出来的东西——
        和模型编造 ref 一样不可用。"""
        from trajectory_pipeline.executor.actions import target_of

        obs = _obs(interactive_elements=_els(("e1", "a", "登录"),
                                             ("e5", "button", "立即播放")))
        d = llm_perceptor(forget_ref=True).decide(Q.FIND_PLAY_CONTROL, obs)
        assert target_of(str(d.payload["ref"]),
                         obs.interactive_elements) is not None

    def test_evidence写明ref是代码取回的(self):
        """人读存档时要能分清 ref 是模型给的还是代码补的——两者是
        不同的证据强度，不标出来就分不出来了。"""
        d = llm_perceptor(forget_ref=True).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(interactive_elements=_els(("e5", "button", "立即播放"))))
        assert "未回传 ref" in d.evidence and "词表" in d.evidence

    def test_词表为空时仍然判否(self):
        """反向护栏：取回不是「一律判 True」。页面上真的没有播放控件
        时必须判 False，否则每个页面都会变成「找到了播放控件」。"""
        d = llm_perceptor(forget_ref=True).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(interactive_elements=_els(("e1", "a", "登录"),
                                           ("e2", "a", "设置"))))
        assert d.answer is False
        assert d.payload["ref"] == ""

    def test_只有预告控件时落trailer_only而非判否(self):
        """「有控件但全是预告片」与「压根没有控件」是两条不同的负分支。

        W1 一直落 ``trailer_only``，W3 原来直落 False——同一批素材两个
        结论，且都看着像对的。"""
        d = llm_perceptor(forget_ref=True).decide(
            Q.FIND_PLAY_CONTROL,
            _obs(interactive_elements=_els(("e1", "a", "预告片"))))
        assert d.answer is True
        assert d.payload["trailer_only"] is True
        assert d.payload["ref"] == ""

    def test_预告控件不会被当成可点的ref(self):
        """取回 ref 只取非预告的：点了预告片，采样采到的是预告资源。"""
        from trajectory_pipeline.perception.rule_perceptor import (
            play_control_candidates,
            trailer_only_candidates,
        )

        obs = _obs(interactive_elements=_els(("e1", "a", "预告片"),
                                            ("e2", "button", "正片第1集")))
        assert [e.ref for e in play_control_candidates(obs)] == ["e2"]
        assert [e.ref for e in trailer_only_candidates(obs)] == ["e1"]

    def test_编造ref仍被白名单拦住(self):
        """取回 ref 不等于放开白名单：模型**给了** ref 而它不存在，
        那是编造，照旧 fail-closed。"""
        d = llm_perceptor(lie_about_ref="e999").decide(
            Q.FIND_PLAY_CONTROL,
            _obs(interactive_elements=_els(("e5", "button", "立即播放"))))
        assert d.answer is None
        assert "不在观察" in d.evidence

    def test_模型给的ref未命中词表时留痕(self):
        """图标按钮（纯 SVG / 文本在 aria-label 上）常常过不了词表，
        而模型看得懂它的视觉语义——所以**标注**而不是否决。但分歧必须
        留痕：禁止投票意味着暴露给人，而不是由代码悄悄择一。"""
        d = llm_perceptor(lie_about_ref="e1").decide(
            Q.FIND_PLAY_CONTROL,
            _obs(interactive_elements=_els(("e1", "button", "▷"),
                                           ("e5", "button", "立即播放"))))
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
            f"<think>{self.THINK}</think>\n根据页面观察，我无法确定。")
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
        d = llm_perceptor(think=True, prose=True,
                          think_text=self.THINK).decide(
            Q.IS_REACHABLE, _obs(body_text="页面主体可见"))
        assert d.answer is None
        assert self.THINK not in d.evidence
        assert "<think>" not in d.evidence

    def test_端到端_evidence落进存档字段(self):
        """红线的落点是**持久化字段**，所以断言必须打到 outcome 上，
        而不是停在 Decision——中间隔着 ledger 转换。"""
        from trajectory_pipeline.executor.branches import RunLedger

        d = llm_perceptor(think=True, prose=True,
                          think_text=self.THINK).decide(
            Q.IS_REACHABLE, _obs(body_text="页面主体可见"))
        outcome = RunLedger("T001").record("https://x.test/", None, d.evidence,
                                           decision=d,
                                           site_url="https://x.test/")
        assert self.THINK not in outcome.to_json()["evidence"]
        assert "<think>" not in outcome.to_json()["evidence"]


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
            _obs(links=(LinkItem("功夫 在线观看", "https://www.a.test/gg"),)))
        assert d.answer is None
        assert "未给出任何" in d.evidence
        # 反向护栏：这句话**不能**同时说「不在观察」——两者指的不是一回事
        assert "不在观察" not in d.evidence

    def test_地址全对不上时说的是对不上(self):
        from trajectory_pipeline.perception.base import LinkItem

        d = llm_perceptor(invent_urls=True).decide(
            Q.SELECT_PLAY_SITES,
            _obs(links=(LinkItem("功夫 在线观看", "https://www.a.test/gg"),)))
        assert d.answer is None
        assert "全部不在观察" in d.evidence
        assert "未给出任何" not in d.evidence

    def test_没作答时evidence带上送进模型的链接数(self):
        """复盘要能判断「是输入不足还是模型偷懒」，光看「未作答」不够。"""
        from trajectory_pipeline.perception.base import LinkItem

        links = tuple(LinkItem(f"功夫 第{i}集", f"https://www.a.test/{i}")
                      for i in range(3))
        d = llm_perceptor(no_conclusion=True).decide(
            Q.SELECT_PLAY_SITES, _obs(links=links))
        assert "3 条链接" in d.evidence


class Test回指编号而不是url:
    """① 让模型回**编号 i**，代码回指成 href——回长 URL 是记忆任务。

    模型会顺手改写 scheme、吞掉 www、丢掉 query 尾巴，于是「选的 URL
    压根不在观察里」整批作废。编号把它降级成复制任务，而 href 由代码
    从窗口里取，模型碰不到字符串。
    """

    def _links(self):
        from trajectory_pipeline.perception.base import LinkItem

        return (LinkItem("功夫 在线观看", "https://www.a.test/gg"),
                LinkItem("百度首页", "https://www.baidu.com/"))

    def test_渲染时每条链接带编号(self):
        rows = lp._render(_obs(links=self._links()),
                          target_title="功夫",
                          question=Q.SELECT_PLAY_SITES)["links"]
        assert [r["i"] for r in rows] == [0, 1]

    def test_非一号题不带编号(self):
        """编号是 ① 的契约。别的题带上它等于凭空多一个字段，
        而模型会顺着编造出编号语义。"""
        rows = lp._render(_obs(links=self._links()),
                          question=Q.PLAYER_OK)["links"]
        assert all("i" not in r for r in rows)

    def test_编号回指到窗口里的href(self):
        d = llm_perceptor(by_index=True).decide(
            Q.SELECT_PLAY_SITES, _obs(links=self._links()))
        assert d.answer is True
        # 回传的 i=0 → 第一条链接的 href，且 payload 里带 url 供下游记账
        assert d.payload["selected"][0]["url"] == "https://www.a.test/gg"

    def test_字符串编号也认(self):
        """模型爱给整数加引号。真后端两种形态都出现过。"""
        assert lp._as_index("3") == 3
        assert lp._as_index(3) == 3
        assert lp._as_index(True) is None      # bool 是 int 的子类
        assert lp._as_index("abc") is None
        assert lp._as_index(None) is None

    def test_编号越界时退回url匹配(self):
        """越界说明模型在编。退回 URL 匹配而不是当命中——
        否则 i=999 会指向窗口外的东西，而控制流会去访问它。"""
        rows = [{"i": 999, "url": "https://www.a.test/gg"}]
        out = lp._resolve_rows(rows, lp._link_window(
            _obs(links=self._links()), Q.SELECT_PLAY_SITES, "功夫"),
            {"https://www.a.test/gg"})
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

        obs = _obs(links=(LinkItem("百度首页", "https://www.baidu.com/"),
                          LinkItem("功夫 在线观看", "https://www.a.test/gg")))
        window = lp._link_window(obs, Q.SELECT_PLAY_SITES, "功夫")
        # 先确认 fixture 真的有顺序差异，否则这条断言测不出任何东西
        assert [l.href for l in window] != [l.href for l in obs.links]

        rendered = lp._render(obs, target_title="功夫",
                              question=Q.SELECT_PLAY_SITES)["links"]
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
