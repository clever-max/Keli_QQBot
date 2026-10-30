"""漂流瓶（原「群精华」）—— **再导出层**。

实现已按职责拆成四个模块（依赖单向，无循环）：

* ``bottle_pool``   池子拉取与缓存、键空间、抽样与防重复、内容提取
* ``bottle_stats``  被抽到次数与被抽到榜的数据层
* ``bottle_render`` 卡片渲染与「榜单键 → 现行内容」回查
* ``bottle_flow``   抽取流程编排与用户可见文案

保留本文件是为了让 ``handlers.py`` 与 12 个行为套件的 **导入路径不变**。
⚠️ 但**打桩（monkeypatch）不行**：``essence.choose_bottle = fake`` 只改了本模块的名字，
实现模块里的调用仍指向真函数。新增/修改测试时请直接给拥有该函数的模块打桩
（例如 ``bottle_pool.choose_bottle``）。

另：``import random`` 与几个 config/media 名字是给测试与历史调用方用的代理，
不是本层自己的逻辑。
"""

from __future__ import annotations

import random  # noqa: F401  套件会 seed/patcher 这个模块对象（全局共享，故仍有效）

from ..core.config import BOTTLE_ALBUM_ENABLED, BOTTLE_ALBUM_SHARE, BOTTLE_SWITCH_STREAK  # noqa: F401
from ..media.album import fetch_album_photos, materialize_photo  # noqa: F401
from ..media.pipeline import cache_avatar  # noqa: F401
from ..core.state import _album_broken, _essence_broken  # noqa: F401
from ..core.config import GROUP_TIMEZONE  # noqa: F401  套件用它换算日期
from ..common.card import render_card_page  # noqa: F401  套件用 gb.render_card_page 做桩

from .pool import (
    EssenceUnavailable,
    ChosenBottle,
    ESSENCE_DRAW_RETENTION_DAYS,
    ESSENCE_IMAGE_ATTEMPTS,
    ESSENCE_IMAGE_SUFFIXES,
    _album_key,
    _essence_key,
    _is_playable_ref,
    _pick_key,
    album_pool_enabled,
    album_pool_size,
    choose_album_photo,
    choose_bottle,
    choose_essence,
    draw_history,
    drawn_message_ids,
    essence_file_parts,
    essence_image_segments,
    essence_pool_size,
    essence_text,
    essence_video_ref,
    fetch_essence_list,
    format_local,
    kind_of_drawn_key,
    local_day_key,
    materialize_images,
    pool_sizes,
    preferred_kind_order,
    preferred_order,
    recent_drawn_kinds,
    record_draw,
    resolve_mentions,
)
from .stats import (
    DrawnContent,
    content_label,
    draw_times,
    draw_times_after,
    drawn_content_leaderboard,
    drawn_content_leaderboard_text,
    drawn_rank_label,
)
from .render import (
    DrawnContentCard,
    LEADERBOARD_IMAGE_BUDGET,
    LEADERBOARD_TEXT_CHARS,
    LEADERBOARD_THUMB_SIZE,
    render_bottle_card,
    render_drawn_leaderboard_card,
    resolve_drawn_content,
)
from .flow import (
    DrawVerdict,
    EssenceUnavailable,
    album_disabled_text,
    album_empty_text,
    album_exhausted_text,
    album_no_showable_text,
    bottle_hint_text,
    claim_cooldown_notice,
    claim_essence_draw,
    cooldown_text,
    daily_limit_text,
    empty_bottle_text,
    leaderboard_text,
    mark_limit_notified,
    no_showable_text,
    pool_exhausted_text,
    refund_essence_draw,
    safe_send_drawn_content_leaderboard,
    safe_send_essence,
    safe_send_essence_leaderboard,
    send_bottle,
    send_bottle_leaderboard,
    send_drawn_content_leaderboard,
)
