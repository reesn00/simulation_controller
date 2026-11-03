"""P2 切分的单元测试——**一个任务一条轨迹**。

⚠️ **2026-10-30 · 方案 23**：粒度从「一个决策单元一条」改成
**「一个任务一条轨迹」**——一份 P1 存档切出 **0 或 1** 条样本。

重点不是「能切出几条」，而是四件切错了就静默的事：

1. **粒度**：一份存档一条轨迹，站点级结论降级成**轨迹内明细**
   （``site_outcomes``），不再被切成独立样本。
2. **outcome 配对**：存档里 ``visits[i].url`` 是候选链接，
   ``outcomes[j].url`` 是落地地址，跳转过的站两者不等——配不上就是
   每条负样本都显示成「无结论」，报表上负样本一条不少、分子全空。
   口径在 ``trajectory_pipeline.outcomerules``（单一真源）。
3. **降级可见**：老存档缺 ``steps`` / ``user_prompt`` / 全文正文，
   这些样本必须**自带标记**，不能和新存档的样本混着用。
4. **被反爬拦的 run 不产出轨迹**：那一步的观察是验证码页，
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
        # 单元级 ``reflection`` 键 = **INC46 时代标记**（键在即新口径，
        # 哪怕值是 None）。缺了它 ``rationale_era_of`` 会把这份单元判成
        # 事后反思口径、闸门转 ``not_run``。要测旧口径就显式 pop 掉它。
        "reflection": None,
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
        # 搜索阶段**一步**（实测 145 份真实存档：138 份顶层恰好 1 步、
        # 7 份 0 步——而那 7 份**同时**没有 ``search_observation``，
        # 即「有快照必有动作步」是实测常态）。fixture 按真实形态给，
        # 否则观察流会与动作流**对不上**（动作 0 份、观察 1 份）。
        "steps": [{
            "action": {"tool": "goto", "params": {"url": "https://s.test"},
                       "origin": "model"},
            "observation": _obs("https://s.test"),
            "error": "",
            "rationale": None,
        }],
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
        # 搜索单元的时代标记（同 ``_visit``）：键在 = INC46 口径。
        "reflection": None,
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
    """⚠️ **2026-10-30 · 方案 23**：粒度从「一个决策单元一条」改成
    **「一个任务一条轨迹」**——一份 P1 存档切出 **0 或 1** 条样本。"""

    def test_一份存档切出一条轨迹(self, tmp_path):
        arc = _arc(visits=[_visit("https://a.test/"), _visit("https://b.test/")])
        samples = schema.split_archive(_write(tmp_path, arc))
        assert len(samples) == 1
        # id = <task_id>__<run_id>，与 P1 存档名、与 T 的 trajectory_id 同构
        assert samples[0].sample_id == "T001__abc123"

    def test_站点级明细留在轨迹里(self, tmp_path):
        """裁定 ③：站点级结论**不再被切成独立样本**，但**不能丢**——
        它回答「这条轨迹里到底在哪一步走不通」。"""
        arc = _arc(
            visits=[_visit("https://a.test/"), _visit("https://b.test/")],
            outcomes=[
                _outcome("https://a.test/", "no_play_control"),
                _outcome("https://b.test/", "subscription_wall"),
            ],
        )
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert [o.index for o in s.site_outcomes] == [1, 2]
        assert [o.outcome.branch for o in s.site_outcomes] == [
            "no_play_control",
            "subscription_wall",
        ]

    def test_动作流是摊平的(self, tmp_path):
        """搜索阶段的步在前、站点步在后，接成**一条**流——
        ``step_index`` 是全局序号（否则第 5 步的 rationale 会撞上
        第 1 个单元的第 5 步，静默丢思考）。"""
        arc = _arc(
            steps=[_step("goto", url="https://s.test")],
            visits=[_visit("https://a.test/", steps=[_step("click", url="x")])],
        )
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert [a.tool for a in s.actions] == ["goto", "click"]

    def test_站点单元带落地前后两个观察(self, tmp_path):
        """观察**逐步对位**取自 ``steps[].observation``（2026-10-15 改，
        原取 ``(site_obs, player_obs)`` 去重快照对）——那对快照与动作流
        不等长，``new_tab`` 步的 null 观察会被真值过滤丢掉，整段错位。

        ⚠️ **2026-10-30 · 方案 23**：观察流是**整条轨迹**摊平的——
        搜索阶段那一步的观察排在最前，后面才是站点的两步。"""
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
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert [o["url"] for o in s.observations] == [
            "https://s.test",
            "https://a.test",
            "https://a.test/play",
        ]

    def test_搜索快照没有动作步时不进样本(self, tmp_path):
        """退化形态：**有搜索页快照、顶层没有动作步**。

        真实批次里 138/145 份存档「顶层恰好 1 步 + 有快照」同生共死
        （那 7 份 0 步的**同时**没有快照），所以这条形态走不到线上。

        处置：快照**不进 P2**——P2 的观察流与动作流是**一一对应**的，
        一份没有动作配对的观察会让后面每一步的「动作↔观察」整段错位
        （E005 那个老坑）。宁可少一份，也不错位；但这份存档会被
        ``steps`` 降级标记点名，不让它悄悄混进干净样本堆。
        """
        arc = _arc(steps=[], search_observation=_obs("https://s.test"))
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert s.observations == ()
        assert s.actions == ()
        assert "steps" in s.degraded_from

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
        s = schema.split_archive(_write(tmp_path, arc))[0]
        # 4 个动作 = 搜索阶段 1 步 + 站点 3 步；4 份观察逐一对位
        assert len(s.actions) == 4
        assert len(s.observations) == 4, "缺位观察被丢掉就会再次错位"
        assert s.observations[2]["degraded"] == ["observation_missing"]
        assert s.observations[2], "占位必须真值为真（防下游 if o 再丢）"
        assert s.observations[3]["url"] == "https://a.test/play"

    def test_没有观察的单元不产出(self, tmp_path):
        """两者皆空 → 切不出东西。抛出来而不是给一条空样本。"""
        p = _write(tmp_path, _arc(visits=[], search_observation=None))
        with pytest.raises(P1ShapeError, match="切不出轨迹"):
            schema.split_archive(p)

    def test_没有结论时任务级outcome为None(self, tmp_path):
        """一条站点结论都没有 → **判不出来**（fail-closed），
        不是成功。``outcome=None`` 与「成功」（``branch=None``）是两回事。"""
        s = schema.split_archive(_write(tmp_path, _arc()))[0]
        assert s.outcome is None


class TestOutcomePairing:
    """⚠️ **2026-10-30 · 方案 23**：配对口径收敛到
    :mod:`trajectory_pipeline.outcomerules`（候选 → 落地 → 播放页逐档试，
    每档匹 ``site_url`` 或 ``url``，**同档取末条**）。"""

    def test_跳转过的站按落地地址配(self, tmp_path):
        """实测存档：4 个 run 各有 3 个访问点是 http→https 跳转过的，
        候选 url 与 outcome url 对不上。只按候选 url 配的后果是静默的——
        负样本一条不少，分子全空。"""
        arc = _arc(
            visits=[_visit("http://x.test/movie", "https://x.test/movie")],
            outcomes=[_outcome("https://x.test/movie", "no_play_control")],
        )
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert s.outcome is not None
        assert s.outcome.branch == "no_play_control"

    def test_候选地址也能配(self, tmp_path):
        arc = _arc(
            visits=[_visit("https://x.test/movie")],
            outcomes=[_outcome("https://x.test/movie", "no_play_control")],
        )
        assert (
            schema.split_archive(_write(tmp_path, arc))[0].outcome.branch
            == "no_play_control"
        )

    def test_同档取末条(self, tmp_path):
        """同一个候选先记过一条中间结论、后记过最终结论——**末条**才是
        最终结论（取首个会拿到中间那条）。"""
        arc = _arc(
            visits=[_visit("https://x.test/movie")],
            outcomes=[
                _outcome("https://x.test/movie", "unresolved"),
                _outcome("https://x.test/movie", "subscription_wall"),
            ],
        )
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert s.outcome.branch == "subscription_wall"

    def test_配不上要留痕(self, tmp_path):
        """静默 None 与「这条轨迹本来就没有结论」同形。"""
        arc = _arc(visits=[_visit("https://x.test/movie")], outcomes=[])
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert s.outcome is None
        assert "outcome_missing" in s.degraded_from

    def test_成功即无分支(self, tmp_path):
        """``ledger`` 只记失败分支，成功的访问不记账——
        所以 ``branch=None`` 是**成功**，不是「没结论」。"""
        arc = _arc(
            visits=[_visit("https://x.test/", success=True)],
            outcomes=[_outcome("https://x.test/", None)],
        )
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert s.outcome.succeeded is True

    def test_成功与分支对不上要留痕(self, tmp_path):
        """「成功」直接决定样本进不进训练集，而它必须只有一个来源说了算。
        两个来源打架时标记出来，而不是挑一个——挑一个就是在猜。"""
        arc = _arc(
            visits=[_visit("https://x.test/", success=True)],
            outcomes=[_outcome("https://x.test/", "no_play_control")],
        )
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert "success_branch_mismatch" in s.degraded_from

    def test_不从存档的is_negative_sample取判据(self, tmp_path):
        """那份判据由 executor 的 ``NON_SAMPLE_BRANCHES`` 定义，assembler
        不 import executor，两份口径就可能不同步。存档里把
        ``is_negative_sample`` 写成 True 也不采信。"""
        o = _outcome("https://x.test/", None)
        o["is_negative_sample"] = True
        arc = _arc(visits=[_visit("https://x.test/", success=True)], outcomes=[o])
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert s.outcome.branch is None


class TestBlockedRun:
    """搜索阶段被拦 / 采集失败 ⇒ **整条轨迹产出零条**。

    ⚠️ **2026-10-30 · 方案 23**：旧口径下「搜索单元不产出、站点单元仍
    产出」。现在一条轨迹是一个整体——搜索页是验证码页的轨迹，第一步
    就是「模型对着验证码页看」，那样的样本只会教它对着验证码编答案。

    而「被拦但还有访问记录」在执行端**不可能发生**：拦截发生在取候选
    之前，一收口就整条 run 结束（``executor/orchestrator.py`` 的
    ``record.search_blocked = blocked`` 之后直接 return）。实测 145 份
    真实存档里 14 份被拦，**0 份**带访问记录。
    """

    def test_被反爬拦时不产出轨迹(self, tmp_path):
        """拦截发生在取候选之前，那一步的观察是验证码页。切成样本只会
        教模型对着验证码页编答案。"""
        arc = _arc(search_blocked="captcha", visits=[])
        assert schema.split_archive(_write(tmp_path, arc)) == []

    def test_被拦即使有访问记录也不产出(self, tmp_path):
        arc = _arc(
            search_blocked="captcha",
            visits=[_visit("https://a.test/")],
            outcomes=[_outcome("https://a.test/", "unresolved")],
        )
        assert schema.split_archive(_write(tmp_path, arc)) == []

    def test_搜索页采集失败时不产出轨迹(self, tmp_path):
        """T048 这类坏档：``search_blocked`` 为空，但 warnings 记了
        「搜索页采集失败」（网络错误），同样没有进入决策——零条是
        正常形态，不能抛。"""
        arc = _arc(
            search_observation=None,
            visits=[],
            warnings=["搜索页采集失败: DriverError: Network error"],
        )
        assert schema.split_archive(_write(tmp_path, arc)) == []

    def test_采集失败即使有访问记录也不产出(self, tmp_path):
        arc = _arc(
            search_observation=None,
            visits=[_visit("https://a.test/")],
            outcomes=[_outcome("https://a.test/", "unresolved")],
            warnings=["搜索页采集失败: DriverError: Network error"],
        )
        assert schema.split_archive(_write(tmp_path, arc)) == []


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
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert list(s.degraded_from) == list(dict.fromkeys(s.degraded_from))

    def test_新存档零降级(self, tmp_path):
        """一条干净的样本必须能一眼认出来。标记若总在，整条标记就失效了。

        ⚠️ **2026-10-30 · 方案 23**：降级标记是**轨迹级**的——访问点配不
        到 outcome 会把整条轨迹标成「outcome_missing」（旧口径下只是那个
        站点单元降级）。所以这条夹具必须给访问点配一条 outcome，
        才算「干净存档」。"""
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
            outcomes=[_outcome("https://a.test/", "no_play_control")],
        )
        assert schema.split_archive(_write(tmp_path, arc))[0].degraded_from == ()


class TestSampleId:
    def test_格式可反查源存档(self, tmp_path):
        """⚠️ **2026-10-30 · 方案 23**：id = ``<task_id>__<run_id>``——
        与 P1 存档名、与 T 的 ``trajectory_id`` **逐字相同**。
        从 id 就能反查源存档，不必开文件。"""
        sid = schema.split_archive(_write(tmp_path, _arc()))[0].sample_id
        assert sid == "T001__abc123"
        task, run = sid.split("__")
        assert task == "T001"
        assert run

    def test_一份存档一条id(self, tmp_path):
        """12 个访问点仍是**一条**轨迹、一个 id。"""
        arc = _arc(visits=[_visit(f"https://a{i}.test/") for i in range(12)])
        samples = schema.split_archive(_write(tmp_path, arc))
        assert [s.sample_id for s in samples] == ["T001__abc123"]
        assert len(samples[0].site_outcomes) == 12

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
            "T001__a1"
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
        """模型可见集 = 4 已用动作 + 7 能力声明；**排除**执行器自己的
        会话管理动作（``new_tab`` / ``close_tab``）。

        ⚠️ 2026-10-20（增量六十二）起可见集从 4 条变成 11 条——多的 7 条
        是**能力声明**（模型被告知存在，但本项目没有任何真实调用产生过
        它们）。可见性由 catalog 的 ``visibility`` 字段判，不再是名字
        黑名单。口径见 ``trajectory_pipeline/toolcatalog/``。
        """
        visible = {t.name for t in schema.TOOL_SPECS if t.visible_to_model()}
        assert visible == {
            # 已用动作
            "goto", "click", "fill", "press",
            # 能力声明
            "web_search", "web_fetch", "view_image", "desktop_screenshot",
            "get_current_time", "read_file", "glob_search",
        }
        assert not (visible & {"new_tab", "close_tab"}), (
            "执行器自己的会话管理动作不该给模型看"
        )

    def test_不可见的两条在全集里(self):
        """排除 ≠ 不声明。``new_tab`` / ``close_tab`` 留在 P2 的
        ``six.tools`` 里——动作流里有它们，工具清单里没有的话，
        读样本的人会以为那两步是凭空冒出来的。"""
        names = {t.name for t in schema.TOOL_SPECS}
        assert {"new_tab", "close_tab"} <= names

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
        # ⚠️ **2026-10-30 · 方案 23**：一条轨迹一条样本，**没有单元之分**。
        return samples[0]

    def test_逐条对齐不压成一条(self, tmp_path):
        s = self._site_sample(
            tmp_path,
            [
                _step("goto", _written("先打开候选站"), "https://a.test/"),
                _step("click", _written("页面里有播放按钮，点它")),
            ],
        )
        # ⚠️ **2026-10-30 · 方案 23**：``step_index`` 是**全局**序号——
        # 搜索阶段那一步占了 0，所以站点的两步是 1、2。旧口径每个单元
        # 各自从 0 数起，第 5 步的 rationale 会撞上第 1 个单元的第 5 步。
        assert [r.step_index for r in s.rationales] == [1, 2]
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
        assert got == {1: "第 0 步", 3: "第 2 步"}

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


class TestRationale分代:
    """INC46 分代（2026-10-25）：**旧口径文本不能按新窗口核**。

    同一份存档里两代文本同构（落盘形状一模一样），唯一的结构判据是
    unit 级 ``reflection`` 键在不在。判错的后果是双向的——
    旧文本按新窗口核会产生**虚假失败**（它本来就该看本步之后的页面），
    也会产生**虚假通过**（窗口恰好没覆盖到它引用的东西）。
    """

    def _sample(self, tmp_path, *, era_new: bool, rationale=None):
        arc = _arc(
            search_observation=_obs("https://s.test", body="正文"),
            steps=[_step("goto", None), _step("goto", rationale)],
            **({"reflection": None} if era_new else {}),
        )
        if not era_new:
            arc.pop("reflection")
        s = schema.split_archive(_write(tmp_path, arc))[0]
        return s

    def test_新口径标pre_action(self, tmp_path):
        s = self._sample(tmp_path, era_new=True,
                         rationale=_written("打开搜索结果页看看有哪些站点"))
        assert s.rationale_era == "pre_action"
        assert s.gate.status == "passed"

    def test_旧口径标post_hoc且闸门不判(self, tmp_path):
        """⚠️ 关键：**不是 failed**。旧文本按新窗口核出来的「失败」
        是窗口错配的产物，把它当真实结论去修数据，等于按错的尺子改数据。"""
        s = self._sample(
            tmp_path, era_new=False,
            rationale=_written("页面上写着「详见 https://evil.test/x」这个入口"),
        )
        assert s.rationale_era == "post_hoc"
        assert s.gate.status == "not_run"
        assert any("事后反思" in r for r in s.gate.reasons)

    def test_一条没写成时口径是空串(self, tmp_path):
        """空串 ≠ 旧口径：前者没什么可训，后者是**有文本但口径不对**。"""
        s = self._sample(tmp_path, era_new=True, rationale=None)
        assert s.rationale_era == ""
        assert s.rationale_missing is True

    def test_轨迹按存档顶层判代(self, tmp_path):
        """⚠️ **2026-10-30 · 方案 23**：口径按**存档顶层**的 ``reflection``
        键判——不再分单元。

        依据：实测 145 份真实存档，顶层与各 visit 的 ``reflection`` 键
        **总是同时在或同时不在**，所以轨迹级口径不会混代。"""
        arc = _arc(
            visits=[_visit("https://a.test/",
                           steps=[_step("goto", _written("直接点进去看看"))])],
            outcomes=[_outcome("https://a.test/", "no_play_control")],
        )
        arc.pop("reflection")
        samples = schema.split_archive(_write(tmp_path, arc))
        assert samples[0].rationale_era == "post_hoc"
        assert samples[0].gate.status == "not_run"

    def test_era写进to_json(self, tmp_path):
        """消费方（报告/人工复核/批次筛选）读的是 JSON，不是 dataclass。"""
        s = self._sample(tmp_path, era_new=True,
                         rationale=_written("打开搜索结果页"))
        assert s.to_json()["rationale_era"] == "pre_action"


class Test终局验收读回复:
    """方案 23 R2-3（owner 裁定 O-2）：**终局验收读最终回复**。

    判据只有一条：任务库 ``output_contract.min_urls ≥ 1``，而这条样本的
    收尾答复里没有 URL ⇒ **不许判 ``passed``**。

    这条闸门**不是** rationale 闸门：它判的是「答复兑现没兑现任务合同」。
    所以它只降 ``passed``（``failed`` 已经是失败、``not_run`` 是「没判」），
    且读的文本必须与 P3 渲染的末条 assistant **是同一个函数产出**——
    分开写的那天，验收读的就不是模型看到的那句了。
    """

    def _run_sample(
        self, tmp_path, *, branch="no_play_control", play_page_url="",
        task_id="T001",
    ):
        """一条**轨迹**（per-Run 之后一份存档切一条，不再按单元切）。"""
        url = "https://a.test/"
        outcome = dict(
            _outcome(url, branch), site_url=url, play_page_url=play_page_url
        )
        arc = _arc(
            task_id=task_id,
            visits=[
                dict(_visit(url), steps=[_step("goto", _written("看看这个站"))])
            ],
            outcomes=[outcome],
        )
        samples = schema.split_archive(_write(tmp_path, arc))
        assert len(samples) == 1, (
            f"per-Run 之后一份存档切一条轨迹，实得 {len(samples)}"
        )
        return samples[0]

    def test_失败且答复无网址时不许passed(self, tmp_path):
        """排除站点只写 host（不带协议），所以失败答复里没有 URL。"""
        s = self._run_sample(tmp_path)
        assert s.gate.status == "failed"
        assert any("输出契约" in r for r in s.gate.reasons)
        # 原有的 rationale 核查理由不丢——两条命运分开记。
        assert any("逐条核查" in r for r in s.gate.reasons)

    def test_成功且答复带网址时仍passed(self, tmp_path):
        s = self._run_sample(
            tmp_path, branch=None, play_page_url="https://a.test/play"
        )
        assert s.gate.status == "passed", s.gate.reasons

    def test_网址不带协议不算兑现(self, tmp_path):
        """答复说「找到了网址：a.test」——那不是一个能交付的网址。

        合同要的是 ``http(s)://``；只有 host 的地址用户点不开。
        """
        url = "a.test"
        outcome = dict(_outcome(url, None), site_url=url, play_page_url="")
        arc = _arc(
            visits=[dict(_visit(url), steps=[_step("goto", _written("看看这个站"))])],
            outcomes=[outcome],
        )
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert s.gate.status == "failed"
        assert any("只有 0 个" in r for r in s.gate.reasons)

    def test_要求两个网址只给一个也不许passed(self, tmp_path):
        """T019 的合同是 ``min_urls=2``——数不够同样没兑现。"""
        s = self._run_sample(
            tmp_path, task_id="T019", branch=None,
            play_page_url="https://a.test/play",
        )
        assert s.gate.status == "failed"
        assert any("至少 2 个网址" in r and "只有 1 个" in r
                   for r in s.gate.reasons)

    def test_不要求网址的任务不受影响(self, tmp_path):
        """T025 的合同没有 ``min_urls`` ⇒ 这条判据不适用，判定原样。"""
        s = self._run_sample(tmp_path, task_id="T025")
        assert s.gate.status == "passed", s.gate.reasons

    def test_任务库没有这个task时不猜(self, tmp_path):
        """未知 ``task_id`` ⇒ 读不到合同 ⇒ **不改判定**（fail-closed）。"""
        s = self._run_sample(tmp_path, task_id="ZZZ999")
        assert s.gate.status == "passed", s.gate.reasons

    def test_没有任务级结论时不判(self, tmp_path):
        """一个候选都没访问 ⇒ 没有任务级结论 ⇒ **没有「答复」可读**。

        ⚠️ **2026-11-03 · re-base 到 per-Run**：per-unit 时代这条判据是
        「搜索单元不判」（搜索单元的收尾是「接下来核验候选」，不是任务
        答复）。per-Run 之后**没有搜索单元**了——同一条判据落在
        「这条 run 没有任务级结论」上（判据不适用 ≠ 判通过）。
        """
        arc = _arc(
            search_observation=_obs("https://s.test", body="正文"),
            steps=[
                _step("goto", None),
                _step("goto", _written("打开搜索结果页看看有哪些站点")),
            ],
        )
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert s.outcome is None
        assert s.gate.status == "passed", s.gate.reasons

    def test_not_run不被改写成failed(self, tmp_path):
        """没有 rationale 的站点单元本来就没判——「没判」不是「通过」，
        但也不能被这条判据改写成「失败」（那是两个不同的事实）。"""
        url = "https://a.test/"
        arc = _arc(
            visits=[dict(_visit(url), steps=[_step("goto", None)])],
            outcomes=[dict(_outcome(url, "no_play_control"), site_url=url)],
        )
        s = schema.split_archive(_write(tmp_path, arc))[0]
        assert s.gate.status == "not_run"
