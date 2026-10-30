"""漂流瓶的流程编排与用户可见文案：一次抽取从配额到发图的完整链路。

这是唯一同时依赖 pool / stats / render 的模块；handler 只跟这一层打交道。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import tempfile
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment

from ..media.album import (
    AlbumPhoto,
    AlbumUnavailable,
    fetch_album_photos,
    materialize_photo,
    mark_broken,
)
from ..media.archive import archived_media_paths
from ..media.files import file_name, file_size_text, send_original_file
from ..common.card import CardEntry, render_card_page
from ..common.replies import load_forward_previews, load_reply_preview
from ..core.config import (
    ARCHIVE_MAX_BYTES,
    BOTTLE_ALBUM_ENABLED,
    BOTTLE_ALBUM_SHARE,
    BOTTLE_HISTORY_DAYS,
    BOTTLE_REPEAT_MODE,
    BOTTLE_REPEAT_PICK_K,
    BOTTLE_SWITCH_STREAK,
    ESSENCE_CACHE_SECONDS,
    ESSENCE_COOLDOWN_SECONDS,
    ESSENCE_DAILY_LIMIT,
    GROUP_TIMEZONE,
    MEDIA_CACHE_DIR,
)
from ..media.pipeline import (
    cache_avatar,
    cache_message_images,
    cached_image_paths_from_json,
)
from ..common.render import render_message
from ..core.state import (
    _album_broken,
    _avatar_warm_tasks,
    _essence_broken,
    _essence_cache,
    _essence_locks,
    _member_names,
)
from ..core.store import connect

from .pool import (
    ChosenBottle,
    ESSENCE_DRAW_RETENTION_DAYS,
    EssenceUnavailable,
    _is_playable_ref,
    _pick_key,
    album_pool_enabled,
    album_pool_size,
    choose_bottle,
    essence_pool_size,
    fetch_essence_list,
    format_local,
    local_day_key,
    pool_sizes,
    record_draw,
)
from .render import render_bottle_card, render_drawn_leaderboard_card, resolve_drawn_content
from ..common.bot_info import group_name_for
from ..common.safety import guard_group
from .stats import content_label, draw_times_after, drawn_content_leaderboard


class DrawVerdict(NamedTuple):
    """Outcome of asking for one draw slot."""

    kind: str  # "ok" | "cooldown" | "daily_limit"
    remaining: int = 0  # draws left today
    wait_seconds: int = 0  # only meaningful for "cooldown"
    cooldown_key: float = 0.0  # last_draw_at of the cooldown being reported


async def claim_essence_draw(group_id: int, user_id: int | str) -> DrawVerdict:
    """Reserve one draw for this user.

    A ``cooldown`` verdict never spends quota.
    """
    day = local_day_key()
    now = time.time()
    async with connect() as db:
        await db.execute(
            """INSERT OR IGNORE INTO essence_usage(group_id, user_id, day)
               VALUES (?, ?, ?)""",
            (str(group_id), str(user_id), day),
        )
        cursor = await db.execute(
            "SELECT used, last_draw_at FROM essence_usage WHERE group_id=? AND user_id=? AND day=?",
            (str(group_id), str(user_id), day),
        )
        row = await cursor.fetchone()
        used = int(row["used"]) if row else 0
        last_draw_at = float(row["last_draw_at"]) if row else 0.0
        if used >= ESSENCE_DAILY_LIMIT:
            return DrawVerdict("daily_limit", 0, 0)
        waited = now - last_draw_at if last_draw_at else ESSENCE_COOLDOWN_SECONDS
        if waited < ESSENCE_COOLDOWN_SECONDS:
            return DrawVerdict(
                "cooldown",
                max(0, ESSENCE_DAILY_LIMIT - used),
                int(ESSENCE_COOLDOWN_SECONDS - waited) + 1,
                last_draw_at,
            )
        await db.execute(
            """UPDATE essence_usage SET used=used+1, last_draw_at=?
               WHERE group_id=? AND user_id=? AND day=?""",
            (now, str(group_id), str(user_id), day),
        )
        await db.commit()
    return DrawVerdict("ok", max(0, ESSENCE_DAILY_LIMIT - used - 1), 0)


async def mark_limit_notified(group_id: int, user_id: int | str) -> bool:
    """True the first time this user runs out of quota on this local day."""
    day = local_day_key()
    async with connect() as db:
        cursor = await db.execute(
            """UPDATE essence_usage SET limit_notified=1
               WHERE group_id=? AND user_id=? AND day=? AND limit_notified=0""",
            (str(group_id), str(user_id), day),
        )
        await db.commit()
        return cursor.rowcount == 1


async def refund_essence_draw(group_id: int, user_id: int | str) -> None:
    """Give the quota back when the card could not be delivered."""
    day = local_day_key()
    async with connect() as db:
        await db.execute(
            """UPDATE essence_usage SET used=MAX(used-1, 0)
               WHERE group_id=? AND user_id=? AND day=?""",
            (str(group_id), str(user_id), day),
        )
        await db.commit()


async def claim_cooldown_notice(
    group_id: int, user_id: int | str, cooldown_key: float
) -> bool:
    """True only for the first attempt inside one cooldown window.

    The notice marker is stored next to the draw itself (``cooldown_notified_at``
    holds the ``last_draw_at`` it was sent for), so a restart does not make the
    bot repeat a cooldown notice and every later attempt in that same window is
    answered with silence.
    """
    day = local_day_key()
    async with connect() as db:
        cursor = await db.execute(
            """UPDATE essence_usage SET cooldown_notified_at=?
               WHERE group_id=? AND user_id=? AND day=? AND cooldown_notified_at<>?""",
            (cooldown_key, str(group_id), str(user_id), day, cooldown_key),
        )
        await db.commit()
        return cursor.rowcount == 1


def cooldown_text(seconds: int) -> str:
    return "\n".join(
        [
            "可莉还在给上一个漂流瓶封口呢，稍微等一下下～",
            f"· 大约还要 {seconds} 秒",
            f"· 每人每群 {ESSENCE_COOLDOWN_SECONDS} 秒一次",
        ]
    )


def daily_limit_text(total: int) -> str:
    lines = [f"你今天已经捞满 {ESSENCE_DAILY_LIMIT} 个漂流瓶啦，明天再来找可莉吧～"]
    if total > 0:
        lines.append(f"· 本群一共有 {total} 个漂流瓶可以捞")
    lines.append(f"· 每人每天 {ESSENCE_DAILY_LIMIT} 次")
    return "\n".join(lines)


def empty_bottle_text() -> str:
    return "\n".join(
        [
            "可莉翻了翻，这个群里还没有能装进漂流瓶的东西哦。",
            "· 群精华是空的，群相册里也没有照片",
            "· 让谁把消息设为精华、或者往群相册传张照片，就有啦",
        ]
    )


def pool_exhausted_text(essence_total: int, album_total: int) -> str:
    return "\n".join(
        [
            "本群今天的漂流瓶都被捞过一遍啦，可莉要等明天才有新的～",
            f"· 精华 {essence_total} 条 + 相册照片 {album_total} 张",
            "· 每天捞到的不会重样",
            f"· 每人每天还是 {ESSENCE_DAILY_LIMIT} 次机会（这次没算数）",
        ]
    )


def no_showable_text(essence_total: int, album_total: int) -> str:
    return "\n".join(
        [
            "剩下的漂流瓶里，图片都打不开，可莉捞不到能看的啦…",
            f"· 精华 {essence_total} 条 + 相册照片 {album_total} 张",
            "· 图片坏掉的那些可莉会跳过",
            "· 这次的次数已经还给你啦",
        ]
    )


def album_disabled_text() -> str:
    """``漂流瓶 相册`` while the album half is switched off."""
    return "\n".join(
        [
            "群相册这一半被关掉了，可莉捞不到照片哦。",
            "· 不带「相册」的话，可莉会从精华和相册里一起捞",
        ]
    )


def album_empty_text() -> str:
    return "\n".join(
        [
            "可莉翻了翻群相册，里面还没有照片哦。",
            "· 往群相册传张照片，就能捞到啦",
            "· 不带「相册」的话，可莉会从精华里一起捞",
        ]
    )


def album_exhausted_text(album_total: int) -> str:
    return "\n".join(
        [
            "群相册里的照片今天都被捞过一遍啦，可莉要等明天才有新的～",
            f"· 相册照片共 {album_total} 张",
            "· 每天捞到的不会重样",
            f"· 每人每天还是 {ESSENCE_DAILY_LIMIT} 次机会（这次没算数）",
        ]
    )


def album_no_showable_text(album_total: int) -> str:
    return "\n".join(
        [
            "相册里的照片可莉都打不开，捞不到能看的啦…",
            f"· 相册照片共 {album_total} 张",
            "· 下载失败的会跳过",
            "· 这次的次数已经还给你啦",
        ]
    )


async def _download_file_part(part: dict[str, Any], index: int) -> Path:
    """把文件段落成本地临时文件：优先用 ``file`` 本地路径，否则下载 ``url``。"""
    local = str(part.get("file") or "").strip()
    remote = str(part.get("url") or "").strip()
    if local and _is_playable_ref(local):
        path = Path(local[7:] if local.startswith("file://") else local)
        if path.is_file():
            return path
    if not remote:
        raise RuntimeError(f"file part {index} has no downloadable reference")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        response = await client.get(remote)
        response.raise_for_status()
    content = response.content
    if ARCHIVE_MAX_BYTES and len(content) > ARCHIVE_MAX_BYTES:
        raise RuntimeError(f"file part {index} exceeds ARCHIVE_MAX_BYTES")

    suffix = Path(file_name(part, index)).suffix or ".bin"
    handle, temp_name = tempfile.mkstemp(prefix="bottle-file-", suffix=suffix)
    with os.fdopen(handle, "wb") as file_obj:
        file_obj.write(content)
    return Path(temp_name)


async def upload_bottle_files(
    bot: Bot, group_id: int, chosen: ChosenBottle
) -> list[str]:
    """把精华里的文件原样发到群里，返回文件名列表。

    优先用 ``message_media`` 里已经归档的本地副本（不联网、不受 URL 过期影响），
    缺失的再按 ``url`` 下载成临时文件。任一步失败都抛异常，由调用方退次数。
    """
    item = chosen.item if isinstance(chosen.item, dict) else {}
    message_id = str(item.get("message_id") or "")
    archived: list[Path] = []
    if message_id:
        archived_map = await archived_media_paths(group_id, [message_id], kind="file")
        archived = archived_map.get(message_id, [])

    temp_files: list[Path] = []
    names: list[str] = []
    try:
        for index, part in enumerate(chosen.file_parts):
            name = file_name(part, index)
            source = archived[index] if index < len(archived) else None
            if source is None:
                source = await _download_file_part(part, index)
                temp_files.append(source)
            await send_original_file(bot, source, name, group_id=group_id)
            names.append(name)
    finally:
        for path in temp_files:
            path.unlink(missing_ok=True)
    return names


def bottle_hint_text(
    chosen: ChosenBottle,
    essence_total: int,
    album_total: int,
    remaining: int,
    *,
    drawn_times: int = 0,
) -> str:
    """The text sent right after the card/video, so a draw is never a bare media.

    ``drawn_times`` 是**含这次**的抽取次数（由 :func:`draw_times` 加一得到）。
    """
    if chosen.kind == "album":
        photo: AlbumPhoto = chosen.item
        uploader = " ".join(str(photo.uploader_name or "群友").split())[:16] or "群友"
        subject = "一段视频" if chosen.video_ref else "一张照片"
        lines = [
            f"可莉从群相册里捞到一个漂流瓶，里面是{subject}！",
            f"· 来自《{photo.album_name or '未命名相册'}》",
            f"· 由 {uploader} 上传"
            + (f"（{format_local(photo.upload_time, '%m-%d %H:%M')}）" if photo.upload_time else ""),
            f"· 本群相册一共有 {album_total} 项",
        ]
    else:
        item = chosen.item
        sender = " ".join(str(item.get("sender_nick") or "群友").split())[:16] or "群友"
        operator = " ".join(str(item.get("operator_nick") or "群友").split())[:16] or "群友"
        if chosen.video_ref:
            subject = "一段视频"
        elif chosen.file_parts:
            subject = "一个文件"
        else:
            subject = "一条精华消息"
        lines = [f"可莉捞上来一个漂流瓶，里面是{subject}！"]
        if chosen.file_parts:
            for index, part in enumerate(chosen.file_parts[:3]):
                size_text = file_size_text(part)
                lines.append(
                    f"· 文件：{file_name(part, index)}"
                    + (f"（{size_text}）" if size_text else "")
                )
            if len(chosen.file_parts) > 3:
                lines.append(f"· 另有 {len(chosen.file_parts) - 3} 个文件")
        lines += [
            f"· 出自 {sender}",
            f"· 由 {operator} 设为精华（{format_local(item.get('operator_time') or 0, '%m-%d %H:%M')}）",
            f"· 本群一共有 {essence_total} 条精华",
        ]
    if drawn_times > 0:
        lines.append(
            f"· 近 {ESSENCE_DRAW_RETENTION_DAYS} 天里，这条内容被捞到过 {drawn_times} 次（含这次）"
        )
    if chosen.drawn_count:
        lines.append(f"· 这是今天捞起的第 {chosen.drawn_count + 1} 个，今天不会重样")
    if remaining > 0:
        lines.append(f"今天你还能捞 {remaining} 次哦～")
    else:
        lines.append("今天的次数用完啦，明天再来吧～")
    return "\n".join(lines)


def leaderboard_text(
    items: list[dict[str, Any]], photos: list[AlbumPhoto]
) -> str:
    """丢漂流瓶榜：谁的内容被装进瓶子最多，以及谁丢瓶子最勤。

    「丢瓶子」既包括把消息设为精华，也包括往群相册上传照片——两种动作都是往
    池子里放东西；对应地，「被丢进瓶子」对精华是原作者，对相册照片是上传者。
    """
    if not items and not photos:
        return empty_bottle_text()

    bottled: Counter[str] = Counter()      # 内容被装进瓶子的人
    throwers: Counter[str] = Counter()     # 往池子里丢东西的人
    names: dict[str, str] = {}
    unattributed = 0

    def note(key: str, name: str) -> str:
        cleaned = " ".join(str(name or "群友").split())[:16] or "群友"
        names.setdefault(key, cleaned)
        return key

    for item in items:
        author = str(item.get("sender_id") or item.get("sender_nick") or "群友")
        bottled[note(author, item.get("sender_nick"))] += 1
        operator = str(item.get("operator_id") or item.get("operator_nick") or "群友")
        throwers[note(operator, item.get("operator_nick"))] += 1

    for photo in photos:
        if not photo.uploader_id:
            # 解析不到上传者就不能归到任何人名下，只计入总数。
            unattributed += 1
            continue
        who = note(photo.uploader_id, photo.uploader_name)
        bottled[who] += 1
        throwers[who] += 1

    def bullets(counter: Counter[str], limit: int) -> list[str]:
        return [
            f"{index}. {names.get(key, '群友')} —— {count} 个"
            for index, (key, count) in enumerate(counter.most_common(limit), start=1)
        ]

    total = len(items) + len(photos)
    lines = [
        f"可莉数了数本群的漂流瓶，一共 {total} 个"
        f"（精华 {len(items)} 条 + 相册照片 {len(photos)} 张）～",
        "",
        "【丢漂流瓶榜】",
        "",
        "【被丢进瓶子里最多的人】",
    ]
    lines += [f"· {line}" for line in bullets(bottled, 10)] or ["· 还没有人被丢进瓶子"]
    lines += ["", "【最爱丢漂流瓶的人】", "（设精华 + 往相册传照片，合并计算）"]
    lines += [f"· {line}" for line in bullets(throwers, 5)] or ["· 还没有人丢过瓶子"]
    if unattributed:
        lines.append("")
        lines.append(f"（另有 {unattributed} 张相册照片认不出上传者，未计入个人榜）")
    lines.append("")
    lines.append("（只统计现在还留在精华列表与群相册里的内容哦）")
    return "\n".join(lines)


async def send_bottle(
    bot: Bot, event: GroupMessageEvent, *, album_only: bool = False
) -> None:
    """Everyone's one-minute, five-a-day lucky dip.

    ``album_only`` covers the ``漂流瓶 相册`` form; quota and cooldown are shared
    with the mixed form because it is still one bottle.
    """
    group_id = int(event.group_id)
    verdict = await claim_essence_draw(group_id, event.user_id)
    if verdict.kind == "cooldown":
        # One notice per cooldown window, then silence.
        if await claim_cooldown_notice(group_id, event.user_id, verdict.cooldown_key):
            await bot.send_group_msg(
                group_id=group_id, message=cooldown_text(verdict.wait_seconds)
            )
        return
    if verdict.kind == "daily_limit":
        if await mark_limit_notified(group_id, event.user_id):
            if album_only:
                total = await album_pool_size(bot, group_id)
            else:
                essence, album = await pool_sizes(bot, group_id)
                total = essence + album
            await bot.send_group_msg(group_id=group_id, message=daily_limit_text(total))
        return

    day = local_day_key()
    chosen = await choose_bottle(bot, group_id, day=day, album_only=album_only)
    if album_only:
        # 只要相册时不必为了报个数去拉精华列表（冷缓存下那是 1-3 秒）。
        essence_total, album_total = 0, await album_pool_size(bot, group_id)
    else:
        essence_total, album_total = await pool_sizes(bot, group_id)

    if chosen.item is None:
        await refund_essence_draw(group_id, event.user_id)
        if album_only:
            if not album_pool_enabled():
                message = album_disabled_text()
            elif not album_total:
                message = album_empty_text()
            elif chosen.reason == "exhausted":
                message = album_exhausted_text(album_total)
            else:
                message = album_no_showable_text(album_total)
            await bot.send_group_msg(group_id=group_id, message=message)
            return
        if not essence_total and not album_total:
            await bot.send_group_msg(group_id=group_id, message=empty_bottle_text())
            return
        await bot.send_group_msg(
            group_id=group_id,
            message=(
                pool_exhausted_text(essence_total, album_total)
                if chosen.reason == "exhausted"
                else no_showable_text(essence_total, album_total)
            ),
        )
        return

    group_name = await group_name_for(bot, group_id, event.group_name)

    avatar_task = _avatar_warm_tasks.get(group_id)
    if avatar_task and not avatar_task.done():
        await avatar_task

    # 「被抽到次数」：含这次的次数（当天已抽过同一条则不再 +1，见 draw_times_after）。
    # 本次成功才真正落库，但卡片/提示也只在送达后才算数，所以这里按"这次算数"展示。
    pick_key = _pick_key(chosen)
    pick_label = content_label(chosen, group_id)
    drawn_times = await draw_times_after(group_id, pick_key, day)

    # 视频直接转发：不下载、不渲染卡片，元数据交给后面那条文字提示。
    if chosen.video_ref:
        try:
            result = await bot.send_group_msg(
                group_id=group_id,
                message=MessageSegment("video", {"file": chosen.video_ref}),
            )
            if isinstance(result, dict) and result.get("status") == "failed":
                raise RuntimeError("OneBot failed to send the bottle video")
        except Exception:
            logger.exception("Could not forward the bottle video")
            await refund_essence_draw(group_id, event.user_id)
            await bot.send_group_msg(
                group_id=group_id,
                message="这段视频可莉没能转发出去，次数已经还给你啦，等会儿再试一次吧。",
            )
            return
        await record_draw(group_id, pick_key, day, label=pick_label)
        await bot.send_group_msg(
            group_id=group_id,
            message=bottle_hint_text(
                chosen._replace(drawn_count=chosen.drawn_count + 1),
                essence_total,
                album_total,
                verdict.remaining,
                drawn_times=drawn_times,
            ),
        )
        return

    # 文件类精华（含以文件形式发送的视频）：直接发原文件，不做卡片。
    if chosen.file_parts:
        try:
            await upload_bottle_files(bot, group_id, chosen)
        except Exception:
            logger.exception("Could not send the bottle files")
            await refund_essence_draw(group_id, event.user_id)
            await bot.send_group_msg(
                group_id=group_id,
                message="这个文件可莉没能发出去，次数已经还给你啦，等会儿再试一次吧。",
            )
            return
        await record_draw(group_id, pick_key, day, label=pick_label)
        await bot.send_group_msg(
            group_id=group_id,
            message=bottle_hint_text(
                chosen._replace(drawn_count=chosen.drawn_count + 1),
                essence_total,
                album_total,
                verdict.remaining,
                drawn_times=drawn_times,
            ),
        )
        return

    # 精华卡片要作者的群头像；相册卡片要上传者的。
    if chosen.kind == "essence" and isinstance(chosen.item, dict):
        content = chosen.item.get("content")
        if isinstance(content, list):
            preview = await load_reply_preview(group_id, content)
            if preview is not None:
                chosen.item["_reply_preview"] = preview
            forward_previews = await load_forward_previews(bot, [content])
            forward_ids_for_item = [
                str(segment.get("data", {}).get("id"))
                for segment in content
                if isinstance(segment, dict) and segment.get("type") == "forward"
            ]
            if forward_ids_for_item and forward_ids_for_item[0] in forward_previews:
                chosen.item["_forward_preview"] = forward_previews[forward_ids_for_item[0]]

    card_user_id = (
        str(chosen.item.uploader_id)
        if chosen.kind == "album"
        else str(chosen.item.get("sender_id") or "")
    )
    if card_user_id:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                await cache_avatar(group_id, card_user_id, client)
        except Exception:
            logger.debug("Could not cache bottle sender avatar", exc_info=True)

    path = None
    try:
        # 渲染是纯 CPU 活（960px 卡片实测 157–455 ms），放线程里跑，别占住事件循环
        # ——否则这段时间内 NapCat 的其它事件（心跳、别的群消息）只能排队。
        path = await asyncio.to_thread(
            render_bottle_card,
            chosen,
            group_id,
            group_name,
            essence_total=essence_total,
            album_total=album_total,
            drawn_times=drawn_times,
        )
        result = await bot.send_group_msg(
            group_id=group_id, message=MessageSegment.image(path.resolve().as_uri())
        )
        if isinstance(result, dict) and result.get("status") == "failed":
            raise RuntimeError("OneBot failed to send the bottle card")
    except Exception:
        logger.exception("Could not send the bottle card")
        await refund_essence_draw(group_id, event.user_id)
        await bot.send_group_msg(
            group_id=group_id,
            message="这个漂流瓶可莉没画好，次数已经还给你啦，等会儿再试一次吧。",
        )
        return
    finally:
        if path is not None:
            path.unlink(missing_ok=True)

    # Only a delivered card counts as drawn, so a failed send can be retried.
    await record_draw(group_id, pick_key, day, label=pick_label)
    drawn_total = chosen.drawn_count + 1
    chosen = chosen._replace(drawn_count=drawn_total)
    await bot.send_group_msg(
        group_id=group_id,
        message=bottle_hint_text(
            chosen,
            essence_total,
            album_total,
            verdict.remaining,
            drawn_times=drawn_times,
        ),
    )


async def send_drawn_content_leaderboard(
    bot: Bot, event: GroupMessageEvent, *, limit: int = 10
) -> None:
    """「被抽到榜」TOP10 —— 以卡片图片发出。

    数据来自 ``drawn_content_leaderboard()``（按「被捞到过几次」倒序，同次数则最近
    被捞到的在前）。注意次数受 ``BOTTLE_HISTORY_DAYS`` 限制，所以卡片上写「近 N 天」。
    """
    group_id = int(event.group_id)
    entries = await drawn_content_leaderboard(group_id, limit=limit)
    if not entries:
        await bot.send_group_msg(
            group_id=group_id,
            message=f"本群最近 {ESSENCE_DRAW_RETENTION_DAYS} 天还没有捞瓶记录，先捞几个漂流瓶再来看看吧～",
        )
        return

    group_name = await group_name_for(bot, group_id, event.group_name)

    # 回查具体的消息内容与相册照片，榜单才不是一串编号。
    cards = await resolve_drawn_content(bot, group_id, entries)

    path = None
    try:
        path = await asyncio.to_thread(
            render_drawn_leaderboard_card, cards, group_id, group_name
        )
        result = await bot.send_group_msg(
            group_id=group_id, message=MessageSegment.image(path.resolve().as_uri())
        )
        if isinstance(result, dict) and result.get("status") == "failed":
            raise RuntimeError("OneBot failed to send the drawn-content leaderboard")
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


async def safe_send_drawn_content_leaderboard(
    bot: Bot, event: GroupMessageEvent
) -> None:
    await guard_group(
        "Drawn-content leaderboard handler",
        bot,
        int(event.group_id),
        lambda: send_drawn_content_leaderboard(bot, event),
        "可莉数榜单的时候数晕了，等会儿再来吧；错误已经记进日志啦。",
    )


async def send_bottle_leaderboard(bot: Bot, event: GroupMessageEvent) -> None:
    group_id = int(event.group_id)
    essence_total, album_total = await pool_sizes(bot, group_id)
    if not essence_total and not album_total:
        # 读不到和真的空都落到这里；前者已在 pool_sizes 里记过日志。
        await bot.send_group_msg(group_id=group_id, message=empty_bottle_text())
        return
    try:
        items = await fetch_essence_list(bot, group_id)
    except EssenceUnavailable:
        items = []
    try:
        photos = await fetch_album_photos(bot, group_id) if BOTTLE_ALBUM_ENABLED else []
    except AlbumUnavailable:
        photos = []
    await bot.send_group_msg(
        group_id=group_id, message=leaderboard_text(items, photos)
    )


async def safe_send_essence(
    bot: Bot, event: GroupMessageEvent, *, album_only: bool = False
) -> None:
    await guard_group(
        "Bottle draw handler",
        bot,
        int(event.group_id),
        lambda: send_bottle(bot, event, album_only=album_only),
        "可莉捞漂流瓶的时候摔了一跤，等会儿再来吧；错误已经记进日志啦。",
    )


async def safe_send_essence_leaderboard(bot: Bot, event: GroupMessageEvent) -> None:
    await guard_group(
        "Bottle leaderboard handler",
        bot,
        int(event.group_id),
        lambda: send_bottle_leaderboard(bot, event),
        "可莉数漂流瓶的时候数晕了，等会儿再来吧；错误已经记进日志啦。",
    )
