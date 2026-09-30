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

import json
import os
import uuid
from pathlib import Path

import pytest
from conftest import write_c3

#: R11 凭据扫描用的诱饵串。推送侧一旦漏扫, 它会原样出现在 LS 端 task.data 里 ——
#: 这是"含凭据的样本一条都不能上去"唯一能验的地方。
_LEAK = "sk-abcdefghij1234567890"

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


def _task_items(response):
    """``GET /api/tasks`` 的响应 → **task 列表**。

    本机 LS 1.23 三个测试里原本写的是 ``r.get("results", r)``, 而该端点实际回的键
    是 ``tasks``(响应字段: total_annotations / total_predictions / total / tasks)。
    取不到就整个退化成 ``r`` —— 接着 ``for t in r`` 迭代的是 dict 的**键**, 于是
    ``t.get("inner_id", "")`` 抛 ``'str' object has no attribute 'get'``。

    这三处失败与被测逻辑无关(全链路推送本身是通的), 但它把整个集成测试变成了
    常红: 端点返回键名对不上, 而这类"断言写错 vs 代码写错"混在一起时, 没人会去
    怀疑前者。``results`` 那条留着, 不同版本/不同端点形态不排除。
    """
    if not isinstance(response, dict):
        return response
    return response.get("tasks", response.get("results", response))


def _task_data(items, session_id: str) -> dict:
    """按 session_id 从 LS 端的 task 列表里取出 ``task.data``。

    **只能靠 ``data.session_id`` 找, 不能靠 ``task.inner_id``** —— 后者是 LS 自己
    的自增整数: exporter 发过去的字符串被静默丢弃, 端上拿到的是 ``1`` / ``2``。
    (实测 2026-09-30: 发 ``useramulation-20260928-abc``, LS 端 ``inner_id == 1``。)
    同一个事实也让"靠 LS 原生 inner_id 去重"这条路不存在, 去重实际由本地
    ``push_index`` 台账做, 见设计文档 §16 R7 与 ``test_push_batch_dedupes_against_index``。
    """
    for task in items:
        if (task.get("data") or {}).get("session_id") == session_id:
            return task["data"]
    raise AssertionError(
        f"LS 端没找到 session_id={session_id!r} 的 task; "
        f"现有 = {[((t.get('data') or {}).get('session_id')) for t in items]}"
    )


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
        items = _task_items(tasks)
        data = _task_data(items, "useramulation-20260928-abc")
        assert data["task_id"] == "T001"
        assert data["scorecard"]["schema_version"] == "scorecard.v1"
        # criteria_text 是换行分隔的单串（LS 绑 list 给文本标签会 400，
        # 绑 list 给 <Text> 会被 "," 连成一整段 —— 见 _criteria_text）
        assert isinstance(data["criteria_text"], str)
        assert "C1" in data["criteria_text"].splitlines()[0]
    finally:
        client.delete_project(project_id)


def test_credential_leak_is_never_uploaded(client, ls_settings, tmp_path: Path):
    """R11 端到端: 含凭据的 C3 一条都不能出现在 LS 端。"""
    from label_studio.project_manager import init_project
    from label_studio.task_exporter import export_batch, push_batch

    write_c3(tmp_path / "refine_data", session_id="leaky",
             messages={"messages": [{"content": _LEAK}]})
    write_c3(tmp_path / "refine_data", session_id="clean")
    project_id = init_project(client, ls_settings)["project_id"]
    try:
        plan = export_batch(tmp_path / "refine_data", settings=ls_settings)
        assert [t["inner_id"] for t in plan.tasks] == ["useramulation-20260928-abc"]
        assert plan.rejected and plan.rejected[0][0].startswith("T001__leaky")
        push_batch(plan, settings=ls_settings, project_id=project_id,
                   client_factory=lambda: client)
        tasks = client._request("GET", f"/api/tasks?project={project_id}&page_size=50")
        items = _task_items(tasks)
        # 判据是**凭据串本身**, 不是 session_id: write_c3 的 session_id 走
        # setdefault, 撞上 RICH_META 已有的值就不改写了, 于是 leaky 与 clean
        # 两条的 data.session_id 是同一个值, 根本区分不开。
        #
        # 也刻意不用 `all("leaky" not in ... for t in items)`: 那个写法对**空列表**
        # 返回 True —— 整批一条都没推上去时它照样绿, 而"什么都没推"与"拒推了含凭据
        # 的那条"是完全相反的两种结局。先钉条数, 再钉内容。
        assert len(items) == 1, f"拒推后端上应当只剩 1 条, 实际 {len(items)} 条"
        assert _LEAK not in json.dumps(items[0], ensure_ascii=False, default=str)
    finally:
        client.delete_project(project_id)
