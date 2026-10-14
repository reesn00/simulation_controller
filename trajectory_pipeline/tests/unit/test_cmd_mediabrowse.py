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
import dataclasses
import inspect
import json
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


# ─────────────── TestMultimVLMCLIWiring ───────────────
# 增量三十四（2026-10-13）：--reviewer multim | heuristic 选项 + build_reviewer 工厂接线。
# 验证三点：(1) args.reviewer="multim" 透传到 build_reviewer 被调一次；
# (2) 默认走 heuristic（无 args.reviewer 字段时也不爆）；
# (3) argparse choices=("heuristic","multim") 拒绝非法值。
class TestMultimVLMCLIWiring:
    """--reviewer 选项 + build_reviewer 工厂接线（增量三十四，2026-10-13）。"""

    async def test_l4_reviewer_multim_invokes_build_reviewer(
        self, tmp_path, monkeypatch, capsys,
    ):
        driver = FakePageDriver()
        driver.set_body_text("当前设备不支持播放")
        _patch_driver(monkeypatch, driver)

        from trajectory_pipeline.executor.review import build_reviewer as real_factory
        calls: list[str] = []

        def spy(name: str = "heuristic"):
            calls.append(name)
            return real_factory(name)

        monkeypatch.setattr(cli_mod, "build_reviewer", spy)
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://a.example.com/reviewer-multim"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P034-multim", title="t",
            timeout=5.0, wait_s=0.01, driver="fake", out=str(tmp_path),
            reviewer="multim",
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        # build_reviewer 被调过一次且入参是 "multim"——验证 args.reviewer 透传到 L4。
        assert calls == ["multim"]

    async def test_l4_reviewer_default_is_heuristic(
        self, tmp_path, monkeypatch, capsys,
    ):
        driver = FakePageDriver()
        _patch_driver(monkeypatch, driver)
        from trajectory_pipeline.executor.review import build_reviewer as real_factory
        calls: list[str] = []

        def spy(name: str = "heuristic"):
            calls.append(name)
            return real_factory(name)

        monkeypatch.setattr(cli_mod, "build_reviewer", spy)
        urls = _write_urls(
            tmp_path / "u.txt",
            ["https://a.example.com/reviewer-default"],
        )
        ns = argparse.Namespace(
            from_urls=str(urls), task_id="P034-default", title="t",
            timeout=5.0, wait_s=0.01, driver="fake", out=str(tmp_path),
            # 注意：不设 reviewer 字段——验证 getattr(args, "reviewer", "heuristic") 默认值
        )
        rc = await cmd_mediabrowse(ns)
        assert rc == 0
        out_dir = tmp_path / "mediabrowse"
        written = list(out_dir.glob("P034-default__*.json"))
        assert len(written) == 1
        import json
        body = json.loads(written[0].read_text(encoding="utf-8"))
        # 默认行为：vlm_review 字段存在，source 应是 heuristic。
        assert body["vlm_review"] is not None
        assert body["vlm_review"]["source"] == "heuristic"
        # 没显式传 reviewer 时 _run_l4_review_async 默认走 keyword 默认 "heuristic"，
        # 但 CLI 主循环仍显式传 reviewer_name=getattr(args, "reviewer", "heuristic")，
        # 所以 build_reviewer 被调一次，name="heuristic"。
        assert calls == ["heuristic"]

    async def test_l4_reviewer_argparse_rejects_invalid_choice(self, capsys):
        # argparse choices=("heuristic", "multim") 守住——非法值不进 cmd_mediabrowse。
        from trajectory_pipeline.executor.cli import build_parser
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "mediabrowse",
                "--reviewer", "bogus_reviewer",
                "--from-urls", "u.txt",
            ])


# ══════════════════════════════════════════════════════════════════════
# 增量三十六：L6 自动派发接线（mediabrowse + run 两入口）
# ══════════════════════════════════════════════════════════════════════


class TestL6AutoDispatchMediabrowse:
    """mediabrowse 主循环末尾挂 :func:`_dispatch_l6_summary`——守住
    ``L5.branch in REVIEW_BRANCHES`` 与 ``note in AUTO_REVIEW_NOTES``
    双轨触发；末尾一行汇总。"""

    async def test_L5_unresolved_派发到_queue(self, tmp_path, monkeypatch,
                                              capsys):
        from trajectory_pipeline.executor.review_queue import (
            AUTO_QUEUE_SUBDIR, AUTO_QUEUE_FILENAME,
        )
        from trajectory_pipeline.executor import archive as archive_mod

        # 改 archive.DEFAULT_ROOT 让 auto_queue_path 解析到 tmp_path。
        monkeypatch.setattr(archive_mod, "DEFAULT_ROOT", tmp_path)

        from trajectory_pipeline.executor import cli as cli_mod2
        from dataclasses import dataclass as _dc

        @_dc
        class _SigStub:
            play_started: bool = False
            timeupdate_progress: bool = False
            current_time_sec: float | None = None
            frames_decoded: int | None = None
            black_screen: bool = False
            media_error: int | None = None

        @_dc
        class _SiteOutcomeStub:
            branch: str | None = "unresolved"
            evidence: str = "fixture"

        @_dc
        class _ResultStub:
            requested_url: str = "https://example.com/v"
            final_url: str = "https://example.com/v"
            driver_name: str = "fake"
            ok: bool = True
            media_count: int = 0
            signal: object = None
            error: str = ""
            elapsed_ms: int = 0
            note: str = ""
            body_excerpt: str = "body"
            snapshot_excerpt: str = "snap"
            vlm_review: object = None
            l3_decision: object = None
            l5_outcome: _SiteOutcomeStub | None = None
            l1_probe: object = None

        async def _fake_probe(driver, url, *, timeout_s, wait_s):
            return _ResultStub(
                signal=_SigStub(),
                requested_url=url,
                final_url=url,
                l5_outcome=_SiteOutcomeStub(branch="unresolved"),
            )

        async def _fake_collect(*a, **kw):
            return "", ""

        def _fake_arbitrate(result):
            # 模拟 L5 输出 unresolved（避免被真 arbitrate_l5 覆盖）
            return _SiteOutcomeStub(branch="unresolved",
                                    evidence="test unresolved")

        monkeypatch.setattr(cli_mod2, "arbitrate_l5", _fake_arbitrate)
        # 注入：探测 + L4 文本采集都短路；dump_browser_result 复制 result 即可。
        from trajectory_pipeline.executor.cli import (
            _mediabrowse_with_driver, _dispatch_l6_summary, FakePageDriver,
        )
        monkeypatch.setattr(cli_mod2, "probe_browser_media", _fake_probe)
        monkeypatch.setattr(cli_mod2, "_collect_l4_text", _fake_collect)

        # URL 文件：写两个；预期两条 unresolved 都被派。
        urls_file = tmp_path / "urls.txt"
        urls_file.write_text("https://example.com/v1\n"
                             "https://example.com/v2\n", encoding="utf-8")
        args = SimpleNamespace(
            out=str(tmp_path), task_id="P036", timeout=15.0, wait_s=2.0,
            no_l1=True, l1_timeout=10.0, l4_timeout=8.0, reviewer="heuristic",
            from_urls=str(urls_file),
        )

        # dump_browser_result 也短路——只写一个空 json 占位。
        def _fake_dump(result, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            return path

        monkeypatch.setattr(cli_mod2, "dump_browser_result", _fake_dump)

        # 不开 ffprobe（--no_l1 已设）。
        monkeypatch.setattr(cli_mod2, "ffprobe_available", lambda *a, **kw: False)

        rc = await _mediabrowse_with_driver(args, FakePageDriver())
        assert rc == 0
        queue = tmp_path / AUTO_QUEUE_SUBDIR / AUTO_QUEUE_FILENAME
        assert queue.exists()
        lines = [ln for ln in queue.read_text(encoding="utf-8").splitlines() if ln]
        assert len(lines) == 2
        # 每行 branch 都是 unresolved；task_id 都是 P036。
        for ln in lines:
            row = json.loads(ln)
            assert row["branch"] == "unresolved"
            assert row["task_id"] == "P036"

    async def test_L5_真负样本_不派发(self, tmp_path, monkeypatch):
        """branch=broken 是真负样本；与人工 REVIEW_BRANCHES 同源不应进 L6。"""
        from trajectory_pipeline.executor import archive as archive_mod
        monkeypatch.setattr(archive_mod, "DEFAULT_ROOT", tmp_path)
        from trajectory_pipeline.executor import cli as cli_mod2

        # 改用 _dc 装饰：
        from dataclasses import dataclass as _dc
        @_dc
        class _SigStub:
            play_started: bool = False
            timeupdate_progress: bool = False
            current_time_sec: float | None = None
            frames_decoded: int | None = None
            black_screen: bool = False
            media_error: int | None = None

        @_dc
        class _SiteOutcomeStub:
            branch: str | None = "broken"
            evidence: str = "fixture"

        @_dc
        class _ResultStub:
            requested_url: str = "https://example.com/v"
            final_url: str = "https://example.com/v"
            driver_name: str = "fake"
            ok: bool = True
            media_count: int = 0
            signal: _SigStub = None
            error: str = ""
            elapsed_ms: int = 0
            note: str = ""
            body_excerpt: str = ""
            snapshot_excerpt: str = ""
            vlm_review: object = None
            l3_decision: object = None
            l5_outcome: _SiteOutcomeStub | None = None
            l1_probe: object = None

        async def _fake_probe(driver, url, **kw):
            return _ResultStub(
                signal=_SigStub(),
                l5_outcome=_SiteOutcomeStub(branch="broken"),
            )

        def _fake_arbitrate_broken(result):
            return _SiteOutcomeStub(branch="broken", evidence="test broken")

        monkeypatch.setattr(cli_mod2, "arbitrate_l5", _fake_arbitrate_broken)

        urls_file = tmp_path / "urls.txt"
        urls_file.write_text("https://example.com/broken\n",
                             encoding="utf-8")
        args = SimpleNamespace(
            out=str(tmp_path), task_id="P036", timeout=15.0, wait_s=2.0,
            no_l1=True, l1_timeout=10.0, l4_timeout=8.0, reviewer="heuristic",
            from_urls=str(urls_file),
        )

        def _fake_dump(result, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            return path

        monkeypatch.setattr(cli_mod2, "probe_browser_media", _fake_probe)
        monkeypatch.setattr(cli_mod2, "dump_browser_result", _fake_dump)
        monkeypatch.setattr(cli_mod2, "ffprobe_available", lambda *a, **kw: False)
        from trajectory_pipeline.executor.cli import (
            _mediabrowse_with_driver, FakePageDriver,
        )
        rc = await _mediabrowse_with_driver(args, FakePageDriver())
        assert rc == 0
        # 真负样本不进 review_auto queue。
        queue_dir = tmp_path / "review_auto"
        assert not (queue_dir / "l6_dispatch.jsonl").exists()

    async def test_fail_closed_兜底派发(self, tmp_path, monkeypatch):
        """note=vlm_error 或 credential_leak 等 fail-closed → 派发。"""
        from trajectory_pipeline.executor import archive as archive_mod
        monkeypatch.setattr(archive_mod, "DEFAULT_ROOT", tmp_path)
        from trajectory_pipeline.executor import cli as cli_mod2
        from dataclasses import dataclass as _dc

        @_dc
        class _SigStub:
            play_started: bool = False
            timeupdate_progress: bool = False
            current_time_sec: float | None = None
            frames_decoded: int | None = None
            black_screen: bool = False
            media_error: int | None = None

        @_dc
        class _SiteOutcomeStub:
            branch: str | None = None
            evidence: str = ""

        @_dc
        class _ResultStub:
            requested_url: str = "https://example.com/v"
            final_url: str = "https://example.com/v"
            driver_name: str = "fake"
            ok: bool = False
            media_count: int = 0
            signal: _SigStub = None
            error: str = ""
            elapsed_ms: int = 0
            note: str = "vlm_error"
            body_excerpt: str = ""
            snapshot_excerpt: str = ""
            vlm_review: object = None
            l3_decision: object = None
            l5_outcome: _SiteOutcomeStub | None = None
            l1_probe: object = None

        async def _fake_probe(driver, url, **kw):
            return _ResultStub(
                signal=_SigStub(),
                l5_outcome=_SiteOutcomeStub(branch=None),
                note="vlm_error",
            )

        def _fake_arbitrate_none(result):
            # 模拟真实路径：fail-closed note=vlm_error 时 arbitrate_l5 也
            # 会产出 unresolved（因为 L3/L4 sentinel、L2 无信号，
            # 仲裁默认走 unresolved）。本测试验「note 与 L5 双轨触发的真实
            # 表现」——L5 路径已经派上。
            return _SiteOutcomeStub(branch="unresolved",
                                    evidence="L5 vlm_error fallback")

        monkeypatch.setattr(cli_mod2, "arbitrate_l5", _fake_arbitrate_none)

        urls_file = tmp_path / "urls.txt"
        urls_file.write_text("https://example.com/vlmerr\n",
                             encoding="utf-8")
        args = SimpleNamespace(
            out=str(tmp_path), task_id="P036", timeout=15.0, wait_s=2.0,
            no_l1=True, l1_timeout=10.0, l4_timeout=8.0, reviewer="heuristic",
            from_urls=str(urls_file),
        )
        def _fake_dump(result, path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
            return path

        monkeypatch.setattr(cli_mod2, "probe_browser_media", _fake_probe)
        monkeypatch.setattr(cli_mod2, "dump_browser_result", _fake_dump)
        monkeypatch.setattr(cli_mod2, "ffprobe_available", lambda *a, **kw: False)
        from trajectory_pipeline.executor.cli import (
            _mediabrowse_with_driver, FakePageDriver,
        )
        rc = await _mediabrowse_with_driver(args, FakePageDriver())
        assert rc == 0
        queue = tmp_path / "review_auto" / "l6_dispatch.jsonl"
        assert queue.exists()
        line = [ln for ln in queue.read_text(encoding="utf-8").splitlines()
                if ln][0]
        row = json.loads(line)
        # fail-closed 派发落到 unresolved 出口。
        assert row["branch"] == "unresolved"
        assert "vlm_error" in row["degraded"]


class TestL6DispatchSummary:
    """_dispatch_l6_summary：失败吞掉 + 末尾一行 + 零派发不打空行。"""

    def test_正常派发打印一行(self, tmp_path, capsys):
        from trajectory_pipeline.executor.cli import _dispatch_l6_summary
        # 走 dispatch_l6 真实路径——喂一个能被 duck 出 1 条的 P1 archive。
        arch = {"task_id": "P036",
                "outcomes": [{"url": "https://a.com", "branch": "unresolved",
                              "evidence": "e"}],
                "visits": [{"url": "https://a.com",
                            "landed_url": "https://a.com",
                            "player_obs": {"video_tag_count": 1,
                                           "iframe_count": 0,
                                           "degraded": [], "page_title": "t"}}]}
        # 临时把 DEFAULT_ROOT 设为 tmp_path，dispatch_l6 内部走 auto_queue_path。
        from trajectory_pipeline.executor import cli as cli_mod
        from trajectory_pipeline.executor import archive as archive_mod
        original = archive_mod.DEFAULT_ROOT
        archive_mod.DEFAULT_ROOT = tmp_path
        try:
            _dispatch_l6_summary(arch, "P036")
        finally:
            archive_mod.DEFAULT_ROOT = original
        out = capsys.readouterr().out
        assert "L6 dispatch: +1 条" in out
        assert "去重跳过 0" in out
        assert (tmp_path / "review_auto" / "l6_dispatch.jsonl").exists()

    def test_异常_不崩_仅一行提示(self, tmp_path, capsys):
        from trajectory_pipeline.executor.cli import _dispatch_l6_summary
        # 制造一条 dispatch_l6 会抛异常的输入——但 wrapper 必须吞掉。
        # 用一个把 dispatch_l6 monkeypatch 成抛异常的 fake 来验。
        from trajectory_pipeline.executor import cli as cli_mod
        original = cli_mod.dispatch_l6
        try:
            cli_mod.dispatch_l6 = lambda *a, **kw: (_ for _ in ()).throw(
                RuntimeError("forced")
            )
            _dispatch_l6_summary({}, "P036")
        finally:
            cli_mod.dispatch_l6 = original
        captured = capsys.readouterr()
        assert "L6 dispatch fail" in captured.err

    def test_零派发_不打空行(self, tmp_path, capsys):
        """成功 / 真负样本 → written=0, skipped=0 → 不该有空行。"""
        from trajectory_pipeline.executor.cli import _dispatch_l6_summary
        from trajectory_pipeline.executor import archive as archive_mod
        # 喂一个非派发表面的 Mapping（既不在 L5 也不在 fail-closed）：
        arch = {"task_id": "P036",
                "outcomes": [{"url": "https://a.com", "branch": "broken",
                              "evidence": "e"}],
                "visits": []}
        original = archive_mod.DEFAULT_ROOT
        archive_mod.DEFAULT_ROOT = tmp_path
        try:
            _dispatch_l6_summary(arch, "P036")
        finally:
            archive_mod.DEFAULT_ROOT = original
        out = capsys.readouterr().out
        assert "L6 dispatch" not in out  # 零派发，沉默
