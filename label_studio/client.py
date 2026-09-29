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

import base64
import binascii
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

#: access token 提前多久刷新（秒）。LS 的 access token 只活 ~5 分钟, 留出余量
#: 免得请求发出时刚好过期 —— 那会表现为毫无规律的间歇性 401。
_ACCESS_REFRESH_MARGIN_S = 60.0

#: 读不出 ``exp`` 时的兜底有效期（秒）。宁可早刷一次, 也不能拿一个会突然失效
#: 的 token 去发请求。
_ACCESS_FALLBACK_TTL_S = 240.0


def _is_jwt(token: str) -> bool:
    """是 LS 的 Personal Access Token（JWT）还是 legacy token。

    LS 有两代凭据 (官方文档 "Manage Your Organization > Access tokens"):

    * **PAT** —— JWT, 三段 ``header.payload.signature``。它是**刷新令牌**:
      直接当 API key 用一律 401 ``Invalid token.``; 必须先
      ``POST /api/token/refresh`` 换短时 access token, 再用 ``Bearer`` 调接口。
    * **Legacy token** —— 40 字符十六进制, 永不过期, 直接 ``Authorization: Token``。

    按**点数**判断而不是看前缀: legacy token 的形态将来可能变, JWT 的段数不会。
    """
    return token.count(".") == 2


def _jwt_exp(token: str) -> float | None:
    """从 JWT payload 读 ``exp``（不验签 —— 我们只是读时钟, 验签由 LS 负责）。

    读不出返回 None, 调用方走兜底 TTL。
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)  # base64url 去掉了填充
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(claims, dict):
        return None
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


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
        # PAT 换取的短时 access token 缓存 (legacy token 不走这条路, 恒为 None)
        self._access: str | None = None
        self._access_expires_at: float = 0.0

    # -- 凭据 ---------------------------------------------------------------

    def _authorization(self) -> str:
        """``Bearer <access>``（PAT）或 ``Token <key>``（legacy）。"""
        if not _is_jwt(self._api_key):
            return f"Token {self._api_key}"
        return f"Bearer {self._access_token()}"

    def _access_token(self) -> str:
        """取 access token, 过期（含临期）则重新换。"""
        now = time.time()
        if self._access and now < self._access_expires_at - _ACCESS_REFRESH_MARGIN_S:
            return self._access
        self._refresh_access_token(now)
        return self._access or ""

    def _refresh_access_token(self, now: float | None = None) -> None:
        """``POST /api/token/refresh`` —— PAT 换短时 access token。

        PAT 走 **body** (``{"refresh": ...}``), 不走 Authorization 头 ——
        实测 1.23.0 放头里返回 400 ``refresh: This field is required.``。
        """
        import httpx

        try:
            with httpx.Client(base_url=self._base_url, timeout=self._timeout) as client:
                response = client.post(
                    "/api/token/refresh", json={"refresh": self._api_key}
                )
        except httpx.HTTPError as exc:
            raise LabelStudioAuthFailed(
                f"Label Studio 换取 access token 失败（网络错误）: "
                f"{redact(exc)} @ {self._base_url}"
            ) from exc

        if response.status_code in (400, 401, 403):
            raise LabelStudioAuthFailed(
                f"Label Studio Personal Access Token 无效或已撤销 "
                f"(HTTP {response.status_code}); 去 /user/account 重新生成"
            )
        if response.status_code >= 400:
            raise LabelStudioUnavailable(
                f"POST /api/token/refresh 服务端错误 "
                f"(HTTP {response.status_code}) @ {self._base_url}"
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LabelStudioAuthFailed(
                f"PAT 换取 access token 的响应不是 JSON: {redact(response.text)[:200]}"
            ) from exc
        access = payload.get("access") if isinstance(payload, dict) else None
        if not access:
            raise LabelStudioAuthFailed("PAT 换取 access token 的响应里没有 access 字段")

        self._access = str(access)
        self._access_expires_at = (
            _jwt_exp(self._access)
            or (now if now is not None else time.time()) + _ACCESS_FALLBACK_TTL_S
        )
        log.info(
            "Label Studio: PAT 已换取 access token (%.0f 秒后过期)",
            self._access_expires_at - time.time(),
        )

    # -- 底层 ---------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._authorization(),
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

    def update_project(
        self,
        project_id: int,
        *,
        label_config: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """``PATCH /api/projects/{id}`` —— 改已存在项目的字段。

        存在的理由: 复用同名项目时, LS 端**存着当初建项目那一刻**的
        label_config。本地 XML 改了不推过去, 项目就一直按旧配置渲染 ——
        而 ``POST .../validate/`` 校验的是你递过去的 XML 文本, 不是项目里
        存的那份, 于是校验报绿、import 却 400, 排查起来极其费劲。
        """
        body: dict[str, Any] = {}
        if label_config is not None:
            body["label_config"] = label_config
        if title is not None:
            body["title"] = title
        if not body:
            raise LabelStudioError("update_project 至少需要 label_config 或 title 之一")
        return self._request(
            "PATCH", f"/api/projects/{int(project_id)}", json_body=body, retry=False
        )

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
        return _accepted_count(payload, len(batch))

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
        return _accepted_count(payload, len(batch))

    def list_recent_tasks(
        self, project_id: int, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """``GET /api/projects/{id}/tasks?page_size=N`` —— **倒序**, 只回
        ``id`` / ``data`` / ``meta`` / ``created_at``。

        存在的理由: ``POST /import`` 的返回体只有计数, 不带 task id; 而
        ``import/predictions`` 的 ``task`` 字段**只认** LS 侧的数字 id。
        所以推完一批得回头把它刚建的那些 id 捞出来。

        用这个端点而不是 ``/api/tasks``: 后者永远返回全量字段（含整条
        trajectory 和一堆 annotation 计数）, 实测同 2 条 task 是 3181 vs
        986 字符 —— 大项目里这个差距是数量级的。**代价**: 它靠 id 倒序而非
        时间戳, 同一瞬间若有别的进程往同一项目推数据, 取到的最新 N 条里
        可能混进别人的。本项目的推送是单进程串行的, 接受这个假设。
        """
        payload = self._request(
            "GET",
            f"/api/projects/{int(project_id)}/tasks?page_size={int(limit)}",
        )
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            tasks = payload.get("tasks") or payload.get("results") or []
            return [item for item in tasks if isinstance(item, dict)]
        return []


def _task_count_from(payload: Any) -> int | None:
    """LS import 返回体在不同端点/版本间形态不一, 尽力取计数。

    **返回 ``None`` 表示"服务端没给出计数"**, 与"服务端说 0"是两回事 ——
    这个区分是必须的, 见 :func:`_accepted_count`。

    实测 LS 1.23.0:
    - ``/import`` → ``{"task_count": N, "annotation_count": N}``
    - ``/import/predictions`` → ``{"created": N}``   ← 键名不一样, 极易漏
    """
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("task_count", "created", "annotation_count", "prediction_count"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
        for key in ("tasks", "annotations", "predictions"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
    return None


def _accepted_count(payload: Any, sent: int) -> int:
    """服务端给了计数就照抄, **没给**才按 ``sent`` 兜底。

    这里的 0 是**真的 0**: ``import/predictions`` 对格式不对的预测会返回
    ``{"created": 0}`` —— 201、一条 prediction 也没建成, 而 0 恰恰是正确
    的答案(参见 :func:`build_prediction` 里 ``result`` 必须是 region 列表)。
    早先这里写成 ``_task_count_from(payload) or sent``, 把这个诚实的 0 覆盖成
    "发了几条就是几条", 于是报告一边显示 ``predictions_pushed: 1``、LS 里
    一条都没有 —— **假成功比假失败更难查**。
    """
    counted = _task_count_from(payload)
    return sent if counted is None else counted


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
