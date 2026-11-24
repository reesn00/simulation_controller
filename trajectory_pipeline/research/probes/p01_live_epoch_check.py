"""P0-1 现场验证（真实浏览器）：open_tab 之后，用「入口页的观察」vs「新页的观察」
定位同一个目标，看驱动给出什么结果。

S3 前：`open_tab` 只返回 tab_id，调用方拿 site_obs 的元素清单去点。
S3 后：`open_tab` 返回 PageContext(tab_id, obs)，调用方拿新页的清单去点。

判据：
  - 用入口页清单定位 ⇒ 期望 StaleContextError（新版）或静默 not_found（旧版）
  - 用新页清单定位   ⇒ 期望命中
"""
import asyncio
import sys

from trajectory_pipeline.executor.browser.camofox_session import CamofoxSession
from trajectory_pipeline.executor.browser.driver_factory import make_driver
from trajectory_pipeline.executor.browser.page_driver import StaleContextError

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.youku.com/"


async def main():
    session = await CamofoxSession.launch()
    d = make_driver(kind="camofox", camofox_session=session)
    try:
        # ① 在「入口页」上观察
        await d.goto(URL)
        site_obs = await d.observe()
        site_names = [
            (e.role, e.name)
            for e in (site_obs.interactive_elements or ())
            if e.name
        ]
        print(f"入口页: {len(site_names)} 个有名元素, epoch={d.page_epoch}")

        # ② 开新 tab（S3 之前调用方只拿到 tab_id；现在拿到 PageContext）
        tab_id = await d.new_tab(URL)
        new_obs = await d.observe()
        new_names = [
            (e.role, e.name)
            for e in (new_obs.interactive_elements or ())
            if e.name
        ]
        print(f"新 tab: {len(new_names)} 个有名元素, epoch={d.page_epoch}")

        # 找一个两边都有的目标，用它对比「旧清单 vs 新清单」
        common = [n for n in site_names if n in set(new_names)]
        print(f"两侧共有目标: {len(common)}")

        if not common:
            print("!! 没有共有目标，本次无法对比（换 URL 重试）")
            return
        role, name = common[0]
        print(f"对比目标: role={role!r} name={name!r}")

        # ③ 模拟「旧清单」：把驱动内的枚举世代退回入口页
        #    （S3 前的形态 = 调用方手上是入口页的清单，而驱动已换页）
        d._enum_epoch = d.page_epoch - 1
        try:
            await d.click(role=role, name=name)
            print(f"用旧清单点击: 没抛错, outcome={d.last_action.get('outcome')}")
        except StaleContextError as exc:
            print(f"用旧清单点击: StaleContextError({exc})")

        # ④ 用当前（新页）清单点击
        await d.observe()
        try:
            await d.click(role=role, name=name)
            print(f"用新清单点击: outcome={d.last_action.get('outcome')}")
        except StaleContextError as exc:
            print(f"用新清单点击: StaleContextError({exc})")
    finally:
        await d.close()


asyncio.run(main())
