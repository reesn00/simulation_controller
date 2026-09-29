"""tests/label_studio: C3 样本数据（与 conftest 的路径工厂分离）。

不放 conftest 是因为测试模块要直接 import 常量做参数化; conftest 只负责
按 ``tmp_path`` 落盘。同目录模块可被 pytest 直接 import（rootdir 模式下
basedir 会被插入 ``sys.path``）。
"""

from __future__ import annotations

from typing import Any

#: 一份"信息完整"的 meta —— L0–L5 六个维度全部有数据源。
RICH_META: dict[str, Any] = {
    "session_id": "useramulation-20260928-abc",
    "training_value_score": 0.58,
    "complexity_tier": "medium",
    # L0：由 orchestration F2 从 output/runs/<run_id>/ 注入
    "criterion_results": {
        "run_id": "run-1",
        "rounds": 2,
        "final_verdict": "fail",
        "missing_items": ["未给出具体库存数字"],
        "criteria": [
            {
                "criterion_id": "C1",
                "verdict": "pass",
                "reason_code": "found_in_reply",
                "message": "已提供推荐列表",
                "evidence_ids": ["ev_3f2a"],
                "retryable": False,
            },
            {
                "criterion_id": "C4",
                "verdict": "fail",
                "reason_code": "missing_item",
                "message": "未给出具体库存数字",
                "evidence_ids": [],
                "retryable": True,
            },
        ],
    },
    # L1：gdr trajectory_compare
    "trajectory_compare": {
        "overall": "review",
        "instruction_adherence": {
            "score": "review",
            "diff_summary": [
                {"step_range": "12-14", "type": "required_change",
                 "note": "删掉了要求的对比表"},
                {"step_range": "20", "type": "regression", "note": "结论与前文矛盾"},
            ],
        },
        "fidelity": {
            "lost_elements": ["对比表"],
            "preserved_core": ["主推商品"],
        },
    },
    # L2：gdr 红线结果
    "trajectory_free": {
        "redline": {
            "violation": False,
            "labels": [
                {"type": "prompt_injection", "step_location": 7,
                 "evidence": "工具返回含指令注入"},
            ],
        }
    },
    # L3：块级校验计数
    "validation_summary": {
        "total_blocks": 9,
        "passed_L1": 9, "failed_L1": 0,
        "passed_L2": 8, "failed_L2": 1,
        "passed_L3": 7, "failed_L3": 2,
        "modified_blocks": 3,
    },
    # L4：quality_scorer 七维分量（health 是粗估，见 scorecard 模块注释）
    "quality_scorer_components": {
        "health": 0.83, "judge": 0.6, "intent": 1.0,
        "modified": 0.75, "diversity": 0.5, "noise": 0.9, "depth": 0.9,
    },
    # L5：精修编辑状态分布
    "edit_status_summary": {"EDIT": 2, "KEEP": 6, "REWRITE": 1},
    "meta_tag_contamination": {"has_meta_tag": False, "total_count": 0},
}

RICH_MESSAGES: dict[str, Any] = {
    "messages": [
        {"role": "user", "content": "帮我推荐三款高性价比耳机"},
        {"role": "assistant", "content": "好的，推荐如下…"},
    ],
    "tools": [{"type": "function", "function": {"name": "search"}}],
}

QF_TEXT = (
    "<|im_start|>system\n# Tools\n\nYou have access to the following functions:…<|im_end|>\n"
    "<|im_start|>user\n帮我推荐三款高性价比耳机<|im_end|>\n"
)

#: 一个"干净"但缺 C4 的 meta，用于 source=missing 分支。
SPARSE_META: dict[str, Any] = {
    "session_id": "sparse-1",
    "training_value_score": 0.91,
    "complexity_tier": "easy",
}
