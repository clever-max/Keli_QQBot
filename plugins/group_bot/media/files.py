"""把「原文件」原样发到群或私聊。

漂流瓶（精华文件）和撤回记录（文件撤回）都走这里，保证同一类消息的发送方式一致：

* 群里：OneBot ``upload_group_file``（QQ 客户端会显示成文件消息）；
* 私聊：OneBot ``upload_private_file``。

调用方负责准备本地路径（归档副本或下载好的临时文件）；这里只负责上传与失败检查。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nonebot.adapters.onebot.v11 import Bot


def file_name(part: dict[str, Any], index: int = 0) -> str:
    """文件段里的人类可读文件名；拿不到就用序号兜底。"""
    for key in ("file_name", "name"):
        name = str(part.get(key) or "").strip()
        if name:
            return Path(name).name[:120]
    raw = str(part.get("file") or "").strip()
    if raw and not raw.startswith(("http://", "https://", "file://", "/")):
        candidate = Path(raw).name
        if candidate and "." in candidate:
            return candidate[:120]
    return f"文件{index + 1}"


def file_size_text(part: dict[str, Any]) -> str:
    try:
        size = int(part.get("file_size") or part.get("size") or 0)
    except (TypeError, ValueError):
        return ""
    if size <= 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


async def send_original_file(
    bot: Bot,
    path: Path,
    name: str,
    *,
    group_id: int | None = None,
    user_id: int | None = None,
) -> None:
    """把 ``path`` 以 ``name`` 发出去；失败抛异常，由调用方决定退次数/提示。"""
    if group_id is not None:
        result = await bot.call_api(
            "upload_group_file", group_id=int(group_id), file=str(path), name=name
        )
    elif user_id is not None:
        result = await bot.call_api(
            "upload_private_file", user_id=int(user_id), file=str(path), name=name
        )
    else:
        raise ValueError("send_original_file needs group_id or user_id")
    if isinstance(result, dict) and result.get("status") == "failed":
        raise RuntimeError(f"OneBot failed to upload {name}")
