"""分群隔离与分群功能开关的回归测试。

锁住三件事：
1. 新增群只拿到自己的授权名单：别的群的 `*` 不会漏进来，陌生人在新群仍然被拒；
2. 喝水提醒按 ``WATER_EXCLUDED_GROUP_IDS`` 排除该群，其它群照旧；
3. status 完全不计入该群（所有按群聚合的查询都要过滤），并且在该群发 `status` 静默。
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import asyncio  # noqa: E402
import sys  # noqa: E402
from datetime import datetime, timezone
from pathlib import Path  # noqa: E402

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
TEMP.mkdir(parents=True, exist_ok=True)
DB = TEMP / "verify_group_scope.sqlite3"
for stale in TEMP.glob("verify_group_scope.sqlite3*"):
    stale.unlink()

import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001,123450002,123450003",
    authorized_users="100000009",
    group_authorized_users=(
        "123450001:100000009,100000011;"
        "123450002:*;"
        "123450003:100000009,100000011"
    ),
    water_excluded_group_ids="123450003",
    status_excluded_group_ids="123450003",
    bottle_only_help_group_ids="123450003",
    bottle_only_group_ids="123450003",
    database_path=str(DB),
)

from plugins.group_bot import handlers  # noqa: E402
from plugins.group_bot.core import config as gb_config  # noqa: E402
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402
from plugins.group_bot.runtime import status, water  # noqa: E402
from nonebot.adapters.onebot.v11 import GroupMessageEvent  # noqa: E402

GROUP = 123450001
OPEN_GROUP = 123450002
NEW_GROUP = 123450003
STRANGER = 123456789
failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


print("== 1. 群与授权 ==")
check("三个群都在白名单", gb_config.ALLOWED_GROUP_IDS, {GROUP, OPEN_GROUP, NEW_GROUP})
check(
    "新群授权与老群一致",
    gb_config.GROUP_AUTHORIZED_USERS[NEW_GROUP],
    {"100000009", "100000011"},
)
check_true("新群允许名单内的人", gb_config.is_authorized(NEW_GROUP, 100000011))
check_true("新群拒绝名单外的人", not gb_config.is_authorized(NEW_GROUP, STRANGER))
check_true("开放群的 * 不串到新群", not gb_config.is_authorized(NEW_GROUP, "999999"))
check_true("开放群本身仍然全开", gb_config.is_authorized(OPEN_GROUP, STRANGER))
check_true("新群不是全员开放", not gb_config.group_allows_everyone(NEW_GROUP))
check_true("跨群点名按目标群判定", gb_config.is_named_in_group(NEW_GROUP, 100000009))
check_true("陌生人不在新群名单", not gb_config.is_named_in_group(NEW_GROUP, STRANGER))

print("\n== 2. 喝水提醒排除新群 ==")
check("只给未排除的群发", water.target_groups(), [GROUP, OPEN_GROUP])
check("新群被排除", NEW_GROUP not in water.target_groups(), True)

print("\n== 3. status 排除新群 ==")
check("新群 status 关闭", status.status_enabled_for(NEW_GROUP), False)
check("老群 status 正常", status.status_enabled_for(GROUP), True)

NOW = datetime.now(timezone.utc).isoformat()
TODAY = datetime.now().astimezone().date().isoformat()


async def seed() -> None:
    await initialize_database()
    async with connect() as db:
        for group_id, message_id in (
            (GROUP, "a1"),
            (GROUP, "a2"),
            (OPEN_GROUP, "c1"),
            (NEW_GROUP, "b1"),
            (NEW_GROUP, "b2"),
            (NEW_GROUP, "b3"),
        ):
            await db.execute(
                """INSERT INTO messages
                   (group_id, message_id, user_id, sender_name, message_json, plain_text, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(group_id), message_id, "1", "甲", "[]", "hi", NOW),
            )
        await db.execute(
            """INSERT INTO recall_events (group_id, message_id, recalled_at)
               VALUES (?, 'b1', ?)""",
            (str(NEW_GROUP), NOW),
        )
        await db.execute(
            """INSERT INTO essence_draws (group_id, day, message_id, drawn_at)
               VALUES (?, ?, 'b1', ?)""",
            (str(NEW_GROUP), TODAY, NOW),
        )
        await db.execute(
            """INSERT INTO summary_runs
               (group_id, day, covered_day, state, summary_text, closing_text, created_at)
               VALUES (?, ?, ?, 'answered', 'x', 'y', ?)""",
            (str(NEW_GROUP), TODAY, TODAY, NOW),
        )
        await db.execute(
            """INSERT INTO essence_usage (group_id, user_id, day, used)
               VALUES (?, '1', ?, 1)""",
            (str(NEW_GROUP), TODAY),
        )
        await db.commit()


asyncio.run(seed())


async def read_status() -> dict:
    return await status.collect()


data = asyncio.run(read_status())
check("总消息不含新群", data["messages_total"], 3)
check("今日消息不含新群", data["messages_today"], 3)
check_true("按群统计里没有新群", str(NEW_GROUP) not in data["today_by_group"])
check_true("未召回统计里没有新群", str(NEW_GROUP) not in data["recall_pending"])
check_true("昨日总结统计里没有新群", str(NEW_GROUP) not in data["summary"])
check_true("漂流瓶统计里没有新群", str(NEW_GROUP) not in data["essence_draws"])
check("今日漂流瓶人数不含新群", data["essence_users_today"], 0)

print("\n== 4. 新群 help 只讲漂流瓶 ==")
check("只给新群开精简 help", gb_config.BOTTLE_ONLY_HELP_GROUP_IDS, {NEW_GROUP})
bottle_help = handlers.group_help(NEW_GROUP, is_admin=True)
check_true("help 里有漂流瓶", "漂流瓶" in bottle_help)
check_true("help 不提管理员", "管理员" not in bottle_help)
check_true("help 不提撤回记录", "撤回记录" not in bottle_help)
check_true("help 不提昨日总结", "昨日" not in bottle_help and "总结" not in bottle_help)
check_true("help 不提私聊投递", "私聊" not in bottle_help)
full_help = handlers.group_help(GROUP, is_admin=True)
check_true("老群帮助仍有管理员段", "【管理员】" in full_help)

print("\n== 5. 新群只开放漂流瓶系列 ==")
check("只给新群开 bottle-only", gb_config.BOTTLE_ONLY_GROUP_IDS, {NEW_GROUP})
for command in ("漂流瓶", "漂流瓶 相册", "丢漂流瓶榜", "被抽到榜", "help", "帮助"):
    check_true(
        f"{command} 放行",
        handlers.bottle_only_allowed(command, command.casefold()),
    )
for command in (
    "撤回记录",
    "撤回记录 历史",
    "!recalled",
    "status",
    "昨日总结",
    "炸乌云 让风告诉你",
    "私聊投递",
    "clear",
):
    check(
        f"{command} 被挡",
        handlers.bottle_only_allowed(command, command.casefold()),
        False,
    )
check_true("老群不受 bottle-only 影响", GROUP not in gb_config.BOTTLE_ONLY_GROUP_IDS)

print("\n== 6. 新群 handler 层只放行漂流瓶 ==")


class GateBot:
    self_id = "100000000"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send_group_msg(self, **kwargs):
        self.calls.append("send_group_msg")
        return {"status": "ok"}

    async def send_private_msg(self, **kwargs):
        self.calls.append("send_private_msg")
        return {"status": "ok"}

    async def call_api(self, *args, **kwargs):
        return {}


def mentioned(text: str):
    return GroupMessageEvent(
        time=int(datetime.now(timezone.utc).timestamp()),
        self_id=100000000,
        post_type="message",
        message_type="group",
        sub_type="normal",
        message_id=300,
        user_id=100000009,
        group_id=NEW_GROUP,
        message=[
            {"type": "at", "data": {"qq": "100000000"}},
            {"type": "text", "data": {"text": f" {text}"}},
        ],
        raw_message=f"[CQ:at,qq=100000000] {text}",
        font=0,
        sender={"user_id": 100000009, "nickname": "测试员", "card": ""},
        to_me=True,
    )


async def no_save(*args, **kwargs):
    return None


bottle_calls: list[str] = []


async def fake_essence(bot, event, *, album_only=False):
    bottle_calls.append("draw")


handlers.save_message = no_save
handlers.safe_send_essence = fake_essence
bot = GateBot()
asyncio.run(handlers.handle_group_message(bot, mentioned("撤回记录")))
check("撤回记录被挡", (bot.calls, bottle_calls), ([], []))
asyncio.run(handlers.handle_group_message(bot, mentioned("status")))
check("status 被挡", (bot.calls, bottle_calls), ([], []))
asyncio.run(handlers.handle_group_message(bot, mentioned("炸乌云 让风告诉你")))
check("点歌被挡", (bot.calls, bottle_calls), ([], []))
asyncio.run(handlers.handle_group_message(bot, mentioned("漂流瓶")))
check("漂流瓶放行", bottle_calls, ["draw"])

print("\n" + "=" * 60)
if failures:
    print(f"失败 {len(failures)} 项：")
    for item in failures:
        print(" -", item)
    sys.exit(1)
print("全部通过")
