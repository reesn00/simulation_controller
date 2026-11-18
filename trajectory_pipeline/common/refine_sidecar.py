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
旁挂按**候选集的哈希**索引，不按步号索引：

- P2 与 T 是**两份独立渲染**（P2 读 P1 切条、T 读 P1 直接渲），
  它们的步号坐标系不同（per-unit vs per-Run），按步号索引要维护
  两套映射，映射错了就是「A 步的精炼贴到 B 步」——静默的语料污染；
- 哈希两边算出来一样，且**天然幂等**：同一份候选集精炼一次，
  换步号、换渲染口径都不用重算。

⚠️ **键覆盖的是「这条记录选过的那几维」**（:func:`selection_key`）：

- **老协议（``select.v1``，只选正文）** ⇒ 键只看正文；
- **三段协议（``select.v2``，正文 + 元素 + 链接）** ⇒ 键看
  **正文 + 元素候选 + 链接候选**。

⚠️ **为什么三段协议的键不能只看正文**（2026-11-18 实测，H-41 阶段 3）：
**同一份正文**在一条轨迹里可能被看到**多次**，而那几次的
**元素 / 链接集合可以不同**（动态页面、镜像站：正文文字一样，
链接指向不同的站）。实测（E005 存档）：同一份正文出现 8 次，
其中 5 次的链接集合与第 1 次不同 ⇒ 用正文哈希当键 ⇒
**第 1 次的选择被贴到了第 3 次上** ⇒ 渲染层逐字核对「模型选的
那个链接」⇒ 对不上 ⇒ **整条回退规则裁剪**。
⇒ 实测 **13 条回退里 12 条是这一条**（Tier B 达标率 87.7%，
判据要求 ≥95%）。

⇒ 所以键必须是**候选集**的哈希：模型的判断只对它**当时看到的那份
候选清单**成立。

⚠️ 哈希只用于**定位**，不用于判真伪：渲染侧仍要逐行核对「保留的行
确实还在这份正文里」「模型选的元素 / 链接确实在这一份的候选集里」
（:func:`trajectory_pipeline.assembler.observation_view.render` 的
``refined`` 分支），对不上就回退到规则裁剪。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

#: 旁挂格式版本。**只在破坏性变更时升**——加可选字段不动它
#: （同 ``P1-observation.md`` 的版本纪律）。
#:
#: ⚠️ **2026-11-17（H-41 阶段 2）：``refine.v2`` → ``refine.v3``**（破坏性变更）：
#: 条目新增 ``elements`` / ``links``（**模型选的元素 / 链接**）与
#: ``protocol``（这条记录是**哪一版选择协议**产出的）。
#:
#: 为什么要升版：``refine.v2`` 的条目只有 ``kept``（正文行），而阶段 2 的
#: 「语义渲染」需要**元素与链接也由模型选**。不升版本的话，旧档会被
#: **认出来但读不出内容**（没有元素 / 链接选择）⇒ **静默当没精炼**，
#: 而人会以为「精炼过了但效果不好」——那正是本模块 docstring 里
#: 「宁可少一次精炼，也不要一次崩掉整批渲染」要防的那类静默退化。
#:
#: 旧档（``refine.v2``）**读侧显式作废**（:func:`read_sidecar` 认版本）
#: ⇒ 那些档案走规则裁剪，**与没精炼时逐字一致**（零回归）。
#:
#: ⚠️ **2026-11-18（H-41 阶段 3）：``refine.v3`` → ``refine.v4``**
#: （破坏性变更）：**条目的键**从「正文哈希」改成「**候选集哈希**」
#: （:func:`selection_key`）。
#:
#: 为什么必须升版：``refine.v3`` 的条目键是**正文哈希**，而三段协议
#: （``select.v2``）选的是**正文 + 元素 + 链接** ⇒ 同一份正文在不同
#: 步上的元素 / 链接集合可以不同 ⇒ 用正文哈希当键 ⇒ **A 步的选择被
#: 贴到 B 步上** ⇒ 渲染层逐字核对不过 ⇒ **整条回退规则裁剪**
#: （实测：13 条回退里 12 条是这一条，Tier B 达标率 87.7% < 95%）。
#:
#: 不升版本的后果：**旧档被认出来但查不到**（键对不上）⇒ 静默当没
#: 精炼 ⇒ 而人会以为「精炼过了但效果不好」——那正是本模块 docstring
#: 里「宁可少一次精炼，也不要一次崩掉整批渲染」要防的那类静默退化。
SIDECAR_SCHEMA_VERSION = "refine.v4"

#: **条目协议版本**——「这一条的选择是按哪一版协议做的」。
#:
#: 为什么要**逐条**记（而不是只记在旁挂顶层）：渲染层的「语义模式」
#: 需要知道**这条**的模型选择覆盖了哪些维度（正文 / 元素 / 链接）。
#: 老协议只选正文 ⇒ 语义模式不能用它选元素 ⇒ **整条回退规则裁剪**
#: （而不是「正文用模型的、元素用规则的」那种半吊子——两套口径混在
#: 一条观察里，读的人分不清哪部分是模型说的）。
ENTRY_PROTOCOL = "select.v2"

#: **旧的选择协议**（只选正文，方案 25）——留痕用。
#:
#: 语义渲染**只认** :data:`ENTRY_PROTOCOL`：老协议产出的条目只有正文
#: 一个维度，元素 / 链接**没被选过** ⇒ 拿它渲染语义模式会造出
#: 「正文是模型选的、元素是规则选的」那种混口径观察。
ENTRY_PROTOCOL_LEGACY = "select.v1"

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

    ⚠️ **它只是「候选集键」的一个特例**（:func:`selection_key`，
    元素与链接都为空时）——**老协议**（只选正文）用它，
    **三段协议**必须用 :func:`selection_key`。
    """
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()[:_BODY_KEY_LEN]


def _feed(digest: "hashlib._Hash", value: str) -> None:
    """把一个字段喂进哈希——**长度前缀**，免得拼接出歧义。

    ⚠️ 不用分隔符：正文里什么字符都可能有（分隔符也可能出现）
    ⇒ 「A + B」和「AB + 空」撞成同一个哈希 ⇒ **A 步的精炼贴到
    B 步上**——正是本模块要防的那类静默污染。
    """
    raw = (value or "").encode("utf-8")
    digest.update(str(len(raw)).encode("ascii"))
    digest.update(b":")
    digest.update(raw)


def selection_key(
    body: str,
    *,
    elements: Sequence[tuple[str, str]] = (),
    links: Sequence[tuple[str, str]] = (),
) -> str:
    """**候选集**的稳定键（正文 + 元素候选 + 链接候选）。

    Args:
        body: **完整正文**（取证态全文）。
        elements: 元素候选 ``(role, name)``——**摆给模型挑的那一份**
            （:func:`trajectory_pipeline.common.view_candidates.
            element_candidates` 的产物，语义口径 ``filter_noise=False``）。
        links: 链接候选 ``(text, href)``——同上。

    ⚠️ **键必须覆盖「模型当时看到的那份清单」**：模型的判断只对它
    看到过的东西成立。**同一份正文**在不同步上的元素 / 链接集合可以
    不同（动态页面、镜像站）⇒ 只按正文哈希定位 ⇒ **A 步的选择被贴
    到 B 步上** ⇒ 渲染层逐字核对不过 ⇒ 整条回退规则裁剪。
    实测（2026-11-18 · E005）：13 条回退里 **12 条**是这一条。

    ⚠️ **元素 / 链接的顺序有意义**（页面顺序）⇒ 键按**顺序**算，
    不排序——排序会把「页面从上到下」这个结构信号从键里抹掉。
    """
    digest = hashlib.sha256()
    digest.update(b"trajectory.selection.v1")
    _feed(digest, body or "")
    digest.update(b"|elements|")
    for role, name in elements or ():
        _feed(digest, str(role or ""))
        _feed(digest, str(name or ""))
    digest.update(b"|links|")
    for text, href in links or ():
        _feed(digest, str(text or ""))
        _feed(digest, str(href or ""))
    return digest.hexdigest()[:_BODY_KEY_LEN]


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


def _normalize_entry(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """一条旁挂记录 → **规范形状**（认不出的返回 ``None``）。

    规范形状::

        {"selection_key": str, "body_key": str, "source": str, "kept": [str],
         "elements": [{"role": str, "name": str}],
         "links": [{"text": str, "href": str}],
         "dropped": int, "candidates": int, "candidates_shown": int,
         "error": str, "call": dict, "protocol": str}

    ⚠️ **坏条目一律跳过**——索引里的坏数据会被渲染侧逐项核对拦下，
    这里先滤一道是为了不让坏数据进热路径。

    ⚠️ ``selection_key`` 是**索引键**（候选集哈希，见
    :func:`selection_key`）；``body_key`` 是**留痕**（正文哈希）——
    两者**不是一回事**：同一份正文可以有多条记录（元素 / 链接集合
    不同 ⇒ 候选集不同 ⇒ 键不同）。⚠️ 缺 ``selection_key`` 的条目
    **一律跳过**——它是定位用的键，缺了这条记录**定位不到**，
    留着只会让人以为「精炼过了」。
    """
    key = str(entry.get("selection_key") or "")
    kept = entry.get("kept")
    if not key or not isinstance(kept, list):
        return None
    elements: list[dict[str, str]] = []
    for item in entry.get("elements") or ():
        if not isinstance(item, Mapping):
            continue
        role = str(item.get("role") or "")
        name = str(item.get("name") or "")
        if not name:
            continue
        elements.append({"role": role, "name": name})
    links: list[dict[str, str]] = []
    for item in entry.get("links") or ():
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text") or "")
        href = str(item.get("href") or "")
        if not href:
            continue
        links.append({"text": text, "href": href})
    return {
        "selection_key": key,
        "body_key": str(entry.get("body_key") or ""),
        "source": str(entry.get("source") or ""),
        "kept": [str(x) for x in kept if isinstance(x, str)],
        "elements": elements,
        "links": links,
        "dropped": int(entry.get("dropped") or 0),
        "candidates": int(entry.get("candidates") or 0),
        "candidates_shown": int(entry.get("candidates_shown") or 0),
        "error": str(entry.get("error") or ""),
        "call": dict(entry.get("call") or {}),
        "protocol": str(entry.get("protocol") or ""),
    }


def index_entries(payload: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """旁挂 → ``{候选集键: 规范记录}`` 索引。**渲染层的唯一读法**。

    ⚠️ **``kept`` 为空的条目也留在索引里**（H-41 阶段 2）：「模型一行都
    没选」与「这一条没精炼过」是**两件事**——前者是模型的判断（合法结果），
    后者是失败。混在一起 ⇒ 语义模式分不出「模型认为这页没有核心内容」
    与「精炼没跑成」⇒ **静默退回规则裁剪**，而报表上写着「精炼过了」。

    ⇒ 判据交给调用方：看 ``source``（``"llm"`` = 模型选过）与
    ``kept`` 是否为空。

    ⚠️ 顶层键叫 ``entries`` **不是** ``steps``：P1 存档的 ``steps`` 是
    **列表**，而这里的 ``entries`` 是**映射**（候选集键 → 记录）。
    叫同一个名字会让「按形状筛存档」的扫描器把旁挂收进去（实测踩过）。
    """
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, Mapping):
        return out
    entries = payload.get("entries")
    if not isinstance(entries, Mapping):
        return out
    for entry in entries.values():
        if not isinstance(entry, Mapping):
            continue
        normalized = _normalize_entry(entry)
        if normalized is None:
            continue
        out.setdefault(normalized["selection_key"], normalized)
    return out


def index_by_body(payload: Mapping[str, Any] | None) -> dict[str, list[str]]:
    """旁挂 → ``{正文哈希: 保留的行}`` 索引（**旧形状**，保留兼容）。

    ⚠️ **``kept`` 为空的条目在这里被丢弃**——旧形状只有「行」这一维，
    分不出「没选」与「没精炼」。要区分请用 :func:`index_entries`。

    新代码**不该**再用这个函数（渲染层要元素 / 链接选择 ⇒ 必须用
    :func:`index_entries`）。

    ⚠️ **正文哈希不是索引键**（:func:`selection_key`）⇒ 同一份正文
    可以有多条记录（元素 / 链接集合不同）⇒ 这里**只留第一条**
    （``setdefault`` 语义）——旧形状本来就没有「多份」的表达能力。
    """
    out: dict[str, list[str]] = {}
    for entry in index_entries(payload).values():
        lines = entry["kept"]
        body = entry.get("body_key") or ""
        if not lines or not body:
            continue
        out.setdefault(body, list(lines))
    return out


def make_payload(
    archive_name: str,
    entries: Mapping[str, Mapping[str, Any]],
    *,
    prompt_version: str = "",
) -> dict[str, Any]:
    """拼一份旁挂。

    ``entries`` 是 ``{候选集键: 一条记录}``——记录形状见
    :meth:`trajectory_pipeline.refine.refiner.RefineStep.to_json`
    （**必须带** ``selection_key``（索引键）与 ``body_key``（留痕）；
    保留的行逐字来自 P1 正文 + 丢掉的行数 + 调用账）。

    ⚠️ **按候选集键去重**：同一份候选集在一份存档里可能被看到多次
    （同一个页面访问两次），而候选集一样 ⇒ 精炼结果一样 ⇒ 只存一条、
    只花一次 LLM 调用。

    ⚠️ **正文一样但元素 / 链接不一样 ⇒ 键不一样**（:func:`selection_key`）
    ⇒ **不合并**——合并就是把 A 步的选择贴到 B 步上（实测踩过：
    E005 存档 13 条回退里 12 条是这一条）。

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
) -> dict[str, dict[str, Any]]:
    """读一份旁挂 → ``{候选集键: 规范记录}`` 索引（渲染层唯一入口）。

    **读不到就是没有精炼**（旁挂是派生品，坏了就当没有）——渲染层于是走
    规则裁剪，行为与没精炼时**逐字一致**。

    ``refined_dir=None`` = 不做精炼（默认）：旁挂目录没给就不去找，
    免得「目录空着」与「目录没配」两件事分不开。

    ⚠️ **旧版本旁挂（``refine.v3`` 及更早）读侧显式作废**
    （:func:`read_sidecar` 认版本）⇒ 返回 ``{}`` ⇒ 那些档案**整条走规则
    裁剪**（零回归）。⚠️ 不升版本的后果是「认出来但查不到」⇒ **静默当
    没精炼** ⇒ 而人会以为「精炼过了但效果不好」。
    """
    if refined_dir is None:
        return {}
    payload = read_sidecar(sidecar_path(refined_dir, archive_name))
    return index_entries(payload)


__all__ = [
    "ENTRY_PROTOCOL",
    "ENTRY_PROTOCOL_LEGACY",
    "SIDECAR_SCHEMA_VERSION",
    "SIDECAR_SUFFIX",
    "body_key",
    "index_by_body",
    "index_entries",
    "load_index",
    "make_payload",
    "read_sidecar",
    "selection_key",
    "sidecar_path",
    "write_sidecar",
]
