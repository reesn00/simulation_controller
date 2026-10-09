"""reassembly: 一致性终检 + 重写 blocks + 元数据落盘。

通过 `__init__.py` 重新导出 `reassemble`, 兼容 `from reassembler import reassemble` 旧写法。
"""
from reassembly.reassembler import reassemble, fold_failed_toolresults, fold_repeated_thinking

__all__ = ["reassemble", "fold_failed_toolresults", "fold_repeated_thinking"]