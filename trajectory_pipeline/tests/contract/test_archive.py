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