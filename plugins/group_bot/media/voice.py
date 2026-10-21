"""语音点歌：`@可莉 炸乌云` 把 ``data/voice/`` 里的歌**以语音（record）发出**。

设计成"投放目录 + 自动发现"，是为了以后加别的原神歌曲时**不用改代码**：
把一个音频文件丢进 ``data/voice/`` 就等于上架了一首，歌名就是文件名（不含扩展名）。

两条现实约束（NAP CAT / QQ 侧，不是本模块能解决的）：

* QQ 语音消息通常限 **60 秒**左右，整首歌（3–4 分钟）很可能发不出去；建议放剪辑版，
  或者接受"语音发不出去时回一句提示"。
* 支持的容器取决于 NapCat 的解码能力，``.mp3`` / ``.amr`` / ``.silk`` 最稳。

因此发送失败时**不静默**：记日志 + 回一句带解决方法的提示（见 :func:`voice_failed_text`）。
"""

from __future__ import annotations

from pathlib import Path

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageSegment

from ..core.config import DB_PATH, VOICE_MAX_SECONDS
from ..common.safety import guard_group
from .voice_audio import ensure_short

# 语音素材目录：和 media_cache / avatars 同级，都是本地文件，进 .gitignore 的 data/。
# ⚠️ 必须 resolve 成绝对路径：`DB_PATH` 来自 .env，是相对路径（data/messages.sqlite3），
# 直接拼出来会是 `data\voice\...`，NapCat 会报「识别URL失败」（retcode 1200）。
VOICE_DIR = (DB_PATH.parent / "voice").resolve()
VOICE_SUFFIXES = (".mp3", ".amr", ".silk", ".wav", ".m4a", ".ogg", ".flac")
# 截断后的副本放在这里：源文件一改（mtime/大小变化）就自动失效。
VOICE_TRIM_DIR = VOICE_DIR / ".trimmed"
# `炸乌云` 不带歌名时优先放这首（文件名包含即可）。
DEFAULT_VOICE_KEYWORD = "让风告诉你"


def list_voices() -> list[Path]:
    """``data/voice/`` 下所有可用音频，按文件名排序（结果稳定，便于测试与复现）。"""
    if not VOICE_DIR.is_dir():
        return []
    return sorted(
        path
        for path in VOICE_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in VOICE_SUFFIXES
    )


def voice_names() -> list[str]:
    return [path.stem for path in list_voices()]


def match_voice(name: str = "") -> tuple[Path | None, str]:
    """按歌名片段找一首。

    返回 ``(路径, 状态)``，状态是 ``"ok"`` / ``"empty"``（目录里没有歌）/
    ``"unknown"``（点了歌名但没匹配上）。不给歌名时优先 :data:`DEFAULT_VOICE_KEYWORD`，
    否则取第一首——**不做随机**，免得"每次炸乌云放的都不一样"让人困惑。
    """
    voices = list_voices()
    if not voices:
        return None, "empty"

    wanted = " ".join(str(name or "").split()).strip()
    if wanted:
        for path in voices:
            if path.stem == wanted:
                return path, "ok"
        for path in voices:
            if wanted in path.stem or path.stem in wanted:
                return path, "ok"
        return None, "unknown"

    for path in voices:
        if DEFAULT_VOICE_KEYWORD in path.stem:
            return path, "ok"
    return voices[0], "ok"


def empty_voice_text() -> str:
    return (
        "可莉还没学会这首歌呢…\n"
        f"把音频文件放进 {VOICE_DIR} 就好啦（歌名用文件名，比如 "
        f"「{DEFAULT_VOICE_KEYWORD}.mp3」）。\n"
        "小提示：QQ 语音一般只支持 60 秒以内，整首歌建议先剪一段。"
    )


def unknown_voice_text(name: str, available: list[str]) -> str:
    lines = [f"可莉的曲库里没有「{name}」这首歌呢。"]
    if available:
        lines.append("现在会的是：" + "、".join(available[:10]))
    else:
        lines.append(f"曲库还是空的，把音频放进 {VOICE_DIR} 就行啦。")
    return "\n".join(lines)


def voice_failed_text() -> str:
    return (
        "这首歌可莉没能用语音发出去…常见原因是文件太大或超过 60 秒。\n"
        "可以把歌剪成 60 秒以内的片段再放进去，或者换 .mp3 / .silk 格式试试。"
    )


async def send_voice(bot: Bot, event: GroupMessageEvent, name: str = "") -> None:
    """按歌名（可空）发一条语音。找不到歌就回提示，不抛异常。"""
    path, status = match_voice(name)
    group_id = int(event.group_id)

    if status == "empty":
        await bot.send_group_msg(group_id=group_id, message=empty_voice_text())
        return
    if status == "unknown" or path is None:
        await bot.send_group_msg(
            group_id=group_id,
            message=unknown_voice_text(" ".join(str(name).split()), voice_names()),
        )
        return

    # 超过时长上限的歌自动截成前 N 秒（QQ 语音一般只支持 60 秒左右）。
    # 截不了（格式不支持/解析失败）会原样返回，交给 NapCat 去试，失败时另有兜底文案。
    if VOICE_MAX_SECONDS > 0:
        path = ensure_short(path, VOICE_MAX_SECONDS, cache_dir=VOICE_TRIM_DIR)

    # NapCat 对 `record` 段的 file 走"识别 URL / 本地路径"的逻辑：给相对路径会直接
    # 报「识别URL失败, retcode 1200」。所以先试 `file://` URI（本项目图片一直这么发，
    # 已验证可用），不行再退回**绝对**路径，两种形式都失败才认输。
    path = path.resolve()
    candidates = [path.as_uri(), str(path)]
    logger.info("Sending voice {} to group {} ({} 字节)", path.name, group_id, path.stat().st_size)
    last_error: Exception | None = None
    for index, candidate in enumerate(candidates, start=1):
        try:
            result = await bot.send_group_msg(
                group_id=group_id, message=MessageSegment("record", {"file": candidate})
            )
            if isinstance(result, dict) and result.get("status") == "failed":
                raise RuntimeError(f"OneBot failed to send the voice message: {result}")
        except Exception as error:  # noqa: BLE001  两种形式都要试，最后一次才认输
            last_error = error
            logger.warning(
                "Voice send failed with {} form ({}): {}", "uri" if index == 1 else "path", candidate, error
            )
            continue
        logger.info("Voice accepted by NapCat using {} form", "uri" if index == 1 else "absolute path")
        return

    logger.error("Could not send voice {} ; last error: {}", path.name, last_error)
    await bot.send_group_msg(group_id=group_id, message=voice_failed_text())


async def safe_send_voice(bot: Bot, event: GroupMessageEvent, name: str = "") -> None:
    """处理器层入口：任何异常都只记日志 + 回一句兜底，不炸 matcher。"""
    await guard_group(
        "Voice handler",
        bot,
        int(event.group_id),
        lambda: send_voice(bot, event, name),
        "可莉找歌的时候把谱子弄丢了，等会儿再试一次吧～",
    )
