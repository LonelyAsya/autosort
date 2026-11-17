"""Загрузка настроек из config.toml и списков тегов из текстовых файлов."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

FOLDER_KEYS = ("camera", "art", "meme", "screenshot", "review")

DEFAULT_FOLDERS = {
    "camera": "Камера",
    "art": "Арты",
    "meme": "Мемы",
    "screenshot": "Скриншоты",
    "review": "На ручной разбор",
}


def normalize_tag(tag: str) -> str:
    return tag.strip().lower().replace(" ", "_")


def read_tag_file(path: Path) -> set[str]:
    tags = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            tags.add(normalize_tag(line))
    return tags


@dataclass
class Category:
    name: str
    tags: set[str]


@dataclass
class AuthorPattern:
    name: str
    regex: re.Pattern


@dataclass
class Config:
    base_dir: Path
    source: Path
    target: Path
    database: Path
    cache_dir: Path
    folders: dict[str, str]
    by_month: bool
    screen_sizes: set[tuple[int, int]]
    art_hint_tags: set[str]
    art_threshold: float
    photo_tags: set[str]
    meme_tags: set[str]
    meme_threshold: float
    tagger_enabled: bool
    tagger_model: str
    tagger_device: str
    general_threshold: float
    character_threshold: float
    video_frames: int
    categories: list[Category]
    default_category: str
    author_patterns: list[AuthorPattern]
    saucenao_enabled: bool
    saucenao_api_key: str
    saucenao_min_similarity: float
    saucenao_delay: float
    phash_distance: int
    onedrive_free_space: bool
    onedrive_reserve_bytes: int


def _parse_size(text: str) -> tuple[int, int]:
    w, h = text.lower().replace("х", "x").split("x")
    return int(w), int(h)


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    base = path.parent

    def resolve(value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else base / p

    paths = raw.get("paths", {})
    classify = raw.get("classify", {})
    tagger = raw.get("tagger", {})
    cats = raw.get("categories", {})
    authors = raw.get("authors", {})
    sauce = authors.get("saucenao", {})
    onedrive = raw.get("onedrive", {})

    sizes = set()
    for s in classify.get("screen_sizes", []):
        w, h = _parse_size(s)
        sizes.add((w, h))
        sizes.add((h, w))

    categories = [
        Category(c["name"], read_tag_file(resolve(c["tags_file"])))
        for c in cats.get("list", [])
    ]

    return Config(
        base_dir=base,
        source=resolve(paths["source"]),
        target=resolve(paths["target"]),
        database=resolve(paths.get("database", "autosort.db")),
        cache_dir=resolve(paths.get("cache", ".cache")),
        folders={**DEFAULT_FOLDERS, **raw.get("folders", {})},
        by_month=raw.get("layout", {}).get("by_month", True),
        screen_sizes=sizes,
        art_hint_tags={normalize_tag(t) for t in classify.get("art_hint_tags", [])},
        art_threshold=float(classify.get("art_threshold", 0.5)),
        photo_tags={normalize_tag(t) for t in classify.get("photo_tags", [])},
        meme_tags={normalize_tag(t) for t in classify.get("meme_tags", [])},
        meme_threshold=float(classify.get("meme_threshold", 0.6)),
        tagger_enabled=bool(tagger.get("enabled", True)),
        tagger_model=tagger.get("model", "SmilingWolf/wd-swinv2-tagger-v3"),
        tagger_device=tagger.get("device", "cuda"),
        general_threshold=float(tagger.get("general_threshold", 0.35)),
        character_threshold=float(tagger.get("character_threshold", 0.85)),
        video_frames=int(tagger.get("video_frames", 5)),
        categories=categories,
        default_category=cats.get("default", "Общее"),
        author_patterns=[
            AuthorPattern(p["name"], re.compile(p["regex"], re.IGNORECASE))
            for p in authors.get("patterns", [])
        ],
        saucenao_enabled=bool(sauce.get("enabled", False)),
        saucenao_api_key=sauce.get("api_key", ""),
        saucenao_min_similarity=float(sauce.get("min_similarity", 80)),
        saucenao_delay=float(sauce.get("delay_seconds", 6)),
        phash_distance=int(raw.get("dupes", {}).get("phash_distance", 6)),
        onedrive_free_space=bool(onedrive.get("free_space", True)),
        onedrive_reserve_bytes=int(float(onedrive.get("reserve_gb", 3)) * 1024**3),
    )
