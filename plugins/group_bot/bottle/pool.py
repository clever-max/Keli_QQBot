"""漂流瓶的池子与选取：拉取、缓存、键空间、抽样与防重复。

同时托管几个共用小工具（`local_day_key` / `format_local` / 内容文本提取），因为它们是
这一层的最底层依赖，被 stats / render / flow 共用。

分层约定：本模块**不导入**同目录的 bottle_stats / bottle_render / bottle_flow，
以保证依赖单向（pool → stats → render → flow）。
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
    ALLOWED_GROUP_IDS,
    BOTTLE_ALBUM_CACHE_SECONDS,
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
    _album_cache,
    _avatar_warm_tasks,
    _essence_broken,
    _essence_cache,
    _essence_locks,
    _member_names,
)
from ..core.store import connect


ESSENCE_IMAGE_SUFFIXES = (".myqcloud.com",)


ESSENCE_IMAGE_ATTEMPTS = 6


ESSENCE_DRAW_RETENTION_DAYS = BOTTLE_HISTORY_DAYS


class EssenceUnavailable(RuntimeError):
    """The essence list could not be read (API failure or unsupported action)."""


def local_day_key(now: datetime | None = None) -> str:
    return (now or datetime.now(GROUP_TIMEZONE)).astimezone(GROUP_TIMEZONE).date().isoformat()


def format_local(epoch_seconds: int | float, pattern: str = "%Y-%m-%d %H:%M") -> str:
    try:
        return datetime.fromtimestamp(float(epoch_seconds), GROUP_TIMEZONE).strftime(pattern)
    except (OSError, OverflowError, TypeError, ValueError):
        return ""


async def fetch_essence_list(bot: Bot, group_id: int) -> list[dict[str, Any]]:
    """Return the group's essence list, served from a short in-memory cache."""
    cached = _essence_cache.get(group_id)
    now = time.monotonic()
    if cached and now - cached[0] < ESSENCE_CACHE_SECONDS:
        return cached[1]

    lock = _essence_locks.setdefault(group_id, asyncio.Lock())
    async with lock:
        cached = _essence_cache.get(group_id)
        now = time.monotonic()
        if cached and now - cached[0] < ESSENCE_CACHE_SECONDS:
            return cached[1]
        try:
            response = await bot.call_api("get_essence_msg_list", group_id=group_id)
        except Exception as exc:  # noqa: BLE001 - surfaced to the group as a notice
            logger.warning("get_essence_msg_list failed for group {}", group_id, exc_info=True)
            raise EssenceUnavailable(str(exc)) from exc

        data = response.get("data", response) if isinstance(response, dict) else response
        items = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        _essence_cache[group_id] = (now, items)
        logger.info("Cached {} essence messages for group {}", len(items), group_id)
        return items


async def drawn_message_ids(group_id: int, day: str) -> set[str]:
    """Essence message ids already drawn in this group on this local day."""
    async with connect() as db:
        cursor = await db.execute(
            "SELECT message_id FROM essence_draws WHERE group_id=? AND day=?",
            (str(group_id), day),
        )
        return {str(row["message_id"]) for row in await cursor.fetchall()}


async def record_draw(
    group_id: int, message_id: str, day: str, *, label: str = ""
) -> None:
    """记一次成功抽取。

    ``label`` 是这条内容的人类可读名字，落库是为了将来的「被抽到榜」——精华列表是
    实时从 QQ 拉的，几个月后原来那条消息可能早就没了，只剩 message_id 就说不清榜单
    上是什么内容。同一天重复记同一条时只刷新 label，不改 ``drawn_at``。
    """
    async with connect() as db:
        await db.execute(
            """INSERT INTO essence_draws(group_id, day, message_id, drawn_at, label)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(group_id, day, message_id) DO UPDATE SET label=excluded.label""",
            (
                str(group_id),
                day,
                str(message_id),
                datetime.now(timezone.utc).isoformat(),
                (label or "")[:80],
            ),
        )
        cutoff = (
            (datetime.now(GROUP_TIMEZONE) - timedelta(days=ESSENCE_DRAW_RETENTION_DAYS))
            .date()
            .isoformat()
        )
        await db.execute("DELETE FROM essence_draws WHERE day < ?", (cutoff,))
        await db.commit()


class ChosenBottle(NamedTuple):
    """One pick from the mixed pool.

    ``item`` is an essence dict when ``kind == "essence"`` and an
    :class:`AlbumPhoto` when ``kind == "album"``.

    A non-empty ``video_ref`` means this pick is a **video**: it is forwarded as-is
    with a ``video`` message segment and no card is rendered for it.

    A non-empty ``file_parts`` means the essence is a **file message** (including a
    video sent as a file): the files are uploaded/sent as-is instead of a card.
    """

    kind: str  # "essence" | "album"
    item: Any | None
    image_paths: list[Path]
    drawn_count: int
    reason: str  # "ok" | "exhausted" | "unavailable"
    pool_total: int = 0  # size of the pool this pick (or failure) came from
    video_ref: str = ""  # playable video reference when this pick is a video
    file_parts: tuple[dict[str, Any], ...] = ()  # file segment data when this pick is a file


def _essence_key(item: dict[str, Any]) -> str:
    return str(item.get("message_id"))


def _album_key(photo: AlbumPhoto) -> str:
    # 相册抽取与精华共用一张「今天抽过什么」的表，用前缀把两个池子的键隔开。
    return f"album:{photo.media_id}"


def kind_of_drawn_key(drawn_key: str) -> str:
    """把 ``essence_draws.message_id`` 还原成它属于哪个池子。"""
    return "album" if drawn_key.startswith("album:") else "essence"


def _pick_key(chosen: ChosenBottle) -> str:
    return (
        _album_key(chosen.item)
        if chosen.kind == "album"
        else str(chosen.item.get("message_id"))
    )


def resolve_mentions(text: str, group_id: int) -> str:
    """Replace the ``@123456`` placeholders render_message produces with names."""
    names = _member_names.get(group_id, {})

    def swap(match: re.Match[str]) -> str:
        qq = match.group(1)
        return "@" + (names.get(qq) or "群友")

    return re.sub(r"@(\d{5,})", swap, text)


def essence_text(item: dict[str, Any], group_id: int) -> str:
    raw = item.get("content")
    if not isinstance(raw, list):
        return ""
    return resolve_mentions(
        render_message(
            raw,
            omit_images=True,
            omit_replies=item.get("_reply_preview") is not None,
            omit_forwards=item.get("_forward_preview") is not None,
        ),
        group_id,
    )


def essence_image_segments(item: dict[str, Any]) -> list[dict[str, Any]]:
    raw = item.get("content")
    if not isinstance(raw, list):
        return []
    return [segment for segment in raw if isinstance(segment, dict) and segment.get("type") == "image"]


def essence_file_parts(item: dict[str, Any]) -> list[dict[str, Any]]:
    """精华消息里所有 ``file`` 段的数据（视频以文件形式发送时也在这里）。

    返回的是 ``segment["data"]`` 的副本，调用方只读；文件名/大小/下载地址都在里面。
    """
    raw = item.get("content")
    if not isinstance(raw, list):
        return []
    parts: list[dict[str, Any]] = []
    for segment in raw:
        if not isinstance(segment, dict) or segment.get("type") != "file":
            continue
        data = segment.get("data")
        if isinstance(data, dict):
            parts.append(dict(data))
    return parts


def _is_playable_ref(value: str) -> bool:
    """看起来能直接交给 OneBot 当文件用的引用（URL / base64 / 绝对路径）。"""
    if value.startswith(("http://", "https://", "base64://", "file://")):
        return True
    return len(value) > 2 and value[1] == ":"  # Windows 盘符，例如 C:\...


def essence_video_ref(item: dict[str, Any]) -> str:
    """精华消息里第一段视频的可播放引用，没有视频则空串。

    优先用 ``file``（NapCat 通常已经把它落成本地路径，省一次下载），不行再用
    ``url``；两者都不是"能直接用的引用"时原样返回，交给发送失败分支兜底。
    """
    raw = item.get("content")
    if not isinstance(raw, list):
        return ""
    for segment in raw:
        if not isinstance(segment, dict) or segment.get("type") != "video":
            continue
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        local = str(data.get("file") or "").strip()
        remote = str(data.get("url") or "").strip()
        if local and _is_playable_ref(local):
            return local
        if remote:
            return remote
        if local:
            return local
    return ""


async def materialize_images(bot: Bot, group_id: int, item: dict[str, Any]) -> list[Path] | None:
    """Download every picture of one essence.

    Returns the cached paths when *all* of them are usable, ``None`` as soon as
    one is not — a card with a broken picture must never be drawn.
    """
    segments = item.get("content") if isinstance(item.get("content"), list) else []
    image_segments = [
        segment
        for segment in segments
        if isinstance(segment, dict) and segment.get("type") == "image"
    ]
    if not image_segments:
        return []
    MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        await cache_message_images(
            bot,
            group_id,
            str(item.get("message_id")),
            segments,
            client,
            ESSENCE_IMAGE_SUFFIXES,
        )
    paths = cached_image_paths_from_json(json.dumps(segments, ensure_ascii=False))
    # 一张都没取到才放弃；部分失败时用剩下的出卡片（不然一条精华里有一张坏图就永远抽不到）。
    if not paths:
        return None
    if len(paths) < len(image_segments):
        logger.warning(
            "Essence {}: only {}/{} images usable, drawing with what we have",
            item.get("message_id"),
            len(paths),
            len(image_segments),
        )
    return paths


async def draw_history(group_id: int) -> dict[str, float]:
    """每条内容**最近一次**被抽到的日期（本地零点对应的 epoch 秒），供「最久未抽优先」。

    只读 ``BOTTLE_HISTORY_DAYS`` 天内的记录——它同时是"能看多远"的上限。
    从未出现过的内容不在返回值里，调用方把它们当作最旧。

    为什么按 ``day`` 而不是 ``drawn_at`` 排序：``drawn_at`` 是**落库时刻**，补写历史
    日期时它不代表那次抽取真正发生在哪天（同一个坑在取 ``label`` 时也踩过）。而按天
    排序在这里足够精确——候选池已经排除了今天抽过的内容，所以每条候选的"上次被抽到"
    都落在更早的日期上，日期就完全决定了先后。
    """
    cutoff = (
        datetime.now(GROUP_TIMEZONE) - timedelta(days=ESSENCE_DRAW_RETENTION_DAYS)
    ).date().isoformat()
    async with connect() as db:
        cursor = await db.execute(
            """SELECT message_id, MAX(day) AS last_day
                 FROM essence_draws
                WHERE group_id=? AND day>=?
                GROUP BY message_id""",
            (str(group_id), cutoff),
        )
        rows = await cursor.fetchall()
    history: dict[str, float] = {}
    for row in rows:
        raw = str(row["last_day"] or "")
        try:
            stamp = datetime.fromisoformat(raw).replace(tzinfo=GROUP_TIMEZONE).timestamp()
        except ValueError:
            stamp = 0.0
        history[str(row["message_id"])] = stamp
    return history


def preferred_order(
    pool: list[Any],
    history: dict[str, float],
    *,
    key: Callable[[Any], str],
    mode: str | None = None,
    pick: int | None = None,
    head: int | None = None,
) -> list[Any]:
    """把候选排成"越靠前越该被抽到"的顺序。

    推荐模式 ``lru`` 用**随机化的最久未抽优先**（power of k choices）：反复从剩余候选
    里随机取 ``pick`` 个，把其中最近一次被抽到时间最早的那个放到队首。从未抽过的内容
    视为最旧，因此新内容会被优先铺开。

    为什么不用"时间衰减权重抽样"：实测它反而让重复率升高 2–3 倍——压低"刚抽过"的
    概率等于把抽样挤到"最久没抽"的尾巴子集上，有效池子变小、重复更早出现。

    只对前 ``head`` 个做严格排序（真正会被尝试的就是这几个），其余保持随机——
    避免为了完整排序做 O(n²)，也不改变"候选失败后继续往下找"的既有语义。
    """
    items = list(pool)
    if len(items) <= 1:
        return items
    if (mode or BOTTLE_REPEAT_MODE) != "lru" or (pick or BOTTLE_REPEAT_PICK_K) <= 1:
        random.shuffle(items)
        return items

    width = max(1, min(pick or BOTTLE_REPEAT_PICK_K, 50))
    head_len = min(len(items), head if head is not None else max(ESSENCE_IMAGE_ATTEMPTS, width) + 2)
    remaining = items
    ordered: list[Any] = []
    for _ in range(head_len):
        if len(remaining) <= width:
            best = min(remaining, key=lambda item: history.get(key(item), -1.0))
        else:
            candidates = random.sample(remaining, width)
            best = min(candidates, key=lambda item: history.get(key(item), -1.0))
        ordered.append(best)
        remaining = [item for item in remaining if item is not best]
    random.shuffle(remaining)
    return ordered + remaining


async def recent_drawn_kinds(group_id: int, limit: int) -> list[str]:
    """最近 ``limit`` 次成功捞取的类别，新的在前。

    直接读 ``essence_draws``（按 ``drawn_at`` 倒序），所以这个「连续同类」的记录
    跨重启依然成立，不需要额外状态。
    """
    if limit <= 0:
        return []
    async with connect() as db:
        cursor = await db.execute(
            """SELECT message_id FROM essence_draws
               WHERE group_id=? ORDER BY drawn_at DESC LIMIT ?""",
            (str(group_id), limit),
        )
        rows = await cursor.fetchall()
    return [kind_of_drawn_key(str(row["message_id"])) for row in rows]


def preferred_kind_order(streak: list[str]) -> list[str] | None:
    """连续同类时的强制顺序；没有连续同类则返回 ``None``（交给随机比例）。

    只有连续 ``BOTTLE_SWITCH_STREAK`` 次（默认 3）全都一样才触发。返回的顺序里
    被强制的那一类排在最前，另一类仍作为兜底保留——否则相册为空时就会被卡死，
    连精华也捞不到。
    """
    if BOTTLE_SWITCH_STREAK <= 0 or len(streak) < BOTTLE_SWITCH_STREAK:
        return None
    window = streak[:BOTTLE_SWITCH_STREAK]
    if len(set(window)) != 1:
        return None
    stuck = window[0]
    other = "essence" if stuck == "album" else "album"
    return [other, stuck]


async def choose_essence(
    bot: Bot,
    group_id: int,
    items: list[dict[str, Any]],
    *,
    day: str,
    attempts: int = ESSENCE_IMAGE_ATTEMPTS,
) -> ChosenBottle:
    """Pick one essence that has not been drawn today and whose pictures work."""
    drawn = await drawn_message_ids(group_id, day)
    broken = _essence_broken.setdefault(group_id, set())
    key = _essence_key

    pool = [item for item in items if key(item) not in drawn and key(item) not in broken]
    if not pool:
        remaining = {key(item) for item in items} - drawn
        reason = "unavailable" if remaining else "exhausted"
        return ChosenBottle("essence", None, [], len(drawn), reason, len(items))

    # 最久未抽的排前面（见 preferred_order）。
    candidates = preferred_order(pool, await draw_history(group_id), key=key)

    def video_pick(item: dict[str, Any]) -> ChosenBottle | None:
        """含视频的精华直接转发视频，不下载、不做卡片。"""
        ref = essence_video_ref(item)
        if not ref:
            return None
        return ChosenBottle("essence", item, [], len(drawn), "ok", len(items), ref)

    def file_pick(item: dict[str, Any]) -> ChosenBottle | None:
        """文件类精华（含以文件形式发的视频）直接发原文件，不做卡片。"""
        parts = essence_file_parts(item)
        if not parts:
            return None
        return ChosenBottle(
            "essence", item, [], len(drawn), "ok", len(items), "", tuple(parts)
        )

    for item in candidates[:attempts]:
        picked = video_pick(item)
        if picked is not None:
            return picked
        picked = file_pick(item)
        if picked is not None:
            return picked
        if not essence_image_segments(item):
            return ChosenBottle("essence", item, [], len(drawn), "ok", len(items))
        paths = await materialize_images(bot, group_id, item)
        if paths is not None:
            return ChosenBottle("essence", item, paths, len(drawn), "ok", len(items))
        broken.add(key(item))
        logger.info("Skipping essence {} : pictures unavailable", key(item))

    # Nothing with pictures worked; fall back to a text-only, file or video essence.
    for item in candidates:
        picked = video_pick(item)
        if picked is not None:
            return picked
        picked = file_pick(item)
        if picked is not None:
            return picked
        if not essence_image_segments(item):
            return ChosenBottle("essence", item, [], len(drawn), "ok", len(items))
    return ChosenBottle("essence", None, [], len(drawn), "unavailable", len(items))


async def choose_album_photo(
    bot: Bot,
    group_id: int,
    photos: list[AlbumPhoto],
    *,
    day: str,
    attempts: int = ESSENCE_IMAGE_ATTEMPTS,
) -> ChosenBottle:
    """Pick one album photo that has not been drawn today and downloads fine.

    A photo whose picture cannot be fetched is remembered and skipped from then
    on, exactly like a broken essence.
    """
    drawn = await drawn_message_ids(group_id, day)
    broken = _album_broken.setdefault(group_id, set())
    key = _album_key

    pool = [photo for photo in photos if key(photo) not in drawn and key(photo) not in broken]
    if not pool:
        remaining = {key(photo) for photo in photos} - drawn
        reason = "unavailable" if remaining else "exhausted"
        return ChosenBottle("album", None, [], len(drawn), reason, len(photos))

    candidates = preferred_order(pool, await draw_history(group_id), key=key)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for photo in candidates[:attempts]:
            if photo.kind == "video":
                # 视频直接转发，不下载也不做卡片；因此永远不会进"坏图"集合。
                return ChosenBottle(
                    "album", photo, [], len(drawn), "ok", len(photos), photo.url
                )
            path = await materialize_photo(photo, group_id, client)
            if path is not None:
                return ChosenBottle("album", photo, [path], len(drawn), "ok", len(photos))
            # 坏图集合必须和 drawn 用同一套键（带 album: 前缀），否则筛选永远命不中，
            # 这张照片每次都会被重新下载一遍。
            mark_broken(group_id, key(photo))
            logger.info("Skipping album photo {} : picture unavailable", photo.media_id)
    return ChosenBottle("album", None, [], len(drawn), "unavailable", len(photos))


async def choose_bottle(
    bot: Bot, group_id: int, *, day: str, album_only: bool = False
) -> ChosenBottle:
    """Pick from the mixed pool: 精华消息 plus 群相册照片.

    ``album_only`` is the explicit ``漂流瓶 相册`` form: it goes to the album and
    **only** the album — no compensation, no fallback to essences.  An explicit
    request must not be silently answered with something else.

    Otherwise the kind is decided in this order of precedence:

    1. ``BOTTLE_ALBUM_ENABLED`` / ``BOTTLE_ALBUM_SHARE`` —— 相册这一半被关掉
       （开关为 0，或占比为 0）时就只抽精华，**下面的补偿也不生效**：配置说
       "不要相册"就该是不要，不能被防单调的逻辑重新打开。
    2. **连续同类补偿** —— 最近 ``BOTTLE_SWITCH_STREAK`` 次（默认 3）都捞到同一类
       时，强制先试另一类，免得一直是精华或一直是相册。
    3. 否则按 ``BOTTLE_ALBUM_SHARE`` 的比例随机决定先试哪一类。

    2 和 3 都保留「首选那类没东西就回退到另一类」的兜底，所以空相册或空精华
    都不会让抽取变成空手而归——补偿只是调整优先级，不是把另一条路堵死。
    """
    if album_only:
        # 显式要相册，所以不参与补偿、比例，也不回退到精华；但「相册被配置关掉」
        # 仍然有效——配置说了不要，一条显式命令也不该把它打开。
        order = ["album"] if album_pool_enabled() else []
    elif not album_pool_enabled():
        order = ["essence"]
    else:
        order = preferred_kind_order(
            await recent_drawn_kinds(group_id, BOTTLE_SWITCH_STREAK)
        )
        if order is None:
            order = (
                ["album", "essence"]
                if random.randrange(100) < BOTTLE_ALBUM_SHARE
                else ["essence", "album"]
            )

    if not order:
        return ChosenBottle("album", None, [], 0, "unavailable", 0)

    failure: ChosenBottle | None = None
    for kind in order:
        fetch_started = time.perf_counter()
        if kind == "essence":
            try:
                items = await fetch_essence_list(bot, group_id)
            except EssenceUnavailable:
                items = []
            fetch_done = time.perf_counter()
            chosen = await choose_essence(bot, group_id, items, day=day)
        else:
            try:
                photos = await fetch_album_photos(bot, group_id)
            except AlbumUnavailable:
                photos = []
            fetch_done = time.perf_counter()
            chosen = await choose_album_photo(bot, group_id, photos, day=day)
        chosen_at = time.perf_counter()
        if chosen_at - fetch_started > 0.8:
            logger.info(
                "Bottle choose timing kind={} fetch={:.2f}s select+materialize={:.2f}s total={:.2f}s",
                kind,
                fetch_done - fetch_started,
                chosen_at - fetch_done,
                chosen_at - fetch_started,
            )
        if chosen.item is not None:
            return chosen
        if failure is None or chosen.reason == "unavailable":
            failure = chosen
    return failure or ChosenBottle("essence", None, [], 0, "exhausted", 0)


def album_pool_enabled() -> bool:
    """相册这一半是否被配置允许使用（开关与占比任一为"关"即关闭）。"""
    return BOTTLE_ALBUM_ENABLED and BOTTLE_ALBUM_SHARE > 0


async def essence_pool_size(bot: Bot, group_id: int) -> int:
    try:
        return len(await fetch_essence_list(bot, group_id))
    except EssenceUnavailable:
        return 0


async def album_pool_size(bot: Bot, group_id: int) -> int:
    """相册照片数；相册关掉或读不到都算 0。"""
    if not album_pool_enabled():
        return 0
    try:
        return len(await fetch_album_photos(bot, group_id))
    except AlbumUnavailable:
        return 0


async def pool_sizes(bot: Bot, group_id: int) -> tuple[int, int]:
    """(精华条数, 相册照片数)，任一来源读不到都当作 0。"""
    return await essence_pool_size(bot, group_id), await album_pool_size(bot, group_id)


_CACHE_WARM_TASK: asyncio.Task | None = None
_CACHE_WARM_BOT: Bot | None = None


def start_bottle_cache_warmer(bot: Bot) -> None:
    """Keep the essence/album caches warm in the background.

    The first draw after a restart used to pay the full cold fetch (measured
    1.4–7.2 s, mostly album pagination).  Refreshing shortly after connect and
    then well inside the 600 s TTL moves that cost off the user's request path.
    """
    global _CACHE_WARM_TASK, _CACHE_WARM_BOT
    if _CACHE_WARM_TASK is not None and not _CACHE_WARM_TASK.done():
        if _CACHE_WARM_BOT is bot:
            return
        # OneBot reconnect creates a new Bot object; the old task would keep
        # calling a dead connection forever.
        _CACHE_WARM_TASK.cancel()
    _CACHE_WARM_BOT = bot

    interval = max(120, min(ESSENCE_CACHE_SECONDS, BOTTLE_ALBUM_CACHE_SECONDS) - 120)

    async def loop() -> None:
        await asyncio.sleep(20)
        while True:
            for group_id in sorted(ALLOWED_GROUP_IDS):
                try:
                    await asyncio.wait_for(fetch_essence_list(bot, group_id), timeout=45)
                except Exception:
                    logger.debug("Bottle cache warm: essence failed for {}", group_id, exc_info=True)
                if album_pool_enabled():
                    try:
                        await asyncio.wait_for(fetch_album_photos(bot, group_id), timeout=90)
                    except Exception:
                        logger.debug("Bottle cache warm: album failed for {}", group_id, exc_info=True)
                await asyncio.sleep(1)
            await asyncio.sleep(interval)

    _CACHE_WARM_TASK = asyncio.create_task(loop())
