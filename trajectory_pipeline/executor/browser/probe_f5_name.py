"""P-1 · F5 探针（**离线段**）：采集层算的 ``(role, name)`` 与 playwright 算的是不是同一个东西。

为什么单列一支
--------------
D12 把动作的目标编码定为 ``role + name``，执行期落到
``page.get_by_role(role, name=name)``。但 role 与 name 是**采集层自己算的**——
采集面（``Driver.interactive``）不 import playwright 的定位器，它枚举 DOM 后自报
role 与 accessible name。若两边算法不一致，``get_by_role(role, name)`` 定位不到 →
动作被记成「元素不存在」→ 上层转 ``unresolved`` → **一批理由通顺的假负样本，零报错**。

这个症状与「元素真不存在」**完全同形**（F4：两种情形都是 ``TimeoutError``），
所以它是 D12 的生死门——[方案 §4.5.3 / §6 P-1](../../docs/设计方案/04-camoufox执行层替换方案.md)。

本探针只回答**离线能回答的那一半**
--------------------------------
在一个构造页面上，逐元素量三件事：

1. **朴素 JS 近似**（方案 §4.5.3 点名的那条链 ``aria-label || innerText || placeholder
   || title || alt``）算出来的 name，能不能被 ``get_by_role(role, name=…, exact=True)`` 定位到；
2. **加料 JS 近似**（补 ``aria-labelledby`` 引用、``aria-hidden`` 子树排除、
   ``input[type=submit]`` 的 ``value``、input 的 title/placeholder 优先级、
   块级/行内拼接规则、空白归一）能不能把落差补上——
   这一条决定「值不值得不引入 aria_snapshot」；
3. playwright 自己算的 ``(role, name)``——用 ``locator.aria_snapshot()`` 取，
   **与定位器同源**，作为权威参照。

逐元素分类：``ok`` / ``multi``（strict mode 会报错，§4.5.4，**不算命中**）/
``no_name``（朴素规则**给不出 name**——图标按钮、submit 输入框就是这样）/
``miss_js_name``（权威侧能定位 ⇒ 近似算错）/ ``miss_both``（连权威侧都定位不到 ⇒
结构性错误，最危险）/ ``error``（探针自身出错）。

⚠️ **离线段测不出命中率，别把这里的比例当 P-1 结论**
-------------------------------------------------------
本页用例是**故意挑的边界**（aria-labelledby、图标按钮、嵌套拼接、同名多元素…）。
真实站点上大多数可交互元素就是纯文本按钮，朴素近似在那类元素上必然命中，
所以这里算出的比例会**低于**真实页面。离线段的产出是**机制层的落差清单**——
它回答「哪几类规则 JS 补不上、补上要付什么代价」。

另一件离线段做不到的事：**没有「元素不存在」这个干扰项**。
本页每个元素都是 JS 亲自枚举出来的，它一定存在，所以 ``count == 0``
只能解释成「name/role 算错」。真实页面上这两者同形，必须靠第二段区分。

第二段（真实页面）
------------------
给 ``--url`` / ``--urls-file`` / ``--from-archive`` 就切到真实页面段：在真站点上
量 §6 P-1 的 **≥95% 命中率**，并把每个未命中分进**六个桶**——这是离线段做不到、
而恰恰决定「算错」与「不存在」同形问题的那一步：

| 桶 | 判据 | 是否计入分母 |
|---|---|---|
| ``ok`` | 朴素 name 唯一定位 | ✅ |
| ``ok_plus_only`` | 朴素不中、加料 name 唯一定位 | ✅ |
| ``ok_plus_css_only`` | 加料也不中、补 ``::before/::after`` 的 content 后唯一定位 | ✅（第四条臂，单列） |
| ``multi`` | 命中 ≥2（strict mode 不可执行） | ✅（且单列） |
| ``name_wrong_pw_ok`` | playwright 自己的 name 能定位 ⇒ **近似算错** | ✅ |
| ``hidden_from_a11y`` | 带 ``include_hidden=True`` 才命中 ⇒ 元素**没进可访问性树**（隐藏/零尺寸） | ❌ 单列 |
| ``no_accessible_name`` | 有 role 节点但**没有可访问名** ⇒ `role+name` 编码对它天然失效 | ❌ 单列（**编码适用边界**） |
| ``not_in_a11y_tree`` | ``aria_snapshot`` 空空如也（元素没进可访问性树） | ❌ 单列 |
| ``stale_element`` | CSS 路径不再唯一 / 指向别的元素 ⇒ 测量期间 DOM 变了 | ❌ 单列，**该页读数存疑** |
| ``unexplained`` | 以上都不是 | ❌ **但按最坏情况计入分母**——不敢排除 |

⚠️ **分母有三套，三套都打印**：``eligible``（确认可寻址）、
``unique``（``eligible`` 去掉真同名 ``multi``——同名不是算法能救的）、
``eligible + unexplained``（最坏情况）。**判据用 ``unique``**，但
``unexplained > 0`` 时直接拒绝出结论（有解释不了的元素，比例就不该被引用）。
只有一套分母时，排除项会悄悄抬高命中率（CLAUDE.md：贴标签前核前置事实）。

⚠️ **必须跟成分一起读**：实测 240 个枚举元素里 97 个（40%）**根本没有可访问名**，
其中 90 个不可见（隐藏导航链）。所以「命中率 96.7%」和「四成元素没有名字」
是同一个报告里的两句话，漏掉后一句会把编码边界读成算法缺陷。

**实测结论（2026-10-20，6 URL / 240 行，camoufox 0.5.8 + playwright 1.62.0）**

- 唯一名分母 91：朴素链 **87.9%**、加料 5 条规则 **87.9%**（多救 0–1 条）、
  加料 + 伪元素内容 **96.7%**、playwright 自己的 name **100%**；
- **加料规则在真实页面上几乎不增益**，落差全部是 CSS 生成内容
  （`::before{content:"\\e608"}` 的图标字形，文本遍历取不到）——
  离线那 23/23 是边界用例挑出来的，别外推；
- 真同名 15 个（6.3%）→ strict mode fail-closed 是正解，不是编码缺陷；
- `get_by_role` 默认 `name` 是 substring 口径，同一批元素能到 100%
  ——**判据必须用 `exact=True`**，否则这个门是空的；
- 每页一次 `aria_snapshot`：**57–165ms**（839–1511 节点），JS 枚举 24–30ms。

→ 结论：**name 由 playwright 算（路径 1），采集层不许自己重算**；
无名字元素与真同名元素交给路径 3 / fail-closed。详见方案 §1.6。

跑法::

    uv run python -m trajectory_pipeline.executor.browser.probe_f5_name            # 离线段
    uv run python -m trajectory_pipeline.executor.browser.probe_f5_name --utf8     # 中文不转义
    uv run python -m trajectory_pipeline.executor.browser.probe_f5_name --headful
    uv run python -m trajectory_pipeline.executor.browser.probe_f5_name --strict    # 结构性失败退 1

    # 真实页面段（会联网）
    uv run python -m trajectory_pipeline.executor.browser.probe_f5_name --from-archive trajectory_pipeline/output/pipeline --url-limit 5
    uv run python -m trajectory_pipeline.executor.browser.probe_f5_name --url https://… --limit 80

与 ``camoufox_probe.py`` 同惯例：**探针必须在包内**（产物目录被 .gitignore 挡掉，
放那儿等于换台机器就没了），只读，不写仓库任何目录，产物只打印到 stdout。

``--utf8`` 默认关：Windows 控制台是 GBK，中文按 ``\\uXXXX`` 转义打印最稳
（与 ``camoufox_probe._dump`` 同口径）。要读 name 原文时开 ``--utf8``。

探针自身的两个坑（踩过，记下来）
--------------------------------
1. **``aria_snapshot`` 首行可能带行内文本**，形如 ``- button "搜索结果": 忽略我``。
   若按「整行匹配 role+name」解析，会把这行判成解析失败 → 权威侧 ``(None, None)``
   → 一堆用例被误分类成 ``miss_both``。**只能取节点头，忽略冒号之后的尾巴。**
2. **``<img alt>`` 与相邻文本拼接时没有空格**（实测 ``图标`` + ``附加文本`` →
   ``图标附加文本``），而**块级子元素之间有空格**（``下载``/``客户端`` →
   ``下载 客户端``）、行内子元素之间没有。这三条一起说明：拼接规则取决于**布局**，
   纯 JS 想复刻就得自己实现块级/行内判定——**这就是「JS 近似」路线的真实成本**。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata as md
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from camoufox.async_api import AsyncCamoufox

#: §6 P-1 的判据线。**这条线是给真实页面段用的**；离线段只做提示，不作结论。
HIT_RATE_THRESHOLD = 0.95

#: 1×1 透明 GIF。只用在内联 ``<img>`` 上（不是在 URL 里导航），
#: 所以不踩 ``data:`` URL 的 fragment 截断那个坑。
_PIXEL = (
    "data:image/gif;base64,"
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

#: 构造页。**每个元素都带 ``data-f5``**——枚举、定位、快照三条路都靠它对齐，
#: 否则「哪个 JS 元素对应快照里哪个节点」会变成猜谜。
#:
#: 用例按**accessible name 的规则**分组，不按站点形态分组：
#: 取值优先级（aria-labelledby / aria-label / value / 内容 / title / placeholder）、
#: 内容拼接（行内 vs 块级 vs img）、子树排除（aria-hidden）、role 推断（tag → role）、
#: 同名多元素（strict mode）。方案 §4.5.3 点名的四类规则全在其中。
#:
#: ⚠️ **除 ``dup-a``/``dup-b`` 这一对，所有用例的 name 必须互不相同**——
#: 撞名会让 ``get_by_role`` 的 count 变成 2+，把「撞名」误读成「strict mode 多命中」
#: （实测踩过：``nested-inline`` 曾与 ``text`` 同得「立即播放」，两个用例一起被判 multi）。
F5_HTML = f"""<html><head><title>f5</title></head><body>
<div id="f5-root">

  <!-- ── 取值优先级 ───────────────────────────────────── -->
  <button data-f5="text">立即播放</button>
  <button data-f5="aria-label-wins" aria-label="关闭广告">×</button>
  <span id="f5-lbl1">搜索结果</span>
  <button data-f5="labelledby" aria-labelledby="f5-lbl1">忽略我</button>
  <span id="f5-lbl2">第一段</span><span id="f5-lbl3">第二段</span>
  <button data-f5="labelledby-multi" aria-labelledby="f5-lbl2 f5-lbl3">忽略</button>
  <button data-f5="empty-aria-label" aria-label="">用文本</button>
  <input data-f5="value-submit" type="submit" value="提交">
  <input data-f5="placeholder" type="text" placeholder="请输入搜索词">
  <input data-f5="ph-beats-title" type="text" placeholder="占位" title="标题">
  <button data-f5="title-only" title="提示文本"></button>
  <button data-f5="text-beats-title" title="提示文本">可见文本</button>
  <button data-f5="img-alt"><img src="{_PIXEL}" alt="播放"></button>
  <button data-f5="img-plus-text"><img src="{_PIXEL}" alt="图标">附加文本</button>

  <!-- ── 内容拼接 ─────────────────────────────────────── -->
  <button data-f5="nested-inline"><span>立即</span><span>查看</span></button>
  <button data-f5="nested-block"><div>下载</div><div>客户端</div></button>
  <button data-f5="whitespace">  多余    空白  </button>

  <!-- ── 子树排除 ─────────────────────────────────────── -->
  <button data-f5="aria-hidden-child"><span aria-hidden="true">×</span>删除条目</button>

  <!-- ── role 推断 ────────────────────────────────────── -->
  <div data-f5="role-button-div" role="button" tabindex="0">扮演按钮</div>
  <a data-f5="link-text" href="#a">影片 详情</a>
  <a data-f5="link-img" href="#b"><img src="{_PIXEL}" alt="首页"></a>
  <input data-f5="searchbox" type="search" placeholder="搜一下">
  <textarea data-f5="textarea-ph" placeholder="说点什么"></textarea>
  <input data-f5="checkbox" type="checkbox" aria-label="记住我">
  <select data-f5="select" aria-label="排序方式"><option>默认</option></select>

  <!-- ── 同名多元素：strict mode 的实地 ───────────────── -->
  <button data-f5="dup-a">关闭</button>
  <button data-f5="dup-b">关闭</button>

</div>
</body></html>"""

#: 真实页面段的可交互元素选择器。**同一串同时传给 JS 与 ``page.locator``**——
#: 两侧各写一份迟早漂，而漂了之后「第 i 个元素」指向不同元素，
#: 读数看起来完全正常（这正是 index 型探针最阴的失效方式）。
INTERACTIVE_SEL = (
    "button, a[href], input, textarea, select,"
    " [role=button], [role=link], [role=textbox], [role=searchbox],"
    " [role=combobox], [role=checkbox], [role=radio], [role=tab]"
)

#: 采集层对同一页面的**两次 JS 近似**。两个都返回，是为了回答
#: 「落差是 JS 根本补不上，还是只差几条已知规则」——这决定 §4.5.3 走哪条路。
#:
#: ``naive`` = 方案 §4.5.3 点名的那条链，一字不改（它的失败方式本身就是证据）。
#: ``plus`` = 只补**机制上确定**的规则：aria-labelledby 引用、aria-hidden 排除、
#: submit 的 value、input 的 title 先于 placeholder、块级/行内拼接、空白归一。
#:
#: ⚠️ ``plus`` 里那张 ``BLOCK`` 表就是代价所在：它复刻的是 ``innerText`` 的
#: **布局相关**行为。谁要维护这份 JS，就得跟着浏览器行为一起漂。
#:
#: 入参 ``{sel, filterUnknown}``：离线段传 ``[data-f5]`` + ``False``（全部保留，
#: 词表外 role 也要看见，好发现词表缺类）；真实页面段传 :data:`INTERACTIVE_SEL`
#: + ``True``（词表外不枚举，与采集层口径一致，并把被丢掉的东西**计数**报出来）。
JS_ENUMERATE = """({sel, filterUnknown}) => {
  const norm = (s) => String(s == null ? '' : s).replace(/\\s+/g, ' ').trim();

  // 朴素近似：方案 §4.5.3 点名的那条链，一字不改。
  function naiveName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return norm(aria);
    const it = norm(el.innerText);
    if (it) return it;
    const ph = el.getAttribute('placeholder');
    if (ph) return norm(ph);
    const t = el.getAttribute('title');
    if (t) return norm(t);
    const alt = el.getAttribute('alt');
    if (alt) return norm(alt);
    return '';
  }

  // 块级元素之间要补空格，行内元素之间不补——实测结论，见模块 docstring 坑 2。
  const BLOCK = new Set(['ADDRESS','ARTICLE','ASIDE','BLOCKQUOTE','BR','DD','DIV','DL','DT',
    'FIELDSET','FIGCAPTION','FIGURE','FOOTER','FORM','H1','H2','H3','H4','H5','H6','HEADER',
    'HR','LI','MAIN','NAV','OL','P','PRE','SECTION','TABLE','TBODY','TD','TFOOT','TH','THEAD',
    'TR','UL']);

  // 加料近似的内容取值：排除 aria-hidden 子树；img 用 alt 且**不加空格**（坑 2）。
  //
  // ⚠️ 子元素的内容取 ``child.innerText`` 而不是递归读文本节点——``innerText``
  // 是**布局感知**的，``display:none`` 子树在那里天然是空串。真实页面上
  // 45% 的可交互元素是隐藏的（方案 §1.6），手写递归读 nodeValue 会把隐藏文本
  // 也算进名字里，于是「加料」比「朴素」还差（实测踩过：3 页 82 个元素里
  // 加料一个都没多救出来）。
  function textOf(el) {
    let out = '';
    for (const n of el.childNodes) {
      if (n.nodeType === 3) { out += n.nodeValue; continue; }
      if (n.nodeType !== 1) continue;
      if (n.getAttribute('aria-hidden') === 'true') continue;
      if (n.tagName === 'IMG') { out += (n.getAttribute('alt') || ''); continue; }
      const sub = n.innerText || '';
      if (!sub) continue;
      out += BLOCK.has(n.tagName) ? ' ' + sub + ' ' : sub;
    }
    return norm(out);
  }
  function labelledbyText(el) {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
    const parts = [];
    for (const id of ids) {
      const t = document.getElementById(id);
      if (t) { const s = textOf(t); if (s) parts.push(s); }
    }
    return norm(parts.join(' '));
  }
  function plusNameWith(el, textFn) {
    const lb = labelledbyText(el);
    if (lb) return lb;
    const aria = el.getAttribute('aria-label');
    if (aria) return norm(aria);
    const tag = el.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      if (ty === 'submit' || ty === 'button' || ty === 'reset') {
        const v = norm(el.value);
        if (v) return v;
      }
      // HTML-AAM 的顺序：title 先于 placeholder（实测见 ph-beats-title）
      const t = el.getAttribute('title');
      if (t) return norm(t);
      const ph = el.getAttribute('placeholder');
      if (ph) return norm(ph);
      return '';
    }
    const txt = textFn(el);
    if (txt) return txt;
    const t = el.getAttribute('title');
    if (t) return norm(t);
    return '';
  }
  function plusName(el) { return plusNameWith(el, textOf); }

  // ── 第四条臂：plus + CSS 生成内容（::before/::after）───────────────────
  // 存在理由：实测真实页面 105 个唯一名元素里有 8 个（7.6%）的 playwright
  // 名字带私有区字形（``网页``），文本遍历取不到——那是图标字体的
  // ``::before { content: "&#xe608;" }``（字体图标，写作 CSS 转义 \\e608）。
  // **路径 2 的全部损失几乎都在这 8 个上**，
  // 所以「补伪元素内容能不能救」必须有一档读数，而不是靠猜。
  //
  // 代价要看清：CSSOM 的 ``content`` 是**未解转义**的字符串（反斜杠 + hex），
  // 而且它可以是 ``url()`` / ``counter()`` / ``attr()``。这里只解 hex 转义；
  // 非字符串字面量**原样保留**（不猜），所以这条臂的上界是「只差这 8 条」。
  function cssUnescape(s) {
    return s.replace(/\\\\(?:([0-9a-fA-F]{1,6})\\s?|([\\s\\S]))/g, (m, hex, ch) =>
      hex ? String.fromCodePoint(parseInt(hex, 16)) : ch);
  }
  function pseudoContent(n, which) {
    let c;
    try { c = getComputedStyle(n, which).content; } catch (e) { return ''; }
    if (!c || c === 'none' || c === 'normal') return '';
    const m = c.match(/^"([\\s\\S]*)"$/);
    const raw = m ? cssUnescape(m[1]) : c;
    return norm(raw);
  }
  // 与 textOf 同构，只是每个元素子节点前后各补伪元素内容。
  function textOfCss(el) {
    let out = '';
    for (const n of el.childNodes) {
      if (n.nodeType === 3) { out += n.nodeValue; continue; }
      if (n.nodeType !== 1) continue;
      if (n.getAttribute('aria-hidden') === 'true') continue;
      if (n.tagName === 'IMG') { out += (n.getAttribute('alt') || ''); continue; }
      const piece = pseudoContent(n, '::before') + (n.innerText || '')
                  + pseudoContent(n, '::after');
      if (!piece) continue;
      out += BLOCK.has(n.tagName) ? ' ' + piece + ' ' : piece;
    }
    return norm(out);
  }
  function plusCssName(el) {
    // aria-label / aria-labelledby **压过**内容，此时伪元素不参与名字
    if (el.getAttribute('aria-labelledby') || el.getAttribute('aria-label')) {
      return plusNameWith(el, textOf);
    }
    return norm(pseudoContent(el, '::before')
                + plusNameWith(el, textOfCss)
                + pseudoContent(el, '::after'));
  }

  // 方案 §4.5.2 的短词表；**词表外不枚举**。
  // ⚠️ ``<input>`` 只认文本型与按钮/勾选型：真实页面上一大半 ``<input>`` 是
  // ``type=hidden``，把它们当 textbox 枚举会让分母灌水（实测见方案 §1.6）。
  const TYPE_ROLE = {
    text: 'textbox', search: 'searchbox', email: 'textbox', tel: 'textbox',
    url: 'textbox', password: 'textbox', number: 'textbox',
    submit: 'button', button: 'button', reset: 'button',
    checkbox: 'checkbox', radio: 'radio',
  };
  function roleOf(el) {
    const r = (el.getAttribute('role') || '').toLowerCase().split(/\\s+/)[0];
    if (r) return r;
    const tag = el.tagName.toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a') return el.hasAttribute('href') ? 'link' : null;
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'input') {
      const ty = (el.getAttribute('type') || 'text').toLowerCase();
      return TYPE_ROLE[ty] || null;
    }
    return null;
  }
  function skipKey(el) {
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      return 'input[' + ((el.getAttribute('type') || 'text').toLowerCase()) + ']';
    }
    return tag;
  }

  // ⚠️ **不要用 index 定位元素**（`page.locator(sel).nth(i)`）：真实页面在测量
  // 期间会重渲染，index 会指向**另一个 tag 相同的元素**，而 tag 校验挡不住
  // ——实测 iqiyi 5 页里 4 页 `dom_changed=True`，一次性产生 74 个假的
  // 「unexplained」（快照取到的是隔壁元素的名字）。
  // 退化路径也要留：路径算不出来（深处 / 非元素父节点）就返回 ''，
  // 上层据此判 ``stale`` 而不是硬算。
  function cssPath(el) {
    const parts = [];
    let n = el;
    while (n && n.nodeType === 1 && parts.length < 12) {
      let sel = n.tagName.toLowerCase();
      const parent = n.parentElement;
      if (!parent) { parts.unshift(sel); break; }
      const idx = Array.prototype.indexOf.call(parent.children, n) + 1;
      sel += ':nth-child(' + idx + ')';
      parts.unshift(sel);
      n = parent;
    }
    return parts.join(' > ');
  }

  const all = document.querySelectorAll(sel);
  const items = [];
  const skipped = {};
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    const role = roleOf(el);
    if (role === null && filterUnknown) {
      const key = skipKey(el);
      skipped[key] = (skipped[key] || 0) + 1;
      continue;
    }
    items.push({
      index: i,                                  // 仅供参考，**不要拿它定位**
      path: cssPath(el),                          // 稳定定位（见下）
      case: el.getAttribute('data-f5'),
      tag: el.tagName.toLowerCase(),
      role: role,
      roleAttr: el.getAttribute('role'),         // 原始属性：用来发现「作者写的 role 无效」
      naive: naiveName(el),
      plus: plusName(el),
      plusCss: plusCssName(el),
      text: norm(el.innerText).slice(0, 60),     // 只给人逐条读证据用
    });
  }
  return {items: items, skipped: skipped, matched: all.length};
}"""

#: ``aria_snapshot`` 首行的**节点头**：``- button "立即播放"``。
#:
#: ⚠️ **不许锚定行尾**——实测首行可能在冒号后带行内文本
#: （``- button "搜索结果": 忽略我``），锚定就把这类节点判成解析失败（坑 1）。
_SNAP_HEAD = re.compile(r'^-\s+(?P<role>[^\s"]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?')


def _dump(obj: Any, *, ascii_only: bool = True) -> str:
    """序列化——Windows 控制台是 GBK，非 ASCII 默认转义（与 camoufox_probe 同惯例）。

    ``ascii_only=False``（``--utf8``）时若当前 stdout 装不下这些字符，**自动退回转义**
    而不是抛 ``UnicodeEncodeError``——只能读不能崩。
    """
    text = json.dumps(obj, ensure_ascii=ascii_only, default=str)
    if not ascii_only:
        try:
            text.encode(sys.stdout.encoding or "utf-8")
        except (UnicodeEncodeError, LookupError):
            return json.dumps(obj, ensure_ascii=True, default=str)
    return text


def _parse_snapshot_head(snapshot: str) -> tuple[str | None, str | None]:
    """取快照首行的 ``(role, name)``——即 playwright 对该元素自己算的那一对。

    首行若不是可访问性树节点（空快照 / ``- text:`` 之类），返回 ``(None, None)``：
    那本身就是结论（该元素没进可访问性树），不能当成「name 为空」。
    """
    for line in (snapshot or "").splitlines():
        if not line.strip():
            continue
        m = _SNAP_HEAD.match(line)
        if not m:
            return None, None
        name = m.group("name")
        if name is not None:
            name = name.replace('\\"', '"').replace("\\\\", "\\")
        return m.group("role"), name
    return None, None


async def _count(
    page: Any,
    role: str | None,
    name: str | None,
    exact: bool | None,
    *,
    include_hidden: bool = False,
) -> Any:
    """``get_by_role(...).count()``——返回整数，异常转成字符串（异常本身就是读数）。

    ``include_hidden=True`` 是**诊断用**：默认 ``get_by_role`` 不匹配隐藏元素，
    所以「名字算对了但元素是 hidden」会表现成 ``count == 0``——
    与「名字算错」同形。带上这个开关才分得开。
    """
    if not role or not name:
        return None
    kwargs: dict[str, Any] = {"name": name}
    if exact is not None:
        kwargs["exact"] = exact
    if include_hidden:
        kwargs["include_hidden"] = True
    try:
        return await page.get_by_role(role, **kwargs).count()
    except Exception as exc:                      # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _classify(row: dict[str, Any]) -> str:
    """四态分类。**这是探针的核心判决**，别拿 ``count`` 直接当真值用。

    - ``ok``          朴素近似能唯一定位 → 该元素不需要 aria_snapshot
    - ``multi``       命中的不止一个 → strict mode 会报错（§4.5.4），**不算命中**
    - ``no_name``     朴素近似**给不出 name**（图标按钮 / submit）→ 不在分母里就是自欺
    - ``miss_js_name`` 朴素近似定位不到、但权威侧能 → 近似算错（可由加料规则补）
    - ``miss_both``   连权威侧都定位不到 → 结构性错误（role 推断 / 快照解析 /
                      元素没进可访问性树），**最危险，必看**
    - ``error``       探针自身出错（读数不是整数）
    """
    naive = row.get("js_naive_exact")
    if row.get("js_naive") == "":
        return "no_name"
    if not isinstance(naive, int):
        return "error"
    if naive == 1:
        return "ok"
    if naive > 1:
        return "multi"
    for key in ("pw_exact", "pw_role_js_name_exact"):
        value = row.get(key)
        if isinstance(value, int) and value >= 1:
            return "miss_js_name"
    return "miss_both"


async def _measure(
    page: Any, items: list[dict[str, Any]], *, full_snapshot: str = ""
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        case = item["case"]
        js_role = item["role"]
        row: dict[str, Any] = {
            "case": case,
            "tag": item["tag"],
            "js_role": js_role,
            "js_naive": item["naive"],
            "js_plus": item["plus"],
        }

        # ── 权威侧：playwright 自己算的 (role, name) ──────────────
        try:
            snapshot = await page.locator(f'[data-f5="{case}"]').aria_snapshot()
        except Exception as exc:                  # noqa: BLE001
            snapshot = ""
            row["snapshot_error"] = f"{type(exc).__name__}: {exc}"
        pw_role, pw_name = _parse_snapshot_head(snapshot)
        row.update(pw_role=pw_role, pw_name=pw_name, pw_snapshot=snapshot)
        row["pw_exact"] = await _count(page, pw_role, pw_name, True)
        if full_snapshot and pw_name:
            # 自洽校验：单元素快照解析出的 name 必须也能在全页快照里找到。
            # 找不到说明 _parse_snapshot_head 解析错了——权威列不可信，结论作废。
            row["pw_name_in_full_snapshot"] = pw_name in full_snapshot

        if js_role is None:
            # 词表外 role：采集层**不枚举**它，所以它不进命中率分母。
            # 但它仍留在明细里——真实页面上「词表少了一类」正是靠这些行发现的。
            row["status"] = "role_out_of_vocab"
            rows.append(row)
            continue

        # ── 采集层侧：用**它自己的 role** 去定位（这才是执行期会发生的事）──
        row["role_only"] = (
            await page.get_by_role(js_role).count() if js_role else None
        )
        row["js_naive_exact"] = await _count(page, js_role, item["naive"], True)
        row["js_naive_loose"] = await _count(page, js_role, item["naive"], False)
        if item["naive"] == "":
            # 空 name 是**真实会发生**的产物（图标按钮 / 图片链接 / submit）：
            # 朴素链取的是元素**自己的** alt，取不到子 <img> 的 alt。
            # 这里量「执行期拿着空 name 去定位」的结局——不量就是在猜。
            try:
                row["empty_name_count"] = await page.get_by_role(
                    js_role, name="", exact=True
                ).count()
            except Exception as exc:              # noqa: BLE001
                row["empty_name_count"] = f"{type(exc).__name__}: {exc}"
        row["js_plus_exact"] = await _count(page, js_role, item["plus"], True)
        row["js_plus_css_exact"] = await _count(
            page, js_role, item.get("plusCss", ""), True
        )
        if pw_role and pw_role != js_role:
            # 隔离变量：role 换成权威值、name 仍用朴素值 → 判断是 role 错还是 name 错
            row["pw_role_js_name_exact"] = await _count(page, pw_role, item["naive"], True)

        row["status"] = _classify(row)
        rows.append(row)
    return rows


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [r for r in rows if r["status"] != "role_out_of_vocab"]
    total = len(measured)
    # 与真实页面段同一条纪律：**真同名元素不进「算法可行不可行」的分母**。
    # strict mode 会 fail-closed（§4.5.4），那是设计行为，不是 name 算错；
    # 把它算进分母只会让离线读数看起来「差一点」，而缺的那一点根本不是算法能补的。
    multi = [r for r in measured if r["status"] == "multi"]
    denom = total - len(multi)

    def rate(key: str) -> dict[str, Any]:
        hits = sum(1 for r in measured if isinstance(r.get(key), int) and r[key] == 1)
        return {
            "hit": hits,
            "total": total,
            "denom_unique": denom,
            "rate": round(hits / total, 4) if total else None,
            "rate_unique": round(hits / denom, 4) if denom else None,
        }

    status: dict[str, list[str]] = {}
    for r in rows:
        status.setdefault(r["status"], []).append(r["case"])

    # 「能定位」= 命中 ≥1（含多命中）。多命中的执行期结局是 strict mode 报错，
    # 所以两个口径都要给——只报前一个会让人以为多命中是可执行的成功。
    any_hit = sum(
        1
        for r in measured
        if isinstance(r.get("js_naive_exact"), int) and r["js_naive_exact"] >= 1
    )
    role_only = sorted(
        r["role_only"] for r in measured if isinstance(r.get("role_only"), int)
    )
    median_role_only = role_only[len(role_only) // 2] if role_only else None
    return {
        "cases_total": len(rows),
        "cases_measured": total,
        "by_status": {k: sorted(v) for k, v in sorted(status.items())},
        "naive_exact_unique_hit": rate("js_naive_exact"),
        "naive_any_hit_incl_multi": {
            "hit": any_hit,
            "total": total,
            "rate": round(any_hit / total, 4) if total else None,
        },
        "plus_exact_unique_hit": rate("js_plus_exact"),
        "plus_css_exact_unique_hit": rate("js_plus_css_exact"),
        "pw_authoritative_exact_unique_hit": rate("pw_exact"),
        "naive_loose_unique_hit": rate("js_naive_loose"),
        # §4.5.3 路径 3 的候选池规模：不带 name 时同 role 有多少个候选。
        "role_only_candidates_median": median_role_only,
    }


def _verdict(summary: dict[str, Any]) -> dict[str, Any]:
    """把读数翻成 §4.5.3 的三条路——**离线段只给提示，不给 P-1 结论**。"""
    threshold = HIT_RATE_THRESHOLD
    # 分母去掉 ``multi``（真同名）——理由见 :func:`_summarize`。
    naive = summary["naive_exact_unique_hit"]["rate_unique"] or 0.0
    plus = summary["plus_exact_unique_hit"]["rate_unique"] or 0.0
    pw = summary["pw_authoritative_exact_unique_hit"]["rate_unique"] or 0.0
    by_status = summary["by_status"]
    structural = by_status.get("miss_both", [])

    if structural:
        reading = (
            "结构性失败：有元素连 playwright 自己算的 (role, name) 都定位不到。"
            "先查 role 推断与快照解析，别急着换近似算法。"
        )
        path = "路径 0（先修探针/证据）"
    elif plus >= threshold and pw >= threshold:
        # ⚠️ 这里**不能再写「路径 2 可行」**：真实页面段已经跑完并否决了它
        # （§1.6：加料在真实页面上只多救 0–1 条，87.9% < 95%）。
        # 离线段只说明「规则是完备的」，不能说明「规则够用」——两句话差着一次实测。
        reading = (
            "加料 JS 近似在**离线边界用例**上与 playwright 等价（规则完备），"
            "但真实页面上命中率只有 87.9%（§1.6 F5-h）→ **路径 2 不达标**；"
            "最终口径是路径 1（name 取 playwright 的 `aria_snapshot`）。"
        )
        path = "路径 1（离线只证规则完备；真实页面段已否掉路径 2）"
    elif pw >= threshold:
        reading = (
            "加料 JS 近似补不上，但 playwright 自己的 name 够用 → "
            "§4.5.3 路径 1（采集面改用 aria_snapshot，一页取一次并缓存）。"
        )
        path = "路径 1：aria_snapshot"
    else:
        reading = (
            "连 playwright 自己的 name 都不够用 → §4.5.3 路径 3："
            "退化到 get_by_role(role) + strict mode 列候选 + 代码按文本挑，"
            "仍不确定则 fail-closed 记 unresolved。**此时 D12 编码要重选，停。**"
        )
        path = "路径 3：D12 编码要重选"

    return {
        "reading": reading,
        "suggested_path": path,
        "offline_naive_rate": naive,
        "offline_plus_rate": plus,
        "offline_authoritative_rate": pw,
        "threshold_for_real_pages": threshold,
        "caveat": (
            "离线用例是故意挑的边界，比例会低于真实页面；"
            "§6 的 ≥95% 判据必须在真实页面上量（第二段：--url / --from-archive）。"
            "不要把这里的数字当 P-1 结论。"
        ),
        "no_name_cases": by_status.get("no_name", []),
        "multi_match_cases": by_status.get("multi", []),
        "structural_failures": structural,
        "role_out_of_vocab": by_status.get("role_out_of_vocab", []),
    }


async def _run(*, headless: bool) -> dict[str, Any]:
    versions: dict[str, Any] = {}
    for pkg in ("camoufox", "playwright"):
        try:
            versions[pkg] = md.version(pkg)
        except md.PackageNotFoundError:                       # pragma: no cover
            versions[pkg] = "not-installed"

    async with AsyncCamoufox(
        os="windows",
        headless=headless,
        locale="zh-CN",
        humanize=False,       # 离线页面，行为特征无意义；关掉省墙钟
    ) as browser:
        ctx = await browser.new_context()
        page = await ctx.new_page()
        # ⚠️ 必须 set_content，不能走 data: URL（fragment 会截断 payload，
        # 症状见 camoufox_probe.py 的同一条注释）
        await page.set_content(F5_HTML)

        # ── 路径 1 的代价（方案里目前只是「推断」）：JS 枚举 vs aria_snapshot ──
        # ⚠️ 本页只有二十几个节点，这里量到的是**常数开销下界**，
        # 不是真实页面的比例（真实页面节点数大两三个数量级）。真实页面的
        # 绝对值在第二段量（`--url`），别拿这个比例当结论。
        args = {"sel": "[data-f5]", "filterUnknown": False}
        t0 = time.perf_counter()
        data = await page.evaluate(JS_ENUMERATE, args)
        # 预热：首次 evaluate 含 isolated world 初始化，不能算进枚举的读数
        # （实测首次 24.8ms、第二次 6ms 量级——不预热会把初始化成本记成算法成本）。
        await page.evaluate("() => 1")

        # 取 3 次最小值：本页全是常数开销，抖动可达 2×（实测 6.3ms vs 11.1ms）。
        js_ms = (time.perf_counter() - t0) * 1000
        snapshot_ms = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            data = await page.evaluate(JS_ENUMERATE, args)
            js_ms = min(js_ms, (time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            full_snapshot = await page.locator("body").aria_snapshot()
            snapshot_ms = min(snapshot_ms, (time.perf_counter() - t0) * 1000)
        cost = {
            "dom_nodes": await page.evaluate("() => document.querySelectorAll('*').length"),
            "js_enumerate_ms": round(js_ms, 3),
            "aria_snapshot_ms": round(snapshot_ms, 3),
            "aria_snapshot_bytes": len(full_snapshot),
            "caveat": (
                "常数开销下界，不是真实页面比例；真实页面的绝对值须在第二段量。"
            ),
        }

        rows = await _measure(page, data["items"], full_snapshot=full_snapshot)
        await ctx.close()

    summary = _summarize(rows)
    # 权威列自洽：所有「解析出了 name」的行都必须能在全页快照里找到。
    checked = [r for r in rows if "pw_name_in_full_snapshot" in r]
    summary["authoritative_self_check"] = {
        "checked": len(checked),
        "failed": sorted(
            r["case"] for r in checked if not r["pw_name_in_full_snapshot"]
        ),
    }
    return {
        "probe": "P-1 · F5（离线段）",
        "versions": versions,
        "cost": cost,
        "summary": summary,
        "verdict": _verdict(summary),
        "rows": rows,
    }


# ══════════════════════════════════════════════════════════════════════
# 第二段：真实页面
# ══════════════════════════════════════════════════════════════════════

#: 单页最少要量到多少「可寻址元素」才允许下结论。5 个元素的 100% 不是证据。
MIN_SAMPLE = 30

#: 判据线（§6 P-1）。**只对真实页面段生效**。
_REAL_THRESHOLD = HIT_RATE_THRESHOLD


def _classify_real(row: dict[str, Any]) -> str:
    """先只看 count 的粗分类；分不出「算错 vs 不可达」就交给 :func:`_classify_real_miss`。"""
    if _is(row.get("plus_exact"), 1) and _is(row.get("naive_exact"), 1):
        return "ok"
    if _is(row.get("plus_exact"), 1):
        return "ok_plus_only"
    if _is(row.get("plus_css_exact"), 1):
        return "ok_plus_css_only"
    if (_is(row.get("plus_exact"), ">1")) or (_is(row.get("naive_exact"), ">1")):
        return "multi"
    return "miss"


def _is(value: Any, expect: int | str) -> bool:
    """``count`` 读数的安全比较——``_count`` 可能返回 ``None`` 或异常串。"""
    if not isinstance(value, int):
        return False
    if expect == ">1":
        return value > 1
    if expect == "any":
        return value >= 1
    return value == expect


def _classify_real_miss(row: dict[str, Any]) -> str:
    """未命中的六个桶。**顺序有讲究**：越确定的判据越靠前。

    1. 带 ``include_hidden`` 才命中 → 元素没进可访问性树（隐藏/零尺寸）。
       这条要排在「权威 name 能定位」**之前**吗？不用：能定位就是能定位，
       但它只在可见世界里成立，所以先看 hidden 更保守——见下方实现顺序。
    2. playwright 自己的 name 能定位 → 近似算错（路径 2 可救）。
    3. 权威 name 也命中多个 → 真同名。
    4. ``aria_snapshot`` 空 → 元素没进可访问性树。
    5. 全不是 → ``unexplained``（**按失败计入最坏分母**）。
    """
    pw_exact = row.get("pw_exact")
    naive_hidden = row.get("naive_include_hidden")
    pw_hidden = row.get("pw_include_hidden")

    if _is(pw_exact, 1):
        return "name_wrong_pw_ok"
    if _is(pw_exact, ">1"):
        return "multi"
    # 可见世界里两个 name 都定位不到，但算上隐藏元素就能定位 → 元素本身不可达
    if _is(naive_hidden, "any") or _is(pw_hidden, "any"):
        return "hidden_from_a11y"
    # 有 role 节点但**没有可访问名**：``role+name`` 这套编码对它天然失效
    # （playwright 也说不出该拿什么名字去定位它）。这不是「算错」，
    # 是编码的适用边界——只能由 §4.5.3 路径 3 兜底，或从交互列表里剔除。
    if row.get("pw_role") and not row.get("pw_name"):
        return "no_accessible_name"
    if not row.get("pw_role") and not row.get("pw_name"):
        return "not_in_a11y_tree"
    return "unexplained"


async def _settle_until_stable(
    page: Any,
    *,
    sel: str = INTERACTIVE_SEL,
    max_wait_ms: float = 12000.0,
    step_ms: float = 400.0,
    quiet_rounds: int = 2,
) -> int:
    """等元素总数连续 ``quiet_rounds`` 次不变再开量。

    为什么必须有这一步：真实页面的可交互元素是**异步长出来**的。
    实测 v.qq 在 ``--settle 1500`` 时还在 `78 → 107` 地变，iqiyi 更极端
    （`2 → 26`）——拿着半页读数去算命中率，等于给一个还没画完的页面判死。
    """
    last: int | None = None
    stable = 0
    waited = 0.0
    while waited < max_wait_ms:
        count = await page.evaluate(
            "(sel) => document.querySelectorAll(sel).length", sel
        )
        # ⚠️ **``0`` 不算稳定**：JS 渲染页 goto 刚回来时元素数就是 0，
        # 把 0,0 判成「稳定」会立刻开量空页（实测同一 URL 一次 40 个元素、
        # 一次 0 个——读数不可复现，而报告上看不出任何异常）。
        if count == last and count > 0:
            stable += 1
            if stable >= quiet_rounds:
                return count
        else:
            stable = 0
            last = count
        await page.wait_for_timeout(step_ms)
        waited += step_ms
    return last if last is not None else 0


def _mechanism(row: dict[str, Any]) -> str:
    """未命中的**机理**——比 ``status`` 更细一层，用来回答「补规则能不能救」。

    ``status`` 说「这是算错」，``mechanism`` 说「错在哪」：

    - ``css_generated_content``：名字里有 ``::before/::after`` 的字形（图标字体）。
      **任何 DOM 文本遍历都取不到，补规则也救不了** → 只能走 aria_snapshot。
    - ``invalid_role_attr``：作者写的 ``role`` 属性被 playwright 判无效而回落到原生 role
      （实测 ``role="pagedescription"`` 在 ``<a>`` 上被当成 ``link``）。
    - ``hidden_text_in_name``：pw 名字里含 CSS 隐藏子树的文本（与上面相反方向）。
    - ``unknown``：看不出原因，人读 evidence。
    """
    if row.get("pua_in_pw_name"):
        return "css_generated_content"
    if row.get("role_attr") and row.get("pw_role") and row["role_attr"] != row["pw_role"]:
        return "invalid_role_attr"
    if row.get("css_generated"):
        return "css_generated_content"
    return "unknown"


async def _measure_real_page(
    page: Any, *, limit: int, timeout_ms: float
) -> dict[str, Any]:
    """单页测量：枚举 → 逐元素三档定位 → 未命中逐条归桶 → 漂移检测。"""
    js_args = {"sel": INTERACTIVE_SEL, "filterUnknown": True}

    t0 = time.perf_counter()
    data = await page.evaluate(JS_ENUMERATE, js_args)
    js_ms = (time.perf_counter() - t0) * 1000
    # 取 2 次最小值（真实页面抖动小于离线页的常数开销，但仍值得取小）
    t0 = time.perf_counter()
    snap = await page.locator("body").aria_snapshot()
    snapshot_ms = (time.perf_counter() - t0) * 1000
    try:
        t0 = time.perf_counter()
        data = await page.evaluate(JS_ENUMERATE, js_args)
        js_ms = min(js_ms, (time.perf_counter() - t0) * 1000)
    except Exception:                             # noqa: BLE001
        pass

    items = data["items"]
    truncated = max(0, len(items) - limit)
    rows: list[dict[str, Any]] = []
    for item in items[:limit]:
        row: dict[str, Any] = {
            "index": item["index"],
            "path": item["path"],
            "tag": item["tag"],
            "role": item["role"],
            "role_attr": item.get("roleAttr"),
            "js_naive": item["naive"],
            "js_plus": item["plus"],
            "text": item["text"],
        }
        row["naive_exact"] = await _count(page, item["role"], item["naive"], True)
        row["naive_loose"] = await _count(page, item["role"], item["naive"], False)
        row["plus_exact"] = await _count(page, item["role"], item["plus"], True)
        row["plus_css_exact"] = await _count(
            page, item["role"], item.get("plusCss", ""), True
        )
        status = _classify_real(row)
        if status != "miss":
            row["status"] = status
            rows.append(row)
            continue

        # ── 未命中：这里才花代价去区分「算错」与「不存在」 ────────────
        row["status"] = "unexplained"             # 先给最坏，再往下翻案
        if not item.get("path"):
            # 路径算不出来 → 不猜，直接记 stale（按 index 定位会骗人）
            row["status"] = "stale_element"
            rows.append(row)
            continue
        element = page.locator(item["path"])
        try:
            found = await element.count()
            tag_now = await element.evaluate(
                "e => e.tagName.toLowerCase()", timeout=timeout_ms
            )
        except Exception as exc:                  # noqa: BLE001
            # 枚举到的元素现在找不到了：页面在测量期间变了
            row["probe_error"] = f"{type(exc).__name__}: {exc}"
            row["status"] = "stale_element"
            rows.append(row)
            continue
        if found != 1 or tag_now != item["tag"]:
            # 路径不再唯一 / 指向别的元素——该元素读数不可信
            row["found"] = found
            row["tag_now"] = tag_now
            row["status"] = "stale_element"
            rows.append(row)
            continue

        try:
            snapshot = await element.aria_snapshot(timeout=timeout_ms)
        except Exception as exc:                  # noqa: BLE001
            snapshot = ""
            row["snapshot_error"] = f"{type(exc).__name__}: {exc}"
        pw_role, pw_name = _parse_snapshot_head(snapshot)
        row.update(pw_role=pw_role, pw_name=pw_name, pw_snapshot=snapshot)
        try:
            row["visible"] = await element.is_visible(timeout=timeout_ms)
        except Exception as exc:                  # noqa: BLE001
            row["visible"] = f"{type(exc).__name__}: {exc}"

        row["pw_exact"] = await _count(page, pw_role, pw_name, True)
        if pw_name:
            # 图标字体：pw 名字里有私有区字符，而 innerText 里没有 ⇒ 来自 CSS
            # 生成内容（::before/::after），**任何 DOM 文本遍历都取不到**。
            # 实测百度知道：`link "网页"` ↔ `::before{content:""}`
            # （方案 §1.6）。这条是路径 2 在真实站点上的**硬否决**。
            row["pua_in_pw_name"] = any(
                0xE000 <= ord(ch) <= 0xF8FF for ch in pw_name
            )
            try:
                row["css_generated"] = await element.evaluate(
                    """e => {
                      const pick = (n, p) => {
                        const c = getComputedStyle(n, p).content;
                        return (c && c !== 'none' && c !== 'normal') ? c : '';
                      };
                      const out = [pick(e, '::before'), pick(e, '::after')];
                      for (const d of e.querySelectorAll('*')) {
                        out.push(pick(d, '::before'), pick(d, '::after'));
                      }
                      return out.filter(Boolean).slice(0, 4);
                    }""",
                    timeout=timeout_ms,
                )
            except Exception as exc:              # noqa: BLE001
                row["css_generated"] = f"{type(exc).__name__}: {exc}"
        # include_hidden 诊断：reveal 「名字对但元素不可达」
        row["naive_include_hidden"] = await _count(
            page, item["role"], item["naive"], True, include_hidden=True
        )
        row["pw_include_hidden"] = await _count(
            page, pw_role, pw_name, True, include_hidden=True
        )
        row["status"] = _classify_real_miss(row)
        # 机理只对「算法算错」和「解释不了」两类有意义。给 ``no_accessible_name``
        # 之类的桶也贴一个 ``unknown``，报告上会变成 ``unknown: 132``——
        # 读起来像 132 个未解之谜，其实全是已经解释清楚的排除项。
        if row["status"] in ("name_wrong_pw_ok", "unexplained"):
            row["mechanism"] = _mechanism(row)
        rows.append(row)

    # 漂移检测：测量期间元素总数变了，说明该页在读一个移动靶
    try:
        matched_after = await page.evaluate(
            "(sel) => document.querySelectorAll(sel).length", INTERACTIVE_SEL
        )
    except Exception:                             # noqa: BLE001
        matched_after = None

    return {
        "url": page.url,
        "title": await page.title(),
        "js_ms": round(js_ms, 3),
        "snapshot_ms": round(snapshot_ms, 3),
        "snapshot_bytes": len(snap),
        "dom_nodes": await page.evaluate(
            "() => document.querySelectorAll('*').length"
        ),
        "iframes": await page.evaluate("() => document.querySelectorAll('iframe').length"),
        "matched": data["matched"],
        "matched_after": matched_after,
        "dom_changed": (
            matched_after is not None and matched_after != data["matched"]
        ),
        "skipped": data["skipped"],
        "enumerated": len(items),
        "measured": min(len(items), limit),
        "truncated": truncated,
        "rows": rows,
    }


def _summarize_real(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合。**两套分母都出**——``eligible`` 与 ``eligible + unexplained``。"""
    rows = [r for p in pages for r in p["rows"] if "status" in r]
    total = len(rows)
    by_status: dict[str, list[str]] = {}
    for p in pages:
        for r in p["rows"]:
            key = f"{p['url']}#{r['index']}"
            by_status.setdefault(r.get("status", "?"), []).append(key)

    addressable = {
        "ok", "ok_plus_only", "ok_plus_css_only", "multi", "name_wrong_pw_ok",
    }
    eligible = sum(1 for r in rows if r["status"] in addressable)
    unexplained = sum(1 for r in rows if r["status"] == "unexplained")
    worst = eligible + unexplained

    # 🔴 **两个分母来自两个不同的问题，不能混报**：
    #   1) 「有多少元素的 (role,name) 是唯一可定位的」→ 分母 = eligible
    #      但 ``multi`` 是**真同名**，任何名字来源都定位不了它（strict mode 会
    #      fail-closed，这是 §4.5.4 的设计行为，不是编码失败）。
    #      所以判「编码可行不可行」要用 ``unique`` 分母。
    #   2) 「采集层能不能把页面上的元素都指出来」→ 分母 = 全部枚举数。
    #      实测 35% 的元素**没有可访问名**，这条只能靠路径 3 兜底。
    # 只报一个比例会把两个问题糊成一个数，读起来像是「算法差 5%」。
    dup = sum(1 for r in rows if r["status"] == "multi")
    unique = eligible - dup

    naive_hit = sum(1 for r in rows if r["status"] == "ok")
    plus_hit = sum(1 for r in rows if r["status"] in ("ok", "ok_plus_only"))
    plus_css_hit = sum(
        1 for r in rows if r["status"] in ("ok", "ok_plus_only", "ok_plus_css_only")
    )
    # 🔴 这条臂必须单列：`get_by_role` 的默认 ``name`` 是**substring + 忽略大小写**，
    # 而 ``exact=True`` 才是整串。执行期用哪个口径**会直接改变判据结果**
    # （实测来源：图标字形的 ``\\ue608网页`` 在 substring 口径下会被 ``网页`` 命中）。
    # 判据不钉死这个口径，P-1 的结论就不可比。
    loose_hit = sum(
        1 for r in rows
        if isinstance(r.get("naive_loose"), int) and r["naive_loose"] >= 1
    )
    # 权威臂：playwright 自己算的 name 能唯一定位的比例
    auth_hit = sum(
        1 for r in rows
        if r["status"] in ("ok", "ok_plus_only", "ok_plus_css_only", "name_wrong_pw_ok")
    )
    mechanisms: dict[str, int] = {}
    for r in rows:
        mech = r.get("mechanism")
        if mech:
            mechanisms[mech] = mechanisms.get(mech, 0) + 1

    def frac(n: int, d: int) -> Any:
        return round(n / d, 4) if d else None

    return {
        "pages": len(pages),
        "rows_total": total,
        "by_status": {k: len(v) for k, v in sorted(by_status.items())},
        "eligible": eligible,
        "unique_name_eligible": unique,
        "dup_name": dup,
        "no_accessible_name": len(by_status.get("no_accessible_name", [])),
        # 没名字的元素里有多少**是可见的**：可见的那些才是编码真缺口，
        # 不可见的那些是采集层本来就不该枚举的（枚举前必须先过可见性）。
        "no_accessible_name_visible": sum(
            1 for r in rows
            if r["status"] == "no_accessible_name" and r.get("visible") is True
        ),
        "unexplained": unexplained,
        "worst_case_denominator": worst,
        "naive_unique_hit": {
            "hit": naive_hit,
            "denom_eligible": frac(naive_hit, eligible),
            "denom_unique": frac(naive_hit, unique),
            "denom_worst": frac(naive_hit, worst),
        },
        "plus_unique_hit": {
            "hit": plus_hit,
            "denom_eligible": frac(plus_hit, eligible),
            "denom_unique": frac(plus_hit, unique),
            "denom_worst": frac(plus_hit, worst),
        },
        "plus_css_unique_hit": {
            "hit": plus_css_hit,
            "denom_eligible": frac(plus_css_hit, eligible),
            "denom_unique": frac(plus_css_hit, unique),
            "denom_worst": frac(plus_css_hit, worst),
            "note": "第四条臂：plus + ::before/::after 的 content（解 hex 转义）",
        },
        "naive_loose_any_hit": {
            "hit": loose_hit,
            "denom_worst": frac(loose_hit, worst),
            "note": "substring 口径（get_by_role 默认）；它不是精确命中，且更容易多命中",
        },
        "authoritative_unique_hit": {
            "hit": auth_hit,
            "denom_eligible": frac(auth_hit, eligible),
            "denom_unique": frac(auth_hit, unique),
            "denom_worst": frac(auth_hit, worst),
        },
        "mechanisms": mechanisms,
        "multi": by_status.get("multi", []),
        "unexplained_keys": by_status.get("unexplained", []),
        "min_sample_required": MIN_SAMPLE,
        "sample_sufficient": worst >= MIN_SAMPLE,
    }


def _verdict_real(summary: dict[str, Any]) -> dict[str, Any]:
    """§6 P-1 的判据：``get_by_role(role, name, exact=True)`` 唯一命中率 ≥ 95%。

    **分母钉死在「唯一名元素」上**（``denom_unique``），理由见 :func:`_summarize_real`：
    真同名元素（``multi``）不是算法能救的，它的执行期结局是 strict mode 报错。

    判据同时**必须带成分**——`no_accessible_name` 占枚举总数三成以上时，
    讨论 95% 前得先决定这些元素采不采；只报一个比例会把这个决定藏起来。
    """
    worst = summary["worst_case_denominator"]
    unique = summary["unique_name_eligible"]
    comp = summary["by_status"]

    def rate(arm: str) -> float:
        return summary[arm]["denom_unique"] or 0.0

    naive = rate("naive_unique_hit")
    plus = rate("plus_unique_hit")
    plus_css = rate("plus_css_unique_hit")
    auth = rate("authoritative_unique_hit")

    base = {
        "denominator": "unique_name_eligible",
        "unique_name_eligible": unique,
        "dup_name": summary["dup_name"],
        "no_accessible_name": summary["no_accessible_name"],
        "no_accessible_name_visible": summary["no_accessible_name_visible"],
        "measured_rows": summary["rows_total"],
        "rate_naive": naive,
        "rate_plus": plus,
        "rate_plus_css": plus_css,
        "rate_authoritative": auth,
        "threshold": _REAL_THRESHOLD,
        "composition": comp,
    }

    if not summary["sample_sufficient"]:
        return {
            **base,
            "verdict": "样本不足，不作结论",
            "reading": (
                f"可寻址+不可解释的元素合计只有 {worst} 个，"
                f"少于 {MIN_SAMPLE}。跑更多页或加大 --limit 再判。"
            ),
        }

    if summary["unexplained"]:
        return {
            **base,
            "verdict": "不能放行：有 unexplained",
            "reading": (
                f"{summary['unexplained']} 个元素未命中且归不进任何已知桶。"
                "这不是统计噪声——它可能是「名字对但有别的门槛」（例如在 iframe 里、"
                "被遮挡、pointer-events:none）。**逐条读 evidence 再判**，别用比例盖过去。"
            ),
            "unexplained": summary["unexplained_keys"][:20],
        }

    if auth >= _REAL_THRESHOLD:
        # 判据已过。**推荐哪条路不是判据的一部分**，是成本与残余风险的权衡，
        # 所以把两条都过线的事实一起写出来，别让读者以为只有一条可行。
        if plus_css >= _REAL_THRESHOLD:
            return {
                **base,
                "verdict": "达标（两条路都过线）",
                "suggested_path": "路径 1：采集面用 aria_snapshot 取名（推荐）",
                "reading": (
                    f"aria_snapshot 取名 {auth:.1%}（等于同源，构造上不会错），"
                    f"JS 近似补伪元素后 {plus_css:.1%}。**两条都过 "
                    f"{_REAL_THRESHOLD:.0%}**，但差的 {auth - plus_css:.1%} 是结构性的："
                    "作者写错的 role（实测被 playwright 回落到原生 role）、"
                    "以及 `content` 为 `url()`/`counter()`/`attr()` 时 JS 取不到。"
                    "残余还剩下几条事先无法枚举，正是 D12 最怕的「理由通顺的假负样本」。"
                    "代价是每页一次 `aria_snapshot`（§1.6 实测 snap_ms）。"
                ),
                "alternatives": {
                    "path_2_css": "JS 近似 + 伪元素，过线但残余不可枚举",
                },
            }
        return {
            **base,
            "verdict": "JS 近似不达标，但 aria_snapshot 达标",
            "suggested_path": "路径 1：采集面改用 playwright 的 aria_snapshot 取名",
            "reading": (
                f"playwright 自己的 (role,name) 在唯一名元素上 {auth:.1%}，"
                f"而 JS 近似（含伪元素）只到 {plus_css:.1%} → "
                "**名字必须由 playwright 算，采集层不许自己重算**。"
                "做法：枚举仍走 DOM（要可见性/几何/ref），名字取快照；"
                "代价是每页一次 `aria_snapshot`（本机实测见 §1.6 的 snap_ms）。"
            ),
        }

    if plus_css >= _REAL_THRESHOLD:
        return {
            **base,
            "verdict": "达标（JS 近似 + 伪元素内容）",
            "suggested_path": "路径 2′：JS 近似 + ::before/::after 的 content",
            "reading": (
                f"纯 JS 只到 {plus:.1%}，补 CSS 生成内容后 {plus_css:.1%} ≥ "
                f"{_REAL_THRESHOLD:.0%}。**但这是自己实现 accname 的一部分**："
                "`content` 还会是 `url()` / `counter()` / `attr()`，"
                "本探针对这些一律原样保留、不解；维护成本要算进长期账。"
            ),
        }

    if plus >= _REAL_THRESHOLD:
        return {
            **base,
            "verdict": "达标（纯 JS 近似）",
            "suggested_path": "路径 2：JS 近似（§1.5 规则 + §1.6 补的两条）",
            "reading": (
                f"唯一名元素命中 {plus:.1%} ≥ {_REAL_THRESHOLD:.0%} → "
                "自算 name 立得住，不必为取名字付 `aria_snapshot` 的每页开销。"
            ),
        }

    return {
        **base,
        "verdict": "不达标",
        "suggested_path": "路径 3：D12 编码要重选",
        "reading": (
            f"连 playwright 自己的 name 都只到 {auth:.1%} → "
            "退化到「不带 name + strict mode 列候选 + 代码按文本挑」，"
            "或重选编码。**此时停，不要进 P0。**"
        ),
    }


def _urls_of_args(args: argparse.Namespace) -> list[str]:
    """URL 来源：``--url`` / ``--urls-file`` / ``--from-archive``。**不猜。**"""
    urls: list[str] = list(args.url or [])
    if args.urls_file:
        path = Path(args.urls_file)
        if not path.exists():
            raise SystemExit(f"--urls-file 不存在：{path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    if args.from_archive:
        # 与 `cli probe --from-archive` 同源：读 P1 存档里的落地 URL，
        # 免得手打 URL——抄错一个字符就探到别的页面，而报告上完全看不出来。
        from trajectory_pipeline.executor.review_queue import collect as collect_reviews

        try:
            items = collect_reviews(Path(args.from_archive))
        except Exception as exc:                  # noqa: BLE001
            print(
                f"  [SKIP] 读 {args.from_archive} 失败：{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            items = []
        urls += [i.landed_url or i.url for i in items if (i.landed_url or i.url)]
    urls = list(dict.fromkeys(urls))              # 去重但保留顺序
    if args.url_limit:
        urls = urls[: args.url_limit]
    return urls


async def _run_real(
    urls: list[str],
    *,
    limit: int,
    timeout_ms: float,
    settle_ms: float,
    headless: bool,
) -> dict[str, Any]:
    versions: dict[str, Any] = {}
    for pkg in ("camoufox", "playwright"):
        try:
            versions[pkg] = md.version(pkg)
        except md.PackageNotFoundError:                       # pragma: no cover
            versions[pkg] = "not-installed"

    pages: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    async with AsyncCamoufox(
        os="windows",
        headless=headless,
        locale="zh-CN",
        humanize=True,            # 真实站点：开（与 §4.4 的生产口径一致）
    ) as browser:
        for url in urls:
            ctx = await browser.new_context()
            page = await ctx.new_page()
            print(f"── {url}", file=sys.stderr)
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=timeout_ms)
                if settle_ms:
                    await page.wait_for_timeout(settle_ms)
                # 固定等待不够：异步渲染的站点要等到元素总数稳定
                # （12s 上限是为了那些首屏骨架屏 + 二次渲染的站点，如 iqiyi）
                await _settle_until_stable(
                    page, max_wait_ms=max(settle_ms, 12000.0)
                )
                pages.append(
                    await _measure_real_page(
                        page, limit=limit, timeout_ms=timeout_ms
                    )
                )
            except Exception as exc:              # noqa: BLE001
                # 拦截 / 超时 / DNS：**记录，不推断**
                errors.append(
                    {"url": url, "error": f"{type(exc).__name__}: {exc}"}
                )
            finally:
                await ctx.close()

    summary = _summarize_real(pages)
    return {
        "probe": "P-1 · F5（真实页面段）",
        "versions": versions,
        "urls_requested": len(urls),
        "urls_measured": len(pages),
        "errors": errors,
        "summary": summary,
        "verdict": _verdict_real(summary),
        "pages": pages,
    }


def _print_real_report(result: dict[str, Any], *, utf8: bool) -> None:
    ascii_only = not utf8
    print("=== P-1 / F5 real-page: JS-computed (role,name) vs playwright's ===")
    print(_dump(result["versions"]))
    print(
        _dump(
            {
                "urls_requested": result["urls_requested"],
                "urls_measured": result["urls_measured"],
                "errors": result["errors"],
            }
        )
    )
    print()

    header = (
        f"{'url':<40} {'elements':<9} {'js_ms':<8} {'snap_ms':<8} "
        f"{'nodes':<7} {'changed':<8}"
    )
    print(header)
    print("-" * len(header))
    for page in result["pages"]:
        print(
            f"{page['url'][:40]:<40} {page['measured']}/{page['enumerated']:<7} "
            f"{page['js_ms']:<8} {page['snapshot_ms']:<8} {page['dom_nodes']:<7} "
            f"{str(page['dom_changed']):<8}"
        )
    print()

    print("=== summary ===")
    print(_dump(result["summary"], ascii_only=ascii_only))
    print()

    # 逐页元数据：``skipped`` 是**词表外被丢掉的元素**（真实页面上主要是
    # ``input[hidden]``）。不打印它，读者会以为「采集层只看到这些」——
    # 而被丢掉的量决定了 elements_total 的口径能不能对齐（方案 §7 风险）。
    print("=== pages meta（skipped = 词表外未枚举）===")
    for page in result["pages"]:
        meta = {k: v for k, v in page.items() if k != "rows"}
        print(_dump(meta, ascii_only=ascii_only))
    print()

    # 非 ok 的逐条明细：**这就是「人读 evidence」那一步的输入**
    interesting = [
        (page["url"], row)
        for page in result["pages"]
        for row in page["rows"]
        if row.get("status") != "ok"
    ]
    if interesting:
        print("=== non-ok rows（逐条，含 name 原文）===")
        for url, row in interesting:
            print(_dump({"url": url, **row}, ascii_only=ascii_only))
        print()

    print("=== verdict ===")
    print(_dump(result["verdict"], ascii_only=ascii_only))


def _print_report(result: dict[str, Any], *, utf8: bool) -> None:
    ascii_only = not utf8
    print("=== P-1 / F5 offline: JS-computed (role,name) vs playwright's ===")
    print(_dump(result["versions"]))
    print()

    rows = result["rows"]
    header = (
        f"{'case':<20} {'js_role':<10} {'pw_role':<10} {'status':<16} "
        "counts naive/plus/pw"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['case']:<20} {str(row.get('js_role')):<10} "
            f"{str(row.get('pw_role')):<10} {row['status']:<16} "
            f"{row.get('js_naive_exact')}/{row.get('js_plus_exact')}/{row.get('pw_exact')}"
        )
    print()

    print("=== summary ===")
    print(_dump(result["summary"], ascii_only=ascii_only))
    print()
    print("=== cost (路径 1 的代价，常数开销下界) ===")
    print(_dump(result["cost"], ascii_only=ascii_only))
    print()

    detail = [r for r in rows if r["status"] != "ok"]
    if detail:
        print("=== non-ok rows (name 原文) ===")
        for row in detail:
            print(_dump(row, ascii_only=ascii_only))
        print()

    print("=== verdict ===")
    print(_dump(result["verdict"], ascii_only=ascii_only))


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="P-1 · F5 离线段探针")
    parser.add_argument(
        "--utf8",
        action="store_true",
        help="中文不转义打印（控制台支持 UTF-8 时用；GBK 控制台会花屏）",
    )
    parser.add_argument(
        "--headful", action="store_true", help="开窗口（默认可快照的 headless）"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "退 1 当：离线段出现结构性失败（miss_both），或真实页面段判不达标。"
            "⚠️ **不拿 ≥95% 判离线比例**——离线用例故意挑边界，比例天然偏低。"
        ),
    )

    real = parser.add_argument_group("真实页面段（给任意一项就切过去，会联网）")
    real.add_argument("--url", action="append", default=None, help="可重复")
    real.add_argument("--urls-file", default=None, help="一行一个 URL，# 开头是注释")
    real.add_argument(
        "--from-archive", default=None, help="从 P1 存档读落地 URL（不用手打）"
    )
    real.add_argument(
        "--url-limit", type=int, default=None, help="最多测几个 URL（默认全测）"
    )
    real.add_argument(
        "--limit", type=int, default=50,
        help="单页最多量几个元素（默认 50，与 PageDriver.interactive 同口径）",
    )
    real.add_argument(
        "--timeout", type=float, default=30.0, help="页面/定位器超时秒数（默认 30）"
    )
    real.add_argument(
        "--settle", type=float, default=1200.0,
        help="导航后等多少毫秒让异步内容落地（默认 1200）",
    )
    real.add_argument(
        "--headless", action="store_true",
        help="无头跑真实页面（默认有头——反爬环境下无头更容易被拦）",
    )
    args = parser.parse_args(argv)

    urls = _urls_of_args(args)
    if urls:
        result = await _run_real(
            urls,
            limit=args.limit,
            timeout_ms=args.timeout * 1000,
            settle_ms=args.settle,
            headless=args.headless,
        )
        _print_real_report(result, utf8=args.utf8)
        # ``verdict`` 是「达标（…）」这类带括注的串，**别用等号**——
        # 等号会让「达标（两条路都过线）」被 --strict 判成失败，而报告上明明写着达标。
        if args.strict and not result["verdict"]["verdict"].startswith("达标"):
            return 1
        return 0

    result = await _run(headless=not args.headful)
    _print_report(result, utf8=args.utf8)

    if args.strict and result["verdict"]["structural_failures"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))