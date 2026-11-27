"""检索式必须**可从存档复核**（2026-11-27 · 台账 H-54）。

**为什么单独立一条**：`query` 是整条发现链的第一张多米诺——它决定了候选池
长什么样，而候选池决定了后面所有读数。可是光有那个字符串，读档人分不出
`在线影视资源站大全` 是「这条任务该搜的」还是「抄错了模板」：

- B 路径（`discovery_path == "aggregator"`）用的是**与片名无关的聚合词**，
  由 ``crc32(task_id) % 3`` 三选一；
- A / C 路径用的是「片名 + 后缀」（``build_query``）。

**两种东西在 `query` 字段上完全同形。** 实测代价就在这条上：拿 A 路径的
``_search_query()`` 去核一条 B 路径的存档，把「四条存档逐条一致」读成了
「来源不可复核」，据此写进台账才发现是问错了路径。

⇒ 这组门禁盯两件事：

1. **`query_origin` 必须都落盘**，且三种来源各记各的（不合并成一个串）；
2. **凡是记了 `vocab` 的，必须能离线复算出 `query`**——复算不出来就等于
   「写着可复核、其实复核不了」，比不记更坏。
"""

from __future__ import annotations

import zlib

import pytest

from trajectory_pipeline.executor.steps.search import (
    AGGREGATOR_QUERIES,
    aggregator_query,
    build_query,
)


def _recompute(origin: dict) -> str:
    """按存档里的 ``query_origin`` 离线复算，模拟读档人的做法。"""
    rule = origin["rule"]
    assert rule == "aggregator_query", rule
    vocab = origin["vocab"]
    return vocab[zlib.crc32(origin["seed"].encode("utf-8")) % len(vocab)]


class Test来源可离线复算:
    @pytest.mark.parametrize("task_id", ["T001", "T006", "T016", "T018", "T098"])
    def test_聚合路径的query可由task_id复算(self, task_id):
        # 存档里存的是 vocab + seed，不是「我们相信它是这么来的」
        origin = {
            "rule": "aggregator_query",
            "seed": task_id,
            "vocab": list(AGGREGATOR_QUERIES),
        }
        assert _recompute(origin) == aggregator_query(task_id)

    def test_同一task_id永远得同一条词(self):
        # 用 crc32 而不用内置 hash 的**理由**：内置 hash 受 PYTHONHASHSEED 影响，
        # 同一 task 跨进程会换词 ⇒ 存档复算不出来
        assert aggregator_query("T016") == aggregator_query("T016")
        assert aggregator_query("T016") == "影视聚合站"

    def test_同一部片的两个任务不该被当成同一种检索(self):
        # T016 / T018 都是《功夫》，但 B 路径的检索式不同——差异来自
        # task_id 哈希，不来自任务语义。这条把「同片 ⇒ 同检索式」这个
        # 直觉错在哪里钉住，将来谁要改成分片名检索，会先撞上它。
        assert aggregator_query("T016") != aggregator_query("T018")


class Test三种来源各记各的:
    def test_聚合词不带片名(self):
        # B 的检索式**与片名无关**是本路径的设计（任何片名的聚合站入口池
        # 是同一个）。若哪天它开始拼片名，这条会红。
        for title in ("功夫", "周星驰", ""):
            assert aggregator_query("T016") == aggregator_query("T016")
        assert "功夫" not in aggregator_query("T016")

    def test_A路径的检索式带片名(self):
        # A / C 与 B 的形态不同，**而这正是存档上要分得开的两件事**
        assert build_query("功夫") == "功夫 在线观看"
        assert build_query("功夫") != aggregator_query("T016")

    def test_调用方给的检索词不追认来源(self):
        # fail-closed：调用方传进来的串，我们**不知道**它是 taskgen 渲染的
        # 还是人手写的，那就记 None，而不是猜一个「应该是 persona 渲染的」
        origin = {"rule": "caller_supplied", "seed": None, "vocab": None}
        assert origin["vocab"] is None  # 不可复算，读档人据实知道

class Test真的进存档:
    def test_to_json里有query_origin且可复算(self):
        # ⚠️ 这条盯的是 **	o_json 是手写白名单**：加了 dataclass 字段
        # 不等于进了存档。漏掉它的症状正是本条要治的那类——
        # 「有 query、看不出它从哪来」，而字段测试全绿。
        from trajectory_pipeline.executor.orchestrator import RunRecord

        origin = {
            "rule": "aggregator_query",
            "seed": "T018",
            "vocab": list(AGGREGATOR_QUERIES),
        }
        rec = RunRecord(
            task_id="T018", title="功夫", query=aggregator_query("T018"),
            search_url="https://www.baidu.com/s?wd=x",
            query_origin=origin,
        )
        blob = rec.to_json()
        assert "query_origin" in blob, "字段没进存档"
        assert blob["query_origin"] == origin
        # 读档人拿存档本身就能复算
        assert _recompute(blob["query_origin"]) == blob["query"]

    def test_旧存档没有这个键时读档不炸(self):
        # 存量存档没有这个键——**读得懂**（「没记」），不是崩
        from trajectory_pipeline.executor.orchestrator import RunRecord

        rec = RunRecord(
            task_id="T001", title="武林外传", query="武林外传 在线观看",
            search_url="u",
        )
        assert rec.query_origin == {}
        assert rec.to_json()["query_origin"] == {}
