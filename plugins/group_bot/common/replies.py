"""Resolve local previews for OneBot ``reply`` message segments.

OneBot stores only the referenced message id in a reply segment.  This module
keeps the lookup local and bounded: the normal path is a single batch query,
and a missing target is represented explicitly instead of becoming a generic
``[回复]`` label.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..media.pipeline import cached_image_paths
from ..core.store import connect
from .render import render_message


@dataclass(frozen=True)
class ReplyPreview:
    message_id: str
    title: str = "回复"
    sender_name: str = "群成员"
    sent_at: str = ""
    text: str = ""
    image_paths: tuple[Path, ...] = ()
    missing: bool = False


def reply_ids(message_data: Iterable[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for segment in message_data:
        if not isinstance(segment, dict) or segment.get("type") != "reply":
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        message_id = str(data.get("id") or "").strip()
        if message_id and message_id not in ids:
            ids.append(message_id)
    return ids


def forward_ids(message_data: Iterable[dict[str, Any]]) -> list[str]:
    ids: list[str] = []
    for segment in message_data:
        if not isinstance(segment, dict) or segment.get("type") != "forward":
            continue
        data = segment.get("data")
        if not isinstance(data, dict):
            continue
        forward_id = str(data.get("id") or "").strip()
        if forward_id and forward_id not in ids:
            ids.append(forward_id)
    return ids


def _missing(message_id: str) -> ReplyPreview:
    return ReplyPreview(
        message_id=message_id,
        text="原消息未归档，无法展开引用内容",
        missing=True,
    )


def _forward_missing(forward_id: str) -> ReplyPreview:
    return ReplyPreview(
        message_id=forward_id,
        title="合并转发",
        sender_name="聊天记录",
        text="合并转发内容暂不可用",
        missing=True,
    )


async def load_reply_previews(
    group_id: int | str, message_sets: Iterable[Iterable[dict[str, Any]]]
) -> dict[str, ReplyPreview]:
    """Load all reply targets for a group with one bounded database query."""
    ids: list[str] = []
    for message_data in message_sets:
        for message_id in reply_ids(message_data):
            if message_id not in ids:
                ids.append(message_id)
    if not ids:
        return {}

    previews = {message_id: _missing(message_id) for message_id in ids}
    placeholders = ",".join("?" for _ in ids)
    async with connect() as db:
        cursor = await db.execute(
            f"""SELECT message_id, sender_name, sent_at, message_json
                FROM messages
               WHERE group_id=? AND message_id IN ({placeholders})""",
            (str(group_id), *ids),
        )
        rows = await cursor.fetchall()
    for row in rows:
        message_id = str(row["message_id"])
        try:
            target_data = json.loads(row["message_json"])
            if not isinstance(target_data, list):
                raise TypeError("message_json is not a list")
            target_data = [item for item in target_data if isinstance(item, dict)]
        except (json.JSONDecodeError, TypeError):
            previews[message_id] = ReplyPreview(
                message_id=message_id,
                sender_name=str(row["sender_name"] or "群成员"),
                sent_at=str(row["sent_at"] or ""),
                text="原消息内容损坏，无法展开引用",
                missing=True,
            )
            continue
        text = render_message(target_data, omit_images=True)
        if not text and any(item.get("type") == "image" for item in target_data if isinstance(item, dict)):
            text = "[图片消息]"
        if not text:
            text = "[非文本消息]"
        previews[message_id] = ReplyPreview(
            message_id=message_id,
            sender_name=str(row["sender_name"] or "群成员"),
            sent_at=str(row["sent_at"] or ""),
            text=text,
            image_paths=tuple(cached_image_paths(row)),
        )
    return previews


async def load_reply_preview(
    group_id: int | str, message_data: Iterable[dict[str, Any]]
) -> ReplyPreview | None:
    """Resolve the first reply in one message, if present."""
    ids = reply_ids(message_data)
    if not ids:
        return None
    return (await load_reply_previews(group_id, [message_data])).get(ids[0])


async def load_forward_previews(
    bot: Any, message_sets: Iterable[Iterable[dict[str, Any]]]
) -> dict[str, ReplyPreview]:
    """Expand forward ids through OneBot without allowing one failure to abort recall."""
    ids: list[str] = []
    for message_data in message_sets:
        for forward_id in forward_ids(message_data):
            if forward_id not in ids:
                ids.append(forward_id)
    if not ids:
        return {}

    previews = {forward_id: _forward_missing(forward_id) for forward_id in ids}
    semaphore = asyncio.Semaphore(4)

    async def load_one(forward_id: str) -> None:
        async with semaphore:
            try:
                result = await bot.call_api("get_forward_msg", id=forward_id)
                payload = result.get("data", result) if isinstance(result, dict) else result
                nodes = payload
                if isinstance(payload, dict):
                    nodes = payload.get("messages") or payload.get("message") or []
                if not isinstance(nodes, list):
                    raise TypeError("get_forward_msg returned no node list")
                lines: list[str] = []
                for node in nodes[:8]:
                    if not isinstance(node, dict):
                        continue
                    data = node.get("data") if isinstance(node.get("data"), dict) else node
                    content = data.get("content")
                    if isinstance(content, list):
                        text = render_message(
                            [item for item in content if isinstance(item, dict)],
                            omit_images=True,
                            omit_replies=True,
                            omit_forwards=True,
                        )
                    else:
                        text = str(content or "").strip()
                    if not text:
                        text = "[非文本消息]"
                    name = str(data.get("nickname") or data.get("user_id") or "群成员").strip()
                    lines.append(f"{name}：{text[:240]}")
                if len(nodes) > 8:
                    lines.append(f"…还有 {len(nodes) - 8} 条消息")
                previews[forward_id] = ReplyPreview(
                    message_id=forward_id,
                    title="合并转发",
                    sender_name="聊天记录",
                    text="\n".join(lines)[:1200] or "合并转发中没有可展示的内容",
                )
            except Exception:
                return

    await asyncio.gather(*(load_one(forward_id) for forward_id in ids))
    return previews
