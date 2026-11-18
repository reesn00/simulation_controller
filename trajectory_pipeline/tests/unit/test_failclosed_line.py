"""缺件负样本线（fail-closed 第三类信号 · 方案 26 §6.4 · H-41 阶段 3）的测试。

⚠️ **这组测试盯的不是「造出来了」，是「造得对」**：

- 缺件之后**标记必须是真的**（回执里出现 ``[内容不完整: …]`` /
  ``[采集降级: …]``）——标记是假的 ⇒ 这条线教的是「标记可以乱写」；
- 期望答案**必须由结论派生**（``unresolved`` → 收尾话术）——手写话术
  ⇒ 模型学到两种说法；
- **只可能漏，不可能造**（正文行是原文的子集）；
- **别的站点保留真实结论**——一并改成「判不了」就是改写真实事实；
- **不进主池**（目录分开 + 不被当成 P1 存档）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_pipeline.assembler import builder, closing, failclosed, observation_view, views
from trajectory_pipeline.assembler import schema


# ── 夹具 ────────────────────────────────────────────────────────────


def _obs(url: str, **kw) -> dict:
    base = {
        "url": url, "page_title": "T", "body_text": "第一行\n第二行\n第三行\n第四行",
        "body_len": 4, "body_source": "inner_text", "truncated": False,
        "raw_len": 4, "stripped_ratio": 1.0, "degraded": [],
        "video_tag_count": 0, "iframe_count": 0,
        "interactive_elements": [], "links": [],
    }
    base.update(kw)
    return base


def _archive(**over) -> dict:
    steps = [{"action": {"tool": "goto", "params": {"url": "https://s.test/?q=1"},
                         "origin": "model", "target": None},
              "observation": _obs("https://s.test/?q=1"), "error": ""}]
    #: **第一个站点**：被排除的站（真实结论 ``no_play_control``）——
    #: 它**不该**被缺件线改判（改了就成改写真实事实）。
    asteps = [{"action": {"tool": "click", "params": {},
                          "origin": "model",
                          "target": {"tag": "button", "label": "详情"}},
               "observation": _obs("https://a.test/movie"), "error": ""}]
    vsteps = [
        {"action": {"tool": "click", "params": {},
                    "origin": "model",
                    "target": {"tag": "button", "label": "立即播放"}},
         "observation": _obs("https://x.test/play", video_tag_count=1,
                             interactive_elements=[{"tag": "button", "label": "立即播放"}]),
         "error": ""},
        {"action": {"tool": "click", "params": {},
                    "origin": "model",
                    "target": {"tag": "button", "label": "播放"}},
         "observation": _obs("https://x.test/play2", video_tag_count=1),
         "error": ""},
    ]
    base = {
        "task_id": "T001", "title": "功夫", "query": "功夫 在线观看",
        "user_prompt": "有没有能看正片的 给个链接呗",
        "search_url": "https://s.test/?q=1", "perceptor": "rule",
        "candidate_source": "heuristic", "search_blocked": "",
        "candidate_filter": {}, "provenance": {"genre": "喜剧"},
        "elapsed_ms": 1, "warnings": [],
        "search_observation": _obs("https://s.test/?q=1"),
        "steps": steps,
        "candidates": [{"url": "https://x.test/movie", "text": "x",
                        "rank": 1, "host": "x.test"}],
        "visits": [
            {"url": "http://a.test/movie", "landed_url": "https://a.test/movie",
             "success": False, "notes": [], "steps": asteps,
             "site_obs": _obs("https://a.test/movie"),
             "player_obs": _obs("https://a.test/movie")},
            {"url": "http://x.test/movie", "landed_url": "https://x.test/movie",
             "success": True, "notes": [], "steps": vsteps,
             "site_obs": _obs("https://x.test/movie"),
             "player_obs": _obs("https://x.test/play", video_tag_count=1)},
        ],
        "ledger": {"task_id": "T001", "total_sites": 2, "succeeded": 1,
                   "negative_samples": 1, "by_branch": {},
                   "missing_branches": [], "heuristic_candidates": 0},
        "outcomes": [
            {"url": "https://a.test/movie", "branch": "no_play_control",
             "branch_label": "", "evidence": "e", "question": "player.ok",
             "source": "rule", "fallback_used": True,
             "reached_play_page": False, "is_negative_sample": True},
            {"url": "https://x.test/movie", "branch": "no_play_control",
             "branch_label": "", "evidence": "e2", "question": "player.ok",
             "source": "rule", "fallback_used": True,
             "reached_play_page": False, "is_negative_sample": True},
        ],
    }
    base.update(over)
    return base


@pytest.fixture()
def sample(tmp_path) -> schema.Sample:
    p = tmp_path / "T001__abc123.json"
    p.write_text(json.dumps(_archive(), ensure_ascii=False), encoding="utf-8")
    return schema.split_archive(p)[0]


@pytest.fixture()
def payload(sample) -> dict:
    """已渲染的 P2 样本（**主池口径**）。"""
    return builder._sample_to_json(sample)


# ── 缺件方式 ────────────────────────────────────────────────────────


class Test缺件方式:
    def test_三种方式都在(self):
        assert failclosed.CUT_MODES == (
            "body_truncated", "elements_missing", "body_missing",
        )

    def test_每种方式都有标签(self):
        for m in failclosed.CUT_MODES:
            assert failclosed.CUT_MODE_LABELS.get(m), f"{m} 没有标签"

    def test_认不出的方式抛(self):
        with pytest.raises(ValueError):
            failclosed.degrade_observation({"body_text": "x"}, mode="nope")

    def test_不改原观察(self, payload):
        obs = payload["six"]["observations"][-1]
        before = json.dumps(obs, ensure_ascii=False, sort_keys=True)
        failclosed.degrade_observation(obs, mode="body_missing")
        assert json.dumps(obs, ensure_ascii=False, sort_keys=True) == before


class Test标记必须是真的:
    """缺件之后**回执里必须说得出缺了什么**。"""

    def test_正文截断说上游截断(self, payload):
        obs = payload["six"]["observations"][-1]
        out = failclosed.degrade_observation(obs, mode="body_truncated")
        assert out["truncated"]["source"] is True
        text = views.render_observation(out)
        assert "[内容不完整:" in text
        assert "上游截断" in text

    def test_元素缺失说采集降级(self, payload):
        obs = payload["six"]["observations"][-1]
        out = failclosed.degrade_observation(obs, mode="elements_missing")
        assert "interactive_elements" in out["degraded"]
        text = views.render_observation(out)
        assert "[采集降级: interactive_elements]" in text

    def test_正文缺失说采集降级(self, payload):
        obs = payload["six"]["observations"][-1]
        out = failclosed.degrade_observation(obs, mode="body_missing")
        assert "body_text" in out["degraded"]
        text = views.render_observation(out)
        assert "[采集降级: body_text]" in text

    def test_三种方式都留痕(self, payload):
        """没有一种方式是「静默缺件」——静默缺件教出来的是幻觉。"""
        obs = payload["six"]["observations"][-1]
        for mode in failclosed.CUT_MODES:
            out = failclosed.degrade_observation(obs, mode=mode)
            text = views.render_observation(out)
            assert ("[内容不完整:" in text) or ("[采集降级:" in text), (
                f"{mode} 造出来的观察在回执里没留下任何缺件信号")


class Test只可能漏不可能造:
    def test_正文是原文的前缀(self, payload):
        """⚠️ **砍前缀**（不是砍后缀）：砍后缀会删掉**动作目标的行**
        （keep 命中的行在尾部）⇒ 观察里出现「点了但观察里没有的东西」
        ⇒ 教模型编目标。⇒ 正文必须是原文的**前缀**。"""
        obs = payload["six"]["observations"][-1]
        original = str(obs.get("body_text") or "")
        out = failclosed.degrade_observation(obs, mode="body_truncated")
        got = str(out.get("body_text") or "")
        assert got
        assert original.startswith(got), "正文不是原文的前缀"

    def test_正文截断真的少了东西(self, payload):
        """⚠️ **标记与观察不能自相矛盾**：说「上游截断」而正文其实完整
        ⇒ 模型学到「标记可以乱写」⇒ 前面两条真实的缺件信号也一起废掉。"""
        obs = payload["six"]["observations"][-1]
        original = str(obs.get("body_text") or "")
        out = failclosed.degrade_observation(obs, mode="body_truncated")
        assert len(str(out.get("body_text") or "")) < len(original)

    def test_元素表空了就是空了(self, payload):
        obs = payload["six"]["observations"][-1]
        obs = dict(obs)
        obs["interactive_elements"] = [{"tag": "button", "label": "立即播放"}]
        out = failclosed.degrade_observation(obs, mode="elements_missing")
        assert out["interactive_elements"] == []

    def test_正文没了就是没了(self, payload):
        obs = payload["six"]["observations"][-1]
        out = failclosed.degrade_observation(obs, mode="body_missing")
        assert out["body_text"] == ""
        assert out["body_chars"] == 0


class Test标记与观察自洽:
    """⚠️ **标记不能与观察自相矛盾**——这是这条线自己的纪律。"""

    def _cut_obs(self, payload) -> dict:
        """**被切的那一条**观察（``pick_cut_index`` 挑的那一步）。"""
        observations = payload["six"]["observations"]
        return observations[failclosed.pick_cut_index(observations)]

    def test_truncated永远是两键(self, payload):
        """P2 契约：``truncated`` 是 ``{source, view}`` **2 键**——
        少一个键就落进下游「不认识的老形状」分支。"""
        obs = self._cut_obs(payload)
        for mode in failclosed.CUT_MODES:
            out = failclosed.degrade_observation(obs, mode=mode)
            assert set(out["truncated"]) == {"source", "view"}

    def test_动了就说动了(self, payload):
        """``cropped`` 是「本层动没动过」⇒ 砍了就得为真。

        ⚠️ 用**被切的那一条**（正文与元素都有）⇒ 三种方式都真的少东西。
        """
        obs = self._cut_obs(payload)
        for mode in failclosed.CUT_MODES:
            out = failclosed.degrade_observation(obs, mode=mode)
            assert out["limits"]["cropped"] is True, (
                f"{mode} 砍了东西但 limits.cropped 是 False")

    def test_limits还是七键(self, payload):
        """``limits`` 的 7 键形状是契约——不许多不许少。"""
        obs = self._cut_obs(payload)
        expected = {
            "body_chars", "elements", "links",
            "body_lines_dropped", "elements_dropped", "links_dropped",
            "cropped",
        }
        for mode in failclosed.CUT_MODES:
            out = failclosed.degrade_observation(obs, mode=mode)
            assert set(out["limits"]) == expected

    def test_精炼留痕不与正文自相矛盾(self, payload):
        """⚠️ 回执里「正文：（空）」与「[内容已精简: 保留 6 行]」**并排**
        ⇒ 自相矛盾 ⇒ 模型学到「标记可以乱写」。

        ⇒ 正文被动过 ⇒ 精炼留痕**跟着重算**：正文还在就按实际留下的
        行数算；正文没了就与真实的 ``observation_missing`` 占位同款
        （``body_source`` 记 ``missing``、留痕清空）。
        """
        obs = self._cut_obs(payload)
        out = failclosed.degrade_observation(obs, mode="body_truncated")
        if out.get("body_refined"):
            real = len([
                ln for ln in str(out["body_text"]).splitlines() if ln.strip()
            ])
            assert out["body_refined_lines"] == real

        out = failclosed.degrade_observation(obs, mode="body_missing")
        assert out["body_source"] == "missing"
        assert out["body_refined"] is False
        assert out["body_refined_lines"] == 0


class Test轨迹截断:
    def test_取最后一条有内容的观察(self):
        obs = [
            {"body_text": "有内容", "interactive_elements": []},
            {"body_text": "", "interactive_elements": []},
        ]
        assert failclosed.pick_cut_index(obs) == 0

    def test_没有可缺件的就返回负一(self):
        assert failclosed.pick_cut_index([{"body_text": "", "interactive_elements": []}]) == -1

    def test_优先取正文与元素都有的那一步(self):
        """⚠️ 三种缺件方式各要一样东西：正文两种、元素一种 ⇒ 挑一条
        **两样都有**的 ⇒ 三种方式**各自都真的少东西**。

        标记说「上游截断」而正文其实完整 ⇒ 模型学到「标记可以乱写」
        ⇒ 前面两条真实的缺件信号也一起废掉。
        """
        obs = [
            {"body_text": "有正文也有元素", "interactive_elements": [{"tag": "a"}]},
            {"body_text": "只有正文", "interactive_elements": []},
        ]
        assert failclosed.pick_cut_index(obs) == 0

    def test_没有两样都有的就取有正文的(self):
        obs = [
            {"body_text": "只有正文", "interactive_elements": []},
            {"body_text": "", "interactive_elements": [{"tag": "b"}]},
            {"body_text": "", "interactive_elements": []},
        ]
        assert failclosed.pick_cut_index(obs) == 0

    def test_只有元素的就取有元素的(self):
        obs = [
            {"body_text": "", "interactive_elements": [{"tag": "b"}]},
            {"body_text": "", "interactive_elements": []},
        ]
        assert failclosed.pick_cut_index(obs) == 0

    def test_轨迹在缺件那步截断(self, payload):
        observations = payload["six"]["observations"]
        cut = failclosed.pick_cut_index(observations)
        line = failclosed.build_line_payload(
            payload, cut_mode="body_missing", cut_index=cut,
        )
        assert len(line["six"]["observations"]) == cut + 1
        assert len(line["six"]["actions"]) <= cut + 1
        for r in line["six"]["rationales"]:
            assert int(r.get("step_index") or 0) <= cut


class Test结论改判:
    def test_任务级结论改成判不了(self, payload):
        cut = failclosed.pick_cut_index(payload["six"]["observations"])
        line = failclosed.build_line_payload(
            payload, cut_mode="body_missing", cut_index=cut,
        )
        assert line["outcome"]["branch"] == failclosed.FAIL_CLOSED_BRANCH
        assert line["outcome"]["succeeded"] is False

    def test_期望答案是派生出来的不是手写的(self):
        """``unresolved`` 必须在收尾词表里——否则收尾落到兜底原因，
        这条线教出来的 fail-closed 说法就与主池里的**不一样**。"""
        assert failclosed.FAIL_CLOSED_BRANCH in closing.CLOSING_DISPOSITION

    def test_收尾话术说判不了(self, payload):
        cut = failclosed.pick_cut_index(payload["six"]["observations"])
        line = failclosed.build_line_payload(
            payload, cut_mode="body_missing", cut_index=cut,
        )
        text = closing.closing_for_run(
            title=str(line.get("title") or ""),
            outcomes=closing.run_closing_outcomes(
                decisive=line.get("outcome"),
                site_outcomes=[s.get("outcome") for s in (line.get("site_outcomes") or ())],
            ),
        )
        assert "无法确认能否播放，不予记录" in text

    def test_别的站点保留真实结论(self, payload):
        """⚠️ 只有**缺件那一步**对应的站点改成判不了。

        别的站点确实被访问过、确实被排除过 ⇒ 一并改成「判不了」就是
        **改写真实事实** ⇒ 训练语料里出现假事实。

        断言写成**逐行对比**（原样本 vs 缺件变体的 ``branch``）而不是
        「不是 unresolved 就算过」——后者在「原样本本来就是 unresolved」
        时会**假绿**。
        """
        cut = failclosed.pick_cut_index(payload["six"]["observations"])
        line = failclosed.build_line_payload(
            payload, cut_mode="body_missing", cut_index=cut,
        )
        before_rows = list(payload.get("site_outcomes") or ())
        after_rows = list(line.get("site_outcomes") or ())
        assert len(after_rows) == len(before_rows)
        changed = 0
        for before, after in zip(before_rows, after_rows):
            b = (before.get("outcome") or {}).get("branch")
            a = (after.get("outcome") or {}).get("branch")
            start = int(after.get("step_start") or 0)
            end = int(after.get("step_end") or 0)
            if start <= cut < end:
                assert a == failclosed.FAIL_CLOSED_BRANCH
                changed += 1
            else:
                assert a == b, (
                    f"不在缺件那一步的站点结论被改写了：{b} → {a}"
                    "（改写真实事实）")
        # ⚠️ 夹具里**必须有**一个「不在缺件那一步」的站点，否则这条
        # 测试压根没验证到「只改一个」——那正是它要防的回归。
        assert changed == 1, "改判的站点数不对（应为缺件那一步那一个）"
        assert len(before_rows) >= 2, "夹具至少要有两个站点"


class Test留痕:
    def test_degraded_from带标记(self, payload):
        cut = failclosed.pick_cut_index(payload["six"]["observations"])
        line = failclosed.build_line_payload(
            payload, cut_mode="body_missing", cut_index=cut,
        )
        assert failclosed.FAIL_CLOSED_MARKER in line["degraded_from"]

    def test_记着缺在哪缺什么(self, payload):
        cut = failclosed.pick_cut_index(payload["six"]["observations"])
        line = failclosed.build_line_payload(
            payload, cut_mode="elements_missing", cut_index=cut,
        )
        assert line["failclosed_cut_mode"] == "elements_missing"
        assert line["failclosed_cut_step"] == cut

    def test_样本id与原样本不同(self, payload):
        cut = failclosed.pick_cut_index(payload["six"]["observations"])
        line = failclosed.build_line_payload(
            payload, cut_mode="body_missing", cut_index=cut,
        )
        assert line["sample_id"] != payload["sample_id"]
        assert line["sample_id"].startswith(payload["sample_id"])


class Test不进主池:
    def test_目录与主池分开(self, tmp_path):
        assert builder.failclosed_dir(tmp_path) != builder.samples_dir(tmp_path)
        assert builder.failclosed_dir(tmp_path).name == "failclosed_samples"

    def test_不会被当成P1存档(self, tmp_path):
        """⚠️ ``select_archives`` 扫的是 ``<根>/*.json``（不递归）⇒
        缺件线在**子目录**里 ⇒ 扫不到。"""
        from trajectory_pipeline.executor.archive import select_archives

        root = tmp_path / "pipeline"
        root.mkdir()
        (root / "T001__abc.json").write_text("{}", encoding="utf-8")
        fc = builder.failclosed_dir(root)
        fc.mkdir(parents=True)
        (fc / "T001__abc__fc-body_missing.json").write_text("{}", encoding="utf-8")
        names = [p.name for p, _ in select_archives(root)]
        assert "T001__abc.json" in names
        assert not any("fc-" in n for n in names)


class Test端到端:
    def test_造出来并落盘(self, tmp_path):
        p = tmp_path / "T001__abc123.json"
        p.write_text(json.dumps(_archive(), ensure_ascii=False), encoding="utf-8")
        res = builder.build_failclosed_from_archive(p, root=tmp_path, overwrite=True)
        assert res.written == len(failclosed.CUT_MODES)
        out_dir = builder.failclosed_dir(tmp_path)
        assert len(list(out_dir.glob("*.json"))) == len(failclosed.CUT_MODES)

    def test_缺件样本能被P3渲染(self, tmp_path):
        """它必须**能进训练流程**——造出来却渲不了，等于没造。"""
        p = tmp_path / "T001__abc123.json"
        p.write_text(json.dumps(_archive(), ensure_ascii=False), encoding="utf-8")
        builder.build_failclosed_from_archive(p, root=tmp_path, overwrite=True)
        line = json.loads(
            (builder.failclosed_dir(tmp_path) / "T001__abc123__fc-body_missing.json")
            .read_text(encoding="utf-8"),
        )
        messages = views.to_messages(line)
        assert messages
        assert any("内容不完整" in str(m.get("content") or "")
                   or "采集降级" in str(m.get("content") or "")
                   for m in messages)
