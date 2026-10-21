"""「炸乌云」语音点歌的回归测试。

覆盖：曲库发现、歌名匹配（精确/模糊/缺省走默认）、三种失败提示（空库/点了没的歌/
发送失败）、以及真的发出 `record` 段（不是图片、不是文字）。

注意：曲库目录是 `data/voice/`，测试里临时改 `voice.VOICE_DIR` 指向临时目录，
避免碰到真实素材——也不会因为真实素材的存在而让结果漂移。
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import shutil
import sys
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
DB = TEMP / "verify_voice.sqlite3"
for stale in TEMP.glob("verify_voice.sqlite3*"):
    stale.unlink()

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001",
    authorized_users="100000009",
    database_path=str(DB),
)

from plugins.group_bot import handlers, voice  # noqa: E402

GROUP = 123450001
failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")



def file_of(segment) -> Path:
    """把 record 段里的 file 取成本地路径（可能是 file:// URI，也可能是绝对路径）。"""
    raw = str(getattr(segment, "data", {}).get("file", ""))
    if raw.startswith("file:"):
        import urllib.parse

        return Path(urllib.parse.unquote(urllib.parse.urlparse(raw).path.lstrip("/")))
    return Path(raw)

def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


def make_library(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("让风告诉你.mp3", "璃月.silk", "让风告诉你（剪辑版）.mp3", "note.txt"):
        (root / name).write_bytes(b"fake")


class SendBot:
    def __init__(self, fail: bool = False) -> None:
        self.messages: list = []
        self.fail = fail

    async def send_group_msg(self, **kwargs):
        self.messages.append(kwargs["message"])
        if self.fail and len(self.messages) <= 2:  # URI 与绝对路径两次都失败，提示本身要能发出去
            raise RuntimeError("send failed")
        return {"status": "ok"}

    @property
    def self_id(self):
        return "100000000"


def main() -> None:
    import asyncio

    original_dir = voice.VOICE_DIR
    library = TEMP / "voice_lib"
    if library.exists():
        for item in library.iterdir():
            item.unlink()
    make_library(library)
    voice.VOICE_DIR = library
    try:
        print("=== 1. 曲库发现 ===")
        check("只认音频后缀", voice.voice_names(), ["璃月", "让风告诉你", "让风告诉你（剪辑版）"])

        print("\n=== 2. 歌名匹配 ===")
        path, status = voice.match_voice("")
        check("缺省优先默认那首", (status, path.name if path else None),
              ("ok", "让风告诉你.mp3"))
        path, status = voice.match_voice("让风告诉你（剪辑版）")
        check("精确匹配优先", (status, path.name if path else None),
              ("ok", "让风告诉你（剪辑版）.mp3"))
        path, status = voice.match_voice("璃月")
        check("点别的歌", (status, path.name if path else None), ("ok", "璃月.silk"))
        path, status = voice.match_voice("不存在的歌")
        check("点了没有的歌", (status, path), ("unknown", None))

        print("\n=== 3. 发送：是 record 段，不是图片/文字 ===")
        event = SimpleNamespace(group_id=GROUP, user_id=777, group_name="测试群")
        bot = SendBot()
        asyncio.run(voice.send_voice(bot, event, ""))
        check("只发一条", len(bot.messages), 1)
        segment = bot.messages[0]
        check("段类型是 record", getattr(segment, "type", None), "record")
        check_true("file 是绝对路径或 URI", "让风告诉你" in urllib.parse.unquote(str(segment.data.get("file", ""))))
        check("默认歌名正确", file_of(segment).stem, "让风告诉你")

        bot = SendBot()
        asyncio.run(voice.send_voice(bot, event, "璃月"))
        check("点歌发出对应文件", file_of(bot.messages[0]).stem, "璃月")

        print("\n=== 4. 失败与空曲库 ===")
        bot = SendBot()
        asyncio.run(voice.send_voice(bot, event, "不存在的歌"))
        check("未知歌名只回文字", len(bot.messages), 1)
        check_true("文字提到了歌名", "不存在的歌" in str(bot.messages[0]))

        bot = SendBot(fail=True)
        asyncio.run(voice.send_voice(bot, event, ""))
        check("两次形式都失败后才回提示", len(bot.messages), 3)
        check_true("说明里提到 60 秒", "60 秒" in str(bot.messages[2]))

        voice.VOICE_DIR = TEMP / "voice_empty"
        voice.VOICE_DIR.mkdir(exist_ok=True)
        for item in voice.VOICE_DIR.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        bot = SendBot()
        asyncio.run(voice.send_voice(bot, event, ""))
        check("空曲库只回文字", len(bot.messages), 1)
        check_true("文字里给了投放路径", "voice" in str(bot.messages[0]))

        print("\n=== 5. 命令解析 ===")
        check("炸乌云", handlers.parse_voice_command("炸乌云"), "")
        check("炸乌云 + 歌名", handlers.parse_voice_command("炸乌云 让风告诉你"), "让风告诉你")
        check("不是这条命令", handlers.parse_voice_command("漂流瓶"), None)
        check_true("命令被识别", handlers.is_recognized_command("炸乌云", "炸乌云"))
        check_true("help 里有它", "炸乌云" in handlers.command_help(is_admin=False))

        print("\n=== 6. 超过上限会被截成前 N 秒再发 ===")
        # 造一首 90 秒的合法 MP3（MPEG1 L3 128k/44.1k，每帧 26.12 ms）
        long_lib = TEMP / "voice_long"
        long_lib.mkdir(exist_ok=True)
        for item in long_lib.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 413
        frames = int(90 * 1000 / (1152 / 44100 * 1000))
        (long_lib / "让风告诉你.mp3").write_bytes(frame * frames)
        voice.VOICE_DIR = long_lib
        voice.VOICE_TRIM_DIR = long_lib / ".trimmed"
        original_max = voice.VOICE_MAX_SECONDS
        voice.VOICE_MAX_SECONDS = 60
        try:
            from plugins.group_bot.media.voice_audio import mp3_duration

            bot = SendBot()
            asyncio.run(voice.send_voice(bot, event, ""))
            sent = file_of(bot.messages[0])
            check_true("发的是截断副本（在 .trimmed 里）", ".trimmed" in sent.parent.name)
            check_true("副本 ≤ 60 秒", mp3_duration(sent) <= 60.0)
            check_true("副本时长接近 60 秒", mp3_duration(sent) > 59.5)
            check_true("原文件没被改动", (long_lib / "让风告诉你.mp3").stat().st_size > sent.stat().st_size)
        finally:
            voice.VOICE_MAX_SECONDS = original_max

        print("\n=== 7. 发送形式：先用 file:// URI，失败回退绝对路径 ===")
        voice.VOICE_DIR = library

        class UriAwareBot:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def send_group_msg(self, **kwargs):
                raw = str(kwargs["message"].data.get("file", ""))
                self.calls.append(raw)
                if len(self.calls) == 1:
                    raise RuntimeError("文件处理失败: 识别URL失败 (retcode 1200)")
                return {"status": "ok"}

        bot = UriAwareBot()
        asyncio.run(voice.send_voice(bot, event, ""))
        check("只发成功一次（回退后不再重复发）", len(bot.calls), 2)
        check_true("第一次尝试用 file:// URI", bot.calls[0].startswith("file://"))
        check_true("第二次回退用绝对路径", Path(bot.calls[1]).is_absolute() and not bot.calls[1].startswith("file:"))
        check_true(
            "两次指向同一个文件",
            Path(bot.calls[1]).name in urllib.parse.unquote(bot.calls[0]),
        )
    finally:
        voice.VOICE_DIR = original_dir

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


main()
