"""精炼旁挂（sidecar）的**格式唯一真源**。

为什么是旁挂而不是写回 P1
------------------------
P1 是**取证态**：它是证据链的起点，正文落全文、不落摘要
（``docs/contracts/P1-observation.md`` 第 2 条）。LLM 精炼是**派生的
语料加工**——可以重跑、可以换模型、可以整批丢弃。把模型输出写回 P1
就是把「一次性的推断」混进「事实」：闸门与评分器读的是 P1，
它们会开始拿模型改写过的文字当页面证据（付费墙词表、片名一致性核、
实体锚定全部按「逐字能在正文里检索到」判据走）。

所以精炼结果落在**旁挂文件**里：``<refined_dir>/<archive 去 .json>.refined.json``。
读侧（装配层）只把它当**数据**读，**不 import 任何精炼实现**——
装配层是纯代码、不调 LLM（``docs/01-架构总览.md`` 模块 5）。

⚠️ **旁挂不能长得像 P1 存档**（2026-11-13 实测踩到）
--------------------------------------------------
旁挂与 P1 存档**同在产物根下**（``output/pipeline/refined/``），于是两件事
会撞：

1. **文件名**：P1 的形状约定是 ``<task_id>__<run_id>.json``
   （``executor/archive.py::select_archives`` 靠它挑存档）。旁挂若也叫
   ``<task_id>__<run_id>.json``，**任何按名扫存档的消费者都会把它当存档**
   ⇒ 解析一份没有 ``run_config`` 的东西当 P1。
   所以旁挂带**自己的后缀** ``.refined.json``。
2. **顶层键**：P1 的 ``steps`` 是**列表**（一步一个 dict）。旁挂原来也叫
   ``steps``，但它是**映射**（正文键 → 记录）⇒ 按形状筛存档的扫描器会把它
   收进来，然后在 ``st.get("action")`` 上炸掉（``'str' object has no
   attribute 'get'``——**实测**，不是推演）。
   所以旁挂的顶层键叫 ``entries``：**它一条 step 都没有**。

这两条是**真实批次跑出来**的：一跑，契约测试就红了
（``test_p2_contract.py::TestToolSpaceDrift::test_存档里出现过的动作都在声明内``）。

键怎么选
--------
旁挂按**正文内容哈希**索引，不按步号索引：

- P2 与 T 是**两份独立渲染**（P2 读 P1 切条、T 读 P1 直接渲），
  它们的步号坐标系不同（per-unit vs per-Run），按步号索引要维护
  两套映射，映射错了就是「A 步的精炼贴到 B 步」——静默的语料污染；
- 内容哈希两边算出来一样，且**天然幂等**：同一份正文精炼一次，
  换步号、换渲染口径都不用重算。

⚠️ 哈希只用于**定位**，不用于判真伪：渲染侧仍要逐行核对「保留的行
确实还在这份正文里」（:func:`trajectory_pipeline.assembler.
observation_view.render` 的 ``refined`` 分支），对不上就回退到规则裁剪。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

#: 旁挂格式版本。**只在破坏性变更时升**——加可选字段不动它
#: （同 ``P1-observation.md`` 的版本纪律）。
#:
#: ⚠️ **2026-11-13：``refine.v1`` → ``refine.v2``**（破坏性变更）：
#: 顶层键 ``steps`` 改名 ``entries``、文件后缀加 ``.refined``。
#: 升版本是**故意的**：旧档（v1）读侧**显式作废**（``read_sidecar`` 认版本）
#: ⇒ 那些档案走规则裁剪，**与没精炼时逐字一致**；
#: 不升版本的话，旧档会被**认出来但读不出内容**（键名对不上 ⇒ 索引为空）
#: ⇒ **静默当没精炼**，而人会以为「精炼过了但效果不好」。
SIDECAR_SCHEMA_VERSION = "refine.v2"

#: 正文键的长度（sha256 前 16 位十六进制 = 64 bit）。
#:
#: 64 bit 在十万条观察这个量级上的碰撞概率约 5e-10——比「同一批里
#: 两条观察正文逐字相同」的概率还低，而后者本来就是允许的
#: （同一个页面被访问两次，正文一样 ⇒ 精炼结果一样，正好）。
_BODY_KEY_LEN = 16


def body_key(body: str) -> str:
    """正文的**稳定键**（内容哈希）。

    空正文也有键（``sha256("")``）——「空观察」同样要被定位到，
    否则「这一条没有精炼过」与「这一条精炼成了空」分不开。
    """
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:_BODY_KEY_LEN]


#: 旁挂文件的后缀。与 P1 的 ``.json`` **刻意不同**——见模块 docstring
#: 「旁挂不能长得像 P1 存档」第 ① 条。
SIDECAR_SUFFIX = ".refined.json"


def sidecar_path(refined_dir: str | Path, archive_name: str) -> Path:
    """旁挂文件路径：``<refined_dir>/<archive 去 .json>.refined.json``。

    存档名带后缀去一次就够——P1 的文件名形如 ``T001__abc123.json``，
    旁挂叫 ``T001__abc123.refined.json``（**同名不同目录，且不同后缀**）。

    ⚠️ 后缀不是装饰：P1 的形状约定是 ``<task>__<run>.json``，旁挂若
    顶着同一个名字，按名扫存档的消费者会把它当存档收走。理由见模块
    docstring。
    """
    name = str(archive_name or "")
    if name.endswith(".json"):
        name = name[:-len(".json")]
    return Path(refined_dir) / f"{name}{SIDECAR_SUFFIX}"


def read_sidecar(path: str | Path) -> dict[str, Any] | None:
    """读旁挂。**不存在 = 没有精炼**（不是错误）。

    读不出来（坏 JSON / 版本对不上）也返回 ``None``：旁挂是**派生品**，
    坏了就当没有，回退到规则裁剪——**宁可少一次精炼，不要一次崩掉整批渲染**。
    """
    p = Path(path)
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    if str(blob.get("schema_version") or "") != SIDECAR_SCHEMA_VERSION:
        return None
    return blob


def write_sidecar(path: str | Path, payload: Mapping[str, Any]) -> None:
    """原子写旁挂（同 builder 的 ``.tmp`` + ``os.replace`` 纪律）。

    半截旁挂比没有旁挂更难查——文件名在、内容读不出。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, p)


def index_by_body(payload: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """旁挂 → ``{正文键: 保留的行}`` 索引。

    渲染侧唯一要用的读法。**认不出的一律跳过**（缺键 / 类型不对 /
    保留行不是字符串列表）——索引里的坏条目会被渲染侧逐行核对拦下，
    这里先滤一道是为了不让坏数据进热路径。

    ⚠️ 顶层键叫 ``entries`` **不是** ``steps``：P1 存档的 ``steps`` 是
    **列表**，而这里的 ``entries`` 是**映射**（正文键 → 记录）。
    叫同一个名字会让「按形状筛存档」的扫描器把旁挂收进去（实测踩过）。
    """
    out: dict[str, list[str]] = {}
    if not isinstance(payload, Mapping):
        return out
    entries = payload.get("entries")
    if not isinstance(entries, Mapping):
        return out
    for entry in entries.values():
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("body_key") or "")
        kept = entry.get("kept")
        if not key or not isinstance(kept, list):
            continue
        lines = [str(x) for x in kept if isinstance(x, str)]
        if not lines:
            continue
        out.setdefault(key, lines)
    return out


def make_payload(
    archive_name: str,
    entries: Mapping[str, Mapping[str, Any]],
    *,
    prompt_version: str = "",
) -> dict[str, Any]:
    """拼一份旁挂。

    ``entries`` 是 ``{正文键: 一条记录}``——记录形状见
    :meth:`trajectory_pipeline.refine.refiner.RefineStep.to_json`
    （保留的行逐字来自 P1 正文 + 丢掉的行数 + 调用账）。

    ⚠️ **按正文键去重**：同一份正文在一份存档里可能被看到多次
    （同一个页面访问两次），而正文一样 ⇒ 精炼结果一样 ⇒ 只存一条、
    只花一次 LLM 调用。

    ⚠️ 顶层键叫 ``entries``（**不是** ``steps``）——理由见
    :func:`index_by_body` 的警告。
    """
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "archive": str(archive_name or ""),
        "prompt_version": str(prompt_version or ""),
        "entries": {str(k): dict(v) for k, v in entries.items()},
    }


def load_index(
    refined_dir: str | Path | None,
    archive_name: str,
) -> dict[str, list[str]]:
    """读一份旁挂 → ``{正文键: 保留的行}`` 索引（渲染侧唯一入口）。

    **读不到就是没有精炼**（旁挂是派生品，坏了就当没有）——渲染层于是走
    规则裁剪，行为与没精炼时**逐字一致**。

    ``refined_dir=None`` = 不做精炼（默认）：旁挂目录没给就不去找，
    免得「目录空着」与「目录没配」两件事分不开。
    """
    if refined_dir is None:
        return {}
    payload = read_sidecar(sidecar_path(refined_dir, archive_name))
    return index_by_body(payload)


__all__ = [
    "SIDECAR_SCHEMA_VERSION",
    "SIDECAR_SUFFIX",
    "body_key",
    "index_by_body",
    "load_index",
    "make_payload",
    "read_sidecar",
    "sidecar_path",
    "write_sidecar",
]
