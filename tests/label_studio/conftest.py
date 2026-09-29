"""tests/label_studio: C3 文件工厂 + 常用 fixture。

C3 的 4 视图由 ``save_session_v2`` 从**同一个 stem** 派生, 所以成组产出,
避免测试里手写 4 条路径时写错后缀。样本数据见 :mod:`c3_fixtures`。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from c3_fixtures import QF_TEXT, RICH_MESSAGES, RICH_META


def write_c3(
    directory: Path,
    *,
    task_id: str = "T001",
    session_id: str = "useramulation-20260928-abc",
    meta: dict[str, Any] | None = None,
    messages: dict[str, Any] | None = None,
    qf_text: str | None = QF_TEXT,
    with_openai: bool = True,
) -> Path:
    """在 ``directory`` 下写一组 C3 4 视图, 返回 ``<stem>.meta.json`` 路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    base = f"{task_id}__{session_id}_refined"
    meta_payload = copy.deepcopy(RICH_META if meta is None else meta)
    meta_payload.setdefault("session_id", session_id)
    msg_payload = RICH_MESSAGES if messages is None else messages

    (directory / f"{base}.messages.json").write_text(
        json.dumps(msg_payload, ensure_ascii=False), encoding="utf-8"
    )
    if with_openai:
        (directory / f"{base}.openai.json").write_text(
            json.dumps({"openai_messages": [{"role": "user", "content": "hi"}]}),
            encoding="utf-8",
        )
    if qf_text is not None:
        (directory / f"{base}.qwenjina.txt").write_text(qf_text, encoding="utf-8")
    meta_path = directory / f"{base}.meta.json"
    meta_path.write_text(
        json.dumps(meta_payload, ensure_ascii=False), encoding="utf-8"
    )
    return meta_path


@pytest.fixture
def c3_dir(tmp_path: Path) -> Path:
    return tmp_path / "refine_data"


@pytest.fixture
def rich_meta() -> dict[str, Any]:
    """一份新的 RICH_META 副本 —— 各测试自行改字段, 互不污染。"""
    return copy.deepcopy(RICH_META)


@pytest.fixture
def rich_c3(c3_dir: Path) -> Path:
    return write_c3(c3_dir)


@pytest.fixture
def settings():
    from label_studio.settings import LabelStudioSettings

    return LabelStudioSettings(
        base_url="http://127.0.0.1:8099",
        api_key="placeholder-not-a-real-credential",
    )
