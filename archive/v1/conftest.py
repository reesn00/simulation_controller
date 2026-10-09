"""归档区 conftest —— v1 两棵测试树 (``tests/`` 与 ``gdr/tests/``, 均在本目录下) 的公共前置.

2026-10-09 随 v1 代码自仓库根迁入 archive/v1/：路径全部 ``__file__`` 相对, 迁移不改行为.
v1 测试已退役出默认门禁, 手动跑法见本目录 README.md.

存在的理由是 2026-09-30 把 ``gdr/tests`` 并进默认 ``testpaths`` 之后, 有两件
事必须在**任何**子目录 conftest 之前就位, 否则谁先被收集谁坏:

1. ``gdr/`` 要在 ``sys.path`` 上
   gdr 的包内到处是 ``from config import Settings`` / ``from domain import ...``
   这类**顶层**导入 (它的 ``pyproject.toml`` 用
   ``tool.setuptools.packages.find include = ["config*", "domain*", ...]``
   把子目录当顶层包装)。而仓库根又有一个 ``config/`` 目录 (放 config.yaml
   的真实配置目录, 没有 ``__init__.py`` → 命名空间包)。**根目录一旦先进
   sys.path, ``import config`` 就会解析到根配置目录, gdr 的 Settings 直接
   load_error。** 所以必须让 ``gdr/`` 排在 ``sys.path`` 更前面。

   这段以前只写在 ``tests/conftest.py`` 里, 于是 ``pytest gdr/tests/`` 单独跑
   时行为与 ``pytest`` 跑全量时不一致 (前者靠 editable 安装的 finder 兜住,
   后者靠 tests/conftest.py 插进来的路径) —— 两种姿势下 gdr 加载的是**不同
   的类对象**, 正是 ``gdr/refiners/usage_prune.py`` 里那两个 isinstance 静默
   失效的根源。统一到根 conftest 后只剩一种姿势。

2. Windows 不弹控制台窗口
   见下方 ``install_*_no_window_policy``。两棵树各自装一份也可以 (都幂等),
   但同样会漂 —— 根 conftest 装一次就够, ``gdr/tests/conftest.py`` 里那份
   保留是为了 ``cd gdr && pytest`` 的独立跑法。
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent

# 必须在任何 import 之前; 且用 insert(0) 而非 append —— append 会被根目录的
# ``config/`` 命名空间包抢先 (根目录由 ``python -m pytest`` 自动入 path)。
_GDR_DIR = _PROJECT_ROOT / "gdr"
if str(_GDR_DIR) not in sys.path:
    sys.path.insert(0, str(_GDR_DIR))

# pytest session 启动时立即安装 Windows no-window 策略 (两层, 都幂等):
#   1. install_no_window_policy            → multiprocessing.Pool spawn worker
#      (拦 _winapi.CreateProcess, 认 --multiprocessing-fork 指纹)
#   2. install_subprocess_no_window_policy → 任意 subprocess.Popen
#      (测试里手写的 `python -c` 不带上面那个指纹, 第一层拦不住 —— 实测
#       test_push_index.py 一次弹 4 个黑窗)
# 两者都只补 creationflags=0 的调用, 已显式带 flag 的 (daemon.start_detached)
# 行为不变。
#
# 注:生产代码 orchestration.daemon / orchestration.pipeline_executor module
# 加载时已自带第 1 层;此处显式再调是为 pytest 在未 import orchestration.* 就
# 直接起 multiprocessing.Pool 的场景也覆盖。第 2 层生产不装。
try:
    from orchestration._windows import (
        install_no_window_policy,
        install_subprocess_no_window_policy,
    )

    install_no_window_policy()
    install_subprocess_no_window_policy()
except Exception:  # pragma: no cover - 兜底,绝不阻塞测试启动
    pass
