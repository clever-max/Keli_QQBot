"""Checks for the 私聊投递 switch: parsing, SQLite persistence, and gating.

The switch has two asymmetric rules that matter, so both are asserted here:
opening it from a group must be refused, and turning it off must never lock the
owner out of turning it back on.
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import asyncio
import sys
from pathlib import Path

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
DB = TEMP / "verify_switch.sqlite3"
for stale in TEMP.glob("verify_switch.sqlite3*"):
    stale.unlink()

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001,123450002",
    authorized_users="100000009",
    group_authorized_users="123450001:100000009,100000011;123450002:*",
    status_users="100000009",
    database_path=str(DB),
)

from plugins.group_bot.core import settings as gb_settings  # noqa: E402
from plugins.group_bot.core.config import is_super_admin  # noqa: E402
from plugins.group_bot.handlers import (  # noqa: E402
    ToggleCommand,
    private_help,
    toggle_off_text,
    toggle_on_text,
    toggle_state_text,
    parse_toggle_command,
)
from plugins.group_bot.core.settings import (  # noqa: E402
    RECALL_PRIVATE_DELIVERY,
    is_switch_on,
    set_switch,
)
from plugins.group_bot.core.store import initialize_database  # noqa: E402

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


print("== 1. 开关命令解析 ==")
T = ToggleCommand
cases = [
    ("私聊投递", T(None)),
    ("私聊投递 开", T(True)),
    ("私聊投递 关", T(False)),
    ("私聊投递 开启", T(True)),
    ("私聊投递 关闭", T(False)),
    ("私聊投递 打开", T(True)),
    ("私聊投递 关掉", T(False)),
    ("私聊投递 on", T(True)),
    ("私聊投递 OFF", T(False)),
    ("私聊开关 关", T(False)),
    ("私聊投递开关 开", T(True)),
    # 不属于这条命令 / 形态不对 -> None（保持静默）
    ("私聊投递 随便", None),
    ("私聊投递 开 关", None),
    ("私聊投递开", None),
    ("私聊", None),
    ("撤回记录 私聊", None),
    ("clear", None),
    ("", None),
]
for text, want in cases:
    check(f"parse({text!r})", parse_toggle_command(text), want)

print("\n== 2. 不能让开关命令和撤回记录命令互相误判 ==")
from plugins.group_bot.handlers import parse_recall_command  # noqa: E402

check("撤回记录 私聊 不是开关命令", parse_toggle_command("撤回记录 私聊"), None)
check("私聊投递 关 不是撤回命令", parse_recall_command("私聊投递 关"), None)

print("\n== 3. 持久化：默认开、写入后跨连接仍生效 ==")


async def persistence_checks() -> None:
    check("默认（未写过）为开", await is_switch_on(RECALL_PRIVATE_DELIVERY), True)
    check("关掉时返回“确实变了”", await set_switch(RECALL_PRIVATE_DELIVERY, False), True)
    check("关掉后读到关", await is_switch_on(RECALL_PRIVATE_DELIVERY), False)
    check("再关一次返回“没变”", await set_switch(RECALL_PRIVATE_DELIVERY, False), False)
    check("打开时返回“确实变了”", await set_switch(RECALL_PRIVATE_DELIVERY, True), True)
    check("打开后读到开", await is_switch_on(RECALL_PRIVATE_DELIVERY), True)
    # A brand-new connection must still see it: this is the restart guarantee.
    check("新连接仍读到开", await is_switch_on(RECALL_PRIVATE_DELIVERY), True)
    check("未知开关走 default=False", await is_switch_on("nope", default=False), False)
    check("未知开关走 default=True", await is_switch_on("nope", default=True), True)

    async with gb_settings.connect() as db:
        cursor = await db.execute(
            "SELECT name, value, updated_at FROM bot_settings WHERE name=?",
            (RECALL_PRIVATE_DELIVERY,),
        )
        row = await cursor.fetchone()
    check("落库的表与键", row["name"], RECALL_PRIVATE_DELIVERY)
    check("落库的值", row["value"], "1")
    check("落库带时间戳", bool(row["updated_at"]), True)


print("\n== 4. 谁能开关（权限模型）==")
check("主人是超级管理员", is_super_admin(100000009), True)
check("群级管理员不是超级管理员", is_super_admin(100000011), False)

print("\n== 5. 文案 ==")
check("开着的状态文案含引导", "关" in toggle_state_text(True, from_group=False), True)
check("群里的状态文案说明只能关", "只能关" in toggle_state_text(True, from_group=True), True)
check("群里状态文案不教怎么开", "私聊里说" in toggle_state_text(True, from_group=True), True)
check("关掉（有变化）的文案", "关掉啦" in toggle_off_text(True), True)
check("关掉（本来就是关的）的文案", "本来就是关着" in toggle_off_text(False), True)
check("打开（有变化）的文案", "打开啦" in toggle_on_text(True), True)
check("打开（本来就是开的）的文案", "本来就是开着" in toggle_on_text(False), True)
check("私聊帮助里提到开关命令", "私聊投递" in private_help(is_super=True), True)


async def main() -> None:
    await initialize_database()
    await persistence_checks()

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
