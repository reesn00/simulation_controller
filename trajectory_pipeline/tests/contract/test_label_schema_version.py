"""标签宇宙版本 ``label_schema_version`` 的守门（2026-10-26 立项）。

病
----
``label_schema_version`` 写进每份冻结档的 manifest / 索引，是
「跨版本对比 D10 前先核对」这条纪律的**唯一凭据**。但它**只写不读**
——代码从不校验——而标注写的是「2 = 15 类」。注册表后来长到 22 类，
标记还写 15。**同一个号指两种宇宙**，于是那条纪律恒为真、等于没信号
（实测：v1–v13 **全部**写 "2"，全靠人工看才知道注册表已经变了）。

治
----
1. 版本号 = **冻结时注册表的合法 label 全集**，每个号在
   :data:`builder.LABEL_UNIVERSES` 里**冻结一份**副本（手抄的快照——
   从注册表现算的话，号不变、集合变，等于没治）；
2. :data:`builder.LABEL_SCHEMA_VERSION` 升 ``"3"``（22 类）；历史
   v1–v13 **一个字节都不动**（冻结档不可变——方案 11 §二：改 label
   走新版本，不改历史）；
3. 冻结前**代码核对**这批标签是否落在当前宇宙内——人工纪律变代码保证；
4. ``agreement`` 输出的该字段**取自 manifest**，不拿当前值顶替
   （与同文件里 ``rubric_version`` 同款纪律）。

7 条检查
--------
    1. ``test_注册表与当前宇宙一致`` —— 加分支不改版本号即红（漂移守卫）；
    2. ``test_当前版本号是最新登记的宇宙``；
    3. ``test_宇宙单调递增``；
    4. ``test_冻结拒绝越界标签``；
    5. ``test_存量冻结档标签不越界`` —— 逐档核 v1–v13；
    6. ``test_agreement取自manifest而非当前常量``；
    7. ``test_老冻结档缺该字段时输出空串``。
"""

from __future__ import annotations

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation.golden import builder
from trajectory_pipeline.executor.branches import BRANCH_LABELS


# ── 1–3. 宇宙本身的性质 ────────────────────────────────────────


def test_注册表与当前宇宙一致():
    """**漂移守卫**：加分支忘了新起版本号 ⇒ 这里当场红。

    这是整套治法的支点。改注册表的人不会去记得改版本号——但他一定会
    撞上这个测试。以前没有这个测试，于是注册表从 15 类长到 22 类、
    标记还写 "2"，一路走到 v13 都没人看见。
    """
    live = set(BRANCH_LABELS) | {builder.SUCCESS_LABEL}
    assert live == set(builder.LABEL_UNIVERSES[builder.LABEL_SCHEMA_VERSION]), (
        f"注册表现在 {len(live)} 类，标签体系版本 {builder.LABEL_SCHEMA_VERSION!r} "
        f"登记的是 {len(builder.LABEL_UNIVERSES[builder.LABEL_SCHEMA_VERSION])} 类——"
        "加分支要三步：改注册表 → 新起版本号 → 在 LABEL_UNIVERSES 加一条冻结副本"
    )


def test_当前版本号是最新登记的宇宙():
    assert builder.LABEL_SCHEMA_VERSION == max(
        builder.LABEL_UNIVERSES, key=int
    ), "当前版本号不是最新登记的那个——新起版本号时把它设成了别的"


def test_宇宙单调递增():
    """旧宇宙是当前宇宙的子集 ⇒ 继承式重冻结不会因为「上一版就这么标的」而失败。"""
    keys = sorted(builder.LABEL_UNIVERSES, key=int)
    for older, newer in zip(keys, keys[1:]):
        assert set(builder.LABEL_UNIVERSES[older]) <= set(
            builder.LABEL_UNIVERSES[newer]
        ), f"宇宙 {older} 不是 {newer} 的子集"


# ── 4. 冻结前代码核对 ──────────────────────────────────────────


def _candidate(golden_id: str) -> dict:
    return {
        "golden_id": golden_id,
        "source_kind": "archive_outcome",
        "task_id": "T001",
        "run_id": "abc12345",
        "url": f"https://{golden_id}.test/",
        "site_url": f"https://{golden_id}.test/",
        "play_page_url": "",
        "code": {"branch": None, "branch_label": "success"},
        "persona": None,
        "golden": None,
        "retest": None,
    }


def _write_jsonl(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _golden_root(tmp_path: pathlib.Path, labels: list[str]) -> pathlib.Path:
    root = tmp_path / "golden"
    _write_jsonl(
        root / builder.CANDIDATES_FILE,
        [_candidate(f"g{i}") for i in range(len(labels))],
    )
    _write_jsonl(
        root / builder.ANNOTATIONS_FILE,
        [
            {
                "golden_id": f"g{i}",
                "round": 1,
                "label": lab,
                "label_label": lab,
                "reviewed_by": "manual-review",
                "reviewed_at": "2026-10-22T00:00:00+00:00",
                "note": "",
            }
            for i, lab in enumerate(labels)
        ],
    )
    return root


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = builder.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_冻结拒绝越界标签(tmp_path):
    """标签不在当前宇宙内 ⇒ 冻结当场失败，不落盘。

    这条把「跨版本对比前先核对」从**人工纪律**变成**代码保证**：
    越界的数据进不了冻结档，于是 manifest 里的版本号**说得清**。
    """
    root = _golden_root(tmp_path, ["success", "success", "brand_new_branch"])
    rc, out, err = _run(
        ["--golden-root", str(root), "freeze", "--version", "9", "--force"]
    )
    assert rc != 0
    assert "brand_new_branch" in (out + err)
    assert not (root / "v9").exists(), "越界数据不许落盘"


def test_冻结正常档写入当前版本号(tmp_path):
    root = _golden_root(tmp_path, ["success", "success", "broken"])
    rc, out, err = _run(
        ["--golden-root", str(root), "freeze", "--version", "9", "--force"]
    )
    assert rc == 0, out + err
    manifest = json.loads(
        (root / "v9" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["label_schema_version"] == builder.LABEL_SCHEMA_VERSION
    index = [
        json.loads(line)
        for line in (root / builder.VERSIONS_INDEX)
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert index[-1]["label_schema_version"] == builder.LABEL_SCHEMA_VERSION


# ── 5. 存量冻结档逐档核 ────────────────────────────────────────


@pytest.mark.parametrize("root_name", ["data", "data_scout"])
def test_存量冻结档标签不越界(root_name):
    """逐档核：每份冻结档实际用到的标签 ⊆ 它声明的宇宙。

    v1 的 manifest **没有**这个字段（那时的口径是隐式的）——按 ``"1"``
    核。v2 起有字段，按它自己的值核。

    这条测试是本项目**唯一**能证明「历史数据的标签没有越界」的地方：
    漂移发生在标记与注册表之间，而数据本身一直是干净的。
    """
    root = builder.GOLDEN_ROOT.parent / root_name
    if not root.is_dir():
        pytest.skip(f"{root_name} 不在盘上")
    versions_path = root / builder.VERSIONS_INDEX
    if not versions_path.exists():
        pytest.skip(f"{root_name} 没有版本索引")
    metas = [
        json.loads(line)
        for line in versions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert metas, f"{root_name}/versions.jsonl 是空的"
    for meta in metas:
        version = int(meta["version"])
        vdir = root / f"v{version}"
        if not vdir.is_dir():
            continue
        manifest_path = vdir / "manifest.json"
        declared = ""
        if manifest_path.exists():
            declared = str(
                json.loads(manifest_path.read_text(encoding="utf-8")).get(
                    "label_schema_version"
                )
                or ""
            )
        # v1 没有这个字段 ⇒ 口径是隐式的，按 "1" 核
        key = declared or "1"
        assert key in builder.LABEL_UNIVERSES, (
            f"{root_name} v{version} 声明的标签体系版本 {key!r} 没在 "
            "LABEL_UNIVERSES 里登记——版本号不能凭空出现"
        )
        universe = builder.LABEL_UNIVERSES[key]
        used: set[str] = set()
        for split in ("calibration", "holdout", "regression"):
            p = vdir / f"{split}.jsonl"
            if not p.exists():
                continue
            for line in p.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                used |= {
                    lab
                    for lab in (builder._label_of(json.loads(line)),)
                    if lab
                }
        out = sorted(used - universe)
        assert not out, (
            f"{root_name} v{version} 的标签 {out} 越出它声明的宇宙 "
            f"{key!r}（{len(universe)} 类）"
        )


# ── 6–7. agreement 输出的取值口径 ─────────────────────────────


def _agreement_vdir(
    tmp_path: pathlib.Path, *, declared: str | None
) -> pathlib.Path:
    root = tmp_path / "golden"
    vdir = root / "v7"
    row = {
        "golden_id": "g0",
        "source_kind": "archive_outcome",
        "task_id": "T001",
        "run_id": "abc12345",
        "url": "https://g0.test/",
        "site_url": "https://g0.test/",
        "play_page_url": "",
        "code": {
            "branch": None,
            "branch_label": "success",
            "evidence": "e",
            "question": "player.ok",
            "source": "rule",
            "fallback_used": False,
            "reached_play_page": True,
        },
        "persona": None,
        "golden": {"label": "success", "label_label": "success"},
        "retest": None,
    }
    _write_jsonl(vdir / "calibration.jsonl", [row])
    _write_jsonl(vdir / "holdout.jsonl", [])
    manifest = {
        "schema_version": "1",
        "version": 7,
        "frozen_at": "2026-10-12T00:00:00+00:00",
        "seed": 42,
        "counts": {"calibration": 1, "holdout": 0},
    }
    if declared is not None:
        manifest["label_schema_version"] = declared
    (vdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return root


def _agreement_json(root: pathlib.Path, tmp_path: pathlib.Path) -> dict:
    out_path = tmp_path / "agree.json"
    rc, out, err = _run(
        [
            "--golden-root", str(root),
            "agreement", "--version", "7",
            "--json-out", str(out_path),
        ]
    )
    assert rc == 0, out + err
    return json.loads(out_path.read_text(encoding="utf-8"))


def test_agreement取自manifest而非当前常量(tmp_path):
    """老档的 agreement 必须报**它自己那档**的标签体系版本。

    改口径前这里用的是当前常量——「v6 的 agreement 报 22 类」这种话
    一直在发生，只因为当时常量恰好也是旧值。改成读 manifest 后，
    顶替就不可能了。
    """
    root = _agreement_vdir(tmp_path, declared="2")
    data = _agreement_json(root, tmp_path)
    assert data["label_schema_version"] == "2"
    assert data["label_schema_version"] != builder.LABEL_SCHEMA_VERSION, (
        "这个档声明的是 '2'，当前常量是 "
        f"{builder.LABEL_SCHEMA_VERSION!r}——两者不等才对，"
        "相等说明还在拿当前值顶替"
    )


def test_老冻结档缺该字段时输出空串(tmp_path):
    """缺字段给空串，**不拿当前值顶替**——与 ``rubric_version`` 同款纪律。

    v1 的 manifest 就没有这个字段。给它空串是诚实的；给它当前常量
    等于把新尺子的编号贴在旧数字旁边。
    """
    root = _agreement_vdir(tmp_path, declared=None)
    data = _agreement_json(root, tmp_path)
    assert data["label_schema_version"] == ""
