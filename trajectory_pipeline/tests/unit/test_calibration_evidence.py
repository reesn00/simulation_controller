"""``worklist show``（读现场那一半）的守门测试。

设计来源：``docs/设计方案/16-评估器与rubric双向校准回路方案.md`` §9.3 / C-06。

守的是五件事：

1. **条款块不串味**：D-10 那块不能把「## 批次级断言表」的引言吞进去——
   人判尺子时读到半段别人的话，比读不到更糟；
2. **条款原文只有一个真源**：从 rubric 文档现读，**不抄一份到代码里**；
3. **两种观察形状都读得出**：P1 存档用 ``body_len`` / ``elements_total``，
   P2 样本用 ``body_chars`` / ``interactive_elements``——只认一种就会把
   另一种读成空值，而空值会被读成「页面里没有 video 标签」；
4. **读不到就说读不到**：定位不到现场时给 note + 候选清单，**不返回空读数**
   （把「没找到」当成「看到了没有」是这类读数最贵的错）；
5. **只读**：``show`` 不写任何东西，也不碰分诊档。

真实读数（v7，33 张卡）在最后一节。
"""

import json

import pytest

from trajectory_pipeline.evaluation.calibration import evidence, worklist


# ── 夹具：一份最小的 P1 存档 ───────────────────────────────


def _obs(url: str = "https://x.test/a", *, video: int = 0, **over) -> dict:
    obs = {
        "url": url,
        "page_title": "测试页",
        "body_text": "正文内容",
        "body_len": 4,
        "body_source": "inner_text",
        "truncated": {"source": True, "view": True},
        "raw_len": 4,
        "stripped_ratio": 1.0,
        "video_tag_count": video,
        "iframe_count": 0,
        "elements_total": 200,
        "interactive_elements": [{"tag": "a"}, {"tag": "a"}],
        "links": [],
        "links_total": 10,
        "degraded": [],
    }
    obs.update(over)
    return obs


def _archive(
    tmp_path, *, task_id: str = "T001", run_id: str = "r1",
    url: str = "https://x.test/a", video: int = 0, branch: str = "not_play_site",
):
    """写一份能被 ``split_archive`` 切开的最小存档，返回路径。"""
    obs = _obs(url=url, video=video)
    archive = {
        "schema_version": "2",
        "task_id": task_id,
        "title": "测试任务",
        "user_prompt": "找片",
        "run_config": {"engine": "e", "max_chars": 2000},
        "steps": [],
        "candidates": [],
        "visits": [
            {
                "url": url,
                "landed_url": url,
                "success": False,
                "site_obs": obs,
                "player_obs": obs,
                "steps": [
                    {
                        "action": {
                            "tool": "goto",
                            "params": {"url": url},
                            "target": None,
                            "origin": "model",
                        },
                        "observation": obs,
                    }
                ],
            }
        ],
        "outcomes": [
            {
                "url": url,
                "branch": branch,
                "source": "llm",
                "fallback_used": False,
                "question": "player.ok",
                "reached_play_page": False,
                "site_url": url,
                "play_page_url": "",
            }
        ],
    }
    path = tmp_path / f"{task_id}__{run_id}.json"
    path.write_text(json.dumps(archive, ensure_ascii=False), encoding="utf-8")
    return path


def _row(**over) -> dict:
    row = {
        "golden_id": "T001__r1__h",
        "task_id": "T001",
        "run_id": "r1",
        "url": "https://x.test/a",
        "site_url": "https://x.test/a",
        "play_page_url": "",
        "source_kind": "archive_outcome",
        "code": {
            "branch": "not_play_site",
            "branch_label": "不是播放站点",
            "evidence": "证据原文",
            "question": "player.ok",
            "source": "llm",
            "reached_play_page": False,
            "fallback_used": False,
        },
        "golden": {
            "label": "not_play_site",
            "label_label": "不是播放站点",
            "reviewed_by": "manual-review",
            "reviewed_at": "2026-10-12T00:00:00+00:00",
        },
    }
    row.update(over)
    return row


def _card(**over) -> dict:
    card = {
        "golden_id": "T001__r1__h",
        "dimension": "d10",
        "rubric_ref": "D-10",
        "delta_kind": "eq_class_mismatch",
        "gold_value": "not_play_site",
        "pred_value": "success",
        "pred_evidence": "存在 <video> 标签 ×1——代码层事实，不问 LLM",
        "suggested_bucket": None,
        "suggested_reason": "",
        "needs_human_reason": "没有规则命中：机器判不了这一条。",
        "facts": {"pred_origin": "llm", "reached_play_page": False},
    }
    card.update(over)
    return card


# ── 1. 条款原文 ─────────────────────────────────────────────


def test_条款块在下一个条款头或分隔线处截断():
    text = (
        "**D-10 结论正确性 ★** | 类别：x\n"
        "- 定义：结论一致\n"
        "---\n"
        "## 另一节\n"
        "不该被吞进来的引言\n"
        "**D-4 播放组件验证正确性** | 类别：y\n"
        "- 定义：组件\n"
    )
    clauses = evidence.parse_clauses(text)
    assert "结论一致" in clauses["D-10"]
    assert "另一节" not in clauses["D-10"]
    assert "不该被吞进来的引言" not in clauses["D-10"]
    assert clauses["D-4"].startswith("**D-4")


def test_真实rubric文档里三条都在且D10不串进批次级():
    clauses = evidence.load_clauses()
    for cid in ("D-4", "D-5", "D-10"):
        assert cid in clauses, cid
    # D-10 那块**不能**把「## 批次级断言表」这一节连同 B-1 一起吞进去。
    # （D-10 自己的正文里提到「批次级断言表」是**交叉引用**，那个留着；
    #   要防的是**章节标题与后续条款**被并进来。）
    assert "## 批次级断言表" not in clauses["D-10"]
    assert "**B-1" not in clauses["D-10"]
    assert clauses["B-1"].startswith("**B-1")
    assert "决策树" in clauses["D-5"]


def test_条款查不到返回空串_由调用方印标记():
    assert evidence.clause_text("D-99", {}) == ""
    assert evidence.clause_text("D-10", {"D-10": "x"}) == "x"


def test_rubric文档不存在时抛错而不是给空表(tmp_path):
    with pytest.raises(FileNotFoundError):
        evidence.load_clauses(tmp_path / "nope.md")


def test_rubric文档解析不出条款时抛错(tmp_path):
    p = tmp_path / "empty.md"
    p.write_text("这里没有任何条款\n", encoding="utf-8")
    with pytest.raises(ValueError):
        evidence.load_clauses(p)


# ── 2. 观察读数：两种形状都读得出 ──────────────────────────


def test_观察读数吃P1形状_body_len与elements_total():
    obs = evidence.observation_digest(_obs(video=3))
    assert obs["body_chars"] == 4
    assert obs["elements_total"] == 200
    assert obs["elements_listed"] == 2
    assert obs["video_tag_count"] == 3


def test_观察读数吃P2形状_body_chars与interactive_elements():
    p2 = {
        "url": "https://x.test/a",
        "page_title": "t",
        "body_text": "正文",
        "body_chars": 2,
        "body_source": "body_text",
        "video_tag_count": 0,
        "iframe_count": 1,
        "interactive_elements": [{"tag": "a"}],
    }
    obs = evidence.observation_digest(p2)
    assert obs["body_chars"] == 2
    assert obs["elements_listed"] == 1
    assert obs["iframe_count"] == 1


def test_观察读数把降级标记带出来():
    obs = evidence.observation_digest(_obs(degraded=["observation_missing"]))
    assert obs["degraded"] == ["observation_missing"]


# ── 3. 定位：读不到就说读不到 ─────────────────────────────


def test_按url匹配到站点样本(tmp_path):
    _archive(tmp_path)
    located = evidence.locate_sample(_row(), p2_root=tmp_path)
    assert located["sample"] is not None
    assert located["unit"] == "site-1"
    assert "匹配" in located["note"]


def test_url对不上时列候选而不是给空读数(tmp_path):
    _archive(tmp_path)
    located = evidence.locate_sample(_row(url="https://other.test/z"), p2_root=tmp_path)
    assert located["sample"] is None
    assert "没有匹配" in located["note"]
    assert located["candidates"]


def test_存档不存在时明说(tmp_path):
    located = evidence.locate_sample(_row(), p2_root=tmp_path / "none")
    assert located["sample"] is None
    assert "不存在" in located["note"]


def test_negative_pool行说读不到现场(tmp_path):
    located = evidence.locate_sample(
        _row(source_kind="negative_pool"), p2_root=tmp_path
    )
    assert located["sample"] is None
    assert "读不到现场" in located["note"]


def test_不认识的source_kind不猜(tmp_path):
    located = evidence.locate_sample(_row(source_kind="mystery"), p2_root=tmp_path)
    assert located["sample"] is None
    assert "不认识" in located["note"]


def test_缺定位字段时明说(tmp_path):
    located = evidence.locate_sample(_row(run_id=""), p2_root=tmp_path)
    assert located["sample"] is None
    assert "缺" in located["note"]


def test_p2_sample按样本文件读(tmp_path):
    (tmp_path / "samples").mkdir()
    (tmp_path / "samples" / "T001__r1__site-1.json").write_text(
        json.dumps(
            {
                "sample_id": "T001__r1__site-1",
                "unit": "site-1",
                "six": {"actions": [], "observations": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    located = evidence.locate_sample(
        _row(source_kind="p2_sample", sample_id="T001__r1__site-1"), p2_root=tmp_path
    )
    assert located["sample"] is not None
    assert located["unit"] == "site-1"


# ── 4. 读数组装与渲染 ──────────────────────────────────────


def test_动作与观察按下标配对(tmp_path):
    _archive(tmp_path)
    digest = evidence.card_digest(_card(), _row(), p2_root=tmp_path)
    assert len(digest["steps"]) == 1
    step = digest["steps"][0]
    assert step["action"]["tool"] == "goto"
    assert step["observation"]["url"] == "https://x.test/a"
    assert digest["archive"]["found"] is True


def test_render_card把读不到现场明说(tmp_path):
    digest = evidence.card_digest(
        _card(), _row(url="https://other.test/z"), p2_root=tmp_path
    )
    text = evidence.render_card(digest, {})
    assert "没有读到现场" in text
    assert "⚠ 条款 D-10 原文不可用" in text


def test_render_card印出条款原文(tmp_path):
    _archive(tmp_path)
    digest = evidence.card_digest(_card(), _row(), p2_root=tmp_path)
    text = evidence.render_card(digest, {"D-10": "**D-10 结论正确性**", "D-4": "**D-4**", "D-5": "**D-5**"})
    assert "**D-10 结论正确性**" in text
    assert "**D-4**" in text
    assert "**D-5**" in text


def test_render_compact读不到现场时打警告(tmp_path):
    digest = evidence.card_digest(
        _card(), _row(url="https://other.test/z"), p2_root=tmp_path
    )
    text = evidence.render_compact(digest, index=1, total=1)
    assert "⚠" in text
    assert "[1/1]" in text


def test_render_compact把标签数变化写成箭头(tmp_path):
    _archive(tmp_path, video=0)
    digest = evidence.card_digest(_card(), _row(), p2_root=tmp_path)
    digest["steps"].append(
        {
            "index": 2,
            "action": {"tool": "click", "params": {}, "origin": "model"},
            "observation": evidence.observation_digest(_obs(video=6)),
        }
    )
    text = evidence.render_compact(digest, index=1, total=1)
    assert "video=0→6" in text


# ── 5. CLI 三态 ────────────────────────────────────────────


def _cli_env(tmp_path):
    """造一套 CLI 能吃的目录：黄金集 v{N} + worklist v{N} + P1 存档。"""
    golden_root = tmp_path / "golden"
    (golden_root / "v7").mkdir(parents=True)
    (golden_root / "v7" / "calibration.jsonl").write_text(
        json.dumps(_row(), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    out = tmp_path / "calib"
    (out / "v7").mkdir(parents=True)
    (out / "v7" / "worklist.jsonl").write_text(
        json.dumps(_card(), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    p2 = tmp_path / "p2"
    p2.mkdir()
    _archive(p2)
    return golden_root, out, p2


def test_cli_show未知golden_id退2(tmp_path, capsys):
    golden_root, out, p2 = _cli_env(tmp_path)
    code = worklist.main([
        "show", "--version", "7", "--golden-root", str(golden_root),
        "--out", str(out), "--p2-root", str(p2), "--golden-id", "NOPE__x__y",
    ])
    assert code == 2
    assert "不在本轮" in capsys.readouterr().err


def test_cli_show既不给id也不给all退2(tmp_path, capsys):
    golden_root, out, p2 = _cli_env(tmp_path)
    code = worklist.main([
        "show", "--version", "7", "--golden-root", str(golden_root),
        "--out", str(out), "--p2-root", str(p2),
    ])
    assert code == 2
    assert "不知道要看哪张卡" in capsys.readouterr().err


def test_cli_show单卡出读数(tmp_path, capsys):
    golden_root, out, p2 = _cli_env(tmp_path)
    code = worklist.main([
        "show", "--version", "7", "--golden-root", str(golden_root),
        "--out", str(out), "--p2-root", str(p2),
        "--golden-id", "T001__r1__h",
    ])
    assert code == 0
    out_text = capsys.readouterr().out
    assert "T001__r1__h" in out_text
    assert "测试页" in out_text


def test_cli_show_全部出紧凑读数并可导出(tmp_path, capsys):
    golden_root, out, p2 = _cli_env(tmp_path)
    json_path = tmp_path / "evidence.jsonl"
    code = worklist.main([
        "show", "--version", "7", "--golden-root", str(golden_root),
        "--out", str(out), "--p2-root", str(p2), "--all", "--json", str(json_path),
    ])
    assert code == 0
    assert "[1/1]" in capsys.readouterr().out
    rows = [json.loads(x) for x in json_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(rows) == 1
    assert rows[0]["archive"]["found"] is True


# ── 6. 现场摘要与尺子（工作表那两样东西）────────────────────


def test_scene_summary把现场摊成可读的几行(tmp_path):
    """工作表的 ``scene`` 栏：人扫一眼就知道「这页长什么样」。"""
    _archive(tmp_path, video=2)
    digest = evidence.card_digest(_card(), _row(), p2_root=tmp_path)
    scene = evidence.scene_summary(digest)
    assert scene["found"] is True
    assert scene["status"] == "located"
    assert scene["unit"] == "site-1"
    assert scene["title"] == "测试页"
    assert len(scene["steps"]) == 1
    assert "goto" in scene["steps"][0]
    assert "video=2" in scene["steps"][0]
    assert scene["body"] == "正文内容"
    assert "第1步" in scene["body_from"]


def test_scene_summary读不到现场时四态是not_found(tmp_path):
    _archive(tmp_path)
    digest = evidence.card_digest(
        _card(), _row(url="https://other.test/z"), p2_root=tmp_path
    )
    scene = evidence.scene_summary(digest)
    assert scene["found"] is False
    assert scene["status"] == "not_found"
    assert scene["steps"] == []
    assert scene["note"] and scene["candidates"]


def test_scene_summary匹配到了但那一步没观察时明说为什么(tmp_path):
    """「匹配到了存档」与「读到现场」是两回事：存档自报降级把观察丢了的时候，
    不说清楚就会被读成「这一步什么都没发生」。"""
    _archive(tmp_path)
    digest = evidence.card_digest(_card(), _row(), p2_root=tmp_path)
    digest["steps"] = []
    digest["archive"]["degraded_from"] = ["steps"]
    scene = evidence.scene_summary(digest)
    assert scene["status"] == "located_no_steps"
    assert scene["degraded"] == ["steps"]
    assert "现场读不到" in scene["note"]


def test_scene_summary的正文标出来自第几步(tmp_path):
    """正文可能来自**更早**的一步（最后一步常是没读到正文的那步）——不标出来
    人会默认是最后一步，于是读到一个不对的页面的正文。"""
    _archive(tmp_path)
    digest = evidence.card_digest(_card(), _row(), p2_root=tmp_path)
    digest["steps"].append(
        {
            "index": 2,
            "action": {"tool": "click", "params": {}, "origin": "model"},
            "observation": evidence.observation_digest(
                _obs(body_text="", body_len=0, degraded=["observation_missing"])
            ),
        }
    )
    scene = evidence.scene_summary(digest)
    assert scene["body_from"] == "第1步（goto）"
    assert scene["body"] == "正文内容"


def test_clauses_markdown把条款原文拼成一把尺子():
    clauses = {
        "D-10": "**D-10 结论正确性 ★**\n- 判据：对照黄金集预存档的站点核验结论",
        "D-4": "**D-4 播放组件验证正确性**\n- 判据：对照 DOM 存档",
    }
    md = evidence.clauses_markdown(clauses, ["D-10", "D-4", "D-99"])
    assert "## D-10" in md and "## D-4" in md
    assert "对照黄金集预存档的站点核验结论" in md
    # 查不到的条款要**明说不可用**，不能静默少一条（人判尺子时少一条会偏向
    # 「没歧义」）
    assert "⚠️ 条款 D-99 原文不可用" in md


def test_clauses_markdown默认带D4与D5():
    md = evidence.clauses_markdown({})
    assert "## D-4" in md and "## D-5" in md


def test_build_scenes按golden_id把现场配到卡上(tmp_path):
    _archive(tmp_path)
    scenes = worklist.build_scenes([_card()], [_row()], p2_root=tmp_path)
    assert set(scenes) == {"T001__r1__h"}
    assert scenes["T001__r1__h"]["found"] is True
    assert scenes["T001__r1__h"]["status"] == "located"


def test_build_scenes配不上时明说版本错配_不静默给空(tmp_path):
    _archive(tmp_path)
    scenes = worklist.build_scenes(
        [_card()], [_row(golden_id="别的行")], p2_root=tmp_path
    )
    scene = scenes["T001__r1__h"]
    assert scene["status"] == "not_read"
    assert "版本错配" in scene["note"]


# ── 7. 真实读数（v7，33 张卡）────────────────────────────────


def test_真实v7的33张卡都能定位或明说定位不到():
    """真数据回归：33 张卡里，定位不到的**必须**明说，不能给空读数。

    当前读数（2026-10-23，v7）：**28 张定位到存档 / 5 张明说定位不到**
    （5 张全是「存档里没有匹配该 url 的站点样本」——那个 url 压根没被访问成
    站点样本，**不是「现场是空的」**）。门槛留了余量（≥ 27）：真数据会重跑，
    这里守的是「**定位不到的必须明说**」这条纪律，不是某个具体数字。
    """
    import pathlib

    # 路径按**文件位置**算，不按 cwd——pytest 从哪儿起跑都行。
    pkg = pathlib.Path(__file__).resolve().parents[2]
    golden_root = pkg / "evaluation" / "golden" / "data"
    out_root = pkg / "output" / "pipeline" / "calibration"
    cards_path = out_root / "v7" / "worklist.jsonl"
    if not cards_path.exists():
        pytest.skip("v7 的 worklist 还没生成（先跑 worklist build）")
    cards = [
        json.loads(x)
        for x in cards_path.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    rows = {}
    for name in ("calibration.jsonl", "regression.jsonl", "holdout.jsonl"):
        p = golden_root / "v7" / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows.setdefault(row["golden_id"], row)
    assert cards, "v7 的卡不该是空的"
    located = 0
    for card in cards:
        digest = evidence.card_digest(card, rows[card["golden_id"]])
        if digest["archive"]["found"]:
            located += 1
        else:
            assert digest["archive"]["note"], card["golden_id"]
    # 定位到的必须是多数（读不到现场的那几条恰恰是「压根没访问过」的卡）
    assert located >= len(cards) - 6, f"只定位到 {located}/{len(cards)}，比预期少"
