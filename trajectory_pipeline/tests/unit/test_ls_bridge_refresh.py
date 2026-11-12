"""``ls_bridge refresh`` 的单测（台账 §H-34）。

盯住三条**写死在实现里的纪律**（不是文档里的建议）：

1. **绝不 PATCH ``label_config``** —— 那会覆盖标注员在 LS 端的改动；
   ⇒ 假 client 断言**没有任何 projects 端点的调用**。
2. **默认只刷没有人工标注的** —— ``PATCH data`` 会不会影响 annotation
   **没验过** ⇒ 有标注的默认**跳过并报出来**，要刷得显式 ``include_annotated``。
3. **保留旧卡的 ``uploaded_at``** —— 它是「什么时候上的 LS」的事实；
   且比对时**不比它**（每次 push 都变，拿它比会把「没变」误报成「有差异」）。

外加：默认 **dry-run**（不写盘）、**只刷有差异的**、一条失败不打断其余。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_pipeline.evaluation.ls_bridge.refresh import (
    VOLATILE_KEYS,
    apply_refresh,
    diff_fields,
    plan_refresh,
)


# ── 假 client ─────────────────────────────────────────────────────


class FakeClient:
    """记录全部调用；可让某个 task 的写入抛异常。"""

    def __init__(
        self,
        *,
        tasks: dict[str, dict[str, Any]] | None = None,
        annotations: dict[int, list[dict[str, Any]]] | None = None,
        predictions: dict[int, list[dict[str, Any]]] | None = None,
        fail_task_ids: set[int] | None = None,
    ) -> None:
        self.tasks = tasks or {}
        self.annotations = annotations or {}
        self.predictions = predictions or {}
        self.fail_task_ids = fail_task_ids or set()
        self.calls: list[tuple[str, Any]] = []

    # -- 读侧 --
    def list_recent_tasks(self, project_id: int, *, limit: int = 100) -> list[dict]:
        return list(self.tasks.values())

    def list_task_annotations(self, task_id: int) -> list[dict]:
        return list(self.annotations.get(int(task_id), []))

    def list_task_predictions(self, task_id: int) -> list[dict]:
        return list(self.predictions.get(int(task_id), []))

    # -- 写侧 --
    def update_task_data(self, task_id: int, data: Any) -> dict:
        self.calls.append(("update_task_data", int(task_id)))
        if int(task_id) in self.fail_task_ids:
            raise RuntimeError("boom")
        # ⚠️ **真写回**：LS 接受 PATCH 后那张卡的内容就变了 ⇒
        # 「刷完再计划一次」必须报无差异（否则这条断言测的是假 client 的缺陷）。
        for task in self.tasks.values():
            if int(task.get("id", -1)) == int(task_id):
                task["data"] = dict(data)
                break
        return {}

    def update_prediction(self, prediction_id: int, **kw: Any) -> dict:
        self.calls.append(("update_prediction", int(prediction_id)))
        return {}


# ── 测试脚手架 ───────────────────────────────────────────────────


def _dim(dim: str, score: int | None) -> dict:
    return {"dimension": dim, "score": score, "is_veto": False, "reasons": []}


def _record(tid: str = "T001__r-1") -> dict:
    return {
        "trajectory_id": tid, "task_id": "T001", "run_id": "r-1",
        "status": "scored",
        "scores": [_dim("d2", 4), _dim("d4", 5), _dim("d5", 4), _dim("d6", 5)],
        "tags": [], "unit_outcomes": [], "evidence_supplement": {},
        "aggregation_log": [],
    }


def _write_candidates(scout_dir: Path, *records: dict) -> None:
    scout_dir.mkdir(parents=True, exist_ok=True)
    (scout_dir / "trajectory_candidates.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


def _write_t_view(root: Path, tid: str, *, body: str = "hi") -> None:
    from trajectory_pipeline.storage import task_views_dir
    tdir = task_views_dir(root)
    tdir.mkdir(parents=True, exist_ok=True)
    msgs = [{"role": "user", "content": body}, {"role": "assistant", "content": "ok"}]
    (tdir / f"{tid}.json").write_text(
        json.dumps({
            "messages": msgs,
            "openai": {"messages": msgs},
            "meta": {"task_id": "T001", "run_id": "r-1"},
        }, ensure_ascii=False),
        encoding="utf-8",
    )


@pytest.fixture()
def scene(tmp_path: Path) -> tuple[Path, Path, FakeClient]:
    """scout 目录 + T 视图根 + 假 client（LS 上已有一张内容**过时**的卡）。"""
    scout_dir = tmp_path / "scout"
    _write_candidates(scout_dir, _record())
    views_root = tmp_path / "pipeline"
    _write_t_view(views_root, "T001__r-1", body="新内容")
    client = FakeClient(
        tasks={
            "T001__r-1": {
                "id": 591,
                "data": {
                    "sample_id": "T001__r-1", "task_id": "T001", "run_id": "r-1",
                    "status": "quarantine", "tags": [],
                    "messages_view_raw": [{"role": "user", "content": "旧内容"}],
                    "uploaded_at": "2026-10-19T09:58:00+00:00",
                },
            },
        },
        predictions={591: [{"id": 700, "result": [], "score": 0.5}]},
    )
    return scout_dir, views_root, client


# ── TestDiffFields ──────────────────────────────────────────────


class TestDiffFields:
    """比对：全量 + 跳过易变键。"""

    def test_只比差异字段名(self):
        old = {"a": 1, "b": 2}
        new = {"a": 1, "b": 3, "c": 4}
        assert diff_fields(old, new) == ("b", "c")

    def test_uploaded_at_不比(self):
        """每次 push 都变 ⇒ 拿它比会把「内容没变」误报成「有差异」。"""
        old = {"a": 1, "uploaded_at": "2026-10-19"}
        new = {"a": 1, "uploaded_at": "2026-11-12"}
        assert diff_fields(old, new) == ()
        assert "uploaded_at" in VOLATILE_KEYS

    def test_缺键算差异(self):
        assert diff_fields({"a": 1}, {}) == ("a",)
        assert diff_fields({}, {"a": 1}) == ("a",)


# ── TestPlanRefresh ─────────────────────────────────────────────


class TestPlanRefresh:
    """计划阶段：只算不写。"""

    def test_有差异_进计划(self, scene):
        scout_dir, views_root, client = scene
        plan = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
        )
        assert plan.count == 1
        assert plan.items[0].ls_task_id == 591
        assert "messages_view_raw" in plan.items[0].diff_fields
        assert not client.calls, "计划阶段不许写"

    def test_内容一致_归unchanged(self, scene):
        scout_dir, views_root, client = scene
        plan = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
        )
        # 先把 LS 上的卡刷成与当前渲染一致 → 二次计划应报无差异
        item = plan.items[0]
        client.update_task_data(591, item.new_data)
        client.calls.clear()
        plan2 = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
        )
        assert plan2.count == 0
        assert plan2.unchanged == ("T001__r-1",)

    def test_LS上没有_归missing(self, scene):
        scout_dir, views_root, client = scene
        client.tasks = {}
        plan = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
        )
        assert plan.missing == ("T001__r-1",)
        assert plan.count == 0

    def test_有人工标注_默认跳过并报出(self, scene):
        """纪律 2：PATCH data 会不会影响 annotation 没验过 ⇒ 不猜。"""
        scout_dir, views_root, client = scene
        client.annotations[591] = [{"id": 1, "result": []}]
        plan = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
        )
        assert plan.count == 0
        assert plan.skipped_annotated == ("T001__r-1",)

    def test_显式include_annotated_才刷(self, scene):
        scout_dir, views_root, client = scene
        client.annotations[591] = [{"id": 1, "result": []}]
        plan = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
            include_annotated=True,
        )
        assert plan.count == 1
        assert plan.skipped_annotated == ()

    def test_保留旧uploaded_at(self, scene):
        """纪律 3：刷新不改「什么时候上的 LS」。"""
        scout_dir, views_root, client = scene
        plan = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
        )
        assert plan.items[0].new_data["uploaded_at"] == "2026-10-19T09:58:00+00:00"

    def test_带出预标注id(self, scene):
        scout_dir, views_root, client = scene
        plan = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
        )
        assert plan.items[0].prediction_id == 700
        assert plan.items[0].new_prediction is not None

    def test_桶过滤(self, scene):
        """--buckets 之外的候选不进计划。"""
        scout_dir, views_root, client = scene
        plan = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
            buckets=("negative",),
        )
        assert plan.count == 0


# ── TestApplyRefresh ────────────────────────────────────────────


class TestApplyRefresh:
    """写阶段：只 PATCH 两处，逐条独立。"""

    def test_刷task与prediction(self, scene):
        scout_dir, views_root, client = scene
        plan = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
        )
        result = apply_refresh(plan, client=client)
        assert result["tasks_patched"] == 1
        assert result["predictions_patched"] == 1
        assert not result["failed"]
        assert ("update_task_data", 591) in client.calls
        assert ("update_prediction", 700) in client.calls

    def test_一条失败不打断其余(self, scene):
        scout_dir, views_root, client = scene
        client.fail_task_ids = {591}
        plan = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
        )
        result = apply_refresh(plan, client=client)
        assert result["tasks_patched"] == 0
        assert len(result["failed"]) == 1

    def test_绝不碰label_config(self, scene):
        """纪律 1：假 client 上没有 projects 端点 ⇒ 一调就 AttributeError。"""
        scout_dir, views_root, client = scene
        plan = plan_refresh(
            scout_dir, client=client, task_views_root=views_root, project_id=77,
        )
        apply_refresh(plan, client=client)
        kinds = {c[0] for c in client.calls}
        assert kinds <= {"update_task_data", "update_prediction"}


# ── TestRefreshCli ──────────────────────────────────────────────


class TestRefreshCli:
    """CLI 层：默认 dry-run（不写盘）。"""

    def _ns(self, **overrides: Any):
        base = {
            "url": None, "api_key": None, "command": "refresh",
            "scout_dir": None, "task_views_root": None,
            "buckets": "positive,negative,quarantine",
            "limit": None, "include_annotated": False, "apply": False,
        }
        base.update(overrides)
        return type("Args", (), base)()

    def test_默认dry_run不写(self, scene, monkeypatch):
        from trajectory_pipeline.evaluation.ls_bridge import cli as cli_mod
        scout_dir, views_root, client = scene
        monkeypatch.setattr(cli_mod, "load_settings", lambda **kw: _FakeSettings())
        monkeypatch.setattr(cli_mod, "build_client", lambda s: client, raising=False)
        monkeypatch.setattr(
            "trajectory_pipeline.evaluation.ls_bridge.client.build_client",
            lambda s: client,
        )
        monkeypatch.setattr(
            "trajectory_pipeline.evaluation.ls_bridge.project_manager.resolve_project_id",
            lambda c, s, **kw: 77,
        )
        ns = self._ns(scout_dir=str(scout_dir), task_views_root=str(views_root))
        rc = cli_mod._cmd_refresh(ns)
        assert rc == 0
        assert not client.calls, "默认不许写"

    def test_缺scout_dir_返2(self):
        from trajectory_pipeline.evaluation.ls_bridge import cli as cli_mod
        ns = self._ns(scout_dir="/nonexistent/dir")
        assert cli_mod._cmd_refresh(ns) == 2

    def test_空buckets_返2(self, scene):
        from trajectory_pipeline.evaluation.ls_bridge import cli as cli_mod
        scout_dir, views_root, _ = scene
        ns = self._ns(scout_dir=str(scout_dir), buckets="  ")
        assert cli_mod._cmd_refresh(ns) == 2


class _FakeSettings:
    """只带 refresh 用得到的两个方法。"""

    def has_credentials(self) -> bool:
        return True
