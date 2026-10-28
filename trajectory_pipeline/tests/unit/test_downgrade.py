"""``executor.downgrade`` 的测试。

盯的是**那些真的咬过人的坑**，不是「函数能不能跑」：
处置范围静默扩大、自己写的降级理由被当成判据、``visits[].success``
漏翻导致 ``success_branch_mismatch``。
"""

from __future__ import annotations

import json

import pytest

from trajectory_pipeline.executor import downgrade as dg


def _arch(outcomes, visits=None):
    return {
        "task_id": "T001", "title": "功夫",
        "ledger": {"by_branch": {"no_play_control": 2, "not_play_site": 1},
                   "negative_samples": 3, "missing_branches": []},
        "visits": visits if visits is not None else [],
        "outcomes": outcomes,
    }


def _o(url, branch, evidence="判定正文", **over):
    rec = {"url": url, "branch": branch, "evidence": evidence,
           "is_negative_sample": branch is not None}
    rec.update(over)
    return rec


def _write(tmp_path, arch, name="T001__r1.json"):
    p = tmp_path / name
    p.write_text(json.dumps(arch, ensure_ascii=False), encoding="utf-8")
    return p


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


class Test判定正文:
    def test_只取第一段(self):
        """**追加的降级理由里也含判据关键词**——全文匹配会把自己
        刚降的记录再降一次。这个 bug 真发生过：第二版改用正则却仍
        全文匹配，把刚恢复的 5 条又降了级。"""
        ev = "原判正文\n\n人工降级：否定依据是「0 个交互元素」"
        assert dg.base_evidence(ev) == "原判正文"

    def test_剥掉原判前缀(self):
        assert dg.base_evidence("[原判 no_play_control] 正文") == " 正文"

    def test_200个不算0个(self):
        """负向前视。``"0 个交互元素" in "200 个交互元素"`` 是 True。

        走**真实的 selector**，不为这条另写一个谓词——判据的第二份
        实现正是今天两次漂移的成因（见模块里 ``ZERO_ELEMENTS`` 的注释）。
        """
        arch = _arch([_o("a.com", "no_play_control",
                         evidence="200 个交互元素中命中播放控件")])
        assert dg.select_by_evidence([dg.ZERO_ELEMENTS])(arch) == []

    def test_真0个命中(self):
        arch = _arch([_o("a.com", "no_play_control",
                         evidence="0 个交互元素中无一命中播放控件词表")])
        assert len(dg.select_by_evidence([dg.ZERO_ELEMENTS])(arch)) == 1

    def test_理由段里的不算(self):
        arch = _arch([_o("a.com", "no_play_control",
                         evidence="判定正文\n\n理由提到 0 个交互元素")])
        assert dg.select_by_evidence([dg.ZERO_ELEMENTS])(arch) == []


class Test选择器:
    def test_不受理unresolved(self):
        """它已经是降级后的状态。把它算进来正是那 8 条误伤的起因——
        幂等判断该看「有没有处置戳」，不靠 branch 值。"""
        with pytest.raises(ValueError, match="不该含"):
            dg.select_by_evidence([dg.ZERO_ELEMENTS], branches=[dg.TARGET_BRANCH])

    def test_不给分支参数也不受理unresolved(self):
        """**这条盯的是沉成子命令后长出来的那次复发。**

        守卫原先挂在「调用方显式传了 ``branches``」上，于是
        ``--evidence``（走 ``branches=None``）把 9 条**已经 unresolved**
        的记录整整齐齐选了出来，每行还印着 ``unresolved×1``。

        「不受理目标态」是**处置动作的定义**，不是可选的过滤规则——
        挂到调用方给什么参数上，就等于给同一个 bug 留了第二个入口。
        """
        arch = _arch([_o("a.com", dg.TARGET_BRANCH,
                         evidence="0 个交互元素中无一命中播放控件词表")])
        assert dg.select_by_evidence([dg.ZERO_ELEMENTS])(arch) == []

    def test_select_by_url同样不受理unresolved(self):
        arch = _arch([_o("a.com", dg.TARGET_BRANCH)])
        assert dg.select_by_url("a.com")(arch) == []

    def test_URL路也认分支过滤(self):
        """``--url`` 那条路**必须**认 ``branches``。

        2026-10-28 实测踩到：CLI 的 ``--branch`` 原本只走 ``--evidence``，
        ``--url`` 那条路**静默忽略**它 ⇒ 同一个 URL 上别的分支会被一起降级。
        实测那批数据里一个 URL 挂着 **11 条 ``not_play_site`` + 4 条
        ``unreachable_hard``** ⇒ 不限分支就是**误伤 11 条真负样本**，
        而报告上只看得到「降级 15 条」——看不出误伤。
        """
        arch = _arch([
            _o("x.com", "unreachable_hard"),
            _o("x.com", "not_play_site"),
            _o("x.com", "login_required"),
        ])
        sel = dg.select_by_url("x.com", branches=["unreachable_hard"])
        assert [r["branch"] for r in sel(arch)] == ["unreachable_hard"]

    def test_URL路不认unresolved作过滤(self):
        """「不受理目标态」是**处置动作的定义**，不是可选的过滤规则——
        两条路都得拒（见模块 docstring 坑 6：守卫挂在哪个入口，
        就是给同一个 bug 留几个入口）。"""
        with pytest.raises(ValueError):
            dg.select_by_url("x.com", branches=[dg.TARGET_BRANCH])

    def test_已打过戳的跳过(self):
        arch = _arch([_o("a.com", "no_play_control",
                         evidence="[原判 no_play_control] 正文\n\n已降级")])
        assert dg.select_by_url("a.com")(arch) == []

    def test_按evidence只认原始正文(self):
        arch = _arch([_o("a.com", "no_play_control", evidence="0 个交互元素")])
        arch["outcomes"][0]["evidence"] += "\n\n补：这里也是 0 个交互元素"
        sel = dg.select_by_evidence([dg.ZERO_ELEMENTS])
        assert len(sel(arch)) == 1

    def test_空片段与空正则都拒(self):
        with pytest.raises(ValueError):
            dg.select_by_url()
        with pytest.raises(ValueError):
            dg.select_by_evidence([])


class Test处置:
    def test_改的是传入的那个对象(self, tmp_path):
        """``apply_downgrades`` **只改内存对象**。传进去的 archive 必须
        真的变了——它不读盘也不写盘，收了路径却没落盘这种事必须
        结构上不可能（见函数 docstring）。"""
        arch = _arch([_o("a.com", "no_play_control")])
        p = _write(tmp_path, arch)
        s = dg.apply_downgrades(arch, hits=arch["outcomes"], reason="r",
                                name=p.name, dry_run=False)
        assert s["downgraded"] == 1
        assert arch["outcomes"][0]["branch"] == dg.TARGET_BRANCH

    def test_报告说降了就真落盘了(self, tmp_path):
        """**这条盯的是一次真的静默失败。**

        第一版 ``apply_downgrades`` 收路径、内部自己重读一份再写盘，
        而调用方传的 ``hits`` 来自它自己读的那份：改的是 A、写的是 B，
        **什么都没落盘而返回值说「降级 N 条」**。

        它长得极像成功——清单整整齐齐，下一次读存档发现一条没变，
        而没有任何东西提示要查。所以端到端这条（走 ``apply_to_root``、
        断言**文件**变了）必须与上面那条分开写，不合并。
        """
        p = _write(tmp_path, _arch([_o("a.com", "no_play_control")]))
        out = dg.apply_to_root(tmp_path, select=dg.select_by_url("a.com"),
                               reason="r", dry_run=False)
        assert out[0]["downgraded"] == 1
        assert _read(p)["outcomes"][0]["branch"] == dg.TARGET_BRANCH

    def test_dry_run不落盘(self, tmp_path):
        p = _write(tmp_path, _arch([_o("a.com", "no_play_control")]))
        before = p.read_text(encoding="utf-8")
        dg.apply_to_root(tmp_path, select=dg.select_by_url("a.com"),
                         reason="理由", dry_run=True)
        assert p.read_text(encoding="utf-8") == before

    def test_理由必填(self, tmp_path):
        arch = _arch([_o("a.com", "no_play_control")])
        with pytest.raises(ValueError, match="--reason"):
            dg.apply_downgrades(arch, hits=arch["outcomes"], reason="  ")

    def test_留痕可还原(self, tmp_path):
        p = _write(tmp_path, _arch([_o("a.com", "no_play_control",
                                       evidence="原正文")]))
        arch = _read(p)
        dg.apply_downgrades(arch, hits=arch["outcomes"], reason="取证依据",
                            name=p.name, dry_run=False)
        rec = arch["outcomes"][0]
        assert rec["branch"] == dg.TARGET_BRANCH
        assert rec["branch_before_downgrade"] == "no_play_control"
        assert "原正文" in rec["evidence"] and "取证依据" in rec["evidence"]
        # 幂等：再跑一次选不中它
        assert dg.select_by_url("a.com")(arch) == []

    def test_摘掉负样本标记(self, tmp_path):
        """它是「这条是否进池」的判据，而 unresolved 不进池。"""
        arch = _arch([_o("a.com", "not_play_site")])
        p = _write(tmp_path, arch)
        dg.apply_downgrades(arch, hits=arch["outcomes"], reason="r",
                            name=p.name, dry_run=False)
        assert "is_negative_sample" not in arch["outcomes"][0]

    def test_同步visits成功位(self, tmp_path):
        """「成功」在存档里记在两处：``outcome.branch is None`` 与
        ``visits[i].success``。只改一处就留下 ``success=True 但
        branch=unresolved``——``check-archives`` 的
        ``success_branch_mismatch`` 正查这个。"""
        arch = _arch([_o("a.com", "no_play_control")],
                     visits=[{"url": "a.com", "landed_url": "a.com",
                              "success": True}])
        p = _write(tmp_path, arch)
        s = dg.apply_downgrades(arch, hits=arch["outcomes"], reason="r",
                                name=p.name, dry_run=False)
        assert s["visits"] == 1
        v = arch["visits"][0]
        assert v["success"] is False and "success_downgraded_by" in v

    def test_不同URL的visits不动(self, tmp_path):
        arch = _arch([_o("a.com", "no_play_control")],
                     visits=[{"url": "b.com", "landed_url": "b.com",
                              "success": True}])
        p = _write(tmp_path, arch)
        assert dg.apply_downgrades(arch, hits=arch["outcomes"], reason="r",
                                   name=p.name,
                                   dry_run=False)["visits"] == 0

    def test_不动reached_play_page(self, tmp_path):
        """它记的是**采集事实**（控制流到没到过播放页 URL）。
        为了让 D-5 判 5 分而改成 False，等于**改写采集事实去迎合结论**。"""
        arch = _arch([_o("a.com", "no_play_control", reached_play_page=True)])
        p = _write(tmp_path, arch)
        dg.apply_downgrades(arch, hits=arch["outcomes"], reason="r",
                            name=p.name, dry_run=False)
        assert arch["outcomes"][0]["reached_play_page"] is True

    def test_ledger记账(self, tmp_path):
        arch = _arch([_o("a.com", "no_play_control"),
                      _o("b.com", "no_play_control")])
        p = _write(tmp_path, arch)
        dg.apply_downgrades(arch, hits=arch["outcomes"], reason="r",
                            name=p.name, dry_run=False)
        led = arch["ledger"]
        assert led["by_branch"][dg.TARGET_BRANCH] == 2
        assert "no_play_control" not in led["by_branch"]   # 归零则删键
        assert led["negative_samples"] == 1               # 3 - 2
        assert dg.TARGET_BRANCH in led["missing_branches"]

    def test_判成功的不从池子里减(self, tmp_path):
        """``branch=None`` 是「这条判成功」，**本来就不在池里**。

        按 ``len(hits)`` 减会把 ``negative_samples`` 减成比实际小，
        而报表只看这一个数——池子少了，训练数据就少了一条，而报告
        看不出是哪条减的。
        """
        arch = _arch([_o("a.com", None)])
        p = _write(tmp_path, arch)
        dg.apply_downgrades(arch, hits=arch["outcomes"], reason="r",
                            name=p.name, dry_run=False)
        assert arch["ledger"]["negative_samples"] == 3    # 原样不动

    def test_降成功要减ledger的成功数(self, tmp_path):
        """**「成功」记在三处**：``outcome.branch`` / ``visits[].success`` /
        ``ledger.succeeded``。

        第三处是**落盘时的快照**（``branches.summary()`` 算好的），
        不跟着减 ⇒ 存档里「成功 3 条」而 ``outcomes`` 里只剩 2 条。
        门禁**不查这一项**（只查 visits ↔ branch），所以它会一路飘到
        报表的成功率上（模块 docstring 坑 7）。
        """
        arch = _arch([_o("a.com", None), _o("b.com", None)])
        arch["ledger"]["succeeded"] = 2
        p = _write(tmp_path, arch)
        dg.apply_downgrades(arch, hits=[arch["outcomes"][0]], reason="r",
                            name=p.name, dry_run=False)
        assert arch["ledger"]["succeeded"] == 1
        # 实际成功条数与快照必须一致（这条才是门禁该守的不变量）
        real = sum(1 for o in arch["outcomes"] if o.get("branch") is None)
        assert arch["ledger"]["succeeded"] == real

    def test_降负样本不动ledger的成功数(self, tmp_path):
        """降的是负样本 ⇒ 成功数**不动**（别顺手减一）。"""
        arch = _arch([_o("a.com", "no_play_control")])
        arch["ledger"]["succeeded"] = 4
        p = _write(tmp_path, arch)
        dg.apply_downgrades(arch, hits=arch["outcomes"], reason="r",
                            name=p.name, dry_run=False)
        assert arch["ledger"]["succeeded"] == 4


class Test目录遍历:
    def test_非T前缀的存档也要收(self, tmp_path):
        """**``E``/``P``/``DR`` 前缀的存档以前结构性地降不掉。**

        原先遍历是 ``root.glob("T*__*.json")``。实测（2026-10-26）生产根
        目录 78 份存档里 28 份是 ``E``/``P`` 前缀，双跑那批判是 ``DR01__``
        ⇒ H-17 那 7 条（全是 ``DR`` 前缀）**根本没有命令能碰**，而 CLI
        打印的是「没有匹配的 outcome —— 归档可能已被降过（幂等），或
        判据不匹配」：判据被冤枉，真正的成因在文件名上。
        """
        for name in ("E005__aa.json", "P001__bb.json", "DR01__cc.json"):
            _write(tmp_path, _arch([_o("a.com", "no_play_control")]), name=name)
        out = dg.apply_to_root(tmp_path, select=dg.select_by_url("a.com"),
                               reason="r", dry_run=True)
        assert sorted(s["archive"] for s in out) == [
            "DR01__cc.json", "E005__aa.json", "P001__bb.json"]

    def test_同目录的探针取证不算存档(self, tmp_path):
        """``obscura_tools.json`` / ``_cause_probe.json`` 这类与 P1 同目录、
        却不是存档（文件名里没有 ``__``）。收进来会让「降级了 N 条」里
        混进**根本没有 run_id** 的文件，而它的改动无处可回溯。"""
        _write(tmp_path, _arch([_o("a.com", "no_play_control")]),
               name="_cause_probe.json")
        assert dg.original_archive_paths(tmp_path) == []

    def test_复核档也改(self, tmp_path):
        """**两层都改**（2026-10-28 改）——复核档是门禁与报表**实际读的
        那一份**（``select_archives`` 优先取它）。

        原先只动原档 ⇒ 复核档还留着旧分支 ⇒ 门禁当场报
        ``negative_not_pooled``，而人看到的却是「已经降过级了」。
        :mod:`executor.relabel` 早就是两份都改，理由一模一样。
        """
        for name in ("T001__a.json", "T001__a.reviewed.json"):
            _write(tmp_path, _arch([_o("a.com", "no_play_control")]), name)
        out = dg.apply_to_root(tmp_path, select=dg.select_by_url("a.com"),
                               reason="r", dry_run=False)
        assert sorted(s["archive"] for s in out) == [
            "T001__a.json", "T001__a.reviewed.json"]
        assert sorted(s["layer"] for s in out) == ["原档", "复核档"]
        for name in ("T001__a.json", "T001__a.reviewed.json"):
            rev = _read(tmp_path / name)
            assert rev["outcomes"][0]["branch"] == "unresolved"
            assert rev["outcomes"][0]["branch_before_downgrade"] == \
                "no_play_control"
            # 两层的 ledger 都要跟着走，否则报表上的负样本数比实际多
            assert rev["ledger"]["negative_samples"] == 2

    def test_带人工裁定的复核档不动(self, tmp_path):
        """**人的裁定优先于代码侧的「判定不成立」**。

        代码能说「这条标签没依据」，不能说「人判的这条不是负样本」——
        那是另一层的结论。复核档上带 ``review_verdict`` / ``site_verdict``
        的记录因此不受理降级（原档照降——两层各记各的）。
        """
        for name in ("T001__a.json", "T001__a.reviewed.json"):
            _write(tmp_path, _arch([_o("a.com", "no_play_control")]), name)
        rev_path = tmp_path / "T001__a.reviewed.json"
        rev = _read(rev_path)
        rev["outcomes"][0]["review_verdict"] = "confirm_not_play_site"
        rev["outcomes"][0]["branch"] = "not_play_site"
        rev_path.write_text(json.dumps(rev, ensure_ascii=False),
                            encoding="utf-8")
        out = dg.apply_to_root(tmp_path, select=dg.select_by_url("a.com"),
                               reason="r", dry_run=False)
        assert [s["archive"] for s in out] == ["T001__a.json"]
        # 原档降了
        assert _read(tmp_path / "T001__a.json")["outcomes"][0]["branch"] \
            == "unresolved"
        # 复核档的人工裁定原样保留
        assert _read(rev_path)["outcomes"][0]["branch"] == "not_play_site"

    def test_没有配对原档的复核档不算(self, tmp_path):
        """``<run>.reviewed.json`` 这种**死文件**（对不上任何
        ``trajectory_id``）不是复核档——改了它，改动无处可回溯。"""
        _write(tmp_path, _arch([_o("a.com", "no_play_control")]),
               name="T001__a.json")
        _write(tmp_path, _arch([_o("a.com", "no_play_control")]),
               name="T001__a.reviewed.json")
        (tmp_path / "T001__a.json").unlink()
        assert dg.reviewed_archive_paths(tmp_path) == []
        out = dg.apply_to_root(tmp_path, select=dg.select_by_url("a.com"),
                               reason="r", dry_run=False)
        assert out == []

    def test_选择器就是处置判据(self, tmp_path):
        """**筛选与处置必须同一份判据**。

        第一版让 ``apply_downgrades`` 自己按参数重筛，而遍历函数另外用
        ``predicate`` 预筛「有没有命中」——于是 predicate 说「不算」的
        记录照样被降级，**处置范围静默扩大**，而跑完只报一个总数。
        """
        arch = _arch([_o("a.com", "no_play_control", evidence="命中判据"),
                      _o("b.com", "no_play_control", evidence="无关正文")])
        p = _write(tmp_path, arch)

        def select(a):
            return [r for r in a["outcomes"] if "命中判据" in r["evidence"]]

        out = dg.apply_to_root(tmp_path, select=select, reason="r",
                               dry_run=False)
        assert out[0]["downgraded"] == 1
        after = {r["url"]: r["branch"] for r in _read(p)["outcomes"]}
        assert after["a.com"] == dg.TARGET_BRANCH
        assert after["b.com"] == "no_play_control"      # **没被动**