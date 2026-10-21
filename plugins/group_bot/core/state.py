"""Mutable runtime state shared across the package.

Keeping these here (instead of scattered across feature modules) is what lets
``media``, ``summary``, ``recall`` and ``handlers`` all agree on the same view
without importing each other in a cycle.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

# Module import time, used as a fallback "process started" clock for status.
_process_started_at = time.monotonic()

# Wall-clock time (epoch seconds) of the current OneBot connection; 0 = never.
_bot_connected_at: float = 0.0


def mark_bot_connected() -> None:
    global _bot_connected_at
    _bot_connected_at = time.time()

# group_id -> the in-flight avatar warm-up task for that group
_avatar_warm_tasks: dict[int, asyncio.Task[None]] = {}

# group_id -> lock serialising recall commands for that group
_recall_locks: dict[str, asyncio.Lock] = {}

# group_id -> {user_id: display name}, filled from group member lists
_member_names: dict[int, dict[str, str]] = {}

# group_id -> (monotonic timestamp, essence list) as returned by OneBot
_essence_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}

# group_id -> lock so a burst of draws triggers a single API call
_essence_locks: dict[int, asyncio.Lock] = {}

# group_id -> essence message ids whose pictures could not be fetched, so they
# are skipped instead of being drawn with a broken image
_essence_broken: dict[int, set[str]] = {}

# 群相册：结构与精华池平行，供漂流瓶的第二个来源使用。
# group_id -> (monotonic timestamp, album photo list)
_album_cache: dict[int, tuple[float, list[Any]]] = {}

# group_id -> lock so a burst of draws walks the albums once
_album_locks: dict[int, asyncio.Lock] = {}

# group_id -> media ids whose picture could not be fetched
_album_broken: dict[int, set[str]] = {}

