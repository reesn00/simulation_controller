"""``python -m trajectory_pipeline.evaluation.golden_ls`` 的入口。"""

from __future__ import annotations

import sys

from trajectory_pipeline.evaluation.golden_ls.cli import main

if __name__ == "__main__":
    sys.exit(main())
