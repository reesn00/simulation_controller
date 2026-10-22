"""双跑对账（P5）：判据要**一眼能判，且判不了的要说判不了**。

三条容易写错的：

1. 分母。分支占比的分母是「出过分支的站点数」，不是 run 数——用 run 数
   会让「一个 run 访问了 3 个站」的分支权重被压成 1/1。
2. 「判不了」不是「过了」。基线中位数为 0 时比值无从计算，写 0 就是把
   没法判写成了一模一样。
3. 两侧任务集合不同必须显形。悄悄按并集比，会让少跑的那一侧分母更小、
   比例差看着更小。
"""

from __future__ import annotations

import json
from pathlib import Path

from trajectory_pipeline.executor import dual_run


def _obs(*, body: int, elements: int, basis: dict | None = None) -> dict:
    out = {
        "url": "https://example.com/x",
        "body_text": "x" * body,
        "body_len": body,
        "elements_total": elements,
        "interactive_elements": [{"tag": "a", "label": "x"}] * min(elements, 2),
    }
    if basis is not None:
        out["element_basis"] = basis
    return out


def _record(task_id: str, *, driver: str, branches: dict[str, int],
            body: int, elements: int, negatives: int = 0,
            search_blocked: bool = False, basis: dict | None = None) -> dict:
    return {
        "task_id": task_id,
        "driver": driver,
        "search_blocked": search_blocked,
        "search_observation": _obs(body=body, elements=elements, basis=basis),
        "steps": [{"action": {}, "observation": _obs(body=body, elements=elements,
                                                    basis=basis),
                   "error": "", "rationale": None}],
        "visits": [],
        "ledger": {"by_branch": dict(branches)},
        "outcomes": [
            {"url": f"https://example.com/{task_id}/{i}", "branch": "no_play_control",
             "evidence": "证据原文", "is_negative_sample": True}
            for i in range(negatives)
        ],
    }


def _basis(*, candidates: int, kept: int, hidden: int = 0) -> dict:
    """一份共享口径的账（形状照 :mod:`element_basis` 的真源）。"""
    return {
        "shared": {
            "candidates": candidates, "hidden": hidden,
            "visible": candidates - hidden,
            "role_matched": kept, "role_unmatched": candidates - kept - hidden,
            "nameless_dropped": 0, "ambiguous_dropped": 0,
            "kept": kept, "truncated": 0,
        },
        "face": {"nodes": candidates, "kept": kept},
    }


def _write(root: Path, records: list[dict]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for n, record in enumerate(records):
        name = f"{record['task_id']}__run{n}.json"
        (root / name).write_text(json.dumps(record, ensure_ascii=False),
                                 encoding="utf-8")
    return root


class Test汇总:
    def test_分支分母是站点不是run(self, tmp_path):
        root = _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={"a": 1, "b": 3},
                    body=100, elements=10),
        ])
        side = dual_run.summarize("A", root)
        assert side.outcomes_total == 4
        assert side.branch_shares["b"] == 75.0

    def test_观察数与中位数来自全部观察(self, tmp_path):
        # 每份存档 2 个观察（search + 1 步），body 都一样
        root = _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=100, elements=10),
            _record("T2", driver="obscura", branches={}, body=300, elements=30),
        ])
        side = dual_run.summarize("A", root)
        assert side.observations == 4
        assert side.body_len_median == 200.0
        assert side.elements_median == 20.0

    def test_驱动面按打开不算抛(self, tmp_path):
        root = _write(tmp_path / "a", [
            _record("T1", driver="", branches={}, body=1, elements=1),
            _record("T2", driver="来自未来的驱动", branches={}, body=1, elements=1),
        ])
        side = dual_run.summarize("A", root)
        assert side.surfaces == {"obscura-1": 1, "unknown": 1}

    def test_探针文件不是存档(self, tmp_path):
        root = _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=1, elements=1),
        ])
        (root / "probe_observe.json").write_text("{}", encoding="utf-8")
        assert dual_run.summarize("A", root).runs == 1


class Test判据:
    def test_分支差超5pp判fail并点名(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={"x": 10}, body=100, elements=10),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox",
                    branches={"x": 5, "y": 5}, body=100, elements=10),
        ]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        branch = crit["分支分布"]
        assert branch["result"] == "fail"
        assert set(branch["detail"]["offenders"]) == {"x", "y"}
        assert report["verdict"] == "fail"

    def test_中位数差30个百分点为界(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=100, elements=100),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=130, elements=100),
        ]))
        crit = {c["criterion"]: c for c in dual_run.compare(a, b)["criteria"]}
        assert crit["body_text 长度中位数"]["detail"]["ratio"] == 0.3
        assert crit["body_text 长度中位数"]["result"] == "pass"

    def test_基线为0时判不了而不是判过(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=0, elements=0),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=50, elements=50),
        ]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        assert crit["body_text 长度中位数"]["result"] == "incomplete"
        assert report["verdict"] == "incomplete"

    def test_两侧任务集合不同要显形(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=10, elements=1),
            _record("T2", driver="obscura", branches={}, body=10, elements=1),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=10, elements=1),
        ]))
        report = dual_run.compare(a, b)
        assert report["paired_task_ids"] == ["T1"]
        assert report["only_in_a"] == ["T2"]
        assert "只在 A：T2" in dual_run.format_report(report)

    def test_负样本原文进报告供人读(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=1, elements=1,
                    negatives=2),
        ]))
        side = a.to_json()
        assert len(side["negatives"]) == 2
        assert side["negatives"][0]["evidence"] == "证据原文"
        report = dual_run.compare(a, a)
        assert "负样本（逐条读" in dual_run.format_report(report)


class Test元素口径:
    """§6.1 判据③在换层场景下的口径（设计方案 04 §4.9.6）。

    **两面的 ``elements_total`` 不是同一个东西**——obscura 数上游工具给的
    交互元素，camoufox 数 ARIA 树里过了词表的那些。直接比得出的差值
    （本轮实测 −70%）混着「枚举基础不同」与「过滤规则不同」两件事，
    **不是一个可解释的量**。
    """

    def test_跨面时面内口径判不了而不是判过(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=10, elements=100),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=10, elements=30),
        ]))
        crit = {c["criterion"]: c for c in dual_run.compare(a, b)["criteria"]}
        face = crit["interactive_elements 条数中位数（面内口径）"]
        assert face["result"] == "incomplete"
        # 读数留着（人要看），但结论是「不可解释」。
        assert face["detail"]["a"] == 100
        assert face["detail"]["b"] == 30
        assert "不可解释" in face["detail"]["note"]

    def test_同面时面内口径照旧判(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=10, elements=100),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="obscura", branches={}, body=10, elements=130),
        ]))
        crit = {c["criterion"]: c for c in dual_run.compare(a, b)["criteria"]}
        face = crit["interactive_elements 条数中位数（面内口径）"]
        assert face["result"] == "pass"

    def test_没有共享口径就判不了而不是判过(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=10, elements=100),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=10, elements=30),
        ]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        shared = crit["跨面可比元素条数中位数（共享口径）"]
        assert shared["result"] == "incomplete"
        assert report["verdict"] == "incomplete"

    def test_共享口径可比时照旧判(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=10, elements=100,
                    basis=_basis(candidates=100, kept=100)),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=10, elements=30,
                    basis=_basis(candidates=100, kept=110)),
        ]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        shared = crit["跨面可比元素条数中位数（共享口径）"]
        assert shared["result"] == "pass"
        assert shared["detail"]["a"] == 100
        assert shared["detail"]["b"] == 110

    def test_共享口径差太多判fail(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=10, elements=100,
                    basis=_basis(candidates=100, kept=100)),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=10, elements=30,
                    basis=_basis(candidates=100, kept=30)),
        ]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        shared = crit["跨面可比元素条数中位数（共享口径）"]
        assert shared["result"] == "fail"
        assert report["verdict"] == "fail"

    def test_归因表逐档列出差值落在哪一档(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=10, elements=100,
                    basis=_basis(candidates=100, kept=100)),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=10, elements=30,
                    basis=_basis(candidates=100, kept=30)),
        ]))
        report = dual_run.compare(a, b)
        rows = {r["stage"]: r for r in report["attribution"]["shared"]}
        assert rows["candidates"]["a"] == 100
        assert rows["candidates"]["b"] == 100
        assert rows["kept"]["diff"] == -70
        # 档位顺序：枚举基础在前、过滤档在后。
        stages = [r["stage"] for r in report["attribution"]["shared"]]
        assert stages.index("candidates") < stages.index("kept")

    def test_报告里看得见归因表(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _record("T1", driver="obscura", branches={}, body=10, elements=100,
                    basis=_basis(candidates=100, kept=100)),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _record("T1", driver="camoufox", branches={}, body=10, elements=30,
                    basis=_basis(candidates=100, kept=30)),
        ]))
        text = dual_run.format_report(dual_run.compare(a, b))
        assert "元素差值归因" in text
        assert "kept" in text


def _page_record(task_id: str, *, driver: str,
                 pages: list[tuple[str, str, list[str], int | None]]) -> dict:
    """一份带**页面行**的存档。

    ``pages`` 每项 = ``(落地 URL, 分支, degraded, body_len)``。
    落地 URL 另配一个**每次都变的百度 token**——真机上两面的
    ``/link?url=`` 就是不同的，配对必须扛得住这个。
    """
    outcomes, visits, branches = [], [], {}
    for i, (landed, branch, degraded, body_len) in enumerate(pages):
        token = f"tok{abs(hash(landed)) % 10**9 + i}"
        outcomes.append({
            "url": landed, "branch": branch, "evidence": "证据原文",
            "is_negative_sample": True,
        })
        visits.append({
            "url": f"http://www.baidu.com/link?url={token}",
            "landed_url": landed, "success": False, "notes": [],
            "steps": [],
            "site_obs": {"url": landed, "degraded": degraded,
                         "body_len": body_len, "elements_total": 3},
            "player_obs": {},
        })
        if branch:
            branches[branch] = branches.get(branch, 0) + 1
    return {
        "task_id": task_id, "driver": driver, "search_blocked": False,
        "search_observation": _obs(body=10, elements=3),
        "steps": [], "visits": visits,
        "ledger": {"by_branch": branches},
        "outcomes": outcomes,
    }


class Test配对比较:
    """配对是**读数**不是判据——但读数错了要红。"""

    def test_落地URL归一化去掉查询串与www(self):
        # 百度跳转 token 每次都变、查询串也变 ⇒ 只留 host+path
        assert dual_run._page_key(
            "https://www.iqiyi.com/v_abc.html?fr=baidu_x&vfm=2008_"
        ) == "iqiyi.com/v_abc.html"
        assert dual_run._page_key(
            "http://www.baidu.com/link?url=TOKEN"
        ) == "baidu.com/link"
        assert dual_run._page_key("https://example.com/x/") == "example.com/x"
        assert dual_run._page_key("") == ""

    def test_按落地URL配对_百度token不同也算同一页(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _page_record("T1", driver="obscura",
                         pages=[("https://v.qq.com/x/cover/a.html", "blocked",
                                 [], 100)]),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _page_record("T1", driver="camoufox",
                         pages=[("https://www.v.qq.com/x/cover/a.html", "blocked",
                                 [], 100)]),
        ]))
        report = dual_run.compare(a, b)
        paired = report["paired_pages"]
        assert paired["totals"]["paired"] == 1
        assert paired["totals"]["disagreements"] == 0

    def test_未配上的页面不进分母(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _page_record("T1", driver="obscura", pages=[
                ("https://a.com/1", "blocked", [], 10),
                ("https://only-a.com/2", "blocked", [], 10),
            ]),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _page_record("T1", driver="camoufox", pages=[
                ("https://a.com/1", "unresolved", [], 10),
                ("https://only-b.com/3", "blocked", [], 10),
            ]),
        ]))
        paired = dual_run.compare(a, b)["paired_pages"]
        assert paired["totals"]["paired"] == 1
        assert paired["totals"]["only_a"] == 1
        assert paired["totals"]["only_b"] == 1
        # A 的被拦率按**配对子集**算：配上的 1 个里 A 拦了、B 没拦
        assert paired["a_block_rate_pct"] == 100.0
        assert paired["b_block_rate_pct"] == 0.0

    def test_分歧逐条列出并带降级读数(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _page_record("T1", driver="obscura", pages=[
                ("https://x.com/1", "unresolved", ["body_text"], 0),
            ]),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _page_record("T1", driver="camoufox", pages=[
                ("https://x.com/1", "component_unverified", [], 900),
            ]),
        ]))
        paired = dual_run.compare(a, b)["paired_pages"]
        diffs = paired["per_task"]["T1"]["disagreements"]
        assert len(diffs) == 1
        assert diffs[0]["a_branch"] == "unresolved"
        assert diffs[0]["b_branch"] == "component_unverified"
        # **观测能力差 vs 判定差**靠 degraded/body_len 分开
        assert diffs[0]["a_degraded"] == ["body_text"]
        assert diffs[0]["b_degraded"] == []
        assert diffs[0]["a_body_len"] == 0
        assert diffs[0]["b_body_len"] == 900

    def test_判据四带配对读数(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _page_record("T1", driver="obscura", pages=[
                ("https://x.com/1", "blocked", [], 10),
            ]),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _page_record("T1", driver="camoufox", pages=[
                ("https://x.com/1", "blocked", [], 10),
            ]),
        ]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        block = crit["反爬拦截率（读数，不构成否决）"]
        assert block["result"] == "needs_manual"
        assert block["detail"]["paired_a_pct"] == 100.0
        assert block["detail"]["paired_b_pct"] == 100.0
        assert block["detail"]["paired_pages"] == 1

    def test_报告里看得见配对段(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _page_record("T1", driver="obscura", pages=[
                ("https://x.com/1", "unresolved", ["body_text"], 0),
            ]),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _page_record("T1", driver="camoufox", pages=[
                ("https://x.com/1", "component_unverified", [], 900),
            ]),
        ]))
        text = dual_run.format_report(dual_run.compare(a, b))
        assert "配对比较" in text
        assert "配对子集拦截率" in text
        assert "unresolved" in text and "component_unverified" in text

    def test_配不上时读数是空不是零(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _page_record("T1", driver="obscura", pages=[
                ("https://only-a.com/1", "blocked", [], 10),
            ]),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _page_record("T1", driver="camoufox", pages=[
                ("https://only-b.com/2", "blocked", [], 10),
            ]),
        ]))
        paired = dual_run.compare(a, b)["paired_pages"]
        # **没配上一对 ≠ 一个都没被拦**：读数必须是「—」不是 0.0%
        assert paired["a_block_rate_pct"] is None
        assert paired["b_block_rate_pct"] is None


class Test拦截率口径:
    """**登录墙不是反爬拦截**——合并口径会给出相反的结论（2026-10-21 实测）。"""

    def test_真反爬与登录墙分开算(self, tmp_path):
        root = _write(tmp_path / "a", [
            _page_record("T1", driver="obscura", pages=[
                ("https://x.com/1", "blocked", [], 10),
                ("https://x.com/2", "login_required", [], 10),
                ("https://x.com/3", "login_wall_or_blocked", [], 10),
                ("https://x.com/4", "unresolved", [], 10),
            ]),
        ])
        side = dual_run.summarize("A", root)
        assert side.outcomes_total == 4
        assert side.antibot_rate == 25.0        # 只有 blocked 算反爬
        assert side.loginwall_rate == 50.0      # 两种登录墙
        assert side.block_rate == 75.0          # 合并口径（保留，不改语义）

    def test_判据四同时给合并与拆分读数(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _page_record("T1", driver="obscura", pages=[
                ("https://x.com/1", "blocked", [], 10),
                ("https://x.com/2", "login_required", [], 10),
            ]),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _page_record("T1", driver="camoufox", pages=[
                ("https://x.com/1", "blocked", [], 10),
                ("https://x.com/2", "login_required", [], 10),
            ]),
        ]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        d = crit["反爬拦截率（读数，不构成否决）"]["detail"]
        assert d["a_antibot_pct"] == 50.0
        assert d["a_loginwall_pct"] == 50.0
        assert d["paired_a_antibot_pct"] == 50.0
        assert d["paired_a_loginwall_pct"] == 50.0

    def test_配对子集也拆开(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [
            _page_record("T1", driver="obscura", pages=[
                ("https://x.com/1", "blocked", [], 10),
                ("https://x.com/2", "unresolved", [], 10),
            ]),
        ]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [
            _page_record("T1", driver="camoufox", pages=[
                ("https://x.com/1", "unresolved", [], 10),
                ("https://x.com/2", "login_wall_or_blocked", [], 10),
            ]),
        ]))
        paired = dual_run.compare(a, b)["paired_pages"]
        # A：1 个反爬、0 个登录墙；B：0 个反爬、1 个登录墙
        assert paired["a_antibot_rate_pct"] == 50.0
        assert paired["b_antibot_rate_pct"] == 0.0
        assert paired["a_loginwall_rate_pct"] == 0.0
        assert paired["b_loginwall_rate_pct"] == 50.0


def _family_record(task_id: str, *, driver: str,
                   search_body: int = 0, search_kept: int = 0,
                   site_body: int = 0, site_kept: int = 0,
                   visit_body: int = 0, visit_kept: int = 0) -> dict:
    """一份存档，观察**摊在三个族**上：搜索页 / 站点页 / 站内步骤。"""
    return {
        "task_id": task_id,
        "driver": driver,
        "search_blocked": False,
        "search_observation": _obs(
            body=search_body, elements=search_kept,
            basis=_basis(candidates=100, kept=search_kept) if search_kept else None),
        "steps": [],
        "visits": [{
            "site_url": "https://x.com/1",
            "site_obs": _obs(
                body=site_body, elements=site_kept,
                basis=_basis(candidates=10, kept=site_kept) if site_kept else None),
            "player_obs": None,
            "steps": [{
                "action": {},
                "observation": _obs(
                    body=visit_body, elements=visit_kept,
                    basis=_basis(candidates=20, kept=visit_kept) if visit_kept else None),
                "error": "", "rationale": None,
            }],
        }],
        "ledger": {"by_branch": {}},
        "outcomes": [],
    }


class Test分族读数:
    """**混合中位数不可当结论**——各族规模差一个量级，混起来取中位数
    会被各族占比带走（2026-10-21 第二轮双跑实测）。"""

    def test_观察按族分列(self, tmp_path):
        root = _write(tmp_path / "a", [_family_record(
            "T1", driver="obscura",
            search_body=1000, search_kept=50,
            site_body=10, site_kept=2,
            visit_body=100, visit_kept=5,
        )])
        side = dual_run.summarize("A", root)
        assert side.observations_by_family == {
            "search": 1, "site_obs": 1, "visit_step": 1,
        }
        assert side.body_len_median_by_family["site_obs"] == 10.0
        assert side.body_len_median_by_family["visit_step"] == 100.0
        assert side.shared_ledger_by_family["site_obs"]["kept"] == 2.0
        assert side.shared_ledger_by_family["visit_step"]["kept"] == 5.0
        # 共享账的观察条数**单独记**——不是该族的观察总数
        assert side.shared_observations_by_family == {
            "search": 1, "site_obs": 1, "visit_step": 1,
        }

    def test_判据二带分族读数(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [_family_record(
            "T1", driver="obscura", site_body=100, site_kept=10,
        )]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [_family_record(
            "T1", driver="camoufox", site_body=50, site_kept=20,
        )]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        fam = crit["body_text 长度中位数"]["detail"]["by_family"]
        assert fam["site_obs"]["a"] == 100.0
        assert fam["site_obs"]["b"] == 50.0
        assert fam["site_obs"]["ratio"] == 0.5
        assert fam["site_obs"]["a_n"] == 1

    def test_判据三带分族读数(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [_family_record(
            "T1", driver="obscura", site_kept=10, visit_kept=5,
        )]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [_family_record(
            "T1", driver="camoufox", site_kept=20, visit_kept=2,
        )]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        fam = crit["跨面可比元素条数中位数（共享口径）"]["detail"]["by_family"]
        # 站内步骤 A 更好、站点页 B 更好——**方向不一致，不投票**
        assert fam["visit_step"]["a"] == 5.0
        assert fam["visit_step"]["b"] == 2.0
        assert fam["site_obs"]["a"] == 10.0
        assert fam["site_obs"]["b"] == 20.0

    def test_分族读数不合成总分(self, tmp_path):
        """分族表**没有**「总体」那一行——合成就是投票。"""
        a = dual_run.summarize("A", _write(tmp_path / "a", [_family_record(
            "T1", driver="obscura", site_kept=10, visit_kept=5,
        )]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [_family_record(
            "T1", driver="camoufox", site_kept=20, visit_kept=2,
        )]))
        report = dual_run.compare(a, b)
        crit = {c["criterion"]: c for c in report["criteria"]}
        fam = crit["跨面可比元素条数中位数（共享口径）"]["detail"]["by_family"]
        assert set(fam) == {"site_obs", "visit_step"}
        assert "总计" not in fam and "overall" not in fam

    def test_报告打出分族表(self, tmp_path):
        a = dual_run.summarize("A", _write(tmp_path / "a", [_family_record(
            "T1", driver="obscura", site_body=100, site_kept=10,
        )]))
        b = dual_run.summarize("B", _write(tmp_path / "b", [_family_record(
            "T1", driver="camoufox", site_body=50, site_kept=20,
        )]))
        text = dual_run.format_report(dual_run.compare(a, b))
        assert "按观察槽族分列" in text
        assert "site_obs" in text
        assert "没有投票、没有合成" in text