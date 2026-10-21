"""增量总结的回归测试：4 小时回退、截止点左开、OCR 并入、窗口为空。"""

from __future__ import annotations

import tempfile

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = Path(tempfile.gettempdir()) / "recall_verify" / "verify_incremental.sqlite3"
for stale in DB.parent.glob("verify_incremental.sqlite3*"):
    stale.unlink()

import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001",
    authorized_users="100000009",
    database_path=str(DB),
)

from plugins.group_bot.core.config import GROUP_TIMEZONE  # noqa: E402
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402
from plugins.group_bot.runtime import incremental  # noqa: E402

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


class OcrBot:
    """假 bot：ocr_image 返回 QQ 那种 textDetections 结构。"""

    def __init__(self) -> None:
        self.calls = 0

    async def call_api(self, action, **kwargs):
        self.calls += 1
        return {"data": {"textDetections": [{"text": "开会改到三点"}, {"text": "带上笔记本"}]}}


async def insert(sent_at: str, message_id: str, text: str, segments=None) -> None:
    payload = segments or [{"type": "text", "data": {"text": text}}]
    async with connect() as db:
        await db.execute(
            """INSERT INTO messages (group_id, message_id, user_id, sender_name,
               message_json, plain_text, sent_at) VALUES (?,?,?,?,?,?,?)""",
            (str(GROUP), message_id, "1", "甲",
             json.dumps(payload, ensure_ascii=False), text, sent_at),
        )


async def main() -> None:
    await initialize_database()

    print("=== 1. 没有截止缓存时回退 4 小时 ===")
    now = datetime(2026, 10, 21, 14, 0, tzinfo=GROUP_TIMEZONE)
    start = incremental.window_start(None, now)
    check("回退到前 4 小时", (now - start).total_seconds(), 4 * 3600)
    check("回退常量是 4", incremental.FALLBACK_HOURS, 4)

    print("\n=== 2. 有截止缓存就用它 ===")
    checkpoint_time = (now - timedelta(minutes=90)).astimezone(timezone.utc).isoformat()
    check("用截止点", incremental.window_start(checkpoint_time, now).isoformat(), checkpoint_time)
    check("坏值回退 4 小时",
          (now - incremental.window_start("not-a-date", now)).total_seconds(), 4 * 3600)

    print("\n=== 3. 截止点持久化（左开）===")
    check("初始没有截止", await incremental.last_checkpoint(GROUP), None)
    t1 = (now - timedelta(minutes=60)).astimezone(timezone.utc).isoformat()
    t2 = (now - timedelta(minutes=30)).astimezone(timezone.utc).isoformat()
    t3 = (now - timedelta(minutes=10)).astimezone(timezone.utc).isoformat()
    await insert(t1, "m1", "一")
    await insert(t2, "m2", "二")
    await insert(t3, "m3", "三")
    await incremental.save_checkpoint(GROUP, t2, "m2")
    check("读回截止", await incremental.last_checkpoint(GROUP), t2)
    rows = await incremental.window_rows(GROUP, incremental.window_start(t2, now), now)
    check("只取截止之后的消息（不含截止那条）", [r["message_id"] for r in rows], ["m3"])

    print("\n=== 4. OCR 并入摘要 ===")
    bot = OcrBot()
    image_row = {
        "sender_name": "乙",
        "message_json": json.dumps(
            [{"type": "image", "data": {"file": "abc.png", "url": "https://x/y.png"}}],
            ensure_ascii=False,
        ),
    }
    digest = await incremental.collect_digest(bot, [image_row])
    check_true("调用了 OCR", bot.calls >= 1)
    check_true("图中文字进入摘要", "开会改到三点" in digest)
    check_true("带发送者", "乙:" in digest)

    print("\n=== 5. 空窗口也要推进截止点 ===")
    later = now + timedelta(minutes=5)
    rows = await incremental.window_rows(GROUP, incremental.window_start(t3, later), later)
    check("t3 之后没有消息", rows, [])
    await incremental.save_checkpoint(GROUP, later.astimezone(timezone.utc).isoformat())
    check_true("截止点被推进", await incremental.last_checkpoint(GROUP) is not None)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
