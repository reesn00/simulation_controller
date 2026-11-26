"""``partial_free`` 的**媒体信号前置事实核**（H-51 · 2026-11-26）。

owner 裁定：「``partial_free`` 的核用 ``media_signal``，要有执行核」。

判据唯一所有者：:func:`branches.media_signal_evidence`（两帧 ``MEDIA_PROBE_JS``
的原始返回 ⇒ ``media_probe.signal_from_samples`` ⇒ 有媒体 · 真推进 ·
``currentTime`` 低于试看上限 · 无 ``media_error``）。

⚠️ **补之前的样子**：这一支**完全没有核**——② / ④ 的
``paywall_type=preview|trial`` 是模型看页面文本给的，模型说「试看」就贴
``partial_free`` 进池。而「试看页」与「视频就是短 / 暂停在 0 秒」在文本上
完全同形，只有媒体信号能分开它们。

⚠️ **为什么不用文本判据**：那会是**第三份词表**（已有一份
``PAYWALL_WORDS``、一份 ``PAYWALL_PRICE_*``），而且核的输入就是模型看过
的那段文本——拿文本再核一遍文本，核等于没核。
"""

from __future__ import annotations

from trajectory_pipeline.executor.branches import (
    RunLedger,
    media_signal_evidence,
    precondition_failed,
)
from trajectory_pipeline.perception.base import Decision, Observation


def _obs(body: str, **kw) -> Observation:
    base = {"url": "https://x.test/v/1", "page_title": "试看", "body_text": body}
    base.update(kw)
    return Observation(**base)  # type: ignore[arg-type]


def _frame(times: list[float], error_code: int = 0) -> dict:
    """一帧 ``MEDIA_PROBE_JS`` 的返回（只填判据用得到的字段）。"""
    return {
        "count": len(times),
        "media": [
            {
                "tag": "video",
                "currentTime": t,
                "duration": 3600.0,
                "paused": False,
                "readyState": 4,
                "networkState": 1,
                "ended": False,
                "videoWidth": 1280,
                "videoHeight": 720,
                "errorCode": error_code,
                "framesDecoded": 120,
            }
            for t in times
        ],
    }


def _decision_preview() -> Decision:
    """④ 判「不可播」且 payload 指试看 ⇒ ``decision_branch`` 落 ``partial_free``。"""
    return Decision(
        question="PLAYER_OK",
        answer=False,
        confidence=0.8,
        evidence="试看 6 分钟，完整需会员",
        source="llm",
        payload={"paywall_type": "preview"},
    )


def _with_samples(first: dict | None, second: dict | None) -> Observation:
    return _obs(
        "试看6分钟，完整需会员",
        media_sample_first=first,
        media_sample_second=second,
    )


class Test判据本身:
    def test_真推进且在上限内_成立(self):
        hit = media_signal_evidence(_frame([1.0]), _frame([3.2]))
        assert hit, "播了 2.2s、上限 60s：这就是试看形态"
        assert "真推进" in hit

    def test_两帧都没推进_不成立(self):
        """暂停在 0 秒的页与试看页在单帧上同形——两帧才有差别。"""
        assert media_signal_evidence(_frame([0.0]), _frame([0.0])) == ""

    def test_只采到一帧_不成立(self):
        """没测过 ≠ 没推进：缺第二帧拿不到差分，核不能认。"""
        assert media_signal_evidence(_frame([3.2]), None) == ""

    def test_播过试看上限_不成立(self):
        """currentTime 已经 120s：那是能看，不是试看。"""
        assert media_signal_evidence(_frame([118.0]), _frame([120.5])) == ""

    def test_媒体报错_不成立(self):
        """error.code > 0 是 ``broken`` 的形态，不是试看。"""
        assert media_signal_evidence(_frame([1.0], 3), _frame([3.2], 3)) == ""

    def test_没有媒体元素_不成立(self):
        empty = {"count": 0, "media": []}
        assert media_signal_evidence(empty, empty) == ""

    def test_两帧都没有_不成立(self):
        assert media_signal_evidence(None, None) == ""


class Test分支前置事实核:
    def test_有媒体信号_不降级(self):
        out = RunLedger("T001").record_from(
            "https://x.test/v/1",
            _decision_preview(),
            obs=_with_samples(_frame([1.0]), _frame([3.2])),
        )
        assert out.branch == "partial_free"

    def test_没播起来_降级unresolved(self):
        """这是**补核要挡的那一类**：正文写着「试看 6 分钟」，媒体根本没动。"""
        out = RunLedger("T001").record_from(
            "https://x.test/v/1",
            _decision_preview(),
            obs=_with_samples(_frame([0.0]), _frame([0.0])),
        )
        assert out.branch == "unresolved"
        assert "[记账降级]" in out.evidence
        assert "没播起来" in out.evidence

    def test_没采到帧时放行_不是不成立(self):
        """这一页没跑媒体采样 ⇒ 核无从成立（放行），不记成「不是试看」。"""
        assert precondition_failed(
            "partial_free", _obs("试看6分钟，完整需会员"),
            decision=_decision_preview(),
        ) == ""

    def test_媒体坏掉的成因写在降级文案里(self):
        out = RunLedger("T001").record_from(
            "https://x.test/v/1",
            _decision_preview(),
            obs=_with_samples(_frame([1.0], 3), _frame([3.2], 3)),
        )
        assert out.branch == "unresolved"
        assert "broken" in out.evidence, "成因要指名，不然读档的人无从下手"
