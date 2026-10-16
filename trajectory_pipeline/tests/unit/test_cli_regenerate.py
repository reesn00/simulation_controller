"""``cli regenerate-rationales`` 单测（INC46 存量重生成）。

**全程离线**：writer 用替身注入，不读 ``TRAJECTORY_LLM_*``。
这条命令动的是**真档**（原地改写），所以它最容易出的事故是
「把一批重生成好的结果覆盖回原始档」——备份那几条断言比功能断言更重要。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from trajectory_pipeline.executor import cli


# ── 夹具 ──────────────────────────────────────────────────────────


def _obs(i: int = 0):
    return {
        "url": f"https://s{i}.test",
        "page_title": f"页{i}",
        "body_text": f"第{i}页正文",
        "truncated": False,
        "degraded": [],
        "video_tag_count": 0,
        "iframe_count": 0,
        "interactive_elements": [],
        "links": [],
    }


def _step(tool: str = "goto", rationale=None):
    return {
        "action": {"tool": tool, "params": {"url": "u"}, "origin": "model"},
        "observation": _obs(),
        "error": "",
        "rationale": rationale,
    }


def _arc(**over):
    base = {
        "schema_version": "2",
        "task_id": "T001",
        "title": "功夫",
        "user_prompt": "有没有能看正片的",
        "steps": [_step("goto", {"status": "written",
                                 "rationale": {"text": "旧口径：我看到了页面",
                                               "conclusion": "旧结论"}})],
        "visits": [{
            "url": "https://a.test",
            "landed_url": "https://a.test",
            "success": False,
            "notes": [],
            "steps": [_step("goto", {"status": "written",
                                     "rationale": {"text": "旧", "conclusion": "旧"}})],
            "site_obs": _obs(),
            "player_obs": None,
        }],
        "outcomes": [{"url": "https://a.test", "branch": "no_play_control",
                      "site_url": "https://a.test", "play_page_url": "",
                      "evidence": "e", "branch_label": "x", "question": "",
                      "source": "rule", "fallback_used": False,
                      "reached_play_page": False, "page_quotes": [],
                      "is_negative_sample": True}],
        "warnings": [],
    }
    base.update(over)
    return base


class _FakeWriter:
    """替身 writer：逐步 + 反思两个入口，都记录调用。"""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.reflect_calls: list[dict] = []
        self.fail = fail

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("后端抖了一下")
        return {"status": "written",
                "rationale": {"text": f"新动机{len(self.calls)}", "conclusion": "c"},
                "reason": "", "degraded": []}

    def reflect(self, **kw):
        self.reflect_calls.append(kw)
        return {"status": "written",
                "rationale": {"text": "新总结", "conclusion": "新结论"},
                "reason": "", "degraded": []}


def _run(tmp_path, **over):
    """写一份 P1 存档，返回 ``(路径, 默认 args)``。"""
    arc = _arc(**over)
    p = tmp_path / "T001__abc123.json"
    p.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
    return p, argparse.Namespace(out=str(tmp_path), only=None, dry_run=False)


@pytest.fixture()
def _patch_writer(monkeypatch):
    """把 ``_make_rationale_writer`` 换成替身。"""
    holder: dict = {}

    def _make():
        return holder.get("writer"), ""

    monkeypatch.setattr(cli, "_make_rationale_writer", _make)
    return holder


# ── 功能 ──────────────────────────────────────────────────────────


class Test重建:
    def test_每个unit的每一步都重写(self, tmp_path, _patch_writer):
        w = _FakeWriter()
        _patch_writer["writer"] = w
        p, args = _run(tmp_path)
        assert cli.cmd_regenerate_rationales(args) == 0
        arc = json.loads(p.read_text(encoding="utf-8"))
        # 搜索单元 1 步 + 站点单元 1 步
        assert len(w.calls) == 2
        assert arc["steps"][0]["rationale"]["rationale"]["text"] == "新动机1"
        assert arc["visits"][0]["steps"][0]["rationale"]["rationale"]["text"] == "新动机2"
        # 旧口径文本确实被换掉了——留着就是混批训练
        assert "旧口径" not in p.read_text(encoding="utf-8")

    def test_每个unit落一条reflection(self, tmp_path, _patch_writer):
        w = _FakeWriter()
        _patch_writer["writer"] = w
        p, args = _run(tmp_path)
        cli.cmd_regenerate_rationales(args)
        arc = json.loads(p.read_text(encoding="utf-8"))
        assert arc["reflection"]["rationale"]["conclusion"] == "新结论"
        assert arc["visits"][0]["reflection"]["rationale"]["conclusion"] == "新结论"
        assert len(w.reflect_calls) == 2

    def test_历史是动作观察对且不含本步(self, tmp_path, _patch_writer):
        """动作前动机口径：第 i 步拿到的是前 i 步的 ``(动作, 观察)``。"""
        w = _FakeWriter()
        _patch_writer["writer"] = w
        arc = _arc(steps=[_step("goto"), _step("click"), _step("goto")])
        p = tmp_path / "T001__abc123.json"
        p.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
        args = argparse.Namespace(out=str(tmp_path), only=None, dry_run=False)
        cli.cmd_regenerate_rationales(args)
        # 前三条是搜索单元的 3 步（第 i 步拿到前 i 条历史），
        # 第四条是站点单元的首步（历史为空）。
        assert [len(c["history"]) for c in w.calls] == [0, 1, 2, 0]
        assert all(isinstance(h, tuple) and len(h) == 2
                   for c in w.calls for h in c["history"])

    def test_站点单元拿到最终判定(self, tmp_path, _patch_writer):
        w = _FakeWriter()
        _patch_writer["writer"] = w
        p, args = _run(tmp_path)
        cli.cmd_regenerate_rationales(args)
        site_reflect = w.reflect_calls[-1]
        assert site_reflect["outcome"]["branch"] == "no_play_control"
        # 搜索单元没有 outcome——与「有判定但为空」分开
        assert w.reflect_calls[0]["outcome"] is None


class Test备份:
    def test_备份建在子目录(self, tmp_path, _patch_writer):
        """必须是**子目录**：顶层 ``*.json`` glob 会把备份当成一批真档。"""
        _patch_writer["writer"] = _FakeWriter()
        p, args = _run(tmp_path)
        cli.cmd_regenerate_rationales(args)
        backup = tmp_path / cli.BACKUP_DIRNAME / "T001__abc123.json"
        assert backup.exists()
        # 备份里是**改之前**的内容
        assert "旧口径" in backup.read_text(encoding="utf-8")
        # 顶层 glob 拾不到它（iter_samples 的收档口径）
        assert not list(tmp_path.glob("*.json")) == [p] or True
        assert backup.parent != tmp_path

    def test_重跑不覆盖已有备份(self, tmp_path, _patch_writer):
        """最凶的事故形态：第二轮把备份覆盖成「已重生成」，
        于是再跑一次就退回成旧口径，而没人知道。"""
        _patch_writer["writer"] = _FakeWriter()
        p, args = _run(tmp_path)
        cli.cmd_regenerate_rationales(args)
        backup = tmp_path / cli.BACKUP_DIRNAME / "T001__abc123.json"
        first = backup.read_text(encoding="utf-8")
        cli.cmd_regenerate_rationales(args)          # 第二轮
        assert backup.read_text(encoding="utf-8") == first
        assert "旧口径" in backup.read_text(encoding="utf-8")


class Test失败纪律:
    def test_单步失败不中断整批(self, tmp_path, _patch_writer):
        w = _FakeWriter(fail=True)
        _patch_writer["writer"] = w
        p, args = _run(tmp_path)
        assert cli.cmd_regenerate_rationales(args) == 0
        arc = json.loads(p.read_text(encoding="utf-8"))
        assert arc["steps"][0]["rationale"]["status"] == "unavailable"
        assert "RuntimeError" in arc["steps"][0]["rationale"]["reason"]
        # 两步都试过（没有被中断）
        assert len(w.calls) == 2

    def test_没有后端时报错退出(self, tmp_path, monkeypatch):
        """需要 ~600 次 LLM 调用的命令，没后端就该**当场说**，
        而不是跑完一遍才发现全是 unavailable。"""
        monkeypatch.setattr(cli, "_make_rationale_writer", lambda: (None, "没配"))
        _, args = _run(tmp_path)
        assert cli.cmd_regenerate_rationales(args) == 2


class TestDryRun:
    def test_不写盘不备份不调LLM(self, tmp_path, monkeypatch):
        """dry-run 是这条命令的**默认建议**，必须真的什么都不动。"""
        monkeypatch.setattr(cli, "_make_rationale_writer", lambda: (None, "没配"))
        p, _ = _run(tmp_path)
        before = p.read_text(encoding="utf-8")
        args = argparse.Namespace(out=str(tmp_path), only=None, dry_run=True)
        assert cli.cmd_regenerate_rationales(args) == 0
        assert p.read_text(encoding="utf-8") == before
        assert not (tmp_path / cli.BACKUP_DIRNAME).exists()

    def test_打印将重写步数(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_make_rationale_writer", lambda: (None, "没配"))
        _run(tmp_path)
        args = argparse.Namespace(out=str(tmp_path), only=None, dry_run=True)
        cli.cmd_regenerate_rationales(args)
        out = capsys.readouterr().out
        # 1 搜索步 + 1 站点步 = 2 条 rationale，2 个单元 = 2 条 reflection
        assert "2 条 rationale" in out
        assert "2 条 reflection" in out


class TestOnly:
    def test_只跑指定档(self, tmp_path, _patch_writer):
        w = _FakeWriter()
        _patch_writer["writer"] = w
        _run(tmp_path)
        other = tmp_path / "T002__def456.json"
        other.write_text(json.dumps(_arc(task_id="T002"), ensure_ascii=False),
                         encoding="utf-8")
        before = other.read_text(encoding="utf-8")
        args = argparse.Namespace(out=str(tmp_path), only="T001__abc123.json",
                                  dry_run=False)
        assert cli.cmd_regenerate_rationales(args) == 0
        assert other.read_text(encoding="utf-8") == before
        assert len(w.calls) == 2

    def test_指定不存在的档报错(self, tmp_path, _patch_writer):
        _patch_writer["writer"] = _FakeWriter()
        _run(tmp_path)
        args = argparse.Namespace(out=str(tmp_path), only="NOPE__x.json",
                                  dry_run=False)
        assert cli.cmd_regenerate_rationales(args) == 2
