# Langfuse 集成 — gdr 模块详细实施方案

> 配套基线:[docs/observability-langfuse-plan.md](observability-langfuse-plan.md)
> 配套模块:[docs/langfuse-simulate-server.md](langfuse-simulate-server.md) · [docs/langfuse-etl.md](langfuse-etl.md)
> 边界协商:见 §3.6

---

## 1. 模块概述

gdr 是 `uv` workspace 成员(`pyproject.toml` `[tool.uv.workspace]`),按 21 步流水线把 C1 trajectory 精修为 C2 refined Session。每条 session 走 `process_one`(`gdr/pipeline/runner.py:275-526`),按序经历:

- 硬过滤 / 轻量健康分(零 LLM)
- 上下文理解构建 / 会话级折叠(零 LLM)
- **重试循环 LLM 判剪枝**(`retry_loop_clip`,LLM)
- **增量状态追踪**(`retrack_state`,LLM)
- 启发式 user_intent(零 LLM)
- **Router.tag**(规则层 + LLM 投票层 fan-out,内含 `ThreadPoolExecutor`)
- policy 决策(零 LLM,串行)
- **refiner 块级精修**(`_run_repairs`,内嵌 `ThreadPoolExecutor` 块间并发,`llm_concurrency=4`)
- L1 / L2 / L3 验证(L3 LLM)
- **`reassemble`**(含 `extract_user_intent_llm`、一致性校验 fan-out、L3 end-to-end judge 共 3 处 LLM)
- 落盘

gdr 在 orchestrator 中由 `orchestration/workers/gdr_worker.py::run_gdr_once`(行 71-199)经 `orchestration.task_pipeline._safe_run_gdr`(81-130)反复调起;它本身又通过 `_process_one_file`(1054-1122)与多进程 Pool(`_worker_init` 1126-1130,`mp.get_context("spawn")`)协作。Worker 子进程必须 fork-safe 重 init Langfuse 客户端。

**与 etl 的边界**:gdr 写 C2 单文件 `output/refined/<TXXX>__<session_id>.json`(`gdr.domain.schema.save_refined_session`),etl 从该文件重加载;Langfuse 观察链由同一 `Session.session_id` 字符串串联,不强制父子 trace。

---

## 2. 接入点逐个详述

### 2.1 `gdr/observability/runner_helpers.py`(新增)

```python
# gdr/observability/runner_helpers.py
from __future__ import annotations
import threading
from contextlib import contextmanager
from typing import Any

from simulate_serve.observability.langfuse_client import (
    get_client, step_span, snapshot,
)

_TLS = threading.local()


def set_current_task_id(task_id: str | None) -> None:
    """由 gdr_worker.run_gdr_once 进入时设置,退出时清空。"""
    _TLS.task_id = task_id


def _current_task_id() -> str | None:
    return getattr(_TLS, "task_id", None)


def _payload_mode(cfg) -> str:
    """gdr 端优先读 langfuse_upload_payload(扁平);回退 cfg.upload_payload(嵌套);再回退 'full'.

    注:工厂 `_extract_langfuse_fields` 已经处理扁平→嵌套双向抽取,
    本 helper 仅做"gdr Settings 扁平字段优先 + 兜底"语义。
    """
    v = getattr(cfg, "langfuse_upload_payload", None)
    if v is not None:
        return v
    return getattr(cfg, "upload_payload", None) or "full"


def _per_step_enabled(cfg) -> bool:
    return bool(getattr(cfg, "langfuse_enabled", False))         and bool(getattr(cfg, "langfuse_gdr_per_step_span", True))


def _max_block_payload_bytes(cfg) -> int:
    return int(getattr(cfg, "langfuse_max_block_payload_bytes", 0))


@contextmanager
def _gdr_step_span_ctx(name: str, session, **metadata):
    """21 步骤模板。进入时 snapshot(session) → input;退出时 output_capture 拿当前 session → output."""
    cfg = getattr(session, "_gdr_cfg", None)
    if cfg is None or not _per_step_enabled(cfg):
        yield None
        return
    client = get_client(cfg)
    if client is None:
        yield None
        return
    payload_mode = _payload_mode(cfg)
    before = snapshot(session)
    with step_span(
        client,
        name=name,
        payload_mode=payload_mode,
        input_data=before,
        output_capture=lambda: session,
        session_id=getattr(session, "session_id", "") or "",
        task_id=_current_task_id(),
        max_payload_bytes=_max_block_payload_bytes(cfg),
        metadata=metadata,
    ) as span:
        yield span
```

### 2.2 21 步接入点

| # | runner.py 行 | 步骤 | span 名 |
|---|---|---|---|
| 0 | 283 | `_hard_filter_session` | `gdr.hard_filter` |
| 1 | 288 | `light_health_score_for_session` | `gdr.light_health` |
| 2 | 292 | `build_context_for_session` | `gdr.context_understanding.build` |
| 3 | 303 | `fold_failed_toolresults` | `gdr.fold.failed_toolresults` |
| 4 | 307 | `fold_repeated_thinking` | `gdr.fold.repeated_thinking` |
| 5 | 315 | `clip_session` | `gdr.retry_loop_clip` + 子 `generation` |
| 6 | 339 | `retrack_state` | `gdr.cu.retrack_state` + 子 `generation` |
| 7 | 354 | `heuristic_user_intent` | `gdr.user_intent.heuristic` |
| 8 | 362 | `router.tag` | `gdr.router.tag` |
| 9 | 404-466 | 决策层 `for` 循环 | `gdr.policy.decide` |
| 10 | 469 | `_run_repairs` | `gdr.refine.run_repairs`(外层,内层并发不嵌套) |
| 11 | 215-240 | `validate_block` | 不单起 span,由外层 `gdr.refine.run_repairs` 覆盖 |
| 12 | 474-485 | 早退分支 | `gdr.early_exit` |
| 13 | 488 | `reassemble` | `gdr.reassemble` + 内嵌 3 个 `generation` 子 span |
| 14 | 499-516 | 超时 fallback | `gdr.timeout_fallback` |
| 15 | 524-526 | 顶层异常 | `gdr.unhandled_error` |
| 16 | 1079 | `_append_routing_abstain_queue` | `gdr.audit.routing_abstain` |
| 17 | 1086-1100 | `_detect_incomplete_session` | `gdr.incomplete_check` |
| 18 | 1102 | `save_refined_session` | `gdr.save_refined_session`(IO,只 metadata)|
| 19 | 1105 | `_append_deferred_queue` | `gdr.audit.deferred` |
| 20 | 1120 | `_append_judge_low_queue` | `gdr.audit.judge_low` |

调用样例:

```python
# 步骤 5:retry_loop_clip
with _gdr_step_span_ctx("gdr.retry_loop_clip", session,
                        metadata={"tool": "retry_loop_clip"}):
    removed_clip = clip_session(session, clip_client, ...)

# 步骤 13:reassemble(嵌 3 generation 子 span 在 reassembler 内)
with _gdr_step_span_ctx("gdr.reassemble", session,
                        metadata={"refine_records_count": len(refine_records)}):
    result = reassemble(session, refine_records, health_scores, cfg,
                        policy_decisions=policy_decisions,
                        prune_block_ids=prune_block_ids,
                        deferred_block_ids=deferred_block_ids,
                        cu=context_understanding)
```

#### `_run_repairs` 与 span 并发

`_run_repairs`(`runner.py:243-272`)内 `ThreadPoolExecutor` 不为每条 repair_item 单独起 span,只在外层起 `gdr.refine.run_repairs`,内层并发走 metadata 聚合。原因:

- Langfuse SDK `start_as_current_observation` 在多线程上下文可并发,但每条 repair_item 都起 span 会让 trace 树出现几千个 `gdr.refine.execute_repair_item:*` 子节点
- refine_records 已通过 `save_refined_session` 落盘,metadata 聚合 `success_count / failure_count` 即可

高级用户可开 `langfuse_gdr_per_refine_span=true`(默认 false),逐条声明支持。

#### `reassemble` 内嵌 3 个 generation 子 span

| 子 span 名 | 触发位置 | metadata |
|---|---|---|
| `gdr.reassemble.user_intent_llm` | `reassembler.py:884-885` `extract_user_intent_llm(session, cfg)` | `{"tool": "user_intent_extract", "max_chars": cfg.user_intent_max_chars}` |
| `gdr.reassemble.consistency_check` | `reassembler.py:172-265` `_validate_edit_consistency(...)` | `{"tool": "edit_consistency", "lost_fields_count": ...}` |
| `gdr.reassemble.l3_judge` | `reassembler.py:911-925` `client.chat(...)` | `{"tool": "l3_judge", "judge_model": cfg.judge_model, "judge_score": ...}` |

包法:`gdr.reassemble` 父 span 内嵌 3 个 `step_span(..., as_type="generation")` 子 span。**嵌套 vs 平级决策**:**嵌套**(父子树,UI 折叠友好,树深 4 层远低于 Langfuse 32 层上限)。

#### `Router.tag` / `_llm_layer` 投票层 span 策略

- `Router.tag`(`router.py:665-826`)外层起 `gdr.router.tag` span,metadata `{"candidate_blocks": ...}` 记并发度
- `_llm_layer`(`router.py:384-453`)的 `ThreadPoolExecutor` 不起 span;投票失败退入 `abstentions` 列表

### 2.3 `gdr/pipeline/runner.py` 外层 + save

`_process_one_file`(1054-1122)首行包 `stage_trace` outer span:

```python
def _process_one_file(input_path: Path, output_path: Path, cfg: Settings) -> dict:
    client = get_client(cfg)
    with stage_trace(
        client,
        session_id="",  # 待 from_trajectory 完成后填
        name="gdr.process_one",
        task_id=_current_task_id(),
        tags=["stage:gdr"],
        metadata={"input_path": str(input_path)},
        input_data=None,    # C1 trajectory 太大,simulate_serve 已传过
        output_capture=lambda: {"output": str(output), "status": "success"} if 'output' in locals() else None,
        payload_mode=_payload_mode(cfg),
    ):
        log.info("loading trajectory from %s", input_path)
        try:
            session = from_trajectory(input_path)
            session._gdr_cfg = cfg   # 绑定 cfg 给 helper 读
        except Exception as e:
            log.error("failed to load %s: %s", input_path, e)
            return {"input": str(input_path), "status": "load_error", "error": str(e)}
        # ... 原 process_one / save 逻辑不变
```

### 2.4 `gdr/infrastructure/llm_client.py`(`per_llm_span=true` 时)

仅当 `langfuse_gdr_per_llm_span=true` 时改 `LlamaCppClient.chat`(行 146-220)入口:

```python
def chat(self, ...):
    if self._cfg and getattr(self._cfg, "langfuse_gdr_per_llm_span", False):
        from gdr.observability.llm_hook import maybe_llm_span  # gdr 端自定义
        client = get_client(self._cfg)
        with maybe_llm_span(client, name=f"gdr.llm.{self._model}", model=self._model, ...):
            ...
    # 原有 chat 逻辑
```

**注**:`maybe_llm_span` 是 gdr 端自定义 helper,**不**在工厂 PR 1 范围。实施时新建 `gdr/observability/llm_hook.py`(见 §7 Commit 9),内部调用工厂 `step_span(..., as_type="generation", metadata={"model": ..., "usage": ...})` 即可。

默认 false,不改动;仅作可选 feature(Commit 9)。

### 2.5 `gdr_worker.run_gdr_once` 外层 span(唯一有 task_id 的入口)

```python
def run_gdr_once(*, src_path, refined_dir, gdr_settings, task_id, session_id):
    set_current_task_id(task_id)
    try:
        client = get_client(gdr_settings)
        with stage_trace(
            client,
            session_id=session_id,
            name=f"gdr:{task_id}",
            task_id=task_id,
            tags=["stage:gdr", f"task:{task_id}"],
            metadata={"src_path": str(src_path), "refined_dir": str(refined_dir)},
            input_data=None,
            output_capture=lambda: {"refined_path": str(refined_path), "status": status} if available else None,
            payload_mode=_payload_mode(gdr_settings),
        ):
            # ... 原逻辑
            return GdrResult(...)
    finally:
        set_current_task_id(None)
```

### 2.6 fork-safe client reinit(`_worker_init`)

`gdr/pipeline/runner.py::_worker_init`(行 1126-1130)末尾追加(实施时按现有真实签名插入,**不引入新参数**):

```python
def _worker_init(log_dir: Path, llm_concurrency: int) -> None:
    setup_logger(log_dir)
    set_generation_concurrency(llm_concurrency)
    log.info("worker pid=%d initialized", os.getpid())
    # Langfuse fork-safe: 强制清旧 singleton, 下次 get_client 时重建
    try:
        from simulate_serve.observability.langfuse_client import _reset_for_fork
        _reset_for_fork()
    except Exception:
        pass
```

`_worker_process_file`(行 1133,真实签名)加 try/finally flush — **按现有真实签名,不引入 cfg_dict**:

```python
def _worker_process_file(args: tuple) -> dict:
    input_path, output_path, cfg = args   # 真实签名:Settings 直接传,不序列化为 cfg_dict
    try:
        return _process_one_file(input_path, output_path, cfg)
    finally:
        try:
            from simulate_serve.observability.langfuse_client import get_client
            c = get_client(cfg)
            if c is not None:
                c.flush()
        except Exception:
            pass
```

**注**:跨进程传 `cfg: Settings` 走 multiprocessing 序列化(spawn 上下文),worker 子进程 `Settings(**cfg_dict)` 重建在 `_worker_process_file` 入口。**真实签名以 `gdr/pipeline/runner.py` 当前代码为准**,实施时同步调整。

---

## 3. 边界契约

### 3.1 从 simulate_serve 接收的契约

- gdr **不读** Langfuse 后端。simulate_serve `QwenPawTrajectoryArchiver._emit_trail` 通过 `stage_trace(...)` 已经上传 `simulate_serve:{task_id}` trace(详见 [langfuse-simulate-server.md §3.1](langfuse-simulate-server.md))
- **跨进程 trace 关联靠 session_id 字符串一致**。`Session.session_id`(`gdr/domain/schema.py:163`)就是 simulate_serve 端的 `run.remote_session_id`;本模块写入 Langfuse 时必须使用同一个串
- **依赖 Langfuse 后端 v3 的 session 聚合功能** — 若后端版本/项目不支持按 `session_id` 跨 trace 聚合,UI 上无法串联三阶段 trace,只能按 `metadata.task_id` / `metadata.stage` 单独过滤。这是约定,不是强制
- gdr 端**只通过 `from_trajectory`**(`gdr/parsers/from_trajectory` 读本地 `output/agent_trajectory/<run>__<session>.json`)关联 session_id,不解 Langfuse SDK

### 3.2 给 etl 的输出契约

`save_refined_session` 写出的 C2 文件是 etl 的唯一入口;Langfuse 观察链继续靠 session_id 字符串一致。本模块 Langfuse 上传必须满足:

- **trace_name 格式**:
  - 外层(`gdr_worker.run_gdr_once`):`gdr:{task_id}`
  - 子 span:`gdr.process_one` / `gdr.<step>` / `gdr.reassemble.<sub>`
- **session_id**:`Session.session_id`,与 simulate_serve / etl 完全一致
- **tags**:`["stage:gdr", "task:{task_id}"]`(顶层);子 span 继承
- **payload 模式**:full / summary / none 三态
- **多步上传语义**:
  - 父 trace span:`input=None` / `output={refined_path, status}`
  - 一级子 span(`gdr.process_one`):`input=C1 dict` / `output=C2 dict`(或 None)
  - 二级子 span(21 步):`input/output=snapshot(session)`
  - 三级子 span(`gdr.reassemble.l3_judge` 等):`as_type="generation"`
- **etl 端如何接续**:etl 的 `etl_worker.run_etl_once` 外层调用 `stage_trace(..., session_id=<同 session_id>, name=f"etl:{task_id}", tags=["stage:etl", "task:{task_id}"])`,input=C2 dict(`load_refined_session` 读回),output=C3 4 视图 dict。Langfuse UI 按 session_id 聚合三阶段 trace

### 3.3 重试 trace 策略

gdr 端**不重试**(`_safe_run_gdr` 失败由上层任务调度管理,`run_gdr_once` 每次都是单次完整调用),所以 outer trace 1 次/调用,与 etl 端的"N 次独立 trace"语义不同。etl 端在 `_safe_run_etl` 重试时 N 次独立 trace(详见 [langfuse-etl.md §2.6](langfuse-etl.md))。

### 3.4 metadata 白名单

| 字段 | 取值 | 来源 |
|---|---|---|
| `task_id` | `TXXX` | `task_pipeline.py:_run_one_task_pipeline` |
| `session_id` | `useramulation-xxx` | `Session.session_id` |
| `stage` | `"gdr"` | runner.py 顶层 |
| `src_path` | 输入 C1 路径 | `gdr_worker.run_gdr_once` |
| `refined_dir` | 输出 C2 目录 | 同上 |
| 步骤级 metadata | `candidate_blocks` / `vote_concurrency` / `clipped_segments` / `refine_records_count` / ... | 各步骤内 |

### 3.5 错误契约

- `level="ERROR"` + `status_message` 由 factory PR 1 行为统一处理(详见 §3.6)
- gdr 阶段特有:重试由上层处理,本模块不参与

### 3.6 边界协商(本模块与 simulate_serve / etl)

#### 客户端工厂位置

**结论**:`simulate_serve/observability/langfuse_client.py`,本模块 import 共享:

```python
from simulate_serve.observability.langfuse_client import (
    get_client, step_span, snapshot, _to_jsonable, _maybe_truncate, _extract_langfuse_fields,
)
```

详见 [langfuse-simulate-server.md §3.6](langfuse-simulate-server.md)。

#### 配置字段形态(扁平)

gdr 端**扁平字段** `langfuse_*`:

| gdr 字段 | 映射到工厂 |
|---|---|
| `langfuse_enabled` | `enabled` |
| `langfuse_public_key` | `public_key` |
| `langfuse_secret_key` | `secret_key` |
| `langfuse_base_url` | `base_url` |
| `langfuse_environment` | `environment` |
| `langfuse_release` | `release` |
| `langfuse_sample_rate` | `sample_rate` |
| `langfuse_flush_at` | `flush_at` |
| `langfuse_flush_interval` | `flush_interval` |
| `langfuse_timeout` | `timeout` |
| `langfuse_upload_payload` | `upload_payload` |
| `langfuse_max_block_payload_bytes` | `max_block_payload_bytes` |

`_extract_langfuse_fields(cfg)` 鸭子类型兼容(详见 [langfuse-simulate-server.md §3.6](langfuse-simulate-server.md)):优先嵌套,回退扁平 `langfuse_<key>`,默认兜底。

#### 决策:扁平 vs 嵌套

**结论**:**gdr 端扁平字段** `langfuse_*`。

**理由**:
1. 现有 `Settings` 全部字段扁平(`llm_base_url` / `main_model` 等),新增嵌套破坏 `env_prefix="GDR_"` 的扁平覆盖语义
2. `pydantic-settings` 双下划线嵌套(`GDR_LANGFUSE__ENABLED`)运维别扭
3. 工厂已通过 `_extract_langfuse_fields` 兼容嵌套,扁平不影响

#### Outer trace 命名

**结论**:`gdr:{task_id}`(顶层 trace,`gdr_worker.run_gdr_once` 外层)。

#### session_id 串联

**结论**:`Session.session_id == run.remote_session_id`,跨进程约定。

#### Worker fork-safe

**结论**:`gdr/pipeline/runner.py::_worker_init`(gdr 内部 Pool)末尾重置 `_client = None`。`orchestration/task_pipeline.py::_worker_init`(orchestration Pool)由 etl 模块改(互不冲突)。

---

## 4. 配置文件改动清单

### 4.1 根 `config/config.yaml` 新增 `gdr:` 段字段

```yaml
# === Langfuse 观测 (gdr 阶段开关, 扁平字段) ===
# 与根 langfuse.enabled 联动:两者均 true 才上传。
gdr:
  # ... 既有 gdr 字段 ...
  langfuse_enabled: false                       # 缺省 false
  langfuse_gdr_per_step_span: true
  langfuse_gdr_per_llm_span: false             # LlamaCppClient.chat 级
  langfuse_gdr_per_refine_span: false          # 每条 repair_item 一个 span
  langfuse_upload_payload: full                # 覆盖根 langfuse.upload_payload
  langfuse_max_block_payload_bytes: 0
```

### 4.2 `gdr/config/settings.py` 新增字段

```python
class Settings(BaseSettings):
    # ... 既有字段 ...
    # === Langfuse 可观测性 (扁平, 7 字段) ===
    langfuse_enabled: bool = False
    langfuse_gdr_per_step_span: bool = True
    langfuse_gdr_per_llm_span: bool = False
    langfuse_gdr_per_refine_span: bool = False  # 高级:每条 repair_item 一个 span(默认 false)
    langfuse_upload_payload: Literal["full", "summary", "none"] | None = None
    langfuse_max_payload_bytes: int = 0
    langfuse_max_block_payload_bytes: int = 0
```

**字段覆盖来源优先级**:`init_settings > GDR_LANGFUSE_* env > 根配置 gdr.langfuse_* > 字段默认值`。

**注意**:由于 `Settings` 是 pydantic-settings,根配置 `gdr:` 段字段与 `Settings` 字段同名才被自动注入;`langfuse_*` 扁平字段意味着根配置里 `gdr.langfuse_enabled: true` 直接映射到 `Settings.langfuse_enabled`(不是 `Settings.langfuse.enabled`)。如果需要从根共享 `langfuse:` 段读(`langfuse.enabled` 等),由 `gdr/config/settings.py` 自定义 `init_settings` 加一层 `find_root_config + load_yaml_file` 抽取。

### 4.3 `pyproject.toml` 依赖

```toml
[project.optional-dependencies]
observability = ["langfuse>=3.0,<4.0"]
```

---

## 5. 新增 / 修改文件清单

### 新增

| 路径 | 行数 | 职责 |
|---|---|---|
| `gdr/observability/__init__.py` | 6 | 暴露 `_gdr_step_span_ctx` / `set_current_task_id` |
| `gdr/observability/runner_helpers.py` | ~140 | step_span 模板 + thread-local task_id |
| `gdr/observability/llm_hook.py`(可选) | ~30 | `maybe_llm_span` context manager |
| `tests/observability/__init__.py` | 1 | |
| `tests/observability/test_gdr_pipeline.py` | ~250 | 集成 + per-step 测试 |
| `tests/observability/test_gdr_deepcopy_isolation.py` | ~100 | 深拷贝隔离测试 |
| `tests/observability/test_gdr_observability_disabled.py` | ~50 | disabled 零开销 |

### 修改

| 路径 | 改动 |
|---|---|
| `pyproject.toml` | +3 行,observability extra |
| `gdr/config/settings.py` | +8 行,扁平字段 |
| `gdr/pipeline/runner.py` | +90 行,21 个 span + outer + _worker_init + _worker_process_file finally flush |
| `orchestration/workers/gdr_worker.py` | +30 行,run_gdr_once outer + set_current_task_id |
| `orchestration/task_pipeline.py` | +10 行,_worker_init 末尾 Langfuse reset(与 etl 模块协调,各自加) |
| `gdr/infrastructure/llm_client.py`(可选) | +10 行,per_llm_span hook |
| `config/config.example.yaml` | +25 行,gdr: 段扁平字段 + 根 langfuse: 段 |

---

## 6. 测试清单

### unit (`tests/observability/test_gdr_pipeline.py`)

| 测试名 | 验证 |
|---|---|
| `test_gdr_pipeline_per_step_spans` | mock 跑 `_process_one_file` 样本,断言 `len(_RECORDED) >= 21`,span `name` 在白名单,`input` 是 dict |
| `test_gdr_router_tag_metadata` | `gdr.router.tag` metadata 含 `candidate_blocks` / `vote_concurrency` |
| `test_gdr_reassemble_l3_judge_gen` | `gdr.reassemble.l3_judge` 是 `as_type="generation"`,metadata 含 `judge_score` |
| `test_gdr_save_refined_session_metadata_only` | `gdr.save_refined_session` 只 metadata `output_path`,无 payload |
| `test_gdr_deepcopy_isolation` | with 块内修改 session 不影响 input |
| `test_payload_modes_full_summary_none` | 三模式契约 |
| `test_per_llm_span_disabled_default` | `langfuse_gdr_per_llm_span=False` 时 generation span 计数=0 |
| `test_per_llm_span_enabled` | true 时 LlamaCppClient.chat 每调用产生 1 generation |
| `test_gdr_disable_no_langfuse_call` | `langfuse_enabled=False` 时 Langfuse() 构造 0 次 |

### contract

| 测试名 | 验证 |
|---|---|
| `test_gdr_span_naming_convention` | 所有 span name 命中正则 `^gdr(?:\:[A-Za-z0-9_]+)?(?:\.[a-z_]+)+$` |
| `test_gdr_trace_session_id_consistent` | `gdr.process_one` 与所有子 span 用同一 `session_id` |
| `test_gdr_tags_on_trace` | trace tags `["stage:gdr", "task:{task_id}"]` 严格匹配 |
| `test_gdr_payload_modes_against_schema` | full 模式 `input/output` 是 dict,keys 含 Session.pydantic 字段子集 |
| `test_gdr_block_payload_truncation` | `langfuse_max_block_payload_bytes=1024` 触发截断 |

### functional

| 测试名 | 验证 |
|---|---|
| `test_gdr_e2e_sample_trajectory` | 用 fixture `single_session.jsonl` 跑 `run_gdr_once`,断言 `[22, 25]` spans,`gdr.reassemble.l3_judge` 出现,`gdr.save_refined_session` metadata `output_path` 文件存在 |

### 加载/开销

| 测试名 | 验证 |
|---|---|
| `test_gdr_disabled_zero_overhead` | `langfuse_enabled=False` 时 `copy.deepcopy` 计数=0 |

---

## 7. 实施步骤(commit 级)

假设分支 `feat/gdr-langfuse`。每个 commit 单测绿。

### Commit 1:依赖 + 配置 schema + helpers

- `pyproject.toml` — `observability` extra
- `gdr/config/settings.py` — 6 个 `langfuse_*` 字段
- 新增 `gdr/observability/__init__.py` + `runner_helpers.py`
- `config/config.example.yaml` — gdr: 段扁平字段
- 测试 `tests/observability/test_gdr_observability_disabled.py`

commit message:`feat(gdr): add observability config schema + runner_helpers scaffold`

### Commit 2:`process_one` 外层 span

- `gdr/pipeline/runner.py::_process_one_file`(1054-1122)首行加 `with stage_trace(...) as outer:`

commit message:`feat(gdr): wire outer trace at _process_one_file`

### Commit 3-7:21 子步骤 hook(拆 5 个 commit)

| commit | 步骤 | runner.py 范围 |
|---|---|---|
| 3a | 0/1/2/3/4 (硬过滤 / light_health / build_context / folds)| 283-309 |
| 3b | 5/6 (retry_loop_clip + retrack_state,首次 LLM)| 315-346 |
| 3c | 7/8/9/10/11/12 (heuristic / router / policy / refine / early_exit)| 354-485 |
| 3d | 13/14/15 (reassemble 含 3 generation 子 span + timeout + 异常)| 488-526;`reassembler.py` 嵌 3 个 `step_span(..., as_type="generation")` |
| 3e | 16-20 (audit + save)| 1079-1122 |

每个 commit 配套一个 unit 测试。

### Commit 8:`gdr_worker.run_gdr_once` outer + `_worker_init`

- `orchestration/workers/gdr_worker.py::run_gdr_once`(71-199)外层 span + `set_current_task_id` 守护
- `gdr/pipeline/runner.py::_worker_init`(1126-1130)末尾 Langfuse reset
- `gdr/pipeline/runner.py::_worker_process_file`(1133)finally flush

commit message:`feat(gdr): wire gdr_worker outer trace + fork-safe client reinit`

### Commit 9:`per_llm_span` 模式(可选)

- 新增 `gdr/observability/llm_hook.py`
- `gdr/infrastructure/llm_client.py::LlamaCppClient.chat`(146-220)入口加 `maybe_llm_span`

commit message:`feat(gdr): optional per-llm-span hook in LlamaCppClient.chat`

### Commit 10:测试大综合 + 文档

- `tests/observability/test_gdr_pipeline.py` + `test_gdr_deepcopy_isolation.py`
- `docs/observability-langfuse.md`(用户视角)新增 §"gdr 接入点"

commit message:`test(observability): comprehensive gdr pipeline span tests + docs`

---

## 8. 风险与回退开关

| 风险 | 缓解 |
|---|---|
| 21 步 `snapshot(session)` `O(N)` × 21 | `langfuse_gdr_per_step_span=false` 或 `upload_payload=summary` 兜底 |
| ThreadPoolExecutor 内 span 命名冲突 | 默认不开 per-repair;开启时 metadata={"repair_item_idx": i} |
| `reassemble` 嵌 3 层 generation 的树深度 | 树深 4 层 < Langfuse 32 层上限 |
| `per_llm_span=true` 配额爆炸(100+ spans/session)| 默认 false;开启时 `sample_rate=0.1` |
| `payload_mode` 切换时旧 trace 是否兼容 | 旧 trace 仍为 full,新 trace summary,不强一致 |
| Worker fork 后 client 失效 | `_worker_init` 末尾 `_client = None` |
| IO span `save_refined_session` 与 etl 的 `save_session_v2` 命名冲突 | 不冲突,gdr 阶段 `save_refined_session` 单文件写,etl 阶段 `save_session_v2` 拆 4 视图 |

---

## 9. 验证脚本

```bash
# 单元 + 合约(无 Langfuse 网络)
uv run pytest -q tests/observability/

# 既有测试不退化
uv run pytest -q -x

# grep 检查 21 步骤都 hook
uv run python -c "
import pathlib
texts = pathlib.Path('gdr/pipeline/runner.py').read_text(encoding='utf-8')
required = [
    'gdr.hard_filter', 'gdr.light_health', 'gdr.context_understanding.build',
    'gdr.fold.failed_toolresults', 'gdr.fold.repeated_thinking',
    'gdr.retry_loop_clip', 'gdr.cu.retrack_state', 'gdr.user_intent.heuristic',
    'gdr.router.tag', 'gdr.policy.decide', 'gdr.refine.run_repairs',
    'gdr.early_exit', 'gdr.reassemble', 'gdr.timeout_fallback',
    'gdr.unhandled_error', 'gdr.audit.routing_abstain', 'gdr.incomplete_check',
    'gdr.save_refined_session', 'gdr.audit.deferred', 'gdr.audit.judge_low',
]
missing = [s for s in required if s not in texts]
print(f'Missing: {missing}' if missing else 'All 21 step spans wired.')
"

# 端到端(可选)
LANGFUSE_PUBLIC_KEY=mock LANGFUSE_SECRET_KEY=mock \
uv run pytest -q tests/observability/test_gdr_e2e_sample_trajectory.py

# 真实 Langfuse 后端
LANGFUSE_PUBLIC_KEY=<real> LANGFUSE_SECRET_KEY=<real> \
uv run python -m orchestration start --tasks T001 --dry-run
```

---

## 文档元信息

- **配套基线**:`docs/observability-langfuse-plan.md`(2026-09-23 定稿)
- **本方案覆盖**:plan.md §5.2 接入点工程细化 + §11 PR 3-4 拆分到 10 个 commit
- **客户端工厂位置**:§3.6(共享 `simulate_serve/observability/langfuse_client.py`)
- **配置字段形态**:§3.6 扁平字段 `langfuse_*`
- **未覆盖**:plan.md 其他 PR(simulate_serve / etl)各自独立,详见 [langfuse-simulate-server.md](langfuse-simulate-server.md) · [langfuse-etl.md](langfuse-etl.md)