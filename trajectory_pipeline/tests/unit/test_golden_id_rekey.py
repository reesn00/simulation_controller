"""池行候选 ``golden_id`` 的口径迁移（M-42 → v14）：**行号后缀 → 分支后缀**。

为什么要有这组测试
------------------
M-42 记的那个洞：``golden_id`` 里嵌的是**池文件里的行号**（``__neg{i:02d}``）
⇒ 池一增删，后面所有行的 id 一起前移/后移 ⇒ **人核标签（按 id 挂的）
静默掉线**。

⚠️ **掉线在报表上看不出来**：只是「D10 分母少了几条」，没人会知道为什么。
⇒ 门禁必须守在**代码层**（CLAUDE.md：代码层做护栏、LLM 层做探索），
不能靠「下次重建候选集时记得看」。

这组测试守三件事
----------------
1. **id 只依赖身份**——池增删不动它（核心回归测试）；
2. **换键有桥**——旧 id → 新 id 的映射表落盘，迁移期**双 id 并存**
   （历史冻结档不可改写 ⇒ 旧 id 仍在流通）；
3. **重建候选集不许让人核标签掉线**——掉线就**拒绝重建**，不静默丢。
"""

from __future__ import annotations

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation.golden import builder


# ── 工具 ────────────────────────────────────────────────────────


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: pathlib.Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = builder.main(argv)
    return rc, out.getvalue(), err.getvalue()


def _pool_row(
    *,
    task: str = "T001",
    run: str = "0123abcd",
    url: str = "https://movie.test/v/1",
    branch: str = "blocked",
    driver: str | None = None,
) -> dict:
    row = {
        "task_id": task,
        "run_id": run,
        "url": url,
        "branch": branch,
        "branch_label": "被拦",
        "evidence": "机器码原文",
        "question": "这里能看吗",
        "source": "executor",
        "fallback_used": False,
        "reached_play_page": False,
    }
    if driver is not None:
        row["driver"] = driver
    return row


def _pool_root(tmp_path: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    root = tmp_path / "pipeline"
    root.mkdir(parents=True, exist_ok=True)
    _write_jsonl(root / "negative.jsonl", rows)
    return root


def _ids(root: pathlib.Path) -> list[str]:
    """按现行口径扫池，返回 id 列表（顺序 = 池文件顺序）。"""
    return [r["golden_id"] for r in builder._scan_negative_pool(root, {})]


# ── 判据一：id 只依赖身份（M-42 的老洞） ─────────────────────────


class Test池行id只依赖身份:
    def test_id形态是分支后缀(self, tmp_path):
        root = _pool_root(tmp_path, [_pool_row(branch="blocked")])
        assert _ids(root) == [
            "T001__0123abcd__"
            + builder._url_hash("https://movie.test/v/1")
            + "__neg_blocked"
        ]

    def test_池里插一行其它行的id一个都不变(self, tmp_path):
        """⚠️ **核心回归测试**：M-42 那个洞就是「插一行 ⇒ 后面全体漂」。

        旧口径下这个测试必红：插在第 0 行 ⇒ 后面所有行的 ``__neg{i:02d}``
        全体 +1 ⇒ 人核标签静默掉线。
        """
        rows = [
            _pool_row(url=f"https://movie.test/v/{i}") for i in range(5)
        ]
        before = _ids(_pool_root(tmp_path, rows))

        inserted = [_pool_row(url="https://movie.test/v/NEW")] + rows
        after = _ids(_pool_root(tmp_path / "b", inserted))

        # ★ 关键断言：**原来的 5 个 id 一个都没变**
        assert set(before) <= set(after)
        assert len(after) == len(before) + 1

    def test_池里删一行其它行的id一个都不变(self, tmp_path):
        rows = [_pool_row(url=f"https://movie.test/v/{i}") for i in range(5)]
        before = _ids(_pool_root(tmp_path, rows))
        after = _ids(_pool_root(tmp_path / "b", rows[1:]))
        assert set(before) - set(after) == {before[0]}
        assert set(before) & set(after) == set(before) - {before[0]}

    def test_同一访问的不同分支是两个不同的id(self, tmp_path):
        """分支在同 ``(task, run, url)`` 内唯一 ⇒ id 唯一 ⇒ 挂不错行。"""
        root = _pool_root(
            tmp_path,
            [
                _pool_row(branch="blocked"),
                _pool_row(branch="broken"),
            ],
        )
        ids = _ids(root)
        assert len(ids) == 2
        assert len(set(ids)) == 2

    def test_分支缺失直接抛(self, tmp_path):
        """fail-closed：没有分支就没有身份 ⇒ 不猜后缀。"""
        root = _pool_root(tmp_path, [_pool_row(branch="")])
        with pytest.raises(builder.GoldenError):
            _ids(root)

    def test_脏分支名直接抛(self, tmp_path):
        """脏字符进键 = 两个不同的池行算出同一个 id。"""
        root = _pool_root(tmp_path, [_pool_row(branch="../../etc")])
        with pytest.raises(builder.GoldenError):
            _ids(root)


# ── 判据二：换键的桥（迁移期双 id 并存） ─────────────────────────


class Test迁移映射表:
    def test_没表就是没迁移(self, tmp_path):
        assert builder._load_alias_map(tmp_path) == {}

    def test_表坏了直接抛(self, tmp_path):
        """映射表坏了还当没有 ⇒ 旧 id 静默失联（正是要治的病）。"""
        (tmp_path / builder.ID_ALIAS_FILE).write_text(
            json.dumps({"map": ["不是对象"]}), encoding="utf-8"
        )
        with pytest.raises(builder.GoldenError):
            builder._load_alias_map(tmp_path)

    def test_解析旧id(self):
        alias = {"old": "new"}
        assert builder._resolve_gid("old", alias) == "new"

    def test_不在表里的id原样返回(self):
        """不猜、不按 url 反推——反推会把另一条访问当成同一条。"""
        assert builder._resolve_gid("别的", {"old": "new"}) == "别的"

    def test_表里有环不转(self):
        """环 = 表坏了，不是「转不动就用原 id」。"""
        assert builder._resolve_gid("a", {"a": "b", "b": "a"}) in {"a", "b"}

    def test_换键后旧id留在行里(self):
        """双 id 并存：旧引用仍能查到这行。"""
        rows, changed = builder._rekey_rows(
            [{"golden_id": "old", "golden": {"label": "blocked"}}],
            {"old": "new"},
        )
        assert changed == 1
        assert rows[0]["golden_id"] == "new"
        assert rows[0]["golden_id_legacy"] == "old"
        assert rows[0]["golden"]["label"] == "blocked"  # 行内容一个字没动


# ── 判据三：换键命令 + 重建门禁 ────────────────────────────────


def _golden_with_old_ids(
    tmp_path: pathlib.Path, *, pool: list[dict], annotations: list[dict]
) -> tuple[pathlib.Path, pathlib.Path]:
    """造一套「旧口径」的黄金集数据根 + 管线根。

    候选集用**旧口径**的 id（``__neg{i:02d}``），人核标注挂在旧 id 上
    ——正是 M-42 那个现场的形状。
    """
    golden_root = tmp_path / "golden"
    golden_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, r in enumerate(pool):
        rows.append(
            {
                "golden_id": (
                    f"{r['task_id']}__{r['run_id']}__"
                    f"{builder._url_hash(r['url'])}__neg{i:02d}"
                ),
                "source_kind": "negative_pool",
                "task_id": r["task_id"],
                "run_id": r["run_id"],
                "url": r["url"],
                "code": {"branch": r["branch"]},
                "golden": None,
            }
        )
    _write_jsonl(golden_root / builder.CANDIDATES_FILE, rows)
    _write_jsonl(golden_root / builder.ANNOTATIONS_FILE, annotations)
    return golden_root, _pool_root(tmp_path / "pipeline", pool)


class Test换键命令:
    def test_dry_run不写盘(self, tmp_path):
        pool = [_pool_row(branch="blocked")]
        ann = [
            {
                "golden_id": "T001__0123abcd__"
                + builder._url_hash("https://movie.test/v/1")
                + "__neg00",
                "round": 1,
                "label": "blocked",
                "label_label": "被拦",
                "reviewed_by": "reesn",
                "reviewed_at": "2026-10-28T00:00:00+00:00",
                "note": "",
                "access_surface": None,
                "observed_at": "",
                "dimension_scores": None,
                "interval_days": None,
            }
        ]
        golden_root, pipeline_root = _golden_with_old_ids(
            tmp_path, pool=pool, annotations=ann
        )
        rc, out, _err = _run(
            [
                "--golden-root",
                str(golden_root),
                "--root",
                str(pipeline_root),
                "rekey",
            ]
        )
        assert rc == 0
        assert "dry-run" in out
        assert not (golden_root / builder.ID_ALIAS_FILE).exists()
        # 标注还是旧键（没写盘）
        assert _read_jsonl(golden_root / builder.ANNOTATIONS_FILE)[0][
            "golden_id"
        ].endswith("__neg00")

    def test_apply写映射表并换标注键(self, tmp_path):
        pool = [_pool_row(branch="blocked")]
        old_gid = (
            "T001__0123abcd__"
            + builder._url_hash("https://movie.test/v/1")
            + "__neg00"
        )
        ann = [
            {
                "golden_id": old_gid,
                "round": 1,
                "label": "blocked",
                "label_label": "被拦",
                "reviewed_by": "reesn",
                "reviewed_at": "2026-10-28T00:00:00+00:00",
                "note": "人核备注",
                "access_surface": None,
                "observed_at": "",
                "dimension_scores": None,
                "interval_days": None,
            }
        ]
        golden_root, pipeline_root = _golden_with_old_ids(
            tmp_path, pool=pool, annotations=ann
        )
        rc, _out, _err = _run(
            [
                "--golden-root",
                str(golden_root),
                "--root",
                str(pipeline_root),
                "rekey",
                "--apply",
            ]
        )
        assert rc == 0
        alias_doc = json.loads(
            (golden_root / builder.ID_ALIAS_FILE).read_text(encoding="utf-8")
        )
        assert alias_doc["map"][old_gid].endswith("__neg_blocked")
        row = _read_jsonl(golden_root / builder.ANNOTATIONS_FILE)[0]
        assert row["golden_id"].endswith("__neg_blocked")
        # ★ **标签内容一个字没动**（改键 ≠ 改判）
        assert row["label"] == "blocked"
        assert row["reviewed_by"] == "reesn"
        assert row["note"] == "人核备注"

    def test_重建候选集时标注挂不上就拒绝(self, tmp_path):
        """⚠️ **门禁**：重建会让标注掉线 ⇒ 拒绝重建（不静默丢）。"""
        pool = [_pool_row(branch="blocked")]
        ann = [
            {
                "golden_id": "T001__0123abcd__deadbeef__neg99",  # 候选集里没有
                "round": 1,
                "label": "blocked",
            }
        ]
        golden_root, pipeline_root = _golden_with_old_ids(
            tmp_path, pool=pool, annotations=ann
        )
        rc, _out, err = _run(
            [
                "--golden-root",
                str(golden_root),
                "--root",
                str(pipeline_root),
                "candidates",
                "--force",
            ]
        )
        assert rc == 2
        assert "挂不上" in err

    def test_换键后重建候选集不再掉线(self, tmp_path):
        pool = [_pool_row(branch="blocked")]
        old_gid = (
            "T001__0123abcd__"
            + builder._url_hash("https://movie.test/v/1")
            + "__neg00"
        )
        ann = [{"golden_id": old_gid, "round": 1, "label": "blocked"}]
        golden_root, pipeline_root = _golden_with_old_ids(
            tmp_path, pool=pool, annotations=ann
        )
        rc, _out, _err = _run(
            [
                "--golden-root",
                str(golden_root),
                "--root",
                str(pipeline_root),
                "rekey",
                "--apply",
            ]
        )
        assert rc == 0
        rc, _out, err = _run(
            [
                "--golden-root",
                str(golden_root),
                "--root",
                str(pipeline_root),
                "candidates",
                "--force",
            ]
        )
        assert rc == 0, err
        ids = {
            r["golden_id"] for r in _read_jsonl(golden_root / builder.CANDIDATES_FILE)
        }
        assert old_gid.endswith("__neg00")
        assert not any(i.endswith("__neg00") for i in ids)
        assert any(i.endswith("__neg_blocked") for i in ids)


# ── 判据四：冻结时行自动换键（历史档不动，新版本换键） ──────────


class Test冻结时换键:
    def _v6(self, root: pathlib.Path, gid: str) -> pathlib.Path:
        vdir = root / "v6"
        _write_jsonl(
            vdir / "calibration.jsonl",
            [
                {
                    "golden_id": gid,
                    "source_kind": "negative_pool",
                    "code": {"branch": "blocked"},
                    "golden": {"label": "blocked", "note": ""},
                }
            ]
            + [
                {
                    "golden_id": f"c{i}",
                    "source_kind": "archive_outcome",
                    "code": {"branch": "success"},
                    "golden": {"label": "success", "note": ""},
                }
                for i in range(20)
            ],
        )
        _write_jsonl(
            vdir / "holdout.jsonl",
            [
                {
                    "golden_id": f"h{i}",
                    "source_kind": "archive_outcome",
                    "code": {"branch": "success"},
                    "golden": {"label": "success", "note": ""},
                }
                for i in range(10)
            ],
        )
        (vdir / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "1",
                    "label_schema_version": builder.LABEL_SCHEMA_VERSION,
                    "version": 6,
                    "holdout_runs": 0,
                    "seed": 42,
                    "counts": {"annotated": 31},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return vdir

    def test_新版本按映射表换键且记进manifest(self, tmp_path):
        golden_root = tmp_path / "golden"
        golden_root.mkdir(parents=True, exist_ok=True)
        old_gid = "T001__0123abcd__deadbeef__neg00"
        new_gid = "T001__0123abcd__deadbeef__neg_blocked"
        self._v6(golden_root, old_gid)
        (golden_root / builder.ID_ALIAS_FILE).write_text(
            json.dumps(
                {"schema_version": "1", "map": {old_gid: new_gid}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        rc, _out, err = _run(
            [
                "--golden-root",
                str(golden_root),
                "freeze",
                "--version",
                "7",
                "--from-version",
                "6",
            ]
        )
        assert rc == 0, err
        rows = _read_jsonl(golden_root / "v7" / "calibration.jsonl")
        row = next(r for r in rows if r.get("golden_id_legacy") == old_gid)
        assert row["golden_id"] == new_gid
        assert row["golden"]["label"] == "blocked"  # 金标一个字没动
        manifest = json.loads(
            (golden_root / "v7" / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["id_migration"]["enabled"] is True
        assert manifest["id_migration"]["rows_rekeyed"] == 1
        # ⚠️ **历史档不动**（方案 11 §二）
        old_rows = _read_jsonl(golden_root / "v6" / "calibration.jsonl")
        assert old_gid in {r["golden_id"] for r in old_rows}


# ── 判据五：换键后跨版本仍配得上（不然整对版本直接"测不成"） ──────


def _snap_row(gid: str, *, split: str = "calibration", legacy: str = "") -> dict:
    return {
        "golden_id": gid,
        "source_kind": "negative_pool",
        "task_id": gid.split("__")[0],
        "run_id": "r1",
        "url": "https://x.test/v/1",
        "__split__": split,
        "code": {"branch": "blocked", "evidence": "机器码原文"},
        "golden": {"label": "blocked", "note": "", "dimension_scores": {}},
        **({"golden_id_legacy": legacy} if legacy else {}),
    }


class Test换键后跨版本配对:
    def test_配对键优先取旧id(self):
        """迁移期双 id 并存 ⇒ 配对按**旧 id**（同一行在前后两版是两个键）。"""
        from trajectory_pipeline.evaluation.calibration import verify

        assert verify._entry_key({"golden_id": "new", "dimension": "d10"}) == (
            "new",
            "d10",
        )
        assert verify._entry_key(
            {"golden_id": "new", "golden_id_legacy": "old", "dimension": "d10"}
        ) == ("old", "d10")

    def test_快照条目带上旧id(self):
        """快照条目必须把旧 id 带出来（否则配对无从知道它是同一行）。"""
        from trajectory_pipeline.evaluation.calibration import snapshot

        snap = snapshot.build_snapshot(
            [_snap_row("new", legacy="old")],
            golden_version=14,
            success_label="success",
        )
        entry = next(e for e in snap["entries"] if e["dimension"] == "d10")
        assert entry["golden_id"] == "new"
        assert entry["golden_id_legacy"] == "old"

    def test_换键行在两版之间配得上(self):
        """基线用旧键、本轮用新键 ⇒ **配得上**（不是"这行消失了"）。"""
        from trajectory_pipeline.evaluation.calibration import snapshot, verify

        old = snapshot.build_snapshot(
            [_snap_row("old")], golden_version=13, success_label="success"
        )
        new = snapshot.build_snapshot(
            [_snap_row("new", legacy="old")],
            golden_version=14,
            success_label="success",
        )
        b_idx = verify.index_entries(old)
        c_idx = verify.index_entries(new)
        assert set(b_idx) & set(c_idx), "换键行配不上 ⇒ 被算成两行"

    def test_回归集里有换键行时不退2(self):
        """⚠️ **回归集里有一行换键 ⇒ 整对版本直接退 2（测不成）**。

        回归集键集不同 ⇒ ``verify`` 报「两轮回归集不是同一批行」⇒ 门禁
        永远比不了这一对 ⇒ 迁移必须让旧 id 参与配对。
        """
        from trajectory_pipeline.evaluation.calibration import snapshot, verify

        old = snapshot.build_snapshot(
            [_snap_row("old", split="regression")],
            golden_version=13,
            success_label="success",
        )
        new = snapshot.build_snapshot(
            [_snap_row("new", split="regression", legacy="old")],
            golden_version=14,
            success_label="success",
        )
        report = verify.analyze(old, new)  # 不抛 = 通过
        reg = report["regression"]["d10"]
        assert reg["only_baseline"] == 0
        assert reg["only_current"] == 0
        assert reg["paired"] == 1
