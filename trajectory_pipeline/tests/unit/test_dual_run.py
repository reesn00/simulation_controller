"""双跑对账（P5）：判据要**一眼能判，且判不了的要说判不了**。

三条容易写错的：

1. 分母。分支占比的分母是「出过分支的站点数」，不是 run 数——用 run 数
   会让「一个 run 访问了 3 个站」的分支权重被压成 1/1。
2. 「判不了」不是「过了」。基线中位数为 0 时比值无从计算，写 0 就是把
   没法判写成了一模一样。
3. 两侧任务集合不同必须显形。悄悄按并集比，会让少跑的那一侧分母更小、
   比例差看着更小。
"""

from __future__ import annotations

import json
from pathlib import Path

from trajectory_pipeline.executor import dual_run


def _obs(*, body: int, elements: int) -> dict:
    return {
        "url": "https://example.com/x",
        "body_text": "x" * body,
        "body_len": body,
        "elements_total": elements,
        "interactive_elements": [{"tag": "a", "label": "x"}] * min(elements, 2),
    }


def _record(task_id: str, *, driver: str, branches: dict[str, int],
            body: int, elements: int, negatives: int = 0,
            search_blocked: bool = False) -> dict:
    return {
        "task_id": task_id,
        "driver": driver,
        "search_blocked": search_blocked,
        "search_observation": _obs(body=body, elements=elements),
        "steps": [{"action": {}, "observation": _obs(body=body, elements=elements),
                   "error": "", "rationale": None}],
        "visits": [],
        "ledger": {"by_branch": dict(branches)},
        "outcomes": [
            {"url": f"https://example.com/{task_id}/{i}", "branch": "no_play_control",
             "evidence": "证据原文", "is_negative_sample": True}
            for i in range(negatives)
        ],
    }


def _write(root: Path, records: list[dict]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for n, record in enumerate(records):
        name = f"{record['task_id']}__run{n}.json"
        (root / name).write_text(json.dumps(record, ensure_ascii=False),
                                 encoding="utf-8")
    return root


class Test汇总:
    def test_分支分母是站点不是run(self, tmp_path):
        root = _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={"a": 1, "b": 3},
                    body=100, elements=10),
        ])
        side = dual_run.summarize("A", root)
        assert side.outcomes_total == 4
        assert side.branch_shares["b"] == 75.0

    def test_观察数与中位数来自全部观察(self, tmp_path):
        # 每份存档 2 个观察（search + 1 步），body 都一样
        root = _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=100, elements=10),
            _record("T2", driver="obscura", branches={}, body=300, elements=30),
        ])
        side = dual_run.summarize("A", root)
        assert side.observations == 4
        assert side.body_len_median == 200.0
        assert side.elements_median == 20.0

    def test_驱动面按打开不算抛(self, tmp_path):
        root = _write(tmp_path / "a", [
            _record("T1", driver="", branches={}, body=1, elements=1),
            _record("T2", driver="来自未来的驱动", branches={}, body=1, elements=1),
        ])
        side = dual_run.summarize("A", root)
        assert side.surfaces == {"obscura-1": 1, "unknown": 1}

    def test_探针文件不是存档(self, tmp_path):
        root = _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=1, elements=1),
        ])
        (root / "probe_observe.json").write_text("{}", encoding="utf-8")
        assert dual_run.summarize("A", root).runs == 1


class Test判据:
    def test_分支差超5pp判fail并点名(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={"x": 10}, body=100, elements=10),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox",
                    branches={"x": 5, "y": 5}, body=100, elements=10),
        ]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        branch = crit["分支分布"]
        assert branch["result"] == "fail"
        assert set(branch["detail"]["offenders"]) == {"x", "y"}
        assert report["verdict"] == "fail"

    def test_中位数差30个百分点为界(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=100, elements=100),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=130, elements=100),
        ]))
        crit = {c["criterion"]: c for c in dual_run.compare(a, b)["criteria"]}
        assert crit["body_text 长度中位数"]["detail"]["ratio"] == 0.3
        assert crit["body_text 长度中位数"]["result"] == "pass"

    def test_基线为0时判不了而不是判过(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=0, elements=0),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=50, elements=50),
        ]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        assert crit["body_text 长度中位数"]["result"] == "incomplete"
        assert report["verdict"] == "incomplete"

    def test_两侧任务集合不同要显形(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=10, elements=1),
            _record("T2", driver="obscura", branches={}, body=10, elements=1),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=10, elements=1),
        ]))
        report = dual_run.compare(a, b)
        assert report["paired_task_ids"] == ["T1"]
        assert report["only_in_a"] == ["T2"]
        assert "只在 A：T2" in dual_run.format_report(report)

    def test_负样本原文进报告供人读(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=1, elements=1,
                    negatives=2),
        ]))
        side = a.to_json()
        assert len(side["negatives"]) == 2
        assert side["negatives"][0]["evidence"] == "证据原文"
        report = dual_run.compare(a, a)
        assert "负样本（逐条读" in dual_run.format_report(report)