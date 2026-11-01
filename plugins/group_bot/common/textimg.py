"""Text drawing helpers for the Pillow-rendered pages.

字体不是单个而是**一条链**：主字体（微软雅黑）缺哪个字形就换一个有的字体来画。
原因是实测发现昵称与正文里大量 emoji 在雅黑里没有字形，Pillow 会把它们画成
``.notdef`` 空框——群里看到的就是"口口"。

实测（2026-10-15，44 个归档昵称 + 全部归档正文，共 86 种雅黑缺字）：
- ``❤️`` 会变成**两个**空框，因为 ``U+2764`` 与 ``U+FE0F`` 都缺：变体选择符这类
  组合/格式字符本身不产生字形，必须直接跳过，否则凭空多一个框；
- 各字体的真实覆盖（这组数字是用非字符 ``U+FFFF`` 作 `.notdef` 基准量出来的）：
  ``seguiemj`` 80/86（emoji 全中且是彩色）、``seguisym`` 73/86、``ebrima`` 恰好覆盖
  4 个埃塞俄比亚音节、``malgun`` 覆盖韩文、``simsunb`` 覆盖 CJK 扩展 B 区（生僻姓名，
  如 ``𠮷``）、``SimsunExtG`` 覆盖扩展 G 区；
- ⚠️ **不能用私用区 ``U+E000`` 当 `.notdef` 基准**：``seguisym`` 这类字体真的映射了
  私用区，基准本身带墨，会把"画成空框"误判成"有字形"（这个坑我踩过一次）。
"""

from __future__ import annotations

import os
from pathlib import Path

from nonebot import logger
from PIL import Image, ImageDraw, ImageFont

# 缺字回退顺序。主字体（微软雅黑）没有字形时，按这个顺序找第一个有的：
# 彩色 emoji → 老符号 → 埃塞俄比亚文 → 泰文 → 拉丁语音扩展 → 韩文 → CJK 扩展 B/G。
FALLBACK_FONTS = (
    "seguiemj.ttf",  # Segoe UI Emoji：emoji，彩色
    "seguisym.ttf",  # Segoe UI Symbol：老符号的宽覆盖兜底
    "ebrima.ttf",  # Ebrima：埃塞俄比亚文等非洲文字
    "LeelawUI.ttf",  # Leelawadee UI：泰文
    "segoeui.ttf",  # Segoe UI：拉丁语音扩展（ᴗ / ᴬ 等）
    "malgun.ttf",  # Malgun Gothic：韩文
    "simsunb.ttf",  # SimSun-ExtB：CJK 扩展 B 区生僻字（𠮷 之类）
    "SimsunExtG.ttf",  # SimSun-ExtG：CJK 扩展 G 区
)
# 这些字形由位图提供（彩色 emoji），绘制时要开 embedded_color；按文件名识别。
_COLOR_EMOJI_MARKERS = ("seguiemj", "segoeuiemoji")
# 组合记号 / 格式字符：本身不产生字形（如 ❤️ 的 U+FE0F），绘制时跳过。
_SKIP_CATEGORIES = ("Mn", "Me", "Cf")
# `.notdef`（缺字方框）的基准字符：Unicode 非字符，保证任何字体都不会给它真实字形。
_NOTDEF_PROBES = ("\uFFFF", "\uFFFE", "\U0010FFFF", "\U0001FFFE")

_scratch = ImageDraw.Draw(Image.new("L", (1, 1)))
_unrenderable_logged: set[str] = set()


def _bitmap(font_obj: ImageFont.ImageFont, char: str, box: int = 110) -> bytes:
    image = Image.new("L", (box, box), 0)
    ImageDraw.Draw(image).text((8, 8), char, font=font_obj, fill=255)
    return image.tobytes()


class FontChain:
    """一组同尺寸字体，逐字符挑第一个有字形的来画。

    ``PIL.ImageDraw.text`` 只接受单个字体对象，所以这里自己分段：逐字符选字体，
    把相邻同字体的字符合成一段，按段绘制并推进 x 坐标。
    """

    def __init__(self, primary: ImageFont.ImageFont, fallbacks: list[ImageFont.ImageFont]):
        self.primary = primary
        self.fonts = [primary, *fallbacks]
        self._pick: dict[str, ImageFont.ImageFont | None] = {}
        self._reference: dict[int, tuple[bytes, bytes]] = {}

    # ---- 覆盖判断 -------------------------------------------------------
    def _reference_bitmaps(self, font_obj: ImageFont.ImageFont) -> tuple[bytes, ...]:
        cached = self._reference.get(id(font_obj))
        if cached is None:
            cached = tuple(_bitmap(font_obj, probe) for probe in _NOTDEF_PROBES) + (
                _bitmap(font_obj, " "),
            )
            self._reference[id(font_obj)] = cached
        return cached

    def _has_glyph(self, font_obj: ImageFont.ImageFont, char: str) -> bool:
        references = self._reference_bitmaps(font_obj)
        rendered = _bitmap(font_obj, char)
        # 与该字体自己的 .notdef（缺字方框）或完全空白相同，都算"没这个字形"。
        return all(rendered != reference for reference in references)

    def pick(self, char: str) -> ImageFont.ImageFont | None:
        """该字符用哪个字体画；``None`` 表示这个字符不该画（组合/格式字符）。"""
        if char in self._pick:
            return self._pick[char]
        import unicodedata

        if unicodedata.category(char) in _SKIP_CATEGORIES:
            chosen: ImageFont.ImageFont | None = None
        elif char.isspace():
            # 空格没有字形但有**宽度**：必须留着，也不能当成"无法绘制"去报警。
            chosen = self.primary
        else:
            chosen = None
            for candidate in self.fonts:
                if self._has_glyph(candidate, char):
                    chosen = candidate
                    break
            if chosen is None:
                # 整条链都没有：退回主字体（会画成缺字方框，比其他渲染器的行为一致），
                # 但每个字符只记一次日志，方便事后知道到底缺的是哪个码位。
                chosen = self.primary
                code = f"U+{ord(char):04X}"
                if code not in _unrenderable_logged:
                    _unrenderable_logged.add(code)
                    logger.warning(
                        "No font in the fallback chain can draw {} ({!r}); "
                        "it will show as an empty box.",
                        code,
                        char,
                    )
        self._pick[char] = chosen
        return chosen

    # ---- 分段、测宽、绘制 ------------------------------------------------
    def runs(self, text: str) -> list[tuple[ImageFont.ImageFont, str]]:
        out: list[tuple[ImageFont.ImageFont, str]] = []
        for char in text or "":
            font_obj = self.pick(char)
            if font_obj is None:
                continue
            if out and out[-1][0] is font_obj:
                out[-1] = (font_obj, out[-1][1] + char)
            else:
                out.append((font_obj, char))
        return out

    @staticmethod
    def _is_color(font_obj: ImageFont.ImageFont) -> bool:
        name = str(getattr(font_obj, "path", "") or "").lower()
        return any(marker in name for marker in _COLOR_EMOJI_MARKERS)

    def textlength(self, text: str) -> float:
        return sum(
            _scratch.textlength(run, font=font_obj) for font_obj, run in self.runs(text)
        )

    def draw(
        self,
        draw: ImageDraw.ImageDraw,
        xy: tuple[int, int],
        text: str,
        fill: object = None,
        **kwargs: object,
    ) -> None:
        """按段绘制，逐段推进 x。

        ``anchor`` 的水平分量在这里自己算（先按总宽度求出起点，再让每段都用
        ``l`` + 原垂直分量绘制），否则多段文本会各自以同一个锚点对齐而叠在一起。
        垂直分量直接交给 Pillow；各字体同尺寸，基线一致。
        """
        x, y = xy
        anchor = kwargs.pop("anchor", None)
        if isinstance(anchor, str) and anchor:
            horizontal, vertical = anchor[0], (anchor[1] if len(anchor) > 1 else "a")
            total = self.textlength(text)
            if horizontal == "m":
                x = int(x - total / 2)
            elif horizontal == "r":
                x = int(x - total)
            kwargs["anchor"] = "l" + vertical

        for font_obj, run in self.runs(text):
            options = dict(kwargs)
            if self._is_color(font_obj):
                options.setdefault("embedded_color", True)
            try:
                draw.text((x, y), run, font=font_obj, fill=fill, **options)
            except TypeError:
                # 个别 Pillow 版本不支持 embedded_color：退回单色，别整个失败。
                options.pop("embedded_color", None)
                draw.text((x, y), run, font=font_obj, fill=fill, **options)
            x = int(x + draw.textlength(run, font=font_obj))


def font(size: int, bold: bool = False) -> FontChain:
    """主字体 + 缺字回退链。返回的 ``FontChain`` 用 ``.draw()`` / ``.textlength()``。"""
    windows = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    primary_candidates = (
        [windows / "msyhbd.ttc", windows / "simhei.ttf", windows / "msyh.ttc"]
        if bold
        else [windows / "msyh.ttc", windows / "simhei.ttf"]
    )
    primary_candidates += [
        windows / "NotoSansSC-VF.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]

    primary: ImageFont.ImageFont = ImageFont.load_default()
    for candidate in primary_candidates:
        try:
            if candidate.exists():
                primary = ImageFont.truetype(str(candidate), size=size)
                break
        except OSError:
            continue

    fallbacks: list[ImageFont.ImageFont] = []
    for name in FALLBACK_FONTS:
        path = windows / name
        try:
            if path.exists():
                fallbacks.append(ImageFont.truetype(str(path), size=size))
        except OSError:
            continue
    return FontChain(primary, fallbacks)

def wrap_text(text: str, font_obj: FontChain, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in (text or "[非文本消息]").splitlines() or [""]:
        current = ""
        for char in paragraph:
            if current and font_obj.textlength(current + char) > max_width:
                lines.append(current)
                current = char
            else:
                current += char
        lines.append(current)
    return lines or [""]
