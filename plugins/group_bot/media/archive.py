"""把每条消息里的多媒体**永久归档**（图片、视频、语音、文件）。

为什么要有它：之前多媒体的落盘是"按需"的——只有有人查撤回、要渲染卡片时才下载
（`media.cache_message_images`），平时收到的图片/视频/语音**根本没存**，而 QQ 的媒体
URL 是有时效的，过期后就再也拿不回来了。本模块把"归档"从渲染流程里拆出来，收到消息就存。

三条设计取舍：

1. **内容寻址**：落盘名是 ``sha256`` 的前缀分层（``media_archive/ab/cd/abcdef….jpg``），
   同一张图被转发十次也只占一份；重复消息天然幂等。
2. **可续跑**：消息行上的 ``archived_at`` 标记进度。归档是后台任务，崩了/失败了不丢——
   启动时与每 10 分钟扫一遍"还没归档"的消息补上（历史消息也会因此被补一次，
   只是老消息的 URL 多半已过期，会记成 ``error`` 而不是无限重试）。
3. **绝不静默丢数据**：下载失败、超过体积上限都要在 ``message_media`` 里留一行带原因，
   而不是当作"这条消息没有多媒体"。

**没有任何清理逻辑**：这个目录是"永久保存"的承诺，不是缓存。磁盘不够时应该加硬盘，
而不是删旧文件（见 docs/operations.md）。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from ..core.config import ARCHIVE_MAX_BYTES, DB_PATH
from .pipeline import image_bytes_from_segment
from ..core.store import connect

# 归档根目录：和 messages.sqlite3 同级，进 .gitignore 的 data/。
ARCHIVE_ROOT = (DB_PATH.parent / "media_archive").resolve()
# 一次归档任务最多同时下几个文件（机械盘 + 家庭带宽，别开太大）。
_ARCHIVE_CONCURRENCY = 2
_TASKS: set[asyncio.Task] = set()
_SEMAPHORE: asyncio.Semaphore | None = None

_MEDIA_KINDS = {"image", "video", "record", "file"}
_SUFFIX_BY_KIND = {"image": ".jpg", "video": ".mp4", "record": ".amr", "file": ".bin"}
# 单次取文件的硬上限（秒）。见 archive_message 里的注释。
_FETCH_TIMEOUT = 20.0


def _semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:  # 懒创建：必须在事件循环里
        _SEMAPHORE = asyncio.Semaphore(_ARCHIVE_CONCURRENCY)
    return _SEMAPHORE


def stored_path(sha256: str, suffix: str) -> Path:
    """内容寻址的落盘位置（按前 4 个十六进制字符分两级目录，避免单目录几万文件）。"""
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    return ARCHIVE_ROOT / sha256[:2] / sha256[2:4] / f"{sha256}{suffix.lower()}"


def sniff_suffix(content: bytes, fallback: str) -> str:
    """从文件头猜扩展名；猜不出就用调用方给的兜底。"""
    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content[:2] == b"BM":
        return ".bmp"
    if content[:5] == b"%PDF-":
        return ".pdf"
    if content[4:8] == b"ftyp":
        return ".mp4"
    if content[:4] == b"#!AM":
        return ".amr"
    if content[:3] == b"ID3" or content[:2] in (b"\xff\xfb", b"\xff\xf3"):
        return ".mp3"
    if content[:4] == b"fLaC":
        return ".flac"
    if content[:4] == b"OggS":
        return ".ogg"
    return fallback


def _guess_suffix(segment: dict[str, Any], kind: str, content: bytes) -> str:
    data = segment.get("data") or {}
    for key in ("file", "file_name", "name", "path", "url"):
        value = str(data.get(key) or "")
        suffix = Path(value.split("?")[0]).suffix
        if suffix and 1 < len(suffix) <= 6:
            return suffix if suffix.startswith(".") else f".{suffix}"
    return sniff_suffix(content, _SUFFIX_BY_KIND.get(kind, ".bin"))


def _local_path(ref: str) -> Path | None:
    """把 ``file://`` 或普通路径变成 Path（不读文件）。"""
    if not ref or ref.startswith(("base64://", "http://", "https://")):
        return None
    parsed = urlparse(ref)
    raw = unquote(parsed.path) if parsed.scheme == "file" else ref
    if parsed.scheme == "file" and os.name == "nt" and raw.startswith("/"):
        raw = raw[1:]
    return Path(raw)


def _read_local(ref: str) -> bytes | None:
    """读本地路径或 ``base64://``；超上限、不存在都返回 None（调用方负责给原因）。"""
    if ref.startswith("base64://"):
        try:
            content = base64.b64decode(ref[len("base64://"):], validate=True)
        except (ValueError, binascii.Error):
            return None
        return content if not ARCHIVE_MAX_BYTES or len(content) <= ARCHIVE_MAX_BYTES else None
    path = _local_path(ref)
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        if ARCHIVE_MAX_BYTES and path.stat().st_size > ARCHIVE_MAX_BYTES:
            return None
        return path.read_bytes()
    except OSError:
        return None


async def _download(
    bot: Bot, segment: dict[str, Any], client: httpx.AsyncClient
) -> tuple[bytes | None, str]:
    """取一个多媒体段的字节。返回 ``(内容, 失败原因)``，成功时原因为空串。

    图片先在本地/``base64`` 里找（会话里的图常常就在 NapCat 的缓存目录里，比再过一遍
    网络稳），找不到再回落到 ``media.image_bytes_from_segment``（它有域名白名单）。
    超上限要给出**精确原因**，不能笼统说"取不到内容"——否则事后无法区分
    "没这张图"和"图太大被跳过"。
    """
    data = segment.setdefault("data", {})
    kind = str(segment.get("type") or "")

    for key in ("file", "path"):
        ref = str(data.get(key) or "")
        if not ref:
            continue
        if ref.startswith("base64://"):
            content = await asyncio.to_thread(_read_local, ref)
            if content:
                return content, ""
            continue
        path = _local_path(ref)
        if path is None or not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if ARCHIVE_MAX_BYTES and size > ARCHIVE_MAX_BYTES:
            return None, f"超过归档上限（{size} > {ARCHIVE_MAX_BYTES} 字节）"
        try:
            return await asyncio.to_thread(path.read_bytes), ""
        except OSError as error:
            return None, f"读取本地文件失败: {error}"

    if kind == "image":
        content = await image_bytes_from_segment(bot, data, client)
        if content:
            return content, ""
        return None, "取不到内容（本地文件不存在，或 URL 不可达/域名不在白名单）"

    url = str(data.get("url") or "").strip()
    if url.startswith(("http://", "https://")):
        response = await client.get(url)
        response.raise_for_status()
        if ARCHIVE_MAX_BYTES and len(response.content) > ARCHIVE_MAX_BYTES:
            return None, f"超过归档上限（{len(response.content)} > {ARCHIVE_MAX_BYTES} 字节）"
        return response.content, ""

    return None, "取不到内容（URL 可能已过期，且没有本地副本）"


async def _store(
    group_id: str,
    message_id: str,
    index: int,
    segment: dict[str, Any],
    content: bytes,
) -> tuple[str, int, str]:
    kind = str(segment.get("type") or "file")
    digest = hashlib.sha256(content).hexdigest()
    suffix = _guess_suffix(segment, kind, content)
    destination = stored_path(digest, suffix)
    if not destination.exists() or destination.stat().st_size != len(content):
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_bytes, content)
    return digest, len(content), suffix


async def archive_message(
    bot: Bot,
    group_id: str,
    message_id: str,
    segments: list[dict[str, Any]],
    *,
    reason: str = "ingest",
) -> int:
    """归档一条消息的所有多媒体，返回成功落盘的份数。

    幂等：同一条消息重复归档只会覆盖同样的行、命中同样的内容寻址路径。
    """
    media = [
        (index, segment)
        for index, segment in enumerate(segments)
        if str(segment.get("type") or "") in _MEDIA_KINDS
    ]
    if not media:
        async with connect() as db:
            cursor = await db.execute(
                "UPDATE messages SET archived_at=? WHERE message_id=? AND group_id=?",
                (_now(), message_id, group_id),
            )
            # 消息行不存在（理论上不该发生：归档总在 save_message 之后）只记一笔，
            # 不要让它看起来像"归档成功"——扫描会再试一次。
            if not cursor.rowcount:
                logger.debug("Archive: message {}-{} not in messages yet", group_id, message_id)
            await db.commit()  # connect() 不会自动提交，漏了就等于白写
        return 0

    stored = 0
    rows: list[tuple[Any, ...]] = []
    async with _semaphore():
        # 老消息的 URL 多半已过期：DNS/连接会一直等到超时。给个短超时，
        # 否则一晚上都扫不完（实测 30 s 超时下，50 条死链能卡住十几分钟）。
        timeout = httpx.Timeout(connect=4.0, read=10.0, write=10.0, pool=4.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            for index, segment in media:
                data = segment.setdefault("data", {})
                source_url = str(data.get("url") or data.get("file") or "")
                kind = str(segment.get("type"))
                try:
                    # 外层再套一道硬超时：httpx 的 read 超时只管"两次读之间"，遇到慢速
                    # 滴流或卡住的 DNS 仍可能无限期挂着（实测把整个补归档卡死）。
                    content, reason = await asyncio.wait_for(
                        _download(bot, segment, client), timeout=_FETCH_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    rows.append(
                        (
                            group_id, message_id, index, kind,
                            "", 0, "", source_url, _now(),
                            f"下载超时（超过 {_FETCH_TIMEOUT:.0f} 秒）",
                        )
                    )
                    logger.warning(
                        "Archive: {} of {}-{} timed out", kind, group_id, message_id
                    )
                    continue
                except Exception as error:  # noqa: BLE001  单段失败不能连累整条消息
                    rows.append(
                        (
                            group_id, message_id, index, kind,
                            "", 0, "", source_url, _now(),
                            f"下载失败: {type(error).__name__}: {error}",
                        )
                    )
                    logger.warning(
                        "Archive: could not fetch {} of {}-{}: {}",
                        kind, group_id, message_id, error,
                    )
                    continue
                if not content:
                    rows.append(
                        (
                            group_id, message_id, index, kind,
                            "", 0, "", source_url, _now(), reason or "取不到内容",
                        )
                    )
                    logger.info(
                        "Archive: {} of {}-{} not stored: {}", kind, group_id, message_id, reason
                    )
                    continue
                digest, size, suffix = await _store(
                    group_id, message_id, index, segment, content
                )
                rows.append(
                    (
                        group_id, message_id, index, kind,
                        digest, size, suffix, source_url, _now(), "",
                    )
                )
                stored += 1

    async with connect() as db:
        await db.executemany(
            """INSERT OR REPLACE INTO message_media
               (group_id, message_id, part_index, kind, sha256, size, suffix,
                source_url, stored_at, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await db.execute(
            "UPDATE messages SET archived_at=? WHERE message_id=? AND group_id=?",
            (_now(), message_id, group_id),
        )
        await db.commit()
    if rows:
        logger.info(
            "Archived {} / {} media of message {}-{} ({})",
            stored, len(rows), group_id, message_id, reason,
        )
    return stored


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def schedule_archive(bot: Bot, event: GroupMessageEvent, segments: list[dict[str, Any]]) -> None:
    """收到消息后**不阻塞**地归档（在后台任务里跑，失败由扫描兜底）。"""

    async def runner() -> None:
        try:
            await archive_message(bot, str(event.group_id), str(event.message_id), segments)
        except Exception:
            logger.exception("Archive task failed for message {}", event.message_id)

    task = asyncio.create_task(runner())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def archive_pending(
    bot: Bot, limit: int = 20, *, deadline_seconds: float | None = None
) -> int:
    """补归档：把还没归档的消息扫一遍（启动时与定时器调用）。

    ``deadline_seconds`` 是墙钟预算：扫到预算用完就停，把剩下的留给下一轮——
    绝不能因为一批死链超时把整个循环拖住（实测过）。
    """
    started = time.perf_counter()
    async with connect() as db:
        cursor = await db.execute(
            """SELECT group_id, message_id, message_json FROM messages
               WHERE archived_at IS NULL ORDER BY sent_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
    total = 0
    for row in rows:
        if deadline_seconds is not None and time.perf_counter() - started > deadline_seconds:
            logger.info("Archive sweep: out of time budget, {} rows left for next round", len(rows))
            break
        try:
            segments = json.loads(row["message_json"])
        except (json.JSONDecodeError, TypeError):
            segments = []
        try:
            total += await archive_message(
                bot, str(row["group_id"]), str(row["message_id"]), segments, reason="sweep"
            )
        except Exception:
            logger.exception("Could not archive message {}", row["message_id"])
    if rows:
        logger.info("Archive sweep: {} messages scanned, {} media stored", len(rows), total)
    return total


def archived_media(group_id: str, message_id: str) -> list[dict[str, Any]]:
    """读回某条消息已归档的多媒体（同步接口，给离线检查用）。"""
    import sqlite3

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    try:
        cursor = db.execute(
            """SELECT * FROM message_media WHERE group_id=? AND message_id=?
               ORDER BY part_index""",
            (group_id, message_id),
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        db.close()


async def archived_media_paths(
    group_id: int | str,
    message_ids: list[str],
    *,
    kind: str = "video",
) -> dict[str, list[Path]]:
    """Return existing permanent media files for a batch of messages.

    Only successful archive rows are returned.  The caller can then decide
    whether to trigger a one-shot retry for rows that still have no file.
    """
    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    result: dict[str, list[Path]] = {}
    async with connect() as db:
        cursor = await db.execute(
            f"""SELECT message_id, sha256, suffix
                  FROM message_media
                 WHERE group_id=? AND kind=? AND error='' AND sha256<>''
                   AND message_id IN ({placeholders})
                 ORDER BY message_id, part_index""",
            (str(group_id), kind, *message_ids),
        )
        rows = await cursor.fetchall()
    for row in rows:
        path = stored_path(str(row["sha256"]), str(row["suffix"] or ".bin"))
        if path.is_file():
            result.setdefault(str(row["message_id"]), []).append(path)
    return result


# 每轮补归档处理多少条消息；小批量、可中断，避免一次扫太久占住带宽。
_SWEEP_BATCH = 100
# 没有积压时的巡检间隔。有积压时不停，一轮接一轮地冲（见 start_archive_sweeper）。
_SWEEP_INTERVAL_SECONDS = 600
_sweeper_started = False


async def pending_count() -> int:
    """还有多少条消息没归档过（判断要不要连续冲）。"""
    async with connect() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE archived_at IS NULL"
        )
        row = await cursor.fetchone()
        return int(row["n"]) if row else 0


def start_archive_sweeper(bot: Bot) -> None:
    """在 bot 连上后启动补归档循环（只启动一次）。

    行为：启动后几秒就开始；**只要有积压就一轮接一轮地冲**（每轮 100 条 + 90 秒预算），
    积压清空后转入每 10 分钟一次的低频巡检。这样"上次崩溃没归档完的消息"与"历史存量"
    能在几十分钟内补齐，而不是按 10 分钟一批拖几天（实测 13,200 条待归档时那样要 4 天多）。
    """
    global _sweeper_started
    if _sweeper_started:
        return
    _sweeper_started = True

    async def loop() -> None:
        from ..core.config import ARCHIVE_ENABLED

        if not ARCHIVE_ENABLED:
            logger.info("Archive disabled (ARCHIVE_ENABLED=0); sweeper not started")
            return
        await asyncio.sleep(5)  # 让连接/首屏工作先稳定
        while True:
            try:
                before = await pending_count()
                if before:
                    await archive_pending(bot, limit=_SWEEP_BATCH, deadline_seconds=90)
                    after = await pending_count()
                    if after and after < before:
                        await asyncio.sleep(2)  # 还有积压，马上继续
                        continue
            except Exception:
                logger.exception("Archive sweep failed")
            await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)

    task = asyncio.create_task(loop())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    logger.info(
        "Archive sweeper started (every {}s, batch {})", _SWEEP_INTERVAL_SECONDS, _SWEEP_BATCH
    )
