"""模块 1（taskgen）的单元与不变式测试。

测试分两类：

**取值域测试**（解析对不对）——骨架字段、模式判定、画像加载。

**不变式测试**（纪律守不守）——每条纪律配一个「反过来仍成立」的反例。
只测正例的纪律等于没测：一个从不误伤的检查和一个恒过的检查，
在测试报告里长得一模一样。
"""

from __future__ import annotations

import re

import pytest

from trajectory_pipeline.taskgen.persona import renderer
from trajectory_pipeline.taskgen.persona.library import (
    BARE_TASK_RATIO,
    LibraryError,
    PersonaLibrary,
    load_library,
)
from trajectory_pipeline.taskgen.persona.lexicon import (
    CLOSERS,
    CONSTRAINT_PROBES,
    GENRE_TERMS,
    OPENERS,
    SPECIFICITY_RENDER,
    VERBAL_TAILS,
)
from trajectory_pipeline.taskgen.persona.schema import PersonaProfile, load_profile
from trajectory_pipeline.taskgen.normalizer import (
    FAIL_EMPTY,
    FAIL_PROBE_MISS,
    FAIL_TOO_SHORT,
    normalize,
    probe_report,
)
from trajectory_pipeline.taskgen.sampler import probe_skeletons, sample_tasks
from trajectory_pipeline.taskgen.skeleton import (
    SkeletonError,
    TaskSkeleton,
    extract_title,
    load_skeletons,
    mode_distribution,
    parse_skeleton,
)

TASKS_YAML = "simulate_serve/config/tasks.yaml"


# ══════════════════════════════════════════════════════════════════════
# fixtures
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def skeletons() -> tuple[TaskSkeleton, ...]:
    return load_skeletons(TASKS_YAML)


@pytest.fixture(scope="module")
def library() -> PersonaLibrary:
    return load_library()


def _persona(**over) -> PersonaProfile:
    base = dict(
        persona_id="t-1", genre="科幻", popularity="腰部", urgency="本周",
        verbal_style="口语", persona_presence="无", task_specificity="指名",
        has_standard=True,
    )
    base.update(over)
    return PersonaProfile.model_validate(base)


def _skeleton(**over) -> TaskSkeleton:
    base = dict(
        task_id="X001", scenario_id="media_lookup_standard", dimension="d",
        task_type="视频查询", explain="",
        initial_request="找到电视剧《武林外传》全集在线观看的可播放网址",
        goal="找到电视剧《武林外传》在线观看的可播放网址",
        title="武林外传", retrieval_mode="single_title", constraints=(),
        criterion_texts=(), output_contract={}, excluded_platforms=(),
        reference_notes=(),
    )
    base.update(over)
    return TaskSkeleton(**base)


# ══════════════════════════════════════════════════════════════════════
# 骨架解析
# ══════════════════════════════════════════════════════════════════════


class TestSkeleton:
    def test_存量98条全部可解析(self, skeletons):
        assert len(skeletons) == 98
        assert all(s.task_id for s in skeletons)

    def test_书名号片名(self):
        assert extract_title("找到电视剧《武林外传》全集") == "武林外传"

    def test_直角引号片名也算(self):
        # 存量英文片名用的是直角引号，不是书名号
        assert extract_title("搜索\"Kung Fu Hustle\"在线观看") == "Kung Fu Hustle"

    def test_无片名的泛指任务不硬提(self):
        # 反例：不能把句子里的普通引号当片名
        assert extract_title("我想找\"电影\"在线观看") is None
        assert extract_title("周星驰执导的所有电影") is None

    def test_三种检索模式(self, skeletons):
        dist = mode_distribution(skeletons)
        assert dist == {"single_title": 81, "aggregate": 15, "unknown_title": 2}

    def test_聚合型不带单数信号(self, skeletons):
        # 「梁朝伟参演的文艺片」没有片名也没有"找一部"→ 集合，不是单片
        t8 = next(s for s in skeletons if s.task_id == "T008")
        assert t8.retrieval_mode == "aggregate"

    def test_单数信号才是unknown(self, skeletons):
        t67 = next(s for s in skeletons if s.task_id == "T067")
        assert "不记得" in t67.initial_request
        assert t67.retrieval_mode == "unknown_title"

    def test_只有single_title在W1范围内(self, skeletons):
        for s in skeletons:
            assert s.runnable_in_w1 == (s.retrieval_mode == "single_title")

    def test_缺task_id显式抛(self):
        with pytest.raises(SkeletonError, match="task_id"):
            parse_skeleton({"initial_request": "找《X》"})

    def test_缺initial_request显式抛(self):
        with pytest.raises(SkeletonError, match="initial_request"):
            parse_skeleton({"task_id": "T999"})

    def test_文件缺失显式抛不猜默认(self, tmp_path):
        with pytest.raises(SkeletonError, match="找不到"):
            load_skeletons(tmp_path / "nope.yaml")

    def test_判分标准原样保留(self, skeletons):
        # criterion_texts 是只读副本，改写不得覆盖——这里确认它非空且成句
        t1 = next(s for s in skeletons if s.task_id == "T001")
        assert t1.criterion_texts
        assert any("网址" in c for c in t1.criterion_texts)


class TestConstraintDerivation:
    def test_约束只从用户表述推导(self, skeletons):
        """min_results=5 在判分标准里，但用户**没说**要 5 个 → 不得推导。"""
        t1 = next(s for s in skeletons if s.task_id == "T001")
        keys = {c.key for c in t1.constraints}
        assert "has_url" in keys
        assert "has_count" not in keys, "用户表述里没有条数要求，不该推导"

    def test_表述里说了才算(self, skeletons):
        t2 = next(s for s in skeletons if s.task_id == "T002")
        assert "has_url" in {c.key for c in t2.constraints}

    def test_检测词与探针同源(self, skeletons):
        """推导用 CONSTRAINT_PROBES 的词，不另写一套。"""
        t1 = next(s for s in skeletons if s.task_id == "T001")
        for c in t1.constraints:
            if c.key in CONSTRAINT_PROBES:
                probes = CONSTRAINT_PROBES[c.key][1]
                assert c.detail.split("（")[0] in probes

    def test_全集能识别成集数要求(self, skeletons):
        t1 = next(s for s in skeletons if s.task_id == "T001")
        assert "has_episode_range" in {c.key for c in t1.constraints}


# ══════════════════════════════════════════════════════════════════════
# persona
# ══════════════════════════════════════════════════════════════════════


class TestPersona:
    def test_frozen不可改(self):
        p = _persona()
        with pytest.raises(Exception):
            p.genre = "恐怖"          # type: ignore[misc]

    def test_extra字段被拒(self):
        with pytest.raises(Exception):
            PersonaProfile.model_validate({
                **_persona().model_dump(), "content_tier": "A",
            })

    def test_content_tier不在画像上(self):
        """来源纪律：档位由组合层推导，画像自带它就可能被随手填。"""
        assert "content_tier" not in PersonaProfile.model_fields

    def test_tier派生规则(self):
        assert _persona(popularity="冷门").content_tier_for() == "C"
        assert _persona(popularity="头部").content_tier_for() == "A"
        assert _persona(popularity="腰部").content_tier_for() == "B"

    def test_骨架可否决画像(self):
        """画像说头部、骨架是冷门 → 按骨架走，否则切片轴分母失真。"""
        p = _persona(popularity="头部")
        assert p.content_tier_for(skeleton_popularity="冷门") == "C"

    def test_空persona_id显式抛(self):
        with pytest.raises(ValueError):
            load_profile({"persona_id": "  ", "genre": "科幻",
                          "popularity": "腰部", "urgency": "本周",
                          "verbal_style": "口语", "persona_presence": "无"})

    def test_报错信息含persona_id(self):
        with pytest.raises(ValueError, match="找不到片名的人"):
            load_profile({"persona_id": "找不到片名的人", "genre": "武侠"})

    def test_六维全在(self):
        fields = PersonaProfile.model_fields
        for dim in ("genre", "popularity", "urgency", "verbal_style",
                    "persona_presence", "task_specificity", "has_standard"):
            assert dim in fields


class TestLibrary:
    def test_库可加载且非空(self, library):
        assert len(library) > 0

    def test_无缺档(self, library):
        assert library.missing_dims() == {}

    def test_无退化维度(self, library):
        """每档 ≥3 条，否则切片表出单格行。"""
        assert library.degenerate_dims() == {}

    def test_每档至少三条(self, library):
        for dim, counts in library.coverage().items():
            present = [n for n in counts.values() if n > 0]
            assert min(present) >= 3, f"{dim} 有档位不足 3 条"

    def test_库有裸任务(self, library):
        assert sum(1 for p in library if not p.has_standard) >= 5

    def test_id唯一(self, library):
        ids = [p.persona_id for p in library]
        assert len(ids) == len(set(ids))

    def test_采样确定性(self, library):
        a = library.stratified_sample(10, seed=42)
        b = library.stratified_sample(10, seed=42)
        assert a == b

    def test_换seed会换结果(self, library):
        a = library.stratified_sample(10, seed=1)
        b = library.stratified_sample(10, seed=2)
        assert a != b

    def test_采样量超库存显式抛(self, library):
        with pytest.raises(LibraryError, match="库里只有"):
            library.stratified_sample(len(library) + 1)

    def test_配比不达标显式抛而非降配比(self):
        """配比降了不会报错，只会静默失去对照——所以必须抛。"""
        bare_only = PersonaLibrary(profiles=tuple(
            _persona(persona_id=f"b{i}", has_standard=False) for i in range(10)
        ))
        with pytest.raises(LibraryError, match="裸任务"):
            bare_only.stratified_sample(10, bare_ratio=BARE_TASK_RATIO)

    def test_未知分层维度显式抛(self, library):
        with pytest.raises(LibraryError, match="未知分层维度"):
            library.stratified_sample(5, strata=("不存在的维度",))

    def test_digest随内容变(self, library):
        import dataclasses

        changed = dataclasses.replace(
            library,
            profiles=(library.profiles[0].model_copy(
                update={"urgency": "闲时"}), *library.profiles[1:]),
        )
        assert changed.digest() != library.digest()

    def test_按id取_profile(self, library):
        assert library.by_id(library.profiles[0].persona_id) == library.profiles[0]

    def test_取不存在的id抛KeyError(self, library):
        with pytest.raises(KeyError):
            library.by_id("nope")


# ══════════════════════════════════════════════════════════════════════
# 渲染器
# ══════════════════════════════════════════════════════════════════════


class TestTailJoining:
    """句尾语气词必须接成**同一句**。

    这条不是洁癖：「…就行。啊。」读起来是两段话拼接，
    而**现有检查一条都抓不到**——探针只看判分要求丢没丢，
    措辞内部的语义与句法问题不在任何一条判分要求里。
    """

    def test_剥掉closer的句号(self, library, skeletons):
        from trajectory_pipeline.taskgen.persona import renderer
        assert renderer._join_tail("这两天给我就行。", "啊。") == "这两天给我就行啊。"
        assert renderer._join_tail("越快越好，今晚要看！", "啊。") == \
            "越快越好，今晚要看啊。"

    def test_无tail时原样保留(self, library, skeletons):
        from trajectory_pipeline.taskgen.persona import renderer
        assert renderer._join_tail("慢慢找也没关系。", "") == "慢慢找也没关系。"

    def test_渲染结果无双句边界(self, library, skeletons):
        """真跑一遍渲染：口语与强口语档都不能出现「。啊。」这类断句。"""
        from trajectory_pipeline.taskgen.persona import renderer
        checked = 0
        for persona in library.profiles:
            if persona.verbal_style not in ("口语", "强口语"):
                continue
            for skel in skeletons:
                if not skel.runnable_in_w1 or not renderer.is_compatible(persona, skel):
                    continue
                text = renderer.render(skel, persona, seed=0).prompt_text
                for punct in ("。啊", "。呢", "。吧"):
                    assert punct not in text, f"{persona.persona_id}: {text}"
                assert not text.endswith("。啊。"), text
                checked += 1
                break
        assert checked >= 20, f"只覆盖了 {checked} 条，口语档本该占全库多数"


class TestClauseJoining:
    """片段拼接必须留分隔——存量 initial_request 大多不以标点结尾。"""

    def test_closer前补逗号(self, library, skeletons):
        from trajectory_pipeline.taskgen.persona import renderer
        assert renderer._join_closer("做版本对比", "慢慢找也没关系。") == \
            "做版本对比，慢慢找也没关系。"

    def test_已有标点不重复加(self, library, skeletons):
        from trajectory_pipeline.taskgen.persona import renderer
        assert renderer._join_closer("想看。", "慢慢找。") == "想看。慢慢找。"
        assert renderer._join_closer("想看，", "慢慢找。") == "想看，慢慢找。"

    def test_无closer时原样(self, library, skeletons):
        from trajectory_pipeline.taskgen.persona import renderer
        assert renderer._join_closer("做版本对比", "") == "做版本对比"

    def test_渲染结果无粘连(self, library, skeletons):
        """端到端：骨架原文的末字不得与 closer 的首字直接相连。

        必须先过 :func:`renderer.is_compatible`——不兼容组合（纯描述 ×
        单片任务）会**早退返回骨架原文且 applied 为空**，那是设计行为，
        不该按拼接规则去检查。
        """
        from trajectory_pipeline.taskgen.persona import renderer
        checked = 0
        for persona in library.profiles:
            for skel in skeletons:
                if not skel.runnable_in_w1:
                    continue
                if not renderer.is_compatible(persona, skel):
                    continue
                text = renderer.render(skel, persona, seed=0).prompt_text
                assert "，" in text or text.rstrip().endswith(("。", "！")), \
                    f"{persona.persona_id}/{skel.task_id}: {text}"
                checked += 1
                break                      # 每个 persona 取一个骨架就够
        assert checked > 20, "兼容性过滤后样本太少，这条检查没实际覆盖"

    def test_closer措辞内部自洽(self, library, skeletons):
        """同一条 closer 里不得同时出现「不急」与紧期限词。"""
        from trajectory_pipeline.taskgen.persona.lexicon import CLOSERS
        tight = ("今天", "今晚", "这两天", "越快越好", "马上")
        for urgency, options in CLOSERS.items():
            for option in options:
                if "不急" in option or "不着急" in option:
                    assert not any(t in option for t in tight), \
                        f"{urgency} 档自相矛盾: {option}"

    def test_tail以语气词起头(self, library, skeletons):
        """tail 必须能当**句尾语气词**接在任意 closer 后面，不是独立成句。

        早期版本的「求求了真的。」「拜托哈，越快越好。」接在 closer 后
        得到「今晚要看求求了真的。」——「看」与「求求」粘成词组。

        判别式不靠分词（中文分词在这里太脆），而是**查设计契约**：
        它们是语气词，所以**必须以语气词起头**。这个规则可执行、
        无歧义，且旧词表一条都过不了。
        """
        from trajectory_pipeline.taskgen.persona.lexicon import VERBAL_TAILS
        particles = ("啊", "呀", "呢", "吧", "嘛", "哦", "噢")
        for style, tails in VERBAL_TAILS.items():
            for tail in tails:
                assert tail.startswith(particles), \
                    f"{style} 档的 {tail!r} 不以语气词起头，接不上 closer"

    def test_接上任意closer都不粘连(self, library, skeletons):
        """穷举 closer × tail，接缝处必须落在标点或语气词边界上。"""
        from trajectory_pipeline.taskgen.persona.lexicon import CLOSERS, VERBAL_TAILS
        particles = ("啊", "呀", "呢", "吧", "嘛", "哦", "噢")
        closers = [c for opts in CLOSERS.values() for c in opts]
        for style, tails in VERBAL_TAILS.items():
            for tail in tails:
                for closer in closers:
                    joined = renderer._join_tail(closer, tail)
                    stem = joined[: -len(tail)]          # 接缝右侧 = tail 的首字符
                    left, right = stem.rstrip(), tail[0]
                    assert (left[-1] in "，、：…—。！？"
                            or right in particles), \
                        f"{style} {tail!r} 接 {closer!r} 粘连: {joined}"


class TestRenderer:
    def test_指名保留片名原样(self):
        r = renderer.render(_skeleton(), _persona(task_specificity="指名"))
        assert "《武林外传》" in r.prompt_text
        assert r.applied_specificity == "指名"

    def test_半指代降级为指名并说明原因(self):
        """W1 渲染不出真半指代——说清楚，别伪装成已实现。"""
        r = renderer.render(_skeleton(), _persona(task_specificity="半指代"))
        assert r.compatibility == "downgraded"
        assert r.requested_specificity == "半指代"
        assert r.applied_specificity == "指名"
        assert any("半指代" in n for n in r.notes)

    def test_指名原样保留片名(self):
        r = renderer.render(_skeleton(), _persona(task_specificity="指名"))
        assert "《武林外传》" in r.prompt_text
        assert r.compatibility == "ok"

    def test_降级也保留骨架其余限定(self):
        """骨架要求里有别的限定时，不能连它一起改。"""
        sk = _skeleton(initial_request="找到《武林外传》的版权方与官方渠道",
                       goal="找到《武林外传》的版权方与官方渠道")
        r = renderer.render(sk, _persona(task_specificity="半指代"))
        assert "版权方与官方渠道" in r.prompt_text
        assert "《武林外传》" in r.prompt_text

    def test_纯描述配单片任务判不兼容(self):
        r = renderer.render(_skeleton(), _persona(task_specificity="纯描述"))
        assert r.compatibility == "incompatible"
        assert not r.usable

    def test_纯描述配无片名骨架可渲染(self):
        sk = _skeleton(title=None, retrieval_mode="aggregate",
                       initial_request="找周星驰执导的所有电影在线播放渠道",
                       goal="找周星驰执导的所有电影在线播放渠道")
        r = renderer.render(sk, _persona(task_specificity="纯描述"))
        assert r.usable

    def test_指名配无片名骨架降级(self):
        sk = _skeleton(title=None, retrieval_mode="aggregate",
                       initial_request="找周星驰执导的所有电影在线播放渠道",
                       goal="找周星驰执导的所有电影在线播放渠道")
        r = renderer.render(sk, _persona(task_specificity="指名"))
        assert r.compatibility == "downgraded"
        assert r.applied_specificity == "半指代"
        assert r.requested_specificity == "指名"

    def test_检索式始终用片名(self):
        """persona 改的是用户怎么说，不是世界的事实。"""
        r = renderer.render(_skeleton(), _persona(task_specificity="半指代"))
        assert r.search_query == "武林外传 在线观看"

    def test_无片名时检索式为空(self):
        sk = _skeleton(title=None, retrieval_mode="aggregate",
                       initial_request="找周星驰所有电影", goal="找周星驰所有电影")
        assert renderer.render(sk, _persona()).search_query == ""

    def test_渲染确定性(self):
        a = renderer.render(_skeleton(), _persona(persona_presence="重"), seed=1)
        b = renderer.render(_skeleton(), _persona(persona_presence="重"), seed=1)
        assert a.prompt_text == b.prompt_text

    # ── 不变式：genre 不得混入表述 ──────────────────────────────

    def test_genre不注入表述(self):
        """反例是 T027 那类：任务是找版权方，persona 却塞"动作片"。"""
        sk = _skeleton(
            initial_request="先确认电影《功夫》的版权方，再找官方合法观看渠道",
            goal="先确认电影《功夫》的版权方，再找官方合法观看渠道",
        )
        for genre in GENRE_TERMS:
            for term in GENRE_TERMS[genre]:
                p = _persona(genre=genre, task_specificity="半指代")
                out = renderer.render(sk, p).prompt_text
                assert term not in out, f"{genre} 的 {term!r} 混进了表述"

    def test_genre未生效被显式记录(self):
        """区分"有意不生效"与"忘了做"。"""
        r = renderer.render(_skeleton(), _persona())
        assert "genre_in_prompt" in r.applied

    # ── 不变式：不得拼出病句 ────────────────────────────────────

    def test_opener模板不含主语(self):
        for style, options in OPENERS.items():
            for opt in options:
                assert not opt.startswith("我想"), f"{style} 的 opener 带主语：{opt}"
                assert "我要" not in opt

    def test_渲染结果无主语重复(self, library, skeletons):
        stutter = re.compile(r"(?:我想|我要)\s*(?:我想|我要)")
        runnable = [s for s in skeletons if s.runnable_in_w1][:20]
        for sk in runnable:
            for p in library.profiles:
                if not renderer.is_compatible(p, sk):
                    continue
                text = renderer.render(sk, p, seed=0).prompt_text
                assert not stutter.search(text), f"病句：{text!r}"

    def test_语气词只在句尾(self, library, skeletons):
        """VERBAL_TAILS 加在末尾，不得插进要求中间。"""
        runnable = [s for s in skeletons if s.runnable_in_w1][:10]
        for sk in runnable:
            r = renderer.render(sk, _persona(verbal_style="强口语"))
            for tail in VERBAL_TAILS["强口语"]:
                if tail in r.prompt_text:
                    assert r.prompt_text.endswith(tail)


class TestLexicon:
    def test_探针词非空(self):
        for key, (label, probes) in CONSTRAINT_PROBES.items():
            assert label and probes, key

    def test_spec模板只依赖title(self):
        """模板不得再引用 {genre}——那是已知的越权来源。"""
        for name, tpl in SPECIFICITY_RENDER.items():
            assert "{genre}" not in tpl, f"{name} 模板仍注入 genre"

    def test_closer非空每档(self):
        for level in ("立即", "本周", "闲时"):
            assert CLOSERS[level]


# ══════════════════════════════════════════════════════════════════════
# normalizer —— 判分保真守卫
# ══════════════════════════════════════════════════════════════════════


class TestNormalizer:
    def test_原样通过(self, skeletons):
        s = next(x for x in skeletons if x.task_id == "T001")
        r = normalize(s, _persona(), s.initial_request)
        assert r.accepted

    def test_同义改写不被误杀(self, skeletons):
        s = next(x for x in skeletons if x.task_id == "T001")
        r = normalize(s, _persona(), "找到电视剧《武林外传》全集在线观看的可播放地址")
        assert r.accepted, "网址→地址是同义，不该判死"

    def test_丢网址要求被抓(self, skeletons):
        s = next(x for x in skeletons if x.task_id == "T001")
        r = normalize(s, _persona(), "找到电视剧《武林外传》全集在线观看")
        assert not r.accepted and r.fail_kind == FAIL_PROBE_MISS

    def test_丢全集要求被抓(self, skeletons):
        s = next(x for x in skeletons if x.task_id == "T001")
        r = normalize(s, _persona(), "找到电视剧《武林外传》在线观看的可播放网址")
        assert not r.accepted and r.fail_kind == FAIL_PROBE_MISS

    def test_只剩语气词被抓(self, skeletons):
        s = next(x for x in skeletons if x.task_id == "T001")
        r = normalize(s, _persona(), "今天就要看到，麻烦快一点。啊，急死我了。")
        assert not r.accepted

    def test_空串(self, skeletons):
        s = next(x for x in skeletons if x.task_id == "T001")
        assert normalize(s, _persona(), "").fail_kind == FAIL_EMPTY

    def test_过短(self, skeletons):
        s = next(x for x in skeletons if x.task_id == "T001")
        assert normalize(s, _persona(), "帮我找下。").fail_kind == FAIL_TOO_SHORT

    def test_不过时退回骨架原文(self, skeletons):
        s = next(x for x in skeletons if x.task_id == "T001")
        # ⚠️ 用例文本必须**不含任何探针词**。早先版本写成
        # 「没有网址也没有集数」——它含「网址」和「集数」，探针正确地放了行，
        # 于是测试红了，而红的原因是**用例写错了**，不是守卫有问题。
        r = normalize(s, _persona(), "随便给点什么都行，无所谓了")
        assert r.prompt_text == s.initial_request
        assert not r.accepted

    def test_probe_report能定位到样本(self, skeletons):
        s = next(x for x in skeletons if x.task_id == "T001")
        rep = probe_report([(s, _persona(), "把要求全删了"), (s, _persona(), s.initial_request)])
        assert rep["total"] == 2 and rep["accepted"] == 1
        assert rep["samples"] and rep["samples"][0]["task_id"] == "T001"

    def test_探针不是恒过的(self, skeletons, library):
        """接受率 100% 本身可疑——必须确认真丢要求时会被抓。"""
        s = next(x for x in skeletons if x.task_id == "T001")
        assert normalize(s, _persona(), "我想找《功夫》。").accepted is False


# ══════════════════════════════════════════════════════════════════════
# 采样器
# ══════════════════════════════════════════════════════════════════════


class TestSampler:
    def test_产出条数(self, library, skeletons):
        b = sample_tasks(12, library=library, skeletons=skeletons, seed=1)
        assert len(b.instances) == 12

    def test_确定性(self, library, skeletons):
        a = sample_tasks(8, library=library, skeletons=skeletons, seed=1)
        b = sample_tasks(8, library=library, skeletons=skeletons, seed=1)
        assert [i.to_json() for i in a.instances] == [i.to_json() for i in b.instances]

    def test_只产出W1可跑的(self, library, skeletons):
        b = sample_tasks(20, library=library, skeletons=skeletons, seed=1)
        assert all(i.retrieval_mode == "single_title" for i in b.instances)

    def test_排除被记账(self, library, skeletons):
        """静默排除会让报告看起来覆盖良好。

        ⚠️ 排除数是 **17**（aggregate 15 + unknown_title 2），不是 98——
        那 81 个 single_title 是被**用上**的。字段名说 excluded，
        值里就不能含未排除项：否则读的人得自己减一遍才知道真相。
        """
        b = sample_tasks(10, library=library, skeletons=skeletons, seed=1)
        r = b.report
        assert r.skeletons_seen == 98
        assert r.skeletons_w1_runnable == 81
        assert r.excluded_by_skeleton_mode == {"aggregate": 15, "unknown_title": 2}
        assert sum(r.excluded_by_skeleton_mode.values()) == 98 - 81
        assert any("不在 W1 范围内" in w for w in r.warnings())

    def test_排除数与可跑数互补(self, library, skeletons):
        """记账的自洽性：排除的 + 可跑的 = 全库。"""
        r = sample_tasks(6, library=library, skeletons=skeletons, seed=1).report
        assert (sum(r.excluded_by_skeleton_mode.values())
                + r.skeletons_w1_runnable) == r.skeletons_seen

    def test_模式分布无条件记录(self, library, skeletons):
        """模式分布是**骨架库的属性**，跟采样参数无关。"""
        for w1_only in (True, False):
            r = sample_tasks(6, library=library, skeletons=skeletons,
                             seed=1, w1_only=w1_only).report
            assert r.mode_distribution == {
                "single_title": 81, "aggregate": 15, "unknown_title": 2}, w1_only

    def test_全模式采样不谎报排除(self, library, skeletons):
        """``--all-modes`` 下**一个都没排除**。计数说谎比不计数更坏：
        读的人会以为真排掉了 98 个，进而以为"剩下 81 个都被用上了"。"""
        r = sample_tasks(6, library=library, skeletons=skeletons,
                         seed=1, w1_only=False).report
        assert r.excluded_by_skeleton_mode == {}
        assert not any("不在 W1 范围内" in w for w in r.warnings())
        # 但要说清"产出里会有跑不了的"
        assert any("--all-modes" in w for w in r.warnings())

    def test_seed落盘(self, library, skeletons):
        """计划文件说不清自己怎么采出来的，重跑对照就只能碰运气。"""
        js = sample_tasks(5, library=library, skeletons=skeletons,
                          seed=42).report.to_json()
        assert js["seed"] == 42

    def test_每条带全切片字段(self, library, skeletons):
        b = sample_tasks(10, library=library, skeletons=skeletons, seed=1)
        for inst in b.instances:
            for key in ("persona_id", "genre", "popularity", "urgency",
                        "verbal_style", "persona_presence",
                        "requested_specificity", "actual_specificity",
                        "content_tier", "compatibility", "rewritten"):
                assert key in inst.provenance, key

    def test_切片轴有值(self, library, skeletons):
        b = sample_tasks(30, library=library, skeletons=skeletons, seed=2)
        tiers = {i.provenance["content_tier"] for i in b.instances}
        assert tiers <= {"A", "B", "C"} and tiers

    def test_不兼容组合不产出(self, library, skeletons):
        b = sample_tasks(30, library=library, skeletons=skeletons, seed=3)
        for inst in b.instances:
            assert inst.provenance["compatibility"] != "incompatible"

    def test_归一结论落provenance(self, library, skeletons):
        b = sample_tasks(10, library=library, skeletons=skeletons, seed=4)
        for inst in b.instances:
            assert "normalize_accepted" in inst.provenance

    def test_检索式非空(self, library, skeletons):
        b = sample_tasks(10, library=library, skeletons=skeletons, seed=5)
        assert all(i.search_query for i in b.instances)

    def test_零条不报错(self, library, skeletons):
        b = sample_tasks(0, library=library, skeletons=skeletons, seed=1)
        assert b.instances == () and b.report.produced == 0

    def test_report可序列化(self, library, skeletons):
        import json

        b = sample_tasks(5, library=library, skeletons=skeletons, seed=1)
        assert json.loads(json.dumps(b.to_json(), ensure_ascii=False))

    def test_骨架多样性过低会报警(self, library, skeletons):
        b = sample_tasks(5, library=library, skeletons=skeletons, seed=1)
        # 5 条落在 9 个骨架上不算低；这里只断言 warnings() 可调用且不含异常
        assert isinstance(b.report.warnings(), tuple)

    def test_真实骨架上探针全过(self, library, skeletons):
        """renderer 不该丢判分要求。掉 0 条是这里的期望值。"""
        rep = probe_skeletons(library, skeletons, limit=20)
        assert rep["accept_rate"] == 1.0, rep["samples"][:3]

    def test_被排除的task在provenance可见(self, library, skeletons):
        """模式分布要跟着样本走，否则切片表看不到"这三类没跑"。"""
        b = sample_tasks(10, library=library, skeletons=skeletons, seed=6)
        assert b.report.by_mode == {"single_title": 10}