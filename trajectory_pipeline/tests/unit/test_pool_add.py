"""``executor.pool_add`` 的测试——**人工补判的负样本要有池行**。

盯的是**真的会咬人的坑**：

* 人工把 ``unresolved`` 改判成真负样本却没并池 ⇒ 门禁报
  ``negative_not_pooled``，而**训练侧吃的是池子**（不是存档）⇒
  这条负样本压根没进训练集（实测 29 条）；
* 无脑并 ⇒ 把「与成功记录矛盾」的标签也并进去，造出训练数据里最毒的
  那种错（一条「这里看不了」正对着一条「这里能看」）；
* 撞键 ⇒ 池里出现两条同键的行，违反去重约定；
* 人工改分支后池里留着旧行 ⇒ 同一个 ``(task_id, url)`` 挂两个分支，
  训练时读到哪条取决于顺序。
"""

from __future__ import annotations

import argparse
import json

from trajectory_pipeline.executor import cli
from trajectory_pipeline.executor import integrity
from trajectory_pipeline.executor import pool_add as pa


# ── 夹具 ──────────────────────────────────────────────────────────


def _arch(task_id="T001", outcomes=None, **over):
    outcomes = outcomes if outcomes is not None else []
    doc = {
        "task_id": task_id, "title": "功夫", "run_id": "r1",
        "driver": "camoufox",
        "provenance": {"persona_id": "parent-喜剧", "content_tier": "长尾",
                       "popularity": "低", "genre": "喜剧"},
        "ledger": {"by_branch": {}, "negative_samples": len(outcomes),
                   "missing_branches": []},
        "outcomes": outcomes,
    }
    doc.update(over)
    return doc


def _o(url, branch, evidence="判定正文", question="player.ok", **over):
    rec = {"url": url, "branch": branch, "evidence": evidence,
           "question": question, "source": "llm",
           "is_negative_sample": branch is not None}
    rec.update(over)
    return rec


def _write(tmp_path, doc, name="T001__r1.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _write_pool(tmp_path, rows):
    p = tmp_path / "negative.jsonl"
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                         for r in rows), encoding="utf-8")
    return p


def _pool_rows(tmp_path):
    p = tmp_path / "negative.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _reviewed(tmp_path, doc, name="T001__r1.reviewed.json"):
    """人工复核档——**门禁与报表实际看的那一份**。"""
    return _write(tmp_path, doc, name=name)


# ── 主路径 ────────────────────────────────────────────────────────


class Test人工补判能并池:
    def test_改判成的真负样本进了池(self, tmp_path):
        """人工把 ``unresolved`` 改判成 ``partial_free`` ⇒ 池里必须有它。

        这是本模块存在的**唯一理由**（2026-10-26 实测 29 条欠账）。
        """
        _write(tmp_path, _arch(outcomes=[_o("https://v.youku.com/x.html",
                                             "unresolved")]))
        _reviewed(tmp_path, _arch(outcomes=[_o(
            "https://v.youku.com/x.html", "partial_free",
            evidence="试看6分钟｜开通会员看完整视频", source="human")]))

        out = pa.add_from_root(tmp_path, dry_run=False)

        assert out["added"] == 1
        rows = _pool_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["branch"] == "partial_free"
        assert rows[0]["url"] == "https://v.youku.com/x.html"
        assert "试看6分钟" in rows[0]["evidence"]
        assert rows[0]["source"] == "human"

    def test_池行字段与写入时同形(self, tmp_path):
        """训练侧按字段读，少一个就少一条解释。"""
        _write(tmp_path, _arch(outcomes=[_o("https://a.com/x",
                                         "subscription_wall")]))
        pa.add_from_root(tmp_path, dry_run=False)

        row = _pool_rows(tmp_path)[0]
        for field in ("task_id", "run_id", "persona_id", "driver", "url",
                      "branch", "branch_label", "evidence", "question",
                      "source", "fallback_used", "reached_play_page",
                      "content_tier", "popularity", "genre"):
            assert field in row, f"池行缺字段 {field}"
        assert row["task_id"] == "T001"
        assert row["persona_id"] == "parent-喜剧"
        assert row["driver"] == "camoufox"
        assert row["branch_label"] == "Subscription-based playback（有播放页，但需订阅/付费）"

    def test_并完门禁不再报没并池(self, tmp_path):
        """**端到端**：并池前门禁报 ``negative_not_pooled``，并完不报。"""
        _write(tmp_path, _arch(outcomes=[_o("https://v.youku.com/x.html",
                                             "unresolved")]))
        _reviewed(tmp_path, _arch(outcomes=[_o("https://v.youku.com/x.html",
                                               "partial_free")]))

        before = integrity.check_root(tmp_path)
        assert "negative_not_pooled" in {f.code for a in before.archives
                                          for f in a.findings}

        pa.add_from_root(tmp_path, dry_run=False)

        after = integrity.check_root(tmp_path)
        assert "negative_not_pooled" not in {f.code for a in after.archives
                                             for f in a.findings}


    def test_人工裁定的理由进了evidence(self, tmp_path):
        """**训练侧只读 ``evidence``**——人的理由不进 evidence，训练样本里
        就会出现「evidence 说『无证据不记负样本』」配一个负样本标签，
        理由与标签互相打脸。

        复核档里 outcome 的 ``evidence`` 保留的是**代码原判**正文
        （要靠它测人工与代码的分歧率），人的理由在 ``review_note`` 里。
        """
        _write(tmp_path, _arch(outcomes=[_o("https://a.com/x", "unresolved",
                                            evidence="代码原判：模型没答")]))
        _reviewed(tmp_path, _arch(outcomes=[_o(
            "https://a.com/x", "not_play_site",
            evidence="代码原判：模型没答", source="human",
            review_verdict="confirm_not_play_site", reviewed_by="reesn",
            review_note="门户导航站首页：站点形态即结论",
            branch_before_review="unresolved")]))

        pa.add_from_root(tmp_path, dry_run=False)

        row = _pool_rows(tmp_path)[0]
        assert "代码原判：模型没答" in row["evidence"], "代码原判正文要留着"
        assert "门户导航站首页：站点形态即结论" in row["evidence"], \
            "人工裁定的理由没进 evidence"
        assert row["source"] == "human"
        assert row["review_verdict"] == "confirm_not_play_site"
        assert row["reviewed_by"] == "reesn"
        assert row["branch_before_review"] == "unresolved"


class Test幂等:
    def test_再跑一次不重复加(self, tmp_path):
        """池是 append-only 资产，**同键只能有一行**。"""
        _write(tmp_path, _arch(outcomes=[_o("https://a.com/x",
                                         "subscription_wall")]))
        pa.add_from_root(tmp_path, dry_run=False)
        second = pa.add_from_root(tmp_path, dry_run=False)

        assert second["added"] == 0
        assert second["candidates"] == []
        assert len(_pool_rows(tmp_path)) == 1

    def test_同一份存档里同键有多行只并一行(self, tmp_path):
        """同一 URL 被访问两次 ⇒ 存档里两行同键 ⇒ 池里**只能有一行**。"""
        _write(tmp_path, _arch(outcomes=[
            _o("https://a.com/x", "subscription_wall"),
            _o("https://a.com/x", "subscription_wall"),
        ]))
        out = pa.add_from_root(tmp_path, dry_run=False)

        assert out["added"] == 1
        assert len(_pool_rows(tmp_path)) == 1

    def test_dry_run不写盘(self, tmp_path):
        _write(tmp_path, _arch(outcomes=[_o("https://a.com/x",
                                         "subscription_wall")]))
        out = pa.add_from_root(tmp_path, dry_run=True)

        assert out["added"] == 0
        assert len(out["candidates"]) == 1
        assert not (tmp_path / "negative.jsonl").exists()


# ── 三条拒并条件 ──────────────────────────────────────────────────


class Test有分歧不并:
    def test_已拒收的URL不并(self, tmp_path):
        """人工判「整条 URL 拒收」⇒ 并进去就是与人工裁定打架。"""
        _write(tmp_path, _arch(outcomes=[_o("https://a.com/x",
                                         "subscription_wall")]))
        (tmp_path / "quarantine.jsonl").write_text(
            json.dumps({"url": "https://a.com/x"}, ensure_ascii=False) + "\n",
            encoding="utf-8")

        out = pa.add_from_root(tmp_path, dry_run=False)

        assert out["added"] == 0
        assert out["skipped"][0].refuse == "quarantined"
        assert _pool_rows(tmp_path) == []

    def test_与成功记录矛盾的不并(self, tmp_path):
        """同 URL 在别的存档里被判成功 ⇒ 并进去就造出自相矛盾的标签。

        训练数据里最毒的一种错：一条「这里看不了」正对着一条「这里能看」。
        """
        _write(tmp_path, _arch(outcomes=[_o("https://a.com/x",
                                         "subscription_wall")]))
        _write(tmp_path, _arch(task_id="T002",
                               outcomes=[_o("https://a.com/x", None,
                                           evidence="LLM 判 True：能看")]),
               name="T002__r9.json")

        out = pa.add_from_root(tmp_path, dry_run=False)

        assert out["added"] == 0
        assert out["skipped"][0].refuse == "contradicted"
        assert "T002" in out["skipped"][0].detail
        assert _pool_rows(tmp_path) == []

    def test_池内同址另有分支的不并(self, tmp_path):
        """人工改判的旧行还在池里 ⇒ 先走 ``cli relabel``（两边一起改）。

        不并是为了不出现「同 ``(task_id, url)`` 两个分支」——
        训练时读到哪条取决于顺序。
        """
        _write(tmp_path, _arch(outcomes=[_o("https://a.com/x",
                                         "component_unverified")]))
        _write_pool(tmp_path, [{"task_id": "T001", "url": "https://a.com/x",
                                 "branch": "not_play_site",
                                 "evidence": "旧判"}])

        out = pa.add_from_root(tmp_path, dry_run=False)

        assert out["added"] == 0
        assert out["skipped"][0].refuse == "stale_branch"
        assert "not_play_site" in out["skipped"][0].detail
        assert len(_pool_rows(tmp_path)) == 1, "旧行不该被动"


class Test口径:
    def test_无题的兜底分支不进候选(self, tmp_path):
        """``unresolved`` / ``trailer_suspect`` / ``player_unverified``
        不是负样本——混进池会毁掉「这里真的看不了」这条信号。"""
        _write(tmp_path, _arch(outcomes=[
            _o("https://a.com/x", "unresolved"),
            _o("https://b.com/y", "trailer_suspect"),
            _o("https://c.com/z", "player_unverified"),
        ]))
        out = pa.add_from_root(tmp_path, dry_run=False)

        assert out["candidates"] == []
        assert _pool_rows(tmp_path) == []

    def test_采集侧账不进候选(self, tmp_path):
        """``blocked`` 是「这次没进去」，不是「这里看不了」。"""
        _write(tmp_path, _arch(outcomes=[_o("https://a.com/x",
                                         "blocked")]))
        out = pa.add_from_root(tmp_path, dry_run=False)

        assert out["candidates"] == []


# ── 接线：回填时自动并池 ─────────────────────────────────────────


class Test回填接线:
    def test_review_apply把改判的真负样本并进池(self, tmp_path):
        """**接线本身**也要有测试——模块有测试 ≠ 接线有测试。

        ``review --apply`` 是人工回填的落盘点，M-40 的修法就是
        「回填 ⇒ 顺带并池」。这条不测，那个缺口会原样漏到生产
        （2026-10-26 实测：29 条人工判定的负样本从没进过池）。
        """
        url = "https://v.youku.com/x.html"
        _write(tmp_path, _arch(outcomes=[_o(url, "unresolved")]))
        vf = tmp_path / "verdicts.jsonl"
        vf.write_text(json.dumps({
            "landed_url": url, "verdict": "confirm_paywall",
            "reviewed_by": "reesn",
            "note": "试看6分钟｜开通会员看完整视频",
        }, ensure_ascii=False) + "\n", encoding="utf-8")

        rc = cli.cmd_apply_review(argparse.Namespace(
            out=str(tmp_path), verdicts=str(vf)))

        assert rc == 0
        rows = _pool_rows(tmp_path)
        assert len(rows) == 1, "改判出的真负样本必须有一条池行"
        assert rows[0]["branch"] == "subscription_wall"
        assert rows[0]["source"] == "human"
        assert "试看6分钟" in rows[0]["evidence"], "人工的理由要折进 evidence"
        # 复核档也在（回填确实落了盘）
        assert (tmp_path / "T001__r1.reviewed.json").exists()

    def test_回填后门禁不再报没并池(self, tmp_path):
        """**端到端**：回填（顺带并池）之后，门禁不再报 ``negative_not_pooled``。

        ⚠️ **回填前门禁不报**——回填前那条是 ``unresolved``
        （``NON_SAMPLE_BRANCHES`` 之一，**本来就不是负样本**）。
        历史缺陷的状态是「**复核档里已经改判成真负样本、池里却没有**」，
        那正是 M-40 修掉的那一层。
        """
        url = "https://v.youku.com/x.html"
        _write(tmp_path, _arch(outcomes=[_o(url, "unresolved")]))
        vf = tmp_path / "verdicts.jsonl"
        vf.write_text(json.dumps({
            "landed_url": url, "verdict": "confirm_paywall",
            "reviewed_by": "reesn", "note": "开通会员看完整视频",
        }, ensure_ascii=False) + "\n", encoding="utf-8")

        cli.cmd_apply_review(argparse.Namespace(
            out=str(tmp_path), verdicts=str(vf)))

        after = integrity.check_root(tmp_path)
        assert "negative_not_pooled" not in {f.code for a in after.archives
                                             for f in a.findings}
