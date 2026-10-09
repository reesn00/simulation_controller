"""P1 落盘——观测存档的唯一出口。

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

4. **只写审计必需字段**：正文只留摘要与长度。理由见
   :func:`executor.orchestrator._obs_json` 的注释——25 万字符的页面
   全量落盘会让 P1 体积失控，而复核的真正入口是 url + title + 交互元素。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

#: 产物根目录。**默认指向新树自己的 output/**，不碰仓库根 ``output/``
#: （那里是存量管线的产物，见方案决策 D8）。
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
        run = run_id or uuid.uuid4().hex[:8]
        return self._root / f"{task_id}__{run}.json"

    def write(self, record: Any, *, task_id: str, run_id: str | None = None) -> Path:
        """落盘一条运行记录。

        Raises:
            CredentialLeak: 内容命中凭据特征。**不写任何文件**。
        """
        payload = json.dumps(record.to_json(), ensure_ascii=False, indent=2)
        hits = scan_credentials(payload)
        if hits:
            raise CredentialLeak(
                f"P1 内容命中 {len(hits)} 项凭据特征，已拒绝写盘: {hits[:3]}"
            )
        return self._write_text(self.path_for(task_id, run_id), payload)

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