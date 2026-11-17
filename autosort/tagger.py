"""Локальный тегер WD14 (ONNX). Картинки никуда не отправляются."""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from .media import flatten

RATING, GENERAL, CHARACTER = 9, 0, 4
_CHARACTER_UNIVERSE = re.compile(r"^(.+)_\((.+)\)$")


@dataclass
class TagResult:
    general: dict[str, float] = field(default_factory=dict)
    characters: dict[str, float] = field(default_factory=dict)
    rating: dict[str, float] = field(default_factory=dict)


def universe_of(character: str) -> str | None:
    """admiral_hipper_(azur_lane) → azur_lane. Без скобок вселенная неизвестна."""
    m = _CHARACTER_UNIVERSE.match(character)
    return m.group(2) if m else None


def merge(results: list[TagResult]) -> TagResult:
    """Теги нескольких кадров: берём максимальную уверенность по каждому тегу."""
    out = TagResult()
    for r in results:
        for src, dst in ((r.general, out.general), (r.characters, out.characters), (r.rating, out.rating)):
            for tag, score in src.items():
                dst[tag] = max(score, dst.get(tag, 0.0))
    return out


class WD14Tagger:
    def __init__(self, repo: str, device: str, general_threshold: float, character_threshold: float):
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download

        model_path = hf_hub_download(repo, "model.onnx")
        tags_path = hf_hub_download(repo, "selected_tags.csv")
        providers = ["CPUExecutionProvider"]
        if device == "cuda":
            providers.insert(0, "CUDAExecutionProvider")
        self.session = ort.InferenceSession(model_path, providers=providers)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.size = inp.shape[1]
        with open(tags_path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.names = [r["name"] for r in rows]
        self.categories = np.array([int(r["category"]) for r in rows])
        self.general_threshold = general_threshold
        self.character_threshold = character_threshold

    def _prepare(self, img: Image.Image) -> np.ndarray:
        img = flatten(img)
        side = max(img.size)
        square = Image.new("RGB", (side, side), (255, 255, 255))
        square.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
        square = square.resize((self.size, self.size), Image.BICUBIC)
        return np.asarray(square, dtype=np.float32)[:, :, ::-1]  # модель ждёт BGR

    def _tag_one(self, img: Image.Image) -> TagResult:
        batch = self._prepare(img)[None, ...]
        probs = self.session.run(None, {self.input_name: batch})[0][0]
        result = TagResult()
        for name, cat, p in zip(self.names, self.categories, probs):
            p = float(p)
            if cat == GENERAL and p >= self.general_threshold:
                result.general[name] = p
            elif cat == CHARACTER and p >= self.character_threshold:
                result.characters[name] = p
            elif cat == RATING:
                result.rating[name] = p
        return result

    def tag(self, frames: list[Image.Image]) -> TagResult:
        return merge([self._tag_one(f) for f in frames])
