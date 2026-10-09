"""P1 完整性门禁的测试。

重点不是「每种坏法都能被发现」，而是**门禁自己不能骗人**。这个模块
写出来当天就自己犯了一次：``Finding.code`` 装的是 ``degraded_from`` 的 marker
（``steps``）而渲染表按本模块的码（``steps_missing``）建，归组输出里那两组
**静默消失**了——一个总是少报的门禁比没有门禁更坏，因为它会让人以为查过了。
所以测试盯的是这几条：

1. **不许静默丢码**：每条能报出来的 code 都必须在渲染输出里出现。
2. **不许两套词表漂**：本模块的 DEGRADED 码与 ``schema.split_archive`` 的
   ``degraded_from`` 标记必须一一对上（靠 :func:`degraded_marker_map`）。
3. **不许静默失配**：候选 url 与落地 url 不等时不能误报 ``outcome_missing``。
4. **不许替 assembler 做决定**：本模块不 import assembler，也不重跑感知层。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from trajectory_pipeline.assembler import schema as p2_schema
from trajectory_pipeline.executor import integrity
from trajectory_pipeline.executor.integrity import (
    REPLACEMENT_CHAR, Severity, check_archive, check_root, degraded_marker_map,
    format_report,
)


# ── 语料 ───────────────────────────────────────────────────────────

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


def _outcome(url, branch, **kw):
    base = {"url": url, "branch": branch, "branch_label": "", "evidence": "看到了播放控件",
            "question": "player.ok", "source": "rule", "fallback_used": False,
            "reached_play_page": True, "is_negative_sample": False}
    base.update(kw)
    return base


def _arc(**over):
    base = {
        "task_id": "T001", "title": "功夫", "query": "功夫 在线观看",
        "user_prompt": "有没有能看正片的", "search_url": "https://s.test",
        "search_observation": _obs("https://s.test"), "search_blocked": "",
        "steps": [], "provenance": {"genre": "喜剧"}, "perceptor": "rule",
        # 健康存档**有**运行参数快照。缺它会被 ``run_config_missing`` 判
        # DEGRADED——测试里要模拟降级就显式 pop 掉。
        "run_config": {"engine": "baidu", "max_candidates": 20,
                       "max_chars": None, "stop_after_success": 0,
                       "per_site_timeout_s": 90.0},
        "candidates": [], "visits": [], "outcomes": [], "ledger": None, "warnings": [],
    }
    base.update(over)
    return base


def _write(tmp_path: Path, arc, name="T001__abc12345.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
    return p


def _codes(report) -> set[str]:
    return {f.code for f in report.findings}


# ── 1 · 干净的存档不该报错 ─────────────────────────────────────────

class TestCleanArchive:

    def test_完整存档没有FATAL(self, tmp_path):
        arc = _arc(visits=[_visit("http://a.test", success=True,
                                  player_obs=_obs(video_tag_count=1))],
                   outcomes=[_outcome("http://a.test", None)])
        r = check_archive(_write(tmp_path, arc))
        assert r.fatal == (), [str(f) for f in r.fatal]
        assert r.ok

    def test_无visits无outcomes也算干净(self, tmp_path):
        # 被反爬拦的 run 就是这个形状，不该被判成坏档
        arc = _arc(search_blocked="captcha", search_observation=_obs())
        r = check_archive(_write(tmp_path, arc))
        assert r.fatal == ()

    def test_计数如实(self, tmp_path):
        arc = _arc(visits=[_visit("http://a.test"), _visit("http://b.test")],
                   outcomes=[_outcome("http://a.test", "no_play_control"),
                             _outcome("http://b.test", "unresolved")])
        r = check_archive(_write(tmp_path, arc))
        assert (r.sites, r.outcomes, r.negatives) == (2, 2, 1)   # unresolved 不算真负样本


# ── 2 · 证据链 ─────────────────────────────────────────────────────

class TestEvidenceChain:

    def test_outcome的evidence坏了判FATAL(self, tmp_path):
        arc = _arc(visits=[_visit("http://a.test")],
                   outcomes=[_outcome("http://a.test", "no_play_control",
                                      evidence="坏" + REPLACEMENT_CHAR)])
        r = check_archive(_write(tmp_path, arc))
        assert "evidence_mojibake" in _codes(r)
        assert any(f.severity is Severity.FATAL for f in r.findings)

    def test_观察里的坏字符只降级不致命(self, tmp_path):
        # 同一个字符，坏在观察里 vs 坏在结论依据里，波及面差一个数量级：
        # 前者只让那一条观察不能用，后者让那条结论作废。
        arc = _arc(visits=[_visit("http://a.test",
                                  site_obs=_obs(body_text="正文" + REPLACEMENT_CHAR))],
                   outcomes=[_outcome("http://a.test", "no_play_control")])
        r = check_archive(_write(tmp_path, arc))
        assert "mojibake" in _codes(r)
        assert "evidence_mojibake" not in _codes(r)
        assert r.ok, "一条观察烂了不该把整批 20 个访问点一起判死"

    def test_有结论没证据判FATAL(self, tmp_path):
        arc = _arc(outcomes=[_outcome("http://ghost.test", "no_play_control", evidence="  ")])
        r = check_archive(_write(tmp_path, arc))
        assert "evidence_empty" in _codes(r)

    def test_孤儿outcome照样查证据(self, tmp_path):
        # 没有对应访问点的 outcome 会照样进负样本池、照样出现在报表上
        arc = _arc(outcomes=[_outcome("http://ghost.test", "no_play_control", evidence="")])
        assert "evidence_empty" in _codes(check_archive(_write(tmp_path, arc)))

    def test_成功没有media标签是自相矛盾(self, tmp_path):
        arc = _arc(visits=[_visit("http://hao.test", success=True,
                                  player_obs=_obs(video_tag_count=0, iframe_count=15))],
                   outcomes=[_outcome("http://hao.test", None, evidence="有 iframe")])
        r = check_archive(_write(tmp_path, arc))
        assert "success_without_media" in _codes(r)
        assert not r.ok

    def test_llm版的成功只提示不判死(self, tmp_path):
        # W3 可以有标签之外的判据，不能拿 W1 的判据去否定它
        arc = _arc(visits=[_visit("http://youku.test", success=True,
                                  player_obs=_obs(video_tag_count=0, iframe_count=1))],
                   outcomes=[_outcome("http://youku.test", None, source="llm",
                                      evidence="播放器已加载")])
        r = check_archive(_write(tmp_path, arc))
        sev = [f.severity for f in r.findings if f.code == "success_without_media"]
        assert sev == [Severity.INFO]
        assert r.ok

    def test_非UTF8与坏JSON不抛异常(self, tmp_path):
        # 坏档静默变成「零样本」的话，批次看起来只是少了几条，没人会去查
        p = tmp_path / "T001__bad1.json"
        p.write_bytes(b'{"task_id": "T001", "x": "\xff\xfe"}')
        assert "not_utf8" in _codes(check_archive(p))
        p2 = tmp_path / "T001__bad2.json"
        p2.write_text("{不是 json", encoding="utf-8")
        assert "bad_json" in _codes(check_archive(p2))


# ── 3 · 静默失配（这类坑最贵）─────────────────────────────────────

class TestNoSilentMismatch:

    def test_候选url与落地url不等不该误报缺outcome(self, tmp_path):
        # 站点做 http→https 跳转时 visits[i].url 与 outcomes[j].url 不等，
        # 实测 4 份真实存档的每个 run 都有 3 个访问点对不上。只查一个的后果
        # 是每次都报 outcome_missing，报到没人看为止。
        arc = _arc(visits=[_visit("http://iqiyi.test/x", landed="https://iqiyi.test/x")],
                   outcomes=[_outcome("https://iqiyi.test/x", "no_play_control")])
        r = check_archive(_write(tmp_path, arc))
        assert "outcome_missing" not in _codes(r)

    def test_success与branch打架要报出来(self, tmp_path):
        arc = _arc(visits=[_visit("http://a.test", success=True)],
                   outcomes=[_outcome("http://a.test", "no_play_control")])
        assert "success_branch_mismatch" in _codes(check_archive(_write(tmp_path, arc)))

    def test_真访问点真缺outcome才报(self, tmp_path):
        arc = _arc(visits=[_visit("http://a.test")], outcomes=[])
        assert "outcome_missing" in _codes(check_archive(_write(tmp_path, arc)))


# ── 4 · 并池 ───────────────────────────────────────────────────────

class TestNegativePooling:

    def test_没并池要报出来(self, tmp_path):
        arc = _arc(visits=[_visit("http://a.test")],
                   outcomes=[_outcome("http://a.test", "no_play_control")])
        r = check_archive(_write(tmp_path, arc), pool_keys=frozenset())
        assert "negative_not_pooled" in _codes(r)

    def test_并了就别报(self, tmp_path):
        arc = _arc(visits=[_visit("http://a.test")],
                   outcomes=[_outcome("http://a.test", "no_play_control")])
        keys = frozenset({("T001", "http://a.test", "no_play_control")})
        assert "negative_not_pooled" not in _codes(
            check_archive(_write(tmp_path, arc), pool_keys=keys))

    def test_unresolved不入池也不报(self, tmp_path):
        # unresolved 不是负样本，没并池是对的——报它等于逼人往池里灌脏数据
        arc = _arc(visits=[_visit("http://a.test")],
                   outcomes=[_outcome("http://a.test", "unresolved")])
        r = check_archive(_write(tmp_path, arc), pool_keys=frozenset())
        assert "negative_not_pooled" not in _codes(r)

    def test_池子从来没建过要在notes里说(self, tmp_path):
        _write(tmp_path, _arc(visits=[_visit("http://a.test")],
                              outcomes=[_outcome("http://a.test", "no_play_control")]))
        r = check_root(tmp_path)
        assert any("negative.jsonl" in n for n in r.notes)


# ── 5 · 门禁不能骗人 ───────────────────────────────────────────────

class TestGateDoesNotLie:

    def test_每个能报出的code都渲染得出来(self, tmp_path):
        """写这个测试是因为它真的抓到过一次。

        ``Finding.code`` 一度装的是 ``degraded_from`` 的 marker（``steps``）
        而渲染表按本模块的码（``steps_missing``）建，归组输出里那两组
        **静默消失**了。少报的门禁比没有门禁更坏：它让人以为查过了。
        """
        _write(tmp_path, _arc(
            steps=[], user_prompt="", search_observation=_obs(body_text=""),
            visits=[_visit("http://a.test",
                           site_obs=_obs(body_text="x" + REPLACEMENT_CHAR))],
            outcomes=[_outcome("http://a.test", "no_play_control",
                               evidence="y" + REPLACEMENT_CHAR),
                      _outcome("http://b.test", "trailer_only")],
        ))
        r = check_root(tmp_path)
        emitted = _codes(r)
        rendered = format_report(r)
        assert emitted, "这份语料应当同时触发多类问题"
        for code in emitted:
            assert code in rendered, f"{code} 报了但没印出来（会被当成没报）"

    def test_DEGRADED按code归组而不是刷屏(self, tmp_path):
        # 实测 4 份老存档逐条列会刷出 50+ 行，真问题被埋在重复里
        _write(tmp_path, _arc(steps=[]))
        for i in range(6):
            _write(tmp_path, _arc(steps=[]), name=f"T001__run{i:04d}.json")
        out = format_report(check_root(tmp_path))
        assert "steps_missing ×7" in out

    def test_同组详情只印一次(self, tmp_path):
        """归组之后**详情只印第一条**：印 N 遍等于把 N 行重复塞回报告，
        而重复正是「门禁天天报同一批」让人整体跳过它的原因。"""
        _write(tmp_path, _arc(steps=[]))
        for i in range(4):
            _write(tmp_path, _arc(steps=[]), name=f"T001__run{i:04d}.json")
        out = format_report(check_root(tmp_path))
        assert out.count("没有动作流") == 1


class TestRunConfigSnapshot:
    """``run_config`` 落盘——**批次级评分的分母**。

    没有它，两批 ``max_candidates`` 相差 4 倍的数据在存档里**长得一模一样**：
    都跑满 5 个候选、都有 3 条 outcome。B-2「覆盖完整性」的分母不可知，
    B-4「时间限制」更是无从判断（``per_site_timeout_s`` 连 ``--help`` 里
    都没有）。这类缺失不会让任何一条 outcome 变坏，只会让**整批的结论**
    变成无根据的——比缺个字段严重，而它在报表上完全不可见。
    """

    def test_缺运行参数判DEGRADED(self, tmp_path):
        arc = _arc()
        arc.pop("run_config")
        r = check_archive(_write(tmp_path, arc))
        assert "run_config_missing" in _codes(r)
        assert all(f.severity is Severity.DEGRADED for f in r.findings
                   if f.code == "run_config_missing"), \
            "缺运行参数是降级不是坏档"

    def test_空运行参数同样判DEGRADED(self, tmp_path):
        """空 dict 与缺键同义——手工路径不填它。
        判据用「为假」而不是「键在不在」，否则两种空值会得到两种结论。"""
        r = check_archive(_write(tmp_path, _arc(run_config={})))
        assert "run_config_missing" in _codes(r)

    def test_有运行参数时不报(self, tmp_path):
        assert "run_config_missing" not in _codes(check_archive(
            _write(tmp_path, _arc())))

    def test_不是FATAL(self, tmp_path):
        """老存档全都没有它。判 FATAL 会让整批真实存档不可用——
        而它们的价值只是「分母不可知」，数据本身仍然是真证据。"""
        arc = _arc()
        arc.pop("run_config")
        assert check_archive(_write(tmp_path, arc)).fatal == ()

    def test_渲染得出且说得清代价(self, tmp_path):
        """说明必须写清**丢了什么**。「配置不全」四个字不足以让人判断
        这批数据还能不能用——丢的是覆盖完整性分母。"""
        arc = _arc()
        arc.pop("run_config")
        out = format_report(check_root(_write(tmp_path, arc).parent))
        assert "run_config_missing" in out and "分母" in out


class TestCheckRoot:
    """目录级口径。"""

    def test_空目录不炸(self, tmp_path):
        r = check_root(tmp_path / "nope")
        assert r.archives == () and r.notes

    def test_挑选口径与报表一致(self, tmp_path):
        """门禁和 report 必须看同一批档，否则两边都像对的。"""
        from trajectory_pipeline.executor.archive import select_archives

        for name in ("T001__aaaa1111.json", "T002__bbbb2222.json"):
            _write(tmp_path, _arc(), name=name)
        _write(tmp_path, _arc(), name="obscura_tools.json")       # 探针取证
        _write(tmp_path, _arc(), name="T001__aaaa1111.reviewed.json")
        r = check_root(tmp_path)
        assert {a.name for a in r.archives} == {
            p.name for p, _ in select_archives(tmp_path)}
        assert {a.name for a in r.archives} == {
            "T001__aaaa1111.reviewed.json", "T002__bbbb2222.json"}


class TestContradictedNegatives:
    """池里的负样本被同一 URL 的成功记录推翻——**训练数据里最毒的一种错**。

    实测（2026-10-09，判断点 ① 接线后第一批）一次抓出 3 条，其中 2 条
    是手工翻存档时没看见的：

    ```text
    negative.jsonl  not_play_site  v-wb.youku.com/…id_XNDE0NjYzODAzNg==
    T011__d6badc6f  成功            同一条 URL
    ```

    单看任一份存档都自洽，只有横向摆到一起才现形。
    """

    URL = "https://v-wb.youku.com/v_show/id_X.ht"

    def _corpus(self, tmp_path: Path) -> None:
        """池里一条 not_play_site，另一份存档把同一 URL 记成成功。"""
        _write(tmp_path, _arc(
            outcomes=[_outcome(self.URL, "not_play_site", question="select.play_sites",
                               evidence="检索摘要未显示作品标题，无法确认对应《功夫》")],
        ), name="T011__aaaa0001.json")
        _write(tmp_path, _arc(task_id="T011", outcomes=[
            _outcome(self.URL, None, evidence="LLM 判 True：页面为《功夫》优酷视频播放页"),
        ]), name="T011__bbbb0002.json")
        (tmp_path / "negative.jsonl").write_text(json.dumps(
            {"task_id": "T011", "url": self.URL, "branch": "not_play_site",
             "evidence": "检索摘要未显示作品标题，无法确认对应《功夫》"},
            ensure_ascii=False), encoding="utf-8")

    def test_矛盾被抓出且是FATAL(self, tmp_path):
        self._corpus(tmp_path)
        r = check_root(tmp_path)
        codes = {f.code for f in r.pool_findings}
        assert codes == {"negative_contradicted_by_success"}
        assert all(f.severity is Severity.FATAL for f in r.pool_findings)
        assert not r.ok, "池里有错标签时报告必须不可信"

    def test_挂在报告级而不是每份存档(self, tmp_path):
        """挂到存档上会让同一条矛盾在 N 份存档里各报一遍——实测
        3 条 × 14 份 = 42 行噪声，而 N 随批次数增长。"""
        self._corpus(tmp_path)
        for i in range(5):
            _write(tmp_path, _arc(), name=f"T001__extra{i:04d}.json")
        r = check_root(tmp_path)
        assert len(r.pool_findings) == 1, "同一条矛盾只该报一次"
        for a in r.archives:
            assert "negative_contradicted_by_success" not in _codes(a)

    def test_渲染里只出现一次(self, tmp_path):
        self._corpus(tmp_path)
        out = format_report(check_root(tmp_path))
        assert out.count("negative_contradicted_by_success") == 1
        assert "要动的是池" in out

    def test_没有矛盾时不报(self, tmp_path):
        """成功的 URL 与池里的负样本不是同一个 → 无矛盾。"""
        _write(tmp_path, _arc(outcomes=[_outcome("http://other.test", None)]),
               name="T011__aaaa0001.json")
        _write(tmp_path, _arc(outcomes=[_outcome(self.URL, "no_play_control")]),
               name="T011__bbbb0002.json")
        (tmp_path / "negative.jsonl").write_text(json.dumps(
            {"task_id": "T011", "url": self.URL, "branch": "no_play_control"},
            ensure_ascii=False), encoding="utf-8")
        assert check_root(tmp_path).pool_findings == ()

    def test_池不存在时不报(self, tmp_path):
        _write(tmp_path, _arc(outcomes=[_outcome(self.URL, None)]))
        assert check_root(tmp_path).pool_findings == ()

    def test_坏池行不炸(self, tmp_path):
        """池被写坏时末行可能是半截 JSON——丢弃，不抛。

        坏行另起一行：**粘在好行后面会把那条好行一起废掉**，
        那不是「丢弃坏行」，是「丢了好行」。所以这里显式补 ``\\n``。
        """
        self._corpus(tmp_path)
        with (tmp_path / "negative.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('\n{"task_id": "T011", "url": "http://x.test", "bra')
        assert len(check_root(tmp_path).pool_findings) == 1


class TestNoAssemblerImport:
    """门禁读 P1 是读**存档形状**，不是读 P2 的切条逻辑。

    反向也成立：assembler 不该知道 executor 的存在（那边已有
    ``TestNoExecutorImport`` 盯着）。两边对「缺件」各有权威定义，
    靠 :func:`degraded_marker_map` 显式对齐而不是互相 import。
    """

    def test_不import_assembler(self):
        src = (Path(integrity.__file__)).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any("assembler" in n for n in names), names


class TestDegradedMarkersMatchSplit:
    """本模块的 DEGRADED 码 ↔ ``schema.split_archive`` 的 ``degraded_from``。

    分开实现必然漂，所以钉死：**缺件齐全的存档，两边给出的集合必须相等**。
    谁先改谁红。

    注意只对齐**缺件**，不把「损坏」也算进去——``degraded_from`` 答的是
    「P1 里没有的键」，「P1 里有但坏了」是门禁的事。混进一张表会让
    「正文是截短的」与「正文是烂的」看起来一样重，而后者更糟。
    """

    @staticmethod
    def _degraded_archive(tmp_path) -> Path:
        # 只留 body_preview（触发 body_preview_only / no_body 两种），
        # 不给 steps 与 user_prompt，站点级还差一个 outcome。
        return _write(tmp_path, _arc(
            steps=[], user_prompt="",
            search_observation={"url": "https://s.test", "body_preview": "预览"},
            visits=[_visit("http://a.test", site_obs=_obs(body_text="", body_preview="预览"))],
            outcomes=[],                       # → outcome_missing
        ))

    def test_两边集合相等(self, tmp_path):
        p = self._degraded_archive(tmp_path)
        mine = {degraded_marker_map()[c] for c in _codes(check_archive(p))
                if c in degraded_marker_map()}
        theirs = set()
        for sample in p2_schema.split_archive(p):
            theirs |= set(sample.degraded_from)
        # search_observation / site_obs 给了 body_preview，两条都不算 no_body
        assert mine == theirs, f"门禁 {sorted(mine)} vs P2 {sorted(theirs)}"

    def test_映射里的每条都真被报出来过(self):
        """映射表里挂一条永远不会触发的项 = 两侧悄悄分家，且没人发现。"""
        src = Path(integrity.__file__).read_text(encoding="utf-8")
        for code in degraded_marker_map():
            assert f'"{code}"' in src, f"{code} 在映射表里但模块里从没报出来过"

    def test_损坏不进degraded_from词表(self):
        """``mojibake`` 故意**不在**映射表里：``degraded_from`` 答的是
        「P1 里没有的键」，损坏是「有但坏了」。混进去会让「正文截短」与
        「正文是烂的」看起来一样重，而后者更糟。"""
        assert "mojibake" not in degraded_marker_map()
        assert "evidence_mojibake" not in degraded_marker_map()