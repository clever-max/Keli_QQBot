"""Checks for the opened-up recall permissions and the group-wide cooldown.

Behaviour under test:
  * 撤回记录 / !recalled  -> everyone in the group
  * 撤回记录 私聊         -> still admins only
  * clear                 -> owner only
  * one shared 120s window per group, owner exempt, "wait N" told once
  * the in-group help no longer advertises the super-admin section
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
DB = TEMP / "verify_permissions.sqlite3"
for stale in TEMP.glob("verify_permissions.sqlite3*"):
    stale.unlink()

SENT_AT = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
RECALLED_AT = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001,123450002",
    authorized_users="100000009",
    group_authorized_users="123450001:100000009,100000011;123450002:*",
    status_users="100000009",
    database_path=str(DB),
)

from nonebot.adapters.onebot.v11 import (  # noqa: E402
    GroupMessageEvent,
    PrivateMessageEvent,
)

from plugins.group_bot.recall import history as gb_recall  # noqa: E402
from plugins.group_bot.runtime.cooldown import (  # noqa: E402
    GROUP_RECALL_COOLDOWN_SECONDS,
    take_group_recall_slot,
)
from plugins.group_bot.handlers import (  # noqa: E402
    command_help,
    handle_group_message,
    handle_private_message,
)
from plugins.group_bot.core.settings import set_switch  # noqa: E402
from plugins.group_bot.core.settings import RECALL_PRIVATE_DELIVERY  # noqa: E402
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402

GROUP = 123450001          # explicit admin list
OPEN_GROUP = 123450002    # '*' = everyone in it
OWNER = 100000009         # super admin
GROUP_ADMIN = 100000011   # named admin of GROUP only
MEMBER = 666666666         # plain member, named nowhere

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


class StubBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def send_group_msg(self, **kwargs):
        self.calls.append(("send_group_msg", kwargs))
        return {"status": "ok", "data": {"message_id": len(self.calls)}}

    async def send_private_msg(self, **kwargs):
        self.calls.append(("send_private_msg", kwargs))
        return {"status": "ok", "data": {"message_id": len(self.calls)}}

    async def call_api(self, api: str, **kwargs):
        self.calls.append((f"call_api:{api}", kwargs))
        return {"status": "ok", "data": {"group_name": "验证群"}}

    @property
    def self_id(self) -> str:
        return "100000000"

    def texts(self) -> list[str]:
        return [kw["message"] for _, kw in self.calls if isinstance(kw.get("message"), str)]

    def sends(self) -> list[str]:
        return [n for n, _ in self.calls if n.startswith("send_")]

    def privates(self) -> list[dict]:
        return [kw for n, kw in self.calls if n == "send_private_msg"]


def group_event(text: str, user_id: int, group_id: int = GROUP, message_id: int = 1,
                mention: bool = True):
    message = []
    if mention:
        message.append({"type": "at", "data": {"qq": "100000000"}})
        # without_bot_mention() strips the leftover whitespace for @-commands.
        message.append({"type": "text", "data": {"text": f" {text}"}})
    else:
        # !recalled is matched on the exact concatenated text, so no padding here.
        message.append({"type": "text", "data": {"text": text}})
    return GroupMessageEvent(
        time=int(datetime.now(timezone.utc).timestamp()),
        self_id=100000000,
        post_type="message",
        message_type="group",
        sub_type="normal",
        message_id=message_id,
        user_id=user_id,
        group_id=group_id,
        message=message,
        raw_message=text,
        font=0,
        sender={"user_id": user_id, "nickname": "测试员", "card": ""},
        to_me=mention,
    )


def private_event(text: str, user_id: int, group_id: int | None = GROUP, message_id: int = 2):
    extra = {} if group_id is None else {"group_id": group_id}
    return PrivateMessageEvent(
        time=int(datetime.now(timezone.utc).timestamp()),
        self_id=100000000,
        post_type="message",
        message_type="private",
        sub_type="group" if group_id is not None else "friend",
        message_id=message_id,
        user_id=user_id,
        message=[{"type": "text", "data": {"text": text}}],
        raw_message=text,
        font=0,
        sender={"user_id": user_id, "nickname": "测试员"},
        **extra,
    )


async def add_recall(group_id: int, message_id: str, text: str) -> None:
    async with connect() as db:
        await db.execute(
            """INSERT OR REPLACE INTO messages
               (message_id, group_id, user_id, sender_name, message_json,
                plain_text, sent_at, recalled_at, recalled_by)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                message_id, str(group_id), str(MEMBER), "测试员",
                f'[{{"type": "text", "data": {{"text": "{text}"}}}}]',
                text, SENT_AT, RECALLED_AT, str(MEMBER),
            ),
        )
        await db.execute(
            """INSERT OR REPLACE INTO recall_events
               (group_id, message_id, recalled_at, summoned_at)
               VALUES (?,?,?,NULL)""",
            (str(group_id), message_id, RECALLED_AT),
        )
        await db.commit()


async def pending(group_id: int) -> int:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM recall_events WHERE group_id=? AND summoned_at IS NULL",
            (str(group_id),),
        )
        return int((await cursor.fetchone())["n"])


async def reset(group_id: int, count: int) -> None:
    """Rebuild `count` pending rows for the group and forget every cooldown."""
    async with connect() as db:
        await db.execute("DELETE FROM group_cooldowns")
        await db.execute("DELETE FROM mention_cooldowns")
        await db.execute("DELETE FROM recall_events WHERE group_id=?", (str(group_id),))
        await db.commit()
    for index in range(count):
        await add_recall(group_id, str(7000 + index), f"记录 {index}")


def stub_rendering() -> None:
    counter = {"n": 0}

    def fake_render(rows, group_id, group_name, page, pages):
        counter["n"] += 1
        target = TEMP / f"perm_{counter['n']}.png"
        target.write_bytes(b"fake-png")
        return target

    async def noop(*args, **kwargs):
        return None

    gb_recall.render_recall_page = fake_render
    gb_recall.cache_avatar = noop
    gb_recall.cache_message_images = noop
    gb_recall.cached_image_paths = lambda row: []


async def main() -> None:
    await initialize_database()
    stub_rendering()
    await set_switch(RECALL_PRIVATE_DELIVERY, True)

    print("\n== A. 撤回记录现在人人可用 ==")
    await reset(GROUP, 2)
    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录", MEMBER, message_id=10))
    check("A 普通成员拿到卡片", bot.sends(), ["send_group_msg"])
    check("A 记录已投递", await pending(GROUP), 0)

    print("\n== B. 全群共用 120s 冷却 ==")
    await reset(GROUP, 2)
    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录", MEMBER, message_id=11))
    check("B 第一个人查到了", await pending(GROUP), 0)

    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录", GROUP_ADMIN, message_id=12))
    check("B 第二个人被冷却挡住", bot.sends(), ["send_group_msg"])
    check("B 并且被明确告知要等", "还要等" in bot.texts()[0], True)
    check("B 提示里带上限", str(GROUP_RECALL_COOLDOWN_SECONDS) in bot.texts()[0], True)

    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录", MEMBER, message_id=13))
    check("B 后续尝试完全静默", bot.calls, [])

    print("\n== C. 主人不受这个冷却限制 ==")
    await reset(GROUP, 1)
    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录", MEMBER, message_id=14))
    check("C 成员先查（开始冷却）", await pending(GROUP), 0)
    await add_recall(GROUP, "8001", "给主人的一条")
    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录", OWNER, message_id=15))
    check("C 主人仍能查到", bot.sends(), ["send_group_msg"])
    check("C 主人这次也投递成功", await pending(GROUP), 0)

    print("\n== D. 冷却以群为单位，不跨群 ==")
    await reset(GROUP, 1)
    await add_recall(OPEN_GROUP, "8002", "另一个群的一条")
    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录", MEMBER, message_id=16))
    bot = StubBot()
    await handle_group_message(
        bot, group_event("撤回记录", MEMBER, group_id=OPEN_GROUP, message_id=17)
    )
    check("D 另一个群不受影响", bot.sends(), ["send_group_msg"])

    print("\n== E. !recalled 也放开且计入同一个冷却 ==")
    await reset(GROUP, 1)
    bot = StubBot()
    await handle_group_message(
        bot, group_event("!recalled", MEMBER, message_id=18, mention=False)
    )
    check("E 普通成员可用", bot.sends(), ["send_group_msg"])
    await add_recall(GROUP, "8003", "又一条")
    bot = StubBot()
    await handle_group_message(
        bot, group_event("!recalled", MEMBER, message_id=19, mention=False)
    )
    check("E 共享同一个冷却", "还要等" in bot.texts()[0], True)

    print("\n== F. 「私聊」变体仍限管理员 ==")
    await reset(GROUP, 1)
    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录 私聊", MEMBER, message_id=20))
    check("F 普通成员被拒", "管理员的小工具" in bot.texts()[0], True)
    check("F 没有私聊发送", bot.privates(), [])
    check("F 记录未被消耗", await pending(GROUP), 1)
    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录", MEMBER, message_id=21))
    check("F 被拒的这次没占用全群冷却", bot.sends(), ["send_group_msg"])

    print("\n== G. 「全部」仍限超级管理员，且不占用冷却 ==")
    await reset(GROUP, 1)
    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录 全部", MEMBER, message_id=22))
    check("G 普通成员被拒", "钥匙" in bot.texts()[0], True)
    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录", MEMBER, message_id=23))
    check("G 冷却没被这次占用", bot.sends(), ["send_group_msg"])

    print("\n== H. clear 只有主人能用 ==")
    await reset(GROUP, 3)
    bot = StubBot()
    await handle_group_message(bot, group_event("clear", MEMBER, message_id=24))
    check("H 普通成员被拒", "钥匙" in bot.texts()[0], True)
    check("H 记录没被清掉", await pending(GROUP), 3)

    await reset(GROUP, 3)
    bot = StubBot()
    await handle_group_message(bot, group_event("clear", GROUP_ADMIN, message_id=25))
    check("H 群管理员也被拒", "钥匙" in bot.texts()[0], True)
    check("H 记录没被清掉", await pending(GROUP), 3)

    await reset(GROUP, 3)
    bot = StubBot()
    await handle_group_message(bot, group_event("clear", OWNER, message_id=26))
    check("H 主人可以清", await pending(GROUP), 0)

    print("\n== I. clear 在私聊里同样只有主人能用 ==")
    await reset(GROUP, 3)
    bot = StubBot()
    await handle_private_message(bot, private_event("clear", GROUP_ADMIN))
    check("I 群管理员在私聊也被拒", "钥匙" in bot.texts()[0], True)
    check("I 记录没被清掉", await pending(GROUP), 3)
    bot = StubBot()
    await handle_private_message(bot, private_event("clear", OWNER))
    check("I 主人可以清", await pending(GROUP), 0)

    print("\n== J. help 里不再出现超级管理员段落 ==")
    plain = command_help(is_admin=False)
    admin = command_help(is_admin=True)
    check("J 公开 help 无超级管理员", "超级管理员" in plain, False)
    check("J 管理员 help 无超级管理员", "超级管理员" in admin, False)
    check("J 管理员 help 仍分两块", "【管理员】" in admin and "【大家都能用】" in admin, True)
    check("J help 里也不列 clear", "clear" in admin, False)
    check("J help 里也不列 全部", "全部" in admin, False)
    check("J help 说明全群冷却", "120" in admin, True)
    check("J help 公开块含 撤回记录", "撤回记录" in plain, True)

    print("\n== K. 跨群点名需要在该群名单里（* 不算）==")
    await reset(GROUP, 1)
    bot = StubBot()
    # MEMBER 不在 123450002 的名单里被点名，虽然那个群是 '*'。
    await handle_group_message(
        bot, group_event(f"撤回记录 {OPEN_GROUP}", MEMBER, message_id=27)
    )
    check("K 普通成员跨群被拒", "别的群" in bot.texts()[0], True)
    bot = StubBot()
    # GROUP_ADMIN 被点名在 123450001 的名单里，从 123450002 反查它是允许的。
    await handle_group_message(
        bot,
        group_event("撤回记录 123450001", GROUP_ADMIN, group_id=OPEN_GROUP, message_id=28),
    )
    check("K 被点名的管理员可以跨群", bot.sends(), ["send_group_msg"])

    print("\n== L. 冷却单元行为：过期后重新可用 ==")
    async with connect() as db:
        await db.execute("DELETE FROM group_cooldowns")
        await db.commit()
    allowed, remaining, first = await take_group_recall_slot(GROUP)
    check("L 首次允许", (allowed, remaining, first), (True, 0, False))
    allowed, remaining, first = await take_group_recall_slot(GROUP)
    check("L 第二次挡住", allowed, False)
    check("L 剩余时间接近 120", remaining > 115 and remaining <= 120, True)
    check("L 第一次挡住会提示", first, True)
    allowed, remaining, first = await take_group_recall_slot(GROUP)
    check("L 之后不再提示", first, False)
    async with connect() as db:
        await db.execute("UPDATE group_cooldowns SET cooldown_until=0 WHERE group_id=?", (str(GROUP),))
        await db.commit()
    allowed, remaining, first = await take_group_recall_slot(GROUP)
    check("L 过期后重新允许", (allowed, first), (True, False))

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
