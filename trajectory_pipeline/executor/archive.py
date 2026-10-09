"""P1 落盘 + 负样本池并池——观测存档与负样本的唯一出口。

P1 是**后续所有环节的输入**（gdr 精修、失败归因、Label Studio 推送都从它来），
所以这里的纪律比其他落盘点更严：

1. **一 task 一文件，文件名带 task_id**——``T001__<run_id>.json``。
   不按时间分目录：按 task 分才能在「某个 task 重跑」时定位到它的历史记录，
   而这正是失败归因（模块 5）最常做的事。

2. **原子写**：先写 ``.tmp`` 再 ``os.replace``。批处理中途崩了，
   不能留下一半的 JSON——半个文件比没有文件更坏，因为下游会照着它解析。

3. **凭据扫描（fail-closed）**：写盘前扫一遍。命中即**拒绝写**并抛错，
   不是「写进去打个警告」。这条对应 CLAUDE.md 的 R11 凭据红线，
   也对应新树在 :data:`obscura_driver.FORBIDDEN_TOOLS` 层的拦截——
   两道闸门，因为一道是纪律一道是代码，而纪律会被绕过。

4. **正文落全文，不落摘要**。早期版本只留前 400 字符，理由是体积失控、
   「需要时按 url 重抓」——那条理由站不住：站点会下线改版（重抓拿到的是
   另一个页面）、被反爬时根本重抓不回来。理由与现由
   :func:`executor.orchestrator._obs_json` 持有。

5. **负样本池在本模块并入**，与 P1 同一个出口：见 :meth:`P1Archive.write`。
   追加语义与去重键见 :meth:`P1Archive._append_negatives`。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from trajectory_pipeline.executor.branches import BRANCH_LABELS

#: 产物根目录。**默认指向新树自己的 output/**，不碰仓库根 ``output/``
#: （那里是存量管线的产物，见方案决策 D8）。
#:
#: ================== P1 的落点形状（唯一的定义处）====================
#: 本文件是 P1 路径的**权威定义**。P2 / P3 / 负样本池 / 复核队列的落点
#: 以此为基准，不要在别处另写一份路径字面量。
#:
#:     output/pipeline/<task_id>__<run_id>.json     ← 平铺，**不是目录**
#:
#: ⚠️ 设计方案 §4 与 storage/ 的 docstring 早期写的是
#: ``observations/<run_id>/``（每 run 一个目录）。**本实现是平铺单文件**，
#: 两处 docstring 已按本实现对齐——理由如下，改动前请先读完：
#:
#:   1. **平铺已是既定事实**，不是随手选的：4 处消费方按文件名模式读它
#:      （``P1Archive.list_runs`` / ``review_queue.collect`` /
#:      ``cli.cmd_report`` / ``cli.cmd_apply_review``），
#:      加上 [archive.py] 自己的路径断言测试。改成目录要四处全换 ``rglob``。
#:   2. **两者不在同一层**：``observations/<run_id>/`` 的目录形式是为
#:      「动作 + 工具参数 + 观察原文」的**分片**存档预留的（一个 run 目录下
#:      多个文件）。而 P1 现状是**每个 run 一个汇总文件**，一行事实判断
#:      的差异，不是「设计 vs 实现谁对」。
#:   3. **未来要开目录时改这里**：一旦 rationale 落地、动作流真的需要
#:      分片存放（P1 拆成 ``observations/<run_id>/{actions.json,obs.json}``），
#:      改的是本常量与 :class:`P1Archive`，以及上面 4 处消费方——
#:      前提是那时它们都已收敛到 :class:`P1Archive` 之下。
#: =====================================================================
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "output" / "pipeline"

#: 凭据特征。**宁可误报**——误报的代价是人工看一眼，
#: 漏报的代价是把 API key 写进要推给 Label Studio 的存档。
#:
#: 覆盖：Authorization 头、Bearer/JWT、常见云厂商 key、URL 内嵌 user:pass@、
#: Set-Cookie、以及明显的私有密钥 PEM 头。
#:
#: **一律不锚定尾部词边界**（早期版本写了 ``\b`` 结尾，实测会漏）：
#: Google key 恰好 39 字符时命中，40 字符就不命中——因为第 36 个字符仍在
#: key 字符集内、词边界不成立。凭据后面紧跟其他字符（拼接、截断、
#: 被塞进更长的串）恰恰是最需要拦住的形态，所以统一用「前缀 + 至少 N 位」，
#: 长度只当下限不做上限。
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"authorization\s*[:=]",
        r"bearer\s+[A-Za-z0-9._\-]{16,}",
        r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",     # JWT
        r"sk-ant-[A-Za-z0-9_\-]{16,}",                      # Anthropic
        r"sk-[A-Za-z0-9]{16,}",                             # OpenAI 风格
        r"AKIA[0-9A-Z]{16,}",                               # AWS Access Key
        r"AIza[0-9A-Za-z_\-]{35,}",                         # Google API Key
        r"https?://[^\s/:]+:[^\s/@]+@",                     # URL 内嵌口令
        r"set-cookie\s*:",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"ghp_[A-Za-z0-9]{36,}",                            # GitHub token
    )
)


class CredentialLeak(RuntimeError):
    """P1 内容命中凭据特征。**fail-closed：拒绝写盘**。"""


#: 人工复核存档的后缀。``cli.cmd_apply_review`` 写 ``<run>.reviewed.json``，
#: 读的一侧（``cmd_report`` / ``select_archives``）都以此为准——
#: **散落成两个字面量就会出现「写的地方改了、读的地方没改」**。
REVIEWED_SUFFIX = ".reviewed.json"


def select_archives(root: Path) -> list[tuple[Path, bool]]:
    """挑出该纳入统计的存档，返回 ``[(路径, 是否已过人工复核)]``。

    两条规则都是被真实目录逼出来的，缺一条报表就出错：

    1. **只认 ``<task_id>__<run_id>.json`` 形状**。同目录里还躺着
       ``obscura_tools.json`` / ``probe_observe.json`` / ``obscura_returns*.json``
       这些**探针取证**，与 P1 同目录但不是存档。按 ``*.json`` 收会把它们
       算进「存档 N 个」，于是「被反爬拦截 X/N」「可跑 N 个」的分母偏大——
       而分母错在这张表上会直接改结论：实测目录里 9 个 json 只有 4 个是
       存档，拦截率会从 1/4 被稀释成 1/9。

    2. **``.reviewed.json`` 取代原档，不并列计两份**。回填是覆盖式的
       （未裁定的条目原样保留，见 ``review_queue.apply_verdicts``），
       两份一起数等于把同一批站点数两遍，成功数与分母同时翻倍。
       同时**优先取复核档**——人工确认的成功是 W1 正样本的**唯一**来源
       （规则版只认 ``<video>`` 标签，真实视频站全是 JS/iframe 播放器），
       只读原档等于把 W1 的正样本统计清零。

    不认内容、只认文件名：判别内容需要读全量存档（几十 MB 正文），
    而文件名形状已经是 :meth:`P1Archive.path_for` 声明的契约，
    同一目录里两类文件的区别也只在文件名上。
    """
    reviewed = {
        p.name[: -len(REVIEWED_SUFFIX)] + ".json": p   # → 原档名，用于配对
        for p in root.glob(f"*{REVIEWED_SUFFIX}")
    }
    out: list[tuple[Path, bool]] = []
    for path in sorted(root.glob("*.json")):
        if path.name.endswith(REVIEWED_SUFFIX):
            continue                      # 已由原档那条带走
        if "__" not in path.stem:
            continue                      # 探针取证，不是存档
        hit = reviewed.get(path.name)
        out.append((hit, True) if hit else (path, False))
    return out


def _pool_separator(path: Path) -> str:
    """追加前判断要不要先补一个换行。

    池文件被写坏时末行可能是**半截 JSON 且没有换行符**（进程在
    ``write`` 中途被杀、断电、磁盘满）。此时直接追加，新条目会被粘到
    那半截后面，**两条一起报废**：坏行本来只丢 1 条，粘连之后新写的
    负样本也一起读不出来。

    所以追加前检查末字节，不为空就补 ``\\n``。宁可让坏行单独成行被
    :meth:`P1Archive._load_negative_keys` 丢弃，也不要把新数据寄存在它
    后面——新数据是刚跑出来的，坏行可能是三天前的。
    """
    if not path.exists() or path.stat().st_size == 0:
        return ""
    with path.open("rb") as fh:
        fh.seek(-1, 2)                      # 空文件已在上面挡掉
        return "" if fh.read(1) == b"\n" else "\n"


def scan_credentials(payload: str) -> list[str]:
    """返回命中的特征名列表；空 = 干净。

    单独抽出来是为了能**离线单测**——凭据红线不能靠「跑一遍看看有没有炸」。
    """
    hits: list[str] = []
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(payload):
            hits.append(pattern.pattern)
    return hits


class P1Archive:
    """P1 存档写入器。"""

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root or DEFAULT_ROOT)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, task_id: str, run_id: str | None = None) -> Path:
        """P1 的文件名形状：``<task_id>__<run_id>.json``。

        双下划线分隔是刻意的——``runnable_in_w1`` 式的 glob
        （``T001__*.json``）据此与「按 task 分组」共存，而不会把
        ``T001__a.reviewed.json`` 误当成另一条 run（后缀在 ``__`` 之后，
        不匹配 ``*__*`` 之外的形式；``cmd_apply_review`` 另有一道显式
        后缀排除作为第二道闸门）。

        落点是**平铺**单文件，理由见 :data:`DEFAULT_ROOT` 的长注释。
        """
        run = run_id or uuid.uuid4().hex[:8]
        return self._root / f"{task_id}__{run}.json"

    def write(self, record: Any, *, task_id: str, run_id: str | None = None) -> Path:
        """落盘一条运行记录，**并把它贡献的负样本并入负样本池**。

        两件事绑在一个方法里，不是偷懒：「每条失败分支都必须有对应样本入库」
        是硬要求（见 :mod:`trajectory_pipeline.executor.branches` 的模块
        docstring），而 P1 与负样本池来自**同一次运行、同一个 ledger**。
        拆成两个方法就等于给了调用方「写完 P1 忘了并池」的机会，而那种失效
        是静默的：报表上分部数字都在，就是有一支永远是 0，而那可能只是
        没并池、不是没产出。

        Raises:
            CredentialLeak: 内容命中凭据特征。**不写任何文件**。
        """
        run = run_id or uuid.uuid4().hex[:8]
        payload = json.dumps(record.to_json(), ensure_ascii=False, indent=2)
        hits = scan_credentials(payload)
        if hits:
            raise CredentialLeak(
                f"P1 内容命中 {len(hits)} 项凭据特征，已拒绝写盘: {hits[:3]}"
            )
        path = self._write_text(self.path_for(task_id, run), payload)
        self._append_negatives(record, task_id=task_id, run_id=run)
        return path

    # ── 负样本池 ──────────────────────────────────────────────────

    @property
    def negative_pool_path(self) -> Path:
        """负样本池落点：``output/pipeline/negative.jsonl``。

        与 P1 平铺同目录（不另开子目录），理由同 :data:`DEFAULT_ROOT`。
        """
        return self._root / "negative.jsonl"

    def _append_negatives(self, record: Any, *, task_id: str, run_id: str) -> int:
        """把这次运行的**真负样本**并入池子，返回新增条数。

        追加而非覆盖：负样本池是**跨 run 累积**的资产，而 P1 是每 run
        一个独立文件（同一 task 重跑会产生多个存档）。若每次覆盖，最后
        一次运行会把之前的全部抹掉——而「这批跑出的负样本比上一批少」
        这个信息恰恰会一起消失。

        ⚠️ **去重键是 ``(task_id, url, branch)``，不是 url 单键**：
        同一个站在不同 task 下失败，训练信号是**两条**（判分上下文不同）；
        而同一 task 同一站同分支重复入池是**一条**（重跑产生了同样的结论）。
        用 url 单键会把前者误合并成一条，负样本池按 task 切片时就会少样本。

        只并入 ``is_negative_sample`` 的条目：``unresolved`` 与
        ``trailer_suspect`` 是「没判出来」，混进池子会毁掉「这里真的看不了」
        这条信号（见 :class:`~trajectory_pipeline.executor.branches.SiteOutcome`）。
        """
        ledger = getattr(record, "ledger", None)
        if ledger is None:
            return 0
        negatives = [o for o in ledger.negative_samples() if o.is_negative_sample]
        if not negatives:
            return 0

        existing = self._load_negative_keys()
        fresh: list[dict[str, Any]] = []
        for outcome in negatives:
            key = (task_id, outcome.url, outcome.branch or "")
            if key in existing:
                continue
            existing.add(key)
            fresh.append(self._negative_row(outcome, task_id, run_id, record))

        if not fresh:
            return 0
        blob = "".join(
            json.dumps(row, ensure_ascii=False) + "\n" for row in fresh
        )
        hits = scan_credentials(blob)
        if hits:
            raise CredentialLeak(
                f"负样本内容命中 {len(hits)} 项凭据特征，已拒绝并池: {hits[:3]}"
            )
        path = self.negative_pool_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_pool_separator(path) + blob)
        return len(fresh)

    def _negative_row(
        self, outcome: Any, task_id: str, run_id: str, record: Any
    ) -> dict[str, Any]:
        """一条负样本的池内形状。

        带上 ``task_id`` 与 ``persona_id``：负样本池是**按切片消费**的
        （"强口语 × 长尾这类组合是不是特别容易判失败"），只有 url 与 branch
        时那份 provenance 已经不在了，而它就在同一个 ``record`` 上——
        不带过来等于白丢。
        """
        provenance = dict(getattr(record, "provenance", {}) or {})
        return {
            "task_id": task_id,
            "run_id": run_id,
            "persona_id": provenance.get("persona_id", ""),
            "url": outcome.url,
            "branch": outcome.branch,
            "branch_label": BRANCH_LABELS.get(outcome.branch or "", ""),
            "evidence": outcome.evidence,
            "question": outcome.question,
            "source": outcome.source,
            "fallback_used": outcome.fallback_used,
            "reached_play_page": outcome.reached,
            "content_tier": provenance.get("content_tier", ""),
            "popularity": provenance.get("popularity", ""),
            "genre": provenance.get("genre", ""),
        }

    def _load_negative_keys(self) -> set[tuple[str, str, str]]:
        """读池内已有的去重键。

        **逐行解析**：文件被截断时末行可能是半截 JSON，那一行**丢弃**——
        与 :func:`trajectory_pipeline.executor.dom.parse_links` 同一处置
        （宁缺勿滥，补全会造出一条不存在的负样本）。
        """
        import json as _json

        path = self.negative_pool_path
        if not path.exists():
            return set()
        keys: set[tuple[str, str, str]] = set()
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
            keys.add((
                str(row.get("task_id") or ""),
                str(row.get("url") or ""),
                str(row.get("branch") or ""),
            ))
        return keys

    def _write_text(self, path: Path, payload: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)          # 同目录内原子替换
        finally:
            if tmp.exists():                # replace 失败时别留半个文件
                tmp.unlink(missing_ok=True)
        return path

    def list_runs(self, task_id: str) -> list[Path]:
        return sorted(self._root.glob(f"{task_id}__*.json"))

    def select_archives(self) -> list[tuple[Path, bool]]:
        """本目录下的全部存档（人工复核后取复核档）。见 :func:`select_archives`。"""
        return select_archives(self._root)

    # ── 回滚 ────────────────────────────────────────────────────────

    def purge_run(self, run_id: str) -> tuple[tuple[Path, ...], tuple[dict[str, Any], ...]]:
        """回滚一个 run：**存档与它并进池的负样本一起删**。

        返回 ``(删掉的存档路径, 删掉的池行)``。**先返回再删**——调用方
        要先看清要动什么，确认了才 ``--apply``。

        ## 为什么必须成对删
        负样本池是**跨 run 累积**的 append-only 文件。只删存档会留下一批
        「没有来源的标签」：它们出现在的 task 在池里挂着，但翻遍存档找不到
        依据，而 :func:`~trajectory_pipeline.executor.integrity.check_root`
        的横向矛盾检查正是**靠存档去推翻池行**的——存档没了，那批行
        再也无人能证伪，就此常驻。

        ## 为什么需要这个命令
        实测（2026-10-09）：``run`` 少给 ``--title``，拿 ``T001`` 当片名，
        搜成「轮胎 T001」，产出 6 条 ``not_play_site``。这些标签**对
        实际检索式是真的**（那几个站确实不是 T001 轮胎的观看页），
        但它们要表达的是「这里看不了《功夫》」——片名错了，
        标签的前提就整个塌了，而**存档里看不出任何异常**。

        同一批的行还有第二种回滚理由：判断点 ① 的输入缺陷曾产出
        与同一 URL 成功记录直接矛盾的 ``not_play_site``。
        那类不必删干净——``check-archives`` 会指出来，由人按 URL 逐条定。

        ## 键从哪来
        ``run_id`` 是**存档文件名里的那一段**（``T001__b3a3548a.json``），
        池行里也带着同名字段——两者由 :meth:`write` 同时写下，
        所以它是唯一能把两边对上的键。
        """
        run_id = (run_id or "").strip()
        if not run_id:
            raise ValueError("run_id 为空")

        archives: list[Path] = []
        for path, _reviewed in select_archives(self._root):
            if _run_id_of(path.name) == run_id:
                archives.append(path)
        # 复核档与原档成对存在：删一份留一份会让 select_archives 的
        # 「复核档取代原档」配对落空，下一个同名原档凭空多出一次复核。
        for path in self._root.glob(f"*{run_id}*"):
            if path not in archives and path.suffix == ".json":
                archives.append(path)

        rows = [row for row in self._load_pool_rows()
                if str(row.get("run_id") or "") == run_id]
        return tuple(sorted(set(archives))), tuple(rows)

    def apply_purge(
        self,
        archives: tuple[Path, ...],
        rows: tuple[dict[str, Any], ...],
    ) -> tuple[int, int]:
        """真正执行 :meth:`purge_run` 的删除。返回 ``(存档数, 池行数)``。

        只按 ``run_id`` 过滤，不按去重键——而去重键
        ``(task_id, url, branch)`` 是**跨 run 全局**的（见
        :meth:`_append_negatives`），所以同一个 task 的两个 run 撞上同一个
        ``url + branch`` 时池里只会有一行、``run_id`` 记的是**先写的那批**。
        回滚先写的那批会连带删掉后写那批也认同的这条标签。

        这个代价是有意接受的：那一行的 ``task_id`` / ``url`` / ``branch``
        与被回滚的完全相同，而回滚的前提正是「这批 task 的结论整体不成立」——
        同 task 同 URL 的另一条标签同样不成立。与其为这个保留一条已经
        不成立的标签，不如让池干净。

        这两个「顺手」实测都出过事，所以都堵上了：

        1. **按身份删，不按 ``run_id`` 删。** 早先的写法是「取所有行的
           ``run_id`` 集合，删掉命中集合的行」——于是调用方一旦传进一行
           别的 run 的行（拼错、复用别人的 ``rows``），爆炸半径就静默扩大。
           签名说的是「删这些行」，实现说的却是「删这些 run_id 的所有行」。
        2. **重写时保留坏行原文。** 池子会被回滚整体重写一遍，而读的时候
           坏行是跳过的——不保留就等于**每回滚一次顺手清掉全池的坏行**。
           那是另一件事（且坏行可能是被人手工截断、正等人查的），
           不该由一个 run 级回滚顺手决定。空行照旧丢弃。

        返回的 ``池行数`` 是**实际删掉**的行数，与传入 ``rows`` 条数不等
        时说明调用方传了库里没有的行。
        """
        # 按行的 JSON 文本配对记账：同一 ``url + branch`` 被两个 run 并过时
        # 池里只有一行，用计数而不是集合才不会「删一行少一行」。
        budget: dict[str, int] = {}
        for row in rows:
            key = json.dumps(row, ensure_ascii=False, sort_keys=True)
            budget[key] = budget.get(key, 0) + 1

        kept: list[str] = []
        deleted = 0
        for raw, row in self._load_pool_lines():
            if row is not None:
                key = json.dumps(row, ensure_ascii=False, sort_keys=True)
                if budget.get(key):
                    budget[key] -= 1
                    deleted += 1
                    continue
            kept.append(raw)

        payload = "".join(raw + "\n" for raw in kept)
        self._write_text(self.negative_pool_path, payload)
        for path in archives:
            path.unlink(missing_ok=True)
        return len(archives), deleted

    def _load_pool_lines(self) -> list[tuple[str, dict[str, Any] | None]]:
        """读池子，**坏行原样带出**（第二项为 ``None``）。

        重写整份文件时要用它而不是 :meth:`_load_pool_rows`：只经过后者
        的行会被原样丢弃，等于回滚顺手清了坏行。
        """
        path = self.negative_pool_path
        if not path.exists():
            return []
        out: list[tuple[str, dict[str, Any] | None]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                out.append((line, None))
                continue
            out.append((line, row if isinstance(row, dict) else None))
        return out

    def _load_pool_rows(self) -> list[dict[str, Any]]:
        """读池子里的**好行**。坏行不在返回里（要保留原文走
        :meth:`_load_pool_lines`）——一次回滚不该顺手把别人的坏行清掉，
        而把坏行塞进返回值只会让调用方不小心把它写回去。"""
        return [row for _, row in self._load_pool_lines() if row is not None]

def _run_id_of(filename: str) -> str:
    """从存档文件名里取 ``run_id``，取不到返回空串。

    ``T001__b3a3548a.json`` → ``b3a3548a``；``T001__b3a3548a.reviewed.json``
    同样 → ``b3a3548a``——**复核后缀要剥掉**，否则 :meth:`P1Archive.purge_run`
    只能认出原档、漏掉复核档，删一半留一半（见那里的说明）。

    判定依据与 :func:`select_archives` 一致（都有 ``__``、都以 ``.json`` 结尾），
    两处口径不同就会出现「有的地方找得到、有的地方找不到」。
    """
    name = filename
    if name.endswith(REVIEWED_SUFFIX):
        name = name[: -len(REVIEWED_SUFFIX)] + ".json"
    stem = name[: -len(".json")] if name.endswith(".json") else name
    if "__" not in stem:
        return ""
    return stem.rsplit("__", 1)[1]
