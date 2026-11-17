"""Точные хэши, «отпечатки внешнего вида» и миниатюры."""
from __future__ import annotations

import hashlib
from pathlib import Path

import imagehash
from PIL import Image


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def phash_hex(img: Image.Image) -> str:
    return str(imagehash.phash(img))


def thumb_path(cache_dir: Path, sha256: str) -> Path:
    return cache_dir / "thumbs" / sha256[:2] / f"{sha256}.jpg"


def save_thumb(img: Image.Image, dest: Path, size: int = 360) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    thumb = img.copy()
    thumb.thumbnail((size, size))
    thumb.save(dest, "JPEG", quality=82)
