# Phase 1 Catalog 迁移报告

| 项目 | 结果 |
|---|---|
| Task 数 | 58 |
| Scenario 数 | 2 |
| Catalog 版本 | v1 envelope |
| 编译错误 | 0 |
| 编译警告 | 0 |
| 未知字段静默忽略 | 已禁止 |
| T034/T046 冲突继承 | 已使用 replace 修复 |
| 模糊 `min_length` | 内置任务已全部迁移 |

配置路径首先相对 `config.yaml` 所在目录解析；只为外部 legacy 配置保留 package-relative 回退并打印弃用警告。
