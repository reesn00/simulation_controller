"""P2 切分的单元测试——**一个决策单元一条**。

重点不是「能切出几条」，而是四件切错了就静默的事：

1. **粒度**：搜索与每个站点各自成条。压成长轨迹会把「选哪个站」与
   「这个站行不行」塞进同一次预测里。
2. **outcome 配对**：存档里 ``visits[i].url`` 是候选链接，
   ``outcomes[j].url`` 是落地地址，跳转过的站两者不等——配不上就是
   每条负样本都显示成「无结论」，报表上负样本一条不少、分子全空。
3. **降级可见**：老存档缺 ``steps`` / ``user_prompt`` / 全文正文，
   这些样本必须**自带标记**，不能和新存档的样本混着用。
4. **被反爬拦的 run 不切搜索单元**：那一步的观察是验证码页，
   切成「模型该怎么做」的样本只会教它对着验证码页编答案。
"""

from __future__ import annotations

import json

import pytest

from trajectory_pipeline.assembler import schema
from trajectory_pipeline.assembler.schema import P1ShapeError


def _obs(url="u", **kw):
    base = {
        "url": url,
        "page_title": "T",
        "body_text": "正文",
        "truncated": False,
        "degraded": [],
        "video_tag_count": 0,
        "iframe_count": 0,
        "interactive_elements": [],
        "links": [],
    }
    base.update(kw)
    return base


def _visit(url, landed=None, success=False, **over):
    return {
        "url": url,
        "landed_url": landed or url,
        "success": success,
        "notes": [],
        "steps": [],
        "site_obs": _obs(landed or url),
        "player_obs": None,
        **over,
    }


def _outcome(url, branch):
    return {
        "url": url,
        "branch": branch,
        "branch_label": "",
        "evidence": "e",
        "question": "player.ok",
        "source": "rule",
        "fallback_used": False,
        "reached_play_page": True,
        "is_negative_sample": False,
    }


def _arc(**over):
    base = {
        "task_id": "T001",
        "title": "功夫",
        "query": "功夫 在线观看",
        "user_prompt": "有没有能看正片的",
        "search_url": "https://s.test",
        "search_observation": _obs("https://s.test"),
        "search_blocked": "",
        "steps": [],
        "provenance": {"genre": "喜剧"},
        # 健康存档**有**运行参数快照：批次级断言 B-2（覆盖完整性）的分母
        # 取自 run_config.max_candidates。缺它会被 ``run_config_missing``
        # 判 DEGRADED，测试里要模拟降级就显式 pop 掉。
        "run_config": {
            "engine": "baidu",
            "max_candidates": 20,
            "max_chars": None,
            "stop_after_success": 0,
            "per_site_timeout_s": 90.0,
        },
        "candidates": [],
        "visits": [],
        "outcomes": [],
        "ledger": None,
        "warnings": [],
    }
    base.update(over)
    return base


def _write(tmp_path, arc, name="T001__abc123.json"):
    p = tmp_path / name
    p.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
    return p


class TestSchemaVersion:
    """P1 版本身份在 P2 侧的三条口径。

    这个字段解决的是「这批数据是哪一批」，而**回填历史存档就是猜**：
    没有依据断定一份没版本号的存档属于哪一批，猜错的代价是一个
    **静默错误**的批次归属（报表上只多出一条看不懂的数据）。
    """

    def test_有版本号原样带出(self, tmp_path):
        arc = _arc(schema_version="2", visits=[_visit("https://a.test/")])
        for s in schema.split_archive(_write(tmp_path, arc)):
            assert s.schema_version == "2"

    def test_缺版本号是空串而不是默认版本(self, tmp_path):
        """**空串 ≠「v1」**。前者说「这份数据没有版本身份」，
        后者说「这份数据是旧的」——后者是猜的。补一个默认值等于
        凭空断言，于是所有历史存档会被静默归进「旧版」那一档。"""
        arc = _arc(visits=[_visit("https://a.test/")])
        assert "schema_version" not in arc  # 模拟 2026-10-10 之前的存档
        for s in schema.split_archive(_write(tmp_path, arc)):
            assert s.schema_version == ""

    def test_进P2(self, tmp_path):
        arc = _arc(schema_version="2", visits=[_visit("https://a.test/")])
        assert (
            schema.split_archive(_write(tmp_path, arc))[0].to_json()["schema_version"]
            == "2"
        )


class TestGranularity:
    def test_搜索与每个站点各自成条(self, tmp_path):
        arc = _arc(visits=[_visit("https://a.test/"), _visit("https://b.test/")])
        units = [s.unit for s in schema.split_archive(_write(tmp_path, arc))]
        assert units == ["search", "site-1", "site-2"]

    def test_搜索单元在前(self, tmp_path):
        """顺序即时间序。乱序会让按 id 排序的批次读起来像时序错乱。"""
        arc = _arc(visits=[_visit("https://a.test/")])
        assert schema.split_archive(_write(tmp_path, arc))[0].unit == "search"

    def test_站点单元带落地前后两个观察(self, tmp_path):
        """观察**逐步对位**取自 ``steps[].observation``（2026-10-15 改，
        原取 ``(site_obs, player_obs)`` 去重快照对）——那对快照与动作流
        不等长，``new_tab`` 步的 null 观察会被真值过滤丢掉，整段错位。"""
        arc = _arc(
            visits=[
                _visit(
                    "https://a.test/",
                    "https://a.test/play",
                    steps=[
                        _step("goto", url="https://a.test"),
                        _step("click", url="https://a.test/play"),
                    ],
                    site_obs=_obs("https://a.test"),
                    player_obs=_obs("https://a.test/play", video_tag_count=1),
                )
            ]
        )
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert [o["url"] for o in site.observations] == [
            "https://a.test",
            "https://a.test/play",
        ]

    def test_null观察顶占位保持对位(self, tmp_path):
        """E005 错位的根因：``new_tab`` 步 observation=null 被真值过滤
        **静默丢掉**，3 个动作配 2 份观察，rationale 与观察整段错位。
        现在顶 :data:`MISSING_OBSERVATION` 占位——**真值必须为真**，
        否则下游任何 ``if o`` 过滤会把它再次丢掉。"""
        steps = [
            _step("goto", url="https://a.test"),
            {
                "action": {
                    "tool": "new_tab",
                    "params": {"url": "https://a.test/play"},
                    "origin": "infrastructure",
                },
                "observation": None,
                "error": "",
                "rationale": None,
            },
            _step("click", url="https://a.test/play"),
        ]
        arc = _arc(
            visits=[dict(_visit("https://a.test/"), steps=steps)],
            outcomes=[_outcome("https://a.test/", "no_play_control")],
        )
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert len(site.actions) == 3
        assert len(site.observations) == 3, "缺位观察被丢掉就会再次错位"
        assert site.observations[1]["degraded"] == ["observation_missing"]
        assert site.observations[1], "占位必须真值为真（防下游 if o 再丢）"
        assert site.observations[2]["url"] == "https://a.test/play"

    def test_没有观察的单元不产出(self, tmp_path):
        """两者皆空 → 切不出东西。抛出来而不是给一条空样本。"""
        p = _write(tmp_path, _arc(visits=[], search_observation=None))
        with pytest.raises(P1ShapeError, match="切不出样本"):
            schema.split_archive(p)

    def test_搜索阶段无outcome(self, tmp_path):
        """搜索阶段不记站点级 outcome。给它硬配一条是编造。"""
        s = schema.split_archive(_write(tmp_path, _arc()))[0]
        assert s.outcome is None


class TestOutcomePairing:
    def test_跳转过的站按落地地址配(self, tmp_path):
        """实测存档：4 个 run 各有 3 个访问点是 http→https 跳转过的，
        候选 url 与 outcome url 对不上。只按候选 url 配的后果是静默的——
        负样本一条不少，分子全空。"""
        arc = _arc(
            visits=[_visit("http://x.test/movie", "https://x.test/movie")],
            outcomes=[_outcome("https://x.test/movie", "no_play_control")],
        )
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert site.outcome is not None
        assert site.outcome.branch == "no_play_control"

    def test_候选地址也能配(self, tmp_path):
        arc = _arc(
            visits=[_visit("https://x.test/movie")],
            outcomes=[_outcome("https://x.test/movie", "no_play_control")],
        )
        assert (
            schema.split_archive(_write(tmp_path, arc))[1].outcome.branch
            == "no_play_control"
        )

    def test_配不上要留痕(self, tmp_path):
        """静默 None 与「搜索阶段本来就没有 outcome」同形。"""
        arc = _arc(visits=[_visit("https://x.test/movie")], outcomes=[])
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert site.outcome is None
        assert "outcome_missing" in site.degraded_from

    def test_成功即无分支(self, tmp_path):
        """``ledger`` 只记失败分支，成功的访问不记账——
        所以 ``branch=None`` 是**成功**，不是「没结论」。"""
        arc = _arc(
            visits=[_visit("https://x.test/", success=True)],
            outcomes=[_outcome("https://x.test/", None)],
        )
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert site.outcome.succeeded is True

    def test_成功与分支对不上要留痕(self, tmp_path):
        """「成功」直接决定样本进不进训练集，而它必须只有一个来源说了算。
        两个来源打架时标记出来，而不是挑一个——挑一个就是在猜。"""
        arc = _arc(
            visits=[_visit("https://x.test/", success=True)],
            outcomes=[_outcome("https://x.test/", "no_play_control")],
        )
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert "success_branch_mismatch" in site.degraded_from

    def test_不从存档的is_negative_sample取判据(self, tmp_path):
        """那份判据由 executor 的 ``NON_SAMPLE_BRANCHES`` 定义，assembler
        不 import executor，两份口径就可能不同步。存档里把
        ``is_negative_sample`` 写成 True 也不采信。"""
        o = _outcome("https://x.test/", None)
        o["is_negative_sample"] = True
        arc = _arc(visits=[_visit("https://x.test/", success=True)], outcomes=[o])
        site = schema.split_archive(_write(tmp_path, arc))[1]
        assert site.outcome.branch is None


class TestBlockedRun:
    def test_被反爬拦时不切搜索单元(self, tmp_path):
        """拦截发生在取候选之前，那一步的观察是验证码页。切成样本只会
        教模型对着验证码页编答案。"""
        arc = _arc(search_blocked="captcha", visits=[])
        units = [s.unit for s in schema.split_archive(_write(tmp_path, arc))]
        assert units == []

    def test_被拦但还有访问记录时站点仍成条(self, tmp_path):
        """拦截的是搜索页，已访问的站点不受影响。"""
        arc = _arc(
            search_blocked="captcha",
            visits=[_visit("https://a.test/")],
            outcomes=[_outcome("https://a.test/", "unresolved")],
        )
        assert [s.unit for s in schema.split_archive(_write(tmp_path, arc))] == [
            "site-1"
        ]

    def test_搜索页采集失败时不切搜索单元(self, tmp_path):
        """T048 这类坏档：``search_blocked`` 为空，但 warnings 记了
        「搜索页采集失败」（网络错误），同样没有进入决策——零条是
        正常形态，不能抛。"""
        arc = _arc(
            search_observation=None,
            visits=[],
            warnings=["搜索页采集失败: DriverError: Network error"],
        )
        assert schema.split_archive(_write(tmp_path, arc)) == []

    def test_采集失败但还有访问记录时站点仍成条(self, tmp_path):
        """失败的只是搜索页，已访问过的站点不受影响。"""
        arc = _arc(
            search_observation=None,
            visits=[_visit("https://a.test/")],
            outcomes=[_outcome("https://a.test/", "unresolved")],
            warnings=["搜索页采集失败: DriverError: Network error"],
        )
        assert [s.unit for s in schema.split_archive(_write(tmp_path, arc))] == [
            "site-1"
        ]


class TestDegradedMarkers:
    def test_老存档缺steps要留痕(self, tmp_path):
        arc = _arc(visits=[_visit("https://a.test/")])
        for s in schema.split_archive(_write(tmp_path, arc)):
            assert "steps" in s.degraded_from

    def test_有steps时不留痕(self, tmp_path):
        arc = _arc(
            steps=[
                {
                    "action": {
                        "tool": "goto",
                        "params": {"url": "u"},
                        "origin": "model",
                        "target": None,
                    },
                    "observation": None,
                    "error": "",
                }
            ],
            visits=[
                dict(
                    _visit("https://a.test/"),
                    steps=[
                        {
                            "action": {
                                "tool": "goto",
                                "params": {"url": "u"},
                                "origin": "model",
                                "target": None,
                            },
                            "observation": None,
                            "error": "",
                        }
                    ],
                )
            ],
        )
        for s in schema.split_archive(_write(tmp_path, arc)):
            assert "steps" not in s.degraded_from

    def test_缺用户轮次要留痕(self, tmp_path):
        arc = _arc()
        arc.pop("user_prompt")
        assert (
            "user_prompt"
            in schema.split_archive(_write(tmp_path, arc))[0].degraded_from
        )

    def test_只有预览正文标preview_only(self, tmp_path):
        """400 字符冒充全文 → rationale 的实体核查会把落在预览外的实体
        判成幻觉。那不是幻觉，是**没存**。"""
        arc = _arc(search_observation={"url": "u", "body_preview": "前四百字"})
        assert (
            "body_preview_only"
            in schema.split_archive(_write(tmp_path, arc))[0].degraded_from
        )

    def test_完全没正文标no_body(self, tmp_path):
        arc = _arc(search_observation={"url": "u", "body_text": ""})
        assert "no_body" in schema.split_archive(_write(tmp_path, arc))[0].degraded_from

    def test_缺运行参数要留痕(self, tmp_path):
        """缺 ``run_config`` 时批次级断言 B-2 的分母不可知。
        样本本身仍然可用，所以**不是**丢弃——但降级标记必须跟着它走，
        否则评分器会把「不知道分母」当成「分母是 0」。"""
        arc = _arc()
        arc.pop("run_config")
        for s in schema.split_archive(_write(tmp_path, arc)):
            assert "run_config" in s.degraded_from

    def test_标记去重且有序(self, tmp_path):
        arc = _arc(visits=[_visit("https://a.test/")])
        s = schema.split_archive(_write(tmp_path, arc))[1]
        assert list(s.degraded_from) == list(dict.fromkeys(s.degraded_from))

    def test_新存档零降级(self, tmp_path):
        """一条干净的样本必须能一眼认出来。标记若总在，整条标记就失效了。"""
        arc = _arc(
            steps=[
                {
                    "action": {
                        "tool": "goto",
                        "params": {"url": "u"},
                        "origin": "model",
                        "target": None,
                    },
                    "observation": None,
                    "error": "",
                }
            ],
            visits=[
                dict(
                    _visit("https://a.test/"),
                    steps=[
                        {
                            "action": {
                                "tool": "goto",
                                "params": {"url": "u"},
                                "origin": "model",
                                "target": None,
                            },
                            "observation": None,
                            "error": "",
                        }
                    ],
                )
            ],
        )
        assert schema.split_archive(_write(tmp_path, arc))[0].degraded_from == ()


class TestSampleId:
    def test_格式可反查源存档(self, tmp_path):
        sid = schema.split_archive(_write(tmp_path, _arc()))[0].sample_id
        assert sid == "T001__abc123__search"
        task, run, unit = sid.split("__")
        assert (task, unit) == ("T001", "search")
        assert run

    def test_同批次内按unit单调(self, tmp_path):
        arc = _arc(visits=[_visit(f"https://a{i}.test/") for i in range(12)])
        units = [
            s.sample_id.split("__")[2]
            for s in schema.split_archive(_write(tmp_path, arc))
        ]
        assert units == ["search"] + [f"site-{i}" for i in range(1, 13)]

    def test_取run_id(self):
        assert schema.run_id_of("T001__004b6be9.json") == "004b6be9"

    def test_探针取证取run_id不炸(self):
        """``obscura_tools.json`` 没有 ``__``。它走不到这里，但要炸得
        明白而不是抛 IndexError。"""
        assert schema.run_id_of("obscura_tools.json") == "obscura_tools"


class TestIterSamples:
    def test_跳过探针取证(self, tmp_path):
        """取证文件顶层没有 ``task_id``，当存档读会直接打断整批。"""
        _write(tmp_path, _arc(), "T001__a1.json")
        (tmp_path / "obscura_tools.json").write_text('{"tools": []}', encoding="utf-8")
        assert len(list(schema.iter_samples(tmp_path))) == 1

    def test_跳过复核档(self, tmp_path):
        """复核档由 executor 侧统一挑选（``select_archives``）。
        这里再选一次就会同一条跑两遍，而两遍的复核结论可能不同。"""
        _write(tmp_path, _arc(), "T001__a1.json")
        _write(tmp_path, _arc(), "T001__a1.reviewed.json")
        assert [s.sample_id for s in schema.iter_samples(tmp_path)] == [
            "T001__a1__search"
        ]

    def test_按文件名排序(self, tmp_path):
        _write(tmp_path, _arc(task_id="T002"), "T002__b1.json")
        _write(tmp_path, _arc(task_id="T001"), "T001__a1.json")
        assert [s.task_id for s in schema.iter_samples(tmp_path)] == ["T001", "T002"]

    def test_被拦的run不打断整批(self, tmp_path):
        """一批里混进几个被反爬拦的 run 是常态（executor 的警告里就写着
        「直接重跑同一引擎大概率仍是验证码页」）。抛出去等于整批陪葬。"""
        _write(
            tmp_path,
            _arc(
                task_id="T001",
                search_blocked="captcha",
                search_observation=None,
                visits=[],
            ),
            "T001__a1.json",
        )
        _write(tmp_path, _arc(task_id="T002"), "T002__a1.json")
        assert [s.task_id for s in schema.iter_samples(tmp_path)] == ["T002"]

    def test_采集失败的run不打断整批(self, tmp_path):
        """一批里混进一个搜索页采集失败的 run（T048 坏档形态）不能
        打断整批，后续正常 run 仍要产出。"""
        _write(
            tmp_path,
            _arc(
                task_id="T001",
                search_observation=None,
                visits=[],
                warnings=["搜索页采集失败: DriverError: Network error"],
            ),
            "T001__a1.json",
        )
        _write(tmp_path, _arc(task_id="T002"), "T002__a1.json")
        assert [s.task_id for s in schema.iter_samples(tmp_path)] == ["T002"]

    def test_坏档抛出且带文件名(self, tmp_path):
        """坏档静默变成「零样本」的话，批次看起来只是少了几条，
        没人会去查。异常里必须点名是哪个文件。"""
        (tmp_path / "T009__bad.json").write_text('{"visits": []}', encoding="utf-8")
        with pytest.raises(P1ShapeError, match="T009__bad.json"):
            list(schema.iter_samples(tmp_path))


class TestReadArchive:
    def test_不是JSON(self, tmp_path):
        p = tmp_path / "T001__a1.json"
        p.write_text("{ 不是", encoding="utf-8")
        with pytest.raises(P1ShapeError, match="不是合法 JSON"):
            schema.read_archive(p)

    def test_顶层不是对象(self, tmp_path):
        p = tmp_path / "T001__a1.json"
        p.write_text("[1,2]", encoding="utf-8")
        with pytest.raises(P1ShapeError, match="顶层不是 JSON 对象"):
            schema.read_archive(p)

    def test_缺task_id(self, tmp_path):
        p = tmp_path / "T001__a1.json"
        p.write_text('{"visits": []}', encoding="utf-8")
        with pytest.raises(P1ShapeError, match="缺 task_id"):
            schema.read_archive(p)


class TestToolSpec:
    def test_可见动作排除执行器内部动作(self):
        assert {t.name for t in schema.TOOL_SPECS if t.visible_to_model()} == {
            "goto",
            "click",
        }

    def test_参数名可序列化(self):
        assert schema.TOOL_SPECS[0].to_json()["params"] == ["url"]


# ═══════════════════════════════════════════════════════════════════════
# 模块 4 接线：P1 的 steps[i].rationale → 第 ⑥ 件 → 闸门
# ═══════════════════════════════════════════════════════════════════════


def _step(tool, rationale=None, url="u"):
    """一条 P1 的 step，形状对齐 executor.actions.ActionStep.to_json。"""
    return {
        "action": {"tool": tool, "params": {"url": url}, "origin": "model"},
        "observation": _obs(url),
        "error": "",
        "rationale": rationale,
    }


def _written(text, conclusion=""):
    return {"status": "written", "rationale": {"text": text, "conclusion": conclusion}}


class TestRationale对齐:
    """第 ⑥ 件**与动作逐步对齐**（2026-10-10 改，原为整条一条）。"""

    def _site_sample(self, tmp_path, vsteps):
        arc = _arc(
            visits=[dict(_visit("https://a.test/"), steps=vsteps)],
            outcomes=[_outcome("https://a.test/", "no_play_control")],
        )
        samples = schema.split_archive(_write(tmp_path, arc))
        # [0] 是搜索单元、[1] 是 site-1——**取错单元是这里最容易踩的坑**，
        # 而症状是「rationale 一条都没有」（搜索单元的 steps 本来就是空的）。
        return samples[1]

    def test_逐条对齐不压成一条(self, tmp_path):
        s = self._site_sample(
            tmp_path,
            [
                _step("goto", _written("先打开候选站"), "https://a.test/"),
                _step("click", _written("页面里有播放按钮，点它")),
            ],
        )
        assert [r.step_index for r in s.rationales] == [0, 1]
        assert s.rationales[1].text == "页面里有播放按钮，点它"
        assert s.rationale_missing is False

    def test_缺一条不挪位(self, tmp_path):
        """中间缺一条时**不得按出现顺序猜**——猜出来会整体错位，
        而错位的 rationale 会被时序核查当成凭空引入的实体。"""
        s = self._site_sample(
            tmp_path,
            [
                _step("goto", _written("第 0 步")),
                _step("click", None),
                _step("type", _written("第 2 步")),
            ],
        )
        got = {r.step_index: r.text for r in s.rationales}
        assert got == {0: "第 0 步", 2: "第 2 步"}

    def test_未写成的步骤三态透传(self, tmp_path):
        s = self._site_sample(
            tmp_path,
            [
                _step(
                    "goto", {"status": "unavailable", "reason": "LLM 不可用（连不上）"}
                ),
            ],
        )
        r = s.rationales[0]
        assert r.status == "unavailable"
        assert r.text == ""  # 文本位留空，不合成占位
        assert "连不上" in r.reason
        assert s.rationale_missing is True

    def test_空元组即缺失(self, tmp_path):
        s = self._site_sample(tmp_path, [_step("goto", None)])
        assert s.rationales == ()
        assert s.rationale_missing is True
        assert s.to_json()["six"]["rationales"] == []


class Test闸门真跑:
    """闸门在**组装期**跑（2026-10-10 接线），不再是恒 ``not_run``。"""

    def _sample(self, tmp_path, rationale, obs_text="正文"):
        """⚠️ INC46：被测的 rationale 放在**第 1 步**。

        第 0 步的可见窗口是空的（首步没有历史观察），实体核查显式早退——
        照旧把被测 rationale 放在第 0 步，测的其实是「首步」那一支，
        而它现在一律不判幻觉。第 1 步的窗口含第 0 条观察，
        才是「引用了观察里没有的实体」这条断言真正要打的位置。
        """
        arc = _arc(
            search_observation=_obs("https://s.test", body=obs_text),
            steps=[_step("goto", None), _step("goto", rationale)],
        )
        return schema.split_archive(_write(tmp_path, arc))[0]

    def test_写了且无硬发现判通过(self, tmp_path):
        s = self._sample(tmp_path, _written("打开搜索结果页看看有哪些站点"))
        assert s.gate.status == "passed", s.gate.reasons
        assert s.gate.reasons

    def test_一条都没写仍记not_run(self, tmp_path):
        """⚠️ 绝不渲染成「通过」——批次上线时它看起来像过了闸。"""
        s = self._sample(tmp_path, None)
        assert s.gate.status == "not_run"
        assert s.gate.reasons

    def test_全部未写成也是not_run而非passed(self, tmp_path):
        s = self._sample(tmp_path, {"status": "empty", "reason": "模型没说话"})
        assert s.gate.status == "not_run"

    def test_有硬发现判失败且定位到步(self, tmp_path):
        """实体核查命中：rationale 提到一个观察里没有的 URL。"""
        s = self._sample(
            tmp_path,
            _written("页面上写着「详见 https://evil.test/x」这个入口"),
        )
        assert s.gate.status == "failed"
        assert any("第 1 步" in r for r in s.gate.reasons)

    def test_首步不判幻觉(self, tmp_path):
        """INC46：首步没有历史观察，实体核查显式早退——不判幻觉。

        这是全链路最容易误伤的位置：每一条 goto 首步的动机都只能
        引用任务给的片名/诉求，判它幻觉等于把整批首步判死。
        """
        arc = _arc(
            search_observation=_obs("https://s.test", body="正文"),
            steps=[_step("goto", _written("页面上写着「详见 https://evil.test/x」"))],
        )
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert s.gate.status == "passed", s.gate.reasons

    def test_片名是豁免域不判幻觉(self, tmp_path):
        """rationale 提到片名而观察里没有它，**不是幻觉**——
        片名来自任务。把任务给的词当「模型引入的实体」去核，
        会把每一条提到片名的 rationale 都判成失败。"""
        s = self._sample(tmp_path, _written("这是《功夫》的搜索结果页"))
        assert s.gate.status == "passed", s.gate.reasons

    def test_理由不是模块未落地(self, tmp_path):
        """2026-10-10 前的错理由：这批存档没写「rationale 模块未落地」。
        模块 4 早就落地了，真实理由只能是「这批存档没有 rationale 字段」。"""
        s = self._sample(tmp_path, None)
        assert not any("模块未落地" in r for r in s.gate.reasons)
