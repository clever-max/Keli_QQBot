"""增量总结：只总结"上次总结之后到现在"的消息；没有截止缓存就回退到前 4 小时。

为什么改成增量：原来的"昨日总结"每天只看**一整天**，一天里想再看一次就得等内容变多；
而主人真正想要的是"**从上次总结到现在都发生了什么**"。

截止点怎么存：``summary_checkpoint`` 表按群记 ``(last_sent_at, last_message_id)``。
窗口是 **左开右闭** ``(last_sent_at, now]``，所以不会把上次已经总结过的那条再算一遍。
没有记录（第一次用，或换了库）时用 ``now - 4 小时``，这个回退值写在常量里。

OCR：窗口里带图片的消息会调 NapCat 的本地 OCR（``runtime.ocr``），把图里的字并进摘要——
群里很多信息是截图，只给 LLM 一串 ``[图片]`` 等于没说。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from ..core.config import GROUP_TIMEZONE, SUMMARY_DIGEST_CHARS, SUMMARY_MAX_CHARS, SUMMARY_USE_LLM
from ..core.store import connect
from . import ocr
from .llm import report_body
from ..common.render import cap_text
from .summary import (
    day_facts,
    detect_repeat,
    local_highlights,
    message_text,
    summary_digest,
)

FALLBACK_HOURS = 4
MAX_MESSAGES = 800
TABLE = "summary_checkpoint"


async def ensure_table() -> None:
    async with connect() as db:
        await db.execute(
            f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                group_id TEXT PRIMARY KEY,
                last_sent_at TEXT NOT NULL,
                last_message_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )"""
        )


async def last_checkpoint(group_id: int) -> str | None:
    await ensure_table()
    async with connect() as db:
        cursor = await db.execute(
            f"SELECT last_sent_at FROM {TABLE} WHERE group_id=?", (str(group_id),)
        )
        row = await cursor.fetchone()
    return str(row["last_sent_at"]) if row else None


async def save_checkpoint(group_id: int, sent_at: str, message_id: str = "") -> None:
    await ensure_table()
    now = datetime.now(timezone.utc).isoformat()
    async with connect() as db:
        await db.execute(
            f"""INSERT INTO {TABLE} (group_id, last_sent_at, last_message_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET
                    last_sent_at=excluded.last_sent_at,
                    last_message_id=excluded.last_message_id,
                    updated_at=excluded.updated_at""",
            (str(group_id), sent_at, message_id, now),
        )


def window_start(checkpoint: str | None, now: datetime) -> datetime:
    """窗口左边界：有截止就用它，没有就回退 ``now - 4h``。"""
    if checkpoint:
        try:
            parsed = datetime.fromisoformat(checkpoint)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            logger.warning("Bad summary checkpoint {!r}, falling back to {}h", checkpoint, FALLBACK_HOURS)
    return now - timedelta(hours=FALLBACK_HOURS)


async def window_rows(group_id: int, since: datetime, until: datetime) -> list:
    await ensure_table()
    async with connect() as db:
        cursor = await db.execute(
            """SELECT * FROM messages
               WHERE group_id=? AND sent_at>? AND sent_at<=?
               ORDER BY sent_at LIMIT ?""",
            (str(group_id), since.astimezone(timezone.utc).isoformat(),
             until.astimezone(timezone.utc).isoformat(), MAX_MESSAGES),
        )
        return await cursor.fetchall()


async def collect_digest(bot: Bot, rows: list, budget: int = SUMMARY_DIGEST_CHARS) -> str:
    """把窗口里的消息拼成交给模型的文本；图片消息附上本地 OCR 结果。"""
    lines: list[str] = []
    used = 0
    ocr_budget = ocr.DEFAULT_OCR_LIMIT
    for row in rows:
        try:
            segments = __import__("json").loads(row["message_json"])
        except Exception:
            segments = []
        text = message_text(segments)
        if any(str(s.get("type")) == "image" for s in segments) and ocr_budget > 0:
            try:
                texts = await ocr.ocr_images(bot, segments, limit=min(2, ocr_budget))
            except Exception:
                texts = []
            ocr_budget -= min(2, ocr_budget)
            if texts:
                text = f"{text} ［图中文字］{' '.join(texts)}".strip()
        if not text:
            continue
        line = f"{row['sender_name']}: {text}"
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


async def send_incremental_summary(bot: Bot, event: GroupMessageEvent) -> None:
    """总结"上次到现在"（或前 4 小时）的消息。"""
    group_id = int(event.group_id)
    now = datetime.now(GROUP_TIMEZONE)
    checkpoint = await last_checkpoint(group_id)
    since = window_start(checkpoint, now)

    try:
        rows = await window_rows(group_id, since, now)
    except Exception:
        logger.exception("Incremental summary: could not read the window")
        await bot.send_group_msg(group_id=group_id, message="可莉翻记录的时候卡住了，等会儿再试吧。")
        return

    span = f"{since.astimezone(GROUP_TIMEZONE).strftime('%m-%d %H:%M')} → {now.strftime('%H:%M')}"
    if not rows:
        await bot.send_group_msg(
            group_id=group_id,
            message=f"从 {span} 这条线上没有新消息哦，可莉的笔记还是空的～",
        )
        await save_checkpoint(group_id, now.astimezone(timezone.utc).isoformat())
        return

    facts = day_facts(rows, str(bot.self_id), group_id)
    repeat = detect_repeat(facts["entries"], 4)
    digest = await collect_digest(bot, rows)
    body = ""
    if SUMMARY_USE_LLM:
        try:
            body = await report_body(f"最近一段（{span}）", facts, digest, repeat)
        except Exception:
            logger.exception("Incremental summary: LLM failed")
    if not body:
        body = local_highlights(facts["entries"])

    report = cap_text(
        f"【增量总结 {span}】共 {len(rows)} 条消息\n{body}",
        SUMMARY_MAX_CHARS,
    )
    await bot.send_group_msg(group_id=group_id, message=report)
    newest = rows[-1]
    await save_checkpoint(group_id, str(newest["sent_at"]), str(newest["message_id"]))
    logger.info(
        "Incremental summary sent for {} ({} rows, since {})", group_id, len(rows), span
    )
