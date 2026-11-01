"""Recall history: incremental cursor, image recovery and page rendering."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageSegment

from ..common.card import CardEntry, CardQuote, page_size_for, paginate, render_card_page
from ..core.config import (
    GROUP_TIMEZONE,
    MEDIA_CACHE_DIR,
    RECALL_IMAGE_DIR,
    RECALL_WINDOW_MINUTES,
)
from ..media.pipeline import (
    avatar_dir,
    cache_avatar,
    cache_message_images,
    cached_image_paths,
    cached_image_paths_from_json,
)
from ..media.archive import archive_message, archived_media_paths
from ..media.files import file_name, send_original_file
from ..common.render import render_message
from ..common.replies import ReplyPreview, forward_ids, load_forward_previews, load_reply_previews
from ..core.state import _avatar_warm_tasks, _recall_locks
from ..common.bot_info import group_name_for
from ..common.safety import guard
from ..core.store import connect


@dataclass(frozen=True)
class RecallRequest:
    """Who asked for recall records, about which group, and where they go.

    ``private`` sends the cards to the requester's 1:1 chat with the bot
    instead of into the group: OneBot/NapCat turn a private send that carries
    ``group_id`` into a 群临时会话 for a non-friend, and into a plain private
    message for a friend, so the requester receives it either way.

    ``from_group`` is where the command was typed, which decides where text
    notices go: a command typed in the group keeps its "nothing to show /
    it failed" answers in the group, while a command typed in a private chat
    is answered privately and never touches the group.
    """

    group_id: int
    user_id: int
    private: bool = False
    from_group: bool = True
    group_name: str = ""


async def _deliver(bot: Bot, request: RecallRequest, message: Any, *, to_group: bool) -> Any:
    if to_group:
        return await bot.send_group_msg(group_id=request.group_id, message=message)
    return await bot.send_private_msg(
        user_id=request.user_id, group_id=request.group_id, message=message
    )


async def send_cards(bot: Bot, request: RecallRequest, message: Any) -> Any:
    """Send one recall card image to wherever this request asked for."""
    return await _deliver(bot, request, message, to_group=not request.private)


async def send_recall_videos(
    bot: Bot, request: RecallRequest, rows: list[Any]
) -> None:
    """Send archived original videos belonging to one already-rendered page."""
    for row in rows:
        for path in row.get("_video_paths", []):
            result = await send_cards(
                bot,
                request,
                MessageSegment("video", {"file": path.resolve().as_uri()}),
            )
            if isinstance(result, dict) and result.get("status") == "failed":
                raise RuntimeError("OneBot failed to send recalled video")


async def send_recall_files(
    bot: Bot, request: RecallRequest, rows: list[Any]
) -> None:
    """Send archived original files belonging to one already-rendered page.

    与漂流瓶的文件投递共用 ``send_original_file``：群里走 ``upload_group_file``，
    私聊走 ``upload_private_file``，保证同一种消息各处表达一致。
    """
    for row in rows:
        paths = row.get("_file_paths", [])
        names = row.get("_file_names", [])
        for index, path in enumerate(paths):
            name = names[index] if index < len(names) else path.name
            await send_original_file(
                bot,
                path,
                name,
                group_id=None if request.private else request.group_id,
                user_id=request.user_id if request.private else None,
            )


async def send_notice(bot: Bot, request: RecallRequest, message: Any) -> Any:
    """Send one text notice to the channel the command was typed in."""
    return await _deliver(bot, request, message, to_group=request.from_group)


def render_recall_page(
    rows: list[Any], group_id: int, group_name: str, page: int, pages: int
) -> Path:
    """Describe the recalled rows to the shared card renderer."""
    entries: list[CardEntry] = []
    for row in rows:
        preview = row.get("_reply_preview") or row.get("_forward_preview")
        has_reply = preview is not None and row.get("_reply_preview") is not None
        has_forward = row.get("_forward_preview") is not None
        try:
            message_data = json.loads(row["message_json"])
            has_image_segment = any(segment.get("type") == "image" for segment in message_data)
            text = render_message(
                message_data,
                omit_images=True,
                omit_replies=has_reply,
                omit_forwards=has_forward,
            )
        except (json.JSONDecodeError, TypeError):
            text = str(row["plain_text"] or "[非文本消息]")
            has_image_segment = "[图片]" in text
            if has_image_segment:
                text = text.replace("[图片]", "").strip()
        image_paths = cached_image_paths(row)
        shown_images = image_paths[:4]
        quote = _card_quote(row.get("_reply_preview") or row.get("_forward_preview"))
        entries.append(
            CardEntry(
                user_id=str(row["user_id"]),
                name=str(row["sender_name"] or "群成员"),
                right_text=datetime.fromisoformat(row["sent_at"])
                .astimezone(GROUP_TIMEZONE)
                .strftime("%Y-%m-%d %H:%M:%S"),
                meta_text=datetime.fromisoformat(row["recalled_at"])
                .astimezone(GROUP_TIMEZONE)
                .strftime("撤回于 %H:%M:%S"),
                text=text,
                image_paths=shown_images,
                image_note="图片暂不可用" if has_image_segment and not shown_images else "",
                extra_images=max(0, len(image_paths) - len(shown_images)),
                quote=quote,
            )
        )
    display_group_name = " ".join(str(group_name).split())[:28] or "本群"
    return render_card_page(
        "撤回记录",
        f"{display_group_name}  ·  第 {page}/{pages} 页",
        entries,
        group_id=group_id,
        directory=RECALL_IMAGE_DIR,
    )


def _card_quote(preview: ReplyPreview | None) -> CardQuote | None:
    if preview is None:
        return None
    meta = ""
    if preview.sent_at:
        try:
            meta = datetime.fromisoformat(preview.sent_at).astimezone(GROUP_TIMEZONE).strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            pass
    return CardQuote(
        title=preview.title,
        name=preview.sender_name,
        meta_text=meta or ("未归档" if preview.missing else ""),
        text=preview.text,
        image_paths=list(preview.image_paths),
    )


async def _mark_summoned(group_id: str, event_ids: list[int], summoned_at: str) -> int:
    """Commit one successfully delivered page before sending the next one."""
    if not event_ids:
        return 0
    placeholders = ",".join("?" * len(event_ids))
    async with connect() as db:
        cursor = await db.execute(
            f"""UPDATE recall_events SET summoned_at=?
                WHERE group_id=? AND summoned_at IS NULL
                  AND event_id IN ({placeholders})""",
            (summoned_at, group_id, *event_ids),
        )
        await db.execute(
            """INSERT OR IGNORE INTO recall_cursor(group_id, last_event_id)
               VALUES (?, 0)""",
            (group_id,),
        )
        await db.execute(
            """UPDATE recall_cursor
                  SET last_event_id=MAX(last_event_id, ?), last_summoned_at=?
                WHERE group_id=?""",
            (max(event_ids), summoned_at, group_id),
        )
        await db.commit()
        return int(cursor.rowcount or 0)


async def clear_pending_recall(group_id: str) -> int:
    lock = _recall_locks.setdefault(group_id, asyncio.Lock())
    async with lock:
        async with connect() as db:
            await db.execute(
                "INSERT OR IGNORE INTO recall_cursor(group_id, last_event_id) VALUES (?, 0)",
                (group_id,),
            )
            cursor = await db.execute(
                """SELECT e.event_id, m.message_json FROM recall_events e
                   LEFT JOIN messages m ON m.group_id=e.group_id AND m.message_id=e.message_id
                   WHERE e.group_id=? AND e.summoned_at IS NULL
                   ORDER BY e.event_id""",
                (group_id,),
            )
            pending = await cursor.fetchall()
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "UPDATE recall_events SET summoned_at=? WHERE group_id=? AND summoned_at IS NULL",
                (now, group_id),
            )
            await db.execute(
                """UPDATE recall_cursor
                   SET last_event_id=MAX(last_event_id, COALESCE(
                           (SELECT MAX(event_id) FROM recall_events WHERE group_id=?), 0)),
                       last_summoned_at=?
                   WHERE group_id=?""",
                (group_id, now, group_id),
            )
            await db.commit()

        # Remove only image files attached to discarded, not-yet-summoned recalls.
        for row in pending:
            if row["message_json"]:
                for path in cached_image_paths_from_json(row["message_json"]):
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("Could not remove discarded image cache {}", path)
        return len(pending)


def recall_window_cutoff() -> str:
    """ISO timestamp marking the start of the default recall window."""
    return (datetime.now(timezone.utc) - timedelta(minutes=RECALL_WINDOW_MINUTES)).isoformat()


async def pending_recall_counts(db: aiosqlite.Connection, group_id: str) -> tuple[int, int]:
    """(未召回且落在默认时间窗内, 未召回总数) for one group."""
    cursor = await db.execute(
        """SELECT SUM(CASE WHEN recalled_at>=? THEN 1 ELSE 0 END) AS recent,
                  COUNT(*) AS total
           FROM recall_events WHERE group_id=? AND summoned_at IS NULL""",
        (recall_window_cutoff(), group_id),
    )
    row = await cursor.fetchone()
    return int(row["recent"] or 0), int(row["total"] or 0)


def empty_recall_text(recent: int, total: int, all_pending: bool) -> str:
    """Structured answer when there is nothing to draw."""
    if total == 0:
        if all_pending:
            return "可莉把小本本翻到底啦，已经没有没看过的撤回记录咯。"
        return (
            f"可莉只翻了最近 {RECALL_WINDOW_MINUTES} 分钟，这段时间里没有被撤回的消息，"
            "更早的也都看过了。"
        )
    if all_pending:
        return f"唔…可莉这边显示还有 {total} 条没看过的记录，但一条都没取出来，待会儿再试试吧。"
    return "\n".join(
        [
            f"可莉只翻了最近 {RECALL_WINDOW_MINUTES} 分钟，这段时间里没有被撤回的消息。",
            f"不过还有 {total} 条更早的、没看过的记录躺在小本本里。",
            "超级管理员可以让可莉把它们都翻出来：",
            "· @可莉 撤回记录 全部 —— 所有还没看过的撤回消息",
        ]
    )


def clear_recall_text(cleared: int) -> str:
    if cleared == 0:
        return "可莉的小本本上没有没看过的撤回记录，不用清啦。"
    return "\n".join(
        [
            f"可莉擦掉了 {cleared} 条还没看过的撤回记录，对应的图片缓存也一起收拾干净了。",
            "从现在开始重新累计哦。",
        ]
    )


async def send_recall_history(
    bot: Bot,
    request: RecallRequest,
    count: int | None,
    *,
    all_pending: bool = False,
    include_summoned: bool = False,
    mark_summoned: bool = True,
    silent_empty: bool = False,
) -> int:
    """Send not-yet-summoned recalls as images; return how many were drawn.

    By default only recalls that happened inside ``RECALL_WINDOW_MINUTES`` are
    shown; ``all_pending`` (super admin only, gated in ``handlers``) lifts that
    limit.  Either way only the rows actually drawn are marked as summoned, so
    older records stay pending for a later "全部" call.

    ``include_summoned`` + ``mark_summoned=False`` is the private owner-only
    "历史" mode: every recalled message is shown, already-seen ones included,
    and nothing is marked.  ``silent_empty`` lets the all-groups loop skip a
    group that has no history instead of sending an empty notice per group.

    The return value is what the caller needs to decide whether a short "already
    sent it to your private chat" notice is warranted; 0 means nothing was
    delivered (no rows, or the send failed and was reported).
    """
    group_id = str(request.group_id)
    lock = _recall_locks.setdefault(group_id, asyncio.Lock())
    async with lock:
        async with connect() as db:
            query = """SELECT e.event_id, e.recalled_at, m.message_id, m.user_id,
                              m.sender_name, m.plain_text, m.sent_at, m.message_json
                       FROM recall_events e JOIN messages m
                         ON m.group_id=e.group_id AND m.message_id=e.message_id
                       WHERE e.group_id=?"""
            params: list[Any] = [group_id]
            if not include_summoned:
                query += " AND e.summoned_at IS NULL"
            if not all_pending:
                query += " AND e.recalled_at>=?"
                params.append(recall_window_cutoff())
            if count is None:
                query += " ORDER BY e.event_id ASC"
            else:
                # "只看最近 N 条" picks the newest N, then they are drawn oldest-first.
                query += " ORDER BY e.event_id DESC LIMIT ?"
                params.append(count)
            cursor = await db.execute(query, tuple(params))
            rows = [dict(row) for row in await cursor.fetchall()]
            recent, total = await pending_recall_counts(db, group_id)
        if count is not None:
            rows.reverse()
        if not rows:
            if silent_empty:
                return 0
            await send_notice(bot, request, empty_recall_text(recent, total, all_pending))
            return 0

        parsed_messages: list[list[dict[str, Any]]] = []
        for row in rows:
            try:
                message_data = json.loads(row["message_json"])
                parsed_messages.append(message_data if isinstance(message_data, list) else [])
            except (json.JSONDecodeError, TypeError):
                parsed_messages.append([])
        reply_previews = await load_reply_previews(group_id, parsed_messages)
        forward_previews = await load_forward_previews(bot, parsed_messages)
        video_message_ids = [
            str(row["message_id"])
            for row, message_data in zip(rows, parsed_messages)
            if any(
                isinstance(segment, dict) and segment.get("type") == "video"
                for segment in message_data
            )
        ]
        video_paths = await archived_media_paths(group_id, video_message_ids)
        # The background archiver normally wins this race.  If a user asks for
        # a just-sent video before it finishes, make one bounded retry now so
        # recall can still return the original file.
        for row, message_data in zip(rows, parsed_messages):
            message_id = str(row["message_id"])
            if message_id not in video_message_ids or video_paths.get(message_id):
                continue
            try:
                await archive_message(bot, group_id, message_id, message_data, reason="recall")
            except Exception:
                logger.debug("Could not archive recalled video {}", message_id, exc_info=True)
        video_paths = await archived_media_paths(group_id, video_message_ids)
        file_message_ids = [
            str(row["message_id"])
            for row, message_data in zip(rows, parsed_messages)
            if any(
                isinstance(segment, dict) and segment.get("type") == "file"
                for segment in message_data
            )
        ]
        file_paths = await archived_media_paths(group_id, file_message_ids, kind="file")
        for row, message_data in zip(rows, parsed_messages):
            message_id = str(row["message_id"])
            if message_id not in file_message_ids or file_paths.get(message_id):
                continue
            try:
                await archive_message(bot, group_id, message_id, message_data, reason="recall")
            except Exception:
                logger.debug("Could not archive recalled file {}", message_id, exc_info=True)
        file_paths = await archived_media_paths(group_id, file_message_ids, kind="file")
        for row, message_data in zip(rows, parsed_messages):
            ids = [
                str(segment.get("data", {}).get("id"))
                for segment in message_data
                if isinstance(segment, dict) and segment.get("type") == "reply"
            ]
            if ids and ids[0] in reply_previews:
                row["_reply_preview"] = reply_previews[ids[0]]
            forward_ids_for_row = forward_ids(message_data)
            if forward_ids_for_row and forward_ids_for_row[0] in forward_previews:
                row["_forward_preview"] = forward_previews[forward_ids_for_row[0]]
            row["_video_paths"] = video_paths.get(str(row["message_id"]), [])
            file_segments = [
                segment
                for segment in message_data
                if isinstance(segment, dict) and segment.get("type") == "file"
            ]
            row["_file_names"] = [
                file_name(segment.get("data") or {}, index)
                for index, segment in enumerate(file_segments)
            ]
            row["_file_paths"] = file_paths.get(str(row["message_id"]), [])

        group_name = await group_name_for(bot, request.group_id, request.group_name)

        # Wait for the initial full-group cache; then fill any missing senders.
        avatar_task = _avatar_warm_tasks.get(request.group_id)
        if avatar_task and not avatar_task.done():
            await avatar_task
        avatar_dir(request.group_id).mkdir(parents=True, exist_ok=True)
        MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            await asyncio.gather(
                *(cache_avatar(request.group_id, str(row["user_id"]), client) for row in rows)
            )
            for row in rows:
                try:
                    message_data = json.loads(row["message_json"])
                    if any(
                        segment.get("type") == "image"
                        and not segment.get("data", {}).get("cached_path")
                        for segment in message_data
                    ):
                        await cache_message_images(
                            bot,
                            request.group_id,
                            str(row["message_id"]),
                            message_data,
                            client,
                        )
                        row["message_json"] = json.dumps(message_data, ensure_ascii=False)
                        async with connect() as db:
                            await db.execute(
                                "UPDATE messages SET message_json=? WHERE group_id=? AND message_id=?",
                                (row["message_json"], group_id, str(row["message_id"])),
                            )
                            await db.commit()
                except (json.JSONDecodeError, TypeError, KeyError):
                    logger.debug("Could not recover cached image data for recalled message")

        max_images = max((len(cached_image_paths(row)) for row in rows), default=0)
        chunks = paginate(rows, page_size_for(max_images))
        files: list[Path] = []
        summoned_count = 0
        try:
            # 分页后可能有好几张，逐页在线程里渲染：一次请求能画出数秒的 CPU 活，
            # 放在事件循环里会连带挡住心跳与其它群的消息处理。
            files = [
                await asyncio.to_thread(
                    render_recall_page,
                    chunk,
                    request.group_id,
                    group_name or "本群",
                    page + 1,
                    len(chunks),
                )
                for page, chunk in enumerate(chunks)
            ]
            for path, chunk in zip(files, chunks):
                result = await send_cards(
                    bot, request, MessageSegment.image(path.resolve().as_uri())
                )
                if isinstance(result, dict) and result.get("status") == "failed":
                    raise RuntimeError("OneBot failed to send recall image")
                await send_recall_videos(bot, request, chunk)
                await send_recall_files(bot, request, chunk)
                if mark_summoned:
                    summoned_count += await _mark_summoned(
                        group_id,
                        [int(row["event_id"]) for row in chunk],
                        datetime.now(timezone.utc).isoformat(),
                    )
                else:
                    summoned_count += len(chunk)
        except Exception:
            logger.exception("Could not send recall image batch")
            await send_notice(
                bot,
                request,
                (
                    f"撤回记录已发送 {summoned_count} 条，但后续页面发送失败；"
                    "剩余记录可以稍后重试。"
                    if summoned_count
                    else "撤回记录图片发送失败；记录没有标记为已召回，可以稍后重试。"
                ),
            )
            return summoned_count
        finally:
            for path in files:
                path.unlink(missing_ok=True)

        # Each page was marked immediately after its send.  Anything older than
        # the window (or beyond the requested count) remains pending on purpose.
        return summoned_count


async def safe_send_recall_history(
    bot: Bot,
    request: RecallRequest,
    count: int | None,
    *,
    all_pending: bool = False,
) -> int:
    sent = await guard(
        "Recall history handler",
        lambda: send_recall_history(bot, request, count, all_pending=all_pending),
        on_error=lambda: send_notice(
            bot,
            request,
            "可莉的撤回记录处理失败了，请稍后再试；详细错误已记入机器人日志。",
        ),
    )
    return 0 if sent is None else sent


async def send_all_recall_history(
    bot: Bot,
    request: RecallRequest,
    count: int | None,
    *,
    groups: list[int],
) -> int:
    """Owner-only private history: every recall, seen or not, across groups.

    Each group is sent as its own card set so avatars, group names and archived
    media stay scoped to the right group.  Nothing is marked as summoned: this
    is an audit view, not "consume the pending queue".
    """
    total = 0
    for group_id in groups:
        remaining = None if count is None else max(0, count - total)
        if remaining == 0:
            break
        sub_request = RecallRequest(
            group_id=group_id,
            user_id=request.user_id,
            private=True,
            from_group=False,
        )
        total += await send_recall_history(
            bot,
            sub_request,
            remaining,
            all_pending=True,
            include_summoned=True,
            mark_summoned=False,
            silent_empty=True,
        )
    if total == 0:
        await send_notice(bot, request, "没有找到任何撤回记录哦。")
    return total


async def safe_send_all_recall_history(
    bot: Bot,
    request: RecallRequest,
    count: int | None,
    *,
    groups: list[int],
) -> int:
    sent = await guard(
        "Recall all-history handler",
        lambda: send_all_recall_history(bot, request, count, groups=groups),
        on_error=lambda: send_notice(
            bot,
            request,
            "可莉的撤回历史处理失败了，请稍后再试；详细错误已记入机器人日志。",
        ),
    )
    return 0 if sent is None else sent
