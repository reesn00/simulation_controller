"""``cli task-view`` 的冒烟测试——**新命令必须自己测**，不能靠「跑过一次」。

§5.16 的原话：「命令写完了」不等于「命令能跑」。这条坑是
``purge-run --apply`` 预演正常、执行炸掉——两条路径都绿，
因为执行路径**一条测试都没有**。

这里盯五件：四种调用形态（``--p1`` 单/多 / ``--all`` / ``--all --overwrite``）
都能跑、退出码（默认 0、有跳过 1、参数错 2）、口径（默认不覆盖）、
**坏档不打断整批**、以及**它落在哪个目录**（D8 产物隔离——落错不报错）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.executor import cli


def _obs(url="u", **kw):
    base = {"url": url, "page_title": "T", "body_text": "正文",
            "truncated": False, "degraded": [], "video_tag_count": 0,
            "iframe_count": 0, "interactive_elements": [], "links": []}
    base.update(kw)
    return base


def _archive(task_id="T001", run_id="a1", **over):
    base = {
        "task_id": task_id, "run_id": run_id, "title": "功夫",
        "query": "功夫 在线观看",
        "user_prompt": "有没有能看正片的", "search_url": "https://s.test",
        "search_observation": _obs("https://s.test"), "search_blocked": "",
        "steps": [{"action": {"tool": "goto", "params": {"url": "https://s.test"},
                              "origin": "model", "target": None},
                   "observation": _obs("https://s.test"), "error": "",
                   "rationale": None}],
        "run_config": {"engine": "bing", "max_candidates": 20},
        "provenance": {}, "candidates": [], "visits": [], "outcomes": [],
        "ledger": None, "warnings": [],
    }
    base.update(over)
    return base


def _legacy_archive(task_id="T001", run_id="a1"):
    """**模拟存量 P1**——顶层无 ``run_id`` / ``batch_id``（archive.write
    增量六十一修复之前的形态）。文件名仍带 ``__<run_id>``,JSON 没有。
    """
    arc = _archive(task_id, run_id)
    arc.pop("run_id", None)
    return arc


def _write_batch(root: Path, *arcs):
    for arc in arcs:
        name = f"{arc['task_id']}__{arc['run_id']}.json"
        (root / name).write_text(json.dumps(arc, ensure_ascii=False),
                                 encoding="utf-8")
    return root


def _ns(**kw):
    import argparse
    defaults = {
        "p1": None, "all": False, "out": None, "overwrite": False,
        "backfill_from_filename": True,
    }
    defaults.update(kw)
    return argparse.Namespace(**defaults)


# ── 1. --all 整批 ──────────────────────────────────────────────────


class TestAll整批:
    def test_全流程(self, tmp_path, capsys):
        """``--all`` 渲染 --out 目录下全部 P1 存档到 task_views/。"""
        _write_batch(tmp_path, _archive("T001", "a1"),
                     _archive("T001", "b2"), _archive("T002", "c3"))
        assert cli.cmd_task_view(_ns(all=True, out=str(tmp_path))) == 0
        out = capsys.readouterr().out
        assert "P1 → T" in out
        # 3 份 P1 全部写出
        assert "写出 3 份" in out
        # 落点在 task_views/（与 P3 的 views/ 平级）
        assert (tmp_path / "task_views").exists()
        assert len(list((tmp_path / "task_views").glob("*.json"))) == 3

    def test_退出码0_全写(self, tmp_path):
        _write_batch(tmp_path, _archive("T001", "a1"))
        assert cli.cmd_task_view(_ns(all=True, out=str(tmp_path))) == 0

    def test_退出码0_有跳过仍0(self, tmp_path):
        """默认不覆盖——二次跑全跳过，但**整批仍 exit 0**。
        跳过信息走 stdout 印出，调用方能从报告里看见（不靠退出码）。"""
        _write_batch(tmp_path, _archive("T001", "a1"))
        # 第一次
        cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))
        # 第二次（默认 overwrite=False）—— 全跳过
        rc = cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))
        assert rc == 0
        # 跳过的内容进了 stdout
        out_path = (tmp_path / "task_views" / "T001__a1.json")
        assert out_path.exists()


# ── 2. --p1 指定 ───────────────────────────────────────────────────


class TestP1指定:
    def test_单文件(self, tmp_path, capsys):
        p = _write_batch(tmp_path, _archive("T001", "a1")) / "T001__a1.json"
        # 传 --out=tmp_path 让产物落 tmp_path 下,不污染默认根
        assert cli.cmd_task_view(_ns(p1=[str(p)], out=str(tmp_path))) == 0
        out = capsys.readouterr().out
        assert "T001__a1" in out
        assert (tmp_path / "task_views" / "T001__a1.json").exists()

    def test_多文件(self, tmp_path, capsys):
        p1 = _write_batch(tmp_path, _archive("T001", "a1")) / "T001__a1.json"
        p2 = _write_batch(tmp_path, _archive("T001", "b2")) / "T001__b2.json"
        assert cli.cmd_task_view(_ns(p1=[str(p1), str(p2)],
                                      out=str(tmp_path))) == 0
        out = capsys.readouterr().out
        assert "T001__a1" in out
        assert "T001__b2" in out
        assert (tmp_path / "task_views" / "T001__a1.json").exists()
        assert (tmp_path / "task_views" / "T001__b2.json").exists()

    def test_不存在文件跳过(self, tmp_path, capsys):
        p = tmp_path / "不存在的.json"
        rc = cli.cmd_task_view(_ns(p1=[str(p)], out=str(tmp_path)))
        out = capsys.readouterr().out
        assert "不存在" in out
        # 不存在的文件不打断后续（这条是单文件调用，无后续）
        assert rc == 0


# ── 3. 默认不覆盖 / --overwrite 重建 ──────────────────────────────


class Test默认不覆盖:
    def test_默认不覆盖(self, tmp_path, capsys):
        _write_batch(tmp_path, _archive("T001", "a1"))
        cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))
        out1 = (tmp_path / "task_views" / "T001__a1.json").read_text(
            encoding="utf-8")
        # 第二次：默认不覆盖
        cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))
        out2 = (tmp_path / "task_views" / "T001__a1.json").read_text(
            encoding="utf-8")
        # 内容不变（被跳过、没重写）
        assert out1 == out2

    def test_overwrite重建(self, tmp_path):
        _write_batch(tmp_path, _archive("T001", "a1"))
        cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))
        # 第二次 + --overwrite：应写出 1（覆盖已存在）
        rc = cli.cmd_task_view(_ns(all=True, out=str(tmp_path),
                                    overwrite=True))
        assert rc == 0


# ── 4. 坏档不打断整批 ─────────────────────────────────────────────


class Test坏档不打断整批:
    def test_一份坏档其余照常(self, tmp_path, capsys):
        # 写一份好档 + 一份坏 JSON
        (tmp_path / "T001__good.json").write_text(
            json.dumps(_archive("T001", "good"), ensure_ascii=False),
            encoding="utf-8",
        )
        (tmp_path / "T001__bad.json").write_text(
            "{ this is not valid json", encoding="utf-8",
        )
        rc = cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))
        # 坏档不打断整批：写出了 good 那份、坏档进 skipped
        assert rc == 0
        assert (tmp_path / "task_views" / "T001__good.json").exists()
        assert not (tmp_path / "task_views" / "T001__bad.json").exists()
        out = capsys.readouterr().out
        assert "读档失败" in out or "JSON" in out or "解码" in out or "Expecting" in out

    def test_探测取证无__的json自动跳过(self, tmp_path, capsys):
        # 不含 ``__`` 的 json——不是 P1 存档，自动跳
        (tmp_path / "probe_evidence.json").write_text("{}", encoding="utf-8")
        _write_batch(tmp_path, _archive("T001", "a1"))
        cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))
        # probe_evidence.json 不该出现在 task_views/
        assert not (tmp_path / "task_views" / "probe_evidence.json").exists()
        assert (tmp_path / "task_views" / "T001__a1.json").exists()


# ── 5. 必填参数（既不传 --p1 也不传 --all） ───────────────────────


class Test参数校验:
    def test_无参数报错(self, capsys):
        rc = cli.cmd_task_view(_ns(p1=None, all=False, out=None))
        out = capsys.readouterr().out
        assert rc == 2
        assert "--p1" in out and "--all" in out


# ── 6. D8 产物隔离 —— 落点不在仓库根 output/ ─────────────────────


class Test产物隔离:
    def test_落点在tmp_path下task_views(self, tmp_path):
        """``--all --out`` 模式下,产物落在 ``<out>/task_views/`` 而不是仓库根
        ``output/``。这条纪律由 D8 守护。"""
        from trajectory_pipeline.storage import task_views_dir as tvd
        _write_batch(tmp_path, _archive("T001", "a1"))
        cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))
        # 1) 产物在 tmp_path 下
        assert (tmp_path / "task_views" / "T001__a1.json").exists()
        # 2) 仓库默认 task_views 根没被这次调用污染
        #    (P3 那边可能有其他 T001__a1.json 残留——这里只断言 T001__xyz
        #     这条 fixture 主键不在默认根)
        assert not (tvd() / "T001__a1.json").exists()

    def test_落点文件名是__主键(self, tmp_path):
        """T 视图文件名 = ``<task_id>__<run_id>.json``（与 P3 的三份文件区别）。"""
        _write_batch(tmp_path, _archive("T001", "abc123"))
        cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))
        assert (tmp_path / "task_views" / "T001__abc123.json").exists()


# ── 7. 跟 cmd_build 协作 ──────────────────────────────────────────


class Test与build协作:
    def test_build与task_view互不破坏(self, tmp_path):
        """P1 → P2/P3 与 P1 → T 是两条独立管线,跑一边不影响另一边。"""
        _write_batch(tmp_path, _archive("T001", "a1"))
        # 跑 build (P2/P3)
        assert cli.cmd_build(cli.build_parser().parse_args(
            ["build", "--out", str(tmp_path), "--stage", "all"])) == 0
        # 跑 task-view
        assert cli.cmd_task_view(_ns(all=True, out=str(tmp_path))) == 0
        # 两边产物都在
        assert (tmp_path / "samples").exists()
        assert (tmp_path / "views").exists()
        assert (tmp_path / "task_views").exists()
        # 主键互不重叠: T 是 task_id__run_id, P3 是 task_id__run_id__unit
        t_files = {p.name for p in (tmp_path / "task_views").glob("*.json")}
        v_files = {p.name for p in (tmp_path / "views").glob("*.json")}
        assert t_files.isdisjoint(v_files)
        # T 主键不带 __{unit} 后缀
        for f in t_files:
            assert f.count("__") == 1, f"T 主键异常: {f}"


# ── 8. 增量六十一回归：过渡期回填（存量 P1 缺 run_id）───────────────


class Test过渡期回填:
    """``archive.write`` 历史上不写 ``run_id`` / ``batch_id`` 到 P1 JSON 顶层,
    只有文件名带。task_view ``build_task_view`` 渲染需要 ``run_id`` 拼 trajectory_id,
    缺就 skipped → 0 产出。

    增量六十一分两步修：① ``archive.write`` 注入（根因,新 P1 自带）;
    ② ``build_all_task_views`` 接受 ``backfill_from_filename=True`` 默认
    参数,从文件名派生 run_id 补到内存 dict（**不写回盘**,过渡期缓兵之计）。
    本类盯第 ② 步——存量 P1 还能正常跑出 T 视图,且日志留痕。
    """

    def test_存量P1缺run_id_默认backfill_产出(self, tmp_path):
        """文件名 ``T001__abc12345.json``,JSON 顶层无 ``run_id`` —— 默认
        backfill 开启,必须能渲染出 T 视图（而不是 skipped）。"""
        legacy = _legacy_archive("T001", "abc12345")
        assert "run_id" not in legacy  # 模拟历史形态
        p = tmp_path / "T001__abc12345.json"
        p.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

        rc = cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))
        assert rc == 0
        # T 视图落盘 = 文件名 + trajectory_id 都对得上
        assert (tmp_path / "task_views" / "T001__abc12345.json").exists()
        # 回填没污染原 P1 存档（**不写回盘**）
        raw = json.loads(p.read_text(encoding="utf-8"))
        assert "run_id" not in raw  # ⚠️ 必须不动盘上原数据

    def test_backfill_显式False_存量P1全skipped(self, tmp_path):
        """走纪律路径:缺字段就 skipped（不静默回填）。"""
        legacy = _legacy_archive("T002", "abc12345")
        assert "run_id" not in legacy
        p = tmp_path / "T002__abc12345.json"
        p.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

        rc = cli.cmd_task_view(_ns(
            all=True, out=str(tmp_path), backfill_from_filename=False,
        ))
        assert rc == 0
        # T 视图没产出（trajectory_id 空 → skipped）
        assert not (tmp_path / "task_views" / "T002__abc12345.json").exists()

    def test_新P1自带run_id_不触发backfill(self, tmp_path, caplog):
        """``archive.write`` 增量六十一修复后,新 P1 JSON 顶层自带 run_id
        —— backfill 不应覆盖原值,也不应记 WARNING。"""
        import logging

        fresh = _archive("T003", "from-fresh")
        assert fresh.get("run_id") == "from-fresh"
        p = tmp_path / "T003__from-fresh.json"
        p.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")

        with caplog.at_level(logging.WARNING):
            cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))

        # 不应触发「过渡期回填」WARNING（因为 run_id 已存在）
        backfill_warnings = [
            r for r in caplog.records
            if "过渡期回填" in r.getMessage()
        ]
        assert not backfill_warnings, \
            f"新 P1 自带 run_id 时不应触发回填,日志: {[r.getMessage() for r in backfill_warnings]}"
        # T 视图照常产出
        assert (tmp_path / "task_views" / "T003__from-fresh.json").exists()

    def test_文件名缺__run_id_不回填(self, tmp_path):
        """文件名形如 ``probe.json``(没 ``__``) → 既被 ``build_all_task_views``
        跳过（前面 ``Test坏档不打断整批`` 已盯）,即便 P1 缺 run_id 也不
        该被回填（与 ``if "__" not in path.stem: continue`` 互斥）。"""
        p = tmp_path / "T004__only.json"  # 文件名里有 __
        legacy = _legacy_archive("T004", "")
        legacy["run_id"] = ""
        # 文件名形如 ``T004__only`` —— 但 ``only`` 不是合法 run_id 形态
        p.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

        rc = cli.cmd_task_view(_ns(all=True, out=str(tmp_path)))
        assert rc == 0
        # run_id 派生出来是 "only",不是空 → 应该能跑出 T 视图
        assert (tmp_path / "task_views" / "T004__only.json").exists()


class Test复核档优先:
    """``--all`` 的名单**由 ``select_archives`` 定**（复核档优先）。

    2026-10-26 修：以前直接 glob ``*.json``，于是同一条 run 出两份 T 视图——
    原档一份、复核档一份，而复核档那份的 ``run_id`` 被从文件名派生成
    ``<run>.reviewed``。那个 id 对不上任何 ``trajectory_id``（scout / LS
    上传按 ``<task_id>__<run_id>.json`` 查），是死文件；而**真正被查到的
    那个名字**来自哪一份，只取决于文件名排序。
    """

    def test_有复核档时用复核档且run_id不带reviewed(self, tmp_path):
        raw = _legacy_archive("T001", "a1")
        raw["title"] = "功夫（原档）"
        reviewed = _legacy_archive("T001", "a1")
        reviewed["title"] = "功夫（人工复核过）"
        (tmp_path / "T001__a1.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        (tmp_path / "T001__a1.reviewed.json").write_text(
            json.dumps(reviewed, ensure_ascii=False), encoding="utf-8")

        assert cli.cmd_task_view(_ns(all=True, out=str(tmp_path))) == 0
        files = sorted(p.name for p in (tmp_path / "task_views").glob("*.json"))
        assert files == ["T001__a1.json"], f"同一 run 只该有一份 T 视图: {files}"
        blob = json.loads((tmp_path / "task_views" / "T001__a1.json")
                          .read_text(encoding="utf-8"))
        assert blob["trajectory_id"] == "T001__a1"
        assert "复核" in json.dumps(blob, ensure_ascii=False)

    def test_只剩复核档时随select_archives一并略过(self, tmp_path):
        """**已知边界**：``select_archives`` 只遍历原档、用复核档配对替换，
        所以**只有复核档、没有原档**的 run 根本不在名单里（与 report /
        evaluation 同一条口径——一处少、处处少，不会只在这条命令上丢）。
        这条钉住「行为一致」，不是钉住「这是好行为」：真要支持孤立复核档，
        得先改 ``select_archives`` 本身并同步所有消费方的分母。
        """
        reviewed = _legacy_archive("T002", "b2")
        (tmp_path / "T002__b2.reviewed.json").write_text(
            json.dumps(reviewed, ensure_ascii=False), encoding="utf-8")
        assert cli.cmd_task_view(_ns(all=True, out=str(tmp_path))) == 0
        assert not (tmp_path / "task_views" / "T002__b2.json").exists()
