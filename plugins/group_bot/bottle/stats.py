"""漂流瓶的统计：被抽到次数与被抽到榜的数据层。

只读 `essence_draws`，不参与抽取与渲染。
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
from ..common.card import CardEntry, render_card_page
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
from ..core.state import (
    _album_broken,
    _avatar_warm_tasks,
    _essence_broken,
    _essence_cache,
    _essence_locks,
    _member_names,
)
from ..core.store import connect

from .pool import ESSENCE_DRAW_RETENTION_DAYS, essence_text, kind_of_drawn_key


class DrawnContent(NamedTuple):
    """「被抽到榜」的一条记录。"""

    key: str  # essence_draws.message_id：精华是消息号，相册带 album: 前缀
    kind: str  # "essence" | "album"
    times: int
    label: str
    last_drawn_at: str


async def draw_times_after(group_id: int, message_id: str, day: str) -> int:
    """**含本次**的抽取次数，用于展示。

    不能简单地用 ``draw_times() + 1``：同一天重复抽到同一条内容时，记录是
    ``(group_id, day, message_id)`` 上的 upsert，行数不会增加，展示却会多算一次。
    正常路径下当天不会重样（抓取时会过滤掉今天已抽过的），但这个边界必须处理对，
    否则一旦出现"发出去但没记上"之类的重试，提示里的次数就会虚高。
    """
    async with connect() as db:
        cursor = await db.execute(
            """SELECT COUNT(*) AS times,
                      MAX(CASE WHEN day = ? THEN 1 ELSE 0 END) AS drawn_today
                 FROM essence_draws WHERE group_id=? AND message_id=?""",
            (day, str(group_id), str(message_id)),
        )
        row = await cursor.fetchone()
    if row is None:
        return 1
    times = int(row["times"])
    return times if int(row["drawn_today"] or 0) else times + 1


async def draw_times(group_id: int, message_id: str) -> int:
    """这条内容在本群被捞到过几次。

    ⚠️ 两个边界，展示时必须如实说明：
    - ``essence_draws`` 只保留 ``ESSENCE_DRAW_RETENTION_DAYS`` 天，所以这是
      **近 N 天内**的次数，不是历史总数；
    - 同一内容每天最多被捞一次（主键含 day，且当天已抽过的会被过滤掉），
      因此这个数字实际等于「被捞到过多少天」。
    """
    async with connect() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) AS times FROM essence_draws WHERE group_id=? AND message_id=?",
            (str(group_id), str(message_id)),
        )
        row = await cursor.fetchone()
        return int(row["times"]) if row else 0


def content_label(chosen: ChosenBottle, group_id: int) -> str:
    """给「被抽到榜」用的一行人类可读标签（不超过 80 字）。"""
    if chosen.kind == "album":
        photo: AlbumPhoto = chosen.item
        album = " ".join(str(photo.album_name or "未命名相册").split())[:24] or "未命名相册"
        uploader = " ".join(str(photo.uploader_name or "群友").split())[:16] or "群友"
        return f"群相册《{album}》里 {uploader} 的照片"[:80]
    item = chosen.item
    sender = " ".join(str(item.get("sender_nick") or "群友").split())[:16] or "群友"
    snippet = " ".join(essence_text(item, group_id).split())[:40]
    return f"{sender}：{snippet}"[:80] if snippet else f"{sender} 的精华消息"


def drawn_rank_label(entry: DrawnContent, *, width: int = 30) -> str:
    """榜单一行的内容名。

    优先用抽取时落库的 ``label``；早期记录没有 label（那一列是后加的），回退成通用名，
    至少不显示一串裸消息号。
    """
    label = " ".join(str(entry.label or "").split())
    if not label:
        label = (
            "群相册里的一张照片"
            if entry.kind == "album"
            else f"精华消息 #{entry.key.split(':')[-1][:12]}"
        )
    return label[:width]


async def drawn_content_leaderboard(
    group_id: int, *, limit: int = 10
) -> list[DrawnContent]:
    """【预留接口】被抽到榜的数据层 —— 已实现，**尚未挂任何命令**。

    按「这条内容被捞到过几次」倒序返回，次数相同则最近被捞到的在前。

    将来要开放查看时，只需两步（都不必改这里）：
    1. `handlers.py`：在命令集合里加一个词（例如「被抽到榜」），路由到新函数；
    2. `essence.py`：加一个薄包装，形如
       ``async def send_drawn_content_leaderboard(bot, event)``，
       内部 ``entries = await drawn_content_leaderboard(group_id)`` 再
       ``bot.send_group_msg(message=drawn_content_leaderboard_text(entries))``。

    注意数据边界见 :func:`draw_times`：只覆盖最近 ``ESSENCE_DRAW_RETENTION_DAYS`` 天。
    """
    async with connect() as db:
        cursor = await db.execute(
            """SELECT message_id,
                      COUNT(*) AS times,
                      MAX(drawn_at) AS last_drawn_at,
                      (SELECT label FROM essence_draws AS inner
                        WHERE inner.group_id = outer.group_id
                          AND inner.message_id = outer.message_id
                          AND inner.label <> ''
                        -- 按「逻辑抽取日」优先、写入时刻次之：drawn_at 是写入时间，
                        -- 补写历史日期时它不代表那次抽取真正发生在哪天。
                        ORDER BY inner.day DESC, inner.drawn_at DESC LIMIT 1) AS label
                 FROM essence_draws AS outer
                WHERE group_id = ?
                GROUP BY message_id
                ORDER BY times DESC, last_drawn_at DESC
                LIMIT ?""",
            (str(group_id), int(max(1, limit))),
        )
        rows = await cursor.fetchall()
    return [
        DrawnContent(
            key=str(row["message_id"]),
            kind=kind_of_drawn_key(str(row["message_id"])),
            times=int(row["times"]),
            label=str(row["label"] or ""),
            last_drawn_at=str(row["last_drawn_at"] or ""),
        )
        for row in rows
    ]


def drawn_content_leaderboard_text(entries: list[DrawnContent]) -> str:
    """【预留接口】把被抽到榜排成群消息文案 —— 同样尚未挂命令。"""
    if not entries:
        return "本群还没有可统计的捞瓶记录～"
    lines = [
        "【被抽到榜】",
        f"（近 {ESSENCE_DRAW_RETENTION_DAYS} 天，同一内容每天最多被捞到一次）",
        "",
    ]
    for index, entry in enumerate(entries, start=1):
        label = entry.label or (
            "群相册里的一张照片" if entry.kind == "album" else f"精华消息 #{entry.key}"
        )
        lines.append(f"{index}. {label} —— {entry.times} 次")
    return "\n".join(lines)
