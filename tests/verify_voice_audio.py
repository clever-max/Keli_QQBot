"""语音截断的回归测试：MP3 帧解析、按时长截断、缓存与失效、不支持的格式原样返回。

测试素材是**合成的合法 MP3 帧**（MPEG1 Layer III / 128 kbps / 44.1 kHz，每帧 26.12 ms），
不依赖真实歌曲，也不会因为用户换了素材而结果漂移。
"""

from __future__ import annotations

import tempfile

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


import sys
from pathlib import Path

TEMP = Path(tempfile.gettempdir()) / "recall_verify"
LIB = TEMP / "voice_audio_lib"
CACHE = TEMP / "voice_audio_cache"

sys.path.insert(0, str(ROOT))
import nonebot  # noqa: E402

nonebot.init(driver="~fastapi", allowed_group_ids="123450001", authorized_users="100000009")

from plugins.group_bot.media import voice_audio  # noqa: E402

failures: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"ok   {label} -> {got!r}")


def check_true(label: str, value: object) -> None:
    check(label, bool(value), True)


FRAME_MS = 1152 / 44100 * 1000  # 26.122 ms


def make_mp3(path: Path, seconds: float, *, id3: bool = True) -> int:
    """造一个指定时长的合法 MP3（帧头合法、负载全零）。返回帧数。"""
    frame_size = 417  # int(144 * 128000 / 44100)
    header = bytes([0xFF, 0xFB, 0x90, 0x00])  # MPEG1 L3 / 128k / 44100 / 无 padding
    frames = max(1, int(seconds * 1000 / FRAME_MS))
    payload = header + b"\x00" * (frame_size - 4)
    data = b""
    if id3:
        body = b"\x00" * 16
        size = len(body)
        syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
        data = b"ID3" + bytes([4, 0, 0]) + syncsafe + body
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data + payload * frames)
    return frames


def main() -> None:
    for directory in (LIB, CACHE):
        if directory.exists():
            for item in directory.rglob("*"):
                if item.is_file():
                    item.unlink()

    print("=== 1. 帧解析与时长 ===")
    short = LIB / "short.mp3"
    frames = make_mp3(short, 10)
    check("造样例的帧数与时长一致", abs(frames - int(10 * 1000 / FRAME_MS)) <= 1, True)
    duration = voice_audio.mp3_duration(short)
    check_true("时长约 10 秒", duration is not None and 9.9 < duration < 10.1)
    check("解析出的帧数一致", len(voice_audio.mp3_frames(short.read_bytes())), frames)

    print("\n=== 2. 超过上限才截断 ===")
    result = voice_audio.ensure_short(short, 60, cache_dir=CACHE)
    check("本来就不超长 → 原样返回", result, short)

    long = LIB / "long.mp3"
    long_frames = make_mp3(long, 90)
    check_true("90 秒样例够长", voice_audio.mp3_duration(long) > 89)
    trimmed = voice_audio.ensure_short(long, 60, cache_dir=CACHE)
    check_true("产生了截断副本", trimmed != long)
    check_true("副本在缓存目录", CACHE in trimmed.parents)
    trimmed_duration = voice_audio.mp3_duration(trimmed)
    check_true("截断后 ≤ 60 秒", trimmed_duration <= 60.0)
    check_true("截断后尽量接近 60 秒（误差 < 1 帧）", 60.0 - trimmed_duration < FRAME_MS / 1000)
    kept = len(voice_audio.mp3_frames(trimmed.read_bytes()))
    expect = int(60 * 1000 / FRAME_MS)
    check_true("保留的帧数与 60 秒对应", abs(kept - expect) <= 1)
    check_true("输出仍能解析为 MP3", kept > 0)
    check_true("ID3 头被保留", trimmed.read_bytes()[:3] == b"ID3")

    print("\n=== 3. 缓存与失效 ===")
    again = voice_audio.ensure_short(long, 60, cache_dir=CACHE)
    check("第二次命中缓存", again, trimmed)
    # 改一下源文件（大小变化）→ 缓存键变化，应重新生成
    make_mp3(long, 91)
    changed = voice_audio.ensure_short(long, 60, cache_dir=CACHE)
    check_true("源文件变了就重新截断", changed != trimmed)
    check_true("新副本也 ≤ 60 秒", voice_audio.mp3_duration(changed) <= 60.0)

    print("\n=== 4. 上限可配 / 不支持的格式不碰 ===")
    tiny = voice_audio.ensure_short(long, 5, cache_dir=CACHE)
    check_true("5 秒上限生效", voice_audio.mp3_duration(tiny) <= 5.0)
    silk = LIB / "fake.silk"
    silk.write_bytes(b"not really silk")
    check("silk 原样返回（截不了就不动）", voice_audio.ensure_short(silk, 60, cache_dir=CACHE), silk)
    broken = LIB / "broken.mp3"
    broken.write_bytes(b"\x00" * 4096)
    check("坏文件原样返回（不抛异常）", voice_audio.ensure_short(broken, 60, cache_dir=CACHE), broken)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} 项失败：")
        for item in failures:
            print("  -", item)
        sys.exit(1)
    print("全部通过")


main()
