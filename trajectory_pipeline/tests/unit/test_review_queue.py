"""人工复核队列的测试。

重点不是"能不能抽出来"，而是**复核员拿到的东西够不够做判断**——
上下文不全的队列等于把判断责任转嫁给复核员却没有给材料，
那种队列会退化成"打开网址自己再看一遍"，也就没有存在意义。
"""

from __future__ import annotations

import json

import pytest

from trajectory_pipeline.executor.review_queue import (
    REVIEW_BRANCHES,
    VERDICTS,
    ReviewQueueError,
    apply_verdicts,
    collect,
    extract_items,
    write_queue,
)


def _archive(outcomes: list[dict], visits: list[dict] | None = None) -> dict:
    return {
        "task_id": "T001",
        "title": "功夫",
        "query": "功夫 在线观看",
        "outcomes": outcomes,
        "visits": visits if visits is not None else [],
        "ledger": {"total_sites": len(outcomes), "by_branch": {}},
    }


def _visit(url: str, *, landed: str | None = None, **obs) -> dict:
    base = {
        "url": url,
        "landed_url": landed or url,
        "success": False,
        "notes": [],
        "player_obs": {
            "url": landed or url,
            "page_title": "功夫 - 搜狐视频",
            "body_preview": "功夫 在线播放 高清完整版",
            "body_len": 4210,
            "degraded": [],
            "video_tag_count": 1,
            "iframe_count": 3,
            "interactive_elements": [
                {"ref": "e1", "tag": "button", "label": "立即播放"},
                {"ref": "e2", "tag": "a", "label": "第1集"},
            ],
            **obs,
        },
        "site_obs": None,
    }
    return base


class TestExtract:
    def test_只抽需人工的分支(self):
        arch = _archive([
            {"url": "a.com", "branch": "unresolved", "evidence": "x"},
            {"url": "b.com", "branch": "trailer_suspect", "evidence": "y"},
            {"url": "c.com", "branch": "no_play_control", "evidence": "z"},
            {"url": "d.com", "branch": None, "evidence": "ok"},
        ])
        items = extract_items(arch)
        assert [i.url for i in items] == ["a.com", "b.com"]

    def test_真负样本不进队列(self):
        """代码已给出确定性结论的，复核它们属于抽检，不是本队列的事。"""
        assert "no_play_control" not in REVIEW_BRANCHES
        assert "component_unverified" not in REVIEW_BRANCHES

    def test_带齐复核所需上下文(self):
        arch = _archive(
            [{"url": "https://tv.sohu.com/kan", "branch": "unresolved",
              "evidence": "未见 <video>（iframe=3）", "reached_play_page": True}],
            [_visit("https://tv.sohu.com/kan")],
        )
        it = extract_items(arch)[0]
        assert it.page_title == "功夫 - 搜狐视频"
        assert it.video_tag_count == 1
        assert it.iframe_count == 3
        assert it.elements and it.elements[0]["label"] == "立即播放"
        assert "iframe=3" in it.evidence
        assert it.reached_play_page

    def test_按landed_url关联观察(self):
        """点击后落地 URL 变了，观察得跟着落地那份，不是候选那份。"""
        arch = _archive(
            [{"url": "https://a.com/x", "branch": "unresolved", "evidence": "e"}],
            [_visit("https://a.com/x", landed="https://a.com/play/1")],
        )
        it = extract_items(arch)[0]
        assert it.landed_url == "https://a.com/play/1"

    def test_找不到观察时不崩(self):
        arch = _archive([{"url": "z.com", "branch": "unresolved", "evidence": "e"}])
        it = extract_items(arch)[0]
        assert it.page_title == "" and it.video_tag_count == 0

    def test_元素样本有上限(self):
        arch = _archive(
            [{"url": "u.com", "branch": "unresolved", "evidence": "e"}],
            [_visit("u.com", interactive_elements=[
                {"ref": f"e{i}", "tag": "a", "label": f"L{i}"} for i in range(200)
            ])],
        )
        assert len(extract_items(arch)[0].elements) == 25


class TestApplyVerdicts:
    def test_确认成功改为成功样本(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved",
                          "evidence": "e", "reached_play_page": True}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "confirm_success",
                                                  "reviewed_by": "张"}})
        rec = out["outcomes"][0]
        assert rec["branch"] is None
        assert rec["source"] == "human"
        assert rec["is_negative_sample"] is False
        assert st["confirm_success"] == 1

    def test_保留代码原始判定(self):
        """覆盖掉就永远测不出"人工与代码的分歧率"。"""
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}])
        out, _ = apply_verdicts(arch, {"a.com": {"verdict": "confirm_success"}})
        assert out["outcomes"][0]["branch_before_review"] == "unresolved"

    def test_确认失败按是否到达播放页分岔(self):
        arch = _archive([
            {"url": "reached.com", "branch": "unresolved", "evidence": "e",
             "reached_play_page": True},
            {"url": "site.com", "branch": "unresolved", "evidence": "e",
             "reached_play_page": False},
        ])
        out, _ = apply_verdicts(arch, {
            "reached.com": {"verdict": "confirm_negative"},
            "site.com": {"verdict": "confirm_negative"},
        })
        assert out["outcomes"][0]["branch"] == "component_unverified"
        assert out["outcomes"][1]["branch"] == "no_play_control"

    def test_裁定为预告片(self):
        arch = _archive([{"url": "a.com", "branch": "trailer_suspect",
                          "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "trailer"}})
        assert out["outcomes"][0]["branch"] == "trailer_only"
        assert st["trailer"] == 1

    def test_待看与跳过不动分支(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "need_browser"}})
        assert out["outcomes"][0]["branch"] == "unresolved"
        assert out["outcomes"][0]["review_pending"] is True
        assert st["skipped"] == 1

    def test_未裁定保持原样(self):
        """不默认成功也不默认丢弃。"""
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}])
        out, st = apply_verdicts(arch, {})
        assert out["outcomes"][0] == arch["outcomes"][0]
        assert st["unreviewed"] == 1

    def test_未知裁定显式抛(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}])
        with pytest.raises(ReviewQueueError, match="未知裁定"):
            apply_verdicts(arch, {"a.com": {"verdict": "looks_good_to_me"}})

    def test_记复核人与备注(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}])
        out, _ = apply_verdicts(arch, {"a.com": {
            "verdict": "confirm_success", "reviewed_by": "李",
            "note": "播放器确实能播"}})
        rec = out["outcomes"][0]
        assert rec["reviewed_by"] == "李"
        assert rec["review_note"] == "播放器确实能播"

    def test_真负样本不被回填覆盖(self):
        """误填一个 url 就把 no_play_control 改成成功 = 静默毁掉负样本池。"""
        arch = _archive([{"url": "a.com", "branch": "no_play_control", "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "confirm_success"}})
        assert out["outcomes"][0]["branch"] == "no_play_control"
        assert st["ignored"] == 1
        assert st["confirm_success"] == 0

    def test_成功样本不被回填改写(self):
        arch = _archive([{"url": "a.com", "branch": None, "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "confirm_negative"}})
        assert out["outcomes"][0]["branch"] is None
        assert st["ignored"] == 1

    def test_verdicts枚举是闭合的(self):
        assert set(VERDICTS) == {"confirm_success", "confirm_negative",
                                 "trailer", "need_browser", "skip"}


class TestIO:
    def test_写出可回读(self, tmp_path):
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}],
                        [_visit("a.com")])
        items = extract_items(arch)
        p = tmp_path / "q.jsonl"
        assert write_queue(p, items) == 1
        row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        assert row["verdict"] == ""
        assert row["verdict_options"] == dict(VERDICTS)
        assert row["video_tag_count"] == 1

    def test_collect跳过非法json(self, tmp_path):
        (tmp_path / "good.json").write_text(
            json.dumps(_archive([{"url": "a", "branch": "unresolved", "evidence": "e"}])),
            encoding="utf-8")
        (tmp_path / "bad.json").write_text("{ not json", encoding="utf-8")
        assert len(collect(tmp_path)) == 1

    def test_collect跳过review文件本身(self, tmp_path):
        """否则把队列当存档重跑会自我复制。"""
        (tmp_path / "a_review.jsonl").write_text("{}\n", encoding="utf-8")
        assert collect(tmp_path) == []

    def test_目录不存在显式抛(self, tmp_path):
        with pytest.raises(ReviewQueueError, match="不是目录"):
            collect(tmp_path / "nope")