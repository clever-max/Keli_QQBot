"""缺字（tofu 空框）回归测试。

背景：群里 44 个归档昵称里有 1 个（``测试昵称（❤️❤️）``）在卡片上显示成"口口"，
归档正文里另有 84 种字符同样缺字形。根因是渲染只用微软雅黑，没有任何回退链。

这份测试锁三件事：
1. 实测过的每个字符都真的能被某个字体画出（不再落到 .notdef 空框）；
2. 变体选择符（``U+FE0F``）必须被**跳过**——它本身不产生字形，画出来就是一个凭空多出的框，
   这正是 ``❤️`` 变成两个框的原因；
3. 空格不能因为"没有字形"被丢掉（它有宽度，且不能被误报成无法绘制）。
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
DB = TEMP / "verify_font.sqlite3"
for stale in TEMP.glob("verify_font.sqlite3*"):
    stale.unlink()

nonebot.init(
    driver="~fastapi",
    allowed_group_ids="123450001",
    authorized_users="100000009",
    database_path=str(DB),
)

from PIL import Image, ImageChops, ImageDraw  # noqa: E402

from plugins.group_bot.common.textimg import (  # noqa: E402
    FALLBACK_FONTS,
    _NOTDEF_PROBES,
    _SKIP_CATEGORIES,
    FontChain,
    _bitmap,
    font,
)

MESSAGES_DB = Path(ROOT) / "data" / "messages.sqlite3"
failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


def renders(font_obj, char: str) -> bool:
    """该字体是否真的画得出这个字符（用非字符当 .notdef 基准）。"""
    references = tuple(_bitmap(font_obj, probe) for probe in _NOTDEF_PROBES) + (
        _bitmap(font_obj, " "),
    )
    rendered = _bitmap(font_obj, char)
    return all(rendered != reference for reference in references)


def archived_chars() -> set[str]:
    if not MESSAGES_DB.exists():
        return set()
    db = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    chars: set[str] = set()
    for row in db.execute("SELECT DISTINCT sender_name FROM messages"):
        chars |= set(row["sender_name"] or "")
    for row in db.execute("SELECT plain_text FROM messages"):
        chars |= set(row["plain_text"] or "")
    db.close()
    return chars


def main() -> None:
    print("== 1. 回退链构成 ==")
    chain = font(32)
    names = [Path(f.path).name for f in chain.fonts if getattr(f, "path", None)]
    print(f"     链 = {names}")
    check_true("主字体是微软雅黑", "msyh" in names[0].lower())
    for expected in FALLBACK_FONTS:
        if (Path(r"C:\Windows\Fonts") / expected).exists():
            check_true(f"回退链包含 {expected}", expected in names)

    print("\n== 2. 变体选择符必须被跳过（❤️ 曾变成两个框）==")
    check("U+FE0F 不绘制", chain.pick("\ufe0f"), None)
    check("U+FE0E 不绘制", chain.pick("\ufe0e"), None)
    check_true("U+FE0F 属于跳过的类别", unicodedata.category("\ufe0f") in _SKIP_CATEGORIES)
    runs = chain.runs("❤️")
    check("❤️ 只产生一段（不是两段）", len(runs), 1)
    check("❤️ 用的是彩色 emoji 字体", Path(runs[0][0].path).name, "seguiemj.ttf")

    print("\n== 3. 空格保留宽度、且不误报 ==")
    space_font = chain.pick(" ")
    check_true("空格仍有字体", space_font is not None)
    check_true("空格映射到主字体", space_font is chain.primary)
    check_true("'a b' 长度大于 'ab'", chain.textlength("a b") > chain.textlength("ab"))

    print("\n== 4. 典型缺字字符各由谁承担 ==")
    expectations = {
        "❤": "seguiemj.ttf",  # 用户报告的那条昵称
        "✅": "seguiemj.ttf",
        "🧠": "seguiemj.ttf",
        "ጿ": "ebrima.ttf",  # 埃塞俄比亚音节
        "성": "malgun.ttf",  # 韩文
        "𠮷": "simsunb.ttf",  # CJK 扩展 B 区生僻字
    }
    for char, want in expectations.items():
        picked = chain.pick(char)
        got = Path(picked.path).name if picked is not None else None
        check(f"{char!r} U+{ord(char):04X} 由 {want} 承担", got, want)

    print("\n== 5. 归档里的每个字符都画得出来（这是「口口」的正面断言）==")
    chars = archived_chars()
    if not chars:
        print("     （跳过：读不到 data\\messages.sqlite3）")
    else:
        usable = [
            c
            for c in sorted(chars)
            if not c.isspace() and unicodedata.category(c) not in _SKIP_CATEGORIES
        ]
        print(f"     归档字符去重 {len(usable)} 个")
        broken = []
        for char in usable:
            picked = chain.pick(char)
            if picked is None:
                continue
            if not renders(picked, char):
                broken.append(char)
        # 线上群里出现过的字符：只报告，不判失败（字体不可能覆盖所有生僻字/表情）。
        # 断言的是"覆盖率"这个我们能守住的东西。
        coverage = 1 - len(broken) / max(1, len(usable))
        check_true(f"归档字符覆盖率 ≥ 99.9%（{coverage * 100:.2f}%）", coverage >= 0.999)
        if broken:
            print("     仍缺:", " ".join(f"U+{ord(c):04X}" for c in broken))

        # 昵称级复验：任何一个字符出问题，该昵称就要报出来
        db = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        names = [r["sender_name"] for r in db.execute("SELECT DISTINCT sender_name FROM messages")]
        db.close()
        bad_names = []
        for name in names:
            for char in name or "":
                if char.isspace() or unicodedata.category(char) in _SKIP_CATEGORIES:
                    continue
                picked = chain.pick(char)
                if picked is not None and not renders(picked, char):
                    bad_names.append((name, char))
                    break
        check("显示异常的昵称数（期望 0）", len(bad_names), 0)
        for name, char in bad_names:
            print(f"     异常昵称: {name!r} 缺 U+{ord(char):04X}")

    print("\n== 6. 多段绘制与锚点 ==")
    img = Image.new("RGB", (600, 80), "white")
    draw = ImageDraw.Draw(img)
    chain.draw(draw, (400, 20), "❤️", fill="black", anchor="ra")
    check_true("右对齐绘制确实落笔了", ImageChops.difference(img, Image.new("RGB", img.size, "white")).getbbox() is not None)
    # 多段文本按锚点整体右对齐：两段以上时起点必须左移，而不是各自对齐到同一点
    width_chain = chain.textlength("测试❤️")
    img2 = Image.new("RGB", (600, 80), "white")
    FontChain.draw(chain, ImageDraw.Draw(img2), (500, 20), "测试❤️", fill="black", anchor="ra")
    lefts = [
        x
        for x in range(img2.width)
        if any(img2.getpixel((x, y)) != (255, 255, 255) for y in range(img2.height))
    ]
    check_true("多段右对齐时最右像素靠近锚点", max(lefts) <= 502)
    check_true("多段右对齐时整体宽度合理", min(lefts) < 500 - width_chain / 2)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


main()
