"""gdr/refiners/meta_tag_strip 模块的单元 + 集成测试.

TDD 起点: 这些测试先于实现存在, 用以驱动 meta_tag_strip 模块的 API.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gdr.refiners.meta_tag_strip import (
    annotate_meta_tags,
    strip_meta_tags,
    strip_session_payload,
)
from gdr.domain.schema import (
    Message,
    Session,
    TextBlock,
    ThinkingBlock,
    save_session,
)


# ---------------------------------------------------------------------------
# 纯文本剥离
# ---------------------------------------------------------------------------


def test_strip_basic_single_tag():
    text = "正常结论。\n⟦ 任务｜已完成 ⟧\n"
    out = strip_meta_tags(text)
    assert "⟦" not in out and "⟧" not in out
    assert "正常结论" in out


def test_strip_multiple_tags():
    text = "A 段。\n⟦ tag1 ⟧\nB 段。\n⟦ tag2 ⟧\nC 段。"
    out = strip_meta_tags(text)
    assert "⟦" not in out
    assert "A 段" in out and "B 段" in out and "C 段" in out


def test_strip_no_tag_unchanged():
    text = "干净文本，无任何标签。"
    assert strip_meta_tags(text) == text


def test_strip_empty_string():
    assert strip_meta_tags("") == ""


def test_strip_collapses_excess_blank_lines():
    text = "段落1。\n\n\n⟦ tag ⟧\n\n\n段落2。"
    out = strip_meta_tags(text)
    # 不应出现 3+ 连续换行
    assert "\n\n\n" not in out
    # 两段内容都应保留
    assert "段落1" in out and "段落2" in out


def test_strip_multiline_tag_content():
    """⟦...⟧ 标签内部可含多行（虽然实际少见）."""
    text = "前置。\n⟦ 任务\n多行\n摘要 ⟧\n后置。"
    out = strip_meta_tags(text)
    assert "⟦" not in out and "⟧" not in out
    assert "前置" in out and "后置" in out


# ---------------------------------------------------------------------------
# 注解扫描
# ---------------------------------------------------------------------------


def test_annotate_finds_tag_in_dict_string():
    obj = {"messages": [{"role": "assistant", "content": "x⟦ tag ⟧"}]}
    out = annotate_meta_tags(obj)
    assert out["has_meta_tag"] is True
    assert out["total_count"] == 1
    occ = out["occurrences"][0]
    assert occ["tag"] == "⟦ tag ⟧"
    assert "messages" in occ["path"]


def test_annotate_finds_tag_in_nested_list():
    obj = {"messages": [
        {"role": "assistant", "blocks": [
            {"type": "text", "text": "正常"},
            {"type": "text", "text": "⟦ 嵌入标签 ⟧"},
        ]},
    ]}
    out = annotate_meta_tags(obj)
    assert out["has_meta_tag"] is True
    assert out["total_count"] == 1


def test_annotate_no_tag_returns_empty():
    obj = {"k": "clean", "list": ["a", "b", "c"]}
    out = annotate_meta_tags(obj)
    assert out["has_meta_tag"] is False
    assert out["total_count"] == 0
    assert out["occurrences"] == []


def test_annotate_multiple_payloads():
    p1 = {"k": "x⟦ a ⟧"}
    p2 = {"k2": "z⟦ b ⟧"}
    out = annotate_meta_tags(p1, p2)
    assert out["total_count"] == 2


def test_annotate_handles_non_string_keys():
    """dict 的非字符串 key 也能递归."""
    obj = {1: "⟦ 整数 key ⟧", "k": "clean"}
    out = annotate_meta_tags(obj)
    assert out["total_count"] == 1


# ---------------------------------------------------------------------------
# 整 session payload 递归剥离
# ---------------------------------------------------------------------------


def test_strip_session_payload_dict():
    obj = {"a": "x⟦ t ⟧", "b": {"c": "y⟦ t2 ⟧"}}
    out = strip_session_payload(obj)
    assert "⟦" not in json.dumps(out, ensure_ascii=False)
    assert "x" in out["a"] and "y" in out["b"]["c"]


def test_strip_session_payload_list():
    obj = ["⟦ t ⟧", "normal", ["nested ⟦ t2 ⟧"]]
    out = strip_session_payload(obj)
    assert out[0] == ""
    assert out[1] == "normal"
    assert "⟦" not in out[2][0]


def test_strip_session_payload_preserves_non_strings():
    obj = {"n": 42, "f": 3.14, "b": True, "nl": None}
    out = strip_session_payload(obj)
    assert out == obj  # 完全不变


def test_strip_session_payload_empty():
    assert strip_session_payload({}) == {}
    assert strip_session_payload([]) == []


# ---------------------------------------------------------------------------
# 集成: save_session 必须对所有 4 个后缀文件做剥离 / 标注
# ---------------------------------------------------------------------------


def _build_session_with_meta_tag(tmp_path: Path) -> Path:
    """构造一个含 ⟦⟧ 的 session 并通过 save_session 写出 4 个后缀文件."""
    base_path = Path(tmp_path) / "test_refined"

    msg = Message(
        role="assistant",
        id="m0",
        blocks=[
            TextBlock(type="text", id="b0", text="结论段落。\n⟦ 任务｜已完成 ⟧"),
            ThinkingBlock(type="thinking", id="th0", thinking="内部思考。\n⟦ 思考标签 ⟧"),
        ],
    )
    sess = Session(
        session_id="test",
        messages=[msg],
        metadata={
            "qf_text": "结论段落。\n⟦ 任务｜已完成 ⟧\n",
            "openai_messages": [
                {"role": "assistant", "content": "结论段落。\n⟦ 任务｜已完成 ⟧"},
            ],
        },
    )
    save_session(sess, base_path)
    return base_path


def test_save_session_strips_qwenjina_txt(tmp_path: Path):
    base = _build_session_with_meta_tag(tmp_path)
    qj = base.with_suffix(".qwenjina.txt")
    # qwenjina 文件名形如 test_refined.qwenjina.txt
    candidates = list(tmp_path.glob("*.qwenjina.txt"))
    assert candidates, "qf_text 存在但 qwenjina.txt 未写出"
    text = candidates[0].read_text(encoding="utf-8")
    assert "⟦" not in text and "⟧" not in text
    assert "结论段落" in text


def test_save_session_strips_messages_json(tmp_path: Path):
    base = _build_session_with_meta_tag(tmp_path)
    candidates = list(tmp_path.glob("*.messages.json"))
    assert candidates
    payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    text_blob = json.dumps(payload, ensure_ascii=False)
    assert "⟦" not in text_blob and "⟧" not in text_blob
    assert "结论段落" in text_blob


def test_save_session_strips_openai_json(tmp_path: Path):
    base = _build_session_with_meta_tag(tmp_path)
    candidates = list(tmp_path.glob("*.openai.json"))
    assert candidates
    payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    text_blob = json.dumps(payload, ensure_ascii=False)
    assert "⟦" not in text_blob and "⟧" not in text_blob


def test_save_session_meta_json_has_annotation(tmp_path: Path):
    base = _build_session_with_meta_tag(tmp_path)
    candidates = list(tmp_path.glob("*.meta.json"))
    assert candidates
    meta = json.loads(candidates[0].read_text(encoding="utf-8"))
    ann = meta.get("meta_tag_contamination")
    assert ann is not None
    assert ann["has_meta_tag"] is True
    assert ann["total_count"] >= 1
    # occurrences 记录的是剥离后内容里的位置, 此时应全部为空
    for occ in ann["occurrences"]:
        assert occ["tag"] != ""


def test_save_session_clean_session_no_annotation(tmp_path: Path):
    sess = Session(
        session_id="clean",
        messages=[Message(role="assistant", id="m0", blocks=[
            TextBlock(type="text", id="b0", text="无任何标签的干净文本。"),
        ])],
        metadata={"qf_text": "无任何标签的干净文本。"},
    )
    save_session(sess, tmp_path / "clean_refined")
    meta = json.loads((tmp_path / "clean_refined.meta.json").read_text(encoding="utf-8"))
    ann = meta["meta_tag_contamination"]
    assert ann["has_meta_tag"] is False
    assert ann["total_count"] == 0