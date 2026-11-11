"""**LLM 熔断 + 批次标不可用**（H-31 · all80 批次事故 P1-3）的门禁。

事故形状
--------
all80 批次（2026-11-10 18:12 → 11-11 08:53）里，LLM 后端从 08:48:39 起
**持续返 401**，一直持续到收尾——**170 次**。每一次都在等一个不可能有
答案的请求，而判断点全程 fail-closed ⇒ 那批的 ``unresolved`` 占 83 %、
**字节量占全批 35 %**：结构完整、看着最像正常轨迹、实则判分不可信。

这里封四件事：

    1. **连续 4xx ⇒ 熔断**（阈值默认 3）——熔断打开后**一个请求都不发**；
    2. **429/408 不计入熔断**——限流与超时是可恢复的，把它读成
       「后端死了」会白白把一整批判死；
    3. **熔断跨客户端共享**（批次级）——客户端是**每 task 一个**，
       逐 task 计数拦不住跨 task 的连续失败（all80 那 170 次就是跨
       6 条 task 打空的）；
    4. **判分不可信 ⇒ 不进负样本池、不进训练集**——但**证据留着**
       （隔离 ≠ 删除）。

⚠️ **P1 存档照落**：那是**证据**（人工复核要看的就是它），隔离掉的
是**训练语料**。删存档会让「这批判分被污染了」这件事**无痕可查**。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from trajectory_pipeline.assembler.schema import split_archive_report
from trajectory_pipeline.assembler.task_view import build_task_view
from trajectory_pipeline.executor import cli
from trajectory_pipeline.executor.archive import P1Archive
from trajectory_pipeline.executor.branches import RunLedger
from trajectory_pipeline.executor.orchestrator import RunRecord
from trajectory_pipeline.llm import client as llm_client_mod
from trajectory_pipeline.llm.client import (
    CIRCUIT_BREAKER_THRESHOLD,
    ENV_CIRCUIT_THRESHOLD,
    NON_TRIPPING_4XX,
    CircuitBreaker,
    LLMClient,
    LLMConfig,
)


# ─────────────── 替身 ───────────────


class _FakeResp:
    """假 HTTP 响应。只带客户端真的用到的两个面：状态码与 JSON 体。"""

    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeHTTP:
    """假 httpx 客户端：**数请求数**。

    ⚠️ **请求数是这个测试的主角**：熔断的意义就是「别再发了」，
    而「别再发了」只能靠**没发出去**来证明，不能靠「发得快」。
    """

    OK_BODY = {
        "choices": [{"message": {"content": "好"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }

    def __init__(self, status: int = 401) -> None:
        self.status = status
        self.requests = 0

    def post(self, url, json=None, headers=None):        # noqa: A002
        self.requests += 1
        return _FakeResp(self.status, self.OK_BODY)

    def close(self) -> None:
        pass


def _client(status: int = 401, *, breaker=None, max_retries: int = 0):
    """造一个**不联网**的客户端（假 HTTP 层已挂好）。"""
    cfg = LLMConfig(
        base_url="http://fake.invalid",
        model="fake-model",
        max_retries=max_retries,
    )
    cl = LLMClient(cfg, breaker=breaker)
    http = _FakeHTTP(status)
    cl._client = http
    return cl, http


# ─────────────── 熔断器本身 ───────────────


class Test熔断器本身:
    def test_连续4xx到阈值才打开(self):
        b = CircuitBreaker(3)
        assert not b.open, "还没到阈值就熔断 ⇒ 一次抖动就掐死整批"
        b.record_4xx(401)
        b.record_4xx(401)
        assert not b.open
        b.record_4xx(401)
        assert b.open

    def test_429与408不计入连续计数(self):
        """⚠️ **限流与超时不是「后端死了」。**

        把 429 算进熔断 ⇒ 后端只是被限速，一整批却被判「判分不可信」
        而全数隔离——这是**误杀**，而误杀的代价是那一批白跑。
        """
        b = CircuitBreaker(2)
        for _ in range(5):
            b.record_4xx(429)
        assert not b.open, "429 是可恢复的限流，不许熔断"
        b.record_4xx(408)
        assert not b.open, "408 是可恢复的超时，不许熔断"
        assert 429 in NON_TRIPPING_4XX and 408 in NON_TRIPPING_4XX

    def test_成功一次清零(self):
        """后端恢复了 ⇒ 前面的失败不算数（连续，不是累计）。"""
        b = CircuitBreaker(3)
        b.record_4xx(401)
        b.record_4xx(401)
        b.record_ok()
        assert b.consecutive_4xx == 0
        assert not b.open

    def test_熔断发生过就记住(self):
        """``tripped`` 是「这批出过事」的读数，**只增不减**。"""
        b = CircuitBreaker(1)
        b.record_4xx(401)
        assert b.open
        assert b.tripped == 1
        b.record_ok()
        assert b.tripped == 1, "手动清零不许抹掉「这批出过事」"

    def test_原因文本带最后状态码(self):
        b = CircuitBreaker(1)
        b.record_4xx(401)
        why = b.reason()
        assert "401" in why, f"熔断原因里没有状态码：{why!r}"
        assert "4xx" in why

    def test_没熔断时原因是空串(self):
        assert CircuitBreaker(3).reason() == ""

    def test_阈值至少为1(self):
        """阈值 0 会让**第一次**失败就熔断——那不是熔断，那是禁用。"""
        assert CircuitBreaker(0).threshold == 1
        assert CircuitBreaker(-5).threshold == 1

    def test_默认阈值是3(self):
        assert CIRCUIT_BREAKER_THRESHOLD == 3
        assert CircuitBreaker().threshold == 3


# ─────────────── 客户端层熔断 ───────────────


class Test客户端熔断:
    def test_熔断打开后一个请求都不发(self):
        """**all80 那 170 次就是这么打空的**——每次都在等一个不可能有
        答案的请求。熔断的意义是「别再发了」。"""
        b = CircuitBreaker(3)
        cl, http = _client(401, breaker=b)
        for _ in range(5):
            assert cl.chat("s", "u") is None
        assert http.requests == 3, (
            f"熔断后还发了 {http.requests} 次请求——熔断没起作用"
        )

    def test_熔断后错误是circuit_open而不是http401(self):
        """错误文本要能区分「**熔断了**」与「**又失败了一次**」。

        前者说的是「别再试了，去修后端」；后者说的是「再试一次也许行」。
        混成一句 ⇒ 读日志的人会一直重试一个已经死透的后端。
        """
        b = CircuitBreaker(1)
        cl, _http = _client(401, breaker=b)
        cl.chat("s", "u")
        assert cl.last_call_error == "http:401"
        cl.chat("s", "u")
        assert cl.last_call_error == "circuit:open"

    def test_429不熔断请求照发(self):
        b = CircuitBreaker(2)
        cl, http = _client(429, breaker=b)
        for _ in range(4):
            cl.chat("s", "u")
        assert http.requests == 4, "429 被误当成致命错误 ⇒ 限流被熔断掐死"
        assert not b.open

    def test_5xx重试后成功会清零计数(self):
        """5xx 是**后端过载**，重试有意义；成功一次就清零。"""
        b = CircuitBreaker(2)
        cl, http = _client(200, breaker=b)
        assert cl.chat("s", "u") == "好"
        assert b.consecutive_4xx == 0
        assert http.requests == 1


# ─────────────── 批次级共享 ───────────────


class Test熔断跨客户端共享:
    def test_一个客户端熔断了另一个也不发(self):
        """⚠️ **客户端是每 task 一个**——不共享就拦不住跨 task 的连续失败。

        all80 那 170 次 401 是**跨 6 条 task** 打空的：每条 task 各发
        自己的那几次，逐 task 计数一次都没到阈值。
        """
        b = CircuitBreaker(2)
        a, http_a = _client(401, breaker=b)
        a.chat("s", "u")
        a.chat("s", "u")
        assert b.open

        c, http_c = _client(401, breaker=b)
        assert c.chat("s", "u") is None
        assert http_c.requests == 0, (
            "第二个客户端还在发请求——熔断没跨客户端生效"
        )

    def test_客户端自带的熔断器只管自己(self):
        """**不给 breaker 就自带一个**——这是兜底，不是批次语义。

        批次层必须显式传共享的那个进来（见
        :func:`trajectory_pipeline.executor.cli._make_circuit_breaker`）。
        """
        a, _ = _client(401)
        b, _ = _client(401)
        assert a.breaker is not b.breaker


# ─────────────── 批次层接线 ───────────────


def _llm_env(monkeypatch, **extra) -> None:
    monkeypatch.setenv("TRAJECTORY_LLM_BASE_URL", "http://fake.invalid")
    monkeypatch.setenv("TRAJECTORY_LLM_MODEL", "fake-model")
    monkeypatch.setenv("TRAJECTORY_PERCEPTOR", "llm")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


class Test批次层接线:
    def test_所有LLM出口共享同一个熔断器(self, monkeypatch):
        """感知层 / rationale writer / 反思 / agent 主后端**打的是同一个
        后端** ⇒ 同一个 key 坏了就是全都坏 ⇒ 必须共享一个熔断器。

        分开熔断等于让 401 从 writer 那条路继续漏出去。
        """
        _llm_env(monkeypatch)
        seen: list[Any] = []

        class _C:
            def __init__(self, cfg, *, breaker=None):
                seen.append(breaker)

        monkeypatch.setattr(llm_client_mod, "LLMClient", _C)

        breaker = cli._make_circuit_breaker()
        args = argparse.Namespace(perceptor="llm")
        cli._make_perceptor(args, "功夫", breaker)
        cli._make_rationale_writer(breaker)
        cli._make_agent_backend(breaker)

        assert seen, "没造出任何 LLM 客户端——接线没跑到"
        assert all(b is breaker for b in seen), (
            f"有出口没挂上批次熔断器：{[id(b) for b in seen]}"
        )

    def test_升配后端用自己那个熔断器(self, monkeypatch):
        """⚠️ **升配后端是另一台机器另一份凭据**——主后端 401 不代表
        升配也坏了。把两个后端塞进同一个熔断器 ⇒ 主后端一坏就把升配
        一起掐死（而升配正是「主后端不行时的救场」）。"""
        _llm_env(
            monkeypatch,
            TRAJECTORY_LLM_ESCALATE_BASE_URL="http://esc.invalid",
            TRAJECTORY_LLM_ESCALATE_MODEL="esc-model",
        )
        seen: list[Any] = []

        class _C:
            def __init__(self, cfg, *, breaker=None):
                seen.append(breaker)

        monkeypatch.setattr(llm_client_mod, "LLMClient", _C)

        breaker = cli._make_circuit_breaker()
        backend, why = cli._make_agent_backend(breaker)
        assert backend is not None, f"升配后端没造出来：{why}"
        assert len(seen) == 2, f"客户端个数不对：{len(seen)}"
        assert seen[0] is breaker, "主后端没挂批次熔断器"
        assert seen[1] is None, "升配后端不该共享主后端的熔断器"

    def test_阈值从环境变量读(self, monkeypatch):
        _llm_env(monkeypatch, TRAJECTORY_LLM_CIRCUIT_THRESHOLD="5")
        assert cli._make_circuit_breaker().threshold == 5

    def test_阈值不合法时报错不猜(self, monkeypatch):
        """阈值配错 = 配置错 ⇒ **当场报错**（与「缺件即报错」同款）。"""
        _llm_env(monkeypatch, TRAJECTORY_LLM_CIRCUIT_THRESHOLD="abc")
        with pytest.raises(Exception):
            llm_client_mod.LLMConfig.from_env()

    def test_环境变量名对得上(self):
        assert ENV_CIRCUIT_THRESHOLD == "TRAJECTORY_LLM_CIRCUIT_THRESHOLD"


# ─────────────── 判分不可信 ⇒ 隔离 ───────────────


def _record(*, untrusted: bool, url: str = "https://v.youku.com/x.html",
            branch: str = "not_play_site") -> RunRecord:
    """造一条**带真负样本**的 RunRecord（分支走 ``not_play_site``）。"""
    rec = RunRecord(
        task_id="T001", title="功夫", query="功夫 在线观看",
        search_url="https://www.bing.com/search?q=功夫",
        driver="camofox",
    )
    ledger = RunLedger(task_id="T001")
    ledger.record(url, branch, "页面没有播放控件",
                  site_url=url, reached=False)
    rec.ledger = ledger
    rec.judgement_trustworthy = not untrusted
    return rec


class Test判分不可信不进池:
    def test_不可信时不并池(self, tmp_path):
        """**并池是写入时的动作** ⇒ 判据必须在写入这一刻读。

        产物照落（那是证据），但**不进负样本池**。
        """
        arch = P1Archive(tmp_path)
        arch.write(_record(untrusted=True), task_id="T001", run_id="r1")
        assert not (tmp_path / "negative.jsonl").exists(), (
            "判分不可信的存档进了负样本池——它会把「我们判不出来」"
            "教成「这些站看不了」"
        )
        assert list(tmp_path.glob("T001__*.json")), "P1 存档必须照落（那是证据）"

    def test_可信时照常并池(self, tmp_path):
        """对照组：不熔断的那条**照旧**并池——隔离不能连坐。"""
        arch = P1Archive(tmp_path)
        arch.write(_record(untrusted=False), task_id="T001", run_id="r1")
        pool = tmp_path / "negative.jsonl"
        assert pool.exists(), "可信的存档没并池——隔离把好的一起扣了"
        rows = [json.loads(x) for x in pool.read_text(encoding="utf-8").splitlines() if x.strip()]
        assert len(rows) == 1
        assert rows[0]["branch"] == "not_play_site"

    def test_存档里带着不可信标记(self, tmp_path):
        arch = P1Archive(tmp_path)
        path = arch.write(_record(untrusted=True), task_id="T001", run_id="r1")
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["judgement_trustworthy"] is False

    def test_可信的存档标记是true(self, tmp_path):
        arch = P1Archive(tmp_path)
        path = arch.write(_record(untrusted=False), task_id="T001", run_id="r1")
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["judgement_trustworthy"] is True


class Test判分不可信不出训练样本:
    def test_切不出样本且显式报出来(self, tmp_path):
        """⚠️ **必须显式报出来**：静默返回零条会与「这份存档切不出轨迹」
        同形——而这两件事的处置完全不同（一个是数据坏了，一个是判分
        没跑成）。"""
        p = tmp_path / "T001__r1.json"
        p.write_text(json.dumps({
            "task_id": "T001", "run_id": "r1", "driver": "camofox",
            "judgement_trustworthy": False,
            "search_observation": {"url": "https://x.test/", "body_text": "正文"},
            "steps": [],
        }, ensure_ascii=False), encoding="utf-8")
        res = split_archive_report(p)
        assert res.judgement_untrusted is True
        assert res.samples == (), "判分不可信还切出了样本"

    def test_历史存档缺键照常切(self, tmp_path):
        """⚠️ **判据用「显式 False」而不是「falsy」**。

        H-31 之前产的存档没有这个键 ⇒ 缺键 = 不知道 = 按「没证据说它
        不可信」放行——**不回填**（「并池是写入时的动作」）。
        """
        p = tmp_path / "T001__r1.json"
        p.write_text(json.dumps({
            "task_id": "T001", "run_id": "r1", "driver": "camofox",
            "search_observation": {"url": "https://x.test/", "body_text": "正文"},
            "steps": [],
        }, ensure_ascii=False), encoding="utf-8")
        res = split_archive_report(p)
        assert res.judgement_untrusted is False

    def test_任务视图跳过并说理由(self, tmp_path):
        res = build_task_view(
            {"task_id": "T001", "run_id": "r1", "driver": "camofox",
             "judgement_trustworthy": False},
            root=tmp_path,
        )
        assert res.written == 0
        assert res.skipped, "跳过了但没说理由——读的人不知道为什么少了一条"
        assert "判分不可信" in res.skipped[0]


# ─────────────── CLI 批次层 ───────────────


class _FakeSession:
    def __init__(self) -> None:
        self.closed = 0
        self.fingerprint = {"engine": "camoufox", "base_url": "http://localhost:9377"}

    async def close(self) -> None:
        self.closed += 1


def _plan_doc(task_ids: list[str]) -> dict:
    return {
        "report": {"seed": 7, "library_digest": "abc123",
                   "by_mode": {"single_title": len(task_ids)}},
        "instances": [
            {
                "task_id": tid,
                "persona_id": "parent-喜剧",
                "scenario_id": "media_lookup_standard",
                "prompt_text": "我想找到《功夫》电影在线观看的可播放网址",
                "search_query": "功夫 在线观看",
                "has_standard": True,
                "retrieval_mode": "single_title",
                "title": "功夫",
                "rewritten": True,
                "provenance": {"persona_id": "parent-喜剧"},
            }
            for tid in task_ids
        ],
    }


def _args(plan: Path, out: Path) -> argparse.Namespace:
    return argparse.Namespace(
        plan=str(plan), title=None, include_unrewritten=False,
        limit=None, dry_run=False, driver="camofox", executor_mode=None,
        engine="bing", max_candidates=1, max_chars=None,
        stop_after_success=0, discovery_path="direct",
        max_per_host=0,
        max_intermediates=3, max_referral_links=5,
        per_site_timeout=None, out=str(out), task_id="T001",
        element_basis=False, headful=False, perceptor="llm",
    )


class _CountingHTTP:
    """假 HTTP 层：**数真发出去的请求**。

    ⚠️ **计数在 ``post`` 里**——客户端在熔断打开时压根不调 ``post``，
    所以这个数就是「**真打了几次后端**」，而不是「被调用了几次」。
    差一个数量级的两种读数混在一起，会把「熔断封住了」读成
    「熔断没生效」。
    """

    #: **类级**计数：客户端是每 task 一个，批次级的读数只能靠类属性。
    requests = 0
    status = 401

    def post(self, url, json=None, headers=None):        # noqa: A002
        type(self).requests += 1
        return _FakeResp(type(self).status, _FakeHTTP.OK_BODY)

    def close(self) -> None:
        pass


class _OkHTTP(_CountingHTTP):
    """后端正常的假 HTTP 层（对照组）。"""

    requests = 0
    status = 200


class _StubbedLLMClient(llm_client_mod.LLMClient):
    """**真客户端 + 假 HTTP 层**。

    ⚠️ **不能整个换成一个假客户端类**——那样测的就不再是
    「熔断有没有生效」，而是「假客户端有没有照抄熔断契约」
    （假客户端自己会检查熔断 ⇒ 真代码坏了它照样绿）。
    这里**只把网络换掉**：熔断、失败分类、重试、错误文本全是
    **真代码**。
    """

    http_cls = _CountingHTTP

    def _ensure_client(self):
        if self._client is None:
            self._client = type(self).http_cls()
        return self._client


class TestCLI批次层:
    @pytest.fixture(autouse=True)
    def _reset_counter(self):
        _CountingHTTP.requests = 0
        _OkHTTP.requests = 0
        yield
        _CountingHTTP.requests = 0
        _OkHTTP.requests = 0

    async def test_连返401时请求数封顶且批次被标不可用(
            self, tmp_path, monkeypatch, capsys):
        """**台账 H-31 的验收条件**：用假客户端连返 401 跑 2 条计划 ⇒
        4xx 调用数 ≤ N+1、批次被标「不可用于判分」、产物不并池。"""
        _llm_env(monkeypatch, TRAJECTORY_LLM_CIRCUIT_THRESHOLD="3")
        monkeypatch.setattr(llm_client_mod, "LLMClient", _StubbedLLMClient)

        async def _launch():
            return _FakeSession()

        monkeypatch.setattr(cli, "_launch_camofox", _launch)
        monkeypatch.setattr(cli, "_execution_layer_missing_reason",
                            lambda kind: "")
        monkeypatch.setattr(cli, "make_driver", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "_print_summary", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "_dispatch_l6_summary", lambda *a, **kw: None)

        from trajectory_pipeline.perception.base import (
            InteractiveElement,
            Observation,
            Q,
        )

        class _Orch:
            """假 Orchestrator：**真的去问感知层**——否则熔断根本不会触发。"""

            def __init__(self, driver, perceptor, cfg, **kw):
                self.perceptor = perceptor

            async def run(self, task_id, title, **kw):
                obs = Observation(
                    url="https://v.youku.com/x.html",
                    page_title="优酷",
                    body_text="页面正文",
                    interactive_elements=(
                        InteractiveElement(ref="e1", role="link",
                                           name="立即播放"),
                    ),
                )
                for _ in range(5):
                    self.perceptor.decide(Q.IS_REACHABLE, obs)
                return _record(untrusted=False)

        monkeypatch.setattr(cli, "Orchestrator", _Orch)

        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(_plan_doc(["T001", "T002"]),
                                    ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "out"

        rc = await cli._run_plan(_args(plan, out))
        text = capsys.readouterr().out

        # ① 4xx 调用数封顶：每 task 5 次调用 × 2 条 task = 10 次需求，
        #    阈值 3 ⇒ 实际只该发 3 次。
        assert _CountingHTTP.requests == 3, (
            f"发了 {_CountingHTTP.requests} 次请求——熔断没封住"
            "（all80 就是这样打空 170 次的）"
        )

        # ② 批次被标「判分不可信」。
        assert "判分不可信" in text, f"批次小结没点出判分不可信：\n{text}"

        # ③ 存档里带着不可信标记，且**没进负样本池**。
        archives = sorted(out.glob("T*__*.json"))
        assert len(archives) == 2, f"存档只落了 {[p.name for p in archives]}"
        for p in archives:
            doc = json.loads(p.read_text(encoding="utf-8"))
            assert doc["judgement_trustworthy"] is False, (
                f"{p.name} 没标判分不可信"
            )
        assert not (out / "negative.jsonl").exists(), (
            "判分不可信的产物进了负样本池"
        )

        # ④ 退出码非 0：退出码 0 会让「这批判分可用」与「这批判分被污染」
        #    长得一样。
        assert rc == 1, f"退出码是 {rc}——判分不可信必须非 0"

    async def test_没熔断时退出码是0(self, tmp_path, monkeypatch):
        """对照组：后端正常 ⇒ 退出码 0（隔离不能连坐好批次）。"""
        _llm_env(monkeypatch)

        class _OkClient(_StubbedLLMClient):
            http_cls = _OkHTTP

        monkeypatch.setattr(llm_client_mod, "LLMClient", _OkClient)

        async def _launch():
            return _FakeSession()

        monkeypatch.setattr(cli, "_launch_camofox", _launch)
        monkeypatch.setattr(cli, "_execution_layer_missing_reason",
                            lambda kind: "")
        monkeypatch.setattr(cli, "make_driver", lambda *a, **kw: object())
        monkeypatch.setattr(cli, "_print_summary", lambda *a, **kw: None)
        monkeypatch.setattr(cli, "_dispatch_l6_summary", lambda *a, **kw: None)

        class _Orch:
            def __init__(self, driver, perceptor, cfg, **kw):
                pass

            async def run(self, task_id, title, **kw):
                return _record(untrusted=False)

        monkeypatch.setattr(cli, "Orchestrator", _Orch)

        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(_plan_doc(["T001", "T002"]),
                                    ensure_ascii=False), encoding="utf-8")
        out = tmp_path / "out"

        rc = await cli._run_plan(_args(plan, out))
        assert rc == 0
        for p in sorted(out.glob("T*__*.json")):
            doc = json.loads(p.read_text(encoding="utf-8"))
            assert doc["judgement_trustworthy"] is True
