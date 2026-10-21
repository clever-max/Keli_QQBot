"""把超过时长上限的语音**截成前 N 秒**，不依赖 ffmpeg。

为什么自己写：这台机器没有 ffmpeg / ffprobe（NapCat 也没自带），而 QQ 语音一般只支持
60 秒左右。MP3 是**按帧**组织的，每帧自带时长信息，所以在**帧边界**上截断既能精确控制
时长，又不会破坏文件（不需要重新编码，也不会有半个帧导致的爆音）。

支持程度（诚实标注）：

* ``.mp3``：按帧解析并截断，精确到最后不足一帧的位置（误差 < 一帧，约 26 ms）；
* ``.wav``：RIFF PCM，直接截 ``data`` 块并修正长度字段；
* ``.amr``：每帧 20 ms，按帧截断；
* 其它（``.silk`` / ``.flac`` / ``.m4a`` / ``.ogg``）：**不做截断**，原样返回；
  调用方按"截不了"处理（仍然发，交给 NapCat，失败时有兜底文案）。

截断结果会缓存到 ``data/voice/.trimmed/``，文件名带上原始文件的 mtime 与大小，
源文件一改就自动失效——避免每次 `炸乌云` 都重新解析 3 MB 的文件。
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

from nonebot import logger

# MPEG Layer III 的每帧采样数与码率表（索引 0 = free，15 = 非法）。
_MPEG1_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_MPEG2_BITRATES = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)
_SAMPLE_RATES = {
    3: (44100, 48000, 32000),  # MPEG1
    2: (22050, 24000, 16000),  # MPEG2
    0: (11025, 12000, 8000),  # MPEG2.5
}
LAYER3 = 1


@dataclass(frozen=True)
class Frame:
    offset: int
    size: int
    seconds: float


def _id3v2_size(data: bytes) -> int:
    """返回 ID3v2 标签占用的字节数（没有则 0）。"""
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    # 后 4 字节是 syncsafe 整数：每个字节只用低 7 位
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return 10 + size + (10 if data[5] & 0x10 else 0)  # 有 footer 时再多 10 字节


def _parse_header(data: bytes, offset: int) -> Frame | None:
    """解析 offset 处的 MP3 帧头；不是合法帧就返回 None。"""
    if offset + 4 > len(data):
        return None
    header = int.from_bytes(data[offset:offset + 4], "big")
    if (header >> 21) & 0x7FF != 0x7FF:  # 11 位同步字
        return None
    version = (header >> 19) & 0x3
    layer = (header >> 17) & 0x3
    if version == 1 or layer != LAYER3:  # 只用 MPEG1/2/2.5 的 Layer III
        return None
    bitrate_index = (header >> 12) & 0xF
    rate_index = (header >> 10) & 0x3
    padding = (header >> 9) & 0x1
    if bitrate_index in (0, 15) or rate_index == 3:
        return None
    bitrate = (_MPEG1_BITRATES if version == 3 else _MPEG2_BITRATES)[bitrate_index] * 1000
    sample_rate = _SAMPLE_RATES[version][rate_index]
    samples = 1152 if version == 3 else 576
    coefficient = 144 if version == 3 else 72
    size = int(coefficient * bitrate / sample_rate) + padding
    if size <= 4:
        return None
    return Frame(offset=offset, size=size, seconds=samples / sample_rate)


def mp3_frames(data: bytes) -> list[Frame]:
    """顺序走一遍所有帧。遇到坏帧就尝试向后重新同步，避免整首作废。"""
    start = _id3v2_size(data)
    end = len(data)
    if end - start > 128 and data[end - 128:end - 125] == b"TAG":  # 末尾 ID3v1
        end -= 128
    frames: list[Frame] = []
    offset = start
    misses = 0
    while offset + 4 <= end:
        frame = _parse_header(data, offset)
        if frame is None:
            offset += 1  # 重新同步
            misses += 1
            if misses > 4096 and not frames:
                return []  # 前 4 KB 里一帧都没解析出来，认定不是 MP3
            continue
        misses = 0
        if offset + frame.size > end:
            break
        frames.append(frame)
        offset += frame.size
    return frames


def mp3_duration(path: Path) -> float | None:
    try:
        frames = mp3_frames(path.read_bytes())
    except OSError:
        return None
    if not frames:
        return None
    return sum(frame.seconds for frame in frames)


def _trim_mp3(source: Path, destination: Path, seconds: float) -> tuple[bool, float, float]:
    data = source.read_bytes()
    frames = mp3_frames(data)
    if not frames:
        return False, 0.0, 0.0
    total = 0.0
    kept: list[Frame] = []
    for frame in frames:
        if total + frame.seconds > seconds + 1e-6:
            break
        kept.append(frame)
        total += frame.seconds
    if not kept:
        return False, 0.0, 0.0
    source_total = sum(frame.seconds for frame in frames)
    if len(kept) == len(frames):
        return True, total, source_total  # 一秒都不用截
    pieces = [data[: _id3v2_size(data)]]  # 保留 ID3v2 标签（封面/歌名等信息）
    for frame in kept:
        pieces.append(data[frame.offset: frame.offset + frame.size])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"".join(pieces))
    logger.info(
        "Trimmed {} to {:.1f}s ({} of {} frames)", source.name, total, len(kept), len(frames)
    )
    return True, total, source_total


def _trim_wav(source: Path, destination: Path, seconds: float) -> tuple[bool, float, float]:
    data = source.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return False, 0.0, 0.0
    offset = 12
    fmt_chunk = b""
    while offset + 8 <= len(data):
        chunk_id = data[offset:offset + 4]
        chunk_size = int.from_bytes(data[offset + 4:offset + 8], "little")
        body = data[offset + 8: offset + 8 + chunk_size]
        if chunk_id == b"fmt ":
            fmt_chunk = body
        elif chunk_id == b"data":
            if len(fmt_chunk) < 12:
                return False, 0.0, 0.0
            channels = int.from_bytes(fmt_chunk[2:4], "little")
            rate = int.from_bytes(fmt_chunk[4:8], "little")
            bits = int.from_bytes(fmt_chunk[14:16], "little")
            block = max(1, channels * bits // 8)
            source_total = len(body) / block / rate
            keep = min(len(body), int(rate * seconds) * block)
            keep -= keep % block
            if keep >= len(body):
                return True, source_total, source_total  # 不需要截
            out = bytearray()
            out += b"RIFF" + (4 + 8 + len(fmt_chunk) + 8 + keep).to_bytes(4, "little") + b"WAVE"
            out += b"fmt " + len(fmt_chunk).to_bytes(4, "little") + fmt_chunk
            out += b"data" + keep.to_bytes(4, "little") + body[:keep]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(bytes(out))
            return True, keep / block / rate, source_total
        offset += 8 + chunk_size + (chunk_size & 1)
    return False, 0.0, 0.0


def _trim_amr(source: Path, destination: Path, seconds: float) -> tuple[bool, float, float]:
    data = source.read_bytes()
    if not data.startswith(b"#!AMR\n"):
        return False, 0.0, 0.0
    # AMR-NB：每帧固定 20 ms，帧长由每帧首字节的帧类型决定（下表是 payload+1）。
    frame_sizes = (12, 13, 15, 17, 19, 20, 26, 31, 5, 6, 5, 5, 0, 0, 0, 0)
    limit = int(seconds / 0.02)
    offset = 6
    keep_end = offset
    kept = 0
    total = 0
    while offset < len(data):
        kind = (data[offset] >> 3) & 0xF
        size = frame_sizes[kind]
        if size == 0 or offset + size > len(data):
            break
        offset += size
        total += 1
        if total <= limit:
            kept = total
            keep_end = offset
    if total == 0:
        return False, 0.0, 0.0
    source_total = total * 0.02
    if kept >= total:
        return True, source_total, source_total  # 不需要截
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data[:keep_end])
    return True, kept * 0.02, source_total


def ensure_short(source: Path, max_seconds: float, *, cache_dir: Path) -> Path:
    """保证返回一个「不超过 ``max_seconds``」的文件路径（必要时写缓存副本）。

    截不了的情况（格式不支持、解析失败、本来就不超长）一律**返回原文件**：
    宁可让 NapCat 去试、由发送失败的兜底文案兜住，也不要在这里把歌弄丢。
    """
    suffix = source.suffix.lower()
    if suffix not in {".mp3", ".wav", ".amr"}:
        logger.info("Voice {} : format {} cannot be trimmed, sending as-is", source.name, suffix)
        return source

    stat = source.stat()
    # 副本文件名用**纯 ASCII**：中文路径多一个变量（NapCat 的路径/URL 解析对非 ASCII
    # 不可靠，曾直接报「识别URL失败」），所以用歌名的短哈希代替原名，可读性靠日志补。
    digest = hashlib.sha1(source.stem.encode("utf-8")).hexdigest()[:10]
    destination = cache_dir / f"voice-{digest}-{int(stat.st_mtime)}-{stat.st_size}-{int(max_seconds)}s{suffix}"
    if destination.exists() and destination.stat().st_size > 0:
        return destination

    trimmers = {".mp3": _trim_mp3, ".wav": _trim_wav, ".amr": _trim_amr}
    try:
        ok, kept, total = trimmers[suffix](source, destination, max_seconds)
    except (OSError, ValueError, struct.error):
        logger.exception("Could not trim voice {}", source.name)
        return source
    # 没得截（本来就不超长）：把可能写出的副本删掉，直接用原文件，避免无谓的磁盘复制。
    if ok and total > 0 and kept >= total - 1e-6:
        destination.unlink(missing_ok=True)
        return source
    if not ok or not destination.exists() or kept <= 0:
        return source
    return destination
