"""label_studio.config_loader 单元测试 (P1, 2026-09-28).

用临时 yaml 驱动, 不碰真 ``config/config.yaml``（含真实凭据, 红线）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from label_studio.config_loader import (
    SECTION_KEY,
    build_settings,
    load_label_studio_config,
)
from label_studio.settings import SettingsError


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 缺省 / 无 section
# ---------------------------------------------------------------------------


def test_missing_config_returns_all_off():
    s = load_label_studio_config(Path("/nonexistent/config.yaml"))
    assert s.is_push_enabled() is False
    assert s.has_credentials() is False


@pytest.mark.parametrize("raw", [None, {}, {"other": 1}, {"label_studio": None},
                                 {"label_studio": "oops"}])
def test_absent_or_malformed_section_yields_defaults(raw):
    s = build_settings(raw)
    assert s.base_url == "http://127.0.0.1:8099"
    assert s.upload.enabled is False
    assert s.hook.enabled is False
    assert s.scorecard.enabled is True       # 评分卡默认开 —— 它不推送, 只是构造


def test_label_config_defaults_to_repo_path():
    s = build_settings(None)
    assert s.label_config_file().is_file()


# ---------------------------------------------------------------------------
# 基本字段
# ---------------------------------------------------------------------------


def test_reads_core_fields(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {
        "base_url": "http://ls:8099/",
        "api_key": "placeholder",
        "project_title": "my-tasks",
        "project_id": 12,
    }})
    s = load_label_studio_config(path)
    assert s.base_url == "http://ls:8099"      # 尾斜杠被去掉
    assert s.project_title == "my-tasks"
    assert s.project_id == 12
    assert s.resolve_api_key() == "placeholder"


def test_project_id_zero_becomes_none(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"project_id": 0}})
    assert load_label_studio_config(path).project_id is None


def test_blank_base_url_rejected(tmp_path: Path):
    """纯空白 URL 静默通过会到推送时才炸, 报错点离配置点很远。"""
    path = _write(tmp_path, {SECTION_KEY: {"base_url": "   "}})
    with pytest.raises(SettingsError, match="base_url"):
        load_label_studio_config(path)


# ---------------------------------------------------------------------------
# 凭据
# ---------------------------------------------------------------------------


def test_api_key_path_wins_over_api_key(tmp_path: Path):
    key_file = tmp_path / "key.txt"
    key_file.write_text("from-file\n", encoding="utf-8")
    path = _write(tmp_path, {SECTION_KEY: {
        "api_key": "from-yaml", "api_key_path": str(key_file),
    }})
    s = load_label_studio_config(path)
    assert s.resolve_api_key() == "from-file"


def test_missing_key_file_returns_none(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {
        "api_key_path": str(tmp_path / "nope.txt"), "api_key": "fallback",
    }})
    # 配了 path 就以 path 为准, 读不出就是没有 —— 不静默回退到 yaml
    assert load_label_studio_config(path).resolve_api_key() is None


def test_blank_api_key_is_none(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"api_key": "   "}})
    assert load_label_studio_config(path).has_credentials() is False


def test_env_placeholder_expansion(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LABEL_STUDIO_TEST_KEY", "env-value")
    path = _write(tmp_path, {SECTION_KEY: {"api_key": "${LABEL_STUDIO_TEST_KEY}"}})
    assert load_label_studio_config(path).resolve_api_key() == "env-value"


def test_env_placeholder_unset_becomes_blank(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("LABEL_STUDIO_ABSENT", raising=False)
    path = _write(tmp_path, {SECTION_KEY: {"api_key": "${LABEL_STUDIO_ABSENT}"}})
    assert load_label_studio_config(path).has_credentials() is False


# ---------------------------------------------------------------------------
# upload / scorecard / hook 子段
# ---------------------------------------------------------------------------


def test_upload_settings(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"upload": {
        "enabled": True, "batch_size": 25, "include_predictions": False,
        "filter_min_training_value_score": 0.5,
        "filter_complexity_tiers": ["easy", "hard"],
        "skip_task_ids": ["T001"], "dry_run_skip_threshold": 10,
    }}})
    u = load_label_studio_config(path).upload
    assert (u.enabled, u.batch_size, u.include_predictions) == (True, 25, False)
    assert u.filter_min_training_value_score == 0.5
    assert u.filter_complexity_tiers == ("easy", "hard")
    assert u.skip_task_ids == ("T001",)
    assert u.dry_run_skip_threshold == 10


def test_tier_filter_is_frozenset():
    s = build_settings({SECTION_KEY: {"upload": {
        "filter_complexity_tiers": ["easy", "medium"]}}})
    assert s.tier_filter() == frozenset({"easy", "medium"})


@pytest.mark.parametrize("bad", [0, -1])
def test_non_positive_batch_size_rejected(tmp_path: Path, bad):
    path = _write(tmp_path, {SECTION_KEY: {"upload": {"batch_size": bad}}})
    with pytest.raises(SettingsError, match="batch_size"):
        load_label_studio_config(path)


@pytest.mark.parametrize("bad", ["x", None])
def test_uncastable_batch_size_falls_back_to_default(tmp_path: Path, bad):
    path = _write(tmp_path, {SECTION_KEY: {"upload": {"batch_size": bad}}})
    assert load_label_studio_config(path).upload.batch_size == 50


def test_unknown_tier_rejected(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"upload": {
        "filter_complexity_tiers": ["insane"]}}})
    with pytest.raises(SettingsError, match="insane"):
        load_label_studio_config(path)


def test_min_score_out_of_range_rejected(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"upload": {
        "filter_min_training_value_score": 1.5}}})
    with pytest.raises(SettingsError, match=r"\[0,1\]"):
        load_label_studio_config(path)


def test_scorecard_settings(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"scorecard": {
        "enabled": False, "require_estimated_flag": False,
        "drop_missing_dimensions": False,
    }}})
    sc = load_label_studio_config(path).scorecard
    assert (sc.enabled, sc.require_estimated_flag, sc.drop_missing_dimensions) == (
        False, False, False)


def test_hook_settings(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"hook": {
        "enabled": True, "hook_timeout_seconds": 12.5,
    }}})
    h = load_label_studio_config(path).hook
    assert (h.enabled, h.hook_timeout_seconds) == (True, 12.5)


def test_hook_ignores_legacy_on_failure_key(tmp_path: Path):
    """``on_failure`` 已删除 —— 老配置里留着不能炸, 也必须不生效。

    它是被解析、被校验、被写进示例配置, 但 ls_hook 从未读取过; 推送是旁路,
    失败既不重试也不抛, 本来就没有可配置的分叉。留着一个不生效的开关比
    没有更糟 —— 配了 log_and_metric 的人会以为指标是特意打开的。
    """
    path = _write(tmp_path, {SECTION_KEY: {"hook": {
        "enabled": True, "on_failure": "log_and_metric",
    }}})
    h = load_label_studio_config(path).hook
    assert h.enabled is True
    assert not hasattr(h, "on_failure")


def test_hook_zero_timeout_rejected(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"hook": {"hook_timeout_seconds": 0}}})
    with pytest.raises(SettingsError, match="hook_timeout_seconds"):
        load_label_studio_config(path)


def test_is_push_enabled_is_or_of_both_switches():
    from dataclasses import replace

    from label_studio.settings import HookSettings, UploadSettings

    base = build_settings(None)
    assert base.is_push_enabled() is False
    assert replace(base, upload=UploadSettings(enabled=True)).is_push_enabled() is True
    assert replace(base, hook=HookSettings(enabled=True)).is_push_enabled() is True


# ---------------------------------------------------------------------------
# credential_scan
# ---------------------------------------------------------------------------


def test_credential_scan_defaults(tmp_path: Path):
    cs = load_label_studio_config(build_settings(None) and
                                  _write(tmp_path, {SECTION_KEY: {}})).credential_scan
    assert cs.enabled is True
    assert cs.on_hit == "reject_task"
    assert any("sk-" in p for p in cs.patterns)


def test_credential_scan_custom_patterns(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"credential_scan": {
        "patterns": ["MYKEY-[0-9]+"], "on_hit": "skip_task",
    }}})
    cs = load_label_studio_config(path).credential_scan
    assert cs.patterns == ("MYKEY-[0-9]+",)
    assert cs.on_hit == "skip_task"


def test_credential_scan_invalid_regex_rejected(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"credential_scan": {
        "patterns": ["[unclosed"]}}})
    with pytest.raises(SettingsError, match="非法正则"):
        load_label_studio_config(path)


def test_credential_scan_bad_on_hit_rejected(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"credential_scan": {"on_hit": "ignore"}}})
    with pytest.raises(SettingsError, match="on_hit"):
        load_label_studio_config(path)


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


def test_health_check_settings(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"health_check": {
        "timeout_seconds": 2, "retry_attempts": 1, "retry_backoff_seconds": 0.5}}})
    h = load_label_studio_config(path).health_check
    assert (h.timeout_seconds, h.retry_attempts, h.retry_backoff_seconds) == (2.0, 1, 0.5)


def test_health_check_zero_attempts_rejected(tmp_path: Path):
    path = _write(tmp_path, {SECTION_KEY: {"health_check": {"retry_attempts": 0}}})
    with pytest.raises(SettingsError, match="retry_attempts"):
        load_label_studio_config(path)


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def test_repo_relative_label_config_path(tmp_path: Path):
    from shared_config import REPO_ROOT

    path = _write(tmp_path, {SECTION_KEY: {
        "label_config_path": "label_studio/label_configs/trajectory_review.xml"}})
    resolved = load_label_studio_config(path).label_config_file()
    assert resolved == REPO_ROOT / "label_studio/label_configs/trajectory_review.xml"
    assert resolved.is_file()


def test_absolute_label_config_path_wins(tmp_path: Path):
    custom = tmp_path / "mine.xml"
    custom.write_text("<View/>", encoding="utf-8")
    path = _write(tmp_path, {SECTION_KEY: {"label_config_path": str(custom)}})
    assert load_label_studio_config(path).label_config_file() == custom


# ---------------------------------------------------------------------------
# 与仓库示例配置的一致性
# ---------------------------------------------------------------------------


def test_example_config_parses_and_is_off_by_default():
    """``config/config.example.yaml`` 必须能被自己解析, 且默认不推送。"""
    from shared_config import REPO_ROOT

    example = REPO_ROOT / "config" / "config.example.yaml"
    if not example.is_file():
        pytest.skip("config.example.yaml 不存在")
    s = build_settings(yaml.safe_load(example.read_text(encoding="utf-8")))
    assert s.upload.enabled is False
    assert s.hook.enabled is False
    assert s.credential_scan.enabled is True


def test_example_config_has_no_literal_credential():
    """红线: 提交版配置不得含真实 key。"""
    from shared_config import REPO_ROOT

    example = REPO_ROOT / "config" / "config.example.yaml"
    if not example.is_file():
        pytest.skip("config.example.yaml 不存在")
    text = example.read_text(encoding="utf-8")
    section = (yaml.safe_load(text) or {}).get(SECTION_KEY) or {}
    api_key = str(section.get("api_key") or "")
    assert not api_key or api_key.startswith("${")
