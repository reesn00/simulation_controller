# Langfuse 集成 — simulate_serve 模块详细实施方案

> 配套基线:[docs/observability-langfuse-plan.md](observability-langfuse-plan.md)(整体设计 + 配置 Schema + 客户端工厂设计 + 命名规范)
> 配套模块:[docs/langfuse-gdr.md](langfuse-gdr.md) · [docs/langfuse-etl.md](langfuse-etl.md)
> 边界协商:见 §3.6

---

## 1. 模块概述

simulate_serve 是三阶段流水线的**第 0 阶段**,把 Persona + Scenario + Task 编译为 `CompiledTask`,以用户身份驱动**远端 QwenPaw** 执行 Agent,多轮验证并追问,最终落 C1 trajectory 事件流(`output/agent_trajectory/<run_id>__<session_id>.json`)。它是唯一会**发起远端执行**的阶段;gdr / etl 都只读其落盘产物。

**Trajectory 写入时机**:每个 executor turn(开场 + 每次追问)结束后立即同步落盘一次(`TaskRuntime._record_response` → `_archive_trajectory` → `QwenPawTrajectoryArchiver.archive`)。同一 `(run_id, session_id)` 文件被**覆盖式多轮累积**,文件名 `<run_id>__<session_id>.json` 自指语义。Archiver 内部带 60 秒终态事件预算(`_COMPLETION_WAIT_BUDGET_S=60.0`),等终态事件 `final_reply` / `error` / `cancel`,规避 QwenPaw HTTP `finished` 早于落盘的 race。

**与远端 QwenPaw 的同步关系**:`AsyncQwenPawExecutor` 异步 HTTP,每次 `ExecutorResponse` 立即调用 `_archive_trajectory`;archiver 同步阻塞(最多 60 秒等终态事件)。这一窗口是 Langfuse 上传必须遵守的约束——不能让 Langfuse 把本已可观的 60 秒拉成几分钟。

---

## 2. 接入点逐个详述

### 2.1 客户端工厂:`simulate_serve/observability/langfuse_client.py`(新建)

定位:**放在 `simulate_serve/observability/`**(非顶级包,详见 §3.6 边界决策)。`simulate_serve` 是 Langfuse 集成的**首方发起者**,gdr / etl 通过 `from simulate_serve.observability.langfuse_client import ...` 复用,模块物理位置不耦合 import 路径。

完整实现见 [observability-langfuse-plan.md §4](observability-langfuse-plan.md)。本模块补充要点:

- **公开函数**:`get_client(config)`、`stage_trace(...)`、`step_span(...)`、`snapshot(obj)`、`shutdown()`
- **鸭子类型兼容**:`get_client()` 接受"任何含 langfuse 字段的对象"(详见 §3.6 协商结论),不强制 `isinstance(LangfuseConfig)` 检查
- **Import 兜底**:`from langfuse import Langfuse, propagate_attributes` 包在 try/except 内,SDK 未安装时 `get_client` 永久返 None

### 2.2 `simulate_serve/infrastructure/trajectory_archiver.py`

> **异常处理分工**:`_copy_with_retry` 已在 `archive()` 内部 try/except 兜住 OSError / Exception(原 line 86-102),业务异常不会传到这里。`_emit_trail` 内的 `stage_trace` 调用即使触发 `level="ERROR"`(理论上不会发生,因业务异常已被外层吞),也只是 Langfuse 端标记,不影响 archiver 行为。**外层 try/except 保留**作为上传本身的 fail-safe(网络异常 / SDK 异常)。

```python
class QwenPawTrajectoryArchiver:
    def __init__(
        self,
        output_dir: str | Path,
        *,
        user_id: str,
        source_dir: str | Path | None = None,
    ):
        self._user_id = user_id
        self._source_override = Path(source_dir) if source_dir else None
        self.output_dir = Path(output_dir) / "agent_trajectory"
        self._warned_missing: set[str] = set()

    def archive(self, run_id: str, agent_id: str, session_id: str) -> None:
        if not session_id:
            return
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            source = self._source_path(agent_id, session_id)
            target = self.output_dir / trajectory_filename(run_id, session_id)
            self._copy_with_retry(source, target, session_id)
        except OSError as exc:
            self._warn(session_id, "trajectory copy failed for session %s: %s", session_id, exc)
        except Exception as exc:
            self._warn(session_id, "unexpected trajectory capture error for session %s: %s", session_id, exc)
```

#### 改动后

```python
class QwenPawTrajectoryArchiver:
    def __init__(
        self,
        output_dir: str | Path,
        *,
        user_id: str,
        source_dir: str | Path | None = None,
        langfuse_config: LangfuseConfig | None = None,   # NEW
    ):
        self._user_id = user_id
        self._source_override = Path(source_dir) if source_dir else None
        self.output_dir = Path(output_dir) / "agent_trajectory"
        self._warned_missing: set[str] = set()
        self._langfuse_config = langfuse_config        # NEW; None = disabled
        self._run_ctx: dict[str, Any] = {}             # NEW; via set_run_context()

    def set_run_context(self, run_ctx: dict[str, Any]) -> None:
        """Set per-turn run context used by ``_emit_trail`` for Langfuse metadata."""
        self._run_ctx = dict(run_ctx)

    def archive(self, run_id: str, agent_id: str, session_id: str) -> None:
        if not session_id:
            return
        target = None
        last_event_type: str | None = None
        terminal_reached = False
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            source = self._source_path(agent_id, session_id)
            target = self.output_dir / trajectory_filename(run_id, session_id)
            self._copy_with_retry(source, target, session_id)
            last_event_type = _trajectory_last_event_type(source)
            terminal_reached = last_event_type in _TERMINAL_EVENT_TYPES
        except OSError as exc:
            self._warn(session_id, "trajectory copy failed for session %s: %s", session_id, exc)
        except Exception as exc:
            self._warn(session_id, "unexpected trajectory capture error for session %s: %s", session_id, exc)
        finally:
            # NEW: emit Langfuse trail after every archive call (fail-safe).
            if target is not None:
                self._emit_trail(target, last_event_type, terminal_reached)

    def _emit_trail(self, source: Path, last_event_type: str | None,
                    terminal_reached: bool) -> None:
        cfg = self._langfuse_config
        if cfg is None or not getattr(cfg, "enabled", False):
            return
        client = get_client(cfg)
        if client is None:
            return
        run = self._run_ctx
        session_id = run.get("remote_session_id") or run.get("run_id") or ""
        task_id = run.get("task_id") or ""
        if not task_id:
            return

        payload_mode = getattr(cfg, "upload_payload", "full")

        def _output_payload():
            if not source.exists():
                return {"_missing": True}
            try:
                return [
                    json.loads(line)
                    for line in source.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            except Exception as exc:
                return {"_read_error": str(exc)}

        try:
            with stage_trace(
                client,
                session_id=session_id,
                name=f"simulate_serve:{task_id}",
                user_id=session_id,
                task_id=task_id,
                tags=["stage:simulate_serve", f"task:{task_id}"],
                metadata={
                    "stage": "simulate_serve",
                    "run_id": run.get("run_id"),
                    "task_id": task_id,
                    "session_id": session_id,
                    "agent_id": run.get("remote_agent_id"),
                    "terminal_reached": terminal_reached,
                    "last_event_type": last_event_type,
                    "trajectory_path": str(source),
                },
                input_data=None,
                output_capture=_output_payload,
                payload_mode=payload_mode,
                max_payload_bytes=getattr(cfg, "max_payload_bytes", 0),
            ):
                pass
        except Exception as exc:
            logger.warning("Langfuse _emit_trail failed for session %s: %s", session_id, exc)
```

### 2.3 `simulate_serve/application/run_task.py` — `_archive_trajectory`

#### 当前(line 353-364)

```python
def _archive_trajectory(self, run: TaskRun) -> None:
    if not self.trajectory_archiver or not run.remote_session_id:
        return
    try:
        self.trajectory_archiver.archive(run.run_id, run.remote_agent_id, run.remote_session_id)
    except Exception:
        logger.warning("trajectory archiver raised; capture skipped", exc_info=True)
```

#### 改动后(注入 run_ctx,不破坏 Port 契约)

```python
def _archive_trajectory(self, run: TaskRun) -> None:
    if not self.trajectory_archiver or not run.remote_session_id:
        return
    try:
        if hasattr(self.trajectory_archiver, "set_run_context"):
            self.trajectory_archiver.set_run_context({
                "run_id": run.run_id,
                "task_id": run.task_id,
                "remote_session_id": run.remote_session_id,
                "remote_agent_id": run.remote_agent_id,
            })
        self.trajectory_archiver.archive(run.run_id, run.remote_agent_id, run.remote_session_id)
    except Exception:
        logger.warning("trajectory archiver raised; capture skipped", exc_info=True)
```

`hasattr` 守护,因为 `TrajectoryArchivePort` 协议未变,旧 mock / 替代实现可不实现 `set_run_context`。

### 2.4 `simulate_serve/bootstrap.py` — `ApplicationServices` + `build_application` + `close()`

```python
from dataclasses import dataclass, field

@dataclass
class ApplicationServices:
    config: AppConfig
    task_manager: TaskManager
    repository: JsonRunRepository
    registry: ToolRegistry
    executor: AsyncQwenPawExecutor
    batch_runner: BatchRunner
    readiness_gaps: dict[str, tuple[str, ...]] = field(default_factory=dict)
    langfuse: Langfuse | None = None   # NEW; None = disabled

    async def close(self) -> None:
        await self.executor.close()
        await self.registry.close()
        # NEW: flush + clear Langfuse client (process-local singleton).
        try:
            from simulate_serve.observability.langfuse_client import shutdown as _lf_shutdown
            _lf_shutdown()
        except Exception:
            logger.warning("Langfuse shutdown raised; ignored", exc_info=True)
```

```python
# build_application 改动(约 line 154-175)
executor = AsyncQwenPawExecutor(config.agent_endpoint)
trajectory_archiver = None
if config.agent_endpoint.trajectory_capture_enabled:
    trajectory_archiver = QwenPawTrajectoryArchiver(
        config.output_dir,
        user_id=config.agent_endpoint.user_id,
        source_dir=config.agent_endpoint.trajectory_source_dir or None,
        langfuse_config=config.langfuse,   # NEW
    )
runtime = TaskRuntime(
    executor=executor,
    actor=actor,
    validator=validator,
    repository=repository,
    trajectory_archiver=trajectory_archiver,
)
# NEW: instantiate Langfuse client once (process-local singleton).
from simulate_serve.observability.langfuse_client import get_client
langfuse_client = (
    get_client(config.langfuse)
    if config.langfuse and getattr(config.langfuse, "enabled", False)
    else None
)
return ApplicationServices(
    config=config,
    task_manager=manager,
    repository=repository,
    registry=registry,
    executor=executor,
    batch_runner=BatchRunner(runtime),
    readiness_gaps=readiness_gaps,
    langfuse=langfuse_client,
)
```

### 2.5 `simulate_serve/config.py` — 新增 `LangfuseConfig` + `AppConfig.langfuse`

```python
from typing import Literal

class LangfuseConfig(StrictConfig):
    """Langfuse observability toggle for simulate_serve stage.
    Off by default — old batches / old tasks are zero-impact.
    """
    enabled: bool = False
    public_key: str = ""
    secret_key: str = ""
    base_url: str = "https://cloud.langfuse.com"
    environment: str = "dev"
    release: str = ""
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    flush_at: int = Field(default=512, ge=1)
    flush_interval: float = Field(default=5.0, gt=0)
    timeout: int = Field(default=10, gt=0)
    upload_payload: Literal["full", "summary", "none"] = "full"
    max_payload_bytes: int = Field(default=0, ge=0)
    max_block_payload_bytes: int = Field(default=0, ge=0)
    # 注:工厂 `_extract_langfuse_fields` 自行处理 enabled + public_key + secret_key 三联判,
    # 不依赖额外的 is_actionable 装饰器属性。`is_actionable` 在本方案中**不实现** —
    # 工厂 PR 1 已经覆盖该判断(详见 plan.md §4)。

class AppConfig(StrictConfig):
    model: ModelConfig = Field(default_factory=ModelConfig)
    agent_endpoint: AgentEndpointConfig = Field(default_factory=AgentEndpointConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)
    interaction: InteractionConfig = Field(default_factory=InteractionConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)   # NEW
    # ... 既有字段不变
```

`_parse_config_raw` 改动:从根 raw 顶层 `langfuse:` 段合并到 `AppConfig.langfuse`,**必须剥除 `stages:` 子段**(根共享段含阶段级开关,`AppConfig.LangfuseConfig` 是 StrictConfig extra_forbid,会拒绝 stages):

```python
def _parse_config_raw(raw: dict[str, Any]) -> dict[str, Any]:
    if "simulate_serve" not in raw:
        return raw
    section = dict(raw.get("simulate_serve") or {})
    # ... 既有 model / tasks_file / scenarios_file 处理不变
    # Root-level ``langfuse:`` section (shared with gdr / etl stages) becomes
    # the simulate_serve.langfuse sub-config when the latter is absent.
    # The shared ``stages:`` sub-key is consumed by the orchestration-side
    # loader (gdr / etl read it directly from root); it is NOT part of the
    # simulate_serve LangfuseConfig schema, so we drop it on the way down
    # to avoid AppConfig's extra_forbid rejection.
    if "langfuse" in raw and "langfuse" not in section:
        lf_root = dict(raw["langfuse"])
        lf_root.pop("stages", None)
        section["langfuse"] = lf_root
    return section
```

---

## 3. 边界契约(simulate_serve → gdr / etl)

simulate_serve 是 Langfuse 事件的**产出方**之一,gdr / etl 是**消费方**。本节定义本模块产出的 trace / span 结构契约,gdr / etl 端按此契约读取与对齐。

### 3.1 Trace 字段表

| Langfuse 字段 | 取值 | 必填 |
|---|---|---|
| `trace_name` | `simulate_serve:{task_id}` | ✓ |
| `session_id` | `run.remote_session_id` | ✓ |
| `user_id` | 同 `session_id` | ✓ |
| `tags` | `["stage:simulate_serve", "task:{task_id}"]` | ✓ |
| `metadata.stage` | `"simulate_serve"` | ✓ |
| `metadata.task_id` | 任务 ID | ✓ |
| `metadata.run_id` | `run_xxx` | ✓ |
| `metadata.session_id` | 同 Langfuse `session_id`(冗余便于过滤)| ✓ |
| `metadata.agent_id` | `run.remote_agent_id` | 可空 |
| `metadata.terminal_reached` | bool,是否等到终态事件 | ✓ |
| `metadata.last_event_type` | `final_reply` / `error` / `cancel` / `model_response` | ✓ |
| `metadata.trajectory_path` | 绝对路径字符串 | ✓ |

### 3.2 Payload 结构(`span.output`)

`span.output` 是完整 C1 trajectory dict(JSONL → list of events):

```python
[
    {"event_type": "model_response", "payload": {"content": [{"thinking": ...}, {"tool_call": ...}, {"text": ...}]}},
    {"event_type": "tool_execution", "metadata": {"end_state": "..."}, ...},
    {"event_type": "final_reply", "payload": {"content": [{"text": "..."}]}, "metadata": {"usage": {...}}},
    ...
]
```

异常兜底:`_output_payload` lambda 内 try/except,失败时返回 `{"_missing": True}` 或 `{"_read_error": "..."}`,**不**抛异常。

### 3.3 命名规范

- **trace_name**:`f"simulate_serve:{task_id}"`,snake_case
- **span name**:与 trace_name 一致(顶层 span 即 trace)
- **metadata keys**:snake_case,白名单:`stage / run_id / task_id / session_id / agent_id / terminal_reached / last_event_type / trajectory_path`
- **tags**:`["stage:simulate_serve", "task:{task_id}"]`

### 3.4 多轮覆盖语义(关键约定)

**同一 `session_id` 下会产生多个 trace**(每轮一次 `archive()` → 一次 `_emit_trail`)。gdr / etl 端按 `trace_name="simulate_serve:{task_id}"` 过滤后,**取最后一个 `output` 作为本阶段最终 C1 trajectory**(覆盖式累积语义)。`metadata.terminal_reached=true` 的 trace 是终态;`false` 可标 partial=true 旁路。

### 3.5 关闭 / 失败契约

- `langfuse.enabled=false` 或凭据缺失:所有 helper 是 no-op,**不**产生任何 Langfuse HTTP 请求
- `stage_trace` / `step_span` / `_emit_trail` 内部 try/except 兜底:任何异常仅 `logger.warning`,**不**抛、不影响原业务
- factory 在 PR 1 增加 `level="ERROR"` 行为(详见 §3.6 协商结论):业务异常时 `span.update(level="ERROR", status_message=...)`

### 3.6 边界协商(本模块与 gdr / etl)

#### 客户端工厂位置

**结论**:`simulate_serve/observability/langfuse_client.py`(本模块,已落地),gdr / etl 通过 `from simulate_serve.observability.langfuse_client import get_client, step_span, snapshot, _to_jsonable, _maybe_truncate, stage_trace, shutdown` 复用。**完整落地代码与签名以 [observability-langfuse-plan.md §4](observability-langfuse-plan.md) 为准**(PR 1 冻结版)。

**理由**:
1. CLAUDE.md 当前模块组织是 `simulate_serve / orchestration / etl / gdr` 四模块,顶级新建 `observability/` 引入新顶层 package 与既有组织冲突
2. simulate_serve 是发起者,工厂语义中心在 simulate_serve
3. gdr / etl import 路径清晰,Python 无障碍;将来"提取到顶级"是低成本重构,不在 PR 1-5 范围

#### 配置字段形态

| 模块 | 字段形态 | 入口 |
|---|---|---|
| simulate_serve | `AppConfig.langfuse: LangfuseConfig`(pydantic 嵌套,13 字段含 `max_block_payload_bytes`) | `simulate_serve/config.py` |
| gdr | `Settings.langfuse_enabled` / `langfuse_public_key` / ...(pydantic 扁平) | `gdr/config/settings.py` |
| etl | `LangfuseConfig`(frozen dataclass,14 字段含 `max_block_payload_bytes` + `per_step_span`) | `orchestration/observability/langfuse_config.py` |

**结论**:`get_client(config)` 用**鸭子类型**接受三种入参,内部通过 `_extract_langfuse_fields(cfg)` 抽取。**完整实现见 [observability-langfuse-plan.md §4](observability-langfuse-plan.md) 的 PR 1 落地版**,本节不再重复代码。

三方可独立选风格(嵌套 / 扁平 / dataclass),工厂兼容。

#### `_parse_config_raw` 剥除 `stages:` 子段(关键细节)

`AppConfig.LangfuseConfig` 是 `StrictConfig`(`extra="forbid"`),**不接受 `stages:` 子字段**。但根共享 `langfuse:` 段含 `stages:` 用于阶段级开关(`stages.simulate_serve.enabled` / `stages.gdr.per_step_span` / `stages.etl.per_step_span`),该字段是 gdr / etl 关心的,simulate_serve 不感知。

`_parse_config_raw` 在把根 `langfuse:` 段复制到 `simulate_serve.langfuse` 时必须 `lf_root.pop("stages", None)`,否则 `tests/unit/test_cli.py::test_validate_example_config_succeeds` 失败。

#### Outer trace 命名

**结论**:三阶段都 `{stage}:{task_id}` outer:
- `simulate_serve:{task_id}`(本模块)
- `gdr:{task_id}`(gdr `run_gdr_once` 外层)
- `etl:{task_id}`(etl `run_etl_once` 外层)

#### session_id 串联

**结论**:三阶段都用 `run.remote_session_id`(`useramulation-xxx`)作为 Langfuse `session_id`,跨进程 trace 链靠**字符串一致约定**关联(不强制父子 trace,Langfuse 端按 session_id 聚合)。

#### task_id tag 统一

**结论**:三阶段 outer trace tags 都加 `task:{task_id}`(本模块补齐;此前 simulate_serve 报告只写 `["stage:simulate_serve"]`,现统一为 `["stage:simulate_serve", "task:{task_id}"]`)。

#### 错误状态(level="ERROR")

**结论**:工厂 PR 1 增加行为:业务异常穿透 `step_span` / `stage_trace` 时,except 分支显式 `span.update(level="ERROR", status_message=f"{type(exc).__name__}: {exc}")`。本模块 `_emit_trail` 在 finally 块执行,业务异常已被外层 try/except 兜住,Lange 上传仍标 ERROR。

---

## 4. 配置文件改动清单

### 4.1 根 `config/config.yaml` 新增 `langfuse:` 段

实际 YAML 写法(详见 plan.md §3):

```yaml
# === Langfuse observability (simulate_serve / gdr / etl 三阶段共享) ===
# 默认 enabled=false, 旧批次零侵入。
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
  upload_payload: full       # full | summary | none
  max_payload_bytes: 0
  stages:
    simulate_serve:
      enabled: true
      export_trajectory_on_terminal: true
```

### 4.2 `simulate_serve/config.py` 新增 `LangfuseConfig`

详见 §2.5。`AppConfig.langfuse` 字段 + `_parse_config_raw` 改动。

### 4.3 `config/config.example.yaml` 模板同步

顶部新增 `langfuse:` 段,字段写法同 §4.1,`public_key` / `secret_key` 留空字符串提示符。

---

## 5. 新增 / 修改文件清单

### 新增

| 文件 | 行数 | 说明 |
|---|---|---|
| `simulate_serve/observability/__init__.py` | 5 | re-export:`from .langfuse_client import get_client, stage_trace, step_span, snapshot, shutdown` |
| `simulate_serve/observability/langfuse_client.py` | 200 | 完整 plan.md §4 + §3.6 `_extract_langfuse_fields` 兜底 |
| `tests/observability/__init__.py` | 1 | pytest 收集 |
| `tests/observability/test_simulate_serve_archiver.py` | 280 | archiver + 客户端工厂合约测试 |
| `tests/observability/test_langfuse_client_factory.py` | 120 | 客户端工厂 unit 测试 |

**总新增**:~605 行。

### 修改

| 文件 | 改动 |
|---|---|
| `pyproject.toml` | +3 行,`[project.optional-dependencies]` 加 `observability = ["langfuse>=3.0,<4.0"]` |
| `simulate_serve/config.py` | +60 行,`LangfuseConfig` + `AppConfig.langfuse` + `_parse_config_raw` 改动 |
| `simulate_serve/infrastructure/trajectory_archiver.py` | +95 行,`__init__` 改 + `set_run_context` + `_emit_trail` + `archive` finally |
| `simulate_serve/bootstrap.py` | +20 行,`ApplicationServices.langfuse` + `close()` + `build_application` |
| `simulate_serve/application/run_task.py` | +12 行,`_archive_trajectory` 内 `set_run_context` 注入 |
| `config/config.example.yaml` | +25 行 |
| `config/config.yaml`(本地 gitignored) | +25 行 |
| `CLAUDE.md` | +5 行,隐私段落澄清 |

**总修改**:~245 行。

### 不动

- `simulate_serve/application/ports.py` — `TrajectoryArchivePort` 协议不变
- `simulate_serve/__main__.py` — CLI 不动;`--check-tools` / `--readiness` 自然不触发 Langfuse
- `simulate_serve/infrastructure/json_run_repository.py` — 不动
- `shared_config.py` — 不动;`load_yaml_file` / `find_root_config` / `${VAR}` 占位符复用

---

## 6. 测试清单

新建 `tests/observability/` 子目录。Mock Langfuse SDK,CI 默认不连公网(CLAUDE.md 公网约束)。

### 单元测试 `tests/observability/test_langfuse_client_factory.py`

| 测试 | 验证 |
|---|---|
| `test_get_client_returns_none_when_disabled` | `enabled=False` 返 None,无请求 |
| `test_get_client_returns_none_when_no_keys` | 凭据空返 None |
| `test_get_client_init_failure_logs_warning` | Langfuse init 抛异常 → None + WARNING 日志 |
| `test_get_client_singleton_per_process` | 多次调用只 init 一次 |
| `test_get_client_accepts_duck_typed_settings` | 扁平 pydantic(`langfuse_enabled` 等字段)被正确抽取 |
| `test_get_client_accepts_nested_langfuse_config` | 嵌套 LangfuseConfig 被正确抽取 |
| `test_get_client_accepts_dataclass` | frozen dataclass 被正确抽取 |
| `test_stage_trace_yields_none_when_client_none` | fail-safe |
| `test_stage_trace_calls_propagate_attributes` | mock client 上 `propagate_attributes` 被正确调用 |
| `test_stage_trace_payload_full_uploads_input_output` | full 模式下 input/output 各调一次 |
| `test_stage_trace_marks_error_on_exception` | 业务异常 → `level="ERROR"` + `status_message`(factory PR 1 行为)|
| `test_stage_trace_failure_isolation` | 异常不影响业务返回 |
| `test_step_span_metadata_kwarg` | metadata 字段透传 |
| `test_snapshot_isolates_pydantic_model` | 深拷贝隔离 |
| `test_to_jsonable_handles_pydantic_model_dump` | pydantic → dict |
| `test_maybe_truncate_returns_obj_when_under_limit` | 不超上限原样返回 |
| `test_maybe_truncate_returns_placeholder_when_over` | 超上限返 `{"_truncated": True, ...}` |
| `test_shutdown_resets_singleton` | `_client` 重置 |

### 合约测试 `tests/observability/test_simulate_serve_archiver.py`

| 测试 | 验证 |
|---|---|
| `test_archive_emits_langfuse_trace` | mock 跑 archiver → propagate_attributes 含 remote_session_id,output 是 list of events |
| `test_archive_emits_metadata_run_and_task` | metadata 含 run_id / task_id / agent_id / stage |
| `test_archive_tags_include_task` | tags 含 `"task:{task_id}"` |
| `test_archive_does_not_emit_when_langfuse_disabled` | enabled=False 时零调用 |
| `test_archive_does_not_emit_when_task_id_missing` | 无 task_id 不发 trace |
| `test_archive_emit_trail_fail_safe` | mock client 抛异常业务不挂 |
| `test_archive_emits_with_terminal_reached_true` | source 含 `final_reply` → metadata terminal_reached=true |
| `test_archive_emits_with_terminal_reached_false` | 预算耗尽 → terminal_reached=false |
| `test_archive_payload_full_with_full_mode` | output 含完整 trajectory list |
| `test_archive_payload_summary_no_ping_with_summary_mode` | summary 模式仅 metadata |
| `test_archive_payload_none_no_data_with_none_mode` | none 模式 input/output 都不调 |
| `test_archive_multi_turn_emits_multiple_traces` | 同 session 跑 2 次 → 2 个 trace,session_id 一致 |
| `test_archive_set_run_context_empty_disables_emit` | run_ctx={} 不发 |
| `test_archive_long_terminal_wait_does_not_block_langfuse` | _copy_with_retry 慢,_emit_trail 在 finally 执行不重复阻塞 |

### 功能测试(可选,默认 skip)

`tests/observability/test_simulate_serve_integration.py`(标 `@pytest.mark.functional`,无 `LANGFUSE_TEST_PUBLIC_KEY` 时 skip):

```bash
LANGFUSE_TEST_PUBLIC_KEY=pk-test LANGFUSE_TEST_SECRET_KEY=sk-test \
uv run pytest -m functional tests/observability
```

---

## 7. 实施步骤(commit 级)

假设分支 `feat/simulate-serve-langfuse`。每个 commit 单测绿。

### Commit 1:依赖 + 配置字段(infra-only)

- `pyproject.toml` — 新增 `observability` extra
- `simulate_serve/config.py` — 新增 `LangfuseConfig` + `AppConfig.langfuse` + `_parse_config_raw` 改动
- `config/config.example.yaml` — 新增 `langfuse:` 段
- `config/config.yaml`(本地,gitignored)— 同步

commit message:`feat(simulate_serve): add LangfuseConfig schema and observability optional-dep`

### Commit 2:客户端工厂(独立 module)

- `simulate_serve/observability/__init__.py`(新建)
- `simulate_serve/observability/langfuse_client.py`(新建,含 `_extract_langfuse_fields` 鸭子类型兼容)
- **factory 包含 `level="ERROR"` 行为**(详见 §3.6 协商结论,影响三阶段)

commit message:`feat(simulate_serve): add langfuse_client factory (fail-safe, duck-typed, level=ERROR)`

### Commit 3:archiver `_emit_trail` hook

- `simulate_serve/infrastructure/trajectory_archiver.py` — `__init__` + `set_run_context` + `_emit_trail` + `archive` finally

commit message:`feat(simulate_serve): emit Langfuse trail from trajectory_archiver (per-turn)`

### Commit 4:bootstrap + run_task 接入

- `simulate_serve/bootstrap.py` — `ApplicationServices.langfuse` + `close()` + `build_application`
- `simulate_serve/application/run_task.py` — `_archive_trajectory` 内 `set_run_context`

commit message:`feat(simulate_serve): wire Langfuse client into bootstrap and TaskRuntime`

### Commit 5:测试 + 文档

- `tests/observability/__init__.py` + `test_langfuse_client_factory.py` + `test_simulate_serve_archiver.py`
- `CLAUDE.md` — 隐私段落澄清 +5 行
- `docs/observability-langfuse.md`(用户视角)

commit message:`test(simulate_serve): cover Langfuse client + archiver trail emission; docs`

**总改动**:~850 行(含测试)。

---

## 8. 风险与回退开关

| 风险 | 影响 | 缓解 |
|---|---|---|
| 每 turn 多 trace 对 Langfuse 配额 | 5 轮 × 58 task ≈ 290 trace | `upload_payload: summary/none` 或 `sample_rate: 0.1` 或 `stages.simulate_serve.enabled: false` |
| `run_ctx` 注入失败 | 旧 mock archiver 无 `set_run_context` | `hasattr` 守护(§2.3)|
| `_copy_with_retry` 60s 等待 vs Langfuse 上传同步阻塞 | Langfuse 网络慢理论增加 ≤10s | finally 块执行,Langfuse SDK 默认 timeout=10s;`upload_payload=none` 时 <100ms |
| Langfuse SDK 凭据缺失启动失败 | SDK 抛 ValueError | `get_client` 全 try/except 兜底 |
| Pydantic model 序列化兼容性 | trajectory 内含非标准对象 | `_to_jsonable` 走 `json.dumps(default=str)` 兜底 |
| 多进程 fork 后 client 失效 | 子进程 socket / 线程锁失效 | factory per-process singleton + 子进程 `get_client` 触发重 init |

回退:`langfuse.enabled=false` 一键关闭整阶段,业务零侵入。

---

## 9. 验证脚本

```bash
# 1) Pydantic schema 可加载 + 默认 enabled=false
uv run python -c "
from simulate_serve.config import LangfuseConfig
cfg = LangfuseConfig()
assert cfg.enabled is False
print('LangfuseConfig default OK:', cfg.model_dump())
"

# 2) 根 config.yaml 可加载
uv run python -c "
from simulate_serve.config import load_config
cfg = load_config()
assert hasattr(cfg, 'langfuse')
print('AppConfig.langfuse:', cfg.langfuse.enabled)
"

# 3) CLI --validate-config 不报错
uv run python -m simulate_serve --validate-config

# 4) enabled=false 时 get_client 返 None
uv run python -c "
from simulate_serve.observability.langfuse_client import get_client
from simulate_serve.config import LangfuseConfig
assert get_client(LangfuseConfig(enabled=False)) is None
print('disabled: get_client returned None OK')
"

# 5) snapshot / _to_jsonable / _maybe_truncate 单元行为
uv run python -c "
from simulate_serve.observability.langfuse_client import snapshot, _to_jsonable, _maybe_truncate
m = {'a': 1, 'b': [1, 2]}
s = snapshot(m); s['b'].append(3)
assert m['b'] == [1, 2] and s['b'] == [1, 2, 3]
print('snapshot OK')
truncated = _maybe_truncate({'k': 'v' * 100}, 50)
assert truncated.get('_truncated') is True
print('_maybe_truncate OK')
"

# 6) grep 自检
grep -n "langfuse_config" simulate_serve/infrastructure/trajectory_archiver.py
grep -n "_emit_trail" simulate_serve/infrastructure/trajectory_archiver.py
grep -n "set_run_context" simulate_serve/application/run_task.py
grep -n "from simulate_serve.observability" simulate_serve/bootstrap.py

# 7) 测试
uv run pytest tests/observability/ -v

# 8) 既有测试不退化
uv run pytest tests/unit/test_trajectory_archiver.py tests/unit/test_bootstrap.py -v

# 9) 端到端 dry-run(可选)
uv run python -m orchestration start --tasks T001 --dry-run 2>&1 | grep -i "langfuse"
```

---

## 文档元信息

- **配套基线**:`docs/observability-langfuse-plan.md`(2026-09-23 定稿)
- **本方案覆盖**:plan.md §5.1 simulate_serve 接入点工程细化 + §11 PR 2 拆分到 5 个 commit
- **客户端工厂位置决策**:§3.6(本模块下,被三阶段 import)
- **配置字段形态**:§3.6 鸭子类型兼容(嵌套 + 扁平 + dataclass)
- **未覆盖**(plan.md 其他 PR 范围):PR 3-5(gdr / etl)各自独立,详见 [docs/langfuse-gdr.md](langfuse-gdr.md) · [docs/langfuse-etl.md](langfuse-etl.md)