"""scout 的 LLM 接线：**客户端由调用方注入，库层不读环境变量**。

钉两件事：

1. ``llm_client`` 只传给接受它的维度（今天只有 D-2）——d4/d5/d6 的签名
   没有它，无脑传会 TypeError，而那种错只在真跑批次时才炸；
2. ``scout.cli`` 按 ``TRAJECTORY_LLM_*`` 建一次客户端，**dry-run 与真实
   run 共用**——分开建会让预演与真跑的三态统计对不上。
"""

from __future__ import annotations

from types import SimpleNamespace

from trajectory_pipeline.evaluation.scout import cli as scout_cli
from trajectory_pipeline.evaluation.scout import io as scout_io


def _verdict(score: int) -> SimpleNamespace:
    return SimpleNamespace(score=score, is_veto=False, reasons=())


def _fake_sample() -> SimpleNamespace:
    """没有站点视图 ⇒ ``_units_of`` 回退整条样本。"""
    return SimpleNamespace(
        sample_id="T001__r1",
        task_id="T001",
        site_views=lambda: (),
        outcome=SimpleNamespace(
            url="a.com", branch=None, question="q", p1_missing_fields=()),
    )


def _p2_row(tmp_path) -> dict:
    return {"task_id": "T001",
            "_p2_path": str(tmp_path / "T001__r1.json")}


class Test评分器传参:
    def test_只有LLM维度拿到客户端(self, monkeypatch):
        seen = {}

        def fake_d2(sample, *, llm_client=None):
            seen["d2"] = llm_client
            return _verdict(5)

        def fake_d4(sample):                 # 刻意不收 kwargs
            seen["d4"] = True
            return _verdict(5)

        monkeypatch.setitem(scout_io._SCORERS, "d2", fake_d2)
        monkeypatch.setitem(scout_io._SCORERS, "d4", fake_d4)
        monkeypatch.setitem(scout_io._SCORERS, "d5", lambda s: _verdict(5))
        monkeypatch.setitem(scout_io._SCORERS, "d6", lambda s: _verdict(5))

        client = object()
        rows = scout_io._score_one(object(), llm_client=client)
        assert seen["d2"] is client
        assert seen["d4"] is True
        assert [r["dimension"] for r in rows] == ["d2", "d4", "d5", "d6"]

    def test_没有客户端时按单参调用(self, monkeypatch):
        """``--no-llm`` / env 没配的路径必须走原签名——测试桩与
        默认参数都吃这一形态。"""
        monkeypatch.setitem(scout_io._SCORERS, "d2", lambda s: _verdict(5))
        for dim in ("d4", "d5", "d6"):
            monkeypatch.setitem(scout_io._SCORERS, dim, lambda s: _verdict(5))
        rows = scout_io._score_one(object())
        assert [r["score"] for r in rows] == [5, 5, 5, 5]


class Test整批透传:
    def test_run_scout把客户端传到评分器(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scout_io, "list_p2_samples",
                            lambda root: [_p2_row(tmp_path)])
        monkeypatch.setattr(scout_io, "_resolve_sample",
                            lambda p2, root: _fake_sample())
        seen = {}

        def fake_score_one(unit, *, llm_client=None, **kwargs):
            seen["client"] = llm_client
            return [{"dimension": d, "score": 5, "is_veto": False,
                     "reasons": []} for d in ("d2", "d4", "d5", "d6")]

        monkeypatch.setattr(scout_io, "_score_one", fake_score_one)
        client = object()
        scout_io.run_scout(tmp_path, tmp_path / "out", llm_client=client)
        assert seen["client"] is client


class TestCLI建客户端:
    def test_dry_run用env客户端(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scout_io, "list_p2_samples",
                            lambda root: [_p2_row(tmp_path)])
        monkeypatch.setattr(scout_io, "_resolve_sample",
                            lambda p2, root: _fake_sample())
        seen = {}

        def fake_score_one(unit, *, llm_client=None, **kwargs):
            seen["client"] = llm_client
            return [{"dimension": d, "score": 5, "is_veto": False,
                     "reasons": []} for d in ("d2", "d4", "d5", "d6")]

        monkeypatch.setattr(scout_io, "_score_one", fake_score_one)
        monkeypatch.setenv("TRAJECTORY_LLM_BASE_URL", "http://127.0.0.1:9/v1")
        monkeypatch.setenv("TRAJECTORY_LLM_MODEL", "test-model")

        rc = scout_cli.main([
            "run", "--p2-root", str(tmp_path),
            "--out", str(tmp_path / "out"), "--dry-run",
        ])
        assert rc == 0
        assert seen["client"] is not None

    def test_没配env时警告后继续(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(scout_io, "list_p2_samples", lambda root: [])
        rc = scout_cli.main([
            "run", "--p2-root", str(tmp_path),
            "--out", str(tmp_path / "out"), "--dry-run",
        ])
        assert rc == 0
        assert "未配置 TRAJECTORY_LLM_*" in capsys.readouterr().err

    def test_半配env退2(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("TRAJECTORY_LLM_BASE_URL", "http://127.0.0.1:9/v1")
        rc = scout_cli.main([
            "run", "--p2-root", str(tmp_path),
            "--out", str(tmp_path / "out"), "--dry-run",
        ])
        assert rc == 2
        assert "LLM 配置错误" in capsys.readouterr().err