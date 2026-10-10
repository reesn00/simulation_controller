"""D-4「播放组件验证正确性」评分器的单元测试。

两处判反都在真实数据上发生过，且**症状都是「分数低了」**，
看不出是评分器把管辖范围搞错了：

1. **管辖范围**：第一版把「应判未验证」这一项套在**所有**分支上，
   于是 95/196 条样本被判 4 分——而那 95 条是判断点 ①②③ 的负样本，
   它们说的是「这个站能不能到」，与「播放组件在不在」毫无关系。
   → :class:`Test管辖范围`
2. **判据**：第一版用 ``truncated`` / ``degraded`` 当「采集不充分」，
   而 ``video_tag_count`` 来自**独立的 ``browser_count`` 调用**、
   不受 ``max_chars`` 正文预算影响；实测 ``degraded`` 只有 ``body_text``
   一种取值也不影响它。两条都是判反。
   → :class:`Test判据`：改用「点击前后是不是同一个页面」——
   这才是 rubric 边界规则里「组件懒加载需触发」的可机械形态。

外加三处设计边界：
- **iframe 不作判据**（rubric 反例明写「不作活跃扣分项」）；
- **变更 E 未落地 → player 类名判据 skipped**，不静默省略；
- **rubric 内部口径冲突**（决策树说「无 video/组件」而
  ``BRANCH_PRECONDITION`` 说「有 video 标签」）→ 判 undetermined 并留痕，
  **不替 rubric 选边**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.assembler.schema import (
    GateMark, OutcomeRef, Sample,
)
from trajectory_pipeline.evaluation.scorers import d4_scorer
from trajectory_pipeline.evaluation.scorers.d4_scorer import score_d4


def _obs(url="https://x.test/play", video=0, iframe=0, body="正文内容",
         elements=3, **kw):
    base = {"url": url, "page_title": "T", "body_text": body,
            "video_tag_count": video, "iframe_count": iframe,
            "interactive_elements": [{"tag": "a", "label": f"e{i}"}
                                     for i in range(elements)],
            "links": [], "degraded": [], "truncated": False}
    base.update(kw)
    return base


def _out(branch, question="player.ok", source="llm", fallback=False,
         reached=True, **kw):
    base = {"url": "https://x.test", "branch": branch, "source": source,
            "fallback_used": fallback, "succeeded": branch is None,
            "question": question, "reached_play_page": reached,
            "site_url": "https://x.test", "play_page_url": "https://x.test/play",
            "p1_missing_fields": []}
    base.update(kw)
    return base


def _sample(outcome, observations=()):
    return Sample(
        sample_id="T001__a1__site-1", task_id="T001", unit="site-1",
        title="功夫", system_prompt="s", tools=(), user_prompt="u",
        provenance={}, actions=(), observations=tuple(observations),
        rationale=None, gate=GateMark("not_run"),
        outcome=OutcomeRef(**{k: v for k, v in outcome.items()
                              if k != "p1_missing_fields"})
        if outcome else None,
    )


def _dict_sample(outcome, observations=()):
    return {"sample_id": "T001__a1__site-1", "task_id": "T001",
            "unit": "site-1", "title": "功夫",
            "six": {"observations": list(observations)}, "outcome": outcome}


def _changed_pair(video=1, iframe=1):
    """点击前后**确实换了页**的一对观察。"""
    return [_obs(url="https://x.test/movie", video=0, iframe=0),
            _obs(url="https://x.test/play", video=video, iframe=iframe)]


# ── 1. 管辖范围 ───────────────────────────────────────────────────


class Test管辖范围:
    @pytest.mark.parametrize("branch,question", [
        ("not_play_site", "select.play_sites"),
        ("login_wall_or_blocked", "site.is_reachable"),
        ("no_play_control", "site.find_play_control"),
        ("trailer_only", "site.find_play_control"),
        ("trailer_suspect", "site.find_play_control"),
        ("unreachable_hard", ""),
        ("unresolved", "site.find_play_control"),
        ("player_unverified", "player.ok"),
    ])
    def test_非组件结论不判低分(self, branch, question):
        """判断点 ①②③ 的结论与「播放组件在不在」无关。
        第一版套在所有分支上，实测 95/196 条被判 4 分。"""
        s = _sample(_out(branch, question=question, reached=False,
                         source="rule"),
                    observations=[_obs(video=0, iframe=0)])
        v = score_d4(s)
        assert v.score in (5, None), f"{branch} 被判了 {v.score}"

    def test_I6违约不要求第二份观察(self):
        """``find_play_control`` 的 ``component_unverified`` 是
        「判有控件却拿不到 ref」，发生在**站点页**、点击压根没发生——
        没有 player_obs 是正常的，拿它扣分等于要求一份不存在的快照。"""
        s = _sample(_out("component_unverified", question="site.find_play_control"),
                    observations=[_obs(url="https://x.test/movie")])
        assert score_d4(s).score == 5

    def test_搜索单元判不可判定(self):
        """没有 outcome 就没核过，判 5 分是「没查」渲染成「查过了」。"""
        v = score_d4(_dict_sample(None))
        assert v.score is None and v.kind == "undetermined"


# ── 2. 判据：点击前后是否换页 ─────────────────────────────────────


class Test判据:
    def test_点击前后同页判4分(self):
        """rubric 边界规则「组件懒加载需触发」的可机械形态。
        实测 11 条真实样本是这个形态（mgtvtv / bilibili / youku / sohu）。

        ``iframe=1`` 是必要的隔离：video 与 iframe **同时**为 0 会先撞上
        「存档无任何播放元素却判成功」（1 分，更重的一档），
        那样测的就不是「同页」这条判据了。"""
        same = [_obs(url="https://x.test/play", iframe=1),
                _obs(url="https://x.test/play", iframe=1)]
        s = _sample(_out(None, source="llm"), observations=same)
        v = score_d4(s)
        assert v.score == 4

    def test_点击前后换页判5分(self):
        s = _sample(_out(None), observations=_changed_pair(video=1))
        assert score_d4(s).score == 5

    def test_只有一份观察判4分(self):
        """点击之后没取到第二份快照——那一步根本没产生观察。"""
        s = _sample(_out(None, source="llm"),
                    observations=[_obs(iframe=1)])
        assert score_d4(s).score == 4

    def test_不拿truncated当判据(self):
        """``video_tag_count`` 来自独立 ``browser_count`` 调用，
        不受 ``max_chars`` 正文预算影响。判据错一次就误判一整批。"""
        obs = [_obs(url="https://x.test/a", truncated=True, video=1),
               _obs(url="https://x.test/b", truncated=True, video=1)]
        s = _sample(_out(None), observations=obs)
        assert score_d4(s).score == 5, "正文截断被当成了组件判据不可靠"

    def test_不拿degraded_body_text当判据(self):
        """实测 39 份存档里 ``degraded`` 只有 ``body_text`` 一种取值（54 次），
        而它不影响媒体计数。"""
        obs = [_obs(url="https://x.test/a", degraded=["body_text"], video=1),
               _obs(url="https://x.test/b", video=1)]
        s = _sample(_out(None), observations=obs)
        assert score_d4(s).score == 5

    def test_同页判定只比三个粗粒度特征(self):
        """逐字段比对会把「同一页的两次快照」判成不同页
        （http→https 跳转、懒加载微调都会造成）。"""
        a = _obs(url="https://x.test/p", video=0, elements=5, iframe=1)
        b = _obs(url="https://x.test/p", video=0, elements=5, iframe=1)
        b["page_title"] = "加载中…"
        s = _sample(_out(None, source="llm"), observations=[a, b])
        assert score_d4(s).score == 4, "page_title 微调不应被判成换了页"


# ── 3. iframe 不作判据 ────────────────────────────────────────────


class TestIframe不作判据:
    def test_只有iframe不构成有组件(self):
        """rubric 反例「存档仅含广告 iframe → 有组件，记录」明写
        **已知并已修复，不作活跃扣分项**。"""
        v = score_d4(_sample(_out(None), observations=_changed_pair(
            video=0, iframe=15)))
        f = next(c for c in v.checks if "媒体证据" in c.name)
        assert f.kind == "undetermined", "只凭 iframe 不该肯定「有组件」"
        assert f.tier == "", "iframe 反例不该被记为误判（rubric：不作扣分项）"

    def test_video大于0才判成立(self):
        v = score_d4(_sample(_out(None), observations=_changed_pair(video=2)))
        f = next(c for c in v.checks if "媒体证据" in c.name)
        assert f.kind == "ok"

    def test_无任何播放元素却判成功判1分(self):
        """rubric 锚点 1 原文：「存档无任何播放元素却判成功」。"""
        obs = [_obs(url="https://x.test/a", video=0, iframe=0),
               _obs(url="https://x.test/b", video=0, iframe=0)]
        v = score_d4(_sample(_out(None, source="llm"), observations=obs))
        assert v.score == 1

    def test_rule判成功但video为0判不可判定(self):
        """**当前代码判不出这个形态**（``_player_ok`` 只在 video>=1 时判 True），
        所以它来自旧代码批次。按 rubric「不作活跃扣分项」只留痕。"""
        obs = [_obs(url="https://x.test/a", video=0, iframe=2),
               _obs(url="https://x.test/b", video=0, iframe=2)]
        v = score_d4(_sample(_out(None, source="rule", fallback=True),
                             observations=obs))
        f = next(c for c in v.checks if "媒体证据" in c.name)
        assert f.kind == "undetermined"
        assert "当前代码" in f.why


# ── 4. 变更 E 未落地 ──────────────────────────────────────────────


class Test变更E:
    def test_player类名判据skipped并说明(self):
        """**不静默省略**——省略会让人以为这条判据也核过了。"""
        f = next(c for c in score_d4(_sample(_out(None),
                                              observations=_changed_pair()))
                 .checks if "player" in c.name)
        assert f.kind == "skipped"
        assert "变更 E" in f.why

    def test_判据可用性由常量控制(self):
        """变更 E 落地后这个值翻 True，那条判据才从 skipped 变成真核对。
        留一个永不改变的死值等于把判据悄悄丢掉。"""
        assert d4_scorer.PLAYER_CLASS_JUDGE_AVAILABLE is False
        assert "未落地" in d4_scorer.__doc__


# ── 5. rubric 口径裁定（2026-10-10）──────────────────────────────


class TestRubric口径裁定:
    """裁定前：决策树说「无 video/组件」、``BRANCH_PRECONDITION`` 说
    「有 video 标签」，两种读法互斥，本检查判 undetermined 不选边。
    裁定结果**按决策树**：分支语义是「到了播放页，没有可验证的播放
    组件」，``video_tag_count`` 不再是判据，「点击是否换页」归检查 3。"""

    def test_video为0与决策树一致(self):
        v = score_d4(_sample(_out("component_unverified"),
                             observations=_changed_pair(video=0, iframe=1)))
        f = next(c for c in v.checks if "component_unverified" in c.name)
        assert f.kind == "ok"
        assert "决策树" in f.why

    def test_有video时结论不矛盾(self):
        """llm 判「不能正常播放」可以有 video 之外的判据
        （片源缺失 / 播放器未起）——组件在页面上与判 False 不矛盾。"""
        v = score_d4(_sample(_out("component_unverified"),
                             observations=_changed_pair(video=1)))
        f = next(c for c in v.checks if "component_unverified" in c.name)
        assert f.kind == "ok"


# ── 6. 严重度分档 ────────────────────────────────────────────────


class Test分档:
    def test_无判错判5分(self):
        s = _sample(_out("component_unverified"),
                    observations=_changed_pair(video=1))
        assert score_d4(s).score == 5

    def test_混判取最重档(self):
        """出现「判成功但无播放元素」就不可能因别处是「应判未验证」
        而升到 3/4 档——两条是不同性质的问题。"""
        obs = [_obs(url="https://x.test/p", video=0, iframe=0),
               _obs(url="https://x.test/p", video=0, iframe=0)]
        s = _sample(_out(None, source="llm"), observations=obs)
        assert score_d4(s).score == 1


# ── 7. 输入形态与序列化 ───────────────────────────────────────────


class Test输入形态:
    def test_dataclass与字典结论一致(self):
        o = _out(None)
        a = score_d4(_sample(o, observations=_changed_pair(video=1)))
        b = score_d4(_dict_sample(o, observations=_changed_pair(video=1)))
        assert a.score == b.score and a.kind == b.kind

    def test_可JSON序列化(self):
        v = score_d4(_sample(_out(None), observations=_changed_pair(video=1)))
        json.dumps(v.to_json(), ensure_ascii=False)

    def test_含维度名(self):
        v = score_d4(_sample(_out(None), observations=_changed_pair(video=1)))
        assert v.to_json()["dimension"] == "D-4"


# ── 8. 真实存档回归 ───────────────────────────────────────────────


class TestOnRealSamples:
    def test_低分条目形态一致(self):
        """D-4 在真实数据上判出的低分**应当全是**「点击前后同一页」
        这一种形态——那是 rubric 边界规则明写的「组件懒加载未触发」。
        形态不一致就说明检查项本身在漂。

        顺带印证 D-4 的价值：其中含 ``check-archives`` 漏掉的
        「判成功但有 iframe」那一类（门禁只查「判成功但 video=0」）。
        """
        root = Path(__file__).resolve().parents[2] / "output" / "pipeline" / "samples"
        files = sorted(root.glob("*.json"))
        if not files:
            pytest.skip("没有 P2 样本（先跑 cli build）")
        low = [json.loads(p.read_text(encoding="utf-8")) for p in files
               if (score_d4(json.loads(p.read_text(encoding="utf-8"))).score
                   or 5) < 5]
        for s in low:
            wrong = score_d4(s).wrong
            assert wrong, f"{s.get('sample_id')} 判了低分但没有 wrong 项"
            assert all(c.tier == "premature" for c in wrong), (
                f"{s.get('sample_id')} 的低分来自非 prematurity 档："
                f"{[(c.name, c.tier) for c in wrong]}")

    def test_低分不得包含非第四判断点的结论(self):
        """**变异测试逼出来的一条**：把管辖范围退回「所有分支」后，
        本文件只红 1 条，而真实数据从 153/11 变 152/12——I6 违约那条
        被误判成 4 分。

        ``test_低分条目形态一致`` 抓不住它：I6 那条被判的**也是**
        ``premature`` 档（确实只有一份观察），形态一致、蒙混过关。
        所以这里直接盯**判断点**：D-4 只核 ``player.ok``。
        """
        root = Path(__file__).resolve().parents[2] / "output" / "pipeline" / "samples"
        files = sorted(root.glob("*.json"))
        if not files:
            pytest.skip("没有 P2 样本（先跑 cli build）")
        for p in files:
            s = json.loads(p.read_text(encoding="utf-8"))
            v = score_d4(s)
            if not v.wrong:
                continue
            question = (s.get("outcome") or {}).get("question")
            assert question == d4_scorer.GOVERNING_QUESTION, (
                f"{p.stem} 的结论出自判断点 {question!r}，"
                f"不归 D-4 管辖却被判了错——检查项越界了")

    def test_不出现无观察却判5分(self):
        """没有观察就核不了组件，判 5 分等于「没查」渲染成「查过了」。"""
        root = Path(__file__).resolve().parents[2] / "output" / "pipeline" / "samples"
        files = sorted(root.glob("*.json"))
        if not files:
            pytest.skip("没有 P2 样本（先跑 cli build）")
        for p in files:
            s = json.loads(p.read_text(encoding="utf-8"))
            v = score_d4(s)
            if v.score == 5:
                obs = (s.get("six") or {}).get("observations") or []
                assert len(obs) >= 2 or (s.get("outcome") or {}).get("branch"), (
                    f"{p.stem}：无观察、无分支，却判了 5 分")
