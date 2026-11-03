"""负样本池的**成员资格**（方案 23 R4-2 / Q-33 裁「甲」）。

池成员资格有**两段**：

1. **分支口径**（旧有）——「没判出来」（``unresolved`` / ``trailer_suspect``
   / ``player_unverified`` / ``site_search_routed``）与「这次没看到」
   （``blocked``）都不进池；
2. **任务口径**（2026-11-03 补）——任务声明了 ``excluded_platforms`` 时，
   **落在那些平台上的访问点整条剔掉** ⇒ 它的失败不是「这个任务里这个站
   看不了」，是「这个站本来就不在任务范围里」。

第二段漏掉的后果实测过（2026-11-03）：**121 条**正确的「没并池」被门禁
报成错（报表全红，而池子是对的），回填还会把剔掉的行**又并回池里**
（池里混进不该训的站）——**两种都不报错**，只是数字悄悄不对。

⇒ 判定只许有一份（:mod:`trajectory_pipeline.executor.negative_rules`），
三处调用方（写入路径 / 门禁 / 回填）认同同一份结果。

⚠️ **池的身份是「键」不是「行」**：去重键是 ``(task_id, url, branch)``，
同一条存档里两个 outcome 可能带同一个键 ⇒ 判定落在键上。
"""

from __future__ import annotations

import json

from trajectory_pipeline.executor import archive as ar
from trajectory_pipeline.executor import integrity
from trajectory_pipeline.executor import negative_rules as nr
from trajectory_pipeline.executor import pool_add as pa
from trajectory_pipeline.executor.branches import SiteOutcome


# ── 夹具 ──────────────────────────────────────────────────────────

#: 腾讯视频（T001 声明了它 ⇒ 落在它上面的访问点整条剔掉）。
QQ = "https://v.qq.com/x/cover/abc.html"
#: 一个没被排除的站。
SITE = "https://a.test/x.html"


def _obs(url):
    return {
        "url": url, "page_title": "", "body_text": "正文", "truncated": False,
        "degraded": [], "video_tag_count": 0, "iframe_count": 0,
        "interactive_elements": [], "links": [],
    }


def _step(action, observation=None):
    return {"action": action, "observation": observation,
            "error": "", "rationale": None}


def _goto(url):
    return {"tool": "goto", "params": {"url": url},
            "origin": "model", "target": None}


def _visit(url, steps=None):
    return {
        "url": url, "landed_url": url, "success": False, "notes": [],
        "steps": steps or [], "site_obs": _obs(url),
        "player_obs": None, "reflection": None,
    }


def _qq_visit():
    """一个**访问了被排除平台**的访问点（模型自己 goto 过去的）。"""
    return _visit(QQ, steps=[_step(_goto(QQ), _obs(QQ))])


def _site_visit():
    return _visit(SITE, steps=[_step(_goto(SITE), _obs(SITE))])


def _outcome(url, branch, evidence="判定正文"):
    return {
        "url": url, "site_url": url, "play_page_url": "",
        "branch": branch, "branch_label": branch, "evidence": evidence,
        "question": "player.ok", "source": "llm", "fallback_used": False,
        "reached_play_page": False,
    }


def _arch(task_id="T001", outcomes=None, visits=None):
    return {
        "task_id": task_id, "title": "功夫", "run_id": "r1",
        "driver": "camofox",
        "provenance": {"persona_id": "parent-喜剧"},
        "user_prompt": "有没有能看正片的",
        "run_config": {"engine": "baidu", "max_candidates": 20,
                       "max_chars": None, "stop_after_success": 0,
                       "per_site_timeout_s": 90.0},
        "steps": [], "visits": visits if visits is not None else [],
        "outcomes": outcomes if outcomes is not None else [],
        "ledger": {"by_branch": {}, "negative_samples": 0,
                   "missing_branches": []},
        "warnings": [], "reflection": None,
    }


def _mixed_arch(task_id="T001"):
    """一份同时含「被排除平台上的失败」与「正常站上的失败」的存档。"""
    return _arch(
        task_id=task_id,
        visits=[_qq_visit(), _site_visit()],
        outcomes=[
            _outcome(QQ, "subscription_wall", "点播放后跳到开通会员页"),
            _outcome(SITE, "subscription_wall", "点播放后跳到开通会员页"),
        ],
    )


class _Ledger:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)

    def negative_samples(self):
        return list(self._outcomes)


class _Record:
    """一份可写盘的运行记录（只带 ``write()`` 用到的那几个面）。"""

    def __init__(self, doc, outcomes=None):
        self._doc = dict(doc)
        self.ledger = _Ledger(outcomes or [])
        self.provenance = dict(doc.get("provenance") or {})
        self.driver = str(doc.get("driver") or "")

    def to_json(self):
        return dict(self._doc)


def _pool_rows(tmp_path):
    path = tmp_path / "negative.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


# ── 第一段：分支口径 ──────────────────────────────────────────────


class TestIsPoolMemberBranch:
    def test_真负样本算成员(self):
        assert nr.is_pool_member_branch("subscription_wall")
        assert nr.is_pool_member_branch("partial_free")
        assert nr.is_pool_member_branch("component_unverified")

    def test_没判出来的不算(self):
        """「没判出来」不是「这里看不了」——混进池子会毁掉那条信号。"""
        for branch in ("unresolved", "trailer_suspect",
                       "player_unverified", "site_search_routed"):
            assert not nr.is_pool_member_branch(branch), branch

    def test_采集侧账不算(self):
        assert not nr.is_pool_member_branch("blocked")

    def test_成功不算(self):
        assert not nr.is_pool_member_branch(None)


# ── 第二段：任务级剔除 ────────────────────────────────────────────


class TestPoolMemberKeys:
    def test_被排除平台上的失败不是池成员(self):
        members = nr.pool_member_keys(_mixed_arch())
        assert (QQ, "subscription_wall") not in members

    def test_没被排除的站照旧是池成员(self):
        members = nr.pool_member_keys(_mixed_arch())
        assert (SITE, "subscription_wall") in members

    def test_任务没声明排除就照旧(self):
        """剔除是**任务口径**：同一个站在别的任务里没被排除 ⇒ 那边照旧。"""
        members = nr.pool_member_keys(_mixed_arch(task_id="NO_SUCH_TASK"))
        assert (QQ, "subscription_wall") in members
        assert (SITE, "subscription_wall") in members

    def test_被剔的键集(self):
        dropped = nr.excluded_pool_keys(_mixed_arch())
        assert dropped == {(QQ, "subscription_wall")}

    def test_下标口径与键口径一致(self):
        arch = _mixed_arch()
        members = nr.pool_member_keys(arch)
        indices = nr.pool_member_indices(arch)
        assert indices == {1}          # 第 0 条是被剔的


# ── 写入路径 ──────────────────────────────────────────────────────


class TestWritePathSkipsExcluded:
    def test_被排除平台的失败不进池(self, tmp_path):
        """并池是**写入时**的动作 ⇒ 剔掉的行根本不该落进池文件。"""
        doc = _mixed_arch()
        outcomes = [
            SiteOutcome(url=QQ, branch="subscription_wall",
                        evidence="点播放后跳到开通会员页"),
            SiteOutcome(url=SITE, branch="subscription_wall",
                        evidence="点播放后跳到开通会员页"),
        ]
        store = ar.P1Archive(tmp_path)
        store.write(_Record(doc, outcomes), task_id="T001", run_id="r1")

        rows = _pool_rows(tmp_path)
        assert [r["url"] for r in rows] == [SITE]

    def test_存档本身一个字不动(self, tmp_path):
        """剔除只发生在**组装层与池**——P1 是证据，不许改。"""
        doc = _mixed_arch()
        outcomes = [
            SiteOutcome(url=QQ, branch="subscription_wall",
                        evidence="点播放后跳到开通会员页"),
        ]
        store = ar.P1Archive(tmp_path)
        path = store.write(_Record(doc, outcomes), task_id="T001", run_id="r1")

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert len(saved["outcomes"]) == 2
        assert saved["outcomes"][0]["url"] == QQ


# ── 门禁 ──────────────────────────────────────────────────────────


class TestGateDoesNotReport:
    def test_被剔除的行不报未并池(self, tmp_path):
        """甲口径下那批行**本来就该没并池**——报成错就是报表全红而池子是对的。"""
        path = tmp_path / "T001__r1.json"
        path.write_text(json.dumps(_mixed_arch(), ensure_ascii=False),
                        encoding="utf-8")

        report = integrity.check_archive(path, pool_keys=frozenset())
        missed = [f for f in report.findings if f.code == "negative_not_pooled"]
        # 池是空的 ⇒ 真欠账（正常站那条）照报；但被剔的那条**不报**
        assert [f.where for f in missed] == ["$.outcomes[1]"]
        assert all(QQ not in f.detail for f in missed)


# ── 回填 ──────────────────────────────────────────────────────────


class TestPoolAddDoesNotReAdd:
    def test_被剔除的行不再被回填(self, tmp_path):
        """回填（人工补判后并池）会把剔掉的行**又并回池里**——
        池里混进不该训的站，而且**不报错**。"""
        path = tmp_path / "T001__r1.json"
        path.write_text(json.dumps(_mixed_arch(), ensure_ascii=False),
                        encoding="utf-8")

        cands = pa.collect_candidates(tmp_path)
        urls = [c.url for c in cands]
        assert QQ not in urls
        assert SITE in urls

    def test_回填不会把剔掉的行写进池(self, tmp_path):
        path = tmp_path / "T001__r1.json"
        path.write_text(json.dumps(_mixed_arch(), ensure_ascii=False),
                        encoding="utf-8")

        out = pa.add_from_root(tmp_path, dry_run=False)
        assert out["added"] == 1
        assert [r["url"] for r in _pool_rows(tmp_path)] == [SITE]
