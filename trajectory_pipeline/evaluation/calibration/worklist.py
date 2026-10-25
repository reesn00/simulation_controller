"""首轮分诊 worklist（方案 16 §9.3 / C-06）。

一句话：把「33 张卡摊在桌上等人一条条读」变成「每张卡先贴上**机器能确认的
事实**与**至多一条建议**，人只确认或改」——**建议不是裁定**。

三层东西，别混
--------------
===========================  ==============================================
``disagreements.jsonl``      派生：卡。只说「这一行两侧不一致」（C-02）
``worklist.jsonl``           **派生**：卡 + 机器事实 + 建议（本模块）
``worksheet.jsonl``          **表单**：人填的那一份（本模块顺手产出）
``data/triage/triage.jsonl`` **裁定**：人签名的桶（``triage`` 模块，入库）
===========================  ==============================================

所以本模块**一行都不写分诊档**：``bucket`` 的唯一写入口仍是
:func:`triage.validate_rows`，机器身份在那里被拒（D-C3）。人填完工作表后走
``triage import``——签名是人给的，机器只提供了建议。

读现场（``show`` 与工作表的 ``scene`` 栏）
-----------------------------------------
工作表给的是「结论 + 一句话证据」，**不是现场**——D-10 的判据原文是「对照
黄金集**预存档**的站点核验结论」，而预存档不在表里。两条路都补上这一层：

- ``worklist build`` 把**现场读数直接写进工作表的 ``scene`` 栏**（页面标题 /
  每步观察读数 / 正文预览）——人填的那张表里就有现场，不必为看一条现场去开
  第二个文件、跑第二条命令；
- ``worklist show`` 把同一批读数连同 **rubric 条款原文**摊到终端（逐条查）
  ——尺子另外落一份 ``v{N}/clauses.md``，因为**尺子是同一把**（D-4 / D-5 /
  各卡自己的条款），每行各带一份会让同一个文件长 N 倍。

两处都只读：``bucket`` 的唯一写入口仍是 ``triage`` 模块。


为什么建议要单独一列（而不是直接填 ``bucket``）
-----------------------------------------------
1. §4 的桶是**结论的标签**，而每个标签的定义里都带前提。机器能给的是前提里的
   **可观测事实**（采集器是否声明降级 / pred 是不是规则判的 / 站点有没有被
   观察），不是结论；
2. 建议与裁定分开记，C-07 才算得出「机器预填的命中率」——这一列被覆盖掉之后
   就再也算不回来了；
3. 机器身份被拒绝写 ``bucket`` 是**结构约束**，不靠"大家都记得别那么干"。

能预填哪两个桶
--------------
§11.2 只放开 ``evaluator_logic`` / ``evidence_missing``（其余四个桶的
"谁能判"都是「只能人」）。这条限制在 **import 期**检查：注册表里出现
``HUMAN_ONLY`` 的桶就在导入时炸，而不是等某一次分诊发现桶分布长歪了。

⚠️ **今天注册表里只有采集层那一条规则**（``evidence_missing``）。``evaluator_logic``
的预填**留空是结论不是遗漏**：能机械确认的只有"pred 不是 LLM 判的 ⇒
``evaluator_prompt`` 不成立"，而"所以是代码判据"这一步要靠读条款才能站住——
中间还隔着 ``rubric_ambiguous`` / ``rubric_missing`` / ``gold_defect`` 三桶。
理由与"要真做还差什么"写在 :func:`_detect_rule_decided`。

fail-closed 的两处
------------------
- **没有规则命中** ⇒ 不给建议（``suggested_bucket = None``），原因写进
  ``needs_human_reason``。绝大多数卡会是这一档——机器确实判不了；
- **两条规则指向不同桶** ⇒ 也不给建议。这不是"平均一下"能解的：两件事同时
  成立时该记哪一桶本来就是人的判断（与桶表"不认识就留空"同款）。

工作表不覆盖
------------
``build`` 只写 ``worklist.jsonl``；``worksheet.jsonl`` **已存在就跳过**，
``--overwrite`` 才重写。人填到一半被重算冲掉是这类流程最贵的损坏，比"多跑一次
命令"贵得多。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from trajectory_pipeline.evaluation.calibration import buckets as buckets_mod
from trajectory_pipeline.evaluation.calibration import evidence as evidence_mod
from trajectory_pipeline.evaluation.calibration import triage
from trajectory_pipeline.evaluation.calibration.disagreement import read_cards
from trajectory_pipeline.evaluation.calibration.snapshot import (
    HARD_DIM,
    PROVIDER_FROZEN_BRANCH,
    provider_kind,
)
from trajectory_pipeline.evaluation.golden.builder import (
    GOLDEN_ROOT,
    PIPELINE_ROOT,
    _load_jsonl,
)
from trajectory_pipeline.evaluation.golden.splits import SPLIT_FILES

#: 派生数据的缺省根：与 ``agreement --emit-disagreements`` 的缺省**同一个**
#: （都锚在 ``trajectory_pipeline/output/pipeline/``，不跟 cwd 走），所以
#: ``v{N}/`` 下四种产物（snapshot / disagreements / worklist / worksheet）天然
#: 挨在一起。用相对路径的话，"在仓库根跑"与"在 trajectory_pipeline 里跑"会读
#: 到两个不同的目录，而两处都不会报错。
DEFAULT_CALIBRATION_ROOT = PIPELINE_ROOT / "calibration"

WORKLIST_FILE = "worklist.jsonl"
WORKSHEET_FILE = "worksheet.jsonl"
CARDS_FILE = "disagreements.jsonl"
#: ``show`` 的结构化读数导出（**派生**：可重建，落在 output/ 下不入库）。
EVIDENCE_FILE = "evidence.jsonl"
#: 分诊用的**尺子**（rubric 条款原文）。单独一份而不是每行各带一份：
#: 尺子是同一把，33 行各带一份 ⇒ 同一个文件长 33 倍。
CLAUSES_FILE = "clauses.md"

#: 机器预填的签名。**带命名空间前缀** ⇒ :func:`triage.actor_kind` 判成机器，
#: 万一有人把工作表的建议值直接搬进分诊档，那边会当场拒。
SUGGESTED_BY = "agent:worklist"

#: 采集器**自己声明**没观察到的那个分支（D-10 的 fail-closed 分支）。
#: 与 ``executor.branches.BRANCH_LABELS`` 里的同名分支是同一个东西；单测钉住
#: 它还在（分支改名时这里必须一起改，而不是悄悄一条都命中不了）。
UNRESOLVED_LABEL = "unresolved"

#: 采集层拒判的**证据短语** → 给人看的说明。每一条都必须是「采集器说了没观察
#: 到 / 采集降级」，**不是**「这个结论看着不对」——后者是语义判断，机器不做。
COLLECTOR_REFUSAL_MARKERS: tuple[tuple[str, str], ...] = (
    ("采集降级", "采集器声明采集降级（证据不完整），按 fail-closed 不判"),
    ("未选中该站点", "判断点①未选中该站点 ⇒ 这个页面压根没被打开过"),
    ("站点遍历超时", "站点遍历超时 ⇒ 没拿到观察"),
    ("导航失败（传输层）", "导航在传输层就失败了 ⇒ 页面没打开过"),
    ("哨兵返回无法区分", "哨兵无法区分「页面没渲染完」与「页面确实没控件」"),
)


class WorklistError(Exception):
    """worklist 的输入不成立（→ 调用方退 2）。**不是驳回**：判据没跑起来。"""


# --------------------------------------------------------------------------
# 规则注册表
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PrefillRule:
    """一条预填规则：``detect`` 命中时返回**判据原话**，否则 ``None``。

    返回原话而不是 bool，是为了让 ``suggested_reason`` 里能写清「凭哪句话贴的
    这个桶」——人在确认时要核的正是那句话，而不是一个规则编号。
    """

    rule_id: str
    bucket: str
    why: str
    detect: Callable[[Mapping[str, Any], Mapping[str, Any]], str | None]


def _detect_collector_refused(
    card: Mapping[str, Any], facts: Mapping[str, Any]
) -> str | None:
    """采集层自己拒判 ⇒ ``evidence_missing``。

    两个条件缺一不可：**预测侧就是这个 fail-closed 分支**（``unresolved``），
    **且**证据里有一句采集器的拒判声明。只有后者不够——一条证据里提到"超时"
    并不意味着这次判定的依据全是超时。
    """
    if str(card.get("dimension") or "") != HARD_DIM:
        return None
    if str(card.get("pred_value") or "") != UNRESOLVED_LABEL:
        return None
    hits = [str(h) for h in (facts.get("collection_refusals") or [])]
    return "；".join(hits) if hits else None


def _detect_rule_decided(
    card: Mapping[str, Any], facts: Mapping[str, Any]
) -> str | None:
    """pred 侧**不是 LLM 判的** ⇒ ``evaluator_prompt`` 前置事实不成立。

    ⚠️ **当前未启用**（不在 :data:`PREFILL_RULES` 里），留在这里是因为它把
    「为什么 ``evaluator_logic`` 现在没有预填」写清楚了，同时给后继改动一个起点。

    它成立的那半步是硬的：§4 对 ``evaluator_prompt`` 的前置事实是「LLM judge
    输出与锚点冲突」，规则判据产出的分支里根本没有 LLM judge 参与，这一桶按
    定义不成立。**不成立的那半步**是"所以一定是代码判据"——剩下三桶里
    ``rubric_ambiguous`` / ``rubric_missing`` / ``gold_defect`` 全都还活着，而
    它们恰恰是"尺子读得出来两种结论/没写/金标读错"的形态，机器不读条款就分不出来。

    ⇒ 按 fail-closed：「不可能是 prompt」够不着「一定是 logic」，于是不贴建议。
    要让这一条变成真建议，得先让判据自己落下「我按哪条条款判的」以及该条款
    里可机械核的那部分（:data:`~...scorers.rubric_version.DIM_CLAUSES` 的反向
    使用）——那是另一条改动，不在 C-06 里塞一个隐式推断。
    """
    if provider_kind(card.get("pred_provider")) != PROVIDER_FROZEN_BRANCH:
        return None
    if str(facts.get("pred_origin") or "") != "rule":
        return None
    return (
        "pred 侧由规则判据产出（冻结行 code.source=rule），"
        "evaluator_prompt 的前置事实（LLM judge 输出与锚点冲突）不成立"
    )


#: 预填规则表。**顺序不影响结果**（多桶命中时一律不给建议），只影响报障时的
#: 呈现顺序。
#:
#: 现在只有一条。``evaluator_logic`` 那一条见
#: :func:`_detect_rule_decided` 的说明——**留空是结论，不是遗漏**：现有数据里
#: 没有一条「不读条款就能成立」的信号，硬填就会把机器的不确定伪装成建议。
PREFILL_RULES: tuple[PrefillRule, ...] = (
    PrefillRule(
        rule_id="R-COLLECTOR-REFUSED",
        bucket=buckets_mod.BUCKET_EVIDENCE_MISSING,
        why="采集层自己声明没观察到 / 采集降级（§4 evidence_missing 前置事实）",
        detect=_detect_collector_refused,
    ),
)

#: 规则只许指向「机器可以预填建议值」的桶（§11.2：逻辑 / 证据两类）。
PREFILLABLE_BUCKETS: frozenset[str] = frozenset(
    {buckets_mod.BUCKET_EVALUATOR_LOGIC, buckets_mod.BUCKET_EVIDENCE_MISSING}
)


def check_rules(rules: Sequence[PrefillRule]) -> None:
    """注册表守卫：**import 期**跑一次，测试里也直接跑（不改文件就能验）。

    两条都必须在导入时炸，而不是等某一次分诊发现桶分布长歪了：

    1. 规则指向的桶必须在六桶封闭集里（拼错桶名不会静默）；
    2. 规则不许指向 ``HUMAN_ONLY`` 的桶——那是尺子与真值的裁定（D-C3）。
    """
    for rule in rules:
        buckets_mod.require_bucket(rule.bucket)
        if rule.bucket not in PREFILLABLE_BUCKETS:
            raise RuntimeError(
                f"预填规则 {rule.rule_id} 指向 {rule.bucket!r}，那不是可预填的桶"
                f"（{', '.join(sorted(PREFILLABLE_BUCKETS))}）："
                "rubric_* / gold_* 是尺子与真值的裁定，连**建议值**都不许由机器"
                "先开口（方案 16 §11.2 / D-C3）"
            )


check_rules(PREFILL_RULES)


def rule_of(rule_id: str) -> PrefillRule:
    for rule in PREFILL_RULES:
        if rule.rule_id == rule_id:
            return rule
    raise KeyError(f"{rule_id!r} 不是预填规则（{', '.join(r.rule_id for r in PREFILL_RULES)}）")


# --------------------------------------------------------------------------
# 事实与建议（纯函数，吃 dict 不吃文件）
# --------------------------------------------------------------------------


def facts_of(card: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, Any]:
    """从**冻结行**取机器能确认的事实（卡上没有的那些）。

    这些字段卡不携带，而人判一条分歧恰恰需要它们：这条 pred 是谁给的（规则 /
    LLM）、有没有真的到过播放页、金标是谁审的。**都只是事实**——不预填任何
    桶，也不判断哪一侧对。
    """
    code = row.get("code") if isinstance(row.get("code"), Mapping) else {}
    golden = row.get("golden") if isinstance(row.get("golden"), Mapping) else {}
    evidence = str(card.get("pred_evidence") or "")
    return {
        "pred_origin": str(code.get("source") or ""),
        "reached_play_page": bool(code.get("reached_play_page")),
        "fallback_used": bool(code.get("fallback_used")),
        "collection_refusals": [
            f"{why}（证据含「{phrase}」）"
            for phrase, why in COLLECTOR_REFUSAL_MARKERS
            if phrase in evidence
        ],
        "gold_reviewed_by": str(golden.get("reviewed_by") or ""),
        "gold_reviewed_at": str(golden.get("reviewed_at") or ""),
    }


def suggest(
    card: Mapping[str, Any], facts: Mapping[str, Any]
) -> dict[str, Any]:
    """规则 → 建议字段（``suggested_*`` / ``needs_human*``）。**不产出 ``bucket``**。

    三种结局，都要能说清是哪种：

    - 命中且**只指向一个桶** → 给建议，``needs_human`` 仍为真（要人签名）；
    - 一条都没命中 → 不给建议，写明「机器判不了」；
    - 命中**两个不同桶** → 不给建议，把两条并排列出来（机器不替人挑）。
    """
    matched: list[dict[str, Any]] = []
    for rule in PREFILL_RULES:
        because = rule.detect(card, facts)  # 每条规则只问一次：纯函数也别问两遍
        if because:
            matched.append(
                {"rule": rule.rule_id, "bucket": rule.bucket, "because": because}
            )
    rules = [str(m["rule"]) for m in matched]
    hit_buckets = {str(m["bucket"]) for m in matched}

    if len(hit_buckets) == 1:
        because = "；".join(str(m["because"]) for m in matched)
        bucket = next(iter(hit_buckets))
        return {
            "suggested_bucket": bucket,
            "suggested_by": SUGGESTED_BY,
            "suggested_rules": rules,
            "suggested_reason": f"{because}。仍需人核 §4 的前置事实后签名",
            "needs_human": True,
            "needs_human_reason": "建议不等于裁定：桶由人填（D-C3）",
        }

    if not matched:
        return {
            "suggested_bucket": None,
            "suggested_by": "",
            "suggested_rules": [],
            "suggested_reason": "",
            "needs_human": True,
            "needs_human_reason": (
                "没有规则命中：机器判不了这一条。"
                "桶名是结论的标签，机器只报事实、不猜结论"
            ),
        }

    detail = "；".join(f"{m['rule']}→{m['bucket']}（{m['because']}）" for m in matched)
    return {
        "suggested_bucket": None,
        "suggested_by": "",
        "suggested_rules": rules,
        "suggested_reason": detail,
        "needs_human": True,
        "needs_human_reason": (
            "两条规则指向不同桶，机器不选（fail-closed）："
            "两件事同时成立时该记哪个桶本来就得人判"
        ),
    }


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def _rows_by_id(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """冻结行按 ``golden_id`` 建索引。**唯一一处**配对逻辑。

    为什么抽出来：worklist 的组装与工作表的现场读数都要「卡 ↔ 冻结行」配对。
    两处各写一遍 ⇒ 将来一边改了另一边没改 ⇒ 现场配到别的行上，而**不报错**
    （现场读数是只读上下文，配错了只表现为「现场对不上」，人会以为是站点变了）。
    """
    by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        by_id[str(row.get("golden_id") or "")] = row
    return by_id


def build_worklist(
    cards: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """卡 × 冻结行 → worklist 行（每题一条，顺序 = 卡序，重算逐字节相同）。

    行里**没有 ``bucket`` 这个键**——不是留空，是压根不给这个字段，免得有人
    「反说是空的，我顺手填上」。
    """
    by_id = _rows_by_id(rows)

    out: list[dict[str, Any]] = []
    for card in cards:
        golden_id = str(card.get("golden_id") or "")
        row = by_id.get(golden_id)
        if row is None:
            raise WorklistError(
                f"卡上的 golden_id {golden_id!r} 在冻结档里找不到："
                "卡与冻结档不是同一版，或者读错了版本号。"
                "（拿旧卡配新档会让「建议」建立在过期的观察上）"
            )
        facts = facts_of(card, row)
        entry: dict[str, Any] = {
            "disagreement_id": card.get("disagreement_id") or "",
            "golden_id": golden_id,
            "golden_version": card.get("golden_version"),
            "dimension": card.get("dimension") or "",
            "rubric_ref": card.get("rubric_ref") or "",
            "rubric_version": card.get("rubric_version") or "",
            "delta_kind": card.get("delta_kind"),
            "gold_value": card.get("gold_value"),
            "pred_value": card.get("pred_value"),
            "pred_provider": card.get("pred_provider") or "",
            "pred_evidence": card.get("pred_evidence"),
            "gold_note": card.get("gold_note") or "",
            # ↓ **可比性**（方案 16 C-11 补齐）：这条 pred 出自哪一面、
            # 那一面是不是当前这一版。判「评估器」桶时它是要看的现场之一
            # ——**不是同一版**判的分歧，可能是版本差而不是质量差。
            "pred_face": card.get("pred_face") or "",
            "pred_comparability": card.get("pred_comparability"),
            "pred_comparability_reason": card.get("pred_comparability_reason") or "",
            "facts": facts,
        }
        entry.update(suggest(card, facts))
        out.append(entry)
    return out


def build_scenes(
    cards: Sequence[Mapping[str, Any]],
    rows: Sequence[Mapping[str, Any]],
    *,
    p2_root: pathlib.Path | str | None = None,
    preview_chars: int = evidence_mod.DEFAULT_PREVIEW_CHARS,
) -> dict[str, dict[str, Any]]:
    """卡 × 冻结行 → ``{golden_id: 现场摘要}``（工作表 ``scene`` 栏的来源）。

    为什么现场要进工作表：工作表原本给的是「结论 + 一句话证据」，**不是现场**。
    而 D-10 的判据原文是「对照黄金集**预存档**的站点核验结论」——预存档不在
    表里。于是人打开表看到的是「这个页面是不是播放站」这句话，**却没有页面**
    可看，「该归哪个桶」就答不了。

    这不是「人不肯判」，是**表里没有可判的东西**。把现场塞进同一行，人就不用
    为了看一条现场去开第二个文件、跑第二条命令。

    只读：这里**不碰** ``bucket``。现场是「判这一条的依据」，裁定仍然是人签的
    （D-C3）。
    """
    by_id = _rows_by_id(rows)
    scenes: dict[str, dict[str, Any]] = {}
    for card in cards:
        golden_id = str(card.get("golden_id") or "")
        row = by_id.get(golden_id)
        if row is None:
            # 配不上就是**版本错配**（build_worklist 已经会抛），这里不重复抛，
            # 只留下一个显式的「没读到」占位——工作表里每一行都要有 scene 键，
            # 免得人以为「没有 scene 键 = 现场是空的」。
            scenes[golden_id] = {
                "found": False,
                "status": "not_read",
                "unit": "",
                "note": "卡与冻结档配不上（版本错配）⇒ 没读现场",
                "degraded": [],
                "title": "",
                "steps": [],
                "body": "",
                "body_from": "",
                "candidates": [],
            }
            continue
        digest = evidence_mod.card_digest(
            card, row, p2_root=p2_root, preview_chars=preview_chars
        )
        scenes[golden_id] = evidence_mod.scene_summary(digest)
    return scenes


def build_worksheet(
    worklist_rows: Sequence[Mapping[str, Any]],
    scenes: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """人填的表单：三个空栏 + 只读上下文（**含现场**）。

    三个空栏就是分诊档的必填项（``triage.REQUIRED_FIELDS``）里**由人给的那三个**
    ——``golden_id`` / ``dimension`` 由卡带出，不让人手写（手写必抄错，而抄错的
    行在 ``triage import`` 之前不会被发现）。

    ``scene`` 是**只读上下文**：判这一条要看的现场（页面读数 + 正文）。
    它**不进分诊档**（``triage.strip_row`` 只留裁定字段），也不作数——桶由人填。
    """
    scene_map = scenes or {}
    out: list[dict[str, Any]] = []
    for row in worklist_rows:
        golden_id = str(row.get("golden_id") or "")
        scene = scene_map.get(golden_id) or {
            "found": False,
            "status": "not_read",
            "unit": "",
            "note": "**没生成现场读数**（build 时没取存档）——"
            "这一栏空着不等于页面是空的",
            "degraded": [],
            "title": "",
            "steps": [],
            "body": "",
            "body_from": "",
            "candidates": [],
        }
        out.append(
            {
                "golden_id": golden_id,
                "dimension": row.get("dimension") or "",
                # ↓ 人填这三栏
                "bucket": "",
                "triage_by": "",
                "triage_evidence": "",
                # ↓ 只读上下文：判这一条要看的
                "rubric_ref": row.get("rubric_ref") or "",
                "delta_kind": row.get("delta_kind"),
                "gold_value": row.get("gold_value"),
                "pred_value": row.get("pred_value"),
                "pred_evidence": row.get("pred_evidence"),
                "facts": row.get("facts") or {},
                "suggested_bucket": row.get("suggested_bucket"),
                "suggested_reason": row.get("suggested_reason") or "",
                "needs_human_reason": row.get("needs_human_reason") or "",
                # 可比性三态 + 人话理由：**只读上下文**，判「评估器」桶时看。
                "pred_face": row.get("pred_face") or "",
                "pred_comparability": row.get("pred_comparability"),
                "pred_comparability_reason": row.get("pred_comparability_reason") or "",
                "scene": dict(scene),
            }
        )
    return out


def summarize_worklist(
    worklist_rows: Sequence[Mapping[str, Any]],
    triage_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """归属进度视图：覆盖率 / 建议命中 / 接受与改判 / 对不上的分诊行。"""
    decided = {
        (str(r.get("golden_id") or ""), str(r.get("dimension") or "")): str(
            r.get("bucket") or ""
        )
        for r in (triage_rows or [])
    }
    cards = [
        (str(r.get("golden_id") or ""), str(r.get("dimension") or ""))
        for r in worklist_rows
    ]
    covered = [k for k in cards if k in decided]
    suggested = [r for r in worklist_rows if r.get("suggested_bucket")]
    accepted = [
        r
        for r in suggested
        if decided.get((str(r.get("golden_id") or ""), str(r.get("dimension") or "")))
        == r["suggested_bucket"]
    ]
    overridden = [
        r
        for r in suggested
        if decided.get((str(r.get("golden_id") or ""), str(r.get("dimension") or "")))
        not in (None, "", r["suggested_bucket"])
    ]
    by_bucket: dict[str, int] = {}
    for row in worklist_rows:
        key = (str(row.get("golden_id") or ""), str(row.get("dimension") or ""))
        bucket = decided.get(key) or ""
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
    by_rule: dict[str, int] = {}
    for row in worklist_rows:
        for rule_id in row.get("suggested_rules") or []:
            by_rule[str(rule_id)] = by_rule.get(str(rule_id), 0) + 1
    reviewers: dict[str, int] = {}
    for row in worklist_rows:
        who = str((row.get("facts") or {}).get("gold_reviewed_by") or "")
        reviewers[who] = reviewers.get(who, 0) + 1
    return {
        "cards": len(cards),
        "triaged": len(covered),
        "untriaged": [f"{k[0]}:{k[1]}" for k in cards if k not in decided],
        "fully_triaged": len(covered) == len(cards) and bool(cards),
        "suggested": len(suggested),
        "accepted": len(accepted),
        "overridden": len(overridden),
        "by_bucket": dict(sorted(by_bucket.items())),
        "by_rule": dict(sorted(by_rule.items())),
        "gold_reviewed_by": dict(sorted(reviewers.items())),
        "unmatched_triage": sorted(
            f"{k[0]}:{k[1]}" for k in decided if k not in set(cards)
        ),
    }


# --------------------------------------------------------------------------
# 读写
# --------------------------------------------------------------------------


def _write_jsonl(path: pathlib.Path, rows: Sequence[Mapping[str, Any]]) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return path


def worklist_path(root: pathlib.Path | str, version: int) -> pathlib.Path:
    return pathlib.Path(root) / f"v{version}" / WORKLIST_FILE


def worksheet_path(root: pathlib.Path | str, version: int) -> pathlib.Path:
    return pathlib.Path(root) / f"v{version}" / WORKSHEET_FILE


def cards_path(root: pathlib.Path | str, version: int) -> pathlib.Path:
    return pathlib.Path(root) / f"v{version}" / CARDS_FILE


def clauses_path(root: pathlib.Path | str, version: int) -> pathlib.Path:
    """分诊用的尺子（rubric 条款原文）落在工作表旁边。"""
    return pathlib.Path(root) / f"v{version}" / CLAUSES_FILE


def load_worklist(path: pathlib.Path | str) -> list[dict[str, Any]]:
    p = pathlib.Path(path)
    if not p.exists():
        raise WorklistError(f"worklist 不存在：{p}")
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_frozen_rows(golden_root: pathlib.Path | str, version: int) -> list[dict[str, Any]]:
    """读某一版的**全部三档**冻结行（带 ``__split__``，与 ``agreement`` 同源）。

    三档都读，是因为卡本身只从调参池出——但校验"卡上的 golden_id 在冻结档里
    存在"时不该顺手把其余两档当成不存在（那会把配错版本报成"卡与档不是同一版"）。
    """
    vdir = pathlib.Path(golden_root) / f"v{version}"
    if not vdir.exists():
        raise WorklistError(f"{vdir} 不存在：先 freeze 出这一版")
    rows: list[dict[str, Any]] = []
    for split, fname in SPLIT_FILES.items():
        p = vdir / fname
        if not p.exists():
            continue
        for row in _load_jsonl(p):
            row["__split__"] = split
            rows.append(row)
    if not rows:
        raise WorklistError(f"{vdir} 下没有任何档样本文件（{', '.join(SPLIT_FILES.values())}）")
    return rows


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _cmd_build(args: argparse.Namespace) -> int:
    cards_file = pathlib.Path(args.cards) if args.cards else cards_path(
        args.out, args.version
    )
    if not cards_file.exists():
        # 卡不存在 ≠ 卡是空的：前者是**没跑 agreement**，后者是这一版真没有分歧。
        # 混成一个数，就会把「忘了先跑」读成「这批全都一致」。
        raise WorklistError(
            f"分歧卡不存在：{cards_file}\n"
            f"先跑 `agreement --version {args.version} --emit-disagreements`"
            "（worklist 是卡的视图，不自造判据）"
        )
    cards = read_cards(cards_file)
    if not cards:
        print(
            f"⚠ {cards_file} 里 0 张分歧卡：先把 agreement 跑出卡，或者核对版本号"
            f"（`agreement --version {args.version} --emit-disagreements`）"
        )
    rows = load_frozen_rows(args.golden_root, args.version)
    worklist = build_worklist(cards, rows)

    out_path = _write_jsonl(worklist_path(args.out, args.version), worklist)
    stats = summarize_worklist(worklist)
    print(
        f"→ 已写 {out_path}（{stats['cards']} 条："
        f"有建议 {stats['suggested']} / 待人判 {stats['cards'] - stats['suggested']}）"
    )
    for rule_id, n in stats["by_rule"].items():
        print(f"    {rule_id:<24}{n:>4}  {rule_of(rule_id).bucket}")

    # 尺子：条款原文单独一份。**现读**（不抄第二份到代码里）——条款改了重跑
    # build 即可。读不到就**喊出来**，不静默少一条：人拿着没有尺子的表判桶，
    # 会把「尺子没写」误判成「代码判错了」。
    clauses_path_ = clauses_path(args.out, args.version)
    try:
        clauses = evidence_mod.load_clauses()
    except (FileNotFoundError, ValueError) as exc:
        print(f"⚠ 尺子取不到：{exc}")
        print("   工作表的 scene 仍可用，但分支语义条款（D-4/D-5）没落盘——"
              "判「该给哪个分支标签」时没有尺子可用")
    else:
        clauses_path_.parent.mkdir(parents=True, exist_ok=True)
        wanted = list(dict.fromkeys(
            [str(c.get("rubric_ref") or "") for c in cards]
            + list(evidence_mod.DEFAULT_EXTRA_CLAUSES)
        ))
        clauses_path_.write_text(
            evidence_mod.clauses_markdown(clauses, wanted), encoding="utf-8"
        )
        print(
            f"→ 已写尺子 {clauses_path_}"
            f"（{' / '.join(c for c in wanted if c)}）"
        )

    # 现场：反查回 P1 存档，把「页面长什么样」放进**人填的那张表**。
    scenes = build_scenes(
        cards,
        rows,
        p2_root=args.p2_root,
        preview_chars=args.preview,
    )
    located = sum(1 for s in scenes.values() if s.get("found"))
    print(
        f"→ 现场读数：{located}/{len(scenes)} 条定位到存档"
        f"（其余的会在表里明说「读不到」，不是「页面是空的」）"
    )

    sheet = worksheet_path(args.out, args.version)
    if sheet.exists() and not args.overwrite:
        # 人填到一半被冲掉是这里最贵的损坏：宁可让人显式 --overwrite。
        print(f"→ 工作表已存在，**不覆盖**：{sheet}（要重来加 --overwrite）")
    else:
        _write_jsonl(sheet, build_worksheet(worklist, scenes))
        print(f"→ 已写工作表 {sheet}（填 bucket / triage_by / triage_evidence 三栏）")
        print("   每行带 scene 栏：现场读数（页面标题 / 每步观察 / 正文预览）"
              "——判桶前先看它")
        print(f"   尺子（分支语义 D-4 / D-5）在 {clauses_path_}")
        print(
            "   填完 → `triage import <工作表> --apply`"
            "（校验后才进 data/triage/triage.jsonl）"
        )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    path = pathlib.Path(args.worklist) if args.worklist else worklist_path(
        args.out, args.version
    )
    worklist = load_worklist(path)

    if args.triage:
        triage_rows = triage.load_triage(args.triage)
        assert triage_rows is not None
    else:
        # 缺省档：**不存在不算错**（首轮分诊之前它本来就不存在），但要念一句，
        # 免得「0% 覆盖」被读成「工具坏了」。
        default = triage.DEFAULT_TRIAGE_FILE
        if default.exists():
            triage_rows = triage.load_triage([str(default)])
        else:
            print(f"（分诊档 {default} 还不存在：本轮覆盖率按 0 算）")
            triage_rows = []

    stats = summarize_worklist(worklist, triage_rows)
    pct = (100.0 * stats["triaged"] / stats["cards"]) if stats["cards"] else 0.0
    print(
        f"分诊进度 {stats['triaged']}/{stats['cards']} = {pct:.0f}%"
        f" · 机器建议 {stats['suggested']} 条"
        f"（接受 {stats['accepted']} / 改判 {stats['overridden']}）"
    )
    for bucket in buckets_mod.BUCKETS:
        n = stats["by_bucket"].get(bucket, 0)
        mark = "（只能人填）" if buckets_mod.is_human_only(bucket) else ""
        print(f"  {bucket:<18}{n:>4}  {buckets_mod.owner_of(bucket)}{mark}")
    if stats["by_bucket"].get(""):
        print(f"  {'(未分诊)':<16}{stats['by_bucket']['']:>4}")
    if stats["unmatched_triage"]:
        print(
            f"  ⚠ 有 {len(stats['unmatched_triage'])} 行分诊对不上本轮卡"
            "（换了版本或卡已消失，不参与覆盖率）"
        )
    if stats["untriaged"]:
        shown = ", ".join(stats["untriaged"][:5])
        more = "…" if len(stats["untriaged"]) > 5 else ""
        print(f"  待分诊：{shown}{more}")
    return 0 if stats["fully_triaged"] else 1


def _cmd_show(args: argparse.Namespace) -> int:
    """把一张（或全部）分歧卡的**现场**与**尺子**摊开。

    为什么要有它：工作表给的是「结论 + 一句话证据」，而 D-10 的判据原文是
    「对照黄金集**预存档**的站点核验结论」——**预存档不在表里**。人判桶要回答
    「这个页面到底是不是播放站」，不回现场就答不了。

    只读：找不到现场就说找不到（``⚠`` 标记），**不返回空读数**——空读数会被
    读成「页面里没有 video 标签」，那是把「没找到」当成「看到了没有」。
    """
    version = int(args.version)
    cards = load_worklist(worklist_path(args.out, version))
    rows = load_frozen_rows(args.golden_root, version)
    row_by_id: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        row_by_id.setdefault(str(row.get("golden_id") or ""), row)

    if not args.all and not args.golden_id:
        raise WorklistError(
            "要么给 --golden-id <id>，要么给 --all：一条都不指 ⇒ 不知道要看哪张卡"
        )

    if args.golden_id:
        picked = [
            c for c in cards if str(c.get("golden_id") or "") == args.golden_id
        ]
        if not picked:
            if args.golden_id in row_by_id:
                raise WorklistError(
                    f"{args.golden_id} 在冻结档 v{version} 里存在，但**不是本轮的"
                    f"分歧卡**（{len(cards)} 张卡里没有它）——它是两侧一致的行，"
                    "不用分诊"
                )
            raise WorklistError(
                f"{args.golden_id} 不在本轮的 {len(cards)} 张卡里，"
                f"冻结档 v{version} 里也找不到：版本对不上？"
                f"（先跑 `worklist build --version {version}`）"
            )
    else:
        picked = list(cards)

    digests = []
    for card in picked:
        gid = str(card.get("golden_id") or "")
        row = row_by_id.get(gid)
        if row is None:
            raise WorklistError(
                f"卡上的 golden_id {gid!r} 在冻结档 v{version} 里找不到："
                "卡与冻结档不是同一版，或者读错了版本号。"
                "（拿旧卡配新档会让读数建立在过期的观察上）"
            )
        digests.append(
            evidence_mod.card_digest(
                card,
                row,
                p2_root=args.p2_root,
                preview_chars=args.preview,
            )
        )

    if args.all:
        total = len(digests)
        for i, digest in enumerate(digests, 1):
            print(evidence_mod.render_compact(digest, index=i, total=total))
            print()
    else:
        clauses = evidence_mod.load_clauses()
        for digest in digests:
            print(evidence_mod.render_card(digest, clauses))
            print()

    if args.json is not None:
        path = (
            pathlib.Path(args.json)
            if args.json
            else pathlib.Path(args.out) / f"v{version}" / EVIDENCE_FILE
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in digests),
            encoding="utf-8",
        )
        print(f"→ 已写读数 {path}（{len(digests)} 条，派生数据不入库）")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(
        prog="worklist",
        description=(
            "首轮分诊 worklist（方案 16 C-06）：给每张分歧卡贴机器事实与"
            "至多一条建议桶，产出人填的工作表"
        ),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--version", type=int, required=True, help="冻结版本号（v7 → 7）")
        p.add_argument(
            "--out", default=str(DEFAULT_CALIBRATION_ROOT),
            help=f"派生数据根（缺省 {DEFAULT_CALIBRATION_ROOT}）",
        )

    p_build = sub.add_parser("build", help="从分歧卡派生 worklist + 工作表")
    _common(p_build)
    p_build.add_argument("--cards", default="", help="分歧卡路径（缺省 v{N}/disagreements.jsonl）")
    p_build.add_argument(
        "--golden-root", default=str(GOLDEN_ROOT), help="冻结档根（缺省 evaluation/golden/data）"
    )
    p_build.add_argument(
        "--p2-root", default=str(PIPELINE_ROOT),
        help=f"P1 存档根（缺省 {PIPELINE_ROOT}）——工作表的 scene 栏从这儿读现场",
    )
    p_build.add_argument(
        "--preview", type=int, default=evidence_mod.DEFAULT_PREVIEW_CHARS,
        help=f"正文预览字符数（缺省 {evidence_mod.DEFAULT_PREVIEW_CHARS}）",
    )
    p_build.add_argument("--overwrite", action="store_true", help="重写已存在的工作表（会冲掉人填的内容）")

    p_status = sub.add_parser("status", help="归属进度：覆盖率 / 接受与改判（退出码 0 全覆盖 / 1 未满）")
    _common(p_status)
    p_status.add_argument("--worklist", default="", help="worklist 路径（缺省 v{N}/worklist.jsonl）")
    p_status.add_argument(
        "--triage", nargs="*", default=None,
        help=f"分诊档（缺省 {triage.DEFAULT_TRIAGE_FILE}；显式给的路径不存在 = 退 2）",
    )

    p_show = sub.add_parser(
        "show", help="读现场：把卡的存档观察与 rubric 条款原文摊开（只读）"
    )
    _common(p_show)
    p_show.add_argument("--golden-id", default="", help="只看这一张卡")
    p_show.add_argument("--all", action="store_true", help="本轮全部卡的紧凑读数")
    p_show.add_argument(
        "--golden-root", default=str(GOLDEN_ROOT), help="冻结档根（缺省 evaluation/golden/data）"
    )
    p_show.add_argument(
        "--p2-root", default=str(PIPELINE_ROOT),
        help=f"P1 存档根（缺省 {PIPELINE_ROOT}）",
    )
    p_show.add_argument(
        "--preview", type=int, default=evidence_mod.DEFAULT_PREVIEW_CHARS,
        help=f"正文预览字符数（缺省 {evidence_mod.DEFAULT_PREVIEW_CHARS}）",
    )
    p_show.add_argument(
        "--json", nargs="?", const="", default=None,
        help="导出结构化读数（不给路径时落派生目录 v{N}/evidence.jsonl）",
    )

    args = ap.parse_args(argv)
    try:
        if args.cmd == "build":
            return _cmd_build(args)
        if args.cmd == "status":
            return _cmd_status(args)
        return _cmd_show(args)
    except (WorklistError, triage.TriageError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print("退出码 2（输入不成立）：这不是「驳回」，是 worklist 没跑起来", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print("退出码 2（输入不成立）：读数取不到——rubric 文档或存档有问题", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CARDS_FILE",
    "CLAUSES_FILE",
    "COLLECTOR_REFUSAL_MARKERS",
    "DEFAULT_CALIBRATION_ROOT",
    "PREFILLABLE_BUCKETS",
    "PREFILL_RULES",
    "PrefillRule",
    "SUGGESTED_BY",
    "UNRESOLVED_LABEL",
    "WORKLIST_FILE",
    "WORKSHEET_FILE",
    "WorklistError",
    "build_scenes",
    "build_worklist",
    "build_worksheet",
    "cards_path",
    "check_rules",
    "clauses_path",
    "facts_of",
    "load_frozen_rows",
    "load_worklist",
    "main",
    "rule_of",
    "suggest",
    "summarize_worklist",
    "worklist_path",
    "worksheet_path",
]