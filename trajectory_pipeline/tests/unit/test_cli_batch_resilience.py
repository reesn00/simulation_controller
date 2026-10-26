"""**批次韧性**的门禁：一条没跑成**不许毁整批**（2026-10-26 实测踩到）。

起因（真实事故）
----------------
12 条计划的 agent 生产批跑到第 3 条时死了::

    playwright._impl._errors.TimeoutError: BrowserType.launch: Timeout 180000ms exceeded
    Crash Annotation GraphicsCriticalError: |[0][GFX1-]: RenderCompositorSWGL failed mapping default framebuffer

**起浏览器**这一步偶发失败 ⇒ 整个批次的循环被异常掀掉 ⇒ 后面 9 条
**一条没跑**，而且**不留任何痕**（没跑成的条目在存档里压根不存在，
批次报表上它们和「没采过」长得一模一样）。退出码是 1，但终端上只看到
一条 traceback。

这里封三件事：

    1. **起浏览器失败只毁那一条**——其余条目照跑，存档照落；
    2. **失败要记账**——失败清单打进批次小结，退出码非 0；
    3. **起浏览器重试一次**——偶发失败重试一次就回来（实测 3 分钟成本
       换回 10 条数据）；连挂两次才抛。

另有一件同源的：**批次身份**（``batch_id``）。一批计划**分两次跑**
（前半段挂了、后半段续跑）时，两份计划的文件名不同而**批次身份必须
相同**——否则同一批被切成两批，批次元数据反而更糊。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from trajectory_pipeline.executor import cli
from trajectory_pipeline.executor.orchestrator import RunRecord


# ─────────────── 替身 ───────────────


class _FakeSession:
    """假 camoufox 会话。**不真起浏览器**。"""

    def __init__(self) -> None:
        self.closed = 0
        self.fingerprint = {"os": "windows", "headless": True}

    async def close(self) -> None:
        self.closed += 1


def _stub_launch(monkeypatch, *, fail_on: set[int] | None = None):
    """打桩 ``cli._launch_camoufox``。第 N 次调用起失败。

    返回 ``(state, made)``：``state["n"]`` 是调用次数，``made`` 是成功
    起过的会话。
    """
    state = {"n": 0}
    made: list[_FakeSession] = []

    async def _launch(*, headless: bool = True):
        state["n"] += 1
        if fail_on and state["n"] in fail_on:
            # 抄真实事故的那句话——报错文本要像，才能验「读得懂」
            raise RuntimeError(
                "BrowserType.launch: Timeout 180000ms exceeded"
            )
        s = _FakeSession()
        made.append(s)
        return s

    monkeypatch.setattr(cli, "_launch_camoufox", _launch)
    return state, made


def _stub_orchestrator(monkeypatch) -> list[str]:
    """打桩 ``cli.Orchestrator``：回一条真 ``RunRecord``（不联网）。"""
    ran: list[str] = []

    class _Orch:
        def __init__(self, driver, perceptor, cfg, **kw):
            self.driver = driver

        async def run(self, task_id, title, **kw):
            ran.append(task_id)
            return RunRecord(
                task_id=task_id, title=title, query="q", search_url="u",
                driver="camoufox",
            )

    monkeypatch.setattr(cli, "Orchestrator", _Orch)
    monkeypatch.setattr(cli, "make_driver", lambda *a, **kw: object())
    monkeypatch.setattr(cli, "_make_perceptor",
                        lambda *a, **kw: type("P", (), {"name": "rule"})())
    monkeypatch.setattr(cli, "_dispatch_l6_summary", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "_print_summary", lambda *a, **kw: None)
    return ran


def _plan_doc(task_ids: list[str], *, batch_id: str = "") -> dict:
    """一份最小可跑计划（结构与 ``executor/plan.py`` 的读入口径一致）。"""
    doc: dict = {
        "report": {"seed": 7, "library_digest": "abc123",
                   "by_mode": {"single_title": len(task_ids)}},
        "instances": [
            {
                "task_id": tid,
                "persona_id": "parent-喜剧",
                "scenario_id": "media_lookup_standard",
                "prompt_text": "我想找到《功夫》电影在线观看的可播放网址",
                "search_query": "功夫 在线观看",
                "has_standard": True,
                "retrieval_mode": "single_title",
                "title": "功夫",
                "rewritten": True,
                "provenance": {"persona_id": "parent-喜剧"},
            }
            for tid in task_ids
        ],
    }
    if batch_id:
        doc["batch_id"] = batch_id
    return doc


def _args(plan: Path, out: Path) -> argparse.Namespace:
    return argparse.Namespace(
        plan=str(plan), title=None, include_unrewritten=False,
        limit=None, dry_run=False, driver="camoufox", executor_mode=None,
        engine="bing", max_candidates=1, max_chars=None,
        stop_after_success=0, discovery_path="direct",
        max_intermediates=3, max_referral_links=5,
        per_site_timeout=None, out=str(out), task_id="T001",
        element_basis=False, headful=False,
    )


# ─────────────── 批次韧性 ───────────────


class Test一条失败不毁整批:
    async def test_中间一条起浏览器失败其余照跑(self, tmp_path, monkeypatch,
                                                 capsys):
        """**起浏览器失败只毁那一条**。

        实测事故：12 条计划在第 3 条死于 launch 超时，剩下 9 条**全丢**。
        这里钉住「丢一条，不丢整批」。
        """
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(_plan_doc(["T001", "T002", "T003"]),
                                    ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "out"
        _stub_launch(monkeypatch, fail_on={2})     # 第 2 条起浏览器失败
        ran = _stub_orchestrator(monkeypatch)

        rc = await cli._run_plan(_args(plan, out))

        assert rc == 1, "有条目没跑成 ⇒ 退出码必须非 0"
        assert ran == ["T001", "T003"], (
            f"失败的一条之后不该停批：实际跑了 {ran}"
        )
        archives = sorted(p.name for p in out.glob("T*__*.json"))
        assert len(archives) == 2, f"存档只落了 {archives}——其余条目被连坐了"

    async def test_失败清单进批次小结(self, tmp_path, monkeypatch, capsys):
        """**没跑成的那几条必须被点名**。

        「没跑成」与「跑了但没成功」是**两件事**：前者是能力问题，
        后者才是素材质量差。不点名就会混进批次报表被读成「这个 persona
        组合表现差」——而真相是「这三条压根没跑」。
        """
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(_plan_doc(["T001", "T002"]),
                                    ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "out"
        _stub_launch(monkeypatch, fail_on={2})
        _stub_orchestrator(monkeypatch)

        await cli._run_plan(_args(plan, out))
        text = capsys.readouterr().out
        assert "没跑成" in text, f"批次小结没点出失败条目：\n{text}"
        assert "T002" in text, f"失败清单里没有 T002：\n{text}"

    async def test_全部成功时退出码是0(self, tmp_path, monkeypatch):
        """**没有失败不许给非 0**——否则「没跑成」与「跑成了」分不开。"""
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(_plan_doc(["T001", "T002"]),
                                    ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "out"
        _stub_launch(monkeypatch)
        _stub_orchestrator(monkeypatch)

        rc = await cli._run_plan(_args(plan, out))
        assert rc == 0
        assert len(list(out.glob("T*__*.json"))) == 2


class Test起浏览器重试:
    async def test_第一次失败重试一次就回来(self, monkeypatch):
        """**偶发失败重试一次**——实测的 180s launch 超时是偶发的。

        重试成本 3 分钟，换回 10 条数据。
        """
        from trajectory_pipeline.executor.browser import camoufox_session

        calls = {"n": 0}

        async def _fake_launch(cls, *, headless: bool = True):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("BrowserType.launch: Timeout 180000ms exceeded")
            return _FakeSession()

        monkeypatch.setattr(camoufox_session.CamoufoxSession, "launch",
                            classmethod(_fake_launch))
        monkeypatch.setattr(cli, "_LAUNCH_RETRY_DELAY_S", 0.0)

        session = await cli._launch_camoufox(headless=True)
        assert session is not None
        assert calls["n"] == 2, "第一次失败后没有重试"

    async def test_连挂两次才抛(self, monkeypatch):
        """**只重试一次**：连挂说明是真问题，再试只是把同一个错误拖两遍。"""
        from trajectory_pipeline.executor.browser import camoufox_session

        calls = {"n": 0}

        async def _fake_launch(cls, *, headless: bool = True):
            calls["n"] += 1
            raise RuntimeError("BrowserType.launch: Timeout 180000ms exceeded")

        monkeypatch.setattr(camoufox_session.CamoufoxSession, "launch",
                            classmethod(_fake_launch))
        monkeypatch.setattr(cli, "_LAUNCH_RETRY_DELAY_S", 0.0)

        with pytest.raises(RuntimeError):
            await cli._launch_camoufox(headless=True)
        assert calls["n"] == 2, "重试次数超了——真问题时不该一直试"


# ─────────────── 批次身份 ───────────────


class Test批次id:
    def test_优先取计划里的batch_id(self):
        """**计划里写了就用它**——文件名只是退回路径。"""
        from types import SimpleNamespace

        plan = SimpleNamespace(batch_id="plan-camoufox-prod1",
                               source="/x/plan-camoufox-prod1-rest.json")
        assert cli._plan_batch_id(plan) == "plan-camoufox-prod1"

    def test_没写就退回文件名(self):
        """存量计划文件顶层只有 ``report`` 与 ``instances`` ⇒ 退回文件名。"""
        from types import SimpleNamespace

        plan = SimpleNamespace(batch_id="", source="/x/plan-ab.json")
        assert cli._plan_batch_id(plan) == "plan-ab"

    def test_两段式计划取同一批次身份(self):
        """**同一批分两次跑 ⇒ 批次身份相同**，哪怕文件名不同。"""
        from trajectory_pipeline.executor.plan import parse_plan

        first = parse_plan(_plan_doc(["T001"], batch_id="plan-prod1"),
                           source="/x/plan-prod1.json")
        rest = parse_plan(_plan_doc(["T002"], batch_id="plan-prod1"),
                          source="/x/plan-prod1-rest.json")
        assert first.batch_id == rest.batch_id == "plan-prod1"
        assert cli._plan_batch_id(first) == cli._plan_batch_id(rest)

    async def test_批次id落进存档(self, tmp_path, monkeypatch):
        """**存档里的 ``batch_id`` 取计划里那个**——不是 ``--task-id``，
        也不是文件名。

        实测（2026-10-26）：12 实例那批的存档 ``batch_id`` 全是 ``"T001"``，
        而那一批根本不含 T001 ⇒ 批次元数据退化成常数，两批分不开。
        """
        plan = tmp_path / "plan-prod1-rest.json"
        plan.write_text(
            json.dumps(_plan_doc(["T001"], batch_id="plan-prod1"),
                       ensure_ascii=False),
            encoding="utf-8")
        out = tmp_path / "out"
        _stub_launch(monkeypatch)
        _stub_orchestrator(monkeypatch)

        await cli._run_plan(_args(plan, out))
        archives = list(out.glob("T*__*.json"))
        assert archives, "没落盘"
        blob = json.loads(archives[0].read_text(encoding="utf-8"))
        assert blob["batch_id"] == "plan-prod1", (
            f"存档的 batch_id 是 {blob['batch_id']!r}，不是计划里那个"
        )


# ─────────────── 手工路径同款 ───────────────


class Test手工路径也隔离:
    async def test_手工路径一条失败其余照跑(self, tmp_path, monkeypatch):
        """**手工路径与计划路径是两处独立调用点**（2026-10-22 踩过）。

        漏一处 ⇒ 手工路径不炸、计划路径炸，而后者才是批量生产入口。
        """
        out = tmp_path / "out"
        _stub_launch(monkeypatch, fail_on={1})
        ran = _stub_orchestrator(monkeypatch)

        args = argparse.Namespace(
            task_id="T001", title=["功夫", "叶问"], out=str(out),
            engine="bing", max_candidates=1, max_chars=None,
            stop_after_success=0, perceptor=None, discovery_path="direct",
            max_intermediates=3, max_referral_links=5, executor_mode=None,
            driver="camoufox", per_site_timeout=None, element_basis=False,
            headful=False,
        )
        rc = await cli._run_manual(args)
        assert rc == 1
        assert ran == ["T001"], f"失败的那条之后不该停：实际跑了 {ran}"
        assert len(list(out.glob("T*__*.json"))) == 1
