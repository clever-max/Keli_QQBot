"""Avatar and message-image caching.

All network I/O for images lives here.  Every failure path returns None (or
logs at debug/warning level) instead of raising, so a flaky campus network can
never take the bot down through this module.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from PIL import Image, ImageDraw, ImageOps
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from ..core.config import (
    ALLOWED_GROUP_IDS,
    AVATAR_CACHE_MAX_AGE,
    AVATAR_ROOT,
    DB_PATH,
    IMAGE_CACHE_JPEG_QUALITY,
    IMAGE_CACHE_MAX_DIMENSION,
    MAX_CACHED_IMAGE_BYTES,
    MEDIA_CACHE_DIR,
)
from ..core.state import _avatar_warm_tasks, _member_names
from ..common.textimg import font


def read_local_image(value: str) -> bytes | None:
    parsed = urlparse(value)
    raw_path = unquote(parsed.path) if parsed.scheme == "file" else value
    if parsed.scheme == "file" and os.name == "nt" and raw_path.startswith("/"):
        raw_path = raw_path[1:]
    path = Path(raw_path)
    try:
        if path.is_file() and path.stat().st_size <= MAX_CACHED_IMAGE_BYTES:
            return path.read_bytes()
    except OSError:
        return None
    return None


async def read_remote_image(
    value: str, client: httpx.AsyncClient, extra_suffixes: tuple[str, ...] = ()
) -> bytes | None:
    """Fetch an image URL, but only from hosts this bot is willing to talk to.

    ``extra_suffixes`` widens the allowlist for one call path (group essence
    images are served from Tencent COS, see ``essence.py``); ordinary message
    images keep the strict default list, so group content cannot point the bot
    at a new host.
    """
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    suffixes = (".qq.com", ".qq.com.cn", ".qpic.cn", ".qlogo.cn") + tuple(extra_suffixes)
    allowed = host == "qq.com" or any(host.endswith(suffix) for suffix in suffixes)
    if parsed.scheme != "https" or not allowed:
        return None
    try:
        response = await client.get(value)
        response.raise_for_status()
        if len(response.content) <= MAX_CACHED_IMAGE_BYTES:
            return response.content
    except httpx.HTTPError:
        return None
    return None


async def image_bytes_from_segment(
    bot: Bot,
    segment_data: dict[str, Any],
    client: httpx.AsyncClient,
    extra_suffixes: tuple[str, ...] = (),
) -> bytes | None:
    file_ref = str(segment_data.get("file", "") or "")
    if file_ref.startswith("base64://"):
        try:
            decoded = base64.b64decode(file_ref[len("base64://") :], validate=True)
            return decoded if len(decoded) <= MAX_CACHED_IMAGE_BYTES else None
        except (ValueError, binascii.Error):
            return None

    # Ask OneBot for its local copy first. NapCat can resolve its own opaque
    # image file IDs to a local file or a Tencent-hosted URL.
    if file_ref:
        try:
            result = await bot.call_api("get_image", file=file_ref)
            image_info = result.get("data", result) if isinstance(result, dict) else {}
            for value in (image_info.get("file"), image_info.get("url")):
                if not isinstance(value, str) or not value:
                    continue
                if value.startswith(("https://", "http://")):
                    content = await read_remote_image(value, client, extra_suffixes)
                else:
                    content = read_local_image(value)
                if content:
                    return content
        except Exception:
            logger.debug("OneBot could not resolve an image segment", exc_info=True)

    url = segment_data.get("url")
    if isinstance(url, str) and url:
        return await read_remote_image(url, client, extra_suffixes)
    return None


def normalize_cached_image(content: bytes, destination: Path) -> bool:
    """Write a downloaded picture into the media cache, **keeping the original**.

    This used to always downscale to 1000px and re-encode at JPEG q88, throwing
    away most of what had just been downloaded: a 4096x3072 original (4.2 MB) was
    stored as 1000x750 / 170 KB — 4% of the bytes and 1/16 of the pixels — and no
    amount of care at render time could bring that back.  Now the bytes are stored
    verbatim whenever they already fit the dimension and size caps; only a picture
    that exceeds them is downscaled, and then at a high quality.
    """
    try:
        with Image.open(io.BytesIO(content)) as source:
            source.seek(0)
            width, height = source.size
            if max(width, height) <= IMAGE_CACHE_MAX_DIMENSION and len(
                content
            ) <= MAX_CACHED_IMAGE_BYTES:
                # 原样落盘。扩展名可能和真实格式不符（可能是 PNG/WebP），但 Pillow
                # 按内容识别格式，渲染那条链路不受影响。
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
                return True
            source.thumbnail(
                (IMAGE_CACHE_MAX_DIMENSION, IMAGE_CACHE_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            if source.mode in ("RGBA", "LA"):
                background = Image.new("RGB", source.size, "white")
                background.paste(source, mask=source.getchannel("A"))
                image = background
            else:
                image = source.convert("RGB")
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(
                destination,
                format="JPEG",
                quality=IMAGE_CACHE_JPEG_QUALITY,
                optimize=True,
            )
        return True
    except (OSError, ValueError):
        return False


async def _archived_images(
    group_id: int | str, message_id: str
) -> dict[int, tuple[str, str]]:
    """All successfully archived images of one message, in one query.

    Previously ``_fill_from_archive`` opened a new SQLite connection per image;
    a 9-image message meant 9 connections before any rendering happened.
    """
    from ..core.store import connect

    async with connect() as db:
        cursor = await db.execute(
            """SELECT part_index, sha256, suffix FROM message_media
               WHERE group_id=? AND message_id=? AND kind='image'
                 AND error='' AND sha256<>''""",
            (str(group_id), str(message_id)),
        )
        rows = await cursor.fetchall()
    return {
        int(row["part_index"]): (str(row["sha256"]), str(row["suffix"]) or ".jpg")
        for row in rows
    }


async def cache_message_images(
    bot: Bot,
    group_id: int,
    message_id: str,
    message_data: list[dict[str, Any]],
    client: httpx.AsyncClient,
    extra_suffixes: tuple[str, ...] = (),
) -> None:
    from .archive import stored_path

    file_key = hashlib.sha256(f"{group_id}:{message_id}".encode("utf-8")).hexdigest()[:32]
    image_indexes = [
        index for index, segment in enumerate(message_data) if segment.get("type") == "image"
    ]
    archived = await _archived_images(group_id, str(message_id)) if image_indexes else {}
    for index in image_indexes:
        segment = message_data[index]
        segment_data = segment.setdefault("data", {})
        if segment_data.get("cached_path"):
            continue
        relative_path = Path("media_cache") / f"{file_key}_{index}.jpg"
        destination = DB_PATH.parent / relative_path
        archived_row = archived.get(index)
        if archived_row:
            source = stored_path(archived_row[0], archived_row[1])
            if source.is_file():
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(shutil.copyfile, source, destination)
                    segment_data["cached_path"] = relative_path.as_posix()
                    continue
                except OSError:
                    logger.debug("Could not copy archived image {}", source, exc_info=True)
        content = await image_bytes_from_segment(bot, segment_data, client, extra_suffixes)
        if not content:
            continue
        if await asyncio.to_thread(normalize_cached_image, content, destination):
            segment_data["cached_path"] = relative_path.as_posix()


def avatar_dir(group_id: int) -> Path:
    return AVATAR_ROOT / str(group_id)


def avatar_path(group_id: int, user_id: str) -> Path:
    return avatar_dir(group_id) / f"{user_id}.png"


async def cache_avatar(
    group_id: int, user_id: str, client: httpx.AsyncClient, force: bool = False
) -> None:
    path = avatar_path(group_id, user_id)
    if not force and path.exists():
        age = datetime.now().timestamp() - path.stat().st_mtime
        if age < AVATAR_CACHE_MAX_AGE.total_seconds():
            return
    if not user_id.isdigit():
        return
    url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
    try:
        response = await client.get(url)
        response.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(io.BytesIO(response.content)) as avatar:
            avatar.convert("RGB").resize((128, 128), Image.Resampling.LANCZOS).save(path, "PNG")
    except (httpx.HTTPError, OSError, ValueError):
        logger.debug("Could not cache QQ avatar for {}", user_id, exc_info=True)


async def warm_group_avatars(bot: Bot, group_id: int) -> None:
    """Refresh the avatar cache for one group.

    Runs on every OneBot (re)connect, so it must absorb transient network
    failures: the whole body is guarded and per-avatar errors are swallowed by
    ``cache_avatar``.
    """
    if group_id not in ALLOWED_GROUP_IDS:
        return
    avatar_dir(group_id).mkdir(parents=True, exist_ok=True)
    try:
        response = await bot.call_api("get_group_member_list", group_id=group_id)
        members = response.get("data", response) if isinstance(response, dict) else response
        if not isinstance(members, list):
            return
        names = _member_names.setdefault(group_id, {})
        for member in members:
            user_id = str(member.get("user_id", ""))
            name = str(member.get("card") or member.get("nickname") or "").strip()
            if user_id and name and not name.isdigit():
                names[user_id] = name
        semaphore = asyncio.Semaphore(8)
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            async def cache_one(member: dict[str, Any]) -> None:
                user_id = str(member.get("user_id", ""))
                async with semaphore:
                    await cache_avatar(group_id, user_id, client)

            await asyncio.gather(*(cache_one(member) for member in members))
        logger.info("Cached avatars for {} group members", len(members))
    except Exception:
        logger.warning("Could not warm group avatar cache", exc_info=True)


def schedule_avatar_warm(bot: Bot) -> None:
    """Start the warm-up tasks for every configured group (idempotent)."""
    if not str(getattr(bot, "self_id", "")):
        return
    for group_id in ALLOWED_GROUP_IDS:
        task = _avatar_warm_tasks.get(group_id)
        if task is None or task.done():
            _avatar_warm_tasks[group_id] = asyncio.create_task(
                warm_group_avatars(bot, group_id)
            )


def draw_avatar(
    canvas: Image.Image, group_id: int, user_id: str, sender_name: str, x: int, y: int, size: int
) -> None:
    path = avatar_path(group_id, user_id)
    avatar: Image.Image | None = None
    if path.exists():
        try:
            with Image.open(path) as source:
                avatar = ImageOps.fit(source.convert("RGB"), (size, size), method=Image.Resampling.LANCZOS)
        except OSError:
            avatar = None
    if avatar is None:
        digest = hashlib.sha256(user_id.encode("utf-8")).digest()
        color = (70 + digest[0] % 90, 80 + digest[1] % 90, 110 + digest[2] % 90)
        avatar = Image.new("RGB", (size, size), color)
        draw = ImageDraw.Draw(avatar)
        initials = sender_name[:1] if sender_name and not sender_name.isdigit() else "·"
        # 首字可能是 emoji（昵称常以 emoji 开头），用回退链才不会画成空框。
        font(24, True).draw(draw, (size // 2, size // 2), initials, fill="white", anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    canvas.paste(avatar, (x, y), mask)


def cached_image_paths_from_json(message_json: str) -> list[Path]:
    try:
        message_data = json.loads(message_json)
    except (json.JSONDecodeError, TypeError):
        return []
    media_root = MEDIA_CACHE_DIR.resolve()
    paths: list[Path] = []
    for segment in message_data:
        if segment.get("type") != "image":
            continue
        cached_path = segment.get("data", {}).get("cached_path")
        if not cached_path:
            continue
        path = (DB_PATH.parent / str(cached_path)).resolve()
        try:
            path.relative_to(media_root)
        except ValueError:
            continue
        if path.is_file():
            paths.append(path)
    return paths


def cached_image_paths(row: Any) -> list[Path]:
    try:
        return cached_image_paths_from_json(row["message_json"])
    except (KeyError, TypeError):
        return []
