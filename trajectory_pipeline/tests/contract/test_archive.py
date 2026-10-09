"""``executor.archive`` 单测——重点是凭据红线的 **fail-closed** 行为。

红线不能靠「跑一遍看看有没有炸」来保证：那只能证明**当前**没炸。
这里逐个特征构造命中样本，断言扫描器认得出来，且写盘被拒。
"""

from __future__ import annotations

import json

import pytest

from trajectory_pipeline.executor.archive import (
    CredentialLeak,
    P1Archive,
    scan_credentials,
)


class FakeRecord:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_json(self) -> dict:
        return self._payload


#: 每条凭据特征配一个真实形态的样本。逐条构造而非「跑一遍看看炸不炸」——
#: 后者只能证明**当前**没炸，证明不了扫描器认得每一种形态。
LEAK_SAMPLES = [
    ("Authorization: Bearer abc123", "authorization 头"),
    ("token=bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig", "bearer token"),
    ("key sk-abcdefghijklmnopqrstuvwxyz1234", "OpenAI 风格 key"),
    ("key sk-ant-api03-abcdefghijklmnopqrstuv", "Anthropic key"),
    ("AKIAIOSFODNN7EXAMPLE", "AWS access key"),
    ("AIzaSyA1234567890A1234567890A1234567890", "Google API key"),
    ("https://user:secretpass@example.com/x", "URL 内嵌口令"),
    ("Set-Cookie: sid=abc", "cookie"),
    ("-----BEGIN RSA PRIVATE KEY-----", "PEM 私钥"),
    ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "GitHub token"),
]


@pytest.mark.parametrize("leak,label", LEAK_SAMPLES)
def test_凭据特征_逐条命中(leak, label):
    assert scan_credentials(leak), f"未识别: {label}"


def test_正常内容不误报():
    clean = json.dumps({
        "url": "https://www.iqiyi.com/v_19rr7depxo.html",
        "page_title": "功夫 在线观看",
        "evidence": "命中播放控件 ref=e5 label='立即播放'",
        "body_preview": "该影片由爱奇艺提供在线观看",
    }, ensure_ascii=False)
    assert scan_credentials(clean) == []


@pytest.mark.parametrize("suffix", ["", "A", "XYZ", '"', ",后跟中文"])
def test_尾部不锚定_后接字符仍命中(suffix):
    """早期版本用 ``\\b`` 锚定尾部，结果 key 恰好 39 字符时命中、
    40 字符就漏。而**后接其他字符恰恰是最该拦住的形态**（被拼接、被截断）。"""
    key = "AIzaSy" + "A1234567890" * 3 + "A1234567890"[:10] + suffix
    assert scan_credentials(key), f"后接 {suffix!r} 时漏检"


class TestFailClosed:
    def test_命中即拒写(self, tmp_path):
        """**拒绝写盘**而不是「写进去打个警告」——
        警告会被忽略，文件一旦落盘就可能被推到 Label Studio。"""
        arc = P1Archive(tmp_path)
        record = FakeRecord({"url": "https://x.test",
                             "authorization": "Bearer sk-abcdefghijklmnopqrst"})
        with pytest.raises(CredentialLeak):
            arc.write(record, task_id="T001")
        assert list(tmp_path.glob("*.json")) == [], "凭据命中时不得留下任何文件"

    def test_不命中时正常落盘(self, tmp_path):
        arc = P1Archive(tmp_path)
        path = arc.write(FakeRecord({"url": "https://x.test"}), task_id="T001")
        assert path.exists()
        assert json.loads(path.read_text(encoding="utf-8"))["url"] == "https://x.test"

    def test_嵌套深处也扫得到(self, tmp_path):
        """凭据常常藏在 observations 数组里，浅层扫描会漏。"""
        arc = P1Archive(tmp_path)
        record = FakeRecord({"visits": [{"obs": {"headers": {
            "Authorization": "Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789"}}}]})
        with pytest.raises(CredentialLeak):
            arc.write(record, task_id="T001")


class TestAtomicWrite:
    def test_不留_tmp_残留(self, tmp_path):
        arc = P1Archive(tmp_path)
        arc.write(FakeRecord({"a": 1}), task_id="T001")
        assert list(tmp_path.glob("*.tmp")) == []

    def test_文件名含_task_与_run(self, tmp_path):
        arc = P1Archive(tmp_path)
        path = arc.write(FakeRecord({"a": 1}), task_id="T001", run_id="abc123")
        assert path.name == "T001__abc123.json"

    def test_按_task_列出历史(self, tmp_path):
        """失败归因（模块 5）最常做的事是「某个 task 重跑后对比历史」。"""
        arc = P1Archive(tmp_path)
        arc.write(FakeRecord({"a": 1}), task_id="T001", run_id="r1")
        arc.write(FakeRecord({"a": 2}), task_id="T001", run_id="r2")
        arc.write(FakeRecord({"a": 3}), task_id="T002", run_id="r3")
        assert len(arc.list_runs("T001")) == 2

    def test_默认落在新树_output_下(self):
        """不能碰仓库根 ``output/``——那是存量管线的产物（决策 D8）。"""
        root = P1Archive().root
        assert root.name == "pipeline"
        assert "trajectory_pipeline" in str(root)
        assert root.parent.parent.name == "trajectory_pipeline"


class TestCandidateSourceAudit:
    def test_候选来源标注进存档(self, tmp_path):
        """存档里必须能分辨候选是启发式取的还是感知层选的——
        否则 W1 的站点会被误读成「规则版选对了」。"""
        from trajectory_pipeline.executor.orchestrator import RunRecord

        rec = RunRecord(task_id="T001", title="功夫", query="q",
                        search_url="u", candidate_source="heuristic")
        arc = P1Archive(tmp_path)
        path = arc.write(rec, task_id="T001")
        assert json.loads(path.read_text(encoding="utf-8"))["candidate_source"] == "heuristic"


# ═══════════════════════════════════════════════════════════════════════
# 负样本池
#
# 「每条失败分支都必须有对应样本入库」是硬要求，所以并池与 P1 同出口
# （见 P1Archive.write）。这里锁的是三条会静默失效的性质。
# ═══════════════════════════════════════════════════════════════════════


def _ledger(*outcomes):
    """造一个最小 ledger。只要有 ``negative_samples()`` 即可。"""
    class _L:
        pass
    led = _L()
    led.negative_samples = lambda: list(outcomes)
    return led


def _outcome(url: str, branch: str | None, **kw):
    from trajectory_pipeline.executor.branches import SiteOutcome

    return SiteOutcome(
        url=url, branch=branch, evidence=kw.pop("evidence", "证据"),
        question=kw.pop("question", ""), source=kw.pop("source", "rule"),
        reached=kw.pop("reached", False),
    )


def _record(outcomes, provenance=None):
    class _R:
        def to_json(self):
            return {"task_id": "T001", "outcomes": [o.to_json() for o in outcomes]}

    rec = _R()
    rec.ledger = _ledger(*outcomes)
    rec.provenance = provenance or {}
    return rec


def _pool_rows(tmp_path) -> list[dict]:
    """读池。坏行**跳过**——与生产侧 ``_load_negative_keys`` 同一处置。"""
    path = tmp_path / "negative.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


class TestNegativePool:
    def test_真负样本入池(self, tmp_path):
        arc = P1Archive(tmp_path)
        arc.write(_record([_outcome("https://a.test/x", "no_play_control")]),
                  task_id="T001")
        rows = _pool_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["branch"] == "no_play_control"
        assert rows[0]["branch_label"] == "页面上没有播放控件"
        assert rows[0]["task_id"] == "T001"

    def test_unresolved不入池(self, tmp_path):
        """``unresolved`` 是「没判出来」，混进池子会毁掉
        「这里真的看不了」这条信号——负样本池的全部价值就在这句话上。"""
        arc = P1Archive(tmp_path)
        arc.write(_record([
            _outcome("https://a.test/x", "unresolved"),
            _outcome("https://b.test/x", "trailer_suspect"),
            _outcome("https://c.test/x", "no_play_control"),
        ]), task_id="T001")
        rows = _pool_rows(tmp_path)
        assert [r["url"] for r in rows] == ["https://c.test/x"]

    def test_成功不入池(self, tmp_path):
        arc = P1Archive(tmp_path)
        arc.write(_record([_outcome("https://a.test/x", None)]), task_id="T001")
        assert _pool_rows(tmp_path) == []

    def test_跨run累积不覆盖(self, tmp_path):
        """追加而非覆盖：覆盖会让「这批比上批少」这个信息一起消失。"""
        arc = P1Archive(tmp_path)
        arc.write(_record([_outcome("https://a.test/1", "no_play_control")]),
                  task_id="T001", run_id="r1")
        arc.write(_record([_outcome("https://a.test/2", "unreachable_hard")]),
                  task_id="T002", run_id="r2")
        assert len(_pool_rows(tmp_path)) == 2
        # 两个 P1 存档也都在
        assert len(arc.list_runs("T001")) == 1
        assert len(arc.list_runs("T002")) == 1

    def test_同task同站同分支不重复(self, tmp_path):
        """重跑产生同样结论时是**一条**，不是两条。"""
        arc = P1Archive(tmp_path)
        out = _outcome("https://a.test/x", "no_play_control")
        arc.write(_record([out]), task_id="T001", run_id="r1")
        arc.write(_record([out]), task_id="T001", run_id="r2")
        assert len(_pool_rows(tmp_path)) == 1

    def test_同站不同task是两条(self, tmp_path):
        """⚠️ 去重键含 task_id：同一站在不同 task 下失败，判分上下文不同，
        是**两条**训练信号。用 url 单键会误合并，按 task 切片时少样本。"""
        arc = P1Archive(tmp_path)
        out = _outcome("https://a.test/x", "no_play_control")
        arc.write(_record([out]), task_id="T001", run_id="r1")
        arc.write(_record([out]), task_id="T002", run_id="r2")
        rows = _pool_rows(tmp_path)
        assert len(rows) == 2
        assert {r["task_id"] for r in rows} == {"T001", "T002"}

    def test_同站同task不同分支是两条(self, tmp_path):
        arc = P1Archive(tmp_path)
        arc.write(_record([_outcome("https://a.test/x", "no_play_control")]),
                  task_id="T001", run_id="r1")
        arc.write(_record([_outcome("https://a.test/x", "component_unverified")]),
                  task_id="T001", run_id="r2")
        assert len(_pool_rows(tmp_path)) == 2

    def test_切片轴随样本落盘(self, tmp_path):
        """负样本池是**按切片消费**的，只存 url + branch 等于把
        就在手边的 provenance 丢掉。"""
        arc = P1Archive(tmp_path)
        arc.write(
            _record([_outcome("https://a.test/x", "no_play_control")],
                    provenance={"persona_id": "office-正式", "content_tier": "B",
                                "popularity": "腰部", "genre": "喜剧"}),
            task_id="T001",
        )
        row = _pool_rows(tmp_path)[0]
        assert row["persona_id"] == "office-正式"
        assert row["content_tier"] == "B"
        assert row["genre"] == "喜剧"

    def test_无真负样本时池不被创建(self, tmp_path):
        """全是 unresolved 的一批不该凭空建出池文件——空文件会被
        下游读成「跑过了，一条负样本都没有」。"""
        arc = P1Archive(tmp_path)
        arc.write(_record([_outcome("https://a.test/x", "unresolved")]),
                  task_id="T001")
        assert not (tmp_path / "negative.jsonl").exists()

    def test_凭据命中时池也不被写(self, tmp_path):
        """并池与 P1 同属一个出口，红线不能只守 P1——负样本池是要推给
        Label Studio 的。"""
        arc = P1Archive(tmp_path)
        bad = _outcome("https://a.test/x", "no_play_control",
                       evidence="Authorization: Bearer sk-abcdefghijklmnopqrst")
        with pytest.raises(CredentialLeak):
            arc.write(_record([bad]), task_id="T001")
        assert _pool_rows(tmp_path) == []

    def test_池内截断行被丢弃(self, tmp_path):
        """文件被写坏时末行可能是半截 JSON。丢弃而非补全——
        补全会造出一条**不存在的**负样本。"""
        arc = P1Archive(tmp_path)
        arc.write(_record([_outcome("https://a.test/1", "no_play_control")]),
                  task_id="T001", run_id="r1")
        pool = tmp_path / "negative.jsonl"
        with pool.open("a", encoding="utf-8") as fh:
            fh.write('{"task_id": "T002", "url": "https://b.test')   # 无换行结尾
        # 坏行在末尾；新条目仍能并入，且不会因坏行崩溃
        arc.write(_record([_outcome("https://c.test/3", "unreachable_hard")]),
                  task_id="T003", run_id="r3")
        urls = {r["url"] for r in _pool_rows(tmp_path)}
        assert "https://a.test/1" in urls
        assert "https://c.test/3" in urls, "新并入的负样本被坏行粘连后一起读不出来"

    def test_截断行不粘连新条目(self, tmp_path):
        """无换行的坏行必须与后写的条目**分行**，否则一坏俱坏。

        追加前不补换行时，新 blob 会直接接在半截 JSON 后面，于是
        「坏行只丢 1 条」退化成「刚跑出来的那条也一起丢」——
        而负样本池少条目没人会发现。
        """
        arc = P1Archive(tmp_path)
        pool = tmp_path / "negative.jsonl"
        pool.write_text('{"task_id": "T000", "url": "https://x.test', encoding="utf-8")
        arc.write(_record([_outcome("https://c.test/3", "no_play_control")]),
                  task_id="T003", run_id="r3")
        lines = pool.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2, "坏行与新条目没有被分行"
        assert json.loads(lines[1])["url"] == "https://c.test/3"