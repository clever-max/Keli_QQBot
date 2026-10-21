"""Daily-report statistics and report rendering.

Every number in the report is computed here, locally, so it can never drift
from what was archived.  Only ``rows_for_day`` touches the database; the rest
are pure functions over already-fetched rows.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

import aiosqlite
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from ..core.config import (
    GREETING_TEMPLATES,
    GROUP_TIMEZONE,
    REPEAT_MIN_COUNT,
    REPEAT_MIN_SENDERS,
    SUMMARY_DIGEST_CHARS,
    SUMMARY_MAX_CHARS,
    SUMMARY_USE_LLM,
)
from .llm import closing_line, report_body
from ..common.render import cap_text
from ..core.state import _member_names
from ..core.store import connect


def safe_member_name(user_id: str, stored_name: str = "", group_id: int = 0) -> str:
    cached = _member_names.get(group_id, {}).get(user_id, "")
    for candidate in (stored_name, cached):
        name = " ".join(str(candidate or "").split())[:32]
        if name and not name.isdigit():
            return name
    return "一位冒险家"


def message_text(segments: Any) -> str:
    """Pure text of a message, or "" when it carries any non-text segment.

    Image/voice/face messages must never count as 复读: a hundred people posting
    different pictures all render to the same placeholder.
    """
    if not isinstance(segments, list) or not segments:
        return ""
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, dict) or str(segment.get("type")) != "text":
            return ""
        parts.append(str(segment.get("data", {}).get("text", "")))
    return " ".join(" ".join(parts).split())


def local_day_bounds(day: date) -> tuple[str, str]:
    start_local = datetime(day.year, day.month, day.day, tzinfo=GROUP_TIMEZONE)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(),
        end_local.astimezone(timezone.utc).isoformat(),
    )


async def rows_for_day(group_id: int, day: date) -> list[aiosqlite.Row]:
    start_utc, end_utc = local_day_bounds(day)
    async with connect() as db:
        cursor = await db.execute(
            """SELECT user_id, sender_name, plain_text, message_json, sent_at
               FROM messages WHERE group_id=? AND sent_at>=? AND sent_at<?
               ORDER BY sent_at ASC""",
            (str(group_id), start_utc, end_utc),
        )
        return list(await cursor.fetchall())


def day_facts(rows: list[aiosqlite.Row], own_id: str, group_id: int) -> dict[str, Any]:
    """Every number of the daily report, computed locally so it can never drift."""
    speakers: Counter[str] = Counter()
    speaker_names: dict[str, str] = {}
    mentioned: Counter[str] = Counter()
    hours: Counter[int] = Counter()
    periods: Counter[str] = Counter()
    entries: list[dict[str, Any]] = []

    for row in rows:
        user_id = str(row["user_id"])
        if user_id == own_id:
            continue
        name = str(row["sender_name"] or "")
        speakers[user_id] += 1
        if name and not name.isdigit():
            speaker_names[user_id] = name

        sent_local = None
        try:
            sent_local = datetime.fromisoformat(row["sent_at"]).astimezone(GROUP_TIMEZONE)
        except (TypeError, ValueError):
            pass
        if sent_local is not None:
            hour = sent_local.hour
            hours[hour] += 1
            if hour < 6:
                periods["凌晨"] += 1
            elif hour < 12:
                periods["上午"] += 1
            elif hour < 18:
                periods["下午"] += 1
            else:
                periods["晚上"] += 1

        try:
            segments = json.loads(row["message_json"])
        except (json.JSONDecodeError, TypeError):
            segments = []
        entries.append(
            {
                "user_id": user_id,
                "name": safe_member_name(user_id, name, group_id),
                # "text" drives 复读 detection (pure text only); "plain" is the
                # rendered form used for the model digest and the fallback list.
                "text": message_text(segments),
                "plain": " ".join(str(row["plain_text"] or "").split()),
                "time": sent_local.strftime("%H:%M") if sent_local else "--:--",
            }
        )
        for segment in segments if isinstance(segments, list) else []:
            if not isinstance(segment, dict) or segment.get("type") != "at":
                continue
            target_id = str(segment.get("data", {}).get("qq", ""))
            if target_id and target_id.casefold() != "all" and target_id != own_id:
                mentioned[target_id] += 1

    _member_names.setdefault(group_id, {}).update(speaker_names)
    busiest = max(hours, key=hours.get) if hours else None
    return {
        "total": len(rows),
        "hours": hours,
        "busiest": busiest,
        "period_text": "、".join(
            f"{label} {periods[label]} 条"
            for label in ("凌晨", "上午", "下午", "晚上")
            if periods[label]
        ),
        "top_speakers": [
            (safe_member_name(uid, speaker_names.get(uid, ""), group_id), count)
            for uid, count in speakers.most_common(5)
        ],
        "top_mentions": [
            (safe_member_name(uid, group_id=group_id), count)
            for uid, count in mentioned.most_common(5)
        ],
        "entries": entries,
        "per_user": dict(speakers),
    }


def detect_repeat(entries: list[dict[str, Any]], min_count: int) -> dict[str, Any] | None:
    """Longest run of one line repeated back to back by at least two people."""
    best: dict[str, Any] | None = None
    run_key: str | None = None
    run_text = ""
    run_count = 0
    run_senders: set[str] = set()

    def flush() -> None:
        nonlocal best, run_key, run_text, run_count, run_senders
        if (
            run_key is not None
            and run_count >= min_count
            and len(run_senders) >= REPEAT_MIN_SENDERS
        ):
            if best is None or run_count > best["count"]:
                best = {"text": run_text, "count": run_count, "senders": len(run_senders)}
        run_key = None
        run_text = ""
        run_count = 0
        run_senders = set()

    for entry in entries:
        text = entry["text"]
        if not text:
            # A picture or a voice message breaks the chain.
            flush()
            continue
        key = text.casefold()
        if key == run_key:
            run_count += 1
            run_senders.add(entry["user_id"])
        else:
            flush()
            run_key, run_text, run_count, run_senders = key, text, 1, {entry["user_id"]}
    flush()
    return best


def summary_digest(entries: list[dict[str, Any]], budget: int) -> str:
    lines = [
        f"{entry['time']} {entry['name']}：{entry['plain']}"
        for entry in entries
        if entry["plain"]
    ]
    if not lines:
        return ""
    joined = "\n".join(lines)
    if len(joined) <= budget:
        return joined
    # Thin the day out evenly instead of dropping its tail.
    keep = max(1, int(len(lines) * budget / len(joined)))
    step = max(1, len(lines) // keep)
    thinned = "\n".join(lines[::step])
    if len(thinned) > budget:
        thinned = thinned[:budget].rsplit("\n", 1)[0]
    return thinned


def local_highlights(entries: list[dict[str, Any]]) -> str:
    candidates = [entry for entry in entries if len(entry["plain"]) >= 8]
    candidates.sort(key=lambda entry: len(entry["plain"]), reverse=True)
    picked = candidates[:3]
    if not picked:
        return ""
    lines = ["可莉的记事本今天有点糊，只好挑几个名场面给你看："]
    lines += [f"· {entry['name']}：「{entry['plain'][:40]}」" for entry in picked]
    return "\n".join(lines)


def pick_greeting(nickname: str, my_count: int) -> str:
    name = " ".join(str(nickname or "").split())[:16] or "这位冒险家"
    opener = random.choice(GREETING_TEMPLATES).format(name=name)
    if my_count >= 20:
        tail = f"可莉数了一下，{name}昨天说了 {my_count} 句话，好厉害呀！"
    elif my_count > 0:
        tail = f"（可莉看到{name}昨天也来说了 {my_count} 句呢。）"
    else:
        tail = f"不过可莉昨天好像没怎么见到{name}呀…"
    return f"{opener}\n{tail}"


def render_daily_report(
    date_text: str, facts: dict[str, Any], repeat: dict[str, Any] | None, body: str
) -> str:
    lines = [f"可莉的昨日巡逻报告（{date_text}）"]
    summary = [f"一共记下 {facts['total']} 条消息。"]
    if facts["busiest"] is not None:
        busiest = facts["busiest"]
        tail = f"；{facts['period_text']}" if facts["period_text"] else ""
        summary.append(
            f"最热闹的是 {busiest:02d}:00–{busiest:02d}:59"
            f"（{facts['hours'][busiest]} 条）{tail}。"
        )
    if facts["top_speakers"]:
        summary.append(
            "最活跃的："
            + "、".join(f"{name}（{count} 条）" for name, count in facts["top_speakers"])
            + "。"
        )
    if facts["top_mentions"]:
        summary.append(
            "被点名最多的："
            + "、".join(f"{name}（{count} 次）" for name, count in facts["top_mentions"])
            + "。"
        )
    if repeat:
        summary.append(
            f"复读现场：「{repeat['text'][:30]}」被连着刷了 {repeat['count']} 次，"
            f"{repeat['senders']} 个人参与！"
        )
    lines.append("".join(summary[:2]))
    lines.extend(summary[2:])
    if body:
        lines.append(body)
    return "\n".join(lines)


async def claim_summary_slot(group_id: str, day: str, covered_day: str) -> str:
    """Return 'generate', 'closing' or 'silent' for one group and one day."""
    now = datetime.now(timezone.utc)
    stale_before = (now - timedelta(minutes=5)).isoformat()
    async with connect() as db:
        cursor = await db.execute(
            """INSERT OR IGNORE INTO summary_runs
               (group_id, day, covered_day, state, created_at)
               VALUES (?, ?, ?, 'generating', ?)""",
            (group_id, day, covered_day, now.isoformat()),
        )
        await db.commit()
        if cursor.rowcount == 1:
            return "generate"

        # The report exists: the next asker gets the "already answered" line once.
        cursor = await db.execute(
            """UPDATE summary_runs SET state='closed'
               WHERE group_id=? AND day=? AND state='answered'""",
            (group_id, day),
        )
        await db.commit()
        if cursor.rowcount == 1:
            return "closing"

        # Reclaim a generation that died midway instead of losing the whole day.
        cursor = await db.execute(
            """UPDATE summary_runs SET created_at=?
               WHERE group_id=? AND day=? AND state='generating' AND created_at<?""",
            (now.isoformat(), group_id, day, stale_before),
        )
        await db.commit()
        if cursor.rowcount == 1:
            logger.warning("Reclaiming a stalled daily summary for group {}", group_id)
            return "generate"
    return "silent"


async def store_summary_result(
    group_id: str,
    day: str,
    state: str,
    summary_text: str | None = None,
    closing_text: str | None = None,
) -> None:
    fields = ["state=?"]
    values: list[Any] = [state]
    if summary_text is not None:
        fields.append("summary_text=?")
        values.append(summary_text)
    if closing_text is not None:
        fields.append("closing_text=?")
        values.append(closing_text)
    values.extend([group_id, day])
    async with connect() as db:
        await db.execute(
            f"UPDATE summary_runs SET {', '.join(fields)} WHERE group_id=? AND day=?",
            tuple(values),
        )
        await db.commit()


async def send_yesterday_summary(bot: Bot, event: GroupMessageEvent) -> None:
    """One report per group per day, then one closing line, then silence."""
    group_id = int(event.group_id)
    local_now = datetime.now(GROUP_TIMEZONE)
    today = local_now.date()
    covered = today - timedelta(days=1)
    date_text = covered.strftime("%Y年%m月%d日")
    nickname = str(event.sender.card or event.sender.nickname or event.user_id)

    slot = await claim_summary_slot(str(group_id), today.isoformat(), covered.isoformat())
    if slot == "silent":
        return

    if slot == "closing":
        closing = await closing_line(nickname, date_text) if SUMMARY_USE_LLM else None
        if not closing:
            closing = f"{nickname}，可莉今天已经写过这份报告啦，往上翻翻就能看到哦～"
        await store_summary_result(
            str(group_id), today.isoformat(), "closed", closing_text=closing
        )
        await bot.send_group_msg(group_id=group_id, message=cap_text(closing, 200))
        return

    try:
        rows = await rows_for_day(group_id, covered)
        facts = day_facts(rows, str(bot.self_id), group_id)
        repeat = detect_repeat(facts["entries"], REPEAT_MIN_COUNT)

        if not rows:
            body = (
                "可莉把昨天翻了个遍，一条消息都没记到…大家是不是都去休息了呀？"
                "今天要一起热闹起来哦！"
            )
        elif SUMMARY_USE_LLM:
            digest = summary_digest(facts["entries"], SUMMARY_DIGEST_CHARS)
            body = await report_body(date_text, facts, digest, repeat)
            if not body:
                logger.warning("Daily summary fell back to local highlights")
                body = local_highlights(facts["entries"])
        else:
            body = local_highlights(facts["entries"])

        greeting = pick_greeting(nickname, facts["per_user"].get(str(event.user_id), 0))
        report = cap_text(
            greeting + "\n" + render_daily_report(date_text, facts, repeat, body),
            SUMMARY_MAX_CHARS,
        )
    except Exception:
        logger.exception("Failed to build the daily summary")
        await store_summary_result(str(group_id), today.isoformat(), "answered", summary_text="")
        return

    await store_summary_result(str(group_id), today.isoformat(), "answered", summary_text=report)
    await bot.send_group_msg(group_id=group_id, message=report)
