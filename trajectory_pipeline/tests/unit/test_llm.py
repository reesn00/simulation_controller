"""``llm/`` 底座的单元测试——共享容错层 + 客户端。

这些函数的价值全在**容错**，而容错的价值全在**真实形态**。
每个容错分支都对着一条实测形态或一个已修 bug 写，
而不是「理论上模型可能这么干」。
"""

from __future__ import annotations

import json

import pytest

from trajectory_pipeline.llm import schema_parse as sp
from trajectory_pipeline.llm.client import LLMConfig, LLMUnavailable


class TestStripThink:
    def test_闭合的think被剥(self):
        assert sp.strip_think("<think>abc</think>\n{}") == "{}"

    def test_未闭合的think也要剥(self):
        """模型被 max_tokens 截断时只留开标签。
        不剥的话后面全是思维链正文，JSON 永远找不到 → 整批落 None。"""
        assert sp.strip_think("<think>我先看看页面上有什么") == ""

    def test_alternative标签(self):
        """OpenAI 系用 <reasoning>。"""
        assert sp.strip_think("<reasoning>x</reasoning>{}") == "{}"

    def test_无think时原样返回(self):
        assert sp.strip_think('{"a":1}') == '{"a":1}'


class TestExtractJson:
    def test_裸JSON(self):
        assert sp.extract_json('{"a":1}') == {"a": 1}

    def test_markdown_fence(self):
        assert sp.extract_json('```json\n{"a":1}\n```') == {"a": 1}

    def test_散文包裹(self):
        assert sp.extract_json('好的，结果如下：\n{"a":1}\n希望有帮助') == {"a": 1}

    def test_think包裹(self):
        assert sp.extract_json('<think>想一下</think>{"a":1}') == {"a": 1}

    def test_嵌套取外层不取内层(self):
        """**已修 bug 的回归测试。**

        早期实现从后往前找 ``{``，于是 ``{"selected":[{"url":...}]}``
        被解析成内层的 ``{"url":...}``——外层的 selected 键全丢，
        下游当成「模型没选站点」，判定从 True 静默掉成 None。

        这条测试存在是因为它**当时跑得绿**：只有判断点 ① 的 payload
        是嵌套的，其余三题扁平单层，从后往前照样能取对。
        """
        raw = '{"selected":[{"url":"https://a.test","why":"w"}],"rejected":[]}'
        got = sp.extract_json(raw)
        assert got["selected"][0]["url"] == "https://a.test"
        assert "rejected" in got

    def test_字符串里的花括号不算(self):
        """``"why":"见{a}"`` 里的 ``{`` 若被当成对象起点会截断。"""
        got = sp.extract_json('{"why":"见{a}处","n":1}')
        assert got == {"why": "见{a}处", "n": 1}

    def test_转义引号(self):
        got = sp.extract_json(r'{"label":"他说\"播放\"","n":1}')
        assert got["label"] == '他说"播放"'

    def test_不是JSON返回None(self):
        """返回 None 而不是 {}——``{}`` 与「模型判了但字段为空」同形，
        而 I7 下两者走完全不同的路。"""
        assert sp.extract_json("完全不是 JSON 的一段话") is None

    def test_顶层是数组时返回None(self):
        """契约要的是对象。返回数组会让下游 ``data["answer"]`` 炸。"""
        assert sp.extract_json("[1,2,3]") is None


class TestAsBool:
    @pytest.mark.parametrize("raw,want", [
        (True, True), (False, False),
        ("true", True), ("True", True), ("是", True), ("1", True),
        ("false", False), ("否", False), ("0", False),
        (1, True), (0, False), (1.0, True),
    ])
    def test_认得的写法(self, raw, want):
        assert sp.as_bool(raw) is want

    @pytest.mark.parametrize("raw", ["可能", "不确定", None, [], {}, 2.0, -1, 3])
    def test_认不出返回None(self, raw):
        """「不确定」语义上是 fail-closed 信号。猜成 False 等于把
        「模型不确定」写成「这里没有播放控件」。

        数字**只认 0 与 1**：模型回 ``score: 2`` 那是个评分不是布尔，
        而 ``value != 0`` 会把它读成 True——错的方向还是错的那一侧。"""
        assert sp.as_bool(raw) is None


class TestAsStr:
    def test_非字符串不转(self):
        """``str({'a':1})`` 在 ref 字段上会被当成合法 ref 传给 click。"""
        assert sp.as_str({"a": 1}) == ""
        assert sp.as_str(None) == ""
        assert sp.as_str(["a"]) == ""

    def test_去空白(self):
        assert sp.as_str("  e1  ") == "e1"


class TestConfidence:
    @pytest.mark.parametrize("raw,want", [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0), ("0.7", 0.7)])
    def test_合法值(self, raw, want):
        assert sp.as_confidence(raw) == want

    @pytest.mark.parametrize("raw", [1.7, -0.1, "很高", None, True])
    def test_越界或认不出返回None(self, raw):
        """**不夹逼。** 夹逼会让明显失真的模型输出看起来像合法置信度，
        而阈值判断正建立在这个数上。"""
        assert sp.as_confidence(raw) is None


class TestFieldAlias:
    def test_认别名(self):
        assert sp.field({"element_ref": "e1"}, "ref") == "e1"

    def test_优先取首个命中(self):
        assert sp.field({"ref": "e1", "element_ref": "e9"}, "ref") == "e1"

    def test_全None时跳到下一个别名(self):
        """模型偶尔给 ``"ref": null`` 同时给 ``"element_ref": "e1"``。"""
        assert sp.field({"ref": None, "element_ref": "e1"}, "ref") == "e1"

    def test_未登记的键不认(self):
        """刻意不做模糊匹配：``ref_lookup`` 之类的无关字段会被误当 ref，
        而误认出来的 ref 代码会拿去点击。"""
        assert sp.field({"ref_lookup": "e9"}, "ref", "默认") == "默认"


class TestConfig:
    def test_缺端点报错不猜(self):
        """猜默认端点的后果：判定全落 None，报表上分不清「没连上」
        与「判不了」。"""
        with pytest.raises(LLMUnavailable, match="TRAJECTORY_LLM_MODEL"):
            LLMConfig.from_env(env={"TRAJECTORY_LLM_BASE_URL": "http://x/v1"})

    def test_缺端点也报错(self):
        with pytest.raises(LLMUnavailable, match="TRAJECTORY_LLM_BASE_URL"):
            LLMConfig.from_env(env={"TRAJECTORY_LLM_MODEL": "m"})

    def test_补全尾斜杠与缺v1(self):
        assert LLMConfig(base_url="http://x/v1", model="m").endpoint \
            == "http://x/v1/chat/completions"
        assert LLMConfig(base_url="http://x", model="m").endpoint \
            == "http://x/v1/chat/completions"
        assert LLMConfig(base_url="http://x/", model="m").endpoint \
            == "http://x/v1/chat/completions"

    def test_repr不含key(self):
        """凭据红线：config 一旦被 log 或带进 traceback 就等于泄漏。"""
        c = LLMConfig(base_url="http://x", model="m", api_key="secret-value")
        assert "secret-value" not in repr(c)
        assert "redacted" in repr(c)

    def test_无key时用占位(self):
        """本机 vLLM 不校验 key。"""
        assert LLMConfig.from_env(env={
            "TRAJECTORY_LLM_BASE_URL": "http://x", "TRAJECTORY_LLM_MODEL": "m",
        }).api_key == "not-needed"

    def test_超时非法报错(self):
        with pytest.raises(LLMUnavailable, match="不是数字"):
            LLMConfig.from_env(env={
                "TRAJECTORY_LLM_BASE_URL": "http://x", "TRAJECTORY_LLM_MODEL": "m",
                "TRAJECTORY_LLM_TIMEOUT_S": "abc",
            })

    def test_超时应为正(self):
        with pytest.raises(LLMUnavailable, match="必须为正"):
            LLMConfig.from_env(env={
                "TRAJECTORY_LLM_BASE_URL": "http://x", "TRAJECTORY_LLM_MODEL": "m",
                "TRAJECTORY_LLM_TIMEOUT_S": "0",
            })

    def test_可序列化不含key(self):
        """``dump_for_debug`` 进日志，故也不能含 key。"""
        from trajectory_pipeline.llm.client import dump_for_debug

        c = LLMConfig(base_url="http://x", model="m", api_key="secret-value")
        assert "secret-value" not in dump_for_debug(c)


class _Resp:
    """极简响应替身——只需 status_code 与 json()。"""

    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Transport:
    """替掉 httpx 客户端，记账 ``max_tokens`` 并回放固定响应。"""

    def __init__(self, payload, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.budgets: list = []
        self.closed = False

    def post(self, url, *, json, headers):
        self.budgets.append(json["max_tokens"])
        return _Resp(self.payload, self.status_code)

    def close(self):
        self.closed = True


def _client(payload, **kw):
    """造一个真 :class:`LLMClient`，只把传输层换成替身。"""
    from trajectory_pipeline.llm.client import LLMClient

    c = LLMClient(LLMConfig(base_url="http://x", model="m", **kw))
    c._client = _Transport(payload)
    return c


def _ok(content, finish: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content},
                         "finish_reason": finish}]}


class TestMaxTokens:
    """输出预算。**每个断言都对着一条实测形态**，不是「理论上」。

    实测（本机推理模型，同一个判断点 ① 的请求，只改 ``max_tokens``）::

        600  -> finish_reason=length  content=null   （599 tok 全在思考上）
        2048 -> finish_reason=length  content 截断
        8192 -> finish_reason=stop    content 干净（2096 tok）

    写死 600 时的症状是**正文为空**，而客户端按红线不读
    ``reasoning_content``，于是整条链每道题都只表现为一句
    ``shape:ValueError``——看上去像「LLM 判不了」，实则预算不够。
    """

    def test_默认按推理模型定(self):
        from trajectory_pipeline.llm.client import DEFAULT_MAX_TOKENS

        assert DEFAULT_MAX_TOKENS >= 8192
        assert LLMConfig(base_url="http://x", model="m").max_tokens \
            == DEFAULT_MAX_TOKENS

    def test_环境变量可调(self):
        c = LLMConfig.from_env(env={
            "TRAJECTORY_LLM_BASE_URL": "http://x", "TRAJECTORY_LLM_MODEL": "m",
            "TRAJECTORY_LLM_MAX_TOKENS": "16384",
        })
        assert c.max_tokens == 16384

    def test_预算非法报错(self):
        with pytest.raises(LLMUnavailable, match="不是整数"):
            LLMConfig.from_env(env={
                "TRAJECTORY_LLM_BASE_URL": "http://x",
                "TRAJECTORY_LLM_MODEL": "m",
                "TRAJECTORY_LLM_MAX_TOKENS": "很多",
            })

    def test_预算应为正(self):
        with pytest.raises(LLMUnavailable, match="必须为正"):
            LLMConfig.from_env(env={
                "TRAJECTORY_LLM_BASE_URL": "http://x",
                "TRAJECTORY_LLM_MODEL": "m",
                "TRAJECTORY_LLM_MAX_TOKENS": "0",
            })

    def test_不带参数时走config(self):
        """签名默认值是 ``None`` 而不是数字——给了数字调用点就会各写各的。"""
        from trajectory_pipeline.llm.client import DEFAULT_MAX_TOKENS

        c = _client(_ok("hi"))
        assert c.chat("s", "u") == "hi"
        assert c._client.budgets == [DEFAULT_MAX_TOKENS]

    def test_显式参数可覆盖(self):
        """排查用量边界时才用；生产调用点不该带它（见 _ask 的注释）。"""
        c = _client(_ok("hi"))
        c.chat("s", "u", max_tokens=256)
        assert c._client.budgets == [256]

    def test_预算用尽报可行动的原因(self):
        """**关键断言。** 这一条把「预算不够」与「后端结构不对」分开——
        两者在原始响应上都是「200 且拿不到正文」，混成一句话时
        谁也不知道该改环境变量还是该查后端。"""
        c = _client(_ok(None, finish="length"))
        assert c.chat("s", "u") is None
        assert c.last_error.startswith("budget:")
        assert "TRAJECTORY_LLM_MAX_TOKENS" in c.last_error
        # 不重试：重发一次还是同一个预算，还是空。
        assert c._client.budgets == [c.cfg.max_tokens]

    def test_预算用尽但有正文时照常返回(self):
        """截断但拿到了正文，仍要交给解析层判断——它可能已经完整了。"""
        c = _client(_ok('{"answer": tr', finish="length"))
        assert c.chat("s", "u") == '{"answer": tr'

    def test_非预算原因的空正文仍算shape(self):
        """``finish_reason=stop`` 却没正文，那是后端的事，不是预算的事。"""
        c = _client(_ok(None, finish="stop"))
        assert c.chat("s", "u") is None
        assert c.last_error.startswith("shape:")

    def test_默认不读reasoning_content(self):
        """红线：思考内容是 raw CoT，取它等于把思维链送进训练数据。

        这里刻意用「只有 reasoning_content」的响应：宁可返回 None，
        也不能退回去读它。
        """
        payload = {"choices": [{"message": {
            "content": None, "reasoning_content": "思考过程……",
        }, "finish_reason": "stop"}]}
        c = _client(payload)
        assert c.chat("s", "u") is None
        assert "思考过程" not in c.last_error


class TestNoCredentialInSource:
    """仓库级红线：源码里不得出现任何看起来像凭据的字面量。

    比「我们记得别写进去」可靠——重构、改名、复制粘贴都会破誓，
    这条测试不会。
    """

    @pytest.mark.parametrize("rel", [
        "trajectory_pipeline/llm/client.py",
        "trajectory_pipeline/llm/schema_parse.py",
        "trajectory_pipeline/perception/llm_perceptor.py",
        "trajectory_pipeline/perception/factory.py",
        "trajectory_pipeline/tests/fakes.py",
        "trajectory_pipeline/output/pipeline/llm_probe.py",
    ])
    def test_无硬编码key(self, rel):
        from pathlib import Path

        src = Path(rel).read_text(encoding="utf-8")
        for marker in ("bgw_", "sk-", "Bearer sk-"):
            assert marker not in src, f"{rel} 里出现了疑似凭据字面量 {marker!r}"

    @pytest.mark.parametrize("rel", [
        "trajectory_pipeline/llm/client.py",
        "trajectory_pipeline/perception/factory.py",
    ])
    def test_凭据只从环境变量读(self, rel):
        """key 的来源必须唯一。否则迟早有人在某处硬编码一个「测试用」的 key。

        只查这两个文件——``schema_parse.py`` 与 ``llm_perceptor.py``
        本来就不接触凭据，对它们断言这条没有意义。"""
        from pathlib import Path

        src = Path(rel).read_text(encoding="utf-8")
        assert "environ" in src or "from_env" in src