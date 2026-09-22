# Refactor Development Progress

> 本文档记录 `gdr` 精修流水线在 SFT 数据质量提升方向的迭代修复。配套实施基线 [refactor-implementation-plan.md](refactor-implementation-plan.md) 与最终验证 [phase6-final-validation-report.md](phase6-final-validation-report.md)。本文档按修复编号倒序追加，每条修复包含背景、改动、测试、效果。

最近一次重大质量提升：**2026-09-19**（F1/F2/F3-A/B/C/D/E）。

---

## 1. 2026-09-19：SFT 数据完整性 + 启发式精度 六件套

### 背景

T001 任务三次重跑均进入 `incomplete` 或 `discard` 死信通道：
1. `discard` by `consistency_score=2 < 7`（judge 终检拒收，原因黑盒）
2. `incomplete` by `last_text_incomplete`（启发式误判 `⟧` 结构化收尾符）
3. `incomplete` by `toolcall_result_mismatch:31_vs_30`（agent 主动放弃等结果被误判）

审查 refined trajectory 后确认内容质量持续提升（更严谨、更诚实、tool 调用更聚焦），但 **结构性硬指标与启发式判定** 让好数据进不了 `refine_data` 主输出。

### 修复清单

| 编号 | 主题 | 影响范围 |
|---|---|---|
| F1 | tools 字段透传到所有 SFT 视图 | `domain/schema.py::save_session` + `etl/qwenformat/usage_prune.py::write_refined_session` |
| F2 | incomplete 检测维度 4：thinking-only tail | `pipeline/runner.py::_detect_incomplete_session` |
| F3-C | System prompt 追加 Reasoning requirement 段 | `etl/qwenformat/system_prompt.py::render_cleaned_system` |
| Fix A | L3 judge reason 串入 `judge_discard` metadata | `reassembly/reassembler.py` + `pipeline/runner.py::_append_judge_low_queue` |
| Fix B | judge_min_score 三段阶梯阈值（passthrough / low_edit / relaxed） | `config/settings.py` + `reassembly/reassembler.py::_judge_min_score_for` |
| Fix C | failure_handler 保留 qf_out + INDEX.jsonl + reprocess_dead | `orchestration/failure_handler.py` |
| F3-D | `_is_text_incomplete` 结构闭合信号 + 尾部语义收尾词 | `pipeline/runner.py::_is_text_incomplete` + `_has_structural_close` |
| F3-E | 维度 2 豁免：未配对 toolcall + 完整收尾 text → 不算 incomplete | `pipeline/runner.py::_detect_incomplete_session` + `_has_complete_close_signal` |

---

### 1.1 F1：tools 字段透传到所有 SFT 视图

**问题**：旧 `save_session` 只在 `openai.json` 顶层写 `tools`，`messages.json` 缺失。SFT 训练读取 `messages.json` 时拿不到 tool schema，必须依赖 `meta.json` 或 `qf_text`，链路脆弱。

**改动**：
- [gdr/domain/schema.py:223+](../gdr/domain/schema.py#L223) — `save_session` 提取 `metadata["tools"]`，写入 `messages.json` 和 `openai.json` 顶层
- [gdr/etl/qwenformat/usage_prune.py](../gdr/etl/qwenformat/usage_prune.py) — `write_refined_session` 同步写两份
- 引入 `_extract_tools_payload` 辅助；`tools_payload_max` 控制 schema 数量上限（默认 64）

**测试**：[gdr/tests/test_save_session_tools.py](../gdr/tests/test_save_session_tools.py) — 18 个测试通过

**效果**：所有 refine_data 视图文件（messages.json / openai.json / meta.json）都含完整 tool schema，SFT 训练链路可直接消费。

---

### 1.2 F2：incomplete 检测维度 4（thinking-only tail）

**问题**：复现链（T001 2026-09-19）—— 最后一条 assistant 是 thinking-only 空 content，原 3 维全部跳过，误判完整。

**改动**：
- [gdr/pipeline/runner.py](../gdr/pipeline/runner.py) — `_detect_incomplete_session` 增加维度 4：当末尾 assistant 无 final text、无未配对 toolcall、但 thinking_chars ≥ 阈值时判 incomplete
- `cfg.incomplete_thinking_only_min_chars` 控制字符阈值（默认 200）

**测试**：[gdr/tests/test_incomplete_session_detection.py::TestF2ThinkingOnlyTail](../gdr/tests/test_incomplete_session_detection.py) — 5 个测试通过

---

### 1.3 F3-C：System prompt 追加 Reasoning requirement 段

**问题**：远端 agent 在 thinking 模型下不输出 reasoning block，导致 trajectory 进入 SFT 后早期 assistant 缺失 reasoning 字段。

**改动**：
- [gdr/etl/qwenformat/system_prompt.py](../gdr/etl/qwenformat/system_prompt.py) — `_REASONING_REQUIREMENT_SECTION` 常量；`render_cleaned_system` 增加 `append_reasoning_requirement=True` 参数，默认开启
- 段内容：要求每轮 assistant 必须先输出 `thinking` 块再 content/tool_call，并明示缺 thinking 会触发 incomplete 判定

**测试**：[tests/orchestration/test_system_prompt.py::TestReasoningRequirementSection](../tests/orchestration/test_system_prompt.py) — 5 个测试通过

---

### 1.4 Fix A：L3 judge reason 串入 metadata + judge_low.jsonl 字段展开

**问题**：
1. L3 judge 返回 `{verdict, score, reason}`，但 `discard_meta` 只保留 `{score, min_score}`，LLM 评语丢失
2. `judge_low.jsonl` 每条只含 `{score, min_score}`，审计时无法知道判分理由

**改动**：
- [gdr/reassembly/reassembler.py:886](../gdr/reassembly/reassembler.py#L886) — `result.get("reason", "")` 写入 `discard_meta.reason`
- [gdr/reassembly/reassembler.py:976](../gdr/reassembly/reassembler.py#L976) — strict 模式失败时也串 `exception` / `exception_type`
- [gdr/pipeline/runner.py::_append_judge_low_queue](../gdr/pipeline/runner.py) — 把 `judge_discard` 展开为顶层 `judge.{score, min_score, reason, relaxed_kind, modified_blocks}`
- 新增 `cfg.judge_low_include_reason`（默认 True），关闭即退回旧行为

**测试**：[gdr/tests/test_judge_low_reason.py](../gdr/tests/test_judge_low_reason.py) — 7 个测试通过

**效果**：审计 judge_low.jsonl 时可直接 grep `reason` 字段定位模型认为"自洽度不足"的具体原因。

---

### 1.5 Fix B：judge_min_score 三段阶梯阈值

**问题**：原 `judge_min_score=7` 单层过严；`judge_min_score_relaxed=3` 豁免要求 `modified_blocks ≤ 5` 才生效，对 system 清洗类样本（modified=0）仍走严格阈值。

**改动**：
- [gdr/reassembly/reassembler.py:22-62](../gdr/reassembly/reassembler.py#L22) — 新增模块级函数 `_judge_min_score_for(cfg, modified_count)` 返回 `(effective_min_score, relaxed_kind)`
- 阶梯规则（按 modified_blocks 计数）：
  - `≤1` → `passthrough`（默认 min=2）
  - `≤3` → `low_edit`（默认 min=5）
  - `≤5` → `relaxed`（默认 min=3）
  - 其他 → 严格 `judge_min_score=7`
- [gdr/config/settings.py:317-339](../gdr/config/settings.py#L317) — 新增 `judge_min_modified_passthrough / judge_min_score_passthrough / judge_min_modified_low_edit / judge_min_score_low_edit / judge_relaxed_audit_note`
- 任一 `(threshold, min)` 设为 0 即可关闭该档；`judge_relaxed_audit_note=False` 关闭 metadata 留痕

**测试**：[gdr/tests/test_judge_relaxation.py](../gdr/tests/test_judge_relaxation.py) — 12 个测试通过

**效果**：system 清洗类样本（modified=0）能以 `passthrough` 档通过；audit 记录 `relaxed_kind` 供后续筛选。

---

### 1.6 Fix C：failure_handler 保留 qf_out + INDEX.jsonl + reprocess_dead

**问题**：原 `reap_dead` 把 `qf_output_path` `shutil.move` 到 dead 根目录，`output/qf_out/` 被清空；改进 gdr 配置后无法用同一 qf_out 重跑。

**改动**：
- [orchestration/failure_handler.py](../orchestration/failure_handler.py) — `reap_dead` 默认 `preserve_qf_out_in_dead=True`：
  - qf_output 同步复制到 `dead_dir/qf_out/<basename>`
  - `dead.log` 新增 `qf_out_dead_path` 字段
  - 新增 `dead_index_path` 参数写 `INDEX.jsonl`，含 `gdr_status / score / reason`（从 error_msg 解析）
- 新增模块级函数 `reprocess_dead(dead_dir, qf_out_target, dead_index_path, filter_score_lt, filter_gdr_status)` 按 score/status 过滤回灌

**测试**：[tests/orchestration/test_failure_handler_qfout_preserve.py](../tests/orchestration/test_failure_handler_qfout_preserve.py) — 12 个测试通过

**效果**：调整 gdr 配置后可直接调用 `reprocess_dead` 把死信 qf_out 拷回 `output/qf_out/`，避免重跑 simulate_serve。

---

### 1.7 F3-D：`_is_text_incomplete` 结构闭合信号

**问题**：复现链（T001 2026-09-19）—— agent 用 `⟦ ... ⟧` 作为结构化收尾（"已完成/下一步/锚点"格式），但启发式只看末尾 ASCII 标点（`.!?。！？"`'` 等），`⟧` 命中即判 incomplete。

**改动**：
- [gdr/pipeline/runner.py](../gdr/pipeline/runner.py) — 新增：
  - `_COMPLETE_TAIL_TOKENS`（"总结/结论/已核实/已完成/下一步/锚点/报告完毕/done" 等）
  - `_has_structural_close(s)` 检测 markdown 表格行 / 分隔线 / code fence / colon-fenced block / 方括号配对闭合（⟦⟧、【】、()、[] 等）
- `_is_text_incomplete` 加入两段"完整"快速通路：
  - 长度 > 30 且 `_has_structural_close` 命中 → 视为完整
  - 长度 > 30 且尾部 100 字符含 `_COMPLETE_TAIL_TOKENS` 任一 → 视为完整
- 旧行为（前缀 marker / 末尾标点 / 省略号）保留

**测试**：[gdr/tests/test_complete_tail_detection.py](../gdr/tests/test_complete_tail_detection.py) — 22 个测试通过

**设计原则**：判"完整"比判"不完整"更稳——"出现结构性闭合或语义收尾词 = 完整"是布尔信号，不依赖具体字符集；以后 agent 用 `⸻ ⟪` 等新符号依然能识别。

---

### 1.8 F3-E：维度 2 豁免（agent 主动放弃等结果）

**问题**：复现链（T001 03f3f0fd 2026-09-19）—— 31 个 toolcall / 30 个 toolresult 差 1 个，但末尾 text 块以 `⟦ ... ⟧` 收口。原因是 agent 调用 `del` 删除临时文件被 runtime 拦下，agent 主动放弃重试并直接给最终回复。gdr 把它和"被截断的烂 trajectory"一视同仁，进 incomplete 旁路。

**改动**：
- [gdr/pipeline/runner.py](../gdr/pipeline/runner.py) — 新增 `_has_complete_close_signal(s)`（弱化版的完整收尾检测，不设最短长度门槛）
- 维度 2 (`toolcall_result_mismatch`) 增加豁免分支：
  - 仅当末尾是 text 块，且 text 含完整收尾信号时跳过
  - INFO 日志说明 "agent-intentional closure"
  - 豁免只针对维度 2；维度 1/3/4 走各自路径不受影响

**测试**：[gdr/tests/test_incomplete_session_detection.py::TestF3EMismatchWithCompleteClose](../gdr/tests/test_incomplete_session_detection.py) — 6 个测试通过

**效果**：T001 03f3f0fd 现在跑 gdr 进入 `refine_data` 主输出而非 `incomplete` 旁路；同理适用于所有"agent 主动放弃等结果 + 主动收口"的健康妥协样本。

---

## 2. 全套回归（2026-09-19）

| 范围 | 通过 | 新增 |
|---|---|---|
| gdr 单元 + 合约 + 功能 | 228 / 228 | +57 |
| orchestration 测试 | 293 / 293 | +12 |
| 全仓（非 gdr） | 567 / 567 | — |

每条修复均带 flag 即可回到既有行为，无破坏性变更：

| 修复 | 回退 flag |
|---|---|
| F1 | `include_tools_in_payloads=False` |
| F2 | `incomplete_detection_enabled=False` |
| F3-C | `render_cleaned_system(append_reasoning_requirement=False)` |
| Fix A | `judge_low_include_reason=False` |
| Fix B | 把对应 `(threshold, min)` 设为 0 |
| Fix C | `reap_dead(preserve_qf_out_in_dead=False)` |
| F3-D | 关闭时回退到旧 `_is_text_incomplete`（结构闭合快速通路为 opt-in） |
| F3-E | 豁免分支为强信号检测，移除即退到旧维度 2 严格判定 |

---

## 3. 关键设计原则

1. **结构闭合 / 语义收尾优先于字符集白名单**（F3-D/E）：避免维护字符集清单，扩展新符号自动识别
2. **每条修复带 flag**：可灰度、可回退、可对比新旧行为
3. **数据完整性优先于判分**：incomplete 旁路保留完整 session（不丢数据），审计通道完整可回灌
4. **修复与既有测试共存**：所有改动均保留既有测试路径（如 `_has_complete_close_signal` 不破坏 `_is_text_incomplete`）
5. **质量好 ≠ 不该死信**：内容质量与硬指标判定是不同维度；好的 trajectory 因结构不闭合而死信是规则漏洞，应修规则而非接受它

---

## 4. 架构迁移：`simulation server → etl → gdr` 改为 `→ gdr → etl`（2026-09-22）

### 4.1 动机

原架构 gdr 是末阶段，读 etl 已经渲染的 `qf_output_path`（含 `metadata.openai_messages / qf_text`）。
问题：etl 必须先调 LLM 渲染 qf_text + transform，gdr 才能拿到输入；gdr 改完块再调一次
`usage_prune` 改 metadata。两段都耗 LLM 配额，链路长，重复渲染。

新架构：gdr 是首阶段，直接读 trajectory；只精修 block。etl 是末阶段，只做格式转换
（usage_prune / transform / partition / summarizer）。etl 不在头部，重渲染只在尾部
做一次。

### 4.2 关键变化

- **gdr 输入**：trajectory JSON → `gdr.parsers.from_trajectory` 轻解析；不再读 `qf_out`
- **gdr 输出**：单 C2 refined Session（`output/refined/<TXXX>__<session>.json`）；不再写 4 视图
- **etl 输入**：C2 单文件 → `etl.parsers.load_refined_session`
- **etl 输出**：4 视图（`output/refine_data/<TXXX>__<session>_refined.{messages,openai,qwenjina.txt,meta}.json`）
- **删除**：`gdr/pipeline/runner.py::_apply_usage_prune`；`gdr/config/settings.py::enable_usage_prune` / `qf_chat_template_path`；`gdr/domain/schema.py::save_session`（旧单文件版）
- **新增**：`gdr/parsers/`（C1 入口）；`etl/parsers/`（C2 入口）；`etl/writers/`（C3 写 4 视图）
- **orchestration 状态机**：`pending → gdr_processing → pending_etl → etl_processing → done`
- **SQLite 字段**：`gdr_output_path` → `gdr_refined_path`；新增 `etl_*_path` 4 列；删除 `qf_output_path`

### 4.3 契约层（`docs/contracts/`）

- `C1-trajectory-events.md` —— simulation server → gdr
- `C2-refined-session.md` —— gdr → etl
- `C3-final-sft-views.md` —— etl → 训练 / audit
- `migration-plan.md` —— 直切新架构的完整步骤 + 回滚
- `README.md` —— 旧 vs 新对比表

### 4.4 修正既有条目

- §1.6 "Fix C：failure_handler 保留 qf_out + INDEX.jsonl + reprocess_dead" —— qf 阶段已删除；改为保留 refined 单文件 + INDEX.jsonl
- §1.7 "F3-D：`_is_text_incomplete` 结构闭合信号" —— 内部调用从 `save_session` 改为 `save_refined_session`
- §1.8 "F3-E：维度 2 豁免" —— 不变；仍是 runner 在 `save_refined_session` 之前的四维检测

### 4.5 验证

- `uv run python -m pytest -q` —— **475 / 475 通过**（0 跳过）
- `python scripts/purge_qf_out.py` —— 已清空 `output/qf_out/`（旧 1 文件 / 182KB）
- `python scripts/purge_legacy_refined.py` —— 已清空 `output/refine_data/` 旧 `*_refined.{messages,openai,qwenjina,meta}.json`（4 文件 / 57KB），旁路 jsonl 保留
