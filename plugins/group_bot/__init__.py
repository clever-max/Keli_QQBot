"""QQ group bot plugin.

This package is the module NoneBot loads; ``plugins/group_bot.py`` was split
into the packages below.  Import order matters:

1. ``core``    — settings, shared state, SQLite, runtime switches (no business deps),
2. ``common``  — small shared helpers (text render, fonts, fallback, group name),
3. ``media``   — render cache + permanent archive, album, voice,
4. ``common.card`` — card drawing (needs the media pipeline),
5. ``bottle``  — the four bottle layers (pool -> stats -> render -> flow),
6. ``recall``  — message ingestion and recall history,
7. ``runtime`` — cooldown, daily summary, LLM, status report,
8. ``handlers`` last, so every matcher and the ``on_bot_connect`` hook are
   registered exactly once when NoneBot imports this package.
"""

from __future__ import annotations

from .core import config as config
from .core import state as state
from .core import store as store
from .core import settings as settings
from .common import textimg as textimg
from .common import render as render
from .common import safety as safety
from .common import bot_info as bot_info
from .media import pipeline as media
from .media import album as album
from .media import archive as archive
from .media import voice as voice
from .media import voice_audio as voice_audio
from .common import card as card
from .bottle import essence as essence
from .recall import ingest as ingest
from .recall import history as recall
from .runtime import cooldown as cooldown
from .runtime import summary as summary
from .runtime import llm as llm
from .runtime import status as status
from .runtime import water as water
from . import handlers as handlers

__all__ = [
    "config", "state", "store", "settings", "textimg", "render", "safety",
    "bot_info", "media", "album", "archive", "voice", "voice_audio", "card",
    "essence", "ingest", "recall", "cooldown", "summary", "llm", "status",
    "water",
    "handlers",
]
