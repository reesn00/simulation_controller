"""``golden_ls.diff``：相对上一版的变更标记。

守的是四件事（每一条都对应一个会**静默**出错的可能）：

1. **四种标记判对**：``new`` / ``changed`` / ``id_migrated`` / ``unchanged``；
2. **没有可比对象 ⇒ 全判 ``new``**（不是 ``unchanged``）——两者的后果相反
   （「要人看」vs「不用看」），判错方向就是漏看；
3. **id 迁移靠旧 id 配对**：v14 有 6 行 ``golden_id_legacy``，只按新 id
   查上一版 ⇒ 这 6 行会被误判成 ``new``（人白看一遍）；
4. **``content_key`` 不含 id**：键变了是 ``id_migrated`` 的事，内容变没变
   是另一件事——两件事混在一个键里就说不清是哪一种。

⚠️ 这些都是**纯函数**测试，不碰磁盘、不联网。
"""

from __future__ import annotations

from trajectory_pipeline.evaluation.golden_ls import diff


def _row(
    gid: str,
    *,
    label: str = "success",
    branch: str = "success",
    legacy: str = "",
    dims: dict | None = None,
    evidence: str = "",
    surface: str = "",
    observed_at: str = "",
    reviewed_by: str = "",
    reviewed_at: str = "",
    note: str = "",
) -> dict:
    row = {
        "golden_id": gid,
        "golden": {"label": label},
        "code": {"branch": branch, "branch_label": branch, "evidence": evidence},
    }
    if legacy:
        row["golden_id_legacy"] = legacy
    if dims is not None:
        row["dimension_scores"] = dims
    # 金标块里的观测面与观测时刻（方案 20 §R2）——**没有键**与**有键**是两形
    if surface:
        row["golden"]["access_surface"] = surface
    if observed_at:
        row["golden"]["observed_at"] = observed_at
    if reviewed_by:
        row["golden"]["reviewed_by"] = reviewed_by
    if reviewed_at:
        row["golden"]["reviewed_at"] = reviewed_at
    if note:
        row["golden"]["note"] = note
    return row


# ── 四种标记 ────────────────────────────────────────────────────


class TestMarkChanges:
    def test_新行判new(self) -> None:
        markers = diff.mark_changes([_row("a"), _row("b")], [_row("a")])
        assert markers == {"a": diff.MARK_UNCHANGED, "b": diff.MARK_NEW}

    def test_内容变了判changed(self) -> None:
        markers = diff.mark_changes(
            [_row("a", label="blocked")], [_row("a", label="success")]
        )
        assert markers["a"] == diff.MARK_CHANGED

    def test_代码分支变了也判changed(self) -> None:
        markers = diff.mark_changes(
            [_row("a", branch="broken")], [_row("a", branch="success")]
        )
        assert markers["a"] == diff.MARK_CHANGED

    def test_评分面变了判changed(self) -> None:
        """评分面换了 = 逐维分数全变 = 人要重新看一眼（v6 / v8 的实况）。"""
        markers = diff.mark_changes(
            [_row("a", dims={"d3": 3})], [_row("a", dims={"d3": 5})]
        )
        assert markers["a"] == diff.MARK_CHANGED

    def test_键变了内容没变判id_migrated(self) -> None:
        markers = diff.mark_changes(
            [_row("new-id", legacy="old-id")], [_row("old-id")]
        )
        assert markers["new-id"] == diff.MARK_ID_MIGRATED

    def test_内容也变了压过id_migrated(self) -> None:
        """内容也变了的时候，人要重看的是**内容**。"""
        markers = diff.mark_changes(
            [_row("new-id", legacy="old-id", label="blocked")],
            [_row("old-id", label="success")],
        )
        assert markers["new-id"] == diff.MARK_CHANGED

    def test_没有可比对象全判new(self) -> None:
        """⚠️ 不是 ``unchanged``——后者说的是「不用看」，方向相反。"""
        markers = diff.mark_changes([_row("a"), _row("b")], [])
        assert set(markers.values()) == {diff.MARK_NEW}

    def test_上一版是None也全判new(self) -> None:
        markers = diff.mark_changes([_row("a")], None)
        assert markers["a"] == diff.MARK_NEW


# ── content_key 的边界 ──────────────────────────────────────────


class TestContentKey:
    def test_不含id(self) -> None:
        """键变了不该被算成「内容变了」——那是 ``id_migrated`` 的事。"""
        a = diff.content_key(_row("a", legacy="old"))
        b = diff.content_key(_row("b", legacy="old"))
        assert a == b

    def test_评分面算内容(self) -> None:
        a = diff.content_key(_row("a", dims={"d3": 1}))
        b = diff.content_key(_row("a", dims={"d3": 2}))
        assert a != b

    def test_字典序无关(self) -> None:
        """比较不能靠 dict 的插入序。"""
        a = diff.content_key(_row("a", dims={"x": 1, "y": 2}))
        b = diff.content_key(_row("a", dims={"y": 2, "x": 1}))
        assert a == b

    def test_观测面变了算内容(self) -> None:
        """方案 20 §R2 把面与时刻加进了**金标块** ⇒ 「金标变了」就包括它们。

        ⚠️ 不算的话：页面上一行显示着「human-browser · 2026-10-12」、标记却
        写着 ``unchanged`` ⇒ **人看到的与标记对不上** ⇒ 标记的作用（一眼看出
        哪几行新变了）就没了。
        """
        a = diff.content_key(_row("a"))
        b = diff.content_key(
            _row("a", surface="human-browser", observed_at="2026-10-12T04:01:21+00:00")
        )
        assert a != b

    def test_观测时刻变了也算内容(self) -> None:
        a = diff.content_key(_row("a", surface="human-browser"))
        b = diff.content_key(
            _row("a", surface="human-browser", observed_at="2026-10-12T04:01:21+00:00")
        )
        assert a != b

    def test_署名与时间戳不算内容(self) -> None:
        """⚠️ **判据要宽，但宽在「人看到的东西变了」上，不宽在「时间戳动了」上**。

        ``reviewed_at`` 是写下的那一刻 ⇒ 每次重新标注都变 ⇒ 放进来 ⇒ 标记动辄
        半版都是 ``changed`` ⇒ 「重点看」这个词就废了。
        """
        a = diff.content_key(_row("a"))
        b = diff.content_key(
            _row(
                "a",
                reviewed_by="manual-review",
                reviewed_at="2026-10-29T10:00:00+08:00",
                note="改了个理由",
            )
        )
        assert a == b

    def test_面变了判changed(self) -> None:
        """端到端：标记也变（不只是 content_key 变）。"""
        markers = diff.mark_changes(
            [_row("a", surface="human-browser", observed_at="2026-10-12T04:01:21+00:00")],
            [_row("a")],
        )
        assert markers["a"] == diff.MARK_CHANGED


# ── 过滤与统计 ──────────────────────────────────────────────────


class TestFilterAndSummarize:
    def test_默认全留(self) -> None:
        """owner 裁定「一致行也传」（裁定②）——默认不能只留变的。"""
        rows = [_row("a"), _row("b")]
        markers = diff.mark_changes(rows, [_row("a")])
        kept = diff.filter_changed(rows, markers)
        assert [r["golden_id"] for r in kept] == ["a", "b"]

    def test_only_changed只留三种(self) -> None:
        """``new`` / ``changed`` / ``id_migrated`` 都留下，``unchanged`` 不留。"""
        previous = [_row("a"), _row("c")]
        rows = [_row("a"), _row("b"), _row("c", label="blocked")]
        markers = diff.mark_changes(rows, previous)
        assert markers == {
            "a": diff.MARK_UNCHANGED,
            "b": diff.MARK_NEW,
            "c": diff.MARK_CHANGED,
        }
        kept = diff.filter_changed(rows, markers, only_changed=True)
        assert [r["golden_id"] for r in kept] == ["b", "c"]

    def test_统计按固定顺序(self) -> None:
        rows = [_row("a"), _row("b"), _row("c")]
        markers = diff.mark_changes(rows, [_row("a")])
        counts = diff.summarize_markers(rows, markers)
        assert list(counts) == list(diff.ALL_MARKERS)
        assert counts[diff.MARK_UNCHANGED] == 1
        assert counts[diff.MARK_NEW] == 2
