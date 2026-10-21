"""Cooldowns: one per user for general mentions, one per group for 撤回记录."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from ..core.store import connect

COOLDOWN_SECONDS = 120
# 撤回记录 现在对群内所有人开放，所以用「一个群一个窗口」而不是「一个人一个窗口」：
# 防的是整群把同一批撤回记录反复刷出来，而不是某个人手快。
GROUP_RECALL_COOLDOWN_SECONDS = 120


async def take_non_admin_mention_slot(group_id: int, user_id: int | str) -> bool:
    """Claim the right to answer one non-admin mention.

    Returns True when this mention should be handled, False while the same user
    is still inside their cooldown window for this group.
    """
    now = datetime.now(timezone.utc).timestamp()
    async with connect() as db:
        cursor = await db.execute(
            """INSERT INTO mention_cooldowns(group_id, user_id, cooldown_until)
               VALUES (?, ?, ?)
               ON CONFLICT(group_id, user_id) DO UPDATE
               SET cooldown_until=excluded.cooldown_until
               WHERE mention_cooldowns.cooldown_until <= ?""",
            (
                str(group_id),
                str(user_id),
                now + COOLDOWN_SECONDS,
                now,
            ),
        )
        await db.commit()
        return cursor.rowcount == 1


async def take_group_recall_slot(group_id: int) -> tuple[bool, int, bool]:
    """Claim the group's single 撤回记录 window.

    Returns ``(allowed, seconds_left, first_notice)``.  ``first_notice`` is True
    only for the first blocked attempt inside a window, so a group is told once
    how long to wait rather than being answered on every attempt.
    """
    now = datetime.now(timezone.utc).timestamp()
    async with connect() as db:
        cursor = await db.execute(
            """INSERT INTO group_cooldowns(group_id, cooldown_until, notified)
               VALUES (?, ?, 0)
               ON CONFLICT(group_id) DO UPDATE
               SET cooldown_until=excluded.cooldown_until, notified=0
               WHERE group_cooldowns.cooldown_until <= ?""",
            (str(group_id), now + GROUP_RECALL_COOLDOWN_SECONDS, now),
        )
        await db.commit()
        if cursor.rowcount == 1:
            return True, 0, False

        cursor = await db.execute(
            "SELECT cooldown_until, notified FROM group_cooldowns WHERE group_id=?",
            (str(group_id),),
        )
        row = await cursor.fetchone()
        if row is None:  # pragma: no cover - the row was just written above
            return True, 0, False
        remaining = max(0, math.ceil(float(row["cooldown_until"]) - now))
        if int(row["notified"]) == 0:
            await db.execute(
                "UPDATE group_cooldowns SET notified=1 WHERE group_id=?",
                (str(group_id),),
            )
            await db.commit()
            return False, remaining, True
        return False, remaining, False
