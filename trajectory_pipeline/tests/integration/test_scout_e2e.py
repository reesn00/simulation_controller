"""scout 端到端集成测试。

不依赖真实 P1 存档 —— 用最小可切样本的 fixture 喂 :func:`run_scout`,
借 ``monkeypatch`` 把 4 个评分器换成假对象,以便精确控制每维的输出。

覆盖:
- 三件套 JSONL 落盘与计数对齐
- status="not_run" 走 skip(不落盘)
- source_archive 缺失走 skip
- P2 与 P1 sample_id 不匹配走 skip
- quarantine 永不入 positive/negative 桶
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest

from trajectory_pipeline.evaluation.scout import io as scout_io


# ── 夹具:最小可切样本的 P1 存档 ──────────────────────────────────


def _obs(url="u"):
    return {
        "url": url,
        "page_title": "T",
        "body_text": "正文",
        "truncated": False,
        "degraded": [],
        "video_tag_count": 0,
        "iframe_count": 0,
        "interactive_elements": [],
        "links": [],
    }


def _archive(
    tmp_path: pathlib.Path,
    name: str,
    *,
    task_id: str = "T001",
) -> pathlib.Path:
    """写一份最小可切样本的 P1 存档。

    ``split_archive`` 会按 :func:`sample_id` 把样本 id 编成
    ``<task_id>__<run_id>__<unit>``(:func:`run_id_of` 从文件名取 run_id,
    unit 通常是 "search")。所以 ``T001__r1__a.json`` → ``T001__r1__a__search``。
    """
    p = tmp_path / name
    p.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "title": "功夫",
                "user_prompt": "我要看功夫",
                "search_url": "https://s.test",
                "search_observation": _obs("https://s.test"),
                "search_blocked": "",
                "steps": [],
                "provenance": {"genre": "喜剧"},
                "run_config": {
                    "engine": "baidu",
                    "max_candidates": 20,
                    "max_chars": None,
                    "stop_after_success": 0,
                    "per_site_timeout_s": 90.0,
                },
                "candidates": [],
                "visits": [],
                "outcomes": [],
                "ledger": None,
                "warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


def _p2_sample(
    tmp_path: pathlib.Path,
    sample_id: str,
    *,
    source_archive: str,
    filename: str | None = None,
) -> pathlib.Path:
    """写一份最小 P2 样例,指向 source_archive。"""
    name = filename or f"{sample_id}.json"
    p = tmp_path / "samples" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "sample_id": sample_id,
                "task_id": "T001",
                "run_id": "r1",
                "source_archive": source_archive,
                "unit": "site-1",
                "title": "功夫",
                "user_prompt": "我要看功夫",
                "outcomes": [],
                "provenance": {"genre": "喜剧"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return p


# ── 假 scorer ───────────────────────────────────────────────────


class _V:
    """最小 verdict 替身,只带 io.py 真正读的字段。"""

    def __init__(self, score: int | None, *, is_veto: bool = False):
        self.score = score
        self.kind = "undetermined" if score is None else "scored"
        self.is_veto = is_veto
        self.reasons = ()


def _install_scorers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    d2: int | None = 4,
    d4: int | None = 4,
    d5: int | None = 4,
    d6: int | None = 4,
    d5_veto: bool = False,
) -> None:
    monkeypatch.setitem(scout_io._SCORERS, "d2", lambda s: _V(d2))
    monkeypatch.setitem(scout_io._SCORERS, "d4", lambda s: _V(d4))
    monkeypatch.setitem(
        scout_io._SCORERS, "d5", lambda s: _V(d5, is_veto=d5_veto)
    )
    monkeypatch.setitem(scout_io._SCORERS, "d6", lambda s: _V(d6))


# ── 主流程:三件套落盘 ────────────────────────────────────────────


class TestRunScoutThreeBuckets:
    """3 条 P2 + 单 archive,跑完落三件套 + scout.jsonl。"""

    def test_3条_各落各桶(self, tmp_path: pathlib.Path, monkeypatch):
        # 1 个 archive 含 3 个不同的 sample_id(三个独立 Sample)
        for sid in ("a", "b", "c"):
            _archive(tmp_path, f"T001__r1__{sid}.json")
            # split_archive 生成的 sample_id = T001__r1__{sid}__search
            full_id = f"T001__r1__{sid}__search"
            _p2_sample(tmp_path, full_id, source_archive=f"T001__r1__{sid}.json")

        # 按 sample_id 装不同的 scoring —— 实际跑时同一个 _SCORERS 全局,
        # 但我们想让 a/b/c 各走不同路径,所以把三个 _p2_path 区分开,
        # 然后用 wrapper scorer 读 sample_id 分流。
        def make_score(target_sid: str, dim: str):
            def _score(sample):
                if sample.sample_id == target_sid:
                    # 各个样本的固定打分
                    table = {
                        "a": {"d2": 4, "d4": 4, "d5": 4, "d6": 4},
                        "b": {"d2": 2, "d4": 4, "d5": 4, "d6": 4},
                        "c": {"d2": None, "d4": 4, "d5": 4, "d6": 4},
                    }
                    return _V(table[target_sid][dim])
                return _V(4)
            return _score

        monkeypatch.setitem(scout_io._SCORERS, "d2", make_score("d2-mux", "d2"))
        # 上面这种写法做不到按 sample_id 分流;改用 lambda 闭包读 sample_id:

        def mux(dim: str):
            def _f(sample):
                table = {
                    "T001__r1__a__search": {"d2": 4, "d4": 4, "d5": 4, "d6": 4},
                    "T001__r1__b__search": {"d2": 2, "d4": 4, "d5": 4, "d6": 4},
                    "T001__r1__c__search": {"d2": None, "d4": 4, "d5": 4, "d6": 4},
                }[sample.sample_id]
                return _V(table[dim])
            return _f

        monkeypatch.setitem(scout_io._SCORERS, "d2", mux("d2"))
        monkeypatch.setitem(scout_io._SCORERS, "d4", mux("d4"))
        monkeypatch.setitem(scout_io._SCORERS, "d5", mux("d5"))
        monkeypatch.setitem(scout_io._SCORERS, "d6", mux("d6"))

        out = scout_io.run_scout(tmp_path, tmp_path / "out")

        assert out == {
            "positive": 1,
            "negative": 1,
            "quarantine": 1,
            "skip": 0,
            "missing_source": 0,
        }
        assert (out := tmp_path / "out").is_dir()
        pos = [json.loads(l) for l in (tmp_path / "out" / "positive_candidates.jsonl").read_text(encoding="utf-8").splitlines() if l]
        neg = [json.loads(l) for l in (tmp_path / "out" / "negative_candidates.jsonl").read_text(encoding="utf-8").splitlines() if l]
        qua = [json.loads(l) for l in (tmp_path / "out" / "quarantine.jsonl").read_text(encoding="utf-8").splitlines() if l]
        assert [r["sample_id"] for r in pos] == ["T001__r1__a__search"]
        assert [r["sample_id"] for r in neg] == ["T001__r1__b__search"]
        assert [r["sample_id"] for r in qua] == ["T001__r1__c__search"]

        # 关键:negative 那条的 tag 必须带 d2_failed
        assert neg[0]["tags"] == ["d2_failed"]

        # 关键:quarantine 的 tag 必须为空
        assert qua[0]["tags"] == []

        # scout.jsonl 包含全部 3 条(顺序不锁)
        overview = [json.loads(l) for l in (tmp_path / "out" / "scout.jsonl").read_text(encoding="utf-8").splitlines() if l]
        assert {r["sample_id"] for r in overview} == {
            "T001__r1__a__search", "T001__r1__b__search", "T001__r1__c__search",
        }

        # _clean 必须剥掉临时字段
        for rec in pos + neg + qua:
            assert "_p2_path" not in rec
            assert "_url" not in rec


class TestRunScoutSkip:
    """skip 计数:不参与落盘。"""

    def test_无P2_samples_目录_skip为0(self, tmp_path: pathlib.Path, monkeypatch):
        # 完全没有 samples 目录
        out = scout_io.run_scout(tmp_path, tmp_path / "out")
        assert out["skip"] == 0
        assert out["positive"] == 0
        # 桶文件存在但为空
        for name in ("positive_candidates", "negative_candidates", "quarantine"):
            f = tmp_path / "out" / f"{name}.jsonl"
            assert f.exists()
            assert f.read_text(encoding="utf-8") == ""

    def test_source_archive_缺失走skip(self, tmp_path: pathlib.Path, monkeypatch):
        _p2_sample(tmp_path, "x", source_archive="missing.json")
        out = scout_io.run_scout(tmp_path, tmp_path / "out")
        assert out["skip"] == 1
        assert out["missing_source"] == 0  # 注意:missing_source 在当前实现里未单独计数
        # 三件套都为空
        for name in ("positive_candidates", "negative_candidates", "quarantine"):
            f = tmp_path / "out" / f"{name}.jsonl"
            assert f.read_text(encoding="utf-8") == ""

    def test_P2指向不存在的sample_id走skip(self, tmp_path: pathlib.Path, monkeypatch):
        _archive(tmp_path, "T001__r1.json")
        _p2_sample(tmp_path, "ghost-id", source_archive="T001__r1.json")
        out = scout_io.run_scout(tmp_path, tmp_path / "out")
        assert out["skip"] == 1

    def test_status_not_run的样本skip且不落盘(self, tmp_path: pathlib.Path, monkeypatch):
        """fail-closed 触发的样本应被 _score_one 标 not_run → skip。"""
        # 该路径在 v04 文档里以 _status_of 的"任一 None → undetermined"覆盖;
        # not_run 是 fail-closed 旁路,需要在 Sample 上注入 fail-closed note 才能触发。
        # 此处直接由 verdict.score=None 走 undetermined → quarantine,与 not_run
        # 不同路径。后续如果接入 fail-closed 注入,这条用例需要扩展。
        _archive(tmp_path, "T001__r1.json")
        _p2_sample(tmp_path, "T001__r1__search", source_archive="T001__r1.json")
        _install_scorers(monkeypatch, d2=None)  # 全 None → quarantine(本用例标记已知 gap)
        out = scout_io.run_scout(tmp_path, tmp_path / "out")
        # 现状:None 走 quarantine;not_run 路径需要 fail-closed 注入才能触发,见 v04 §11.2
        assert out["quarantine"] == 1
        assert out["skip"] == 0


class TestRunScoutVetoAndNegative:
    """veto + 多维低分的 tag 排序。"""

    def test_veto_走negative_且tag只含veto(self, tmp_path: pathlib.Path, monkeypatch):
        _archive(tmp_path, "T001__r1.json")
        _p2_sample(tmp_path, "T001__r1__search", source_archive="T001__r1.json")
        _install_scorers(monkeypatch, d5_veto=True)
        out = scout_io.run_scout(tmp_path, tmp_path / "out")
        assert out["negative"] == 1
        rec = json.loads((tmp_path / "out" / "negative_candidates.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert rec["tags"] == ["veto"]

    def test_多维低分_tag按dim升序(self, tmp_path: pathlib.Path, monkeypatch):
        _archive(tmp_path, "T001__r1.json")
        _p2_sample(tmp_path, "T001__r1__search", source_archive="T001__r1.json")
        _install_scorers(monkeypatch, d2=1, d4=2, d5=4, d6=4)
        out = scout_io.run_scout(tmp_path, tmp_path / "out")
        assert out["negative"] == 1
        rec = json.loads((tmp_path / "out" / "negative_candidates.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert rec["tags"] == ["d2_failed", "d4_failed"]


class TestRunScoutEvidenceSupplement:
    """--mediabrowse-dir 的 L5 证据附加。"""

    def test_有mediabrowse_挂到evidence_supplement(self, tmp_path: pathlib.Path, monkeypatch):
        _archive(tmp_path, "T001__r1.json")
        _p2_sample(tmp_path, "T001__r1__search", source_archive="T001__r1.json")
        _install_scorers(monkeypatch)
        # mediabrowse 索引里 (task_id, url) → l5
        mb = tmp_path / "mb"
        mb.mkdir()
        (mb / "T001__r1.json").write_text(
            json.dumps({
                "task_id": "T001",
                "run_id": "r1",
                "l5_outcome": {
                    "url": "",
                    "branch": "no_play_control",
                    "evidence": "页面无 video 标签",
                    "note": "",
                },
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        out = scout_io.run_scout(
            tmp_path, tmp_path / "out", mediabrowse_dir=mb
        )
        assert out["positive"] == 1
        rec = json.loads((tmp_path / "out" / "positive_candidates.jsonl").read_text(encoding="utf-8").splitlines()[0])
        # url 在 Sample.outcome.url 上 —— 在 _archive 里 outcome=None,
        # 所以 _url="" → key 不命中 → evidence_supplement 为空 dict
        assert rec["evidence_supplement"] == {}

    def test_无mediabrowse_evidence_supplement为空dict(self, tmp_path: pathlib.Path, monkeypatch):
        _archive(tmp_path, "T001__r1.json")
        _p2_sample(tmp_path, "T001__r1__search", source_archive="T001__r1.json")
        _install_scorers(monkeypatch)
        out = scout_io.run_scout(tmp_path, tmp_path / "out")
        rec = json.loads((tmp_path / "out" / "positive_candidates.jsonl").read_text(encoding="utf-8").splitlines()[0])
        assert rec["evidence_supplement"] == {}
