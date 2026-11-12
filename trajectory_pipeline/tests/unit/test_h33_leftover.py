"""H-33 · 组装层不清理「本轮未产出」的陈旧派生产物。

盯六件：

1. **判据同源**——P2 与 T 对「切不出样本」用**同一条**判据
   （顶层 :mod:`trajectory_pipeline.sample_gate`）。2026-11-11 实测：
   T 侧原先只挡「判分不可信」，**漏了「搜索阶段没进决策」** ⇒ T 多渲染
   **53 份**退化空壳（P2 侧一份都不出）⇒ 交付目录里多出训练集里没有的
   东西，而**报告上零处异常**。
2. **写侧清点**——交付目录里「本轮名单之外」的产物被**移进隔离区**。
3. **隔离可逆**——搬回原位后读侧照常读得到（隔离 ≠ 删除）。
4. **旧面不受影响**——``legacy/`` 逐字节不变（旧面是只读存量）。
5. **门禁查得出**——``scout verify`` 的反向检查 + ``check-archives`` 的
   派生产物构成体检（原来的检查是**单向**的，查不出「多出来的」）。
6. **安全阀**——名单是子集时**不清点**（否则误伤别的批次），且**看得见**。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from trajectory_pipeline.assembler import builder, pool, task_view, views
from trajectory_pipeline.evaluation.scout import verify as scout_verify
from trajectory_pipeline.executor import integrity
from trajectory_pipeline.sample_gate import sample_blocked_reason
from trajectory_pipeline.storage.read_p3 import read_views


# ── 夹具 ──────────────────────────────────────────────────────────


def _obs(url="u", **kw):
    base = {"url": url, "page_title": "T", "body_text": "正文",
            "truncated": False, "degraded": [], "video_tag_count": 0,
            "iframe_count": 0, "interactive_elements": [], "links": []}
    base.update(kw)
    return base


def _archive(task_id="T001", run_id="a1", driver="camofox", **over):
    base = {
        "task_id": task_id, "run_id": run_id, "title": "功夫",
        "driver": driver,
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


def _write_p1(root: Path, *arcs):
    root.mkdir(parents=True, exist_ok=True)
    for arc in arcs:
        (root / f"{arc['task_id']}__{arc['run_id']}.json").write_text(
            json.dumps(arc, ensure_ascii=False), encoding="utf-8"
        )
    return root


def _p2(sample_id: str, surface: str = "camofox-1", **over) -> dict:
    """够分池读的最小 P2 JSON（直接摆进 samples/ 模拟存量）。"""
    base = {
        "sample_id": sample_id,
        "task_id": sample_id.split("__")[0],
        "title": "功夫",
        "source_archive": "T001__a1.json",
        "tool_surface": surface,
        "six": {
            "system_prompt": "系统提示词",
            "tools": [{"name": "goto", "params": ["url"], "instruction": "打开"}],
            "user_prompt": "有没有能看正片的",
            "provenance": {},
            "actions": [{"tool": "goto", "params": {"url": "https://x.test"},
                         "target": None, "origin": "model"}],
            "observations": [_obs()],
            "rationale": None,
        },
        "gate": {"status": "not_run", "reasons": []},
        "rationale_missing": True,
        "outcome": None,
        "degraded_from": [],
    }
    base.update(over)
    return base


def _write_p2(root: Path, sample_id: str, **kw) -> Path:
    d = root / "samples"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sample_id}.json"
    p.write_text(json.dumps(_p2(sample_id, **kw), ensure_ascii=False),
                 encoding="utf-8")
    return p


def _write_view(root: Path, sample_id: str) -> list[Path]:
    """三份视图——**``meta`` 那份带 ``tool_surface``**（真实渲染就是这样：
    三份里只有它带面，分池迁移靠它认组）。"""
    d = root / "views"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for view in ("messages", "openai", "meta"):
        payload = {"sample_id": sample_id, "view": view}
        if view == "meta":
            payload["tool_surface"] = "camofox-1"
        p = d / f"{sample_id}_{view}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        out.append(p)
    return out


def _write_t_view(root: Path, trajectory_id: str) -> Path:
    d = root / "task_views"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{trajectory_id}.json"
    p.write_text(json.dumps({
        "trajectory_id": trajectory_id, "task_id": "T001", "run_id": "a1",
        "messages": [], "openai": {"messages": []},
        "meta": {"tool_surface": "camofox-1"},
    }, ensure_ascii=False), encoding="utf-8")
    return p


# ── 1. 判据同源（P2 与 T 同一判据）────────────────────────────────


class Test判据同源:
    """T 侧与 P2 侧对「这份存档切不出样本」的判断必须一致。

    ⚠️ 分叉的代价不是报错，是**交付目录里多出训练集里没有的东西**——
    2026-11-11 实测 53 份退化空壳，报告上零处异常。
    """

    def test_搜索采集失败_两侧都不出(self, tmp_path):
        arc = _archive(warnings=["搜索页采集失败: TimeoutError: 读超时"])
        _write_p1(tmp_path / "src", arc)

        rep = builder.build_all(tmp_path / "src", target_root=tmp_path / "out")
        assert rep.total_written == 0, "P2 侧不出样本"

        res = task_view.build_task_view(
            arc, root=tmp_path / "out", source_archive="T001__a1.json")
        assert res.written == 0, "T 侧也不出训练视图"
        assert res.not_produced is True

    def test_反爬拦截_两侧都不出(self, tmp_path):
        arc = _archive(search_blocked="cf_challenge")
        _write_p1(tmp_path / "src", arc)

        rep = builder.build_all(tmp_path / "src", target_root=tmp_path / "out")
        assert rep.total_written == 0

        res = task_view.build_task_view(
            arc, root=tmp_path / "out", source_archive="T001__a1.json")
        assert res.written == 0
        assert res.not_produced is True

    def test_判分不可信_两侧都不出(self, tmp_path):
        """H-31 第 4 层：P2 不出样本 ⇒ T 不出训练视图。"""
        arc = _archive(judgement_trustworthy=False)
        _write_p1(tmp_path / "src", arc)

        rep = builder.build_all(tmp_path / "src", target_root=tmp_path / "out")
        assert rep.total_written == 0

        res = task_view.build_task_view(
            arc, root=tmp_path / "out", source_archive="T001__a1.json")
        assert res.written == 0
        assert res.not_produced is True

    def test_缺键不等于不可信(self, tmp_path):
        """历史存档没有 ``judgement_trustworthy`` ⇒ 缺键 = 不知道 ≠ 不可信。

        用「显式 False」判据就是防这个：按 falsy 判会把**所有历史档**丢掉。
        """
        assert sample_blocked_reason({}) is None
        assert sample_blocked_reason({"judgement_trustworthy": True}) is None
        assert sample_blocked_reason({"judgement_trustworthy": False}) is not None

    def test_正常档_两侧都出(self, tmp_path):
        arc = _archive()
        _write_p1(tmp_path / "src", arc)

        rep = builder.build_all(tmp_path / "src", target_root=tmp_path / "out")
        assert rep.total_written == 1

        res = task_view.build_task_view(
            arc, root=tmp_path / "out", source_archive="T001__a1.json")
        assert res.not_produced is False


# ── 2. 写侧清点（残留隔离）────────────────────────────────────────


class Test写侧清点:
    def test_名单之外的旧样本被移进隔离区(self, tmp_path):
        """剔除一批存档后重建 ⇒ 它们的旧产物**不该**留在交付目录里。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        stale = _write_p2(tmp_path / "out", "T009__zz9")

        rep = builder.build_all(tmp_path / "src", target_root=tmp_path / "out",
                                overwrite=True, prune_stale=True)

        assert "T009__zz9" in rep.quarantined
        assert not stale.exists(), "旧产物不该留在交付目录里"
        moved = pool.quarantine_dir(tmp_path / "out", "samples") / "T009__zz9.json"
        assert moved.exists(), "移进隔离区（不是删除）"
        assert json.loads(moved.read_text(encoding="utf-8"))["sample_id"] \
            == "T009__zz9"

    def test_名单之内的样本不动(self, tmp_path):
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        rep = builder.build_all(tmp_path / "src", target_root=tmp_path / "out",
                                overwrite=True, prune_stale=True)
        assert (tmp_path / "out" / "samples" / "T001__a1.json").exists()
        assert rep.quarantined == (), "名单之内的不动"

    def test_默认不清点(self, tmp_path):
        """``prune_stale`` 默认关——库调用方的目录不该被默认动。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        stale = _write_p2(tmp_path / "out", "T009__zz9")

        builder.build_all(tmp_path / "src", target_root=tmp_path / "out",
                          overwrite=True)
        assert stale.exists()

    def test_隔离可逆(self, tmp_path):
        """隔离 ≠ 删除：搬回原位后读侧照常读得到（P3 训练视图能读回来）。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        _write_p2(tmp_path / "out", "T009__zz9")
        _write_view(tmp_path / "out", "T009__zz9")

        builder.build_all(tmp_path / "src", target_root=tmp_path / "out",
                          overwrite=True, prune_stale=True)
        views.build_views_from_dir(root=tmp_path / "out", overwrite=True,
                                   prune_stale=True)

        q_samples = pool.quarantine_dir(tmp_path / "out", "samples")
        q_views = pool.quarantine_dir(tmp_path / "out", "views")
        assert (q_samples / "T009__zz9.json").exists()
        assert (q_views / "T009__zz9_meta.json").exists()

        # 搬回原位（P2 样本一份 + P3 三份成组）
        shutil.move(str(q_samples / "T009__zz9.json"),
                    str(tmp_path / "out" / "samples" / "T009__zz9.json"))
        for name in ("messages", "openai", "meta"):
            shutil.move(str(q_views / f"T009__zz9_{name}.json"),
                        str(tmp_path / "out" / "views" / f"T009__zz9_{name}.json"))

        assert read_views("T009__zz9", root=tmp_path / "out") is not None, (
            "搬回原位后读侧照常读得到——隔离必须可逆")

    def test_旧面目录逐字节不变(self, tmp_path):
        """旧面是**只读存量**（owner 裁定 2026-10-10）：清点不许碰它。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        legacy = tmp_path / "out" / "legacy" / "obscura-1" / "samples"
        legacy.mkdir(parents=True)
        old = legacy / "T002__b2.json"
        old.write_text(json.dumps(_p2("T002__b2", "obscura-1"),
                                  ensure_ascii=False), encoding="utf-8")
        before = old.read_bytes()

        builder.build_all(tmp_path / "src", target_root=tmp_path / "out",
                          overwrite=True, prune_stale=True)

        assert old.read_bytes() == before, "legacy/ 逐字节不变"
        assert len(list(legacy.glob("*.json"))) == 1

    def test_pool_meta记了隔离账(self, tmp_path):
        """隔离必须**留痕**——不然下一个读 ``pool_meta.json`` 的人
        只看到交付面少了几条，会读成「这批数据变少了」。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        _write_p2(tmp_path / "out", "T009__zz9")

        builder.build_all(tmp_path / "src", target_root=tmp_path / "out",
                          overwrite=True, prune_stale=True)

        meta = json.loads(
            (tmp_path / "out" / pool.POOL_META_FILENAME).read_text(encoding="utf-8")
        )
        assert meta["quarantine"]["samples"]["count"] == 1
        assert meta["quarantine"]["samples"]["ids"] == ["T009__zz9"]
        assert meta["quarantine"]["dir"] == "quarantine"

    def test_安全阀_名单是子集就不清点(self, tmp_path):
        """``archive_filter`` 只覆盖一部分时，「本轮名单」是子集 ⇒
        拿它清点会把**别的批次**的样本当残留移走 ⇒ 不清，且**看得见**。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1"), _archive("T002", "b2"))
        stale = _write_p2(tmp_path / "out", "T009__zz9")

        rep = builder.build_all(tmp_path / "src", target_root=tmp_path / "out",
                                overwrite=True, archive_filter=["T001__a1.json"],
                                prune_stale=True)

        assert stale.exists(), "子集名单不许清点"
        assert rep.quarantined == ()
        assert rep.prune_skipped, "没清点必须看得见"

    def test_读不出面的文件原样留着(self, tmp_path):
        """坏档不隔离——**不猜**它在哪个池，留在原地等人看。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        bad = tmp_path / "out" / "samples" / "T009__zz9.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{ 坏 JSON", encoding="utf-8")

        builder.build_all(tmp_path / "src", target_root=tmp_path / "out",
                          overwrite=True, prune_stale=True)

        assert bad.exists(), "读不出面的文件原样留着等人看"


def rep_quarantined(root: Path) -> tuple[str, ...]:
    """交付目录里剩下的样本 id（测试用的小读数）。"""
    d = root / "out" / "samples"
    return tuple(sorted(p.stem for p in d.glob("*.json")))


# ── 3. P3 镜像 P2 ────────────────────────────────────────────────


class TestP3镜像P2:
    def test_名单外的三份视图成组隔离(self, tmp_path):
        """P3 是 P2 的渲染 ⇒ 名单外的视图是陈旧产物。

        ⚠️ **成组**：三份视图是一组，只搬一份等于把一组拆散
        （同分池机制的 companion 规则）。
        """
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        written = _write_view(tmp_path / "out", "T009__zz9")

        rep = views.build_views_from_dir(root=tmp_path / "out", overwrite=True,
                                         prune_stale=True)

        assert "T009__zz9" in rep.quarantined
        q = pool.quarantine_dir(tmp_path / "out", "views")
        assert sorted(p.name for p in q.glob("T009__zz9_*.json")) == \
            sorted(p.name for p in written)
        assert not (tmp_path / "out" / "views" / "T009__zz9_meta.json").exists()

    def test_P2目录里的样本都有视图(self, tmp_path):
        """P2 有 ⇒ P3 有（镜像）。"""
        _write_p1(tmp_path / "src", _archive("T001", "a1"))
        builder.build_all(tmp_path / "src", target_root=tmp_path / "out",
                          overwrite=True)
        views.build_views_from_dir(root=tmp_path / "out", overwrite=True,
                                   prune_stale=True)
        assert (tmp_path / "out" / "views" / "T001__a1_meta.json").exists()


# ── 4. 门禁查得出「多出来的」 ────────────────────────────────────


class Test门禁反向体检:
    def test_scout_verify查出多出来的T视图(self, tmp_path):
        """原来的检查是**单向**的：只查「该有的有没有」，查不出「多出来的」。

        ⇒ 组装层只写不删留下的陈旧产物在报告上**完全隐形**。
        """
        scout_dir = tmp_path / "scout"
        scout_dir.mkdir()
        (scout_dir / "trajectory_candidates.jsonl").write_text(
            json.dumps({"trajectory_id": "T001__a1"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        (scout_dir / "trajectory_stats.json").write_text(
            json.dumps({"scored": 1, "undetermined": 0, "not_run": 0}),
            encoding="utf-8")
        _write_t_view(tmp_path / "out", "T001__a1")
        _write_t_view(tmp_path / "out", "T009__zz9")   # 多出来的

        report = scout_verify.verify_from_dir(
            scout_dir, task_views_root=tmp_path / "out")

        extras = [i for i in report.issues if i.code == "extra_t_view"]
        assert len(extras) == 1
        assert extras[0].trajectory_id == "T009__zz9"
        assert extras[0].severity == "warning", (
            "多出来的 T 视图不一定��脏数据（候选名单可能只覆盖一部分）"
            "⇒ 报出来让人判，不替人判")

    def test_check_archives报口径分叉(self, tmp_path):
        """存档体检**看不见**交付目录里的残留 ⇒ 补一条派生产物构成体检。"""
        _write_p2(tmp_path / "out", "T001__a1")
        _write_t_view(tmp_path / "out", "T001__a1")
        _write_t_view(tmp_path / "out", "T009__zz9")

        report = integrity.check_root(tmp_path / "out")

        assert any("T 视图比 P2 样本多" in n for n in report.notes), (
            f"口径分叉必须报出来；notes={report.notes}")

    def test_口径归一时不报(self, tmp_path):
        _write_p2(tmp_path / "out", "T001__a1")
        _write_view(tmp_path / "out", "T001__a1")
        _write_t_view(tmp_path / "out", "T001__a1")

        report = integrity.check_root(tmp_path / "out")

        assert not [n for n in report.notes if "口径分叉" in n]


# ── 5. 契约：判据只有一个真源 ────────────────────────────────────


class Test判据唯一真源:
    def test_sample_gate不import功能模块(self):
        """顶层地基不能挂功能模块（同 ``projection.py`` 的纪律）。"""
        import trajectory_pipeline.sample_gate as gate
        src = Path(gate.__file__).read_text(encoding="utf-8")
        for line in src.splitlines():
            line = line.strip()
            if line.startswith(("import ", "from ")) and "trajectory_pipeline" in line:
                pytest.fail(f"sample_gate.py 不许 import 功能模块：{line}")

    def test_task_view不import_schema(self):
        """T 不读 P2（契约 D14.3）——判据因此挂在顶层
        :mod:`trajectory_pipeline.sample_gate`，两边都不 import 对方。

        ⚠️ 用 **AST** 判（不是文本找）——docstring 里提到那个模块名是
        允许的（那是给人看的说明），import 才是契约。
        """
        import ast

        import trajectory_pipeline.assembler.task_view as tv
        tree = ast.parse(Path(tv.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert "trajectory_pipeline.assembler.schema" not in imported
        assert "trajectory_pipeline.sample_gate" in imported
