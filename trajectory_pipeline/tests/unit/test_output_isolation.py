"""产物隔离守卫：**测试不得写生产产物目录**。

事故（2026-11-10 · 台账 **H-28**）
--------------------------------
``tests/unit/test_cmd_mediabrowse.py`` 有 **7 处**把 ``out=None`` 传给
``cmd_mediabrowse`` ⇒ ``P1Archive(root=None)`` 落 :data:`DEFAULT_ROOT`
（= **生产产物目录** ``trajectory_pipeline/output/pipeline/``，不是
``tmp_path``）⇒ **每跑一次 pytest 就往生产目录写 4 个文件**。

文件名带 run_id（``P024-reuse__<run_id>.json``）⇒ **只增不减**：
盘上积了 **2218 个** pytest 残留（假域名 ``a.example.com``），占 4 MB。

⚠️ **它当时一声不吭**：测试全绿、门禁全绿，只是产物目录脏了。
而 ``mediabrowse/`` 是子目录、不被组装 glob 收走 ⇒ 训练数据没被污染，
**脏的只是产物目录本身**。⇒ 这类污染**不显形，只能靠守卫挡住**。

两条守卫
--------
1. **静态**：测试树里不许出现 ``out=None``。``out=None`` 在 CLI 层的
   含义就是「落默认根 = 生产目录」——**默认根在测试里永远是错的**。
   真要验「不给 ``out`` 会落哪」，就显式 ``monkeypatch.setattr(
   archive.DEFAULT_ROOT, tmp_path)``，别用 ``out=None`` 顺手带过。
2. **运行时**：``mediabrowse/`` 与 ``review_auto/`` 里不许出现**保留测试
   域名**（RFC 2606 / RFC 6761）。这两个目录是 ``cmd_mediabrowse`` 的两个
   落点，里面的东西按定义只可能是真站点。

⚠️ **第 2 条只在产物目录在盘上时有意义**——目录不在就跳过（产物不入库，
干净克隆上没这两个目录）。
"""

from __future__ import annotations

import ast
import json
import pathlib
import urllib.parse

import pytest

#: 本测试文件所在包的上级 = ``trajectory_pipeline/tests/``。
TESTS_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: 生产产物根（``trajectory_pipeline/output/pipeline/``）。
PIPELINE_ROOT = pathlib.Path(__file__).resolve().parents[2] / "output" / "pipeline"

#: 保留测试域名（RFC 2606 §3 + RFC 6761）。真站点不可能落在这些名字上。
RESERVED_TEST_HOSTS = frozenset({
    "example.com", "example.net", "example.org",
    "localhost", "127.0.0.1", "0.0.0.0", "::1",
})


def _host(url: str) -> str:
    """取 URL 的 host（端口去掉）。不是 URL 就原样返回。"""
    if "://" not in url:
        return url
    host = urllib.parse.urlsplit(url).netloc
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _offending_out_none() -> list[str]:
    """测试树里所有 ``out=None``（关键字实参 / dict 键值对）。"""
    hits: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.keyword) and node.arg == "out"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is None):
                hits.append(f"{path.relative_to(TESTS_ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Dict):
                for key, val in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and key.value == "out"
                            and isinstance(val, ast.Constant)
                            and val.value is None):
                        hits.append(f"{path.relative_to(TESTS_ROOT)}:{node.lineno}")
    return hits


class Test静态守卫:
    """``out=None`` = 「落默认根」= 落生产产物目录。测试里永远是错的。"""

    def test_测试树里没有_out_None(self):
        hits = _offending_out_none()
        assert not hits, (
            "这些地方把 out=None 传给了产物写入口——CLI 层它的含义是"
            "「落 DEFAULT_ROOT（生产产物目录）」，于是每跑一次 pytest 就往"
            f"生产目录写一批文件（2026-11-10 实测：{'、'.join(hits)}）。"
            "改成 out=str(tmp_path)；要验默认根的行为就显式 monkeypatch "
            "archive.DEFAULT_ROOT"
        )

    def test_守卫本身覆盖得到已知的坏处(self):
        """守卫不是空转的——拿一段坏例子喂它，它得报出来。"""
        bad = ast.parse("f(out=None)")
        hits = [
            n for n in ast.walk(bad)
            if isinstance(n, ast.keyword) and n.arg == "out"
        ]
        assert hits, "守卫的匹配逻辑坏了：连 out=None 都认不出"


class Test运行时残留:
    """``mediabrowse/`` 与 ``review_auto/`` 里不许有保留测试域名。"""

    @pytest.mark.parametrize("subdir", ["mediabrowse", "review_auto"])
    def test_产物目录里没有测试域名(self, subdir: str):
        d = PIPELINE_ROOT / subdir
        if not d.is_dir():
            pytest.skip(f"{subdir}/ 不在盘上（产物不入库）")
        offenders: list[str] = []
        for path in sorted(d.glob("*.json")):
            try:
                blob = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            url = str(blob.get("requested_url") or blob.get("url") or "")
            if _host(url) in RESERVED_TEST_HOSTS:
                offenders.append(f"{path.name} → {url}")
        for path in sorted(d.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                url = str(row.get("url") or row.get("landed_url") or "")
                if _host(url) in RESERVED_TEST_HOSTS:
                    offenders.append(f"{path.name} → {url}")
        assert not offenders, (
            f"{subdir}/ 里有测试域名残留（{len(offenders)} 处）——这是 pytest "
            "写进生产产物目录的产物：测试写生产目录不报错，只留脏文件。"
            f"先查调用点有没有漏给 out：{offenders[:5]}"
        )
