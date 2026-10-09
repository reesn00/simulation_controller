# v1 存量归档（冻结只读）

2026-10-09 归档。v2 轨迹合成管线（`trajectory_pipeline/`）是唯一开发目标；
本目录是 v1 全量代码的封存快照，**只读参考**——可读代码抄思路，
禁止 import、禁止修改（纪律见根 `CLAUDE.md` 开发纪律一节）。

## 内容

| 目录/文件 | 职责 |
|---|---|
| `simulate_serve/` | v1 模拟采集端（入口 `python -m simulate_serve`） |
| `orchestration/` | 顶层调度 + SQLite 队列状态机（入口 `python -m orchestration`） |
| `gdr/` | C1→C2 精修。⚠️ 历史上存在双导入姿势（顶层风格 + `gdr.` 前缀），跨边界不要用 `isinstance` 认类型 |
| `etl/` | C2→C3 格式转换 |
| `label_studio/` | 流水线终点，单向推送不回流 |
| `tool_runtime/` | Node 侧 Playwright MCP 依赖，默认禁用 |
| `config/` | v1 配置。`config.yaml` / `config_bak.yaml` **含真实凭据**（已 gitignore；`config_bak.yaml` 已解除跟踪） |
| `conftest.py` | 本区测试树的公共前置（gdr sys.path 特技 + Windows 不弹窗），路径 `__file__` 相对，迁移不改行为 |
| `shared_config.py` | v1 三段共用的根配置读取工具 |
| `docs/` `scripts/` `tests/` | v1 文档 / 迁移与训练脚本 / 测试两棵树（`tests/`、`gdr/tests/`） |

## 数据文件的去向

`simulate_serve/config/tasks.yaml`（98 个任务骨架）是 v2 模块 1 的输入资产，
已**字节级复制**为 `trajectory_pipeline/taskgen/data/tasks.yaml` 随新树携带；
本目录保留原件。v1 已冻结不再变更，两侧永不漂移。

## 手动运行（默认不装依赖）

v1 依赖已移出主环境（`camel-ai` / `jinja2` / `cloverlabs-camoufox` 进了 `legacy` extra）：

    uv sync --extra legacy
    uv run pytest archive/v1/tests archive/v1/gdr/tests

v1 测试已退役出默认门禁（`testpaths` 只含 `trajectory_pipeline/tests`）——
门禁保护「会被修改的代码」，冻结树无从回归。

## 已知遗留

- `config/config_bak.yaml` **曾被提交入库**（凭据红线违规，2026-10-09 归档时
  解除跟踪并加 ignore）。git 历史中仍存在；是否清洗历史由仓库所有者决定。
