"""执行层装配的**唯一入口**——生产代码里不许直接 new 驱动。

为什么单列一支
--------------
``obscura_driver.py`` 的 docstring 一直写着「将来换浏览器，只改本文件 + factory」，
但 **factory 从未存在**：``cli.py`` 里有 6 处硬编码 ``ObscuraDriver(client)``。

硬编码本身不痛，痛的是**换实现时会漏**：漏一处就变成「一条命令跑新实现、
另一条跑旧实现」，而两边的产出物长得一模一样（同一份 P1 存档结构）——
混在一起统计时没人看得出来（方案 §8「禁止混池」）。收敛到一处，
「当前跑的是哪个实现」才是一个**可读、可断言、可写进批次元数据**的事实。

P0 的边界（设计方案 04 §2.3）
------------------------------
**纯重构、零行为变化**：P0 落地时本模块只有 obscura 一个分支，产出物必须与之前逐字节同形。
``make_driver`` 的 ``kind`` 参数就是 camoufox 分支要长的地方。

P1 加了 ``"camoufox"`` 分支。**2026-10-22 owner 裁定：默认实现改为 camoufox**
（:data:`DEFAULT_KIND`）——生产链走 camoufox，obscura 降为显式点名
（``--driver obscura``）的回溯路径。

裁定依据（**不是**「camoufox 更好看」，是三条实测读数）：

1. **反爬**：真·反爬拦截率 B（camoufox）**9.09%** < A（obscura）**14.89%**，
   配对子集同向（增量七十二）。⚠️ 必须用「真·反爬」口径——合并口径
   （反爬 + 登录墙）给出**相反**的结论（A 19.15% / B 34.09%）。
2. **页面内容**：obscura 在爱奇艺这类**客户端渲染**站上正文一个字都抽不出
   （``body_len=0`` + ``degraded=['body_text']``，19 条），A 侧 60% 的站点
   是「没测到」（H-14）。**抽不到正文的浏览器对「页面内容获取为主」的
   项目没有价值**——反而不准。
3. **速度不是目标**：本项目的目标是**减少被拦截 + 拿到页面内容**，
   速度排在后面——为此接受 camoufox 的等待时间（真人鼠标轨迹回放）。

⚠️ **obscura 没有被删掉**：它是存量 P1/P2 产物的方言（工具面
``obscura-1`` 仍在 catalog 里），也是 replay 与口径回溯的参照系。
「不再比对」≠「删掉实现」。

2026-10-26：执行层**再换一次**——camoufox（playwright 直连）暂退，
改走 **camofox-browser 自带的 MCP 适配器**（owner 裁定，方案 21）。
换的是**传输层与定位面**，不是控制流铁律：浏览器会话归
:mod:`trajectory_pipeline.executor.browser.camofox_session`，动作/采集面归
:mod:`trajectory_pipeline.executor.browser.camofox_driver`，
传输层（:mod:`trajectory_pipeline.executor.browser.mcp_client`）**没改**。

⚠️ **camoufox 那一支的代码也删了**（owner 明确许可「清理原有的 camoufox
代码与内容」）：``camoufox_driver.py`` / ``camoufox_session.py`` 已从本树
移除，**接不回来**。但**存量的方言与证据留着**：工具面 ``camoufox-1``
与采集面 ``camoufox-plain`` 仍在词表里（只读），盘上 430 份 P2 样本与
14 份 P1 存档（顶层 ``{task}__{run}.json``；连 ``.reviewed.json`` 复核副本与
探针产物共 29 个文件带该标记）靠它们才读得懂——**删的是实现，不是证据**。

camofox 分支的会话（launch）由调用方持有：一个 Run 一次 launch（方案 §4.3），
factory 只负责把会话包成驱动，不负责起停浏览器。

为什么不把 ``FakePageDriver`` 也收进来
--------------------------------------
``FakePageDriver`` 是**测试替身**，不是执行层实现。把它做成一个 ``kind``，
等于给了「生产链跑假驱动」一条正路——而这种错**不报错**，只是数据全假。
它继续留在 ``steps/`` 的测试侧，按测试需要直接构造。
"""

from __future__ import annotations

from trajectory_pipeline.executor.browser.mcp_client import McpClient
from trajectory_pipeline.executor.browser.obscura_driver import ObscuraDriver
from trajectory_pipeline.executor.browser.page_driver import PageDriver

#: 当前生产链在用的实现。
#:
#: **2026-10-26 owner 裁定 = ``"camofox"``**（camoufox 执行层暂退，改走
#: camofox-browser 的 MCP 适配器；理由见模块 docstring与设计方案 21）。
#: 改这一处就切生产链——它不是六处调用点各自的隐式约定，
#: 而是一个**可读、可断言、可写进批次元数据**的事实。
DEFAULT_KIND = "camofox"

#: 本模块认的实现名。**逗号式的白名单，不在表里就报错**——
#: 认不出时静默回落到默认实现，会让「配错了」与「配对了」产出同一份数据。
#:
#: ⚠️ **只有活着的实现**（2026-10-26）：camoufox 那一支已删，**不在表里**——
#: 所以 ``--driver camoufox`` 会**当场报错**，而不是悄悄走到别的实现上。
SUPPORTED_KINDS = ("obscura", "camofox")


def make_driver(
    client: McpClient | None = None,
    *,
    kind: str = DEFAULT_KIND,
    camofox_session: object | None = None,
    collect_element_basis: bool = False,
) -> PageDriver:
    """按 ``kind`` 造一个 :class:`PageDriver`。

    Args:
        client: MCP 连接（仅 ``kind="obscura"`` 需要）。**生命周期由调用方持有**——
            本函数不建连接、不关连接（多站遍历要复用一条连接 + tab 切换，见
            :class:`ObscuraDriver` 的 docstring）。
        kind: 实现名，见 :data:`SUPPORTED_KINDS`。
        camofox_session: 已 launch 的
            :class:`~trajectory_pipeline.executor.browser.camofox_session.CamofoxSession`
            （仅 ``kind="camofox"`` 需要）。**同样由调用方持有**——一个 Run
            一次 launch，驱动只是它的使用者。
        collect_element_basis: **跨面可比的元素账目要不要采**
            （设计方案 04 §4.9.6）。**默认关**——它多一次 evaluate 往返，
            是**测量仪器**不是生产链必需项。开着时两本账落进
            ``Observation.element_basis``。

    Raises:
        ValueError: ``kind`` 不在 :data:`SUPPORTED_KINDS` 里，或该实现缺它
            必需的句柄。**不回落、不代建**——两者都是配置错误，
            不是业务事实；静默回落会让「配错了」与「配对了」产出同一份数据。
    """
    if kind not in SUPPORTED_KINDS:
        raise ValueError(
            f"未知的执行层实现 {kind!r}；可选：{', '.join(SUPPORTED_KINDS)}"
        )
    if kind == "obscura":
        if client is None:
            raise ValueError("obscura 实现需要 McpClient，未提供")
        return ObscuraDriver(client, collect_element_basis=collect_element_basis)
    if camofox_session is None:
        raise ValueError("camofox 实现需要 CamofoxSession，未提供")
    from trajectory_pipeline.executor.browser.camofox_driver import CamofoxDriver

    # 延迟 import：camofox 走的是**另一条链**（MCP 适配器 + REST 服务），
    # obscura 那条链必须保持能跑——不该因为另一条链的可选依赖没装而 import 失败。
    return CamofoxDriver(  # type: ignore[arg-type]
        camofox_session, collect_element_basis=collect_element_basis,
    )