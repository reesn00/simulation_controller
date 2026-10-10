"""代码断言层评分器 —— **D-6「记录字段完整性」**（样本级）。

## 判据

rubric v1.1 D-6 的必填四项，逐字段核**存在性 + 与观察一致性**：

======================  ==========================================
必填字段                数据源
======================  ==========================================
站点 URL                 ``OutcomeRef.site_url``
播放页 URL               ``OutcomeRef.play_page_url``
目标内容标识（**片名**）  ``Sample.title`` × 观察里的片名
验证证据                 P1 的 ``outcome.evidence``
======================  ==========================================

## 三条不能含糊的地方

**1. 「未到达播放页」时播放页 URL 必然为空，此时该项不适用、不扣分。**
rubric 边界规则原文。这一条不写清楚，评分器会系统性惩罚
``no_play_control`` / ``login_wall_or_blocked`` 那些**根本走不到播放页**
的站点——而它们恰恰是**正确**的负样本。

**2. 验证证据在 P1，样本里不复制。**
:mod:`trajectory_pipeline.assembler.schema` 的 ``OutcomeRef`` 刻意不带
``evidence``（带了就产生第二份需要同步的副本，复核后 P1 更新、样本没重建
就是**静默失配**）。所以本模块**判不了**证据质量，只能核
「P1 的 outcome 存在且带非空 evidence」——而那要读 P1。
故：本模块对证据项输出 **``skipped``** 并说明原因，
**不猜、不用 ``evidence`` 的长度当代理**。

**3. 「片名与观察一致」的可判定性有边界。**
搜索结果页/站点页的正文里**不一定**出现片名（真实形态：西瓜视频那类
站点页正文 100 字符、全是导航文案）。观察里没有片名时判
「不可判定」而不是「不一致」——与 D-2 的 ``undetermined`` 同源。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from trajectory_pipeline.assembler.schema import Sample

#: 判定结果类型。
VerdictKind = Literal["scored", "undetermined"]

#: 单个字段的核对结果。
FieldKind = Literal["ok", "missing", "wrong", "undetermined", "not_applicable"]


@dataclass(frozen=True, slots=True)
class FieldCheck:
    """一个必填字段的核对结果。``why`` 必须写清依据。"""

    field: str
    kind: FieldKind
    why: str

    def to_json(self) -> dict[str, Any]:
        return {"field": self.field, "kind": self.kind, "why": self.why}


@dataclass(frozen=True, slots=True)
class D6Verdict:
    """D-6 的判定结果。"""

    score: int | None
    kind: VerdictKind
    fields: tuple[FieldCheck, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def missing_fields(self) -> tuple[str, ...]:
        return tuple(f.field for f in self.fields if f.kind == "missing")

    def to_json(self) -> dict[str, Any]:
        return {"dimension": "D-6", "score": self.score, "kind": self.kind,
                "fields": [f.to_json() for f in self.fields],
                "reasons": list(self.reasons)}


# ── 取值（dataclass 与 P2 字典都吃）─────────────────────────────


def _get(obj: Any, key: str, default: Any = "") -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _sample_field(sample: Any, key: str, default: Any = "") -> Any:
    """读样本顶层字段。``six.*`` 里的先在 ``six`` 里找。"""
    if isinstance(sample, Mapping):
        six = sample.get("six") or {}
        return sample.get(key, six.get(key, default))
    return getattr(sample, key, default)


def _outcome_of(sample: Any) -> Any:
    if isinstance(sample, Mapping):
        return sample.get("outcome")
    return getattr(sample, "outcome", None)


def _p1_missing(outcome: Any) -> tuple[str, ...]:
    """P1 的 outcome 里**压根没有**哪些键。逐条读 :attr:`OutcomeRef.p1_missing_fields`。

    不能靠 ``_has_key`` 判断——那层信息在 P2 的 ``to_json`` 里已经丢了
    （缺字段与空值同形）。:mod:`trajectory_pipeline.assembler.schema`
    刻意把它单独记下来，就是为了这里。
    """
    if outcome is None:
        return ()
    raw = _get(outcome, "p1_missing_fields", ())
    return tuple(str(x) for x in (raw or ()))


def _observations(sample: Any) -> list[Mapping[str, Any]]:
    six = _sample_field(sample, "observations", ())
    if not six:
        return []
    # six.observations 可能是「训练态视图的 list」也可能是「原始 dict」
    return [o for o in six if isinstance(o, Mapping)]


# ── 逐字段核对 ────────────────────────────────────────────────────


def _check_site_url(outcome: Any) -> FieldCheck:
    """站点 URL。**恒有值**——候选链接在搜索阶段就定了，
    后面无论走到哪一步都拿得出来。"""
    url = str(_get(outcome, "site_url", "") or "")
    if url:
        return FieldCheck("site_url", "ok", f"站点 URL 齐全：{url[:60]}")
    if "site_url" in _p1_missing(outcome):
        # 老存档压根没这个键（2026-10-10 才补）。判「缺失」是把
        # **采集器缺字段**记成**轨迹字段不全**——实测那样判出 20 条假 3 分。
        fallback = str(_get(outcome, "url", "") or "")
        return FieldCheck(
            "site_url", "undetermined",
            f"老存档的 outcome 没有 site_url 这个键（字段 2026-10-10 才补），"
            f"当前用 outcome.url 顶替：{fallback[:40]}——"
            f"「站点 URL 与播放页 URL 分离」这条 rubric 要点无法核对，"
            f"先重跑一批再判",
        )
    fallback = str(_get(outcome, "url", "") or "")
    if fallback:
        return FieldCheck(
            "site_url", "wrong",
            f"site_url 存在但为空，而 outcome.url={fallback[:40]}——"
            f"两者应当一致（P1 的 record 调用点都传 site_url）",
        )
    return FieldCheck("site_url", "missing", "既没有 site_url 也没有 outcome.url")


def _check_play_page_url(outcome: Any) -> FieldCheck:
    """播放页 URL。**未到播放页时必然为空，此时不适用、不扣分。**

    这是 rubric 边界规则的直接落地——不写清楚的话，
    评分器会系统性惩罚那些**根本走不到播放页**的负样本，
    而它们恰恰是正确的结论。
    """
    url = str(_get(outcome, "play_page_url", "") or "")
    if url:
        return FieldCheck("play_page_url", "ok",
                          f"播放页 URL 齐全：{url[:60]}")
    reached = bool(_get(outcome, "reached_play_page", False))
    if not reached:
        return FieldCheck(
            "play_page_url", "not_applicable",
            "未到达播放页——该项按 rubric D-6 边界规则不适用、不扣分",
        )
    if "play_page_url" in _p1_missing(outcome):
        return FieldCheck(
            "play_page_url", "undetermined",
            "老存档的 outcome 没有 play_page_url 这个键（字段 2026-10-10 才补）"
            "——无法判断当时有没有落盘，先重跑一批再判",
        )
    # 到了播放页、键也在、但值为空 = 真缺失。
    return FieldCheck(
        "play_page_url", "missing",
        "标记 reached_play_page=True 却没有 play_page_url——"
        "rubric D-6 反例：「只有一条 URL 承担站点页与播放页两种语义」",
    )


def _check_title(sample: Any, observations: list[Mapping[str, Any]]) -> FieldCheck:
    """片名。**存在性 + 与观察一致**两件事。

    「一致」判不了的情况：观察正文里根本没出现片名。
    真实形态（实测）——站点页正文 100 字符、全是导航文案，
    片名压根不在观察里。那是**采集**的事实，不是「值不符」。
    """
    title = str(_sample_field(sample, "title", "") or "")
    if not title:
        return FieldCheck("title", "missing", "没有 title（目标内容标识）")

    if not observations:
        return FieldCheck("title", "undetermined", "本样本没有观察，无法核对与片名的一致性")

    # 正文里出现了片名（去掉引号与书名号这类包装）即视为一致。
    needle = title.strip("《》〈〉\"'")
    if not needle:
        return FieldCheck("title", "ok", "片名为空包装字符，跳过一致性核对")
    for obs in observations:
        body = str(obs.get("body_text") or "")
        page_title = str(obs.get("page_title") or "")
        if needle and (needle in body or needle in page_title):
            return FieldCheck("title", "ok",
                              f"片名「{title}」在观察里可检索到")
    # 全都查不到：可能是「不符」，也可能是「观察里根本没提」。
    # 区分靠**是否有任何一条观察带正文**——都没有正文就无从核查。
    bodies = [o for o in observations if str(o.get("body_text") or "").strip()]
    if not bodies:
        return FieldCheck(
            "title", "undetermined",
            "所有观察都没有正文，片名一致性无从核查（采集降级）")
    return FieldCheck(
        "title", "undetermined",
        f"片名「{title}」在 {len(bodies)} 条观察的正文里都检索不到——"
        f"可能是值不符，也可能是站点页正文压根没提片名，"
        f"**两者在存档里同形**，需人工看一眼",
    )


def _check_evidence(outcome: Any) -> FieldCheck:
    """验证证据。

    ⚠️ **P2 刻意不复制 evidence 原文**（见 :class:`OutcomeRef` 的 docstring：
    复制就产生第二份需要同步的副本，复核后 P1 更新、样本没重建就是静默失配）。
    所以本模块**判不了证据质量**，也不能拿长度当代理——
    那会把「引用了观察原文」与「写了一句话」混成同一个分数。

    非空由 :mod:`trajectory_pipeline.executor.integrity` 的
    ``evidence_empty``（**FATAL**）把守——那是批次级门禁，位置对。
    """
    return FieldCheck(
        "evidence", "skipped",
        "验证证据的原文在 P1 的 outcome.evidence 里，P2 不复制它"
        "（避免复核后 P1 更新而样本没更新的静默失配）——"
        "非空由 check-archives 的 evidence_empty（FATAL）把关",
    )


# ── 打分 ──────────────────────────────────────────────────────────


def score_d6(sample: Sample | Any) -> D6Verdict:
    """D-6 打分。

    锚点（rubric v1.1 D-6）::

        5 = 齐全正确
        4 = 缺 1 可选字段或证据含糊
        3 = 缺 1 必填 或 1 字段值与观察不符
        2 = 缺 ≥2 必填
        1 = 大面积缺失/虚构

    **只有「有正文且查不到」才可能判 3 分的「值不符」**——
    而本模块把它降为 ``undetermined``（理由见 :func:`_check_title`：
    「值不符」与「观察里压根没提」在存档里同形，判错就是在
    把采集器的取舍记成轨迹的缺陷）。所以本模块实际输出的
    低分档只有 2 / 3 两档，且 3 档只来自「缺 1 必填」。
    """
    outcome = _outcome_of(sample)
    if outcome is None:
        # 搜索单元本来就没有 outcome——它没「记录」可核。
        # 判 2 分（缺 ≥2 必填）是把「这一条不适用」当成「这一条不合格」，
        # 而 D-5 早就处理过同一件事（判 undetermined）。
        return D6Verdict(None, "undetermined", (), (
            "本条样本没有 outcome（搜索阶段不记站点级结论）——"
            "D-6 核的是「记录」的字段完整性，此处无记录可核",
        ))

    observations = _observations(sample)

    fields = (
        _check_site_url(outcome),
        _check_play_page_url(outcome),
        _check_title(sample, observations),
        _check_evidence(outcome),
    )

    missing = [f for f in fields if f.kind == "missing"]
    # ⚠️ **``skipped`` 不阻断出分**：它是**已知的、不依赖数据**的边界
    # （验证证据的原文在 P1，由 ``check-archives`` 的 ``evidence_empty``
    # 把关），不是「这一条查不了」。把它算进不可判定，
    # 于是**任何样本都不可能判 5 分**——而 D-6 的 5 分锚点原文是
    # 「齐全正确」，evidence 非空本就在 D-6 的管辖之外。
    unresolved = [f for f in fields if f.kind == "undetermined"]
    reasons = list(dict.fromkeys(f.why for f in fields
                                 if f.kind in ("undetermined", "skipped")))

    if len(missing) >= 2:
        return D6Verdict(2, "scored", fields, tuple(reasons))
    if len(missing) == 1:
        return D6Verdict(3, "scored", fields, tuple(reasons))

    # 一个必填都不缺。**仍可能有不可判定项**——那不是「不齐」，
    # 是「齐但有两项没法核」。两者后续动作不同：前者补数据，
    # 后者改采集口径。所以不出 5 分。
    if unresolved:
        return D6Verdict(None, "undetermined", fields, tuple(reasons + [
            f"{len(unresolved)}/{len(fields)} 个必填项不可核——"
            "本维不出分（先修采集，不要先扣轨迹的分）",
        ]))
    return D6Verdict(5, "scored", fields, tuple(reasons))


__all__ = [
    "VerdictKind", "FieldKind", "FieldCheck", "D6Verdict", "score_d6",
]
