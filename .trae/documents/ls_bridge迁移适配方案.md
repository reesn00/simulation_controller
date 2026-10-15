# ls\_bridge 迁移适配方案：旧版 LabelStudio → trajectory\_pipeline

## Context

当前管线的数据链路止于 scout 三态分流落盘本地 JSONL（`positive/negative/quarantine_candidates.jsonl`），无上传环节；`trajectory_pipeline/evaluation/ls_bridge/` 仅有设计 docstring 空壳。旧版 [archive/v1/label\_studio/](file:///c:/Users/klpc/workspace/code/items/simulation_controller/archive/v1/label_studio) 有完整 LS 上传实现（10 文件），但数据契约耦合 v1 的 C3 视图，且 CLAUDE.md 约定 archive 禁止 import、必须照读重写。

**目标**：评判打分后将合格（positive）与劣质（negative，含 `d*_failed`/`veto` 标签）数据一起上传 LabelStudio。

**已确认决策**：

1. 数据范围：positive + negative 全量（quarantine 留本地）
2. 数据载体：P3 视图（messages/openai/meta）作为 task.data，经 [read\_p3.py](file:///c:/Users/klpc/workspace/code/items/simulation_controller/trajectory_pipeline/storage/read_p3.py) 的 `read_views(sample_id)` 回溯
3. 评分：新评分卡 scorecard.v2（d2/d4/d5/d6 各维 score/reasons + tags）作为 predictions
4. 配置：环境变量 `TRAJECTORY_LS_*` 前缀，CLI 参数覆盖，不用 YAML

## 一、旧版模块适配分析

### 适配结论总表

| 旧版文件                       | 处置                                  | 适配说明                                                                                           |
| -------------------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------- |
| `client.py`                | **近乎照搬**                            | httpx 直打 REST（无 SDK）、PAT JWT 刷新/legacy 双凭据、`MAX_TASKS_PER_IMPORT=1000` 全保留；仅改 import 路径与异常类    |
| `push_index.py`            | **照搬**                              | append-only JSONL 台账（LS 1.23 无原生去重）、msvcrt/fcntl 双平台锁、先落盘再改内存；主键 `session_id` 语义改为 `sample_id` |
| `errors.py`                | **照搬微调**                            | `redact()` + `CredentialLeakDetected` 原样；`C3ParseError` → `P3ViewError`                        |
| `settings.py`              | **重写**                              | 去 YAML/`COMPONENT_WEIGHTS`/`HookSettings`；环境变量版 frozen dataclass                               |
| `config_loader.py`         | **丢弃**                              | YAML 加载整体不需要（唯一依赖 `shared_config` 随之消除）                                                        |
| `task_exporter.py`（1050 行） | **重写 →** **`exporter.py`**（约 400 行） | 数据源从 C3 meta.json 换成 ScoutRecord + P3 视图；`render_openai_text` 等渲染函数择优照搬                        |
| `scorecard.py`             | **重写**                              | 6 维 L0-L5 是 v1 专有维度，换 v2 口径 4 维                                                                |
| `project_manager.py`       | **照搬微调**                            | 去 config 依赖，参数改 `LSSettings`                                                                   |
| `__main__.py`              | **重写 →** **`cli.py`**               | 对齐 scout/golden 的 `python -m` argparse 风格                                                      |
| 凭据扫描（task\_exporter 内）     | **拆出 →** **`scan.py`**              | R11 七正则 fail-closed 原样保留                                                                       |

### 关键差异点

| 维度   | 旧版（v1）                                                                    | 新版（v2）                            |
| ---- | ------------------------------------------------------------------------- | --------------------------------- |
| 数据输入 | C3 四视图文件（meta.json 需 `training_value_score`/`criterion_results` 等 v1 专有键） | scout JSONL 行（ScoutRecord）+ P3 视图 |
| 评分维度 | L0-L5 六维                                                                  | d2/d4/d5/d6 四维（`DimensionScore`）  |
| 分数过滤 | `filter_min_training_value_score`                                         | 不需要（scout 已三态分流）                  |
| 数量闸  | `dry_run_skip_threshold=5000`                                             | 保留（是数量安全闸，非分数过滤）                  |
| 配置   | YAML `label_studio:` 段 + api\_key 文件                                      | `TRAJECTORY_LS_*` 环境变量            |
| 调用方  | orchestration ls\_hook 旁路推送                                               | 独立 CLI（本次不做 executor 集成）          |

## 二、新模块结构

```
trajectory_pipeline/evaluation/ls_bridge/
├── __init__.py          # 补充导出（现有五条设计约束 docstring 保留）
├── settings.py          # 环境变量版配置
├── errors.py            # redact + CredentialLeakDetected + P3ViewError
├── client.py            # REST 包装（照搬）
├── push_index.py        # 台账去重（照搬）
├── scan.py              # R11 凭据扫描（从 task_exporter 拆出）
├── scorecard.py         # scorecard.v2
├── exporter.py          # scout JSONL → task/prediction（重写核心）
├── project_manager.py   # init/status/purge
├── cli.py               # argparse 命令
└── __main__.py          # from .cli import main
```

关键签名：

* `settings.py`：`LSSettings`（frozen dataclass），`load_settings() -> LSSettings` 读 `TRAJECTORY_LS_URL / API_KEY / PROJECT_ID / ENABLED`，校验 URL 非空、batch\_size≥1

* `exporter.py`：`build_task_data(record: ScoutRecord, views: dict) -> dict`；`export_batch(scout_dir, buckets, settings) -> ExportPlan`；`push_batch(client, plan, index) -> dict`

* `scorecard.py`：`build_scorecard(record: ScoutRecord, classify_kind: str) -> dict`

* `scan.py`：`scan_for_credentials(payload, *, view, settings) -> list[ScanHit]`

## 三、数据流

```
scout_dir/{positive,negative}_candidates.jsonl
  → 逐行读 ScoutRecord（quarantine 不读）
  → storage.read_p3.read_views(sample_id)      # 返回 None → 记 missing 跳过（fail-closed，不推半份）
  → build_task_data: {sample_id, task_id, run_id, status, tags,
      messages, openai, meta_summary(gate/step_count/degraded_from)}
  → build_scorecard(记录, classify 重算) → prediction（只落 risk_hints，不落决策）
  → scan_for_credentials 扫三视图 → 命中拒推（fail-closed）
  → PushIndex.has(sample_id) 过滤已推
  → 分批(≤1000) POST /api/projects/<id>/import + import/predictions
  → PushIndex.append（锁内）→ 汇总计数
```

**不发 inner\_id**（LS 1.23 字符串 inner\_id 批量静默丢弃，台账是唯一去重）。

## 四、评分卡 scorecard.v2

```json
{
  "schema_version": "scorecard.v2",
  "sample_id": "...", "enabled": true,
  "overall": {"kind": "negative", "confidence": "high", "reason": "veto|d6_failed"},
  "dimensions": [
    {"id": "d2", "label": "指令遵循", "score": 85, "is_veto": false,
     "source": "measured", "reasons": ["..."]}
  ],
  "dimension_summary": {"total": 4, "measured": 3, "missing": 1},
  "tags": ["d6_failed", "veto"]
}
```

* `source`：`score is None → "missing"`，有分 → `"measured"`（保留 `partly_estimated/estimated` 枚举兼容）

* `confidence`：veto 或 4 维全有分 = high；任一 missing = low

* prediction 只写 risk\_hints TextArea，**绝不写 overall\_decision**（v1 验证过的铁律，避免盖过标注员判断）

## 五、CLI 设计

`python -m trajectory_pipeline.evaluation.ls_bridge <子命令>`：

* `upload --scout-dir <path> [--buckets positive,negative] [--dry-run] [--limit N] [--batch-size] [--no-scorecard] [--force]`

* `status`（连通性 + 配置检查）

* `init-project [--label-config PATH]`（初始化 label\_config XML，需按 v2 标签域新写）

* `purge --project-id <id> --confirm`

风格对齐 scout/golden CLI：`main(argv) -> int`、`set_defaults(func=...)`、`sys.stdout.reconfigure(encoding="utf-8")`。

## 六、测试方案

* **unit**（`trajectory_pipeline/tests/unit/`）：

  * client：`httpx.MockTransport`（401 触发刷新、predictions 静默 `created:0`）

  * push\_index：坏行跳过、锁并发、never-rewrite

  * scorecard v2：快照测试

  * exporter：tmp views 目录 + 合成 scout JSONL

* **contract**（`tests/contract/test_ls_bridge_contract.py`）：AST 强制 ls\_bridge 禁 import `archive.*`、只准依赖 `storage`/`scout.types`/标准库+httpx

* **integration**（`tests/integration/test_ls_bridge_live.py`）：`@pytest.mark.integration`，真实 LS 127.0.0.1:8088（pyproject.toml L63 已预留 marker）

## 七、实施步骤（6 步，每步可独立验证）

1. **底座**：errors / client / push\_index / settings 照搬重写 + unit 测试
   验证：`pytest tests/unit -k ls`
2. **scan.py + scorecard.py** + unit（纯函数，最易测）
3. **exporter.py**：读 scout JSONL → read\_views 回溯 → task/prediction，`--dry-run` 跑通
4. **project\_manager + cli**：四命令串起来
5. **contract 测试**：AST 边界 + 控件名与 label\_config XML 一致性
6. **integration**：本地 LS 实推，验收以标注页实视为准（validate 绿灯不算数）

## 八、风险与注意点

* **LS 1.23 行为**：inner\_id 字符串批量静默丢弃（不发即可）；`import/predictions` 的 from\_name 不命中时静默 `created:0`，契约测试盯住；GET tasks 全量返回勿用于去重

* **双向化预留**：本次只做单向推送；init-project 后禁全量 PATCH label\_config（`ls_bridge/__init__.py` 约束）；回流只限黄金集/评估集

* **Windows**：msvcrt 锁文件方案保留；恒 UTF-8；stdout reconfigure

* **凭据**：key 只走 `TRAJECTORY_LS_API_KEY` 环境变量；日志/异常经 `redact()`；R11 扫描不豁免

* **数量闸**：`dry_run_skip_threshold=5000` 保留，超出需 `--force`

* **缺视图**：`read_views` 返回 None 时跳过并计数，绝不推半份数据

* **依赖方向**：ls\_bridge 属 evaluation 层，仅依赖 `storage`/`scout.types`，禁 import executor/assembler

## 验证方式

1. 单测：`pytest trajectory_pipeline/tests/unit -k ls_bridge`
2. 契约：`pytest trajectory_pipeline/tests/contract/test_ls_bridge_contract.py`
3. 端到端 dry-run：先造小型 scout 产物目录，`python -m trajectory_pipeline.evaluation.ls_bridge upload --scout-dir <tmp> --dry-run` 核对 plan 计数与明细
4. 实推（可选）：本地起 LS 于 127.0.0.1:8088，`pytest -m integration`，人工到标注页实视确认

