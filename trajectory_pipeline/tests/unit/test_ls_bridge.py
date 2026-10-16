"""ls_bridge 单元测试:settings / push_index / scorecard / scan / exporter。

不触网 —— client 只测纯函数（``_accepted_count`` 的诚实 0 语义）,
推送链路用 fake client。真实 LS 交互在 ``tests/integration``（marker
``integration``, 默认不跑）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.evaluation.ls_bridge import errors as ls_errors
from trajectory_pipeline.evaluation.ls_bridge.exporter import (
    build_prediction,
    build_task_data,
    export_batch,
    export_one,
    push_batch,
    read_scout_records,
)
from trajectory_pipeline.evaluation.ls_bridge.push_index import PushIndex, PushRecord
from trajectory_pipeline.evaluation.ls_bridge.scan import scan_for_credentials
from trajectory_pipeline.evaluation.ls_bridge.scorecard import (
    NEGATIVE_SCORE_THRESHOLD,
    build_risk_hints,
    build_scorecard,
    derive_overall,
    render_risk_hints,
)
from trajectory_pipeline.evaluation.ls_bridge.settings import (
    CredentialScanSettings,
    LSSettings,
    SettingsError,
    load_settings,
)


# ── fixtures ───────────────────────────────────────────────────────


def _dim(dim: str, score: int | None, *, veto: bool = False, reasons=None):
    return {
        "dimension": dim,
        "score": score,
        "is_veto": veto,
        "reasons": list(reasons or []),
    }


def _record(sample_id: str = "s-001", *, scores=None, tags=None, status="scored"):
    return {
        "sample_id": sample_id,
        "task_id": "T001",
        "run_id": "r-1",
        "status": status,
        "scores": scores or [
            _dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5),
        ],
        "tags": list(tags or []),
        "evidence_supplement": {},
    }


@pytest.fixture()
def views_root(tmp_path: Path) -> Path:
    """三份 P3 视图齐全的 root（``read_views`` 要求 ``<root>/views/``）。"""
    views = tmp_path / "views"
    views.mkdir()
    (views / "s-001_messages.json").write_text(
        json.dumps([
            {"role": "user", "content": "播放这首歌"},
            {"role": "assistant", "content": "好的,正在播放。"},
        ]),
        encoding="utf-8",
    )
    (views / "s-001_openai.json").write_text(
        json.dumps([
            {"role": "user", "content": "播放这首歌"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "play", "arguments": '{"ref": "btn-1"}'}}
            ]},
        ]),
        encoding="utf-8",
    )
    (views / "s-001_meta.json").write_text(
        json.dumps({"gate": "pass", "step_count": 2, "degraded_from": None}),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def scout_dir(tmp_path: Path) -> Path:
    """含 positive 1 条 + negative 1 条的 scout 产物目录。"""
    positive = _record("s-001")
    negative = _record(
        "s-002",
        scores=[_dim("d2", 4), _dim("d4", 1, reasons=["组件验证失败"]),
                _dim("d5", 4), _dim("d6", 5)],
        tags=["d4_failed"],
    )
    quarantine = _record(
        "s-003",
        scores=[_dim("d2", None, reasons=["没有模型可见的 click 动作"]),
                _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
    )
    (tmp_path / "positive_candidates.jsonl").write_text(
        json.dumps(positive, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (tmp_path / "negative_candidates.jsonl").write_text(
        json.dumps(negative, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (tmp_path / "quarantine.jsonl").write_text(
        json.dumps(quarantine, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return tmp_path


# ── settings ───────────────────────────────────────────────────────


class TestSettings:

    def test_环境变量读取与覆盖(self, monkeypatch):
        monkeypatch.setenv("TRAJECTORY_LS_URL", "http://ls.example:8099")
        monkeypatch.setenv("TRAJECTORY_LS_API_KEY", "k" * 40)
        monkeypatch.setenv("TRAJECTORY_LS_PROJECT_ID", "7")
        settings = load_settings()
        assert settings.base_url == "http://ls.example:8099"
        assert settings.resolve_api_key() == "k" * 40
        assert settings.project_id == 7

        override = load_settings(base_url="http://other:1", project_id=9)
        assert override.base_url == "http://other:1"
        assert override.project_id == 9

    def test_零配置回落默认(self, monkeypatch):
        for name in ("TRAJECTORY_LS_URL", "TRAJECTORY_LS_API_KEY",
                     "TRAJECTORY_LS_PROJECT_ID", "TRAJECTORY_LS_ENABLED"):
            monkeypatch.delenv(name, raising=False)
        settings = load_settings()
        assert settings.base_url  # 默认非空
        assert settings.resolve_api_key() is None
        assert settings.project_id is None

    def test_坏project_id报错(self, monkeypatch):
        monkeypatch.setenv("TRAJECTORY_LS_PROJECT_ID", "abc")
        with pytest.raises(SettingsError):
            load_settings()

    def test_空白base_url拒绝(self):
        with pytest.raises(SettingsError):
            LSSettings(base_url="   ")

    def test_非法batch_size拒绝(self):
        from trajectory_pipeline.evaluation.ls_bridge.settings import UploadSettings
        with pytest.raises(SettingsError):
            UploadSettings(batch_size=0)


# ── push_index ─────────────────────────────────────────────────────


class TestPushIndex:

    def test_判重与追加(self, tmp_path):
        path = tmp_path / "push_index__1.jsonl"
        index = PushIndex.load(path)
        assert not index.has("s-001")
        index.record("s-001", 42, task_ref="T001")
        assert index.has("s-001")
        assert index.task_id("s-001") == 42

        # 重读:落盘的记录还在
        reloaded = PushIndex.load(path)
        assert reloaded.has("s-001")
        assert reloaded.task_id("s-001") == 42

    def test_坏行跳过不抛(self, tmp_path):
        path = tmp_path / "push_index__1.jsonl"
        path.write_text(
            "{not json}\n"
            + json.dumps({"sample_id": "s-002", "ls_task_id": 7}) + "\n"
            + json.dumps({"sample_id": "", "ls_task_id": 8}) + "\n",
            encoding="utf-8",
        )
        index = PushIndex.load(path)
        assert index.has("s-002")
        assert len(index) == 1

    def test_追加不改写已有内容(self, tmp_path):
        path = tmp_path / "push_index__1.jsonl"
        index = PushIndex.load(path)
        index.record("s-001", 1)
        first = path.read_text(encoding="utf-8")
        index.record("s-002", 2)
        second = path.read_text(encoding="utf-8")
        assert second.startswith(first)  # 纯追加
        assert len(second.strip().splitlines()) == 2

    def test_PushRecord坏形态拒绝(self):
        assert PushRecord.from_dict("not a dict") is None
        assert PushRecord.from_dict({"sample_id": "s", "ls_task_id": "x"}) is None


# ── scorecard ──────────────────────────────────────────────────────


class TestScorecard:

    def test_全过_正向(self):
        overall = derive_overall(_record())
        assert overall["kind"] == "positive"
        assert overall["confidence"] == "high"

    def test_veto优先于低分与缺判(self):
        rec = _record(scores=[_dim("d2", None, veto=True), _dim("d4", 5),
                              _dim("d5", 4), _dim("d6", 5)])
        overall = derive_overall(rec)
        assert overall["kind"] == "negative"
        assert "d2" in overall["reason"]

    def test_低分负向(self):
        rec = _record(scores=[_dim("d2", 2), _dim("d4", 5),
                              _dim("d5", 4), _dim("d6", 5)])
        assert derive_overall(rec)["kind"] == "negative"

    def test_缺判低置信度(self):
        rec = _record(scores=[_dim("d2", None), _dim("d4", 5),
                              _dim("d5", 4), _dim("d6", 5)])
        overall = derive_overall(rec)
        assert overall["kind"] == "undetermined"
        assert overall["confidence"] == "low"

    def test_score为0判了零分不是缺判(self):
        rec = _record(scores=[_dim("d2", 0), _dim("d4", 5),
                              _dim("d5", 4), _dim("d6", 5)])
        overall = derive_overall(rec)
        assert overall["kind"] == "negative"  # 0 <= 2

    def test_结构与source标注(self):
        card = build_scorecard(_record(), classify_kind="positive")
        assert card["schema_version"] == "scorecard.v2"
        assert card["scout_bucket"] == "positive"
        assert len(card["dimensions"]) == 4
        assert all(d["source"] == "measured" for d in card["dimensions"])
        assert card["dimension_summary"] == {"total": 4, "measured": 4, "missing": 0}

        missing = build_scorecard(
            _record(scores=[_dim("d2", None), _dim("d4", 5),
                            _dim("d5", 4), _dim("d6", 5)])
        )
        assert missing["dimensions"][0]["source"] == "missing"

    def test_风险提示_只提示不预判(self):
        card = build_scorecard(_record())
        hints = build_risk_hints(card)
        assert hints == ["机器未发现风险信号; 仍需人工确认。"]
        assert "accept" not in render_risk_hints(card)

        bad = build_scorecard(_record(
            scores=[_dim("d2", 1, reasons=["观察与动作不一致"]),
                    _dim("d4", 5), _dim("d5", 4, veto=True, reasons=["决策错误"]),
                    _dim("d6", 5)],
            tags=["d5_failed", "veto"],
        ))
        hints = build_risk_hints(bad)
        assert any("d2" in h for h in hints)
        assert any("VETO" in h or "否决" in h for h in hints)


# ── scan ───────────────────────────────────────────────────────────


class TestScan:

    def test_命中并只记位置(self):
        hits = scan_for_credentials(
            {"content": "password=supersecret123"}, view="messages_view_raw",
            settings=CredentialScanSettings(),
        )
        assert hits
        assert all("supersecret" not in h.describe() for h in hits)

    def test_干净内容无命中(self):
        assert scan_for_credentials(
            {"content": "正常内容"}, view="meta_view_raw",
            settings=CredentialScanSettings(),
        ) == []

    def test_关闭即不扫(self):
        assert scan_for_credentials(
            {"content": "sk-" + "a" * 20}, view="x",
            settings=CredentialScanSettings(enabled=False),
        ) == []


# ── exporter ───────────────────────────────────────────────────────


class TestExporter:

    def test_读scout三件套(self, scout_dir):
        records = read_scout_records(scout_dir)
        assert [b for b, _ in records] == ["positive", "negative"]

    def test_默认三态全读(self, scout_dir):
        records = read_scout_records(scout_dir, ("positive", "negative", "quarantine"))
        assert [b for b, _ in records] == ["positive", "negative", "quarantine"]

    def test_未知bucket报错(self, scout_dir):
        with pytest.raises(ValueError):
            read_scout_records(scout_dir, buckets=("unknown",))

    def test_视图齐全_导出task与prediction(self, views_root):
        task, prediction, hits = export_one(_record(), "positive", views_root=views_root)
        assert task is not None and not hits
        data = task["data"]
        assert data["sample_id"] == "s-001"
        assert data["status"] == "positive"
        assert "播放这首歌" in data["messages_text"]
        assert "play" in data["openai_text"]
        assert "gate: pass" in data["meta_text"]
        assert "inner_id" not in task  # LS 1.23 字符串 inner_id 不去重, 不发

        assert prediction is not None
        assert prediction["result"][0]["from_name"] == "risk_hints"
        assert prediction["task"] == "s-001"  # 占位, push 时换数字 id

    def test_视图缺失_返回None(self, tmp_path):
        empty = tmp_path / "views"
        empty.mkdir()
        task, prediction, hits = export_one(_record(), "positive", views_root=tmp_path)
        assert task is None and prediction is None and not hits

    def test_凭据命中_拒推(self, views_root):
        rec = _record()
        # 把凭据形态塞进 evidence 之外的字段: 直接构造带泄密的 openai 视图
        bad_views = views_root / "views"
        (bad_views / "s-001_openai.json").write_text(
            json.dumps([{"role": "assistant", "content": "key is sk-" + "a" * 20}]),
            encoding="utf-8",
        )
        task, prediction, hits = export_one(rec, "positive", views_root=views_root)
        assert task is None and hits

    def test_export_batch_缺视图进skipped(self, scout_dir, tmp_path):
        # 只有 s-001 的视图, s-002 缺 → skipped
        views = tmp_path / "views"
        views.mkdir()
        for name in ("messages", "openai", "meta"):
            (views / f"s-001_{name}.json").write_text("{}", encoding="utf-8")
        plan = export_batch(scout_dir, views_root=tmp_path)
        assert plan.count == 1
        assert [sid for sid, _ in plan.skipped] == ["s-002"]

    def test_export_batch_negative带劣质标记(self, scout_dir, views_root):
        # 给 s-002 也补齐视图
        views = views_root / "views"
        for name in ("messages", "openai", "meta"):
            (views / f"s-002_{name}.json").write_text("{}", encoding="utf-8")
        plan = export_batch(scout_dir, views_root=views_root)
        assert plan.count == 2
        neg = next(t for t in plan.tasks if t["data"]["status"] == "negative")
        assert "d4_failed" in neg["data"]["tags"]
        assert "组件验证失败" in neg["data"]["audit_text"]
        assert "劣质" in neg["data"]["audit_text"]

    def test_quarantine样本_缺判提示人工裁决(self, views_root):
        from trajectory_pipeline.evaluation.ls_bridge.exporter import build_task
        rec = _record(
            scores=[_dim("d2", None, reasons=["没有模型可见的 click 动作"]),
                    _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
        )
        views = {"messages": [{"role": "user", "content": "hi"}],
                 "openai": [], "meta": {}}
        data, _card = build_task_data(rec, "quarantine", views)
        task = build_task(data)
        assert task["data"]["status"] == "quarantine"
        assert "无法判定" in task["data"]["audit_text"]
        assert "缺判" in task["data"]["audit_text"]
        assert "d2: 缺判" in task["data"]["audit_text"]

    def test_positive样本_开场行合格(self, views_root):
        views = {"messages": [], "openai": [], "meta": {}}
        data, _card = build_task_data(_record(), "positive", views)
        assert data["audit_text"].startswith("机器判定: 合格")

    def test_build_task_data的扫描字段在渲染前(self, views_root):
        record = _record()
        views = {
            "messages": [{"role": "user", "content": "hi"}],
            "openai": [], "meta": {},
        }
        data, card = build_task_data(record, "positive", views)
        assert data["messages_view_raw"] == views["messages"]
        assert card["schema_version"] == "scorecard.v2"

    def test_prediction_禁用scorecard时为None(self):
        assert build_prediction({"sample_id": "s"}, None) is None
        assert build_prediction({"sample_id": "s"}, {"enabled": False}) is None


class _FakeClient:
    """push_batch 的假 LS: import 后回捞 id、记推送次数。"""

    def __init__(self):
        self.imported: list[dict] = []
        self.predictions: list[dict] = []
        self._next_id = 100

    def import_tasks(self, project_id, tasks):
        batch = list(tasks)
        self.imported.extend(batch)
        return len(batch)

    def list_recent_tasks(self, project_id, *, limit):
        out = []
        for task in self.imported[-limit:]:
            out.append({"id": self._next_id,
                        "data": task["data"]})
            self._next_id += 1
        return out

    def import_predictions(self, project_id, predictions):
        batch = list(predictions)
        self.predictions.extend(batch)
        return len(batch)


class TestPushBatch:

    def _plan(self, views_root):
        # views_root fixture 只有 s-001 的视图, 这里补齐 s-002
        views = views_root / "views"
        for name in ("messages", "openai", "meta"):
            target = views / f"s-002_{name}.json"
            if not target.exists():
                target.write_text("{}", encoding="utf-8")
        records = [("positive", _record("s-001")), ("negative", _record("s-002"))]
        plan_tasks = []
        plan_preds = []
        from trajectory_pipeline.evaluation.ls_bridge.exporter import build_task
        for bucket, rec in records:
            task, pred, hits = export_one(rec, bucket, views_root=views_root)
            assert task is not None and not hits
            plan_tasks.append(task)
            if pred is not None:
                plan_preds.append(pred)
        from trajectory_pipeline.evaluation.ls_bridge.exporter import ExportPlan
        return ExportPlan(tasks=plan_tasks, predictions=plan_preds)

    def test_推送_台账_预标注换数字id(self, views_root, tmp_path):
        plan = self._plan(views_root)
        client = _FakeClient()
        index = PushIndex(tmp_path / "idx.jsonl")
        settings = LSSettings()
        result = push_batch(plan, settings=settings, project_id=1,
                            client=client, index=index)
        assert result["tasks_pushed"] == 2
        assert result["predictions_pushed"] == 2
        assert all(isinstance(p["task"], int) for p in client.predictions)
        assert index.has("s-001") and index.has("s-002")

    def test_台账判重_不推重复(self, views_root, tmp_path):
        plan = self._plan(views_root)
        client = _FakeClient()
        index = PushIndex(tmp_path / "idx.jsonl")
        settings = LSSettings()
        push_batch(plan, settings=settings, project_id=1, client=client, index=index)
        again = push_batch(plan, settings=settings, project_id=1,
                           client=client, index=index)
        assert again["tasks_pushed"] == 0
        assert again["skipped_duplicate"] == 2

    def test_空plan直接返回(self):
        from trajectory_pipeline.evaluation.ls_bridge.exporter import ExportPlan
        result = push_batch(ExportPlan(), settings=LSSettings(), project_id=1)
        assert result["tasks_pushed"] == 0


# ── client 纯函数 ──────────────────────────────────────────────────


class TestAcceptedCount:

    def test_诚实0不被覆盖(self):
        from trajectory_pipeline.evaluation.ls_bridge.client import _accepted_count
        # predictions 对格式不对的预测回 {"created": 0} —— 0 是真的 0
        assert _accepted_count({"created": 0}, sent=1) == 0
        assert _accepted_count({"task_count": 3}, sent=5) == 3
        # 服务端没给计数 → 按发送数兜底
        assert _accepted_count({}, sent=4) == 4
        assert _accepted_count(None, sent=2) == 2

    def test_redact脱敏(self):
        assert "sk-" + "a" * 20 not in ls_errors.redact(f"key=sk-{'a' * 20}")
        assert "secret" not in ls_errors.redact("password=secret")


# ── 阈值口径 ───────────────────────────────────────────────────────


def test_负例阈值与scout一致():
    from trajectory_pipeline.evaluation.scout.policy import (
        NEGATIVE_SCORE_THRESHOLD as SCOUT_THRESHOLD,
    )
    assert NEGATIVE_SCORE_THRESHOLD == SCOUT_THRESHOLD
