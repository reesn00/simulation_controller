# CLAUDE.md

## 项目

采集**可归因**的 Agent 浏览器操作轨迹：任务生成 → 代码执行 → 语义感知 → 样本组装 → 评估。

核心主张：**代码管事实层、LLM 管语言层、persona 管输入分布**。

仓库里有**两代架构并存**：

| | 位置 | 状态 |
|---|---|---|
| **v2 轨迹合成管线** | `trajectory_pipeline/` | **当前唯一开发目标**。全部新功能、新修复写在这里 |
| **v1 存量管线** | `archive/v1/`（原 `simulate_serve/` `orchestration/` `gdr/` `etl/` `label_studio/` `tool_runtime/` `data_refiner/`，2026-10-09 归档） | **冻结**。只读参考：可读代码抄思路，**不可 import、不可修改** |

一句话区别：v1 是「**远端 QwenPaw agent 当驾驶员**，本地只当模拟用户 + 裁判」；
v2 把那颗心脏换掉——「**本地代码当驾驶员、obscura 当浏览器、Perceptor 当可插拔感知器**」。
v2 要采集的是**可归因的证据**，而 v1 的观察经远端 LLM 转述后锚会被削弱。

---

## ⚠️ 开发纪律（先读这一节，违反即视为架构回退）

1. **新代码一律写入 `trajectory_pipeline/`。** 任何新模块、新 CLI、新测试、新文档都落在新树内。
2. **存量仅供功能参考——禁止 import、禁止修改。**（2026-10-09 起存量代码归档至 `archive/v1/`，纪律不变。）需要存量某个能力时的正确姿势是**照着读一遍然后在新树重写**，而不是 `from simulate_serve.x import y`。
3. **两处例外（不是破例）**：仓库根 `pyproject.toml`（`testpaths` 必须含 `trajectory_pipeline/tests`）与 `.gitignore`（`trajectory_pipeline/output/`）。这两处是构建/门禁接线，改它们是为了让新树能被门禁覆盖、产物不入库。
4. **跨树/外部能力走进程边界**：MCP 协议 / HTTP，与 Python 导入边无关。这条让「不 import 存量」与「能复用外部能力」同时成立。
5. **产物隔离**：新树**禁止读取仓库根 `output/`**，产物全部落 `trajectory_pipeline/output/pipeline/`（决策 D8）。读了根 `output/` 就继承了存量那套隐性耦合。
6. **导入姿势唯一**：新树一律 `trajectory_pipeline.<模块>` 绝对导入，禁止顶层裸导入；`trajectory_pipeline` **刻意不进** `[tool.uv.workspace]`。理由是存量事故——gdr 成为 workspace 成员后同时以顶层风格和 `gdr.` 前缀风格被加载，同一份源码产生两套类对象，跨边界 `isinstance` 静默判 `False`，`usage_prune` 从未真正执行。

### 跨两棵树的红线

- **凭据不得提交、打包、复制到测试、文档或日志。**
- **不保存 raw CoT / Cookie / Authorization Header / 浏览器 Profile。**「思维链」按加工状态分两类：**raw CoT**（未经 gdr 精修的原始输出，受此红线约束）与 **refined CoT**（经 `thought_refactor` 精修的训练制品，**不受限**——CoT SFT 需要它，不要「修复」掉）。新树的落地方式是在 `executor/browser/obscura_driver.py` 里写死 `FORBIDDEN_TOOLS` + `_assert_tool_allowed` 闸门，所有 tool 调用统一走 `_call`，这是唯一入口。
- **fail-closed（I4）**：能力不可用时返回 `answer=None`，**绝不猜**；且 **`None` 不中断采集，只有 `False` 才中断**——fail-closed 管的是「结论」不是「采集」。
- **禁止投票/集成**（生产链上），分歧必须暴露给人看。

### 控制流铁律：LLM 是传感器，不是驾驶员

**决策权在控制流（代码），感知内容由 `Perceptor.decide` 给。**
遍历谁、按什么顺序、点哪个、记什么、何时终止、失败样本是否入池——**全部是代码，LLM 无否决权**。

`executor/orchestrator.py` 里有 `if`，但**没有一句在判断业务语义**——「这是不是播放站 / 有没有播放控件 / 是不是播放页」全在 `Perceptor` 里，控制流只负责按答案分流。详见[架构总览 · 四个判断点](trajectory_pipeline/docs/01-架构总览.md)。

### 三条容易踩塌的判断

这三条不是风格偏好，是踩过之后写进规范的：

- **每条分支都必须有对应样本入库**——负样本不是副产品。反过来，`unresolved` / `trailer_suspect` / `player_unverified` **不是负样本**，混进池里会污染它。
- **并池是写入时的动作，不是读取时的推导**——新分支（如 `player_unverified`）**不回填历史存档**。批次边界前后分布会跳变，不记批次元数据就会读成「质量变好了」。
- **贴标签前核前置事实**（`branches.BRANCH_PRECONDITION`）——分支名是结论的标签，而定义里带前提。前置事实不成立就降级为 `unresolved`。**这张表要短**，长得跟分支表一样长就没人维护了。

---

## 常用命令

```powershell
uv sync --group dev

# ── 执行层 ─────────────────────────────────────────────
uv run python -m trajectory_pipeline.executor.cli check              # 探 obscura 能力（唯一需真实浏览器的自检）
uv run python -m trajectory_pipeline.executor.cli run --task-id T001 --title 功夫
uv run python -m trajectory_pipeline.executor.cli run --plan <plan.json> --limit 20 --dry-run
uv run python -m trajectory_pipeline.executor.cli run --plan <plan.json> --limit 5 --perceptor llm

# ── 任务生成层（不联网）────────────────────────────────
uv run python -m trajectory_pipeline.executor.cli gen -n 20 --seed 7 --out <plan.json>
uv run python -m trajectory_pipeline.executor.cli check-persona

# ── 组装层（不联网）─────────────────────────────────────
uv run python -m trajectory_pipeline.executor.cli build          # P1 → P2 → P3 落盘
uv run python -m trajectory_pipeline.executor.cli build --overwrite   # 重建（**默认不覆盖**）

# ── 人工复核与报表 ─────────────────────────────────────
uv run python -m trajectory_pipeline.executor.cli review --write <queue.jsonl> --show 3
uv run python -m trajectory_pipeline.executor.cli review --verdicts <queue.jsonl> --apply
uv run python -m trajectory_pipeline.executor.cli report --out trajectory_pipeline/output/pipeline
uv run python -m trajectory_pipeline.executor.cli check-archives          # 存档完整性门禁（不联网）
uv run python -m trajectory_pipeline.executor.cli replay --perceptor rule   # 感知层回放（不开浏览器）

# ── 整批回滚（默认只列不删）────────────────────────────
uv run python -m trajectory_pipeline.executor.cli purge-run --run-id <run_id>
uv run python -m trajectory_pipeline.executor.cli purge-run --run-id <run_id> --apply

# ── 门禁 ───────────────────────────────────────────────
uv run python -m pytest -q
```

> ⚠️ **`run` 的 `--title` 缺省即报错**，不拿 `--task-id` 顶替。
> 实测：少了它就搜成 `T001 在线观看`（**轮胎** T001），产出整批理由通顺的
> 假负样本且**零报错**。见避坑指南 §5.13。

`run` / `check` 需要 `OBSCURA_EXE`，**缺失即报错，不猜路径**；`--perceptor llm` 另需 `TRAJECTORY_LLM_BASE_URL` / `_MODEL` / `_API_KEY`，**同样缺失即报错不猜**。`TRAJECTORY_LLM_TIMEOUT_S` 默认 30s 对判断点 ① 不够，跑真实批次先调到 90。`setx` 只对新终端生效。

**key 只走环境变量**：代码里没有任何凭据字面量，`LLMConfig.__repr__` 刻意不吐 key，`test_llm.py::TestNoCredentialInSource` 盯着源码里不许出现 `sk-` / `bgw_` 这类前缀。

> 门禁 `testpaths = ["trajectory_pipeline/tests"]`（v1 测试树 2026-10-09 随代码归档退役出默认门禁，手动跑法见 [`archive/v1/README.md`](archive/v1/README.md)）。
> 历史教训仍然有效：gdr/tests 曾漏在门禁外，360+ 条不跑，掩盖了 2 处生产代码从未执行——
> **门禁必须显式枚举每一棵在开发的树，漏一棵 = 静默失效不可见**。

**每次真实运行后都要逐条打开存档读 evidence 核对**——固定动作不是可选项。设计缺陷基本是离线测试测不出来的（fixture 是我们自己造的，规则和 fixture 一起错，测试照样绿）。这条现在是命令了：`cli check-archives` 是它的机械版；**判断「结论对不对」仍不在门禁里**，那要人读证据。

---

## 文档

### 项目指引（`trajectory_pipeline/docs/`）

| 文档 | 内容 |
|---|---|
| [01-架构总览.md](trajectory_pipeline/docs/01-架构总览.md) | 模块地图 + 依赖方向、控制流与四个判断点、失败分支全集、obscura 执行层、感知层七不变式、persona 六维、模块 5/6、**存量 v1 各目录职责** |
| [03-数据契约.md](trajectory_pipeline/docs/03-数据契约.md) | P1/P2/P3 契约表、`steps[]` 三条口径、P1 三处「不给就不可判定」的字段 |
| [04-运行手册.md](trajectory_pipeline/docs/04-运行手册.md) | 命令全表 + 联网与否、环境变量、门禁与输出目录、`replay` 口径 |
| [05-现状与验收.md](trajectory_pipeline/docs/05-现状与验收.md) | **已跑通什么、待办什么、实测数据**（会过时，用前先重跑命令核对） |
| [02-避坑指南.md](trajectory_pipeline/docs/02-避坑指南.md) | **9 类 52 条**，症状写成终端里真正会看到的那句话，可全文搜索 |

### 设计方案（`trajectory_pipeline/docs/设计方案/`）

- [00-总体方案.md](trajectory_pipeline/docs/设计方案/00-总体方案.md) —— v2 设计基线：决策 D1–D10、模块设计、四个判断点、失败分支、契约、里程碑、风险
- [01-模块3-可替换感知层.md](trajectory_pipeline/docs/设计方案/01-模块3-可替换感知层.md) —— 插件化机制完整规格 + 七不变式
- [02-rubric与代码对齐.md](trajectory_pipeline/docs/设计方案/02-rubric与代码对齐.md) —— 评分标准 `RUB-VID-PLAY-001` 逐维度 ↔ 新树代码的对齐账。**改 rubric 或改控制流之前先读**——它记的是两侧哪里已经错位，而错位不会自己显形
- [03-rubric修订与实施方案.md](trajectory_pipeline/docs/设计方案/03-rubric修订与实施方案.md) —— rubric v1.1 逐条改稿清单 + 五个代码变更（点文件、点函数）。**§9 是实施结果**（哪条做了、哪条没做、每条由哪个测试封着、方案漏掉了什么）。**要动 `branches.py` / `observation_view.py` / `integrity.py` 的先读**
- [在线视频场景rubric.md](trajectory_pipeline/docs/设计方案/在线视频场景rubric.md) —— 评分标准本体

### 存量 v1（均在 `archive/v1/` 下，按需查阅）

`docs/orchestration-design.md`、`docs/observability-label-studio.md`、`docs/label-studio-playbook.md`、`docs/label-studio-annotation-export.md`、`docs/设计方案/`、`docs/contracts/`、`gdr/docs/`。目录职责见[架构总览](trajectory_pipeline/docs/01-架构总览.md)末节。