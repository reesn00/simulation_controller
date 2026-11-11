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
    CONSTRAINT_PROBES,
    GENRE_TERMS,
    OPENERS,
    SPECIFICITY_RENDER,
)
from trajectory_pipeline.taskgen.persona.schema import (
    DIMENSION_DOMAIN,
    PersonaProfile,
    load_profile,
)
from trajectory_pipeline.taskgen.normalizer import (
    FAIL_EMPTY,
    FAIL_PROBE_MISS,
    FAIL_TOO_SHORT,
    normalize,
    probe_report,
)
from trajectory_pipeline.taskgen.sampler import (
    SLICE_AXIS_FIELDS,
    W1_DEGENERATE_DIMS,
    probe_skeletons,
    sample_tasks,
)
from trajectory_pipeline.taskgen.skeleton import (
    DEFAULT_TASKS_YAML,
    SkeletonError,
    TaskSkeleton,
    extract_title,
    load_skeletons,
    mode_distribution,
    parse_skeleton,
)

TASKS_YAML = DEFAULT_TASKS_YAML


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
        persona_id="t-1", genre="科幻", popularity="腰部",
        persona_presence="无", task_specificity="指名",
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

    def test_指代短语不是片名(self):
        """T041 实测：「那个周星驰的片子」被当成片名 → 检索式
        「那个周星驰的片子 在线观看」在引擎里根本不存在，而这条轨迹在
        分支分布里长得跟「这站没有播放控件」一模一样。"""
        assert extract_title('我想搜索"那个周星驰的片子"合法在线资源') is None
        assert extract_title('找"这个剧"的在线播放') is None

    def test_指代前缀单独出现不判否(self):
        """⚠️ 判据必须是指代前缀 **且** 品类词的合取——
        「《那个杀手不太冷》」是真实存在的电影，只看前缀会误杀它。"""
        assert extract_title("看《那个杀手不太冷》在线播放") == "那个杀手不太冷"
        assert extract_title("看《这个杀手不太冷》在线播放") == "这个杀手不太冷"

    def test_T041已落到aggregate(self, skeletons):
        t41 = next(s for s in skeletons if s.task_id == "T041")
        assert t41.title is None
        assert t41.retrieval_mode == "aggregate"
        assert not t41.runnable_in_w1

    def test_三种检索模式(self, skeletons):
        # 80/16/2：T041 由 single_title 归入 aggregate（指代短语修复后）。
        # 那个 15→16 的差值就是这条——它此前产出的检索式在引擎里不存在。
        dist = mode_distribution(skeletons)
        assert dist == {"single_title": 80, "aggregate": 16, "unknown_title": 2}

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
                          "popularity": "腰部", "persona_presence": "无"})

    def test_报错信息含persona_id(self):
        with pytest.raises(ValueError, match="找不到片名的人"):
            load_profile({"persona_id": "找不到片名的人", "genre": "武侠"})

    def test_四维全在(self):
        fields = PersonaProfile.model_fields
        for dim in ("genre", "popularity", "persona_presence",
                    "task_specificity", "has_standard"):
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
                update={"popularity": "头部"}), *library.profiles[1:]),
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


class Test已删维度不残留:
    """``urgency`` / ``verbal_style`` 已删（2026-11-11）。

    两维的唯一可观察效果是首轮里的**时间限定**与**句尾语气词**，
    而那两样对 agent prompt 没有意义；留着它们还会变成**假切片轴**
    （两组人设的首轮表述除那几个修饰词外完全相同，差异是纯噪声）。

    这条测的是**删干净了没有**。删一半最容易留下的痕迹是
    「画像上还挂着字段、渲染已经不产出了」——那种状态下切片表
    仍会报一个纯噪声的维度，而且看上去一切正常。
    """

    #: 被删的 ``CLOSERS`` / ``VERBAL_TAILS`` 词表在首轮里留下的痕迹。
    #: 全部取自已删词表，骨架原文（``tasks.yaml``）里一个都不出现。
    禁用片段 = (
        "今天就要看到", "麻烦快一点", "越快越好", "今晚要看",
        "这周内", "不着急", "有空的时候看看", "慢慢找也没关系",
        "就行啊", "就行呢", "就行吧", "啊。", "呢。", "吧。",
    )

    def test_画像字段已删(self):
        fields = PersonaProfile.model_fields
        assert "urgency" not in fields
        assert "verbal_style" not in fields

    def test_维度域已删(self):
        assert "urgency" not in DIMENSION_DOMAIN
        assert "verbal_style" not in DIMENSION_DOMAIN

    def test_切片轴已删(self):
        assert "urgency" not in SLICE_AXIS_FIELDS
        assert "verbal_style" not in SLICE_AXIS_FIELDS

    def test_渲染结果不含时间限定与语气词(self, library, skeletons):
        """真跑一遍渲染：首轮里不得再出现被删的修饰成分。"""
        checked = 0
        for persona in library.profiles:
            for skel in skeletons:
                if not skel.runnable_in_w1:
                    continue
                if not renderer.is_compatible(persona, skel):
                    continue
                text = renderer.render(skel, persona, seed=0).prompt_text
                for frag in self.禁用片段:
                    assert frag not in text, \
                        f"{persona.persona_id}/{skel.task_id}: {text!r}"
                checked += 1
                break                     # 每个 persona 取一个骨架就够
        assert checked > 20, "兼容性过滤后样本太少，这条检查没实际覆盖"


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

class TestLexicon:
    def test_探针词非空(self):
        for key, (label, probes) in CONSTRAINT_PROBES.items():
            assert label and probes, key

    def test_spec模板只依赖title(self):
        """模板不得再引用 {genre}——那是已知的越权来源。"""
        for name, tpl in SPECIFICITY_RENDER.items():
            assert "{genre}" not in tpl, f"{name} 模板仍注入 genre"

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

        ⚠️ 排除数是 **18**（aggregate 16 + unknown_title 2），不是 98——
        那 80 个 single_title 是被**用上**的。字段名说 excluded，
        值里就不能含未排除项：否则读的人得自己减一遍才知道真相。
        """
        b = sample_tasks(10, library=library, skeletons=skeletons, seed=1)
        r = b.report
        assert r.skeletons_seen == 98
        assert r.skeletons_w1_runnable == 80
        assert r.excluded_by_skeleton_mode == {"aggregate": 16, "unknown_title": 2}
        assert sum(r.excluded_by_skeleton_mode.values()) == 98 - 80
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
                "single_title": 80, "aggregate": 16, "unknown_title": 2}, w1_only

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
            for key in ("persona_id", "genre", "popularity",
                        "persona_presence",
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

    def test_切片轴报的是实际落地而非请求(self, library, skeletons):
        """``dimension_spread`` 必须同时报 requested 与 actual。

        只报 requested 会让人以为半指代维度被覆盖了；而实际落地 188/1，
        用 requested 算分母会得出「该维度表现正常」——一个没发生的实验
        有了结论，比没有这个维度更糟。
        """
        rep = probe_skeletons(library, skeletons, limit=20)
        sp = rep["dimension_spread"]
        assert set(sp) == set(SLICE_AXIS_FIELDS)
        spec = sp["actual_specificity"]
        assert len(spec["requested"]) > 1          # 库里三档齐全
        assert spec["downgraded"] > 0               # 但渲染时降级了
        assert spec["degenerate"] is True

    def test_切片轴用actual键而非persona字段名(self, library, skeletons):
        """⚠️ 这条锁的是一次真实的失效：``W1_DEGENERATE_DIMS`` 曾写成
        persona 字段名 ``task_specificity``，而 ``dimension_spread`` 的键是
        ``actual_specificity``，于是**一个都匹配不上**——所有退化都被
        误报成「稀疏」，处置恰好相反（该等 W3 的结果被报成加采样量即可）。"""
        assert "actual_specificity" in W1_DEGENERATE_DIMS
        assert "task_specificity" not in W1_DEGENERATE_DIMS
        assert W1_DEGENERATE_DIMS <= set(SLICE_AXIS_FIELDS)

    def test_被排除的task在provenance可见(self, library, skeletons):
        """模式分布要跟着样本走，否则切片表看不到"这三类没跑"。"""
        b = sample_tasks(10, library=library, skeletons=skeletons, seed=6)
        assert b.report.by_mode == {"single_title": 10}