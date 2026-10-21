"""喝水提醒：按固定作息，在**私聊**与**群**里各发一条。

作息表（Asia/Shanghai，2026-10-18 由主人指定）：

* 07:50 起床（单独文案）
* 08:30 / 09:05 / 10:00 / 10:30 / 10:50 上午喝水
* 12:40 午餐后
* 13:50 午休起来
* 14:00 起每 45 分钟一节（14:00 / 14:45 / 15:30 / 16:15 / 17:00 / 17:45），到 18:00 下课
* 19:00 晚餐后
* 之后每小时一次到 22:30（20:00 / 21:00 / 22:00 / 22:30）

实现取舍：

* **不引入 apscheduler**：项目依赖刻意保持极简，这里用一个 20 秒 tick 的 asyncio 循环，
  按 `HH:MM` 匹配；同一分钟只触发一次（`_last_fired` 去重），跨天自动重置。
* **开关持久化在 `bot_settings`**（`settings` 模块），重启后仍然有效；默认开启。
* 主人可用私聊 `喝水提醒 开|关|状态` 控制；非主人只提示"仅主人可改"。
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from ..core.config import (
    ALLOWED_GROUP_IDS,
    AUTHORIZED_USERS,
    GROUP_TIMEZONE,
    STATUS_USERS,
    WATER_EXCLUDED_GROUP_IDS,
)
from ..core.settings import is_switch_on, set_switch

WATER_SWITCH = "water_reminder"
WAKE = "wake"
MORNING = "morning"
LUNCH = "lunch"
NAP = "nap"
CLASS = "class"
DINNER = "dinner"
HOURLY = "hourly"
LAST = "last"

# (HH:MM, 类型)，已按时间排序；同一天内不重复。
SCHEDULE: tuple[tuple[str, str], ...] = (
    ("07:50", WAKE),
    ("08:30", MORNING),
    ("09:05", MORNING),
    ("10:00", MORNING),
    ("10:30", MORNING),
    ("10:50", MORNING),
    ("12:40", LUNCH),
    ("13:50", NAP),
    ("14:00", CLASS),
    ("14:45", CLASS),
    ("15:30", CLASS),
    ("16:15", CLASS),
    ("17:00", CLASS),
    ("17:45", CLASS),
    ("19:00", DINNER),
    ("20:00", HOURLY),
    ("21:00", HOURLY),
    ("22:00", HOURLY),
    ("22:30", LAST),
)

TEXTS: dict[str, tuple[str, ...]] = {
    WAKE: (
        "七点五十啦，该起床了！可莉已经把窗帘拉开了一条缝，再睡就要迟到咯～",
        "起床时间到～先坐起来喝一小杯温水，今天的可莉也在等你哦。",
    ),
    MORNING: (
        "喝口水吧！杯子就在手边，可莉帮你数三下：一、二、三——喝完再走～",
        "上午的加水时刻到啦，喝两口再继续，脑子会清醒很多哦。",
        "可莉提醒：水杯该空了。去接一杯，顺便站起来伸个懒腰～",
    ),
    LUNCH: (
        "午饭吃完啦，喝杯水再休息，别马上躺下哦～",
        "午饭后一小杯水，可莉帮你记着这一杯。",
    ),
    NAP: (
        "午休起来了！先喝口水醒醒神，下午才有力气～",
        "睡醒啦，喝水，洗脸，然后上课去。可莉已经准备好笔记了。",
    ),
    CLASS: (
        "这一节该喝水啦——趁课间把杯子喝掉一半，下一节才不困。",
        "休息一下，喝水！可莉帮你看着时间，还有下一节呢。",
        "课间加水：喝几口，站起来走两步，别一直坐着。",
    ),
    DINNER: (
        "晚饭吃完啦，喝水，然后去散步消食～",
        "晚餐后的这一杯别忘了，可莉在看着呢。",
    ),
    HOURLY: (
        "整点喝水时间！一杯水，一分钟，谁都能做到～",
        "可莉的加水提醒：现在就喝一口，别等渴了才想起来。",
        "喝口水，眨眨眼，看看远处。这样眼睛和人都舒服一点。",
    ),
    LAST: (
        "今天最后一杯水啦，喝完就准备休息吧，明天见～",
        "十点半了，喝完这杯就不要再多喝了，早点睡哦。",
    ),
}

# 同一分钟只提醒一次。
_last_fired: str | None = None
_scheduler_started = False


def kind_at(hhmm: str) -> str | None:
    """``"HH:MM"`` 对应的提醒类型；不在表里返回 None。"""
    for time_text, kind in SCHEDULE:
        if time_text == hhmm:
            return kind
    return None


def text_for(kind: str) -> str:
    return random.choice(TEXTS[kind])


async def water_enabled() -> bool:
    return await is_switch_on(WATER_SWITCH, default=True)


async def set_water(enabled: bool) -> bool:
    """返回是否真的改变了状态。"""
    return await set_switch(WATER_SWITCH, enabled, default=True)


def owner_user_id() -> int | None:
    """提醒私聊发给谁：优先 STATUS_USERS（主人），其次 AUTHORIZED_USERS。"""
    for group in (STATUS_USERS, AUTHORIZED_USERS):
        for value in group:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def target_groups() -> list[int]:
    """除 ``WATER_EXCLUDED_GROUP_IDS`` 外的所有白名单群。"""
    return sorted(
        int(group)
        for group in ALLOWED_GROUP_IDS
        if int(group) not in WATER_EXCLUDED_GROUP_IDS
    )


async def deliver(bot: Bot, kind: str) -> int:
    """把一条提醒发到私聊与所有允许的群，返回成功的份数。"""
    message = text_for(kind)
    sent = 0
    user_id = owner_user_id()
    if user_id is not None:
        try:
            await bot.send_private_msg(user_id=user_id, message=message)
            sent += 1
        except Exception:
            logger.exception("Water reminder private delivery failed")
    for group_id in target_groups():
        try:
            await bot.send_group_msg(group_id=group_id, message=message)
            sent += 1
        except Exception:
            logger.exception("Water reminder group delivery failed for {}", group_id)
    return sent


async def tick(bot: Bot, now: datetime | None = None) -> bool:
    """处理一次调度检查；真的发了提醒返回 True。"""
    global _last_fired
    current = now or datetime.now(GROUP_TIMEZONE)
    hhmm = current.strftime("%H:%M")
    if hhmm == _last_fired:
        return False
    kind = kind_at(hhmm)
    if kind is None:
        return False
    _last_fired = hhmm
    if not await water_enabled():
        logger.debug("Water reminder skipped at {} (switch off)", hhmm)
        return False
    count = await deliver(bot, kind)
    logger.info("Water reminder sent at {} ({}) to {} targets", hhmm, kind, count)
    return True


TEST_KINDS: tuple[str, ...] = (MORNING, CLASS, HOURLY)


async def send_test(bot: Bot) -> tuple[int, str]:
    """立刻发一条示例提醒（**不受开关影响**，就是用来验证通道的）。"""
    kind = random.choice(TEST_KINDS)
    message = f"【测试】{text_for(kind)}"
    sent = 0
    user_id = owner_user_id()
    if user_id is not None:
        try:
            await bot.send_private_msg(user_id=user_id, message=message)
            sent += 1
        except Exception:
            logger.exception("Water test private delivery failed")
    for group_id in target_groups():
        try:
            await bot.send_group_msg(group_id=group_id, message=message)
            sent += 1
        except Exception:
            logger.exception("Water test group delivery failed for {}", group_id)
    logger.info("Water test reminder sent to {} targets", sent)
    return sent, message


TICK_SECONDS = 20


def start_water_scheduler(bot: Bot) -> None:
    """在 bot 连上后启动提醒循环（只启动一次）。"""

    async def loop() -> None:
        while True:
            try:
                await tick(bot)
            except Exception:
                logger.exception("Water reminder tick failed")
            await asyncio.sleep(TICK_SECONDS)

    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    asyncio.create_task(loop())
    logger.info("Water reminder scheduler started ({} slots/day)", len(SCHEDULE))


def parse_water_command(text: str) -> str | None:
    """解析私聊命令：返回 ``"on"`` / ``"off"`` / ``"status"``，不是该命令返回 None。"""
    tokens = str(text).split()
    if not tokens:
        return None
    if tokens[0] not in {"喝水提醒", "喝水"}:
        return None
    if len(tokens) == 1:
        return "status"
    word = tokens[1].casefold()
    if word in {"开", "开启", "打开", "on", "start"}:
        return "on"
    if word in {"关", "关闭", "关掉", "off", "stop"}:
        return "off"
    if word in {"测试", "试一下", "试试点", "test"}:
        return "test"
    return "status"
