"""漂流瓶「软防重复抽样」回归测试。

要锁住的语义：

- ``preferred_order`` 是**排序**而不是"禁抽"：池子里每条内容都还在结果里，只是顺序变了，
  所以不存在"某几天内不许抽"的硬窗口；
- 排序优先级 = 最近一次被抽到的时间（从未抽过视为最旧），且只严格排序真正会被尝试的头部；
- ``mode=off`` / ``pick=1`` 时回到纯随机，便于一键回滚；
- 两个池子的选取都真的接上了它；**硬性的"当天不重复"仍然优先于它**；
- 保留期 ``ESSENCE_DRAW_RETENTION_DAYS`` 跟 ``BOTTLE_HISTORY_DAYS`` 一致，且 ``draw_history``
  只回窗口内的最近一次时间。
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import asyncio
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
DB = TEMP / "verify_repeat.sqlite3"
for stale in TEMP.glob("verify_repeat.sqlite3*"):
    stale.unlink()

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001",
    authorized_users="100000009",
    database_path=str(DB),
)

from plugins.group_bot.bottle import essence as gb  # noqa: E402
from plugins.group_bot.bottle import pool as gb_pool
from plugins.group_bot.media.album import AlbumPhoto  # noqa: E402
from plugins.group_bot.core.config import (  # noqa: E402
    BOTTLE_HISTORY_DAYS,
    BOTTLE_REPEAT_PICK_K,
)
from plugins.group_bot.bottle.essence import (  # noqa: E402
    ESSENCE_DRAW_RETENTION_DAYS,
    choose_album_photo,
    choose_essence,
    draw_history,
    local_day_key,
    preferred_order,
    record_draw,
)
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402

GROUP = 123450001
failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


def day_offset(days: int) -> str:
    return (datetime.now(gb.GROUP_TIMEZONE) - timedelta(days=days)).date().isoformat()


def essence_item(key: str) -> dict:
    return {
        "message_id": key,
        "sender_id": "11",
        "sender_nick": "甲",
        "operator_id": "22",
        "operator_nick": "乙",
        "content": [{"type": "text", "data": {"text": f"内容 {key}"}}],
    }


def album_photo(key: str) -> AlbumPhoto:
    return AlbumPhoto(media_id=key, url="https://x.qpic.cn/a.jpg", album_name="相册")


async def clear() -> None:
    async with connect() as db:
        await db.execute("DELETE FROM essence_draws WHERE group_id=?", (str(GROUP),))
        await db.execute("DELETE FROM essence_usage WHERE group_id=?", (str(GROUP),))
        await db.commit()


def check_order_contract() -> None:
    print("\n== 1. preferred_order 的契约 ==")
    keys = [f"k{i}" for i in range(10)]
    history = {f"k{i}": 1000.0 + i for i in range(6)}  # k0 最旧 ... k5 最新；k6-k9 从未抽过
    # k >= 池子大小时退化为严格 LRU，可以断言完整顺序
    strict = preferred_order(keys, history, key=lambda k: k, mode="lru", pick=10, head=10)
    check("结果长度不变", len(strict), len(keys))
    check("结果无重复", len(set(strict)), len(keys))
    check("结果集合不变（不是「禁抽」）", sorted(strict), sorted(keys))
    check_true("严格模式下从未抽过的排在最前", set(strict[:4]) == {"k6", "k7", "k8", "k9"})
    check("严格模式下已抽过的按「越旧越前」", strict[4:], ["k0", "k1", "k2", "k3", "k4", "k5"])

    # k=5 时是概率性的：绝大多数情况下队首仍是"最久没抽的"，但不是保证
    random.seed(3)
    firsts = [
        preferred_order(keys, history, key=lambda k: k, mode="lru", pick=5, head=10)[0]
        for _ in range(200)
    ]
    fresh = sum(1 for k in firsts if k in {"k6", "k7", "k8", "k9"})
    print(f"     k=5 时队首落在「从未抽过」的比例：{fresh}/200")
    check_true("k=5 时队首大概率落在最旧的一侧", fresh >= 150)
    check_true("但不是每次（说明仍有随机性）", fresh < 200)

    print("\n== 2. 回滚开关与边界 ==")
    random.seed(7)
    off_order = preferred_order(keys, history, key=lambda k: k, mode="off")
    check_true("mode=off 仍返回全部内容", sorted(off_order) == sorted(keys))
    check_true("mode=off 不强制最旧在队首", off_order[0] != "k6" or off_order.index("k0") < off_order.index("k6"))
    pick1 = preferred_order(keys, history, key=lambda k: k, mode="lru", pick=1)
    check_true("pick=1 时退化为随机", sorted(pick1) == sorted(keys))
    check("空池子", preferred_order([], {}, key=lambda k: k), [])
    check("单个候选", preferred_order(["only"], {}, key=lambda k: k), ["only"])
    small = preferred_order(["a", "b"], {}, key=lambda k: k, mode="lru", pick=8)
    check("k 大于池子也不报错", sorted(small), ["a", "b"])

    print("\n== 3. 统计性质：与纯随机对比 ==")
    # 60 条内容、连续抽 120 次，同一随机种子下对比「最久未抽优先」与纯随机。
    # 不做绝对断言（避免偶发波动），而是要求它**明显优于**随机。
    def sequence(mode: str, seed: int = 11) -> list[str]:
        random.seed(seed)
        names = [f"d{i}" for i in range(60)]
        hist: dict[str, float] = {}
        out: list[str] = []
        for step in range(120):
            head = preferred_order(names, hist, key=lambda k: k, mode=mode, pick=5, head=3)
            picked = head[0]
            out.append(picked)
            hist[picked] = float(step)
        return out

    def repeats_within(seq: list[str], window: int) -> int:
        return sum(1 for i in range(window, len(seq)) if seq[i] in seq[i - window:i])

    lru_seq, uni_seq = sequence("lru"), sequence("off")
    print(f"     重复@20  最久未抽={repeats_within(lru_seq, 20)}  纯随机={repeats_within(uni_seq, 20)}")
    print(f"     重复@40  最久未抽={repeats_within(lru_seq, 40)}  纯随机={repeats_within(uni_seq, 40)}")
    check("最久未抽在近 20 次内没有重复", repeats_within(lru_seq, 20), 0)
    check_true(
        "近 40 次内的重复远少于纯随机",
        repeats_within(lru_seq, 40) * 3 < max(1, repeats_within(uni_seq, 40)),
    )
    windows = [len(set(lru_seq[i:i + 25])) for i in range(0, len(lru_seq) - 25, 5)]
    print(f"     25 次窗口内不同内容数（最差 {min(windows)}，平均 {sum(windows)/len(windows):.1f}）")
    check_true("任意 25 次窗口内至少 20 种不同内容", min(windows) >= 20)


async def check_history_query() -> None:
    print("\n== 4. draw_history 与保留期 ==")
    check("保留期跟 BOTTLE_HISTORY_DAYS 一致", ESSENCE_DRAW_RETENTION_DAYS, BOTTLE_HISTORY_DAYS)
    await clear()
    await record_draw(GROUP, "h1", day_offset(0), label="今天")
    await record_draw(GROUP, "h1", day_offset(3), label="三天前")
    await record_draw(GROUP, "h2", day_offset(1), label="昨天")
    history = await draw_history(GROUP)
    check("只按内容去重", sorted(history), ["h1", "h2"])
    check_true("取最近一次时间（今天晚于三天前）", history["h1"] > history["h2"])
    await record_draw(GROUP, "expired", day_offset(BOTTLE_HISTORY_DAYS + 5), label="过期")
    history = await draw_history(GROUP)
    check_true("超出保留期的记录不计入", "expired" not in history)
    await clear()


async def check_choose_uses_it() -> None:
    print("\n== 5. choose_essence / choose_album_photo 真的用了它 ==")
    await clear()
    await record_draw(GROUP, "e1", day_offset(1), label="昨天抽过")
    await record_draw(GROUP, "e2", day_offset(3), label="三天前抽过")
    await record_draw(GROUP, "album:p1", day_offset(1), label="昨天抽过的相册照片")
    await record_draw(GROUP, "album:p2", day_offset(4), label="四天前抽过的相册照片")
    day = local_day_key()

    items = [essence_item("e1"), essence_item("e2"), essence_item("e3")]
    picked = await choose_essence(SimpleNamespace(), GROUP, items, day=day)
    check("精华优先抽从未抽过的 e3", picked.item["message_id"], "e3")
    picked = await choose_essence(SimpleNamespace(), GROUP, items[:2], day=day)
    check("都抽过时优先最旧的 e2", picked.item["message_id"], "e2")

    photos = [album_photo("p1"), album_photo("p2"), album_photo("p3")]
    async def fake_materialize(photo, group_id, client):
        return TEMP / "fake.jpg"

    original = gb.materialize_photo
    gb_pool.materialize_photo = fake_materialize
    try:
        picked = await choose_album_photo(SimpleNamespace(), GROUP, photos, day=day)
        check("相册优先抽从未抽过的 p3", picked.item.media_id, "p3")
        picked = await choose_album_photo(SimpleNamespace(), GROUP, photos[:2], day=day)
        check("都抽过时优先最旧的 p2", picked.item.media_id, "p2")
    finally:
        gb_pool.materialize_photo = original

    print("\n== 6. 当天不重复仍然优先 ==")
    await record_draw(GROUP, "e3", day, label="今天已抽过")  # e3 是"最旧"但它今天已被抽过
    picked = await choose_essence(SimpleNamespace(), GROUP, items, day=day)
    check_true("今天抽过的不会被再抽", picked.item["message_id"] != "e3")
    check("退而抽剩下的最旧 e2", picked.item["message_id"], "e2")
    await clear()


async def main() -> None:
    await initialize_database()
    check_order_contract()
    await check_history_query()
    await check_choose_uses_it()

    print("\n== 7. 默认参数 ==")
    check_true("默认 K 在合理范围", 2 <= BOTTLE_REPEAT_PICK_K <= 50)
    print(f"     BOTTLE_REPEAT_PICK_K={BOTTLE_REPEAT_PICK_K} "
          f"BOTTLE_HISTORY_DAYS={BOTTLE_HISTORY_DAYS}")

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
