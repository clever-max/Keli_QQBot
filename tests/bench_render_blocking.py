"""验证 R1：渲染放进 to_thread 后，事件循环是否真的不再被阻塞。

做法：跑一个 10ms 一次的"心跳"协程，分别用「同步调用渲染」和「await to_thread(渲染)」
各跑一次，数心跳次数。同步时心跳几乎停摆，换成线程后应基本不丢。
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))
import nonebot  # noqa: E402

nonebot.init()
from plugins.group_bot.common.card import CardEntry, render_card_page  # noqa: E402

ENTRIES = [
    CardEntry(user_id=str(i), name=f"群友{i}", right_text=f"第 {i} 条",
              meta_text="昨天 21:03 · 文本", text=f"这是第 {i} 条消息的内容，用来把卡片画满一点")
    for i in range(1, 6)
]
TEMP = Path(tempfile.gettempdir()) / "recall_verify"


def draw():
    return render_card_page("群消息 · 撤回记录", "共 5 条 · 第 1/1 页", ENTRIES,
                            group_id=123450001, directory=TEMP)


async def heartbeat(counter: list[int], stop: asyncio.Event) -> None:
    while not stop.is_set():
        counter[0] += 1
        await asyncio.sleep(0.01)


async def measure(label: str, use_thread: bool) -> None:
    counter = [0]
    stop = asyncio.Event()
    task = asyncio.create_task(heartbeat(counter, stop))
    await asyncio.sleep(0.05)          # 让心跳先跑起来
    baseline = counter[0]
    start = time.perf_counter()
    if use_thread:
        path = await asyncio.to_thread(draw)
    else:
        path = draw()
    elapsed = (time.perf_counter() - start) * 1000
    ticks = counter[0] - baseline
    stop.set()
    await task
    path.unlink(missing_ok=True)
    print(f"  {label:<34} 渲染 {elapsed:6.1f} ms   期间心跳 {ticks:3d} 次"
          f"   （按 10ms 一次，期望约 {int(elapsed / 10)} 次）")


async def main() -> None:
    print("=== 事件循环阻塞对比（同一张 5 行卡片）===")
    await measure("同步调用（改动前）", use_thread=False)
    await measure("await asyncio.to_thread（改动后）", use_thread=True)


asyncio.run(main())
