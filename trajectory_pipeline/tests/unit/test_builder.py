"""``assembler.builder`` 的单元测试——落盘契约 + 计数 + 失败语义。

切条语义本身在 :mod:`test_assembler`（``split_archive`` 的全套），
本测试只盯 builder **多出来**的几件事：

- 落盘路径与文件名
- 原子写（``.tmp`` 中转）
- 失败样本不打断整批
- ``overwrite`` 默认 False 的契约
- 真负样本计数
- 不 import executor
- 训练态观察走 ``render``（ref 剥掉、limits 留痕）
"""

from __future__ import annotations

import ast
import json
import pathlib
from pathlib import Path

import pytest

from trajectory_pipeline.assembler import builder, schema


def _obs(url="u", **kw):
    base = {"url": url, "page_title": "T", "body_text": "正文", "truncated": False,
            "degraded": [], "video_tag_count": 0, "iframe_count": 0,
            "interactive_elements": [], "links": []}
    base.update(kw)
    return base


def _visit(url, landed=None, success=False, **over):
    return {"url": url, "landed_url": landed or url, "success": success,
            "notes": [], "steps": [], "site_obs": _obs(landed or url),
            "player_obs": None, **over}


def _outcome(url, branch):
    return {"url": url, "branch": branch, "branch_label": "", "evidence": "e",
            "question": "player.ok", "source": "rule", "fallback_used": False,
            "reached_play_page": True, "is_negative_sample": False}


def _arc(**over):
    base = {
        "task_id": "T001", "title": "功夫", "query": "功夫 在线观看",
        "user_prompt": "有没有能看正片的", "search_url": "https://s.test",
        "search_observation": _obs("https://s.test"), "search_blocked": "",
        "steps": [], "provenance": {"genre": "喜剧"},
        "run_config": {"engine": "baidu", "max_candidates": 20,
                       "max_chars": None, "stop_after_success": 0,
                       "per_site_timeout_s": 90.0},
        "candidates": [], "visits": [], "outcomes": [], "ledger": None,
        "warnings": [],
    }
    base.update(over)
    return base


def _write(tmp_path, arc, name="T001__abc123.json"):
    p = tmp_path / "src" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
    return p


class Test落盘形状:
    def test_样本文件名(self, tmp_path):
        src = _write(tmp_path, _arc())
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        files = sorted(p.name for p in out.glob("*.json"))
        assert files == ["T001__abc123__search.json"]

    def test_多单元切出多文件(self, tmp_path):
        src = _write(tmp_path, _arc(
            visits=[_visit("https://a.test/"), _visit("https://b.test/")],
            outcomes=[_outcome("https://a.test/", "no_play_control"),
                      _outcome("https://b.test/", None)],
        ))
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        files = sorted(p.name for p in out.glob("*.json"))
        assert files == ["T001__abc123__search.json",
                         "T001__abc123__site-1.json",
                         "T001__abc123__site-2.json"]

    def test_写出JSON可读(self, tmp_path):
        src = _write(tmp_path, _arc())
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        sample_file = out / "T001__abc123__search.json"
        json.loads(sample_file.read_text(encoding="utf-8"))

    def test_写入UTF8可直读中文(self, tmp_path):
        src = _write(tmp_path, _arc(
            user_prompt="有没有能看正片的 给个链接呗",
            search_observation=_obs("https://s.test", body_text="功夫 正片 播放"),
        ))
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        sample = json.loads(
            (out / "T001__abc123__search.json").read_text(encoding="utf-8")
        )
        assert sample["six"]["user_prompt"] == "有没有能看正片的 给个链接呗"
        assert "功夫" in sample["six"]["observations"][0]["body_text"]


class Test原子写:
    def test_落盘不留_tmp(self, tmp_path):
        src = _write(tmp_path, _arc())
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        assert not list(out.glob("*.tmp"))
        assert not (out / "T001__abc123__search.json.tmp").exists()

    def test_samples目录自动建(self, tmp_path):
        src = _write(tmp_path, _arc())
        assert not (tmp_path / "samples").exists()
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        assert (tmp_path / "samples").is_dir()


class Test观察裁剪:
    def test_训练态观察不带ref(self, tmp_path):
        src = _write(tmp_path, _arc(search_observation=_obs("u", interactive_elements=[
            {"ref": "e1", "tag": "button", "label": "播放"},
        ])))
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        sample = json.loads(
            (out / "T001__abc123__search.json").read_text(encoding="utf-8")
        )
        elem = sample["six"]["observations"][0]["interactive_elements"][0]
        assert "ref" not in elem
        assert elem == {"tag": "button", "label": "播放"}

    def test_训练态观察带limits(self, tmp_path):
        long_body = "x" * 5000
        src = _write(tmp_path, _arc(search_observation=_obs("u", body_text=long_body)))
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        sample = json.loads(
            (out / "T001__abc123__search.json").read_text(encoding="utf-8")
        )
        obs = sample["six"]["observations"][0]
        assert obs["body_chars"] == 2000
        assert obs["limits"]["body_chars"] == 2000
        assert obs["limits"]["cropped"] is True


class Test负样本计数:
    def test_真负样本分支计入(self, tmp_path):
        src = _write(tmp_path, _arc(
            visits=[_visit("https://a.test/"), _visit("https://b.test/")],
            outcomes=[_outcome("https://a.test/", "no_play_control"),
                      _outcome("https://b.test/", "unresolved")],
        ))
        res = builder.build_from_archive(src, root=tmp_path, overwrite=True)
        assert res.negatives == 1

    def test_成功访问不计入(self, tmp_path):
        src = _write(tmp_path, _arc(
            visits=[_visit("https://a.test/")],
            outcomes=[_outcome("https://a.test/", None)],
        ))
        res = builder.build_from_archive(src, root=tmp_path, overwrite=True)
        assert res.negatives == 0

    def test_非负样本分支不计入(self, tmp_path):
        """``unresolved`` / ``trailer_suspect`` / ``player_unverified`` 都不是
        负样本（见 ``executor/branches.NON_SAMPLE_BRANCHES``）。"""
        for br in ("unresolved", "trailer_suspect", "player_unverified"):
            src = _write(tmp_path, _arc(
                visits=[_visit("https://a.test/")],
                outcomes=[_outcome("https://a.test/", br)],
            ), name=f"T001__{br}.json")
            res = builder.build_from_archive(src, root=tmp_path, overwrite=True)
            assert res.negatives == 0, f"{br} 被误计为负样本"


class Test降级样本:
    def test_降级标记保留在P2(self, tmp_path):
        arc = _arc()
        arc.pop("run_config")
        src = _write(tmp_path, arc)
        res = builder.build_from_archive(src, root=tmp_path, overwrite=True)
        assert res.degraded == 1


class TestOverwrite:
    def test_默认不覆盖(self, tmp_path):
        src = _write(tmp_path, _arc())
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        res2 = builder.build_from_archive(src, root=tmp_path)
        assert res2.written == 0
        assert "T001__abc123__search" in res2.skipped[0]

    def test_显式overwrite才覆盖(self, tmp_path):
        src = _write(tmp_path, _arc())
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        (out / "T001__abc123__search.json").write_text("坏", encoding="utf-8")
        res = builder.build_from_archive(src, root=tmp_path, overwrite=True)
        assert res.written == 1
        json.loads((out / "T001__abc123__search.json").read_text(encoding="utf-8"))


class TestBuildAll:
    def test_全目录遍历(self, tmp_path):
        _write(tmp_path, _arc(task_id="T001"), "T001__a1.json")
        _write(tmp_path, _arc(task_id="T002"), "T002__b2.json")
        _write(tmp_path, _arc(task_id="T003"), "T003__c3.json")
        (tmp_path / "src" / "obscura_tools.json").write_text(
            '{"tools": []}', encoding="utf-8")
        report = builder.build_all(source_root=tmp_path / "src",
                                    target_root=tmp_path, overwrite=True)
        assert report.total_written == 3
        assert report.total_skipped == 0

    def test_坏档不打断整批(self, tmp_path):
        _write(tmp_path, _arc(task_id="T001"), "T001__a1.json")
        bad = tmp_path / "src" / "T002__bad.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text('{"visits": []}', encoding="utf-8")
        _write(tmp_path, _arc(task_id="T003"), "T003__c3.json")

        report = builder.build_all(source_root=tmp_path / "src",
                                    target_root=tmp_path, overwrite=True)
        assert report.total_written == 2
        bad_arc = next(a for a in report.archives if a.archive == "T002__bad.json")
        assert bad_arc.written == 0
        assert bad_arc.skipped


class Test报告:
    def test_单份结果字段(self, tmp_path):
        src = _write(tmp_path, _arc(
            visits=[_visit("https://a.test/")],
            outcomes=[_outcome("https://a.test/", "no_play_control")],
        ))
        res = builder.build_from_archive(src, root=tmp_path, overwrite=True)
        assert res.archive == "T001__abc123.json"
        assert res.sample_count == 2
        assert res.written == 2
        assert res.negatives == 1
        # fixture 故意 _arc(steps=[])：两条样本都缺 steps 走降级。
        # 真实批次里只要 P1 有 steps[]，degraded=0。
        assert res.degraded == 2

    def test_BuildReport_ok(self, tmp_path):
        _write(tmp_path, _arc(), "T001__a.json")
        report = builder.build_all(source_root=tmp_path / "src",
                                    target_root=tmp_path, overwrite=True)
        assert report.ok is True

    def test_BuildReport_非ok当有跳过的样本(self, tmp_path):
        _write(tmp_path, _arc(), "T001__a.json")
        builder.build_all(source_root=tmp_path / "src",
                          target_root=tmp_path, overwrite=True)
        report2 = builder.build_all(source_root=tmp_path / "src",
                                     target_root=tmp_path)
        assert report2.ok is False
        assert report2.total_skipped == 1


class Test产物根:
    def test_落在trajectory_pipeline下而不是仓库根(self):
        """``parents[2]`` 会落到**仓库根**的 ``output/pipeline/``——
        CLAUDE.md 明令禁止碰的存量产物目录（D8）。而它**不报错**：
        ``build_all`` 只是遍历到空目录、返回 0 条，
        看起来像「这批没切出样本」。

        路径形状 ``<包根>/output/pipeline/samples``。
        """
        resolved = builder.samples_dir().resolve()
        assert resolved.name == "samples"
        assert resolved.parent.name == "pipeline"
        assert resolved.parent.parent.name == "output"
        assert resolved.parent.parent.parent.name == "trajectory_pipeline", (
            f"产物落到了 {resolved}——仓库根的 output/ 是存量目录，"
            f"新树产物必须落在 trajectory_pipeline/output/（D8）")


class Test契约:
    def test_不import_executor(self):
        """P2 builder 不允许 import executor——与 schema 一致的纪律。"""
        src = pathlib.Path(builder.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod_name = (node.module or "")
                if node.level:
                    mod_name = f"{'.' * node.level}{mod_name}"
                if "trajectory_pipeline.executor" in mod_name:
                    offenders.append(mod_name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if "trajectory_pipeline.executor" in a.name:
                        offenders.append(a.name)
        assert not offenders, f"builder 不该 import executor: {offenders}"

    def test_NEGATIVE_SAMPLE_BRANCHES与executor对得上(self):
        """两份表必须含同样的真负样本分支——漂移是负样本池静默变少的源头。"""
        from trajectory_pipeline.perception.questions import all_branches
        from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES
        all_b = set(all_branches())
        non_sample = set(NON_SAMPLE_BRANCHES)
        ours = builder.NEGATIVE_SAMPLE_BRANCHES
        assert ours & non_sample == frozenset(), (
            f"builder 与 executor 口径打架: {ours & non_sample}")
        assert ours.issubset(all_b), (
            f"builder 列了不在 all_branches 里的分支: {ours - all_b}")
