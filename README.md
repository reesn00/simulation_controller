# trajectory_pipeline · Agent 轨迹合成管线

采集**可归因**的 Agent 浏览器操作轨迹：任务生成 → 代码执行 → 语义感知 → 样本组装 → 评估。

核心主张：**代码层做护栏、LLM 层做探索、persona 管输入分布**。

---

## 怎么跑

```text
任务生成 ──▶ 代码执行循环 ──▶ 语义感知 ──▶ 组装切分 ──▶ 评估
 taskgen      executor         perception     assembler   evaluation
                  │                ▲
                  │                └── Perceptor 可替换（规则 rule / LLM llm）
                  ▼
          camoufox 浏览器（patched Firefox + Playwright 进程内 API）
```

`executor/orchestrator.py` 里有 `if`，但**没有一句在判断业务语义**——「这是不是播放站 / 有没有播放控件 / 是不是播放页」全在 `Perceptor` 里，控制流只负责按答案分流。遍历谁、按什么顺序、点哪个、记什么、何时终止、失败样本是否入池——**全部是代码**。

分工是：**代码层做护栏**（预算 / 终止判定 / 处置映射 / 分支记账 / fail-closed），**LLM 层做探索**（判断语义、挑下一步动作）。agent runtime 自己选动作，**终局仍由代码验收**（见[设计方案 15](trajectory_pipeline/docs/设计方案/15-执行器与agent-runtime重新定义.md)）。

感知层在链上的四个位置（判断点）：

| # | 判断题 | 代码拿它做什么 | agent 链问不问 |
|---|---|---|---|
| ①.5 | `CLASSIFY_SEARCH_LINKS` | 候选四分类，按发现路径分发 | ✅ 问（搜索页不归 agent 管） |
| ② | `IS_REACHABLE` | 跳过登录墙 / 地区限制 / 错误页 | ❌ 不问（gatekeeper 逐步拦截更早也更硬） |
| ③ | `FIND_PLAY_CONTROL` | 拿定位去点播放控件 | ❌ 不问（agent 自己找——这就是交给它的那 8 处语义判断） |
| ④ | `PLAYER_OK` | 成功 or 标记未验证 | ✅ **只问它**（终局验收） |

① `SELECT_PLAY_SITES` 保留未删——站内搜索结果页复用 + 金标重放。

> **执行层**：生产链默认 **camoufox**（`DEFAULT_KIND = "camoufox"`，2026-10-22 owner 裁定）。obscura 未删——它是存量 P1/P2 的方言，`--driver obscura` 显式点名仍可跑（回溯路径）。**两个面不混池**：元素键名与词表都随面变，详见[设计方案 04](trajectory_pipeline/docs/设计方案/04-camoufox执行层替换方案.md)。

---

## 模块地图

```text
trajectory_pipeline/
├─ taskgen/       模块 1  任务生成 + persona 画像库
├─ executor/      模块 2  代码执行循环 —— 控制流在这里，不在任何 LLM 里
│  ├─ browser/    执行层（PageDriver 协议 + camoufox / obscura 实现）
│  ├─ steps/      各环节（search / visit / aggregator / article / site_search / agent_*）
│  ├─ review/     L4 复核层（VLM）
│  └─ probe/      媒体探测（ffprobe / static）
├─ perception/    模块 3  语义感知 —— 可替换插件（rule / llm + 注入工厂）
├─ rationale/     模块 4  rationale 边写边生成 + 一致性闸门三查
├─ assembler/     模块 5  组装与切分（切条 schema / 观察视图 / P2 落盘 / P3 三视图 / T 任务视图）
├─ evaluation/    模块 6  评估体系（D-2/D-4/D-5/D-6 + 黄金集 v6 + scout 门禁 + LS 双向）
├─ common/        跨模块地基（元素方言原语 elements.py + 原子写 atomic.py）
├─ llm/           LLM 客户端 + 结构化输出解析（全树唯一出口）
├─ storage/       P3 / T 视图读写收拢点
├─ toolcatalog/   训练侧工具声明的**唯一真源**（跨层地基）
├─ docs/          指引 01–08 + 设计方案 + contracts/ + 依赖说明/
└─ output/pipeline/   全部产物（决策 D8，gitignored）
```

依赖方向**单向、禁止回指**：

```text
common ← llm ← {taskgen, perception, rationale}
              ↑
      executor ──→ assembler ──→ storage
         │              ↑
         └──────────────┘
evaluation ──→ {executor, assembler, perception}   ※ 唯一的例外，且方向是反的
```

四条不可破的边（都有 AST 契约测试盯着）：

1. `executor` 只依赖 `perception` 的**协议**（`perception/base.py`），不依赖任何实现——这是规则版与 LLM 版能无痛替换的唯一保证；
2. `perception` **不得 import `executor`**——输入只有 `Observation` 值对象，不持页面句柄、不回调控制流；
3. `executor` **不得 import `llm`**——LLM 客户端由 `cli` 在边界组装后注入（`rationale_writer` / `AgentBackend`）；
4. `assembler` **不得 import `executor`**——P1 贵、P2 便宜，重建 P2 必须独立于执行端当前版本。

`evaluation.cli` 反向 import 上游是**有意的例外**：验收方（存档选取口径 + 切样本）必须与生产方一致；而 `executor` 不得反向知道 `evaluation` 的存在。

---

## 快速开始

```powershell
uv sync --group dev
# camoufox 0.5.8 / playwright 1.62.0 已钉在主依赖里（D11 目标执行层）；
# 首次装还要下浏览器：uv run camoufox fetch

# OBSCURA_EXE **只在显式点名 obscura 时才需要**（回溯路径）。缺省执行层是
# camoufox，走进程内 API，不依赖它。缺失即报错，不猜路径。
# setx OBSCURA_EXE "C:\...\obscura.exe"

# 探一次执行层能力（唯一需要真实浏览器的自检）
uv run python -m trajectory_pipeline.executor.cli check
uv run python -m trajectory_pipeline.executor.cli check --driver camoufox --probe-url https://www.baidu.com/

$P = trajectory_pipeline/output/pipeline
uv run python -m trajectory_pipeline.executor.cli gen -n 20 --seed 7 --out $P/plan.json   # 采样任务，不联网
uv run python -m trajectory_pipeline.executor.cli run --plan $P/plan.json --limit 5         # 跑（联网）
uv run python -m trajectory_pipeline.executor.cli run --task-id T001 --title 功夫           # 单条跑：**--title 必给**

# 组装（不联网）
uv run python -m trajectory_pipeline.executor.cli build --overwrite        # P1 → P2 → P3
uv run python -m trajectory_pipeline.executor.cli task-view --all --overwrite   # P1 → T 任务视图

# 人工复核：导出队列 → 填 verdict → 回填
uv run python -m trajectory_pipeline.executor.cli review --write $P/review_queue.jsonl --show 3
uv run python -m trajectory_pipeline.executor.cli review --verdicts $P/review_queue.jsonl --apply

# 门禁与报表（不联网）
uv run python -m trajectory_pipeline.executor.cli report --out $P          # 分支分布 + 成功域名分布
uv run python -m trajectory_pipeline.executor.cli check-archives           # 存档完整性门禁
uv run python -m trajectory_pipeline.executor.cli replay --perceptor rule   # 感知层回放（不开浏览器）

# 评估（独立入口，不挂进 executor.cli）
uv run python -m trajectory_pipeline.evaluation.cli                        # D-2/D-4/D-5/D-6

uv run python -m pytest -q
```

> ⚠️ **`run` 的 `--title` 缺省即报错**，不拿 `--task-id` 顶替：少了它会搜成 `T001 在线观看`（**轮胎** T001），产出整批理由通顺的假负样本且**零报错**。

**完整命令表与环境变量见 [常用命令速查](trajectory_pipeline/docs/08-常用命令速查.md)**（命令清单的唯一真源，一行一条、复制即用）。

---

## 数据契约与产物

| 契约 | 落点 | 内容 |
|---|---|---|
| **P1 观察存档** | `output/pipeline/<task_id>__<run_id>.json` | 动作 + 工具参数 + **观察原文全文**（真实回放，非 LLM 转述） |
| **P2 单条样本** | `output/pipeline/samples/<sample_id>.json` | 六件套 + provenance + gate 标记（一条 = 一个决策单元） |
| **P3 训练视图** | `output/pipeline/views/<sample_id>_{messages,openai,meta}.json` | 训练框架可读形态 |
| **T 任务视图** | `output/pipeline/task_views/<task_id>__<run_id>.json` | per-Run 三视图合一（LS 上传 / 任务级复核） |
| 负样本池 | `output/pipeline/negative.jsonl` | 失败分支样本（带 `branch`） |
| 复核队列 | `output/pipeline/review_*.jsonl` | 人工裁定（`verdict` + `reviewed_by`） |

**负样本是强制产出，不是副产品**——每条失败分支都必须有对应样本入库。但 `unresolved` / `trailer_suspect` / `player_unverified` / `site_search_routed` **不是负样本**（「没判出来」≠「看不了」），混进池里会污染它。字段级说明见 [数据契约](trajectory_pipeline/docs/03-数据契约.md)。

> **观察是真实回放，不是 LLM 转述**——这条一旦破，rationale 就没有锚。

---

## 读代码的入口顺序

新接手建议按这个顺序读（每一步都能独立跑通）：

| 顺序 | 读什么 | 回答什么问题 |
|---|---|---|
| 1 | [`executor/orchestrator.py`](trajectory_pipeline/executor/orchestrator.py) | 控制流长什么样、四个判断点在哪儿分流 |
| 2 | [`perception/base.py`](trajectory_pipeline/perception/base.py) + [`perception/questions.py`](trajectory_pipeline/perception/questions.py) | 判断题的协议与注册表（判断点 → 分支封闭集的映射） |
| 3 | [`executor/agent_loop.py`](trajectory_pipeline/executor/agent_loop.py) | agent 形态的七步流水线与校验 |
| 4 | [`executor/branches.py`](trajectory_pipeline/executor/branches.py) | 分支封闭集 + 贴标签前的前置事实核 |
| 5 | [`assembler/schema.py`](trajectory_pipeline/assembler/schema.py) | P1 怎么切成 P2（切条规则） |
| 6 | [`assembler/views.py`](trajectory_pipeline/assembler/views.py) | P3 三视图的渲染语义（训练侧真正看到的东西） |
| 7 | [`toolcatalog/__init__.py`](trajectory_pipeline/toolcatalog/__init__.py) | 工具面 × 方言轴——**同名工具不同参**的源头 |

---

## 文档

### 项目指引

| 文档 | 内容 |
|---|---|
| [01-架构总览.md](trajectory_pipeline/docs/01-架构总览.md) | 模块地图、控制流与四个判断点、失败分支全集、执行层、感知层、persona 六维 |
| [02-避坑指南.md](trajectory_pipeline/docs/02-避坑指南.md) | **9 类 77 条**，症状写成终端里真正会看到的那句话，可全文搜索 |
| [03-数据契约.md](trajectory_pipeline/docs/03-数据契约.md) | P1/P2/P3/T 契约表、`steps[]` 三条口径 |
| [08-常用命令速查.md](trajectory_pipeline/docs/08-常用命令速查.md) | **命令清单唯一真源**——一行一条、复制即用，附「缺了就报错不猜」必填项表 |
| [04-运行手册.md](trajectory_pipeline/docs/04-运行手册.md) | 每条命令「联网与否 / 作用」口径表、环境变量、门禁与输出目录 |
| [05-现状与验收.md](trajectory_pipeline/docs/05-现状与验收.md) | 索引页——内容见 [`计划进展/`](trajectory_pipeline/docs/计划进展/) |
| [计划进展/00-总览.md](trajectory_pipeline/docs/计划进展/00-总览.md) | **跑到哪儿了、接下来做什么**（不含历史），按 9 个功能域分册 |
| [06-端到端流程.md](trajectory_pipeline/docs/06-端到端流程.md) | 一条数据从骨架走到训练样本的完整路径 |
| [07-人工复核与判读手册.md](trajectory_pipeline/docs/07-人工复核与判读手册.md) | 人工判读的操作手册——要判的事、怎么判、填哪、确认后记哪 |
| [tools.md](trajectory_pipeline/docs/tools.md) | 工具调用说明（总目录）——按工具形态分大类 |
| [依赖说明/camoufox.md](trajectory_pipeline/docs/依赖说明/camoufox.md) | camoufox 依赖手册：安装、配置、**本机实测记录与判据** |

### 设计方案

- [00-总体方案.md](trajectory_pipeline/docs/设计方案/00-总体方案.md) —— 设计基线：决策 D1–D10、模块设计、里程碑、风险与待决
- [01-模块3-可替换感知层.md](trajectory_pipeline/docs/设计方案/01-模块3-可替换感知层.md) —— 插件化机制完整规格 + 七不变式
- [02-rubric与代码对齐.md](trajectory_pipeline/docs/设计方案/02-rubric与代码对齐.md) —— 评分标准逐维度 ↔ 代码的对齐账。**改 rubric 或改控制流之前先读**
- [03-rubric修订与实施方案.md](trajectory_pipeline/docs/设计方案/03-rubric修订与实施方案.md) —— rubric v1.1 改稿清单 + 五个代码变更。**§9 是实施结果**
- [04-camoufox执行层替换方案.md](trajectory_pipeline/docs/设计方案/04-camoufox执行层替换方案.md) —— D11 修订 D7：camoufox 取代 obscura。含实测取证与 13 原语映射表
- [15-执行器与agent-runtime重新定义.md](trajectory_pipeline/docs/设计方案/15-执行器与agent-runtime重新定义.md) —— 执行器 / agent runtime 分界（D-A6–D-A11），「LLM 层做探索、代码层做护栏」的完整定义
- [14-完整任务视图与LS上传.md](trajectory_pipeline/docs/设计方案/14-完整任务视图与LS上传.md) —— T 视图设计与 LS 上传链路
- [在线视频场景rubric.md](trajectory_pipeline/docs/设计方案/在线视频场景rubric.md) —— 评分标准本体

**开发纪律见 [`CLAUDE.md`](CLAUDE.md)**——新代码一律写进 `trajectory_pipeline/`，存量 `archive/v1/` 只读不 import。
