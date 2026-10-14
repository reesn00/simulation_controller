"""reviewer-only —— 4 处 ``_observation_shortfall`` 措辞差异矩阵。

## 为什么需要这个工具

:func:`trajectory_pipeline.tests.contract.test_observation_shortfall_consistency`
盯的是「能不能判」的**决策边界**——同输入四处必须返回一致（空 / 非空）。
但**字面措辞**允许有差异：

  - ``gates.observation_shortfall`` 在 truncated 消息里夹了「CSS 会抢光预算」
    之类的额外说明
  - ``code_scorer._observation_shortfall`` 在 degraded 消息里强调「缺项无法
    与『真没有』区分」
  - ``d3_scorer._observation_shortfall`` / ``d9_scorer._observation_shortfall``
    走中性版本

这些措辞漂移**不**破功能正确（决策边界已守住），但当 reviewer 想确认
「这条样本在四处看到的具体原因是什么」时，需要把措辞打 side-by-side
做对照。本工具即为此而生。

## 不是 pytest 测试

本文件命名 ``observation_shortfall_wording_diff.py`` 而非 ``test_*.py``，
pytest 不会自动收集。它是 standalone CLI：

.. code-block:: bash

    # 默认 markdown（贴到 review 友好）
    .venv/Scripts/python.exe -m trajectory_pipeline.tests.contract.tools.observation_shortfall_wording_diff

    # 三种格式可选
    .venv/Scripts/python.exe -m trajectory_pipeline.tests.contract.tools.observation_shortfall_wording_diff --format table
    .venv/Scripts/python.exe -m trajectory_pipeline.tests.contract.tools.observation_shortfall_wording_diff --format unified

    # 也支持直接跑
    .venv/Scripts/python.exe trajectory_pipeline/tests/contract/tools/observation_shortfall_wording_diff.py

## 输出形态

对每条「不可判」case：

  - **table** — 5 列对齐（case id + 4 处措辞）。终端友好
  - **unified** — diff 风格（以 gates 为基线，标 - / + 偏离）
  - **markdown** — Markdown 表格（贴 GitHub / GitLab review 友好，**默认**）

另带「可判」基线（``can_judge`` 形态）作 sanity check。

## 退出码

  - ``0`` —— 工具运行成功（**不论**措辞是否一致；这是 reviewer 视角不是
    contract 视角，不应该让 CI 挂红）
  - ``1`` —— 内部错误（如 import 失败）
"""

from __future__ import annotations

import argparse
import difflib
import sys
from typing import Any, Mapping


# Windows 默认 stdout 编码是 GBK (cp936)，输出含中文时会乱码。
# 这里强制 UTF-8，避免 review 工具拉到错码——reviewer 多半在 GitHub
# 上看，希望所见即所明。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


# ── 复用 contract test 的 4 笔 case（不动决策边界，只看措辞） ──────


def _obs(**kw: Any) -> Mapping[str, Any]:
    """同 :func:`test_observation_shortfall_consistency._obs` —— 决策边界
    助记路径，措辞看实际值。"""
    base = {
        "body_text": "页面有 video 标签 + iframe 内嵌",
        "interactive_elements": [{"tag": "a", "label": "链接0"}],
        "elements_total": 1,
        "links": [],
        "links_total": 0,
        "degraded": [],
        "truncated": False,
    }
    base.update(kw)
    return base


# 与 contract 测试同款的 4 笔「不可判」case + 1 笔「可判」基线
CASES: list[tuple[str, Mapping[str, Any]]] = [
    ("can_judge（基线）", _obs()),
    ("degraded-非空", _obs(degraded=["video_tag_count"])),
    ("truncated-真", _obs(truncated=True)),
    ("elements-被裁剪",
     _obs(interactive_elements=[{"tag": "a", "label": "L"}],
          elements_total=5)),
    ("body-与-elements-皆空", _obs(body_text="", interactive_elements=[])),
]


# 4 处实现的固定顺序
IMPLS: list[tuple[str, Any]] = [
    ("gates", __import__("trajectory_pipeline.rationale.gates",
                          fromlist=["observation_shortfall"])
     .observation_shortfall),
    ("code_scorer", __import__("trajectory_pipeline.evaluation.scorers.code_scorer",
                                fromlist=["_observation_shortfall"])
     ._observation_shortfall),
    ("d3_scorer", __import__("trajectory_pipeline.evaluation.scorers.d3_scorer",
                              fromlist=["_observation_shortfall"])
     ._observation_shortfall),
    ("d9_scorer", __import__("trajectory_pipeline.evaluation.scorers.d9_scorer",
                              fromlist=["_observation_shortfall"])
     ._observation_shortfall),
]


# ── 数据采集 ──────────────────────────────────────────────────────


def collect_matrix() -> list[tuple[str, dict[str, str]]]:
    """跑 4 处实现 × N 个 case，返回 ``[(case_id, {impl: msg}, ...]``。

    公开为函数（不只 CLI 调用）方便复用 / 单测。
    """
    matrix: list[tuple[str, dict[str, str]]] = []
    for case_id, obs in CASES:
        row: dict[str, str] = {}
        for impl_name, impl_fn in IMPLS:
            try:
                row[impl_name] = impl_fn(obs)  # type: ignore[arg-type]
            except Exception as e:
                # code_scorer 不接受 obs=None 是已知能力差异——本工具不背锅
                row[impl_name] = f"<{type(e).__name__}: {e}>"
        matrix.append((case_id, row))
    return matrix


# ── 三种渲染 ──────────────────────────────────────────────────────


def _render_table(matrix: list[tuple[str, dict[str, str]]]) -> str:
    """纯文本对齐表——终端友好。"""
    col_w = {"case": 22}
    for impl_name, _ in IMPLS:
        col_w[impl_name] = max(48, len(impl_name) + 2)
    # 收集所有 cell 求 max 宽
    for _case_id, row in matrix:
        for impl_name, msg in row.items():
            # 处理多行（围栏内部换行）：取最大行宽
            line_w = max((len(c) for c in msg.splitlines()), default=0)
            if line_w > col_w[impl_name]:
                col_w[impl_name] = line_w

    headers = ["case"] + [impl_name for impl_name, _ in IMPLS]
    lines: list[str] = []
    lines.append("  ".join(h.ljust(col_w[h]) for h in headers))
    lines.append("  ".join("-" * col_w[h] for h in headers))
    for case_id, row in matrix:
        cells = [case_id.ljust(col_w["case"])]
        for impl_name, _ in IMPLS:
            cells.append(row[impl_name].ljust(col_w[impl_name]))
        lines.append("  ".join(cells))
    return "\n".join(lines)


def _render_unified(matrix: list[tuple[str, dict[str, str]]]) -> str:
    """以 gates 为基线，difflib 输出 side-by-side 偏离——纯文本 diff。"""
    base_impl = IMPLS[0][0]
    chunks: list[str] = []
    for case_id, row in matrix:
        chunks.append(f"--- {case_id} ---")
        base = row.get(base_impl, "<missing>")
        # 自身 diff 无意义；标 base + 其他三个 diff
        chunks.append(f"  {base_impl}: {base!r}")
        for impl_name, _ in IMPLS[1:]:
            other = row.get(impl_name, "<missing>")
            diff_lines = list(difflib.unified_diff(
                base.splitlines(keepends=False),
                other.splitlines(keepends=False),
                fromfile=base_impl, tofile=impl_name, lineterm="",
            ))
            if not diff_lines:
                chunks.append(f"  {impl_name}: {other!r}（与基线一致）")
            else:
                chunks.append(f"  {impl_name}:")
                for dl in diff_lines:
                    chunks.append(f"    {dl}")
        chunks.append("")
    return "\n".join(chunks)


def _render_markdown(matrix: list[tuple[str, dict[str, str]]]) -> str:
    """Markdown 表格——贴 review 友好（**默认**）。"""
    headers = ["case"] + [impl_name for impl_name, _ in IMPLS]
    lines: list[str] = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for case_id, row in matrix:
        cells = [case_id]
        for impl_name, _ in IMPLS:
            msg = row.get(impl_name, "<missing>")
            # Markdown 转义：`|` 替换为 `\|`，换行符替换为 `<br>`
            safe = msg.replace("|", "\\|").replace("\n", "<br>")
            cells.append(safe)
        lines.append("| " + " | ".join(cells) + " |")
    # 注解：可判形态四处必须空串
    lines.append("")
    lines.append("> 注：")
    lines.append("> 1. ``can_judge（基线）`` 行——四处均应返回**空串**（可判）。")
    lines.append("> 2. ``<missing>`` —— 该实现跳过该 case（code_scorer 不接受 "
                 "``obs=None`` 是已知能力差异，见 contract 测试 fixture 表）。")
    lines.append("> 3. 表中字符串是**字面措辞**，与 contract 测试的「决策边界"
                 " 一致」**正交**——决策边界对齐不代表措辞一致。")
    return "\n".join(lines)


RENDERERS = {
    "table": _render_table,
    "unified": _render_unified,
    "markdown": _render_markdown,
}


# ── CLI ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="4 处 _observation_shortfall 措辞漂移矩阵",
    )
    parser.add_argument(
        "--format", choices=sorted(RENDERERS),
        default="markdown",
        help="输出格式（默认 markdown）",
    )
    args = parser.parse_args(argv)

    matrix = collect_matrix()
    renderer = RENDERERS[args.format]
    output = renderer(matrix)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
