"""多媒体永久归档的回归测试。

覆盖：内容寻址落盘（去重）、四种媒体类型、超限与不可达的**留痕**（不能静默当"没有多媒体"）、
无多媒体消息的进度标记、以及补归档扫描。
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import asyncio
import hashlib
import sys
from pathlib import Path

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
DB = TEMP / "verify_archive.sqlite3"
FIXTURES = TEMP / "archive_fixtures"
STORE = TEMP / "archive_store"
for stale in TEMP.glob("verify_archive.sqlite3*"):
    stale.unlink()
for directory in (FIXTURES, STORE):
    if directory.exists():
        for item in directory.rglob("*"):
            if item.is_file():
                item.unlink()

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001",
    authorized_users="100000009",
    database_path=str(DB),
)

from plugins.group_bot.media import archive  # noqa: E402
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402

GROUP = "123450001"
failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


class DummyBot:
    """归档走本地文件时不需要它；保留最小形状以防图片走 OneBot 解析。"""

    async def call_api(self, *args, **kwargs):
        raise RuntimeError("no OneBot in tests")


def segment(kind: str, path: Path) -> dict:
    return {"type": kind, "data": {"file": str(path)}}


async def rows_of(message_id: str) -> list[dict]:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT * FROM message_media WHERE group_id=? AND message_id=? ORDER BY part_index",
            (GROUP, message_id),
        )
        return [dict(row) for row in await cursor.fetchall()]


async def archived_at(message_id: str) -> str | None:
    async with connect() as db:
        cursor = await db.execute(
            "SELECT archived_at FROM messages WHERE group_id=? AND message_id=?",
            (GROUP, message_id),
        )
        row = await cursor.fetchone()
        return row["archived_at"] if row else None


async def insert_message(message_id: str, segments: list[dict]) -> None:
    import json

    async with connect() as db:
        await db.execute(
            """INSERT OR IGNORE INTO messages
               (message_id, group_id, user_id, sender_name, message_json, plain_text, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (message_id, GROUP, "777", "甲", json.dumps(segments, ensure_ascii=False), "", "2026-10-18T00:00:00+00:00"),
        )
        await db.commit()


def main() -> None:
    asyncio.run(initialize_database())
    archive.ARCHIVE_ROOT = STORE

    print("=== 1. 内容寻址与后缀识别 ===")
    path = archive.stored_path("abcdef0123456789", ".JPG")
    check("按 sha 前 4 位分两级目录", path.parent.parent.name, "ab")
    check("扩展名小写", path.suffix, ".jpg")
    check("文件名是完整 sha", path.stem, "abcdef0123456789")
    check("JPEG 魔数识别", archive.sniff_suffix(b"\xff\xd8\xff\xe0abc", ".bin"), ".jpg")
    check("PNG 魔数识别", archive.sniff_suffix(b"\x89PNG\r\n\x1a\n....", ".bin"), ".png")
    check("MP4 识别", archive.sniff_suffix(b"\x00\x00\x00\x18ftypmp42", ".bin"), ".mp4")
    check("AMR 识别", archive.sniff_suffix(b"#!AMR\n....", ".bin"), ".amr")
    check("认不出就用兜底", archive.sniff_suffix(b"\x00\x01\x02\x03", ".bin"), ".bin")

    print("\n=== 2. 四种媒体都归档，内容寻址 ===")
    FIXTURES.mkdir(parents=True, exist_ok=True)
    image = FIXTURES / "a.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0" + b"img-content" * 10)
    video = FIXTURES / "v.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"vid" * 100)
    record = FIXTURES / "r.amr"
    record.write_bytes(b"#!AMR\n" + b"\x3c" * 40)
    doc = FIXTURES / "f.pdf"
    doc.write_bytes(b"%PDF-1.4\n" + b"pdf" * 50)

    segments = [segment("image", image), segment("video", video), segment("record", record), segment("file", doc)]
    asyncio.run(insert_message("m1", segments))
    stored = asyncio.run(archive.archive_message(DummyBot(), GROUP, "m1", segments))
    check("四段全部落盘", stored, 4)
    rows = asyncio.run(rows_of("m1"))
    check("写了四行", len(rows), 4)
    check("类型正确", [row["kind"] for row in rows], ["image", "video", "record", "file"])
    check("没有错误行", [row["error"] for row in rows], ["", "", "", ""])
    check_true("sha256 与内容一致", rows[0]["sha256"] == hashlib.sha256(image.read_bytes()).hexdigest())
    check_true("文件真的在内容寻址路径上", archive.stored_path(rows[0]["sha256"], rows[0]["suffix"]).is_file())
    check("PDF 后缀被识别", rows[3]["suffix"], ".pdf")
    check_true("消息标记为已归档", asyncio.run(archived_at("m1")) is not None)

    print("\n=== 3. 同一张图被转发两次只占一份 ===")
    asyncio.run(insert_message("m2", [segment("image", image)]))
    asyncio.run(archive.archive_message(DummyBot(), GROUP, "m2", [segment("image", image)]))
    rows2 = asyncio.run(rows_of("m2"))
    check("两行 sha 相同", (rows[0]["sha256"], rows2[0]["sha256"]), (rows[0]["sha256"], rows[0]["sha256"]))
    files = [item for item in STORE.rglob("*") if item.is_file()]
    check("磁盘上仍是四份文件", len(files), 4)

    print("\n=== 4. 超限与不可达都要留痕 ===")
    original_limit = archive.ARCHIVE_MAX_BYTES
    archive.ARCHIVE_MAX_BYTES = 100
    try:
        big = FIXTURES / "big.bin"
        big.write_bytes(b"x" * 500)
        asyncio.run(insert_message("m3", [segment("file", big)]))
        stored = asyncio.run(archive.archive_message(DummyBot(), GROUP, "m3", [segment("file", big)]))
        check("超限不落盘", stored, 0)
        rows3 = asyncio.run(rows_of("m3"))
        check("但留了一行", len(rows3), 1)
        check_true("原因写了上限", "归档上限" in rows3[0]["error"])
    finally:
        archive.ARCHIVE_MAX_BYTES = original_limit

    unreachable = {"type": "video", "data": {"url": "https://example.invalid/x.mp4"}}
    asyncio.run(insert_message("m4", [unreachable]))
    asyncio.run(archive.archive_message(DummyBot(), GROUP, "m4", [unreachable]))
    rows4 = asyncio.run(rows_of("m4"))
    check("不可达也留一行", len(rows4), 1)
    check_true("原因非空", bool(rows4[0]["error"]))
    check("没有 sha（没存）", rows4[0]["sha256"], "")

    print("\n=== 5. 没有多媒体的消息也要标记进度 ===")
    asyncio.run(insert_message("m5", [{"type": "text", "data": {"text": "hi"}}]))
    asyncio.run(archive.archive_message(DummyBot(), GROUP, "m5", [{"type": "text", "data": {"text": "hi"}}]))
    check("没有 message_media 行", len(asyncio.run(rows_of("m5"))), 0)
    check_true("但仍然标记已处理（避免扫描反复重试）", asyncio.run(archived_at("m5")) is not None)

    print("\n=== 6. 补归档扫描 ===")
    pending_image = FIXTURES / "pending.png"
    pending_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"png" * 20)
    asyncio.run(insert_message("m6", [segment("image", pending_image)]))
    check("插入后还没归档", asyncio.run(archived_at("m6")), None)
    asyncio.run(archive.archive_pending(DummyBot(), limit=10))
    rows6 = asyncio.run(rows_of("m6"))
    check("扫描补上了", len(rows6), 1)
    check_true("扫描后标记完成", asyncio.run(archived_at("m6")) is not None)
    check("再次扫描不重复写", len(asyncio.run(rows_of("m6"))), 1)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


main()
