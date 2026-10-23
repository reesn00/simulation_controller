"""rubric 可追溯性的**契约测试**（方案 16 §8 列在 C-05 这一条上）。

这里放的是**不变式**，不是行为细节——它们要在"有人加东西"的那一刻就红：

1. **桶表封顶 6 项**（D-C2）：加第 7 个桶必须同时改 D-C2，否则这里先红；
2. **没有万能垃圾桶**：`other` / `unknown` / `accepted_exception` 都不许当桶
   （`accepted_exception` 是**状态**，§3.3）；
3. **`rubric_*` / `gold_*` 只能人填**（D-C3），且这份「只能人填」清单是
   **按前缀派生**的——新增 `rubric_*` 桶自动落到人这一侧；
4. **分诊两条必填**：签名（`triage_by`）与理由（`triage_evidence`）缺一不可。

行为细节（各分支的报错文案、读写幂等、CLI 退出码）在
`tests/unit/test_calibration_triage.py`；这里只钉"规矩本身还在不在"。
"""

from __future__ import annotations

import pytest

from trajectory_pipeline.evaluation.calibration import buckets, triage, worklist
from trajectory_pipeline.evaluation.scorers import rubric_changelog, rubric_version

#: 万能垃圾桶候选：任何一个成为桶都意味着分布不再可读。
_TRASH_BUCKETS = ("other", "unknown", "misc", "accepted_exception", "none", "")


def test_桶表封顶六项_D_C2():
    assert len(buckets.BUCKETS) == 6
    assert buckets.MAX_BUCKETS == 6
    # 顺序即 §4 表序；换序会让报表的历史对比错位，所以也钉住
    assert buckets.BUCKETS == (
        "evaluator_logic",
        "evaluator_prompt",
        "rubric_ambiguous",
        "rubric_missing",
        "gold_defect",
        "evidence_missing",
    )


@pytest.mark.parametrize("name", _TRASH_BUCKETS)
def test_没有万能垃圾桶(name):
    assert name not in buckets.BUCKETS
    with pytest.raises(KeyError):
        buckets.require_bucket(name)


def test_每个桶都带前置事实责任方与回路():
    """三者缺一，桶就只是一个词：不知道什么时候能贴、该谁改、走哪条回路。"""
    for bucket in buckets.BUCKETS:
        assert buckets.BUCKET_PRECONDITION[bucket].strip(), bucket
        assert buckets.BUCKET_OWNER[bucket] in {
            "code", "prompt", "rubric", "gold", "collector"
        }, bucket
        assert buckets.BUCKET_LOOP[bucket] in buckets.LOOPS, bucket


def test_rubric与gold桶只能人填_D_C3():
    assert buckets.HUMAN_ONLY == frozenset(
        b for b in buckets.BUCKETS if b.startswith(("rubric_", "gold_"))
    )
    assert buckets.HUMAN_ONLY == frozenset(
        {"rubric_ambiguous", "rubric_missing", "gold_defect"}
    )
    for bucket in buckets.HUMAN_ONLY:
        with pytest.raises(triage.TriageError):
            triage.validate_row({
                "golden_id": "X__r1__h",
                "dimension": "d10",
                "bucket": bucket,
                "triage_by": "agent",
                "triage_evidence": "agent 想替人裁尺子",
            })


def test_分诊的签名与理由都必填():
    base = {
        "golden_id": "X__r1__h",
        "dimension": "d10",
        "bucket": "evaluator_logic",
        "triage_by": "owner",
        "triage_evidence": "按明写条款应得另一个值",
    }
    assert triage.validate_row(base)["bucket"] == "evaluator_logic"
    for field in ("triage_by", "triage_evidence"):
        row = dict(base)
        row[field] = "   "
        with pytest.raises(triage.TriageError):
            triage.validate_row(row)


def test_分诊只能经校验写入():
    """`add_rows` 是唯一写入口，且写之前一定过 `validate_rows`。"""
    import inspect

    src = inspect.getsource(triage.add_rows)
    assert "validate_rows(" in src
    # 两条旁路都不许存在：直接写 JSON 或用 pathlib.write_text 绕开校验
    assert "json.dumps(r" in src  # 写盘用的是已经合并好的 merged
    assert "validate_rows(rows)" in src


# ── C-06：机器只出建议，裁定权不挪 ─────────────────────────


def test_预填规则不许指向human_only桶():
    """注册表是唯一能加规则的地方，所以规矩钉在这里（§11.2 / D-C3）。"""
    bad = worklist.PrefillRule(
        rule_id="R-SNEAKY",
        bucket="gold_defect",
        why="机器想替人判金标",
        detect=lambda card, facts: "（伪造命中）",
    )
    with pytest.raises(RuntimeError):
        worklist.check_rules((bad,))
    # 现有表本身是干净的
    worklist.check_rules(worklist.PREFILL_RULES)
    for rule in worklist.PREFILL_RULES:
        assert not buckets.is_human_only(rule.bucket), rule.rule_id


def test_预填签名是机器身份():
    """建议的签名走的是与分诊同一套身份词汇，不是自造字符串。"""
    assert triage.actor_kind(worklist.SUGGESTED_BY) == "agent"
    with pytest.raises(triage.TriageError):
        triage.validate_row(
            {
                "golden_id": "X__r1__h",
                "dimension": "d10",
                "bucket": "evidence_missing",
                "triage_by": worklist.SUGGESTED_BY,
                "triage_evidence": "机器把自己的建议当成了裁定",
            }
        )


def test_worklist产不出bucket_也写不了分诊档():
    """两条结构性约束，都是"绕不过去"而不是"记得别那么干"：

    1. `build_worklist` 的源码里没有给 `bucket` 赋值——建议只落 `suggested_*`；
    2. 本模块不碰 `add_rows`——分诊档的唯一写入口在 `triage` 里。
    """
    import inspect

    assert '"bucket"' not in inspect.getsource(worklist.build_worklist)
    assert "add_rows" not in inspect.getsource(worklist)
    assert triage.KEPT_FIELDS[2] == "bucket"  # 裁定那一栏只在 triage 侧定义


# ── C-12 / C-18：变更记录是动机链，「未列条款逐位不变」有分母 ─────────────


def test_真实变更记录合契约():
    """rubric 文档里那张表每次测试都过一遍——格式错了当场红，不靠人记得。"""
    entries = rubric_changelog.parse_entries()
    rubric_changelog.validate_entries(entries)
    assert rubric_changelog.check_current(entries).version == rubric_version.RUBRIC_VERSION


def test_升版本要在三处同时落地():
    """文档版本行 / ``RUBRIC_VERSION`` / 变更记录的一行——三处缺一即红。

    这是这张表最常见的漂：**版本号升了、记录忘了写**。
    """
    assert rubric_version.documented_rubric_version() == rubric_version.RUBRIC_VERSION
    entries = rubric_changelog.parse_entries()
    assert rubric_version.RUBRIC_VERSION in rubric_changelog.versions(entries)


def test_条款词表与rubric文档三族一致():
    """``G-1..G-7`` / ``D-1..D-10`` / ``B-1..B-4`` 必须都还在文档里。

    条款号被改名或删掉而代码这边照旧 ⇒ 变更记录会写出一个文档里不存在的条款，
    而它看起来完全正常。
    """
    text = rubric_version.RUBRIC_DOC.read_text(encoding="utf-8")
    missing = [c for c in rubric_changelog.BASE_CLAUSES if c not in text]
    assert missing == [], f"这些条款号在 rubric 文档里找不到：{missing}"


def test_每个维度都能写出条款号():
    assert rubric_changelog.clause_vocabulary_covers_dims() == []
    # 档位条款号由 clause_of 投影，不手抄
    assert rubric_version.clause_of("d3", 5) in rubric_changelog.known_clauses()


def test_调权重是没有权重表时的空类():
    """留着取值是为了将来不必改契约，**不是现在有东西可调**（`G-7`）。"""
    assert rubric_changelog.WEIGHT_TABLE_EXISTS is False
    assert rubric_changelog.KIND_REWEIGHT in rubric_changelog.CHANGE_KINDS


def test_派生根与verify同源():
    """本地常量 + 断言：只 import 会拖上报表模块，抄一份又必然漂。"""
    from trajectory_pipeline.evaluation.calibration import verify
    from trajectory_pipeline.evaluation.golden import builder

    assert rubric_changelog.DEFAULT_CALIBRATION_ROOT == verify.DEFAULT_SNAPSHOT_ROOT
    assert (
        rubric_changelog.DEFAULT_CALIBRATION_ROOT
        == builder.PIPELINE_ROOT / "calibration"
    )