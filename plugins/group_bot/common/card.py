"""Shared message-card renderer.

One layout is reused by every picture the bot sends (撤回记录, 群精华, and the
statistics cards planned later): a white header, then one rounded row per entry
with avatar, name, timestamps, a text bubble and image thumbnails.

Callers only describe *what* to draw (:class:`CardEntry`); the geometry lives
here so all cards keep the same look.  Nothing here touches the network or the
database, which keeps it cheap to test on its own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from PIL import Image, ImageDraw, ImageOps

from ..core.config import CARD_IMAGE_DIR, RECALL_THUMB_SIZE
from ..media.pipeline import draw_avatar
from . import richtext
from .textimg import font

# --- geometry -------------------------------------------------------------
CARD_WIDTH = 960
CARD_MARGIN = 34
CARD_HEADER_TOP = 20
CARD_HEADER_BOX_HEIGHT = 104
# Background between the title block and the first message block: without it the
# two white surfaces touch and read as one merged block.
CARD_HEADER_GAP = 22
# Where the first message row starts.
CARD_HEADER_HEIGHT = CARD_HEADER_TOP + CARD_HEADER_BOX_HEIGHT + CARD_HEADER_GAP
CARD_ROW_GAP = 22
CARD_ROW_RADIUS = 22
CARD_ROW_TOP_PADDING = 20
CARD_AVATAR_SIZE = 68
CARD_BODY_TOP = 94  # where the bubble/thumbnail area starts inside a row
CARD_BODY_LINE_HEIGHT = 46  # generous leading for 23 px text
CARD_BUBBLE_RADIUS = 18
CARD_BUBBLE_PAD_X = 22
CARD_BUBBLE_PAD_Y = 14
CARD_THUMB_GAP = 12
CARD_MAX_TEXT_CHARS = 3000
CARD_MAX_THUMBS = 4
# 正文里的 QQ 表情画成方形图：比字号略大一点，接近 QQ 聊天窗里的观感。
CARD_FACE_SCALE = 1.15
# 表情图相对文字顶端的偏移（像素）。正数往下：表情比字高，往下压一点才和基线齐。
CARD_FACE_TOP = -1
# PNG 保存是否做 optimize。默认关：实测多花 134 ms 只省 2 KB（见 render_card_page 里的注释）。
PNG_OPTIMIZE = False

# --- palette --------------------------------------------------------------
CARD_BACKGROUND = (243, 245, 250)
CARD_SURFACE = (255, 255, 255)
CARD_BUBBLE = (241, 244, 250)
CARD_BUBBLE_OUTLINE = (226, 232, 243)
CARD_BUBBLE_SHADOW = (231, 236, 246)
CARD_TEXT = (38, 44, 55)
CARD_NAME = (32, 39, 51)
CARD_META = (128, 137, 152)
CARD_RIGHT_TEXT = (140, 148, 162)
CARD_THUMB_BACKDROP = (236, 239, 245)
CARD_THUMB_OUTLINE = (226, 231, 240)

T = TypeVar("T")

# 一行正文的排版结果：文字/表情单元列表。
RichLine = list[richtext.Run]
# 测量阶段缓存下来的：条目、折好的行、整行高度、图片缩略图。
MeasuredEntry = tuple["CardEntry", list[RichLine], int, list[tuple[Path, tuple[int, int]]]]


@dataclass
class CardEntry:
    """One row of a card.  Empty strings simply leave that part out."""

    user_id: str
    name: str
    right_text: str = ""
    meta_text: str = ""
    text: str = ""
    image_paths: list[Path] = field(default_factory=list)
    image_note: str = ""
    extra_images: int = 0


def display_name(name: object) -> str:
    """Normalise a nickname for drawing: never empty, never a bare QQ number."""
    cleaned = " ".join(str(name or "").split())[:20]
    if not cleaned or cleaned.isdigit():
        return "群成员"
    return cleaned


def page_size_for(max_images: int) -> int:
    """How many rows fit on one page, given the most images any row carries."""
    if max_images == 0:
        return 12
    if max_images == 1:
        return 6
    if max_images == 2:
        return 4
    return 2


def paginate(items: list[T], page_size: int) -> list[list[T]]:
    return [items[index : index + page_size] for index in range(0, len(items), page_size)]


def fitted_size(image_path: Path, max_w: int, max_h: int) -> tuple[int, int] | None:
    """Size the picture keeps when it is fitted into ``max_w`` x ``max_h``.

    绝不放大：``scale`` 被夹在 1 以内。以前没有这一夹，一张 240px 的缩略图会被拉伸
    3.2 倍塞进 768 的框里，本来只是小图，结果变成一张糊图。小图现在按原尺寸显示。
    """
    try:
        with Image.open(image_path) as source:
            width, height = source.size
    except OSError:
        return None
    if width <= 0 or height <= 0:
        return None
    scale = min(max_w / width, max_h / height, 1.0)
    return max(1, int(width * scale)), max(1, int(height * scale))


def bubble_size(lines: list[str], line_height: int, font_size: int) -> int:
    """Height of a bubble holding ``lines`` text lines."""
    if not lines:
        return 0
    return 2 * CARD_BUBBLE_PAD_Y + font_size + (len(lines) - 1) * line_height


def _soft_round_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, int, int],
    outline: tuple[int, int, int] | None = None,
    shadow: tuple[int, int, int] | None = None,
    shadow_offset: int = 3,
) -> None:
    left, top, right, bottom = box
    if shadow is not None:
        draw.rounded_rectangle(
            (left, top + shadow_offset, right, bottom + shadow_offset),
            radius=radius,
            fill=shadow,
        )
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=1)


def render_card_page(
    title: str,
    subtitle: str,
    entries: list[CardEntry],
    *,
    group_id: int,
    directory: Path | None = None,
    thumb_max: int | None = None,
) -> Path:
    """Render one page of ``entries`` into a PNG and return its path.

    The caller owns chunking (see :func:`paginate`) and deleting the file after
    it was sent.  ``thumb_max`` 限制缩略图边长：默认按 ``RECALL_THUMB_SIZE``（大图，
    适合"一次只看一条"的卡片）；多行榜单传一个小值，否则一张图就占掉半屏。
    """
    margin = CARD_MARGIN
    avatar_size = CARD_AVATAR_SIZE
    body_x = margin + avatar_size + 24
    body_width = CARD_WIDTH - body_x - margin
    header_height = CARD_HEADER_HEIGHT
    title_font = font(33, True)
    name_font = font(25, True)
    meta_font = font(20)
    body_font = font(23)
    body_font_size = 23
    face_size = max(body_font_size, int(round(body_font_size * CARD_FACE_SCALE)))
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    line_height = CARD_BODY_LINE_HEIGHT
    # 图片框取「卡片内容宽度」和上限里较小的那个：以前固定 768，而内容宽度约
    # 812，等于白白浪费掉一截；原图保住之后，这里放大才看得出差别。
    # 多行榜单会传一个更小的 thumb_max，避免一行图就把卡片撑到几千像素高。
    thumb_size = max(64, min(thumb_max or RECALL_THUMB_SIZE, body_width))
    thumb_columns = 1 if body_width < thumb_size * 2 + CARD_THUMB_GAP else 2

    measured: list[MeasuredEntry] = []
    for entry in entries:
        text = (entry.text or "")[:CARD_MAX_TEXT_CHARS]
        lines = (
            richtext.wrap(text, body_font, body_width - 2 * CARD_BUBBLE_PAD_X, face_size)
            if text
            else []
        )
        frames: list[tuple[Path, tuple[int, int]]] = []
        for image_path in list(entry.image_paths)[:CARD_MAX_THUMBS]:
            size = fitted_size(image_path, thumb_size, thumb_size)
            if size is None:
                continue
            frames.append((image_path, size))
        image_rows = (len(frames) + thumb_columns - 1) // thumb_columns
        cursor = CARD_BODY_TOP
        if lines:
            cursor += bubble_size(lines, line_height, body_font_size) + 14
        if entry.image_note:
            cursor += 30
        for row_index in range(image_rows):
            row = frames[row_index * thumb_columns : (row_index + 1) * thumb_columns]
            cursor += max(height for _, (_, height) in row) + CARD_THUMB_GAP
        if entry.extra_images > 0:
            cursor += 30
        height = max(126, cursor + 18)
        measured.append((entry, lines, height, frames))

    total_height = (
        header_height
        + sum(height + CARD_ROW_GAP for _, _, height, _ in measured)
        - CARD_ROW_GAP
        + margin
    )
    canvas = Image.new("RGB", (CARD_WIDTH, total_height), CARD_BACKGROUND)
    draw = ImageDraw.Draw(canvas)
    _soft_round_rect(
        draw,
        (20, CARD_HEADER_TOP, CARD_WIDTH - 20, CARD_HEADER_TOP + CARD_HEADER_BOX_HEIGHT),
        CARD_ROW_RADIUS + 2,
        CARD_SURFACE,
        shadow=(233, 237, 245),
        shadow_offset=3,
    )
    # Vertically centre both title lines inside the header block.
    title_font.draw(draw, (margin, CARD_HEADER_TOP + 28), title, fill=CARD_NAME)
    meta_font.draw(
        draw,
        (CARD_WIDTH - margin + 2, CARD_HEADER_TOP + 38),
        subtitle,
        fill=CARD_META,
        anchor="ra",
    )

    y = header_height
    for entry, lines, height, frames in measured:
        _soft_round_rect(
            draw,
            (20, y, CARD_WIDTH - 20, y + height),
            CARD_ROW_RADIUS,
            CARD_SURFACE,
            shadow=(234, 238, 246),
            shadow_offset=3,
        )
        name = display_name(entry.name)
        draw_avatar(
            canvas,
            group_id,
            str(entry.user_id),
            name,
            margin,
            y + CARD_ROW_TOP_PADDING,
            avatar_size,
        )
        if entry.right_text:
            meta_font.draw(
                draw,
                (CARD_WIDTH - margin + 2, y + CARD_ROW_TOP_PADDING + 3),
                entry.right_text,
                fill=CARD_RIGHT_TEXT,
                anchor="ra",
            )
        name_font.draw(draw, (body_x, y + CARD_ROW_TOP_PADDING), name, fill=CARD_NAME)
        if entry.meta_text:
            meta_font.draw(
                draw,
                (body_x, y + CARD_ROW_TOP_PADDING + 32),
                entry.meta_text,
                fill=CARD_META,
            )

        content_y = y + CARD_BODY_TOP
        if lines:
            bubble_h = bubble_size(lines, line_height, body_font_size)
            bubble_w = min(
                body_width,
                max(
                    140,
                    int(max(richtext.measure(line, body_font, face_size) for line in lines))
                    + 2 * CARD_BUBBLE_PAD_X,
                ),
            )
            _soft_round_rect(
                draw,
                (body_x, content_y, body_x + bubble_w, content_y + bubble_h),
                CARD_BUBBLE_RADIUS,
                CARD_BUBBLE,
                outline=CARD_BUBBLE_OUTLINE,
                shadow=CARD_BUBBLE_SHADOW,
                shadow_offset=3,
            )
            for index, line in enumerate(lines):
                richtext.draw(
                    canvas,
                    draw,
                    (
                        body_x + CARD_BUBBLE_PAD_X,
                        content_y + CARD_BUBBLE_PAD_Y - 4 + index * line_height,
                    ),
                    line,
                    body_font,
                    face_size,
                    CARD_TEXT,
                    face_top=CARD_FACE_TOP,
                )
            content_y += bubble_h + 14

        if entry.image_note:
            meta_font.draw(draw, (body_x, content_y + 2), entry.image_note, fill=CARD_META)
            content_y += 30

        if frames:
            image_rows = (len(frames) + thumb_columns - 1) // thumb_columns
            for row_index in range(image_rows):
                row = frames[row_index * thumb_columns : (row_index + 1) * thumb_columns]
                row_height = max(frame_height for _, (_, frame_height) in row)
                row_width = sum(frame_width for _, (frame_width, _) in row) + CARD_THUMB_GAP * (len(row) - 1)
                x = body_x + max(0, (body_width - row_width) // 2)
                for image_path, (frame_width, frame_height) in row:
                    mask = Image.new("L", (frame_width, frame_height), 0)
                    radius = max(6, min(16, frame_width // 2, frame_height // 2))
                    ImageDraw.Draw(mask).rounded_rectangle(
                        (0, 0, frame_width - 1, frame_height - 1), radius=radius, fill=255
                    )
                    canvas.paste(Image.new("RGB", (frame_width, frame_height), CARD_THUMB_BACKDROP), (x, content_y), mask)
                    try:
                        with Image.open(image_path) as source:
                            fitted = ImageOps.contain(
                                source.convert("RGB"),
                                (frame_width, frame_height),
                                method=Image.Resampling.LANCZOS,
                            )
                        frame = Image.new("RGB", (frame_width, frame_height), CARD_THUMB_BACKDROP)
                        frame.paste(
                            fitted,
                            ((frame_width - fitted.width) // 2, (frame_height - fitted.height) // 2),
                        )
                        canvas.paste(frame, (x, content_y), mask)
                    except OSError:
                        pass
                    draw.rounded_rectangle(
                        (x, content_y, x + frame_width - 1, content_y + frame_height - 1),
                        radius=radius,
                        outline=CARD_THUMB_OUTLINE,
                        width=1,
                    )
                    x += frame_width + CARD_THUMB_GAP
                content_y += row_height + CARD_THUMB_GAP

        if entry.extra_images > 0:
            meta_font.draw(
                draw,
                (body_x, content_y + 2),
                f"另有 {entry.extra_images} 张图片",
                fill=CARD_META,
            )
        y += height + CARD_ROW_GAP

    target_dir = directory or CARD_IMAGE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"card-{uuid.uuid4().hex}.png"
    # optimize=True 实测代价极大而收益极小：同一张 960px 卡片保存中位数 170.7 ms（101 KB）
    # vs 36.3 ms（103 KB）——多花 134 ms 只省 2 KB。卡片本来就是要立刻发出去的临时文件，
    # 所以默认不做 PNG 优化。
    canvas.save(path, format="PNG", optimize=PNG_OPTIMIZE)
    return path
