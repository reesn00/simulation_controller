"""``executor.label_sync`` 的测试。

盯的是**判据的边界**——这个工具改的是「结论的两个字段没同步」，
不是「结论错了」，也不是「口径演进留下的旧文本」：

* 该修的：分支被工具改过、标签没跟上（自相矛盾）；
* 不该修的：分支没被工具动过、只是标签**文本**随口径演进变旧
  （两代口径，回填会把新旧两代混在一起）；
* 绝不能碰的：``branch`` / ``evidence`` / ``ledger``——那是结论层，
  本工具只修标签。
"""

from __future__ import annotations

import json

from trajectory_pipeline.executor import label_sync as ls
from trajectory_pipeline.executor.branches import BRANCH_LABELS


def _arch(outcomes):
    return {"task_id": "T001", "title": "功夫",
            "ledger": {"by_branch": {}, "negative_samples": 0,
                       "missing_branches": []},
            "outcomes": outcomes}


def _o(url, branch, label, **over):
    rec = {"url": url, "branch": branch, "branch_label": label,
           "evidence": "判定正文", "question": "player.ok", "source": "llm"}
    rec.update(over)
    return rec


def _write(tmp_path, arch, name="T001__r1.json"):
    p = tmp_path / name
    p.write_text(json.dumps(arch, ensure_ascii=False), encoding="utf-8")
    return p


def _read(p):
    return json.loads(p.read_text(encoding="utf-8"))


class Test判据:
    def test_工具改过分支而标签没跟上_该修(self):
        """降级把分支改成 ``unresolved`` 却留着旧标签 ⇒ 自相矛盾。"""
        arch = _arch([_o("https://a.com", "unresolved", "导航失败（网络/DNS/超时）",
                         branch_before_downgrade="unreachable_hard")])
        fixes = ls.label_fixes(arch)
        assert len(fixes) == 1
        assert fixes[0]["label_before"] == "导航失败（网络/DNS/超时）"
        assert fixes[0]["label_after"] == BRANCH_LABELS["unresolved"]

    def test_改判留了戳而标签没跟上_该修(self):
        """``relabel`` 改判只改 ``branch``（2026-11-04 前的缺陷）。"""
        arch = _arch([_o("https://a.com", "login_required",
                         BRANCH_LABELS["subscription_wall"],
                         branch_before_relabel="subscription_wall")])
        fixes = ls.label_fixes(arch)
        assert len(fixes) == 1
        assert fixes[0]["label_after"] == BRANCH_LABELS["login_required"]

    def test_降级一条成功_戳的值是空_照样该修(self):
        """降级一条**成功**（``branch=None``）时 ``branch_before_downgrade``
        记的是 ``None``——**键在、值为空**，那**也是**「分支被工具改过」。
        只认「值非空」就会漏掉这批。"""
        arch = _arch([_o("https://a.com", "unresolved", "成功",
                         branch_before_downgrade=None)])
        fixes = ls.label_fixes(arch)
        assert len(fixes) == 1
        assert fixes[0]["label_after"] == BRANCH_LABELS["unresolved"]

    def test_口径演进留下的旧文本_不修(self):
        """分支**没被工具改过**、只是标签文本随口径收窄变旧
        （``unreachable_hard`` 的旧文本）⇒ **不回填**：新旧两代口径
        不可直接比。"""
        arch = _arch([_o("https://a.com", "unreachable_hard",
                         "导航失败（网络/DNS/超时）")])
        assert ls.label_fixes(arch) == []

    def test_标签已经对上了_不动(self):
        arch = _arch([_o("https://a.com", "subscription_wall",
                         BRANCH_LABELS["subscription_wall"],
                         branch_before_relabel="broken")])
        assert ls.label_fixes(arch) == []


class Test处置:
    def test_只改标签_结论一个字不动(self):
        arch = _arch([_o("https://a.com", "unresolved", "导航失败（网络/DNS/超时）",
                         evidence="判定正文", branch_before_downgrade="unreachable_hard")])
        before = json.dumps(arch["outcomes"][0], ensure_ascii=False, sort_keys=True)
        n = ls.sync_archive_labels(arch)
        rec = arch["outcomes"][0]
        assert n == 1
        assert rec["branch_label"] == BRANCH_LABELS["unresolved"]
        assert rec["branch"] == "unresolved"
        assert rec["evidence"] == "判定正文"
        after = dict(rec)
        after.pop("branch_label")
        orig = json.loads(before)
        orig.pop("branch_label")
        assert json.dumps(after, ensure_ascii=False, sort_keys=True) == \
            json.dumps(orig, ensure_ascii=False, sort_keys=True)

    def test_两层都改(self, tmp_path):
        """复核档是门禁与报表**实际读的那一份** ⇒ 只改原档不算改。"""
        _write(tmp_path, _arch([_o("https://a.com", "unresolved", "成功",
                                   branch_before_downgrade=None)]),
               name="T001__r1.json")
        _write(tmp_path, _arch([_o("https://a.com", "unresolved", "成功",
                                   branch_before_downgrade=None)]),
               name="T001__r1.reviewed.json")
        summaries = ls.sync_to_root(tmp_path, dry_run=False)
        assert len(summaries) == 2
        for s in summaries:
            assert s["fixed"] == 1
        for name in ("T001__r1.json", "T001__r1.reviewed.json"):
            rec = _read(tmp_path / name)["outcomes"][0]
            assert rec["branch_label"] == BRANCH_LABELS["unresolved"]

    def test_dry_run不落盘(self, tmp_path):
        p = _write(tmp_path, _arch([_o("https://a.com", "unresolved", "成功",
                                      branch_before_downgrade=None)]))
        summaries = ls.sync_to_root(tmp_path, dry_run=True)
        assert summaries and summaries[0]["fixed"] == 1
        assert _read(p)["outcomes"][0]["branch_label"] == "成功"


class Test命令行:
    def test_dry_run默认(self, tmp_path, capsys):
        from trajectory_pipeline.executor import cli
        _write(tmp_path, _arch([_o("https://a.com", "unresolved", "成功",
                                   branch_before_downgrade=None)]))
        assert cli.main(["sync-labels", "--out", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "**将**改" in out
        assert _read(tmp_path / "T001__r1.json")["outcomes"][0][
            "branch_label"] == "成功"

    def test_apply落盘(self, tmp_path):
        from trajectory_pipeline.executor import cli
        _write(tmp_path, _arch([_o("https://a.com", "unresolved", "成功",
                                   branch_before_downgrade=None)]))
        assert cli.main(["sync-labels", "--out", str(tmp_path), "--apply"]) == 0
        assert _read(tmp_path / "T001__r1.json")["outcomes"][0][
            "branch_label"] == BRANCH_LABELS["unresolved"]
