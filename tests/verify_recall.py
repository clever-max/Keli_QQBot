"""Offline self-check for the 撤回记录 私聊/临时会话 feature.

Imports the real plugin package (without running it), then exercises the pure
parsing and group-resolution logic.  Nothing here touches the live database,
OneBot, or the network.
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import py_compile
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT = Path(str(ROOT))
TARGETS = [
    PROJECT / "plugins" / "group_bot" / "handlers.py",
    PROJECT / "plugins" / "group_bot" / "recall" / "history.py",
    PROJECT / "plugins" / "group_bot" / "core" / "config.py",
]

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


print("== 1. 语法编译 ==")
for target in TARGETS:
    py_compile.compile(str(target), doraise=True)
    print(f"ok   compiled {target.name}")

print("\n== 2. 导入插件包 ==")
sys.path.insert(0, str(PROJECT))
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001,123450002",
    authorized_users="100000009",
    group_authorized_users="123450001:100000009,100000011;123450002:*",
    status_users="100000009",
    database_path=str(Path(tempfile.gettempdir()) / "recall_verify" / "unused.sqlite3"),
)

import plugins.group_bot  # noqa: E402,F401
from plugins.group_bot.core import config as gb_config  # noqa: E402
from plugins.group_bot.handlers import (  # noqa: E402
    RecallCommand,
    parse_recall_command,
    private_target_group,
)
from plugins.group_bot.recall.history import RecallRequest  # noqa: E402

print("ok   plugins.group_bot imported")

print("\n== 3. 权限助手 ==")
check("super admin", gb_config.is_super_admin(100000009), True)
check("super admin (str)", gb_config.is_super_admin("100000009"), True)
check("stranger is not super", gb_config.is_super_admin(123456), False)
check("authorized in a specific group", gb_config.is_authorized(123450001, 100000011), True)
# 私聊入口的身份判定：必须被点名，或由群临时会话证明成员身份。
check("named: global list", gb_config.is_named_admin(100000009), True)
check("named: group list", gb_config.is_named_admin(100000011), True)
check("stranger is NOT named", gb_config.is_named_admin(123456789), False)
check("wildcard group allows everyone", gb_config.group_allows_everyone(123450002), True)
check("normal group does not", gb_config.group_allows_everyone(123450001), False)
# 陷阱提醒：把「任一白名单群授权」当成私聊入口的门，会因为有 `*` 群而放过任何人。
check("any-group check would let a stranger through", gb_config.is_authorized(123450002, 123456789), True)
check("...but is_authorized on the normal group is not", gb_config.is_authorized(123450001, 123456789), False)

print("\n== 4. 撤回记录 命令解析 ==")
C = RecallCommand
cases = [
    # text, expected
    ("撤回记录", C(False, False, None, None)),
    ("撤回记录 20", C(False, False, 20, None)),
    ("撤回记录 全部", C(True, False, None, None)),
    ("撤回记录 全部 50", C(True, False, 50, None)),
    ("撤回记录 私聊", C(False, True, None, None)),
    ("撤回记录 私聊 20", C(False, True, 20, None)),
    ("撤回记录 20 私聊", C(False, True, 20, None)),
    ("撤回记录 全部 私聊 50", C(True, True, 50, None)),
    ("撤回记录 全部 50 私聊", C(True, True, 50, None)),
    ("撤回记录 临时会话", C(False, True, None, None)),
    ("撤回记录 悄悄", C(False, True, None, None)),
    ("撤回记录 123450001", C(False, False, None, 123450001)),
    ("撤回记录 123450001 20", C(False, False, 20, 123450001)),
    ("撤回记录 123450001 20 私聊", C(False, True, 20, 123450001)),
    # not this command / malformed -> None (silently ignored, as before)
    ("撤回记录 随便什么", None),
    ("撤回记录 20 30", None),
    ("撤回记录 123450001 123450002", None),
    ("撤回记录x", None),
    ("撤回记录 20 x", None),
    ("clear", None),
    ("!recalled", None),
    ("", None),
]
for text, want in cases:
    check(f"parse({text!r})", parse_recall_command(text), want)

print("\n== 5. 私聊上下文推导目标群 ==")
ALLOWED = gb_config.ALLOWED_GROUP_IDS
print(f"     ALLOWED_GROUP_IDS = {sorted(ALLOWED)}")
# 群临时会话：NapCat 在事件上带 group_id
check("temp session, group on event", private_target_group(SimpleNamespace(group_id=123450001), None), 123450001)
# 普通私聊（好友）：事件上没有 group_id，靠显式群号
check("plain private, explicit group", private_target_group(SimpleNamespace(), 123450002), 123450002)
# NapCat 解析不到来源群时填的占位群号，必须被白名单挡掉
check("placeholder group rejected", private_target_group(SimpleNamespace(group_id=284840486), 284840486), None)
# 非白名单群号
check("non-whitelisted group rejected", private_target_group(SimpleNamespace(), 123456), None)
# 普通私聊 + 多个群 + 无群号 => 无法判定
check("plain private, ambiguous", private_target_group(SimpleNamespace(), None), None)
# 事件带了合法群号时，显式群号优先
check("explicit wins over event", private_target_group(SimpleNamespace(group_id=123450001), 123450002), 123450002)
# 事件 group_id 是字符串（OneBot 有时给字符串）
check("string group id on event", private_target_group(SimpleNamespace(group_id="123450001"), None), 123450001)

print("\n== 6. RecallRequest 投递方向 ==")
group_mode = RecallRequest(group_id=1, user_id=2)
private_from_group = RecallRequest(group_id=1, user_id=2, private=True, from_group=True)
private_from_private = RecallRequest(group_id=1, user_id=2, private=True, from_group=False)
check("group mode: cards to group", group_mode.private, False)
check("group+private: cards private", private_from_group.private, True)
check("private-issued: notices stay private", private_from_private.from_group, False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} 项失败：")
    for item in failures:
        print("  -", item)
    sys.exit(1)
print("全部通过")
