"""Разбор одного файла: метаданные, хэши, тип, теги, категория, автор."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from PIL import Image

from . import authors
from .classify import Decision, classify_by_tags, pre_classify
from .config import Config
from .hashing import phash_hex, save_thumb, sha256_file, thumb_path
from .media import MediaInfo, probe, sample_frames
from .tagger import TagResult, universe_of


class Tagger(Protocol):
    def tag(self, frames: list[Image.Image]) -> TagResult: ...


@dataclass
class Analysis:
    info: MediaInfo
    sha256: str
    phash: str | None
    type: str
    reason: str
    category: str | None = None
    author: str | None = None
    source: str | None = None
    tags: list[tuple[str, str, float]] = field(default_factory=list)

    def fields(self) -> dict:
        i = self.info
        return dict(kind=i.kind, width=i.width, height=i.height, duration=i.duration, camera=i.camera,
                    taken_at=i.taken_at, sha256=self.sha256, phash=self.phash, type=self.type,
                    reason=self.reason, category=self.category, author=self.author, source=self.source)


def match_categories(tags: dict[str, float], cfg: Config) -> list[str]:
    """Все подходящие категории в порядке приоритета. tags — обычные теги и персонажи вместе."""
    present = set(tags)
    return [c.name for c in cfg.categories if c.tags & present]


def hash_media(path: Path, cfg: Config) -> tuple[MediaInfo, str, str | None, list[Image.Image]]:
    """Метаданные, sha256, отпечатки кадров; заодно сохраняет миниатюру."""
    info = probe(path)
    sha = sha256_file(path)
    frames = sample_frames(path, info, cfg.video_frames)
    phash = ",".join(phash_hex(f) for f in frames) or None
    if frames:
        save_thumb(frames[len(frames) // 2], thumb_path(cfg.cache_dir, sha))
    return info, sha, phash, frames


class Analyzer:
    def __init__(self, cfg: Config, get_tagger: Callable[[], Tagger | None], sauce=None):
        self.cfg = cfg
        self.get_tagger = get_tagger
        self.sauce = sauce

    def analyze(self, path: Path) -> Analysis:
        cfg = self.cfg
        info, sha, phash, frames = hash_media(path, cfg)
        decision = pre_classify(path.name, info, cfg)

        result: TagResult | None = None
        if (decision is None or decision.type == "art") and frames:
            tagger = self.get_tagger()
            if tagger is not None:
                result = tagger.tag(frames)
        if decision is None:
            decision = (classify_by_tags(result.general, path.name, cfg) if result
                        else Decision("review", "нет признаков, а тегер недоступен"))

        a = Analysis(info, sha, phash, decision.type, decision.reason)
        if result:
            a.tags += [("general", t, s) for t, s in result.general.items()]
            a.tags += [("character", t, s) for t, s in result.characters.items()]
            universes: dict[str, float] = {}
            for char, score in result.characters.items():
                if u := universe_of(char):
                    universes[u] = max(score, universes.get(u, 0.0))
            a.tags += [("universe", u, s) for u, s in universes.items()]
            if result.rating:
                top = max(result.rating, key=result.rating.get)
                a.tags.append(("rating", top, result.rating[top]))

        if a.type == "art":
            cats = match_categories({**result.general, **result.characters} if result else {}, cfg)
            a.category = cats[0] if cats else cfg.default_category
            a.tags += [("category", c, 1.0) for c in cats[1:]]
            a.author, a.source = authors.from_filename(path.name, cfg.author_patterns)
            if a.author is None and self.sauce is not None and info.kind == "image":
                try:
                    author, source = self.sauce.lookup(path)
                    if author:
                        a.author, a.source = author, source
                except Exception:  # сеть/лимит — не повод останавливать сортировку
                    pass
        return a
