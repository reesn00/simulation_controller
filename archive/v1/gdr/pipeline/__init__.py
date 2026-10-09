"""pipeline: GDR 数据精修主编排入口。

子模块:
  runner  单 session 处理 + 多文件批量 + 多进程 Pool 编排
  cli     argparse CLI

通过 `__init__.py` 重新导出 `main`, 兼容 `pipeline:main` 入口点。
"""
from pipeline.cli import main
from pipeline.runner import process_one, run

__all__ = ["main", "process_one", "run"]