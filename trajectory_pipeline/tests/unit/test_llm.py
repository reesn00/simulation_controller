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


class TestUnescapedQuoteRepair:
    """**实测形态**：模型在 JSON 字符串值里写未转义的 ASCII 双引号。

    2026-10-10 后端 ``deepseek-v4.1-flash``，同一提示词**时好时坏**
    （4 次里坏 2 次）。可怕之处是**大括号配平**——``_balanced`` 取出来
    那一段看起来完全正常，只有 ``json.loads`` 知道它坏了。
    而坏掉的症状是「解析失败」，在感知层就是**整道题 fail-closed 成
    ``None``**：把解析故障记成「模型判不出来」。
    """

    #: 实测那条的缩微版：中文逗号跟在引号后面。
    REAL = '{"thinking": "标题只是笼统的"腾讯视频"，正文只有导航。", "conclusion": "无法确认"}'

    def test_修好后语义不变(self):
        got = sp.extract_json(self.REAL)
        assert got["thinking"] == '标题只是笼统的"腾讯视频"，正文只有导航。'
        assert got["conclusion"] == "无法确认"

    def test_修的动作被记账(self):
        got, repairs = sp.extract_json_reporting(self.REAL)
        assert got is not None
        assert len(repairs) == 2, "两个坏引号要记两笔，不能合并成一条"
        assert all("未转义" in x for x in repairs)

    def test_本就合法时不记账(self):
        """合法 JSON 的修复记录必须为空——否则「被修过」这个信号会失效。"""
        got, repairs = sp.extract_json_reporting('{"a":"他说\\"播放\\"","n":1}')
        assert got["a"] == '他说"播放"'
        assert repairs == ()

    def test_旧入口照旧返回对象(self):
        """``extract_json`` 保持签名与返回值，感知层那边不用改。"""
        assert sp.extract_json(self.REAL) == sp.extract_json_reporting(self.REAL)[0]

    @pytest.mark.parametrize(
        "raw",
        [
            '{"a": 1,}',  # 尾逗号
            '{"a": "没写完',  # 未闭合（max_tokens 用尽）
            "{'a': 1}",  # 单引号键
            '{"a" 1}',  # 缺冒号
            '{"a": "x\ty"}',  # 未转义控制字符
            "完全不是 JSON 的一段话",
        ],
    )
    def test_别的畸形一律不修(self, raw):
        """**刻意窄。** 尾逗号、单引号键、缺冒号的「正确修法」要靠猜字段结构，
        而猜出来的字段名会被当成模型的结论用——感知层猜错的是 ref，
        而 ref 拿它去点，**点错不可逆**。

        未闭合（截断）尤其不能修：把它接上就是凭空补内容。
        """
        assert sp.extract_json(raw) is None

    def test_任意位置截断都不得被修成假对象(self):
        """**性质断言**，不是样例断言。

        修复的判据是「解析器报错的位置恰好是一个引号」。这条断言覆盖
        **把一条真含未转义引号的合法 JSON 从每个位置砍断**——也就是
        ``max_tokens`` 用尽的全部形态。它证明截断不会被「修」成
        凭空补出来的对象：转义掉那个**开**引号之后，下一轮报的是
        「对象开头缺键名」，那个位置不是引号，于是放弃。

        ⚠️ 曾经给修复加过一道「按错误类型白名单」的第二道门，它看着在
        限定范围，删掉它本测试与上面全部用例**照样全绿**——枚举 17 种
        畸形输入没有一种靠它救回来。它是死守卫，已删。这条性质断言
        才是真正起作用的那道。
        """
        src = (
            '{"thinking": "标题只是笼统的"腾讯视频"，正文只有导航。",'
            ' "conclusion": "无法确认"}'
        )
        for i in range(1, len(src)):
            assert sp.extract_json(src[:i]) is None, f"截断在第 {i} 字处被修成了对象"

    def test_修不好时返回None而不是半成品(self):
        """坏引号超过上限时必须**放弃**，绝不能返回「修了一半」的对象。"""
        raw = '{"a": "' + '"'.join(f"片段{i}" for i in range(40)) + '"}'
        assert sp.extract_json(raw) is None

    def test_只修引号不碰其他字符(self):
        """修复只能动引号——动别的字符就是**改内容**而不是修格式。"""
        raw = '{"a": "未闭合, 尾逗号: },"b": 2}'
        assert sp.extract_json(raw) is None

    def test_嵌套结构修复后仍取外层(self):
        """修复不能破坏「从头往后取第一个 ``{``」这条口径（见 TestExtractJson）。"""
        raw = '{"selected":[{"url":"https://a.test","why":"见"首页"处"}],"rejected":[]}'
        got = sp.extract_json(raw)
        assert got["selected"][0]["why"] == '见"首页"处'
        assert got["rejected"] == []

    def test_修复上限本身有界(self):
        """上限必须是常量而不是无限循环——无上限的修复在「怎么都修不好」
        时会吃满 CPU，而症状与不修一样，只是更慢。"""
        assert 0 < sp.MAX_REPAIR_ROUNDS <= 32


class TestAsBool:
    @pytest.mark.parametrize(
        "raw,want",
        [
            (True, True),
            (False, False),
            ("true", True),
            ("True", True),
            ("是", True),
            ("1", True),
            ("false", False),
            ("否", False),
            ("0", False),
            (1, True),
            (0, False),
            (1.0, True),
        ],
    )
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
    @pytest.mark.parametrize(
        "raw,want", [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0), ("0.7", 0.7)]
    )
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
        assert (
            LLMConfig(base_url="http://x/v1", model="m").endpoint
            == "http://x/v1/chat/completions"
        )
        assert (
            LLMConfig(base_url="http://x", model="m").endpoint
            == "http://x/v1/chat/completions"
        )
        assert (
            LLMConfig(base_url="http://x/", model="m").endpoint
            == "http://x/v1/chat/completions"
        )

    def test_repr不含key(self):
        """凭据红线：config 一旦被 log 或带进 traceback 就等于泄漏。"""
        c = LLMConfig(base_url="http://x", model="m", api_key="secret-value")
        assert "secret-value" not in repr(c)
        assert "redacted" in repr(c)

    def test_无key时用占位(self):
        """本机 vLLM 不校验 key。"""
        assert (
            LLMConfig.from_env(
                env={
                    "TRAJECTORY_LLM_BASE_URL": "http://x",
                    "TRAJECTORY_LLM_MODEL": "m",
                }
            ).api_key
            == "not-needed"
        )

    def test_默认超时为90(self):
        """30s 对判断点 ① 不够（实测 5 条里超时 3 条），默认值校准到 90
        （2026-10-11）。**换机器不设环境变量也不该再踩 30s。**"""
        cfg = LLMConfig.from_env(
            env={
                "TRAJECTORY_LLM_BASE_URL": "http://x",
                "TRAJECTORY_LLM_MODEL": "m",
            }
        )
        assert cfg.timeout_s == 90.0

    def test_超时非法报错(self):
        with pytest.raises(LLMUnavailable, match="不是数字"):
            LLMConfig.from_env(
                env={
                    "TRAJECTORY_LLM_BASE_URL": "http://x",
                    "TRAJECTORY_LLM_MODEL": "m",
                    "TRAJECTORY_LLM_TIMEOUT_S": "abc",
                }
            )

    def test_超时应为正(self):
        with pytest.raises(LLMUnavailable, match="必须为正"):
            LLMConfig.from_env(
                env={
                    "TRAJECTORY_LLM_BASE_URL": "http://x",
                    "TRAJECTORY_LLM_MODEL": "m",
                    "TRAJECTORY_LLM_TIMEOUT_S": "0",
                }
            )

    def test_可序列化不含key(self):
        """``dump_for_debug`` 进日志，故也不能含 key。"""
        from trajectory_pipeline.llm.client import dump_for_debug

        c = LLMConfig(base_url="http://x", model="m", api_key="secret-value")
        assert "secret-value" not in dump_for_debug(c)


class TestEscalateConfig:
    """升配后端的配置解析（方案 13 §3.5 G2，A2a-4）。

    三态语义：全没配 → ``None``（无升配，不 fail）；全配 → 接上；
    半配 → 报错不猜。键名刻意带 ``ESCALATE_`` 中缀，与主后端三键互相
    独立——两台机器两份模型，共用键名等于把「升配」偷换成「同一台机器
    换模型」。
    """

    def test_三键全没配返回None(self):
        assert LLMConfig.escalate_from_env(env={}) is None

    def test_主后端键不触发升配(self):
        """只配主后端三键 = 无升配。升配是**额外**能力，不配是常态。"""
        assert LLMConfig.escalate_from_env(
            env={"TRAJECTORY_LLM_BASE_URL": "http://x",
                 "TRAJECTORY_LLM_MODEL": "m"}
        ) is None

    def test_只配BASE_URL报错(self):
        with pytest.raises(LLMUnavailable, match="TRAJECTORY_LLM_ESCALATE_MODEL"):
            LLMConfig.escalate_from_env(env={"TRAJECTORY_LLM_ESCALATE_BASE_URL": "http://x"})

    def test_只配MODEL报错(self):
        with pytest.raises(LLMUnavailable,
                           match="TRAJECTORY_LLM_ESCALATE_BASE_URL"):
            LLMConfig.escalate_from_env(env={"TRAJECTORY_LLM_ESCALATE_MODEL": "big"})

    def test_只配KEY也算半配报错(self):
        """光给个 key 说不出升配指向哪台机器——同样是想配没配齐。"""
        with pytest.raises(LLMUnavailable, match="缺少环境变量"):
            LLMConfig.escalate_from_env(env={"TRAJECTORY_LLM_ESCALATE_API_KEY": "k"})

    def test_配齐两键无KEY用占位(self):
        cfg = LLMConfig.escalate_from_env(env={
            "TRAJECTORY_LLM_ESCALATE_BASE_URL": "http://x",
            "TRAJECTORY_LLM_ESCALATE_MODEL": "big",
        })
        assert cfg is not None and cfg.api_key == "not-needed"

    def test_继承主后端的timeout与max_tokens(self):
        """升配接的是同一类请求（agent 指令 + 推理预算）：主后端上校准过的
        输出预算原样带过去，被类默认值悄悄放大/缩小的代价都是真金白银。"""
        base = LLMConfig(base_url="http://x", model="m",
                         timeout_s=42.0, max_tokens=2048)
        cfg = LLMConfig.escalate_from_env(
            env={"TRAJECTORY_LLM_ESCALATE_BASE_URL": "http://y",
                 "TRAJECTORY_LLM_ESCALATE_MODEL": "big"},
            base=base,
        )
        assert cfg is not None
        assert cfg.timeout_s == 42.0 and cfg.max_tokens == 2048

    def test_不传base用类默认(self):
        cfg = LLMConfig.escalate_from_env(env={
            "TRAJECTORY_LLM_ESCALATE_BASE_URL": "http://y",
            "TRAJECTORY_LLM_ESCALATE_MODEL": "big",
        })
        assert cfg is not None and cfg.timeout_s == 90.0

    def test_repr同样不含key(self):
        """凭据红线对升配后端同等有效——它也会被 log / traceback 带出去。"""
        cfg = LLMConfig.escalate_from_env(env={
            "TRAJECTORY_LLM_ESCALATE_BASE_URL": "http://y",
            "TRAJECTORY_LLM_ESCALATE_MODEL": "big",
            "TRAJECTORY_LLM_ESCALATE_API_KEY": "esc-secret",
        })
        assert cfg is not None
        assert "esc-secret" not in repr(cfg)


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
        #: 每次请求**完整**的 payload。A2a-1 起要断言「messages 原样透传」
        #: 与「不带 tools」，只记 ``max_tokens`` 不够。
        self.sent: list = []
        self.closed = False

    def post(self, url, *, json, headers):
        self.budgets.append(json["max_tokens"])
        self.sent.append(json)
        return _Resp(self.payload, self.status_code)


class _BoomTransport:
    """``post`` 固定抛异常——模拟连不上 / 超时 / DNS 失败。"""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    def post(self, url, *, json, headers):
        self.calls += 1
        raise self.exc

    def close(self):
        pass

    def close(self):
        self.closed = True


def _client(payload, **kw):
    """造一个真 :class:`LLMClient`，只把传输层换成替身。"""
    from trajectory_pipeline.llm.client import LLMClient

    c = LLMClient(LLMConfig(base_url="http://x", model="m", **kw))
    c._client = _Transport(payload)
    return c


def _ok(content, finish: str = "stop") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


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
        assert (
            LLMConfig(base_url="http://x", model="m").max_tokens == DEFAULT_MAX_TOKENS
        )

    def test_环境变量可调(self):
        c = LLMConfig.from_env(
            env={
                "TRAJECTORY_LLM_BASE_URL": "http://x",
                "TRAJECTORY_LLM_MODEL": "m",
                "TRAJECTORY_LLM_MAX_TOKENS": "16384",
            }
        )
        assert c.max_tokens == 16384

    def test_预算非法报错(self):
        with pytest.raises(LLMUnavailable, match="不是整数"):
            LLMConfig.from_env(
                env={
                    "TRAJECTORY_LLM_BASE_URL": "http://x",
                    "TRAJECTORY_LLM_MODEL": "m",
                    "TRAJECTORY_LLM_MAX_TOKENS": "很多",
                }
            )

    def test_预算应为正(self):
        with pytest.raises(LLMUnavailable, match="必须为正"):
            LLMConfig.from_env(
                env={
                    "TRAJECTORY_LLM_BASE_URL": "http://x",
                    "TRAJECTORY_LLM_MODEL": "m",
                    "TRAJECTORY_LLM_MAX_TOKENS": "0",
                }
            )

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
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "reasoning_content": "思考过程……",
                    },
                    "finish_reason": "stop",
                }
            ]
        }
        c = _client(payload)
        assert c.chat("s", "u") is None
        assert "思考过程" not in c.last_error


class TestChatMessages:
    """多轮入口（方案 12 A2a-1）——agent 循环的调用面。

    与 :meth:`chat` 是**同一个出口、同一套失败语义**（请求循环抽在
    ``_exchange`` 里共用），所以这里断言的不是「能不能调通」，而是三件
    容易悄悄漂移的事：

    1. **消息原样透传**——客户端不合并 system、不裁剪历史。加工了就会
       让「模型实际看到什么」有两份真相，而轨迹存档要求唯一真相源。
    2. **不带 ``tools`` / ``tool_choice``**——D-B1 拍板走 JSON 动作指令
       的文本通道，加上去等于把半个原生 tool_calls 协议引进来，而 P3
       视图是从 Action 渲染的，两套形状会打架。
    3. **失败分类与 ``chat`` 一字不差**——重试策略分叉的后果是「某一类
       失败只在其中一条路径上被正确处理」，它要等真实后端出状况才暴露。
    """

    @staticmethod
    def _msgs() -> list[dict[str, str]]:
        return [
            {"role": "system", "content": "你是站内导航助手"},
            {"role": "user", "content": "目标片名：功夫"},
            {"role": "assistant", "content": '{"tool": "goto", "ref": "e1"}'},
            {"role": "user", "content": '{"observation": {"links": []}}'},
        ]

    def test_消息原样透传(self):
        c = _client(_ok("hi"))
        result = c.chat_messages(self._msgs())
        assert result.content == "hi"
        assert c._client.sent[0]["messages"] == self._msgs()

    def test_不带tools与tool_choice(self):
        """D-B1：JSON 动作指令走文本通道，不引原生 tool_calls 协议。"""
        c = _client(_ok("hi"))
        c.chat_messages(self._msgs())
        assert "tools" not in c._client.sent[0]
        assert "tool_choice" not in c._client.sent[0]

    def test_历史不裁剪(self):
        """客户端不加工消息——裁剪是 agent_loop 的事（那是步数预算的下游）。"""
        c = _client(_ok("hi"))
        long = self._msgs() * 3
        c.chat_messages(long)
        assert len(c._client.sent[0]["messages"]) == len(long)

    def test_usage回传(self):
        payload = _ok("hi")
        payload["usage"] = {"prompt_tokens": 456, "completion_tokens": 78,
                            "total_tokens": 534}
        r = _client(payload).chat_messages(self._msgs())
        # total_tokens 刻意不取：由两者相加可得，记下来就多一份要同步的东西
        assert r.usage == {"prompt_tokens": 456, "completion_tokens": 78}

    def test_后端没给usage时为None(self):
        """**不补 0**：账本上「0」与「没给」是两个事实，补了成本分母就错了。"""
        r = _client(_ok("hi")).chat_messages(self._msgs())
        assert r.usage is None

    def test_usage只认int且排除bool(self):
        payload = _ok("hi")
        payload["usage"] = {"prompt_tokens": "456", "completion_tokens": 78}
        # 字符串形态当没给——转换规则一放宽，各后端各写一种写法，账本就
        # 变成不可比的，而可比是它唯一的价值。
        assert _client(payload).chat_messages(self._msgs()).usage == {
            "completion_tokens": 78}

    def test_usage全坏时为None而不是空dict(self):
        payload = _ok("hi")
        payload["usage"] = {"prompt_tokens": True, "completion_tokens": None}
        assert _client(payload).chat_messages(self._msgs()).usage is None

    def test_latency是整数(self):
        r = _client(_ok("hi")).chat_messages(self._msgs())
        assert isinstance(r.latency_ms, int) and r.latency_ms >= 0

    def test_失败时content与usage同时为None(self):
        """一次没跑完的请求没有可信的 token 数——usage 不能靠猜。"""
        r = _client(_ok(None, finish="stop")).chat_messages(self._msgs())
        assert r.content is None
        assert r.usage is None
        assert r.error.startswith("shape:")

    def test_4xx不重试(self):
        c = _client(_ok("hi"), max_retries=2)
        c._client = _Transport({}, status_code=404)
        r = c.chat_messages(self._msgs())
        assert r.content is None
        assert r.error == "http:404"
        assert len(c._client.budgets) == 1

    def test_5xx重试到底(self):
        # max_retries=1：够证「重试真的发生了」（2 次请求），又不让门禁
        # 白等一次 4s 退避——退避时长本身不是这里要断言的东西。
        c = _client(_ok("hi"), max_retries=1)
        c._client = _Transport({}, status_code=503)
        r = c.chat_messages(self._msgs())
        assert r.content is None
        assert r.error == "http:503"
        assert len(c._client.budgets) == 2

    def test_传输层异常重试到底(self):
        from trajectory_pipeline.llm.client import LLMClient

        c = LLMClient(LLMConfig(base_url="http://x", model="m", max_retries=1))
        c._client = _BoomTransport(RuntimeError("boom"))
        r = c.chat_messages(self._msgs())
        assert r.content is None
        assert r.error == "transport:RuntimeError"
        assert c._client.calls == 2
        assert c.last_error == "transport:RuntimeError"

    def test_预算用尽与其它chat同口径(self):
        r = _client(_ok(None, finish="length")).chat_messages(self._msgs())
        assert r.content is None
        assert r.error.startswith("budget:")
        assert "TRAJECTORY_LLM_MAX_TOKENS" in r.error

    def test_空消息不发请求(self):
        """调用方的装配错误——发一次 90s 超时只为换一个 400，不值。"""
        c = _client(_ok("hi"))
        r = c.chat_messages([])
        assert r.content is None
        assert r.error == "shape:empty_messages"
        assert c._client.sent == []

    def test_chat仍是单轮两消息(self):
        """回归守卫：``chat`` 改成薄壳后，十一道判断题的请求形态不能变。"""
        c = _client(_ok("hi"))
        assert c.chat("s", "u") == "hi"
        assert c._client.sent[0]["messages"] == [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ]

    def test_成功后last_error不清空(self):
        """``last_error`` 的语义是「上一次失败的原因」——原实现成功时不
        清除，diag 路径依赖这一点，抽壳后保持不动。"""
        c = _client(_ok("hi"))
        c._client = _Transport({}, status_code=500)
        c.chat("s", "u")
        assert c.last_error == "http:500"
        c._client = _Transport(_ok("hi"))
        assert c.chat("s", "u") == "hi"
        assert c.last_error == "http:500"


class TestFake多轮接口:
    """``FakeLLM.chat_messages`` 与单轮 ``chat`` 必须**同一个脑子**。

    抽 ``_respond`` 之前，两个入口各走一份 dispatch 与坏行为开关——
    分叉的后果是「某个开关只在单轮形态下生效」，而它要等真实批次里
    那条路径第一次被走到才暴露，那时已经分不清是模型的问题还是 fake
    的问题。
    """

    @staticmethod
    def _body() -> str:
        from trajectory_pipeline.perception import llm_perceptor as lp

        return json.dumps({"question": lp.Q.IS_REACHABLE})

    def _msgs(self) -> list[dict[str, str]]:
        return [{"role": "system", "content": "sys"},
                {"role": "user", "content": self._body()}]

    def test_多轮与单轮同内容(self):
        from trajectory_pipeline.tests.fakes import FakeLLM

        fake = FakeLLM()
        single = fake.chat("sys", self._body())
        r = fake.chat_messages(self._msgs())
        assert r.content == single
        assert r.error == ""

    def test_usage默认有值且可关(self):
        from trajectory_pipeline.tests.fakes import FakeLLM

        r = FakeLLM().chat_messages(self._msgs())
        assert r.usage == {"prompt_tokens": 128, "completion_tokens": 32}
        # 「后端没给 usage」的形态：账本要记 None，不是 0
        assert FakeLLM(no_usage=True).chat_messages(self._msgs()).usage is None

    def test_失败形态与单轮一致(self):
        from trajectory_pipeline.tests.fakes import FakeLLM

        for kwargs, prefix in (
            ({"down": True}, "transport:"),
            ({"budget_exhausted": True}, "budget:"),
        ):
            fake = FakeLLM(**kwargs)
            r = fake.chat_messages(self._msgs())
            assert r.content is None, kwargs
            assert r.error.startswith(prefix), kwargs
            assert r.usage is None, kwargs
            assert fake.last_error.startswith(prefix), kwargs

    def test_记账取首system末user(self):
        from trajectory_pipeline.tests.fakes import FakeLLM

        fake = FakeLLM()
        msgs = [
            {"role": "system", "content": "第一条 system"},
            {"role": "user", "content": "第一次问"},
            {"role": "assistant", "content": "第一次答"},
            {"role": "user", "content": self._body()},
        ]
        fake.chat_messages(msgs)
        assert fake.calls[-1] == ("第一条 system", self._body())
        assert fake.budgets[-1] is None

    def test_坏行为开关在多轮形态下同样生效(self):
        """开关表是 fake 的价值所在——它必须**两个入口都生效**。"""
        from trajectory_pipeline.tests.fakes import FakeLLM

        r = FakeLLM(prose=True).chat_messages(self._msgs())
        assert r.content is not None and "结论" in r.content
        r = FakeLLM(fence=True).chat_messages(self._msgs())
        assert r.content is not None and r.content.startswith("```json")
        r = FakeLLM(think_only=True).chat_messages(self._msgs())
        assert r.content is not None and r.content.startswith("<think>")


class TestNoCredentialInSource:
    """仓库级红线：源码里不得出现任何看起来像凭据的字面量。

    比「我们记得别写进去」可靠——重构、改名、复制粘贴都会破誓，
    这条测试不会。
    """

    @pytest.mark.parametrize(
        "rel",
        [
            "trajectory_pipeline/llm/client.py",
            "trajectory_pipeline/llm/schema_parse.py",
            "trajectory_pipeline/perception/llm_perceptor.py",
            "trajectory_pipeline/perception/factory.py",
            "trajectory_pipeline/tests/fakes.py",
            "trajectory_pipeline/perception/probe_llm.py",
        ],
    )
    def test_无硬编码key(self, rel):
        from pathlib import Path

        src = Path(rel).read_text(encoding="utf-8")
        for marker in ("bgw_", "sk-", "Bearer sk-"):
            assert marker not in src, f"{rel} 里出现了疑似凭据字面量 {marker!r}"

    @pytest.mark.parametrize(
        "rel",
        [
            "trajectory_pipeline/llm/client.py",
            "trajectory_pipeline/perception/factory.py",
        ],
    )
    def test_凭据只从环境变量读(self, rel):
        """key 的来源必须唯一。否则迟早有人在某处硬编码一个「测试用」的 key。

        只查这两个文件——``schema_parse.py`` 与 ``llm_perceptor.py``
        本来就不接触凭据，对它们断言这条没有意义。"""
        from pathlib import Path

        src = Path(rel).read_text(encoding="utf-8")
        assert "environ" in src or "from_env" in src
