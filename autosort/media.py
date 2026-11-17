"""Чтение служебных данных (EXIF, ffprobe) и кадров из картинок, GIF и видео."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # HEIC просто не будет читаться
    pass

IMAGE_EXT = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp"}

# Чтобы на Windows не мигали чёрные окна ffmpeg.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def find_tool(name: str) -> str | None:
    """ffmpeg/ffprobe: сначала из комплекта программы (exe), потом из PATH."""
    if getattr(sys, "frozen", False):
        for base in (Path(getattr(sys, "_MEIPASS", "")), Path(sys.executable).parent):
            for candidate in (base / "ffmpeg" / f"{name}.exe", base / f"{name}.exe"):
                if candidate.exists():
                    return str(candidate)
    return shutil.which(name)


def is_media(path: Path) -> bool:
    ext = path.suffix.lower()
    return ext in IMAGE_EXT or ext in VIDEO_EXT


@dataclass
class MediaInfo:
    kind: str  # image | animation | video
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    camera: str | None = None  # «Производитель Модель» из EXIF/метаданных видео
    taken_at: str | None = None  # ISO-дата съёмки
    has_location: bool = False


def probe(path: Path) -> MediaInfo:
    if path.suffix.lower() in VIDEO_EXT:
        return _probe_video(path)
    return _probe_image(path)


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    return str(value).strip("\x00 ").strip()


def _parse_exif_dt(value) -> str | None:
    text = _clean(value)
    if len(text) < 19:
        return None
    return text[:10].replace(":", "-") + "T" + text[11:19]


def _probe_image(path: Path) -> MediaInfo:
    with Image.open(path) as im:
        animated = getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1
        info = MediaInfo("animation" if animated else "image", *im.size)
        exif = im.getexif()
        camera = " ".join(x for x in (_clean(exif.get(0x010F)), _clean(exif.get(0x0110))) if x)
        info.camera = camera or None
        sub = exif.get_ifd(0x8769)
        info.taken_at = _parse_exif_dt(sub.get(0x9003) or exif.get(0x0132))
        info.has_location = bool(exif.get_ifd(0x8825))
    return info


def _probe_video(path: Path) -> MediaInfo:
    info = MediaInfo("video")
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return info
    out = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, timeout=120, **NO_WINDOW,
    )
    data = json.loads(out.stdout or b"{}")
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("width"):
            info.width, info.height = stream["width"], stream["height"]
            break
    fmt = data.get("format", {})
    if fmt.get("duration"):
        info.duration = float(fmt["duration"])
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    make = tags.get("com.apple.quicktime.make") or tags.get("com.android.manufacturer") or ""
    model = tags.get("com.apple.quicktime.model") or tags.get("com.android.model") or ""
    info.camera = " ".join(x for x in (make, model) if x) or None
    info.has_location = any("location" in k for k in tags)
    if tags.get("creation_time"):
        info.taken_at = tags["creation_time"][:19]
    return info


def flatten(img: Image.Image) -> Image.Image:
    """RGB-копия с прозрачностью, залитой белым."""
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[3])
        return bg
    return img.convert("RGB")


def _positions(n: int) -> list[float]:
    return [(i + 0.5) / n for i in range(n)]


def sample_frames(path: Path, info: MediaInfo, n: int) -> list[Image.Image]:
    """Кадры для тегов и отпечатков: сама картинка или n равномерных кадров."""
    if info.kind == "image":
        with Image.open(path) as im:
            return [flatten(im)]
    if info.kind == "animation":
        frames = []
        with Image.open(path) as im:
            total = im.n_frames
            for pos in _positions(min(n, total)):
                im.seek(min(int(pos * total), total - 1))
                frames.append(flatten(im))
        return frames
    return _video_frames(path, info.duration, n)


def _video_frames(path: Path, duration: float | None, n: int) -> list[Image.Image]:
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        return []
    times = [p * duration for p in _positions(n)] if duration else [0.0]
    frames = []
    for t in times:
        out = subprocess.run(
            [ffmpeg, "-v", "error", "-ss", f"{t:.2f}", "-i", str(path),
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=120, **NO_WINDOW,
        )
        if out.stdout:
            frames.append(flatten(Image.open(io.BytesIO(out.stdout))))
    return frames
