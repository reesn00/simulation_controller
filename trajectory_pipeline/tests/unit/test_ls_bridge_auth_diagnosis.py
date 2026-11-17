"""凭据**形态**诊断: 401 的报错必须**指对方向**（**不联网**）。

为什么有这份测试
----------------
2026-11-17 实测（用户重建 LS 实例后）：新实例的
``jwt_auth_jwtsettings.legacy_api_tokens_enabled=0`` ⇒ 40 位 legacy token
走 ``Token`` 头**一律 401**，而它**长得没错、也没过期**。

改之前的报错是「API key 无效或权限不足」⇒ 把人引去**反复换 key**
（换一百把还是 401），而真因是**鉴权机制被禁用**（⇒ 该用 PAT）。
本文件钉住两件事：

1. :func:`describe_api_key` 能把形态说清楚（PAT / legacy / 空）；
2. 401 的报错**按形态分流**：legacy 那条必须指向 PAT，PAT 那条必须指向
   「重新签发」。

⚠️ 凭据红线：测试里只用**假凭据**（形状对、内容假），不写任何真值。
"""

from __future__ import annotations

import base64
import json

import pytest

from trajectory_pipeline.evaluation.ls_bridge.client import (
    LabelStudioClient,
    describe_api_key,
)
from trajectory_pipeline.evaluation.ls_bridge.errors import LabelStudioAuthFailed
from trajectory_pipeline.evaluation.ls_bridge.project_manager import describe_status
from trajectory_pipeline.evaluation.ls_bridge.settings import LSSettings

#: 形状对、内容假的 PAT（真 PAT 不得进仓库 —— 凭据红线）。
_FAKE_PAT = "header.payload.signature"


def _pat_with_exp(exp: int) -> str:
    """造一个**假的** PAT，只为让 ``exp`` 可读。"""
    payload = base64.urlsafe_b64encode(
        json.dumps({"token_type": "refresh", "exp": exp}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode(
        "ascii"
    ).rstrip("=")
    return f"{header}.{payload}.sig"


class _Resp:
    """最小响应桩（只给 client._handle 用的字段）。"""

    def __init__(self, status_code: int, text: str = "", content: bytes = b"") -> None:
        self.status_code = status_code
        self.text = text
        self.content = content


class TestDescribeApiKey:
    def test_空凭据(self) -> None:
        assert describe_api_key("") == "empty"
        assert describe_api_key("   ") == "empty"

    def test_legacy按长度与无有效期说(self) -> None:
        out = describe_api_key("a" * 40)
        assert out.startswith("legacy(")
        assert "40" in out
        assert "exp=" not in out, "legacy token 没有有效期，别编一个出来"

    def test_PAT带exp(self) -> None:
        out = describe_api_key(_pat_with_exp(8102097735))
        assert out.startswith("pat(")
        assert "exp=" in out
        assert "2226" in out, "exp 是 UTC 秒 ⇒ 2226 年"

    def test_读不出exp的PAT不编造(self) -> None:
        out = describe_api_key(_FAKE_PAT)
        assert out.startswith("pat(")
        assert "读不出 exp" in out


class TestAuthHintRouting:
    """401/403 的报错按**形态**分流（别一律说「key 无效」）。"""

    def test_legacy被拒要指向PAT(self) -> None:
        client = LabelStudioClient("http://ls.invalid", "a" * 40)
        with pytest.raises(LabelStudioAuthFailed) as exc:
            client._handle(_Resp(401), "GET", "/api/projects")
        msg = str(exc.value)
        assert "legacy" in msg
        assert "PAT" in msg, "legacy 被拒时必须指向 PAT（机制被禁用）"
        assert "legacy_api_tokens_enabled" in msg, "要说出机制名，人才查得到"

    def test_PAT被拒要指向重新签发(self) -> None:
        client = LabelStudioClient("http://ls.invalid", _pat_with_exp(8102097735))
        with pytest.raises(LabelStudioAuthFailed) as exc:
            client._handle(_Resp(401), "GET", "/api/projects")
        msg = str(exc.value)
        assert "PAT" in msg
        assert "重新签发" in msg
        assert "legacy" not in msg, "PAT 路径不该把人引去 legacy"

    def test_403走同一条分流(self) -> None:
        client = LabelStudioClient("http://ls.invalid", "a" * 40)
        with pytest.raises(LabelStudioAuthFailed) as exc:
            client._handle(_Resp(403), "GET", "/api/projects")
        assert "PAT" in str(exc.value)


class TestStatusShowsKeyKind:
    """``status`` 必须报**形态**（环境变量会被继承 ⇒ 旧值常驻进程）。"""

    class _Stub:
        def get_project(self, project_id: int) -> dict:
            return {"id": project_id, "title": "trajectory-review"}

    def test_status报形态(self) -> None:
        settings = LSSettings(
            base_url="http://ls.invalid", api_key="a" * 40, project_id=7
        )
        out = describe_status(
            self._Stub(), settings, health={"status": "UP"}  # type: ignore[arg-type]
        )
        assert out["api_key_kind"].startswith("legacy(")

    def test_status不打印凭据(self) -> None:
        settings = LSSettings(
            base_url="http://ls.invalid", api_key="a" * 40, project_id=7
        )
        out = describe_status(
            self._Stub(), settings, health={"status": "UP"}  # type: ignore[arg-type]
        )
        assert "a" * 40 not in str(out), "status 不得回显凭据本身"
