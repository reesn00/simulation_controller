"""orchestration.ls_hook 单元测试 (P2, 2026-09-28).

hook 的价值全在**失败隔离**上, 所以测试重心是:
- hook 关闭时零副作用
- LS 挂死 → 超时放弃, 不拖住主流程
- LS 抛任何异常 → task 终态不受影响
- 开关只看 hook.enabled, 不看 upload.enabled
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from orchestration.ls_hook import (
    METRICS,
    load_hook_settings,
    push_with_timeout,
    reset_metrics,
    run_hook,
)
from label_studio.settings import (
    HookSettings,
    LabelStudioSettings,
    UploadSettings,
)


@pytest.fixture(autouse=True)
def _clean_metrics():
    reset_metrics()
    yield
    reset_metrics()


@pytest.fixture
def meta_file(tmp_path: Path) -> Path:
    path = tmp_path / "T001__s1_refined.meta.json"
    path.write_text("{}", encoding="utf-8")
    return path


def _settings(*, hook=True, upload=False) -> LabelStudioSettings:
    return LabelStudioSettings(
        base_url="http://127.0.0.1:8099",
        api_key="placeholder",
        hook=HookSettings(enabled=hook, hook_timeout_seconds=1.0),
        upload=UploadSettings(enabled=upload),
    )


# ---------------------------------------------------------------------------
# 开关语义
# ---------------------------------------------------------------------------


def test_disabled_hook_is_noop(meta_file: Path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("不该被调用")

    monkeypatch.setattr("orchestration.ls_hook._push", boom)
    out = run_hook(_settings(hook=False), project_id=1, meta_path=meta_file)
    assert out["skipped"] is True and out["ok"] is False
    assert out["reason"] == "hook_disabled"
    assert METRICS["ls_hook_attempted"] == 0


def test_upload_enabled_alone_does_not_enable_hook(meta_file: Path, monkeypatch):
    """两个开关独立 —— 配了 upload.enabled 不代表 hook 开了。"""
    def boom(*a, **k):
        raise AssertionError("不该被调用")

    monkeypatch.setattr("orchestration.ls_hook._push", boom)
    out = run_hook(_settings(hook=False, upload=True), project_id=1, meta_path=meta_file)
    assert out["reason"] == "hook_disabled"


def test_none_settings_is_noop(meta_file: Path):
    out = run_hook(None, project_id=1, meta_path=meta_file)
    assert out["skipped"] is True


def test_missing_meta_is_skipped():
    out = run_hook(_settings(), project_id=1, meta_path=Path("/nope/x.meta.json"))
    assert out["skipped"] is True
    assert METRICS["ls_hook_skipped"] == 1
    assert METRICS["ls_hook_attempted"] == 0


# ---------------------------------------------------------------------------
# 超时隔离
# ---------------------------------------------------------------------------


def test_timeout_does_not_block(meta_file: Path, monkeypatch):
    """挂死的 LS 不能拖住 orchestration。"""
    release = threading.Event()

    def slow(*a, **k):
        release.wait(timeout=5)
        return {"task_id": "T001", "session_id": "s1"}

    monkeypatch.setattr("orchestration.ls_hook._push", slow)
    started = time.perf_counter()
    out = push_with_timeout(_settings(), project_id=1, meta_path=meta_file,
                            timeout_seconds=0.3)
    elapsed = time.perf_counter() - started
    assert out["ok"] is False and out["reason"] == "timeout"
    assert METRICS["ls_hook_timed_out"] == 1
    assert elapsed < 2.0, "超时后不该继续等"
    release.set()


def test_slow_but_within_timeout_succeeds(meta_file: Path, monkeypatch):
    monkeypatch.setattr(
        "orchestration.ls_hook._push",
        lambda *a, **k: {"task_id": "T001", "session_id": "s1"},
    )
    out = push_with_timeout(_settings(), project_id=1, meta_path=meta_file,
                            timeout_seconds=5.0)
    assert out["ok"] is True
    assert out["result"]["task_id"] == "T001"
    assert METRICS["ls_hook_succeeded"] == 1


def test_default_timeout_comes_from_settings(meta_file: Path, monkeypatch):
    """不显式传 timeout 时从 settings.hook 取 —— 不静默落到内置默认值。"""
    from orchestration.ls_hook import DEFAULT_HOOK_TIMEOUT_SECONDS, _default_timeout

    settings = _settings()
    assert _default_timeout(settings) == 1.0
    # 兼容直接传 HookSettings 的调用方
    assert _default_timeout(HookSettings(enabled=True, hook_timeout_seconds=3.0)) == 3.0
    # 兜底值必须够走完 PAT refresh + PATCH label_config + 建 task + 预标注
    assert _default_timeout(object()) == DEFAULT_HOOK_TIMEOUT_SECONDS
    assert DEFAULT_HOOK_TIMEOUT_SECONDS >= 30.0


# ---------------------------------------------------------------------------
# 异常隔离
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("LS down"),
        ConnectionError("refused"),
        TimeoutError("slow"),
        ValueError("bad payload"),
    ],
)
def test_any_exception_is_swallowed(meta_file: Path, monkeypatch, exc):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr("orchestration.ls_hook._push", boom)
    out = push_with_timeout(_settings(), project_id=1, meta_path=meta_file,
                            timeout_seconds=5.0)
    assert out["ok"] is False
    assert out["reason"] == type(exc).__name__
    assert METRICS["ls_hook_failed"] == 1


def test_credential_leak_is_recorded_not_raised(meta_file: Path, monkeypatch):
    """R11 命中在 hook 里也只记录 —— 推送失败不是 task 失败。"""
    from label_studio.errors import CredentialLeakDetected

    def boom(*a, **k):
        raise CredentialLeakDetected("命中 sk-abcdefghij1234567890")

    monkeypatch.setattr("orchestration.ls_hook._push", boom)
    out = push_with_timeout(_settings(), project_id=1, meta_path=meta_file,
                            timeout_seconds=5.0)
    assert out["ok"] is False
    assert "***" in out["error"]


# ---------------------------------------------------------------------------
# project 解析
# ---------------------------------------------------------------------------


def test_project_resolver_called_when_id_missing(meta_file: Path, monkeypatch):
    monkeypatch.setattr(
        "orchestration.ls_hook._push", lambda *a, **k: {"task_id": "T001"}
    )
    seen = {}

    def resolve(settings):
        seen["called"] = True
        return 7

    out = run_hook(_settings(), project_id=None, meta_path=meta_file,
                   resolve_project_id=resolve)
    assert out["ok"] is True
    assert seen["called"] is True


def test_no_project_id_and_no_resolver_is_skip(meta_file: Path):
    out = run_hook(_settings(), project_id=None, meta_path=meta_file)
    assert out["skipped"] is True
    assert out["reason"] == "no_project_id"


def test_project_resolver_failure_is_swallowed(meta_file: Path):
    def resolve(_s):
        raise RuntimeError("LS unreachable")

    out = run_hook(_settings(), project_id=None, meta_path=meta_file,
                   resolve_project_id=resolve)
    assert out["ok"] is False
    assert out["reason"] == "project_resolve_failed"
    assert METRICS["ls_hook_failed"] == 1


# ---------------------------------------------------------------------------
# project_id 进程级缓存
# ---------------------------------------------------------------------------


def test_project_resolved_once_per_process(meta_file: Path, monkeypatch):
    """同批次只解析一次 project, 但**每条照样推**。

    ``resolve_project_id(sync=True)`` 会把本地 XML PATCH 进 LS —— 那是覆盖,
    不是同步。98 个 task 各 PATCH 一次, 批次跑到一半就把标注员在 LS 上做的
    调整冲掉了。同批次内 label_config 文件不会变, 第一次解析后必须复用。
    """
    calls: list[LabelStudioSettings] = []
    monkeypatch.setattr("orchestration.ls_hook._push",
                        lambda *a, **k: {"task_id": "T001", "session_id": "s1"})

    def resolve(s):
        calls.append(s)
        return 7

    settings = _settings()
    for _ in range(3):
        out = run_hook(settings, project_id=None, meta_path=meta_file,
                       resolve_project_id=resolve)
        assert out["ok"] is True

    assert len(calls) == 1, "project 被重复解析 = label_config 被重复 PATCH"
    assert METRICS["ls_hook_succeeded"] == 3, "缓存不能把推送本身也跳过"


def test_project_cache_is_keyed_by_url_and_title(meta_file: Path, monkeypatch):
    """换项目 / 换实例必须重新解析, 不能命中上一个的缓存。"""
    calls: list[str] = []
    monkeypatch.setattr("orchestration.ls_hook._push",
                        lambda *a, **k: {"task_id": "T001", "session_id": "s1"})

    def resolve(s):
        calls.append(s.project_title)
        return 7

    run_hook(_settings(), project_id=None, meta_path=meta_file,
             resolve_project_id=resolve)
    other = LabelStudioSettings(
        base_url="http://127.0.0.1:8099",
        api_key="x",
        project_title="another-project",
        hook=HookSettings(enabled=True, hook_timeout_seconds=1.0),
    )
    run_hook(other, project_id=None, meta_path=meta_file,
             resolve_project_id=resolve)

    assert calls == ["trajectory-sft-quality", "another-project"]


def test_failed_resolve_is_not_cached(meta_file: Path, monkeypatch):
    """解析失败不能进缓存 —— 否则一次偶发网络抖动就永久跳过项目定位。"""
    monkeypatch.setattr("orchestration.ls_hook._push",
                        lambda *a, **k: {"task_id": "T001", "session_id": "s1"})
    attempts = {"n": 0}

    def resolve(_s):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient")
        return 7

    first = run_hook(_settings(), project_id=None, meta_path=meta_file,
                     resolve_project_id=resolve)
    assert first["reason"] == "project_resolve_failed"
    second = run_hook(_settings(), project_id=None, meta_path=meta_file,
                      resolve_project_id=resolve)
    assert second["ok"] is True
    assert attempts["n"] == 2


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------


def test_load_hook_settings_returns_none_when_disabled(tmp_path: Path):
    import yaml

    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"label_studio": {"hook": {"enabled": False}}}),
                    encoding="utf-8")
    assert load_hook_settings(path) is None


def test_load_hook_settings_returns_hook_when_enabled(tmp_path: Path):
    import yaml

    path = tmp_path / "c.yaml"
    path.write_text(
        yaml.safe_dump({"label_studio": {"hook": {
            "enabled": True, "hook_timeout_seconds": 9.0}}}),
        encoding="utf-8",
    )
    hook = load_hook_settings(path)
    assert hook is not None
    assert hook.enabled is True
    assert hook.hook_timeout_seconds == 9.0


def test_load_hook_settings_never_raises_on_broken_yaml(tmp_path: Path):
    path = tmp_path / "c.yaml"
    path.write_text("{not: [valid", encoding="utf-8")
    assert load_hook_settings(path) is None


def test_load_hook_settings_none_on_missing_file(tmp_path: Path):
    assert load_hook_settings(tmp_path / "nope.yaml") is None


# ---------------------------------------------------------------------------
# 与 task_pipeline 的接线
# ---------------------------------------------------------------------------


def test_task_pipeline_helper_is_silent_when_hook_off(tmp_path, meta_file, monkeypatch):
    """step 11 在 hook 关闭时必须**零副作用**（不建 client, 不 import LS 传输层）。"""
    from orchestration.settings import Paths
    from orchestration.task_pipeline import _push_to_label_studio

    monkeypatch.setattr("orchestration.ls_hook.load_hook_settings", lambda *a, **k: None)

    def no_client(*a, **k):
        raise AssertionError("hook 关闭时不该建 client")

    monkeypatch.setattr("label_studio.client.build_client", no_client)
    paths = Paths(**{f.name: tmp_path for f in _path_fields()})
    assert _push_to_label_studio(paths, meta_file) is None


def test_task_pipeline_helper_never_raises(tmp_path, meta_file, monkeypatch):
    from orchestration.settings import Paths
    from orchestration.task_pipeline import _push_to_label_studio

    def boom(*a, **k):
        raise RuntimeError("loader 崩了")

    monkeypatch.setattr("orchestration.ls_hook.load_hook_settings", boom)
    paths = Paths(**{f.name: tmp_path for f in _path_fields()})
    assert _push_to_label_studio(paths, meta_file) is None


def test_task_pipeline_helper_passes_meta_path(tmp_path, meta_file, monkeypatch):
    from orchestration.settings import Paths
    from orchestration.task_pipeline import _push_to_label_studio
    from label_studio.settings import HookSettings, LabelStudioSettings

    seen = {}

    monkeypatch.setattr(
        "orchestration.ls_hook.load_hook_settings",
        lambda *a, **k: HookSettings(enabled=True),
    )
    monkeypatch.setattr(
        "label_studio.config_loader.load_label_studio_config",
        lambda *a, **k: LabelStudioSettings(api_key="k"),
    )
    monkeypatch.setattr(
        "orchestration.ls_hook.run_hook",
        lambda settings, **kw: seen.update(kw) or {"ok": True, "skipped": False,
                                                  "reason": None, "error": None},
    )
    paths = Paths(**{f.name: tmp_path for f in _path_fields()})
    _push_to_label_studio(paths, meta_file)
    assert seen["meta_path"] == meta_file


def _path_fields():
    from dataclasses import fields

    from orchestration.settings import Paths

    return [f for f in fields(Paths)]
