"""D-3「播放站识别正确性」LLM judge 评分器的单元测试。

8 条 contract 测试（plan §三·第二步）+ 2 条边界测试。覆盖：

  1. rubric §D-3 五档映射（5/4/3/2/1）
  2. 三态纪律（``score=None`` ≠ 误判）
  3. ``llm_client=None`` 不抛
  4. category 不在契约 5 类 → 2 档
  5. playable subtype 不在契约 4 类 → 2 档
  6. evidence 「concrete」启发式（5/4 二选一）
  7. raw CoT 红线（``strip_think`` 在 evidence 拼装前完成）
  8. rubric v1.2 契约冻结（frozenset 比对）
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.assembler.schema import GateMark, OutcomeRef, Sample
from trajectory_pipeline.evaluation.scorers import d3_scorer
from trajectory_pipeline.evaluation.scorers.d3_scorer import (
    CONFIDENCE_JUDGED,
    D3_CATEGORIES,
    D3_SUBTYPES,
    D3Verdict,
    score_d3,
)


# ── helpers ────────────────────────────────────────────────────────


def _obs(body="这里是导航首页,包含各类链接,点击进入。",
         elements=3, video=0, iframe=0, url="https://hao123.test/",
         title="hao123", **kw):
    base = {
        "url": url, "page_title": title, "body_text": body,
        "video_tag_count": video, "iframe_count": iframe,
        "interactive_elements": [{"tag": "a", "label": f"链接{i}"}
                                 for i in range(elements)],
        "links": [], "degraded": [], "truncated": False,
    }
    base.update(kw)
    return base


def _out(branch, source="llm", question="select.play_sites", reached=True):
    return OutcomeRef(
        url="https://hao123.test/", branch=branch, source=source,
        reached_play_page=reached,
        site_url="https://hao123.test/",
        play_page_url="https://hao123.test/play",
    )


def _sample(outcome, observations=()):
    return Sample(
        sample_id="T001__a1__site-1", task_id="T001", unit="site-1",
        title="功夫", system_prompt="s", tools=(), user_prompt="u",
        provenance={}, actions=(), observations=tuple(observations),
        gate=GateMark("not_run"), outcome=outcome,
    )


class _StubLLM:
    """最小 LLM stub —— 不依赖 MagicMock，便于读源码时一眼看清判据。"""

    def __init__(self, *, content=None, return_none=False):
        self._content = content
        self._return_none = return_none
        self.last_error = ""

    def chat(self, system, user, *, max_tokens=None):
        if self._return_none:
            return None
        return self._content


# ── 1. 五档映射 ───────────────────────────────────────────────────


class Test五档映射:
    def test_5分_依据引用观察(self):
        """满档：category 判对 + evidence 引用观察里的具体短语。"""
        obs = _obs(body="页面是导航聚合,每条都是外部链接",
                   elements=3)
        stub = _StubLLM(content=(
            '{"category": "nav_aggregator", "playable_subtype": null,'
            ' "evidence": "页面是导航聚合,每条都是外部链接",'
            ' "confidence": 0.9}'
        ))
        v = score_d3(_sample(_out(branch="not_play_site"), observations=[obs]),
                     llm_client=stub)
        assert v.score == 5
        assert v.kind == "scored"
        assert v.category == "nav_aggregator"
        assert v.is_veto is False
        assert v.confidence == CONFIDENCE_JUDGED
        assert "锚点 5" in v.reasons[0]

    def test_4分_泛泛证据(self):
        """4 档：category 判对但 evidence 没引用观察具体短语。"""
        obs = _obs(body="这里是导航首页", elements=3)
        stub = _StubLLM(content=(
            '{"category": "nav_aggregator", "playable_subtype": null,'
            ' "evidence": "不是播放站",'
            ' "confidence": 0.7}'
        ))
        v = score_d3(_sample(_out(branch="not_play_site"), observations=[obs]),
                     llm_client=stub)
        assert v.score == 4
        assert "锚点 4" in v.reasons[0]

    def test_3分_ambiguous_边界(self):
        """3 档：ambiguous 站处置正确（归 ambiguous 而非判是/否）。"""
        obs = _obs(body="既有导航链接又有内嵌视频", elements=3, video=1)
        stub = _StubLLM(content=(
            '{"category": "ambiguous", "playable_subtype": null,'
            ' "evidence": "无法判断,既有导航也有视频",'
            ' "confidence": 0.6}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score == 3
        assert v.category == "ambiguous"
        assert "锚点 3" in v.reasons[0]

    def test_2分_类别判错(self):
        """2 档：模型写出契约外的字符串（如 video_hosting）→ 判错。"""
        obs = _obs(body="页面是某导航", elements=3)
        stub = _StubLLM(content=(
            '{"category": "video_hosting", "playable_subtype": null,'
            ' "evidence": "看着像视频站",'
            ' "confidence": 0.8}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score == 2
        assert v.category == "video_hosting"
        assert "锚点 2" in v.reasons[0]

    def test_2分_playable_subtype_飘出契约(self):
        """2 档：playable 但 subtype 不在 4 类内。"""
        obs = _obs(body="这里是某视频站,可以直接看正片", video=1)
        stub = _StubLLM(content=(
            '{"category": "playable", "playable_subtype": "pay_per_view",'
            ' "evidence": "单片付费,正文有 vip 字样",'
            ' "confidence": 0.7}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score == 2
        assert v.category == "playable"
        assert v.playable_subtype == "pay_per_view"

    def test_1分_未判且可判(self):
        """1 档 + is_veto：观察充分 + LLM 调不通 → fail-closed。"""
        obs = _obs(body="这里是某导航", elements=3)
        stub = _StubLLM(content=None, return_none=True)
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score == 1
        assert v.is_veto is True
        assert "锚点 1" in v.reasons[0]

    def test_解析失败仍判1分(self):
        """解析失败（不是 JSON）仍归 1 档 —— 不静默吞。"""
        obs = _obs(body="这里是某导航", elements=3)
        stub = _StubLLM(content="完全不是 JSON,就是一段散文。")
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score == 1
        assert v.is_veto is True


# ── 2. 三态纪律 ───────────────────────────────────────────────────


class Test三态纪律:
    def test_观察全空_判_undetermined_不走_1档(self):
        """观察三项全空 → quarantine（不走 1 档）。"""
        # body_text="" + 元素空 + truncated=False + degraded=[]
        # 但 truncated=False / degraded=[] / elements_total=0 →
        # _observation_shortfall 走最后一条「正文与元素皆空」
        obs = _obs(body="", elements=0, video=0, iframe=0)
        # 但 truncated=False, degraded=[] — 已足够触发最后一条
        stub = _StubLLM(content=(
            '{"category": "nav_aggregator", "playable_subtype": null,'
            ' "evidence": "导航", "confidence": 0.5}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score is None
        assert v.kind == "undetermined"
        assert v.is_veto is False

    def test_观察裁剪_判_undetermined(self):
        """elements_total > len(interactive_elements) → 观察被裁 → quarantine。"""
        obs = _obs(body="内容", elements=3)
        obs["elements_total"] = 100  # 远大于实际保留的 3 条
        stub = _StubLLM(content=(
            '{"category": "review", "playable_subtype": null,'
            ' "evidence": "影评内容", "confidence": 0.5}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score is None
        assert v.kind == "undetermined"
        assert "裁剪" in v.reasons[0]

    def test_观察_truncated_判_undetermined(self):
        """观察被 max_chars 截断 → quarantine。"""
        obs = _obs(body="内容", elements=3, truncated=True)
        stub = _StubLLM(content=(
            '{"category": "review", "playable_subtype": null,'
            ' "evidence": "影评内容", "confidence": 0.5}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score is None
        assert v.kind == "undetermined"

    def test_无_LLM_client_不抛(self):
        """``llm_client=None`` → undetermined，不抛。"""
        obs = _obs(body="这里是某导航", elements=3)
        v = score_d3(_sample(_out(branch=None), observations=[obs]))
        assert v.score is None
        assert v.kind == "undetermined"
        assert "未提供 LLM client" in v.reasons[0]

    def test_无_观察_判_undetermined(self):
        """样本没有观察 → undetermined（不调 LLM）。"""
        stub = _StubLLM(content=(
            '{"category": "nav_aggregator", "playable_subtype": null,'
            ' "evidence": "x", "confidence": 0.5}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=()),
                     llm_client=stub)
        assert v.score is None
        assert v.kind == "undetermined"
        # **关键**：这种情况下不应触达 stub —— 观察不充分早返回
        # 通过 stub 不被实例化时的 chat() 自然不会被调用来隐式保证


# ── 3. rubric v1.2 契约冻结 ────────────────────────────────────────


class TestRubric对齐:
    def test_5类_契约冻结(self):
        """rubric §D-3 5 类必须与本模块 D3_CATEGORIES 完全一致。
        增删类别 = 改 rubric → 必须升 v 号 + 重打黄金集。"""
        assert D3_CATEGORIES == frozenset({
            "playable", "nav_aggregator", "review", "irrelevant", "ambiguous",
        })

    def test_4_子分类_契约冻结(self):
        """rubric §D-3 playable 子分类 4 类必须冻结。"""
        assert D3_SUBTYPES == frozenset({
            "free", "login_required", "partial_free", "subscription",
        })

    def test_协议_三态(self):
        """VerdictKind 必须是 ``Literal['scored', 'undetermined']``。"""
        from typing import Literal
        assert d3_scorer.VerdictKind == Literal["scored", "undetermined"]

    def test_协议_字段(self):
        """D3Verdict 必须含 is_veto + to_json。"""
        v = D3Verdict(score=1, kind="scored")
        assert v.is_veto is True
        j = v.to_json()
        assert j["dimension"] == "D-3"
        assert j["score"] == 1
        assert j["kind"] == "scored"
        # 非 1 档 is_veto=False
        v2 = D3Verdict(score=5, kind="scored")
        assert v2.is_veto is False


# ── 4. raw CoT 红线 ──────────────────────────────────────────────


class Test红线:
    def test_evidence_不含_think(self):
        """CLAUDE.md 红线：evidence 拼装前已 ``strip_think``，
        模型套 ``<think>...</think>`` 时 evidence 不含 think 残段。"""
        obs = _obs(body="这里是导航列表", elements=3)
        stub = _StubLLM(content=(
            '<think>模型内心推理过程,想到导航二字</think>'
            '{"category": "nav_aggregator", "playable_subtype": null,'
            ' "evidence": "页面是导航列表",'
            ' "confidence": 0.8}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score in (4, 5)
        assert "<think>" not in v.evidence
        assert "</think>" not in v.evidence

    def test_evidence_含_嵌套_alt_think(self):
        """模型偶用 ``<thinking>`` 嵌套也得剥 —— schema_parse 兜底。"""
        obs = _obs(body="这里是导航列表", elements=3)
        stub = _StubLLM(content=(
            '<thinking>模型内心推理</thinking>'
            '{"category": "nav_aggregator", "playable_subtype": null,'
            ' "evidence": "页面是导航列表",'
            ' "confidence": 0.8}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score in (4, 5)
        assert "<thinking>" not in v.evidence
        assert "</thinking>" not in v.evidence


# ── 5. evidence 「concrete」启发式 ────────────────────────────────


class TestEvidenceConcrete:
    def test_evidence_引用_body_子串_5分(self):
        obs = _obs(body="abc这里是导航聚合站abcdef", elements=3)
        stub = _StubLLM(content=(
            '{"category": "nav_aggregator", "playable_subtype": null,'
            ' "evidence": "观察含「这里是导航聚合站」,符合导航聚合形态",'
            ' "confidence": 0.8}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score == 5

    def test_evidence_引用_元素_label_5分(self):
        obs = _obs(body="内容", elements=2)
        # label 之一为「链接0」,evidence 引用它
        stub = _StubLLM(content=(
            '{"category": "nav_aggregator", "playable_subtype": null,'
            ' "evidence": "页面元素 链接0 是导航链接",'
            ' "confidence": 0.8}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score == 5

    def test_evidence_提到_数字_5分(self):
        obs = _obs(body="内容", elements=3, video=3)
        stub = _StubLLM(content=(
            '{"category": "playable", "playable_subtype": "free",'
            ' "evidence": "页面 video 标签 3 个,可直接播放",'
            ' "confidence": 0.8}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score == 5

    def test_evidence_完全泛泛_4分(self):
        obs = _obs(body="这里是导航聚合站", elements=3)
        stub = _StubLLM(content=(
            '{"category": "nav_aggregator", "playable_subtype": null,'
            ' "evidence": "不是播放站",'
            ' "confidence": 0.7}'
        ))
        v = score_d3(_sample(_out(branch=None), observations=[obs]),
                     llm_client=stub)
        assert v.score == 4


# ── 6. 边界 ──────────────────────────────────────────────────────


class Test边界:
    def test_acceptable_evidence_4字子串(self):
        """``_is_evidence_concrete`` 的 4 字子串匹配 —— body 的 4 字连续子串。"""
        # 直接调内部函数做性质断言
        from trajectory_pipeline.evaluation.scorers.d3_scorer import (
            _is_evidence_concrete,
        )
        obs = {"body_text": "abcdefghijk",
               "interactive_elements": [{"label": ""}],
               "video_tag_count": 0, "iframe_count": 0}
        # 含 "cdef" → True
        assert _is_evidence_concrete("包含 cdef 的 evidence", [obs]) is True
        # 不含任何 4 字子串 → False
        assert _is_evidence_concrete("泛泛描述", [obs]) is False

    def test_mapping_形态_也能打分(self):
        """P2 字典形态（Mapping）样本走同一入口 —— 与 d4 同款纪律。"""
        obs = _obs(body="这里是导航列表", elements=3)
        stub = _StubLLM(content=(
            '{"category": "nav_aggregator", "playable_subtype": null,'
            ' "evidence": "页面是导航列表",'
            ' "confidence": 0.8}'
        ))
        dict_sample = {
            "sample_id": "T1", "task_id": "T1", "unit": "u", "title": "t",
            "six": {"observations": [obs]},
            "outcome": {"url": "https://x.test", "branch": None,
                        "site_url": "https://x.test"},
        }
        v = score_d3(dict_sample, llm_client=stub)
        assert v.score in (4, 5)
        assert v.category == "nav_aggregator"
    def test_协议_三态(self):
        """VerdictKind 必须是 ``Literal['scored', 'undetermined']``。"""
        from typing import Literal, get_args
        assert d3_scorer.VerdictKind == Literal["scored", "undetermined"]
