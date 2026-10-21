"""喝水提醒的回归测试：作息表、文案、开关持久化、每分钟只触发一次、私聊命令解析。"""

from __future__ import annotations

import tempfile

import asyncio
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = Path(tempfile.gettempdir()) / "recall_verify" / "verify_water.sqlite3"
for stale in DB.parent.glob("verify_water.sqlite3*"):
    stale.unlink()

import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001",
    authorized_users="100000009",
    database_path=str(DB),
)

from plugins.group_bot.core.config import GROUP_TIMEZONE  # noqa: E402
from plugins.group_bot.core.store import initialize_database  # noqa: E402
from plugins.group_bot.runtime import water  # noqa: E402

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


class FakeBot:
    def __init__(self) -> None:
        self.private: list[str] = []
        self.group: list[str] = []

    async def send_private_msg(self, **kwargs):
        self.private.append(str(kwargs["message"]))

    async def send_group_msg(self, **kwargs):
        self.group.append(str(kwargs["message"]))

    @property
    def self_id(self):
        return "100000000"


def at(hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(2026, 10, 19, hour, minute, tzinfo=GROUP_TIMEZONE)


def main() -> None:
    asyncio.run(initialize_database())

    print("=== 1. 作息表 ===")
    check("19 个时间点", len(water.SCHEDULE), 19)
    times = [time_text for time_text, _ in water.SCHEDULE]
    check("按时间升序且无重复", times, sorted(set(times)))
    check_true("全部是 HH:MM", all(re.fullmatch(r"\d{2}:\d{2}", t) for t in times))
    for expected in ("07:50", "08:30", "09:05", "10:00", "10:30", "10:50", "12:40",
                     "13:50", "14:00", "14:45", "15:30", "16:15", "17:00", "17:45",
                     "19:00", "20:00", "21:00", "22:00", "22:30"):
        check_true(f"含 {expected}", expected in times)

    print("\n=== 2. 类型映射 ===")
    check("07:50 起床", water.kind_at("07:50"), water.WAKE)
    check("08:30 上午", water.kind_at("08:30"), water.MORNING)
    check("10:30 上午", water.kind_at("10:30"), water.MORNING)
    check("12:40 午餐后", water.kind_at("12:40"), water.LUNCH)
    check("13:50 午休起来", water.kind_at("13:50"), water.NAP)
    check("14:45 上课", water.kind_at("14:45"), water.CLASS)
    check("17:45 上课", water.kind_at("17:45"), water.CLASS)
    check("19:00 晚餐后", water.kind_at("19:00"), water.DINNER)
    check("21:00 每小时", water.kind_at("21:00"), water.HOURLY)
    check("22:30 最后一杯", water.kind_at("22:30"), water.LAST)
    check("11:00 没有提醒", water.kind_at("11:00"), None)
    check("18:00 下课不提醒", water.kind_at("18:00"), None)

    print("\n=== 3. 文案 ===")
    for kind in (water.WAKE, water.MORNING, water.LUNCH, water.NAP,
                 water.CLASS, water.DINNER, water.HOURLY, water.LAST):
        check_true(f"{kind} 有文案且非空", bool(water.text_for(kind).strip()))
    check_true("起床文案与上午文案不同", water.TEXTS[water.WAKE] != water.TEXTS[water.MORNING])
    check_true("起床文案提到起床", "起床" in "".join(water.TEXTS[water.WAKE]))

    print("\n=== 4. 开关（持久化在 bot_settings）===")
    check("默认开启", asyncio.run(water.water_enabled()), True)
    check("关闭生效", asyncio.run(water.set_water(False)), True)
    check("关闭后读到关", asyncio.run(water.water_enabled()), False)
    check("重复关闭不报变化", asyncio.run(water.set_water(False)), False)
    check("重新打开", asyncio.run(water.set_water(True)), True)
    check("打开后读到开", asyncio.run(water.water_enabled()), True)

    print("\n=== 5. 同一分钟只触发一次 ===")
    water._last_fired = None
    bot = FakeBot()
    check("08:30 首次触发", asyncio.run(water.tick(bot, at("08:30"))), True)
    check("私聊 + 群都发了", (len(bot.private), len(bot.group)), (1, len(water.target_groups())))
    check("同一分钟不再触发", asyncio.run(water.tick(bot, at("08:30"))), False)
    check("非表内时间不触发", asyncio.run(water.tick(bot, at("08:31"))), False)
    check("下一节 08:31 之后的 09:05 触发", asyncio.run(water.tick(bot, at("09:05"))), True)
    water._last_fired = None
    asyncio.run(water.set_water(False))
    before = (len(bot.private), len(bot.group))
    check("关闭后不发送", asyncio.run(water.tick(bot, at("10:00"))), False)
    check("确实没有新消息", (len(bot.private), len(bot.group)), before)
    asyncio.run(water.set_water(True))

    print("\n=== 6. 私聊命令解析 ===")
    check("喝水提醒", water.parse_water_command("喝水提醒"), "status")
    check("喝水提醒 开", water.parse_water_command("喝水提醒 开"), "on")
    check("喝水提醒 关", water.parse_water_command("喝水提醒 关"), "off")
    check("喝水 关", water.parse_water_command("喝水 关"), "off")
    check("喝水提醒 开启", water.parse_water_command("喝水提醒 开启"), "on")
    check("喝水提醒 测试", water.parse_water_command("喝水提醒 测试"), "test")
    check("喝水提醒 test", water.parse_water_command("喝水提醒 test"), "test")
    check("不是这个命令", water.parse_water_command("漂流瓶"), None)
    check_true("主人 QQ 已配置", water.owner_user_id() is not None)
    check_true("至少一个群", len(water.target_groups()) >= 1)


    print("\n=== 7. 测试提醒（不受开关影响）===")
    await_result = asyncio.run(water.set_water(False))
    bot2 = FakeBot()
    sent, message = asyncio.run(water.send_test(bot2))
    check("关闭状态下也能发测试", sent, 1 + len(water.target_groups()))
    check("测试消息带【测试】前缀", message.startswith("【测试】"), True)
    check("私聊收到一条", len(bot2.private), 1)
    check("每个群收到一条", len(bot2.group), len(water.target_groups()))
    asyncio.run(water.set_water(True))
    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


main()
