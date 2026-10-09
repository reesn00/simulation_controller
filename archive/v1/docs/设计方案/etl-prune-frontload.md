# etl 修剪调整前移方案（处理提前 / 门控验证放后）

> ✅ **已实施**（2026-09-24）。Phase A→B→C→D 全绿：564→764 全测试绿。
> 实现位置：
> - gdr step 22 `prune_session_in_place`：[`gdr/refiners/usage_prune.py`](../../gdr/refiners/usage_prune.py) + [`gdr/refiners/system_prompt.py`](../../gdr/refiners/system_prompt.py)
> - gdr step 23 reject 门控：[`gdr/pipeline/runner.py`](../../gdr/pipeline/runner.py) `_append_scoring_reject_queue`
> - etl 兜底：[`etl/parsers/__init__.py`](../../etl/parsers/__init__.py) `gate_then_load` + `append_scoring_reject_fallback`
> - 配置：[`config/config.example.yaml`](../../config/config.example.yaml) `gdr.usage_prune_enabled` / `gdr.scoring_reject_audit_enabled` / `gdr.scoring_reject_output_path`
> - 测试：[`tests/unit/test_usage_prune_gdr.py`](../../tests/unit/test_usage_prune_gdr.py)（33 项）+ [`tests/unit/test_etl_gate.py`](../../tests/unit/test_etl_gate.py)（12 项）
> 场景：将 `etl/qwenformat/` 中属于"结构裁剪 / 隐私脱敏"性质的修剪调整前移到
> gdr 精修阶段，让 gdr 写出的 C2 refined Session **天然已是训练就绪形态**；
> 门控验证（两层评分）下沉到 C2 落盘前的最后一步生效，让评分结果真正
> 阻断不合格数据进入 refine_data。
> 配套文档：[`trajectory-scoring-two-layer.md`](trajectory-scoring-two-layer.md)
> （两层评分方法论）。本文档聚焦**前移矩阵 + 改动清单**，评分方法论
> 详见姊妹篇。

---

## 〇、结论先行

`etl/qwenformat/` 当前承担两类工作：**结构裁剪 + 隐私脱敏**（应前移到 gdr）
与 **内容精简 + 格式转换**（应保留在 etl）。两者混在 etl 导致三个具体问题：

| 问题 | 后果 |
|---|---|
| **跨阶段配置依赖** | `usage_prune.prune_tools` 已读 `gdr.config.settings.tools_prune_strategy`，配置在 gdr、实现在 etl |
| **隐私脱敏滞后** | 本机路径属个人数据，etl 才泛化意味着 C2 文件可能含原始路径，仅靠 etl 兜底不够 |
| **门控验证悬空** | 两层评分写到 metadata 但不阻止 C2 落盘，reject 形同虚设 |

**改造原则**：扩展现有 gdr pipeline（process_one step 22 新增 usage_prune），
不另起炉灶；etl 收窄为"格式转换 + 内容精简 + 4 视图拆分"。

---

## 一、当前 etl 修剪模块全貌

`etl/qwenformat/` 共 2749 行，6 个核心模块：

| 模块 | 行数 | 职责 | 关键函数 |
|---|---|---|---|
| `usage_prune.py` | 636 | session 级结构裁剪 | `collect_usage` / `prune_system_text` / `prune_tools` / `generalize_local_paths` / `prune_session_in_place` |
| `tool_output_summarizer.py` | 728 | tool result 内容精简 | `clean_l0` / `LLMAnchoredSummarizer` / `summarize_record` |
| `transform.py` | 330 | qf_text 重新渲染 | `trajectory_to_session_with_openai_metadata` |
| `system_prompt.py` | 296 | system prompt 段级拆解 | `partition_system_prompt` |
| `tool_templates.py` | 167 | tool schema 持久化 | `save_tool_templates` |
| `load.py` | 501 | C2 refined Session 解析 | — |

调用入口在 [`etl/writers/__init__.py:18`](../../etl/writers/__init__.py) `render_to_4_views`
（8 步调用链，骨架已就位但 `NotImplementedError`）。

---

## 二、前移评估矩阵

### 2.1 应当前移到 gdr

| 模块 | 前移理由 | 前移后位置 |
|---|---|---|
| `collect_usage` | called_tools 计算与 gdr `router.tag` / `policy.decide` / `reassembler` 是同一逻辑的两面；分散到 etl 会让"哪些 tool 被精修保留"与"哪些 tool 真实调用"形成双源真值 | `gdr/refiners/usage_prune.py::collect_usage`，与 router 同源 |
| `prune_system_text` | 依赖 partition；保留段原样拼接；逻辑上属"按真实调用裁剪 system"，与精修同阶段 | `gdr/refiners/usage_prune.py::prune_system_text` |
| `prune_tools` | **已依赖** `gdr/config/settings.py` 的 `tools_prune_strategy` / `_keep_unused_*` ——配置在 gdr、实现在 etl，跨阶段配置依赖违反单一职责 | `gdr/refiners/usage_prune.py::prune_tools` |
| `generalize_local_paths` | **关键前移**：路径属个人数据，CLAUDE.md 红线要求"不保存……个人隐私"，etl 才泛化意味着 C2 文件可能含原始路径 | `gdr/refiners/usage_prune.py::generalize_local_paths`，**与 F3-D meta_tag_strip 同级隐私脱敏** |
| `partition_system_prompt` | 是 `prune_system_text` 的前置依赖；纯函数无副作用 | `gdr/refiners/usage_prompt.py::partition_system_prompt`（从 etl 迁过来） |
| `prune_session_in_place` | 上述 5 项的编排入口；前移后 C2 天然是已精简 + 已脱敏 | `gdr/pipeline/runner.py::process_one` step 22 |

### 2.2 应当保留在 etl

| 模块 | 保留理由 |
|---|---|
| `tool_output_summarizer` | L1 摘要需要**全 session 锚点**（user_query + tool_input + assistant_response），gdr 中途处理时 session 还在精修，单 block 没有稳定上下文。LLM 摘要失败兜底"保留完整内容"是**降级策略**，与 gdr "精修 + 回滚"哲学不同。缓存层（`cache_dir`）适合 etl 这种 IO 密集阶段 |
| `transform.trajectory_to_session_with_openai_metadata` | 依赖 `chat_template`（etl 专属）；qf_text 是最终送入 LLM 的形态，是**纯渲染**而非内容修改 |
| `tool_templates.save_tool_templates` | 纯输出格式持久化；与 etl 拆 4 视图同语义 |
| `load.py`（etl/parsers） | etl 契约入口，gdr 已有 `from_trajectory` 加载 C1；前移会形成循环依赖 |
| `usage_prune.load_refined_session` / `write_refined_session` | 写入/读取拆分视图的 I/O 细节，留在 etl；读侧只读 gdr 已精简过的 session |

---

## 三、关键决策点：四要四不要

### 要前移（与 CLAUDE.md 边界一致）

1. **本机路径泛化必须前移**——C2 文件直接对应训练集输入，C2 含原始路径等于个人数据直接进训练管线。**CLAUDE.md 红线**要求"不保存……个人隐私"，路径属个人数据。
2. **tools 列表裁剪必须前移**——已存在 gdr → etl 跨阶段配置依赖（`tools_prune_strategy`），不前移违反单一职责。
3. **system prompt 段级裁剪必须前移**——逻辑上属"按真实调用精修 system prompt"，与 gdr router/policy/refine 同源。
4. **partition_system_prompt 必须随 prune_system_text 一起前移**——保持依赖就近。

### 不要前移（破坏 etl 边界）

1. **tool_output_summarizer 不要前移**——L1 摘要需要全 session 锚点 + 缓存层管理，gdr 不适合；保留完整内容的降级哲学与 gdr "精修回滚"哲学不同。
2. **transform (qf_text 渲染) 不要前移**——qf_text 是 chat_template 渲染产物，chat_template 是 etl 专属；qf_text 在 C2 阶段不需要存在，**仅在 C3 阶段作为 4 视图之一出现**。
3. **tool_templates 不要前移**——纯输出格式持久化。
4. **load.py 不要前移**——gdr 已有 `from_trajectory` 加载 C1，前移会形成 gdr → etl → gdr 循环依赖。

---

## 四、调整后的处理链

```
simulation server
  │
  ▼ (C1 trajectory)
┌──────────────────────────────┐
│ gdr                          │
│                              │
│ step -1~7  精修 (已有 21 步) │
│ step 21    两层评分 (已有)    │  ← 门控验证：trajectory_compare +
│                              │              free_quality，写 metadata
│ step 22 ★  usage_prune (新增)│  ← 处理提前：系统裁剪 + tools 裁剪 +
│                              │              本机路径泛化 + 工具输出 L0
│ step 23 ★  reject 门控 (新增)│  ← 独立式 reject → 不写 C2，
│                              │     转 audit/scoring_reject.jsonl
└──────────────────────────────┘
  │
  ▼ (C2 refined Session: 已脱敏 + 已精简系统/工具)
┌──────────────────────────────┐
│ etl                          │
│                              │
│ 入口: gate_then_load         │  ← 门控验证放后：读 metadata.trajectory_compare
│       (新增)                 │     compare_warn → meta.json 标 warn
│                              │     reject 兜底（理论上 gdr 已拦截）
│                              │
│ 1. transform (qf_text 渲染)  │  ← 纯格式转换
│ 2. tool_output_summarizer    │  ← L0 + L1 内容精简（保留完整内容兜底）
│ 3. save_session_v2 (4 视图)  │  ← 训练集最终形态
└──────────────────────────────┘
```

---

## 五、"门控验证放后"的实现路径

### 5.1 当前状态

[`gdr/pipeline/runner.py:648-680`](../../gdr/pipeline/runner.py#L648-L680) 把两层
评分写 metadata 但**不阻止 C2 落盘**——评分悬空。`scoring_reject=True` 仅是
metadata 标记，`save_refined_session` 不读它。

### 5.2 前移 + 门控放后的完整路径

**gdr 末尾**（process_one step 21-23）：

```python
# step 21: 两层评分（门控数据采集）
trajectory_compare_result = compare(original, refined, ...)
trajectory_free_result = evaluate(refined, ...)
metadata.trajectory_compare = compare_result.model_dump(...)
metadata.trajectory_free = free_result.model_dump(...)

# step 22: usage_prune（处理提前到落盘前）
from gdr.refiners.usage_prune import prune_session_in_place
prune_stats = prune_session_in_place(refined, cfg)
metadata.usage_prune = prune_stats

# step 23: 独立式 reject 门控（不写 C2）
if trajectory_free_result.decision == "reject":
    metadata.scoring_reject = True
    _append_scoring_reject_queue(refined, trajectory_free_result, cfg)
    return None  # 不返回 refined session → 不写 C2

# accept / resample → 正常 save_refined_session
save_refined_session(refined, output_path)
```

**etl 入口**（`etl/parsers/__init__.py` 增强）：

```python
def gate_then_load(c2_path: Path) -> Session | None:
    """读 C2 refined Session, 应用门控决策.

    Returns:
        Session: 通过门控的 session
        None: 应被 redirect 到 audit 旁路 (调用方处理)
    """
    session = load_refined_session(c2_path)
    meta = session.metadata or {}
    free = meta.get("trajectory_free") or {}

    # 独立式 reject → C2 不应到这里（gdr 已拦截），兜底
    if free.get("decision") == "reject":
        log.warning(f"unexpected scoring_reject in C2: {c2_path}")
        return None  # 调用方转 audit/scoring_reject.jsonl

    # 对比式 fail + 独立式 accept → 落 refine_data 但打 compare_warn
    compare = meta.get("trajectory_compare") or {}
    if compare.get("overall") == "fail" and free.get("decision") == "accept":
        meta["compare_warn"] = True
        meta["compare_diff_summary"] = (
            compare.get("instruction_adherence", {}).get("diff_summary", [])
        )

    return session
```

### 5.3 三类决策的实际落点

| 决策 | gdr 行为 | C2 是否存在 | etl 行为 | 最终位置 |
|---|---|---|---|---|
| `accept` | 写 metadata + save C2 | ✅ | 正常 transform + summarizer + 4 视图 | `output/refine_data/<T>__<session>_refined.{...}` |
| `resample` | 写 metadata + save C2 + 标 resample | ✅ | 正常处理但 meta.json 标 `resample=true` | 同上 + audit 标记 |
| `reject`（红线/低分） | **不写 C2**，转 audit 旁路 | ❌ | etl 看不到 | `output/audit/scoring_reject.jsonl` |
| `compare_fail`（仅对比式） | 写 metadata + save C2 | ✅ | 落 refine_data 但 meta.json 标 `compare_warn=true` | `output/refine_data/...` (带 warn) |

---

## 六、改动落地清单

### P0（必须前移）

| 路径 | 改动 |
|---|---|
| `gdr/refiners/usage_prune.py` | **新建**，把 etl `usage_prune.py` 的 `collect_usage` / `prune_system_text` / `prune_tools` / `generalize_local_paths` / `prune_session_in_place` 迁过来 |
| `gdr/refiners/system_prompt.py` | **新建**，从 etl `system_prompt.py` 迁 `partition_system_prompt` |
| `gdr/pipeline/runner.py` | step 22 插入 `prune_session_in_place`；step 23 reject 门控（reject → return None + 转 `audit/scoring_reject.jsonl`） |
| `etl/qwenformat/usage_prune.py` | 删减为只剩 `load_refined_session` / `write_refined_session`（4 视图 I/O），不含裁剪逻辑 |
| `etl/qwenformat/system_prompt.py` | 删除（已迁到 gdr） |

### P1（门控放后）

| 路径 | 改动 |
|---|---|
| `etl/parsers/__init__.py` | 新增 `gate_then_load` 读取 metadata 做准入门控 |
| `etl/writers/__init__.py` | `render_to_4_views` 在 `load_refined_session` 后调用 `gate_then_load` |
| `gdr/pipeline/runner.py` | `_append_scoring_reject_queue` 新增，与 `judge_low` / `incomplete` / `routing_abstain` 旁路同模式 |

### P2（边界收窄 + 测试）

| 路径 | 改动 |
|---|---|
| `gdr/config/settings.py` | 新增 `gdr.usage_prune` 配置 section（与 `gdr.scoring` 同级） |
| `config/config.example.yaml` | 新增 `gdr.usage_prune` 段 |
| `tests/unit/test_usage_prune_gdr.py` | 新增：路径泛化 / tools 裁剪 / system 段级裁剪 / 整体 in-place |
| `tests/unit/test_runner_scoring_gate.py` | 新增：reject 门控 → 不写 C2；resample 标 metadata |

---

## 七、关键收益

| 项 | 前移前 | 前移后 |
|---|---|---|
| C2 文件含本机路径 | ❌ 风险（etl 异常即泄漏） | ✅ 不可能（gdr 阶段已脱敏） |
| tools 列表跨阶段配置依赖 | ❌ gdr 配 / etl 用 | ✅ 同阶段 |
| 评分门控是否生效 | ❌ 仅写 metadata 不阻止 | ✅ 独立式 reject 不写 C2 |
| 对比式 fail 行为 | ❌ 无处理 | ✅ compare_warn 标记 + meta 留痕 |
| etl 职责 | 结构裁剪 + 格式整理 | 仅格式整理 + 4 视图拆分（更纯） |
| gdr 审计完整性 | metadata 无 usage_prune | metadata 含完整 usage_prune stats |

---

## 八、风险与缓解

| 风险 | 缓解 |
|---|---|
| usage_prune 前移后 gdr 处理时间增加（路径扫描 + tools 排序 + L0 清洗） | 增量处理：复用 `collect_usage` 已收集的 called_tools/tools，避免重复扫描；L0 清洗只对 toolresult 跑一次 |
| 路径泛化失败导致 session 异常 | 失败兜底：`_WIN_USER_ROOT_RE` 未匹配到任何路径时静默跳过，不阻断主流程 |
| 门控验证后置导致 etl 入口需读 metadata 复杂度上升 | 封装 `gate_then_load` 函数，与现有 `load_refined_session` 同签名；落 audit 走统一旁路模式 |
| etl 移除 usage_prune 后与历史 C2 文件不兼容 | C2 schema_version 标 `refined_session.v1`，etl 入口做版本判断；旧 C2 走 etl 兼容路径（已带 usage_prune 的不再做） |
| audit/scoring_reject.jsonl 数据增长 | 与 judge_low 同级，单独审计通道；批次报告 `_batch_report.json` 增加 scoring_reject 计数 |

---

## 九、与姊妹篇的衔接

本文档与 [`trajectory-scoring-two-layer.md`](trajectory-scoring-two-layer.md) 的衔接点：

| 姊妹篇章节 | 本文档对应 |
|---|---|
| 第二节 2.1 维度 2（instruction_adherence） | `compare_warn` 标记是 trajectory_compare.overall=fail 的处理出口 |
| 第二节 2.2 维度 1（红线合规） | `scoring_reject` 门控让 redline.violation 真正阻断 C2 落盘 |
| 第三节 管道接入点 step 5.5 / 6.5 | step 21-23 在 gdr 内的位置（已有 step 21 的两步评分 + 新增 step 22-23） |
| 第八节 与参考方案的差异说明 | 前移 etl 修剪是参考方案**未涉及**的项目化调整 |

---

## 十、验收标准

### 功能验收

- [ ] `gdr/refiners/usage_prune.py` 实现 `collect_usage` / `prune_system_text` / `prune_tools` / `generalize_local_paths` / `prune_session_in_place`，与 etl 版行为等价
- [ ] `gdr/refiners/system_prompt.py` 实现 `partition_system_prompt`，与 etl 版行为等价
- [ ] `gdr/pipeline/runner.py` step 22 插入 usage_prune 调用，结果写入 `metadata.usage_prune`
- [ ] `gdr/pipeline/runner.py` step 23 实现 reject 门控：`trajectory_free.decision == "reject"` → return None + 落 `audit/scoring_reject.jsonl`
- [ ] `etl/parsers/__init__.py` 新增 `gate_then_load`：reject 兜底 + compare_fail 标 warn
- [ ] `etl/writers/__init__.py` `render_to_4_views` 调用 `gate_then_load`，通过门控才进入 transform/summarizer
- [ ] `etl/qwenformat/usage_prune.py` 删减为只剩 4 视图 I/O
- [ ] `etl/qwenformat/system_prompt.py` 删除

### 隐私验收（CLAUDE.md 红线）

- [ ] C2 文件不包含 `C:\Users\<原始用户名>\...` 形态的路径
- [ ] C2 文件不包含 `<盘符>:\Users\<name>` 任意反斜杠变体（1/2/4 反斜杠）

### 性能验收

- [ ] gdr 处理单 session 时间增幅 < 200ms（P0-R fix 已有的 deterministic 采样 + 单次 scan，不重复劳动）
- [ ] etl 处理单 session 时间降幅 > 50ms（移除结构裁剪步骤）

### 测试验收

- [ ] `tests/unit/test_usage_prune_gdr.py` 覆盖：路径泛化（4 种反斜杠变体）/ tools 裁剪 / system 段级裁剪 / 整体 in-place
- [ ] `tests/unit/test_runner_scoring_gate.py` 覆盖：reject 门控 → 不写 C2；resample 标 metadata；accept 正常落 C2
- [ ] 回归：现有 566 全测试保持绿（轨迹评分 + quality_scorer + reassembler 测试无破坏）

---

## 十一、实施阶段（待用户决策）

| 阶段 | 内容 | 工作量 | 是否阻塞 |
|---|---|---|---|
| **阶段 A** | 落盘本文档（已完成） | 0 | — |
| **阶段 B** | 实施 P0：新建 `gdr/refiners/usage_prune.py` + `system_prompt.py`，从 etl 迁代码，runner 接入 step 22-23 | 2-3 天 | 阻塞后续阶段 |
| **阶段 C** | 实施 P1：`etl/parsers/gate_then_load` + `render_to_4_views` 接入 | 1 天 | 不阻塞 P0 验证 |
| **阶段 D** | 实施 P2：配置 + 测试 + 性能验收 | 1 天 | — |
| **阶段 E** | 历史 C2 文件迁移策略（schema_version 升级 + 兼容路径） | 0.5 天 | 与阶段 B/C 并行 |

P0 应优先实施——它解决"隐私脱敏在 etl 才发生"的根本风险（CLAUDE.md 红线）；
P1 让评分门控真正生效；P2 是配置和测试配套。

阶段 A（本文档）已完成。是否进入阶段 B？请确认。
