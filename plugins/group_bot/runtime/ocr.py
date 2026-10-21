"""用 NapCat 的本地 OCR 读图里的字（`ocr_image` 动作，走 QQ 自带的 Windows OCR）。

为什么值得做：群里大量信息是**截图**（课表、通知、订单、聊天记录）。只给 LLM 一串
``[图片]`` 等于什么都没说；把图里的文字抽出来，总结才有内容。

三条边界：

* **本地**：`ocr_image` 由 NapCat 调 QQ 的本地服务，不经过任何第三方接口；
* **限量**：一次总结只 OCR 前 ``DEFAULT_OCR_LIMIT`` 张（可配置），每张有超时——OCR 是同步
  的屏幕识别，几十张会把一次总结拖到分钟级；
* **失败不致命**：动作不存在、图片取不到、返回结构变了，都只是"这张没字"，不影响总结。
"""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

DEFAULT_OCR_LIMIT = 12
OCR_TIMEOUT = 8.0


def _texts_from(result: Any) -> list[str]:
    """从 ``ocr_image`` 的返回里尽量抽出文本，容忍几种不同形状。"""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            return
        if isinstance(node, dict):
            for key in ("text", "content", "wording"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    found.append(value.strip())
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(result)
    # 去重且保序
    seen: set[str] = set()
    unique: list[str] = []
    for text in found:
        if text not in seen:
            seen.add(text)
            unique.append(text)
    return unique


async def ocr_one(bot: Bot, reference: str) -> str:
    """OCR 一张图（本地路径 / file:// / URL）；失败返回空串。"""
    if not reference:
        return ""
    for payload in ({"image": reference}, {"file": reference}):
        try:
            result = await asyncio.wait_for(
                bot.call_api("ocr_image", **payload), timeout=OCR_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.debug("ocr_image timed out for {}", reference[:60])
            return ""
        except Exception:
            # 动作不存在或参数不对：换一种参数再试一次，都失败就当这张没字。
            continue
        texts = _texts_from(result)
        if texts:
            return " ".join(texts)
    return ""


def _placeholder(reference: str) -> bool:
    """不透明 id（纯十六进制/无扩展名）通常取不到图，别浪费一次 OCR。"""
    name = reference.rsplit("/", 1)[-1]
    return "." not in name and len(name) > 16


async def ocr_images(
    bot: Bot, segments: Iterable[dict[str, Any]], limit: int = DEFAULT_OCR_LIMIT
) -> list[str]:
    """按顺序 OCR 消息里的图片，最多 ``limit`` 张，返回每张的文字。"""
    references: list[str] = []
    for segment in segments:
        if str(segment.get("type")) != "image":
            continue
        data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
        for key in ("file", "url", "path"):
            value = str(data.get(key) or "").strip()
            if value and not (key == "file" and _placeholder(value)):
                references.append(value)
                break
        if len(references) >= limit:
            break
    texts: list[str] = []
    for reference in references[:limit]:
        text = await ocr_one(bot, reference)
        if text:
            texts.append(text)
    return texts
