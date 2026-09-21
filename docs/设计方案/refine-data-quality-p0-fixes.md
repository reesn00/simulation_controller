# Refine Data 质量 P0 优化方案

> 依据 2026-09-21 对 `output/refine_data/*_refined.qwenjina.txt` 69 个文件的
> 结构化质量梳理结论制定。适用对象：gdr refine 落盘的 4 类后缀文件
> （`.qwenjina.txt` / `.messages.json` / `.openai.json` / `.meta.json`）。
> 本方案对应三项 P0 优化：① ⟦⟧ 全量剥离 + 模板下线；② 重试循环 LLM 判剪枝；
> ③ 429 切换启发式按反馈暂不做。

## 1. 现状与触发

### 1.1 现状盘点（69 个文件样本）

| 维度 | 数据 |
|---|---|
| 文件总数 / 总大小 | 69 / 6.23 MB |
| 格式合规 | 100%（ChatML `system / user / assistant ... ` 平衡，无截断） |
| 工具调用总量 / 函数种类 | 1160 / 8 种（browser 37.6%、web_fetch 25.3%、execute_shell_command 19.0%、web_search 15.3% 等） |
| 同函数连续重试循环 | 28 文件（40.6%），最严重单文件 36 次 web_search 死磕 |
| 含 ⟦⟧ 审计标签的文件 | 13（18.8%） |
| 仅 1 个 `<think>` 的文件 | 34（49.3%） |

### 1.2 三个 P0 问题

1. **`⟦...⟧` 标签污染 13 个文件**：外部 QwenPaw agent system prompt 强制要求每轮 reply 末尾追加 `⟦ 任务｜状态：... ⟧` headline。本仓库仅检测、不产生；下游 SFT 训练若直接使用会让模型学"末尾输出 ⟦⟧ 元注释"。
2. **同函数连续重试循环污染 28 个文件**：Tavily `web_search` 触发 429 后 agent 不切换，反复重试同一接口。规则层"高相似度"判定不覆盖 `query="功夫 Hustle 中文"` 与 `query="功夫 Hustle 正版"` 这种"同一意图 / 不同措辞"的真实重试场景。
3. **（暂不做）429 切换启发式**：system prompt 由外部仓库控制，本仓库只能改本地 retry 改造；按反馈本期不动。

## 2. 方案① ⟦⟧ 全量剥离 + system prompt 模板下线

### 2.1 目标

| 后缀文件 | 包含 ⟦⟧ 的字段 | 处理 |
|---|---|---|
| `.qwenjina.txt` | 全部 assistant text 块 | **剥离** |
| `.messages.json` | `messages[].blocks[].text` / `messages[].blocks[].content` | **剥离** |
| `.openai.json` | `messages[].content` (assistant 角色) | **剥离** |
| `.meta.json` | 不含 ⟦⟧ | **新增** `meta_tag_contamination` 标注字段（供观测） |

### 2.2 改动清单

| 改动点 | 文件 | 操作 |
|---|---|---|
| 删除模板 | `etl/qwenformat/templates/constraints/retrieval_headline.txt` | **删除文件** |
| 移除 boundary 分类 | `etl/qwenformat/system_prompt.py:53` | 从 `_SYSTEM_BOUNDARIES` 列表移除 `(r"^检索标题（RETRIEVAL HEADLINE）", "constraint", "RETRIEVAL HEADLINE")` |
| 清理检测注释 | `etl/qwenformat/usage_prune.py:8` | 文档注释更新（描述 RETRIEVAL HEADLINE 段已下线） |
| 清理 keep 逻辑 | `etl/qwenformat/usage_prune.py:91-92` | 移除 RETRIEVAL HEADLINE 分支；`_HEADLINE_RE` 保留供 `collect_usage` 用 |
| 新增剥离模块 | `gdr/refiners/meta_tag_strip.py` | **新增** `strip_meta_tags(text)` + `annotate_meta_tags(*payloads)` |
| 改造 save_session | `gdr/domain/schema.py:226` | 4 个后缀文件全部走剥离；meta.json 额外加 `meta_tag_contamination` 字段 |

### 2.3 新增 `gdr/refiners/meta_tag_strip.py`

```python
"""剥离 SFT 训练数据中的 ⟦...⟧ 审计摘要标签.

该标签由 QwenPaw agent system prompt 强制输出, 不属于任务答复本体.
本仓库不修改上游配置, 而在 save_session 落盘前对所有 4 类导出文件做剥离.
"""
import re
from typing import Any, Iterable

# 容忍多行, 容忍中文标点/竖线分隔符, 不贪婪
_META_TAG_RE = re.compile(r"⟦[^⟧]{0,500}⟧", re.MULTILINE)


def strip_meta_tags(text: str) -> str:
    """从纯文本剥离 ⟦⟧ 块, 并清理多余空行."""
    if not text or "⟦" not in text:
        return text
    cleaned = _META_TAG_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.rstrip() + "\n" if cleaned.endswith("\n\n") else cleaned


def annotate_meta_tags(*payloads: Iterable[Any]) -> dict[str, Any]:
    """扫描多个 payload, 收集 ⟦⟧ 出现位置; 不修改任何内容."""
    occurrences: list[dict[str, Any]] = []
    _scan(payloads, "", occurrences)
    return {
        "has_meta_tag": bool(occurrences),
        "total_count": len(occurrences),
        "occurrences": [{"path": p["path"], "tag": p["tag"], "char_offset": p["off"]}
                        for p in occurrences],
    }


def _scan(obj: Any, path: str, out: list[dict]) -> None:
    if isinstance(obj, str):
        for m in _META_TAG_RE.finditer(obj):
            out.append({"path": path, "tag": m.group(0), "off": m.start()})
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _scan(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _scan(v, f"{path}[{i}]", out)
```

### 2.4 `save_session` 改造伪代码

```python
def save_session(session: Session, base_path: Path) -> list[Path]:
    from gdr.refiners.meta_tag_strip import strip_meta_tags, annotate_meta_tags

    paths: list[Path] = []

    # 1. messages.json
    msgs_dict = _strip_session_dict(session.to_messages_dict())
    paths.append(_write_json(base_path.with_suffix(".messages.json"), msgs_dict))

    # 2. openai.json
    openai_dict = _strip_session_dict(session.to_openai_dict())
    paths.append(_write_json(base_path.with_suffix(".openai.json"), openai_dict))

    # 3. qwenjina.txt
    qf_text = strip_meta_tags(render_qf_text(session))
    paths.append(_write_text(base_path.with_suffix(".qwenjina.txt"), qf_text))

    # 4. meta.json (不剥离, 但写入标注)
    meta_payload = session.to_meta_dict()
    meta_payload["meta_tag_contamination"] = annotate_meta_tags(
        msgs_dict, openai_dict, qf_text, session.to_session_dict(),
    )
    paths.append(_write_json(base_path.with_suffix(".meta.json"), meta_payload))

    return paths


def _strip_session_dict(obj: Any) -> Any:
    """递归剥离 dict/list 中所有 string 字段的 ⟦⟧."""
    if isinstance(obj, str):
        return strip_meta_tags(obj)
    if isinstance(obj, dict):
        return {k: _strip_session_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_strip_session_dict(v) for v in obj]
    return obj
```

### 2.5 测试要点

`gdr/tests/test_meta_tag_strip.py`：

| 测试名 | 断言 |
|---|---|
| `test_strip_basic` | `⟦ 任务｜已完成 ⟧` 被剥离，前后文本保留 |
| `test_strip_multi_tag` | 同一文本多 ⟦⟧ 全部剥离 |
| `test_strip_no_tag_unchanged` | 无 ⟦⟧ 输入原样返回 |
| `test_strip_collapses_blank_lines` | 剥离后 3+ 空行收敛为 2 |
| `test_annotate_finds_paths` | `annotate_meta_tags({"k": "x⟦...⟧"})` 返回 `path=k, has_meta_tag=True` |
| `test_save_session_strips_all_suffix_files` | 4 类后缀文件均无 ⟦⟧ 残留 + `meta.json.meta_tag_contamination.has_meta_tag=True` |
| `test_save_session_clean_session_no_annotation` | 干净 session 写出 `has_meta_tag=False` |
| `test_regression_on_13_known_contaminated_files` | 回放 13 个已知 ⟦⟧ 文件，全部通过剥离 |

### 2.6 既有测试更新

依赖 RETRIEVAL HEADLINE section 的测试需同步改造：

| 测试文件 | 行 | 改动 |
|---|---|---|
| `tests/orchestration/test_system_prompt.py` | 32,60,78,93,196,206,251 | 断言改为 cleaned system **不**含 RETRIEVAL HEADLINE |
| `tests/orchestration/test_qf_worker.py` | 103,269 | 删除 fixture 中的该 section |
| `tests/orchestration/test_usage_prune.py` | 35,73,100,112,167,175,177 | 改为断言 `meta_tag_contamination` 字段 |
| `gdr/tests/test_runner_load_session.py` | 84,169,185 | 改为断言该 section 不出现 |
| `gdr/tests/test_complete_tail_detection.py` | 6,49,63,81,93,96 | **保留** ⟦⟧ 配对判定作为 structural close 兜底 |
| `gdr/tests/test_incomplete_session_detection.py` | 345,364,470 | 同上保留作为 fallback |

### 2.7 工作量：2 天

- 0.3 天 删模板 + 移除 boundary + 清理 usage_prune
- 0.4 天 `meta_tag_strip.py` + `annotate_meta_tags` + 递归剥离
- 0.4 天 `save_session` 改造（4 个文件全剥离）
- 0.4 天 单元 + 集成测试
- 0.5 天 13 个 ⟦⟧ 文件回归 + 既有测试更新

## 3. 方案② 重试循环 LLM 判剪枝

### 3.1 设计原则

| 维度 | 规则 |
|---|---|
| 触发条件（rule 预筛） | 连续 ≥ 5 次同函数调用，且每次结果都含 429 / rate_limited / timeout 标记 |
| LLM 输入 | 该连续段内每条的 (function_name, input_json 摘要, error_truncated) |
| LLM 判定 | 1. 是否"同一意图反复重试"（请求高度一致，差异仅在标点 / 同义词 / 重复字符）<br>2. 返回结果是否全部都是 429（不允许其它错误）<br>3. 给出 `keep_indices` — 最多 3 条，**至少 1 条含失败** |
| 剪枝 | 删除 keep_indices 之外的 toolcall + 配对 toolresult |
| 保守 fallback | LLM 调用失败 / 解析失败 / 字段缺失 → **整段保留**，不做任何剪枝 |

### 3.2 新增 `gdr/refiners/retry_loop_clip.py`

```python
"""基于 LLM 判断的同函数连续重试剪枝器.

⚠️ 触发严格: 仅 rule-based 预筛通过的连续同函数 429 段才进入 LLM 评估.
LLM 失败时保留原状不剪枝(保守 fallback).
"""
import json
import re
from typing import Any

from gdr.infrastructure.llm_client import LLMClient
from gdr.reassembly.reassembler import (
    _group_consecutive_same_function, ToolBlock, ToolResultBlock,
)

_RATE_LIMIT_MARKERS = ("429", "rate limit", "too many requests",
                       "rate_limited", "timeout")

_CLIP_PROMPT = """你是一名 SFT 训练数据质量评估员. 以下是一段连续 {count} 次的同函数调用记录, 全部返回 429 / 限流 / 超时错误.

任务: 判断这 {count} 次调用是否属于"同一意图反复重试". 仅在判断为真时, 才给出保留方案.

要求:
1. "同一意图反复重试" = 各次调用的输入参数语义高度一致, 差异仅为:
   - 标点 / 大小写 / 同义改写
   - 增加少量限定词 (如 "正版", "免费", "2024")
   - 重复字符 / 关键词顺序调整
   但核心搜索目标 / 输入 schema 不允许发生本质变化.

2. 如果判定为否 (例如用户主动换了搜索方向), 返回 {{"is_retry_loop": false, "reason": "..."}}, 不要给保留方案.

3. 如果判定为真, 给出 keep_indices (list[int], 从 0 开始的下标), 满足:
   - 长度 >= 1, <= 3
   - 至少 1 个是含失败结果的下标
   - 优先保留最早与最晚的几次

4. 必须返回合法 JSON.

【调用记录】:
{calls}

【输出 JSON 格式】:
{{"is_retry_loop": bool, "reason": str, "keep_indices": [int, ...] | null}}
"""


def _is_all_rate_limited(results: list[ToolResultBlock]) -> bool:
    if not results:
        return False
    for r in results:
        text = (r.error or "") + " " + (r.stdout or "")
        if not any(m in text.lower() for m in _RATE_LIMIT_MARKERS):
            return False
    return True


def _summarize_call(call: ToolBlock, result: ToolResultBlock) -> dict:
    return {
        "function": call.name,
        "input": (call.input or "")[:200],
        "error": (result.error or "")[:200],
        "state": result.state,
    }


async def llm_clip_retry_loop(
    blocks: list, llm_client: LLMClient,
    *, min_consecutive: int = 5, max_keep: int = 3,
) -> list[int] | None:
    runs = _group_consecutive_same_function(blocks)
    target_run = None
    for run in runs:
        if len(run) < min_consecutive:
            continue
        results = [r for _, r in run]
        if not _is_all_rate_limited(results):
            continue
        target_run = run
        break
    if target_run is None:
        return None

    calls_json = json.dumps(
        [_summarize_call(c, r) for c, r in target_run],
        ensure_ascii=False, indent=2,
    )
    prompt = _CLIP_PROMPT.format(count=len(target_run), calls=calls_json)

    try:
        raw = await llm_client.complete(prompt, max_tokens=300)
        parsed = json.loads(raw)
        if not parsed.get("is_retry_loop"):
            return None
        keep = parsed.get("keep_indices")
        if not isinstance(keep, list) or not keep:
            return None
        keep = [i for i in keep if 0 <= i < len(target_run)]
        if not keep:
            return None
        has_failure = any(
            target_run[i][1].state in ("rate_limited", "timeout", "network")
            for i in keep
        )
        if not has_failure:
            return None
        return keep[:max_keep]
    except Exception:
        return None  # 保守: LLM 出错不动数据


def apply_llm_clip(blocks: list, keep_indices: list[int]) -> list:
    if not keep_indices:
        return blocks
    keep_set = set(keep_indices)
    out = []
    call_idx = -1
    for b in blocks:
        if isinstance(b, ToolBlock):
            call_idx += 1
            if call_idx in keep_set:
                out.append(b)
        elif isinstance(b, ToolResultBlock):
            if call_idx in keep_set:
                out.append(b)
        else:
            out.append(b)
    return out
```

### 3.3 pipeline 接入位置

`gdr/pipeline/runner.py` 现有顺序：

```
hard-filter → light health → ContextUnderstanding →
fold_failed_toolresults → fold_repeated_thinking → retrack_state →
Router.tag → decision layer → _run_repairs → reassembler.reassemble
```

**新规则插入**：`fold_failed_toolresults` 之后、`reassembler.reassemble` 之前。

```python
# gdr/pipeline/runner.py: 现有 fold_failed_toolresults 调用之后
if cfg.refiners.retry_loop_clip.enabled:
    from gdr.refiners.retry_loop_clip import (
        llm_clip_retry_loop, apply_llm_clip,
    )
    for msg in session.messages:
        keep = await llm_clip_retry_loop(
            msg.blocks, llm_client,
            min_consecutive=cfg.refiners.retry_loop_clip.min_consecutive,
            max_keep=cfg.refiners.retry_loop_clip.max_keep,
        )
        if keep is not None:
            msg.blocks = apply_llm_clip(msg.blocks, keep)
```

### 3.4 配置项

`gdr/config/defaults.yaml`：

```yaml
refiners:
  retry_loop_clip:
    enabled: true
    min_consecutive: 5      # rule 预筛: 连续 ≥5 次同函数
    max_keep: 3             # LLM 决定保留最多 3 条
    llm_max_retries: 1      # LLM 调用失败即 fallback 保留
```

### 3.5 测试要点

`gdr/tests/test_retry_loop_clip.py`：

| 测试名 | 断言 |
|---|---|
| `test_rule_filter_non_429_returns_none` | 含 success/error（非 429）→ 返回 None，不调 LLM |
| `test_short_run_not_triggered` | 段长 3 < min_consecutive → 不调 LLM |
| `test_llm_says_no_returns_none` | LLM `is_retry_loop=false` → None |
| `test_llm_returns_keep_indices` | LLM `is_retry_loop=true, keep=[0,4]` → 返回 `[0,4]` |
| `test_llm_keep_without_failure_rejected` | keep 全是 success（违反"至少 1 失败"）→ None |
| `test_llm_exception_returns_none` | LLM 抛异常 → None（保守） |
| `test_apply_llm_clip_drops_blocks` | toolcall idx=1 + 配对 toolresult 被丢弃 |
| `test_integration_clip_e010` | E010（60+ 次 web_search）剪后显著下降 |

### 3.5.1 prompt 调优评估

新增 `gdr/refiners/retry_loop_ground_truth.py` 与 `gdr/refiners/retry_loop_prompt_eval.py`，提供：

- **23 个 ground truth 样本**（4 类）：
  - `clear_retry` × 10：完全相同 query / 大小写变体 / 限定词增加 / 同义词改写 / 关键词顺序 / 标点变体 / URL 变体 / 数字变体
  - `not_retry` × 6：不同搜索方向 / 跨语言 / schema 变化 / 不同细分查询 / 不同时间维度 / 同名不同实体
  - `edge_case` × 4：段长 < 5 / 含 success / 含 timeout-500 混合 / 异常 query 跳变
  - `real_world` × 3：从 `output/refine_data/E010*` 抽取的 Tavily web_search 真实 429 样本
- `evaluate_prompt(samples, llm_client)` → `EvalResult` 数据类（总数 / 判对 / 准确率 / 按类别 / 错判列表 / 错误列表）
- `format_report(result)` → Markdown 报告（含错判样本 diff: 期望 vs 实际 + LLM reason）
- `gdr/refiners/retry_loop_prompt_tune.py` CLI：`uv run python -m gdr.refiners.retry_loop_prompt_tune [--output FILE] [--category CAT]`

判定规则（双向严判）：
- `expected_is_retry_loop=True, actual=False` → 漏判 (FN)
- `expected_is_retry_loop=False, actual=True` → 误判 (FP)
- `is_retry_loop` 命中但 `keep_indices` 错 → 部分正确 (计入 misclassified, 标 `is_retry_loop_hit=True`)
- LLM 异常 / JSON 解析失败 → 视为漏判 (计入 misclassified)

调优工作流：
1. 跑 baseline：`uv run python -m gdr.refiners.retry_loop_prompt_tune --output baseline.md`
2. 改 `retry_loop_clip.py::_CLIP_PROMPT`
3. 重跑对比 `baseline.md` 与新报告
4. 目标：整体准确率 ≥ 90%，各 category ≥ 80%

### 3.6 风险与缓解

| 风险 | 缓解 |
|---|---|
| LLM 误判成本高（剪错丢训练信号） | rule 预筛 + 保守 fallback + "至少保留 1 失败"三层兜底 |
| LLM 调用开销 | rule 通过的连续 429 段量小（粗估每文件 0–2 段），建议复用 `thought_refactor.py` 已用的 9B 本地模型 |
| prompt 调优依赖 | 需构造 20–30 个 ground truth 样本做调优，单独排期 |

### 3.7 工作量：3 天

- 0.5 天 实现 `retry_loop_clip.py` 主流程
- 0.5 天 实现 `_group_consecutive_same_function` / `_summarize_call` 辅助
- 1.0 天 单元 + 集成测试 + 历史样本回归
- 1.0 天 prompt 调优（多次实验 LLM 在不同 query 改写下的判定准确性）

## 4. 方案③ 暂不做

按反馈，429 立即切换的 system prompt 启发式本期不落地。后续可拆为独立工单讨论：
- **路 A**：本地 4 个 LLM HTTP 客户端（`llm_client.py` / `http_embed.py` / `thought_refactor.py` / `tool_fixer.py`）的 429 Retry-After 解析 + 指数退避封顶
- **路 B**：`gdr/config/tools.yaml` 的 `tool_descriptions` 填充并下发到 qf_text system prompt

## 5. 验证脚本

```powershell
# 1. 跑 refine
uv run python -m gdr --config config/config.yaml --batch-id after_p0_fixes

# 2. 验证 ⟦⟧ 全部剥离
python -c "
import os, re, json
base = 'output/refine_data/after_p0_fixes'
files = sorted(os.listdir(base))
qj_files = [f for f in files if f.endswith('.qwenjina.txt')]
json_files = [f for f in files if f.endswith(('.messages.json','.openai.json','.meta.json'))]
for fn in qj_files + json_files:
    text = open(os.path.join(base, fn), encoding='utf-8').read()
    assert '⟦' not in text, f'{fn} 残留 ⟦'
print(f'✓ {len(qj_files)+len(json_files)} 个文件无 ⟦⟧ 残留')

# 3. 验证 meta.json 标注
contaminated = sum(
    1 for fn in files if fn.endswith('.meta.json')
    if json.load(open(os.path.join(base, fn), encoding='utf-8'))
    .get('meta_tag_contamination', {}).get('has_meta_tag')
)
print(f'✓ {contaminated} 个 meta.json 含 meta_tag_contamination 标注')
assert contaminated >= 10, f'预期 ≥ 10, 实际 {contaminated}'
"

# 4. 验证重试循环剪枝
python -c "
import os, json
base = 'output/refine_data/after_p0_fixes'
import re
real_func = re.compile(r'<function=(?!example_function_name)([a-zA-Z_]+)>')
e010 = next(f for f in os.listdir(base) if f.startswith('E010') and f.endswith('.qwenjina.txt'))
text = open(os.path.join(base, e010), encoding='utf-8').read()
n = len(real_func.findall(text))
print(f'E010 web_search 计数 = {n}, 预期 ≤ 3')
assert n <= 3
"

# 5. 跑测试
uv run python -m pytest gdr/tests/test_meta_tag_strip.py -v
uv run python -m pytest gdr/tests/test_retry_loop_clip.py -v
uv run python -m pytest tests/orchestration/test_system_prompt.py tests/orchestration/test_usage_prune.py -v
```

## 6. 总览与排期

| 项 | 改动 | 工作量 | 风险 |
|---|---|---|---|
| ① ⟦⟧ 全量剥离 + 模板下线 | 5 源文件 + 新模块 + 8 处测试更新 | **2 天** | 低 |
| ② LLM 判重试剪枝 | 新模块 + pipeline 接入 + 测试 + prompt 调优 | **3 天** | 中（LLM 依赖） |
| ③ 暂不做 | — | — | — |
| **合计** |  | **5 天** |  |

**执行顺序**：① 先做（独立、稳、立即见效）。② 在①跑通后做（避免回归叠加）。

## 7. 关联文档

- `docs/refactor-development-progress.md` — gdr SFT 数据质量修复迭代日志（含 F1/F2/F3-C + Fix A/B/C + F3-D/E）
- `docs/设计方案/cot-sft-trajectory-data-spec.md` — CoT SFT 训练数据规范
- `docs/设计方案/gdr-plan.md` — gdr 模块功能与时序概括
- `docs/refactor-development-progress.md` 第 131 / 136 行 — 既有 ⟦⟧ structural close 兜底逻辑（保留作为 fallback）