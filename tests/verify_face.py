"""QQ 内置表情渲染回归测试。

要锁住的语义：
- ``render_message`` 不再把表情压成 ``[表情]``，而是写成 ``[表情:id:名字]``；
- 卡片正文按「文字 + 表情图」混排，表情标记在换行时是**原子**的；
- 表情图优先取本机 QQ 客户端的 ``sysface_res``，解析结果落到 ``data/face_cache``；
- 取不到图时退回 ``[名字]`` 文字，不能抛异常，也不能把整张卡片拖垮。

本套件不联网：本地图源用临时目录伪造，远程兜底被替换成 404。
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import shutil  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
TEMP.mkdir(parents=True, exist_ok=True)
shutil.rmtree(TEMP / "face_cache", ignore_errors=True)
DB = TEMP / "verify_face.sqlite3"
for stale in TEMP.glob("verify_face.sqlite3*"):
    stale.unlink()

import nonebot  # noqa: E402

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001",
    authorized_users="100000009",
    database_path=str(DB),
)

from PIL import Image  # noqa: E402

from plugins.group_bot.common import faces, richtext  # noqa: E402
from plugins.group_bot.common.card import CardEntry, render_card_page  # noqa: E402
from plugins.group_bot.common.render import render_message  # noqa: E402
from plugins.group_bot.common.textimg import font  # noqa: E402

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


def fake_face_root() -> Path:
    """伪造一个 QQ 客户端表情目录：s14.png 是一张纯红色 128px 图。"""
    root = TEMP / "fake_qq_face" / "static"
    root.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (128, 128), (255, 0, 0, 255)).save(root / "s14.png")
    return TEMP / "fake_qq_face"


print("== 1. 表情 id → 名字 / 标记 ==")
check("14 是微笑", faces.face_name(14), "微笑")
check("387 是太好笑", faces.face_name(387), "太好笑")
check(
    "faceText 优先于 id",
    faces.face_token({"id": "387", "raw": {"faceText": "/太好笑"}}),
    "[表情:387:太好笑]",
)
check(
    "没有 faceText 时用映射表",
    faces.face_token({"id": "13", "raw": {"faceText": None}}),
    "[表情:13:呲牙]",
)
check(
    "mface 用 summary",
    faces.face_token({"emoji_id": "1", "summary": "[开心]"}),
    "[表情:开心]",
)
check("完全没有信息时仍是占位符", faces.face_token({}), "[表情]")

print("\n== 2. render_message 保留表情信息 ==")
segments = [
    {"type": "text", "data": {"text": "笑死"}},
    {"type": "face", "data": {"id": "387", "raw": {"faceText": "/太好笑"}}},
    {"type": "text", "data": {"text": "了"}},
    {"type": "image", "data": {"file": "x.jpg"}},
]
check("文字与表情顺序不变", render_message(segments), "笑死[表情:387:太好笑]了[图片]")
check(
    "omit_images 也保留表情",
    render_message(segments, omit_images=True),
    "笑死[表情:387:太好笑]了",
)

print("\n== 3. 混排：解析、换行、测宽 ==")
runs = richtext.parse("a[表情:14:呲牙]b")
check("解析出三段", [run.text or f"face:{run.face_id}" for run in runs], ["a", "face:14", "b"])
check("mface 只有名字时能找回 id", richtext.parse("[表情:微笑]")[0].face_id, 14)

faces._reset_caches()
faces._ROOTS = [fake_face_root()]
image_file = faces.image_path(14)
check_true("本地表情被复制进缓存", image_file is not None and image_file.exists())
with Image.open(image_file) as cached:
    check("缓存图是 64px", cached.size, (64, 64))
check("二次解析走缓存", faces.image_path(14), image_file)

body_font = font(23)
face_size = 26
narrow = richtext.wrap("字" * 200 + "[表情:14:呲牙]", body_font, 120, face_size)
check_true("折行后仍有完整的表情单元", any(run.face_id == 14 for line in narrow for run in line))
check_true(
    "窄行里没有把标记拆成文字",
    all("[表情:" not in run.text for line in narrow for run in line),
)
check(
    "表情宽度按边长算",
    richtext.measure([richtext.Run(face_id=14)], body_font, face_size),
    float(face_size),
)

print("\n== 4. 卡片真的把表情画出来了 ==")
text = "看这个[表情:14:呲牙]"
card = render_card_page(
    "表情测试",
    "1 条",
    [CardEntry(user_id="100000015", name="测试昵称", text=text)],
    group_id=123450001,
    directory=TEMP / "verify_face_cards",
)
check_true("卡片文件存在", card.is_file())
with Image.open(card) as image:
    pixels = image.convert("RGB")
    # 只看气泡区域：头像圆也带颜色，全图计数会误判。
    red = 0
    for y in range(230, 310):
        for x in range(120, 900):
            r, g, b = pixels.getpixel((x, y))
            if r > 200 and g < 70 and b < 70:
                red += 1
check_true("卡片里出现了伪造的红色表情像素", red > 200)
card.unlink(missing_ok=True)

print("\n== 5. 缺图时退回文字，且只探测一次 ==")
faces._reset_caches()
faces._ROOTS = []
calls: list[str] = []


def fake_fetch(url: str) -> bytes:
    calls.append(url)
    return b""


original_fetch = faces._fetch
faces._fetch = fake_fetch  # type: ignore[assignment]
try:
    check("没有图时返回 None", faces.image_path(999999), None)
    check("404 只探测一次", len(calls), 1)
    check("再问一次走缓存", faces.image_path(999999), None)
    check("缓存标记后不再请求", len(calls), 1)
    fallback = richtext.measure([richtext.Run(face_id=999999, label="马上到")], body_font, face_size)
    check("退回文字宽度", fallback, body_font.textlength("[马上到]"))
finally:
    faces._fetch = original_fetch  # type: ignore[assignment]

print("\n== 6. 配置文档三处一致依赖的键已声明 ==")
env_example = (Path(ROOT) / ".env.example").read_text(encoding="utf-8")
config_doc = (Path(ROOT) / "docs" / "configuration.md").read_text(encoding="utf-8")
check_true("QQ_FACE_DIR 在 .env.example", "QQ_FACE_DIR" in env_example)
check_true("QQ_FACE_DIR 在 configuration.md", "QQ_FACE_DIR" in config_doc)

print("\n" + "=" * 60)
if failures:
    print(f"失败 {len(failures)} 项：")
    for item in failures:
        print(" -", item)
    sys.exit(1)
print("全部通过")
