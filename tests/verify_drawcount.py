"""「被抽到次数」与被抽到榜预留接口的回归测试。

要锁住的语义：
- 次数 = ``essence_draws`` 里该内容的行数。因为主键含 ``day`` 且当天已抽过的会被过滤，
  同一条内容每天最多被捞一次，所以这个数字实际等于"被捞到过多少天"；
- ``essence_draws`` 只保留 ``ESSENCE_DRAW_RETENTION_DAYS`` 天，超期行会被清掉，
  因此展示时必须写明"近 N 天"；
- ``label`` 是给「被抽到榜」用的人类可读名字：重复记录同一天只刷新 label、不动 drawn_at；
  取 label 时按**逻辑日期**优先而不是写入时刻（补写历史日期时二者会不一致）；
- 榜单接口已实现但**没有**挂任何命令，这条也要有断言守着，避免"预留"变成"悄悄上线"。
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import asyncio
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
DB = TEMP / "verify_drawcount.sqlite3"
for stale in TEMP.glob("verify_drawcount.sqlite3*"):
    stale.unlink()

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001",
    authorized_users="100000009",
    database_path=str(DB),
)

from PIL import Image  # noqa: E402

from plugins.group_bot.bottle import essence as gb  # noqa: E402
from plugins.group_bot.bottle import flow as gb_flow
from plugins.group_bot.bottle import render as gb_render
from plugins.group_bot.media.album import AlbumPhoto  # noqa: E402
from plugins.group_bot.core.config import GROUP_TIMEZONE  # noqa: E402
from plugins.group_bot.bottle.essence import (  # noqa: E402
    ESSENCE_DRAW_RETENTION_DAYS,
    ChosenBottle,
    DrawnContent,
    bottle_hint_text,
    content_label,
    draw_times,
    drawn_content_leaderboard,
    drawn_content_leaderboard_text,
    local_day_key,
    record_draw,
    send_bottle,
)
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402

GROUP = 123450001
failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


def day_offset(days: int) -> str:
    return (datetime.now(GROUP_TIMEZONE) - timedelta(days=days)).date().isoformat()


async def clear(key: str) -> None:
    async with connect() as db:
        await db.execute("DELETE FROM essence_draws WHERE message_id=?", (key,))
        await db.commit()


def essence_item(message_id: str = "900", text: str = "今天也要开心呀") -> dict:
    return {
        "message_id": message_id,
        "sender_id": "11",
        "sender_nick": "甲同学",
        "operator_id": "22",
        "operator_nick": "乙管理",
        "operator_time": 1700000000,
        "content": [{"type": "text", "data": {"text": text}}],
    }


def chosen_essence(message_id: str = "900", text: str = "今天也要开心呀") -> ChosenBottle:
    return ChosenBottle("essence", essence_item(message_id, text), [], 0, "ok", 1)


def chosen_album() -> ChosenBottle:
    photo = AlbumPhoto(
        media_id="m1",
        url="https://x.qpic.cn/a.jpg",
        album_id="A1",
        album_name="暑假照片",
        uploader_id="33",
        uploader_name="丙同学",
        upload_time=1700000000,
    )
    return ChosenBottle("album", photo, [], 0, "ok", 1)


async def check_counting() -> None:
    print("\n== 1. 次数统计 ==")
    key = "__count_test__"
    await clear(key)
    check("未知内容为 0", await draw_times(GROUP, key), 0)
    await record_draw(GROUP, key, day_offset(0), label="今天")
    check("今天一次 -> 1", await draw_times(GROUP, key), 1)
    await record_draw(GROUP, key, day_offset(0), label="今天再记一次")
    check("同一天重复记录不累加", await draw_times(GROUP, key), 1)
    await record_draw(GROUP, key, day_offset(1), label="昨天")
    check("跨天 -> 2（等于被捞到过 2 天）", await draw_times(GROUP, key), 2)
    await record_draw(GROUP, key, day_offset(ESSENCE_DRAW_RETENTION_DAYS + 3), label="过期")
    check(
        f"写入 {ESSENCE_DRAW_RETENTION_DAYS + 3} 天前的记录会被保留期清掉",
        await draw_times(GROUP, key),
        2,
    )
    await clear(key)

    print("\n== 2. label 落库与 upsert ==")
    key = "__label_test__"
    await clear(key)
    await record_draw(GROUP, key, day_offset(0), label="第一次的标签")
    async with connect() as db:
        cursor = await db.execute(
            "SELECT label, drawn_at FROM essence_draws WHERE group_id=? AND message_id=?",
            (str(GROUP), key),
        )
        row = await cursor.fetchone()
        first_at = row["drawn_at"]
    check("label 已写入", row["label"], "第一次的标签")
    await record_draw(GROUP, key, day_offset(0), label="第二次的标签")
    async with connect() as db:
        cursor = await db.execute(
            "SELECT label, drawn_at FROM essence_draws WHERE group_id=? AND message_id=?",
            (str(GROUP), key),
        )
        row = await cursor.fetchone()
    check("重复记录刷新 label", row["label"], "第二次的标签")
    check("但不改 drawn_at", row["drawn_at"], first_at)
    check("超长 label 被截断到 80 字", len((await _label_of(key)) or ""), 80)

    print("\n== 3. 榜单取 label 按逻辑日期优先 ==")
    await clear(key)
    await record_draw(GROUP, key, day_offset(0), label="TODAY")
    await record_draw(GROUP, key, day_offset(1), label="YESTERDAY")
    entries = [e for e in await drawn_content_leaderboard(GROUP, limit=100) if e.key == key]
    check("取日期更新那条的 label", entries[0].label if entries else None, "TODAY")
    await clear(key)


async def _label_of(key: str) -> str:
    long_label = "超" * 200
    await record_draw(GROUP, key, day_offset(0), label=long_label)
    async with connect() as db:
        cursor = await db.execute(
            "SELECT label FROM essence_draws WHERE group_id=? AND message_id=?",
            (str(GROUP), key),
        )
        row = await cursor.fetchone()
    return row["label"] if row else ""


async def check_leaderboard() -> None:
    print("\n== 4. 被抽到榜接口 ==")
    for key in ("__lb_a__", "__lb_b__", "album:__lb_c__"):
        await clear(key)
    await record_draw(GROUP, "__lb_a__", day_offset(0), label="内容 A")
    for offset in (0, 1, 2):
        await record_draw(GROUP, "__lb_b__", day_offset(offset), label="内容 B")
    await record_draw(GROUP, "album:__lb_c__", day_offset(0), label="相册照片 C")

    entries = [e for e in await drawn_content_leaderboard(GROUP, limit=100) if e.key.startswith("__lb") or e.key.startswith("album:__lb")]
    by_key = {e.key: e for e in entries}
    check("B 被抽到 3 次", by_key["__lb_b__"].times, 3)
    check("A 被抽到 1 次", by_key["__lb_a__"].times, 1)
    check("次数倒序：第一条是 B", entries[0].key, "__lb_b__")
    check("album 前缀被识别为相册", by_key["album:__lb_c__"].kind, "album")
    check("精华被识别为 essence", by_key["__lb_a__"].kind, "essence")
    check("label 随条目返回", by_key["__lb_a__"].label, "内容 A")
    check_true("last_drawn_at 非空", all(e.last_drawn_at for e in entries))
    check("limit 生效（只取 1 条）", len(await drawn_content_leaderboard(GROUP, limit=1)), 1)

    text = drawn_content_leaderboard_text(entries[:3])
    check_true("榜单文案有标题", "【被抽到榜】" in text)
    check_true("文案写明保留期", f"近 {ESSENCE_DRAW_RETENTION_DAYS} 天" in text)
    check_true("文案写明每天最多一次", "每天最多被捞到一次" in text)
    check_true("条目带次数", "—— 3 次" in text)
    check("空榜单也有文案", drawn_content_leaderboard_text([]), "本群还没有可统计的捞瓶记录～")

    for key in ("__lb_a__", "__lb_b__", "album:__lb_c__"):
        await clear(key)


def check_labels_and_text() -> None:
    print("\n== 5. content_label ==")
    label = content_label(chosen_essence(), GROUP)
    check_true("精华标签含昵称", label.startswith("甲同学："))
    check_true("精华标签含正文片段", "今天也要开心呀" in label)
    check_true("精华标签不超过 80 字", len(label) <= 80)
    long_label = content_label(chosen_essence(text="很长的正文" * 40), GROUP)
    check_true("超长正文被截断", len(long_label) <= 80)

    album_label = content_label(chosen_album(), GROUP)
    check_true("相册标签含相册名", "暑假照片" in album_label)
    check_true("相册标签含上传者", "丙同学" in album_label)

    print("\n== 6. 提示文案显示次数 ==")
    hint = bottle_hint_text(chosen_essence(), 5, 0, 3, drawn_times=2)
    check_true("提示里有次数", "2 次" in hint)
    check_true("提示写明含这次", "含这次" in hint)
    check_true("提示写明保留期", f"近 {ESSENCE_DRAW_RETENTION_DAYS} 天" in hint)
    hint_album = bottle_hint_text(chosen_album(), 5, 7, 3, drawn_times=4)
    check_true("相册提示也有次数", "4 次" in hint_album)
    no_count = bottle_hint_text(chosen_essence(), 5, 0, 3)
    check_true("未传次数时不出现该行", "被捞到过" not in no_count)


async def check_card_subtitle() -> None:
    print("\n== 7. 卡片副标题显示次数 ==")
    captured: dict[str, object] = {}
    real_renderer = gb.render_card_page

    def fake_renderer(title, subtitle, entries, *, group_id, directory=None):
        captured["title"] = title
        captured["subtitle"] = subtitle
        return TEMP / "fake-card.png"
    gb_render.render_card_page = fake_renderer
    try:
        gb.render_bottle_card(
            chosen_essence(), GROUP, "测试群", essence_total=5, album_total=0, drawn_times=3
        )
        check_true("副标题含次数", "已被捞 3 次" in str(captured["subtitle"]))
        captured.clear()
        gb.render_bottle_card(
            chosen_album(), GROUP, "测试群", essence_total=0, album_total=7, drawn_times=1
        )
        check_true("相册卡片也含次数", "已被捞 1 次" in str(captured["subtitle"]))
        captured.clear()
        gb.render_bottle_card(
            chosen_essence(), GROUP, "测试群", essence_total=5, album_total=0
        )
        check_true("不传次数时不出现", "已被捞" not in str(captured["subtitle"]))
    finally:
        gb_render.render_card_page = real_renderer


async def check_send_bottle_records_label() -> None:
    print("\n== 8. 抽取时把次数与 label 落到卡片/提示与数据库 ==")

    class SendBot:
        def __init__(self) -> None:
            self.messages: list = []

        async def send_group_msg(self, **kwargs):
            self.messages.append(kwargs["message"])
            return {"status": "ok"}

        @property
        def self_id(self):
            return "100000000"

    async def ok_draw(group_id, user_id):
        return gb.DrawVerdict("ok", 9, 0)

    async def fixed_choose(bot, group_id, *, day, album_only=False):
        return chosen_essence(message_id="__e2e__", text="端到端测试正文")

    async def fixed_sizes(bot, group_id):
        return (1, 0)

    def fake_renderer(chosen, group_id, group_name, *, essence_total, album_total, drawn_times=0):
        captured_times.append(drawn_times)
        path = TEMP / "e2e-card.png"
        Image.new("RGB", (40, 40), "white").save(path)
        return path

    await clear("__e2e__")
    captured_times: list[int] = []
    saved = (gb.claim_essence_draw, gb.choose_bottle, gb.pool_sizes, gb.render_bottle_card)
    gb_flow.claim_essence_draw = ok_draw
    gb_flow.choose_bottle = fixed_choose
    gb_flow.pool_sizes = fixed_sizes
    gb_flow.render_bottle_card = fake_renderer
    try:
        bot = SendBot()
        event = SimpleNamespace(group_id=GROUP, user_id=777777777, group_name="测试群")
        await send_bottle(bot, event)
        check("首次抽取时卡片拿到 drawn_times=1", captured_times, [1])
        check("数据库里已有 1 条记录", await draw_times(GROUP, "__e2e__"), 1)
        async with connect() as db:
            cursor = await db.execute(
                "SELECT label FROM essence_draws WHERE group_id=? AND message_id=?",
                (str(GROUP), "__e2e__"),
            )
            row = await cursor.fetchone()
        check_true("label 已落库", bool(row and row["label"]))
        check_true("label 含发送者昵称", "甲同学" in str(row["label"]))

        hint = [m for m in bot.messages if isinstance(m, str)]
        check_true("提示里出现了次数", any("1 次" in m for m in hint))

        # 同一天再抽一次同一条：次数不变（当天不重样由抓取层过滤，这里直接验证计数语义）
        captured_times.clear()
        bot2 = SendBot()
        await send_bottle(bot2, event)
        check("同一天再次抽到同一条时展示次数不虚增", captured_times, [1])
    finally:
        (
            gb.claim_essence_draw,
            gb.choose_bottle,
            gb.pool_sizes,
            gb.render_bottle_card,
        ) = saved
        await clear("__e2e__")


def check_interface_is_reserved() -> None:
    print("\n== 9. 榜单接口已挂成命令（接线正确）==")
    handlers_src = (Path(ROOT) / "plugins/group_bot/handlers.py").read_text(encoding="utf-8")
    essence_src = "\n".join(
        (Path(ROOT) / "plugins/group_bot" / name).read_text(encoding="utf-8")
        for name in (
            "bottle/pool.py",
            "bottle/stats.py",
            "bottle/render.py",
            "bottle/flow.py",
            "bottle/essence.py",
        )
    )

    check_true("handlers.py 里有「被抽到榜」命令词", "被抽到榜" in handlers_src)
    flow_src = (Path(ROOT) / "plugins/group_bot/bottle/flow.py").read_text(encoding="utf-8")
    check_true(
        "bottle_flow.py 里定义了 send_drawn_content_leaderboard",
        bool(re.search(r"^async def send_drawn_content_leaderboard\(", flow_src, re.M)),
    )
    check_true(
        "再导出层能拿到 send_drawn_content_leaderboard",
        callable(getattr(gb, "send_drawn_content_leaderboard", None)),
    )
    check_true(
        "bottle_flow.py 里定义了 safe_ 包装（异常不炸处理器）",
        bool(re.search(r"^async def safe_send_drawn_content_leaderboard\(", flow_src, re.M)),
    )
    check_true(
        "榜单数据函数存在",
        bool(re.search(r"async def drawn_content_leaderboard\(", essence_src)),
    )
    check_true(
        "榜单卡片渲染函数存在",
        bool(re.search(r"def render_drawn_leaderboard_card\(", essence_src)),
    )


async def check_command_sends_image() -> None:
    print("\n== 10. 命令返回图片（TOP10）==")
    from plugins.group_bot import handlers
    from plugins.group_bot.bottle.essence import (
        send_drawn_content_leaderboard,
        render_drawn_leaderboard_card,
    )

    for word in ("被抽到榜", "被捞榜", "抽到榜"):
        check_true(f"{word} 被识别为命令", handlers.is_recognized_command(word, word))
    check_true("help 里有它", "被抽到榜" in handlers.command_help(is_admin=False))
    check("命令词集合", sorted(handlers.DRAWN_RANK_COMMANDS), ["抽到榜", "被抽到榜", "被捞榜"])

    class SendBot:
        def __init__(self) -> None:
            self.messages: list = []

        async def send_group_msg(self, **kwargs):
            self.messages.append(kwargs["message"])
            return {"status": "ok"}

        async def call_api(self, *args, **kwargs):
            return {"group_name": "测试群"}

    event = SimpleNamespace(group_id=GROUP, group_name="测试群")

    print("\n    -- 有记录：应发图片 --")
    for key in ("__rank_a__", "__rank_b__"):
        await clear(key)
    await record_draw(GROUP, "__rank_a__", day_offset(0), label="最常被捞的内容")
    for offset in (0, 1, 2):
        await record_draw(GROUP, "__rank_b__", day_offset(offset), label="第二常被捞的")
    try:
        bot = SendBot()
        await send_drawn_content_leaderboard(bot, event)
        check("只发一条消息", len(bot.messages), 1)
        sent = bot.messages[0]
        check("发的是图片段", getattr(sent, "type", None), "image")
        check_true("图片是 file:// URI", str(sent.data.get("file", "")).startswith("file://"))
        check_true(
            "发送后临时卡片已删除",
            not Path(str(sent.data["file"]).replace("file:///", "").replace("/", "\\")).exists(),
        )
    finally:
        await clear("__rank_a__")
        await clear("__rank_b__")

    print("\n    -- 无记录：应回文字提示而不是空图 --")
    async with connect() as db:
        await db.execute("DELETE FROM essence_draws WHERE group_id=?", (str(GROUP),))
        await db.commit()
    bot = SendBot()
    await send_drawn_content_leaderboard(bot, event)
    check("无记录也只发一条", len(bot.messages), 1)
    check_true("无记录时是文字", isinstance(bot.messages[0], str))
    check_true("文字说明了没有记录", "还没有捞瓶记录" in str(bot.messages[0]))

    print("\n    -- 卡片内容：名次与次数 --")
    captured: dict[str, object] = {}
    real_page = gb.render_card_page

    def fake_page(title, subtitle, entries, *, group_id, directory=None, thumb_max=None):
        captured["title"] = title
        captured["subtitle"] = subtitle
        captured["entries"] = list(entries)
        captured["thumb_max"] = thumb_max
        path = TEMP / "rank-card.png"
        Image.new("RGB", (40, 40), "white").save(path)
        return path

    print("\n    -- 内容回查：键 → 现行精华/相册内容 --")
    from plugins.group_bot.bottle.essence import (
        DrawnContentCard,
        LEADERBOARD_IMAGE_BUDGET,
        resolve_drawn_content,
    )

    thumb = TEMP / "rank-thumb.png"
    Image.new("RGB", (120, 80), (80, 120, 160)).save(thumb)

    async def fake_essence_list(bot, group_id):
        return [
            {
                "message_id": "live-1",
                "sender_nick": "小明",
                "operator_nick": "管理员",
                "operator_time": 1759300000,
                "content": [
                    {"type": "text", "data": {"text": "一起去爬山吧"}},
                    {"type": "image", "data": {"url": "https://multimedia.nt.qq.com.cn/x.jpg"}},
                ],
            }
        ]

    async def fake_album_photos(bot, group_id):
        return [
            AlbumPhoto(
                media_id="live-photo",
                url="https://x.qpic.cn/a.jpg",
                album_name="团建照片",
                uploader_id="33",
                uploader_name="小美",
                upload_time=1759330000,
            )
        ]

    async def fake_images(bot, group_id, item):
        return [thumb, thumb]

    async def fake_photo(record, group_id, client):
        return thumb

    saved = (gb_render.fetch_essence_list, gb_render.fetch_album_photos,
             gb_render.materialize_images, gb_render.materialize_photo)
    gb_render.fetch_essence_list = fake_essence_list
    gb_render.fetch_album_photos = fake_album_photos
    gb_render.materialize_images = fake_images
    gb_render.materialize_photo = fake_photo
    try:
        entries = [
            DrawnContent(key="live-1", kind="essence", times=6, label="小明：一起去爬山吧", last_drawn_at="2026-10-17T04:00:00+00:00"),
            DrawnContent(key="album:live-photo", kind="album", times=3, label="", last_drawn_at="2026-10-16T04:00:00+00:00"),
            DrawnContent(key="gone-9", kind="essence", times=1, label="甲：已经不在精华里的内容", last_drawn_at=""),
        ]
        cards = await resolve_drawn_content(None, GROUP, entries, image_budget=LEADERBOARD_IMAGE_BUDGET)
        first, second, third = cards
        check("精华行用发送者昵称", first.name, "小明")
        check_true("精华行来源写了操作人", "由 管理员 设为精华" in first.source)
        check("精华行显示正文", first.text, "一起去爬山吧")
        check_true("精华行有图", first.image is not None)
        check("多出来的图记成「另有」", first.extra_images, 1)
        check("相册行用上传者", second.name, "小美")
        check_true("相册行来源写了相册名", "团建照片" in second.source)
        check_true("相册行有图", second.image is not None)
        check("原消息不在池子里 → missing", third.missing, True)
        check("缺失时退回 label 当名字", third.name, "甲：已经不在精华里的内容")
        check("缺失时说明原因", third.source, "原消息已不在精华列表里")
        check("缺失时没有图", third.image, None)

        print("\n    -- 图片预算：超出预算的行不下图 --")
        many = [
            DrawnContent(key="live-1", kind="essence", times=9, label="x", last_drawn_at=""),
            DrawnContent(key="live-1", kind="essence", times=8, label="x", last_drawn_at=""),
            DrawnContent(key="live-1", kind="essence", times=7, label="x", last_drawn_at=""),
        ]
        budget_cards = await resolve_drawn_content(None, GROUP, many, image_budget=1)
        check("只有第一行拿到图", sum(1 for c in budget_cards if c.image), 1)

        print("\n    -- 渲染：用回查后的卡片 --")
        rows = [
            DrawnContentCard(
                DrawnContent(key="live-1", kind="essence", times=6, label="", last_drawn_at="2026-10-17T04:00:00+00:00"),
                1, "小明", "精华 · 由 管理员 设为精华", "一起去爬山吧", None, 2,
            ),
            DrawnContentCard(
                DrawnContent(key="album:live-photo", kind="album", times=3, label="", last_drawn_at=""),
                2, "小美", "群相册《团建照片》", "", None,
            ),
        ]
        gb_render.render_card_page = fake_page
        try:
            path = render_drawn_leaderboard_card(rows, GROUP, "测试群")
            check("标题", captured["title"], "被抽到榜")
            check_true("副标题写了保留期", "近 30 天" in str(captured["subtitle"]))
            check_true("副标题写了 TOP 数", "TOP 2" in str(captured["subtitle"]))
            check("渲染时收小缩略图", captured.get("thumb_max"), 260)
            first_row, second_row = captured["entries"]
            check("名字是发送者", first_row.name, "小明")
            check("名次与次数在右侧", first_row.right_text, "第 1 名  ·  6 次")
            check_true("元信息含来源与时间", "精华" in first_row.meta_text and "上次被捞到" in first_row.meta_text)
            check("正文进气泡", first_row.text, "一起去爬山吧")
            check("另有图片数传进卡片", first_row.extra_images, 2)
            check("相册行没有正文", second_row.text, "")
            check("没有时间就不写该行", second_row.meta_text, "群相册《团建照片》")
            path.unlink(missing_ok=True)
        finally:
            gb_render.render_card_page = real_page
    finally:
        (gb_render.fetch_essence_list, gb.fetch_album_photos, gb.materialize_images, gb.materialize_photo) = saved


async def main() -> None:
    await initialize_database()
    await check_counting()
    await check_leaderboard()
    check_labels_and_text()
    await check_card_subtitle()
    await send_bottle_records_label_helper()

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


async def send_bottle_records_label_helper() -> None:
    await check_send_bottle_records_label()
    check_interface_is_reserved()
    await check_command_sends_image()


asyncio.run(main())
