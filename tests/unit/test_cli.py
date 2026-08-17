from simulate_serve.__main__ import build_parser, main
from simulate_serve.config import load_config


def test_help_parser_has_operational_commands() -> None:
    parser = build_parser()
    options = {action.dest for action in parser._actions}
    assert {"validate_config", "check_tools", "readiness", "output_format", "rerun_task", "list_interrupted"}.issubset(options)


def test_validate_builtin_config_succeeds() -> None:
    assert main(["--validate-config"]) == 0


def test_missing_explicit_config_returns_cli_error(tmp_path) -> None:
    assert main(["--config", str(tmp_path / "missing.yaml"), "--validate-config"]) == 2


def test_builtin_config_does_not_package_model_credentials_or_internal_endpoint() -> None:
    config = load_config()

    assert config.model.api_key == ""
    assert config.model.base_url == ""


def test_readiness_is_read_only_and_reports_blocked_tasks(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)

    assert main(["--readiness"]) == 0

    output = capsys.readouterr().out
    assert "Validation readiness" in output
    assert "semantic_judge" in output
    assert not (tmp_path / "output" / "run.log").exists()
