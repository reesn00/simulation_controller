Rubric Gate 改造方案 v04
=========================

> 状态:方案 v04  ·  适用范围:`trajectory_pipeline/evaluation` + `trajectory_pipeline/evaluation/golden`  ·  编写日期:2026-10-14
> 上游设计:`docs/设计方案/08-阶段4七层定位与黄金数据集rubric门禁方案.md`(回答 3 问 + scout 三态分流设计)
> 替代:原 `07-rubric-gate改造方案.md`(1 行占位空壳,已于 2026-10-25 废弃文本清理删除;本档为其实施层细化)

---

## 0. 与 08 文档的关系

08 文档回答三个根因问题 + 给出 scout 三态分流的设计意图。本档是它的 **实施层细化**,把 §3 的 `evaluation.cli scout` 命令落到代码骨架、契约测试、文件层签名。

08 已锁定的设计决策(本档不重议):
- 三态:`scored` / `undetermined` / `not_run`
- 正负分流规则、quarantine 不进黄金集、可选 `--mediabrowse-dir` 旁路
- L6 人工不复用 scout 算法(违反不变量 7)
- 旧 `golden.builder.candidates` 加 `--deprecated` 警告但保留

本档新增 / 落实的细节:
- §4 评分器调用规范(直接吃 P2 dict,**不**经过 `split_archive`)
- §5 `Evaluation/Scout` 模块目录与文件级签名
- §6 `from-scout` 子命令的字段协议与 builder 内部数据流
- §9 5 条契约测试的具体 AST 断言式

---

## 1. 现状审计(基线)

实测 2026-10-10 38 份历史存档 / 2026-10-13 7 层仲裁落地后的代码面:

| 组件                                              | 现在的角色                                            | 进入黄金集的依据 |
| ------------------------------------------------- | ----------------------------------------------------- | ---------------- |
| `evaluation/cli.py::evaluate()`                   | 跑 D-2/D-4/D-5/D-6 打分,**只生成报表**,不写候选      | 无               |
| `evaluation/golden/builder.py::cmd_candidates()`  | 从 `negative.jsonl` + P1 outcomes + P2 samples 拼候选 | 无 rubric 评分   |
| `evaluation/golden/builder.py::cmd_annotate()`    | 合并 `annotations.jsonl`,**纯人工**,不进机器标注     | 无               |
| `evaluation/golden/builder.py::cmd_freeze()`      | 校准 / 持有切块并冻结成 `v<N>/`                       | 仅 `annotations.jsonl` |
| `evaluation/golden/builder.py::cmd_agreement()`   | D10 = 黄金集 vs 代码层一致率                          | 报表,不入候选     |
| `executor/arbitration.py::L5_FAIL_CLOSED_NOTES`   | L5 fail-closed 短路白名单                             | L5 不入 P1        |
| `executor/review_queue.py::dispatch_l6`           | L6 自动审(review_auto/),**不**进 P1                  | L6 不入 P1        |

**最关键的两个 gap**:

1. `evaluation.cli::evaluate` 跑过 D-2/D-4/D-5/D-6,但产物(JSON / stdout)从来没有回写到 P2 / P1 / 负样本池 / 黄金集 —— **评估与黄金集构建之间是断路**。
2. `golden.builder.cmd_candidates` 直读 `negative.jsonl`,把"进入黄金集池"约简成 "出现在了负样本池" —— 这是 **池子主**,不是 **评分主**。

v04 修复 gap 1:把评估结果接回到候选构建管线。v04 不修复 gap 2(留 09-rubric-gate改造方案-v05 处理 —— 抽象成 `Filter` 接口,允许后续接入 rubric-或-rationale-或-VLM-Judge 作为过滤源)。

---

## 2. 改造目标

1. **scout 命令**:跑 D-2/D-4/D-5/D-6 评分 → 三态分流 → JSONL 三件套落盘。
2. **from-scout 子命令**:只读 scout 产物生成候选,`candidates` 子命令加 `--deprecated` 警告。
3. **freeze 不变**:仍需 `annotations.jsonl`,但来自 scout 的正/负样本已带 `dimension_scores`,人审只做边界校准。
4. **可选 L5 旁路**:`--mediabrowse-dir` 将 L5 evidence 作为 D-3 的补充,不替代主分。
5. **D10 行为不变**:仍由 `cmd_agreement` 计算,只是分母里加一条 `*_from_scout` 子集标签,方便事后排查"自动评分后再人工校准的一致率 vs 纯人工一致率"。

---

## 3. 端到端数据流

```text
executor.cli run
    │
    ▼
assembler.builder.build_all()                    ── output/pipeline/<task>__<run>.json  (P1+P2 合一)
rationale.writer.write_one()              ── output/pipeline/<task>__<run>.rationale.json
    │
    ▼
evaluation.cli scout  ── NEW ──►                evaluation/scout/data/<ts>/scout.jsonl
                                                  ├ positive_candidates.jsonl
                                                  ├ negative_candidates.jsonl
                                                  └ quarantine.jsonl
    │
    ▼ (可选)
evaluation.cli scout --mediabrowse-dir <mb>  ── NEW ──►   (走 BrowserProbeResult.l5_outcome,
                                                              写 evidence_supplement,
                                                              找不到留空,不算缺件)
    │
    ▼
golden.builder from-scout  ── NEW ──►          evaluation/golden/data/candidates.jsonl  (强制覆盖旧版)
    │                                                    ※ 取的是 scout 的正+负,不取 quarantine
    ▼
 人工 annotate                                                    ※ annotations.jsonl 仍由人写
    │
    ▼
golden.builder freeze                             evaluation/golden/data/v<N>/
    │
    ▼
golden.builder agreement                          D10 报表(含 *_from_scout 子集)
```

---

## 4. 评分调用规范

### 4.1 评分器对象形态契约(继承 2026-10-13 已成事实)

`evaluation/scorers/{code,d4,d5,d6}_scorer.py` 的 4 个入口函数返回 `*Verdict` 对象,统一带 4 个属性:
- `kind: Literal["scored", "undetermined"]`
- `score: int | None` —— `None` 表示不可判定
- `is_veto: bool` —— 是否触发一票否决
- `reasons: list[str]` —— 不可判定 / 否决的具体原因

⚠️ **score=0 与 score=None 不一样**:`score=0` 是「判了,判 0 分」;`score=None` 是「没判」,走 quarantine 而不进负例池。

### 4.2 scout 不经过 `split_archive`

`evaluation.cli::evaluate()` 当前吃的是 `assembler.schema.Sample` 对象(从 `split_archive` 解出来),scout 必须吃 P2 dict。**原因**:scout 设计目标之一就是不接入 executor 的归档路径,保持 `交接面是格式不是 import`(不变量 2)。

代码骨架:

```python
# trajectory_pipeline/evaluation/scout/io.py

def _score_one_sample(sample: Mapping[str, Any]) -> dict[str, int | None]:
    """对一条 P2 样本跑 D-2/D-4/D-5/D-6,返回各维 score(允许 None)。"""
    verdicts: dict[str, int | None] = {}
    for dim in ("d2", "d4", "d5", "d6"):
        verdict = _SCORERS[dim](sample)            # 见 §5.1 的解耦改造
        verdicts[dim] = verdict.score
    return verdicts
```

### 4.3 评分器从吃 `Sample` 改为吃 P2 dict

`d2_scorer.score_d2(sample)` 现在通过 `getattr(sample, "observations")` 取观察;改成吃 P2 dict 时,用 `sample.get("six", {}).get("observations")` 或顶层 key,具体由 `evaluation/scout/io.py::_P2_KEYS` 维护,**不**改 scorer 自身(`不要把 scorer 接到 io.py` —— 保持 §1 当前形态以让原 `evaluation.cli` 不破)。

具体做法:`scout.io` 自己实现一组 `_score_d2_p2(sample_dict)` 等薄包装,内部调用 `assembler.schema.split_archive` 反向重建 `Sample`,**或**根据 P2 形态直读字段。后者更快但需双签 `evaluation/golden/builder.py::_scan_p2_samples` 的字段表(已存在,可对照)。

---

## 5. 三态分流

### 5.1 模块目录

```text
trajectory_pipeline/evaluation/scout/
    __init__.py
    cli.py              # 子命令定义,nargs 风格
    io.py               # P2 读取 + JSONL 原子写
    policy.py           # classify() 纯函数,无副作用
    fail_closed.py      # NOT_RUN_NOTES 白名单镜像
    types.py            # ScoutStatus / ScoutRecord TypedDict
```

### 5.2 ScoutRecord 形状(`types.py`)

```python
from typing import Any, Literal, TypedDict

ScoutStatus = Literal["scored", "undetermined", "not_run"]
ScoutTag = Literal["d2_failed", "d4_failed", "d5_failed", "d6_failed", "veto"]

class DimensionScore(TypedDict):
    dimension: Literal["d2", "d4", "d5", "d6"]
    score: int | None
    is_veto: bool
    reasons: list[str]

class ScoutRecord(TypedDict):
    sample_id: str
    task_id: str
    run_id: str
    status: ScoutStatus
    scores: list[DimensionScore]            # 4 条
    tags: list[ScoutTag]                    # 空 = 无负向信号
    evidence_supplement: dict[str, Any]     # 可选 L5 附加,缺件留空
```

### 5.3 classify() 纯函数(`policy.py`)

```python
def classify(rec: ScoutRecord) -> Literal["positive", "negative", "quarantine", "skip"]:
    if rec["status"] != "scored":
        # undetermined → quarantine,not_run → skip(不落盘)
        return "quarantine" if rec["status"] == "undetermined" else "skip"
    negatives = {s["dimension"] for s in rec["scores"] if s["score"] is not None and s["score"] <= 2}
    vetoes = {s["dimension"] for s in rec["scores"] if s["is_veto"]}
    if negatives or vetoes:
        return "negative"
    return "positive"
```

判定逻辑:
- 任意维度 ≤ 2 → `negative`,打 `d2_failed` / `d4_failed` / `d5_failed` / `d6_failed` tag
- 任意维度 `is_veto` → `negative`,打 `veto` tag
- 否则(`scored` 且全部 ≥ 3)→ `positive`
- `undetermined` → `quarantine`
- `not_run`(fail-closed 短路)→ `skip`,只进 `--stats` 计数

### 5.4 NOT_RUN 白名单镜像(`fail_closed.py`)

```python
# 直接复用 executor 端的常量,**不许复制字面量**
from trajectory_pipeline.executor.arbitration import L5_FAIL_CLOSED_NOTES
```

§10 不变量 5 用 AST 断言:**`scout/fail_closed.py` 不含四条字符串字面量**,只允许 `from ... import ...` 语句。

---

## 6. from-scout 子命令

### 6.1 子命令签名

```python
# evaluation/golden/builder.py 新增
p_from_scout = sub.add_parser(
    "from-scout",
    help="只读 scout 三件套 → candidates.jsonl(quarantine 永不进入)",
)
p_from_scout.add_argument("--scout-dir", required=True,
                          help="指向 evaluation/scout/data/<ts>/")
p_from_scout.add_argument("--force", action="store_true")
p_from_scout.set_defaults(func=cmd_from_scout)
```

### 6.2 `cmd_from_scout` 数据流

1. 读 `<scout-dir>/positive_candidates.jsonl` 和 `negative_candidates.jsonl`
2. 拒绝 `quarantine.jsonl`(**绝对 import 校验 + 字段白名单共同保证**,见 §9 契约测试)
3. 把 `ScoutRecord` 投影成 `candidates.jsonl` 的标准候选 dict,保留:
   - `golden_id`(沿用 P2 `sample_id`)
   - `task_id` / `run_id` / `sample_id`(供 D9 retest 使用)
   - `code`(沿用 `golden.builder._scan_p2_samples` 的字段表)
   - `dimension_scores`(整段塞进候选,供人审时一眼看到机器判了哪几维几分)
   - `tags` / `source_kind`(新增 `golden_kind="positive_scored"` / `negative_scored`)
   - `golden=None` —— 仍等人标
   - `retest=None`

### 6.3 旧 `candidates` 加 `--deprecated` 警告

```python
p_cand = sub.add_parser("candidates", help="⚠️ deprecated · 推荐 from-scout")
p_cand.add_argument("--dry-run", action="store_true")
p_cand.add_argument("--force", action="store_true")
p_cand.set_defaults(func=cmd_candidates)
```

`cmd_candidates` 入口加一行 `print("WARNING: 'candidates' 是 deprecated 入口,推荐 'from-scout'", file=sys.stderr)`,**不**作 silent 兼容 —— 因为旧产物 `candidates.jsonl` 现在带 `*_from_scout` 字段,两种路径跑出来的候选不能混。

---

## 7. 可选 L5 证据附加

### 7.1 接口与定位

`--mediabrowse-dir` 路径(`evaluation/scout/cli.py`):

```python
ap.add_argument("--mediabrowse-dir", default=None,
                help="可选:指向 output/pipeline/mediabrowse/,L5 evidence 仅作 D-3 补充")
```

### 7.2 索引与匹配

`scout/io.py::_mediabrowse_index(dir) -> dict[tuple[str, str], dict[str, Any]]`:
- key = `(task_id, url)`
- value = `BrowserProbeResult.l5_outcome` 的最小子集(`branch`, `evidence`, `note`)

`scout.io._attach_mediabrowse(records, mediabrowse_dir)`:
- 命中 → `evidence_supplement["l5"] = {branch, evidence, note}`
- 不命中 → `evidence_supplement` 字段留空 dict,**不**算缺件

### 7.3 与 D10 的一致性

D10 报表新增一个视图:`agreement --include-scout-supplement`,可读 `evidence_supplement` 的 L5 branch 与候选的 `code.branch` 是否同源。这种"双源交叉"是新口径,本档 **不** 改变 D10 阈值或 D10_THRESHOLD。

---

## 8. 文件清单 & 代码签名

| #  | 路径                                                              | 操作         | 估行 |
| -- | ----------------------------------------------------------------- | ------------ | ---- |
| 1  | `trajectory_pipeline/evaluation/scout/__init__.py`                | 新增(空)    | 3    |
| 2  | `trajectory_pipeline/evaluation/scout/types.py`                   | 新增         | 30   |
| 3  | `trajectory_pipeline/evaluation/scout/io.py`                      | 新增         | 120  |
| 4  | `trajectory_pipeline/evaluation/scout/fail_closed.py`             | 新增         | 15   |
| 5  | `trajectory_pipeline/evaluation/scout/policy.py`                  | 新增         | 80   |
| 6  | `trajectory_pipeline/evaluation/scout/cli.py`                     | 新增         | 90   |
| 7  | `trajectory_pipeline/evaluation/cli.py`                           | 注册 `scout` 子命令 | +12   |
| 8  | `trajectory_pipeline/evaluation/golden/builder.py`                | 加 `from-scout` + `--deprecated` | +90  |
| 9  | `trajectory_pipeline/tests/contract/test_scout_contract.py`       | 新增         | 200  |
| 10 | `trajectory_pipeline/tests/contract/test_shared_literal_sync.py` | 新增         | 80   |
| 11 | `trajectory_pipeline/tests/unit/test_scout_policy.py`             | 新增         | 200  |
| 12 | `trajectory_pipeline/docs/04-运行手册.md`                        | 加 scout 工作流一节 | +50 |
| 13 | `trajectory_pipeline/docs/05-现状与验收.md`                      | "已跑通"表加一行  | +3   |

合计:约 1000 行新增(含契约测试)。

---

## 9. 契约测试

### 9.1 `tests/contract/test_scout_contract.py`(AST 验)

```python
import ast
from pathlib import Path

SCOUT_DIR = Path("trajectory_pipeline/evaluation/scout")
DEPRECATED_BRANCHES = "executor.branches"
DEPRECATED_NOTES = "executor.arbitration.L5_FAIL_CLOSED_NOTES"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {ast.unparse(n) for n in ast.walk(tree)
            if isinstance(n, (ast.Import, ast.ImportFrom))}


def test_scout_does_not_import_executor_cli():
    for p in SCOUT_DIR.glob("*.py"):
        assert "trajectory_pipeline.executor.cli" not in _imports(p)


def test_scout_does_not_import_executor_top_level():
    for p in SCOUT_DIR.glob("*.py"):
        asserts = {imp for imp in _imports(p) if imp.startswith("trajectory_pipeline.executor")}
        assert not asserts, "交接面是格式不是 import"


def test_fail_closed_only_imports_from_executor():
    p = SCOUT_DIR / "fail_closed.py"
    imports = {imp for imp in _imports(p)
               if imp.startswith("trajectory_pipeline")}
    assert imports == {"trajectory_pipeline.executor.arbitration.L5_FAIL_CLOSED_NOTES",
                       "trajectory_pipeline.executor.review_queue.AUTO_REVIEW_NOTES"}
    src = p.read_text(encoding="utf-8")
    for tok in ("credential_leak", "driver_missing", "timeout", "driver_error"):
        assert tok not in src, f"fail-closed 字面量只能从 executor 镜像: {tok}"
```

### 9.2 `tests/unit/test_scout_policy.py`(`classify()` 纯函数矩阵)

最小覆盖:
- 4 维全 ≥ 3 → `positive`
- 任一维 = 2 → `negative`,tag 含 `d*_failed`
- 任一维 `is_veto=True` → `negative`,tag 含 `veto`
- 任一维 `score=None` → `quarantine`
- `status=not_run` → `skip`
- 边界:score=3 全过且无 veto → `positive`(阈值的语义)

### 9.3 端到端 smoke 测试

`tests/integration/test_scout_e2e.py`:
- 用 3 条 fixture(P2 形态手写)跑 scout
- 验证:`positive_candidates.jsonl` 条数 = fixture 中"全 ≥ 3"条数;`negative_candidates.jsonl` 条数 = fixture 中"任一 ≤ 2"条数;`quarantine.jsonl` 条数 = fixture 中"任一 None"条数
- 验证:三态合计 == 输入样本数

### 9.4 `test_shared_literal_sync.py`(沿用 §5 风格)

```python
def test_l5_fail_closed_notes_unique_source():
    p1 = Path("trajectory_pipeline/executor/arbitration.py")
    p2 = Path("trajectory_pipeline/evaluation/scout/fail_closed.py")
    src2 = p2.read_text(encoding="utf-8")
    # scout 端**只能 import**,不许写字面量
    assert "credential_leak" not in src2
    assert "driver_missing" not in src2
    assert "timeout" not in src2
    assert "driver_error" not in src2
```

---

## 10. 不变量(从 08 §4 沿用,增补 v04 特有)

| #   | 不变量                                                                   | 落地形式                                |
| --- | ------------------------------------------------------------------------ | --------------------------------------- |
| 1   | **绝对导入**:所有 import 走 `trajectory_pipeline.xxx`                    | mypy strict + ruff E402/TID252          |
| 2   | **交接面是格式不是 import**:scout 不 import executor.cli / executor 顶层  | `test_scout_contract.py`(§9.1)        |
| 3   | **三态**:`scored` / `undetermined` / `not_run` 一致,与 `GateStatus` 同款  | `types.py` TypedDict                    |
| 4   | **缺件 ≠ 扣分**:score=None 进 quarantine,不进负例                        | `policy.classify()` 单测 + smoke 测试    |
| 5   | **共享字面量两处必同步**:13 分支标签、fail-closed 白名单                  | `test_shared_literal_sync.py`           |
| 6   | **黄金集不虚构**:只接受真实 scout 产物,`quarantine.jsonl` 永不入 candidates | `cmd_from_scout` 字段白名单 + smoke 验  |
| 7   | **L6 人工不复用 scout**:人审走 `annotations.jsonl`,不走 `from-scout`     | 命名空间与文件路径分开                  |
| 8   | **Score≠Undetermined**:score=0 ≠ score=None                              | `policy.classify` 不把 0 当 None 看待    |
| 9   | **D10 阈值不降**:本档不修改 `D10_THRESHOLD=0.90`,仅新增 scout 视图       | `cmd_agreement` 未改                    |

---

## 11. 验收信号

| 信号                                          | 期望                                                              |
| --------------------------------------------- | ----------------------------------------------------------------- |
| `python -m ...evaluation.cli scout --stats`   | 三态合计 == P2 总样本数,无丢失                                    |
| `from-scout` 入 `freeze` 路径                 | 报错只来自 IO/JSON 损坏,不来自分类逻辑                            |
| D-10 重测(可选)                              | 纯人工子集 vs scout 驱动子集各自计一致率,二者可同向解读           |
| `test_scout_contract` 全部 AST 断言          | 5 条契约测试全绿,关键:scout 不 import executor 顶层               |
| `test_shared_literal_sync`                   | 13 分支标签 / 4 fail-closed note 字面量源唯一                     |

---

## 12. 风险与回退

| 风险                                     | 触发场景                                    | 回退路径                                                |
| ---------------------------------------- | ------------------------------------------- | ------------------------------------------------------- |
| 三态分流阈值(全 ≥ 3)与项目语境不符       | 首批测试下来"全 ≥ 3"正例率太低(< 5%)       | `policy.classify` 内阈值常量化 + `--threshold` CLI 参数 |
| `dim_score=None` 量产,quarantine 膨胀    | P1 字段不齐的样本多                        | 走 v05:加 rationale 侧信号(`GateStatus`)作 fallback    |
| P2 反向重建 `Sample` 字段不齐            | `assembler.schema` 改了字段,P2 没改         | 走 `assembler.schema` 的 P2 反序列化器,作 v05+          |
| 与 `cmd_agreement` 旧产物混算 D10        | 两次 `candidates.jsonl` 形状不一           | 加 `manifest.schema_version=2`,旧 D10 报告自动拒绝      |

**回退开关**:`evaluation.cli scout --no-fail-closed` 临时禁用 fail-closed 短路,允许所有样本跑一遍评分。**不**留 `candidates` 子命令的兼容开关(§6.3 已强制提醒)。

---

## 13. 实施顺序(本档 §8 编号一致)

1-2. 写 `types.py` + `fail_closed.py`(纯声明,无副作用)
3. 写 `io.py`(含 P2 JSON 读取 + 三件套原子写)
4. 写 `policy.py`(`classify()` 纯函数)
5. 写 `cli.py`(子命令与参数)
6. 在 `evaluation/cli.py` 注册 `scout`
7. `cmd_from_scout` + 旧 `candidates` 的 `--deprecated`
8. `test_scout_contract.py` 与 `test_shared_literal_sync.py`(契约测试)
9. `test_scout_policy.py`(单测)
10. `test_scout_e2e.py`(集成)
11. docs 同步(04-运行手册、05-现状与验收)
12. 跑 end-to-end:`run → scout → from-scout → annotate(几条)→ freeze → agreement`

---

## 14. 变更记录

| 日期       | 版本 | 说明                                       |
| ---------- | ---- | ------------------------------------------ |
| 2026-10-14 | v04  | 初稿,落实 08 的 scout 实施层细节           |
