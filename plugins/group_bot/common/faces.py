"""QQ 内置表情（小黄脸）的定位与缓存。

卡片正文里的表情要**画成 QQ 里那个表情**，而不是 ``[表情]`` 占位符。这里负责把
``face`` 段的 id / faceText 变成一个可以贴进卡片的 PNG：

1. ``data/face_cache/s{id}.png``：已经解析好的 64px 图（与 QQ 安装位置、网络都无关）；
2. 本机 QQ 客户端的 ``sysface_res/static/s{id}.png``（没有就试 ``apng/``，取第一帧）：
   这是客户端自己用的图，和聊天窗口里看到的一模一样；
3. 腾讯表情 CDN ``qzonestyle.gtimg.cn/qzone/em/e{code}.gif``。老表情的编号不是
   ``id+10000``（例如 14 → 100、13 → 113），所以 ``code`` 从 ``face_map.json`` 查，
   只有查不到的新表情才用 ``id+10000`` 兜底。

任何一步失败都只记日志并返回 ``None``，由调用方退回文字：一张卡片不能因为一个
表情没图就整张失败。

``face_map.json`` 是 id → 名字 / qzone 编号的映射，取自 QQ 客户端的
``Emoji/emoji-resource/face_config.json``（``sysface`` 段）；新版 QQ 增加了表情时
重新生成这份 JSON 即可。
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from nonebot import logger
from PIL import Image

from ..core.config import FACE_CACHE_DIR, QQ_FACE_DIR

# 卡片里最终贴的是 64px 的透明 PNG；再大对 23px 的字号没有意义，还会拖慢渲染。
FACE_IMAGE_PX = 64
# 表情图最多这么大，防止 CDN 返回异常大文件。
MAX_FACE_BYTES = 4 * 1024 * 1024
# 网络兜底最多等多久：渲染在线程里跑，但也不能让一张卡片等上几十秒。
REMOTE_TIMEOUT = 4
# 网络刚失败过就先别一张张重试：否则一张卡片里有 10 个没缓存的表情，最坏要等 40s。
NETWORK_COOLDOWN = 60

# QQ 表情段在纯文本里的序列化格式：``[表情:387:太好笑]`` / ``[表情:13:呲牙]`` /
# ``[表情:太好笑]``（没有数字 id 的 mface）。richtext 用同一个正则把它还原成图。
TOKEN_RE = r"\[表情:(?:(\d+)(?::([^\]\n]*))?|([^\]\n]*))\]"

_MAP_PATH = Path(__file__).with_name("face_map.json")
try:
    FACE_MAP: dict[str, dict[str, str]] = json.loads(_MAP_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):  # pragma: no cover - 只在文件被删坏时发生
    FACE_MAP = {}

_ROOTS: list[Path] | None = None
_RESOLVED: dict[str, Path | None] = {}
_PIXMAPS: dict[str, Image.Image] = {}
_SCALED: dict[tuple[str, int], Image.Image] = {}
# 网络失败后的冷却截止时间（time.monotonic）；期间不再尝试下载，直接走文字兜底。
_network_blocked_until = 0.0
# 名字 → id：mface 只带 summary（例如 "[微笑]"），没有数字 id 时靠它找回图。
_NAME_TO_ID: dict[str, int] = {}
for _face_id, _entry in FACE_MAP.items():
    _name = str(_entry.get("name") or "").strip()
    if _name and _name not in _NAME_TO_ID and _face_id.isdigit():
        _NAME_TO_ID[_name] = int(_face_id)


def _discover_roots() -> list[Path]:
    """候选的 ``sysface_res`` 目录：显式配置优先，其次自动探测常见安装位置。"""
    roots: list[Path] = []
    if QQ_FACE_DIR:
        base = Path(QQ_FACE_DIR).expanduser()
        roots += [
            base,
            base / "sysface_res",
            base / "sysface_res" / "static",
            base / "emoji-resource" / "sysface_res",
            base / "static",
        ]
        # 直接指到 static/ 时，图就在这一层。
        if base.name.lower() == "static":
            roots.append(base.parent)
    for documents in ("Documents", "OneDrive/Documents"):
        tencent = Path.home() / documents / "Tencent Files"
        if not tencent.is_dir():
            continue
        roots.append(tencent / "nt_qq" / "global" / "nt_data" / "Emoji" / "emoji-resource" / "sysface_res")
        # 多账号目录：Tencent Files/<uin>/nt_qq/global/...
        roots.extend(sorted(tencent.glob("*/nt_qq/global/nt_data/Emoji/emoji-resource/sysface_res")))

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _roots() -> list[Path]:
    global _ROOTS
    if _ROOTS is None:
        _ROOTS = _discover_roots()
    return _ROOTS


def face_name(face_id: int | str) -> str:
    """表情 id → 中文名（来自 ``face_map.json``）；没有记录就是空串。"""
    entry = FACE_MAP.get(str(face_id).strip())
    return str(entry.get("name") or "") if entry else ""


def face_id_for_name(name: str) -> int | None:
    """中文名 → 表情 id；重名只认第一个（``face_map.json`` 里同名极少）。"""
    return _NAME_TO_ID.get(str(name or "").strip())


def _clean_label(value: Any) -> str:
    """把 faceText / summary 收成可以放进 ``[表情:…]`` 的一段文字。"""
    text = " ".join(str(value or "").split()).strip("/").strip()
    text = text.strip("[]").strip().replace("[", "").replace("]", "")
    return text[:24]


def face_token(data: dict[str, Any]) -> str:
    """把一个 ``face`` / ``mface`` 段序列化成纯文本里的表情标记。"""
    if not isinstance(data, dict):
        return "[表情]"
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    label = _clean_label(raw.get("faceText") or data.get("summary") or "")
    face_id = str(data.get("id") or raw.get("faceIndex") or "").strip()
    if not face_id.isdigit():
        face_id = ""
    if not label and face_id:
        label = face_name(face_id)
    if face_id:
        return f"[表情:{face_id}:{label}]" if label else f"[表情:{face_id}]"
    return f"[表情:{label}]" if label else "[表情]"


def _local_image(face_id: str) -> Path | None:
    for root in _roots():
        for sub in ("static", "apng", ""):
            path = (root / sub / f"s{face_id}.png") if sub else (root / f"s{face_id}.png")
            if path.is_file():
                return path
    return None


def _remote_url(face_id: str) -> str:
    entry = FACE_MAP.get(face_id) or {}
    code = str(entry.get("code") or "").strip() or str(int(face_id) + 10000)
    return f"https://qzonestyle.gtimg.cn/qzone/em/e{code}.gif"


def _fetch(url: str) -> bytes | None:
    """下载表情图；``b""`` 表示 404（永久没有），``None`` 表示临时失败/冷却中。"""
    global _network_blocked_until
    if time.monotonic() < _network_blocked_until:
        return None
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=REMOTE_TIMEOUT) as response:
            content = response.read(MAX_FACE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return b""
        logger.debug("Face image HTTP {} for {}", exc.code, url)
        _network_blocked_until = time.monotonic() + NETWORK_COOLDOWN
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.debug("Face image download failed for {}: {}", url, exc)
        _network_blocked_until = time.monotonic() + NETWORK_COOLDOWN
        return None
    if not content or len(content) > MAX_FACE_BYTES:
        return None
    return content


def _write_png(source: Path | bytes, target: Path) -> bool:
    """把任意表情图（GIF/APNG/PNG，可能带多帧）收成 64px 透明 PNG。"""
    try:
        with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
            image.seek(0)
            frame = image.convert("RGBA")
    except (OSError, ValueError) as exc:
        logger.debug("Face image could not be decoded ({}): {}", source, exc)
        return False
    # 有些 CDN 图的画布是 28x28 但画面只占中间一小块，透明边会把表情显得很小；
    # 先裁掉透明边再缩放，卡片里的大小才和文字协调。用 alpha 阈值而不是 getbbox()：
    # 边缘的极淡像素会让 bbox 误以为画满了。
    alpha = frame.getchannel("A").point(lambda value: 255 if value > 24 else 0)
    bounds = alpha.getbbox()
    if bounds:
        frame = frame.crop(bounds)
    # 注意用 resize 而不是 thumbnail：thumbnail 只缩不放，28px 的 CDN 图会一直
    # 是 28px，贴进卡片后比文字还小。统一缩放到 64px 的画布大小。
    scale = min(FACE_IMAGE_PX / frame.width, FACE_IMAGE_PX / frame.height)
    if scale != 1:
        frame = frame.resize(
            (max(1, round(frame.width * scale)), max(1, round(frame.height * scale))),
            Image.Resampling.LANCZOS,
        )
    canvas = Image.new("RGBA", (FACE_IMAGE_PX, FACE_IMAGE_PX), (0, 0, 0, 0))
    canvas.paste(frame, ((FACE_IMAGE_PX - frame.width) // 2, (FACE_IMAGE_PX - frame.height) // 2), frame)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(target, format="PNG")
    except OSError as exc:
        logger.warning("Could not write face cache {}: {}", target, exc)
        return False
    return True


def image_path(face_id: int | str) -> Path | None:
    """该表情的本地 PNG；解析不到返回 ``None``（结果在进程内缓存）。"""
    key = str(face_id).strip()
    if not key.isdigit():
        return None
    if key in _RESOLVED:
        return _RESOLVED[key]

    target = FACE_CACHE_DIR / f"s{key}.png"
    if target.is_file():
        _RESOLVED[key] = target
        return target

    marker = FACE_CACHE_DIR / f"s{key}.missing"
    if marker.is_file():
        _RESOLVED[key] = None
        return None

    local = _local_image(key)
    if local is not None and _write_png(local, target):
        _RESOLVED[key] = target
        return target

    payload = _fetch(_remote_url(key))
    if payload:
        if _write_png(payload, target):
            _RESOLVED[key] = target
            return target
    elif payload == b"":
        try:
            FACE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            marker.write_text("404\n", encoding="utf-8")
        except OSError:
            pass
    else:
        # 临时失败（或网络冷却中）：不写进程缓存，下一次渲染还会再试。
        return None

    _RESOLVED[key] = None
    return None


def image(face_id: int | str) -> Image.Image | None:
    """该表情的 RGBA 图（64px），解析不到返回 ``None``。"""
    path = image_path(face_id)
    if path is None:
        return None
    cached = _PIXMAPS.get(str(path))
    if cached is None:
        try:
            with Image.open(path) as source:
                cached = source.convert("RGBA")
        except OSError:
            return None
        _PIXMAPS[str(path)] = cached
    return cached


def scaled_image(face_id: int | str, size: int) -> Image.Image | None:
    """缩放到卡片实际字号的表情图（按 id+尺寸缓存，避免每行重采样）。"""
    path = image_path(face_id)
    if path is None:
        return None
    key = (str(path), int(size))
    cached = _SCALED.get(key)
    if cached is None:
        cached = image(face_id)
        if cached is None:
            return None
        cached = cached.resize((int(size), int(size)), Image.Resampling.LANCZOS)
        _SCALED[key] = cached
    return cached


def _reset_caches() -> None:
    """测试用：丢掉进程内缓存（不动磁盘上的 cache）。"""
    global _ROOTS, _network_blocked_until
    _ROOTS = None
    _network_blocked_until = 0.0
    _RESOLVED.clear()
    _PIXMAPS.clear()
    _SCALED.clear()
