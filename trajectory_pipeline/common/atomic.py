"""原子写：**先写同目录的临时文件，再 ``os.replace`` 换上去**。

为什么要有这一份
----------------
「就地改」在写到一半崩掉时留下半截文件，而下一次读它的人**分不出**
「崩了」与「格式坏了」——两种都要从头查起。临时文件 + 原子替换让读者
永远只看到「改之前的完整内容」或「改之后的完整内容」。

⚠️ **临时文件必须与目标同目录**：``os.replace`` 在同一文件系统内才是
原子的，跨目录（``/tmp`` → 工作区）会退化成「复制 + 覆盖」，中途崩了
照样留下半截文件。

⚠️ **失败时清掉临时文件**：否则每次失败都在目录里留一个 ``.tmp`` 垃圾，
而下一个读目录的人会把它当成一份存档。

原先有三份实现（:mod:`executor.downgrade`、:meth:`executor.archive.P1Archive._write_text`
与各处 ``tmp.write_text`` + ``os.replace`` 的散写）。三份会漂——
一份清临时文件、另一份不清，于是「崩了之后目录里有没有垃圾」这件事
在不同命令上表现不同。**合成一份**。
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

__all__ = ["write_text_atomic", "write_json_atomic"]


def write_text_atomic(path: pathlib.Path | str, payload: str) -> pathlib.Path:
    """把 ``payload`` 原子写到 ``path``。返回该路径。

    失败时**不留临时文件**，也不动原文件。
    """
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, p)          # 同目录内原子替换
    finally:
        if tmp.exists():            # replace 失败时别留半个文件
            tmp.unlink(missing_ok=True)
    return p


def write_json_atomic(path: pathlib.Path | str, payload: Any, *,
                      indent: int = 1) -> pathlib.Path:
    """把 ``payload`` 序列化成 JSON 后原子写。``ensure_ascii=False``。

    默认 ``indent=1``：存档与池文件都是给人看的，一行一项比一行一整坨好读；
    而体积敏感的地方（池文件动辄几百行）用 1 而不是 2。
    """
    text = json.dumps(payload, ensure_ascii=False, indent=indent)
    return write_text_atomic(path, text)
