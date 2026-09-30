"""tests/label_studio: C3 样本数据（与 conftest 的路径工厂分离）。

不放 conftest 是因为测试模块要直接 import 常量做参数化; conftest 只负责
按 ``tmp_path`` 落盘。同目录模块可被 pytest 直接 import（rootdir 模式下
basedir 会被插入 ``sys.path``）。
"""

from __future__ import annotations

from typing import Any

#: ChatML 渲染样本（``*.qwenjina.txt`` 全文）。2026-09-30 起 LS 不再上传它,
#: 但 ``conftest.write_c3`` 落盘与 :data:`LEGACY_RICH_META` 仍要它。
QF_TEXT = (
    "<|im_start|>system\n# Tools\n\nYou have access to the following functions:…<|im_end|>\n"
    "<|im_start|>user\n帮我推荐三款高性价比耳机<|im_end|>\n"
)

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
    # 2026-09-30 起 C3 磁盘 meta.json **不再**内嵌 4 视图内容副本
    # （save_session_v2 的 _VIEW_PAYLOAD_KEYS）。审计由 tools_declared
    # （未截断工具名）+ views（尺寸 + sha256）承担。
    "tools_declared": ["search"],
    "views": {
        "messages": {"file": "T001__x_refined.messages.json", "bytes": 74188,
                     "sha256": "a" * 64},
        "openai": {"file": "T001__x_refined.openai.json", "bytes": 73473,
                   "sha256": "b" * 64},
        "qwenjina": {"file": "T001__x_refined.qwenjina.txt", "bytes": 59964,
                     "sha256": "c" * 64},
    },
    # qf_stats / qf_rendered_at 刻意保留: qf_rendered_at 是标注页
    # c3_rendered_at 时间线的唯一数据源 (task_exporter)。
    "qf_stats": {"openai_messages": 2, "tools": 1},
    "qf_rendered_at": "2026-09-30T00:00:00.000000Z",
}

#: 2026-09-30 之前的 meta 形态 —— 磁盘上仍可能存在（存量产物），且推送侧
#: ``_META_VIEW_PAYLOAD_KEYS`` 剥离逻辑保留为防御。仅供「旧形态也能正确剥离」
#: 的测试用，不代表新 C3 的形状。RICH_META 里刻意不嵌视图载荷。
LEGACY_RICH_META: dict[str, Any] = {
    **RICH_META,
    "openai_messages": [{"role": "user", "content": "帮我推荐三款高性价比耳机"}],
    "tools": [{"type": "function", "function": {"name": "search"}}],
    "qf_text": QF_TEXT,
}

RICH_MESSAGES: dict[str, Any] = {
    "messages": [
        {
            "role": "user",
            "content": "帮我推荐三款高性价比耳机",
            # 真实 C3 里只有 user 消息带 created_at（system/assistant 恒空串）
            "created_at": "2026-09-30T04:50:12.972292+00:00",
        },
        {"role": "assistant", "content": "好的，推荐如下…"},
    ],
    "tools": [{"type": "function", "function": {"name": "search"}}],
}

#: 一个"干净"但缺 C4 的 meta，用于 source=missing 分支。
SPARSE_META: dict[str, Any] = {
    "session_id": "sparse-1",
    "training_value_score": 0.91,
    "complexity_tier": "easy",
}
