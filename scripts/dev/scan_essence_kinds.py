"""扫一遍库里所有消息，检查精华链路能否正确处理（文件类 / 多图 / 未知类型 / 空文本）。"""

from __future__ import annotations

import collections
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import nonebot  # noqa: E402
nonebot.init(driver="~fastapi", allowed_group_ids="123450001")
from plugins.group_bot.bottle.pool import (  # noqa: E402
    essence_image_segments,
    essence_text,
    essence_video_ref,
)
from plugins.group_bot.common.render import render_message  # noqa: E402

DB = Path(__file__).resolve().parents[2] / "data" / "messages.sqlite3"
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
db.row_factory = sqlite3.Row

types: Counter[str] = Counter()
image_counts: Counter[int] = Counter()
empty_text = 0
file_messages = 0
multi_image_messages = 0
video_messages = 0
unknown_types: Counter[str] = Counter()
samples: dict[str, str] = {}
total = 0

KNOWN = {
    "text", "image", "at", "file", "record", "video", "face", "mface", "json",
    "xml", "reply", "forward", "node", "share", "location", "music", "dice",
    "rps", "poke", "contact", "markdown", "rps", "shake", "anonymous", "dice",
}

for row in db.execute("SELECT group_id, message_id, message_json FROM messages"):
    total += 1
    try:
        segments = json.loads(row["message_json"])
    except (json.JSONDecodeError, TypeError):
        continue
    if not isinstance(segments, list):
        continue
    for segment in segments:
        kind = str(segment.get("type") or "")
        types[kind] += 1
        if kind and kind not in KNOWN:
            unknown_types[kind] += 1
            samples.setdefault(kind, json.dumps(segment, ensure_ascii=False)[:120])

    item = {"content": segments}
    images = essence_image_segments(item)
    image_counts[len(images)] += 1
    if images:
        if len(images) > 1:
            multi_image_messages += 1
    if any(str(s.get("type")) == "file" for s in segments):
        file_messages += 1
        samples.setdefault(
            "file",
            render_message(segments)[:120],
        )
    if essence_video_ref(item):
        video_messages += 1
    if not essence_text(item, row["group_id"]) and essence_text(item, row["group_id"]) == "":
        empty_text += 1

print(f"=== 全库扫描（{total} 条消息）===")
print("段类型分布（前 12）：")
for kind, count in types.most_common(12):
    print(f"  {kind or '(空)':<12} {count}")

print(f"\n含文件的单条消息     {file_messages}")
print(f"含多图（>1 张）      {multi_image_messages}")
print(f"含视频               {video_messages}")
print(f"文本渲染为空         {empty_text}")
print(f"图片张数分布         {dict(sorted(image_counts.items()))}")
if unknown_types:
    print("\n⚠ 程序里没有专门标签的段类型：")
    for kind, count in unknown_types.most_common():
        print(f"  {kind:<12} {count} 次   样例 {samples.get(kind, '')[:100]}")
else:
    print("\n所有段类型都有标签（不会出现 [xxx] 这种生疏占位）")

print("\n=== 文件类样例渲染 ===")
print("  " + (samples.get("file") or "（库里没有文件类消息）"))
db.close()
