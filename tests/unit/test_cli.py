from urllib.parse import urlparse

from simulate_serve.__main__ import build_parser, main
from simulate_serve.config import PACKAGE_DIR, load_config

# 提交版配置模板 (模块级配置文件已删除, 唯一配置入口为仓库根 config/config.yaml,
# 含真实凭据不入库; 提交版模板为 config/config.example.yaml)
EXAMPLE_CONFIG = PACKAGE_DIR.parent / "config" / "config.example.yaml"


def test_help_parser_has_operational_commands() -> None:
    parser = build_parser()
    options = {action.dest for action in parser._actions}
    assert {"validate_config", "check_tools", "readiness", "output_format", "rerun_task", "list_interrupted"}.issubset(options)


def test_validate_example_config_succeeds() -> None:
    assert main(["--config", str(EXAMPLE_CONFIG), "--validate-config"]) == 0


def test_missing_explicit_config_returns_cli_error(tmp_path) -> None:
    assert main(["--config", str(tmp_path / "missing.yaml"), "--validate-config"]) == 2


def test_missing_root_config_returns_cli_error(tmp_path, monkeypatch) -> None:
    # 根配置缺失 (SIMCTL_CONFIG 指向不存在文件) → CLI error, 无兜底
    monkeypatch.setenv("SIMCTL_CONFIG", str(tmp_path / "nope.yaml"))
    assert main(["--validate-config"]) == 2


def _placeholder_key(value: str) -> bool:
    # Empty is fine; a committed local placeholder (e.g. "local-your-api-key")
    # is not a usable credential. Anything else must not ship.
    return not value or value.lower().startswith("local") or "your_api_key" in value.lower()


def _local_or_empty_url(value: str) -> bool:
    if not value:
        return True
    host = urlparse(value).hostname or ""
    return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def test_committed_example_config_carries_no_credentials_or_internal_endpoint() -> None:
    # 提交版模板 config/config.example.yaml 不得含真实凭据或内部 endpoint
    assert EXAMPLE_CONFIG.exists()
    config = load_config(str(EXAMPLE_CONFIG))

    assert _placeholder_key(config.model.api_key)
    assert _local_or_empty_url(config.model.base_url)
    assert config.agent_endpoint.auth_token == ""


def test_module_level_config_files_are_deleted() -> None:
    # 配置统一收敛到根 config/config.yaml, 模块级配置文件必须不存在
    # (防止真实凭据随模块文件/打包含入仓库)
    for rel in (
        "simulate_serve/config/config.yaml",
        "orchestration/config.yaml",
        "gdr/config/gdr_config.yaml",
        "etl/qwenformat/config.yaml",
    ):
        assert not (PACKAGE_DIR.parent / rel).exists(), rel


def test_readiness_is_read_only_and_reports_blocked_tasks(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)
    # Explicit credential-free config: the builtin config.yaml carries local
    # runtime values, and this test must not depend on them.
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "model:\n"
        "  api_key: \"\"\n"
        "  base_url: \"\"\n"
        f"tasks_file: \"{(PACKAGE_DIR / 'config' / 'tasks.yaml').as_posix()}\"\n"
        f"scenarios_file: \"{(PACKAGE_DIR / 'config' / 'scenarios.yaml').as_posix()}\"\n",
        encoding="utf-8",
    )

    assert main(["--readiness", "--config", str(config_file)]) == 0

    output = capsys.readouterr().out
    assert "Validation readiness" in output
    assert "semantic_judge" in output
    assert not (tmp_path / "output" / "run.log").exists()
