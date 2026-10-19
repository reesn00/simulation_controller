"""执行器模式的单测（方案 13 §6.1，A2a-3 埋点）。

测的是**灰度开关本身**，不是两条链跑出来的轨迹（那在
``test_agent_visit.py`` / ``test_agent_loop.py``）：

1. 模式解析的优先级——``--executor-mode`` > ``TRAJECTORY_EXECUTOR_MODE``
   > 默认 scripted（A3-1 转正后参数门是正门，环境变量是 fallback）；
2. 准入在**构造期**——「要 agent 却没后端」必须当场炸，不能静默退回；
3. 分派恰好两处——站点链 ``_visit_guarded`` 与中间页链
   ``_intermediate_visit`` 各一处（A2b 起），且**只在那两处**；
4. 产地唯一——模式的值活在 ``RunConfig``，Orchestrator 不再收
   ``executor_mode`` 关键字（A3-1）。

第 2 条是这批测试存在的理由：agent 模式没有后端时若静默退回 scripted，
那一批存档上写的是 ``executor_mode="scripted"``，于是「agent 批失败了
40%」会被读成「scripted 跑得好好的」——**配置错误伪装成一次成功的
灰度**，而它不报错、不空转，报表还挺好看。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trajectory_pipeline.executor import orchestrator
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.mode import (
    AGENT,
    DEFAULT_EXECUTOR_MODE,
    ENV_EXECUTOR_MODE,
    EXECUTOR_MODES,
    SCRIPTED,
    configured_mode,
)
from trajectory_pipeline.executor.orchestrator import (
    Orchestrator,
    RunConfig,
    RunRecord,
)
from trajectory_pipeline.executor.steps import agent_intermediate
from trajectory_pipeline.executor.steps import aggregator as aggregator_step
from trajectory_pipeline.executor.steps import article as article_step
from trajectory_pipeline.executor.steps.search import Candidate

_REPO = Path(__file__).resolve().parents[3]


def _orch(mode: str, path: str) -> Orchestrator:
    """一条中间页链的 orchestrator。后端用字符串占位——本题测的是
    分派到谁，不是后端能不能用（后者由 :class:`Test构造期准入` 管）。"""
    return Orchestrator(
        None,
        None,
        RunConfig(discovery_path=path, executor_mode=mode),
        agent_backend="_backend" if mode == AGENT else None,
    )


class _Recorder:
    """替掉两条链的入口，记下「谁被调用了、拿到什么参数」。"""

    def __init__(self, result=None) -> None:
        self.calls: list[dict] = []
        self._result = result

    async def __call__(self, driver, perceptor, ledger, candidate, *args, **kw):
        self.calls.append({"candidate": candidate, "args": args, "kw": dict(kw)})
        return self._result


class _ArityRecorder:
    """不假定签名的记录器（A2b：两条中间页链的签名本就不同）。

    与 :class:`_Recorder` 分开而不是给它加兼容：站点链的签名是固定
    的，那个记录器**按签名断言**是有价值的（签名变了测试会红）；中间页
    链恰恰要测「两种模式签名不同」，按固定签名记就测不到了。
    """

    def __init__(self, name: str, result=None) -> None:
        self.name = name
        self.calls: list[dict] = []
        self._result = result

    async def __call__(self, *args, **kw):
        self.calls.append({"name": self.name, "args": args, "kw": dict(kw)})
        return self._result

    @property
    def called(self) -> bool:
        return bool(self.calls)


class Test环境变量:
    def test_未设则默认scripted(self):
        assert configured_mode(env={}) == SCRIPTED
        assert DEFAULT_EXECUTOR_MODE == SCRIPTED

    @pytest.mark.parametrize("value", ["  agent ", "AGENT", "agent"])
    def test_agent生效且大小写与空白不敏感(self, value):
        assert configured_mode(env={ENV_EXECUTOR_MODE: value}) == AGENT

    def test_显式scripted与未设同值(self):
        """显式写 scripted 与没设是同一个结果——它不是「灰度失败」。"""
        assert configured_mode(env={ENV_EXECUTOR_MODE: "scripted"}) == SCRIPTED

    def test_空串按未设处理(self):
        """与「设了但打错」是两回事：前者是常态，后者是事故。"""
        assert configured_mode(env={ENV_EXECUTOR_MODE: "   "}) == SCRIPTED

    def test_未知值报错不猜(self):
        with pytest.raises(ValueError) as ei:
            configured_mode(env={ENV_EXECUTOR_MODE: "agentt"})
        assert "agentt" in str(ei.value)
        assert "scripted" in str(ei.value)      # 说清它会静默跑成什么

    def test_两值与模块常量同源(self):
        assert set(EXECUTOR_MODES) == {SCRIPTED, AGENT}


class Test构造期准入:
    def test_默认构造是scripted(self):
        """既有调用点零改动——加配置的第一纪律。"""
        orch = Orchestrator(None, None)
        assert orch._mode == SCRIPTED
        assert orch._agent is None

    def test_agent模式要有后端(self):
        with pytest.raises(ValueError) as ei:
            Orchestrator(None, None, RunConfig(executor_mode=AGENT))
        assert "没注入 agent 后端" in str(ei.value)

    def test_报错要说清该去配什么(self):
        """报错文案要**给出能照着做的下一步**。

        只说「后端没注入」，读的人下一步是去翻代码；说清
        ``TRAJECTORY_LLM_*``，他下一步是去配环境。
        """
        with pytest.raises(ValueError) as ei:
            Orchestrator(None, None, RunConfig(executor_mode=AGENT))
        text = str(ei.value)
        assert "TRAJECTORY_LLM_" in text
        assert SCRIPTED in text      # 也告诉他「配不齐就显式用 scripted 跑」

    def test_模式落进RunRecord(self):
        """批次级属性：**每一份存档都要能自己回答「这批是谁跑的」**。"""
        orch = Orchestrator(None, None, RunConfig(executor_mode=AGENT),
                            agent_backend=object())
        assert orch._mode == AGENT
        rec = RunRecord(task_id="T1", title="t", query="q", search_url="u",
                        executor_mode=orch._mode)
        assert rec.to_json()["executor_mode"] == AGENT


class TestRunConfig字段:
    """``executor_mode`` 的产地唯一性（A3-1 转正，方案 13 §6.1）。

    值活在 :class:`RunConfig`——Orchestrator 不再收 ``executor_mode``
    关键字。两个产地等于两种真相：构造器能绕开 config 传模式的那一天，
    「这批是谁跑的」就有了两个互相矛盾的答案来源。
    """

    def test_默认scripted零改动(self):
        """既有调用点零改动——加配置的第一纪律。"""
        assert RunConfig().executor_mode == SCRIPTED

    def test_未知取值报错不猜(self):
        """取值合法性在 RunConfig.__post_init__ 查（构造 Orchestrator
        之前就炸），文案沿用 mode.py 的纪律原文。"""
        with pytest.raises(ValueError) as ei:
            RunConfig(executor_mode="自动")
        text = str(ei.value)
        assert "自动" in text                 # 说清收到什么
        assert "scripted" in text             # 说清它会静默跑成什么

    def test_Orchestrator从config读模式(self):
        orch = Orchestrator(None, None, RunConfig(executor_mode=AGENT),
                            agent_backend=object())
        assert orch._mode == AGENT

    def test_Orchestrator不再收executor_mode关键字(self):
        """kwarg 已删——还传就是 TypeError，钉死「产地只有 config」。"""
        with pytest.raises(TypeError):
            Orchestrator(None, None, executor_mode=AGENT)


class TestCLI注入:
    """CLI 是唯一的边界层：LLM 客户端在那里组装，``executor`` 不 import
    ``llm``（01-架构总览 L26-50）。这里盯的是「要 agent 而后端配不齐」时
    的处置——**缺件即报错，不猜**（与 ``--perceptor llm`` 同款口径）。"""

    KEYS = ("TRAJECTORY_LLM_BASE_URL", "TRAJECTORY_LLM_MODEL",
            "TRAJECTORY_LLM_API_KEY",
            # 升配三键（A2a-4）：不一起清掉，配齐了主后端的用例会被
            # 环境里残留的 ESCALATE 键污染——「无升配」的断言会 silently
            # 变成「有升配」，而那条用例照绿。
            "TRAJECTORY_LLM_ESCALATE_BASE_URL",
            "TRAJECTORY_LLM_ESCALATE_MODEL",
            "TRAJECTORY_LLM_ESCALATE_API_KEY")

    @pytest.fixture
    def clean_llm_env(self, monkeypatch):
        for k in self.KEYS:
            monkeypatch.delenv(k, raising=False)

    def test_默认是scripted且不去造后端(self, monkeypatch):
        from trajectory_pipeline.executor import cli

        monkeypatch.delenv(ENV_EXECUTOR_MODE, raising=False)
        mode, backend, banner = cli._resolve_executor(None)
        assert mode == SCRIPTED and backend is None
        assert "scripted" in banner and not banner.startswith("[FAIL]")

    def test_要agent但没配后端则挡在开浏览器之前(self, monkeypatch, clean_llm_env):
        from trajectory_pipeline.executor import cli

        monkeypatch.setenv(ENV_EXECUTOR_MODE, "agent")
        mode, backend, banner = cli._resolve_executor(None)
        assert mode == "" and backend is None
        assert banner.startswith("[FAIL]")
        # 说清缺什么、以及不配就该怎么跑
        assert "TRAJECTORY_LLM_" in banner
        assert SCRIPTED in banner

    def test_配齐了才给后端(self, monkeypatch, clean_llm_env):
        from trajectory_pipeline.executor import cli

        monkeypatch.setenv(ENV_EXECUTOR_MODE, "agent")
        monkeypatch.setenv("TRAJECTORY_LLM_BASE_URL", "http://127.0.0.1:8000/v1")
        monkeypatch.setenv("TRAJECTORY_LLM_MODEL", "fake-model")
        mode, backend, banner = cli._resolve_executor(None)
        assert mode == AGENT
        assert backend is not None and backend.escalate is None
        assert not banner.startswith("[FAIL]")
        # 升配未配置这件事要**写在明面上**，别让人以为它已经能升配了
        assert "升配" in banner
        # 解析器必须是真的那个（剥 think + 抠 fence），不然带 <think> 的
        # 输出会直接进协议解析——那条路径的失败形态看着像模型笨。
        raw = '<think>想想</think>\n```json\n{"tool": "click", "ref": "e1",' \
              ' "params": {}, "done": false}\n```'
        got = backend.json_of(raw)
        assert got is not None and got["tool"] == "click" and got["ref"] == "e1"

    def test_拼错则挡下且不静默跑成scripted(self, monkeypatch):
        from trajectory_pipeline.executor import cli

        monkeypatch.setenv(ENV_EXECUTOR_MODE, "Agent ")
        assert cli._resolve_executor(None)[0] == AGENT      # 大小写/空白容错
        monkeypatch.setenv(ENV_EXECUTOR_MODE, "agnt")
        mode, _, banner = cli._resolve_executor(None)
        assert mode == "" and banner.startswith("[FAIL]")


class Test参数门:
    """``--executor-mode`` 参数门（A3-1 转正，方案 13 §6.1）。

    优先级：参数 > 环境变量 > 默认 scripted。环境变量保留到 A3 结束再撤
    （老机器脚本不一夜改完），两条门同一套「缺件即报错」纪律。proberun
    不加这个参数（沿用 discovery-path 先例：灰度只在 run 子命令开）。
    """

    @pytest.fixture
    def clean_llm_env(self, monkeypatch):
        for k in TestCLI注入.KEYS:
            monkeypatch.delenv(k, raising=False)

    @staticmethod
    def _set_main(monkeypatch):
        monkeypatch.setenv("TRAJECTORY_LLM_BASE_URL", "http://127.0.0.1:8000/v1")
        monkeypatch.setenv("TRAJECTORY_LLM_MODEL", "fake-model")

    def test_参数优先于环境变量(self, monkeypatch, clean_llm_env):
        """环境里残留 scripted（或没配），参数显式 agent 就照 agent 走。"""
        from trajectory_pipeline.executor import cli

        monkeypatch.setenv(ENV_EXECUTOR_MODE, "scripted")
        self._set_main(monkeypatch)
        mode, backend, banner = cli._resolve_executor(AGENT)
        assert mode == AGENT and backend is not None
        # banner 写明走的是哪扇门——读日志的人要知道这批是参数定的
        assert "--executor-mode" in banner

    def test_参数显式scripted压住环境变量agent(self, monkeypatch, clean_llm_env):
        """反向优先级同样要钉：环境里残留 agent（上一批灰度的 shell 忘了
        关），参数显式 scripted 就照常跑 scripted 批——不被迫去造后端。"""
        from trajectory_pipeline.executor import cli

        monkeypatch.setenv(ENV_EXECUTOR_MODE, "agent")
        mode, backend, banner = cli._resolve_executor(SCRIPTED)
        assert mode == SCRIPTED and backend is None
        assert not banner.startswith("[FAIL]")
        assert "--executor-mode" in banner

    def test_缺省参数回退环境变量(self, monkeypatch, clean_llm_env):
        """不传参数（None）= 走环境变量——A2a 的行为原样保留。"""
        from trajectory_pipeline.executor import cli

        monkeypatch.setenv(ENV_EXECUTOR_MODE, "agent")
        self._set_main(monkeypatch)
        mode, backend, _ = cli._resolve_executor(None)
        assert mode == AGENT and backend is not None

    def test_run有参数门而proberun没有(self):
        """灰度只在 run 子命令开（方案 13 §6.1）——参数集与取值集
        钉在解析器上：proberun 多了这扇门就是有人破了先例。"""
        import argparse

        from trajectory_pipeline.executor.cli import build_parser

        parser = build_parser()
        sub = next(a for a in parser._actions
                   if isinstance(a, argparse._SubParsersAction))

        def _options(p):
            return {s for a in p._actions for s in a.option_strings}

        assert "--executor-mode" in _options(sub.choices["run"])
        assert "--executor-mode" not in _options(sub.choices["proberun"])

    def test_参数集与封闭集同源(self):
        """argparse choices 用 ``EXECUTOR_MODES`` 本身——「参数有哪些值」
        与「模式有哪些值」一个产地；parser 层面多一个值就是改 mode.py
        的事，不该在两处各维护一份名单。"""
        import argparse

        from trajectory_pipeline.executor.cli import build_parser

        parser = build_parser()
        sub = next(a for a in parser._actions
                   if isinstance(a, argparse._SubParsersAction))
        action = next(a for a in sub.choices["run"]._actions
                      if "--executor-mode" in a.option_strings)
        assert tuple(action.choices) == EXECUTOR_MODES


class Test依赖方向:
    """``common ← llm ← {taskgen, perception, rationale}``，``executor`` 只
    依赖 ``perception`` 的协议（01-架构总览 L26-50）。

    agent 模式是这条纪律**压力最大的地方**：它要用 LLM 客户端，而
    ``rationale_writer`` 那套注入姿势若在这里破例，``executor → llm``
    这条边就长出来了，之后「切 perceptor 实现时 executor 的 git diff
    为空」这条验收谁都说不清还算不算数。
    """

    #: 本 INC 新增/改动的 executor 侧文件。
    FILES = (
        "executor/agent_loop.py",
        "executor/steps/agent_visit.py",
        "executor/steps/visit.py",
        "executor/orchestrator.py",
        "executor/mode.py",
    )

    @staticmethod
    def _runtime_imports(text: str) -> list[str]:
        """取**运行时**会执行的 import，跳过 ``if TYPE_CHECKING:`` 里的。"""
        tree = ast.parse(text)
        skipped: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and _is_type_checking(node.test):
                for sub in ast.walk(node):
                    skipped.add(id(sub))
        out: list[str] = []
        for node in ast.walk(tree):
            if id(node) in skipped:
                continue
            if isinstance(node, ast.Import):
                out.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                out.append(node.module or "")
        return out

    @pytest.mark.parametrize("rel", FILES)
    def test_运行时不import_llm(self, rel):
        path = _REPO / "trajectory_pipeline" / rel
        bad = [
            name for name in self._runtime_imports(
                path.read_text(encoding="utf-8")
            )
            if name.startswith("trajectory_pipeline.llm")
        ]
        assert not bad, (
            f"{rel} 在运行时 import 了 {bad}——agent 的后端与解析器都要"
            f"由 CLI 注入（见 cli._make_agent_backend），理由见"
            f"``executor/agent_loop.py`` 的模块 docstring。"
        )


def _is_type_checking(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "TYPE_CHECKING"


class Test分派:
    async def test_agent模式走agent_visit(self, monkeypatch):
        agent = _Recorder(result="agent-result")
        scripted = _Recorder(result="scripted-result")
        monkeypatch.setattr(orchestrator.agent_visit_step, "agent_visit", agent)
        monkeypatch.setattr(orchestrator.visit_step, "visit_site", scripted)
        orch = Orchestrator(None, None, RunConfig(executor_mode=AGENT),
                            agent_backend="_backend")
        rec = RunRecord(task_id="T1", title="功夫", query="q", search_url="u")
        from trajectory_pipeline.executor.branches import RunLedger

        got = await orch._visit_guarded(
            Candidate(url="https://x.test/", text="站点", rank=1),
            RunLedger(task_id="T1"), rec, title="功夫",
        )
        assert got == "agent-result"
        assert len(agent.calls) == 1 and not scripted.calls
        # 后端的三件套是整体注入的：本题里那个字符串就是占位的后端。
        assert agent.calls[0]["args"] == ("_backend",)
        # 事件容器、账本容器与片名都透传下去——漏掉前两者等于
        # 「拦截/成本没发生过」
        assert agent.calls[0]["kw"]["gatekeeper_events"] is rec.gatekeeper_events
        assert agent.calls[0]["kw"]["llm_calls"] is rec.llm_calls
        assert agent.calls[0]["kw"]["title"] == "功夫"

    async def test_scripted模式走visit_site(self, monkeypatch):
        agent = _Recorder(result="agent-result")
        scripted = _Recorder(result="scripted-result")
        monkeypatch.setattr(orchestrator.agent_visit_step, "agent_visit", agent)
        monkeypatch.setattr(orchestrator.visit_step, "visit_site", scripted)
        orch = Orchestrator(None, None)
        rec = RunRecord(task_id="T1", title="功夫", query="q", search_url="u")
        from trajectory_pipeline.executor.branches import RunLedger

        got = await orch._visit_guarded(
            Candidate(url="https://x.test/", text="站点", rank=1),
            RunLedger(task_id="T1"), rec, title="功夫",
        )
        assert got == "scripted-result"
        assert not agent.calls and len(scripted.calls) == 1

    def _patch_intermediates(self, monkeypatch):
        """换掉四个中间页入口（两种模式 × 两条路径），返回记录器。"""
        rec = {
            "agg/agent": _ArityRecorder("agg/agent", result="ir-agg-agent"),
            "article/agent": _ArityRecorder("article/agent",
                                            result="ir-article-agent"),
            "agg/scripted": _ArityRecorder("agg/scripted",
                                           result="ir-agg-scripted"),
            "article/scripted": _ArityRecorder("article/scripted",
                                               result="ir-article-scripted"),
        }
        monkeypatch.setattr(
            agent_intermediate, "visit_aggregator_agent", rec["agg/agent"])
        monkeypatch.setattr(
            agent_intermediate, "visit_article_agent", rec["article/agent"])
        monkeypatch.setattr(
            aggregator_step, "visit_aggregator", rec["agg/scripted"])
        monkeypatch.setattr(
            article_step, "visit_article", rec["article/scripted"])
        return rec

    async def test_agent模式的B路径走agent聚合站入口(self, monkeypatch):
        rec = self._patch_intermediates(monkeypatch)
        orch = _orch(AGENT, "aggregator")
        record = RunRecord(task_id="T1", title="功夫", query="q", search_url="u")
        got = await orch._intermediate_guarded(
            Candidate(url="https://agg.test/", text="聚合", rank=1),
            RunLedger(task_id="T1"), record,
        )
        assert got == "ir-agg-agent"
        # 只有链上该走的那个被调了——分派表错位会让两条链同时存在
        # 调用记录，那是最容易溜过去的一类错
        assert [k for k, r in rec.items() if r.called] == ["agg/agent"]
        call = rec["agg/agent"].calls[0]
        # agent 链的签名是 (driver, ledger, candidate, backend)——
        # **没有 perceptor**（三道判断题一道都不跑）
        assert len(call["args"]) == 4
        assert call["args"][3] == "_backend"
        # 片名与事件容器都要透传下去：漏了前者终局任务没有目标，
        # 漏了后者等于「中间页上的拦截没发生过」
        assert call["kw"]["title"] == "功夫"
        assert call["kw"]["gatekeeper_events"] is record.gatekeeper_events
        # 账本与事件账本同款透传：同一个 list 对象，循环往里 append，
        # orchestrator 在收口时统一盖 ts（§3.6）。传副本或 None 都是
        # 「看起来接上了」的半接线。
        assert call["kw"]["llm_calls"] is record.llm_calls

    async def test_agent模式的C路径走agent文章入口(self, monkeypatch):
        rec = self._patch_intermediates(monkeypatch)
        orch = _orch(AGENT, "article")
        got = await orch._intermediate_guarded(
            Candidate(url="https://post.test/", text="文章", rank=1),
            RunLedger(task_id="T1"),
            RunRecord(task_id="T1", title="功夫", query="q", search_url="u"),
        )
        assert got == "ir-article-agent"
        assert [k for k, r in rec.items() if r.called] == ["article/agent"]

    async def test_scripted模式走scripted_两路径各自对位(self, monkeypatch):
        """A2b 的改动的**另一侧**：scripted 链的行为一字不能变。

        这条是 B/C 存量的守门人——B/C 路径在 A2b 期间仍是主跑形态
        （§4「灰度期间 B/C 路径仍是 scripted 主跑」），改崩了它比
        agent 链不好用严重得多。
        """
        rec = self._patch_intermediates(monkeypatch)
        for path, key in (("aggregator", "agg/scripted"),
                          ("article", "article/scripted")):
            orch = _orch(SCRIPTED, path)
            await orch._intermediate_guarded(
                Candidate(url="https://x.test/", text="t", rank=1),
                RunLedger(task_id="T1"),
                RunRecord(task_id="T1", title="功夫", query="q", search_url="u"),
            )
            assert rec[key].called
            # scripted 链**带 perceptor**（三道判断题都跑），
            # 且不带 title / gatekeeper_events——那些是 agent 链的东西
            assert len(rec[key].calls[0]["args"]) == 4
            assert "title" not in rec[key].calls[0]["kw"]
            assert "gatekeeper_events" not in rec[key].calls[0]["kw"]
        assert not rec["agg/agent"].called
        assert not rec["article/agent"].called

    async def test_中间页的角色标签只由路径决定(self, monkeypatch):
        """``role`` 是 P1 字段，与谁执行的无关——两种模式必须同值。

        分了模式就会出现「agent 链的 role 与 scripted 不一致」的写法，
        而那会让 P2 切条按执行器分叉，A/B 两批的样本数不再可比。
        """
        self._patch_intermediates(monkeypatch)
        for mode in (SCRIPTED, AGENT):
            orch = _orch(mode, "aggregator")
            _, role = orch._intermediate_visit()
            assert role == "aggregator"
            orch = _orch(mode, "article")
            _, role = orch._intermediate_visit()
            assert role == "article"

    async def test_分派恰好两处_且只在两条链的入口(self):
        """模式判断**恰好两处**：站点链一处、中间页链一处。

        为什么是 2 而不是 1（A2b 从 1 涨上来的）：run 的执行流有两条
        独立的遍历链，各有一个入口，入口之下必须先分派才能组装参数
        （两条链的被调函数签名不同）。把数字钉成 2 而不是「不设上限」，
        是因为第三个出现的地方几乎一定是**逐候选**判断——那会让
        「这条轨迹是谁产的」变成逐条才能回答的问题，而
        ``RunRecord.executor_mode`` 是批次级字段，它答不了。

        只数总数还不够：**位置**也要钉住。有人把 ``if 模式`` 塞进
        ``_visit_guarded`` 的超时兜底里，总数还是 2，但那条兜底从此
        背上模式的包袱。
        """
        src = orchestrator.__file__
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
        assert text.count("self._mode ==") == 2

        tree = ast.parse(text)
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Compare)
                    and isinstance(sub.left, ast.Attribute)
                    and sub.left.attr == "_mode"
                ):
                    found.append(node.name)
        assert sorted(found) == ["_intermediate_visit", "_visit_guarded"]
        assert text.count("self._mode !=") == 0


class TestLLM调用账本:
    """``llm_calls[]`` 账本的接线与盖戳（方案 13 §3.6，A2a-4）。

    记账动作本身在 ``agent_loop._record``（那边测）；这里钉的是
    orchestrator 侧的三件事：容器透传的是**同一个 list 对象**、
    候选访问收口时统一盖 ``ts``、scripted 批的账本恒空但键照落。
    """

    async def test_收口给账本条目盖ts(self, monkeypatch):
        """agent_visit 往里 append 的条目（无 ts），访问返回后必须有戳。

        用整链而不仅调 ``_stamp_llm_calls`` 的理由：盖章的**时机**是
        本测试要钉的东西——盖在收口处（候选级粒度），不是盖在循环里
        （那要让循环体背上时间源）。
        """
        agent = _Recorder(result="r")

        async def _fake_visit(*args, **kw):
            agent.calls.append({"args": args, "kw": dict(kw)})
            kw["llm_calls"].append({"caller": "agent_loop", "model": "m"})
            return "r"

        monkeypatch.setattr(orchestrator.agent_visit_step, "agent_visit",
                            _fake_visit)
        orch = Orchestrator(None, None, RunConfig(executor_mode=AGENT),
                            agent_backend="_backend")
        rec = RunRecord(task_id="T1", title="功夫", query="q", search_url="u")
        got = await orch._visit_guarded(
            Candidate(url="https://x.test/", text="站点", rank=1),
            RunLedger(task_id="T1"), rec, title="功夫",
        )
        assert got == "r"
        assert rec.llm_calls[0]["ts"]            # 盖上了
        assert rec.to_json()["llm_calls"][0]["ts"]  # 落盘带戳

    async def test_中间页链收口同样盖ts(self, monkeypatch):
        rec = {"entry": _ArityRecorder("entry", result="ir")}

        async def _fake(*args, **kw):
            rec["entry"].calls.append({"args": args, "kw": dict(kw)})
            kw["llm_calls"].append({"caller": "agent_loop"})
            return "ir"

        monkeypatch.setattr(agent_intermediate, "visit_aggregator_agent", _fake)
        orch = _orch(AGENT, "aggregator")
        record = RunRecord(task_id="T1", title="功夫", query="q", search_url="u")
        got = await orch._intermediate_guarded(
            Candidate(url="https://agg.test/", text="聚合", rank=1),
            RunLedger(task_id="T1"), record,
        )
        assert got == "ir"
        assert record.llm_calls[0]["ts"]

    def test_只补缺戳不重盖(self):
        """多个候选 sequential 共用同一个账本 list：先访问的候选已盖过，
        重盖会把旧条目刷成新时间——账本的 ts 是该候选的成本封口时间。"""
        from trajectory_pipeline.executor.orchestrator import _stamp_llm_calls

        calls = [{"ts": "2026-10-17T00:00:00+00:00", "model": "old"},
                 {"model": "new"}]
        _stamp_llm_calls(calls)
        assert calls[0]["ts"] == "2026-10-17T00:00:00+00:00"  # 原样
        assert calls[1]["ts"]                     # 补上了
        assert calls[1]["ts"] != calls[0]["ts"]   # 新戳不是复制旧戳
        ts_after_first_pass = calls[1]["ts"]
        _stamp_llm_calls(calls)                    # 幂等：再盖一遍不变
        assert calls[1]["ts"] == ts_after_first_pass

    def test_空账本是空操作(self):
        from trajectory_pipeline.executor.orchestrator import _stamp_llm_calls

        calls: list[dict] = []
        _stamp_llm_calls(calls)                    # 不炸、不加
        assert calls == []

    async def test_scripted批账本恒空但键照落(self, monkeypatch):
        """scripted 不碰账本，但 ``to_json`` 里**键要照落**——
        「没问模型」与「没这个键」在下游是两回事（gatekeeper_events
        同款纪律）。"""
        scripted = _Recorder(result="scripted-result")
        monkeypatch.setattr(orchestrator.visit_step, "visit_site", scripted)
        orch = Orchestrator(None, None)
        rec = RunRecord(task_id="T1", title="功夫", query="q", search_url="u")
        await orch._visit_guarded(
            Candidate(url="https://x.test/", text="站点", rank=1),
            RunLedger(task_id="T1"), rec, title="功夫",
        )
        assert rec.llm_calls == []
        assert rec.to_json()["llm_calls"] == []


class Test升配后端:
    """``TRAJECTORY_LLM_ESCALATE_*`` 三键的落地（方案 13 §3.5 G2，A2a-4）。

    三态口径：全没配 = 无升配（不 fail）；全配 = 接上；配一半 =
    挡下报错——「缺件即报错，不猜」与主后端同纪律（半配静默当无升配，
    那一半配置就成了没人读的尸体）。
    """

    @pytest.fixture
    def clean_env(self, monkeypatch):
        for k in TestCLI注入.KEYS:
            monkeypatch.delenv(k, raising=False)

    @staticmethod
    def _set_main(monkeypatch):
        monkeypatch.setenv("TRAJECTORY_LLM_BASE_URL", "http://127.0.0.1:8000/v1")
        monkeypatch.setenv("TRAJECTORY_LLM_MODEL", "fake-model")

    def test_三键全没配则无升配且不报错(self, monkeypatch, clean_env):
        from trajectory_pipeline.executor import cli

        self._set_main(monkeypatch)
        monkeypatch.setenv(ENV_EXECUTOR_MODE, "agent")
        mode, backend, banner = cli._resolve_executor(None)
        assert mode == AGENT
        assert backend is not None and backend.escalate is None
        assert "升配" in banner and "未配置" in banner

    def test_三键配齐则升配接上(self, monkeypatch, clean_env):
        from trajectory_pipeline.executor import cli

        self._set_main(monkeypatch)
        monkeypatch.setenv(ENV_EXECUTOR_MODE, "agent")
        monkeypatch.setenv("TRAJECTORY_LLM_ESCALATE_BASE_URL",
                           "http://api.example.com/v1")
        monkeypatch.setenv("TRAJECTORY_LLM_ESCALATE_MODEL", "big-model")
        monkeypatch.setenv("TRAJECTORY_LLM_ESCALATE_API_KEY", "esc-secret")
        mode, backend, banner = cli._resolve_executor(None)
        assert mode == AGENT and backend is not None
        esc_cfg = backend.escalate.cfg
        assert esc_cfg.model == "big-model"
        assert esc_cfg.api_key == "esc-secret"
        # 凭据红线：banner 进终端与日志，绝不带 key
        assert "esc-secret" not in banner
        assert "big-model" in banner

    @pytest.mark.parametrize("partial", [
        {"TRAJECTORY_LLM_ESCALATE_BASE_URL": "http://api.example.com/v1"},
        {"TRAJECTORY_LLM_ESCALATE_MODEL": "big-model"},
    ])
    def test_只配一半则挡下并说明缺什么(self, monkeypatch, clean_env, partial):
        from trajectory_pipeline.executor import cli

        self._set_main(monkeypatch)
        for k, v in partial.items():
            monkeypatch.setenv(k, v)
        backend, why = cli._make_agent_backend()
        assert backend is None
        assert "升配" in why                     # 说清是哪一端配置不合法
        assert "TRAJECTORY_LLM_ESCALATE_" in why  # 给出能照着改的键名

    def test_描述文案两种形态(self):
        from trajectory_pipeline.executor import cli

        class _Cfg:
            def __init__(self, model):
                self.model = model

        class _Client:
            def __init__(self, model):
                self.cfg = _Cfg(model)

        from trajectory_pipeline.executor.agent_loop import AgentBackend
        plain = AgentBackend(client=_Client("main-7b"),
                             json_of=lambda raw: None)
        assert "main-7b" in cli._describe_agent(plain)
        assert "未配置" in cli._describe_agent(plain)
        with_esc = AgentBackend(client=_Client("main-7b"),
                                json_of=lambda raw: None,
                                escalate=_Client("big-model"))
        text = cli._describe_agent(with_esc)
        assert "main-7b" in text and "big-model" in text
        assert "未配置" not in text
