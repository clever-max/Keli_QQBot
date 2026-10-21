"""媒体收敛的回归测试：渲染缓存优先从归档复制，而不是再打网络。

验证"归档是唯一权威副本、media_cache 只是可删除的派生缓存"：
- 归档里有图 → `cache_message_images` 直接复制，`cached_path` 指向缓存文件；
- 归档里没有 → 才走网络（这里用一个必抛异常的假 bot + 必失败的 client 来证明"没走网络"）。
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import asyncio
import sys
from pathlib import Path

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
DB = TEMP / "verify_converge.sqlite3"
ARCHIVE = TEMP / "converge_archive"
CACHE = TEMP / "converge_cache"
for stale in TEMP.glob("verify_converge.sqlite3*"):
    stale.unlink()
for directory in (ARCHIVE, CACHE):
    if directory.exists():
        for item in directory.rglob("*"):
            if item.is_file():
                item.unlink()

sys.path.insert(0, str(ROOT))
import httpx  # noqa: E402
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001",
    authorized_users="100000009",
    database_path=str(DB),
)

from plugins.group_bot.media import archive, pipeline as media  # noqa: E402
from plugins.group_bot.core.config import DB_PATH  # noqa: E402
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402

GROUP = 123450001
failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


class NoNetworkBot:
    async def call_api(self, *args, **kwargs):
        raise RuntimeError("测试里不该走 OneBot")


def segment(image: Path) -> dict:
    return {"type": "image", "data": {"file": str(image)}}


async def main() -> None:
    await initialize_database()
    archive.ARCHIVE_ROOT = ARCHIVE

    source = TEMP / "converge_src.jpg"
    source.write_bytes(b"\xff\xd8\xff\xe0" + b"payload" * 100)
    segments = [segment(source)]

    async with connect() as db:
        await db.execute(
            """INSERT INTO messages (group_id, message_id, user_id, sender_name,
               message_json, plain_text, sent_at) VALUES (?,?,?,?,?,?,?)""",
            (str(GROUP), "7001", "1", "甲", "[]", "", "2026-10-18T00:00:00+00:00"),
        )

    print("=== 1. 先归档 ===")
    stored = await archive.archive_message(NoNetworkBot(), str(GROUP), "7001", segments)
    check("归档 1 份", stored, 1)

    print("\n=== 2. 渲染缓存从归档复制（不联网）===")
    target = [{"type": "image", "data": {"file": str(source)}}]
    async with httpx.AsyncClient(timeout=1) as client:
        await media.cache_message_images(NoNetworkBot(), GROUP, "7001", target, client)
    cached = target[0]["data"].get("cached_path")
    check_true("cached_path 被写上", bool(cached))
    cache_file = DB_PATH.parent / str(cached)
    check_true("缓存文件存在", cache_file.is_file())
    check("缓存内容与归档一致", cache_file.read_bytes(), source.read_bytes())

    print("\n=== 3. 归档里没有时才会走网络（用必失败的桩证明）===")
    missing = [{"type": "image", "data": {"file": str(TEMP / "does_not_exist.jpg")}}]
    async with httpx.AsyncClient(timeout=1) as client:
        await media.cache_message_images(NoNetworkBot(), GROUP, "7002", missing, client)
    check("拿不到就不写 cached_path", missing[0]["data"].get("cached_path"), None)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
