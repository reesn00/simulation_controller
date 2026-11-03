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

from trajectory_pipeline.assembler import builder, observation_view, schema


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
        # 交付面（方案 23 R3-3）：P2 按 tool_surface 分池落盘，夹具走交付面
        "driver": "camofox",
        "user_prompt": "有没有能看正片的", "search_url": "https://s.test",
        "search_observation": _obs("https://s.test"), "search_blocked": "",
        # 搜索阶段**一步**——真实存档实测「有搜索快照必有这一步」
        # （145 份里 138 份顶层恰好 1 步，其余 7 份连快照也没有）。
        "steps": [{
            "action": {"tool": "goto", "params": {"url": "https://s.test"},
                       "origin": "model"},
            "observation": _obs("https://s.test"),
            "error": "",
            "rationale": None,
        }],
        "provenance": {"genre": "喜剧"},
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
        assert files == ["T001__abc123.json"]

    def test_多访问点仍切出一条(self, tmp_path):
        """⚠️ **2026-10-30 · 方案 23**：一个任务一条轨迹——
        2 个访问点仍只落 **1 个文件**（旧口径落 3 个：search + site-1 + site-2）。"""
        src = _write(tmp_path, _arc(
            visits=[_visit("https://a.test/"), _visit("https://b.test/")],
            outcomes=[_outcome("https://a.test/", "no_play_control"),
                      _outcome("https://b.test/", None)],
        ))
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        files = sorted(p.name for p in out.glob("*.json"))
        assert files == ["T001__abc123.json"]
        # 站点级明细留在轨迹里（裁定 ③）
        sample = json.loads((out / "T001__abc123.json").read_text(encoding="utf-8"))
        assert len(sample["site_outcomes"]) == 2

    def test_写出JSON可读(self, tmp_path):
        src = _write(tmp_path, _arc())
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        sample_file = out / "T001__abc123.json"
        json.loads(sample_file.read_text(encoding="utf-8"))

    def test_写入UTF8可直读中文(self, tmp_path):
        src = _write(tmp_path, _arc(
            user_prompt="有没有能看正片的 给个链接呗",
            search_observation=_obs("https://s.test", body_text="功夫 正片 播放"),
        ))
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        sample = json.loads(
            (out / "T001__abc123.json").read_text(encoding="utf-8")
        )
        assert sample["six"]["user_prompt"] == "有没有能看正片的 给个链接呗"
        assert "功夫" in sample["six"]["observations"][0]["body_text"]


class Test原子写:
    def test_落盘不留_tmp(self, tmp_path):
        src = _write(tmp_path, _arc())
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        assert not list(out.glob("*.tmp"))
        assert not (out / "T001__abc123.json.tmp").exists()

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
            (out / "T001__abc123.json").read_text(encoding="utf-8")
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
            (out / "T001__abc123.json").read_text(encoding="utf-8")
        )
        obs = sample["six"]["observations"][0]
        assert obs["body_chars"] == observation_view.VIEW_BODY_CHARS
        assert obs["limits"]["body_chars"] == observation_view.VIEW_BODY_CHARS
        assert obs["limits"]["cropped"] is True


class Test负样本计数:
    def test_真负样本分支计入(self, tmp_path):
        src = _write(tmp_path, _arc(
            visits=[_visit("https://a.test/")],
            outcomes=[_outcome("https://a.test/", "no_play_control")],
        ))
        res = builder.build_from_archive(src, root=tmp_path, overwrite=True)
        assert res.negatives == 1

    def test_按末条结论判负样本(self, tmp_path):
        """⚠️ **2026-10-30 · 方案 23（裁定 ②）**：轨迹的标签看**最后一个
        结论**——前面撞过付费墙、最后停在 ``unresolved`` 的轨迹，
        **不算**负样本。

        站点级的那条负样本**仍在池子里**（裁定 ③：池保留站点级行作
        轨迹内细节），只是**训练侧**消费的是任务级标签。"""
        src = _write(tmp_path, _arc(
            visits=[_visit("https://a.test/"), _visit("https://b.test/")],
            outcomes=[_outcome("https://a.test/", "no_play_control"),
                      _outcome("https://b.test/", "unresolved")],
        ))
        res = builder.build_from_archive(src, root=tmp_path, overwrite=True)
        assert res.negatives == 0

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
        assert "T001__abc123" in res2.skipped[0]

    def test_显式overwrite才覆盖(self, tmp_path):
        src = _write(tmp_path, _arc())
        builder.build_from_archive(src, root=tmp_path, overwrite=True)
        out = builder.samples_dir(tmp_path)
        (out / "T001__abc123.json").write_text("坏", encoding="utf-8")
        res = builder.build_from_archive(src, root=tmp_path, overwrite=True)
        assert res.written == 1
        json.loads((out / "T001__abc123.json").read_text(encoding="utf-8"))


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
        # ⚠️ **2026-10-30 · 方案 23**：一个 Run 一条轨迹——这份存档有
        # 一个访问点，切出**一条**样本（以前是「搜索单元 + 站点单元」两条）。
        assert res.sample_count == 1
        assert res.written == 1
        assert res.negatives == 1
        # fixture 故意 _arc(steps=[])：这条轨迹缺 steps 走降级。
        # 真实批次里只要 P1 有 steps[]，degraded=0。
        assert res.degraded == 1

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
        """两份表必须含**同样的**真负样本分支——漂移是报表口径分叉的源头。

        ⚠️ executor 侧是**两段排除**（无题的兜底分支 + 采集侧账），
        P2 这份是「进池」的正表，所以它要与**两张表都**不相交
        （2026-10-26 · R1：``blocked`` 从真负样本表里拿掉）。

        ⚠️ **断言的是「集合相等」，不是「不相交 + 是子集」**
        （2026-10-28 修）。原来的断言**少一支不红**⇒ 实测本表漏了
        ``subscription_wall`` 等分支 ⇒ 同一批 **126 存档 / 1227 条样本**下
        旧窄表报 **155** 条、实际 **322** 条 ⇒ **少算 167 条**
        ⇒ 与 ``cli check-archives``（executor 侧判据）
        **两个报表对同一件事给出两个数**，读的人只能猜哪个对。

        ⚠️ ``login_wall_or_blocked`` 是**已废弃的合并态**（不在
        ``all_branches`` 里），但池里仍有 34 行 ⇒ 它**仍是真负样本**，
        显式列在这张表里；``all_branches`` 之外的分支只允许它一个。
        """
        from trajectory_pipeline.perception.questions import all_branches
        from trajectory_pipeline.executor.branches import (
            COLLECTION_SIDE_BRANCHES,
            NON_SAMPLE_BRANCHES,
        )
        all_b = set(all_branches())
        non_sample = set(NON_SAMPLE_BRANCHES)
        collection_side = set(COLLECTION_SIDE_BRANCHES)
        ours = builder.NEGATIVE_SAMPLE_BRANCHES
        assert ours & non_sample == frozenset(), (
            f"builder 与 executor 口径打架: {ours & non_sample}")
        assert ours & collection_side == frozenset(), (
            f"builder 把采集侧账当成负样本了: {ours & collection_side}")
        # ① 活跃分支：一张都不许少（少一支 = 报表少算，池却并了）
        active = all_b - non_sample - collection_side
        assert active <= ours, (
            f"builder 漏了真负样本分支: {sorted(active - ours)}")
        # ② 也不许多（多出来的那支必须显式登记：只有已废弃的合并态）
        assert ours - all_b <= {"login_wall_or_blocked"}, (
            f"builder 列了不在 all_branches 里的分支: {sorted(ours - all_b)}")


class Test分代筛选:
    """INC46 分代（2026-10-25）：**混批是隐性伤害**，所以构建期要报要能筛。

    两代文本同构、正文里没有时代印记，判据只有 unit 级 ``reflection`` 键
    在不在。这里钉三件事：默认产出不丢东西、逐条标记能看见、
    ``era_filter`` 能出同口径批次。
    """

    @staticmethod
    def _era_arc(*, new_era: bool, task_id: str = "T001"):
        arc = _arc(task_id=task_id, steps=[{
            "action": {"tool": "goto", "params": {"url": "https://a.test/"},
                       "origin": "model"},
            "observation": _obs("https://a.test/"),
            "error": "",
            "rationale": {"status": "written",
                          "rationale": {"text": "先看看这个站", "conclusion": ""}},
        }])
        if new_era:
            arc["reflection"] = None
        return arc

    def test_默认两代都产出并各自计数(self, tmp_path):
        """⚠️ 默认**不筛**：旧样本照样入库（它仍是真实证据），
        但每一份都带上自己的时代标记，报表上分得开。"""
        _write(tmp_path, self._era_arc(new_era=True, task_id="T001"),
               "T001__new.json")
        _write(tmp_path, self._era_arc(new_era=False, task_id="T002"),
               "T002__old.json")
        rep = builder.build_all(source_root=tmp_path / "src",
                                target_root=tmp_path, overwrite=True)
        assert rep.total_written == 2
        assert rep.total_pre_action == 1
        assert rep.total_post_hoc == 1
        assert rep.mixed_era is True
        assert rep.total_era_skipped == 0

    def test_同口径批次只收一代(self, tmp_path):
        _write(tmp_path, self._era_arc(new_era=True, task_id="T001"),
               "T001__new.json")
        _write(tmp_path, self._era_arc(new_era=False, task_id="T002"),
               "T002__old.json")
        rep = builder.build_all(source_root=tmp_path / "src",
                                target_root=tmp_path, overwrite=True,
                                era_filter=schema.RATIONALE_ERA_PRE_ACTION)
        assert rep.total_written == 1
        assert rep.total_era_skipped == 1
        # 筛掉的**逐条列出来**，不是静默消失
        skipped = [s for a in rep.archives for s in a.skipped]
        assert any("T002__old" in s for s in skipped)
        assert rep.mixed_era is True, "被筛掉的那一代仍然要报出来源存档里有"

    def test_时代写进P2文件(self, tmp_path):
        _write(tmp_path, self._era_arc(new_era=False, task_id="T002"),
               "T002__old.json")
        builder.build_all(source_root=tmp_path / "src",
                          target_root=tmp_path, overwrite=True)
        blob = json.loads(
            (builder.samples_dir(tmp_path) / "T002__old.json")
            .read_text(encoding="utf-8"))
        assert blob["rationale_era"] == "post_hoc"
        assert blob["gate"]["status"] == "not_run"
