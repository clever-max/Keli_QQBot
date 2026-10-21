"""`messages` 复合主键迁移的回归测试。

覆盖：旧库（单列主键）→ 新库（复合主键）的迁移、行数不丢、跨群同 id 能共存、
索引补齐（`message_id` 单列索引必须存在，否则撤回 JOIN 会退化成全表扫描）、幂等。
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import sqlite3
import sys
from pathlib import Path

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
DB = TEMP / "verify_pk.sqlite3"
for stale in TEMP.glob("verify_pk.sqlite3*"):
    stale.unlink()

# ---- 先造一个"旧结构"的库（单列主键） -------------------------------------
old = sqlite3.connect(DB)
old.execute(
    """CREATE TABLE messages (
        message_id TEXT PRIMARY KEY,
        group_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        sender_name TEXT NOT NULL,
        message_json TEXT NOT NULL,
        plain_text TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        recalled_at TEXT,
        recalled_by TEXT
    )"""
)
old.execute("CREATE INDEX idx_messages_group_time ON messages(group_id, sent_at DESC)")
old.executemany(
    "INSERT INTO messages (message_id, group_id, user_id, sender_name, message_json,"
    " plain_text, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
    [
        ("100", "g1", "u1", "甲", "[]", "hi", "2026-10-01T00:00:00+00:00"),
        ("101", "g1", "u2", "乙", "[]", "hello", "2026-10-01T00:01:00+00:00"),
        ("102", "g2", "u3", "丙", "[]", "yo", "2026-10-01T00:02:00+00:00"),
    ],
)
old.commit()
old.close()

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001",
    authorized_users="100000009",
    database_path=str(DB),
)

from plugins.group_bot.core import store  # noqa: E402

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


def columns(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("PRAGMA table_info(messages)").fetchall()
    keyed = [(int(row[5]), row[1]) for row in rows if int(row[5]) > 0]
    return [name for _, name in sorted(keyed)]


def main() -> None:
    import asyncio

    conn = sqlite3.connect(DB)
    check("迁移前是单列主键", columns(conn), ["message_id"])
    check("迁移前 3 行", conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)
    conn.close()

    print("\n=== 1. 执行迁移 ===")
    asyncio.run(store.initialize_database())
    conn = sqlite3.connect(DB)
    check("主键变成 (group_id, message_id)", columns(conn), ["group_id", "message_id"])
    check("行数没变", conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 3)
    check("内容还在", conn.execute("SELECT plain_text FROM messages WHERE message_id='101'").fetchone()[0], "hello")

    print("\n=== 2. 索引补齐（否则撤回 JOIN 会退化）===")
    names = {row[1] for row in conn.execute("PRAGMA index_list(messages)")}
    check_true("有 message_id 单列索引", "idx_messages_message_id" in names)
    check_true("有 group_time 索引", "idx_messages_group_time" in names)
    plan = " | ".join(
        str(row[3])
        for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT m.message_json FROM recall_events e "
            "JOIN messages m ON m.message_id = e.message_id WHERE e.group_id=?",
            ("g1",),
        )
    )
    check_true(f"JOIN 走索引而不是全表（{plan[:80]}）", "SEARCH m" in plan)

    print("\n=== 3. 跨群复用同一 message_id 不再被丢 ===")
    conn.execute(
        "INSERT OR IGNORE INTO messages (group_id, message_id, user_id, sender_name,"
        " message_json, plain_text, sent_at) VALUES ('g2','100','u9','丁','[]','dup','2026-10-02T00:00:00+00:00')"
    )
    conn.commit()
    check(
        "两个群都有 100",
        conn.execute("SELECT COUNT(*) FROM messages WHERE message_id='100'").fetchone()[0],
        2,
    )
    conn.execute(
        "INSERT OR IGNORE INTO messages (group_id, message_id, user_id, sender_name,"
        " message_json, plain_text, sent_at) VALUES ('g1','100','u9','丁','[]','dup','2026-10-02T00:00:00+00:00')"
    )
    conn.commit()
    check(
        "同群重复插入仍被忽略",
        conn.execute("SELECT COUNT(*) FROM messages WHERE message_id='100' AND group_id='g1'").fetchone()[0],
        1,
    )
    conn.close()

    print("\n=== 4. 幂等：再跑一次不重复迁移 ===")
    async def run_again() -> bool:
        async with store.connect() as db:
            return await store.migrate_messages_primary_key(db)

    check_true("第二次不再迁移", asyncio.run(run_again()) is False)
    conn = sqlite3.connect(DB)
    check("行数仍然正确", conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 4)
    conn.close()

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


main()
