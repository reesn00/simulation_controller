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
