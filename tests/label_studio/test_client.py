"""label_studio.client 单元测试 (P1, 2026-09-28).

不打真实网络 —— 用 ``httpx.MockTransport`` 替换 transport 层。覆盖:
状态码 → 异常的映射、重试语义、错误信息脱敏、import 分批上限。
"""

from __future__ import annotations

import json

import httpx
import pytest

from label_studio.client import (
    MAX_TASKS_PER_IMPORT,
    LabelStudioClient,
    build_client,
    read_label_config,
)
from label_studio.errors import (
    LabelStudioAuthFailed,
    LabelStudioError,
    LabelStudioUnavailable,
)
from label_studio.settings import HealthCheckSettings, LabelStudioSettings

API_KEY = "placeholder-not-a-real-credential"


def _client(*, retry_attempts: int = 1) -> LabelStudioClient:
    """构造一个 ``base_url`` 不可达的 client —— 传输层由 ``_patch_transport`` 桩。"""
    return LabelStudioClient(
        "http://ls.invalid",
        API_KEY,
        settings=HealthCheckSettings(
            retry_attempts=retry_attempts, retry_backoff_seconds=0.0
        ),
    )


def _patch_transport(monkeypatch, handler) -> None:
    """让 ``httpx.Client(...)`` 默认带上 MockTransport。"""
    original = httpx.Client.__init__

    def patched(self, *args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched)


# ---------------------------------------------------------------------------
# 构造 / 凭据
# ---------------------------------------------------------------------------


def test_build_client_requires_credentials():
    with pytest.raises(LabelStudioAuthFailed, match="LABEL_STUDIO_API_KEY"):
        build_client(LabelStudioSettings())


def test_build_client_strips_trailing_slash():
    s = LabelStudioSettings(base_url="http://ls:8099/", api_key="k")
    assert build_client(s)._base_url == "http://ls:8099"


def test_constructor_rejects_empty_key():
    with pytest.raises(LabelStudioAuthFailed):
        LabelStudioClient("http://ls", "")


# ---------------------------------------------------------------------------
# 状态码映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure(monkeypatch, status):
    _patch_transport(monkeypatch, lambda r: httpx.Response(status))
    with pytest.raises(LabelStudioAuthFailed):
        _client().health_check()


def test_network_error_is_unavailable(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("connection refused")

    _patch_transport(monkeypatch, boom)
    with pytest.raises(LabelStudioUnavailable, match="不可达"):
        _client().health_check()


def test_server_error_is_unavailable(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(503))
    with pytest.raises(LabelStudioUnavailable):
        _client().list_projects()


def test_404_raises_generic(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(404))
    with pytest.raises(LabelStudioError, match="404"):
        _client().get_project(1)


def test_4xx_body_is_redacted(monkeypatch):
    """LS 4xx body 可能回显请求体; 脱敏后才进异常消息。"""
    secret = "sk-abcdefghij1234567890"

    def handler(request):
        return httpx.Response(400, text=f"bad payload {secret}")

    _patch_transport(monkeypatch, handler)
    with pytest.raises(LabelStudioError) as exc:
        _client().create_project(title="t", label_config="<View/>")
    assert secret not in str(exc.value)
    assert "***" in str(exc.value)


def test_non_json_response_is_wrapped(monkeypatch):
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, text="<html>hi</html>"))
    with pytest.raises(LabelStudioError, match="非 JSON"):
        _client().list_projects()


def test_health_ok(monkeypatch):
    payload = {"status": "UP"}
    _patch_transport(monkeypatch, lambda r: httpx.Response(200, json=payload))
    assert _client().health_check() == payload


def test_health_non_json_is_tolerated(monkeypatch):
    handler = lambda r: httpx.Response(200, text="pong")  # noqa: E731
    _patch_transport(monkeypatch, handler)
    assert _client().health_check()["status"] == "ok"


# ---------------------------------------------------------------------------
# 重试
# ---------------------------------------------------------------------------


def test_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, json=[])

    _patch_transport(monkeypatch, handler)
    client = _client(retry_attempts=3)
    client._health = HealthCheckSettings(retry_attempts=3, retry_backoff_seconds=0.0)
    assert client.list_projects() == []
    assert calls["n"] == 3


def test_auth_error_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401)

    _patch_transport(monkeypatch, handler)
    client = _client(retry_attempts=3)
    client._health = HealthCheckSettings(retry_attempts=3, retry_backoff_seconds=0.0)
    with pytest.raises(LabelStudioAuthFailed):
        client.list_projects()
    assert calls["n"] == 1        # 401 重试没有意义


def test_create_project_not_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(409)

    _patch_transport(monkeypatch, handler)
    with pytest.raises(LabelStudioError):
        _client(retry_attempts=3).create_project(title="t", label_config="x")
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"results": [{"id": 1}, {"id": 2}]}, 2),
        ([{"id": 1}], 1),
        ({"id": 7}, 1),
        ({}, 0),
    ],
)
def test_list_projects_shapes(monkeypatch, payload, expected):
    handler = lambda r: httpx.Response(200, json=payload)  # noqa: E731
    _patch_transport(monkeypatch, handler)
    assert len(_client().list_projects()) == expected


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"task_count": 3}, 3),
        ({"tasks": [1, 2]}, 2),
        ([1, 2, 3], 3),
        # 服务端一个计数键都没给 —— 才按"发了 N 条"兜底
        ({"weird": 1}, 1),
    ],
)
def test_import_task_count_shapes(monkeypatch, payload, expected):
    handler = lambda r: httpx.Response(200, json=payload)  # noqa: E731
    _patch_transport(monkeypatch, handler)
    assert _client().import_tasks(1, [{"data": {}}]) == expected


@pytest.mark.parametrize(
    "payload,expected",
    [
        # 1.23.0 实际返回的键是 created, 不是 task_count —— 漏读会报 0
        ({"created": 2}, 2),
        ({"task_count": 2}, 2),
        # 服务端明说"建成了 0 条"时, 0 就是答案, 不能被 sent 覆盖
        ({"created": 0}, 0),
        ({"task_count": 0, "prediction_count": 0}, 0),
        # 真的没给计数才兜底
        ({"weird": 1}, 1),
    ],
)
def test_import_predictions_count_shapes(monkeypatch, payload, expected):
    handler = lambda r: httpx.Response(201, json=payload)  # noqa: E731
    _patch_transport(monkeypatch, handler)
    assert _client().import_predictions(1, [{"task": 1, "result": []}]) == expected


def test_server_side_zero_is_not_overwritten(monkeypatch):
    """``created: 0`` 是**真的 0 条** —— 预测格式不对时 LS 照样回 201。

    这里要是按 sent 兜底, 报告就会显示 ``predictions_pushed: 1``, 而 LS 里
    一条 prediction 都没有。假成功比假失败难查得多。
    """
    handler = lambda r: httpx.Response(201, json={"created": 0})  # noqa: E731
    _patch_transport(monkeypatch, handler)
    assert _client().import_predictions(1, [{"task": 1, "result": {}}]) == 0


def test_validate_label_config_returns_error_list(monkeypatch):
    handler = lambda r: httpx.Response(200, json=[{"detail": "bad tag"}])  # noqa: E731
    _patch_transport(monkeypatch, handler)
    assert _client().validate_label_config(1, "<View/>") == [{"detail": "bad tag"}]


def test_validate_label_config_accepts_error_key(monkeypatch):
    payload = {"validation_errors": [{"detail": "x"}]}
    handler = lambda r: httpx.Response(200, json=payload)  # noqa: E731
    _patch_transport(monkeypatch, handler)
    assert _client().validate_label_config(1, "<View/>") == [{"detail": "x"}]


# ---------------------------------------------------------------------------
# 批量上限
# ---------------------------------------------------------------------------


def test_import_tasks_enforces_limit():
    client = LabelStudioClient("http://ls", API_KEY)
    with pytest.raises(LabelStudioError, match="batch_size"):
        client.import_tasks(1, [{"data": {}}] * (MAX_TASKS_PER_IMPORT + 1))


def test_import_predictions_enforces_limit():
    client = LabelStudioClient("http://ls", API_KEY)
    with pytest.raises(LabelStudioError, match="batch_size"):
        client.import_predictions(1, [{"task": "x"}] * (MAX_TASKS_PER_IMPORT + 1))


def test_import_empty_batch_is_noop_without_http():
    client = LabelStudioClient("http://ls", API_KEY)
    assert client.import_tasks(1, []) == 0
    assert client.import_predictions(1, []) == 0


# ---------------------------------------------------------------------------
# label_config 读取
# ---------------------------------------------------------------------------


def test_read_label_config(tmp_path):
    path = tmp_path / "c.xml"
    path.write_text("<View/>", encoding="utf-8")
    assert read_label_config(path) == "<View/>"


def test_read_label_config_missing_raises(tmp_path):
    with pytest.raises(LabelStudioError, match="无法读取"):
        read_label_config(tmp_path / "nope.xml")


# ---------------------------------------------------------------------------
# 凭据不出现在请求体日志里
# ---------------------------------------------------------------------------


def test_api_key_only_in_auth_header(monkeypatch):
    seen: dict = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        seen["content"] = request.content
        return httpx.Response(200, json=[])

    _patch_transport(monkeypatch, handler)
    _client().list_projects()
    assert seen["headers"]["authorization"] == f"Token {API_KEY}"
    assert API_KEY.encode() not in seen["content"]
