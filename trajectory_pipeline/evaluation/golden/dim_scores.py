"""软维度金标的**写入规则**（方案 16 · 让 d2 / d4 / d5 / d6 不再是 `ungraded`）。

它解决的那个具体问题
--------------------
黄金集的 `dimension_scores` 至今**一条都没填**（294 行全是 `null`），于是
校准回路的条款级读数只覆盖 D-10：T-2 / T-3 测不成，抽样 S2 / S3 两层总体为 0，
v8 快照 1133 条里 **844 条 `ungraded`**。

填它需要三样东西，本模块是最底下那一样：**一条只改分数、不碰标签的写入路径**。

为什么不能直接用 `annotate`
---------------------------
`cmd_annotate` 的一条输入是**整条标注**：`label` 必填，`dimension_scores`
是它的一个字段。拿它补分数有两个坑：

1. **标签会被一起重写**——补 d5 的人手里往往没有"这条该归哪个分支"的裁定，
   为了走通命令去抄一个标签，等于**伪造金标**；
2. **`dimension_scores: null` 会把已有分数抹掉**（整行替换语义）。

所以这里另立一条路：**按 `(golden_id, dimension)` 合并，只动分数**。
`label` / `label_label` / `round` / `interval_days` 一个字不碰；
谁填的分数记在 `dimension_reviewed_by` / `dimension_reviewed_at` 两栏——
**不覆盖标签的原署名人**（那是另一件事的账）。

三条硬规矩
----------
1. **只能补已存在的标注**：`golden_id` 在 `annotations.jsonl` 里没有 round=1
   行 ⇒ 报错。金标分数不能凭空造出一条样本。
2. **`null` 是结论，不是空**：人判"这条这一维判不了"就写 `null`——键在、
   值为空，与"没填"（键不在）分得开。两者进快照都是 `ungraded`，但
   **覆盖率**看得出来（「91 条里 40 条人判过、51 条没人看」≠「91 条都没看」）。
3. **默认不覆盖已填的分**：同键同值 = 幂等跳过；同键不同值 = 报错，
   要用 `--force`（与 `triage.add_rows` 同款口径——人改判要留痕，不能悄悄盖）。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from trajectory_pipeline.evaluation.golden.alignment import SOFT_DIM_NAMES


class DimScoreError(ValueError):
    """分数不合法 / 找不到对应的标注。**一律抛，不降级。**"""


#: 一维的合法取值：1–5 整数，或 ``None``（人判"判不了"）。
TIER_RANGE = (1, 5)


def normalize_scores(raw: Mapping[str, Any] | None, *, where: str = "") -> dict[str, int | None]:
    """校验并归一化一条 `dimension_scores`。

    - 维度名必须在 :data:`~...alignment.SOFT_DIM_NAMES` 白名单内（拼错的 dim
      算进一致率等于把价值丢了，与 ``validate_soft_dim_name`` 同口径）；
    - 值只能是 1–5 的**整数**或 ``None``——`4.5` / `"4"` / `True` 一律拒：
      ``True`` 是子类 int，放过它会变成 1 分；
    - 至少要有一个键（空 dict 是"什么都没填"，不是"填了判不了"）。
    """
    if not isinstance(raw, Mapping) or not raw:
        raise DimScoreError(f"{where} dimension_scores 须为非空 dict（{{dim: 1..5|null}}）")
    out: dict[str, int | None] = {}
    for key, value in raw.items():
        dim = str(key)
        if dim not in SOFT_DIM_NAMES:
            raise DimScoreError(
                f"{where} 维度 {dim!r} 不在软维度白名单 {sorted(SOFT_DIM_NAMES)} 内"
            )
        if value is None:
            out[dim] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise DimScoreError(f"{where} {dim}={value!r} 不是 1–5 的整数（或 null）")
        lo, hi = TIER_RANGE
        if not (lo <= value <= hi):
            raise DimScoreError(f"{where} {dim}={value} 越界，允许 {lo}..{hi}")
        out[dim] = int(value)
    return out


def _cell_value(value: Any) -> Any:
    """把工作表里的一个格子读成分数：``""``（没填）→ 哨兵；``"na"`` → ``None``。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "":
            return _EMPTY
        if text in {"na", "n/a", "-", "判不了"}:
            return None
        if text.isdigit():
            return int(text)
        return value  # 交给 normalize 报错，别在这里猜
    return value


#: 「这一格还没填」的哨兵。与 ``None``（人判"判不了"）**不是一回事**。
_EMPTY = object()


def group_fills(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把输入行按 ``golden_id`` 归并成「每条样本一份分数」。

    接受两种输入形态（都在同一条命令里）：

    - **工作表行**（一维一行）：``{golden_id, dimension, gold_score}``；
    - **手写补丁**（一行多维）：``{golden_id, dimension_scores: {d5: 4}}``。

    空格子（``""``）跳过——**没填不是判不了**。一条样本一个维度都没填时
    整条跳过，不产出空补丁（空补丁会被 :func:`merge_dimension_scores` 拒掉，
    但那句话对填表的人没有意义）。
    """
    order: list[str] = []
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        gid = str(row.get("golden_id") or "")
        if not gid:
            raise DimScoreError(f"输入行缺 golden_id：{row!r}")
        bucket = grouped.get(gid)
        if bucket is None:
            bucket = {
                "golden_id": gid,
                "dimension_scores": {},
                "reviewed_by": str(row.get("reviewed_by") or ""),
                "reviewed_at": str(row.get("reviewed_at") or ""),
                "note": str(row.get("gold_note") or row.get("note") or ""),
            }
            grouped[gid] = bucket
            order.append(gid)

        if "dimension" in row:
            dim = str(row.get("dimension") or "")
            if not dim:
                raise DimScoreError(f"{gid} 的工作表行缺 dimension")
            if "gold_score" not in row:
                # 整栏省掉是**另一个意思**吗？不是，但它一定要报出来：
                # `gold_score` 缺席会被 `dict.get` 读成 None，而 None 是
                # 「人判判不了」——那会把半张表静默写成一堆 null。
                raise DimScoreError(
                    f"{gid} / {dim} 的工作表行里没有 gold_score 栏；"
                    "没填的格子写空串，**别整栏省掉**（省掉会被读成「判不了」）"
                )
            value = _cell_value(row.get("gold_score"))
            if value is _EMPTY:
                continue
            if dim in bucket["dimension_scores"]:
                raise DimScoreError(f"{gid} / {dim} 在输入里出现了两次")
            bucket["dimension_scores"][dim] = value
            # 署名人以**具体格子**为准：同一张表里不同行可能不同人填。
            if row.get("reviewed_by"):
                bucket["reviewed_by"] = str(row["reviewed_by"])
            if row.get("reviewed_at"):
                bucket["reviewed_at"] = str(row["reviewed_at"])
        else:
            raw = row.get("dimension_scores")
            if raw is None:
                continue
            if not isinstance(raw, Mapping):
                raise DimScoreError(f"{gid} dimension_scores 须为 dict")
            for dim, value in raw.items():
                if str(dim) in bucket["dimension_scores"]:
                    raise DimScoreError(f"{gid} / {dim} 在输入里出现了两次")
                bucket["dimension_scores"][str(dim)] = value

    out = []
    for gid in order:
        bucket = grouped[gid]
        if bucket["dimension_scores"]:
            out.append(bucket)
    return out


def merge_dimension_scores(
    annotations: Sequence[Mapping[str, Any]],
    fills: Sequence[Mapping[str, Any]],
    *,
    allow_overwrite: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """把分数合并进 ``annotations.jsonl`` 的行。**只动分数那几栏。**

    返回 ``(合并后的行, 统计)``。统计里有三样必报的：

    - ``changed`` / ``unchanged``：真的写了几条、幂等跳过了几条；
    - ``judged_na``：人判"判不了"（写 ``null``）的格子数——**它不是没填**；
    - ``per_dim``：每个维度这次新填了几条。读数会按这个数算覆盖率。

    fail-closed 的两处：找不到 round=1 标注 ⇒ 列出**全部**缺失 id；同键已有
    非空分且不同 ⇒ 报错（``--force`` 才覆盖）。**留着旧分不说，比报错更糟**。
    """
    merged = [dict(r) for r in annotations]
    index: dict[str, int] = {}
    for i, row in enumerate(merged):
        if int(row.get("round", 1)) != 1:
            continue
        index[str(row.get("golden_id") or "")] = i

    missing = [str(f.get("golden_id") or "") for f in fills if str(f.get("golden_id") or "") not in index]
    if missing:
        raise DimScoreError(
            "这些 golden_id 在 annotations.jsonl 里没有 round=1 标注（"
            f"{len(missing)} 个）：{' / '.join(missing)}；"
            "先跑 annotate 补标签——**分数不能凭空造出一条样本**"
        )

    stats: dict[str, Any] = {
        "rows_in": len(fills),
        "changed": 0,
        "unchanged": 0,
        "judged_na": 0,
        "per_dim": {},
        "overwritten": 0,
    }
    for fill in fills:
        gid = str(fill.get("golden_id") or "")
        if not str(fill.get("reviewed_by") or "").strip():
            raise DimScoreError(
                f"{gid} 缺 reviewed_by：金标分数要人签——「谁填的」这一栏空着，"
                "分数与机器猜的就分不开了"
            )
        row = merged[index[gid]]
        previous = row.get("dimension_scores")
        previous = dict(previous) if isinstance(previous, Mapping) else {}
        incoming = normalize_scores(fill.get("dimension_scores"), where=gid)
        patch: dict[str, Any] = {}
        for dim, value in incoming.items():
            # 「有没有判过」看**键在不在**，不看值：`{"d5": None}` 是「人判
            # 判不了」，`{}` 才是「没填」。拿 `.get()` 判断会把 null 当成没填
            # ——那一格既写不进去（变化检测读成 unchanged），也拦不住覆盖。
            judged = dim in previous
            old = previous.get(dim)
            if judged and old != value and not allow_overwrite:
                raise DimScoreError(
                    f"{gid} / {dim} 已有分数 {old}，新值 {value} 不一致；"
                    "确认后加 --force（人改判要留痕，不能悄悄盖）"
                )
            if judged and old != value:
                stats["overwritten"] += 1
            if value is None:
                stats["judged_na"] += 1
            stats["per_dim"][dim] = stats["per_dim"].get(dim, 0) + 1
            patch[dim] = value
        if not patch:
            continue
        if all(dim in previous and previous[dim] == v for dim, v in patch.items()):
            stats["unchanged"] += 1
            continue
        row["dimension_scores"] = {**previous, **patch}
        if fill.get("reviewed_by"):
            row["dimension_reviewed_by"] = str(fill["reviewed_by"])
        if fill.get("reviewed_at"):
            row["dimension_reviewed_at"] = str(fill["reviewed_at"])
        if fill.get("note"):
            row["dimension_note"] = str(fill["note"])
        stats["changed"] += 1
    return merged, stats


def coverage(
    rows: Iterable[Mapping[str, Any]], dims: Iterable[str]
) -> dict[str, dict[str, int]]:
    """每个维度在**给定行集**上的金标覆盖率（``filled`` / ``judged_na`` / ``empty``）。

    「人判过但判不了」单列：把它并进 ``empty``，覆盖率就会永远上不去，
    而真相是那一条**已经看过**了。
    """
    dims = list(dims)
    out = {dim: {"filled": 0, "judged_na": 0, "empty": 0} for dim in dims}
    for row in rows:
        scores = row.get("dimension_scores")
        scores = scores if isinstance(scores, Mapping) else {}
        for dim in dims:
            if dim not in scores:
                out[dim]["empty"] += 1
            elif scores[dim] is None:
                out[dim]["judged_na"] += 1
            else:
                out[dim]["filled"] += 1
    return out


__all__ = [
    "DimScoreError",
    "TIER_RANGE",
    "coverage",
    "group_fills",
    "merge_dimension_scores",
    "normalize_scores",
]