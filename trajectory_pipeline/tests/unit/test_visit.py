"""``executor`` 控制流的离线单测——用 fake driver 跑完整条链。

**不碰真浏览器**。理由：`visit_site` 里值得测的不是 obscura 的返回格式
（那是 ``test_dom.py`` 的事），而是**控制流的正确性**——分支该走哪条、
记账对不对、点击后有没有重新观察。用 fake 是把这些测得又快又全的办法。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.browser.page_driver import DriverError
from trajectory_pipeline.executor.orchestrator import Orchestrator, RunConfig
from trajectory_pipeline.executor.steps.search import Candidate
from trajectory_pipeline.executor.steps.visit import visit_site
from trajectory_pipeline.perception.base import (
    Decision,
    InteractiveElement,
    LinkItem,
    Observation,
    Perceptor,
    Q,
)
from trajectory_pipeline.perception.rule_perceptor import RulePerceptor


# ═══════════════════════════════════════════════════════════════════════
# Fake
# ═══════════════════════════════════════════════════════════════════════


class FakeDriver:
    """按 URL 返回预设观察的 driver。

    ``pages`` 是 ``url → Observation`` 映射；未登记的 URL 抛
    :class:`DriverError`，用来模拟导航失败。
    """

    name = "fake"

    def __init__(self, pages: dict[str, Observation] | None = None) -> None:
        self.pages = pages or {}
        self.current: Observation | None = None
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.observe_calls = 0
        self.tabs: list[str] = []

    async def goto(self, url: str, *, wait_until: str = "load") -> None:
        self.visited.append(url)
        if url not in self.pages:
            raise DriverError(f"fake: {url} 不存在")
        self.current = self.pages[url]

    async def snapshot(self, *, max_chars=None):
        from trajectory_pipeline.executor.dom import CleanedSnapshot
        c = self.current
        return CleanedSnapshot(
            url=c.url, title=c.page_title, body=c.body_text,
            raw_len=len(c.body_text), stripped_ratio=1.0, truncated=c.truncated,
            max_chars=max_chars, body_source=c.body_source,
        )

    async def links(self, *, limit=50):
        return ()

    async def interactive(self, *, limit=50):
        return self.current.interactive_elements if self.current else ()

    async def count(self, selector: str) -> int:
        return 0

    async def evaluate(self, expression: str):
        return None

    async def click(self, *, ref=None, selector=None) -> None:
        # 点击后**换页**——这正是「必须重新 observe」要捕获的场景
        self.clicked.append(ref or selector or "")
        target = f"https://x.test/play/{ref}"
        if target in self.pages:
            self.current = self.pages[target]

    async def type_text(self, *, text, ref=None, selector=None) -> None: ...

    async def press_key(self, key: str) -> None: ...

    async def new_tab(self, url: str | None = None) -> str:
        self.tabs.append(url or "")
        return f"tab{len(self.tabs)}"

    async def close_tab(self, tab_id: str | None = None) -> None: ...

    async def observe(self, *, max_chars=None) -> Observation:
        self.observe_calls += 1
        return self.current

    async def close(self) -> None: ...


class ScriptedPerceptor:
    """按题返回预设答案。记录调用顺序。"""

    def __init__(self, script: dict[str, Decision]) -> None:
        self.script = script
        self.calls: list[str] = []

    name = "scripted"

    def decide(self, question: str, obs: Observation) -> Decision:
        self.calls.append(question)
        d = self.script.get(question)
        if d is None:
            return Decision(question, None, 1.0, "未配置", "rule", {})
        return Decision(question, d.answer, 1.0, d.evidence, "rule", d.payload)


def ok(question, payload=None, evidence="e"):
    return Decision(question, True, 1.0, evidence, "rule", payload or {})


def no(question, evidence="e"):
    return Decision(question, False, 1.0, evidence, "rule", {})


def none(question, evidence="e"):
    return Decision(question, None, 1.0, evidence, "rule", {})


def obs_at(url, *, title="T", elements=(), body="正文", video=0, iframe=0, **kw):
    return Observation(
        url=url, page_title=title, body_text=body,
        interactive_elements=tuple(
            InteractiveElement(ref=r, tag=t, label=l) for r, t, l in elements
        ),
        video_tag_count=video, iframe_count=iframe, **kw,
    )


CAND = Candidate(url="https://x.test/movie", text="电影", rank=1)


# ═══════════════════════════════════════════════════════════════════════
# visit_site 控制流
# ═══════════════════════════════════════════════════════════════════════


class TestVisitFlow:
    async def test_导航失败记_unreachable_hard(self):
        d = FakeDriver()                          # 没有任何页面 → 全部导航失败
        ledger = RunLedger("T001")
        await visit_site(d, ScriptedPerceptor({}), ledger, CAND)
        assert ledger.outcomes[0].branch == "unreachable_hard"

    async def test_页面不可达记_login_wall(self):
        d = FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")})
        p = ScriptedPerceptor({Q.IS_REACHABLE: no(Q.IS_REACHABLE)})
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "login_wall_or_blocked"

    async def test_无播放控件记_no_play_control(self):
        d = FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")})
        p = ScriptedPerceptor({
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: no(Q.FIND_PLAY_CONTROL),
        })
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "no_play_control"

    async def test_trailer_only_不进_播放页(self):
        d = FakeDriver({"https://x.test/movie": obs_at(
            "https://x.test/movie", elements=(("e1", "a", "预告片"),))})
        p = ScriptedPerceptor({
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(
                Q.FIND_PLAY_CONTROL, {"ref": "", "trailer_only": True}),
        })
        ledger = RunLedger("T001")
        result = await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "trailer_only"
        assert d.clicked == []            # 预告片不该被点——它不是播放控件

    async def test_trailer_suspect_交人工不自动判负(self):
        """误杀不可逆：样本没了且没人知道它曾存在。

        payload 形状必须用 :class:`RulePerceptor` **真会发出的那种**：
        ``trailer_only = not suspects``，两者互斥。早先这里写的是
        ``trailer_only=True`` + 有 suspects，而感知层从不这样发——测试绿着，
        控制流里那条分支却一次都走不到（疑似预告会掉进
        ``component_unverified``，变成一条货真价实的假负样本）。
        """
        d = FakeDriver({"https://x.test/movie": obs_at(
            "https://x.test/movie", elements=(("e1", "a", "预告"),))})
        p = ScriptedPerceptor({
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(
                Q.FIND_PLAY_CONTROL,
                {"ref": "", "trailer_only": False, "trailer_suspect": ["观看 trailer 解析"]}),
        })
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "trailer_suspect"
        assert ledger.negative_samples() == []

    async def test_suspect_优先于_trailer_only(self):
        """真·规则版产出的 payload 走一遍控制流——
        疑似预告必须落 ``trailer_suspect`` 而不是被当成「无 ref 契约违规」。"""
        d = FakeDriver({"https://x.test/movie": obs_at(
            "https://x.test/movie", elements=(("e1", "a", "观看 trailer 解析"),))})
        p = RulePerceptor()
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "trailer_suspect"
        assert d.clicked == []

    async def test_answer_true_但无_ref_按_fail_closed_记账(self):
        """I6 被违反时不能硬点一个——ref 错会点到别的元素上。"""
        d = FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")})
        p = ScriptedPerceptor({
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": ""}),
        })
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "component_unverified"
        assert "I6" in ledger.outcomes[0].evidence
        assert d.clicked == []

    async def test_成功路径记_success(self):
        pages = {
            "https://x.test/movie": obs_at(
                "https://x.test/movie", elements=(("e5", "button", "播放"),)),
            "https://x.test/play/e5": obs_at(
                "https://x.test/play/e5", title="播放器", video=1),
        }
        d = FakeDriver(pages)
        p = ScriptedPerceptor({
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e5"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK, {"media_count": 1}),
        })
        ledger = RunLedger("T001")
        result = await visit_site(d, p, ledger, CAND)
        assert result.success
        assert ledger.outcomes[0].branch is None
        assert ledger.outcomes[0].reached is True
        assert d.clicked == ["e5"]

    async def test_点击后_必须重新_observe(self):
        """**这条最容易写错且最难发现**：不重新 observe 就判播放页，
        等于判的是站点页，点击结果完全没参与判定。"""
        pages = {
            "https://x.test/movie": obs_at(
                "https://x.test/movie", elements=(("e5", "button", "播放"),), video=0),
            "https://x.test/play/e5": obs_at(
                "https://x.test/play/e5", title="播放器", video=3),
        }
        d = FakeDriver(pages)
        p = ScriptedPerceptor({
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e5"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        })
        ledger = RunLedger("T001")
        result = await visit_site(d, p, ledger, CAND)
        assert d.observe_calls == 2, "站点页 + 播放页各一次"
        # 第二次观察到的必须是播放页的 URL
        assert result.player_obs.url == "https://x.test/play/e5"
        assert result.player_obs.video_tag_count == 3

    async def test_点击失败记_component_unverified(self):
        pages = {"https://x.test/movie": obs_at(
            "https://x.test/movie", elements=(("e5", "button", "播放"),))}
        d = FakeDriver(pages)

        async def boom(**kw):
            raise DriverError("click failed")
        d.click = boom

        p = ScriptedPerceptor({
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e5"}),
        })
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "component_unverified"

    async def test_开_tab_失败不阻断(self):
        pages = {"https://x.test/movie": obs_at("https://x.test/movie")}
        d = FakeDriver(pages)

        async def boom(url=None):
            raise DriverError("tab failed")
        d.new_tab = boom

        p = ScriptedPerceptor({Q.IS_REACHABLE: no(Q.IS_REACHABLE)})
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "login_wall_or_blocked"   # 走到了判断环节

    async def test_感知层返回_none_一律记_unresolved(self):
        """fail-closed：没判出来不是失败。"""
        for q in (Q.IS_REACHABLE, Q.FIND_PLAY_CONTROL, Q.PLAYER_OK):
            d = FakeDriver({"https://x.test/movie": obs_at("https://x.test/movie")})
            ledger = RunLedger("T001")
            await visit_site(d, ScriptedPerceptor({}), ledger, CAND)
            assert ledger.outcomes[0].branch == "unresolved", q
            assert ledger.negative_samples() == []

    async def test_前置_none_不中断_继续收集证据(self):
        """**fail-closed 管结论不管采集**。

        实测逼出来的：W1 规则版对 IS_REACHABLE 只会返回 None，
        若据此中断，5 个真实候选站点全落 unresolved，负样本池一条不产，
        W1 验收直接不达标。

        继续走**不等于**断言「页面可达」——分支仍按有没有得到
        确定性结论来定：拿到 False 就是货真价实的 no_play_control。
        """
        d = FakeDriver({"https://x.test/movie": obs_at(
            "https://x.test/movie", elements=(("e1", "a", "首页"),))})
        p = ScriptedPerceptor({
            Q.IS_REACHABLE: none(Q.IS_REACHABLE),      # 前置判不出来
            Q.FIND_PLAY_CONTROL: no(Q.FIND_PLAY_CONTROL),   # 但这一题有确定结论
        })
        ledger = RunLedger("T001")
        result = await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "no_play_control"
        assert ledger.negative_samples()
        assert any("不中断" in n for n in (result.notes or []))

    async def test_前置_false_仍然中断(self):
        """None 不中断 ≠ 所有 None 都放过。**确定判 False 时必须停**——
        页面明明是登录墙还继续点控件，点出来的结论没有意义。"""
        d = FakeDriver({"https://x.test/movie": obs_at(
            "https://x.test/movie", elements=(("e1", "a", "播放"),))})
        p = ScriptedPerceptor({
            Q.IS_REACHABLE: no(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
        })
        ledger = RunLedger("T001")
        await visit_site(d, p, ledger, CAND)
        assert ledger.outcomes[0].branch == "login_wall_or_blocked"
        assert d.clicked == []          # 登录墙上不该点播放


# ═══════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════


class TestOrchestrator:
    def _search_page(self, *links):
        return Observation(
            url="https://www.baidu.com/s?wd=x", page_title="百度",
            body_text="搜索结果",
            links=tuple(LinkItem(text=t, href=h) for t, h in links),
        )

    async def test_取候选并遍历(self):
        pages = {
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B":
                self._search_page(
                    ("功夫电影", "https://www.a.test/movie"),
                    ("知乎讨论", "https://www.zhihu.com/question/1"),
                ),
            "https://www.a.test/movie": obs_at("https://www.a.test/movie"),
        }
        d = FakeDriver(pages)
        cfg = RunConfig(stop_after_success=1)
        orch = Orchestrator(d, ScriptedPerceptor({Q.IS_REACHABLE: no(Q.IS_REACHABLE)}), cfg)
        rec = await orch.run("T001", "功夫")

        assert rec.title == "功夫"
        assert "功夫 在线观看" in rec.query
        # 知乎在结构性排除表里 → 剔除；a.test 不在任何表里 → 保留
        assert [c.url for c in rec.candidates] == ["https://www.a.test/movie"]
        assert rec.candidate_source == "heuristic"
        assert rec.ledger is not None and len(rec.ledger.outcomes) == 1

    async def test_代码不做语义筛选(self):
        """`news.b.test` 这种域名**必须**留在候选里。

        写成「剔除以 news. 开头的域名」会是错的——`newsletter.com`、`newsy.com`
        也以 news 开头，而国内视频站的域名里带 news 的更不少见。
        代码能做的是剔除**结构上就不是内容站**的（社交 / CDN / 搜索引擎自身），
        「这是不是播放站」是 SELECT_PLAY_SITES 的语义职责。
        """
        pages = {
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B":
                self._search_page(("新闻", "https://news.b.test/x")),
        }
        d = FakeDriver(pages)
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert [c.url for c in rec.candidates] == ["https://news.b.test/x"]

    async def test_搜索页_失败时提前返回且不崩(self):
        d = FakeDriver()                       # 搜索页也不存在
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert rec.candidates == []
        assert rec.warnings
        assert rec.succeeded is False

    async def test_超时按_unresolved_记账而非静默消失(self):
        """超时的站点若不记账，样本分布里会凭空少一块——看不见的损失。"""
        import asyncio

        pages = {
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B":
                self._search_page(("a", "https://slow.test/movie")),
            "https://slow.test/movie": obs_at("https://slow.test/movie"),
        }
        d = FakeDriver(pages)

        # 让该站点的观察真的慢到超时——不 sleep 的话 wait_for 可能来不及触发，
        # 那测到的就不是超时路径
        fast = d.observe

        async def slow_observe(*, max_chars=None):
            if d.current and d.current.url.startswith("https://slow.test"):
                await asyncio.sleep(0.5)
            return await fast(max_chars=max_chars)

        d.observe = slow_observe

        cfg = RunConfig(per_site_timeout_s=0.02)
        orch = Orchestrator(d, ScriptedPerceptor({}), cfg)
        rec = await orch.run("T001", "功夫")
        assert rec.ledger.outcomes[0].branch == "unresolved"
        assert any("超时" in w for w in rec.warnings)

    async def test_stop_after_success_生效(self):
        base = ("{host}/movie", "")
        links = [(f"站{i}", f"https://s{i}.test/movie") for i in range(5)]
        pages = {
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B":
                self._search_page(*links),
        }
        for i in range(5):
            pages[f"https://s{i}.test/movie"] = obs_at(
                f"https://s{i}.test/movie", elements=(("e1", "button", "播放"),))
            pages[f"https://x.test/play/e1"] = obs_at(
                "https://x.test/play/e1", video=1)

        d = FakeDriver(pages)
        script = {
            Q.IS_REACHABLE: ok(Q.IS_REACHABLE),
            Q.FIND_PLAY_CONTROL: ok(Q.FIND_PLAY_CONTROL, {"ref": "e1"}),
            Q.PLAYER_OK: ok(Q.PLAYER_OK),
        }
        orch = Orchestrator(d, ScriptedPerceptor(script), RunConfig(stop_after_success=2))
        rec = await orch.run("T001", "功夫")
        assert len(rec.visits) == 2


# ═══════════════════════════════════════════════════════════════════════
# 审计字段
# ═══════════════════════════════════════════════════════════════════════


class TestRunRecordAudit:
    async def test_候选来源必须显式标注(self):
        """读档的人若不知道候选来自启发式，会把「W1 选出的站点」
        误读成「规则版能选出正确站点」——它不能。"""
        from trajectory_pipeline.executor.orchestrator import RunRecord
        rec = RunRecord(task_id="T001", title="功夫", query="q",
                        search_url="u", candidate_source="heuristic")
        assert rec.to_json()["candidate_source"] == "heuristic"

    async def test_观察序列化_只留摘要不留全文(self):
        from trajectory_pipeline.executor.orchestrator import RunRecord
        big = obs_at("https://x.test/", body="x" * 100_000)
        rec = RunRecord(task_id="T001", title="t", query="q", search_url="u",
                        search_obs=big)
        j = rec.to_json()["search_observation"]
        assert j["body_len"] == 100_000
        assert len(j["body_preview"]) == 400

    async def test_序列化不含页面句柄(self):
        from trajectory_pipeline.executor.orchestrator import RunRecord
        rec = RunRecord(task_id="T001", title="t", query="q", search_url="u",
                        search_obs=obs_at("https://x.test/"))
        blob = repr(rec.to_json())
        for token in ("page_driver", "McpClient", "obscura", "_driver"):
            assert token not in blob


# ═══════════════════════════════════════════════════════════════════════
# 反爬拦截：必须与「素材没价值」分开
# ═══════════════════════════════════════════════════════════════════════


class TestAntiScraping:
    """实测：连跑 3 个 task，第 1 个正常，后 2 个被百度验证码页拦下。

    拦截的表现是「正文短 + 零链接」，若混进「搜索页未提取到候选」，
    处置就错了——那句话说「这批素材没价值」，真相是「搜索引擎把我们拦了」。
    """

    CAPTCHA_URL = "https://wappass.baidu.com/static/captcha/tuxing_v2.html?logid=1"

    async def test_被拦时短路且不产候选(self):
        d = FakeDriver({
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B":
                Observation(url=self.CAPTCHA_URL, page_title="百度安全验证",
                            body_text="网络不给力，请稍后重试"),
        })
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert rec.search_blocked == "captcha"
        assert rec.candidates == []
        assert any("被反爬拦截" in w for w in rec.warnings)

    async def test_告警必须否定素材质量说法(self):
        """措辞本身就是防线——读的人第一眼看到的就是它。"""
        d = FakeDriver({
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B":
                Observation(url=self.CAPTCHA_URL, page_title="百度安全验证",
                            body_text="x"),
        })
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        joined = " ".join(rec.warnings)
        assert "不是素材质量问题" in joined
        assert "换引擎" in joined

    async def test_被拦不进负样本池(self):
        """⚠️ 拦截发生在**取候选之前**，根本没有 outcome——
        塞进 8 条分支会让负样本池混入连站点都没访问到的条目。"""
        d = FakeDriver({
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B":
                Observation(url=self.CAPTCHA_URL, page_title="百度安全验证",
                            body_text="x"),
        })
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert rec.ledger is not None and rec.ledger.outcomes == []
        assert rec.ledger.negative_samples() == []

    async def test_落盘带search_blocked(self):
        from trajectory_pipeline.executor.orchestrator import RunRecord
        rec = RunRecord(task_id="T001", title="t", query="q", search_url="u",
                        search_blocked="captcha")
        assert rec.to_json()["search_blocked"] == "captcha"

    async def test_未被拦时字段为空(self):
        d = FakeDriver({
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B":
                Observation(url="https://www.baidu.com/s?wd=x", page_title="百度",
                            body_text="结果",
                            links=(LinkItem(text="a", href="https://a.test/x"),)),
        })
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert rec.search_blocked == ""

    async def test_候选过滤落进存档(self):
        """「引擎给了 50 个链接、我们只跑了 11 个」中间那批去哪了要能查到。"""
        d = FakeDriver({
            "https://www.baidu.com/s?wd=%E5%8A%9F%E5%A4%AB+%E5%9C%A8%E7%BA%BF%E8%A7%82%E7%9C%8B":
                Observation(url="https://www.baidu.com/s?wd=x", page_title="百度",
                            body_text="结果",
                            links=(LinkItem(text="备案", href="https://beian.miit.gov.cn/"),
                                   LinkItem(text="腾讯", href="https://v.qq.com/x/1"),
                                   LinkItem(text="百科", href="https://baike.baidu.com/x"))),
        })
        orch = Orchestrator(d, ScriptedPerceptor({}), RunConfig())
        rec = await orch.run("T001", "功夫")
        assert "impossible_content:gov.cn" in rec.candidate_filter
        assert "excluded_host:baidu.com" in rec.candidate_filter
        assert rec.to_json()["candidate_filter"] == rec.candidate_filter

    async def test_报告重算存量存档(self):
        """字段引入之前跑的存档没有 ``search_blocked``。

        若直接 ``.get(...) or ""``，那些被验证码页拦下的存档会被算成
        「可跑」——恰恰是最不该算的那批。**缺字段 ≠ 空值。**
        存档里的观察数据都在，用同一个 :func:`detect_block` 确定性重算。
        """
        import json
        from trajectory_pipeline.executor.cli import cmd_report
        import argparse
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "T001__a.json").write_text(json.dumps({
                "task_id": "T001", "query": "q", "ledger": {"total_sites": 0,
                                                          "by_branch": {}, "missing_branches": []},
                # 旧格式：没有 search_blocked 键
                "search_observation": {
                    "url": "https://wappass.baidu.com/static/captcha/tuxing_v2.html?x=1",
                    "page_title": "百度安全验证",
                    "body_preview": "网络不给力，请稍后重试",
                },
            }, ensure_ascii=False), encoding="utf-8")
            (root / "T018__b.json").write_text(json.dumps({
                "task_id": "T018", "query": "q", "search_blocked": "",
                "ledger": {"total_sites": 1, "by_branch": {"unresolved": 1},
                           "missing_branches": []},
                "search_observation": {
                    "url": "https://www.baidu.com/s?wd=x", "page_title": "功夫_百度搜索",
                    "body_preview": "功夫 在线观看"},
            }, ensure_ascii=False), encoding="utf-8")

            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cmd_report(argparse.Namespace(out=str(root)))
            out = buf.getvalue()
        assert "被反爬拦截 1/2" in out
        assert "存量存档重算" in out
        assert "T001__a.json" in out
        # 显式记了空串的那条**不算**重算，且仍算可跑
        assert "可跑 1 个" in out