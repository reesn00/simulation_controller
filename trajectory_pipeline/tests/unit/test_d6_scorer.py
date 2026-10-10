"""D-6「记录字段完整性」评分器的单元测试。

四个必填字段各有一件「判错了不报错、只是分数低了」的事，
而 D-6 开发时在真实数据上真犯过两次：

1. **「未到达播放页」时播放页 URL 必然为空，此时不扣分**
   （rubric 边界规则）。不写清楚，评分器会系统性惩罚
   ``no_play_control`` / ``login_wall_or_blocked`` 那些**根本走不到播放页**
   的站点——而它们恰恰是**正确**的负样本。
2. **缺字段 ≠ 字段为空**（避坑指南 §2.3）。第一版靠 ``_has_key`` 判，
   但那层信息在 ``OutcomeRef.to_json()`` 里已经丢了（都渲染成 ``""``），
   实测判出 **20 条假 3 分**，而那 20 条的结论本身是对的。
   → 现在靠 :attr:`OutcomeRef.p1_missing_fields` 逐条记。
3. **搜索单元没有 outcome，不该按「缺 ≥2 必填」判 2 分**——
   它是「没有记录可核」，不是「记录不合格」。
4. **验证证据在 P1，P2 不复制**——判不了就明说判不了，
   **不拿 evidence 长度当代理**。

外加：片名「查不到」时判 ``undetermined`` 而不是 5 分——
拿「查不到」当「对」是 I4 fail-closed 在评分侧的对应物。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.assembler.schema import (
    GateMark, OutcomeRef, Sample,
)
from trajectory_pipeline.evaluation.scorers import d6_scorer
from trajectory_pipeline.evaluation.scorers.d6_scorer import score_d6


def _obs(body="页面正文 功夫 在线观看", title="功夫 在线观看", **kw):
    base = {"url": "https://x.test", "page_title": title, "body_text": body}
    base.update(kw)
    return base


def _sample(outcome=None, observations=(), title="功夫", unit="site-1"):
    return Sample(
        sample_id=f"T001__a1__{unit}", task_id="T001", unit=unit, title=title,
        system_prompt="s", tools=(), user_prompt="u", provenance={},
        actions=(), observations=tuple(observations), rationale=None,
        gate=GateMark("not_run"),
        outcome=outcome if outcome is not None else OutcomeRef(
            url="https://x.test", branch=None,
            site_url="https://x.test", play_page_url="https://x.test/play",
            question="player.ok", reached_play_page=True),
    )


def _dict_sample(outcome: dict | None, observations=(), title="功夫"):
    return {"sample_id": "T001__a1__site-1", "task_id": "T001", "unit": "site-1",
            "title": title,
            "six": {"observations": list(observations)},
            "outcome": outcome}


def _outcome_dict(**kw):
    base = {"url": "https://x.test", "branch": None, "source": "rule",
            "fallback_used": False, "succeeded": True, "question": "player.ok",
            "reached_play_page": True, "site_url": "https://x.test",
            "play_page_url": "https://x.test/play", "p1_missing_fields": []}
    base.update(kw)
    return base


# ── 1. 播放页 URL 的「不适用」边界 ─────────────────────────────────


class Test播放页URL边界:
    def test_未到播放页判不适用(self):
        """rubric 边界规则原文。不写清楚，评分器会系统性惩罚
        **根本走不到播放页**的正确负样本。"""
        s = _sample(outcome=OutcomeRef(
            url="https://x.test", branch="no_play_control",
            question="site.find_play_control", reached_play_page=False,
            site_url="https://x.test"))
        f = next(x for x in score_d6(s).fields if x.field == "play_page_url")
        assert f.kind == "not_applicable"

    def test_未到播放页的空url不扣分(self):
        """负样本池里绝大多数是这类。它们不该因为「播放页 URL 为空」扣分。

        带上含片名的观察——本条要验的是**播放页 URL** 那项，
        不该被 title 的不可核（无观察）干扰掉。"""
        s = _sample(
            outcome=OutcomeRef(
                url="https://x.test", branch="no_play_control",
                question="site.find_play_control", reached_play_page=False,
                site_url="https://x.test"),
            observations=[_obs(body="本页没有播放控件")],
        )
        assert score_d6(s).score == 5, "未到播放页的负样本被扣分了"

    def test_到播放页且键在却为空判缺失(self):
        """真缺失。rubric 反例：「只有一条 URL 承担两种语义」。"""
        s = _sample(outcome=OutcomeRef(
            url="https://x.test", branch="component_unverified",
            question="player.ok", reached_play_page=True,
            site_url="https://x.test", play_page_url=""))
        f = next(x for x in score_d6(s).fields if x.field == "play_page_url")
        assert f.kind == "missing"


# ── 2. 缺字段 ≠ 字段为空 ──────────────────────────────────────────


class Test缺字段不等于空值:
    def test_老存档缺键判不可判定而不是缺失(self):
        """实测 20 条 ``reached_play_page=True`` 的老 outcome 全部
        ``run_config=False``——字段压根不存在。第一版判「缺失」，
        于是 20 条**正确**的结论被判成 3 分。"""
        s = _sample(outcome=OutcomeRef(
            url="https://x.test", branch="unresolved", question="player.ok",
            reached_play_page=True, site_url="", play_page_url="",
            p1_missing_fields=("site_url", "play_page_url")))
        v = score_d6(s)
        assert v.score is None and v.kind == "undetermined"
        assert "3" not in str(v.score), "老存档被判了低分"

    def test_缺件信息经P2往返不丢(self):
        """``to_json`` 里缺字段与空值同形，所以要单独记
        :attr:`OutcomeRef.p1_missing_fields`——靠 ``_has_key`` 判会失效。"""
        o = OutcomeRef(url="u", branch="unresolved", question="player.ok",
                       reached_play_page=True, site_url="", play_page_url="",
                       p1_missing_fields=("site_url", "play_page_url"))
        js = o.to_json()
        assert js["p1_missing_fields"] == ["site_url", "play_page_url"]
        # 键本身**不输出**——老存档的形状不被伪造成「有键但为空」
        assert "site_url" not in js
        assert "play_page_url" not in js

    def test_新存档正常输出两个键(self):
        js = OutcomeRef(url="u", branch=None, question="player.ok",
                        reached_play_page=True, site_url="https://s",
                        play_page_url="https://p").to_json()
        assert js["site_url"] == "https://s"
        assert js["play_page_url"] == "https://p"
        assert js["p1_missing_fields"] == []

    def test_键在但为空且有outcome_url判值不符(self):
        """不是「缺字段」而是「该有却没有」——新存档才有这个问题。"""
        s = _sample(outcome=OutcomeRef(
            url="https://x.test", branch="component_unverified",
            question="player.ok", reached_play_page=True,
            site_url="", play_page_url="https://p"))
        f = next(x for x in score_d6(s).fields if x.field == "site_url")
        assert f.kind == "wrong"


# ── 3. 搜索单元 ───────────────────────────────────────────────────


class Test搜索单元:
    def test_无outcome判不可判定(self):
        """它没有「记录」可核。判 2 分是把「不适用」当「不合格」。"""
        s = _sample(outcome=OutcomeRef(url="u", branch=None), unit="search")
        v = score_d6(s)
        assert v.score is None and v.kind == "undetermined"
        assert not v.missing_fields

    def test_p2字典形态同样判不可判定(self):
        v = score_d6(_dict_sample(None))
        assert v.score is None and v.kind == "undetermined"


# ── 4. 验证证据 ───────────────────────────────────────────────────


class Test验证证据:
    def test_判不了就明说判不了(self):
        """P2 刻意不复制 evidence（复核后 P1 更新而样本没更新的静默失配）。
        判不了要写清理由，**不拿长度当代理**。"""
        s = _sample(observations=[_obs()])
        f = next(x for x in score_d6(s).fields if x.field == "evidence")
        assert f.kind == "skipped"
        assert "P1" in f.why

    def test_不判证据缺失(self):
        """它不在 P2 里，不构成「缺必填」。"""
        s = _sample(observations=[_obs()])
        assert "evidence" not in score_d6(s).missing_fields


# ── 5. 片名 ───────────────────────────────────────────────────────


class Test片名:
    def test_片名在观察里判ok(self):
        s = _sample(observations=[_obs(body="功夫 完整版 正在播放")])
        f = next(x for x in score_d6(s).fields if x.field == "title")
        assert f.kind == "ok"

    def test_片名查不到判不可判定而非ok(self):
        """拿「查不到」当「对」是 fail-closed 在评分侧的对应物。
        真实形态：站点页正文 100 字符、全是导航文案，压根没提片名。

        ⚠️ 观察的 ``page_title`` **也要不含片名**——第一版 fixture
        保留了默认的「功夫 在线观看」，于是「查不到」这个场景
        压根没构造出来，测试却以为构造好了（§7.2「测试写错 ≠ 守卫有问题」）。"""
        s = _sample(observations=[_obs(body="电视剧 电影 综艺 动漫",
                                        title="腾讯视频")])
        f = next(x for x in score_d6(s).fields if x.field == "title")
        assert f.kind == "undetermined"

    def test_无title判缺失(self):
        s = _sample(title="", observations=[_obs()])
        f = next(x for x in score_d6(s).fields if x.field == "title")
        assert f.kind == "missing"

    def test_观察无正文时一致性不可核(self):
        """正文全空 → 无从核查，不判「不符」。``page_title`` 同样不含片名。"""
        s = _sample(observations=[_obs(body="", title="腾讯视频")])
        f = next(x for x in score_d6(s).fields if x.field == "title")
        assert f.kind == "undetermined"

    def test_无观察时一致性不可核(self):
        s = _sample(observations=[])
        f = next(x for x in score_d6(s).fields if x.field == "title")
        assert f.kind == "undetermined"

    def test_片名包装字符被剥(self):
        s = _sample(title="《功夫》", observations=[_obs(body="功夫 在线观看")])
        f = next(x for x in score_d6(s).fields if x.field == "title")
        assert f.kind == "ok"


# ── 6. 输入形态与序列化 ───────────────────────────────────────────


class Test输入形态:
    def test_dataclass与字典结论一致(self):
        o = _outcome_dict()
        a = score_d6(_sample(outcome=OutcomeRef(**{k: v for k, v in o.items()
                                                  if k != "p1_missing_fields"}),
                             observations=[_obs()]))
        b = score_d6(_dict_sample(o, observations=[_obs()]))
        assert a.score == b.score and a.kind == b.kind

    def test_可JSON序列化(self):
        s = _sample(observations=[_obs()])
        json.dumps(score_d6(s).to_json(), ensure_ascii=False)

    def test_含维度名(self):
        assert score_d6(_sample()).to_json()["dimension"] == "D-6"


# ── 7. 真实存档回归 ───────────────────────────────────────────────


class TestOnRealSamples:
    def test_真实样本上不得有低分误判(self):
        """开发中这里真出过 **20 条假 3 分**——症状只是「分数低了」，
        看不出是评分器把「老存档缺字段」当成了「记录字段不全」。"""
        root = Path(__file__).resolve().parents[2] / "output" / "pipeline" / "samples"
        files = sorted(root.glob("*.json"))
        if not files:
            pytest.skip("没有 P2 样本（先跑 cli build）")
        low = [(p.stem, score_d6(json.loads(p.read_text(encoding='utf-8'))).score)
               for p in files]
        bad = [(n, s) for n, s in low if s is not None and s < 5]
        assert not bad, f"{len(bad)} 条真实样本判了低分：{bad[:5]}"

    def test_老存档应判不可判定而非低分(self):
        """现存 39 份存档全部跑在 rubric v1.1 字段变更之前，
        所以 D-6 判不可判定是**正确输出**，不是缺陷。"""
        root = Path(__file__).resolve().parents[2] / "output" / "pipeline" / "samples"
        files = sorted(root.glob("*.json"))
        if not files:
            pytest.skip("没有 P2 样本（先跑 cli build）")
        kinds = {score_d6(json.loads(p.read_text(encoding='utf-8'))).kind
                 for p in files}
        assert kinds <= {"undetermined", "scored"}

    def test_老存档不得被判成合规(self):
        """**变异测试逼出来的一条**：删掉「键缺失判不可判定」那条守卫，
        本文件原本全绿，而真实数据从 151 不可判定 / 45 五分变成
        133 / 63——18 条老存档被误判成「齐全正确」。

        断言必须**回到 P1 核真值**，不能只盯分数分布：
        分数分布随批次变，且「5 分」的样本压根不会带
        「老存档没这个键」的理由（那条理由正是被变异删掉的分支产生的）——
        拿它当断言条件在结构上就不可能成立。

        口径：凡判 5 分的样本，其 ``p1_missing_fields`` **必须为空**。
        有缺件却判「齐全正确」= 把采集器缺字段说成了记录完整。
        """
        root = Path(__file__).resolve().parents[2] / "output" / "pipeline" / "samples"
        files = sorted(root.glob("*.json"))
        if not files:
            pytest.skip("没有 P2 样本（先跑 cli build）")
        bad = []
        for p in files:
            sample = json.loads(p.read_text(encoding="utf-8"))
            v = score_d6(sample)
            if v.score != 5:
                continue
            missing = tuple((sample.get("outcome") or {})
                            .get("p1_missing_fields") or ())
            if missing:
                bad.append((p.stem, missing))
        assert not bad, (
            f"{len(bad)} 条样本带 p1_missing_fields 却被判 5 分——"
            f"「字段齐」与「键压根不存在」是两回事（避坑指南 §2.3）：{bad[:3]}")
