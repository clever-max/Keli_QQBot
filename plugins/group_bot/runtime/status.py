"""Operator status report (``@可莉 status``).

Everything here is local: process metrics, the SQLite archive, recall/essence
bookkeeping.  No model calls, no network.  The command is restricted to the QQ
numbers in ``STATUS_USERS`` (default: the owner) and is deliberately absent from
the public command list.
"""

from __future__ import annotations

import asyncio
import ctypes
import shutil
import struct
import sys
import time
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from ..core import state as bot_state
from ..core.config import (
    ALLOWED_GROUP_IDS,
    CARD_IMAGE_DIR,
    DB_PATH,
    ESSENCE_CACHE_SECONDS,
    ESSENCE_COOLDOWN_SECONDS,
    ESSENCE_DAILY_LIMIT,
    GROUP_TIMEZONE,
    MEDIA_CACHE_DIR,
    RECALL_WINDOW_MINUTES,
    STATUS_EXCLUDED_GROUP_IDS,
    SUMMARY_USE_LLM,
)
from ..common.safety import guard_group
from ..core.store import connect

FILETIME_UNIX_EPOCH = 11644473600  # seconds between 1601-01-01 and 1970-01-01


def status_enabled_for(group_id: int | str) -> bool:
    """该群是否参与 status：被 ``STATUS_EXCLUDED_GROUP_IDS`` 点名的群不参与。"""
    try:
        return int(group_id) not in STATUS_EXCLUDED_GROUP_IDS
    except (TypeError, ValueError):
        return True


# --- formatting helpers ----------------------------------------------------


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def human_size(num_bytes: float) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def local_stamp(epoch: float, pattern: str = "%m-%d %H:%M:%S") -> str:
    try:
        return datetime.fromtimestamp(epoch, GROUP_TIMEZONE).strftime(pattern)
    except (OSError, OverflowError, ValueError):
        return "?"


# --- process metrics (Windows, best effort) --------------------------------


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("PageFaultCount", ctypes.c_uint32),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


def process_metrics() -> dict[str, Any]:
    """Working set and physical memory, or None values when unavailable."""
    metrics: dict[str, Any] = {"working_set": None, "total_ram": None, "available_ram": None}
    try:
        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            metrics["working_set"] = counters.WorkingSetSize
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            metrics["total_ram"] = status.ullTotalPhys
            metrics["available_ram"] = status.ullAvailPhys
    except (OSError, AttributeError, ValueError):
        pass
    return metrics


def process_start_epoch() -> float:
    """OS-reported start time of this process, falling back to import time."""
    try:
        creation, exit_time, kernel, user = (_FILETIME() for _ in range(4))
        kernel32 = ctypes.WinDLL("kernel32")
        if kernel32.GetProcessTimes(
            kernel32.GetCurrentProcess(),
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return ticks / 10_000_000 - FILETIME_UNIX_EPOCH
    except (OSError, AttributeError, ValueError):
        pass
    return time.time() - (time.monotonic() - bot_state._process_started_at)


def directory_usage(folder: Path) -> tuple[int, int]:
    """(file count, total bytes) below ``folder``, ignoring errors."""
    count = 0
    total = 0
    try:
        for path in folder.rglob("*"):
            if path.is_file():
                count += 1
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return count, total


# --- data collection -------------------------------------------------------


async def collect() -> dict[str, Any]:
    local_today = datetime.now(GROUP_TIMEZONE).date()
    day_start = (
        datetime(local_today.year, local_today.month, local_today.day, tzinfo=GROUP_TIMEZONE)
        .astimezone(timezone.utc)
        .isoformat()
    )
    data: dict[str, Any] = {"day": local_today.isoformat()}
    # 被 ``STATUS_EXCLUDED_GROUP_IDS`` 点名的群完全不计入报告：所有按群聚合的查询
    # 都拼上这个过滤片，避免某个群的数据从"其他群"汇总里漏出来。
    excluded = sorted(STATUS_EXCLUDED_GROUP_IDS)
    scope = ""
    scope_params: list[str] = []
    if excluded:
        scope = " AND group_id NOT IN (" + ",".join("?" * len(excluded)) + ")"
        scope_params = [str(group) for group in excluded]
    async with connect() as db:
        cursor = await db.execute(
            """SELECT COUNT(*) AS total, COUNT(DISTINCT user_id) AS senders, MAX(sent_at) AS last
               FROM messages WHERE 1=1""" + scope,
            scope_params,
        )
        row = await cursor.fetchone()
        data["messages_total"] = int(row["total"] or 0)
        data["senders"] = int(row["senders"] or 0)
        data["last_message_at"] = row["last"]

        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE sent_at>=?" + scope,
            [day_start, *scope_params],
        )
        data["messages_today"] = int((await cursor.fetchone())["n"] or 0)

        cursor = await db.execute(
            "SELECT group_id, COUNT(*) AS n FROM messages WHERE sent_at>=?" + scope + " GROUP BY group_id",
            [day_start, *scope_params],
        )
        data["today_by_group"] = {str(r["group_id"]): int(r["n"]) for r in await cursor.fetchall()}

        cursor = await db.execute(
            "SELECT group_id, COUNT(*) AS n FROM recall_events WHERE summoned_at IS NULL"
            + scope + " GROUP BY group_id",
            scope_params,
        )
        data["recall_pending"] = {str(r["group_id"]): int(r["n"]) for r in await cursor.fetchall()}

        cursor = await db.execute(
            "SELECT group_id, COUNT(*) AS n FROM recall_events WHERE recalled_at>=?"
            + scope + " GROUP BY group_id",
            [(datetime.now(timezone.utc) - timedelta(minutes=RECALL_WINDOW_MINUTES)).isoformat(), *scope_params],
        )
        data["recall_in_window"] = {str(r["group_id"]): int(r["n"]) for r in await cursor.fetchall()}

        cursor = await db.execute(
            "SELECT group_id, state, summary_text FROM summary_runs WHERE day=?" + scope,
            [local_today.isoformat(), *scope_params],
        )
        data["summary"] = {
            str(r["group_id"]): (str(r["state"]), len(str(r["summary_text"] or "")))
            for r in await cursor.fetchall()
        }

        cursor = await db.execute(
            "SELECT group_id, COUNT(*) AS draws FROM essence_draws WHERE day=?" + scope + " GROUP BY group_id",
            [local_today.isoformat(), *scope_params],
        )
        data["essence_draws"] = {
            str(r["group_id"]): int(r["draws"]) for r in await cursor.fetchall()
        }

        cursor = await db.execute(
            "SELECT COUNT(*) AS n FROM essence_usage WHERE day=?" + scope,
            [local_today.isoformat(), *scope_params],
        )
        data["essence_users_today"] = int((await cursor.fetchone())["n"] or 0)

    data["db_size"] = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    wal = DB_PATH.with_name(DB_PATH.name + "-wal")
    data["wal_size"] = wal.stat().st_size if wal.exists() else 0
    data["media_files"], data["media_bytes"] = directory_usage(MEDIA_CACHE_DIR)
    data["card_files"], _ = directory_usage(CARD_IMAGE_DIR)
    try:
        usage = shutil.disk_usage(str(DB_PATH.parent))
        data["disk_free"], data["disk_total"] = usage.free, usage.total
    except OSError:
        data["disk_free"] = data["disk_total"] = None
    return data


def _split_groups(per_group: dict[str, Any], current: str) -> tuple[Any, list[Any]]:
    """Split a ``{group_id: value}`` mapping into (本群的值, 其他群的值列表).

    The report goes out as a chat message, so it never prints a group number:
    other groups are only ever reported as counts.
    """
    own = per_group.get(current, 0)
    others = [value for group, value in per_group.items() if group != current]
    return own, others


def _summed(own: int, others: list[int]) -> str:
    """``本群 N``，其他群有量时再补一句汇总。"""
    text = f"本群 {own}"
    other_total = sum(others)
    if other_total:
        text += f"、其他群 {other_total}"
    return text


def render_status(
    data: dict[str, Any], *, self_id: str, uptime: float, current_group_id: int
) -> str:
    metrics = process_metrics()
    groups = sorted(ALLOWED_GROUP_IDS - STATUS_EXCLUDED_GROUP_IDS)
    current = str(current_group_id)
    lines: list[str] = ["可莉的小本本体检报告（仅主人可见）", ""]

    lines.append("【运行】")
    lines.append(f"· 进程启动 {local_stamp(process_start_epoch())}（已运行 {human_duration(uptime)}）")
    if metrics["working_set"] is not None:
        memory = f"· 内存 {human_size(metrics['working_set'])}"
        if metrics["total_ram"] and metrics["available_ram"] is not None:
            memory += (
                f" / 系统可用 {human_size(metrics['available_ram'])}"
                f"（共 {human_size(metrics['total_ram'])}）"
            )
        lines.append(memory)
    try:
        nonebot_version = metadata.version("nonebot2")
    except Exception:  # noqa: BLE001 - metadata is best effort
        nonebot_version = "?"
    bits = struct.calcsize("P") * 8
    lines.append(
        f"· Python {sys.version.split()[0]}（{bits} 位）· NoneBot {nonebot_version} · {sys.platform}"
    )
    connected = f"，本次连接 {local_stamp(bot_state._bot_connected_at)}" if bot_state._bot_connected_at else ""
    lines.append(f"· OneBot 已连接：{self_id}{connected}")
    lines.append("")

    lines.append("【归档】")
    today_line = (
        f"· 总消息 {data['messages_total']} 条 / {data['senders']} 人；"
        f"今天 {data['messages_today']} 条"
    )
    if groups:
        own_today, other_today = _split_groups(
            {str(key): value for key, value in data["today_by_group"].items()}, current
        )
        today_line += f"（{_summed(int(own_today), [int(value) for value in other_today])}）"
    lines.append(today_line)
    if data["last_message_at"]:
        try:
            last = datetime.fromisoformat(str(data["last_message_at"])).timestamp()
            lines.append(f"· 最近一条 {local_stamp(last)}")
        except ValueError:
            pass
    lines.append(
        f"· 数据库 {human_size(data['db_size'])}"
        + (f" + WAL {human_size(data['wal_size'])}" if data["wal_size"] else "")
        + f"｜图片缓存 {data['media_files']} 个 / {human_size(data['media_bytes'])}"
    )
    lines.append("")

    lines.append("【撤回】")
    if data["recall_pending"]:
        own, others = _split_groups(data["recall_pending"], current)
        lines.append(f"· 未召回：{_summed(int(own), [int(v) for v in others])} 条")
    else:
        lines.append("· 未召回：无")
    window_line = f"· 默认只看最近 {RECALL_WINDOW_MINUTES} 分钟"
    if any(data["recall_in_window"].values()):
        own, others = _split_groups(data["recall_in_window"], current)
        window_line += f"（这段时间里 {_summed(int(own), [int(v) for v in others])} 条）"
    else:
        window_line += "（这段时间里没有新撤回）"
    lines.append(window_line)
    lines.append("")

    lines.append("【漂流瓶】")
    own_cached = bot_state._essence_cache.get(current_group_id)
    if own_cached:
        lines.append(
            f"· 本群精华缓存 {len(own_cached[1])} 条"
            f"（{human_duration(time.monotonic() - own_cached[0])}前刷新）"
        )
    else:
        lines.append("· 本群：还没拉取过精华列表")
    own_album = bot_state._album_cache.get(current_group_id)
    if own_album:
        lines.append(
            f"· 本群相册缓存 {len(own_album[1])} 张照片"
            f"（{human_duration(time.monotonic() - own_album[0])}前刷新）"
        )
    else:
        lines.append("· 本群：还没拉取过群相册")
    others = [group for group in groups if str(group) != current]
    if others:
        cached_others = sum(1 for group in others if bot_state._essence_cache.get(group))
        albums_others = sum(1 for group in others if bot_state._album_cache.get(group))
        lines.append(
            f"· 其他群：{cached_others}/{len(others)} 个群已缓存精华列表，"
            f"{albums_others}/{len(others)} 个群已缓存群相册"
        )
    own_drawn, other_drawn = _split_groups(data["essence_draws"], current)
    lines.append(
        f"· 今天抽走：{_summed(int(own_drawn), [int(v) for v in other_drawn])} 次"
        if (own_drawn or any(other_drawn))
        else "· 今天抽走：还没有人抽"
    )
    broken = sum(len(ids) for ids in bot_state._essence_broken.values())
    lines.append(
        f"· 冷却 {ESSENCE_COOLDOWN_SECONDS} 秒 / 每人每天 {ESSENCE_DAILY_LIMIT} 次"
        f"｜列表缓存 {ESSENCE_CACHE_SECONDS} 秒"
        + (f"｜已跳过坏图 {broken} 条" if broken else "")
        + f"｜今日参与 {data['essence_users_today']} 人"
    )
    lines.append("")

    lines.append("【昨日总结】")
    own_state = data["summary"].get(current)
    if not own_state:
        lines.append("· 本群：今天还没人问")
    else:
        kind, length = own_state
        label = {
            "answered": "已回答",
            "closed": "已回答（配额已用）",
            "generating": "正在生成",
        }.get(kind, kind)
        lines.append(f"· 本群：{label}（报告 {length} 字）")
    if others:
        asked_others = sum(
            1 for group in others if data["summary"].get(str(group))
        )
        lines.append(f"· 其他群：{asked_others}/{len(others)} 个群今天问过")
    lines.append(f"· 模型开关 {'开' if SUMMARY_USE_LLM else '关'}（只有昨日总结会调用）")
    lines.append("")

    lines.append("【路径】")
    lines.append(f"· 数据库 {DB_PATH}")
    lines.append(f"· 卡片临时文件 {data['card_files']} 个（{CARD_IMAGE_DIR}）")
    if data["disk_free"] is not None:
        lines.append(f"· 数据盘可用 {human_size(data['disk_free'])} / {human_size(data['disk_total'])}")
    return "\n".join(lines)


async def send_status(bot: Bot, event: GroupMessageEvent) -> None:
    data = await collect()
    started = process_start_epoch()
    # 报告要画一张 900 多像素高的卡片，渲染放线程里，别占住事件循环。
    report = await asyncio.to_thread(
        render_status,
        data,
        self_id=str(getattr(bot, "self_id", "?")),
        uptime=max(0.0, time.time() - started),
        current_group_id=event.group_id,
    )
    # 体检报告里有各群的运行数据，只回私聊；群里保持完全静默。
    await bot.send_private_msg(
        user_id=event.user_id, group_id=event.group_id, message=report
    )


async def safe_send_status(bot: Bot, event: GroupMessageEvent) -> None:
    # 报告走私聊；私聊没送到时回群里说一声，否则主人完全不知道报告失败了。
    await guard_group(
        "Status report",
        bot,
        int(event.group_id),
        lambda: send_status(bot, event),
        "可莉量体温的时候摔了一跤，等会儿再试吧；错误已经记进日志啦。",
    )
