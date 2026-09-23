# Langfuse 观测性集成方案

> 状态:设计中(2026-09-23);尚未开工。
> 目的:把 simulate_serve / gdr / etl 三阶段的轨迹数据处理过程上传到 Langfuse,用于**对比观测**原始 agent 轨迹、gdr 每步骤前后变化、etl 处理前后变化。

---

## 1. 设计目标

| 观测目标 | Langfuse 体现 |
|---|---|
| 对比**原始 C1 trajectory** vs **gdr 每步精修后** vs **etl 4 视图** | 同一 `session_id` 串联 3 个独立 trace,每个步骤一个 span,`input` / `output` 传完整 payload |
| 看到**每一步前/后** session 长什么样 | 步骤进入时深拷贝 → 上传为 `input`;步骤结束时 → 上传为 `output` |
| 排查**某条 session** 退化原因 | Langfuse UI 按 `session_id` 过滤,展开步骤树,直接 diff input/output |

---

## 2. 设计原则

| 原则 | 决定 |
|---|---|
| 跨阶段串联 | 三阶段使用**同一个 Langfuse `session_id`**(`run.remote_session_id`,即 `useramulation-xxx`),由该字段在 Langfuse 端按 session 聚合 |
| Trace 形态 | 三阶段各产生**独立 trace**,由同一 `session_id` 串联;不强制父子跨进程 |
| 客户端实例 | 每进程一份;worker 子进程 fork 后需重新 `Langfuse()`(SDK 不 fork-safe),统一通过 `get_client()` 工厂 |
| 默认关闭 | `langfuse.enabled: false`,根 `config/config.yaml` 切换,旧批次 / 旧任务零侵入 |
| Payload 完整 | `upload_payload: full`(默认)— `input` / `output` 传**完整 session dict / C1 trajectory / C3 4 视图**;提供 `summary` / `none` 兜底 |
| 失败回退 | Langfuse 异常 → `logger.warning` + 继续原业务,**不可让 trace 影响远端执行** |
| 异常传播 | 业务异常穿透 `stage_trace` / `step_span` 时,span 标 `level="ERROR"` + `status_message="<ExceptionType>: <msg>"`,**异常本身不吞** — 外层 try/except 是否吞由调用方决定 |
| 隐私约束解耦 | CLAUDE.md "不保存自由文本思维链…" 是**落盘产物**(`output/`)的约束;Langfuse 后端是独立观测副本,**完整上传**,不入训练集 |

> **关键边界**:Langfuse 上传内容与项目 `output/` 落盘内容的隐私策略**解耦**。前者是观测副本,后者是 SFT 训练数据;后者按 CLAUDE.md 既有规则继续脱敏(不存 thinking / Cookie / Auth / 浏览器 profile),前者按本方案完整上传。

---

## 3. 统一配置 Schema(根 `config/config.yaml`)

新增顶层段,所有模块共享:

```yaml
langfuse:
  enabled: false                      # 全局开关(默认 false,显式开)
  public_key: "${LANGFUSE_PUBLIC_KEY}"
  secret_key: "${LANGFUSE_SECRET_KEY}"
  base_url: "https://cloud.langfuse.com"
  environment: "dev"                  # 生产改 "prod"
  release: "${LANGFUSE_RELEASE:-local}"
  sample_rate: 1.0
  flush_at: 512
  flush_interval: 5.0
  timeout: 10

  # 三种 payload 模式
  upload_payload: full                # full | summary | none
  # full   : span.input/output = 完整 session dict / C1 / C3(本方案默认,用于轨迹对比)
  # summary: span.input/output = {size, blocks_count, tool_calls, ...}
  # none   : 不传 input/output,只传 metadata + tags(节省带宽)

  # 大体积兜底,超过则降级为 summary(可选,默认不开)
  max_payload_bytes: 0                # 0 = 不限制;>0 触发降级
  max_block_payload_bytes: 0          # 单 span 块级 payload 上限

  # 阶段级开关(便于渐进裁剪)
  stages:
    simulate_serve:
      enabled: true
      export_trajectory_on_terminal: true
    gdr:
      enabled: true
      per_step_span: true             # 21 个子步骤每步一个 span
      per_llm_span: false             # LlamaCppClient.chat 级(可选)
    etl:
      enabled: true
      per_step_span: true
```

`${VAR}` 占位符与现有 `llm:` 段一致;`shared_config.py` 已有 YAML + 环境变量加载,复用即可。

---

## 4. 客户端工厂 `simulate_serve/observability/langfuse_client.py`(新建)

**状态**:已落地(PR 1,2026-09-23)。本节为落地后真实代码摘录,签名冻结,**所有 PR 2/3/4 子 agent 必须按此签名调用**。

三模块各持一份客户端实例,工厂返回同一 client(per-process 单例):

```python
# simulate_serve/observability/langfuse_client.py — 真实落地版本(PR 1)
from __future__ import annotations

import copy
import json
import logging
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

try:
    from langfuse import Langfuse, propagate_attributes
except Exception:  # pragma: no cover - SDK not installed
    Langfuse = None
    propagate_attributes = None


# ---------------------------------------------------------------------------
# Process-local singleton + fork-safe reset
# ---------------------------------------------------------------------------

_client: Any | None = None
_lock = threading.Lock()


def _reset_for_fork() -> None:
    """重置 per-process singleton,worker 子进程 fork 后调用."""
    global _client
    with _lock:
        _client = None


def _extract_langfuse_fields(cfg: Any) -> dict[str, Any]:
    """鸭子类型:接受嵌套 pydantic / 扁平 pydantic / frozen dataclass.

    字段白名单(frozen):enabled / public_key / secret_key / base_url /
    environment / release / sample_rate / flush_at / flush_interval /
    timeout / upload_payload / max_payload_bytes / max_block_payload_bytes
    """
    keys = [
        "enabled", "public_key", "secret_key", "base_url",
        "environment", "release", "sample_rate", "flush_at",
        "flush_interval", "timeout", "upload_payload", "max_payload_bytes",
        "max_block_payload_bytes",
    ]
    defaults = {
        "enabled": False, "public_key": "", "secret_key": "",
        "base_url": "https://cloud.langfuse.com", "environment": "dev",
        "release": "local", "sample_rate": 1.0, "flush_at": 512,
        "flush_interval": 5.0, "timeout": 10, "upload_payload": "full",
        "max_payload_bytes": 0, "max_block_payload_bytes": 0,
    }
    out = {}
    for k in keys:
        v = getattr(cfg, k, None)
        if v is None or v == "":
            v = getattr(cfg, f"langfuse_{k}", None)
        if v is None or v == "" or v is False:
            v = defaults[k]
        if k in ("sample_rate", "flush_interval", "timeout") and not isinstance(v, (int, float)):
            v = defaults[k]
        if k in ("flush_at", "max_payload_bytes", "max_block_payload_bytes") and not isinstance(v, int):
            v = defaults[k]
        out[k] = v
    return out


def get_client(cfg: Any) -> Any | None:
    """Per-process singleton. 关闭 / 缺失凭据 / SDK 未装 → 返 None,业务无感."""
    global _client
    if cfg is None:
        return None
    fields = _extract_langfuse_fields(cfg)
    if not fields["enabled"] or not fields["public_key"] or not fields["secret_key"]:
        return None
    if Langfuse is None:
        return None
    with _lock:
        if _client is not None:
            return _client
        try:
            _client = Langfuse(
                public_key=fields["public_key"],
                secret_key=fields["secret_key"],
                host=fields["base_url"],
                environment=fields["environment"] or None,
                release=fields["release"] or None,
                sample_rate=fields["sample_rate"],
                flush_at=fields["flush_at"],
                flush_interval=fields["flush_interval"],
                timeout=fields["timeout"],
            )
        except Exception as exc:
            logger.warning("Langfuse init failed; observability disabled: %s", exc)
            _client = None
            return None
        return _client


def shutdown() -> None:
    """Flush + reset the process-local Langfuse client."""
    global _client
    with _lock:
        if _client is None:
            return
        try:
            _client.flush()
        except Exception:
            pass
        try:
            _client.shutdown()
        except Exception:
            pass
        _client = None


# ---------------------------------------------------------------------------
# Payload helpers (deep copy, JSON-safe, size truncation)
# ---------------------------------------------------------------------------

def snapshot(obj: Any) -> Any:
    """深拷贝,pydantic / dataclass / dict 都安全;失败回退 _to_jsonable."""
    try:
        return copy.deepcopy(obj)
    except Exception:
        try:
            return _to_jsonable(obj)
        except Exception:
            return {"_unrepr": str(obj)[:1000]}


def _to_jsonable(obj: Any) -> Any:
    """递归 → JSON 可序列化;pydantic v2 model_dump → dict / list / str 兜底."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(mode="json", exclude_none=True)
        except Exception:
            pass
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_jsonable(v) for v in obj]
    if hasattr(obj, "__dict__") and vars(obj):       # 注意:空 __dict__ 不吞掉,fall-through 到 str
        try:
            return {k: _to_jsonable(v) for k, v in vars(obj).items() if not k.startswith("_")}
        except Exception:
            pass
    return str(obj)


def _maybe_truncate(obj: Any, max_bytes: int) -> Any:
    """超 max_bytes 降级为 {"_truncated": True, ...} 占位;max_bytes=0 不限制."""
    if not max_bytes or obj is None:
        return _to_jsonable(obj) if obj is not None else obj
    try:
        s = json.dumps(_to_jsonable(obj), ensure_ascii=False)
        size = len(s.encode("utf-8"))
        if size <= max_bytes:
            return _to_jsonable(obj)
        keys = list(obj.keys()) if isinstance(obj, dict) else None
        return {"_truncated": True, "size": size, "max": max_bytes, "keys": keys}
    except Exception:
        return _to_jsonable(obj)


# ---------------------------------------------------------------------------
# Span contexts (outer trace + child step) — ERROR 行为 + payload_mode
# ---------------------------------------------------------------------------

@contextmanager
def stage_trace(
    client: Any | None,
    *,
    session_id: str,
    name: str,
    user_id: str | None = None,
    task_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    input_data: Any = None,
    output_capture: Callable[[], Any] | None = None,
    payload_mode: str = "full",
    max_payload_bytes: int = 0,
) -> Iterator[Any]:
    """Outer trace span.

    - client=None:yield None,no-op
    - 业务异常穿透:except 分支标 ``level="ERROR"`` + ``status_message``,**异常重新抛出**
      (不吞);调用方决定是否外层 try/except
    - payload_mode:full / summary / none;full + max_payload_bytes>0 触发 _maybe_truncate
    """
    if client is None:
        yield None
        return
    md = dict(metadata or {})
    if task_id and "task_id" not in md:
        md["task_id"] = task_id
    safe_user_id = user_id if user_id is not None else session_id

    def _input():
        return _resolve_payload(input_data, payload_mode, max_payload_bytes)
    def _output():
        if output_capture is None:
            return None
        try:
            return _resolve_payload(output_capture(), payload_mode, max_payload_bytes)
        except Exception as exc:
            return {"_output_capture_error": f"{type(exc).__name__}: {exc}"}

    span_cm = _open_observation(
        client, as_type=None, name=name, session_id=session_id,
        user_id=safe_user_id, tags=tags, metadata=md,
        input_fn=_input, output_fn=_output,
    )
    try:
        with span_cm as span:
            yield span
    except Exception as exc:
        try:
            if span is not None:
                span.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        raise


@contextmanager
def step_span(
    client: Any | None,
    *,
    name: str,
    payload_mode: str = "full",
    input_data: Any = None,
    output_capture: Callable[[], Any] | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
    max_payload_bytes: int = 0,
    metadata: dict[str, Any] | None = None,
    as_type: str | None = None,
) -> Iterator[Any]:
    """子步骤 span. 异常穿透 + level=ERROR;``as_type="generation"`` 标记 LLM 调用."""
    if client is None:
        yield None
        return
    md = dict(metadata or {})
    if task_id and "task_id" not in md:
        md["task_id"] = task_id
    def _input():
        return _resolve_payload(input_data, payload_mode, max_payload_bytes)
    def _output():
        if output_capture is None:
            return None
        try:
            return _resolve_payload(output_capture(), payload_mode, max_payload_bytes)
        except Exception as exc:
            return {"_output_capture_error": f"{type(exc).__name__}: {exc}"}
    span_cm = _open_observation(
        client, as_type=as_type, name=name, session_id=session_id,
        user_id=session_id, tags=None, metadata=md,
        input_fn=_input, output_fn=_output,
    )
    try:
        with span_cm as span:
            yield span
    except Exception as exc:
        try:
            if span is not None:
                span.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        raise


@contextmanager
def _open_observation(
    client, *, as_type, name, session_id, user_id, tags,
    metadata, input_fn, output_fn,
):
    """内部:propagate_attributes + start_as_current_observation 装配."""
    pa_ctx = None
    if propagate_attributes is not None:
        pa_kwargs = {}
        if session_id:
            pa_kwargs["session_id"] = session_id
        if user_id:
            pa_kwargs["user_id"] = user_id
        if tags:
            pa_kwargs["tags"] = list(tags)
        if pa_kwargs:
            pa_ctx = propagate_attributes(**pa_kwargs)   # **不传 trace_name;trace_name 由 start_as_current_observation(name=name) 给出**

    span = None
    try:
        if pa_ctx is not None:
            pa_ctx.__enter__()
        kwargs = {"name": name}
        if as_type:
            kwargs["as_type"] = as_type
        cm = client.start_as_current_observation(**kwargs)
        span = cm.__enter__()
        try:
            if metadata:
                span.update(metadata=metadata)
            try:
                span.update(input=input_fn())
            except Exception:
                pass
        except Exception:
            pass
        yield span
        try:
            out = output_fn()
            if out is not None:
                span.update(output=out)
        except Exception:
            pass
    finally:
        if span is not None:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass
        if pa_ctx is not None:
            try:
                pa_ctx.__exit__(None, None, None)
            except Exception:
                pass


def _resolve_payload(data: Any, mode: str, max_bytes: int) -> Any:
    """payload_mode 解析:none → None;summary → {"summary": True, size_hint};
    full → _maybe_truncate(data, max_bytes) 或 _to_jsonable(data)。"""
    if mode == "none":
        return None
    if mode == "summary":
        try:
            s = json.dumps(_to_jsonable(data), ensure_ascii=False)
            return {"summary": True, "size_hint": len(s.encode("utf-8"))}
        except Exception:
            return {"summary": True, "size_hint": 0}
    if max_bytes:
        return _maybe_truncate(data, max_bytes)
    return _to_jsonable(data)
```

**关键点(已落地,冻结)**:

- **进程级 `_client` + `_lock`**;子进程 fork 后调用 `_reset_for_fork()`(本模块导出供 worker_init 用)
- **所有 helper fail-safe**;Langfuse 不可用时业务照常(client=None → yield None)
- **`snapshot()`** 集中处理深拷贝成本
- **`_to_jsonable`** 处理 pydantic v2 (`model_dump(mode="json", exclude_none=True)`) / dataclass / dict
- **`_maybe_truncate`** 是大体积兜底;`max_bytes=0` 不限制
- **`payload_mode`** 三态:`full` / `summary` / `none`,`summary` 模式只传 `{summary, size_hint}` 不传完整 payload
- **异常穿透 + level=ERROR**:业务异常穿透 span 时,span 标 `level="ERROR"` + `status_message="<Type>: <msg>"`,异常**重新抛出**(不吞);调用方用 try/except 决定是否吞
- **`propagate_attributes`** 不传 `trace_name`(v3 SDK 不接受);`trace_name` 由 `start_as_current_observation(name=name)` 给出

> **PR 5 补丁 (2026-09-23)**: 工厂 `_open_observation` 内 `start_as_current_observation(...)` 与 `cm.__enter__()` 也包 try/except (PR 4 报告建议 #2). SDK init 异常不再 bubble 到业务路径,与 `_resolve_payload` / `span.update` / `client.flush` 的 fail-safe 对称。异常以 `logger.warning("langfuse start_observation failed: %s", exc)` 记录。`_resolve_payload` / `snapshot` / `_to_jsonable` / `_maybe_truncate` / `stage_trace` / `step_span` 签名与白名单仍冻结。

---

## 5. 三阶段接入点

### 5.1 simulate_serve — trajectory 文件导 Langfuse

| 文件 | 行 | 改动 |
|---|---|---|
| [simulate_serve/config.py](../simulate_serve/config.py) | 紧随 `AppConfig` | 新增 `LangfuseConfig` pydantic 类,作为 `AppConfig.langfuse` 字段 |
| [simulate_serve/bootstrap.py](../simulate_serve/bootstrap.py) | `build_application` 内,约 157-168 | `self.langfuse = get_client(config.langfuse)` |
| [simulate_serve/bootstrap.py](../simulate_serve/bootstrap.py) | `ApplicationServices.close()` 82-95 | 末尾追加 `shutdown()` |
| [simulate_serve/infrastructure/trajectory_archiver.py](../simulate_serve/infrastructure/trajectory_archiver.py) | `_copy_with_retry` 终态事件分支(line 158)与 budget-exceeded 分支(line 171) | 包一个 `stage_trace` 调用 `_emit_trail` |
| [simulate_serve/application/run_task.py](../simulate_serve/application/run_task.py) | `_archive_trajectory` 353-364 | 把 `run` 传给 archiver,供 `_emit_trail` 取 `remote_session_id` / `task_id` |

新增 `_emit_trail` 在 `QwenPawTrajectoryArchiver` 内:

```python
def _emit_trail(self, source: Path, last_event_type: str | None,
                terminal_reached: bool) -> None:
    client = self.langfuse
    run = self._run_ctx  # {run_id, task_id, remote_session_id, remote_agent_id, ...}
    sid = run.get("remote_session_id") or run.get("run_id")
    payload_mode = self.langfuse_config.upload_payload

    def _output_payload():
        # 完整 C1 trajectory → dict(JSONL → list of events)
        if not source.exists():
            return {"_missing": True}
        try:
            return [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        except Exception as exc:
            return {"_read_error": str(exc)}

    with stage_trace(
        client,
        session_id=sid,
        name=f"simulate_serve:{run.get('task_id')}",
        user_id=sid,
        task_id=run.get("task_id"),
        tags=["stage:simulate_serve"],
        metadata={
            "run_id": run.get("run_id"),
            "task_id": run.get("task_id"),
            "agent_id": run.get("remote_agent_id"),
            "terminal_reached": terminal_reached,
            "last_event_type": last_event_type,
            "trajectory_path": str(source),
        },
        input_data=None,
        output_capture=_output_payload,
        payload_mode=payload_mode,
    ):
        pass
```

**注意**:`_run_ctx` 需要在 `_record_response` 调用 `archive()` 时塞入 archiver(目前 `archive()` 无 run 参数,需小改 `run_task.py:_archive_trajectory` 把 run 传进去)。

### 5.2 gdr — 21 个步骤前后上传

> **行号以 [docs/langfuse-gdr.md §2.2](langfuse-gdr.md) 为准**(PR 1 后 gdr 端二次复核)。本节保留作为设计意图参考。

| 层级 | 文件 | 行 | span 名 |
|---|---|---|---|
| 外层(有 task_id) | [orchestration/workers/gdr_worker.py](../orchestration/workers/gdr_worker.py) | `run_gdr_once` body 71-199 | `gdr:<task_id>` |
| 内层(session 维度) | [gdr/pipeline/runner.py](../gdr/pipeline/runner.py) | `process_one` body 275-526 | `gdr.process_one` |
| 子步骤 | `runner.py` 各步 | 见 gdr mate §2.2 表 | 见该表 |

子步骤表完整 21 项(0..20)见 [docs/langfuse-gdr.md §2.2](langfuse-gdr.md),含:

- 步骤 0..4 硬过滤 / health / folds(零 LLM)
- 步骤 5 retry_loop_clip / 6 retrack_state(LLM)
- 步骤 8 router.tag(LLM fan-out)
- 步骤 10 refine.run_repairs(LLM 内嵌 ThreadPoolExecutor)
- 步骤 13 reassemble(嵌 3 个 generation 子 span)
- 步骤 18 save_refined_session(IO,只 metadata)
- 步骤 16/17/19/20 audit + 旁路队列(deferred / judge_low / routing_abstain / incomplete)

**特殊点**:
- `Router.tag`(363)是 `ThreadPoolExecutor` 投票层,**包外层即可**;内层 `_vote_block` 通过 metadata 记录并发度
- `reassemble` 内含 3 个 LLM(`extract_user_intent_llm` / `_validate_edit_consistency` fan-out / L3 end-to-end),**外层 span + 内嵌 3 个 generation 子 span**
- `_execute_repair_item`(runner.py:215-240)是 refiner + validator 集中分发,直接包这一处即拿到每次 repair 的 span;**每条 repair_item 一个 span**,内嵌 refiner + 三个 validator 子 span
- 可选更细:`LlamaCppClient.chat`(llm_client.py:146)是**所有 LLM 调用的总咽喉**,包这一个就能拿全 token 数 / 模型 / latency(`meta` 已有);span 数会爆炸,**`per_llm_span: false` 默认**

**helper 模板**(放在 `gdr/pipeline/runner.py` 顶部):

```python
from simulate_serve.observability.langfuse_client import (
    get_client, step_span, snapshot,
)

def _gdr_step_span(name: str, session: Session, **metadata):
    """每步骤一个 span,fail-safe;内部负责深拷贝与 payload 模式。"""
    client = get_client(_get_langfuse_cfg())  # 从 cfg 读
    payload_mode = _get_payload_mode()
    before = snapshot(session)
    return step_span(
        client, name=name, payload_mode=payload_mode,
        input_data=before,
        output_capture=lambda: session,  # by-ref,函数退出前 session 已被修改
        session_id=session.session_id,
        task_id=_current_task_id(),  # 从 runner ctx 拿
        **metadata,
    )
```

调用:

```python
with _gdr_step_span("gdr.reassemble", session, stage="reassembly"):
    result = reassemble(session, refine_records, ...)
```

### 5.3 etl — 每步骤前后上传

| 文件 | 行 | span 名 |
|---|---|---|
| [orchestration/workers/etl_worker.py](../orchestration/workers/etl_worker.py) | `run_etl_once` body 68-159 | `etl:<task_id>`(外层) |
| 同上 | 126 `load_refined_session` | `etl.load_refined_session` |
| 同上 | 141 `save_session_v2` | `etl.save_c3_4views`,内嵌 4 文件路径 metadata |

**进程模型关键**:`run_etl_once` 在 `multiprocessing.Pool` worker 中跑,必须 `finally` flush:

```python
# etl_worker.py:run_etl_once 末尾加
finally:
    client = get_client(cfg.langfuse)
    if client is not None:
        try:
            client.flush()
        except Exception:
            pass
```

或者在 `task_pipeline.py:_worker_init`(若有 hook)统一 shutdown。

**粒度选项**:
- 粗粒度(推荐先上):`etl.session` + 2 个子 span(load / save)
- 细粒度(待 `render_to_4_views` 实现 + `qf_*` 接入后):每 `qf_*` 模块加子 span(`usage_prune` / `system_prompt.partition` / `tool_templates.save` / `tool_output_summarize` / `transform.qf_render`)

---

## 6. Trace 命名规范(全局)

| 元素 | Langfuse 字段 | 取值 |
|---|---|---|
| `session_id`(贯穿三阶段) | Langfuse `session_id` | `run.remote_session_id`(`useramulation-xxx`) |
| `user_id` | Langfuse `user_id` | 同上 |
| `trace_name` | Langfuse `trace_name` | `simulate_serve:{task_id}` / `gdr:{task_id}` / `etl:{task_id}` |
| Span `name` | Langfuse observation name | 阶段内子步骤名(见 §5 各表) |
| `metadata.task_id` | metadata | `T001` 等 |
| `metadata.run_id` | metadata | `run_xxx`(simulate_serve 用) |
| `metadata.stage` | metadata | `simulate_serve` / `gdr` / `etl` |
| `metadata.session_id` | metadata | 同 session_id(冗余便于过滤) |
| `tags` | Langfuse tags | `["stage:gdr", "task:T001", "schemav1"]` |
| `input` / `output` | span 字段 | **完整 session dict / C1 / C3 视图**(见 §7) |

---

## 7. Payload 内容(核心对比观测设计)

| 阶段 / 步骤 | `span.input` | `span.output` | 备注 |
|---|---|---|---|
| **simulate_serve.terminal** | (空) | **完整 C1 trajectory dict**(JSONL → list of events) | base 快照 |
| **gdr.outer** | C1 trajectory dict | C2 refined Session dict | 整体演化 |
| **gdr.hard_filter** | 进入时 session dict(深拷贝) | 过滤后 session dict | 看到哪些被剔除 |
| **gdr.light_health** | 同上 | 同上 + `light_health` 字段 | 健康分前后 |
| **gdr.context_understanding.build** | 同上 | 同上 + `cu.summary` | CU 结构前后 |
| **gdr.fold.failed_toolresults** | 同上 | 同上;metadata 记 `removed_block_ids` | 折叠前后 |
| **gdr.fold.repeated_thinking** | 同上 | 同上;metadata 记 `removed_block_ids` | 同上 |
| **gdr.retry_loop_clip** | 同上 | 同上;metadata 记 `clipped_segments` | LLM 裁剪前后 |
| **gdr.cu.retrack_state** | 同上 | 同上 + `cu.global_state` | state 跟踪前后 |
| **gdr.user_intent.heuristic** | 同上 | 同上 + `user_intent_heuristic` | |
| **gdr.router.tag** | 同上 | 同上 + `defects_index` | 路由标注前后 |
| **gdr.policy.decide** | 同上 | 同上 + `policy_decisions` | |
| **gdr.refine.execute_repair_item**(每条) | 单个 block dict | 精修后 block dict + 三 validator 结果 | block 级对比 |
| **gdr.validate.{l1,l2,l3}** | original block | refined block + verdict | |
| **gdr.reassemble** | session | session + `quality_score` | 重组前后 |
| **gdr.save_refined_session** | session | `output_path` | IO |
| **etl.outer** | C2 refined Session dict | C3 4 视图 dict(messages/openai/qwenjina/meta) | 终态对比 |
| **etl.load_refined_session** | C2 文件内容(读回 dict) | Session 对象 | 加载前后 |
| **etl.save_c3_4views** | Session | 4 视图 dict | 写入前后 |

**关键实现细节**:
- 每个 LLM 步骤包一层**深拷贝**(`copy.deepcopy(session)`)以保留"输入前"完整快照
- Langfuse `input` / `output` 字段类型:JSON 完整 dict
- 单条 refiner / validator 因步骤只传**受影响的 block dict**,不传整 session(避免 span 太大)
- `output_capture` 必须用 lambda,延迟到 span 退出时取(因为 session 是 in-place 修改的)

---

## 8. 性能影响

| 项 | 成本 | 备注 |
|---|---|---|
| 深拷贝 session | 每次步骤 `O(N)`(N = session 大小) | gdr 21 步 → 21 次深拷贝;典型 trajectory 50-500 个 block,Python 深拷贝可接受 |
| Langfuse 上传体积 | 1 session ≈ 50KB-2MB(完整 blocks) | 21 个子 span × 1-2MB ≈ 20-40MB / session;`flush_at=512` 触发批传 |
| 上传带宽 | `sample_rate` 控;默认 1.0 | 可降到 0.1(10% 采样)做轻量模式 |
| Langfuse 配额 | 按 span 计数 | gdr 全开 ~22 span/session,etl ~3,simulate_serve 1;30 task ≈ 750 spans |

**深度优化(可选,默认不开)**:

```yaml
langfuse:
  max_payload_bytes: 5242880          # 5MB;超过则只传 summary + skip input/output
  max_block_payload_bytes: 204800     # 200KB;单 block payload 上限
```

由 `_maybe_truncate` 在客户端工厂内统一执行。

---

## 9. CLAUDE.md 同步建议

在 CLAUDE.md 隐私段落补一句澄清:

> "不保存自由文本思维链、Cookie、Authorization Header 或浏览器 Profile" 的范围是**项目落盘产物**(`output/agent_trajectory/`、`output/refined/`、`output/refine_data/`);Langfuse 观测上传走独立配置(`langfuse.upload_payload=full`),完整 payload 上传到 Langfuse 后端用于轨迹对比观测,**不入训练集**。

---

## 10. 依赖与测试

### 10.1 依赖(`pyproject.toml`)

```toml
[project.dependencies]
# ...

[project.optional-dependencies]
observability = ["langfuse>=3.0,<4.0"]
```

按 CLAUDE.md "默认不访问公网" 的约束,`langfuse` 放 optional,只在显式开启观测时安装。

### 10.2 测试策略(`tests/observability/test_langfuse_*.py`)

| 测试 | 验证内容 |
|---|---|
| `test_langfuse_client_disabled` | `enabled=false` 时 `get_client` 返回 None,所有 helper 安全通过 |
| `test_langfuse_client_init_fail` | 凭据缺失时 `get_client` 返回 None + warning |
| `test_simulate_serve_archiver_emits` | mock `Langfuse.start_as_current_observation`,跑一次 archive,断言调用次数 / `output` 含完整 C1 trajectory |
| `test_gdr_runner_step_spans` | mock `Langfuse`,跑 `_process_one_file` 一份 sample,断言关键 span(`gdr.router.tag` / `gdr.reassemble` / `gdr.save_refined_session`)都被调用且 `input` / `output` 是 session dict |
| `test_gdr_deepcopy_isolation` | 步骤结束后修改 `input_data` 不影响 `output_data`,验证 snapshot() 隔离正确 |
| `test_etl_worker_flush` | 跑 `run_etl_once` 一份,断言 `finally` 块触发 `flush()` |
| `test_etl_payload_full` | 跑 etl 步骤,断言 `output` 含 4 视图 dict |
| `test_langfuse_failure_isolation` | 注入 `Langfuse` 抛异常,断言业务函数正常返回 / 不抛 |
| `test_payload_modes` | 三种模式 `full` / `summary` / `none` 都符合契约 |

CI 默认不连 Langfuse(凭据缺失场景),只跑 unit + contract;functional 可加 `LANGFUSE_TEST_PUBLIC_KEY` env 触发。

### 10.3 实施 Commit 历史(2026-09-23 落地后)

下面按 **git 时间顺序** 列出 PR 1-6 实际落地的 commit hash 与各自承担的范围。由于 PR 2/3/4/5 的子 agent 在执行时把代码落在了工作区未提交,PR 6 在不重写历史(`git rebase -i` 会破坏 review trail 且影响 e773ac4 测试 runtime)的前提下用**增量 commit** 拆分补齐,详情见各 commit message。

| # | 范围 | Commit | 说明 |
|---|---|---|---|
| 0 | 工厂 PR 1 早期落地 | `e816b42` | `simulate_serve/observability/langfuse_client.py` 工厂 + SDK init fail-safe;`test_langfuse_factory.py` |
| 0 | 工厂 PR 1 测试 | `35040b5` | `tests/observability/test_etl_worker.py` etl 端合约测试 |
| 0 | bootstrap + archiver + run_task + etl_worker(混合) | `e773ac4` | **混合 commit**:`simulate_serve/bootstrap.py` + `application/run_task.py` + `infrastructure/trajectory_archiver.py` + `orchestration/workers/etl_worker.py` + `tests/observability/test_simulate_serve_archiver.py`。该 commit 标题仅 "test" 但实际包含 PR 2 + PR 4 落地。已加 `git notes` 说明,见 `git notes show e773ac4` |
| 0 | docs 清理 + etl 模块方案 | `d309b3c` / `78e6948` | `docs/cleanup` + `docs/langfuse-etl.md` 658 行 + `docs/observability-langfuse-plan.md` 737 行 |
| 0 | PR 5 orchestration 接线 | `b550f39` | `orchestration/task_pipeline.py` + `gdr_worker.py` dual-track + etl / task_pipeline 单测 |
| 1 | PR 6 docs backfill | `ba76c0b` | `docs/langfuse-gdr.md` 638 行 + `docs/langfuse-simulate-server.md` 658 行(原 PR 2/3 留下未提交的模块级方案) |
| 2 | PR 6 deps + gdr flat settings | `33d2988` | `pyproject.toml` / `uv.lock` / `gdr/pyproject.toml` / `gdr/config/settings.py` / `config/config.example.yaml` |
| 3 | PR 6 simulate_serve schema + CLAUDE.md | `7642302` | `simulate_serve/config.py` LangfuseConfig + `_parse_config_raw` 剥除 `stages:` + CLAUDE.md 隐私段落 |
| 4 | PR 6 gdr LLM hooks | `0062005` | `gdr/infrastructure/llm_client.py` per-LLM span + `gdr/refiners/retry_loop_clip.py` judge generation + `gdr/reassembly/reassembler.py` 3 reassemble generation spans |
| 5 | PR 6 gdr runner + observability modules + tests | `aabd1e3` | `gdr/pipeline/runner.py` 21 step spans + `gdr/observability/` + `orchestration/observability/langfuse_config.py` + `simulate_serve/observability/__init__.py` + `tests/observability/test_gdr_*` |

**关于 `e773ac4` 不重写的决定**:

- 该 commit 已 lock,影响 `tests/observability/test_simulate_serve_archiver.py` 与 `test_bootstrap.py` 等 runtime 测试路径
- `git rebase -i` 重写历史会触发上述测试 re-run,引入非 PR 6 范围的副作用
- `git notes` 已显式记录其内容归属,后续 review 可在 `git log --notes` 中读到
- 净效果:5 commit + 4 历史 commit,共 9 commit + e773ac4 注解,符合 PR 1-6 整体节奏

---

## 11. 实施拆分(PR 顺序)

| # | 范围 | 改动量 |
|---|---|---|
| 1 | 依赖(`pyproject.toml`)+ 客户端工厂 + 配置 schema + 单测 | ~180 行 |
| 2 | simulate_serve:`trajectory_archiver._emit_trail` 上传完整 C1 trajectory dict | ~60 行 |
| 3 | gdr 外层 + 4 个 LLM 步骤 + `_process_with_payload` 模板 + 21 步骤深拷贝 helper | ~350 行 |
| 4 | gdr 剩余零 LLM 步骤 + `_execute_repair_item` per-block payload | ~200 行 |
| 5 | etl `run_etl_once` 外层 + 2 子 span,`input` 完整 C2,`output` 完整 C3 4 视图 + finally flush | ~120 行 |
| 6 | CLAUDE.md 隐私段落澄清 + 本文档同步至 `docs/` + `docs/observability-langfuse.md` 用户视角文档 | docs |

**总改动估算**:~900 行(含测试)+ 文档。

---

## 12. 风险与回退

| 风险 | 缓解 |
|---|---|
| Langfuse SDK 升级破坏 API | 锁主版本(`>=3.0,<4.0`);helper 集中,改客户端工厂即可 |
| Worker fork 后 client 失效 | 工厂每进程重 init;`finally` 块 flush |
| Langfuse 不可用拖慢业务 | 所有 helper fail-safe;只 `logger.warning`,不 raise |
| 上传体积过大 | `max_payload_bytes` 兜底降级;`upload_payload: summary` 模式可随时切 |
| Span 数爆炸 | `stages.gdr.per_llm_span: false` 默认关;`sample_rate` 调低 |
| simulate_serve 每 turn 都写,trace 变多 | trace_id 用 `remote_session_id` 串,不是 `run_id`;Langfuse 端用同一 session_id 聚合 |
| qf_* 阶段未启用,细粒度 etl span 无法挂 | 暂挂 2 个子 span,`render_to_4_views` 实现后补齐 |
| 测试公网受限 | `tests/observability/` 用 mock + monkeypatch;functional 可选 |
| 深拷贝 session 性能 | `max_payload_bytes=0` 时全量,典型 session <2MB 可接受;极端情况切 `summary` 模式 |

---

## 13. 待办(开工前确认)

- [ ] `upload_payload` 默认值确认(本方案:**full**)
- [ ] `stages.gdr.per_llm_span` 默认值(**本方案:**false)
- [ ] `langfuse` 依赖放在 `[project.optional-dependencies]` 还是主 `dependencies`(**本方案:**optional,贴 CLAUDE.md 公网约束)
- [ ] PR 1 开工