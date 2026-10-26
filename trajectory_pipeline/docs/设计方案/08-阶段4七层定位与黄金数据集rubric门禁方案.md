阶段 4 七层定位与黄金数据集 rubric 门禁方案
=========================================

> 状态:草案 v1  ·  适用范围:`trajectory_pipeline/executor` + `trajectory_pipeline/evaluation`  ·  编写日期:2026-10-14
> ⚠️ **分支数按 2026-10-26 现状回填**：20 活跃 + 1 deprecated = 21 key / 22 类合法标签。

---

## 0. 背景

用户连续追问三件事,串联起来是同一个根因 —— **黄金数据集没有真正经过 rubric 评分门禁**,所以 74.7% 一致率没有可信的语义根基。本文档一次性回答三问并给出整改方案。

---

## 1. 三问的直接回答

### 1.1 阶段 4 的 7 层在哪?

阶段 4(L0–L7)当前位于 **"URL 媒体核验"分支轨道**,由 `executor.cli mediabrowse` 触发,**与主流程 `run → P1 → P2 → P3` 不交汇**。

| 层  | 代码位置                                              | 落盘位置                              | 进 P1? |
| --- | ----------------------------------------------------- | ------------------------------------- | ------ |
| L0  | `executor/probe/static_probe.py`                      | `output/pipeline/staticprobe/*.json`  | 否     |
| L1  | `executor/probe/ffprobe.py`                           | `mediabrowse/*.json` 的 `l1_probe`    | 否     |
| L2  | `executor/probe/media_probe.py`                       | `mediabrowse/*.json` 的 `l2_signals`  | 否     |
| L3  | `perception/rule_perceptor.py` + `executor/arbitration.py` | `mediabrowse/*.json` 的 `l3_rule`     | 否     |
| L4  | `executor/review/vlm_reviewer.py` + `multim_vlm.py`   | `mediabrowse/*.json` 的 `l4_review`   | 否     |
| L5  | `executor/arbitration.py::arbitrate_l5`               | `mediabrowse/*.json` 的 `l5_outcome`  | 否     |
| L6  | `executor/review_queue.py::dispatch_l6`               | `output/pipeline/review_auto/`        | 否     |
| L7  | `evaluation/golden/builder.py`                        | `evaluation/golden/data/*`            | 否     |

只有 L7 间接读 P1;L0–L6 完全在主流程评估的视野之外。

### 1.2 验证流与主流程评估有交集吗?

几乎没交集。两路只在两处共享:

1. 客户端驱动器(`ObscuraDriver` / `McpClient` / `LLMClient`)—— 但调用点不同。
2. `executor.branches.BRANCH_LABELS` 的 **21 分支**名字面量 —— 同名,**但数据不共享**。

`evaluation.cli::select_archives` 显式只挑 `output/pipeline/<task>__*.json`(P1 形态),跳过 `staticprobe/`、`mediaprobe/`、`mediabrowse/`、`proberun/`。所以 L0–L5 事实从不进入 D-2/D-4/D-5/D-6 的分母,也不进入 D-10(74.7% 一致率)的对齐集。

### 1.3 74.7% 靠不住的根因

三层共因:

- **A. 分母切割**:`evaluation.cli` 跳过核验子目录,L0–L5 从未进入比较。
- **B. 黄金集不门禁**:`golden/builder.py::candidates` 只读 `negative.jsonl` + P1 结局 + P2 样本,**不**走 rubric 评分过滤;`annotations.jsonl` 是纯人工。
- **C. 共错风险**:若代码与人都误读了 rubric v1.2,D-10 = 100% 但两边都错,无任何纠错信号。

---

## 2. 整改目标

让 **"进入黄金数据集"** 这件事变成一个 **门槛事件**,而不是一个普通落盘事件:

1. rationale + assembler 之后完整组装的 agent 轨迹 **必须** 走 rubric 评分(D-2/D-4/D-5/D-6)。
2. 评分结果 **三态分流**:正例、负例、隔离。
3. 黄金集天然含正负例,与人工标注 `annotations.jsonl` 校对前先自洽。
4. L0–L5 证据可通过可选接口作为 D-3 的 `evidence_supplement`,但不替代 rubric 主评分。

---

## 3. 方案

### 3.1 命名与入口

新增 `evaluation.cli` 子命令 `scout`,在 `assembler.builder.build_p2()` 之后、`golden.builder.candidates()` 之前执行。

```text
executor.cli run
    └── assembler.builder.build_p2()          → output/pipeline/<task>__*.json (P1 + P2 合一)
            └── evaluation.cli scout --p2-dir <p2> --out <scout_dir> [--mediabrowse-dir <mb>]
                    └── evaluation.cli golden candidates --from-scout <scout_dir>
                            └── evaluation.cli golden freeze
```

### 3.2 三态契约

`scout` 对每条 P2 样本产出 **唯一** 一种状态:

| 状态            | 触发条件                                                            | 落盘文件                          |
| --------------- | ------------------------------------------------------------------- | --------------------------------- |
| `scored`        | D-2/D-4/D-5/D-6 全部有 `score: int`(None 视为缺件)                  | `positive_candidates.jsonl` 或 `negative_candidates.jsonl` |
| `undetermined`  | 任何维度 `score is None`(采集/门禁问题,不代表内容差)                | `quarantine.jsonl`                |
| `not_run`       | fail-closed:`credential_leak` / `driver_missing` / `timeout` / `driver_error` 触发 | 不落盘,日志 + 计数                       |

正负分流规则(写在 `evaluation/scout/policy.py`,**不**写在 builder 内):

- **正例候选** `positive_candidates.jsonl`:D-2/D-4/D-5/D-6 **全部** ≥ 3,且 **无** 否决项触发。
- **负例候选** `negative_candidates.jsonl`:任意维度 ≤ 2,或否决项触发;按失败维度打 tag(`d2_failed` / `d4_failed` / `d5_failed` / `d6_failed` / `veto`)。
- **隔离** `quarantine.jsonl`:**永不**进入黄金集,只供 `evaluation.cli scout --stats` 报告用。

### 3.3 可选 L5 交叉证据

传 `--mediabrowse-dir output/pipeline/mediabrowse` 时:

1. 按 `(task_id, url)` 索引 `BrowserProbeResult.l5_outcome`。
2. 仅作 D-3 的 `evidence_supplement` 字段附加,**不**改 rubric 主分。
3. 找不到 L5 证据时,**不**当作缺件 —— 仅留空字段。这是继承了 §3.2 "缺件 ≠ 扣分" 的不变量。

### 3.4 与现有 golden builder 的衔接

`evaluation/golden/builder.py` 新增子命令 `from-scout`,只读 scout 产物,**不再**直接扫 `negative.jsonl`。这样:

- 旧入口 `candidates` 保留,但加 `--deprecated` 警告并打印迁移指引。
- 新 `from-scout` 是唯一通往 `freeze` 的合法路径。
- `annotations.jsonl` 仍由人写,作为对抗样本/边界 case 的人工覆盖。

### 3.5 失败-关门(fail-closed)白名单

沿用 `executor.arbitration.L5_FAIL_CLOSED_NOTES` / `executor.review_queue.AUTO_REVIEW_NOTES` 的字面量,**新增** `evaluation/scout/fail_closed.py` 镜像,**绝对导入**统一指向 `executor.branches` 的 **21 分支**标签。两边由 `tests/contract/test_shared_literal_sync.py` AST 监视(沿用现有 `tests/contract/test_p2_contract.py` 的风格)。

### 3.6 L0–L5 与 P1 的关系(本轮不改造,留接口)

L0–L5 仍落 `output/pipeline/mediabrowse/` 等独立子目录,本轮 **不动** P1 schema。但 `evaluation.cli scout` 通过 `--mediabrowse-dir` 提供 L5 旁路;后续若决定合并入口,只需把 `select_archives()` 加入 mediabrowse 路径,**一处改动**。

### 3.7 实施步骤

1. `evaluation/scout/` 新目录,包含 `cli.py` / `policy.py` / `fail_closed.py` / `io.py`。
2. `evaluation/cli.py` 注册 `scout` 子命令(命名空间挂在 `evaluation` 下,**不**新建顶层 CLI)。
3. `evaluation/golden/builder.py` 新增 `from-scout` 子命令,旧 `candidates` 加 `--deprecated` 警告。
4. `tests/contract/test_scout_contract.py`:AST 验 `evaluation/scout/` 不反向 `import executor.cli`、不 `import trajectory_pipeline.executor`。
5. `tests/contract/test_scout_contract.py`:验 `evaluation/scout/fail_closed.py` 与 `executor.branches` 的 21 标签 AST 字面量同步。
6. `docs/04-运行手册.md` 增加 "阶段 4 scout 工作流" 一节。
7. `docs/05-现状与验收.md` 的 "已跑通" 表新增 `scout → from-scout → freeze` 行。

---

## 4. 不变量 & 纪律(必须保留)

| #   | 不变量                                                         | 落地形式                              |
| --- | -------------------------------------------------------------- | ------------------------------------- |
| 1   | **绝对导入**:`from trajectory_pipeline.xxx import ...`,不许相对 | mypy strict + ruff TID252            |
| 2   | **交接面是格式不是 import**:scout 只读 P2 JSON,不 import executor | `tests/contract/test_scout_contract.py` |
| 3   | **三态**:`scored` / `undetermined` / `not_run` 处处一致         | `evaluation/scout/types.py` TypedDict |
| 4   | **缺件 ≠ 扣分**:`score is None` 走 quarantine,不进负例         | `policy.classify()` 单元测试           |
| 5   | **共享字面量两处必同步**:**21 分支**标签、fail-closed 白名单        | `tests/contract/test_shared_literal_sync.py` |
| 6   | **黄金集不虚构**:只接受真实 scout 产物                         | `from-scout` 拒绝任意手工拼接          |
| 7   | **L6 人工不复用 scout**:人审仍走 `annotations.jsonl`,不走 `from-scout` | 命名空间与文件路径分开                  |

---

## 5. 不在本轮范围

- 把 L0–L5 合并进 P1 schema(已留接口,见 §3.6)。
- 改 rubric v1.2 的语义(沿用,不重写)。
- 让 L6 自动审复用 scout 算法(违反 §4-7,显式排除)。

---

## 6. 验收信号

| 信号                        | 期望                                                       |
| --------------------------- | ---------------------------------------------------------- |
| `scout --stats` 报告三态占比 | 三态合计 = P2 总样本数,无任何丢失                          |
| `from-scout` 入 `freeze`    | 0 报错的批次占主导;报错只来自 IO/JSON 损坏,不来自分类逻辑 |
| D-10 重测(可选)             | 74.7% 不再上升;若上升说明 annotation 也需走 scout         |

---

## 7. 实施前置 & 文件清单

| 文件                                                         | 状态        |
| ------------------------------------------------------------ | ----------- |
| `trajectory_pipeline/evaluation/scout/__init__.py`           | 新增        |
| `trajectory_pipeline/evaluation/scout/cli.py`                | 新增        |
| `trajectory_pipeline/evaluation/scout/policy.py`             | 新增        |
| `trajectory_pipeline/evaluation/scout/fail_closed.py`        | 新增        |
| `trajectory_pipeline/evaluation/scout/types.py`              | 新增        |
| `trajectory_pipeline/evaluation/scout/io.py`                 | 新增        |
| `trajectory_pipeline/evaluation/cli.py`                      | 注册 scout  |
| `trajectory_pipeline/evaluation/golden/builder.py`           | 加 from-scout |
| `trajectory_pipeline/tests/contract/test_scout_contract.py`  | 新增        |
| `trajectory_pipeline/tests/contract/test_shared_literal_sync.py` | 新增     |
| `trajectory_pipeline/docs/04-运行手册.md`                    | 加 scout 工作流 |
| `trajectory_pipeline/docs/05-现状与验收.md`                  | 加 scout 行 |

---

## 8. 变更记录

| 日期       | 版本 | 说明                              |
| ---------- | ---- | --------------------------------- |
| 2026-10-14 | v1   | 初稿,落实 scout 三态门禁设计       |
