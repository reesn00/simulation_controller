"""orchestration.workers.etl_worker 单元测试 (ST-3).

通过 monkeypatch ``etl.parsers.load_refined_session`` 和
``gdr.domain.save_session_v2`` 避免依赖真实 C2 文件.

新接口::

    run_etl_once(*, c2_path, etl_outputs_dir, task_id, session_id)
        -> EtlOutputs
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestration.workers.etl_worker import (
    EtlNonRetryableError,
    EtlOutputs,
    run_etl_once,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path):
    etl_outputs_dir = tmp_path / "etl_outputs"
    return tmp_path, etl_outputs_dir


def _write_c2(tmp_path: Path, *, session_id: str = "s1", schema_version: str = "refined_session.v1") -> Path:
    """写一个最小 C2 refined Session JSON 文件供 load_refined_session 读."""
    payload = {
        "session_id": session_id,
        "messages": [],
        "schema_version": schema_version,
        "metadata": {},
    }
    c2 = tmp_path / f"{session_id}.json"
    c2.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return c2


class _FakeSessionOutputs:
    """模拟 ``gdr.domain.SessionOutputs`` —— 4 路径集合."""

    def __init__(self, message: Path, openai: Path, qwenjina: Path | None, meta: Path) -> None:
        self.messages = message
        self.openai = openai
        self.qwenjina = qwenjina
        self.meta = meta


def _mock_etl(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_session_outputs_factory: Any,
    fake_load_session: Any | None = None,
) -> None:
    """mock ``load_refined_session`` + ``save_session_v2``."""

    if fake_load_session is None:
        def fake_load_session(path):
            # 返回一个空对象, 模拟 Session (含 messages / metadata).
            return _FakeSession(path)
        monkeypatch.setattr(
            "orchestration.workers.etl_worker.load_refined_session",
            fake_load_session,
        )
    else:
        monkeypatch.setattr(
            "orchestration.workers.etl_worker.load_refined_session",
            fake_load_session,
        )

    monkeypatch.setattr(
        "orchestration.workers.etl_worker.save_session_v2",
        fake_session_outputs_factory,
    )


class _FakeSession:
    """最小 Session 替身 (满足 ``model_dump`` 接口足够)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.session_id = path.stem
        self.messages: list = []
        self.metadata: dict = {}

    def model_dump(self, mode: str = "python", **kwargs):
        return {
            "session_id": self.session_id,
            "messages": [],
            "metadata": self.metadata,
        }


def _make_fake_save_v2(tmp_path: Path, *, with_qwenjina: bool = True):
    """生成 fake ``save_session_v2`` —— 在 etl_outputs_dir 下写 4 个文件."""
    def fake_save_v2(session, base_path):
        base_path = Path(base_path)
        messages_path = Path(str(base_path) + ".messages.json")
        openai_path = Path(str(base_path) + ".openai.json")
        meta_path = Path(str(base_path) + ".meta.json")
        qwenjina_path: Path | None = None
        if with_qwenjina:
            qwenjina_path = Path(str(base_path) + ".qwenjina.txt")

        for p in (messages_path, openai_path, meta_path):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}", encoding="utf-8")
        if qwenjina_path is not None:
            qwenjina_path.parent.mkdir(parents=True, exist_ok=True)
            qwenjina_path.write_text("text", encoding="utf-8")
        return _FakeSessionOutputs(
            messages_path, openai_path, qwenjina_path, meta_path,
        )

    return fake_save_v2


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


def test_run_etl_once_success(env, monkeypatch) -> None:
    """run_etl_once 成功: 4 视图写出, 返 EtlOutputs."""
    tmp_path, etl_outputs_dir = env
    c2 = _write_c2(tmp_path, session_id="s1")

    _mock_etl(monkeypatch, fake_session_outputs_factory=_make_fake_save_v2(tmp_path))

    result = run_etl_once(
        c2_path=c2, etl_outputs_dir=etl_outputs_dir,
        task_id="T001", session_id="s1",
    )

    assert isinstance(result, EtlOutputs)
    assert result.task_id == "T001"
    assert result.session_id == "s1"
    assert result.duration_seconds >= 0
    # 4 视图文件名遵循 <safe_task_id>__<safe_session_id>.<suffix>
    assert result.messages_path.name == "T001__s1.messages.json"
    assert result.openai_path.name == "T001__s1.openai.json"
    assert result.qwenjina_path is not None
    assert result.qwenjina_path.name == "T001__s1.qwenjina.txt"
    assert result.meta_path.name == "T001__s1.meta.json"
    # 4 文件都已写出
    assert result.messages_path.exists()
    assert result.openai_path.exists()
    assert result.qwenjina_path.exists()
    assert result.meta_path.exists()


def test_run_etl_once_qwenjina_none(env, monkeypatch) -> None:
    """qf_text 缺失时 qwenjina_path 应为 None; 其他 3 视图仍返非 None."""
    tmp_path, etl_outputs_dir = env
    c2 = _write_c2(tmp_path, session_id="s1")

    _mock_etl(
        monkeypatch,
        fake_session_outputs_factory=_make_fake_save_v2(tmp_path, with_qwenjina=False),
    )

    result = run_etl_once(
        c2_path=c2, etl_outputs_dir=etl_outputs_dir,
        task_id="T001", session_id="s1",
    )

    assert result.qwenjina_path is None
    assert result.messages_path.exists()
    assert result.openai_path.exists()
    assert result.meta_path.exists()


def test_run_etl_once_creates_outputs_dir(env, monkeypatch) -> None:
    """etl_outputs_dir 不存在时, run_etl_once 应自动创建."""
    tmp_path, etl_outputs_dir = env
    c2 = _write_c2(tmp_path, session_id="s1")
    assert not etl_outputs_dir.exists()

    _mock_etl(monkeypatch, fake_session_outputs_factory=_make_fake_save_v2(tmp_path))

    run_etl_once(
        c2_path=c2, etl_outputs_dir=etl_outputs_dir,
        task_id="T001", session_id="s1",
    )
    assert etl_outputs_dir.exists()


def test_run_etl_once_filename_sanitizes_unsafe_chars(env, monkeypatch) -> None:
    """task_id / session_id 含非法字符时, 文件名 sanitize 为 ``_``."""
    tmp_path, etl_outputs_dir = env
    # 不走 _write_c2 (该辅助函数自己就会因 session_id 含 '/' 报错);
    # 直接造一个物理 C2 + 传一个虚拟 session_id 给 run_etl_once,
    # 焦点是 task_id / session_id 的 sanitize 行为, 不是 C2 文件内容.
    c2 = _write_c2(tmp_path, session_id="s_safe")

    _mock_etl(monkeypatch, fake_session_outputs_factory=_make_fake_save_v2(tmp_path))

    result = run_etl_once(
        c2_path=c2, etl_outputs_dir=etl_outputs_dir,
        task_id="T/001", session_id="s/1",
    )
    # 文件名应 sanitize: '/' → '_'
    assert "/" not in result.messages_path.name
    assert result.messages_path.name == "T_001__s_1.messages.json"


# ---------------------------------------------------------------------------
# 异常路径 - 永久性 (EtlNonRetryableError)
# ---------------------------------------------------------------------------


def test_run_etl_once_missing_c2_raises_non_retryable(env, monkeypatch) -> None:
    """C2 路径不存在 → EtlNonRetryableError (永久)."""
    tmp_path, etl_outputs_dir = env
    c2 = tmp_path / "missing.json"  # 不创建

    _mock_etl(monkeypatch, fake_session_outputs_factory=_make_fake_save_v2(tmp_path))

    with pytest.raises(EtlNonRetryableError, match="missing"):
        run_etl_once(
            c2_path=c2, etl_outputs_dir=etl_outputs_dir,
            task_id="T001", session_id="s1",
        )


def test_run_etl_once_load_failure_raises_non_retryable(env, monkeypatch) -> None:
    """load_refined_session 抛 ValueError (schema 不匹配) → EtlNonRetryableError."""
    tmp_path, etl_outputs_dir = env
    c2 = _write_c2(tmp_path, session_id="s1")

    def boom_load(_path):
        raise ValueError("unsupported schema_version: 'foo'")
    _mock_etl(
        monkeypatch,
        fake_session_outputs_factory=_make_fake_save_v2(tmp_path),
        fake_load_session=boom_load,
    )

    with pytest.raises(EtlNonRetryableError, match="schema_version"):
        run_etl_once(
            c2_path=c2, etl_outputs_dir=etl_outputs_dir,
            task_id="T001", session_id="s1",
        )


def test_run_etl_once_load_json_broken_raises_non_retryable(env, monkeypatch) -> None:
    """C2 文件是 broken JSON → EtlNonRetryableError."""
    tmp_path, etl_outputs_dir = env
    c2 = tmp_path / "broken.json"
    c2.write_text("{not json", encoding="utf-8")

    def boom_load(_path):
        # json.JSONDecodeError 继承自 ValueError
        raise ValueError("invalid json")
    _mock_etl(
        monkeypatch,
        fake_session_outputs_factory=_make_fake_save_v2(tmp_path),
        fake_load_session=boom_load,
    )

    with pytest.raises(EtlNonRetryableError, match="load_refined_session"):
        run_etl_once(
            c2_path=c2, etl_outputs_dir=etl_outputs_dir,
            task_id="T001", session_id="s1",
        )


# ---------------------------------------------------------------------------
# 异常路径 - save_session_v2 失败 (写盘)
# ---------------------------------------------------------------------------


def test_run_etl_once_save_failure_propagates(env, monkeypatch) -> None:
    """save_session_v2 抛异常 → 透传 (PipelineExecutor 兜底走 attempts_etl)."""
    tmp_path, etl_outputs_dir = env
    c2 = _write_c2(tmp_path, session_id="s1")

    def boom_save(_session, _base_path):
        raise RuntimeError("disk full")
    _mock_etl(monkeypatch, fake_session_outputs_factory=boom_save)

    with pytest.raises(RuntimeError, match="disk full"):
        run_etl_once(
            c2_path=c2, etl_outputs_dir=etl_outputs_dir,
            task_id="T001", session_id="s1",
        )


# ---------------------------------------------------------------------------
# 边界场景
# ---------------------------------------------------------------------------


def test_run_etl_once_is_pure_module_function(env, monkeypatch) -> None:
    """run_etl_once 不维护状态; 多次调用互不影响."""
    tmp_path, etl_outputs_dir = env
    c2_a = _write_c2(tmp_path, session_id="s_a")
    c2_b = _write_c2(tmp_path, session_id="s_b")

    _mock_etl(monkeypatch, fake_session_outputs_factory=_make_fake_save_v2(tmp_path))

    r_a = run_etl_once(
        c2_path=c2_a, etl_outputs_dir=etl_outputs_dir,
        task_id="T_A", session_id="s_a",
    )
    r_b = run_etl_once(
        c2_path=c2_b, etl_outputs_dir=etl_outputs_dir,
        task_id="T_B", session_id="s_b",
    )

    assert r_a.task_id == "T_A"
    assert r_b.task_id == "T_B"
    assert r_a.messages_path != r_b.messages_path
    assert r_a.messages_path.exists()
    assert r_b.messages_path.exists()