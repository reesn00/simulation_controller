"""训练侧工具声明的**唯一真源**。

为什么是独立包，不放在 ``assembler/`` 下
----------------------------------------
工具声明要同时喂给三个地方，而它们**互相不能 import**：

- :mod:`trajectory_pipeline.assembler.schema` —— P2 六件套第 ② 件
  （``six.tools``）；
- :mod:`trajectory_pipeline.assembler.views` —— P3 的 openai 视图
  （``tools[]``）；
- :mod:`trajectory_pipeline.assembler.task_view` —— T 的 openai 视图。

P3 与 T 是**两种独立渲染**（D14.3 / D14.6），契约测试用 AST 禁止它们
互相 import（``test_t_contract.py::Test模块图``）。所以工具清单不能挂在
``assembler/`` 任何一个子模块下——挂哪边都会被另一边的禁令扫到。

本包定位同 :mod:`trajectory_pipeline.storage`：**跨层地基**。它不 import
任何功能模块，只放字面量与纯函数。

三条口径
--------
1. **能力声明 ≠ 动作空间。**
   默认面（:data:`SURFACE_OBSCURA`）13 条里只有 6 条是执行端真发得出、
   且有存档兜底的**动作**（:data:`trajectory_pipeline.executor.actions.TOOLS`）；
   另外 7 条是**能力声明**——模型被告知存在、但本项目没有任何真实运行
   产生过它们的调用。

   为什么不因此收窄：``executor.actions.TOOLS`` 的收窄理由是「多列一个
   就会训出一批永远不会被真实运行验证过的动作」。那条顾虑针对的是
   **动作流里出现过的动作**，不是**工具清单里声明过的能力**——声明了但
   没人调用，不产生任何"验证不了的动作"。真正要防的是"模型发出了一
   个没人验证过的调用"，而那由 :attr:`ToolDef.visibility` 与样本级的
   ``meta.tools_unused`` 共同兜住（见下）。

2. **"这条轨迹没用到"是样本属性，不是工具属性。**
   所以本模块**不**给工具打"未使用"标记——同一个工具在 A 样本用了、
   B 样本没用到是正常的。哪些没用到由渲染侧按本条的 ``actions[]`` 现算，
   落进 ``meta.tools_unused``（人工复核与批次筛选看的那份元数据）。

3. **红线改写。**
   原始说明来自 :doc:`trajectory_pipeline.docs.tools`——那份文档记的是
   **obscura 原语**与 **v1 agent 会话工具**，两者的形态都**不能**直接抄
   进训练数据：

   - **不提会话句柄**（``ref`` / ``handle``）——会话内有效、跨会话失效，
     进训练数据等于让模型学一个随机数（CLAUDE.md 红线）；
   - **只说当前执行层的调用面**——D12 之后（方案 04 §5.1）这条改口径：
     禁的是**已经不是执行层**的那个实现名（obscura 面禁 obscura、
     camoufox 面禁 obscura），而**当前**执行层（camoufox 面）**要点名**
     ——不点名，模型学不会「匹配到多个元素时怎么办」，而那是执行期最
     常见的失败。MCP / 句柄 / isolate 两面都禁；
   - **不提红线能力**（Cookie / Profile / Authorization）——永久禁；
   - **不提 v1 独占形态**——v1 的 ``browser``（Browser SDK 代码）与 v2
     的 ``PageDriver`` 原语没有任何对应关系，两套不混写。

   由 ``tests/contract/test_tool_catalog_contract.py`` 用**按面分开的**
   禁用词表机械盯着——改 description 时忘了改禁用词表，测试会红；
   另有一条**正向断言**：camoufox 面的说明里必须出现 ``camoufox``
   或 ``get_by_role``，否则有人「顺手」把措辞改回抽象说法，测试不会红，
   而数据会静默地退回到「脱离本项目不成立」的状态。

工具面（surface）——方言轴
------------------------
**一个工具声明有两个正交的身份**：

- **叫什么、调什么参数**（``click`` / ``fill`` / ``press``…）——由**执行层**
  决定。D12 之后执行层是 camoufox，动作词汇表**必须等于它的调用面**，
  目标编码从 ``{tag,label}`` 变成 ``{role,name}``（方案 04 §4.5）；
- **在哪个方言里成立**（``obscura-1`` / ``camoufox-1``）——由**这批存档
  是谁跑的**决定。

这两件事以前是混在一起的：catalog 只有一份，于是「obscura 时代的
``click(tag,label)``」与「camoufox 时代的 ``click(role,name)``」只能
二选一。选前者，camoufox 的调用面就永远进不了训练数据；选后者，
盘上 361 份样本的声明当场全错。

**所以拆成两个轴**：

- **工具面**（:data:`SURFACES` 的键）= 方言。同一份 P1 存档只对应一个面；
- **工具名 + 参数**（:data:`SURFACES` 的值）= 该方言下的调用面。

**面从哪来**：P1 存档的 ``driver`` 字段 → :func:`surface_of_driver` →
P2 的 ``tool_surface`` → P3 / T 按该面渲染。**一个存档一个面**，
没有「这份样本混了两个方言」的状态。

⚠️ **两个面不混池**（方案 04 §4.5.5）：目标编码不同义，
``{tag,label}`` 与 ``{role,name}`` 的样本放在一起，模型学到的是
「有时按标签点、有时按角色点」——那是**两种任务**的混合。

**读原语（``snapshot`` / ``interactive`` / ``links``）只在 camoufox 面
里有**：push 模式下观察由执行器推给模型，模型没有「读」这个动作；
pull 模式（方案 15）下模型自己决定看什么，读原语就进了动作面，
因而也进了训练工具声明。方案 04 §2.2 末尾那句「采集面不进训练工具
声明」是 **push 模式的产物**，在 pull 模式下不成立。

参数 required 口径
-----------------
**按真实存档统计，不拍脑袋。** 盘上 361 份 P2 样本的统计（2026-10-20）::

    goto   url                        377/377  100%
    click  tag                        91/91   100%
    click  label                      91/91   100%  ← 键 91 次都在；其中 1 次值为空串
    fill   text / tag / label           5/5    100%
    press  key                         20/20   100%
    press  tag / label                  6/20    30%   ← 全局按键，不指向元素
    new_tab url                       112/112  100%

**「出现率」按参数键是否在场算，不按值是否非空算。** 这两件事不一样，
混起来就会把口径定歪：

- **键在场率 100% ⇒ required**。``click`` 的 ``label`` 键 91 次都在
  （其中 1 次值是空串——那个按钮没有可见文字），键从不缺席，所以必填；
- **非空率**另作一条护栏：required 参数的非空值比例须 ≥ 90%。``click``
  的 ``label`` 是 90/91 = 98.9%，过；若某个参数**有键但基本是空串**，
  标成必填等于教模型输出空字符串，那就该退回可选。

出现率 100% 的才 ``required``。``press`` 的 30% 印证
:func:`trajectory_pipeline.executor.actions.press` 的注释——「回车提交是
全局按键，不指向具体元素；点『搜索』按钮提交时才带目标」。

**required 会进训练数据的 function 定义**，模型会学着必填。所以它必须
是**数据的口径**而不是**设计的愿望**：把 ``press`` 的 ``tag`` 标成必填，
模型就会给每次回车都编一个元素——而真实分布里 70% 的回车没有目标。

口径漂移由 ``test_tool_catalog_contract.py::Test参数口径与真实存档一致``
机械盯着：它重跑一遍盘上样本的统计，与本模块的 ``required`` 对不上就红。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

#: 工具对模型是否可见。
#:
#: - ``model`` —— 模型可能发出、且会被记进动作流；
#: - ``infrastructure`` —— 执行器自己的会话管理动作（``new_tab`` /
#:   ``close_tab``）。它们**留在 P1 里**（删了无法回放），但**不进工具
#:   声明**——让模型看到一条它永远不该发、也不该学的 ``tool_calls``，
#:   就是教它输出越权动作。
Visibility = Literal["model", "infrastructure"]

#: 工具的**族**——它在训练动作空间里的位置。
#:
#: - ``action`` —— 模型可选的**动作**（``goto`` / ``click`` / ``fill`` /
#:   ``press``）：会改变页面状态；
#: - ``collection`` —— **读原语**（``snapshot`` / ``interactive`` /
#:   ``links``）：只读，不改页面。pull 模式下模型**自己决定看什么**，
#:   它们因此进了动作面，也就进了训练工具声明；
#: - ``capability`` —— **能力声明**：模型被告知存在，但本项目没有真实
#:   调用（那 7 条与浏览器无关的工具）；
#: - ``infrastructure`` —— 执行器自己的会话管理动作，模型永远不该选。
#:
#: 「4~8 个额外不被使用的工具」这条口径按 **``capability`` 族**计，
#: 不按「没被真实调用过」计——后者会把**读原语**也算进去，而读原语
#: 是**任务自己的工具**（pull 模式下任务真的会调它），不是额外能力。
Family = Literal["action", "collection", "capability", "infrastructure"]


@dataclass(frozen=True, slots=True)
class ToolParam:
    """一个参数。

    :attr:`required` 取自**真实存档的出现率**（见模块 docstring），
    不是设计愿望。``description`` 是给模型看的参数语义——**不写实现细节**
    （相对路径基准、句柄、超时默认值那些是 v1 运行时的事实，v2 的模型
    没有那些概念，编进去等于教它幻觉）。
    """

    name: str
    desc: str
    required: bool = True

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "desc": self.desc, "required": self.required}


@dataclass(frozen=True, slots=True)
class ToolDef:
    """一条工具声明。

    :attr:`instruction` 是**行为契约**而非实现说明——写清楚「什么时候会
    失败」「失败了该怎么办」，让模型学会在证据不足时换路，而不是重复
    同一步。

    :attr:`family` 决定它在动作空间里的位置（见 :data:`Family`）。
    ``family="infrastructure"`` 与 ``visibility="infrastructure"``
    恒等——两条都留着是因为它们回答的是**两个问题**：能不能被模型选
    （visibility）与它属于哪一族（family）。
    """

    name: str
    params: tuple[ToolParam, ...]
    instruction: str
    visibility: Visibility = "model"
    family: Family = "action"
    #: 这条工具在本项目里**有没有真实运行产生过调用**。
    #: ``False`` = 能力声明（模型被告知存在，但没有任何存档用过它）。
    #: 落进 ``meta`` 让人工复核能分清「验证过的动作」与「告知的能力」。
    #:
    #: ⚠️ 它是**逐面**的：``click`` 在 obscura 面被真实运行验证过
    #: （``{tag,label}`` 形状），在 camoufox 面没有（那个形状一次都没
    #: 跑出来过）——所以两个面的 ``click`` 是**两条不同的声明**，
    #: ``exercised`` 也各自成立。
    exercised: bool = True

    def to_json(self) -> dict[str, Any]:
        """P2 六件套第 ② 件的形状——**三字段，与既有契约一致**。

        参数级说明与 ``required`` **不进 P2**：P2 的契约形状是
        ``{name, params, instruction}``，动它就是动训练样本的字段契约。
        参数元信息落在 openai 视图的 function 定义里（那是训练框架真正
        读的那一份）。
        """
        return {
            "name": self.name,
            "params": [p.name for p in self.params],
            "instruction": self.instruction,
        }

    def param(self, name: str) -> ToolParam | None:
        for p in self.params:
            if p.name == name:
                return p
        return None


# ── 已用工具（执行端真发得出，有存档兜底）──────────────────────────

_GOTO = ToolDef(
    name="goto",
    params=(ToolParam("url", "要打开的完整网址。"),),
    instruction=(
        "打开一个网址，默认等页面加载完成。地址打不开或页面崩溃时，"
        "这一步拿不到页面内容——不要把「没看到」当成「页面是空的」，"
        "换一个地址，或如实说找不到。"
    ),
    family="action",
)

#: **obscura 面**的 ``click``：目标编码是 ``{tag,label}``。
#:
#: 这是 obscura 时代的形状（盘上 361 份样本都是它），**冻结**——
#: 改它就等于改历史样本的训练契约。camoufox 面的同名工具是
#: :data:`_CLICK_CAMOUFOX`，形状不同、``exercised`` 也不同。
_CLICK_OBSCURA = ToolDef(
    name="click",
    params=(
        ToolParam("tag", "元素类型，例如 button、a、input。"),
        ToolParam("label", "元素上的可见文字。"),
    ),
    instruction=(
        "点击页面上一个可交互元素（按元素类型与可见文字指定，"
        "例如 <button> 立即播放）。目标不在页面上时这一步不报错，"
        "但也拿不到新页面——观察里没有变化就换一个目标，不要重复点同一个。"
    ),
    family="action",
)

_FILL_OBSCURA = ToolDef(
    name="fill",
    params=(
        ToolParam("text", "要写入的文本，例如把片名填进搜索框。"),
        ToolParam("tag", "输入框的元素类型，例如 input。"),
        ToolParam("label", "输入框的可见文字或占位提示。"),
    ),
    instruction=(
        "往输入框里填文本（站内搜索：把片名填进搜索框）。"
        "这是「设值」不是追加——框里原有的内容会被清掉。"
    ),
    family="action",
)

_PRESS_OBSCURA = ToolDef(
    name="press",
    params=(
        ToolParam("key", "按键名，例如 Enter、Escape、Tab、ArrowDown。"),
        ToolParam(
            "tag",
            "可选：这次按键作用于哪个元素。回车提交是全局按键，通常不填。",
            required=False,
        ),
        ToolParam(
            "label",
            "可选：这次按键作用于哪个元素。回车提交是全局按键，通常不填。",
            required=False,
        ),
    ),
    instruction=(
        "按一个键（站内搜索：回车提交）。按键是全局的，不指向具体元素；"
        "只有当按键确实作用于某个输入框时才带上那个元素。"
    ),
    family="action",
)

# ── camoufox 面：动作词汇表 = camoufox 的调用面（D12）──────────────
#
# 方案 04 §2.2 的动作表 + §4.5 的目标编码。**参数形状与执行层 1:1**，
# 说明里**点名 camoufox 与 get_by_role**（方案 04 §5.1）——不点名，
# 模型学不会「匹配到多个元素时怎么办」，而那是执行期最常见的失败。
#
# ⚠️ ``exercised`` 逐条成立，**不是**整段一起成立（2026-10-21 补记）：
#
# - ``click``：**已验证**——2026-10-20 的第一轮双跑（DR01《让子弹飞》，
#   camoufox 侧）真实产生过 3 次 ``{role, name}`` 目标的点击，落在
#   ``output/pipeline/dualrun/camoufox/``。**它的 ``required`` 口径
#   （``role`` 与 ``name`` 都必填）是按那次真实调用校出来的**，不是猜的。
# - ``fill`` / ``press``：**仍是能力声明**——那批 camoufox 存档里
#   没跑出过这个形状（脚本化链没走到填搜索框那一步）。
#
# ⚠️ 之前这三条一律标 ``exercised=False``，理由是「camoufox 面一次都没
# 跑过」。**那条理由已经不成立**（2026-10-20 跑过了），而标注没跟着改——
# 于是契约测试查不到真实调用，只能「按没跑过」处理。**标注撒谎比
# 标注缺失更难发现**：它会让 ``required`` 口径永远不被真实数据校准。
# 2026-10-21 把契约测试的存档扫描改成**递归**（原先 glob 不递归，
# ``dualrun/`` 下的存档看不见）之后，这条断言立刻变红并指名了要改哪一条。
#
# 它们是**已验证能力的方言变体**，不是「本项目没有的能力」——两者的区别
# 在 ``family`` 与 ``exercised`` 上分开记。

_CLICK_CAMOUFOX = ToolDef(
    name="click",
    params=(
        ToolParam("role", "元素的无障碍角色，例如 button、link、textbox。"),
        ToolParam(
            "name",
            "元素的可访问名——从元素清单里逐字照抄，不要改写、不要补全。",
        ),
    ),
    instruction=(
        "点击一个元素。camoufox 用 get_by_role(role, name=name, exact=True) 定位，"
        "role 与 name 都必须与你在清单里看到的完全一致；"
        "name 是可访问名——图标按钮算它的 alt，输入框算它的占位提示。"
        "定位不到时这一步不报错，页面也不会有任何变化——"
        "重新读一遍页面，换一个清单里真有的目标，别重复点同一个。"
        "页面上有相当一部分元素没有可访问名，那些定位不到，别选它们。"
    ),
    family="action",
    exercised=True,
)

_FILL_CAMOUFOX = ToolDef(
    name="fill",
    params=(
        ToolParam("role", "输入框的无障碍角色，例如 textbox、searchbox。"),
        ToolParam("name", "输入框的可访问名——搜索框常是「搜索」这类占位提示。"),
        ToolParam("text", "要写入的文本，例如把片名填进搜索框。"),
    ),
    instruction=(
        "往输入框里填文本（站内搜索：把片名填进搜索框）。"
        "camoufox 用 get_by_role(role, name=name, exact=True) 定位输入框，"
        "role 与 name 都照清单里写。"
        "这是「设值」不是追加——框里原有的内容会被清掉。"
        "定位不到时框里不会出现内容，别当已经填进去了，换一个清单里真有的输入框。"
    ),
    family="action",
    exercised=False,
)

_PRESS_CAMOUFOX = ToolDef(
    name="press",
    params=(
        ToolParam("key", "按键名，例如 Enter、Escape、Tab、ArrowDown。"),
        ToolParam(
            "role",
            "可选：这次按键作用于哪个元素。回车提交是全局按键，通常不填。",
            required=False,
        ),
        ToolParam(
            "name",
            "可选：这次按键作用于哪个元素。回车提交是全局按键，通常不填。",
            required=False,
        ),
    ),
    instruction=(
        "按一个键（站内搜索：回车提交）。不带 role 与 name 时按键发给当前页面；"
        "带上时 camoufox 用 get_by_role(role, name=name, exact=True) 定位元素再按键，"
        "两者都照清单里写。"
        "按键是全局的，不指向具体元素；只有当按键确实作用于某个输入框时才带上那个元素。"
    ),
    family="action",
    exercised=False,
)

# ── camoufox 面：读原语（pull 模式，模型自己决定看什么）────────────
#
# **只在 camoufox 面里有**：push 模式下观察由执行器推给模型，模型没有
# 「读」这个动作；pull 模式（方案 15 §5.4）下模型自己发起观察，读原语
# 就进了动作面，因而也进了训练工具声明。
#
# 三条的**名字 = 驱动原语的名字**（``PageDriver.snapshot`` /
# ``.interactive`` / ``.links``）——一个词表，不造第二套。
# 参数**一律不给**：条数上限是**执行层的护栏**（模型说了不算），
# 而给了参数就得声明类型，声明了类型就有「模型以为能分页、实际不能」
# 的风险。

_SNAPSHOT = ToolDef(
    name="snapshot",
    params=(),
    instruction=(
        "读当前页：网址、标题与正文文本。只读，没有副作用。"
        "正文与条数都有上限，看到的不是全量。"
        "读到的正文是空的时先当「还没读到」，别当「页面是空的」——"
        "再读一次，或者换个页面确认。"
    ),
    family="collection",
    exercised=False,
)

_INTERACTIVE = ToolDef(
    name="interactive",
    params=(),
    instruction=(
        "读当前页可交互元素的清单：每条给出它的 role（button、link、textbox 等）"
        "与 name（可访问名）。"
        "点击与填值的目标只能从这份清单里选，role 与 name 逐字照抄。"
        "name 为空的元素定位不到，别选。"
        "清单有条数上限，不是全量。"
    ),
    family="collection",
    exercised=False,
)

_LINKS = ToolDef(
    name="links",
    params=(),
    instruction=(
        "读当前页的链接：每条给出可见文字与网址。"
        "链接文字常常是面包屑而不是页面标题——别拿它当标题。"
        "清单有条数上限，不是全量。"
    ),
    family="collection",
    exercised=False,
)

# ── 执行器自己的会话管理动作（模型永远不该选）──────────────────────

_NEW_TAB = ToolDef(
    name="new_tab",
    params=(ToolParam("url", "要打开的完整网址。"),),
    instruction="在新标签页打开网址。",
    visibility="infrastructure",
    family="infrastructure",
)

_CLOSE_TAB = ToolDef(
    name="close_tab",
    params=(),
    instruction="关闭当前标签页。",
    visibility="infrastructure",
    family="infrastructure",
    # 执行端**能**发（:data:`trajectory_pipeline.executor.actions.TOOLS`
    # 里有它），但盘上 75 份存档里还没有一次真正落地的调用——外链校验
    # 那条链（``new_tab`` 开、看完关）还没走到过。按「有没有真实调用」
    # 的口径记 False，别把它算进已验证能力。
    exercised=False,
)

# ── 额外工具（能力声明：模型被告知存在，本项目无真实调用）──────────
#
# 挑选准则（见模块 docstring 与 docs/tools.md）：
#   ① 语义与「帮用户找在线观看资源」相邻；
#   ② v2 概念上可实现；
#   ③ 不碰 v1 独占形态、不碰红线、不碰文件系统写入与多 agent 协作
#      （那会教模型去做代码任务，而不是找资源）。
#
# ⚠️ 与 docs/tools.md 的差异：上游原文给 web_search / web_fetch 的兜底
# 指引是「退到 execute_shell_command 跑 curl」。本工具集**不含 shell**
# （刻意的——代码执行能力离本场景太远），所以那半句改写成
# 「失败时如实说」。这是**能力集变更导致的改写**，不是改语义。

_WEB_SEARCH = ToolDef(
    name="web_search",
    params=(ToolParam("search_term", "检索词。技术类查询带上版本号或日期更准。"),),
    instruction=(
        "在互联网上检索，给出与检索词相关的网站与摘要。"
        "检索服务有速率限制，网络或配额出错时这个工具会失败——"
        "失败就如实说搜不了，不要编造结果。"
    ),
    family="capability",
    exercised=False,
)

_WEB_FETCH = ToolDef(
    name="web_fetch",
    params=(ToolParam("url", "要抓取的完整网址。"),),
    instruction=(
        "抓取一个网址的内容并转成可读文本。只读，无副作用；"
        "不支持需要登录的地址，也不返回图片、PDF 这类二进制内容。"
        "抓不到或内容为空时这个工具会失败——如实说，别用猜测补上。"
    ),
    family="capability",
    exercised=False,
)

_VIEW_IMAGE = ToolDef(
    name="view_image",
    params=(
        ToolParam("image_path", "图片的本地路径或 HTTP(S) 网址。"),
    ),
    instruction=(
        "把一张图片载入上下文（海报、播放画面截图、验证码图等）。"
        "远程图片会先下载并校验再载入。"
    ),
    family="capability",
    exercised=False,
)

_DESKTOP_SCREENSHOT = ToolDef(
    name="desktop_screenshot",
    params=(
        ToolParam(
            "path",
            "可选：保存路径。留空存到当前工作目录；文件名以 .png 结尾则存 PNG。",
            required=False,
        ),
    ),
    instruction=(
        "截取整个桌面或某个窗口的画面，存成图片文件。"
        "截整个桌面在所有平台都可用；只截指定窗口仅 macOS 可用。"
    ),
    family="capability",
    exercised=False,
)

_GET_CURRENT_TIME = ToolDef(
    name="get_current_time",
    params=(),
    instruction=(
        "取当前时间，格式为「年-月-日 时:分:秒 时区 (星期几)」。"
        "需要给时间做锚点时调用。"
    ),
    family="capability",
    exercised=False,
)

_READ_FILE = ToolDef(
    name="read_file",
    params=(
        ToolParam("file_path", "要读的文件路径。"),
        ToolParam(
            "start_line",
            "可选：从第几行开始读（1 起，含）。省略就从文件开头读。",
            required=False,
        ),
        ToolParam(
            "end_line",
            "可选：读到第几行（1 起，含）。省略就读到文件末尾。",
            required=False,
        ),
    ),
    instruction=(
        "读一个文本文件，输出带行号。内容太长被截断时，"
        "末尾会告诉你从第几行续读。图片、PDF 这类二进制文件不会报错，"
        "但读回来的字节没有意义——那类文件改用专门看图的方式。"
    ),
    family="capability",
    exercised=False,
)

_GLOB_SEARCH = ToolDef(
    name="glob_search",
    params=(
        ToolParam("pattern", "文件名的匹配模式，例如 *.py、**/*.json。"),
        ToolParam(
            "path",
            "可选：从哪个目录开始找。省略则从当前工作目录开始。",
            required=False,
        ),
    ),
    instruction=(
        "按文件名匹配模式查找文件，返回命中的路径列表。"
        "只看文件名，不看文件内容。"
    ),
    family="capability",
    exercised=False,
)


# ── 工具面（方言）───────────────────────────────────────────────────

#: obscura 面的标识。**盘上 361 份样本都是这个面**（目标编码
#: ``{tag,label}``），冻结。
#:
#: ⚠️ **2026-10-22 owner 裁定后：只读存量面，不再产出。**
#: 生产链默认执行层已是 camoufox（见
#: :data:`trajectory_pipeline.executor.browser.driver_factory.DEFAULT_KIND`），
#: 新批次走 :data:`SURFACE_CAMOUFOX`。这个面**留在这里不删**——
#: 存量存档正是唯一的真实证据，而它们全在这个面上。
SURFACE_OBSCURA = "obscura-1"

#: camoufox 面的标识。目标编码 ``{role,name}`` + 三个读原语。
#:
#: **2026-10-22 起是生产面**（owner 裁定切 camoufox 为主力执行层）。
#: 此前它是**目标方言的声明**（方案 15 §7 的 R2）——第一份真实数据
#: 要等第一批 camoufox 批次落盘才出现。
SURFACE_CAMOUFOX = "camoufox-1"

#: **默认面**。没有记录方言的存档（历史批次）按它渲染。
#:
#: ⚠️ **它不是「生产面」**——生产面由 P1 的 ``driver`` 一对一折算
#: （:func:`surface_of_driver`），而 2026-10-22 起生产链写的是
#: ``driver="camoufox"`` ⇒ 折算到 :data:`SURFACE_CAMOUFOX`。
#: 这个默认值服务的是**没记 driver 的历史存档**，而那批存档真实跑的
#: 就是 obscura——所以默认取 obscura 是**如实**的，不是「保守」。
DEFAULT_SURFACE = SURFACE_OBSCURA

#: 驱动实现名 → 工具面。**1:1**——一个驱动实现只有一个调用面。
#:
#: ⚠️ 这张表是「方言」的**唯一**来源：不是两个独立的轴（执行器 × 编码），
#: 而是**一个**轴。写成两个独立字段，两边就会漂——出现「driver=camoufox
#: 但编码=obscura」这种**自相矛盾**的存档，而它长得与正常存档一模一样。
DRIVER_SURFACE: Mapping[str, str] = {
    "obscura": SURFACE_OBSCURA,
    "camoufox": SURFACE_CAMOUFOX,
}


#: 一个方言的**三个投影**：元素词汇表 + 存档键名。**一处定义，不许拆开抄。**
#:
#: 这三样说的是同一件事——「这批数据的 ``(kind, text)`` 槽里装的是哪套值、
#: 在存档里写成哪两个键」。拆成三张独立的表就会漂，漂出来的形态是
#: 「声明是 ``role``/``name``、存档写 ``tag``/``label``」，而它在报告上
#: 与正常数据长得一模一样（CLAUDE.md：面由 P1 的 ``driver`` 一对一折算，
#: 认不出直接抛）。
#:
#: 词表字面量的**类型侧定义**在 :mod:`trajectory_pipeline.perception.base`
#: （``ELEMENT_VOCAB_*``）——那边是消费方（按词表筛元素），这里是方言轴；
#: 两者由 ``test_tool_catalog_contract.py`` 的同步断言钉住，不许出现第三个词表。
@dataclass(frozen=True, slots=True)
class Dialect:
    """一个工具面的元素方言。"""

    #: 交互元素的值域（``html_tag`` / ``aria_role``），
    #: 取值来自 :data:`trajectory_pipeline.perception.base.ELEMENT_VOCABULARIES`。
    vocabulary: str
    #: 存档里 kind 槽的键名（``tag`` / ``role``）。
    kind_key: str
    #: 存档里 text 槽的键名（``label`` / ``name``）。
    text_key: str


#: 工具面 → 方言。**与 :data:`SURFACES` 同键**（契约测试按集合相等盯着）。
DIALECTS: Mapping[str, Dialect] = {
    SURFACE_OBSCURA: Dialect("html_tag", "tag", "label"),
    SURFACE_CAMOUFOX: Dialect("aria_role", "role", "name"),
}


def dialect_of(surface: str = DEFAULT_SURFACE) -> Dialect:
    """工具面 → 方言。面认不出 → **抛**（不回落默认面）。"""
    try:
        return DIALECTS[surface]
    except KeyError:
        raise ValueError(
            f"认不出的工具面 {surface!r}——方言无从确定。"
            f"已知的：{sorted(DIALECTS)}"
        ) from None


def surface_of_driver(driver: str | None) -> str:
    """驱动实现名 → 工具面。

    - **空串 / ``None``** → :data:`DEFAULT_SURFACE`。那是「这份存档没记
      驱动」——历史批次都是这样。空串**不**等于「未知方言」：它是
      「没记」，按默认面渲染，且默认面**就是**那批存档真实跑出来的面；
    - **认不出的实现名** → **抛**。静默回落到默认面，会让「配错了」
      与「配对了」产出**同一份**工具声明——而声明错的那份在报告上
      看不出任何异常。与 ``driver_factory`` 同一个纪律（认不出就报错，
      不回落）。
    """
    name = str(driver or "").strip()
    if not name:
        return DEFAULT_SURFACE
    try:
        return DRIVER_SURFACE[name]
    except KeyError:
        raise ValueError(
            f"认不出的驱动实现名 {name!r}——工具面无从确定。"
            f"已知的：{sorted(DRIVER_SURFACE)}"
        ) from None


#: 各工具面的声明全集。**唯一真源**。
#:
#: 顺序 = 喂给模型时的顺序：先动作，再读原语，再额外能力，最后基础
#: 设施动作（后两者对模型不可见，但留在 P2 的 ``six.tools`` 里——动作流
#: 要能回放）。
SURFACES: Mapping[str, tuple[ToolDef, ...]] = {
    SURFACE_OBSCURA: (
        _GOTO,
        _CLICK_OBSCURA,
        _FILL_OBSCURA,
        _PRESS_OBSCURA,
        _WEB_SEARCH,
        _WEB_FETCH,
        _VIEW_IMAGE,
        _DESKTOP_SCREENSHOT,
        _GET_CURRENT_TIME,
        _READ_FILE,
        _GLOB_SEARCH,
        _NEW_TAB,
        _CLOSE_TAB,
    ),
    SURFACE_CAMOUFOX: (
        _GOTO,
        _CLICK_CAMOUFOX,
        _FILL_CAMOUFOX,
        _PRESS_CAMOUFOX,
        _SNAPSHOT,
        _INTERACTIVE,
        _LINKS,
        _WEB_SEARCH,
        _WEB_FETCH,
        _VIEW_IMAGE,
        _DESKTOP_SCREENSHOT,
        _GET_CURRENT_TIME,
        _READ_FILE,
        _GLOB_SEARCH,
        _NEW_TAB,
        _CLOSE_TAB,
    ),
}

#: **默认面**的声明全集。**唯一真源**。
#:
#: 保留这个名字是因为下游（assembler / 契约测试）大量按「默认面」取
#: 声明；带面的取法是 :func:`defs_of`。
CATALOG: tuple[ToolDef, ...] = SURFACES[DEFAULT_SURFACE]


# ── 查询 ────────────────────────────────────────────────────────────


def defs_of(surface: str = DEFAULT_SURFACE) -> tuple[ToolDef, ...]:
    """某个面的声明全集。面认不出 → **抛**（不猜）。"""
    try:
        return SURFACES[surface]
    except KeyError:
        raise ValueError(
            f"认不出的工具面 {surface!r}——已知的：{sorted(SURFACES)}"
        ) from None


def by_name(name: str, surface: str = DEFAULT_SURFACE) -> ToolDef | None:
    """按名字取一条工具声明。取不到返回 ``None``——**不猜**。

    ⚠️ **同名不同面**：``click`` 在两个面里参数不同（``{tag,label}`` vs
    ``{role,name}``）。所以面是**必答项**——用默认面去查一份 camoufox
    样本，会渲染出错的参数形状，而那正是「声明与调用对不上」。
    """
    for d in defs_of(surface):
        if d.name == name:
            return d
    return None


def visible_defs(surface: str = DEFAULT_SURFACE) -> tuple[ToolDef, ...]:
    """模型看得见的工具声明。"""
    return tuple(d for d in defs_of(surface) if d.visibility == "model")


def visible_names(surface: str = DEFAULT_SURFACE) -> tuple[str, ...]:
    return tuple(d.name for d in visible_defs(surface))


def capability_names(surface: str = DEFAULT_SURFACE) -> tuple[str, ...]:
    """**能力声明**（``family="capability"``）的工具名。

    「任务调用的工具 + 4~8 个额外不被使用的工具」这条口径按**它**计，
    不按「没被真实调用过」计——后者会把读原语也算进去，而读原语是
    **任务自己的工具**（pull 模式下任务真的会调它）。
    """
    return tuple(d.name for d in defs_of(surface) if d.family == "capability")


def unexercised_names(surface: str = DEFAULT_SURFACE) -> tuple[str, ...]:
    """**告知了但没有真实调用过**的工具名。

    模型被告知存在、但本项目没有任何真实运行产生过调用。
    落进 ``meta`` 让人工复核能分清「验证过的动作」与「告知的能力」——
    这条信息不显眼时，读样本的人会把未验证的能力当成已验证的事实。

    ⚠️ 它是**逐面**的：换面 = 换一批声明，``exercised`` 各自成立。
    """
    return tuple(d.name for d in visible_defs(surface) if not d.exercised)


def infra_names(surface: str = DEFAULT_SURFACE) -> tuple[str, ...]:
    return tuple(d.name for d in defs_of(surface) if d.visibility == "infrastructure")


# ── 渲染成 OpenAI function 定义 ────────────────────────────────────


def openai_function(d: ToolDef) -> dict[str, Any]:
    """一条工具声明 → OpenAI function 定义。

    与 :func:`trajectory_pipeline.assembler.views._openai_tool` **同款
    形状**，但这里多带一样东西：**参数的 ``description`` 与正确的
    ``required`` 集合**。

    为什么必须多带：``click`` 的语义目标在 ``target={tag,label}`` 里，
    而动作的 ``params`` 是空的——只声明 ``params`` 的话 function 定义会
    长成 ``properties: {} / required: []``，**模型无从知道要传什么**，
    而真实调用里是 ``{"tag": ..., "label": ...}``。声明与调用对不上，
    训出来的模型就会发空参数的点击。

    ⚠️ ``type`` 一律 ``"string"``，**不编造类型也不加 enum**：
    ``press`` 的 ``key`` 本来就是任意按键名，加了 enum 会把训练集框死在
    一个取值子集上。
    """
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in d.params:
        props[p.name] = {"type": "string", "description": p.desc}
        if p.required:
            required.append(p.name)
    return {
        "type": "function",
        "function": {
            "name": d.name,
            "description": d.instruction,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


def openai_functions(
    defs: tuple[ToolDef, ...] | None = None,
    surface: str = DEFAULT_SURFACE,
) -> list[dict[str, Any]]:
    """一批工具声明 → OpenAI function 定义列表。

    ``defs=None`` 时取 :func:`visible_defs`（模型可见集）。
    """
    chosen = defs if defs is not None else visible_defs(surface)
    return [openai_function(d) for d in chosen]


__all__ = [
    "Dialect",
    "Visibility",
    "Family",
    "ToolParam",
    "ToolDef",
    "SURFACE_OBSCURA",
    "SURFACE_CAMOUFOX",
    "DEFAULT_SURFACE",
    "DRIVER_SURFACE",
    "DIALECTS",
    "dialect_of",
    "SURFACES",
    "CATALOG",
    "surface_of_driver",
    "defs_of",
    "by_name",
    "visible_defs",
    "visible_names",
    "capability_names",
    "unexercised_names",
    "infra_names",
    "openai_function",
    "openai_functions",
]
