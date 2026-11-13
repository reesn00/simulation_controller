"""H-36：评估侧的「判分不可信」必须**可归因 + 可熔断 + 不进人工队列**。

事故背景（2026-11-11 那批，919 个评分单元）::

    pos=73  neg=1  quarantine=838  skip=7   ← 退出码仍是 0

那 838 条 quarantine 里有 **578 条**是「LLM 未判（LLM 返回空）」
——**裁判后端答不出**；另 478 条是「老存档缺字段」——**素材不全**。
两者落进**同一个桶**，报告上分不出来，而处置方向正好相反
（一个去修后端，一个去补素材）。

客户端的 :class:`CircuitBreaker` 只数 **4xx** ⇒ 那批失败全是「4xx 之外」
⇒ 熔断器一次都没动 ⇒ 整批**安静地**退化成 91 % quarantine。

钉四件事：

1. **归因**：错误码 → 分类 → reason 里说得清是哪一类失败；
2. **熔断**：连续 N 次失败 ⇒ 停本批 D-2 调用 + 显式报「本批判分不可信」；
3. **分栏**：quarantine 分成「数据判不出」/「LLM 没答」两栏；
4. **闸门**：LLM 没答的记录**不进人工队列**（upload / refresh 都拦）。

不触网：LLM 客户端用假对象，scout 跑在临时目录里。
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

from trajectory_pipeline.evaluation.scout import io as scout_io
from trajectory_pipeline.evaluation.scorers.code_scorer import (
    _parse_response,
    score_d2,
)
from trajectory_pipeline.judgement_causes import (
    CAUSE_DATA,
    CAUSE_LLM,
    CAUSE_NO_CLIENT,
    CAUSE_SCORED,
)
from trajectory_pipeline.llm.client import (
    LLM_FAILURE_BUDGET,
    LLM_FAILURE_CIRCUIT,
    LLM_FAILURE_CLIENT,
    LLM_FAILURE_NONE,
    LLM_FAILURE_RATE_LIMIT,
    LLM_FAILURE_SERVER,
    LLM_FAILURE_SHAPE,
    LLM_FAILURE_TIMEOUT,
    LLM_FAILURE_TRANSPORT,
    LLM_FAILURE_UNKNOWN,
    classify_llm_failure,
    describe_llm_failure,
)


# ── 假 LLM 客户端 ────────────────────────────────────────────────


class _FakeClient:
    """只实现 scout 用到的那几个口子的假客户端。

    ⚠️ ``breaker = None``：客户端级熔断**只数 4xx**，2026-11-11 那批
    失败全在 4xx 之外 ⇒ 客户端熔断一次都没动。**这正是要测的场景**，
    所以这里刻意不给它 breaker。
    """

    def __init__(self, *, answer: str | None = None, error: str = "http:500"):
        self._answer = answer
        self._error = error
        self.last_call_error = ""
        self.calls = 0
        self.cfg = SimpleNamespace(circuit_breaker_threshold=3)
        self.breaker = None

    def chat(self, system: str, user: str, *, max_tokens: int | None = None) -> str | None:
        self.calls += 1
        if self._answer is None:
            self.last_call_error = self._error
            return None
        self.last_call_error = ""
        return self._answer


# ── 假样本 ────────────────────────────────────────────────────────


def _fake_sample(sample_id: str = "T001__r1") -> SimpleNamespace:
    """一条**代码筛能过**的样本：1 个模型动作 + 1 条可用观察。

    ⚠️ 「可用观察」很关键：观察三项全空 ⇒ ``screen_d2`` 判 ``undetermined``
    ⇒ 归因落到 ``data``，测的就不是 LLM 侧了。
    """
    action = SimpleNamespace(
        tool="goto", params={"url": "https://x.test/"}, target=None, origin="model",
    )
    observation = {
        "url": "https://x.test/",
        "page_title": "首页",
        "body_text": "欢迎",
        "interactive_elements": [],
        "links": [],
    }
    return SimpleNamespace(
        sample_id=sample_id,
        task_id="T001",
        user_prompt="去 x.test",
        actions=[action],
        observations=[observation],
    )


# ════════════════════════════════════════════════════════════════════
# 1. 错误码归因
# ════════════════════════════════════════════════════════════════════


class Test错误码归因:
    def test_空码算没失败(self):
        assert classify_llm_failure("") == LLM_FAILURE_NONE

    @pytest.mark.parametrize(
        ("code", "kind"),
        [
            ("circuit:open", LLM_FAILURE_CIRCUIT),
            ("budget:_BudgetExhausted", LLM_FAILURE_BUDGET),
            ("shape:ValueError", LLM_FAILURE_SHAPE),
            ("http:429", LLM_FAILURE_RATE_LIMIT),
            ("http:408", LLM_FAILURE_TIMEOUT),
            ("http:401", LLM_FAILURE_CLIENT),
            ("http:500", LLM_FAILURE_SERVER),
            ("transport:ReadTimeout", LLM_FAILURE_TIMEOUT),
            ("transport:ConnectError", LLM_FAILURE_TRANSPORT),
        ],
    )
    def test_各类错误码归到哪一类(self, code: str, kind: str):
        assert classify_llm_failure(code) == kind

    def test_认不出的码不猜(self):
        """猜错归因 = 把「后端挂了」说成「参数错了」⇒ 处置方向正好相反。"""
        assert classify_llm_failure("http:notanumber") == LLM_FAILURE_UNKNOWN
        assert classify_llm_failure("形状不对") == LLM_FAILURE_UNKNOWN

    def test_描述带码说清哪一类(self):
        assert "http:500" in describe_llm_failure("http:500")
        assert "熔断" in describe_llm_failure("circuit:open")
        assert "限流" in describe_llm_failure("http:429")

    def test_空码描述保留旧口径(self):
        """既有调用点（与既有测试）认「LLM 返回空」这一句——别改口径。"""
        assert describe_llm_failure("") == "LLM 返回空"


# ════════════════════════════════════════════════════════════════════
# 2. 解析层归因
# ════════════════════════════════════════════════════════════════════


class Test解析归因:
    def test_空原文带错误码说清是哪一类(self):
        data, why = _parse_response("", error_code="http:500")
        assert data is None
        assert "http:500" in why

    def test_空原文没错误码保持旧口径(self):
        data, why = _parse_response("")
        assert data is None
        assert why == "LLM 返回空"

    def test_答了但不是JSON是另一类(self):
        """「模型答错了形状」与「模型没答出 JSON」是两件要分别修的事。"""
        data, why = _parse_response("今天天气不错")
        assert data is None
        assert why == "parse_fail"

    def test_顶层不是对象给明确原因(self):
        data, why = _parse_response("[1, 2, 3]")
        assert data is None
        assert "不是对象" in why


# ════════════════════════════════════════════════════════════════════
# 3. D-2 归因
# ════════════════════════════════════════════════════════════════════


class TestD2归因:
    def test_数据侧缺事实归数据(self):
        """没有模型动作 ⇒ 代码筛判不出 ⇒ **与后端无关**。"""
        sample = SimpleNamespace(
            sample_id="T001__r1", task_id="T001", user_prompt="x",
            actions=[], observations=[],
        )
        verdict = score_d2(sample, llm_client=_FakeClient(answer='{"points":[]}'))
        assert verdict.score is None
        assert verdict.cause == CAUSE_DATA

    def test_没配客户端归no_client(self):
        verdict = score_d2(_fake_sample(), llm_client=None)
        assert verdict.score is None
        assert verdict.cause == CAUSE_NO_CLIENT

    def test_后端失败归llm且带错误码(self):
        client = _FakeClient(answer=None, error="http:500")
        verdict = score_d2(_fake_sample(), llm_client=client)
        assert verdict.score is None
        assert verdict.cause == CAUSE_LLM
        assert "http:500" in verdict.reasons[0]

    def test_判出来归空串(self):
        """判出来了 ⇒ 没有缺判 ⇒ cause 为空串（「不是缺判」≠「缺判」）。"""
        client = _FakeClient(answer='{"points": [], "new_breaks": []}')
        verdict = score_d2(_fake_sample(), llm_client=client)
        assert verdict.score == 5
        assert verdict.cause == CAUSE_SCORED

    def test_契约不合也算llm侧(self):
        """后端答了但不合契约 ⇒ 仍是「后端没给出可用判读」⇒ 归 LLM 侧。"""
        client = _FakeClient(answer='{"points": "not-a-list"}')
        verdict = score_d2(_fake_sample(), llm_client=client)
        assert verdict.score is None
        assert verdict.cause == CAUSE_LLM


# ════════════════════════════════════════════════════════════════════
# 4. 批次级熔断
# ════════════════════════════════════════════════════════════════════


def _run_scout(tmp_path: pathlib.Path, monkeypatch, client, *, n: int = 5,
               threshold: int = 3):
    """跑一批（n 条样本，全部同一份假样本）⇒ 返回 (stats, out_dir)。"""
    rows = [
        {"task_id": "T001", "_p2_path": str(tmp_path / f"T001__r{i}.json")}
        for i in range(n)
    ]
    monkeypatch.setattr(scout_io, "list_p2_samples", lambda root: rows)
    monkeypatch.setattr(scout_io, "_resolve_sample", lambda p2, root: _fake_sample())
    out_dir = tmp_path / "out"
    stats = scout_io.run_scout(
        tmp_path, out_dir, llm_client=client, llm_failure_threshold=threshold,
    )
    return stats, out_dir


class Test批次级熔断:
    def test_连续失败到阈值就停(self, tmp_path, monkeypatch):
        """⚠️ **这是 H-36 的核心**：客户端熔断没动（失败不在 4xx 里），
        批次层必须自己停——不然整批安静退化成 91 % quarantine。"""
        client = _FakeClient(answer=None, error="http:500")
        stats, out_dir = _run_scout(tmp_path, monkeypatch, client, n=5, threshold=3)

        # 只发了 3 次请求——第 4、5 条被熔断挡住
        assert client.calls == 3
        assert stats["llm_circuit_open"] == 1
        assert stats["llm_failure_total"] == 3
        # 5 条全落 quarantine，且**全归 LLM 侧**
        assert stats["quarantine"] == 5
        assert stats["quarantine_llm"] == 5
        assert stats["quarantine_data"] == 0
        # 单元计数键相加 = 输入样本数（分栏不进这个和）
        assert sum(stats[k] for k in scout_io.UNIT_COUNTER_KEYS) == 5

        meta = json.loads(
            (out_dir / scout_io.BATCH_META_FILE).read_text(encoding="utf-8"),
        )
        assert meta["judgement_trustworthy"] is False
        assert meta["llm_circuit_open"] is True
        assert "http:500" in meta["last_error_code"]
        assert "不可信" in meta["circuit_reason"]

    def test_后端健康不熔断(self, tmp_path, monkeypatch):
        client = _FakeClient(answer='{"points": [], "new_breaks": []}')
        stats, out_dir = _run_scout(tmp_path, monkeypatch, client, n=4, threshold=3)

        assert client.calls == 4
        assert stats["llm_circuit_open"] == 0
        assert stats["llm_failure_total"] == 0
        assert stats["quarantine_llm"] == 0
        meta = json.loads(
            (out_dir / scout_io.BATCH_META_FILE).read_text(encoding="utf-8"),
        )
        assert meta["judgement_trustworthy"] is True

    def test_健康后失败不误熔断(self, tmp_path, monkeypatch):
        """失败计数是**连续**的：中间判出来一次就清零——单次抖动不该停整批。"""
        class _Flaky(_FakeClient):
            def chat(self, system, user, *, max_tokens=None):
                self.calls += 1
                if self.calls in (2, 4):
                    self.last_call_error = "http:500"
                    return None
                self.last_call_error = ""
                return '{"points": [], "new_breaks": []}'

        client = _Flaky()
        stats, out_dir = _run_scout(tmp_path, monkeypatch, client, n=6, threshold=3)
        assert client.calls == 6
        assert stats["llm_circuit_open"] == 0
        assert stats["llm_failure_total"] == 2

    def test_熔断后剩余的不算失败数(self, tmp_path, monkeypatch):
        """熔断打开后 D-2 **压根没发请求** ⇒ 不计入「后端失败了几次」。"""
        client = _FakeClient(answer=None, error="http:500")
        stats, _ = _run_scout(tmp_path, monkeypatch, client, n=8, threshold=3)
        assert stats["llm_failure_total"] == 3   # 不是 8
        assert stats["quarantine_llm"] == 8      # 但 8 条都缺判

    def test_熔断打开后D4D5D6照跑(self, tmp_path, monkeypatch):
        """熔断的是**裁判后端**，不是全部评分——纯代码判据照跑不误。"""
        client = _FakeClient(answer=None, error="http:500")
        _run_scout(tmp_path, monkeypatch, client, n=2, threshold=1)
        # D-4/D-5/D-6 仍跑了（它们的 score 字段在产物里，不是 None 因熔断）
        rows = [
            json.loads(line)
            for line in (tmp_path / "out" / "quarantine.jsonl")
            .read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert rows
        for row in rows:
            dims = {s["dimension"]: s for s in row["scores"]}
            assert dims["d2"]["cause"] == CAUSE_LLM
            assert "d4" in dims and "d5" in dims and "d6" in dims

    def test_dry_run也熔断(self, tmp_path, monkeypatch):
        """⚠️ **dry-run 会真发请求**——后端挂了就等于对着死端点发几百次。
        预演的意义是「跑之前先看一眼」，不是「替真跑把失败重演一遍」。"""
        from trajectory_pipeline.evaluation.scout import cli as scout_cli

        rows = [
            {"task_id": "T001", "_p2_path": str(tmp_path / f"T001__r{i}.json")}
            for i in range(6)
        ]
        monkeypatch.setattr(scout_io, "list_p2_samples", lambda root: rows)
        monkeypatch.setattr(scout_io, "_resolve_sample",
                            lambda p2, root: _fake_sample())
        client = _FakeClient(answer=None, error="http:500")
        rc = scout_cli._dry_run(tmp_path, llm_client=client,
                                llm_failure_threshold=3)
        assert client.calls == 3          # 熔断后不再发请求
        assert rc == 1                    # 显式报「不可信」，不是安静退 0


# ════════════════════════════════════════════════════════════════════
# 5. 人工队列闸门
# ════════════════════════════════════════════════════════════════════


class Test人工队列闸门:
    def test_归因llm的记录被认出(self):
        from trajectory_pipeline.judgement_causes import llm_unanswered

        assert llm_unanswered({"undetermined_cause": CAUSE_LLM}) is True
        assert llm_unanswered({
            "scores": [{"dimension": "d2", "score": None, "cause": CAUSE_LLM}],
        }) is True
        assert llm_unanswered({
            "scores": [{"dimension": "d2", "score": None, "cause": CAUSE_NO_CLIENT}],
        }) is True

    def test_数据侧缺判与旧产物照旧推(self):
        """⚠️ **不能因为「读不出归因」就把历史产物全拦下来**——旧产物没有
        归因字段，一律当「不是后端没答」。"""
        from trajectory_pipeline.judgement_causes import llm_unanswered

        assert llm_unanswered({"undetermined_cause": CAUSE_DATA}) is False
        assert llm_unanswered({
            "scores": [{"dimension": "d2", "score": None, "cause": CAUSE_DATA}],
        }) is False
        assert llm_unanswered({"scores": [{"dimension": "d2", "score": None}]}) is False
        assert llm_unanswered({}) is False

    def test_upload跳过归因llm的轨迹(self, tmp_path):
        from trajectory_pipeline.evaluation.ls_bridge.exporter import export_batch

        record = {
            "trajectory_id": "T001__r-1",
            "task_id": "T001",
            "run_id": "r-1",
            "status": "undetermined",
            "scores": [
                {"dimension": "d2", "score": None, "is_veto": False,
                 "reasons": [], "cause": CAUSE_LLM},
                {"dimension": "d4", "score": 5, "is_veto": False,
                 "reasons": [], "cause": ""},
            ],
            "tags": [],
            "unit_outcomes": ["T001__r-1__search"],
            "evidence_supplement": {},
            "aggregation_log": [],
            "undetermined_cause": CAUSE_LLM,
        }
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        plan = export_batch(tmp_path, task_views_root=tmp_path)
        assert plan.count == 0
        assert plan.skipped and plan.skipped[0][0] == "T001__r-1"
        assert "裁判后端没答" in plan.skipped[0][1]

    def test_refresh跳过归因llm的卡(self, tmp_path):
        """⚠️ **不许拿不可信的判分盖掉 LS 上已有的好卡**——刷新会把卡
        内容换成当前产物；当前产物若因后端故障而缺判，刷新等于把好卡刷成
        「无法判定」。"""
        from trajectory_pipeline.evaluation.ls_bridge.refresh import plan_refresh

        record = {
            "trajectory_id": "T001__r-1",
            "task_id": "T001",
            "run_id": "r-1",
            "status": "undetermined",
            "scores": [
                {"dimension": "d2", "score": None, "is_veto": False,
                 "reasons": [], "cause": CAUSE_LLM},
            ],
            "tags": [],
            "unit_outcomes": ["T001__r-1__search"],
            "evidence_supplement": {},
            "aggregation_log": [],
            "undetermined_cause": CAUSE_LLM,
        }
        (tmp_path / "trajectory_candidates.jsonl").write_text(
            json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8",
        )

        class _FakeLS:
            def list_recent_tasks(self, project_id, limit=1000):
                return [{
                    "id": 7,
                    "data": {"sample_id": "T001__r-1", "messages": ["旧内容"]},
                }]

            def list_task_annotations(self, task_id):
                return []

            def list_task_predictions(self, task_id):
                return []

        plan = plan_refresh(tmp_path, client=_FakeLS(), project_id=1)
        assert plan.count == 0
        assert plan.skipped_untrusted == ("T001__r-1",)
        assert "判分不可信跳过 1" in plan.summary()


# ════════════════════════════════════════════════════════════════════
# 6. 聚合层的归因传递
# ════════════════════════════════════════════════════════════════════


class Test聚合归因传递:
    def _unit(self, cause: str) -> dict[str, Any]:
        return {
            "sample_id": "T001__r-1__search",
            "task_id": "T001",
            "run_id": "r-1",
            "status": "undetermined",
            "scores": [
                {"dimension": "d2", "score": None, "is_veto": False,
                 "reasons": [], "cause": cause},
                {"dimension": "d4", "score": 5, "is_veto": False,
                 "reasons": [], "cause": ""},
                {"dimension": "d5", "score": 5, "is_veto": False,
                 "reasons": [], "cause": ""},
                {"dimension": "d6", "score": 5, "is_veto": False,
                 "reasons": [], "cause": ""},
            ],
            "tags": [],
            "evidence_supplement": {},
            "undetermined_cause": cause,
        }

    def test_llm归因传到任务级(self):
        from trajectory_pipeline.evaluation.scout.aggregate import (
            aggregate_to_trajectory,
        )

        rec = aggregate_to_trajectory([self._unit(CAUSE_LLM)])
        assert rec["status"] == "undetermined"
        assert rec["undetermined_cause"] == CAUSE_LLM

    def test_数据归因传到任务级(self):
        from trajectory_pipeline.evaluation.scout.aggregate import (
            aggregate_to_trajectory,
        )

        rec = aggregate_to_trajectory([self._unit(CAUSE_DATA)])
        assert rec["undetermined_cause"] == CAUSE_DATA

    def test_全判出归因空串(self):
        from trajectory_pipeline.evaluation.scout.aggregate import (
            aggregate_to_trajectory,
        )

        unit = self._unit(CAUSE_SCORED)
        unit["scores"][0]["score"] = 5
        unit["status"] = "scored"
        rec = aggregate_to_trajectory([unit])
        assert rec["status"] == "scored"
        assert rec["undetermined_cause"] == CAUSE_SCORED

    def test_两栏计数分开(self, tmp_path):
        """quarantine 分两栏——「数据判不出」与「LLM 没答」处置方向相反，
        报告上必须能分开读。"""
        from trajectory_pipeline.evaluation.scout.aggregate import (
            aggregate_from_buckets,
        )

        (tmp_path / "quarantine.jsonl").write_text(
            json.dumps(self._unit(CAUSE_LLM), ensure_ascii=False) + "\n"
            + json.dumps(
                {**self._unit(CAUSE_DATA), "sample_id": "T002__r-1__search",
                 "task_id": "T002"},
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )
        _trajectories, stats = aggregate_from_buckets(
            scout_dir=tmp_path, overwrite=True,
        )
        assert stats["undetermined"] == 2
        assert stats["quarantine_llm"] == 1
        assert stats["quarantine_data"] == 1


# ════════════════════════════════════════════════════════════════════
# 7. 全量评估 CLI 的归因分栏
# ════════════════════════════════════════════════════════════════════


class Test评估CLI归因分栏:
    """``evaluation.cli`` 是另一个评估侧入口——它也报「不可判定」，
    **同一个数也回答不了「为什么」**。"""

    def _fake_sample(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            sample_id="T001__r1",
            degraded_from=(),
            schema_version="",
            outcome=SimpleNamespace(
                question="site.find_play_control", branch=None, url="a.com",
                source="human", p1_missing_fields=(),
            ),
        )

    def _verdict(self, score, cause):
        class _V:
            def __init__(self):
                self.score = score
                self.kind = "undetermined" if score is None else "scored"
                self.reasons = ("后端没答",)
                self.is_veto = False
                self.cause = cause

            def to_json(self):
                return {"dimension": "D-2", "score": self.score,
                        "kind": self.kind, "reasons": list(self.reasons),
                        "cause": self.cause}

        return _V()

    def test_不可判定按成因分栏(self, tmp_path, monkeypatch):
        from trajectory_pipeline.evaluation import cli as ecli

        (tmp_path / "T001__r1.json").write_text(
            json.dumps({"task_id": "T001", "run_id": "r1"}, ensure_ascii=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(ecli, "split_archive", lambda p: [self._fake_sample()])
        monkeypatch.setitem(
            ecli.SCORERS, "d2", lambda s: self._verdict(None, CAUSE_LLM),
        )
        summary, _rows = ecli.evaluate(tmp_path, ["d2"])
        st = summary["dimensions"]["d2"]
        assert st["undetermined"] == 1
        assert st["undetermined_by_cause"] == {CAUSE_LLM: 1}

    def test_代码判据缺判归数据侧(self, tmp_path, monkeypatch):
        """D-4/D-5/D-6 的 verdict **没有** ``cause`` 字段 ⇒ 归「数据侧」
        （事实缺失）——不然「数据判不出」那一栏会被算成空。"""
        from trajectory_pipeline.evaluation import cli as ecli

        (tmp_path / "T001__r1.json").write_text(
            json.dumps({"task_id": "T001", "run_id": "r1"}, ensure_ascii=False),
            encoding="utf-8",
        )
        monkeypatch.setattr(ecli, "split_archive", lambda p: [self._fake_sample()])

        class _V:
            score = None
            kind = "undetermined"
            reasons = ("缺观察",)
            is_veto = False

            def to_json(self):
                return {"dimension": "D-4", "score": None,
                        "kind": "undetermined", "reasons": ["缺观察"]}

        monkeypatch.setitem(ecli.SCORERS, "d4", lambda s: _V())
        summary, _rows = ecli.evaluate(tmp_path, ["d4"])
        assert summary["dimensions"]["d4"]["undetermined_by_cause"] == {
            CAUSE_DATA: 1,
        }
