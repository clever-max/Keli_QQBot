"""读群名：优先用事件里带的，其次问 OneBot，最后留空由调用方兜"本群"。

这段逻辑原来在 `bottle_flow`（两处）与 `recall`（一处）各抄了一份，三份一模一样：
事件里没有群名时调 `get_group_info`，取不到就空串，名字是纯数字也当没取到（QQ 有时把
群号当名字返回，直接印在卡片上很难看）。

抽出来后只在这里维护一次；调用方一句 ``await group_name_for(...)`` 即可。
"""

from __future__ import annotations

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot


async def group_name_for(bot: Bot, group_id: int, hint: str = "") -> str:
    """返回可读的群名；拿不到就返回空串（调用方自己回退成"本群"）。"""
    name = str(hint or "").strip()
    if not name:
        try:
            info = await bot.call_api("get_group_info", group_id=group_id)
            payload = info.get("data", info) if isinstance(info, dict) else {}
            name = str(payload.get("group_name", "") or "").strip()
        except Exception:
            # 拿不到群名不是错误：卡片上会显示"本群"，但原因要留在日志里。
            logger.debug("Could not read the group name for {}", group_id, exc_info=True)
            name = ""
    # QQ 有时把群号当群名返回；纯数字的名字印在卡片上没有意义，当作没取到。
    return "" if name.isdigit() else name
