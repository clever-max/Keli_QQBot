"""手打 `@可莉 命令`（没有 at 段）也要被识别；但闲聊里提到名字不能触发。

背景：2026-10-18 19:14 群里发 `@可莉 漂流瓶`，因为 QQ 只在成员列表选择时生成 at 段，
这条是纯文本 → 机器人按设计静默忽略，用户看到的现象是"漂流瓶死了"。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import nonebot  # noqa: E402

nonebot.init(driver="~fastapi", allowed_group_ids="123450001", authorized_users="100000009")

from nonebot.adapters.onebot.v11 import Message, MessageSegment  # noqa: E402

from plugins.group_bot import handlers  # noqa: E402

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def text_message(text: str) -> Message:
    return Message(MessageSegment.text(text))


def main() -> None:
    print("=== 1. 手打 @ + 已知命令 ===")
    check("@可莉 漂流瓶", handlers.textual_mention_command(text_message("@可莉 漂流瓶")), "漂流瓶")
    check("@可莉 炸乌云", handlers.textual_mention_command(text_message("@可莉 炸乌云")), "炸乌云")
    check("可莉，漂流瓶（逗号分隔）", handlers.textual_mention_command(text_message("可莉，漂流瓶")), "漂流瓶")
    check("全角空格分隔", handlers.textual_mention_command(text_message("@可莉　漂流瓶")), "漂流瓶")
    check("带歌名", handlers.textual_mention_command(text_message("@可莉 炸乌云 璃月")), "炸乌云 璃月")

    print("\n=== 2. 不能误触发 ===")
    check("只有名字", handlers.textual_mention_command(text_message("可莉")), None)
    check("名字 + 闲聊", handlers.textual_mention_command(text_message("@可莉 今天吃什么")), None)
    check("不是开头", handlers.textual_mention_command(text_message("我觉得可莉 漂流瓶")), None)
    at_message = Message(MessageSegment.at("100000000")) + MessageSegment.text(" 漂流瓶")
    check("已有 at 段（走正常路径）", handlers.textual_mention_command(at_message), None)

    print("\n=== 3. 命令识别仍按原规则 ===")
    check("漂流瓶 被识别", handlers.is_recognized_command("漂流瓶", "漂流瓶"), True)
    check("闲话不被识别", handlers.is_recognized_command("今天吃什么", "今天吃什么"), False)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


main()
