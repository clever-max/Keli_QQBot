"""SQLite access: a single-writer connection and schema initialisation.

为什么是"一条常驻连接 + 一把锁"（2026-10-18 改）：

* **正确性**。SQLite ≤3.51.2 有一个 WAL-reset 数据竞争（3.51.3 修复），触发条件正是
  "多个连接同时写/checkpoint"。本机 Python 自带的 sqlite 是 3.50.4，收敛成单写者等于
  从根上避开它，而不是赌版本。
* **性能**。原来每次操作都 `connect()`：开连接 + 两句 PRAGMA，实测 **2.38 ms**，而复用
  连接上同一条查询只要 **0.17 ms**（13×）。一次漂流瓶抽取有 6 次 DB 操作，等于白花 13 ms。
* **少一类 bug**。以前 `connect()` 不自动提交，忘写 `commit()` 的写入会被静默丢弃
  （归档进度标记就踩过一次）。现在**成功即提交、异常即回滚**。

重入规则：同一个任务里嵌套 `connect()` 会复用外层连接与事务（用 contextvar 判断），
否则"自己等自己的锁"会死锁。换事件循环（测试里多次 `asyncio.run`）会自动重开连接。
"""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite
from nonebot import logger

from .config import ALLOWED_GROUP_IDS, DB_PATH, driver

# WAL 上限 64 MiB：超过就让 SQLite 在 checkpoint 后截断，避免 -wal 文件无限长。
_JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024
# 每多少次提交做一次被动 checkpoint（被动 = 不阻塞写者）。
_CHECKPOINT_EVERY = 200

_connection: aiosqlite.Connection | None = None
_connection_loop: asyncio.AbstractEventLoop | None = None
_lock: asyncio.Lock = asyncio.Lock()
_depth: contextvars.ContextVar[int] = contextvars.ContextVar("store_depth", default=0)
_commits = 0


def _force_daemon_worker_threads() -> None:
    """让 aiosqlite 的工作线程变成守护线程。

    aiosqlite 用 ``Thread(target=...)``（默认非守护）承载每条连接。换成"常驻连接"后，
    任何用到 store 的**脚本**都会在退出时卡死：解释器在 ``threading._shutdown`` 里等这个
    永远阻塞在队列上的线程（实测 15 个套件全部挂在退出阶段）。

    正常关闭由下面的 ``on_shutdown`` 负责；这里的守护线程只是兜底，保证"忘了关"也不会
    卡住进程退出。守护线程被回收时进行中的事务会回滚，而我们已经提交过的事务在 WAL 里，
    不会丢。
    """

    import threading

    import aiosqlite.core as core

    if getattr(core, "_dsh_daemon_threads", False):
        return

    class _DaemonThread(threading.Thread):
        def __init__(self, *args, **kwargs):
            kwargs["daemon"] = True
            super().__init__(*args, **kwargs)

    core.Thread = _DaemonThread
    core._dsh_daemon_threads = True


async def _open() -> aiosqlite.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _force_daemon_worker_threads()
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    # 单写者下 synchronous=NORMAL 是安全且更快的选择：断电最多丢最后一个事务，
    # 不会损坏数据库（FULL 只为满足"绝不丢事务"，代价是每次提交一次 fsync）。
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute(f"PRAGMA journal_size_limit={_JOURNAL_SIZE_LIMIT}")
    return db


async def _ensure() -> aiosqlite.Connection:
    """取常驻连接；跨事件循环（或已关闭）就重开。"""
    global _connection, _connection_loop
    loop = asyncio.get_running_loop()
    if _connection is not None and (_connection_loop is not loop or loop.is_closed()):
        try:
            await _connection.close()
        except Exception:  # noqa: BLE001  旧循环里的连接可能已经不可用
            pass
        _connection = None
    if _connection is None:
        _connection = await _open()
        _connection_loop = loop
        logger.debug("SQLite single-writer connection opened")
    return _connection


async def close_connection() -> None:
    """关掉常驻连接（测试与进程退出用；下次 connect() 会自动重开）。"""
    global _connection
    if _connection is not None:
        try:
            await _connection.close()
        finally:
            _connection = None


@driver.on_shutdown
async def _on_shutdown_close() -> None:
    """停机时正常关闭连接（把 WAL 收尾，别只靠守护线程被回收）。"""
    logger.debug("Closing the SQLite connection on shutdown")
    await close_connection()


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    """借出常驻连接：成功自动提交，异常自动回滚。"""
    global _commits
    if _depth.get() > 0:
        # 嵌套调用：复用外层连接与事务，不再抢锁。
        yield await _ensure()
        return
    async with _lock:
        db = await _ensure()
        token = _depth.set(1)
        try:
            yield db
        except BaseException:
            await db.rollback()
            raise
        else:
            await db.commit()
            _commits += 1
            if _commits % _CHECKPOINT_EVERY == 0:
                try:
                    await db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except aiosqlite.Error:
                    logger.debug("WAL checkpoint failed", exc_info=True)
        finally:
            _depth.reset(token)


async def _messages_primary_key_columns(db: aiosqlite.Connection) -> list[str]:
    """返回当前 `messages` 主键包含的列（按 pk 顺序）。"""
    cursor = await db.execute("PRAGMA table_info(messages)")
    rows = await cursor.fetchall()
    keyed = [(int(row["pk"]), str(row["name"])) for row in rows if int(row["pk"]) > 0]
    return [name for _, name in sorted(keyed)]


async def migrate_messages_primary_key(db: aiosqlite.Connection) -> bool:
    """旧库（`message_id` 单列主键）就地重建为 `(group_id, message_id)`。返回是否迁移。

    重建期间不丢数据：建新表 → 整表复制 → 校验行数 → 删旧表 → 改名；全程在一个事务里
    （`connect()` 成功即提交、异常回滚）。
    """
    columns = await _messages_primary_key_columns(db)
    if columns == ["group_id", "message_id"]:
        return False
    if columns != ["message_id"]:
        logger.warning("Unexpected primary key on messages: {}", columns)
        return False

    logger.info("Migrating messages primary key to (group_id, message_id)")
    await db.execute(
        """CREATE TABLE messages_new (
            group_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            sender_name TEXT NOT NULL,
            message_json TEXT NOT NULL,
            plain_text TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            recalled_at TEXT,
            recalled_by TEXT,
            PRIMARY KEY (group_id, message_id)
        )"""
    )
    await db.execute(
        """INSERT INTO messages_new
           (group_id, message_id, user_id, sender_name, message_json,
            plain_text, sent_at, recalled_at, recalled_by)
           SELECT group_id, message_id, user_id, sender_name, message_json,
                  plain_text, sent_at, recalled_at, recalled_by
           FROM messages"""
    )
    before = (await (await db.execute("SELECT COUNT(*) AS n FROM messages")).fetchone())["n"]
    after = (await (await db.execute("SELECT COUNT(*) AS n FROM messages_new")).fetchone())["n"]
    if before != after:
        raise RuntimeError(f"messages 迁移行数不一致（{before} -> {after}），已回滚")
    await db.execute("DROP TABLE messages")
    await db.execute("ALTER TABLE messages_new RENAME TO messages")
    logger.info("messages primary key migrated ({} rows)", after)
    return True


@driver.on_startup
async def initialize_database() -> None:
    async with connect() as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                group_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                message_json TEXT NOT NULL,
                plain_text TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                recalled_at TEXT,
                recalled_by TEXT,
                PRIMARY KEY (group_id, message_id)
            )"""
        )
        await migrate_messages_primary_key(db)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_group_time "
            "ON messages(group_id, sent_at DESC)"
        )
        # 复合主键的索引是 (group_id, message_id)，帮不上"只按 message_id 查"的路径
        # （撤回记录的 JOIN、mark_recalled）。旧表靠 PK 的隐式索引，重建后必须显式补上。
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id)"
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS recall_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                recalled_at TEXT NOT NULL,
                summoned_at TEXT,
                UNIQUE(group_id, message_id)
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_recall_events_group_event "
            "ON recall_events(group_id, event_id)"
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS recall_cursor (
                group_id TEXT PRIMARY KEY,
                last_event_id INTEGER NOT NULL DEFAULT 0,
                last_summoned_at TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS mention_cooldowns (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                cooldown_until REAL NOT NULL,
                PRIMARY KEY(group_id, user_id)
            )"""
        )
        # One row per user, per group, per local day: how many 群精华 draws were
        # used, when the last one happened, and which cooldown/daily-limit notice
        # was already sent (every notice is sent at most once).
        await db.execute(
            """CREATE TABLE IF NOT EXISTS essence_usage (
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                last_draw_at REAL NOT NULL DEFAULT 0,
                limit_notified INTEGER NOT NULL DEFAULT 0,
                cooldown_notified_at REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(group_id, user_id, day)
            )"""
        )
        # Every essence message already drawn in a group on a local day, so the
        # same essence never comes back twice in one day (survives restarts).
        # ``label`` keeps a short human-readable name for the content: the live
        # essence list moves on, but the 「被抽到榜」 must still be able to say
        # *what* was drawn.  ``essence_draws`` doubles as the draw counter, which
        # is why the (group_id, message_id) index below exists.
        await db.execute(
            """CREATE TABLE IF NOT EXISTS essence_draws (
                group_id TEXT NOT NULL,
                day TEXT NOT NULL,
                message_id TEXT NOT NULL,
                drawn_at TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(group_id, day, message_id)
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_essence_draws_content "
            "ON essence_draws(group_id, message_id)"
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS summary_runs (
                group_id TEXT NOT NULL,
                day TEXT NOT NULL,
                covered_day TEXT NOT NULL,
                state TEXT NOT NULL,
                summary_text TEXT NOT NULL DEFAULT '',
                closing_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                PRIMARY KEY(group_id, day)
            )"""
        )
        # Runtime feature switches (see ``settings``).  They must survive a
        # restart, so they live here rather than in memory or in ``.env``.
        await db.execute(
            """CREATE TABLE IF NOT EXISTS bot_settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        # One shared 撤回记录 window per group (see ``cooldown``): whoever asks
        # first is answered, the rest of the group waits.  ``notified`` makes the
        # "wait N seconds" notice fire once per window instead of on every try.
        await db.execute(
            """CREATE TABLE IF NOT EXISTS group_cooldowns (
                group_id TEXT PRIMARY KEY,
                cooldown_until REAL NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # Each group has its own incremental recall window.
        for group_id in ALLOWED_GROUP_IDS:
            await db.execute(
                "INSERT OR IGNORE INTO recall_cursor(group_id, last_event_id) VALUES (?, 0)",
                (str(group_id),),
            )
        # Databases written before the daily window existed lack ``summoned_at``:
        # add it and treat everything up to the stored cursor as already shown, so
        # the new window neither replays nor silently swallows old recall history.
        cursor = await db.execute("PRAGMA table_info(recall_events)")
        if "summoned_at" not in {str(row["name"]) for row in await cursor.fetchall()}:
            await db.execute("ALTER TABLE recall_events ADD COLUMN summoned_at TEXT")
            await db.execute(
                """UPDATE recall_events SET summoned_at=recalled_at
                   WHERE event_id <= COALESCE(
                       (SELECT last_event_id FROM recall_cursor
                        WHERE recall_cursor.group_id=recall_events.group_id), 0)"""
            )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_recall_events_pending "
            "ON recall_events(group_id, summoned_at, event_id)"
        )
        # Databases written before the cooldown-notice bookkeeping existed need
        # the extra column; 0 means "no notice sent for the current cooldown".
        cursor = await db.execute("PRAGMA table_info(essence_usage)")
        if "cooldown_notified_at" not in {str(row["name"]) for row in await cursor.fetchall()}:
            await db.execute(
                "ALTER TABLE essence_usage ADD COLUMN cooldown_notified_at REAL NOT NULL DEFAULT 0"
            )
        # Databases written before the 「被抽到榜」 label existed need the column too.
        # Existing rows keep an empty label and fall back to a generic name.
        cursor = await db.execute("PRAGMA table_info(essence_draws)")
        if "label" not in {str(row["name"]) for row in await cursor.fetchall()}:
            await db.execute(
                "ALTER TABLE essence_draws ADD COLUMN label TEXT NOT NULL DEFAULT ''"
            )
        # 多媒体永久归档（见 archive.py）：一条消息的每个多媒体段一行。
        # ``sha256`` 是内容寻址的文件名，同一个文件被转发十次也只有一份落盘。
        # ``error`` 非空表示这一段**没存下来**（URL 过期、超限等），不是"没有多媒体"。
        await db.execute(
            """CREATE TABLE IF NOT EXISTS message_media (
                group_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                kind TEXT NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                suffix TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                stored_at TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(group_id, message_id, part_index)
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_media_sha ON message_media(sha256)"
        )
        # 归档进度：NULL = 还没归档过（启动/定时扫描会补），非空 = 已尝试过。
        cursor = await db.execute("PRAGMA table_info(messages)")
        if "archived_at" not in {str(row["name"]) for row in await cursor.fetchall()}:
            await db.execute("ALTER TABLE messages ADD COLUMN archived_at TEXT")
        await db.commit()
