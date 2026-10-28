"""「这条不是负样本」的处置是**两个动作、一个顺序**——钉成契约。

为什么单独一支
--------------
台账 H-17 把这件事记成「**转正无路**」（2026-10-21），07 手册跟着在三处
告诉复核员「要转正 ⇒ 没有处置路径」。而路其实存在，只是**从来没有一处
把这两步写在一起**，所以读文档的人拼不出来：

1. ``cli downgrade``（:mod:`executor.downgrade`）——把存档里的结论落
   ``unresolved``（**不是** ``branch=None``，见下），
2. ``cli pool-drop``（:mod:`executor.pool_drop`）——把因此成了孤儿的
   那一行从 ``negative.jsonl`` 撤掉，原行 + 理由进 ``pool_dropped.jsonl``。

顺序是**载荷**的（``pool_drop`` 模块 docstring 写的「先改存档、后撤池行」）：
反过来做，撤池那一刻存档里还写着负分支 ⇒ 门禁当场报
``negative_not_pooled``，读起来像「撤错了」。

⚠️ 第二条顺手钉住：**「转正」不是把 ``branch`` 改成 ``None``。**
``None`` 的含义是「视频播出来了」，而那批假负样本的采集事实是
``visits[].success=False`` / ``reached_play_page=False``——只改结论不改
事实会被 ``success_branch_mismatch`` 抓住，改事实则是「改写采集事实去
迎合结论」（``downgrade`` 模块 docstring 坑 4）。⇒ 诚实的落点是
``unresolved``（「我们没判出来」），不是「成功」。
"""

from __future__ import annotations

import json
from pathlib import Path

from trajectory_pipeline.executor import downgrade as dg
from trajectory_pipeline.executor import integrity
from trajectory_pipeline.executor import pool_drop as pd

URL = "https://56dy.com/movie/2842"


# ── 语料 ───────────────────────────────────────────────────────────

def _obs(url: str = URL) -> dict:
    return {"url": url, "page_title": "T", "body_text": "正文",
            "truncated": False, "degraded": [], "video_tag_count": 0,
            "iframe_count": 0, "interactive_elements": [], "links": []}


def _step(url: str = URL) -> dict:
    return {"action": {"tool": "goto", "params": {"url": url},
                       "origin": "model", "target": None},
            "observation": _obs(url)}


def _visit(url: str = URL, success: bool = False) -> dict:
    return {"url": url, "landed_url": url, "success": success,
            "notes": [], "steps": [_step(url)], "site_obs": _obs(url),
            "player_obs": None}


def _outcome(url: str = URL, branch: str = "component_unverified",
             **over) -> dict:
    """一条**假负样本**：点击播放控件超时，压根没到播放页。"""
    rec = {"url": url, "branch": branch, "branch_label": "",
           "evidence": "点击 ref=e12 失败：Locator.click: Timeout 5000ms exceeded",
           "question": "player.ok", "source": "llm", "fallback_used": False,
           "reached_play_page": False, "is_negative_sample": True}
    rec.update(over)
    return rec


def _archive(outcomes=None, visits=None) -> dict:
    return {
        "task_id": "T001", "title": "功夫",
        "user_prompt": "找到电影《功夫》在线观看的可播放网址。",
        "run_config": {"engine": "baidu", "max_candidates": 12,
                       "max_chars": 4000, "stop_after_success": True,
                       "per_site_timeout_s": 180.0},
        "steps": [_step()],
        "ledger": {"by_branch": {"component_unverified": 1},
                   "negative_samples": 1, "succeeded": 0,
                   "missing_branches": []},
        "visits": [_visit()] if visits is None else visits,
        "outcomes": [_outcome()] if outcomes is None else outcomes,
        "warnings": [],
    }


def _write_archive(root: Path, arc: dict | None = None,
                   name: str = "T001__aa.json") -> Path:
    path = root / name
    path.write_text(json.dumps(arc or _archive(), ensure_ascii=False),
                    encoding="utf-8")
    return path


def _write_pool(root: Path, rows) -> Path:
    path = root / "negative.jsonl"
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                            for r in rows), encoding="utf-8")
    return path


def _pool_row(url: str = URL, branch: str = "component_unverified") -> dict:
    return {"task_id": "T001", "url": url, "branch": branch,
            "branch_label": "", "question": "player.ok", "source": "llm",
            "evidence": "点击 ref=e12 失败：Locator.click: Timeout 5000ms exceeded"}


def _codes(report: integrity.IntegrityReport) -> set[str]:
    return {f.code for f in report.findings}


def _pool_lines(root: Path) -> list[str]:
    path = root / "negative.jsonl"
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── 正路：先改存档、后撤池行 ───────────────────────────────────────

class Test出池两步:

    def test_先降级后撤池_门禁不报任何一条(self, tmp_path):
        """两步走完：存档落 ``unresolved``、池里那行没了、门禁干净。

        这是 H-17 那类「**不是负样本**」的完整处置——2026-10-21 记的是
        「无路」，实际是这条路没人写下来。
        """
        arc_path = _write_archive(tmp_path)
        _write_pool(tmp_path, [_pool_row()])

        # ① 存档侧：结论不成立 ⇒ unresolved（**不是** branch=None）
        out = dg.apply_to_root(tmp_path, select=dg.select_by_url("56dy.com"),
                               reason="人工复核：资源存在，标签不成立",
                               dry_run=False)
        assert [s["downgraded"] for s in out] == [1]
        arc = _read(arc_path)
        assert arc["outcomes"][0]["branch"] == "unresolved"
        assert arc["outcomes"][0]["branch_before_downgrade"] == \
            "component_unverified"
        # 采集事实**不动**：没到过播放页就是没到过
        assert arc["outcomes"][0]["reached_play_page"] is False
        assert arc["visits"][0]["success"] is False
        # ledger 记账跟着走，否则报表上的负样本数比实际多
        assert arc["ledger"]["negative_samples"] == 0
        assert "is_negative_sample" not in arc["outcomes"][0]

        # 池里那一行此刻是**孤儿**，但门禁还不报——报的是「存档说负样本
        # 却没并池」，而存档已经不说负样本了
        assert "negative_not_pooled" not in _codes(integrity.check_root(tmp_path))
        assert len(_pool_lines(tmp_path)) == 1

        # ② 池侧：撤掉成了孤儿的那一行
        dropped = pd.drop_from_root(tmp_path, url_fragments=("56dy.com",),
                                    reason="人工复核：资源存在，标签不成立",
                                    dry_run=False)
        assert dropped["dropped"] == 1
        assert _pool_lines(tmp_path) == []
        report = integrity.check_root(tmp_path)
        assert _codes(report) == set(), f"两步走完还报：{_codes(report)}"
        assert report.ok

    def test_撤池行留痕带原行与理由(self, tmp_path):
        """半年后能回答「这行为什么没了」——撤行是池**唯一**的删除动作。"""
        _write_archive(tmp_path)
        _write_pool(tmp_path, [_pool_row()])
        dg.apply_to_root(tmp_path, select=dg.select_by_url("56dy.com"),
                         reason="人判：资源存在", dry_run=False)
        pd.drop_from_root(tmp_path, url_fragments=("56dy.com",),
                          reason="人判：资源存在", dry_run=False)

        log = tmp_path / pd.DROPPED_LOG_NAME
        assert log.exists()
        entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        assert entry["reason"] == "人判：资源存在"
        assert entry["row"]["url"] == URL
        assert entry["row"]["branch"] == "component_unverified"

    def test_有复核档时门禁也干净(self, tmp_path):
        """**门禁读的是复核档**（``select_archives`` 优先取它）。

        只降原档 ⇒ 复核档还留着负分支 ⇒ 池行撤掉后门禁当场报
        ``negative_not_pooled``——「撤错了」的假象。
        ⇒ 降级必须**两层都落**（``downgrade`` 模块 docstring 坑 1）。
        """
        _write_archive(tmp_path)
        _write_archive(tmp_path, name="T001__aa.reviewed.json")
        _write_pool(tmp_path, [_pool_row()])

        dg.apply_to_root(tmp_path, select=dg.select_by_url("56dy.com"),
                         reason="人判：资源存在", dry_run=False)
        assert "negative_not_pooled" not in _codes(integrity.check_root(tmp_path))

        pd.drop_from_root(tmp_path, url_fragments=("56dy.com",),
                          reason="人判：资源存在", dry_run=False)
        report = integrity.check_root(tmp_path)
        assert _codes(report) == set(), f"两步走完还报：{_codes(report)}"
        assert report.ok


# ── 反路：顺序颠倒会当场报 ───────────────────────────────────────

class Test顺序颠倒:

    def test_先撤池后降级_门禁报negative_not_pooled(self, tmp_path):
        """撇开这一步，是「先撤池行」的人唯一会看到的信号。

        存档还写着 ``component_unverified``，池里那行却没了 ⇒
        门禁说「真负样本没并池」（DEGRADED）。这条断言把
        ``pool_drop`` docstring 的「先改存档、后撤池行」变成可执行的规定。
        """
        _write_archive(tmp_path)
        _write_pool(tmp_path, [_pool_row()])

        pd.drop_from_root(tmp_path, url_fragments=("56dy.com",),
                          reason="先撤了池行", dry_run=False)
        report = integrity.check_root(tmp_path)
        assert "negative_not_pooled" in _codes(report)

        # 补上第一步就干净了——顺序可补救，不用重跑采集
        dg.apply_to_root(tmp_path, select=dg.select_by_url("56dy.com"),
                         reason="补降级", dry_run=False)
        assert "negative_not_pooled" not in _codes(integrity.check_root(tmp_path))


# ── 「转正」不等于 branch=None ────────────────────────────────────

class Test转正不是改branch:

    def test_只改结论不改事实会被success_branch_mismatch抓住(self, tmp_path):
        """把 ``branch`` 手改成 ``None`` 想「转正」——门禁当场报。

        这条是给「翻文档看到『要转正』就去改存档」的人准备的：
        改了结论没改采集事实，``success=False`` 与 ``branch=None``
        互相打架（``integrity.py`` 的 ``succeeded != (branch is None)``）。
        """
        arc = _archive(outcomes=[_outcome(branch=None,
                                          is_negative_sample=False)],
                       visits=[_visit(success=False)])
        path = _write_archive(tmp_path, arc)
        assert "success_branch_mismatch" in _codes(
            integrity.check_archive(path))

    def test_连事实一起改才是真的伪造(self, tmp_path):
        """两边都改成「成功」门禁就不报了——**所以它不是一个安全阀**。

        记这条是为了让人看清：门禁能拦「改一半」，拦不住「全改」。
        全改等于伪造采集事实（``downgrade`` 坑 4：为迎合结论改写
        ``reached_play_page``）。⇒ 处置口径必须落在文档上，
        而不是指望门禁把这条堵死。
        """
        arc = _archive(outcomes=[_outcome(branch=None,
                                          is_negative_sample=False)],
                       visits=[_visit(success=True)])
        path = _write_archive(tmp_path, arc)
        assert "success_branch_mismatch" not in _codes(
            integrity.check_archive(path))