"""Pure rendering helpers.

Nothing here touches the network or the database, which makes these functions
safe to unit-test in isolation and the lowest-risk part of the package.
"""

from __future__ import annotations

import re
from typing import Any

from .faces import face_token


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _file_label(data: dict[str, Any]) -> str:
    """文件消息：``[文件] 报告.pdf (1.2 MB)``；拿不到名字就只写 ``[文件]``。"""
    name = str(data.get("file_name") or data.get("name") or data.get("file") or "").strip()
    # NapCat 有时把 file 给成不透明 id（纯十六进制、没有扩展名），那种不当名字用。
    if name and ("." not in name or len(name) > 80):
        name = ""
    label = "[文件]"
    if name:
        label += f" {name}"
    size = data.get("file_size") or data.get("size")
    try:
        if size:
            label += f" ({_human_size(int(size))})"
    except (TypeError, ValueError):
        pass
    return label


LABELS = {
    "record": "[语音]",
    "video": "[视频]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "reply": "[回复消息]",
    "forward": "[合并转发]",
    "node": "[转发节点]",
    "share": "[分享]",
    "location": "[位置]",
    "music": "[音乐]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "poke": "[戳一戳]",
    "contact": "[名片]",
    "markdown": "[卡片]",
    "shake": "[窗口抖动]",
    "anonymous": "[匿名消息]",
}


def _media_label(kind: str, data: dict[str, Any]) -> str:
    """Give non-image media a useful, stable summary in text-only cards."""
    if kind == "record":
        label = "[语音]"
    elif kind == "video":
        label = "[视频]"
    else:
        return _file_label(data)
    name = str(data.get("file_name") or data.get("name") or data.get("file") or "").strip()
    if name and "." in name and len(name) <= 80:
        label += f" {name}"
    size = data.get("file_size") or data.get("size")
    try:
        if size:
            label += f" ({_human_size(int(size))})"
    except (TypeError, ValueError):
        pass
    return label


def render_message(
    message_data: list[dict[str, Any]],
    omit_images: bool = False,
    omit_replies: bool = False,
    omit_forwards: bool = False,
) -> str:
    parts: list[str] = []
    images = 0

    def flush_images() -> None:
        """连续的多张图片折成 ``[图片]×N``，避免一整条消息被 [图片] 刷屏。"""
        nonlocal images
        if images == 1:
            parts.append("[图片]")
        elif images > 1:
            parts.append(f"[图片]×{images}")
        images = 0

    for item in message_data:
        kind = item.get("type", "")
        data = item.get("data", {})
        if kind == "image":
            if omit_images:
                continue
            images += 1
            continue
        flush_images()
        if kind == "reply" and omit_replies:
            continue
        if kind == "forward" and omit_forwards:
            continue
        if kind == "text":
            parts.append(str(data.get("text", "")))
        elif kind == "at":
            parts.append(f"@{data.get('qq', '某人')}")
        elif kind in ("record", "video", "file"):
            parts.append(_media_label(kind, data))
        elif kind in ("face", "mface"):
            # 保留 id / 名称，卡片才能把表情画成图而不是占位符（见 common/faces.py）。
            parts.append(face_token(data if isinstance(data, dict) else {}))
        else:
            parts.append(LABELS.get(kind, f"[{kind or '消息'}]"))
    flush_images()
    rendered = "".join(parts).strip()
    if rendered:
        return rendered
    return "" if omit_images else "[非文本消息]"


def clean_model_text(text: str) -> str:
    cleaned = text.strip()
    # Some replies arrive wrapped in a code fence even when told not to.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[A-Za-z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.replace("**", "").replace("##", "")
    # Model output often carries a stray blank line between blocks.
    cleaned = re.sub(r"\n{2,}", "\n", cleaned)
    return cleaned.strip()


def cap_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if "\n" in cut:
        cut = cut.rsplit("\n", 1)[0]
    return cut.rstrip() + "…"
