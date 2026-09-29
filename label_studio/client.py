"""label_studio.client: Label Studio REST API 的最小包装.

**不引入 ``label-studio-sdk``** —— 只需要 6 个端点, SDK 会带来一整条依赖链
(且版本与 LS 服务端耦合)。直接用项目已有的 ``httpx`` 打 REST。

端点（方案 §7）::

    GET    /health
    GET    /api/projects
    POST   /api/projects
    DELETE /api/projects/{id}
    POST   /api/projects/{id}/import
    POST   /api/projects/{id}/import/predictions
    POST   /api/projects/{id}/validate/

约定：**client 用完即弃，不做模块级单例**（方案 §9.3）。LS 项目配置
（label_config / project_id）落 `config/config.yaml` 与 LS 端自身，进程内不缓存
任何状态 —— 推送失败后重跑 `upload` 一定能拿到真实状态，不会读到过期缓存。

凭据红线：本模块**不打印也不记录** api_key。所有异常经
:mod:`label_studio.errors` 统一脱敏。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from label_studio.errors import (
    LabelStudioAuthFailed,
    LabelStudioError,
    LabelStudioUnavailable,
    redact,
)
from label_studio.settings import HealthCheckSettings, LabelStudioSettings

log = logging.getLogger(__name__)

#: LS 单次 import 的任务上限（服务端硬限 250K，这里保守取小）。
MAX_TASKS_PER_IMPORT = 1000


class LabelStudioClient:
    """LS REST 客户端。**用完即弃**：一个进程/一次推送建一个实例。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        settings: HealthCheckSettings | None = None,
    ) -> None:
        if not api_key:
            raise LabelStudioAuthFailed("未提供 Label Studio API key")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._health = settings or HealthCheckSettings()

    # -- 底层 ---------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            # LS 同时接受 Token 与 Bearer，两种都行；Token 对老版本更稳。
            "Authorization": f"Token {self._api_key}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        retry: bool = True,
    ) -> Any:
        """发一次请求；``retry=True`` 时按 health_check 配置做退避重试。"""
        import httpx

        attempts = self._health.retry_attempts if retry else 1
        last: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                with httpx.Client(
                    base_url=self._base_url, headers=self._headers(), timeout=self._timeout
                ) as client:
                    response = client.request(method, path, json=json_body)
                return self._handle(response, method, path)
            except (LabelStudioAuthFailed, LabelStudioUnavailable) as exc:
                last = exc
                if isinstance(exc, LabelStudioAuthFailed) or attempt >= attempts:
                    raise
            except httpx.HTTPError as exc:
                last = LabelStudioUnavailable(f"{method} {path} 网络错误: {redact(exc)}")
                if attempt >= attempts:
                    raise last from exc
            if attempt < attempts:
                time.sleep(self._health.retry_backoff_seconds * attempt)
        raise last or LabelStudioError(f"{method} {path} 失败")

    @staticmethod
    def _handle(response: Any, method: str, path: str) -> Any:
        status = response.status_code
        if status in (401, 403):
            raise LabelStudioAuthFailed(
                f"{method} {path} 认证失败 (HTTP {status}): API key 无效或权限不足"
            )
        if status == 404:
            raise LabelStudioError(f"{method} {path} 不存在 (HTTP 404)")
        if status >= 500:
            raise LabelStudioUnavailable(f"{method} {path} 服务端错误 (HTTP {status})")
        if status >= 400:
            # 4xx 的 body 常含 label_config 校验详情, 有诊断价值 —— 但先脱敏。
            raise LabelStudioError(f"{method} {path} 请求被拒 (HTTP {status}): {redact(response.text)}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LabelStudioError(f"{method} {path} 返回非 JSON: {redact(response.text)[:200]}") from exc

    # -- 端点 ---------------------------------------------------------------

    def health_check(self) -> dict[str, Any]:
        """``GET /health``。不可达时抛 :class:`LabelStudioUnavailable`。"""
        import httpx

        try:
            with httpx.Client(
                base_url=self._base_url, headers=self._headers(), timeout=self._health.timeout_seconds
            ) as client:
                response = client.get("/health")
        except httpx.HTTPError as exc:
            raise LabelStudioUnavailable(
                f"Label Studio 不可达 @ {self._base_url}: {redact(exc)}"
            ) from exc
        if response.status_code in (401, 403):
            # 401/403 报 "不可达" 会把人引到错误方向 (查网络 / 查端口),
            # 实际是 key 无效。
            raise LabelStudioAuthFailed(
                f"Label Studio /health 认证失败 (HTTP {response.status_code}); "
                f"检查 API key"
            )
        if response.status_code >= 400:
            raise LabelStudioUnavailable(
                f"Label Studio /health 返回 HTTP {response.status_code} @ {self._base_url}"
            )
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return {"status": "ok", "raw": response.text[:200]}

    def list_projects(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        payload = self._request("GET", "/api/projects", json_body={"page_size": page_size})
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list):
                return results
            return [payload] if payload.get("id") else []
        return payload if isinstance(payload, list) else []

    def get_project(self, project_id: int) -> dict[str, Any]:
        return self._request("GET", f"/api/projects/{int(project_id)}")

    def create_project(
        self,
        *,
        title: str,
        label_config: str,
        project_type: str = "DocumentClassification",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/projects",
            json_body={
                "title": title,
                "description": "simulation_controller C3 轨迹 + 评分卡质量评审",
                "label_config": label_config,
                "project_type": project_type,
            },
            retry=False,  # 409 冲突不该重试
        )

    def delete_project(self, project_id: int) -> None:
        self._request("DELETE", f"/api/projects/{int(project_id)}", retry=False)

    def validate_label_config(self, project_id: int, label_config: str) -> list[dict[str, Any]]:
        """``POST /api/projects/{id}/validate/`` —— 返回错误列表, 空表示通过。"""
        payload = self._request(
            "POST",
            f"/api/projects/{int(project_id)}/validate/",
            json_body={"label_config": label_config},
            retry=False,
        )
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            errors = payload.get("validation_errors") or payload.get("errors") or []
            return [item for item in errors if isinstance(item, dict)]
        return []

    def import_tasks(self, project_id: int, tasks: Iterable[Mapping[str, Any]]) -> int:
        """``POST /api/projects/{id}/import``。返回 LS 侧接受的任务数。"""
        batch = list(tasks)
        if not batch:
            return 0
        if len(batch) > MAX_TASKS_PER_IMPORT:
            raise LabelStudioError(
                f"单批 {len(batch)} 条超过上限 {MAX_TASKS_PER_IMPORT}, 请调小 upload.batch_size"
            )
        payload = self._request(
            "POST",
            f"/api/projects/{int(project_id)}/import",
            json_body=batch,
            retry=False,
        )
        return _task_count_from(payload)

    def import_predictions(
        self,
        project_id: int,
        predictions: Iterable[Mapping[str, Any]],
    ) -> int:
        """``POST /api/projects/{id}/import/predictions`` —— ML 预标注（方案 §5.2）。"""
        batch = list(predictions)
        if not batch:
            return 0
        if len(batch) > MAX_TASKS_PER_IMPORT:
            raise LabelStudioError(
                f"单批 {len(batch)} 条预测超过上限 {MAX_TASKS_PER_IMPORT}, 请调小 batch_size"
            )
        payload = self._request(
            "POST",
            f"/api/projects/{int(project_id)}/import/predictions",
            json_body=batch,
            retry=False,
        )
        return _task_count_from(payload)


def _task_count_from(payload: Any) -> int:
    """LS import 返回体在不同版本间形态不一, 尽力取任务数。"""
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("task_count", "annotation_count", "prediction_count"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
        for key in ("tasks", "annotations", "predictions"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    return 0


def build_client(
    settings: LabelStudioSettings, *, require_credentials: bool = True
) -> LabelStudioClient:
    """从 settings 建 client；无凭据时按需报错。"""
    api_key = settings.resolve_api_key()
    if not api_key:
        if require_credentials:
            raise LabelStudioAuthFailed(
                "未找到 Label Studio 凭据: 请设置 label_studio.api_key "
                "(${LABEL_STUDIO_API_KEY}) 或 label_studio.api_key_path"
            )
        raise LabelStudioAuthFailed("未找到 Label Studio 凭据")
    return LabelStudioClient(
        settings.base_url,
        api_key,
        timeout=settings.health_check.timeout_seconds * 6,
        settings=settings.health_check,
    )


def read_label_config(path: Path) -> str:
    """读 label_config XML, 不存在则报清晰错误。"""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise LabelStudioError(f"无法读取 label_config {path}: {redact(exc)}") from exc


__all__ = [
    "LabelStudioClient",
    "MAX_TASKS_PER_IMPORT",
    "build_client",
    "read_label_config",
]
