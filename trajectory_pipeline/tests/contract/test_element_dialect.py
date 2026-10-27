"""元素方言（P4）的契约测试——**键名是方言的投影，不是随手起的字段名**。

P4 之前，元素的 kind 槽叫 ``tag``、text 槽叫 ``label``，因为唯一的执行层
obscura 给的确实是 HTML 标签名。aria 面给的是 ARIA role，于是名字
统一成 ``{role, name}``，而**值域没有统一**——obscura 面的 ``role`` 槽里
装的仍然是 ``input``/``button``，aria 面装的是 ``textbox``/``button``。
两者不可混池：``input`` 与 ``textbox`` 在两个词表里根本对不上。

⚠️ **三张面、两种方言**（2026-10-26）：``obscura-1`` 是 ``html_tag``；
``camoufox-1``（**已退役**，只读存量）与 ``camofox-1``（**现行生产面**）
**共用** ``aria_role``——换掉的是执行层实现，**不是**目标编码。

本模块守四件事：

1. **一条轴**：``driver`` → 工具面 → 方言。三张表（``SURFACES`` /
   ``DIALECTS`` / ``ELEMENT_KEYS``）必须逐面相等。多一张手工维护的表，
   就多一处会漂的地方，而漂了的症状是「声明与真实调用对不上」——
   报告上看不出异常。
2. **投影而不是改名**：同一份数据按方言写出不同键名，**值原样搬**。
3. **认不出的东西一律抛**：词汇表 / 工具面 / 驱动名。回落 = 猜。
   ——但**已退役的 camoufox 面不抛**：盘上存量产物记着它，删了那个
   代号它们就成了一串读不出来的字符串（**删的是实现，不是证据**）。
4. **键名是方言的投影**：已落盘的 P1/P2 产物里，每条的元素键名都与
   **它自己声明的面**一致——不跨面。这一条同时是**探针**，而它的
   前提换过一次（2026-10-22，见 :class:`Test逐面口径` 的说明）：
   原先是「全库只有 obscura 面」，第一批 camoufox 生产批落盘后那个
   事实不成立；``required`` 随之按真实调用重算（结论：声明值不用改），
   断言改问更强的那一条——**「键名配不配它自己的面」**。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from trajectory_pipeline import toolcatalog
from trajectory_pipeline.assembler import observation_view
from trajectory_pipeline.assembler.builder import samples_dir
from trajectory_pipeline.common.elements import (
    ELEMENT_KEYS,
    ELEMENT_VOCAB_ARIA_ROLE,
    ELEMENT_VOCAB_HTML_TAG,
    ELEMENT_VOCABULARIES,
    element_json,
    element_keys,
    element_name,
    element_role,
    vocabulary_of_archive_elements,
)
from trajectory_pipeline.executor import orchestrator
from trajectory_pipeline.executor.actions import ActionTarget
from trajectory_pipeline.executor.archive import DEFAULT_ROOT
from trajectory_pipeline.perception.base import InteractiveElement, Observation

SURFACES = tuple(toolcatalog.SURFACES)

#: 每个面**不该出现**的参数名。面的键名是互斥的：obscura 面写
#: ``tag``/``label``，两张 aria 面写 ``role``/``name``。混进来的后果是
#: 模型见到一个真实调用里不存在的参数（或反过来，声明漏掉一个真实参数）。
OTHER_SURFACE_KEYS: dict[str, frozenset[str]] = {
    toolcatalog.SURFACE_OBSCURA: frozenset({"role", "name"}),
    toolcatalog.SURFACE_CAMOUFOX: frozenset({"tag", "label"}),
    toolcatalog.SURFACE_CAMOFOX: frozenset({"tag", "label"}),
}


def _obs(vocabulary: str) -> Observation:
    """一份最小观察：一个元素，词表按参数给。"""
    kind = "input" if vocabulary == ELEMENT_VOCAB_HTML_TAG else "textbox"
    return Observation(
        url="https://v.test/",
        page_title="T",
        body_text="正文",
        interactive_elements=(InteractiveElement(ref="e1", role=kind, name="搜索"),),
        element_vocabulary=vocabulary,
    )


def _run_records() -> list[tuple[Path, dict]]:
    """``output/pipeline/`` 整棵子树下的 **P1 run 存档**。

    同目录还有 ``probe_observe*.json`` 这类探针产物（也带 ``driver``），
    它们不是 run 存档——用 ``task_id`` + ``steps`` 两个只有存档才有的键
    把它们排除掉，而不是按文件名过滤（文件名不含 ``probe`` 的探针迟早有）。

    ⚠️ **必须递归**（2026-10-22 补记）：这里原先是 ``glob("*.json")``，
    **只扫顶层**⇒ 子目录下的批次一条都看不见。而批次落子目录是常态
    （``dualrun/{obscura,camoufox}/`` 是双跑、``camoufox_prod1/`` 是
    2026-10-22 的第一批 camoufox 生产批）。

    ⇒ **这个类当时给出的绿是假绿**：第一批 camoufox 批次落进
    ``camoufox_prod1/`` 之后，``test_现有run全部是默认面`` 与
    ``test_现有P1产物里元素键名仍是tag_label`` **依然通过**——
    它们看的是顶层 66 条 obscura 面存档，新批次压根没进视野。
    类文档里那句「一旦有 camoufox 批次落盘，Test存量口径 会红」
    **当时是不成立的**，且不成立的方式正是最坏的一种：**静默**。

    同 :mod:`test_tool_catalog_contract` 的 ``_walk_products`` 一样
    **按 run_id 去重**：``_backup_pre_motivation/`` 里有根目录存档的
    副本，同一个 run 算两次会让「存量条数」虚高，且副本里可能存着
    与主档**不同面**的旧读数。
    """
    out: list[tuple[Path, dict]] = []
    seen: set[str] = set()
    for path in sorted(Path(DEFAULT_ROOT).rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not (isinstance(data, dict) and "task_id" in data and "steps" in data):
            continue
        run_id = str(data.get("run_id") or "").strip()
        if run_id:
            if run_id in seen:
                continue
            seen.add(run_id)
        out.append((path, data))
    return out


def _all_element_dicts(node: object):
    """递归找出所有 ``interactive_elements`` 列表里的元素字典。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "interactive_elements" and isinstance(value, list):
                for el in value:
                    if isinstance(el, dict):
                        yield el
            else:
                yield from _all_element_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _all_element_dicts(item)


# ── 1 · 一条轴 ────────────────────────────────────────────────────


class Test一条轴:
    def test_方言表覆盖全部工具面(self):
        """``DIALECTS`` 与 ``SURFACES`` 的键集必须相等。

        少一个面 = 那个面的元素键名无从确定；多一个面 = 一份没有工具
        声明的方言（P2 按面对这份表取 ``required``，取不到就静默空）。
        """
        assert set(toolcatalog.DIALECTS) == set(toolcatalog.SURFACES)

    @pytest.mark.parametrize("surface", SURFACES)
    def test_方言的键名与元素键表逐面相等(self, surface):
        """``Dialect`` 与 ``ELEMENT_KEYS`` 是同一个轴的两处投影——
        对不上就是两处各说各话。"""
        d = toolcatalog.dialect_of(surface)
        assert d.vocabulary in ELEMENT_VOCABULARIES
        assert ELEMENT_KEYS[d.vocabulary] == (d.kind_key, d.text_key)

    def test_词汇表与键名是字面契约(self):
        """这两个词表名与四个键名会**写进磁盘上的存档**。

        改字面值等于宣告旧存档换了一种读法，而旧存档不会跟着变——
        所以钉死在这里，要改就是一次显式的破坏性变更。
        """
        assert ELEMENT_VOCAB_HTML_TAG == "html_tag"
        assert ELEMENT_VOCAB_ARIA_ROLE == "aria_role"
        assert ELEMENT_KEYS[ELEMENT_VOCAB_HTML_TAG] == ("tag", "label")
        assert ELEMENT_KEYS[ELEMENT_VOCAB_ARIA_ROLE] == ("role", "name")

    def test_方言按词表分组且obscura单独一组(self):
        """**三张面、两种方言**（2026-10-26）。

        原来这条是「每个面的方言互不相同」——camofox 面加进来之后
        它**不再成立**：camofox 与 camoufox **共用** ``aria_role``
        方言（换掉的是执行层实现，**不是**目标编码）。

        但真正要守的不变式是**更强**的那条：**obscura 的方言与所有
        aria 面的方言都不同**。两边混池 = ``input`` 与 ``textbox``
        在两个词表里根本对不上（见模块文档）。

        ⚠️ 两张 aria 面**同方言、不同语义**（上游定位 ``exact`` 与
        子串匹配之别）——那条由 :mod:`test_tool_catalog_contract` 的
        ``test_两张aria面的同名工具说明不同`` 守。
        """
        by_vocab: dict[str, set[str]] = defaultdict(set)
        for surface, d in toolcatalog.DIALECTS.items():
            by_vocab[d.vocabulary].add(surface)
        assert by_vocab[ELEMENT_VOCAB_HTML_TAG] == {toolcatalog.SURFACE_OBSCURA}
        assert by_vocab[ELEMENT_VOCAB_ARIA_ROLE] == {
            toolcatalog.SURFACE_CAMOUFOX,
            toolcatalog.SURFACE_CAMOFOX,
        }
        assert len(by_vocab) == 2

    def test_认不出的面与驱动名都抛(self):
        with pytest.raises(ValueError):
            toolcatalog.dialect_of("camoufox")  # 实现名不是面名
        with pytest.raises(ValueError):
            toolcatalog.surface_of_driver("playwright")
        with pytest.raises(ValueError):
            element_keys("aria")

    def test_退役的camoufox_仍折得出面与方言(self):
        """**退役不等于失读**（2026-10-26）。

        盘上 430 份 P2 样本与 14 份 P1 存档记着 ``camoufox-1``；
        那个代号读不出来 ⇒ 那些产物里逐面校验的断言全成红，
        而它们**没坏**。⇒ 旧面**留在** ``DIALECTS`` 里，
        只是不再由任何驱动产出。
        """
        assert toolcatalog.surface_of_driver("camoufox") == toolcatalog.SURFACE_CAMOUFOX
        assert toolcatalog.surface_of_driver("camofox") == toolcatalog.SURFACE_CAMOFOX
        legacy = toolcatalog.dialect_of(toolcatalog.surface_of_driver("camoufox"))
        assert legacy.vocabulary == ELEMENT_VOCAB_ARIA_ROLE

    def test_空驱动名按默认面(self):
        """空串不是「认不出」，是「没记」——历史批次都是这样，
        而默认面正是它们真实跑出来的面。"""
        assert toolcatalog.surface_of_driver("") == toolcatalog.DEFAULT_SURFACE
        assert toolcatalog.surface_of_driver(None) == toolcatalog.DEFAULT_SURFACE


# ── 2 · 投影 ──────────────────────────────────────────────────────


class Test投影:
    def test_观测落盘按方言写键名(self):
        """``_obs_json`` 是 P1 落盘的唯一出口——两个面各自写出自己的键名。"""
        obscura = orchestrator._obs_json(_obs(ELEMENT_VOCAB_HTML_TAG))
        camoufox = orchestrator._obs_json(_obs(ELEMENT_VOCAB_ARIA_ROLE))
        assert set(obscura["interactive_elements"][0]) == {"ref", "tag", "label"}
        assert set(camoufox["interactive_elements"][0]) == {"ref", "role", "name"}

    def test_值原样搬_不翻译(self):
        """``input`` 不会变成 ``textbox``。

        翻译需要猜 ``<input>`` 的 type（text / submit / checkbox…），
        猜错就是「声明与真实调用对不上」，而报告上看不出异常。
        """
        el = InteractiveElement(ref="e1", role="input", name="搜索")
        assert element_json(el, ELEMENT_VOCAB_HTML_TAG)["tag"] == "input"
        assert element_json(el, ELEMENT_VOCAB_ARIA_ROLE)["role"] == "input"

    def test_动作目标按方言写键名(self):
        t = ActionTarget(role="button", name="立即播放")
        assert t.to_json(ELEMENT_VOCAB_HTML_TAG) == {
            "tag": "button", "label": "立即播放"}
        assert t.to_json(ELEMENT_VOCAB_ARIA_ROLE) == {
            "role": "button", "name": "立即播放"}

    def test_训练态渲染剥掉ref且跟着方言(self):
        """观察渲染（P2/P3 共用）不带 ``ref``——会话句柄进训练数据等于
        教模型背一个随机数；键名仍跟方言走。"""
        html = observation_view.render(orchestrator._obs_json(_obs(ELEMENT_VOCAB_HTML_TAG)))
        aria = observation_view.render(orchestrator._obs_json(_obs(ELEMENT_VOCAB_ARIA_ROLE)))
        assert set(html["interactive_elements"][0]) == {"tag", "label"}
        assert set(aria["interactive_elements"][0]) == {"role", "name"}

    def test_元素行写法逐面不同(self):
        """提示词侧的标记：``<button>`` 与 ``[link]``。

        尖括号在 camoufox 面会被读成一个 HTML 标签（``<link>`` 真实存在），
        而那一列装的是 ARIA role。写法不同也是「新旧样本不混池」的
        提示词侧那一半——光改存档键名、两边提示词长一样，模型看不出差别。
        """
        html = {"ref": "e1", "tag": "button", "label": "播放"}
        aria = {"ref": "e1", "role": "button", "name": "播放"}
        assert observation_view.element_line(html) == "  - <button> 播放"
        assert observation_view.element_line(aria) == "  - [button] 播放"

    def test_存档键名能反推回方言(self):
        """读档层拿不到 ``driver``，只能按键名判方言——这不是第二处
        推断源，键名本来就是那个轴写下的。"""
        assert vocabulary_of_archive_elements(
            [{"ref": "e1", "tag": "a", "label": "x"}]
        ) == ELEMENT_VOCAB_HTML_TAG
        assert vocabulary_of_archive_elements(
            [{"ref": "e1", "role": "link", "name": "x"}]
        ) == ELEMENT_VOCAB_ARIA_ROLE
        # 空清单无从选键名，也只影响「没有东西可渲染」的那一档
        assert vocabulary_of_archive_elements([]) == ELEMENT_VOCAB_HTML_TAG

    def test_两种形状都能读回两个槽(self):
        """值对象与存档字典同一个读取口（``element_role``/``element_name``）。"""
        assert (element_role(InteractiveElement("e1", "a", "x")),
                element_name(InteractiveElement("e1", "a", "x"))) == ("a", "x")
        assert (element_role({"role": "link", "name": "x"}),
                element_name({"role": "link", "name": "x"})) == ("link", "x")
        # 缺槽取空串，不猜
        assert element_role({}) == "" and element_name({}) == ""


# ── 3 · 工具声明逐面 ──────────────────────────────────────────────


class Test工具声明逐面:
    @pytest.mark.parametrize("surface", SURFACES)
    def test_参数名不串面(self, surface):
        """obscura 面不许出现 ``role``/``name`` 参数，camoufox 面不许出现
        ``tag``/``label``——串了就说明有一处是从另一个面抄来的。"""
        banned = OTHER_SURFACE_KEYS[surface]
        for d in toolcatalog.defs_of(surface):
            hit = {p.name for p in d.params} & banned
            assert not hit, f"{d.name}（{surface} 面）混进了别面的参数名: {hit}"

    def test_click的两个槽逐面不同名(self):
        """``click`` 是唯一在各面**同名不同参**的动作——拿错面渲染出来的
        声明与真实调用对不上，而那正是训出「发空参数点击」的那类错。

        ⚠️ 两张 aria 面（camoufox / camofox）**参数相同**——那是**设计**
        （目标编码没换），不是「抄漏了」；它们的差别在**说明**里
        （exact vs 子串匹配），由 ``test_tool_catalog_contract`` 守。
        """
        obscura = {p.name for p in toolcatalog.by_name(
            "click", toolcatalog.SURFACE_OBSCURA).params}
        camoufox = {p.name for p in toolcatalog.by_name(
            "click", toolcatalog.SURFACE_CAMOUFOX).params}
        camofox = {p.name for p in toolcatalog.by_name(
            "click", toolcatalog.SURFACE_CAMOFOX).params}
        assert obscura == {"tag", "label"}
        assert camoufox == {"role", "name"}
        assert camofox == {"role", "name"}


# ── 4 · 逐面口径（同时是探针） ────────────────────────────────────


def _shape(el: dict) -> tuple[str, ...]:
    """元素的键名形状（**去掉 ``ref``**——它是会话句柄，不属方言）。

    P1 的元素带 ``ref``（会话内定位用），P2 落盘时已经剥掉，
    所以比形状时必须把 ``ref`` 排除，否则同一个面会出两种形状。
    """
    return tuple(sorted(k for k in el if k != "ref"))


def _expected_shape(surface: str) -> tuple[str, ...]:
    """该面的元素键名形状（``{kind_key, text_key}``，按字典序）。"""
    d = toolcatalog.dialect_of(surface)
    return tuple(sorted((d.kind_key, d.text_key)))


class Test逐面口径:
    """**每条产物的元素键名必须与它自己声明的面一致。**

    ⚠️ **这一节换过一次口径**（2026-10-22），换的理由记在这儿，别删：

    原来三条断言是「**现有产物全是默认面**」——「全库只有 obscura 面」
    这一条**事实**。第一条 camoufox 生产批（``camoufox_prod1/``）落盘后，
    那个事实**不成立了**，三条一起红。

    按当时的约定（「红了要重算 ``required``，不是把门禁改松」），
    ``required`` **已按新批次的真实调用重算**：camoufox 面 ``click``
    59 次调用 ``{role, name}`` 键名齐备率 100%、非空率 100%，
    ``goto`` / ``new_tab`` 的 ``url`` 100% ⇒ **声明值不需要改**。
    重算完回头看，那三条断言守护的**不变式**其实不是「全是 obscura」，
    而是「**键名是方言的投影**」——后者**更强地成立**了：

        P1 存档 138 条（obscura 121 / camoufox 17）——形状与面不符 **0 例**
        P2 样本 543 条（obscura 361 / camoufox 182）——形状与面不符 **0 例**

    所以改成**逐面**断言：不再问「是不是默认面」，改问「**这条产物
    的键名配不配它自己的面**」。这条比原来那条**更严**——
    原来的断言压根发现不了「camoufox 面的存档写着 obscura 键名」，
    而那正是 CLAUDE.md 点名的失败形态（**拿错面 = 声明与真实调用对不上，
    而报告上看不出异常**）。

    **探针价值不变**：落一个认不出的 driver、或某条产物的键名串了面，
    立刻红。
    """

    def test_每条run的面都认得出(self):
        """面由 P1 的 ``driver`` 一对一折算，**认不出的 driver 直接抛**
        （不猜、不静默回落——见 ``surface_of_driver``）。

        这条守的是「产出侧没绕过折算」：落盘的批次里出现一个认不出的
        driver，说明有人手写了面名。
        """
        records = _run_records()
        if not records:
            pytest.skip("没有真实 P1 存档（output/pipeline/ 是产物目录）")
        for path, data in records:
            surface = toolcatalog.surface_of_driver(data.get("driver"))
            assert surface in toolcatalog.SURFACES, (
                f"{path.name} 的 driver={data.get('driver')!r} 折出未知面 "
                f"{surface!r}"
            )

    def test_P1产物里元素键名跟着自己的面(self):
        """P1：键名形状 == **该存档自己**的面的方言。"""
        records = _run_records()
        if not records:
            pytest.skip("没有真实 P1 存档（output/pipeline/ 是产物目录）")
        for path, data in records:
            surface = toolcatalog.surface_of_driver(data.get("driver"))
            expected = _expected_shape(surface)
            for el in _all_element_dicts(data):
                assert _shape(el) == expected, (
                    f"{path.name} 是 {surface} 面，元素却是 {_shape(el)} 形状："
                    f"{el}"
                )

    def test_P2样本里元素键名跟着自己的面(self):
        """P2：键名形状 == **该样本自己**的 ``tool_surface`` 的方言。

        ⚠️ 没记 ``tool_surface`` 的样本（2026-10-20 之前建的）算默认面——
        那批样本真实跑出来的就是它，不是「缺信息就往新面上靠」。
        """
        files = sorted(samples_dir().glob("*.json"))
        if not files:
            pytest.skip("没有 P2 样本（output/pipeline/samples/ 是产物目录）")
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            surface = str(data.get("tool_surface") or "") or toolcatalog.DEFAULT_SURFACE
            expected = _expected_shape(surface)
            for el in _all_element_dicts(data):
                assert _shape(el) == expected, (
                    f"{path.name} 是 {surface} 面，元素却是 {_shape(el)} 形状："
                    f"{el}"
                )