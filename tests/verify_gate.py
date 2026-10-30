"""Drives the real handlers with the 私聊投递 switch to check its gating.

Handlers are plain coroutines, so they can be awaited directly with a stub bot
and constructed OneBot events.  Covers the asymmetry that matters: a group may
only close the switch, and closing it must never lock the owner out.
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
DB = TEMP / "verify_gate.sqlite3"
for stale in TEMP.glob("verify_gate.sqlite3*"):
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
from plugins.group_bot.handlers import (  # noqa: E402
    handle_group_message,
    handle_private_message,
)
from plugins.group_bot.core.settings import (  # noqa: E402
    RECALL_PRIVATE_DELIVERY,
    is_switch_on,
    set_switch,
)
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402

GROUP = 123450001
OTHER = 123450002
OWNER = 100000009
GROUP_ADMIN = 100000011      # 123450001 的群级管理员，不是超级管理员
STRANGER = 123456789

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
        out = []
        for _, kw in self.calls:
            msg = kw.get("message")
            if isinstance(msg, str):
                out.append(msg)
        return out

    def sent_private(self) -> list[dict]:
        return [kw for name, kw in self.calls if name == "send_private_msg"]


def group_event(text: str, user_id: int, group_id: int = GROUP, message_id: int = 1):
    # Faithful shape: the mention is a real `at` segment, which
    # without_bot_mention() then strips before the text is parsed.
    return GroupMessageEvent(
        time=int(datetime.now(timezone.utc).timestamp()),
        self_id=100000000,
        post_type="message",
        message_type="group",
        sub_type="normal",
        message_id=message_id,
        user_id=user_id,
        group_id=group_id,
        message=[
            {"type": "at", "data": {"qq": "100000000"}},
            {"type": "text", "data": {"text": f" {text}"}},
        ],
        raw_message=f"[CQ:at,qq=100000000] {text}",
        font=0,
        sender={"user_id": user_id, "nickname": "测试员", "card": ""},
        to_me=True,
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
                message_id,
                str(group_id),
                str(GROUP_ADMIN),
                "测试员",
                f'[{{"type": "text", "data": {{"text": "{text}"}}}}]',
                text,
                SENT_AT,
                RECALLED_AT,
                str(GROUP_ADMIN),
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


def stub_rendering() -> None:
    counter = {"n": 0}

    def fake_render(rows, group_id, group_name, page, pages):
        counter["n"] += 1
        target = TEMP / f"gate_{counter['n']}.png"
        target.write_bytes(b"fake-png")
        return target

    async def noop(*args, **kwargs):
        return None

    gb_recall.render_recall_page = fake_render
    gb_recall.cache_avatar = noop
    gb_recall.cache_message_images = noop
    gb_recall.cached_image_paths = lambda row: []


async def reset_pending(group_id: int) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE recall_events SET summoned_at=NULL WHERE group_id=?", (str(group_id),)
        )
        await db.commit()


async def main() -> None:
    await initialize_database()
    stub_rendering()
    for index in range(3):
        await add_recall(GROUP, str(3000 + index), f"第 {index} 条")

    print("\n== A. 群里不能打开，只能关 ==")
    await set_switch(RECALL_PRIVATE_DELIVERY, False)
    bot = StubBot()
    await handle_group_message(bot, group_event("私聊投递 开", OWNER, message_id=10))
    check("A 群里开被拒绝后开关仍是关", await is_switch_on(RECALL_PRIVATE_DELIVERY), False)
    check("A 只回了一条群消息", len(bot.calls), 1)
    check("A 回的是“群里只能关”", "只能关" in bot.texts()[0], True)

    bot = StubBot()
    await handle_group_message(bot, group_event("私聊投递 关", OWNER, message_id=11))
    check("A 群管理员可以关（本来就是关）", "本来就是关着" in bot.texts()[0], True)

    print("\n== B. 群管理员（非超级管理员）也能在群里关 ==")
    await set_switch(RECALL_PRIVATE_DELIVERY, True)
    bot = StubBot()
    await handle_group_message(
        bot, group_event("私聊投递 关", GROUP_ADMIN, message_id=12)
    )
    check("B 关闭生效", await is_switch_on(RECALL_PRIVATE_DELIVERY), False)
    check("B 回的是“关掉啦”", "关掉啦" in bot.texts()[0], True)

    print("\n== C. 群管理员在群里也开不了 ==")
    bot = StubBot()
    await handle_group_message(
        bot, group_event("私聊投递 开", GROUP_ADMIN, message_id=13)
    )
    check("C 仍是关", await is_switch_on(RECALL_PRIVATE_DELIVERY), False)
    check("C 被拒绝", "只能关" in bot.texts()[0], True)

    print("\n== D. 非管理员在群里碰开关 ==")
    bot = StubBot()
    await handle_group_message(bot, group_event("私聊投递 关", STRANGER, message_id=14))
    check("D 仍保持原状（上一次是关）", await is_switch_on(RECALL_PRIVATE_DELIVERY), False)
    check("D 回的是管理员专属说明", "管理员的小工具" in bot.texts()[0], True)

    print("\n== E. 关着时群里要求私聊投递：拒绝，且绝不回落进群 ==")
    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录 私聊", OWNER, message_id=15))
    check("E 没有任何私聊发送", bot.sent_private(), [])
    check("E 只有一条群提示", [n for n, _ in bot.calls], ["send_group_msg"])
    check("E 提示说明已关闭", "现在是关着的" in bot.texts()[0], True)
    check("E 记录仍是未召回（没有泄漏也没有消耗）", await pending(GROUP), 3)

    print("\n== F. 关着不影响群内普通撤回记录 ==")
    bot = StubBot()
    await handle_group_message(bot, group_event("撤回记录", OWNER, message_id=16))
    check("F 仍发到群里", {n for n, _ in bot.calls if n.startswith("send_")}, {"send_group_msg"})
    check("F 三条都投递了", await pending(GROUP), 0)

    print("\n== G. 私聊里主人可以重新打开 ==")
    await reset_pending(GROUP)
    bot = StubBot()
    await handle_private_message(bot, private_event("私聊投递 开", OWNER))
    check("G 开关被打开", await is_switch_on(RECALL_PRIVATE_DELIVERY), True)
    check("G 回复走私聊", [n for n, _ in bot.calls], ["send_private_msg"])
    check("G 回复说明已打开", "打开啦" in bot.texts()[0], True)

    print("\n== H. 非超级管理员的群管理员不能从私聊打开 ==")
    await set_switch(RECALL_PRIVATE_DELIVERY, False)
    bot = StubBot()
    await handle_private_message(bot, private_event("私聊投递 开", GROUP_ADMIN))
    check("H 仍是关", await is_switch_on(RECALL_PRIVATE_DELIVERY), False)
    check("H 被拒绝", "钥匙" in bot.texts()[0], True)

    print("\n== I. 关着时私聊命令入口只回提示，不发记录 ==")
    bot = StubBot()
    await handle_private_message(bot, private_event("撤回记录", OWNER))
    check("I 只有一条私聊消息", [n for n, _ in bot.calls], ["send_private_msg"])
    check("I 提示说明已关闭", "现在是关着的" in bot.texts()[0], True)
    check("I 记录仍是未召回", await pending(GROUP), 3)

    print("\n== J. 关着时私聊仍能查状态与看帮助 ==")
    bot = StubBot()
    await handle_private_message(bot, private_event("私聊投递", OWNER))
    check("J 状态查询可用（说的是关）", "关着" in bot.texts()[0], True)
    bot = StubBot()
    await handle_private_message(bot, private_event("help", OWNER))
    check("J 帮助可用且提到开关", "私聊投递" in bot.texts()[0], True)

    print("\n== K. 关着时非管理员私聊依旧完全静默 ==")
    bot = StubBot()
    await handle_private_message(bot, private_event("私聊投递 开", STRANGER))
    await handle_private_message(bot, private_event("撤回记录", STRANGER))
    check("K 一个字都没回", bot.calls, [])

    print("\n== L. 打开后私聊入口恢复工作 ==")
    await set_switch(RECALL_PRIVATE_DELIVERY, True)
    bot = StubBot()
    await handle_private_message(bot, private_event("撤回记录 20", OWNER))
    check("L 走的是私聊投递", {n for n, _ in bot.calls if n.startswith("send_")}, {"send_private_msg"})
    check("L 带上群号以支持临时会话", bot.calls[0][1].get("group_id"), GROUP)
    check("L 记录被标记为已召回", await pending(GROUP), 0)

    print("\n== M. 群里私聊投递打开后可用，且群里只留回执 ==")
    await reset_pending(OTHER)
    await add_recall(OTHER, "4001", "另一个群的一条")
    bot = StubBot()
    await handle_group_message(
        bot, group_event("撤回记录 私聊", OWNER, group_id=OTHER, message_id=17)
    )
    names = [n for n, _ in bot.calls]
    check("M 私聊投递了一张卡", names.count("send_private_msg"), 1)
    check("M 群里只有一条回执", names.count("send_group_msg"), 1)
    check("M 回执在群里且带条数", "悄悄发到你的私聊" in bot.texts()[0], True)

    print("\n== N. 普通私聊里显式群号可查任一授权群 ==")
    await reset_pending(OTHER)
    await add_recall(OTHER, "4002", "另一个群的一条")
    bot = StubBot()
    await handle_private_message(
        bot, private_event(f"撤回记录 {OTHER}", OWNER, group_id=None)
    )
    check("N 只走私聊", {n for n, _ in bot.calls if n.startswith("send_")}, {"send_private_msg"})
    check("N 带的是目标群", bot.calls[0][1].get("group_id"), OTHER)
    check("N 目标群的记录被标记", await pending(OTHER), 0)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
