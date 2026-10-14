"""``evaluation/cli`` 的测试。

重点不是「能不能跑」，而是**报表口径**：不可判定必须与已判分开，
复核档必须被优先取到——这两条错了，报表上的数字会正���地反映错误结论。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from trajectory_pipeline.evaluation import cli as ecli


def _archive(path, *, task_id="T001", run_id="r1", outcomes=None, steps=None):
    """写一份最小可切样本的 P1 存档。"""
    path.write_text(json.dumps({
        "task_id": task_id,
        "run_id": run_id,
        "title": "功夫",
        "user_prompt": "我要看功夫",
        "steps": steps if steps is not None else [],
        "outcomes": outcomes if outcomes is not None else [],
        "visits": [],
    }, ensure_ascii=False), encoding="utf-8")
    return path


class _V:
    """最小 verdict 替身——只带 CLI 真正读到的字段。"""
    def __init__(self, score, veto=False):
        self.score, self.kind = score, "scored"
        self.reasons = ("分支走错",)
        self.is_veto = veto

    def to_json(self):
        return {"dimension": "D-9", "score": self.score, "kind": self.kind,
                "veto": self.is_veto, "reasons": list(self.reasons)}


def _fake_sample(monkeypatch, *, n, score, veto=False, branch=None,
                 legacy=0, version="", p1_missing=0):
    """把切样本与打分都换掉——口径测试不该依赖 P1 的字段细节。

    ``legacy`` 是「前几条缺 ``run_config``」的条数，用来测分档；
    ``version`` 是 P1 写入的 ``schema_version``，空 = 那份存档没这个键；
    ``p1_missing`` 是「前几条的 outcome 里压根没有 ``site_url`` 键」，
    用来测缺件分布（那一项与 ``legacy`` 是**两回事**，别合并）。
    """
    from types import SimpleNamespace
    samples = []
    for i in range(n):
        degraded = (ecli.LEGACY_MARKER,) if i < legacy else ()
        samples.append(SimpleNamespace(
            sample_id=f"T001__r1__site-{i}",
            degraded_from=degraded,
            schema_version=version,
            outcome=SimpleNamespace(
                question="site.find_play_control", branch=branch, url="a.com",
                source="human",
                p1_missing_fields=(("site_url",) if i < p1_missing else ()))))
    monkeypatch.setattr(ecli, "split_archive", lambda p: samples)
    monkeypatch.setitem(ecli.SCORERS, "d9", lambda s: _V(score, veto))


class Test选档:
    def test_优先取复核档(self, tmp_path):
        """人工确认的正样本只存在于复核档里。选错档 = W1 正样本清零，
        而报表上只是「成功数少了几条」，看不出是这个原因。"""
        _archive(tmp_path / "T001__r1.json", outcomes=[
            {"url": "a.com", "branch": "no_play_control", "evidence": "e",
             "question": "site.find_play_control"}])
        _archive(tmp_path / "T001__r1.reviewed.json", outcomes=[
            {"url": "a.com", "branch": None, "source": "human",
             "succeeded": True, "evidence": "e",
             "question": "site.find_play_control"}])
        picked = ecli._picked(tmp_path, None)
        assert [p.name for p in picked] == ["T001__r1.reviewed.json"]

    def test_不选原档两次(self, tmp_path):
        _archive(tmp_path / "T001__r1.json")
        _archive(tmp_path / "T001__r1.reviewed.json")
        assert len(ecli._picked(tmp_path, None)) == 1

    def test_跳过探针取证(self, tmp_path):
        (tmp_path / "obscura_tools.json").write_text("{}", encoding="utf-8")
        _archive(tmp_path / "T001__r1.json")
        assert [p.name for p in ecli._picked(tmp_path, None)] == ["T001__r1.json"]

    def test_limit截断(self, tmp_path):
        for i in range(3):
            _archive(tmp_path / f"T001__r{i}.json")
        assert len(ecli._picked(tmp_path, 2)) == 2


class Test口径:
    def test_不可判定不混进分布(self, tmp_path, monkeypatch):
        """``score=None``（不可判定）渲染成 0 分/1 分就是凭空发通行证。"""
        _fake_sample(monkeypatch, n=3, score=None)
        _archive(tmp_path / "T001__r1.json")
        summary, _rows = ecli.evaluate(tmp_path, ["d9"])
        st = summary["dimensions"]["d9"]
        assert st["scored"] == 0
        assert st["undetermined"] == 3
        assert st["distribution"] == {}          # **没有 0 分这一档**

    def test_已判分进分布(self, tmp_path, monkeypatch):
        _fake_sample(monkeypatch, n=4, score=5)
        _archive(tmp_path / "T001__r1.json")
        st = ecli.evaluate(tmp_path, ["d9"])[0]["dimensions"]["d9"]
        assert st["distribution"] == {"5": 4}
        assert st["undetermined"] == 0

    def test_单份存档坏掉不拖垮整批(self, tmp_path, capsys):
        (tmp_path / "T001__bad.json").write_text("{ 不是 json", encoding="utf-8")
        _archive(tmp_path / "T001__ok.json")
        summary, _rows = ecli.evaluate(tmp_path, ["d5"])
        assert summary["archives"] == 2          # 两份都挑了
        err = capsys.readouterr().err
        assert "[skip]" in err and "T001__bad.json" in err

    def test_维度短名到大写维度号(self):
        assert {d: f"D-{d[1:]}" for d in ecli.SCORERS} == {
            "d2": "D-2", "d4": "D-4", "d5": "D-5", "d6": "D-6"}

    def test_未知维度拒跑(self, tmp_path, capsys):
        assert ecli.main(["--out", str(tmp_path), "--dimension", "d9"]) == 2
        assert "未知维度" in capsys.readouterr().err


class Test否决:
    def test_一票否决带得出question(self, tmp_path, monkeypatch):
        """``question``/``branch`` 挂在 ``sample.outcome`` 上。挂在 sample 上取，
        清单里就只剩一个 sample_id —— 「有理由但看不出判的是哪道题」。"""
        _fake_sample(monkeypatch, n=1, score=1, veto=True)
        _archive(tmp_path / "T001__r1.json")
        v = ecli.evaluate(tmp_path, ["d9"])[0]["vetoes"][0]
        assert v["question"] == "site.find_play_control"

    def test_branch为None渲染成成功而不是空串(self, tmp_path, monkeypatch):
        """成功样本的 branch 恒为 None。渲染成空串就与「字段没填」合并，
        清单里读起来像「这条压根没有分支」。"""
        _fake_sample(monkeypatch, n=1, score=1, veto=True, branch=None)
        _archive(tmp_path / "T001__r1.json")
        v = ecli.evaluate(tmp_path, ["d9"])[0]["vetoes"][0]
        assert v["branch"] == "(成功)"

    def test_没有否决就不列(self, tmp_path, monkeypatch):
        _fake_sample(monkeypatch, n=1, score=5, veto=False)
        _archive(tmp_path / "T001__r1.json")
        assert ecli.evaluate(tmp_path, ["d9"])[0]["vetoes"] == []


def test_没有把SCORERS改坏():
    """monkeypatch 之后原表必须还在——否则测试之间会互相污染。"""
    assert set(ecli.SCORERS) == {"d2", "d4", "d5", "d6"}




class Test批次分档:
    """分档的判据、边界与那几条最容易悄悄失效的不变式。

    判据是两级：**``schema_version`` 权威**，没有才退回 ``run_config``
    缺件代理。背景（2026-10-10 实测 38 份存档）：**全部**没有
    ``schema_version``；其中 16 份缺 ``run_config``，而那 16 份的 74 个
    ``outcome`` 一个都没有 ``site_url`` / ``play_page_url`` **键**，另
    22 份的 131 个全都有——相关性 100%。于是 D-6「100% vs 60%」完全由
    缺件决定，与数据新旧无关。

    所以**档名不许带年代**：那两档出自同一次采集，叫「旧批次 / 当前
    批次」就是替没有版本号的存档断言新旧。
    """

    def test_有版本号时按版本分档(self):
        """**权威判据**。认得出来就直接按版本分，不看任何缺件。"""
        assert ecli._batch_of(SimpleNamespace(
            schema_version="2", degraded_from=())) == "v2"
        assert ecli._batch_of(SimpleNamespace(
            schema_version="2", degraded_from=(ecli.LEGACY_MARKER,))) == "v2"

    def test_有版本号时不被缺件代理改判(self):
        """**这条最容易悄悄失效**：某次 run 因崩溃漏写 ``run_config``，
        它是 v2 的新存档，却被代理改判到「未标版本·缺 run_config」那档
        ——问题被记到一个「已知缺件」的名下，报表上却没有任何东西显示
        这是误分。"""
        assert ecli._batch_of(SimpleNamespace(
            schema_version="2",
            degraded_from=(ecli.LEGACY_MARKER, "steps"))) == "v2"

    def test_无版本号才用缺件代理(self):
        assert ecli._batch_of(SimpleNamespace(
            schema_version="", degraded_from=(ecli.LEGACY_MARKER,))) \
            == ecli.BATCH_UNVERSIONED_MISSING
        assert ecli._batch_of(SimpleNamespace(
            schema_version="", degraded_from=())) == ecli.BATCH_UNVERSIONED
        assert ecli._batch_of(SimpleNamespace(
            schema_version="", degraded_from=("no_body",))) \
            == ecli.BATCH_UNVERSIONED
        # 没有这个属性（外部构造的样本）也不能炸，按无版本号走
        assert ecli._batch_of(SimpleNamespace()) == ecli.BATCH_UNVERSIONED

    def test_无版本号的档名不断言年代(self):
        """**这条最容易悄悄失效**：现存 38 份**全部**没有
        ``schema_version``，代理切出的两档同批。档名一旦叫「旧批次 /
        当前批次」，读的人就会把那 144 条当成新数据——而它们同样没
        版本号。"""
        for name in (ecli.BATCH_UNVERSIONED, ecli.BATCH_UNVERSIONED_MISSING):
            assert "批次" not in name
            assert "当前" not in name and "旧" not in name

    def test_依据随分档一起报出去(self, tmp_path, monkeypatch):
        """分栏只印数字不印依据，读的人会把**推断**当成事实——
        而依据从推断变成事实的那天，报表上没有任何东西会提醒他。"""
        _fake_sample(monkeypatch, n=2, score=5, legacy=1)
        _archive(tmp_path / "T001__r1.json")
        split = ecli.evaluate(tmp_path, ["d9"])[0]["batch_split"]
        assert split["basis"].startswith("无版本号")
        assert split["proxied"] is True

        _fake_sample(monkeypatch, n=2, score=5, version="2")
        split = ecli.evaluate(tmp_path, ["d9"])[0]["batch_split"]
        assert split["basis"] == "schema_version=2"
        assert split["proxied"] is False

    def test_混合批次如实说混合(self, tmp_path, monkeypatch):
        """一部分有版本号一部分没有——写成单一依据就是在替其中一半做主。"""
        from types import SimpleNamespace as NS
        samples = [NS(sample_id="a", schema_version="2", degraded_from=(),
                      outcome=NS(p1_missing_fields=())),
                   NS(sample_id="b", schema_version="", degraded_from=(),
                      outcome=NS(p1_missing_fields=()))]
        monkeypatch.setattr(ecli, "split_archive", lambda p: samples)
        monkeypatch.setitem(ecli.SCORERS, "d9", lambda s: _V(5))
        _archive(tmp_path / "T001__r1.json")
        split = ecli.evaluate(tmp_path, ["d9"])[0]["batch_split"]
        assert "混合" in split["basis"]
        assert set(split["counts"]) == {"v2", ecli.BATCH_UNVERSIONED}

    def test_混合批次仍然打代理警告(self, tmp_path, monkeypatch, capsys):
        """**这是 2026-10-10 修的那个缺陷，门禁必须钉住它**。

        原来按 ``basis.startswith("无版本号")`` 判要不要打警告，而混合档
        的 basis 是「混合…」——**新存档落盘的那一刻，恰恰是无版本号那
        批（现存 221 条）仍在用缺件代理分档、警告却自动消失**的那一刻。
        最该警告的状态变成最安静的，且没有任何东西显示发生过。
        """
        from types import SimpleNamespace as NS
        samples = [NS(sample_id="a", schema_version="2", degraded_from=(),
                      outcome=NS(p1_missing_fields=())),
                   NS(sample_id="b", schema_version="",
                      degraded_from=(ecli.LEGACY_MARKER,),
                      outcome=NS(p1_missing_fields=()))]
        monkeypatch.setattr(ecli, "split_archive", lambda p: samples)
        monkeypatch.setitem(ecli.SCORERS, "d9", lambda s: _V(5))
        _archive(tmp_path / "T001__r1.json")
        summary, rows = ecli.evaluate(tmp_path, ["d9"])
        assert summary["batch_split"]["proxied"] is True
        ecli._print(summary, rows, per_sample=False, undetermined_only=False)
        out = capsys.readouterr().out
        assert "推测" in out and "没有 schema_version" in out

    def test_分档不改总分母(self, tmp_path, monkeypatch):
        """**这条最容易悄悄失效**：把某一档从分母里拿掉就叫「剔除难看
        的」，而且剔除后每个数字都好看，没人知道曾经少算了什么。"""
        _fake_sample(monkeypatch, n=10, score=5, legacy=4)
        _archive(tmp_path / "T001__r1.json")
        summary = ecli.evaluate(tmp_path, ["d9"])[0]
        st = summary["dimensions"]["d9"]
        assert st["scored"] == 10                      # 两档加起来
        assert summary["samples"] == 10
        # 分栏只解释差异，不替代全量
        b = summary["batches"]
        assert b[ecli.BATCH_UNVERSIONED]["samples"] == 6
        assert b[ecli.BATCH_UNVERSIONED_MISSING]["samples"] == 4
        assert sum(x["samples"] for x in b.values()) == 10

    def test_不可判定也分档(self, tmp_path, monkeypatch):
        """缺件那档的高不可判定率正是要显示的东西，不分档等于白分。"""
        _fake_sample(monkeypatch, n=6, score=None, legacy=4)
        _archive(tmp_path / "T001__r1.json")
        b = ecli.evaluate(tmp_path, ["d9"])[0]["batches"]
        missing = ecli.BATCH_UNVERSIONED_MISSING
        assert b[missing]["dimensions"]["d9"]["undetermined"] == 4
        assert b[ecli.BATCH_UNVERSIONED]["dimensions"]["d9"]["undetermined"] == 2
        assert b[missing]["dimensions"]["d9"]["distribution"] == {}

    def test_只有一档时不打印分档(self, tmp_path, monkeypatch, capsys):
        """空的分栏只会让人去找「另一档在哪」，找不到就以为功能坏了。"""
        _fake_sample(monkeypatch, n=3, score=5)
        _archive(tmp_path / "T001__r1.json")
        summary, rows = ecli.evaluate(tmp_path, ["d9"], per_sample=True)
        ecli._print(summary, rows, per_sample=True, undetermined_only=False)
        out = capsys.readouterr().out
        assert "── 分档（" not in out
        assert "[当" not in out                    # 同档时不加标记列

    def test_代理分档时明说靠推测(self, tmp_path, monkeypatch, capsys):
        """分栏印档名却不印依据，读的人会当成既定事实。"""
        _fake_sample(monkeypatch, n=4, score=5, legacy=2)
        _archive(tmp_path / "T001__r1.json")
        summary, rows = ecli.evaluate(tmp_path, ["d9"])
        ecli._print(summary, rows, per_sample=False, undetermined_only=False)
        out = capsys.readouterr().out
        assert "没有 schema_version" in out and "推测" in out

    def test_有版本号时不打推测警告(self, tmp_path, monkeypatch, capsys):
        from types import SimpleNamespace as NS
        samples = [NS(sample_id=f"s{i}", schema_version="2", degraded_from=(),
                      outcome=NS(p1_missing_fields=()))
                   for i in range(3)]
        monkeypatch.setattr(ecli, "split_archive", lambda p: samples)
        monkeypatch.setitem(ecli.SCORERS, "d9", lambda s: _V(5))
        _archive(tmp_path / "T001__r1.json")
        summary, rows = ecli.evaluate(tmp_path, ["d9"])
        ecli._print(summary, rows, per_sample=False, undetermined_only=False)
        out = capsys.readouterr().out
        assert "推测" not in out

    def test_全批无版本号时不讲批次的话(self, tmp_path, monkeypatch, capsys):
        """「老批次跑在字段补齐之前」只在**确实存在版本档**时可讲。
        全批都没版本号时，那句话在解释一个不存在的分界。"""
        _fake_sample(monkeypatch, n=4, score=5, legacy=2)
        _archive(tmp_path / "T001__r1.json")
        summary, rows = ecli.evaluate(tmp_path, ["d9"])
        ecli._print(summary, rows, per_sample=False, undetermined_only=False)
        out = capsys.readouterr().out
        assert "老批次" not in out and "字段补齐之前" not in out

    def test_逐条行带档位(self, tmp_path, monkeypatch):
        _fake_sample(monkeypatch, n=3, score=5, legacy=1)
        _archive(tmp_path / "T001__r1.json")
        _summary, rows = ecli.evaluate(tmp_path, ["d9"])
        assert [r["batch"] for r in rows] == [
            ecli.BATCH_UNVERSIONED_MISSING,
            ecli.BATCH_UNVERSIONED, ecli.BATCH_UNVERSIONED]
        assert rows[0]["degraded_from"] == [ecli.LEGACY_MARKER]
        assert rows[0]["schema_version"] == ""


class Test缺件分布:
    """**这是事实那张表**：键在不在 P1 里，不需要推断。

    它是「把缺件当批次」那个循环论证的正解——实测 38 份存档里
    ``outcome`` 缺 ``site_url`` 键与存档缺 ``run_config`` 相关性 100%，
    分档看着在讲年代，实际在讲字段。
    """

    def test_逐条行带p1缺件(self, tmp_path, monkeypatch):
        """``degraded_from`` 只记存档级缺件；D-6 的主因在 outcome 上，
        不带出来就等于让「缺键」与「空值」在报表里同形。"""
        _fake_sample(monkeypatch, n=3, score=5, p1_missing=2)
        _archive(tmp_path / "T001__r1.json")
        _summary, rows = ecli.evaluate(tmp_path, ["d9"])
        assert [r["p1_missing_fields"] for r in rows] == [
            ["site_url"], ["site_url"], []]

    def test_缺件分组统计出分(self, tmp_path, monkeypatch):
        _fake_sample(monkeypatch, n=4, score=5, p1_missing=2)
        _archive(tmp_path / "T001__r1.json")
        _summary, rows = ecli.evaluate(tmp_path, ["d9"])
        stats = ecli._marker_stats(rows, ["d9"])
        assert stats["outcome.site_url"]["samples"] == 2
        assert stats["outcome.site_url"]["scored"] == {"d9": 2}

    def test_缺件与存档级标记分开算(self, tmp_path, monkeypatch):
        """两者都叫「缺件」但不是一回事，混进一栏就分不出是谁造成的。"""
        _fake_sample(monkeypatch, n=4, score=5, legacy=2, p1_missing=2)
        _archive(tmp_path / "T001__r1.json")
        _summary, rows = ecli.evaluate(tmp_path, ["d9"])
        assert set(ecli._marker_stats(rows, ["d9"])) == {
            f"archive.{ecli.LEGACY_MARKER}", "outcome.site_url"}

    def test_没有缺件时不编行(self, tmp_path, monkeypatch, capsys):
        _fake_sample(monkeypatch, n=3, score=5)
        _archive(tmp_path / "T001__r1.json")
        summary, rows = ecli.evaluate(tmp_path, ["d9"])
        ecli._print_markers(summary, rows)
        out = capsys.readouterr().out
        assert "无任何缺件" in out and "outcome." not in out

    def test_对照行始终印出来(self, tmp_path, monkeypatch, capsys):
        """每行的分母是组内条数，没有对照行就看不出缺件贡献了多少。"""
        _fake_sample(monkeypatch, n=4, score=5, p1_missing=2)
        _archive(tmp_path / "T001__r1.json")
        summary, rows = ecli.evaluate(tmp_path, ["d9"])
        ecli._print_markers(summary, rows)
        out = capsys.readouterr().out
        assert "outcome.site_url" in out and "全部样本（对照）" in out

    def test_只有一档时也印(self, tmp_path, monkeypatch, capsys):
        """只有一档时没有分栏解释，缺件分布是唯一能说明不可判定的东西。"""
        _fake_sample(monkeypatch, n=3, score=None, p1_missing=3)
        _archive(tmp_path / "T001__r1.json")
        summary, rows = ecli.evaluate(tmp_path, ["d9"])
        ecli._print_markers(summary, rows)
        assert "outcome.site_url" in capsys.readouterr().out


# ── LLM judge 接入（方案 10-§四 --include-llm-judge 默认关） ────────────


class TestLLMJudge接入:
    """``--include-llm-judge`` 默认关的契约——LLM judge 与代码 judge
    **不**默认同跑（CLAUDE.md「生产链禁止投票/集成」红线）。

    默认下 d3 / d9 是「未知维度」（同代码 judge 之外的名字一致），
    带 ``--include-llm-judge`` 时才进入 dispatch。
    """

    def test_LLM_SCORERS_注册了d3与d9(self):
        """LLM judge 注册表与 SCORERS 独立维护——SCORERS 仍是 4 个代码 judge
        （``test_门禁`` 钉），LLM_SCORERS 含 d3 + d9。"""
        assert set(ecli.LLM_SCORERS) == {"d3", "d9"}

    def test_SCORERS_仍是4个(self):
        """SCORERS **不**被 LLM judge 污染——见 ``test_门禁``。"""
        assert set(ecli.SCORERS) == {"d2", "d4", "d5", "d6"}

    def test_d3_默认未知维度(self, tmp_path, capsys):
        """不带 ``--include-llm-judge`` 时，``d3`` 走「未知维度」出口。"""
        rc = ecli.main(["--out", str(tmp_path), "--dimension", "d3"])
        assert rc == 2
        assert "未知维度" in capsys.readouterr().err

    def test_d9_默认未知维度(self, tmp_path, capsys):
        """不带 ``--include-llm-judge`` 时，``d9`` 走「未知维度」出口。"""
        rc = ecli.main(["--out", str(tmp_path), "--dimension", "d9"])
        assert rc == 2
        assert "未知维度" in capsys.readouterr().err

    def test_带flag_d3可跑(self, tmp_path):
        """``--include-llm-judge`` + ``--dimension d3`` —— d3 维度进入 dispatch
        （llm_client=None → 全出 undetermined，但 dispatch 走通）。"""
        _archive(tmp_path / "T001__r1.json")
        summary, _rows = ecli.evaluate(
            tmp_path, ["d3"], include_llm_judge=True)
        assert "d3" in summary["dimensions"]

    def test_带flag_d9可跑(self, tmp_path):
        """``--include-llm-judge`` + ``--dimension d9`` —— d9 走快路径
        （CLI **不**主动调 gates.gate()，cost = 0）。"""
        _archive(tmp_path / "T001__r1.json")
        summary, _rows = ecli.evaluate(
            tmp_path, ["d9"], include_llm_judge=True)
        assert "d9" in summary["dimensions"]

    def test_带flag_d3与代码judge同跑(self, tmp_path):
        """``--include-llm-judge`` 不局限走 LLM——代码 judge + LLM judge 同跑
        是显式开关的唯一使命（默认下不启，避免静默集成）。"""
        _archive(tmp_path / "T001__r1.json", outcomes=[
            {"url": "a.com", "branch": "no_play_control", "evidence": "e",
             "question": "site.find_play_control"}])
        summary, _rows = ecli.evaluate(
            tmp_path, ["d5", "d3", "d9"], include_llm_judge=True)
        assert set(summary["dimensions"]) == {"d5", "d3", "d9"}
