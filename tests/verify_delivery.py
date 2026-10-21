"""End-to-end check of the recall delivery path against a throwaway database.

Exercises send_recall_history for the three routing cases (group, group+private,
private-issued) with the renderer and media cache stubbed out, then asserts both
the OneBot calls made and the summoned_at bookkeeping in SQLite.
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
DB = TEMP / "verify.sqlite3"
for stale in TEMP.glob("verify.sqlite3*"):
    stale.unlink()

# Inside the default RECALL_WINDOW_MINUTES, so the window filter keeps them.
SENT_AT = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
RECALLED_AT = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
TEXT_JSON = json.dumps

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001,123450002",
    authorized_users="100000009",
    group_authorized_users="123450001:100000009;123450002:*",
    database_path=str(DB),
)

from plugins.group_bot.recall import history as gb_recall  # noqa: E402
from plugins.group_bot.core.config import DB_PATH  # noqa: E402
from plugins.group_bot.recall.history import RecallRequest, send_recall_history  # noqa: E402
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


class StubBot:
    """Records every OneBot call instead of talking to NapCat."""

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


GROUP = 123450001
OTHER = 123450002
ADMIN = 100000009


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
                "100000011",
                "测试员",
                TEXT_JSON([{"type": "text", "data": {"text": text}}]),
                text,
                SENT_AT,
                RECALLED_AT,
                "100000011",
            ),
        )
        await db.execute(
            """INSERT OR REPLACE INTO recall_events
               (group_id, message_id, recalled_at, summoned_at)
               VALUES (?,?,?,NULL)""",
            (str(group_id), message_id, RECALLED_AT),
        )
        await db.commit()


async def seed() -> None:
    await initialize_database()
    for index in range(5):
        await add_recall(GROUP, str(1000 + index), f"第 {index} 条")
    await add_recall(OTHER, "2001", "另一个群")


async def pending(group_id: int) -> int:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM recall_events WHERE group_id=? AND summoned_at IS NULL",
            (str(group_id),),
        )
        return int((await cursor.fetchone())["n"])


def stub_rendering() -> None:
    """Replace the picture pipeline with a throwaway file per page."""

    counter = {"n": 0}

    def fake_render(rows, group_id, group_name, page, pages):
        counter["n"] += 1
        target = TEMP / f"card_{counter['n']}.png"
        target.write_bytes(b"fake-png")
        return target

    async def fake_cache_avatar(*args, **kwargs):
        return None

    async def fake_cache_message_images(*args, **kwargs):
        return None

    def fake_cached_image_paths(row):
        return []

    gb_recall.render_recall_page = fake_render
    gb_recall.cache_avatar = fake_cache_avatar
    gb_recall.cache_message_images = fake_cache_message_images
    gb_recall.cached_image_paths = fake_cached_image_paths


async def main() -> None:
    print(f"     DB_PATH = {DB_PATH}")
    check("temp database in use", DB_PATH, DB)
    await seed()
    stub_rendering()

    print("\n== A. 群里发的普通撤回记录（沿用原行为）==")
    bot = StubBot()
    delivered = await send_recall_history(
        bot, RecallRequest(group_id=GROUP, user_id=ADMIN, group_name="验证群"), None
    )
    check("A delivered count", delivered, 5)
    check("A all calls are send_group_msg", {n for n, _ in bot.calls}, {"send_group_msg"})
    check("A target group", {kw["group_id"] for _, kw in bot.calls}, {GROUP})
    check("A group has no pending left", await pending(GROUP), 0)
    check("A other group untouched", await pending(OTHER), 1)

    print("\n== B. 空的撤回记录队列 ==")
    bot = StubBot()
    delivered = await send_recall_history(
        bot, RecallRequest(group_id=GROUP, user_id=ADMIN, group_name="验证群"), None
    )
    check("B delivered count", delivered, 0)
    check("B one notice to the group", [n for n, _ in bot.calls], ["send_group_msg"])
    check("B notice is plain text", isinstance(bot.calls[0][1]["message"], str), True)

    print("\n== C. 群里发「撤回记录 私聊」=> 图片走私聊 ==")
    bot = StubBot()
    delivered = await send_recall_history(
        bot,
        RecallRequest(
            group_id=OTHER, user_id=ADMIN, private=True, from_group=True, group_name="验证群"
        ),
        None,
    )
    check("C delivered count", delivered, 1)
    check("C used send_private_msg", [n for n, _ in bot.calls], ["send_private_msg"])
    check("C carries user_id", bot.calls[0][1].get("user_id"), ADMIN)
    check("C carries group_id for the temp session", bot.calls[0][1].get("group_id"), OTHER)
    check("C marked summoned", await pending(OTHER), 0)

    print("\n== D. 私聊里发命令 => 图片与提示都走私聊 ==")
    async with connect() as db:
        await db.execute(
            "UPDATE recall_events SET summoned_at=NULL WHERE group_id=?", (str(OTHER),)
        )
        await db.commit()
    bot = StubBot()
    delivered = await send_recall_history(
        bot,
        RecallRequest(group_id=OTHER, user_id=ADMIN, private=True, from_group=False),
        None,
    )
    check("D delivered count", delivered, 1)
    check(
        "D only private sends",
        {n for n, _ in bot.calls if n.startswith("send_")},
        {"send_private_msg"},
    )
    check(
        "D nothing sent to the group",
        any(n == "send_group_msg" for n, _ in bot.calls),
        False,
    )

    print("\n== E. 私聊下空队列 => 提示仍走私聊，不进群 ==")
    bot = StubBot()
    delivered = await send_recall_history(
        bot,
        RecallRequest(group_id=OTHER, user_id=ADMIN, private=True, from_group=False),
        None,
    )
    check("E delivered count", delivered, 0)
    check("E used send_private_msg", [n for n, _ in bot.calls], ["send_private_msg"])

    print("\n== F. 群名缺失时回退到 get_group_info ==")
    async with connect() as db:
        await db.execute(
            "UPDATE recall_events SET summoned_at=NULL WHERE group_id=?", (str(GROUP),)
        )
        await db.commit()
    bot = StubBot()
    await send_recall_history(
        bot, RecallRequest(group_id=GROUP, user_id=ADMIN, group_name=""), None
    )
    check(
        "F asked OneBot for the group name",
        "call_api:get_group_info" in {n for n, _ in bot.calls},
        True,
    )

    print("\n== G. 条数上限只标记真正画出来的记录 ==")
    # F consumed every pending row, so put them back before the limit test.
    async with connect() as db:
        await db.execute(
            "UPDATE recall_events SET summoned_at=NULL WHERE group_id=?", (str(GROUP),)
        )
        await db.commit()
    bot = StubBot()
    delivered = await send_recall_history(
        bot, RecallRequest(group_id=GROUP, user_id=ADMIN, group_name="验证群"), 2
    )
    check("G delivered count", delivered, 2)
    check("G three still pending", await pending(GROUP), 3)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
