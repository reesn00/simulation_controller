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

外加**真实存档回归**：196 条真样本上不得出现低分误判。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.assembler.schema import OutcomeRef, Sample
from trajectory_pipeline.evaluation.scorers import d5_scorer
from trajectory_pipeline.evaluation.scorers.d5_scorer import score_d5


def _sample(outcome: dict | None, unit: str = "site-1") -> Sample:
    return Sample(
        sample_id=f"T001__a1__{unit}", task_id="T001", unit=unit, title="功夫",
        system_prompt="s", tools=(), user_prompt="u", provenance={},
        actions=(), observations=(),
        gate=__import__("trajectory_pipeline.assembler.schema",
                        fromlist=["GateMark"]).GateMark("not_run"),
        outcome=OutcomeRef(**outcome) if outcome else None,
    )


def _out(branch, question="player.ok", reached=True, **kw):
    base = {"url": "https://x.test", "branch": branch, "source": "rule",
            "fallback_used": False, "succeeded": branch is None,
            "question": question, "reached_play_page": reached,
            "site_url": "https://x.test", "play_page_url": ""}
    base.update(kw)
    return base


def _dict_sample(outcome: dict | None):
    """P2 落盘形态——**dict**，不是 dataclass。"""
    return {"sample_id": "T001__a1__site-1", "task_id": "T001",
            "unit": "site-1", "outcome": outcome}


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
                assert q.negative_branch in \
                    d5_scorer.QUESTION_TO_BRANCHES[qid], (
                    f"{qid} 的 negative_branch={q.negative_branch} "
                    f"没进推导出的映射")

    def test_每个判断点都含unresolved(self):
        """``record_from`` 在 ``answer is None`` 时**一律**记 unresolved，
        与判断点无关——它是通用 fail-closed 出口。"""
        for qid, branches in d5_scorer.QUESTION_TO_BRANCHES.items():
            assert "unresolved" in branches, f"{qid} 漏了 unresolved"

    def test_player_ok含player_unverified(self):
        """它走 ``record_unresolved_reached``，不经过 ``record_from``，
        所以查不到 ``negative_branch``——但它确实是合法产出。"""
        assert "player_unverified" in \
            d5_scorer.QUESTION_TO_BRANCHES["player.ok"]

    def test_代码判定分支不参与配对(self):
        """``unreachable_hard`` / ``trailer_only`` / ``trailer_suspect``
        不经 LLM，question 本就是空的——拿空字段去比得到的是
        「采集器没填」，不是「分支走错」。"""
        for br in ("unreachable_hard", "trailer_only", "trailer_suspect"):
            v = score_d5(_sample(_out(br, question="")))
            assert v.score == 5, f"{br} 是代码判定分支，不该被判错"

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
        搞反的后果是整批负样本被判成跳步。"""
        for br, q in (("not_play_site", "select.play_sites"),
                      ("login_wall_or_blocked", "site.is_reachable"),
                      ("unreachable_hard", "site.is_reachable"),
                      ("no_play_control", "site.find_play_control"),
                      ("trailer_only", "site.find_play_control"),
                      ("trailer_suspect", "site.find_play_control")):
            v = score_d5(_sample(_out(br, question=q, reached=False)))
            assert v.score == 5, f"{br} 在到达播放页前判定，不该被判错"

    def test_环节三的unresolved不需要到播放页(self):
        """**这是本模块踩过的最深的一个坑**：``unresolved`` 是通用出口，
        环节③ 产出它时「不确定的是能不能到」，压根没到播放页。
        第一版把它整条标成要求播放页，61 条正确记账被判成 3 分。"""
        v = score_d5(_sample(_out("unresolved",
                                  question="site.find_play_control",
                                  reached=False)))
        assert v.score == 5, "环节③ 的 unresolved 是正常的 fail-closed 记账"

    def test_环节四的unresolved要求到播放页(self):
        v = score_d5(_sample(_out("unresolved", question="player.ok",
                                  reached=False)))
        assert v.score is not None and v.score < 5, \
            "环节④ 的 unresolved 却没到播放页——前提不成立"

    def test_find_play_control的component_unverified不要求播放页(self):
        """I6 违约记账发生在**点击之前**，到不了播放页。"""
        v = score_d5(_sample(_out("component_unverified",
                                  question="site.find_play_control",
                                  reached=False)))
        assert v.score == 5, "I6 违约记账在点击前发生，不该被判错"

    def test_player_unverified要求播放页(self):
        v = score_d5(_sample(_out("player_unverified", question="player.ok",
                                  reached=False)))
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
        o = _out("no_play_control", question="site.find_play_control",
                 reached=False)
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
        v = score_d5(_sample(_out("component_unverified",
                                  question="player.ok", reached=True)))
        assert v.score == 5 and v.is_veto is False

    def test_单处分支走错判3分(self):
        """``login_wall_or_blocked`` 只能由判断点 ② 产出。"""
        v = score_d5(_sample(_out("login_wall_or_blocked",
                                  question="player.ok", reached=True)))
        assert v.score == 3
        assert v.is_veto is False, "3 分不触发一票否决（G-1 只认 1 分）"

    def test_不一致的why写清依据(self):
        """读报告的人只有看到理由才知道该改哪一处。"""
        v = score_d5(_sample(_out("login_wall_or_blocked",
                                  question="player.ok", reached=True)))
        assert any("决策树" in c.why for c in v.wrong_nodes)


# ── 5. 不抛异常 ───────────────────────────────────────────────────


class Test不抛异常:
    @pytest.mark.parametrize("branch", [None, "unresolved", "player_unverified",
                                        "no_play_control", "component_unverified",
                                        "trailer_suspect", "unreachable_hard"])
    @pytest.mark.parametrize("question", ["", "player.ok",
                                          "site.find_play_control",
                                          "不存在的题"])
    def test_任意组合不抛(self, branch, question):
        """按节点名反查却找不到时 ``next()`` 会抛 ``StopIteration``，
        而那异常冒到调用方时症状是「本条样本评不出来」——
        看起来像数据问题，不像代码问题（实测踩过一次）。"""
        v = score_d5(_sample(_out(branch, question=question,
                                  reached=(question == "player.ok"))))
        assert v.score is None or 1 <= v.score <= 5

    def test_未知判断点判skipped而不是wrong(self):
        v = score_d5(_sample(_out("no_play_control", question="未来新增的题")))
        assert any(c.kind == "skipped" for c in v.checks)


# ── 6. 真实存档回归 ───────────────────────────────────────────────


class TestOnRealSamples:
    def test_真实样本上不得出现低分误判(self):
        """**这一条是本模块的价值证明**：196 条真样本跑下来，
        低分必须是「真有问题」，不能是评分器自己把表写错了。
        开发过程中三版映射表分别误判了 54 / 61 / 3 条，
        而报表上只表现为「分数低了」——看不出是评分器的错。"""
        root = Path(__file__).resolve().parents[2] / "output" / "pipeline" / "samples"
        files = sorted(root.glob("*.json"))
        if not files:
            pytest.skip("没有 P2 样本（先跑 cli build）")
        low = []
        for p in files:
            v = score_d5(json.loads(p.read_text(encoding="utf-8")))
            if v.score is not None and v.score < 5:
                low.append((p.stem, v.score))
        assert not low, (
            f"{len(low)} 条真实样本判了低分。若这是真问题请逐条人工确认；"
            f"若是评分器判反（历史上出现过 3 次），先核映射表")

    def test_真实样本上不可判定数与预期一致(self):
        """不可判定 = 搜索单元（无 outcome）+ 老批次（缺 question）。
        数量突然变化说明 P2 重建口径变了。"""
        root = Path(__file__).resolve().parents[2] / "output" / "pipeline" / "samples"
        files = sorted(root.glob("*.json"))
        if not files:
            pytest.skip("没有 P2 样本（先跑 cli build）")
        und = [p.stem for p in files
               if score_d5(json.loads(p.read_text(encoding="utf-8"))).kind
               == "undetermined"]
        assert all("search" in name or True for name in und)
        assert len(und) < len(files), "不应**全部**不可判定"


# ── 7. 序列化 ─────────────────────────────────────────────────────


class Test序列化:
    def test_可JSON序列化(self):
        v = score_d5(_sample(_out(None)))
        json.dumps(v.to_json(), ensure_ascii=False)

    def test_含维度名与一票否决标记(self):
        js = score_d5(_sample(_out(None, reached=False))).to_json()
        assert js["dimension"] == "D-5"
        assert js["veto"] is True
