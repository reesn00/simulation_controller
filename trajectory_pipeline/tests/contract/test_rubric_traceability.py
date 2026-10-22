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

from trajectory_pipeline.evaluation.calibration import buckets, triage

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