# Langfuse 集成 — etl 模块详细实施方案

> **用户视角文档已迁至 [docs/observability-langfuse.md](observability-langfuse.md)。本文档保留为模块级实施参考。**
>
> 配套基线:[docs/observability-langfuse-plan.md](observability-langfuse-plan.md)
> 配套模块:[docs/langfuse-simulate-server.md](langfuse-simulate-server.md) · [docs/langfuse-gdr.md](langfuse-gdr.md)
> 边界协商:见 §3.6

---

## 1. 模块概述

etl 模块当前结构极其收敛:流水线尾部只有**一个模块函数 `run_etl_once`**(`orchestration/workers/etl_worker.py:68-159`),负责把 gdr 产出的 C2 refined Session 单文件转换为 C3 4 视图文件。完整调用链:

```text
gdr 输出: output/refined/<TXXX>__<session_id>.json  (C2)
    -> etl.parsers.load_refined_session(c2_path)              # C2 契约入口,验 schema_version
    -> gdr.domain.schema.save_session_v2(session, base_path) # 拆 4 视图
    -> 返 EtlOutputs(messages_path, openai_path, qwenjina_path|None, meta_path)
```

**关键边界事实**:

- **qf_* 模块暂未启用**:`etl.writers.render_to_4_views` 是 `NotImplementedError` 桩;`etl.qwenformat.*` 各模块(usage_prune / transform / system_prompt / tool_templates / tool_output_summarizer)都不在 `run_etl_once` 当前路径上。`run_etl_once` 当前直接 `load → save`,跳过整个 qf 处理链(plan.md §5.3 "qf_* 暂未启用")。
- **进程模型**:`run_etl_once` 在 `multiprocessing.Pool` worker 中跑(`task_pipeline._run_one_task_pipeline` → `_safe_run_etl` → `run_etl_once`)。Langfuse SDK **不是 fork-safe**,worker 子进程必须重新 init 客户端(工厂已处理:进程级 `_client` 单例 + `_lock`)。
- **跨阶段 trace 串联边界**:etl **不读** Langfuse 后端。三阶段独立生成 trace,通过 `session_id` 字符串一致在 Langfuse 端聚合。
- **两个 `load_refined_session` 名字冲突已存在**:`etl.parsers.load_refined_session`(C2 契约入口,返 `Session` 对象)和 `etl.qwenformat.usage_prune.load_refined_session`(从 messages.json + meta.json 重组 session dict)同名异构。本方案仅 hook `etl.parsers.load_refined_session`,与 `usage_prune` 路径无关;不改 import 链,无新冲突。

---

## 2. 接入点逐个详述

### 2.1 `run_etl_once` 签名改造

```python
from __future__ import annotations  # 顶部必须有,否则字符串前向引用在 3.10+ 报 NameError
from orchestration.observability.langfuse_config import LangfuseConfig

def run_etl_once(
    *, c2_path: Path, etl_outputs_dir: Path, task_id: str, session_id: str,
    attempt: int = 0,
    langfuse_cfg: LangfuseConfig | None = None,
) -> EtlOutputs:
```

- `attempt` 来自 `_safe_run_etl` 循环下标(`0..max_retry_etl`),用于 outer trace metadata 标记重试序号
- `langfuse_cfg` 由 `_run_one_task_pipeline` 注入;`None` 时内部走懒加载读根配置

### 2.2 外层 `stage_trace`:`run_etl_once` body

**位置**:`run_etl_once` body 起始(`c2_path = Path(c2_path)` 之前),紧接 `t0 = time.perf_counter()` 之后。

**span 名**:`etl:{task_id}`

```python
from simulate_serve.observability.langfuse_client import (
    get_client, stage_trace, step_span, snapshot,
)

client = get_client(langfuse_cfg)
payload_mode = getattr(langfuse_cfg, "upload_payload", "full") if langfuse_cfg else "none"
c2_raw_dict: dict | None = None
outputs: EtlOutputs | None = None

try:
    with stage_trace(
        client,
        session_id=session_id,
        name=f"etl:{task_id}",
        user_id=session_id,
        task_id=task_id,
        tags=["stage:etl", f"task:{task_id}", f"attempt:{attempt}"],
        metadata={
            "stage": "etl",
            "task_id": task_id,
            "session_id": session_id,
            "attempt": attempt,
            "c2_path": str(c2_path),
            "etl_outputs_dir": str(etl_outputs_dir),
        },
        input_data=None,
        output_capture=lambda: _capture_save_payload(outputs) if outputs else None,
        payload_mode=payload_mode,
        max_payload_bytes=getattr(langfuse_cfg, "max_payload_bytes", 0) if langfuse_cfg else 0,
    ):
        # ... 原 body(load + save)
finally:
    if client is not None:
        try:
            client.flush()
        except Exception:
            pass
```

### 2.3 子 span `load_refined_session`

**位置**:`run_etl_once` 原 line 124-131。

**span 名**:`etl.load_refined_session`

```python
# body 早于 step_span,先读 raw
try:
    import json as _json
    c2_raw_dict = _json.loads(c2_path.read_text(encoding="utf-8"))
except Exception as exc:
    c2_raw_dict = {"_raw_read_error": f"{type(exc).__name__}: {exc}"}

c2_raw_snap = snapshot(c2_raw_dict) if c2_raw_dict else None

try:
    with step_span(
        client,
        name="etl.load_refined_session",
        input_data=c2_raw_snap,
        output_capture=lambda: _to_jsonable(session) if session is not None else None,
        payload_mode=payload_mode,
        session_id=session_id,
        task_id=task_id,
        max_payload_bytes=getattr(langfuse_cfg, "max_payload_bytes", 0) if langfuse_cfg else 0,
        metadata={"c2_path": str(c2_path)},
    ):
        session = load_refined_session(c2_path)
except (ValueError, UnicodeDecodeError, FileNotFoundError) as exc:
    raise EtlNonRetryableError(
        f"run_etl_once: load_refined_session error ({type(exc).__name__}): {exc}"
    ) from exc
```

### 2.4 子 span `save_session_v2`(`etl.save_c3_4views`)

**位置**:`run_etl_once` 原 line 141。

**span 名**:`etl.save_c3_4views`

```python
def _capture_save_payload(out: EtlOutputs | None) -> dict | None:
    if out is None:
        return None
    def _size(p):
        return p.stat().st_size if p and p.exists() else None
    return {
        "messages": str(out.messages_path),
        "openai": str(out.openai_path),
        "qwenjina": str(out.qwenjina_path) if out.qwenjina_path else None,
        "meta": str(out.meta_path),
        "messages_bytes": _size(out.messages_path),
        "openai_bytes": _size(out.openai_path),
        "qwenjina_bytes": _size(out.qwenjina_path),
        "meta_bytes": _size(out.meta_path),
    }

def _safe_dump_session_obj(s) -> dict:
    """DEPRECATED: 直接用工厂 `_to_jsonable`,本函数保留仅为向后兼容."""
    if s is None:
        return None
    if hasattr(s, "model_dump"):
        return s.model_dump(mode="json", exclude_none=True)
    return getattr(s, "__dict__", {"_unrepr": str(s)[:1000]})

base_path = etl_outputs_dir / _output_filename(task_id, session_id, suffix="")
base_path.parent.mkdir(parents=True, exist_ok=True)

with step_span(
    client,
    name="etl.save_c3_4views",
    input_data=_safe_dump_session_obj(session),
    output_capture=lambda: _capture_save_payload(outputs),
    payload_mode=payload_mode,
    session_id=session_id,
    task_id=task_id,
    max_payload_bytes=getattr(langfuse_cfg, "max_payload_bytes", 0) if langfuse_cfg else 0,
    metadata={"base_path": str(base_path)},
):
    outputs = EtlOutputs.from_session_v2(save_session_v2(session, base_path))
```

### 2.5 重试 trace 策略(关键决策)

**决策**:`_safe_run_etl` 重试 N 次 → Langfuse 上生成 **N 个独立 trace**(`metadata.attempt=N`)。

**理由**:
1. 每次 `run_etl_once` 是**独立执行**(重新 load + 重新 save + 独立内存状态)
2. 把 4 次尝试塞进同一 trace 会导致 `input/output` 语义不清
3. 独立 trace + `metadata.attempt` 让 Langfuse 端按 `session_id` 聚合时,N 次尝试按时间排列清晰
4. `flush_at=512` 默认批传,N 个 trace 在 worker 内存里聚合;finally flush 强制单 task 边界

```python
# orchestration/task_pipeline.py::_safe_run_etl,line 148-156
for attempt in range(max_retry + 1):
    try:
        queue.increment_attempts(task_id, stage="etl")
        outputs = run_etl_once(
            c2_path=c2_path,
            etl_outputs_dir=etl_outputs_dir,
            task_id=task_id,
            session_id=session_id,
            attempt=attempt,
            langfuse_cfg=langfuse_cfg,
        )
        ...
```

### 2.6 错误状态(工厂 PR 1 + 异常处理)

- `EtlNonRetryableError` 抛出 → factory except 标 `level="ERROR"` + `status_message`(详见 §3.6)
- `save_session_v2` 抛非 retryable 异常 → outer span `level="ERROR"` → `_safe_run_etl` 捕获重试

### 2.7 `finally` flush 钩子

**主路径**:`run_etl_once` body 的外层 `try / finally`(包围整个 outer `stage_trace`),每 task 边界由该 finally 强制 `client.flush()`。

**兜底路径**:`orchestration/task_pipeline._worker_init` 注册 `atexit` handler,worker 子进程退出时(无论正常退出还是 uncaught 异常)最后再 `flush()` 一次。覆盖 SIGTERM 正常关闭;SIGKILL / OOM 不可避免。

```python
# orchestration/task_pipeline.py:_worker_init 末尾追加
import atexit
from simulate_serve.observability.langfuse_client import get_client

def _worker_init(paths: Paths) -> None:
    global _WORKER_PATHS
    _WORKER_PATHS = paths
    # Langfuse fork-safe: 强制清旧 singleton, 下次 get_client 时重建
    try:
        from simulate_serve.observability.langfuse_client import _client, _lock
        with _lock:
            _client = None
    except Exception:
        pass
    # atexit flush 兜底
    def _atexit_flush():
        try:
            c = get_client(None)
            if c is not None:
                c.flush()
        except Exception:
            pass
    atexit.register(_atexit_flush)
```

---

## 3. 边界契约

### 3.1 从 gdr 接收的契约(etl 的 Langfuse 上游)

| 维度 | 规则 |
|---|---|
| gdr 上传给 Langfuse 的内容 | `gdr.<task_id>` outer trace + ~21 子 span,outer.output = 完整 C2 refined Session dict |
| etl **不读** Langfuse 后端 | 与 gdr 不读 simulate_serve 的 Langfuse 同理 |
| etl **只**通过 `load_refined_session` 读 C2 文件 | 沿用既有契约(验 schema_version)|
| 跨进程 trace 串联唯一字段 | `session_id = run.remote_session_id`;etl worker 取自 `run_etl_once(session_id=)` 参数 |
| Session 字段交叉校验 | `assert session.session_id == session_id` 失败时 EtlNonRetryableError + outer span ERROR |

### 3.2 给训练框架 / audit 的输出契约(etl 的 Langfuse 下游)

C3 文件产物(磁盘)**不**变——4 视图文件按现有契约输出。Langfuse 是观测副本,**不**替代文件产物。

| 元素 | Langfuse 字段 | 取值 |
|---|---|---|
| `session_id` | Langfuse `session_id` | `Session.session_id` |
| `user_id` | 同 `session_id` | |
| `trace_name` | outer span `name` | `etl:{task_id}` |
| Span `name`(子)| `etl.load_refined_session` / `etl.save_c3_4views` | |
| `metadata.task_id` | T001 等 | |
| `metadata.stage` | `"etl"` | |
| `metadata.attempt` | `0..max_retry_etl` | |
| `metadata.c2_path` | str(c2_path) | |
| `metadata.etl_outputs_dir` | str(etl_outputs_dir) | |
| `tags` | `["stage:etl", "task:{task_id}", "attempt:{N}"]` | |

| span | `input` | `output` |
|---|---|---|
| `etl:<task_id>`(outer) | (空,C2 已在 gdr 端上传)| 完整 C3 4 视图 dict(`_capture_save_payload`) |
| `etl.load_refined_session` | C2 文件 dict(`c2_raw_dict`)| Session 对象 dump dict |
| `etl.save_c3_4views` | Session 对象 dump dict | C3 4 视图路径 dict(含每文件字节数) |

**payload_mode 切换**:`langfuse.upload_payload=full` 默认;`stages.etl.per_step_span=false` 时外层仍开,`load`/`save` 两个子 span 跳过。

### 3.3 进程退出契约

| 退出场景 | flush 保证 |
|---|---|
| `run_etl_once` 成功 / 业务异常返回 | finally 块同步 flush(主路径) |
| `multiprocessing.Pool` worker 正常 shutdown | atexit handler 在 worker exit 时 flush |
| Worker SIGTERM 优雅关闭 | atexit 触发;若超出 Python shutdown 超时则可能丢 |
| Worker SIGKILL / OOM | atexit 不触发;该 task 整体标 dead,Langfuse 端那段 trace 缺失视为观测不完整(可接受) |

### 3.4 错误状态契约

- `EtlNonRetryableError` 抛出 → outer + load 子 span `level="ERROR"` + `status_message` 含异常摘要
- `save_session_v2` 抛非 retryable 异常 → outer + save 子 span `level="ERROR"`

### 3.5 metadata 白名单

| 字段 | 来源 |
|---|---|
| `stage` | `"etl"` |
| `task_id` | `run_etl_once(task_id=)` 参数 |
| `session_id` | `run_etl_once(session_id=)` 参数 |
| `attempt` | `_safe_run_etl` 循环下标 |
| `c2_path` | `run_etl_once(c2_path=)` 参数 |
| `etl_outputs_dir` | `run_etl_once(etl_outputs_dir=)` 参数 |
| `base_path` | save_session_v2 调用路径 |

### 3.6 边界协商(本模块与 simulate_serve / gdr)

#### 客户端工厂位置

**结论**:`simulate_serve/observability/langfuse_client.py`,本模块 import 共享:

```python
from simulate_serve.observability.langfuse_client import (
    get_client, stage_trace, step_span, snapshot,
    _to_jsonable as _safe_dump_session_obj,   # 复用工厂 _to_jsonable
    _maybe_truncate, _reset_for_fork,
)
```

详见 [langfuse-simulate-server.md §3.6](langfuse-simulate-server.md)。

#### 配置字段形态(dataclass)

etl 端**frozen dataclass**:

```python
# orchestration/observability/langfuse_config.py
@dataclass(frozen=True)
class LangfuseConfig:
    """编排层视图; 与 simulate_serve/observability 的 pydantic 类字段一致."""
    enabled: bool = False
    public_key: str = ""
    secret_key: str = ""
    base_url: str = "https://cloud.langfuse.com"
    environment: str = "dev"
    release: str = "local"
    sample_rate: float = 1.0
    flush_at: int = 512
    flush_interval: float = 5.0
    timeout: int = 10
    upload_payload: str = "full"
    max_payload_bytes: int = 0
    per_step_span: bool = True    # stages.etl.per_step_span

def load_langfuse_config() -> LangfuseConfig:
    """从根配置 langfuse: 段懒加载."""
    from shared_config import find_root_config, load_yaml_file
    root = find_root_config()
    raw = load_yaml_file(root) if root else {}
    section = (raw.get("langfuse") or {}) if isinstance(raw, dict) else {}
    stages = (section.get("stages") or {}).get("etl") or {}
    return LangfuseConfig(
        enabled=bool(section.get("enabled", False)),
        public_key=str(section.get("public_key", "")),
        secret_key=str(section.get("secret_key", "")),
        base_url=str(section.get("base_url", "https://cloud.langfuse.com")),
        environment=str(section.get("environment", "dev")),
        release=str(section.get("release", "local")),
        sample_rate=float(section.get("sample_rate", 1.0)),
        flush_at=int(section.get("flush_at", 512)),
        flush_interval=float(section.get("flush_interval", 5.0)),
        timeout=int(section.get("timeout", 10)),
        upload_payload=str(section.get("upload_payload", "full")),
        max_payload_bytes=int(section.get("max_payload_bytes", 0)),
        per_step_span=bool(stages.get("per_step_span", True)),
    )
```

`_extract_langfuse_fields(cfg)` 鸭子类型兼容(详见 [langfuse-simulate-server.md §3.6](langfuse-simulate-server.md)):dataclass / pydantic 嵌套 / pydantic 扁平都支持。

#### 决策:dataclass vs pydantic

**结论**:etl 端**frozen dataclass**(`orchestration/observability/langfuse_config.py`)。

**理由**:
1. etl worker 在 `orchestration/` 下,本地 dataclass 与 `PipelineSettings` 风格一致
2. 不在 worker 内重新走 pydantic 校验,纯数据容器
3. `load_langfuse_config()` 懒加载避免每次 `run_etl_once` 重复 IO
4. 父进程已展开配置,worker 子进程共享(避免重复 IO)

#### Outer trace 命名

**结论**:`etl:{task_id}`(顶层 trace,`run_etl_once` 外层)。

#### session_id 串联

**结论**:`run_etl_once(session_id=)` 参数即 gdr-端 `Session.session_id` 即 simulate_serve-端 `run.remote_session_id`,跨进程约定。

#### 重试 trace 策略

**结论**:N 次独立 trace + `metadata.attempt=N` + `tags=["attempt:N"]`。

#### Worker fork-safe

**结论**:`orchestration/task_pipeline.py::_worker_init` 末尾:
- Langfuse client reset(`_client = None`)
- atexit flush 注册

与 gdr 端(`gdr/pipeline/runner.py::_worker_init`)是两个独立位置,互不冲突。

> **PR 5 实施状态 (2026-09-23, 已落地)**: `_worker_init` 在 `langfuse.enabled=True` 时调 `_reset_for_fork()` + 注册 `atexit.register(_lf_shutdown)`; `_run_one_task_pipeline` 入口构造 `langfuse_cfg = load_langfuse_config()` + `langfuse_client = get_client(cfg)`,透传到 `_safe_run_gdr` / `_safe_run_etl` / `run_gdr_once` / `run_etl_once`; `_safe_run_etl` 每次 attempt 透传 `attempt=N, langfuse_cfg=cfg`, `run_etl_once` 每次开独立 outer `stage_trace` (PR 4 设计意图)。`run_gdr_once` 加 `langfuse_client` / `langfuse_cfg` 双轨注入参数 (PR 3 旧测试不破)。

#### finally flush vs atexit

**结论**:**两者都要**:finally 主路径(每 task 边界 flush)+ atexit 兜底(worker 退出)。

---

## 4. 配置文件改动清单

### 4.1 根 `config/config.yaml` 模板

`config/config.example.yaml` 顶部新增 `langfuse:` 段(同 [langfuse-simulate-server.md §4.1](langfuse-simulate-server.md)):

```yaml
langfuse:
  enabled: false
  public_key: "${LANGFUSE_PUBLIC_KEY}"
  secret_key: "${LANGFUSE_SECRET_KEY}"
  base_url: "https://cloud.langfuse.com"
  environment: "dev"
  release: "${LANGFUSE_RELEASE:-local}"
  sample_rate: 1.0
  flush_at: 512
  flush_interval: 5.0
  timeout: 10
  upload_payload: full
  max_payload_bytes: 0
  stages:
    simulate_serve:
      enabled: true
      export_trajectory_on_terminal: true
    gdr:
      enabled: true
      per_step_span: true
      per_llm_span: false
    etl:
      enabled: true
      per_step_span: true
```

### 4.2 etl 端不独立段

etl **不**新增 `etl.qwenformat.langfuse:` 子段;配置从根 `langfuse:` 段读,与 simulate_serve / gdr **共享**。阶段级开关由 `stages.etl.enabled` / `stages.etl.per_step_span` 控制。

---

## 5. 新增 / 修改文件清单

### 新增

| 路径 | 行数 | 用途 |
|---|---|---|
| `orchestration/observability/__init__.py` | 1 | 包标识 |
| `orchestration/observability/langfuse_config.py` | ~40 | `LangfuseConfig` dataclass + `load_langfuse_config()` |
| `tests/observability/__init__.py` | 1 | |
| `tests/observability/test_etl_worker.py` | ~250 | etl 阶段 Langfuse 接入单元测试 |
| `tests/observability/test_langfuse_factory.py` | ~150 | 客户端工厂 unit 测试(disabled / init_fail / level=ERROR)|

### 修改

| 路径 | 改动 |
|---|---|
| `config/config.example.yaml` | 顶部新增 `langfuse:` 段 |
| `orchestration/workers/etl_worker.py` | **主改动**:outer `stage_trace` + 2 子 `step_span` + finally flush + `attempt` / `langfuse_cfg` 参数 |
| `orchestration/task_pipeline.py` | `_worker_init` 注册 atexit flush + Langfuse reset;`_run_one_task_pipeline` 构造 `langfuse_cfg` 透传;`_safe_run_etl` 调 `run_etl_once` 时传 `attempt=N, langfuse_cfg=cfg` |
| `simulate_serve/observability/langfuse_client.py` | **factory 改动**:except 分支标 `level="ERROR"` |
| `pyproject.toml` | `[project.optional-dependencies]` 加 `observability = ["langfuse>=3.0,<4.0"]`;`[dependency-groups]` dev 加 `langfuse>=3.0,<4.0` |

### 不动

- `etl/parsers/__init__.py` — 契约入口,Langfuse hook 在调用方(`run_etl_once`)而非被调方
- `etl/writers/__init__.py` — `render_to_4_views` 是 `NotImplementedError` 桩
- `etl/qwenformat/*` — qf_* 未在 `run_etl_once` 路径,`render_to_4_views` 实现后另案处理

---

## 6. 测试清单

### 单元测试 `tests/observability/test_etl_worker.py`

`MockLangfuseClient` / `MockSpan` 见 §6.1。

| 测试名 | 验证 |
|---|---|
| `test_run_etl_once_emits_outer_span` | 1 outer + 2 子 span |
| `test_run_etl_once_outer_input_c2_dict` | outer.input = C2 dict |
| `test_run_etl_once_outer_output_4views` | outer.output = `_capture_save_payload` 产物 |
| `test_run_etl_once_load_subspan_input_output` | etl.load_refined_session 子 span |
| `test_run_etl_once_save_subspan_input_output` | etl.save_c3_4views 子 span |
| `test_run_etl_once_qwenjina_none_subspan` | qwenjina=None 时 outer.output["qwenjina"]=None |
| `test_run_etl_once_payload_mode_summary` | summary 模式仅 metadata |
| `test_run_etl_once_payload_mode_none` | none 模式 input/output=None |
| `test_run_etl_once_non_retryable_span_error` | ValueError → EtlNonRetryableError → span ERROR |
| `test_run_etl_once_save_failure_span_error` | save_session_v2 抛 RuntimeError → span ERROR |
| `test_run_etl_once_disabled_no_span` | enabled=False 时无 span 创建 |
| `test_run_etl_once_retry_creates_independent_traces` | max_retry=2 → 3 个 outer span,metadata.attempt=0/1/2 |
| `test_run_etl_once_finally_flush` | finally 触发 flush |
| `test_run_etl_once_finally_flush_on_error` | EtlNonRetryableError 抛出后 flush 仍触发 |
| `test_run_etl_once_session_id_mismatch_assert` | C2 session_id ≠ 参数 → ERROR |
| `test_run_etl_once_attempt_in_tags` | tags 含 `attempt:N` |
| `test_run_etl_once_langfuse_failure_isolated` | mock client 抛异常 → 业务正常返回 |

### 单元测试 `tests/observability/test_langfuse_factory.py`

| 测试名 | 验证 |
|---|---|
| `test_get_client_disabled_returns_none` | enabled=False 返 None |
| `test_get_client_missing_credentials_returns_none` | 凭据空返 None |
| `test_get_client_accepts_dataclass` | frozen dataclass 被正确抽取 |
| `test_stage_trace_yields_none_on_disabled` | client=None 时不创建 span |
| `test_step_span_marks_error_on_exception` | 子 span 内异常 → level="ERROR" + status_message |
| `test_snapshot_deepcopy_isolation` | 深拷贝隔离 |

### Functional(可选)

| 测试名 | 验证 |
|---|---|
| `test_etl_pipeline_e2e_with_mock_langfuse` | 完整 trace 链结构 |
| `test_etl_real_langfuse_smoke` | 真 Langfuse 凭据时跑 sample |

### 现有测试不破

- `tests/orchestration/test_etl_worker.py` — `run_etl_once` 新参数 `attempt=0, langfuse_cfg=None` 有默认值
- `tests/orchestration/test_task_pipeline.py` — `_run_one_task_pipeline` / `_safe_run_etl` 新参数同上

---

## 7. 实施步骤(commit 级)

### Commit 1:依赖 + 客户端工厂 + 配置 schema + factory `level="ERROR"`

- `simulate_serve/observability/__init__.py` + `langfuse_client.py`(plan.md §4 + `_extract_langfuse_fields` + `level="ERROR"` 行为)
- `orchestration/observability/__init__.py` + `langfuse_config.py`
- `config/config.example.yaml` 顶部 `langfuse:` 段
- `pyproject.toml` `observability` extra + dev `langfuse>=3.0,<4.0`
- 测试 `tests/observability/test_langfuse_factory.py`

commit message:`feat(observability): shared LangfuseConfig + factory with level=ERROR`

### Commit 2:`run_etl_once` outer `stage_trace` + finally flush

- `run_etl_once` 加 `attempt=0, langfuse_cfg=None` 参数
- outer `stage_trace` 包 body
- finally 同步 flush
- `c2_raw_dict` 读取 helper

commit message:`feat(etl): run_etl_once outer stage_trace + finally flush`

### Commit 3:`etl.load_refined_session` sub-span

- `load_refined_session` 调用块包 `step_span`
- `c2_raw_dict` 深拷贝传 input
- Session dump helper

commit message:`feat(etl): etl.load_refined_session sub-span with C2 input/Session output`

### Commit 4:`etl.save_c3_4views` sub-span

- `save_session_v2` 调用块包 `step_span`
- `_capture_save_payload` helper(4 路径 + 字节数)
- outer `output_capture` 用 `_capture_save_payload` 闭包
- qwenjina=None 边界处理

commit message:`feat(etl): etl.save_c3_4views sub-span with Session input/4views output`

### Commit 5:`_safe_run_etl` 重试 → N 独立 trace + atexit flush

- `_run_one_task_pipeline` 入口构造 `langfuse_cfg=load_langfuse_config()`
- `_safe_run_etl` 接收 `langfuse_cfg` 并透传给 `run_etl_once`
- `_safe_run_etl` 调用处传 `attempt=attempt`
- `_worker_init` 注册 atexit flush + Langfuse reset
- `session_id` assert helper

commit message:`feat(etl): _safe_run_etl retry -> N independent traces + atexit flush`

### Commit 6:测试

- `tests/observability/test_etl_worker.py` 新建 + 17 个测试用例

commit message:`test(observability): etl worker Langfuse unit tests`

### Commit 7:文档

- `CLAUDE.md` 隐私段落澄清(plan.md §9)
- `docs/observability-langfuse.md`(用户视角)

commit message:`docs: CLAUDE.md privacy section + observability-langfuse.md`

### Commit 8(future):qf_* 子 span

待 `render_to_4_views` 实现时再拆。每 qf_* 模块一个 `step_span`(usage_prune / transform / system_prompt / tool_templates / tool_output_summarize)。

**总改动估算**:~830 行(含测试)+ 文档,不含 PR 8 future。

---

## 8. 风险与回退开关

| 风险 | 缓解 |
|---|---|
| Worker fork 后 Langfuse client 失效 | factory per-process singleton + worker init `_client = None` |
| Worker 异常退出(SIGTERM / OOM)时 flush 不保证 | finally 主路径 + atexit 兜底;SIGKILL/OOM 不可避免 |
| `_safe_run_etl` 重试 3 次 → Langfuse 配额被 3 倍 trace 占用 | 决策固定 N 个独立 trace;`sample_rate=0.1` 或 `max_retry_etl=0` 兜底 |
| qf_* 启用前无法挂细粒度 span | Commit 8 future |
| `save_session_v2` 跳过 `qwenjina.txt` 时 output 字段 None | `_capture_save_payload` 显式处理 |
| 两个 `load_refined_session` 同名 | 仅 hook `etl.parsers.load_refined_session`,无新冲突 |
| `session_id` 参数与 C2 文件内 session_id 不一致 | `assert session.session_id == session_id` 触发 EtlNonRetryableError + outer ERROR |
| `langfuse.enabled=true` 但 Langfuse 后端宕机 | factory fail-safe + 业务无感 |
| 大 payload(单 session > 5MB) | `max_payload_bytes` 自动降级 |

**总回退开关**:`langfuse.enabled=false` 一键关闭三阶段全部观测;`stages.etl.enabled=false` 仅关 etl;`stages.etl.per_step_span=false` 仅关 etl 子 span(保留 outer)。

---

## 9. 验证脚本

```bash
# 单元测试
uv run pytest tests/observability/ -v

# 既有测试不破
uv run pytest tests/orchestration/test_etl_worker.py -v
uv run pytest tests/orchestration/test_task_pipeline.py -v

# 全量回归
uv run pytest -q

# grep 自检
rg "stage_trace\(" orchestration/workers/etl_worker.py
rg "run_etl_once\(" orchestration/task_pipeline.py
rg "load_langfuse_config\(\)" orchestration/
rg "atexit" orchestration/task_pipeline.py
rg "_capture_save_payload" orchestration/workers/etl_worker.py

# finally flush 触发检查
uv run pytest tests/observability/test_etl_worker.py::test_run_etl_once_finally_flush -v
uv run pytest tests/observability/test_etl_worker.py::test_run_etl_once_finally_flush_on_error -v

# 端到端真实 Langfuse(可选)
export LANGFUSE_PUBLIC_KEY="pk-..."
export LANGFUSE_SECRET_KEY="sk-..."
# config/config.yaml: langfuse.enabled: true, stages.etl.enabled: true
# orchestration.pipeline.max_parallelism: 1, max_retry_etl: 0
uv run python -m orchestration start --tasks T001 --parallelism 1

# 重试 trace 独立检查
# orchestration.pipeline.max_retry_etl: 2 + mock save 抛错
# Langfuse 端应看到 3 个独立 etl:T001 trace,metadata.attempt=0/1/2
```

---

## 文档元信息

- **配套基线**:`docs/observability-langfuse-plan.md`(2026-09-23 定稿)
- **本方案覆盖**:plan.md §5.3 etl 接入点工程细化 + §11 PR 5 拆分到 7 个 commit
- **客户端工厂位置**:§3.6(共享 `simulate_serve/observability/langfuse_client.py`)
- **配置字段形态**:§3.6 frozen dataclass(`orchestration/observability/langfuse_config.py`)
- **未覆盖**:plan.md 其他 PR(simulate_serve / gdr)各自独立,详见 [langfuse-simulate-server.md](langfuse-simulate-server.md) · [langfuse-gdr.md](langfuse-gdr.md)