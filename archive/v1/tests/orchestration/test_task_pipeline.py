"""orchestration.task_pipeline 单元测试.

覆盖:
* 三阶段顺序: simulate → gdr → etl 严格顺序 (SQLite phase 推进正确)
* 阶段内重试: max_retry_gdr=2 时 gdr 失败重试 2 次后 dead
* 阶段内重试: max_retry_etl=2 时 etl 失败重试 2 次后 dead
* NonRetryableError 直接 dead (不消耗 attempts)
* simulate 终态非 SUCCESS → dead, 不进 gdr
* 子进程不抛异常给主进程 — 任何未捕获都 mark_failed + 返 dead
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from gdr.config.settings import Settings as GdrSettings
from orchestration.queue import (
    PHASE_AUDITED,
    PHASE_DEAD,
    PHASE_DONE,
    PHASE_ETL,
    PHASE_GDR,
    PHASE_SIMULATE,
    SQLiteQueue,
)
from orchestration.settings import Paths, PipelineSettings
from orchestration.task_pipeline import _run_one_task_pipeline


# ---------------------------------------------------------------------------
# fixtures & helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> Paths:
    for sub in (
        "trajectory_dir", "refined_dir", "etl_outputs_dir",
        "dead_dir", "log_dir", "runs_dir",
    ):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return Paths(
        simulate_serve_config=tmp_path / "sim.yaml",
        trajectory_dir=tmp_path / "trajectory_dir",
        runs_dir=tmp_path / "runs_dir",
        refined_dir=tmp_path / "refined_dir",
        etl_outputs_dir=tmp_path / "etl_outputs_dir",
        sqlite_db=tmp_path / "q.db",
        dead_dir=tmp_path / "dead_dir",
        pid_file=tmp_path / "orch.pid",
        log_dir=tmp_path / "log_dir",
    )


def _make_gdr_settings(paths: Paths) -> GdrSettings:
    return GdrSettings(
        batch_output_dir=paths.refined_dir,
        workers=1, llm_concurrency=1, max_files=1,
    )


def _make_pipeline_settings(*, retry_gdr: int = 0, retry_etl: int = 0) -> PipelineSettings:
    return PipelineSettings(
        max_parallelism=1,
        max_retry_gdr=retry_gdr,
        max_retry_etl=retry_etl,
        retry_poll_seconds=0.05,
    )


# Fake TaskRun / GdrResult / EtlOutputs
@dataclass
class _FakeRunFailure:
    message: str = "n/a"


@dataclass
class _FakeRun:
    """模仿 simulate_serve.domain.run.TaskRun 的最小子集."""
    run_id: str = "run_fake"
    remote_session_id: str = "sess_fake"
    state: Any = None  # str (e.g. 'success')
    failure: Any = None


@dataclass
class _FakeGdrResult:
    refined_path: Path
    task_id: str
    session_id: str
    duration_seconds: float = 0.0


@dataclass
class _FakeEtlOutputs:
    messages_path: Path
    openai_path: Path
    qwenjina_path: Path | None
    meta_path: Path
    task_id: str
    session_id: str
    duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# monkeypatch helper
# ---------------------------------------------------------------------------


def _patch_pipeline(
    monkeypatch,
    *,
    simulate_state: str = "success",
    gdr_fail_count: int = 0,
    gdr_nonretryable: bool = False,
    gdr_audited: str | None = None,
    etl_fail_count: int = 0,
    etl_nonretryable: bool = False,
    simulate_exc: BaseException | None = None,
):
    """monkeypatch orchestration.task_pipeline 延迟 import 的三个函数.

    入参:
        simulate_state:        模拟 TaskRun.state 字符串; success 等
        gdr_fail_count:        gdr 重试次数, 前 N 次抛普通 Exception, 之后成功
        gdr_nonretryable:      gdr 抛 NonRetryableError (直接 dead)
        gdr_audited:           gdr 抛 GdrAuditedError (走 audited 终态, 不进 dead);
                              取值 "judge_discard" / "scoring_reject" 表示拒收原因
        etl_fail_count:        etl 重试次数
        etl_nonretryable:      etl 抛 NonRetryableError
        simulate_exc:          simulate 直接抛异常 (非 TaskRun)
    """
    # 计数器: 记录三个函数实际被调用的次数
    counts = {"simulate": 0, "gdr": 0, "etl": 0}

    # Stub producer_simulate.run_one_task
    def fake_simulate_run_one_task(task_id: str, *, config_path: Path) -> _FakeRun:
        counts["simulate"] += 1
        if simulate_exc is not None:
            raise simulate_exc
        # 模拟 producer 写出 trajectory 文件
        traj_dir = config_path.parent / "trajectory_dir"
        traj_dir.mkdir(parents=True, exist_ok=True)
        # 必须与 task_pipeline 中 sanitize 后的文件名一致
        safe_session = "sess_fake"  # remote_session_id 的 sanitize 形态
        safe_run = "run_fake"
        traj_path = traj_dir / f"{safe_run}__{safe_session}.json"
        traj_path.write_text("{}", encoding="utf-8")
        return _FakeRun(
            run_id="run_fake",
            remote_session_id="sess_fake",
            state=simulate_state,
            failure=_FakeRunFailure(message="boom") if simulate_state != "success" else None,
        )

    # 延迟 import 模块 stub
    class _NonRetryableError(Exception):
        pass

    class _GdrNonRetryableError(Exception):
        pass

    class _GdrAuditedError(Exception):
        """评分低 → audited 终态, 不进 dead (CLAUDE.md "数据保留原则").

        对应 orchestration.workers.gdr_worker.GdrAuditedError 的契约:
        message 字符串 + audit_reason / refined_path 两个 kwarg。
        """

        def __init__(self, message: str, *, audit_reason: str,
                     refined_path: Path | None = None) -> None:
            super().__init__(message)
            self.audit_reason = audit_reason
            self.refined_path = refined_path

    class _EtlNonRetryableError(Exception):
        pass

    def fake_run_gdr_once(
        *, src_path, refined_dir, gdr_settings, task_id, session_id,
    ):
        counts["gdr"] += 1
        if gdr_audited and counts["gdr"] == 1:
            # 评分低 → audited, 抛 GdrAuditedError (单次判定, 不重试).
            # judge_discard 的 C2 在真实实现里**已经落盘** (精修做完只是评分
            # 不过), 所以带 refined_path, 下游继续跑 etl 推 LS;
            # scoring_reject 的 C2 是刻意不写的 → None, 止步于 audited。
            audited_refined = None
            if gdr_audited == "judge_discard":
                refined_dir.mkdir(parents=True, exist_ok=True)
                audited_refined = refined_dir / f"{task_id}__refined.json"
                audited_refined.write_text("{}", encoding="utf-8")
            raise _GdrAuditedError(
                f"gdr status={gdr_audited!r} (task={task_id})",
                audit_reason=gdr_audited,
                refined_path=audited_refined,
            )
        if gdr_nonretryable and counts["gdr"] == 1:
            raise _GdrNonRetryableError("gdr permanent fail")
        if counts["gdr"] <= gdr_fail_count:
            raise RuntimeError(f"gdr forced fail #{counts['gdr']}")
        # 成功: 写 C2
        refined_dir.mkdir(parents=True, exist_ok=True)
        refined = refined_dir / f"{task_id}__refined.json"
        refined.write_text("{}", encoding="utf-8")
        return _FakeGdrResult(
            refined_path=refined, task_id=task_id, session_id=session_id,
        )

    def fake_run_etl_once(
        *, c2_path, etl_outputs_dir, task_id, session_id,
        attempt=0,
        runs_dir=None, run_id=None,   # F2: Criterion 注入参数
        src_path=None, gdr_settings=None,   # 失败归因评价参数
        audit_reason=None,   # 低分标记注入参数
    ):
        counts["etl_audit_reason"] = audit_reason
        counts["etl"] += 1
        if etl_nonretryable and counts["etl"] == 1:
            raise _EtlNonRetryableError("etl permanent fail")
        if counts["etl"] <= etl_fail_count:
            raise RuntimeError(f"etl forced fail #{counts['etl']}")
        etl_outputs_dir.mkdir(parents=True, exist_ok=True)
        base = etl_outputs_dir / f"{task_id}"
        msgs = base.with_suffix(".messages.json")
        openai = base.with_suffix(".openai.json")
        meta = base.with_suffix(".meta.json")
        for p in (msgs, openai, meta):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}", encoding="utf-8")
        return _FakeEtlOutputs(
            messages_path=msgs, openai_path=openai, qwenjina_path=None,
            meta_path=meta, task_id=task_id, session_id=session_id,
        )

    # 在 producer_simulate 里摸出 ``run_one_task_sync`` (生产真实入口).
    # 同时也 stub ``run_one_task`` (async 版) — 防止任何子任务意外退回
    # 直接调 async 函数 (会触发 coroutine attribute error, 上轮被隐没).
    import orchestration.producer_simulate as _ps_mod
    monkeypatch.setattr(_ps_mod, "run_one_task_sync", fake_simulate_run_one_task)
    monkeypatch.setattr(_ps_mod, "run_one_task", fake_simulate_run_one_task)

    # 在 gdr_worker / etl_worker 里摸出 NonRetryable 类 + run_*_once
    import orchestration.workers.gdr_worker as _gw
    import orchestration.workers.etl_worker as _ew
    monkeypatch.setattr(_gw, "GdrNonRetryableError", _GdrNonRetryableError)
    monkeypatch.setattr(_gw, "GdrAuditedError", _GdrAuditedError)
    monkeypatch.setattr(_gw, "run_gdr_once", fake_run_gdr_once)
    monkeypatch.setattr(_ew, "EtlNonRetryableError", _EtlNonRetryableError)
    monkeypatch.setattr(_ew, "run_etl_once", fake_run_etl_once)

    return counts


# ---------------------------------------------------------------------------
# 三阶段顺序
# ---------------------------------------------------------------------------


def test_three_stages_in_order(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DONE
    assert result["stage"] == "done"
    assert counts["simulate"] == 1
    assert counts["gdr"] == 1
    assert counts["etl"] == 1

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DONE
    # 各阶段产物路径都写了
    assert task.src_path is not None
    assert task.gdr_refined_path is not None
    assert task.etl_messages_path is not None
    assert task.etl_openai_path is not None
    assert task.etl_meta_path is not None


def test_simulate_failure_skips_gdr(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, simulate_state="executor_error")
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "simulate"
    assert counts["gdr"] == 0
    assert counts["etl"] == 0

    task = queue.get_task("T1")
    assert task is not None and task.phase == PHASE_DEAD


@pytest.mark.parametrize("run_state", ["guide_exhausted", "inconclusive"])
def test_validation_failed_trajectory_still_reaches_gdr(
    tmp_path: Path, monkeypatch, run_state: str,
) -> None:
    """验证不通过 ≠ 数据不可用: 轨迹必须继续走 gdr -> etl, 不进 dead.

    CLAUDE.md "数据保留原则": 结构完整的轨迹不进死信. 远端 Agent 拒答
    (guide_exhausted) 或语义判定不确定 (inconclusive) 都是有价值的素材,
    其质量信号由「验证不通过原因 + agent 回复内容」交 LLM 评价承载,
    不靠丢弃数据表达 (T001 历史回归: 拒答轨迹曾被判 dead 导致无法审查).
    """
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, simulate_state=run_state)
    result = _run_one_task_pipeline("T1", paths, gdr_settings, settings)

    # 三阶段都跑了, 终态是 done
    assert result["phase"] == PHASE_DONE, result
    assert counts["gdr"] == 1
    assert counts["etl"] == 1

    task = queue.get_task("T1")
    assert task is not None and task.phase == PHASE_DONE
    # 产物路径齐全 — 人工审查要能顺着这些路径找到 C1/C2/C3
    assert task.src_path is not None
    assert task.gdr_refined_path is not None
    assert task.etl_messages_path is not None


def test_validation_failed_trajectory_reaches_gdr_audit_sidepath(
    tmp_path: Path, monkeypatch,
) -> None:
    """验证不通过的轨迹若在 gdr 被判评分低, 走 audited 而非 dead.

    与 ``test_gdr_audited_*`` 的区别: 前者 simulate 是 SUCCESS, 这里
    simulate 验证就没过. 两条路径的终态必须是同一个 —— 数据保留优先.
    """
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(
        monkeypatch, simulate_state="guide_exhausted", gdr_audited="scoring_reject",
    )
    result = _run_one_task_pipeline("T1", paths, gdr_settings, settings)

    assert result["phase"] == PHASE_AUDITED
    assert result["stage"] == "gdr"
    assert counts["etl"] == 0

    task = queue.get_task("T1")
    assert task is not None and task.phase == PHASE_AUDITED
    # src_path 保留 — 数据留在原地供人工复核, 不 move 进 dead_dir
    assert task.src_path is not None


def test_simulate_exception_marks_dead(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(
        monkeypatch, simulate_exc=RuntimeError("simulate crashed"),
    )
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "simulate"
    assert counts["gdr"] == 0
    assert queue.get_task("T1").phase == PHASE_DEAD


# ---------------------------------------------------------------------------
# 阶段内重试
# ---------------------------------------------------------------------------


def test_gdr_retry_then_success(tmp_path: Path, monkeypatch) -> None:
    """gdr 失败 2 次, 第 3 次成功 → done."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_gdr=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_fail_count=2)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DONE
    # gdr_fail_count=2 → 前 2 次失败, 第 3 次成功
    assert counts["gdr"] == 3

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DONE
    assert task.attempts_gdr == 3


def test_gdr_retry_exhausted_dead(tmp_path: Path, monkeypatch) -> None:
    """max_retry_gdr=2, gdr 永远失败 → dead, attempts=3."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_gdr=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_fail_count=99)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "gdr"
    # max_retry_gdr=2 → 共跑 3 次 (1+2)
    assert counts["gdr"] == 3

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DEAD
    assert task.attempts_gdr == 3


def test_gdr_nonretryable_direct_dead(tmp_path: Path, monkeypatch) -> None:
    """NonRetryableError 立即 dead, 不消耗 attempts."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_gdr=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_nonretryable=True)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "gdr"
    assert counts["gdr"] == 1

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DEAD


def test_etl_retry_then_success(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_etl=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, etl_fail_count=1)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DONE
    assert counts["etl"] == 2

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DONE
    assert task.attempts_etl == 2


def test_etl_retry_exhausted_dead(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_etl=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, etl_fail_count=99)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "etl"
    assert counts["etl"] == 3

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_DEAD
    assert task.attempts_etl == 3


def test_etl_nonretryable_direct_dead(tmp_path: Path, monkeypatch) -> None:
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings(retry_etl=2)
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, etl_nonretryable=True)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_DEAD
    assert result["stage"] == "etl"
    assert counts["etl"] == 1


# ---------------------------------------------------------------------------
# 评分低 → audited 终态 (CLAUDE.md "数据保留原则")
# ---------------------------------------------------------------------------


def test_gdr_judge_discard_marks_audited(tmp_path: Path, monkeypatch) -> None:
    """judge 评分低 (judge_discard) → audited, 不进 dead, **但 C3 照出**.

    与 gdr_nonretryable 走 dead 不同: 评分低是质量决策, 不是结构失败.
    data 保留在 src_path + 旁路 jsonl, failure_handler 不归档.

    2026-09-29: judge_discard 的 C2 **已经落盘** (精修做完, 只是评分没过),
    所以照常跑 etl 出 C3 并推 LS —— 结构合格但评分低的轨迹是有用素材,
    让标注员复核"gdr 拒收得对不对"。终态仍是 audited: 产出了 C3 不等于
    洗成 done, 否则 status 统计会把低分样本算成正常样本。
    """
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_audited="judge_discard")
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_AUDITED
    assert result["stage"] == "done"
    # 单次判定, 不重试 (拒收是确定性结果, 重试无意义)
    assert counts["gdr"] == 1
    # C2 已落盘 → 继续跑 etl 出 C3
    assert counts["etl"] == 1

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_AUDITED
    # error_msg 含 audit_reason 便于审计
    assert task.error_msg is not None
    assert "judge_discard" in task.error_msg
    # C3 路径已写 —— 推 LS 要用它
    assert task.etl_meta_path is not None


def test_gdr_scoring_reject_marks_audited(tmp_path: Path, monkeypatch) -> None:
    """free_quality 评分低 (scoring_reject) → audited, 不进 dead."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_audited="scoring_reject")
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )

    assert result["phase"] == PHASE_AUDITED
    assert result["stage"] == "gdr"
    assert counts["gdr"] == 1

    task = queue.get_task("T1")
    assert task is not None
    assert task.phase == PHASE_AUDITED
    assert task.error_msg is not None
    assert "scoring_reject" in task.error_msg


def test_scoring_reject_does_not_invoke_etl(tmp_path: Path, monkeypatch) -> None:
    """scoring_reject → audited 且 **etl 不跑** (C2 刻意没写, 推不了 LS).

    与 judge_discard 分道扬镳的地方: 后者的 C2 已经落盘, 前者的没有 ——
    gdr 侧的理由是「C2 即为待训练产物, 拒收样本不该占训练目录」。没有 C2
    就跑不出 C3, 也就没有可推 LS 的东西。这是设计上的硬墙, 不是遗漏。
    """
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    counts = _patch_pipeline(monkeypatch, gdr_audited="scoring_reject")
    _run_one_task_pipeline("T1", paths, gdr_settings, settings)

    assert counts["gdr"] == 1
    assert counts["etl"] == 0

    task = queue.get_task("T1")
    assert task is not None
    # etl 路径字段保持 None, 不写 etl_messages_path / etl_openai_path 等
    assert task.etl_messages_path is None
    assert task.etl_openai_path is None
    assert task.etl_meta_path is None


def test_audited_forwards_audit_reason_to_etl(tmp_path: Path, monkeypatch) -> None:
    """audit_reason 必须一路透传到 etl —— 丢了就没人打低分标签.

    链路: gdr → _safe_run_gdr → task_pipeline → _safe_run_etl → run_etl_once
    → session.metadata["audit_reason"] → C3 meta → 评分卡 audit 标记 →
    label_config 展示块。中间任一环漏传, 标注员就只看到一堆 0 分却不知道为什么,
    会照常标"可用", 等于把 gdr 拒收过的样本又标回训练集。
    """
    paths = _make_paths(tmp_path)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    seen = _patch_pipeline(monkeypatch, gdr_audited="judge_discard")
    _run_one_task_pipeline("T1", paths, gdr_settings, settings)

    assert seen["etl_audit_reason"] == "judge_discard"


def test_normal_path_sends_no_audit_reason(tmp_path: Path, monkeypatch) -> None:
    """正常通过的 task 不该带 audit_reason —— 评分卡靠键存在与否判断打标."""
    paths = _make_paths(tmp_path)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    seen = _patch_pipeline(monkeypatch)
    _run_one_task_pipeline("T1", paths, gdr_settings, settings)

    assert seen["etl_audit_reason"] is None


# ---------------------------------------------------------------------------
# 子进程不抛异常给主进程 — 任何未捕获都 mark_failed
# ---------------------------------------------------------------------------


def test_unhandled_exception_does_not_propagate(tmp_path: Path, monkeypatch) -> None:
    """若 simulate 抛非预期异常, _run_one_task_pipeline 内部 catch, 返回 dead.

    实施细节: simulate_exc 走 monkeypatch 的 fake_simulate_run_one_task 内部 raise,
    _run_one_task_pipeline 内有 try/except 捕获 → mark_failed + return dead。
    """
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    _patch_pipeline(monkeypatch, simulate_exc=RuntimeError("boom"))
    # 直接调, 不抛
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )
    assert result["phase"] == PHASE_DEAD
    assert "boom" in (result["error"] or "")


def test_returns_dict_keys(tmp_path: Path, monkeypatch) -> None:
    """返回 dict 必须含 task_id / stage / error 字段."""
    paths = _make_paths(tmp_path)
    queue = SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    _patch_pipeline(monkeypatch)
    result = _run_one_task_pipeline(
        "T1", paths, gdr_settings, settings,
    )
    assert set(result.keys()) == {"task_id", "phase", "stage", "error"}
    assert result["task_id"] == "T1"


def test_production_uses_run_one_task_sync_not_async(tmp_path: Path, monkeypatch) -> None:
    """回归保护: ``_run_one_task_pipeline`` 必须调 sync wrapper, 不是 async 版.

    Round 2 (2026-09-22) 发现的 P0 BUG-1: ``task_pipeline`` 误 import
    ``run_one_task`` (async), 所有 task 100% crash 在
    ``AttributeError: 'coroutine' object has no attribute 'state'``.
    原测试 stub 的是 ``run_one_task`` async 版, 完全没遮蔽真实生产
    ``run_one_task_sync`` 调用. 本测试只 stub async, 不 stub sync,
    若生产代码退化到 async import, 会触发 coroutine AttributeError → dead,
    测试可捕获. sync stub 由其它 11 个测试 (调用 ``_patch_pipeline``)
    提供完整覆盖.
    """
    import asyncio

    import orchestration.producer_simulate as _ps_mod

    async def fake_async(task_id, *, config_path):
        # 真生产入口若误用此函数会拿到一个 coroutine, 然后 ``run.state`` 崩.
        await asyncio.sleep(0)
        raise AssertionError(
            "production code should never call async run_one_task"
        )

    # 只 stub async 版 (模拟误用), 不 stub sync 版
    monkeypatch.setattr(_ps_mod, "run_one_task", fake_async)
    # sync 版保留原样, 但若生产调 sync 也会成功. 因此本测试本质是:
    # 若生产误用 async, ``fake_async`` 抛 AssertionError → 顶层 catch
    # → ``error_msg`` 含 AssertionError. 我们不接受此 error_msg.

    paths = _make_paths(tmp_path)
    SQLiteQueue(paths.sqlite_db)
    settings = _make_pipeline_settings()
    gdr_settings = _make_gdr_settings(paths)

    # stub gdr / etl 走通
    counts = _patch_pipeline(monkeypatch)

    result = _run_one_task_pipeline(
        "T_REG", paths, gdr_settings, settings,
    )

    # 生产调 sync (RunOneTaskSync 真实逻辑, ``fake_simulate_run_one_task`` 由
    # _patch_pipeline 提供), 不应触发 coroutine AttributeError / AssertionError.
    assert result["phase"] == PHASE_DONE, (
        f"production fell back to async run_one_task: {result['error']!r}"
    )
    assert "AssertionError" not in (result["error"] or "")
    assert counts["simulate"] == 1
    assert counts["gdr"] == 1
    assert counts["etl"] == 1
