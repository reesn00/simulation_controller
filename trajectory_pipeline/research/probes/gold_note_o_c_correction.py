"""改掉真源里**一句写错的话**：「O-c 仍待主人裁」。

错在哪
------
2026-10-30 那一轮给回归集判软维度时，有 7 行的 ``dimension_note`` 里写了
「⚠️ **O-c 仍待主人裁**（unreachable_hard 该不该进负样本池）」。

**O-c 已经闭了**，而且闭了两步：

- **Q-17**（2026-10-27，owner）：「**先裁再跑**」——「这次没打开」（超时 /
  打断 / 建页失败 / 会话失效 / 快照失败）从 ``unreachable_hard`` 降成
  ``unresolved``（**不进池**），只留「**真·站点不可达**」（机器码证明：
  DNS 解析不出来 / 连接被拒）；
- **Q-23**（2026-10-28）：存量 54 条 ``unreachable_hard`` 逐条真浏览器复核
  ⇒ **40 条出池**、14 条留池。

⇒ 那 7 行 note 里的「仍待主人裁」是**错的**，会让人以为还有一个开口没裁。

改什么
------
**只改 ``dimension_note`` 里那一句**，**分数一个都不碰**、标签一个都不碰、
署名与时刻一个都不碰（凭据不重复写）。**金标变动 = 新基线** 那条纪律针对的
是**分数与标签**；改判读理由的文字不改任何读数 ⇒ 不需要新版本。

⚠️ **为什么这件事值得单开一个脚本**：真源是**唯一可写入口**，改它的每一步
都得留下可核对的痕迹 ⇒ 本脚本干跑时逐条报「哪一行、哪一句改成了什么」，
``--write`` 才落盘；落盘后它自己再核一遍「**除 ``dimension_note`` 外一个字段
都没变**」。

跑法（**必须在仓库根跑**）::

    python trajectory_pipeline/research/probes/gold_note_o_c_correction.py            # 干跑
    python trajectory_pipeline/research/probes/gold_note_o_c_correction.py --write    # 落盘
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
ANN = REPO / "evaluation" / "golden" / "data" / "annotations.jsonl"

#: 写错的那一句（原样，长度必须对得上 ⇒ 不匹配就退 2，不猜着改）。
STALE = (
    "⚠️ **O-c 仍待主人裁**（unreachable_hard 该不该进负样本池）："
    "本行按「底料已 relabel 后的落点」判，池归属跟着 relabel 走。"
)

#: 改成的句子。**只陈述已发生的事**：裁定已闭、存量已执行；剩下的是
#: 「金标标签还没跟上」⇒ 那件事归 M-57，不是 O-c。
FIXED = (
    "⚠️ **O-c 已闭**（owner 2026-10-27 裁 Q-17：这类「这次没打开」不算"
    "「站点不可达」⇒ 落 `unresolved`、**不进池**；2026-10-28 Q-23 存量逐条"
    "真浏览器复核已执行：54 条中 40 条出池、14 条真不可达留池）⇒ 本行按"
    "「底料已 relabel 后的落点」判，池归属跟着 relabel 走。"
    "⚠️ **剩下要人做的是把金标标签跟上**（金标仍写旧分支名）——归 **M-57**，"
    "不是 O-c。"
)


def main() -> int:
    # Windows 控制台默认 GBK ⇒ 打印 ✅ 会炸（踩过）⇒ 强制 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - 老 Python 没有这个 API
        pass
    ap = argparse.ArgumentParser(description="改掉真源里「O-c 仍待主人裁」这句错话")
    ap.add_argument("--write", action="store_true", help="落盘（缺省只干跑）")
    args = ap.parse_args()

    raw = ANN.read_text(encoding="utf-8")
    lines = [line for line in raw.splitlines() if line.strip()]
    rows = [json.loads(line) for line in lines]

    hits: list[tuple[int, dict]] = []
    for i, row in enumerate(rows):
        note = str(row.get("dimension_note") or "")
        if STALE in note:
            hits.append((i, row))
    print(f"真源 {len(rows)} 行 · 命中这句错话的 {len(hits)} 行")
    for i, row in hits:
        print(f"  [{i}] {row.get('golden_id')}（round={row.get('round', 1)}）")

    if not hits:
        print("没有命中 ⇒ 已经是改过的状态（幂等）")
        return 0

    changed = 0
    for i, row in hits:
        note = str(row.get("dimension_note") or "")
        new_note = note.replace(STALE, FIXED)
        assert new_note != note, f"{row.get('golden_id')}：替换后没变 ⇒ 原句不对"
        row["dimension_note"] = new_note
        changed += 1
    print(f"改写 {changed} 行的 dimension_note")

    if not args.write:
        print("干跑（加 --write 才落盘）")
        return 0

    # 落盘：保持「每行一个 JSON」的既有排版（不重排键序 ⇒ diff 只落在这句上）。
    out_lines = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows]
    ANN.write_text("\n".join(out_lines) + "\n", encoding="utf-8")

    # 自核：除 dimension_note 外，一个字段都没变。
    check = [json.loads(line) for line in ANN.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(check) != len(rows):
        print(f"❌ 行数变了：{len(rows)} → {len(check)}")
        return 2
    bad: list[str] = []
    for before, after in zip(rows, check):
        b = {k: v for k, v in before.items() if k != "dimension_note"}
        a = {k: v for k, v in after.items() if k != "dimension_note"}
        if b != a:
            bad.append(str(before.get("golden_id")))
    if bad:
        print(f"❌ 这些行除了 dimension_note 还有别的字段变了：{bad}")
        return 2
    print(f"✅ 已落盘并自核：{len(rows)} 行，**除 dimension_note 外无任何字段变化**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
