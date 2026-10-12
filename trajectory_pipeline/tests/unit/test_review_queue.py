"""人工复核队列的测试。

重点不是"能不能抽出来"，而是**复核员拿到的东西够不够做判断**——
上下文不全的队列等于把判断责任转嫁给复核员却没有给材料，
那种队列会退化成"打开网址自己再看一遍"，也就没有存在意义。
"""

from __future__ import annotations

import json

import pytest

from trajectory_pipeline.executor.cli import _load_verdicts
from trajectory_pipeline.executor.review_queue import (
    AUTO_BY_INSUFFICIENT,
    AUTO_BY_PORTAL,
    REVIEW_BRANCHES,
    VERDICTS,
    ReviewQueueError,
    apply_verdicts,
    collect,
    extract_items,
    prefill,
    settle_same_site,
    write_queue,
)


def _archive(outcomes: list[dict], visits: list[dict] | None = None) -> dict:
    return {
        "task_id": "T001",
        "title": "功夫",
        "query": "功夫 在线观看",
        "outcomes": outcomes,
        "visits": visits if visits is not None else [],
        "ledger": {"total_sites": len(outcomes), "by_branch": {}},
    }


def _visit(url: str, *, landed: str | None = None, **obs) -> dict:
    base = {
        "url": url,
        "landed_url": landed or url,
        "success": False,
        "notes": [],
        "player_obs": {
            "url": landed or url,
            "page_title": "功夫 - 搜狐视频",
            "body_preview": "功夫 在线播放 高清完整版",
            "body_len": 4210,
            "degraded": [],
            "video_tag_count": 1,
            "iframe_count": 3,
            "interactive_elements": [
                {"ref": "e1", "tag": "button", "label": "立即播放"},
                {"ref": "e2", "tag": "a", "label": "第1集"},
            ],
            **obs,
        },
        "site_obs": None,
    }
    return base


class TestExtract:
    def test_只抽需人工的分支(self):
        arch = _archive([
            {"url": "a.com", "branch": "unresolved", "evidence": "x"},
            {"url": "b.com", "branch": "trailer_suspect", "evidence": "y"},
            {"url": "c.com", "branch": "no_play_control", "evidence": "z"},
            {"url": "d.com", "branch": None, "evidence": "ok"},
        ])
        items = extract_items(arch)
        assert [i.url for i in items] == ["a.com", "b.com"]

    def test_真负样本不进队列(self):
        """代码已给出确定性结论的，复核它们属于抽检，不是本队列的事。"""
        assert "no_play_control" not in REVIEW_BRANCHES
        assert "component_unverified" not in REVIEW_BRANCHES

    def test_带齐复核所需上下文(self):
        arch = _archive(
            [{"url": "https://tv.sohu.com/kan", "branch": "unresolved",
              "evidence": "未见 <video>（iframe=3）", "reached_play_page": True}],
            [_visit("https://tv.sohu.com/kan")],
        )
        it = extract_items(arch)[0]
        assert it.page_title == "功夫 - 搜狐视频"
        assert it.video_tag_count == 1
        assert it.iframe_count == 3
        assert it.elements and it.elements[0]["label"] == "立即播放"
        assert "iframe=3" in it.evidence
        assert it.reached_play_page

    def test_按landed_url关联观察(self):
        """点击后落地 URL 变了，观察得跟着落地那份，不是候选那份。"""
        arch = _archive(
            [{"url": "https://a.com/x", "branch": "unresolved", "evidence": "e"}],
            [_visit("https://a.com/x", landed="https://a.com/play/1")],
        )
        it = extract_items(arch)[0]
        assert it.landed_url == "https://a.com/play/1"

    def test_找不到观察时不崩(self):
        arch = _archive([{"url": "z.com", "branch": "unresolved", "evidence": "e"}])
        it = extract_items(arch)[0]
        assert it.page_title == "" and it.video_tag_count == 0

    def test_元素样本有上限(self):
        arch = _archive(
            [{"url": "u.com", "branch": "unresolved", "evidence": "e"}],
            [_visit("u.com", interactive_elements=[
                {"ref": f"e{i}", "tag": "a", "label": f"L{i}"} for i in range(200)
            ])],
        )
        assert len(extract_items(arch)[0].elements) == 25

    def test_带媒体探针信号(self):
        """阶段 1：media_signal / paywall_type / screenshots 带进队列供复核
        （来源 outcome/obs 可能尚未写入，缺失一律空串/空元组，不崩）。"""
        arch = _archive(
            [{"url": "u.com", "branch": "player_unverified", "evidence": "e",
              "question": "player.ok",
              "media_signal": "duration=0",
              "paywall_type": "preview",
              "screenshots": ["s3://bucket/u.png"]}],
            [_visit("u.com")],
        )
        it = extract_items(arch)[0]
        assert it.media_signal == "duration=0"
        assert it.paywall_type == "preview"
        assert it.screenshots == ("s3://bucket/u.png",)

    def test_缺探针字段不崩(self):
        arch = _archive([{"url": "u.com", "branch": "unresolved", "evidence": "e"}])
        it = extract_items(arch)[0]
        assert it.media_signal == ""
        assert it.paywall_type == ""
        assert it.screenshots == ()


class TestApplyVerdicts:
    def test_确认成功改为成功样本(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved",
                          "question": "player.ok",
                          "evidence": "e", "reached_play_page": True}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "confirm_success",
                                                  "reviewed_by": "张"}})
        rec = out["outcomes"][0]
        assert rec["branch"] is None
        assert rec["source"] == "human"
        assert rec["is_negative_sample"] is False
        assert st["confirm_success"] == 1

    def test_保留代码原始判定(self):
        """覆盖掉就永远测不出"人工与代码的分歧率"。"""
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}])
        out, _ = apply_verdicts(arch, {"a.com": {"verdict": "confirm_success"}})
        assert out["outcomes"][0]["branch_before_review"] == "unresolved"

    def test_确认失败按是否到达播放页分岔(self):
        arch = _archive([
            {"url": "reached.com", "branch": "unresolved", "evidence": "e",
             "question": "player.ok", "reached_play_page": True},
            {"url": "site.com", "branch": "unresolved", "evidence": "e",
             "question": "site.find_play_control", "reached_play_page": False},
        ])
        out, _ = apply_verdicts(arch, {
            "reached.com": {"verdict": "confirm_negative"},
            "site.com": {"verdict": "confirm_negative"},
        })
        assert out["outcomes"][0]["branch"] == "component_unverified"
        assert out["outcomes"][1]["branch"] == "no_play_control"

    def test_裁定为预告片(self):
        arch = _archive([{"url": "a.com", "branch": "trailer_suspect",
                          "question": "site.find_play_control",
                          "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "trailer"}})
        assert out["outcomes"][0]["branch"] == "trailer_only"
        assert st["trailer"] == 1

    def test_待看与跳过不动分支(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "need_browser"}})
        assert out["outcomes"][0]["branch"] == "unresolved"
        assert out["outcomes"][0]["review_pending"] is True
        assert st["skipped"] == 1

    def test_未裁定保持原样(self):
        """不默认成功也不默认丢弃。"""
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}])
        out, st = apply_verdicts(arch, {})
        assert out["outcomes"][0] == arch["outcomes"][0]
        assert st["unreviewed"] == 1

    def test_未知裁定显式抛(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}])
        with pytest.raises(ReviewQueueError, match="未知裁定"):
            apply_verdicts(arch, {"a.com": {"verdict": "looks_good_to_me"}})

    def test_记复核人与备注(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}])
        out, _ = apply_verdicts(arch, {"a.com": {
            "verdict": "confirm_success", "reviewed_by": "李",
            "note": "播放器确实能播"}})
        rec = out["outcomes"][0]
        assert rec["reviewed_by"] == "李"
        assert rec["review_note"] == "播放器确实能播"

    def test_真负样本不被回填覆盖(self):
        """误填一个 url 就把 no_play_control 改成成功 = 静默毁掉负样本池。"""
        arch = _archive([{"url": "a.com", "branch": "no_play_control", "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "confirm_success"}})
        assert out["outcomes"][0]["branch"] == "no_play_control"
        assert st["ignored"] == 1
        assert st["confirm_success"] == 0

    def test_成功样本不被回填改写(self):
        arch = _archive([{"url": "a.com", "branch": None, "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "confirm_negative"}})
        assert out["outcomes"][0]["branch"] is None
        assert st["ignored"] == 1

    def test_verdicts枚举是闭合的(self):
        assert set(VERDICTS) == {"confirm_success", "confirm_negative",
                                 "confirm_not_play_site", "confirm_paywall",
                                 "confirm_login", "confirm_blocked",
                                 "confirm_broken", "confirm_partial_free",
                                 "trailer", "need_browser", "skip"}

    def test_不是播放站与确认看不了是两个分支(self):
        """**这一支存在的理由**。导航站首页（hao123.com）点了导航项也是
        「到了播放页」，若按 ``confirm_negative`` 的 ``reached`` 分岔就会被
        贴成 ``component_unverified``——语义相反，且它是真负样本会进池。"""
        arch = _archive([
            {"url": "hao123.com", "branch": "unresolved", "evidence": "e",
             "question": "select.play_sites", "reached_play_page": True},
            {"url": "iqiyi.com", "branch": "unresolved", "evidence": "e",
             "question": "player.ok", "reached_play_page": True},
        ])
        out, st = apply_verdicts(arch, {
            "hao123.com": {"verdict": "confirm_not_play_site"},
            "iqiyi.com": {"verdict": "confirm_negative"},
        })
        assert out["outcomes"][0]["branch"] == "not_play_site"
        assert out["outcomes"][0]["is_negative_sample"] is True
        assert out["outcomes"][0]["source"] == "human"
        assert out["outcomes"][1]["branch"] == "component_unverified"
        assert st["confirm_not_play_site"] == 1

    def test_不是播放站是真负样本(self):
        """``not_play_site`` 不在 ``NON_SAMPLE_BRANCHES`` 里，所以这一档
        入池。守卫写死，避免将来有人把它挪进「没判出来」那三支。"""
        from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES
        assert "not_play_site" not in NON_SAMPLE_BRANCHES

    def test_付费墙单独成支(self):
        """优酷《功夫》页**有控件有播放器**，只卡在「立即购买/升SVIP」。
        贴 ``component_unverified`` 会说成「控件没渲染」，贴
        ``not_play_site`` 更是反的——优酷**就是**《功夫》的可观看站。"""
        from trajectory_pipeline.executor.branches import NON_SAMPLE_BRANCHES
        arch = _archive([
            {"url": "youku.com", "branch": "unresolved", "evidence": "e",
             "question": "player.ok", "reached_play_page": True},
        ])
        out, st = apply_verdicts(
            arch, {"youku.com": {"verdict": "confirm_paywall"}})
        rec = out["outcomes"][0]
        assert rec["branch"] == "subscription_wall"
        assert rec["is_negative_sample"] is True
        assert rec["source"] == "human"
        assert st["confirm_paywall"] == 1
        assert "subscription_wall" not in NON_SAMPLE_BRANCHES

    def test_付费墙分支名在注册表里(self):
        """分支 id 必须与 :data:`branches.BRANCH_LABELS` 一致，否则
        ``report`` 渲染成空白、``ledger.by_branch`` 与报表对不上。"""
        from trajectory_pipeline.executor.branches import BRANCH_LABELS
        assert "subscription_wall" in BRANCH_LABELS

    # ── 2026-10-12 阶段 1：②/④ payload 细分的人工裁定 ──────────────
    def test_确认登录墙落login_required(self):
        """② 的细分：登录后可免费看，agent 无登录态。归属 site.is_reachable。"""
        arch = _archive([{"url": "a.com", "branch": "unresolved",
                          "question": "site.is_reachable", "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "confirm_login"}})
        rec = out["outcomes"][0]
        assert rec["branch"] == "login_required"
        assert rec["is_negative_sample"] is True
        assert rec["source"] == "human"
        assert st["confirm_login"] == 1

    def test_确认登录墙跨判断点不贴(self):
        """④ 的 outcome 上裁「登录墙」不贴 branch——那是②的结论。"""
        arch = _archive([{"url": "a.com", "branch": "player_unverified",
                          "question": "player.ok", "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "confirm_login"}})
        rec = out["outcomes"][0]
        assert rec["branch"] == "player_unverified"
        assert rec["site_verdict"] == "login_required"
        assert st["off_question"] == 1
        assert st["confirm_login"] == 1

    def test_确认环境阻断落blocked(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved",
                          "question": "site.is_reachable", "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "confirm_blocked"}})
        rec = out["outcomes"][0]
        assert rec["branch"] == "blocked"
        assert rec["is_negative_sample"] is True
        assert st["confirm_blocked"] == 1

    def test_确认内容损坏落broken(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved",
                          "question": "site.is_reachable", "evidence": "e"}])
        out, st = apply_verdicts(arch, {"a.com": {"verdict": "confirm_broken"}})
        rec = out["outcomes"][0]
        assert rec["branch"] == "broken"
        assert rec["is_negative_sample"] is True
        assert st["confirm_broken"] == 1

    def test_确认部分免费落partial_free(self):
        """④ 的细分：有免费部分（试看/预告），完整内容需付费。"""
        arch = _archive([{"url": "a.com", "branch": "player_unverified",
                          "question": "player.ok", "evidence": "e"}])
        out, st = apply_verdicts(
            arch, {"a.com": {"verdict": "confirm_partial_free"}})
        rec = out["outcomes"][0]
        assert rec["branch"] == "partial_free"
        assert rec["is_negative_sample"] is True
        assert st["confirm_partial_free"] == 1

    def test_确认部分免费跨判断点降级(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved",
                          "question": "site.is_reachable", "evidence": "e"}])
        out, _ = apply_verdicts(
            arch, {"a.com": {"verdict": "confirm_partial_free"}})
        rec = out["outcomes"][0]
        assert rec["branch"] == "unresolved"
        assert rec["site_verdict"] == "partial_free"

    def test_新增细分裁定全落在负样本池(self):
        """四个新裁定都要入池（描述带「负样本」），否则人工确认的
        负样本会静默丢出统计与训练信号。"""
        for v in ("confirm_login", "confirm_blocked", "confirm_broken",
                  "confirm_partial_free"):
            assert "负样本" in VERDICTS[v], v

    def test_新分支名在注册表与归属表里(self):
        """分支 id 必须登记在 BRANCH_LABELS；归属必须由 question_of_branch
        给出（登录/阻断/损坏 → site.is_reachable，部分免费 → player.ok）。"""
        from trajectory_pipeline.executor.branches import BRANCH_LABELS
        from trajectory_pipeline.perception.questions import question_of_branch
        assert {"login_required", "blocked", "broken",
                "partial_free"} <= set(BRANCH_LABELS)
        assert question_of_branch("login_required") == "site.is_reachable"
        assert question_of_branch("blocked") == "site.is_reachable"
        assert question_of_branch("broken") == "site.is_reachable"
        assert question_of_branch("partial_free") == "player.ok"


class Test跨判断点:
    """**B 口径**：人工给的是**站点级**结论，outcome 是**决策单元级**的。

    跨判断点贴分支会造出决策树里不存在的 ``(question, branch)`` 组合，
    且破坏是静默的——实测 14 条触发 15 条 D-5 一票否决，
    门禁 ``test_d5_scorer::Test同源`` 当场红。
    """

    def test_归属不对就不动branch(self):
        arch = _archive([{"url": "youku.com", "branch": "unresolved",
                          "question": "site.find_play_control",
                          "evidence": "e"}])
        out, st = apply_verdicts(arch, {"youku.com": {"verdict": "confirm_paywall"}})
        rec = out["outcomes"][0]
        assert rec["branch"] == "unresolved"            # ← 分支没动
        assert rec["site_verdict"] == "subscription_wall"
        assert "player.ok" in rec["site_verdict_mismatch"]
        assert "site.find_play_control" in rec["site_verdict_mismatch"]
        assert st["off_question"] == 1
        assert st["confirm_paywall"] == 1

    def test_人工结论仍然留得住可统计(self):
        """不改 branch 不等于丢掉结论——``review_verdict`` 照记。"""
        arch = _archive([{"url": "hao123.com", "branch": "unresolved",
                          "question": "site.is_reachable", "evidence": "e"}])
        out, _ = apply_verdicts(
            arch, {"hao123.com": {"verdict": "confirm_not_play_site"}})
        rec = out["outcomes"][0]
        assert rec["review_verdict"] == "confirm_not_play_site"
        assert rec["site_verdict"] == "not_play_site"
        assert rec["branch_before_review"] == "unresolved"

    def test_归属对了就照贴(self):
        arch = _archive([{"url": "youku.com", "branch": "unresolved",
                          "question": "player.ok", "evidence": "e"}])
        out, st = apply_verdicts(arch, {"youku.com": {"verdict": "confirm_paywall"}})
        rec = out["outcomes"][0]
        assert rec["branch"] == "subscription_wall"
        assert "site_verdict" not in rec
        assert st["off_question"] == 0

    def test_成功也核归属只能落player_ok(self):
        """**曾经写错的一条**：以为「成功」不归属任何判断点，于是无条件放行。
        实测被 D-5 否掉 15 条——``branch=None`` 的含义是「这次访问成功了」，
        而 outcome 记的是②/③ 就说明控制流在③停了（没走到④）。
        人确认「这站能看」给出的是「**代码漏判了正样本**」，不是「访问成功」。
        """
        off = _archive([{"url": "a.com", "branch": "unresolved",
                         "question": "site.is_reachable", "evidence": "e"}])
        rec = apply_verdicts(off, {"a.com": {"verdict": "confirm_success"}})[0][
            "outcomes"][0]
        assert rec["branch"] == "unresolved"
        assert rec["site_verdict"] == "success"

        on = _archive([{"url": "a.com", "branch": "unresolved",
                        "question": "player.ok", "evidence": "e"}])
        out, st = apply_verdicts(on, {"a.com": {"verdict": "confirm_success"}})
        assert out["outcomes"][0]["branch"] is None
        assert "site_verdict" not in out["outcomes"][0]
        assert st["off_question"] == 0

    def test_question为空时拒绝贴(self):
        """老存档压根没记是哪个判断点。「无法核对」不等于「可以贴」。"""
        arch = _archive([{"url": "a.com", "branch": "unresolved",
                          "question": "", "evidence": "e"}])
        out, _ = apply_verdicts(arch, {"a.com": {"verdict": "confirm_paywall"}})
        assert out["outcomes"][0]["branch"] == "unresolved"
        assert out["outcomes"][0]["site_verdict"] == "subscription_wall"


class Test同步访问成功:
    def test_确认成功翻visits的success(self):
        """outcome.branch=None 与 visits[].success 是同一件事的两个副本。
        只翻一处就留下 ``success=False 但 branch=None``——assembler 切样本
        时按 visits 判终止，于是切出来的样本带着一条「失败」outcome，
        D-5 把它读成「分支走错」（实测 15 条一票否决）。"""
        arch = _archive([{"url": "a.com", "branch": "unresolved",
                          "question": "player.ok", "evidence": "e"}],
                        [_visit("a.com")])
        assert arch["visits"][0]["success"] is False
        out, _ = apply_verdicts(arch, {"a.com": {"verdict": "confirm_success"}})
        assert out["outcomes"][0]["branch"] is None
        assert out["visits"][0]["success"] is True
        assert "review --apply" in out["visits"][0]["success_by_review"]

    def test_按landed_url也认(self):
        """outcome 的 url 记的是**结论形成处**，visit 的 url 是候选地址，
        重定向后两者不同。只按 url 匹配会一条都翻不到。"""
        arch = _archive([{"url": "landed.com", "branch": "unresolved",
                          "question": "player.ok", "evidence": "e"}],
                        [_visit("origin.com", landed="landed.com")])
        out, _ = apply_verdicts(arch, {"landed.com": {"verdict": "confirm_success"}})
        assert out["visits"][0]["success"] is True

    def test_不翻无关的visit(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved",
                          "question": "player.ok", "evidence": "e"}],
                        [_visit("a.com"), _visit("b.com")])
        out, _ = apply_verdicts(arch, {"a.com": {"verdict": "confirm_success"}})
        assert out["visits"][0]["success"] is True
        assert out["visits"][1]["success"] is False

    def test_非成功裁定不翻(self):
        arch = _archive([{"url": "a.com", "branch": "unresolved",
                          "question": "player.ok", "evidence": "e"}],
                        [_visit("a.com")])
        out, _ = apply_verdicts(arch, {"a.com": {"verdict": "confirm_paywall"}})
        assert out["visits"][0]["success"] is False


class Test载入裁定:
    """``_load_verdicts``——**静默覆盖是这里存在的唯一理由**。

    队列逐存档导出，同一站点在多份存档里各出现一次（实测 106 行只对应
    21 个唯一 URL，爱奇艺那一个 URL 占 20 行）。而 ``dict[key]=…`` 是
    后者覆盖前者：复核员的判断被下一行悄悄吃掉，回填照常成功、零报错。
    """

    @staticmethod
    def _q(tmp_path, rows):
        p = tmp_path / "q.jsonl"
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                               for r in rows) + "\n", encoding="utf-8")
        return p

    def test_同URL同裁定不算冲突(self, tmp_path):
        p = self._q(tmp_path, [
            {"url": "a.com", "verdict": "confirm_success", "reviewed_by": "张"},
            {"url": "a.com", "verdict": "confirm_success", "reviewed_by": "张"},
        ])
        verdicts, conflicts = _load_verdicts(p)
        assert conflicts == []
        assert verdicts["a.com"]["verdict"] == "confirm_success"

    def test_同URL不同裁定当场报冲突(self, tmp_path):
        """实测事故：第 2 行裁 ``confirm_success``，第 6/16/40/80 行是
        ``need_browser``——预演显示 confirm_success 落 0 条，无任何报错。"""
        p = self._q(tmp_path, [
            {"url": "a.com", "verdict": "confirm_success", "reviewed_by": "张"},
            {"url": "a.com", "verdict": "need_browser", "reviewed_by": "auto:x"},
        ])
        _verdicts, conflicts = _load_verdicts(p)
        assert len(conflicts) == 1
        assert conflicts[0][0] == "a.com"
        assert conflicts[0][1] == {"confirm_success", "need_browser"}

    def test_空裁定不参与冲突判定(self, tmp_path):
        p = self._q(tmp_path, [
            {"url": "a.com", "verdict": "confirm_success"},
            {"url": "a.com", "verdict": ""},
        ])
        _verdicts, conflicts = _load_verdicts(p)
        assert conflicts == []

    def test_未知裁定在这里就抛(self, tmp_path):
        """不能等到 :func:`apply_verdicts` 抛——那是**逐存档**抛的，
        抛之前已经有别的存档写完 ``.reviewed.json``，留下一半新一半旧。"""
        p = self._q(tmp_path, [{"url": "a.com", "verdict": "not_play_site"}])
        with pytest.raises(ReviewQueueError, match="未知裁定"):
            _load_verdicts(p)

    def test_按landed_url优先取键(self, tmp_path):
        p = self._q(tmp_path, [{"url": "u", "landed_url": "L",
                                "verdict": "skip"}])
        verdicts, _ = _load_verdicts(p)
        assert "L" in verdicts and "u" not in verdicts


class TestIO:
    def test_写出可回读(self, tmp_path):
        arch = _archive([{"url": "a.com", "branch": "unresolved", "evidence": "e"}],
                        [_visit("a.com")])
        items = extract_items(arch)
        p = tmp_path / "q.jsonl"
        assert write_queue(p, items) == 1
        row = json.loads(p.read_text(encoding="utf-8").splitlines()[0])
        assert row["verdict"] == ""
        assert row["verdict_options"] == dict(VERDICTS)
        assert row["video_tag_count"] == 1
        assert row["media_signal"] == ""
        assert row["paywall_type"] == ""
        assert row["screenshots"] == []

    def test_collect跳过非法json(self, tmp_path):
        (tmp_path / "good.json").write_text(
            json.dumps(_archive([{"url": "a", "branch": "unresolved", "evidence": "e"}])),
            encoding="utf-8")
        (tmp_path / "bad.json").write_text("{ not json", encoding="utf-8")
        assert len(collect(tmp_path)) == 1

    def test_collect跳过review文件本身(self, tmp_path):
        """否则把队列当存档重跑会自我复制。"""
        (tmp_path / "a_review.jsonl").write_text("{}\n", encoding="utf-8")
        assert collect(tmp_path) == []

    def test_目录不存在显式抛(self, tmp_path):
        with pytest.raises(ReviewQueueError, match="不是目录"):
            collect(tmp_path / "nope")

def _row(url, *, elements=0, verdict="", **over):
    row = {"url": url, "landed_url": url, "elements": elements,
           "verdict": verdict, "reviewed_by": "", "note": ""}
    row.update(over)
    return row


class Test预填:
    """按证据形态预填，**只填空行**。

    这套规则是从一批 58 条零交互元素的复核里提炼的，那批的教训
    有硬反证：58 条里 24 条 ``video_tag_count=1``（页面上有 ``<video>``
    标签——这是「它是播放页」的**正证据**）、14 条标题逐字就是
    「功夫 - 电影 - 高清完整版在线观看 - 西瓜视频」。把它们贴成
    ``not_play_site`` 就是把**采集失败**写成业务负样本，而那会直接
    进池污染它——理由通顺、零报错。
    """

    def test_零元素进need_browser不是负分支(self):
        """``need_browser`` 的含义是「**暂不裁定**」，不是「这里看不了」。

        它与四个会落分支的裁定（``confirm_negative`` /
        ``confirm_not_play_site`` / ``confirm_paywall`` / ``trailer``）
        的区别是**唯一要紧的**区别：前三个直接进负样本池，池被污染
        之后没人会回头查是哪一批进去的。
        """
        rows = [_row("https://www.iqiyi.com/v_19rrk40ajc.html")]
        prefill(rows)
        assert rows[0]["verdict"] == "need_browser"
        landing = {v for v in VERDICTS if "负样本" in VERDICTS[v]}
        assert landing, "VERDICTS 应当有落负样本池的裁定作为对照"
        assert rows[0]["verdict"] not in landing

    def test_门户站进not_play_site(self):
        """域名本身就是结论，与采没采到元素无关。"""
        rows = [_row("https://www.hao123.com/", elements=0)]
        prefill(rows)
        assert rows[0]["verdict"] == "confirm_not_play_site"

    def test_有元素一个字都不改(self):
        rows = [_row("https://www.iqiyi.com/v_x.html", elements=30)]
        prefill(rows)
        assert rows[0]["verdict"] == "" and rows[0]["note"] == ""

    def test_不覆盖人的裁定(self):
        rows = [_row("https://a.com", verdict="confirm_success")]
        stats = prefill(rows)
        assert rows[0]["verdict"] == "confirm_success"
        assert rows[0]["reviewed_by"] == ""
        assert stats["（已填，未动）"] == 1

    def test_来源标auto不写人名(self):
        """复核档是人工裁定的正式载体。记成人名等于**伪造
        「人看过证据」**，而这批行一个都没人看过。"""
        rows = [_row("https://www.hao123.com/", elements=0),
                _row("https://www.iqiyi.com/v_x.html", elements=0)]
        prefill(rows)
        assert rows[0]["reviewed_by"] == AUTO_BY_PORTAL
        assert rows[1]["reviewed_by"] == AUTO_BY_INSUFFICIENT
        for r in rows:
            assert not any(ch.isalpha() and ord(ch) < 128 and ch.isupper()
                           for ch in r["reviewed_by"].split(":")[-1])


class Test同URL收敛:
    """同 URL 多行必须收敛成一个裁定。

    实测某个《功夫》页 8 行里 1 行零元素（被填成 ``need_browser``）、
    7 行有元素（证据指向付费墙）。裁定按 URL 应用，两档并存就是
    **后一行静默吃掉前一行**，而 ``--apply`` 照常成功、一行报错都没有。
    """

    def test_结论档压过没证据档(self):
        url = "https://www.iqiyi.com/v_19rrk40ajc.html"
        rows = [_row(url, elements=0, verdict="need_browser"),
                _row(url, elements=40, verdict="confirm_paywall")]
        assert settle_same_site(rows) == 1
        assert {r["verdict"] for r in rows} == {"confirm_paywall"}

    def test_反向顺序同样收敛(self):
        """判据不是「哪个在后」——那是文件顺序的副产物。"""
        url = "https://www.iqiyi.com/v_19rrk40ajc.html"
        rows = [_row(url, elements=40, verdict="confirm_paywall"),
                _row(url, elements=0, verdict="need_browser")]
        assert settle_same_site(rows) == 1
        assert {r["verdict"] for r in rows} == {"confirm_paywall"}

    def test_两个结论档冲突时不动(self):
        """分歧必须暴露给人看（CLAUDE.md 红线）。**不替人选一个**——
        挑一个就是猜，而这条裁定决定样本进不进训练集。"""
        url = "https://a.com"
        rows = [_row(url, verdict="confirm_success"),
                _row(url, verdict="confirm_paywall")]
        assert settle_same_site(rows) == 0
        assert rows[0]["verdict"] == "confirm_success"
        assert rows[1]["verdict"] == "confirm_paywall"

    def test_两个都没证据时取任一(self):
        url = "https://a.com"
        rows = [_row(url, verdict="need_browser"),
                _row(url, verdict="skip")]
        settle_same_site(rows)
        assert len({r["verdict"] for r in rows}) == 1

    def test_不同URL互不影响(self):
        rows = [_row("https://a.com", verdict="need_browser"),
                _row("https://b.com", verdict="confirm_success")]
        assert settle_same_site(rows) == 0
        assert rows[0]["verdict"] == "need_browser"

    def test_同步那行留痕(self):
        """被改过的那行必须写清为什么变——否则复核员看到自己的
        裁定变了，却查不出是谁按什么规则改的。"""
        url = "https://a.com"
        rows = [_row(url, verdict="need_browser", note="零交互元素"),
                _row(url, verdict="confirm_success")]
        settle_same_site(rows)
        assert "同步为 confirm_success" in rows[0]["note"]
