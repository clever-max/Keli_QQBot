"""Runtime feature switches, persisted in SQLite.

Anything that comes from ``.env`` belongs in ``config``; a switch lives here
instead because it is flipped while the bot is running — often to stop
something mid-flight — and it must therefore survive a restart.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .store import connect

# 撤回记录私聊投递（`撤回记录 私聊` 与私聊里的命令入口）。
# 默认开启：这个功能本身是新加的，默认关掉会让人以为它坏了。
RECALL_PRIVATE_DELIVERY = "recall_private_delivery"


async def _stored_value(name: str) -> bool | None:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT value FROM bot_settings WHERE name=?", (name,)
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["value"]) == "1"


async def is_switch_on(name: str, *, default: bool = True) -> bool:
    """Current state of a switch; a switch that was never touched uses ``default``."""
    stored = await _stored_value(name)
    return default if stored is None else stored


async def set_switch(name: str, enabled: bool, *, default: bool = True) -> bool:
    """Store a switch and report whether it actually changed.

    The previous value is read and written inside one connection so the caller
    can tell "just turned it off" from "it was already off" in its reply.
    """
    async with connect() as db:
        cursor = await db.execute(
            "SELECT value FROM bot_settings WHERE name=?", (name,)
        )
        row = await cursor.fetchone()
        previous = default if row is None else str(row["value"]) == "1"
        await db.execute(
            """INSERT INTO bot_settings(name, value, updated_at) VALUES (?,?,?)
               ON CONFLICT(name) DO UPDATE SET value=excluded.value,
                                              updated_at=excluded.updated_at""",
            (name, "1" if enabled else "0", datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    return previous != enabled
