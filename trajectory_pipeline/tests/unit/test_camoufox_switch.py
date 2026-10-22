"""**切 camoufox 为主力执行层**之后的门禁（2026-10-22 owner 裁定）。

owner 裁定
----------
「不计划再继续比对 obscura 与 camoufox，**选定 camoufox 做默认浏览器
调用主力**」——理由是反爬拦截率与页面内容获取是主目标，速度次要，
可以接受增加等待时间。

本文件盯的是**这次切换留下的接缝**。切换本身是一次「默认值搬家」，
而默认值搬家最危险的地方在于**它不报错**：

- 默认实现改了，但**调用点没钉住** ⇒ 一条命令跑 camoufox、另一条跑
  obscura，两边产出物**结构一模一样**——混池统计时**看不出来**；
- 指纹只在 launch 那一刻确定，**事后补不回来**⇒ 没进存档，这批
  的执行层几何就永远丢了；
- 单站超时从 90 抬到 180 是**必需**的配套：camoufox 单步慢一个量级，
  撞 90s 上限会被记成 ``unreachable_hard``——**理由通顺的假负样本**，
  而假负样本进了池，模型学到的是「这类站点了不得」。

⚠️ **测试不许真起浏览器**。``CamoufoxSession.launch`` 会**真的下载
fpgen 模型**并起进程——被 ``pytest-socket`` 拦成红，且慢到不可接受。
所以本文件一律打桩 ``cli._launch_camoufox`` 这一个缝。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trajectory_pipeline.executor import cli
from trajectory_pipeline.executor.browser.driver_factory import (
    DEFAULT_KIND,
    SUPPORTED_KINDS,
)
from trajectory_pipeline.executor.orchestrator import RunConfig


# ─────────────── 替身 ───────────────


class _FakeSession:
    """假 camoufox 会话。记 launch/close 次数与 headless 参数。"""

    def __init__(self) -> None:
        self.closed = 0
        self.fingerprint = {
            "os": "windows", "screen": {"width": 1920, "height": 1080},
            "window": {"inner_width": 1905, "inner_height": 940},
            "dpr": 1.0, "humanize": True, "geoip": False,
            "main_world_eval": True, "headless": True, "locale": "zh-CN",
        }

    async def close(self) -> None:
        self.closed += 1


def _stub_launch(monkeypatch, sessions: list[_FakeSession] | None = None):
    """打桩 ``cli._launch_camoufox``。**不真起浏览器**。"""
    made = sessions if sessions is not None else []

    async def _launch(*, headless: bool = True):
        s = _FakeSession()
        s.fingerprint["headless"] = headless
        made.append(s)
        return s

    monkeypatch.setattr(cli, "_launch_camoufox", _launch)
    return made


# ─────────────── 默认值 ───────────────


class Test默认实现:
    def test_默认实现是camoufox(self):
        assert DEFAULT_KIND == "camoufox"

    def test_默认实现在白名单里(self):
        assert DEFAULT_KIND in SUPPORTED_KINDS

    def test_两个实现都还留着(self):
        """obscura **没被删掉**——它是存量产物的方言与口径回溯参照系。
        「不再比对」≠「删掉实现」。"""
        assert set(SUPPORTED_KINDS) == {"obscura", "camoufox"}


# ─────────────── 指纹进 P1（P6） ───────────────


class Test指纹进存档:
    def test_驱动有指纹就落进存档(self):
        """指纹**必须**跟着 driver 一起进存档。

        它只在 launch 那一刻确定——事后再去问浏览器，已经没有这一批
        的读数了。不记它，批次分布的变化会被读成「站点变了」。
        """
        from trajectory_pipeline.executor.orchestrator import RunRecord

        rec = RunRecord(task_id="T001", title="x", query="q", search_url="u")
        rec.fingerprint = dict(_FakeSession().fingerprint)
        js = rec.to_json()
        assert js["fingerprint"]["humanize"] is True
        assert js["fingerprint"]["window"]["inner_width"] == 1905

    def test_空指纹是空对象而非缺字段(self):
        """**缺字段与空 dict 在切片脚本里长得一样**——缺字段会被读成
        「未知」。obscura 批次就是「没记」（远端 MCP 看不见），
        但它仍然是**一个记好的空对象**。"""
        from trajectory_pipeline.executor.orchestrator import RunRecord

        js = RunRecord(task_id="T001", title="x", query="q",
                       search_url="u").to_json()
        assert js["fingerprint"] == {}

    def test_序列化后仍是副本(self):
        from trajectory_pipeline.executor.orchestrator import RunRecord

        fp = {"humanize": True}
        rec = RunRecord(task_id="T001", title="x", query="q", search_url="u",
                        fingerprint=fp)
        fp["humanize"] = False
        assert rec.to_json()["fingerprint"]["humanize"] is True

    def test_编排器从驱动取指纹(self):
        """**接线点**也要钉住：``RunRecord.fingerprint`` 的来源是
        ``self._driver.fingerprint``。取错（比如取成请求值而不是实测值）
        不会报错，只会读到一份假的读数。"""
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "executor"
               / "orchestrator.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) > 1
                    and isinstance(node.args[0], ast.Attribute)
                    and node.args[0].attr == "_driver"
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "fingerprint"):
                found = True
        assert found, "编排器不再从驱动取指纹了——P6 的接线断了"


# ─────────────── 单站超时 ───────────────


class Test单站超时:
    def test_默认值抬到180(self):
        """90 是 **obscura 时代**的数。

        camoufox 单次点击实测上界 13.3s（真人鼠标轨迹回放），8 步
        ≈ 122s ⇒ 90s **必然触顶**。触顶不是「超时」这么轻：它会被记成
        ``unreachable_hard``——一条理由通顺的假负样本。
        """
        assert RunConfig.per_site_timeout_s == 180.0

    def test_命令行参数能覆盖(self, tmp_path, monkeypatch):
        """``--per-site-timeout`` 落进 ``run_config``。

        B-4（异常处置）唯一的时限判据是 ``run_config`` 里那一项——
        不落盘等于没有判据。
        """
        seen: list[RunConfig] = []

        class _Driver:
            name = "fake"

            async def goto(self, url):
                return None

            async def observe(self, **kw):
                return None

            async def close(self):
                return None

        class _Perceptor:
            name = "rule"

            async def evaluate(self, q, obs):
                return None

        def _fake_make_driver(client, **kw):
            return _Driver()

        monkeypatch.setattr(cli, "make_driver", _fake_make_driver)
        monkeypatch.setattr(cli.McpClient, "from_env",
                            staticmethod(lambda: SimpleNamespace(
                                info=SimpleNamespace(name="f", version="0",
                                                     tool_count=0))))

        args = argparse.Namespace(
            plan=None, out=str(tmp_path), limit=1, title=["功夫"],
            dry_run=False, task_id="T001", engine="bing", max_candidates=1,
            max_chars=None, stop_after_success=0, perceptor=None,
            discovery_path="direct", max_intermediates=3,
            max_referral_links=5, executor_mode=None, driver="obscura",
            per_site_timeout=42.0, element_basis=False, headful=False,
        )
        # 只验参数落位，不跑整条链：把 ``RunConfig`` 的构造截住。
        import dataclasses

        real = cli.RunConfig

        def _spy(**kw):
            seen.append(real(**kw))
            return real(**kw)

        monkeypatch.setattr(cli, "RunConfig", _spy)
        try:
            asyncio.run(cli._run_manual(args))
        except Exception:
            pass          # 跑不完没关系：参数落位已经记下了
        assert seen, "RunConfig 没被构造——参数根本没往下传"
        assert seen[0].per_site_timeout_s == 42.0

    def test_不给就走源码默认值(self):
        """**不传 ≠ 传 0**。没给就是「用默认值」，不是「用 0」。

        ⚠️ 这里踩过的坑：取值的写法若是 ``value or DEFAULT``，
        ``0`` 会被静默读成默认 180——而 0 的业务含义是「立刻超时」，
        配错了就该当场报错。
        """
        args = argparse.Namespace(per_site_timeout=None)
        assert cli._per_site_timeout(args) == RunConfig.per_site_timeout_s

    def test_非正数被拒(self):
        """0 / 负数 / 非数字 / inf 一律拒绝。

        放行会产出一批「立刻超时、看起来正常的空批」——那是最贵的一种
        静默：它有产出、有存档、只是产出全是空的。
        """
        for bad in ("0", "-1", "abc", "inf", "nan"):
            with pytest.raises(SystemExit):
                cli.main(["run", "--title", "功夫",
                          "--per-site-timeout", bad])


# ─────────────── 探针工具 ───────────────


class Test探针驱动:
    async def test_camoufox分支起一次会话并关掉(self, monkeypatch):
        made = _stub_launch(monkeypatch)
        async with cli._probe_driver("camoufox") as driver:
            assert driver.name == "camoufox"
        assert len(made) == 1, "一条命令一条会话——每 URL 启一次太慢"
        assert made[0].closed == 1, "会话没关——浏览器进程会留着"

    async def test_obscura分支不起camoufox会话(self, monkeypatch):
        made = _stub_launch(monkeypatch)

        class _Client:
            info = SimpleNamespace(name="f", version="0", tool_count=0)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(cli.McpClient, "from_env", staticmethod(lambda: _Client()))
        async with cli._probe_driver("obscura") as driver:
            assert driver.name == "obscura"
        assert not made, "obscura 分支不该起 camoufox 会话"

    async def test_认不出的实现报错不回落(self, monkeypatch):
        _stub_launch(monkeypatch)
        with pytest.raises(ValueError):
            async with cli._probe_driver("playwright"):
                pass


class TestProberun落盘:
    async def test_存档记driver与fingerprint(self, tmp_path, monkeypatch):
        """proberun 的存档**必须**记 driver——它不记就会被折算成
        obscura-1 面渲染，而真实调用是另一种编码：
        「声明与真实调用对不上」，报告上看不出异常。"""
        _stub_launch(monkeypatch)
        urls = tmp_path / "urls.txt"
        urls.write_text("https://example.test/a\n", encoding="utf-8")
        out = tmp_path / "out"

        class _Driver:
            name = "camoufox"
            fingerprint = {"humanize": True, "os": "windows"}

            async def goto(self, url):
                return None

            async def observe(self, **kw):
                return None

            async def snapshot(self):
                return ""

            async def evaluate(self, js):
                return None

            async def close(self):
                return None

        monkeypatch.setattr(cli, "make_driver",
                            lambda *a, **kw: _Driver())
        monkeypatch.setattr(cli, "_make_perceptor",
                            lambda *a, **kw: SimpleNamespace(name="rule"))

        from trajectory_pipeline.executor.steps.visit import VisitResult
        from trajectory_pipeline.executor.steps.search import Candidate

        monkeypatch.setattr(
            cli, "visit_site",
            _async_return(VisitResult(
                candidate=Candidate(url="https://example.test/a",
                                    text="a", rank=0),
                landed_url="https://example.test/a", success=False,
                notes=[])))

        args = argparse.Namespace(
            from_urls=str(urls), out=str(out), title="功夫", task_id="T001",
            engine="bing", max_chars=None, perceptor=None,
            driver="camoufox", headful=False,
        )
        rc = await cli.cmd_proberun(args)
        assert rc == 0
        files = list((out / "proberun").glob("*.json"))
        assert files, "proberun 没落盘"
        doc = json.loads(files[0].read_text(encoding="utf-8"))
        assert doc["driver"] == "camoufox"
        assert doc["fingerprint"] == {"humanize": True, "os": "windows"}


class Test调用点钉住实现:
    """**默认值搬家会静默走错实现。**

    ``DEFAULT_KIND`` 从 obscura 换成 camoufox 之后，任何一处没钉住
    ``kind=`` 的 ``make_driver`` 调用都会**默默地**按新默认值走：
    obscura 专属的代码路径（拿 MCP 连接造驱动）会当场抛 ``ValueError``，
    camoufox 专属的路径会拿到一个 obscura 驱动。

    ⚠️ **这个 bug 真的发生过**：手工路径（``run --task-id``）钉了
    ``kind="obscura"``，**计划路径（``run --plan``）漏了**——而后者才是
    批量生产入口。手工路径不炸、计划路径炸 ⇒ 只测过手工路径的用例
    完全发现不了。这正是「默认值不能是隐式约定」的原因。
    """

    def test_cli里每处make_driver都显式传kind(self):
        """**通用守卫**：``cli.py`` 里每一处 ``make_driver`` 调用都必须
        显式传 ``kind``。

        写成 AST 断言而不是逐个行为测试，是因为要防的是**下一处**漏写
        ——逐个行为测试只能守住已经写过的调用点。
        """
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2] / "executor"
               / "cli.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "make_driver"):
                if not any(kw.arg == "kind" for kw in node.keywords):
                    offenders.append(f"cli.py:{node.lineno}")
        assert not offenders, (
            "这些 make_driver 调用没显式传 kind ⇒ 它们跟着 DEFAULT_KIND 走，"
            f"而 DEFAULT_KIND 已经变成 camoufox：{offenders}"
        )

    def test_各命令的driver缺省都跟着DEFAULT_KIND(self):
        """**每条命令的 ``--driver`` 缺省必须是 ``DEFAULT_KIND``**。

        ``driver_factory`` 的模块 docstring 写着「改这一处就切生产链——
        它不是六处调用点各自的隐式约定」。而 ``check`` 曾把 ``"obscura"``
        硬编码在自己的 parser 里：那是**第二份隐式约定**，下次切换会漂。

        症状：自检默认探的**不是生产面** ⇒ 读数不能外推到生产批次
        （与「工具声明按面取」同一条纪律），而报告上看不出异常。

        ⚠️ 唯一例外是 ``mediabrowse --driver fake``——它是**离线 in-process
        mock**，默认不联网是刻意的（见命令的 help 文本）。它不是
        「执行层实现」，不参与生产面口径。
        """
        parser = cli.build_parser()
        sub = [a for a in parser._actions
               if isinstance(a, argparse._SubParsersAction)][0]
        for name, sp in sub.choices.items():
            action = next((a for a in sp._actions
                           if a.option_strings == ["--driver"]), None)
            if action is None:
                continue
            if name == "mediabrowse":
                assert action.default == "fake", (
                    "mediabrowse 的 --driver 缺省是离线 mock（fake），"
                    "不是执行层实现"
                )
                continue
            assert action.default == DEFAULT_KIND, (
                f"{name} 的 --driver 缺省是 {action.default!r}，"
                f"不是 DEFAULT_KIND（{DEFAULT_KIND!r}）——"
                "那是第二份隐式约定，下次切换会漂"
            )

    async def test_计划路径的obscura调用点钉住了实现(self, tmp_path, monkeypatch):
        """``run --plan --driver obscura`` 必须是回溯路径，不是炸。

        症状（漏钉时）：``ValueError: camoufox 实现需要 CamoufoxSession，
        未提供``——因为 ``make_driver(client)`` 按新默认走 camoufox 分支，
        而 camoufox 分支要的是 session 不是 client。
        """
        from trajectory_pipeline.executor.browser.driver_factory import (
            make_driver as real_make_driver,
        )

        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(_plan_doc(), ensure_ascii=False),
                        encoding="utf-8")

        kinds: list[str | None] = []

        def _spy_make_driver(client=None, **kw):
            kinds.append(kw.get("kind"))
            # 交给**真的** factory：漏钉时它抛的正是生产上那条 ValueError。
            real_make_driver(client, **kw)
            raise _StopAfterDriver()

        class _Client:
            info = SimpleNamespace(name="f", version="0", tool_count=0)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(cli.McpClient, "from_env", staticmethod(lambda: _Client()))
        monkeypatch.setattr(cli, "make_driver", _spy_make_driver)

        args = argparse.Namespace(
            plan=str(plan), title=None, include_unrewritten=False,
            limit=None, dry_run=False, driver="obscura", executor_mode=None,
            engine="bing", max_candidates=1, max_chars=None,
            stop_after_success=0, discovery_path="direct",
            max_intermediates=3, max_referral_links=5,
            per_site_timeout=None, out=str(tmp_path), task_id="T001",
            element_basis=False, headful=False,
        )
        with pytest.raises(_StopAfterDriver):
            await cli._run_plan(args)
        assert kinds == ["obscura"], (
            f"计划路径的 obscura 驱动造错了实现：{kinds}"
        )


class _StopAfterDriver(Exception):
    """在驱动造好之后打断——后面的活不是本测试要验的。"""


def _plan_doc() -> dict:
    """一份最小的可跑计划（结构与 ``executor/plan.py`` 的读入口径一致）。"""
    return {
        "report": {"seed": 7, "library_digest": "abc123",
                   "by_mode": {"single_title": 1}},
        "instances": [{
            "task_id": "T001",
            "persona_id": "parent-喜剧",
            "scenario_id": "media_lookup_standard",
            "prompt_text": "我想找到《功夫》电影在线观看的可播放网址",
            "search_query": "功夫 在线观看",
            "has_standard": True,
            "retrieval_mode": "single_title",
            "title": "功夫",
            "rewritten": True,
            "provenance": {"persona_id": "parent-喜剧"},
        }],
    }


class TestMediabrowse:
    async def test_camoufox分支被接上(self, tmp_path, monkeypatch):
        """``mediabrowse --driver camoufox`` 要真的走 camoufox 会话。

        L2 要在**真实生产面**上采两次 ``currentTime`` 判「真推进」——
        探针跑的浏览器不是生产面，结论就不能外推。
        """
        made = _stub_launch(monkeypatch)
        urls = tmp_path / "urls.txt"
        urls.write_text("https://example.test/v\n", encoding="utf-8")

        class _Driver:
            name = "camoufox"

            async def goto(self, url):
                return None

            async def evaluate(self, js, *a, **kw):
                return None

            async def snapshot(self):
                return ""

            async def close(self):
                return None

        monkeypatch.setattr(cli, "make_driver",
                            lambda *a, **kw: _Driver())
        monkeypatch.setattr(
            cli, "_mediabrowse_with_driver",
            _async_return(0))

        args = argparse.Namespace(
            from_urls=str(urls), title="功夫", out=str(tmp_path),
            task_id="P023", driver="camoufox", headful=False,
            timeout=15.0, wait_s=2.0, no_l1=True, l1_timeout=10.0,
        )
        rc = await cli.cmd_mediabrowse(args)
        assert rc == 0
        assert len(made) == 1, "mediabrowse 没走 camoufox 会话"
        assert made[0].closed == 1, "会话没关"


def _async_return(value):
    """造一个「返回固定值」的协程函数。"""
    async def _f(*a, **kw):
        return value
    return _f


# ── 扫不到新批次：四条「产物扫描器」的通用守卫 ──────────────────────
#
# ⚠️ **为什么这一组是通用的、不是逐点的**：2026-10-22 第一批 camoufox
# 生产批落盘时抓到四条同源漏——`test_element_dialect._run_records` /
# `test_p1_contract._real_archives` / `test_p2_contract._real_archives`
# 三处都用 ``glob("*.json")``（只扫顶层），加上 `_ARCHIVE_NAME`
# 只放行两段名（把全部 P2 样本挡掉）。
#
# 四条的共同形状是**没有一条会报错**：扫得少 ⇒ 断言在残缺分母上照常绿。
# 逐点测试只能盯住这四处，**下一处新写的扫描器照样会漏**——所以这里
# 改成**扫全仓**：任何测试里出现「读产物目录的非递归 glob」就红。


class Test产物扫描不许只看顶层:
    """读 ``output/pipeline/`` 的扫描器**必须递归**。

    为什么这条值钱：批次落子目录是常态（``dualrun/`` 是双跑、
    ``camoufox_prod1/`` 是第一批生产批、``camoufox_smoke/`` 是冒烟）。
    只扫顶层 ⇒ 新批次**压根没进视野**⇒「存量口径」那类断言给出的
    是**假绿**——它核的还是上一批老数据，而报告上看不出任何异常。

    ⚠️ **只盯真实产物目录，不盯测试替身**：``tmp_path`` 上的
    ``glob("*.json")`` 是**对的**——那些测试造的是平铺结构，
    让它们递归反而会扫到别处去。所以判据是**扫的那个根是不是产物目录**，
    不是「有没有用 glob」。
    """

    #: **会随批次长出子目录**的产物根 —— 扫它们必须递归。
    #:
    #: 只有 ``output/pipeline/``（及其派生根）会长子目录：``--out``
    #: 可以给任意目录（``dualrun/`` 是双跑、``camoufox_prod1/`` 是第一批
    #: 生产批、``camoufox_smoke/`` 是冒烟）。
    #:
    #: ⚠️ **``SAMPLES`` / ``samples_dir()``（= ``output/pipeline/samples/``）
    #: 不在此列，而且这是对的**：``archive.py`` 的 ``DEFAULT_ROOT``
    #: 长注释明写「**平铺已是既定事实**，不是随手选的——4 处消费方
    #: 按文件名模式读它」。P2 样本按 ``<task>__<run_id>__<slot>.json``
    #: 平铺（实测 361 个文件 / **0 个子目录**）。**它现在只扫一层是对的。**
    #:
    #: 哪天真给 P2 开目录（``samples/<run_id>/``），改的是那个常量与
    #: 4 处消费方，**不是**先把这几处 glob 改成 rglob——那样会让它们
    #: 在改契约之前先扫到一个空目录，然后静默地什么都读不到。
    _PIPELINE_DIR = "pipeline"
    #: 明确的**测试替身**：这些根下面造的是平铺结构，扫一层才对。
    _TEST_DOUBLES = frozenset({"tmp_path", "out", "views_dir", "samples_dir"})

    def test_读产物目录的地方不许用非递归glob(self):
        offenders: list[str] = []
        for path in sorted(
            (Path(__file__).resolve().parents[1] / "contract").glob("test_*.py")
        ):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # 只盯 ``X.glob("*.json")`` 这种「扫一层」的形态；
                # ``rglob`` 本身带 r，不匹配这个模式。
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "glob"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and node.args[0].value.endswith(".json")):
                    continue
                if self._root_is_pipeline(path, tree, node):
                    offenders.append(f"{path.name}:{node.lineno}")
        assert not offenders, (
            "这些地方读 output/pipeline/ 却用 glob(\"*.json\") 只扫一层，"
            "子目录下的批次（dualrun/ camoufox_prod1/ camoufox_smoke/ …）"
            f"会静默漏掉：{offenders}。要扫整棵子树用 rglob，或按形状认。"
        )

    def _root_is_pipeline(self, path: Path, tree, node) -> bool:
        """``node`` 扫的那个根是不是 ``output/pipeline``（或它的子树）。

        **按语义判，不按变量名判**：第一版守卫把
        ``{DEFAULT_ROOT, PIPELINE}`` 列成白名单，结果
        ``test_p1_contract`` 里那个**局部变量** ``root`` 没被认出来
        ——回退它的 ``rglob`` 守卫照样绿。**白名单会漏，而漏的方式正是
        这个类要防的那种**（静默）。

        判法：根的赋值链里出现 ``"output" / "pipeline"`` 字面量（模块级
        常量与局部变量一视同仁），**或**它就是 ``DEFAULT_ROOT`` /
        ``PIPELINE`` 这两个名字。测试替身（``tmp_path`` / ``out`` /
        ``samples_dir``）显式排除。
        """
        recv = node.func.value
        if isinstance(recv, ast.Call) and isinstance(recv.func, ast.Name):
            if recv.func.id in self._TEST_DOUBLES:
                return False
            recv = recv.func
        if isinstance(recv, ast.Name):
            if recv.id in self._TEST_DOUBLES:
                return False
            if recv.id in ("DEFAULT_ROOT", "PIPELINE"):
                return True
        # 往上追赋值：`root = REPO / "trajectory_pipeline" / "output" / "pipeline"`
        for parent in ast.walk(tree):
            targets = getattr(parent, "targets", None) or ()
            if not targets:
                continue
            for tgt in targets:
                if (isinstance(tgt, ast.Name) and isinstance(recv, ast.Name)
                        and tgt.id == recv.id
                        and "pipeline" in ast.dump(parent.value)):
                    return True
        return False

    def test_归档名正则必须放行三段名(self):
        """P2 样本是 ``<task>__<run_id>__<slot>.json``（三段），
        P1 存档是两段。**只放行两段 ⇒ 全部 P2 样本被挡在门外**。

        后果不是报错，是**静默换统计源**：``_observed_from_p2`` 恒返回 0
        ⇒ ``_observed`` 走 fallback 按 P1 算 ⇒ ``required`` 的统计口径
        与它自己文档里写的（「统计源选 P2 样本，训练数据是 P2」）不一致。
        两个口径结论相近时差异会被抹平，所以**永远不会自己显形**。
        """
        from trajectory_pipeline.tests.contract.test_tool_catalog_contract import (
            _ARCHIVE_NAME,
        )
        assert _ARCHIVE_NAME.match("T001__67a05faa.json"), "P1 两段名该放行"
        assert _ARCHIVE_NAME.match("T014__200a9529__search.json"), (
            "P2 样本是三段名（多了 slot），正则不放行它 ⇒ "
            "required 的首选统计源恒为空，静默退回 P1"
        )
