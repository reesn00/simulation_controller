"""正文精炼（方案 25）——**只可能漏，不可能造**。

这一组盯的是精炼层的四条硬保证：

1. **解析**：模型只报行号；输出里混进任何非数字 ⇒ 整条作废
   （「15 是行号还是行数」分不清时，宁可不精炼）；
2. **保真**：下一步动作的目标必须保住——丢了就是「教模型编目标」；
3. **回退**：任何一步不对 ⇒ 整条作废，渲染层走规则裁剪；
4. **旁挂**：写得进、读得出、坏文件当没有（旁挂是派生物，不是证据）。
"""

from __future__ import annotations

import ast
from pathlib import Path

from trajectory_pipeline.common import body_lines, refine_sidecar
from trajectory_pipeline.refine import prompt as refine_prompt
from trajectory_pipeline.refine import reader as refine_reader
from trajectory_pipeline.refine import refiner as refine_refiner


class _FakeClient:
    """假 LLM 客户端——只记「被叫了几次、回的什么」。"""

    def __init__(self, reply: str | None = "1,2"):
        self.reply = reply
        self.calls: list[tuple[str, str]] = []
        self.cfg = type("Cfg", (), {"model": "fake"})()
        self.last_usage = {}
        self.last_latency_ms = 1
        self.last_call_error = ""

    def chat(self, system: str, user: str, **kw) -> str | None:
        self.calls.append((system, user))
        return self.reply


class Test提示词口径:
    def test_提示词不许出现摘要类的词(self):
        """「总结 / 概括 / 提炼 / 摘要」会把模型从「选行」推向「写摘要」——
        摘要的失效模式是不可验证的（改写 ⇒ 幻觉无法逐字核对）。"""
        for word in ("总结", "概括", "提炼", "摘要"):
            assert word not in refine_prompt.REFINE_SYSTEM, word

    def test_提示词只输出编号(self):
        assert "只输出编号" in refine_prompt.REFINE_SYSTEM

    def test_候选行编号从1开始(self):
        payload = refine_prompt.build_user_payload(["甲", "乙"], max_lines=5)
        assert "1. 甲" in payload
        assert "2. 乙" in payload
        assert "最多保留 5 行" in payload


class Test解析:
    def test_纯编号能解析(self):
        picked, err = refine_refiner.parse_selection("3,7,12", 20)
        assert err == ""
        assert picked == [3, 7, 12]

    def test_中文逗号与分号也能解析(self):
        picked, err = refine_refiner.parse_selection("3、7；12", 20)
        assert err == ""
        assert picked == [3, 7, 12]

    def test_代码块围栏剥掉再解析(self):
        picked, err = refine_refiner.parse_selection("```\n3,7\n```", 20)
        assert err == ""
        assert picked == [3, 7]

    def test_混进文字整条作废(self):
        """「共 15 行」——15 是行号还是行数？分不清就整条作废。"""
        picked, err = refine_refiner.parse_selection("共 15 行", 20)
        assert picked == []
        assert err

    def test_行号越界整条作废(self):
        """越界 = 模型看错了行数表。把「第 37 行」当成「第 7 行」会选出
        一份**看起来合法**、实际与任务无关的正文。"""
        picked, err = refine_refiner.parse_selection("99", 20)
        assert picked == []
        assert "越界" in err

    def test_零与负号也是越界(self):
        picked, err = refine_refiner.parse_selection("0", 20)
        assert picked == []
        assert err

    def test_没回话是失败不是空结果(self):
        picked, err = refine_refiner.parse_selection("", 20)
        assert picked == []
        assert err
        picked, err = refine_refiner.parse_selection(None, 20)
        assert picked == []
        assert err

    def test_重复行号去重(self):
        picked, err = refine_refiner.parse_selection("3,3,7", 20)
        assert err == ""
        assert picked == [3, 7]


class Test保真判据:
    def test_动作目标没保住要报错(self):
        cands = ["无关内容", "点击这里播放正片"]
        err = refine_refiner.validate_selection([1], cands, keep=("播放正片",))
        assert "操作目标" in err

    def test_候选里本来就没有目标不算丢(self):
        """页面上根本没有目标文本，不是「被模型丢了」。"""
        cands = ["无关内容", "别的行"]
        assert refine_refiner.validate_selection([1], cands, keep=("不存在",)) == ""

    def test_任务词命中的行全丢要报错(self):
        cands = ["《让子弹飞》在线观看", "无关内容"]
        err = refine_refiner.validate_selection([2], cands, focus=("让子弹飞",))
        assert err

    def test_全选则无错(self):
        cands = ["《让子弹飞》在线观看", "点击播放"]
        assert refine_refiner.validate_selection(
            [1, 2], cands, focus=("让子弹飞",), keep=("点击播放",),
        ) == ""


class Test精炼入口:
    def test_正文在预算内不发请求(self):
        """**不选就没有**——候选行本来就在预算内 ⇒ 一次请求都不发。"""
        client = _FakeClient()
        out = refine_refiner.refine_body(client, "短正文", limit=320)
        assert out.source == "not_needed"
        assert client.calls == []

    def test_模型没回话整条作废(self):
        client = _FakeClient(reply=None)
        out = refine_refiner.refine_body(client, "字" * 5000, limit=320)
        assert out.source == "failed"
        assert out.lines == ()
        assert out.error

    def test_输出不纯整条作废(self):
        client = _FakeClient(reply="我选了第 3 行")
        out = refine_refiner.refine_body(client, "字" * 5000, limit=320)
        assert out.source == "failed"

    def test_成功时只保留选中的行(self):
        body = "\n".join(["《让子弹飞》在线观看", "无关第一行", "无关第二行"])
        client = _FakeClient(reply="1")
        out = refine_refiner.refine_body(client, body, focus=("让子弹飞",),
                                         limit=20)
        assert out.source == "llm"
        assert out.lines == ("《让子弹飞》在线观看",)
        assert len(client.calls) == 1

    def test_保留的行逐字来自原文(self):
        """**只可能漏，不可能造**——模型加不了一个原文没有的词。"""
        body = "\n".join([f"第{i}行内容" for i in range(1, 40)])
        client = _FakeClient(reply="1,2,3")
        out = refine_refiner.refine_body(client, body, limit=20)
        assert out.source == "llm"
        for line in out.lines:
            assert line in body.splitlines()

    def test_动作目标被丢掉就整条作废(self):
        body = "\n".join([f"第{i}行内容" for i in range(1, 40)])
        client = _FakeClient(reply="2")
        out = refine_refiner.refine_body(client, body, keep=("第1行内容",),
                                         limit=20)
        assert out.source == "failed"

    def test_候选行超上限时只给前N行(self):
        body = "\n".join([f"第{i}行内容" for i in range(1, 400)])
        client = _FakeClient(reply="1")
        out = refine_refiner.refine_body(client, body, limit=20)
        assert out.source == "llm"
        payload = client.calls[0][1]
        assert payload.count("\n") <= refine_refiner.MAX_CANDIDATES + 10

    def test_需不需要精炼的判据与真调用一致(self):
        short = "短正文"
        assert refine_refiner.needs_refine(short, 320) is False
        long_body = "\n".join([f"第{i}行内容" for i in range(1, 200)])
        assert refine_refiner.needs_refine(long_body, 320) is True


class Test旁挂:
    def test_读写往返(self, tmp_path):
        key = refine_sidecar.body_key("正文")
        payload = refine_sidecar.make_payload(
            "T001__abc.json",
            {key: {"body_key": key, "source": "llm", "kept": ["正文"],
                   "dropped": 3, "candidates": 4, "error": "", "call": {}}},
            prompt_version=refine_prompt.REFINE_PROMPT_VERSION,
        )
        path = refine_sidecar.sidecar_path(tmp_path, "T001__abc.json")
        refine_sidecar.write_sidecar(path, payload)
        blob = refine_sidecar.read_sidecar(path)
        assert blob is not None
        assert blob["archive"] == "T001__abc.json"
        assert blob["prompt_version"] == refine_prompt.REFINE_PROMPT_VERSION
        assert refine_sidecar.index_by_body(blob) == {key: ["正文"]}

    def test_版本对不上当没有(self, tmp_path):
        path = tmp_path / "x.json"
        path.write_text(
            '{"schema_version": "refine.v0", "entries": {}}', encoding="utf-8",
        )
        assert refine_sidecar.read_sidecar(path) is None

    def test_坏JSON当没有(self, tmp_path):
        path = tmp_path / "x.json"
        path.write_text("{不是 JSON", encoding="utf-8")
        assert refine_sidecar.read_sidecar(path) is None

    def test_文件不存在当没有(self, tmp_path):
        assert refine_sidecar.read_sidecar(tmp_path / "nope.json") is None

    def test_空保留行不进索引(self):
        """失败的条目（kept 为空）不进索引——渲染层于是走规则裁剪。"""
        key = refine_sidecar.body_key("正文")
        blob = {"schema_version": refine_sidecar.SIDECAR_SCHEMA_VERSION,
                "entries": {key: {"body_key": key, "kept": []}}}
        assert refine_sidecar.index_by_body(blob) == {}

    def test_索引按正文键定位而不是步号(self):
        a = refine_sidecar.body_key("甲")
        b = refine_sidecar.body_key("乙")
        assert a != b
        blob = {"schema_version": refine_sidecar.SIDECAR_SCHEMA_VERSION,
                "entries": {a: {"body_key": a, "kept": ["甲"]},
                            b: {"body_key": b, "kept": ["乙"]}}}
        index = refine_sidecar.index_by_body(blob)
        assert index[a] == ["甲"] and index[b] == ["乙"]

    def test_旁挂不长得像P1存档(self):
        """**实测踩过的坑**：旁挂与 P1 存档同在产物根下。

        - **名字**：不能顶着 ``<task>__<run>.json``（P1 的形状约定）——
          否则按名扫存档的消费者会把它当存档收走。
        - **形状**：顶层不能有 ``steps``/``visits``/``run_config``——
          P1 的 ``steps`` 是**列表**，旁挂的顶层键叫 ``entries``（**映射**）。
          不卡住的话，按形状筛存档的扫描器会把它收进来，
          然后在 ``st.get("action")`` 上炸掉。
        """
        path = refine_sidecar.sidecar_path("X", "T001__abc12345.json")
        assert path.name == "T001__abc12345.refined.json"
        assert not path.name.endswith(".json") or path.name.endswith(
            refine_sidecar.SIDECAR_SUFFIX)

        payload = refine_sidecar.make_payload("T001__abc12345.json", {})
        for key in ("steps", "visits", "run_config"):
            assert key not in payload, f"旁挂顶层有 {key}：会被当 P1 存档收走"
        assert isinstance(payload.get("entries"), dict)

    def test_load_index目录没给就是空(self):
        assert refine_sidecar.load_index(None, "T001__abc.json") == {}


class Test读P1:
    def test_正文为空的观察不进清单(self):
        archive = {"steps": [{"action": {}, "observation": {"body_text": "  "}}]}
        assert refine_reader.iter_observations(archive) == []

    def test_正文按与渲染层同一份挑法取(self):
        """挑错了就是「按 A 的哈希去查 B 的精炼」——查不到，白跑一趟。"""
        archive = {"steps": [{"action": {},
                              "observation": {"body_preview": "老存档正文"}}]}
        specs = refine_reader.iter_observations(archive)
        assert specs and specs[0].body == "老存档正文"

    def test_保真口径取并集(self):
        """本单元内下一个可见动作 + 下一单元第一个可见动作——两边都要保住。"""
        archive = {
            "steps": [
                {"action": {"tool": "click", "target": {"name": "第一步"}},
                 "observation": {"body_text": "正文一"}},
                {"action": {"tool": "click", "target": {"name": "本单元下一步"}},
                 "observation": {"body_text": "正文二"}},
            ],
            "visits": [{
                "steps": [{
                    "action": {"tool": "click", "target": {"name": "下一单元"}},
                    "observation": {"body_text": "正文三"},
                }],
            }],
        }
        specs = refine_reader.iter_observations(archive)
        assert specs[0].keep == ("本单元下一步", "下一单元")
        assert specs[1].keep == ("下一单元",)

    def test_infrastructure步不占保真名额(self):
        """开合标签页是执行器自己的动作，模型永远不该输出它——
        它也不该占「下一步动作目标」的名额（同 P2 的 ``_observation_keeps``）。"""
        archive = {
            "steps": [
                {"action": {"tool": "click", "target": {"name": "第一步"}},
                 "observation": {"body_text": "正文一"}},
                {"action": {"tool": "new_tab", "origin": "infrastructure",
                            "target": {"name": "会话标签"}},
                 "observation": {"body_text": "正文二"}},
                {"action": {"tool": "click", "target": {"name": "真下一步"}},
                 "observation": {"body_text": "正文三"}},
            ],
        }
        specs = refine_reader.iter_observations(archive)
        # infrastructure 被跳过 ⇒ 目标是它**后面**那个可见动作
        assert specs[0].keep == ("真下一步",)
        assert specs[1].keep == ("真下一步",)
        assert all("会话标签" not in s.keep for s in specs)


class Test规则与地基同源:
    def test_候选行与渲染层用的是同一份规则(self):
        """两份实现漂了不报错——只是「给模型看的候选集」与「渲染时剔掉的集」
        对不上，精炼结果里会多出本该剔掉的行。"""
        body = "首页\n京ICP备12345号\n《让子弹飞》在线观看"
        assert body_lines.candidate_lines(body) == ["《让子弹飞》在线观看"]
        assert body_lines.noise_dropped(body) == 2


class Test预算用尽:
    """``budget:`` 开头的那一类失败——**换预算能好**。

    真实批次踩到过（2026-11-13，62 条里 1 条）：推理模型的**思考与正文
    共用** ``max_tokens``，思考吃光预算 ⇒ ``finish_reason=length`` +
    ``content=null``——**请求成功、正文为空**，看着像「后端坏了」。
    """

    BUDGET_ERR = ("budget:max_tokens 用尽（finish_reason=length，"
                  "content 为空）；调大 TRAJECTORY_LLM_MAX_TOKENS")

    #: 39 行正文（远超 20 字预算 ⇒ 值得发请求），且「1,2」在行号范围内
    #:（**一行**正文会让「1,2」越界——那测的是另一件事）。
    BODY = "\n".join(f"第{i}行内容" for i in range(1, 40))

    def _client(self, *, first_error: str, retry_reply: str | None = "1,2",
                max_tokens: int = 8192):
        class _C:
            def __init__(self) -> None:
                self.calls: list[int | None] = []
                self.cfg = type("Cfg", (), {"model": "fake",
                                            "max_tokens": max_tokens})()
                self.last_usage: dict = {}
                self.last_latency_ms = 1
                self.last_call_error = ""

            def chat(self, system, user, *, max_tokens=None):
                self.calls.append(max_tokens)
                if len(self.calls) == 1:
                    self.last_call_error = first_error
                    return None
                if retry_reply is None:
                    self.last_call_error = first_error
                    return None
                self.last_call_error = ""
                return retry_reply

        return _C()

    def test_预算用尽会换大预算重试一次(self):
        client = self._client(first_error=self.BUDGET_ERR)
        out = refine_refiner.refine_body(client, self.BODY, limit=20)
        assert out.source == "llm"
        assert len(client.calls) == 2
        assert client.calls[0] is None, "第一次走客户端自己的配置预算"
        assert client.calls[1] == refine_refiner.RECOMMENDED_MAX_TOKENS, (
            "重试必须换大预算——重发同一个预算结果一样"
        )

    def test_重试预算从配置推而不是写死(self):
        """配置 16384 ⇒ 重试 32768。写死一个数必然在某个后端上不够。"""
        client = self._client(first_error=self.BUDGET_ERR, max_tokens=16384)
        refine_refiner.refine_body(client, self.BODY, limit=20)
        assert client.calls[1] == 32768

    def test_两次调用的账要合并(self):
        """重试是**两次真实调用**——只记第二次就把第一次藏起来了。"""
        client = self._client(first_error=self.BUDGET_ERR)
        out = refine_refiner.refine_body(client, self.BODY, limit=20)
        assert out.call["retried"] is True
        assert out.call["retry_max_tokens"] == (
            refine_refiner.RECOMMENDED_MAX_TOKENS)
        assert out.call["error"] == ""
        assert out.call["latency_ms"] == 2, "两次各 1ms ⇒ 2ms"

    def test_重试也失败就整条作废(self):
        """只重试**一次**：预算一直不够说明这模型话多，再试就是烧钱。"""
        client = self._client(first_error=self.BUDGET_ERR, retry_reply=None)
        out = refine_refiner.refine_body(client, self.BODY, limit=20)
        assert out.source == "failed"
        assert out.lines == ()
        assert "budget:" in out.error
        assert len(client.calls) == 2

    def test_别的失败不重试(self):
        """``http:500`` 是后端过载（客户端内部已经重试过）——换预算没用，
        再发一次只是烧钱。"""
        client = self._client(first_error="http:500")
        out = refine_refiner.refine_body(client, self.BODY, limit=20)
        assert out.source == "failed"
        assert out.error == "http:500"
        assert len(client.calls) == 1, "只有预算用尽才重试"

    def test_客户端不认max_tokens也不崩(self):
        """第三方/假客户端可能不收 ``max_tokens`` ⇒ 这次重试**没发生**，
        账本也**不能**标成「重试过」。"""

        class _NoKw:
            def __init__(self) -> None:
                self.cfg = type("Cfg", (), {"model": "fake",
                                            "max_tokens": 8192})()
                self.last_usage: dict = {}
                self.last_latency_ms = 1
                self.last_call_error = ""

            def chat(self, system, user):        # 不收 max_tokens
                self.last_call_error = Test预算用尽.BUDGET_ERR
                return None

        out = refine_refiner.refine_body(_NoKw(), self.BODY, limit=20)
        assert out.source == "failed"
        assert "budget:" in out.error
        assert not out.call.get("retried"), "没发生的重试不许记账"


class Test模块边界:
    def test_精炼层不import装配层(self):
        """**方向不能反**：精炼是**上游**的一步（它产出训练语料要用的东西），
        装配层是下游。精炼 import 装配层就把「320 这个数」的权威拖进两层
        ——而那个数在渲染层裁（`observation_view`），精炼层只能**转发**。"""
        pkg = Path(refine_refiner.__file__).parent
        for path in sorted(pkg.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""]
                elif isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                else:
                    continue
                for mod in mods:
                    assert "assembler" not in mod, (
                        f"{path.name} import 了 {mod}：方向反了"
                    )

    def test_精炼层不import执行端(self):
        """精炼读 P1 按**文件格式**读，不 import executor——
        执行端改 dataclass 不该让「重建一批精炼」失败。"""
        pkg = Path(refine_refiner.__file__).parent
        for path in sorted(pkg.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    mods = [node.module or ""]
                elif isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                else:
                    continue
                for mod in mods:
                    assert "executor" not in mod, (
                        f"{path.name} import 了 {mod}：精炼层绑死在执行端版本上"
                    )
