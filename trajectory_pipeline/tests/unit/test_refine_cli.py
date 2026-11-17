"""``cli refine`` 的**批次口径**——运营方靠这几个数估预算。

这一组盯三件事：

1. **计划数 = 真发请求数**（计划阶段按正文键去重，与真跑同口径）；
2. **预算用尽 ⇒ 那份档案整份不落盘**，且**必须报出来**
   （「算过」与「写下去」不是一件事——报表比真实产出多是本仓库最贵的
   那类 bug，避坑指南 §2.2b）；
3. **不静默少一半**：被丢弃的条数要**单独印一个数字**。
"""

from __future__ import annotations

import argparse
import json

import pytest

from trajectory_pipeline.executor import cli


def _ns(root, **kw) -> argparse.Namespace:
    """CLI 的参数命名空间。

    ⚠️ ``out`` **必须给一个真目录**：``out=None`` 在 CLI 层的含义是
    「落 DEFAULT_ROOT = **生产产物目录**」（H-28 事故，`out=None` 有专门的
    静态守卫盯着——`tests/unit/test_output_isolation.py`）。这里给
    ``tmp_path``，测试就**只可能**写临时目录。
    """
    base = {
        "out": str(root),
        "p1": [],
        "limit": 0,
        "refined_dir": None,
        "dry_run": False,
        "max_calls": 0,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _body(seed: str, lines: int = 60) -> str:
    """一份**长**正文（⇒ 会被裁 ⇒ 值得发请求）。

    ⚠️ 行数不能随便给：训练态正文上限是 **320 字**，40 行 × 7 字 = 280
    ⇒ **规则层够用** ⇒ 不发请求（那条路走的是 ``not_needed``）。
    60 行 × 8 字 = 480 > 320 ⇒ 才会真的发请求。
    """
    return "\n".join(f"{seed}第{i}行内容" for i in range(1, lines))


def _archive(bodies: list[str]) -> dict:
    return {
        "title": "测试任务",
        "steps": [
            {"action": {}, "observation": {"body_text": b}} for b in bodies
        ],
    }


def _write(tmp_path, name: str, archive: dict):
    p = tmp_path / name
    p.write_text(json.dumps(archive, ensure_ascii=False), encoding="utf-8")
    return p


class Test计划口径:
    def test_计划按正文键去重(self, tmp_path, capsys):
        """同一份正文出现两次 ⇒ **只发一次请求**。

        计划阶段**必须按同一口径**算：多报 ⇒ 运营方按它估 ``--max-calls``
        ⇒ 预算不够 ⇒ 一半档案被丢弃。
        """
        body = _body("甲")
        p1 = _write(tmp_path, "T001__abc12345.json",
                    _archive([body, body]))
        assert cli.cmd_refine(_ns(tmp_path, p1=[str(p1)], dry_run=True)) == 0
        out = capsys.readouterr().out
        assert "约 1 次 LLM 请求" in out, out

    def test_不同正文各算一次(self, tmp_path, capsys):
        p1 = _write(tmp_path, "T001__abc12345.json",
                    _archive([_body("甲"), _body("乙")]))
        assert cli.cmd_refine(_ns(tmp_path, p1=[str(p1)], dry_run=True)) == 0
        out = capsys.readouterr().out
        assert "约 2 次 LLM 请求" in out, out

    def test_预算内的正文不算请求(self, tmp_path, capsys):
        """规则层够用 ⇒ 不发请求（**不选就没有**）。"""
        short = "\n".join(f"第{i}行" for i in range(1, 5))
        p1 = _write(tmp_path, "T001__abc12345.json", _archive([short]))
        assert cli.cmd_refine(_ns(tmp_path, p1=[str(p1)], dry_run=True)) == 0
        out = capsys.readouterr().out
        assert "约 0 次 LLM 请求" in out, out

    def test_dry_run不写盘(self, tmp_path, capsys):
        p1 = _write(tmp_path, "T001__abc12345.json",
                    _archive([_body("甲")]))
        out_dir = tmp_path / "refined"
        assert cli.cmd_refine(
            _ns(tmp_path, p1=[str(p1)], dry_run=True, refined_dir=str(out_dir)),
        ) == 0
        assert not out_dir.exists() or not list(out_dir.glob("*.json"))


class Test预算用尽:
    """预算用尽 ⇒ **那份档案整份不落盘**（不是「落盘但少几条」）。"""

    def test_用尽的那份不落盘且要报出来(
        self, tmp_path, capsys, monkeypatch,
    ):
        # 后端指向一个**没人听的端口** ⇒ 调用必失败（不真连外网）。
        monkeypatch.setenv("TRAJECTORY_LLM_BASE_URL", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("TRAJECTORY_LLM_MODEL", "fake")
        monkeypatch.setenv("TRAJECTORY_LLM_API_KEY", "x")
        monkeypatch.setenv("TRAJECTORY_LLM_TIMEOUT_S", "1")
        monkeypatch.setenv("TRAJECTORY_LLM_MAX_TOKENS", "256")

        p1 = _write(tmp_path, "T001__abc12345.json",
                    _archive([_body("甲"), _body("乙"), _body("丙")]))
        out_dir = tmp_path / "refined"
        rc = cli.cmd_refine(
            _ns(tmp_path, p1=[str(p1)], max_calls=1, refined_dir=str(out_dir)),
        )
        # ⚠️ **退 1**（H-39 · 2026-11-17 起）：这条用例里后端是**死的**
        # ⇒ 发过请求的那条**失败** ⇒ 本批**一条都没精炼** ⇒ 退出码必须
        # 说破（否则脚本会以为「精炼过了」，而产物与没精炼时逐字一致）。
        assert rc == 1
        out = capsys.readouterr().out

        # ① 那份**没有落盘**（预算用尽 ⇒ 整份丢弃）
        assert not list(out_dir.glob("*.json")), (
            "预算用尽时那份档案不该落盘——它是不完整的"
        )
        # ② **丢弃的条数要单独印出来**（不能混进「成功」）
        assert "已算但没落盘" in out, out
        assert "落盘统计：成功 0 条" in out, out
        # ③ 预算用尽要**逐份**报出来
        assert "[预算用尽]" in out, out
        # ④ **失败原因要分类报出来**（H-39）——「失败 N 条」不说是谁在失败
        assert "失败原因分布" in out, out

    def test_没配后端退2不静默跳过(self, tmp_path, capsys):
        """没配后端 ⇒ **退 2**：不静默跳过（否则会让人以为「精炼过了
        但效果不好」，而真相是压根没跑）。"""
        p1 = _write(tmp_path, "T001__abc12345.json",
                    _archive([_body("甲")]))
        rc = cli.cmd_refine(_ns(tmp_path, p1=[str(p1)]))
        assert rc == 2
        out = capsys.readouterr().out
        assert "LLM 后端不可用" in out, out


class Test全败退非零:
    """**一条都没成 ⇒ 退 1**（H-39 · 2026-11-17 实测逼出来的）。

    为什么要有这条：2026-11-17 那轮 ``refine`` 里 **22 条全部失败**
    （LLM 网关的 key 失效 ⇒ ``http:401`` ⇒ 熔断打开 ⇒ 后 19 条压根
    没发请求），而 ``refine`` **退 0** ⇒ 脚本/流水线会以为「精炼过了」。

    ⚠️ 而事实上这批**一条都没精炼**：旁挂里的 ``kept`` 全空 ⇒ 渲染层
    **全走规则裁剪** ⇒ 产物与没精炼时**逐字一致** ⇒ **报表上完全看不出
    异常**。这正是设计 §5「没配后端 ⇒ 退 2」要防的那类误读——只是这次
    后端**配着、只是 key 失效了**。
    """

    def test_全败退1(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setenv("TRAJECTORY_LLM_BASE_URL", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("TRAJECTORY_LLM_MODEL", "fake")
        monkeypatch.setenv("TRAJECTORY_LLM_API_KEY", "x")
        monkeypatch.setenv("TRAJECTORY_LLM_TIMEOUT_S", "1")
        monkeypatch.setenv("TRAJECTORY_LLM_MAX_TOKENS", "256")

        p1 = _write(tmp_path, "T001__abc12345.json",
                    _archive([_body("甲"), _body("乙")]))
        out_dir = tmp_path / "refined"
        rc = cli.cmd_refine(
            _ns(tmp_path, p1=[str(p1)], refined_dir=str(out_dir)),
        )
        assert rc == 1, "一条都没成必须退 1（不许退 0）"
        out = capsys.readouterr().out
        assert "一条都没精炼" in out, out
        assert "失败原因分布" in out, out

    def test_规则层够用不算失败退0(self, tmp_path, capsys, monkeypatch):
        """**没发过请求**就不算「全败」——规则层够用时压根不需要后端。

        ⚠️ 这条挡的是一个误伤：``not_needed`` 那条也走 ``refine_body``，
        若把它算进「请求失败」，那「一条都没成」就会在**压根没用后端**
        的批次上误报 ⇒ 退出码撒谎（比不报更坏）。
        """
        monkeypatch.setenv("TRAJECTORY_LLM_BASE_URL", "http://127.0.0.1:1/v1")
        monkeypatch.setenv("TRAJECTORY_LLM_MODEL", "fake")
        monkeypatch.setenv("TRAJECTORY_LLM_API_KEY", "x")
        monkeypatch.setenv("TRAJECTORY_LLM_TIMEOUT_S", "1")
        monkeypatch.setenv("TRAJECTORY_LLM_MAX_TOKENS", "256")

        short = "\n".join(f"第{i}行" for i in range(1, 5))   # 规则层够用
        p1 = _write(tmp_path, "T001__abc12345.json", _archive([short]))
        rc = cli.cmd_refine(
            _ns(tmp_path, p1=[str(p1)], refined_dir=str(tmp_path / "refined")),
        )
        assert rc == 0, "没发过请求就没有「全败」⇒ 不该退 1"


class Test失败原因分类:
    """失败原因要**分类**报出来（H-39）——「失败 N 条」不说是谁在失败。"""

    def test_原因只取前缀不取全文(self):
        from trajectory_pipeline.executor.cli import _refine_reason_head

        assert _refine_reason_head("http:401") == "http:401"
        assert _refine_reason_head("circuit:open") == "circuit"
        assert _refine_reason_head("transport:ConnectError") == "transport"
        assert _refine_reason_head("budget:max_tokens 用尽") == "budget"
        assert _refine_reason_head("动作目标没保住") == "模型质量"
        assert _refine_reason_head("") == "未记录原因"

    def test_模型质量类不带正文片段(self):
        """模型质量类的错误文本可能带页面正文 ⇒ 分类里不许回显它。"""
        from trajectory_pipeline.executor.cli import _refine_reason_head

        head = _refine_reason_head("行号越界: 正文里没有第 999 行 «机密内容»")
        assert head == "模型质量"
        assert "密" not in head
