"""Checks for the media-quality fix and for forwarding videos without a card.

The measured defect this locks down: `normalize_cached_image` used to force every
download down to 1000px and re-encode at JPEG q88, so a 4096x3072 original (4.2 MB)
was stored as 1000x750 / 170 KB — 4% of the bytes — and `fitted_size` then stretched
anything smaller than its box up to the box size.
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import asyncio
import io
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
MEDIA = TEMP / "media_quality"
MEDIA.mkdir(parents=True, exist_ok=True)
DB = TEMP / "verify_media.sqlite3"
for stale in TEMP.glob("verify_media.sqlite3*"):
    stale.unlink()

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001",
    authorized_users="100000009",
    database_path=str(DB),
)

from plugins.group_bot.bottle import essence as gb_essence  # noqa: E402
from plugins.group_bot.bottle import flow as gb_flow
from plugins.group_bot.bottle import pool as gb_pool
from plugins.group_bot.media import pipeline as gb_media  # noqa: E402
from plugins.group_bot.media.album import AlbumPhoto, parse_media_item  # noqa: E402
from plugins.group_bot.common.card import fitted_size  # noqa: E402
from plugins.group_bot.core.config import (  # noqa: E402
    IMAGE_CACHE_MAX_DIMENSION,
    RECALL_THUMB_SIZE,
)
from plugins.group_bot.bottle.essence import (  # noqa: E402
    bottle_hint_text,
    choose_album_photo,
    choose_essence,
    essence_video_ref,
    local_day_key,
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


def make_image(path: Path, size: tuple[int, int], *, fmt: str = "PNG") -> bytes:
    image = Image.new("RGB", size)
    for x in range(0, size[0], 7):  # 一点纹理，避免压缩后完全同质
        for y in range(0, size[1], 7):
            image.putpixel((x, y), (x % 256, y % 256, 128))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    content = buffer.getvalue()
    path.write_bytes(content)
    return content


def check_card_size() -> None:
    print("\n== 1. fitted_size 绝不放大 ==")
    small = TEMP / "small.png"
    make_image(small, (240, 160))
    check("240x160 不再被拉伸", fitted_size(small, 812, 812), (240, 160))

    mid = TEMP / "mid.png"
    make_image(mid, (400, 300))
    check("400x300 保持原尺寸", fitted_size(mid, 812, 812), (400, 300))

    big = TEMP / "big.png"
    make_image(big, (4096, 3072))
    check("4096x3072 缩到框内", fitted_size(big, 812, 812), (812, 609))

    tiny = TEMP / "tiny.png"
    make_image(tiny, (16, 16))
    check("16x16 保持原尺寸", fitted_size(tiny, 812, 812), (16, 16))


def check_cache_keeps_original() -> None:
    print("\n== 2. 缓存保留原图（这是 4% 那个缺陷的正面断言）==")
    source = TEMP / "origin.png"
    # 1200x900：比旧的 1000 上限大，但在新的上限之内 —— 以前必然被压，现在应原样保留。
    content = make_image(source, (1200, 900))
    destination = TEMP / "cached_keep.jpg"
    check_true("normalize 成功", gb_media.normalize_cached_image(content, destination))
    check("缓存与原图字节完全一致", destination.read_bytes(), content)
    with Image.open(destination) as image:
        check("缓存像素未被改动", image.size, (1200, 900))

    print("\n== 3. 超过上限才缩，且缩到上限而不是 1000 ==")
    huge_side = IMAGE_CACHE_MAX_DIMENSION + 400
    huge = TEMP / "huge.png"
    huge_content = make_image(huge, (huge_side, 1000))
    destination2 = TEMP / "cached_shrunk.jpg"
    check_true("normalize 成功", gb_media.normalize_cached_image(huge_content, destination2))
    with Image.open(destination2) as image:
        check(
            "长边被压到上限",
            max(image.size),
            IMAGE_CACHE_MAX_DIMENSION,
        )
        check("超限时才重编码为 JPEG", image.format, "JPEG")

    print("\n== 4. 卡片图片框用满内容宽度（不再固定 768）==")
    check_true("配置上限不小于旧值", RECALL_THUMB_SIZE >= 768)
    # 有效框宽 = min(配置上限, 内容宽度)；内容宽度 = CARD_WIDTH - body_x - margin。
    from plugins.group_bot import card as gb_card

    body_x = gb_card.CARD_MARGIN + gb_card.CARD_AVATAR_SIZE + 24
    body_width = gb_card.CARD_WIDTH - body_x - gb_card.CARD_MARGIN
    effective = max(64, min(RECALL_THUMB_SIZE, body_width))
    check_true("有效框宽等于内容宽度", effective == body_width)
    check_true("比旧的 768 更大", effective > 768)
    print(f"     CARD_WIDTH={gb_card.CARD_WIDTH} 内容宽度={body_width} 有效图片框={effective}")


def check_album_url_quality() -> None:
    print("\n== 5. 相册取图优先原图键（跨层级也算）==")
    nested = {
        "media_id": "m1",
        "url": "https://a.qpic.cn/thumb.jpg",
        "image": {"originUrl": "https://a.qpic.cn/origin.jpg"},
    }
    photo = parse_media_item(nested, "A1", "相册")
    check("嵌套 originUrl 胜过顶层 url", photo.url, "https://a.qpic.cn/origin.jpg")

    flat = {
        "media_id": "m2",
        "thumb_url": "https://a.qpic.cn/t.jpg",
        "origin_url": "https://a.qpic.cn/o.jpg",
    }
    check("同层按优先级取原图", parse_media_item(flat, "A1", "相册").url, "https://a.qpic.cn/o.jpg")

    print("\n== 6. 视频识别（相册）==")
    video_cases = [
        ("video_url 键", {"media_id": "v1", "video_url": "https://x.qpic.cn/a.mp4"}, "https://x.qpic.cn/a.mp4"),
        ("嵌套 playUrl", {"id": "v2", "video": {"playUrl": "https://x.qpic.cn/play_url?x=1"}}, "https://x.qpic.cn/play_url?x=1"),
        ("mp4 扩展名", {"id": "v3", "src": "https://x.qpic.cn/clip.MP4"}, "https://x.qpic.cn/clip.MP4"),
    ]
    for label, raw, want in video_cases:
        photo = parse_media_item(raw, "A1", "相册")
        check(f"{label} -> video", photo.kind, "video")
        check(f"{label} -> 地址", photo.url, want)

    image_only = parse_media_item({"media_id": "p9", "url": "https://a.qpic.cn/x.jpg"}, "A", "B")
    check("图片项仍是 image", image_only.kind, "image")

    print("\n== 7. 视频识别（精华消息段）==")
    check(
        "本地路径优先（省一次下载）",
        essence_video_ref({"content": [{"type": "video", "data": {"file": r"C:\x\v.mp4", "url": "https://y/v.mp4"}}]}),
        r"C:\x\v.mp4",
    )
    check(
        "只有 url 时用 url",
        essence_video_ref({"content": [{"type": "video", "data": {"url": "https://y/v.mp4"}}]}),
        "https://y/v.mp4",
    )
    check("纯文本没有视频", essence_video_ref({"content": [{"type": "text", "data": {"text": "hi"}}]}), "")
    check(
        "图片消息没有视频",
        essence_video_ref({"content": [{"type": "image", "data": {"url": "https://y/i.jpg"}}]}),
        "",
    )


async def check_video_draw() -> None:
    print("\n== 8. choose_* 对视频直接返回引用，不下载 ==")
    calls = {"materialize": 0, "photo": 0}

    async def no_drawn(group_id, day_key):
        return set()

    async def spy_materialize_photo(photo, group_id, client):
        calls["photo"] += 1
        return TEMP / "should_not_be_used.jpg"

    async def spy_materialize_images(bot, group_id, item):
        calls["materialize"] += 1
        return []
    gb_pool.drawn_message_ids = no_drawn
    gb_pool.materialize_photo = spy_materialize_photo
    gb_pool.materialize_images = spy_materialize_images
    gb_essence._album_broken.clear()
    gb_essence._essence_broken.clear()

    video_photo = AlbumPhoto(media_id="v1", url="https://x.qpic.cn/a.mp4", kind="video")
    chosen = await choose_album_photo(SimpleNamespace(), GROUP, [video_photo], day=local_day_key())
    check("相册视频被选中", (chosen.kind, chosen.item.media_id), ("album", "v1"))
    check("相册视频直接给引用", chosen.video_ref, "https://x.qpic.cn/a.mp4")
    check("相册视频没有下载", calls["photo"], 0)

    video_essence = {
        "message_id": 900,
        "sender_id": "11",
        "sender_nick": "甲",
        "operator_id": "22",
        "operator_nick": "乙",
        "content": [{"type": "video", "data": {"url": "https://y/v.mp4"}}],
    }
    chosen = await choose_essence(
        SimpleNamespace(), GROUP, [video_essence], day=local_day_key()
    )
    check("精华视频被选中", (chosen.kind, chosen.item["message_id"]), ("essence", 900))
    check("精华视频直接给引用", chosen.video_ref, "https://y/v.mp4")
    check("精华视频没有做图片物化", calls["materialize"], 0)

    print("\n== 9. 视频提示文案 ==")
    hint = bottle_hint_text(chosen, 3, 0, 2)
    check_true("提示说明是视频", "一段视频" in hint)

    print("\n== 10. send_bottle 转发视频：不发卡片、不退次数 ==")

    class SendBot:
        def __init__(self, fail: bool = False):
            self.messages: list = []
            self.fail = fail

        async def send_group_msg(self, **kwargs):
            self.messages.append(kwargs["message"])
            if self.fail and len(self.messages) == 1:
                # 只让视频那一次失败：失败提示本身必须能发出去（真实链路更上一层
                # 还有 safe_send_essence 兜底）。
                raise RuntimeError("send failed")
            return {"status": "ok"}

        @property
        def self_id(self):
            return "100000000"

    async def ok_draw(group_id, user_id):
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

    async def real_record(group_id, key, day, *, label=""):
        async with connect() as db:
            await db.execute(
                """INSERT OR REPLACE INTO essence_draws(group_id, day, message_id, drawn_at)
                   VALUES (?,?,?,?)""",
                (str(group_id), day, str(key), datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()

    async def used_count(group_id, user_id) -> int:
        async with connect() as db:
            cursor = await db.execute(
                "SELECT used FROM essence_usage WHERE group_id=? AND user_id=?",
                (str(group_id), str(user_id)),
            )
            row = await cursor.fetchone()
            return int(row["used"]) if row else 0

    setattr(gb_flow, "claim_essence_draw", ok_draw)
    setattr(gb_flow, "record_draw", real_record)

    def rendered(*args, **kwargs):  # 视频路径绝不该调用卡片渲染
        raise AssertionError("视频不该构造卡片")

    setattr(gb_flow, "render_bottle_card", rendered)

    async def fetch_one_video(bot, group_id):
        return [video_photo]

    async def fetch_essence_only(bot, group_id):
        return []

    setattr(gb_pool, "fetch_album_photos", fetch_one_video)
    setattr(gb_pool, "fetch_essence_list", fetch_essence_only)
    gb_pool.BOTTLE_ALBUM_SHARE = 100

    user = 777777777
    async with connect() as db:
        await db.execute("DELETE FROM essence_usage WHERE user_id=?", (str(user),))
        await db.execute("DELETE FROM essence_draws WHERE group_id=?", (str(GROUP),))
        await db.commit()

    bot = SendBot()
    event = SimpleNamespace(group_id=GROUP, user_id=user, group_name="测试群")
    await send_bottle(bot, event, album_only=True)
    check("发出两条（视频 + 提示）", len(bot.messages), 2)
    check("第一条是视频段", bot.messages[0].type, "video")
    check("视频段带上引用", bot.messages[0].data.get("file"), "https://x.qpic.cn/a.mp4")
    check("第二条是文字提示", isinstance(bot.messages[1], str), True)
    check_true("提示说明是视频", "一段视频" in bot.messages[1])
    check("成功不退次数", await used_count(GROUP, user), 1)

    print("\n== 11. 视频转发失败 -> 退次数 + 提示 ==")
    async with connect() as db:
        await db.execute("DELETE FROM essence_usage WHERE user_id=?", (str(user),))
        await db.commit()
    bot = SendBot(fail=True)
    await send_bottle(bot, event, album_only=True)
    check("失败后次数已退还", await used_count(GROUP, user), 0)
    check_true(
        "失败提示提到视频",
        any(isinstance(m, str) and "视频" in m for m in bot.messages),
    )


async def main() -> None:
    await initialize_database()
    check_card_size()
    check_cache_keeps_original()
    check_album_url_quality()
    await check_video_draw()

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


asyncio.run(main())
