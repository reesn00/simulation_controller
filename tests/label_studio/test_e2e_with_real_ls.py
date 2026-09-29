"""label_studio 端到端 (需真实本地 Label Studio, 默认 skip).

运行前提:
  1. 本地 LS 已在 ``127.0.0.1:8099`` 起来 (端口不同用
     ``LABEL_STUDIO_BASE_URL`` 覆盖; **不是 8088** —— 那是 QwenPaw 后端)
  2. env ``LABEL_STUDIO_API_KEY`` 已设置
  3. 显式加 ``-m integration`` (或设 ``LS_E2E=1``)

跑法::

    $env:LS_E2E = "1"
    uv run python -m pytest tests/label_studio/test_e2e_with_real_ls.py \\
        -m integration --allow-hosts=127.0.0.1

**不建 Run 日志, 不碰 output/** —— 只在临时目录造 C3 fixture, 用完即删。
不 ``purge`` 生产项目: 每次跑建一个 ``<title>-e2e-<pid>`` 临时项目。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from conftest import write_c3

pytestmark = pytest.mark.integration

#: 端口与 ``label_studio.settings.LabelStudioSettings.base_url`` 默认值一致。
#: 8088 上是 QwenPaw Console —— 连过去报的是"认证失败", 不是"连不上",
#: 极容易被误判成 API key 配错。
LS_BASE_URL = os.environ.get("LABEL_STUDIO_BASE_URL", "http://127.0.0.1:8099")
API_KEY_ENV = "LABEL_STUDIO_API_KEY"

pytest.importorskip("httpx")


def _require_ls():
    if os.environ.get("LS_E2E") != "1":
        pytest.skip(f"设 LS_E2E=1 且 {LS_BASE_URL} 上有 LS 才跑")
    if not os.environ.get(API_KEY_ENV):
        pytest.skip(f"未设 {API_KEY_ENV}")


@pytest.fixture
def ls_settings():
    _require_ls()
    from label_studio.settings import LabelStudioSettings

    return LabelStudioSettings(
        base_url=LS_BASE_URL,
        api_key=os.environ[API_KEY_ENV],
        project_title=f"trajectory-sft-quality-e2e-{uuid.uuid4().hex[:8]}",
    )


@pytest.fixture
def client(ls_settings):
    from label_studio.client import build_client
    from label_studio.errors import LabelStudioError

    try:
        return build_client(ls_settings)
    except LabelStudioError as exc:
        pytest.skip(f"LS 不可用: {exc}")


# ---------------------------------------------------------------------------


def test_health_check(client):
    assert client.health_check()


def test_init_project_creates_then_reuses(client, ls_settings):
    from label_studio.project_manager import init_project

    first = init_project(client, ls_settings)
    try:
        assert first["created"] is True
        assert first["label_config_valid"] is True
        second = init_project(client, ls_settings)
        assert second["created"] is False
        assert second["project_id"] == first["project_id"]
    finally:
        client.delete_project(first["project_id"])


def test_label_config_validates_on_real_server(client, ls_settings):
    """LS 服务端真的认这份 XML —— 静态 XML 校验拦不住版本差异。"""
    from label_studio.client import read_label_config
    from label_studio.project_manager import init_project

    result = init_project(client, ls_settings)
    try:
        assert client.validate_label_config(
            result["project_id"], read_label_config(ls_settings.label_config_file())
        ) == []
    finally:
        client.delete_project(result["project_id"])


def test_end_to_end_push_with_scorecard(client, ls_settings, tmp_path: Path):
    """C3 → 评分卡 → LS task → 预标注, 全链路。"""
    from label_studio.project_manager import init_project
    from label_studio.task_exporter import export_batch, push_batch

    write_c3(tmp_path / "refine_data")
    result = init_project(client, ls_settings)
    project_id = result["project_id"]
    try:
        plan = export_batch(tmp_path / "refine_data", settings=ls_settings)
        assert plan.count == 1
        assert len(plan.predictions) == 1
        pushed = push_batch(
            plan, settings=ls_settings, project_id=project_id,
            client_factory=lambda: client,
        )
        assert pushed["tasks_pushed"] >= 1
        assert pushed["predictions_pushed"] >= 1

        tasks = client._request("GET", f"/api/tasks?project={project_id}&page_size=10")
        items = tasks.get("results", tasks) if isinstance(tasks, dict) else tasks
        data = next(t["data"] for t in items if t.get("inner_id", "").startswith("useramulation"))
        assert data["task_id"] == "T001"
        assert data["scorecard"]["schema_version"] == "scorecard.v1"
        assert data["criteria"][0]["criterion_id"] == "C1"
    finally:
        client.delete_project(project_id)


def test_inner_id_dedupes_on_reupload(client, ls_settings, tmp_path: Path):
    """同一 session 推两次只留一条 —— 靠 LS 原生 inner_id, 不引本地索引。"""
    from label_studio.project_manager import init_project
    from label_studio.task_exporter import export_batch, push_batch

    write_c3(tmp_path / "refine_data")
    project_id = init_project(client, ls_settings)["project_id"]
    try:
        for _ in range(2):
            plan = export_batch(tmp_path / "refine_data", settings=ls_settings)
            push_batch(plan, settings=ls_settings, project_id=project_id,
                       client_factory=lambda: client)
        tasks = client._request("GET", f"/api/tasks?project={project_id}&page_size=50")
        items = tasks.get("results", tasks) if isinstance(tasks, dict) else tasks
        matching = [t for t in items
                    if t.get("inner_id") == "useramulation-20260928-abc"]
        assert len(matching) == 1
    finally:
        client.delete_project(project_id)


def test_credential_leak_is_never_uploaded(client, ls_settings, tmp_path: Path):
    """R11 端到端: 含凭据的 C3 一条都不能出现在 LS 端。"""
    from label_studio.project_manager import init_project
    from label_studio.task_exporter import export_batch, push_batch

    write_c3(tmp_path / "refine_data", session_id="leaky",
             messages={"messages": [{"content": "sk-abcdefghij1234567890"}]})
    write_c3(tmp_path / "refine_data", session_id="clean")
    project_id = init_project(client, ls_settings)["project_id"]
    try:
        plan = export_batch(tmp_path / "refine_data", settings=ls_settings)
        assert [t["inner_id"] for t in plan.tasks] == ["useramulation-20260928-abc"]
        assert plan.rejected and plan.rejected[0][0].startswith("T001__leaky")
        push_batch(plan, settings=ls_settings, project_id=project_id,
                   client_factory=lambda: client)
        tasks = client._request("GET", f"/api/tasks?project={project_id}&page_size=50")
        items = tasks.get("results", tasks) if isinstance(tasks, dict) else tasks
        assert all("leaky" not in t.get("inner_id", "") for t in items)
    finally:
        client.delete_project(project_id)
