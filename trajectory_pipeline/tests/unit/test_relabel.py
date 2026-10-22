"""``executor.relabel`` 的测试。

盯的是**那些真的会咬人的坑**，不是「函数能不能跑」：

* 改了存档却没改池 ⇒ 门禁报「没并池」，而报告上看不出是改判写了一半；
* 改了池却没改 ledger ⇒ 分支分布与实际标签对不上；
* 归属不核 ⇒ 造出决策树里不存在的 ``(question, branch)`` 组合，
  D-5 一票否决（实测 15 条）；
* 同键只改一份 ⇒ 另一份当场报 ``negative_not_pooled``；
* 撞键时新建行 ⇒ 池里出现两条同键的行，违反去重约定。
"""

from __future__ import annotations

import json
import re

import pytest

from trajectory_pipeline.executor import cli
from trajectory_pipeline.executor import relabel as rl
from trajectory_pipeline.executor.branches import BRANCH_LABELS


# ── 夹具 ──────────────────────────────────────────────────────────


def _arch(task_id="T001", outcomes=None):
    outcomes = outcomes if outcomes is not None else []
    return {
        "task_id": task_id, "title": "功夫",
        "ledger": {"by_branch": {"subscription_wall": len(outcomes)},
                   "negative_samples": len(outcomes),
                   "missing_branches": []},
        "outcomes": outcomes,
    }


def _o(url, branch, evidence="判定正文", question="player.ok", **over):
    rec = {"url": url, "branch": branch, "evidence": evidence,
           "question": question, "source": "llm",
           "is_negative_sample": branch is not None}
    rec.update(over)
    return rec


def _write(tmp_path, arch, name="T001__r1.json"):
    p = tmp_path / name
    p.write_text(json.dumps(arch, ensure_ascii=False), encoding="utf-8")
    return p


def _pool_row(task_id="T001", url="https://v.qq.com/x.html",
              branch="subscription_wall", **over):
    row = {"task_id": task_id, "url": url, "branch": branch,
           "branch_label": BRANCH_LABELS.get(branch, ""),
           "question": "player.ok", "source": "llm", "evidence": "判定正文"}
    row.update(over)
    return row


def _write_pool(tmp_path, rows):
    """写池子。**字符串行原样落盘**（用来塞坏行）。"""
    p = tmp_path / "negative.jsonl"
    lines = [r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)
             for r in rows]
    p.write_text("".join(l + "\n" for l in lines), encoding="utf-8")
    return p


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _read_pool(path):
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append(line)
    return out


# ── 目标分支 ──────────────────────────────────────────────────────


class Test目标分支:
    def test_活跃负样本通过(self):
        ok, why = rl.check_target("blocked")
        assert ok and why == ""

    def test_未活跃分支被拒(self):
        """分支表是**封闭集**——拼错的名字放进去就是造了一个不存在的
        结论，而下游（rubric / 训练侧）都按分支名取标签。"""
        ok, why = rl.check_target("blocked_typo")
        assert not ok and "不在活跃分支表" in why

    def test_已弃用分支被拒(self):
        """``login_wall_or_blocked`` 是历史合并态，**不再产出**。
        改判成它等于把记录塞回一个没人会再生成的分支。"""
        ok, why = rl.check_target("login_wall_or_blocked")
        assert not ok and "已弃用" in why

    @pytest.mark.parametrize("branch", ["unresolved", "trailer_suspect",
                                        "player_unverified",
                                        "site_search_routed"])
    def test_反思档被拒(self, branch):
        """**它们不是负样本**（:data:`NON_SAMPLE_BRANCHES`）——改判的前提
        就是「仍是负样本」。把一条真负样本改成它们等于把它踢出池子，
        那是 ``cli downgrade`` 的活，不是本命令的。"""
        ok, why = rl.check_target(branch)
        assert not ok and "不是负样本" in why

    def test_空目标被拒(self):
        ok, why = rl.check_target("")
        assert not ok and why


# ── 归属 ──────────────────────────────────────────────────────────


class Test归属:
    def test_判断点相符放行(self):
        ok, why = rl.check_attribution(["player.ok"], "subscription_wall",
                                       "blocked")
        assert ok, why

    def test_判断点不符被拒(self):
        """``site.find_play_control`` 只产得出 ``no_play_control`` /
        ``trailer_only`` 那一支——挂一个 ``blocked`` 上去就是造出决策树
        里不存在的 ``(question, branch)`` 组合（D-5 判一票否决）。"""
        ok, why = rl.check_attribution(["site.find_play_control"],
                                       "subscription_wall", "blocked")
        assert not ok and "site.find_play_control" in why

    def test_没记判断点时按源分支产地反推放行(self):
        """实测池里 **57/181** 行没记判断点。「无法核对」≠「可以贴」，
        但源分支自己就带着产地（``unreachable_hard`` 只可能出自②）。"""
        ok, why = rl.check_attribution([], "unreachable_hard", "blocked")
        assert ok, why

    def test_没记判断点且产地无交集被拒(self):
        ok, why = rl.check_attribution([], "no_play_control",
                                       "unreachable_hard")
        assert not ok and "无交集" in why

    def test_多判断点里有一个不容就整条拒(self):
        """同一个键在几份存档里各有一条时（按键聚合），每一条的判断点
        都要容得下目标分支——任一条不容，整组就拒。"""
        ok, why = rl.check_attribution(["player.ok", "site.find_play_control"],
                                       "subscription_wall", "blocked")
        assert not ok


# ── 计划 ──────────────────────────────────────────────────────────


class Test计划:
    def _plan(self, cands, **kw):
        kw.setdefault("to_branch", "blocked")
        kw.setdefault("reason", "人工复核：地区限制不是付费墙")
        return rl.plan_relabels(cands, **kw)

    def test_理由必填(self):
        with pytest.raises(rl.RelabelError, match="--reason"):
            self._plan({}, reason="   ")

    def test_非法目标报错(self):
        with pytest.raises(rl.RelabelError):
            self._plan({}, to_branch="unresolved")

    def test_凭据理由被拒(self):
        """理由是人手输的，**凭据不得进存档、不得进池、不得进日志**。
        挂在「计划」这一步 ⇒ dry-run 也拒（dry-run 会打印它）。"""
        with pytest.raises(rl.RelabelError, match="凭据"):
            self._plan({}, reason="带凭据 Authorization: Bearer abcdefghijklmnop")

    def test_已是目标分支跳过(self):
        cands = {("T001", "u", "blocked"): rl.Candidate("T001", "u", "blocked")}
        plan = self._plan(cands)
        assert len(plan.skipped) == 1 and not plan.accepted

    def test_归属不符进rejected(self):
        cands = {("T001", "u", "subscription_wall"): rl.Candidate(
            "T001", "u", "subscription_wall", questions=["site.find_play_control"])}
        plan = self._plan(cands)
        assert len(plan.rejected) == 1 and not plan.accepted
        assert "site.find_play_control" in plan.rejected[0][1]

    def test_正常进accepted(self):
        cands = {("T001", "u", "subscription_wall"): rl.Candidate(
            "T001", "u", "subscription_wall", questions=["player.ok"])}
        plan = self._plan(cands)
        assert len(plan.accepted) == 1 and not plan.rejected

    def test_撞已有行并入(self):
        """实测（2026-10-21 人工复核）：同一个 ``(task_id, url)`` 在池里有
        **两行**、标签还**互相矛盾**——两次跑出了两个结论。改判之后两条
        都是 ``blocked`` ⇒ 池里两行同键 ⇒ 违反池的去重约定。

        ⇒ 处置是「**并入**」：保留判对了的那条，删掉贴错标签的那条，
        **不新建行**。不是「拒绝」——拒绝等于让这条错标签永远留在池里。
        """
        cands = {("T001", "u", "subscription_wall"): rl.Candidate(
            "T001", "u", "subscription_wall", questions=["player.ok"])}
        pool_keys = {("T001", "u", "subscription_wall"),
                     ("T001", "u", "blocked")}
        plan = self._plan(cands, pool_keys=pool_keys)
        assert not plan.accepted and len(plan.merged) == 1
        assert "并入" in plan.merged[0][1]

    def test_同批撞键并入(self):
        """两条候选改成同一个分支 ⇒ 新键撞上 ⇒ 后一条并入前一条。

        这不是「同 ``(task_id, url)`` 的两次跑」（那是上一条），而是
        **这一批里**两条不同的旧分支改到同一个新分支。"""
        cands = {
            ("T001", "u", "subscription_wall"): rl.Candidate(
                "T001", "u", "subscription_wall", questions=["player.ok"]),
            ("T001", "u", "partial_free"): rl.Candidate(
                "T001", "u", "partial_free", questions=["player.ok"]),
        }
        plan = self._plan(cands)
        assert len(plan.accepted) == 1 and len(plan.merged) == 1
        assert "已被另一条占用" in plan.merged[0][1]


# ── 处置 ──────────────────────────────────────────────────────────


class Test处置:
    def test_存档与池一起改(self, tmp_path):
        """**训练侧吃的是池子**（``evaluation.golden.builder`` 读池行的
        ``branch``），不是存档。只改存档等于没改；只改池等于存档与门禁
        对不上（``negative_not_pooled``）。"""
        p = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]))
        _write_pool(tmp_path, [_pool_row()])
        out = rl.relabel_to_root(tmp_path, to_branch="blocked",
                                 reason="地区限制不是付费墙", dry_run=False)
        assert out["relabelled"] == 1 and out["pool_rows"] == 1
        assert _read(p)["outcomes"][0]["branch"] == "blocked"
        row = _read_pool(tmp_path / "negative.jsonl")[0]
        assert row["branch"] == "blocked"
        assert row["branch_label"] == BRANCH_LABELS["blocked"]

    def test_留痕(self, tmp_path):
        """三样都要留：``branch_before_relabel``（原判）、evidence 追加
        （**不覆盖**原正文——覆盖掉之后就永远测不出「人工与代码的分歧
        率」了）、``source=human``（人工改过的结论要认得出来）。"""
        p = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall",
               evidence="原始判定正文")]))
        rl.relabel_to_root(tmp_path, to_branch="blocked",
                           reason="取证依据在此", dry_run=False)
        rec = _read(p)["outcomes"][0]
        assert rec["branch_before_relabel"] == "subscription_wall"
        assert rec["source"] == "human"
        assert rec["is_negative_sample"] is True
        assert "原始判定正文" in rec["evidence"]
        assert rl.RELABEL_MARK in rec["evidence"] and "取证依据在此" in rec["evidence"]

    def test_链式改判不覆盖原判(self, tmp_path):
        """``branch_before_relabel`` 记的是「这条**最初**被贴成什么」——
        A→B→C 之后它还得是 A，那才是日后要回查的东西。"""
        p = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]))
        rl.relabel_to_root(tmp_path, to_branch="blocked", reason="第一次",
                           dry_run=False)
        rl.relabel_to_root(tmp_path, to_branch="partial_free", reason="第二次",
                           dry_run=False)
        rec = _read(p)["outcomes"][0]
        assert rec["branch"] == "partial_free"
        assert rec["branch_before_relabel"] == "subscription_wall"

    def test_ledger记账且负样本计数不动(self, tmp_path):
        """``by_branch`` 数的是 outcome 条数；``negative_samples`` 数的是
        **池里的负样本**——改判前后**都是真负样本**，池的规模没变。
        把计数也改一遍就会说成「池里少了一条」。"""
        p = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall"),
            _o("https://v.qq.com/y.html", "subscription_wall")]))
        rl.relabel_to_root(tmp_path, to_branch="blocked", reason="r",
                           dry_run=False)
        led = _read(p)["ledger"]
        assert led["by_branch"]["blocked"] == 2
        assert "subscription_wall" not in led["by_branch"]   # 归零则删键
        assert led["negative_samples"] == 2               # 不动
        assert "subscription_wall" in led["missing_branches"]
        assert "blocked" not in led["missing_branches"]

    def test_dry_run不落盘(self, tmp_path):
        p = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]))
        _write_pool(tmp_path, [_pool_row()])
        before = p.read_text(encoding="utf-8")
        pool_before = (tmp_path / "negative.jsonl").read_text(encoding="utf-8")
        out = rl.relabel_to_root(tmp_path, to_branch="blocked", reason="r",
                                 dry_run=True)
        assert out["relabelled"] == 1 and out["dry_run"] is True
        assert p.read_text(encoding="utf-8") == before
        assert (tmp_path / "negative.jsonl").read_text(encoding="utf-8") == pool_before

    def test_复核档一起改(self, tmp_path):
        """复核档（``.reviewed.json``）是读侧**优先取**的那一份——
        只改原档 ⇒ 复核档里还是旧键 ⇒ 门禁当场报 ``negative_not_pooled``。

        这与 :mod:`downgrade` 的做法**不同**（它只动原档），因为降级的
        目标分支不进池、不涉及池键。
        """
        _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]),
            name="T001__a.json")
        rev = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]),
            name="T001__a.reviewed.json")
        _write_pool(tmp_path, [_pool_row()])
        rl.relabel_to_root(tmp_path, to_branch="blocked", reason="r",
                           dry_run=False)
        assert _read(rev)["outcomes"][0]["branch"] == "blocked"

    def test_非T前缀的存档也收(self, tmp_path):
        """真实目录里存档**不只 T 前缀**（E001 / E014 / P033 / TPIN02 …）。
        按 ``T*__*.json`` 通配会静默漏掉它们——漏掉的那些存档里的旧分支
        永远留在池里，而报告上看不出来。"""
        p = _write(tmp_path, _arch(task_id="E021", outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]),
            name="E021__94a1392f.json")
        _write_pool(tmp_path, [_pool_row(task_id="E021")])
        out = rl.relabel_to_root(tmp_path, to_branch="blocked", reason="r",
                                 dry_run=False)
        assert out["relabelled"] == 1
        assert _read(p)["outcomes"][0]["branch"] == "blocked"

    def test_同键跨存档一起改(self, tmp_path):
        """同一个 ``(task_id, url, branch)`` 在几份存档里各有一条时必须
        **一起改**——改一份不改另一份，另一份当场报 ``negative_not_pooled``。"""
        a = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]),
            name="T001__a.json")
        b = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]),
            name="T001__b.json")
        _write_pool(tmp_path, [_pool_row()])
        out = rl.relabel_to_root(tmp_path, to_branch="blocked", reason="r",
                                 dry_run=False)
        assert out["outcomes"] == 2
        assert _read(a)["outcomes"][0]["branch"] == "blocked"
        assert _read(b)["outcomes"][0]["branch"] == "blocked"

    def test_并入时删旧行不新建(self, tmp_path):
        """撞键 ⇒ **删掉贴错标签的那一行**，保留判对了的那条。
        不新建行——同 ``(task_id, url, branch)`` 只能有一行（池的去重约定）。"""
        p = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]))
        _write_pool(tmp_path, [
            _pool_row(branch="subscription_wall", evidence="贴错标签那次"),
            _pool_row(branch="blocked", evidence="判对了那次"),
        ])
        out = rl.relabel_to_root(tmp_path, to_branch="blocked", reason="r",
                                 dry_run=False)
        assert out["merged"] == 1 and out["pool_dropped"] == 1
        rows = _read_pool(tmp_path / "negative.jsonl")
        assert len(rows) == 1
        assert rows[0]["evidence"] == "判对了那次"      # 保留的是判对的那条
        # 存档那一侧照样改标签（否则它与池里的行对不上）
        assert _read(p)["outcomes"][0]["branch"] == "blocked"

    def test_坏行原样保留(self, tmp_path):
        """坏行可能是被人手工截断、**正等人查**的——一次改判顺手清掉
        全池的坏行是另一件事。"""
        _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]))
        pool = _write_pool(tmp_path, [
            "{坏行不是 JSON", _pool_row(),
        ])
        rl.relabel_to_root(tmp_path, to_branch="blocked", reason="r",
                           dry_run=False)
        rows = _read_pool(pool)
        assert rows[0] == "{坏行不是 JSON"
        assert rows[1]["branch"] == "blocked"

    def test_反思档不被选(self, tmp_path):
        """``unresolved`` / ``trailer_suspect`` / ``player_unverified``
        走 ``review --apply``——它们**不是负样本**，而本命令的整个前提
        就是「仍是负样本」。"""
        p = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "unresolved",
               question="player.ok")]))
        _write_pool(tmp_path, [_pool_row(branch="unresolved")])
        out = rl.relabel_to_root(tmp_path, to_branch="blocked", reason="r",
                                 dry_run=False)
        assert out["relabelled"] == 0
        assert _read(p)["outcomes"][0]["branch"] == "unresolved"

    def test_不碰成功样本(self, tmp_path):
        """``branch=None`` 是「这条判成功」，**本来就不在池里**。
        把它拉进改判就是把人判成功的记录改成负样本。"""
        p = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", None, question="player.ok")]))
        out = rl.relabel_to_root(tmp_path, to_branch="blocked", reason="r",
                                 dry_run=False)
        assert out["relabelled"] == 0
        assert _read(p)["outcomes"][0]["branch"] is None

    def test_凭据闸门(self, tmp_path):
        """凭据不得进存档、不得进池。理由是人手输的 ⇒ 闸门挂在入口。"""
        p = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]))
        before = p.read_text(encoding="utf-8")
        with pytest.raises(rl.RelabelError, match="凭据"):
            rl.relabel_to_root(
                tmp_path, to_branch="blocked",
                reason="Authorization: Bearer abcdefghijklmnopqrst",
                dry_run=False)
        assert p.read_text(encoding="utf-8") == before


# ── 选择器 ────────────────────────────────────────────────────────


class Test选择器:
    def test_按URL片段挑(self):
        cand = rl.Candidate("T001", "https://v.qq.com/x.html", "blocked")
        assert rl.select_by_url("v.qq.com")(cand)
        assert not rl.select_by_url("youku.com")(cand)

    def test_多片段是OR(self):
        cand = rl.Candidate("T001", "https://v.qq.com/x.html", "blocked")
        assert rl.select_by_url("youku.com", "v.qq.com")(cand)

    def test_按当前分支挑(self):
        cand = rl.Candidate("T001", "u", "subscription_wall")
        assert rl.select_by_branch("subscription_wall")(cand)
        assert not rl.select_by_branch("blocked")(cand)

    def test_按evidence只看第一段(self):
        """追加的说明里也会出现判据关键词——全文匹配会把自己刚改判的
        记录再改一次（与 :mod:`downgrade` 同一个坑）。"""
        cand = rl.Candidate("T001", "u", "subscription_wall",
                            evidence="原判正文\n\n[改判 subscription_wall → "
                                     "blocked] 依据是「当前设备不支持播放」")
        sel = rl.select_by_evidence([re.compile("当前设备不支持播放")])
        assert not sel(cand)

    def test_第一段命中(self):
        cand = rl.Candidate("T001", "u", "subscription_wall",
                            evidence="当前设备不支持播放，错误码 70017001.1")
        assert rl.select_by_evidence([re.compile("当前设备不支持播放")])(cand)

    def test_空判据拒(self):
        with pytest.raises(ValueError):
            rl.select_by_url()
        with pytest.raises(ValueError):
            rl.select_by_branch()
        with pytest.raises(ValueError):
            rl.select_by_evidence([])


# ── 命令行 ────────────────────────────────────────────────────────


class Test命令行:
    """**校验必须在碰盘之前**——理由漏了、目标非法、判据没给，
    都在碰目录之前就拒。抛裸 traceback 看起来像崩溃，实际只是参数写漏。"""

    def test_缺reason在碰盘之前就拒(self, tmp_path, capsys):
        rc = cli.main(["relabel", "--to", "blocked", "--url", "a.com",
                       "--out", str(tmp_path)])
        assert rc == 2
        assert "必须给 --reason" in capsys.readouterr().err

    def test_非法目标在碰盘之前就拒(self, tmp_path, capsys):
        rc = cli.main(["relabel", "--to", "unresolved", "--url", "a.com",
                       "--reason", "r", "--out", str(tmp_path)])
        assert rc == 2
        assert "不是负样本" in capsys.readouterr().err

    def test_不给判据就拒(self, tmp_path, capsys):
        """不给判据 = 全选 = 改判池里的每一条——不能默认。"""
        rc = cli.main(["relabel", "--to", "blocked", "--reason", "r",
                       "--out", str(tmp_path)])
        assert rc == 2
        assert "得给" in capsys.readouterr().err

    def test_默认dry_run(self, tmp_path, capsys):
        p = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]))
        _write_pool(tmp_path, [_pool_row()])
        before = p.read_text(encoding="utf-8")
        rc = cli.main(["relabel", "--to", "blocked", "--branch",
                       "subscription_wall", "--reason", "地区限制不是付费墙",
                       "--out", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "**将**改判" in out
        assert "dry-run" in out
        assert p.read_text(encoding="utf-8") == before

    def test_加apply才落盘(self, tmp_path, capsys):
        p = _write(tmp_path, _arch(outcomes=[
            _o("https://v.qq.com/x.html", "subscription_wall")]))
        _write_pool(tmp_path, [_pool_row()])
        rc = cli.main(["relabel", "--to", "blocked", "--branch",
                       "subscription_wall", "--reason", "地区限制不是付费墙",
                       "--apply", "--out", str(tmp_path)])
        assert rc == 0
        assert _read(p)["outcomes"][0]["branch"] == "blocked"
        assert _read_pool(tmp_path / "negative.jsonl")[0]["branch"] == "blocked"
