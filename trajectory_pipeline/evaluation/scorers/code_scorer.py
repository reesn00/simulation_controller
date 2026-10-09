"""代码断言层评分器——rubric 的 D-2「观察-动作一致性」。

## 为什么第一个落地的是 D-2

D-2 是 rubric v1.1 里**唯一一条**「不补数据就必然产生反向错判」的维度。
存档侧把交互元素裁到 80 条（``orchestrator.OBS_ELEMENT_LIMIT``），
于是「目标确实不在这一页」与「目标恰好落在裁剪区外」在数据上**完全同形**——
两种相反的结论共用同一个证据。变更 B 补的 ``elements_total`` /
``links_total`` 就是为了让这两种情况可分，而**分不开就写不出这个评分器**。

顺带说明为什么 D-2 属于**代码层**而不是 LLM 层：「这个 label 在不在这页」
是检索题，不是理解题。交给 LLM 只会让它把两种情况都答成「没找到」，
也就是把确定性缺陷重新变成不确定性。

## 本轮只做 D-2，不做的

- ``goto`` 的 URL 引用**不在本模块校验**。它要核的是「这个 URL 在搜索页
  的链接里吗」，而搜索页观察属于**另一条样本**（决策单元）——跨样本比对
  是批次级断言的事（D-7 / B-2 的口径）。在本样本内查它必然查不到，
  于是每条 ``goto`` 都记一条「引用不存在的实体」——**全批恒 1 分**。
  恒定分数没有区分力（这条与 B-1 被划到批次级是同一个理由）。
- D-5 / D-9 等需要 ground truth 或人工裁定的维度、批次级断言 B-1..B-4：
  都不在本模块。它们的管辖层与数据源各不相同，混进一个类里只会让
  「哪个维度用了哪些输入」这件事不可查。

## 三态而非两态

:attr:`D2Verdict.score` 取 ``None`` 表示**不可判定**，与「5 分」严格分开。
两个方向都不能含糊：

- 没做成「不可判定」→ 把采集器的取舍记成轨迹的缺陷（v1.1 明确禁止）；
- 没做成「5 分」→ 一条没有任何 ``click`` 的样本（只导航就被拦下）会拿到满分，
  而它其实**什么都没可查**。这与 ``gate.status`` 用三值不用两值同源：
  「没跑」渲染成「通过」会让整批数据看起来比实际干净。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

from trajectory_pipeline.assembler.schema import Sample

#: 一次引用（一个 click 目标）的判定结果。
RefKind = Literal["found", "ambiguous", "missing", "undetermined"]

#: 整条样本的判定结果。``undetermined`` 与 ``scored`` 分开，不共用 ``score=None``。
VerdictKind = Literal["scored", "undetermined"]

#: 存档侧对 label 的截断长度（``orchestrator._obs_json``）。
#: 超过它的 label 在存档里是**前缀**，比对必须走前缀匹配——
#: 走全等匹配会把一条真实存在的引用判成「不存在」。
LABEL_TRUNCATED_AT = 120


@dataclass(frozen=True, slots=True)
class RefFinding:
    """一次 ``click`` 引用的判定。

    ``reason`` 必须写清**依据**：rubric 的 D-2 备注说「不可判定项越多，
    说明采集越该改，而不是轨迹越该扣分」——而读报告的人只有看到理由
    才知道该去改采集器的哪一处。
    """

    kind: RefKind
    label: str
    tag: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "tag": self.tag,
                "reason": self.reason}


@dataclass(frozen=True, slots=True)
class D2Verdict:
    """D-2 的判定结果。``score`` 为 ``None`` 时 ``kind`` 必为 ``undetermined``。"""

    score: int | None
    kind: VerdictKind
    refs: tuple[RefFinding, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def checked(self) -> int:
        """实际做了检索的引用数。``undetermined`` 不计入。"""
        return sum(1 for r in self.refs if r.kind in ("found", "ambiguous", "missing"))

    def to_json(self) -> dict[str, Any]:
        return {"dimension": "D-2", "score": self.score, "kind": self.kind,
                "checked": self.checked, "refs": [r.to_json() for r in self.refs],
                "reasons": list(self.reasons)}


# ═══════════════════════════════════════════════════════════════════════
# 单条引用
# ═══════════════════════════════════════════════════════════════════════


def _label_of(action: Any) -> str:
    """动作的点击目标 label。兼容 ``ActionView``（dataclass）与存档字典。"""
    target = getattr(action, "target", None)
    if target is None and isinstance(action, Mapping):
        target = action.get("target")
    if target is None:
        return ""
    label = (getattr(target, "label", None) if not isinstance(target, Mapping)
             else target.get("label"))
    return str(label or "")


def _tag_of(action: Any) -> str:
    target = getattr(action, "target", None)
    if target is None and isinstance(action, Mapping):
        target = action.get("target")
    if target is None:
        return ""
    tag = (getattr(target, "tag", None) if not isinstance(target, Mapping)
           else target.get("tag"))
    return str(tag or "")


def _click_actions(sample: Any) -> list[Any]:
    """样本里模型可见的 ``click`` 动作。

    只取 ``origin == "model"``：``new_tab`` 是执行器的会话隔离动作，
    模型永不输出它（见 ``executor/actions.py``），把它算进模型动作
    等于拿执行器的行为去扣模型的分。
    """
    out = []
    for act in getattr(sample, "actions", ()) or ():
        tool = getattr(act, "tool", None)
        if tool is None and isinstance(act, Mapping):
            tool = act.get("tool")
        if tool != "click":
            continue
        origin = getattr(act, "origin", "model")
        if origin is None and isinstance(act, Mapping):
            origin = act.get("origin", "model")
        if origin != "model":
            continue
        out.append(act)
    return out


def _observation_shortfall(obs: Mapping[str, Any]) -> str:
    """这份观察**能不能支撑**「某个元素不存在」的结论。

    返回空串表示能支撑；非空则是不可判定的**具体原因**。

    四种情形，缺一样就会判反：

    1. ``degraded`` 非空——驱动层自己说了「这一项没采到」。iqiyi/ixigua/sohu
       三站的实测形态是 ``body_len=0`` + 元素 0 个，采集失败与「页面真没有」
       在存档里同形（见 ``tests/unit/test_obscura_driver.py`` 的模块 docstring）。
    2. ``truncated``——正文被 snapshot 预算截断，元素可能落在截断外。
    3. 元素/链接**裁剪**（``elements_total >`` 保留条数）——这是变更 B
       专门补的：没有分母时「裁掉了」与「不存在」不可分。
    4. 观察整个为空（``body_text`` 与元素、链接全空）——可能是采集失败。
    """
    if obs.get("degraded"):
        return f"观察有降级项 {list(obs['degraded'])}，缺项无法与「真没有」区分"
    if obs.get("truncated"):
        return f"观察被截断（{obs.get('raw_len')}/{obs.get('max_chars')}），" \
               "元素可能落在截断外"

    elements = list(obs.get("interactive_elements") or ())
    links = list(obs.get("links") or ())
    elements_total = int(obs.get("elements_total") or 0) or len(elements)
    links_total = int(obs.get("links_total") or 0) or len(links)
    if elements_total > len(elements):
        return (f"交互元素被裁剪（存档保留 {len(elements)}，"
                f"采到 {elements_total}），目标可能落在裁剪区外")
    if links_total > len(links):
        return (f"链接被裁剪（存档保留 {len(links)}，采到 {links_total}），"
                "目标可能落在裁剪区外")
    if not elements and not links and not str(obs.get("body_text") or "").strip():
        return "观察三项全空，采集可能整体失败"
    return ""


def _matches(label: str, elements: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """按 label 检索交互元素。超长 label 走**截断后全等**。

    方向容易搞反：动作里的 label 来自内存里的 DOM 元素（``actions.target_of``
    取的是 ``el.label``，**完整**），而存档里只留前 120 字
    （``orchestrator._obs_json`` 的 ``label[:120]``）。所以是**存档那侧短**，
    要拿动作 label 的前 120 字去比，不是拿存档那侧去前缀匹配动作 label。
    """
    needle = label if len(label) <= LABEL_TRUNCATED_AT \
        else label[:LABEL_TRUNCATED_AT]
    return [e for e in elements if str(e.get("label") or "") == needle]


def judge_ref(label: str, tag: str,
              observations: Sequence[Mapping[str, Any]]) -> RefFinding:
    """判一次引用在观察存档里能不能找到。

    顺序刻意是**先看有没有完整观察能定案，再看不可判定的理由**——
    一次 site_obs 完整而 player_obs 被裁时，裁剪**不能**把结论拖成
    不可判定：完整的那一份已经能定案了。

    偏松的方向也是刻意的：任一份观察里命中就算命中（不去比「哪一份
    是这次点击的前一观察」）。这会放过一些本该判 `missing` 的引用，
    但绝不会把真实存在的引用判成不存在——而这个模块存在的目的正是
    **不判反**。代价是一部分假阴性，记在这里而不是留给后来人踩。
    """
    if not label:
        # 执行器自己没解出目标（I6 存疑，`target=None`）。那是**存档完整性**
        # 的问题，不是轨迹引用了不存在的东西——两者方向相反。
        return RefFinding("undetermined", label, tag,
                          "动作没有语义目标（target=None）：ref 未溯源到 "
                          "interactive_elements，按存档完整性问题处理")
    if not observations:
        return RefFinding("undetermined", label, tag, "本样本没有观察存档")

    hits: list[Mapping[str, Any]] = []
    seen_refs: set[tuple[str, str]] = set()
    blocking: list[str] = []
    settled_by: str = ""
    for obs in observations:
        found = _matches(label, list(obs.get("interactive_elements") or ()))
        if found:
            # **按 ref 去重**再计数量：站点页与播放页常有同一个控件
            # （点击前后快照里 `ref=e5` 都在），不去重会把「命中一条」
            # 数成两条同名 → 误判含糊（4 分 / 3 分）。
            for el in found:
                key = (str(el.get("ref") or ""), str(el.get("label") or ""))
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                hits.append(el)
            continue
        shortfall = _observation_shortfall(obs)
        if shortfall:
            blocking.append(f"{obs.get('url') or '?'}：{shortfall}")
        elif not settled_by:
            # 这份观察**完整**且确实没有它——定案成立
            settled_by = str(obs.get("url") or "?")

    if len(hits) >= 2:
        # 两条以上同名元素，而动作里**只有 label 没有 ref**（ref 是会话内句柄，
        # 按 `ActionView` 的设计刻意不进训练数据）→ 模型无从指明是哪一条。
        return RefFinding(
            "ambiguous", label, tag,
            f"{len(hits)} 个元素同名，样本里不带 ref，模型无从指明是哪一条",
        )
    if len(hits) == 1:
        hit_tag = str(hits[0].get("tag") or "")
        note = "" if hit_tag == tag else f"（动作记 tag={tag or '空'}，" \
                                          f"观察里是 {hit_tag or '空'}）"
        return RefFinding("found", label, tag, f"命中 {len(hits)} 个元素{note}")
    if settled_by:
        reason = f"{settled_by} 是完整采集且无同名元素"
        if blocking:
            # 有别的观察被裁/降级过——**结论不变**，但那几份观察没能参与
            # 作证。不写出来的话，读报告的人会以为这条是「全查过了」。
            reason += "；另有观察未能作证：" + "；".join(blocking)
        return RefFinding("missing", label, tag, reason)
    # 每一份没命中的观察都带着自己的短板——没有能定案的那一份。
    # v1.1 边界规则：这种情况判「不可判定（存档裁剪 / 采集降级）」，
    # **不判**「引用了不存在的实体」——判后者等于把采集器的取舍
    # 记成轨迹的缺陷。
    return RefFinding("undetermined", label, tag, "；".join(blocking))


# ═══════════════════════════════════════════════════════════════════════
# 打分
# ═══════════════════════════════════════════════════════════════════════


def score_d2(sample: Sample | Any) -> D2Verdict:
    """D-2 打分。输入是一条 P2 样本（``assembler.schema.Sample``）。

    锚点（rubric v1.1 D-2）::

        5 = 全部实体可找到
        4 = 1 处含糊引用
        3 = 2 处含糊引用
        2 = 1 处引用不存在的实体
        1 = ≥2 处引用不存在的实体

    **「不存在」优先于「含糊」**：同一批里既有一条真缺失又有两条同名时，
    报 3 分会让那条真缺失消失，而它是唯一一条需要人去查的。
    """
    actions = _click_actions(sample)
    if not actions:
        return D2Verdict(None, "undetermined", (),
                         ("本条样本没有模型可见的 click 动作，无引用可校验；"
                          "「没有可校验项」不等于「全部可找到」"))

    observations = list(getattr(sample, "observations", ()) or ())
    refs = tuple(judge_ref(_label_of(a), _tag_of(a), observations) for a in actions)

    missing = [r for r in refs if r.kind == "missing"]
    ambiguous = [r for r in refs if r.kind == "ambiguous"]
    undetermined = [r for r in refs if r.kind == "undetermined"]

    if missing:
        score = 1 if len(missing) >= 2 else 2
    elif ambiguous:
        score = 3 if len(ambiguous) >= 2 else 4
    else:
        score = 5

    reasons = undetermined_ref_reasons(undetermined)
    if undetermined and not missing and not ambiguous:
        # 只要**存在**一条不可判定，本条的 5 分就不是「全部实体可找到」——
        # 它是「查过的都找到了，另有一条查不了」。两者的后续动作不同：
        # 前者可以进训练集，后者要先修采集器。
        return D2Verdict(None, "undetermined", refs, tuple(reasons + [
            f"{len(undetermined)}/{len(refs)} 处引用不可判定，"
            "本维不出分——先修采集，不要先扣轨迹的分",
        ]))
    return D2Verdict(score, "scored", refs, tuple(reasons))


def undetermined_ref_reasons(refs: Sequence[RefFinding]) -> list[str]:
    """不可判定项的理由列表。按出现次序去重。"""
    return list(dict.fromkeys(r.reason for r in refs if r.kind == "undetermined"))


__all__ = [
    "RefKind", "VerdictKind", "RefFinding", "D2Verdict",
    "LABEL_TRUNCATED_AT", "judge_ref", "score_d2", "undetermined_ref_reasons",
]