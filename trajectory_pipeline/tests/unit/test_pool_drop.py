"""``executor.pool_drop`` 的测试。

盯的是**那些真的会咬人的坑**，不是「函数能不能跑」：

* 不给判据就全选 ⇒ 池是 append-only 资产，一���全选等于清空训练集；
* 撤行时把**坏行**顺手清了（坏行可能是被人手工截断、正等人查的）；
* 撤了行却没留痕 ⇒ 半年后只剩「这行没了」，没人知道为什么；
* 撤错了行（判据是 AND 还是 OR 说不清）⇒ 池里少了完全正确的标签，
  而报告只看总数，看不出少了哪条。
"""

from __future__ import annotations

import json

import pytest

from trajectory_pipeline.executor import cli
from trajectory_pipeline.executor import pool_drop as pd


def _pool_row(task_id="T001", url="https://v.qq.com/x.html",
              branch="subscription_wall", **over):
    row = {"task_id": task_id, "url": url, "branch": branch,
           "branch_label": "付费墙", "question": "player.ok",
           "source": "llm", "evidence": "判定正文"}
    row.update(over)
    return row


def _write_pool(tmp_path, rows):
    """写池子。**字符串行原样落盘**（用来塞坏行）。"""
    p = tmp_path / "negative.jsonl"
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
             for r in rows]
    p.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    return p


def _pool_lines(tmp_path):
    return [ln for ln in
            (tmp_path / "negative.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.strip()]


class Test选择器:
    def test_url片段之间是OR(self):
        rows = [_pool_row(url="https://a.com/1"), _pool_row(url="https://b.com/2"),
                _pool_row(url="https://c.com/3")]
        got = pd.select_rows(rows, url_fragments=("a.com", "b.com"))
        assert [r["url"] for r in got] == ["https://a.com/1", "https://b.com/2"]

    def test_不同组之间是AND(self):
        """URL 片段与 task_id 同时给 ⇒ 交集，不是并集。

        池里同一个 URL 可能挂着不同 task 的行（去重键是
        ``(task_id, url, branch)``）⇒ 撤行要精确到一行。
        """
        rows = [_pool_row(task_id="T001", url="https://a.com/1"),
                _pool_row(task_id="T002", url="https://a.com/1")]
        got = pd.select_rows(rows, url_fragments=("a.com",), task_id="T001")
        assert [r["task_id"] for r in got] == ["T001"]

    def test_分支也是AND(self):
        rows = [_pool_row(branch="blocked"), _pool_row(branch="login_required")]
        got = pd.select_rows(rows, url_fragments=("v.qq.com",), branch="blocked")
        assert len(got) == 1 and got[0]["branch"] == "blocked"


class Test撤行:
    def test_不给判据就拒(self, tmp_path):
        """池是 append-only 资产 ⇒ 「不给判据」不能等于「全选」。"""
        _write_pool(tmp_path, [_pool_row()])
        with pytest.raises(ValueError):
            pd.drop_from_root(tmp_path, reason="r", dry_run=False)

    def test_理由必填(self, tmp_path):
        _write_pool(tmp_path, [_pool_row()])
        with pytest.raises(ValueError):
            pd.drop_from_root(tmp_path, url_fragments=("a.com",),
                              reason="  ", dry_run=False)

    def test_dry_run不碰盘(self, tmp_path):
        _write_pool(tmp_path, [_pool_row()])
        out = pd.drop_from_root(tmp_path, url_fragments=("v.qq.com",),
                                reason="r", dry_run=True)
        assert len(out["selected"]) == 1
        assert out["dropped"] == 0
        assert len(_pool_lines(tmp_path)) == 1          # 池没动
        assert not (tmp_path / pd.DROPPED_LOG_NAME).exists()

    def test_apply只撤选中的行(self, tmp_path):
        _write_pool(tmp_path, [_pool_row(url="https://a.com/1"),
                               _pool_row(url="https://b.com/2")])
        out = pd.drop_from_root(tmp_path, url_fragments=("a.com",),
                                reason="r", dry_run=False)
        assert out["dropped"] == 1
        left = _pool_lines(tmp_path)
        assert len(left) == 1
        assert "b.com" in left[0] and "a.com" not in left[0]

    def test_坏行原样保留(self, tmp_path):
        """坏行是**别人正在查的东西**——撤行动作不该顺手清掉它。"""
        _write_pool(tmp_path, [_pool_row(url="https://a.com/1"),
                               "{半截 JSON"])
        pd.drop_from_root(tmp_path, url_fragments=("a.com",),
                          reason="r", dry_run=False)
        left = _pool_lines(tmp_path)
        assert left == ["{半截 JSON"]

    def test_撤掉的行留痕(self, tmp_path):
        """池里没有反向操作 ⇒ 撤掉的行连同理由进审计日志。"""
        _write_pool(tmp_path, [_pool_row(url="https://a.com/1")])
        pd.drop_from_root(tmp_path, url_fragments=("a.com",),
                          reason="结论不成立", dry_run=False)
        log = (tmp_path / pd.DROPPED_LOG_NAME).read_text(encoding="utf-8")
        rec = json.loads(log.strip())
        assert rec["reason"] == "结论不成立"
        assert rec["row"]["url"] == "https://a.com/1"
        assert rec["dropped_at"]

    def test_幂等(self, tmp_path):
        """撤过再撤 ⇒ 撤不到东西，且**不报错**（幂等）。"""
        _write_pool(tmp_path, [_pool_row(url="https://a.com/1")])
        pd.drop_from_root(tmp_path, url_fragments=("a.com",), reason="r",
                          dry_run=False)
        out = pd.drop_from_root(tmp_path, url_fragments=("a.com",), reason="r",
                                dry_run=False)
        assert out["selected"] == [] and out["dropped"] == 0


class Test命令行:
    def test_不给判据就停(self, capsys):
        rc = cli.main(["pool-drop", "--reason", "r"])
        assert rc == 2
        assert "判据" in capsys.readouterr().err

    def test_不给理由就停(self, capsys):
        rc = cli.main(["pool-drop", "--url", "a.com"])
        assert rc == 2
        assert "reason" in capsys.readouterr().err

    def test_默认dry_run(self, tmp_path, capsys):
        _write_pool(tmp_path, [_pool_row(url="https://a.com/1")])
        rc = cli.main(["pool-drop", "--out", str(tmp_path),
                       "--url", "a.com", "--reason", "r"])
        assert rc == 0
        assert "dry-run" in capsys.readouterr().out
        assert len(_pool_lines(tmp_path)) == 1
