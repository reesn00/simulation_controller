"""label_studio.project_manager: LS 项目的创建 / 复用 / 校验 / 清空.

对应 CLI 的 ``init-project`` 与 ``purge``（方案 §8）。全部操作**幂等**：
重复跑 ``init-project`` 不会建出第二个项目。

去重口径：优先用 ``config.yaml`` 里显式的 ``project_id``；为空时按
``project_title`` 在 LS 列表里找**标题精确匹配**的那一个。刻意不做模糊匹配
—— 标题相近的两个项目里推错一个，比推失败更糟。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from label_studio.client import LabelStudioClient, read_label_config
from label_studio.errors import LabelStudioError, LabelStudioProjectError
from label_studio.settings import LabelStudioSettings

log = logging.getLogger(__name__)


def list_existing_projects(client: LabelStudioClient) -> list[dict[str, Any]]:
    return client.list_projects()


def find_project_by_title(
    client: LabelStudioClient, title: str
) -> dict[str, Any] | None:
    """标题**精确**匹配。找不到返回 None（不抛 —— 调用方决定建还是报）。"""
    for project in client.list_projects():
        if str(project.get("title", "")).strip() == title.strip():
            return project
    return None


def validate_label_config(
    client: LabelStudioClient, project_id: int, label_config: str
) -> list[dict[str, Any]]:
    """校验 label_config；返回错误列表，空列表 = 通过。

    校验**失败即抛** :class:`LabelStudioProjectError` —— 带着 LS 返回的逐条
    错误消息抛，否则 CLI 只会打印一个"退出码 1"，人没法修。
    """
    errors = client.validate_label_config(project_id, label_config)
    if errors:
        detail = "; ".join(str(e.get("detail") or e) for e in errors[:5])
        raise LabelStudioProjectError(
            f"project {project_id} 的 label_config 校验未通过 "
            f"({len(errors)} 处): {detail}"
        )
    return errors


def get_or_create_project(
    client: LabelStudioClient, settings: LabelStudioSettings
) -> tuple[dict[str, Any], bool]:
    """复用或创建项目。返回 ``(project, created)``。"""
    if settings.project_id:
        project = client.get_project(settings.project_id)
        log.info("get_or_create_project: 复用配置中的 project %s", settings.project_id)
        return project, False

    existing = find_project_by_title(client, settings.project_title)
    if existing is not None:
        log.info("get_or_create_project: 复用同名项目 id=%s", existing.get("id"))
        return existing, False

    label_config = read_label_config(settings.label_config_file())
    project = client.create_project(
        title=settings.project_title, label_config=label_config
    )
    log.info("get_or_create_project: 新建项目 id=%s title=%s", project.get("id"), settings.project_title)
    return project, True


def init_project(
    client: LabelStudioClient, settings: LabelStudioSettings
) -> dict[str, Any]:
    """``python -m label_studio init-project`` 的实现。幂等。"""
    label_config = read_label_config(settings.label_config_file())
    project, created = get_or_create_project(client, settings)
    project_id = int(project["id"])
    validate_label_config(client, project_id, label_config)
    return {
        "project_id": project_id,
        "title": project.get("title", settings.project_title),
        "created": created,
        "label_config_path": str(settings.label_config_file()),
        "label_config_valid": True,
    }


def resolve_project_id(
    client: LabelStudioClient, settings: LabelStudioSettings
) -> int:
    """推送前的项目定位：显式 id 优先，否则按标题查（**不新建**）。"""
    if settings.project_id:
        return int(settings.project_id)
    existing = find_project_by_title(client, settings.project_title)
    if existing is None:
        raise LabelStudioProjectError(
            f"未找到 LS 项目 {settings.project_title!r}; "
            f"先跑 `python -m label_studio init-project` 或在 config 里设 project_id"
        )
    return int(existing["id"])


def purge_tasks(
    client: LabelStudioClient, project_id: int, *, confirm: bool = False
) -> dict[str, Any]:
    """``python -m label_studio purge`` —— 清空项目内**全部** task。

    这是**不可逆**操作（LS 的 ``DELETE /api/projects/{id}`` 会连同标注一起
    删），所以默认 ``confirm=False`` 时只做 dry-run 报告。
    """
    project = client.get_project(project_id)
    title = project.get("title", "")
    if not confirm:
        return {
            "project_id": project_id,
            "title": title,
            "purged": False,
            "reason": "未加 --confirm, 仅报告不执行",
        }
    client.delete_project(project_id)
    log.warning("purge_tasks: 已删除项目 %s (%s) 及其全部标注", project_id, title)
    return {
        "project_id": project_id,
        "title": title,
        "purged": True,
        "note": "项目已删除; 重跑 init-project 可重建（标注一并丢失）",
    }


def describe_status(
    client: LabelStudioClient, settings: LabelStudioSettings, *, health: dict[str, Any]
) -> dict[str, Any]:
    """``status`` 子命令的输出。不创建 Run 日志，不打印任何凭据内容。"""
    config_path = settings.label_config_file()
    entry: dict[str, Any] = {
        "base_url": settings.base_url,
        "health": health,
        "credentials_present": settings.has_credentials(),
        "api_key_source": "api_key_path" if settings.api_key_path else "api_key",
        "label_config_path": str(config_path),
        "label_config_exists": Path(config_path).is_file(),
        "upload_enabled": settings.upload.enabled,
        "hook_enabled": settings.hook.enabled,
        "credential_scan_enabled": settings.credential_scan.enabled,
    }
    if settings.has_credentials():
        try:
            project = (
                client.get_project(settings.project_id)
                if settings.project_id
                else find_project_by_title(client, settings.project_title)
            )
            entry["project"] = (
                {"id": project.get("id"), "title": project.get("title")}
                if project
                else None
            )
        except LabelStudioError as exc:
            entry["project"] = None
            entry["project_error"] = str(exc)
    return entry


__all__ = [
    "describe_status",
    "find_project_by_title",
    "get_or_create_project",
    "init_project",
    "list_existing_projects",
    "purge_tasks",
    "resolve_project_id",
    "validate_label_config",
]
