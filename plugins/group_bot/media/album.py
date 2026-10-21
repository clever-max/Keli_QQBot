"""群相册照片：拉取、解析与缓存，供漂流瓶的第二个来源使用。

NapCat 实现了 ``get_qun_album_list`` 与 ``get_group_album_media_list``，但后者
在它的 schema 里声明为 ``Any``（未定型），只有示例 ``media_list: [{media_id, url}]``。
真实响应里字段名可能是 ``url`` / ``originUrl`` / ``lloc`` 等等，还会带上传者信息。

因此这里不写死字段名，而是**按优先级递归查找**：先在本层找显式键，再往嵌套里找；
图片 URL 还留了一手兜底——扫描任何看起来像图片的 https 字符串。每轮拉取都会把
第一项的字段名记进日志，方便以后按实测把解析收窄成确定的形状。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from ..core.config import (
    BOTTLE_ALBUM_CACHE_SECONDS,
    BOTTLE_ALBUM_MAX_PAGES,
    BOTTLE_ALBUM_MAX_PHOTOS,
    MEDIA_CACHE_DIR,
)
from .pipeline import read_remote_image
from ..core.state import _album_broken, _album_cache, _album_locks

# 相册照片可能托管在腾讯 COS / gtimg，默认白名单没覆盖；
# 这个后缀列表只在「漂流瓶取相册图」这一条链路上放开。
ALBUM_IMAGE_SUFFIXES = (
    ".myqcloud.com",
    ".gtimg.cn",
    ".qcloud.com",
    ".photo.qq.com",
)

# 键名按优先级排列：越具体的越靠前，"id" 这种泛化键放在最后。
_MEDIA_ID_KEYS = ("media_id", "mediaId", "lloc", "file_id", "fileId", "batch_id", "id")
_ALBUM_ID_KEYS = ("album_id", "albumId", "id")
_UPLOADER_ID_KEYS = (
    "uploader_uin",
    "uploader_id",
    "uploaderUin",
    "uploaderId",
    "owner_uin",
    "owner_id",
    "uin",
    "user_id",
    "qq",
)
_UPLOADER_NAME_KEYS = (
    "uploader_nick",
    "uploader_name",
    "uploaderNick",
    "uploaderName",
    "nick",
    "nickname",
    "name",
    "card",
)
_TIME_KEYS = (
    "upload_time",
    "uploadTime",
    "create_time",
    "createTime",
    "shoot_time",
    "shootTime",
    "ctime",
)
_IMAGE_URL_KEYS = (
    "origin_url",
    "original_url",
    "originUrl",
    "originalUrl",
    "raw_url",
    "rawUrl",
    "hd_url",
    "big_url",
    "image_url",
    "img_url",
    "file_url",
    "download_url",
    "url",
    "src",
    "thumb_url",
    "cover_url",
)
# 已知会被用作图片 URL 的键；兜底扫描时用它判断"这看着像图片地址"。
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic")
_VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".flv", ".wmv", ".3gp")
# 相册里视频的键名（具体键优先）。视频是**直接转发**的，所以这里取的是可播放地址。
_VIDEO_URL_KEYS = (
    "origin_video_url",
    "originVideoUrl",
    "video_url",
    "videoUrl",
    "play_url",
    "playUrl",
    "mp4_url",
    "mp4Url",
    "download_url",
    "url",
    "src",
)
# 兜底扫描只接受「下载那一步也会放行」的主机：认出来却下不下来的 URL 没有意义，
# 而且会被记成坏图。这份列表与 media.read_remote_image 的白名单保持一致。
_IMAGE_HOST_SUFFIXES = (
    ".qq.com",
    ".qq.com.cn",
    ".qpic.cn",
    ".qlogo.cn",
) + ALBUM_IMAGE_SUFFIXES


class AlbumUnavailable(RuntimeError):
    """The album could not be read (API failure or unsupported action)."""


@dataclass(frozen=True)
class AlbumPhoto:
    """One item from a group album, already normalised to plain fields.

    ``kind`` is ``"image"`` or ``"video"``; for a video, ``url`` is the playable
    video reference that gets forwarded as-is (no card is built for it).
    """

    media_id: str
    url: str
    album_id: str = ""
    album_name: str = ""
    uploader_id: str = ""
    uploader_name: str = ""
    upload_time: Any = 0
    kind: str = "image"


def _find_value_anywhere(node: Any, key: str, depth: int = 0) -> str:
    """Find ``key`` anywhere in the subtree."""
    if depth > 4:
        return ""
    if isinstance(node, dict):
        value = node.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
        for child in node.values():
            found = _find_value_anywhere(child, key, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for child in node[:8]:
            found = _find_value_anywhere(child, key, depth + 1)
            if found:
                return found
    return ""


def _looks_like_video_url(value: str) -> bool:
    if not value.startswith(("https://", "http://")):
        return False
    parsed = urlparse(value)
    if parsed.path.lower().endswith(_VIDEO_EXTENSIONS):
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in ("video", "/mp4", "play_url", "playurl"))


def _scan_video_url(node: Any, depth: int = 0) -> str:
    """Any string in the subtree that looks like a video reference."""
    if depth > 4:
        return ""
    if isinstance(node, dict):
        for value in node.values():
            found = _scan_video_url(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node[:8]:
            found = _scan_video_url(value, depth + 1)
            if found:
                return found
    elif isinstance(node, str) and _looks_like_video_url(node.strip()):
        return node.strip()
    return ""


def _find_value(node: Any, keys: Iterable[str], depth: int = 0) -> str:
    """按键的优先级在整棵子树里查找，而不是先扫完一层再往下。

    这个区别对图片 URL 很关键：一条媒体既可能有顶层 ``url``（缩略图）又有嵌套的
    ``originUrl``（原图）。逐层查找会先撞上缩略图，按键优先则让"原图"这个键在任何
    层级都胜出——这就是"拿到最高质量那一张"的实现方式。
    """
    for key in keys:
        found = _find_value_anywhere(node, key)
        if found:
            return found
    return ""


def _looks_like_image_url(value: str) -> bool:
    if not value.startswith(("https://", "http://")):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host == "qq.com" or any(host.endswith(suffix) for suffix in _IMAGE_HOST_SUFFIXES):
        return True
    if parsed.path.lower().endswith(_IMAGE_EXTENSIONS):
        return True
    return "image" in value.lower()


def _scan_image_url(node: Any, depth: int = 0) -> str:
    """Last resort: any string anywhere that looks like a picture URL."""
    if depth > 4:
        return ""
    if isinstance(node, dict):
        for value in node.values():
            found = _scan_image_url(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for value in node[:8]:
            found = _scan_image_url(value, depth + 1)
            if found:
                return found
    elif isinstance(node, str) and _looks_like_image_url(node.strip()):
        return node.strip()
    return ""


def _as_list(response: Any, keys: Iterable[str]) -> list[Any]:
    """Pull the item list out of a response whose shape is not guaranteed."""
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        data = response.get("data", response)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if isinstance(value, list):
                    return value
            # 再退一步：任何值是列表的键
            for value in data.values():
                if isinstance(value, list):
                    return value
    return []


def _next_attach_info(response: Any) -> str:
    if isinstance(response, dict):
        data = response.get("data", response)
        if isinstance(data, dict):
            for key in ("attach_info", "attachInfo", "next_attach_info"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
    return ""


def parse_media_item(raw: Any, album_id: str, album_name: str) -> AlbumPhoto | None:
    """Normalise one raw media entry, or ``None`` when it carries no media.

    先判视频再判图片：视频是**直接转发**的（不下载、不做卡片），所以只要认出视频
    地址就按视频处理。这一步同时修掉一个隐患——以前任何 QQ 主机上的 URL 都会被当作
    图片放行，相册里的视频因此被当成照片，下载后 Pillow 打不开，最后被记成"坏图"。
    """
    if not isinstance(raw, dict):
        return None

    video = _find_value(raw, _VIDEO_URL_KEYS)
    video = video if _looks_like_video_url(video) else _scan_video_url(raw)
    if video:
        url, kind = video, "video"
    else:
        url, kind = _find_value(raw, _IMAGE_URL_KEYS) or _scan_image_url(raw), "image"
        if not url:
            return None

    media_id = _find_value(raw, _MEDIA_ID_KEYS)
    if not media_id:
        # 分页顺序可能变，用 URL 做稳定键，保证"今天不重复"仍然成立。
        media_id = "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    return AlbumPhoto(
        media_id=media_id,
        url=url,
        album_id=album_id,
        album_name=album_name,
        uploader_id=_find_value(raw, _UPLOADER_ID_KEYS),
        uploader_name=_find_value(raw, _UPLOADER_NAME_KEYS),
        upload_time=_find_value(raw, _TIME_KEYS) or 0,
        kind=kind,
    )


def _shape_hint(raw: Any) -> str:
    """Small, log-safe summary of a response's field names (never its values)."""
    if isinstance(raw, dict):
        data = raw.get("data", raw)
        if isinstance(data, dict):
            return ",".join(sorted(str(key) for key in data)[:12])
        return f"dict->{type(data).__name__}"
    if isinstance(raw, list):
        first = raw[0] if raw else None
        if isinstance(first, dict):
            return "list[" + ",".join(sorted(str(key) for key in first)[:12]) + "]"
        return f"list[{type(first).__name__}]"
    return type(raw).__name__


async def _fetch_album_photos_uncached(bot: Bot, group_id: int) -> list[AlbumPhoto]:
    # 1. 相册列表（分页由 attach_info 驱动，翻页次数设上限）
    albums: list[dict[str, Any]] = []
    attach_info = ""
    for page in range(BOTTLE_ALBUM_MAX_PAGES):
        try:
            response = await bot.call_api(
                "get_qun_album_list", group_id=group_id, attach_info=attach_info
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the group as a notice
            logger.warning("get_qun_album_list failed for group {}", group_id, exc_info=True)
            raise AlbumUnavailable(str(exc)) from exc
        if page == 0:
            logger.info("Album list shape for group {}: {}", group_id, _shape_hint(response))
        page_albums = [
            item for item in _as_list(response, ("album_list", "albumList")) if isinstance(item, dict)
        ]
        albums.extend(page_albums)
        attach_info = _next_attach_info(response)
        if not attach_info or not page_albums:
            break

    photos: list[AlbumPhoto] = []
    for album in albums:
        album_id = _find_value(album, _ALBUM_ID_KEYS)
        album_name = _find_value(album, ("album_name", "albumName", "name"))
        if not album_id:
            continue
        media_attach = ""
        for page in range(BOTTLE_ALBUM_MAX_PAGES):
            try:
                response = await bot.call_api(
                    "get_group_album_media_list",
                    group_id=group_id,
                    album_id=album_id,
                    attach_info=media_attach,
                )
            except Exception:
                # 单个相册读不到（权限、已删除）不该毁掉整次抽取。
                logger.warning(
                    "get_group_album_media_list failed for group {} album {}",
                    group_id,
                    album_id,
                    exc_info=True,
                )
                break
            if page == 0 and not photos:
                logger.info(
                    "Album media shape for group {}: {}", group_id, _shape_hint(response)
                )
            raw_items = _as_list(
                response, ("media_list", "mediaList", "photos", "items", "list")
            )
            for raw in raw_items:
                photo = parse_media_item(raw, album_id, album_name)
                if photo is not None:
                    photos.append(photo)
                if len(photos) >= BOTTLE_ALBUM_MAX_PHOTOS:
                    break
            media_attach = _next_attach_info(response)
            if (
                not media_attach
                or not raw_items
                or len(photos) >= BOTTLE_ALBUM_MAX_PHOTOS
            ):
                break
        if len(photos) >= BOTTLE_ALBUM_MAX_PHOTOS:
            break

    # 去重：同一张照片可能出现在多个相册/多页里。
    unique: dict[str, AlbumPhoto] = {}
    for photo in photos:
        unique.setdefault(photo.media_id, photo)
    result = list(unique.values())
    unattributed = sum(1 for photo in result if not photo.uploader_id)
    logger.info(
        "Cached {} album photos for group {} ({} albums, {} without uploader)",
        len(result),
        group_id,
        len(albums),
        unattributed,
    )
    return result


async def fetch_album_photos(bot: Bot, group_id: int) -> list[AlbumPhoto]:
    """Every photo in the group's albums, served from a short in-memory cache."""
    cached = _album_cache.get(group_id)
    now = time.monotonic()
    if cached and now - cached[0] < BOTTLE_ALBUM_CACHE_SECONDS:
        return list(cached[1])

    lock = _album_locks.setdefault(group_id, asyncio.Lock())
    async with lock:
        cached = _album_cache.get(group_id)
        now = time.monotonic()
        if cached and now - cached[0] < BOTTLE_ALBUM_CACHE_SECONDS:
            return list(cached[1])
        photos = await _fetch_album_photos_uncached(bot, group_id)
        _album_cache[group_id] = (now, photos)
        return list(photos)


async def materialize_photo(
    photo: AlbumPhoto, group_id: int, client: httpx.AsyncClient
) -> Path | None:
    """Download one album picture into the media cache, or ``None`` on failure."""
    MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    content = await read_remote_image(photo.url, client, ALBUM_IMAGE_SUFFIXES)
    if not content:
        return None
    key = hashlib.sha256(f"album:{group_id}:{photo.media_id}".encode("utf-8")).hexdigest()[:32]
    path = MEDIA_CACHE_DIR / f"{key}.jpg"
    try:
        path.write_bytes(content)
    except OSError:
        logger.warning("Could not write album photo cache {}", path, exc_info=True)
        return None
    return path


def mark_broken(group_id: int, drawn_key: str) -> None:
    """记住一个下载不下来的相册照片。

    ``drawn_key`` 用的是 ``essence_draws`` 那套键（``album:<media_id>``），与
    ``_album_broken`` 的筛选条件保持一致——两处键空间一旦不一致，坏图就永远筛不掉。
    """
    _album_broken.setdefault(group_id, set()).add(drawn_key)
