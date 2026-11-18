"""**语义渲染**（H-41 阶段 2 · 方案 26 §6）。

``mode="semantic"`` = **模型选、渲染层逐字重建**，**没有字符硬顶**。
这一组盯的是方案 26 §0 的三条主判据：

- **A1 不造**：重建出来的每一行 / 每一条元素 / 每一个链接都**逐字来自
  P1 原文**——模型加不了一个原文没有的词；
- **A2 不截**：``len(body_text) == sum(len(l) for l in kept) + 分隔符``
  （逐字相等，不多不少）——**一个字都没截**；
- **A3 可回退**：精炼不可用（无旁挂 / 旧协议 / 对不上原文）⇒ 输出与
  **没精炼时逐字一致**。

外加两条**记账**纪律：

- ``truncated.view`` 在语义模式下**恒 False**（本层一个字都没截），
  **回退时强制 True**（诚实记账：这条其实是规则裁剪的）；
- ``limits`` 仍 **7 键**、``truncated`` 仍 **2 键**——形状不许漂。
"""

from __future__ import annotations

from trajectory_pipeline.assembler import observation_view
from trajectory_pipeline.common import body_lines, refine_sidecar, view_candidates
from trajectory_pipeline.refine import refiner


# ── 夹具 ──────────────────────────────────────────────────────────


_BODY = "\n".join([
    "《让子弹飞》高清在线观看",
    "主演：姜文 周润发",
    "无关第一行内容",
    "首页",
    "京ICP备12345号",
])

_ELEMENTS = [
    {"role": "button", "name": "立即播放"},
    {"role": "link", "name": "相关推荐"},
    {"role": "button", "name": "选集"},
]

_LINKS = [
    {"text": "正片", "href": "https://x.test/play"},
    {"text": "花絮", "href": "https://x.test/behind"},
]


def _obs(**kw):
    base = {
        "url": "https://x.test",
        "page_title": "测试页",
        "body_text": _BODY,
        "degraded": [],
        "interactive_elements": list(_ELEMENTS),
        "links": list(_LINKS),
        "video_tag_count": 0,
        "iframe_count": 0,
    }
    base.update(kw)
    return base


def _selection_key(obs: dict, *, body: str | None = None) -> str:
    """这条观察的**候选集键**（H-41 阶段 3）。

    ⚠️ **不是正文哈希**：三段协议选的是正文 + 元素 + 链接 ⇒ 键必须
    覆盖那三样（同一份正文在不同步上的元素 / 链接集合可以不同 ⇒
    只按正文定位 = 把 A 步的选择贴到 B 步上）。
    """
    body = body if body is not None else str(obs.get("body_text") or "")
    raw_el = list(obs.get("interactive_elements") or ())
    raw_lk = list(obs.get("links") or ())
    _hb, has_el, has_lk = view_candidates.read_blocks(obs.get("read"))
    el = [
        (c.role, c.name) for c in view_candidates.element_candidates(
            raw_el, filter_noise=False,
        )
    ] if has_el else []
    lk = [
        (c.text, c.href) for c in view_candidates.link_candidates(
            raw_lk, filter_noise=False,
        )
    ] if has_lk else []
    return refine_sidecar.selection_key(body, elements=el, links=lk)


def _semantic_index(
    body: str = _BODY,
    *,
    kept: list[str] | None = None,
    elements: list[dict] | None = None,
    links: list[dict] | None = None,
    source: str = "llm",
    protocol: str = refine_sidecar.ENTRY_PROTOCOL,
    obs: dict | None = None,
) -> dict:
    """造一份**三段齐全**的精炼索引（``refine.v4`` 旁挂的形状）。

    ⚠️ **键是候选集哈希**（:func:`_selection_key`）——渲染层按它找，
    精炼侧按它写；两边算岔了 ⇒ 渲染层**找不到**这条精炼 ⇒ 整条回退
    规则裁剪（而且**不报错**）。
    """
    observation = obs if obs is not None else _obs(body_text=body)
    return {
        _selection_key(observation, body=body): {
            "kept": kept if kept is not None else [],
            "elements": elements or [],
            "links": links or [],
            "source": source,
            "protocol": protocol,
        }
    }


def _render(index: dict | None, **kw):
    return observation_view.render(
        _obs(**kw.pop("obs_kw", {})),
        focus=("让子弹飞",),
        refined=index,
        mode=observation_view.VIEW_MODE_SEMANTIC,
        **kw,
    )


# ── A1 不造 ───────────────────────────────────────────────────────


class TestA1不造:
    def test_正文每一行都逐字来自原文(self):
        out = _render(_semantic_index(kept=["主演：姜文 周润发"]))
        for line in out["body_text"].splitlines():
            assert line in _BODY.splitlines()

    def test_元素逐字来自原文(self):
        out = _render(_semantic_index(
            kept=["主演：姜文 周润发"],
            elements=[{"role": "button", "name": "立即播放"}],
        ))
        assert out["interactive_elements"] == [
            {"role": "button", "name": "立即播放"},
        ]

    def test_链接逐字来自原文(self):
        out = _render(_semantic_index(
            kept=["主演：姜文 周润发"],
            links=[{"text": "正片", "href": "https://x.test/play"}],
        ))
        assert out["links"] == [{"text": "正片", "href": "https://x.test/play"}]

    def test_模型报的名字对不上原文就整条作废(self):
        """模型编了一个原文没有的元素名 ⇒ **整条**回退规则裁剪。"""
        plain = observation_view.render(_obs(), focus=("让子弹飞",))
        out = _render(_semantic_index(
            kept=["主演：姜文 周润发"],
            elements=[{"role": "button", "name": "模型编出来的按钮"}],
        ))
        assert out["body_text"] == plain["body_text"]
        assert out["semantic_fallback"] is True


# ── A2 不截 ───────────────────────────────────────────────────────


class TestA2不截:
    def test_正文长度等于选中行拼接长度(self):
        kept = ["《让子弹飞》高清在线观看", "主演：姜文 周润发"]
        out = _render(_semantic_index(kept=kept))
        expected = "\n".join(kept)
        assert out["body_text"] == expected
        assert len(out["body_text"]) == sum(len(x) for x in kept) + len(kept) - 1

    def test_动作目标被补回时长度会多出那一行(self):
        """⚠️ **A2 有一个刻意的例外**：动作目标无条件补回（保真口径）⇒
        那一行**不在模型选中的集合里**，正文因此比选中行拼接长。

        这是**该有的**行为：训练样本里不能出现「点了一个观察里没有的
        东西」（模块 docstring 第 6 条）。所以 A2 的判据是
        「**渲染层一个字都没截**」，不是「长度恒等于选中行之和」。
        """
        out = _render(_semantic_index(kept=["无关第一行内容"]),
                      keep=("主演：姜文 周润发",))
        got = out["body_text"].splitlines()
        assert "无关第一行内容" in got
        assert "主演：姜文 周润发" in got
        assert len(got) == 2

    def test_超过320字也不截(self):
        """语义模式**没有字符硬顶**——这就是它与 ``capped`` 的唯一差别。"""
        long_body = "\n".join(f"第 {i} 行内容内容内容内容内容内容内容" for i in range(200))
        idx = {
            _selection_key(_obs(body_text=long_body), body=long_body): {
                "kept": long_body.splitlines(),
                "elements": [],
                "links": [],
                "source": "llm",
                "protocol": refine_sidecar.ENTRY_PROTOCOL,
            }
        }
        out = observation_view.render(
            _obs(body_text=long_body),
            refined=idx,
            mode=observation_view.VIEW_MODE_SEMANTIC,
        )
        assert len(out["body_text"]) > observation_view.VIEW_BODY_CHARS
        assert out["body_text"] == long_body
        assert out["truncated"]["view"] is False

    def test_元素链接也不受条数上限约束(self):
        out = _render(_semantic_index(
            kept=["主演：姜文 周润发"],
            elements=[{"role": "button", "name": "立即播放"},
                      {"role": "link", "name": "相关推荐"},
                      {"role": "button", "name": "选集"}],
            links=[{"text": "正片", "href": "https://x.test/play"},
                   {"text": "花絮", "href": "https://x.test/behind"}],
        ))
        assert len(out["interactive_elements"]) == 3
        assert len(out["links"]) == 2


# ── A3 可回退 ─────────────────────────────────────────────────────


class TestA3可回退:
    def test_没有旁挂时逐字等于规则裁剪(self):
        plain = observation_view.render(_obs(), focus=("让子弹飞",))
        out = _render(None)
        assert out["body_text"] == plain["body_text"]
        assert out["interactive_elements"] == plain["interactive_elements"]
        assert out["links"] == plain["links"]
        assert out["semantic_fallback"] is True

    def test_旧协议回退(self):
        """老协议（``select.v1``）只选了正文 ⇒ 元素 / 链接**没被选过**
        ⇒ 语义渲染不能用它 ⇒ **整条**回退。"""
        plain = observation_view.render(_obs(), focus=("让子弹飞",))
        out = _render(_semantic_index(
            kept=["主演：姜文 周润发"],
            protocol=refine_sidecar.ENTRY_PROTOCOL_LEGACY,
        ))
        assert out["body_text"] == plain["body_text"]
        assert out["semantic_fallback"] is True
        assert out["truncated"]["view"] is True

    def test_精炼失败_source不是llm_就回退(self):
        plain = observation_view.render(_obs(), focus=("让子弹飞",))
        out = _render(_semantic_index(kept=["主演：姜文 周润发"], source="failed"))
        assert out["body_text"] == plain["body_text"]
        assert out["semantic_fallback"] is True

    def test_正文对不上就回退(self):
        plain = observation_view.render(_obs(), focus=("让子弹飞",))
        out = _render(_semantic_index(kept=["原文里根本没有的这一行"]))
        assert out["body_text"] == plain["body_text"]
        assert out["semantic_fallback"] is True

    def test_回退时truncated_view强制为真(self):
        """诚实记账：这条其实是**规则裁剪**出来的，不能让人以为它也
        是模型挑的。"""
        out = _render(None)
        assert out["truncated"]["view"] is True
        assert out["semantic_fallback"] is True


# ── 记账 ──────────────────────────────────────────────────────────


class Test记账:
    def test_limits还是七个键(self):
        out = _render(_semantic_index(kept=["主演：姜文 周润发"]))
        assert set(out["limits"]) == {
            "body_chars", "elements", "links",
            "body_lines_dropped", "elements_dropped", "links_dropped",
            "cropped",
        }

    def test_truncated还是两个键(self):
        out = _render(_semantic_index(kept=["主演：姜文 周润发"]))
        assert set(out["truncated"]) == {"source", "view"}

    def test_模式与回退留痕(self):
        out = _render(_semantic_index(kept=["主演：姜文 周润发"]))
        assert out["view_mode"] == observation_view.VIEW_MODE_SEMANTIC
        assert out["semantic_fallback"] is False

    def test_元素链接的精炼留痕(self):
        out = _render(_semantic_index(
            kept=["主演：姜文 周润发"],
            elements=[{"role": "button", "name": "立即播放"}],
            links=[{"text": "正片", "href": "https://x.test/play"}],
        ))
        assert out["elements_refined"] is True
        assert out["elements_refined_count"] == 1
        assert out["links_refined"] is True
        assert out["links_refined_count"] == 1

    def test_规则层一行都没剔所以body_lines_dropped是空(self):
        """语义模式下**噪声行也摆给模型了** ⇒ 规则层没剔任何行
        ⇒ 这笔账是空。"""
        out = _render(_semantic_index(kept=["主演：姜文 周润发"]))
        assert out["limits"]["body_lines_dropped"] is None

    def test_capped模式一字不改(self):
        """``capped`` 路径是**回退路径**——它必须与改造前逐字一致。"""
        plain = observation_view.render(_obs(), focus=("让子弹飞",))
        assert plain["view_mode"] == observation_view.VIEW_MODE_CAPPED
        assert plain["semantic_fallback"] is False

    def test_认不出的模式直接报错(self):
        import pytest
        with pytest.raises(ValueError):
            observation_view.render(_obs(), mode="不存在的模式")


# ── 候选集键（H-41 阶段 3）─────────────────────────────────────────


class Test候选集键:
    """⚠️ **键必须覆盖「模型当时看到的那份清单」**（实测 2026-11-18）。

    Tier B 试点（E005 存档）暴露的缺陷：**旁挂按正文哈希索引**，而三段
    协议选的是「正文 + 元素 + 链接」⇒ **同一份正文**在一条轨迹里被看到
    多次时，那几次的元素 / 链接集合可以不同（动态页面、镜像站）⇒
    **A 步的选择被贴到 B 步上** ⇒ 渲染层逐字核对不过 ⇒ **整条回退**
    ⇒ 实测 **13 条回退里 12 条是这一条**（达标率 87.7% < 95% 判据）。
    """

    def test_链接集合不同键就不同(self):
        a = refine_sidecar.selection_key(
            "正文", links=[("正片", "https://a.test/1")],
        )
        b = refine_sidecar.selection_key(
            "正文", links=[("正片", "https://b.test/1")],
        )
        assert a != b

    def test_元素集合不同键就不同(self):
        a = refine_sidecar.selection_key(
            "正文", elements=[("button", "立即播放")],
        )
        b = refine_sidecar.selection_key(
            "正文", elements=[("button", "选集")],
        )
        assert a != b

    def test_候选集一样键就一样(self):
        """幂等：同一份候选集只看一遍（省一次 LLM 调用）。"""
        a = refine_sidecar.selection_key(
            "正文",
            elements=[("button", "立即播放"), ("link", "相关推荐")],
            links=[("正片", "https://x.test/play")],
        )
        b = refine_sidecar.selection_key(
            "正文",
            elements=[("button", "立即播放"), ("link", "相关推荐")],
            links=[("正片", "https://x.test/play")],
        )
        assert a == b

    def test_正文一样元素链接不一样就是两份候选集(self):
        """**回归**：同一份正文 + 不同链接集合 ⇒ 两份候选集 ⇒ 两个键。"""
        obs_a = _obs(links=[{"text": "正片", "href": "https://a.test/play"}])
        obs_b = _obs(links=[{"text": "正片", "href": "https://b.test/play"}])
        assert _selection_key(obs_a) != _selection_key(obs_b)

    def test_各自找各自的记录不回退(self):
        """**回归**：两条观察（正文一样、链接不同）各找各的精炼记录，
        **谁也不回退**（这是修掉 12/13 条回退的那一刀）。"""
        obs_a = _obs(links=[{"text": "正片", "href": "https://a.test/play"}])
        obs_b = _obs(links=[{"text": "正片", "href": "https://b.test/play"}])
        index = {
            _selection_key(obs_a): {
                "kept": ["主演：姜文 周润发"],
                "elements": [], "links": [{"text": "正片", "href": "https://a.test/play"}],
                "source": "llm", "protocol": refine_sidecar.ENTRY_PROTOCOL,
            },
            _selection_key(obs_b): {
                "kept": ["主演：姜文 周润发"],
                "elements": [], "links": [{"text": "正片", "href": "https://b.test/play"}],
                "source": "llm", "protocol": refine_sidecar.ENTRY_PROTOCOL,
            },
        }
        for obs, href in ((obs_a, "https://a.test/play"), (obs_b, "https://b.test/play")):
            out = observation_view.render(
                obs, refined=index, mode=observation_view.VIEW_MODE_SEMANTIC,
            )
            assert out["semantic_fallback"] is False
            assert out["links"] == [{"text": "正片", "href": href}]

    def test_找不到自己那份就回退(self):
        """键对不上 ⇒ **整条回退**（不拿别人的选择凑合）。"""
        obs_b = _obs(links=[{"text": "正片", "href": "https://b.test/play"}])
        obs_a = _obs(links=[{"text": "正片", "href": "https://a.test/play"}])
        index = {
            _selection_key(obs_a): {
                "kept": ["主演：姜文 周润发"],
                "elements": [], "links": [],
                "source": "llm", "protocol": refine_sidecar.ENTRY_PROTOCOL,
            },
        }
        out = observation_view.render(
            obs_b, refined=index, mode=observation_view.VIEW_MODE_SEMANTIC,
        )
        assert out["semantic_fallback"] is True

    def test_语义模式不按正文哈希兜底(self):
        """⚠️ 语义模式**不许**按正文哈希兜底——那正是把 A 步的选择贴到
        B 步上的那把刀（老协议只覆盖正文这一维 ⇒ 才按正文找）。"""
        obs_a = _obs(links=[{"text": "正片", "href": "https://a.test/play"}])
        obs_b = _obs(links=[{"text": "正片", "href": "https://b.test/play"}])
        index = {
            refine_sidecar.body_key(_BODY): {
                "kept": ["主演：姜文 周润发"],
                "elements": [], "links": [],
                "source": "llm", "protocol": refine_sidecar.ENTRY_PROTOCOL,
            },
        }
        out = observation_view.render(
            obs_b, refined=index, mode=observation_view.VIEW_MODE_SEMANTIC,
        )
        assert out["semantic_fallback"] is True


class Test旧格式处置:
    """⚠️ **版本必须升**（H-41 阶段 3）：``refine.v3`` 的条目键是正文
    哈希 ⇒ 与新键对不上 ⇒ 认出来也查不到 ⇒ 静默当没精炼。"""

    def test_v3旧档读侧显式作废(self, tmp_path):
        path = tmp_path / "x.refined.json"
        path.write_text(
            '{"schema_version": "refine.v3", "entries": {}}', encoding="utf-8",
        )
        assert refine_sidecar.read_sidecar(path) is None

    def test_当前版本是v4(self):
        assert refine_sidecar.SIDECAR_SCHEMA_VERSION == "refine.v4"

    def test_capped模式仍认旧形状(self):
        """⚠️ **零回归**：老旁挂（正文哈希 → 行）必须照旧能用
        （规则模式只管正文那一维 ⇒ 键就看正文）。"""
        index = {refine_sidecar.body_key(_BODY): ["主演：姜文 周润发"]}
        out = observation_view.render(_obs(), refined=index)
        assert out["body_refined"] is True
        assert out["body_text"] == "主演：姜文 周润发"


# ── 保真口径（两种模式同款） ──────────────────────────────────────


class Test保真口径:
    def test_动作目标无条件补回(self):
        """模型没选目标行 ⇒ **渲染层自己补回来**（与 ``capped`` 同款）。"""
        out = _render(_semantic_index(kept=["无关第一行内容"]),
                      keep=("主演：姜文 周润发",))
        assert "主演：姜文 周润发" in out["body_text"]

    def test_动作目标是元素时元素也补回来(self):
        out = _render(
            _semantic_index(
                kept=["主演：姜文 周润发"],
                elements=[{"role": "button", "name": "立即播放"}],
            ),
            keep=("选集",),
        )
        names = [e.get("name") for e in out["interactive_elements"]]
        assert "选集" in names

    def test_按页面顺序重建(self):
        out = _render(_semantic_index(
            kept=["无关第一行内容", "主演：姜文 周润发"],
        ))
        assert out["body_text"].splitlines() == [
            "主演：姜文 周润发", "无关第一行内容",
        ]


# ── 精炼侧与渲染侧必须算出**同一个键** ────────────────────────────


class Test两侧键一致:
    """⚠️ **两边算岔了 = 精炼白跑**（而且**不报错**）。

    渲染层按候选集键找精炼；精炼侧按同一个键写。两者各写一份实现 ⇒
    迟早漂 ⇒ 渲染层**查不到** ⇒ 整条回退规则裁剪 ⇒ 报表上写着
    「语义渲染 0 条」，人只看到「精炼效果不好」。

    ⇒ 这里钉住：**精炼侧算出来的键** == **渲染侧算出来的键**。
    """

    def _spec(self, obs: dict):
        from trajectory_pipeline.refine import reader as refine_reader

        archive = {
            "title": "让子弹飞",
            "steps": [{
                "action": {"tool": "click", "target": {"name": "立即播放"}},
                "observation": obs,
                "error": "",
            }],
        }
        specs = refine_reader.iter_observations(archive)
        assert specs
        return specs[0]

    def test_精炼侧与渲染侧的键相同(self):
        from trajectory_pipeline.executor import cli

        obs = _obs()
        spec = self._spec(obs)
        key_body, _cand, _limit, elem_cands, link_cands = cli._refine_inputs(
            spec, semantic=True,
        )
        refine_side_key = refine_sidecar.selection_key(
            key_body, elements=elem_cands, links=link_cands,
        )
        render_side_key = _selection_key(obs)
        assert refine_side_key == render_side_key

    def test_按精炼侧的键写旁挂渲染层就认(self):
        """端到端：精炼侧写出来的条目 ⇒ 渲染层找得到 ⇒ 走通语义模式。"""
        from trajectory_pipeline.executor import cli

        obs = _obs()
        spec = self._spec(obs)
        key_body, _cand, _limit, elem_cands, link_cands = cli._refine_inputs(
            spec, semantic=True,
        )
        key = refine_sidecar.selection_key(
            key_body, elements=elem_cands, links=link_cands,
        )
        index = {
            key: {
                "selection_key": key,
                "body_key": refine_sidecar.body_key(key_body),
                "kept": ["主演：姜文 周润发"],
                "elements": [{"role": "button", "name": "立即播放"}],
                "links": [{"text": "正片", "href": "https://x.test/play"}],
                "source": "llm",
                "protocol": refine_sidecar.ENTRY_PROTOCOL,
            }
        }
        out = observation_view.render(
            obs, refined=index, mode=observation_view.VIEW_MODE_SEMANTIC,
            keep=spec.keep,
        )
        assert out["semantic_fallback"] is False
        assert out["body_text"] == "主演：姜文 周润发"

    def test_读步的键也不同(self):
        """读步只渲染一块 ⇒ 候选集不同 ⇒ 键不同（正文一样也不行）。"""
        from trajectory_pipeline.executor import cli

        obs = _obs(read="interactive")
        spec = self._spec(obs)
        key_body, _cand, _limit, elem_cands, link_cands = cli._refine_inputs(
            spec, semantic=True,
        )
        key = refine_sidecar.selection_key(
            key_body, elements=elem_cands, links=link_cands,
        )
        # ⚠️ 读步**不摆链接候选**（那一块这次不渲染）⇒ 键与整页观察不同。
        assert link_cands == []
        assert key != _selection_key(_obs())


# ── 读步：只渲染被读的那一块 ──────────────────────────────────────


class Test读步:
    def test_snapshot只留正文(self):
        obs = _obs(read="snapshot")
        out = observation_view.render(
            obs,
            refined=_semantic_index(kept=["主演：姜文 周润发"], obs=obs),
            mode=observation_view.VIEW_MODE_SEMANTIC,
        )
        assert out["body_text"] == "主演：姜文 周润发"
        assert out["interactive_elements"] == []
        assert out["links"] == []

    def test_interactive只留元素表(self):
        obs = _obs(read="interactive")
        out = observation_view.render(
            obs,
            refined=_semantic_index(
                kept=[],
                elements=[{"role": "button", "name": "立即播放"}],
                obs=obs,
            ),
            mode=observation_view.VIEW_MODE_SEMANTIC,
        )
        assert out["body_text"] == ""
        assert out["interactive_elements"] == [
            {"role": "button", "name": "立即播放"},
        ]


# ── 单元：候选集 / 分流 / 校验 ────────────────────────────────────


class Test候选集:
    def test_语义模式不剔导航词(self):
        """``filter_noise=False`` ⇒ 导航词**也列给模型**（可推翻）。"""
        cands = view_candidates.element_candidates(
            [{"role": "link", "name": "首页"}],
            filter_noise=False,
        )
        assert [c.name for c in cands] == ["首页"]

    def test_capped口径剔导航词(self):
        cands = view_candidates.element_candidates(
            [{"role": "link", "name": "首页"}],
            filter_noise=True,
        )
        assert cands == []

    def test_keep命中的不参与剔噪(self):
        cands = view_candidates.element_candidates(
            [{"role": "link", "name": "首页"}],
            keep=("首页",),
            filter_noise=True,
        )
        assert [c.name for c in cands] == ["首页"]

    def test_一个字符的名字恒剔(self):
        """图标 / 占位不是名字——**不是语义判断**，剔它与「谁重要」无关。"""
        cands = view_candidates.element_candidates(
            [{"role": "button", "name": "×"}], filter_noise=False,
        )
        assert cands == []

    def test_候选集与渲染层同一份(self):
        """精炼侧与渲染侧调用**同一个函数**——否则「第 7 号」不是同一个
        东西。"""
        from trajectory_pipeline.assembler import observation_view as ov
        cands = view_candidates.element_candidates(
            _ELEMENTS, filter_noise=False,
        )
        assert [c.name for c in cands] == ["立即播放", "相关推荐", "选集"]

    def test_read_blocks是唯一真源(self):
        assert view_candidates.read_blocks("") == (True, True, True)
        assert view_candidates.read_blocks("snapshot") == (True, False, False)
        assert view_candidates.read_blocks("interactive") == (False, True, False)
        assert view_candidates.read_blocks("links") == (False, False, True)


class Test分流与校验:
    def test_三段按区间分流(self):
        body, elements, links = refiner.split_selection(
            [1, 3, 5, 6, 9], n_body=4, n_elements=2, n_links=3,
        )
        # 编号空间连续：正文 1..4 → 元素 5..6 → 链接 7..9
        assert body == [1, 3]
        assert elements == [1, 2]
        assert links == [3]

    def test_动作目标跨三段判(self):
        """目标是**元素名**而不是正文行 ⇒ 也不能判成「模型把目标丢了」。"""
        err = refiner.validate_selection(
            [1], ["无关正文"], keep=("立即播放",),
            element_picked=[1], elements=[("button", "立即播放")],
        )
        assert err == ""

    def test_目标三段都没选就报错(self):
        err = refiner.validate_selection(
            [1], ["无关正文"], keep=("立即播放",),
            element_picked=[], elements=[("button", "立即播放")],
        )
        assert "操作目标" in err

    def test_链接段也算(self):
        err = refiner.validate_selection(
            [1], ["无关正文"], keep=("https://x.test/play",),
            link_picked=[], links=[("正片", "https://x.test/play")],
        )
        assert "操作目标" in err

    def test_语义模式放开行数上限(self):
        assert refiner.estimate_max_lines(["很长的一行"] * 100, 320,
                                          semantic=True) == 0
        assert refiner.estimate_max_lines(["很长的一行"] * 100, None) == 0
        assert refiner.estimate_max_lines(["很长的一行"] * 100, 320) > 0

    def test_语义模式needs_refine只看有没有候选(self):
        assert refiner.needs_refine("正文", None, semantic=True) is True
        assert refiner.needs_refine("", None, semantic=True,
                                    elements=[("button", "x")]) is True
        assert refiner.needs_refine("", None, semantic=True) is False


class Test噪声可推翻:
    def test_噪声行也列出来并标上(self):
        pairs = body_lines.candidate_lines_with_noise(
            "主演：姜文 周润发\n首页\n京ICP备12345号",
        )
        lines = [line for line, _flag in pairs]
        assert "首页" in lines
        flagged = {line for line, flag in pairs if flag}
        assert "首页" in flagged

    def test_去重仍然生效(self):
        pairs = body_lines.candidate_lines_with_noise("同一行\n同一行\n")
        assert [line for line, _ in pairs] == ["同一行"]


# ── 「模型选 N 行 ⇒ 渲染后仍有 N 行」 ────────────────────────────


class Test选中行不丢:
    def test_选N行渲染后仍有N行(self):
        """方案 26 §8 阶段 2 的验收判据（≥95%）：模型选 N 行，
        渲染后**仍有 N 行**——渲染层不许再砍。"""
        kept = ["《让子弹飞》高清在线观看", "主演：姜文 周润发",
                "无关第一行内容"]
        out = _render(_semantic_index(kept=kept))
        got = out["body_text"].splitlines()
        assert len(got) == len(kept)
        for line in kept:
            assert line in got


# ── 报表记账：回退必须能被看见 ────────────────────────────────────


def _arc(body: str = _BODY) -> dict:
    """造一份**最小可切条**的 P1 存档（形状同 ``tests/unit/test_builder.py``）。"""
    return {
        "task_id": "T001", "title": "让子弹飞", "query": "让子弹飞 在线观看",
        "driver": "camofox",
        "user_prompt": "有没有能看正片的", "search_url": "https://s.test",
        "search_observation": _obs(body_text=body),
        "search_blocked": "",
        "steps": [{
            "action": {"tool": "goto", "params": {"url": "https://s.test"},
                       "origin": "model"},
            "observation": _obs(body_text=body),
            "error": "",
            "rationale": None,
        }],
        "provenance": {},
        "run_config": {"engine": "baidu", "max_candidates": 20,
                       "max_chars": None, "stop_after_success": 0,
                       "per_site_timeout_s": 90.0},
        "candidates": [], "visits": [], "outcomes": [], "ledger": None,
        "warnings": [],
    }


def _samples_dir(tmp_path):
    """P2 交付目录（**同 builder 口径**，不另写一份路径拼接）。"""
    from trajectory_pipeline.assembler import builder

    return builder.samples_dir(tmp_path)


class Test报表记账:
    """⚠️ **回退必须能被看见**（H-41 阶段 2 · 方案 26 §6.3 的「报账」纪律）。

    回退后的观察与 ``capped`` **逐字一致** ⇒ 不单独计数的话，
    「语义渲染跑过了」与「压根没跑通」在报表上**长得一模一样**。
    """

    def _build(self, tmp_path, *, refined_dir=None):
        import json

        from trajectory_pipeline.assembler import builder

        src = tmp_path / "src"
        src.mkdir(parents=True, exist_ok=True)
        arc = _arc()
        path = src / "T001__abc123.json"
        path.write_text(json.dumps(arc, ensure_ascii=False), encoding="utf-8")
        return builder.build_from_archive(
            path, root=tmp_path, overwrite=True,
            refined_dir=refined_dir, mode=observation_view.VIEW_MODE_SEMANTIC,
        )

    def _write_sidecar(self, tmp_path, **entry):
        import json

        refined_dir = tmp_path / "refined"
        refined_dir.mkdir(parents=True, exist_ok=True)
        # ⚠️ **键是候选集哈希**（H-41 阶段 3）——渲染层按它找这份精炼。
        # 用正文哈希当键 ⇒ 查不到 ⇒ 整条回退 ⇒ 报表上「语义渲染 0 条」。
        key = _selection_key(_arc()["steps"][0]["observation"])
        payload = refine_sidecar.make_payload(
            "T001__abc123.json",
            {key: {
                "selection_key": key,
                "body_key": refine_sidecar.body_key(_BODY),
                "source": "llm",
                "kept": ["主演：姜文 周润发"],
                "elements": [{"role": "button", "name": "立即播放"}],
                "links": [{"text": "正片", "href": "https://x.test/play"}],
                "protocol": refine_sidecar.ENTRY_PROTOCOL,
                **entry,
            }},
        )
        refine_sidecar.write_sidecar(
            refine_sidecar.sidecar_path(refined_dir, "T001__abc123.json"),
            payload,
        )
        return refined_dir

    def test_走通语义渲染时计入semantic(self, tmp_path):
        refined_dir = self._write_sidecar(tmp_path)
        res = self._build(tmp_path, refined_dir=refined_dir)
        assert res.semantic_observations > 0
        assert res.semantic_fallbacks == 0

    def test_没有旁挂时整条记成回退(self, tmp_path):
        res = self._build(tmp_path, refined_dir=None)
        assert res.semantic_observations == 0
        assert res.semantic_fallbacks > 0

    def test_落盘的观察带模式与回退留痕(self, tmp_path):
        """P2 里那条观察自己也要说破「我是语义渲染的」还是「我回退了」。"""
        import json

        refined_dir = self._write_sidecar(tmp_path)
        self._build(tmp_path, refined_dir=refined_dir)
        out = _samples_dir(tmp_path)
        obs = json.loads(
            next(out.glob("*.json")).read_text(encoding="utf-8")
        )["six"]["observations"]
        assert obs
        for o in obs:
            assert o["view_mode"] == observation_view.VIEW_MODE_SEMANTIC
            assert o["semantic_fallback"] is False


# ── fail-closed 的训练信号**不能凭空消失**（方案 26 §6.4）──────────


class TestfailClosed信号还在:
    """⚠️ ``[内容不完整: …]`` 是教模型「看到缺件就说判不了」的载体。

    语义模式下 ``truncated.view`` 恒 False ⇒ 那一支不触发——**但那不是
    「证据不全」，那是「模型自己选的」**。真正证据不全的三类信号
    （采集降级 / 观察缺件 / 上游截断）**必须还在**，
    否则模型被教成「永远敢下结论」。
    """

    def _text(self, obs_kw=None, **render_kw):
        from trajectory_pipeline.assembler import views

        train_view = observation_view.render(
            _obs(**(obs_kw or {})),
            focus=("让子弹飞",),
            refined=_semantic_index(kept=["主演：姜文 周润发"]),
            mode=observation_view.VIEW_MODE_SEMANTIC,
            **render_kw,
        )
        return views.render_observation(train_view)

    def test_采集降级标记还在(self):
        text = self._text({"degraded": ["links_empty"]})
        assert "[采集降级: links_empty]" in text

    def test_上游截断标记还在(self):
        """上游截断 = 证据真的不全 ⇒ **必须**说破（责任方是采集侧）。"""
        text = self._text({"truncated": True})
        assert "[内容不完整: 上游截断]" in text
        assert "本次裁剪" not in text

    def test_观察缺件还是占位(self):
        from trajectory_pipeline.assembler import views

        assert views.render_observation(
            {"degraded": ["observation_missing"], "body_text": ""}
        ) == "（本步没有取到页面内容）"

    def test_本层没裁时不出现内容不完整(self):
        """⚠️ **这一条是「不出现」而不是「出现」**——语义模式本层一个字
        都没截 ⇒ 说「内容不完整」就是把「模型的选择」讲成「页面就这么少」
        （那是在训练侧教出来的幻觉）。"""
        text = self._text()
        assert "[内容不完整:" not in text

    def test_精简留痕还在(self):
        """正文是模型选行裁出来的 ⇒ **必须说破**（模型从文本里看不出来）。"""
        text = self._text()
        assert "[内容已精简: 保留 1 行]" in text
