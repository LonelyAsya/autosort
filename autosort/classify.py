"""Разделение на камеру / арты / мемы / скриншоты / ручной разбор."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config
from .media import MediaInfo

CAMERA_NAME = re.compile(r"^(IMG|VID|PXL|DSC|MVIMG|PANO|BURST)[_-]?\d{8}", re.IGNORECASE)
SCREENSHOT_NAME = re.compile(
    r"(screenshot|screen_shot|screenrecord|screen_recording|скриншот|снимок экрана|запись экрана)",
    re.IGNORECASE,
)
MEME_NAME = re.compile(r"(\bmeme|мем)", re.IGNORECASE)
ART_NAMES = [
    re.compile(r"^(illust_)?\d{6,10}_p\d+", re.IGNORECASE),  # Pixiv
    re.compile(r"^(?=[^.]*[A-Z])(?=[^.]*[a-z])(?=[^.]*\d)[A-Za-z0-9_-]{15}(\.|$)"),  # медиа-id Twitter/X
    re.compile(r"^[0-9a-f]{32}(\.|$)", re.IGNORECASE),  # md5 с бору-сайтов
    re.compile(r"^__.+__"),  # Danbooru: __персонаж_drawn_by_автор__md5
    re.compile(r"(danbooru|gelbooru|yande\.re|konachan|sankaku|rule34|e621|pixiv)", re.IGNORECASE),
]


@dataclass
class Decision:
    type: str  # camera | art | meme | screenshot | review
    reason: str


def pre_classify(name: str, info: MediaInfo, cfg: Config) -> Decision | None:
    """Решение по имени и служебным данным. None — нужны теги."""
    if SCREENSHOT_NAME.search(name):
        return Decision("screenshot", "имя скриншота")
    if info.camera:
        return Decision("camera", f"данные камеры: {info.camera}")
    if info.kind == "video" and info.has_location:
        return Decision("camera", "в видео записано место съёмки")
    if info.kind == "image" and (info.width, info.height) in cfg.screen_sizes:
        return Decision("screenshot", f"размер экрана {info.width}x{info.height}")
    if MEME_NAME.search(name):
        return Decision("meme", "имя похоже на мем")
    for pattern in ART_NAMES:
        if pattern.search(name):
            return Decision("art", "имя как у арта с сайта")
    if info.kind == "animation":
        return Decision("art", "анимация без данных камеры")
    # IMG_2024… без EXIF — частый случай для картинок из мессенджеров,
    # поэтому одно имя ничего не решает: смотрим теги.
    return None


def classify_by_tags(general: dict[str, float], name: str, cfg: Config) -> Decision:
    def best(tags: set[str]) -> tuple[str, float]:
        found = [(t, general[t]) for t in tags if t in general]
        return max(found, key=lambda x: x[1]) if found else ("", 0.0)

    tag, score = best(cfg.meme_tags)
    if score >= cfg.meme_threshold:
        return Decision("meme", f"тег {tag} ({score:.2f})")
    tag, score = best(cfg.photo_tags)
    if score >= cfg.art_threshold:
        hint = ", имя как у камеры" if CAMERA_NAME.search(name) else ""
        return Decision("review", f"похоже на фото без данных камеры: {tag}{hint}")
    tag, score = best(cfg.art_hint_tags)
    if score >= cfg.art_threshold:
        return Decision("art", f"тег {tag} ({score:.2f})")
    return Decision("review", "нет уверенных признаков")
