"""评分面出处台账（方案 16 C-11 补齐：版本级登记 + **面感知**可比性）。

为什么要有这个模块（C-11 留下的那半个缺口）
--------------------------------------------
C-11 做了两件事：冻结行写 ``scorer_version``（内容指纹）、分歧卡多三栏版本。
但它只覆盖**新冻结的版本**（v9 起）。**存量冻结档 v1–v8 一行都没有这一栏**，
而冻结档是**一字不许改**的（红线）⇒ 「这批 pred 是哪版评分器打的」对存量
数据仍然是 `unrecorded`。

看上去只能重跑 ``overlay-scout`` 重打分——**那条路是错的**：
``overlay-scout`` 会把**当前**评分器的读数写回 ``candidates.jsonl``，
那是「改数」（而且改的是中央文件）。M-29 之所以存在，正是因为「分数过期」
这件事一个字都不报。

本模块给的是**第三条路**：**版本级台账 + 重算取证**。

    冻结档不动 · 中央文件不动 · 只在**档外**落一份出处登记

台账怎么回答「这版是哪版评分器打的」
------------------------------------
分三个**面**（face）——这是本模块最要紧的一件事：

============= ========================================== ==================
面            谁产出该维的 pred                          怎么认定
============= ========================================== ==================
``code``      D-2 / D-4 / D-5 / D-6（**代码**评分器）      行里记了指纹 ⇒ 直接读；
                                                          没记 ⇒ **重算**：
                                                          用当前代码面重跑该版
                                                          的样本，逐行比对存档
                                                          里的读数
``llm``       D-3 / D-9（**LLM 裁判**）                   行里记了 prompt 指纹 ⇒ 直接读；
                                                          没记 ⇒ **不可判定**
``collection`` D-10（**采集侧** ``code.branch``）          判定侧版本**从未记录** ⇒
                                                          不可判定（且**不猜**）
============= ========================================== ==================

⚠️ **面不是同一件事，混起来就是 C-11 那半个缺口**：
D-2/D-4/D-5/D-6 的分数是**代码**打的，问「旧 prompt 还是新 prompt 判的」
对它们**根本不适用**；D-10 的 pred 是采集时写下的分支，连「评分器」都不是。
原来的 ``prompt_comparability()`` 一律拿 prompt 面去比 ⇒ 对代码面的行
**问错了问题**，对 D-10 的行**问了个不适用的问题**。

重算取证的效力边界（**说清，不夸大**）
--------------------------------------
「当前代码面逐行复现了本版读数」证明的是**读数层面的一致**，不是
「当时跑的就是这份源码」——源码当时长什么样没记，谁也证不了。
但可比性要的正是前者：拿这批读数跟当前读数对账，前提是「当前评分面能复现
它们」。复现得出来 ⇒ 可比；复现不出来 ⇒ **已知不可比**（不是「不可判定」）。

fail-closed
-----------
- 版本号不在台账里 ⇒ 出处栏写 ``unrecorded`` + 状态 ``unrecorded``，
  **不拿当前指纹顶替**（顶替会让「没量到」看起来像「量过且一致」）；
- 台账行缺 ``faces`` / 缺该面 ⇒ 同样按未记录处理，**不抛**——
  台账是**旁路账**，缺一条不能让整条链路断掉；
- 但**台账文件本身**读不成 JSON ⇒ 抛（半份台账比没有台账更坏）。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Iterable, Mapping

from trajectory_pipeline.evaluation.scorers.rubric_version import (
    VERSION_UNRECORDED,
    code_part,
    llm_part,
)

# ---------------------------------------------------------------------------
# 面（face）
# ---------------------------------------------------------------------------

#: **代码**评分器面——D-2 / D-4 / D-5 / D-6 的分数出自这里。
FACE_CODE = "code"
#: **LLM 裁判**面——D-3 / D-9 的分数出自这里（prompt + 锚点）。
FACE_LLM = "llm"
#: **采集侧**面——D-10 的 pred 是采集时写下的 ``code.branch``，不是评分器的产物。
FACE_COLLECTION = "collection"
#: 认不出的面（维度未登记）。**不猜**：落到这一档 ⇒ 不可判定。
FACE_UNKNOWN = "unknown"

#: 全部面（台账里逐面登记，顺序即报表顺序）。
FACES: tuple[str, ...] = (FACE_CODE, FACE_LLM, FACE_COLLECTION)

#: 维度 → 产出它那一侧 pred 的面。**唯一真源**：可比性判断与台账都从这里投影。
#:
#: - ``d7`` / ``d8`` 是**批次级**维度（B-2 / B-4），不出逐行分数 ⇒ 不登记
#:   ⇒ 落到 :data:`FACE_UNKNOWN`（不可判定），**不猜它们属于哪一面**。
DIM_FACES: dict[str, str] = {
    "d2": FACE_CODE,
    "d3": FACE_LLM,
    "d4": FACE_CODE,
    "d5": FACE_CODE,
    "d6": FACE_CODE,
    "d9": FACE_LLM,
    "d10": FACE_COLLECTION,
}


def face_of_dimension(dimension: str) -> str:
    """该维的 pred 出自哪一面；维度未登记 → ``unknown``（**不猜**）。

    登记在 :data:`DIM_FACES`。**不许手抄第二份**——抄两份必然漂，而漂了
    的后果是「拿错面比版本」，读数上看不出异常。
    """
    return DIM_FACES.get(str(dimension or ""), FACE_UNKNOWN)


# ---------------------------------------------------------------------------
# 面状态（face status）
# ---------------------------------------------------------------------------

#: 行里**记着**该面的版本（v9 起随行写入）。
STATUS_RECORDED = "recorded"
#: 行里没记，但**重算证明当前该面能逐行复现本版读数** ⇒ 与当前评分面可比。
STATUS_REPRODUCED = "reproduced"
#: 行里没记，且**重算证明当前该面复现不出本版读数** ⇒ **已知不可比**。
STATUS_DIVERGED = "diverged"
#: 什么都没记 ⇒ 不可判定。
STATUS_UNRECORDED = "unrecorded"
#: 该面与本版 pred **无关**（例：LLM 面——归档里没有 LLM 产的 pred）。
STATUS_NOT_APPLICABLE = "not_applicable"

FACE_STATUSES: tuple[str, ...] = (
    STATUS_RECORDED,
    STATUS_REPRODUCED,
    STATUS_DIVERGED,
    STATUS_UNRECORDED,
    STATUS_NOT_APPLICABLE,
)


def face_version_of(fingerprint: str | None, face: str) -> str:
    """从评分面指纹里取**该面**的版本段；取不到 → ``unrecorded``。

    指纹格式 ``code:<12位>|llm:<12位>``（见
    :func:`~...scorers.rubric_version.scorer_fingerprint`）。

    - ``code`` 面 → ``code:`` 段；
    - ``llm`` 面 → ``llm:`` 段；
    - ``collection`` 面 → **永远没有版本段**：采集侧版本从未被指纹化 ⇒
      ``unrecorded``（**不是空串**——空串会被读成「版本为空」）。
    """
    fp = str(fingerprint or "")
    if not fp:
        return VERSION_UNRECORDED
    if face == FACE_CODE:
        return code_part(fp)
    if face == FACE_LLM:
        return llm_part(fp)
    return VERSION_UNRECORDED


# ---------------------------------------------------------------------------
# 台账（版本级出处登记）
# ---------------------------------------------------------------------------

#: 台账文件名——落在黄金集数据根下，**与冻结档平级**（不在 ``v{N}/`` 里）。
#:
#: ⚠️ 为什么不能放进 ``v{N}/``：那一层是**冻结档**，一字不许改；台账是
#: **可重算的旁路账**，每次核验都可能更新它。
PROVENANCE_FILENAME = "scorer_provenance.jsonl"


def provenance_path(golden_root: pathlib.Path | str) -> pathlib.Path:
    """台账路径：``<golden_root>/scorer_provenance.jsonl``。"""
    return pathlib.Path(golden_root) / PROVENANCE_FILENAME


def read_provenance(path: pathlib.Path | str) -> dict[int, dict[str, Any]]:
    """读台账 → ``{版本号: 行}``。

    文件不存在 → **空字典**（不是错误）：台账是旁路账，没建过就是没建过。

    文件存在但**行读不成** ⇒ 抛 ``ValueError``：半份台账比没有台账更坏
    （读的人会以为「没记录」＝「没差异」）。
    """
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    out: dict[int, dict[str, Any]] = {}
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p} 第 {lineno} 行不是合法 JSON：{exc}") from exc
        if not isinstance(row, Mapping):
            raise ValueError(f"{p} 第 {lineno} 行不是对象：{row!r}")
        try:
            version = int(row["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{p} 第 {lineno} 行缺 version（或不是整数）") from exc
        out[version] = dict(row)
    return out


def write_provenance(path: pathlib.Path | str, rows: Iterable[Mapping[str, Any]]) -> pathlib.Path:
    """落台账（按版本号排序，一行一版）。

    **排序**：按版本号升序 ⇒ 同一份台账反复重写不产生 diff 噪声。
    """
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        (dict(r) for r in rows),
        key=lambda r: int(r.get("version") or 0),
    )
    p.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in ordered) + "\n",
        encoding="utf-8",
    )
    return p


def face_record(
    version_row: Mapping[str, Any] | None, face: str
) -> dict[str, Any]:
    """台账里某版某面的记录；没有 → **未记录的空记录**（不抛）。

    返回的字典至少含 ``version`` / ``status`` / ``comparable`` 三键，
    缺的用 ``unrecorded`` / ``None`` 补齐——**缺一条不能让链路断**，
    但也不许把「没记」读成「一致」。
    """
    empty = {
        "version": VERSION_UNRECORDED,
        "status": STATUS_UNRECORDED,
        "comparable": None,
        "evidence": {},
        "note": f"台账里没有 v{version_row.get('version') if version_row else '?'} "
                f"的 {face} 面记录",
    }
    if not version_row:
        return empty
    faces = version_row.get("faces")
    if not isinstance(faces, Mapping):
        return empty
    rec = faces.get(face)
    if not isinstance(rec, Mapping):
        return empty
    out = dict(empty)
    out.update({
        "version": str(rec.get("version") or VERSION_UNRECORDED),
        "status": str(rec.get("status") or STATUS_UNRECORDED),
        "comparable": rec.get("comparable"),
        "evidence": rec.get("evidence") or {},
        "note": str(rec.get("note") or ""),
    })
    return out


__all__ = [
    "DIM_FACES",
    "FACE_CODE",
    "FACE_COLLECTION",
    "FACE_LLM",
    "FACE_STATUSES",
    "FACE_UNKNOWN",
    "FACES",
    "PROVENANCE_FILENAME",
    "STATUS_DIVERGED",
    "STATUS_NOT_APPLICABLE",
    "STATUS_RECORDED",
    "STATUS_REPRODUCED",
    "STATUS_UNRECORDED",
    "face_of_dimension",
    "face_record",
    "face_version_of",
    "provenance_path",
    "read_provenance",
    "write_provenance",
]
