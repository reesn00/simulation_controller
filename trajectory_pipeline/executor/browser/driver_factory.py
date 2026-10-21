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

P1 加了 ``"camoufox"`` 分支，但**默认实现仍是 obscura**（:data:`DEFAULT_KIND`）——
生产链一行没变，camoufox 只由 ``cli check --driver camoufox`` 显式点名进入。
这样「默认值改了没」是一个可读的事实，而不是六处调用点各自的隐式约定。
camoufox 分支的会话（launch）由调用方持有：一个 Run 一次 launch（方案 §4.3），
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

#: 当前生产链唯一在用的实现。D11 完成后这里会改成 ``"camoufox"``。
DEFAULT_KIND = "obscura"

#: 本模块认的实现名。**逗号式的白名单，不在表里就报错**——
#: 认不出时静默回落到默认实现，会让「配错了」与「配对了」产出同一份数据。
SUPPORTED_KINDS = ("obscura", "camoufox")


def make_driver(
    client: McpClient | None = None,
    *,
    kind: str = DEFAULT_KIND,
    camoufox_session: object | None = None,
) -> PageDriver:
    """按 ``kind`` 造一个 :class:`PageDriver`。

    Args:
        client: MCP 连接（仅 ``kind="obscura"`` 需要）。**生命周期由调用方持有**——
            本函数不建连接、不关连接（多站遍历要复用一条连接 + tab 切换，见
            :class:`ObscuraDriver` 的 docstring）。
        kind: 实现名，见 :data:`SUPPORTED_KINDS`。
        camoufox_session: 已 launch 的 :class:`CamoufoxSession`（仅
            ``kind="camoufox"`` 需要）。**同样由调用方持有**——一个 Run
            一次 launch，驱动只是它的使用者。

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
        return ObscuraDriver(client)
    if camoufox_session is None:
        raise ValueError("camoufox 实现需要 CamoufoxSession，未提供")
    from trajectory_pipeline.executor.browser.camoufox_driver import CamoufoxDriver

    # 延迟 import：camoufox 是可选依赖（D11 阶段 P1），default 路径不该因为
    # 它没装而 import 失败——obscura 生产链必须保持能跑。
    return CamoufoxDriver(camoufox_session)  # type: ignore[arg-type]