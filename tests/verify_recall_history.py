"""私聊主人「撤回记录 历史」的完整性与权限回归。

锁住：跨群、包含已经看过的撤回、不标记未读；只有主人、只有私聊能触发。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
TEMP.mkdir(parents=True, exist_ok=True)
DB = TEMP / "verify_recall_history.sqlite3"
for stale in TEMP.glob("verify_recall_history.sqlite3*"):
    stale.unlink(missing_ok=True)

import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001,123450002",
    authorized_users="100000009",
    group_authorized_users="123450001:100000009,100000011;123450002:*",
    database_path=str(DB),
    archive_enabled=False,
)

from nonebot.adapters.onebot.v11 import (  # noqa: E402
    GroupMessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)

from plugins.group_bot import handlers  # noqa: E402
from plugins.group_bot.core.settings import (  # noqa: E402
    RECALL_PRIVATE_DELIVERY,
    set_switch,
)
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402
from plugins.group_bot.recall import history as recall_history  # noqa: E402
from plugins.group_bot.recall.history import (  # noqa: E402
    RecallRequest,
    send_all_recall_history,
)

GROUP = 123450001
OTHER = 123450002
OWNER = 100000009
GROUP_ADMIN = 100000011
NOW = datetime.now(timezone.utc).isoformat()
failures: list[str] = []
rendered: list[tuple[int, int]] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


class Bot:
    def __init__(self) -> None:
        self.messages: list[object] = []

    async def send_private_msg(self, **kwargs):
        self.messages.append(kwargs.get("message"))
        return {"status": "ok"}

    async def send_group_msg(self, **kwargs):
        self.messages.append(kwargs.get("message"))
        return {"status": "ok"}

    async def call_api(self, *args, **kwargs):
        return {}

    @property
    def self_id(self) -> str:
        return "100000000"


async def add_recall(
    group_id: int, message_id: str, text: str, *, summoned: bool = False
) -> None:
    async with connect() as db:
        await db.execute(
            """INSERT OR REPLACE INTO messages
               (message_id, group_id, user_id, sender_name, message_json,
                plain_text, sent_at, recalled_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                message_id,
                str(group_id),
                str(OWNER),
                "甲",
                json.dumps([{"type": "text", "data": {"text": text}}]),
                text,
                NOW,
                NOW,
            ),
        )
        await db.execute(
            """INSERT OR REPLACE INTO recall_events
               (group_id, message_id, recalled_at, summoned_at)
               VALUES (?,?,?,?)""",
            (str(group_id), message_id, NOW, NOW if summoned else None),
        )
        await db.commit()


async def summoned_state(message_id: str) -> str | None:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT summoned_at FROM recall_events WHERE message_id=?", (message_id,)
        )
        row = await cursor.fetchone()
        return row["summoned_at"] if row else None


def fake_render(rows, group_id, group_name, page, pages):
    rendered.append((group_id, len(rows)))
    path = TEMP / f"history-{group_id}-{page}.png"
    path.write_bytes(b"png")
    return path


async def no_io(*args, **kwargs):
    return None


def private_event(text: str, user_id: int, *, group_id: int | None = None):
    extra = {} if group_id is None else {"group_id": group_id}
    return PrivateMessageEvent(
        time=int(datetime.now(timezone.utc).timestamp()),
        self_id=100000000,
        post_type="message",
        message_type="private",
        sub_type="group" if group_id is not None else "friend",
        message_id=200,
        user_id=user_id,
        message=[{"type": "text", "data": {"text": text}}],
        raw_message=text,
        font=0,
        sender={"user_id": user_id, "nickname": "测试员"},
        **extra,
    )


def group_event(text: str, user_id: int):
    return GroupMessageEvent(
        time=int(datetime.now(timezone.utc).timestamp()),
        self_id=100000000,
        post_type="message",
        message_type="group",
        sub_type="normal",
        message_id=201,
        user_id=user_id,
        group_id=GROUP,
        message=[
            {"type": "at", "data": {"qq": "100000000"}},
            {"type": "text", "data": {"text": f" {text}"}},
        ],
        raw_message=f"[CQ:at,qq=100000000] {text}",
        font=0,
        sender={"user_id": user_id, "nickname": "测试员", "card": ""},
        to_me=True,
    )


async def main() -> None:
    await initialize_database()
    await set_switch(RECALL_PRIVATE_DELIVERY, True)

    await add_recall(GROUP, "h1", "群一未看")
    await add_recall(GROUP, "h2", "群一看过", summoned=True)
    await add_recall(OTHER, "h3", "群二未看")

    recall_history.render_recall_page = fake_render
    recall_history.cache_avatar = no_io
    recall_history.cache_message_images = no_io
    recall_history.cached_image_paths = lambda row: []
    recall_history.group_name_for = no_io

    request = RecallRequest(group_id=GROUP, user_id=OWNER, private=True, from_group=False)
    bot = Bot()
    total = await send_all_recall_history(bot, request, None, groups=[GROUP, OTHER])
    print("\n== 1. 跨群 + 包含已看过的 ==")
    check("共三条", total, 3)
    check("两个群各一张", sorted(rendered), [(GROUP, 2), (OTHER, 1)])
    check("两条私聊卡片", len(bot.messages), 2)
    check("群一未看仍然未看", await summoned_state("h1"), None)
    check("群二未看仍然未看", await summoned_state("h3"), None)

    print("\n== 2. 条数限制 ==")
    bot = Bot()
    rendered.clear()
    total = await send_all_recall_history(bot, request, 1, groups=[GROUP, OTHER])
    check("只发一条", total, 1)
    check("只渲染第一个群", rendered, [(GROUP, 1)])

    print("\n== 3. 主人私聊可触发 ==")
    calls: list[tuple[int, tuple[int, ...], int | None]] = []

    async def fake_all(bot, request, count, *, groups):
        calls.append((request.user_id, tuple(groups), count))
        return len(groups)

    handlers.safe_send_all_recall_history = fake_all
    bot = Bot()
    await handlers.handle_private_message(bot, private_event("撤回记录 历史", OWNER))
    check("调用一次", len(calls), 1)
    check("默认全部群", calls[0][1], (GROUP, OTHER))
    check("没有私聊回复", bot.messages, [])

    print("\n== 4. 非主人被拒 ==")
    calls.clear()
    bot = Bot()
    await handlers.handle_private_message(
        bot, private_event("撤回记录 历史", GROUP_ADMIN)
    )
    check("没有调用历史", calls, [])
    check("回了一条私聊", len(bot.messages), 1)
    check_true("说明只有主人", "主人" in str(bot.messages[0]))

    print("\n== 5. 群里完全不触发 ==")
    calls.clear()
    bot = Bot()

    async def no_save(*args, **kwargs):
        return None

    handlers.save_message = no_save
    await handlers.handle_group_message(bot, group_event("撤回记录 历史", OWNER))
    check("没有调用历史", calls, [])
    check("群里没有回复", bot.messages, [])

    print("\n" + "=" * 60)
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print(" -", item)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
