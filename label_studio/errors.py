"""label_studio.errors: 统一异常族 + 凭据脱敏.

设计约束 (方案 §16 R9): **凭据不得出现在日志 / 异常信息 / repr 里**。
本模块的所有异常在构造时就把消息按 :func:`redact` 过一遍 —— 即使调用方不小心
把整条异常塞进 log, 也不会漏出 api_key。
"""

from __future__ import annotations

import re
from typing import Any

#: 需要脱敏的敏感键名 (小写匹配)。出现在消息里就替换成 ***。
_SECRET_KEY_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|authorization|bearer)\b"
    r"(\s*[:=]\s*)(\S+)"
)

#: 常见凭据字面量形态 —— 独立于键名, 直接拦。
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def redact(text: Any) -> str:
    """把消息里的凭据形态替换为 ``***``。任何输入都安全 (非 str 原样 str())。"""
    if text is None:
        return ""
    out = str(text)
    out = _SECRET_KEY_PATTERN.sub(r"\1\2***", out)
    for pattern in _SECRET_VALUE_PATTERNS:
        out = pattern.sub("***", out)
    return out


class LabelStudioError(Exception):
    """Label Studio 集成层所有异常的基类。

    ``__str__`` 统一走 :func:`redact`, 所以子类构造时不必自己记得脱敏。
    """

    def __init__(self, message: str = "", **context: Any) -> None:
        self.context = context
        super().__init__(redact(message))

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        # 显式不打印 context: 里面可能有原始响应体 / header
        return f"{type(self).__name__}({redact(str(self))!r})"


class LabelStudioUnavailable(LabelStudioError):
    """LS 服务不可达 (连接拒绝 / 超时 / DNS 失败)。"""


class LabelStudioAuthFailed(LabelStudioError):
    """凭据缺失或被拒 (401 / 403 / token 无效)。"""


class LabelStudioProjectError(LabelStudioError):
    """项目创建 / 复用 / label_config 校验失败。"""


class C3ParseError(LabelStudioError):
    """C3 产物缺失、文件名不可解析、或内容结构异常。"""


class CredentialLeakDetected(LabelStudioError):
    """推送前扫描命中凭据模式 —— fail-closed 拒推该 task (方案 §16 R11)。

    这是**有意的失败**: 静默脱敏会污染标注语义 (标注员看到的样本与训练用样本
    不一致), 所以宁可拒推并记录, 让人来决定怎么处理。
    """


__all__ = [
    "LabelStudioError",
    "LabelStudioUnavailable",
    "LabelStudioAuthFailed",
    "LabelStudioProjectError",
    "C3ParseError",
    "CredentialLeakDetected",
    "redact",
]
