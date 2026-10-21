"""处理器兜底：handler 抛异常时只记日志 + 回一句人话，绝不让 matcher 炸掉。

`handlers.py` 里那 6 个 ``safe_send_*`` 原本各自抄了一段：

    try:
        await do_the_thing()
    except Exception:
        logger.exception("... failed")
        try:
            await bot.send_group_msg(group_id=..., message="...")
        except Exception:
            logger.exception("Could not send ... notice")

三行结构完全相同，只有"做什么/说什么/发哪里"不同。这里把它收成一个 `guard()`，
调用点只剩"做什么 + 失败说什么"，文案仍然留在调用处（review 时一眼能看到）。

刻意**不做**的事：不吞 `BaseException`（`CancelledError` 必须继续往上冒，否则停机时
协程取消会被当作"发送失败"再发一条消息）。
"""

from __future__ import annotations

import inspect
from typing import Any, Awaitable, Callable

from nonebot import logger

Action = Callable[[], Awaitable[Any]]
Fallback = Callable[[], Awaitable[Any]]


async def guard(what: str, action: Action, *, on_error: Fallback | None = None) -> Any:
    """执行 ``action``；抛异常就记日志、执行 ``on_error``，并返回 ``None``。

    返回值照原样透传（`safe_send_recall_history` 需要把"送了几条"传回去）。
    """
    try:
        return await action()
    except Exception:
        logger.exception("{} failed", what)
        if on_error is not None:
            await _run_fallback(what, on_error)
        return None


async def _run_fallback(what: str, on_error: Fallback) -> None:
    """兜底动作本身也可能失败（比如群里已被禁言），那时只记日志。"""
    try:
        result = on_error()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("{} failure notice failed", what)


async def guard_group(
    what: str,
    bot: Any,
    group_id: int,
    action: Action,
    text: str,
) -> Any:
    """:func:`guard` 的常用形态：失败时往群里补一句 ``text``。"""
    return await guard(
        what,
        action,
        on_error=lambda: bot.send_group_msg(group_id=group_id, message=text),
    )
