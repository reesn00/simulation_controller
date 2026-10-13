"""L2 浏览器媒体探针 CLI 测试——2026-10-13 增量二十三配套。

盯的目标：
1. CLI 分发：mediabrowse 子命令能被 cli.main() 正确路由到 cmd_mediabrowse。
2. FakePageDriver 作为 PageDriver 协议的最小实现：
   走 probe_browser_media 后 to_browser_branch() 返 None（passes）。
3. fake driver 调用契约：CLI 调用 goto + evaluate 两次；
   凭据闸门命中 → 不再调用 driver。
4. --driver obscura 闸门：exit code 3，不调 driver。
5. 错误码契约：参数错 → exit 2；缺 --title → exit 2。
"""

from __future__ import annotations

import argparse
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from trajectory_pipeline.executor import cli as cli_mod
from trajectory_pipeline.executor.cli import (
    FakePageDriver,
    cmd_mediabrowse,
)
from trajectory_pipeline.executor.probe import (
    probe_browser_media,
    to_browser_branch,
)


# ─────────────── helpers ───────────────

def _write_urls(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _patch_driver(monkeypatch, driver: FakePageDriver) -> None:
    """把 cmd_mediabrowse 里的 FakePageDriver 钩到测试用的实例。"""
    monkeypatch.setattr(cli_mod, "FakePageDriver", lambda: driver)


def _make_fake_mcp_factory():
    """建一个可记录进/出次数 + 实例列表的 fake McpClient。

    返 (factory_fn, instances, counter)；test 可断言 counter['calls'] == 1
    验「连接复用」/断言 instances[0].entered 验 async with。
    """
    class FakeMcpClient:
        def __init__(self):
            self.entered = False
            self.exited = False
            self.info = SimpleNamespace(
                name="test-obscura", version="0.0.1", tool_count=0,
            )

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, *exc):
            self.exited = True
            return False

    instances: list[FakeMcpClient] = []
    counter = {"calls": 0}

    def factory():
        counter["calls"] += 1
        inst = FakeMcpClient()
        instances.append(inst)
        return inst

    return factory, instances, counter


def _make_fake_obscura_driver_cls(driver_recorder: list[str]):
    """建一个用调用记录器做地基的 fake ObscuraDriver class.

    record 是 list of tuple(method, arg)；外部 monkeypatch cli_mod.ObscuraDriver
    后, cli 内部 `ObscuraDriver(client)` 走这个 class, 调 goto/evaluate 都记录。
    """
    class FakeObscuraDriver:
        def __init__(self, client):
            self.client = client
            self.name = "obscura"

        async def goto(self, url, *, wait_until="load"):
            driver_recorder.append(("goto", url))

        async def evaluate(self, expression):
            driver_recorder.append(("evaluate", expression))
            return {"count": 0, "media": []}

        async def snapshot(self, *, max_chars=None, degraded=None):
            # 增量二十五:L4 文本采集让 fake driver 也走 .snapshot() ——
            # 真实 ObscuraDriver 返 CleanedSnapshot,fake 简化返字符串。
            driver_recorder.append(("snapshot", str(max_chars)))
            if degraded is not None:
                degraded.append("fake_snapshot")
            return ""

        async def close(self):
            driver_recorder.append(("close", None))

    return FakeObscuraDriver


# ─────────────── TestFakePageDriver ───────────────

class TestFakePageDriver:
    """验证 FakePageDriver 作为 PageDriver 协议子集的最小实现。

    L2 探针只触达 name / goto / evaluate 三个原语；
    其他原子原语（snapshot / links / interactive / click 等）靠
    鸭子类型按需调用，本测试不覆盖。
    """

    def test_name_is_fake(self):
        assert FakePageDriver().name == "fake"

    async def test_goto_records_url(self):
        d = FakePageDriver()
        await d.goto("https://example.com/video")
        assert ("goto", "https://example.com/video") in d.calls

    async def test_evaluate_returns_empty_payload(self):
        d = FakePageDriver()
        out = await d.evaluate("() => ({count:0,media:[]})")
        assert out == {"count": 0, "media": []}
        assert any(c[0] == "evaluate" for c in d.calls)

    async def test_close_clears_calls(self):
        d = FakePageDriver()
        await d.goto("https://x")
        await d.evaluate("() => 1")
        assert len(d.calls) == 2
        await d.close()
        assert d.calls == []


# ─────────────── TestFakeDriverProbe ───────────────

class TestFakeDriverProbe:
    """probe_browser_media + FakePageDriver 跑通 happy path：
    media_count=0 → to_branch() 返 None（passes）。"""

    async def test_probe_with_fake_driver_passes(self):
        driver = FakePageDriver()
        result = await probe_browser_media(
            driver, "https://example.com/video",
            timeout_s=5.0, wait_s=0.01,
        )
        assert result.ok is True
        assert result.media_count == 0
        assert result.driver_name == "fake"
        sig = result.signal
        assert sig.play_started is False
        assert sig.timeupdate_progress is False
        assert sig.current_time_sec is None
        assert sig.frames_decoded is None
        assert sig.black_screen is False
        assert sig.media_error is None
        assert any(c[0] == "evaluate" for c in driver.calls)
        assert to_browser_branch(result) is None

    async def test_probe_with_no_driver_is_fail_closed(self):
        from trajectory_pipeline.executor.probe.media_probe import (
            MediaSignal,
        )
        result = await probe_browser_media(
            None, "https://example.com",
            timeout_s=5.0, wait_s=0.01,
        )
        assert result.ok is False
        assert result.note == "driver_missing"
        assert result.driver_name == ""
        assert result.signal == MediaSignal(
            False, False, None, None, False, None
        )


# ─────────────── TestCLIDispatch ───────────────

class TestCLIDispatch:
    """mediabrowse 子命令的分发 + 参数校验。"""

    def test_subparser_is_registered(self):
        parser = cli_mod.build_parser()
        sub_action = next(
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        assert "mediabrowse" in sub_action.choices

    def test_subparser_binds_cmd_mediabrowse(self):
        parser = cli_mod.build_parser()
        sub_action = next(
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        chosen = sub_action.choices["mediabrowse"]
        assert chosen.get_default("func") is cmd_mediabrowse

    def test_cmd_mediabrowse_is_async(self):
        assert inspect.iscoroutinefunction(cmd_mediabrowse)

    def test_subparser_default_driver_is_fake(self):
        parser = cli_mod.build_parser()
        sub_action = next(
            a for a in parser._actions
            if isinstance(a, argparse._SubParsersAction)
        )
        ns = sub_action.choices["mediabrowse"].parse_args(
            ["--from-urls", "x.txt", "--title", "t"]
        )
        assert ns.driver == "fake"
        assert ns.task_id == "P023"
        assert ns.wait_s == 2.0
        assert ns.timeout == 15.0


# ─────────────── TestArgsValidation ───────────────

class TestArgsValidation:
    """错误码契约——参数错 → exit 2。"""

    async def test_missing_from_urls_returns_2(self, capsys):
        ns = argparse.Namespace(
            from_urls=None, task_id="P023", title="t",
            timeout=15.0, wait_s=2.0, driver="fake", out=None,
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 2
        assert "--from-urls" in capsys.readouterr().err

    async def test_missing_title_returns_2(self, tmp_path, capsys):
        urls = _write_urls(tmp_path / "u.txt", ["https://example.com"])
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P023", title=None,
            timeout=15.0, wait_s=2.0, driver="fake", out=None,
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 2
        assert "--title" in capsys.readouterr().err

    async def test_unknown_driver_returns_2(self, tmp_path, capsys):
        urls = _write_urls(tmp_path / "u.txt", ["https://example.com"])
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P023", title="t",
            timeout=15.0, wait_s=2.0, driver="playwright", out=None,
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 2
        err = capsys.readouterr().err
        assert "playwright" in err

    async def test_obscura_without_env_returns_3(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("OBSCURA_EXE", raising=False)
        urls = _write_urls(tmp_path / "u.txt", ["https://example.com"])
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P023", title="t",
            timeout=15.0, wait_s=2.0, driver="obscura", out=None,
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 3
        err = capsys.readouterr().err
        assert "OBSCURA_EXE" in err


# ─────────────── TestHappyPathWritesArtifacts ───────────────

class TestHappyPathWritesArtifacts:
    """happy path：fake driver 跑 2 条 URL → rc=0 + branches = passes。

    同时验：凭据闸门命中（user:pass@ URL 形态）→ driver 不被调用。
    """

    async def test_runs_two_urls_passes(self, tmp_path, monkeypatch, capsys):
        driver = FakePageDriver()
        _patch_driver(monkeypatch, driver)
        urls = _write_urls(
            tmp_path / "u.txt",
            [
                "https://example.com/v1",
                "https://example.com/v2",
            ],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P023-test", title="smoke",
            timeout=5.0, wait_s=0.01, driver="fake", out=str(tmp_path),
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        gotos = [c for c in driver.calls if c[0] == "goto"]
        evaluates = [c for c in driver.calls if c[0] == "evaluate"]
        assert len(gotos) == 2
        assert len(evaluates) == 4
        out = capsys.readouterr().out
        assert "'passes': 2" in out

    async def test_credential_url_blocks_before_driver(
        self, tmp_path, monkeypatch, capsys
    ):
        """URL 内嵌口令（命中 user:pass@ 条目）→ 凭据闸门 fail-closed，
        driver 不被调用。"""
        driver = FakePageDriver()
        _patch_driver(monkeypatch, driver)
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://user:pass@example.com/v"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P023-leak", title="leak",
            timeout=5.0, wait_s=0.01, driver="fake", out=str(tmp_path),
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        out_err = capsys.readouterr()
        assert (
            "credential" in out_err.out.lower()
            or "credential" in out_err.err.lower()
        )
        assert driver.calls == []


# ─────────────── TestObscuraDriverPath ───────────────

class TestObscuraDriverPath:
    """--driver obscura 真实路径：McpClient 连接复用 + 错误闸门契约。

    与 fake 路径的差别只在 cmd_mediabrowse 顶层 — 主循环同款 _mediabrowse_with_driver。
    重点盯连接复用（from_env 仅调一次）+ async with 进/出 + probe 调用 driver +
    McpError fail-closed 退出码 3。
    """

    async def test_obscura_connection_reused_for_batch(
        self, tmp_path, monkeypatch, capsys,
    ):
        # 跑 3 URL;McpClient.from_env 仅调 1 次（不每步重连）
        factory, instances, counter = _make_fake_mcp_factory()
        monkeypatch.setattr(
            cli_mod, "McpClient",
            SimpleNamespace(from_env=staticmethod(factory)),
        )
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr(
            cli_mod, "ObscuraDriver",
            _make_fake_obscura_driver_cls(recorder),
        )
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://a.example.com/1",
             "https://a.example.com/2",
             "https://a.example.com/3"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P024-reuse", title="t",
            timeout=15.0, wait_s=0.01, driver="obscura", out=None,
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        assert counter["calls"] == 1
        assert len(instances) == 1
        # 3 URL → 3 次 goto（每 URL 一次）
        gotos = [c for c in recorder if c[0] == "goto"]
        assert len(gotos) == 3

    async def test_obscura_async_with_entered_and_exited(
        self, tmp_path, monkeypatch, capsys,
    ):
        # 验证 cmd_mediabrowse 走 `async with client:` 包裹整批
        factory, instances, counter = _make_fake_mcp_factory()
        monkeypatch.setattr(
            cli_mod, "McpClient",
            SimpleNamespace(from_env=staticmethod(factory)),
        )
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr(
            cli_mod, "ObscuraDriver",
            _make_fake_obscura_driver_cls(recorder),
        )
        urls = _write_urls(
            tmp_path / "u.txt", ["https://a.example.com/1"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P024-enter", title="t",
            timeout=15.0, wait_s=0.01, driver="obscura", out=None,
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        assert len(instances) == 1
        client = instances[0]
        assert client.entered is True, "cmd 应走 async with client: 进入"
        assert client.exited is True, "cmd 应走 async with client: 退出"

    async def test_obscura_probe_calls_driver_per_url(
        self, tmp_path, monkeypatch, capsys,
    ):
        # 增量二十五 L4 串接后契约：
        # 1 URL = 1 goto + 2 L2 evaluate(双采样) + 1 L4 evaluate(innerText) + 1 L4 snapshot()
        factory, instances, counter = _make_fake_mcp_factory()
        monkeypatch.setattr(
            cli_mod, "McpClient",
            SimpleNamespace(from_env=staticmethod(factory)),
        )
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr(
            cli_mod, "ObscuraDriver",
            _make_fake_obscura_driver_cls(recorder),
        )
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://a.example.com/1", "https://a.example.com/2"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P024-probe", title="t",
            timeout=15.0, wait_s=0.01, driver="obscura", out=str(tmp_path),
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        gotos = [c for c in recorder if c[0] == "goto"]
        evaluates = [c for c in recorder if c[0] == "evaluate"]
        snapshots = [c for c in recorder if c[0] == "snapshot"]
        assert len(gotos) == 2
        # 2 L2 evaluate per URL(probe_browser_media 双采样)
        l2_evals = [c for c in evaluates if "querySelectorAll" in str(c[1])]
        assert len(l2_evals) == 4
        # 1 L4 evaluate per URL(_collect_l4_text 采 innerText)
        l4_evals = [c for c in evaluates if "innerText" in str(c[1])]
        assert len(l4_evals) == 2
        # 1 L4 snapshot per URL(_collect_l4_text 采 snapshot)
        assert len(snapshots) == 2

    async def test_obscura_mcp_error_on_connection_returns_3(
        self, tmp_path, monkeypatch, capsys,
    ):
        # McpClient async with 进入时抛 McpError（传输失败）→ exit 3 + 含"obscura MCP 连接失败"
        # 注意：probe 内部 driver.goto/evaluate 抛 McpError 是 _mediabrowse_with_driver
        # 的 try/except Exception 吃掉（per-URL fail-soft），不进这个路径。
        from trajectory_pipeline.executor.browser.mcp_client import McpError

        class ClientExplode:
            def __init__(self):
                self.info = SimpleNamespace(
                    name="boom", version="0.0.1", tool_count=0,
                )

            async def __aenter__(self):
                raise McpError("transport lost")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(
            cli_mod, "McpClient",
            SimpleNamespace(from_env=staticmethod(lambda: ClientExplode())),
        )
        urls = _write_urls(
            tmp_path / "u.txt", ["https://a.example.com/1"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P024-conn", title="t",
            timeout=15.0, wait_s=0.01, driver="obscura", out=None,
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 3
        err = capsys.readouterr().err
        assert "obscura MCP" in err

    async def test_obscura_happy_path_two_urls_passes(
        self, tmp_path, monkeypatch, capsys,
    ):
        # happy path：2 URL + obscura mock driver → rc=0 + 分支汇总 passes=2
        factory, instances, counter = _make_fake_mcp_factory()
        monkeypatch.setattr(
            cli_mod, "McpClient",
            SimpleNamespace(from_env=staticmethod(factory)),
        )
        recorder: list[tuple[str, object]] = []
        monkeypatch.setattr(
            cli_mod, "ObscuraDriver",
            _make_fake_obscura_driver_cls(recorder),
        )
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://a.example.com/1", "https://a.example.com/2"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P024-happy", title="t",
            timeout=15.0, wait_s=0.01, driver="obscura", out=str(tmp_path),
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        out = capsys.readouterr().out
        assert "'passes': 2" in out


# ─────────────── TestL4ReviewWiring ───────────────

class TestL4ReviewWiring:
    """L4 VLM 复核在 _mediabrowse_with_driver 主循环里串接 ——
    增量二十五：dump JSON 含 vlm_review / 凭据闸门 note 跳过 L4 /
    FakePageDriver body 文本被 L4 消费并触发 blocked。
    """

    async def test_l4_review_attached_to_mediabrowse_result(
        self, tmp_path, monkeypatch, capsys,
    ):
        # L4 跑完后 dump JSON 含 vlm_review 字段（不是 None）——
        # 验 _mediabrowse_with_driver 主循环真的注入了 L4 result。
        driver = FakePageDriver()
        _patch_driver(monkeypatch, driver)
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://a.example.com/l4-attached"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P025-attached", title="t",
            timeout=5.0, wait_s=0.01, driver="fake", out=str(tmp_path),
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        # dump 落盘：output_dir = tmp_path / mediabrowse / P025-attached__*.json
        out_dir = tmp_path / "mediabrowse"
        assert out_dir.exists()
        written = list(out_dir.glob("P025-attached__*.json"))
        assert len(written) == 1
        import json
        body = json.loads(written[0].read_text(encoding="utf-8"))
        # vlm_review 字段必须存在（None 或 dict）；不能缺省列。
        assert "vlm_review" in body
        # 默认空 body + 空 signal → unknown / no_signal。
        assert body["vlm_review"] is not None
        assert body["vlm_review"]["label"] == "unknown"
        assert body["vlm_review"]["note"] == "no_signal"

    async def test_l4_review_skipped_on_credential_leak(
        self, tmp_path, monkeypatch, capsys,
    ):
        # URL 含 user:pass@ → 凭据闸门 fail-closed 提前返 result.note=credential_leak；
        # 主循环不应调 driver（凭据闸门守的是这点）也不应跑 L4（避免
        # 污染凭据 fail-closed 语义）。dump 里 vlm_review 应该是
        # vlm_error sentinel + note 标注 skipped。
        driver = FakePageDriver()
        _patch_driver(monkeypatch, driver)
        # 凭据 URL — _scan_credentials 会先回返 → note=credential_leak
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://user:pass@a.example.com/leak"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P025-skip", title="t",
            timeout=5.0, wait_s=0.01, driver="fake", out=str(tmp_path),
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        # driver 完全没被调（凭据闸门 fail-closed 在 probe 之前）
        assert all(c[0] != "goto" for c in driver.calls)
        assert all(c[0] != "evaluate:body" for c in driver.calls)
        out_dir = tmp_path / "mediabrowse"
        assert out_dir.exists()
        written = list(out_dir.glob("P025-skip__*.json"))
        assert len(written) == 1
        import json
        body = json.loads(written[0].read_text(encoding="utf-8"))
        assert body["note"] == "credential_leak"
        # L4 sentinel：label=unknown + note 标 skipped 原因。
        assert body["vlm_review"]["label"] == "unknown"
        assert body["vlm_review"]["confidence"] == 0.0
        # 注意：note 固定是 "vlm_error"（_vlm_error_sentinel 设计），
        # skipped 原因写在 evidence 里。
        assert body["vlm_review"]["note"] == "vlm_error"
        assert "skipped" in body["vlm_review"]["evidence"]
        assert "credential_leak" in body["vlm_review"]["evidence"]

    async def test_l4_review_picks_up_region_text_from_fake_body(
        self, tmp_path, monkeypatch, capsys,
    ):
        # FakePageDriver.set_body_text 设 region 文案 → L4 启发式
        # 应该把 body 当页面原文消费，触发 blocked（v.qq.com 真实场景）。
        driver = FakePageDriver()
        driver.set_body_text("当前设备不支持播放，请检查网络环境")
        _patch_driver(monkeypatch, driver)
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://a.example.com/region-block"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P025-region", title="t",
            timeout=5.0, wait_s=0.01, driver="fake", out=str(tmp_path),
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        out_dir = tmp_path / "mediabrowse"
        written = list(out_dir.glob("P025-region__*.json"))
        assert len(written) == 1
        import json
        body = json.loads(written[0].read_text(encoding="utf-8"))
        # L4 命中 region 文案 → blocked。
        assert body["vlm_review"]["label"] == "blocked"
        assert "当前设备不支持播放" in body["vlm_review"]["evidence"]



class TestL3Wiring:
    """L3 规则层在 _mediabrowse_with_driver 主循环里串接（增量二十六）。"""

    async def test_l3_decision_attached_after_l4(self, tmp_path, monkeypatch, capsys):
        driver = FakePageDriver()
        driver.set_body_text("该视频仅限中国大陆播放")
        _patch_driver(monkeypatch, driver)
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://a.example.com/l3-region"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P026-l3", title="t",
            timeout=5.0, wait_s=0.01, driver="fake", out=str(tmp_path),
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        out_dir = tmp_path / "mediabrowse"
        written = list(out_dir.glob("P026-l3__*.json"))
        assert len(written) == 1
        import json
        body = json.loads(written[0].read_text(encoding="utf-8"))
        # L3 串接：l3_decision 字段必须非空，且 access_kind 命中 region
        assert "l3_decision" in body
        assert body["l3_decision"] is not None
        assert body["l3_decision"]["question"] == "business.signal"
        assert body["l3_decision"]["payload"]["access_kind"] == "region"
        assert body["l3_decision"]["confidence"] == pytest.approx(0.85, abs=1e-9)

    async def test_l3_skipped_on_credential_leak(self, tmp_path, monkeypatch, capsys):
        driver = FakePageDriver()
        _patch_driver(monkeypatch, driver)
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://user:pass@a.example.com/leak-l3"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P026-skip", title="t",
            timeout=5.0, wait_s=0.01, driver="fake", out=str(tmp_path),
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        out_dir = tmp_path / "mediabrowse"
        written = list(out_dir.glob("P026-skip__*.json"))
        assert len(written) == 1
        import json
        body = json.loads(written[0].read_text(encoding="utf-8"))
        # fail-closed 跳过：credential_leak 时 l3_decision 必须是 None
        assert body["note"] == "credential_leak"
        assert body["l3_decision"] is None



class TestL5Wiring:
    """增量二十七。L5 仲裁层串接到 _mediabrowse_with_driver 循环体。
    验证两点：(1) arbitrate_l5() 总能跑通并把 SiteOutcome 挂到 result.l5_outcome；
    (2) dump_result() 把 l5_outcome.to_json() 写进 JSON。"""

    async def test_l5_outcome_attached_after_l3(self, tmp_path, monkeypatch, capsys):
        driver = FakePageDriver()
        driver.set_body_text("该视频仅限中国大陆播放")
        _patch_driver(monkeypatch, driver)
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://a.example.com/l5-region"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P027-l5", title="t",
            timeout=5.0, wait_s=0.01, driver="fake", out=str(tmp_path),
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        out_dir = tmp_path / "mediabrowse"
        written = list(out_dir.glob("P027-l5__*.json"))
        assert len(written) == 1
        import json
        body = json.loads(written[0].read_text(encoding="utf-8"))
        # L3 region → blocked；L5 加权仲裁采纳 L3
        assert body["l3_decision"] is not None
        assert body["l3_decision"]["payload"]["access_kind"] == "region"
        assert "l5_outcome" in body
        assert body["l5_outcome"] is not None
        assert body["l5_outcome"]["branch"] == "blocked"
        assert body["l5_outcome"]["source"] == "arbiter"
        assert "L5仲裁" in body["l5_outcome"]["evidence"]

    async def test_l5_unresolved_on_credential_leak(self, tmp_path, monkeypatch, capsys):
        driver = FakePageDriver()
        _patch_driver(monkeypatch, driver)
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://user:pass@a.example.com/leak-l5"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P027-skip", title="t",
            timeout=5.0, wait_s=0.01, driver="fake", out=str(tmp_path),
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        out_dir = tmp_path / "mediabrowse"
        written = list(out_dir.glob("P027-skip__*.json"))
        assert len(written) == 1
        import json
        body = json.loads(written[0].read_text(encoding="utf-8"))
        # fail-closed 短路：credential_leak 时 L5 也进 unresolved
        assert body["note"] == "credential_leak"
        assert body["l5_outcome"] is not None
        assert body["l5_outcome"]["branch"] == "unresolved"
        assert "fail-closed" in body["l5_outcome"]["evidence"]
