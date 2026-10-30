"""Regression checks for recall previews, races, media labels and page idempotency."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import nonebot  # noqa: E402

TEMP = Path(os.environ.get("TEMP", ".")) / "recall_verify"
TEMP.mkdir(parents=True, exist_ok=True)
DB = TEMP / "verify_recall_enhancements.sqlite3"
for stale in TEMP.glob("verify_recall_enhancements.sqlite3*"):
    stale.unlink(missing_ok=True)

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="1,2",
    authorized_users="9",
    group_authorized_users="1:9;2:9",
    database_path=str(DB),
    archive_enabled=False,
)

from nonebot.adapters.onebot.v11 import (  # noqa: E402
    GroupMessageEvent,
    GroupRecallNoticeEvent,
    Message,
    MessageSegment,
)

from plugins.group_bot.common.render import render_message  # noqa: E402
from plugins.group_bot.common.replies import (  # noqa: E402
    load_forward_previews,
    load_reply_preview,
)
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402
from plugins.group_bot.media import archive  # noqa: E402
from plugins.group_bot.recall.ingest import mark_recalled, save_message  # noqa: E402
from plugins.group_bot.recall.history import (  # noqa: E402
    RecallRequest,
    send_recall_history,
)
from plugins.group_bot.recall import history as recall_history  # noqa: E402


NOW = datetime.now(timezone.utc).isoformat()
failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


class Bot:
    def __init__(self, *, fail_send: int | None = None) -> None:
        self.send_count = 0
        self.fail_send = fail_send
        self.messages: list[object] = []
        self.uploads: list[dict] = []

    async def send_group_msg(self, **kwargs):
        self.send_count += 1
        self.messages.append(kwargs.get("message"))
        if self.fail_send == self.send_count:
            return {"status": "failed"}
        return {"status": "ok"}

    async def send_private_msg(self, **kwargs):
        self.messages.append(kwargs.get("message"))
        return {"status": "ok"}

    async def call_api(self, api: str, **kwargs):
        if api in ("upload_group_file", "upload_private_file"):
            self.uploads.append(kwargs)
            return {"status": "ok"}
        if api == "get_forward_msg":
            return {
                "data": {
                    "messages": [
                        {
                            "type": "node",
                            "data": {
                                "nickname": "甲",
                                "content": [{"type": "text", "data": {"text": "转发正文"}}],
                            },
                        }
                    ]
                }
            }
        return {"data": {"group_name": "测试群"}}


async def insert_message(
    group_id: int,
    message_id: str,
    segments: list[dict],
    *,
    recalled_at: str | None = NOW,
) -> None:
    async with connect() as db:
        await db.execute(
            """INSERT OR REPLACE INTO messages
               (group_id, message_id, user_id, sender_name, message_json,
                plain_text, sent_at, recalled_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(group_id),
                message_id,
                "8",
                "测试员",
                json.dumps(segments, ensure_ascii=False),
                render_message(segments),
                NOW,
                recalled_at,
            ),
        )
        await db.commit()


async def insert_recall(group_id: int, message_id: str) -> None:
    async with connect() as db:
        await db.execute(
            "INSERT OR REPLACE INTO recall_events(group_id, message_id, recalled_at, summoned_at) "
            "VALUES (?, ?, ?, NULL)",
            (str(group_id), message_id, NOW),
        )
        await db.commit()


async def insert_media_row(
    group_id: int, message_id: str, sha256: str, suffix: str, *, kind: str = "video"
) -> None:
    async with connect() as db:
        await db.execute(
            """INSERT OR REPLACE INTO message_media
               (group_id, message_id, part_index, kind, sha256, size, suffix,
                source_url, stored_at, error)
               VALUES (?, ?, 0, ?, ?, ?, ?, '', ?, '')""",
            (str(group_id), message_id, kind, sha256, 12, suffix, NOW),
        )
        await db.commit()


async def main() -> None:
    await initialize_database()

    print("== 1. media and message labels ==")
    rendered = render_message(
        [
            {"type": "record", "data": {"file": "voice.amr", "file_size": "1024"}},
            {"type": "video", "data": {"file": "clip.mp4", "file_size": "2048"}},
            {"type": "file", "data": {"file_name": "report.pdf", "file_size": "4096"}},
            {"type": "reply", "data": {"id": "10"}},
            {"type": "forward", "data": {"id": "f1"}},
        ]
    )
    check("media labels", rendered, "[语音] voice.amr (1.0 KB)[视频] clip.mp4 (2.0 KB)[文件] report.pdf (4.0 KB)[回复消息][合并转发]")

    print("== 2. reply and forward previews ==")
    await insert_message(1, "10", [{"type": "text", "data": {"text": "被回复的原文"}}], recalled_at=None)
    found = await load_reply_preview(1, [{"type": "reply", "data": {"id": "10"}}])
    missing = await load_reply_preview(1, [{"type": "reply", "data": {"id": "missing"}}])
    check("reply target text", found.text if found else None, "被回复的原文")
    check("missing reply is explicit", missing.missing if missing else None, True)
    forward = await load_forward_previews(
        Bot(), [[{"type": "forward", "data": {"id": "f1"}}]]
    )
    check("forward title", forward["f1"].title, "合并转发")
    check("forward content", "转发正文" in forward["f1"].text, True)

    print("== 3. recall notice can precede message save ==")
    notice = GroupRecallNoticeEvent(
        time=1,
        self_id=9,
        post_type="notice",
        notice_type="group_recall",
        user_id=9,
        group_id=1,
        operator_id=9,
        message_id=700,
    )
    await mark_recalled(notice)
    event = GroupMessageEvent(
        time=int(datetime.now(timezone.utc).timestamp()),
        self_id=9,
        post_type="message",
        sub_type="normal",
        user_id=8,
        message_type="group",
        message_id=700,
        message=Message(MessageSegment.text("late message")),
        original_message=Message(MessageSegment.text("late message")),
        raw_message="late message",
        font=0,
        sender={"user_id": 8, "nickname": "测试员", "card": ""},
        group_id=1,
    )
    await save_message(Bot(), event)
    async with connect() as db:
        message = await (await db.execute(
            "SELECT recalled_at FROM messages WHERE group_id='1' AND message_id='700'"
        )).fetchone()
        event_count = await (await db.execute(
            "SELECT COUNT(*) AS n FROM recall_events WHERE group_id='1' AND message_id='700'"
        )).fetchone()
    check("late save keeps recalled_at", bool(message["recalled_at"]), True)
    check("late save keeps recall event", event_count["n"], 1)

    print("== 4. partial pages mark only delivered rows ==")
    for index in range(3):
        await insert_message(2, f"partial-{index}", [{"type": "text", "data": {"text": str(index)}}])
        await insert_recall(2, f"partial-{index}")

    async def no_io(*args, **kwargs):
        return None

    counter = {"value": 0}

    def fake_render(*args, **kwargs):
        counter["value"] += 1
        path = TEMP / f"partial-card-{counter['value']}.png"
        path.write_bytes(b"png")
        return path

    old_page_size = recall_history.page_size_for
    old_render = recall_history.render_recall_page
    old_avatar = recall_history.cache_avatar
    old_images = recall_history.cache_message_images
    old_paths = recall_history.cached_image_paths
    recall_history.page_size_for = lambda maximum: 1
    recall_history.render_recall_page = fake_render
    recall_history.cache_avatar = no_io
    recall_history.cache_message_images = no_io
    recall_history.cached_image_paths = lambda row: []
    try:
        delivered = await send_recall_history(
            Bot(fail_send=2), RecallRequest(group_id=2, user_id=9, group_name="测试群"), None
        )
    finally:
        recall_history.page_size_for = old_page_size
        recall_history.render_recall_page = old_render
        recall_history.cache_avatar = old_avatar
        recall_history.cache_message_images = old_images
        recall_history.cached_image_paths = old_paths
    async with connect() as db:
        pending = await (await db.execute(
            "SELECT COUNT(*) AS n FROM recall_events WHERE group_id='2' AND summoned_at IS NULL"
        )).fetchone()
    check("partial send returns delivered count", delivered, 1)
    check("partial send leaves only unsent rows", pending["n"], 2)

    print("== 5. recalled video is sent from permanent archive ==")
    archive.ARCHIVE_ROOT = TEMP / "video_archive"
    video_content = b"original-video-content"
    video_sha = hashlib.sha256(video_content).hexdigest()
    video_path = archive.stored_path(video_sha, ".mp4")
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(video_content)
    video_segments = [{"type": "video", "data": {"file": "expired.mp4"}}]
    await insert_message(1, "video-1", video_segments)
    await insert_media_row(1, "video-1", video_sha, ".mp4")
    await insert_recall(1, "video-1")

    counter["value"] = 0
    old_page_size = recall_history.page_size_for
    old_render = recall_history.render_recall_page
    old_avatar = recall_history.cache_avatar
    old_images = recall_history.cache_message_images
    old_paths = recall_history.cached_image_paths
    recall_history.page_size_for = lambda maximum: 1
    recall_history.render_recall_page = fake_render
    recall_history.cache_avatar = no_io
    recall_history.cache_message_images = no_io
    recall_history.cached_image_paths = lambda row: []
    video_bot = Bot()
    try:
        video_delivered = await send_recall_history(
            video_bot, RecallRequest(group_id=1, user_id=9, group_name="测试群"), 1
        )
    finally:
        recall_history.page_size_for = old_page_size
        recall_history.render_recall_page = old_render
        recall_history.cache_avatar = old_avatar
        recall_history.cache_message_images = old_images
        recall_history.cached_image_paths = old_paths
    recalled_videos = [
        message
        for message in video_bot.messages
        if isinstance(message, MessageSegment) and message.type == "video"
    ]
    check("video recall delivered one event", video_delivered, 1)
    check("video recall sends original video", len(recalled_videos), 1)
    check("video recall uses archived URI", recalled_videos[0].data["file"], video_path.resolve().as_uri())

    async def run_recall(bot: Bot, count: int = 1, *, private: bool = False) -> int:
        saved = (
            recall_history.page_size_for,
            recall_history.render_recall_page,
            recall_history.cache_avatar,
            recall_history.cache_message_images,
            recall_history.cached_image_paths,
        )
        counter["value"] = 0
        recall_history.page_size_for = lambda maximum: 1
        recall_history.render_recall_page = fake_render
        recall_history.cache_avatar = no_io
        recall_history.cache_message_images = no_io
        recall_history.cached_image_paths = lambda row: []
        try:
            return await send_recall_history(
                bot,
                RecallRequest(group_id=1, user_id=9, group_name="测试群", private=private),
                count,
            )
        finally:
            (
                recall_history.page_size_for,
                recall_history.render_recall_page,
                recall_history.cache_avatar,
                recall_history.cache_message_images,
                recall_history.cached_image_paths,
            ) = saved

    print("== 6. recalled file is sent from permanent archive ==")
    file_content = b"original-file-content"
    file_sha = hashlib.sha256(file_content).hexdigest()
    file_path = archive.stored_path(file_sha, ".pdf")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(file_content)
    await insert_message(
        1,
        "file-1",
        [{"type": "file", "data": {"file": "报告.pdf", "file_size": "4096"}}],
    )
    await insert_media_row(1, "file-1", file_sha, ".pdf", kind="file")
    await insert_recall(1, "file-1")

    file_bot = Bot()
    file_delivered = await run_recall(file_bot)
    check("file recall delivered one event", file_delivered, 1)
    check("file recall uploads one file", len(file_bot.uploads), 1)
    check("file recall uses archived path", file_bot.uploads[0].get("file"), str(file_path))
    check("file recall keeps original name", file_bot.uploads[0].get("name"), "报告.pdf")

    print("== 7. recalled chat record stays card-only ==")
    await insert_message(1, "forward-1", [{"type": "forward", "data": {"id": "f1"}}])
    await insert_recall(1, "forward-1")
    forward_bot = Bot()
    forward_delivered = await run_recall(forward_bot)
    check("forward recall delivered one event", forward_delivered, 1)
    check("forward recall uploads nothing", len(forward_bot.uploads), 0)

    print("== 8. private recall sends file to the requester ==")
    file2_content = b"private-file-content"
    file2_sha = hashlib.sha256(file2_content).hexdigest()
    file2_path = archive.stored_path(file2_sha, ".zip")
    file2_path.parent.mkdir(parents=True, exist_ok=True)
    file2_path.write_bytes(file2_content)
    await insert_message(
        1,
        "file-2",
        [{"type": "file", "data": {"file": "资料.zip", "file_size": "1234"}}],
    )
    await insert_media_row(1, "file-2", file2_sha, ".zip", kind="file")
    await insert_recall(1, "file-2")
    private_bot = Bot()
    private_delivered = await run_recall(private_bot, private=True)
    check("private file recall delivered", private_delivered, 1)
    check("private file uploaded to user", private_bot.uploads[0].get("user_id"), 9)
    check_true("private file has no group_id", "group_id" not in private_bot.uploads[0])

    if failures:
        print(f"{len(failures)} 项失败：")
        for failure in failures:
            print("  -", failure)
        raise SystemExit(1)
    print("全部通过")


asyncio.run(main())
