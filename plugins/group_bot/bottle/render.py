"""漂流瓶的卡片渲染：单条卡片、被抽到榜卡片，以及把榜单键回查成现行内容。

只依赖 `card`（几何与绘制）、`bottle_pool`（内容提取）与 `bottle_stats`（榜单数据）。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
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
from ..common.card import CardEntry, CardQuote, render_card_page
from ..core.config import (
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
from ..common.replies import ReplyPreview, load_forward_previews, load_reply_preview
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
    _album_key,
    _essence_key,
    essence_text,
    fetch_essence_list,
    format_local,
    materialize_images,
)
from .stats import DrawnContent, drawn_rank_label


LEADERBOARD_IMAGE_BUDGET = 3


LEADERBOARD_TEXT_CHARS = 120


LEADERBOARD_THUMB_SIZE = 260


class DrawnContentCard(NamedTuple):
    """榜单里的一行：抽取记录 + 回查到的现行内容。"""

    entry: DrawnContent
    rank: int
    name: str  # 发送者 / 上传者；原内容已不在池子里时退回落库的 label
    source: str  # 来源说明（精华 / 群相册《X》），或在缺失时的说明
    text: str  # 正文（相册没有正文）
    image: Path | None
    extra_images: int = 0
    missing: bool = False
    quote: CardQuote | None = None


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


def render_bottle_card(
    chosen: ChosenBottle,
    group_id: int,
    group_name: str,
    *,
    essence_total: int,
    album_total: int,
    drawn_times: int = 0,
) -> Path:
    """Render one already-materialised pick as a one-row card.

    ``drawn_times`` 含这次；>0 时在副标题里标出「已被捞 N 次」。
    """
    subtitle = f"{(group_name or '本群')[:24]}"
    if drawn_times > 0:
        subtitle += f"  ·  已被捞 {drawn_times} 次"
    if chosen.kind == "album":
        photo: AlbumPhoto = chosen.item
        subtitle += f"  ·  群相册共 {album_total} 张"
        entry = CardEntry(
            user_id=photo.uploader_id,
            name=photo.uploader_name,
            right_text=format_local(photo.upload_time, "%Y-%m-%d %H:%M"),
            meta_text=f"来自群相册《{(photo.album_name or '未命名')[:16]}》",
            text="",
            image_paths=list(chosen.image_paths)[:4],
            image_note="",
            extra_images=max(0, len(chosen.image_paths) - 4),
        )
        return render_card_page("漂流瓶", subtitle, [entry], group_id=group_id)

    item = chosen.item
    operator = str(item.get("operator_nick") or item.get("operator_id") or "群友")
    subtitle += f"  ·  精华共 {essence_total} 条"
    if chosen.drawn_count:
        subtitle += f"  ·  今日第 {chosen.drawn_count + 1} 个"
    entry = CardEntry(
        user_id=str(item.get("sender_id") or ""),
        name=str(item.get("sender_nick") or ""),
        right_text=format_local(item.get("operator_time") or 0, "%Y-%m-%d %H:%M"),
        meta_text=f"由 {operator} 设为精华",
        text=essence_text(item, group_id),
        image_paths=list(chosen.image_paths)[:4],
        image_note="",
        extra_images=max(0, len(chosen.image_paths) - 4),
        quote=_card_quote(item.get("_reply_preview") or item.get("_forward_preview")),
    )
    return render_card_page("漂流瓶", subtitle, [entry], group_id=group_id)


def render_drawn_leaderboard_card(
    cards: list[DrawnContentCard],
    group_id: int,
    group_name: str,
    *,
    covered_days: int = 0,
) -> Path:
    """把「被抽到榜」渲染成一张卡片图（每行是内容本身 + 名次次数）。"""
    days = covered_days or ESSENCE_DRAW_RETENTION_DAYS
    subtitle = f"{(group_name or '本群')[:16]}  ·  近 {days} 天  ·  TOP {len(cards)}"
    rows: list[CardEntry] = []
    for card in cards:
        meta_parts = [card.source]
        try:
            stamp = datetime.fromisoformat(
                str(card.entry.last_drawn_at).replace("Z", "+00:00")
            ).timestamp()
            meta_parts.append(f"上次被捞到 {format_local(stamp, '%m-%d %H:%M')}")
        except (ValueError, TypeError):
            pass
        rows.append(
            CardEntry(
                user_id="",  # 榜单没有"作者头像"，头像位置退化为按名字取首字
                # 名次放在右侧而不是标题里：标题若以 "10." 开头，头像圆圈取首字符会显示成
                # "1"，看着像第 1 名。
                name=card.name,
                right_text=f"第 {card.rank} 名  ·  {card.entry.times} 次",
                meta_text="  ·  ".join(part for part in meta_parts if part),
                text=card.text,
                image_paths=[card.image] if card.image else [],
                extra_images=card.extra_images,
                quote=card.quote,
            )
        )
    return render_card_page(
        "被抽到榜", subtitle, rows, group_id=group_id, thumb_max=LEADERBOARD_THUMB_SIZE
    )


async def resolve_drawn_content(
    bot: Bot,
    group_id: int,
    entries: list[DrawnContent],
    *,
    image_budget: int = LEADERBOARD_IMAGE_BUDGET,
) -> list[DrawnContentCard]:
    """把榜单的键回查成「具体消息与各种信息」。

    精华与群相册各拉一次列表（相册本身有内存缓存），按 ``_essence_key`` /
    ``_album_key`` 对应到抽取记录上。**原内容可能早已被移出精华列表或被删除**，这时
    ``missing=True``，卡片退回显示抽取时落库的 ``label``。

    图片只下载前 ``image_budget`` 条：每张都要走网络与缓存，而且失败不能影响整张卡片
    —— ``materialize_*`` 本身不抛异常，失败就当作没有图。
    """
    essence_by_key: dict[str, dict[str, Any]] = {}
    try:
        for item in await fetch_essence_list(bot, group_id):
            essence_by_key[_essence_key(item)] = item
    except EssenceUnavailable:
        logger.info("Leaderboard: essence list unavailable; rows will fall back to labels")

    album_by_key: dict[str, AlbumPhoto] = {}
    if BOTTLE_ALBUM_ENABLED:
        try:
            for photo in await fetch_album_photos(bot, group_id):
                album_by_key[_album_key(photo)] = photo
        except AlbumUnavailable:
            logger.info("Leaderboard: album list unavailable; rows will fall back to labels")

    cards: list[DrawnContentCard] = []
    budget = max(0, image_budget)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for rank, entry in enumerate(entries, start=1):
            if entry.kind == "album":
                photo = album_by_key.get(entry.key)
                if photo is None:
                    cards.append(
                        DrawnContentCard(
                            entry, rank, drawn_rank_label(entry), "原照片已不在群相册里",
                            "", None, 0, True,
                        )
                    )
                    continue
                uploader = " ".join(str(photo.uploader_name or "群友").split())[:16] or "群友"
                album = " ".join(str(photo.album_name or "未命名相册").split())[:12] or "未命名相册"
                source = f"群相册《{album}》"
                text = "（这段漂流瓶里是一段视频）" if photo.kind == "video" else ""
                image = None
                if budget and photo.kind == "image":
                    image = await materialize_photo(photo, group_id, client)
                    if image is not None:
                        budget -= 1
                cards.append(
                    DrawnContentCard(entry, rank, uploader, source, text, image)
                )
                continue

            item = essence_by_key.get(entry.key)
            if item is None:
                cards.append(
                    DrawnContentCard(
                        entry, rank, drawn_rank_label(entry), "原消息已不在精华列表里",
                        "", None, 0, True,
                    )
                )
                continue
            sender = " ".join(str(item.get("sender_nick") or "群友").split())[:16] or "群友"
            operator = " ".join(str(item.get("operator_nick") or "").split())[:16]
            source = f"精华 · 由 {operator} 设为精华" if operator else "精华消息"
            content = item.get("content")
            if isinstance(content, list):
                preview = await load_reply_preview(group_id, content)
                if preview is not None:
                    item["_reply_preview"] = preview
                forward_previews = await load_forward_previews(bot, [content])
                forward_ids_for_item = [
                    str(segment.get("data", {}).get("id"))
                    for segment in content
                    if isinstance(segment, dict) and segment.get("type") == "forward"
                ]
                if forward_ids_for_item and forward_ids_for_item[0] in forward_previews:
                    item["_forward_preview"] = forward_previews[forward_ids_for_item[0]]
            body = " ".join(essence_text(item, group_id).split())
            image = None
            extra = 0
            if budget:
                paths = await materialize_images(bot, group_id, item)
                if paths:
                    image = paths[0]
                    extra = max(0, len(paths) - 1)
                    budget -= 1
            cards.append(
                DrawnContentCard(
                    entry,
                    rank,
                    sender,
                    source,
                    body[:LEADERBOARD_TEXT_CHARS] + ("…" if len(body) > LEADERBOARD_TEXT_CHARS else ""),
                    image,
                    extra,
                    False,
                    _card_quote(item.get("_reply_preview") or item.get("_forward_preview")),
                )
            )
    return cards
