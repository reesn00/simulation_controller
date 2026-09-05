"""gdr.pipeline.runner._process_one_file 加载契约回归.

qf 阶段产物(qf_out)是 Session JSON(整体多行 indent), 必须用 load_session 加载;
曾错用 load_trajectory(JSONL 逐行) 导致
"Expecting property name enclosed in double quotes: line 1 column 2 (char 1)".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from config import Settings


def _write_qf_out(tmp_path: Path) -> Path:
    """造 qf_worker 产物形态: 多行 indent Session JSON(含 metadata)."""
    fp = tmp_path / "qf_out" / "s1.json"
    fp.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": "s1",
        "summary": "",
        "messages": [
            {
                "role": "user", "name": "user", "id": "u1",
                "blocks": [{"type": "text", "id": "b1", "text": "hi"}],
                "metadata": {},
            }
        ],
        "metadata": {
            "openai_messages": [{"role": "user", "content": "hi"}],
            "tools": [],
            "qf_text": "user\nhi",
        },
    }
    fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


def test_process_one_file_loads_session_json(tmp_path, monkeypatch):
    """qf_out 是多行 indent Session JSON; _process_one_file 应成功加载并保存."""
    from pipeline import runner

    qf_in = _write_qf_out(tmp_path)
    out_path = tmp_path / "gdr_out" / "s1_refined.json"

    monkeypatch.setattr(runner, "load_tools", lambda *a, **k: ([], []))
    monkeypatch.setattr(runner, "process_one", lambda session, cfg, tn, ha: session)

    cfg = Settings(
        batch_output_dir=tmp_path / "gdr_out",
        workers=1,
        max_files=1,
        enable_llm_layer=False,
    )
    result = runner._process_one_file(qf_in, out_path, cfg)

    assert result["status"] == "success", result
    assert out_path.exists()


def test_load_trajectory_rejects_session_json(tmp_path):
    """反向佐证: load_trajectory(JSONL 逐行) 对 Session JSON 报 JSONDecodeError.

    固化"qf_out 不可用 load_trajectory"的事实, 防止回退.
    """
    from domain import load_trajectory

    qf_in = _write_qf_out(tmp_path)
    with pytest.raises(json.JSONDecodeError):
        load_trajectory(qf_in)
