"""人判两条的探针（``inc73_*``）的口径测试——**不联网、不开浏览器**。

这三条判据是**人判**，机器判不了，但「人判看的表」与「人判的归因规则」
本身是**机器可测的**：如果归因规则错了，人判出来的结论就系统性偏了。

测三件事：

1. **阶梯成员判定**（:func:`_in_ladder`）——「目标当时在不在采集面给出的集合里」。
   它是判据⑥ 归因的**分水岭**：当时不在 ⇒ 重放定位不到**不算**可移植性问题。
2. **归因映射**（:func:`_replay_inner`）——四种结局各落各的标签，
   尤其「页面漂移」与「名字脆」**不许混**（混了就把「测的是另一个页面」
   误记成「动作不可移植」）。
3. **分母**（:func:`_summarize`）——可移植率的分母**只算拿到读数的条目**。
   把超时/出错的算进分母 = 把「没测出来」读成「不可移植」，
   那正是判据⑥ 要拆开的混淆。
"""

from __future__ import annotations

from typing import Any

import pytest

from trajectory_pipeline.research.probes.inc73_action_replay import (
    _replay_inner,
    _summarize,
)
from trajectory_pipeline.research.probes.inc73_human_review import (
    CAUSE_KEEP,
    CAUSES,
    KEEP_AUDIT_FIELD,
    KEEP_VALUES,
    LEGACY_KEEP_FIELD,
    NOT_NEGATIVE_CAUSES,
    OUR_SIDE_CAUSES,
    SITE_SIDE_CAUSES,
    UNKNOWN_CAUSE,
    _action_rows,
    _carry_over,
    _check_vocabulary,
    _derive_keep,
    _in_ladder,
    _keep_conflicts,
    _summary,
)
from trajectory_pipeline.executor.branches import (
    BRANCH_LABELS,
    NON_SAMPLE_BRANCHES,
)


class _Obs:
    def __init__(self, *, url: str = "https://x.test/vod/1",
                 title: str = "示例影片", body: str = "正文", elements: int = 12) -> None:
        self.url = url
        self.page_title = title
        self.body_text = body
        self.elements_total = elements


class _FakeDriver:
    """只实现重放要用的三个口：``goto`` / ``observe`` / ``resolve``。"""

    def __init__(self, obs: _Obs, resolve: dict[str, Any]) -> None:
        self._obs = obs
        self._resolve = resolve
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **_kw: Any) -> None:
        self.goto_calls.append(url)

    async def observe(self) -> _Obs:
        return self._obs

    async def resolve(self, *, role: str | None, name: str | None) -> dict[str, Any]:
        return dict(self._resolve)


class Test阶梯成员判定:
    def test_在阶梯里算在(self):
        obs = {"interactive_elements": [
            {"ref": "e1", "role": "link", "name": "立即播放"},
            {"ref": "e2", "role": "button", "name": "下一集"},
        ]}
        assert _in_ladder(obs, "link", "立即播放") is True

    def test_不在阶梯里算不在(self):
        obs = {"interactive_elements": [{"ref": "e1", "role": "link", "name": "立即播放"}]}
        assert _in_ladder(obs, "link", "正片") is False

    def test_同名不同角色不算在(self):
        """判定是 **(role, name) 对**，不是「名字出现在页面上就行」——
        名字对、角色错 ⇒ 定位不到（``get_by_role`` 按角色筛）。"""
        obs = {"interactive_elements": [{"ref": "e1", "role": "link", "name": "立即播放"}]}
        assert _in_ladder(obs, "button", "立即播放") is False

    def test_没有阶梯产物时算不在(self):
        """obscura 面的存档没有 ``interactive_elements``（它走 ref 编码）。
        读不到就是「不知道」——本实现取**保守值 False**，
        于是这类条目不会被算进「名字脆」那一档。"""
        assert _in_ladder({}, "link", "立即播放") is False
        assert _in_ladder({"interactive_elements": "not-a-list"}, "link", "x") is False


class Test动作行:
    def test_动作行带上当时的页面事实与阶梯成员(self):
        rec = {
            "task_id": "DR01",
            "run_id": "r1",
            "visits": [{
                "landed_url": "https://x.test/vod/1",
                "steps": [{
                    "action": {"tool": "click", "params": {}, "origin": "model",
                               "target": {"role": "link", "name": "立即播放"}},
                    "observation": {
                        "url": "https://x.test/vod/1",
                        "page_title": "示例影片",
                        "body_len": 736,
                        "elements_total": 76,
                        "interactive_elements": [
                            {"ref": "e1", "role": "link", "name": "立即播放"}],
                    },
                    "error": "",
                    "duration_ms": 500,
                }],
            }],
        }
        rows = _action_rows(rec, src="dir")
        assert len(rows) == 1
        row = rows[0]
        assert row["role"] == "link" and row["name"] == "立即播放"
        assert row["visit_landed_url"] == "https://x.test/vod/1"
        assert row["archived_page_title"] == "示例影片"
        assert row["archived_body_len"] == 736
        assert row["archived_in_ladder"] is True

    def test_非定位动作不进表(self):
        """``goto`` / ``new_tab`` 不带元素定位——判据⑥ 管的是「目标能不能定位到」，
        把无目标的导航混进来会稀释分母。"""
        rec = {
            "task_id": "DR01", "run_id": "r1",
            "visits": [{"landed_url": "https://x.test/", "steps": [
                {"action": {"tool": "goto", "params": {"url": "https://x.test/"},
                            "origin": "model", "target": None},
                 "observation": {}, "error": "", "duration_ms": 1},
            ]}],
        }
        assert _action_rows(rec, src="dir") == []


class Test归因映射:
    async def test_命中一个算可移植(self):
        out: dict[str, Any] = {
            "visit_landed_url": "https://x.test/vod/1",
            "archived_page_title": "示例影片", "archived_body_len": 2,
            "archived_in_ladder": True,
        }
        driver = _FakeDriver(_Obs(title="示例影片", body="正文"),
                             {"outcome": "ok", "matches": 1})
        await _replay_inner(driver, out, "link", "立即播放")
        assert out["outcome"] == "ok" and out["matches"] == 1
        assert out["verdict"] == "portable"
        assert out["page_drift"] is False

    async def test_真同名算不可判定(self):
        out: dict[str, Any] = {
            "visit_landed_url": "https://x.test/vod/1",
            "archived_page_title": "示例影片", "archived_body_len": 2,
            "archived_in_ladder": True,
        }
        driver = _FakeDriver(_Obs(title="示例影片", body="正文"),
                             {"outcome": "ambiguous", "matches": 3})
        await _replay_inner(driver, out, "link", "立即播放")
        assert out["verdict"] == "ambiguous"

    async def test_页面漂了不记成名字脆(self):
        """⚠️ **页面漂与名字脆是两回事**。漂了 = 测的是另一个页面
        （站点改版/换内容），不是动作目标不可移植——混起来就会把
        「站点变了」误记成「动作面不可移植」，判据⑥ 直接判错对象。"""
        out: dict[str, Any] = {
            "visit_landed_url": "https://x.test/vod/1",
            "archived_page_title": "老标题", "archived_body_len": 2,
            "archived_in_ladder": True,
        }
        driver = _FakeDriver(_Obs(title="新标题", body="正文"),
                             {"outcome": "not_found", "matches": 0})
        await _replay_inner(driver, out, "link", "立即播放")
        assert out["page_drift"] is True
        assert out["verdict"] == "not_found_page_drift"

    async def test_当时不在阶梯里不记成名字脆(self):
        """目标**当时就不在**采集面给出的集合里（真同名被阶梯整组丢掉）⇒
        重放定位不到**不是**重放的问题——是「本来就没这条动作面」。"""
        out: dict[str, Any] = {
            "visit_landed_url": "https://x.test/vod/1",
            "archived_page_title": "示例影片", "archived_body_len": 2,
            "archived_in_ladder": False,
        }
        driver = _FakeDriver(_Obs(title="示例影片", body="正文"),
                             {"outcome": "not_found", "matches": 0})
        await _replay_inner(driver, out, "link", "立即播放")
        assert out["page_drift"] is False
        assert out["verdict"] == "not_found_never_collected"

    async def test_页面没漂且当时在阶梯里才是名字脆(self):
        out: dict[str, Any] = {
            "visit_landed_url": "https://x.test/vod/1",
            "archived_page_title": "示例影片", "archived_body_len": 2,
            "archived_in_ladder": True,
        }
        driver = _FakeDriver(_Obs(title="示例影片", body="正文"),
                             {"outcome": "not_found", "matches": 0})
        await _replay_inner(driver, out, "link", "立即播放")
        assert out["verdict"] == "not_found_name_fragile"

    async def test_没有基线时归因不了而不是硬塞(self):
        """旧批次存档既没标题也没正文长度 ⇒ **没有可比基线**。

        把漂移记成 ``False``（「没漂」）就是把「没测出来」读成「没问题」——
        与 §3.7 同源的错。漂移记 ``None``，归因落「归因不了」，
        **不许**硬塞进「名字脆」或「本来就没这条」那两个有基线才成立的结论。
        """
        out: dict[str, Any] = {
            "visit_landed_url": "https://x.test/vod/1",
            "archived_page_title": "", "archived_body_len": None,
            "archived_in_ladder": True,
        }
        driver = _FakeDriver(_Obs(title="现场标题", body="正文"),
                             {"outcome": "not_found", "matches": 0})
        await _replay_inner(driver, out, "link", "立即播放")
        assert out["page_drift"] is None
        assert out["baseline_available"] is False
        assert out["verdict"] == "not_found_unattributable"

    async def test_空标题不算基线(self):
        """采集时标题没抓到（很常见）⇒ **空标题不是基线**。
        拿空标题跟现场标题比只会得出「漂了」——而真相是「当时就没这个读数」。"""
        out: dict[str, Any] = {
            "visit_landed_url": "https://x.test/vod/1",
            "archived_page_title": "", "archived_body_len": 2,
            "archived_in_ladder": True,
        }
        driver = _FakeDriver(_Obs(title="现场标题", body="正文"),
                             {"outcome": "ok", "matches": 1})
        await _replay_inner(driver, out, "link", "立即播放")
        assert out["baseline_available"] is True
        assert out["page_drift"] is False      # 正文长度一致 ⇒ 没漂
        assert out["verdict"] == "portable"


class Test分母:
    def test_无读数的条目不进分母(self):
        """超时/出错 = **没测出来**，不是「不可移植」——
        算进分母就是把「没测出来」读成「不可移植」。"""
        s = _summarize([
            {"verdict": "portable", "page_drift": False},
            {"verdict": "timeout", "page_drift": False},
            {"verdict": "error", "page_drift": False},
        ])
        assert s["total"] == 3
        assert s["measured"] == 1
        assert s["unmeasured"] == 2
        assert s["portable"] == 1
        assert s["portable_rate_of_measured"] == 1.0

    def test_漂移的条目仍算有读数(self):
        """漂了也**测到了**（读数是「另一个页面上定位不到」）——
        它不是「没测出来」，所以进分母，但单独记一笔漂移条数。"""
        s = _summarize([
            {"verdict": "not_found_page_drift", "page_drift": True},
            {"verdict": "portable", "page_drift": False},
        ])
        assert s["measured"] == 2
        assert s["page_drift"] == 1
        assert s["portable_rate_of_measured"] == 0.5

    def test_空集不报零分(self):
        s = _summarize([])
        assert s["total"] == 0
        assert s["portable_rate_of_measured"] is None


class Test成因到留池的映射:
    """``cause`` → ``keep_as_negative`` 的默认映射。

    这条映射是**判据⑤ 的口径本身**：责任在站点 ⇒ 留池；责任在我们 ⇒ 出池。
    映射表跟词表是两份东西，**必须一起改**——只改一边就出静默的错
    （人按旧词表填、汇总按新映射读，汇总出来的是错的分布）。
    """

    def test_词表每一项都有映射(self):
        assert set(CAUSE_KEEP) == set(CAUSES)

    def test_取值都在封闭集里或留空(self):
        for cause, keep in CAUSE_KEEP.items():
            assert keep in KEEP_VALUES or keep == "", cause

    def test_站点侧成因都是注册分支(self):
        """**站点侧成因取值就是分支名**——这样「人判的真因」与
        「机器贴的标签」能逐条对账（``cause != branch`` ⇒ 标签错了）。
        手抄一份分支名必然漂，所以这条钉住它与注册表一致。"""
        for cause in SITE_SIDE_CAUSES:
            assert cause in BRANCH_LABELS, cause

    def test_站点侧成因都不是非样本分支(self):
        """非样本分支**永远不进池**——真因是它们的行必须出池，
        不能被当成「站点侧真因」留在池里。"""
        for cause in SITE_SIDE_CAUSES:
            assert cause not in NON_SAMPLE_BRANCHES, cause

    def test_我们侧成因要么是非样本分支要么不是分支(self):
        """``not_collected`` / ``nav_interrupted`` 是**动作码**（分支体系里
        没有对应分支）；``player_unverified`` / ``trailer_suspect`` /
        ``unresolved`` 是分支但**永远不进池**。"""
        for cause in OUR_SIDE_CAUSES:
            assert cause in NON_SAMPLE_BRANCHES or cause not in BRANCH_LABELS, cause

    def test_没判出来与说不清是两回事(self):
        """``unresolved`` = **判过了**，结论就是「没判出来」（fail-closed 落点，
        台账 H-15 的裁定）⇒ 出池；``?`` = **还没判** ⇒ 留空待查。

        两者合成一个码 ⇒ 「要人再去看一眼的」被当成「已认定的坏数据」
        处理掉，或者反过来把该清的留在池里。"""
        assert "unresolved" in OUR_SIDE_CAUSES
        assert CAUSE_KEEP["unresolved"] == "no"
        assert CAUSE_KEEP[UNKNOWN_CAUSE] == ""

    def test_责任在站点的留池(self):
        for cause in SITE_SIDE_CAUSES:
            assert CAUSE_KEEP[cause] == "yes", cause

    def test_责任在我们的出池(self):
        for cause in OUR_SIDE_CAUSES:
            assert CAUSE_KEEP[cause] == "no", cause

    def test_说不清留空而不是判否(self):
        """「说不清」≠「不是负样本」——
        填 ``no`` 等于把一条没查清的行当成已认定的坏数据处理掉。"""
        assert CAUSE_KEEP[UNKNOWN_CAUSE] == ""

    def test_登录墙与反爬不共用一个码(self):
        """⚠️ **反爬 + 登录墙的合并口径会给出相反的结论**——
        判据① 已经踩过（``dual_run.py``：合并口径 B 34.09% > A 19.15%，
        但真·反爬是 B 9.09% < A 14.89%，**差全在登录墙桶里**，
        而登录墙不是反爬）。成因词表**不许**再合。"""
        assert "login_required" in SITE_SIDE_CAUSES
        assert "blocked" in SITE_SIDE_CAUSES

    def test_付费墙不与登录墙共用(self):
        """付费墙是**第三种**站点侧成因：登录了也要付钱。

        与登录墙共用一个码 ⇒「登录能不能解决」这个区分就没了——
        而那恰是判断「这里真的看不了」的核心：
        登录墙登录就消了（对真人不是负样本），
        付费墙登录也不消（是真负样本）。"""
        assert "subscription_wall" in SITE_SIDE_CAUSES
        assert "partial_free" in SITE_SIDE_CAUSES
        assert CAUSE_KEEP["subscription_wall"] == CAUSE_KEEP["login_required"] == "yes"
        # 但它们是**两个不同的成因**——不许合成一个码
        assert "subscription_wall" != "login_required"

    def test_词表与分支注册表对账通过(self):
        """探针生成工作表时会跑这道对账——**词表写错时工作表出不来**，
        而不是「生成一份口径已经错了的表」。"""
        _check_vocabulary()

    def test_success入词表且是假负样本(self):
        """``success`` = **资源存在**（页面在剧集/资源页），我们没走到播放页。

        它是**第三类**（「不是负样本」），不是站点侧也不是我们侧：
        站点没说看不了，反而给了资源 ⇒ 机器把它记成负样本是**假的**
        ⇒ 出池。

        ⚠️ 处置与「我们侧」同（出池）但**理由不同**：我们侧是「数据坏了，
        降级 ``unresolved``」，这里是「**它其实是个好样本**，只是标签错了」
        ⇒ 这一类**要转正**，不能只降级。
        """
        assert "success" in CAUSES
        assert "success" in NOT_NEGATIVE_CAUSES
        assert CAUSE_KEEP["success"] == "no"

    def test_success不是注册分支(self):
        """⚠️ **同名不同义**：分支体系里已经有一个 ``success``
        （``branch=None`` = **视频播出来了**），本词表的 ``success`` 是
        「**资源存在**（页面在剧集/资源页）」。

        两边共同点只有「这行不是负样本」。哪天 ``success`` 进了分支注册表，
        这两个含义就会混成**一个码**——那正是「登录墙与反爬共用一码」
        那条老路。``_check_vocabulary`` 会当场抛。
        """
        assert "success" not in BRANCH_LABELS
        assert "success" not in NON_SAMPLE_BRANCHES

    def test_不是负样本的成因都不是分支(self):
        """这一类记的是「标签错了」，不是「站点说了什么」——
        填成分支名就等于把「机器的标签」当「人判的真因」，对账失去意义。"""
        for cause in NOT_NEGATIVE_CAUSES:
            assert cause not in BRANCH_LABELS, cause


class Test派生留池与冲突:
    """留池列**由成因派生**——人只填 ``cause``，机器按 ``CAUSE_KEEP`` 算。

    为什么不能两头都留一份：留给人手填那一列就是**两份真源**。
    实测（2026-10-21 首轮人判）漂了 4 行——**同一个 URL 在两批里给出
    相反的留池值**，而汇总读的是映射 ⇒ 汇总出来的是错的分布。
    """

    @staticmethod
    def _row(cause: str, human: str = "",
             branch: str = "component_unverified") -> dict[str, Any]:
        return {
            "task_id": "DR01", "run_id": "r1", "url": "https://x.test/vod/1",
            "branch": branch,
            "cause": cause, "keep_as_negative": "",
            KEEP_AUDIT_FIELD: human, "human_note": "n",
        }

    def test_留池列按成因派生(self):
        rows = [self._row("subscription_wall"), self._row("success"),
                self._row("unresolved"), self._row("?")]
        _derive_keep(rows)
        assert [r["keep_as_negative"] for r in rows] == ["yes", "no", "no", ""]

    def test_未判的行留空(self):
        rows = [self._row("")]
        _derive_keep(rows)
        assert rows[0]["keep_as_negative"] == ""

    def test_人填值与派生值冲突要点名(self):
        """人填 ``no``、规则派生 ``yes`` ⇒ **以规则为准**，但**要点名**——
        不静默取一个（静默取一个 = 把矛盾藏起来，下次还会漂）。"""
        rows = [self._row("subscription_wall", human="no")]
        _derive_keep(rows)
        assert rows[0]["keep_as_negative"] == "yes"
        conflicts = _keep_conflicts(rows)
        assert len(conflicts) == 1
        c = conflicts[0]
        assert c["cause"] == "subscription_wall"
        assert c["keep_human"] == "no" and c["keep_derived"] == "yes"
        assert c["url"] == "https://x.test/vod/1"

    def test_词表外的成因派生不出留池值也要点名(self):
        """人填了一个词表外的成因 ⇒ 派生不出留池值——
        **不能静默当成「未判」混进分布**（那会把一条判过的行算成没判）。"""
        rows = [self._row("subscripton_wall", human="yes")]
        _derive_keep(rows)
        assert rows[0]["keep_as_negative"] == ""
        conflicts = _keep_conflicts(rows)
        assert len(conflicts) == 1
        assert "派生不出" in conflicts[0]["reason"]

    def test_没填过留池值的行不报冲突(self):
        rows = [self._row("subscription_wall")]
        _derive_keep(rows)
        assert _keep_conflicts(rows) == []

    def test_汇总按派生值统计且单列已弃用合并态(self):
        """汇总读**派生值**，不读人填的那一列；
        且「真·标签错」要把**已弃用合并态**单列——
        那 10 条的标签错是已知的（H-15），混进来会盖掉这轮新发现的错。"""
        rows = [
            self._row("not_play_site", branch="not_play_site"),  # cause == branch
            self._row("success"),                       # 标签错（非合并态）
            {"task_id": "DR02", "run_id": "r2", "url": "u2",
             "branch": "login_wall_or_blocked", "cause": "subscription_wall",
             "keep_as_negative": "yes", KEEP_AUDIT_FIELD: "", "human_note": ""},
        ]
        _derive_keep(rows)
        s = _summary(rows)
        assert s["rows"] == 3 and s["judged"] == 3
        assert s["keep_dist"] == {"yes": 2, "no": 1}
        assert s["cause_equals_branch"] == 1
        assert s["cause_differs_branch"] == 2
        # 2 条不一致里，1 条是已弃用合并态 ⇒ 真·标签错 1 条
        assert s["cause_differs_branch_excluding_deprecated"] == 1
        assert s["unknown_causes"] == {}

    def test_词表外的成因没人填过也要点名(self):
        """⚠️ **词表外的成因本身就是问题**，不能因为「人没填留池值」
        就放过——那一条会静默混进「未判」那一桶，
        看着像「还没判」，实际是「填错了」。"""
        rows = [self._row("subscripton_wall", human="")]
        _derive_keep(rows)
        conflicts = _keep_conflicts(rows)
        assert len(conflicts) == 1
        assert "派生不出" in conflicts[0]["reason"]

    def test_还没判的行不算冲突(self):
        """⚠️ **「还没判」不是「填错了」**——``cause`` 为空 ⇒ 那是**待查**。
        报成冲突 ⇒ **一张还没判的表会报出满屏冲突**（39 行全中），
        而「冲突」这一格是给人看分歧的，满屏噪声等于没有信号。"""
        rows = [self._row(""), self._row("")]
        _derive_keep(rows)
        assert _keep_conflicts(rows) == []


class Test沿用与重生成:
    """重生成工作表**不能把人的判断静默抹掉**——本组盯的就是这条。

    实测（2026-10-21）：沿用逻辑分不清「留池列是人填的」还是
    「留池列是派生的」，于是把**派生值**当成了人的值写进审计列 ⇒
    4 条留池冲突在重生成后**静默消失**（``keep_conflicts`` 4 → 0）。
    """

    @staticmethod
    def _prev(cause: str, keep: str, audit: str | None = None) -> dict[str, Any]:
        """旧表的一行。``audit is None`` ⇒ **旧表**（没有审计列，
        留池列就是人填的）；否则是**新表**（留池列是派生的）。"""
        row: dict[str, Any] = {
            "task_id": "DR01", "run_id": "r1", "url": "https://x.test/vod/1",
            "branch": "subscription_wall", "cause": cause,
            LEGACY_KEEP_FIELD: keep, "human_note": "n",
        }
        if audit is not None:
            row[KEEP_AUDIT_FIELD] = audit
        return row

    @staticmethod
    def _new(cause: str = "subscription_wall") -> dict[str, Any]:
        return {
            "task_id": "DR01", "run_id": "r1", "url": "https://x.test/vod/1",
            "branch": "subscription_wall", "cause": cause,
            LEGACY_KEEP_FIELD: "", KEEP_AUDIT_FIELD: "", "human_note": "",
        }

    def test_新表沿用后审计值不被派生值覆盖(self):
        """新表里 ``keep_as_negative`` 是**派生值**、``keep_as_negative_human``
        才是**人填的**。沿用时取错 ⇒ 4 条冲突静默消失（实测踩过）。"""
        prev = [self._prev("subscription_wall", "yes", audit="no")]
        new = [self._new()]
        assert _carry_over(prev, new) == 3      # cause + note + 审计值
        assert new[0][KEEP_AUDIT_FIELD] == "no"
        _derive_keep(new)
        assert new[0][LEGACY_KEEP_FIELD] == "yes"
        conflicts = _keep_conflicts(new)
        assert len(conflicts) == 1
        assert conflicts[0]["keep_human"] == "no"
        assert conflicts[0]["keep_derived"] == "yes"

    def test_旧表那一列就是人填的(self):
        """首轮那张表**没有审计列**——留池列就是人填的。
        沿用时它该进审计列（人填的那一版要留住）。"""
        prev = [self._prev("subscription_wall", "no")]
        new = [self._new()]
        _carry_over(prev, new)
        assert new[0][KEEP_AUDIT_FIELD] == "no"
        _derive_keep(new)
        assert new[0][LEGACY_KEEP_FIELD] == "yes"    # 生效值按映射
        assert len(_keep_conflicts(new)) == 1        # 矛盾点名，不静默取一个

    def test_人改过派生列则以手改的为准并点名(self):
        """派生列**允许人手动改**（那是人的最新表达）——
        但改了什么要看得见 ⇒ 与映射不一致的行要进冲突表。"""
        prev = [self._prev("subscription_wall", "no", audit="yes")]
        new = [self._new()]
        _carry_over(prev, new)
        _derive_keep(new)
        assert new[0][LEGACY_KEEP_FIELD] == "no"    # 手改的值生效
        conflicts = _keep_conflicts(new)
        assert len(conflicts) == 1
        assert "手改" in conflicts[0]["reason"]

    def test_成因变了就不把手改当手改(self):
        """人改了**成因** ⇒ 留池列按新成因重算——
        旧的手改值是**旧成因**下的表达，不该压住新映射。"""
        prev = [self._prev("subscription_wall", "no", audit="yes")]
        new = [self._new("success")]
        _carry_over(prev, new)
        _derive_keep(new)
        assert new[0][LEGACY_KEEP_FIELD] == "no"    # success ⇒ 映射 no
        assert new[0][KEEP_AUDIT_FIELD] == "yes"   # 旧成因下的审计值仍在


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
