"""批次级断言（rubric v1.3 B-1..B-5）的单元测试。

**一批 = 一个 run**（owner 2026-11-02 裁定）——所以这里测的每条断言，
对象都是**一个 run 的存档**，不是一堆 run 混起来。

覆盖：

  1. B-1 动作 schema 合规性（含 ``press key=" "`` 的回归）；
  2. B-2 检索覆盖完整性（分母不可知 / 分母为 0 / 正常读数）；
  3. B-3 语义判断一致率（无对照 ⇒ **测不成**，不是 0%）；
  4. B-4 异常处置合规率（①②③ 三条判据 + 采集侧归因）；
  5. B-5 结论错率（三态分母、未判定不进分母、未登记标签 ⇒ 测不成）；
  6. 证据锚定（:mod:`trajectory_pipeline.evaluation.observation_match`）——
     D-3 与 B-4 共用的**同一套**判据。

⚠️ **「测不成」不是「不通过」**：本文件里每条 ``STATUS_UNTESTABLE``
的用例都在钉这件事——把「判不出」读成「判错」是 fail-closed 的反面。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.evaluation import observation_match
from trajectory_pipeline.evaluation.batch import asserts
from trajectory_pipeline.evaluation.golden import alignment
from trajectory_pipeline.evaluation.scorers import thresholds


# ── helpers ────────────────────────────────────────────────────────


def _archive(**kw):
    """一份最小存档骨架。"""
    base = {
        "schema_version": 2,
        "task_id": "T001",
        "run_config": {"max_candidates": 20},
        "candidates": [{"url": "https://a.test/"}, {"url": "https://b.test/"}],
        "outcomes": [],
        "visits": [],
        "steps": [],
    }
    base.update(kw)
    return base


def _action(tool, params=None, target=None):
    act = {"tool": tool, "params": dict(params or {}), "target": target}
    return act


def _step(action, error=""):
    step = {"action": action}
    if error:
        step["error"] = error
    return step


def _obs(body="页面正文内容", elements=(), video=0, iframe=0, **kw):
    base = {
        "url": "https://a.test/",
        "page_title": "标题",
        "body_text": body,
        "truncated": False,
        "degraded": [],
        "video_tag_count": video,
        "iframe_count": iframe,
        "elements_total": len(elements),
        "interactive_elements": list(elements),
        "links": [],
    }
    base.update(kw)
    return base


def _visit(url, *, obs=None, steps=(), landed=None):
    v = {
        "url": url,
        "landed_url": landed or url,
        "steps": list(steps),
        "site_obs": obs,
        "player_obs": None,
    }
    return v


def _outcome(url, branch, evidence="", **kw):
    o = {
        "url": url,
        "site_url": url,
        "branch": branch,
        "evidence": evidence,
        "question": "site.find_play_control",
    }
    o.update(kw)
    return o


def _row(task_id="T001", run_id="r1", gold="success", code="success"):
    return {
        "task_id": task_id,
        "run_id": run_id,
        "golden": {"label": gold},
        "code": {"branch": code},
    }


# ── 1. B-1 动作 schema 合规性 ──────────────────────────────────────


class TestB1动作Schema:
    def test_合规动作_通过(self):
        archive = _archive(steps=[
            _step(_action("goto", {"url": "https://a.test/"})),
            _step(_action("click", {}, {"role": "link", "name": "线路1"})),
            _step(_action("fill", {"text": "x"}, {"tag": "input", "label": "搜索框"})),
            _step(_action("press", {"key": "Enter"})),
        ])
        r = asserts.assert_run(archive).by_clause("B-1")
        assert r.status == asserts.STATUS_PASS
        assert r.is_pass is True
        assert r.numerator == r.denominator == 4

    def test_未声明的tool_不通过(self):
        archive = _archive(steps=[_step(_action("teleport", {"url": "x"}))])
        r = asserts.assert_run(archive).by_clause("B-1")
        assert r.status == asserts.STATUS_FAIL
        assert "不在已声明动作空间" in " ".join(r.caveats)

    def test_click缺目标_不通过(self):
        archive = _archive(steps=[_step(_action("click", {}, None))])
        r = asserts.assert_run(archive).by_clause("B-1")
        assert r.status == asserts.STATUS_FAIL
        assert "缺目标" in " ".join(r.caveats)

    def test_空目标_不通过(self):
        """目标存在但两套方言键都是空 ⇒ 仍算缺。"""
        archive = _archive(steps=[_step(_action("click", {}, {"role": "", "name": ""}))])
        r = asserts.assert_run(archive).by_clause("B-1")
        assert r.status == asserts.STATUS_FAIL

    def test_press空格键_合规(self):
        """**回归**：`press` 的 ``key=" "``（空格键）是合法取值。

        早先按 ``strip()`` 判「缺参数」，实测存档里有 32 处 ``key=" "``，
        会造出 32 条**假的**不合规。
        """
        archive = _archive(steps=[_step(_action("press", {"key": " "}))])
        r = asserts.assert_run(archive).by_clause("B-1")
        assert r.status == asserts.STATUS_PASS

    def test_goto缺url_不通过(self):
        archive = _archive(steps=[_step(_action("goto", {}))])
        r = asserts.assert_run(archive).by_clause("B-1")
        assert r.status == asserts.STATUS_FAIL
        assert "缺必填参数" in " ".join(r.caveats)

    def test_goto的url只有空白_不通过(self):
        """``url`` 要求**非空白**——「  」不是地址。"""
        archive = _archive(steps=[_step(_action("goto", {"url": "   "}))])
        r = asserts.assert_run(archive).by_clause("B-1")
        assert r.status == asserts.STATUS_FAIL
        assert "只有空白" in " ".join(r.caveats)

    def test_visit里的动作也查(self):
        """分母口径 = 存档级 steps **＋** 每个 visit 的 steps。"""
        archive = _archive(
            visits=[_visit("https://a.test/", steps=[_step(_action("nope", {}))])],
        )
        r = asserts.assert_run(archive).by_clause("B-1")
        assert r.status == asserts.STATUS_FAIL
        assert r.denominator == 1

    def test_本批没有动作_测不成不是通过(self):
        """没有动作时 B-1 没有对象——**测不成，不是通过**（M-8）。

        早先只加了一句 caveat，状态仍是 ``STATUS_PASS`` ⇒ 空白存档 / 没
        跑起来的批次能拿「全部合规」出厂。
        """
        archive = _archive()
        r = asserts.assert_run(archive).by_clause("B-1")
        assert r.status == asserts.STATUS_UNTESTABLE
        assert r.is_pass is False
        assert r.denominator == 0
        assert "没有任何动作" in r.reading
        assert any("零动作不等于合规" in c for c in r.caveats)


# ── 2. B-2 检索覆盖完整性 ──────────────────────────────────────────


class TestB2检索覆盖:
    def test_run_config缺失_不通过(self):
        """分母不可知 ⇒ **不通过**（rubric B-2 明写的情形）。"""
        archive = _archive(run_config={})
        r = asserts.assert_run(archive).by_clause("B-2")
        assert r.status == asserts.STATUS_FAIL
        assert "分母不可知" in r.reading

    def test_正常_只出读数(self):
        archive = _archive(outcomes=[_outcome("https://a.test/", "no_play_control")])
        r = asserts.assert_run(archive).by_clause("B-2")
        assert r.status == asserts.STATUS_READOUT
        assert r.numerator == 1
        assert r.denominator == 2
        assert r.rate == pytest.approx(0.5)

    def test_没有候选_分母为0_不是0(self):
        """0/0 **不是 0**——是「没有可判的东西」。"""
        archive = _archive(candidates=[])
        r = asserts.assert_run(archive).by_clause("B-2")
        assert r.status == asserts.STATUS_READOUT
        assert r.denominator == 0
        assert r.rate is None

    def test_结构性剔除不可复核_有告警(self):
        archive = _archive(candidate_filter={"dedup_removed": 3})
        r = asserts.assert_run(archive).by_clause("B-2")
        assert any("不可复核" in c for c in r.caveats)

    def test_分母取候选数与上限的较小者(self):
        """候选数超过 ``max_candidates`` 时**不把分母撑大**。"""
        archive = _archive(
            run_config={"max_candidates": 2},
            candidates=[{"url": f"https://x{i}.test/"} for i in range(5)],
        )
        r = asserts.assert_run(archive).by_clause("B-2")
        assert r.denominator == 2

    def test_候选超上限时比率不超100(self):
        """分子只在分母覆盖的候选里数（M-11）。

        早先分子统计**全部**候选、分母取 ``min(候选数, cap)`` ⇒ 候选
        超过上限时可以算出 >100% 的覆盖率。
        """
        archive = _archive(
            run_config={"max_candidates": 2},
            candidates=[{"url": f"https://x{i}.test/"} for i in range(5)],
            outcomes=[
                _outcome(f"https://x{i}.test/", "no_play_control")
                for i in range(5)  # 5 条都有处置，但分母只有 2
            ],
        )
        r = asserts.assert_run(archive).by_clause("B-2")
        assert r.denominator == 2
        assert r.numerator == 2
        assert r.rate == pytest.approx(1.0)
        assert any("超过 run_config.max_candidates" in c for c in r.caveats)


# ── 3. B-3 语义判断一致率 ──────────────────────────────────────────


class TestB3语义一致率:
    def test_没有对照_测不成(self):
        """**不是 0%**——分母不存在就是「测不成」。"""
        r = asserts.assert_run(_archive()).by_clause("B-3")
        assert r.status == asserts.STATUS_UNTESTABLE
        assert r.is_pass is False
        assert r.rate is None

    def test_过线_通过(self):
        line = thresholds.value("d3_batch_agreement_min")
        pairs = [("playable", "playable")] * 10
        r = asserts.assert_run(_archive(), d3_pairs=pairs).by_clause("B-3")
        assert r.status == asserts.STATUS_PASS
        assert r.rate == 1.0 >= line

    def test_不过线_不通过(self):
        """8 / 10 = 80% < 领证线 85% ⇒ 不通过。"""
        r = asserts.assert_run(
            _archive(), d3_pairs=[("playable", "playable")] * 8
            + [("playable", "review")] * 2
        ).by_clause("B-3")
        assert r.status == asserts.STATUS_FAIL
        assert r.rate == pytest.approx(0.8)

    def test_读数带线的口径(self):
        r = asserts.assert_run(
            _archive(), d3_pairs=[("playable", "playable")]
        ).by_clause("B-3")
        assert any(thresholds.scope_text("d3_batch_agreement_min") in c for c in r.caveats)


# ── 4. B-4 异常处置合规率 ──────────────────────────────────────────


class TestB4异常处置:
    def test_合规_计入分子(self):
        """①记成分支 ②转向下一站 ③理由锚定观察——三条都成立。"""
        archive = _archive(
            visits=[
                _visit("https://a.test/",
                       obs=_obs(body="页面显示需要登录才能观看"),
                       steps=[_step(_action("goto", {"url": "https://a.test/"}))]),
                _visit("https://b.test/", obs=_obs(body="正常页面内容")),
            ],
            outcomes=[
                _outcome("https://a.test/", "login_required",
                         evidence="页面显示需要登录才能观看"),
                _outcome("https://b.test/", "not_play_site", evidence="页面是导航页"),
            ],
        )
        r = asserts.assert_run(archive).by_clause("B-4")
        assert r.status == asserts.STATUS_READOUT
        assert r.numerator == 1
        assert r.denominator == 1
        assert r.rate == 1.0

    def test_异常未记账_不合规(self):
        """**静默丢弃**：有异常信号（动作报错）却连分支都没记。"""
        archive = _archive(
            visits=[
                _visit("https://a.test/", obs=_obs(),
                       steps=[_step(_action("goto", {"url": "https://a.test/"}),
                                    error="连接超时")]),
            ],
            outcomes=[],
        )
        r = asserts.assert_run(archive).by_clause("B-4")
        assert r.denominator == 1
        assert r.numerator == 0
        assert any("① 异常未记账" in c for c in r.caveats)

    def test_空观察_视为异常信号_进分母(self):
        """空壳观察**不是**「有观察、无异常」（M-9）。

        ``{"body_text": "", "interactive_elements": []}`` 是 truthy，旧判据
        ``not observations`` 判 False ⇒ 采集降级悄悄不进分母。
        """
        archive = _archive(
            visits=[
                _visit("https://a.test/",
                       obs=_obs(body="", elements=(), degraded=["network_error"]),
                       steps=[_step(_action("goto", {"url": "https://a.test/"}))]),
            ],
            outcomes=[],  # 连分支都没记 ⇒ 静默丢弃
        )
        r = asserts.assert_run(archive).by_clause("B-4")
        assert r.denominator == 1
        assert r.numerator == 0
        assert any("异常信号 = 观察为空/降级" in c for c in r.caveats)

    def test_同一URL重复_不合规(self):
        """② 异常后在同一 URL 上重复无效动作。"""
        archive = _archive(
            visits=[
                _visit("https://a.test/", obs=_obs(body="需要登录"),
                       steps=[_step(_action("goto", {"url": "https://a.test/"}))]),
                _visit("https://a.test/", obs=_obs(body="需要登录"),
                       steps=[_step(_action("goto", {"url": "https://a.test/"}))]),
            ],
            outcomes=[_outcome("https://a.test/", "login_required", evidence="需要登录")],
        )
        r = asserts.assert_run(archive).by_clause("B-4")
        assert any("② 异常后在同一 URL 上重复动作" in c for c in r.caveats)

    def test_理由无锚定_观察完整_责任在理由侧(self):
        """③ 理由没引用观察，且观察**完整** ⇒ 是理由自己的问题。"""
        archive = _archive(
            visits=[_visit("https://a.test/", obs=_obs(body="页面显示需要登录"))],
            outcomes=[_outcome("https://a.test/", "login_required", evidence="感觉不太行")],
        )
        r = asserts.assert_run(archive).by_clause("B-4")
        assert any("③ 止损理由无观察锚定" in c for c in r.caveats)

    def test_理由无锚定_观察为空_责任在采集侧(self):
        """观察**压根是空的** ⇒ 「引用观察里的内容」无从谈起，责任在采集侧。"""
        archive = _archive(
            visits=[_visit("https://a.test/",
                           obs=_obs(body="", elements=(), degraded=["network_error"]))],
            outcomes=[_outcome("https://a.test/", "login_required", evidence="感觉不太行")],
        )
        r = asserts.assert_run(archive).by_clause("B-4")
        assert any("责任在采集侧" in c for c in r.caveats)

    def test_观察被截断_责任在采集侧(self):
        archive = _archive(
            visits=[_visit("https://a.test/",
                           obs=_obs(body="页面显示需要登录", truncated=True))],
            outcomes=[_outcome("https://a.test/", "login_required", evidence="感觉不太行")],
        )
        r = asserts.assert_run(archive).by_clause("B-4")
        assert any("责任在采集侧" in c for c in r.caveats)

    def test_没有异常访问_分母为0(self):
        """0/0 **不是 100%**。"""
        archive = _archive(
            visits=[_visit("https://a.test/", obs=_obs())],
            outcomes=[_outcome("https://a.test/", "not_play_site", evidence="导航页")],
        )
        r = asserts.assert_run(archive).by_clause("B-4")
        assert r.denominator == 0
        assert r.rate is None

    def test_明细截断要留痕(self):
        """列不全时必须说「还有 N 条」——否则「20」会被读成「全部」。"""
        n = asserts.MAX_CAVEATS + 7
        visits = [
            _visit(f"https://s{i}.test/", obs=_obs(body="内容"),
                   steps=[_step(_action("goto", {"url": f"https://s{i}.test/"}),
                                error="连接超时")])
            for i in range(n)
        ]
        archive = _archive(visits=visits, outcomes=[])
        r = asserts.assert_run(archive).by_clause("B-4")
        assert r.denominator == n
        assert r.numerator == 0
        assert any("还有 7 条未列" in c for c in r.caveats)

    def test_正常负结论不进分母(self):
        """``not_play_site`` 是**正常负结论**，不是异常——不进分母。"""
        archive = _archive(
            visits=[_visit("https://a.test/", obs=_obs())],
            outcomes=[_outcome("https://a.test/", "not_play_site", evidence="导航页")],
        )
        r = asserts.assert_run(archive).by_clause("B-4")
        assert r.denominator == 0


# ── 5. B-5 结论错率 ────────────────────────────────────────────────


class TestB5结论错率:
    def test_没有行_测不成(self):
        r = asserts.assert_run(_archive()).by_clause("B-5")
        assert r.status == asserts.STATUS_UNTESTABLE
        assert r.is_pass is False

    def test_分母只进两侧都有结论的行(self):
        rows = [
            _row(gold="success", code="success"),          # agree
            _row(gold="success", code="no_play_control"),  # disagree
            _row(gold="unresolved", code="unresolved"),    # undetermined（两侧都没结论）
        ]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        assert r.status == asserts.STATUS_READOUT
        assert r.denominator == 2
        assert r.numerator == 1
        assert r.rate == pytest.approx(0.5)

    def test_未判定不进分母(self):
        rows = [_row(gold="unresolved", code="unresolved")]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        assert r.status == asserts.STATUS_UNTESTABLE
        assert r.denominator == 0

    def test_标签未登记_测不成(self):
        """fail-closed：标签不在等价类表里 ⇒ **判不出**，不猜。"""
        rows = [_row(gold="某个没登记的标签", code="success")]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        assert r.status == asserts.STATUS_UNTESTABLE
        assert "未登记" in r.reading

    def test_合并记账与表不一致_测不成(self):
        """偷偷合并等价类（加了标签不补记账）⇒ **读数不成立**。

        合并等价类 = 放水（方案 16 D-C4 原话）：D-10 口径 C 再也分不出
        被并进来的那些标签 —— **分母语义被悄悄改宽**，而「丢了什么细分」
        没有记在任何地方（C-13 的记账面）。

        守卫与上面的「标签未登记 ⇒ 测不成」是**同一类 fail-closed**：
        表缺标签 vs 记账没跟上，都是「口径不可信 ⇒ 不出读数」。
        """
        original = dict(alignment.EQUIV_CLASS)
        try:
            alignment.EQUIV_CLASS["_偷偷并进来的_"] = "SUCCESS"
            rows = [_row(gold="success", code="success")]
            r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
            assert r.status == asserts.STATUS_UNTESTABLE
            assert r.is_pass is False
            assert "合并记账" in r.reading
            assert any("_偷偷并进来的_" in c for c in r.caveats)
            # 问题清单要指出**该补记账**，而不只是「有问题」
            assert any("先补记账" in c for c in r.caveats)
        finally:
            alignment.EQUIV_CLASS.clear()
            alignment.EQUIV_CLASS.update(original)
        # 复原后恢复「只出读数」
        rows = [_row(gold="success", code="success")]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        assert r.status == asserts.STATUS_READOUT

    def test_语义分离护栏破了_测不成(self):
        """把**刻意不许合并**的两组并进一类 ⇒ 读数不成立（**哪怕记账补齐**）。

        ``broken``（内容损坏：页面在但坏）vs ``unreachable_hard``
        （连页面都没到）——两种失败形态的处置完全不同（换站 vs 查环境）。
        并进一类 ⇒ 口径 C 再也分不出二者。

        ⚠️ 与上一条的区别：**注记补齐了照样红** —— 「记账一致」只说明
        「改动被记了账」，不说明「这组合并本身是对的」。⇒ 两道护栏都要过。
        """
        original = dict(alignment.EQUIV_CLASS)
        original_notes = dict(alignment.EQUIV_CLASS_NOTES)
        try:
            # 把 unreachable_hard 并进 BROKEN_CONTENT，**并把注记补齐**
            # —— 只骗过「合并记账」那道护栏。
            # （BROKEN_CONTENT 原本是**单标签类**，按模块规则「单标签类不需要
            #   注记」，所以合并后必须**新写**一张，不是改旧的。）
            alignment.EQUIV_CLASS["unreachable_hard"] = "BROKEN_CONTENT"
            alignment.EQUIV_CLASS_NOTES["BROKEN_CONTENT"] = {
                "canonical": "broken",
                "merged": ("unreachable_hard",),
                "detail_lost": "内容损坏 vs 连页面都没到",
                "reason": "（测试构造的偷合并）",
            }
            # 记账护栏此时是**干净的** —— 正是这条用例要验的分离点
            assert alignment.validate_merge_notes() == []
            # 语义分离护栏抓到了
            assert alignment.validate_split_guards()

            rows = [_row(gold="success", code="success")]
            r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
            assert r.status == asserts.STATUS_UNTESTABLE
            assert r.is_pass is False
            assert "语义分离" in r.reading
            assert any("unreachable_hard" in c for c in r.caveats)
            assert any("语义分离护栏" in c for c in r.caveats)
        finally:
            alignment.EQUIV_CLASS.clear()
            alignment.EQUIV_CLASS.update(original)
            alignment.EQUIV_CLASS_NOTES.clear()
            alignment.EQUIV_CLASS_NOTES.update(original_notes)
        # 复原后恢复「只出读数」
        rows = [_row(gold="success", code="success")]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        assert r.status == asserts.STATUS_READOUT

    def test_只出读数_不出合格与否(self):
        """rubric v1.3 未给 B-5 定线 ⇒ 只报数，不报合格 / 不合格。"""
        rows = [_row(gold="success", code="success")]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        assert r.status == asserts.STATUS_READOUT

    def test_合并代价清单只报本批碰到的等价类(self):
        """**C-13 的产出面** —— 合并的代价要在读数里看得见。

        口径 C 把几个标签并进一类之后，这一类里哪些区分**已经分不出来**
        了 —— 不报的话，读者只看到「一致率 92%」，看不到「这 92% 里有一
        部分是**并出来的**」。

        ⚠️ 只报**本批碰到过**的等价类：全表 7 类大半与本批无关，全列是噪声。
        """
        rows = [_row(gold="paywall", code="partial_free")]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        assert r.status == asserts.STATUS_READOUT
        assert r.denominator == 1
        # 本批碰到 PAYWALL（gold 与 code 同类）⇒ 它在清单里
        assert any(c.startswith("PAYWALL：") for c in r.caveats)
        # 本批**没碰到**的类不在清单里（SUCCESS 等）
        assert not any(c.startswith("SUCCESS：") for c in r.caveats)
        assert not any(c.startswith("NO_PLAY_RESOURCE：") for c in r.caveats)
        # 清单要有个抬头，别让读者以为那是分母口径
        assert any("合并等价类的代价" in c for c in r.caveats)

    def test_合并代价清单跟着两侧标签走(self):
        """**两侧**标签的等价类都算「本批碰到」——金标侧与代码侧都要算。"""
        rows = [_row(gold="paywall", code="success")]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        assert r.status == asserts.STATUS_READOUT
        assert r.numerator == 1          # disagree
        assert any(c.startswith("PAYWALL：") for c in r.caveats)
        assert any(c.startswith("SUCCESS：") for c in r.caveats)

    def test_清单里的细分就是注记里的detail_lost(self):
        """清单**派生**自注记，不是手抄的第二份（一个出处）。"""
        rows = [_row(gold="paywall", code="paywall")]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        detail = alignment.EQUIV_CLASS_NOTES["PAYWALL"]["detail_lost"]
        assert any(c == f"PAYWALL：{detail}" for c in r.caveats)

    def test_空branch是成功_不是missing(self):
        """代码侧**空 branch = 成功**（M-7）——成功行必须进分母。

        仓库口径：成功跑完的存档不记分支名（``code.get("branch") or
        SUCCESS_LABEL``，见 builder / snapshot）。早先 ``or ""`` 把成功行
        读成 missing ⇒ 判对的行被踢出分母，读数只剩失败。
        """
        rows = [_row(gold="success", code=None)]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        assert r.status == asserts.STATUS_READOUT
        assert r.denominator == 1
        assert r.numerator == 0  # agree，不是 disagree
        assert r.rate == pytest.approx(0.0)

    def test_空branch与不同金标_判不一致(self):
        """空 branch = success，与 ``no_play_control`` 金标**必须判不一致**。"""
        rows = [_row(gold="no_play_control", code=None)]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        assert r.denominator == 1
        assert r.numerator == 1
        assert r.rate == pytest.approx(1.0)

    def test_未判定理由截断要留痕(self):
        """未判定明细列不全时说「还有 N 条」——不许只给前 10 条（M-12）。"""
        rows = [_row(gold="", code=None) for _ in range(asserts.MAX_CAVEATS + 3)]
        r = asserts.assert_run(_archive(), gold_rows=rows).by_clause("B-5")
        assert r.denominator == 0
        assert any("还有 3 条未列" in c for c in r.caveats)


class Test按run挑行:
    def test_两个键都认(self):
        rows = [
            _row(task_id="T001", run_id="r1"),
            _row(task_id="T001", run_id="r2"),
            _row(task_id="T002", run_id="r1"),
        ]
        got = asserts.rows_for_run(rows, "T001", "r1")
        assert len(got) == 1
        assert got[0]["run_id"] == "r1"

    def test_行里没有run_id_挑不出来(self):
        """没记 run_id 的行**不认**——猜不得。"""
        rows = [{"task_id": "T001", "golden": {"label": "success"},
                 "code": {"branch": "success"}}]
        assert asserts.rows_for_run(rows, "T001", "r1") == []


# ── 6. 证据锚定（D-3 与 B-4 共用） ─────────────────────────────────


class Test证据锚定:
    def test_正文子串(self):
        obs = [_obs(body="页面显示需要登录才能观看")]
        assert observation_match.evidence_anchored("页面显示需要登录", obs) is True

    def test_泛泛而谈_不算锚定(self):
        obs = [_obs(body="页面显示需要登录才能观看")]
        assert observation_match.evidence_anchored("感觉不太行", obs) is False

    def test_元素名_aria面(self):
        obs = [_obs(body="内容", elements=[{"role": "link", "name": "线路1"}])]
        assert observation_match.evidence_anchored("点击了 线路1", obs) is True

    def test_元素名_obscura面(self):
        obs = [_obs(body="内容", elements=[{"tag": "a", "label": "线路1"}])]
        assert observation_match.evidence_anchored("点击了 线路1", obs) is True

    def test_计数算锚(self):
        obs = [_obs(body="内容", video=3)]
        assert observation_match.evidence_anchored("页面 video 标签 3 个", obs) is True

    def test_计数为0不算锚(self):
        """计数为 0 时「0」不构成锚——到处都有 0。"""
        obs = [_obs(body="内容", video=0)]
        assert observation_match.evidence_anchored("耗时 0 秒", obs) is False

    def test_小于4字的子串不算(self):
        obs = [_obs(body="登录")]
        assert observation_match.evidence_anchored("需要登录", obs) is False

    def test_text_anchor_报出可读片段(self):
        hit = observation_match.text_anchor("页面显示需要登录才能观看", "页面显示需要登录")
        assert hit
        assert "页面显示需要登录" in hit


# ── 7. 「一批 = 一个 run」 ──────────────────────────────────────────


class Test一批一个run:
    def test_每个run五条断言(self):
        r = asserts.assert_run(_archive(), run_id="r1", task_id="T001")
        assert [x.clause for x in r.readings] == list(asserts.CLAUSES)
        assert len(r.readings) == 5

    def test_两个run两张表(self):
        """批次级断言**不跨 run 合并**——每 run 一份。"""
        a = asserts.assert_run(_archive(), run_id="r1", task_id="T001")
        b = asserts.assert_run(_archive(), run_id="r2", task_id="T001")
        assert a.run_id == "r1" and b.run_id == "r2"
        assert a is not b
        assert a.readings is not b.readings

    def test_by_clause_条款不在表里_抛(self):
        r = asserts.assert_run(_archive(), run_id="r1")
        with pytest.raises(KeyError):
            r.by_clause("B-9")

    def test_测不成_不算通过(self):
        """``is_pass`` 对「测不成」必须是 False——缺数据不是达标。"""
        r = asserts.assert_run(_archive(), run_id="r1")
        b3 = r.by_clause("B-3")
        assert b3.status == asserts.STATUS_UNTESTABLE
        assert b3.is_pass is False
        assert "B-3" in [x.clause for x in r.untestable]
        assert "B-3" not in [x.clause for x in r.failed]

    def test_不通过的条款被单列(self):
        archive = _archive(run_config={})  # B-2 分母不可知 ⇒ 不通过
        r = asserts.assert_run(archive, run_id="r1")
        assert "B-2" in [x.clause for x in r.failed]

    def test_json与人读版都带口径(self):
        r = asserts.assert_run(_archive(), run_id="r1", task_id="T001")
        j = r.to_json()
        assert j["run_id"] == "r1"
        assert len(j["readings"]) == 5
        text = r.render_text()
        assert "B-1" in text and "B-5" in text
        assert "r1" in text

    def test_测不成时人读版不许写通过(self):
        """``UNTESTABLE`` 不是通过（M-10）——人读版必须照实说。"""
        text = asserts.assert_run(_archive(), run_id="r1").render_text()
        assert "通过（无断言不通过）" not in text
        assert "测不成不是通过" in text

    def test_只出读数时人读版不说通过(self):
        """``READOUT`` 没有合格线（M-10）——「无不通过」不等于「通过」。"""
        archive = _archive(
            steps=[_step(_action("goto", {"url": "https://a.test/"}))],
            outcomes=[_outcome("https://a.test/", "not_play_site", evidence="导航页")],
        )
        r = asserts.assert_run(
            archive,
            run_id="r1",
            d3_pairs=[("playable", "playable")] * 10,
            gold_rows=[_row(gold="success", code="success")],
        )
        text = r.render_text()
        assert "通过（无断言不通过）" not in text
        assert "只出读数不等于通过" in text

    def test_阈值线在阈值表里登记(self):
        """B-3 的线**不许**手抄字面量——只从阈值表取。"""
        assert "d3_batch_agreement_min" in thresholds.THRESHOLDS
        assert thresholds.value("d3_batch_agreement_min") == 0.85
