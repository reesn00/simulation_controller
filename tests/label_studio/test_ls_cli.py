"""label_studio CLI (P1, 2026-09-28).

不打真实 LS —— 用 monkeypatch 换掉 client 层。验证: 子命令路由 / 参数透传 /
dry-run 不推送 / threshold 闸门 / 凭据缺失时 status 走无凭据分支 /
**CLI 输出不含凭据**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from conftest import write_c3

from label_studio import __main__ as cli
from label_studio.errors import CredentialLeakDetected, LabelStudioError


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {"label_studio": {
                "base_url": "http://127.0.0.1:8088",
                "api_key": "${LABEL_STUDIO_CLI_TEST_KEY}",
                "upload": {"enabled": True, "batch_size": 2},
            }},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setenv("LABEL_STUDIO_CLI_TEST_KEY", "placeholder-cli-key")


class FakeClient:
    def __init__(self, *a, **kw):
        self.imported: list = []
        self.predicted: list = []
        self.deleted: list = []
        self.calls: list[str] = []

    def health_check(self):
        self.calls.append("health")
        return {"status": "UP"}

    def list_projects(self, **_):
        return [{"id": 1, "title": "trajectory-sft-quality"}]

    def get_project(self, project_id):
        return {"id": project_id, "title": "trajectory-sft-quality"}

    def create_project(self, *, title, label_config, project_type="x"):
        return {"id": 77, "title": title}

    def delete_project(self, project_id):
        self.deleted.append(project_id)

    def validate_label_config(self, project_id, label_config):
        return []

    def import_tasks(self, project_id, tasks):
        batch = list(tasks)
        self.imported.append(batch)
        return len(batch)

    def import_predictions(self, project_id, preds):
        batch = list(preds)
        self.predicted.append(batch)
        return len(batch)


@pytest.fixture
def fake_client(monkeypatch):
    created: list[FakeClient] = []

    def factory(*_args, **_kwargs):
        client = FakeClient()
        created.append(client)
        return client

    monkeypatch.setattr("label_studio.client.build_client", factory)
    return created


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def test_parser_requires_subcommand():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_parser_accepts_all_four_subcommands():
    parser = cli.build_parser()
    for cmd in ("init-project", "status", "upload", "purge"):
        args = parser.parse_args([cmd] + (["--confirm"] if cmd == "purge" else []))
        assert args.command == cmd


def test_purge_dry_run_is_the_default():
    assert cli.build_parser().parse_args(["purge"]).confirm is False


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_without_credentials(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("LABEL_STUDIO_CLI_TEST_KEY", raising=False)
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump({"label_studio": {"api_key": "${NOT_SET_AT_ALL_XYZ}"}}),
                    encoding="utf-8")
    assert cli.main(["--config", str(path), "status"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["credentials_present"] is False
    assert "LABEL_STUDIO_API_KEY" in out["hint"]


def test_status_with_credentials(config_file, with_key, fake_client, capsys):
    assert cli.main(["--config", str(config_file), "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["health"]["status"] == "UP"
    assert out["project"]["id"] == 1
    assert "placeholder-cli-key" not in json.dumps(out)


def test_status_creates_no_run_log(config_file, with_key, fake_client, capsys):
    """status 只读 LS, 不该产生 Run 日志。"""
    cli.main(["--config", str(config_file), "status"])
    assert fake_client[0].calls == ["health"]


# ---------------------------------------------------------------------------
# init-project
# ---------------------------------------------------------------------------


def test_init_project(config_file, with_key, fake_client, capsys):
    assert cli.main(["--config", str(config_file), "init-project"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project_id"] in (1, 77)
    assert out["label_config_valid"] is True


def test_init_project_label_config_override(config_file, with_key, fake_client, capsys):
    from shared_config import REPO_ROOT

    assert cli.main([
        "--config", str(config_file), "init-project",
        "--label-config", "label_studio/label_configs/trajectory_review.xml",
    ]) == 0
    out = json.loads(capsys.readouterr().out)
    # 相对路径按仓库根解析 (与 config_loader 同口径)
    assert out["label_config_path"] == str(
        REPO_ROOT / "label_studio/label_configs/trajectory_review.xml")


# ---------------------------------------------------------------------------
# upload
# ---------------------------------------------------------------------------


def test_upload_dry_run_makes_no_http_call(
    tmp_path, config_file, with_key, fake_client, capsys
):
    write_c3(tmp_path / "refine_data")
    rc = cli.main([
        "--config", str(config_file), "upload",
        "--refine-dir", str(tmp_path / "refine_data"), "--dry-run",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["total_would_push"] == 1
    assert out["would_push"][0]["task_id"] == "T001"
    assert fake_client == []          # 一个 client 都没建


def test_upload_rejects_missing_dir(config_file, with_key, capsys):
    rc = cli.main([
        "--config", str(config_file), "upload", "--refine-dir", str(config_file.parent / "nope")
    ])
    assert rc == 2
    assert "目录不存在" in capsys.readouterr().out


def test_upload_pushes(tmp_path, config_file, with_key, fake_client, capsys):
    write_c3(tmp_path / "refine_data")
    rc = cli.main([
        "--config", str(config_file), "upload",
        "--refine-dir", str(tmp_path / "refine_data"),
    ])
    assert rc == 0
    client = fake_client[0]
    assert sum(len(b) for b in client.imported) == 1
    assert sum(len(b) for b in client.predicted) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["project_id"] == 1
    assert out["tasks_pushed"] == 1


def test_upload_batch_size_override(tmp_path, config_file, with_key, fake_client, capsys):
    for i in range(3):
        write_c3(tmp_path / "refine_data", session_id=f"s{i}")
    cli.main([
        "--config", str(config_file), "upload",
        "--refine-dir", str(tmp_path / "refine_data"), "--batch-size", "1",
    ])
    assert [len(b) for b in fake_client[0].imported] == [1, 1, 1]


def test_upload_threshold_blocks_large_batch(
    tmp_path, config_file, with_key, fake_client, capsys
):
    for i in range(3):
        write_c3(tmp_path / "refine_data", session_id=f"s{i}")
    rc = cli.main([
        "--config", str(config_file), "upload",
        "--refine-dir", str(tmp_path / "refine_data"), "--min-score", "0.0",
    ])
    assert rc == 0   # 3 < 5000 默认阈值


def test_upload_threshold_requires_force(
    tmp_path, config_file, with_key, fake_client, capsys
):
    # 阈值只能经配置改 —— dataclass 字段默认值在类创建时就固化进 __init__,
    # 事后 patch 类属性对已构造的实例无效。
    config_file.write_text(
        yaml.safe_dump(
            {"label_studio": {
                "base_url": "http://127.0.0.1:8088",
                "api_key": "${LABEL_STUDIO_CLI_TEST_KEY}",
                "upload": {"enabled": True, "dry_run_skip_threshold": 1},
            }},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    for i in range(3):
        write_c3(tmp_path / "refine_data", session_id=f"s{i}")
    rc = cli.main([
        "--config", str(config_file), "upload",
        "--refine-dir", str(tmp_path / "refine_data"),
    ])
    assert rc == 2
    assert "--force" in capsys.readouterr().out
    assert fake_client == []


def test_upload_threshold_force_overrides(tmp_path, config_file, with_key, fake_client, capsys):
    config_file.write_text(
        yaml.safe_dump(
            {"label_studio": {
                "base_url": "http://127.0.0.1:8088",
                "api_key": "${LABEL_STUDIO_CLI_TEST_KEY}",
                "upload": {"enabled": True, "dry_run_skip_threshold": 1},
            }},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    for i in range(3):
        write_c3(tmp_path / "refine_data", session_id=f"s{i}")
    rc = cli.main([
        "--config", str(config_file), "upload",
        "--refine-dir", str(tmp_path / "refine_data"), "--force",
    ])
    assert rc == 0
    assert fake_client[0].imported


def test_upload_task_id_filter(tmp_path, config_file, with_key, fake_client, capsys):
    write_c3(tmp_path / "refine_data", task_id="T001", session_id="a")
    write_c3(tmp_path / "refine_data", task_id="T002", session_id="b")
    cli.main([
        "--config", str(config_file), "upload",
        "--refine-dir", str(tmp_path / "refine_data"), "--task-id", "T002",
    ])
    sent = fake_client[0].imported[0][0]["data"]["task_id"]
    assert sent == "T002"


def test_upload_no_scorecard(tmp_path, config_file, with_key, fake_client, capsys):
    write_c3(tmp_path / "refine_data")
    cli.main([
        "--config", str(config_file), "upload",
        "--refine-dir", str(tmp_path / "refine_data"), "--no-scorecard",
    ])
    data = fake_client[0].imported[0][0]["data"]
    assert "scorecard" not in data
    assert fake_client[0].predicted == []


def test_upload_reports_credential_rejection(
    tmp_path, config_file, with_key, fake_client, capsys
):
    write_c3(tmp_path / "refine_data",
             messages={"messages": [{"content": "sk-abcdefghij1234567890"}]})
    cli.main([
        "--config", str(config_file), "upload",
        "--refine-dir", str(tmp_path / "refine_data"),
    ])
    payload = json.loads(capsys.readouterr().out)
    assert payload["rejected"][0]["stem"].startswith("T001")
    # fail-closed: 全部被拒 → 无可推 → 连 client 都不建
    assert fake_client == []
    assert payload["pushed"] is False


def test_upload_no_candidates_is_explicit(
    tmp_path, config_file, with_key, fake_client, capsys
):
    (tmp_path / "refine_data").mkdir()
    rc = cli.main([
        "--config", str(config_file), "upload",
        "--refine-dir", str(tmp_path / "refine_data"),
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pushed"] is False
    assert fake_client == []


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


def test_purge_dry_run(config_file, with_key, fake_client, capsys):
    assert cli.main(["--config", str(config_file), "purge"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["purged"] is False
    assert fake_client[0].deleted == []


def test_purge_confirmed(config_file, with_key, fake_client, capsys):
    assert cli.main(["--config", str(config_file), "purge", "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["purged"] is True
    assert fake_client[0].deleted == [1]


def test_purge_explicit_project_id(config_file, with_key, fake_client, capsys):
    cli.main(["--config", str(config_file), "purge", "--project-id", "42", "--confirm"])
    assert fake_client[0].deleted == [42]


# ---------------------------------------------------------------------------
# 错误映射
# ---------------------------------------------------------------------------


def test_credential_leak_maps_to_exit_3(tmp_path, config_file, with_key, monkeypatch, capsys):
    write_c3(tmp_path / "refine_data")

    def boom(*a, **kw):
        raise CredentialLeakDetected("命中凭据")

    monkeypatch.setattr("label_studio.task_exporter.export_batch", boom)
    rc = cli.main([
        "--config", str(config_file), "upload",
        "--refine-dir", str(tmp_path / "refine_data"),
    ])
    assert rc == 3
    assert json.loads(capsys.readouterr().out)["error"] == "credential_leak"


def test_generic_ls_error_maps_to_exit_1(config_file, with_key, monkeypatch, capsys):
    def boom(*a, **kw):
        raise LabelStudioError("service down")

    monkeypatch.setattr("label_studio.project_manager.init_project", boom)
    assert cli.main(["--config", str(config_file), "init-project"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "LabelStudioError"


def test_error_output_is_redacted(config_file, with_key, monkeypatch, capsys):
    def boom(*a, **kw):
        raise LabelStudioError("api_key=sk-abcdefghij1234567890 failed")

    monkeypatch.setattr("label_studio.project_manager.init_project", boom)
    cli.main(["--config", str(config_file), "init-project"])
    assert "sk-abcdefghij1234567890" not in capsys.readouterr().out
