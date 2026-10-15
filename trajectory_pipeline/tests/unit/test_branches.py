"""``executor.branches`` 单测。

重点测**校验**而不是计数：分支名写错必须当场炸，
「静默落盘一条分支名拼错的负样本」是这类系统最典型的慢性病。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.executor.branches import (
    BRANCH_LABELS,
    NON_SAMPLE_BRANCHES,
    RunLedger,
    UnknownBranch,
    decision_branch,
)
from trajectory_pipeline.perception import questions
from trajectory_pipeline.perception.base import Decision, Observation, Q


def dec(question: str, answer: bool | None, **kw) -> Decision:
    return Decision(
        question=question,
        answer=answer,
        confidence=1.0,
        evidence=kw.pop("evidence", "e"),
        source="rule",
        payload=kw.pop("payload", {}),
        fallback_used=kw.pop("fallback_used", False),
    )


class TestLedgerValidation:
    def test_合法分支可记(self):
        ledger = RunLedger("T001")
        for b in questions.all_branches():
            ledger.record("https://x.test/", b, "理由")
        assert len(ledger.outcomes) == len(questions.all_branches())

    def test_未知分支当场抛错(self):
        ledger = RunLedger("T001")
        with pytest.raises(UnknownBranch):
            ledger.record("https://x.test/", "not_a_branch", "理由")
        assert ledger.outcomes == []  # 抛错时不得留下半条记录

    def test_成功可记空分支(self):
        ledger = RunLedger("T001")
        out = ledger.record("https://x.test/", None, "有播放组件")
        assert out.branch is None
        assert ledger.succeeded == 1

    def test_每个分支都有中文标签(self):
        """Label Studio 与报表直接用这张表，不在下游做二次翻译。"""
        for b in questions.all_branches():
            assert BRANCH_LABELS.get(b), f"{b} 缺标签"


class TestSampleClassification:
    def test_unresolved_不算负样本(self):
        """I4 的直接后果：没判出来 ≠ 失败。混进负样本池会污染纯度指标，
        且这种污染在报表上看不出来（都表现为「有一条负样本」）。"""
        ledger = RunLedger("T001")
        ledger.record("https://x.test/", "unresolved", "规则版不具备语义能力")
        assert ledger.negative_samples() == []
        assert ledger.summary()["negative_samples"] == 0

    def test_trailer_suspect_也不算负样本(self):
        ledger = RunLedger("T001")
        ledger.record("https://x.test/", "trailer_suspect", "词表判不准")
        assert ledger.negative_samples() == []

    def test_真负样本进池(self):
        ledger = RunLedger("T001")
        ledger.record("https://x.test/", "no_play_control", "无控件")
        ledger.record("https://y.test/", "login_wall_or_blocked", "登录墙")
        assert len(ledger.negative_samples()) == 2


class TestSummary:
    def test_缺失分支可见(self):
        """十三条里有哪条没产出——这比「各分支数量」更能说明问题。
        数量为 0 可能只是样本少，missing 会直接指向「这条路径没接上」。

        2026-10-12 阶段 1：``login_wall_or_blocked`` 已弃用、不进
        ``all_branches()``，missing 断言改盯活跃新分支 ``login_required``
        ——合并态由 ``record()`` 按 DEPRECATED_BRANCHES 放行，但不计缺。"""
        ledger = RunLedger("T001")
        ledger.record("https://x.test/", "no_play_control", "e")
        s = ledger.summary()
        assert "no_play_control" in s["by_branch"]
        assert "login_required" in s["missing_branches"]
        assert len(s["missing_branches"]) == len(questions.all_branches()) - 1

    def test_全分支覆盖时_missing_为空(self):
        ledger = RunLedger("T001")
        for b in questions.all_branches():
            ledger.record("https://x.test/", b, "e")
        assert ledger.summary()["missing_branches"] == []


class TestDecisionMapping:
    def test_none_映射_unresolved(self):
        assert decision_branch(dec(Q.IS_REACHABLE, None)) == "unresolved"

    def test_false_映射该题的负分支(self):
        assert decision_branch(dec(Q.IS_REACHABLE, False)) == "login_wall_or_blocked"
        assert decision_branch(dec(Q.FIND_PLAY_CONTROL, False)) == "no_play_control"
        assert decision_branch(dec(Q.PLAYER_OK, False)) == "component_unverified"

    def test_true_不是失败分支(self):
        assert decision_branch(dec(Q.PLAYER_OK, True)) == ""

    def test_映射有唯一出处(self):
        """判 false 归哪支只在 questions 里定义一次——控制流不许硬编码字符串。"""
        assert decision_branch(dec(Q.SELECT_PLAY_SITES, False)) == "not_play_site"

    def test_record_from_走同一映射(self):
        ledger = RunLedger("T001")
        out = ledger.record_from("https://x.test/", dec(Q.IS_REACHABLE, None))
        assert out.branch == "unresolved"
        out2 = ledger.record_from("https://y.test/", dec(Q.IS_REACHABLE, False))
        assert out2.branch == "login_wall_or_blocked"


class TestSerialization:
    def test_成功项_标签_不显示为失败(self):
        ledger = RunLedger("T001")
        out = ledger.record("https://x.test/", None, "有播放组件", reached=True)
        j = out.to_json()
        assert j["branch"] is None
        assert j["branch_label"] == "成功"
        assert j["reached_play_page"] is True

    def test_决策来源透传(self):
        ledger = RunLedger("T001")
        out = ledger.record_from(
            "https://x.test/", dec(Q.PLAYER_OK, True, fallback_used=True)
        )
        assert out.source == "rule"
        assert out.fallback_used is True

    def test_成功不记负分支(self):
        """``answer=True`` 曾被记成 ``component_unverified``——
        而 ``test_决策来源透传`` 只断言 source/fallback_used，
        **没有一条断言看 branch**，所以这条错分支一直没被发现。

        症状很重：一条真成功会带着「点了但没组件」的标签进负样本池，
        而报告上它是「成功」——两份记录互相矛盾。
        """
        ledger = RunLedger("T001")
        out = ledger.record_from("https://x.test/", dec(Q.PLAYER_OK, True))
        assert out.branch is None
        assert out.to_json()["branch_label"] == "成功"


class TestObservationWarning:
    def test_降级与截断都提示(self):
        from trajectory_pipeline.executor.branches import (
            validate_observation_for_decision,
        )

        obs = Observation(
            url="u",
            page_title="t",
            body_text="b",
            degraded=("browser_links",),
            truncated=True,
        )
        msg = validate_observation_for_decision(obs, Q.SELECT_PLAY_SITES)
        assert "降级" in msg and "截断" in msg

    def test_正常观察无提示(self):
        from trajectory_pipeline.executor.branches import (
            validate_observation_for_decision,
        )

        obs = Observation(url="u", page_title="t", body_text="b")
        assert validate_observation_for_decision(obs, Q.SELECT_PLAY_SITES) == ""


def test_non_sample_branches_确实不在注册表的负分支里():
    """NON_SAMPLE_BRANCHES 必须是「无题的兜底分支」，不能是某道题的负分支——
    否则它既会被 question 判出、又不算负样本，语义打架。"""
    q_branches = {q.negative_branch for q in questions.REGISTRY.values()}
    assert not (NON_SAMPLE_BRANCHES & q_branches)


class TestPlayerUnverified:
    """环节⑦ 的 ``None``：控件点了、快照拿到了、播放页就在眼前，
    只是判不了「组件能不能用」。

    与 ``unresolved`` 的区别不是程度、是**责任方**：那个是「连播放页都没到」，
    这个是「到了但证据不足以判」。混在一支里，复核队列就分不出
    「请补一条判据」与「请看一眼这个页面」这两种待办。
    """

    def _dec(self, answer=None):
        return dec(Q.PLAYER_OK, answer, evidence="判不了能不能播")

    def test_落_player_unverified_而不是_unresolved(self):
        led = RunLedger("T001")
        out = led.record_unresolved_reached("https://x.test/p", self._dec())
        assert out.branch == "player_unverified"

    def test_是访问过的(self):
        """``reached`` 必须为 True——这条恰恰是「到了却没结论」，
        报成「没到」会把最有价值的复核样本（页面就在存档里）说没了。"""
        led = RunLedger("T001")
        assert (
            led.record_unresolved_reached("https://x.test/p", self._dec()).reached
            is True
        )

    def test_不进负样本池(self):
        """同 I4：判不了 ≠ 失败。这条进池就是在说「这里看不了」，
        而我们明明正站在播放页上。"""
        led = RunLedger("T001")
        led.record_unresolved_reached("https://x.test/p", self._dec())
        assert led.negative_samples() == []
        assert led.summary()["negative_samples"] == 0

    def test_三支没判出来的成因分开统计(self):
        """报表按分支定位该修什么。合成一支后，
        「感知层该加能力」与「播放页证据不足」会长得一模一样。"""
        assert {
            "unresolved",
            "player_unverified",
            "trailer_suspect",
        } <= NON_SAMPLE_BRANCHES
        led = RunLedger("T001")
        led.record("https://a.test/", "unresolved", "e")
        led.record_unresolved_reached("https://b.test/p", self._dec())
        assert led.summary()["by_branch"] == {"unresolved": 1, "player_unverified": 1}

    def test_拒收非None(self):
        """传了 True/False 进来会记错分支，而调用方拼错应该炸。"""
        led = RunLedger("T001")
        for answer in (True, False):
            with pytest.raises(ValueError, match="只接受 answer=None"):
                led.record_unresolved_reached("https://x.test/p", self._dec(answer))

    def test_拒收时不留半条记录(self):
        """抛错却已追加了一条 outcome 的话，批次报表会多出一条来路不明的记录，
        而谁也不知道它是怎么来的。"""
        led = RunLedger("T001")
        with pytest.raises(ValueError):
            led.record_unresolved_reached("https://x.test/p", self._dec(True))
        assert led.outcomes == []

    def test_record_from_不会产出这一支(self):
        """反向护栏：环节①–④ 的入口。环节⑦ 若误接它，None 会退回
        unresolved——分类退化但不炸，属于最该被测试盯住的那类改动。"""
        led = RunLedger("T001")
        assert led.record_from("https://x.test/p", self._dec()).branch == "unresolved"


class TestOutcomeUrls:
    """D-6 要「站点 URL / 播放页 URL」两条必填，前提是账本分得清三层地址。"""

    def test_三个字段各自落盘(self):
        led = RunLedger("T001")
        out = led.record_unresolved_reached(
            "https://x.test/play/e5",
            dec(Q.PLAYER_OK, None),
            site_url="http://x.test/movie",
            play_page_url="https://x.test/play/e5",
        )
        j = out.to_json()
        assert j["site_url"] == "http://x.test/movie"
        assert j["play_page_url"] == "https://x.test/play/e5"
        assert j["url"] == "https://x.test/play/e5"

    def test_跳转过的站两个地址不同且都留着(self):
        """实测存档里 4 个 run 的候选 url 是 http、outcome url 是 https。
        只留一个字段的话，丢的那个就再也无法从存档恢复了。"""
        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/play/e5",
            dec(Q.PLAYER_OK, False),
            site_url="http://x.test/movie",
            play_page_url="https://x.test/play/e5",
        )
        assert out.site_url != out.url

    def test_没到播放页时播放页地址为空(self):
        """空是**正确**的值：按 D-6 口径「不适用、不扣分」。
        编一个出来才是把猜测写成事实。"""
        led = RunLedger("T001")
        assert (
            led.record("https://x.test/m", "no_play_control", "e").play_page_url == ""
        )

    # ── page_quotes 透传（2026-10-12 增量二十）────────────────

    def test_page_quotes从决策payload提升(self):
        """LLM 路径：``_is_reachable`` / ``_player_ok`` 把 ``page_quotes``
        放在 ``Decision.payload``，``record_from`` 必须把它**提升**到
        ``SiteOutcome.page_quotes`` 独立字段，``to_json`` 写出 list。
        """
        quotes = ["请先登录", "登录后观看完整版"]
        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/",
            dec(Q.IS_REACHABLE, False, payload={"page_quotes": quotes}),
        )
        # 字段在 dataclass 上是 tuple
        assert out.page_quotes == tuple(quotes)
        # 落盘是 list（与 LLM 约定一致）
        assert out.to_json()["page_quotes"] == quotes

    def test_page_quotes空payload时为空list(self):
        """LLM 没给 page_quotes → 落 ``[]``，不是省略字段。
        显式空数组让读档侧拿不到"未填"和"填了空数组"的区别时也能跑。"""
        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/",
            dec(Q.IS_REACHABLE, False, payload={}),
        )
        assert out.page_quotes == ()
        assert out.to_json()["page_quotes"] == []

    def test_record不传decision时page_quotes空(self):
        """W1 规则版无 LLM 原文短语——``record()`` 不传 decision 时
        ``page_quotes`` 字段必须为空（而非抛错或拉 default 路径漏字段）。"""
        led = RunLedger("T001")
        out = led.record("https://x.test/", None, "有播放组件")
        assert out.page_quotes == ()
        assert out.to_json()["page_quotes"] == []

    def test_显式传page_quotes覆盖decision(self):
        """显式 ``page_quotes=...`` 时**覆盖** ``decision.payload``
        里的同名字段——给控制流留一个 escape hatch：偶尔需要人工
        补的"页面原文"覆盖 LLM 给的（典型场景：LLM 给空、人工从
        ``body_text`` 抽了关键短语）。"""
        led = RunLedger("T001")
        out = led.record(
            "https://x.test/", "blocked", "e",
            decision=dec(Q.IS_REACHABLE, False,
                         payload={"page_quotes": ["LLM 抽的"]}),
            page_quotes=("人工补的", "短语"),
        )
        assert out.page_quotes == ("人工补的", "短语")
        assert out.to_json()["page_quotes"] == ["人工补的", "短语"]

    def test_page_quotes归一_脏值不抛(self):
        """``_coerce_quotes`` 的"宁少勿错"过滤：非字符串元素、空串、
        None 都不抛——LLM 给的脏数据进 P1 不能炸。"""
        from trajectory_pipeline.executor.branches import _coerce_quotes
        # 脏数据：None、空串、整数、混入真字符串
        result = _coerce_quotes([None, "", 42, "真短语", "  "])
        # 注意：str(42)="42"会被保留（LLM 误把数字塞进引用），
        # "  "也是非空字符串——本函数只丢明确的空/Nil，不做语义清洗
        # （语义清洗是后续 evidence fallback 候选路径的事，不在 P4a 边界）
        assert "真短语" in result
        assert "" not in result
        assert None not in result
        # 非可迭代输入
        assert _coerce_quotes(None) == ()
        assert _coerce_quotes(123) == ()  # 整数不可迭代

    def test_page_quotes经to_json是list不是tuple(self):
        """JSON 序列化必须把 tuple 转 list——下游 (json.dumps) 不吃 tuple。
        这条在改 to_json 时容易漏：``self.page_quotes`` 是元组，``json.dumps`` 会
        把 tuple 当成 list 序列化（合法），但反序列化侧读档时类型又是 list，
        落盘一致性还是 list 更直接。"""
        import json
        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/",
            dec(Q.IS_REACHABLE, False, payload={"page_quotes": ["a", "b"]}),
        )
        j = out.to_json()
        # 双重落盘：走 json.dumps 再读回
        reloaded = json.loads(json.dumps(j, ensure_ascii=False))
        assert reloaded["page_quotes"] == ["a", "b"]
        assert isinstance(reloaded["page_quotes"], list)
        # 同时核一下字段就地在 SiteOutcome 上是 tuple（frozen 不变式）
        assert isinstance(out.page_quotes, tuple)

class TestBranchPrecondition:
    """负分支的**前置事实**——贴标签之前页面状态必须满足分支定义的前提。

    ``component_unverified`` 的前提（2026-10-10 rubric 口径裁定的侧 A）：
    「**到了播放页**，且页面上没有可验证的播放组件」。守卫核的判据是
    「点击有没有把页面带进播放页」（点击前后是否同页），**不是**
    ``video_tag_count``——真实视频站全是 iframe/JS 播放器，真播放页上
    video=0 是常态，拿它当信号会把合法排除一起杀掉。

    实测（2026-10-09 www.mgtvtv.com/tv/32080/）：点击前后两次观察完全
    相同（390 字正文 / 37 元素 / video=0），说明点击没把页面带进播放页；
    模型判 False 的原话是「仍显示影片详情和『立即播放』按钮，并非实际
    播放页」。映射只看「模型说了 False」，于是贴上 ``component_unverified``
    ——而它有片、有播放按钮，只是我们没点进去。
    """

    def _dec(self, answer):
        from trajectory_pipeline.perception.base import Decision

        return Decision(
            question=Q.PLAYER_OK,
            answer=answer,
            confidence=0.8,
            evidence="LLM 判 False：并非实际播放页",
            source="llm",
        )

    def _obs(self, **kw):
        from trajectory_pipeline.perception.base import Observation

        base = dict(
            url="https://x.test/p", page_title="功夫 - 某站", body_text="立即播放"
        )
        base.update(kw)
        return Observation(**base)

    def test_点击前后同页时降级为unresolved(self):
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/p",
            self._dec(False),
            obs=self._obs(url="https://x.test/p", video_tag_count=0),
            pre_obs=self._obs(url="https://x.test/p"),
        )
        assert out.branch == "unresolved", "点击没换页就不该贴 component_unverified"
        assert "同一个页面" in out.evidence

    def test_原始证据保留(self):
        """降级不是改写结论——模型的原话必须留在证据里，
        否则人工复核看不到「它当时说了什么」，而降级理由会变成唯一线索。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/p",
            self._dec(False),
            obs=self._obs(url="https://x.test/p", video_tag_count=0),
            pre_obs=self._obs(url="https://x.test/p"),
        )
        assert "并非实际播放页" in out.evidence

    def test_换页后video为0也照贴(self):
        """裁定后的核心场景：到了播放页（点击换了页）但没有组件——
        那正是决策树第四行的合法排除。旧守卫（video_tag_count<1 降级）
        会把它错杀成 unresolved。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/play",
            self._dec(False),
            obs=self._obs(url="https://x.test/play", video_tag_count=0),
            pre_obs=self._obs(url="https://x.test/site"),
        )
        assert out.branch == "component_unverified"

    def test_没传观察时照旧(self):
        """老调用方不传 obs 时不能被打成 unresolved——
        那是「守卫反噬」，比不设守卫更糟。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from("https://x.test/p", self._dec(False))
        assert out.branch == "component_unverified"

    def test_没传点击前观察时放行(self):
        """obs 有、pre_obs 没有（调用点未接线）同样放行——
        守卫缺一边就缺判据，缺判据时不做第二次猜测。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/p", self._dec(False), obs=self._obs(video_tag_count=0)
        )
        assert out.branch == "component_unverified"

    def test_login_required_同页时降级(self):
        """② 拆分的 login_required 复用**同页守卫**（设计文档 §8 风险 5）：
        站点页导航栏的「请登录」不是登录墙，点击没换页就没有新证据。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/site",
            Decision(Q.IS_REACHABLE, False, 0.8, "需登录", "llm",
                     {"access_control_type": "login"}),
            obs=self._obs(url="https://x.test/site", body_text="请登录"),
            pre_obs=self._obs(url="https://x.test/site", body_text="请登录"),
        )
        assert out.branch == "unresolved", "同页却记了 login_required"
        assert "同一个页面" in out.evidence

    def test_login_required_到了目标页才成立(self):
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/login",
            Decision(Q.IS_REACHABLE, False, 0.8, "需登录", "llm",
                     {"access_control_type": "login"}),
            obs=self._obs(url="https://x.test/login", body_text="登录后可免费观看"),
            pre_obs=self._obs(url="https://x.test/site", body_text="影片详情"),
        )
        assert out.branch == "login_required"

    def test_blocked_同页时降级(self):
        """blocked 同样复用同页守卫：站点入口页的弹窗/文案不是环境阻断。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/site",
            Decision(Q.IS_REACHABLE, False, 0.8, "地区限制", "llm",
                     {"access_control_type": "region"}),
            obs=self._obs(url="https://x.test/site", body_text="仅限中国大陆"),
            pre_obs=self._obs(url="https://x.test/site", body_text="仅限中国大陆"),
        )
        assert out.branch == "unresolved", "同页却记了 blocked"

    def test_blocked_到了目标页才成立(self):
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/play",
            Decision(Q.IS_REACHABLE, False, 0.8, "验证码", "llm",
                     {"access_control_type": "anti_bot"}),
            obs=self._obs(url="https://x.test/play", body_text="验证码"),
            pre_obs=self._obs(url="https://x.test/site", body_text="影片详情"),
        )
        assert out.branch == "blocked"

    def test_partial_free_同页时降级(self):
        """④ 拆分的 partial_free 复用同页守卫：站点页的「会员抢先看」
        是宣传文案，不是试看付费墙。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/site",
            Decision(Q.PLAYER_OK, False, 0.8, "会员抢先看", "llm",
                     {"paywall_type": "preview"}),
            obs=self._obs(url="https://x.test/site", body_text="会员抢先看"),
            pre_obs=self._obs(url="https://x.test/site", body_text="会员抢先看"),
        )
        assert out.branch == "unresolved", "同页却记了 partial_free"

    def test_partial_free_到了播放页才成立(self):
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/play",
            Decision(Q.PLAYER_OK, False, 0.8, "试看6分钟", "llm",
                     {"paywall_type": "trial"}),
            obs=self._obs(url="https://x.test/play", body_text="试看6分钟，完整需会员"),
            pre_obs=self._obs(url="https://x.test/site", body_text="影片详情"),
        )
        assert out.branch == "partial_free"

    def test_broken_不受同页守卫影响(self):
        """broken 是**条件式**：404 形态 URL 即证据、无需播放页前置，
        因此不进 ``_precondition_failed`` 的守卫集合——同页也不降级。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/404",
            Decision(Q.IS_REACHABLE, False, 0.8, "404", "llm",
                     {"access_control_type": "not_found"}),
            obs=self._obs(url="https://x.test/404", body_text="404"),
            pre_obs=self._obs(url="https://x.test/404", body_text="404"),
        )
        assert out.branch == "broken", "broken 不适用同页守卫"

    def test_answer为None不受影响(self):
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/p", self._dec(None), obs=self._obs(video_tag_count=0)
        )
        assert out.branch == "unresolved"

    def test_前置事实表_六条(self):
        """这张表要短。长得跟分支表一样长就没人维护了——
        一旦要求给每条分支都填前置事实，没人填的会变成空串，
        而空串与「不需要前置事实」长得一模一样。

        2026-10-12 阶段 1：② 拆分（login_required/blocked）与 ④ 拆分
        （partial_free）按设计文档 §8 风险 5 复用**同页守卫**，表从 2 涨到
        6；``broken`` 是**条件式**——404 形态无需播放页前置（URL 即证据），
        媒体损坏形态走 media_signal 暴露、不经 decision_branch 自动产出，
        所以它登记口径但**不在** ``_precondition_failed`` 的守卫集合里。
        上限随表同步到 **6 条**——到了就该停下来重新想这张表，而不是改数字。
        """
        from trajectory_pipeline.executor.branches import BRANCH_PRECONDITION

        assert set(BRANCH_PRECONDITION) == {
            "component_unverified",
            "subscription_wall",
            "login_required",
            "blocked",
            "partial_free",
            "broken",
        }
        assert len(BRANCH_PRECONDITION) <= 6, (
            "前置事实表超过 6 条了——这张表的价值在于短，每加一条都要有人维护它"
        )

    def test_付费墙前置事实不成立时降级(self):
        """「有播放页但需付费」要求**确实到了播放页**。站点页上的
        「开通会员」是导航栏文案，不是付费墙——所以同页守卫适用。"""
        from trajectory_pipeline.executor.branches import RunLedger

        pre = self._obs(video_tag_count=0, body_text="开通会员观看")
        same = self._obs(video_tag_count=0, body_text="开通会员观看")
        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/movie",
            Decision("player.ok", False, 0.8, "需付费", "llm", {"paywall": True}),
            obs=same,
            pre_obs=pre,
        )
        assert out.branch == "unresolved", "点击前后同页却记了付费墙"

    def test_付费墙到了播放页才成立(self):
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/movie",
            Decision("player.ok", False, 0.8, "需付费", "llm", {"paywall": True}),
            obs=self._obs(video_tag_count=0, body_text="开通会员观看"),
            pre_obs=self._obs(video_tag_count=0, body_text="站点首页"),
        )
        assert out.branch == "subscription_wall"

    # 2026-10-13 增量三十二 · D10 偏差修缮：subscription_wall 强信号旁路
    def test_subscription_wall_强信号旁路_多关键词(self):
        """真付费墙页面点击前后就是同一个页面（页面即终点站）。
        LLM 决策证据里 ≥2 个付费墙关键词即视为「看穿页面」——同页守卫放行。
        实测样本：v.qq.com/x/cover/zr5a67l333ehzu9/d0032x85stp.html
        """
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/movie",
            Decision(
                "player.ok", False, 0.8,
                "页面明确显示「新客VIP专享特惠」和「开通会员」，且播放器报错70017001.1",
                "llm", {"paywall": True},
            ),
            obs=self._obs(url="https://x.test/movie", body_text="VIP专享 开通会员"),
            pre_obs=self._obs(url="https://x.test/movie", body_text="VIP专享 开通会员"),
        )
        assert out.branch == "subscription_wall", (
            f"强信号却降级为 {out.branch}——真付费墙页应该旁路同页守卫"
        )

    def test_subscription_wall_强信号旁路_单关键词_带价格(self):
        """单关键词 + 价格模式也是强信号。
        实测样本：v.youku.com/v_show/id_XMTQwNzMzMTc1Mg==
        （VIP + 首3月9.9元/月 + 立即开通）。
        """
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/movie",
            Decision(
                "player.ok", False, 0.8,
                "页面正片明确标注「VIP」，并显示「首3月9.9元/月，立即开通」，需订阅会员才能观看",
                "llm", {"paywall": True},
            ),
            obs=self._obs(url="https://x.test/movie", body_text="VIP 9.9元/月"),
            pre_obs=self._obs(url="https://x.test/movie", body_text="VIP 9.9元/月"),
        )
        assert out.branch == "subscription_wall"

    def test_subscription_wall_强信号旁路_续费会员(self):
        """续费 + 会员也是强信号。
        实测样本：v-wb.youku.com/v_show/id_XNDE0NjYzODAzNg==
        """
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/movie",
            Decision(
                "player.ok", False, 0.8,
                "页面显示「续费会员」，表明该影片需续费订阅后才能观看",
                "llm", {"paywall": True},
            ),
            obs=self._obs(url="https://x.test/movie", body_text="续费会员"),
            pre_obs=self._obs(url="https://x.test/movie", body_text="续费会员"),
        )
        assert out.branch == "subscription_wall"

    def test_subscription_wall_弱信号不旁路(self):
        """只命中 1 个关键词（付费）且无价格模式 = 弱信号 = 仍降级。
        与 ``test_付费墙前置事实不成立时降级`` 同款设计。
        """
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/movie",
            Decision(
                "player.ok", False, 0.8,
                "需付费",
                "llm", {"paywall": True},
            ),
            obs=self._obs(url="https://x.test/movie", body_text="开通会员观看"),
            pre_obs=self._obs(url="https://x.test/movie", body_text="开通会员观看"),
        )
        assert out.branch == "unresolved", (
            "弱信号却放行了——会污染 nav-bar ad CTA 类的真负样本池"
        )

    def test_partial_free_强信号不旁路(self):
        """旁路**只对** ``subscription_wall`` 生效。partial_free 即使
        命中 1 关键词也仍降级——不扩张旁路范围。
        """
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger(task_id="T001")
        out = led.record_from(
            "https://x.test/site",
            Decision(Q.PLAYER_OK, False, 0.8, "会员抢先看", "llm",
                     {"paywall_type": "preview"}),
            obs=self._obs(url="https://x.test/site", body_text="会员抢先看"),
            pre_obs=self._obs(url="https://x.test/site", body_text="会员抢先看"),
        )
        assert out.branch == "unresolved", "partial_free 走旁路了——超出设计范围"

    # 2026-10-15 INC44 · 簇 A：④ 记账降级误伤订阅墙——确定性信号旁路
    def test_subscription_wall_救济标记旁路同页守卫(self):
        """②③④ 负分支付费墙救济留下的 ``paywall_word`` + body 过强弱
        信号组合（与 LLM evidence 旁路同款强度）→ 同页守卫放行。

        实测簇 A（INC44 前研，75 对直访 9 行）：④ False + body 含
        付费墙文案，本该 subscription_wall 却被同页降级 unresolved。
        本例 body「续费会员」：续费（强）+ 会员（弱）→ 过强度闸。
        """
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/movie",
            Decision(
                "player.ok", False, 0.8,
                "LLM 判 False：页面未检测到媒体元素，当前仍是影片详情页\n"
                "[付费墙救济] 正文含付费墙短语「续费」",
                "llm",
                {"paywall": True, "paywall_word": "付费墙短语「续费」"},
            ),
            obs=self._obs(url="https://x.test/movie", body_text="续费会员"),
            pre_obs=self._obs(url="https://x.test/movie", body_text="续费会员"),
        )
        assert out.branch == "subscription_wall", (
            f"强确定性信号却被降级为 {out.branch}——真付费墙页应旁路同页守卫"
        )

    def test_subscription_wall_弱短语标记且弱body_仍降级(self):
        """INC32 纪律回归保护：``_player_ok`` 短语命中路径对弱短语
        （「开通会员」）也设 ``paywall_word``——那是 nav-bar CTA 形态
        （mgtv 实测：详情页 + 开通会员广告）。标记 + 弱 body（无强弱
        组合、无价格）→ 同页守卫照旧降级，不因标记存在而放行。
        """
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/movie",
            Decision(
                "player.ok", False, 0.8,
                "LLM 判 False：仍显示影片详情和『立即播放』按钮",
                "llm",
                {"paywall": True, "paywall_word": "开通会员"},
            ),
            obs=self._obs(url="https://x.test/movie", body_text="开通会员 享更多精彩"),
            pre_obs=self._obs(url="https://x.test/movie", body_text="开通会员 享更多精彩"),
        )
        assert out.branch == "unresolved", (
            "弱短语标记 + 弱 body 却放行了——nav-bar CTA 会污染真负样本池"
        )

    def test_subscription_wall_无标记且弱信号_仍降级(self):
        """LLM 自报 ``paywall=True`` 但 evidence 无强弱组合、也无救济
        标记——同页守卫照旧降级（INC32 原始形态，保持不变）。"""
        from trajectory_pipeline.executor.branches import RunLedger

        led = RunLedger("T001")
        out = led.record_from(
            "https://x.test/movie",
            Decision("player.ok", False, 0.8, "需付费", "llm", {"paywall": True}),
            obs=self._obs(url="https://x.test/movie", body_text="开通会员观看"),
            pre_obs=self._obs(url="https://x.test/movie", body_text="开通会员观看"),
        )
        assert out.branch == "unresolved"

    def test_has_strong_paywall_signal_单元(self):
        """直接测判据函数，方便回归。"""
        from trajectory_pipeline.executor.branches import _has_strong_paywall_signal

        cases = [
            ("需付费", False, "弱信号 1 关键词无价格"),
            # 仅弱关键词（开通/会员）= nav-bar CTA 模式，不旁路
            ("开通会员观看", False, "仅弱关键词 不旁路"),
            ("开通会员 享更多精彩", False, "仅弱关键词 不旁路"),
            ("会员抢先看", False, "仅弱关键词 不旁路（partial_free 同款）"),
            # 强 + 弱组合 = 真付费墙
            ("VIP 会员", True, "1 强+1 弱"),
            ("续费会员", True, "1 强+1 弱"),
            ("订阅 开通", True, "1 强+1 弱"),
            # ≥2 强关键词
            ("VIP 订阅 续费 开通会员", True, "多强关键词"),
            ("续费 包年", True, "2 强"),
            # 强 + 价格
            ("VIP 首3月9.9元/月", True, "1 强+价格"),
            ("¥99 包月", True, "1 强+价格"),
            ("订阅 9.9元", True, "1 强+价格"),
            # 弱+价格（无强关键词）= 仍弱——保守起见不放行
            ("开通会员 ¥9.9", False, "仅弱+价格 不旁路"),
            ("", False, "空 evidence"),
        ]
        for ev, want, note in cases:
            d = Decision("player.ok", False, 0.8, ev, "llm", {"paywall": True})
            got = _has_strong_paywall_signal(d)
            assert got is want, f"{note}: evidence={ev!r} want={want} got={got}"

        # decision=None 必须返回 False（不让 None 误触发旁路）
        assert _has_strong_paywall_signal(None) is False

    def test_同页判定与评分器同源(self):
        """executor 与 evaluation 各有一份 ``_same_page``（executor 不
        import evaluation，见 branches.py）。两处漂移会让「守卫拦的」
        与「评分器扣的」变成两套标准——D-5 映射表与注册表不同源的
        教训（避坑指南 §1.1）。"""
        from trajectory_pipeline.evaluation.scorers import d4_scorer
        from trajectory_pipeline.executor.branches import _same_page as exec_same

        def as_dict(o):
            if o is None:
                return None
            return {
                "url": o.url,
                "interactive_elements": o.interactive_elements,
                "body_text": o.body_text,
            }

        a = self._obs(url="https://x.test/p", body_text="同页")
        b = self._obs(url="https://x.test/p", body_text="同页")
        c = self._obs(url="https://x.test/p2", body_text="同页")
        d = self._obs(url="https://x.test/p", body_text="不同")
        for x, y in ((a, b), (a, c), (a, d), (a, None), (None, b)):
            assert exec_same(x, y) == d4_scorer._same_page(as_dict(x), as_dict(y)), (
                f"两份 _same_page 漂移：exec={exec_same(x, y)} "
                f"scorer={d4_scorer._same_page(as_dict(x), as_dict(y))}"
            )
