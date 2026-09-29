"""label_studio.project_manager 单元测试 (P1, 2026-09-28).

用 FakeClient 替掉 REST 层, 覆盖: 幂等复用 / 标题精确匹配 / label_config
校验失败即抛 / purge 的 confirm 闸门 / status 不泄露凭据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from label_studio.errors import LabelStudioError, LabelStudioProjectError
from label_studio.project_manager import (
    describe_status,
    find_project_by_title,
    get_or_create_project,
    init_project,
    list_existing_projects,
    purge_tasks,
    resolve_project_id,
    validate_label_config,
)
from label_studio.settings import LabelStudioSettings

SECRET = "placeholder-not-a-real-credential"


class FakeClient:
    def __init__(self, projects=None, config_errors=None):
        self.projects = list(projects or [])
        self.config_errors = list(config_errors or [])
        self.created: list[dict] = []
        self.deleted: list[int] = []
        self.validated: list[tuple[int, str]] = []

    def list_projects(self, **_):
        return list(self.projects)

    def get_project(self, project_id):
        for p in self.projects:
            if int(p["id"]) == int(project_id):
                return p
        raise LabelStudioError(f"project {project_id} 不存在")

    def create_project(self, *, title, label_config, project_type="DocumentClassification"):
        project = {"id": 100 + len(self.projects), "title": title}
        self.projects.append(project)
        self.created.append({"title": title, "label_config": label_config})
        return project

    def delete_project(self, project_id):
        self.deleted.append(int(project_id))
        self.projects = [p for p in self.projects if int(p["id"]) != int(project_id)]

    def validate_label_config(self, project_id, label_config):
        self.validated.append((int(project_id), label_config))
        return list(self.config_errors)


@pytest.fixture
def xml(tmp_path: Path) -> Path:
    path = tmp_path / "label_config.xml"
    path.write_text("<View><TextArea name='task' value='$messages'/></View>", encoding="utf-8")
    return path


@pytest.fixture
def ls_settings(xml: Path) -> LabelStudioSettings:
    return LabelStudioSettings(
        base_url="http://ls.invalid",
        api_key=SECRET,
        label_config_path=xml,
        project_title="trajectory-sft-quality",
    )


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def test_list_existing_projects():
    assert list_existing_projects(FakeClient([{"id": 1}])) == [{"id": 1}]


def test_find_by_title_exact_match():
    client = FakeClient([{"id": 1, "title": "trajectory-sft-quality"}])
    assert find_project_by_title(client, "trajectory-sft-quality")["id"] == 1


def test_find_by_title_no_fuzzy_match():
    """标题相近的两个项目里推错一个, 比推失败更糟。"""
    client = FakeClient([{"id": 1, "title": "trajectory-sft-quality-v2"}])
    assert find_project_by_title(client, "trajectory-sft-quality") is None


def test_find_by_title_ignores_whitespace_only_difference():
    client = FakeClient([{"id": 1, "title": "  trajectory-sft-quality "}])
    assert find_project_by_title(client, "trajectory-sft-quality")["id"] == 1


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def test_validate_returns_empty_on_pass():
    assert validate_label_config(FakeClient(), 1, "<View/>") == []


def test_validate_raises_with_ls_detail():
    """校验失败必须带 LS 的逐条消息, 否则 CLI 只给退出码 1, 人没法修。"""
    client = FakeClient(config_errors=[{"detail": "toName references missing tag"}])
    with pytest.raises(LabelStudioProjectError, match="toName references missing tag"):
        validate_label_config(client, 1, "<View/>")


def test_validate_reports_error_count():
    client = FakeClient(config_errors=[{"detail": "a"}, {"detail": "b"}, {"detail": "c"}])
    with pytest.raises(LabelStudioProjectError, match="3 处"):
        validate_label_config(client, 1, "<View/>")


# ---------------------------------------------------------------------------
# get_or_create
# ---------------------------------------------------------------------------


def test_creates_when_absent(ls_settings):
    client = FakeClient()
    project, created = get_or_create_project(client, ls_settings)
    assert created is True
    assert project["title"] == ls_settings.project_title
    assert "<View>" in client.created[0]["label_config"]


def test_reuses_by_title(ls_settings):
    client = FakeClient([{"id": 9, "title": "trajectory-sft-quality"}])
    project, created = get_or_create_project(client, ls_settings)
    assert (project["id"], created) == (9, False)
    assert client.created == []


def test_explicit_project_id_wins(ls_settings):
    from dataclasses import replace

    client = FakeClient([{"id": 9, "title": "trajectory-sft-quality"},
                         {"id": 42, "title": "other"}])
    project, created = get_or_create_project(client, replace(ls_settings, project_id=42))
    assert (project["id"], created) == (42, False)


# ---------------------------------------------------------------------------
# init_project
# ---------------------------------------------------------------------------


def test_init_project_reports_creation(ls_settings):
    result = init_project(FakeClient(), ls_settings)
    assert result["created"] is True
    assert result["label_config_valid"] is True
    assert result["label_config_path"] == str(ls_settings.label_config_path)


def test_init_project_is_idempotent(ls_settings):
    client = FakeClient()
    first = init_project(client, ls_settings)
    second = init_project(client, ls_settings)
    assert first["project_id"] == second["project_id"]
    assert second["created"] is False
    assert len(client.created) == 1


def test_init_project_validates_after_create(ls_settings):
    client = FakeClient()
    result = init_project(client, ls_settings)
    assert client.validated[0][0] == result["project_id"]


def test_init_project_fails_on_bad_config(tmp_path: Path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<View/>", encoding="utf-8")
    settings = LabelStudioSettings(
        base_url="http://ls.invalid", api_key="k", label_config_path=bad
    )
    client = FakeClient(config_errors=[{"detail": "missing toName anchor"}])
    with pytest.raises(LabelStudioProjectError):
        init_project(client, settings)


def test_init_project_missing_xml_raises(tmp_path: Path):
    settings = LabelStudioSettings(
        base_url="http://ls.invalid", api_key="k", label_config_path=tmp_path / "nope.xml"
    )
    with pytest.raises(LabelStudioError, match="无法读取"):
        init_project(FakeClient(), settings)


# ---------------------------------------------------------------------------
# resolve_project_id
# ---------------------------------------------------------------------------


def test_resolve_uses_explicit_id(ls_settings):
    from dataclasses import replace

    client = FakeClient([{"id": 42, "title": "x"}])
    assert resolve_project_id(client, replace(ls_settings, project_id=42)) == 42


def test_resolve_finds_by_title(ls_settings):
    client = FakeClient([{"id": 5, "title": "trajectory-sft-quality"}])
    assert resolve_project_id(client, ls_settings) == 5


def test_resolve_does_not_create(ls_settings):
    """推送路径不新建项目 —— 免得 `upload` 顺手建出一个空项目。"""
    client = FakeClient()
    with pytest.raises(LabelStudioProjectError, match="init-project"):
        resolve_project_id(client, ls_settings)
    assert client.created == []


# ---------------------------------------------------------------------------
# purge
# ---------------------------------------------------------------------------


def test_purge_dry_run_without_confirm():
    client = FakeClient([{"id": 3, "title": "t"}])
    result = purge_tasks(client, 3, confirm=False)
    assert result["purged"] is False
    assert client.deleted == []


def test_purge_deletes_with_confirm():
    client = FakeClient([{"id": 3, "title": "t"}])
    result = purge_tasks(client, 3, confirm=True)
    assert result["purged"] is True
    assert client.deleted == [3]
    assert "标注一并丢失" in result["note"]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_project(ls_settings):
    client = FakeClient([{"id": 8, "title": "trajectory-sft-quality"}])
    out = describe_status(client, ls_settings, health={"status": "UP"})
    assert out["project"]["id"] == 8
    assert out["credentials_present"] is True
    assert out["api_key_source"] == "api_key"


def test_status_never_prints_key(ls_settings):
    out = describe_status(FakeClient(), ls_settings, health={"status": "UP"})
    assert SECRET not in str(out)


def test_status_reports_api_key_path_source(tmp_path: Path, xml: Path):
    key_file = tmp_path / "key.txt"
    key_file.write_text(SECRET, encoding="utf-8")
    settings = LabelStudioSettings(api_key_path=key_file, label_config_path=xml)
    out = describe_status(FakeClient(), settings, health={})
    assert out["api_key_source"] == "api_key_path"
    assert SECRET not in str(out)


def test_status_survives_project_lookup_failure(ls_settings):
    from dataclasses import replace

    out = describe_status(
        FakeClient(), replace(ls_settings, project_id=999), health={}
    )
    assert out["project"] is None
    assert "project_error" in out


def test_status_flags_missing_label_config(tmp_path: Path):
    settings = LabelStudioSettings(
        api_key="k", label_config_path=tmp_path / "nope.xml"
    )
    out = describe_status(FakeClient(), settings, health={})
    assert out["label_config_exists"] is False
    assert out["upload_enabled"] is False
    assert out["hook_enabled"] is False
