"""Assert that no bot reply can carry a group number.

Covers the three surfaces that used to leak one, plus the private-only status
report.  Group numbers are read from the live config, so this keeps holding if
the whitelist changes.
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
DB = TEMP / "verify_nogroupid.sqlite3"
for stale in TEMP.glob("verify_nogroupid.sqlite3*"):
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

from nonebot.adapters.onebot.v11 import GroupMessageEvent  # noqa: E402

from plugins.group_bot.runtime import status as gb_status  # noqa: E402
from plugins.group_bot.core.config import ALLOWED_GROUP_IDS  # noqa: E402
from plugins.group_bot.handlers import (  # noqa: E402
    command_help,
    private_help,
    recall_all_refused_reply,
    recall_cooldown_reply,
    recall_private_admin_only_reply,
    recall_target_refused_reply,
    recall_unknown_group_reply,
    clear_owner_only_reply,
)
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402

GROUP = 123450001
OTHER = 123450002
OWNER = 100000009

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_no_group_id(label: str, text: str) -> None:
    leaked = sorted(gid for gid in ALLOWED_GROUP_IDS if str(gid) in text)
    if leaked:
        failures.append(f"{label}: leaked {leaked}")
        print(f"FAIL {label}: leaked {leaked}")
    else:
        print(f"ok   {label} -> 无群号")


class StubBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def send_group_msg(self, **kwargs):
        self.calls.append(("send_group_msg", kwargs))
        return {"status": "ok"}

    async def send_private_msg(self, **kwargs):
        self.calls.append(("send_private_msg", kwargs))
        return {"status": "ok"}

    @property
    def self_id(self) -> str:
        return "100000000"


async def seed() -> None:
    await initialize_database()
    async with connect() as db:
        for group, message_id in ((GROUP, "9001"), (OTHER, "9002")):
            await db.execute(
                """INSERT OR REPLACE INTO messages
                   (message_id, group_id, user_id, sender_name, message_json,
                    plain_text, sent_at, recalled_at, recalled_by)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    message_id, str(group), "100000011", "测试员",
                    '[{"type": "text", "data": {"text": "记录"}}]',
                    "记录", SENT_AT, RECALLED_AT, "100000011",
                ),
            )
            await db.execute(
                """INSERT OR REPLACE INTO recall_events
                   (group_id, message_id, recalled_at, summoned_at)
                   VALUES (?,?,?,NULL)""",
                (str(group), message_id, RECALLED_AT),
            )
        await db.commit()


def group_event(user_id: int, group_id: int = GROUP) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=int(datetime.now(timezone.utc).timestamp()),
        self_id=100000000,
        post_type="message",
        message_type="group",
        sub_type="normal",
        message_id=1,
        user_id=user_id,
        group_id=group_id,
        message=[{"type": "text", "data": {"text": "status"}}],
        raw_message="status",
        font=0,
        sender={"user_id": user_id, "nickname": "主人", "card": ""},
        to_me=True,
    )


async def main() -> None:
    await seed()

    print("\n== A. 静态回复文本里没有群号 ==")
    check_no_group_id("A recall_unknown_group_reply", recall_unknown_group_reply())
    check_no_group_id("A private_help", private_help(is_super=True))
    check_no_group_id("A private_help（非主人）", private_help(is_super=False))
    check_no_group_id("A command_help（管理员）", command_help(is_admin=True))
    check_no_group_id("A recall_target_refused_reply", recall_target_refused_reply())
    check_no_group_id("A recall_all_refused_reply", recall_all_refused_reply())
    check_no_group_id("A recall_private_admin_only_reply", recall_private_admin_only_reply())
    check_no_group_id("A clear_owner_only_reply", clear_owner_only_reply())
    check_no_group_id("A recall_cooldown_reply", recall_cooldown_reply(88))
    check("A 提示改用占位符", "<群号>" in recall_unknown_group_reply(), True)
    check("A 私聊 help 也用占位符", "<群号>" in private_help(is_super=True), True)

    print("\n== B. status 报告不含群号，且改成汇总 ==")
    data = await gb_status.collect()
    # 两个群都塞了未召回记录，确保逐群分支真的被走到。
    check("B 未召回数据非空", len(data["recall_pending"]), 2)
    for current in (GROUP, OTHER):
        report = gb_status.render_status(
            data, self_id="100000000", uptime=3600.0, current_group_id=current
        )
        check_no_group_id(f"B 报告(current={current})", report)
        check(f"B 报告(current={current}) 出现「本群」", "本群" in report, True)
    first = gb_status.render_status(
        data, self_id="100000000", uptime=3600.0, current_group_id=GROUP
    )
    check("B 其他群按汇总计数", "其他群" in first, True)
    check("B 保留总消息量", "总消息" in first, True)
    check("B 保留未召回量", "未召回" in first, True)

    print("\n== C. status 只发私聊，群里静默 ==")
    bot = StubBot()
    await gb_status.send_status(bot, group_event(OWNER))
    check("C 只调用私聊发送", [n for n, _ in bot.calls], ["send_private_msg"])
    check("C 发给发起人", bot.calls[0][1].get("user_id"), OWNER)
    check("C 带上来源群（临时会话可达）", bot.calls[0][1].get("group_id"), GROUP)
    check_no_group_id("C 私聊内容里无群号", bot.calls[0][1]["message"])

    print("\n== D. 失败的兜底提示仍回群里，且不含群号 ==")
    class FailingBot(StubBot):
        async def send_private_msg(self, **kwargs):
            self.calls.append(("send_private_msg", kwargs))
            raise RuntimeError("private send failed")

    bot = FailingBot()
    await gb_status.safe_send_status(bot, group_event(OWNER))
    check("D 失败后回群提示", [n for n, _ in bot.calls], ["send_private_msg", "send_group_msg"])
    check_no_group_id("D 兜底提示无群号", bot.calls[1][1]["message"])

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
