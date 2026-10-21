"""元素方言（P4）的契约测试——**键名是方言的投影，不是随手起的字段名**。

P4 之前，元素的 kind 槽叫 ``tag``、text 槽叫 ``label``，因为唯一的执行层
obscura 给的确实是 HTML 标签名。camoufox 面给的是 ARIA role，于是名字
统一成 ``{role, name}``，而**值域没有统一**——obscura 面的 ``role`` 槽里
装的仍然是 ``input``/``button``，camoufox 面装的是 ``textbox``/``button``。
两者不可混池：``input`` 与 ``textbox`` 在两个词表里根本对不上。

本模块守四件事：

1. **一条轴**：``driver`` → 工具面 → 方言。三张表（``SURFACES`` /
   ``DIALECTS`` / ``ELEMENT_KEYS``）必须逐面相等。多一张手工维护的表，
   就多一处会漂的地方，而漂了的症状是「声明与真实调用对不上」——
   报告上看不出异常。
2. **投影而不是改名**：同一份数据按方言写出不同键名，**值原样搬**。
3. **认不出的东西一律抛**：词汇表 / 工具面 / 驱动名。回落 = 猜。
4. **老存档仍是老键名**：已落盘的 P1/P2 产物里没有一条 ``{role, name}``。
   这一条同时是**探针**：它的前提是「现有批次全是 obscura 面」，一旦
   有 camoufox 批次落盘，:class:`Test存量口径` 会红——那时候要做的
   不是改断言，是重算 ``toolcatalog`` 里 camoufox 面的 ``required``
   （见该类最后一条测试的提示）。
"""

from __future__ import annotations

import json
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

#: 两个面各自**不该出现**的参数名。面的键名是互斥的：obscura 面写
#: ``tag``/``label``，camoufox 面写 ``role``/``name``。混进来的后果是
#: 模型见到一个真实调用里不存在的参数（或反过来，声明漏掉一个真实参数）。
OTHER_SURFACE_KEYS: dict[str, frozenset[str]] = {
    toolcatalog.SURFACE_OBSCURA: frozenset({"role", "name"}),
    toolcatalog.SURFACE_CAMOUFOX: frozenset({"tag", "label"}),
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
    """``output/pipeline/`` 下的**P1 run 存档**。

    同目录还有 ``probe_observe*.json`` 这类探针产物（也带 ``driver``），
    它们不是 run 存档——用 ``task_id`` + ``steps`` 两个只有存档才有的键
    把它们排除掉，而不是按文件名过滤（文件名不含 ``probe`` 的探针迟早有）。
    """
    out: list[tuple[Path, dict]] = []
    for path in sorted(Path(DEFAULT_ROOT).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "task_id" in data and "steps" in data:
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

    def test_每个面的方言互不相同(self):
        """两面共用一套键名 = 面这个维度消失了。"""
        triples = [
            (d.vocabulary, d.kind_key, d.text_key)
            for d in toolcatalog.DIALECTS.values()
        ]
        assert len(set(triples)) == len(triples)

    def test_认不出的面与驱动名都抛(self):
        with pytest.raises(ValueError):
            toolcatalog.dialect_of("camoufox")  # 实现名不是面名
        with pytest.raises(ValueError):
            toolcatalog.surface_of_driver("playwright")
        with pytest.raises(ValueError):
            element_keys("aria")

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
        声明与真实调用对不上，而那正是训出「发空参数点击」的那类错。"""
        obscura = {p.name for p in toolcatalog.by_name(
            "click", toolcatalog.SURFACE_OBSCURA).params}
        camoufox = {p.name for p in toolcatalog.by_name(
            "click", toolcatalog.SURFACE_CAMOUFOX).params}
        assert obscura == {"tag", "label"}
        assert camoufox == {"role", "name"}


# ── 4 · 存量口径（同时是探针） ────────────────────────────────────


class Test存量口径:
    def test_现有run全部是默认面(self):
        """下面两条断言的前提：已落盘的批次全来自默认面。

        **这条红了不是改断言了事**：它意味着有 camoufox 批次落盘了，
        于是「老键名」不再是全部产物的事实，而 ``toolcatalog`` 里
        camoufox 面的 ``required`` 也该按真实调用重算了（此前没有数据，
        一直只能沿用声明值）。
        """
        records = _run_records()
        if not records:
            pytest.skip("没有真实 P1 存档（output/pipeline/ 是产物目录）")
        for path, data in records:
            surface = toolcatalog.surface_of_driver(data.get("driver"))
            assert surface == toolcatalog.DEFAULT_SURFACE, (
                f"{path.name} 是 {surface} 面——有 camoufox 批次落盘了，"
                f"该重算 catalog 里 camoufox 面的 required，再改这里的口径"
            )

    def test_现有P1产物里元素键名仍是tag_label(self):
        records = _run_records()
        if not records:
            pytest.skip("没有真实 P1 存档（output/pipeline/ 是产物目录）")
        for path, data in records:
            for el in _all_element_dicts(data):
                assert "role" not in el and "name" not in el, (
                    f"{path.name} 里出现了 {{role, name}} 形状的元素：{el}"
                )

    def test_现有P2样本里元素键名仍是tag_label(self):
        files = sorted(samples_dir().glob("*.json"))
        if not files:
            pytest.skip("没有 P2 样本（output/pipeline/samples/ 是产物目录）")
        for path in files:
            data = json.loads(path.read_text(encoding="utf-8"))
            for el in _all_element_dicts(data):
                assert "role" not in el and "name" not in el, (
                    f"{path.name} 的 P2 里出现了 {{role, name}} 形状的元素：{el}"
                )