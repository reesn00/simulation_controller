"""A3 守卫：agent 批次的 D-2 / D-5 / D-9 判据复验（方案 13 §6.3 / §6.4，INC58）。

§6.3 的调研结论是「天然兼容 + 守卫钉住」——本文件就是那些钉子。判据层
（code_scorer / d5_scorer / gates）**零改动**：它们吃的是 P2 形状，而
A2a 的同形纪律保证了两批同形。要钉的是**断链与误判**两类事：

1. **断链（A3-3 修的）**：agent_loop 此前不落 ``rationale`` → P2 切出的
   agent 样本 ``rationales=()`` → 闸门 ``not_run`` → D-9 永不触发——
   「D-9 纳入验收」会落空成一个永远缺分的维度。修复后这里钉全链：
   P1 落 ``native-think`` payload → P2 长出 rationale → 闸门有得跑 →
   D-9 触发口径与 scripted 同（成本控制也同）。
2. **误判**：时序核查对「引用后续步才出现的实体」的 think 必须抓得出
   （那是 agent 模式唯一的后见之明来源——think 是动作前动机，引用
   还没去的页面的内容就是 hindsight）；首步空窗口早退在 agent 形状上
   不得误伤（agent 链首条 rationale 步的窗口里有进站观察）。
3. **同分不同质**：两批过同一条闸门/同一位 judge，分数可比的前提是
   judge 手里有 think 原文（不是空 rationale）且来源可区分
   （P1 ``reason="native-think"`` vs writer 的 ``""``）。

D-2 现有口径只核 ``click`` 的 target（code_scorer 的 ``_click_actions``
按 ``tool == "click"`` 过滤，``fill`` 无校验产地）——「保持现有口径」
的决策 A 指的即是此实现，本文件不改它，只钉它的分类在 agent 形状上正确。
D-5 的全量守卫是 ``Test同源``（FailClosed 映射），这里只补一条 agent 链
的端到端冒烟：判断点 id 确实落进了 P2 的 outcome。
"""

from __future__ import annotations

import json

import pytest

from trajectory_pipeline.evaluation.scorers.code_scorer import score_d2
from trajectory_pipeline.evaluation.scorers.d9_scorer import score_d9
from trajectory_pipeline.rationale import gates

import trajectory_pipeline.tests.unit.test_agent_visit as tav


# ═══════════════════════════════════════════════════════════════════════
# 工具：复用真链 fixture（test_agent_visit），构造与判分
# ═══════════════════════════════════════════════════════════════════════


class _Judge:
    """D-9 的假 judge：记录收到的 prompt，回一个契约内 verdict。"""

    def __init__(self, verdict: str = "anchored") -> None:
        self.verdict = verdict
        self.prompts: list[tuple[str, str]] = []

    def chat(self, system: str, user: str) -> str:
        self.prompts.append((system, user))
        return json.dumps(
            {"evidence": "think 与观察逐项对照一致", "verdict": self.verdict,
             "confidence": 0.8},
            ensure_ascii=False,
        )


def _site_sample(archive: dict, tmp_path):
    """切一份 P1，返回第一个**带动作**的单元的 P2 样本（站点单元）。"""
    from trajectory_pipeline.assembler.schema import split_archive

    p = tmp_path / "guard.json"
    p.write_text(json.dumps(archive, ensure_ascii=False), encoding="utf-8")
    for sample in split_archive(p):
        if sample.actions:
            return sample
    raise AssertionError("切不出带动作的样本——fixture 链没产出动作流")


# ═══════════════════════════════════════════════════════════════════════
# 断链修复：think 落 P1 → P2 rationale → 闸门 → D-9 触发
# ═══════════════════════════════════════════════════════════════════════


class TestThink落链:
    async def test_agent循环步的rationale带native_think来源标记(self):
        """P1 层：循环执行的步都挂上 think payload，``reason="native-
        think"``——同分不同质核对的来源标记。进站 goto / 会话 new_tab
        是执行器自己的步，保持 ``None``（挂接点在循环侧，不在壳侧）。"""
        ar, al, _ = await tav._run_agent()
        doc = tav._visit_json(ar, al)
        steps = doc["steps"]
        # 形状：0=goto（进站） 1=new_tab（开独立 tab） 2+=循环步
        assert steps[0]["action"]["tool"] == "goto"
        assert steps[0]["rationale"] is None
        assert steps[1]["action"]["tool"] == "new_tab"
        assert steps[1]["rationale"] is None
        loop_steps = steps[2:]
        assert loop_steps, "fixture 链没有循环步——这条守卫没测到东西"
        for s in loop_steps:
            payload = s["rationale"]
            assert payload is not None, "agent 循环步没落 think——D-9 断链原样"
            assert payload["status"] == "written"
            assert payload["reason"] == "native-think"
            assert payload["rationale"]["text"]

    async def test_scripted链该字段保持None(self):
        """同形纪律的另一半：scripted 步（本 fixture 无 writer 接线）
        rationale 保持 ``None``——「来源标记」不会在 scripted 批凭空冒出来。"""
        sr, sl, _ = await tav._run_scripted()
        doc = tav._visit_json(sr, sl)
        assert all(s["rationale"] is None for s in doc["steps"])

    async def test_P2切出的样本有rationale且闸门跑过(self, tmp_path):
        """断链修复的端到端证据：agent 样本不再是 ``rationales=()``，
        闸门状态从 ``not_run`` 变成真跑（passed/failed 都算跑过）。"""
        ar, al, _ = await tav._run_agent()
        sample = _site_sample(tav._full_archive(ar, al, "agent"), tmp_path)
        assert [r.status for r in sample.rationales] == ["written"] * len(
            sample.rationales)
        assert sample.gate.status in ("passed", "failed")
        assert sample.gate.status != "not_run"

    async def test_失败步同样带think(self):
        """think 是动作前动机：动作执行失败（导航失败）的步也必须带——
        那一步的动机正是 D-9 最该看的原文。"""
        driver = tav._driver()          # 站内没注册坏 URL → goto 必失败
        ledger = tav.RunLedger(task_id="T1")
        result = await tav.agent_visit(
            driver,
            tav._player(True),
            ledger,
            tav._cand(),
            tav._backend(tav.FakeAgentLLM(
                [{"tool": "goto",
                  "params": {"url": "https://nope.test/dead"}}]
            )),
            title="功夫",
        )
        doc = tav._visit_json(result, ledger)
        failed = [s for s in doc["steps"] if s["error"]]
        assert failed, "fixture 没造出失败步——这条守卫没测到东西"
        for s in failed:
            assert s["action"]["origin"] == "model"
            assert s["action"]["tool"] == "goto"
            assert s["rationale"] is not None
            assert s["rationale"]["reason"] == "native-think"


# ═══════════════════════════════════════════════════════════════════════
# D-9 判据复验：时序 / 首步早退 / 触发口径
# ═══════════════════════════════════════════════════════════════════════


class TestD9判据复验:
    async def test_后见think被时序核查抓出并触发judge(self, tmp_path):
        """think 引用**下一步才出现**的页面内容 = hindsight——
        时序核查必须给出 temporal_leak（hard → 闸门 failed → D-9
        强制走 LLM）。这是 agent 模式后见之明的唯一抓法，抓不出就是
        「同分不同质」里「质」的失守。"""
        think = "点进去应该能看到「独家花絮抢先看」的段落"
        after = tav._obs(url="https://x.test/play/1", title="播放页",
                         body="正在播放，独家花絮抢先看", refs=("e9",))
        driver = tav._driver(after=(after,))
        ledger = tav.RunLedger(task_id="T1")
        result = await tav.agent_visit(
            driver,
            tav._player(True),
            ledger,
            tav._cand(),
            tav._backend(tav.FakeAgentLLM(
                [{"tool": "click", "ref": "e1", "think": think}, tav._done()]
            )),
            title="功夫",
        )
        sample = _site_sample(tav._full_archive(result, ledger, "agent"),
                              tmp_path)
        # think 挂在 click 那一步（step_index=2，窗口=[进站观察, 缺件占位]）
        assert sample.gate.status == "failed"
        assert any("temporal_leak" in r for r in sample.gate.reasons)

        judge = _Judge()
        verdict = score_d9(sample, llm_client=judge)
        assert verdict.kind == "scored"      # failed → 强制走 LLM 出分
        # judge 手里必须是 think 原文——空 rationale 出分是「同分不同质」
        assert think in judge.prompts[0][1]

    async def test_干净think走成本控制口径与scripted同(self, tmp_path):
        """闸门 passed + 无可判短板 → 不走 LLM（not_evaluated）——
        与 scripted 同一条成本控制口径，agent 批不得更宽松也不得更严。"""
        ar, al, _ = await tav._run_agent()
        sample = _site_sample(tav._full_archive(ar, al, "agent"), tmp_path)
        assert sample.gate.status == "passed"
        judge = _Judge()
        verdict = score_d9(sample, llm_client=judge)
        assert verdict.kind == "not_evaluated"
        assert judge.prompts == []           # 一个 LLM 调用都没烧

    def test_首步空窗口早退在agent形状上照走(self):
        """防御复验：若某条 agent 链的 think 落在 step 0（窗口真空），
        实体核查必须早退「首步无历史观察」，不得走「查不到 → 判幻觉」
        那条隐式路径——agent 链首条 rationale 步窗口里有进站观察
        （上组端到端已证），这条钉的是形状无关的兜底。"""
        obs = {"url": "https://x.test/",
               "body_text": "本站有 功夫 全集",
               "interactive_elements": [], "elements_total": 0}
        r = gates.gate(
            "这是 v.qq.com 的页面。", action_tool="goto",
            observations=[obs], step_index=0, exempt="功夫",
        )
        assert not [f for f in r.findings if f.severity == "hard"]
        assert any("首步无历史观察" in u for u in r.undeterminable)


# ═══════════════════════════════════════════════════════════════════════
# D-2 守卫：agent 形状的引用分类（§6.3 表的落点）
# ═══════════════════════════════════════════════════════════════════════


class _Ns:
    """``score_d2`` 的最小样本替身（只要 ``actions`` / ``observations``
    两个属性；actions 内的条目吃 Mapping 形态）。"""

    def __init__(self, actions, observations):
        self.actions = actions
        self.observations = observations


class TestD2守卫:
    async def test_真链样本的click引用判found(self, tmp_path):
        """FakeAgent 轨迹切出的 P2 样本：click 的 target label 在
        进站观察的元素里 → found → 5 分。§6.3 表「found/missing 正确
        分类」的 found 半边，用的就是真链而不是手搭的样本。"""
        ar, al, _ = await tav._run_agent()
        sample = _site_sample(tav._full_archive(ar, al, "agent"), tmp_path)
        verdict = score_d2(sample)
        assert verdict.kind == "scored" and verdict.score == 5
        assert verdict.refs and all(r.kind == "found" for r in verdict.refs)

    def test_引用不存在的实体判missing并扣分(self):
        """missing 半边：click target 不在任何完整观察里 → 2 分。
        agent 形状（origin=model、target 带 label）下分类不得漂移。"""
        sample = _Ns(
            actions=[{"tool": "click", "origin": "model",
                      "target": {"tag": "a", "label": "不存在的按钮"}}],
            observations=[{
                "url": "https://x.test/", "body_text": "欢迎光临",
                "interactive_elements": [
                    {"ref": "e1", "tag": "a", "label": "首页"},
                ],
                "elements_total": 1, "links": [], "links_total": 0,
            }],
        )
        verdict = score_d2(sample)
        assert verdict.kind == "scored" and verdict.score == 2
        assert verdict.refs[0].kind == "missing"

    def test_无click样本不出满分(self):
        """只导航就被拦下的 agent 样本没有可校验引用 → undetermined，
        拿不到「全部可找到」的 5 分——「没查」不得渲染成「查过全对」。"""
        sample = _Ns(
            actions=[{"tool": "goto", "origin": "model",
                      "params": {"url": "https://x.test/"}}],
            observations=[{"url": "https://x.test/", "body_text": "x"}],
        )
        verdict = score_d2(sample)
        assert verdict.kind == "undetermined" and verdict.score is None


# ═══════════════════════════════════════════════════════════════════════
# D-5 冒烟：agent 链的判断点 id 落进 P2 outcome
# ═══════════════════════════════════════════════════════════════════════


class TestD5冒烟:
    async def test_agent链的outcome带判断点与播放页标记(self, tmp_path):
        """D-5 锚 ``outcome.(branch/question/reached_play_page)``——
        agent 链的 ⑦ 与 scripted 共用 ``judge_player``，这三个字段必须
        照常在 P2 样本上。全量守卫（FailClosed 映射 Test同源）之外，
        这条钉的是「agent 链没把它们弄丢」。

        用 SPA 形态（点击不换 URL，播放页就是候选地址）——配对按
        候选/落地 url 走（见下一组的缺口钉板）。"""
        from trajectory_pipeline.perception.base import Q

        spa_play = tav._obs(url=tav.CAND_URL, title="播放页",
                            body="正在播放 功夫 第一集", refs=("e9",))
        driver = tav._driver(after=(spa_play,))
        ledger = tav.RunLedger(task_id="T1")
        result = await tav.agent_visit(
            driver,
            tav._player(True),
            ledger,
            tav._cand(),
            tav._backend(tav.FakeAgentLLM([{"tool": "click", "ref": "e1"}])),
            title="功夫",
        )
        sample = _site_sample(tav._full_archive(result, ledger, "agent"),
                              tmp_path)
        assert sample.outcome is not None
        assert sample.outcome.question == Q.PLAYER_OK
        assert sample.outcome.reached_play_page is True
        assert sample.outcome.branch is None          # 成功访问

    async def test_根候选到播放页的outcome经播放页url配对(self, tmp_path):
        """**钉修复**（INC59，A3-4 前置）：访问从根页导航到播放页时，
        outcome 记在播放页 url 上，配对第三档试 ``player_obs.url``
        必须配上——scripted 与 agent 两条链都钉。INC58 时这条还是
        「缺口钉板」（两模式双双 ``outcome_missing``），修复后同一
        fixture 改钉「配上」，防回归。"""
        ar, al, _ = await tav._run_agent()
        adoc = tav._full_archive(ar, al, "agent")
        play_url = adoc["visits"][0]["player_obs"]["url"]
        assert play_url != adoc["visits"][0]["url"]   # 根 → 播放页的换址
        sr, sl, _ = await tav._run_scripted()
        sdoc = tav._full_archive(sr, sl, "scripted")
        assert sdoc["visits"][0]["player_obs"]["url"] == play_url

        from trajectory_pipeline.assembler.schema import split_archive

        for doc in (adoc, sdoc):
            sample = _site_sample(doc, tmp_path)
            assert sample.outcome is not None
            assert sample.outcome.url == play_url
            assert "outcome_missing" not in sample.degraded_from
