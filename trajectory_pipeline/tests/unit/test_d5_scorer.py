"""D-5「记录/排除决策正确性」评分器的单元测试。

重点不是「能打几分」，而是**四件判反了就静默的事**——
它们全都在本模块开发时真实发生过，且症状都是「分数低了」，
看不出是评分器错了：

1. **映射表不得手写**。第一版手写字面量、漏了 ``unresolved``
   （fail-closed 的通用出口），48 条正确结论被判成「分支走错」。
   → :class:`Test同源`：映射从注册表推导，且**覆盖实测数据里的每一对**。
2. **前置事实按二元组判，不按分支一刀切**。把 ``unresolved`` 整条
   标成要求播放页，61 条 ``find_play_control → unresolved``
   （正常记账）被判错。
   → :class:`Test前置事实`。
3. **dataclass 与 P2 字典都要吃**。只用 ``getattr`` 时，传字典的调用
   路径恒取到 ``None`` → 全部判「无决策可判」，而不可判定看起来像
   一条正常结果。
   → :class:`Test输入形态`。
4. **反查节点名不许抛**。按字符串反查却找不到时 ``next()`` 抛
   ``StopIteration``，症状像数据问题。
   → :class:`Test不抛异常`。
5. **判据不许全从自己身上取**。前三条检查读的 ``branch`` /
   ``question`` / ``reached`` 出自同一处记账，**同源自洽**：
   接线一错它们跟着一起错，接线不错就恒真——465 条真实样本上
   4/3/2/1 四档一次都没出过。第四条检查（结论的证据核）读的是
   **另一条信息通道**（判分理由 / 观察正文），才可能判出「结论
   贴反了」。
   → :class:`Test证据核`。

外加**真实存档回归**：真实样本上的低分必须**全部由证据核判出**
（这是唯一一处已裁定方向），前三条检查一条都不许判低分。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.assembler.schema import OutcomeRef, Rationale, Sample
from trajectory_pipeline.evaluation.scorers import d5_scorer
from trajectory_pipeline.evaluation.scorers.d5_scorer import score_d5


def _sample(
    outcome: dict | None,
    unit: str = "site-1",
    *,
    body: str = "",
    rationale: str = "",
) -> Sample:
    """``body`` 落进观察正文（**证据核只认这一路**）；
    ``rationale`` 落进 ``rationales``（**动作前动机，不算证据**）。"""
    return Sample(
        sample_id=f"T001__a1__{unit}",
        task_id="T001",
        unit=unit,
        title="功夫",
        system_prompt="s",
        tools=(),
        user_prompt="u",
        provenance={},
        actions=(),
        observations=({"body_text": body},) if body else (),
        rationales=(Rationale(step_index=0, text=rationale),) if rationale else (),
        gate=__import__(
            "trajectory_pipeline.assembler.schema", fromlist=["GateMark"]
        ).GateMark("not_run"),
        outcome=OutcomeRef(**outcome) if outcome else None,
    )


def _out(branch, question="player.ok", reached=True, **kw):
    base = {
        "url": "https://x.test",
        "branch": branch,
        "source": "rule",
        "fallback_used": False,
        "succeeded": branch is None,
        "question": question,
        "reached_play_page": reached,
        "site_url": "https://x.test",
        "play_page_url": "",
    }
    base.update(kw)
    return base


def _dict_sample(outcome: dict | None):
    """P2 落盘形态——**dict**，不是 dataclass。"""
    return {
        "sample_id": "T001__a1__site-1",
        "task_id": "T001",
        "unit": "site-1",
        "outcome": outcome,
    }


# ── 1. 同源 ───────────────────────────────────────────────────────


class Test同源:
    """映射表**从注册表推导**，且必须覆盖实测数据里的每一对
    ``(question, branch)``。"""

    def test_映射不是手写字面量(self):
        """真值源是 :data:`questions.REGISTRY`。改注册表就改映射——
        不同源则两边安静地互相打架（避坑指南 §1.1）。"""
        from trajectory_pipeline.perception import questions

        for qid, q in questions.REGISTRY.items():
            assert qid in d5_scorer.QUESTION_TO_BRANCHES
            if q.negative_branch:
                assert q.negative_branch in d5_scorer.QUESTION_TO_BRANCHES[qid], (
                    f"{qid} 的 negative_branch={q.negative_branch} 没进推导出的映射"
                )

    def test_每个判断点都含unresolved(self):
        """``record_from`` 在 ``answer is None`` 时**一律**记 unresolved，
        与判断点无关——它是通用 fail-closed 出口。"""
        for qid, branches in d5_scorer.QUESTION_TO_BRANCHES.items():
            assert "unresolved" in branches, f"{qid} 漏了 unresolved"

    def test_player_ok含player_unverified(self):
        """它走 ``record_unresolved_reached``，不经过 ``record_from``，
        所以查不到 ``negative_branch``——但它确实是合法产出。"""
        assert "player_unverified" in d5_scorer.QUESTION_TO_BRANCHES["player.ok"]

    def test_代码判定分支不参与配对(self):
        """``unreachable_hard`` / ``trailer_only`` / ``trailer_suspect``
        不经 LLM，question 本就是空的——拿空字段去比得到的是
        「采集器没填」，不是「分支走错」。"""
        for br in ("unreachable_hard", "trailer_only", "trailer_suspect"):
            v = score_d5(_sample(_out(br, question="")))
            assert v.score == 5, f"{br} 是代码判定分支，不该被判错"

    def test_站内搜索收尾分支不参与配对(self):
        """2026-10-16 路径 B/C：``site_search_routed`` / ``site_search_no_results``
        由代码按子候选数量选用（``_finish``），记账刻意不带 question——
        归属是发现链而不是选题那步的 ``select.play_sites``。"""
        for br in ("site_search_routed", "site_search_no_results"):
            v = score_d5(_sample(_out(br, question="", reached=False)))
            assert v.score == 5, f"{br} 是代码判定分支，不该被判错"

    def test_站点首页核可判5分(self):
        """``_drop_site_homepages`` 是**代码层确定性事实核**：path 为 `/`
        的候选在任何判断点之前就被判 ``not_play_site``，question 恒为空。
        判据全在代码/存档里，**应当正常评 5 分**——不能因为 question
        为空就落成「老批次缺字段」的不可判定（那是 2026-10-10 之前的
        旧 P2 才有的形态，两码事）。"""
        v = score_d5(_sample(_out("not_play_site", question="", reached=False)))
        assert v.score == 5, f"站点首页核应可判定，实际 {v!r}"
        assert v.kind == "scored", "确定性事实核不该判不可判定"

    def test_覆盖真实存档里的每一对(self):
        """把现存存档里的 ``(question, branch)`` 全跑一遍，
        任何一个落在映射外都是判反。"""
        pairs = _real_pairs()
        if not pairs:
            pytest.skip("没有真实 P1 存档（产物目录未入库）")
        bad = []
        # 按 (question, branch) 排序——``branch`` 可能是 ``None``（成功记录），
        # 直接 ``sorted(pairs)`` 会因 None 与 str 不可比而抛 TypeError。
        for q, b in sorted(pairs, key=lambda p: (p[0], p[1] or "")):
            if b is None:
                # 成功记录：只有 ``player.ok`` 判 True 能得出——由
                # ``_check_branch_matches_question`` 单独核（它在
                # ``CODE_ONLY_BRANCHES`` 之前判，因为成功不是代码分支）。
                continue
            if b in d5_scorer.CODE_ONLY_BRANCHES:
                continue
            allowed = d5_scorer.QUESTION_TO_BRANCHES.get(q)
            if allowed is None or b not in allowed:
                bad.append((q, b))
        assert not bad, f"真实数据里出现映射外的组合：{bad}"


def _real_pairs() -> set[tuple[str, str | None]]:
    root = Path(__file__).resolve().parents[2] / "output" / "pipeline"
    out: set[tuple[str, str | None]] = set()
    for p in sorted(root.glob("T*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for o in doc.get("outcomes") or ():
            out.add((str(o.get("question") or ""), o.get("branch")))
    return out


# ── 2. 前置事实 ───────────────────────────────────────────────────


class Test前置事实:
    def test_前三判断点的结论不需要到播放页(self):
        """``not_play_site`` / ``login_wall_or_blocked`` / ``no_play_control``
        本来就发生在到达播放页**之前**，``reached=False`` 是正确的。
        搞反的后果是整批负样本被判成跳步。

        ⚠️ 断言的是**「结论的前置事实」这一条检查的结论**，不是总分：
        2026-10-23 加了第四条检查（结论的证据核）之后，拦截类分支
        在没有依据原文时是 ``undetermined``——那是「核不了这个结论
        对不对」，与「前置事实搞反了」是两件事，不能混在一个分数里断言。
        """
        for br, q in (
            ("not_play_site", "select.play_sites"),
            ("login_wall_or_blocked", "site.is_reachable"),
            ("unreachable_hard", "site.is_reachable"),
            ("no_play_control", "site.find_play_control"),
            ("trailer_only", "site.find_play_control"),
            ("trailer_suspect", "site.find_play_control"),
        ):
            v = score_d5(_sample(_out(br, question=q, reached=False)))
            reached = [c for c in v.checks if c.node == d5_scorer._NODE_REACHED]
            assert reached and reached[0].kind == "ok", (
                f"{br} 在到达播放页前判定，前置事实核却判了 "
                f"{reached[0].kind if reached else '（没这一条）'}——"
                f"整批负样本会被算成跳步"
            )
            assert not v.wrong_nodes, f"{br} 不该有任何一条检查判错：{v.wrong_nodes}"

    def test_环节三的unresolved不需要到播放页(self):
        """**这是本模块踩过的最深的一个坑**：``unresolved`` 是通用出口，
        环节③ 产出它时「不确定的是能不能到」，压根没到播放页。
        第一版把它整条标成要求播放页，61 条正确记账被判成 3 分。"""
        v = score_d5(
            _sample(
                _out("unresolved", question="site.find_play_control", reached=False)
            )
        )
        assert v.score == 5, "环节③ 的 unresolved 是正常的 fail-closed 记账"

    def test_环节四的unresolved要求到播放页(self):
        v = score_d5(_sample(_out("unresolved", question="player.ok", reached=False)))
        assert v.score is not None and v.score < 5, (
            "环节④ 的 unresolved 却没到播放页——前提不成立"
        )

    def test_find_play_control的component_unverified不要求播放页(self):
        """I6 违约记账发生在**点击之前**，到不了播放页。"""
        v = score_d5(
            _sample(
                _out(
                    "component_unverified",
                    question="site.find_play_control",
                    reached=False,
                )
            )
        )
        assert v.score == 5, "I6 违约记账在点击前发生，不该被判错"

    def test_player_unverified要求播放页(self):
        v = score_d5(
            _sample(_out("player_unverified", question="player.ok", reached=False))
        )
        assert v.score is not None and v.score < 5


# ── 3. 输入形态 ───────────────────────────────────────────────────


class Test输入形态:
    def test_dataclass样本(self):
        assert score_d5(_sample(_out(None))).score == 5

    def test_p2字典样本(self):
        """传 dict 时若只用 getattr，取 ``outcome`` 恒得 ``None``——
        全部判「无决策可判」，而**不可判定看起来像一条正常结果**。"""
        v = score_d5(_dict_sample(_out(None)))
        assert v.score == 5, "P2 字典形态没被判出来——取值路径漏了 Mapping"

    def test_两形态结论一致(self):
        o = _out("no_play_control", question="site.find_play_control", reached=False)
        assert score_d5(_sample(o)).score == score_d5(_dict_sample(o)).score

    def test_搜索单元无outcome判不可判定(self):
        """搜索阶段本来就没有 outcome——那是「没有决策可判」，
        不是「决策错了」。"""
        v = score_d5(_sample(None, unit="search"))
        assert v.score is None and v.kind == "undetermined"

    def test_缺question判不可判定(self):
        """老批次的 P2 没有 question。拿空字段比对得到的结论是
        **采集器缺字段**，不是「分支走错」——判 3 分会把它算进
        「轨迹质量」。"""
        v = score_d5(_sample(_out("no_play_control", question="")))
        assert v.score is None and v.kind == "undetermined"
        assert "question" in " ".join(v.reasons)


# ── 4. 跳步与打分 ─────────────────────────────────────────────────


class Test跳步:
    def test_判成功但没到播放页判1分(self):
        """rubric 锚点 1 的反例：「首页见『立即观看』字样直接记录，
        未进播放页」。"""
        v = score_d5(_sample(_out(None, question="player.ok", reached=False)))
        assert v.score == 1
        assert v.is_veto is True, "1 分触发 G-1 样本级一票否决"

    def test_三步全对判5分(self):
        v = score_d5(
            _sample(_out("component_unverified", question="player.ok", reached=True))
        )
        assert v.score == 5 and v.is_veto is False

    def test_单处分支走错判3分(self):
        """``login_wall_or_blocked`` 只能由判断点 ② 产出。"""
        v = score_d5(
            _sample(_out("login_wall_or_blocked", question="player.ok", reached=True))
        )
        assert v.score == 3
        assert v.is_veto is False, "3 分不触发一票否决（G-1 只认 1 分）"

    def test_不一致的why写清依据(self):
        """读报告的人只有看到理由才知道该改哪一处。"""
        v = score_d5(
            _sample(_out("login_wall_or_blocked", question="player.ok", reached=True))
        )
        assert any("决策树" in c.why for c in v.wrong_nodes)


# ── 5. 不抛异常 ───────────────────────────────────────────────────


class Test不抛异常:
    @pytest.mark.parametrize(
        "branch",
        [
            None,
            "unresolved",
            "player_unverified",
            "no_play_control",
            "component_unverified",
            "trailer_suspect",
            "unreachable_hard",
        ],
    )
    @pytest.mark.parametrize(
        "question", ["", "player.ok", "site.find_play_control", "不存在的题"]
    )
    def test_任意组合不抛(self, branch, question):
        """按节点名反查却找不到时 ``next()`` 会抛 ``StopIteration``，
        而那异常冒到调用方时症状是「本条样本评不出来」——
        看起来像数据问题，不像代码问题（实测踩过一次）。"""
        v = score_d5(
            _sample(_out(branch, question=question, reached=(question == "player.ok")))
        )
        assert v.score is None or 1 <= v.score <= 5

    def test_未知判断点判skipped而不是wrong(self):
        v = score_d5(_sample(_out("no_play_control", question="未来新增的题")))
        assert any(c.kind == "skipped" for c in v.checks)


# ── 6. 证据核（检查 4） ───────────────────────────────────────────


class Test证据核:
    """前三条检查的输入（branch / question / reached）**出自同一处记账**，
    所以它们只能抓「接线断了」，抓不了「结论贴反了」。第四条检查读的
    是另一条通道（**页面正文**），这是它能判出低分的前提。

    这一节的重点不是「能判 2 分」，而是**它只在自己有把握的那一处开口**：
    找不到形态 → ``undetermined``（词表可能不全，不据此判错）；
    合并态方向不明 → ``undetermined``；反向没有裁定 → **不判**；
    理由文本（动作前动机）→ **不算证据**。
    """

    #: 搜狐实测正文（``T038__a78dc57e__site-7``）的节选：真的付费门禁。
    SOHU = ("由于浏览器限制，请手动开启声音 ⏎ 您可以试看5分钟，点击开通会员 "
            "⏎ 高清 ⏎ 倍速 ⏎ 客户端播放")

    def test_付费墙分支与地区阻断冲突判2分(self):
        """H-16 让位裁定：正文已是「这个设备/地区不给看」，再贴「要付费」
        就是把真因换成另一条支——最终结论错，锚点 2 分。"""
        v = score_d5(
            _sample(
                _out("subscription_wall"),
                body="当前设备不支持播放 ⏎ 重试 ⏎ 错误码： ⏎ 70017001.1",
            )
        )
        assert v.score == 2, f"实际 {v.score}：{v.reasons}"
        c = [c for c in v.wrong_nodes if c.node == d5_scorer._NODE_EVIDENCE][0]
        assert c.tier == d5_scorer.EVIDENCE_CONFLICT_TIER
        assert "70017001" in c.why or "地区" in c.why, (
            "判低分却没把正文原话抄出来，读报告的人无法复核"
        )

    def test_正文写着本地区不给看也判2分(self):
        v = score_d5(
            _sample(
                _out("subscription_wall"),
                body="很抱歉，您所在地区无法观看本视频",
            )
        )
        assert v.score == 2, f"实际 {v.score}：{v.reasons}"

    def test_付费墙分支与付费门禁一致判5分(self):
        """正文确有门禁语境短语（「试看」+「开通会员」同句）——
        这才是真的付费墙。"""
        v = score_d5(_sample(_out("subscription_wall"), body=self.SOHU))
        assert v.score == 5, f"实际 {v.score}：{v.reasons}"

    def test_没留正文判不可判定而不是错(self):
        """**没有正文 ≠ 判错。** 词表可能不全、采集可能缺件，
        判错比不出分贵。"""
        v = score_d5(_sample(_out("subscription_wall")))
        assert v.score is None and v.kind == "undetermined"
        assert any("没有留存页面正文" in r for r in v.reasons)

    def test_正文里没有付费形态判不可判定(self):
        v = score_d5(
            _sample(
                _out("subscription_wall"),
                body="这是一个视频网站首页，包含热门推荐列表",
            )
        )
        assert v.score is None and not v.wrong_nodes
        assert any("词表不全" in r for r in v.reasons)

    def test_理由文本不算证据(self):
        """**2026-10-23 实测踩过的判反。** INC46 口径里 ``rationales`` 是
        **动作前动机**（「我先打开链接核对…；是否存在地区限制还需确认」），
        不是对页面的观察。第一版把它也算证据，搜狐那两条正文明明是
        「您可以试看5分钟，点击开通会员」，却因为动机句里的「地区限制」
        被判了 2 分。判反比不出分贵，所以理由一路**整条不采信**。"""
        v = score_d5(
            _sample(
                _out("subscription_wall"),
                rationale="我先打开链接核对影片；无地区限制仍需根据页面信息进一步确认",
                body=self.SOHU,
            )
        )
        assert v.score == 5, f"动机句被当成证据了，实际 {v.score}：{v.reasons}"

    def test_只有理由写着错误码仍不出分(self):
        """理由里写着「页面出现 70017001」也不行：分不清那是**看到的**
        还是**打算去看的**。正文缺了就落不可判定。"""
        v = score_d5(
            _sample(
                _out("subscription_wall"),
                rationale="页面出现错误码70017001，当前地区不可播放",
            )
        )
        assert v.score is None and not v.wrong_nodes

    def test_拦截分支与登录形态一致判5分(self):
        v = score_d5(
            _sample(
                _out("login_required", question="site.is_reachable"),
                body="请先登录 ⏎ 登录后即可观看完整版",
            )
        )
        assert v.score == 5, f"实际 {v.score}：{v.reasons}"

    def test_拦截分支遇到付费门禁不判错(self):
        """反向（正文是付费门禁而分支贴了墙）**没有裁定过**优先级。
        判了就是评分器自己发明一条规则——表现是「一堆样本莫名其妙 2 分」。"""
        v = score_d5(
            _sample(
                _out("blocked", question="site.is_reachable"),
                body="页面要求购买会员后才能观看完整版",
            )
        )
        assert v.score is None and not v.wrong_nodes, (
            f"反向方向不许判错，实际 {v.score}：{v.wrong_nodes}"
        )

    def test_拦截分支无形态判不可判定而不是错(self):
        """实测踩过：kkday 的 Cloudflare「Sorry, you have been blocked」
        在词表里没有对应短语。**找不到形态不是判错的依据。**"""
        v = score_d5(
            _sample(
                _out("broken", question="site.is_reachable"),
                body="页面报错，提示 Sorry, you have been blocked",
            )
        )
        assert v.score is None and not v.wrong_nodes
        assert any("词表不全" in r for r in v.reasons)

    def test_合并态遇到付费门禁判不可判定(self):
        """``login_wall_or_blocked`` 已弃用：按定义它**不区分**登录墙与
        阻断，在付费门禁输入上判不出方向。实测 3 条优酷样本（「浏览器
        版本太低，下载优酷PC客户端继续观看」）会被 ``paywall_evidence``
        的 L1 门禁短语打中——那不是付费墙，是客户端限制。"""
        v = score_d5(
            _sample(
                _out("login_wall_or_blocked", question="site.is_reachable"),
                body="浏览器版本太低，下载优酷PC客户端继续观看",
            )
        )
        assert v.score is None and not v.wrong_nodes, (
            f"合并态永不判错，实际 {v.score}：{v.wrong_nodes}"
        )

    def test_合并态遇到阻断形态判5分(self):
        v = score_d5(
            _sample(
                _out("login_wall_or_blocked", question="site.is_reachable"),
                body="很抱歉，该视频在当前地区不支持播放",
            )
        )
        assert v.score == 5, f"实际 {v.score}：{v.reasons}"

    def test_不声称形态的分支不归本条管(self):
        """``unresolved`` 不声称任何结论；``unreachable_hard`` /
        ``trailer_*`` 由代码判定；组件类由 D-4 核。它们一律 ``skipped``，
        **不许**因为「正文里没写拦截形态」而落成不可判定——
        那会把正常的 fail-closed 记账变成不出分。"""
        for br, q in (
            ("unresolved", "site.is_reachable"),
            ("unreachable_hard", "site.is_reachable"),
            ("no_play_control", "site.find_play_control"),
            ("trailer_only", "site.find_play_control"),
            ("component_unverified", "site.find_play_control"),
        ):
            v = score_d5(_sample(_out(br, question=q, reached=True)))
            c = [c for c in v.checks if c.node == d5_scorer._NODE_EVIDENCE][0]
            assert c.kind == "skipped", f"{br} 不该被证据核管辖，实际 {c.kind}"

    def test_tier随序列化落盘(self):
        c = score_d5(
            _sample(_out("subscription_wall"), body="页面提示 70017001.1，地区限制")
        ).to_json()
        ev = [x for x in c["checks"] if x["node"] == d5_scorer._NODE_EVIDENCE][0]
        assert ev["tier"] == d5_scorer.EVIDENCE_CONFLICT_TIER
        assert ev["kind"] == "wrong"


# ── 7. 真实存档回归 ───────────────────────────────────────────────


def _real_samples() -> list[tuple[str, dict]]:
    root = Path(__file__).resolve().parents[2] / "output" / "pipeline" / "samples"
    files = sorted(root.glob("*.json"))
    if not files:
        pytest.skip("没有 P2 样本（先跑 cli build）")
    return [(p.stem, json.loads(p.read_text(encoding="utf-8"))) for p in files]


class TestOnRealSamples:
    def test_低分只许由证据核判出(self):
        """**这一条是本模块的价值证明**：真样本跑下来，低分必须是
        「真有问题」，不能是评分器自己把表写错了。

        开发过程中三版映射表分别误判了 54 / 61 / 3 条，而报表上
        只表现为「分数低了」——看不出是评分器的错。所以这里把
        **责任检查点名**：前三条（同源自洽的那三条）一条都不许判低分，
        低分只能来自第四条「结论的证据核」，且必须是 ``final`` 档
        （错的是最终结论本身，不是过程瑕疵）。
        """
        bad: list[tuple[str, int, str, str]] = []
        for name, doc in _real_samples():
            v = score_d5(doc)
            if v.score is None or v.score >= 5:
                continue
            for c in v.wrong_nodes:
                if c.node != d5_scorer._NODE_EVIDENCE:
                    bad.append((name, v.score, c.node, c.why))
                elif c.tier != d5_scorer.EVIDENCE_CONFLICT_TIER:
                    bad.append((name, v.score, f"{c.node}(tier={c.tier!r})", c.why))
        assert not bad, (
            f"{len(bad)} 处低分不来自「结论的证据核 · final」档，"
            f"前三条检查或非最终档不该判轨迹的分：{bad[:5]}"
        )

    def test_低分的理由必须引用到依据原话(self):
        """判低分而不把原话抄出来，读报告的人只能回去自己翻存档——
        那这条低分与「评分器写错了表」在报告上无法区分。"""
        for name, doc in _real_samples():
            v = score_d5(doc)
            if v.score is None or v.score >= 5:
                continue
            why = " ".join(c.why for c in v.wrong_nodes)
            assert "H-16" in why, f"{name} 判了 {v.score} 分却没引裁定：{why}"
            assert "「" in why, f"{name} 判了 {v.score} 分却没引依据原话：{why}"

    def test_证据核在真实数据上确实在跑(self):
        """**防恒真的机械线**：第四条检查在真实数据上不许全是 ``skipped``
        ——全 skip 说明它管辖的分支一条都没出现，这条检查等于没写。
        而新增一条检查最常见的失败方式，就是它安静地什么都没管。"""
        live = 0
        for _, doc in _real_samples():
            v = score_d5(doc)
            if any(
                c.node == d5_scorer._NODE_EVIDENCE and c.kind != "skipped"
                for c in v.checks
            ):
                live += 1
        assert live > 0, (
            "所有真实样本上证据核都是 skipped——检查没接上"
            "（分支名拼错 / 管辖集合为空）"
        )

    def test_真实样本上不可判定数与预期一致(self):
        """不可判定只有两个来源，且**各自的前置事实要对**：

        ① 老批次 / 搜索单元没有 outcome —— 「没有决策可判」；
        ② 证据核核不了 —— 「决策有了，依据核不了」。

        混成一个数看不出来是哪种：前者是采集器缺字段，后者是词表
        或口径不够，该改的地方完全不同。"""
        vague = 0
        for name, doc in _real_samples():
            v = score_d5(doc)
            if v.kind != "undetermined":
                continue
            if not v.checks:
                # 老批次两条形态：无 outcome，或 outcome 缺 ``question``
                # （2026-10-10 之前切的样本）。两者都该是「没有决策可判」，
                # 且理由里必须说清缺的是哪一样。
                assert any(
                    "outcome" in r or "question" in r for r in v.reasons
                ), f"{name} 不可判定，理由却没说清缺什么：{v.reasons}"
                continue
            assert any(
                c.node == d5_scorer._NODE_EVIDENCE and c.kind == "undetermined"
                for c in v.checks
            ), f"{name} 有 outcome 却因别的原因不可判定：{[c.node for c in v.checks]}"
            vague += 1
        assert vague < len(_real_samples()), "不应**全部**不可判定"


# ── 7. 序列化 ─────────────────────────────────────────────────────


class Test序列化:
    def test_可JSON序列化(self):
        v = score_d5(_sample(_out(None)))
        json.dumps(v.to_json(), ensure_ascii=False)

    def test_含维度名与一票否决标记(self):
        js = score_d5(_sample(_out(None, reached=False))).to_json()
        assert js["dimension"] == "D-5"
        assert js["veto"] is True
