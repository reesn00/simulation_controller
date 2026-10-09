"""orchestration.ls_hook: task_pipeline step 11 的 Label Studio 自动推送.

三条硬约束（方案 §9 / §16）:

1. **绝不阻塞主流程**。LS 挂了、慢了、认证失败了, task 仍然是 ``done`` ——
   推送是**旁路**, 不是流水线的一环。失败只记日志 + 指标。
2. **独立超时**。挂死不能拖住 orchestration。用 ``ThreadPoolExecutor`` +
   ``future.result(timeout=)``; 超时后线程继续跑完就被回收（daemon 线程不会
   阻止进程退出), 但**不再等它**。注意 ``with ThreadPoolExecutor(...)`` 的
   退出时会 join, 所以这里**不用 with**, 显式 ``shutdown(wait=False)``。

   超时 / 进程被杀**不等于丢样本**: ``push_single_c3`` 是先拿到 LS task id
   再写台账, 所以「台账里没有」严格等价于「LS 上没建成」。批次后跑一次
   ``python -m label_studio upload`` 全量补推, 缺的会补上 —— 因为 LS 1.23
   不去重, 台账就是唯一防线, 这个补推是幂等的。
3. **默认关闭**。只看 ``hook.enabled``, 不看 ``upload.enabled`` —— 两个开关
   交叉会导致 hook 静默空转（配了 upload.enabled 就以为 hook 也开了）。

凭据：本模块**不读**凭据, 只把 settings 透传给 :mod:`label_studio`; 异常消息
由那里的 :func:`label_studio.errors.redact` 统一脱敏。
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

#: 进程级指标。刻意放模块级而不是 settings —— 指标是**观测**, 不该影响
#: 配置语义。
METRICS: dict[str, int] = {
    "ls_hook_attempted": 0,
    "ls_hook_succeeded": 0,
    "ls_hook_failed": 0,
    "ls_hook_timed_out": 0,
    "ls_hook_skipped": 0,
}

#: 默认 hook 超时（秒）。**不是 5** —— 一次推送要串完: PAT refresh + 建连 +
#: 查项目 + PATCH label_config + create task + 预标注 POST, 首次跑还要多
#: 付一次 TLS/进程冷启动。5s 下首次必超, 超时即丢样本（台账来不及写,
#: 下次 upload 又补推一遍）。留足余量比"快速失败"划算: 推送本来就是旁路,
#: 多等 25s 不占任何主流程时间。
DEFAULT_HOOK_TIMEOUT_SECONDS = 30.0

#: 进程级 project_id 缓存, key = ``(base_url, project_title)``。
#: 每个 task 跑完都调一次 ``resolve_project_id`` = 每 task 一次「查项目 +
#: PATCH label_config」, 98 个 task 就是 98 次 PATCH。PATCH 会**覆盖 LS 上
#: 人工调整过的配置**, 且批次跑一半时把标注员的修改冲掉 —— 这不是性能问题
#: 是正确性问题。同批次内 label_config 文件不会变, 第一次解析后直接复用。
_PROJECT_CACHE: dict[tuple[str, str], int] = {}


def reset_metrics() -> None:
    for key in METRICS:
        METRICS[key] = 0
    _PROJECT_CACHE.clear()


def load_hook_settings(config_path: Path | str | None = None) -> Any:
    """读根配置的 ``label_studio.hook`` 段。**永不抛**。

    配置文件缺失 / 解析失败 / 字段非法 → 返回 ``None``（等价于未启用）。
    配置问题不该让整条流水线起不来。
    """
    try:
        from label_studio.config_loader import load_label_studio_config
        from shared_config import find_root_config

        if config_path is None:
            config_path = find_root_config()
        if config_path is None:
            return None
        settings = load_label_studio_config(Path(config_path))
        hook = settings.hook
        if not hook.enabled:
            return None
        return hook
    except Exception as exc:  # pragma: no cover - 配置异常路径
        _log.warning("ls_hook: 配置加载失败, hook 视为未启用: %s", exc)
        return None


def _push(settings: Any, project_id: int, meta_path: Path) -> dict[str, Any]:
    """真正干活的函数（跑在线程里）。延迟 import 保持 orchestration 不硬依赖 LS。"""
    from label_studio.client import build_client
    from label_studio.task_exporter import push_single_c3

    client = build_client(settings)
    return push_single_c3(
        meta_path=meta_path,
        settings=settings,
        project_id=project_id,
        client_factory=lambda: client,
    )


def _default_timeout(settings: Any) -> float:
    """取 hook 超时。兼容两种传参形态。

    ``run_hook`` 传的是 :class:`LabelStudioSettings`（超时在其 ``.hook`` 子对象上）,
    直接调用 ``push_with_timeout`` 的调用方可能只拿到 ``HookSettings``。两处都
    支持, 免得调用方漏传 timeout 时静默落到 5s 默认值。
    """
    for holder in (settings, getattr(settings, "hook", None)):
        value = getattr(holder, "hook_timeout_seconds", None)
        if value:
            return float(value)
    return DEFAULT_HOOK_TIMEOUT_SECONDS


def push_with_timeout(
    settings: Any,
    *,
    project_id: int,
    meta_path: Path,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """把一条 C3 推给 LS, 带独立超时与全异常兜底。**永不抛异常。**

    Returns:
        ``{"ok": bool, "skipped": bool, "reason": str, "error": str|None}``

    ``ok=False`` 时 task 本身不受影响 —— 调用方只记日志。
    """
    if not meta_path or not Path(meta_path).is_file():
        METRICS["ls_hook_skipped"] += 1
        return {"ok": False, "skipped": True, "reason": f"meta 不存在: {meta_path}",
                "error": None}

    METRICS["ls_hook_attempted"] += 1
    timeout = float(
        timeout_seconds
        if timeout_seconds is not None
        else _default_timeout(settings)
    )

    # 显式 shutdown(wait=False), 不用 with —— with 退出时会 join, 超时就白设了
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ls-hook")
    try:
        future = executor.submit(_push, settings, project_id, Path(meta_path))
    except Exception as exc:  # pragma: no cover - submit 本身极少失败
        executor.shutdown(wait=False)
        METRICS["ls_hook_failed"] += 1
        return {"ok": False, "skipped": False, "reason": "submit_failed", "error": str(exc)}

    try:
        result = future.result(timeout=timeout)
    except FutureTimeout as exc:
        # `concurrent.futures.TimeoutError` 在 3.11+ 就是内置 `TimeoutError`
        # 的别名 —— LS 侧**自己**抛的 TimeoutError 会落到这个分支, 被误记成
        # "我们等超时了", 指标从此不可信。用 future.done() 区分二者。
        if future.done():
            METRICS["ls_hook_failed"] += 1
            _log.error("ls_hook: 推送失败 (meta=%s): %s", meta_path, exc)
            return {"ok": False, "skipped": False, "reason": type(exc).__name__,
                    "error": str(exc)}
        METRICS["ls_hook_timed_out"] += 1
        _log.error(
            "ls_hook: 推送超时 (>%.1fs), 已放弃等待; task 本身不受影响 (meta=%s)",
            timeout, meta_path,
        )
        return {"ok": False, "skipped": False, "reason": "timeout", "error": None}
    except Exception as exc:
        METRICS["ls_hook_failed"] += 1
        # 凭据泄漏风险: LS 异常的 str() 已由 label_studio.errors.redact 处理,
        # 这里不再自行拼接 settings 里的任何字段。
        _log.error("ls_hook: 推送失败 (meta=%s): %s", meta_path, exc)
        return {"ok": False, "skipped": False, "reason": type(exc).__name__,
                "error": str(exc)}
    finally:
        executor.shutdown(wait=False)

    METRICS["ls_hook_succeeded"] += 1
    _log.info("ls_hook: 已推送 task=%s session=%s", result.get("task_id"),
              result.get("session_id"))
    return {"ok": True, "skipped": False, "reason": None, "error": None,
            "result": result}


def run_hook(
    settings: Any,
    *,
    project_id: int | None,
    meta_path: Path | None,
    resolve_project_id: Any = None,
) -> dict[str, Any]:
    """step 11 的完整入口: 判定开关 → 解析 project → 带超时推送。

    Args:
        settings: :class:`label_studio.settings.LabelStudioSettings`（非 HookSettings,
            ``push_single_c3`` 要用其中的凭据 / 扫描 / 评分卡配置）。
        project_id: 已知的项目 id; None 时用 ``resolve_project_id`` 回调解析。
        resolve_project_id: 无 id 时的解析回调（orchestration 传
            :func:`label_studio.project_manager.resolve_project_id` + client）。
    """
    hook = getattr(settings, "hook", None)
    if settings is None or hook is None or not getattr(hook, "enabled", False):
        return {"ok": False, "skipped": True, "reason": "hook_disabled", "error": None}

    try:
        if project_id is None:
            # 进程级缓存: 同批次只解析一次。resolve_project_id(sync=True) 会
            # PATCH label_config 进 LS, 每 task 调一次 = 每 task 覆盖一次
            # 标注员在 LS 上做的调整。
            cache_key = (
                str(getattr(settings, "base_url", "")),
                str(getattr(settings, "project_title", "")),
            )
            cached = _PROJECT_CACHE.get(cache_key)
            if cached is not None:
                project_id = cached
            else:
                if resolve_project_id is None:
                    return {"ok": False, "skipped": True, "reason": "no_project_id",
                            "error": None}
                project_id = resolve_project_id(settings)
                _PROJECT_CACHE[cache_key] = int(project_id)
    except Exception as exc:
        METRICS["ls_hook_failed"] += 1
        _log.error("ls_hook: 项目解析失败, 跳过推送: %s", exc)
        return {"ok": False, "skipped": True, "reason": "project_resolve_failed",
                "error": str(exc)}

    return push_with_timeout(
        settings,
        project_id=int(project_id),
        meta_path=Path(meta_path) if meta_path else Path(""),
        timeout_seconds=getattr(hook, "hook_timeout_seconds",
                                DEFAULT_HOOK_TIMEOUT_SECONDS),
    )


__all__ = [
    "METRICS",
    "load_hook_settings",
    "push_with_timeout",
    "reset_metrics",
    "run_hook",
]
