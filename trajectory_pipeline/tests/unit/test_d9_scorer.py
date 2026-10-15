"""D-9「思考真实性」LLM judge 评分器的单元测试。

7 条 contract 测试（plan §三·第三步）+ 几条边界测试。覆盖：

  1. 触发条件（gate.failed / passed+undeterminable / passed+undeterminable 空 / not_run）
  2. 五档映射（anchored / vague / mild_hallucination / hindsight / systematic / misleading）
  3. 三态 + ``not_evaluated``（成本控制跳过 vs 不可判定 vs 有分）
  4. 观察不充分 → undetermined
  5. ``llm_client=None`` 不抛
  6. 失败 ≠ 1 档（rubric 锚点 1 是 LLM 真判，不是 fail-closed）
  7. raw CoT 红线（evidence 不含 think）
  8. rubric v1.2 契约冻结（6 类 verdict + 锚点映射）
"""

from __future__ import annotations

from typing import Literal

import pytest

from trajectory_pipeline.assembler.schema import GateMark, OutcomeRef, Sample
from trajectory_pipeline.evaluation.scorers import d9_scorer
from trajectory_pipeline.evaluation.scorers.d9_scorer import (
    CONFIDENCE_JUDGED,
    CONFIDENCE_NONE,
    D9_VERDICTS,
    D9Verdict,
    VerdictKind,
    score_d9,
)


# ── helpers ────────────────────────────────────────────────────────


def _obs(idx=0, body="页面有 video 标签", url="https://x.test/", **kw):
    base = {
        "url": url, "page_title": f"P{idx}", "body_text": body,
        "video_tag_count": 1, "iframe_count": 0,
        "interactive_elements": [{"tag": "a", "label": f"链接{idx}"}],
        "links": [], "degraded": [], "truncated": False,
    }
    # `elements=N` / `elements=0` 是 `interactive_elements` 的同义简写——
    # 让「观察全空」类测试一行能写出。
    if "elements" in kw:
        kw["interactive_elements"] = (
            [{"tag": "a", "label": f"链接{idx}"}] if kw.pop("elements")
            else []
        )
    base.update(kw)
    return base


def _rationale(text="观察到 video 标签,无法确认可用,进入验证",
               step_index=0, conclusion="无法确认是否可用"):
    return {"step_index": step_index, "text": text,
            "conclusion": conclusion, "status": "written", "reason": ""}


def _gate_result(status="passed", findings=(), undeterminable=()):
    """闸门详细结果（Mapping 形态，与 d4 helper 同款纪律）。"""
    return {"status": status, "findings": list(findings),
            "undeterminable": list(undeterminable), "checked_entities": 0}


def _finding(kind="unsupported_entity", detail="引用了不在观察里的 X",
             severity="hard"):
    return {"kind": kind, "detail": detail, "severity": severity}


def _sample(rationales=(), observations=(), gate=None):
    return Sample(
        sample_id="T001__a1__site-1", task_id="T001", unit="site-1",
        title="功夫", system_prompt="s", tools=(), user_prompt="u",
        provenance={}, actions=(), observations=tuple(observations),
        rationales=tuple(rationales),
        gate=gate or GateMark(status="not_run"),
        outcome=OutcomeRef(url="https://x.test/", branch=None),
    )


class _StubLLM:
    """最小 LLM stub。"""

    def __init__(self, *, content=None, return_none=False):
        self._content = content
        self._return_none = return_none
        self.last_error = ""
        self.last_user = ""
        self.last_system = ""

    def chat(self, system, user, *, max_tokens=None):
        self.last_system = system
        self.last_user = user
        if self._return_none:
            return None
        return self._content


# ── 1. 触发条件 ───────────────────────────────────────────────────


class Test触发条件:
    def test_gate_failed_强制触发(self):
        """``gate.status == failed`` → 必须走 LLM（plan §三·第三步）。"""
        obs = _obs()
        rat = _rationale()
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        stub = _StubLLM(content=(
            '{"verdict": "misleading",'
            ' "evidence": "rationale 引用了不存在的实体 X",'
            ' "confidence": 0.8}'
        ))
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.score == 1
        assert v.kind == "scored"
        assert v.verdict == "misleading"

    def test_gate_passed_且_undeterminable_空_不触发(self):
        """成本控制：``gate.passed + undeterminable 空`` → ``not_evaluated``。"""
        obs = _obs()
        rat = _rationale()
        gate_result = _gate_result(status="passed",
                                   findings=[], undeterminable=[])
        # 即使给了 LLM client，也不该调它
        stub = _StubLLM(content=(
            '{"verdict": "anchored", "evidence": "x", "confidence": 0.8}'
        ))
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.score is None
        assert v.kind == "not_evaluated"
        assert "成本控制" in v.reasons[0]

    def test_gate_passed_且_undeterminable_非空_触发(self):
        """``gate.passed + undeterminable 非空`` → 走 LLM。"""
        obs = _obs()
        rat = _rationale()
        gate_result = _gate_result(
            status="passed", findings=[],
            undeterminable=["观察被裁剪,实体可能落在裁剪区外"],
        )
        stub = _StubLLM(content=(
            '{"verdict": "anchored",'
            ' "evidence": "rationale 措辞如实表达不确定",'
            ' "confidence": 0.8}'
        ))
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.kind == "scored"
        assert v.score == 5

    def test_gate_not_run_不触发(self):
        """``gate.not_run`` → ``not_evaluated``（rationale 为空或闸门未跑）。"""
        obs = _obs()
        rat = _rationale()
        gate_result = _gate_result(status="not_run")
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=_StubLLM())
        assert v.kind == "not_evaluated"
        assert v.score is None

    def test_回落_sample_gate_status_failed_也触发(self):
        """``gate_result=None`` 但 ``sample.gate.status == failed`` → 仍触发。"""
        obs = _obs()
        rat = _rationale()
        s = _sample(rationales=[rat], observations=[obs],
                    gate=GateMark(status="failed", reasons=("闸门失败",)))
        stub = _StubLLM(content=(
            '{"verdict": "systematic",'
            ' "evidence": "rationale 系统性套话,无具体锚定",'
            ' "confidence": 0.8}'
        ))
        v = score_d9(s, gate_result=None, llm_client=stub)
        assert v.kind == "scored"
        assert v.score == 2

    def test_回落_sample_gate_status_passed_不触发(self):
        """``gate_result=None`` 且 ``sample.gate.status == passed`` → 不触发。"""
        obs = _obs()
        rat = _rationale()
        s = _sample(rationales=[rat], observations=[obs],
                    gate=GateMark(status="passed"))
        v = score_d9(s, gate_result=None, llm_client=_StubLLM())
        assert v.kind == "not_evaluated"


# ── 2. 五档映射 ───────────────────────────────────────────────────


class Test五档映射:
    def test_5分_全锚定(self):
        obs = _obs(body="页面有 video 标签")
        rat = _rationale(text="观察到 video 标签,无法确认可用,进入验证")
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        stub = _StubLLM(content=(
            '{"verdict": "anchored",'
            ' "evidence": "rationale 含『观察到 video 标签』,与 obs 一致",'
            ' "confidence": 0.8}'
        ))
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.score == 5
        assert v.verdict == "anchored"
        assert v.confidence == CONFIDENCE_JUDGED

    def test_3分_后见之明(self):
        obs = _obs(body="页面有 video 标签")
        rat = _rationale(text="事后看,这个站是付费站——其实一开始就该订阅")
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        stub = _StubLLM(content=(
            '{"verdict": "hindsight",'
            ' "evidence": "rationale 含『事后看』『其实一开始就该』",'
            ' "confidence": 0.7}'
        ))
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.score == 3
        assert v.verdict == "hindsight"

    def test_1分_编造关键事实(self):
        """rubric 锚点 1：rationale 编造关键事实 → 1 档（LLM 真判，不是 fail-closed）。"""
        obs = _obs(body="页面无 video 标签,只有广告 iframe")
        rat = _rationale(text="观察到 video 标签,直接播放")
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        stub = _StubLLM(content=(
            '{"verdict": "misleading",'
            ' "evidence": "rationale 说『观察到 video 标签』但 obs 显示 video=0",'
            ' "confidence": 0.9}'
        ))
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.score == 1
        assert v.verdict == "misleading"
        # D-9 当前不是 G-1 一票否决维度（只有 D-5/D-10 是）
        assert v.is_veto is False


# ── 3. 三态 + not_evaluated 区分 ──────────────────────────────────


class Test三态:
    def test_观察全空_触发条件成立时_判_undetermined(self):
        """闸门触发 + 观察全空 → undetermined（quarantine）。"""
        rat = _rationale()
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        # body_text="" + 元素空 → 观察全空
        obs = _obs(body="", elements=0, video=0)
        # 但 truncated=False, degraded=[] → 仍触发最后一条判据
        stub = _StubLLM(content=(
            '{"verdict": "anchored", "evidence": "x", "confidence": 0.5}'
        ))
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.kind == "undetermined"
        assert v.score is None

    def test_无_LLM_client_触发条件成立时_判_undetermined(self):
        """闸门触发但 llm_client=None → undetermined（不抛）。"""
        obs = _obs()
        rat = _rationale()
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result)
        assert v.score is None
        assert v.kind == "undetermined"
        assert "未提供 LLM client" in v.reasons[0]

    def test_not_evaluated_与_undetermined_语义不同(self):
        """``not_evaluated`` 是成本控制跳过，``undetermined`` 是不可判定——
        两者 kind 不同，下游分别记账。"""
        # not_evaluated: 闸门 passed + undeterminable 空
        obs = _obs()
        rat = _rationale()
        gate_result = _gate_result(status="passed")
        v_skip = score_d9(_sample(rationales=[rat], observations=[obs]),
                          gate_result=gate_result,
                          llm_client=_StubLLM(content="x"))
        assert v_skip.kind == "not_evaluated"

        # undetermined: 闸门触发 + 观察全空
        gate_result2 = _gate_result(status="failed",
                                    findings=[_finding(severity="hard")])
        obs_empty = _obs(body="", elements=0)
        v_und = score_d9(_sample(rationales=[rat], observations=[obs_empty]),
                         gate_result=gate_result2,
                         llm_client=_StubLLM(content="x"))
        assert v_und.kind == "undetermined"
        assert v_skip.kind != v_und.kind


# ── 4. 失败 ≠ 1 档 ────────────────────────────────────────────────


class Test失败不走1档:
    def test_LLM_调不通_判_undetermined_不是_1(self):
        """D-9 rubric 锚点 1 是 LLM 真判「misleading」，不是 fail-closed。
        LLM 调不通 → undetermined，不走 1 档。"""
        obs = _obs()
        rat = _rationale()
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        stub = _StubLLM(content=None, return_none=True)
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.score is None
        assert v.kind == "undetermined"
        assert "不走 1 档" in v.reasons[0]

    def test_解析失败_判_undetermined(self):
        """非 JSON → undetermined。"""
        obs = _obs()
        rat = _rationale()
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        stub = _StubLLM(content="完全不是 JSON 的散文。")
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.score is None
        assert v.kind == "undetermined"

    def test_verdict_不在契约_判_undetermined(self):
        """verdict 飘出契约（如 ``fictional``）→ undetermined。"""
        obs = _obs()
        rat = _rationale()
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        stub = _StubLLM(content=(
            '{"verdict": "fictional", "evidence": "x", "confidence": 0.8}'
        ))
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.score is None
        assert v.kind == "undetermined"


# ── 5. raw CoT 红线 ──────────────────────────────────────────────


class Test红线:
    def test_evidence_不含_think(self):
        """CLAUDE.md 红线：evidence 拼装前已 ``strip_think``。"""
        obs = _obs()
        rat = _rationale()
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        stub = _StubLLM(content=(
            '<think>模型内心推理</think>'
            '{"verdict": "anchored",'
            ' "evidence": "rationale 措辞如实,无事实错误",'
            ' "confidence": 0.8}'
        ))
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.kind == "scored"
        assert "<think>" not in v.evidence
        assert "</think>" not in v.evidence

    def test_evidence_含_嵌套_alt_think(self):
        obs = _obs()
        rat = _rationale()
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        stub = _StubLLM(content=(
            '<thinking>alt 标签</thinking>'
            '{"verdict": "anchored",'
            ' "evidence": "rationale 措辞如实",'
            ' "confidence": 0.8}'
        ))
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate_result, llm_client=stub)
        assert v.kind == "scored"
        assert "<thinking>" not in v.evidence
        assert "</thinking>" not in v.evidence


# ── 6. rubric v1.2 契约冻结 ────────────────────────────────────────


class TestRubric对齐:
    def test_6类_契约冻结(self):
        """rubric §D-9 6 类 verdict 必须与本模块 D9_VERDICTS 完全一致。"""
        assert D9_VERDICTS == frozenset({
            "anchored", "vague", "mild_hallucination", "hindsight",
            "systematic", "misleading",
        })

    def test_VerdictKind_三态加一档(self):
        """VerdictKind 必须是三态 + not_evaluated（成本控制跳过）。"""
        assert VerdictKind == Literal["scored", "undetermined", "not_evaluated"]

    def test_协议_字段(self):
        """D9Verdict 必须含 verdict / evidence / is_veto / to_json。"""
        v = D9Verdict(score=1, kind="scored", verdict="misleading",
                      evidence="x")
        assert v.is_veto is False  # D-9 不是 G-1 一票否决
        j = v.to_json()
        assert j["dimension"] == "D-9"
        assert j["score"] == 1
        assert j["verdict"] == "misleading"

    def test_锚点映射_冻结(self):
        """verdict → 分数映射必须冻结——任意一条改了都会动 rubric 锚点。"""
        from trajectory_pipeline.evaluation.scorers.d9_scorer import (
            _VERDICT_TO_SCORE,
        )
        assert dict(_VERDICT_TO_SCORE) == {
            "anchored": 5,
            "vague": 4,
            "mild_hallucination": 3,
            "hindsight": 3,
            "systematic": 2,
            "misleading": 1,
        }


# ── 7. 边界 ──────────────────────────────────────────────────────


class Test边界:
    def test_mapping_形态_也能打分(self):
        """P2 字典形态（Mapping）样本走同一入口 —— 与 d3 同款纪律。"""
        obs = _obs(body="页面有 video 标签")
        rat = _rationale(text="观察到 video 标签")
        gate_result = _gate_result(status="failed",
                                   findings=[_finding(severity="hard")])
        stub = _StubLLM(content=(
            '{"verdict": "anchored",'
            ' "evidence": "rationale 含『观察到 video 标签』",'
            ' "confidence": 0.8}'
        ))
        dict_sample = {
            "sample_id": "T1", "task_id": "T1", "unit": "u", "title": "t",
            "six": {"observations": [obs], "rationales": [rat]},
            "gate": {"status": "failed", "reasons": ("x",)},
            "outcome": {"url": "https://x.test", "branch": None},
        }
        v = score_d9(dict_sample, gate_result=gate_result, llm_client=stub)
        assert v.score == 5
        assert v.kind == "scored"

    def test_未提供_gate_result_且_sample_gate_也缺失(self):
        """``gate_result=None`` 且 sample.gate 缺失 → ``not_evaluated``。"""
        obs = _obs()
        rat = _rationale()
        # Mapping 形态且无 gate 字段
        dict_sample = {
            "sample_id": "T1", "task_id": "T1", "unit": "u", "title": "t",
            "six": {"observations": [obs], "rationales": [rat]},
            # 故意不放 gate
            "outcome": {"url": "https://x.test", "branch": None},
        }
        v = score_d9(dict_sample, llm_client=_StubLLM(content="x"))
        assert v.kind == "not_evaluated"

    def test_should_trigger_llm_内部函数_直接钉(self):
        """``_should_trigger_llm`` 行为契约——直接调内部函数钉住触发矩阵。"""
        from trajectory_pipeline.evaluation.scorers.d9_scorer import (
            _should_trigger_llm,
        )
        sample = _sample()

        # failed + 1 条 hard → True
        assert _should_trigger_llm(
            sample,
            _gate_result(status="failed",
                         findings=[_finding(severity="hard")])
        )[0] is True
        # failed + 1 条 soft（无 hard）→ 仍是 failed（status 字段决定），
        # 仍然触发（plan 说 failed 就触发）
        assert _should_trigger_llm(
            sample,
            _gate_result(status="failed",
                         findings=[_finding(severity="soft")])
        )[0] is True
        # passed + undeterminable 非空 → True
        assert _should_trigger_llm(
            sample,
            _gate_result(status="passed", undeterminable=["x"])
        )[0] is True
        # passed + undeterminable 空 → False
        assert _should_trigger_llm(
            sample,
            _gate_result(status="passed", undeterminable=[])
        )[0] is False
        # not_run → False
        assert _should_trigger_llm(
            sample, _gate_result(status="not_run"))[0] is False
        # passed + undeterminable 空 → False
        assert _should_trigger_llm(
            sample,
            _gate_result(status="passed", undeterminable=[])
        )[0] is False
        # not_run → False
        assert _should_trigger_llm(
            sample, _gate_result(status="not_run"))[0] is False


# ── 9. few-shot 注入点（增量三十八联动事项 #1）────────────────


class TestFewShot注入点:
    """D-9 few-shot 注入点：``D9_FEW_SHOT_EXAMPLES`` + ``_render_few_shot``
    + ``_render_user(few_shot_examples=...)`` + ``score_d9(few_shot_examples=...)``。

    设计纪律：
      - ``None`` → 回落 :data:`D9_FEW_SHOT_EXAMPLES` 模块常量
        （INC40 后非空 → 默认**插入** few-shot 块）
      - ``[]`` → 强制不插（绕过常量）
      - 非空 → 按 verdict / rationale / observation 三字段渲染
      - 块在闸门 findings 之后、Rationale 原文之前
    """

    def test_默认_不插_few_shot_块(self):
        """``_render_user`` 不传 ``few_shot_examples`` → ``None`` 渲染为空 →
        输出 prompt **不含** ``## few-shot examples`` 字样（常量回落只发生在
        :func:`score_d9`，渲染层 ``None`` 就是「不带示例」）。"""
        obs = _obs()
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        from trajectory_pipeline.evaluation.scorers.d9_scorer import (
            _render_user,
        )
        prompt = _render_user([obs], [rat], [], gate)
        assert "## few-shot examples" not in prompt

    def test_显式_空序列_也_不插_few_shot_块(self):
        """``few_shot_examples=[]`` 强制不插——区分 ``None``（用常量）与
        ``[]``（明确不要）。"""
        obs = _obs()
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        from trajectory_pipeline.evaluation.scorers.d9_scorer import (
            _render_user,
        )
        prompt = _render_user([obs], [rat], [], gate, few_shot_examples=[])
        assert "## few-shot examples" not in prompt

    def test_非空序列_插入示例(self):
        """``few_shot_examples=[{verdict, rationale, observation}]`` →
        例子被注入 ``## few-shot examples`` 块。"""
        obs = _obs()
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        from trajectory_pipeline.evaluation.scorers.d9_scorer import (
            _render_user,
        )
        shots = [
            {"verdict": "anchored", "rationale": "看到 video 标签",
             "observation": "页面有 video 标签"},
            {"verdict": "vague", "rationale": "措辞含混",
             "observation": "页面提到会员但无字面"},
        ]
        prompt = _render_user([obs], [rat], [], gate,
                              few_shot_examples=shots)
        assert "## few-shot examples" in prompt
        # 两份示例都进 prompt
        assert "示例 1（anchored）" in prompt
        assert "示例 2（vague）" in prompt
        # 三字段都渲染
        assert "看到 video 标签" in prompt
        assert "页面有视频标签" in prompt or "页面有 video 标签" in prompt
        assert "措辞含混" in prompt
        assert "页面提到会员但无字面" in prompt

    def test_位置_在_闸门_findings_之后_Rationale_原文_之前(self):
        """few-shot 块插在闸门 findings 之后、rationale 原文之前（与
        ``_render_user`` docstring 一致）。"""
        obs = _obs()
        rat = _rationale(text="看到 video 标签 —— 测试定位专用")
        gate = _gate_result(
            status="failed",
            findings=[_finding(severity="hard", detail="测试定位专用 finding")],
        )
        from trajectory_pipeline.evaluation.scorers.d9_scorer import (
            _render_user,
        )
        shots = [{"verdict": "anchored", "rationale": "测试定位专用 shot",
                  "observation": "测试定位专用 obs"}]
        prompt = _render_user([obs], [rat], [], gate,
                              few_shot_examples=shots)
        # 三段文本各自带唯一前缀——finding / shot / rationale 段
        # rationale 文本是「看到 video 标签 —— 测试定位专用」，
        # 用 `—— 测试定位专用` 作锚点（含 em dash，避免与 few-shot
        # 段「测试定位专用 shot」混淆）
        i_finding = prompt.find("测试定位专用 finding")
        i_shot = prompt.find("测试定位专用 shot")
        i_rationale = prompt.find("—— 测试定位专用")
        # 三个位置都找到
        assert i_finding >= 0, f"未找到 finding 文本: {prompt!r}"
        assert i_shot >= 0, f"未找到 shot 文本: {prompt!r}"
        assert i_rationale >= 0, f"未找到 rationale 文本: {prompt!r}"
        # 顺序：finding < shot < rationale
        assert i_finding < i_shot < i_rationale, (
            f"顺序错：finding={i_finding}, shot={i_shot}, "
            f"rationale={i_rationale}；prompt={prompt!r}"
        )

    def test_D9_SYSTEM_PROMPT_含_sentinel_行(self):
        """``D9_SYSTEM_PROMPT`` 含 sentinel 行 ``## few-shot examples
        会在下面追加``——后续定位提示，方便 reviewer 发现 few-shot 插入位。"""
        assert "## few-shot examples 会在下面追加" in d9_scorer.D9_SYSTEM_PROMPT
        # sentinel 出现在 discipline 之后（最后一段）
        i_discipline = d9_scorer.D9_SYSTEM_PROMPT.rfind("## 纪律")
        i_sentinel = d9_scorer.D9_SYSTEM_PROMPT.find("## few-shot examples 会在下面追加")
        assert i_discipline >= 0 and i_sentinel >= 0
        assert i_discipline < i_sentinel, (
            "sentinel 必须出现在 ## 纪律 段之后"
        )

    def test_few_shot_不破坏_JSON_契约字段(self):
        """few-shot 块不污染 LLM 输出解析——``_parse_response`` 入口在
        system + user prompt 之外。这里验证 ``_render_user`` 含 few-shot
        时仍能产出合法 JSON 字符串（不出现 ``{`` / ``}`` 嵌套破坏）。"""
        from trajectory_pipeline.evaluation.scorers.d9_scorer import (
            _render_user,
        )
        obs = _obs()
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        # 含 JSON-like 文本的示例
        shots = [{"verdict": "anchored",
                  "rationale": "看到 {\"video\": 1} 标签",
                  "observation": "{\"tag\": \"video\"}"}]
        prompt = _render_user([obs], [rat], [], gate,
                              few_shot_examples=shots)
        # 内容仍存在
        assert "示例 1（anchored）" in prompt
        # JSON 字符串转义正确（双花括号被原样保留——LLaMA 类模型常见的转义）
        assert '{"video": 1}' in prompt
        assert '{"tag": "video"}' in prompt

    def test_score_d9_few_shot_None_回落常量(self):
        """``score_d9(few_shot_examples=None)`` 回落 ``D9_FEW_SHOT_EXAMPLES``。

        INC40 后常量非空 → 实际发给 LLM 的用户提示词**含**
        ``## few-shot examples`` 块与每条示例的 verdict 标签（生产路径
        不传参即激活示例池）。"""
        obs = _obs()
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        stub = _StubLLM(content='{"verdict": "anchored", '
                               '"evidence": "ok", "confidence": 0.9}')
        sample = _sample(rationales=[rat], observations=[obs])
        # 调用不抛、给正常 verdict
        v = score_d9(sample, gate_result=gate, llm_client=stub)
        assert v.kind == "scored"
        assert v.verdict == "anchored"
        # 回落常量后池真正注入了用户提示词
        assert "## few-shot examples" in stub.last_user
        for i, ex in enumerate(d9_scorer.D9_FEW_SHOT_EXAMPLES):
            assert f"示例 {i + 1}（{ex['verdict']}）" in stub.last_user
        # few-shot 块在系统提示词的 sentinel 承诺的位置（用户提示词内）
        assert "## few-shot examples 会在下面追加" in d9_scorer.D9_SYSTEM_PROMPT

    def test_score_d9_显式_非空_注入_LLM(self):
        """``score_d9(few_shot_examples=[...])`` 不破坏 LLM 调通路径——
        LLM 仍按 verdict 输出 evidence。"""
        obs = _obs()
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        stub = _StubLLM(content='{"verdict": "vague", '
                               '"evidence": "措辞含糊", "confidence": 0.85}')
        sample = _sample(rationales=[rat], observations=[obs])
        v = score_d9(sample, gate_result=gate, llm_client=stub,
                     few_shot_examples=[{"verdict": "anchored",
                                          "rationale": "示例文本",
                                          "observation": "示例观察"}])
        assert v.kind == "scored"
        assert v.verdict == "vague"
        assert v.score == 4

    def test_score_d9_显式_空_不插(self):
        """``score_d9(few_shot_examples=[])`` 强制不插 few-shot 块。"""
        obs = _obs()
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        stub = _StubLLM(content='{"verdict": "anchored", '
                               '"evidence": "ok", "confidence": 0.9}')
        sample = _sample(rationales=[rat], observations=[obs])
        # 调用不抛；分照常出
        v = score_d9(sample, gate_result=gate, llm_client=stub,
                     few_shot_examples=[])
        assert v.kind == "scored"
        assert v.verdict == "anchored"

    def test_D9_FEW_SHOT_EXAMPLES_池契约(self):
        """INC40 v1 池契约——非空不可变 tuple；每条三字段齐全、verdict
        合法枚举、rationale/observation 非空；覆盖 anchored / vague /
        mild_hallucination 三档（hindsight / systematic / misleading 在
        v1 素材库无干净实例，留待 round=2 锚点补充）。"""
        pool = d9_scorer.D9_FEW_SHOT_EXAMPLES
        assert isinstance(pool, tuple)
        assert len(pool) > 0
        valid = {"anchored", "vague", "mild_hallucination",
                 "hindsight", "systematic", "misleading"}
        verdicts = set()
        for ex in pool:
            assert set(ex.keys()) == {"verdict", "rationale", "observation"}
            assert ex["verdict"] in valid
            assert ex["rationale"].strip(), "rationale 不得为空"
            assert ex["observation"].strip(), "observation 不得为空"
            verdicts.add(ex["verdict"])
        assert {"anchored", "vague", "mild_hallucination"} <= verdicts


# ── 10. INC42：truncated 字典兼容 + capping 定向放行 ─────────────


class TestINC42观察充分性两档:
    """INC42（2026-10-15）：原版观察不充分一律 fail-closed，实测 D-9
    judge 有效覆盖仅 1/14（INC41），最大头是 80/N 元素裁剪 + body 完好。

    两档处理：
      - hard（降级 / 截断 / 全空 / 没有观察）→ 仍 fail-closed；
      - capping-only → 放行 LLM，missing-anchor 类 verdict 方向性降级。
    """

    def _capped_obs(self):
        """capping-only 观察：body 完好、无降级、未截断、元素 80/102。"""
        elements = [{"tag": "a", "label": f"L{i}"} for i in range(80)]
        return _obs(body="页面含选集列表与播放入口",
                    interactive_elements=elements, elements_total=102)

    def test_capping_only_不再阻塞_anchored_照常出分(self):
        """裁剪 + body 完好 → LLM 照跑；anchored 在保留区找到锚必真，
        不受裁剪影响 → scored。"""
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        stub = _StubLLM(content='{"verdict": "anchored", '
                               '"evidence": "锚在 body", "confidence": 0.8}')
        v = score_d9(_sample(rationales=[rat],
                             observations=[self._capped_obs()]),
                     gate_result=gate, llm_client=stub)
        assert v.kind == "scored"
        assert v.verdict == "anchored"
        assert v.score == 5

    def test_capping_only_mild_hallucination_降级_undetermined(self):
        """裁剪观察上 judge 判 missing-anchor 类 verdict → 不可信
        （「没找到锚」可能是「被裁掉」）→ fail-closed，judge 原始
        verdict / evidence 留档。"""
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        stub = _StubLLM(content='{"verdict": "mild_hallucination", '
                               '"evidence": "『第8期链接』无观察锚", '
                               '"confidence": 0.8}')
        v = score_d9(_sample(rationales=[rat],
                             observations=[self._capped_obs()]),
                     gate_result=gate, llm_client=stub)
        assert v.kind == "undetermined"
        assert v.score is None
        assert v.verdict is None
        assert "INC42" in v.reasons[0]
        assert "第8期链接" in v.reasons[0]  # judge 原始证据留档

    @pytest.mark.parametrize("verdict", ["systematic", "misleading"])
    def test_capping_only_其余裁剪敏感_verdict_同样降级(self, verdict):
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        stub = _StubLLM(content=f'{{"verdict": "{verdict}", '
                               f'"evidence": "x", "confidence": 0.8}}')
        v = score_d9(_sample(rationales=[rat],
                             observations=[self._capped_obs()]),
                     gate_result=gate, llm_client=stub)
        assert v.kind == "undetermined"

    @pytest.mark.parametrize("verdict,score",
                             [("vague", 4), ("hindsight", 3)])
    def test_capping_only_非锚点类_verdict_不降级(self, verdict, score):
        """vague / hindsight 是措辞判断，不依赖元素全集 → 照常出分。"""
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        stub = _StubLLM(content=f'{{"verdict": "{verdict}", '
                               f'"evidence": "x", "confidence": 0.8}}')
        v = score_d9(_sample(rationales=[rat],
                             observations=[self._capped_obs()]),
                     gate_result=gate, llm_client=stub)
        assert v.kind == "scored"
        assert v.score == score

    def test_降级_观察仍fail_closed(self):
        """degraded body_text（采集失败与「真没有」同形——§5.5 最贵教训）
        不因 INC42 放松：仍 undetermined，LLM 不被调。"""
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        stub = _StubLLM(content='{"verdict": "anchored", '
                               '"evidence": "x", "confidence": 0.8}')
        obs = _obs(body="", elements=0, degraded=["body_text"])
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate, llm_client=stub)
        assert v.kind == "undetermined"
        assert stub.last_user == ""  # LLM 根本没被调

    def test_truncated字典全False_放行_E001回归(self):
        """E001__57bbf995__site-2 回归：p2 样本 truncated 为
        {'source': False, 'view': False}——原 truthy 判断误标「被截断」，
        修复后不阻塞。"""
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        stub = _StubLLM(content='{"verdict": "anchored", '
                               '"evidence": "x", "confidence": 0.8}')
        obs = _obs(body="vip.1905.com 播放页提示需登录会员观看",
                   truncated={"source": False, "view": False})
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate, llm_client=stub)
        assert v.kind == "scored"

    def test_truncated字典含True_仍阻塞(self):
        """dict 任一子观察截断 → 真 · 截断，仍 fail-closed。"""
        rat = _rationale()
        gate = _gate_result(status="failed",
                            findings=[_finding(severity="hard")])
        stub = _StubLLM(content='{"verdict": "anchored", '
                               '"evidence": "x", "confidence": 0.8}')
        obs = _obs(body="正文", truncated={"source": False, "view": True})
        v = score_d9(_sample(rationales=[rat], observations=[obs]),
                     gate_result=gate, llm_client=stub)
        assert v.kind == "undetermined"
        assert stub.last_user == ""

    def test__capping_only_判定表(self):
        """``_capping_only`` 真值表直接钉：只有「纯裁剪」返回 True。"""
        from trajectory_pipeline.evaluation.scorers.d9_scorer import (
            _capping_only,
        )
        assert _capping_only(self._capped_obs()) is True
        # 无裁剪 → False
        assert _capping_only(_obs()) is False
        # 降级 → False（hard）
        assert _capping_only(
            _obs(degraded=["body_text"], elements_total=102)) is False
        # 截断（bool 真）→ False（hard）
        assert _capping_only(
            _obs(truncated=True, elements_total=102)) is False
        # 截断（字典含真）→ False（hard）
        assert _capping_only(
            _obs(truncated={"view": True}, elements_total=102)) is False
        # 全空 + 裁剪 → False（body 与保留元素皆空是 hard）
        assert _capping_only(
            _obs(body="", elements=0, elements_total=5)) is False
        # None → False
        assert _capping_only(None) is False
