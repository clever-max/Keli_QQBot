"""卡片正文的「文字 + QQ 表情图」混排。

``render_message`` 把表情段写成 ``[表情:387:太好笑]`` 这样的标记。这里负责把标记
还原成一个个排版单元：普通文字交给字体链画，表情从 :mod:`faces` 取本地 PNG 直接
贴进这一行。标记在换行时是**原子**的——不会出现"[表情:38" 落在行尾的情况。

取不到表情图时退回 ``[太好笑]`` 文字，语义仍在，只是没有图；渲染永远不会因为
一个表情失败。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PIL import Image, ImageDraw

from . import faces
from .textimg import FontChain

_TOKEN = re.compile(faces.TOKEN_RE)
_EMPTY_FALLBACK = "[表情]"


@dataclass(frozen=True)
class Run:
    """一段行内内容：要么是文字，要么是一个 QQ 表情。"""

    text: str = ""
    face_id: int | None = None
    label: str = ""


def parse(text: str) -> list[Run]:
    """把一段正文拆成文字 / 表情交替的排版单元。"""
    source = text or ""
    runs: list[Run] = []
    position = 0
    for match in _TOKEN.finditer(source):
        if match.start() > position:
            runs.append(Run(text=source[position : match.start()]))
        face_id, label = match.group(1), match.group(2)
        if face_id is None:
            face_id, label = None, match.group(3)
        label = (label or "").strip()
        resolved = int(face_id) if face_id else None
        if resolved is None and label:
            # mface 只有 summary（"[微笑]"），名字能对上就仍然画得出图。
            resolved = faces.face_id_for_name(label)
        runs.append(Run(face_id=resolved, label=label))
        position = match.end()
    if position < len(source):
        runs.append(Run(text=source[position:]))
    return runs


def _fallback_text(run: Run) -> str:
    return f"[{run.label}]" if run.label else _EMPTY_FALLBACK


def _face_width(run: Run, font_obj: FontChain, face_size: int) -> float:
    if run.face_id is not None and faces.image_path(run.face_id) is not None:
        return float(face_size)
    return font_obj.textlength(_fallback_text(run))


def measure(line: list[Run], font_obj: FontChain, face_size: int) -> float:
    """一行（已排版）的像素宽度。"""
    width = 0.0
    for run in line:
        if run.face_id is not None:
            width += _face_width(run, font_obj, face_size)
        else:
            width += font_obj.textlength(run.text)
    return width


def wrap(text: str, font_obj: FontChain, max_width: int, face_size: int) -> list[list[Run]]:
    """按宽度折行；返回值每项是一行的排版单元列表。"""
    lines: list[list[Run]] = []
    for paragraph in (text or "").splitlines() or [""]:
        current: list[Run] = []
        width = 0.0
        for run in parse(paragraph):
            if run.face_id is not None:
                run_width = _face_width(run, font_obj, face_size)
                if current and width + run_width > max_width:
                    lines.append(current)
                    current, width = [], 0.0
                current.append(run)
                width += run_width
                continue

            for char in run.text:
                char_width = font_obj.textlength(char)
                if current and width + char_width > max_width:
                    lines.append(current)
                    current, width = [], 0.0
                if current and current[-1].face_id is None:
                    current[-1] = Run(text=current[-1].text + char)
                else:
                    current.append(Run(text=char))
                width += char_width
        lines.append(current)
    return lines


def draw(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    line: list[Run],
    font_obj: FontChain,
    face_size: int,
    fill: object,
    *,
    face_top: int = 0,
) -> None:
    """把一行画到 ``canvas`` 上；表情图按 ``face_size`` 缩放后贴在文字基线上。"""
    x, y = xy
    for run in line:
        if run.face_id is not None:
            pixmap = faces.scaled_image(run.face_id, face_size)
            if pixmap is not None:
                canvas.paste(pixmap, (int(x), int(y) + face_top), pixmap)
                x += face_size
                continue

        text = run.text if run.face_id is None else _fallback_text(run)
        if not text:
            continue
        font_obj.draw(draw, (int(x), int(y)), text, fill=fill)
        x += font_obj.textlength(text)
