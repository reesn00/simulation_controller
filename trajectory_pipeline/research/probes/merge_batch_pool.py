"""把**一批的落盘产物**并进仓库根的 P1 存档与负样本池（**默认 dry-run**）。

⚠️ **这不是探针**，是批次的后处理入口——放在 ``research/`` 是因为它
**离线、可重跑、只读存量 + 只写增量**（产品代码不收一次性运维脚本）。

## 为什么需要它

``cli run --out <子目录>`` 跑完一批，产物落在**子目录**里：

- P1 存档 → ``<子目录>/<task_id>__<run_id>.json``
- 负样本池 → ``<子目录>/negative.jsonl``（池**与 P1 同根**，
  见 :meth:`P1Archive.negative_pool_path`）

而**下游只认仓库根那一层**：

| 读侧 | 扫法 | 后果 |
|---|---|---|
| :func:`trajectory_pipeline.executor.archive.select_archives` | ``root.glob("*.json")`` | 子目录里的存档**看不见** ⇒ 报表分母少 |
| :func:`trajectory_pipeline.assembler.builder.build_all` | ``src.glob("*.json")`` | 子目录里的存档**切不出样本** |
| :func:`trajectory_pipeline.executor.dual_run.load_runs` | 同上 | 双跑对账看不见 |

⇒ 批次跑完必须**展平**（P1 存档挪到根）+ **并池**（子池并进根池）。

⚠️ **为什么这两处只扫一层、不是漏**：根目录下躺着 ``mediabrowse/``
（1400+ 份）、``dualrun/``、``proberun/``、``samples/``、``views/``、
``task_views/`` 这些**别的资产类**；改成递归会把它们**重复计进分母**
（分母错在这张表上会直接改结论，见 :func:`select_archives` 的注释）。
⇒ **批次存档就该落在根那一层**，子目录只放**别的资产类**。

## 幂等与去重

- 池的去重键是 ``(task_id, url, branch)``——**与
  :meth:`P1Archive._append_negatives` 同一口径**（不手抄第二份）。
- 同名存档已在根目录 ⇒ **跳过并报出来**（不覆盖：存档是不可变真值源）。
- 池行已存在 ⇒ 跳过。

⚠️ **回滚与并池的顺序不能反**：先并池再回滚 ⇒ 批次与被回滚那一代**共用
去重键**时，批次那行会因为「键已存在」被**跳过** ⇒ 紧接着的回滚把那一行
删掉 ⇒ **批次的那条结论整个丢了**（既没进池、也没了存档）。
⇒ **先回滚（``cli purge-run --apply``）、再并池**。实测（2026-10-22）：
两代共 12 个 task，重叠键 39 个；先并池的话这 39 条里**有多少会被吞掉
取决于顺序**——先并后回 = 全丢。

用法（**必须在仓库根跑**）::

    python trajectory_pipeline/research/probes/merge_batch_pool.py \
        --batch trajectory_pipeline/output/pipeline/camoufox_prod2 \
        --root trajectory_pipeline/output/pipeline

    # 确认清单后：
    python trajectory_pipeline/research/probes/merge_batch_pool.py ... --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from trajectory_pipeline.executor.archive import _pool_separator, scan_credentials


def _utf8_console() -> None:
    """把标准输出改成 utf-8（Windows 控制台默认 GBK：中文花屏、``⇒`` 直接抛）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):    # pragma: no cover - 老解释器
            pass


def _is_archive(path: Path) -> bool:
    """**只认存档形状** ``<task_id>__<run_id>.json``。

    与 :func:`archive.select_archives` 同一条规则（探针取证不是存档）。
    """
    if path.suffix != ".json" or path.name.endswith(".reviewed.json"):
        return False
    return path.stem.count("__") == 1


def _pool_keys(path: Path) -> set[tuple[str, str, str]]:
    """读池内已有的去重键。

    **逐行解析**：文件被截断时末行可能是半截 JSON，那一行**丢弃**
    （与 :meth:`P1Archive._load_negative_keys` 同一处置）。
    """
    keys: set[tuple[str, str, str]] = set()
    if not path.exists():
        return keys
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        keys.add((str(row.get("task_id") or ""),
                  str(row.get("url") or ""),
                  str(row.get("branch") or "")))
    return keys


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(description="把一批的 P1 存档与负样本池并进根目录")
    ap.add_argument("--batch", required=True, help="批次目录（例：output/pipeline/camoufox_prod2）")
    ap.add_argument("--root", default="trajectory_pipeline/output/pipeline",
                    help="仓库根的 P1 存档 + 池目录（默认 output/pipeline）")
    ap.add_argument("--apply", action="store_true", help="真改（默认只列不改）")
    args = ap.parse_args(argv)

    batch = Path(args.batch)
    root = Path(args.root)
    if not batch.is_dir():
        print(f"[FAIL] 批次目录不存在: {batch}")
        return 2
    if not root.is_dir():
        print(f"[FAIL] 根目录不存在: {root}")
        return 2
    if batch.resolve() == root.resolve():
        print("[FAIL] 批次目录就是根目录——没有东西要并。")
        return 2

    archives = sorted(p for p in batch.glob("*.json") if _is_archive(p))
    conflicts = [p for p in archives if (root / p.name).exists()]
    movable = [p for p in archives if p not in conflicts]

    batch_pool = batch / "negative.jsonl"
    root_pool = root / "negative.jsonl"
    root_keys = _pool_keys(root_pool)
    #: **并池前的根池键数**——循环里 ``root_keys`` 会被就地补进批次的新键，
    #: 事后打 ``len(root_keys)`` 会把「并完之后的数」当成「并之前的数」
    #: （读的人会以为根池本来就有那么多键）。
    root_keys_before = len(root_keys)
    fresh: list[dict] = []
    dupes = 0
    if batch_pool.exists():
        for line in batch_pool.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            key = (str(row.get("task_id") or ""),
                   str(row.get("url") or ""),
                   str(row.get("branch") or ""))
            if key in root_keys:
                dupes += 1
                continue
            root_keys.add(key)
            fresh.append(row)

    print(f"批次目录: {batch}")
    print(f"根目录  : {root}\n")
    print(f"P1 存档：{len(archives)} 份"
          f"（可挪 {len(movable)} · 同名已存在 {len(conflicts)}）")
    for path in movable:
        print(f"  + {path.name}")
    for path in conflicts:
        print(f"  ! 同名已存在，**不覆盖**（存档是不可变真值源）：{path.name}")

    print(f"\n负样本池：根池并前 {root_keys_before} 键"
          f" · 批次池新增 {len(fresh)} 行 · 已存在跳过 {dupes} 行"
          f" · 并后 {len(root_keys)} 键")

    blob = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in fresh)
    if blob:
        hits = scan_credentials(blob)
        if hits:
            print(f"[FAIL] 待并入的池行命中 {len(hits)} 项凭据特征，拒绝并池：{hits[:3]}")
            return 2

    if not args.apply:
        print("\n以上为**预演**。确认无误后加 --apply 执行。")
        return 0

    for path in movable:
        shutil.move(str(path), str(root / path.name))
    if fresh:
        with root_pool.open("a", encoding="utf-8") as fh:
            fh.write(_pool_separator(root_pool) + blob)
    print(f"\n已并入：存档 {len(movable)} 份，池行 {len(fresh)} 条。")
    print("接着跑：cli build --overwrite  →  cli check-archives")
    return 0


if __name__ == "__main__":
    sys.exit(main())
