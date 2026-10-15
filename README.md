# trajectory_pipeline · Agent 轨迹合成管线

采集**可归因**的 Agent 浏览器操作轨迹：任务生成 → 代码执行 → 语义感知 → 样本组装 → 评估。

核心主张：**代码管事实层、LLM 管语言层、persona 管输入分布**——LLM 是传感器，不是驾驶员。

---

## 怎么跑

```text
任务生成 ──▶ 代码执行循环 ──▶ 语义感知 ──▶ 组装切分 ──▶ 评估
 taskgen      executor         perception     assembler   evaluation
                  │                ▲
                  │                └── Perceptor 可替换（规则 W1 / LLM W3）
                  ▼
              obscura 浏览器（MCP 直调，观察真回放）
```

`executor/orchestrator.py` 里有 `if`，但**没有一句在判断业务语义**——「这是不是播放站 / 有没有播放控件 / 是不是播放页」全在 `Perceptor` 里，控制流只负责按答案分流。遍历谁、按什么顺序、点哪个、记什么、何时终止、失败样本是否入池——**全部是代码，LLM 无否决权**。

LLM 只有四个位置：

| # | 判断题 | 代码拿它做什么 |
|---|---|---|
| ① | `SELECT_PLAY_SITES` | 决定遍历哪些站 |
| ② | `IS_REACHABLE` | 跳过登录墙 / 地区限制 / 错误页 |
| ③ | `FIND_PLAY_CONTROL` | **拿 ref 去点** |
| ④ | `PLAYER_OK` | 成功 or 标记未验证 |

---

## 模块地图

```text
trajectory_pipeline/
├─ taskgen/       模块 1  任务生成 + persona 画像库
├─ executor/      模块 2  代码执行循环 —— 控制流在这里，不在任何 LLM 里
├─ perception/    模块 3  语义感知 —— 可替换插件（rule / llm + 注入工厂）
├─ rationale/     模块 4  rationale 边写边生成 + 一致性闸门三查       [闸门+writer 已建，未接线]
├─ assembler/     模块 5  组装与切分（schema + 观察视图 + 落盘 + P3 三视图）  [闸门未接线]
├─ evaluation/    模块 6  评估体系（D-2/D-4/D-5/D-6 已建，黄金集待建）
├─ common/        跨模块地基（无业务逻辑）
├─ llm/           LLM 客户端（全树唯一出口）
├─ storage/       P1/P2/P3 契约读写                                   [待建]
├─ docs/          指引 01–05 + 设计方案 + contracts/
└─ output/pipeline/   全部产物（决策 D8，gitignored）
```

依赖方向**单向、禁止回指**：

```text
common ← llm ← {taskgen, perception, rationale}
              ↑
           executor  ──→ storage
              ↓
          assembler ──→ storage
              ↓
          evaluation ──→ storage
```

两条不可破的约束：`executor` 只依赖 `perception` 的**协议**（`perception/base.py`），不依赖任何实现——这是规则版与 LLM 版能无痛替换的唯一保证；`perception` **不得 import `executor`**，它的输入只有 `Observation` 值对象，不持页面句柄、不回调控制流。

---

## 快速开始

```powershell
uv sync --group dev

# obscura 可执行文件位置（不硬编码本机路径，缺失即报错不猜）
setx OBSCURA_EXE "C:\Users\klpc\workspace\tool\obscura-x86_64-windows-stealth\obscura.exe"

# 探一次执行层能力（唯一需要真实浏览器的自检）
uv run python -m trajectory_pipeline.executor.cli check

$P = trajectory_pipeline/output/pipeline
uv run python -m trajectory_pipeline.executor.cli gen -n 20 --seed 7 --out $P/plan.json   # 采样任务，不联网
uv run python -m trajectory_pipeline.executor.cli run --plan $P/plan.json --limit 5 --dry-run

# 人工复核：导出队列 → 填 verdict → 回填
uv run python -m trajectory_pipeline.executor.cli review --write $P/review_queue.jsonl --show 3
uv run python -m trajectory_pipeline.executor.cli review --verdicts $P/review_queue.jsonl --apply

uv run python -m trajectory_pipeline.executor.cli report --out $P      # 分支分布 + 成功域名分布
uv run python -m trajectory_pipeline.executor.cli check-archives        # 存档完整性门禁，不联网
uv run python -m trajectory_pipeline.executor.cli replay --perceptor rule   # 感知层回放，不联网

uv run python -m pytest -q
```

完整命令表与环境变量见 [运行手册](trajectory_pipeline/docs/04-运行手册.md)。

---

## 数据契约

| 契约 | 落点 | 内容 |
|---|---|---|
| **P1 观察存档** | `output/pipeline/<task_id>__<hash>.json` | 动作 + 工具参数 + **观察原文全文**（真实回放，非 LLM 转述） |
| **P2 单条样本** | `output/pipeline/samples/<id>.json` | 六件套 + provenance + gate 标记 [待建] |
| **P3 训练视图** | `output/pipeline/views/<id>_{messages,openai,meta}.json` | 训练框架可读形态 [待建] |
| 负样本池 | `output/pipeline/negative.jsonl` | 失败分支样本（带 `branch`） |
| 复核队列 | `output/pipeline/review_*.jsonl` | 人工裁定（`verdict` + `reviewed_by`） |

**负样本是强制产出，不是副产品**——每条失败分支都必须有对应样本入库。但 `unresolved` / `trailer_suspect` / `player_unverified` **不是负样本**（「没判出来」≠「看不了」），混进池里会污染它。字段级说明见 [数据契约](trajectory_pipeline/docs/03-数据契约.md)。

> **观察是真实回放，不是 LLM 转述**——这条一旦破，rationale 就没有锚。

---

## 文档

### 项目指引

| 文档 | 内容 |
|---|---|
| [01-架构总览.md](trajectory_pipeline/docs/01-架构总览.md) | 模块地图、控制流与四个判断点、失败分支全集、执行层、感知层、persona 六维 |
| [02-避坑指南.md](trajectory_pipeline/docs/02-避坑指南.md) | **9 类 60 条**，症状写成终端里真正会看到的那句话，可全文搜索 |
| [03-数据契约.md](trajectory_pipeline/docs/03-数据契约.md) | P1/P2/P3 契约表、`steps[]` 三条口径 |
| [04-运行手册.md](trajectory_pipeline/docs/04-运行手册.md) | 命令全表 + 联网与否、环境变量、门禁与输出目录 |
| [05-现状与验收.md](trajectory_pipeline/docs/05-现状与验收.md) | 索引页——内容见 [`计划进展/`](trajectory_pipeline/docs/计划进展/) |
| [计划进展/00-总览.md](trajectory_pipeline/docs/计划进展/00-总览.md) | **跑到哪儿了、接下来做什么**（不含历史），按 9 个功能域分册 |
| [06-端到端流程.md](trajectory_pipeline/docs/06-端到端流程.md) | 一条数据从骨架走到训练样本的完整路径 |
| [07-增量档案.md](trajectory_pipeline/docs/07-增量档案.md) | 增量一~十一的编年史——动守卫之前读它 |

### 设计方案

- [00-总体方案.md](trajectory_pipeline/docs/设计方案/00-总体方案.md) —— 设计基线：决策 D1–D10、模块设计、里程碑、风险与待决
- [01-模块3-可替换感知层.md](trajectory_pipeline/docs/设计方案/01-模块3-可替换感知层.md) —— 插件化机制完整规格 + 七不变式
- [02-rubric与代码对齐.md](trajectory_pipeline/docs/设计方案/02-rubric与代码对齐.md) —— 评分标准逐维度 ↔ 代码的对齐账。**改 rubric 或改控制流之前先读**
- [03-rubric修订与实施方案.md](trajectory_pipeline/docs/设计方案/03-rubric修订与实施方案.md) —— rubric v1.1 改稿清单 + 五个代码变更。**§9 是实施结果**
- [在线视频场景rubric.md](trajectory_pipeline/docs/设计方案/在线视频场景rubric.md) —— 评分标准本体

**开发纪律见 [`CLAUDE.md`](CLAUDE.md)**——新代码一律写进本目录，存量只读不 import。