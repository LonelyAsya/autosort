from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from autosort.config import load_config
from autosort.db import Database
from autosort.tagger import TagResult

CONFIG = """
[paths]
source = "src"
target = "out"
database = "test.db"
cache = ".cache"

[classify]
screen_sizes = ["1080x2400"]
art_hint_tags = ["1girl", "solo"]
photo_tags = ["realistic"]
meme_tags = ["meme"]

[tagger]
video_frames = 3

[categories]
default = "Общее"

[[categories.list]]
name = "Пейзажи"
tags_file = "rules/scenery.txt"

[[categories.list]]
name = "Животные"
tags_file = "rules/animals.txt"

[authors]
patterns = [
    { name = "twitter", regex = '^@?(?P<author>[A-Za-z0-9_]{1,15})[-_ ](?P<id>\\d{15,20})' },
    { name = "pixiv", regex = '^(?:illust_)?(?P<id>\\d{6,10})_p\\d+' },
]

[onedrive]
free_space = false
"""


def noise_image(seed: int, size=(256, 256)) -> Image.Image:
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 255, (8, 8, 3), dtype=np.uint8)
    return Image.fromarray(small).resize(size, Image.BICUBIC)


class FakeTagger:
    """Теги по имени файла не знает, поэтому отвечает по цвету первого пикселя."""

    def __init__(self, answers: dict[int, TagResult] | None = None, default: TagResult | None = None):
        self.answers = answers or {}
        self.default = default or TagResult(general={"1girl": 0.9})

    def __call__(self):  # Pipeline ждёт фабрику тегера
        return self

    def tag(self, frames):
        key = frames[0].getpixel((0, 0))[0]
        return self.answers.get(key, self.default)


@pytest.fixture
def workspace(tmp_path: Path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "scenery.txt").write_text("# пример\nscenery\nblue sky\n", encoding="utf-8")
    (tmp_path / "rules" / "animals.txt").write_text("cat\n", encoding="utf-8")
    (tmp_path / "config.toml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "src").mkdir()
    return tmp_path


@pytest.fixture
def cfg(workspace):
    return load_config(workspace / "config.toml")


@pytest.fixture
def db(cfg):
    d = Database(cfg.database)
    yield d
    d.close()
