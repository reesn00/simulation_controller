"""etl.parsers — C2 契约入口.

新架构 ``simulation server → gdr → etl`` 下，etl 不再消费原始 trajectory，
而是消费 gdr 产出的 C2 refined Session（详见 ``docs/contracts/C2-refined-session.md``）。

本包是 etl 对 C2 契约的**唯一入口**：所有 etl 内部代码必须通过本包加载
refined Session，不得直接 ``json.load`` C2 文件。

约定：未来若 C2 schema 演进（``schema_version`` 升级），本包内部处理版本转换，
其他 etl 代码不动。
"""
from __future__ import annotations

import json
from pathlib import Path

from gdr.domain.schema import Session


def load_refined_session(path: Path) -> Session:
    """把 gdr 产出的 C2 refined Session JSON 解析为 Session 对象.

    校验 ``schema_version`` 是 ``refined_session.v1``；后续 schema 升级时
    在此处做版本转换（旧版本 → 新版本），调用方零改动。

    Raises:
        ValueError: schema_version 不匹配时.
    """
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    version = raw.get("schema_version")
    if version != "refined_session.v1":
        raise ValueError(
            f"unsupported refined_session schema_version: {version!r}; "
            f"expected 'refined_session.v1'"
        )
    return Session.model_validate(raw)


__all__ = ["load_refined_session"]