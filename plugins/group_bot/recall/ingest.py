"""Message ingestion: archiving new messages and marking recalls."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, GroupRecallNoticeEvent, Message

from ..media.archive import schedule_archive
from ..core.config import ARCHIVE_ENABLED
from ..media.pipeline import cache_message_images
from ..common.render import render_message
from ..core.store import connect


def message_data(message: Message) -> list[dict[str, Any]]:
    return [{"type": segment.type, "data": dict(segment.data)} for segment in message]


async def save_message(bot: Bot, event: GroupMessageEvent) -> None:
    # Keep the unmodified segments too; some OneBot implementations strip the
    # bot mention from `message` and expose it only in `original_message`/to_me.
    source_message = getattr(event, "original_message", event.get_message())
    data = message_data(source_message)
    # 不在这里预写 `media_cache`：`media_archive/` 才是权威副本（见 archive.py），
    # 渲染需要缓存时由 `media.cache_message_images` 从归档复制，复制不到才联网。
    # 以前每条带图消息都写两份（缓存一份 + 归档一份），纯属浪费磁盘。
    async with connect() as db:
        await db.execute(
            """INSERT OR IGNORE INTO messages
               (message_id, group_id, user_id, sender_name, message_json,
                plain_text, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(event.message_id),
                str(event.group_id),
                str(event.user_id),
                str(event.sender.card or event.sender.nickname or event.user_id),
                json.dumps(data, ensure_ascii=False),
                render_message(data),
                datetime.fromtimestamp(event.time, timezone.utc).isoformat(),
            ),
        )
        await db.commit()
    # 多媒体永久归档（图片 / 视频 / 语音 / 文件）：后台任务，不阻塞消息处理；
    # 失败由启动与定时的补归档扫描兜底（见 archive.archive_pending）。
    if ARCHIVE_ENABLED:
        schedule_archive(bot, event, data)


async def mark_recalled(event: GroupRecallNoticeEvent) -> None:
    recalled_at = datetime.now(timezone.utc).isoformat()
    async with connect() as db:
        cursor = await db.execute(
            "UPDATE messages SET recalled_at=?, recalled_by=? "
            "WHERE message_id=? AND group_id=?",
            (
                recalled_at,
                str(event.operator_id),
                str(event.message_id),
                str(event.group_id),
            ),
        )
        if cursor.rowcount:
            await db.execute(
                "INSERT OR IGNORE INTO recall_events(group_id, message_id, recalled_at) "
                "VALUES (?, ?, ?)",
                (str(event.group_id), str(event.message_id), recalled_at),
            )
        await db.commit()
