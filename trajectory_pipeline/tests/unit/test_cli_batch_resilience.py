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
    3. **起浏览器这一层不重试**（2026-10-26 换 camofox 后改的）——
       camofox 侧起不来只有两种原因：**适配器不可执行**（缺件，
       重试没用）或 **REST 服务没起**（重试也没用，要人去起服务）。
       旧 camoufox 那支的「重试一次」是为**偶发的浏览器启动超时**
       设计的（实测 3 分钟成本换回 10 条数据），而 camofox 这条链上
       **没有那个偶发因素** ⇒ 重试只是把同一个错误拖成两遍。

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
from trajectory_pipeline.executor.archive import CredentialLeak
from trajectory_pipeline.executor.orchestrator import RunRecord


# ─────────────── 替身 ───────────────


class _FakeSession:
    """假 camofox 会话。**不真起浏览器**。"""

    def __init__(self) -> None:
        self.closed = 0
        self.fingerprint = {"engine": "camoufox", "base_url": "http://localhost:9377"}

    async def close(self) -> None:
        self.closed += 1


def _stub_launch(monkeypatch, *, fail_on: set[int] | None = None):
    """打桩 ``cli._launch_camofox``。第 N 次调用起失败。

    返回 ``(state, made)``：``state["n"]`` 是调用次数，``made`` 是成功
    起过的会话。

    ⚠️ **camofox 没有 ``headless`` 开关**（浏览器由上游 REST 服务持有），
    所以打桩的 launch **不带参数**——带了就与生产链的调用形状对不上，
    而「形状对不上」在本仓库的症状是「测的不是生产链」。

    ⚠️ **顺带打桩执行层缺件预检**（2026-10-26）：批次入口会先问
    「camofox 适配器在不在盘上」，不在就**启动前退 2**（缺件即报错，
    不猜）。本文件验的是**批次韧性**（一条失败不毁整批 / 失败要记账），
    **不是**执行层可用性——而这里起浏览器整个是假的 ⇒
    预检也必须是假的，否则这条测试会因为**环境**红，而不是因为**被测逻辑**红。
    执行层缺件这件事有它自己的测试（``Test执行层缺件``）。
    """
    state = {"n": 0}
    made: list[_FakeSession] = []

    async def _launch():
        state["n"] += 1
        if fail_on and state["n"] in fail_on:
            # 抄真实事故的那句话——报错文本要像，才能验「读得懂」
            raise RuntimeError(
                "BrowserType.launch: Timeout 180000ms exceeded"
            )
        s = _FakeSession()
        made.append(s)
        return s

    monkeypatch.setattr(cli, "_launch_camofox", _launch)
    monkeypatch.setattr(cli, "_execution_layer_missing_reason", lambda kind: "")
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
                driver="camofox",
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
        limit=None, dry_run=False, driver="camofox", executor_mode=None,
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


class Test凭据闸门不毁整批:
    """⚠️ **凭据闸门不许炸整批**（2026-10-28 实测踩到）。

    真实事故：28 条计划跑到**第 12 条**，那条的内容命中了 ``sk-`` 形态
    ⇒ ``CredentialLeak`` 从 ``archive.write`` **一路冒到** ``main``
    ⇒ **整批只跑成 11 条**，剩下 16 条全丢，**且不留痕**
    （进程直接死 ⇒ 连失败清单都没有 ⇒ 批次报表上那 16 条
    长得和「没采过」一模一样）。

    ⇒ 拒绝写盘**是对的**（凭据红线不容商量），但**炸整批是错的**：
    一条失败只该毁那一条。
    """

    async def test_一条命中凭据特征其余照跑(self, tmp_path, monkeypatch,
                                            capsys):
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(_plan_doc(["T001", "T002", "T003"]),
                                    ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "out"

        class _命中就拒写:
            """替身：第 2 条的内容命中凭据特征 ⇒ 拒绝写盘。"""

            def __init__(self, *a, **kw):
                self.root = out

            def write(self, rec, *, task_id, run_id=None, batch_id=None):
                if task_id == "T002":
                    raise CredentialLeak(
                        "P1 内容命中 1 项凭据特征，已拒绝写盘: "
                        "['sk-[A-Za-z0-9]{16,}']"
                    )
                p = self.root / f"{task_id}__run.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}", encoding="utf-8")
                return p

        monkeypatch.setattr(cli, "P1Archive", _命中就拒写)
        _stub_launch(monkeypatch)
        ran = _stub_orchestrator(monkeypatch)

        rc = await cli._run_plan(_args(plan, out))
        text = capsys.readouterr().out

        assert ran == ["T001", "T002", "T003"], (
            f"凭据闸门不该中断后面的条目：实际跑了 {ran}"
        )
        assert rc == 1, "有条目没落盘 ⇒ 退出码必须非 0"
        archives = sorted(p.name for p in out.glob("T*__*.json"))
        assert len(archives) == 2, f"存档只落了 {archives}——被拒的那条不该有档"
        assert "T002" in text, f"失败清单里没有 T002：\n{text}"
        assert "凭据" in text, (
            f"失败理由没说是「内容命中凭据特征」：\n{text}"
        )


class Test起浏览器不重试:
    """**camofox 侧起浏览器不重试**（2026-10-26）。

    旧 camoufox 那支重试一次，是因为撞到过**偶发**的浏览器启动超时
    （实测 3 分钟成本换回 10 条数据）。camofox 这条链上**没有那个
    偶发因素**：起不来只有两种原因——

    - **适配器不可执行**（缺件）：重试没用；
    - **REST 服务没起**：重试也没用，要人去起服务。

    ⇒ 重试只是把同一个错误拖成两遍，还把「缺件」这个**配置错**的
    读数推迟成「跑了 6 分钟才失败」。
    """

    async def test_失败一次就抛不重试(self, monkeypatch):
        from trajectory_pipeline.executor.browser import camofox_session

        calls = {"n": 0}

        async def _fake_launch(cls, *a, **kw):
            calls["n"] += 1
            raise RuntimeError("camofox-browser-mcp 启动失败")

        monkeypatch.setattr(camofox_session.CamofoxSession, "launch",
                            classmethod(_fake_launch))

        with pytest.raises(RuntimeError):
            await cli._launch_camofox()
        assert calls["n"] == 1, (
            f"起浏览器重试了 {calls['n']} 次——camofox 侧重试没有意义"
        )


# ─────────────── 执行层缺件 ───────────────


class Test执行层缺件:
    """**缺件即报错，不猜**（2026-10-26 · 执行层切 camofox）。

    执行层的依赖不在盘上 ⇒ **整批都跑不成**。逐条失败只会刷一屏
    报错，让人误读成「站点的问题」——**那是能力问题，不是数据问题**。
    ⇒ 启动前就退 2，**不启动批次**（一条存档都不该落）。

    ⚠️ camofox 侧缺的是 **MCP 适配器可执行文件**（浏览器本身由上游的
    REST 服务持有，本仓库不装浏览器）；**REST 服务没起**归
    ``CamofoxSession.launch`` 的预检——它把「服务没起」与「站点有问题」
    分开，报错也分开。
    """

    def test_适配器不在时给出可读理由(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_MCP_EXE", "")
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)
        reason = cli._execution_layer_missing_reason("camofox")
        assert reason, "缺 camofox 适配器却没给出理由"
        assert "camofox-browser-mcp" in reason
        # 理由里必须带**怎么装**——只说「不可用」等于把问题推回给读的人
        assert "npm" in reason, f"理由里没有恢复指令：{reason!r}"

    def test_环境变量指向的文件不存在时给出可读理由(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CAMOFOX_MCP_EXE", str(tmp_path / "没有这个"))
        reason = cli._execution_layer_missing_reason("camofox")
        assert reason
        assert "CAMOFOX_MCP_EXE" in reason

    def test_适配器在时没有理由(self, monkeypatch):
        monkeypatch.setenv("CAMOFOX_MCP_EXE", "")
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/x")
        assert cli._execution_layer_missing_reason("camofox") == ""

    def test_别的执行层不管(self):
        """obscura 的可用性由 ``OBSCURA_EXE`` 管，**不归这条预检**；
        已退役的 ``camoufox`` 也不归——它**不再产出**。"""
        assert cli._execution_layer_missing_reason("obscura") == ""
        assert cli._execution_layer_missing_reason("camoufox") == ""

    async def test_缺件时启动前就退2且不落盘(self, tmp_path, monkeypatch,
                                             capsys):
        """**退 2（配置错）而不是 1（跑失败）**，且**一条存档都不落**。"""
        monkeypatch.setattr(cli, "_execution_layer_missing_reason",
                            lambda kind: "camofox 执行层不可用（测试）")
        ran: list[str] = []
        monkeypatch.setattr(cli, "Orchestrator", type("O", (), {
            "__init__": lambda self, *a, **kw: None,
            "run": lambda self, task_id, title, **kw: ran.append(task_id),
        }))

        out = tmp_path / "out"
        plan = tmp_path / "plan-prod1-rest.json"
        plan.write_text(
            json.dumps(_plan_doc(["T001", "T002"], batch_id="plan-prod1"),
                       ensure_ascii=False),
            encoding="utf-8")

        rc = await cli._run_plan(_args(plan, out))
        assert rc == 2, f"缺件应当退 2（配置错），实际 {rc}"
        assert not ran, f"缺件时不该启动批次，实际跑了 {ran}"
        assert not list(out.glob("T*__*.json")), "缺件时不该落存档"
        assert "camofox" in capsys.readouterr().out


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
