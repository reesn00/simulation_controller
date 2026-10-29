"""ls_bridge.client: Label Studio REST API 的最小包装.

照读自 ``archive/v1/label_studio/client.py``（v1 方案 §7/§9.3）, 仅改
import 路径与配置类型。

**不引入 ``label-studio-sdk``** —— 只需要 7 个端点, SDK 会带来一整条依赖
链（且版本与 LS 服务端耦合）。直接用项目已有的 ``httpx`` 打 REST。

端点::

    GET    /health
    GET    /api/projects
    POST   /api/projects
    PATCH  /api/projects/{id}
    DELETE /api/projects/{id}
    POST   /api/projects/{id}/import
    POST   /api/projects/{id}/import/predictions
    POST   /api/projects/{id}/validate/
    GET    /api/projects/{id}/tasks?page_size=N
    GET    /api/tasks?project={id}&page_size=N&page=P

约定: **client 用完即弃, 不做模块级单例**。LS 项目配置（label_config /
project_id）落 LS 端自身, 进程内不缓存任何状态 —— 推送失败后重跑
``upload`` 一定能拿到真实状态, 不会读到过期缓存。

凭据红线: 本模块**不打印也不记录** api_key。所有异常经
:mod:`trajectory_pipeline.evaluation.ls_bridge.errors` 统一脱敏。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from trajectory_pipeline.evaluation.ls_bridge.errors import (
    LabelStudioAuthFailed,
    LabelStudioError,
    LabelStudioUnavailable,
    redact,
)
from trajectory_pipeline.evaluation.ls_bridge.settings import (
    HealthCheckSettings,
    LSSettings,
)

log = logging.getLogger(__name__)

#: LS 单次 import 的任务上限（服务端硬限 250K, 这里保守取小）。
MAX_TASKS_PER_IMPORT = 1000

#: 翻页取 task 的页数上限（**兜底**，不是业务上限）——
#: 一版黄金集 294 条 ⇒ 100/页 也就 3 页；这个数只是防「翻页翻到手抽筋」。
_MAX_TASK_PAGES = 1000

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
    """LS REST 客户端。**用完即弃**: 一次推送建一个实例。"""

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
        """发一次请求; ``retry=True`` 时按 health_check 配置做退避重试。"""
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
            raise LabelStudioError(
                f"{method} {path} 请求被拒 (HTTP {status}): {redact(response.text)}"
            )
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise LabelStudioError(
                f"{method} {path} 返回非 JSON: {redact(response.text)[:200]}"
            ) from exc

    # -- 端点 ---------------------------------------------------------------

    def health_check(self) -> dict[str, Any]:
        """``GET /health``。不可达时抛 :class:`LabelStudioUnavailable`。"""
        import httpx

        try:
            with httpx.Client(
                base_url=self._base_url,
                headers=self._headers(),
                timeout=self._health.timeout_seconds,
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
        description: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/projects",
            json_body={
                "title": title,
                "description": description
                or "trajectory_pipeline scout 候选 + scorecard.v2 质量评审",
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

        ⚠️ ls_bridge 设计约束（见 ``__init__.py``）: 双向化之前**禁全量
        PATCH label_config** —— PATCH 会覆盖标注员在 LS 端的改动。本方法
        仅供 ``init-project`` 复用同名项目时同步 label_config, 且仅在项目
        尚无标注时调用。
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
        """``POST /api/projects/{id}/validate/`` —— 返回错误列表, 空表示通过。

        ⚠️ **绿灯不算数**: 它校验的是递过去的 XML 文本, 不是项目里存的那份
        （ls_bridge 设计约束）, 验收以标注页实视为准。
        """
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
                f"单批 {len(batch)} 条超过上限 {MAX_TASKS_PER_IMPORT}, 请调小 --batch-size"
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
        """``POST /api/projects/{id}/import/predictions`` —— 评分卡预标注。"""
        batch = list(predictions)
        if not batch:
            return 0
        if len(batch) > MAX_TASKS_PER_IMPORT:
            raise LabelStudioError(
                f"单批 {len(batch)} 条预测超过上限 {MAX_TASKS_PER_IMPORT}, 请调小 --batch-size"
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

        **代价**: 它靠 id 倒序而非时间戳, 同一瞬间若有别的进程往同一项目
        推数据, 取到的最新 N 条里可能混进别人的。本桥的推送是单进程串行的,
        接受这个假设。
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

    def list_project_tasks(
        self, project_id: int, *, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """``GET /api/tasks?project={id}`` —— **分页**取该项目的全部 task。

        ⚠️ **为什么不用 ``/api/projects/{id}/tasks``**：LS 1.23 里那个端点
        **不分页**（``page_size`` 只是截断，且返回的是**倒序**的一段）⇒
        一版 294 条只能拿到前 N 条，剩下的**静默少掉**。
        ``/api/tasks`` 返回 ``{"tasks": [...], "total": N}`` 且**支持
        ``page``** ⇒ 按 ``total`` 翻页取完。

        ⚠️ **为什么不能拿 export 端点代替**：``/api/projects/{id}/export``
        **只给已标注的 task**（一条标注都没有时是 ``[]``）⇒
        「**没复核的行也要出一行**」这条要求根本满足不了——漏掉没复核的那些
        会让「覆盖率」读成 100%。

        ⚠️ **翻页翻到头就停**：``total`` 与「这页没满」两个条件任一成立即止；
        两者都不成立而页码还在涨 ⇒ **抛错**（不静默截断——截断出来的
        「导出」看起来是完整的）。
        """
        collected: list[dict[str, Any]] = []
        page = 1
        total: int | None = None
        while True:
            payload = self._request(
                "GET",
                f"/api/tasks?project={int(project_id)}"
                f"&page_size={int(page_size)}&page={page}",
            )
            if not isinstance(payload, dict):
                raise LabelStudioError(
                    f"project {project_id} 的 task 列表形态异常（期望 dict, 得到 "
                    f"{type(payload).__name__}）"
                )
            batch = payload.get("tasks")
            batch = batch if isinstance(batch, list) else []
            collected.extend(t for t in batch if isinstance(t, dict))
            if total is None:
                raw_total = payload.get("total")
                total = int(raw_total) if isinstance(raw_total, int) else None
            if not batch or len(batch) < int(page_size):
                break
            if total is not None and len(collected) >= total:
                break
            page += 1
            if page > _MAX_TASK_PAGES:
                raise LabelStudioError(
                    f"project {project_id} 翻页超过 {_MAX_TASK_PAGES} 页仍未取完"
                    "（total 与实取数对不上）——不静默截断"
                )
        return collected

    def list_task_annotations(self, task_id: int) -> list[dict[str, Any]]:
        """``GET /api/tasks/{id}/annotations`` —— 这条 task 的**全部**标注。

        ⚠️ **为什么需要它**：任务列表（``/api/tasks?project=``）里的
        ``annotations`` 数组**恒为空**（实测：``total_annotations=1`` 而
        ``annotations=[]``）⇒ 从任务列表里读标注**永远读到 0 条**，
        而那读起来跟「一条都没复核」**一模一样**（静默的错）。

        ⚠️ **端点名别猜**：``/api/annotations?task=3`` 在 LS 1.23 里是
        **404**；``/api/tasks/3/annotations`` 才是真的。

        ⚠️ **它也是唯一带 ``created_username`` 的地方**（实测形态：
        ``" reesn@qq.com, 1"``）⇒ 批量端点给的那份只有数字
        ``completed_by``⇒ **只读批量端点，署名栏会读成「1」**。
        """
        payload = self._request("GET", f"/api/tasks/{int(task_id)}/annotations")
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            items = payload.get("annotations") or payload.get("results")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
            # ⚠️ 有的部署回**单条**标注对象（带 ``result`` / ``task``）⇒
            # 别当「没有标注」——那会把一条标注静默丢掉。
            if "result" in payload or "task" in payload:
                return [payload]
            return []
        return []

    def export_project_json(self, project_id: int) -> dict[str, Any]:
        """整个项目的 task + annotation 快照（**口径见** :meth:`list_project_tasks`）。

        返回 ``{"tasks": [...], "annotations": [...], "mismatches": [...]}``：
        ``tasks`` 是该项目的**全部** task（含未标注的），``annotations`` 是全部
        标注的扁平列表（每条都带 ``task`` 字段指回 LS 侧 task id），
        ``mismatches`` 是**两个端点对不上账**的地方（见下）。

        **存在的理由**：client 里原本**没有任何读 annotation 的方法**——要导出
        人工裁定（裁定④：annotator 身份单列一栏）就得靠这个端点。

        ⚠️⚠️ **实测（LS 1.23.0，2026-10-29）打翻了初稿的两处写法**：

        1. ``GET /api/projects/{id}/export?exportType=JSON`` 返回的是
           **list**（不是初稿假设的 dict），且**只含已标注的 task**
           ——一条标注都没有时是 ``[]``；
        2. 任务列表里的 ``annotations`` **恒为空**（``total_annotations=1``
           而 ``annotations=[]``）⇒ **从任务列表里读标注永远读到 0 条**。

        ⚠️⚠️ **第三个坑（也是最贵的那个）：两个端点给的「同一条标注」字段
        **不一样**——**身份字段只在一处有**：

        =======================  ==========================  ==================
        字段                     ``/export``（批量端点）      ``/api/tasks/{id}``
        =======================  ==========================  ==================
        ``id`` / ``task``        有                          有
        ``result``（裁决四栏）    有                          有
        ``created_username``     **没有**（实测键集里不存在）  **有**（``" 账号, id"``）
        ``completed_by``         有（**只有数字 id**）        有
        =======================  ==========================  ==================

        ⇒ 只读批量端点 ⇒ **署名栏读出来是「1」**（数字 id），而不是账号——
        而裁定④要的就是「谁在复核」。⇒ **每条有标注的 task 都去逐条端点
        取一份**，两边**按 annotation id 合并**（逐条那份补上批量那份缺的字段）。

        ⚠️ **不是「信任逐条端点」而是「两边对账」**：条数与任务列表说的
        ``total_annotations`` 对不上时，**记进 ``mismatches``**（不静默、
        也不静默吞掉）——少报比多报更难发现：它读起来像「没复核」。
        """
        tasks = self.list_project_tasks(project_id)

        bulk_by_task: dict[Any, list[dict[str, Any]]] = {}
        exported = self._request(
            "GET", f"/api/projects/{int(project_id)}/export"
        )
        if isinstance(exported, list):
            for item in exported:
                if not isinstance(item, dict):
                    continue
                for annotation in item.get("annotations") or []:
                    if isinstance(annotation, dict):
                        bulk_by_task.setdefault(item.get("id"), []).append(annotation)

        annotations: list[dict[str, Any]] = []
        mismatches: list[dict[str, Any]] = []
        for task in tasks:
            task_id = task.get("id")
            expected = int(task.get("total_annotations") or 0)
            got = list(bulk_by_task.get(task_id, []))
            # ⚠️ 任务对象里**可能**内联着 annotations（LS 1.23 实测恒为空，
            # 但别的版本/别的端点不保证）⇒ 一并收进来，按 id 去重。
            nested = task.get("annotations")
            if isinstance(nested, list):
                got.extend(item for item in nested if isinstance(item, dict))
            if expected > 0 or got:
                # ⚠️ 有标注 ⇒ 去逐条端点取一份（**身份字段只在那儿**）。
                try:
                    single = self.list_task_annotations(task_id)
                except LabelStudioError as exc:
                    single = []
                    mismatches.append(
                        {"task": task_id, "reason": "逐条端点失败", "detail": str(exc)}
                    )
                merged = _merge_annotations(got, single)
                if expected and len(merged) != expected:
                    mismatches.append(
                        {
                            "task": task_id,
                            "reason": "条数对不上",
                            "expected": expected,
                            "got": len(merged),
                        }
                    )
                got = merged
            for annotation in got:
                item = dict(annotation)
                # ⚠️ 标注**没有** `task` 字段时，**补上它所属的 task id**——
                # 否则下游按 `annotation["task"]` 配对会**一条都配不上**，
                # 而那读起来跟「一条都没复核」**一模一样**（静默的错）。
                if not item.get("task"):
                    item["task"] = task_id
                annotations.append(item)
        return {
            "tasks": tasks,
            "annotations": annotations,
            "mismatches": mismatches,
        }


def _is_empty(value: Any) -> bool:
    """「这个字段等于没给」——``None`` / 空串 / 空 list / 空 dict。

    ⚠️ **不是 falsy 判定**：``0`` 与 ``False`` 是**有值**（LS 的
    ``completed_by=0`` 少见，但 ``result`` 里的空 list 得跟「没这个键」
    分开——混起来会把「有但为空」误判成「没有」，于是**静默拿别处的值
    填进去**。
    """
    return value is None or value == "" or value == [] or value == {}


def _merge_annotation_fields(
    base: dict[str, Any], extra: dict[str, Any]
) -> dict[str, Any]:
    """两条**同一条标注**的字段合并（``extra`` 补 ``base`` 的空缺）。

    ⚠️ **只填空缺，不覆盖已有值**——但有一个例外：``result`` 取**更长**的
    那份（批量端点偶尔会给裁过的 result，逐条端点给全的）。

    ⚠️ 为什么需要它：**同一个 annotation 在两个端点上的字段集不一样**
    （见 :meth:`LabelStudioClient.export_project_json` 的表）⇒ 只读一个
    端点就会**少一整个字段**（身份字段），而少的那份读起来像「没复核人」。
    """
    merged = dict(base)
    for key, value in extra.items():
        if _is_empty(value):
            continue
        current = merged.get(key)
        if _is_empty(current):
            merged[key] = value
        elif (
            key == "result"
            and isinstance(current, list)
            and isinstance(value, list)
            and len(value) > len(current)
        ):
            merged[key] = value
    return merged


def _merge_annotations(
    bulk: list[dict[str, Any]], single: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """按 annotation ``id`` 合并两份标注列表（``single`` 补 ``bulk``）。

    ⚠️ **没有 ``id`` 的标注不合并**：宁当两条，也不当一条——把两条不同的
    标注并成一条，比多报一条更坏（少报**看不出**来）。
    """
    order: list[Any] = []
    merged: dict[Any, dict[str, Any]] = {}
    for item in list(bulk) + list(single):
        if not isinstance(item, dict):
            continue
        ident = item.get("id")
        key: Any = ident if ident is not None else ("__no_id__", len(order))
        if key not in merged:
            merged[key] = dict(item)
            order.append(key)
        else:
            merged[key] = _merge_annotation_fields(merged[key], item)
    return [merged[key] for key in order]


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
    的答案。早先写成 ``... or sent`` 会把这个诚实的 0 覆盖成 "发了几条
    就是几条", 于是报告显示推成功、LS 里一条都没有 —— **假成功比假失败
    更难查**。
    """
    counted = _task_count_from(payload)
    return sent if counted is None else counted


def build_client(
    settings: LSSettings, *, require_credentials: bool = True
) -> LabelStudioClient:
    """从 settings 建 client; 无凭据时按需报错。"""
    api_key = settings.resolve_api_key()
    if not api_key:
        if require_credentials:
            raise LabelStudioAuthFailed(
                "未找到 Label Studio 凭据: 请设置环境变量 TRAJECTORY_LS_API_KEY"
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
