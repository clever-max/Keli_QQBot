"""NoneBot entry points: command routing and event handlers."""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from nonebot import logger, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    GroupRecallNoticeEvent,
    Message,
    PrivateMessageEvent,
)
from nonebot.rule import is_type

from .runtime.cooldown import (
    GROUP_RECALL_COOLDOWN_SECONDS,
    take_group_recall_slot,
    take_non_admin_mention_slot,
)
from .core.config import (
    ALLOWED_GROUP_IDS,
    BOTTLE_ONLY_GROUP_IDS,
    BOTTLE_ONLY_HELP_GROUP_IDS,
    ESSENCE_DAILY_LIMIT,
    RECALL_MAX_COUNT,
    RECALL_WINDOW_MINUTES,
    group_allows_everyone,
    is_authorized,
    is_named_admin,
    is_named_in_group,
    is_status_user,
    is_super_admin,
)
from .bottle.essence import (
    safe_send_drawn_content_leaderboard,
    safe_send_essence,
    safe_send_essence_leaderboard,
)
from .bottle.pool import start_bottle_cache_warmer
from .media.archive import start_archive_sweeper
from .recall.ingest import mark_recalled, save_message
from .media.pipeline import avatar_dir, cache_avatar, schedule_avatar_warm
from .media.voice import safe_send_voice
from .recall.history import (
    RecallRequest,
    clear_pending_recall,
    clear_recall_text,
    safe_send_all_recall_history,
    safe_send_recall_history,
)
from .core.settings import (
    RECALL_PRIVATE_DELIVERY,
    is_switch_on,
    set_switch,
)
from .core.state import mark_bot_connected
from .runtime.status import safe_send_status, status_enabled_for
from .core.store import driver
from .runtime.summary import send_yesterday_summary
from .runtime.incremental import send_incremental_summary
from .runtime import water

SUMMARY_COMMANDS = {"昨日消息总结", "昨日总结", "昨日群聊总结"}
# 增量总结：只看"上次总结之后到现在"（首次使用回退 4 小时），见 runtime/incremental.py。
INCREMENTAL_COMMANDS = {"总结", "增量总结", "最近总结"}
HELP_COMMANDS = {"help", "帮助", "命令"}
CLEAR_COMMANDS = {"clear", "清除未召回"}
RECALL_ALL_WORDS = {"全部", "所有", "all"}
# 仅私聊主人可用：查看所有已撤回记录（含已经看过的），不改变未读状态。
RECALL_HISTORY_WORDS = {"历史", "历史记录", "所有记录", "全部记录"}
# 「撤回记录」的投递方式开关：把卡片发到发起人和机器人的一对一对话里
# （非好友走群临时会话），而不是发进群。
RECALL_PRIVATE_WORDS = {"私聊", "私发", "临时会话", "临时对话", "悄悄"}
# 私聊投递这个功能本身的持久开关。开启只能在私聊里做，群里只能关掉，
# 免得群成员把它打开、把撤回记录引到自己的私聊里。
TOGGLE_COMMANDS = {"私聊投递", "私聊开关", "私聊投递开关"}
TOGGLE_ON_WORDS = {"开", "开启", "打开", "on", "enable"}
TOGGLE_OFF_WORDS = {"关", "关闭", "关掉", "off", "disable"}
# 漂流瓶（原「群精华」）。旧名 `精华` / `精华榜` 等已按要求删除，不再是可识别命令，
# 群里打老词会走"未知指令 → 完全静默"。
BOTTLE_COMMANDS = {
    "漂流瓶",
    "捞漂流瓶",
    "捡漂流瓶",
    "捞一个漂流瓶",
    "来一个漂流瓶",
}
BOTTLE_RANK_COMMANDS = {
    "丢漂流瓶榜",
    "漂流瓶榜",
    "捞漂流瓶榜",
}
# 「被抽到榜」= 内容被捞到过几次的 TOP10，返回图片（与"谁丢的瓶子多"是两回事）。
DRAWN_RANK_COMMANDS = {
    "被抽到榜",
    "被捞榜",
    "抽到榜",
}
# 语音点歌：`炸乌云` 放默认那首（让风告诉你），`炸乌云 <歌名>` 点别的。
# 曲库是 data/voice/ 下的文件，加歌不用改代码（见 voice.py）。
VOICE_COMMANDS = {"炸乌云"}
# `漂流瓶 相册` = 只从群相册随机，不参与混合池，也不被连续同类补偿改道。
BOTTLE_ALBUM_WORDS = {"相册", "群相册", "照片", "相片"}


def parse_voice_command(text: str) -> str | None:
    """解析 ``炸乌云 [歌名]``：返回歌名（可能为空串）表示命中，``None`` 表示不是这条命令。

    与 ``漂流瓶 相册`` 的写法保持一致：命令词后面只取一个"歌名"片段（允许带空格），
    不去猜别的语法。
    """
    tokens = text.split(maxsplit=1)
    if not tokens or tokens[0].casefold() not in VOICE_COMMANDS:
        return None
    return tokens[1].strip() if len(tokens) > 1 else ""
# `漂流瓶 相册` = 只从群相册随机，不参与混合池，也不被连续同类补偿改道。
BOTTLE_ALBUM_WORDS = {"相册", "群相册", "照片", "相片"}


def parse_bottle_command(text: str) -> bool | None:
    """Parse ``漂流瓶 [相册]``.

    Returns ``True`` for the album-only form, ``False`` for the mixed pool, and
    ``None`` when the text is not this command at all.  Quota and cooldown are
    shared by both forms (it is still one bottle), so nothing else is parsed.
    """
    tokens = text.split()
    if not tokens:
        return None
    if len(tokens) == 1:
        return False if tokens[0].casefold() in BOTTLE_COMMANDS else None
    if (
        len(tokens) == 2
        and tokens[0].casefold() in BOTTLE_COMMANDS
        and tokens[1].casefold() in BOTTLE_ALBUM_WORDS
    ):
        return True
    return None
STATUS_COMMANDS = {"status", "状态"}


def is_at_bot(message: Message, bot: Bot) -> bool:
    return any(
        segment.type == "at" and str(segment.data.get("qq")) == str(bot.self_id)
        for segment in message
    )


def without_bot_mention(message: Message, bot: Bot) -> str:
    content = Message(
        segment
        for segment in message
        if not (
            segment.type == "at"
            and str(segment.data.get("qq")) == str(bot.self_id)
        )
    )
    return content.extract_plain_text().strip()


@dataclass(frozen=True)
class RecallCommand:
    """One parsed ``撤回记录`` invocation."""

    all_pending: bool
    private: bool
    count: int | None
    group_id: int | None
    history: bool = False


def parse_recall_command(text: str) -> RecallCommand | None:
    """Parse ``撤回记录 [全部] [私聊] [条数] [群号]`` — the parts may be in any order.

    Returns ``None`` when the text is not that command at all.  Permission
    checks happen in the handler, not here.
    """
    tokens = text.split()
    if not tokens or tokens[0] != "撤回记录":
        return None
    all_pending = False
    private = False
    history = False
    count: int | None = None
    group_id: int | None = None
    for token in tokens[1:]:
        if token.casefold() in RECALL_ALL_WORDS:
            all_pending = True
        elif token in RECALL_HISTORY_WORDS:
            history = True
        elif token in RECALL_PRIVATE_WORDS:
            private = True
        elif re.fullmatch(r"\d{1,3}", token):
            # A long number is a group id, so "最近 N 条" stays unambiguous.
            if count is not None:
                return None
            count = int(token)
        elif re.fullmatch(r"\d{5,12}", token):
            if group_id is not None:
                return None
            group_id = int(token)
        else:
            return None
    return RecallCommand(all_pending, private, count, group_id, history)


@dataclass(frozen=True)
class ToggleCommand:
    """One parsed 私聊投递 命令；``wanted`` is None when it only asks for state."""

    wanted: bool | None


def parse_toggle_command(text: str) -> ToggleCommand | None:
    """Parse ``私聊投递 [开|关]``.

    Returns ``None`` when the text is not that command at all.  Who may flip it
    in which direction is decided by the handler, not here.
    """
    tokens = text.split()
    if not tokens or tokens[0] not in TOGGLE_COMMANDS:
        return None
    if len(tokens) == 1:
        return ToggleCommand(None)
    if len(tokens) == 2:
        word = tokens[1].casefold()
        if word in TOGGLE_ON_WORDS:
            return ToggleCommand(True)
        if word in TOGGLE_OFF_WORDS:
            return ToggleCommand(False)
    return None


def is_recognized_command(text: str, folded: str) -> bool:
    """True when the mention carries a command this bot knows.

    Everything else (a bare @, chit-chat, typos, unknown commands) is ignored
    silently: no reply, and no non-admin cooldown slot is spent on it.
    """
    if not text:
        return False
    if folded in INCREMENTAL_COMMANDS:
        return True
    if folded in SUMMARY_COMMANDS or folded in HELP_COMMANDS or folded in CLEAR_COMMANDS:
        return True
    if folded in BOTTLE_COMMANDS or folded in BOTTLE_RANK_COMMANDS:
        return True
    if folded in DRAWN_RANK_COMMANDS:
        return True
    if parse_voice_command(text) is not None:
        return True
    if parse_bottle_command(text) is not None:
        return True
    if parse_toggle_command(text) is not None:
        return True
    return parse_recall_command(text) is not None


def bottle_only_allowed(text: str, folded: str) -> bool:
    """Bottle-only groups: stick to bottle commands + help, silence everything else."""
    return (
        parse_bottle_command(text) is not None
        or folded in BOTTLE_RANK_COMMANDS
        or folded in DRAWN_RANK_COMMANDS
        or folded in HELP_COMMANDS
    )


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"· {line}" for line in lines)


def everyone_commands() -> list[str]:
    return [
        "@可莉 昨日消息总结 —— 看昨天群里的巡逻报告（每个群每天只有一次）",
        f"@可莉 撤回记录 —— 最近 {RECALL_WINDOW_MINUTES} 分钟内被撤回的消息",
        "@可莉 撤回记录 20 —— 上面这些里，只看最近的 20 条",
        "!recalled —— 不用 @ 的快捷方式，等同于「撤回记录」",
        "@可莉 漂流瓶 —— 从群精华和群相册里捞一个漂流瓶（每人每群 1 分钟冷却）",
        "@可莉 漂流瓶 相册 —— 只从群相册里捞一张照片",
        "@可莉 丢漂流瓶榜 —— 看看谁被丢进瓶子最多、谁最爱丢瓶子",
        "@可莉 被抽到榜 —— 被捞得最多的 10 条内容（发图片）",
        "@可莉 炸乌云 —— 放一首歌（默认《让风告诉你》，用语音发送）",
        "@可莉 help —— 再念一遍这份清单",
    ]


def admin_commands() -> list[str]:
    return [
        "@可莉 撤回记录 私聊 —— 同样的记录，改发到你和可莉的私聊里",
        "@可莉 私聊投递 关 —— 把私聊投递关掉（群里只能关，打开要去私聊）",
    ]


def command_help(*, is_admin: bool) -> str:
    """The in-group command list.

    Deliberately stops at 【管理员】: it is answered into the group, and the
    super-admin commands (撤回记录 全部 / clear) are not meant to be advertised
    there.  They stay documented in the private help and in README.
    """
    blocks = ["可莉的指令清单来啦！", "", "【大家都能用】", _bullets(everyone_commands())]
    if is_admin:
        blocks += ["", "【管理员】", _bullets(admin_commands())]
    tips = [
        f"小提示：漂流瓶每人每天 {ESSENCE_DAILY_LIMIT} 次，可莉会把它画成图片发出来；"
        "瓶子里的东西来自群精华和群相册照片。",
        f"撤回记录是大家共用的：一个群每 {GROUP_RECALL_COOLDOWN_SECONDS} 秒只能翻一次，"
        "条数多了会分成好几页。",
    ]
    if is_admin:
        tips.append(
            "加上「私聊」之后，记录只发到你和可莉的一对一对话里，群里只会留一句回执；"
            "在那个对话里直接下同样的命令也可以。"
        )
    blocks += ["", *tips]
    return "\n".join(blocks)


def bottle_help() -> str:
    """只讲漂流瓶的群 help：不提撤回记录、昨日总结和管理员权限。"""
    return "\n".join(
        [
            "可莉的漂流瓶说明来啦！",
            "",
            _bullets(
                [
                    "@可莉 漂流瓶 —— 从群精华和群相册里捞一个漂流瓶",
                    "@可莉 漂流瓶 相册 —— 只从群相册里捞一张照片",
                    "@可莉 丢漂流瓶榜 —— 看看谁最爱丢瓶子、谁被丢得最多",
                    "@可莉 被抽到榜 —— 被捞得最多的 10 条内容（发图片）",
                ]
            ),
            "",
            f"小提示：漂流瓶每人每天 {ESSENCE_DAILY_LIMIT} 次，每人每群 1 分钟冷却；"
            "瓶子里的东西来自群精华和群相册照片，抽到后会画成图片发出来。",
        ]
    )


def group_help(group_id: int, *, is_admin: bool) -> str:
    """群里的 help：默认按身份分块，被点名的群只讲漂流瓶。"""
    if group_id in BOTTLE_ONLY_HELP_GROUP_IDS or group_id in BOTTLE_ONLY_GROUP_IDS:
        return bottle_help()
    return command_help(is_admin=is_admin)


def private_help(*, is_super: bool) -> str:
    """Command list for the 1:1 chat (群临时会话 or a plain private chat)."""
    commands = [
        f"撤回记录 —— 最近 {RECALL_WINDOW_MINUTES} 分钟内被撤回的消息（发在这里）",
        "撤回记录 20 —— 只看最近的 20 条",
        "撤回记录 私聊 —— 在这里写不写「私聊」都一样，反正都发这儿",
        "私聊投递 开 / 关 —— 打开或关掉私聊投递（打开只有主人能做）",
        "私聊投递 —— 看看现在是开着还是关着",
    ]
    if is_super:
        commands += [
            "撤回记录 全部 —— 所有还没看过的撤回消息（不限时间）",
            "撤回记录 全部 50 —— 全部里只看最近的 50 条",
            "撤回记录 历史 —— 所有已经撤回的消息（含看过的，默认全部群）",
            "撤回记录 历史 <群号> —— 只看那个群的全部撤回历史",
            "撤回记录 历史 50 —— 只看最近 50 条",
            "clear —— 清掉还没看过的撤回记录（只有主人能用）",
        ]
    blocks = ["这里可莉只认撤回记录相关的命令哦。", "", "【能用的命令】", _bullets(commands)]
    blocks += [
        "",
        "小提示：如果可莉不知道你想看哪个群，把群号一起写上就行，"
        "比如「撤回记录 <群号> 20」。",
        f"这里不受群里的 {GROUP_RECALL_COOLDOWN_SECONDS} 秒冷却限制，随时能翻。",
    ]
    return "\n".join(blocks)


def recall_private_admin_only_reply() -> str:
    """A non-admin asked for 「私聊」 delivery, which stays an admin tool."""
    return "\n".join(
        [
            "「撤回记录 私聊」是管理员的小工具哦——它会把记录搬到某个人的私聊里。",
            "想看记录的话，不加「私聊」可莉就直接发在群里：",
            _bullets(
                [
                    f"@可莉 撤回记录 —— 最近 {RECALL_WINDOW_MINUTES} 分钟",
                    "@可莉 撤回记录 20 —— 只看最近的 20 条",
                ]
            ),
        ]
    )


def clear_owner_only_reply() -> str:
    return "\n".join(
        [
            "clear 是可莉留给主人的钥匙呀，你还拿不到。",
            "想看撤回记录不用 clear，直接跟可莉说就行：",
            _bullets(
                [
                    f"@可莉 撤回记录 —— 最近 {RECALL_WINDOW_MINUTES} 分钟",
                    "@可莉 撤回记录 20 —— 只看最近的 20 条",
                ]
            ),
        ]
    )


def recall_cooldown_reply(remaining: int) -> str:
    """Sent once per window; later blocked attempts stay silent."""
    return (
        f"这个群刚刚才翻过一遍，还要等 {remaining} 秒才能再翻哦～"
        f"（撤回记录是整个群共用 {GROUP_RECALL_COOLDOWN_SECONDS} 秒一次）"
    )


def recall_history_owner_only_reply() -> str:
    return "「撤回记录 历史」只留给主人哦，普通管理员看不到全部群的完整撤回记录。"


def recall_all_refused_reply() -> str:
    return "\n".join(
        [
            "唔…「全部」是可莉留给超级管理员的钥匙呀，你还拿不到。",
            "",
            f"不过最近 {RECALL_WINDOW_MINUTES} 分钟内被撤回的消息大家都能看：",
            _bullets(
                [
                    f"@可莉 撤回记录 —— 看最近 {RECALL_WINDOW_MINUTES} 分钟",
                    "@可莉 撤回记录 20 —— 只看最近的 20 条",
                ]
            ),
        ]
    )


def recall_private_notice(delivered: int) -> str:
    """One-line receipt left in the group after a private delivery."""
    return f"可莉把 {delivered} 条记录悄悄发到你的私聊里啦，去那里看吧～"


def recall_target_refused_reply() -> str:
    """The named group is unknown, or the asker may not reach across to it."""
    return "\n".join(
        [
            "可莉只认得自己在管的群；想在别的群里翻记录，得在那个群的名单里才行哦。",
            "",
            "不写群号的话，可莉就默认查当前这个群：",
            _bullets(
                [
                    f"@可莉 撤回记录 —— 最近 {RECALL_WINDOW_MINUTES} 分钟",
                    "@可莉 撤回记录 20 —— 只看最近的 20 条",
                ]
            ),
        ]
    )


def recall_unknown_group_reply() -> str:
    """Private chat, and the group the command should read from is unclear.

    Deliberately does not list the allowed groups: this answer is a chat message,
    and group numbers should not end up in one.
    """
    return "\n".join(
        [
            "可莉不知道你想看哪个群的记录呀。",
            "把群号写在命令里就行，比如：",
            _bullets(
                [
                    "撤回记录 <群号> 20 —— 那个群最近 20 条",
                    "撤回记录 <群号> 全部 —— 那个群所有没看过的",
                ]
            ),
        ]
    )


def toggle_state_text(enabled: bool, *, from_group: bool) -> str:
    """The switch's current state, plus where it may be flipped."""
    lines = [f"撤回记录的私聊投递现在是{'开着' if enabled else '关着'}的。"]
    if from_group:
        lines.append("群里只能关掉它；想重新打开，来和可莉的私聊里说「私聊投递 开」。")
    else:
        lines.append("想关掉就发「私聊投递 关」，想打开就发「私聊投递 开」。")
    return "\n".join(lines)


def toggle_off_text(changed: bool) -> str:
    if not changed:
        return "撤回记录的私聊投递本来就是关着的哦。"
    return "\n".join(
        [
            "好哦，可莉把撤回记录的私聊投递关掉啦。",
            "以后「撤回记录 私聊」和被关掉的私聊命令都不会再发东西了。",
            "想重新打开，来和可莉的私聊里说「私聊投递 开」。",
        ]
    )


def toggle_on_text(changed: bool) -> str:
    if not changed:
        return "私聊投递本来就是开着的哦，随时可以用。"
    return "\n".join(
        [
            "私聊投递打开啦！",
            "以后在群里发「撤回记录 私聊」，可莉就把记录发到你的私聊里；"
            "在这边直接发「撤回记录」也可以。",
        ]
    )


def toggle_group_only_off_text() -> str:
    """A group tried to switch it on, which only the owner may do."""
    return "\n".join(
        [
            "私聊投递在群里只能关掉，不能打开哦。",
            "想重新打开，来和可莉的私聊里说「私聊投递 开」。",
        ]
    )


def toggle_owner_only_text() -> str:
    """Someone who is not the owner tried to switch it on from a private chat."""
    return "\n".join(
        [
            "打开私聊投递是可莉留给主人的钥匙呀，你还拿不到。",
            "不过关掉它是可以的：发「私聊投递 关」就行。",
        ]
    )


def private_delivery_disabled_reply() -> str:
    return "\n".join(
        [
            "撤回记录的私聊投递现在是关着的，可莉先不发啦。",
            "想重新打开，来和可莉的私聊里说「私聊投递 开」。",
        ]
    )


def recall_request_for(
    event: GroupMessageEvent, *, group_id: int | None = None, private: bool = False
) -> RecallRequest:
    """Build the delivery target for a command typed in a group."""
    target_group = event.group_id if group_id is None else group_id
    # The event's group name is only trustworthy for the group the command was
    # typed in; a cross-group lookup has to ask OneBot instead.
    group_name = str(getattr(event, "group_name", "") or "")
    return RecallRequest(
        group_id=target_group,
        user_id=event.user_id,
        private=private,
        from_group=True,
        group_name=group_name if target_group == event.group_id else "",
    )


async def guarded_incremental_summary(bot: Bot, event: GroupMessageEvent) -> None:
    """增量总结的兜底：异常只回一句人话。"""
    try:
        await send_incremental_summary(bot, event)
    except Exception:
        logger.exception("Incremental summary failed")
        try:
            await bot.send_group_msg(
                group_id=event.group_id,
                message="可莉翻笔记的时候把纸弄乱了，等会儿再试一次吧～",
            )
        except Exception:
            logger.exception("Could not send incremental summary failure notice")


@driver.on_bot_connect
async def on_bot_connect(bot: Bot) -> None:
    mark_bot_connected()
    schedule_avatar_warm(bot)
    start_archive_sweeper(bot)
    start_bottle_cache_warmer(bot)
    water.start_water_scheduler(bot)


group_messages = on_message(rule=is_type(GroupMessageEvent), priority=10, block=False)
private_messages = on_message(rule=is_type(PrivateMessageEvent), priority=10, block=False)
group_recall = on_notice(priority=10, block=False)
group_member_increase = on_notice(priority=5, block=False)


async def serve_recall_query(
    bot: Bot, event: GroupMessageEvent, command: RecallCommand, *, is_super: bool
) -> None:
    """One 撤回记录 query in a group; shared by the @-command and ``!recalled``.

    Open to every member of the group, with three exceptions: 「私聊」 delivery is
    still an admin tool (it moves the cards into someone's 1:1 chat), 「全部」
    stays with the super admin, and reaching into *another* group needs the asker
    to be named for that group — a ``*`` list means "everyone in this group",
    which says nothing about a member of some other group.

    Everyone except the super admin shares one window per group, so opening the
    command to all members cannot be turned into a way to make the bot redraw the
    same cards over and over.
    """
    target_group = command.group_id or event.group_id
    if target_group not in ALLOWED_GROUP_IDS:
        await bot.send_group_msg(
            group_id=event.group_id, message=recall_target_refused_reply()
        )
        return
    if target_group != event.group_id and not is_named_in_group(
        target_group, event.user_id
    ):
        await bot.send_group_msg(
            group_id=event.group_id, message=recall_target_refused_reply()
        )
        return

    if command.private:
        if not is_authorized(target_group, event.user_id):
            await bot.send_group_msg(
                group_id=event.group_id, message=recall_private_admin_only_reply()
            )
            return
        if not await is_switch_on(RECALL_PRIVATE_DELIVERY):
            await bot.send_group_msg(
                group_id=event.group_id, message=private_delivery_disabled_reply()
            )
            return

    if command.all_pending and not is_super:
        await bot.send_group_msg(
            group_id=event.group_id, message=recall_all_refused_reply()
        )
        return

    if not is_super:
        allowed, remaining, first_notice = await take_group_recall_slot(target_group)
        if not allowed:
            # Told once per window; a group hammering the command gets silence
            # instead of a wall of "wait N seconds".
            if first_notice:
                await bot.send_group_msg(
                    group_id=event.group_id, message=recall_cooldown_reply(remaining)
                )
            return

    count = command.count
    if count is not None:
        count = max(1, min(count, RECALL_MAX_COUNT))
    request = recall_request_for(
        event, group_id=target_group, private=command.private
    )
    delivered = await safe_send_recall_history(
        bot, request, count, all_pending=command.all_pending
    )
    # The pictures went private, so the group gets one receipt line; that is the
    # only trace the command leaves there.
    if command.private and delivered:
        await bot.send_group_msg(
            group_id=event.group_id, message=recall_private_notice(delivered)
        )


# 手打的 `@可莉` 不会生成 at 段（QQ 只在成员列表里选 @ 才发 at）。这里把"看起来在叫我"
# 的纯文本也算作 mention——但必须后面紧跟的是**已知命令**，否则群里随便提到名字都会响应。
TEXTUAL_MENTION_ALIASES = ("@可莉", "可莉")
TEXTUAL_MENTION_SEPARATORS = " \t\u3000,，:：、"  # 空格/全角空格/逗号/冒号/顿号


def textual_mention_command(message: Message) -> str | None:
    """若这条消息是"文本形式的 @机器人 + 命令"，返回命令部分；否则返回 None。"""
    if any(segment.type != "text" for segment in message):
        return None
    raw = "".join(str(segment.data.get("text", "")) for segment in message).strip()
    if not raw:
        return None
    for alias in TEXTUAL_MENTION_ALIASES:
        if not raw.startswith(alias):
            continue
        rest = raw[len(alias):].lstrip(TEXTUAL_MENTION_SEPARATORS).strip()
        if not rest:
            continue
        if is_recognized_command(rest, rest.casefold()):
            return rest
    return None


@group_messages.handle()
async def handle_group_message(bot: Bot, event: GroupMessageEvent) -> None:
    # Archive messages from the configured group, including unauthorized users.
    if event.group_id not in ALLOWED_GROUP_IDS:
        return
    await save_message(bot, event)

    bottle_only = event.group_id in BOTTLE_ONLY_GROUP_IDS
    original_message = getattr(event, "original_message", event.get_message())
    is_admin = is_authorized(event.group_id, event.user_id)
    is_super = is_super_admin(event.user_id)
    only_text_segments = all(segment.type == "text" for segment in original_message)
    exact_text = "".join(
        str(segment.data.get("text", ""))
        for segment in original_message
        if segment.type == "text"
    )
    if only_text_segments and exact_text == "!recalled":
        if bottle_only:
            return
        await serve_recall_query(
            bot, event, RecallCommand(False, False, None, None), is_super=is_super
        )
        return

    is_mentioning_bot = (
        event.is_tome()
        or is_at_bot(event.get_message(), bot)
        or is_at_bot(original_message, bot)
    )
    if is_mentioning_bot:
        text = without_bot_mention(original_message, bot)
    else:
        # 手打 `@可莉 漂流瓶` 没有 at 段：只有后半段是已知命令时才认，否则照旧静默。
        textual = textual_mention_command(original_message)
        if textual is None:
            return
        text = textual
    folded = text.casefold()

    # 只开漂流瓶的群：撤回记录、总结、点歌、私聊投递等全部静默。
    if bottle_only and not bottle_only_allowed(text, folded):
        return

    # Private ops command: only the owner's QQ numbers get an answer, everyone
    # else is ignored (and silently, without spending a cooldown slot).
    if folded in STATUS_COMMANDS:
        # 被点名为“不参与 status”的群：这条命令在这些群里完全静默。
        if not status_enabled_for(event.group_id):
            return
        if is_status_user(event.user_id):
            await safe_send_status(bot, event)
        return

    # 漂流瓶 carries its own per-user cooldown and daily quota for everyone,
    # so it is routed before the generic non-admin mention cooldown.
    bottle_album_only = parse_bottle_command(text)
    if bottle_album_only is not None:
        await safe_send_essence(bot, event, album_only=bottle_album_only)
        return

    # A bare @,闲聊 or a typo is ignored completely: no reply and no cooldown
    # slot burned, so a stray mention cannot swallow the next real command.
    if not is_recognized_command(text, folded):
        return

    # 撤回记录 is open to every member now, and it carries its own *group-wide*
    # cooldown, so it is routed before the per-user mention cooldown — otherwise
    # one query would also block that member's 总结/help for two minutes.
    recall_command = parse_recall_command(text)
    if recall_command is not None:
        # 「历史」只允许主人在私聊里看；群里完全静默。
        if recall_command.history:
            return
        await serve_recall_query(bot, event, recall_command, is_super=is_super)
        return

    if not is_admin and not await take_non_admin_mention_slot(event.group_id, event.user_id):
        return

    if folded in INCREMENTAL_COMMANDS:
        await guarded_incremental_summary(bot, event)
        return

    if folded in SUMMARY_COMMANDS:
        await send_yesterday_summary(bot, event)
        return

    if folded in HELP_COMMANDS:
        # 个别群只讲漂流瓶：连管理员段和撤回/总结说明都不出现。
        await bot.send_group_msg(
            group_id=event.group_id,
            message=group_help(event.group_id, is_admin=is_admin),
        )
        return

    if folded in BOTTLE_RANK_COMMANDS:
        await safe_send_essence_leaderboard(bot, event)
        return

    if folded in DRAWN_RANK_COMMANDS:
        await safe_send_drawn_content_leaderboard(bot, event)
        return

    voice_name = parse_voice_command(text)
    if voice_name is not None:
        await safe_send_voice(bot, event, voice_name)
        return

    # 私聊投递的总开关。群里只能关：群成员若能打开它，就等于能自定义一条
    # 把撤回记录送进自己私聊的通道，方向刚好是隐私敏感的那一侧。
    toggle_command = parse_toggle_command(text)
    if toggle_command is not None:
        if not is_admin:
            await bot.send_group_msg(
                group_id=event.group_id, message=recall_private_admin_only_reply()
            )
            return
        if toggle_command.wanted is True:
            await bot.send_group_msg(
                group_id=event.group_id, message=toggle_group_only_off_text()
            )
            return
        if toggle_command.wanted is False:
            changed = await set_switch(RECALL_PRIVATE_DELIVERY, False)
            await bot.send_group_msg(
                group_id=event.group_id, message=toggle_off_text(changed)
            )
            return
        await bot.send_group_msg(
            group_id=event.group_id,
            message=toggle_state_text(
                await is_switch_on(RECALL_PRIVATE_DELIVERY), from_group=True
            ),
        )
        return

    if folded in CLEAR_COMMANDS:
        # clear 会把整个群的待召回队列一次抹掉，所以只有主人能用。
        if not is_super:
            await bot.send_group_msg(
                group_id=event.group_id, message=clear_owner_only_reply()
            )
            return
        cleared = await clear_pending_recall(str(event.group_id))
        await bot.send_group_msg(
            group_id=event.group_id, message=clear_recall_text(cleared)
        )
        return

    # Unreachable: 撤回记录 is routed above and is_recognized_command() already
    # filtered everything else out.
    logger.debug("Unrouted group command {!r}", text)


@group_recall.handle()
async def handle_group_recall(event: GroupRecallNoticeEvent) -> None:
    if event.group_id in ALLOWED_GROUP_IDS:
        await mark_recalled(event)


async def _reply_private(
    bot: Bot, event: PrivateMessageEvent, message: str, *, group_id: int | None
) -> None:
    """Answer in the 1:1 chat.

    Carrying ``group_id`` keeps a 群临时会话 reachable for someone who is not a
    friend; NapCat folds it back into an ordinary private message when they are.
    """
    if group_id is None:
        await bot.send_private_msg(user_id=event.user_id, message=message)
    else:
        await bot.send_private_msg(
            user_id=event.user_id, group_id=group_id, message=message
        )


def private_context_group(event: PrivateMessageEvent) -> int | None:
    """The whitelisted group NapCat attached to a 群临时会话, if any.

    Being addressed through a group's temp session is itself evidence that the
    sender is in that group, which is what makes a ``*`` (全体成员) group usable
    from the 1:1 side.  NapCat substitutes a placeholder group id when it cannot
    resolve the origin, which the whitelist check here filters out.
    """
    raw_group_id = getattr(event, "group_id", None)
    try:
        group_id = int(raw_group_id) if raw_group_id is not None else None
    except (TypeError, ValueError):
        return None
    return group_id if group_id in ALLOWED_GROUP_IDS else None


def private_target_group(event: PrivateMessageEvent, explicit: int | None) -> int | None:
    """Which group a command typed in a 1:1 chat should read records from.

    A 群临时会话 carries its origin group on the event; an ordinary private chat
    does not, so an explicit group number wins there, and a single-group install
    needs neither.  ``None`` means "could not be decided", never "no group".
    """
    if explicit is not None:
        return explicit if explicit in ALLOWED_GROUP_IDS else None
    context_group = private_context_group(event)
    if context_group is not None:
        return context_group
    if len(ALLOWED_GROUP_IDS) == 1:
        return next(iter(ALLOWED_GROUP_IDS))
    return None


def private_entry_allowed(event: PrivateMessageEvent, user_id: int) -> bool:
    """Whether a 1:1 message may be answered at all.

    A ``*`` in a group's list means "everyone in that group", which a private
    message does not prove on its own — so the sender must either be named by
    number somewhere, or be arriving through a temp session of such a group.
    """
    if is_named_admin(user_id):
        return True
    context_group = private_context_group(event)
    return context_group is not None and group_allows_everyone(context_group)


async def _handle_water_command(
    bot: Bot,
    event: PrivateMessageEvent,
    action: str,
    user_id: int,
    *,
    group_id: int | None,
) -> None:
    """私聊里的喝水提醒开关（仅主人可改）。"""
    if not is_status_user(user_id):
        await _reply_private(bot, event, "喝水提醒只有主人能开关哦。", group_id=group_id)
        return
    if action == "test":
        sent, message = await water.send_test(bot)
        await _reply_private(
            bot,
            event,
            f"测试提醒已发到 {sent} 个地方（私聊 + 群），内容是：\n{message}",
            group_id=group_id,
        )
        return
    if action == "on":
        changed = await water.set_water(True)
        message = (
            "喝水提醒已经开启啦，可莉会按时提醒你～"
            if changed
            else "喝水提醒本来就是开着的哦。"
        )
    elif action == "off":
        changed = await water.set_water(False)
        message = "喝水提醒已关闭，想开的时候再跟我说～" if changed else "喝水提醒本来就关着哦。"
    else:
        message = (
            "喝水提醒现在是开启的：每天 19 次（07:50 起床、08:30–10:50 五次、12:40、13:50、"
            "14:00–17:45 每 45 分钟、19:00、20:00–22:30 每小时）。\n"
            "发「喝水提醒 关」可以关掉，发「喝水提醒 测试」可以立刻试一条。"
            if await water.water_enabled()
            else "喝水提醒现在是关闭的。发「喝水提醒 开」可以打开。"
        )
    await _reply_private(bot, event, message, group_id=group_id)


@private_messages.handle()
async def handle_private_message(bot: Bot, event: PrivateMessageEvent) -> None:
    """The 1:1 chat entry point: 群临时会话, or an ordinary private message.

    Only the recall tooling is served here, and only for someone who counts as an
    admin: named by QQ number in some list, or arriving through a temp session of
    a group that grants everyone.  Anything else — chit-chat, strangers, typos —
    stays completely silent, exactly like an unrecognised @ does in a group.
    """
    user_id = int(event.user_id)
    if not private_entry_allowed(event, user_id):
        return
    context_group = private_context_group(event)

    text = event.get_message().extract_plain_text().strip()
    folded = text.casefold()
    water_command = water.parse_water_command(text)
    if water_command is not None:
        await _handle_water_command(bot, event, water_command, user_id, group_id=context_group)
        return

    recall_command = parse_recall_command(text)
    toggle_command = parse_toggle_command(text)
    wants_clear = folded in CLEAR_COMMANDS
    wants_help = folded in HELP_COMMANDS
    if (
        recall_command is None
        and toggle_command is None
        and not wants_clear
        and not wants_help
    ):
        return

    # The switch is handled first and is never gated by its own state, otherwise
    # turning it off would also disable the only way to turn it back on.
    if toggle_command is not None:
        if toggle_command.wanted is True and not is_super_admin(user_id):
            await _reply_private(
                bot, event, toggle_owner_only_text(), group_id=context_group
            )
            return
        if toggle_command.wanted is None:
            await _reply_private(
                bot,
                event,
                toggle_state_text(
                    await is_switch_on(RECALL_PRIVATE_DELIVERY), from_group=False
                ),
                group_id=context_group,
            )
            return
        changed = await set_switch(RECALL_PRIVATE_DELIVERY, toggle_command.wanted)
        await _reply_private(
            bot,
            event,
            toggle_on_text(changed) if toggle_command.wanted else toggle_off_text(changed),
            group_id=context_group,
        )
        return

    # "help" still answers while the feature is off: it is how you find out how
    # to turn it back on.
    if wants_help:
        await _reply_private(
            bot, event, private_help(is_super=is_super_admin(user_id)), group_id=context_group
        )
        return

    if not await is_switch_on(RECALL_PRIVATE_DELIVERY):
        await _reply_private(
            bot, event, private_delivery_disabled_reply(), group_id=context_group
        )
        return

    if recall_command is not None and recall_command.history:
        if not is_super_admin(user_id):
            await _reply_private(
                bot, event, recall_history_owner_only_reply(), group_id=context_group
            )
            return
        explicit = recall_command.group_id
        if explicit is not None and explicit not in ALLOWED_GROUP_IDS:
            await _reply_private(
                bot, event, recall_target_refused_reply(), group_id=context_group
            )
            return
        groups = [explicit] if explicit is not None else sorted(ALLOWED_GROUP_IDS)
        count = recall_command.count
        if count is not None:
            count = max(1, min(count, RECALL_MAX_COUNT))
        await safe_send_all_recall_history(
            bot,
            RecallRequest(
                group_id=groups[0], user_id=user_id, private=True, from_group=False
            ),
            count,
            groups=groups,
        )
        return

    target_group = private_target_group(
        event, recall_command.group_id if recall_command else None
    )
    if target_group is None:
        await _reply_private(bot, event, recall_unknown_group_reply(), group_id=context_group)
        return
    # The 1:1 side stays an admin tool.  Reaching another group by number also
    # needs the asker to be named for it, so a `*` group cannot be read from a
    # temp session of a different one.
    reaches_across = target_group != context_group and not is_named_in_group(
        target_group, user_id
    )
    if reaches_across or not is_authorized(target_group, user_id):
        await _reply_private(
            bot, event, recall_target_refused_reply(), group_id=target_group
        )
        return

    if wants_clear:
        if not is_super_admin(user_id):
            await _reply_private(
                bot, event, clear_owner_only_reply(), group_id=target_group
            )
            return
        cleared = await clear_pending_recall(str(target_group))
        await _reply_private(
            bot, event, clear_recall_text(cleared), group_id=target_group
        )
        return

    if recall_command is None:
        # Unreachable: "help" and "clear" were the only non-parsing commands and
        # both returned already.  Kept so the block below reads unambiguously.
        return

    if recall_command.all_pending and not is_super_admin(user_id):
        await _reply_private(
            bot, event, recall_all_refused_reply(), group_id=target_group
        )
        return
    count = recall_command.count
    if count is not None:
        count = max(1, min(count, RECALL_MAX_COUNT))
    # Everything about this request is private: the cards and the notices both
    # go back to the chat the command came from, never into the group.
    await safe_send_recall_history(
        bot,
        RecallRequest(
            group_id=target_group, user_id=user_id, private=True, from_group=False
        ),
        count,
        all_pending=recall_command.all_pending,
    )


@group_member_increase.handle()
async def handle_group_member_increase(bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
    if event.group_id in ALLOWED_GROUP_IDS:
        avatar_dir(event.group_id).mkdir(parents=True, exist_ok=True)
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
                await cache_avatar(event.group_id, str(event.user_id), client, force=True)
        except Exception:
            logger.warning("Could not cache avatar for new member {}", event.user_id, exc_info=True)
