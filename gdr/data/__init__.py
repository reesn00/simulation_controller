"""data: SFT 训练对程序化构造。

通过 `__init__.py` 重新导出 `build_all_pairs` / `augment_pairs_for_module` / `save_pairs_by_module` / `append_pairs`,
兼容 `from build_sft_pairs import ...` 旧写法。
"""
from data.sft_pairs import (
    append_pairs,
    augment_pairs_for_module,
    build_all_pairs,
    save_pairs_by_module,
)

__all__ = [
    "append_pairs",
    "augment_pairs_for_module",
    "build_all_pairs",
    "save_pairs_by_module",
]