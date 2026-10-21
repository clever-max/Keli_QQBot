"""Checks for the 漂流瓶 revamp: two pools, defensive album parsing, new board.

The album response shape is undeclared in NapCat, so the parser is exercised
against several plausible shapes plus junk — that is where it can silently break
once the real payload is known.
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
DB = TEMP / "verify_bottle.sqlite3"
for stale in TEMP.glob("verify_bottle.sqlite3*"):
    stale.unlink()

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001,123450002",
    authorized_users="100000009",
    database_path=str(DB),
)

from plugins.group_bot.media import album as gb_album  # noqa: E402
from plugins.group_bot.bottle import flow as gb_flow
from plugins.group_bot.bottle import pool as gb_pool
from plugins.group_bot.bottle import essence as gb_essence  # noqa: E402
from plugins.group_bot.core import state as gb_state  # noqa: E402
from plugins.group_bot.media.album import (  # noqa: E402
    AlbumPhoto,
    AlbumUnavailable,
    parse_media_item,
)
from plugins.group_bot.core.config import ALLOWED_GROUP_IDS  # noqa: E402
from plugins.group_bot.bottle.essence import (  # noqa: E402
    ChosenBottle,
    _album_key,
    album_disabled_text,
    album_empty_text,
    album_exhausted_text,
    album_no_showable_text,
    choose_bottle,
    empty_bottle_text,
    kind_of_drawn_key,
    leaderboard_text,
    local_day_key,
    pool_exhausted_text,
    pool_sizes,
    preferred_kind_order,
    recent_drawn_kinds,
    send_bottle,
)
from plugins.group_bot.handlers import (  # noqa: E402
    BOTTLE_COMMANDS,
    BOTTLE_RANK_COMMANDS,
    is_recognized_command,
    parse_bottle_command,
)
from plugins.group_bot.core.store import connect, initialize_database  # noqa: E402

GROUP = 123450001
failures: list[str] = []


def bottle_event(user_id: int, group_id: int = GROUP):
    """send_bottle 只读 group_id / user_id / group_name，别处不需要真事件。"""
    return SimpleNamespace(group_id=group_id, user_id=user_id, group_name="测试群")


# 记下未被替身覆盖的真实实现，供需要走数据库的用例恢复。
REAL_DRAWN_MESSAGE_IDS = gb_essence.drawn_message_ids


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


print("== 1. 相册媒体解析：多种可能的字段形状 ==")
shapes = [
    (
        "NapCat 文档示例",
        {"media_id": "m1", "url": "https://x.qpic.cn/a.jpg"},
        "m1", "https://x.qpic.cn/a.jpg", "", "",
    ),
    (
        "驼峰 + 上传者",
        {
            "lloc": "lloc-1",
            "originUrl": "https://p.qpic.cn/b.png",
            "uploaderUin": "12345",
            "uploaderNick": "小明",
            "uploadTime": 1734567890,
        },
        "lloc-1", "https://p.qpic.cn/b.png", "12345", "小明",
    ),
    (
        "嵌套 image.url",
        {"id": "m3", "image": {"url": "https://o.qpic.cn/c.jpg"}},
        "m3", "https://o.qpic.cn/c.jpg", "", "",
    ),
    (
        "原始键优先于缩略图",
        {"media_id": "m4", "origin_url": "https://a/q.jpg", "thumb_url": "https://a/t.jpg"},
        "m4", "https://a/q.jpg", "", "",
    ),
    (
        "只有无扩展名的 CDN 链接（靠兜底扫描）",
        {"lloc": "m5", "somethingElse": "https://multimedia.nt.qq.com.cn/download?fileid=abc"},
        "m5", "https://multimedia.nt.qq.com.cn/download?fileid=abc", "", "",
    ),
]
for label, raw, want_id, want_url, want_uin, want_nick in shapes:
    photo = parse_media_item(raw, "album-1", "相册一")
    if photo is None:
        failures.append(f"{label}: parsed to None")
        print(f"FAIL {label}: parsed to None")
        continue
    check(f"{label} media_id", photo.media_id, want_id)
    check(f"{label} url", photo.url, want_url)
    check(f"{label} uploader_id", photo.uploader_id, want_uin)
    check(f"{label} uploader_name", photo.uploader_name, want_nick)
    check(f"{label} 带上相册信息", (photo.album_id, photo.album_name), ("album-1", "相册一"))

print("\n== 2. 解析的拒绝路径 ==")
check("没有 URL -> None", parse_media_item({"media_id": "x", "text": "hi"}, "a", "A"), None)
check("非 dict -> None", parse_media_item("nope", "a", "A"), None)
check("空 dict -> None", parse_media_item({}, "a", "A"), None)
photo = parse_media_item({"url": "https://x.qpic.cn/z.jpg"}, "a", "A")
check("缺 media_id 时用 URL 稳定散列", photo.media_id.startswith("url:"), True)
same = parse_media_item({"url": "https://x.qpic.cn/z.jpg"}, "b", "B")
check("同一 URL 的散列稳定", photo.media_id, same.media_id)

print("\n== 3. 列表/分页字段的容错 ==")
check(
    "_as_list: media_list",
    len(gb_album._as_list({"media_list": [1, 2]}, ("media_list",))), 2,
)
check(
    "_as_list: data.mediaList",
    len(gb_album._as_list({"data": {"mediaList": [1, 2, 3]}}, ("mediaList",))), 3,
)
check("_as_list: 裸列表", len(gb_album._as_list([1], ("media_list",))), 1)
check("_as_list: 认不出来时为空", gb_album._as_list({"nope": 1}, ("media_list",)), [])
check(
    "_next_attach_info: 取到",
    gb_album._next_attach_info({"data": {"attach_info": "next"}}), "next",
)
check("_next_attach_info: 没有则为空", gb_album._next_attach_info({"data": {}}), "")


class AlbumBot:
    """Stub OneBot that serves scripted album responses."""

    def __init__(self, album_pages, media_pages):
        self.album_pages = list(album_pages)
        self.media_pages = dict(media_pages)
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, api, **kwargs):
        self.calls.append((api, kwargs))
        if api == "get_qun_album_list":
            index = kwargs.get("attach_info") or "0"
            return self.album_pages[int(index)]
        if api == "get_group_album_media_list":
            index = f"{kwargs['album_id']}:{kwargs.get('attach_info') or '0'}"
            result = self.media_pages.get(index)
            if result is None:
                raise RuntimeError("album not readable")
            return result
        raise AssertionError(f"unexpected api {api}")


async def album_checks() -> None:
    print("\n== 4. 多相册 + 分页 + 坏相册容错 + 去重 ==")
    bot = AlbumBot(
        album_pages=[
            {
                "album_list": [
                    {"album_id": "A1", "album_name": "活动"},
                    {"album_id": "A2", "album_name": "日常"},
                ],
                "attach_info": "",
                "has_more": False,
            }
        ],
        media_pages={
            # A1 第一页有两条（其中一条重复 media_id），还要翻第二页
            "A1:0": {
                "media_list": [
                    {"media_id": "p1", "url": "https://x.qpic.cn/1.jpg", "uin": "11", "nick": "甲"},
                    {"media_id": "p1", "url": "https://x.qpic.cn/1.jpg", "uin": "11", "nick": "甲"},
                    {"media_id": "p2", "url": "https://x.qpic.cn/2.jpg", "uin": "12", "nick": "乙"},
                ],
                "attach_info": "n1",
            },
            "A1:n1": {
                "media_list": [
                    {"media_id": "p3", "url": "https://x.qpic.cn/3.jpg", "uin": "13", "nick": "丙"}
                ],
                "attach_info": "",
            },
            # A2 读不到 -> 应被跳过而不是整次失败
        },
    )
    gb_state._album_cache.clear()
    photos = await gb_album.fetch_album_photos(bot, GROUP)
    check("拿到 3 张（去重后）", sorted(p.media_id for p in photos), ["p1", "p2", "p3"])
    check("p1 只出现一次", sum(1 for p in photos if p.media_id == "p1"), 1)
    check("上传者被解析", next(p for p in photos if p.media_id == "p3").uploader_name, "丙")
    # p3 来自 A1 的第二页，所以它属于「活动」这个相册。
    check("相册名带上", next(p for p in photos if p.media_id == "p3").album_name, "活动")
    check(
        "翻页用了 attach_info",
        any(kw.get("attach_info") == "n1" for api, kw in bot.calls if api == "get_group_album_media_list"),
        True,
    )

    print("\n== 5. 相册列表整体失败 -> AlbumUnavailable ==")
    class DeadBot:
        async def call_api(self, api, **kwargs):
            raise RuntimeError("unsupported action")

    gb_state._album_cache.clear()
    try:
        await gb_album.fetch_album_photos(DeadBot(), GROUP)
    except AlbumUnavailable:
        print("ok   抛出 AlbumUnavailable")
    else:
        failures.append("相册失败没有抛 AlbumUnavailable")
        print("FAIL 相册失败没有抛 AlbumUnavailable")

    print("\n== 6. 缓存生效（同一群人只走一次 API）==")
    bot = AlbumBot(
        album_pages=[{"album_list": [{"album_id": "A1", "album_name": "x"}], "attach_info": ""}],
        media_pages={"A1:0": {"media_list": [{"media_id": "q1", "url": "https://x.qpic.cn/q.jpg"}]}},
    )
    gb_state._album_cache.clear()
    await gb_album.fetch_album_photos(bot, GROUP)
    first_calls = len(bot.calls)
    await gb_album.fetch_album_photos(bot, GROUP)
    check("第二次走缓存", len(bot.calls), first_calls)


async def choice_checks() -> None:
    print("\n== 7. 双池抽取与降级回退 ==")
    day = local_day_key()
    fake = TEMP / "fake.jpg"
    fake.write_bytes(b"x")

    async def no_drawn(group_id, day_key):
        return set()

    async def fake_materialize_essence(bot, group_id, item):
        return [fake]

    async def fake_materialize_photo(photo, group_id, client):
        return fake
    gb_pool.drawn_message_ids = no_drawn
    gb_pool.materialize_images = fake_materialize_essence
    gb_pool.materialize_photo = fake_materialize_photo

    essence_item = {
        "message_id": 555,
        "sender_id": "11",
        "sender_nick": "甲",
        "operator_id": "22",
        "operator_nick": "乙",
        "operator_time": 1734567890,
        "content": [{"type": "text", "data": {"text": "hello"}}],
    }
    photo = AlbumPhoto(
        media_id="m1", url="https://x.qpic.cn/a.jpg",
        album_id="A1", album_name="活动", uploader_id="33", uploader_name="丙",
        upload_time=1734567890,
    )

    async def essence_only(bot, group_id):
        return [essence_item]

    async def no_photos(bot, group_id):
        return []

    async def photos_only(bot, group_id):
        return [photo]

    async def no_essence(bot, group_id):
        return []

    # share=100 -> 首选相册
    gb_pool.BOTTLE_ALBUM_SHARE = 100
    gb_pool.BOTTLE_ALBUM_ENABLED = True
    gb_pool.fetch_essence_list = essence_only
    gb_pool.fetch_album_photos = photos_only
    gb_essence._album_broken.clear()
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=day)
    check("share=100 时抽到相册照片", (chosen.kind, chosen.item.media_id), ("album", "m1"))
    check("相册照片的图片已物化", chosen.image_paths, [fake])

    # 相册为空 -> 回退到精华
    gb_pool.fetch_album_photos = no_photos
    gb_essence._essence_broken.clear()
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=day)
    check("相册为空时回退到精华", (chosen.kind, chosen.item["message_id"]), ("essence", 555))

    # share=0 -> 只抽精华，即使相册有照片
    gb_pool.BOTTLE_ALBUM_SHARE = 0
    gb_pool.fetch_album_photos = photos_only
    gb_essence._essence_broken.clear()
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=day)
    check("share=0 时只抽精华", chosen.kind, "essence")

    # 精华池为空且相册关闭 -> 明确报 empty
    gb_pool.BOTTLE_ALBUM_ENABLED = False
    gb_pool.fetch_essence_list = no_essence
    gb_essence._essence_broken.clear()
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=day)
    check("两个池都空 -> item 为 None", chosen.item, None)
    check("原因归为 exhausted", chosen.reason, "exhausted")

    print("\n== 8. pool_sizes 与读取失败降级 ==")
    gb_pool.BOTTLE_ALBUM_ENABLED = True
    gb_pool.BOTTLE_ALBUM_SHARE = 50  # 上一节留在 0，会（按设计）把相册算作关闭
    gb_pool.fetch_essence_list = essence_only
    gb_pool.fetch_album_photos = photos_only
    check("两边都读得到", await pool_sizes(SimpleNamespace(), GROUP), (1, 1))

    async def boom_essence(bot, group_id):
        raise gb_essence.EssenceUnavailable("nope")

    async def boom_album(bot, group_id):
        raise AlbumUnavailable("nope")
    gb_pool.fetch_essence_list = boom_essence
    gb_pool.fetch_album_photos = boom_album
    check("都读不到时记 0", await pool_sizes(SimpleNamespace(), GROUP), (0, 0))
    gb_pool.BOTTLE_ALBUM_ENABLED = False
    gb_pool.fetch_essence_list = essence_only
    check("相册关掉后不计相册", await pool_sizes(SimpleNamespace(), GROUP), (1, 0))
    gb_pool.BOTTLE_ALBUM_ENABLED = True


def leaderboard_checks() -> None:
    print("\n== 9. 丢漂流瓶榜：上传相册 + 设精华合并 ==")
    items = [
        {"sender_id": "11", "sender_nick": "甲", "operator_id": "22", "operator_nick": "乙"},
        {"sender_id": "11", "sender_nick": "甲", "operator_id": "22", "operator_nick": "乙"},
        {"sender_id": "33", "sender_nick": "丙", "operator_id": "11", "operator_nick": "甲"},
    ]
    photos = [
        AlbumPhoto(media_id="a", url="u", uploader_id="22", uploader_name="乙"),
        AlbumPhoto(media_id="b", url="u", uploader_id="22", uploader_name="乙"),
        AlbumPhoto(media_id="c", url="u"),  # 没有上传者
    ]
    text = leaderboard_text(items, photos)
    check("标题是丢漂流瓶榜", "【丢漂流瓶榜】" in text, True)
    check("两个分段都在", "【被丢进瓶子里最多的人】" in text and "【最爱丢漂流瓶的人】" in text, True)
    check("说明是合并计算", "合并计算" in text, True)
    check("总数为 3 精华 + 3 照片", "一共 6 个" in text, True)
    # 被丢进瓶子：甲 2 条精华（两条都是他说的）+ 丙 1 条 = 3 条精华；乙 2 张照片
    check("甲被丢进瓶子 2 个", "甲 —— 2 个" in text, True)
    check("丙被丢进瓶子 1 个", "丙 —— 1 个" in text, True)
    check("乙被丢进瓶子 2 个", "乙 —— 2 个" in text, True)
    # 丢瓶子：乙 2 次设精华 + 2 张上传 = 4；甲 1 次设精华 = 1
    check("乙丢瓶子合并为 4 个", "乙 —— 4 个" in text, True)
    check("认不出上传者的照片单独说明", "认不出上传者" in text, True)
    check("无群号", not any(str(g) in text for g in ALLOWED_GROUP_IDS), True)

    check("两个池都空时给出空提示", leaderboard_text([], []), empty_bottle_text())
    only_photos = leaderboard_text([], photos[:1])
    check("只有相册也能出榜", "【丢漂流瓶榜】" in only_photos, True)


def text_checks() -> None:
    print("\n== 10. 文案 ==")
    for label, text in (
        ("空提示", empty_bottle_text()),
        ("池子捞完", pool_exhausted_text(3, 4)),
    ):
        check(f"{label} 无群号", not any(str(g) in text for g in ALLOWED_GROUP_IDS), True)
        check(f"{label} 提到漂流瓶", "漂流瓶" in text, True)
    check("捞完时列出两个池子的量", "精华 3 条 + 相册照片 4 张" in pool_exhausted_text(3, 4), True)

    print("\n== 11. 命令名 ==")
    for name in ("漂流瓶", "捞漂流瓶", "捡漂流瓶", "来一个漂流瓶"):
        check(f"绘制命令 {name}", name in BOTTLE_COMMANDS and is_recognized_command(name, name), True)
    for name in ("丢漂流瓶榜", "漂流瓶榜", "捞漂流瓶榜"):
        check(f"榜单命令 {name}", name in BOTTLE_RANK_COMMANDS and is_recognized_command(name, name), True)

    print("\n== 11b. 旧「精华」系指令已删除（应不可识别 → 静默）==")
    for name in ("精华", "抽精华", "来一条精华", "群精华"):
        check(f"旧绘制命令 {name} 已移除", name in BOTTLE_COMMANDS, False)
        check(f"旧绘制命令 {name} 不被识别", is_recognized_command(name, name), False)
    for name in ("精华榜", "群精华榜", "精华排行"):
        check(f"旧榜单命令 {name} 已移除", name in BOTTLE_RANK_COMMANDS, False)
        check(f"旧榜单命令 {name} 不被识别", is_recognized_command(name, name), False)

    print("\n== 12. 相册键加前缀，不会和精华 message_id 撞车 ==")
    check(
        "相册键有前缀",
        _album_key(AlbumPhoto(media_id="123", url="u")).startswith("album:"),
        True,
    )
    check("还原类别：相册", kind_of_drawn_key(_album_key(AlbumPhoto(media_id="9", url="u"))), "album")
    check("还原类别：精华", kind_of_drawn_key("12345"), "essence")


async def streak_checks() -> None:
    print("\n== 13. 连续同类判定（纯函数）==")
    gb_pool.BOTTLE_SWITCH_STREAK = 3
    check("空记录不强制", preferred_kind_order([]), None)
    check("不足三次不强制", preferred_kind_order(["essence", "essence"]), None)
    check("三次精华 -> 先相册", preferred_kind_order(["essence", "essence", "essence"]), ["album", "essence"])
    check("三次相册 -> 先精华", preferred_kind_order(["album", "album", "album"]), ["essence", "album"])
    check(
        "三次里有杂类不强制",
        preferred_kind_order(["essence", "album", "essence"]),
        None,
    )
    check(
        "只看最近三次（更早的不影响）",
        preferred_kind_order(["essence", "essence", "essence", "album", "album"]),
        ["album", "essence"],
    )
    gb_pool.BOTTLE_SWITCH_STREAK = 2
    check("阈值可配为 2", preferred_kind_order(["album", "album"]), ["essence", "album"])
    gb_pool.BOTTLE_SWITCH_STREAK = 0
    check("阈值 0 = 关闭补偿", preferred_kind_order(["essence"] * 5), None)
    gb_pool.BOTTLE_SWITCH_STREAK = 3

    print("\n== 14. 从数据库读出最近类别（跨重启成立）==")
    async with connect() as db:
        await db.execute("DELETE FROM essence_draws WHERE group_id=?", (str(GROUP),))
        await db.commit()
    check("没有记录时为空", await recent_drawn_kinds(GROUP, 3), [])

    # 按时间顺序写入：精华、精华、精华（drawn_at 递增）
    base = datetime.now(timezone.utc)
    async with connect() as db:
        for index, key in enumerate(("101", "102", "103")):
            await db.execute(
                """INSERT OR REPLACE INTO essence_draws(group_id, day, message_id, drawn_at)
                   VALUES (?,?,?,?)""",
                (
                    str(GROUP),
                    local_day_key(),
                    key,
                    (base + timedelta(seconds=index)).isoformat(),
                ),
            )
        await db.commit()
    check("最近三次都是精华", await recent_drawn_kinds(GROUP, 3), ["essence"] * 3)
    check("顺序是新的在前", await recent_drawn_kinds(GROUP, 1), ["essence"])

    async with connect() as db:
        await db.execute(
            """INSERT OR REPLACE INTO essence_draws(group_id, day, message_id, drawn_at)
               VALUES (?,?,?,?)""",
            (str(GROUP), local_day_key(), "album:m9", (base + timedelta(seconds=9)).isoformat()),
        )
        await db.commit()
    check("新捞的相册照片排在最前", await recent_drawn_kinds(GROUP, 2), ["album", "essence"])
    check("最近三次变成混合 -> 不强制", preferred_kind_order(await recent_drawn_kinds(GROUP, 3)), None)

    print("\n== 15. choose_bottle 执行强制切换 ==")
    fake = TEMP / "fake2.jpg"
    fake.write_bytes(b"x")

    async def no_drawn(group_id, day_key):
        return set()

    async def fake_materialize_essence(bot, group_id, item):
        return [fake]

    async def fake_materialize_photo(photo, group_id, client):
        return fake
    gb_pool.drawn_message_ids = no_drawn
    gb_pool.materialize_images = fake_materialize_essence
    gb_pool.materialize_photo = fake_materialize_photo
    gb_pool.BOTTLE_ALBUM_ENABLED = True
    gb_essence._essence_broken.clear()
    gb_state._album_broken.clear()

    essence_item = {
        "message_id": 777,
        "sender_id": "11",
        "sender_nick": "甲",
        "operator_id": "22",
        "operator_nick": "乙",
        "operator_time": 1734567890,
        "content": [{"type": "text", "data": {"text": "hi"}}],
    }
    photo = AlbumPhoto(
        media_id="s1", url="https://x.qpic.cn/a.jpg",
        album_id="A1", album_name="活动", uploader_id="33", uploader_name="丙",
    )

    async def essence_only(bot, group_id):
        return [essence_item]

    async def photos_only(bot, group_id):
        return [photo]

    async def no_photos(bot, group_id):
        return []

    # 把 random 换成可控版本：randrange 固定返回上界-1（"不落进 share 区间"），
    # 这样"随机本来会选精华"是确定的，补偿是否生效才有判别力。
    real_random = gb_pool.random

    class FakeRandom:
        def __init__(self, high: bool):
            self.high = high

        def randrange(self, n):
            return n - 1 if self.high else 0

        shuffle = staticmethod(real_random.shuffle)

    async def seed_streak(keys):
        async with connect() as db:
            await db.execute("DELETE FROM essence_draws WHERE group_id=?", (str(GROUP),))
            for index, key in enumerate(keys):
                await db.execute(
                    """INSERT OR REPLACE INTO essence_draws(group_id, day, message_id, drawn_at)
                       VALUES (?,?,?,?)""",
                    (str(GROUP), local_day_key(), key, (base + timedelta(seconds=index)).isoformat()),
                )
            await db.commit()
    gb_pool.fetch_essence_list = essence_only
    gb_pool.fetch_album_photos = photos_only

    # 连续三次精华 + 随机本来会选精华（share=50 且 randrange=99 -> 不落区间）
    await seed_streak(("201", "202", "203"))
    gb_pool.BOTTLE_ALBUM_SHARE = 50
    gb_pool.random = FakeRandom(high=True)
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=local_day_key())
    check("连续三次精华后强制捞到相册", (chosen.kind, chosen.item.media_id), ("album", "s1"))

    # 连续三次相册 + 随机本来会选相册（share=50 且 randrange=0 -> 落区间）
    await seed_streak(("album:a1", "album:a2", "album:a3"))
    gb_pool.random = FakeRandom(high=False)
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=local_day_key())
    check("连续三次相册后强制捞到精华", (chosen.kind, chosen.item["message_id"]), ("essence", 777))

    # 只有连续两次时不该补偿，仍按随机走
    await seed_streak(("album:a1", "album:a2"))
    gb_pool.random = FakeRandom(high=False)
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=local_day_key())
    check("连续两次不触发补偿", chosen.kind, "album")
    gb_pool.random = real_random

    # 被强制的那一类空着 -> 回退到原来那类，而不是空手而归
    await seed_streak(("album:a1", "album:a2", "album:a3"))
    gb_pool.BOTTLE_ALBUM_SHARE = 100
    gb_pool.fetch_album_photos = no_photos
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=local_day_key())
    check("强制目标为空时回退到精华", chosen.kind, "essence")

    # 配置优先：相册被关掉（开关或占比）时补偿不生效
    gb_pool.fetch_album_photos = photos_only
    gb_pool.BOTTLE_ALBUM_ENABLED = False
    await seed_streak(("201", "202", "203"))
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=local_day_key())
    check("相册开关=0 时只可能抽精华", chosen.kind, "essence")
    gb_pool.BOTTLE_ALBUM_ENABLED = True
    gb_pool.BOTTLE_ALBUM_SHARE = 0
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=local_day_key())
    check("相册占比=0 时补偿不生效", chosen.kind, "essence")
    gb_pool.BOTTLE_ALBUM_SHARE = 50

    print("\n== 16. `漂流瓶 相册`：只从群相册随机 ==")
    for text, want in (
        ("漂流瓶 相册", True),
        ("捞漂流瓶 相册", True),
        ("捡漂流瓶 群相册", True),
        ("来一个漂流瓶 照片", True),
        ("漂流瓶 相片", True),
        ("漂流瓶", False),
        ("捞漂流瓶", False),
        ("漂流瓶 精华", None),
        ("漂流瓶 相册 20", None),
        ("漂流瓶x", None),
        ("精华", None),
        ("", None),
    ):
        check(f"parse({text!r})", parse_bottle_command(text), want)
    check("相册形式可被识别", is_recognized_command("漂流瓶 相册", "漂流瓶 相册"), True)

    # 显式要相册时忽略比例：随机本来会选精华，仍然给相册
    gb_pool.fetch_essence_list = essence_only
    gb_pool.fetch_album_photos = photos_only
    gb_pool.BOTTLE_ALBUM_SHARE = 100
    await seed_streak(())  # 清空连续记录
    gb_pool.random = FakeRandom(high=True)  # share=100 且高端 -> 会选精华
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=local_day_key(), album_only=True)
    check("显式相册忽略随机比例", (chosen.kind, chosen.item.media_id), ("album", "s1"))

    # 相册为空时也不回退到精华
    gb_pool.fetch_album_photos = no_photos
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=local_day_key(), album_only=True)
    check("相册为空时不回退到精华", (chosen.kind, chosen.item), ("album", None))

    # 相册全被标记为坏图时同样不回退（坏图集合用的是带前缀的键）
    gb_state._album_broken.setdefault(GROUP, set()).add(_album_key(photo))
    gb_pool.fetch_album_photos = photos_only
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=local_day_key(), album_only=True)
    check("相册全坏时不回退到精华", (chosen.kind, chosen.item), ("album", None))
    gb_state._album_broken.clear()

    # 配置优先：相册被关掉时显式命令也不硬抽
    gb_pool.BOTTLE_ALBUM_SHARE = 0
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=local_day_key(), album_only=True)
    check("相册占比=0 时显式命令也不抽", (chosen.kind, chosen.item, chosen.reason), ("album", None, "unavailable"))
    gb_pool.BOTTLE_ALBUM_SHARE = 50
    gb_pool.BOTTLE_ALBUM_ENABLED = False
    chosen = await choose_bottle(SimpleNamespace(), GROUP, day=local_day_key(), album_only=True)
    check("相册开关=0 时显式命令也不抽", (chosen.kind, chosen.item), ("album", None))
    gb_pool.BOTTLE_ALBUM_ENABLED = True
    gb_pool.random = real_random

    print("\n== 17. `漂流瓶 相册` 的失败提示与退次数 ==")

    class SendBot:
        def __init__(self):
            self.messages: list[str] = []

        async def send_group_msg(self, **kwargs):
            self.messages.append(kwargs["message"])
            return {"status": "ok"}

        @property
        def self_id(self):
            return "100000000"

    async def ok_draw(group_id, user_id):
        # 替身也要真的记账，否则"失败退次数 / 成功不退"就观察不到。
        async with connect() as db:
            await db.execute(
                """INSERT INTO essence_usage(group_id, user_id, day, used, last_draw_at)
                   VALUES (?,?,?,1,?)
                   ON CONFLICT(group_id, user_id, day) DO UPDATE
                   SET used=used+1, last_draw_at=excluded.last_draw_at""",
                (str(group_id), str(user_id), local_day_key(), time.time()),
            )
            await db.commit()
        return gb_essence.DrawVerdict("ok", 4, 0)

    async def no_record(group_id, key, day, *, label=""):
        return None
    gb_flow.claim_essence_draw = ok_draw
    gb_flow.record_draw = no_record
    # 前面几节把"今天已经捞过什么"打了桩，这里要走真的数据库读数。
    gb_pool.drawn_message_ids = REAL_DRAWN_MESSAGE_IDS
    user = 666666666

    async def used_count(group_id, user_id) -> int:
        async with connect() as db:
            cursor = await db.execute(
                "SELECT used FROM essence_usage WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            )
            row = await cursor.fetchone()
            return int(row["used"]) if row else 0
    gb_flow.claim_essence_draw = ok_draw

    async def no_record(group_id, key, day, *, label=""):
        return None
    gb_flow.record_draw = no_record
    user = 666666666

    async def run_album_case(label, *, enabled=True, share=50, photos=(), broken=()):
        gb_pool.BOTTLE_ALBUM_ENABLED = enabled
        gb_pool.BOTTLE_ALBUM_SHARE = share
        gb_state._album_broken.clear()
        gb_state._album_broken.setdefault(GROUP, set()).update(broken)

        async def fetch_photos(bot, group_id):
            return list(photos)
        gb_pool.fetch_album_photos = fetch_photos
        async with connect() as db:
            await db.execute(
                "DELETE FROM essence_usage WHERE group_id=? AND user_id=?",
                (str(GROUP), str(user)),
            )
            await db.commit()
        bot = SendBot()
        await send_bottle(bot, bottle_event(user), album_only=True)
        return bot.messages, await used_count(GROUP, user)

    msgs, used = await run_album_case("关掉", enabled=False)
    check("关掉相册 -> 关闭提示", msgs and msgs[0] == album_disabled_text(), True)
    check("关掉相册 -> 次数已退还", used, 0)

    msgs, used = await run_album_case("空相册", photos=())
    check("空相册 -> 空提示", msgs and msgs[0] == album_empty_text(), True)
    check("空相册 -> 次数已退还", used, 0)

    msgs, used = await run_album_case("相册全坏", photos=[photo], broken=[_album_key(photo)])
    check("相册全坏 -> 坏图提示", msgs and msgs[0] == album_no_showable_text(1), True)
    check("相册全坏 -> 次数已退还", used, 0)

    # 当天已捞过 -> exhausted
    async with connect() as db:
        await db.execute(
            "DELETE FROM essence_usage WHERE group_id=? AND user_id=?",
            (str(GROUP), str(user)),
        )
        await db.commit()
        await db.execute(
            "DELETE FROM essence_draws WHERE group_id=?", (str(GROUP),)
        )
        await db.execute(
            """INSERT INTO essence_draws(group_id, day, message_id, drawn_at)
               VALUES (?,?,?,?)""",
            (str(GROUP), local_day_key(), _album_key(photo), base.isoformat()),
        )
        await db.commit()
    gb_pool.BOTTLE_ALBUM_ENABLED = True
    gb_pool.BOTTLE_ALBUM_SHARE = 50
    gb_state._album_broken.clear()

    async def fetch_photos_only(bot, group_id):
        return [photo]
    gb_pool.fetch_album_photos = fetch_photos_only
    bot = SendBot()
    await send_bottle(bot, bottle_event(user), album_only=True)
    check("相册今天捞完 -> 捞完提示", bot.messages[0] == album_exhausted_text(1), True)

    # 成功路径：卡片发出去、次数不退、计入已捞
    # render_bottle_card 在生产里是同步函数，替身也必须是同步的。
    def fake_render(chosen, group_id, group_name, *, essence_total, album_total, drawn_times=0):
        target = TEMP / "album_card.png"
        target.write_bytes(b"x")
        return target
    gb_flow.render_bottle_card = fake_render

    async def noop(*args, **kwargs):
        return None
    gb_flow.cache_avatar = noop

    async def real_record(group_id, key, day, *, label=""):
        async with connect() as db:
            await db.execute(
                """INSERT OR REPLACE INTO essence_draws(group_id, day, message_id, drawn_at)
                   VALUES (?,?,?,?)""",
                (str(group_id), day, str(key), datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()
    gb_flow.record_draw = real_record
    async with connect() as db:
        await db.execute("DELETE FROM essence_draws WHERE group_id=?", (str(GROUP),))
        await db.execute(
            "DELETE FROM essence_usage WHERE group_id=? AND user_id=?",
            (str(GROUP), str(user)),
        )
        await db.commit()
    bot = SendBot()
    await send_bottle(bot, bottle_event(user), album_only=True)
    check("成功路径发两条（卡片 + 提示）", len(bot.messages), 2)
    check("成功路径不退次数", await used_count(GROUP, user), 1)
    check("成功路径记为相册类", await recent_drawn_kinds(GROUP, 1), ["album"])


async def main() -> None:
    await initialize_database()
    await album_checks()
    await choice_checks()
    leaderboard_checks()
    text_checks()
    await streak_checks()

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
