"""采集侧账（方案 20 §R3）：跨面 + 采集侧分支 ⇒ **不出分歧卡、出账**。

为什么要把这两种东西分开
------------------------
方案 20 §2 实测：同一台机器、同一天、同一批站点，**人工浏览器 12 个进得去、
camoufox 8 个被拦**。于是「pred 说 blocked」可能根本不是在说站点——它在说
**我们这张面被挡在门外**。两者的下一步动作完全不同：

=============== =================================== ==========================
                这条读数在说什么                     下一步
=============== =================================== ==========================
分歧卡          站点（代码判错了 / rubric 该改）     分诊桌上改判据
采集侧账        我们（这张采集面进不去）            换面重采
=============== =================================== ==========================

把它当成分歧卡，处置栏会指向「改判据」，而真正该做的是换面——所以在报表上
这两个数**必须分得开**（02 避坑 §3.7 的同一条纪律：拦截率要分桶、要单列）。

守的是五件事：

1. **两个条件都要**：跨面（``surface_comparable is False``）**且** pred 落在
   ``COLLECTION_SIDE_BRANCHES``；缺一个照旧出卡；
2. **``None`` 不是 ``False``**：任一侧没记面 ⇒ 不可判定 ⇒ 照旧出卡。
   把「没记」洗成「不是模型的问题」，下一个读报表的人就再也看不到它；
3. **跨面上的站点结论仍是卡**：``no_play_control`` 这类说的还是站点，
   跟面无关，必须留在分诊桌上；
4. **账的形状**：``loop=collection`` / 桶 ``evidence_missing`` / 责任方
   ``collector`` 全部从 ``buckets`` 取（回路归属只有一份真源）；
5. **落盘分文件**：``collection_accounts.jsonl`` 与 ``disagreements.jsonl``
   是两个文件——混在一起就只能靠过滤找，拦截笔数就没人直接数得出来。
"""

import io
import json
import pathlib
from contextlib import redirect_stderr, redirect_stdout

import pytest

from trajectory_pipeline.evaluation import access_surface as A
from trajectory_pipeline.evaluation.calibration import disagreement, snapshot
from trajectory_pipeline.evaluation.calibration.buckets import (
    BUCKET_EVIDENCE_MISSING,
    loop_of,
    owner_of,
)


# ── 夹具 ──────────────────────────────────────────────────────


def _row(
    gid: str,
    *,
    gold_label: str,
    branch: str,
    gold_surface: str | None = A.SURFACE_HUMAN,
    pred_surface: str | None = A.SURFACE_CAMOUFOX,
    split: str = "calibration",
) -> dict:
    """一条冻结行。``gold_surface`` 走 ``golden.access_surface``（金标侧），
    ``pred_surface`` 走行顶层（预测侧）——两侧分居两处，见
    :func:`snapshot._row_surfaces`。"""
    row = {
        "golden_id": gid,
        "source_kind": "archive_outcome",
        "url": f"https://{gid}.test/watch",
        "site_url": f"https://{gid}.test/",
        "__split__": split,
        "code": {"branch": branch, "evidence": f"{gid} 的采集证据"},
        "golden": {"label": gold_label, "note": "人工备注", "dimension_scores": {}},
        "dimension_scores": {},
        "scores_detail": [],
    }
    if pred_surface is not None:
        row["access_surface"] = pred_surface
    if gold_surface is not None:
        row["golden"]["access_surface"] = gold_surface
    return row


def _snap(rows: list[dict], *, version: int = 13) -> dict:
    return snapshot.build_snapshot(
        rows,
        golden_version=version,
        success_label="success",
        rubric_version="1.2",
        evaluator_version="fp-test",
        tolerance=1,
    )


def _both(rows: list[dict], *, version: int = 13):
    return disagreement.cards_and_accounts_from_snapshot(_snap(rows, version=version))


# ── 1/2/3. 分流判据 ───────────────────────────────────────────


def test_跨面且采集侧分支出账不出卡():
    """`blocked` + 两面对不上 ⇒ 这是 B 面事实，进账。"""
    cards, accounts = _both([_row("g1", gold_label="not_play_site", branch="blocked")])
    assert cards == []
    assert [a["golden_id"] for a in accounts] == ["g1"]


def test_账上的面与值原样带出():
    """账要能回答「在哪张面、被判成什么」，所以两侧面与两侧值都得在。"""
    _, accounts = _both(
        [_row("g1", gold_label="not_play_site", branch="blocked")]
    )
    a = accounts[0]
    assert a["pred_access_surface"] == A.SURFACE_CAMOUFOX
    assert a["gold_access_surface"] == A.SURFACE_HUMAN
    assert a["pred_value"] == "blocked"
    assert a["gold_value"] == "not_play_site"
    assert a["surface_comparable"] is False


def test_没记面照旧出卡():
    """**``None`` 不是 ``False``。** 任一侧没记面 ⇒ 判不出跨面 ⇒ 这条差异
    仍要摆到人面前；当成采集侧账等于把「没记」洗成「不是模型的问题」。"""
    for kw in ({"gold_surface": None}, {"pred_surface": None}):
        cards, accounts = _both(
            [_row("g1", gold_label="not_play_site", branch="blocked", **kw)]
        )
        assert accounts == [], kw
        assert len(cards) == 1, kw
        assert cards[0]["surface_comparable"] is None


def test_跨面上的站点结论仍是卡():
    """同一张跨面读数上，pred 说 ``no_play_control`` 说的是**站点**
    （页面上没有播放控件），跟采集面无关 ⇒ 真分歧，留在分诊桌上。"""
    cards, accounts = _both(
        [_row("g1", gold_label="success", branch="no_play_control")]
    )
    assert accounts == []
    assert len(cards) == 1
    assert cards[0]["pred_value"] == "no_play_control"


def test_同面被拦仍是卡():
    """两面相同、pred 仍判 blocked ⇒ 人人在这张面上看到的就是这样，
    那才是「站点不给看」，是结论不是采集事故。"""
    cards, accounts = _both(
        [
            _row(
                "g1",
                gold_label="not_play_site",
                branch="blocked",
                gold_surface=A.SURFACE_HUMAN,
                pred_surface=A.SURFACE_HUMAN,
            )
        ]
    )
    assert accounts == []
    assert len(cards) == 1
    assert cards[0]["surface_comparable"] is True


# ── 4. 账的形状（回路归属从 buckets 取） ───────────────────────


def test_账的桶与回路归属来自buckets():
    """桶 / 责任方 / 回路**不许在账里手写一遍**——回路归属只有一份真源
    （``calibration/buckets.py``），抄一份就会漂。"""
    _, accounts = _both([_row("g1", gold_label="not_play_site", branch="blocked")])
    a = accounts[0]
    assert a["bucket"] == BUCKET_EVIDENCE_MISSING
    assert a["owner"] == owner_of(BUCKET_EVIDENCE_MISSING)
    assert a["loop"] == loop_of(BUCKET_EVIDENCE_MISSING) == "collection"
    assert a["kind"] == "collection_access_block"
    assert a["triage_by"] == disagreement.ACCOUNT_TRIAGE_BY
    assert a["status"] == "open"


def test_按行聚合一次访问():
    """账回答的是「这一行为什么没进分歧桌」，不是「这一维算错了」⇒
    一笔账上挂它涉及的维度。

    同一行里**只有硬维度带分支值**（``d10`` ← ``code.branch``），所以真正
    会进账的维度只有它；软维度的档差是「评分判错了」，那是分歧卡的事
    ——同一行上两种差异并存时各走各的，别把它们并成一笔。"""
    row = _row("g1", gold_label="not_play_site", branch="blocked")
    row["golden"]["dimension_scores"] = {"d5": 1}
    row["dimension_scores"] = {"d5": 5}  # 档差 4 > 容忍 1 ⇒ 一条 tier_gap 卡
    cards, accounts = _both([row])
    assert len(accounts) == 1
    assert [d["dimension"] for d in accounts[0]["dimensions"]] == ["d10"]
    assert [c["dimension"] for c in cards] == ["d5"]


def test_旁置档不出账():
    """回归集 / 持有集本来就不出卡（``SIDE_SPLITS``），账同理——旁置档的
    读数只报条数，处置动作不能发生在它上面。"""
    cards, accounts = _both(
        [
            _row(
                "g1",
                gold_label="not_play_site",
                branch="blocked",
                split="holdout",
            )
        ]
    )
    assert cards == [] and accounts == []


# ── 5. 落盘分文件 ─────────────────────────────────────────────


def test_账单独落一个文件(tmp_path):
    p = disagreement.write_accounts(tmp_path / "sub" / "collection_accounts.jsonl", [
        {"account_id": "v13:g1:collection", "golden_id": "g1"},
    ])
    assert p.exists()
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["account_id"] == "v13:g1:collection"


def _golden_root(tmp_path: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    """最小冻结档：``v6/calibration.jsonl`` + ``manifest.json``。"""
    from trajectory_pipeline.evaluation.golden import builder

    root = tmp_path / "golden"
    vdir = root / "v6"
    vdir.mkdir(parents=True)
    vdir.joinpath("calibration.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    vdir.joinpath("manifest.json").write_text(
        json.dumps({"schema_version": "1", "version": 6, "rubric_version": "1.2"},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def _agreement(tmp_path, rows: list[dict]) -> tuple[str, pathlib.Path]:
    from trajectory_pipeline.evaluation.golden import builder

    root = _golden_root(tmp_path, rows)
    out = tmp_path / "out"
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(io.StringIO()):
        rc = builder.main([
            "--golden-root", str(root), "agreement", "--version", "6",
            "--emit-disagreements", str(out),
        ])
    assert rc == 0
    return buf.getvalue(), out


def test_agreement把账与卡落成两个文件(tmp_path):
    """拦截笔数要能**直接数**——混进 ``disagreements.jsonl`` 就只能靠过滤找。"""
    out_text, out = _agreement(
        tmp_path, [_row("g1", gold_label="not_play_site", branch="blocked")]
    )
    accounts = [
        json.loads(x) for x in
        (out / "v6" / "collection_accounts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    cards = [
        json.loads(x) for x in
        (out / "v6" / "disagreements.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [a["golden_id"] for a in accounts] == ["g1"]
    assert cards == []
    assert "采集侧账" in out_text and "拦截笔数：1" in out_text


def test_账块排在一致率之前(tmp_path):
    """02 避坑 §3.7：拦截率是用来决定「换不换执行层」的数，排在一致率后面会
    被读成「代码判错了、要改判据」。方案 20 §R3 要求**排在分支分布之前**。"""
    out_text, _ = _agreement(
        tmp_path, [_row("g1", gold_label="not_play_site", branch="blocked")]
    )
    assert out_text.index("反爬拦截率") < out_text.index("D10 与黄金集标签一致率"), out_text
    assert out_text.index("反爬拦截率") < out_text.index("下钻口径 D"), out_text


def test_不可判定要说清是哪种0(tmp_path):
    """旧档两侧都没记面 ⇒ 拦截笔数 0，但那是「判不出跨面」而不是
    「没有拦截」——两个 0 必须能分开读。"""
    out_text, _ = _agreement(
        tmp_path,
        [_row("g1", gold_label="not_play_site", branch="blocked",
              gold_surface=None, pred_surface=None)],
    )
    assert "拦截笔数：0" in out_text
    assert "不可判定 1" in out_text
    assert "旧行不回填" in out_text


def test_cards_from_snapshot仍然只返回卡():
    """⚠️ 老入口的返回值**不含账**（签名不能变）。拿它当「全部差异」用的人
    会少掉拦截笔数——所以 docstring 里点了名，测试把它钉住。"""
    snap = _snap([_row("g1", gold_label="not_play_site", branch="blocked")])
    assert disagreement.cards_from_snapshot(snap) == []


# ── 采集面读数 ────────────────────────────────────────────────


def test_面读数按行计数且三态分开():
    """``none`` 大 = 这一版**判不出跨面**（旧行没记面，R2 不回填），
    不是「没有跨面问题」。两个结论在报表上长得一样，所以分三格。"""
    rows = [
        _row("g1", gold_label="success", branch="success"),  # human vs camoufox
        _row("g2", gold_label="success", branch="success",
             pred_surface=A.SURFACE_HUMAN),                 # 同面
        _row("g3", gold_label="success", branch="success", gold_surface=None),
    ]
    surfaces = snapshot.summarize_surfaces(_snap(rows))
    assert surfaces["rows"] == 3
    assert surfaces["comparable"] == {"true": 1, "false": 1, "none": 1}
    assert surfaces["pred_by_surface"] == {
        A.SURFACE_CAMOUFOX: 2, A.SURFACE_HUMAN: 1,
    }
    assert surfaces["gold_by_surface"] == {
        A.SURFACE_UNRECORDED: 1, A.SURFACE_HUMAN: 2,
    }


def test_面读数只数硬维度():
    """面是**行级**事实；按维度数会把同一行数 N 遍，报表上看起来像
    「3 行里有 9 个不可判定」。"""
    row = _row("g1", gold_label="success", branch="success")
    row["golden"]["dimension_scores"] = {"d2": 5, "d4": 5, "d6": 5}
    row["dimension_scores"] = {"d2": 5, "d4": 5, "d6": 5}
    surfaces = snapshot.summarize_surfaces(_snap([row]))
    assert surfaces["rows"] == 1


def test_旧行两侧都没记就是不可判定():
    """历史冻结档两侧都没这两栏 ⇒ ``(unrecorded, unrecorded, None)``：
    **不猜、不补**。这条是本轮报表上真实读数（v13 全 289 行如此）。"""
    row = _row("g1", gold_label="success", branch="success",
               gold_surface=None, pred_surface=None)
    entry = _snap([row])["entries"][0]
    assert entry["gold_access_surface"] == A.SURFACE_UNRECORDED
    assert entry["pred_access_surface"] == A.SURFACE_UNRECORDED
    assert entry["surface_comparable"] is None


def test_快照schema升版因为多了三栏():
    """加了字段必须升版：旧快照没有这三栏，跨 schema 比回归会把「没记」
    读成「变了」。"""
    assert snapshot.SNAPSHOT_SCHEMA_VERSION == "2"
    entry = _snap([_row("g1", gold_label="success", branch="success")])["entries"][0]
    assert {
        "gold_access_surface", "pred_access_surface", "surface_comparable",
    } <= set(entry)


# ── gatekeeper 账本补栏（方案 20 §R3 第 3 条）─────────────────


def _stamped(*, driver: str, events: list[dict]):
    from types import SimpleNamespace

    from trajectory_pipeline.executor.orchestrator import _stamped_gatekeeper_events

    return _stamped_gatekeeper_events(
        SimpleNamespace(driver=driver, gatekeeper_events=events)
    )


def test_gatekeeper事件补面与第几次():
    """「被拦几次、在哪个面上被拦」要能统计 ⇒ 被拦事件补
    ``blocked_surface`` / ``attempt``（同 verdict 内第几次）。"""
    stamped = _stamped(
        driver="camoufox",
        events=[
            {"verdict": "blocked_cf", "reason": "第一次"},
            {"verdict": "escalate", "reason": "升级"},
            {"verdict": "blocked_cf", "reason": "第二次"},
        ],
    )
    assert stamped[0]["blocked_surface"] == A.SURFACE_CAMOUFOX
    assert [e.get("attempt") for e in stamped] == [1, None, 2]
    # 升配档没有 verdict ⇒ 不是「一次拦截」，不盖章
    assert "blocked_surface" not in stamped[1]


def test_死循环不算被拦():
    """``dead_loop`` 有 verdict，但那是**行为失败**不是页面属性——盖上
    ``blocked_surface`` 会把「模型绕不出来」读成「站点在拦我们」。"""
    stamped = _stamped(
        driver="camoufox", events=[{"verdict": "dead_loop"}],
    )
    assert "blocked_surface" not in stamped[0]
    assert "attempt" not in stamped[0]


def test_登录墙与付费墙也算被拦但要分桶数():
    """四条页面侧访问控制都盖面（登录墙 / 付费墙也是「这次没进去」），
    但报表按 verdict 分桶——**不是**把它们并成一个「拦截率」（避坑 §3.7）。"""
    stamped = _stamped(
        driver="camoufox",
        events=[
            {"verdict": "blocked_cf"}, {"verdict": "login_wall"},
            {"verdict": "captcha"}, {"verdict": "paywall"},
        ],
    )
    assert [e.get("blocked_surface") for e in stamped] == [A.SURFACE_CAMOUFOX] * 4
    assert [e.get("attempt") for e in stamped] == [1, 1, 1, 1]


def test_认不出的驱动记unrecorded不猜面():
    """序列化边界上不抛（抛了就丢整份存档），但也**不猜是哪张面**。"""
    stamped = _stamped(driver="fake", events=[{"verdict": "blocked_cf"}])
    assert stamped[0]["blocked_surface"] == A.SURFACE_UNRECORDED


def test_attempt按verdict分开计():
    """不同 verdict 各数各的：把「CF 拦 2 次 + 验证码 1 次」合成「第 1..3 次」，
    读报表的人会以为是同一堵墙拦了三次——两者的处置不同（前者换引擎才有救，
    后者多半是间隔问题）。"""
    attempts = [
        e.get("attempt")
        for e in _stamped(
            driver="obscura",
            events=[
                {"verdict": "blocked_cf"}, {"verdict": "captcha"},
                {"verdict": "blocked_cf"},
            ],
        )
    ]
    assert attempts == [1, 1, 2]