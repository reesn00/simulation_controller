# orchestration · 顶层调度器

把 `simulate_serve` / `etl/qwenformat` / `gdr` 三个独立子系统串成
`trajectory → qwenformat → gdr` 的三阶段流水线。

设计、决策、契约见 [`docs/orchestration-design.md`](../docs/orchestration-design.md)。

## 当前状态

**step 1 / 13**：仅骨架。CLI `start / status / stop / replay` 都返回 TODO 信息，
不启动任何进程。后续步骤按 design doc §12 顺序逐个 commit。

## 快速使用（骨架阶段）

```bash
uv run orchestration start     # 打印 TODO 后退出
uv run orchestration status    # 打印 TODO 后退出
uv run orchestration stop      # 打印 TODO 后退出
uv run orchestration replay    # 打印 TODO 后退出
```

## 目录约定

```
orchestration/
├── __init__.py
├── __main__.py        CLI 入口
├── config.yaml        默认配置（被打进 wheel）
├── data/              运行时数据：SQLite 队列 / qf_out / pid / dead
├── logs/              运行时日志
└── README.md
```
