"""测试替身——**假 LLM 后端**。

⚠️ 放这里而不是 ``tests/unit/`` 或 ``tests/contract/``：那两个子目录
**没有** ``__init__.py``（它们的测试文件按 basename 加载），跨目录绝对
导入会 ``ImportError``。``tests/`` 本身有，所以 ``trajectory_pipeline.tests.fakes``
是唯一能被两处引用的位置。

关于 fake 的定位（决定它「答得对」还是「乱答」）
----------------------------------------------
:class:`FakeLLM` **模拟一个业务上答得对的模型**：页面上有播放控件就
回一个真实 ref，没有就回 ``null``。契约测试（七不变式）验的是
:class:`~trajectory_pipeline.perception.llm_perceptor.LLMPerceptor`
的**代码层守卫**——采集预检、ref 白名单、fail-closed——而 LLM 的语义
能力是另一件事，只能靠真实验证测（见 ``docs/02-避坑指南.md``）。
若 fake 随机乱答，契约测试就变成测「LLM 版能不能处理乱答」，
与七不变式无关，且会掩盖真正要测的东西。

**真实的坏行为由独立开关模拟**，见各 ``mode`` 参数——它们不是「让 fake
变傻」，而是复现 ``output/pipeline/llm_probe.py`` 里实测到的后端行为：
套 ``<think>``、套 markdown fence、输出散文、编造 ref。
"""

from __future__ import annotations

import json
from typing import Any

from trajectory_pipeline.perception import llm_perceptor as lp
from trajectory_pipeline.perception.rule_perceptor import classify_trailer, is_play_control


class FakeLLM:
    """假后端。接口与 :class:`trajectory_pipeline.llm.client.LLMClient` 一致。"""

    def __init__(
        self,
        *,
        down: bool = False,
        think: bool = False,
        fence: bool = False,
        prose: bool = False,
        lie_about_ref: str = "",
        invent_urls: bool = False,
        answers: dict[str, bool] | None = None,
    ) -> None:
        #: 后端整体不可用（连接失败/超时）
        self.down = down
        #: 输出套 ``<think>`` 包裹（**raw CoT 红线的实测形态**）
        self.think = think
        #: 输出套 ```json 代码块
        self.fence = fence
        #: 输出散文而非 JSON
        self.prose = prose
        #: 回传这个 ref（无论它是否真的存在）——验证白名单守卫
        self.lie_about_ref = lie_about_ref
        #: ① 里编造不在观察中的 url
        self.invent_urls = invent_urls
        #: 逐题强制答案（键为 question id）
        self.answers = answers or {}
        #: 记录每次请求，供断言「问了几次、system 里带了什么」
        self.calls: list[tuple[str, str]] = []
        #: 与真客户端同名字段：失败原因
        self.last_error = ""

    # ── 接口 ──────────────────────────────────────────────────────

    def chat(self, system: str, user: str, *, max_tokens: int = 1024) -> str | None:
        self.calls.append((system, user))
        if self.down:
            self.last_error = "transport:ConnectError"
            return None
        qid, obs = _request(user)
        if qid is None:
            self.last_error = "shape:KeyError"
            return None
        payload = self._build(qid, obs)
        if self.prose:
            return _prose_for(qid)
        raw = json.dumps(payload, ensure_ascii=False)
        if self.think:
            raw = f"<think>让我看看页面上有什么</think>\n{raw}"
        if self.fence:
            raw = f"```json\n{raw}\n```"
        return raw

    def health(self) -> tuple[bool, str]:
        return (not self.down), "fake 后端"

    # ── 各题应答 ──────────────────────────────────────────────────

    def _build(self, qid: str, obs: dict[str, Any]) -> dict[str, Any]:
        if qid == lp.Q.SELECT_PLAY_SITES:
            links = obs.get("links") or []
            if self.invent_urls:
                return {"selected": [{"url": "https://编造.test/x",
                                      "title": "编的", "why": "看着像"}],
                        "rejected": []}
            return {"selected": [{"url": links[0].get("href", ""),
                                  "title": links[0].get("text", ""),
                                  "why": "标题含目标片名"}] if links else [],
                    "rejected": []}
        if qid == lp.Q.FIND_PLAY_CONTROL:
            if self.lie_about_ref:
                return {"ref": self.lie_about_ref, "trailer_only": False}
            for e in obs.get("interactive_elements") or []:
                if is_play_control(e.get("label", "")):
                    return {"ref": e.get("ref", ""), "trailer_only": False,
                            "why": f"控件文本 {e.get('label','')!r}"}
            return {"ref": None, "trailer_only": False, "why": "未见播放控件"}
        # IS_REACHABLE / PLAYER_OK
        return {"answer": self.answers.get(qid, True), "evidence": "页面主体可见且可交互"}

    def close(self) -> None:
        pass


def _request(user: str) -> tuple[str, dict[str, Any]]:
    """从请求正文里取 ``(题目, 观察)``。

    **从正文里的 ``question`` 字段读题号，不靠 system prompt 的契约串
    反查。** 实测踩到的坑：``IS_REACHABLE`` 与 ``PLAYER_OK`` 的输出契约
    字面完全相同（同为 ``bool + evidence``），任何字符串反查都会认错，
    而认错的后果是**无声**的——一次 FIND_PLAY_CONTROL 被当成
    IS_REACHABLE 回答，``ref`` 就没了，判定从 True 掉成 None。
    """
    try:
        body = json.loads(user)
    except ValueError:
        return "", {}
    return str(body.get("question") or ""), body.get("observation") or {}


def _prose_for(qid: str) -> str:
    """散文形态——实测模型在没被强制 JSON 时的默认输出。"""
    return (
        "根据页面观察分析：\n\n"
        "1. 页面标题与目标作品一致\n"
        "2. 页面主体内容可见\n"
        "3. 未发现明显的错误页特征\n\n"
        "**结论：这是一个正常可访问的页面。**"
    )


def llm_perceptor(**kwargs: Any) -> lp.LLMPerceptor:
    """构造一个接假后端的 :class:`LLMPerceptor`（契约测试用）。"""
    return lp.LLMPerceptor(FakeLLM(**kwargs))