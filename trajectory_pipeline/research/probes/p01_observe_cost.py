"""S3 代价实测：open_tab 之后那次 observe() 的真实耗时。

S3 之前 open_tab 只新建 tab，不 observe ⇒ 首步动作替它完成枚举。
S3 之后出站多一次 observe（快照往返）——本脚本量这一次往返要多少毫秒。
"""
import asyncio
import statistics
import sys

from trajectory_pipeline.executor.browser.camofox_session import CamofoxSession
from trajectory_pipeline.executor.browser.driver_factory import make_driver

URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.youku.com/"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3


async def main():
    session = await CamofoxSession.launch()
    d = make_driver(kind="camofox", camofox_session=session)
    times = []
    try:
        for i in range(N):
            t0 = asyncio.get_running_loop().time()
            await d.new_tab(URL)
            t_new = asyncio.get_running_loop().time()
            obs = await d.observe()
            t_obs = asyncio.get_running_loop().time()
            ms_new = (t_new - t0) * 1000
            ms_obs = (t_obs - t_new) * 1000
            times.append(ms_obs)
            n_el = len(obs.interactive_elements or ())
            print(f"#{i}: new_tab={ms_new:.0f}ms  observe={ms_obs:.0f}ms  elements={n_el}")
    finally:
        await d.close()
    if times:
        print()
        print(f"observe 往返: n={len(times)} median={statistics.median(times):.0f}ms "
              f"min={min(times):.0f} max={max(times):.0f}")


asyncio.run(main())
